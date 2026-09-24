#!/usr/bin/env python3

from __future__ import annotations

import sys
import typing
import pathlib
import datetime
import diff_match_patch
from typing import NamedTuple

from .config import encoding_open

if typing.TYPE_CHECKING:
    import gru.addon

Op = str
Lines = list[str]
BlockPatch = list[tuple[Op, Lines, bool]]  # bool: last line above has no trailing newline in its own file
BlockHeader = str
FilePatch = list[tuple[BlockHeader, BlockPatch]]
File = pathlib.Path
Patch = dict[tuple[File, File], FilePatch]

NO_NEWLINE_MARKER = r'\ No newline at end of file'


def format_file_mtime(fname: pathlib.Path | None) -> str:
    timestamp = 0 if fname is None else fname.stat().st_mtime
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime(r'%Y-%m-%d %H:%M:%S.%f %z')


def line_diff(orig_text: str, changed_text: str) -> str:
    # Line-splitting below treats '\n' as a separator: an unterminated final line would otherwise
    # run straight into whatever text_patch entry follows it, corrupting the diff. Diff against a
    # padded copy instead, flagged with NO_NEWLINE_MARKER so apply_patch() can leave it off again.
    # '\x00' keeps a same-content-but-different-newline-state final line from padding down to an
    # identical string on both sides, which would otherwise hide the only real difference there is.
    orig_has_nl = not orig_text or orig_text.endswith('\n')
    changed_has_nl = not changed_text or changed_text.endswith('\n')
    orig_padded = orig_text if orig_has_nl else orig_text + '\x00\n'
    changed_padded = changed_text if changed_has_nl else changed_text + '\x00\n'

    dmp = diff_match_patch.diff_match_patch()
    # Map lines to chars in both texts
    orig_chars, changed_chars, line_array = dmp.diff_linesToChars(orig_padded, changed_padded)

    # Generate diff and remap chars and operand-symbols to lines and prefixes
    text_patch = []
    orig_pos = changed_pos = 0
    for patch in dmp.patch_make(orig_chars, dmp.diff_main(orig_chars, changed_chars)):
        start = f'{patch.start1 + (1 if patch.length1 else 0)}{f",{patch.length1}" if patch.length1 != 1 else ""}'
        end = f'{patch.start2 + (1 if patch.length2 else 0)}{f",{patch.length2}" if patch.length2 != 2 else ""}'
        text_patch.append(f'@@ -{start} +{end} @@\n')

        for (op, data) in patch.diffs:
            sym = '+' if op > 0 else '-' if op < 0 else ' '
            for char in data:
                text_patch.append(sym + line_array[ord(char)].replace('\x00\n', '\n'))
                if op <= 0:
                    orig_pos += 1
                    if not orig_has_nl and orig_pos == len(orig_chars):
                        text_patch.append(f'{NO_NEWLINE_MARKER}\n')
                if op >= 0:
                    changed_pos += 1
                    if not changed_has_nl and changed_pos == len(changed_chars):
                        text_patch.append(f'{NO_NEWLINE_MARKER}\n')

    return ''.join(text_patch)


def parse_diff(handle: typing.IO) -> Patch:
    header: list[str] = []
    diff: FilePatch | None = None
    block: BlockPatch | None = None
    patch: Patch = {}
    mnemonic_top_dirs = tuple(map(set, ('ab', 'ci', 'co', 'cw', 'io', 'iw', 'ow', '12')))

    lines = iter(enumerate(ln.rstrip('\n') for ln in handle))
    for n, line in lines:
        if line.startswith('--- '):
            _, nextl = next(lines)
            if not nextl.startswith('+++ '):
                raise ValueError(f'Missing +++ line after --- line at line {n}')
            infile, outfile = (pathlib.Path(string[4:].lstrip().split('\t', 1)[0]) for string in (line, nextl))
            inout_files = (infile, outfile)
            # Drop mnemonics as first directory part
            if set(file.parts[0] for file in inout_files) in mnemonic_top_dirs:
                inout_files = (pathlib.Path(*infile.parts[1:]), pathlib.Path(*outfile.parts[1:]))

            # We want either twice the same file or 1 file, 1 /dev/null -- no other combinations
            if len(set(map(str, inout_files)) - {'/dev/null'}) != 1:
                raise ValueError(f'Incorrect file specification at line {n}')

            diff = patch.setdefault(inout_files, [])
            block = None

        elif diff is None:
            header.append(line)

        elif line.startswith('@@ '):
            _, block_header, _ = line.split('@@', 2)
            block = []
            diff.append((block_header.strip(), block))

        elif block is None:
            raise ValueError(f'Missing @@ header from diff block at line {n}')

        elif line.startswith(('-', ' ', '+')):
            if len(block) and block[-1][0] == line[0]:
                block[-1][1].append(line[1:])
            else:
                block.append((line[0], [line[1:]], False))

        elif line == NO_NEWLINE_MARKER:
            if not block:
                raise ValueError(f'Unexpected "no newline" marker at line {n}')
            op, block_lines, _ = block[-1]
            block[-1] = (op, block_lines, True)

        elif line:
            raise ValueError(f'Malformed line at line {n}')

        else:
            # If last line is empty, ignore it
            try:
                next(lines)
            except StopIteration:
                break
            else:
                raise ValueError(f'Malformed (empty) line at line {n}')

    return patch


