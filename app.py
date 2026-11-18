import os
import re
import io
import uuid
import glob as _glob
import json as _json
import threading
import requests
import pandas as pd
from pathlib import Path
from flask import (
    Flask, Blueprint, render_template, request, jsonify, redirect,
    url_for, make_response, session, send_file,
)
import time
from collections import defaultdict, deque
from translations import TRANSLATIONS, SUPPORTED_LANGS
import database as db
import census_processor  # import early so yolov5_inf sys.path is set before anything else
import damage_processor

from core.auth import login_required, admin_required, DEV_USER_IDS
from core.jobs import _dev_jobs
from core import uploads, census_queue
from core.pests import PESTS, BASE_DIR
from core.estate import _get_tahun_tanam, calculate_decision
from core import gcal_reminder
from core import gcs_store
from core import gps_coords
from core.develop import (
    _load_develop_data, _run_training,
    _DET_DIR, _CLS_DIR, _SEG_DIR, _DET_CHARTS, _CLS_CHARTS,
)
from core.report_utils import generate_excel_report

PEST_KEY = 'bagworm'
PEST     = PESTS[PEST_KEY]

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

# CLOUD_MODE=1 when served to remote users (GCP): photos come in by browser upload,
# server-side folder dialogs and the Develop (training) module are disabled,
# and real secrets are mandatory.
CLOUD_MODE = os.environ.get('CLOUD_MODE', '0') == '1'

_DEFAULT_SECRET   = 'agricensus-bagworm-secret-2025-change-in-prod'
_DEFAULT_ADMIN_PW = 'admin1234'
app.secret_key = os.environ.get('SECRET_KEY', _DEFAULT_SECRET)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', _DEFAULT_ADMIN_PW)

if CLOUD_MODE:
    if app.secret_key == _DEFAULT_SECRET or ADMIN_PASSWORD == _DEFAULT_ADMIN_PW:
        raise RuntimeError('CLOUD_MODE: set SECRET_KEY and ADMIN_PASSWORD environment variables')
    app.config.update(
        SESSION_COOKIE_SECURE=True,      # served over HTTPS only
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )
    # Behind nginx: take the client IP / https scheme from the proxy headers,
    # otherwise every user looks like 127.0.0.1 (shared login-attempt limit).
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# one uploaded photo per request; ground-truth Excel also fits under this
app.config['MAX_CONTENT_LENGTH'] = uploads.MAX_FILE_BYTES + 1024 * 1024

db.init_db()

app.jinja_env.filters['basename']  = os.path.basename
app.jinja_env.filters['from_json'] = lambda s: _json.loads(s or '[]')


@app.context_processor
def inject_dev_flag():
    return {'is_developer': (not CLOUD_MODE) and session.get('user_id') in DEV_USER_IDS,
            'cloud_mode':   CLOUD_MODE}


_CLOUD_BLOCKED_PREFIXES = ('/browse-folder', '/browse-file', '/api/develop', '/bagworm/develop')


@app.before_request
def _before_request():
    # The census worker lives in the process that serves requests (not the
    # Flask debug reloader's watcher process), so start it on first request.
    census_queue.start(_run_census_job)
    if CLOUD_MODE and request.path.startswith(_CLOUD_BLOCKED_PREFIXES):
        return jsonify({'error': 'Not available on the cloud server'}), 404


@app.errorhandler(400)
def bad_request(e):  return jsonify({'error': str(e)}), 400

@app.errorhandler(404)
def not_found(e):    return jsonify({'error': str(e)}), 404

@app.errorhandler(500)
def server_error(e): return jsonify({'error': str(e)}), 500


# ── Language helpers ───────────────────────────────────────────────────────────
def get_lang():
    lang = request.cookies.get('lang', 'en')
    return lang if lang in SUPPORTED_LANGS else 'en'

@app.context_processor
def inject_lang():
    lang = get_lang()
    strings = TRANSLATIONS[lang]
    def t(key):
        return strings.get(key, TRANSLATIONS['en'].get(key, key))
    return dict(t=t, lang=lang, supported_langs=SUPPORTED_LANGS)

@app.route('/set-lang/<lang>')
def set_lang(lang):
    if lang not in SUPPORTED_LANGS:
        lang = 'en'
    referrer = request.referrer or url_for('index')
    resp = make_response(redirect(referrer))
    resp.set_cookie('lang', lang, max_age=60 * 60 * 24 * 365)
    return resp


# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    error    = None
    success  = None
    next_url = request.args.get('next', '') or request.form.get('next', '')
    if request.method == 'POST':
        nama        = request.form.get('nama', '').strip()
        nik         = request.form.get('nik', '').strip()
        no_whatsapp = request.form.get('no_whatsapp', '').strip()
        password    = request.form.get('password', '')
        password2   = request.form.get('password2', '')
        if not nama or not nik or not no_whatsapp or not password:
            error = 'Semua field wajib diisi.'
        elif not nik.isdigit() or len(nik) < 7:
            error = 'NIK harus minimal 7 digit angka.'
        elif len(password) < 6:
            error = 'Password minimal 6 karakter.'
        elif password != password2:
            error = 'Konfirmasi password tidak cocok.'
        else:
            result = db.register_user(nama, nik, no_whatsapp, password)
            if result is None:
                error = 'NIK sudah terdaftar.'
            else:
                success = True
    return render_template('register.html', error=error, success=success,
                           next_url=next_url, requested_module=None)


# Simple brute-force guard: max 10 failed logins per IP per 15 minutes (per process).
_LOGIN_MAX_FAILS = 10
_LOGIN_WINDOW_S  = 15 * 60
_login_fails     = defaultdict(deque)
_login_lock      = threading.Lock()


def _login_blocked(ip):
    now = time.time()
    with _login_lock:
        q = _login_fails[ip]
        while q and now - q[0] > _LOGIN_WINDOW_S:
            q.popleft()
        return len(q) >= _LOGIN_MAX_FAILS


def _login_failed(ip):
    with _login_lock:
        _login_fails[ip].append(time.time())


@app.route('/login', methods=['GET', 'POST'])
def login():
    error    = None
    next_url = request.args.get('next', '') or request.form.get('next', '')
    if request.method == 'POST':
        nik      = request.form.get('nik', '').strip()
        password = request.form.get('password', '')
        if _login_blocked(request.remote_addr):
            return render_template('login.html', next_url=next_url,
                                   error='Terlalu banyak percobaan login. Coba lagi 15 menit lagi.'), 429
        user = db.verify_user(nik, password)
        if not user:
            _login_failed(request.remote_addr)
            error = 'NIK atau password salah.'
        elif user['status'] == 'pending':
            error = 'Akun Anda belum disetujui. Silakan tunggu konfirmasi admin.'
        elif user['status'] == 'rejected':
            error = 'Akun Anda ditolak. Hubungi admin untuk informasi lebih lanjut.'
        else:
            session['user_id']   = user['id']
            session['user_nama'] = user['nama']
            return redirect(next_url if next_url else url_for('index'))
    return render_template('login.html', error=error, next_url=next_url)


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('user_nama', None)
    return redirect(url_for('login'))


