""" Module handling command-line interface """

from __future__ import annotations

import configparser
import warnings
import datetime
import pathlib
import locale
import logging
import shutil
import struct
import click
import math
import typing
import sys
import click_repl
import prompt_toolkit.history as prompt_history
import urllib.parse
import requests
import zipfile
from collections.abc import Iterable

from .config import load_config, save_config, user_cache, user_config, display_config, update_config
from .api import API, AmbiguousDirectory
from .addon import AddonInfo, InstalledAddon, ESO_COLORED_TEXT
from .install import Folder
from .patch import addon_diff, addon_patch_file, PatchError
from . import app as gru_app

if typing.TYPE_CHECKING:
    import gru.addon
    import gru.api
    import gru.install

AddonT = typing.TypeVar('AddonT', bound='gru.addon.AddonInfo | gru.addon.InstalledAddon')


def get_config_bool(ctx: click.Context, string: str) -> bool:
    section, key = string.format(**ctx.obj).rsplit('.', maxsplit=1)
    return ctx.obj['config'].getboolean(section, key)


class SectionedHelpGroup(click.Group):
    """ Sections commands into help groups """

    _cmd_shortcuts = {'rm': 'remove', 'up': 'update', 'ls': 'list', 'se': 'search', 'cc': 'clear-caches',
                      'al': 'add-lock', 'rl': 'remove-lock', 'll': 'list-locks', 'rv': 'review', 'm': 'match',
                      'in': 'install'}

    @classmethod
    def _cmd_group(cls, cmd: click.Command) -> str:
        if cmd.name in {'get', 'remove', 'update'}:
            return 'main'
        elif cmd.name in {'add-lock', 'remove-lock', 'list-locks'}:
            return 'version locking'
        elif cmd.name in {'list', 'search', 'review', 'miss', 'export', 'check-api-release', 'cleanup', 'match'}:
            return 'more addon'
        elif cmd.name in {'diff', 'patch'}:
            return 'patches'
        elif cmd.name in {'clear-caches', 'config', 'help', 'exit', 'about'}:
            return 'utility'
        else:
            return 'extra'

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, self._cmd_shortcuts.get(cmd_name, cmd_name))

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        shortcuts = {long: short for short, long in self._cmd_shortcuts.items()}
        for group in ['main', 'more addon', 'version locking', 'patches', 'utility', 'extra']:
            rows = []
            for subcommand in self.list_commands(ctx):
                cmd = self.get_command(ctx, subcommand)
                if cmd is None or cmd.hidden or group != self._cmd_group(cmd):
                    continue
                short = shortcuts.get(subcommand)
                rows.append((f'{subcommand} [{short}]' if short else subcommand, cmd.get_short_help_str()))

            if rows:
                with formatter.section(f'{group.title()} commands'):
                    formatter.write_dl(rows)


def _unmatched_reason(api: API, dir_: str) -> str:
    """ Why a local addon folder has no online match, re-derived fresh from the API:
    'ambiguous' (several online listings share this dir -- see `gru match`) or 'no listing'. """
    try:
        api.dir(dir_)
    except AmbiguousDirectory:
        return 'ambiguous'
    except FileNotFoundError:
        return 'no listing'
    else:
        return 'no listing'  # shouldn't happen: only reached when unmatched


