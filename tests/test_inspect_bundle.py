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
