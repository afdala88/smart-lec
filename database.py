import sqlite3
import json
import os
import re
import hashlib
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'census.db')

_NC_EXCLUDE = "COALESCE(not_confidence, 0) = 0"


def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.execute('''
            CREATE TABLE IF NOT EXISTS census_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pest        TEXT    NOT NULL,
                folder_path TEXT,
                date_from   TEXT,
                date_to     TEXT,
                estate      TEXT,
                divisi      TEXT,
                block       TEXT,
                created_at  TEXT    NOT NULL,
                job_id      TEXT,
                columns     TEXT,
                records     TEXT,
                total       INTEGER DEFAULT 0
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                nama         TEXT    NOT NULL,
                nik          TEXT    NOT NULL UNIQUE,
                no_whatsapp  TEXT    NOT NULL,
                password     TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   TEXT    NOT NULL
            )
        ''')
        # migrate existing users table — add columns if missing
        for _col in (
            "password TEXT NOT NULL DEFAULT ''",
            "modules TEXT NOT NULL DEFAULT '[]'",
            "requested_module TEXT DEFAULT NULL",
        ):
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {_col}")
            except Exception:
                pass
        # migrate existing DB — add columns if missing
        for col in ('estate', 'divisi', 'user_id'):
            try:
                con.execute(f'ALTER TABLE census_sessions ADD COLUMN {col} INTEGER')
            except Exception:
                pass
        for col in ('estate', 'divisi'):
            try:
                con.execute(f'ALTER TABLE census_sessions ADD COLUMN {col} TEXT')
            except Exception:
                pass
        for col in ('spray_count', 'no_spray_count'):
            try:
                con.execute(f'ALTER TABLE census_sessions ADD COLUMN {col} INTEGER DEFAULT 0')
            except Exception:
                pass
        for col in ('size_small', 'size_medium', 'size_big'):
            try:
                con.execute(f'ALTER TABLE census_sessions ADD COLUMN {col} INTEGER DEFAULT 0')
            except Exception:
                pass
        try:
            con.execute("ALTER TABLE census_sessions ADD COLUMN size_data TEXT DEFAULT '[]'")
        except Exception:
            pass
        for col in ('gt_columns TEXT DEFAULT \'[]\'', 'gt_records TEXT DEFAULT \'[]\''):
            try:
                con.execute(f'ALTER TABLE census_sessions ADD COLUMN {col}')
            except Exception:
                pass
        try:
            con.execute("ALTER TABLE census_sessions ADD COLUMN tanggal_pengendalian TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE census_sessions ADD COLUMN avg_larvae REAL DEFAULT NULL")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE census_sessions ADD COLUMN not_confidence INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            con.execute("ALTER TABLE census_sessions ADD COLUMN gcal_event_id TEXT DEFAULT NULL")
        except Exception:
            pass
        con.execute('''
            CREATE TABLE IF NOT EXISTS census_jobs (
                id          TEXT PRIMARY KEY,
                pest        TEXT NOT NULL,
                user_id     INTEGER,
                upload_id   TEXT,
                status      TEXT NOT NULL DEFAULT 'queued',
                meta        TEXT NOT NULL,
                error       TEXT,
                session_id  INTEGER,
                created_at  TEXT NOT NULL,
                started_at  TEXT,
                finished_at TEXT
            )
        ''')
        # Backfill spray_count / no_spray_count for existing rows
        rows = con.execute(
            'SELECT id, records FROM census_sessions WHERE spray_count IS NULL OR (spray_count = 0 AND no_spray_count = 0)'
        ).fetchall()
        for row in rows:
            try:
                records = json.loads(row['records'] or '[]')
                spray    = sum(1 for r in records if r.get('Decision') == 'Spray')
                no_spray = sum(1 for r in records if r.get('Decision') == 'No Spray')
                if spray or no_spray:
                    con.execute(
                        'UPDATE census_sessions SET spray_count=?, no_spray_count=? WHERE id=?',
                        (spray, no_spray, row['id'])
                    )
            except Exception:
                pass
        # Backfill avg_larvae for sessions where it's NULL
        rows_al = con.execute(
            'SELECT id, records FROM census_sessions WHERE avg_larvae IS NULL'
        ).fetchall()
        for row in rows_al:
            try:
                recs = json.loads(row['records'] or '[]')
                f_vals = []
                for r in recs:
                    for fc in ['F1', 'F2', 'F3']:
                        v = r.get(fc)
                        if v is None:
                            continue
                        try:
                            f_vals.append(float(v))
                        except (ValueError, TypeError):
                            pass
                avg = round(sum(f_vals) / len(f_vals), 4) if f_vals else 0.0
                con.execute('UPDATE census_sessions SET avg_larvae=? WHERE id=?', (avg, row['id']))
            except Exception:
                pass
        # Backfill not_confidence for sessions where it's NULL
        rows_nc = con.execute(
            "SELECT id, pest, user_id, estate, block, created_at, tanggal_pengendalian "
            "FROM census_sessions WHERE not_confidence IS NULL ORDER BY created_at ASC"
        ).fetchall()
        for row in rows_nc:
            try:
                nc = _compute_nc_flag(
                    con, row['pest'], row['user_id'],
                    row['estate'] or '', row['block'] or '',
                    row['created_at'], row['tanggal_pengendalian'],
                )
                con.execute('UPDATE census_sessions SET not_confidence=? WHERE id=?', (nc, row['id']))
            except Exception:
                pass
        con.commit()


