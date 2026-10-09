# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Module holding classes of addon and dependency objects """

from __future__ import annotations

import re
import zlib
import hashlib
import dataclasses
import collections
import datetime
import pathlib
import warnings
import unicodedata
from typing import Protocol, Union
from collections.abc import Sequence

from .config import encoding_open

GARBAGE = {'__MACOSX', '.DS_STORE'}
MANIFEST_EXTS = ('.txt', '.addon')


def atol(val: str) -> int:
    """ Convert to int() with C atol semantics, i.e. ignore leading whitespace and stop at first non-numeric char """
    num = re.match('[0-9]+', val.lstrip())
    return int(num.group(0)) if num is not None else 0


VersionKey = tuple[tuple[int, Union[int, str]], ...]

# Token kinds, in sort order: pre-release words sort below the bare release (END), which sorts below any suffix
_PRE, _END, _WORD, _NUM = range(4)
_PRERELEASE = {name: rank for rank, name in enumerate(('alpha', 'beta', 'pre', 'rc'))}


def _parse_version(value: str) -> VersionKey | None:
    """ Tokenize a version into numbers and letter runs (anything else is a separator), or None if it has no digits.
    Numbers compare numerically, letters lexically (case-insensitive) and sort below numbers, except the
    pre-release words alpha < beta < pre < rc, which sort below the release itself:
    1.2rc1 < 1.2 < 1.2a < 1.2.0 """
    if not isinstance(value, str) or not any(char.isdigit() for char in value):
        return None
    value = re.sub(r'^\s*v\.?\s*(?=\d)', '', value, flags=re.IGNORECASE)
    key: list[tuple[int, int | str]] = []
    for tok in re.findall(r'\d+|[^\W\d_]+', value):
        if tok.isdigit():
            key.append((_NUM, int(tok)))
        elif (word := tok.lower()) in _PRERELEASE:
            key.append((_PRE, _PRERELEASE[word]))
        else:
            key.append((_WORD, word))
    key.append((_END, 0))
    return tuple(key)


def file_crc32(path: pathlib.Path) -> int:
    """ CRC-32 of a file's contents, comparable against gru.remotezip.RemoteZipEntry.crc32. """
    crc = 0
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            crc = zlib.crc32(chunk, crc)
    return crc


@dataclasses.dataclass(frozen=True)
class VersionOverride:
    """ The install of listing `id` at `listing_version` shows `local_version` in its manifest and hashed to
    `fingerprint` (see InstalledAddon.fingerprint()). Only valid while all of them still match. """
    id: int
    listing_version: str
    local_version: str
    fingerprint: str


ESO_COLORED_TEXT = re.compile(r'\|c(?P<color>[0-9a-fA-F]{6})(?P<text>[^|]+)(?:\|r)?')


def strip_eso_text(text: str) -> str:
    """ Plain visible text with ESO |cRRGGBB...|r color markup removed, for comparisons (not
    display -- see cli.TermDisplay._render_eso_text() for the rendering counterpart). """
    return ESO_COLORED_TEXT.sub(lambda match: match.group('text'), text)


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


