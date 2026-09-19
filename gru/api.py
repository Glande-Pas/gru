""" Module handling fetching info from the API """

from __future__ import annotations

import requests
import requests_cache
import datetime
import operator
import difflib
import warnings
import functools
import collections
import configparser
import typing
from collections.abc import Iterable, Iterator, Mapping

from .config import user_cache
from .addon import AddonInfo, DisplayAddonProtocol


def to_list(arg: Iterable | None) -> list:
    if arg is None:
        return []
    else:
        return list(arg)


def case_insensitive(mapping: Mapping) -> requests.structures.CaseInsensitiveDict:
    return requests.structures.CaseInsensitiveDict({
        key: case_insensitive(val) if isinstance(val, collections.abc.Mapping) else val for key, val in mapping.items()
    })


def epoch_ms(val: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(val) / 1000)


def _exception_root_cause(err: Exception) -> str:
    while True:
        if getattr(err, '__cause__', None) is not None:
            err = err.__cause__  # standard python exception chaining
        elif isinstance(err.args[0], Exception):
            err = err.args[0]   # requests exception chaining
        elif getattr(err, 'reason', None) is not None:
            err = err.reason  # urllib3 (requests' backend) exception chaining
        else:
            break

    import urllib3
    if isinstance(err, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.PoolError)):
        # Both error messages are <object>: cause
        return str(err).split(': ', 1)[-1]

    return str(err)


def _fuzz(source: Iterable[DisplayAddonProtocol], attr: str, term: str, cutoff: float, maxlen: int,
          tiebreakattr: list[str] = []) -> list[DisplayAddonProtocol]:
    """ Fuzzy search that prioritises maximal subset matches, then longest match, then by tie breakers.

    Matches `term` in the `attr` attribute within the `source` iterable, returning at most `maxlen` items.
    `tiebreakattr` is a list of numerical attributes to act as tie breakers. Search ignores case and any whitespace.

    Not using SequenceMatcher.ratio() as that compares full strings and we want sub-strings to match.
    """
    cutoff *= len(term)
    candidates = []
    matcher = difflib.SequenceMatcher(str.isspace, term.lower(), None)

    for addon in source:
        matcher.set_seq2(getattr(addon, attr).lower())
        matches = [match.size for match in matcher.get_matching_blocks()]
        # For debug log:
        #print(addon[attr], [repr(term[b.a:b.a + b.size]) for b in matcher.get_matching_blocks() if b.size])
        if sum(matches) < cutoff:
            continue
        # NB. cast for numerical attributes represented as strings in json
        prio = (sum(matches), max(matches), *(addon.metadata.get(tie) for tie in tiebreakattr))
        candidates.append((prio, addon))

    candidates = sorted(candidates, key=operator.itemgetter(0), reverse=True)
    return [addon for prio, addon in candidates[:maxlen]]

def _filter(source: Iterable[DisplayAddonProtocol], attr: str, match: str | int | None) -> Iterator[DisplayAddonProtocol]:
    """ Search with exact match (lowercased) """
    for addon in source:
        value = getattr(addon, attr)
        if isinstance(match, str) and isinstance(value, (list, tuple)):
            if match in [part.lower() for part in value if isinstance(part, str)]:
                yield addon
        elif isinstance(match, str):
            if isinstance(value, str) and match == value.lower():
                yield addon
        elif isinstance(value, (list, tuple)):
            if match in value:
                yield addon
        elif match == value:
            yield addon

def _lookup(source: Iterable[DisplayAddonProtocol], attr: str, match: str | int | None) -> DisplayAddonProtocol:
    """ Search with exact match (lowercased) """
    try:
        return next(_filter(source, attr, match))
    except StopIteration:
        # TODO: type of error?
        raise ValueError(f'{attr} {match!r} not found in list')

