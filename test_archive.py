from contextlib import closing
import argparse
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import pyarrow.parquet as pq

import archive as a


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.output = self.root / 'data'
        self.source.mkdir()
        self.output.mkdir()

    def source_db(self, period):
        path = self.source / f'fide-standard-{period}.sqlite'
        row = [None] * len(a.NAMES)
        for n, v in dict(list_date=period, line_no=7, fide_id=9_000_000_000,
                         name='Zoë, 王\n"Name"', fed='', rating=2800, change=-23,
                         rank_note=r'\N', section='a\\b', birth_year=1900).items():
            row[a.NAMES.index(n)] = v
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE players (' + ','.join(n + (' INTEGER' if n in a.INTS else ' TEXT') for n in a.NAMES) + ')')
            db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('CREATE INDEX players_name ON players(name)')
            db.executemany('INSERT INTO players VALUES (' + ','.join('?' for _ in row) + ')', [row, row])
            db.executemany('INSERT INTO meta VALUES (?,?)', [('schema','1'),('rows','2'),('list_date',period),('extra',None)])
            db.execute('PRAGMA user_version=9')
        return path

    def make_bin(self):
        paths = [self.source_db('2025-01'), self.source_db('2025-02')]
        a.build_one(('2025', list(map(str, paths)), str(self.output), 2026))
        return self.output / 'fide-2025.bin', paths

    def test_lossless_and_skip(self):
        binary, paths = self.make_bin()
        self.assertEqual(a.verify(binary, self.source), 4)
        self.assertEqual(len(a.read_manifest(binary)['lists']), 2)
        before = binary.stat().st_mtime_ns
        a.build_one(('2025', list(map(str, paths)), str(self.output), 2026))
        self.assertEqual(binary.stat().st_mtime_ns, before)

    def test_exports_and_source_restoration(self):
        binary, paths = self.make_bin()
        for fmt in ('sqlite','csv','jsonl'):
            output = self.root / ('export.' + fmt)
            args = argparse.Namespace(archive=binary, output=output, format=fmt, source_db=None)
            a.export(args)
            with self.assertRaises(FileExistsError):
                a.export(args)
        with closing(sqlite3.connect(self.root / 'export.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM players').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT count(*) FROM list_metadata').fetchone()[0], 8)
        rows = [json.loads(line) for line in (self.root / 'export.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIsNone(rows[0]['rank'])
        self.assertEqual(rows[0]['fed'], '')
        with (self.root / 'export.csv').open(encoding='utf-8', newline='') as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row['rank'], r'\N')
        self.assertEqual(row['rank_note'], r'\\N')
        restored = self.root / 'restored.sqlite'
        a.export(argparse.Namespace(archive=binary, output=restored, format='sqlite', source_db=paths[0].name))
        with closing(sqlite3.connect(paths[0])) as original, closing(sqlite3.connect(restored)) as copy:
            for query in ['SELECT * FROM players ORDER BY rowid', 'SELECT * FROM meta ORDER BY key',
                          'SELECT type,name,sql FROM sqlite_master ORDER BY type,name', 'PRAGMA user_version']:
                self.assertEqual(original.execute(query).fetchall(), copy.execute(query).fetchall())

    def test_monthly_current_year(self):
        self.source_db('2026-01')
        self.source_db('2026-02')
        a.build(argparse.Namespace(source=self.source, output=self.output, year=None, current_year=2026, jobs=1))
        self.assertEqual(sorted(p.name for p in self.output.glob('*.bin')), ['fide-2026-01.bin','fide-2026-02.bin'])
        self.assertTrue(a.read_manifest(self.output / 'fide-2026-01.bin')['provisional'])

    def test_reject_bad_source_preserves_existing(self):
        binary, paths = self.make_bin()
        sha = a.digest(binary)
        with closing(sqlite3.connect(paths[0])) as db, db:
            db.execute("UPDATE players SET list_date='WRONG'")
        with self.assertRaises(ValueError):
            a.build_one(('2025', list(map(str, paths)), str(self.output), 2026))
        self.assertEqual(a.digest(binary), sha)

    def test_source_hash_detects_changed_input(self):
        binary, paths = self.make_bin()
        with closing(sqlite3.connect(paths[0])) as db, db:
            db.execute('UPDATE players SET rating=1')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            a.verify(binary, self.source)

    def test_checksum_detects_corrupt_page(self):
        binary, _ = self.make_bin()
        with pq.ParquetFile(binary) as pf:
            col = pf.metadata.row_group(0).column(0)
            start = col.dictionary_page_offset or col.data_page_offset
            position = start + col.total_compressed_size - 1
        data = bytearray(binary.read_bytes())
        data[position] ^= 1
        binary.write_bytes(data)
        with self.assertRaisesRegex(OSError, 'checksum verification failed'):
            a.verify(binary)

    def test_reject_dynamic_sqlite_type_instead_of_truncating(self):
        path = self.source_db('2025-01')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE players SET rating=1.5')
        with self.assertRaisesRegex(ValueError, 'value type'):
            a.build_one(('2025', [str(path)], str(self.output), 2026))


if __name__ == '__main__':
    unittest.main()
