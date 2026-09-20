""" Module handling command-line interface """

from __future__ import annotations

import configparser
import contextlib
import warnings
import datetime
import tempfile
import asyncio
import pathlib
import locale
import shutil
import struct
import click
import math
import sys
import typing
import os
import re
import click_repl
import prompt_toolkit.history as prompt_history
from collections.abc import Iterable, Iterator

from .config import load_config, save_config, user_cache, display_config, update_config
from .api import API
from .addon import DisplayAddonProtocol
from .install import Folder
from .patch import addon_diff, addon_patch_file


def get_config_bool(ctx: click.Context, string: str) -> bool:
    section, key = string.format(**ctx.obj).rsplit('.', maxsplit=1)
    return ctx.obj['config'].getboolean(section, key)


class SectionedHelpGroup(click.Group):
    """ Sections commands into help groups """

    _cmd_shortcuts = {'rm': 'remove', 'up': 'update', 'ls': 'list', 's': 'search', 'cc': 'clear-caches', 'df': 'diff'}

    @classmethod
    def _cmd_group(cls, cmd: click.Command) -> str:
        if cmd.name in {'get', 'remove', 'update'}:
            return 'main'
        elif cmd.name in {'help', 'exit'}:
            return 'command line'
        else:
            return 'extra'

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, self._cmd_shortcuts.get(cmd_name, cmd_name))

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        shortcuts = {long: short for short, long in self._cmd_shortcuts.items()}
        for group in ['main', 'extra', 'command line']:
            rows = []
            for subcommand in self.list_commands(ctx):
                cmd = self.get_command(ctx, subcommand)
                if cmd is None or group != self._cmd_group(cmd):
                    continue
                short = shortcuts.get(subcommand)
                rows.append((f'{subcommand} [{short}]' if short else subcommand, cmd.short_help or ''))

            if rows:
                with formatter.section(f'{group.title()} commands'):
                    formatter.write_dl(rows)


