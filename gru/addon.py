""" Module holding classes of addon and dependency objects """

from __future__ import annotations

import re
import datetime
import pathlib
import warnings
import unicodedata
from typing import Protocol
from collections.abc import Sequence

from .config import encoding_open

GARBAGE = {'__MACOSX', '.DS_STORE'}
MANIFEST_EXTS = ('.txt', '.addon')


def atol(val: str) -> int:
    """ Convert to int() with C atol semantics, i.e. ignore leading whitespace and stop at first non-numeric char """
    num = re.match('[0-9]+', val.lstrip())
    return int(num.group(0)) if num is not None else 0


def _parse_version(value: str) -> tuple[int, ...] | None:
    """ Parse a dotted version string into a tuple of ints, or None if it has no digits at all """
    if not isinstance(value, str) or not any(char.isdigit() for char in value):
        return None
    return tuple(atol(token) for token in value.split('.'))


# AddonInfo
# id api version title author
# date link category directories
# downloads monthly favorites
# thumbnails images donate

# InstalledAddon
# api version author title
# library
# dep_version (numerical version for comparison)
# description savedvariables contributors
# optdeps deps pcdependson consoledependson


class DisplayAddonProtocol(Protocol):
    id: int | None
    title: str
    author: str
    version: str
    api: Sequence[int | dict[str, str]]
    is_local: bool
    metadata: dict
    @property
    def can_update(self) -> bool: raise NotImplementedError
    # Basic metadata


class Dependency:
    def __init__(self, dir_: str, version: int = 0) -> None:
        self.dir = dir_
        self.dep_version = version


# id category directories
# version date title author link api
# downloads monthly favorites
# thumbnails images donate

class AddonInfo(DisplayAddonProtocol):
    """ Addon information from API """
    invalid_chars = re.compile(r'[^\w-]')
    is_local = False

    def __init__(self, id_: int, metadata: dict) -> None:
        self.id = id_
        self.metadata = {**metadata}
        self.title = self.metadata.pop('title')
        self.author = self.metadata.pop('author')
        self.version = self.metadata.pop('version')
        self.api = self.metadata.pop('api')
        self.folders: dict[pathlib.Path, InstalledAddon] = {}

        dirs = set(self.metadata['directories']) - GARBAGE
        self.metadata['directories'] = list(dirs)

        # TODO: assumption here of >1 dir is “naked” addon, but e.g. LibGroupBroadcast
        # declares 2 top-levels: LibGroupBroadcast, LibGroupSocket
        if {'lang', 'libs', 'EsoUI', 'gamedata', ''} & dirs:
            self.dir = self.slugify(self.title)
        elif len(dirs) != 1:
            # warnings.warn(f'Addon {self.title} declares several directories: {", ".join(map(repr, dirs))}')
            self.dir = self.slugify(self.title)
        else:
            self.dir = self.metadata['directories'][0]

    @classmethod
    def slugify(cls, value: str) -> str:
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
        return cls.invalid_chars.sub('', value).strip('_-')

    def register(self, addon: InstalledAddon) -> None:
        self.folders[addon.folder] = addon

    def deregister(self, addon: InstalledAddon) -> None:
        del self.folders[addon.folder]

    @property
    def can_update(self) -> bool:
        if not self.folders:
            return False  # Not installed: can install, but not update

        # How stale is this info?
        return any(inst.can_update for inst in self.folders.values() if inst.folder.exists())


# 122 api
#  59 library
# 121 version  # display version string
# 121 author
# 112 title
#  94 dep_version  # numerical version for comparison
#  90 description
#  82 savedvariables
#  68 optdeps
#  65 deps
#   6 contributors
#   4 pcdependson, consoledependson


