"""Tests for gru.app: state persistence and orchestration shared by any front-end (CLI, GUI, ...)
on top of gru.api/gru.install -- see gru.cli for the thin command-dispatch layer built on this."""

import configparser
import csv
import pathlib

import pytest
import requests

import gru.app as app_mod
from gru.api import AmbiguousDirectory, PreviousVersion
from gru.addon import AddonBundle

from .conftest import (
    make_installed, make_addon_info, make_api, make_folder, mock_remote_zip, mock_remote_zip_capturing_urls, StubAPI,
    FakeSession, FakeCrcResponse, _build_zip,
)


def _patch_user_config(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(app_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))


def _touch_config_path(base: pathlib.Path, *parts: str) -> pathlib.Path:
    path = (base / 'config').joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestAddonsRootConfigured:
    def _config(self, root: str) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.add_section('ESO.addons')
        config.set('ESO.addons', 'root', root)
        return config

    def test_existing_root_is_configured(self, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        assert app_mod.addons_root_configured(self._config(str(addons_root)), 'ESO') is True

    def test_empty_root_is_not_configured(self, tmp_path):
        assert app_mod.addons_root_configured(self._config(''), 'ESO') is False

    def test_nonexistent_root_is_not_configured(self, tmp_path):
        assert app_mod.addons_root_configured(self._config(str(tmp_path / 'nope')), 'ESO') is False


class TestAppendChangeLog:
    """append_change_log() keeps at most max_lines data rows, oldest evicted first."""

    def test_writes_header_and_row(self, tmp_path):
        path = tmp_path / 'changes.csv'
        app_mod.append_change_log(path, ['MyAddon', '1.0', 'link', 'date', 'none'], 100)

        rows = list(csv.reader(path.open()))
        assert rows[0] == ['dir', 'version', 'link', 'date', 'previous_state']
        assert rows[1] == ['MyAddon', '1.0', 'link', 'date', 'none']

    def test_rotates_out_oldest_row_beyond_max_lines(self, tmp_path):
        path = tmp_path / 'changes.csv'
        for i in range(5):
            app_mod.append_change_log(path, [f'Addon{i}', '1.0', '', 'date', 'none'], 3)

        rows = list(csv.reader(path.open()))
        assert [row[0] for row in rows[1:]] == ['Addon2', 'Addon3', 'Addon4']

    def test_zero_max_lines_disables_logging(self, tmp_path):
        path = tmp_path / 'changes.csv'
        app_mod.append_change_log(path, ['MyAddon', '1.0', '', 'date', 'none'], 0)

        rows = list(csv.reader(path.open()))
        assert rows == [['dir', 'version', 'link', 'date', 'previous_state']]


class TestLogChanges:
    """log_changes() diffs before/after install state and appends one row per addon whose
    version differs -- NOT_INSTALLED is the sentinel for a fresh install or a removal."""

    def _config(self) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.add_section('ESO.addons')
        config.set('ESO.addons', 'log_lines', '100')
        return config

    def _changes(self, tmp_path: pathlib.Path) -> list[list[str]]:
        with (tmp_path / 'config' / 'ESO' / 'changes.csv').open() as f:
            return list(csv.reader(f))

    def test_new_install_logs_sentinel_as_previous_state(self, addon_root, folder, monkeypatch, tmp_path):
        _patch_user_config(monkeypatch, tmp_path)
        before = folder.snapshot()

        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}

        app_mod.log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', '1.0', upstream.metadata['link']]
        assert rows[1][4] == app_mod.NOT_INSTALLED

    def test_removal_logs_sentinel_as_new_version(self, addon_root, folder, monkeypatch, tmp_path):
        _patch_user_config(monkeypatch, tmp_path)
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}
        before = folder.snapshot()

        folder._installed = {}

        app_mod.log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', app_mod.NOT_INSTALLED, upstream.metadata['link']]
        assert rows[1][4] == '1.0'

    def test_update_logs_both_real_versions(self, addon_root, folder, monkeypatch, tmp_path):
        _patch_user_config(monkeypatch, tmp_path)
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}
        before = folder.snapshot()

        installed.version = '2.0'

        app_mod.log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', '2.0', upstream.metadata['link']]
        assert rows[1][4] == '1.0'

    def test_no_change_writes_nothing(self, addon_root, folder, monkeypatch, tmp_path):
        _patch_user_config(monkeypatch, tmp_path)
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        folder._installed = {installed.folder: installed}
        before = folder.snapshot()

        app_mod.log_changes(folder, self._config(), before)

        assert not (tmp_path / 'config' / 'ESO' / 'changes.csv').exists()


