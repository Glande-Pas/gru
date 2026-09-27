# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Module handling an install location

AddOns are identified by their manifest matching the directory name: {dir}/{dir}.txt or {dir}/{dir}.txt.
However, this does not always match what zip file structures contain.

"""

from __future__ import annotations

import collections
import contextlib
import tempfile
import pathlib
import zipfile
import requests
import shutil
import datetime
import email.utils
import warnings
import traceback
import configparser
import typing
import csv
from urllib.parse import quote as urllib_quote

from .config import user_cache, user_config
from .addon import InstalledAddon, AddonInfo, AddonBundle, GARBAGE, MANIFEST_EXTS, _parse_version
from .api import _fuzz, _filter, AmbiguousDirectory
from .patch import addon_patch_file, PatchError

from typing import Protocol
from collections.abc import Iterator, Iterable, Callable

if typing.TYPE_CHECKING:
    import gru.addon
    import gru.api


class ProgressProtocol(Protocol):
    """ Instance shape only -- construction is described separately by ProgressFactory below,
    since implementers include plain factory functions as well as classes. """
    def __enter__(self) -> ProgressProtocol: ...
    def __exit__(self, *args, **kwargs) -> None: ...
    def update(self, size: int, /) -> None: ...


#: Anything callable as (size, message) -> ProgressProtocol -- a ProgressProtocol subclass
#: (called as its own constructor) or a plain factory function like cli.py's _progress().
ProgressFactory = Callable[[int, str], ProgressProtocol]

#: Either a fixed answer, or a per-addon decision (e.g. cli.py prompting once per addon that
#: actually has a SavedVariables file) -- reused as-is for cascaded dependency/duplicate removals,
#: so a lib pulled in by remove(deps=True)/remove_unused_deps()/remove_duplicates() gets the same
#: policy as the addon(s) the caller removed directly.
RemoveVarsPolicy = typing.Union[bool, Callable[[InstalledAddon], bool]]


class SilentProgress:
    """ NOP class "implementing" progress as context manager """

    def __init__(self, size: int, message: str) -> None:
        """ Display progress with given total size and message """
        pass

    def __enter__(self) -> SilentProgress:
        """ Start of progress """
        return self

    def __exit__(self, *args, **kwargs) -> None:
        """ End of progress """
        pass

    def update(self, size: int) -> None:
        """ Update progress with given chunk size """
        pass


class Folder:
    def __init__(self, game: str, config: configparser.ConfigParser) -> None:
        self.game = game
        self.root: pathlib.Path = pathlib.Path(config.get(f'{game}.addons', 'root'))
        self.url_template: str = config.get(f'{game}.links', 'download')
        #: A list of addons that have local file info and are enriched as appropriate with API info
        self._installed: dict[pathlib.Path, gru.addon.InstalledAddon] = {}

    @property
    def installed(self) -> Iterable[gru.addon.InstalledAddon]:
        return self._installed.values()

    @contextlib.contextmanager
    def temp_root(self) -> Iterator[Folder]:
        """ Yields a similarly-configured install folder at a temporary location. """
        with tempfile.TemporaryDirectory() as tempdir:
            config = configparser.ConfigParser()
            config.add_section('game.addons')
            config.set('game.addons', 'root', tempdir)
            config.add_section('game.links')
            config.set('game.links', 'download', self.url_template)
            yield Folder('game', config)

    @contextlib.contextmanager
    def unmodified_addon(self, addon: gru.addon.AddonInfo, dir_: str, api: gru.api.API,
                         url: str | None = None) -> Iterator[gru.addon.InstalledAddon]:
        with self.temp_root() as temp_root:
            temp_location = temp_root.root / addon.dir
            # unpack() returns a collection (possibly several addons for a multi-dir bundle);
            # this context manager's contract is a single InstalledAddon to diff against --
            # specifically the one matching `dir_` (e.g. one bundle member among many), not
            # just whichever happens to be first in unpack()'s result.
            installed_addons = list(temp_root.unpack(addon, api, url_override=url))
            temp_addon = next((a for a in installed_addons if a.dir == dir_), installed_addons[0])
            yield temp_addon
            shutil.rmtree(temp_location)

    def scan(self, api: gru.api.API | None = None) -> None:
        links, locked = self.read_csv_hints()
        self._installed = self._scan(self.root, api, links=links, locked=locked)

    def read_csv_hints(self) -> tuple[dict[str, str], set[str]]:
        """ dir -> link, and the set of locked dirs, from the last addons.csv snapshot -- the
        folder scan itself has no way to derive either: `links` only breaks a tie when a folder
        name matches several different online addons (see API.dir()), `locked` restores the
        .locked flag InstalledAddon otherwise always starts False with. Both matched by dir, same
        as every other addons.csv column, so a dir shared by several folders gets the same
        link/lock state for all of them. """
        path = user_config(self.game, 'addons.csv')
        if not path.exists():
            return {}, set()
        with path.open(newline='') as f:
            rows = list(csv.DictReader(f))
        links = {row['dir']: row['link'] for row in rows if row.get('link')}
        locked = {row['dir'] for row in rows if row.get('locked')}
        return links, locked

    def snapshot(self) -> dict[pathlib.Path, tuple[str, str, str]]:
        """ folder -> (dir, version, link) for every currently-installed addon. Keyed by folder,
        not dir: two folders can share the same dir name (a standalone/bundled duplicate pair),
        and a dir-keyed dict would silently drop one of them. """
        return {addon.folder: (addon.dir, addon.version,
                               addon.infos.metadata['link'] if addon.infos is not None else '')
                for addon in self.installed}

    def write_csv(self, out: typing.IO, recurse: bool = False) -> int:
        """ Write dir/version/link/locked rows for installed addons to `out`. Returns the row count. """
        writer = csv.writer(out)
        writer.writerow(['dir', 'version', 'link', 'locked'])
        count = 0
        for addon in self.installed:
            if not (recurse or addon.parent is None):
                continue
            link = addon.infos.metadata['link'] if addon.infos is not None else ''
            writer.writerow([addon.dir, addon.version, link, 'locked' if addon.locked else ''])
            count += 1
        return count

    def export_state(self) -> None:
        """ Keep <config>/<game>/addons.csv in sync with the current install state. """
        with user_config(self.game, 'addons.csv').open('w', newline='') as out:
            self.write_csv(out)

    def _scan(self, root: pathlib.Path, api: gru.api.API | None = None, links: dict[str, str] | None = None,
              locked: set[str] | None = None) -> dict[pathlib.Path, gru.addon.InstalledAddon]:
        """ List the root path """
        results: dict[pathlib.Path, gru.addon.InstalledAddon] = {}
        links = links or {}
        locked = locked or set()

        # Additional housekeeping for partial parsing
        if root != self.root:
            if not root.is_relative_to(self.root):
                raise PermissionError('Can only scan within install folder')
            for parent_dir in root.parents:
                if parent_dir == self.root:
                    break
                elif parent_dir in getattr(self, '_installed', {}):
                    results[parent_dir] = self._installed[parent_dir]
                    break

        try:
            candidates = collections.deque([root] if root != self.root else self.root.iterdir())
        except PermissionError:
            warnings.warn('Skipping folder scan due to permission errors.')
            return results

        while candidates:
            path = candidates.pop()
            if not path.is_dir():
                continue

            # Recursively check for depths up to 3
            if len(path.relative_to(self.root).parts) < 3:
                try:
                    candidates.extend(path.iterdir())
                except PermissionError:
                    warnings.warn(f'Skipping folder scan {path.relative_to(self.root)} due to permission errors.')
                    pass

            parent = next((results[dir_] for dir_ in path.parents if dir_ in results), None)
            try:
                addon = InstalledAddon(path, parent)
            except FileNotFoundError:
                # No manifest, ignore directory
                continue
            except Exception as err:
                warnings.warn(f'Skipping addon at {path.relative_to(self.root)} due to {type(err).__name__} {err}')
                continue

            addon.locked = path.name in locked
            try:
                if api:
                    addon.link(api.dir(path.name, link=links.get(path.name)))
            except FileNotFoundError:
                pass  # left unmatched -- TermDisplay flags it (no listing / ambiguous)

            results[path] = addon

        if api:
            for bundle, infos, candidates in self.find_bundle_matches(results, api, links):
                if infos is not None:
                    bundle.link(infos)
                # else: no confident match (not found at all, or ambiguous) -- left for
                # gru_app.find_ambiguous_bundles()/`gru match` to resolve, same as individual
                # unmatched addons.

        return results

    def find_bundle_matches(self, results: dict[pathlib.Path, gru.addon.InstalledAddon], api: gru.api.API,
                             links: dict[str, str]
                             ) -> Iterator[tuple[gru.addon.AddonBundle, gru.addon.AddonInfo | None,
                                                 list[gru.addon.AddonInfo]]]:
        """ Addons that couldn't resolve individually online (private/library-only names, e.g.
        HarvestMapData's per-region submodules) may still share a containing directory whose
        OWN name is the bundle's real online listing -- not necessarily the immediate one (that
        example bundles them under an extra HarvestMapData/Modules/ pass-through directory), so
        walk up one level at a time, stopping short of the addons root, until the lookup
        resolves, is ambiguous, or nothing higher up. Yields one (bundle, infos, candidates) per
        candidate directory tried with >1 member: infos is set on a confident match, otherwise
        None with `candidates` holding whatever AmbiguousDirectory last raised (empty if the
        walk ran out before matching anything at all). """
        unmatched = [addon for addon in results.values() if addon.infos is None]
        seen: set[pathlib.Path] = set()
        for addon in unmatched:
            candidate_dir = addon.folder.parent
            if candidate_dir == self.root or candidate_dir in seen:
                continue
            unresolved = [a for a in unmatched if a.folder.is_relative_to(candidate_dir)]
            if len(unresolved) < 2:
                continue
            seen.add(candidate_dir)

            while True:
                try:
                    infos = api.dir(candidate_dir.name, link=links.get(candidate_dir.name))
                except AmbiguousDirectory as exc:
                    # Include every co-located addon, not just the unresolved ones -- the
                    # directly-resolving main sibling (its own dir happens to already match
                    # online) still belongs in the same group for display/version-rank purposes.
                    members = [a for a in results.values() if a.folder.is_relative_to(candidate_dir)]
                    yield AddonBundle(candidate_dir.name, members), None, exc.candidates
                    break
                except FileNotFoundError:
                    if candidate_dir.parent == self.root:
                        members = [a for a in results.values() if a.folder.is_relative_to(candidate_dir)]
                        yield AddonBundle(candidate_dir.name, members), None, []
                        break
                    candidate_dir = candidate_dir.parent
                    seen.add(candidate_dir)
                    unresolved = [a for a in unmatched if a.folder.is_relative_to(candidate_dir)]
                    continue
                else:
                    members = [a for a in results.values() if a.folder.is_relative_to(candidate_dir)]
                    yield AddonBundle(candidate_dir.name, members), infos, []
                    break

    def name(self, name: str) -> Iterator[gru.addon.InstalledAddon]:
        """ Lookup addons by name (exact match) """
        return _filter(self.installed, 'title', str(name).lower())

    def dir(self, dir_: str) -> Iterator[gru.addon.InstalledAddon]:
        """ Lookup addons by name (exact match) """
        return _filter(self.installed, 'dir', str(dir_).lower())

    def id(self, id_: int) -> Iterator[gru.addon.InstalledAddon]:
        """ Lookup addons by id (exact match) """
        return _filter(self.installed, 'id', id_)

    def find(self, val: str, api: gru.api.API) -> list[gru.addon.InstalledAddon]:
        """ Search for an installed addon generically """
        # Various methods of exact matches
        if by_name := list(self.name(val)):
            return by_name

        if by_dir := list(self.dir(val)):
            return by_dir

        # Otherwise revert to search and return a list of candidates
        if search := list(self.search(val)):
            return search

        return sum((list(self.id(addon.id)) for addon in api.search(val) if addon.id is not None), [])

    def search(self, term: str, tiebreakattr: str | None = None, maxlen: int = 30) -> list[gru.addon.InstalledAddon]:
        """ Search `term` in addon names """
        # We want at least 75% of search string in result
        tiebreak = [tiebreakattr] if tiebreakattr is not None else []
        return [
            *_fuzz(self.installed, 'title', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                   tiebreakattr=tiebreak),
            *_fuzz(self.installed, 'dir', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                   tiebreakattr=tiebreak),
        ]

    def find_installed(self, spec: gru.addon.Dependency) -> gru.addon.InstalledAddon | None:
        for folder in self.dir(spec.dir):
            if folder.dep_version >= spec.dep_version:
                return folder

    def __repr__(self) -> str:
        return f'Folder({self.root})'

    def _inspect_bundle(self, path: pathlib.Path, zf: zipfile.ZipFile, api: gru.api.API
                        ) -> tuple[pathlib.Path, list[pathlib.Path], list[tuple[pathlib.Path, bool, int]]]:
        """ This is the annoying bit where we need to handle non-standard zip bundles

        General logic:
        - Standard addon: install in addon dir
        - “naked” addon (i.e. addon contents without directory): wrap in top-level dir
        - non-addon folders included: wrap in top-level dir
        - several addons bundled together at top level: install non-standalone addons to avoid clashes

        Returns a tuple of:
        - a destination directory for zip contents,
        - a lits of directories to remove
        - a list of file infos from the zip, such that their extracted path ends up in addon.root
        """
        # NB: always ignore macos garbage
        files = [(pathlib.Path(info.filename), info.is_dir(), info.file_size)
                 for info in zf.infolist() if not info.filename.startswith(tuple(GARBAGE))]
        # Ignore files that would end up outside target directory
        path = path.resolve()
        files = [(fn, is_dir, sz) for fn, is_dir, sz in files if (path / fn).resolve(strict=False).is_relative_to(path)]
        # Extract specific files we’re interested in
        manifests = {fn.with_suffix('') for fn, *_ in files
                     if (fn.stem == fn.parent.name or len(fn.parts) == 1) and fn.suffix in MANIFEST_EXTS}
        toplevels = collections.Counter(fn.parts[0] for fn, *_ in files)

        # Try to find a single manifest at expected location with expected name: standard case
        expected_manifest = pathlib.Path(path.name, path.name) in manifests
        if expected_manifest and len(toplevels) == 1:
            return path.parent, [path], files

        # Try to find a single manifest at expected location with any name: really should not happen
        manifest_depth1 = [fn for fn in manifests if len(fn.parts) == 2]
        if len(manifest_depth1) == 1 and len(toplevels) == 1:
            warnings.warn(f'Using {manifest_depth1[0].name} as install dir instead of {path.name}')
            path = path.parent / manifest_depth1[0].name
            return path.parent, [path], files

        # Try to find a single top-level manifest with expected name: missing top-level directory
        if pathlib.Path(path.name) in manifests:
            warnings.warn(f'Addon bundle missing top-level dir, prepending {path.name}/ to zip contents')
            return path, [path], files

        # Try to find a single top-level manifest with any name
        manifest_depth0 = [fn for fn in manifests if len(fn.parts) == 1]
        # If needed, try to reduce top-level manifest candidates to files whose stem appears in zip’s name
        # i.e. {addon}.txt in {addon.zip}, {addon}-{version}.zip, {addon}r{release}.zip, etc.
        if len(manifest_depth0) > 1 and zf.filename is not None:
            manifest_depth0 = [fn for fn in manifest_depth0 if zf.filename.startswith(fn.name)]

        if len(manifest_depth0) == 1:
            warnings.warn('Addon bundle missing top-level dir and unexpected manifest name, '
                          f'prepending {manifest_depth0[0].name}/ to zip contents, instead of {path.name}')
            path = path.parent / manifest_depth0[0].name
            return path, [path], files

        # Try to find any manifest? Do not take into account non-single top-level .txt files
        manifest_depth1plus = [fn for fn in manifests if len(fn.parts) > 1]

        if not manifest_depth1plus:
            raise ValueError(f'No addon manifest in bundle {zf.filename}')

        # No identified manifest, we have to guess what we’re really installing
        if len(toplevels) == 1:
            top_dir = next(iter(toplevels))

            # A wrapper dir with no manifest of its own may still bundle several addons, each
            # nested at whatever depth its own manifest sits at. Keep the nesting but prune
            # any that's independently installable.
            nested_dirs = {fn.parent for fn in manifest_depth1plus}
            nested = {d.name for d in nested_dirs}
            if len(nested) > 1:
                try:
                    main_id = api.dir(top_dir).id
                except FileNotFoundError:
                    main_id = top_dir
                pruned: set[str] = set()
                for dir_ in nested:
                    try:
                        dep = api.dir(dir_)
                    except FileNotFoundError:
                        pass  # not a standalone addon, keep it
                    else:
                        if dep.id != main_id:
                            pruned.add(dir_)
                nested -= pruned
                warnings.warn(f'Installing {len(nested)} addons nested under {top_dir}/: {", ".join(sorted(nested))}')
                # Only drop files under a pruned addon dir -- loose files (LICENSE, README) stay.
                pruned_dirs = {d for d in nested_dirs if d.name in pruned}
                files = [(fn, is_dir, sz) for fn, is_dir, sz in files
                        if not any(fn == d or fn.is_relative_to(d) for d in pruned_dirs)]
                return path.parent, [path], files

            # In 1-dir case does not really matter
            if top_dir != path.name:
                warnings.warn(f'Using {top_dir} as addon dir, installing under {top_dir}')
                path = path.parent / top_dir
            return path.parent, [path], files

        # From here on we handle several top-level directories, i.e. risk of clashing
        # as some secondary top-levels might be owned by other addons

        def find_siblings(tops: Iterable[str]) -> tuple[pathlib.Path | None, list[pathlib.Path] | None]:
            for parent in path.parents:
                if parent == self.root or not parent.is_relative_to(self.root):
                    break
                siblings = [parent / top for top in tops]
                if all(dir_.exists() for dir_ in siblings):
                    return parent, siblings
            return None, None

        # Try to match zip contents to where addon is already installed
        # E.g. we are updating Foo at {root}/dir/Foo/Foo.txt, zip contains (Foo/Foo.txt, Bar/): install at {root}/dir
        parent, siblings = find_siblings(toplevels)
        if parent is not None and siblings is not None:
            warnings.warn(f'Found local install matching non-standard zip at {parent}, installing under {parent}')
            return parent, siblings, files

        # Some addons bundle gamedata, EsoUI (etc) as top-level folders, install to a subdirectory
        if not all(pathlib.Path(name, name) in manifest_depth1 for name in toplevels):
            warnings.warn(f'Multiple-directory addon, prepending {path.name}/ to zip contents')
            return path, [path], files

        # So now we know we have several top-level *addons*
        # install all directories that resolve:
        # - to this addon (bundle’s main addon), or
        # - to no other addon (not available standalone)
        try:
            main_id = api.dir(path.name).id
        except FileNotFoundError:
            main_id = path.name

        for dir_ in list(toplevels):
            try:
                dep = api.dir(dir_)
            except FileNotFoundError:
                pass  # not a standalone addon, keep it
            else:
                if dep.id != main_id:
                    del toplevels[dir_]

        # After pruning, check again whether the remaining dirs match an existing install
        parent, siblings = find_siblings(toplevels)
        if parent is not None and siblings is not None:
            warnings.warn(f'Found local install matching pruned non-standard zip at {parent}, '
                          f'installing under {parent}')
            return parent, siblings, [(fn, is_dir, sz) for fn, is_dir, sz in files if fn.parts[0] in toplevels]

        if len(toplevels) > 1:
            warnings.warn(f'Installing {len(toplevels)} addons as part of {path.name}: {", ".join(toplevels)}')
        return (path.parent, [path.parent / top for top in toplevels],
                [(fn, is_dir, sz) for fn, is_dir, sz in files if fn.parts[0] in toplevels])

    def _download(self, dl: requests.Response, fd: typing.BinaryIO, progress: ProgressProtocol) -> None:
        with progress as prog:
            for chunk in dl.iter_content(chunk_size=1024):
                fd.write(chunk)
                prog.update(len(chunk))

    @staticmethod
    def _suggested_filename(headers: dict, default: str) -> str:
        """ Extract the server-suggested filename from a Content-Disposition header, if any """
        for tok in map(str.strip, headers.get('content-disposition', '').split(';')):
            if tok.startswith('filename='):
                return tok[10:].strip('"')
        return default

    @staticmethod
    def _cache_is_fresh(headers: dict, zippath: pathlib.Path) -> bool:
        """ Whether the cached zip at `zippath` is still up to date per HEAD response `headers` """
        if not zippath.exists():
            return False
        changed = headers.get('last-modified')
        if not changed:
            return False
        changed = email.utils.parsedate_to_datetime(changed).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        size = int(headers.get('content-length', 0))
        stat = zippath.stat()
        # NB. this is correct on *nix and NTFS, but not FAT which uses local timezone
        # Hopefully FAT is not used too much anymore? Otherwise we need a config() function to handle this
        freshness = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc).replace(tzinfo=None)
        return size == stat.st_size and changed < freshness

    def _unzip(self, zf: zipfile.ZipFile, files: list[tuple[pathlib.Path, bool, int]], dest: pathlib.Path,
               progress: ProgressProtocol) -> None:
        dest = dest.resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with progress as prog:
            for file, is_dir, size in files:
                file_dest = (dest / file).resolve()
                if not file_dest.is_relative_to(dest):
                    continue

                if is_dir:
                    file_dest.mkdir(exist_ok=True, parents=True)
                    continue

                file_dest.parent.mkdir(exist_ok=True, parents=True)
                try:
                    with zf.open(file.as_posix(), 'r') as zfreader, open(file_dest, 'wb') as out:
                        shutil.copyfileobj(zfreader, out)
                except KeyError as exc:
                    # Shouldn't happen, but must not crash whatever command is unzipping.
                    warnings.warn(f'Skipping {file} -- not found in archive: {exc}')
                    continue
                prog.update(size)

    def unpack(self, addon: gru.addon.AddonInfo, api: gru.api.API, progress: ProgressFactory | None = None,
               path: pathlib.Path | None = None, url_override: str | None = None) -> Iterable[gru.addon.InstalledAddon]:
        """ Download and install, calls back to `progress` (100% until return means unzipping) """
        # NB. any file name returns correct file eventually, and “correct” file names are underterministic.
        # However, server-side caching means we can get stale versions if we use a version-independent url.
        # Do not use a random string, so we don’t defeat the purpose of server-side caching.
        progress = progress or SilentProgress
        fname = f'{addon.dir}-{addon.version}.zip'
        url = url_override or self.url_template.format(id=addon.id) + urllib_quote(fname)

        with requests.head(url, allow_redirects=True) as check:
            check.raise_for_status()
            headers = {key.lower(): value for key, value in check.headers.items()}

        fname = self._suggested_filename(headers, fname)
        zippath = user_cache('dl', fname)

        # Download
        if self._cache_is_fresh(headers, zippath):
            print(f'Using cache {fname}')
        else:
            size = int(headers.get('content-length', 0))
            with requests.get(url, stream=True, allow_redirects=True) as dl:
                with open(zippath, 'wb') as fd:
                    self._download(dl, fd, progress(size, f'Downloading {fname}...'))

        # Default install dir -- requires a temporary addon object that’s not the (read-only) API one,
        # and can’t be the one initialized with metadata from the manifest
        install_folder = path or self.root / addon.dir

        # Inspect, extract
        with zipfile.ZipFile(zippath) as zf:
            dest, erase_dirs, extract = self._inspect_bundle(install_folder, zf, api)

            for erased in erase_dirs:
                if not erased.exists():
                    continue
                shutil.rmtree(erased)

            extract_size = sum(sz for fn, dr, sz in extract)
            self._unzip(zf, extract, dest, progress(extract_size, f'Extracting  {fname}...'))

        # Update our list of installed addons
        self._installed = {path: inst for path, inst in self._installed.items()
                           if not any(path.is_relative_to(erased) for erased in erase_dirs)}
        try:
            installed_addons = {install_folder: InstalledAddon(install_folder)}  # TODO: nesting?
        except (FileNotFoundError, AssertionError):
            # Missing or malformed manifest -- defer to _scan(), which warns-and-skips instead of raising.
            installed_addons = self._scan(install_folder, api)

        for inst in installed_addons.values():
            inst.link(addon)
        self._installed.update(installed_addons)
        return installed_addons.values()

    def install(self, addon: gru.addon.AddonInfo, api: gru.api.API, progress: ProgressFactory | None = None,
                path: pathlib.Path | None = None, deps: bool = True, opt: bool = False,
                url_override: str | None = None) -> int | None:
        installed = self.unpack(addon, api, path=path, progress=progress, url_override=url_override)

        if deps:
            return self.install_deps(installed, api, progress=progress, opt=opt)

    def _dedup_deps(self, deps: Iterable[gru.addon.Dependency]) -> list[gru.addon.Dependency]:
        dedup = {}
        for dep in deps:
            dedup.setdefault(dep.dir, []).append(dep)
        return [max(dep_versions, key=lambda dep: dep.dep_version) for dep_versions in dedup.values()]

    def missing_deps(self, pool: Iterable[gru.addon.InstalledAddon], opt: bool = False) -> list[gru.addon.Dependency]:
        """ Return dependencies that are missing from `installed` """
        deps = []
        for addon in pool:
            deps.extend(addon.deps)
            if opt:
                deps.extend(addon.optdeps)
        return [dep for dep in self._dedup_deps(deps) if self.find_installed(dep) is None]

    def depcount(self, lib: gru.addon.InstalledAddon, opt: bool = True) -> int:
        """ Count the number of times this addon is depended on """
        refcount = sum(lib.dir == dep.dir for addon in self.installed for dep in addon.deps)
        if opt:
            refcount += sum(lib.dir == dep.dir for addon in self.installed for dep in addon.optdeps)
        return refcount

    def unused_deps(self, pool: Iterable[gru.addon.InstalledAddon],
                    opt: bool = False) -> list[gru.addon.InstalledAddon]:
        unused = []
        for addon in pool:
            if addon.is_lib and self.depcount(addon, opt=opt) == 0:
                unused.append(addon)
        return unused

    def update(self, api: gru.api.API, progress: ProgressFactory | None = None, opt: bool = False, deps: bool = False,
               patch: bool = False) -> tuple[int, int]:
        updates = []
        for addon in list(self.installed):  # snapshot: a bundled update adds a new key below
            if not addon.can_update or addon.infos is None or addon.locked:
                continue
            if addon.parent is not None and addon.is_superseded:
                continue  # a newer copy already exists elsewhere; nothing to do here
            # A flat bundle's main entry also needs path=None: addon.folder is its own nested
            # dir, not the wrapper, by an amount that varies with nesting depth.
            heads_group = any(other.parent is addon for other in self.installed)
            path = addon.folder if addon.parent is None and not heads_group else None
            try:
                updates.extend(self.unpack(addon.infos, api, progress=progress, path=path))
            except Exception as err:
                warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}\n'
                              f'{"".join(traceback.format_exc())}')

        for addon in updates:
            if patch and (patch_file := user_config(self.game, f'{addon.dir}.patch')).exists():
                self._reapply_patch(addon, patch_file)

        if deps:
            return (len(updates), self.install_deps(updates, api, progress=progress, opt=opt, patch=patch))
        else:
            return (len(updates), 0)

    def install_deps(self, pool: Iterable[gru.addon.InstalledAddon], api: gru.api.API,
                     progress: ProgressFactory | None = None, opt: bool = False, patch: bool = False) -> int:
        added = 0
        deps = [*(pool or self.installed)]
        while newdeps := self.missing_deps(deps, opt=opt):
            deps = []
            for dep in newdeps:
                # Do not check if installed as it’s a missing dep
                try:
                    addon = api.dir(dep.dir)
                except (ValueError, FileNotFoundError):
                    warnings.warn(f'Failed to look up addon dependence {dep.dir!r}')
                    continue
                try:
                    addons = self.unpack(addon, api, progress=progress)
                except Exception as err:
                    warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}\n'
                                  f'{"".join(traceback.format_exc())}')
                    continue
                for addon in addons:
                    if patch and (patch_file := user_config(self.game, f'{addon.dir}.patch')).exists():
                        self._reapply_patch(addon, patch_file)
                added += 1
                deps.extend(addons)

        return added

    def _reapply_patch(self, addon: gru.addon.InstalledAddon, patch_file: pathlib.Path) -> None:
        """ Always normal (non-partial) mode: a silently half-patched file during an unattended
        update is worse than skipping it -- --partial stays a deliberate `gru patch` action. """
        try:
            result = addon_patch_file(addon, patch_file)
        except PatchError as err:
            warnings.warn(f'Saved patch for {addon.dir!r} is invalid, skipped: {err}')
            return
        if result.backed_out:
            warnings.warn(f'Saved patch for {addon.dir!r} failed to reapply cleanly; run '
                          f'`gru patch {addon.dir} --partial` to apply what you can and fix the rest.')

    def saved_variable_files(self, addon: gru.addon.InstalledAddon) -> list[pathlib.Path]:
        """ This addon's SavedVariables file, if it declares any (## SavedVariables: Name ...) and
        it actually exists on disk -- named after the addon itself (not the declared variable
        name(s), which are Lua globals inside that one file), at
        <AddOns root>/../SavedVariables/{addon.dir}.lua. """
        if not addon.metadata.get('savedvariables', '').strip():
            return []
        path = self.root.parent / 'SavedVariables' / f'{addon.dir}.lua'
        return [path] if path.exists() else []

    def _bundled_children(self, addon: gru.addon.InstalledAddon) -> list[gru.addon.InstalledAddon]:
        """ Installed addons nested inside `addon`'s own folder -- about to be swept away by its
        removal (a single shutil.rmtree of the parent), whether or not they're separately
        matched online (most bundled dependencies aren't). """
        return [other for other in self.installed
                if other is not addon and other.folder.is_relative_to(addon.folder)]

    def remove(self, addon: gru.addon.InstalledAddon | gru.addon.AddonInfo, deps: bool = False,
               opt: bool = True, remove_vars: RemoveVarsPolicy = False) -> int:
        """ Uninstall addon. `remove_vars` also applies to any dependency this cascades into
        removing (deps=True) and to any bundled addon nested inside it, swept away by the same
        rmtree: a fixed bool answers for all of them, a callable is asked again for each -- e.g.
        cli.py prompting once per addon that actually has SavedVariables. """
        if isinstance(addon, AddonInfo):
            if not addon.folders:
                raise ValueError(f'Addon {addon.title} is not installed')
            addon = list(addon.folders.values())[0]

        bundled = self._bundled_children(addon)
        for target in (addon, *bundled):
            if remove_vars(target) if callable(remove_vars) else remove_vars:
                for path in self.saved_variable_files(target):
                    path.unlink()

        if addon.folder.exists():
            shutil.rmtree(addon.folder)
        for gone in (addon, *bundled):
            self._installed.pop(gone.folder, None)
            if gone.id and gone.infos is not None:
                gone.infos.deregister(gone)

        if not deps:
            return 0

        return self.remove_unused_deps(opt=opt, remove_vars=remove_vars)

    def remove_unused_deps(self, opt: bool = True, remove_vars: RemoveVarsPolicy = False) -> int:
        removed = 0
        while unused := self.unused_deps(self.installed, opt=opt):
            for dep in unused:
                self.remove(dep, remove_vars=remove_vars)
            removed += len(unused)

        return removed

    def duplicate_standalones(self, pool: Iterable[gru.addon.InstalledAddon]
                              ) -> list[tuple[gru.addon.InstalledAddon, gru.addon.InstalledAddon]]:
        """ (standalone, bundled) pairs where a standalone library install is made redundant by
        an equal-or-newer bundled copy of the same online addon -- ESO always loads the highest
        version it finds regardless of bundling, so these top-level copies serve no purpose.
        Bundled copies are never included as the redundant side: only get/remove of their
        parent addon should touch them. """
        redundant = []
        for addon in pool:
            if not addon.is_lib or addon.parent is not None or addon.infos is None:
                continue
            this = _parse_version(addon.version)
            if this is None:
                continue
            for other in addon.infos.folders.values():
                if other.parent is None:
                    continue
                other_version = _parse_version(other.version)
                if other_version is not None and other_version >= this:
                    redundant.append((addon, other))
                    break
        return redundant

    def remove_duplicates(self, remove_vars: RemoveVarsPolicy = False
                          ) -> list[tuple[gru.addon.InstalledAddon, gru.addon.InstalledAddon]]:
        pairs = self.duplicate_standalones(self.installed)
        for addon, _ in pairs:
            self.remove(addon, remove_vars=remove_vars)
        return pairs