# ── Admin routes ───────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if _login_blocked(request.remote_addr):
            return render_template('admin_login.html',
                                   error='Terlalu banyak percobaan. Coba lagi 15 menit lagi.'), 429
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        _login_failed(request.remote_addr)
        error = 'Password salah.'
    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    users = db.get_all_users()
    return render_template('admin_dashboard.html', users=users)


@app.route('/admin/user/<int:user_id>/approve', methods=['POST'])
@admin_required
def admin_approve(user_id):
    db.update_user_status(user_id, 'approved')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:user_id>/reject', methods=['POST'])
@admin_required
def admin_reject(user_id):
    db.update_user_status(user_id, 'rejected')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    db.delete_user(user_id)
    return redirect(url_for('admin_dashboard'))


# ── Home ───────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', pests=PESTS)


# ── Estate API ─────────────────────────────────────────────────────────────────
@app.route('/api/estates')
@login_required
def api_estates():
    from core.estate import _load_estate_data
    rows = _load_estate_data()
    result: dict = {}
    for row in rows:
        estate = str(row['Estate']).strip()
        if estate not in result:
            result[estate] = []
        result[estate].append({
            'blok':        str(row['Blok']).strip(),
            'tahun_tanam': int(row['Tahun Tanam']),
        })
    return jsonify(result)


# ── Ground Truth parse ─────────────────────────────────────────────────────────
@app.route('/api/ground-truth/parse', methods=['POST'])
@login_required
def gt_parse():
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({'error': 'No file'}), 400
    try:
        df = pd.read_excel(file)
        df.columns = [str(c).strip() for c in df.columns]
        columns = list(df.columns)
        records = df.where(pd.notnull(df), None).to_dict(orient='records')
        return jsonify({'columns': columns, 'records': records})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


# ── AI Chat Agent proxy ────────────────────────────────────────────────────────
# bagworm-agent runs as a separate Flask service (its own venv/conda env, pinned
# to older Flask/Werkzeug for openai-agents+litellm compatibility) — see
# bagworm_agent/bagworm-agent/. This just forwards the dashboard's chat widget
# to it over HTTP so the two apps' dependencies never have to mix.
BAGWORM_AGENT_URL = os.environ.get('BAGWORM_AGENT_URL', 'http://localhost:9556/bagworm-agent')


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    user_question = (data.get('message') or '').strip()
    if not user_question:
        return jsonify({'error': 'message is required'}), 400

    chat_id = f"dashboard_{session.get('user_id')}"

    try:
        r = requests.post(
            BAGWORM_AGENT_URL,
            json={'chat_id': chat_id, 'user_question': user_question},
            timeout=60,
        )
        r.raise_for_status()
        reply = r.json().get('chat_response')
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Chat agent tidak aktif. Hubungi admin untuk menjalankan bagworm-agent.'}), 503
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502

    if not reply:
        return jsonify({'error': 'Agent tidak memberikan jawaban, coba lagi.'}), 502

    return jsonify({'reply': reply})


BAGWORM_AGENT_BROADCAST_URL = os.environ.get(
    'BAGWORM_AGENT_BROADCAST_URL', 'http://localhost:9556/broadcast/census-result'
)


def _broadcast_new_census_result(session_id):
    """Fire-and-forget: send the freshly saved census session to bagworm-agent,
    which relays a WhatsApp summary to the configured broadcastNumbers.
    Best-effort — a broadcast failure must never fail the census job itself."""
    try:
        sess = db.get_session(session_id)
        if not sess:
            return
        payload = {
            'estate':      sess.get('estate'),
            'block':       sess.get('block'),
            'date_from':   sess.get('date_from'),
            'date_to':     sess.get('date_to'),
            'avg_larvae':  sess.get('avg_larvae'),
            'size_small':  sess.get('size_small'),
            'size_medium': sess.get('size_medium'),
            'size_big':    sess.get('size_big'),
        }
        requests.post(BAGWORM_AGENT_BROADCAST_URL, json=payload, timeout=15)
    except Exception as exc:
        print(f'[BROADCAST WARNING] Failed to broadcast census result {session_id}: {exc}')


# ── Browse Folder / File ───────────────────────────────────────────────────────
@app.route('/browse-folder')
@login_required
def browse_folder():
    import tkinter as tk                 # local only: opens a dialog on the server's desktop
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    folder = filedialog.askdirectory(parent=root, title='Pilih Folder Gambar')
    root.destroy()
    if folder:
        return jsonify({'path': folder.replace('/', '\\')})
    return jsonify({'path': ''})


@app.route('/browse-file')
@login_required
def browse_file():
    import tkinter as tk                 # local only: opens a dialog on the server's desktop
    from tkinter import filedialog
    filetypes = request.args.get('types', 'all')
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', True)
    if filetypes == 'yaml':
        types = [('YAML files', '*.yaml *.yml'), ('All files', '*.*')]
    elif filetypes == 'pt':
        types = [('PyTorch model', '*.pt'), ('All files', '*.*')]
    elif filetypes == 'tif':
        types = [('GeoTIFF', '*.tif *.tiff'), ('All files', '*.*')]
    elif filetypes == 'excel':
        types = [('Excel files', '*.xls *.xlsx'), ('All files', '*.*')]
    elif filetypes == 'csv':
        types = [('CSV files', '*.csv'), ('All files', '*.*')]
    else:
        types = [('All files', '*.*')]
    path = filedialog.askopenfilename(parent=root, title='Select File', filetypes=types)
    root.destroy()
    if path:
        return jsonify({'path': path.replace('/', '\\')})
    return jsonify({'path': ''})


# ── Job status ─────────────────────────────────────────────────────────────────
@app.route('/api/job/<job_id>')
@login_required
def api_job_status(job_id):
    job = db.get_census_job(job_id)
    if not job or not _can_access_job(job):
        return jsonify({'status': 'not_found'}), 404
    resp = {'status': job['status'], 'created_at': job['created_at'],
            'started_at': job['started_at'], 'finished_at': job['finished_at'],
            'name': job['meta'].get('display_name', '')}
    if job['status'] == 'queued':
        resp['position'] = db.census_queue_position(job_id)
        resp['running']  = db.census_jobs_running()
    elif job['status'] == 'done':
        sess = db.get_session(job['session_id']) if job['session_id'] else None
        resp.update({'session_id': job['session_id'],
                     'columns': (sess or {}).get('columns', []),
                     'records': (sess or {}).get('records', [])})
    elif job['status'] in ('error', 'cancelled'):
        resp['error'] = job['error']
    return jsonify(resp)


