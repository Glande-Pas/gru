import requests
import requests_cache
import datetime
import operator
import difflib
import functools
import unicodedata
import re

from .installed import Folder


class API:
    endpoint = 'https://api.mmoui.com/v{version}/'
    version = 3

    session = requests_cache.CachedSession('gru', expire_after=datetime.timedelta(hours=1))

    pages = dict(
        globalconf='globalconfig.json',
        gameconf='game/{game}/gameconfig.json',
        catlist='game/{game}/categorylist.json',
        filelist='game/{game}/filelist.json',
    )

    invalid_chars = re.compile(r'[^\w-]')

    @classmethod
    def slugify(cls, value):
        value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
        return cls.invalid_chars.sub('', value).strip('_-')

    def __init__(self, game='ESO', stable=True):
        self.options = dict(version=self.version + int(not stable), game=game)

    @functools.cached_property
    def globalconf(self):
        return self._load('globalconf')

    @functools.cached_property
    def gameconf(self):
        return self._load('gameconf')

    @functools.cached_property
    def filelist(self):
        data = {}
        for addon in self._load('filelist'):
            dirs = set(addon['UIDir']) - {'__MACOSX'}
            if len(dirs) != 1 or {'lang', 'libs', 'EsoUI', 'gamedata'} & dirs:
                addon['slug'] = self.slugify(addon['UIName'])
            else:
                addon['slug'] = addon['UIDir'][0]
            data[int(addon['UID'])] = addon
        return data

    @functools.cached_property
    def catlist(self):
        return {int(cat['UICATID']): cat for cat in self._load('catlist')}

    def _load(self, page):
        """ Load a page and return the JSON, ensure we use cached page if <1h old """
        url = f'{self.endpoint}{self.pages[page]}'.format(**self.options)
        try:
            response = self.session.get(url)
            response.raise_for_status()
        except requests.HTTPError as http_err:
            print(f'HTTP error while loading {url!r}: {http_err}')
        except Exception as err:
            print(f'Error loading {url!r}: {err}')
        else:
            return response.json()


    def cat_name_hierarchy(self, start):
        """ Go up parent category ids (if any) and return list of names """
        cat_list = [int(start)]
        cat_names = []
        while cat_list:
            id_ = cat_list.pop(0)
            try:
                cat = self.cat(id_)
            except KeyError:
                continue  # Upstream references fictional categories, e.g. 0, 23
            cat_names.append(cat['UICATTitle'])
            cat_list.extend(int(id_) for id_ in cat['UICATParentIDs'] if id_ != '0')
        return cat_names[::-1]

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
        matcher = difflib.SequenceMatcher(str.isspace, ''.join(term.lower().split()), None)

        for obj in source:
            matcher.set_seq2(obj[attr].lower())
            matches = [match.size for match in matcher.get_matching_blocks()]
            # For debug log:
            #print(obj[attr], [repr(term[b.a:b.a + b.size]) for b in matcher.get_matching_blocks() if b.size])
            if sum(matches) < cutoff:
                continue
            # NB. cast for numerical attributes represented as strings in json
            prio = (sum(matches), max(matches), *(float(obj[tie]) for tie in tiebreakattr))
            candidates.append((prio, obj))

        candidates = sorted(candidates, key=operator.itemgetter(0), reverse=True)
        return [obj for prio, obj in candidates[:maxlen]]

    def _lookup(self, source, attr, value):
        """ Search with exact match """
        for obj in source:
            if value in obj[attr] if isinstance(obj[attr], list) else obj[attr] == value:
                return obj
        else:
            raise ValueError(f'{attr} {value!r} not found in list')

    def search(self, term, maxlen=10):
        """ Search `term` in addon names """
        # We want at least 75% of search string in result
        return self._fuzz(self.filelist.values(), 'UIName', term, cutoff=.75, maxlen=maxlen, tiebreakattr=[
            'UIDownloadTotal'  # Could be 'UIDownloadMonthly', 'UIFavoriteTotal'
        ])

    def addon(self, id_):
        """ Lookup an addon by id """
        return self.filelist[id_]

    def cat(self, id_):
        """ Lookup a category by id """
        return self.catlist[id_]

    def dir(self, dir_):
        """ Lookup an addon by directory """
        return self._lookup(self.filelist.values(), 'slug', str(dir_))

    def name(self, name):
        """ Lookup an addon by name (exact match) """
        return self._lookup(self.filelist.values(), 'UIName', str(name))

    def find(self, val, installed, local_only=False):
        """ Search for an addon generically """
        id_ = None
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

        results = self.search(val)
        if local_only:
            results = [addon for addon in results if Folder.find_installed(addon['slug'], installed) is not None]

        return results
