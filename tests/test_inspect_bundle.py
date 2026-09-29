"""Tests for Folder._inspect_bundle and zip-slip protection."""

import pytest

from .conftest import make_zip, StubAPI, StubAddon, MANIFEST


# ---------------------------------------------------------------------------
# Zip-slip / path traversal
# ---------------------------------------------------------------------------

class TestZipSlip:
    def test_traversal_filtered_out(self, folder, stub_api, addon_root):
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            '../../../evil.txt': 'pwned',
        })
        path = addon_root / 'MyAddon'
        _, _, files = folder._inspect_bundle(path, zf, stub_api)
        names = [str(fn) for fn, *_ in files]
        assert not any('..' in n for n in names)

    def test_absolute_path_filtered_out(self, folder, stub_api, addon_root):
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            '/etc/passwd': 'root:x:0:0',
        })
        path = addon_root / 'MyAddon'
        _, _, files = folder._inspect_bundle(path, zf, stub_api)
        names = [str(fn) for fn, *_ in files]
        assert not any(n.startswith('/') for n in names)


# ---------------------------------------------------------------------------
# Standard cases
# ---------------------------------------------------------------------------

class TestStandardBundle:
    def test_standard(self, folder, stub_api, addon_root):
        """Zip has MyAddon/MyAddon.txt — standard layout."""
        zf = make_zip({'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon')})
        path = addon_root / 'MyAddon'
        dest, erase, _ = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root
        assert erase == [path]

    def test_wrong_dir_name(self, folder, stub_api, addon_root):
        """Zip has OtherDir/OtherDir.txt — install dir differs from path.name (should not happen)."""
        zf = make_zip({'OtherDir/OtherDir.txt': MANIFEST.format(title='OtherDir')})
        path = addon_root / 'MyAddon'
        dest, erase, _ = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root
        assert erase == [addon_root / 'OtherDir']

    def test_unrecognised_manifest_raises(self, folder, stub_api, addon_root):
        """Zip has MyAddon/Other.txt — stem doesn't match dir, not a valid manifest."""
        zf = make_zip({'MyAddon/Other.txt': MANIFEST.format(title='Other')})
        path = addon_root / 'MyAddon'
        with pytest.raises(ValueError, match='No addon manifest'):
            folder._inspect_bundle(path, zf, stub_api)

    def test_naked_expected_name(self, folder, stub_api, addon_root):
        """Zip has MyAddon.txt at top level — naked bundle, expected name."""
        zf = make_zip({'MyAddon.txt': MANIFEST.format(title='MyAddon')})
        path = addon_root / 'MyAddon'
        dest, erase, _ = folder._inspect_bundle(path, zf, stub_api)
        assert dest == path  # contents go inside path

    def test_naked_unexpected_name(self, folder, stub_api, addon_root):
        """Zip has Other.txt at top level — naked bundle, unexpected name."""
        zf = make_zip({'Other.txt': MANIFEST.format(title='Other')})
        path = addon_root / 'MyAddon'
        dest, erase, _ = folder._inspect_bundle(path, zf, stub_api)
        assert dest.name == 'Other'

    def test_no_manifest_raises(self, folder, stub_api, addon_root):
        """Zip has no manifest at all."""
        zf = make_zip({'MyAddon/foo.lua': '-- code'})
        path = addon_root / 'MyAddon'
        with pytest.raises(ValueError, match='No addon manifest'):
            folder._inspect_bundle(path, zf, stub_api)

    def test_subdir_install(self, folder, stub_api, addon_root):
        """Addon already installed in a subdirectory — dest and erase target that subdir."""
        zf = make_zip({'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon')})
        path = addon_root / 'subdir' / 'MyAddon'
        dest, erase, _ = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root / 'subdir'
        assert erase == [path]


# ---------------------------------------------------------------------------
# Multi-directory bundles
# ---------------------------------------------------------------------------

