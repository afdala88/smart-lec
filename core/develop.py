import os
import csv as _csv
import json as _json
import yaml as _yaml
import glob as _glob
import shutil
import subprocess
import sys
from datetime import datetime
from core.pests import BASE_DIR
from core.jobs import _dev_jobs

_YOLO_DIR  = os.path.join(BASE_DIR, "yolov5_inf")
_DET_DIR   = os.path.join(_YOLO_DIR, "model_fix", "det_yv5", "v2")
_CLS_DIR   = os.path.join(_YOLO_DIR, "model_fix", "bag-dam_v1")
_SEG_DIR   = os.path.join(BASE_DIR, "temp_suport", "LEC_census", "segmentasi")
_CROPS_DIR = os.path.join(BASE_DIR, "temp_suport", "LEC_census", "crops")

_DET_CHARTS = ['results.png', 'F1_curve.png', 'PR_curve.png', 'P_curve.png',
               'R_curve.png', 'confusion_matrix.png', 'labels.jpg']
_CLS_CHARTS = ['train_images.jpg', 'test_images.jpg']


def _parse_best_metrics(csv_path, mode='detection'):
    try:
        with open(csv_path, newline='', encoding='utf-8', errors='replace') as f:
            rows = [{k.strip(): v.strip() for k, v in row.items()}
                    for row in _csv.DictReader(f)]
        if not rows:
            return {}
        if mode == 'detection':
            map50_key = next((k for k in rows[0] if 'mAP50' in k and '95' not in k and '(' not in k), None) \
                        or next((k for k in rows[0] if 'mAP_0.5' in k and '0.95' not in k), 'metrics/mAP_0.5')
            map95_key = next((k for k in rows[0] if ('mAP50-95' in k or 'mAP_0.5:0.95' in k)), 'metrics/mAP_0.5:0.95')
            prec_key  = next((k for k in rows[0] if 'precision' in k.lower()), 'metrics/precision')
            rec_key   = next((k for k in rows[0] if 'recall'    in k.lower()), 'metrics/recall')
            best = max(rows, key=lambda r: float(r.get(map50_key, 0) or 0))
            return {
                'epochs':     len(rows),
                'precision':  f"{float(best.get(prec_key, 0) or 0):.4f}",
                'recall':     f"{float(best.get(rec_key,  0) or 0):.4f}",
                'mAP50':      f"{float(best.get(map50_key,0) or 0):.4f}",
                'mAP50_95':   f"{float(best.get(map95_key,0) or 0):.4f}",
                'best_epoch': best.get('epoch', '—').strip(),
            }
        else:
            best = max(rows, key=lambda r: float(r.get('metrics/accuracy_top1', 0)))
            loss_val = best.get('val/loss') or best.get('test/loss') or '0'
            return {
                'epochs':     len(rows),
                'acc_top1':   f"{float(best['metrics/accuracy_top1']):.4f}",
                'acc_top5':   f"{float(best['metrics/accuracy_top5']):.4f}",
                'val_loss':   f"{float(loss_val):.4f}",
                'best_epoch': best['epoch'].strip(),
            }
    except Exception:
        return {}


def _count_cls_dataset(dataset_path):
    counts = {}
    if not dataset_path or not os.path.isdir(dataset_path):
        return counts
    IMG_EXT = {'.jpg', '.jpeg', '.png', '.bmp'}
    for cls in sorted(os.listdir(dataset_path)):
        cls_dir = os.path.join(dataset_path, cls)
        if not os.path.isdir(cls_dir):
            continue
        n = sum(1 for f in os.listdir(cls_dir)
                if os.path.splitext(f)[1].lower() in IMG_EXT)
        if n > 0:
            counts[cls] = n
    return counts


def _read_train_meta(model_dir):
    try:
        with open(os.path.join(model_dir, "train_meta.json"), encoding='utf-8') as f:
            return _json.load(f)
    except Exception:
        return {}


def _write_train_meta(model_dir, data):
    with open(os.path.join(model_dir, "train_meta.json"), "w", encoding='utf-8') as f:
        _json.dump(data, f, indent=2)


