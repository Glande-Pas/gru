"""Smoke tests for the gru CLI via click's CliRunner.

main() builds its API/Folder through cli.build_app(), which TestWithRealAddons monkeypatches
to a stub -- letting these commands run against real installed addons with zero network."""

import configparser
import csv
import io
import pathlib

import click
import pytest
from click.testing import CliRunner

import gru.cli as cli_mod
from gru.cli import main, TermDisplay
from gru.config import load_config

from .conftest import StubAPI, make_folder, make_installed, make_addon_info


class TestRenderEsoText:
    def test_closed_tag(self):
        result = TermDisplay._render_eso_text('|cFF0000Red|r Text')
        assert 'Red' in result and 'Text' in result
        assert '\x1b[' in result  # got styled, not left as raw markup

    def test_missing_closing_tag_auto_closes_at_end_of_string(self):
        result = TermDisplay._render_eso_text('|cFF0000Unterminated Red Text')
        assert 'Unterminated Red Text' in result
        assert '|c' not in result and '|r' not in result

    def test_missing_closing_tag_auto_closes_before_next_color_code(self):
        result = TermDisplay._render_eso_text('|cFF0000Red|c00FF00Green')
        assert 'Red' in result and 'Green' in result
        assert '|c' not in result and '|r' not in result

    def test_plain_text_unaffected(self):
        assert TermDisplay._render_eso_text('Plain title, no markup') == 'Plain title, no markup'


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    """A config file pointing at a real, empty addons folder. user_config() is redirected
    into tmp_path so commands that write metadata (addons.csv, patches) never touch the
    real user config dir, even though build_app() itself isn't stubbed here."""
    import gru.install as install_mod

    addons_root = tmp_path / 'AddOns'
    addons_root.mkdir()
    config_file = tmp_path / 'gru.ini'
    config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
    monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
    monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
    return config_file


def invoke(config_file, args, input=None):
    runner = CliRunner()
    return runner.invoke(main, ['--config', str(config_file), *args], input=input)


class TestAppendChangeLog:
    """_append_change_log() keeps at most max_lines data rows, oldest evicted first."""

    def test_writes_header_and_row(self, tmp_path):
        path = tmp_path / 'changes.csv'
        cli_mod._append_change_log(path, ['MyAddon', '1.0', 'link', 'date', 'none'], 100)

        rows = list(csv.reader(path.open()))
        assert rows[0] == ['dir', 'version', 'link', 'date', 'previous_state']
        assert rows[1] == ['MyAddon', '1.0', 'link', 'date', 'none']

    def test_rotates_out_oldest_row_beyond_max_lines(self, tmp_path):
        path = tmp_path / 'changes.csv'
        for i in range(5):
            cli_mod._append_change_log(path, [f'Addon{i}', '1.0', '', 'date', 'none'], 3)

        rows = list(csv.reader(path.open()))
        assert [row[0] for row in rows[1:]] == ['Addon2', 'Addon3', 'Addon4']

    def test_zero_max_lines_disables_logging(self, tmp_path):
        path = tmp_path / 'changes.csv'
        cli_mod._append_change_log(path, ['MyAddon', '1.0', '', 'date', 'none'], 0)

        rows = list(csv.reader(path.open()))
        assert rows == [['dir', 'version', 'link', 'date', 'previous_state']]


