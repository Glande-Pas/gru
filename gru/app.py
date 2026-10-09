# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Application layer: state persistence and orchestration that spans gru.api/gru.install/config
but is independent of any particular front-end -- a CLI or a future GUI both hook in here.
gru.cli (or a GUI module in its place) is expected to stay a thin layer of command dispatch,
prompting, and output formatting on top of this. """

from __future__ import annotations

import collections
import configparser
import csv
import datetime
import difflib
import logging
import pathlib
import requests
import typing
import warnings
from typing import NamedTuple, TypeVar
from collections.abc import Iterable
from urllib.parse import quote as urllib_quote

from .api import API, AmbiguousDirectory, PreviousVersion
from .cache import prune_downloads
from .addon import AddonInfo, InstalledAddon, AddonBundle, DisplayAddonProtocol, GARBAGE, strip_eso_text, file_crc32
from .config import root_key, user_config
from .install import Folder
from .remotezip import fetch_remote_zip_directory

logger = logging.getLogger(__name__)

NOT_INSTALLED = 'none'  # sentinel: version/NOT_INSTALLED means installed, NOT_INSTALLED/version means uninstalled

ABOUT = """\
Gru gets, removes, and updates Elder Scrolls Online (ESO) add-ons from ESOUI.com.

Gru is an independent, unofficial tool. It is not affiliated with, endorsed by, or sponsored by \
ZeniMax Online Studios, Bethesda Softworks, ESOUI, or Minion. \
The Elder Scrolls Online and ESOUI are trademarks of their respective owners.

Add-on hosting, organization, and moderation are handled entirely by ESOUI.com.
"""


class ChangeEntry(NamedTuple):
    """ One changes.csv row: `dir` went from `previous_state` to `version`. `version`/
    `previous_state` may be NOT_INSTALLED (a fresh install / a removal, respectively). """
    dir: str
    version: str
    link: str
    date: str
    previous_state: str


def addons_root_configured(config: configparser.ConfigParser, game: str, target: str = 'live') -> bool:
    """ Whether `config` already points at an existing addons directory. """
    root = config.get(f'{game}.addons', root_key(target))
    return bool(root) and pathlib.Path(root).exists()


def build_app(game: str, config: configparser.ConfigParser, target: str = 'live') -> tuple[API, Folder]:
    """ Build a live API + freshly-scanned Folder from an already-valid `config` (see
    addons_root_configured()) -- the non-interactive core a front-end calls once it has a
    usable config; resolving/prompting for a missing addons root is that front-end's job.

    Deliberately doesn't call resolve_exact_matches(): that's for whichever specific command
    needs addons resolved (`update`, `match`), not every command that happens to scan. """
    api = API.live(config)
    local = Folder(game, config, target)
    local.scan(api)
    try:
        prune_downloads(local.installed)
    except OSError as exc:
        logger.debug('could not prune downloads: %s', exc)
    api.prune_cache()
    return api, local


def append_change_log(path: pathlib.Path, row: list[str], max_lines: int) -> None:
    rows: collections.deque[list[str]] = collections.deque(maxlen=max(max_lines, 0))
    if path.exists():
        with path.open(newline='') as f:
            reader = csv.reader(f)
            next(reader, None)
            rows.extend(reader)
    rows.append(row)
    with path.open('w', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['dir', 'version', 'link', 'date', 'previous_state'])
        writer.writerows(rows)


def read_changes(local: Folder, limit: int | None = None) -> list[ChangeEntry]:
    """ Read changes.csv, oldest first (matching on-disk order) -- or just the last `limit` rows.
    Returns [] if nothing has been logged yet (no command has changed the install state). """
    path = user_config(*local.meta, 'changes.csv')
    if not path.exists():
        return []
    with path.open(newline='') as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [ChangeEntry(*row) for row in reader]
    return rows[-limit:] if limit is not None else rows


def log_changes(local: Folder, config: configparser.ConfigParser,
                before: dict[pathlib.Path, tuple[str, str, str]]) -> None:
    """ Append one changes.csv row per folder whose version differs between `before` and now. """
    after = local.snapshot()
    max_lines = config.getint(f'{local.game}.addons', 'log_lines')
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    path = user_config(*local.meta, 'changes.csv')
    for folder in before.keys() | after.keys():
        old_dir, old_version, old_link = before.get(folder, ('', NOT_INSTALLED, ''))
        new_dir, new_version, new_link = after.get(folder, ('', NOT_INSTALLED, ''))
        if old_version == new_version:
            continue
        append_change_log(path, [new_dir or old_dir, new_version, new_link or old_link, now, old_version], max_lines)


def find_ambiguous(local: Folder, api: API) -> list[tuple[InstalledAddon, list[AddonInfo]]]:
    """ Installed addons left unmatched specifically because their dir is ambiguous online (not
    just missing), paired with their candidate listings -- for `gru match` to resolve. """
    found = []
    for addon in local.installed:
        if addon.infos is not None:
            continue
        try:
            api.dir(addon.dir)
        except AmbiguousDirectory as exc:
            found.append((addon, exc.candidates))
        except FileNotFoundError:
            pass
    return found


_T = TypeVar('_T')


def _manifest_anchor(paths: Iterable[tuple[str, ...]], manifest: str) -> tuple[str, ...]:
    """ Lowercased zip path of the directory holding `manifest`, e.g. 'Srendarr/Srendarr.txt': normally at the
    zip root, failing that the shallowest one below wrapper dirs -- e.g. `Srendarr/Srendarr/` in a zip shipping
    `Srendarr/Srendarr/Srendarr.txt`, which _inspect_bundle() installs without its wrapper dir. """
    target = tuple(part.lower() for part in pathlib.PurePosixPath(manifest).parts)
    anchors = [parts[:-1] for parts in (tuple(part.lower() for part in path) for path in paths)
               if parts[-len(target):] == target]
    if not anchors or target[:-1] in anchors:
        return target[:-1]
    return min(anchors, key=len)


def _normalize_remote_entries(entries: Iterable[tuple[str, _T]], manifest: str) -> dict[str, _T]:
    """ (raw zip path, value) pairs -> {path relative to the addon dir: value}, matching what
    InstalledAddon.files yields. The addon dir is the one holding `manifest`, the addon's dir and manifest file
    name (e.g. 'Srendarr/Srendarr.txt'), see _manifest_anchor(). Drops entries outside it (sibling bundled
    addons, wrapper dirs), directory markers, and GARBAGE/dotfiles. """
    files = [(pathlib.PurePosixPath(raw_path).parts, value)
             for raw_path, value in entries if not raw_path.endswith('/')]
    anchor = _manifest_anchor((parts for parts, _ in files), manifest)
    result: dict[str, _T] = {}
    for parts, value in files:
        if tuple(part.lower() for part in parts[:len(anchor)]) != anchor:
            continue
        rel = parts[len(anchor):]
        if not rel or any(part.startswith('.') or part in GARBAGE for part in rel):
            continue
        result['/'.join(rel).lower()] = value
    return result


class VersionMatch(NamedTuple):
    """ `archived` is set only when installed.version matched a specific archived release rather
    than the current one -- that's the release whose zip _crc_match() needs to fetch. """
    matched: bool
    archived: PreviousVersion | None = None