class InstalledAddon(Dependency, DisplayAddonProtocol):
    """ An installed addon as detected by the game, with matching manifest. """
    is_local = True
    is_lib: bool

    def __init__(self, path: pathlib.Path, parent: InstalledAddon | None = None) -> None:
        self.folder = path
        super().__init__(path.name, 0)  # set dir
        self.id = None
        self.infos: AddonInfo | None = None
        self.parent = parent

        # Validate
        self.metadata = self._parse_manifest()

    @property
    def manifest(self) -> pathlib.Path:
        for ext in MANIFEST_EXTS:
            manifest = self.folder / f'{self.dir}{ext}'
            if manifest.exists():
                return manifest
        raise FileNotFoundError('Invalid addon: Missing manifest')

    def _parse_manifest(self) -> dict[str, str]:
        """ Parse the manifest file """
        # Parse metadata handling multiple line entries
        metadata = {}
        with encoding_open(self.manifest) as manifest:
            for line in manifest:
                if line.startswith('## ') and len(line) > 4:
                    try:
                        key, val = line.lstrip('#').split(':', 1)
                    except ValueError:
                        continue
                    metadata.setdefault(key.strip(), []).append(val.strip())

        metadata = {key: ' '.join(val) for key, val in metadata.items()}
        missing_mandatory_keys = {'Title', 'APIVersion'} - metadata.keys()

        # Now handle all interesting metadata
        self.dep_version = atol(metadata.pop('AddOnVersion', '1'))
        api_versions = [atol(api) for api in metadata.pop('APIVersion', '').split()]
        assert 1 <= len(api_versions) <= 2 and 100003 <= min(api_versions) and max(api_versions) <= 999999, \
            f'Unexpected API Version format {api_versions!r}'
        self.api = api_versions

        is_lib_str = metadata.pop('IsLibrary', 'false').lower()
        assert is_lib_str in {'true', 'false'}, f'Unexpected value for IsLibrary {is_lib_str!r}'
        self.is_lib = is_lib_str == 'true'

        self.deps = [Dependency(name, atol(version[0]) if version else 0) for name, *version in (
            dep.split('>=') for dep in metadata.pop('DependsOn', '').split() + metadata.pop('PCDependsOn', '').split()
        )]
        self.optdeps = [Dependency(name, atol(version[0]) if version else 0) for name, *version in (
            dep.split('>=') for dep in metadata.pop('OptionalDependsOn', '').split()
        )]

        # Whatever remains: title, author, etc.
        infos = {key.lower(): val for key, val in metadata.items()}

        self.title = infos.pop('title', self.manifest.stem)
        self.author = infos.pop('author', 'unknown')
        self.version = infos.pop('version', 'unknown')

        # NB. emit warning last
        if missing_mandatory_keys:
            warnings.warn(f'Missing mandatory key(s) {", ".join(map(repr, missing_mandatory_keys))}'
                          f' in {self.manifest.relative_to(self.folder.parent)}')

        return infos

    def __repr__(self) -> str:
        parts = [
            f'folder={self.folder}',
            f'nesting={self.parent is not None}',
            *(f'{key}={val}' for key, val in self.metadata.items()),
        ]
        return f'Addon[dir={getattr(self, "dir", None)}, id={getattr(self, "id", None)}]({", ".join(parts)})'

    def link(self, infos: AddonInfo) -> None:
        self.id = infos.id
        self.infos = infos
        infos.register(self)

    @property
    def can_update(self) -> bool:
        if self.id is None or self.infos is None:
            return False

        is_local = _parse_version(self.version)
        upstream = _parse_version(self.infos.version)
        if is_local is not None and upstream is not None:
            return is_local < upstream

        stat = self.manifest.stat()
        # NB. some file systems have 2s resolution, but addons should never get updates within 2s
        return datetime.datetime.fromtimestamp(max(stat.st_mtime, stat.st_ctime) + 2) <= self.infos.metadata['date']

    @property
    def files(self) -> list[pathlib.Path]:
        return [
            path for path in (path.relative_to(self.folder) for path in self.folder.rglob('*') if path.is_file())
            if not any(part.startswith('.') or part in GARBAGE for part in path.parts)
        ]
