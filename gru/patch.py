#!/usr/bin/env python3

from __future__ import annotations

import io
import sys
import pathlib
import datetime
import warnings
import diff_match_patch

from .config import encoding_open

Op = str
Lines = list[str]
BlockPatch = list[tuple[Op, Lines]]
BlockHeader = str
FilePatch = list[tuple[BlockHeader, BlockPatch]]
File = str
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


def parse_diff(path: pathlib.Path) -> Patch:
    header: list[str] = []
    diff: FilePatch | None = None
    block: Blockpatch | None = None
    patch: Patch = {}
    mnemonic_top_dirs = tuple(map(set, ('ab', 'ci', 'co', 'cw', 'io', 'iw', 'ow', '12')))

    with path.open() as f:
        lines = iter(enumerate(ln.rstrip('\n') for ln in f))
        for n, line in lines:
            if line.startswith('--- '):
                _, nextl = next(lines)
                if not nextl.startswith('+++ '):
                    raise ValueError(f'Missing +++ line after --- line at line {n}')
                inout_files = tuple([
                    pathlib.Path(string[4:].lstrip().split('\t', 1)[0]) for string in (line, nextl)
                ])
                # Drop mnemonics as first directory part
                if set(file.parts[0] for file in inout_files) in mnemonic_top_dirs:
                    inout_files = tuple(pathlib.Path(*file.parts[1:]) for file in inout_files)

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

            else:
                raise ValueError(f'Malformed line at line {n}')

        return patch


def addon_diff(addon: gru.addon.Addon, orig_addon: gru.addon.Addon, out=sys.stdout) -> int:
    # Output some metadata
    utcnow = datetime.datetime.now(datetime.UTC)
    print(
        f'Addon: {addon.metadata["title"]}', f'Version: {addon.metadata["installed_version"]}',
        f'Date: {utcnow.ctime()} +0000', '', sep='\n', file=out
    )

    # Only diff text files
    text_ext = ('.addon', '.lua', '.txt', '.md', '.xml')
    files = {file for file in addon.files if file.suffix in text_ext}
    orig_files = {file for file in orig_addon.files if file.suffix in text_ext}

    n_diff_files = 0
    for file in files & orig_files:
        with encoding_open(addon.folder / file) as f, encoding_open(orig_addon.folder / file) as g:
            diff = line_diff(f.read(), g.read())
        if diff.strip():
            n_diff_files += 1
            print(f'--- {addon.folder.name}/{file}', format_file_mtime(orig_addon.folder / file), sep='\t', file=out)
            print(f'+++ {addon.folder.name}/{file}', format_file_mtime(addon.folder / file), sep='\t', file=out)
            print(diff, end='', file=out)

    for file in files - orig_files:
        n_diff_files += 1
        print(f'--- /dev/null', format_file_mtime(None), sep='\t', file=out)
        print(f'+++ {addon.folder.name}/{file}', format_file_mtime(addon.folder / file), sep='\t', file=out)
        with encoding_open(addon.folder / file) as f:
            print(line_diff('\n', f.read()), end='')

    for file in orig_files - files:
        n_diff_files += 1
        print(f'--- {addon.folder.name}/{file}', format_file_mtime(orig_addon.folder / file), sep='\t', file=out)
        print(f'+++ /dev/null', format_file_mtime(None), sep='\t', file=out)
        with encoding_open(orig_addon.folder / file) as f:
            print(line_diff(f.read(), '\n'), end='')

    return n_diff_files


def apply_patch(orig: str, patch: FilePatch) -> str:
    dmp = diff_match_patch.diff_match_patch()

    diff_lines = '\n'.join(sum((lines for header, changes in patch for op, lines in changes), []))
    orig_chars, diff_chars, line_array = dmp.diff_linesToChars(orig, diff_lines)

    # Reconstitute a char-diff from the patch and remapped diff text
    iter_diff_chars = iter(diff_chars)
    char_patch = []
    for header, changes in patch:
        char_patch.append(f'@@ {header} @@')
        last_op = None
        for op, lines in changes:
            char_patch.append(f'\n{op}')
            for line, char in zip(lines, iter_diff_chars):
                char_patch.append(char)

    # Apply char diff
    result, values = dmp.patch_apply(dmp.patch_fromText(''.join(char_patch)), orig_chars)
    return ''.join(line_array[ord(char)] for char in result), values


def addon_patch(addon: gru.addon.Addon, diff: pathlib.Path) -> tuple[int, int]:
    try:
        patch = parse_diff(diff)

        if not all(str(file) == '/dev/null' or file.parts[0] == addon.folder.name for inout_files in patch for file in inout_files):
            raise ValueError('Patch specifies changes outside of addon folder')
    except Exception as err:
        warnings.warn(f'Patch {diff.name} failed: {err}')
        return 0, 0

    n_changed_files = 0
    for (infile, outfile), changes in patch.items():
        inpath = addon.folder.joinpath(*infile.parts[1:]).resolve()
        outpath = addon.folder.joinpath(*outfile.parts[1:]).resolve()

        if str(infile) == '/dev/null':
            if len(changes) != 1 or changes[0][0].split()[0] != '-0,0' or len(changes[0][1]) != 1 or changes[0][1][0] != '+':
                raise ValueError(f'Malformed patch instructions on creating {outfile}')

            if outpath.exists():
                warnings.warn(f'Patch failed for {outfile}: file already exists')
            else:
                with outpath.open('w') as f:
                    print(*changes[0][1][1], sep='\n', file=f)
                n_changed_files += 1
            continue

        if str(outfile) == '/dev/null':
            print(str(infile), str(outfile))
            if len(changes) != 1 or changes[0][0].split()[-1] != '+0,0' or len(changes[0][1]) != 1 or changes[0][1][0] != '-':
                raise ValueError(f'Malformed patch instructions on removing {infile}')

            try:
                with encoding_open(inpath) as f:
                    contents = f.read()
            except FileNotFoundError:
                warnings.warn(f'Patch failed for {infile}: file does not exist')
                continue

            if contents != changes[0][1][1] + '\n':
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