@app.route('/api/job/<job_id>/cancel', methods=['POST'])
@login_required
def api_job_cancel(job_id):
    job = db.get_census_job(job_id)
    if not job or not _can_access_job(job):
        return jsonify({'error': 'not_found'}), 404
    census_queue.request_stop(job_id)
    return jsonify({'ok': True})


def _is_admin_or_dev():
    return bool(session.get('is_admin')) or session.get('user_id') in DEV_USER_IDS


def _can_access_job(job):
    return job['user_id'] == session.get('user_id') or _is_admin_or_dev()


# ── Census session APIs ────────────────────────────────────────────────────────
@app.route('/api/census-session/<int:session_id>')
@login_required
def api_session_detail(session_id):
    sess = db.get_session(session_id)
    if not sess:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(sess)


@app.route('/api/census-session/<int:session_id>', methods=['DELETE'])
@login_required
def api_session_delete(session_id):
    is_admin = bool(session.get('is_admin')) or session.get('user_id') in DEV_USER_IDS
    brief   = db.get_session_brief(session_id)
    deleted = db.delete_session(session_id, session.get('user_id'), is_admin=is_admin)
    if not deleted:
        return jsonify({'error': 'Not found or not authorized to delete this session'}), 403
    if brief and brief.get('gcal_event_id'):
        gcal_reminder.delete_event(brief['gcal_event_id'])
    return jsonify({'ok': True})


@app.route('/api/census-sessions/delete', methods=['POST'])
@login_required
def api_sessions_bulk_delete():
    """Delete several sessions at once. Body: {"ids": [1, 2, ...]}.
    Same ownership rule as the single delete; sessions not allowed are skipped."""
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    try:
        ids = sorted({int(i) for i in ids})
    except (TypeError, ValueError):
        return jsonify({'error': 'ids tidak valid'}), 400
    if not ids:
        return jsonify({'error': 'Tidak ada data yang dipilih'}), 400

    is_admin = bool(session.get('is_admin')) or session.get('user_id') in DEV_USER_IDS
    deleted, skipped = [], []
    for sid in ids:
        brief = db.get_session_brief(sid)
        if db.delete_session(sid, session.get('user_id'), is_admin=is_admin):
            deleted.append(sid)
            if brief and brief.get('gcal_event_id'):
                gcal_reminder.delete_event(brief['gcal_event_id'])
        else:
            skipped.append(sid)
    return jsonify({'ok': True, 'deleted': deleted, 'skipped': skipped})


@app.route('/api/census-session/<int:session_id>/download')
@login_required
def api_session_download(session_id):
    """Zip of this session's crops + detection results stored in the GCS bucket."""
    sess = db.get_session(session_id)
    if not sess:
        return jsonify({'error': 'Not found'}), 404
    prefix = sess.get('gcs_prefix')
    if not prefix or not gcs_store.enabled():
        return jsonify({'error': 'Hasil sesi ini tidak tersimpan di cloud storage'}), 404
    try:
        zip_path, n = gcs_store.zip_session(prefix)
    except Exception as exc:
        print('[GCS DOWNLOAD ERROR]', exc)
        return jsonify({'error': f'Gagal mengambil file dari storage: {exc}'}), 502
    if n == 0:
        os.remove(zip_path)
        return jsonify({'error': 'Tidak ada file di storage untuk sesi ini'}), 404

    name = gcs_store.session_prefix(session_id, sess.get('estate'), sess.get('block'),
                                    sess.get('date_from') or sess.get('created_at'))
    resp = send_file(zip_path, mimetype='application/zip', as_attachment=True,
                     download_name=name.replace('/', '_') + '.zip')
    resp.call_on_close(lambda: os.path.exists(zip_path) and os.remove(zip_path))
    return resp


def _gps_csv_rows(sess):
    """[[B-P, M1, M2, M3, Latitude, Longitude], ...] for one session with Ground Truth."""
    gps = sess.get('gps_data')
    if gps is None or gps.get('_src') != gps_coords.SOURCE_TAG:
        # Not read yet, or read with an older rule (e.g. F2): re-read if the
        # photo folder still exists, otherwise keep what is stored.
        fresh = gps_coords.coords_for_folder(sess.get('folder_path'))
        if fresh:
            db.set_session_gps(sess['id'], fresh)
            gps = fresh
    gps = gps or {}

    def _m(v):
        if v is None or v == '':
            return ''
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return v

    rows = []
    for r in sess.get('gt_records') or []:
        bp = str(r.get('Baris-Pokok', '') or '').strip()
        if not bp:
            continue
        lat, lon = (gps.get(bp) or ['', ''])[:2]
        rows.append([bp, _m(r.get('M1')), _m(r.get('M2')), _m(r.get('M3')), lat, lon])
    return rows


def _csv_response(header, rows, filename):
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    resp = make_response('﻿' + buf.getvalue())   # BOM so Excel reads UTF-8
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@app.route('/api/census-session/<int:session_id>/gps-csv')
@login_required
def api_session_gps_csv(session_id):
    """CSV: B-P, M1, M2, M3 (Ground Truth), Latitude, Longitude (one F2 photo)."""
    sess = db.get_session(session_id)
    if not sess:
        return jsonify({'error': 'Not found'}), 404
    if not sess.get('gt_records'):
        return jsonify({'error': 'Sesi ini tidak memiliki Ground Truth'}), 404
    name = gcs_store.session_prefix(session_id, sess.get('estate'), sess.get('block'),
                                    sess.get('date_from') or sess.get('created_at'))
    return _csv_response(['B-P', 'M1', 'M2', 'M3', 'Latitude', 'Longitude'],
                         _gps_csv_rows(sess), name.replace('/', '_') + '_gps.csv')


@app.route('/api/census-sessions/gps-csv')
@login_required
def api_sessions_gps_csv():
    """One CSV for several sessions: ?ids=1,2,3. Sessions without Ground Truth are skipped."""
    try:
        ids = [int(i) for i in (request.args.get('ids') or '').split(',') if i.strip()]
    except ValueError:
        return jsonify({'error': 'ids tidak valid'}), 400
    rows = []
    for sid in ids:
        sess = db.get_session(sid)
        if not sess or not sess.get('gt_records'):
            continue
        tgl = (sess.get('date_from') or sess.get('created_at') or '')[:10]
        for r in _gps_csv_rows(sess):
            rows.append([sess.get('estate') or '', sess.get('block') or '', tgl] + r)
    if not rows:
        return jsonify({'error': 'Tidak ada sesi terpilih yang memiliki Ground Truth'}), 404
    return _csv_response(
        ['Estate', 'Block', 'Tanggal Sensus', 'B-P', 'M1', 'M2', 'M3', 'Latitude', 'Longitude'],
        rows, f'gps_ground_truth_{len(ids)}_sesi.csv')


