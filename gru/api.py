""" Module handling fetching info from the API """
import requests
import requests_cache
import datetime
import operator
import difflib
import warnings
import functools

from .addon import APIAddonInfo


def to_list(arg):
    if arg is None:
        return []
    else:
        return list(arg)


def epoch_ms(val):
    return datetime.datetime.fromtimestamp(int(val) / 1000)


def _exception_root_cause(err):
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


class API:
    session = requests_cache.CachedSession('gru', expire_after=datetime.timedelta(hours=1))

    def _load(self, url, fallback=None):
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
    def reset(cls):
        """ Clear the cache """
        requests_cache.clear()

    def _fuzz(self, source, attr, term, cutoff, maxlen, tiebreakattr=[]):
        """ Fuzzy search that prioritises maximal subset matches, then longest match, then by tie breakers.

        Matches `term` in the `attr` attribute within the `source` iterable, returning at most `maxlen` items.
        `tiebreakattr` is a list of numerical attributes to act as tie breakers. Search ignores case and any whitespace.

        Not using SequenceMatcher.ratio() as that compares full strings and we want sub-strings to match.
        """
        cutoff *= len(term)
        candidates = []
        matcher = difflib.SequenceMatcher(str.isspace, term.lower(), None)

        for addon in source:
            matcher.set_seq2(addon.metadata[attr].lower())
            matches = [match.size for match in matcher.get_matching_blocks()]
            # For debug log:
            #print(addon[attr], [repr(term[b.a:b.a + b.size]) for b in matcher.get_matching_blocks() if b.size])
            if sum(matches) < cutoff:
                continue
            # NB. cast for numerical attributes represented as strings in json
            prio = (sum(matches), max(matches), *(addon.metadata[tie] for tie in tiebreakattr))
            candidates.append((prio, addon))

        candidates = sorted(candidates, key=operator.itemgetter(0), reverse=True)
        return [addon for prio, addon in candidates[:maxlen]]

    def _lookup(self, source, attr, match):
        """ Search with exact match """
        for addon in source:
            value = addon.metadata[attr]
            if match in value if isinstance(value, list) else value == match:
                return addon
        else:
            raise ValueError(f'{attr} {value!r} not found in list')

    def search(self, term, tiebreakattr=None, maxlen=30):
        """ Search `term` in addon names """
        # We want at least 75% of search string in result
        if tiebreakattr is None:
            tiebreakattr = 'downloads'
        return self._fuzz(self.addons.values(), 'title', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                          tiebreakattr=[tiebreakattr])

    def addon(self, id_):
        """ Lookup an addon by id """
        return self.addons[id_]

    def cat(self, id_):
        """ Lookup a category by id """
        return self.categories[id_]

    def dir(self, dir_):
        """ Lookup an addon by directory """
        for addon in self.addons.values():
            if addon.dir == dir_:
                return addon
        else:
            raise ValueError(f'Directory {dir_!r} not found in list')

    def name(self, name):
        """ Lookup an addon by name (exact match) """
        return self._lookup(self.addons.values(), 'title', str(name))

    def find(self, val, local):
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
        except ValueError:
            pass

        try:
            idx = [folder.dir for folder in local.installed].index(val)
        except ValueError:
            pass
        else:
            return local.installed[idx]

        # Otherwise revert to search and return a list of candidates
        return self.search(val)

    @classmethod
    def _factory(cls, config, game, stable=True):
        version = config.getint('api', 'version') + int(not stable)
        if game == 'ESO' and version == 3:
            return ESOUIv3(config)
        raise NotImplementedError(f'API version {version} for {game} not implemented')

    @classmethod
    def live(cls, config):
        return cls._factory(config, 'ESO', True)

    @classmethod
    def alpha(cls, config):
        return cls._factory(config, 'ESO', False)


class ESOUIv3(API):

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
        'UICompatibility':   ('api_versions', str),
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

    def __init__(self, config):
        super().__init__()
        endpoint = config.get('api', 'endpoint')
        self.pages = {key: endpoint.format(version=3, path=val) for key, val in config.items('ESOUIv3.paths')}

    @functools.cached_property
    def globalconf(self):
        return self._load(self.pages['globalconf'], {})

    @functools.cached_property
    def gameconf(self):
        return self._load(self.pages['gameconf'], {})

    @functools.cached_property
    def addons(self):
        data = {}
        for addon in self._load(self.pages['filelist'], []):
            infos = {new: typ(addon[old]) for old, (new, typ) in self.fileinfo_rename.items()}
            data[infos['id']] = APIAddonInfo(infos['id'], infos)
        return data

    @functools.cached_property
    def categories(self):
        categories = {}
        for cat in self._load(self.pages['catlist'], []):
            categories[int(cat['UICATID'])] = {new: typ(cat[old]) for old, (new, typ) in self.catlist_rename.items()}

        for cat in categories.values():
            cat['parent_ids'] = [intval for intval in cat['parent_ids'] if intval in categories]

        return categories

    def cat_name_hierarchy(self, start):
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
