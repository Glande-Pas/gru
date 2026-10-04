"""Tests for gru.config: encoding detection, config load/save/update, translation loading."""

import builtins
import pathlib

import pytest

import gru.cli as cli_mod
import gru.config as config_mod
import gru.install as install_mod
import gru.app as app_mod
from gru.config import encoding_open, load_config, display_config, update_config, install_translation


# ---------------------------------------------------------------------------
# Autouse safety net (conftest._no_real_user_config): no test, even one that mocks nothing
# itself, may ever compute a real path under the user's actual home directory.
# ---------------------------------------------------------------------------

class TestUserConfigSafetyNet:
    def test_user_config_is_redirected_under_tmp_path(self, tmp_path):
        path = config_mod.user_config('probe.txt')
        assert path.is_relative_to(tmp_path)
        assert not path.is_relative_to(pathlib.Path.home())

    def test_user_cache_is_redirected_under_tmp_path(self, tmp_path):
        path = config_mod.user_cache('probe.txt')
        assert path.is_relative_to(tmp_path)
        assert not path.is_relative_to(pathlib.Path.home())

    def test_cli_module_reference_is_also_redirected(self, tmp_path):
        assert cli_mod.user_config('probe.txt').is_relative_to(tmp_path)
        assert cli_mod.user_cache('probe.txt').is_relative_to(tmp_path)

    def test_install_module_reference_is_also_redirected(self, tmp_path):
        assert install_mod.user_config('probe.txt').is_relative_to(tmp_path)
        assert install_mod.user_cache('probe.txt').is_relative_to(tmp_path)

    def test_app_module_reference_is_also_redirected(self, tmp_path):
        assert app_mod.user_config('probe.txt').is_relative_to(tmp_path)

    def test_every_loaded_gru_module_with_user_config_is_redirected(self, tmp_path):
        """Regression: gru.app got its own `from .config import user_config` and wasn't on the
        conftest fixture's (then-)fixed module list, so it leaked real rows into the user's actual
        changes.csv before this was caught. The fixture now discovers every gru.* module dynamically
        instead of a hardcoded list -- this asserts that generically, so it stays a real regression
        guard for the *next* new module too, not just the ones enumerated here by name."""
        import sys
        checked = 0
        for name, mod in sys.modules.items():
            if mod is None or not (name == 'gru' or name.startswith('gru.')):
                continue
            if hasattr(mod, 'user_config'):
                checked += 1
                assert mod.user_config('probe.txt').is_relative_to(tmp_path), f'{name}.user_config leaks'
            if hasattr(mod, 'user_cache'):
                checked += 1
                assert mod.user_cache('probe.txt').is_relative_to(tmp_path), f'{name}.user_cache leaks'
        assert checked > 0  # sanity: the scan actually found something to check


# ---------------------------------------------------------------------------
# encoding_open
# ---------------------------------------------------------------------------

class TestEncodingOpen:
    def test_plain_utf8(self, tmp_path):
        f = tmp_path / 'plain.txt'
        f.write_text('hello world', encoding='utf-8')
        with encoding_open(f) as fh:
            assert fh.read() == 'hello world'

    def test_utf8_bom_stripped_and_decoded(self, tmp_path):
        f = tmp_path / 'bom.txt'
        f.write_bytes(b'\xef\xbb\xbfhello')
        with encoding_open(f) as fh:
            content = fh.read()
        assert content == 'hello'
        assert not content.startswith('﻿')


# ---------------------------------------------------------------------------
# load_config / save_config / display_config / update_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_defaults_present_without_file(self, isolated_user_dirs):
        config = load_config(isolated_user_dirs['config_file'])
        assert config.get('api', 'endpoint').startswith('https://api.mmoui.com')
        assert config.getint('api', 'version') == 3

    def test_explicit_root_is_kept(self, tmp_path, isolated_user_dirs):
        config_file = isolated_user_dirs['config_file']
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text('[ESO.addons]\nroot = ' + str(tmp_path) + '\n')
        config = load_config(config_file)
        assert config.get('ESO.addons', 'root') == str(tmp_path)

    def test_no_guess_found_leaves_root_empty(self, tmp_path, monkeypatch, isolated_user_dirs):
        # user_home() resolves under a throwaway dir with none of the guessed AddOns paths present
        monkeypatch.setattr(config_mod, 'user_home', lambda: tmp_path / 'nonexistent-home')
        config = load_config(isolated_user_dirs['config_file'])
        assert config.get('ESO.addons', 'root').strip() == ''


