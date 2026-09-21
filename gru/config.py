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
download = https://cdn.esoui.com/downloads/file{id}/
#download = https://cdn.esoui.com/downloads/getfile.php?id={id}

[ESO.addons]
# Path to addons root directory
root =
# Whether to include optional dependences by default
optional = off
# Whether to automatically re-apply patches on updates
patch_updates = on
# Sort equal matches in search according to one of: downloads, monthly, favorites
sortkey = downloads

[app]
open_in_browser = off
"""


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


def user_config() -> pathlib.Path:
    """ Returns the path to the configuration file in the user config directory

    Returns:
        :class:`~pathlib.Path`: path to the user configuration file.
    """
    if IS_WINDOWS:
        appdata = os.getenv('APPDATA')
        if appdata is None:
            raise EnvironmentError('APPDATA environment variable is not set')
        return pathlib.Path(appdata) / 'gru.ini'
    elif IS_MAC_OS:
        return pathlib.Path('~/Library/Preferences').expanduser() / 'gru'
    else:
        base_dir = pathlib.Path(os.getenv('XDG_CONFIG_HOME', '~/.config')).expanduser()
        if not base_dir.exists():
            base_dir.mkdir(parents=True)
        return base_dir / 'gru'


def load_config(config_file: pathlib.Path | str | None = None) -> configparser.ConfigParser:
    config = configparser.ConfigParser(delimiters=['='])
    config.read_file(io.StringIO(defaults))

    config_file = user_config() if config_file is None else pathlib.Path(config_file)
    if config_file.exists():
        config.read(config_file)

    # Valid addons directory?
    if config.get('ESO.addons', 'root').strip():
        return config

    paths = ['Documents/Elder Scrolls Online/live/AddOns', 'Documents/Elder Scrolls Online/pts/AddOns']
    steam_library = '.local/share/Steam/'
    steam_prefix = steam_library + 'steamapps/compatdata/306130/pfx/drive_c/users/steamuser/'
    for check in [*paths, *(steam_prefix + path for path in paths)]:
        addons_dir = user_home() / check
        if addons_dir.exists():
            config.set('ESO.addons', 'root', str(addons_dir.resolve()))
            break
    else:
        # No valid guesses, return as-is
        return config

    # Otherwise update before returning
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
    config_file = user_config() if config_file is None else pathlib.Path(config_file)
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