class TermDisplay:
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
        return ESO_COLORED_TEXT.sub(lambda match: click.style(
            match.group('text'),
            fg=struct.unpack('BBB', bytes.fromhex(match.group('color')))
        ), text)

    def _styled_width(self, text: str, width: int) -> str:
        text = self._render_eso_text(text)
        return text + ' ' * max(0, width - len(click.unstyle(text)))

    def _installed(self, item: str, addon: gru.addon.InstalledAddon) -> None:
        """ Show addon info from the API endpoint """
        click.echo()
        assert addon.infos is not None  # guaranteed by the is_local/id dispatch in __init__
        moot = addon.parent is not None and addon.is_superseded  # 'superseded copy' tag says enough
        if addon.can_update and not addon.locked and not moot:
            status = click.style('update available', fg='yellow', bold=True)
        else:
            status = click.style('up to date', fg='green')
        infos = addon.infos
        tags = []
        if addon.locked:
            tags.append(click.style('version locked', fg='cyan'))
        if addon.parent is not None:
            tags.append(f'bundled inside {addon.parent.title}')
        if len(infos.folders) > 1:
            rank = addon.version_rank
            fg = {'active': 'green', 'superseded': 'yellow'}.get(rank)
            tags.append(click.style(f'{rank} copy', fg=fg) if rank else 'copy')
        elif addon.title.strip() != infos.title.strip():
            tags.append(f'listed online as {infos.title}')
        suffix = ''.join(f', {tag}' for tag in tags)
        click.echo(f'{item:{self.gutter}}{self._render_eso_text(addon.title)} [{status}{suffix}]')
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

    def _addon_info(self, item: str, addon: gru.addon.AddonInfo) -> None:
        """ Show addon info from the API endpoint """
        # TODO: Based on verbosity level, only click.echo a number of those:
        title = f'{item:{self.gutter}}{addon.title}'
        if addon.folders:
            dirs = ', '.join(str(self.rel_path(dir_)) for dir_ in addon.folders)
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

    def _unmatched_status(self, folder: gru.addon.InstalledAddon) -> str:
        """ Why this folder has no online match, re-derived fresh from the API. """
        if _unmatched_reason(self.api, folder.dir) == 'ambiguous':
            return click.style('ambiguous - see gru match', fg='red', bold=True)
        return click.style('no listing', fg='yellow')

    def _folder(self, item: str, folder: gru.addon.InstalledAddon) -> None:
        """ Show addon info from a local folder that was not matched with the API endpoint """
        click.echo()
        # Based on verbosity level, only click.echo a number of those:
        parent = folder.parent
        tags = []
        if folder.locked:
            tags.append(click.style('version locked', fg='cyan'))
        if parent and parent.id and parent.infos is not None:
            tags.append(f'bundled inside {parent.infos.title}')
        tags.append(self._unmatched_status(folder))
        suffix = ''.join(f', {tag}' for tag in tags)
        click.echo(f'{item:{self.gutter}}{self._render_eso_text(folder.title)}  [installed{suffix}]')
        infos = [[
            f'Author: {self._styled_width(folder.author, 20)}',
            f'Version: {folder.version:10}',
        ], [
            f'Directory: {self.rel_path(folder.folder)}',
        ]]
        if 'description' in folder.metadata:
            infos.append([f'Description: {folder.metadata["description"]}'])
        self._wrapped(*infos)

    def __init__(self, results: list[gru.addon.AddonInfo | gru.addon.InstalledAddon], num_from: int = 0) -> None:
        """ Show a list of addons """
        self.gutter = 2 + math.ceil(math.log(num_from + len(results), 10))
        ctx = click.get_current_context()
        self.api = ctx.obj['api']
        self.cat_name_hierarchy = ctx.obj['api'].cat_name_hierarchy
        self.rel_path = lambda path, root=ctx.obj['local'].root: path.relative_to(root)

        for n, addon in enumerate(results, num_from + 1):
            item = f'{n:{self.gutter - 2}}: ' if len(results) > 1 else ''
            if isinstance(addon, InstalledAddon) and addon.id is not None:
                self._installed(item, addon)
            elif isinstance(addon, InstalledAddon):
                self._folder(item, addon)
            else:
                self._addon_info(item, addon)

        click.echo()


def _confirm(query: str | None) -> bool:
    answer = click.confirm(query or 'Confirm?', prompt_suffix=':\n>> ')
    click.echo()
    return answer


def _prompt_addon(results: Iterable[AddonT], confirm_prompt: str | None = None,
                  show_batch: int | None = None) -> AddonT | None:
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


def _find_installed(local: gru.install.Folder, api: gru.api.API, term: str | None,
                    confirm_prompt: str | None = None,
                    pool: Iterable[gru.addon.InstalledAddon] | None = None) -> gru.addon.InstalledAddon | None:
    """ `pool` narrows what an empty `term` browses/prompts among (default: every installed addon)
    -- e.g. remove-lock only wants to offer already-locked addons, not all of them. """
    if term:
        addon = local.find(term.lower(), api)
    else:
        addon = pool if pool is not None else local.installed

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
    """ Adds commands that are only useful in REPL mode to a click group -- 'help' is a real,
    always-registered command (see print_help()), not added here. """
    @group.command('exit', help='Exit CLI')
    def exit_repl() -> typing.NoReturn:
        raise click_repl.ExitReplException()


def resolve_addons_root(config: configparser.ConfigParser, game: str) -> bool:
    """ Ensure `config` has a valid addons root, prompting interactively if missing.
    Returns whether `config` was changed, so the caller can decide whether to persist it. """
    if gru_app.addons_root_configured(config, game):
        return False
    click.echo(f'{game} addons directory not found!')
    root = typing.cast(pathlib.Path, click.prompt(
        'Path to addons directory', prompt_suffix=':\n>> ',
        type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path)))
    config.set(f'{game}.addons', 'root', str(root.resolve()))
    return True


