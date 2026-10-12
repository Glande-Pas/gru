""" Game API versions ("interface" numbers) and the game updates they belong to.

The source of truth is a JSON list kept in its own repository: entries of {"id", "name", "interface", "default"},
where `default` marks the version live servers run. Fetched with a conditional request and cached. """

import contextlib
import json
import logging
import time
import typing
from dataclasses import dataclass, field

import requests

from .config import user_cache

logger = logging.getLogger(__name__)

URL = 'https://gitlab.com/glandepas/eso-api-versions/-/raw/main/compatible.json'
CACHE_NAME = 'api-versions.json'
CHECK_EVERY = 6 * 3600  # seconds between revalidations
RETRY_AFTER = 600  # seconds before trying again after a failed fetch


@dataclass(frozen=True)
class ApiVersion:
    interface: int
    name: str
    id: str = ''
    default: bool = False


@dataclass(frozen=True)
class ApiVersions:
    versions: dict[int, ApiVersion] = field(default_factory=dict)

    def name(self, interface: int) -> str | None:
        """ The game update an API version belongs to, e.g. 'Season One' """
        version = self.versions.get(interface)
        return version.name if version is not None else None

    def current(self) -> int | None:
        """ The API version live servers run (highest if several are marked) """
        defaults = [version.interface for version in self.versions.values() if version.default]
        return max(defaults) if defaults else None


def parse(data: typing.Any) -> ApiVersions:
    """ Tolerant: entries that aren't well-formed are skipped. """
    versions = {}
    for entry in data if isinstance(data, list) else []:
        try:
            version = ApiVersion(int(entry['interface']), str(entry['name']), str(entry.get('id', '')),
                                 entry.get('default') is True)
        except (KeyError, TypeError, ValueError):
            continue
        versions[version.interface] = version
    return ApiVersions(versions)


def load(url: str = URL, timeout: float = 5, now: float | None = None) -> ApiVersions | None:
    """ The API versions list, revalidated with the server at most every CHECK_EVERY seconds. Falls back to the cached
    copy when offline; None if there is neither. Does HTTP: call from a worker. """
    now = time.time() if now is None else now
    try:
        path = user_cache(CACHE_NAME)
    except OSError:
        return None
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(cached, dict):
            cached = {}
    except (OSError, ValueError):
        cached = {}

    entries = cached.get('versions')
    if (entries is not None and now - cached.get('checked', 0) < CHECK_EVERY) \
            or now - cached.get('failed', 0) < RETRY_AFTER:
        return parse(entries) if entries is not None else None

    headers = {'If-None-Match': cached['etag']} if cached.get('etag') and entries is not None else {}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 304 and entries is not None:
            fresh = {**{k: v for k, v in cached.items() if k != 'failed'}, 'checked': now}
        else:
            response.raise_for_status()
            fresh = {'etag': response.headers.get('ETag', ''), 'checked': now, 'versions': response.json()}
    except (requests.RequestException, ValueError) as exc:
        logger.debug('api versions: could not refresh (%s), using the cached copy', exc)
        with contextlib.suppress(OSError):
            path.write_text(json.dumps({**cached, 'failed': now}), encoding='utf-8')
        return parse(entries) if entries is not None else None

    try:
        path.write_text(json.dumps(fresh), encoding='utf-8')
    except OSError:
        pass
    return parse(fresh['versions'])