def addon_diff(addon: gru.addon.InstalledAddon, orig_addon: gru.addon.InstalledAddon,
               out: typing.IO = sys.stdout) -> int:
    # Output some metadata
    utcnow = datetime.datetime.now(datetime.timezone.utc)
    print(
        f'Addon: {addon.title}', f'Version: {addon.version}',
        f'Date: {utcnow.ctime()} +0000', '', sep='\n', file=out
    )

    # Only diff text files
    text_ext = ('.addon', '.lua', '.txt', '.md', '.xml')
    files = {file for file in addon.files if file.suffix in text_ext}
    orig_files = {file for file in orig_addon.files if file.suffix in text_ext}

    n_diff_files = 0
    for file in files & orig_files:
        with encoding_open(addon.folder / file) as f, encoding_open(orig_addon.folder / file) as g:
            diff = line_diff(g.read(), f.read())
        if diff.strip():
            n_diff_files += 1
            print(f'--- {addon.folder.name}/{file}', format_file_mtime(orig_addon.folder / file), sep='\t', file=out)
            print(f'+++ {addon.folder.name}/{file}', format_file_mtime(addon.folder / file), sep='\t', file=out)
            print(diff, end='', file=out)

    for file in files - orig_files:
        n_diff_files += 1
        print('--- /dev/null', format_file_mtime(None), sep='\t', file=out)
        print(f'+++ {addon.folder.name}/{file}', format_file_mtime(addon.folder / file), sep='\t', file=out)
        with encoding_open(addon.folder / file) as f:
            print(line_diff('', f.read()), end='', file=out)

    for file in orig_files - files:
        n_diff_files += 1
        print(f'--- {addon.folder.name}/{file}', format_file_mtime(orig_addon.folder / file), sep='\t', file=out)
        print('+++ /dev/null', format_file_mtime(None), sep='\t', file=out)
        with encoding_open(orig_addon.folder / file) as f:
            print(line_diff(f.read(), ''), end='', file=out)

    return n_diff_files


def _post_image_at_offset(lines: Lines, expected: int, post_image: Lines, max_drift: int) -> bool:
    """ Whether `post_image` appears, line for line, at `expected` (0-indexed) or up to
    `max_drift` lines either side of it -- position may drift, but the sequence itself must
    still match exactly; no fuzzy content matching. """
    for drift in range(max_drift + 1):
        for pos in {expected - drift, expected + drift}:
            if pos >= 0 and lines[pos:pos + len(post_image)] == post_image:
                return True
    return False


def is_patch_applied(content: str, changes: FilePatch, *, max_drift: int = 20) -> bool:
    """ Whether `changes` (one file's hunks) already matches `content` -- i.e. every hunk's
    post-image (its ' ' and '+' lines, in order) appears verbatim within `max_drift` lines of
    the line number its header's +start declares. Some drift in position is tolerated (earlier
    hunks, or unrelated edits elsewhere, can shift later ones), but the sequence of lines itself
    is still matched exactly -- no fuzzy content matching.

    A hunk with no post-image at all (pure removal: only '-' lines) is skipped -- there's
    nothing here to verify was written, so it neither confirms nor denies the patch is applied. """
    current_lines = content.splitlines()
    for header, block in changes:
        start2 = int(header.split('+', 1)[1].split(',', 1)[0])
        post_image = [line for op, lines, _ in block if op in (' ', '+') for line in lines]
        if not post_image:
            continue
        if not _post_image_at_offset(current_lines, start2 - 1, post_image, max_drift):
            return False
    return True