def build_app(game: str, config_file: pathlib.Path | None) -> tuple[configparser.ConfigParser, API, Folder]:
    """ Load config, ensure a valid addons root (prompting interactively if needed), and build a
    live API + scanned Folder via gru.app.build_app(). The single seam a test needs to monkeypatch
    to drive commands without real network/disk. """
    config = load_config(config_file)
    if resolve_addons_root(config, game):
        save_config(config, config_file)
    api, local = gru_app.build_app(game, config)
    return config, api, local


@click.group(cls=SectionedHelpGroup, invoke_without_command=True, help=gru_app.ABOUT.splitlines()[0],
             context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--config', 'config_file', help='path to config file',
              type=click.Path(dir_okay=False, writable=True, path_type=pathlib.Path), default=None)
@click.option('--game', 'game', help='Choice of game', hidden=True,
              type=click.Choice(['ESO']), default='ESO')
@click.option('--no-color', 'no_color', is_flag=True, default=False, help='Disable colored output')
@click.option('--debug', 'debug', is_flag=True, default=False, hidden=True,
              help='Print addon-matching scoring/decisions (rank_candidates, find_exact_match, ...) to stderr')
@click.pass_context
def main(ctx: click.Context, game: str = 'ESO', config_file: pathlib.Path | None = None,
         no_color: bool = False, debug: bool = False) -> None:
    locale.setlocale(locale.LC_ALL, '')

    if debug:
        # Only gru.app, not root -- avoids unmuting chatty third-party loggers too.
        logging.basicConfig(format='[debug] %(message)s')
        logging.getLogger('gru.app').setLevel(logging.DEBUG)

    # Default to color even when piping
    ctx.color = not no_color

    ctx.ensure_object(dict)
    ctx.obj['warnings'] = ctx.with_resource(warnings.catch_warnings(record=True))

    try:
        if ctx.invoked_subcommand in ('about', 'help'):
            config = api = local = None  # needs neither config, network, nor filesystem access
        elif ctx.invoked_subcommand == 'config':
            config = load_config(config_file)  # skip network fetch/scan as we may be setting those up
            api = local = None
        else:
            config, api, local = build_app(game, config_file)
    except (OSError, configparser.Error) as exc:
        raise click.ClickException(str(exc))
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
    """ Access persistent configuration parameters """
    pass


@config.command('list')
@click.pass_context
@click.argument('section', required=False)
def config_list(ctx: click.Context, section: str | None = None) -> None:
    """ List all current configuration parameters """
    config = display_config(ctx.obj['config'], ctx.obj['game'])
    if not section:
        for key, value in config.items():
            click.echo(f'{key} = {value!r}')
    else:
        items = [(key, value) for key, value in config.items() if key.split('.', 1)[0] == section]
        if not items:
            click.echo(f'Error: section {section} not understood')
            return
        for key, value in items:
            click.echo(f'{key} = {value!r}')


@config.command('get')
@click.pass_context
@click.argument('entry')
def config_get(ctx: click.Context, entry: str) -> None:
    """ Get the value of a configuration parameter """
    config = display_config(ctx.obj['config'], ctx.obj['game'])

    if '.' not in entry:
        click.echo('Entry must be formatted as <section>.<key>')
        return

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
    """ Define the value of a configuration parameter """
    try:
        section_name, key = entry.split('.', 1)
    except ValueError:
        click.echo('Entry must be formatted as <section>.<key>')
        return

    try:
        update_config(ctx.obj['config'], ctx.obj['game'], {entry: value})
    except KeyError:
        click.echo(f'Error: section {section_name} not recognized')
    except configparser.NoOptionError:
        click.echo(f'Error: entry {key} not found in {section_name} options')
    except ValueError:
        click.echo('Error: value must be "on" or "off" for boolean values only')
    else:
        try:
            save_config(ctx.obj['config'], ctx.obj['config_file'])
        except OSError as exc:
            raise click.ClickException(str(exc))


@main.command
def about() -> None:
    """ About this app """
    click.echo(gru_app.ABOUT)


@main.command
@click.pass_context
def help(ctx: click.Context) -> None:
    """ Show this help message """
    assert ctx.parent is not None  # always invoked as a subcommand of `main`
    click.echo(main.get_help(ctx.parent))


def show_warnings(ctx: click.Context) -> None:
    if not ctx.obj['warnings']:
        return

    click.echo(f'\n{len(ctx.obj["warnings"])} warning(s):', err=True)
    while ctx.obj['warnings']:
        click.echo(f'- {ctx.obj["warnings"].pop(0).message}', err=True)