class TestLogChanges:
    """_log_changes() diffs before/after install state and appends one row per addon whose
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
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        before = cli_mod._addon_snapshot(folder)

        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}

        cli_mod._log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', '1.0', upstream.metadata['link']]
        assert rows[1][4] == cli_mod.NOT_INSTALLED

    def test_removal_logs_sentinel_as_new_version(self, addon_root, folder, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}
        before = cli_mod._addon_snapshot(folder)

        folder._installed = {}

        cli_mod._log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', cli_mod.NOT_INSTALLED, upstream.metadata['link']]
        assert rows[1][4] == '1.0'

    def test_update_logs_both_real_versions(self, addon_root, folder, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}
        before = cli_mod._addon_snapshot(folder)

        installed.version = '2.0'

        cli_mod._log_changes(folder, self._config(), before)

        rows = self._changes(tmp_path)
        assert rows[1][:3] == ['MyAddon', '2.0', upstream.metadata['link']]
        assert rows[1][4] == '1.0'

    def test_no_change_writes_nothing(self, addon_root, folder, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        folder._installed = {installed.folder: installed}
        before = cli_mod._addon_snapshot(folder)

        cli_mod._log_changes(folder, self._config(), before)

        assert not (tmp_path / 'config' / 'ESO' / 'changes.csv').exists()


class TestResolveAddonsRoot:
    """resolve_addons_root() is a plain function (no Click context needed): it mutates `config`
    in-memory and reports whether it changed anything via its return value -- build_app() only
    calls save_config() when that's True, so an already-valid root must never be reported as changed."""

    def _config(self, root: str) -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.add_section('ESO.addons')
        config.set('ESO.addons', 'root', root)
        return config

    def test_valid_existing_root_returns_false_and_leaves_config_unchanged(self, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config = self._config(str(addons_root))

        changed = cli_mod.resolve_addons_root(config, 'ESO')

        assert changed is False
        assert config.get('ESO.addons', 'root') == str(addons_root)

    def test_missing_root_prompts_updates_config_and_returns_true(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config = self._config('')  # not set -- must prompt
        monkeypatch.setattr(cli_mod.click, 'prompt', lambda *a, **kw: addons_root)

        changed = cli_mod.resolve_addons_root(config, 'ESO')

        assert changed is True
        assert config.get('ESO.addons', 'root') == str(addons_root.resolve())


class TestBuildAppPersistsResolvedRootOnly:
    """build_app() must call save_config() exactly when resolve_addons_root() actually changed
    something -- never unconditionally, since that would silently rewrite (and, since
    configparser doesn't round-trip comments, lose comments in) config.ini on every command."""

    def _config_file(self, tmp_path: pathlib.Path, addons_root: pathlib.Path) -> pathlib.Path:
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        return config_file

    def test_saves_when_root_was_resolved(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = self._config_file(tmp_path, addons_root)

        saved = []
        monkeypatch.setattr(cli_mod, 'resolve_addons_root', lambda cfg, game: True)
        monkeypatch.setattr(cli_mod, 'save_config', lambda cfg, cfg_file: saved.append(cfg_file))

        cli_mod.build_app('ESO', config_file)

        assert saved == [config_file]

    def test_does_not_save_when_root_was_already_valid(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = self._config_file(tmp_path, addons_root)

        saved = []
        monkeypatch.setattr(cli_mod, 'resolve_addons_root', lambda cfg, game: False)
        monkeypatch.setattr(cli_mod, 'save_config', lambda cfg, cfg_file: saved.append(cfg_file))

        cli_mod.build_app('ESO', config_file)

        assert saved == []


class TestNoNetworkSmokeTests:
    def test_list_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['list'])
        assert result.exit_code == 0
        assert 'No addons installed.' in result.output

    def test_remove_with_no_addons_installed_and_no_term(self, cli_config):
        result = invoke(cli_config, ['remove'])
        assert result.exit_code == 0
        assert 'No corresponding addon found.' in result.output

    def test_export_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['export'])
        assert result.exit_code == 0
        assert 'No addons installed.' in result.output

    def test_config_get_and_set_roundtrip(self, cli_config):
        result = invoke(cli_config, ['config', 'set', 'addons.optional', 'on'])
        assert result.exit_code == 0

        result = invoke(cli_config, ['config', 'get', 'addons.optional'])
        assert result.exit_code == 0
        assert result.output.strip() == 'on'  # fully-qualified entry -> bare value, no repr

    def test_config_set_rejects_bad_boolean(self, cli_config):
        result = invoke(cli_config, ['config', 'set', 'addons.optional', 'sideways'])
        assert result.exit_code == 0  # command catches the error, doesn't propagate it
        assert 'must be "on" or "off"' in result.output

    def test_cleanup_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['cleanup'])
        assert result.exit_code == 0
        assert 'Removed 0 unused dependence(s).' in result.output

    def test_config_subcommands_skip_addons_root_prompt(self, tmp_path):
        """Regression: config get/set must not go through build_app()'s addons-root prompt --
        a plain config read/write shouldn't require (or block on) a valid addons folder. No
        `input=` is given, so if this did hit click.prompt() it would fail rather than hang."""
        config_file = tmp_path / 'gru.ini'  # deliberately no [ESO.addons] root at all

        result = invoke(config_file, ['config', 'get', 'app.open_in_browser'])
        assert result.exit_code == 0
        assert result.output.strip() == 'off'

        result = invoke(config_file, ['config', 'set', 'app.open_in_browser', 'on'])
        assert result.exit_code == 0
        assert 'root = \n' in config_file.read_text()  # stayed empty -- never resolved/prompted for


class TestWithRealAddons:
    """Previously untestable without network: build_app() is monkeypatched to a stub API,
    so these commands run against real installed addons instead of only an empty folder."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_list_shows_installed_addon(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        result = invoke(cli_app['config_file'], ['list'])
        assert result.exit_code == 0
        assert 'My Addon' in result.output

    def test_remove_by_exact_name(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        make_installed(cli_app['addons_root'], 'OtherAddon', Title='Other Addon')
        result = invoke(cli_app['config_file'], ['remove', 'my addon', '--no-clean-deps'], input='y\n')
        assert result.exit_code == 0
        assert 'Removed addon My Addon.' in result.output
        assert not (cli_app['addons_root'] / 'MyAddon').exists()

        # addons.csv is kept in sync after remove -- the removed addon is gone from it
        rows = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'addons.csv').open()))
        assert [row[0] for row in rows[1:]] == ['OtherAddon']

        # changes.csv records the removal, with the sentinel as the new "version"
        changes = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'changes.csv').open()))
        assert changes[1][0] == 'MyAddon'
        assert changes[1][1] == cli_mod.NOT_INSTALLED
        assert changes[1][4] == '1.0'

    def test_remove_no_match_falls_through_without_crashing(self, cli_app):
        """Regression guard: used to crash reaching Folder.search() (dict_values.values())."""
        make_installed(cli_app['addons_root'], 'MyAddon')
        result = invoke(cli_app['config_file'], ['remove', 'totally-unrelated'])
        assert result.exit_code == 0
        assert 'No corresponding addon found.' in result.output
        # Nothing was removed -- addons.csv must not have been (re)written
        assert not (cli_app['config_dir'] / 'ESO' / 'addons.csv').exists()


class TestCleanupCommand:
    """gru cleanup --dedupe: removes a standalone library install superseded by an
    equal-or-newer copy bundled inside another addon; without the flag, it's left alone."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        make_installed(addons_root, 'Parent', Title='Parent', DependsOn='LibShared>=1')
        make_installed(addons_root / 'Parent', 'LibShared', Title='LibShared', IsLibrary='true', Version='2.0')
        make_installed(addons_root, 'LibShared', Title='LibShared', IsLibrary='true', Version='1.0')
        upstream = make_addon_info(id_=1, title='LibShared', directories=['LibShared'])

        class LinkableApi(StubAPI):
            def dir(self, name):  # pyright: ignore[reportIncompatibleMethodOverride] -- real AddonInfo, not StubAddon
                if name == 'LibShared':
                    return upstream
                raise FileNotFoundError(name)

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = LinkableApi()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_dedupe_removes_superseded_standalone_and_reports_it(self, cli_app):
        result = invoke(cli_app['config_file'], ['cleanup', '--dedupe'])
        assert result.exit_code == 0
        assert 'Removed duplicate LibShared, superseded by the copy bundled inside Parent' in result.output
        assert 'Removed 1 duplicate install(s).' in result.output
        assert not (cli_app['addons_root'] / 'LibShared').exists()
        assert (cli_app['addons_root'] / 'Parent' / 'LibShared').exists()

    def test_dedupe_removal_is_reflected_in_addons_csv_and_changes_csv(self, cli_app):
        """Regression: cleanup used to skip _export_addon_state()/_log_changes() entirely,
        so removals (including --dedupe ones) never showed up in either file."""
        result = invoke(cli_app['config_file'], ['cleanup', '--dedupe'])
        assert result.exit_code == 0

        addons = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'addons.csv').open()))
        assert [row[0] for row in addons[1:]] == ['Parent']  # only the nested LibShared remains, hidden from export

        changes = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'changes.csv').open()))
        assert changes[1][0] == 'LibShared'
        assert changes[1][1] == cli_mod.NOT_INSTALLED
        assert changes[1][4] == '1.0'

    def test_without_dedupe_flag_duplicate_is_left_alone(self, cli_app):
        result = invoke(cli_app['config_file'], ['cleanup'])
        assert result.exit_code == 0
        assert 'duplicate' not in result.output
        assert (cli_app['addons_root'] / 'LibShared').exists()


