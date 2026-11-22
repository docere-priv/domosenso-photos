"""Lightroom plan and execution on owned folders (no Home Assistant import)."""
import asyncio
import importlib
import io
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

pkg = types.ModuleType('lightroom_sync_pkg')
pkg.__path__ = [str(Path(__file__).parents[1] / 'custom_components/domosenso_photos')]
sys.modules[pkg.__name__] = pkg
lrs = importlib.import_module('lightroom_sync_pkg.lightroom_sync')
catalog = importlib.import_module('lightroom_sync_pkg.lightroom_catalog')
lrf = importlib.import_module('lightroom_sync_pkg.lightroom_filter')
layout = importlib.import_module('lightroom_sync_pkg.album_layout')
files = importlib.import_module('lightroom_sync_pkg.album_files')

OWNER = 'lightroom:acct-0001'
NOW = 50_000
_META = catalog.CameraMeta(None, None, None, None, None, None, None, None)


def _jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (16, 12), (200, 80, 40)).save(buf, format='JPEG')
    return buf.getvalue()


JPEG = _jpeg()


def photo(n, changed='c1', captured=None):
    return catalog.Photo(f'as{n:03}', 4, None, captured or datetime(2025, 6, 1 + n % 28, 10, 0), changed,
                         False, (), _META)


def placed(n, key='alb1', folder='Trip', **kw):
    return lrf.Placed(photo(n, **kw), key, folder)


def owned(key, path, *ns, changed='c1'):
    items = {f'as{n:03}': {'name': lrs.file_name(placed(n)), 'changed': changed} for n in ns}
    return lrs.OwnedFolder(key, path, items)


def plan(placed_list, folders, listed=None, confirm=False):
    by_key = {f.key: f for f in folders}
    return lrs.plan(placed_list, by_key, len(placed_list) if listed is None else listed, confirm)


async def _direct(fn):
    return fn()


async def _fetch(p):
    return JPEG


def execute(pl, photo_dir, fetch=_fetch, free_floor=0):
    return asyncio.run(lrs.execute(pl, photo_dir, OWNER, fetch, _direct, free_floor, timezone.utc, NOW))


def run(photo_dir, placed_list, prefix='', listed=None, confirm=False, **kw):
    folders = lrs.read_owned(photo_dir, prefix, OWNER, {p.key: p.folder for p in placed_list})
    pl = lrs.plan(placed_list, folders, len(placed_list) if listed is None else listed, confirm)
    return pl, execute(pl, photo_dir, **kw)


def marker(folder):
    return json.loads((folder / '.sync.json').read_text())


def photos(folder):
    return sorted(p.name for p in folder.iterdir() if p.name != '.sync.json')


def ids(items):
    return sorted(p.photo.asset_id for p in items)


# --- plan -------------------------------------------------------------------------------------

def test_file_name():
    assert lrs.file_name(placed(3)) == '2025-06-04_as003.jpg'


def test_plan_add_update_move_remove():
    folders = [owned('alb1', 'Trip', 1, 2, 3), owned('alb2', 'Beach', 4)]
    pl = plan([placed(1), placed(2, changed='c2'), placed(4), placed(5)], folders)
    assert ids(pl.add) == ['as005']
    assert ids(pl.update) == ['as002']
    assert [(p.photo.asset_id, old) for p, old in pl.move] == [('as004', 'alb2')]
    assert pl.remove == [('alb1', 'as003')]
    assert pl.rename == [] and pl.hold is None


def _many(n):
    return [owned('alb1', 'Trip', *range(n))]


def test_plan_mass_remove_holds():
    keep = [placed(n) for n in range(9)]
    pl = plan(keep, _many(20))
    assert pl.hold == 'mass_remove' and pl.remove == [] and pl.move == []


def test_plan_mass_remove_under_20_does_not_hold():
    pl = plan([placed(0)], _many(19))
    assert pl.hold is None and len(pl.remove) == 18


def test_plan_exactly_half_removed_does_not_hold():
    pl = plan([placed(n) for n in range(10)], _many(20))
    assert pl.hold is None and len(pl.remove) == 10


def test_plan_confirm_releases_hold():
    pl = plan([placed(0)], _many(20), confirm=True)
    assert pl.hold is None and len(pl.remove) == 19


