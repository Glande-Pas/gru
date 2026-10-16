"""Tests for gru.git, using the git CLI both to build repos and as the oracle."""

import os
import shutil
import subprocess

import pytest

from gru.git import GitInfo, git_info

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')


@pytest.fixture
def git(monkeypatch):
    for var in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        monkeypatch.delenv(var, raising=False)
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@t'}

    def run(cwd, *args, check=True):
        return subprocess.run(['git', '-c', 'init.defaultBranch=main', '-c', 'protocol.file.allow=always', *args],
                              cwd=cwd, env=env, capture_output=True, text=True, check=check).stdout.strip()
    return run


@pytest.fixture
def repo(tmp_path, git):
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init')
    git(path, 'commit', '--allow-empty', '-m', 'one')
    return path


def info(path):
    result = git_info(path)
    assert result is not None
    return result


def oracle(git, path):
    sha = git(path, 'rev-parse', '--verify', '-q', 'HEAD', check=False)
    branch = git(path, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
    tags = git(path, 'tag', '--points-at', 'HEAD').splitlines() if sha else []
    url = git(path, 'ls-remote', '--get-url') if git(path, 'remote') else ''
    return GitInfo(url, tags[0] if tags else branch or sha, sha)


def test_not_a_repo(tmp_path):
    assert git_info(tmp_path) is None


def test_unborn_branch(tmp_path, git):
    git(tmp_path, 'init')
    assert git_info(tmp_path) == GitInfo('', 'main', '') == oracle(git, tmp_path)


def test_branch_no_remote(repo, git):
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == 'main'


def test_origin(repo, git):
    git(repo, 'remote', 'add', 'origin', 'https://example.com/a/Addon.git')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).remote == 'https://example.com/a/Addon.git'


def test_branch_tracks_other_remote(repo, git):
    git(repo, 'remote', 'add', 'origin', 'https://example.com/origin.git')
    git(repo, 'remote', 'add', 'upstream', 'https://example.com/upstream.git')
    git(repo, 'config', 'branch.main.remote', 'upstream')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).remote == 'https://example.com/upstream.git'


def test_single_non_origin_remote(repo, git):
    git(repo, 'remote', 'add', 'fork', 'https://example.com/fork.git')
    assert git_info(repo) == oracle(git, repo)


def test_detached_no_tag(repo, git):
    git(repo, 'commit', '--allow-empty', '-m', 'two')
    git(repo, 'checkout', '-q', 'HEAD~')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == info(repo).sha


@pytest.mark.parametrize('annotated', [False, True])
def test_tag_preferred_over_branch(repo, git, annotated):
    git(repo, 'tag', *(['-a', '-m', 'v'] if annotated else []), 'v1.0')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == 'v1.0'


@pytest.mark.parametrize('annotated', [False, True])
def test_detached_on_tag(repo, git, annotated):
    git(repo, 'tag', *(['-a', '-m', 'v'] if annotated else []), 'v1.0')
    git(repo, 'commit', '--allow-empty', '-m', 'two')
    git(repo, 'checkout', '-q', 'v1.0')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == 'v1.0'


def test_tag_on_other_commit_ignored(repo, git):
    git(repo, 'tag', '-a', '-m', 'v', 'v1.0')
    git(repo, 'commit', '--allow-empty', '-m', 'two')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == 'main'


def test_multiple_tags(repo, git):
    git(repo, 'tag', 'b')
    git(repo, 'tag', '-a', '-m', 'v', 'a')
    assert git_info(repo) == oracle(git, repo)


@pytest.mark.parametrize('annotated', [False, True])
def test_packed_refs(repo, git, annotated):
    git(repo, 'remote', 'add', 'origin', 'https://example.com/a.git')
    git(repo, 'tag', *(['-a', '-m', 'v'] if annotated else []), 'v1.0')
    git(repo, 'pack-refs', '--all')
    assert not (repo / '.git' / 'refs' / 'heads' / 'main').exists()
    assert git_info(repo) == oracle(git, repo)
    git(repo, 'checkout', '-q', 'v1.0')
    assert git_info(repo) == oracle(git, repo)


def test_loose_ref_overrides_packed(repo, git):
    git(repo, 'pack-refs', '--all')
    git(repo, 'commit', '--allow-empty', '-m', 'two')
    assert git_info(repo) == oracle(git, repo)


def test_moved_tag_overrides_packed_peel(repo, git):
    git(repo, 'tag', '-a', '-m', 'v', 'v1.0')
    git(repo, 'pack-refs', '--all')
    git(repo, 'commit', '--allow-empty', '-m', 'two')
    git(repo, 'tag', '-f', '-a', '-m', 'v', 'v1.0')
    assert git_info(repo) == oracle(git, repo)
    assert info(repo).ref == 'v1.0'


@pytest.mark.xfail(strict=True, reason='annotated tag object only in a pack: no pack parsing')
def test_loose_annotated_tag_with_packed_object(repo, git):
    git(repo, 'tag', '-a', '-m', 'v', 'v1.0')
    git(repo, 'repack', '-a', '-d')
    git(repo, 'prune-packed')
    assert git_info(repo) == oracle(git, repo)


def test_worktree(repo, git, tmp_path):
    git(repo, 'remote', 'add', 'origin', 'https://example.com/a.git')
    wt = tmp_path / 'wt'
    git(repo, 'worktree', 'add', '-q', '-b', 'other', str(wt))
    git(wt, 'commit', '--allow-empty', '-m', 'two')
    assert git_info(wt) == oracle(git, wt)
    assert info(wt).ref == 'other'
    assert info(wt).sha != info(repo).sha


def test_submodule(repo, git, tmp_path):
    git(repo, 'tag', 'v1.0')
    parent = tmp_path / 'parent'
    parent.mkdir()
    git(parent, 'init')
    git(parent, 'submodule', 'add', '-q', str(repo), 'sub')
    sub = parent / 'sub'
    assert (sub / '.git').is_file()
    assert git_info(sub) == oracle(git, sub)
    assert info(sub).remote == str(repo)