class TestAddonsDirGuess:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch, isolated_user_dirs):
        isolated_user_dirs['config_file'].parent.mkdir(parents=True, exist_ok=True)
        home = tmp_path / 'home'
        monkeypatch.setattr(config_mod, 'user_home', lambda: home)
        return home

    @pytest.mark.parametrize('location', [
        'Documents',
        'OneDrive/Documents',
        '.local/share/Steam/steamapps/compatdata/306130/pfx/drive_c/users/steamuser/Documents',
        ('.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/306130/pfx/'
         'drive_c/users/steamuser/Documents'),
        'snap/steam/common/.local/share/Steam/steamapps/compatdata/306130/pfx/drive_c/users/steamuser/Documents',
        '.wine/drive_c/users/someone/Documents',
        'Games/the-elder-scrolls-online/drive_c/users/someone/Documents',
        '.var/app/com.usebottles.bottles/data/bottles/bottles/ESO/drive_c/users/someone/Documents',
    ])
    def test_known_location_is_found(self, home, isolated_user_dirs, location):
        addons = home / location / 'Elder Scrolls Online/live/AddOns'
        addons.mkdir(parents=True)
        config = load_config(isolated_user_dirs['config_file'])
        assert config.get('ESO.addons', 'root') == str(addons.resolve())

    def test_extra_steam_library_is_found(self, tmp_path, home, isolated_user_dirs):
        library = tmp_path / 'mnt' / 'SteamLibrary'
        addons = (library / 'steamapps/compatdata/306130/pfx' /
                  'drive_c/users/steamuser/Documents/Elder Scrolls Online/live/AddOns')
        addons.mkdir(parents=True)
        steamapps = home / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps'
        steamapps.mkdir(parents=True)
        (steamapps / 'libraryfolders.vdf').write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(steamapps.parent) + '"\n\t}\n'
            '\t"1"\n\t{\n\t\t"path"\t\t"' + str(library) + '"\n\t}\n}\n')
        config = load_config(isolated_user_dirs['config_file'])
        assert config.get('ESO.addons', 'root') == str(addons.resolve())

    def test_native_install_wins_over_proton(self, home, isolated_user_dirs):
        native = home / 'Documents/Elder Scrolls Online/live/AddOns'
        native.mkdir(parents=True)
        (home / '.local/share/Steam/steamapps/compatdata/306130/pfx/drive_c/users/steamuser'
         / 'Documents/Elder Scrolls Online/live/AddOns').mkdir(parents=True)
        config = load_config(isolated_user_dirs['config_file'])
        assert config.get('ESO.addons', 'root') == str(native.resolve())


class TestDisplayUpdateConfig:
    def test_display_config_roundtrip(self, isolated_user_dirs):
        config = load_config(isolated_user_dirs['config_file'])
        shown = display_config(config, 'ESO')
        assert shown['addons.patch_updates'] == 'on'
        assert shown['addons.remove_saved_variables'] == 'ask'

    def test_update_config_allows_three_state_enum_transitions(self, isolated_user_dirs):
        """remove_saved_variables is yes/no/ask -- none of which is 'on'/'off', so
        update_config()'s boolean-mismatch guard must never block moving between them."""
        config = load_config(isolated_user_dirs['config_file'])
        update_config(config, 'ESO', {'addons.remove_saved_variables': 'yes'})
        assert config.get('ESO.addons', 'remove_saved_variables') == 'yes'
        update_config(config, 'ESO', {'addons.remove_saved_variables': 'no'})
        assert config.get('ESO.addons', 'remove_saved_variables') == 'no'
        update_config(config, 'ESO', {'addons.remove_saved_variables': 'ask'})
        assert config.get('ESO.addons', 'remove_saved_variables') == 'ask'

    def test_update_config_changes_value(self, isolated_user_dirs):
        config = load_config(isolated_user_dirs['config_file'])
        update_config(config, 'ESO', {'addons.optional': 'on'})
        assert config.get('ESO.addons', 'optional') == 'on'

    def test_update_config_rejects_type_mismatch(self, isolated_user_dirs):
        """A boolean option can't silently become a non-boolean value or vice versa."""
        config = load_config(isolated_user_dirs['config_file'])
        with pytest.raises(ValueError):
            update_config(config, 'ESO', {'addons.optional': 'not-a-bool'})

    def test_update_config_unknown_section_raises_keyerror(self, isolated_user_dirs):
        config = load_config(isolated_user_dirs['config_file'])
        with pytest.raises(KeyError):
            update_config(config, 'ESO', {'nosuchsection.optional': 'on'})