class TermDisplay:
    _eso_colored_text = re.compile(r'\|c(?P<color>[0-9a-fA-F]{6})(?P<text>[^|]+)(?:\|r)?')

    def _wrapped(self, *infos: list[str]) -> None:
        pfx = ' ' * self.gutter
        width = shutil.get_terminal_size()[0] - self.gutter
        sep = ' |  '
        for i0, *inext in infos:
            batch = i0
            for term in inext:
                if len(batch) + len(sep) + len(term) < width:
                    batch += sep + term
                    continue
                click.echo(pfx + batch)
                if len(i0) + len(sep) + len(term) < width:
                    batch = ' ' * len(i0) + sep + term
                else:
                    batch = term
            click.echo(pfx + batch)


    @staticmethod
    def _render_eso_text(text: str) -> str:
        return TermDisplay._eso_colored_text.sub(lambda match: click.style(
            match.group('text'),
            fg=struct.unpack('BBB', bytes.fromhex(match.group('color')))
        ), text)

    @staticmethod
    def _strip_eso_text(text: str) -> str:
        """ Plain visible text with ESO color markup removed, for comparisons (not display) """
        return TermDisplay._eso_colored_text.sub(lambda match: match.group('text'), text)

    def _styled_width(self, text: str, width: int) -> str:
        text = self._render_eso_text(text)
        return text + ' ' * max(0, width - len(click.unstyle(text)))

    def _installed(self, item: str, addon: gru.addon.InstalledAddon) -> None:
        """ Show addon info from the API endpoint """
        click.echo()
        update = '' if not addon.can_update else f'{click.style("update available", bold=True)} - '
        infos = addon.infos
        parent = f', part of {infos.title}' if len(infos.folders) > 1 else f', listed online as {infos.title}' if addon.title.strip() != infos.title.strip() else ''
        click.echo(f'{item:{self.gutter}}{self._render_eso_text(addon.title)} [{update}installed{parent}]')
        # TODO: Based on verbosity level, only click.echo a number of those:
        self._wrapped([
            f'Author: {self._styled_width(addon.author, 25)}',
            f'Version: {addon.version:10}',
            f'Updated: {addon.infos.metadata["date"].strftime("%x"):10}',
        ], [
            f'Category: {" > ".join(self.cat_name_hierarchy(addon.infos.metadata["category"])):23}',
            f'Favorites: {addon.infos.metadata["favorites"]:<8n}',
            f'Downloads: {addon.infos.metadata["downloads"]:<n} [{addon.infos.metadata["monthly"]:<n} / Month]',
        ], [
            f'Directory: {self.rel_path(addon.folder)}',
        ])
        click.echo(' ' * self.gutter + addon.infos.metadata['link'])


    def _addon_info(self, item: int, addon: gru.addon.AddonInfo) -> None:
        """ Show addon info from the API endpoint """
        # TODO: Based on verbosity level, only click.echo a number of those:
        title = f'{item:{self.gutter}}{addon.title}'
        if addon.folders:
            dirs = ", ".join(str(self.rel_path(dir_)) for dir_ in addon.folders)
            title += ' - ' + click.style(f'installed at: {dirs}', bold=True)
        click.echo()
        click.echo(title)
        self._wrapped([
            f'Author: {addon.author:25}',
            f'Version: {addon.version:10}',
            f'Updated: {addon.metadata["date"].strftime("%x"):10}',
        ], [
            f'Category: {" > ".join(self.cat_name_hierarchy(addon.metadata["category"])):23}',
            f'Favorites: {addon.metadata["favorites"]:<8n}',
            f'Downloads: {addon.metadata["downloads"]:<n} [{addon.metadata["monthly"]:<n} / Month]',
        ])
        click.echo(' ' * self.gutter + addon.metadata['link'])


    def _folder(self, item: int, folder: gru.addon.InstalledAddon) -> None:
        """ Show addon info from a local folder that was not matched with the API endpoint """
        click.echo()
        # Based on verbosity level, only click.echo a number of those:
        parent_text = f', bundled inside {parent.infos.title}' if (parent := folder.parent) and parent.id else ''
        click.echo(f'{item:{self.gutter}}{self._render_eso_text(folder.title)}  [installed{parent_text}]')
        infos = [
            f'Author: {self._styled_width(folder.author, 20)}',
            f'Version: {folder.version:10}',
        ], [
            f'Directory: {self.rel_path(folder.folder)}',
        ]
        if 'Description' in folder.metadata:
            infos.append(f'Description: {folder.metadata["Description"]}')
        self._wrapped(*infos)
        if folder.parent is not None:
            click.echo(' ' * self.gutter + 'NB: this add-on could not be matched online and may be deprecated')


    def __init__(self, results: list[gru.addon.AddonInfo | gru.addon.InstalledAddon], num_from: int = 0) -> None:
        """ Show a list of addons """
        self.gutter = 2 + math.ceil(math.log(num_from + len(results), 10))
        ctx = click.get_current_context()
        self.cat_name_hierarchy = ctx.obj['api'].cat_name_hierarchy
        self.rel_path = lambda path, root=ctx.obj['local'].root: path.relative_to(root)

        for n, addon in enumerate(results, num_from + 1):
            item = f'{n:{self.gutter - 2}}: ' if len(results) > 1 else ''
            if addon.is_local and addon.id is not None:
                self._installed(item, addon)
            elif addon.is_local:
                self._folder(item, addon)
            else:
                self._addon_info(item, addon)

        click.echo()


def _confirm(query: str) -> bool:
    answer = click.confirm(query, prompt_suffix=':\n>> ')
    click.echo()
    return answer


