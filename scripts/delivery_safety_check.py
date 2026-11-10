#!/usr/bin/env python3
"""V8 P0-01：发包安全检查。用标准库 sqlite3；失败不得兜底为 0。"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
fail = 0

print(f'[check] root={ROOT}')

for f in ROOT.rglob('*'):
    if not f.is_file():
        continue
    name = f.name.lower()
    if name.endswith(('.bak', '.v6bak')):
        print(f'FAIL: backup file present: {f}')
        fail = 1
    suspicious = (
        name.endswith('.pem')
        or name == '.env'
        or ('secret' in name)
        or ('private' in name and 'key' in name)
    )
    if suspicious:
        if 'node_modules' in f.parts or '.venv' in f.parts or '__pycache__' in f.parts:
            continue
        print(f'FAIL: suspicious credential-like file: {f}')
        fail = 1

for db in ROOT.rglob('projects.sqlite'):
    try:
        sz = db.stat().st_size
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            cur = con.cursor()
            try:
                n = cur.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
            except sqlite3.Error as e:
                print(f'FAIL: cannot read projects table in {db}: {e}')
                fail = 1
                n = None
            try:
                v = cur.execute('SELECT COUNT(*) FROM versions').fetchone()[0]
            except sqlite3.Error as e:
                print(f'FAIL: cannot read versions table in {db}: {e}')
                fail = 1
                v = None
        finally:
            con.close()
        print(f'[sqlite] {db} size={sz} projects={n} versions={v}')
        if n is not None and n != 0:
            print(f'FAIL: delivery DB must be empty/example-only (projects={n})')
            fail = 1
        if v is not None and v != 0:
            print(f'FAIL: delivery DB versions not empty (versions={v})')
            fail = 1
    except sqlite3.Error as e:
        print(f'FAIL: sqlite open error for {db}: {e}')
        fail = 1
    except OSError as e:
        print(f'FAIL: cannot access {db}: {e}')
        fail = 1

uploads = ROOT / 'backend' / 'data' / 'uploads'
if uploads.is_dir():
    files = [p for p in uploads.rglob('*') if p.is_file()]
    if files:
        print(f'FAIL: uploads/ not empty ({len(files)} files)')
        fail = 1

if fail:
    print('DELIVERY SAFETY CHECK FAILED')
    sys.exit(1)
print('DELIVERY SAFETY CHECK OK')
sys.exit(0)
