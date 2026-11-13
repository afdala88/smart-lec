"""Google Calendar re-census reminder for low-infestation census results.

Rule:
  * A census session with avg_larvae < REMINDER_THRESHOLD gets a calendar event.
  * Event date = actual census date (date_from) + REMINDER_DAYS_AFTER days.
    If that lands on Saturday, Sunday or a public holiday ("tanggal merah"),
    it is pushed forward to the next working day (Mon-Fri, not a holiday).
  * The event carries a reminder 1 day before (H-1), as popup and email.
    If H-1 is a Saturday, Sunday or holiday, the reminder fires on the previous working day.

Setup (one time):
  1. Google Cloud Console -> enable "Google Calendar API" -> create an OAuth
     client of type "Desktop app" -> download it as  gcal_credentials.json
     into the app folder (next to app.py).
  2. Run  python scripts/gcal_auth.py  and sign in with the Google account
     whose calendar should receive the reminders. This writes gcal_token.json.
  Without the token the app keeps working; reminders are just skipped (logged).

Extra holidays (cuti bersama, estate holidays) go in holidays_extra.txt,
one YYYY-MM-DD per line ('#' starts a comment).
"""
import os
from datetime import date, datetime, timedelta

from core.pests import BASE_DIR

REMINDER_THRESHOLD   = 0.9
REMINDER_DAYS_AFTER  = 31
EVENT_HOUR           = 8          # 08:00 local time on the reminder day
EVENT_DURATION_MIN   = 30
TIMEZONE             = 'Asia/Jakarta'

SCOPES           = ['https://www.googleapis.com/auth/calendar.events']
CREDENTIALS_PATH = os.path.join(BASE_DIR, 'gcal_credentials.json')
TOKEN_PATH       = os.path.join(BASE_DIR, 'gcal_token.json')
EXTRA_HOLIDAYS   = os.path.join(BASE_DIR, 'holidays_extra.txt')
# "Sensus Bagworm" calendar on project.kita.f1@gmail.com (override with env GCAL_CALENDAR_ID)
CALENDAR_ID      = os.environ.get(
    'GCAL_CALENDAR_ID',
    'd69cb1273afc8ad6a7141461f58c9c0de88a621a583d729f1f56d00b6c0bd2fe@group.calendar.google.com',
)


# ── Date rule ────────────────────────────────────────────────────────────────

def _extra_holidays():
    days = set()
    if os.path.isfile(EXTRA_HOLIDAYS):
        with open(EXTRA_HOLIDAYS, encoding='utf-8') as f:
            for line in f:
                line = line.split('#', 1)[0].strip()
                if not line:
                    continue
                try:
                    days.add(date.fromisoformat(line))
                except ValueError:
                    print(f'[GCAL] ignoring invalid date in holidays_extra.txt: {line!r}')
    return days


def _is_holiday(d, _cache={}):
    if d.year not in _cache:
        try:
            import holidays
            _cache[d.year] = set(holidays.Indonesia(years=d.year).keys())
        except Exception as e:
            print(f'[GCAL] holidays library unavailable ({e}); only weekends/extra file used')
            _cache[d.year] = set()
    return d in _cache[d.year] or d in _extra_holidays()


def reminder_date(census_date):
    """census_date + 31 days, pushed forward past weekends and holidays."""
    d = census_date + timedelta(days=REMINDER_DAYS_AFTER)
    while d.weekday() >= 5 or _is_holiday(d):
        d += timedelta(days=1)
    return d


def notify_minutes_before(event_d):
    """H-1 notification lead time; if H-1 is Sat/Sun or a holiday, move it back to the previous working day."""
    notify_d = event_d - timedelta(days=1)
    while notify_d.weekday() >= 5 or _is_holiday(notify_d):
        notify_d -= timedelta(days=1)
    return (event_d - notify_d).days * 24 * 60


