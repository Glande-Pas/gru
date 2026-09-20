"""Tests for gru.patch: diff parsing/generation and patch application."""

import io
import pathlib
import warnings

import pytest

import gru.patch as patch_mod
from gru.patch import format_file_mtime, line_diff, parse_diff, addon_diff, apply_patch, addon_patch, addon_patch_file

from .conftest import make_installed


# ---------------------------------------------------------------------------
# format_file_mtime / line_diff (small pure helpers)
# ---------------------------------------------------------------------------

class TestFormatFileMtime:
    def test_none_gives_epoch(self):
        assert format_file_mtime(None).startswith('1970-01-01 00:00:00')

    def test_real_file_uses_its_mtime(self, tmp_path):
        f = tmp_path / 'x.txt'
        f.write_text('hi')
        result = format_file_mtime(f)
        assert result.startswith('20')  # sane year prefix, not epoch


class TestLineDiff:
    def test_identical_text_gives_empty_diff(self):
        assert line_diff('same\ntext\n', 'same\ntext\n') == ''

    def test_changed_line_produces_plus_minus(self):
        diff = line_diff('old line\n', 'new line\n')
        assert '-old line' in diff
        assert '+new line' in diff


# ---------------------------------------------------------------------------
# parse_diff
# ---------------------------------------------------------------------------

class TestParseDiff:
    def test_basic_hunk_parsed(self):
        text = (
            'Addon: Foo\nVersion: 1.0\nDate: today\n\n'
            '--- Foo/a.lua\t2024-01-01 00:00:00.000000 +0000\n'
            '+++ Foo/a.lua\t2024-01-02 00:00:00.000000 +0000\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '+new\n'
        )
        patch = parse_diff(io.StringIO(text))
        [(infile, outfile)] = patch.keys()
        assert str(infile) == 'Foo/a.lua' and str(outfile) == 'Foo/a.lua'
        [(header, block)] = patch[(infile, outfile)]
        assert header == '-1 +1'
        assert block == [('-', ['old']), ('+', ['new'])]

    def test_missing_plusplus_line_raises(self):
        text = '--- Foo/a.lua\tdate\nnotplusplus\n'
        with pytest.raises(ValueError, match=r'Missing \+\+\+'):
            parse_diff(io.StringIO(text))

    def test_mnemonic_prefix_stripped(self):
        """diff -u style a/ b/ prefixes are recognised and dropped."""
        text = (
            '--- a/Foo/a.lua\tdate\n'
            '+++ b/Foo/a.lua\tdate\n'
            '@@ -1 +1 @@\n'
            '-x\n'
            '+y\n'
        )
        patch = parse_diff(io.StringIO(text))
        [(infile, outfile)] = patch.keys()
        assert str(infile) == 'Foo/a.lua'
        assert str(outfile) == 'Foo/a.lua'

    def test_content_line_before_at_header_raises(self):
        text = '--- Foo/a.lua\tdate\n+++ Foo/a.lua\tdate\n-stray line\n'
        with pytest.raises(ValueError, match='Missing @@ header'):
            parse_diff(io.StringIO(text))

    def test_dev_null_in_out_pair_allowed(self):
        text = (
            '--- /dev/null\tdate\n'
            '+++ Foo/new.lua\tdate\n'
            '@@ -0,0 +1 @@\n'
            '+hi\n'
        )
        patch = parse_diff(io.StringIO(text))
        [(infile, outfile)] = patch.keys()
        assert str(infile) == '/dev/null'
        assert str(outfile) == 'Foo/new.lua'

    def test_two_real_files_that_differ_raises(self):
        """Neither side is /dev/null, and the two filenames don't match -- invalid."""
        text = '--- Foo/a.lua\tdate\n+++ Foo/b.lua\tdate\n'
        with pytest.raises(ValueError, match='Incorrect file specification'):
            parse_diff(io.StringIO(text))


# ---------------------------------------------------------------------------
# addon_diff + parse_diff + addon_patch round trip
# ---------------------------------------------------------------------------

