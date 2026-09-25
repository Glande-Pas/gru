"""Tests for gru.app: state persistence and orchestration shared by any front-end (CLI, GUI, ...)
on top of gru.api/gru.install -- see gru.cli for the thin command-dispatch layer built on this."""

import configparser
import csv
import pathlib

import pytest

import gru.app as app_mod
from gru.api import AmbiguousDirectory, PreviousVersion

from .conftest import make_installed, make_addon_info, make_api, StubAPI


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

    def test_api_version_match_via_hardcoded_table_ranks_first(self, addon_root):
        """installed's numeric ## APIVersion: (101050) and a candidate's dotted UICompatibility
        ('12.0.0') are different representations of the same thing -- API_VERSION_TO_DOTTED
        bridges them. Author/title/version all disagree here, so this is the only signal."""
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='9.9',
                                    APIVersion='101050')
        unrelated = make_addon_info(id_=1, title='Unrelated', author='x', version='1.0')
        matching = make_addon_info(id_=2, title='Matching', author='y', version='2.0',
                                   api=[{'version': '12.0.0', 'name': 'Season Zero Pt.2'}])
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [unrelated, matching], api, 'downloads')
        assert ranked[0] is matching

    def test_api_version_unknown_interface_does_not_crash_or_match(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='9.9',
                                    APIVersion='999999')  # not in the hardcoded table
        candidate = make_addon_info(id_=1, title='Unrelated', author='x', version='1.0',
                                    api=[{'version': '12.0.0', 'name': 'Season Zero Pt.2'}])
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [candidate], api, 'downloads')
        assert ranked == [candidate]  # doesn't crash; single candidate, order is moot

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
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='3.5')
        candidate = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        api = make_api()

        def boom(id_):
            raise RuntimeError('network is down')
        api.previous_versions = boom  # pyright: ignore[reportAttributeAccessIssue]

        ranked = app_mod.rank_candidates(installed, [candidate], api, 'downloads')
        assert ranked == [candidate]

    def test_filelist_overlap_ranks_first(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='9.9')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        base = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        matching = make_addon_info(id_=2, title='Base', author='x', version='1.0')
        api = make_api()
        files = {1: ['BRHelper/BRHelper.txt'], 2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua']}
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue] -- test double

        ranked = app_mod.rank_candidates(installed, [base, matching], api, 'downloads')
        assert ranked[0] is matching

    def test_falls_back_to_sortkey_when_metadata_and_filelist_tie(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper', Author='nobody', Version='9.9')
        low = make_addon_info(id_=1, title='Base', author='x', version='1.0', downloads=10)
        high = make_addon_info(id_=2, title='Base', author='x', version='1.0', downloads=999)
        api = make_api()

        ranked = app_mod.rank_candidates(installed, [low, high], api, 'downloads')
        assert ranked[0] is high

    def test_missing_filelist_method_is_tolerated(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper')
        base = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        api = make_api()  # no .filelist() attribute at all

        ranked = app_mod.rank_candidates(installed, [base], api, 'downloads')
        assert ranked == [base]

    def test_bloated_filelist_no_longer_ties_with_exact_match(self, addon_root):
        """Jaccard regression: a candidate whose filelist is padded with unrelated extra files
        used to tie with an exact match under plain recall (intersection / len(local_files))."""
        installed = self._installed(addon_root, 'BRHelper', Author='x', Version='1.0')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        exact = make_addon_info(id_=1, title='Base', author='x', version='1.0')
        bloated = make_addon_info(id_=2, title='Base', author='x', version='1.0')
        api = make_api()
        files = {
            1: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua'],
            2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua', 'BRHelper/extra1.lua', 'BRHelper/extra2.lua'],
        }
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue] -- test double

        ranked = app_mod.rank_candidates(installed, [bloated, exact], api, 'downloads')
        assert ranked[0] is exact


class TestFindExactMatch:
    def _installed(self, addon_root, dir_, **fields):
        return make_installed(addon_root, dir_, **fields)

    def test_single_exact_filelist_match_resolves(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        other = make_addon_info(id_=1, title='Other')
        exact = make_addon_info(id_=2, title='Exact')
        api = make_api()
        files = {1: ['BRHelper/BRHelper.txt'], 2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua']}
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue] -- test double

        assert app_mod.find_exact_match(installed, [other, exact], api) is exact

    def test_no_exact_match_returns_none(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        candidate = make_addon_info(id_=1, title='Other')
        api = make_api()
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]

        assert app_mod.find_exact_match(installed, [candidate], api) is None

    def test_two_candidates_both_exact_is_still_ambiguous(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper')
        first = make_addon_info(id_=1, title='First')
        second = make_addon_info(id_=2, title='Second')
        api = make_api()
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]

        assert app_mod.find_exact_match(installed, [first, second], api) is None

    def test_missing_filelist_method_is_tolerated(self, addon_root):
        installed = self._installed(addon_root, 'BRHelper')
        candidate = make_addon_info(id_=1, title='Other')
        api = make_api()  # no .filelist() attribute at all

        assert app_mod.find_exact_match(installed, [candidate], api) is None

    def test_nested_path_distinguishes_from_basename_only_match(self, addon_root):
        """Regression: comparing basenames only used to treat 'sub/util.lua' and
        'other/util.lua' as the same file. Full relative paths must tell them apart."""
        installed = self._installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'sub').mkdir()
        (addon_root / 'BRHelper' / 'sub' / 'util.lua').write_text('-- util')
        wrong_dir = make_addon_info(id_=1, title='WrongDir')
        right_dir = make_addon_info(id_=2, title='RightDir')
        api = make_api()
        files = {
            1: ['BRHelper/BRHelper.txt', 'BRHelper/other/util.lua'],
            2: ['BRHelper/BRHelper.txt', 'BRHelper/sub/util.lua'],
        }
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue]

        assert app_mod.find_exact_match(installed, [wrong_dir, right_dir], api) is right_dir

    def test_online_garbage_entries_are_pruned_before_comparing(self, addon_root):
        """__MACOSX/.DS_Store junk never survives onto disk from a real install (GARBAGE, see
        _inspect_bundle()) -- it must not count against an otherwise-exact filelist match."""
        installed = self._installed(addon_root, 'BRHelper')
        candidate = make_addon_info(id_=1, title='Exact')
        api = make_api()
        api.filelist = lambda id_: [  # pyright: ignore[reportAttributeAccessIssue]
            'BRHelper/BRHelper.txt', 'BRHelper/__MACOSX/BRHelper.txt', 'BRHelper/.DS_Store',
        ]

        assert app_mod.find_exact_match(installed, [candidate], api) is candidate

    def test_online_directory_entries_are_pruned_before_comparing(self, addon_root):
        """Zip contents list intermediate directories as their own entries (e.g. 'BRHelper/libs/')
        -- pathlib silently drops the trailing '/', which would otherwise make a directory
        indistinguishable from a same-named extensionless file. installed.files never lists
        directories either, so these must not count against an otherwise-exact match."""
        installed = self._installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'libs').mkdir()
        (addon_root / 'BRHelper' / 'libs' / 'Lib.lua').write_text('-- lib')
        candidate = make_addon_info(id_=1, title='Exact')
        api = make_api()
        api.filelist = lambda id_: [  # pyright: ignore[reportAttributeAccessIssue]
            'BRHelper/', 'BRHelper/BRHelper.txt', 'BRHelper/libs/', 'BRHelper/libs/Lib.lua',
        ]

        assert app_mod.find_exact_match(installed, [candidate], api) is candidate

    def test_online_sibling_bundled_addon_entries_are_excluded(self, addon_root):
        """A zip bundling several top-level addons reports one combined filelist -- entries
        belonging to a *different* bundled addon must not be compared against this install."""
        installed = self._installed(addon_root, 'BRHelper')
        candidate = make_addon_info(id_=1, title='Exact')
        api = make_api()
        api.filelist = lambda id_: [  # pyright: ignore[reportAttributeAccessIssue]
            'BRHelper/BRHelper.txt', 'OtherLib/OtherLib.txt', 'OtherLib/Data.lua',
        ]

        assert app_mod.find_exact_match(installed, [candidate], api) is candidate

    def test_exact_filelist_but_poor_metadata_does_not_resolve(self, addon_root):
        """Filelist equality alone isn't enough -- guards against two genuinely different addons
        that happen to share the same set of file *names* (title/author/version all say no)."""
        installed = self._installed(addon_root, 'BRHelper', Author='SomeAuthor', Version='3.5')
        candidate = make_addon_info(id_=1, title='CompletelyUnrelatedAddonXYZ', author='OtherAuthor',
                                    version='1.0')
        api = make_api()
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]
        api.previous_versions = lambda id_: []  # pyright: ignore[reportAttributeAccessIssue]

        assert app_mod.find_exact_match(installed, [candidate], api) is None

    def test_exact_filelist_with_only_archived_version_match_resolves(self, addon_root):
        """The metadata gate must search archived versions too, same as rank_candidates()."""
        installed = self._installed(addon_root, 'BRHelper', Author='OtherAuthor', Version='3.5')
        candidate = make_addon_info(id_=1, title='CompletelyUnrelatedAddonXYZ', author='SomeAuthor',
                                    version='9.0')
        api = make_api()
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]
        api.previous_versions = (  # pyright: ignore[reportAttributeAccessIssue]
            lambda id_: [PreviousVersion('3.5', '1KB', None, 'date', 'url', 1)])

        assert app_mod.find_exact_match(installed, [candidate], api) is candidate

    def test_exact_filelist_with_only_api_version_match_resolves(self, addon_root):
        """The metadata gate must consider a matching API_VERSION_TO_DOTTED-bridged API version
        a strong enough signal on its own too, same as rank_candidates()."""
        installed = self._installed(addon_root, 'BRHelper', Author='OtherAuthor', Version='3.5',
                                    APIVersion='101050')
        candidate = make_addon_info(id_=1, title='CompletelyUnrelatedAddonXYZ', author='SomeAuthor',
                                    version='9.0', api=[{'version': '12.0.0', 'name': 'Season Zero Pt.2'}])
        api = make_api()
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]
        api.previous_versions = lambda id_: []  # pyright: ignore[reportAttributeAccessIssue]

        assert app_mod.find_exact_match(installed, [candidate], api) is candidate