class TestReadChanges:
    """read_changes() parses changes.csv back into ChangeEntry rows, oldest first."""

    def _write(self, monkeypatch, tmp_path, folder, rows: list[list[str]]) -> None:
        _patch_user_config(monkeypatch, tmp_path)
        path = _touch_config_path(tmp_path, folder.game, 'changes.csv')
        for row in rows:
            app_mod.append_change_log(path, row, 100)

    def test_no_changes_file_returns_empty_list(self, folder, monkeypatch, tmp_path):
        _patch_user_config(monkeypatch, tmp_path)
        assert app_mod.read_changes(folder) == []

    def test_reads_rows_in_file_order(self, folder, monkeypatch, tmp_path):
        self._write(monkeypatch, tmp_path, folder, [
            ['MyAddon', '2.0', 'link1', 'date1', '1.0'],
            ['NewAddon', '1.0', 'link2', 'date2', app_mod.NOT_INSTALLED],
        ])

        changes = app_mod.read_changes(folder)

        assert changes == [
            app_mod.ChangeEntry('MyAddon', '2.0', 'link1', 'date1', '1.0'),
            app_mod.ChangeEntry('NewAddon', '1.0', 'link2', 'date2', app_mod.NOT_INSTALLED),
        ]

    def test_limit_keeps_only_the_most_recent_rows(self, folder, monkeypatch, tmp_path):
        self._write(monkeypatch, tmp_path, folder, [
            ['Addon1', '1.0', '', 'date1', app_mod.NOT_INSTALLED],
            ['Addon2', '1.0', '', 'date2', app_mod.NOT_INSTALLED],
            ['Addon3', '1.0', '', 'date3', app_mod.NOT_INSTALLED],
        ])

        changes = app_mod.read_changes(folder, limit=2)

        assert [c.dir for c in changes] == ['Addon2', 'Addon3']