def _reminders(event_d):
    minutes = notify_minutes_before(event_d)
    return {
        'useDefault': False,
        'overrides': [
            {'method': 'popup', 'minutes': minutes},
            {'method': 'email', 'minutes': minutes},
        ],
    }


def _census_date(sess):
    """Actual census date from the form (date_from, else date_to) — never created_at."""
    for key in ('date_from', 'date_to'):
        v = (sess.get(key) or '').strip()
        if v:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                pass
    return None


# ── Google Calendar ──────────────────────────────────────────────────────────

def _service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not os.path.isfile(TOKEN_PATH):
        raise RuntimeError('gcal_token.json not found - run scripts/gcal_auth.py once')
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError('Google token invalid - run scripts/gcal_auth.py again')
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def authorize_interactive():
    """Open a browser for Google sign-in and save gcal_token.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not os.path.isfile(CREDENTIALS_PATH):
        raise RuntimeError(f'{CREDENTIALS_PATH} not found (OAuth Desktop client from Google Cloud Console)')
    flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
        f.write(creds.to_json())
    return TOKEN_PATH


def _event_body(sess, census_d, remind_d):
    start = datetime(remind_d.year, remind_d.month, remind_d.day, EVENT_HOUR, 0)
    end   = start + timedelta(minutes=EVENT_DURATION_MIN)
    loc   = ' / '.join(x for x in (sess.get('estate'), sess.get('divisi'), sess.get('block')) if x)
    return {
        'summary': f"Sensus ulang bagworm - {sess.get('estate') or '-'} {sess.get('block') or '-'}",
        'location': loc,
        'description': (
            f"Pengingat sensus ulang (avg larva < {REMINDER_THRESHOLD}).\n"
            f"Estate: {sess.get('estate') or '-'}\n"
            f"Blok: {sess.get('block') or '-'}\n"
            f"Tanggal sensus: {census_d.isoformat()}\n"
            f"Rata-rata larva: {round(float(sess.get('avg_larvae') or 0), 3)}\n"
            f"Jadwal: +{REMINDER_DAYS_AFTER} hari, digeser ke hari kerja berikutnya bila libur.\n"
            f"Census session id: {sess.get('id')}"
        ),
        'start': {'dateTime': start.isoformat(), 'timeZone': TIMEZONE},
        'end':   {'dateTime': end.isoformat(),   'timeZone': TIMEZONE},
        'reminders': _reminders(remind_d),
    }


def schedule_reminder(sess, db, skip_past=False):
    """Create the calendar event for one session if it qualifies.

    Returns (status, detail): status in
    'created' | 'exists' | 'not_needed' | 'no_date' | 'past' | 'error'.
    """
    try:
        avg = sess.get('avg_larvae')
        if avg is None or float(avg) >= REMINDER_THRESHOLD:
            return 'not_needed', None
        if sess.get('gcal_event_id'):
            return 'exists', sess['gcal_event_id']
        census_d = _census_date(sess)
        if census_d is None:
            return 'no_date', None
        remind_d = reminder_date(census_d)
        if skip_past and remind_d < date.today():
            return 'past', remind_d.isoformat()

        event = _service().events().insert(
            calendarId=CALENDAR_ID, body=_event_body(sess, census_d, remind_d)
        ).execute()
        db.set_gcal_event_id(sess['id'], event['id'])
        return 'created', remind_d.isoformat()
    except Exception as e:
        return 'error', str(e)


def schedule_for_session(session_id, db):
    """Hook called after a census is saved. Never raises."""
    sess = db.get_session_brief(session_id)
    if not sess:
        return
    status, detail = schedule_reminder(sess, db)
    if status != 'not_needed':
        print(f'[GCAL] session {session_id} ({sess.get("estate")}/{sess.get("block")}): {status} {detail or ""}')


def delete_event(event_id):
    """Remove a reminder event (used when its census session is deleted). Never raises."""
    if not event_id:
        return
    try:
        _service().events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        print(f'[GCAL] could not delete event {event_id}: {e}')