# ── Develop APIs ────────────────────────────────────────────────────────────────
@app.route('/api/develop/chart-image')
@login_required
def api_develop_chart_image():
    if session.get('user_id') not in DEV_USER_IDS:
        return jsonify({'error': 'Unauthorized'}), 403
    model  = request.args.get('model')
    fname  = os.path.basename(request.args.get('file', ''))
    allowed = _DET_CHARTS if model == 'detection' else _CLS_CHARTS
    if fname not in allowed:
        return ('', 404)
    model_dir = _DET_DIR if model == 'detection' else _CLS_DIR
    fpath = os.path.join(model_dir, fname)
    if not os.path.exists(fpath):
        return ('', 404)
    return send_file(fpath, mimetype='image/png' if fname.endswith('.png') else 'image/jpeg')


@app.route('/api/develop/results-data')
@login_required
def api_develop_results_data():
    import csv as _csv
    if session.get('user_id') not in DEV_USER_IDS:
        return jsonify({'error': 'Unauthorized'}), 403
    model = request.args.get('model')
    model_dir = _DET_DIR if model == 'detection' else _CLS_DIR
    try:
        with open(os.path.join(model_dir, 'results.csv'), newline='', encoding='utf-8', errors='replace') as f:
            rows = [{k.strip(): v.strip() for k, v in row.items()}
                    for row in _csv.DictReader(f)]
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/develop/train', methods=['POST'])
@login_required
def api_develop_train():
    if session.get('user_id') not in DEV_USER_IDS:
        return jsonify({'error': 'Unauthorized'}), 403
    data       = request.get_json(silent=True) or {}
    mode       = data.get('mode')
    ds_path    = data.get('dataset_path', '').strip()
    pretrained = data.get('pretrained', '').strip()
    epochs     = int(data.get('epochs', 100))
    batch      = int(data.get('batch', 16))
    imgsz      = int(data.get('imgsz', 640))
    train_r    = float(data.get('split_train', 70)) / 100
    val_r      = float(data.get('split_val',   20)) / 100
    test_r     = round(1.0 - train_r - val_r, 4)

    if mode not in ('detection', 'classifier'):
        return jsonify({'error': 'Invalid mode'}), 400
    if not ds_path:
        return jsonify({'error': 'Dataset path is required'}), 400
    if test_r < 0:
        return jsonify({'error': 'Split train + val tidak boleh melebihi 100%'}), 400

    job_id = str(uuid.uuid4())
    _dev_jobs[job_id] = {'status': 'running', 'log': [], 'error': None}
    threading.Thread(
        target=_run_training,
        args=(job_id, mode, ds_path, epochs, batch, imgsz, (train_r, val_r, test_r), pretrained),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/api/develop/job/<job_id>')
@login_required
def api_develop_job(job_id):
    job = _dev_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    resp = {'status': job['status'], 'log': job.get('log', []), 'error': job.get('error')}
    if job['status'] == 'done':
        _, models = _load_develop_data()
        resp['models'] = models
    return jsonify(resp)


@app.route('/api/develop/model-info')
@login_required
def api_develop_model_info():
    _, models = _load_develop_data()
    return jsonify(models)


# ── Monitoring analytics APIs ──────────────────────────────────────────────────
@app.route('/api/outbreak')
@login_required
def api_outbreak():
    pest      = request.args.get('pest', '') or PEST_KEY
    estates   = request.args.getlist('estates')
    blocks    = request.args.getlist('blocks')
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None

    sessions = db.get_sessions_filtered(
        pest, None,
        estates=estates or None,
        blocks=blocks or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )

    def _nat_key(s):
        return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]

    result = []
    for s in sessions:
        records = s.get('records', [])
        row_col_map = {}
        row_col_val = {}
        row_col_dmax = {}
        f_vals = []
        for rec in records:
            bp  = str(rec.get('Baris-Pokok', '') or '')
            dec = str(rec.get('Decision', '') or '')
            rec_f_vals = []
            for fc in ['F1', 'F2', 'F3']:
                try:
                    v = float(rec.get(fc) or 0)
                    f_vals.append(v)
                    rec_f_vals.append(v)
                except (ValueError, TypeError):
                    pass
            if rec_f_vals:
                raw_avg = sum(rec_f_vals) / len(rec_f_vals)
                ai_val = round(raw_avg) if raw_avg >= 5 else round(raw_avg, 1)
            else:
                ai_val = None
            d_vals = []
            for dc in ['D1', 'D2', 'D3']:
                try:
                    d_vals.append(float(rec.get(dc) or 0))
                except (ValueError, TypeError):
                    pass
            dmax = round(max(d_vals), 2) if d_vals else None
            if '-' in bp:
                parts = bp.split('-', 1)
                row_col_map[(parts[0], parts[1])] = dec
                row_col_val[(parts[0], parts[1])] = ai_val
                row_col_dmax[(parts[0], parts[1])] = dmax
            else:
                if bp:
                    row_col_map[(bp, '1')] = dec
                    row_col_val[(bp, '1')] = ai_val
                    row_col_dmax[(bp, '1')] = dmax

        unique_rows = sorted({k[0] for k in row_col_map}, key=_nat_key)
        unique_cols = sorted({k[1] for k in row_col_map}, key=_nat_key)[:3]

        avg_larvae = round(sum(f_vals) / len(f_vals), 2) if f_vals else 0
        sc  = s.get('spray_count',    0) or 0
        nsc = s.get('no_spray_count', 0) or 0
        tot = sc + nsc
        spray_pct = round(sc / tot * 100, 1) if tot else None

        size_counts = {
            'small':  s.get('size_small',  0) or 0,
            'medium': s.get('size_medium', 0) or 0,
            'big':    s.get('size_big',    0) or 0,
        }
        size_total = sum(size_counts.values())
        if size_total:
            dominant_size_key = max(size_counts, key=size_counts.get)
            dominant_size_pct = round(size_counts[dominant_size_key] / size_total * 100, 1)
        else:
            dominant_size_key = None
            dominant_size_pct = None

        grid = [
            {'row': rn, 'cells': [
                {'col': cn, 'dec': row_col_map.get((rn, cn), ''), 'val': row_col_val.get((rn, cn)),
                 'dmax': row_col_dmax.get((rn, cn))}
                for cn in unique_cols
            ]}
            for rn in unique_rows
        ]

        peng_aktif, peng_hari = db.pengendalian_status(
            s.get('tanggal_pengendalian'), s.get('date_from'), s.get('created_at'))

        result.append({
            'id':             s['id'],
            'tanggal_pengendalian': s.get('tanggal_pengendalian') or '',
            'pengendalian_aktif':   peng_aktif,
            'pengendalian_hari':    peng_hari,
            'estate':         s.get('estate',    ''),
            'block':          s.get('block',     ''),
            'date_from':      s.get('date_from', ''),
            'date_to':        s.get('date_to',   ''),
            'avg_larvae':     avg_larvae,
            'spray_pct':      spray_pct,
            'spray':          sc,
            'no_spray':       nsc,
            'cols':           unique_cols,
            'grid':           grid,
            'not_confidence': bool(s.get('not_confidence') or 0),
            'dominant_size':     dominant_size_key,
            'dominant_size_pct': dominant_size_pct,
        })

    return jsonify(result)