def apply_patch(orig: str, patch: FilePatch) -> tuple[str, list[bool]]:
    dmp = diff_match_patch.diff_match_patch()

    # Trailing '\n' needed: parse_diff() stripped it off each stored line, and without it
    # back the last line loses its newline when reconstructed below -- unless that last line
    # genuinely has none of its own (NO_NEWLINE_MARKER), in which case leave it off.
    blocks = [block for header, changes in patch for block in changes]
    ends_without_nl = blocks[-1][2] if blocks else False
    diff_lines = '\n'.join(sum((lines for op, lines, no_nl in blocks), [])) + ('' if ends_without_nl else '\n')
    orig_chars, diff_chars, line_array = dmp.diff_linesToChars(orig, diff_lines)

    # Reconstitute a char-diff from the patch and remapped diff text
    iter_diff_chars = iter(diff_chars)
    char_patch = []
    for header, changes in patch:
        char_patch.append(f'@@ {header} @@')
        for op, lines, no_nl in changes:
            char_patch.append(f'\n{op}')
            for line, char in zip(lines, iter_diff_chars):
                char_patch.append(char)

    # Apply char diff
    result, values = dmp.patch_apply(dmp.patch_fromText(''.join(char_patch)), orig_chars)
    return ''.join(line_array[ord(char)] for char in result), values


class PatchError(Exception):
    """ The patch itself is invalid -- a malformed hunk, or paths outside the addon folder --
    as opposed to a hunk that just doesn't match current file content (see FileOutcome/PatchResult,
    which is how *that* gets reported: routine, not exceptional). """


class FileOutcome(NamedTuple):
    """ What happened to one file targeted by a patch. `applied` is True if any of its content
    (fully, or --partial-ly) was written, or it was cleanly removed. `failed` lists the hunk/op
    headers that didn't apply -- empty means this file applied cleanly. `reject` is where those
    were saved (--partial only; normal mode backs out instead of ever writing a .rej). """
    path: pathlib.Path
    applied: bool
    failed: list[str]
    reject: pathlib.Path | None


class PatchResult(NamedTuple):
    files: list[FileOutcome]
    backed_out: bool  # normal (non-partial) mode only: a failure meant nothing was written at all

    @property
    def clean(self) -> bool:
        return not self.backed_out and all(not f.failed for f in self.files)


class _FileComputation(NamedTuple):
    """ Internal: what one file's patch entry resolves to, computed before anything is written --
    so normal mode can check every file succeeded before committing any of them. """
    infile: File
    outfile: File
    write_path: pathlib.Path | None
    content: str | None
    remove_path: pathlib.Path | None
    failed: FilePatch  # failed hunks/ops for this file, as (header, block) pairs -- () if none

    @property
    def rel_path(self) -> pathlib.Path:
        """ Display name and .rej location, relative to the addon folder. """
        prefixed = self.outfile if str(self.outfile) != '/dev/null' else self.infile
        return pathlib.Path(*prefixed.parts[1:])


def _compute_create(addon: gru.addon.InstalledAddon, infile: File, outfile: File,
                    changes: FilePatch) -> _FileComputation:
    outpath = addon.folder.joinpath(*outfile.parts[1:]).resolve()
    header, block = changes[0] if changes else ('', [])
    op, dat, no_nl = block[0] if block else ('', [], False)
    if len(changes) != 1 or header.split()[0] != '-0,0' or len(block) != 1 or op != '+':
        raise PatchError(f'Malformed patch instructions on creating {outfile}')

    if outpath.exists():
        return _FileComputation(infile, outfile, None, None, None, changes)
    content = '\n'.join(dat) + ('' if no_nl else '\n')
    return _FileComputation(infile, outfile, outpath, content, None, [])


def _compute_remove(addon: gru.addon.InstalledAddon, infile: File, outfile: File,
                    changes: FilePatch) -> _FileComputation:
    inpath = addon.folder.joinpath(*infile.parts[1:]).resolve()
    header, block = changes[0] if changes else ('', [])
    op, dat, no_nl = block[0] if block else ('', [], False)
    if len(changes) != 1 or header.split()[-1] != '+0,0' or len(block) != 1 or op != '-':
        raise PatchError(f'Malformed patch instructions on removing {infile}')

    try:
        with encoding_open(inpath) as f:
            contents = f.read()
    except FileNotFoundError:
        return _FileComputation(infile, outfile, None, None, None, changes)

    if contents != '\n'.join(dat) + ('' if no_nl else '\n'):
        return _FileComputation(infile, outfile, None, None, None, changes)
    return _FileComputation(infile, outfile, None, None, inpath, [])


