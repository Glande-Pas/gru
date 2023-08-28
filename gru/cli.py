""" Module handling command-line interface """
import warnings
import datetime
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



class SectionedHelpGroup(click.Group):
    """ Sections commands into help groups """

    _cmd_shortcuts = {'rm': 'remove', 'up': 'update', 'ls': 'list', 's': 'search', 'cc': 'clear-caches'}

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


def _display_addon(ctx, n, addon, width):
    """ Show addon info from the API endpoint """
    click.echo()
    click.echo(f'{n:{width}}{addon.metadata["title"]} (id {addon.id})' +
               (f'  [installed{" - " + click.style("update available", bold=True) if addon.can_update() else ""}]'
                if addon.folder is not None else ''))
    pfx = ' ' * width
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    infos = [
        f'Author: {addon.metadata["author"]:20}',
        f'Version: {addon.metadata["version" if addon.folder is None else "installed_version"]:10}',
        f'Updated: {addon.metadata["date"].strftime("%x"):12}',
        f'Category: {" > ".join(ctx.obj["api"].cat_name_hierarchy(addon.metadata["category"]))}',
    ]
    click.echo(pfx + sep.join(infos))
    infos2 = [
        f'Directory: {addon.dir}',
        f'Favorites: {addon.metadata["favorites"]:n}',
        f'Downloads: {addon.metadata["downloads"]:n} [{addon.metadata["monthly"]:n} / Month]',
    ]
    infos2[0] = f'{infos2[0]:{len(sep) + len(infos[0]) + len(infos[1])}}'
    infos2[1] = f'{infos2[1]:{len(infos[2])}}'
    click.echo(pfx + sep.join(infos2))
    click.echo(pfx + addon.metadata['link'])


def _display_folder(n, folder, width):
    """ Show addon info from a local folder that was not matched with the API endpoint """
    click.echo()
    click.echo(f'{n:{width}}{folder.metadata["title"]} (id not found)  [installed]')
    pfx = ' ' * width
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    # TODO: move to an “Addon” object
    click.echo(pfx + sep.join([f'Author: {folder.metadata["author"]}', f'Version: {folder.metadata["installed_version"]}']))
    if 'Description' in folder.metadata:
        click.echo(pfx + f'Description: {folder.metadata["description"]}')
    click.echo(pfx + f'NB: this add-on may be deprecated')


def _display_unknown(n, folder, width):
    """ Show a missing and unresolved dependence """
    click.echo()
    click.echo(f'{n:{width}}{folder} (id not found)')
    click.echo(' ' * width + f'NB: this add-on may be deprecated')


def _display(ctx, results, num_from=0):
    """ Show a list of addons """
    api = ctx.obj['api']

    width = math.ceil(math.log(len(results), 10))
    width += 2

    for n, addon in enumerate(results, num_from + 1):
        num = f'{n:{width - 2}}: ' if width > 2 else ''
        if addon.id is not None:
            local = ctx.obj['local'].find_installed(addon)
            if local is not None:
                addon = local.merge(addon)
            _display_addon(ctx, num, addon, width)
        elif addon.folder is not None:
            _display_folder(num, addon, width)
        else:
            _display_unknown(num, addon, width)

    click.echo()


def _confirm(query):
    answer = click.confirm(query, prompt_suffix=':\n>> ')
    click.echo()
    return answer


def _prompt_addon(ctx, results, show_batch=None):
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
        return results[0] if _confirm('Confirm removal?') else None

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

    root = config.get(f'{game}.addons', 'root')
    if not root or not pathlib.Path(root).exists():
        click.echo(f'{game} addons directory not found!')
        root = click.prompt('Path to addons directory', prompt_suffix=':\n>> ',
                            type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
        config.set(f'{game}.addons', 'root', str(root.resolve()))
        save_config(config, config_file)

    api = ctx.obj['api'] = API.live(config)
    local = ctx.obj['local'] = Folder(game, config)
    asyncio.run(local.scan(api))

    if ctx.invoked_subcommand is None:
        add_repl_commands(main)
        click_repl.repl(ctx, prompt_kwargs={
            'history': prompt_history.FileHistory(user_cache('history')),
        })


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
        opt = ctx.obj['config'].getboolean(f'{ctx.obj["game"]}.addons', 'optional')

    if addon is None:
        addon = click.prompt(f'Addon to install', prompt_suffix=':\n>> ')

    addon = api.find(addon, local)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon)
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

    installed_addon = asyncio.run(local.unpack(installed_addon.merge(addon), api, _progress))

    if not auto_deps:
        click.echo(f'Done installing {addon.metadata["title"]}')
        show_warnings(ctx)
        return

    added = asyncio.run(local.install_deps([installed_addon], api, _progress, opt=opt))

    click.echo(f'\nDone installing {addon.metadata["title"]} and {added} dependence(s)')
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
        opt = ctx.obj['config'].getboolean(f'{ctx.obj["game"]}.addons', 'optional')

    if addon is None:
        addon = local.installed
    else:
        addon = api.find(addon, local)
        if isinstance(addon, list):
            addon = local.filter_installed(addon)

    if not addon:
        click.echo('No corresponding addon found.')
        show_warnings(ctx)
        return

    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon)
    if not addon:
        click.echo('Nothing do to.')
        return

    installed_addon = local.find_installed(addon)
    if addon is None:
        show_warnings(ctx)
        return

    nremoved = asyncio.run(local.remove(installed_addon, deps=clean_deps, opt=opt))

    if not clean_deps:
        click.echo('Addon removed.')
    else:
        click.echo(f'Addon and {nremoved} unused dependence(s) removed.')

    show_warnings(ctx)


@main.command()
@click.option('--auto-deps/--no-auto-deps', default=True, help='Automatically install new/missing dependences')
@click.option('--opt/--no-opt', default=False, help='Include optional dependences')
@click.pass_context
def update(ctx, auto_deps, opt):
    """ Find out-of-date and missing addons and install them """
    api = ctx.obj['api']
    local = ctx.obj['local']

    if opt is None:
        opt = ctx.obj['config'].getboolean(f'{ctx.obj["game"]}.addons', 'optional')

    updates, added = asyncio.run(local.update(api, _progress, opt=opt, deps=auto_deps))

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
def cleanup(ctx, opt):
    """ Find and uninstall an addon """
    api = ctx.obj['api']
    local = ctx.obj['local']

    if opt is None:
        opt = ctx.obj['config'].getboolean(f'{ctx.obj["game"]}.addons', 'optional')

    nremoved = asyncio.run(local.remove_unused_deps(opt=opt))

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
    except:
        click.echo("Can't check stable API status")

    try:
        alpha = API.alpha(ctx.obj['config'])
        if alpha.globalconf['API']['Version'] == 'ALPHA':
            alpha_ok = True
        else:
            click.echo(f'Version {API.version + 1} seems to have come out of alpha')
    except:
        click.echo("Can't check alpha API status")

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


@main.command()
@click.pass_context
def clear_caches(ctx):
    api = ctx.obj['api']
    local = ctx.obj['local']
    api.reset()
    asyncio.run(local.scan(api))

    click.echo('Caches cleared.')
    show_warnings(ctx)
