"""Tests for gru.addon: AddonInfo dir resolution, manifest parsing, update detection."""

import datetime
import warnings

import pytest

from gru.addon import atol, _parse_version, AddonInfo, InstalledAddon

from .conftest import make_addon_info, make_installed, write_manifest


# ---------------------------------------------------------------------------
# atol
# ---------------------------------------------------------------------------

class TestAtol:
    @pytest.mark.parametrize('value,expected', [
        ('123', 123),
        ('  123', 123),          # leading whitespace ignored
        ('123abc', 123),         # stops at first non-numeric char
        ('abc', 0),              # no leading digits -> 0
        ('', 0),
        ('007', 7),
    ])
    def test_atol(self, value, expected):
        assert atol(value) == expected


class TestParseVersion:
    def test_dotted_numeric(self):
        assert _parse_version('1.20') == (1, 20)

    def test_no_digits_returns_none(self):
        assert _parse_version('unknown') is None

    def test_non_string_returns_none(self):
        assert _parse_version(None) is None  # pyright: ignore[reportArgumentType] -- deliberately wrong type

    def test_mixed_digits_and_text_parses_leading_number(self):
        assert _parse_version('1.2beta') == (1, 2)


# ---------------------------------------------------------------------------
# AddonInfo directory resolution
# ---------------------------------------------------------------------------

class TestAddonInfoDir:
    def test_single_directory_used_directly(self):
        addon = make_addon_info(title='MyAddon', directories=['MyAddon'])
        assert addon.dir == 'MyAddon'

    def test_multiple_directories_slugifies_title(self):
        addon = make_addon_info(title='My Cool Addon', directories=['Foo', 'Bar'])
        assert addon.dir == 'MyCoolAddon'

    @pytest.mark.parametrize('reserved', ['lang', 'libs', 'EsoUI', 'gamedata', ''])
    def test_reserved_toplevel_dir_forces_slugified_title(self, reserved):
        addon = make_addon_info(title='My Addon', directories=[reserved])
        assert addon.dir == 'MyAddon'

    def test_garbage_dirs_filtered_before_counting(self):
        """__MACOSX/.DS_STORE entries must not count towards 'how many directories'."""
        addon = make_addon_info(title='MyAddon', directories=['MyAddon', '__MACOSX'])
        assert addon.dir == 'MyAddon'
        assert '__MACOSX' not in addon.metadata['directories']

    def test_slugify_strips_accents_and_symbols(self):
        # non-word chars are deleted, not replaced by a separator
        assert AddonInfo.slugify('Café! Déjà Vu?') == 'CafeDejaVu'

    def test_metadata_pops_consumed_fields(self):
        addon = make_addon_info()
        assert 'title' not in addon.metadata
        assert 'author' not in addon.metadata
        assert 'version' not in addon.metadata
        assert 'api' not in addon.metadata
        assert 'category' in addon.metadata


class TestAddonInfoCanUpdate:
    def test_not_installed_cannot_update(self):
        addon = make_addon_info()
        assert addon.can_update is False

    def test_can_update_true_when_a_registered_folder_can_update(self, tmp_path):
        addon = make_addon_info(title='MyAddon', version='2.0')
        installed = make_installed(tmp_path, 'MyAddon', Version='1.0')
        installed.link(addon)
        assert addon.can_update is True

    def test_can_update_false_when_up_to_date(self, tmp_path):
        addon = make_addon_info(title='MyAddon', version='1.0')
        installed = make_installed(tmp_path, 'MyAddon', Version='1.0')
        installed.link(addon)
        assert addon.can_update is False


# ---------------------------------------------------------------------------
# InstalledAddon manifest parsing
# ---------------------------------------------------------------------------

