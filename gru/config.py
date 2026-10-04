# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Module handling user configuration """

from __future__ import annotations

import charset_normalizer
import configparser
import contextlib
import builtins
import gettext
import pathlib
import typing
import sys
import os
import io
import re
from collections.abc import Iterator

IS_POSIX = os.name == 'posix'
IS_MAC_OS = sys.platform == 'darwin'
IS_WINDOWS = os.name == 'nt'

defaults = """
[api]
endpoint = https://api.mmoui.com/v{version}/game/{game}/{path}
version = 3

[ESOUIv4.paths]
globalconf = ../../globalconfig.json

[ESOUIv3.paths]
globalconf = ../../globalconfig.json
gameconf = gameconfig.json
catlist = categorylist.json
filelist = filelist.json
listfiles = listfiles/{id}.json
filedetails = filedetails/{id}.json

[ESO.links]
info = https://www.esoui.com/downloads/info{id}.html
download = https://cdn.esoui.com/downloads/getfile.php?id={id}

[ESO.addons]
# Path to addons root directory (live), and to the PTS one
root =
pts_root =
# Whether to include optional dependences by default
optional = off
# Whether to automatically re-apply patches on updates
patch_updates = on
# Sort equal matches in search according to one of: downloads, monthly, favorites
sortkey = downloads
# Number of rows kept in the rotating changes.csv log
log_lines = 100
# Whether `remove` also deletes an addon's SavedVariables file(s): yes, no, or ask
remove_saved_variables = ask

[app]
"""

CONFIG_FILENAME = 'config.ini'


@contextlib.contextmanager
def encoding_open(fname: pathlib.Path | str) -> Iterator[typing.IO]:
    with open(fname, 'rb') as f:
        # charset_normalizer does not recognize BOM?!
        if f.read(3) == b'\xef\xbb\xbf':
            encoding = 'utf_8_sig'
        else:
            f.seek(0)
            match = charset_normalizer.from_fp(f).best()
            encoding = match.encoding if match is not None else 'utf-8'

    with open(fname, 'r', encoding=encoding) as f:
        yield f


def user_home() -> pathlib.Path:
    if (userhome := os.environ.get('HOME')) is not None:
        return pathlib.Path(userhome)
    elif (userhome := os.environ.get('USERPROFILE')) is not None:
        return pathlib.Path(userhome)
    elif (userhome := os.environ.get('HOMEPATH')) is not None:
        if (userdrive := os.environ.get('HOMEDRIVE')) is not None:
            return pathlib.Path(userdrive) / userhome
        else:
            return pathlib.Path(userhome)
    else:
        return pathlib.Path('~').expanduser()


def user_cache(*args: str) -> pathlib.Path:
    """ Returns the appropriate path to the cache file in the user app dirs.

    Returns:
        :class:`~pathlib.Path`: path to the cache file or directory.
    """
    if IS_WINDOWS:
        appdata = os.getenv('LOCALAPPDATA') or os.getenv('APPDATA')
        if appdata is None:
            raise EnvironmentError('Neither LOCALAPPDATA nor APPDATA environment variables are set')
        base_dir = pathlib.Path(appdata)
    elif IS_MAC_OS:
        # NB. for local ~/Library/Logs
        base_dir = pathlib.Path('~/Library/Caches').expanduser()
    else:
        base_dir = pathlib.Path(os.getenv('XDG_CACHE_HOME', '~/.cache')).expanduser()

    base_dir /= 'gru'
    base_dir.mkdir(parents=True, exist_ok=True)

    file = base_dir.joinpath(*args)
    file.parent.mkdir(parents=True, exist_ok=True)
    return file


def user_config(*args: str) -> pathlib.Path:
    """ Returns the path to gru's own directory in the user config dir, or a path within it.
    This holds the config file itself, plus gru's per-game metadata (saved patches, exports)
    that used to live inside the addons folder.

    Returns:
        :class:`~pathlib.Path`: path to the user config directory (no args), or a path within it.
    """
    if IS_WINDOWS:
        appdata = os.getenv('APPDATA')
        if appdata is None:
            raise EnvironmentError('APPDATA environment variable is not set')
        base_dir = pathlib.Path(appdata)
    elif IS_MAC_OS:
        base_dir = pathlib.Path('~/Library/Preferences').expanduser()
    else:
        base_dir = pathlib.Path(os.getenv('XDG_CONFIG_HOME', '~/.config')).expanduser()

    base_dir /= 'gru'
    base_dir.mkdir(parents=True, exist_ok=True)

    file = base_dir.joinpath(*args)
    file.parent.mkdir(parents=True, exist_ok=True)
    return file


# Relative to the home directory
_DOCUMENTS = ['Documents', 'OneDrive/Documents']
TARGETS = ('live', 'pts')
_STEAM_ROOTS = [
    '.local/share/Steam',
    '.steam/steam',
    '.var/app/com.valvesoftware.Steam/.local/share/Steam',  # Flatpak
    '.var/app/com.valvesoftware.Steam/data/Steam',  # older Flatpak
    'snap/steam/common/.local/share/Steam',  # Snap
]
_STEAM_PREFIX = 'steamapps/compatdata/306130/pfx'  # 306130 is ESO's Steam app id
_WINE_PREFIXES = [  # glob patterns
    '.wine',
    'Games/*',  # Lutris
    'Games/Heroic/Prefixes/*',
    'Games/Heroic/Prefixes/default/*',
    '.local/share/bottles/bottles/*',
    '.var/app/com.usebottles.bottles/data/bottles/bottles/*',  # Flatpak Bottles
]


