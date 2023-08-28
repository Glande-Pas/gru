""" Module handling user configuration """
import configparser
import pathlib
import sys
import os
import io

IS_POSIX = os.name == 'posix'
IS_MAC_OS = sys.platform == 'darwin'
IS_WINDOWS = os.name == 'nt'

defaults = '''
[api]
endpoint = https://api.mmoui.com/v{version}/{path}
version = 3

[ESOUIv3.paths]
globalconf = globalconfig.json
gameconf = game/ESO/gameconfig.json
catlist = game/ESO/categorylist.json
filelist = game/ESO/filelist.json

[ESO.links]
info = https://www.esoui.com/downloads/info{id}.html
download = https://cdn.esoui.com/downloads/file{id}/

[ESO.addons]
# Path to addons root directory
root =
# Whether to include optional dependences by default
optional = off
# Sort equal matches in search according to one of: downloads, monthly, favorites
sortkey = downloads

[app]
open_in_browser = off
'''

def user_home():
    if (userhome := os.environ.get('HOME')) is not None:
        return pathlib.Path(userhome)
    elif (userhome := os.environ.get('USERPROFILE')) is not None:
        return pathlib.Path(userhome)
    elif (userhome := os.environ.get('HOMEPATH')) is not None:
        if (userdrive := os.environ.get('HOMEDRIVE')) is not None:
            return pathlib.Path(os.environ['HOMEDRIVE']) / userhome
        else:
            return pathlib.Path(userhome)

def user_cache(*args):
    """ Returns the appropriate path to the cache file in the user app dirs.

    Returns:
        :class:`~pathlib.Path`: path to the cache file or directory.
    """
    if IS_WINDOWS:
        base_dir = pathlib.Path(os.getenv('LOCALAPPDATA', os.getenv('APPDATA')))
    elif IS_MAC_OS:
        # NB. for local ~/Library/Logs
        base_dir = pathlib.Path('~/Library/Caches').expanduser()
    else:
        base_dir = pathlib.Path(os.getenv('XDG_CACHE_HOME', '~/.cache')).expanduser()

    base_dir /= 'gru'
    if not base_dir.exists():
        base_dir.mkdir(parents=True)

    return base_dir.joinpath(*args)

def user_config():
    """ Returns the path to the configuration file in the user config directory

    Returns:
        :class:`~pathlib.Path`: path to the user configuration file.
    """
    if IS_WINDOWS:
        return pathlib.Path(os.getenv('APPDATA')) / 'gru.ini'
    elif IS_MAC_OS:
        return pathlib.Path('~/Library/Preferences').expanduser() / 'gru'
    else:
        base_dir = pathlib.Path(os.getenv('XDG_CONFIG_HOME', '~/.config')).expanduser()
        if not base_dir.exists():
            base_dir.mkdir(parents=True)
        return base_dir / 'gru'

def load_config(config_file=None):
    config = configparser.ConfigParser(delimiters=['='])
    config.read_file(io.StringIO(defaults))

    config_file = user_config() if config_file is None else pathlib.Path(config_file)
    if config_file.exists():
        config.read(config_file)

    # Valid addons directory?
    if config.get('ESO.addons', 'root').strip():
        return config

    for check in ['Documents/Elder Scrolls Online/live/AddOns', 'Documents/Elder Scrolls Online/pts/AddOns']:
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


def save_config(config, config_file=None):
    config_file = user_config() if config_file is None else pathlib.Path(config_file)
    with open(config_file, 'w') as f:
        config.write(f)
