"""Tests for gru.install.Folder: lookup, dependency tracking, scanning, removal,
and (via faked requests.head/get + user_cache) update()/install_deps()."""

import configparser
import inspect
import io
import pathlib
import warnings
import zipfile

import pytest

import gru.install as install_mod
from gru.install import Folder
from gru.addon import AddonBundle
from gru.patch import addon_diff

from .conftest import as_api, make_api, make_installed, make_addon_info, write_manifest, StubAPI


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for fname, content in entries.items():
            zf.writestr(fname, content)
    return buf.getvalue()


class _FakeResponse:
    content: bytes  # only set on responses that carry a body

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1024):
        yield self.content

    def raise_for_status(self):
        pass


def _touch_cache_path(base, *parts):
    path = base / 'cache'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _touch_config_path(base, *parts):
    path = base / 'config'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _mock_download(monkeypatch, tmp_path, zip_bytes: bytes) -> None:
    headers = {'content-length': str(len(zip_bytes))}
    monkeypatch.setattr(install_mod.requests, 'head',
                        lambda url, allow_redirects=True: _FakeResponse(headers=headers))
    monkeypatch.setattr(install_mod.requests, 'get',
                        lambda url, stream=True, allow_redirects=True: _FakeResponse(content=zip_bytes))
    monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(tmp_path, *parts))
    monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))


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

    def test_find_falls_back_to_api_search_by_id_without_crashing(self, populated_folder):
        """Regression: sum() over self.id(...) generators used to crash (TypeError: can only
        concatenate list to list, not generator) since self.id() returns a generator, not a
        list. Needs api.search() to actually return something to exercise the sum() call at
        all -- an empty result (as in the test above) never reaches the buggy line."""
        class RemoteMatch:
            id = 999

        class ApiWithRemoteMatch(StubAPI):
            def search(self, term):
                return [RemoteMatch()]

        result = populated_folder.find('nothing-matches-locally', ApiWithRemoteMatch())
        assert result == []  # no local addon has id=999, but it must not crash getting there


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

    def test_missing_deps_does_not_crash_when_a_bundle_shares_a_members_dir(self, addon_root, folder):
        """Real crash (AttributeError: 'AddonBundle' object has no attribute 'dep_version'):
        HarvestMap's own wrapper dir coincides with one of its members' dir ('HarvestMap' the
        bundle vs. 'HarvestMap' the member nested at HarvestMap/Modules/HarvestMap) -- Folder.dir
        ('HarvestMap') yields both, and find_installed() must skip the bundle (no dep_version of
        its own) rather than crash on it, falling through to the real member."""
        main = make_installed(addon_root / 'HarvestMap' / 'Modules', 'HarvestMap', AddOnVersion='31503')
        other = make_installed(addon_root / 'HarvestMap' / 'Modules', 'HarvestMapAD', AddOnVersion='31503')
        bundle = AddonBundle('HarvestMap', addon_root / 'HarvestMap', [main, other])
        dependent = make_installed(addon_root, 'SomeAddon', DependsOn='HarvestMap>=31503')
        # Bundle inserted before the member it shares a dir with, matching real _scan() order.
        folder._installed = {bundle.folder: bundle, main.folder: main, other.folder: other,
                             dependent.folder: dependent}

        missing = folder.missing_deps(folder.installed)

        assert missing == []

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

    def test_unused_deps_ignores_bundle_members(self, addon_root, folder):
        main = make_installed(addon_root / 'Bundle', 'Main')
        ext = make_installed(addon_root / 'Bundle', 'MainExt', IsLibrary='true')
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [main, ext])
        folder._installed = {bundle.folder: bundle, main.folder: main, ext.folder: ext}
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

    def test_saved_variable_files_finds_existing_declared_file(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', SavedVariables='MyAddonVars')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        saved = addon_root.parent / 'SavedVariables' / 'MyAddon.lua'
        saved.parent.mkdir(parents=True)
        saved.write_text('-- vars --')

        assert folder.saved_variable_files(addon) == [saved]

    def test_saved_variable_files_skips_missing_file(self, addon_root, folder):
        """Declared in the manifest but never actually written (or already deleted)."""
        make_installed(addon_root, 'MyAddon', SavedVariables='MyAddonVars')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        assert folder.saved_variable_files(addon) == []

    def test_saved_variable_files_empty_when_not_declared(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        assert folder.saved_variable_files(addon) == []

    def test_remove_with_remove_vars_deletes_saved_variables(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', SavedVariables='MyAddonVars')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        saved = addon_root.parent / 'SavedVariables' / 'MyAddon.lua'
        saved.parent.mkdir(parents=True)
        saved.write_text('-- vars --')

        folder.remove(addon, remove_vars=True)
        assert not saved.exists()

    def test_remove_without_remove_vars_keeps_saved_variables(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon', SavedVariables='MyAddonVars')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))
        saved = addon_root.parent / 'SavedVariables' / 'MyAddon.lua'
        saved.parent.mkdir(parents=True)
        saved.write_text('-- vars --')

        folder.remove(addon, remove_vars=False)
        assert saved.exists()

    def test_remove_also_deletes_saved_variables_of_unmatched_bundled_child(self, addon_root, folder):
        """The reported gap: a bundled lib not matched online never got its own SavedVariables
        offered/removed, since rmtree()-ing the parent doesn't touch <root>/../SavedVariables/
        and remove() only ever checked the one addon passed to it."""
        make_installed(addon_root, 'Parent')
        make_installed(addon_root / 'Parent', 'LibFoo', SavedVariables='LibFooVars')
        folder.scan()  # no api -- LibFoo stays unmatched, like most bundled deps

        [parent] = list(folder.dir('Parent'))
        [child] = list(folder.dir('LibFoo'))
        assert child.infos is None  # unmatched, the case this is about

        saved_dir = addon_root.parent / 'SavedVariables'
        saved_dir.mkdir(parents=True)
        (saved_dir / 'LibFoo.lua').write_text('-- vars --')

        folder.remove(parent, remove_vars=True)

        assert not (saved_dir / 'LibFoo.lua').exists()
        assert not parent.folder.exists()
        assert list(folder.dir('LibFoo')) == []  # stale entry also cleaned up, not just the file

    def test_remove_bundled_child_saved_variables_respects_remove_vars_false(self, addon_root, folder):
        make_installed(addon_root, 'Parent')
        make_installed(addon_root / 'Parent', 'LibFoo', SavedVariables='LibFooVars')
        folder.scan()
        [parent] = list(folder.dir('Parent'))

        saved_dir = addon_root.parent / 'SavedVariables'
        saved_dir.mkdir(parents=True)
        (saved_dir / 'LibFoo.lua').write_text('-- vars --')

        folder.remove(parent, remove_vars=False)
        assert (saved_dir / 'LibFoo.lua').exists()

    def test_remove_on_a_bundle_deletes_saved_variables_of_every_member(self, addon_root, folder):
        """gru remove on a flat bundle: for the rest, removal is the same as any other addon (one
        rmtree of its own .folder sweeps every member); SavedVariables need explicit handling
        since they live outside AddOns/ entirely, keyed per member -- not once for the bundle
        itself, which has none of its own (see the double-unlink bug this guards against, since
        the main member's dir often coincides with the bundle's own)."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', SavedVariables='BundleVars')
        extra = make_installed(addon_root / 'Bundle', 'BundleExtra', SavedVariables='BundleExtraVars')
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [main, extra])
        folder._installed = {bundle.folder: bundle, main.folder: main, extra.folder: extra}

        saved_dir = addon_root.parent / 'SavedVariables'
        saved_dir.mkdir(parents=True)
        (saved_dir / 'Bundle.lua').write_text('-- main vars --')
        (saved_dir / 'BundleExtra.lua').write_text('-- extra vars --')

        folder.remove(bundle, remove_vars=True)

        assert not (saved_dir / 'Bundle.lua').exists()
        assert not (saved_dir / 'BundleExtra.lua').exists()
        assert not bundle.folder.exists()
        assert list(folder.installed) == []

    def test_remove_bundled_child_saved_variables_policy_asked_per_child(self, addon_root, folder):
        """remove_vars as a callable is asked separately for the parent and each bundled child,
        same as it already is for cascaded dependency removals."""
        make_installed(addon_root, 'Parent', SavedVariables='ParentVars')
        make_installed(addon_root / 'Parent', 'LibFoo', SavedVariables='LibFooVars')
        folder.scan()
        [parent] = list(folder.dir('Parent'))

        saved_dir = addon_root.parent / 'SavedVariables'
        saved_dir.mkdir(parents=True)
        (saved_dir / 'Parent.lua').write_text('-- vars --')
        (saved_dir / 'LibFoo.lua').write_text('-- vars --')

        asked = []

        def policy(candidate):
            asked.append(candidate.dir)
            return candidate.dir == 'LibFoo'  # only agree to remove the child's vars

        folder.remove(parent, remove_vars=policy)

        assert set(asked) == {'Parent', 'LibFoo'}
        assert (saved_dir / 'Parent.lua').exists()
        assert not (saved_dir / 'LibFoo.lua').exists()

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

    def test_remove_vars_callable_policy_is_asked_again_for_cascaded_dependency(self, addon_root, folder):
        """remove_vars can be a per-addon callable, not just a fixed bool -- reused for the
        cascaded dependency removal (deps=True), not just the addon passed in directly."""
        make_installed(addon_root, 'MyAddon', DependsOn='LibFoo>=1', SavedVariables='MyAddonVars')
        make_installed(addon_root, 'LibFoo', IsLibrary='true', SavedVariables='LibFooVars')
        folder.scan()
        [addon] = list(folder.dir('MyAddon'))

        main_saved = addon_root.parent / 'SavedVariables' / 'MyAddon.lua'
        lib_saved = addon_root.parent / 'SavedVariables' / 'LibFoo.lua'
        for saved in (main_saved, lib_saved):
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('-- vars --')

        asked = []

        def policy(candidate):
            asked.append(candidate.dir)
            return candidate.dir == 'LibFoo'  # only agree to remove LibFoo's vars, not MyAddon's

        folder.remove(addon, deps=True, remove_vars=policy)
        assert set(asked) == {'MyAddon', 'LibFoo'}
        assert main_saved.exists()
        assert not lib_saved.exists()

    def test_remove_duplicates_threads_remove_vars_policy(self, addon_root, folder):
        make_installed(addon_root, 'Parent', DependsOn='LibShared>=1')
        make_installed(addon_root / 'Parent', 'LibShared', IsLibrary='true', Version='2.0')
        make_installed(addon_root, 'LibShared', IsLibrary='true', Version='1.0', SavedVariables='LibSharedVars')
        upstream = make_addon_info(id_=1, title='LibShared', directories=['LibShared'])
        api = make_api(addons={1: upstream})
        folder.scan(api)

        saved = addon_root.parent / 'SavedVariables' / 'LibShared.lua'
        saved.parent.mkdir(parents=True)
        saved.write_text('-- vars --')

        pairs = folder.remove_duplicates(remove_vars=True)
        assert len(pairs) == 1
        assert not saved.exists()

    def test_remove_unused_deps_cascades(self, addon_root, folder):
        """LibA depends on LibB; removing LibA as unused should also free LibB."""
        make_installed(addon_root, 'LibA', IsLibrary='true', DependsOn='LibB>=1')
        make_installed(addon_root, 'LibB', IsLibrary='true')
        folder.scan()
        removed = folder.remove_unused_deps()
        assert removed == 2
        assert list(folder.installed) == []


class TestDuplicateStandalones:
    """duplicate_standalones()/remove_duplicates(): a standalone (top-level) library install
    made redundant by an equal-or-newer bundled copy of the same online addon -- ESO always
    loads the highest version it finds, so these top-level copies serve no purpose. Bundled
    copies themselves must never be flagged as the redundant side."""

    def _bundled_and_standalone(self, addon_root, folder, bundled_version, standalone_version, is_lib='true'):
        make_installed(addon_root, 'Parent', Title='Parent')
        make_installed(addon_root / 'Parent', 'LibShared', Title='LibShared',
                       IsLibrary=is_lib, Version=bundled_version)
        make_installed(addon_root, 'LibShared', Title='LibShared', IsLibrary=is_lib, Version=standalone_version)
        folder.scan()

        info = make_addon_info(id_=1, title='LibShared')
        for addon in folder.installed:
            if addon.dir == 'LibShared':
                addon.link(info)

    def test_finds_standalone_superseded_by_newer_bundled_copy(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='2.0', standalone_version='1.0')
        [standalone] = [a for a in folder.installed if a.dir == 'LibShared' and a.parent is None]
        [bundled] = [a for a in folder.installed if a.dir == 'LibShared' and a.parent is not None]

        assert folder.duplicate_standalones(folder.installed) == [(standalone, bundled)]

    def test_tied_versions_also_count_as_superseded(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='1.0', standalone_version='1.0')
        pairs = folder.duplicate_standalones(folder.installed)
        assert len(pairs) == 1
        addon, bundled_in = pairs[0]
        assert addon.parent is None and bundled_in.parent is not None

    def test_standalone_newer_than_bundled_is_not_flagged(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='1.0', standalone_version='2.0')
        assert folder.duplicate_standalones(folder.installed) == []

    def test_bundled_copy_itself_never_flagged_as_redundant(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='1.0', standalone_version='2.0')
        pairs = folder.duplicate_standalones(folder.installed)
        assert all(addon.parent is None for addon, _ in pairs)

    def test_non_library_duplicates_are_ignored(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='2.0',
                                     standalone_version='1.0', is_lib='false')
        assert folder.duplicate_standalones(folder.installed) == []

    def test_unparseable_version_is_not_flagged(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='2.0', standalone_version='unknown')
        assert folder.duplicate_standalones(folder.installed) == []

    def test_remove_duplicates_removes_standalone_and_returns_pairs(self, addon_root, folder):
        self._bundled_and_standalone(addon_root, folder, bundled_version='2.0', standalone_version='1.0')
        removed = folder.remove_duplicates()
        assert len(removed) == 1
        assert list(folder.dir('LibShared')) == [removed[0][1]]  # only the bundled copy remains


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
            def dir(self, name, link=None):
                return info

        folder.scan(LinkableApi())
        [addon] = list(folder.installed)
        assert addon.id == 7

    def test_scan_disambiguates_tied_dir_via_addons_csv_link(self, addon_root, folder, tmp_path):
        """Real esoui.com scenario (BRHelper): several unrelated addons declare the same dir.
        A previously recorded link in addons.csv breaks the tie by the id in its URL, exercising
        the real API.dir() (not a stub reimplementing its logic)."""
        make_installed(addon_root, 'BRHelper')
        base = make_addon_info(id_=2181, title='Blackrose Prison Helper', directories=['BRHelper'])
        jp_version = make_addon_info(id_=2996, title='Blackrose Prison Helper JP', directories=['BRHelper'])
        api = make_api(addons={2181: base, 2996: jp_version})

        csv_path = tmp_path / 'addons.csv'
        csv_path.write_text('dir,version,link,locked\n'
                            'BRHelper,1.0,https://www.esoui.com/downloads/info2996-BRHelperJP.html,\n')
        install_mod.user_config = lambda *parts: csv_path if parts[-1] == 'addons.csv' else tmp_path.joinpath(*parts)

        folder.scan(api)
        [addon] = list(folder.installed)
        assert addon.id == 2996

    def test_scan_without_addons_csv_leaves_unresolved_tie_unmatched(self, addon_root, folder, tmp_path):
        """No prior addons.csv (e.g. first-ever scan) -- read_csv_hints() finds nothing, so
        API.dir() has no link to resolve the tie with. It must not guess: the addon stays
        unmatched (not silently linked to whichever candidate happens to come first), and no
        warning fires -- it *was* found, just ambiguously, unlike a genuine no-match."""
        make_installed(addon_root, 'BRHelper')
        base = make_addon_info(id_=2181, title='Blackrose Prison Helper', directories=['BRHelper'])
        jp_version = make_addon_info(id_=2996, title='Blackrose Prison Helper JP', directories=['BRHelper'])
        api = make_api(addons={2181: base, 2996: jp_version})
        install_mod.user_config = lambda *parts: tmp_path.joinpath(*parts)  # no addons.csv written

        with warnings.catch_warnings():
            warnings.simplefilter('error')  # any warning here would be a regression
            folder.scan(api)
        [addon] = list(folder.installed)
        assert addon.id is None

    def test_scan_leaves_toplevel_addon_missing_from_api_unmatched_without_warning(self, addon_root, folder):
        make_installed(addon_root, 'MyAddon')
        api = StubAPI()  # empty -- MyAddon not found
        with warnings.catch_warnings():
            warnings.simplefilter('error')  # TermDisplay flags it instead, no scan-time warning
            folder.scan(api)
        [addon] = list(folder.installed)
        assert addon.id is None

    def test_scan_finds_nested_addon_with_parent_link(self, addon_root, folder):
        make_installed(addon_root, 'Parent')
        make_installed(addon_root / 'Parent', 'Child')
        folder.scan()
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['Child'].parent is by_dir['Parent']

    def test_scan_ancestor_parenting_unaffected_by_bundle_lookup(self, addon_root, folder):
        make_installed(addon_root, 'Parent')
        make_installed(addon_root / 'Parent', 'Child')
        folder.scan()
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['Child'].parent is by_dir['Parent']

    def test_scan_links_bundle_members_by_walking_up_past_a_pass_through_dir(self, addon_root, folder):
        """HarvestMapData-shaped bundle: submodules sit under an extra Modules/ dir that isn't
        itself an addon, so the direct containing-dir lookup ('Modules') must fail and retry one
        level up ('HarvestMapData'), which is the bundle's real online listing."""
        make_installed(addon_root / 'HarvestMapData' / 'Modules', 'RegionAD')
        make_installed(addon_root / 'HarvestMapData' / 'Modules', 'RegionDC')
        info = make_addon_info(id_=3034, title='HarvestMap-Data', directories=['HarvestMapData'])
        api = make_api(addons={3034: info})

        folder.scan(api)
        by_dir = {a.dir: a for a in folder.installed}
        # Members no longer inherit the bundle's .infos -- only the bundle itself is linked;
        # each member would only get its own .infos from resolving independently online.
        assert by_dir['RegionAD'].infos is None
        assert by_dir['RegionDC'].infos is None
        assert by_dir['RegionAD'].parent is by_dir['RegionDC'].parent
        assert by_dir['RegionAD'].parent.dir == 'HarvestMapData'
        assert by_dir['RegionAD'].parent.title == 'HarvestMapData'
        assert by_dir['RegionAD'].parent.id == 3034

    def test_scan_sets_parent_on_members_even_when_bundle_stays_unmatched(self, addon_root, folder):
        """The structural fact (these are bundled together) is independent of whether an online
        listing was ever found -- .parent must still be set so TermDisplay can show 'bundled
        inside <dir>' rather than leaving the group looking like unrelated top-level addons."""
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        api = StubAPI()  # nothing registered -- lookup fails all the way to the root

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            folder.scan(api)
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['BundleExtra1'].parent is by_dir['BundleExtra2'].parent
        assert by_dir['BundleExtra1'].parent.dir == 'Bundle'

    def test_scan_links_bundle_members_via_immediate_wrapper(self, addon_root, folder):
        """No pass-through dir needed: the immediate containing dir's own name already matches
        an online listing, so members link on the first try."""
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        info = make_addon_info(id_=1, title='Bundle', directories=['Bundle'])
        api = make_api(addons={1: info})

        folder.scan(api)
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['BundleExtra1'].infos is None
        assert by_dir['BundleExtra2'].infos is None
        assert by_dir['BundleExtra1'].parent.id == 1

    def test_scan_leaves_bundle_unmatched_when_nothing_resolves_up_to_root(self, addon_root, folder):
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        api = StubAPI()  # nothing registered at all

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            folder.scan(api)
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['BundleExtra1'].infos is None
        assert by_dir['BundleExtra2'].infos is None

    def test_scan_leaves_ambiguous_bundle_dir_unmatched(self, addon_root, folder):
        """Two different online addons declare the same directory as their own -- same tie
        rule as an individual addon (see test_scan_without_addons_csv_leaves_unresolved_tie_
        unmatched): stays unmatched rather than guessing, no scan-time warning either."""
        make_installed(addon_root / 'Bundle', 'BundleExtra1')
        make_installed(addon_root / 'Bundle', 'BundleExtra2')
        one = make_addon_info(id_=1, title='Bundle One', directories=['Bundle'])
        two = make_addon_info(id_=2, title='Bundle Two', directories=['Bundle'])
        api = make_api(addons={1: one, 2: two})

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            folder.scan(api)
        by_dir = {a.dir: a for a in folder.installed}
        assert by_dir['BundleExtra1'].infos is None
        assert by_dir['BundleExtra2'].infos is None

    def test_scan_never_looks_up_addons_root_itself_as_a_bundle_name(self, addon_root, folder):
        """Two unrelated addons installed directly under the addons root share self.root as
        their immediate parent -- that must never be tried as a candidate bundle name."""
        make_installed(addon_root, 'AddonA')
        make_installed(addon_root, 'AddonB')
        looked_up = []

        class RecordingApi(StubAPI):
            def dir(self, name, link=None):
                looked_up.append(name)
                raise FileNotFoundError(name)

        folder.scan(RecordingApi())
        assert addon_root.name not in looked_up

    def test_scan_ignores_directory_without_manifest(self, addon_root, folder):
        (addon_root / 'NotAnAddon').mkdir()
        folder.scan()
        assert list(folder.installed) == []

    def test_scan_skips_addon_with_malformed_manifest_and_warns(self, addon_root, folder):
        """A manifest that fails an assert in _parse_manifest (not FileNotFoundError) must not abort the scan."""
        write_manifest(addon_root, 'BadAddon', APIVersion='not-a-number')
        make_installed(addon_root, 'GoodAddon')
        with pytest.warns(UserWarning, match='Skipping addon.*BadAddon.*AssertionError'):
            folder.scan()
        assert {a.dir for a in folder.installed} == {'GoodAddon'}

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


# ---------------------------------------------------------------------------
# update() / install_deps() -- network faked via requests.head/get + user_cache
# ---------------------------------------------------------------------------

class TestFolderUpdate:
    def test_update_downloads_new_version_and_reapplies_saved_patch(self, addon_root, folder, monkeypatch, tmp_path):
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        (installed.folder / 'Data.lua').write_text('old = 1\n')
        upstream = make_addon_info(id_=1, title='MyAddon', version='2.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}

        # The re-applied patch turns the freshly-downloaded 'old = 2' into 'patched = 3'.
        patch_dir = tmp_path / 'config' / folder.game
        patch_dir.mkdir(parents=True)
        patched = make_installed(tmp_path / 'patched_src', 'MyAddon', Version='2.0')
        (patched.folder / 'Data.lua').write_text('patched = 3\n')
        unpatched = make_installed(tmp_path / 'unpatched_src', 'MyAddon', Version='2.0')
        (unpatched.folder / 'Data.lua').write_text('old = 2\n')
        with (patch_dir / 'MyAddon.patch').open('w') as f:
            addon_diff(patched, unpatched, out=f)

        zip_bytes = _zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'old = 2\n',
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        updates, added = folder.update(as_api(StubAPI()), patch=True)

        assert (updates, added) == (1, 0)
        assert (installed.folder / 'Data.lua').read_text() == 'patched = 3\n'

    def test_update_skips_addons_that_cannot_update(self, addon_root, folder, monkeypatch, tmp_path):
        installed = make_installed(addon_root, 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0')  # same version -> can_update is False
        installed.link(upstream)
        folder._installed = {installed.folder: installed}

        def boom(*a, **kw):
            raise AssertionError('should not be called: no addon can update')
        monkeypatch.setattr(install_mod.requests, 'head', boom)

        updates, added = folder.update(as_api(StubAPI()))
        assert (updates, added) == (0, 0)

    def test_update_standalone_addon_in_subdir_stays_in_place(self, addon_root, folder, monkeypatch, tmp_path):
        """parent is None -> path = addon.folder: a standalone addon installed in a non-default
        subdirectory (not just directly under AddOns/) must be updated in place there, not moved."""
        installed = make_installed(addon_root / 'subdir', 'MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='2.0', directories=['MyAddon'])
        installed.link(upstream)
        folder._installed = {installed.folder: installed}

        zip_bytes = _zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'new = 2\n',
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        updates, added = folder.update(as_api(StubAPI()))

        assert (updates, added) == (1, 0)
        assert (addon_root / 'subdir' / 'MyAddon' / 'Data.lua').read_text() == 'new = 2\n'
        assert not (addon_root / 'MyAddon').exists()  # must not end up freshly installed at top level

    def test_update_bundled_addon_installs_at_top_level_not_in_place(self, addon_root, folder, monkeypatch, tmp_path):
        make_installed(addon_root, 'Parent', Title='Parent')
        make_installed(addon_root / 'Parent', 'LibFoo', Title='LibFoo', IsLibrary='true', Version='1.0')
        (addon_root / 'Parent' / 'LibFoo' / 'Data.lua').write_text('old = 1\n')
        folder.scan()
        [bundled] = list(folder.dir('LibFoo'))
        upstream = make_addon_info(id_=1, title='LibFoo', version='2.0', directories=['LibFoo'])
        bundled.link(upstream)

        zip_bytes = _zip_bytes({
            'LibFoo/LibFoo.txt': '## Title: LibFoo\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'LibFoo/Data.lua': 'new = 2\n',
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        updates, added = folder.update(as_api(StubAPI()))

        assert (updates, added) == (1, 0)
        assert (addon_root / 'Parent' / 'LibFoo' / 'Data.lua').read_text() == 'old = 1\n'  # untouched
        assert (addon_root / 'LibFoo' / 'Data.lua').read_text() == 'new = 2\n'  # fresh top-level copy

    def test_update_skips_bundled_addon_already_superseded(self, addon_root, folder, monkeypatch, tmp_path):
        make_installed(addon_root, 'Parent', Title='Parent')
        make_installed(addon_root / 'Parent', 'LibFoo', Title='LibFoo', IsLibrary='true', Version='1.0')
        make_installed(addon_root, 'LibFoo', Title='LibFoo', IsLibrary='true', Version='2.0')
        folder.scan()
        upstream = make_addon_info(id_=1, title='LibFoo', version='2.0', directories=['LibFoo'])
        for addon in folder.installed:
            if addon.dir == 'LibFoo':
                addon.link(upstream)

        def boom(*a, **kw):
            raise AssertionError('should not be called: bundled copy is superseded')
        monkeypatch.setattr(install_mod.requests, 'head', boom)

        updates, added = folder.update(as_api(StubAPI()))
        assert (updates, added) == (0, 0)

    def test_update_sibling_of_flat_bundle_installs_like_standalone(self, addon_root, folder, monkeypatch, tmp_path):
        """A flat-bundle sibling (.parent = the group's main entry) is treated exactly like an
        ancestor-nested lib: if it can update, it's valid for it to trigger its own reinstall.
        _inspect_bundle anchors extraction on the zip's own top-level name, not on whatever bare
        dir name was asked for, so the fresh copy always lands back under the wrapper. Manually
        sets .parent since _scan() doesn't populate it for siblings yet; main is deliberately
        never added to folder._installed/linked, to isolate this to the sibling's own decision."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        sibling.parent = main
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0', directories=['Bundle'])
        sibling.link(upstream)
        folder._installed = {sibling.folder: sibling}

        zip_bytes = _zip_bytes({
            'Bundle/Bundle/Bundle.txt': '## Title: Bundle\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'Bundle/BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                   '## Version: 2.0\n## Author: Test\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        updates, added = folder.update(as_api(StubAPI()))

        assert updates >= 1  # _scan()'s fallback re-discovers the whole group from the one shared zip
        assert (addon_root / 'Bundle' / 'BundleExtra' / 'BundleExtra.txt').exists()
        assert not (addon_root / 'BundleExtra').exists()  # never lands directly at the addons root

    def test_update_skips_superseded_sibling_of_flat_bundle(self, addon_root, folder, monkeypatch, tmp_path):
        """A flat-bundle sibling already superseded (main is at a newer version) must not cause
        any further update -- identical to the existing ancestor-nested 'superseded' case."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='2.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        sibling.parent = main
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0', directories=['Bundle'])
        main.link(upstream)
        sibling.link(upstream)
        folder._installed = {main.folder: main, sibling.folder: sibling}

        def boom(*a, **kw):
            raise AssertionError('should not be called: sibling is superseded by main')
        monkeypatch.setattr(install_mod.requests, 'head', boom)

        updates, added = folder.update(as_api(StubAPI()))
        assert updates == 0

    def test_update_skips_sibling_not_matched_online(self, addon_root, folder, monkeypatch):
        """A flat-bundle sibling whose name isn't independently listed never gets .infos set (a
        regular re-scan can't resolve it via the API on its own) -- must not update at all."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        sibling.parent = main
        folder._installed = {sibling.folder: sibling}  # sibling.infos stays None: never linked

        def boom(*a, **kw):
            raise AssertionError('should not be called: sibling has no infos to update from')
        monkeypatch.setattr(install_mod.requests, 'head', boom)

        updates, added = folder.update(as_api(StubAPI()))
        assert updates == 0

    def test_update_sibling_never_clashes_with_unrelated_standalone(self, addon_root, folder, monkeypatch, tmp_path):
        """An unrelated, genuinely standalone addon that happens to share a sibling's bare dir
        name must survive the sibling's update untouched -- _inspect_bundle always anchors on
        the zip's own top-level name, so the sibling's reinstall can never erase or overwrite it."""
        unrelated = make_installed(addon_root, 'BundleExtra', Title='Unrelated standalone addon')
        (unrelated.folder / 'Data.lua').write_text('unrelated = 1\n')

        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        sibling.parent = main
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0', directories=['Bundle'])
        sibling.link(upstream)
        folder._installed = {unrelated.folder: unrelated, sibling.folder: sibling}

        zip_bytes = _zip_bytes({
            'Bundle/Bundle/Bundle.txt': '## Title: Bundle\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'Bundle/BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                   '## Version: 2.0\n## Author: Test\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        folder.update(as_api(StubAPI()))

        assert (unrelated.folder / 'Data.lua').read_text() == 'unrelated = 1\n'

    def test_update_bundle_targets_wrapper_dir_regardless_of_member_nesting_depth(
            self, addon_root, folder, monkeypatch, tmp_path):
        """AddonBundle.folder is the wrapper itself (set explicitly by find_bundle_matches, not
        derived from a member's own .folder.parent) -- update() must reinstall via that path
        even when a member's own manifest sits 2 levels below the wrapper (a pass-through 'src'
        dir), where 'go up one level from some member' would land on 'src' instead."""
        main = make_installed(addon_root / 'Bundle' / 'src', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0', directories=['Bundle'])
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [main, sibling])
        bundle.link(upstream)
        folder._installed = {bundle.folder: bundle, main.folder: main, sibling.folder: sibling}

        zip_bytes = _zip_bytes({
            'Bundle/src/Bundle/Bundle.txt': ('## Title: Bundle\n## APIVersion: 100035\n## Version: 2.0\n'
                                             '## Author: Test\n'),
            'Bundle/BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                   '## Version: 2.0\n## Author: Test\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        folder.update(as_api(StubAPI()))

        assert (addon_root / 'Bundle' / 'src' / 'Bundle' / 'Bundle.txt').exists()
        assert (addon_root / 'Bundle' / 'BundleExtra' / 'BundleExtra.txt').exists()

    def test_update_skips_bundle_members_the_bundle_itself_covers_them(
            self, addon_root, folder, monkeypatch, tmp_path):
        """Members with no .infos of their own (the common case: private/library-only names)
        simply can't update -- only the bundle (also in self.installed, self-linked) does,
        in one shared call. See test_update_member_own_standalone_status_updates_only_itself_
        not_the_bundle for a member that *does* have its own .infos."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        upstream = make_addon_info(id_=1, title='Bundle', version='2.0', directories=['Bundle'])
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [main, sibling])
        bundle.link(upstream)
        folder._installed = {bundle.folder: bundle, main.folder: main, sibling.folder: sibling}

        zip_bytes = _zip_bytes({
            'Bundle/Bundle/Bundle.txt': '## Title: Bundle\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'Bundle/BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                   '## Version: 2.0\n## Author: Test\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)
        orig_head = install_mod.requests.head
        calls = []

        def counting_head(url, **kw):
            calls.append(url)
            return orig_head(url, **kw)
        monkeypatch.setattr(install_mod.requests, 'head', counting_head)

        folder.update(as_api(StubAPI()))

        assert len(calls) == 1

    def test_update_member_own_standalone_status_updates_only_itself_not_the_bundle(
            self, addon_root, folder, monkeypatch, tmp_path):
        """A member that's independently listed online (its own AddonInfo, distinct from the
        bundle's) and has its own update available must trigger only its own standalone
        reinstall -- never the bundle's, which is unlinked here and must stay untouched."""
        main = make_installed(addon_root / 'Bundle', 'Bundle', Title='Bundle', Version='1.0')
        sibling = make_installed(addon_root / 'Bundle', 'BundleExtra', Title='BundleExtra', Version='1.0')
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [main, sibling])
        # Bundle itself is never linked -- its can_update must stay False regardless of members.
        sibling_upstream = make_addon_info(id_=2, title='BundleExtra', version='2.0', directories=['BundleExtra'])
        sibling.link(sibling_upstream)
        folder._installed = {bundle.folder: bundle, main.folder: main, sibling.folder: sibling}

        zip_bytes = _zip_bytes({'BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                                '## Version: 2.0\n## Author: Test\n')})
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        folder.update(as_api(StubAPI()))

        assert bundle.infos is None  # untouched -- its own can_update was never True
        assert (addon_root / 'BundleExtra' / 'BundleExtra.txt').exists()  # standalone, own dir
        assert '## Version: 1.0' in (addon_root / 'Bundle' / 'BundleExtra' / 'BundleExtra.txt').read_text()


class TestUnmodifiedAddon:
    def test_picks_the_member_matching_dir_not_just_the_first(self, folder, monkeypatch, tmp_path):
        """diffing one member of a bundle must yield THAT member, not whichever one happens to
        be first in unpack()'s result -- the bug found while diffing was left aside earlier."""
        upstream = make_addon_info(id_=1, title='Bundle', version='1.0', directories=['Bundle'])
        zip_bytes = _zip_bytes({
            'Bundle/Bundle/Bundle.txt': '## Title: Bundle\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n',
            'Bundle/BundleExtra/BundleExtra.txt': ('## Title: BundleExtra\n## APIVersion: 100035\n'
                                                   '## Version: 1.0\n## Author: Test\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        with folder.unmodified_addon(upstream, 'BundleExtra', StubAPI()) as ref_addon:
            assert ref_addon.dir == 'BundleExtra'

    def test_falls_back_to_first_member_when_dir_not_found(self, folder, monkeypatch, tmp_path):
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        zip_bytes = _zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n',
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        with folder.unmodified_addon(upstream, 'NoSuchMember', StubAPI()) as ref_addon:
            assert ref_addon.dir == 'MyAddon'


def _version_changes(before, after) -> set[tuple[str, str, str]]:
    """(dir, old version, new version) per folder that changed between two Folder.snapshot()s, as log_changes()
    would report them -- '' standing for not installed."""
    return {((after.get(k) or before[k])[0], before.get(k, ('', ''))[1], after.get(k, ('', ''))[1])
            for k in before.keys() | after.keys() if before.get(k, ('', ''))[1] != after.get(k, ('', ''))[1]}


class TestUnpackTracksAllInstalled:
    """unpack() must leave Folder.installed matching what is on disk after extraction -- every addon it wrote,
    not only the one at install_folder -- so snapshot()-based change logs don't report reinstalled addons as
    removed or moved."""

    def _manifest(self, title, version):
        return f'## Title: {title}\n## APIVersion: 100035\n## Version: {version}\n## Author: Test\n'

    def test_nested_library_is_updated_not_removed(self, addon_root, folder, monkeypatch, tmp_path):
        write_manifest(addon_root, 'Foo', Version='1.0')
        write_manifest(addon_root / 'Foo', 'LibX', Version='1.0', IsLibrary='true')
        upstream = make_addon_info(id_=1, title='Foo', version='2.0', directories=['Foo'])
        api = as_api(StubAPI({'Foo': upstream}))
        folder.scan(api)
        before = folder.snapshot()

        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': self._manifest('Foo', '2.0'),
            'Foo/LibX/LibX.txt': self._manifest('LibX', '2.0') + '## IsLibrary: true\n',
        }))
        installed = folder.unpack(upstream, api, path=addon_root / 'Foo')

        assert {a.folder for a in installed} == {addon_root / 'Foo', addon_root / 'Foo' / 'LibX'}
        assert _version_changes(before, folder.snapshot()) == {('Foo', '1.0', '2.0'), ('LibX', '1.0', '2.0')}
        foo = next(folder.dir('Foo'))
        lib = next(folder.dir('LibX'))
        assert foo.infos is upstream
        assert lib.infos is None  # not listed online: must not inherit Foo's listing
        assert lib.parent is foo

    def test_every_top_level_dir_of_the_bundle_is_tracked(self, addon_root, folder, monkeypatch, tmp_path):
        write_manifest(addon_root, 'Bar', Version='1.0')
        write_manifest(addon_root, 'BarLib', Version='1.0')
        upstream = make_addon_info(id_=1, title='Bar', version='2.0', directories=['Bar', 'BarLib'])
        api = as_api(StubAPI({'Bar': upstream}))
        folder.scan(api)
        before = folder.snapshot()

        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Bar/Bar.txt': self._manifest('Bar', '2.0'),
            'BarLib/BarLib.txt': self._manifest('BarLib', '2.0'),
        }))
        with pytest.warns(UserWarning, match='Installing 2 addons as part of Bar'):
            folder.unpack(upstream, api, path=addon_root / 'Bar')

        assert _version_changes(before, folder.snapshot()) == {('Bar', '1.0', '2.0'), ('BarLib', '1.0', '2.0')}

    def test_nested_addon_keeps_its_lock(self, addon_root, folder, monkeypatch, tmp_path):
        write_manifest(addon_root, 'Foo', Version='1.0')
        write_manifest(addon_root / 'Foo', 'LibX', Version='1.0')
        upstream = make_addon_info(id_=1, title='Foo', version='2.0', directories=['Foo'])
        api = as_api(StubAPI({'Foo': upstream}))
        folder.scan(api)
        next(folder.dir('LibX')).locked = True

        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': self._manifest('Foo', '2.0'),
            'Foo/LibX/LibX.txt': self._manifest('LibX', '2.0'),
        }))
        folder.unpack(upstream, api, path=addon_root / 'Foo')

        assert next(folder.dir('LibX')).locked

    def test_untouched_addons_are_kept(self, addon_root, folder, monkeypatch, tmp_path):
        write_manifest(addon_root, 'Foo', Version='1.0')
        write_manifest(addon_root, 'Other', Version='1.0')
        upstream = make_addon_info(id_=1, title='Foo', version='2.0', directories=['Foo'])
        api = as_api(StubAPI({'Foo': upstream}))
        folder.scan(api)
        before = folder.snapshot()

        _mock_download(monkeypatch, tmp_path, _zip_bytes({'Foo/Foo.txt': self._manifest('Foo', '2.0')}))
        installed = folder.unpack(upstream, api, path=addon_root / 'Foo')

        assert [a.folder for a in installed] == [addon_root / 'Foo']
        assert _version_changes(before, folder.snapshot()) == {('Foo', '1.0', '2.0')}
        assert {a.dir for a in folder.installed} == {'Foo', 'Other'}


class TestFolderInstallDeps:
    def test_install_deps_downloads_missing_dependency(self, addon_root, folder, monkeypatch, tmp_path):
        installed = make_installed(addon_root, 'MyAddon', DependsOn='LibFoo>=1')
        folder._installed = {installed.folder: installed}

        lib_info = make_addon_info(id_=2, title='LibFoo', directories=['LibFoo'])

        class DepApi(StubAPI):
            def dir(self, name, link=None):
                if name == 'LibFoo':
                    return lib_info
                raise FileNotFoundError(name)

        zip_bytes = _zip_bytes({
            'LibFoo/LibFoo.txt': ('## Title: LibFoo\n## APIVersion: 100035\n## Version: 1.0\n'
                                  '## Author: Test\n## IsLibrary: true\n'),
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        added = folder.install_deps([installed], as_api(DepApi()))

        assert added == 1
        assert (addon_root / 'LibFoo' / 'LibFoo.txt').exists()
        assert {a.dir for a in folder.installed} == {'MyAddon', 'LibFoo'}

    def test_install_deps_warns_and_continues_when_dependency_lookup_fails(self, addon_root, folder, monkeypatch):
        installed = make_installed(addon_root, 'MyAddon', DependsOn='LibGone>=1')
        folder._installed = {installed.folder: installed}

        with pytest.warns(UserWarning, match='Failed to look up'):
            added = folder.install_deps([installed], as_api(StubAPI()))

        assert added == 0

    def test_unpack_malformed_manifest_warns_instead_of_crashing(self, addon_root, folder, monkeypatch, tmp_path):
        """A manifest that fails an assert in _parse_manifest must not crash a fresh install/update."""
        upstream = make_addon_info(id_=1, title='BadAddon', version='1.0', directories=['BadAddon'])
        zip_bytes = _zip_bytes({
            'BadAddon/BadAddon.txt': ('## Title: BadAddon\n## APIVersion: not-a-number\n'
                                      '## Version: 1.0\n## Author: Test\n'),
            'BadAddon/Data.lua': 'x = 1\n',
        })
        _mock_download(monkeypatch, tmp_path, zip_bytes)

        with pytest.warns(UserWarning, match='Skipping addon.*AssertionError'):
            installed_addons = folder.unpack(upstream, as_api(StubAPI()))

        assert list(installed_addons) == []
        assert (addon_root / 'BadAddon' / 'Data.lua').exists()

    def test_unzip_normalizes_windows_style_path_separators(self, folder, tmp_path):
        """zipfile entries are always '/'-separated; looking one up by a native (WindowsPath)
        str() would send zf.open() a '\\'-joined name and raise KeyError on Windows."""
        zip_bytes = _zip_bytes({'sub/file.txt': 'hello\n'})
        dest = tmp_path / 'dest'
        files = [(pathlib.PureWindowsPath('sub/file.txt'), False, 6)]
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            folder._unzip(zf, files, dest, install_mod.SilentProgress(6, 'test'))
        assert (dest / 'sub' / 'file.txt').read_text() == 'hello\n'

    def test_unzip_missing_entry_warns_and_continues(self, folder, tmp_path):
        """An entry that isn't actually in the archive must not crash & abort the whole extraction."""
        zip_bytes = _zip_bytes({'present.txt': 'hello\n'})
        dest = tmp_path / 'dest'
        files = [(pathlib.Path('missing.txt'), False, 0), (pathlib.Path('present.txt'), False, 6)]
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            with pytest.warns(UserWarning, match='Skipping missing.txt.*not found in archive'):
                folder._unzip(zf, files, dest, install_mod.SilentProgress(6, 'test'))
        assert not (dest / 'missing.txt').exists()
        assert (dest / 'present.txt').read_text() == 'hello\n'


def test_pts_folder_uses_own_root_and_metadata(tmp_path):
    config = configparser.ConfigParser()
    config.add_section('ESO.addons')
    config.set('ESO.addons', 'root', str(tmp_path / 'live'))
    config.set('ESO.addons', 'pts_root', str(tmp_path / 'pts'))
    config.add_section('ESO.links')
    config.set('ESO.links', 'download', 'https://example.com/dl?id={id}/')
    live, pts = Folder('ESO', config), Folder('ESO', config, 'pts')
    assert live.root == tmp_path / 'live' and live.meta == ('ESO',)
    assert pts.root == tmp_path / 'pts' and pts.meta == ('ESO', 'pts')


class TestVersionOverride:
    """A listing version whose zip ships a different manifest version is remembered in versions.csv and shown
    in place of the manifest's -- only while id, manifest version and file contents all still match."""

    def _setup(self, addon_root, folder, monkeypatch, tmp_path, listing='1.0.8', manifest='1.0'):
        write_manifest(addon_root, 'Foo', Version='0.9')
        upstream = make_addon_info(id_=1, title='Foo', version=listing, directories=['Foo'])
        api = as_api(StubAPI({'Foo': upstream}))
        folder.scan(api)
        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': f'## Title: Foo\n## APIVersion: 100035\n## Version: {manifest}\n## Author: Test\n',
        }))
        folder.unpack(upstream, api, path=addon_root / 'Foo')
        return upstream, api

    def _rescan(self, folder, api):
        folder.scan(api)
        return next(folder.dir('Foo'))

    def test_install_shows_listing_version_and_is_not_updatable(self, addon_root, folder, monkeypatch, tmp_path):
        self._setup(addon_root, folder, monkeypatch, tmp_path)
        foo = next(folder.dir('Foo'))
        assert (foo.version, foo.manifest_version) == ('1.0.8', '1.0')
        assert not foo.can_update

    def test_rescan_restores_override_from_file(self, addon_root, folder, monkeypatch, tmp_path):
        _, api = self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        assert list(tmp_path.rglob('versions.csv'))
        assert self._rescan(folder, api).version == '1.0.8'

    def test_no_row_when_versions_agree(self, addon_root, folder, monkeypatch, tmp_path):
        self._setup(addon_root, folder, monkeypatch, tmp_path, listing='1.0', manifest='1.0')
        folder.export_state()
        assert not list(tmp_path.rglob('versions.csv'))

    def test_modified_files_drop_override(self, addon_root, folder, monkeypatch, tmp_path):
        _, api = self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        (addon_root / 'Foo' / 'extra.lua').write_text('x')
        assert self._rescan(folder, api).version == '1.0'
        folder.export_state()
        assert not list(tmp_path.rglob('versions.csv'))

    def test_manifest_version_change_drops_override(self, addon_root, folder, monkeypatch, tmp_path):
        _, api = self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        write_manifest(addon_root, 'Foo', Version='1.1')
        assert self._rescan(folder, api).version == '1.1'

    def test_other_listing_id_does_not_inherit(self, addon_root, folder, monkeypatch, tmp_path):
        self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        other = as_api(StubAPI({'Foo': make_addon_info(id_=2, title='Foo', version='1.0.8', directories=['Foo'])}))
        assert self._rescan(folder, other).version == '1.0'

    def test_newer_listing_is_an_update(self, addon_root, folder, monkeypatch, tmp_path):
        self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        newer = as_api(StubAPI({'Foo': make_addon_info(id_=1, title='Foo', version='1.0.9', directories=['Foo'])}))
        foo = self._rescan(folder, newer)
        assert foo.version == '1.0.8' and foo.can_update

    def test_url_override_install_records_nothing(self, addon_root, folder, monkeypatch, tmp_path):
        write_manifest(addon_root, 'Foo', Version='0.9')
        upstream = make_addon_info(id_=1, title='Foo', version='1.0.8', directories=['Foo'])
        api = as_api(StubAPI({'Foo': upstream}))
        folder.scan(api)
        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': '## Title: Foo\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n'}))
        folder.unpack(upstream, api, path=addon_root / 'Foo', url_override='http://x/old.zip')
        assert next(folder.dir('Foo')).override is None

    def test_malformed_rows_are_skipped(self, addon_root, folder, tmp_path):
        path = _touch_config_path(tmp_path, 'ESO', 'versions.csv')
        install_mod.user_config = lambda *parts: path if parts[-1] == 'versions.csv' else tmp_path.joinpath(*parts)
        path.write_text('id,listing_version,local_version,fingerprint\nnotint,1,2,3\nshort\n')
        assert folder.read_version_overrides() == {}

    def test_bundle_override_applies_to_bundle_not_members(self, addon_root, folder):
        from gru.addon import AddonBundle
        m1 = make_installed(addon_root / 'Bundle', 'A', Title='A', Version='1.0')
        m2 = make_installed(addon_root / 'Bundle', 'B', Title='B', Version='1.0')
        bundle = AddonBundle('Bundle', addon_root / 'Bundle', [m1, m2])
        folder._installed = {bundle.folder: bundle}
        bundle.link(make_addon_info(id_=7, title='Bundle', version='1.0.5', directories=['Bundle']))
        bundle.record_override('1.0.5')
        assert (bundle.version, bundle.manifest_version, m1.version, m2.version) == ('1.0.5', '1.0', '1.0', '1.0')
        assert not bundle.can_update

        folder.export_state()
        folder._override_rows = folder.read_version_overrides()
        fresh = AddonBundle('Bundle', addon_root / 'Bundle', [m1, m2])
        fresh.link(bundle.infos)
        folder._apply_override(fresh)
        assert fresh.version == '1.0.5'
        (addon_root / 'Bundle' / 'A' / 'x.lua').write_text('x')
        stale = AddonBundle('Bundle', addon_root / 'Bundle', [m1, m2])
        stale.link(bundle.infos)
        folder._apply_override(stale)
        assert stale.version == '1.0'

    def test_fixed_release_prunes_row(self, addon_root, folder, monkeypatch, tmp_path):
        upstream, api = self._setup(addon_root, folder, monkeypatch, tmp_path)
        folder.export_state()
        assert list(tmp_path.rglob('versions.csv'))
        fixed = make_addon_info(id_=1, title='Foo', version='1.0.9', directories=['Foo'])
        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': '## Title: Foo\n## APIVersion: 100035\n## Version: 1.0.9\n## Author: Test\n'}))
        folder.unpack(fixed, as_api(StubAPI({'Foo': fixed})), path=addon_root / 'Foo')
        folder.export_state()
        assert not list(tmp_path.rglob('versions.csv'))
        assert next(folder.dir('Foo')).version == '1.0.9'


class TestDownloadCacheName:
    def test_zip_cached_under_deterministic_name_ignoring_server_name(self, addon_root, folder, monkeypatch, tmp_path):
        from gru.cache import download_name
        upstream = make_addon_info(id_=3, title='Foo', version='2.0', directories=['Foo'])
        _mock_download(monkeypatch, tmp_path, _zip_bytes({
            'Foo/Foo.txt': '## Title: Foo\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n'}))
        monkeypatch.setattr(install_mod.requests, 'head', lambda url, allow_redirects=True: _FakeResponse(
            headers={'content-disposition': 'attachment; filename="Server_Name.zip"'}))
        folder.unpack(upstream, as_api(StubAPI({'Foo': upstream})))
        cached = {p.name for p in tmp_path.rglob('*.zip')}
        assert cached == {download_name(3, 'Foo', '2.0')}
