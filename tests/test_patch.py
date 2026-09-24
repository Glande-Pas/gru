"""Tests for gru.patch: diff parsing/generation and patch application."""

import io
import pathlib

import pytest

import gru.patch as patch_mod
from gru.patch import (
    format_file_mtime, line_diff, parse_diff, addon_diff, addon_patch, addon_patch_file, is_patch_applied, PatchError,
)

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


class TestLineDiffNoTrailingNewline:
    """Regression: a final line lacking '\\n' used to run straight into whatever text_patch
    entry followed it (e.g. '-foo+bar' instead of '-foo\\n+bar\\n'), corrupting the diff."""

    def test_last_line_changed_does_not_glue_to_next_line(self):
        diff = line_diff('foo', 'bar')
        assert '-foo\n' in diff
        assert '+bar\n' in diff

    def test_no_trailing_newline_side_gets_marker(self):
        diff = line_diff('line1\nline2', 'line1\nline2\nline3\n')
        assert f'-line2\n{patch_mod.NO_NEWLINE_MARKER}\n' in diff

    def test_identical_text_without_trailing_newline_gives_empty_diff(self):
        assert line_diff('same\ntext', 'same\ntext') == ''


def _changes_for(orig: str, changed: str):
    """ The FilePatch (hunks) diffing orig -> changed, as is_patch_applied() consumes it. """
    text = f'--- a/file\tdate\n+++ b/file\tdate\n{line_diff(orig, changed)}'
    [(_, changes)] = parse_diff(io.StringIO(text)).items()
    return changes


class TestIsPatchApplied:
    """No fuzzing: a hunk's ' '/'+' lines must appear verbatim at its header's exact +start
    line number, not just somewhere plausible nearby."""

    def test_unapplied_original_content_reports_false(self):
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        assert is_patch_applied('a\nold\nc', changes) is False

    def test_applied_content_reports_true(self):
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        assert is_patch_applied('a\nnew\nc', changes) is True

    def test_unrelated_content_reports_false(self):
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        assert is_patch_applied('a\nother\nc', changes) is False

    def test_created_file_missing_reports_false(self):
        changes = _changes_for('', 'x\ny\n')
        assert is_patch_applied('', changes) is False

    def test_created_file_present_reports_true(self):
        changes = _changes_for('', 'x\ny\n')
        assert is_patch_applied('x\ny\n', changes) is True

    def test_only_some_of_several_hunks_applied_reports_false(self):
        orig = '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'
        changed = '1\nA\n3\n4\n5\n6\n7\n8\n9\nB\n'
        changes = _changes_for(orig, changed)
        partially_applied = '1\nA\n3\n4\n5\n6\n7\n8\n9\n10\n'  # first hunk applied, second isn't
        assert is_patch_applied(partially_applied, changes) is False

    def test_all_hunks_applied_reports_true(self):
        orig = '1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n'
        changed = '1\nA\n3\n4\n5\n6\n7\n8\n9\nB\n'
        changes = _changes_for(orig, changed)
        assert is_patch_applied(changed, changes) is True

    def test_shifted_within_drift_still_counts(self):
        """Position may drift (earlier edits can shift later hunks down) as long as the exact
        line sequence is still found somewhere within max_drift."""
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        assert is_patch_applied('X\na\nnew\nc', changes) is True

    def test_shifted_beyond_drift_reports_false(self):
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        padding = '\n'.join(f'pad{i}' for i in range(25))
        assert is_patch_applied(f'{padding}\na\nnew\nc', changes, max_drift=20) is False

    def test_content_never_present_within_drift_reports_false(self):
        """Drift tolerance must not degenerate into a plain 'is this text anywhere in the
        file' search -- content that plain doesn't exist still reports not-applied."""
        changes = _changes_for('a\nold\nc', 'a\nnew\nc')
        assert is_patch_applied('a\nold\nc', changes) is False

    def test_pure_removal_hunk_has_nothing_to_confirm(self):
        """A hunk with only '-' lines has no post-image to check -- neither confirms nor
        denies the patch is applied, so a file with that content removed still reports True."""
        changes = _changes_for('a\nold\nc', 'a\nc')
        assert is_patch_applied('a\nc', changes) is True


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
        assert block == [('-', ['old'], False), ('+', ['new'], False)]

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
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
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
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
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
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
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
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert not (orig.folder / 'Old.lua').exists()


