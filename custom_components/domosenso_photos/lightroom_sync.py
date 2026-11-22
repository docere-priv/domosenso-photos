"""Plan and apply Lightroom changes on the folders this source owns (no Home Assistant import)."""
from __future__ import annotations

import errno
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path, PurePosixPath

from .album_files import MARKER, free_bytes, read_marker, remove_photo, write_marker, write_photo
from .album_layout import PhotoDirMissing, inside, make_dirs, rename_target, resolve_path
from .album_sync import RunBlocking
from .lightroom_filter import Placed

MASS_MIN = 20
_MONTH = re.compile(r'\d{4}-\d{2}')
_DUP = '\x00'  # dict key prefix of a second folder carrying an already seen key; never a real key


class DiskFull(Exception):
    pass


@dataclass(frozen=True)
class OwnedFolder:
    key: str
    path: str
    items: dict[str, dict]  # asset id -> {'name': str, 'changed': str}


@dataclass(frozen=True)
class LrPlan:
    add: list[Placed]
    update: list[Placed]
    move: list[tuple[Placed, str]]  # (placed, old key)
    remove: list[tuple[str, str]]  # (key, asset id)
    rename: list[tuple[str, str, str]]  # (key, old path, new wanted path)
    hold: str | None
    # The folders the plan was made against, so execute finds a key's path. A second folder with the
    # same key (a damaged state) sits under _DUP + its path; the tuples above may name that dict key.
    owned: dict[str, OwnedFolder] = field(default_factory=dict)


@dataclass
class RunResult:
    added: int = 0
    updated: int = 0
    moved: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)


def file_name(p: Placed) -> str:
    return f'{p.photo.captured:%Y-%m-%d}_{p.photo.asset_id}.jpg'


def _plain(name) -> bool:
    """A bare file name from a marker: no separator, not empty, not `.` or `..`."""
    return isinstance(name, str) and name not in ('', '.', '..') and PurePosixPath(name).name == name


def _ours(marker: dict | None, owner: str, key: str | None = None) -> bool:
    return (isinstance(marker, dict) and marker.get('owner') == owner and isinstance(marker.get('key'), str)
            and (key is None or marker['key'] == key) and isinstance(marker.get('items'), dict))


def _present(folder: Path, items: dict) -> dict[str, dict]:
    """Marker entries with a plain name whose file is still there; a missing file is fetched again."""
    return {str(k): dict(v) for k, v in items.items()
            if isinstance(v, dict) and _plain(v.get('name')) and (folder / v['name']).is_file()}


def read_owned(photo_dir: Path, prefix: str, owner: str) -> dict[str, OwnedFolder]:
    base = photo_dir / prefix if prefix else photo_dir
    out: dict[str, OwnedFolder] = {}
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir(), key=lambda c: c.name):
        path = f'{prefix}/{child.name}' if prefix else child.name
        if not child.is_dir() or not inside(photo_dir, path):
            continue
        marker = read_marker(child)
        if not _ours(marker, owner):
            continue
        folder = OwnedFolder(marker['key'], path, _present(child, marker['items']))
        out[folder.key if folder.key not in out else _DUP + path] = folder
    return out


def _in_place(path: str, wanted: str) -> bool:
    """The folder is where the placement wants it, or at the suffixed name a collision gave it."""
    rest = path[len(wanted) + 1:] if path.startswith(f'{wanted}-') else ''
    return path == wanted or (rest.isdigit() and int(rest) >= 2)


def _kept(owned: dict[str, OwnedFolder], wanted: dict[str, str]) -> tuple[dict[str, OwnedFolder], list[str]]:
    """Per key the folder to keep (under the key) and the dict keys of the other same-key folders."""
    by_key: dict[str, list[OwnedFolder]] = {}
    for f in owned.values():
        by_key.setdefault(f.key, []).append(f)
    kept, stale = {}, []
    for key, group in by_key.items():
        group.sort(key=lambda f: f.path)
        want = wanted.get(key)
        best = (next((f for f in group if f.path == want), None)
                or next((f for f in group if want is not None and _in_place(f.path, want)), None) or group[0])
        kept[key] = best
        stale += [_DUP + f.path for f in group if f is not best]
    return kept, stale


