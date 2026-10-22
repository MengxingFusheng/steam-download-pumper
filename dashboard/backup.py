"""Consistent online SQLite backup; never copy a live database without its WAL."""
import argparse
import os
import sqlite3
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('destination');args=parser.parse_args()
    destination=Path(args.destination)
    if destination.exists():raise SystemExit('destination exists; choose a new backup name')
    source=Path(os.environ.get('DASHBOARD_DB','/data/dashboard.sqlite3')).resolve()
    if destination.resolve()==source:raise SystemExit('backup destination must differ from source')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.partial')
    try:
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as reader, sqlite3.connect(temporary) as writer:
            reader.backup(writer,pages=256,sleep=.05)
            if writer.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('backup integrity check failed')
        os.chmod(temporary,0o600)
        temporary.replace(destination)
        print('Backup verified:',destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__=='__main__':main()