# ── Need-to-Visit flag ─────────────────────────────────────────────────────────

def _compute_nc_flag(con, pest, user_id, estate, block, created_at, tanggal_pengendalian):
    if tanggal_pengendalian:
        return 0
    if not estate or not block:
        return 0
    prev = con.execute(
        '''SELECT avg_larvae FROM census_sessions
           WHERE pest=? AND user_id=? AND estate=? AND block=? AND created_at < ?
           ORDER BY created_at DESC LIMIT 1''',
        (pest, user_id, estate, block, created_at),
    ).fetchone()
    if prev is None:
        return 0
    return 1 if float(prev[0] or 0.0) >= 1.0 else 0


# ── Census job queue ───────────────────────────────────────────────────────────
# status: queued -> running -> done | error | cancelled

def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def create_census_job(job_id, pest, user_id, meta, upload_id=None):
    with _conn() as con:
        con.execute(
            '''INSERT INTO census_jobs (id, pest, user_id, upload_id, status, meta, created_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)''',
            (job_id, pest, user_id, upload_id, json.dumps(meta), _now()),
        )
        con.commit()


def claim_next_census_job():
    """Atomically move the oldest queued job to 'running' and return it, or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM census_jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = con.execute(
            "UPDATE census_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (_now(), row['id']),
        )
        con.commit()
        if cur.rowcount != 1:
            return None
    return get_census_job(row['id'])


def get_census_job(job_id):
    with _conn() as con:
        row = con.execute('SELECT * FROM census_jobs WHERE id = ?', (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['meta'] = json.loads(d['meta'] or '{}')
    return d


def finish_census_job(job_id, status, error=None, session_id=None):
    with _conn() as con:
        con.execute(
            'UPDATE census_jobs SET status = ?, error = ?, session_id = ?, finished_at = ? WHERE id = ?',
            (status, error, session_id, _now(), job_id),
        )
        con.commit()


def cancel_queued_census_job(job_id):
    """Cancel a job that has not started yet. Returns True if it was queued."""
    with _conn() as con:
        cur = con.execute(
            "UPDATE census_jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'",
            (_now(), job_id),
        )
        con.commit()
        return cur.rowcount == 1


def census_queue_position(job_id):
    """1-based position among queued jobs (0 if not queued)."""
    with _conn() as con:
        row = con.execute('SELECT created_at, rowid FROM census_jobs WHERE id = ?', (job_id,)).fetchone()
        if not row:
            return 0
        n = con.execute(
            '''SELECT COUNT(*) FROM census_jobs WHERE status = 'queued'
               AND (created_at < ? OR (created_at = ? AND rowid <= ?))''',
            (row['created_at'], row['created_at'], row['rowid']),
        ).fetchone()[0]
    return n


def census_jobs_running():
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM census_jobs WHERE status = 'running'").fetchone()[0]


def fail_interrupted_census_jobs():
    """Jobs left 'running' by a previous process (server restart) can't resume."""
    with _conn() as con:
        con.execute(
            "UPDATE census_jobs SET status = 'error', error = 'Server restart saat proses berjalan. Jalankan ulang census.', "
            "finished_at = ? WHERE status = 'running'",
            (_now(),),
        )
        con.commit()


