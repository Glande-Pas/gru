import collections
import pathlib
import zipfile
import requests
import shutil
import datetime
import functools
import warnings
import re
import charset_normalizer

from .config import config

class NoProgress:
    def __init__(self, size, message):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass

    def update(self, size):
        pass


def atol(val, *args):
    """ Convert to int() with C atol semantics, i.e. ignore non-numeric trailing characters """
    val = val.strip()
    while val and not val.isnumeric():
        val = val[:-1]
    return int(val, *args)


class Folder:
    @classmethod
    def scan(cls, api=None):
        """ List the root path """
        root = config.get('ESO.addons', 'root')
        candidates = collections.deque(pathlib.Path(root).iterdir())
        results = []
        while candidates:
            path = candidates.pop()
            if not path.is_dir():
                continue

            # Recursively check for depths up to 3
            if len(path.relative_to(root).parts) < 3:
                candidates.extend(path.iterdir())

            if not path.joinpath(f'{path.name}.txt').exists():
                continue

            addon = Folder(path)
            try:
                addon.parse_manifest()
            except Exception as err:
                warnings.warn(f'Skipping addon at {path} due to {type(err).__name__} {err}')
                continue

            try:
                if api is not None:
                    addon.lookup(api)
            except ValueError:
                warnings.warn(f'Addon at {path} not found in database')

            results.append(addon)
        return results

    @classmethod
    def find_installed(cls, dir_name, installed, version=0):
        for folder in installed:
            if folder.dir == dir_name:
                if folder.version >= version:
                    return folder
        else:
            return None

    def __repr__(self):
        return f'Folder({self.root})'

    @property
    def manifest(self):
        return self.root / f'{self.dir}.txt'

    def __init__(self, path, **kwargs):
        self.root = pathlib.Path(path)
        if not self.root.is_absolute():
            self.root = config.get('ESO.addons', 'root') / self.root

        self.dir = self.root.name
        self.metadata = {}

        if 'id' in kwargs:
            self.id = kwargs.pop('id')
        if kwargs:
            raise ValueError(f'Unexpected keyword arguments for {type(self).__name__}: {", ".join(kwargs.keys())}')

        if not self.manifest.exists():
            return

    def parse_manifest(self):
        """ Parse the manifest file """
        # Parse metadata handling multiple line entries
        metadata = {}
        encoding = charset_normalizer.from_path(self.manifest).best().encoding
        with open(self.manifest, encoding=encoding) as manifest:
            for line in manifest:
                if line.startswith('## ') and len(line) > 4:
                    try:
                        key, val = line.lstrip('#').split(':', 1)
                    except ValueError:
                        pass
                    metadata.setdefault(key.strip(), []).append(val.strip())

        metadata = {key: ' '.join(val) for key, val in metadata.items()}

        # Now handle all interesting metadata
        self.version = atol(metadata.get('AddOnVersion', '1'), 10)
        self.api = [atol(api, 10) for api in metadata.pop('APIVersion').split()]

        self.display_version = tuple(atol(token) for token in metadata.pop('Version', '').split('.'))
        assert 1 <= len(self.api) <= 2 and 100003 <= min(self.api) and max(self.api) <= 999999, \
            'Unexpected API Version format'

        islib = metadata.pop('IsLibrary', 'false').lower()
        assert islib in {'true', 'false'}, 'Unexpected value for IsLibrary'
        self.library = islib == 'true'

        self.deps = {name: atol(version[0]) if version else 0 for name, *version in (
            dep.split('>=') for dep in metadata.pop('DependsOn', '').split()
        )}
        self.optdeps = {name: atol(version[0]) if version else 0 for name, *version in (
            dep.split('>=') for dep in metadata.pop('OptionalDependsOn', '').split()
        )}

        # Whatever remains: title, author, etc.
        self.metadata.update(metadata)

        # NB. emit warning last on keys that were not popped
        missing_mandatory_keys = {'Title'} - metadata.keys()
        if missing_mandatory_keys:
            warnings.warn(f'Missing mandatory key(s) {", ".join(missing_mandatory_keys)} in {self}')

    def lookup(self, api):
        """ Looks up the addon’s id in the provided `api.API` instance """
        self.id = int(api.dir(self.dir)['UID'])

    def _inspect_bundle(self, zf):
        """ This is the annoying bit where we need to handle non-standard zip bundles

        General logic:
        - Standard addon: install in addon dir
        - “naked” addon (i.e. addon contents without directory): wrap in top-level dir
        - non-addon folders included: wrap in top-level dir
        - several addons bundled together at top level: install non-standalone addons to avoid clashes

        Returns a tuple of:
        - a destination directory for zip contents,
        - a list of file infos from the zip, such that their extracted path ends up in self.root
        """
        # NB: always ignore macos garbage
        garbage = ('__MACOSX', '.DS_Store')
        files = [info for info in zf.infolist() if not info.filename.startswith(garbage)]
        toplevels = {pathlib.Path(info.filename).parts[0] for info in files}

        # Try to find a single manifest at expected location with expected name: standard case
        expected_manifest = any(f.filename == f'{self.dir}/{self.dir}.txt' for f in files)
        if expected_manifest and len(toplevels) == 1:
            return self.root.parent, files

        # Try to find a single manifest at expected location with any name
        manifest_depth1 = [f.filename for f in files if re.match(r'([^/]+)/\1.txt$', f.filename)]
        if len(manifest_depth1) == 1 and len(toplevels) == 1:
            self.dir = pathlib.Path(manifest_depth1[0]).stem
            warnings.warn(f'Using {self.dir} as install dir instead of {self.root.name}')
            self.root = self.root.parent / self.dir
            return self.root.parent, files

        # Try to find a single top-level manifest with expected name
        if any(f.filename == f'{self.dir}.txt' for f in files):
            warnings.warn(f'Addon bundle missing top-level dir, prepending {self.dir}/ to zip contents')
            return self.root, files

        # Try to find a single top-level manifest with any name
        manifest_depth0 = [f.filename for f in files if re.match(r'([^/]+).txt$', f.filename)]
        # If needed, try to reduce top-level manifest candidates to files whose stem appears in zip’s name
        # i.e. {addon}.txt in {addon.zip}, {addon}-{version}.zip, {addon}r{release}.zip, etc.
        if len(manifest_depth0) > 1:
            manifest_depth0 = [f for f in manifest_depth0 if zf.filename.startswith(pathlib.Path(f).stem)]

        if len(manifest_depth0) == 1:
            self.dir = pathlib.Path(manifest_depth0[0]).stem
            warnings.warn(f'Using {self.dir} as install dir instead of {self.root.name}')
            self.root = self.root.parent / self.dir
            warnings.warn(f'Addon bundle missing top-level dir, prepending {self.dir}/ to zip contents')
            return self.root, files

        # Try to find any manifest? Do not take into account non-single top-level .txt files
        manifest_depth1plus = manifest_depth1 + sorted(
            (f.filename for f in files if re.search(r'/([^/]+)/\1.txt$', f.filename) is not None),
            key=lambda f: f.count('/')
        )

        if not manifest_depth1plus:
            raise ValueError(f'No addon manifest in bundle {zf.filename}')

        # Some addons bundle gamedata, EsoUI (etc) as top-level folders, install to a subdirectory
        if len(toplevels) > 1 and not all(f'{name}/{name}.txt' in manifest_depth1 for name in toplevels):
            warnings.warn(f'Multiple-directory addon, prepending {self.dir}/ to zip contents')
            return self.root, files

        # We have a guess of what we’re really installing -- does not really matter in terms of addon clashes as it’s all 1 dir
        if len(toplevels) == 1:
            self.dir = pathlib.Path(manifest_anydepth[0]).stem
            self.root = self.root.parent / toplevels.pop()
            warnings.warn(f'Using {self.dir} as addon dir, installing under {self.root.name}')
            return self.root.parent, files

        # So now we know we have several top-level addons, i.e. risk of clashing
        # install all directories that resolve to this addon (bundle’s main addon) or to no addon (not standalone)
        from .api import API
        api = API()
        for dir_ in list(toplevels):
            try:
                addon = api.dir(dir_)
            except KeyError:
                install.append(dir_)
            else:
                if addon['UID'] != self.id:
                    toplevels.remove(dir_)

        if len(toplevels) > 1:
            warnings.warn(f'Installing {len(toplevels)} addons as part of {self.dir}: {", ".join(toplevels)}')
        return self.root.parent, [info for info in files if pathlib.Path(info.filename).parts[0] in toplevels]


    def unpack(self, progress_context=NoProgress):
        """ Download and install, calls back to `progress` (100% until return means unzipping) """
        fname = f'{self.dir}.zip'
        url = config.get('ESO.links', 'download').format(id=self.id) + fname

        dl = requests.get(url, stream=True, allow_redirects=True)
        size = int(dl.headers.get('content-length', 0))
        # Try to get suggested filename from headers
        for tok in map(str.strip, dl.headers.get('Content-disposition', '').split(';')):
            if tok.startswith('filename='):
                fname = tok[10:].strip('"')
                break

        # Download
        zippath = self.root.parent / fname
        with open(zippath, 'wb') as fd, progress_context(int(dl.headers.get('content-length', 0)),
                                                         f'Downloading {fname}...') as prog:
            for chunk in dl.iter_content(1024):
                fd.write(chunk)
                prog.update(len(chunk))

        # Inspect, extract
        with zipfile.ZipFile(zippath) as zf:
            files = zf.infolist()
            dest, extract = self._inspect_bundle(zf)
            # Safety: only extract files that resolve into self.root, i.e. ignore AddOnName/../../../../.bashrc:w
            extract = [
                info for info in files if (dest / info.filename).resolve().is_relative_to(self.root)
            ]

            if self.root.exists():
                shutil.rmtree(self.root)

            extract_size = sum(getattr(info, 'file_size', 0) for info in extract)
            with progress_context(extract_size, f'Extracting {fname}...') as prog:
                for info in extract:
                    zf.extract(info, path=dest)
                    prog.update(getattr(info, 'file_size', 0))

        self.parse_manifest()
        zippath.unlink()

    def remove(self):
        """ Uninstall addon """
        shutil.rmtree(self.root)

    def check_update(self, api):
        try:
            addon = api.addon(self.id)
        except AttributeError:
            return False

        try:
            return self.display_version < tuple(atol(token) for token in addon['UIVersion'].split('.'))
        except ValueError:
            pass

        stat = self.manifest.stat()
        # NB. some file systems have 2s resolution, but addons should never get updates within 2s
        return max(stat.st_mtime, stat.st_ctime) + 2 <= addon['UIDate'] / 1000

    def missing_deps(self, folders, opt=False):
        """ Return dependencies that are missing from `folders` """
        missing = {}
        for dep, version in [*self.deps.items(), *(self.optdeps.items() if opt else ())]:
            if self.find_installed(dep, folders, version) is None:
                missing[dep] = version
        return missing

    @classmethod
    def all_missing_deps(cls, pool, folders, opt=False):
        missing = {}
        for addon in pool:
            deps = addon.missing_deps(folders, opt=opt)
            missing.update({dep: max(ver, missing.get(dep, ver)) for dep, ver in deps.items()})
        return missing

    def depcount(self, folders, opt=True):
        """ Count the number of times this addon is dependend on in `folders` """
        refcount = sum(self.dir in addon.deps for addon in folders)
        if opt:
            refcount += sum(self.dir in addon.optdeps for addon in folders)
        return refcount

    @classmethod
    def all_unused_deps(cls, pool, folders, opt=False):
        unused = []
        for addon in pool:
            if addon.islib and addon.depcount(folders) == 0:
                unused.append(addon)
        return unused
