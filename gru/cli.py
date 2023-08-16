import warnings
import datetime
import pathlib
import shutil
import click
import math
import sys
import os
import click_repl
import prompt_toolkit.history as prompt_history

from .config import load_config, save_config, user_cache
from .api import API
from .installed import Folder


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


def _display_addon(ctx, n, addon, width, local):
    """ Show addon info from the API endpoint """
    click.echo()
    click.echo(f'{n:{width}}{addon["UIName"]} (id {addon["UID"]}){"  [installed]" if local else ""}')
    pfx = ' ' * width
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    # TODO: move to an “Addon” object
    infos = [
        f'Author: {addon["UIAuthorName"]}', f'Version: {addon["UIVersion"]}',
        f'Updated: {datetime.datetime.fromtimestamp(addon["UIDate"] / 1000).strftime("%c")}',
        f'Category: {" > ".join(ctx.obj["api"].cat_name_hierarchy(addon["UICATID"]))}',
        f'Downloads: {addon["UIDownloadTotal"]} [{addon["UIDownloadMonthly"]} / Month]',
        f'Favorites: {addon["UIFavoriteTotal"]}',
        f'Directory: {addon["slug"] if local is None else local.root}',
    ]
    click.echo(pfx + sep.join(infos[:4]))
    click.echo(pfx + sep.join(infos[4:]))
    click.echo(pfx + addon['UIFileInfoURL'])


def _display_folder(n, folder, width):
    """ Show addon info from a local folder that was not matched with the API endpoint """
    click.echo()
    click.echo(f'{n:{width}}{folder.metadata["Title"]} (id not found)  [installed]')
    pfx = ' ' * width
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    # TODO: move to an “Addon” object
    click.echo(pfx + sep.join([f'Author: {folder.metadata["UIAuthorName"]}', f'Version: {folder["Version"]}']))
    if 'Description' in folder.metadata:
        click.echo(pfx + f'Description: {folder.metadata["Description"]}')
    click.echo(pfx + f'NB: this add-on may be deprecated')


def _display_unknown(n, folder, width):
    """ Show a missing and unresolved dependency """
    click.echo()
    click.echo(f'{n:{width}}{folder} (id not found)')
    click.echo(' ' * width + f'NB: this add-on may be deprecated')


def _display(ctx, results, num_from=0):
    """ Show a list of addons """
    live = ctx.obj['api']

    width = math.ceil(math.log(len(results), 10))
    width += 2

    for n, addon in enumerate(results, num_from + 1):
        num = f'{n:{width - 2}}: ' if width > 2 else ''
        if isinstance(addon, Folder):
            _display_folder(num, addon, width)
        elif isinstance(addon, str):
            _display_unknown(num, addon, width)
        else:
            local = Folder.find_installed(addon['slug'], ctx.obj['installed'])
            _display_addon(ctx, num, addon, width, local)

    click.echo()


def _confirm(query):
    answer = click.confirm(query, prompt_suffix=':\n> ')
    click.echo()
    return answer


def _prompt_addon(ctx, results, show_batch=10):
    """ Pick an addon from a list of addons """
    if not results:
        click.echo('No addon found')
        return None

    click.echo(' '.join([
        'No exact matches.',
        f'{len(results)} possible results:' if len(results) > 1 else 'Only approximate result:'
    ]))

    _display(ctx, results[:show_batch])

    if len(results) == 1:
        return results[0] if _confirm('Confirm removal?') else None

    for shown in range(show_batch, len(results), show_batch):
        answer = click.prompt(f'Select (1-{shown}, 0 cancels, empty continues)', prompt_suffix=':\n> ', default=-1,
                              type=click.IntRange(-1, shown + 1), show_default=False)
        if answer >= 0:
            break

        _display(ctx, results[shown:shown+show_batch], num_from=shown)
    else:
        answer = click.prompt(f'Select (1-{len(results)}, 0 cancels)', prompt_suffix=':\n> ', type=click.IntRange(0, len(results) + 1))

    click.echo()
    try:
        answer = int(answer)
    except ValueError:
        return None

    if 1 <= answer <= len(results):
        return results[answer - 1]


def _progress(size, message):
    return click.progressbar(length=size, label=message)


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
@click.pass_context
def main(ctx, config_file=None):
    ctx.ensure_object(dict)
    ctx.obj['warnings'] = ctx.with_resource(warnings.catch_warnings(record=True, category=UserWarning))

    config = ctx.obj['config'] = load_config(config_file)

    root = config.get('ESO.addons', 'root')
    if not root or not pathlib.Path(root).exists():
        click.echo('ESO addons directory not found!')
        root = click.prompt('Path to addons directory', prompt_suffix=':\n> ',
                            type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
        config.set('ESO.addons', 'root', str(root.resolve()))
        save_config(config_file)

    live = ctx.obj['api'] = API(config)
    installed = ctx.obj['installed'] = Folder.scan(live)

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
def process_result(ctx, result, config_file):
    show_warnings(ctx)


@main.command()
@click.argument('addon')
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--opt/--no-opt', default=False, help='Include optional dependencies')
@click.pass_context
def get(ctx, addon, auto_deps=True, opt=False):
    """ Find, download, and install an addon """
    live = ctx.obj['api']
    installed = ctx.obj['installed']

    addon = live.find(addon, installed)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon)
    if not addon:
        show_warnings(ctx)
        return

    folder = Folder.find_installed(addon['slug'], installed)

    # Try to reuse an existing install dir
    if folder is not None:
        if not _confirm(f'Addon found at {folder.root}, update?'):
            click.echo('Not removing')
            return
    else:
        folder = Folder(addon['slug'], id=int(addon['UID']))

    folder.unpack(_progress)

    if not auto_deps:
        click.echo(f'Done installing {addon["UIName"]}')
        show_warnings(ctx)
        return

    deps = [folder]
    while newdeps := Folder.all_missing_deps(deps, installed, opt=opt):
        deps.clear()
        for add in newdeps:
            # Do not check if installed as it’s a missing dep
            folder = Folder(add)
            folder.lookup(live)
            folder.unpack(_progress)
            deps.append(folder)

    click.echo(f'\nDone installing {addon["UIName"]} and dependencies')
    show_warnings(ctx)


