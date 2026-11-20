#!/usr/bin/env python3
"""Owner-run SQLite backup/check/disposable restore drill; stdlib only.

Never restores over a live path. Backup publication refuses any existing target.
The read-only source connection's online backup includes committed WAL content.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import tempfile


def connect_readonly(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Source must be a regular SQLite file')
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)


def validate(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('SQLite integrity check failed')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('SQLite foreign key check failed')
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    if not tables:
        raise ValueError('Database contains no application tables')
    # Table names come from schema but still require SQL identifier quoting.
    return {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for (name,) in tables}


def check(path):
    connection = connect_readonly(path)
    try:
        return validate(connection)
    finally:
        connection.close()


def backup(source, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Destination already exists; refusing overwrite')
    source_connection = connect_readonly(source)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.uniter-backup-', dir=destination.parent)
        os.close(fd)  # mkstemp creates mode 0600, independent of caller umask.
        output = sqlite3.connect(temporary)
        try:
            source_connection.backup(output, pages=256, sleep=0.1)
            validate(output)
        finally:
            output.close()
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        # Atomic, no-clobber publication on the same filesystem. Never replace().
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        source_connection.close()
        if temporary is not None:
            os.unlink(temporary)


def drill(source):
    expected = check(source)
    with tempfile.TemporaryDirectory(prefix='uniter-restore-drill-') as scratch:
        restored = Path(scratch) / 'restored.db'
        backup(source, restored)
        if check(restored) != expected:
            raise ValueError('Disposable restore schema/table counts differ')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'check', 'drill'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', nargs='?', type=Path)
    args = parser.parse_args()
    if args.action == 'backup':
        if args.destination is None:
            parser.error('backup requires a new destination path')
        backup(args.source, args.destination)
    else:
        if args.destination is not None:
            parser.error('check/drill do not accept a destination')
        (check if args.action == 'check' else drill)(args.source)
    print('SQLite ' + args.action + ': OK')


if __name__ == '__main__':
    main()
