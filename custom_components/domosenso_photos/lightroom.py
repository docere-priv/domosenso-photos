"""Lightroom config entry: OAuth2 session, library read, timer and the sync of one Adobe account."""
from __future__ import annotations

import asyncio
import errno
import logging
import time
from datetime import timedelta
from pathlib import Path

import aiohttp
from PIL import Image, UnidentifiedImageError
from homeassistant.core import callback
from homeassistant.exceptions import (ConfigEntryNotReady, OAuth2TokenRequestError,
                                      OAuth2TokenRequestReauthError)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import album_image, lightroom_api, lightroom_library
from .album_layout import PhotoDirMissing
from .lightroom_api import (AuthRefused, LightroomClient, LightroomError, RenditionUnavailable, Unreachable,
                            account_id, signin_expires)
from .lightroom_exif import exif_adder
from .lightroom_filter import Filter, Placed, filter_from_options, filter_to_options, place
from .lightroom_sync import DiskFull, execute, plan, read_owned
from .transport import make_transport

_LOGGER = logging.getLogger(__name__)
KIND = 'lightroom'
DEFAULT_PHOTO_DIR = '/config/www/ramka'
DEFAULT_OPTIONS = {**filter_to_options(Filter()), 'interval_min': 360, 'max_edge': 2560, 'quality': 85,
                   'free_floor': 1_000_000_000}
STARTUP_DELAY_S = 60
UNLOAD_WAIT_S = 30
_BAD_IMAGE = (UnidentifiedImageError, Image.DecompressionBombError, album_image.HeifUnavailable, OSError, ValueError)
_BAD_RESPONSE = (LightroomError, KeyError, IndexError, AttributeError, TypeError, ValueError)


def store_key(entry_id: str) -> str:
    return f'domosenso_photos.lightroom.{entry_id}'


class _Failed(Exception):
    def __init__(self, status: str, error: str | None = None):
        super().__init__(status)
        self.status, self.error = status, error


