import importlib.metadata

import pytest

import gru.channel as channel_mod
from gru.channel import Channel, detect


class FakeDist:
    def __init__(self, **files):
        self.files = files

    def read_text(self, name):
        return self.files.get(name)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(channel_mod.ENV_OVERRIDE, raising=False)
    monkeypatch.delenv('FLATPAK_ID', raising=False)
    monkeypatch.setattr(channel_mod, '_is_flatpak', lambda: False)
    monkeypatch.setattr(channel_mod, '_is_msstore', lambda: False)
    detect.cache_clear()
    yield
    detect.cache_clear()


def _with_dist(monkeypatch, dist):
    def distribution(name):
        if dist is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return dist
    monkeypatch.setattr(channel_mod.importlib.metadata, 'distribution', distribution)


class TestDetect:
    @pytest.mark.parametrize('installer', ['pip', 'uv', 'PDM\n'])
    def test_index_installers_are_pypi(self, monkeypatch, installer):
        _with_dist(monkeypatch, FakeDist(INSTALLER=installer))
        assert detect() is Channel.PYPI

    @pytest.mark.parametrize('installer', ['rpm', 'dpkg', 'installer', 'conda'])
    def test_other_installers_are_distro(self, monkeypatch, installer):
        _with_dist(monkeypatch, FakeDist(INSTALLER=installer))
        assert detect() is Channel.DISTRO

    def test_direct_url_is_git(self, monkeypatch):
        _with_dist(monkeypatch, FakeDist(INSTALLER='pip', **{'direct_url.json': '{}'}))
        assert detect() is Channel.GIT

    def test_not_installed_or_no_installer_falls_back_to_git(self, monkeypatch):
        _with_dist(monkeypatch, None)
        assert detect() is Channel.GIT
        detect.cache_clear()
        _with_dist(monkeypatch, FakeDist())
        assert detect() is Channel.GIT

    def test_broken_metadata_falls_back_to_git(self, monkeypatch):
        def boom(name):
            raise OSError('unreadable')
        monkeypatch.setattr(channel_mod.importlib.metadata, 'distribution', boom)
        assert detect() is Channel.GIT

    def test_flatpak_and_msstore_win_over_metadata(self, monkeypatch):
        _with_dist(monkeypatch, FakeDist(INSTALLER='pip'))
        monkeypatch.setattr(channel_mod, '_is_flatpak', lambda: True)
        assert detect() is Channel.FLATPAK
        detect.cache_clear()
        monkeypatch.setattr(channel_mod, '_is_flatpak', lambda: False)
        monkeypatch.setattr(channel_mod, '_is_msstore', lambda: True)
        assert detect() is Channel.MSSTORE

    def test_env_override(self, monkeypatch):
        _with_dist(monkeypatch, FakeDist(INSTALLER='pip'))
        monkeypatch.setenv(channel_mod.ENV_OVERRIDE, 'Distro')
        assert detect() is Channel.DISTRO

    def test_invalid_override_is_ignored(self, monkeypatch):
        _with_dist(monkeypatch, FakeDist(INSTALLER='pip'))
        monkeypatch.setenv(channel_mod.ENV_OVERRIDE, 'bogus')
        assert detect() is Channel.PYPI


def test_flatpak_detection_uses_env(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv('FLATPAK_ID', 'org.example.gru')
    assert channel_mod._is_flatpak()


def test_msstore_requires_windows_apps_path(monkeypatch):
    monkeypatch.setattr(channel_mod.os, 'name', 'posix')
    assert not channel_mod._is_msstore()
