"""Lossless FIDE archive builder and exporter. See FORMAT.md for the .bin format."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent
VERSION = 1
LIMIT = 100_000_000  # Deliberately stricter than GitHub's 100 MiB limit.
BATCH = 65_536
PATTERN = re.compile(r"fide-(standard|rapid|blitz)-(\d{4})(?:-(\d{2}))?\.sqlite\Z")
INTS = {'line_no', 'rank', 'fide_id', 'rating', 'change', 'games', 'k', 'birth_year'}
NAMES = ('list_date line_no rank rank_note fide_id name fed sex title w_title '
         'o_title foa_title title_raw rating change games k birth_year birth_date '
         'birth_raw flag section').split()
SCHEMA = pa.schema([(n, pa.int64() if n in INTS else pa.string()) for n in NAMES]
                   + [('rating_type', pa.string()), ('source_db', pa.string())])
SELECT = 'SELECT ' + ', '.join('"' + n + '"' for n in NAMES) + ' FROM players ORDER BY rowid'


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def connect(path):
    path = Path(path).resolve()
    if any(Path(str(path) + s).exists() for s in ('-wal', '-shm', '-journal')):
        raise ValueError(f'Active SQLite sidecar: {path}')
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)


@contextmanager
def build_lock(output):
    with (output / '.build.lock').open('a+b') as f:
        if f.seek(0, 2) == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def inspect_source(path):
    match = PATTERN.fullmatch(path.name)
    if not match:
        raise ValueError(f'Unexpected filename: {path.name}')
    rating_type, year, month = match.groups()
    period = year + ('-' + month if month else '')
    if month and not 1 <= int(month) <= 12:
        raise ValueError(f'Invalid month: {path.name}')
    before = path.stat()
    sha = digest(path)
    with closing(connect(path)) as db:
        columns = [(r[1], r[2].upper()) for r in db.execute('PRAGMA table_info(players)')]
        expected = [(n, 'INTEGER' if n in INTS else 'TEXT') for n in NAMES]
        if columns != expected:
            raise ValueError(f'Unexpected player schema: {path.name}')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'players', 'meta'}:
            raise ValueError(f'Unexpected tables (would lose data): {path.name}: {tables}')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError(f'SQLite integrity failure: {path.name}')
        meta_rows = [list(r) for r in db.execute('SELECT key, value FROM meta ORDER BY key')]
        meta = dict(meta_rows)
        count, wrong = db.execute('SELECT count(*), coalesce(sum(list_date IS NOT ?),0) FROM players', (period,)).fetchone()
        if meta.get('schema') != '1' or meta.get('list_date') != period or int(meta['rows']) != count or wrong:
            raise ValueError(f'Metadata/date/count mismatch: {path.name}')
        sql = [list(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
        user_version = db.execute('PRAGMA user_version').fetchone()[0]
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'Source changed during inspection: {path}')
    return dict(source_db=path.name, rating_type=rating_type, list_date=period,
                rows=count, bytes=before.st_size, sha256=sha, meta=meta_rows,
                sqlite_schema=sql, sqlite_user_version=user_version)


def tables(path, info):
    with closing(connect(path)) as db:
        cur = db.execute(SELECT)
        while rows := cur.fetchmany(BATCH):
            arrays = []
            for n, col in zip(NAMES, zip(*rows)):
                array = pa.array(col)
                expected = SCHEMA.field(n).type
                if array.type != expected and not pa.types.is_null(array.type):
                    raise ValueError(f'Unexpected SQLite value type for {n} in {path}: {array.type}')
                arrays.append(array.cast(expected))
            arrays += [pa.array([info['rating_type']] * len(rows)), pa.array([info['source_db']] * len(rows))]
            yield pa.Table.from_arrays(arrays, schema=SCHEMA)


def read_manifest(path):
    with pq.ParquetFile(path, page_checksum_verification=True) as pf:
        raw = pf.metadata.metadata or {}
        manifest = json.loads(raw[b'fide.archive'])
        if manifest['format_version'] != VERSION or not pf.schema_arrow.remove_metadata().equals(SCHEMA):
            raise ValueError(f'Unsupported archive format: {path}')
        return manifest


def verify(path, source=None):
    manifest = read_manifest(path)
    with pq.ParquetFile(path, page_checksum_verification=True) as pf:
        data = pf.read().replace_schema_metadata(None)
    total = 0
    for info in manifest['lists']:
        if info['row_start'] != total:
            raise ValueError('Unexpected source offset')
        section = data.slice(total, info['rows'])
        if section.num_rows != info['rows']:
            raise ValueError('Row count mismatch')
        if section.num_rows:
            for col, value in [('source_db', info['source_db']), ('rating_type', info['rating_type']), ('list_date', info['list_date'])]:
                if section[col].unique().to_pylist() != [value]:
                    raise ValueError(f'Provenance mismatch: {path}')
        if source is not None:
            original = Path(source) / info['source_db']
            if digest(original) != info['sha256']:
                raise ValueError(f'Source hash mismatch: {original}')
            offset = 0
            for table in tables(original, info):
                if not section.slice(offset, table.num_rows).equals(table):
                    raise ValueError(f'Row comparison failed: {info["source_db"]}')
                offset += table.num_rows
            if offset != info['rows'] or digest(original) != info['sha256']:
                raise ValueError('Source changed during verification')
        total += info['rows']
    if total != data.num_rows:
        raise ValueError('Unexpected archive rows')
    return total


def build_one(task):
    label, paths, output, current_year = task
    output = Path(output)
    target = output / f'fide-{label}.bin'
    infos = [inspect_source(Path(p)) for p in paths]
    manifest = dict(format_version=VERSION, period=label,
                    provisional=int(label[:4]) == current_year, lists=infos)
    # Sources and encoding are unchanged: do not rewrite an already verified archive.
    if target.exists():
        old = read_manifest(target)
        base_old = [{k: v for k, v in i.items() if k != 'row_start'} for i in old['lists']]
        if base_old == infos and old['provisional'] == manifest['provisional']:
            verify(target)
            print(f'{target.name}: unchanged', flush=True)
            return summary(target, old)
    temporary = target.with_suffix('.bin.tmp')
    try:
        chunks = []
        offset = 0
        for path, info in zip(paths, infos):
            info['row_start'] = offset
            count = 0
            for table in tables(path, info):
                chunks.append(table)
                count += table.num_rows
            if count != info['rows'] or digest(path) != info['sha256']:
                raise ValueError(f'Source changed during build: {path}')
            offset += count
        data = pa.concat_tables(chunks) if chunks else pa.Table.from_batches([], schema=SCHEMA)
        metadata = {'fide.archive': json.dumps(manifest, ensure_ascii=False, sort_keys=True)}
        data = data.replace_schema_metadata(metadata)
        pq.write_table(data, temporary, compression='zstd', compression_level=19,
                       use_dictionary=True, write_page_checksum=True,
                       data_page_version='2.0', row_group_size=64 * 1024 * 1024,
                       dictionary_pagesize_limit=64 * 1024 * 1024,
                       data_page_size=64 * 1024 * 1024)
        del data, chunks
        if temporary.stat().st_size >= LIMIT:
            raise ValueError(f'{target.name} exceeds the conservative 100 MB limit')
        verify(temporary, Path(paths[0]).parent)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    result = summary(target, manifest)
    print(f'{target.name}: {result["rows"]:,} rows, {result["bytes"] / 1_000_000:.2f} MB; verified against SQLite', flush=True)
    return result


def summary(path, manifest):
    return dict(file=path.name, period=manifest['period'], provisional=manifest['provisional'],
                bytes=path.stat().st_size, sha256=digest(path),
                source_bytes=sum(i['bytes'] for i in manifest['lists']),
                rows=sum(i['rows'] for i in manifest['lists']), lists=manifest['lists'])


def build(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    grouped = {}
    for path in sorted(args.source.resolve().glob('*.sqlite')):
        match = PATTERN.fullmatch(path.name)
        if not match:
            raise ValueError(f'Unrecognized SQLite input: {path}')
        year = int(match[2])
        if args.year and year not in args.year:
            continue
        if not 1967 <= year <= args.current_year:
            raise ValueError(f'Year outside archive scope: {path}')
        if year == args.current_year and not match[3]:
            raise ValueError('Current-year lists must have a month')
        label = match[2] + ('-' + match[3] if year == args.current_year else '')
        grouped.setdefault(label, []).append(str(path))
    if not grouped:
        raise ValueError('No matching source databases')
    tasks = [(label, paths, str(output), args.current_year) for label, paths in sorted(grouped.items())]
    with build_lock(output):
        if args.jobs == 1:
            results = list(map(build_one, tasks))
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                results = list(pool.map(build_one, tasks))
        # Include existing periods when doing a partial update; verify their schema first.
        built = {r['file']: r for r in results}
        for path in sorted(output.glob('fide-*.bin')):
            if path.name not in built:
                built[path.name] = summary(path, read_manifest(path))
        results = sorted(built.values(), key=lambda r: r['period'])
        report = dict(format_version=VERSION, current_year=args.current_year,
                      missing_completed_years=sorted(set(range(1967, args.current_year)) -
                          {int(r['period']) for r in results if len(r['period']) == 4}),
                      files=results)
        temp = output / 'inventory.json.tmp'
        temp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        temp.replace(output / 'inventory.json')


def selected_batches(path, source_db=None):
    manifest = read_manifest(path)
    with pq.ParquetFile(path, page_checksum_verification=True) as pf:
        data = pf.read().replace_schema_metadata(None)
    for info in manifest['lists']:
        if source_db is None or info['source_db'] == source_db:
            section = data.slice(info['row_start'], info['rows'])
            for offset in range(0, section.num_rows, BATCH):
                yield section.slice(offset, BATCH)


def export(args):
    manifest = read_manifest(args.archive)
    if args.source_db and args.source_db not in {i['source_db'] for i in manifest['lists']}:
        raise ValueError(f'Unknown source database: {args.source_db}')
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f'Output exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.tmp')
    if temp.exists():
        raise FileExistsError(f'Temporary output exists: {temp}')
    try:
        if args.format == 'sqlite':
            export_sqlite(args, manifest, temp)
        else:
            with temp.open('x', encoding='utf-8', newline='') as f:
                writer = csv.writer(f) if args.format == 'csv' else None
                if writer:
                    writer.writerow(SCHEMA.names)
                for table in selected_batches(args.archive, args.source_db):
                    if writer:
                        # Documented reversible convention: NULL -> \N; escape all backslashes.
                        writer.writerows([r'\N' if v is None else str(v).replace('\\', '\\\\') for v in row]
                                         for row in zip(*(col.to_pylist() for col in table.columns)))
                    else:
                        for row in table.to_pylist():
                            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    print(f'Exported {output}', flush=True)


def export_sqlite(args, manifest, temp):
    infos = [i for i in manifest['lists'] if args.source_db is None or i['source_db'] == args.source_db]
    with closing(sqlite3.connect(temp)) as db:
        db.execute('PRAGMA journal_mode=OFF')
        if args.source_db:
            info = infos[0]
            for kind, name, sql in info['sqlite_schema']:
                if kind == 'table':
                    db.execute(sql)
            db.executemany('INSERT INTO meta VALUES (?,?)', info['meta'])
            names = NAMES
        else:
            names = SCHEMA.names
            db.execute('CREATE TABLE players (' + ','.join('"' + n + '" ' + ('INTEGER' if n in INTS else 'TEXT') for n in names) + ')')
            db.execute('CREATE TABLE rating_lists (source_db TEXT PRIMARY KEY, rating_type TEXT, list_date TEXT, rows INTEGER, sha256 TEXT)')
            db.execute('CREATE TABLE list_metadata (source_db TEXT, key TEXT, value TEXT)')
            db.execute('CREATE TABLE archive_metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT INTO archive_metadata VALUES (?, ?)', ('fide.archive', json.dumps(manifest, ensure_ascii=False)))
            for info in infos:
                db.execute('INSERT INTO rating_lists VALUES (?,?,?,?,?)', tuple(info[k] for k in ['source_db','rating_type','list_date','rows','sha256']))
                db.executemany('INSERT INTO list_metadata VALUES (?,?,?)', [(info['source_db'], k, v) for k, v in info['meta']])
        insert = 'INSERT INTO players VALUES (' + ','.join('?' for _ in names) + ')'
        for table in selected_batches(args.archive, args.source_db):
            db.executemany(insert, zip(*(table[n].to_pylist() for n in names)))
        if args.source_db:
            for kind, name, sql in infos[0]['sqlite_schema']:
                if kind != 'table':
                    db.execute(sql)
            db.execute(f'PRAGMA user_version={int(infos[0]["sqlite_user_version"])}')
        else:
            db.execute('CREATE INDEX players_id ON players(fide_id, list_date)')
            db.execute('CREATE INDEX players_list ON players(list_date, rating_type)')
        db.commit()
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('Exported SQLite failed integrity check')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    b = commands.add_parser('build', help='Build annual and current-year monthly .bin files')
    b.add_argument('--source', type=Path, default=Path(r'D:\fide-archive\sqlite'))
    b.add_argument('--output', type=Path, default=ROOT / 'data')
    b.add_argument('--year', type=int, action='append', help='Build only this year (repeatable)')
    b.add_argument('--current-year', type=int, default=datetime.now().year)
    b.add_argument('--jobs', type=int, choices=range(1, 9), default=2)
    b.set_defaults(run=build)
    i = commands.add_parser('info', help='Print all source metadata stored inside a binary')
    i.add_argument('archive', type=Path)
    i.set_defaults(run=lambda a: print(json.dumps(read_manifest(a.archive), indent=2, ensure_ascii=False)))
    v = commands.add_parser('verify', help='Read every data page; optionally compare every row with SQLite')
    v.add_argument('archive', type=Path)
    v.add_argument('--source', type=Path)
    v.set_defaults(run=lambda a: print(f'Verified {verify(a.archive, a.source):,} rows'))
    e = commands.add_parser('export', help='Export SQLite, CSV, or JSONL without overwriting existing files')
    e.add_argument('archive', type=Path)
    e.add_argument('--format', choices=['sqlite', 'csv', 'jsonl'], default='sqlite')
    e.add_argument('--output', type=Path, required=True)
    e.add_argument('--source-db', help='Export only one original list; SQLite restores its original schema')
    e.set_defaults(run=export)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ValueError, OSError, KeyError, sqlite3.Error, pa.ArrowException) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