@app.route('/api/gt-scatter')
@login_required
def api_gt_scatter():
    pest      = request.args.get('pest', '') or PEST_KEY
    estates   = request.args.getlist('estates')
    blocks    = request.args.getlist('blocks')
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    points    = db.get_gt_scatter(
        pest, None,
        estates or None, blocks or None,
        date_from, date_to,
    )
    return jsonify(points)


@app.route('/api/confusion-matrix')
@login_required
def api_confusion_matrix():
    pest      = request.args.get('pest', '') or PEST_KEY
    estates   = request.args.getlist('estates')
    blocks    = request.args.getlist('blocks')
    date_from  = request.args.get('date_from') or None
    date_to    = request.args.get('date_to')   or None
    count_only = request.args.get('mode') == 'count'
    optimize   = request.args.get('optimize') == '1'
    matrix     = db.get_confusion_matrix(
        pest, None,
        estates or None, blocks or None,
        date_from, date_to,
        count_only=count_only,
        optimize=optimize,
    )
    return jsonify(matrix)


@app.route('/api/size-scatter')
@login_required
def api_size_scatter():
    pest      = request.args.get('pest', '') or PEST_KEY
    estates   = request.args.getlist('estates')
    blocks    = request.args.getlist('blocks')
    date_from = request.args.get('date_from') or None
    date_to   = request.args.get('date_to')   or None
    points    = db.get_size_scatter(
        pest, None,
        estates or None, blocks or None,
        date_from, date_to,
    )
    return jsonify(points)


# ── Bagworm module blueprint (Census, Monitoring, Develop, FI-Recommendation) ──
bp = Blueprint('bagworm', __name__, url_prefix='/bagworm')


@bp.route('/')
@login_required
def menu():
    return redirect(url_for('index'))


@bp.route('/census')
@login_required
def census():
    return render_template('bagworm/census.html', pest=PEST, pest_key=PEST_KEY)


# ── Photo folder upload (browser -> server, resumable per file) ───────────────
def _upload_owned(upload_id):
    """Return the upload's meta if it exists and belongs to the current user."""
    try:
        meta = uploads.get_meta(upload_id)
    except uploads.UploadError:
        return None
    if not meta:
        return None
    if meta.get('user_id') != session.get('user_id') and not _is_admin_or_dev():
        return None
    return meta


@bp.route('/upload/start', methods=['POST'])
@login_required
def upload_start():
    data = request.get_json(silent=True) or {}
    upload_id = uploads.create(session.get('user_id'), data.get('name', ''))
    return jsonify({'upload_id': upload_id})


@bp.route('/upload/<upload_id>/status')
@login_required
def upload_status(upload_id):
    if not _upload_owned(upload_id):
        return jsonify({'error': 'Upload not found'}), 404
    return jsonify({'files': uploads.list_files(upload_id)})


@bp.route('/upload/<upload_id>/file', methods=['PUT'])
@login_required
def upload_file(upload_id):
    if not _upload_owned(upload_id):
        return jsonify({'error': 'Upload not found'}), 404
    try:
        size = uploads.save_file(upload_id, request.args.get('path', ''),
                                 request.stream, request.content_length)
    except uploads.UploadError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'size': size})


@bp.route('/upload/<upload_id>/finish', methods=['POST'])
@login_required
def upload_finish(upload_id):
    """Validate the folder structure before the user starts the census."""
    if not _upload_owned(upload_id):
        return jsonify({'error': 'Upload not found'}), 404
    try:
        root, rows, fronds = uploads.census_root(upload_id, census_processor._discover)
    except uploads.UploadError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'rows': len(rows), 'fronds': fronds,
                    'files': len(uploads.list_files(upload_id)),
                    'folder': os.path.basename(root)})


@bp.route('/census/generate', methods=['POST'])
@login_required
def census_generate():
    data      = request.get_json(silent=True) or {}
    upload_id = (data.get('upload_id') or '').strip()

    if upload_id:
        meta_up = _upload_owned(upload_id)
        if not meta_up:
            return jsonify({'error': 'Upload not found'}), 404
        try:
            folder_path, _, _ = uploads.census_root(upload_id, census_processor._discover)
        except uploads.UploadError as e:
            return jsonify({'error': str(e)}), 400
        display_name = os.path.basename(folder_path)
    elif CLOUD_MODE:
        return jsonify({'error': 'Upload foto terlebih dahulu'}), 400
    else:
        # Local (lab PC) mode: a folder on this machine
        folder_path = data.get('folder_path', '').strip()
        if not folder_path or not os.path.isdir(folder_path):
            return jsonify({'error': 'Folder not found'}), 400
        display_name = folder_path

    job_id  = str(uuid.uuid4())
    user_id = session.get('user_id')

    estate_val = data.get('estate', '').strip()
    block_val  = data.get('block',  '').strip()

    tahun_tanam_raw = _get_tahun_tanam(estate_val, block_val)
    if tahun_tanam_raw is None:
        try:
            tahun_tanam_raw = int(data.get('tahun_tanam', 0))
        except (ValueError, TypeError):
            tahun_tanam_raw = 0

    meta = {
        'folder_path':           folder_path,
        'display_name':          display_name,
        'date_from':             data.get('date_from', ''),
        'date_to':               data.get('date_to',   ''),
        'estate':                estate_val,
        'divisi':                data.get('divisi',    ''),
        'block':                 block_val,
        'tahun_tanam':           tahun_tanam_raw,
        'user_id':               user_id,
        'gt_columns':            data.get('gt_columns', []),
        'gt_records':            data.get('gt_records', []),
        'tanggal_pengendalian':  data.get('tanggal_pengendalian', '') or None,
    }

    db.create_census_job(job_id, PEST_KEY, user_id, meta, upload_id=upload_id or None)
    census_queue.notify()
    return jsonify({'job_id': job_id, 'position': db.census_queue_position(job_id)})