def _steam_libraries(root: pathlib.Path) -> list[pathlib.Path]:
    """ A Steam install's own library, plus the extra ones (e.g. on other drives) listed in libraryfolders.vdf """
    libraries = [root]
    with contextlib.suppress(OSError):
        vdf = (root / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
        libraries.extend(pathlib.Path(path.replace('\\\\', '\\')) for path in re.findall(r'"path"\s+"([^"]*)"', vdf))
    return libraries


def root_key(target: str = 'live') -> str:
    """ Name of the `addons` config entry holding the target's AddOns directory """
    return 'root' if target == 'live' else f'{target}_root'


def addons_dir_candidates(target: str = 'live') -> Iterator[pathlib.Path]:
    """ Usual locations of the AddOns folder: native installs, then Steam/Proton, then other Wine prefixes """
    home = user_home()
    _GAME_ADDONS = [f'Elder Scrolls Online/{target}/AddOns']
    for docs in _DOCUMENTS:
        for game in _GAME_ADDONS:
            yield home / docs / game

    for root in _STEAM_ROOTS:
        for library in _steam_libraries(home / root):
            for game in _GAME_ADDONS:
                yield library / _STEAM_PREFIX / 'drive_c/users/steamuser/Documents' / game

    for pattern in _WINE_PREFIXES:
        for prefix in sorted(home.glob(pattern)):
            for game in _GAME_ADDONS:
                yield from sorted(prefix.glob(f'drive_c/users/*/Documents/{game}'))


def load_config(config_file: pathlib.Path | str | None = None) -> configparser.ConfigParser:
    config = configparser.ConfigParser(delimiters=['='])
    config.read_file(io.StringIO(defaults))

    config_file = user_config(CONFIG_FILENAME) if config_file is None else pathlib.Path(config_file)
    if config_file.exists():
        config.read(config_file)

    changed = False
    for target in TARGETS:
        key = root_key(target)
        if config.get('ESO.addons', key).strip():
            continue
        for addons_dir in addons_dir_candidates(target):
            if addons_dir.is_dir():
                config.set('ESO.addons', key, str(addons_dir.resolve()))
                changed = True
                break

    if changed:
        save_config(config, config_file)
    return config


def display_config(config: configparser.ConfigParser, game: str) -> dict[str, str]:
    # The cli-configurable sections and names under which they will appear
    return {
        **{f'app.{key}': value for key, value in config.items('app')},
        **{f'addons.{key}': value for key, value in config.items(f'{game}.addons')},
    }


def update_config(config: configparser.ConfigParser, game: str, values: dict[str, str]) -> None:
    # The cli-configurable sections and names under which they will appear
    sections = {'app': 'app', 'addons': f'{game}.addons'}

    for (sec, entry), value in ((key.split('.', 1), value) for key, value in values.items()):
        prev = config.get(sections[sec], entry)
        if (prev in {'on', 'off'}) != (value in {'on', 'off'}):
            raise ValueError
        config.set(sections[sec], entry, value)


def save_config(config: configparser.ConfigParser, config_file: pathlib.Path | str | None = None) -> None:
    config_file = user_config(CONFIG_FILENAME) if config_file is None else pathlib.Path(config_file)
    with open(config_file, 'w') as f:
        config.write(f)


class FormatMixin():
    if typing.TYPE_CHECKING:
        # Only ever mixed in alongside a gettext.NullTranslations/GNUTranslations base,
        # which is what actually provides gettext() at runtime.
        def gettext(self, message: str) -> str: ...

    def gettext_format(self, string: str, *args: typing.Any, **kwargs: typing.Any) -> str:
        """ A function that condenses translation.gettext(string).format(...) in single function """
        return self.gettext(string).format(*args, **kwargs)

    def install(self, names: typing.Any = None) -> None:
        builtins.__dict__['_'] = self.gettext_format


class NullFormatTranslations(FormatMixin, gettext.NullTranslations):
    pass


class GNUFormatTranslations(FormatMixin, gettext.GNUTranslations):
    pass


def install_translation(domain: str, localedir: pathlib.Path) -> None:
    """ Installs a gettext translation object.

    This re-implements gettext’s translation() and find() followed by .install(), to use a Traversable as localedir

    Use as: install_translation('gru', importlib_resources.files('gru').joinpath('share', 'locale'))
    """
    for envar in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
        if enval := os.environ.get(envar):
            break
    else:
        return gettext.NullTranslations().install()

    # now normalize and expand the languages
    for lang in enval.split(':'):
        for nelang in gettext._expand_lang(lang):  # pyright: ignore[reportAttributeAccessIssue] -- private gettext API
            file = localedir.joinpath(nelang, 'LC_MESSAGES', domain + '.mo')
            if file.is_file():
                with file.open('rb') as fp:
                    return GNUFormatTranslations(fp).install()
    else:
        return NullFormatTranslations().install()
