from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch, Mock

from refresh_monthly import prepare, target_period, COLUMNS, find_release


class MonthlyTests(unittest.TestCase):
    def test_draft_without_a_published_tag_is_resolved_by_gh(self):
        with patch('refresh_monthly.subprocess.run', return_value=Mock(returncode=0, stdout='{"databaseId":123,"isDraft":true}')):
            self.assertEqual(find_release('standard-sqlite-2026-10-monthly'), {'id': 123, 'draft': True})

    def test_calendar(self):
        for date, expected in [('2026-10-30', None), ('2026-10-31', '2026-11'), ('2026-12-31', '2027-01'),
                               ('2028-02-28', None), ('2028-02-29', '2028-03'), ('2026-11-01', '2026-11')]:
            self.assertEqual(target_period(datetime.fromisoformat(date).replace(tzinfo=timezone.utc)), expected)

    def fixture(self, source, mismatch=False, duplicate=False):
        for kind in ('standard', 'rapid', 'blitz', 'registry'):
            rating = 2599 if mismatch and kind == 'standard' else 2600
            player = f'<player><fideid>123</fideid><name>Test, Player</name><country>USA</country><rating>{rating}</rating><rapid_rating>2600</rapid_rating><blitz_rating>2600</blitz_rating><birthday></birthday><games>0</games><title>GM</title></player>'
            name = 'players_list_xml.zip' if kind == 'registry' else kind + '_oct26frl_xml.zip'
            with zipfile.ZipFile(source / name, 'w') as archive:
                archive.writestr('list.xml', '<players>' + player * (2 if duplicate and kind == 'registry' else 1) + '</players>')

    def test_preserves_schema_missing_values_and_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            self.fixture(source)
            result = prepare('2026-10', source / 'out', source)
            with closing(sqlite3.connect(source / 'out' / result['list']['databaseName'])) as db:
                self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(players)')], COLUMNS)
                self.assertEqual(db.execute('SELECT birth_year,rank,games,rating FROM players').fetchone(), (None, None, 0, 2600))
                self.assertEqual(dict(db.execute('SELECT * FROM meta'))['rows'], '1')
            self.assertEqual(result['effectiveFrom'], '2026-10-01T00:00:00Z')
            self.assertEqual(json.loads((source / 'out/players.json').read_text())[0]['std'], 2600)

    def test_rejects_old_registry_and_duplicate_ids(self):
        for options in ({'mismatch': True}, {'duplicate': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                source = Path(temp)
                self.fixture(source, **options)
                with self.assertRaises(ValueError):
                    prepare('2026-10', source / 'out', source)
                self.assertFalse((source / 'out/monthly.json').exists())


if __name__ == '__main__':
    unittest.main()