@main.result_callback()
@click.pass_context
def process_result(ctx: click.Context, result: typing.Any, game: str, config_file: str | None, no_color: bool,
                   debug: bool) -> None:
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False, nargs=-1)
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--yes', '-y', 'batch', is_flag=True, default=False)
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.option('--version', 'version', is_flag=False, flag_value='', default=None,
              help='Install a specific previous version instead of latest (bare flag to pick from a list)')
@click.pass_context
def get(ctx: click.Context, addon: list[str], auto_deps: bool = True, opt: bool | None = None,
        batch: bool = False, version: str | None = None) -> None:
    """ Find, download, and install an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']
    before = local.snapshot()

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    addon_list = addon or [click.prompt('Addon to install', prompt_suffix=':\n>> ')]

    if version is not None and len(addon_list) > 1:
        click.echo('--version only makes sense for a single addon at a time.')
        show_warnings(ctx)
        return

    try:
        for addon_spec in addon_list:

            matches = api.find(addon_spec, local)
            if not matches:
                click.echo('No corresponding addon found')
                click.echo()
                if batch:
                    warnings.warn(f'Skipped install of unmatched addon {addon_spec}')
                continue

            picked: gru.addon.AddonInfo | gru.addon.InstalledAddon | None
            if not batch:
                picked = _prompt_addon(matches, 'Confirm installation?')
            elif len(matches) == 1:
                picked = matches[0]
            else:
                click.echo(f'Ambiguous addon specificiation {addon_spec}, skipping')
                warnings.warn(f'Skipped install of ambiguous addon {addon_spec}')
                picked = None

            if picked is None:
                continue

            if not isinstance(picked, AddonInfo):
                # Matched only by local directory name, not present in the online catalog --
                # nothing to download.
                click.echo(f'{addon_spec} is already installed locally and not found online, skipping')
                continue
            found = picked

            # Try to reuse an existing install dir
            install_path = None
            try:
                installed_addon = next(ad for ad in local.id(found.id) if ad.parent is None)
                if installed_addon.locked:
                    click.echo(f'{found.title} is version locked; run `gru remove-lock {installed_addon.dir}` '
                               'first if you want to replace it.')
                    continue
                if batch or _confirm(f'Addon found at {installed_addon.folder}, update?'):
                    install_path = installed_addon.folder
                else:
                    click.echo('Nothing to do.')
                    continue
            except StopIteration:
                pass

            url_override = None
            if version == '' and batch:
                click.echo(f'Cannot prompt for a version in batch mode, skipping {addon_spec}')
                continue
            elif version == '':
                versions = api.previous_versions(found.id)
                if not versions:
                    click.echo(f'No archived versions found for {found.title}, skipping')
                    continue
                click.echo(f'Archived versions of {found.title}:')
                for n, v in enumerate(versions, 1):
                    click.echo(f'{n:3}: {v.version:15} {v.date:20} {v.size:8} {v.uploader or "unknown"}')
                answer = click.prompt('Select version (0 cancels)', prompt_suffix=':\n>> ',
                                      type=click.IntRange(0, len(versions)))
                if answer == 0:
                    click.echo('Nothing to do.')
                    continue
                url_override = urllib.parse.urljoin(api.info_url_template, versions[answer - 1].download_url)
            elif version is not None:
                archived = next((v for v in api.previous_versions(found.id) if v.version == version), None)
                if archived is None:
                    click.echo(f'Version {version} not found in the archive for {found.title}, skipping')
                    continue
                url_override = urllib.parse.urljoin(api.info_url_template, archived.download_url)

            try:
                result = local.install(found, api, _progress, path=install_path, deps=auto_deps, opt=opt,
                                       url_override=url_override)
            except (KeyError, requests.RequestException, zipfile.BadZipFile) as exc:
                click.echo(f'Failed installing {addon_spec}: {type(exc).__name__} {exc}')
                if not batch:
                    break
                continue

            if result is None:
                click.echo(f'Done installing {TermDisplay._render_eso_text(found.title)}')
            else:
                click.echo(f'Done installing {TermDisplay._render_eso_text(found.title)} and {result} dependence(s)')

            if version is not None:
                click.echo(f'Consider `gru add-lock {found.dir}` to keep update from overwriting this version.')
            if user_config(local.game, f'{found.dir}.patch').exists():
                click.echo(f'Note: a saved patch exists for {found.dir} but was not applied -- '
                           f'run `gru patch {found.dir}` to apply it.')
    finally:
        local.export_state()
        gru_app.log_changes(local, ctx.obj['config'], before)
        show_warnings(ctx)


@main.command(hidden=True)
@click.argument('addon', required=False, nargs=-1)
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--yes', '-y', 'batch', is_flag=True, default=False)
@click.option('--opt/--no-opt', default=None)
@click.pass_context
def install(ctx: click.Context, addon: list[str], auto_deps: bool = True, opt: bool | None = None,
            batch: bool = False) -> None:
    """ Light bulb! """
    click.echo()
    click.echo('Light bulb! But zis is not ze word, keed.')
    click.echo("Ze command is 'get' -- G, R, U: Get, Remove, Update. Zat is ze whole joke, you see?")
    click.echo('Is okay, is okay. I feex it for you. Zis is what I do now, I am a hero.')
    click.echo()
    ctx.invoke(get, addon=addon, auto_deps=auto_deps, opt=opt, batch=batch)


def _remove_vars_policy(ctx: click.Context, local: gru.install.Folder, remove_vars: bool | None
                        ) -> tuple[gru.install.RemoveVarsPolicy, list[str]]:
    """ Builds the policy passed to Folder.remove()/remove_unused_deps()/remove_duplicates(), so
    the same answer governs the addon(s) named on the command line AND any dependency or duplicate
    that removal cascades into -- each gets asked again when that policy prompts. Also returns the
    (growing) list of addon titles actually stripped of their SavedVariables, for the final summary
    message; an addon with nothing declared/on disk never gets prompted or added to that list.

    --remove-vars/--keep-vars was given explicitly: fixed answer for everything, no prompting.
    Otherwise: fall back to config (yes/no/ask), prompting only when it's 'ask'. """
    if remove_vars is None:
        setting = ctx.obj['config'].get(f'{local.game}.addons', 'remove_saved_variables')
        forced = None if setting == 'ask' else setting == 'yes'
    else:
        forced = remove_vars

    removed: list[str] = []

    def resolve(addon: gru.addon.InstalledAddon) -> bool:
        if not local.saved_variable_files(addon):
            return False
        if forced is not None:
            decision = forced
        else:
            title = TermDisplay._render_eso_text(addon.title)
            decision = click.confirm(f'Also remove saved variables for {title}?', prompt_suffix=':\n>> ')
        if decision:
            removed.append(addon.title)
        return decision

    return resolve, removed


