"""Tests for gru.addon: AddonInfo dir resolution, manifest parsing, update detection."""

import io
import zlib
import zipfile
import datetime
import warnings

import pytest

from gru.addon import atol, _parse_version, strip_eso_text, file_crc32, AddonInfo, InstalledAddon, AddonBundle

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


class TestStripEsoText:
    def test_closed_tag(self):
        assert strip_eso_text('|cFF0000Red|r Text') == 'Red Text'

    def test_missing_closing_tag_auto_closes_at_end_of_string(self):
        assert strip_eso_text('|cFF0000Unterminated Red Text') == 'Unterminated Red Text'

    def test_missing_closing_tag_auto_closes_before_next_color_code(self):
        assert strip_eso_text('|cFF0000Red|c00FF00Green') == 'RedGreen'

    def test_plain_text_unaffected(self):
        assert strip_eso_text('Plain title, no markup') == 'Plain title, no markup'


class TestFileCrc32:
    def test_matches_zlib_crc32_directly(self, tmp_path):
        content = b'hello world' * 100
        path = tmp_path / 'a.txt'
        path.write_bytes(content)

        assert file_crc32(path) == zlib.crc32(content)

    def test_matches_zipfile_own_crc_for_the_same_content(self, tmp_path):
        """Must agree with what a zip archive itself records for the same bytes."""
        content = b'## Title: MyAddon\n## Author: Test\n' * 50
        path = tmp_path / 'MyAddon.txt'
        path.write_bytes(content)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('MyAddon/MyAddon.txt', content)
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            [info] = zf.infolist()

        assert file_crc32(path) == info.CRC

    def test_empty_file(self, tmp_path):
        path = tmp_path / 'empty.txt'
        path.write_bytes(b'')

        assert file_crc32(path) == 0

    def test_content_spanning_multiple_read_chunks(self, tmp_path):
        """The running CRC must accumulate across chunks, not just handle a single read()."""
        content = bytes((i * 7) % 256 for i in range(65536 * 3 + 12345))  # several 64KB chunks
        path = tmp_path / 'big.dat'
        path.write_bytes(content)

        assert file_crc32(path) == zlib.crc32(content)


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

    def test_invalid_islibrary_value_warns_and_defaults_false(self, tmp_path):
        with pytest.warns(UserWarning, match='Unexpected value for IsLibrary'):
            addon = make_installed(tmp_path, 'MyAddon', IsLibrary='maybe')
        assert addon.is_lib is False

    def test_duplicate_islibrary_lines_tolerated(self, tmp_path):
        addon_dir = tmp_path / 'MyAddon'
        addon_dir.mkdir()
        manifest = (
            '## Title: MyAddon\n'
            '## APIVersion: 100035\n'
            '## IsLibrary: true\n'
            '## IsLibrary: true\n'
        )
        (addon_dir / 'MyAddon.txt').write_text(manifest)
        addon = InstalledAddon(addon_dir)
        assert addon.is_lib is True

    def test_conflicting_islibrary_lines_keeps_last(self, tmp_path):
        addon_dir = tmp_path / 'MyAddon'
        addon_dir.mkdir()
        manifest = (
            '## Title: MyAddon\n'
            '## APIVersion: 100035\n'
            '## IsLibrary: true\n'
            '## IsLibrary: false\n'
        )
        (addon_dir / 'MyAddon.txt').write_text(manifest)
        addon = InstalledAddon(addon_dir)
        assert addon.is_lib is False

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


class TestAddonBundle:
    def test_folder_is_the_wrapper_not_a_members_own_dir(self, tmp_path):
        main = make_installed(tmp_path / 'Bundle', 'Bundle')
        extra = make_installed(tmp_path / 'Bundle', 'BundleExtra')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [main, extra])
        assert bundle.folder == tmp_path / 'Bundle'

    def test_parent_is_always_none(self, tmp_path):
        main = make_installed(tmp_path / 'Bundle', 'Bundle')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [main])
        assert bundle.parent is None

    def test_locked_is_its_own_flag_defaulting_false(self, tmp_path):
        """Locking a bundle means 'don't update it', independent of any member's own state --
        it's not derived from members at all."""
        main = make_installed(tmp_path / 'Bundle', 'Bundle')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [main])
        assert bundle.locked is False
        bundle.locked = True
        assert bundle.locked is True

    def test_is_lib_true_only_if_every_member_is(self, tmp_path):
        lib1 = make_installed(tmp_path / 'Bundle', 'Lib1', IsLibrary='true')
        lib2 = make_installed(tmp_path / 'Bundle', 'Lib2', IsLibrary='true')
        assert AddonBundle('Bundle', tmp_path / 'Bundle', [lib1, lib2]).is_lib is True

    def test_is_lib_false_if_any_member_is_not(self, tmp_path):
        lib = make_installed(tmp_path / 'Bundle', 'Lib1', IsLibrary='true')
        notlib = make_installed(tmp_path / 'Bundle', 'Main')
        assert AddonBundle('Bundle', tmp_path / 'Bundle', [lib, notlib]).is_lib is False

    def test_deps_is_unique_union_of_members_deps(self, tmp_path):
        a = make_installed(tmp_path / 'Bundle', 'A', DependsOn='Shared>=1 OnlyA')
        b = make_installed(tmp_path / 'Bundle', 'B', DependsOn='Shared>=1 OnlyB')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [a, b])
        assert {dep.dir for dep in bundle.deps} == {'Shared', 'OnlyA', 'OnlyB'}

    def test_optdeps_is_unique_union_of_members_optdeps(self, tmp_path):
        a = make_installed(tmp_path / 'Bundle', 'A', OptionalDependsOn='SharedOpt OnlyAOpt')
        b = make_installed(tmp_path / 'Bundle', 'B', OptionalDependsOn='SharedOpt OnlyBOpt')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [a, b])
        assert {dep.dir for dep in bundle.optdeps} == {'SharedOpt', 'OnlyAOpt', 'OnlyBOpt'}

    def test_link_cascades_to_every_member(self, tmp_path):
        main = make_installed(tmp_path / 'Bundle', 'Bundle')
        extra = make_installed(tmp_path / 'Bundle', 'BundleExtra')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [main, extra])
        upstream = make_addon_info(id_=1, title='Bundle')

        bundle.link(upstream)

        assert bundle.infos is upstream
        assert main.infos is upstream
        assert extra.infos is upstream

    def test_can_update_true_if_any_member_can(self, tmp_path):
        stale = make_installed(tmp_path / 'Bundle', 'Bundle', Version='1.0')
        current = make_installed(tmp_path / 'Bundle', 'BundleExtra', Version='2.0')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [stale, current])
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0')
        bundle.link(upstream)
        assert bundle.can_update is True

    def test_can_update_false_if_no_member_can(self, tmp_path):
        current1 = make_installed(tmp_path / 'Bundle', 'Bundle', Version='2.0')
        current2 = make_installed(tmp_path / 'Bundle', 'BundleExtra', Version='2.0')
        bundle = AddonBundle('Bundle', tmp_path / 'Bundle', [current1, current2])
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0')
        bundle.link(upstream)
        assert bundle.can_update is False
