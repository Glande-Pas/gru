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
import click
import math
import sys
import os
import click_repl
import prompt_toolkit.history as prompt_history

from .config import load_config, save_config, user_cache
from .api import API
from .addon import Addon
from .install import Folder
from .patch import addon_diff, addon_patch


def get_config_bool(ctx, string):
    section, key = string.format(**ctx.obj).rsplit('.', maxsplit=1)
    return ctx.obj['config'].getboolean(section, key)


class SectionedHelpGroup(click.Group):
    """ Sections commands into help groups """

    _cmd_shortcuts = {'rm': 'remove', 'up': 'update', 'ls': 'list', 's': 'search', 'cc': 'clear-caches', 'df': 'diff'}

    @classmethod
    def _cmd_group(cls, cmd):
        if cmd.name in {'get', 'remove', 'update'}:
            return 'main'
        elif cmd.name in {'help', 'exit'}:
            return 'command line'
        else:
            return 'extra'

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self._cmd_shortcuts.get(cmd_name, cmd_name))

    def format_commands(self, ctx, formatter):
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


def _wrapped_display(gutter_width, *infos):
    pfx = ' ' * gutter_width
    width = shutil.get_terminal_size()[0] - gutter_width
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


def _display_addon(ctx, num, addon, gutter_width):
    """ Show addon info from the API endpoint """
    click.echo()
    click.echo(f'{num:{gutter_width}}{addon.metadata["title"]} (id {addon.id})' + (
        '' if addon.folder is None else
        f'  [installed]' if not addon.can_update() else
        f'  [installed - {click.style("update available", bold=True)}]'
    ))
    # TODO: Based on verbosity level, only click.echo a number of those:
    _wrapped_display(gutter_width, [
        f'Author: {addon.metadata["author"]:20}',
        f'Version: {addon.metadata["version" if addon.folder is None else "installed_version"]:10}',
        f'Updated: {addon.metadata["date"].strftime("%x"):10}',
        f'Category: {" > ".join(ctx.obj["api"].cat_name_hierarchy(addon.metadata["category"]))}',
    ], [
        f'Directory: {addon.dir:40}',
        f'Favorites: {addon.metadata["favorites"]:8n}',
        f'Downloads: {addon.metadata["downloads"]:n} [{addon.metadata["monthly"]:n} / Month]',
    ])
    click.echo(' ' * gutter_width + addon.metadata['link'])


def _display_folder(num, folder, gutter_width):
    """ Show addon info from a local folder that was not matched with the API endpoint """
    click.echo()
    click.echo(f'{num:{gutter_width}}{folder.metadata["title"]} (id not found)  [installed]')
    # Based on verbosity level, only click.echo a number of those:
    # TODO: move to an “Addon” object
    infos = [
        f'Author: {folder.metadata["author"]:20}',
        f'Version: {folder.metadata["installed_version"]:10}',
    ]
    if 'Description' in folder.metadata:
        infos.append(f'Description: {folder.metadata["description"]}')
    _wrapped_display(gutter_width, infos)
    click.echo(' ' * gutter_width + 'NB: this add-on may be deprecated')


def _display_unknown(num, folder, gutter_width):
    """ Show a missing and unresolved dependence """
    click.echo()
    click.echo(f'{num:{gutter_width}}{folder} (id not found)')
    click.echo(' ' * gutter_width + f'NB: this add-on may be deprecated')


def _display(ctx, results, num_from=0):
    """ Show a list of addons """
    api = ctx.obj['api']
    gutter_width = 2 + math.ceil(math.log(len(results), 10))

    for n, addon in enumerate(results, num_from + 1):
        num = f'{n:{gutter_width - 2}}: ' if len(results) > 1 else ''
        if addon.id is not None:
            local = ctx.obj['local'].find_installed(addon)
            if local is not None:
                addon = local.merge(addon)
            _display_addon(ctx, num, addon, gutter_width)
        elif addon.folder is not None:
            _display_folder(num, addon, gutter_width)
        else:
            _display_unknown(num, addon, gutter_width)

    click.echo()


def _confirm(query):
    answer = click.confirm(query, prompt_suffix=':\n>> ')
    click.echo()
    return answer