def upload_ids_in_use():
    """Upload ids referenced by jobs that are still queued or running."""
    with _conn() as con:
        rows = con.execute(
            "SELECT upload_id FROM census_jobs WHERE status IN ('queued', 'running') AND upload_id IS NOT NULL"
        ).fetchall()
    return {r[0] for r in rows}


# ── Google Calendar reminder bookkeeping ───────────────────────────────────────

def get_session_brief(session_id):
    """Lightweight session row (no records) for the calendar reminder."""
    with _conn() as con:
        row = con.execute(
            '''SELECT id, pest, estate, divisi, block, date_from, date_to, created_at,
                      avg_larvae, gcal_event_id
               FROM census_sessions WHERE id = ?''', (session_id,)
        ).fetchone()
    return dict(row) if row else None


def get_sessions_for_reminder(threshold):
    """Sessions with avg_larvae below threshold and no calendar event yet."""
    with _conn() as con:
        rows = con.execute(
            '''SELECT id, pest, estate, divisi, block, date_from, date_to, created_at,
                      avg_larvae, gcal_event_id
               FROM census_sessions
               WHERE avg_larvae IS NOT NULL AND avg_larvae < ?
                 AND (gcal_event_id IS NULL OR gcal_event_id = '')
               ORDER BY date_from''', (threshold,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_gcal_event_id(session_id, event_id):
    with _conn() as con:
        con.execute('UPDATE census_sessions SET gcal_event_id = ? WHERE id = ?', (event_id, session_id))
        con.commit()


# ── User / Auth functions ──────────────────────────────────────────────────────

def _hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def register_user(nama, nik, no_whatsapp, password, requested_module=None):
    """Insert a new user with status='pending'. Returns new id or None if NIK exists."""
    try:
        with _conn() as con:
            cur = con.execute(
                '''INSERT INTO users
                   (nama, nik, no_whatsapp, password, status, created_at, requested_module)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (nama.strip(), nik.strip(), no_whatsapp.strip(), _hash_password(password),
                 'pending', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 requested_module or None)
            )
            con.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # NIK already exists


def get_user_by_nik(nik):
    with _conn() as con:
        row = con.execute('SELECT * FROM users WHERE nik = ?', (nik.strip(),)).fetchone()
    return dict(row) if row else None


def verify_user(nik, password):
    """Return user dict if NIK + password match, else None."""
    user = get_user_by_nik(nik)
    if user and user.get('password') == _hash_password(password):
        return user
    return None


def get_all_users():
    with _conn() as con:
        rows = con.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    return [dict(r) for r in rows]


def get_user_by_id(user_id):
    with _conn() as con:
        row = con.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    return dict(row) if row else None


def update_user_status(user_id, status):
    with _conn() as con:
        con.execute('UPDATE users SET status = ? WHERE id = ?', (status, user_id))
        con.commit()


def update_user_modules(user_id, modules):
    """Store a list of module keys as JSON on the user row."""
    with _conn() as con:
        con.execute('UPDATE users SET modules = ? WHERE id = ?',
                    (json.dumps(list(modules)), user_id))
        con.commit()


def get_user_modules(user_id):
    with _conn() as con:
        row = con.execute('SELECT modules FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row['modules'] or '[]')
    except Exception:
        return []


def user_has_module(user_id, module_key):
    """Return True if the user has the given module enabled (or has '*' wildcard)."""
    mods = get_user_modules(user_id)
    return '*' in mods or module_key in mods


def delete_user(user_id):
    with _conn() as con:
        con.execute('DELETE FROM users WHERE id = ?', (user_id,))
        con.commit()


def save_census(pest, folder_path, date_from, date_to, block, job_id, columns, records,
                estate='', divisi='', user_id=None, size_dist=None, size_points=None,
                gt_columns=None, gt_records=None, tanggal_pengendalian=None):
    """Persist one completed census session. Returns the new row id."""
    _damage_cols = {'D1', 'D2', 'D3'}
    _exclude_cols = {'Baris-Pokok', 'Decision'} | _damage_cols
    frond_cols = [c for c in columns if c not in _exclude_cols]

    def _safe_int(v):
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    total    = sum(
        sum(_safe_int(row.get(c, 0)) for c in frond_cols)
        for row in records
    )
    spray_count    = sum(1 for r in records if r.get('Decision') == 'Spray')
    no_spray_count = sum(1 for r in records if r.get('Decision') == 'No Spray')
    sd = size_dist or {}

    _f_vals = []
    for _r in records:
        for _fc in ['F1', 'F2', 'F3']:
            _v = _r.get(_fc)
            if _v is None:
                continue
            try:
                _f_vals.append(float(_v))
            except (ValueError, TypeError):
                pass
    avg_larvae = round(sum(_f_vals) / len(_f_vals), 4) if _f_vals else 0.0

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with _conn() as con:
        nc_flag = _compute_nc_flag(
            con, pest, user_id, estate or '', block or '', created_at, tanggal_pengendalian
        )
        cur = con.execute('''
            INSERT INTO census_sessions
                (pest, folder_path, date_from, date_to, estate, divisi, block,
                 created_at, job_id, columns, records, total, user_id,
                 spray_count, no_spray_count, size_small, size_medium, size_big, size_data,
                 gt_columns, gt_records, tanggal_pengendalian, avg_larvae, not_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            pest,
            folder_path,
            date_from or '',
            date_to   or '',
            estate    or '',
            divisi    or '',
            block     or '',
            created_at,
            job_id,
            json.dumps(columns),
            json.dumps(records),
            total,
            user_id,
            spray_count,
            no_spray_count,
            sd.get('small',  0),
            sd.get('medium', 0),
            sd.get('big',    0),
            json.dumps(size_points or []),
            json.dumps(gt_columns or []),
            json.dumps(gt_records or []),
            tanggal_pengendalian or None,
            avg_larvae,
            nc_flag,
        ))
        con.commit()
        return cur.lastrowid


def get_sessions(pest, user_id=None):
    """Return sessions for a pest, newest first. user_id=None returns sessions for all users."""
    query  = '''
        SELECT id, pest, folder_path, date_from, date_to,
               estate, divisi, block, created_at, total,
               size_small, size_medium, size_big,
               CASE WHEN gt_records IS NOT NULL AND gt_records != '[]' THEN 1 ELSE 0 END AS has_gt,
               COALESCE(not_confidence, 0) AS not_confidence,
               tanggal_pengendalian,
               avg_larvae,
               user_id
        FROM   census_sessions
        WHERE  pest = ?
    '''
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    query += ' ORDER BY created_at DESC'
    with _conn() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id, user_id=None):
    """Return one session, including parsed columns & records. user_id=None allows any owner."""
    query  = 'SELECT * FROM census_sessions WHERE id = ?'
    params = [session_id]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    with _conn() as con:
        row = con.execute(query, params).fetchone()
    if row is None:
        return None
    d = dict(row)
    d['columns']    = json.loads(d['columns']    or '[]')
    d['records']    = json.loads(d['records']    or '[]')
    d['gt_columns'] = json.loads(d.get('gt_columns') or '[]')
    d['gt_records'] = json.loads(d.get('gt_records') or '[]')
    return d


def delete_session(session_id, user_id, is_admin=False):
    """Delete session if it belongs to user_id, or any session when is_admin=True.
    Returns True if a row was actually deleted, False otherwise."""
    with _conn() as con:
        if is_admin:
            cur = con.execute('DELETE FROM census_sessions WHERE id = ?', (session_id,))
        else:
            cur = con.execute('DELETE FROM census_sessions WHERE id = ? AND user_id = ?', (session_id, user_id))
        con.commit()
        return cur.rowcount > 0


def get_sessions_filtered(pest, user_id=None, estates=None, blocks=None, date_from=None, date_to=None):
    """Return full session rows (with columns & records) matching optional filters.

    estates / blocks may each be a list of values (IN filter) or None (no filter).
    user_id=None returns matching sessions for all users.
    """
    query  = 'SELECT * FROM census_sessions WHERE pest = ?'
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    if estates:
        placeholders = ','.join('?' * len(estates))
        query += f' AND estate IN ({placeholders})'
        params.extend(estates)
    if blocks:
        placeholders = ','.join('?' * len(blocks))
        query += f' AND block IN ({placeholders})'
        params.extend(blocks)
    if date_from:
        query += ' AND date_from >= ?'
        params.append(date_from)
    if date_to:
        query += ' AND date_from <= ?'
        params.append(date_to)
    query += ' ORDER BY estate, block, date_from ASC'
    with _conn() as con:
        rows = con.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d['columns'] = json.loads(d['columns'] or '[]')
        d['records'] = json.loads(d['records'] or '[]')
        d['not_confidence'] = bool(d.get('not_confidence') or 0)
        result.append(d)
    return result


def get_gt_scatter(pest, user_id=None, estates=None, blocks=None, date_from=None, date_to=None):
    """Return AI vs GT scatter points {x, y, d, bp} from sessions that have GT data.

    x = avg(F1, F2, F3) from AI result  [chart swaps: displayed as Y]
    y = value among (M1, M2, M3) closest to avg(F1,F2,F3)  [chart swaps: displayed as X]
        — if the session has tanggal_pengendalian filled, y = max(M1, M2, M3) instead
    d = Decision ('Spray' | 'No Spray')
    bp = Baris-Pokok label
    user_id=None aggregates points across all users.
    """
    query  = f'''SELECT records, gt_records, tanggal_pengendalian FROM census_sessions
                WHERE  pest = ?
                  AND  gt_records IS NOT NULL AND gt_records != '[]'
                  AND  {_NC_EXCLUDE}'''
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    if estates:
        query += f' AND estate IN ({",".join("?" * len(estates))})'
        params.extend(estates)
    if blocks:
        query += f' AND block IN ({",".join("?" * len(blocks))})'
        params.extend(blocks)
    if date_from:
        query += ' AND date_from >= ?'; params.append(date_from)
    if date_to:
        query += ' AND date_from <= ?'; params.append(date_to)

    with _conn() as con:
        rows = con.execute(query, params).fetchall()

    def _avg(rec, cols):
        vals = []
        for c in cols:
            try:
                v = rec.get(c)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                pass
        return sum(vals) / len(vals) if vals else None

    points = []
    for row in rows:
        try:
            records    = json.loads(row['records']    or '[]')
            gt_records = json.loads(row['gt_records'] or '[]')
        except Exception:
            continue
        if not records or not gt_records:
            continue

        # Session with prior control (tanggal pengendalian filled) -> GT = max(M1..M3)
        use_max = bool((row['tanggal_pengendalian'] or '').strip())

        # Build GT map: Baris-Pokok -> list of M values
        gt_map = {}
        for r in gt_records:
            bp = str(r.get('Baris-Pokok', '') or '').strip()
            if not bp:
                continue
            vals = []
            for c in ['M1', 'M2', 'M3']:
                try:
                    v = r.get(c)
                    if v is not None:
                        vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            if vals:
                gt_map[bp] = vals

        for r in records:
            bp = str(r.get('Baris-Pokok', '') or '').strip()
            if not bp or bp not in gt_map:
                continue
            ai_avg = _avg(r, ['F1', 'F2', 'F3'])
            if ai_avg is None:
                continue
            if use_max:
                closest_m = max(gt_map[bp])
            else:
                # Pick the M value closest to the AI average
                closest_m = min(gt_map[bp], key=lambda v: abs(v - ai_avg))
            points.append({
                'x':  round(ai_avg,    3),
                'y':  round(closest_m, 3),
                'd':  r.get('Decision', ''),
                'bp': bp,
            })
    return points


_NO_SPRAY_THRESHOLD  = 3    # GT manual value below this => 'No Spray'
_COUNT_ONLY_THRESHOLD = 2   # avg(F1,F2,F3) below this => 'No Spray' (count_only mode)


def _optimized_decisions(records):
    """Return {Baris-Pokok: 'Spray'} for cells flipped by the outbreak optimizer.

    Mirrors optimizeOutbreak() in monitoring_page.html on the same grid as /api/outbreak:
    a cell with value < 2 turns 'Spray' when >= 50% of its 8 neighbors (with data)
    have value >= 2. Neighbors are judged on original values, so flips don't cascade.
    """
    def _nat_key(s):
        return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]

    cell_val = {}
    cell_bp  = {}
    for rec in records:
        bp = str(rec.get('Baris-Pokok', '') or '')
        if not bp:
            continue
        f_vals = []
        for fc in ['F1', 'F2', 'F3']:
            try:
                f_vals.append(float(rec.get(fc) or 0))
            except (ValueError, TypeError):
                pass
        if f_vals:
            raw_avg = sum(f_vals) / len(f_vals)
            val = round(raw_avg) if raw_avg >= 5 else round(raw_avg, 1)
        else:
            val = None
        key = tuple(bp.split('-', 1)) if '-' in bp else (bp, '1')
        cell_val[key] = val
        cell_bp[key]  = bp.strip()

    rows = sorted({k[0] for k in cell_val}, key=_nat_key)
    cols = sorted({k[1] for k in cell_val}, key=_nat_key)[:3]

    flipped = {}
    for ri, rn in enumerate(rows):
        for ci, cn in enumerate(cols):
            val = cell_val.get((rn, cn))
            if val is None or val >= 2:
                continue
            red = total = 0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = ri + dr, ci + dc
                    if not (0 <= nr < len(rows) and 0 <= nc < len(cols)):
                        continue
                    nval = cell_val.get((rows[nr], cols[nc]))
                    if nval is None:
                        continue
                    total += 1
                    if nval >= 2:
                        red += 1
            if total and red / total >= 0.5:
                flipped[cell_bp[(rn, cn)]] = 'Spray'
    return flipped