def test_plan_mass_unfile_holds():
    unfiled = [placed(n, key='2025-06', folder='2025-06') for n in range(20)]
    pl = plan(unfiled, _many(20))
    assert pl.hold == 'mass_unfile' and pl.move == [] and pl.remove == [] and pl.add == []
    released = plan(unfiled, _many(20), confirm=True)
    assert released.hold is None and len(released.move) == 20


def test_plan_moves_between_albums_do_not_unfile_hold():
    pl = plan([placed(n, key='alb2', folder='Beach') for n in range(20)], _many(20))
    assert pl.hold is None and len(pl.move) == 20


def test_plan_empty_library_holds():
    pl = plan([], [owned('alb1', 'Trip', 1)], listed=0)
    assert pl.hold == 'empty_library' and pl.remove == []
    assert plan([], [], listed=0).hold is None


def test_plan_hold_keeps_adds_and_updates():
    keep = [placed(n) for n in range(5)] + [placed(5, changed='c2'), placed(30)]
    pl = plan(keep, _many(20))
    assert pl.hold == 'mass_remove'
    assert ids(pl.add) == ['as030'] and ids(pl.update) == ['as005']
    assert pl.remove == [] and pl.move == []


def test_plan_asset_in_two_owned_folders_removes_stale_copy():
    folders = [owned('alb1', 'Trip', 1), owned('alb2', 'Beach', 1)]
    pl = plan([placed(1)], folders)
    assert pl.remove == [('alb2', 'as001')] and pl.move == [] and pl.add == [] and pl.hold is None
    # stale copies do not count towards a hold: 20 files owned, 20 stale copies removed
    big = [owned('alb1', 'Trip', *range(20)), owned('alb2', 'Beach', *range(20))]
    pl = plan([placed(n) for n in range(20)], big)
    assert pl.hold is None and len(pl.remove) == 20


def test_read_owned_same_key_copy_is_left_alone(tmp_path):
    run(tmp_path, [placed(1), placed(2)])
    # the user copies the folder, marker included
    (tmp_path / 'Copy').mkdir()
    for f in (tmp_path / 'Trip').iterdir():
        (tmp_path / 'Copy' / f.name).write_bytes(f.read_bytes())
    before = {f.name: f.read_bytes() for f in (tmp_path / 'Copy').iterdir()}
    folders = lrs.read_owned(tmp_path, '', OWNER, {'alb1': 'Trip'})
    assert folders == {'alb1': lrs.OwnedFolder('alb1', 'Trip', marker(tmp_path / 'Trip')['items'])}
    # a move out of the album, a removal, an add and an update: the copy is never touched
    run(tmp_path, [placed(1, key='alb2', folder='Beach'), placed(3)])
    run(tmp_path, [placed(3, changed='c2')])
    assert photos(tmp_path / 'Trip') == ['2025-06-04_as003.jpg']
    assert {f.name: f.read_bytes() for f in (tmp_path / 'Copy').iterdir()} == before


def test_read_owned_same_key_rename_keeps_most_recent_marker(tmp_path):
    run(tmp_path, [placed(1), placed(2)])
    (tmp_path / 'Copy').mkdir()
    for f in (tmp_path / 'Trip').iterdir():
        (tmp_path / 'Copy' / f.name).write_bytes(f.read_bytes())
    m = marker(tmp_path / 'Copy')
    m['last_sync'] = NOW - 100  # the copy's marker stops being rewritten once it is made
    (tmp_path / 'Copy/.sync.json').write_text(json.dumps(m))
    before = {f.name: f.read_bytes() for f in (tmp_path / 'Copy').iterdir()}
    pl, _ = run(tmp_path, [placed(1, folder='Coast')])  # album renamed; 'Copy' sorts before 'Trip'
    assert pl.rename == [('alb1', 'Trip', 'Coast')]
    assert photos(tmp_path / 'Coast') == ['2025-06-02_as001.jpg'] and not (tmp_path / 'Trip').exists()
    assert {f.name: f.read_bytes() for f in (tmp_path / 'Copy').iterdir()} == before


def test_read_owned_same_key_keeps_the_placed_one(tmp_path):
    for name in ('A', 'B'):
        (tmp_path / name).mkdir()
        (tmp_path / name / '.sync.json').write_text(json.dumps({'owner': OWNER, 'key': 'alb1', 'items': {}}))
    assert lrs.read_owned(tmp_path, '', OWNER)['alb1'].path == 'A'
    assert lrs.read_owned(tmp_path, '', OWNER, {'alb1': 'B'}) == {'alb1': lrs.OwnedFolder('alb1', 'B', {})}