class TestRoundTripNoTrailingNewline:
    """Regression: a file's own missing trailing newline used to get silently added (or the
    patch failed to apply at all) -- see line_diff()/apply_patch()'s NO_NEWLINE_MARKER handling."""

    def test_modified_last_line_without_trailing_newline_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('old = 1')
        (new.folder / 'Data.lua').write_text('old = 2')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert (orig.folder / 'Data.lua').read_text() == 'old = 2'

    def test_adding_trailing_newline_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('unchanged')
        (new.folder / 'Data.lua').write_text('unchanged\n')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert (orig.folder / 'Data.lua').read_text() == 'unchanged\n'

    def test_removing_trailing_newline_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Data.lua').write_text('unchanged\n')
        (new.folder / 'Data.lua').write_text('unchanged')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert (orig.folder / 'Data.lua').read_text() == 'unchanged'

    def test_created_file_without_trailing_newline_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (new.folder / 'Data.lua').write_text('return 42')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert (orig.folder / 'Data.lua').read_text() == 'return 42'

    def test_deleted_file_without_trailing_newline_round_trips(self, tmp_path):
        orig = make_installed(tmp_path / 'orig', 'MyAddon')
        new = make_installed(tmp_path / 'new', 'MyAddon')
        (orig.folder / 'Old.lua').write_text('legacy')

        stream = io.StringIO()
        n = addon_diff(new, orig, out=stream)
        assert n == 1

        stream.seek(0)
        patch = parse_diff(stream)
        result = addon_patch(orig, patch)

        assert result.clean and len(result.files) == 1
        assert not (orig.folder / 'Old.lua').exists()


# ---------------------------------------------------------------------------
# addon_patch against hand-built Patch structures (bypasses addon_diff)
# ---------------------------------------------------------------------------

class TestAddonPatchCreate:
    def test_create_new_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,2', [('+', ['line1', 'line2'], False)])],
        }
        result = addon_patch(addon, patch)
        assert result.clean
        assert (addon.folder / 'New.lua').read_text() == 'line1\nline2\n'

    def test_create_skips_if_file_already_exists(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'New.lua').write_text('existing\n')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,1', [('+', ['line1'], False)])],
        }
        result = addon_patch(addon, patch)
        assert result.backed_out
        assert (addon.folder / 'New.lua').read_text() == 'existing\n'

    def test_malformed_create_hunk_raises(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,1', [('-', ['not-a-plus'], False)])],
        }
        with pytest.raises(PatchError, match='Malformed patch instructions on creating'):
            addon_patch(addon, patch)


class TestAddonPatchDelete:
    def test_delete_existing_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Old.lua').write_text('line1\n')
        patch = {
            (_p('MyAddon/Old.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'], False)])],
        }
        result = addon_patch(addon, patch)
        assert result.clean
        assert not (addon.folder / 'Old.lua').exists()

    def test_delete_content_mismatch_backs_out_and_keeps_file(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Old.lua').write_text('different content\n')
        patch = {
            (_p('MyAddon/Old.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'], False)])],
        }
        result = addon_patch(addon, patch)
        assert result.backed_out
        assert (addon.folder / 'Old.lua').exists()

    def test_delete_missing_file_backs_out(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('MyAddon/Gone.lua', dev_null_out=True)): [('-1 +0,0', [('-', ['line1'], False)])],
        }
        result = addon_patch(addon, patch)
        assert result.backed_out