def _version_matches(installed: DisplayAddonProtocol, candidate: AddonInfo, api: API) -> VersionMatch:
    """ Whether installed.version matches candidate's current or an archived release. A request
    failure while checking archived versions degrades to no match; other errors propagate. """
    if installed.version == candidate.version:
        logger.debug('  version: %r == current %r -- match', installed.version, candidate.version)
        return VersionMatch(True)
    previous_versions = getattr(api, 'previous_versions', None)
    if previous_versions is None or candidate.id is None:
        logger.debug('  version: %r != current %r, no previous_versions() available -- no match',
                     installed.version, candidate.version)
        return VersionMatch(False)
    try:
        versions = previous_versions(candidate.id)
    except requests.RequestException as exc:
        logger.debug('  version: previous_versions(%r) failed (%s) -- no match', candidate.id, exc)
        return VersionMatch(False)
    archived = next((v for v in versions if v.version == installed.version), None)
    logger.debug('  version: %r != current %r, %s among %d archived version(s) -- %s',
                 installed.version, candidate.version, 'found' if archived else 'not found',
                 len(versions), 'match' if archived else 'no match')
    return VersionMatch(archived is not None, archived)


# Minimum _meta_score() to attempt a CRC check: one strong signal, not just a fuzzy title guess.
META_SCORE_THRESHOLD = 1.0


def _normalize(text: str) -> str:
    """ Lowercased, whitespace-stripped, ESO color-markup-free text, for comparison only. """
    return strip_eso_text(text).strip().lower()


