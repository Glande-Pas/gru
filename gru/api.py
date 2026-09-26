""" Module handling fetching info from the API """

from __future__ import annotations

import requests
from requests.structures import CaseInsensitiveDict
import requests_cache
import datetime
import operator
import difflib
import warnings
import functools
import collections
import collections.abc
import configparser
import html.parser
import re
import typing
from typing import TypeVar, NamedTuple
from collections.abc import Iterable, Iterator, Mapping

from . import config as gruconfig
from .addon import AddonInfo, DisplayAddonProtocol

if typing.TYPE_CHECKING:
    import gru.addon
    import gru.install


def to_list(arg: Iterable | None) -> list:
    if arg is None:
        return []
    else:
        return list(arg)


def case_insensitive(mapping: Mapping) -> CaseInsensitiveDict:
    return CaseInsensitiveDict({
        key: case_insensitive(val) if isinstance(val, collections.abc.Mapping) else val for key, val in mapping.items()
    })


def epoch_ms(val: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(val) / 1000)


def _exception_root_cause(err: BaseException) -> str:
    while True:
        if err.__cause__ is not None:
            err = err.__cause__  # standard python exception chaining
        elif isinstance(err.args[0], Exception):
            err = err.args[0]   # requests exception chaining
        elif (reason := getattr(err, 'reason', None)) is not None:
            err = reason  # urllib3 (requests' backend) exception chaining
        else:
            break

    import urllib3
    if isinstance(err, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.PoolError)):
        # Both error messages are <object>: cause
        return str(err).split(': ', 1)[-1]

    return str(err)


T = TypeVar('T', bound=DisplayAddonProtocol)


def _fuzz(source: Iterable[T], attr: str, term: str, cutoff: float, maxlen: int,
          tiebreakattr: list[str] = []) -> list[T]:
    """ Fuzzy search that prioritises maximal subset matches, then longest match, then by tie breakers.

    Matches `term` in the `attr` attribute within the `source` iterable, returning at most `maxlen` items.
    `tiebreakattr` is a list of numerical attributes to act as tie breakers. Search ignores case and any whitespace.

    Not using SequenceMatcher.ratio() as that compares full strings and we want sub-strings to match.
    """
    cutoff *= len(term)
    candidates = []
    matcher = difflib.SequenceMatcher(str.isspace, term.lower(), '')

    for addon in source:
        matcher.set_seq2(getattr(addon, attr).lower())
        matches = [match.size for match in matcher.get_matching_blocks()]
        # For debug log:
        # print(addon[attr], [repr(term[b.a:b.a + b.size]) for b in matcher.get_matching_blocks() if b.size])
        if sum(matches) < cutoff:
            continue
        # NB. cast for numerical attributes represented as strings in json
        prio = (sum(matches), max(matches), *(addon.metadata.get(tie) for tie in tiebreakattr))
        candidates.append((prio, addon))

    candidates = sorted(candidates, key=operator.itemgetter(0), reverse=True)
    return [addon for prio, addon in candidates[:maxlen]]


def _filter(source: Iterable[T], attr: str, match: str | int | None) -> Iterator[T]:
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


def _lookup(source: Iterable[T], attr: str, match: str | int | None) -> T:
    """ Search with exact match (lowercased) """
    try:
        return next(_filter(source, attr, match))
    except StopIteration:
        # TODO: type of error?
        raise ValueError(f'{attr} {match!r} not found in list')


class PreviousVersion(NamedTuple):
    version: str
    size: str
    uploader: str | None
    date: str
    download_url: str
    aid: int


def _extract_aid(href: str) -> int | None:
    match = re.search(r'[?&]aid=(\d+)', href)
    return int(match.group(1)) if match else None


def _extract_info_id(link: str) -> int | None:
    match = re.search(r'/info(?P<id>[0-9]+)-[^/]*\.html', link)
    return int(match.group('id')) if match else None