def _load_develop_data():
    det_opt, cls_opt = {}, {}
    try:
        with open(os.path.join(_DET_DIR, "opt.yaml"), encoding='utf-8') as f: det_opt = _yaml.safe_load(f)
        with open(os.path.join(_CLS_DIR, "opt.yaml"), encoding='utf-8') as f: cls_opt = _yaml.safe_load(f)
    except Exception:
        pass
    _seg_count      = len(_glob.glob(os.path.join(_SEG_DIR,   "*.jpg")))  if os.path.isdir(_SEG_DIR)   else 0
    _seg_json_count = len(_glob.glob(os.path.join(_SEG_DIR,   "*.json"))) if os.path.isdir(_SEG_DIR)   else 0
    dataset = {
        'crops_path':     _CROPS_DIR,
        'seg_path':       _SEG_DIR,
        'crops_count':    0,
        'seg_count':      _seg_count,
        'seg_json_count': _seg_json_count,
    }
    models = {
        'detection': {
            'path':       os.path.join(_DET_DIR, "weights", "best.pt"),
            'arch':       'YOLOv5 (detection)',
            'img_size':   det_opt.get('imgsz', 640),
            'batch':      det_opt.get('batch_size', 16),
            'optimizer':  det_opt.get('optimizer', 'SGD'),
            'metrics':    _parse_best_metrics(os.path.join(_DET_DIR, "results.csv"), 'detection'),
            'train_meta': _read_train_meta(_DET_DIR),
        },
        'classifier': {
            'path':         os.path.join(_CLS_DIR, "weights", "best.pt"),
            'arch':         cls_opt.get('model', 'YOLOv5s-cls'),
            'img_size':     cls_opt.get('imgsz', 224),
            'batch':        cls_opt.get('batch_size', 64),
            'optimizer':    cls_opt.get('optimizer', 'Adam'),
            'metrics':      _parse_best_metrics(os.path.join(_CLS_DIR, "results.csv"), 'classifier'),
            'train_meta':   _read_train_meta(_CLS_DIR),
            'class_counts': _read_train_meta(_CLS_DIR).get('class_counts', {}),
        },
    }
    return dataset, models


def _labelme_to_yolo(json_path, class_map):
    with open(json_path, encoding='utf-8') as f:
        data = _json.load(f)
    W = data.get('imageWidth',  1)
    H = data.get('imageHeight', 1)
    lines = []
    for shape in data.get('shapes', []):
        label = shape.get('label', '')
        if label not in class_map:
            class_map[label] = len(class_map)
        cls_id = class_map[label]
        pts = shape.get('points', [])
        if shape.get('shape_type') == 'rectangle' and len(pts) == 2:
            x1, y1 = pts[0]
            x2, y2 = pts[1]
        elif len(pts) >= 2:
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        else:
            continue
        cx = ((x1 + x2) / 2) / W
        cy = ((y1 + y2) / 2) / H
        w  = abs(x2 - x1) / W
        h  = abs(y2 - y1) / H
        lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return lines


def _split_det_dataset(src_dir, dst_dir, ratios, job):
    import random
    IMG_EXT = {'.jpg', '.jpeg', '.png', '.bmp'}
    all_files = os.listdir(src_dir)
    imgs = [f for f in all_files if os.path.splitext(f)[1].lower() in IMG_EXT
            and os.path.exists(os.path.join(src_dir, os.path.splitext(f)[0] + '.json'))]
    if not imgs:
        raise ValueError(f"Tidak ada pasangan gambar+JSON ditemukan di: {src_dir}")

    random.shuffle(imgs)
    n = len(imgs)
    train_r, val_r, _ = ratios
    n_train = max(1, int(n * train_r))
    n_val   = max(1, int(n * val_r))
    buckets = {
        'train': imgs[:n_train],
        'val':   imgs[n_train:n_train + n_val],
        'test':  imgs[n_train + n_val:],
    }

    class_map = {}
    counts = {}
    for split, files in buckets.items():
        out_img = os.path.join(dst_dir, split, 'images')
        out_lbl = os.path.join(dst_dir, split, 'labels')
        os.makedirs(out_img, exist_ok=True)
        os.makedirs(out_lbl, exist_ok=True)
        for img_f in files:
            stem     = os.path.splitext(img_f)[0]
            json_src = os.path.join(src_dir, stem + '.json')
            shutil.copy2(os.path.join(src_dir, img_f), os.path.join(out_img, img_f))
            yolo_lines = _labelme_to_yolo(json_src, class_map)
            with open(os.path.join(out_lbl, stem + '.txt'), 'w', encoding='utf-8') as lf:
                lf.write('\n'.join(yolo_lines))
        counts[split] = len(files)

    names = [k for k, _ in sorted(class_map.items(), key=lambda x: x[1])]
    nc    = len(names) or 1
    yaml_path = os.path.join(dst_dir, 'dataset.yaml')
    with open(yaml_path, 'w', encoding='utf-8') as yf:
        yf.write(f"path: {dst_dir}\n")
        yf.write(f"train: train/images\n")
        yf.write(f"val:   val/images\n")
        yf.write(f"test:  test/images\n")
        yf.write(f"nc: {nc}\n")
        yf.write(f"names: {names}\n")

    job['log'].append(f"[convert] {n} pasang gambar+JSON → YOLO format, {nc} class: {names}")
    job['log'].append(f"[split] train:{counts['train']} val:{counts['val']} test:{counts['test']} / total:{n}")
    return yaml_path


