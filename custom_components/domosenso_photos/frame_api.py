"""Versioned in-process facade for other integrations (the paired Frame bridge).

No homeassistant imports. The album password is only handed to ShareClient: it never reaches a
URL, a log line, an exception text or a return value.
"""
from __future__ import annotations

import asyncio

from .album_plan import AlbumError
from .album_share import AuthFailed, ShareClient, ShareError, Unreachable, api_base, parse_share_url, same_link

CHECK_TIMEOUT_S = 10


class FrameApi:
    version = 1

    def __init__(self, manager):
        self._manager = manager
        self._tasks: set[asyncio.Task] = set()

    def albums(self) -> list[dict]:
        return self._manager.public_list()

    async def check(self, url: str, password: str | None) -> str:
        """The album name when the link opens with this password; AlbumError(code) otherwise."""
        m = self._manager
        try:
            link = parse_share_url(str(url).strip())
        except ValueError:
            raise AlbumError('invalid_link') from None
        if any(same_link(rec['url'], link) for rec in m.albums.values()):
            raise AlbumError('already_added')
        client = ShareClient(m._transport, api_base(link, m.host_map), link.passphrase, password or None)
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                await client.login()
                return (await client.album()).name
        except AuthFailed:
            raise AlbumError('wrong_password') from None
        except (Unreachable, ShareError, TimeoutError, LookupError, TypeError):  # the last two: a reply of another shape
            raise AlbumError('unreachable') from None

    async def add(self, url: str, password: str | None) -> dict:
        """Save the album and start its first sync in the background."""
        rec = await self._manager.add_background(url, password or None)
        return {'id': rec['id'], 'name': rec.get('name')}

    async def remove(self, album_id: str) -> str:
        if album_id not in self._manager.albums:
            raise AlbumError('unknown_album')
        return 'removed' if await self._manager.remove(album_id) is None else 'busy'

    async def sync(self, album_id: str) -> None:
        if album_id not in self._manager.albums:
            raise AlbumError('unknown_album')
        task = asyncio.get_running_loop().create_task(self._manager._sync_one(album_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