@main.command(short_help='Identify and uninstall a locally installed addon')
@click.argument('addon', required=False)
@click.option('--clean-deps/--no-clean-deps', default=False, help='Clean up unused dependences')
@click.option('--opt/--no-opt', default=None, help='Keep optional dependences')
@click.option('--remove-vars/--keep-vars', default=None, help='Remove SavedVariables (default: from config)')
@click.pass_context
def remove(ctx: click.Context, addon: str | None, clean_deps: bool = False, opt: bool | None = None,
           remove_vars: bool | None = None) -> None:
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']
    before = local.snapshot()

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    installed_addon = _find_installed(local, api, addon, 'Confirm removal?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    remove_vars_policy, vars_removed = _remove_vars_policy(ctx, local, remove_vars)

    try:
        nremoved = local.remove(installed_addon, deps=clean_deps, opt=opt, remove_vars=remove_vars_policy)

        title = TermDisplay._render_eso_text(installed_addon.title)
        if not clean_deps:
            click.echo(f'Removed addon {title}.')
        else:
            click.echo(f'Removed addon {title} and {nremoved} unused dependence(s).')
        if vars_removed:
            names = ', '.join(TermDisplay._render_eso_text(name) for name in vars_removed)
            click.echo(f'Also removed saved variables for: {names}')
    finally:
        local.export_state()
        gru_app.log_changes(local, ctx.obj['config'], before)
        show_warnings(ctx)


@main.command(short_help='Find out-of-date installed addons and install newer versions')
@click.option('--auto-deps/--no-auto-deps', default=True, help='Automatically install new/missing dependences')
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.option('--patch/--no-patch', default=None, help='Automatically re-apply patches')
@click.pass_context
def update(ctx: click.Context, auto_deps: bool, opt: bool | None, patch: bool | None) -> None:
    """ Find out-of-date and missing addons and install them """
    api = ctx.obj['api']
    local = ctx.obj['local']
    gru_app.resolve_exact_matches(local, api)
    before = local.snapshot()

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')
    if patch is None:
        patch = get_config_bool(ctx, '{game}.addons.patch_updates')

    try:
        updates, added = local.update(api, _progress, opt=opt, deps=auto_deps, patch=patch)

        if updates + added == 0:
            click.echo('Nothing to do')
        elif auto_deps:
            click.echo(f'Updated {updates} addon(s) and installed {added} dependence(s)')
        else:
            click.echo(f'Updated {updates} addon(s)')

        unmatched = [addon for addon in local.installed if addon.id is None]
        if unmatched:
            click.echo()
            click.echo(click.style(
                f'WARNING: {len(unmatched)} addon(s) could not be matched online and were NOT checked '
                'for updates:', fg='red', bold=True))
            for addon in unmatched:
                note = ('ambiguous - see `gru match`' if _unmatched_reason(api, addon.dir) == 'ambiguous'
                        else 'no online listing')
                click.echo(click.style(f'  - {TermDisplay._render_eso_text(addon.title)} ({note})', fg='red'))
    finally:
        local.export_state()
        gru_app.log_changes(local, ctx.obj['config'], before)
        show_warnings(ctx)


@main.command
@click.option('--opt/--no-opt', default=True, help='Keep optional dependences')
@click.option('--dedupe/--no-dedupe', default=False,
              help='Also remove standalone libraries superseded by an equal-or-newer bundled copy')
@click.option('--remove-vars/--keep-vars', default=None, help='Remove SavedVariables (default: from config)')
@click.pass_context
def cleanup(ctx: click.Context, opt: bool | None = None, dedupe: bool = False,
            remove_vars: bool | None = None) -> None:
    """ Remove unused dependences """
    local = ctx.obj['local']
    before = local.snapshot()

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')
    remove_vars_policy, vars_removed = _remove_vars_policy(ctx, local, remove_vars)

    try:
        nremoved = local.remove_unused_deps(opt=opt, remove_vars=remove_vars_policy)
        click.echo(f'Removed {nremoved} unused dependence(s).')

        if dedupe:
            pairs = local.remove_duplicates(remove_vars=remove_vars_policy)
            for addon, bundled_in in pairs:
                assert bundled_in.parent is not None  # duplicate_standalones() only pairs with nested siblings
                parent_title = bundled_in.parent.title
                click.echo(f'Removed duplicate {addon.dir}, superseded by the copy bundled inside {parent_title}')
            click.echo(f'Removed {len(pairs)} duplicate install(s).')

        if vars_removed:
            names = ', '.join(TermDisplay._render_eso_text(name) for name in vars_removed)
            click.echo(f'Also removed saved variables for: {names}')
    finally:
        local.export_state()
        gru_app.log_changes(local, ctx.obj['config'], before)
        show_warnings(ctx)


@main.command(short_help='Pin an addon to its currently installed version')
@click.argument('addon', required=False)
@click.pass_context
def add_lock(ctx: click.Context, addon: str | None) -> None:
    """ Pin an addon to its currently installed version """
    api = ctx.obj['api']
    local = ctx.obj['local']

    unlocked = [a for a in local.installed if not a.locked]
    if not addon and not unlocked:
        click.echo('All installed addons are already version locked.')
        show_warnings(ctx)
        return

    found = _find_installed(local, api, addon, 'Confirm lock target?', pool=unlocked)
    if found is None:
        show_warnings(ctx)
        return

    if found.locked:
        click.echo(f'{TermDisplay._render_eso_text(found.title)} is already version locked.')
        show_warnings(ctx)
        return

    found.locked = True
    local.export_state()
    click.echo(f'{TermDisplay._render_eso_text(found.title)} is now version locked.')
    show_warnings(ctx)


@main.command
@click.argument('addon', required=False)
@click.pass_context
def remove_lock(ctx: click.Context, addon: str | None) -> None:
    """ Unpin a previously version-locked addon """
    api = ctx.obj['api']
    local = ctx.obj['local']

    locked = [a for a in local.installed if a.locked]
    if not addon and not locked:
        click.echo('No version-locked addons.')
        show_warnings(ctx)
        return

    found = _find_installed(local, api, addon, 'Confirm unlock target?', pool=locked)
    if found is None:
        show_warnings(ctx)
        return

    if not found.locked:
        click.echo(f'{TermDisplay._render_eso_text(found.title)} is not version locked.')
        show_warnings(ctx)
        return

    found.locked = False
    local.export_state()
    click.echo(f'{TermDisplay._render_eso_text(found.title)} is no longer version locked.')
    show_warnings(ctx)


@main.command
@click.pass_context
def list_locks(ctx: click.Context) -> None:
    """ List version-locked addons """
    local = ctx.obj['local']
    locked = [addon for addon in local.installed if addon.locked]

    if not locked:
        click.echo('No version-locked addons.')
        return

    click.echo(f'{len(locked)} version-locked addon(s):')
    TermDisplay(locked)
    show_warnings(ctx)


@main.command(short_help='Resolve addons that ambiguously match several online listings')
@click.argument('addon', required=False)
@click.pass_context
def match(ctx: click.Context, addon: str | None) -> None:
    """ Resolve addons that ambiguously match several online listings """
    api = ctx.obj['api']
    local = ctx.obj['local']
    sortkey = ctx.obj['config'].get(f'{local.game}.addons', 'sortkey')

    auto_resolved = gru_app.resolve_exact_matches(local, api)
    auto_resolved += gru_app.resolve_ambiguous_bundles(local, api)
    if auto_resolved:
        local.export_state()

    ambiguous = gru_app.find_ambiguous(local, api) + gru_app.find_ambiguous_bundles(local, api)
    if addon:
        term = addon.lower()
        ambiguous = [(inst, c) for inst, c in ambiguous if term in inst.dir.lower() or term in inst.title.lower()]
        auto_resolved = [inst for inst in auto_resolved if term in inst.dir.lower() or term in inst.title.lower()]

    total = len(ambiguous) + len(auto_resolved)
    if not total:
        click.echo('No ambiguous addons to resolve.')
        show_warnings(ctx)
        return

    resolved = len(auto_resolved)
    for installed, candidates in ambiguous:
        ranked = gru_app.rank_candidates(installed, candidates, api, sortkey)
        click.echo(f'\n{TermDisplay._render_eso_text(installed.title)} ({installed.dir}):')
        picked = _prompt_addon(ranked, f'Confirm this is {installed.title}?')
        if picked is None:
            continue
        installed.link(picked)
        resolved += 1

    if resolved > len(auto_resolved):
        local.export_state()
    click.echo(f'Resolved {resolved} of {total} ambiguous addon(s).')
    show_warnings(ctx)


@main.command(hidden=True)
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


@main.command
@click.argument('term', required=False)
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
@click.pass_context
def search(ctx: click.Context, term: str | None, max_: int = 10) -> None:
    """ Search online listings for an addon """
    if term is None:
        term = click.prompt('Term to search for', prompt_suffix=':\n>> ')

    api = ctx.obj['api']
    search = api.search(term, maxlen=max_)
    if search:
        click.echo(f'{len(search)} results:')
        TermDisplay(search)
    else:
        click.echo('No matches.')


@main.command('list')
@click.pass_context
def list_(ctx: click.Context) -> None:
    """ List installed add-ons """
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    click.echo(f'Found {len(local.installed)} addon(s):')
    TermDisplay(local.installed)
    show_warnings(ctx)


@main.command
@click.option('--recurse', '-r', help='Recurse into subdirectories (will show private libraries)', default=False)
@click.option('--output', '-o', 'output_path', type=click.Path(dir_okay=False, path_type=pathlib.Path),
              help='Write to a file instead of stdout')
@click.pass_context
def export(ctx: click.Context, recurse: bool = False, output_path: pathlib.Path | None = None) -> None:
    """ Export installed add-ons """
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    if output_path is None:
        local.write_csv(sys.stdout, recurse)
        sys.stdout.flush()  # unlike click.echo(), a raw sys.stdout write isn't auto-flushed
    else:
        with output_path.open('w', newline='') as out:
            count = local.write_csv(out, recurse)
        click.echo(f'All {count} addon(s) exported to:\n{output_path.resolve()}')
    show_warnings(ctx)


def _format_change(entry: gru_app.ChangeEntry, width: int) -> str:
    try:
        when = datetime.datetime.fromisoformat(entry.date).strftime('%x %X')
    except ValueError:
        when = entry.date

    if entry.version == gru_app.NOT_INSTALLED:
        change = click.style(f'removed (was {entry.previous_state})', fg='red', bold=True)
    elif entry.previous_state == gru_app.NOT_INSTALLED:
        change = click.style(f'installed {entry.version}', fg='green', bold=True)
    else:
        change = f'{entry.previous_state} -> {entry.version}'

    return f'{when}  {entry.dir:{width}}  {change}'


@main.command(help='Review recent addon changes')
@click.option('-n', '--limit', help='Show at most N most recent changes (default: all)', type=int, default=None)
@click.pass_context
def review(ctx: click.Context, limit: int | None) -> None:
    local = ctx.obj['local']
    changes = gru_app.read_changes(local, limit=limit)

    if not changes:
        click.echo('No changes recorded yet.')
        return

    click.echo(f'{len(changes)} change(s):')
    width = max(len(entry.dir) for entry in changes)
    for entry in changes:
        click.echo(_format_change(entry, width))
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
    found: list[gru.addon.AddonInfo] = []
    not_found: list[gru.addon.Dependency] = []
    for dep in missing:
        try:
            found.append(api.dir(dep.dir))
        except FileNotFoundError:
            not_found.append(dep)

    if found:
        TermDisplay(list(found))
    if not_found:
        click.echo('Not found online: ' + ', '.join(dep.dir for dep in not_found))

    if found:
        click.echo('Run update to fetch resolved missing dependences')
    show_warnings(ctx)


@main.command(short_help='Save the diff between current addon and upstream as a patch')
@click.argument('addon', required=False)
@click.option('--url', help='Download url for installed version', required=False)
@click.pass_context
def diff(ctx: click.Context, addon: str | None, url: str | None = None) -> None:
    """ Save the diff between current addon and upstream as a patch """
    api = ctx.obj['api']
    local = ctx.obj['local']

    found = _find_installed(local, api, addon, 'Confirm diff saving?')
    if found is None:
        show_warnings(ctx)
        return
    if found.infos is None:
        click.echo(f'{found.title} is not matched with an online addon, nothing to diff against.')
        show_warnings(ctx)
        return

    if not url and found.infos.version != found.version:
        archived = next((v for v in api.previous_versions(found.infos.id) if v.version == found.version), None)
        if archived is not None:
            url = urllib.parse.urljoin(api.info_url_template, archived.download_url)
        else:
            click.echo(f'Failed to fetch unmodified source for version {found.version} automatically.')
            click.echo()
            url = click.prompt(f'Please manually specify {found.version} download url',
                               prompt_suffix=':\n>> ', type=str)

    result_path = user_config(local.game, f'{found.dir}.patch')

    with local.unmodified_addon(found.infos, api, url=url) as ref_addon, result_path.open('w') as out:
        nfiles = addon_diff(ref_addon, found, out=out)

    if nfiles > 0:
        click.echo(f'Changes saved under:\n{result_path.resolve()}')
    else:
        result_path.unlink()
        click.echo('No changes to be saved.')
    show_warnings(ctx)


@main.command(help='Import a patch of changes for an addon')
@click.argument('addon', required=False)
@click.argument('patch', type=click.Path(dir_okay=False, path_type=pathlib.Path), required=False)
@click.option('--partial/--no-partial', default=False,
              help='Apply every hunk that succeeds; save the rest to <file>.rej')
@click.pass_context
def patch(ctx: click.Context, addon: str | None, patch: pathlib.Path | None, partial: bool = False) -> None:
    api = ctx.obj['api']
    local = ctx.obj['local']

    installed_addon = _find_installed(local, api, addon, 'Confirm patch target?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    if patch is None:
        patch = user_config(local.game, f'{installed_addon.dir}.patch')
        if not patch.exists():
            click.echo('No saved changes to be re-applied.')
            return

    try:
        result = addon_patch_file(installed_addon, patch, partial=partial)
    except PatchError as exc:
        click.echo(f'Patch is invalid: {exc}')
        show_warnings(ctx)
        return

    if not result.files:
        click.echo('No changes to apply in patch.')
    elif result.backed_out:
        click.echo(f"{click.style('Patch failed to apply cleanly!', fg='red', bold=True)} No changes were made.")
        click.echo('Failing hunks:')
        for f in result.files:
            if f.failed:
                click.echo(f'  {f.path}: {", ".join(f.failed)}')
        click.echo()
        click.echo(f'Run `gru patch {installed_addon.dir} --partial{f" {patch}" if patch else ""}` '
                   'to apply what can be applied and fix the rest manually.')
        click.echo()
    elif result.clean:
        click.echo('Applied patch successfully.')
    else:
        applied = [f for f in result.files if f.applied]
        rejects = [f for f in result.files if f.reject]
        if applied:
            click.echo(f'Some changes were applied ({len(applied)} / {len(result.files)} file(s)).')
        else:
            click.echo('No changes could be applied.')
        click.echo()
        click.echo(f'{click.style("Some changes failed to apply!", fg="red", bold=True)} '
                   'Saved to the following, apply them manually:')
        for f in rejects:
            click.echo(f'  {f.reject}')
        click.echo()
        click.echo('The saved patch itself is unchanged: ')
        click.echo(f'- `gru get {installed_addon.dir}` will roll back all changes done by this patch command')
        click.echo(f'- `gru patch {installed_addon.dir}{f" {patch}" if patch else ""}`'
                   ' will try to apply the same patch again')
        click.echo()
        click.echo('After fixing the addon changes manually, save that state as the new patch, replacing this one, '
                   f'using: `gru diff {installed_addon.dir}`')
        click.echo()


@main.command
@click.pass_context
def clear_caches(ctx: click.Context) -> None:
    """ Remove any cached data this app may keep """
    api = ctx.obj['api']
    local = ctx.obj['local']
    api.reset()
    local.scan(api)

    click.echo('Caches cleared.')
    show_warnings(ctx)
