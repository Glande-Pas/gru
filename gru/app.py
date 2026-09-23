""" Application layer: state persistence and orchestration that spans gru.api/gru.install/config
but is independent of any particular front-end -- a CLI or a future GUI both hook in here.
gru.cli (or a GUI module in its place) is expected to stay a thin layer of command dispatch,
prompting, and output formatting on top of this. """

from __future__ import annotations

import collections
import configparser
import csv
import datetime
import pathlib

from .api import API
from .config import user_config
from .install import Folder

NOT_INSTALLED = 'none'  # sentinel: version/NOT_INSTALLED means installed, NOT_INSTALLED/version means uninstalled


def addons_root_configured(config: configparser.ConfigParser, game: str) -> bool:
    """ Whether `config` already points at an existing addons directory. """
    root = config.get(f'{game}.addons', 'root')
    return bool(root) and pathlib.Path(root).exists()


def build_app(game: str, config: configparser.ConfigParser) -> tuple[API, Folder]:
    """ Build a live API + freshly-scanned Folder from an already-valid `config` (see
    addons_root_configured()) -- the non-interactive core a front-end calls once it has a
    usable config; resolving/prompting for a missing addons root is that front-end's job. """
    api = API.live(config)
    local = Folder(game, config)
    local.scan(api)
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


def log_changes(local: Folder, config: configparser.ConfigParser,
                before: dict[pathlib.Path, tuple[str, str, str]]) -> None:
    """ Append one changes.csv row per folder whose version differs between `before` and now. """
    after = local.snapshot()
    max_lines = config.getint(f'{local.game}.addons', 'log_lines')
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    path = user_config(local.game, 'changes.csv')
    for folder in before.keys() | after.keys():
        old_dir, old_version, old_link = before.get(folder, ('', NOT_INSTALLED, ''))
        new_dir, new_version, new_link = after.get(folder, ('', NOT_INSTALLED, ''))
        if old_version == new_version:
            continue
        append_change_log(path, [new_dir or old_dir, new_version, new_link or old_link, now, old_version], max_lines)