class TestAddonPatchModifyFailureNormalMode:
    def test_hunk_failure_backs_out_and_leaves_file_untouched(self, tmp_path, monkeypatch):
        """A hunk that apply_patch() reports as unapplied must not be committed to disk."""
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('actual content\n')
        changes = [('-1 +1', [('-', ['old'], False), ('+', ['new'], False)])]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('new content\n', [False]))

        result = addon_patch(addon, patch)

        assert result.backed_out
        assert result.files[0].failed == ['-1 +1']
        assert (addon.folder / 'Data.lua').read_text() == 'actual content\n'

    def test_apply_patch_raising_outright_is_treated_as_failure_not_a_crash(self, tmp_path):
        """Regression: found via manual verification -- a second hunk whose context matches
        nothing makes diff_match_patch's own reconstruction raise IndexError, not just report
        that hunk unapplied. That must degrade to a normal failed-file outcome, not propagate."""
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('line1\nline2\nline3\n')
        changes = [
            ('-1 +1', [('-', ['line1'], False), ('+', ['LINE1'], False)]),
            ('-99 +99', [('-', ['nonexistent'], False), ('+', ['NONEXISTENT'], False)]),
        ]
        patch = {_p('MyAddon/Data.lua'): changes}

        result = addon_patch(addon, patch)  # real apply_patch(), not mocked -- must not raise

        assert result.backed_out
        assert result.files[0].failed == ['-1 +1', '-99 +99']
        assert (addon.folder / 'Data.lua').read_text() == 'line1\nline2\nline3\n'

    def test_one_failing_hunk_backs_out_the_whole_file(self, tmp_path, monkeypatch):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('actual content\n')
        changes = [
            ('-1 +1', [('-', ['old1'], False), ('+', ['new1'], False)]),
            ('-5 +5', [('-', ['old2'], False), ('+', ['new2'], False)]),
        ]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('new content\n', [True, False]))

        result = addon_patch(addon, patch)

        assert result.backed_out
        assert result.files[0].failed == ['-5 +5']
        assert (addon.folder / 'Data.lua').read_text() == 'actual content\n'

    def test_one_file_failing_backs_out_every_file_in_the_patch(self, tmp_path, monkeypatch):
        """The whole point: files may depend on each other, so one file's mismatch must not
        leave another file in the patch updated on its own."""
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Good.lua').write_text('good old\n')
        (addon.folder / 'Bad.lua').write_text('bad old\n')
        patch = {
            _p('MyAddon/Good.lua'): [('-1 +1', [('-', ['good old'], False), ('+', ['good new'], False)])],
            _p('MyAddon/Bad.lua'): [('-1 +1', [('-', ['bad old'], False), ('+', ['bad new'], False)])],
        }

        def fake_apply(orig, changes):
            ok = 'good' in orig
            return ('new\n', [ok])
        monkeypatch.setattr(patch_mod, 'apply_patch', fake_apply)

        result = addon_patch(addon, patch)

        assert result.backed_out
        assert (addon.folder / 'Good.lua').read_text() == 'good old\n'  # untouched despite matching cleanly
        assert (addon.folder / 'Bad.lua').read_text() == 'bad old\n'
        assert not list(tmp_path.glob('*.grutmp'))  # no stray temp files left behind