def _run_census_job(job, stop_event):
    """Census pipeline for one queued job (runs on the census_queue worker thread).
    Returns the new census_sessions id."""
    meta, job_id = job['meta'], job['id']

    saved_files = []   # (kind, path) of crops / detections written for this run
    df, size_dist, size_points = census_processor.process_files(
        meta['folder_path'], base_dir=BASE_DIR, stop_event=stop_event,
        saved_files=saved_files,
    )
    if stop_event.is_set():
        raise census_queue.JobCancelled()

    damage_df = damage_processor.process_damage(meta['folder_path'], stop_event=stop_event)
    if stop_event.is_set():
        raise census_queue.JobCancelled()
    if not damage_df.empty:
        df = df.merge(damage_df, on='Baris-Pokok', how='left')
    for _dcol in ['D1', 'D2', 'D3']:
        if _dcol not in df.columns:
            df[_dcol] = 0.0
        else:
            df[_dcol] = pd.to_numeric(df[_dcol], errors='coerce').fillna(0.0)

    if meta['tahun_tanam'] > 0:
        try:
            df = calculate_decision(df, meta['tahun_tanam'])
        except Exception:
            import traceback; print('[DECISION ERROR]', traceback.format_exc())
            df['Decision'] = 'N/A'

    records = df.to_dict(orient='records')
    columns = list(df.columns)

    new_session_id = db.save_census(
        pest                  = PEST_KEY,
        folder_path           = meta['folder_path'],
        date_from             = meta['date_from'],
        date_to               = meta['date_to'],
        estate                = meta['estate'],
        divisi                = meta['divisi'],
        block                 = meta['block'],
        job_id                = job_id,
        columns               = columns,
        records               = records,
        user_id               = meta['user_id'],
        size_dist             = size_dist,
        size_points           = size_points,
        gt_columns            = meta['gt_columns'],
        gt_records            = meta['gt_records'],
        tanggal_pengendalian  = meta['tanggal_pengendalian'],
    )

    # Coordinates per Baris-Pokok from the 2nd F1 photo, read now while the photos
    # still exist (uploads are removed after UPLOAD_RETENTION_DAYS).
    try:
        db.set_session_gps(new_session_id, gps_coords.coords_for_folder(meta['folder_path']))
    except Exception:
        import traceback; print('[GPS ERROR]', traceback.format_exc())

    # Copy crops + detections to the GCS bucket. A storage failure must not
    # fail the census: the result is already saved and files stay on disk.
    if gcs_store.enabled():
        try:
            prefix = gcs_store.upload_session(
                new_session_id, meta['estate'], meta['block'],
                meta['date_from'], saved_files,
            )
            if prefix:
                db.set_session_gcs_prefix(new_session_id, prefix)
        except Exception:
            import traceback; print('[GCS UPLOAD ERROR]', traceback.format_exc())

    _broadcast_new_census_result(new_session_id)

    # avg < 0.9 -> Google Calendar re-census reminder (31 days after census date)
    gcal_reminder.schedule_for_session(new_session_id, db)
    return new_session_id


@bp.route('/census/export', methods=['POST'])
@login_required
def census_export():
    data    = request.get_json(silent=True) or {}
    columns = data.get('columns', [])
    records = data.get('records', [])
    if not columns or not records:
        return jsonify({'error': 'No data'}), 400

    df  = pd.DataFrame(records, columns=columns)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name='census_result.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@bp.route('/monitoring')
@login_required
def monitoring():
    sessions = db.get_sessions(PEST_KEY, None)
    acc_map  = db.get_session_accuracies(PEST_KEY)
    for s in sessions:
        a = acc_map.get(s['id']) or {}
        s['gt_accuracy'] = round(a['accuracy'] * 100, 1) if a.get('accuracy') is not None else None
        s['gt_compared'] = a.get('total', 0)
    trend    = db.get_trend(PEST_KEY, None)
    return render_template(
        'bagworm/monitoring.html',
        pest=PEST, pest_key=PEST_KEY,
        sessions=sessions, trend=trend,
    )


@bp.route('/develop')
@login_required
def develop():
    if session.get('user_id') not in DEV_USER_IDS:
        return redirect(url_for('index'))
    dataset, models = _load_develop_data()
    return render_template(
        'bagworm/develop.html',
        pest=PEST, pest_key=PEST_KEY,
        dataset=dataset, models=models,
    )


@bp.route('/report', methods=['POST'])
@login_required
def report():
    data = request.get_json(silent=True) or {}
    return generate_excel_report(PEST_KEY, None, data)


