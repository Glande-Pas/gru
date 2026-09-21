#!/usr/bin/env python3

from __future__ import annotations

import sys
import typing
import pathlib
import datetime
import warnings
import diff_match_patch

from .config import encoding_open

if typing.TYPE_CHECKING:
    import gru.addon

Op = str
Lines = list[str]
BlockPatch = list[tuple[Op, Lines]]
BlockHeader = str
FilePatch = list[tuple[BlockHeader, BlockPatch]]
File = pathlib.Path
Patch = dict[tuple[File, File], FilePatch]


def format_file_mtime(fname: pathlib.Path | None) -> str:
    timestamp = 0 if fname is None else fname.stat().st_mtime
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC).strftime(r'%Y-%m-%d %H:%M:%S.%f %z')


def line_diff(orig_text: str, changed_text: str) -> str:
    dmp = diff_match_patch.diff_match_patch()
    # Map lines to chars in both texts
    orig_chars, changed_chars, line_array = dmp.diff_linesToChars(orig_text, changed_text)

    # Generate diff and remap chars and operand-symbols to lines and prefixes
    text_patch = []
    for patch in dmp.patch_make(orig_chars, dmp.diff_main(orig_chars, changed_chars)):
        start = f'{patch.start1 + (1 if patch.length1 else 0)}{f",{patch.length1}" if patch.length1 != 1 else ""}'
        end = f'{patch.start2 + (1 if patch.length2 else 0)}{f",{patch.length2}" if patch.length2 != 2 else ""}'
        text_patch.append(f'@@ -{start} +{end} @@\n')

        for (op, data) in patch.diffs:
            sym = '+' if op > 0 else '-' if op < 0 else ' '
            for char in data:
                text_patch.append(sym + line_array[ord(char)])

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
                block.append((line[0], [line[1:]]))

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
    utcnow = datetime.datetime.now(datetime.UTC)
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


def apply_patch(orig: str, patch: FilePatch) -> tuple[str, list[bool]]:
    dmp = diff_match_patch.diff_match_patch()

    # Trailing '\n' needed: parse_diff() stripped it off each stored line, and without it
    # back the last line loses its newline when reconstructed below.
    diff_lines = '\n'.join(sum((lines for header, changes in patch for op, lines in changes), [])) + '\n'
    orig_chars, diff_chars, line_array = dmp.diff_linesToChars(orig, diff_lines)

    # Reconstitute a char-diff from the patch and remapped diff text
    iter_diff_chars = iter(diff_chars)
    char_patch = []
    for header, changes in patch:
        char_patch.append(f'@@ {header} @@')
        for op, lines in changes:
            char_patch.append(f'\n{op}')
            for line, char in zip(lines, iter_diff_chars):
                char_patch.append(char)

    # Apply char diff
    result, values = dmp.patch_apply(dmp.patch_fromText(''.join(char_patch)), orig_chars)
    return ''.join(line_array[ord(char)] for char in result), values


def addon_patch(addon: gru.addon.InstalledAddon, patch: Patch) -> tuple[int, int]:
    if not all(str(file) == '/dev/null' or file.parts[0] == addon.folder.name
               for inout_files in patch for file in inout_files):
        raise ValueError('Patch specifies changes outside of addon folder')

    n_changed_files = 0
    for (infile, outfile), changes in patch.items():
        inpath = addon.folder.joinpath(*infile.parts[1:]).resolve()
        outpath = addon.folder.joinpath(*outfile.parts[1:]).resolve()

        if str(infile) == '/dev/null':
            header, block = changes[0] if changes else ('', [])
            op, dat = block[0] if block else ('', [])
            if len(changes) != 1 or header.split()[0] != '-0,0' or len(block) != 1 or op != '+':
                raise ValueError(f'Malformed patch instructions on creating {outfile}')

            if outpath.exists():
                warnings.warn(f'Patch failed for {outfile}: file already exists')
            else:
                with outpath.open('w') as f:
                    print(*dat, sep='\n', file=f)
                n_changed_files += 1
            continue

        if str(outfile) == '/dev/null':
            header, block = changes[0] if changes else ('', [])
            op, dat = block[0] if block else ('', [])
            if len(changes) != 1 or header.split()[-1] != '+0,0' or len(block) != 1 or op != '-':
                raise ValueError(f'Malformed patch instructions on removing {infile}')

            try:
                with encoding_open(inpath) as f:
                    contents = f.read()
            except FileNotFoundError:
                warnings.warn(f'Patch failed for {infile}: file does not exist')
                continue

            if contents != '\n'.join(dat) + '\n':
                warnings.warn(f'Patch failed for {infile}: contents not matching removed lines')
            else:
                inpath.unlink()
                n_changed_files += 1
            continue

        # Finally both files are the same
        try:
            with encoding_open(inpath) as f:
                orig = f.read()
        except FileNotFoundError:
            warnings.warn(f'Patch failed for {infile}: file does not exist')
            continue

        result, values = apply_patch(orig, changes)
        if not all(values):
            failed = []
            for n, ((head, _), ok) in enumerate(zip(changes, values), 1):
                if not ok:
                    failed.append(f'hunk #{n} at {head}')

            warnings.warn(f'Patch failed for {len(values) - sum(values)} hunks in {infile}: {", ".join(failed)}')
            # Don’t commit a patch that (partially) failed
            continue

        with outpath.open('w') as f:
            print(result, file=f, end='')
        n_changed_files += 1

    return n_changed_files, len(patch)


def addon_patch_file(addon: gru.addon.InstalledAddon, diff: pathlib.Path) -> tuple[int, int]:
    try:
        with diff.open() as f:
            patch = parse_diff(f)
    except Exception as err:
        warnings.warn(f'Patch {diff.name} failed: {err}')
        return 0, 0
    else:
        return addon_patch(addon, patch)
