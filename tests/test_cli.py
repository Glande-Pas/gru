"""Smoke tests for the gru CLI via click's CliRunner.

main() always builds a live API and scans the real addons folder, so these are limited to
scenarios that never trigger a network call (empty addons folder, no search term)."""

import pytest
from click.testing import CliRunner

from gru.cli import main


@pytest.fixture
def cli_config(tmp_path):
    """A config file pointing at a real, empty addons folder."""
    addons_root = tmp_path / 'AddOns'
    addons_root.mkdir()
    config_file = tmp_path / 'gru.ini'
    config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
    return config_file


def invoke(config_file, args):
    runner = CliRunner()
    return runner.invoke(main, ['--config', str(config_file), *args])


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