def plan(placed: list[Placed], owned: dict[str, OwnedFolder], listed_assets: int, confirm: bool) -> LrPlan:
    wanted: dict[str, Placed] = {}
    for p in placed:
        wanted.setdefault(p.photo.asset_id, p)
    folder_for = {p.key: p.folder for p in wanted.values()}
    kept, stale_keys = _kept(owned, folder_for)
    by_path = {f.path: f for f in owned.values()}
    folders = kept | {dkey: by_path[dkey[len(_DUP):]] for dkey in stale_keys}

    where: dict[str, list[str]] = {}  # asset id -> keys of kept folders holding it
    for key in sorted(kept):
        for asset in kept[key].items:
            where.setdefault(asset, []).append(key)
    total = len(where)

    add, update, move, remove, stale = [], [], [], [], []
    for asset, p in wanted.items():
        holders = where.get(asset, [])
        if p.key in holders:
            if kept[p.key].items[asset].get('changed') != p.photo.changed:
                update.append(p)
            stale += [(k, asset) for k in holders if k != p.key]
        elif holders:
            move.append((p, holders[0]))
            stale += [(k, asset) for k in holders[1:]]
        else:
            add.append(p)
    gone = [asset for asset in where if asset not in wanted]
    for asset in gone:
        remove.append((where[asset][0], asset))
        stale += [(k, asset) for k in where[asset][1:]]
    for dkey in stale_keys:
        stale += [(dkey, asset) for asset in sorted(folders[dkey].items)]

    rename = [(key, f.path, folder_for[key]) for key, f in sorted(kept.items())
              if key in folder_for and not _in_place(f.path, folder_for[key])]

    unfiled = sum(1 for p, old in move if _MONTH.fullmatch(p.key) and not _MONTH.fullmatch(folders[old].key))
    hold = None
    if listed_assets == 0 and total > 0:
        hold = 'empty_library'
    elif total >= MASS_MIN and len(remove) > total / 2:
        hold = 'mass_remove'
    elif total >= MASS_MIN and unfiled > total / 2:
        hold = 'mass_unfile'
    if hold and confirm:
        hold = None
    if hold:
        move, remove, stale = [], [], []
    return LrPlan(add, update, move, remove + stale, rename, hold, folders)


