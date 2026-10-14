#!/usr/bin/env python3
"""Read-only usage view/export. Amounts are local estimates, never official bills."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import argparse
import json
import re
from datetime import date as date_type
from pathlib import Path
import model_router
import model_call_ledger as ledger


def filtered(root, date=None, purpose=None, hour=None):
    if date: date_type.fromisoformat(date)
    if purpose and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}',purpose): raise ValueError('purpose')
    if hour and hour not in [f'{n:02}' for n in range(24)]: raise ValueError('hour')
    data=ledger.report(root,date,purpose)
    if hour:
        data['calls']=[r for r in data['calls'] if r['started_at'][11:13]==hour]
        data['by_hour_purpose']=[r for r in data.get('by_hour_purpose',[]) if r['hour'][11:13]==hour]
        data['events']=[r for r in data.get('events',[]) if r['at'][11:13]==hour]
    return data


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date', help='YYYY-MM-DD in Asia/Shanghai')
    p.add_argument('--hour', help='00 through 23')
    p.add_argument('--purpose');p.add_argument('--format',choices=['json','csv'],default='json')
    p.add_argument('--root',type=Path,default=model_router.STATE_DIR/'model-calls')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    try: data=filtered(a.root,a.date,a.purpose,a.hour)
    except ValueError: p.error('Invalid date, hour or purpose')
    text=ledger.export_csv(data) if a.format=='csv' else json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    if a.output:
        with a.output.open('x') as f:f.write(text)
        a.output.chmod(0o600)
    else:print(text,end='')

if __name__=='__main__':main()
