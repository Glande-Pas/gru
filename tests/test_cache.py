import os
import time

import gru.cache as cache_mod
from gru.cache import download_name, prune_downloads, trim_history

from .conftest import make_addon_info, make_installed

DAY = 86400


def _touch(path, days: float, size: int = 10) -> None:
    path.write_bytes(b'x' * size)
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def _dl(tmp_path, monkeypatch):
    dl = tmp_path / 'dl'
    dl.mkdir()
    monkeypatch.setattr(cache_mod, 'user_cache', lambda *parts: dl.joinpath(*parts[1:]) if parts else dl)
    return dl


def _linked(root, dir_, version, id_=7):
    addon = make_installed(root, dir_, Version=version)
    addon.link(make_addon_info(id_=id_, title=dir_, version=version, directories=[dir_]))
    return addon


class TestDownloadName:
    def test_is_deterministic_and_filesystem_safe(self):
        assert download_name(7, 'Foo', '1.0') == '7-Foo-1.0.zip'
        assert '/' not in download_name(7, 'a/b', '../1')

    def test_url_variant_differs(self):
        assert download_name(7, 'Foo', '1.0', 'http://x/a.zip') != download_name(7, 'Foo', '1.0')


class TestPruneDownloads:
    def test_installed_release_kept_whatever_its_age(self, addon_root, tmp_path, monkeypatch):
        dl = _dl(tmp_path, monkeypatch)
        foo = _linked(addon_root, 'Foo', '2.0')
        _touch(dl / download_name(7, 'Foo', '2.0'), 365)
        assert prune_downloads([foo]) == []

    def test_superseded_and_unrelated_zips_age_out(self, addon_root, tmp_path, monkeypatch):
        dl = _dl(tmp_path, monkeypatch)
        foo = _linked(addon_root, 'Foo', '2.0')
        _touch(dl / download_name(7, 'Foo', '1.0'), 8)
        _touch(dl / 'legacy_server_name.zip', 8)
        _touch(dl / download_name(7, 'Foo', '1.5'), 1)
        assert {p.name for p in prune_downloads([foo])} == {download_name(7, 'Foo', '1.0'), 'legacy_server_name.zip'}
        assert (dl / download_name(7, 'Foo', '1.5')).exists()

    def test_unlinked_addon_pins_nothing(self, addon_root, tmp_path, monkeypatch):
        dl = _dl(tmp_path, monkeypatch)
        foo = make_installed(addon_root, 'Foo', Version='2.0')
        _touch(dl / download_name(7, 'Foo', '2.0'), 30)
        assert len(prune_downloads([foo])) == 1

    def test_override_version_pins_listing_zip(self, addon_root, tmp_path, monkeypatch):
        dl = _dl(tmp_path, monkeypatch)
        foo = make_installed(addon_root, 'Foo', Version='1.0')
        foo.link(make_addon_info(id_=7, title='Foo', version='1.0.8', directories=['Foo']))
        foo.record_override('1.0.8')
        _touch(dl / download_name(7, 'Foo', '1.0.8'), 30)
        assert prune_downloads([foo]) == []

    def test_size_cap_drops_oldest_unpinned_first(self, addon_root, tmp_path, monkeypatch):
        dl = _dl(tmp_path, monkeypatch)
        foo = _linked(addon_root, 'Foo', '2.0')
        _touch(dl / download_name(7, 'Foo', '2.0'), 100, size=100)
        _touch(dl / download_name(7, 'Foo', '1.1'), 3, size=100)
        _touch(dl / download_name(7, 'Foo', '1.2'), 1, size=100)
        assert [p.name for p in prune_downloads([foo], max_bytes=100)] == [download_name(7, 'Foo', '1.1')]
        assert (dl / download_name(7, 'Foo', '2.0')).exists() and (dl / download_name(7, 'Foo', '1.2')).exists()


class TestTrimHistory:
    def _entries(self, n):
        return ''.join(f'\n# 2026-01-01 00:00:{i:02}\n+cmd{i}\n' for i in range(n))

    def test_keeps_last_entries(self, tmp_path):
        path = tmp_path / 'history'
        path.write_text(self._entries(10))
        trim_history(path, max_entries=3)
        assert path.read_text().count('+cmd') == 3
        assert '+cmd9' in path.read_text() and '+cmd6' not in path.read_text()

    def test_short_or_missing_history_untouched(self, tmp_path):
        path = tmp_path / 'history'
        trim_history(path, max_entries=3)
        assert not path.exists()
        path.write_text(self._entries(3))
        before = path.read_text()
        trim_history(path, max_entries=3)
        assert path.read_text() == before