@main.command()
@click.argument('addon', required=False)
@click.option('--clean-deps/--no-clean-deps', default=False)
@click.pass_context
def remove(ctx, addon, clean_deps=False):
    """ Find and uninstall an addon """
    live = ctx.obj['api']
    installed = ctx.obj['installed']

    if addon is None:
        addon = [live.addon(folder.id) if hasattr(folder, 'id') else folder for folder in installed]
    else:
        addon = live.find(addon, installed, local_only=True)

    if not addon:
        click.echo('No corresponding addon found')
        show_warnings(ctx)
        return

    if isinstance(addon, list):
        addon = _prompt_addon(ctx, addon)
    if not addon:
        return

    folder = Folder.find_installed(addon['slug'], installed)
    if folder is None:
        show_warnings(ctx)
        return

    folder.remove()

    if not clean_deps:
        click.echo('Addon removed.')
        show_warnings(ctx)
        return

    remains = [inst for inst in installed if inst.root != folder.root]
    for lib in Folder.all_unused_deps(remains, remains):
        lib.remove()

    click.echo('Addon and unused dependencies removed.')
    show_warnings(ctx)


@main.command()
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--opt/--no-opt', default=False, help='Include optional dependencies')
@click.pass_context
def update(ctx, auto_deps, opt):
    """ Find out-of-date and missing addons and install them """
    live = ctx.obj['api']
    installed = ctx.obj['installed']

    updates = 0
    for folder in installed:
        if folder.check_update(live):
            updates += 1
            folder.unpack(_progress)

    if not auto_deps:
        click.echo(f'Updated {updates} addon(s)' if updates else 'Nothing to do')
        show_warnings(ctx)
        return

    added = 0
    deps = installed
    while newdeps := Folder.all_missing_deps(deps, installed, opt=opt):
        deps.clear()
        for addon in newdeps:
            added += 1
            # Do not check if installed as it’s a missing dep
            folder = Folder(addon)
            folder.lookup(live)
            folder.unpack(_progress)
            deps.append(folder)

    if updates + added:
        click.echo(f'Updated {updates} addon(s) and installed {added} dependencies')
    else:
        click.echo('Nothing to do')
    show_warnings(ctx)

@main.command()
@click.pass_context
def check_api_release(ctx, hidden=True):
    try:
        alpha = API(ctx.obj['config'], stable=False)
        if alpha.globalconf['API']['Version'] != 'ALPHA':
            click.echo(f'Version {API.version + 1} seems to have come out of alpha')
    except:
        pass

    if ctx.obj['api'].globalconf['API']['Version'] != 'LIVE':
        click.echo(f'Error: Version {ctx.obj["config"].get("api", "version")} has been retired')


@main.command()
@click.argument('term')
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
@click.pass_context
def search(ctx, term, max_=10):
    live = ctx.obj['api']
    search = live.search(term, maxlen=max_)

    if search:
        click.echo(len(search), 'results:')
        _display(ctx, search)
    else:
        click.echo('No matches.')


@main.command('list', help='list installed add-ons')
@click.pass_context
def list_(ctx):
    live = ctx.obj['api']
    installed = ctx.obj['installed']

    if not installed:
        click.echo('No addons installed.')
        return

    click.echo(f'Found {len(installed)} addon(s):')
    _display(ctx, [live.addon(folder.id) if hasattr(folder, 'id') else folder for folder in installed])

@main.command(help='List missing dependencies')
@click.option('--opt/--no-opt', help='include optional dependencies')
@click.pass_context
def miss(ctx, opt):
    live = ctx.obj['api']
    installed = ctx.obj['installed']
    missing = Folder.all_missing_deps(installed, installed, opt=opt)

    if not missing:
        click.echo('No missing dependencies!')
        return

    click.echo(f'{len(missing)} missing dependencies:')
    found, not_found = [], []
    for dep in missing:
        try:
            found.append(live.dir(dep))
        except ValueError:
            not_found.append(dep)
    _display(ctx, found + not_found)

    if found:
        click.echo(f'Run update to fetch resolved missing dependencies')


@main.command()
@click.pass_context
def clear_caches(ctx):
    live = ctx.obj['api']
    live.reset()
    ctx.obj['installed'] = Folder.scan(live)

    click.echo('Caches cleared.')