def _compute_modify(addon: gru.addon.InstalledAddon, infile: File, outfile: File,
                    changes: FilePatch) -> _FileComputation:
    inpath = addon.folder.joinpath(*infile.parts[1:]).resolve()
    outpath = addon.folder.joinpath(*outfile.parts[1:]).resolve()

    try:
        with encoding_open(inpath) as f:
            orig = f.read()
    except FileNotFoundError:
        return _FileComputation(infile, outfile, None, None, None, changes)

    try:
        result, values = apply_patch(orig, changes)
    except Exception:
        return _FileComputation(infile, outfile, None, None, None, changes)  # dmp can raise, not just report
    failed = [hunk for hunk, ok in zip(changes, values) if not ok]
    if len(failed) == len(changes):
        return _FileComputation(infile, outfile, None, None, None, failed)
    return _FileComputation(infile, outfile, outpath, result, None, failed)  # some/all hunks applied


def _compute(addon: gru.addon.InstalledAddon, infile: File, outfile: File, changes: FilePatch) -> _FileComputation:
    if str(infile) == '/dev/null':
        return _compute_create(addon, infile, outfile, changes)
    if str(outfile) == '/dev/null':
        return _compute_remove(addon, infile, outfile, changes)
    return _compute_modify(addon, infile, outfile, changes)


def _write_atomic(path: pathlib.Path, content: str) -> None:
    """ Write to a temp sibling first, then atomically replace -- so a write failure (encoding
    error, disk full, ...) partway through a multi-file patch never leaves a mix of old and new
    content across files that may depend on each other. """
    tmp = path.with_name(path.name + '.grutmp')
    with tmp.open('w') as f:
        print(content, file=f, end='')
    tmp.replace(path)


def _commit(computed: list[_FileComputation]) -> None:
    """ Write every file's new content to a temp sibling first (so a mid-write failure touches no
    real file), only then replace them all, and remove last. """
    staged = [(c.write_path, c.content) for c in computed if c.write_path is not None]
    staged_tmp = [(path.with_name(path.name + '.grutmp'), path, content) for path, content in staged]
    for tmp, _, content in staged_tmp:
        with tmp.open('w') as f:
            print(content, file=f, end='')
    for tmp, real, _ in staged_tmp:
        tmp.replace(real)
    for c in computed:
        if c.remove_path is not None:
            c.remove_path.unlink()


def _write_reject(addon: gru.addon.InstalledAddon, c: _FileComputation) -> pathlib.Path:
    """ Save failed hunks/ops as a small standalone patch, re-applicable on its own later
    (`gru patch <addon> <file>.rej`) -- same format `parse_diff()` already reads. """
    target = addon.folder / c.rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_path = target.with_name(target.name + '.rej')
    with reject_path.open('w') as f:
        print(f'--- {c.infile}', file=f)
        print(f'+++ {c.outfile}', file=f)
        for header, block in c.failed:
            print(f'@@ {header} @@', file=f)
            for op, lines, no_nl in block:
                for i, line in enumerate(lines):
                    print(f'{op}{line}', file=f)
                    if no_nl and i == len(lines) - 1:
                        print(NO_NEWLINE_MARKER, file=f)
    return reject_path


def addon_patch(addon: gru.addon.InstalledAddon, patch: Patch, *, partial: bool = False) -> PatchResult:
    """ Apply `patch` to `addon`'s files. Normal mode (partial=False) is all-or-nothing across
    every file in the patch -- code in one file may depend on another, so a hunk mismatch in one
    file backs out the whole patch rather than leaving files at inconsistent versions of each
    other. --partial applies every hunk that succeeds regardless, and saves what didn't to
    <file>.rej next to it, for manual reconciliation. """
    if not all(str(file) == '/dev/null' or file.parts[0] == addon.folder.name
               for inout_files in patch for file in inout_files):
        raise PatchError('Patch specifies changes outside of addon folder')

    computed = [_compute(addon, infile, outfile, changes) for (infile, outfile), changes in patch.items()]

    if not partial and any(c.failed for c in computed):
        outcomes = [FileOutcome(c.rel_path, False, [header for header, _ in c.failed], None) for c in computed]
        return PatchResult(outcomes, backed_out=True)

    to_write = computed if not partial else [c for c in computed if c.write_path or c.remove_path]
    _commit(to_write)

    outcomes = []
    for c in computed:
        applied = c.write_path is not None or c.remove_path is not None
        reject = _write_reject(addon, c) if partial and c.failed else None
        outcomes.append(FileOutcome(c.rel_path, applied, [header for header, _ in c.failed], reject))
    return PatchResult(outcomes, backed_out=False)


def addon_patch_file(addon: gru.addon.InstalledAddon, diff: pathlib.Path, *, partial: bool = False) -> PatchResult:
    try:
        with diff.open() as f:
            patch = parse_diff(f)
    except PatchError:
        raise
    except Exception as err:
        raise PatchError(f'Patch {diff.name} could not be read: {err}') from err
    return addon_patch(addon, patch, partial=partial)
