# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Read-only identification of git checkouts (remote, ref, commit) by parsing .git directly """

from __future__ import annotations

import pathlib
import re
import zlib
from typing import NamedTuple


class GitInfo(NamedTuple):
    remote: str  # url, '' if none
    ref: str     # tag at HEAD, else branch, else commit sha
    sha: str     # '' on an unborn branch


def _git_dir(folder: pathlib.Path) -> pathlib.Path | None:
    dot = folder / '.git'
    if dot.is_dir():
        return dot
    if dot.is_file():  # worktree or submodule
        m = re.match(r'gitdir:\s*(.+)', dot.read_text())
        return (folder / m[1].strip()).resolve() if m else None
    return None


def _refs(common: pathlib.Path) -> tuple[dict[str, str], dict[str, str]]:
    """ ref -> sha, and tag ref -> peeled sha (from packed-refs only) """
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    packed = common / 'packed-refs'
    if packed.exists():
        last = None
        for line in packed.read_text().splitlines():
            if line.startswith('^') and last is not None:
                peeled[last] = line[1:]
            elif line and not line.startswith('#'):
                sha, last = line.split(' ', 1)
                refs[last] = sha
    for p in (common / 'refs').rglob('*'):
        if p.is_file():
            refs[p.relative_to(common).as_posix()] = p.read_text().strip()
            peeled.pop(p.relative_to(common).as_posix(), None)
    return refs, peeled


def _peel_loose(common: pathlib.Path, sha: str) -> str:
    """ Annotated tag object -> commit sha; `sha` unchanged if not a loose tag object """
    obj = common / 'objects' / sha[:2] / sha[2:]
    if obj.exists():
        data = zlib.decompress(obj.read_bytes())
        if data.startswith(b'tag '):
            m = re.search(rb'\0object ([0-9a-f]{40,64})', data)
            if m:
                return m[1].decode()
    return sha


def _config(common: pathlib.Path) -> dict[tuple[str, str], dict[str, str]]:
    sections: dict[tuple[str, str], dict[str, str]] = {}
    cur = None
    path = common / 'config'
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip()
        if line.startswith(('#', ';')):
            continue
        m = re.match(r'\[([^\s\]]+)(?:\s+"(.*)")?\]', line)
        if m:
            cur = sections.setdefault((m[1].lower(), m[2] or ''), {})
        elif cur is not None and '=' in line:
            k, v = line.split('=', 1)
            cur.setdefault(k.strip().lower(), v.strip().strip('"'))
    return sections


def git_info(folder: pathlib.Path) -> GitInfo | None:
    """ Identify the checkout at `folder`, or None if it is not one """
    gd = _git_dir(folder)
    if gd is None:
        return None
    common = (gd / (gd / 'commondir').read_text().strip()).resolve() if (gd / 'commondir').exists() else gd
    head = (gd / 'HEAD').read_text().strip()
    refs, peeled = _refs(common)

    branch = head[5:].removeprefix('refs/heads/') if head.startswith('ref: ') else None
    sha = refs.get(head[5:], '') if branch is not None else head
    tags = sorted(r.removeprefix('refs/tags/') for r, s in refs.items() if sha and r.startswith('refs/tags/')
                  and sha in (s, peeled.get(r) or _peel_loose(common, s)))

    cfg = _config(common)
    remotes = [sub for sec, sub in cfg if sec == 'remote']
    remote = cfg.get(('branch', branch or ''), {}).get('remote')
    if remote is None:
        remote = 'origin' if 'origin' in remotes else next(iter(remotes), '')
    url = cfg.get(('remote', remote), {}).get('url', '')

    return GitInfo(url, tags[0] if tags else branch or sha, sha)
