import datetime
import pathlib
import shutil
import click
import math
import sys

from .config import config, load_config, save_config
from .api import API
from .installed import Folder


class SectionedHelpGroup(click.Group):
    """ Sections commands into help groups """

    _cmd_shortcuts = {'rm': 'remove', 'up': 'update', 'ls': 'list', 's': 'search', 'cc': 'clear-cache'}

    @classmethod
    def _cmd_group(cls, cmd, group):
        return (cmd.name in {'get': 'get', 'remove': 'rm', 'update': 'up'}) == (group == 'main')

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self._cmd_shortcuts.get(cmd_name, cmd_name))

    def format_commands(self, ctx, formatter):
        shortcuts = {long: short for short, long in self._cmd_shortcuts.items()}
        for group in ['main', 'extra']:
            rows = []
            for subcommand in self.list_commands(ctx):
                cmd = self.get_command(ctx, subcommand)
                if cmd is None or not self._cmd_group(cmd, group):
                    continue
                short = shortcuts.get(subcommand)
                rows.append((f'{subcommand} [{short}]' if short else subcommand, cmd.short_help or ''))

            if rows:
                with formatter.section(f'{group.title()} commands'):
                    formatter.write_dl(rows)


def _display_addon(n, addon, width, local, live):
    """ Show addon info from the API endpoint """
    click.echo()
    click.echo(f'{n:{width}}{addon["UIName"]} (id {addon["UID"]}){"  [installed]" if local else ""}')
    pfx = ' ' * width
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    infos = [
        f'Author: {addon["UIAuthorName"]}', f'Version: {addon["UIVersion"]}',
        f'Updated: {datetime.datetime.fromtimestamp(addon["UIDate"] / 1000).strftime("%c")}',
        f'Category: {" > ".join(live.cat_name_hierarchy(addon["UICATID"]))}',
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
    click.echo(pfx + sep.join([f'Author: {folder.metadata["UIAuthorName"]}', f'Version: {folder["Version"]}']))
    if 'Description' in folder.metadata:
        click.echo(pfx + f'Description: {folder.metadata["Description"]}')
    click.echo(pfx + f'NB: this add-on may be deprecated')


def _display_unknown(n, folder, width):
    """ Show a missing and unresolved dependency """
    click.echo()
    click.echo(f'{n:{width}}{folder} (id not found)')
    click.echo(' ' * width + f'NB: this add-on may be deprecated')


def _display(results, installed=None):
    """ Show a list of addons """
    live = API()
    if installed is None:
        installed = Folder.scan(live)

    width = math.ceil(math.log(len(results), 10))
    width += 2

    for n, addon in enumerate(results, 1):
        num = f'{n:{width - 2}}: ' if width > 2 else ''
        if isinstance(addon, Folder):
            _display_folder(num, addon, width)
        elif isinstance(addon, str):
            _display_unknown(num, addon, width)
        else:
            local = Folder.find_installed(addon['slug'], installed)
            _display_addon(num, addon, width, local, live)

    click.echo()


def _confirm(query):
    answer = click.confirm(query, prompt_suffix=':\n> ')
    click.echo()
    return answer


def _prompt_addon(results, installed=None):
    """ Pick an addon from a list of addons """
    if not results:
        click.echo('No addon found')
        return None

    click.echo(' '.join([
        'No exact matches.',
        f'{len(results)} possible results:' if len(results) > 1 else 'Only approximate result:'
    ]))
    _display(results, installed=installed)

    if len(results) == 1:
        return results[0] if _confirm('Confirm removal?') else None

    answer = click.prompt(f'Select (1-{len(results)}, 0 cancels)', prompt_suffix=':\n> ', type=int)

    click.echo()
    try:
        answer = int(answer)
    except ValueError:
        return None

    if 1 <= answer <= len(results):
        return results[answer - 1]


def _progress(size, message):
    return click.progressbar(length=size, label=message)


@click.group(cls=SectionedHelpGroup)
@click.option('--config', 'config_file', help='path to config file',
              type=click.Path(dir_okay=False, writable=True), default=None)
def main(config_file=None):
    load_config(config_file)

    root = config.get('ESO.addons', 'root')
    if not root or not pathlib.Path(root).exists():
        click.echo('ESO addons directory not found!')
        root = click.prompt('Path to addons directory', prompt_suffix=':\n> ',
                            type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
        config.set('ESO.addons', 'root', str(root.resolve()))



@main.result_callback()
def process_result(result, config_file):
    save_config(config_file)


@main.command()
@click.argument('addon')
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--opt/--no-opt', default=False, help='Include optional dependencies')
def get(addon, auto_deps=True, opt=False):
    """ Find, download, and install an addon """
    live = API()
    installed = Folder.scan(live)

    addon = live.find(addon, installed)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(addon, installed=installed)
    if not addon:
        return

    folder = Folder.find_installed(addon['slug'], installed)

    # Try to reuse an existing install dir
    if folder is not None:
        if not _confirm(f'Addon found at {folder.root}, update?'):
            return
    else:
        folder = Folder(addon['slug'], id=int(addon['UID']))

    folder.unpack(_progress)

    if not auto_deps:
        click.echo(f'Done installing {addon["UIName"]}')
        return

    deps = [folder]
    while newdeps := Folder.all_missing_deps(deps, installed, opt=opt):
        deps.clear()
        for addon in newdeps:
            # Do not check if installed as it’s a missing dep
            folder = Folder(addon)
            folder.lookup(live)
            folder.unpack(_progress)
            deps.append(folder)


@main.command()
@click.argument('addon')
@click.option('--clean-deps/--no-clean-deps', default=False)
def remove(addon, clean_deps=False):
    """ Find and uninstall an addon """
    live = API()
    installed = Folder.scan(live)

    addon = live.find(addon, installed, local_only=True)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(addon, installed=installed)
    if not addon:
        return

    folder = Folder.find_installed(addon['slug'], installed)
    if folder is None:
        return

    folder.remove()

    if not clean_deps:
        return

    remains = [inst for inst in installed if inst.root != folder.root]
    for lib in Folder.all_unused_deps(remains, remains):
        lib.remove()


@main.command()
@click.option('--auto-deps/--no-auto-deps', default=True)
@click.option('--opt/--no-opt', default=False, help='Include optional dependencies')
def update(auto_deps, opt):
    """ Find out-of-date and missing addons and install them """
    live = API()
    installed = Folder.scan(live)

    for folder in installed:
        if folder.check_update(live):
            folder.unpack(_progress)

    if not auto_deps:
        return

    deps = installed
    while newdeps := Folder.all_missing_deps(deps, installed, opt=opt):
        deps.clear()
        for addon in newdeps:
            # Do not check if installed as it’s a missing dep
            folder = Folder(addon)
            folder.lookup(live)
            folder.unpack(_progress)
            deps.append(folder)


@main.command()
def check_api_release(hidden=True):
    try:
        alpha = API(stable=False)
        if alpha.globalconf['api']['version'] != 'ALPHA':
            click.echo(f'Version {API.version + 1} seems to have come out of alpha')
    except:
        pass

    live = API()
    if live.globalconf['API']['Version'] != 'LIVE':
        click.echo(f'Error: Version {API.version} has been retired')


@main.command()
@click.argument('term')
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
def search(term, max_=10):
    live = API()
    search = live.search(term, maxlen=max_)

    if search:
        click.echo(len(search), 'results:')
        _display(search)
    else:
        click.echo('No matches.')


@main.command('list', help='List installed add-ons')
def list_():
    live = API()
    installed = Folder.scan(live)

    _display([live.addon(folder.id) for folder in installed if hasattr(folder, 'id')], installed=installed)

@main.command(help='List missing dependencies')
@click.option('--opt/--no-opt', help='include optional dependencies')
def miss(opt):
    live = API()
    installed = Folder.scan(live)
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
    _display(found + not_found, installed=installed)

    if found:
        click.echo(f'Run update to fetch resolved missing dependencies')


@main.command()
def clear_cache():
    API.reset()