def _prompt_addon(results: Iterable[gru.addon.DisplayAddonProtocol], confirm_prompt: str | None = None, show_batch: int | None = None) -> gru.addon.DisplayAddonProtocol | None:
    """ Pick an addon from a list of addons """
    results = list(results)
    if not results:
        click.echo('No addon found')
        return None

    if show_batch is None:
        show_batch = max(shutil.get_terminal_size()[1] // 5 - 1, 4)

    click.echo(' '.join([
        'No exact matches.',
        f'{len(results)} possible results:' if len(results) > 1 else 'Only approximate result:'
    ]))

    TermDisplay(results[:show_batch])

    if len(results) == 1:
        return results[0] if _confirm(confirm_prompt) else None

    for shown in range(show_batch, len(results), show_batch):
        answer = click.prompt(f'Select (1-{shown}, 0 cancels, empty continues)', prompt_suffix=':\n>> ', default=-1,
                              type=click.IntRange(-1, shown + 1), show_default=False)
        if answer >= 0:
            break

        TermDisplay(results[shown:shown+show_batch], num_from=shown)
    else:
        answer = click.prompt(f'Select (1-{len(results)}, 0 cancels)', prompt_suffix=':\n>> ',
                              type=click.IntRange(0, len(results) + 1))

    click.echo()
    try:
        answer = int(answer)
    except ValueError:
        return None

    if 1 <= answer <= len(results):
        return results[answer - 1]


def _find_installed(local: gru.install.Folder, api: gru.api.API, term: str, confirm_prompt: str | None = None) -> gru.addon.InstalledAddon | None:
    if term:
        addon = local.find(term.lower(), api)
    else:
        addon = local.installed

    if not addon:
        click.echo('No corresponding addon found.')
        return

    if isinstance(addon, Iterable):
        addon = _prompt_addon(addon, confirm_prompt)
    if not addon:
        click.echo('Nothing do to.')
        return None

    return addon


def _progress(size: int, message: str) -> gru.install.ProgressProtocol:
    return click.progressbar(length=size, label=message, width=0)


def add_repl_commands(group: click.Group) -> None:
    """ Adds commands that are only useful in REPL mode to a click group """
    @group.command('help', help='Print CLI help')
    def print_help() -> None:
        with click.Context(group) as ctx:
            click.echo(group.get_help(ctx))

    @group.command('exit', help='Exit CLI')
    def exit_repl() -> typing.NoReturn:
        raise click_repl.ExitReplException()


def resolve_addons_root(config: configparser.ConfigParser, game: str, config_file: pathlib.Path | None) -> None:
    """ Ensure `config` has a valid addons root, prompting interactively (and saving) if missing """
    root = config.get(f'{game}.addons', 'root')
    if not root or not pathlib.Path(root).exists():
        click.echo(f'{game} addons directory not found!')
        root = click.prompt('Path to addons directory', prompt_suffix=':\n>> ',
                            type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
        config.set(f'{game}.addons', 'root', str(root.resolve()))
        save_config(config, config_file)


def build_app(game: str, config_file: pathlib.Path | None) -> tuple[configparser.ConfigParser, API, Folder]:
    """ Load config, ensure a valid addons root, and build a live API + scanned Folder.
    The single seam a test needs to monkeypatch to drive commands without real network/disk. """
    config = load_config(config_file)
    resolve_addons_root(config, game, config_file)
    api = API.live(config)
    local = Folder(game, config)
    local.scan(api)
    return config, api, local


@click.group(cls=SectionedHelpGroup, invoke_without_command=True, context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--config', 'config_file', help='path to config file',
              type=click.Path(dir_okay=False, writable=True, path_type=pathlib.Path), default=None)
@click.option('--game', 'game', help='Choice of game', hidden=True,
              type=click.Choice(['ESO']), default='ESO')
@click.option('--no-color', 'no_color', is_flag=True, default=False, help='Disable colored output')
@click.pass_context
def main(ctx: click.Context, game: str = 'ESO', config_file: pathlib.Path | None = None, no_color: bool = False) -> None:
    locale.setlocale(locale.LC_ALL, '')

    # click.echo() defaults to auto-detecting whether to strip ANSI styling based on
    # whether the stream looks like a tty. Setting ctx.color makes every echo() call
    # (none of which pass color= explicitly) respect this instead, so piping/redirecting
    # gru's output still keeps addon title styling unless --no-color is passed.
    ctx.color = not no_color

    ctx.ensure_object(dict)
    ctx.obj['warnings'] = ctx.with_resource(warnings.catch_warnings(record=True, category=UserWarning))

    config, api, local = build_app(game, config_file)
    ctx.obj['config'] = config
    ctx.obj['game'] = game
    ctx.obj['config_file'] = config_file
    ctx.obj['api'] = api
    ctx.obj['local'] = local

    if ctx.invoked_subcommand is None:
        add_repl_commands(main)
        click_repl.repl(ctx, prompt_kwargs={
            'history': prompt_history.FileHistory(user_cache('history')),
        })


@main.group()
@click.pass_context
def config(ctx: click.Context) -> None:
    pass


@config.command('get')
@click.pass_context
@click.argument('entry', required=False)
def config_get(ctx: click.Context, entry: str | None = None) -> None:
    config = display_config(ctx.obj['config'], ctx.obj['game'])
    if not entry:
        for key, value in config.items():
            click.echo(f'{key} = {value!r}')

    elif '.' not in entry:
        items = [(key, value) for key, value in config.items() if key.split('.', 1)[0] == entry]
        if not items:
            click.echo(f'Error: section {entry} not understood')
            return
        for key, value in items:
            click.echo(f'{key} = {value!r}')

    else:
        try:
            value = config[entry]
        except KeyError:
            click.echo('Error: entry {0[1]} not found in {0[0]}'.format(entry.split('.', 1)))
        else:
            click.echo(value)


@config.command('set')
@click.pass_context
@click.argument('entry')
@click.argument('value')
def config_set(ctx: click.Context, entry: str, value: str) -> None:
    try:
        section_name, key = entry.split('.', 1)
    except ValueError:
        click.echo(f'Entry must be formatted as <section>.<key>')
        return

    try:
        section = update_config(ctx.obj['config'], ctx.obj['game'], {entry: value})
    except KeyError:
        click.echo(f'Error: section {section_name} not recognized')
    except configparser.NoOptionError:
        click.echo(f'Error: entry {key} not found in {section_name} options')
    except ValueError:
        click.echo(f'Error: value must be "on" or "off" for boolean values only')
    else:
        save_config(ctx.obj['config'], ctx.obj['config_file'])


def show_warnings(ctx: click.Context) -> None:
    if not ctx.obj['warnings']:
        return

    click.echo(f'\n{len(ctx.obj["warnings"])} warning(s):', err=True)
    while ctx.obj['warnings']:
        click.echo(f'- {ctx.obj["warnings"].pop(0).message}', err=True)


@main.result_callback()
@click.pass_context
def process_result(ctx: click.Context, result: typing.Any, game: str, config_file: str | None, no_color: bool) -> None:
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False, nargs=-1)
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--yes', '-y', 'batch', is_flag=True, default=False)
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.pass_context
def get(ctx: click.Context, addon: list[str], auto_deps: bool = True, opt: bool | None = None, batch: bool = False) -> None:
    """ Find, download, and install an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']
    game = ctx.obj["game"]

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    addon_list = addon or [click.prompt(f'Addon to install', prompt_suffix=':\n>> ')]

    for addon_spec in addon_list:

        addon = api.find(addon_spec, local)
        if not addon:
            click.echo('No corresponding addon found')
            click.echo()
            if batch:
                warnings.warn(f'Skipped install of unmatched addon {addon_spec}')
            continue

        if not batch:
            addon = _prompt_addon(addon, 'Confirm installation?')
        elif len(addon) == 1:
            addon = addon[0]
        else:
            click.echo(f'Ambiguous addon specificiation {addon_spec}, skipping')
            warnings.warn(f'Skipped install of ambiguous addon {addon_spec}')
            addon = None

        if not addon:
            continue

        # Try to reuse an existing install dir
        install_path = None
        try:
            installed_addon = next(ad for ad in local.id(addon.id) if ad.parent is None)
            if batch or _confirm(f'Addon found at {installed_addon.folder}, update?'):
                install_path = installed_addon.folder
            else:
                click.echo('Nothing to do.')
                continue
        except StopIteration:
            pass

        try:
            result = local.install(addon, api, _progress, path=install_path, deps=auto_deps, opt=opt)
        except KeyError as exc:
            click.echo(f'Failed installing {addon_spec}: {type(exc).__name__} {exc}')
            if not batch:
                break

        if result is None:
            click.echo(f'Done installing {TermDisplay._render_eso_text(addon.title)}')
        else:
            click.echo(f'Done installing {TermDisplay._render_eso_text(addon.title)} and {result} dependence(s)')
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False)
@click.option('--clean-deps/--no-clean-deps', default=False, help='Clean up unused dependences')
@click.option('--opt/--no-opt', default=None, help='Keep optional dependences')
@click.pass_context
def remove(ctx: click.Context, addon: str | None, clean_deps: bool = False, opt: bool | None = None) -> None:
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']
    game = ctx.obj["game"]

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    # TODO: remove one of several matches e.g. lib media provider?
    # prefer
    # - unmatched
    # - top-level / non-subaddon ?
    # TODO: handle removal of several linked local addons (same online addon)
    installed_addon = _find_installed(local, api, addon, 'Confirm removal?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    nremoved = local.remove(installed_addon, deps=clean_deps, opt=opt)

    if not clean_deps:
        click.echo(f'Removed addon {TermDisplay._render_eso_text(installed_addon.title)}.')
    else:
        click.echo(f'Removed addon {TermDisplay._render_eso_text(installed_addon.title)} and {nremoved} unused dependence(s).')
    show_warnings(ctx)


@main.command()
@click.option('--auto-deps/--no-auto-deps', default=True, help='Automatically install new/missing dependences')
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.option('--patch/--no-patch', default=None, help='Automatically re-apply patches')
@click.pass_context
def update(ctx: click.Context, auto_deps: bool, opt: bool | None, patch: bool | None) -> None:
    """ Find out-of-date and missing addons and install them """
    api = ctx.obj['api']
    local = ctx.obj['local']
    game = ctx.obj["game"]

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')
    if patch is None:
        patch = get_config_bool(ctx, '{game}.addons.patch_updates')

    updates, added = local.update(api, _progress, opt=opt, deps=auto_deps, patch=patch)

    if updates + added == 0:
        click.echo('Nothing to do')
    elif auto_deps:
        click.echo(f'Updated {updates} addon(s) and installed {added} dependence(s)')
    else:
        click.echo(f'Updated {updates} addon(s)')
    show_warnings(ctx)


@main.command(help='Remove unused dependences')
@click.option('--opt/--no-opt', default=True, help='Keep optional dependences')
@click.pass_context
def cleanup(ctx: click.Context, opt: bool | None = None) -> None:
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']
    game = ctx.obj["game"]

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    nremoved = local.remove_unused_deps(opt=opt)

    click.echo(f'Removed {nremoved} unused dependence(s).')
    show_warnings(ctx)


@main.command()
@click.pass_context
def check_api_release(ctx: click.Context, hidden: bool = True) -> None:
    alpha_ok, live_ok = False, False
    try:
        if ctx.obj['api'].globalconf['API']['Version'] == 'LIVE':
            live_ok = True
        else:
            click.echo(f'Error: Version {ctx.obj["config"].get("api", "version")} has been retired')
    except Exception as err:
        click.echo(f"Can't check stable API status: {err}")

    try:
        alpha = API.alpha(ctx.obj['config'])
        if alpha.globalconf['API']['Version'] == 'ALPHA':
            alpha_ok = True
        else:
            click.echo(f'Version {alpha.version} seems to have come out of alpha')
    except Exception as err:
        click.echo(f"Can't check alpha API status: {err}")

    if live_ok and alpha_ok:
        # Don’t just exit without
        click.echo('Status of stable and alpha APIs as expected.')


@main.command()
@click.argument('term', required=False)
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
@click.pass_context
def search(ctx: click.Context, term: str | None, max_: int = 10) -> None:
    if term is None:
        term = click.prompt(f'Term to search for', prompt_suffix=':\n>> ')

    api = ctx.obj['api']
    search = api.search(term, maxlen=max_)
    if search:
        click.echo(f'{len(search)} results:')
        _display(ctx, search)
    else:
        click.echo('No matches.')


@main.command('list', help='list installed add-ons')
@click.pass_context
def list_(ctx: click.Context) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    click.echo(f'Found {len(local.installed)} addon(s):')
    TermDisplay(local.installed)
    show_warnings(ctx)


@main.command(help='export installed add-ons')
@click.option('--recurse', '-r', help='Recurse into subdirectories (will show private libraries)', default=False)
@click.pass_context
def export(ctx: click.Context, recurse: bool = False) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    export_path = local.root / '.gru' / 'addons.txt'
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with export_path.open('w') as out:
        for addon in local.installed:
            if recurse or addon.parent is None:
                print(f'{addon.dir} = {addon.version}', file=out)

    click.echo(f'All {len(local.installed)} addon(s) exported to:\n{export_path.resolve()}')
    show_warnings(ctx)


@main.command(help='List missing dependences')
@click.option('--opt/--no-opt', help='include optional dependences')
@click.pass_context
def miss(ctx: click.Context, opt: bool) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']
    missing = local.missing_deps(local.installed, opt=opt)

    if not missing:
        click.echo('No missing dependences!')
        return

    click.echo(f'{len(missing)} missing dependences:')
    found, not_found = [], []
    for dep in missing:
        try:
            found.append(api.dir(dep.dir))
        except ValueError:
            not_found.append(dep)
    _display(ctx, found + not_found)

    if found:
        click.echo(f'Run update to fetch resolved missing dependences')
    show_warnings(ctx)


@contextlib.contextmanager
def unmodified_addon(api: gru.api.API, local: gru.install.Folder, addon: gru.addon.AddonInfo, url: str = None) -> Iterator[gru.addon.InstalledAddon]:
    # Code to acquire resource, e.g.:
    with tempfile.TemporaryDirectory() as tempdir:
        ref_local = local.alt_location(pathlib.Path(tempdir))
        ref_addon = addon.alt_location(ref_local.root)
        # Be sure to compare to installed version not up-to-date upstream
        ref_local.unpack(ref_addon, api, url_override=url)

        if ref_addon.version != addon.version:
            raise ValueError('Downloaded addon does not have same version as installed addon!')

        yield ref_addon


@main.command(help='Save the diff between current addon and upstream as a patch')
@click.argument('addon', required=False)
@click.option('--url', help='Download url for installed version', required=False)
@click.pass_context
def diff(ctx: click.Context, addon: str | None, url: str | None = None) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']

    addon = _find_installed(local, api, addon, 'Confirm diff saving?')
    if addon is None:
        show_warnings(ctx)
        return

    if not url and addon.infos.version != addon.version:
        click.echo(f'Addon is out of date!  Can not fetch unmodified source automatically.')
        click.echo()
        url = click.prompt(f'Please manually specify {addon.version} download url',
                              prompt_suffix=':\n>> ', type=str)

    result_path = local.root / '.gru' / f'{addon.dir}.patch'
    result_path.parent.mkdir(parents=True, exist_ok=True)

    with local.unmodified_addon(addon.infos, api, url=url) as ref_addon, result_path.open('w') as out:
        nfiles = addon_diff(ref_addon, addon, out=out)

    if nfiles > 0:
        click.echo(f'Changes saved under:\n{result_path.resolve()}')
    else:
        result_path.unlink()
        click.echo(f'No changes to be saved.')
    show_warnings(ctx)


@main.command(help='Import a patch of changes for an addon')
@click.argument('addon', required=False)
@click.argument('patch', type=click.Path(dir_okay=False, path_type=pathlib.Path), required=False)
@click.pass_context
def patch(ctx: click.Context, addon: str | None, patch: pathlib.Path) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']

    installed_addon = _find_installed(local, api, addon, 'Confirm patch target?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    if patch is None:
        patch = local.root / '.gru' / f'{installed_addon.dir}.patch'
        if not patch.exists():
            click.echo(f'No saved changes to be re-applied.')
            return

    done, total = addon_patch_file(installed_addon, patch)
    if not total:
        click.echo(f'No changes to be apply in patch.')
    elif done == total:
        click.echo(f'Applied patch successfully.')
    else:
        click.echo(f'Applied {done} / {total} hunks in patch.')


@main.command()
@click.pass_context
def clear_caches(ctx: click.Context) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']
    api.reset()
    local.scan(api)

    click.echo('Caches cleared.')
    show_warnings(ctx)
