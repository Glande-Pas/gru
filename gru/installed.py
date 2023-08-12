import collections
import pathlib
import zipfile
import requests
import shutil
import datetime
import functools
import re

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
    def scan(cls, root, api=None):
        """ List the root path """
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
                print(f'Error: Skipping addon at {path} due to {err}')
                continue

            try:
                if api is not None:
                    addon.lookup(api)
            except ValueError:
                print(f'Warning: Addon at {path} not found in database')

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

    def __init__(self, path):
        self.root = pathlib.Path(path)
        self.dir = self.root.name
        self.metadata = {}

        if not (self.root / f'{self.dir}.txt').exists():
            return

    def parse_manifest(self):
        """ Parse the manifest file """
        # Parse metadata handling multiple line entries
        metadata = {}
        with open(self.root / f'{self.dir}.txt') as manifest:
            for line in manifest:
                if line.startswith('## ') and len(line) > 4:
                    try:
                        key, val = line.lstrip('#').split(':', 1)
                    except ValueError:
                        pass
                    metadata.setdefault(key.strip(), []).append(val.strip())

        metadata = {key: ' '.join(val) for key, val in metadata.items()}

        # Now handle all interesting metadata
        # NB. AddOnVersion supposedly is mandatory but effectively missing from some addons
        assert {'Title', 'APIVersion'} <= metadata.keys(), 'Missing mandatory keys'
        self.version = atol(metadata.get('AddOnVersion', '1'), 10)
        self.api = [atol(api, 10) for api in metadata.pop('APIVersion').split()]

        self.display_version = (atol(token) for token in metadata.pop('Version', '').split('.'))
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

    def lookup(self, api):
        """ Looks up the addon’s id in the provided `api.API` instance """
        self.id = int(api.dir(self.dir)['UID'])

    def unpack(self, template, progress_context=NoProgress):
        """ Download and install, calls back to `progress` (100% until return means unzipping) """
        fname = f'{self.dir}.zip'
        url = template.format(id=self.id) + fname

        dl = requests.get(url, stream=True, allow_redirects=True)
        size = int(dl.headers.get('content-length', 0))
        for tok in map(str.strip, dl.headers.get('Content-disposition', '').split(';')):
            if tok.startswith('filename='):
                fname = tok[10:].strip('"')
                break

        zippath = self.root.parent / fname
        with open(zippath, 'wb') as fd, progress_context(int(dl.headers.get('content-length', 0)),
                                                         f'Downloading {fname}...') as prog:
            for chunk in dl.iter_content(1024):
                fd.write(chunk)
                prog.update(len(chunk))

        with zipfile.ZipFile(zippath) as zf:
            files = zf.infolist()
            dest = self.root.parent

            # Wrong directory! The expected manifest is missing
            if not any(f.filename == f'{self.dir}/{self.dir}.txt' for f in files):
                # Try patterns to find it: foo/foo.txt or foo.txt
                manifest = [f.filename for f in files if re.match(r'([^/]+)/\1.txt$', f.filename)]
                if len(manifest) != 1:
                    manifest = [f.filename for f in files if re.match(r'([^/]+).txt$', f.filename)]
                if len(manifest) == 1 and (newdir := pathlib.Path(manifest[0]).stem) != self.dir:
                    print(f'Warning: using {newdir} as install dir instead of {self.dir}')
                    self.dir = newdir
                    self.root = self.root.parent / newdir
                # If zip was just missing proper top-level dir, wrap contents
                if len(manifest) != 1 or '/' not in manifest[0]:
                    print(f'Warning: prepending {self.dir}/ to zip contents')
                    dest /= self.dir

            if self.root.exists():
                shutil.rmtree(self.root)

            # For security, only extract files/directories that will resolve to within the target directory
            valid_members = [info for info in files if (dest / info.filename).resolve().is_relative_to(self.root)]
            extract_size = sum(getattr(info, 'file_size', 0) for info in valid_members)
            with progress_context(extract_size, f'Extracting {fname}...') as prog:
                for info in files:
                    zf.extract(info, path=dest)
                    prog.update(getattr(info, 'file_size', 0))

        zippath.unlink()

    def check_update(self):
        try:
            addon = api.addon(self.id)
        except AttributeError:
            return False

        if 'Version' in self.metadata:
            installed = tuple(atol(token) for token in self.metadata['Version'].split('.'))
            return installed < addon.version

        if 'Date' in addon:
            date = datetime.datetime.fromtimestamp(addon['UIDate'] / 1000)
            # TODO: compare with folder metadata

    def missing_deps(self, folders, opt=False):
        missing = {}
        for dep, version in [*self.deps.items(), *(self.optdeps.items() if opt else ())]:
            if self.find_installed(dep, folders, version) is None:
                missing[dep] = version
        return missing
