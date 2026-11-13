"""Single background worker that runs census jobs one at a time.

Jobs live in the census_jobs table (database.py), so status survives page
reloads and is shared by every request thread. Only one job runs at a time:
the YOLO pipeline is CPU/RAM heavy and the server is sized for that.

Run the web app with ONE process (gunicorn --workers 1 --threads N): the
worker thread and the stop events below live in that process.
"""
import threading
import time
import traceback

import database as db
from core import uploads

_POLL_SECONDS    = 3
_CLEANUP_SECONDS = 3600

_started      = False
_start_lock   = threading.Lock()
_wake         = threading.Event()
_stop_events  = {}            # job_id -> threading.Event for the running job
_stop_lock    = threading.Lock()
_runner       = None          # callable(job: dict, stop_event) -> session_id


class JobCancelled(Exception):
    pass


def start(runner):
    """Start the worker once per process. runner(job, stop_event) returns the new session id."""
    global _started, _runner
    with _start_lock:
        if _started:
            return
        _runner  = runner
        _started = True
        db.fail_interrupted_census_jobs()
        threading.Thread(target=_loop, name='census-worker', daemon=True).start()


def notify():
    """Wake the worker right away (called after a job is enqueued)."""
    _wake.set()


def request_stop(job_id):
    """Cancel a job: queued -> cancelled immediately; running -> stop at the next stage."""
    if db.cancel_queued_census_job(job_id):
        return True
    with _stop_lock:
        ev = _stop_events.get(job_id)
    if ev:
        ev.set()
        return True
    return False


def _loop():
    last_cleanup = 0.0
    while True:
        try:
            if time.time() - last_cleanup > _CLEANUP_SECONDS:
                last_cleanup = time.time()
                removed = uploads.cleanup(db.upload_ids_in_use())
                if removed:
                    print(f'[QUEUE] removed {removed} old upload folder(s)')

            job = db.claim_next_census_job()
            if job is None:
                _wake.wait(_POLL_SECONDS)
                _wake.clear()
                continue
            _run_one(job)
        except Exception:
            print('[QUEUE] worker error', traceback.format_exc())
            time.sleep(_POLL_SECONDS)


def _run_one(job):
    job_id = job['id']
    stop_ev = threading.Event()
    with _stop_lock:
        _stop_events[job_id] = stop_ev
    print(f'[QUEUE] start job {job_id}')
    try:
        session_id = _runner(job, stop_ev)
        if stop_ev.is_set():
            raise JobCancelled()
        db.finish_census_job(job_id, 'done', session_id=session_id)
        print(f'[QUEUE] done job {job_id} -> session {session_id}')
    except JobCancelled:
        db.finish_census_job(job_id, 'cancelled', error='Dibatalkan oleh user')
        print(f'[QUEUE] cancelled job {job_id}')
    except Exception as exc:
        print('[CENSUS JOB ERROR]', traceback.format_exc())
        db.finish_census_job(job_id, 'error', error=str(exc))
    finally:
        with _stop_lock:
            _stop_events.pop(job_id, None)
