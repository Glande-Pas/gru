""" Module handling an install location """
import collections
import pathlib
import zipfile
import asyncio
import aiofiles
import aiofiles.os
import aiohttp
import aioshutil
import datetime
import functools
import operator
import warnings
import re
import charset_normalizer
from urllib.parse import quote as urllib_quote

from .addon import Addon, Dependency, atol


class SilentProgress:
    """ NOP class "implementing" progress """

    def __init__(self, size, message):
        """ Display progress with given total size and message """
        pass

    def __enter__(self):
        """ Start of progress """
        return self

    def __exit__(self, *args, **kwargs):
        """ End of progress """
        pass

    def update(self, size):
        """ Update progress with given chunk size """
        pass


class Folder:
    def __init__(self, game, config, api=None):
        self.root = pathlib.Path(config.get(f'{game}.addons', 'root'))
        self.url_template = config.get(f'{game}.links', 'download')
        #: A list of Addon() instances that have local file info and are enriched as appropriate with API info
        self.installed = []

    async def scan(self, api=None):
        """ List the root path """
        candidates = collections.deque(self.root.iterdir())
        self.installed.clear()
        while candidates:
            path = candidates.pop()
            if not path.is_dir():
                continue

            # Recursively check for depths up to 3
            if len(path.relative_to(self.root).parts) < 3:
                candidates.extend(pathlib.Path(sub) for sub in await aiofiles.os.listdir(path))

            manifest = path / f'{path.name}.txt'
            if not await aiofiles.os.path.exists(manifest):
                continue

            try:
                infos = await self.parse_manifest(manifest)
            except Exception as err:
                warnings.warn(f'Skipping addon at {path} due to {type(err).__name__} {err}')
                continue

            addon = Addon(None, path, infos)
            try:
                if api is None:
                    raise StopIteration
                addon.merge(api.dir(path.name))
            except (StopIteration, ValueError) as err:
                if not isinstance(err, StopIteration):
                    warnings.warn(f'Addon at {path} not found in database')

            self.installed.append(addon)

    def filter_installed(self, addons):
        # Do not match up by directory as it is flaky, reuse previous matching results through ids
        installed = {
            addon.id: addon for addon in self.installed if addon is not None}
        return [installed[addon.id].merge(addon) for addon in addons if addon.id in installed]

    def check_installed(self, addons):
        # Do not match up by directory as it is flaky, reuse previous matching results through ids
        installed = {
            addon.id: addon for addon in self.installed if addon is not None}
        return [installed[addon.id].merge(addon) if addon.id in installed else addon for addon in addons]

    def find_installed(self, addon, installed=None, version=0):
        if installed is None:
            installed = self.installed

        for folder in installed:
            if folder.dir == addon.dir and folder.metadata['dep_version'] >= version:
                return folder
        else:
            return None

    def __repr__(self):
        return f'Folder({self.root})'

    async def parse_manifest(self, manifest_path):
        """ Parse the manifest file """
        # Parse metadata handling multiple line entries
        metadata = {}
        encoding = charset_normalizer.from_path(manifest_path).best().encoding
        async with aiofiles.open(manifest_path, encoding=encoding) as manifest:
            async for line in manifest:
                if line.startswith('## ') and len(line) > 4:
                    try:
                        key, val = line.lstrip('#').split(':', 1)
                    except ValueError:
                        continue
                    metadata.setdefault(key.strip(), []).append(val.strip())

        metadata = {key: ' '.join(val) for key, val in metadata.items()}
        missing_mandatory_keys = {'Title', 'APIVersion'} - metadata.keys()

        # Now handle all interesting metadata
        infos = {}
        infos['dep_version'] = atol(metadata.get('AddOnVersion', '1'))
        infos['api'] = [atol(api) for api in metadata.pop('APIVersion').split()]
        infos['installed_version'] = metadata.pop('Version', '')
        assert 1 <= len(infos['api']) <= 2 and 100003 <= min(infos['api']) and max(infos['api']) <= 999999, \
            f'Unexpected API Version format {infos["api"]!r}'

        islib = metadata.pop('IsLibrary', 'false').lower()
        assert islib in {'true', 'false'}, f'Unexpected value for IsLibrary {islib!r}'
        infos['library'] = islib == 'true'

        infos['deps'] = [Dependency(name, atol(version[0]) if version else 0) for name, *version in (
            dep.split('>=') for dep in metadata.pop('DependsOn', '').split()
        )]
        infos['optdeps'] = [Dependency(name, atol(version[0]) if version else 0) for name, *version in (
            dep.split('>=') for dep in metadata.pop('OptionalDependsOn', '').split()
        )]

        # Whatever remains: title, author, etc.
        infos.update({key.lower(): val for key,
                     val in metadata.items() if key.lower() not in infos})

        # NB. emit warning last
        if missing_mandatory_keys:
            warnings.warn(f'Missing mandatory key(s) {", ".join(map(repr, missing_mandatory_keys))} in {manifest_path}')

        return infos

    def _inspect_bundle(self, addon, zf, api):
        """ This is the annoying bit where we need to handle non-standard zip bundles

        General logic:
        - Standard addon: install in addon dir
        - “naked” addon (i.e. addon contents without directory): wrap in top-level dir
        - non-addon folders included: wrap in top-level dir
        - several addons bundled together at top level: install non-standalone addons to avoid clashes

        Returns a tuple of:
        - a destination directory for zip contents,
        - a list of file infos from the zip, such that their extracted path ends up in addon.root
        """
        # NB: always ignore macos garbage
        garbage = ('__MACOSX', '.DS_Store')
        files = [info for info in zf.infolist() if not info.filename.startswith(garbage)]
        toplevels = {pathlib.Path(info.filename).parts[0] for info in files}

        # Try to find a single manifest at expected location with expected name: standard case
        expected_manifest = any(f.filename == f'{addon.dir}/{addon.dir}.txt' for f in files)
        if expected_manifest and len(toplevels) == 1:
            return addon.folder.parent, files

        # Try to find a single manifest at expected location with any name
        manifest_depth1 = [f.filename for f in files if re.match(r'([^/]+)/\1.txt$', f.filename)]
        if len(manifest_depth1) == 1 and len(toplevels) == 1:
            addon.dir = pathlib.Path(manifest_depth1[0]).stem
            warnings.warn(f'Using {addon.dir} as install dir instead of {addon.folder.name}')
            addon.folder = addon.folder.parent / addon.dir
            return addon.folder.parent, files

        # Try to find a single top-level manifest with expected name
        if any(f.filename == f'{addon.dir}.txt' for f in files):
            warnings.warn(f'Addon bundle missing top-level dir, prepending {addon.dir}/ to zip contents')
            return addon.folder, files

        # Try to find a single top-level manifest with any name
        manifest_depth0 = [f.filename for f in files if re.match(r'([^/]+).txt$', f.filename)]
        # If needed, try to reduce top-level manifest candidates to files whose stem appears in zip’s name
        # i.e. {addon}.txt in {addon.zip}, {addon}-{version}.zip, {addon}r{release}.zip, etc.
        if len(manifest_depth0) > 1:
            manifest_depth0 = [f for f in manifest_depth0 if zf.filename.startswith(pathlib.Path(f).stem)]

        if len(manifest_depth0) == 1:
            addon.dir = pathlib.Path(manifest_depth0[0]).stem
            warnings.warn(f'Using {addon.dir} as install dir instead of {addon.folder.name}')
            addon.folder = addon.folder.parent / addon.dir
            warnings.warn(f'Addon bundle missing top-level dir, prepending {addon.dir}/ to zip contents')
            return addon.folder, files

        # Try to find any manifest? Do not take into account non-single top-level .txt files
        manifest_depth1plus = manifest_depth1 + sorted(
            (f.filename for f in files if re.search(r'/([^/]+)/\1.txt$', f.filename) is not None),
            key=lambda f: f.count('/')
        )

        if not manifest_depth1plus:
            # TODO: if it’s a single Foo/Foo.lua, can we provide a template manifest? We don’t know API version
            raise ValueError(f'No addon manifest in bundle {zf.filename}')

        # Some addons bundle gamedata, EsoUI (etc) as top-level folders, install to a subdirectory
        if len(toplevels) > 1 and not all(f'{name}/{name}.txt' in manifest_depth1 for name in toplevels):
            warnings.warn(f'Multiple-directory addon, prepending {addon.dir}/ to zip contents')
            return addon.folder, files

        # We have a guess of what we’re really installing -- does not really matter in terms of addon clashes as it’s all 1 dir
        if len(toplevels) == 1:
            addon.dir = pathlib.Path(manifest_anydepth[0]).stem
            addon.folder = addon.folder.parent / toplevels.pop()
            warnings.warn(f'Using {addon.dir} as addon dir, installing under {addon.folder.name}')
            return addon.folder.parent, files

        # So now we know we have several top-level addons, i.e. risk of clashing
        # install all directories that resolve to this addon (bundle’s main addon) or to no addon (not standalone)
        for dir_ in list(toplevels):
            try:
                dep = api.dir(dir_)
            except KeyError:
                install.append(dir_)
            else:
                if dep['id'] != dep.id:
                    toplevels.remove(dir_)

        if len(toplevels) > 1:
            warnings.warn(f'Installing {len(toplevels)} addons as part of {addon.dir}: {", ".join(toplevels)}')
        return addon.folder.parent, [info for info in files if pathlib.Path(info.filename).parts[0] in toplevels]

    async def _download(self, dl, fd, progress):
        with progress as prog:
            async for chunk in dl.content.iter_chunked(1024):
                await fd.write(chunk)
                prog.update(len(chunk))

    async def _unzip(self, zf, fileinfos, dest, progress):
        with progress as prog:
            for info in fileinfos:
                filename = dest.joinpath(info.filename).resolve()
                if not filename.is_relative_to(dest):
                    continue

                await aiofiles.os.makedirs(filename if info.is_dir() else filename.parent, exist_ok=True)
                if info.is_dir():
                    continue

                with zf.open(info, 'r') as zfreader, open(filename, 'wb') as out:
                    await aioshutil.copyfileobj(zfreader, out)
                prog.update(info.file_size)

    async def unpack(self, addon, api, progress=None):
        """ Download and install, calls back to `progress` (100% until return means unzipping) """
        if progress is None:
            progress = SilentProgress

        # NB. any file name returns correct file eventually, and “correct” file names are underterministic.
        # However, server-side caching means we can get stale versions if we use a version-independent url.
        # Do not use a random string, so we don’t defeat the purpose of server-side caching.
        fname = f'{addon.dir}-{addon.metadata["version"]}.zip'
        url = self.url_template.format(id=addon.id) + urllib_quote(fname)

        async with aiohttp.ClientSession(raise_for_status=True) as session:
            async with session.get(url, allow_redirects=True) as dl:
                size = int(dl.headers.get('content-length', 0))
                # Try to get suggested filename from headers
                for tok in map(str.strip, dl.headers.get('Content-disposition', '').split(';')):
                    if tok.startswith('filename='):
                        fname = tok[10:].strip('"')
                        break

                # Download
                zippath = self.root / fname
                async with aiofiles.open(zippath, 'wb') as fd:
                    await self._download(dl, fd, progress(size, f'Downloading {fname}...'))

        # Default install dir -- requires a temporary addon object that’s not the RO-API one,
        # and can’t be the one initialized with metadata from the manifest
        if addon.folder is None:
            addon = Addon(addon.id, self.root / addon.dir, addon.metadata)

        # Inspect, extract
        with zipfile.ZipFile(zippath) as zf:
            files = zf.infolist()
            dest, extract = self._inspect_bundle(addon, zf, api)
            # Safety: only extract files that resolve into Folder root, i.e. ignore AddOnName/../../../../.bashrc
            extract = [
                info for info in files if (dest / info.filename).resolve().is_relative_to(self.root)
            ]

            if await aiofiles.os.path.exists(addon.folder):
                await aioshutil.rmtree(addon.folder)

            extract_size = sum(getattr(info, 'file_size', 0) for info in extract)
            await self._unzip(zf, extract, dest, progress(extract_size, f'Extracting  {fname}...'))

        await aiofiles.os.unlink(zippath)

        # Update our list of installed addons
        metadata = await self.parse_manifest(addon.manifest)
        local_addon = Addon(addon.id, addon.folder, metadata).merge(addon)
        try:
            idx = next(n for n, inst in enumerate(self.installed) if inst.folder == local_addon.folder)
        except StopIteration:
            self.installed.append(local_addon)
        else:
            self.installed[idx] = local_addon
        return local_addon

        return self.remove_unused_deps(opt=opt) if deps else 0

    def _dedup_deps(self, deps):
        dedup = {}
        for dep in deps:
            dedup.setdefault(dep.dir, []).append(dep)
        return [max(dep_versions, key=operator.attrgetter('version')) for dep_versions in dedup.values()]

    def missing_deps(self, addon, installed=None, opt=False):
        """ Return dependencies that are missing from `installed` """
        if self.installed is None:
            installed = self.installed

        missing = []
        for dep in addon.metadata['deps'] + (addon.metadata['optdeps'] if opt else []):
            if self.find_installed(dep, installed) is None:
                missing.append(dep)
        return self._dedup_deps(missing)

    def all_missing_deps(self, pool, installed=None, opt=False):
        missing = []
        for addon in pool:
            missing.extend(self.missing_deps(addon, installed, opt=opt))
        return self._dedup_deps(missing)

    def depcount(self, lib, installed=None, opt=True):
        """ Count the number of times this addon is dependend on in `installed` """
        if installed is None:
            installed = self.installed

        refcount = sum(lib.dir == dep.dir for addon in installed for dep in addon.metadata['deps'])
        if opt:
            refcount += sum(lib.dir == dep.dir for addon in installed for dep in addon.metadata['optdeps'])
        return refcount

    def all_unused_deps(self, pool, installed=None, opt=False):
        unused = []
        for addon in pool:
            if addon.metadata['library'] and self.depcount(addon, installed=installed, opt=opt) == 0:
                unused.append(addon)
        return unused

    async def update(self, api, progress=None, opt=False, deps=False):
        updates = []
        for addon in self.installed:
            if not addon.can_update():
                continue
            try:
                addon = await self.unpack(addon, api, progress=progress)
            except Exception as err:
                warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}')
                continue
            updates.append(addon)

        if deps:
            return (len(updates), await self.install_deps(updates, api, progress=progress, opt=opt))
        else:
            return (len(updates), 0)

    async def install_deps(self, pool, api, progress=None, opt=False):
        added = 0
        deps = pool[:]
        while newdeps := self.all_missing_deps(deps, opt=opt):
            deps.clear()
            for dep in newdeps:
                # Do not check if installed as it’s a missing dep
                try:
                    addon = api.dir(dep.dir)
                except ValueError:
                    warnings.warn(f'Failed to look up addon dependence {dep.dir!r}')
                    continue
                try:
                    addon = await self.unpack(addon, api, progress=progress)
                except Exception as err:
                    warnings.warn(f'Failed to install addon dependence {addon.dir!r}: {err}')
                    continue
                added += 1
                deps.append(addon)

        return added

    async def remove(self, addon, deps=False, opt=True):
        """ Uninstall addon """
        if addon.folder is None:
            addon = self.check_installed([addon])[0]
        if addon.folder is None:
            raise ValueError(f'Addon {addon.title} is not installed')

        await aioshutil.rmtree(addon.folder)
        self.installed = [
            inst for inst in self.installed if inst.folder != addon.folder]

        if not deps:
            return 0

        return await self.remove_unused_deps(opt=opt)

    async def remove_unused_deps(self, opt=True):
        removed = 0
        while remove := self.all_unused_deps(self.installed, opt=opt):
            for lib in remove:
                removed += 1
                await self.remove(lib)

        return removed