def _prompt_addon(ctx, results, confirm_prompt=None, show_batch=None):
    """ Pick an addon from a list of addons """
    if not results:
        click.echo('No addon found')
        return None

    if show_batch is None:
        show_batch = max(shutil.get_terminal_size()[1] // 5 - 1, 4)

    click.echo(' '.join([
        'No exact matches.',
        f'{len(results)} possible results:' if len(results) > 1 else 'Only approximate result:'
    ]))

    _display(ctx, results[:show_batch])

    if len(results) == 1:
        return results[0] if _confirm(confirm_prompt) else None

    for shown in range(show_batch, len(results), show_batch):
        answer = click.prompt(f'Select (1-{shown}, 0 cancels, empty continues)', prompt_suffix=':\n>> ', default=-1,
                              type=click.IntRange(-1, shown + 1), show_default=False)
        if answer >= 0:
            break

        _display(ctx, results[shown:shown+show_batch], num_from=shown)
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


def _find_installed(ctx, api, local, addon, confirm_prompt=None):
    # TODO: make case-insensitive
    if addon is None:
        addon = local.installed
    else:
        addon = api.find(addon, local)
        if isinstance(addon, list):
            addon = local.filter_installed(addon)

    if not addon:
        click.echo('No corresponding addon found.')
        return

    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon, confirm_prompt)
    if not addon:
        click.echo('Nothing do to.')
        return None

    return local.find_installed(addon)


def _progress(size, message):
    return click.progressbar(length=size, label=message, width=0)


def add_repl_commands(group):
    @group.command('help', help='Print CLI help')
    def print_help():
        with click.Context(group) as ctx:
            click.echo(group.get_help(ctx))

    @group.command('exit', help='Exit CLI')
    def exit_repl():
        raise click_repl.ExitReplException()


@click.group(cls=SectionedHelpGroup, invoke_without_command=True, context_settings=dict(help_option_names=['-h', '--help']))
@click.option('--config', 'config_file', help='path to config file',
              type=click.Path(dir_okay=False, writable=True), default=None)
@click.option('--game', 'game', help='Choice of game', hidden=True,
              type=click.Choice(['ESO']), default='ESO')