class TestBuildApp:
    def test_returns_live_api_and_scanned_folder(self, addon_root, monkeypatch, tmp_path):
        """A thin smoke test -- build_app() itself is just API.live() + Folder(...).scan(),
        already covered individually; this only checks it wires them together correctly."""
        import gru.install as install_mod

        make_installed(addon_root, 'MyAddon')
        config = configparser.ConfigParser()
        config.add_section('ESO.addons')
        config.set('ESO.addons', 'root', str(addon_root))
        config.add_section('ESO.links')
        config.set('ESO.links', 'download', 'https://example.com/dl?id={id}')

        stub_api = StubAPI()
        monkeypatch.setattr(app_mod.API, 'live', staticmethod(lambda cfg: stub_api))
        _patch_user_config(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        api, local = app_mod.build_app('ESO', config)

        assert api is stub_api
        assert {a.dir for a in local.installed} == {'MyAddon'}

    def test_does_not_resolve_exact_matches_while_scanning(self, addon_root, monkeypatch, tmp_path):
        """build_app() deliberately does NOT call resolve_exact_matches() -- an addon that stays
        ambiguous would otherwise get re-checked (network calls included) on every single scan
        forever, since there's nothing to persist and thus nothing for a future scan to skip.
        Front-ends call resolve_exact_matches() themselves from whichever commands need it
        (see TestUpdateCommand/TestMatchCommand in test_cli.py)."""
        import gru.install as install_mod

        make_installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        config = configparser.ConfigParser()
        config.add_section('ESO.addons')
        config.set('ESO.addons', 'root', str(addon_root))
        config.add_section('ESO.links')
        config.set('ESO.links', 'download', 'https://example.com/dl?id={id}')

        other = make_addon_info(id_=1, title='Other', directories=['BRHelper'])
        exact = make_addon_info(id_=2, title='Exact', directories=['BRHelper'])
        files = {1: ['BRHelper/BRHelper.txt'], 2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua']}

        class AmbiguousApi(StubAPI):
            def dir(self, name, link=None):  # pyright: ignore[reportIncompatibleMethodOverride]
                if name == 'BRHelper':
                    raise AmbiguousDirectory(name, [other, exact])
                raise FileNotFoundError(name)

            def filelist(self, id_):
                return files[id_]

        stub_api = AmbiguousApi()
        monkeypatch.setattr(app_mod.API, 'live', staticmethod(lambda cfg: stub_api))
        _patch_user_config(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        api, local = app_mod.build_app('ESO', config)

        installed = next(a for a in local.installed if a.dir == 'BRHelper')
        assert installed.infos is None
        assert not _touch_config_path(tmp_path, 'ESO', 'addons.csv').exists()


class TestFindAmbiguous:
    def test_finds_addon_ambiguous_between_several_listings(self, addon_root, folder):
        make_installed(addon_root, 'BRHelper')
        base = make_addon_info(id_=2181, title='Base', directories=['BRHelper'])
        jp = make_addon_info(id_=2996, title='JP', directories=['BRHelper'])
        api = make_api(addons={2181: base, 2996: jp})
        folder.scan(api)

        [(installed, candidates)] = app_mod.find_ambiguous(folder, api)
        assert installed.dir == 'BRHelper'
        assert set(candidates) == {base, jp}

    def test_plain_unmatched_addon_is_not_ambiguous(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        api = make_api()  # empty -- MyAddon not found at all, not ambiguous
        folder.scan(api)
        assert app_mod.find_ambiguous(folder, api) == []

    def test_already_matched_addon_is_not_ambiguous(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        upstream = make_addon_info(id_=1, title='MyAddon', directories=['MyAddon'])
        api = make_api(addons={1: upstream})
        folder.scan(api)
        assert app_mod.find_ambiguous(folder, api) == []


class TestFindAmbiguousBundles:
    def test_finds_bundle_ambiguous_between_several_listings(self, addon_root, folder):
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        one = make_addon_info(id_=1, title='Bundle One', directories=['Bundle'])
        two = make_addon_info(id_=2, title='Bundle Two', directories=['Bundle'])
        api = make_api(addons={1: one, 2: two})
        folder.scan(api)

        [(bundle, candidates)] = app_mod.find_ambiguous_bundles(folder, api)
        assert bundle.dir == 'Bundle'
        assert {m.dir for m in bundle.members} == {'BundleExtra1', 'BundleExtra2'}
        assert set(candidates) == {one, two}

    def test_already_matched_bundle_is_not_ambiguous(self, addon_root, folder):
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        upstream = make_addon_info(id_=1, title='Bundle', directories=['Bundle'])
        api = make_api(addons={1: upstream})
        folder.scan(api)
        assert app_mod.find_ambiguous_bundles(folder, api) == []


def _mock_remote_zips_by_id(monkeypatch, zips_by_id: dict[int, bytes]) -> None:
    """ Like mock_remote_zip(), but serves different zip content per candidate id -- needed to
    tell candidates apart by what their own (fake) archive actually contains. """
    def fake_head(url, allow_redirects=True):
        id_ = next(id_ for id_ in zips_by_id if f'id={id_}/' in url)
        return FakeCrcResponse(headers={'content-length': str(len(zips_by_id[id_]))})
    monkeypatch.setattr(app_mod.requests, 'head', fake_head)

    def fake_get(url, headers, allow_redirects=True):
        id_ = next(id_ for id_ in zips_by_id if f'id={id_}/' in url)
        start, end = (int(n) for n in headers['Range'].removeprefix('bytes=').split('-'))
        return FakeCrcResponse(content=zips_by_id[id_][start:end + 1])
    monkeypatch.setattr('gru.remotezip.requests.get', fake_get)


class TestResolveAmbiguousBundles:
    def test_resolves_bundle_whose_zip_uniquely_contains_all_members(self, addon_root, monkeypatch):
        member1 = make_installed(addon_root / 'Bundle', 'BundleExtra1')
        member2 = make_installed(addon_root / 'Bundle', 'BundleExtra2')
        one = make_addon_info(id_=1, title='Bundle One', directories=['Bundle'])
        two = make_addon_info(id_=2, title='Bundle Two', directories=['Bundle'])
        api = make_api(addons={1: one, 2: two})
        folder = make_folder(addon_root)
        bundle = AddonBundle('Bundle', [member1, member2])
        monkeypatch.setattr(app_mod, 'find_ambiguous_bundles', lambda local, api: [(bundle, [one, two])])
        _mock_remote_zips_by_id(monkeypatch, {
            1: _build_zip({'Bundle/BundleExtra1/BundleExtra1.txt': b'1', 'Bundle/BundleExtra2/BundleExtra2.txt': b'2'}),
            2: _build_zip({'Bundle/BundleExtra1/BundleExtra1.txt': b'1'}),  # missing BundleExtra2
        })

        resolved = app_mod.resolve_ambiguous_bundles(folder, api)

        assert resolved == [bundle]
        assert bundle.infos is one
        assert member1.infos is one
        assert member2.infos is one

    def test_leaves_bundle_ambiguous_when_both_candidates_zips_contain_all_members(self, addon_root, monkeypatch):
        member1 = make_installed(addon_root / 'Bundle', 'BundleExtra1')
        member2 = make_installed(addon_root / 'Bundle', 'BundleExtra2')
        one = make_addon_info(id_=1, title='Bundle One', directories=['Bundle'])
        two = make_addon_info(id_=2, title='Bundle Two', directories=['Bundle'])
        api = make_api(addons={1: one, 2: two})
        folder = make_folder(addon_root)
        bundle = AddonBundle('Bundle', [member1, member2])
        monkeypatch.setattr(app_mod, 'find_ambiguous_bundles', lambda local, api: [(bundle, [one, two])])
        same_zip = _build_zip({'Bundle/BundleExtra1/BundleExtra1.txt': b'1', 'Bundle/BundleExtra2/BundleExtra2.txt': b'2'})
        _mock_remote_zips_by_id(monkeypatch, {1: same_zip, 2: same_zip})

        resolved = app_mod.resolve_ambiguous_bundles(folder, api)

        assert resolved == []
        assert bundle.infos is None


class TestRankCandidates:
    def _installed(self, addon_root, dir_, **fields):
        return make_installed(addon_root, dir_, **fields)

    def test_author_match_ranks_first(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        base = make_addon_info(id_=1, title='Base', author='andy.s', version='1.0')
        jp = make_addon_info(id_=2, title='JP', author='tdenc', version='1.0')
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [base, jp], api, 'downloads')
        assert ranked[0] is jp

    def test_color_coded_author_and_title_still_match(self, addon_root):
        """Addon/author names often carry ESO |cRRGGBB...|r color markup -- styling differences
        alone must not defeat an otherwise-exact title/author match."""
        installed = self._installed(addon_root, 'BRHelper', Author='|cFF0000tdenc|r', Version='9.9')
        base = make_addon_info(id_=1, title='Base', author='andy.s', version='1.0')
        jp = make_addon_info(id_=2, title='|c00FF00Blackrose Prison Helper JP|r', author='tdenc', version='1.0')
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [base, jp], api, 'downloads')
        assert ranked[0] is jp

    def test_version_match_ranks_first(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='3.5')
        base = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        matching = make_addon_info(id_=2, title='Y', author='x', version='3.5')
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [base, matching], api, 'downloads')
        assert ranked[0] is matching

    def test_archived_version_match_ranks_first(self, addon_root):
        """installed hasn't been updated to the candidate's current release, but it matches one
        of its archived ones -- rank_candidates() must search those too, not just .version."""
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='3.5')
        base = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        matching = make_addon_info(id_=2, title='Y', author='x', version='9.0')  # current != 3.5
        api = make_api()
        archives = {
            1: [PreviousVersion('0.9', '1KB', None, 'date', 'url', 1)],
            2: [PreviousVersion('3.5', '1KB', None, 'date', 'url', 2),
                PreviousVersion('9.0', '1KB', None, 'date', 'url', 3)],
        }
        api.previous_versions = lambda id_: archives[id_]  # pyright: ignore[reportAttributeAccessIssue]

        ranked = app_mod.rank_candidates(installed, [base, matching], api, 'downloads')
        assert ranked[0] is matching

    def test_previous_versions_failure_is_tolerated(self, addon_root):
        """A request failure while scraping archived versions degrades to "no match"."""
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='3.5')
        candidate = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        api = make_api()

        def boom(id_):
            raise requests.ConnectionError('network is down')
        api.previous_versions = boom  # pyright: ignore[reportAttributeAccessIssue]

        ranked = app_mod.rank_candidates(installed, [candidate], api, 'downloads')
        assert ranked == [candidate]

    def test_previous_versions_unexpected_error_propagates(self, addon_root):
        """An error outside requests.RequestException from previous_versions() propagates."""
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='3.5')
        candidate = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        api = make_api()

        def boom(id_):
            raise RuntimeError('not a network problem')
        api.previous_versions = boom  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(RuntimeError, match='not a network problem'):
            app_mod.rank_candidates(installed, [candidate], api, 'downloads')

    def test_falls_back_to_sortkey_when_metadata_ties(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='9.9')
        low = make_addon_info(id_=1, title='Base', author='x', version='1.0', downloads=10)
        high = make_addon_info(id_=2, title='Base', author='x', version='1.0', downloads=999)
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [low, high], api, 'downloads')
        assert ranked[0] is high


