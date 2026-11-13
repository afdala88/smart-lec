"""
One-off migration: recompute 'Decision' (Spray/No Spray) for every existing
bagworm census session, using the updated calculate_decision() logic
(N_avg threshold changed from >=2 to >=1).

Usage:
    python scripts/recompute_decision.py           # dry-run, prints a summary
    python scripts/recompute_decision.py --apply    # actually writes to the DB
"""
import sys
import os
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from database import _conn
from core.estate import calculate_decision, _get_tahun_tanam

PEST_KEY = 'bagworm'


def main(apply: bool):
    with _conn() as con:
        rows = con.execute(
            "SELECT id, estate, block, records FROM census_sessions WHERE pest = ?",
            (PEST_KEY,)
        ).fetchall()

        changed = skipped_no_year = unchanged = 0

        for row in rows:
            records = json.loads(row['records'] or '[]')
            if not records:
                continue

            tahun_tanam = _get_tahun_tanam(row['estate'] or '', row['block'] or '')
            if not tahun_tanam:
                skipped_no_year += 1
                continue

            df = pd.DataFrame(records)
            df = calculate_decision(df, tahun_tanam)
            new_records = df.to_dict(orient='records')

            old_decisions = [r.get('Decision') for r in records]
            new_decisions = [r.get('Decision') for r in new_records]

            if old_decisions == new_decisions:
                unchanged += 1
                continue

            changed += 1
            spray_count    = sum(1 for r in new_records if r.get('Decision') == 'Spray')
            no_spray_count = sum(1 for r in new_records if r.get('Decision') == 'No Spray')

            print(f"[session {row['id']}] estate={row['estate']} block={row['block']} "
                  f"tahun_tanam={tahun_tanam}  decisions: {old_decisions} -> {new_decisions}")

            if apply:
                con.execute(
                    "UPDATE census_sessions SET records = ?, spray_count = ?, no_spray_count = ? WHERE id = ?",
                    (json.dumps(new_records), spray_count, no_spray_count, row['id'])
                )

        if apply:
            con.commit()

        print(f"\nTotal sessions: {len(rows)}")
        print(f"Changed:        {changed}")
        print(f"Unchanged:      {unchanged}")
        print(f"Skipped (no tahun_tanam match in data_estate.xlsx): {skipped_no_year}")
        if not apply:
            print("\nDry-run only — nothing was written. Re-run with --apply to persist.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='write changes to the database')
    args = parser.parse_args()
    main(apply=args.apply)
