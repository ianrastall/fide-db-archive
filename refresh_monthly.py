"""Prepare verified monthly SQLite and 2500+ snapshots; publish immutable releases.

The rolling registry is accepted only when its ratings agree with all three
explicitly dated FIDE XML lists. An old rolling download cannot become next
month's data just because it was downloaded on the last day of this month.
Python standard library and the authenticated gh CLI are sufficient.
"""
import argparse
import calendar
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

REPO = 'ianrastall/fide-db-archive'
BASE = 'https://ratings.fide.com/download/'
COLUMNS = 'list_date line_no rank rank_note fide_id name fed sex title w_title o_title foa_title title_raw rating change games k birth_year birth_date birth_raw flag section'.split()
INTS = set('line_no rank fide_id rating change games k birth_year'.split())
TITLES = 'GM IM WGM FM WIM CM WFM NM WCM WNM'.split()


def target_period(now):
    if now.day == calendar.monthrange(now.year, now.month)[1]:
        return (now.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')
    if now.day <= 2:
        return now.strftime('%Y-%m')
    return None


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')


def gh(*args):
    return subprocess.check_output(['gh', *map(str, args)], text=True, encoding='utf-8')


def find_release(tag):
    # REST /releases/tags does not resolve a draft whose tag is not yet created.
    result = subprocess.run(['gh', 'release', 'view', tag, '--repo', REPO, '--json', 'databaseId,isDraft'],
                            text=True, encoding='utf-8', capture_output=True)
    if result.returncode:
        return None
    value = json.loads(result.stdout)
    return {'id': value['databaseId'], 'draft': value['isDraft']}


def players(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != 1 or not names[0].lower().endswith('.xml'):
            raise ValueError(f'Expected a single XML file: {path}')
        with archive.open(names[0]) as stream:
            events = ET.iterparse(stream, events=('start', 'end'))
            _, root = next(events)
            for event, elem in events:
                if event == 'end' and elem.tag == 'player':
                    yield {child.tag: (child.text or '').strip() for child in elem}
                    root.clear()


def integer(value):
    return int(value) if value else None


def prepare(period, output, source=None):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
        raise ValueError('Invalid period')
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    dt = datetime.strptime(period, '%Y-%m')
    suffix = calendar.month_abbr[dt.month].lower() + dt.strftime('%y') + 'frl_xml.zip'
    names = {'registry': 'players_list_xml.zip', **{kind: kind + '_' + suffix for kind in ('standard', 'rapid', 'blitz')}}
    sources = {}
    paths = {}
    for kind, name in names.items():
        dest = output / name
        if source:
            # Preserve source files, including when source/output are the same.
            import shutil
            if (source / name).resolve() != dest.resolve():
                shutil.copyfile(source / name, dest)
        else:
            request = urllib.request.Request(BASE + name, headers={'User-Agent': 'ChessNerd-FIDE-archive/1.0'})
            with urllib.request.urlopen(request, timeout=180) as response, dest.open('wb') as stream:
                import shutil
                shutil.copyfileobj(response, stream)
        with zipfile.ZipFile(dest) as archive:
            if archive.testzip() is not None:
                raise ValueError(f'Corrupt ZIP: {name}')
        paths[kind] = dest
        sources[kind] = {'url': BASE + name, 'name': name, 'bytes': dest.stat().st_size, 'sha256': sha(dest)}

    # Registry IDs must be unique. Rating maps are much smaller than XML trees.
    registry = {}
    shortlist = []
    for p in players(paths['registry']):
        pid = int(p['fideid'])
        if pid in registry or pid <= 0 or not p.get('name'):
            raise ValueError('Invalid/duplicate registry player')
        ratings = tuple(integer(p.get(key)) or 0 for key in ('rating', 'rapid_rating', 'blitz_rating'))
        if any(r < 0 or r > 4000 for r in ratings):
            raise ValueError('Invalid registry rating')
        registry[pid] = ratings
        if max(ratings) >= 2500:
            titles = [t for t in (p.get('title'), p.get('w_title')) if t in TITLES]
            shortlist.append({'id': pid, 'name': p['name'], 'country': p.get('country', ''), 'sex': p.get('sex', ''),
                              'title': min(titles, key=TITLES.index) if titles else '',
                              'std': ratings[0], 'rapid': ratings[1], 'blitz': ratings[2],
                              'born': integer(p.get('birthday')), 'active': 'i' not in p.get('flag', '').lower()})
    if not registry or not shortlist:
        raise ValueError('Empty FIDE registry or shortlist')

    db_name = f'fide-standard-{period}.sqlite'
    db_path = output / db_name
    if db_path.exists():
        db_path.unlink()  # Only the explicitly named generated file in output.
    counts = {}
    with closing(sqlite3.connect(db_path)) as db:
        db.execute('CREATE TABLE players (' + ','.join(f'{col} ' + ('INTEGER' if col in INTS else 'TEXT') for col in COLUMNS) + ')')
        db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
        for index, kind in enumerate(('standard', 'rapid', 'blitz')):
            seen = set()
            for line, p in enumerate(players(paths[kind]), 1):
                pid, rating = int(p['fideid']), int(p['rating'])
                if pid in seen or pid not in registry or registry[pid][index] != rating:
                    raise ValueError(f'{period} {kind} disagrees with rolling registry at ID {pid}')
                seen.add(pid)
                if kind == 'standard':
                    born = p.get('birthday') or None
                    row = dict(list_date=period, line_no=line, fide_id=pid, name=p['name'], fed=p.get('country') or None,
                               sex=p.get('sex') or None, rating=rating, games=integer(p.get('games')), k=integer(p.get('k')),
                               birth_raw=born, birth_year=integer(born), **{key: p.get(key) or None for key in ('title', 'w_title', 'o_title', 'foa_title', 'flag')})
                    db.execute('INSERT INTO players VALUES (' + ','.join('?' for _ in COLUMNS) + ')', [row.get(c) for c in COLUMNS])
            # Check the reverse direction too: no rated registry IDs may be absent.
            if not seen or any(values[index] > 0 and pid not in seen for pid, values in registry.items()):
                raise ValueError(f'Incomplete {kind} list for {period}')
            counts[kind] = len(seen)
        metadata = {'schema': '1', 'list_date': period, 'rows': str(counts['standard']), 'source_file': names['standard'],
                    'source_sha256': sources['standard']['sha256'], 'layout': 'fide-xml', 'converted': stamp}
        db.executemany('INSERT INTO meta VALUES (?,?)', metadata.items())
        for name, columns in [('players_id', 'fide_id'), ('players_name', 'name'), ('players_fed_rating', 'fed,rating')]:
            db.execute(f'CREATE INDEX {name} ON players ({columns})')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        if db.execute('SELECT count(*) FROM players').fetchone()[0] != counts['standard']:
            raise ValueError('SQLite count mismatch')
    zip_path = output / (db_name + '.zip')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(db_path, db_name)
    with zipfile.ZipFile(zip_path) as archive, archive.open(db_name) as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != sha(db_path):
            raise ValueError('SQLite ZIP round trip failed')
    tag = f'standard-sqlite-{period}-monthly'
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    row = {'period': period, 'rows': counts['standard'], 'databaseName': db_name, 'databaseBytes': db_path.stat().st_size,
           'databaseSha256': sha(db_path), 'name': zip_path.name, 'zipBytes': zip_path.stat().st_size, 'zipSha256': sha(zip_path),
           'url': f'https://github.com/{REPO}/releases/download/{tag}/{zip_path.name}', 'metadata': metadata,
           'release': tag, 'sourceRevision': revision, 'effectiveFrom': period + '-01T00:00:00Z'}
    shortlist.sort(key=lambda p: (-max(p['std'], p['rapid'], p['blitz']), p['name']))
    save(output / 'players.json', shortlist)
    save(output / 'players.meta.json', {'source': BASE + names['registry'], 'period': period, 'generated': stamp,
                                      'count': len(shortlist), 'min_rating': 2500, 'sources': sources})
    asset_names = [zip_path.name, 'players.json', 'players.meta.json', *names.values()]
    receipt = {'schema': 1, 'period': period, 'effectiveFrom': row['effectiveFrom'], 'repository': REPO, 'release': tag,
               'sourceRevision': revision, 'generated': stamp, 'sources': sources, 'counts': counts, 'list': row,
               'assets': [{'name': n, 'bytes': (output / n).stat().st_size, 'sha256': sha(output / n)} for n in asset_names]}
    save(output / 'monthly.json', receipt)
    return receipt


def publish(receipt, output):
    tag = receipt['release']
    release = find_release(tag)
    if release is None:
        gh('release', 'create', tag, '--repo', REPO, '--draft', '--target', receipt['sourceRevision'], '--title', f'FIDE {receipt["period"]} monthly data', '--notes', 'Standard SQLite list, 2500+ table, original FIDE downloads and verification receipt.')
        release = find_release(tag)
        if release is None:
            raise ValueError('Created draft release could not be found')
    if not release['draft']:
        raise ValueError('Published monthly releases are immutable; inspect the existing receipt instead')
    assets = [*receipt['assets'], {'name': 'monthly.json', 'bytes': (output / 'monthly.json').stat().st_size, 'sha256': sha(output / 'monthly.json')}]
    for asset in assets:
        gh('release', 'upload', tag, output / asset['name'], '--repo', REPO, '--clobber')
    uploaded = {a['name']: a for a in json.loads(gh('api', f'repos/{REPO}/releases/{release["id"]}/assets?per_page=100'))}
    for expected in assets:
        actual = uploaded[expected['name']]
        if actual['size'] != expected['bytes'] or actual.get('digest') != 'sha256:' + expected['sha256']:
            raise ValueError(f'Uploaded asset verification failed: {expected["name"]}')
    gh('release', 'edit', tag, '--repo', REPO, '--draft=false', '--latest=false')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--period')
    parser.add_argument('--source', type=Path, help='Use existing original downloads for a reproducible offline run')
    parser.add_argument('--output', type=Path, default=Path('monthly-output'))
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    period = args.period or target_period(datetime.now(timezone.utc))
    if not period:
        print('No monthly preparation or recovery scheduled today.')
        return
    # Repeated scheduled runs must not download/rewrite an already published month.
    if args.publish:
        existing = find_release(f'standard-sqlite-{period}-monthly')
        if existing and not existing['draft']:
            print(f'{period} is already published; no changes.')
            return
    receipt = prepare(period, args.output, args.source)
    if args.publish:
        publish(receipt, args.output)
    print(f'Verified {period}: {receipt["counts"]}')


if __name__ == '__main__':
    main()