class TestNormalizeRemoteEntries:
    """_normalize_remote_entries() -- the shared path normalization _crc_match() builds its
    {relative_path: crc32} comparison from. Exercised directly with dummy (path, True) pairs
    rather than through find_exact_match(), since these are about path normalization, not the
    resolution policy."""

    def test_nested_path_distinguishes_from_basename_only_match(self):
        """Full relative paths, not just basenames, distinguish 'sub/util.lua' from
        'other/util.lua'."""
        local_files = {'brhelper.txt', 'sub/util.lua'}
        wrong_dir_files = app_mod._normalize_remote_entries(
            ((f, True) for f in ['BRHelper/BRHelper.txt', 'BRHelper/other/util.lua']), 'BRHelper')
        right_dir_files = app_mod._normalize_remote_entries(
            ((f, True) for f in ['BRHelper/BRHelper.txt', 'BRHelper/sub/util.lua']), 'BRHelper')

        assert set(wrong_dir_files) != local_files
        assert set(right_dir_files) == local_files

    def test_garbage_entries_are_pruned(self):
        """__MACOSX/.DS_Store junk never survives onto disk from a real install (GARBAGE, see
        _inspect_bundle()) -- it must not count against an otherwise-exact content match."""
        files = app_mod._normalize_remote_entries(
            ((f, True) for f in ['BRHelper/BRHelper.txt', 'BRHelper/__MACOSX/BRHelper.txt', 'BRHelper/.DS_Store']),
            'BRHelper')

        assert set(files) == {'brhelper.txt'}

    def test_directory_entries_are_pruned(self):
        """Zip contents list intermediate directories as their own entries (e.g. 'BRHelper/libs/')
        -- pathlib silently drops the trailing '/', which would otherwise make a directory
        indistinguishable from a same-named extensionless file. installed.files never lists
        directories either, so these must not count against an otherwise-exact match."""
        files = app_mod._normalize_remote_entries(
            ((f, True) for f in ['BRHelper/', 'BRHelper/BRHelper.txt', 'BRHelper/libs/', 'BRHelper/libs/Lib.lua']),
            'BRHelper')

        assert set(files) == {'brhelper.txt', 'libs/lib.lua'}

    def test_sibling_bundled_addon_entries_are_excluded(self):
        """A zip bundling several top-level addons -- entries belonging to a *different* bundled
        addon must not be compared against this install."""
        files = app_mod._normalize_remote_entries(
            ((f, True) for f in ['BRHelper/BRHelper.txt', 'OtherLib/OtherLib.txt', 'OtherLib/Data.lua']),
            'BRHelper')

        assert set(files) == {'brhelper.txt'}

    def test_preserves_the_value_alongside_each_normalized_path(self):
        """_crc_match() relies on values (CRC32s) surviving normalization unchanged."""
        files = app_mod._normalize_remote_entries(
            [('BRHelper/BRHelper.txt', 12345), ('BRHelper/Data.lua', 67890)], 'BRHelper')

        assert files == {'brhelper.txt': 12345, 'data.lua': 67890}


