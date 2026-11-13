"""
Create re-census calendar reminders for EXISTING sessions with avg_larvae < 0.9
that do not have one yet. New census results get theirs automatically.

Reminders whose date has already passed are skipped unless --include-past.

Usage:
    python scripts/gcal_backfill.py                  # dry-run: list what would be created
    python scripts/gcal_backfill.py --apply          # create the events
    python scripts/gcal_backfill.py --apply --include-past
"""
import sys
import os
import argparse
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from core import gcal_reminder as gr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='actually create the events')
    ap.add_argument('--include-past', action='store_true', help='also create reminders dated in the past')
    args = ap.parse_args()

    sessions = db.get_sessions_for_reminder(gr.REMINDER_THRESHOLD)
    today = date.today()
    counts = {}
    for s in sessions:
        census_d = gr._census_date(s)
        if census_d is None:
            status, detail = 'no_date', None
        else:
            remind_d = gr.reminder_date(census_d)
            if remind_d < today and not args.include_past:
                status, detail = 'past', remind_d.isoformat()
            elif not args.apply:
                status, detail = 'would_create', remind_d.isoformat()
            else:
                status, detail = gr.schedule_reminder(s, db, skip_past=not args.include_past)
        counts[status] = counts.get(status, 0) + 1
        print(f"  #{s['id']:<4} {s.get('estate') or '-':6} {s.get('block') or '-':6} "
              f"census={s.get('date_from') or '-':10} avg={float(s['avg_larvae']):.3f} -> {status} {detail or ''}")

    print('\nSummary:', counts or 'nothing to do')
    if not args.apply:
        print('Dry-run only. Re-run with --apply to create the events.')


if __name__ == '__main__':
    main()
