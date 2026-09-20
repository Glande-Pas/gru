"""Tests for gru.api. Network access (_load/session) uses a stubbed session, never
requests_cache.CachedSession."""

import datetime
import inspect

import pytest
import requests

from gru.api import to_list, case_insensitive, epoch_ms, _exception_root_cause, _fuzz, _filter, _lookup, API, ESOUIv3
from gru.addon import AddonInfo

from .conftest import make_addon_info, make_api


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

class TestToList:
    def test_none_becomes_empty_list(self):
        assert to_list(None) == []

    def test_iterable_becomes_list(self):
        assert to_list((1, 2, 3)) == [1, 2, 3]


class TestCaseInsensitive:
    def test_top_level_case_insensitive(self):
        d = case_insensitive({'Foo': 1})
        assert d['foo'] == 1
        assert d['FOO'] == 1

    def test_nested_dicts_also_case_insensitive(self):
        d = case_insensitive({'API': {'Version': 'LIVE'}})
        assert d['api']['version'] == 'LIVE'


class TestEpochMs:
    def test_converts_milliseconds(self):
        # 2021-01-01T00:00:00Z in ms
        dt = epoch_ms(1609459200000)
        assert dt.year == 2021 and dt.month == 1 and dt.day == 1


class TestExceptionRootCause:
    def test_plain_exception(self):
        assert _exception_root_cause(ValueError('boom')) == 'boom'

    def test_dunder_cause_chain_followed(self):
        try:
            try:
                raise ValueError('root')
            except ValueError as inner:
                raise RuntimeError('wrapper') from inner
        except RuntimeError as err:
            assert _exception_root_cause(err) == 'root'

    def test_requests_style_args0_chain_followed(self):
        inner = ValueError('deep cause')
        outer = RuntimeError(inner)
        assert _exception_root_cause(outer) == 'deep cause'


# ---------------------------------------------------------------------------
# _fuzz
# ---------------------------------------------------------------------------

class TestFuzz:
    def _addons(self, *titles):
        return [make_addon_info(id_=n, title=t) for n, t in enumerate(titles, 1)]

    def test_substring_match(self):
        source = self._addons('LibAddonMenu-2.0', 'LibChatMessage', 'Unrelated')
        result = _fuzz(source, 'title', 'AddonMenu', cutoff=0.75, maxlen=10)
        assert [a.title for a in result] == ['LibAddonMenu-2.0']

    def test_maxlen_truncates(self):
        source = self._addons('FooBar1', 'FooBar2', 'FooBar3')
        result = _fuzz(source, 'title', 'FooBar', cutoff=0.5, maxlen=2)
        assert len(result) == 2

    def test_cutoff_excludes_weak_matches(self):
        source = self._addons('CompletelyDifferent')
        result = _fuzz(source, 'title', 'FooBar', cutoff=0.75, maxlen=10)
        assert result == []

    def test_tiebreak_orders_by_attribute(self):
        low = make_addon_info(id_=1, title='FooBar', downloads=10)
        high = make_addon_info(id_=2, title='FooBar', downloads=999)
        result = _fuzz([low, high], 'title', 'FooBar', cutoff=0.5, maxlen=10, tiebreakattr=['downloads'])
        assert [a.id for a in result] == [2, 1]


# ---------------------------------------------------------------------------
# _filter / _lookup -- the exact machinery behind the earlier `gru rm` bug
# ---------------------------------------------------------------------------

class TestFilter:
    def test_returns_a_generator_not_a_list(self):
        result = _filter([], 'title', 'x')
        assert inspect.isgenerator(result)
        assert bool(result) is True  # empty generator is still truthy
        assert list(result) == []

    def test_string_match_is_case_insensitive(self):
        addons = [make_addon_info(title='LibAddonMenu-2.0')]
        assert list(_filter(addons, 'title', 'libaddonmenu-2.0')) == addons

    def test_string_match_requires_exact_equality_not_substring(self):
        addons = [make_addon_info(title='LibAddonMenu-2.0')]
        assert list(_filter(addons, 'title', 'libaddonmenu')) == []

    def test_list_attribute_membership_case_insensitive(self):
        """No real caller has a list attribute (AddonInfo keeps 'directories' in .metadata),
        so this uses a minimal stand-in object instead."""
        class Stub:
            tags = ['Foo', 'BarBaz']
        stub = Stub()
        assert list(_filter([stub], 'tags', 'barbaz')) == [stub]
        assert list(_filter([stub], 'tags', 'nope')) == []

    def test_scalar_match_uses_plain_equality(self):
        addon = make_addon_info(id_=42)
        assert list(_filter([addon], 'id', 42)) == [addon]
        assert list(_filter([addon], 'id', 43)) == []

    def test_none_match_only_equals_none(self):
        addon = make_addon_info(id_=None)
        assert list(_filter([addon], 'id', None)) == [addon]


