
import pytest

from gru import gamesettings
from gru.addon import AddonBundle
from gru.gamesettings import Character

from .conftest import make_folder, make_installed

SAMPLE = """#Version 101051
#AcknowledgedOutOfDateAddonsVersion 101050
#AddOnsEnabled 1
#Default
ESOLiveSplit 0
LibAddonMenu-2.0 1
#EU Megaserver-Glande-Pas
Azurah 0
LibAddonMenu-2.0 1
ESOLiveSplit 0
#EU Megaserver-Sakka-do
Azurah 1
#PTS-Drtg sfqsdf dsfg
Azurah 0
"""


class TestParse:
    def test_headers(self):
        settings = gamesettings.parse(SAMPLE)

        assert settings.api_version == 101051
        assert settings.acknowledged_api_version == 101050
        assert settings.addons_enabled is True

    def test_labels_split_on_first_dash(self):
        settings = gamesettings.parse(SAMPLE)

        assert set(settings.characters) == {Character('EU Megaserver', 'Glande-Pas'),
                                            Character('EU Megaserver', 'Sakka-do'),
                                            Character('PTS', 'Drtg sfqsdf dsfg')}

    def test_default_is_not_a_character(self):
        settings = gamesettings.parse(SAMPLE)

        assert settings.disabled_by_default('ESOLiveSplit')
        assert not settings.disabled_by_default('LibAddonMenu-2.0')
        assert all(character.name != 'Default' for character in settings.characters)

    def test_disabled_on_lists_only_explicit_zeros(self):
        settings = gamesettings.parse(SAMPLE)

        assert settings.disabled_on('Azurah') == [Character('EU Megaserver', 'Glande-Pas'),
                                                  Character('PTS', 'Drtg sfqsdf dsfg')]
        assert settings.disabled_on('Unknown') == []
        assert settings.disabled('Unknown') is None

    def test_disabled_info(self):
        settings = gamesettings.parse(SAMPLE)

        info = settings.disabled('ESOLiveSplit')

        assert info is not None and info.everywhere
        assert info.characters == [Character('EU Megaserver', 'Glande-Pas')]

    def test_add_on_names_are_case_insensitive(self):
        assert gamesettings.parse(SAMPLE).disabled_on('azurah')

    def test_tolerates_crlf_noise_and_missing_headers(self):
        settings = gamesettings.parse('#Mystery\r\nAzurah 0\r\nnot a line\r\nAzurah two\r\n\r\n#Other 7\r\nX 1\r\n')

        assert settings.api_version is None
        assert settings.addons_enabled is True
        assert settings.disabled_on('Azurah') == [Character('', 'Mystery')]

    def test_lines_before_any_section_are_ignored(self):
        assert gamesettings.parse('Azurah 0\n').characters == {}

    def test_addons_disabled_globally(self):
        assert gamesettings.parse('#AddOnsEnabled 0\n').addons_enabled is False


class TestLoad:
    def test_missing_file(self, tmp_path):
        assert gamesettings.load(tmp_path) is None

    def test_reads_utf8_bom(self, tmp_path):
        (tmp_path / 'AddOnSettings.txt').write_bytes(b'\xef\xbb\xbf' + SAMPLE.encode())

        settings = gamesettings.load(tmp_path)

        assert settings is not None and settings.api_version == 101051

    def test_finds_the_file_case_insensitively(self, tmp_path):
        (tmp_path / 'addonsettings.TXT').write_text(SAMPLE)

        assert gamesettings.load(tmp_path) is not None


class TestFolder:
    def _folder(self, tmp_path, text=SAMPLE):
        root = tmp_path / 'live' / 'AddOns'
        root.mkdir(parents=True)
        (root.parent / 'AddOnSettings.txt').write_text(text)
        return root, make_folder(root)

    def test_scan_loads_the_file_next_to_the_addons_folder(self, tmp_path):
        root, folder = self._folder(tmp_path)
        make_installed(root, 'Azurah')

        folder.scan()

        assert folder.game_settings is not None and folder.game_settings.api_version == 101051
        addon = next(iter(folder.installed))
        info = folder.disabled_in_game(addon)
        assert info is not None and not info.everywhere and len(info.characters) == 2

    def test_no_file(self, tmp_path):
        root = tmp_path / 'AddOns'
        root.mkdir()
        addon = make_installed(root, 'Azurah')
        folder = make_folder(root)

        folder.scan()

        assert folder.game_settings is None
        assert folder.disabled_in_game(addon) is None
        assert folder.api_vs_game(addon) is None

    @pytest.mark.parametrize('declared, expected', [
        ('101052', 'newer'),
        ('101052 101053', 'newer'),
        ('101051 101052', None),   # live + PTS
        ('101052 101051', None),   # in either order
        ('101050 101051', None),
        ('101049', 'older'),
        ('101049 101050', 'older'),
    ])
    def test_api_vs_game(self, tmp_path, declared, expected):
        root, folder = self._folder(tmp_path)
        addon = make_installed(root, 'Azurah', APIVersion=declared)
        folder.scan()

        assert folder.api_vs_game(addon) == expected

    def test_bundle_is_disabled_where_all_members_are(self, tmp_path):
        root, folder = self._folder(tmp_path, '#Default\nA 0\nB 0\n#S-One\nA 0\nB 0\n#S-Two\nA 0\n')
        members = [make_installed(root / 'Bundle', name) for name in ('A', 'B')]
        bundle = AddonBundle('Bundle', root / 'Bundle', members)
        folder.scan()

        info = folder.disabled_in_game(bundle)

        assert info is not None and info.everywhere
        assert info.characters == [Character('S', 'One')]

    def test_bundle_with_an_enabled_member_is_not_disabled(self, tmp_path):
        root, folder = self._folder(tmp_path, '#Default\nA 0\n')
        members = [make_installed(root / 'Bundle', name) for name in ('A', 'B')]
        bundle = AddonBundle('Bundle', root / 'Bundle', members)
        folder.scan()

        assert folder.disabled_in_game(bundle) is None