class API:
    session = requests_cache.CachedSession(user_cache('api'), expire_after=datetime.timedelta(hours=1))

    def  __init__(self, config: configparser.ConfigParser) -> None:
        self.pages = {}
        endpoint = config.get('api', 'endpoint')
        gamepaths = config.items(f'{self.game}UIv{self.version}.paths')
        self.pages = {key: endpoint.format(version=self.version, game=self.game, path=path) for key, path in gamepaths}

    def _load(self, url: str, fallback: typing.Any = None) -> typing.Any:
        """ Load a page and return the JSON, ensure we use cached page if <1h old """
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except requests.JSONDecodeError as err:
            warnings.warn(f'JSON decode error while loading data from {url!r}')
        except requests.HTTPError as err:
            warnings.warn(f'HTTP error while loading {url!r} status code {err.response.status_code}: {err}')
        except (requests.ConnectionError, requests.Timeout) as err:
            # Get back up to root cause for readability
            msg = _exception_root_cause(err)
            warnings.warn(f'Connection error while loading {url!r}: {msg}')
        except requests.RequestException as err:
            warnings.warn(f'Error loading {url!r}: {err}')
        return fallback

    @classmethod
    def reset(cls) -> None:
        """ Clear the cache """
        requests_cache.clear()

    def search(self, term: str, tiebreakattr: str | None = None, maxlen: int = 30) -> list[AddonInfo]:
        """ Search `term` in addon names """
        # We want at least 75% of search string in result
        if tiebreakattr is None:
            tiebreakattr = 'downloads'
        return _fuzz(self.addons.values(), 'title', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                     tiebreakattr=[tiebreakattr])

    def addon(self, id_: int) -> AddonInfo:
        """ Lookup an addon by id """
        return self.addons[id_]

    def cat(self, id_: int) -> dict:
        """ Lookup a category by id """
        return self.categories[id_]

    def dir(self, dir_: str) -> AddonInfo:
        """ Lookup an addon by directory """
        partial = []
        for addon in self.addons.values():
            if addon.dir == dir_:
                return addon
            elif dir_ in addon.metadata['directories']:
                partial.append(addon)
        if len(partial) == 1:
            return partial[0]
        raise FileNotFoundError(f'Directory {dir_!r} not found in list')

    def name(self, name: str) -> AddonInfo:
        """ Lookup an addon by name (exact match) """
        return _lookup(self.addons.values(), 'title', str(name).lower())

    def find(self, val: str, local: gru.install.Folder) -> AddonInfo | gru.addon.InstalledAddon | list[AddonInfo]:
        """ Search for an addon generically """
        # Various methods of exact matches
        try:
            return self.addon(int(val))
        except (ValueError, KeyError):
            pass

        try:
            return self.name(val)
        except ValueError:
            pass

        try:
            return self.dir(val)
        except FileNotFoundError:
            pass

        # Find by dir but locally, not from API
        for addon in local.installed:
            if addon.dir == val:
                return addon

        # Otherwise revert to search and return a list of candidates
        return self.search(val)

    @classmethod
    def _factory(cls, config: configparser.ConfigParser, game: str, stable: bool = True) -> API:
        version = config.getint('api', 'version') + int(not stable)
        if game == 'ESO' and version == 3:
            return ESOUIv3(config)
        elif game == 'ESO' and version == 4:
            return ESOUIv4(config)
        raise NotImplementedError(f'API version {version} for {game} not implemented')

    @classmethod
    def live(cls, config: configparser.ConfigParser) -> API:
        return cls._factory(config, 'ESO', True)

    @classmethod
    def alpha(cls, config: configparser.ConfigParser) -> API:
        return cls._factory(config, 'ESO', False)


class ESOUIv4(API):
    game = 'ESO'
    version = 4

    def __init__(self, config: configparser.ConfigParser) -> None:
        super().__init__(config)

    @functools.cached_property
    def globalconf(self) -> requests.structures.CaseInsensitiveDict:
        return case_insensitive(self._load(self.pages['globalconf'], {}))


class ESOUIv3(API):

    game = 'ESO'
    version = 3

    fileinfo_rename = {
        'UID':               ('id', int),
        'UICATID':           ('category', int),
        'UIVersion':         ('version', str),
        'UIDate':            ('date', epoch_ms),
        'UIName':            ('title', str),
        'UIAuthorName':      ('author', str),
        'UIFileInfoURL':     ('link', str),
        'UIDownloadTotal':   ('downloads', int),
        'UIDownloadMonthly': ('monthly', int),
        'UIFavoriteTotal':   ('favorites', int),
        'UICompatibility':   ('api', str),
        'UIDir':             ('directories', to_list),
        'UIIMG_Thumbs':      ('thumbnails', to_list),
        'UIIMGs':            ('images', to_list),
        'UIDonationLink':    ('donate', str),
    }

    catlist_rename = {
        'UICATID':        ('id', int),
        'UICATTitle':     ('title', str),
        'UICATICON':      ('icon', str),
        'UICATFileCount': ('addon_count', int),
        'UICATParentIDs': ('parent_ids', functools.partial(map, int)),
    }

    def __init__(self, config: configparser.ConfigParser) -> None:
        super().__init__(config)

    @functools.cached_property
    def globalconf(self) -> dict:
        return self._load(self.pages['globalconf'], {})

    @functools.cached_property
    def gameconf(self) -> dict:
        return self._load(self.pages['gameconf'], {})

    @functools.cached_property
    def filelist(self, id_: int) -> list[str]:
        return self._load(self.pages['filelist'].format(id=id_), {}).get('FileList', [])

    @functools.cached_property
    def filedetails(self, id_: int) -> dict:
        return self._load(self.pages['filedetails'].format(id=id_), {})

    @functools.cached_property
    def addons(self) -> dict[int, AddonInfo]:
        data = {}
        for addon in self._load(self.pages['filelist'], []):
            infos = {new: typ(addon[old]) for old, (new, typ) in self.fileinfo_rename.items()}
            data[infos['id']] = AddonInfo(infos['id'], infos)
        return data

    @functools.cached_property
    def categories(self) -> dict[int, dict]:
        categories = {}
        for cat in self._load(self.pages['catlist'], []):
            categories[int(cat['UICATID'])] = {new: typ(cat[old]) for old, (new, typ) in self.catlist_rename.items()}

        for cat in categories.values():
            cat['parent_ids'] = [intval for intval in cat['parent_ids'] if intval in categories]

        return categories

    def cat_name_hierarchy(self, start: int) -> list[str]:
        """ Go up parent category ids (if any) and return list of names """
        cat_list = [start]
        cat_names = []
        while cat_list:
            id_ = cat_list.pop(0)
            try:
                cat = self.cat(id_)
            except KeyError:
                continue  # Upstream references fictional categories, e.g. 0, 23
            cat_names.append(cat['title'])
            cat_list.extend(id_ for id_ in cat['parent_ids'] if id_ != 0)
        return cat_names[::-1]