class TestLookup:
    def test_finds_exact_match(self):
        addon = make_addon_info(title='MyAddon')
        assert _lookup([addon], 'title', 'myaddon') is addon

    def test_empty_source_raises_valueerror_not_unboundlocalerror(self):
        """Regression: used to raise UnboundLocalError instead."""
        with pytest.raises(ValueError, match='myaddon'):
            _lookup([], 'title', 'myaddon')

    def test_no_match_raises_valueerror(self):
        addon = make_addon_info(title='Other')
        with pytest.raises(ValueError, match='myaddon'):
            _lookup([addon], 'title', 'myaddon')


# ---------------------------------------------------------------------------
# fileinfo_rename -> AddonInfo, against real esoui.com filelist.json data
# ---------------------------------------------------------------------------

class TestFileinfoRenamePipeline:
    # Real entry from `jq -c '.[] | select(.UIDir[0] == "BRHelper")' filelist.json`, trimmed
    # of the thumbnail/preview image arrays (irrelevant noise for this test).
    RAW_BRHELPER = {
        'UID': '2181', 'UICATID': '25', 'UIVersion': '1.0.6', 'UIDate': 1582625544000,
        'UIName': 'Blackrose Prison Helper', 'UIAuthorName': 'andy.s',
        'UIFileInfoURL': 'https://www.esoui.com/downloads/info2181-BlackrosePrisonHelper.html',
        'UIDownloadTotal': '163801', 'UIDownloadMonthly': '380', 'UIFavoriteTotal': '99',
        'UICompatibility': [{'version': '5.3.5', 'name': 'Harrowstorm'}],
        'UIDir': ['BRHelper'], 'UIIMG_Thumbs': [], 'UIIMGs': [], 'UIDonationLink': None,
    }

    def test_real_entry_builds_a_valid_addoninfo(self):
        """Regression guard: UICompatibility is a list of {version, name} dicts in the real
        API, not a space-separated version string -- this used to crash AddonInfo.__init__
        (list has no .split()) after crashing differently (garbled repr) before that."""
        infos = {new: typ(self.RAW_BRHELPER[old]) for old, (new, typ) in ESOUIv3.fileinfo_rename.items()}
        addon = AddonInfo(infos['id'], infos)

        assert addon.id == 2181
        assert addon.title == 'Blackrose Prison Helper'
        assert addon.dir == 'BRHelper'
        assert addon.api == [{'version': '5.3.5', 'name': 'Harrowstorm'}]

    # Real entry with UICompatibility: null -- true for 113 of the entries in a full
    # filelist.json snapshot, so any full `addons` scan (e.g. via `gru update`) hits this.
    RAW_NULL_COMPATIBILITY = {
        'UID': '21', 'UICATID': '33', 'UIVersion': '1.0.7', 'UIDate': 1415337794000,
        'UIName': 'ZAM Stats Exp', 'UIAuthorName': 'Seerah',
        'UIFileInfoURL': 'https://www.esoui.com/downloads/info21-ZAMStatsExp.html',
        'UIDownloadTotal': '14491', 'UIDownloadMonthly': '6', 'UIFavoriteTotal': '13',
        'UICompatibility': None,
        'UIDir': ['ZAM_StatsExp'], 'UIIMG_Thumbs': [], 'UIIMGs': [], 'UIDonationLink': None,
    }

    def test_null_compatibility_does_not_crash(self):
        """Regression guard: UICompatibility is null for ~113 real catalog entries.
        list[dict[str, str]](None) raises TypeError ('NoneType' object is not iterable);
        to_list(None) correctly degrades to []."""
        infos = {new: typ(self.RAW_NULL_COMPATIBILITY[old]) for old, (new, typ) in ESOUIv3.fileinfo_rename.items()}
        addon = AddonInfo(infos['id'], infos)

        assert addon.id == 21
        assert addon.api == []