class LightroomManager:
    def __init__(self, hass, entry, session: config_entry_oauth2_flow.OAuth2Session):
        self.hass, self.entry, self.oauth = hass, entry, session
        self.photo_dir = Path(entry.data['photo_dir'])
        self.prefix = entry.data.get('prefix') or ''
        self.options_seen = dict(entry.options)  # a reload follows only an options change, not a token refresh
        opts = {**DEFAULT_OPTIONS, **entry.options}
        self.filter = filter_from_options(opts)
        self.interval = int(opts['interval_min'])
        self.max_edge, self.quality = int(opts['max_edge']), int(opts['quality'])
        self.free_floor = int(opts['free_floor'])
        # Folders belong to the Adobe account, so a removed and re-added entry takes them back.
        self.owner = f'lightroom:{entry.unique_id or account_id(entry.data["token"])}'
        self.store = Store(hass, 1, store_key(entry.entry_id))
        self.data: dict = {}
        self.lock = asyncio.Lock()
        # Own session, not the albums one; cookies are not needed by Lightroom.
        self.http = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        self.client = LightroomClient(make_transport(self.http, with_headers=True),
                                      session.implementation.client_id, self._token,
                                      base=lightroom_api.API_BASE)  # read here so a smoke can point it at a fake
        self.closing = False
        self._unsubs: list = []
        self._task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()

    async def _token(self) -> str:
        await self.oauth.async_ensure_token_valid()
        return self.oauth.token['access_token']

    async def async_load(self) -> None:
        self.data = await self.store.async_load() or {}

    def start(self) -> None:
        self._unsubs = [
            async_call_later(self.hass, STARTUP_DELAY_S, self._tick),
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=self.interval),
                                      name='domosenso_photos lightroom', cancel_on_shutdown=True)]

    async def async_close(self) -> None:
        """Stop timers, cancel a running sync and wait for it (bounded); nothing is saved after."""
        self.closing = True
        for unsub in self._unsubs:
            unsub()
        tasks = [t for t in (self._task, *self._running) if t and not t.done()]
        for task in tasks:
            task.cancel()
        try:
            async with asyncio.timeout(UNLOAD_WAIT_S):
                await asyncio.gather(*tasks, return_exceptions=True)
                while self.lock.locked():
                    await asyncio.sleep(0.2)
        except TimeoutError:
            _LOGGER.warning('Lightroom sync still running after %s s', UNLOAD_WAIT_S)

    def public(self) -> dict:
        d, counts = self.data, self.data.get('counts') or {}
        expires = signin_expires(self.oauth.token)
        return {'entry_id': self.entry.entry_id,
                'status': 'syncing' if self.lock.locked() else d.get('status'), 'error': d.get('error'),
                'hold': d.get('hold'), 'photos': d.get('photos', 0), 'folders': d.get('folders') or [],
                'last_sync': d.get('last_sync'), 'last_success': d.get('last_success'),
                **{k: counts.get(k, 0) for k in ('added', 'updated', 'moved', 'removed')},
                'skipped_count': len(d.get('skipped') or {}),
                'signin_expires': expires.isoformat() if expires else None,
                'cache_date': (d.get('catalog') or {}).get('read_at')}

    async def sync(self, confirm: bool = False) -> dict:
        """One sync at a time; a call while one runs returns the state shown as syncing."""
        if self.closing:
            return self.public()
        if self.lock.locked():
            return {**self.public(), 'status': 'syncing'}
        task = self.hass.async_create_task(self._sync_locked(confirm), 'domosenso_photos lightroom sync')
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return await task

    def _run_blocking(self, fn):
        return self.hass.async_add_executor_job(fn)

    async def _sync_locked(self, confirm: bool) -> dict:
        if self.lock.locked():
            return {**self.public(), 'status': 'syncing'}
        async with self.lock:
            # Only a hold that exists now is confirmed; a hold this run creates stays.
            confirm = confirm and self.data.get('status') == 'awaiting_confirmation'
            now = int(time.time())
            try:
                update = await self._sync_body(confirm, now)
            except _Failed as err:
                update = {'status': err.status, 'error': err.error}
            except PhotoDirMissing:
                update = {'status': 'error', 'error': 'photo_dir_missing'}
            except (OAuth2TokenRequestReauthError, AuthRefused):
                self.entry.async_start_reauth(self.hass)
                update = {'status': 'error', 'error': 'auth_failed'}
            except (OAuth2TokenRequestError, aiohttp.ClientError, TimeoutError):
                update = {'status': 'unreachable', 'error': 'signin_unreachable'}
            except Unreachable as err:
                update = {'status': 'unreachable', 'error': err.code}
            except DiskFull:
                update = {'status': 'disk_full', 'error': None}
            except OSError as err:
                update = ({'status': 'disk_full', 'error': None} if err.errno == errno.ENOSPC
                          else {'status': 'error', 'error': 'write_failed'})
            except _BAD_RESPONSE:
                update = {'status': 'error', 'error': 'unexpected_response'}
            except Exception as err:  # noqa: BLE001 - a bug must not stop the timer
                _LOGGER.error('Lightroom sync failed unexpectedly: %s', type(err).__name__)
                update = {'status': 'error', 'error': 'unexpected'}
            self.data.update(update, last_sync=now)
            if update['status'] in ('ok', 'awaiting_confirmation'):
                self.data['last_success'] = now
            await self._save()
        return self.public()

    async def _sync_body(self, confirm: bool, now: int) -> dict:
        if not await self._run_blocking(self.photo_dir.is_dir):  # unmounted storage: touch nothing
            raise PhotoDirMissing
        await self.oauth.async_ensure_token_valid()
        # Nothing in the photo folder is written until the whole library has been read.
        library = await lightroom_library.read_library(self.client)
        self.data.update(catalog=lightroom_library.cache(library, now), coverage=library.coverage)
        await self._save()

        placed = place(library.photos, self.filter, library.albums, self.prefix, dt_util.now().date())
        owned = await self._run_blocking(lambda: read_owned(self.photo_dir, self.prefix, self.owner))
        lr_plan = plan(placed, owned, library.asset_count, confirm)
        self.data['ambiguous'] = [{'key': key, 'paths': paths} for key, paths in lr_plan.ambiguous]

        resting = lightroom_library.resting(self.data.get('skipped'), self.data.get('skipped_at'),
                                            library.photos, now)
        tried = []

        async def fetch(p: Placed) -> bytes | None:
            if p.photo.asset_id in resting:
                return None  # skipped less than a day ago and unchanged: no request
            tried.append(p.photo.asset_id)
            try:
                raw = await self.client.image(library.catalog, p.photo.asset_id, self.max_edge)
            except RenditionUnavailable:
                return None
            try:
                return await self.hass.async_add_executor_job(
                    album_image.normalize, raw, self.max_edge, self.quality, exif_adder(p.photo))
            except _BAD_IMAGE:
                return None

        run = await execute(lr_plan, self.photo_dir, self.owner, fetch, self._run_blocking, self.free_floor,
                            dt_util.get_default_time_zone(), now)
        skipped, skipped_at = lightroom_library.next_skips(
            run.skipped, library.photos, any(a in tried for a in run.skipped), self.data.get('skipped_at'), now)
        names = {a.id: a.name for a in library.albums.values()}
        after = await self._run_blocking(lambda: read_owned(self.photo_dir, self.prefix, self.owner))
        folders = [{'key': f.key, 'name': names.get(f.key, f.key), 'path': f.path, 'photos': len(f.items)}
                   for f in sorted(after.values(), key=lambda f: f.path)]
        return {'status': 'awaiting_confirmation' if lr_plan.hold else 'ok', 'error': None, 'hold': lr_plan.hold,
                'counts': {'added': run.added, 'updated': run.updated, 'moved': run.moved, 'removed': run.removed},
                'skipped': skipped, 'skipped_at': skipped_at,
                'photos': sum(f['photos'] for f in folders), 'folders': folders}

    @callback
    def _tick(self, _now) -> None:
        if not self.closing and (self._task is None or self._task.done()) and not self.lock.locked():
            self._task = self.entry.async_create_background_task(self.hass, self.sync(),
                                                                 'domosenso_photos lightroom sync')

    async def _save(self) -> None:
        if self.closing:  # a closed manager must not overwrite what its successor saves
            return
        await self.store.async_save(self.data)


async def _options_changed(hass, entry) -> None:
    """Reload for an options change only; a refreshed token also updates the entry."""
    manager = getattr(entry, 'runtime_data', None)
    if manager is None or dict(entry.options) != manager.options_seen:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass, entry) -> bool:
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    except (ValueError, config_entry_oauth2_flow.ImplementationUnavailableError) as err:
        raise ConfigEntryNotReady('Lightroom application credentials are not available') from err
    manager = LightroomManager(hass, entry, config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation))
    await manager.async_load()
    entry.runtime_data = manager
    entry.async_on_unload(entry.add_update_listener(_options_changed))
    manager.start()
    return True


async def async_unload_entry(hass, entry) -> bool:
    if manager := getattr(entry, 'runtime_data', None):
        await manager.async_close()
    return True


async def async_remove_entry(hass, entry) -> None:
    """The catalog cache goes with the entry (the token is in the entry data). Photo folders stay."""
    await Store(hass, 1, store_key(entry.entry_id)).async_remove()
