""" The game's own per-character add-on enable list: <Documents>/Elder Scrolls Online/<live|pts>/AddOnSettings.txt

Header lines are `#Version <api>`, `#AcknowledgedOutOfDateAddonsVersion <api>` and `#AddOnsEnabled <0|1>`. Every
other `#<server>-<character>` line starts a section (`#Default` for the settings new characters start with), holding
`<AddOn folder name> <0|1>` lines. An add-on without a line is enabled. """

import pathlib
import typing
from dataclasses import dataclass, field

FILE_NAME = 'AddOnSettings.txt'
MAX_BYTES = 2_000_000
_INT_HEADERS = {'Version', 'AcknowledgedOutOfDateAddonsVersion', 'AddOnsEnabled'}


class Character(typing.NamedTuple):
    server: str
    name: str

    def __str__(self) -> str:
        return f'{self.name} ({self.server})' if self.server else self.name


@dataclass(frozen=True)
class DisabledInfo:
    """ Where an add-on is disabled in game. `everywhere`: disabled in `#Default`, which stands for all characters. """
    everywhere: bool
    characters: list[Character]


@dataclass(frozen=True)
class GameSettings:
    path: pathlib.Path
    api_version: int | None = None
    acknowledged_api_version: int | None = None
    addons_enabled: bool = True
    #: `#Default` section, by casefolded add-on folder name
    default: dict[str, bool] = field(default_factory=dict)
    characters: dict[Character, dict[str, bool]] = field(default_factory=dict)

    def disabled_by_default(self, dir_: str) -> bool:
        return self.default.get(dir_.casefold()) is False

    def disabled_on(self, dir_: str) -> list[Character]:
        key = dir_.casefold()
        return [character for character, entries in self.characters.items() if entries.get(key) is False]

    def disabled(self, dir_: str) -> DisabledInfo | None:
        everywhere, characters = self.disabled_by_default(dir_), self.disabled_on(dir_)
        return DisabledInfo(everywhere, characters) if everywhere or characters else None


def parse(text: str, path: pathlib.Path | None = None) -> GameSettings:
    """ Tolerant: unknown or malformed lines are skipped. """
    headers: dict[str, int] = {}
    default: dict[str, bool] = {}
    characters: dict[Character, dict[str, bool]] = {}
    section: dict[str, bool] | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            key, _, value = line[1:].partition(' ')
            if key in _INT_HEADERS and value.strip().isdigit():
                headers[key] = int(value)
                continue
            label = line[1:].strip()
            if label == 'Default':
                section = default
            else:
                server, _, name = label.partition('-')
                character = Character(server, name) if name else Character('', server)
                section = characters.setdefault(character, {})
            continue
        name, _, state = line.rpartition(' ')
        if section is not None and name and state in {'0', '1'}:
            section[name.strip().casefold()] = state == '1'

    return GameSettings(path or pathlib.Path(FILE_NAME), headers.get('Version'),
                        headers.get('AcknowledgedOutOfDateAddonsVersion'), headers.get('AddOnsEnabled', 1) != 0,
                        default, characters)


def find(directory: pathlib.Path) -> pathlib.Path | None:
    """ The settings file of the target whose AddOns folder is a child of `directory`. """
    exact = directory / FILE_NAME
    if exact.is_file():
        return exact
    try:
        return next((path for path in directory.iterdir()
                     if path.name.casefold() == FILE_NAME.casefold() and path.is_file()), None)
    except OSError:
        return None


def load(directory: pathlib.Path) -> GameSettings | None:
    """ Parse the settings file found in `directory`; None if there is none or it can't be read. """
    path = find(directory)
    if path is None:
        return None
    try:
        with path.open('rb') as f:
            data = f.read(MAX_BYTES)
    except OSError:
        return None
    return parse(data.decode('utf-8-sig', errors='replace'), path)