class TestApiLookups:
    def test_addon_by_id(self):
        target = make_addon_info(id_=7, title='Target')
        api = make_api(addons={7: target})
        assert api.addon(7) is target

    def test_addon_missing_id_raises_keyerror(self):
        api = make_api()
        with pytest.raises(KeyError):
            api.addon(999)

    def test_cat(self):
        api = make_api(categories={1: {'title': 'Combat'}})
        assert api.cat(1) == {'title': 'Combat'}

    def test_dir_exact_match(self):
        addon = make_addon_info(id_=1, title='MyAddon', directories=['MyAddon'])
        api = make_api(addons={1: addon})
        assert api.dir('MyAddon') is addon

    def test_dir_partial_secondary_directory_unique(self):
        addon = make_addon_info(id_=1, title='Bundle', directories=['Bundle', 'BundleLib'])
        addon.metadata['directories'] = ['Bundle', 'BundleLib']  # keep both for this test
        api = make_api(addons={1: addon})
        assert api.dir('BundleLib') is addon

    def test_dir_ambiguous_secondary_directory_raises(self):
        a = make_addon_info(id_=1, title='A')
        a.metadata['directories'] = ['A', 'Shared']
        b = make_addon_info(id_=2, title='B')
        b.metadata['directories'] = ['B', 'Shared']
        api = make_api(addons={1: a, 2: b})
        with pytest.raises(FileNotFoundError):
            api.dir('Shared')

    def test_name_case_insensitive(self):
        addon = make_addon_info(title='LibAddonMenu-2.0')
        api = make_api(addons={1: addon})
        assert api.name('libaddonmenu-2.0') is addon

    def test_dir_picks_first_when_several_listings_claim_the_same_dir(self):
        """Real esoui.com data: three separate listings (base addon, a JP translation, and
        a third-party patch) all declare UIDir == ["BRHelper"], from
        `jq -c '.[] | select(.UIDir[0] == "BRHelper")' filelist.json`. Since each has a
        single directory, AddonInfo.dir is 'BRHelper' for all three, and API.dir() has no
        tie-break beyond dict/JSON order -- it just returns whichever it reaches first."""
        base = make_addon_info(id_=2181, title='Blackrose Prison Helper', author='andy.s',
                                directories=['BRHelper'], downloads=163801, favorites=99)
        jp_version = make_addon_info(id_=2996, title='Blackrose Prison Helper JP Version', author='tdenc',
                                      directories=['BRHelper'], downloads=17045, favorites=3)
        patch = make_addon_info(id_=4252, title='Blackrose Prison Helper (Patch)', author='sshogrin',
                                 directories=['BRHelper'], downloads=941, favorites=4)

        # Same order filelist.json lists them in (ascending UID / release order)
        api = make_api(addons={2181: base, 2996: jp_version, 4252: patch})
        assert api.dir('BRHelper') is base

        # Confirm it's genuinely iteration-order-dependent, not id- or popularity-based
        api_reordered = make_api(addons={4252: patch, 2181: base, 2996: jp_version})
        assert api_reordered.dir('BRHelper') is patch


class TestApiFind:
    """API.find() always returns a list (0, 1, or N matches)."""

    def test_find_by_numeric_id(self):
        addon = make_addon_info(id_=5, title='MyAddon')
        api = make_api(addons={5: addon})

        class Local:
            installed = []
        assert api.find('5', Local()) == [addon]

    def test_find_by_exact_name(self):
        addon = make_addon_info(id_=1, title='MyAddon')
        api = make_api(addons={1: addon})

        class Local:
            installed = []
        assert api.find('myaddon', Local()) == [addon]

    def test_find_by_local_installed_dir_not_in_api(self):
        api = make_api()

        class FakeInstalled:
            dir = 'LocalOnly'

        class Local:
            installed = [FakeInstalled()]

        result = api.find('LocalOnly', Local())
        assert result == Local.installed

    def test_find_falls_back_to_fuzzy_search(self):
        addon = make_addon_info(id_=1, title='SomewhatLongName')
        api = make_api(addons={1: addon})

        class Local:
            installed = []

        result = api.find('SomewhatLong', Local())
        assert addon in result

    def test_find_no_match_anywhere_returns_empty_list(self):
        api = make_api()

        class Local:
            installed = []

        assert api.find('nope', Local()) == []


# ---------------------------------------------------------------------------
# API._load -- network layer, exercised via a stubbed session (never the real one)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, json_data=None, status_code=200, json_error=False):
        self._json_data = json_data
        self.status_code = status_code
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(response=self)
            raise err

    def json(self):
        if self._json_error:
            raise requests.JSONDecodeError('bad json', '', 0)
        return self._json_data


class FakeSession:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def get(self, url):
        if self._exc is not None:
            raise self._exc
        return self._response


class TestApiLoad:
    def test_successful_load_returns_json(self):
        api = make_api()
        api.session = FakeSession(response=FakeResponse(json_data={'ok': True}))
        assert api._load('http://x') == {'ok': True}

    def test_json_decode_error_returns_fallback_and_warns(self):
        api = make_api()
        api.session = FakeSession(response=FakeResponse(json_error=True))
        with pytest.warns(UserWarning, match='JSON decode error'):
            assert api._load('http://x', fallback=[]) == []

    def test_http_error_returns_fallback_and_warns(self):
        api = make_api()
        api.session = FakeSession(response=FakeResponse(status_code=404))
        with pytest.warns(UserWarning, match='HTTP error'):
            assert api._load('http://x', fallback={}) == {}

    def test_http_error_with_no_response_object_does_not_crash(self):
        """Regression guard: requests.HTTPError.response can genuinely be None (e.g. when
        raised manually rather than via response.raise_for_status()); accessing
        err.response.status_code unconditionally would raise AttributeError."""
        class NoResponseSession:
            def get(self, url):
                raise requests.HTTPError('boom')  # no response= given -> err.response is None
        api = make_api()
        api.session = NoResponseSession()
        with pytest.warns(UserWarning, match='HTTP error'):
            assert api._load('http://x', fallback={}) == {}

    def test_connection_error_returns_fallback_and_warns(self):
        api = make_api()
        api.session = FakeSession(exc=requests.ConnectionError('refused'))
        with pytest.warns(UserWarning, match='Connection error'):
            assert api._load('http://x', fallback=None) is None

    def test_fallback_defaults_to_none(self):
        api = make_api()
        api.session = FakeSession(exc=requests.ConnectionError('refused'))
        with pytest.warns(UserWarning):
            assert api._load('http://x') is None