class TestMultiDirBundle:
    def test_private_lib_kept(self, folder, addon_root):
        """Bundle includes a lib not in the API — keep it."""
        api = StubAPI({'MyAddon': StubAddon(1, 'MyAddon')})
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            'LibPrivate/LibPrivate.txt': MANIFEST.format(title='LibPrivate'),
        })
        path = addon_root / 'MyAddon'
        dest, erase, files = folder._inspect_bundle(path, zf, api)
        top_dirs = {fn.parts[0] for fn, *_ in files}
        assert 'LibPrivate' in top_dirs

    def test_existing_siblings_respected(self, folder, addon_root):
        """Multi-dir zip where all top-level dirs already exist on disk as siblings —
        install target is their shared parent, even if the libs are standalone in the API."""
        subdir = addon_root / 'subdir'
        (subdir / 'MyAddon').mkdir(parents=True)
        (subdir / 'LibFoo').mkdir(parents=True)
        (subdir / 'RedHerring').mkdir(parents=True)

        # LibFoo is a separate standalone addon in the API — would normally be excluded,
        # but the existing-siblings branch fires first and keeps it.
        api = StubAPI({
            'MyAddon': StubAddon(1, 'MyAddon'),
            'LibFoo': StubAddon(2, 'LibFoo'),
        })
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            'LibFoo/LibFoo.txt': MANIFEST.format(title='LibFoo'),
        })
        dest, erase, files = folder._inspect_bundle(subdir / 'MyAddon', zf, api)
        assert dest == subdir
        assert set(erase) == {subdir / 'MyAddon', subdir / 'LibFoo'}
        top_dirs = {fn.parts[0] for fn, *_ in files}
        assert top_dirs == {'MyAddon', 'LibFoo'}

    def test_existing_siblings_after_pruning(self, folder, addon_root):
        """After pruning a standalone, remaining dirs match siblings on disk — use their parent."""
        subdir = addon_root / 'subdir'
        (subdir / 'MyAddon').mkdir(parents=True)
        (subdir / 'LibPrivate').mkdir(parents=True)

        api = StubAPI({
            'MyAddon': StubAddon(1, 'MyAddon'),
            'OtherAddon': StubAddon(2, 'OtherAddon'),
        })
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            'LibPrivate/LibPrivate.txt': MANIFEST.format(title='LibPrivate'),
            'OtherAddon/OtherAddon.txt': MANIFEST.format(title='OtherAddon'),
        })
        dest, erase, files = folder._inspect_bundle(subdir / 'MyAddon', zf, api)
        assert dest == subdir
        assert set(erase) == {subdir / 'MyAddon', subdir / 'LibPrivate'}
        top_dirs = {fn.parts[0] for fn, *_ in files}
        assert 'OtherAddon' not in top_dirs

    def test_other_standalone_removed(self, folder, addon_root):
        """Bundle includes another addon that exists in the API — exclude it."""
        api = StubAPI({
            'MyAddon': StubAddon(1, 'MyAddon'),
            'OtherAddon': StubAddon(2, 'OtherAddon'),
        })
        zf = make_zip({
            'MyAddon/MyAddon.txt': MANIFEST.format(title='MyAddon'),
            'OtherAddon/OtherAddon.txt': MANIFEST.format(title='OtherAddon'),
        })
        path = addon_root / 'MyAddon'
        dest, erase, files = folder._inspect_bundle(path, zf, api)
        top_dirs = {fn.parts[0] for fn, *_ in files}
        assert 'OtherAddon' not in top_dirs
        assert 'MyAddon' in top_dirs


# ---------------------------------------------------------------------------
# Flat bundles: one top-level wrapper dir, no manifest of its own, several
# addons nested one level inside it (e.g. Arkadius' Trade Tools)
# ---------------------------------------------------------------------------