@dataclass
class _Exec:
    plan: LrPlan
    photo_dir: Path
    owner: str
    run_blocking: RunBlocking
    now: int
    paths: dict[str, str] = field(default_factory=dict)  # plan dict key -> current path
    markers: dict[str, dict | None] = field(default_factory=dict)  # plan dict key -> marker read from disk

    async def io(self, fn, *args):
        return await self.run_blocking(lambda: fn(*args))

    def load(self, dkey: str) -> dict | None:
        """The folder's marker, only while the folder is inside photo_dir and still ours for its key."""
        if dkey not in self.markers:
            path, f = self.paths.get(dkey), self.plan.owned.get(dkey)
            marker = None
            if path and f and inside(self.photo_dir, path):
                found = read_marker(self.photo_dir / path)
                if _ours(found, self.owner, f.key):
                    marker = self._fresh(f.key, _present(self.photo_dir / path, found['items']))
            self.markers[dkey] = marker
        return self.markers[dkey]

    def _fresh(self, key: str, items: dict) -> dict:
        return {'owner': self.owner, 'kind': 'lightroom', 'key': key, 'items': items, 'last_sync': self.now}

    def rename(self, dkey: str, old: str, wanted: str) -> None:
        if self.paths.get(dkey) != old or not inside(self.photo_dir, wanted) or self.load(dkey) is None:
            return
        new = rename_target(self.photo_dir, old, wanted)
        if new == old:
            return
        make_dirs(self.photo_dir, str(PurePosixPath(new).parent))
        (self.photo_dir / old).rename(self.photo_dir / new)
        self.paths[dkey] = new

    def target(self, p: Placed) -> tuple[str, dict] | None:
        """The folder a placed photo is written to, created when this key has none; None if unsafe."""
        if self.load(p.key) is not None:
            return self.paths[p.key], self.markers[p.key]
        if not inside(self.photo_dir, p.folder):
            return None
        path = resolve_path(self.photo_dir, p.folder, self.owner, p.key)
        folder = self.photo_dir / path
        found = read_marker(folder)
        make_dirs(self.photo_dir, path)
        items = _present(folder, found['items']) if _ours(found, self.owner, p.key) else {}
        self.paths[p.key], self.markers[p.key] = path, self._fresh(p.key, items)
        write_marker(folder, self.markers[p.key])  # mark the folder ours before the first file lands
        return path, self.markers[p.key]

    def save(self, dkey: str) -> None:
        """Write the folder's marker; a folder left with nothing but its marker loses both."""
        folder, marker = self.photo_dir / self.paths[dkey], self.markers[dkey]
        marker['last_sync'] = self.now
        write_marker(folder, marker)
        if not marker['items'] and [c.name for c in folder.iterdir()] == [MARKER]:
            (folder / MARKER).unlink()
            folder.rmdir()
            self.markers[dkey] = None
            del self.paths[dkey]

    def drop(self, dkey: str, asset: str) -> bool:
        """Remove one owned file named by the folder's marker; False when there is nothing ours to remove."""
        marker = self.load(dkey)
        entry = marker['items'].get(asset) if marker else None
        if entry is None:
            return False
        remove_photo(self.photo_dir / self.paths[dkey], entry['name'])
        del marker['items'][asset]
        self.save(dkey)
        return True

    def write(self, p: Placed, data: bytes, tz: tzinfo) -> bool:
        spot = self.target(p)
        if spot is None:
            return False
        path, marker = spot
        name, asset = file_name(p), p.photo.asset_id
        previous = marker['items'].get(asset)
        write_photo(self.photo_dir / path, name, data, int(p.photo.captured.replace(tzinfo=tz).timestamp()))
        marker['items'][asset] = {'name': name, 'changed': p.photo.changed}
        self.save(p.key)
        if previous and previous['name'] != name:  # the capture date changed, so did the name
            remove_photo(self.photo_dir / path, previous['name'])
        return True


async def execute(plan: LrPlan, photo_dir: Path, owner: str, fetch: Callable[[Placed], Awaitable[bytes | None]],
                  run_blocking: RunBlocking, free_floor: int, tz: tzinfo, now: int) -> RunResult:
    """Apply the plan; raises PhotoDirMissing (nothing touched) or DiskFull (stops before the next write)."""
    if not await run_blocking(photo_dir.is_dir):  # unmounted storage: touch nothing, never re-create it
        raise PhotoDirMissing
    run = _Exec(plan, photo_dir, owner, run_blocking, now, {k: f.path for k, f in plan.owned.items()})
    result = RunResult()
    try:
        for dkey, old, wanted in plan.rename:
            await run.io(run.rename, dkey, old, wanted)
        writes = ([(p, None, 'added') for p in plan.add] + [(p, None, 'updated') for p in plan.update]
                  + [(p, old, 'moved') for p, old in plan.move])
        for p, old, counter in writes:
            if await run.io(free_bytes, photo_dir) < free_floor:
                raise DiskFull
            data = await fetch(p)
            if data is None or not await run.io(run.write, p, data, tz):
                result.skipped.append(p.photo.asset_id)
                continue
            if old is not None:  # the new copy and its marker are on disk; now the old one goes
                await run.io(run.drop, old, p.photo.asset_id)
            setattr(result, counter, getattr(result, counter) + 1)
        for dkey, asset in plan.remove:
            if await run.io(run.drop, dkey, asset):
                result.removed += 1
    except OSError as err:
        if err.errno == errno.ENOSPC:
            raise DiskFull from err
        raise
    return result
