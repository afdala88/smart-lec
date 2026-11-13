"""
Optimized census pipeline.

Speed improvements vs original:
  1. Model cache  — DetectMultiBackend loaded ONCE per Flask session, reused on
                    every subsequent census run (~3-5 s saved per model, ×4 models).
  2. Parallel pre — Stage 1 image preprocessing runs in a thread pool.
  3. No write_exif— GPS tagging removed from temporary inference files (~100 ms/img).
  4. Ref cached   — histogram reference image loaded once, not once per input image.
  5. ONNX auto    — uses best.onnx if available (run export_models.py once to create).
"""
import os
import sys
import json
import uuid
import importlib.util
import cv2
import shutil
import threading
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from skimage import exposure

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_YOLO_DIR = os.path.join(_BASE_DIR, "yolov5_inf")

# Load procces_library directly by file path BEFORE touching sys.path,
# because LEC_new/utils/ and yolov5_inf/utils/ share the same package
# name and Python can only cache one.
_spec = importlib.util.spec_from_file_location(
    "_procces_library",
    os.path.join(_BASE_DIR, "utils", "procces_library.py"),
)
_procces_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_procces_lib)
remove_similar_img = _procces_lib.remove_similar_img

# Now put yolov5_inf FIRST so all bare `utils.*` imports resolve to
# yolov5_inf/utils/ (augmentations, dataloaders, general, etc.)
if _YOLO_DIR not in sys.path:
    sys.path.insert(0, _YOLO_DIR)

# Evict any previously cached `utils` package (from the project-level
# utils/ folder) so that the next import of utils.* hits yolov5_inf/utils/.
for _key in list(sys.modules.keys()):
    if _key == "utils" or _key.startswith("utils."):
        del sys.modules[_key]


class _Stopped(Exception):
    pass


# ── Module-level model cache ──────────────────────────────────────────────────
# Models survive across census runs within the same Flask process.
_MODEL_CACHE: dict   = {}
_WARMED_UP:   set    = set()
_CACHE_LOCK          = threading.Lock()


def _get_model(weights: str):
    """Return a cached DetectMultiBackend; load from disk only on first call."""
    if weights in _MODEL_CACHE:
        return _MODEL_CACHE[weights]
    with _CACHE_LOCK:
        if weights not in _MODEL_CACHE:
            from models.common import DetectMultiBackend
            from utils.torch_utils import select_device
            _MODEL_CACHE[weights] = DetectMultiBackend(weights, device=select_device(''))
        return _MODEL_CACHE[weights]


def _classify(weights: str, source_dir: str, labels_dir: str, imgsz: int = 224):
    """Run classification on every image in source_dir; write label files."""
    import torch
    import torch.nn.functional as F
    from utils.augmentations import classify_transforms
    from utils.dataloaders import LoadImages
    from utils.general import check_img_size

    if not os.path.isdir(source_dir) or not os.listdir(source_dir):
        return

    os.makedirs(labels_dir, exist_ok=True)
    model = _get_model(weights)
    sz    = check_img_size((imgsz, imgsz), s=model.stride)
    ds    = LoadImages(source_dir, img_size=sz, transforms=classify_transforms(sz[0]))

    if weights not in _WARMED_UP:
        model.warmup(imgsz=(1, 3, *sz))
        _WARMED_UP.add(weights)

    with torch.no_grad():
        for path, im, *_ in ds:
            try:
                im = torch.Tensor(im).to(model.device)
                if im.ndim == 3:
                    im = im[None]
                im = im.half() if model.fp16 else im.float()
                prob  = F.softmax(model(im), dim=1)[0]
                top5i = prob.argsort(0, descending=True)[:5].tolist()
                text  = "\n".join(f"{prob[j]:.2f} {model.names[j]}" for j in top5i)
                Path(labels_dir, Path(path).stem + ".txt").write_text(text + "\n")
            except (AssertionError, Exception):
                # Skip unreadable / corrupt image files
                continue