class TestManifestParsing:
    def test_basic_fields(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', Title='My Addon', Author='Someone', Version='3.2')
        assert addon.title == 'My Addon'
        assert addon.author == 'Someone'
        assert addon.version == '3.2'
        assert addon.dep_version == 1  # no AddOnVersion given -> atol('1') default

    def test_addonversion_used_for_dep_version(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', AddOnVersion='42')
        assert addon.dep_version == 42

    def test_missing_title_falls_back_to_manifest_stem(self, tmp_path):
        with pytest.warns(UserWarning, match='Title'):
            addon = make_installed(tmp_path, 'MyAddon', Title=None)
        assert addon.title == 'MyAddon'

    def test_missing_apiversion_raises_without_warning(self, tmp_path):
        """The APIVersion assert fires before the mandatory-key warning is reached."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with pytest.raises(AssertionError):
                make_installed(tmp_path, 'MyAddon', APIVersion=None)
        assert caught == []

    def test_is_library_true(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', IsLibrary='true')
        assert addon.is_lib is True

    def test_is_library_default_false(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        assert addon.is_lib is False

    def test_invalid_islibrary_value_asserts(self, tmp_path):
        with pytest.raises(AssertionError):
            make_installed(tmp_path, 'MyAddon', IsLibrary='maybe')

    def test_dependson_and_pcdependson_merged(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', DependsOn='LibA>=3 LibB', PCDependsOn='LibC>=1')
        dep_names = {dep.dir for dep in addon.deps}
        assert dep_names == {'LibA', 'LibB', 'LibC'}
        lib_a = next(dep for dep in addon.deps if dep.dir == 'LibA')
        assert lib_a.dep_version == 3
        lib_b = next(dep for dep in addon.deps if dep.dir == 'LibB')
        assert lib_b.dep_version == 0

    def test_optionaldependson_kept_separate(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', OptionalDependsOn='LibOpt>=5')
        assert [d.dir for d in addon.deps] == []
        assert [(d.dir, d.dep_version) for d in addon.optdeps] == [('LibOpt', 5)]

    def test_multiline_field_concatenated(self, tmp_path):
        addon_dir = tmp_path / 'MyAddon'
        addon_dir.mkdir()
        manifest = (
            '## Title: MyAddon\n'
            '## APIVersion: 100035\n'
            '## Description: First line\n'
            '## Description: Second line\n'
        )
        (addon_dir / 'MyAddon.txt').write_text(manifest)
        addon = InstalledAddon(addon_dir)
        assert addon.metadata['description'] == 'First line Second line'

    def test_no_manifest_raises_filenotfound(self, tmp_path):
        addon_dir = tmp_path / 'Empty'
        addon_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            InstalledAddon(addon_dir)

    def test_addon_extension_also_recognised(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', ext='.addon')
        assert addon.title == 'MyAddon'


class TestInstalledAddonCanUpdate:
    def test_not_linked_cannot_update(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        assert addon.can_update is False

    def test_numeric_version_compared(self, tmp_path):
        installed = make_installed(tmp_path, 'MyAddon', Version='1.2')
        upstream = make_addon_info(title='MyAddon', version='1.10')
        installed.link(upstream)
        assert installed.can_update is True  # 1.10 numeric > 1.2, not a string compare

    def test_up_to_date_numeric_version(self, tmp_path):
        installed = make_installed(tmp_path, 'MyAddon', Version='2.0')
        upstream = make_addon_info(title='MyAddon', version='2.0')
        installed.link(upstream)
        assert installed.can_update is False

    def test_non_numeric_version_falls_back_to_date(self, tmp_path):
        installed = make_installed(tmp_path, 'MyAddon', Version='unknown')
        upstream = make_addon_info(title='MyAddon', version='unknown', date=datetime.datetime(2100, 1, 1))
        installed.link(upstream)
        # manifest file was just written -> its mtime is way before the year 2100 upstream date
        assert installed.can_update is True

    def test_non_string_version_falls_back_to_date(self, tmp_path):
        installed = make_installed(tmp_path, 'MyAddon', Version='1.0')
        upstream = make_addon_info(title='MyAddon', version=None, date=datetime.datetime(2100, 1, 1))
        installed.link(upstream)
        assert installed.can_update is True


class TestInstalledAddonVersionRank:
    def test_no_other_folder_is_trivially_active(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon', Version='1.0')
        addon.link(make_addon_info(title='MyAddon', version='1.0'))
        assert addon.version_rank == 'active'
        assert addon.is_superseded is False

    def test_higher_version_is_active(self, tmp_path):
        a = make_installed(tmp_path, 'CopyA', Version='2.0')
        b = make_installed(tmp_path, 'CopyB', Version='1.0')
        upstream = make_addon_info(title='LibShared')
        a.link(upstream)
        b.link(upstream)
        assert a.version_rank == 'active'
        assert a.is_superseded is False

    def test_lower_version_is_superseded(self, tmp_path):
        a = make_installed(tmp_path, 'CopyA', Version='2.0')
        b = make_installed(tmp_path, 'CopyB', Version='1.0')
        upstream = make_addon_info(title='LibShared')
        a.link(upstream)
        b.link(upstream)
        assert b.version_rank == 'superseded'
        assert b.is_superseded is True

    def test_tie_is_active_for_both(self, tmp_path):
        a = make_installed(tmp_path, 'CopyA', Version='1.0')
        b = make_installed(tmp_path, 'CopyB', Version='1.0')
        upstream = make_addon_info(title='LibShared')
        a.link(upstream)
        b.link(upstream)
        assert a.version_rank == 'active'
        assert b.version_rank == 'active'

    def test_unparseable_sibling_version_is_unknown(self, tmp_path):
        a = make_installed(tmp_path, 'CopyA', Version='2.0')
        b = make_installed(tmp_path, 'CopyB', Version='unknown')
        upstream = make_addon_info(title='LibShared')
        a.link(upstream)
        b.link(upstream)
        assert a.version_rank == ''
        assert a.is_superseded is False


class TestInstalledAddonFiles:
    def test_files_lists_relative_paths(self, tmp_path):
        addon_dir = write_manifest(tmp_path, 'MyAddon')
        (addon_dir / 'lib.lua').write_text('-- lua')
        addon = InstalledAddon(addon_dir)
        names = {str(p) for p in addon.files}
        assert 'MyAddon.txt' in names
        assert 'lib.lua' in names

    def test_hidden_and_garbage_files_excluded(self, tmp_path):
        addon_dir = write_manifest(tmp_path, 'MyAddon')
        (addon_dir / '.hidden').write_text('nope')
        macosx = addon_dir / '__MACOSX'
        macosx.mkdir()
        (macosx / 'junk').write_text('nope')
        addon = InstalledAddon(addon_dir)
        names = {str(p) for p in addon.files}
        assert not any(n.startswith('.') for n in names)
        assert not any('__MACOSX' in n for n in names)


class TestAddonInfoRegistration:
    def test_register_and_deregister(self, tmp_path):
        addon = make_addon_info(title='MyAddon')
        installed = make_installed(tmp_path, 'MyAddon')
        addon.register(installed)
        assert addon.folders[installed.folder] is installed
        addon.deregister(installed)
        assert installed.folder not in addon.folders

    def test_link_sets_id_and_registers(self, tmp_path):
        addon = make_addon_info(id_=42, title='MyAddon')
        installed = make_installed(tmp_path, 'MyAddon')
        installed.link(addon)
        assert installed.id == 42
        assert installed.infos is addon
        assert addon.folders[installed.folder] is installed
