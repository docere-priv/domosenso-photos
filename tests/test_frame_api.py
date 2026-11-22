import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

pkg = types.ModuleType('photos_facade_pkg')
pkg.__path__ = [str(Path(__file__).parents[1] / 'custom_components/domosenso_photos')]
sys.modules[pkg.__name__] = pkg
frame_api = importlib.import_module('photos_facade_pkg.frame_api')
album_plan = importlib.import_module('photos_facade_pkg.album_plan')
album_share = importlib.import_module('photos_facade_pkg.album_share')
AlbumError = album_plan.AlbumError

URL = 'https://photos.example.org/mo/sharing/Zq7Lm3Xp9'
PASSWORD = 'pw-Velvet-Otter-42'
LOGIN_OK = (200, 'application/json', json.dumps({'success': True, 'data': {'sharing_sid': 'S'}}).encode())
ALBUM_OK = (200, 'application/json', json.dumps({'success': True, 'data': {'list': [
    {'id': 8, 'name': 'Lake trip', 'item_count': 3, 'start_time': 1, 'version': 2}]}}).encode())
WRONG_PASSWORD = (200, 'application/json', json.dumps({'success': False, 'error': {'code': 1001}}).encode())


class FakeTransport:
    def __init__(self, *replies, delay=0):
        self.replies, self.delay, self.calls = list(replies), delay, []

    async def __call__(self, method, url, params=None, data=None, headers=None):
        self.calls.append((method, url, data))
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeManager:
    def __init__(self, transport=None):
        self.albums, self.host_map = {}, []
        self._transport = transport
        self.added, self.removed, self.synced = [], [], []
        self.remove_result = None

    def public_list(self):
        return [{'id': rid, 'name': rec.get('name'), 'status': rec.get('status')} for rid, rec in self.albums.items()]

    async def add_background(self, url, password, path=None):
        rec = {'id': 'abc123', 'url': url, 'password': password, 'name': None}
        self.added.append((url, password))
        self.albums['abc123'] = rec
        return rec

    async def remove(self, album_id):
        self.removed.append(album_id)
        return self.remove_result

    async def _sync_one(self, album_id, confirm=False):
        self.synced.append(album_id)


def run(coro):
    return asyncio.run(coro)


def check_error(manager, url=URL, password=PASSWORD):
    with pytest.raises(AlbumError) as info:
        run(frame_api.FrameApi(manager).check(url, password))
    return info.value


def test_albums_returns_public_list():
    m = FakeManager()
    m.albums['a1'] = {'id': 'a1', 'name': 'Lake trip', 'status': 'ok', 'url': URL, 'password': PASSWORD}
    result = frame_api.FrameApi(m).albums()
    assert result == m.public_list()
    assert all('url' not in item and 'password' not in item for item in result)
    assert frame_api.FrameApi.version == 1


def test_check_ok_returns_name():
    t = FakeTransport(LOGIN_OK, ALBUM_OK)
    assert run(frame_api.FrameApi(FakeManager(t)).check(URL, PASSWORD)) == 'Lake trip'
    assert PASSWORD not in ''.join(str(c[1]) for c in t.calls)  # never in a URL


def test_check_wrong_password():
    assert check_error(FakeManager(FakeTransport(WRONG_PASSWORD))).code == 'wrong_password'


def test_check_invalid_link():
    assert check_error(FakeManager(), 'https://photos.example.org/nope').code == 'invalid_link'


def test_check_already_added():
    m = FakeManager(FakeTransport())
    m.albums['a1'] = {'id': 'a1', 'url': URL + '?x=1'}
    assert check_error(m).code == 'already_added'


def test_check_timeout(monkeypatch):
    monkeypatch.setattr(frame_api, 'CHECK_TIMEOUT_S', 0.05)
    assert check_error(FakeManager(FakeTransport(LOGIN_OK, ALBUM_OK, delay=1))).code == 'unreachable'


def test_check_unreachable_and_share_error():
    assert check_error(FakeManager(FakeTransport(OSError('boom')))).code == 'unreachable'
    other = (200, 'application/json', json.dumps({'success': False, 'error': {'code': 105}}).encode())
    assert check_error(FakeManager(FakeTransport(other))).code == 'unreachable'


def test_password_not_in_errors(monkeypatch):
    monkeypatch.setattr(frame_api, 'CHECK_TIMEOUT_S', 0.05)
    dup = FakeManager(FakeTransport())
    dup.albums['a1'] = {'id': 'a1', 'url': URL}
    errors = [check_error(FakeManager(FakeTransport(WRONG_PASSWORD))),
              check_error(FakeManager(), 'https://photos.example.org/nope'),
              check_error(dup),
              check_error(FakeManager(FakeTransport(OSError(PASSWORD)))),
              check_error(FakeManager(FakeTransport(LOGIN_OK, ALBUM_OK, delay=1)))]
    assert len(errors) == 5
    for err in errors:
        assert PASSWORD not in str(err) and PASSWORD not in repr(err)
        assert err.__cause__ is None or PASSWORD not in repr(err.__cause__)


def test_add_returns_without_waiting():
    m = FakeManager()  # its add_background only registers the first sync; it never runs here
    result = run(frame_api.FrameApi(m).add(URL, PASSWORD))
    assert result == {'id': 'abc123', 'name': None}
    assert m.added == [(URL, PASSWORD)]
    assert m.synced == []
    assert PASSWORD not in repr(result)


def test_add_maps_manager_errors():
    class Dup(FakeManager):
        async def add_background(self, url, password, path=None):
            raise AlbumError('already_added')
    with pytest.raises(AlbumError) as info:
        run(frame_api.FrameApi(Dup()).add(URL, None))
    assert info.value.code == 'already_added'


def test_remove_busy_and_unknown():
    m = FakeManager()
    api = frame_api.FrameApi(m)
    m.albums['a1'] = {'id': 'a1'}
    m.remove_result = {'id': 'a1', 'status': 'syncing'}
    assert run(api.remove('a1')) == 'busy'
    m.remove_result = None
    assert run(api.remove('a1')) == 'removed'
    with pytest.raises(AlbumError) as info:
        run(api.remove('nope'))
    assert info.value.code == 'unknown_album'
    assert m.removed == ['a1', 'a1']


def test_sync_starts_in_background_and_unknown():
    async def go():
        m = FakeManager()
        m.albums['a1'] = {'id': 'a1'}
        started = asyncio.Event()

        async def slow(album_id, confirm=False):
            started.set()
            await asyncio.sleep(5)
        m._sync_one = slow
        api = frame_api.FrameApi(m)
        await asyncio.wait_for(api.sync('a1'), 1)  # returns while the sync is still running
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(AlbumError) as info:
            await api.sync('nope')
        return info.value.code
    assert run(go()) == 'unknown_album'