class TestAddonPatchPartial:
    def test_partial_writes_successful_hunks_and_saves_reject(self, tmp_path, monkeypatch):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('actual content\n')
        changes = [
            ('-1 +1', [('-', ['old1'], False), ('+', ['new1'], False)]),
            ('-5 +5', [('-', ['old2'], False), ('+', ['new2'], False)]),
        ]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('partially patched\n', [True, False]))

        result = addon_patch(addon, patch, partial=True)

        assert not result.backed_out
        assert not result.clean
        [outcome] = result.files
        assert outcome.applied is True
        assert outcome.failed == ['-5 +5']
        assert (addon.folder / 'Data.lua').read_text() == 'partially patched\n'
        assert outcome.reject is not None
        assert outcome.reject == addon.folder / 'Data.lua.rej'
        assert outcome.reject.exists()

    def test_reject_file_is_a_reapplicable_patch(self, tmp_path, monkeypatch):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Data.lua').write_text('one\ntwo\n')
        changes = [('-2 +2', [('-', ['two'], False), ('+', ['deux'], False)])]
        patch = {_p('MyAddon/Data.lua'): changes}

        monkeypatch.setattr(patch_mod, 'apply_patch', lambda orig, changes: ('one\ntwo\n', [False]))
        result = addon_patch(addon, patch, partial=True)
        [outcome] = result.files
        assert outcome.reject is not None

        reparsed = parse_diff(outcome.reject.open())
        [(infile, outfile)] = reparsed.keys()
        assert str(infile) == 'MyAddon/Data.lua' and str(outfile) == 'MyAddon/Data.lua'
        assert reparsed[(infile, outfile)] == changes

    def test_partial_leaves_fully_failed_file_untouched_but_still_rejects(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'New.lua').write_text('existing\n')
        patch = {
            (_p('MyAddon/New.lua', dev_null_in=True)): [('-0,0 +1,1', [('+', ['line1'], False)])],
        }
        result = addon_patch(addon, patch, partial=True)
        [outcome] = result.files
        assert outcome.applied is False
        assert (addon.folder / 'New.lua').read_text() == 'existing\n'
        assert outcome.reject is not None and outcome.reject.exists()

    def test_partial_does_not_affect_files_that_apply_cleanly(self, tmp_path, monkeypatch):
        addon = make_installed(tmp_path, 'MyAddon')
        (addon.folder / 'Good.lua').write_text('good old\n')
        (addon.folder / 'Bad.lua').write_text('bad old\n')
        patch = {
            _p('MyAddon/Good.lua'): [('-1 +1', [('-', ['good old'], False), ('+', ['good new'], False)])],
            _p('MyAddon/Bad.lua'): [('-1 +1', [('-', ['bad old'], False), ('+', ['bad new'], False)])],
        }

        def fake_apply(orig, changes):
            ok = 'good' in orig
            return (('good new\n' if ok else orig), [ok])
        monkeypatch.setattr(patch_mod, 'apply_patch', fake_apply)

        result = addon_patch(addon, patch, partial=True)

        assert (addon.folder / 'Good.lua').read_text() == 'good new\n'
        assert (addon.folder / 'Bad.lua').read_text() == 'bad old\n'
        by_name = {f.path.name: f for f in result.files}
        assert by_name['Good.lua'].applied and not by_name['Good.lua'].failed
        assert not by_name['Bad.lua'].applied and by_name['Bad.lua'].failed


class TestAddonPatchSafety:
    def test_patch_outside_addon_folder_raises(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        patch = {
            (_p('OtherAddon/Evil.lua', dev_null_in=True)): [('-0,0 +1,1', [('+', ['pwned'], False)])],
        }
        with pytest.raises(PatchError, match='outside of addon folder'):
            addon_patch(addon, patch)


class TestAddonPatchFile:
    def test_unreadable_patch_file_raises_patch_error(self, tmp_path):
        addon = make_installed(tmp_path, 'MyAddon')
        missing = tmp_path / 'nope.patch'
        with pytest.raises(PatchError):
            addon_patch_file(addon, missing)

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

        result = addon_patch_file(orig, diff_file)

        assert result.clean
        assert (orig.folder / 'Data.lua').read_text() == 'old = 2\n'


def _p(spec: str, dev_null_in: bool = False, dev_null_out: bool = False):
    """Build a (infile, outfile) key for a hand-built Patch dict."""
    infile = pathlib.Path('/dev/null') if dev_null_in else pathlib.Path(spec)
    outfile = pathlib.Path('/dev/null') if dev_null_out else pathlib.Path(spec)
    return (infile, outfile)