class TestResolveExactMatches:
    """resolve_exact_matches() is find_ambiguous() + find_exact_match() + link(), run right after
    scanning (see build_app()) so an exact match never needs a `gru match` prompt at all."""

    def test_resolves_and_links_exact_match(self, addon_root, folder):
        make_installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        other = make_addon_info(id_=1, title='Other', directories=['BRHelper'])
        exact = make_addon_info(id_=2, title='Exact', directories=['BRHelper'])
        api = make_api(addons={1: other, 2: exact})
        files = {1: ['BRHelper/BRHelper.txt'], 2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua']}
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue] -- test double
        folder.scan(api)

        resolved = app_mod.resolve_exact_matches(folder, api)

        [installed] = resolved
        assert installed.dir == 'BRHelper'
        assert installed.infos is exact

    def test_stays_ambiguous_when_no_candidate_is_an_exact_match(self, addon_root, folder):
        make_installed(addon_root, 'BRHelper')
        first = make_addon_info(id_=1, title='First', directories=['BRHelper'])
        second = make_addon_info(id_=2, title='Second', directories=['BRHelper'])
        api = make_api(addons={1: first, 2: second})
        api.filelist = lambda id_: ['BRHelper/BRHelper.txt']  # pyright: ignore[reportAttributeAccessIssue]
        folder.scan(api)  # identical filelists on both sides -- still a tie

        resolved = app_mod.resolve_exact_matches(folder, api)

        assert resolved == []
        installed = next(a for a in folder.installed if a.dir == 'BRHelper')
        assert installed.infos is None

    def test_warns_about_each_resolution(self, addon_root, folder):
        make_installed(addon_root, 'BRHelper')
        (addon_root / 'BRHelper' / 'lang.lua').write_text('-- lang')
        other = make_addon_info(id_=1, title='Other', directories=['BRHelper'])
        exact = make_addon_info(id_=2, title='Exact', directories=['BRHelper'])
        api = make_api(addons={1: other, 2: exact})
        files = {1: ['BRHelper/BRHelper.txt'], 2: ['BRHelper/BRHelper.txt', 'BRHelper/lang.lua']}
        api.filelist = lambda id_: files[id_]  # pyright: ignore[reportAttributeAccessIssue] -- test double
        folder.scan(api)

        with pytest.warns(UserWarning, match='exact file match'):
            app_mod.resolve_exact_matches(folder, api)