class TestNestedFlatBundle:
    def test_kept_nested_and_warns(self, folder, stub_api, addon_root):
        """Several addons nested under a manifest-less wrapper dir stay nested (the game
        scans AddOns/ recursively) but are now recognised and reported, instead of being
        silently treated as an opaque single addon."""
        zf = make_zip({
            'Bundle/Bundle/Bundle.txt': MANIFEST.format(title='Bundle'),
            'Bundle/BundleExtra/BundleExtra.txt': MANIFEST.format(title='BundleExtra'),
        })
        path = addon_root / 'Bundle'
        with pytest.warns(UserWarning, match='Installing 2 addons nested under Bundle/'):
            dest, erase, files = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root
        assert erase == [path]
        names = {str(fn) for fn, *_ in files}
        assert names == {'Bundle/Bundle/Bundle.txt', 'Bundle/BundleExtra/BundleExtra.txt'}

    def test_standalone_nested_addon_pruned(self, folder, addon_root):
        """A nested dir that's also independently listed in the API gets excluded,
        same as a sibling standalone addon would in the several-top-level-dirs case."""
        api = StubAPI({
            'Bundle': StubAddon(1, 'Bundle'),
            'BundleExtra': StubAddon(2, 'BundleExtra'),
        })
        zf = make_zip({
            'Bundle/Bundle/Bundle.txt': MANIFEST.format(title='Bundle'),
            'Bundle/BundleExtra/BundleExtra.txt': MANIFEST.format(title='BundleExtra'),
        })
        path = addon_root / 'Bundle'
        dest, erase, files = folder._inspect_bundle(path, zf, api)
        names = {str(fn) for fn, *_ in files}
        assert names == {'Bundle/Bundle/Bundle.txt'}

    def test_loose_wrapper_files_kept(self, folder, stub_api, addon_root):
        """LICENSE/README-style files sitting directly in the wrapper dir survive pruning."""
        zf = make_zip({
            'Bundle/LICENSE': 'MIT',
            'Bundle/Bundle/Bundle.txt': MANIFEST.format(title='Bundle'),
            'Bundle/BundleExtra/BundleExtra.txt': MANIFEST.format(title='BundleExtra'),
        })
        path = addon_root / 'Bundle'
        dest, erase, files = folder._inspect_bundle(path, zf, stub_api)
        names = {str(fn) for fn, *_ in files}
        assert 'Bundle/LICENSE' in names

    def test_single_nested_addon_wrapper_dropped(self, folder, stub_api, addon_root):
        """Only one addon nested in a wrapper dir (e.g. Srendarr's Srendarr/Srendarr/Srendarr.txt): install the
        addon dir itself, so an update doesn't move an existing Bundle/Bundle.txt install to Bundle/Bundle/."""
        zf = make_zip({
            'Bundle/README.md': 'readme',
            'Bundle/Bundle/Bundle.txt': MANIFEST.format(title='Bundle'),
            'Bundle/Bundle/Sub/Code.lua': 'x = 1',
        })
        path = addon_root / 'Bundle'
        with pytest.warns(UserWarning, match='Addon nested in wrapper dir Bundle/, installing Bundle/'):
            dest, erase, files = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root
        assert erase == [path]
        assert {(str(fn), str(src)) for fn, _, _, src in files} == {
            ('Bundle/Bundle.txt', 'Bundle/Bundle/Bundle.txt'),
            ('Bundle/Sub/Code.lua', 'Bundle/Bundle/Sub/Code.lua'),
        }

    def test_single_nested_addon_under_differently_named_wrapper(self, folder, stub_api, addon_root):
        zf = make_zip({'Bundle-1.2/src/Bundle/Bundle.txt': MANIFEST.format(title='Bundle')})
        path = addon_root / 'Bundle'
        with pytest.warns(UserWarning, match='Addon nested in wrapper dir Bundle-1.2/src/, installing Bundle/'):
            dest, erase, files = folder._inspect_bundle(path, zf, stub_api)
        assert dest == addon_root
        assert erase == [path]
        assert [str(fn) for fn, *_ in files] == ['Bundle/Bundle.txt']

    def test_mixed_depth_nesting_detected_by_manifest_location(self, folder, addon_root):
        """One addon's manifest sits 2 levels below the wrapper (through a pass-through 'src'
        dir, e.g. real-world 'AddOn/src/AddOn/AddOn.txt' layouts), the other only 1. Detection
        must be driven by where each manifest actually is, not by assuming a fixed +1 depth --
        otherwise the deeper one is neither recognised as nested nor eligible for pruning."""
        api = StubAPI({'BundleExtra': StubAddon(2, 'BundleExtra')})  # independently listed -> prune it
        zf = make_zip({
            'Bundle/src/Bundle/Bundle.txt': MANIFEST.format(title='Bundle'),
            'Bundle/BundleExtra/BundleExtra.txt': MANIFEST.format(title='BundleExtra'),
        })
        path = addon_root / 'Bundle'
        with pytest.warns(UserWarning, match='Installing 1 addons nested under Bundle/: Bundle'):
            dest, erase, files = folder._inspect_bundle(path, zf, api)
        names = {str(fn) for fn, *_ in files}
        assert names == {'Bundle/src/Bundle/Bundle.txt'}