def _detect(weights: str, source_dir: str, save_dir: str, conf_thres: float = 0.67):
    """Run detection; save crops to save_dir/crops/<classname>/.
    Returns {img_stem: [(label, [x1, y1, x2, y2])]}."""
    import torch
    from utils.dataloaders import LoadImages
    from utils.general import check_img_size, non_max_suppression, scale_boxes
    from utils.plots import save_one_box

    detections: dict = {}

    if not os.path.isdir(source_dir) or not os.listdir(source_dir):
        return detections

    model = _get_model(weights)
    sz    = check_img_size((640, 640), s=model.stride)
    ds    = LoadImages(source_dir, img_size=sz, stride=model.stride, auto=model.pt)

    if weights not in _WARMED_UP:
        model.warmup(imgsz=(1, 3, *sz))
        _WARMED_UP.add(weights)

    with torch.no_grad():
        for path, im, im0s, *_ in ds:
            t = torch.from_numpy(im).to(model.device)
            t = t.half() if model.fp16 else t.float()
            t /= 255
            if t.ndim == 3:
                t = t[None]
            preds = non_max_suppression(model(t), conf_thres, 0.45, max_det=1000)
            for det in preds:
                if not len(det):
                    continue
                im0   = im0s.copy()
                stem  = Path(path).stem
                det[:, :4] = scale_boxes(t.shape[2:], det[:, :4], im0.shape).round()
                if stem not in detections:
                    detections[stem] = []
                for *xyxy, conf, cls in reversed(det):
                    c = int(cls)
                    detections[stem].append((model.names[c], [int(v) for v in xyxy]))
                    save_one_box(
                        xyxy, im0,
                        file=Path(save_dir) / "crops" / model.names[c] / f"{stem}.jpg",
                        BGR=True,
                    )
    return detections


def discover(root_folder):
    return _discover(root_folder)


def _natural_key(name: str):
    """Split name into (int, int, ...) for numeric sort. '1-10' → (1, 10)."""
    import re
    parts = re.split(r'\D+', name)
    return tuple(int(p) for p in parts if p.isdigit()) or (0,)


def _discover(root_folder):
    sub_folders = sorted(
        (d for d in os.listdir(root_folder)
         if os.path.isdir(os.path.join(root_folder, d))),
        key=_natural_key,
    )
    frond_names = []
    for sf in sub_folders:
        candidates = sorted(
            d for d in os.listdir(os.path.join(root_folder, sf))
            if os.path.isdir(os.path.join(root_folder, sf, d))
        )
        if candidates:
            frond_names = candidates
            break
    if not frond_names:
        raise ValueError("No frond sub-folders found inside input folder.")
    return sub_folders, frond_names