# ---------------------------------------------------------------------------
# install_translation
# ---------------------------------------------------------------------------

class TestInstallTranslation:
    def test_no_language_env_gives_null_translation(self, monkeypatch, tmp_path):
        for var in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
            monkeypatch.delenv(var, raising=False)
        result = install_translation('gru', tmp_path)
        assert result is None  # NullTranslations().install() returns None

    def test_no_matching_mo_file_falls_back_to_null(self, monkeypatch, tmp_path):
        monkeypatch.setenv('LANGUAGE', 'xx_XX')
        monkeypatch.delenv('LC_ALL', raising=False)
        monkeypatch.delenv('LC_MESSAGES', raising=False)
        monkeypatch.delenv('LANG', raising=False)
        install_translation('gru', tmp_path)
        # Falling back to Null must not leave a foreign gettext installed as `_`
        assert _gettext('hello') == 'hello'

    def test_matching_mo_file_is_loaded(self, monkeypatch, tmp_path):
        """Regression: a found .mo file used to be discarded in favour of NullTranslations."""
        monkeypatch.setenv('LANGUAGE', 'xx')
        monkeypatch.delenv('LC_ALL', raising=False)
        monkeypatch.delenv('LC_MESSAGES', raising=False)
        monkeypatch.delenv('LANG', raising=False)

        locale_dir = tmp_path / 'xx' / 'LC_MESSAGES'
        locale_dir.mkdir(parents=True)
        mo_path = locale_dir / 'gru.mo'
        _write_minimal_mo(mo_path, {'hello': 'bonjour'})

        install_translation('gru', tmp_path)

        assert _gettext('hello') == 'bonjour'


def _gettext(message: str) -> str:
    # gettext's install() puts `_` into builtins, which type checkers don't know about
    return getattr(builtins, '_')(message)


def _write_minimal_mo(path: pathlib.Path, catalog: dict[str, str]) -> None:
    """Compile `catalog` into a minimal .mo file, avoiding a dependency on msgfmt."""
    import struct

    keys = sorted(catalog)
    offsets = []
    ids, strs = b'', b''
    for key in keys:
        id_bytes = key.encode('utf-8')
        str_bytes = catalog[key].encode('utf-8')
        offsets.append((len(ids), len(id_bytes), len(strs), len(str_bytes)))
        ids += id_bytes + b'\x00'
        strs += str_bytes + b'\x00'

    keystart = 7 * 4 + 16 * len(keys)
    valuestart = keystart + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]

    output = struct.pack('Iiiiiii', 0x950412de, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
    output += struct.pack(f'{len(koffsets)}i', *koffsets)
    output += struct.pack(f'{len(voffsets)}i', *voffsets)
    output += ids
    output += strs
    path.write_bytes(output)


class TestTargets:
    def test_root_key(self):
        assert config_mod.root_key('live') == 'root'
        assert config_mod.root_key('pts') == 'pts_root'

    def test_candidates_are_per_target(self, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        live = list(config_mod.addons_dir_candidates('live'))
        pts = list(config_mod.addons_dir_candidates('pts'))
        assert live and pts
        assert all('/live/' in str(p) for p in live)
        assert all('/pts/' in str(p) for p in pts)