class TestExportCommand:
    """gru export: CSV to stdout by default, or to a file with --output/-o; includes the
    online info-page link as an extra column when the addon is matched, blank otherwise."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        make_installed(addons_root, 'MyAddon', Version='3')
        make_installed(addons_root, 'LocalOnly', Version='1')
        upstream = make_addon_info(id_=1, title='MyAddon', version='3', directories=['MyAddon'])

        class LinkableApi(StubAPI):
            def dir(self, name):  # pyright: ignore[reportIncompatibleMethodOverride] -- real AddonInfo, not StubAddon
                if name == 'MyAddon':
                    return upstream
                raise FileNotFoundError(name)

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = LinkableApi()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'upstream': upstream}

    def test_export_prints_csv_to_stdout_by_default(self, cli_app):
        result = invoke(cli_app['config_file'], ['export'])
        assert result.exit_code == 0

        # stdout only, not .output -- matches what `gru export > file.csv` would receive
        rows = list(csv.reader(io.StringIO(result.stdout)))
        assert rows[0] == ['dir', 'version', 'link']
        by_dir = {row[0]: row for row in rows[1:]}
        assert by_dir['MyAddon'] == ['MyAddon', '3', cli_app['upstream'].metadata['link']]
        assert by_dir['LocalOnly'] == ['LocalOnly', '1', '']

    def test_export_writes_to_output_file(self, cli_app, tmp_path):
        out_path = tmp_path / 'out.csv'
        result = invoke(cli_app['config_file'], ['export', '--output', str(out_path)])
        assert result.exit_code == 0
        assert 'exported to' in result.output
        assert 'dir,version,link' not in result.output  # CSV rows went to the file, not stdout

        rows = list(csv.reader(out_path.open()))
        assert rows[0] == ['dir', 'version', 'link']
        by_dir = {row[0]: row for row in rows[1:]}
        assert by_dir['MyAddon'] == ['MyAddon', '3', cli_app['upstream'].metadata['link']]

    def test_export_short_output_flag(self, cli_app, tmp_path):
        out_path = tmp_path / 'out.csv'
        result = invoke(cli_app['config_file'], ['export', '-o', str(out_path)])
        assert result.exit_code == 0
        assert out_path.exists()


class TestParentSuffixWording:
    """The ', <relation> X' suffix after 'installed' uses a distinct connector per relationship:
    'listed online as' (title differs), 'part of' (multi-folder bundle), 'bundled inside'
    (unmatched folder nested in a matched one) -- not a single overloaded 'as X'."""

    def _list_output(self, monkeypatch, addons_root, config_file, api):
        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            local = make_folder(addons_root)
            local.scan(api)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return invoke(config_file, ['list']).output

    def _single_addon_setup(self, tmp_path, manifest_title, upstream_title):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'MyAddon', Title=manifest_title)
        upstream = make_addon_info(id_=1, title=upstream_title, directories=['MyAddon'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                return upstream

        return addons_root, config_file, LinkableApi()

    def test_listed_online_as_for_markup_only_difference(self, monkeypatch, tmp_path):
        """Real case: HideGroupNecro's manifest Title is 'HideGroup|c5050ffNecro|r'. Byte-for-byte
        comparison correctly detects this differs from the plain upstream title -- and since color
        now defaults on (see TestNoColor), the main title actually renders 'Necro' in color, giving
        a real visual cue for the difference instead of two seemingly-identical strings."""
        addons_root, config_file, api = self._single_addon_setup(tmp_path, 'HideGroup|c5050ffNecro|r', 'HideGroupNecro')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert ', listed online as HideGroupNecro' in output
        assert '\x1b[' in output

    def test_listed_online_as_when_titles_genuinely_differ(self, monkeypatch, tmp_path):
        addons_root, config_file, api = self._single_addon_setup(tmp_path, 'MyAddon', 'Completely Different Name')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert ', listed online as Completely Different Name' in output

    def test_part_of_for_multi_folder_bundle(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'MainPart', Title='MainPart')
        make_installed(addons_root, 'ExtraPart', Title='ExtraPart')
        upstream = make_addon_info(id_=1, title='BundleName', directories=['MainPart', 'ExtraPart'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                return upstream

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', part of BundleName' in output

    def _duplicate_setup(self, tmp_path, version_a, version_b):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'CopyA', Title='CopyA', Version=version_a)
        make_installed(addons_root, 'CopyB', Title='CopyB', Version=version_b)
        upstream = make_addon_info(id_=1, title='LibShared', directories=['CopyA'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                return upstream

        return addons_root, config_file, LinkableApi()

    def test_version_precedence_marks_higher_version_active_lower_superseded(self, monkeypatch, tmp_path):
        addons_root, config_file, api = self._duplicate_setup(tmp_path, '2.0', '1.0')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert 'CopyA [installed, part of LibShared (active)]' in output
        assert 'CopyB [installed, part of LibShared (superseded)]' in output

    def test_version_precedence_ties_are_both_active(self, monkeypatch, tmp_path):
        addons_root, config_file, api = self._duplicate_setup(tmp_path, '1.0', '1.0')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert output.count(', part of LibShared (active)') == 2
        assert 'superseded' not in output

    def test_version_precedence_omitted_when_a_version_is_unparseable(self, monkeypatch, tmp_path):
        addons_root, config_file, api = self._duplicate_setup(tmp_path, '2.0', 'unknown')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert ', part of LibShared]' in output
        assert 'active' not in output and 'superseded' not in output

    def test_bundled_inside_for_unmatched_nested_folder(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'ParentAddon', Title='ParentAddon')
        make_installed(addons_root / 'ParentAddon', 'ChildLib', Title='ChildLib')
        upstream = make_addon_info(id_=1, title='ParentAddon', directories=['ParentAddon'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                if name == 'ParentAddon':
                    return upstream
                raise FileNotFoundError(name)

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', bundled inside ParentAddon' in output

    def test_bundled_inside_for_matched_nested_addon(self, monkeypatch, tmp_path):
        """A nested addon that IS itself matched online goes through _installed(), not
        _folder() -- it must still show where it's bundled. This was the missing annotation:
        _installed() checked infos.folders (duplicate online match) but never addon.parent."""
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'ParentAddon', Title='ParentAddon')
        make_installed(addons_root / 'ParentAddon', 'ChildLib', Title='ChildLib')
        upstream_parent = make_addon_info(id_=1, title='ParentAddon', directories=['ParentAddon'])
        upstream_child = make_addon_info(id_=2, title='ChildLib', directories=['ChildLib'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                if name == 'ParentAddon':
                    return upstream_parent
                elif name == 'ChildLib':
                    return upstream_child
                raise FileNotFoundError(name)

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', bundled inside ParentAddon' in output

    def test_bundled_inside_and_part_of_combine_for_duplicate_nested_install(self, monkeypatch, tmp_path):
        """Real-world shape: a library bundled inside another addon's folder, also installed
        standalone at top level. Both folders share one online AddonInfo (-> 'part of' on
        both), and the nested one additionally shows where it's bundled."""
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'LootLog', Title='Loot Log')
        make_installed(addons_root / 'LootLog', 'LibExtendedJournal', Title='LibExtendedJournal')
        make_installed(addons_root, 'LibExtendedJournal', Title='LibExtendedJournal')
        upstream_parent = make_addon_info(id_=1, title='LootLog', directories=['LootLog'])
        upstream_lib = make_addon_info(id_=2, title='LibExtendedJournal', directories=['LibExtendedJournal'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                if name == 'LootLog':
                    return upstream_parent
                elif name == 'LibExtendedJournal':
                    return upstream_lib
                raise FileNotFoundError(name)

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', bundled inside Loot Log, part of LibExtendedJournal' in output
        assert output.count(', part of LibExtendedJournal') == 2


class TestNoColor:
    """--no-color / default-color-on, driven through ctx.color rather than per-callsite."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        make_installed(addons_root, 'MyAddon', Title='|c5050ffMyAddon|r')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return config_file

    def test_color_on_by_default_even_though_output_is_captured(self, cli_app):
        """CliRunner-captured output isn't a real tty, so click's own auto-detection would
        normally strip ANSI codes here -- ctx.color = True overrides that."""
        result = invoke(cli_app, ['list'])
        assert result.exit_code == 0
        assert '\x1b[' in result.output

    def test_no_color_flag_strips_styling(self, cli_app):
        result = invoke(cli_app, ['--no-color', 'list'])
        assert result.exit_code == 0
        assert '\x1b[' not in result.output
        assert 'MyAddon' in result.output

    def test_no_color_propagates_from_group_to_subcommand_context(self, cli_app):
        """ctx.color is set on main()'s own Context, but list runs in a child Context --
        confirms it's actually inherited, not just set on the group callback's own context."""
        result = invoke(cli_app, ['--no-color', 'list'])
        assert click.unstyle(result.output) == result.output


class TestDiffCommand:
    """gru diff: only the network layer (requests.head/get) is faked -- everything else
    (Folder.unmodified_addon -> unpack -> _inspect_bundle -> InstalledAddon, addon_diff) runs for
    real. Regression guard for passing the wrong type (InstalledAddon instead of its .infos
    AddonInfo) into unmodified_addon(), which crashed with AttributeError deep inside
    InstalledAddon.link() -> infos.register(self)."""

    @staticmethod
    def _zip_bytes(entries: dict[str, str]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for fname, content in entries.items():
                zf.writestr(fname, content)
        return buf.getvalue()

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon')
        (installed.folder / 'Data.lua').write_text('old = 1\n')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)

        zip_bytes = self._zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'old = 2\n',  # the "unmodified" upstream differs -> real diff to save
        })

        class FakeResponse:
            content: bytes  # only set on responses that carry a body

            def __init__(self, **attrs):
                self.__dict__.update(attrs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=1024):
                yield self.content

        import gru.install as install_mod
        headers = {'content-length': str(len(zip_bytes))}
        monkeypatch.setattr(install_mod.requests, 'head',
                            lambda url, allow_redirects=True: FakeResponse(headers=headers))
        monkeypatch.setattr(install_mod.requests, 'get',
                            lambda url, stream=True, allow_redirects=True: FakeResponse(content=zip_bytes))
        monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(tmp_path, *parts))
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            for addon in local.installed:
                addon.link(upstream)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_diff_does_not_crash_and_saves_a_patch(self, cli_app):
        result = invoke(cli_app['config_file'], ['diff', 'MyAddon'], input='y\n')
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'register' not in result.output
        assert 'Changes saved under' in result.output
        assert (cli_app['config_dir'] / 'ESO' / 'MyAddon.patch').exists()


def _touch_cache_path(base: pathlib.Path, *parts: str) -> pathlib.Path:
    path = base / 'cache'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _touch_config_path(base: pathlib.Path, *parts: str) -> pathlib.Path:
    path = base / 'config'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestUpdateCommand:
    """gru update: only the network layer (requests.head/get) is faked -- Folder.update()
    (unpack -> _inspect_bundle -> InstalledAddon, install_deps) runs for real."""

    @staticmethod
    def _zip_bytes(entries: dict[str, str]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for fname, content in entries.items():
                zf.writestr(fname, content)
        return buf.getvalue()

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='2.0', directories=['MyAddon'])
        installed.link(upstream)

        zip_bytes = self._zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'new = 2\n',
        })

        class FakeResponse:
            content: bytes  # only set on responses that carry a body

            def __init__(self, **attrs):
                self.__dict__.update(attrs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=1024):
                yield self.content

        import gru.install as install_mod
        headers = {'content-length': str(len(zip_bytes))}
        monkeypatch.setattr(install_mod.requests, 'head',
                            lambda url, allow_redirects=True: FakeResponse(headers=headers))
        monkeypatch.setattr(install_mod.requests, 'get',
                            lambda url, stream=True, allow_redirects=True: FakeResponse(content=zip_bytes))
        monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(tmp_path, *parts))
        monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            for addon in local.installed:
                addon.link(upstream)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_update_installs_new_version(self, cli_app):
        result = invoke(cli_app['config_file'], ['update'])
        assert result.exit_code == 0
        assert 'Updated 1 addon(s) and installed 0 dependence(s)' in result.output
        assert (cli_app['addons_root'] / 'MyAddon' / 'Data.lua').read_text() == 'new = 2\n'

        # addons.csv is refreshed after update
        rows = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'addons.csv').open()))
        assert [row[0] for row in rows[1:]] == ['MyAddon']

        # changes.csv records the version transition
        changes = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'changes.csv').open()))
        assert changes[1][:2] == ['MyAddon', '2.0']
        assert changes[1][4] == '1.0'

    def test_update_with_nothing_to_do(self, cli_config):
        result = invoke(cli_config, ['update'])
        assert result.exit_code == 0
        assert 'Nothing to do' in result.output


class TestAddonStateSurvivesCrash:
    """addons.csv (and pending warnings) must still be written/shown even when get/remove/update
    raise partway through -- both live in a finally: block precisely so a crash doesn't leave
    addons.csv stale. Regression guard: Click's own result_callback (which also calls
    show_warnings()) does NOT run when the command raises, so there is no safety net above this."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        make_installed(addons_root, 'MyAddon', Version='1.0')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'config_dir': tmp_path / 'config'}

    def _addons_csv_rows(self, cli_app):
        with (cli_app['config_dir'] / 'ESO' / 'addons.csv').open() as f:
            return [row[0] for row in csv.reader(f)][1:]

    def test_update_crash_still_writes_addons_csv(self, monkeypatch, cli_app):
        def boom(self, *a, **kw):
            raise RuntimeError('simulated crash')
        monkeypatch.setattr(cli_mod.Folder, 'update', boom)

        result = invoke(cli_app['config_file'], ['update'])
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert self._addons_csv_rows(cli_app) == ['MyAddon']

    def test_remove_crash_still_writes_addons_csv(self, monkeypatch, cli_app):
        def boom(self, *a, **kw):
            raise RuntimeError('simulated crash')
        monkeypatch.setattr(cli_mod.Folder, 'remove', boom)

        result = invoke(cli_app['config_file'], ['remove', 'MyAddon'], input='y\n')
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert self._addons_csv_rows(cli_app) == ['MyAddon']

    def test_get_crash_still_writes_addons_csv(self, monkeypatch, cli_app):
        upstream = make_addon_info(id_=1, title='OtherAddon', directories=['OtherAddon'])

        class FindableApi(StubAPI):
            def find(self, val, local):
                return [upstream]

        addons_root = cli_app['config_dir'].parent / 'AddOns'

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = FindableApi()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)

        def boom(self, *a, **kw):
            raise RuntimeError('simulated crash')
        monkeypatch.setattr(cli_mod.Folder, 'install', boom)

        result = invoke(cli_app['config_file'], ['get', 'OtherAddon', '--yes'])
        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert self._addons_csv_rows(cli_app) == ['MyAddon']


