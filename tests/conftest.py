import io
import configparser
import pathlib
import zipfile

import pytest

from gru.install import Folder


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
    """Minimal stand-in for API — only implements dir()."""
    def __init__(self, addons: dict[str, StubAddon] | None = None):
        self._addons = addons or {}

    def dir(self, name: str) -> StubAddon:
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