class TestFindExactMatchWithCrcVerification:
    def _installed(self, addon_root, dir_, **fields):
        return make_installed(addon_root, dir_, **fields)

    def test_metadata_pre_filter_skips_implausible_candidates_before_crc_check(self, addon_root, monkeypatch):
        """A candidate scoring below META_SCORE_THRESHOLD never gets a CRC check (a real network
        round trip) at all."""
        installed = self._installed(addon_root, 'BRHelper', Author='LocalAuthor', Version='1.0')
        candidate = make_addon_info(id_=1, title='CompletelyUnrelatedXYZ', author='OtherAuthor', version='9.9')
        api = make_api()
        api.previous_versions = lambda id_: []  # pyright: ignore[reportAttributeAccessIssue]

        def unexpected(url, allow_redirects=True):
            raise AssertionError('requests.head should not be called for a below-threshold candidate')
        monkeypatch.setattr(app_mod.requests, 'head', unexpected)
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is None

    def test_two_candidates_both_crc_match_is_still_ambiguous(self, addon_root, monkeypatch):
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        first = make_addon_info(id_=1, title='First', author='tdenc', version='9.9')
        second = make_addon_info(id_=2, title='Second', author='tdenc', version='9.9')
        api = make_api()
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        mock_remote_zip(monkeypatch, {'BRHelper/BRHelper.txt': manifest_bytes})
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [first, second], api, url_template=folder.url_template)

        assert result is None

    def test_crc_check_fetches_the_matched_archived_release_not_the_current_one(self, addon_root, monkeypatch):
        """When the metadata match came from an archived version, the CRC check must fetch that
        release's zip (via its `aid`), not the current one -- comparing an old local install
        against current content would spuriously mismatch."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='1.0.4')
        candidate = make_addon_info(id_=2181, title='BRHelper', author='tdenc', version='1.0.5')  # != installed
        api = make_api()
        archived = PreviousVersion('1.0.4', '58kB', 'andy.s', 'date',
                                   '/downloads/getfile.php?s=abc&id=2181&aid=28002', 28002)
        api.previous_versions = lambda id_: [archived]  # pyright: ignore[reportAttributeAccessIssue]
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        _zip_bytes, requested = mock_remote_zip_capturing_urls(monkeypatch, {'BRHelper/BRHelper.txt': manifest_bytes})
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is candidate
        expected_url = folder.url_template.format(id=2181) + '&aid=28002'
        assert requested and all(url == expected_url for url in requested)

    def test_crc_mismatch_does_not_resolve_even_with_good_metadata(self, addon_root, monkeypatch):
        """A definite content mismatch is a hard no now -- good metadata no longer rescues it."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        api = make_api()
        mock_remote_zip(monkeypatch, {'BRHelper/BRHelper.txt': b'completely different content'})
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is None

    def test_crc_check_failure_does_not_resolve_even_with_good_metadata(self, addon_root, monkeypatch):
        """A network failure during the CRC check is treated the same as an unverifiable
        candidate -- it doesn't crash, but it also doesn't fall back to metadata anymore."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        api = make_api()

        def boom(url, allow_redirects=True):
            raise requests.ConnectionError('network is down')
        monkeypatch.setattr(app_mod.requests, 'head', boom)
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is None

    def test_missing_content_length_header_does_not_resolve(self, addon_root, monkeypatch):
        """A HEAD response missing content-length (KeyError) is treated as unverifiable."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        api = make_api()

        class NoContentLength:
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass
        monkeypatch.setattr(app_mod.requests, 'head', lambda url, allow_redirects=True: NoContentLength())
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is None

    def test_unexpected_error_during_crc_check_propagates(self, addon_root, monkeypatch):
        """An error outside the tolerated set (request failure, KeyError, ValueError) propagates
        instead of being treated as just another unverifiable candidate."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        api = make_api()

        def boom(url, allow_redirects=True):
            raise TypeError('not a request failure')
        monkeypatch.setattr(app_mod.requests, 'head', boom)
        folder = make_folder(addon_root)

        with pytest.raises(TypeError, match='not a request failure'):
            app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

    def test_uses_apis_zip_session_when_available(self, addon_root, monkeypatch):
        """The CRC check routes through api.zip_session when available, not the bare `requests`
        module."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        api = make_api()
        fake_session = FakeSession({'BRHelper/BRHelper.txt': manifest_bytes})
        api.zip_session = fake_session  # pyright: ignore[reportAttributeAccessIssue]

        def unexpected(*a, **kw):
            raise AssertionError('should route through api.zip_session, not the bare requests module')
        monkeypatch.setattr(app_mod.requests, 'head', unexpected)
        monkeypatch.setattr('gru.remotezip.requests.get', unexpected)
        folder = make_folder(addon_root)

        result = app_mod.find_exact_match(installed, [candidate], api, url_template=folder.url_template)

        assert result is candidate
        assert fake_session.calls

    def test_no_url_template_never_resolves(self, addon_root, monkeypatch):
        """Without url_template, content can never be verified, so find_exact_match() never
        resolves anything -- and doesn't even attempt a HEAD/Range request in the process."""
        installed = self._installed(addon_root, 'BRHelper', Author='tdenc', Version='9.9')
        candidate = make_addon_info(id_=1, title='Base', author='tdenc', version='9.9')
        api = make_api()

        def unexpected(*a, **kw):
            raise AssertionError('requests.head should not be called without a url_template')
        monkeypatch.setattr(app_mod.requests, 'head', unexpected)

        result = app_mod.find_exact_match(installed, [candidate], api)

        assert result is None


