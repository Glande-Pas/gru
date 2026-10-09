from __future__ import annotations

import io
import configparser
import datetime
import importlib
import pathlib
import pkgutil
import sys
import typing
import zipfile

import pytest
import requests

import gru
import gru.app as app_mod
from gru.api import API
from gru.install import Folder
from gru.addon import AddonInfo, InstalledAddon


# ---------------------------------------------------------------------------
# Safety net: no test may ever touch the real ~/.config/gru or ~/.cache/gru
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_real_user_config(tmp_path, monkeypatch):
    """Every test gets user_config()/user_cache() redirected under tmp_path. This used to patch a
    fixed list of modules (gru.config/cli/install) -- when gru.app was added with its own `from
    .config import user_config`, it wasn't on that list, and a test run before anyone noticed wrote
    real rows into the user's actual changes.csv. So instead: import every gru.* submodule (in case
    a future one isn't already imported by some test file) and patch whichever of user_config/
    user_cache each one actually has -- no per-module list to remember to update ever again.
    A test's own fixture can still layer a more specific fake on top -- whichever monkeypatch.setattr()
    call runs last wins, and fixtures run after autouse ones."""
    for info in pkgutil.walk_packages(gru.__path__, prefix='gru.'):
        importlib.import_module(info.name)

    def make_fake(base: pathlib.Path) -> typing.Callable[..., pathlib.Path]:
        def fake(*parts: str) -> pathlib.Path:
            path = base.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        return fake

    fake_config = make_fake(tmp_path / '_autouse_fake_config')
    fake_cache = make_fake(tmp_path / '_autouse_fake_cache')
    for name, mod in list(sys.modules.items()):
        if mod is not None and (name == 'gru' or name.startswith('gru.')):
            if hasattr(mod, 'user_config'):
                monkeypatch.setattr(mod, 'user_config', fake_config)
            if hasattr(mod, 'user_cache'):
                monkeypatch.setattr(mod, 'user_cache', fake_cache)


# ---------------------------------------------------------------------------
# Zip helpers
# ---------------------------------------------------------------------------

def make_zip(entries: dict[str, str], name: str = 'test.zip') -> zipfile.ZipFile:
    """Build an in-memory ZipFile from a {filename: content} dict."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for fname, content in entries.items():
            zf.writestr(fname, content)
    buf.seek(0)
    zf = zipfile.ZipFile(buf)
    zf.filename = name
    return zf


MANIFEST = '## Title: {title}\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n'


# ---------------------------------------------------------------------------
# API stub
# ---------------------------------------------------------------------------

class StubAddon:
    """Minimal stand-in for AddonInfo."""
    def __init__(self, id_: int, dir_: str):
        self.id = id_
        self.dir = dir_


class StubAPI:
    """Minimal stand-in for API — only implements dir() and search()."""
    def __init__(self, addons: dict[str, StubAddon | AddonInfo] | None = None):
        self._addons = addons or {}

    def search(self, term: str) -> list:
        return []

    def prune_cache(self) -> None:
        pass

    def cat_name_hierarchy(self, start: int) -> list:
        return []

    # Any: tests override this to return a real AddonInfo, not just a StubAddon
    def dir(self, name: str, link: str | None = None) -> typing.Any:
        try:
            return self._addons[name]
        except KeyError:
            raise FileNotFoundError(name)


# ---------------------------------------------------------------------------
# Folder fixture
# ---------------------------------------------------------------------------

def make_folder(root: pathlib.Path) -> Folder:
    config = configparser.ConfigParser()
    config.add_section('ESO.addons')
    config.set('ESO.addons', 'root', str(root))
    config.add_section('ESO.links')
    config.set('ESO.links', 'download', 'https://example.com/dl?id={id}/')
    return Folder('ESO', config)


@pytest.fixture
def addon_root(tmp_path):
    root = tmp_path / 'AddOns'
    root.mkdir()
    return root


@pytest.fixture
def folder(addon_root):
    return make_folder(addon_root)


@pytest.fixture
def stub_api():
    return StubAPI()


# ---------------------------------------------------------------------------
# Real addon object builders (AddonInfo / InstalledAddon)
# ---------------------------------------------------------------------------

def make_addon_info(id_: int = 1, title: str = 'MyAddon', directories: list[str] | None = None,
                    **overrides) -> AddonInfo:
    """Build a real AddonInfo with sane defaults."""
    metadata = {
        'author': 'Test Author',
        'version': '1.0',
        'api': [{'version': '5.3.5', 'name': 'Harrowstorm'}],
        'title': title,
        'directories': directories if directories is not None else [title],
        'category': 1,
        'date': datetime.datetime(2024, 1, 1),
        'link': f'https://www.esoui.com/downloads/info{id_}.html',
        'downloads': 0,
        'monthly': 0,
        'favorites': 0,
        'thumbnails': [],
        'images': [],
        'donate': '',
    }
    metadata.update(overrides)
    return AddonInfo(id_, metadata)


def write_manifest(root: pathlib.Path, dir_name: str, ext: str = '.txt', **fields) -> pathlib.Path:
    """Write a minimal manifest for `dir_name` under `root`. Pass a field as None to omit it."""
    addon_dir = root / dir_name
    addon_dir.mkdir(parents=True, exist_ok=True)

    values = {'Title': dir_name, 'APIVersion': '100035', 'Version': '1.0', 'Author': 'Test'}
    values.update(fields)

    lines = [f'## {key}: {value}' for key, value in values.items() if value is not None]
    (addon_dir / f'{dir_name}{ext}').write_text('\n'.join(lines) + '\n')
    return addon_dir


def make_installed(root: pathlib.Path, dir_name: str, **fields) -> InstalledAddon:
    """Write a manifest and return the resulting InstalledAddon."""
    addon_dir = write_manifest(root, dir_name, **fields)
    return InstalledAddon(addon_dir)


def make_api(addons: dict | None = None, categories: dict | None = None, session: typing.Any = None):
    """Bare API instance, .addons/.categories set directly -- bypasses __init__/network.
    previous_versions() defaults to no archived versions known; override it for tests that care."""
    api = API.__new__(API)
    api.game, api.version, api.pages = 'ESO', 3, {}
    api.addons = addons or {}
    api.categories = categories or {}
    api.previous_versions = lambda id_: []  # pyright: ignore[reportAttributeAccessIssue]
    if session is not None:
        api.session = session  # pyright: ignore[reportAttributeAccessIssue] -- test double, not a real CachedSession
    return api


def as_api(stub: object) -> API:
    """Pass a test double where the type checker expects a real gru.api.API."""
    return typing.cast('API', stub)


def as_folder(stub: object) -> Folder:
    """Pass a test double where the type checker expects a real gru.install.Folder."""
    return typing.cast('Folder', stub)


class FakeCrcResponse:
    """Test double for a HEAD response (content-length) or a Range-GET response (content)."""
    def __init__(self, content: bytes = b'', headers: dict | None = None, status_code: int = 206):
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} error')


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def mock_remote_zip(monkeypatch, entries: dict[str, bytes]) -> bytes:
    """ Serve `entries` as a real in-memory zip through gru.app's HEAD and gru.remotezip's
    Range-GET calls. Returns the built zip's raw bytes. """
    zip_bytes = _build_zip(entries)

    monkeypatch.setattr(app_mod.requests, 'head',
                        lambda url, allow_redirects=True: FakeCrcResponse(
                            headers={'content-length': str(len(zip_bytes))}))

    def fake_get(url, headers, allow_redirects=True):
        start, end = (int(n) for n in headers['Range'].removeprefix('bytes=').split('-'))
        return FakeCrcResponse(content=zip_bytes[start:end + 1])
    monkeypatch.setattr('gru.remotezip.requests.get', fake_get)
    return zip_bytes


