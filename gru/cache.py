# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Pruning of the disposable files under the user cache dir """

from __future__ import annotations

import datetime
import hashlib
import logging
import pathlib
import re
import time
from collections.abc import Iterable

from .addon import InstalledAddon
from .config import user_cache

logger = logging.getLogger(__name__)

DOWNLOAD_MAX_AGE = datetime.timedelta(days=7)
DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024
HISTORY_MAX_ENTRIES = 1000


def download_name(id_: int | None, dir_: str, version: str, url: str | None = None) -> str:
    """ File name under `dl/` of a listing's zip. `url` is set when the zip does not come from the listing's own
    download link (e.g. an archived release), so it cannot be mistaken for it. """
    def clean(text: str) -> str:
        return re.sub(r'[^\w.+-]', '_', text)
    suffix = f'-{hashlib.sha1(url.encode()).hexdigest()[:8]}' if url else ''
    return f'{id_}-{clean(dir_)}-{clean(version)}{suffix}.zip'


def prune_downloads(installed: Iterable[InstalledAddon], now: float | None = None,
                    max_age: datetime.timedelta = DOWNLOAD_MAX_AGE,
                    max_bytes: int = DOWNLOAD_MAX_BYTES) -> list[pathlib.Path]:
    """ Delete cached zips from `dl/`, returning what was removed.

    Kept regardless of age: each installed addon's own release (see download_name()) -- that is what
    `gru diff` needs. Everything else goes once older than `max_age`, and beyond that oldest-first
    while the remainder exceeds `max_bytes`. """
    now = time.time() if now is None else now
    zips = sorted((p for p in user_cache('dl').glob('*.zip') if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)

    pinned = {download_name(addon.infos.id, addon.infos.dir, addon.version)
              for addon in installed if addon.infos is not None}

    cutoff = now - max_age.total_seconds()
    removable = [p for p in zips if p.name not in pinned]
    doomed = [p for p in removable if p.stat().st_mtime < cutoff]
    kept = [p for p in removable if p not in doomed]
    total = sum(p.stat().st_size for p in kept)
    for zip_path in reversed(kept):  # oldest first
        if total <= max_bytes:
            break
        total -= zip_path.stat().st_size
        doomed.append(zip_path)

    removed = []
    for zip_path in doomed:
        try:
            zip_path.unlink()
        except OSError as exc:
            logger.debug('could not prune %s: %s', zip_path, exc)
            continue
        removed.append(zip_path)
    return removed


def prune_http_caches(*sessions) -> None:
    """ Drop expired responses from the requests-cache sessions, and reclaim the space """
    for session in sessions:
        try:
            session.cache.delete(expired=True)
            session.cache.responses.vacuum()
        except Exception as exc:  # cache hygiene must never block startup
            logger.debug('could not prune http cache: %s', exc)


def trim_history(path: pathlib.Path, max_entries: int = HISTORY_MAX_ENTRIES) -> None:
    """ Keep only the last `max_entries` commands of a prompt_toolkit FileHistory file """
    try:
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    except OSError:
        return
    starts = [i for i, line in enumerate(lines) if line.startswith('#')]  # one timestamp comment per entry
    if len(starts) <= max_entries:
        return
    try:
        path.write_text(''.join(lines[starts[-max_entries]:]), encoding='utf-8')
    except OSError as exc:
        logger.debug('could not trim %s: %s', path, exc)