def test_plan_album_rename_renames_folder():
    pl = plan([placed(1, folder='Lr/Coast')], [owned('alb1', 'Lr/Trip', 1)])
    assert pl.rename == [('alb1', 'Lr/Trip', 'Lr/Coast')]
    assert pl.add == pl.update == pl.move == pl.remove == []


def test_plan_suffixed_folder_is_not_renamed():
    assert plan([placed(1)], [owned('alb1', 'Trip-2', 1)]).rename == []


# --- read_owned / execute ---------------------------------------------------------------------

def test_execute_writes_file_and_marker(tmp_path):
    p = placed(1, folder='Lr/Trip')
    pl, res = run(tmp_path, [p], prefix='Lr')
    folder = tmp_path / 'Lr/Trip'
    assert photos(folder) == ['2025-06-02_as001.jpg']
    assert (folder / '2025-06-02_as001.jpg').read_bytes() == JPEG
    assert os.stat(folder / '2025-06-02_as001.jpg').st_mtime == datetime(2025, 6, 2, 10, tzinfo=timezone.utc).timestamp()
    assert marker(folder) == {'owner': OWNER, 'kind': 'lightroom', 'key': 'alb1',
                              'items': {'as001': {'name': '2025-06-02_as001.jpg', 'changed': 'c1'}},
                              'last_sync': NOW}
    assert (res.added, res.updated, res.moved, res.removed, res.skipped) == (1, 0, 0, 0, [])
    owned_now = lrs.read_owned(tmp_path, 'Lr', OWNER)
    assert owned_now == {'alb1': lrs.OwnedFolder('alb1', 'Lr/Trip', marker(folder)['items'])}
    pl, res = run(tmp_path, [placed(1, folder='Lr/Trip', changed='c2')], prefix='Lr')
    assert (res.added, res.updated) == (0, 1)
    assert marker(folder)['items']['as001']['changed'] == 'c2'


def test_execute_move_writes_new_before_removing_old(tmp_path, monkeypatch):
    run(tmp_path, [placed(1), placed(2)])
    old = tmp_path / 'Trip/2025-06-02_as001.jpg'
    seen = []
    real = files.write_photo

    def spy(folder, name, data, mtime):
        seen.append((folder.name, old.exists()))
        real(folder, name, data, mtime)

    monkeypatch.setattr(lrs, 'write_photo', spy)
    _, res = run(tmp_path, [placed(1, key='alb2', folder='Beach'), placed(2)])
    assert seen == [('Beach', True)]
    assert res.moved == 1
    assert photos(tmp_path / 'Beach') == ['2025-06-02_as001.jpg']
    assert photos(tmp_path / 'Trip') == ['2025-06-03_as002.jpg']
    assert list(marker(tmp_path / 'Trip')['items']) == ['as002']


def test_execute_empty_folder_removed(tmp_path):
    run(tmp_path, [placed(1), placed(2, key='alb2', folder='Beach')])
    (tmp_path / 'Beach/mine.txt').write_text('x')
    _, res = run(tmp_path, [placed(1, key='alb3', folder='Hills')])
    assert res.removed == 1 and res.moved == 1
    assert not (tmp_path / 'Trip').exists()
    assert sorted(os.listdir(tmp_path / 'Beach')) == ['.sync.json', 'mine.txt']
    assert marker(tmp_path / 'Beach')['items'] == {}


def test_execute_skipped_fetch_counts(tmp_path):
    async def fetch(p):
        return None if p.photo.asset_id == 'as002' else JPEG

    _, res = run(tmp_path, [placed(1), placed(2)], fetch=fetch)
    assert (res.added, res.skipped) == (1, ['as002'])
    assert list(marker(tmp_path / 'Trip')['items']) == ['as001']


def test_execute_skipped_move_keeps_old_copy(tmp_path):
    run(tmp_path, [placed(1)])

    async def fetch(p):
        return None

    _, res = run(tmp_path, [placed(1, key='alb2', folder='Beach')], fetch=fetch)
    assert res.moved == 0 and res.skipped == ['as001']
    assert photos(tmp_path / 'Trip') == ['2025-06-02_as001.jpg']
    assert not (tmp_path / 'Beach').exists()


def test_execute_disk_floor_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(lrs, 'free_bytes', lambda path: 10)
    with pytest.raises(lrs.DiskFull):
        run(tmp_path, [placed(1)], free_floor=11)
    assert list(tmp_path.iterdir()) == []


