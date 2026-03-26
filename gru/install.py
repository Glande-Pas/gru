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
import functools
import warnings
import re
from urllib.parse import quote as urllib_quote

from .config import encoding_open, user_cache
from .addon import InstalledAddon, Dependency, GARBAGE, MANIFEST_EXTS
from .api import _fuzz, _lookup, _filter
from .patch import addon_patch

from typing import Protocol
from collections.abc import Iterator, Iterable


class ProgressProtocol(Protocol):
    def __init__(self, size: int, message: str): ...
    def __enter__(self) -> SilentProgress: ...
    def __exit__(self, *args, **kwargs): ...
    def update(self, size: int): ...


class SilentProgress:
    """ NOP class "implementing" progress as context manager """

    def __init__(self, size: int, message: str):
        """ Display progress with given total size and message """
        pass

    def __enter__(self) -> SilentProgress:
        """ Start of progress """
        return self

    def __exit__(self, *args, **kwargs):
        """ End of progress """
        pass

    def update(self, size: int):
        """ Update progress with given chunk size """
        pass


class Folder:
    def __init__(self, game: str, config: configparser.ConfigParser):
        self.root: pathlib.Path = pathlib.Path(config.get(f'{game}.addons', 'root'))
        self.url_template: str = config.get(f'{game}.links', 'download')
        #: A list of addons that have local file info and are enriched as appropriate with API info
        self._installed: dict[pathlib.Path, gru.addon.InstalledAddon] = {}

    @property
    def installed(self) -> Iterable[gru.addon.InstalledAddon]:
        return self._installed.values()

    @contextlib.contextmanager
    def temp_root(self) -> Iterator[gru.addon.Folder]:
        """ Yields a similarly-configured install folder at a temporary location. """
        with tempfile.TemporaryDirectory() as tempdir:
            yield Folder('game', {'game.addons': tempdir, 'game.links': self.url_template})

    @contextlib.contextmanager
    def unmodified_addon(self, addon: gru.addon.AddonInfo, api: gru.api.API, url: str | None = None) -> Iterator[gru.addon.InstalledAddon]:
        with self.temp_root() as temp_root:
            temp_location = temp_root.root / addon.dir
            temp_addon = temp_root.unpack(addon, api, url_override=url)
            yield temp_addon
            shutil.rmtree(temp_location)

    def scan(self, api: gru.api.API | None = None):
        self._installed = self._scan(self.root, api)

    def _scan(self, root: pathlib.Path, api: gru.api.API | None = None) -> dict[pathlib.Path, gru.addon.InstalledAddon]:
        """ List the root path """
        results: dict[pathlib.Path, gru.addon.InstalledAddon] = {}

        # Additional housekeeping for partial parsing
        if root != self.root:
            if not root.relative_to(self.root):
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
                except PermissionError as err:
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

            try:
                if api:
                    addon.link(api.dir(path.name))
            except FileNotFoundError as err:
                # Only warn for lookup error on top-level addons
                if parent is None:
                    warnings.warn(f'Addon at {path.relative_to(self.root)} not found in database')

            results[path] = addon

        # Now we may have several addons claiming ownership of the same directories
        return results

    def name(self, name: str):
        """ Lookup addons by name (exact match) """
        return _filter(self.installed, 'title', str(name))

    def dir(self, dir_: str):
        """ Lookup addons by name (exact match) """
        return _filter(self.installed, 'dir', str(dir_))

    def id(self, id_: int):
        """ Lookup addons by id (exact match) """
        return _filter(self.installed, 'id', id_)

    def find(self, val: str, api: gru.api.API):
        """ Search for an installed addon generically """
        # Various methods of exact matches
        if by_name := self.name(val):
            return by_name

        if by_dir := self.dir(val):
            return by_dir

        # Otherwise revert to search and return a list of candidates
        if search := self.search(val):
            return search

        return sum((self.id(addon.id) for addon in api.search(val)), [])

    def search(self, term: str, tiebreakattr: str | None = None, maxlen: int = 30):
        """ Search `term` in addon names """
        # We want at least 75% of search string in result
        return [
            *_fuzz(self.installed.values(), 'title', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                   tiebreakattr=[tiebreakattr]),
            *_fuzz(self.installed.values(), 'dir', term, cutoff=.75 if len(term) > 3 else 1, maxlen=maxlen,
                   tiebreakattr=[tiebreakattr]),
        ]

    def find_installed(self, spec: gru.addon.Dependency) -> gru.addon.InstalledAddon | None:
        for folder in self.dir(spec.dir):
            if folder.dep_version >= spec.dep_version:
                return folder

    def __repr__(self):
        return f'Folder({self.root})'

    def _inspect_bundle(self, path: pathlib.Path, zf: zipfile.ZipFile, api: gru.api.API) -> tuple[pathlib.Path, list[pathlib.Path], list[tuple[pathlib.Path, bool, int]]]:
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
        files = [(pathlib.Path(info.filename), info.is_dir(), getattr(info, 'file_size', 0)) for info in zf.infolist() if not info.filename.startswith(tuple(GARBAGE))]
        # Ignore files that would end up outside target directory
        path = path.resolve()
        files = [(fn, *_) for fn, *_ in files if (path / fn).resolve(strict=False).is_relative_to(path)]
        # Extract specific files we’re interested in
        manifests = {fn.with_suffix('') for fn, *_ in files if (fn.stem == fn.parent.name or len(fn.parts) == 1) and fn.suffix in MANIFEST_EXTS}
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
        if len(manifest_depth0) > 1:
            manifest_depth0 = [fn for fn in manifest_depth0 if zf.filename.startswith(fn.name)]

        if len(manifest_depth0) == 1:
            warnings.warn(f'Addon bundle missing top-level dir and unexpected manifest name, prepending {manifest_depth0[0].name}/ to zip contents, instead of {path.name}')
            path = path.parent / manifest_depth0[0].name
            return path, [path], files

        # Try to find any manifest? Do not take into account non-single top-level .txt files
        manifest_depth1plus = [fn for fn in manifests if len(fn.parts) > 1]

        if not manifest_depth1plus:
            raise ValueError(f'No addon manifest in bundle {zf.filename}')

        # No identified manifest, we have to guess what we’re really installing
        # In 1-dir case does not really matter
        if len(toplevels) == 1:
            top_dir = next(iter(toplevels))
            if top_dir != path.name:
                warnings.warn(f'Using {top_dir} as addon dir, installing under {top_dir}')
                path = path.parent / top_dir
            return path.parent, [path], files

        # From here on we handle several top-level directories, i.e. risk of clashing
        # as some secondary top-levels might be owned by other addons

        # Try to match zip contents to where addon is already installed
        # E.g. we are updating Foo at {root}/dir/Foo/Foo.txt, zip contains (Foo/Foo.txt, Bar/): install at {root}/dir
        for parent in path.parents:
            if parent == self.root or not parent.is_relative_to(self.root):
                break
            siblings = [parent / top for top in toplevels]
            if all(dir_.exists() for dir_ in siblings):
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

        if len(toplevels) > 1:
            warnings.warn(f'Installing {len(toplevels)} addons as part of {path.name}: {", ".join(toplevels)}')
        return path.parent, [path.parent / top for top in toplevels], [(fn, *_) for fn, *_ in files if fn.parts[0] in toplevels]

    def _download(self, dl: requests.Response, fd: typing.BinaryIO, progress: ProgressProtocol):
        with progress as prog:
            for chunk in dl.iter_content(chunk_size=1024):
                fd.write(chunk)
                prog.update(len(chunk))

    def _unzip(self, zf: zipfile.ZipFile, files: list[tuple[pathlib.Path, bool, int]], dest: pathlib.Path, progress: ProgressProtocol):
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
                with zf.open(str(file), 'r') as zfreader, open(file_dest, 'wb') as out:
                    shutil.copyfileobj(zfreader, out)
                prog.update(size)

    def unpack(self, addon: gru.addon.AddonInfo, api: gru.api.API, progress: type[ProgressProtocol] = SilentProgress, path : pathlib.Path | None = None, url_override: str | None = None) -> dict[pathlib.Path, gru.addon.InstalledAddon]:
        """ Download and install, calls back to `progress` (100% until return means unzipping) """
        # NB. any file name returns correct file eventually, and “correct” file names are underterministic.
        # However, server-side caching means we can get stale versions if we use a version-independent url.
        # Do not use a random string, so we don’t defeat the purpose of server-side caching.
        fname = f'{addon.dir}-{addon.version}.zip'
        url = url_override or self.url_template.format(id=addon.id) + urllib_quote(fname)

        with requests.head(url, allow_redirects=True) as check:
            headers = {key.lower(): value for key, value in check.headers.items()}

        size = int(headers.get('content-length', 0))
        if changed := headers.get('last-modified'):
            changed = datetime.datetime.strptime(changed, r'%a, %d %b %Y %H:%M:%S %Z')
        # Try to get suggested filename from headers
        for tok in map(str.strip, headers.get('content-disposition', '').split(';')):
            if tok.startswith('filename='):
                fname = tok[10:].strip('"')
                break

        if (zippath := user_cache('dl', fname)).exists():
            stat = zippath.stat()
            # NB. this is correct on *nix and NTFS, but not FAT which uses local timezone
            # Hopefully FAT is not used too much anymore? Otherwise we need a config() function to handle this
            freshness = datetime.datetime.utcfromtimestamp(stat.st_mtime)
            fresh = size == stat.st_size and changed and changed < freshness

        # Download
        if zippath.exists() and fresh:
            print(f'Using cache {fname}')
        else:
            with requests.get(url, stream=True, allow_redirects=True) as dl:
                with open(zippath, 'wb') as fd:
                    self._download(dl, fd, progress(size, f'Downloading {fname}...'))

        # Default install dir -- requires a temporary addon object that’s not the (read-only) API one,
        # and can’t be the one initialized with metadata from the manifest
        install_folder = path or self.root / addon.dir

        # Inspect, extract
        with zipfile.ZipFile(zippath) as zf:
            files = zf.infolist()
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
        except FileNotFoundError: # Manifest not in expected location
            # Not the simple case, maybe a multi-directory addon -- defer to our more complex logic handling
            installed_addons = self._scan(install_folder, api)

        for inst in installed_addons.values():
            inst.link(addon)
        self._installed.update(installed_addons)
        return installed_addons.values()

    def install(self, addon: gru.addon.AddonInfo, api: gru.api.API, progress: ProgressProtocol | None = None, path: pathlib.Path | None = None, deps: bool = True, opt: bool = False):
        installed = self.unpack(addon, api, path=path, progress=progress)

        if deps:
            return self.install_deps(installed, api, progress=progress, opt=opt)

    def _dedup_deps(self, deps: Iterable[gru.addon.Dependency]):
        dedup = {}
        for dep in deps:
            dedup.setdefault(dep.dir, []).append(dep)
        return [max(dep_versions, key=lambda dep: dep.dep_version) for dep_versions in dedup.values()]

    def missing_deps(self, pool: Iterable[gru.addon.InstalledAddon], opt: bool = False):
        """ Return dependencies that are missing from `installed` """
        deps = []
        for addon in pool:
            deps.extend(addon.deps)
            if opt:
                deps.extend(addon.optdeps)
        return [dep for dep in self._dedup_deps(deps) if self.find_installed(dep) is None]

    def depcount(self, lib: gru.addon.InstalledAddon, opt: bool = True):
        """ Count the number of times this addon is depended on """
        refcount = sum(lib.dir == dep.dir for addon in self.installed for dep in addon.metadata.deps)
        if opt:
            refcount += sum(lib.dir == dep.dir for addon in self.installed for dep in addon.metadata.optdeps)
        return refcount

    def unused_deps(self, pool: Iterable[gru.addon.InstalledAddon], opt: bool = False):
        unused = []
        for addon in pool:
            if addon.is_lib and self.depcount(addon, opt=opt) == 0:
                unused.append(addon)
        return unused

    def update(self, api: gru.api.API, progress: ProgressProtocol | None = None, opt: bool = False, deps: bool = False, patch: bool = False):
        updates = []
        for addon in self.installed:
            if not addon.can_update:
                continue
            try:
                updates.extend(self.unpack(addon.infos, api, progress=progress))
            except Exception as err:
                warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}')

        for addon in updates:
            if patch and (patch_file := self.root / '.gru' / f'{addon.dir}.patch').exists():
                addon_patch(addon, patch_file)

        if deps:
            return (len(updates), self.install_deps(updates, api, progress=progress, opt=opt, patch=patch))
        else:
            return (len(updates), 0)

    def install_deps(self, pool: Iterable[gru.addon.InstalledAddon], api: api.API, progress: ProgressProtocol | None = None, opt: bool = False, patch: bool = False):
        added = 0
        deps = [*(pool or self.installed)]
        while newdeps := self.missing_deps(deps, opt=opt):
            deps = []
            for dep in newdeps:
                # Do not check if installed as it’s a missing dep
                try:
                    addon = api.dir(dep.dir)
                except ValueError:
                    warnings.warn(f'Failed to look up addon dependence {dep.dir!r}')
                    continue
                try:
                    addons = self.unpack(addon, api, progress=progress)
                except Exception as err:
                    warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}')
                    continue
                for addon in addons:
                    if patch and (patch_file := self.root / '.gru' / f'{addon.dir}.patch').exists():
                        addon_patch(addons, patch_file)
                added += 1
                deps.extend(addons)

        return added

    def remove(self, addon: gru.addon.InstalledAddon | gru.addon.AddonInfo, deps: bool = False, opt: bool = True):
        """ Uninstall addon """
        if not addon.is_local:
            if not addon.folders:
                raise ValueError(f'Addon {addon.title} is not installed')
            addon = list(addon.folders.values())[0]

        shutil.rmtree(addon.folder)
        del self._installed[addon.folder]
        if addon.id:
            addon.infos.deregister(addon)

        if not deps:
            return 0

        return self.remove_unused_deps(opt=opt)

    def remove_unused_deps(self, opt: bool = True):
        removed = 0
        while unused := self.unused_deps(self.installed, opt=opt):
            for dep in unused:
                self.remove(dep)
            removed += len(unused)

        return removed
