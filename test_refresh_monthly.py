from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
import zipfile
from unittest.mock import patch, Mock

from refresh_monthly import prepare, COLUMNS, find_release, refresh


class MonthlyTests(unittest.TestCase):
    def test_draft_without_a_published_tag_is_resolved_by_gh(self):
        with patch('refresh_monthly.subprocess.run', return_value=Mock(returncode=0, stdout='{"databaseId":123,"isDraft":true}')):
            self.assertEqual(find_release('standard-sqlite-2026-10-monthly'), {'id': 123, 'draft': True})

    def test_unposted_dated_list_fails_without_publishing(self):
        not_found = urllib.error.HTTPError('https://ratings.fide.com/download/standard_nov26frl_xml.zip', 404, 'Not Found', None, None)
        with patch('refresh_monthly.find_release', return_value=None), patch('refresh_monthly.prepare', side_effect=not_found), patch('refresh_monthly.publish') as publish:
            with self.assertRaisesRegex(ValueError, 'not posted'):
                refresh('2026-11', Path('unused'), None, True)
            publish.assert_not_called()

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