def test_execute_photo_dir_missing_touches_nothing(tmp_path):
    gone = tmp_path / 'unmounted'
    pl = plan([placed(1, folder='Lr/Coast')], [owned('alb1', 'Lr/Trip', 2)])
    calls = []

    async def fetch(p):
        calls.append(p)
        return JPEG

    with pytest.raises(layout.PhotoDirMissing):
        asyncio.run(lrs.execute(pl, gone, OWNER, fetch, _direct, 0, timezone.utc, NOW))
    assert not gone.exists() and calls == []


def test_execute_never_touches_unowned_folder(tmp_path):
    run(tmp_path, [placed(1), placed(2)])
    # the user replaces the folder's marker by hand: the folder is no longer ours
    foreign = tmp_path / 'Trip'
    stale = lrs.read_owned(tmp_path, '', OWNER)
    (foreign / '.sync.json').write_text(json.dumps({'owner': 'someone-else', 'key': 'alb1', 'items': {}}))
    pl = lrs.plan([placed(3, key='alb2', folder='Beach'), placed(1, key='alb2', folder='Beach')], stale, 2, True)
    pl = lrs.LrPlan(pl.add, pl.update, pl.move, pl.remove + [('alb1', 'as002')],
                    [('alb1', 'Trip', 'Renamed')], None, pl.owned)
    execute(pl, tmp_path)
    assert photos(foreign) == ['2025-06-02_as001.jpg', '2025-06-03_as002.jpg']
    assert not (tmp_path / 'Renamed').exists()
    # a Synology folder and a hand folder next to the prefix are not read as ours
    (tmp_path / 'syn').mkdir()
    (tmp_path / 'syn/.sync.json').write_text(json.dumps({'album_id': 'a1', 'items': {'as009': 'x.jpg'}}))
    assert set(lrs.read_owned(tmp_path, '', OWNER)) == {'alb2'}


def test_execute_rename_moves_folder_and_keeps_files(tmp_path):
    run(tmp_path, [placed(1, folder='Lr/Trip')], prefix='Lr')
    pl, res = run(tmp_path, [placed(1, folder='Lr/Coast'), placed(2, folder='Lr/Coast')], prefix='Lr')
    assert pl.rename == [('alb1', 'Lr/Trip', 'Lr/Coast')]
    assert not (tmp_path / 'Lr/Trip').exists()
    assert photos(tmp_path / 'Lr/Coast') == ['2025-06-02_as001.jpg', '2025-06-03_as002.jpg']
    assert res.added == 1


def test_execute_rename_never_overwrites(tmp_path):
    run(tmp_path, [placed(1)])
    (tmp_path / 'Coast').mkdir()
    (tmp_path / 'Coast/mine.jpg').write_bytes(b'x')
    run(tmp_path, [placed(1, folder='Coast')])
    assert os.listdir(tmp_path / 'Coast') == ['mine.jpg']
    assert photos(tmp_path / 'Coast-2') == ['2025-06-02_as001.jpg']


def test_two_albums_same_clean_name_stable_suffix(tmp_path):
    both = [placed(1, key='alb1', folder='Trip'), placed(2, key='alb2', folder='Trip')]
    run(tmp_path, both)
    first = {f.key: f.path for f in lrs.read_owned(tmp_path, '', OWNER).values()}
    pl, _ = run(tmp_path, list(reversed(both)))
    second = {f.key: f.path for f in lrs.read_owned(tmp_path, '', OWNER).values()}
    assert first == second == {'alb1': 'Trip', 'alb2': 'Trip-2'}
    assert pl.rename == [] and pl.add == [] and pl.move == []


def test_execute_stale_copy_removed_after_kept(tmp_path):
    run(tmp_path, [placed(1), placed(2, key='alb2', folder='Beach')])
    # damaged state: a copy of as001 also listed in Beach
    beach = tmp_path / 'Beach'
    m = marker(beach)
    m['items']['as001'] = {'name': '2025-06-02_as001.jpg', 'changed': 'c1'}
    (beach / '.sync.json').write_text(json.dumps(m))
    (beach / '2025-06-02_as001.jpg').write_bytes(JPEG)
    pl, res = run(tmp_path, [placed(1), placed(2, key='alb2', folder='Beach')])
    assert pl.remove == [('alb2', 'as001')]
    assert photos(beach) == ['2025-06-03_as002.jpg']
    assert photos(tmp_path / 'Trip') == ['2025-06-02_as001.jpg']