def _meta_score(installed: InstalledAddon, candidate: AddonInfo, api: API) -> tuple[float, VersionMatch]:
    """ Metadata score plus the underlying VersionMatch, so a caller that CRC-checks this
    candidate knows whether to fetch a specific archived release. """
    logger.debug(' meta score: %r vs candidate %r (id=%s)', installed.dir, candidate.title, candidate.id)
    title_ratio = difflib.SequenceMatcher(None, _normalize(installed.title), _normalize(candidate.title)).ratio()
    logger.debug('  title: %r vs %r -- ratio %.2f', installed.title, candidate.title, title_ratio)
    author_match = _normalize(installed.author) == _normalize(candidate.author)
    logger.debug('  author: %r vs %r -- %s', installed.author, candidate.author,
                 'match' if author_match else 'no match')
    version_match = _version_matches(installed, candidate, api)
    score = title_ratio + author_match + version_match.matched
    logger.debug(' meta score total: %.2f', score)
    return score, version_match


def _crc_match(installed: InstalledAddon, candidate: AddonInfo, url_template: str,
               archived: PreviousVersion | None, session: requests.Session | None = None) -> bool | None:
    """ Whether candidate's zip content is byte-for-byte identical to installed's files: file set
    and CRC32s both come from the zip's central directory (gru.remotezip), fetched via HTTP Range
    requests without downloading the archive.

    `archived`, if given, fetches that specific release's zip (via the CDN's `aid=` parameter)
    instead of the current one. `session`, if given (see API.zip_session), caches HEAD/Range
    responses; otherwise falls back to the plain, uncached `requests` module.

    Returns None if the check couldn't be performed (request failure, missing content-length
    header, or a malformed zip), distinct from False (performed, didn't match). Any other
    exception propagates rather than being treated as just another unverifiable candidate. """
    if candidate.id is None:
        return None

    if archived is not None:
        url = url_template.format(id=candidate.id) + f'&aid={archived.aid}'
        logger.debug(' crc check: %r matched archived version %r -- fetching %r',
                     candidate.title, archived.version, url)
    else:
        fname = f'{candidate.dir}-{candidate.version}.zip'
        url = url_template.format(id=candidate.id) + urllib_quote(fname)

    sess: typing.Any = session or requests
    try:
        with sess.head(url, allow_redirects=True) as head:
            head.raise_for_status()
            content_size = int(head.headers['content-length'])
        remote_entries = fetch_remote_zip_directory(url, content_size, session=session)
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.debug(' crc check: could not fetch %r (%s) -- skipping', url, exc)
        return None

    remote_files = _normalize_remote_entries(((e.filename, e.crc32) for e in remote_entries),
                                             f'{installed.dir}/{installed.manifest.name}')
    local_files = {path.as_posix().lower(): file_crc32(installed.folder / path) for path in installed.files}

    matched = local_files == remote_files
    logger.debug(' crc check: %d local / %d remote file(s) -- %s',
                 len(local_files), len(remote_files), 'all match' if matched else 'mismatch')
    return matched


def find_exact_match(installed: InstalledAddon, candidates: list[AddonInfo], api: API,
                     url_template: str | None = None) -> AddonInfo | None:
    """ Resolves a candidate only via CRC32 verification (see _crc_match()) -- metadata alone is
    too weak to auto-resolve on (that's what `gru match`'s human-confirmed prompt is for).

    Metadata is still used as a cheap pre-filter: a candidate scoring below META_SCORE_THRESHOLD
    is skipped before spending a CRC check (a real network round trip) on it. Without
    `url_template`, content can't be checked at all, so nothing here ever resolves. """
    logger.debug('find_exact_match: %r against %d candidate(s)', installed.dir, len(candidates))
    if url_template is None:
        logger.debug(" no url_template given -- content can't be verified, giving up")
        return None
    if not installed.files:
        logger.debug(' local install has no files -- giving up')
        return None

    session = getattr(api, 'zip_session', None)
    exact = []
    for candidate in candidates:
        if candidate.id is None:
            continue

        score, version_match = _meta_score(installed, candidate, api)
        if score < META_SCORE_THRESHOLD:
            logger.debug(' candidate %r (id=%s): meta score %.2f < threshold %.2f -- skipping CRC check',
                         candidate.title, candidate.id, score, META_SCORE_THRESHOLD)
            continue

        if _crc_match(installed, candidate, url_template, version_match.archived, session) is True:
            logger.debug(' candidate %r (id=%s): CRC32-verified content match -- qualifies',
                         candidate.title, candidate.id)
            exact.append(candidate)
        else:
            logger.debug(' candidate %r (id=%s): content is not CRC32-verified -- rejected',
                         candidate.title, candidate.id)

    if len(exact) == 1:
        logger.debug('find_exact_match: %r resolved as %r (id=%s)', installed.dir, exact[0].title, exact[0].id)
        return exact[0]
    logger.debug('find_exact_match: %r -- %d qualifying candidate(s), not resolved', installed.dir, len(exact))
    return None