class _ArchivedFilesParser(html.parser.HTMLParser):
    """ Extracts the "Archived Files" table from an esoui.com addon info page -- the download link for a previous
    version's `aid` isn't exposed anywhere in the JSON API.

    div#other_t holds several tables, told apart only by a preceding heading div. Row/cell boundaries are matched by
    literal tag name (<table>/<tr>/<td> are always closed in practice).

    One row per <tr>, one slot per <td> (kept even if empty, so a blank cell can never shift
    the following cells out of position): [file link href, version, size, uploader, date]. """

    HEADING = 'Archived Files'

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str | None]] = []
        self._reading_heading = False
        self._found_heading = False
        self._in_table = False
        self._done = False
        self._in_row = False
        self._in_cell = False
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        attrs_ = dict(attrs)

        if tag == 'div' and 'title' in (attrs_.get('class') or '').split():
            self._reading_heading = True
        elif tag == 'table' and self._found_heading and not self._in_table:
            self._in_table = True
        elif tag == 'tr' and self._in_table:
            self._in_row = True
            self.rows.append([])
        elif tag == 'td' and self._in_row:
            self._in_cell = True
            self.rows[-1].append(None)
        elif tag == 'a' and self._in_cell and not self._in_link:
            self._in_link = True
            self.rows[-1][-1] = attrs_.get('href')

    def handle_endtag(self, tag: str) -> None:
        if tag == 'table' and self._in_table:
            self._in_table = False
            self._done = self._found_heading  # got the table we wanted, ignore the rest
        elif tag == 'tr':
            self._in_row = False
        elif tag == 'td':
            self._in_cell = False
        elif tag == 'a':
            self._in_link = False

    def handle_data(self, data: str) -> None:
        if self._reading_heading:
            self._reading_heading = False
            if data.strip().startswith(self.HEADING):
                self._found_heading = True
        elif self._in_cell and not self._in_link and self.rows[-1][-1] is None:
            text = data.strip()
            if text:
                self.rows[-1][-1] = text


class AmbiguousDirectory(FileNotFoundError):
    """ Several online addons share a directory and no `link` resolves which one. Subclass of
    FileNotFoundError so existing handling still catches it. """

    def __init__(self, dir_: str, candidates: list[AddonInfo]) -> None:
        super().__init__(f'Directory {dir_!r} matches {len(candidates)} different online addons')
        self.dir = dir_
        self.candidates = candidates