class TestResolveExactMatches:
    """resolve_exact_matches() is find_ambiguous() + find_exact_match() + link(), run right after
    scanning (see build_app()) so an exact match never needs a `gru match` prompt at all."""

    def test_resolves_and_links_exact_match(self, addon_root, folder, monkeypatch):
        make_installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        # `other`'s metadata is implausible enough to be filtered before the CRC check, so it
        # doesn't matter that mock_remote_zip() serves the same content regardless of candidate.
        other = make_addon_info(id_=1, title='Other', author='Someone Else', version='0.1', directories=['BRHelper'])
        exact = make_addon_info(id_=2, title='Exact', directories=['BRHelper'])
        api = make_api(addons={1: other, 2: exact})
        mock_remote_zip(monkeypatch, {'BRHelper/BRHelper.txt': manifest_bytes, 'BRHelper/lang.lua': b'-- lang'})
        folder.scan(api)

        resolved = app_mod.resolve_exact_matches(folder, api)

        [installed] = resolved
        assert installed.dir == 'BRHelper'
        assert installed.infos is exact

    def test_stays_ambiguous_when_no_candidate_is_an_exact_match(self, addon_root, folder):
        """No CRC mocking here -- the CRC check genuinely can't reach anything (fake
        url_template), so nothing gets content-verified and both candidates stay unresolved."""
        make_installed(addon_root, 'BRHelper')
        first = make_addon_info(id_=1, title='First', directories=['BRHelper'])
        second = make_addon_info(id_=2, title='Second', directories=['BRHelper'])
        api = make_api(addons={1: first, 2: second})
        folder.scan(api)

        resolved = app_mod.resolve_exact_matches(folder, api)

        assert resolved == []
        installed = next(a for a in folder.installed if a.dir == 'BRHelper')
        assert installed.infos is None

    def test_warns_about_each_resolution(self, addon_root, folder, monkeypatch):
        make_installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        other = make_addon_info(id_=1, title='Other', author='Someone Else', version='0.1', directories=['BRHelper'])
        exact = make_addon_info(id_=2, title='Exact', directories=['BRHelper'])
        api = make_api(addons={1: other, 2: exact})
        mock_remote_zip(monkeypatch, {'BRHelper/BRHelper.txt': manifest_bytes, 'BRHelper/lang.lua': b'-- lang'})
        folder.scan(api)

        with pytest.warns(UserWarning, match='exact file match'):
            app_mod.resolve_exact_matches(folder, api)

    def test_resolves_via_crc_using_folders_own_url_template(self, addon_root, folder, monkeypatch):
        """End-to-end: resolve_exact_matches() must thread Folder.url_template through to
        find_exact_match(), so a CRC-verified match still resolves via this entry point, not
        just when calling find_exact_match() directly."""
        make_installed(addon_root, 'BRHelper', Author='LocalAuthor', Version='1.0')
        manifest_bytes = (addon_root / 'BRHelper' / 'BRHelper.txt').read_bytes()
        # `other`'s metadata is implausible enough to be filtered before the CRC check, so the
        # local install is genuinely ambiguous (two candidates share the dir) without it
        # interfering with the CRC-only resolution of `matching` below.
        other = make_addon_info(id_=2, title='Other', author='Someone Else', version='0.1', directories=['BRHelper'])
        matching = make_addon_info(id_=1, title='MatchedViaCrc', author='LocalAuthor',
                                   version='1.0', directories=['BRHelper'])
        api = make_api(addons={1: matching, 2: other})
        mock_remote_zip(monkeypatch, {'BRHelper/BRHelper.txt': manifest_bytes})
        folder.scan(api)

        resolved = app_mod.resolve_exact_matches(folder, api)

        [installed] = resolved
        assert installed.infos is matching