def resolve_exact_matches(local: Folder, api: API) -> list[InstalledAddon]:
    """ Auto-link every ambiguous local install with exactly one CRC-verified match. Returns the
    addons resolved this way; each is also reported via warnings.warn(). """
    ambiguous = find_ambiguous(local, api)
    logger.debug('resolve_exact_matches: %d ambiguous addon(s) to check', len(ambiguous))
    resolved = []
    for installed, candidates in ambiguous:
        exact = find_exact_match(installed, candidates, api, url_template=local.url_template)
        if exact is None:
            continue
        installed.link(exact)
        resolved.append(installed)
        warnings.warn(f'Resolved ambiguous addon {installed.dir!r} as {exact.title!r} (exact file match)')
    return resolved


def find_ambiguous_bundles(local: Folder, api: API) -> list[tuple[AddonBundle, list[AddonInfo]]]:
    """ Groups of individually-unmatched sibling addons (private/library-only names, e.g.
    HarvestMapData's per-region submodules) whose containing directory is ambiguous online --
    for `gru match` to resolve, same as find_ambiguous() does for a single addon. """
    links, _ = local.read_csv_hints()
    results = {addon.folder: addon for addon in local.installed}
    return [(bundle, candidates) for bundle, infos, candidates
            in local.find_bundle_matches(results, api, links) if infos is None and candidates]


def _bundle_dirs_match(bundle: AddonBundle, candidate: AddonInfo, url_template: str,
                       session: requests.Session | None = None) -> bool | None:
    """ Whether candidate's zip actually contains a directory for every member of `bundle` --
    confirms which listing an ambiguous bundle group really came from. Lighter than
    _crc_match(): it only needs to place each member somewhere in the archive, not verify its
    content byte-for-byte, since library-only submodules typically aren't separately listed to
    CRC-check against in the first place.

    Returns None if the check couldn't be performed, distinct from False (checked, some member
    missing). """
    if candidate.id is None:
        return None
    fname = f'{candidate.dir}-{candidate.version}.zip'
    url = url_template.format(id=candidate.id) + urllib_quote(fname)
    sess: typing.Any = session or requests
    try:
        with sess.head(url, allow_redirects=True) as head:
            head.raise_for_status()
            content_size = int(head.headers['content-length'])
        remote_entries = fetch_remote_zip_directory(url, content_size, session=session)
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.debug(' bundle dir check: could not fetch %r (%s) -- skipping', url, exc)
        return None

    remote_parts = {part.lower() for entry in remote_entries
                    for part in pathlib.PurePosixPath(entry.filename).parts}
    return all(member.dir.lower() in remote_parts for member in bundle.members)


def resolve_ambiguous_bundles(local: Folder, api: API) -> list[AddonBundle]:
    """ Auto-link every ambiguous bundle group with exactly one candidate whose zip contains all
    of its members. Returns the bundles resolved this way; each is also reported via
    warnings.warn(). """
    ambiguous = find_ambiguous_bundles(local, api)
    resolved = []
    for bundle, candidates in ambiguous:
        confirmed = [c for c in candidates if _bundle_dirs_match(bundle, c, local.url_template) is True]
        if len(confirmed) != 1:
            continue
        bundle.link(confirmed[0])
        resolved.append(bundle)
        warnings.warn(f'Resolved ambiguous bundle {bundle.dir!r} as {confirmed[0].title!r} (member dir match)')
    return resolved


def rank_candidates(installed: InstalledAddon, candidates: list[AddonInfo], api: API,
                    sortkey: str) -> list[AddonInfo]:
    """ Best-guess-first: metadata match (author, version -- current or archived --, title
    similarity), falling back to `sortkey` (e.g. downloads) for ties. """
    logger.debug('rank_candidates: %r against %d candidate(s), sortkey=%r', installed.dir, len(candidates), sortkey)

    def score(candidate: AddonInfo) -> tuple[float, float]:
        meta, _version_match = _meta_score(installed, candidate, api)
        result = (meta, candidate.metadata.get(sortkey) or 0)
        logger.debug(' candidate %r (id=%s): score = %s', candidate.title, candidate.id, result)
        return result

    ranked = sorted(candidates, key=score, reverse=True)
    logger.debug('rank_candidates: ranked order = %s', [c.title for c in ranked])
    return ranked