class API:
    # Provided by subclasses (ESOUIv3/ESOUIv4): game/version as class attributes,
    # addons/categories as cached_property. TYPE_CHECKING-only so it documents the type for
    # static analysis without creating a real descriptor that would block plain instance
    # assignment (both cached_property's own storage and tests that set .addons directly).
    if typing.TYPE_CHECKING:
        game: str
        version: int
        addons: dict[int, AddonInfo]
        categories: dict[int, dict]
        globalconf: Mapping

    def __init__(self, config: configparser.ConfigParser) -> None:
        self.pages = {}
        endpoint = config.get('api', 'endpoint')
        gamepaths = config.items(f'{self.game}UIv{self.version}.paths')
        self.pages = {key: endpoint.format(version=self.version, game=self.game, path=path) for key, path in gamepaths}
        self.info_url_template: str = config.get(f'{self.game}.links', 'info')
        self.session = requests_cache.CachedSession(gruconfig.user_cache('api'),
                                                    expire_after=datetime.timedelta(hours=1))
        # Separate cache: needs match_headers (vary by Range) and allowable_codes (cache 206s).
        self.zip_session = requests_cache.CachedSession(gruconfig.user_cache('zips'),
                                                        expire_after=datetime.timedelta(hours=1),
                                                        allowable_codes=(200, 206), match_headers=True)

    def _load(self, url: str, fallback: typing.Any = None) -> typing.Any:
        """ Load a page and return the JSON, ensure we use cached page if <1h old """
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except requests.JSONDecodeError:
            warnings.warn(f'JSON decode error while loading data from {url!r}')
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else 'unknown'
            warnings.warn(f'HTTP error while loading {url!r} status code {status}: {err}')
        except (requests.ConnectionError, requests.Timeout) as err:
            # Get back up to root cause for readability
            msg = _exception_root_cause(err)
            warnings.warn(f'Connection error while loading {url!r}: {msg}')
        except requests.RequestException as err:
            warnings.warn(f'Error loading {url!r}: {err}')
        return fallback

    def _load_html(self, url: str) -> str:
        """ Load a page and return its decoded text. Decoding follows the server's declared
        charset (requests reads it from the Content-Type header into response.encoding), so
        this must not hardcode an encoding of its own. """
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as err:
            status = err.response.status_code if err.response is not None else 'unknown'
            warnings.warn(f'HTTP error while loading {url!r} status code {status}: {err}')
        except (requests.ConnectionError, requests.Timeout) as err:
            msg = _exception_root_cause(err)
            warnings.warn(f'Connection error while loading {url!r}: {msg}')
        except requests.RequestException as err:
            warnings.warn(f'Error loading {url!r}: {err}')
        return ''

    def previous_versions(self, id_: int) -> list[PreviousVersion]:
        """ Archived (previously released) versions of an addon, scraped from its info page --
        the download link for a specific old version (its `aid`) isn't exposed by the JSON API. """
        parser = _ArchivedFilesParser()
        parser.feed(self._load_html(self.info_url_template.format(id=id_)))

        versions = []
        for row in parser.rows:
            if len(row) != 5:
                warnings.warn(f'Unexpected archived-files row shape for addon {id_}: {row!r}')
                continue
            href, version, size, uploader, date = row
            if href is None or (aid := _extract_aid(href)) is None:
                continue  # not a data row (e.g. the header) or missing its download link
            versions.append(PreviousVersion(version or '', size or '', uploader, date or '', href, aid))
        return versions

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

    def dir(self, dir_: str, link: str | None = None) -> AddonInfo:
        """ Lookup an addon by directory. `link` (from addons.csv) disambiguates via the id in its
        URL when several addons share a dir; an unresolved tie raises AmbiguousDirectory. """
        exact = []
        partial = []
        for addon in self.addons.values():
            if addon.dir == dir_:
                exact.append(addon)
            elif dir_ in addon.metadata['directories']:
                partial.append(addon)

        id_ = _extract_info_id(link) if link is not None else None

        if len(exact) > 1 and id_ is not None:
            matches = [addon for addon in exact if addon.id == id_]
            if len(matches) == 1:
                return matches[0]
        if len(exact) == 1:
            return exact[0]
        if exact:
            raise AmbiguousDirectory(dir_, exact)

        if len(partial) > 1 and id_ is not None:
            matches = [addon for addon in partial if addon.id == id_]
            if len(matches) == 1:
                return matches[0]
        if len(partial) == 1:
            return partial[0]
        if partial:
            raise AmbiguousDirectory(dir_, partial)

        raise FileNotFoundError(f'Directory {dir_!r} not found in list')

    def name(self, name: str) -> AddonInfo:
        """ Lookup an addon by name (exact match) """
        return _lookup(self.addons.values(), 'title', str(name).lower())

    def find(self, val: str, local: gru.install.Folder) -> list[AddonInfo | gru.addon.InstalledAddon]:
        """ Search for an addon generically. Always returns a list (0, 1, or N matches). """
        # Various methods of exact matches
        try:
            return [self.addon(int(val))]
        except (ValueError, KeyError):
            pass

        try:
            return [self.name(val)]
        except ValueError:
            pass

        try:
            return [self.dir(val)]
        except FileNotFoundError:
            pass

        # Find by dir but locally, not from API
        for addon in local.installed:
            if addon.dir == val:
                return [addon]

        # Otherwise revert to search and return a list of candidates
        return list(self.search(val))

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

    # cached_property stores as a plain attribute after first access, hence the ignore below
    @functools.cached_property
    def globalconf(self) -> CaseInsensitiveDict:  # pyright: ignore[reportIncompatibleVariableOverride]
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
        'UICompatibility':   ('api', to_list),
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
    def globalconf(self) -> dict:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._load(self.pages['globalconf'], {})

    @functools.cached_property
    def gameconf(self) -> dict:
        return self._load(self.pages['gameconf'], {})

    def filelist(self, id_: int) -> list[str]:
        data = self._load(self.pages['listfiles'].format(id=id_), [])
        return data[0].get('FileList', []) if data else []

    def filedetails(self, id_: int) -> dict:
        return self._load(self.pages['filedetails'].format(id=id_), {})

    @functools.cached_property
    def addons(self) -> dict[int, AddonInfo]:  # pyright: ignore[reportIncompatibleVariableOverride]
        data = {}
        for addon in self._load(self.pages['filelist'], []):
            infos = {new: typ(addon[old]) for old, (new, typ) in self.fileinfo_rename.items()}
            data[infos['id']] = AddonInfo(infos['id'], infos)
        return data

    @functools.cached_property
    def categories(self) -> dict[int, dict]:  # pyright: ignore[reportIncompatibleVariableOverride]
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
