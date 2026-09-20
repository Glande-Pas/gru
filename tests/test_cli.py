"""Smoke tests for the gru CLI via click's CliRunner.

main() builds its API/Folder through cli.build_app(), which TestWithRealAddons monkeypatches
to a stub -- letting these commands run against real installed addons with zero network."""

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
def cli_config(tmp_path):
    """A config file pointing at a real, empty addons folder."""
    addons_root = tmp_path / 'AddOns'
    addons_root.mkdir()
    config_file = tmp_path / 'gru.ini'
    config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
    return config_file


def invoke(config_file, args, input=None):
    runner = CliRunner()
    return runner.invoke(main, ['--config', str(config_file), *args], input=input)


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


class TestWithRealAddons:
    """Previously untestable without network: build_app() is monkeypatched to a stub API,
    so these commands run against real installed addons instead of only an empty folder."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_list_shows_installed_addon(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        result = invoke(cli_app['config_file'], ['list'])
        assert result.exit_code == 0
        assert 'My Addon' in result.output

    def test_remove_by_exact_name(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        result = invoke(cli_app['config_file'], ['remove', 'my addon', '--no-clean-deps'], input='y\n')
        assert result.exit_code == 0
        assert 'Removed addon My Addon.' in result.output
        assert not (cli_app['addons_root'] / 'MyAddon').exists()

    def test_remove_no_match_falls_through_without_crashing(self, cli_app):
        """Regression guard: used to crash reaching Folder.search() (dict_values.values())."""
        make_installed(cli_app['addons_root'], 'MyAddon')
        result = invoke(cli_app['config_file'], ['remove', 'totally-unrelated'])
        assert result.exit_code == 0
        assert 'No corresponding addon found.' in result.output

    def test_export_writes_installed_addons(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Version='3')
        result = invoke(cli_app['config_file'], ['export'])
        assert result.exit_code == 0
        exported = (cli_app['addons_root'] / '.gru' / 'addons.txt').read_text()
        assert 'MyAddon = 3' in exported


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
            local.scan(api)
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