@click.pass_context
def main(ctx, game='ESO', config_file=None):
    locale.setlocale(locale.LC_ALL, '')

    ctx.ensure_object(dict)
    ctx.obj['warnings'] = ctx.with_resource(warnings.catch_warnings(record=True, category=UserWarning))

    config = ctx.obj['config'] = load_config(config_file)
    ctx.obj['game'] = game
    ctx.obj['config_file'] = config_file

    root = config.get(f'{game}.addons', 'root')
    if not root or not pathlib.Path(root).exists():
        click.echo(f'{game} addons directory not found!')
        root = click.prompt('Path to addons directory', prompt_suffix=':\n>> ',
                            type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
        config.set(f'{game}.addons', 'root', str(root.resolve()))
        save_config(config, config_file)

    api = ctx.obj['api'] = API.live(config)
    local = ctx.obj['local'] = Folder(game, config)
    local.scan(api)

    if ctx.invoked_subcommand is None:
        add_repl_commands(main)
        click_repl.repl(ctx, prompt_kwargs={
            'history': prompt_history.FileHistory(user_cache('history')),
        })


@main.group()
@click.pass_context
def config(ctx):
    # The cli-configurable sections and names under which they will appear
    ctx.obj['sections'] = {
        'app': 'app',
        'addons': f'{ctx.obj["game"]}.addons',
    }


@config.command
@click.pass_context
@click.argument('entry', required=False)
def show(ctx, entry=None):
    if not entry:
        for section_name, section in ctx.obj['sections'].items():
            for key, value in ctx.obj['config'].items(section):
                click.echo(f'{section_name}.{key} = {value!r}')
        return

    section_name, *opt_key = entry.split('.', 1)
    try:
        section = ctx.obj['sections'][section_name]
    except KeyError:
        click.echo(f'Error: section {section_name} not understood')
        return

    try:
        key = opt_key[0]
    except IndexError:
        key = None

    if not key:
        for key, value in ctx.obj['config'].items(section):
            click.echo(f'{section_name}.{key} = {value!r}')
        return

    try:
        click.echo(ctx.obj['config'].get(section, key))
    except configparser.NoOptionError:
        click.echo(f'Error: entry {key} not found in {section_name}')


@config.command
@click.pass_context
@click.argument('entry')
@click.argument('value')
def set(ctx, entry, value):
    try:
        section_name, key = entry.split('.', 1)
    except ValueError:
        click.echo(f'Entry must be formatted as <section>.<key>')
        return

    try:
        section = ctx.obj['sections'][section_name]
    except KeyError:
        click.echo(f'Error: section {section_name} not recognized')
        return

    try:
        is_bool = ctx.obj['config'].get(section, key) in {'on', 'off'}
    except configparser.NoOptionError:
        click.echo(f'Error: entry {key} not found in {section_name} options')
        return

    if is_bool != (value in {'on', 'off'}):
        click.echo(f'Error: value must be "on" or "off"{"" if is_bool else " only"} for boolean values')
        return

    ctx.obj['config'].set(section, key, value)
    save_config(ctx.obj['config'], ctx.obj['config_file'])


def show_warnings(ctx):
    if not ctx.obj['warnings']:
        return

    click.echo(f'\n{len(ctx.obj["warnings"])} warning(s):')
    while ctx.obj['warnings']:
        click.echo(f'- {ctx.obj["warnings"].pop(0).message}')


@main.result_callback()
@click.pass_context
def process_result(ctx, result, game, config_file):
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False)
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.pass_context
def get(ctx, addon, auto_deps=True, opt=None):
    """ Find, download, and install an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    if addon is None:
        addon = click.prompt(f'Addon to install', prompt_suffix=':\n>> ')

    addon = api.find(addon, local)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon, 'Confirm installation?')
    if not addon:
        show_warnings(ctx)
        return

    installed_addon = local.find_installed(addon)

    # Try to reuse an existing install dir
    if installed_addon is not None and installed_addon.folder is not None:
        if not _confirm(f'Addon found at {installed_addon.folder}, update?'):
            click.echo('Nothing to do.')
            return
    else:
        installed_addon = Addon(addon.id, local.root / addon.dir)

    result = local.install(installed_addon.merge(addon), api, _progress, deps=auto_deps, opt=opt)

    if result is None:
        click.echo(f'Done installing {addon.metadata["title"]}')
    else:
        click.echo(f'Done installing {addon.metadata["title"]} and {result} dependence(s)')
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False)
@click.option('--clean-deps/--no-clean-deps', default=False, help='Clean up unused dependences')
@click.option('--opt/--no-opt', default=None, help='Keep optional dependences')
@click.pass_context
def remove(ctx, addon, clean_deps=False, opt=None):
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    installed_addon = _find_installed(ctx, api, local, addon, 'Confirm removal?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    nremoved = local.remove(installed_addon, deps=clean_deps, opt=opt)

    if not clean_deps:
        click.echo(f'Removed addon {installed_addon.metadata["title"]}.')
    else:
        click.echo(f'Removed addon {installed_addon.metadata["title"]} and {nremoved} unused dependence(s).')
    show_warnings(ctx)


@main.command()
@click.option('--auto-deps/--no-auto-deps', default=True, help='Automatically install new/missing dependences')
@click.option('--opt/--no-opt', default=None, help='Include optional dependences')
@click.option('--patch/--no-patch', default=None, help='Automatically re-apply patches')
@click.pass_context
def update(ctx, auto_deps, opt, patch):
    """ Find out-of-date and missing addons and install them """
    api = ctx.obj['api']
    local = ctx.obj['local']

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
def cleanup(ctx, opt=None):
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']

    if opt is None:
        opt = get_config_bool(ctx, '{game}.addons.optional')

    nremoved = local.remove_unused_deps(opt=opt)

    click.echo(f'Removed {nremoved} unused dependence(s).')
    show_warnings(ctx)


@main.command()
@click.pass_context
def check_api_release(ctx, hidden=True):
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
            click.echo(f'Version {API.version + 1} seems to have come out of alpha')
    except Exception as err:
        click.echo(f"Can't check alpha API status: {err}")

    if live_ok and alpha_ok:
        # Don’t just exit without
        click.echo('Status of stable and alpha APIs as expected.')


@main.command()
@click.argument('term', required=False)
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
@click.pass_context
def search(ctx, term, max_=10):
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
def list_(ctx):
    api = ctx.obj['api']
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    click.echo(f'Found {len(local.installed)} addon(s):')
    _display(ctx, local.installed)
    show_warnings(ctx)


@main.command(help='export installed add-ons')
@click.pass_context
def export(ctx):
    api = ctx.obj['api']
    local = ctx.obj['local']

    if not local.installed:
        click.echo('No addons installed.')
        return

    export_path = local.root / '.gru' / 'addons.txt'
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with export_path.open('w') as out:
        for addon in local.installed:
            print(f'{addon.dir} = {addon.metadata["installed_version"]}', file=out)

    click.echo(f'All {len(local.installed)} addon(s) exported to:\n{export_path.resolve()}')
    show_warnings(ctx)


@main.command(help='List missing dependences')
@click.option('--opt/--no-opt', help='include optional dependences')
@click.pass_context
def miss(ctx, opt):
    api = ctx.obj['api']
    local = ctx.obj['local']
    missing = local.all_missing_deps(local.installed, opt=opt)

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
def unmodified_addon(api: gru.api.API, local: gru.install.Folder, addon: gru.addon.Addon, url=None):
    # Code to acquire resource, e.g.:
    with tempfile.TemporaryDirectory() as tempdir:
        ref_local = local.alt_location(pathlib.Path(tempdir))
        ref_addon = addon.alt_location(ref_local.root)
        # Be sure to compare to installed version not up-to-date upstream
        ref_local.unpack(ref_addon, api, url_override=url)

        if ref_addon.metadata['installed_version'] != addon.metadata['installed_version']:
            raise ValueError('Downloaded addon does not have same version as installed addon!')

        yield ref_addon


@main.command(help='Save the diff between current addon and upstream as a patch')
@click.argument('addon', required=False)
@click.option('--url', help='Download url for installed version', required=False)
@click.pass_context
def diff(ctx, addon, url=None):
    api = ctx.obj['api']
    local = ctx.obj['local']

    addon = _find_installed(ctx, api, local, addon, 'Confirm diff saving?')
    if addon is None:
        show_warnings(ctx)
        return

    if not url and addon.metadata['version'] != addon.metadata['installed_version']:
        click.echo(f'Addon is out of date!  Can not fetch unmodified source automatically.')
        click.echo()
        url = click.prompt(f'Please manually specify {addon.metadata["installed_version"]} download url',
                              prompt_suffix=':\n>> ', type=str)

    result_path = local.root / '.gru' / f'{addon.dir}.patch'
    result_path.parent.mkdir(parents=True, exist_ok=True)

    with unmodified_addon(api, local, addon, url=url) as ref_addon, result_path.open('w') as out:
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
def patch(ctx, addon, patch):
    api = ctx.obj['api']
    local = ctx.obj['local']

    installed_addon = _find_installed(ctx, api, local, addon, 'Confirm patch target?')
    if installed_addon is None:
        show_warnings(ctx)
        return

    if patch is None:
        patch = local.root / '.gru' / f'{installed_addon.dir}.patch'
        if not patch.exists():
            click.echo(f'No saved changes to be re-applied.')
            return

    done, total = addon_patch(installed_addon, patch)
    if not total:
        click.echo(f'No changes to be apply in patch.')
    elif done == total:
        click.echo(f'Applied patch successfully.')
    else:
        click.echo(f'Applied {done} / {total} hunks in patch.')


@main.command(help='Save the diff between current addon and upstream')
@click.pass_context
def patch_all(ctx):
    api = ctx.obj['api']
    local = ctx.obj['local']
    missing = local.all_missing_deps(local.installed, opt=opt)


@main.command()
@click.pass_context
def clear_caches(ctx):
    api = ctx.obj['api']
    local = ctx.obj['local']
    api.reset()
    local.scan(api)

    click.echo('Caches cleared.')
    show_warnings(ctx)