def _split_cls_dataset(src_dir, dst_dir, ratios, job):
    import random
    train_r, val_r, _ = ratios
    classes = [d for d in os.listdir(src_dir)
               if os.path.isdir(os.path.join(src_dir, d))]
    if not classes:
        raise ValueError(f"Tidak ada subfolder class di: {src_dir}")

    splits = ['train', 'val', 'test']
    counts = {s: 0 for s in splits}

    for cls in classes:
        imgs = [f for f in os.listdir(os.path.join(src_dir, cls))
                if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))]
        random.shuffle(imgs)
        n = len(imgs)
        n_train = max(1, int(n * train_r))
        n_val   = max(1, int(n * val_r))
        buckets = {
            'train': imgs[:n_train],
            'val':   imgs[n_train:n_train + n_val],
            'test':  imgs[n_train + n_val:],
        }
        for split, files in buckets.items():
            out = os.path.join(dst_dir, split, cls)
            os.makedirs(out, exist_ok=True)
            for f in files:
                shutil.copy2(os.path.join(src_dir, cls, f), os.path.join(out, f))
                counts[split] += 1

    total = sum(counts.values())
    job['log'].append(
        f"[split] {len(classes)} classes — "
        f"train:{counts['train']} val:{counts['val']} test:{counts['test']} / total:{total}"
    )


def _run_training(job_id, mode, dataset_path, epochs, batch, imgsz,
                  split_ratios=(0.7, 0.2, 0.1), pretrained='', extra=None):
    job = _dev_jobs[job_id]
    job['status'] = 'running'
    tmp_out   = os.path.join(_YOLO_DIR, f"_devtrain_{job_id}")
    split_dir = os.path.join(tmp_out, "_split")
    try:
        if mode == 'detection':
            model_dir = _DET_DIR
            weights   = pretrained if pretrained and os.path.isfile(pretrained) \
                        else os.path.join(model_dir, "weights", "best.pt")
            if pretrained and not os.path.isfile(pretrained):
                job['log'].append(f"[warn] pretrained tidak ditemukan: {pretrained} — pakai model default")

            is_yaml = dataset_path.lower().endswith(('.yaml', '.yml'))
            if is_yaml:
                train_data = dataset_path
            else:
                job['log'].append(f"[split] Memisahkan dataset dari: {dataset_path}")
                split_det_dir = os.path.join(tmp_out, "_split_det")
                train_data = _split_det_dataset(dataset_path, split_det_dir, split_ratios, job)
            cmd = [
                sys.executable,
                os.path.join(_YOLO_DIR, "train.py"),
                "--weights",    weights,
                "--data",       train_data,
                "--epochs",     str(epochs),
                "--batch-size", str(batch),
                "--imgsz",      str(imgsz),
                "--project",    tmp_out,
                "--name",       "run",
                "--exist-ok",
            ]
        elif mode == 'classifier':
            model_dir = _CLS_DIR
            weights   = pretrained if pretrained and os.path.isfile(pretrained) \
                        else os.path.join(model_dir, "weights", "best.pt")
            if pretrained and not os.path.isfile(pretrained):
                job['log'].append(f"[warn] pretrained tidak ditemukan: {pretrained} — pakai model default")

            job['log'].append(f"[split] Memisahkan dataset dari: {dataset_path}")
            _split_cls_dataset(dataset_path, split_dir, split_ratios, job)
            train_data = split_dir
            is_yaml = False

            cmd = [
                sys.executable,
                os.path.join(_YOLO_DIR, "classify", "train.py"),
                "--model",      weights,
                "--data",       train_data,
                "--epochs",     str(epochs),
                "--batch-size", str(batch),
                "--imgsz",      str(imgsz),
                "--project",    tmp_out,
                "--name",       "run",
                "--exist-ok",
            ]
        else:
            raise ValueError(f"Unsupported training mode: {mode}")

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=_YOLO_DIR, bufsize=1
        )
        log_lines = list(job['log'])
        for line in proc.stdout:
            log_lines.append(line.rstrip())
            job['log'] = log_lines[-80:]
        proc.wait()

        if proc.returncode != 0:
            raise RuntimeError(f"Training exited with code {proc.returncode}")

        run_dir     = os.path.join(tmp_out, "run")
        new_weights = os.path.join(run_dir, "weights", "best.pt")
        new_results = os.path.join(run_dir, "results.csv")

        if os.path.exists(new_weights):
            shutil.copy2(new_weights, os.path.join(model_dir, "weights", "best.pt"))
        if os.path.exists(new_results):
            shutil.copy2(new_results, os.path.join(model_dir, "results.csv"))

        meta = {
            'trained_at':   datetime.now().strftime('%Y-%m-%d %H:%M'),
            'dataset_path': dataset_path,
            'epochs':       epochs,
            'batch':        batch,
            'imgsz':        imgsz,
        }
        if mode == 'detection' and not is_yaml:
            meta['split'] = {
                'train': round(split_ratios[0] * 100),
                'val':   round(split_ratios[1] * 100),
                'test':  round(split_ratios[2] * 100),
            }
        if mode == 'classifier':
            meta['class_counts'] = _count_cls_dataset(dataset_path)
            meta['split'] = {
                'train': round(split_ratios[0] * 100),
                'val':   round(split_ratios[1] * 100),
                'test':  round(split_ratios[2] * 100),
            }
        meta['imgsz'] = imgsz
        meta['batch'] = batch
        _write_train_meta(model_dir, meta)
        job['status'] = 'done'

    except Exception as e:
        job['status'] = 'error'
        job['error']  = str(e)
    finally:
        if os.path.exists(tmp_out):
            shutil.rmtree(tmp_out, ignore_errors=True)
