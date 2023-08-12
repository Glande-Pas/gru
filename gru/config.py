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
endpoint = https://api.mmoui.com/v{version}/
version = 3

globalconf = globalconfig.json
gameconf = game/{game}/gameconfig.json
catlist = game/{game}/categorylist.json
filelist = game/{game}/filelist.json
game = ESO

info = https://www.esoui.com/downloads/info{id}.html
download = https://cdn.esoui.com/downloads/file{id}/

[user]
path =
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

def load():
    parser = configparser.Configparser(delimiters=['='])
    parser.read_file(io.StringIO(defaults))
    print(parser.items())
