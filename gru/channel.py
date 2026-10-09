# Copyright Glande-Pas and contributors
# Licensed under the EUPL, see LICENSE.md

""" Which distribution channel this copy of gru was installed through """

from __future__ import annotations

import enum
import functools
import importlib.metadata
import os
import pathlib

DIST_NAME = 'Gru-ESO'
#: Packagers can pin the channel instead of relying on detection
ENV_OVERRIDE = 'GRU_INSTALL_CHANNEL'

#: INSTALLER values of tools that install from a Python package index
_INDEX_INSTALLERS = {'pip', 'uv', 'poetry', 'pdm', 'hatch', 'rye'}


class Channel(str, enum.Enum):
    FLATPAK = 'flatpak'
    MSSTORE = 'msstore'
    #: Installed from a Python package index (pip, pipx, uv, ...)
    PYPI = 'pypi'
    #: Installed by a system package manager
    DISTRO = 'distro'
    #: Source checkout, editable or direct-URL install, or anything not recognized
    GIT = 'git'

    def __str__(self) -> str:
        return self.value


def _is_flatpak() -> bool:
    return pathlib.Path('/.flatpak-info').exists() or bool(os.environ.get('FLATPAK_ID'))


def _is_msstore() -> bool:
    """ Packaged apps live under WindowsApps. (A Store-installed *Python* running a pip-installed gru does not:
    its packages are in the user profile.) """
    return os.name == 'nt' and any(part.lower() == 'windowsapps' for part in pathlib.Path(__file__).resolve().parts)


def _from_metadata() -> Channel:
    try:
        dist = importlib.metadata.distribution(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return Channel.GIT  # running from a source tree
    if dist.read_text('direct_url.json') is not None:
        return Channel.GIT  # editable, VCS, local path or archive URL
    installer = (dist.read_text('INSTALLER') or '').strip().lower()
    if not installer:
        return Channel.GIT
    return Channel.PYPI if installer in _INDEX_INSTALLERS else Channel.DISTRO


@functools.lru_cache(maxsize=1)
def detect() -> Channel:
    if override := os.environ.get(ENV_OVERRIDE, '').strip().lower():
        try:
            return Channel(override)
        except ValueError:
            pass
    if _is_flatpak():
        return Channel.FLATPAK
    if _is_msstore():
        return Channel.MSSTORE
    try:
        return _from_metadata()
    except Exception:  # unreadable metadata must not break the CLI
        return Channel.GIT
