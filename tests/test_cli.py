"""Smoke tests for the gru CLI via click's CliRunner.

main() builds its API/Folder through cli.build_app(), which TestWithRealAddons monkeypatches
to a stub -- letting these commands run against real installed addons with zero network."""

import pytest
from click.testing import CliRunner

import gru.cli as cli_mod
from gru.cli import main, TermDisplay
from gru.config import load_config

from .conftest import StubAPI, make_folder, make_installed


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