@bp.route('/fi-recommendation', methods=['POST'])
@login_required
def fi_recommendation():
    from datetime import date as _date
    from PIL import Image as PILImage
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image as RLImage, HRFlowable, PageBreak,
    )

    data      = request.get_json(silent=True) or {}
    estates   = [e.strip() for e in data.get('estates', []) if str(e).strip()]
    blocks    = [b.strip() for b in data.get('blocks',  []) if str(b).strip()]
    date_from = data.get('date_from', '').strip()
    date_to   = data.get('date_to',   '').strip()

    all_sessions = db.get_sessions_filtered(
        PEST_KEY, None,
        estates=estates or None,
        blocks=blocks   or None,
        date_from=date_from or None,
        date_to=date_to     or None,
    )
    if not all_sessions:
        return jsonify({'error': 'No sessions found for the selected filter'}), 404

    all_sessions = [s for s in all_sessions if not s.get('not_confidence')]
    if not all_sessions:
        return jsonify({'error': 'No confident sessions found for the selected filter'}), 404

    def _norm(p):
        return os.path.normcase(os.path.normpath(p))

    def _norm_seg(s):
        return re.sub(r'[^a-z0-9]', '', str(s).lower())

    def _path_segs(p):
        return {_norm_seg(seg) for seg in re.split(r'[\\/]', str(p)) if seg}

    CONF_MIN = 0.8
    all_anns = []
    if os.path.isdir(_SEG_DIR):
        for jf in _glob.glob(os.path.join(_SEG_DIR, '*.json')):
            try:
                with open(jf, encoding='utf-8') as f:
                    ann = _json.load(f)
                src_folder = ann.get('source_folder', '')
                img_name   = ann.get('imagePath', '')
                img_path   = os.path.join(_SEG_DIR, img_name)
                shapes     = ann.get('shapes', [])
                if not src_folder or not os.path.isfile(img_path):
                    continue
                n_conf = sum(1 for sh in shapes if sh.get('confidence', 1.0) >= CONF_MIN)
                if n_conf > 0:
                    all_anns.append({
                        'n':          n_conf,
                        'img_path':   img_path,
                        'src_folder': _norm(src_folder),
                        'src_segs':   _path_segs(src_folder),
                        'src_row':    ann.get('source_row',   ''),
                        'src_frond':  ann.get('source_frond', ''),
                    })
            except Exception:
                pass

    def _top_images(estate, block, n=4):
        est_seg   = _norm_seg(estate)
        block_seg = _norm_seg(block)
        candidates = sorted(
            [a for a in all_anns if est_seg in a['src_segs'] and block_seg in a['src_segs']],
            key=lambda a: a['n'], reverse=True,
        )
        result, seen = [], set()
        for a in candidates:
            key = (a['src_folder'], a['src_row'] or a['src_frond'] or a['img_path'])
            if key in seen:
                continue
            seen.add(key)
            result.append(a)
            if len(result) == n:
                break
        return result

    from collections import defaultdict as _dd
    groups = _dd(list)
    for s in all_sessions:
        key = (s.get('estate', '–') or '–', s.get('block', '–') or '–')
        groups[key].append(s)

    buf    = io.BytesIO()
    W, H   = A4
    margin = 1.0 * cm
    doc    = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin,  bottomMargin=margin,
    )

    BLACK   = colors.HexColor('#1a1a1a')
    GREY    = colors.HexColor('#555555')
    LGREY   = colors.HexColor('#aaaaaa')
    BORDER  = colors.HexColor('#cccccc')
    HEAD_BG = colors.HexColor('#e8e8e8')
    ROW_ALT = colors.HexColor('#f5f5f5')
    ACCENT  = colors.HexColor('#1f6feb')

    def _ps(name, size, bold=False, color=None, after=2, before=0, align=TA_LEFT):
        return ParagraphStyle(name, fontSize=size,
                              fontName='Helvetica-Bold' if bold else 'Helvetica',
                              textColor=color or BLACK,
                              spaceAfter=after, spaceBefore=before,
                              alignment=align)

    s_title  = _ps('fi_title', 13, bold=True, after=2, align=TA_CENTER)
    s_sub    = _ps('fi_sub',    8, color=GREY, after=4, align=TA_CENTER)
    s_block  = _ps('fi_block', 10, bold=True, color=ACCENT, after=1)
    s_meta   = _ps('fi_meta',   7, color=GREY, after=3)
    s_legend = _ps('leg',       7, color=GREY, after=1)
    s_tiny   = _ps('tiny',      6, color=GREY, after=1)

    def _tbl8(rows, widths, bold_last=False):
        t = Table(rows, colWidths=widths)
        st = [
            ('BACKGROUND',    (0,0),(-1,0),  HEAD_BG),
            ('FONTNAME',      (0,0),(-1,0),  'Helvetica-Bold'),
            ('FONTSIZE',      (0,0),(-1,-1), 7),
            ('ALIGN',         (1,0),(-1,-1), 'CENTER'),
            ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1 if not bold_last else -2), [colors.white, ROW_ALT]),
            ('GRID',          (0,0),(-1,-1), 0.3, BORDER),
            ('TOPPADDING',    (0,0),(-1,-1), 2),
            ('BOTTOMPADDING', (0,0),(-1,-1), 2),
            ('LEFTPADDING',   (0,0),(-1,-1), 3),
            ('RIGHTPADDING',  (0,0),(-1,-1), 3),
        ]
        if bold_last:
            st += [('FONTNAME',   (0,-1),(-1,-1), 'Helvetica-Bold'),
                   ('BACKGROUND', (0,-1),(-1,-1), ROW_ALT)]
        t.setStyle(TableStyle(st))
        return t

    content_w = W - 2 * margin
    story = []

    story.append(Paragraph('Report Census', s_title))
    story.append(Paragraph('Bagworm Census', s_sub))
    story.append(HRFlowable(width='100%', thickness=1, color=BORDER))
    story.append(Spacer(1, 12))

    import re as _re
    def _nat(v): return [int(t) if t.isdigit() else t for t in _re.split(r'(\d+)', str(v))]

    RED_C    = colors.HexColor('#c0392b')
    GREEN_C  = colors.HexColor('#27ae60')
    NODATA_C = colors.HexColor('#d0d0d0')
    for idx, ((estate, block), sessions) in enumerate(sorted(groups.items())):
        if idx > 0:
            story.append(PageBreak())

        dates      = sorted({s['date_from'] for s in sessions if s.get('date_from')})
        date_label = f"{dates[0]}  –  {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else '–')
        story.append(Paragraph(f'Kebun: {estate}  |  Blok: {block}', s_block))
        story.append(Paragraph(f'Tanggal Census: {date_label}', s_meta))
        story.append(HRFlowable(width='100%', thickness=0.5, color=BORDER))

        t_small  = sum(int(s.get('size_small',  0) or 0) for s in sessions)
        t_medium = sum(int(s.get('size_medium', 0) or 0) for s in sessions)
        t_big    = sum(int(s.get('size_big',    0) or 0) for s in sessions)
        t_all    = t_small + t_medium + t_big
        f_avgs, d_vals = [], []
        for s in sessions:
            for r in (s.get('records') or []):
                vals = []
                for c in ['F1','F2','F3']:
                    try:
                        v = r.get(c)
                        if v is not None: vals.append(float(v))
                    except (TypeError, ValueError): pass
                if vals: f_avgs.append(sum(vals)/len(vals))
                for dc in ['D1','D2','D3']:
                    try:
                        v = r.get(dc)
                        if v is not None: d_vals.append(float(v))
                    except (TypeError, ValueError): pass
        avg_bw  = round(sum(f_avgs)/len(f_avgs), 2) if f_avgs else 0
        pct_dmg = round((sum(d_vals)/len(d_vals))*100, 2) if d_vals else 0
        pct     = lambda n: f'{round(n/t_all*100,1)}%' if t_all else '–'

        row_col_map = {}
        for s in sessions:
            for rec in (s.get('records') or []):
                bp  = str(rec.get('Baris-Pokok','') or '')
                dec = str(rec.get('Decision','') or '')
                if '-' in bp:
                    p = bp.split('-',1); row_col_map[(p[0],p[1])] = dec
                elif bp:
                    row_col_map[(bp,'1')] = dec

        CELL_H = 15; BP_W = 1.0 * cm; CELL_W = 17
        unique_rows = unique_cols = []
        sc = nsc = sp_pct = 0
        ob_grid = None

        if row_col_map:
            unique_rows = sorted({k[0] for k in row_col_map}, key=_nat)
            unique_cols = sorted({k[1] for k in row_col_map}, key=_nat)
            sc  = sum(1 for d in row_col_map.values() if d == 'Spray')
            nsc = sum(1 for d in row_col_map.values() if d == 'No Spray')
            tot = sc + nsc
            sp_pct = round(sc/tot*100,1) if tot else 0

            cw = min(CELL_W, (content_w * 0.72 - BP_W) / max(len(unique_cols), 1))
            grid_w = BP_W + cw * len(unique_cols)

            ob_data, ob_clr = [['B\\P'] + unique_cols], []
            for ri, rn in enumerate(unique_rows):
                row = [rn]
                for ci, cn in enumerate(unique_cols):
                    dec = row_col_map.get((rn, cn), '')
                    row.append('')
                    if dec == 'Spray':
                        ob_clr += [('BACKGROUND',(ci+1,ri+1),(ci+1,ri+1),RED_C),
                                   ('TEXTCOLOR', (ci+1,ri+1),(ci+1,ri+1),colors.white)]
                    elif dec == 'No Spray':
                        ob_clr += [('BACKGROUND',(ci+1,ri+1),(ci+1,ri+1),GREEN_C),
                                   ('TEXTCOLOR', (ci+1,ri+1),(ci+1,ri+1),colors.white)]
                    else:
                        ob_clr += [('BACKGROUND',(ci+1,ri+1),(ci+1,ri+1),NODATA_C)]
                ob_data.append(row)

            ob_grid = Table(ob_data,
                            colWidths=[BP_W] + [cw]*len(unique_cols),
                            rowHeights=[CELL_H]*(len(unique_rows)+1))
            ob_grid.setStyle(TableStyle([
                ('BACKGROUND',    (0,0),(-1,0),  HEAD_BG),
                ('FONTNAME',      (0,0),(-1,0),  'Helvetica-Bold'),
                ('FONTNAME',      (0,1),(0,-1),  'Helvetica-Bold'),
                ('FONTSIZE',      (0,0),(-1,-1), 7),
                ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
                ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
                ('ROWBACKGROUNDS',(0,1),(-1,-1), [colors.white, ROW_ALT]),
                ('GRID',          (0,0),(-1,-1), 0.3, BORDER),
                ('TOPPADDING',    (0,0),(-1,-1), 0),
                ('BOTTOMPADDING', (0,0),(-1,-1), 0),
                ('LEFTPADDING',   (0,0),(-1,-1), 1),
                ('RIGHTPADDING',  (0,0),(-1,-1), 1),
            ] + ob_clr))

            story.append(Paragraph('Outbreak — Keputusan Semprot',
                                   _ps('obt', 8, bold=True, after=1)))
            story.append(Paragraph(
                f'<font color="#c0392b">■</font> Spray: <b>{sc}</b> ({sp_pct}%)  '
                f'<font color="#27ae60">■</font> No Spray: <b>{nsc}</b>',
                s_legend))

            pad_w = max((content_w - grid_w) / 2, 0)
            if pad_w > 0:
                centered = Table([[Spacer(pad_w, 1), ob_grid, Spacer(pad_w, 1)]],
                                  colWidths=[pad_w, grid_w, pad_w])
                centered.setStyle(TableStyle([
                    ('LEFTPADDING',   (0,0),(-1,-1), 0),
                    ('RIGHTPADDING',  (0,0),(-1,-1), 0),
                    ('TOPPADDING',    (0,0),(-1,-1), 0),
                    ('BOTTOMPADDING', (0,0),(-1,-1), 0),
                ]))
                story.append(centered)
            else:
                story.append(ob_grid)
        else:
            story.append(Paragraph('(tidak ada data outbreak)', s_tiny))

        story.append(Spacer(1, 4))
        hw = content_w / 2 - 2
        w1 = hw * 0.55; w2 = hw - w1
        size_t = _tbl8([
            ['Ukuran','Jml','%'],
            ['Kecil',  str(t_small),  pct(t_small)],
            ['Sedang', str(t_medium), pct(t_medium)],
            ['Besar',  str(t_big),    pct(t_big)],
            ['Total',  str(t_all),    '100%' if t_all else '–'],
        ], [w1, w2*0.5, w2*0.5], bold_last=True)
        stat_t = _tbl8([
            ['Metrik','Nilai'],
            ['Rata-rata Ulat', str(avg_bw)],
            ['% Kerusakan',    f'{pct_dmg}%'],
        ], [w1, w2])

        tbl_row = Table([
            [Paragraph('Distribusi Ukuran', _ps('dh',7,bold=True,after=1)),
             Paragraph('', _ps('sh',7,after=1))],
            [size_t, stat_t],
        ], colWidths=[hw, hw])
        tbl_row.setStyle(TableStyle([
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
            ('TOPPADDING',    (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 0),
        ]))
        story.append(tbl_row)

        story.append(Spacer(1, 4))
        story.append(Paragraph('Gambar Deteksi Tertinggi',
                                _ps('gh',7,bold=True,after=2)))

        top_imgs   = _top_images(estate, block, n=4)
        cell_img_w = (content_w - 0.3 * cm) / 2

        ob_h      = (len(unique_rows)+1) * CELL_H if unique_rows else 0
        used_h    = 32 + ob_h + 60 + 30
        avail_h   = H - 2 * margin - used_h
        max_img_h = max((avail_h / 2) - 14, 50)   # leave room for the caption line
        s_cap     = _ps('img_cap', 7, color=GREY, after=0, align=TA_CENTER)

        if top_imgs:
            def _make_cell(ann):
                cap_parts = [f"Baris-Pokok: <b>{ann['src_row'] or '–'}</b>"]
                if ann['src_frond']:
                    cap_parts.append(ann['src_frond'])
                cap_parts.append(f"{ann['n']} deteksi")
                caption = Paragraph('  ·  '.join(cap_parts), s_cap)
                try:
                    pil    = PILImage.open(ann['img_path']).convert('RGB')
                    ow, oh = pil.size
                    scale  = min(cell_img_w / ow, max_img_h / oh)
                    tw, th = int(ow * scale), int(oh * scale)
                    pil_r  = pil.resize((tw, th), PILImage.LANCZOS)
                    tmp    = io.BytesIO()
                    pil_r.save(tmp, format='JPEG', quality=98, subsampling=0)
                    tmp.seek(0)
                    return [RLImage(tmp, width=tw, height=th), caption]
                except Exception:
                    return [Paragraph('(error)', s_tiny), caption]

            cells = [_make_cell(a) for a in top_imgs]
            while len(cells) < 4:
                cells.append(Paragraph('', _ps('_e',7,after=0)))

            img_grid = Table([cells[:2], cells[2:]], colWidths=[cell_img_w, cell_img_w])
            img_grid.setStyle(TableStyle([
                ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
                ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
                ('TOPPADDING',    (0,0),(-1,-1), 2),
                ('BOTTOMPADDING', (0,0),(-1,-1), 2),
                ('LEFTPADDING',   (0,0),(-1,-1), 1),
                ('RIGHTPADDING',  (0,0),(-1,-1), 1),
            ]))
            story.append(img_grid)
        else:
            story.append(Paragraph(
                f'(Tidak ada gambar dengan confidence ≥ {CONF_MIN})', s_tiny))

        story.append(Spacer(1, 6))
        story.append(HRFlowable(width='100%', thickness=0.4, color=BORDER))
        story.append(Spacer(1, 2))
        story.append(Paragraph(
            f'Generated on {_date.today().strftime("%d %B %Y")}  |  LEC Digital Transformation Project',
            _ps('footer', 6, color=LGREY, after=0, align=TA_CENTER),
        ))

    doc.build(story)
    buf.seek(0)
    fname = f"Report_Census_{_date.today().strftime('%Y%m%d')}.pdf"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype='application/pdf')


app.register_blueprint(bp)


if __name__ == '__main__':
    # Local development only; on the server run gunicorn (see deploy/).
    app.run(host='0.0.0.0', debug=not CLOUD_MODE, port=5001)