class InstalledAddon(Dependency, DisplayAddonProtocol):
    #: Restored from versions.csv by Folder; replaces `version` with the listing's when set. Class-level as
    #: AddonBundle skips __init__.
    override: VersionOverride | None = None

    """ An installed addon as detected by the game, with matching manifest. """
    is_local = True
    is_lib: bool

    def __init__(self, path: pathlib.Path, parent: InstalledAddon | AddonBundle | None = None) -> None:
        self.folder = path
        super().__init__(path.name, 0)  # set dir
        self.id = None
        self.infos: AddonInfo | None = None
        self.parent = parent
        #: Not derivable from the folder scan -- restored from addons.csv by Folder.scan()
        self.locked = False

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

        is_lib_tokens = metadata.pop('IsLibrary', 'false').lower().split()
        is_lib_str = is_lib_tokens[-1] if is_lib_tokens else 'false'
        if is_lib_str not in {'true', 'false'}:
            warnings.warn(f'Unexpected value for IsLibrary {is_lib_str!r}'
                          f' in {self.manifest.relative_to(self.folder.parent)}, treating as false')
            is_lib_str = 'false'
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
        self.manifest_version = infos.pop('version', 'unknown')

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

    def fingerprint(self) -> str:
        """ Digest of every file's path and CRC-32, same normalisation as the remote zip comparison """
        digest = hashlib.sha256()
        for path, crc in sorted((path.as_posix().lower(), file_crc32(self.folder / path)) for path in self.files):
            digest.update(f'{path}\0{crc:08x}\n'.encode())
        return digest.hexdigest()[:32]

    def record_override(self, listing_version: str) -> None:
        """ Called right after installing `listing_version` of the linked listing """
        self.override = None
        if self.id is None or self.manifest_version == listing_version:
            return
        self.override = VersionOverride(self.id, listing_version, self.manifest_version, self.fingerprint())

    def apply_override(self, candidates: Sequence[VersionOverride]) -> bool:
        """ Show the listing version if a candidate still describes this exact install """
        if self.id is None:
            return False
        candidates = [c for c in candidates if c.id == self.id and c.local_version == self.manifest_version]
        if not candidates:
            return False
        fingerprint = self.fingerprint()
        match = next((c for c in candidates if c.fingerprint == fingerprint), None)
        if match is None:
            return False
        self.override = match
        return True

    @property
    def version(self) -> str:
        """ The listing's version if an override applies, otherwise the manifest's """
        return self.override.listing_version if self.override is not None else self.manifest_version

    @version.setter
    def version(self, value: str) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        self.manifest_version = value

    @property
    def can_update(self) -> bool:
        if self.id is None or self.infos is None:
            return False

        is_local = _parse_version(self.version)
        upstream = _parse_version(self.infos.version)
        if is_local is not None and upstream is not None:
            return is_local < upstream

        # NB. some file systems have 2s resolution, but addons should never get updates within 2s
        # NB2. A spurious update 'self-heals': reinstalling rewrites the manifest which bumps mtime.
        return datetime.datetime.fromtimestamp(self.manifest.stat().st_mtime + 2) <= self.infos.metadata['date']

    @property
    def comparable_copies(self) -> list[InstalledAddon]:
        """ Folders sharing `infos` that are genuinely comparable copies of this one. """
        if self.infos is None or isinstance(self, AddonBundle):
            return []
        return [other for other in self.infos.folders.values() if not isinstance(other, AddonBundle)]

    @property
    def version_rank(self) -> str:
        """ 'active'/'superseded' by version among comparable_copies, or '' if unknown.

        Can only compare addons, not bundles – see comparable_copies """
        if self.infos is None:
            return ''
        others = [other for other in self.comparable_copies if other.folder != self.folder]
        if not others:
            return ''
        versions = [v for other in others if (v := _parse_version(other.version)) is not None]
        if len(versions) != len(others):
            return ''
        this = _parse_version(self.version)
        if this is None:
            return ''
        return 'active' if this >= max(versions) else 'superseded'

    @property
    def is_superseded(self) -> bool:
        return self.version_rank == 'superseded'

    @property
    def files(self) -> list[pathlib.Path]:
        return [
            path for path in (path.relative_to(self.folder) for path in self.folder.rglob('*') if path.is_file())
            if not any(part.startswith('.') or part in GARBAGE for part in path.parts)
        ]


class AddonBundle(InstalledAddon):
    """ A wrapper directory with no manifest of its own, holding several addons installed together as one bundle.
    install/update/lock happen at the bundle's own granularity, not per member (see Folder.update()/remove()),
    so it's a real InstalledAddon in its own right rather than just a display stand-in.
    Does not call InstalledAddon.__init__: there's no manifest to parse, every field is derived from its members. """

    def __init__(self, dir_: str, folder: pathlib.Path, members: list[InstalledAddon]) -> None:
        self.members = members
        self.folder = folder
        self.dir = dir_
        self.parent = None
        self.locked = False
        self.id = None
        self.infos: AddonInfo | None = None
        main = members[0]
        self.title = dir_
        self.author = main.author
        self.api = main.api
        self.metadata = main.metadata
        for member in members:
            member.parent = self

    @property
    def manifest_version(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        """ prefer member version whose own dir matches the bundle's, otherwise majority vote """
        named_main = next((member for member in self.members if member.dir == self.dir), None)
        if named_main is not None:
            return named_main.manifest_version
        counts = collections.Counter(member.manifest_version for member in self.members).most_common()
        if len(counts) == 1 or counts[0][1] > counts[1][1]:
            return counts[0][0]
        return self.members[0].manifest_version

    @property
    def deps(self) -> list[Dependency]:
        """ Unique (by dir) union of every member's own DependsOn/PCDependsOn. """
        seen: dict[str, Dependency] = {}
        for member in self.members:
            for dep in member.deps:
                seen.setdefault(dep.dir, dep)
        return list(seen.values())

    @property
    def optdeps(self) -> list[Dependency]:
        seen: dict[str, Dependency] = {}
        for member in self.members:
            for dep in member.optdeps:
                seen.setdefault(dep.dir, dep)
        return list(seen.values())

    @property
    def is_lib(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        return all(member.is_lib for member in self.members)

    @property
    def can_update(self) -> bool:
        """ Same comparison as InstalledAddon.can_update, minus its fallback (mtime check on manifest) """
        if self.id is None or self.infos is None:
            return False
        is_local = _parse_version(self.version)
        upstream = _parse_version(self.infos.version)
        if is_local is not None and upstream is not None:
            return is_local < upstream
        return False