def mock_remote_zip_capturing_urls(monkeypatch, entries: dict[str, bytes]) -> tuple[bytes, list[str]]:
    """ Same as mock_remote_zip(), but also records every URL fetched -- for asserting *which*
    zip was reached. """
    zip_bytes = _build_zip(entries)
    requested: list[str] = []

    def fake_head(url, allow_redirects=True):
        requested.append(url)
        return FakeCrcResponse(headers={'content-length': str(len(zip_bytes))})
    monkeypatch.setattr(app_mod.requests, 'head', fake_head)

    def fake_get(url, headers, allow_redirects=True):
        requested.append(url)
        start, end = (int(n) for n in headers['Range'].removeprefix('bytes=').split('-'))
        return FakeCrcResponse(content=zip_bytes[start:end + 1])
    monkeypatch.setattr('gru.remotezip.requests.get', fake_get)
    return zip_bytes, requested


class FakeSession:
    """Test double for a requests(_cache).Session -- exposes .head()/.get() as bound methods, so
    a test can prove code actually routes through *this* object (as API.zip_session would be
    passed in production) rather than the module-level `requests` calls."""
    def __init__(self, entries: dict[str, bytes]):
        self.zip_bytes = _build_zip(entries)
        self.calls: list[str] = []

    def head(self, url, allow_redirects=True):
        self.calls.append(f'HEAD {url}')
        return FakeCrcResponse(headers={'content-length': str(len(self.zip_bytes))})

    def get(self, url, headers, allow_redirects=True):
        self.calls.append(f'GET {url} {headers["Range"]}')
        start, end = (int(n) for n in headers['Range'].removeprefix('bytes=').split('-'))
        return FakeCrcResponse(content=self.zip_bytes[start:end + 1])


@pytest.fixture
def isolated_user_dirs(tmp_path, monkeypatch):
    """Redirect user_cache()/user_config() into tmp_path."""
    import gru.config as config_mod

    cache_dir = tmp_path / 'cache'
    config_dir = tmp_path / 'config'
    config_file = config_dir / config_mod.CONFIG_FILENAME
    monkeypatch.setattr(config_mod, 'user_cache', lambda *args: cache_dir.joinpath(*args))
    monkeypatch.setattr(config_mod, 'user_config', lambda *args: config_dir.joinpath(*args))
    return {'cache_dir': cache_dir, 'config_dir': config_dir, 'config_file': config_file}
