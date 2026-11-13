"""
Leaf damage analysis processor.

For each Baris-Pokok subfolder, analyses wet damage per frond position:

  Baris-Pokok | D1          | D2          | D3
  ------------|-------------|-------------|-------------
  <row>       | avg wet% F1 | avg wet% F2 | avg wet% F3

D1 = average wet-damage % across all images in frond F1
D2 = average wet-damage % across all images in frond F2
D3 = average wet-damage % across all images in frond F3

HSV thresholds match damage.ipynb exactly.
"""
import os
import cv2
import numpy as np
import pandas as pd
import threading

_LEAF_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "yolov5_inf", "model_fix", "leaf_yv8", "weights", "best.pt",
)

_leaf_model      = None
_leaf_model_lock = threading.Lock()

# Frond folders that map to D1, D2, D3
FROND_ORDER = ["F1", "F2", "F3"]


def _get_leaf_model():
    global _leaf_model
    if _leaf_model is not None:
        return _leaf_model
    with _leaf_model_lock:
        if _leaf_model is None:
            from ultralytics import YOLO
            _leaf_model = YOLO(_LEAF_MODEL_PATH)
    return _leaf_model


# ── HSV damage thresholds (from damage.ipynb) ─────────────────────────────────
_WET_LOWER = np.array([0,  20, 120])
_WET_UPPER = np.array([25, 150, 255])

_KERNEL = np.ones((3, 3), np.uint8)


def _analyse_image(img, model):
    """
    Returns wet damage % for a single image.
    Returns None if no leaf mask found.
    """
    img = cv2.resize(img, (0, 0), fx=0.5, fy=0.5)
    h, w = img.shape[:2]

    results   = model(img, verbose=False)[0]
    mask_leaf = np.zeros((h, w), dtype=np.uint8)

    if results.masks is not None:
        for i, cls in enumerate(results.boxes.cls):
            if model.names[int(cls)] == "leaf":
                seg = results.masks.data[i].cpu().numpy()
                seg = cv2.resize(seg, (w, h))
                mask_leaf = np.logical_or(mask_leaf, seg > 0.5)
        mask_leaf = mask_leaf.astype(np.uint8) * 255

    if cv2.countNonZero(mask_leaf) == 0:
        return None

    hsv   = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m_wet = cv2.inRange(hsv, _WET_LOWER, _WET_UPPER)

    mask_leaf = cv2.morphologyEx(mask_leaf, cv2.MORPH_OPEN, _KERNEL)
    m_wet     = cv2.morphologyEx(m_wet,     cv2.MORPH_OPEN, _KERNEL)
    m_wet     = cv2.bitwise_and(m_wet, mask_leaf)

    leaf_px = cv2.countNonZero(mask_leaf)
    return cv2.countNonZero(m_wet) / leaf_px * 100


def _analyse_frond(frond_path, model):
    """
    Average wet damage % over all images in one frond folder.
    Returns avg_wet (float).
    """
    total = count = 0

    for fname in os.listdir(frond_path):
        if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        img = cv2.imread(os.path.join(frond_path, fname))
        if img is None:
            continue
        result = _analyse_image(img, model)
        if result is None:
            continue
        total += result
        count += 1

    return round(total / count, 2) if count > 0 else 0.0


def process_damage(root_folder, stop_event=None):
    """
    Analyse leaf wet damage for all Baris-Pokok rows in root_folder.

    Parameters
    ----------
    root_folder : str
        Same folder passed to census_processor.process_files().
    stop_event  : threading.Event, optional
        Set to interrupt processing early.

    Returns
    -------
    pd.DataFrame  columns: ['Baris-Pokok', 'D1', 'D2', 'D3']
    """
    if not os.path.isfile(_LEAF_MODEL_PATH):
        return pd.DataFrame(columns=["Baris-Pokok", "D1", "D2", "D3"])

    try:
        model = _get_leaf_model()
    except Exception:
        return pd.DataFrame(columns=["Baris-Pokok", "D1", "D2", "D3"])

    rows = []

    sub_folders = sorted(
        d for d in os.listdir(root_folder)
        if os.path.isdir(os.path.join(root_folder, d))
    )

    for subfolder in sub_folders:
        if stop_event and stop_event.is_set():
            break

        path_b = os.path.join(root_folder, subfolder)

        d_values = {}
        for frond, col in zip(FROND_ORDER, ["D1", "D2", "D3"]):
            frond_path = os.path.join(path_b, frond)
            if os.path.isdir(frond_path):
                d_values[col] = _analyse_frond(frond_path, model)
            else:
                d_values[col] = 0.0

        rows.append({"Baris-Pokok": subfolder, **d_values})

    return pd.DataFrame(rows, columns=["Baris-Pokok", "D1", "D2", "D3"])
