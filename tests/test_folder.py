"""Tests for gru.install.Folder: lookup, dependency tracking, scanning, removal.
Network-touching methods (unpack/install/update) aren't covered here."""

import inspect

import pytest

from gru.addon import Dependency
from gru.install import Folder

from .conftest import make_folder, make_installed, make_addon_info, StubAPI, StubAddon


@pytest.fixture
def populated_folder(addon_root, folder):
    """A Folder with two real installed addons already scanned in."""
    make_installed(addon_root, 'MyAddon', Title='My Addon')
    make_installed(addon_root, 'LibFoo', Title='LibFoo', IsLibrary='true')
    folder.scan()
    return folder


# ---------------------------------------------------------------------------
# name / dir / id -- direct generator-returning lookups
# ---------------------------------------------------------------------------

class TestFolderScalarLookups:
    def test_name_is_a_generator(self, populated_folder):
        """Must be wrapped in list() before checking truthiness -- see Folder.find."""
        result = populated_folder.name('my addon')
        assert inspect.isgenerator(result)

    def test_name_case_insensitive_exact_match(self, populated_folder):
        [found] = list(populated_folder.name('MY ADDON'))
        assert found.title == 'My Addon'

    def test_name_no_match_yields_nothing(self, populated_folder):
        assert list(populated_folder.name('nonexistent')) == []

    def test_dir_case_insensitive_exact_match(self, populated_folder):
        [found] = list(populated_folder.dir('libfoo'))
        assert found.dir == 'LibFoo'

    def test_id_matches_by_scalar_equality(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        folder.scan()
        # link() after scanning: scan() rebuilds InstalledAddon objects from disk, so
        # linking an addon before scan() only links a throwaway object that gets discarded.
        [addon] = list(folder.dir('MyAddon'))
        addon.link(make_addon_info(id_=99, title='MyAddon'))
        [found] = list(folder.id(99))
        assert found.dir == 'MyAddon'


# ---------------------------------------------------------------------------
# find / search -- exercises the exact bug chain fixed earlier this session:
# generator truthiness in find(), and dict_values.values() in search()
# ---------------------------------------------------------------------------

class TestFolderFind:
    def test_find_by_exact_name_case_insensitive(self, populated_folder, stub_api):
        result = populated_folder.find('my addon', stub_api)
        assert [a.title for a in result] == ['My Addon']

    def test_find_by_exact_dir_when_name_does_not_match(self, populated_folder, stub_api):
        result = populated_folder.find('LibFoo', stub_api)
        assert [a.dir for a in result] == ['LibFoo']

    def test_find_falls_through_to_fuzzy_search_without_crashing(self, populated_folder, stub_api):
        result = populated_folder.find('yaddon', stub_api)  # fuzzy match against 'My Addon'
        assert any(a.title == 'My Addon' for a in result)

    def test_find_with_no_match_anywhere_falls_back_to_api_search(self, populated_folder):
        api = StubAPI()
        result = populated_folder.find('completely-unrelated-xyz', api)
        assert result == []


class TestFolderSearch:
    def test_search_does_not_raise(self, populated_folder):
        """Regression: used to raise AttributeError (dict_values.values())."""
        result = populated_folder.search('addon')
        assert any(a.title == 'My Addon' for a in result)

    def test_search_matches_dir_as_well_as_title(self, populated_folder):
        result = populated_folder.search('libfoo')
        assert any(a.dir == 'LibFoo' for a in result)

    def test_search_empty_folder_returns_empty_list(self, folder):
        assert folder.search('anything') == []


# ---------------------------------------------------------------------------
# Dependency tracking: missing_deps / depcount / unused_deps
# ---------------------------------------------------------------------------

class TestDependencyTracking:
    def test_missing_deps_reports_uninstalled_dependency(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', DependsOn='LibMissing>=1')
        folder.scan()
        missing = folder.missing_deps(folder.installed)
        assert [d.dir for d in missing] == ['LibMissing']

    def test_missing_deps_empty_when_dependency_installed(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', DependsOn='LibFoo>=1')
        make_installed(addon_root, 'LibFoo', AddOnVersion='2')
        folder.scan()
        assert folder.missing_deps(folder.installed) == []

    def test_missing_deps_respects_minimum_version(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', DependsOn='LibFoo>=5')
        make_installed(addon_root, 'LibFoo', AddOnVersion='2')  # too old
        folder.scan()
        missing = folder.missing_deps(folder.installed)
        assert [d.dir for d in missing] == ['LibFoo']

    def test_missing_deps_excludes_optional_by_default(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', OptionalDependsOn='LibOpt>=1')
        folder.scan()
        assert folder.missing_deps(folder.installed, opt=False) == []
        assert [d.dir for d in folder.missing_deps(folder.installed, opt=True)] == ['LibOpt']

    def test_depcount_counts_dependents(self, addon_root, folder):
        make_installed(addon_root, 'A', DependsOn='LibFoo>=1')
        make_installed(addon_root, 'B', DependsOn='LibFoo>=1')
        lib = make_installed(addon_root, 'LibFoo')
        folder.scan()
        lib = next(iter(folder.dir('LibFoo')))
        assert folder.depcount(lib) == 2

    def test_depcount_optional_counted_only_when_requested(self, addon_root, folder):
        make_installed(addon_root, 'A', OptionalDependsOn='LibFoo>=1')
        make_installed(addon_root, 'LibFoo')
        folder.scan()
        lib = next(iter(folder.dir('LibFoo')))
        assert folder.depcount(lib, opt=False) == 0
        assert folder.depcount(lib, opt=True) == 1

    def test_unused_deps_flags_zero_dependent_library(self, addon_root, folder):
        make_installed(addon_root, 'LibOrphan', IsLibrary='true')
        folder.scan()
        unused = folder.unused_deps(folder.installed)
        assert [a.dir for a in unused] == ['LibOrphan']

    def test_unused_deps_ignores_non_library_addons(self, addon_root, folder):
        make_installed(addon_root, 'RegularAddon', IsLibrary='false')
        folder.scan()
        assert folder.unused_deps(folder.installed) == []

    def test_unused_deps_ignores_library_with_dependent(self, addon_root, folder):
        make_installed(addon_root, 'A', DependsOn='LibFoo>=1')
        make_installed(addon_root, 'LibFoo', IsLibrary='true')
        folder.scan()
        assert folder.unused_deps(folder.installed) == []


# ---------------------------------------------------------------------------
# remove / remove_unused_deps
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_deletes_folder_and_registry_entry(self, populated_folder):
        [addon] = list(populated_folder.dir('MyAddon'))
        folder_path = addon.folder
        populated_folder.remove(addon)
        assert not folder_path.exists()
        assert list(populated_folder.dir('MyAddon')) == []

    def test_remove_deregisters_from_linked_addoninfo(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        info = make_addon_info(id_=1, title='MyAddon')
        addon.link(info)
        addon_folder = addon.folder
        folder.remove(addon)
        assert addon_folder not in info.folders

    def test_remove_by_addoninfo_uses_first_registered_folder(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        info = make_addon_info(id_=1, title='MyAddon')
        addon.link(info)
        addon_folder = addon.folder
        folder.remove(info)  # pass the AddonInfo, not the InstalledAddon
        assert not addon_folder.exists()

    def test_remove_not_installed_addoninfo_raises(self):
        info = make_addon_info(id_=1, title='NeverInstalled')
        f = Folder.__new__(Folder)
        with pytest.raises(ValueError, match='not installed'):
            f.remove(info)

    def test_remove_with_deps_cleans_orphaned_library(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', DependsOn='LibFoo>=1')
        make_installed(addon_root, 'LibFoo', IsLibrary='true')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        removed_count = folder.remove(addon, deps=True)
        assert removed_count == 1
        assert list(folder.dir('LibFoo')) == []

    def test_remove_unused_deps_cascades(self, addon_root, folder):
        """LibA depends on LibB; removing LibA as unused should also free LibB."""
        make_installed(addon_root, 'LibA', IsLibrary='true', DependsOn='LibB>=1')
        make_installed(addon_root, 'LibB', IsLibrary='true')
        folder.scan()
        removed = folder.remove_unused_deps()
        assert removed == 2
        assert list(folder.installed) == []


# ---------------------------------------------------------------------------
# scan / _scan
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_finds_top_level_addon(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        folder.scan()
        assert {a.dir for a in folder.installed} == {'MyAddon'}

    def test_scan_links_addon_found_in_api(self, addon_root, folder):
        """StubAddon has no .register(), so link() needs a real AddonInfo here instead."""
        make_installed(addon_root, 'MyAddon')
        info = make_addon_info(id_=7, title='MyAddon')

        class LinkableApi:
            def dir(self, name):
                return info

        folder.scan(LinkableApi())
        [addon] = list(folder.installed)
        assert addon.id == 7

    def test_scan_warns_for_toplevel_addon_missing_from_api(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        api = StubAPI()  # empty -- MyAddon not found
        with pytest.warns(UserWarning, match='not found in database'):
            folder.scan(api)
        [addon] = list(folder.installed)
        assert addon.id is None

    def test_scan_finds_nested_addon_with_parent_link(self, addon_root, folder):
        make_installed(addon_root, 'Parent')
        make_installed(addon_root / 'Parent', 'Child')
        folder.scan()
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['Child'].parent is by_dir['Parent']

    def test_scan_ignores_directory_without_manifest(self, addon_root, folder):
        (addon_root / 'NotAnAddon').mkdir()
        folder.scan()
        assert list(folder.installed) == []

    def test_rescan_replaces_previous_results(self, addon_root, folder):
        make_installed(addon_root, 'First')
        folder.scan()
        assert {a.dir for a in folder.installed} == {'First'}

        make_installed(addon_root, 'Second')
        folder.scan()
        assert {a.dir for a in folder.installed} == {'First', 'Second'}


# ---------------------------------------------------------------------------
# unpack()'s extracted, network-free decision logic
# ---------------------------------------------------------------------------

class TestSuggestedFilename:
    def test_uses_content_disposition_filename(self, folder):
        headers = {'content-disposition': 'attachment; filename="Foo.zip"'}
        assert folder._suggested_filename(headers, 'default.zip') == 'Foo.zip'

    def test_falls_back_to_default_without_header(self, folder):
        assert folder._suggested_filename({}, 'default.zip') == 'default.zip'


class TestCacheIsFresh:
    def test_missing_file_is_not_fresh(self, folder, tmp_path):
        headers = {'last-modified': 'Wed, 21 Oct 2015 07:28:00 GMT', 'content-length': '5'}
        assert folder._cache_is_fresh(headers, tmp_path / 'nope.zip') is False

    def test_no_last_modified_header_is_not_fresh(self, folder, tmp_path):
        zippath = tmp_path / 'cached.zip'
        zippath.write_bytes(b'12345')
        assert folder._cache_is_fresh({'content-length': '5'}, zippath) is False

    def test_matching_size_and_newer_mtime_is_fresh(self, folder, tmp_path):
        import os
        zippath = tmp_path / 'cached.zip'
        zippath.write_bytes(b'12345')
        os.utime(zippath, (2000000000, 2000000000))  # 2033 -- well after the header date
        headers = {'last-modified': 'Wed, 21 Oct 2015 07:28:00 GMT', 'content-length': '5'}
        assert folder._cache_is_fresh(headers, zippath) is True

    def test_size_mismatch_is_not_fresh(self, folder, tmp_path):
        import os
        zippath = tmp_path / 'cached.zip'
        zippath.write_bytes(b'12345')
        os.utime(zippath, (2000000000, 2000000000))
        headers = {'last-modified': 'Wed, 21 Oct 2015 07:28:00 GMT', 'content-length': '999'}
        assert folder._cache_is_fresh(headers, zippath) is False

    def test_cache_older_than_last_modified_is_not_fresh(self, folder, tmp_path):
        import os
        zippath = tmp_path / 'cached.zip'
        zippath.write_bytes(b'12345')
        os.utime(zippath, (1000000000, 1000000000))  # 2001 -- before the header date
        headers = {'last-modified': 'Wed, 21 Oct 2015 07:28:00 GMT', 'content-length': '5'}
        assert folder._cache_is_fresh(headers, zippath) is False
