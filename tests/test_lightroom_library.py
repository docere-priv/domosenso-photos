"""Library read, coverage and skip rest: a fake client only, no network, no Home Assistant import."""
import asyncio
import importlib
import sys
import types
from pathlib import Path

pkg = types.ModuleType('lightroom_library_pkg')
pkg.__path__ = [str(Path(__file__).parents[1] / 'custom_components/domosenso_photos')]
sys.modules[pkg.__name__] = pkg
lib = importlib.import_module('lightroom_library_pkg.lightroom_library')

DAY = 86400


def asset(aid, **payload):
    return {'id': aid, 'subtype': 'image', 'payload': payload}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.album_list = [
            {'id': 'set1', 'subtype': 'collection_set', 'payload': {'name': 'Trips'}},
            {'id': 'alb1', 'subtype': 'collection', 'payload': {'name': 'Lake', 'parent': {'id': 'set1'}}},
            {'id': 'alb2', 'subtype': 'collection', 'payload': {'name': 'Garden'}},
        ]
        self.members = {'alb1': ['a1', 'a2'], 'alb2': ['a2']}
        self.asset_list = [
            asset('a1', captureDate='2025-06-01T10:00:00', ratings={'x': {'rating': 4, 'date': '2025'}},
                  xmp={'tiff': {'Make': 'Camco'}, 'aux': {'Lens': ''}}),
            asset('a2', captureDate='2025-06-02T10:00:00', develop={'userUpdated': '2025-06-03T00:00:00Z'},
                  reviews={'x': {'flag': 'pick', 'date': '2025'}}, xmp={'aux': {'Lens': '35 mm'}}),
            asset('a3'),
        ]

    async def catalog_id(self):
        self.calls.append('catalog')
        return 'cat1'

    async def albums(self, catalog):
        self.calls.append(('albums', catalog))
        return self.album_list

    async def album_asset_ids(self, catalog, album_id):
        self.calls.append(('members', album_id))
        return self.members[album_id]

    async def assets(self, catalog):
        self.calls.append(('assets', catalog))
        return self.asset_list


def test_read_library_joins_memberships_and_skips_sets():
    client = FakeClient()
    library = asyncio.run(lib.read_library(client))
    assert library.catalog == 'cat1' and library.asset_count == 3
    by_id = {p.asset_id: p for p in library.photos}
    assert by_id['a1'].album_ids == ('alb1',)
    assert set(by_id['a2'].album_ids) == {'alb1', 'alb2'}
    assert by_id['a3'].album_ids == ()
    assert ('members', 'set1') not in client.calls
    assert set(library.albums) == {'set1', 'alb1', 'alb2'} and library.albums['set1'].is_set


def test_coverage_counts_present_fields_only():
    library = asyncio.run(lib.read_library(FakeClient()))
    assert library.coverage == {
        'payload.ratings': 1, 'payload.reviews': 1, 'payload.captureDate': 2,
        'payload.develop.userUpdated': 1, 'payload.xmp.tiff.Make': 1, 'payload.xmp.aux.Lens': 1}


def test_cache_holds_trimmed_photos_and_albums():
    library = asyncio.run(lib.read_library(FakeClient()))
    cached = lib.cache(library, 1000)
    assert cached['read_at'] == 1000 and cached['asset_count'] == 3
    assert {p['asset_id'] for p in cached['photos']} == {'a1', 'a2', 'a3'}
    assert {'id': 'alb1', 'name': 'Lake', 'parent_id': 'set1', 'is_set': False} in cached['albums']
    assert 'payload' not in str(cached)


def _photos():
    return asyncio.run(lib.read_library(FakeClient())).photos


def test_resting_skips_within_a_day_while_unchanged():
    photos = _photos()
    changed = {p.asset_id: p.changed for p in photos}
    skipped = {'a1': changed['a1'], 'a2': 'older edit'}
    assert lib.resting(skipped, 1000, photos, 1000 + DAY - 1) == {'a1'}
    assert lib.resting(skipped, 1000, photos, 1000 + DAY) == set()
    assert lib.resting(skipped, None, photos, 1000) == set()
    assert lib.resting({}, 1000, photos, 1000) == set()


def test_next_skips_keeps_first_try_time_for_carried_skips():
    photos = _photos()
    changed = {p.asset_id: p.changed for p in photos}
    assert lib.next_skips([], photos, True, 500, 1000) == ({}, None)
    assert lib.next_skips(['a1'], photos, True, 500, 1000) == ({'a1': changed['a1']}, 1000)
    assert lib.next_skips(['a1'], photos, False, 500, 1000) == ({'a1': changed['a1']}, 500)
    assert lib.next_skips(['a1'], photos, False, None, 1000) == ({'a1': changed['a1']}, 1000)
