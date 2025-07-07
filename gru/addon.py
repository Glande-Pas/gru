""" Module holding classes of addon and dependency objects """

from __future__ import annotations

import re
import unicodedata

GARBAGE = {'__MACOSX', '.DS_STORE'}


def atol(val):
    """ Convert to int() with C atol semantics, i.e. ignore leading whitespace and stop at first non-numeric char """
    num = re.match('[0-9]+', val.lstrip())
    return int(num.group(0)) if num is not None else 0


class Dependency:
    def __init__(self, dir_, version=0):
        self.dir = dir_
        self.version = version


class Addon:
    def __init__(self, id_, path, infos=None):
        self.id = id_
        self.folder = path
        self.metadata = infos or {}

        if path is not None:
            self.dir = path.name
        elif id_ is None:
            raise ValueError('At least one of id or path must be provided')
        else:
            assert getattr(self, 'dir', None) is not None, 'dir not set with id'

    def alt_location(self, root: pathlib.Path):
        return Addon(self.id, root / self.dir, self.metadata)

    def merge(self, infos):
        if isinstance(infos, Dependency):
            return self

        if infos.id is not None and self.id is None:
            self.id = infos.id
        elif infos.folder is not None and self.folder is None:
            self.folder = infos.folder
            self.dir = infos.dir  # Should be the same but local wins?
        self.metadata.update(infos.metadata)
        return self

    @property
    def manifest(self):
        for ext in ('.txt', '.addon'):
            manifest = self.folder / f'{self.dir}{ext}'
            if manifest.exists():
                return manifest

    def can_update(self):
        if self.folder is None:
            return True
        elif self.id is None:
            return False

        try:
            local = tuple(atol(token) for token in self.metadata['installed_version'].split('.'))
            upstream = tuple(atol(token) for token in self.metadata['version'].split('.'))
        except KeyError:
            pass
        else:
            return local < upstream

        stat = self.manifest.stat()
        # NB. some file systems have 2s resolution, but addons should never get updates within 2s
        return datetime.datetime.from_timestamp(max(stat.st_mtime, stat.st_ctime) + 2) <= self.metadata['date']

    @property
    def files(self):
        return [
            path for path in (path.relative_to(self.folder) for path in self.folder.rglob('*') if path.is_file())
            if not any(part.startswith('.') or part in GARBAGE for part in path.parts)
        ]


class APIAddonInfo(Addon):
    invalid_chars = re.compile(r'[^\w-]')

    def __init__(self, id_, infos):
        dirs = set(infos['directories']) - GARBAGE
        if len(dirs) != 1 or {'lang', 'libs', 'EsoUI', 'gamedata'} & dirs:
            self.dir = self.slugify(infos['title'])
        else:
            self.dir = infos['directories'][0]
        super().__init__(id_, None, infos)

    @classmethod
    def slugify(cls, value):
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
        return cls.invalid_chars.sub('', value).strip('_-')

    def merge(self, infos):
        raise ValueError('Always merge API info into local Addon -- consider API addons as read-only')