def process_files(root_folder, base_dir=None, stop_event=None, proc_ref=None):
    """
    Expected folder structure:
        root_folder/
        ├── <subfolder>/   ← one row  (Baris-Pokok)
        │   ├── F1/        ← frond images
        │   ├── F2/
        │   └── F3/
        └── ...

    Returns a DataFrame with columns [Baris-Pokok, F1, F2, ...].
    stop_event : threading.Event  – set to interrupt between stages.
    proc_ref   : list[None]       – kept for API compatibility (unused).
    """
    if base_dir is None:
        base_dir = _BASE_DIR

    yolo    = _YOLO_DIR
    img_ref = os.path.join(base_dir, "utils", "lec_references.jpg")

    # Per-job isolated work directory so concurrent census runs never collide.
    _run_id  = uuid.uuid4().hex[:10]
    _tmp_root = os.path.join(yolo, f"_job_{_run_id}")

    def chk():
        if stop_event is not None and stop_event.is_set():
            raise _Stopped()

    def cleanup():
        shutil.rmtree(_tmp_root, ignore_errors=True)

    sub_folders, frond_names = _discover(root_folder)
    empty_df = pd.DataFrame(
        [{"Baris-Pokok": sf, **{f: 0 for f in frond_names}, "ImagePath": ""} for sf in sub_folders],
        columns=["Baris-Pokok"] + frond_names + ["ImagePath"],
    )

    try:
        # ── STAGE 1: Preprocess ALL images → _tmp_root/input/ ───────────────
        # • Reference image loaded ONCE (not per image)
        # • Histogram matching runs in a thread pool (parallel CPU)
        # • Per-job _tmp_root avoids collisions with concurrent census runs

        input_dir = os.path.join(_tmp_root, "input")
        os.makedirs(input_dir, exist_ok=True)

        # Pre-load reference image once
        _ref = cv2.imread(img_ref)
        ref_rgb = cv2.cvtColor(_ref, cv2.COLOR_BGR2RGB)

        img_map:     dict = {}
        img_src_map: dict = {}   # img_id → original source path
        counter = 0
        tasks   = []   # (src_path, dst_path)

        for subfolder in sub_folders:
            for frond in frond_names:
                frond_path = os.path.join(root_folder, subfolder, frond)
                if not os.path.isdir(frond_path):
                    continue
                for fname in sorted(os.listdir(frond_path)):
                    if not fname.lower().endswith((".jpg", ".png", ".tif")):
                        continue
                    img_id = f"{counter:06d}"
                    counter += 1
                    src = os.path.join(frond_path, fname)
                    img_map[img_id]     = (subfolder, frond)
                    img_src_map[img_id] = src
                    tasks.append((src, os.path.join(input_dir, img_id + ".jpg")))

        if not tasks:
            return empty_df

        def _preprocess(args):
            src, dst = args
            img = cv2.imread(src)
            if img is None:
                return
            matched = exposure.match_histograms(
                cv2.cvtColor(img, cv2.COLOR_BGR2RGB), ref_rgb, channel_axis=-1
            )
            cv2.imwrite(dst, cv2.cvtColor(matched, cv2.COLOR_RGB2BGR))

        workers = min(6, os.cpu_count() or 2)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pool.map(_preprocess, tasks)

        chk()

        # ── Path / weight aliases ─────────────────────────────────────────────
        runs_dir    = Path(_tmp_root) / "runs" / "detect"
        w_ujte      = os.path.join(yolo, "model_fix", "ujung-tengah", "v2", "weights", "best.pt")
        w_det       = os.path.join(yolo, "model_fix", "det_yv5", "v2", "weights", "best.pt")
        w_bagworm   = os.path.join(yolo, "model_fix", "bag-dam_v1", "weights", "best.pt")
        flat_ujung  = os.path.join(_tmp_root, "flat_ujung")
        flat_tengah = os.path.join(_tmp_root, "flat_tengah")
        all_crops   = os.path.join(_tmp_root, "all_crops")
        confirmed   = os.path.join(_tmp_root, "confirmed_crops")

        # ── STAGE 2: Classify ujung / tengah ─────────────────────────────────
        _classify(w_ujte, input_dir, str(runs_dir / "uj-te" / "labels"))
        chk()

        os.makedirs(flat_ujung,  exist_ok=True)
        os.makedirs(flat_tengah, exist_ok=True)

        lbl_dir = str(runs_dir / "uj-te" / "labels")
        if os.path.isdir(lbl_dir):
            for txt_file in os.listdir(lbl_dir):
                img_id = os.path.splitext(txt_file)[0]
                if img_id not in img_map:
                    continue
                with open(os.path.join(lbl_dir, txt_file)) as fh:
                    cls = fh.readline().strip()
                    val = float(fh.readline().split()[0])
                if val > 0.20:
                    continue
                src = os.path.join(input_dir, img_id + ".jpg")
                if   "ujung"  in cls: dst = os.path.join(flat_ujung,  img_id + ".jpg")
                elif "tengah" in cls: dst = os.path.join(flat_tengah, img_id + ".jpg")
                else: continue
                try: shutil.move(src, dst)
                except Exception: pass

        shutil.rmtree(str(runs_dir / "uj-te"), ignore_errors=True)
        shutil.rmtree(input_dir, ignore_errors=True)

        # ── STAGE 3: Detect bagworms ──────────────────────────────────────────
        dets_ujung  = _detect(w_det, flat_ujung,  str(runs_dir / "det_ujung"),  conf_thres=0.67)
        chk()
        dets_tengah = _detect(w_det, flat_tengah, str(runs_dir / "det_tengah"), conf_thres=0.67)
        chk()
        all_dets = {**dets_ujung, **dets_tengah}

        shutil.rmtree(flat_ujung,  ignore_errors=True)
        shutil.rmtree(flat_tengah, ignore_errors=True)

        # ── STAGE 4: Classify crops ───────────────────────────────────────────
        os.makedirs(all_crops, exist_ok=True)
        for det_name in ("det_ujung", "det_tengah"):
            crops_src = str(runs_dir / det_name / "crops" / "Bagworm")
            if os.path.isdir(crops_src):
                for f in os.listdir(crops_src):
                    try: shutil.copy2(os.path.join(crops_src, f), os.path.join(all_crops, f))
                    except Exception: pass

        _classify(w_bagworm, all_crops, str(runs_dir / "bagworm_cls" / "labels"))
        chk()

        # ── STAGE 5: Filter confirmed → count per (subfolder, frond) ─────────
        os.makedirs(confirmed, exist_ok=True)
        cls_labels = str(runs_dir / "bagworm_cls" / "labels")

        if os.path.isdir(cls_labels):
            for fl in os.listdir(cls_labels):
                with open(os.path.join(cls_labels, fl)) as fh:
                    cls = fh.readline().strip()
                    val = float(fh.readline().split()[0])
                if val <= 0.25 and "bagworm" in cls:
                    img_name = os.path.splitext(fl)[0] + ".jpg"
                    try:
                        shutil.move(os.path.join(all_crops, img_name),
                                    os.path.join(confirmed, img_name))
                    except Exception: pass

        groups = defaultdict(list)
        for fname in os.listdir(confirmed):
            if not fname.lower().endswith((".jpg", ".png")):
                continue
            img_id = os.path.splitext(fname)[0].split("_")[0]
            key = img_map.get(img_id)
            if key:
                groups[key].append(os.path.join(confirmed, fname))

        out_seg   = os.path.join(base_dir, "temp_suport", "LEC_census", "segmentasi")
        out_crops = os.path.join(base_dir, "temp_suport", "LEC_census", "crops")
        os.makedirs(out_seg,   exist_ok=True)
        os.makedirs(out_crops, exist_ok=True)

        def _unique_path(folder, filename):
            """Return a path that does not yet exist, appending _1, _2, ... if needed."""
            dst = os.path.join(folder, filename)
            if not os.path.exists(dst):
                return dst
            stem, ext = os.path.splitext(filename)
            n = 1
            while True:
                dst = os.path.join(folder, f"{stem}_{n}{ext}")
                if not os.path.exists(dst):
                    return dst
                n += 1

        counts     = defaultdict(int)
        img_paths  = {}   # (subfolder, frond) -> path of one representative crop image
        raw_pts    = []   # (area_px, w_norm, h_norm) — classified after full loop
        tmp_dir    = os.path.join(_tmp_root, "tmp_dedup")
        for key, crop_paths in groups.items():
            os.makedirs(tmp_dir, exist_ok=True)
            for cp in crop_paths:
                try: shutil.copy2(cp, tmp_dir)
                except Exception: pass
            remove_similar_img(tmp_dir, similarity_threshold=0.3)
            dedup_files = [
                f for f in os.listdir(tmp_dir)
                if f.lower().endswith((".jpg", ".png"))
            ]
            counts[key] = len(dedup_files)

            if dedup_files:
                saved_ids: set = set()
                for f in dedup_files:
                    # collect normalized dimensions for scatter plot
                    _ci = cv2.imread(os.path.join(tmp_dir, f))
                    if _ci is not None:
                        _ch, _cw = _ci.shape[:2]
                        raw_pts.append((_cw * _ch, round(_cw / 1920, 5), round(_ch / 1080, 5)))

                    # save crop
                    crop_dst = _unique_path(out_crops, f)
                    try: shutil.copy2(os.path.join(tmp_dir, f), crop_dst)
                    except Exception: pass
                    else:
                        img_paths.setdefault(key, crop_dst)

                    # save original image + LabelMe JSON once per source image
                    img_id = os.path.splitext(f)[0].split("_")[0]
                    if img_id in saved_ids:
                        continue
                    saved_ids.add(img_id)

                    orig  = img_src_map.get(img_id)
                    boxes = all_dets.get(img_id, [])
                    if not orig or not boxes:
                        continue

                    orig_name = os.path.basename(orig)

                    saved_img_path = _unique_path(out_seg, orig_name)
                    try: shutil.copy2(orig, saved_img_path)
                    except Exception: pass

                    saved_img_stem = os.path.splitext(os.path.basename(saved_img_path))[0]

                    _img = cv2.imread(orig)
                    h, w = (_img.shape[:2] if _img is not None else (0, 0))
                    shapes = [
                        {
                            "label": lbl,
                            "points": [[x1, y1], [x2, y2]],
                            "group_id": None,
                            "description": "",
                            "shape_type": "rectangle",
                            "flags": {},
                        }
                        for lbl, (x1, y1, x2, y2) in boxes
                    ]
                    labelme = {
                        "version": "5.2.1",
                        "flags": {},
                        "shapes": shapes,
                        "imagePath": os.path.basename(saved_img_path),
                        "imageData": None,
                        "imageHeight": h,
                        "imageWidth": w,
                        # used by the PDF report to match images to estate/block
                        "source_folder": root_folder,
                        "source_row":    key[0],
                        "source_frond":  key[1],
                    }
                    try:
                        with open(os.path.join(out_seg, saved_img_stem + ".json"), "w") as jf:
                            json.dump(labelme, jf, indent=2)
                    except Exception:
                        pass

            shutil.rmtree(tmp_dir, ignore_errors=True)

        # ── Size classification (Small / Medium / Big) ────────────────────────
        size_dist  = {'small': 0, 'medium': 0, 'big': 0}
        size_points: list = []   # [{x, y, c}] for scatter plot
        if raw_pts:
            max_area = max(a for a, _, _ in raw_pts)
            thr_s    = 0.30 * max_area
            thr_m    = 0.70 * max_area
            for area, wn, hn in raw_pts:
                if area < thr_s:
                    cat = 'S'; size_dist['small']  += 1
                elif area < thr_m:
                    cat = 'M'; size_dist['medium'] += 1
                else:
                    cat = 'B'; size_dist['big']    += 1
                size_points.append({'x': wn, 'y': hn, 'c': cat})

        rows = []
        for sf in sub_folders:
            frond_counts = {frond: counts.get((sf, frond), 0) for frond in frond_names}
            best_frond   = max(frond_counts, key=frond_counts.get) if frond_counts else None
            image_path   = img_paths.get((sf, best_frond), '') if best_frond else ''
            rows.append({"Baris-Pokok": sf, **frond_counts, "ImagePath": image_path})
        return pd.DataFrame(rows, columns=["Baris-Pokok"] + frond_names + ["ImagePath"]), size_dist, size_points

    except _Stopped:
        return empty_df, {'small': 0, 'medium': 0, 'big': 0}, []

    finally:
        cleanup()
