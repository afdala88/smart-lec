"""Browser uploads of census photo folders.

Layout on disk:
    <UPLOAD_ROOT>/<upload_id>/meta.json
    <UPLOAD_ROOT>/<upload_id>/files/<relative path as picked in the browser>

The browser sends one file per request (PUT), so an interrupted upload resumes
by asking /status which files are already complete and skipping them.
Files are written to a .part file first and renamed when complete, so a
half-written file never counts as uploaded.
"""
import json
import os
import shutil
import time
import uuid
from datetime import datetime

from core.pests import BASE_DIR

UPLOAD_ROOT        = os.environ.get('UPLOAD_DIR', os.path.join(BASE_DIR, 'uploads'))
ALLOWED_EXT        = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
MAX_FILE_BYTES     = 50 * 1024 * 1024
RETENTION_DAYS     = int(os.environ.get('UPLOAD_RETENTION_DAYS', '7'))


class UploadError(ValueError):
    pass


def _dir(upload_id):
    if not upload_id or not all(c in '0123456789abcdef' for c in upload_id) or len(upload_id) != 32:
        raise UploadError('upload_id tidak valid')
    return os.path.join(UPLOAD_ROOT, upload_id)


def _files_dir(upload_id):
    return os.path.join(_dir(upload_id), 'files')


def create(user_id, name):
    upload_id = uuid.uuid4().hex
    os.makedirs(_files_dir(upload_id), exist_ok=True)
    meta = {'user_id': user_id, 'name': (name or '').strip()[:200],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    with open(os.path.join(_dir(upload_id), 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f)
    return upload_id


def get_meta(upload_id):
    path = os.path.join(_dir(upload_id), 'meta.json')
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _safe_rel(rel):
    """Normalise a browser-relative path and refuse anything escaping the upload dir."""
    rel = (rel or '').replace('\\', '/').strip('/')
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts) or ':' in parts[0]:
        raise UploadError(f'path tidak valid: {rel!r}')
    if os.path.splitext(parts[-1])[1].lower() not in ALLOWED_EXT:
        raise UploadError(f'tipe file tidak diizinkan: {parts[-1]!r}')
    return os.path.join(*parts)


def list_files(upload_id):
    """{relative path (forward slashes): size} of completed files."""
    root = _files_dir(upload_id)
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith('.part'):
                continue
            full = os.path.join(dirpath, n)
            out[os.path.relpath(full, root).replace('\\', '/')] = os.path.getsize(full)
    return out


def save_file(upload_id, rel, stream, content_length=None):
    if content_length is not None and content_length > MAX_FILE_BYTES:
        raise UploadError('file terlalu besar')
    dst = os.path.join(_files_dir(upload_id), _safe_rel(rel))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.part'
    written = 0
    with open(tmp, 'wb') as f:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_FILE_BYTES:
                f.close()
                os.remove(tmp)
                raise UploadError('file terlalu besar')
            f.write(chunk)
    os.replace(tmp, dst)
    return written


def census_root(upload_id, discover):
    """Folder to hand to the census pipeline.

    A browser folder pick keeps the picked folder's own name as the first path
    segment (E-48/105-17/F1/x.jpg), so the census root is usually that single
    sub-folder. `discover` is census_processor._discover, used to validate.
    """
    base = _files_dir(upload_id)
    candidates = [base]
    subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))] if os.path.isdir(base) else []
    if len(subs) == 1:
        candidates.insert(0, os.path.join(base, subs[0]))
    last_err = None
    for root in candidates:
        try:
            rows, fronds = discover(root)
            if rows:
                return root, rows, fronds
        except Exception as e:
            last_err = e
    raise UploadError('Struktur folder tidak sesuai. Harus: <folder>/<Baris-Pokok>/<F1..F3>/foto. '
                      + (str(last_err) if last_err else ''))


def delete(upload_id):
    shutil.rmtree(_dir(upload_id), ignore_errors=True)


def cleanup(in_use, max_age_days=RETENTION_DAYS):
    """Delete uploads older than max_age_days that no queued/running job needs.
    Census results (crops, segmentation, DB) are stored elsewhere, so this is safe."""
    if not os.path.isdir(UPLOAD_ROOT):
        return 0
    cutoff, removed = time.time() - max_age_days * 86400, 0
    for upload_id in os.listdir(UPLOAD_ROOT):
        path = os.path.join(UPLOAD_ROOT, upload_id)
        if upload_id in in_use or not os.path.isdir(path):
            continue
        if os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