class TestPatchCommand:
    """gru patch: addon_patch_file() applying a real, on-disk .patch file end-to-end."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon')
        (installed.folder / 'Data.lua').write_text('old = 2\n')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_patch_applies_saved_patch_from_default_location(self, cli_app):
        from gru.patch import addon_diff

        patched = make_installed(cli_app['addons_root'].parent / 'patched_src', 'MyAddon')
        (patched.folder / 'Data.lua').write_text('patched = 3\n')
        unpatched = make_installed(cli_app['addons_root'].parent / 'unpatched_src', 'MyAddon')
        (unpatched.folder / 'Data.lua').write_text('old = 2\n')

        patch_dir = cli_app['config_dir'] / 'ESO'
        patch_dir.mkdir(parents=True)
        with (patch_dir / 'MyAddon.patch').open('w') as f:
            addon_diff(patched, unpatched, out=f)

        result = invoke(cli_app['config_file'], ['patch', 'MyAddon'], input='y\n')

        assert result.exit_code == 0
        assert 'Applied patch successfully.' in result.output
        assert (cli_app['addons_root'] / 'MyAddon' / 'Data.lua').read_text() == 'patched = 3\n'

    def test_patch_with_no_saved_changes_reports_nothing_to_apply(self, cli_app):
        result = invoke(cli_app['config_file'], ['patch', 'MyAddon'], input='y\n')
        assert result.exit_code == 0
        assert 'No saved changes to be re-applied.' in result.output


class TestSearchAndMissCommands:
    """Regression guards for bugs found while fixing pyright errors: search/miss called the
    undefined name _display() (NameError), and miss() caught the wrong exception type around
    api.dir() (it raises FileNotFoundError, not ValueError), so an unresolved dependency used
    to crash the whole command instead of being reported."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        found_dep = make_addon_info(id_=2, title='LibFoo', directories=['LibFoo'])

        class RichStubAPI(StubAPI):
            def search(self, term, tiebreakattr=None, maxlen=30):
                return [make_addon_info(id_=1, title='SearchResult', directories=['SearchResult'])]

            def dir(self, name):
                if name == 'LibFoo':
                    return found_dep
                raise FileNotFoundError(name)

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = RichStubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_search_with_results_does_not_crash(self, cli_app):
        result = invoke(cli_app['config_file'], ['search', 'anything'])
        assert result.exit_code == 0
        assert 'NameError' not in result.output
        assert 'SearchResult' in result.output

    def test_miss_reports_unresolvable_dependency_instead_of_crashing(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', DependsOn='LibFoo>=1 LibGone>=1')
        result = invoke(cli_app['config_file'], ['miss'])
        assert result.exit_code == 0
        assert 'NameError' not in result.output
        assert 'LibFoo' in result.output  # resolved -> shown via TermDisplay
        assert 'Not found online: LibGone' in result.output  # unresolved -> reported, not crashed


class TestFolderDisplayDescriptionField:
    """Regression: _folder()'s local `infos` was accidentally a tuple ([a], [b]) rather than a
    list, so infos.append(...) for a manifest's Description field raised AttributeError --
    hit for any unmatched local addon whose manifest declares one (a common manifest field)."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()  # empty -- addon stays unmatched, goes through _folder()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_list_with_description_field_does_not_crash(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Description='A cool addon')
        result = invoke(cli_app['config_file'], ['list'])
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'Description: A cool addon' in result.output


class TestGetCommand:
    """Regression guards for two bugs found while fixing pyright errors in get()'s loop."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))
        return {'addons_root': addons_root, 'config_file': config_file, 'config_dir': tmp_path / 'config'}

    def _wire_build_app(self, monkeypatch, addons_root, api):
        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            local = make_folder(addons_root)
            local.scan(api)
            return config, api, local
        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)

    def test_locally_matched_but_uncataloged_addon_is_skipped_not_installed(self, monkeypatch, cli_app):
        """Regression: api.find() can return a bare InstalledAddon (matched by local
        directory name only, not present online). Passing that into local.install() ->
        unpack() -> InstalledAddon.link() crashes: AttributeError, InstalledAddon has no
        register() (only AddonInfo does) -- same shape as the earlier `gru diff` bug."""
        installed = make_installed(cli_app['addons_root'], 'LocalOnly')

        class LocalOnlyApi(StubAPI):
            def find(self, val, local):
                return [installed]

        self._wire_build_app(monkeypatch, cli_app['addons_root'], LocalOnlyApi())
        result = invoke(cli_app['config_file'], ['get', 'LocalOnly', '--yes'])
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'register' not in result.output
        assert 'already installed locally and not found online' in result.output

        # addons.csv is (re)written after get, even when nothing new was installed
        rows = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'addons.csv').open()))
        assert [row[0] for row in rows[1:]] == ['LocalOnly']

    def test_batch_mode_continues_cleanly_after_install_failure(self, monkeypatch, cli_app):
        """Regression: on KeyError from local.install() in batch (--yes) mode, the loop
        didn't `continue`, so `result` was referenced further down while still unbound
        from any successful assignment -- UnboundLocalError instead of the intended
        'Failed installing ...' message."""
        upstream = make_addon_info(id_=1, title='MyAddon', directories=['MyAddon'])

        class FindableApi(StubAPI):
            def find(self, val, local):
                return [upstream]

        self._wire_build_app(monkeypatch, cli_app['addons_root'], FindableApi())

        def broken_install(self, *args, **kwargs):
            raise KeyError('simulated failure')
        monkeypatch.setattr(cli_mod.Folder, 'install', broken_install)

        result = invoke(cli_app['config_file'], ['get', 'MyAddon', '--yes'])
        assert result.exit_code == 0
        assert 'UnboundLocalError' not in result.output
        assert 'Failed installing' in result.output

    def test_fresh_install_logs_change_with_sentinel_previous_state(self, monkeypatch, cli_app):
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])

        class FindableApi(StubAPI):
            def find(self, val, local):
                return [upstream]

        self._wire_build_app(monkeypatch, cli_app['addons_root'], FindableApi())

        import zipfile
        import gru.install as install_mod

        class FakeResponse:
            content: bytes

            def __init__(self, **attrs):
                self.__dict__.update(attrs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=1024):
                yield self.content

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            manifest = '## Title: MyAddon\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n'
            zf.writestr('MyAddon/MyAddon.txt', manifest)
        zip_bytes = buf.getvalue()

        headers = {'content-length': str(len(zip_bytes))}
        monkeypatch.setattr(install_mod.requests, 'head',
                            lambda url, allow_redirects=True: FakeResponse(headers=headers))
        monkeypatch.setattr(install_mod.requests, 'get',
                            lambda url, stream=True, allow_redirects=True: FakeResponse(content=zip_bytes))
        cache_base = cli_app['addons_root'].parent
        monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(cache_base, *parts))

        result = invoke(cli_app['config_file'], ['get', 'MyAddon', '--yes'])
        assert result.exit_code == 0
        assert 'Done installing' in result.output

        rows = list(csv.reader((cli_app['config_dir'] / 'ESO' / 'changes.csv').open()))
        assert rows[1][:2] == ['MyAddon', '1.0']
        assert rows[1][4] == cli_mod.NOT_INSTALLED


class TestFolderSearchTiebreak:
    def test_exactly_tied_candidates_do_not_crash_sorting(self, addon_root, folder):
        """Regression: Folder.search()'s default tiebreakattr=None used to become the
        list [None]; two candidates tied on (sum(matches), max(matches)) then forced
        sorted() to compare None < None to break the tie, raising TypeError."""
        make_installed(addon_root, 'AddonOne', Title='Tied Title')
        make_installed(addon_root, 'AddonTwo', Title='Tied Title')
        folder.scan()
        result = folder.search('Tied Title')
        assert len(result) >= 2
