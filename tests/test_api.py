"""Tests for gru.api. Network access (_load/session) uses a stubbed session, never
requests_cache.CachedSession."""

import datetime
import inspect

import pytest
import requests

from gru.api import to_list, case_insensitive, epoch_ms, _exception_root_cause, _fuzz, _filter, _lookup, API

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
# API pure lookup methods (no network -- addons/categories set directly)
# ---------------------------------------------------------------------------

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