def get_confusion_matrix(pest, user_id=None, estates=None, blocks=None, date_from=None, date_to=None,
                          count_only=False, optimize=False):
    """Return aggregated Spray/No Spray confusion matrix across matching sessions.

    Uses the same pairing as the AI-vs-Manual scatter: for each AI record,
    the GT (M1-M3) value closest to the AI average (F1-F3) is picked as the
    matched manual reading. Actual = 'No Spray' if that manual value is below
    _NO_SPRAY_THRESHOLD, else 'Spray'.

    Predicted:
      count_only=False (default) -> the stored 'Decision' field (count + damage logic).
      count_only=True            -> recomputed from bagworm count alone:
                                     'Spray' if avg(F1,F2,F3) >= _COUNT_ONLY_THRESHOLD, else 'No Spray'.
      optimize=True overrides cells flipped by the outbreak optimizer
      (see _optimized_decisions).
    """
    query  = f'''SELECT estate, block, records, gt_records FROM census_sessions
                WHERE  pest = ?
                  AND  gt_records IS NOT NULL AND gt_records != '[]'
                  AND  {_NC_EXCLUDE}'''
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    if estates:
        query += f' AND estate IN ({",".join("?" * len(estates))})'
        params.extend(estates)
    if blocks:
        query += f' AND block IN ({",".join("?" * len(blocks))})'
        params.extend(blocks)
    if date_from:
        query += ' AND date_from >= ?'; params.append(date_from)
    if date_to:
        query += ' AND date_from <= ?'; params.append(date_to)

    with _conn() as con:
        rows = con.execute(query, params).fetchall()

    def _avg(rec, cols):
        vals = []
        for c in cols:
            try:
                v = rec.get(c)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                pass
        return sum(vals) / len(vals) if vals else None

    def _metrics(tp, tn, fp, fn):
        total     = tp + tn + fp + fn
        precision = tp / (tp + fp) if (tp + fp) else None
        recall    = tp / (tp + fn) if (tp + fn) else None
        return {
            'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn, 'total': total,
            'accuracy':    (tp + tn) / total if total else None,
            'precision':   precision,
            'recall':      recall,
            'f1':          (2 * precision * recall / (precision + recall)) if (precision and recall) else None,
            'specificity': tn / (tn + fp) if (tn + fp) else None,
        }

    # (estate, block) -> [tp, tn, fp, fn]
    per_block = {}
    for row in rows:
        key = (row['estate'] or '', row['block'] or '')
        cnt = per_block.setdefault(key, [0, 0, 0, 0])
        try:
            records    = json.loads(row['records']    or '[]')
            gt_records = json.loads(row['gt_records'] or '[]')
        except Exception:
            continue
        if not records or not gt_records:
            continue

        gt_map = {}
        for r in gt_records:
            bp = str(r.get('Baris-Pokok', '') or '').strip()
            if not bp:
                continue
            vals = []
            for c in ['M1', 'M2', 'M3']:
                try:
                    v = r.get(c)
                    if v is not None:
                        vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            if vals:
                gt_map[bp] = vals

        flipped = _optimized_decisions(records) if optimize else {}

        for r in records:
            bp = str(r.get('Baris-Pokok', '') or '').strip()
            if not bp or bp not in gt_map:
                continue
            ai_avg = _avg(r, ['F1', 'F2', 'F3'])
            if ai_avg is None:
                continue

            if count_only:
                ai = 'Spray' if ai_avg >= _COUNT_ONLY_THRESHOLD else 'No Spray'
            else:
                ai = str(r.get('Decision', '') or '').strip()
                if not ai:
                    continue
            ai = flipped.get(bp, ai)

            closest_m = min(gt_map[bp], key=lambda v: abs(v - ai_avg))
            gt = 'No Spray' if closest_m < _NO_SPRAY_THRESHOLD else 'Spray'

            if ai == 'Spray'    and gt == 'Spray':    cnt[0] += 1
            elif ai == 'No Spray' and gt == 'No Spray': cnt[1] += 1
            elif ai == 'Spray'    and gt == 'No Spray': cnt[2] += 1
            elif ai == 'No Spray' and gt == 'Spray':    cnt[3] += 1

    tp, tn, fp, fn = (sum(c[i] for c in per_block.values()) for i in range(4))
    result = _metrics(tp, tn, fp, fn)

    def _nat_key(s):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

    result['by_block'] = [
        {'estate': est, 'block': blk, **_metrics(*cnt)}
        for (est, blk), cnt in sorted(per_block.items(), key=lambda kv: (_nat_key(kv[0][0]), _nat_key(kv[0][1])))
        if sum(cnt)
    ]
    return result


