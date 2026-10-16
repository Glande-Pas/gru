import json

import pytest
import requests

from gru import apiversions
from gru.apiversions import ApiVersions

from .conftest import make_folder, make_installed

DATA = [
    {'id': '12.1.0', 'name': 'Season One', 'interface': '101051', 'default': True},
    {'id': '12.0.0', 'name': 'Season Zero Pt2', 'interface': '101050', 'default': False},
    {'bogus': 1},
]


class Response:
    def __init__(self, status=200, data=None, etag='"abc"'):
        self.status_code, self._data, self.headers = status, data, {'ETag': etag} if etag else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._data


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'api-versions.json'
    monkeypatch.setattr(apiversions, 'user_cache', lambda *args: path)
    return path


@pytest.fixture
def calls(monkeypatch):
    log = []

    class FakeGet:
        response = Response(data=DATA)

        def __call__(self, url, headers=None, timeout=None):
            log.append(headers)
            return self.response

    fake_get = FakeGet()
    monkeypatch.setattr(apiversions.requests, 'get', fake_get)
    return fake_get, log


class TestParse:
    def test_names_and_current(self):
        versions = apiversions.parse(DATA)

        assert versions.name(101051) == 'Season One'
        assert versions.name(1) is None
        assert versions.current() == 101051

    def test_malformed_input(self):
        assert apiversions.parse({'not': 'a list'}).versions == {}
        assert apiversions.parse([{'interface': 'x', 'name': 'y'}]).versions == {}
        assert ApiVersions().current() is None


class TestLoad:
    def test_first_fetch_is_cached_with_its_etag(self, cache, calls):
        fake_get, log = calls

        versions = apiversions.load(now=1000)

        assert versions is not None and versions.current() == 101051
        assert log == [{}]
        stored = json.loads(cache.read_text())
        assert stored['etag'] == '"abc"' and stored['checked'] == 1000

    def test_no_request_while_fresh(self, cache, calls):
        fake_get, log = calls
        apiversions.load(now=1000)

        apiversions.load(now=1000 + apiversions.CHECK_EVERY - 1)

        assert len(log) == 1

    def test_revalidates_with_the_etag_and_keeps_the_cache_on_304(self, cache, calls):
        fake_get, log = calls
        apiversions.load(now=1000)
        fake_get.response = Response(status=304, data=None, etag='')

        versions = apiversions.load(now=1000 + apiversions.CHECK_EVERY)

        assert log[-1] == {'If-None-Match': '"abc"'}
        assert versions is not None and versions.current() == 101051
        assert json.loads(cache.read_text())['checked'] == 1000 + apiversions.CHECK_EVERY

    def test_offline_falls_back_to_the_cached_copy(self, cache, calls, monkeypatch):
        fake_get, log = calls
        apiversions.load(now=1000)

        def offline(*args, **kwargs):
            raise requests.ConnectionError('offline')
        monkeypatch.setattr(apiversions.requests, 'get', offline)

        versions = apiversions.load(now=1000 + apiversions.CHECK_EVERY)

        assert versions is not None and versions.name(101050) == 'Season Zero Pt2'

    def test_does_not_retry_right_after_a_failure(self, cache, monkeypatch):
        attempts = []

        def offline(*args, **kwargs):
            attempts.append(1)
            raise requests.ConnectionError('offline')
        monkeypatch.setattr(apiversions.requests, 'get', offline)

        apiversions.load(now=1000)
        apiversions.load(now=1000 + apiversions.RETRY_AFTER - 1)
        apiversions.load(now=1000 + apiversions.RETRY_AFTER)

        assert len(attempts) == 2

    def test_offline_without_a_cache(self, cache, monkeypatch):
        def offline(*args, **kwargs):
            raise requests.ConnectionError('offline')
        monkeypatch.setattr(apiversions.requests, 'get', offline)

        assert apiversions.load() is None


class TestGameVersionFallback:
    def test_live_uses_the_current_version_without_a_settings_file(self, tmp_path):
        root = tmp_path / 'AddOns'
        root.mkdir()
        addon = make_installed(root, 'Azurah', APIVersion='101052')
        folder = make_folder(root)
        folder.scan()
        folder.api_versions = apiversions.parse(DATA)

        assert folder.game_api_version() == 101051
        assert folder.api_vs_game(addon) == 'newer'

    def test_pts_does_not_borrow_the_live_version(self, tmp_path):
        root = tmp_path / 'AddOns'
        root.mkdir()
        folder = make_folder(root)
        folder.target = 'pts'
        folder.scan()
        folder.api_versions = apiversions.parse(DATA)

        assert folder.game_api_version() is None

    def test_settings_file_wins(self, tmp_path):
        root = tmp_path / 'live' / 'AddOns'
        root.mkdir(parents=True)
        (root.parent / 'AddOnSettings.txt').write_text('#Version 101049\n')
        folder = make_folder(root)
        folder.scan()
        folder.api_versions = apiversions.parse(DATA)

        assert folder.game_api_version() == 101049
