import io
import os
import re
from datetime import date as _date
from flask import jsonify, send_file
import database as db


def generate_excel_report(pest, user_id, data):
    """Generate Excel spray-grid report for any pest. Returns Flask Response."""
    estates   = [e.strip() for e in data.get('estates', []) if str(e).strip()]
    blocks    = [b.strip() for b in data.get('blocks',  []) if str(b).strip()]
    date_from = data.get('date_from', '').strip()
    date_to   = data.get('date_to',   '').strip()

    all_sessions = db.get_sessions_filtered(
        pest, user_id,
        estates=estates or None,
        blocks=blocks or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )
    if not all_sessions:
        return jsonify({'error': 'No data found for the selected filters'}), 404

    def _nat_key(s):
        return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]

    sections_list = sorted(
        all_sessions,
        key=lambda s: (
            _nat_key(s.get('estate', '') or ''),
            _nat_key(s.get('block',  '') or ''),
            s.get('date_from', '') or '',
        )
    )

    # Exclude "Need to Visit" sessions from report
    sections_list = [s for s in sections_list if not s.get('not_confidence')]
    if not sections_list:
        return jsonify({'error': 'No confident data found for the selected filters'}), 404

    # Same table layout as /census/export (Baris-Pokok, F1-F3, ImagePath, D1-D3, Decision),
    # one sheet per session.
    import pandas as pd

    def _sheet_name(sess, used):
        base = '_'.join(str(sess.get(k) or '').strip() for k in ('estate', 'block', 'date_from'))
        base = re.sub(r'[\\/*?:\[\]]', '-', base).strip('_') or 'Sheet'
        base = base[:31]
        name, n = base, 1
        while name.lower() in used:
            suffix = f'_{n}'
            name = base[:31 - len(suffix)] + suffix
            n += 1
        used.add(name.lower())
        return name

    buf  = io.BytesIO()
    used = set()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        for sess in sections_list:
            records = sess.get('records') or []
            columns = sess.get('columns') or (list(records[0].keys()) if records else [])
            df = pd.DataFrame(records, columns=columns)
            df.to_excel(writer, sheet_name=_sheet_name(sess, used), index=False)
    buf.seek(0)

    fname = f"report_{_date.today().strftime('%Y%m%d')}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
