import os
from datetime import datetime
import pandas as pd
from core.pests import BASE_DIR

_DATA_ESTATE_PATH = os.path.join(BASE_DIR, 'data_estate.xlsx')


def _load_estate_data():
    """Return list of {estate, blok, tahun_tanam} dicts from data_estate.xlsx."""
    df = pd.read_excel(_DATA_ESTATE_PATH)
    df.columns = [c.strip() for c in df.columns]
    df = df.dropna(subset=['Estate', 'Blok', 'Tahun Tanam'])
    df['Tahun Tanam'] = df['Tahun Tanam'].astype(int)
    return df.to_dict(orient='records')


def _get_tahun_tanam(estate: str, blok: str):
    """Look up planting year for a given estate + block combination."""
    for row in _load_estate_data():
        if str(row['Estate']).strip() == estate.strip() and str(row['Blok']).strip() == blok.strip():
            return int(row['Tahun Tanam'])
    return None


def calculate_decision(df: pd.DataFrame, tahun_tanam: int) -> pd.DataFrame:
    """
    Add 'Decision' column using outbreak logic from calculate_outbreak.xlsx.

    Per-row indicators (K=AI, L=Dm):
      AI  = 1 if mean(F1,F2,F3) >= 2
      umur = tahun sekarang - tahun_tanam
      Dm  = 1 if at least 2 of (D1,D2,D3) >= dm_threshold  [umur >= 4 tahun]
            dm_threshold = 1.55
          = 1 if at least 1 of (D1,D2,D3) >= dm_threshold  [umur <  4 tahun]
            dm_threshold = 0.40

    Global N_avg = mean of all per-row mean(F1,F2,F3):
      Decision = OR(AI,Dm)  if N_avg >= 1   (more permissive)
               = AND(AI,Dm) if N_avg <  1   (stricter)

    Output: 'Spray' (1) or 'No Spray' (0).
    """
    df = df.copy()

    f_cols = ['F1', 'F2', 'F3']
    d_cols = ['D1', 'D2', 'D3']
    for c in f_cols + d_cols:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0.0)

    per_row_avg = df[f_cols].mean(axis=1)
    n_avg = per_row_avg.mean()

    ai = (per_row_avg >= 2).astype(int)
    umur = datetime.now().year - int(tahun_tanam)
    if umur < 4:
        dm_threshold = 0.40
        dm = (df[d_cols].apply(lambda row: (row >= dm_threshold).sum(), axis=1) >= 1).astype(int)
    else:
        dm_threshold = 1.55
        dm = (df[d_cols].apply(lambda row: (row >= dm_threshold).sum(), axis=1) >= 2).astype(int)

    if n_avg >= 1:
        decision_flag = ((ai == 1) | (dm == 1)).astype(int)
    else:
        decision_flag = ((ai == 1) & (dm == 1)).astype(int)

    df['Decision'] = decision_flag.map({1: 'Spray', 0: 'No Spray'})
    return df
