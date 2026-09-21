from __future__ import annotations

import io
import configparser
import datetime
import pathlib
import typing
import zipfile

import pytest

from gru.api import API
from gru.install import Folder
from gru.addon import AddonInfo, InstalledAddon


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
    def __init__(self, addons: dict[str, StubAddon] | None = None):
        self._addons = addons or {}

    def search(self, term: str) -> list:
        return []

    def cat_name_hierarchy(self, start: int) -> list:
        return []

    # Any: tests override this to return a real AddonInfo, not just a StubAddon
    def dir(self, name: str) -> typing.Any:
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
    """Bare API instance, .addons/.categories set directly -- bypasses __init__/network."""
    api = API.__new__(API)
    api.game, api.version, api.pages = 'ESO', 3, {}
    api.addons = addons or {}
    api.categories = categories or {}
    if session is not None:
        api.session = session  # pyright: ignore[reportAttributeAccessIssue] -- test double, not a real CachedSession
    return api


def as_api(stub: object) -> API:
    """Pass a test double where the type checker expects a real gru.api.API."""
    return typing.cast('API', stub)


def as_folder(stub: object) -> Folder:
    """Pass a test double where the type checker expects a real gru.install.Folder."""
    return typing.cast('Folder', stub)


@pytest.fixture
def isolated_user_dirs(tmp_path, monkeypatch):
    """Redirect user_cache()/user_config() into tmp_path."""
    import gru.config as config_mod

    cache_dir = tmp_path / 'cache'
    config_file = tmp_path / 'config' / 'gru.ini'
    monkeypatch.setattr(config_mod, 'user_cache', lambda *args: cache_dir.joinpath(*args))
    monkeypatch.setattr(config_mod, 'user_config', lambda: config_file)
    return {'cache_dir': cache_dir, 'config_file': config_file}