def get_size_scatter(pest, user_id=None, estates=None, blocks=None, date_from=None, date_to=None):
    """Return combined size_points from matching sessions for scatter plot.

    user_id=None aggregates points across all users.
    """
    query  = f'SELECT size_data FROM census_sessions WHERE pest = ? AND {_NC_EXCLUDE}'
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    if estates:
        query += f' AND estate IN ({",".join("?" * len(estates))})'
        params.extend(estates)
    if blocks:
        query += f' AND block IN ({",".join("?" * len(blocks))})'
        params.extend(blocks)
    if date_from:
        query += ' AND date_from >= ?'; params.append(date_from)
    if date_to:
        query += ' AND date_from <= ?'; params.append(date_to)
    with _conn() as con:
        rows = con.execute(query, params).fetchall()
    points = []
    for row in rows:
        try:
            points.extend(json.loads(row[0] or '[]'))
        except Exception:
            pass
    return points


def get_trend(pest, user_id=None):
    """Return spray/no-spray counts per estate, block, month for trend chart.

    month = YYYY-MM from date_from; falls back to created_at[:7].
    user_id=None aggregates across all users.
    """
    query  = f'''
        SELECT
            COALESCE(estate, '')                                AS estate,
            COALESCE(block,  '')                                AS block,
            COALESCE(
                SUBSTR(date_from, 1, 7),
                SUBSTR(created_at, 1, 7)
            )                                                   AS month,
            SUM(COALESCE(spray_count, 0))                       AS spray_count,
            SUM(COALESCE(no_spray_count, 0))                    AS no_spray_count,
            SUM(COALESCE(size_small, 0))                        AS size_small,
            SUM(COALESCE(size_medium, 0))                       AS size_medium,
            SUM(COALESCE(size_big, 0))                          AS size_big
        FROM   census_sessions
        WHERE  pest = ? AND {_NC_EXCLUDE}
    '''
    params = [pest]
    if user_id is not None:
        query += ' AND user_id = ?'
        params.append(user_id)
    query += ' GROUP BY estate, block, month ORDER BY month ASC'
    with _conn() as con:
        rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]
