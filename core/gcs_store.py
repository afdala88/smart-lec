"""Copy each census session's crops + detection results to Google Cloud Storage.

Enabled when GCS_BUCKET is set (e.g. in /etc/bagworm/env). On the GCE VM the
client authenticates through the VM service account (metadata server), so no
key file is needed. When GCS_BUCKET is empty everything stays on local disk
exactly as before.

Layout in the bucket:
    gs://<bucket>/<estate>/<block>/<census-date>_sesi-<id>/crops/<file>.jpg
    gs://<bucket>/<estate>/<block>/<census-date>_sesi-<id>/deteksi/<file>.jpg|.json
"""
import os
import re
import tempfile
import zipfile

GCS_BUCKET       = os.environ.get('GCS_BUCKET', '').strip()
# Delete the local copy after a successful upload. Off by default because the
# PDF report and the Develop page still read temp_suport/LEC_census locally.
GCS_DELETE_LOCAL = os.environ.get('GCS_DELETE_LOCAL', '0').strip() == '1'

_client = None


def enabled():
    return bool(GCS_BUCKET)


def _bucket():
    global _client
    if _client is None:
        from google.cloud import storage   # imported lazily: optional dependency
        _client = storage.Client()
    return _client.bucket(GCS_BUCKET)


def _safe(s, fallback):
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(s or '').strip()).strip('_')
    return s or fallback


def session_prefix(session_id, estate, block, census_date):
    return '{}/{}/{}_sesi-{}'.format(
        _safe(estate, 'tanpa-estate'),
        _safe(block, 'tanpa-block'),
        _safe((census_date or '')[:10], 'tanpa-tanggal'),
        session_id,
    )


def upload_session(session_id, estate, block, census_date, saved_files):
    """Upload [(kind, local_path), ...] where kind is 'crops' or 'deteksi'.
    Returns the bucket prefix, or None when GCS is disabled / nothing to upload."""
    if not enabled() or not saved_files:
        return None
    bucket = _bucket()
    prefix = session_prefix(session_id, estate, block, census_date)
    uploaded = []
    for kind, path in saved_files:
        if not os.path.isfile(path):
            continue
        blob = bucket.blob(f'{prefix}/{kind}/{os.path.basename(path)}')
        blob.upload_from_filename(path)
        uploaded.append(path)
    if GCS_DELETE_LOCAL:
        for path in uploaded:
            try:
                os.remove(path)
            except OSError:
                pass
    print(f'[GCS] session {session_id}: {len(uploaded)} file(s) -> gs://{GCS_BUCKET}/{prefix}/')
    return prefix


def zip_session(prefix):
    """Download every object under prefix into a temporary zip file.
    Returns (path, n_files); the caller deletes the file after sending."""
    bucket = _bucket()
    fd, zip_path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    n = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:   # JPGs are already compressed
        for blob in bucket.list_blobs(prefix=prefix.rstrip('/') + '/'):
            rel = blob.name[len(prefix):].lstrip('/')
            if not rel:
                continue
            with zf.open(rel, 'w') as dst:
                blob.download_to_file(dst)
            n += 1
    return zip_path, n