class TestRoundTripModify:
    def test_modified_middle_line_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('old = 1\nkeep\n')
        (new.folder / 'Data.lua').write_text('old = 2\nkeep\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        done, total = addon_patch(orig, patch)

        assert (done, total) == (1, 1)
        assert (orig.folder / 'Data.lua').read_text() == 'old = 2\nkeep\n'

    def test_modified_last_line_round_trips(self, tmp_path):
        """Regression: apply_patch() used to drop the trailing newline here."""
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('old = 1\n')
        (new.folder / 'Data.lua').write_text('old = 2\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        done, total = addon_patch(orig, patch)

        assert (done, total) == (1, 1)
        assert (orig.folder / 'Data.lua').read_text() == 'old = 2\n'

    def test_identical_files_produce_no_diff(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('same\n')
        (new.folder / 'Data.lua').write_text('same\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 0


class TestRoundTripCreateDelete:
    def test_created_file_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (new.folder / 'Data.lua').write_text('return 42\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        done, total = addon_patch(orig, patch)

        assert (done, total) == (1, 1)
        assert (orig.folder / 'Data.lua').read_text() == 'return 42\n'

    def test_deleted_file_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Old.lua').write_text('legacy\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        done, total = addon_patch(orig, patch)

        assert (done, total) == (1, 1)
        assert not (orig.folder / 'Old.lua').exists()


# ---------------------------------------------------------------------------
# addon_patch against hand-built Patch structures (bypasses addon_diff)
# ---------------------------------------------------------------------------

class TestAddonPatchCreate:
    def test_create_new_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,2', [('+', ['line1', 'line2'])])],
        }
        done, total = addon_patch(addon, patch)
        assert (done, total) == (1, 1)
        assert (addon.folder / 'New.lua').read_text() == 'line1\nline2\n'

    def test_create_skips_if_file_already_exists(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'New.lua').write_text('existing\n')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,1', [('+', ['line1'])])],
        }
        with pytest.warns(UserWarning, match='already exists'):
            done, total = addon_patch(addon, patch)
        assert (done, total) == (0, 1)
        assert (addon.folder / 'New.lua').read_text() == 'existing\n'

    def test_malformed_create_hunk_raises(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,1', [('-', ['not-a-plus'])])],
        }
        with pytest.raises(ValueError, match='Malformed patch instructions on creating'):
            addon_patch(addon, patch)


class TestAddonPatchDelete:
    def test_delete_existing_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Old.lua').write_text('line1\n')
        patch = {
            (_p('MyAddon/Old.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'])])],
        }
        done, total = addon_patch(addon, patch)
        assert (done, total) == (1, 1)
        assert not (addon.folder / 'Old.lua').exists()

    def test_delete_content_mismatch_warns_and_keeps_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Old.lua').write_text('different content\n')
        patch = {
            (_p('MyAddon/Old.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'])])],
        }
        with pytest.warns(UserWarning, match='contents not matching'):
            done, total = addon_patch(addon, patch)
        assert (done, total) == (0, 1)
        assert (addon.folder / 'Old.lua').exists()

    def test_delete_missing_file_warns(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/Gone.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'])])],
        }
        with pytest.warns(UserWarning, match='does not exist'):
            done, total = addon_patch(addon, patch)
        assert (done, total) == (0, 1)


class TestAddonPatchModifyPartialFailure:
    def test_hunk_failure_warns_and_leaves_file_untouched(self, tmp_path, monkeypatch):
        """A hunk that apply_patch() reports as unapplied must not be committed to disk."""
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('actual content\n')
        changes = [('-1 +1', [('-', ['old']), ('+', ['new'])])]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('new content\n', [False]))

        with pytest.warns(UserWarning, match='Patch failed for 1 hunks in MyAddon/Data.lua'):
            done, total = addon_patch(addon, patch)

        assert (done, total) == (0, 1)
        assert (addon.folder / 'Data.lua').read_text() == 'actual content\n'

    def test_one_of_several_hunks_failing_skips_whole_file(self, tmp_path, monkeypatch):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('actual content\n')
        changes = [
            ('-1 +1', [('-', ['old1']), ('+', ['new1'])]),
            ('-5 +5', [('-', ['old2']), ('+', ['new2'])]),
        ]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('new content\n', [True, False]))

        with pytest.warns(UserWarning, match='hunk #2'):
            done, total = addon_patch(addon, patch)

        assert (done, total) == (0, 1)
        assert (addon.folder / 'Data.lua').read_text() == 'actual content\n'


class TestAddonPatchSafety:
    def test_patch_outside_addon_folder_raises(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('OtherAddon/Evil.lua', dev_null_in=True)): [('-0,0 +1,1', [('+', ['pwned'])])],
        }
        with pytest.raises(ValueError, match='outside of addon folder'):
            addon_patch(addon, patch)


class TestAddonPatchFile:
    def test_unreadable_patch_file_returns_zero_and_warns(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        missing = tmp_path / 'nope.patch'
        with pytest.warns(UserWarning, match='failed'):
            done, total = addon_patch_file(addon, missing)
        assert (done, total) == (0, 0)

    def test_valid_patch_file_parses_and_applies(self, tmp_path):
        """Regression: addon_patch_file()'s success path (parse_diff -> addon_patch) was never exercised."""
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('old = 1\n')
        (new.folder / 'Data.lua').write_text('old = 2\n')

        diff_file = tmp_path / 'MyAddon.patch'
        with diff_file.open('w') as out:
            n = addon_diff(new, orig, out=out)
        assert n == 1

        done, total = addon_patch_file(orig, diff_file)

        assert (done, total) == (1, 1)
        assert (orig.folder / 'Data.lua').read_text() == 'old = 2\n'


def _p(spec: str, dev_null_in: bool = False, dev_null_out: bool = False):
    """Build a (infile, outfile) key for a hand-built Patch dict."""
    import pathlib
    infile = pathlib.Path('/dev/null') if dev_null_in else pathlib.Path(spec)
    outfile = pathlib.Path('/dev/null') if dev_null_out else pathlib.Path(spec)
    return (infile, outfile)
