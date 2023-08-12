from .api import API
from .installed import Folder
import datetime
import pathlib
import shutil
import click
import math


live = API()
root = pathlib.Path('/tmp/test')


def _display_addon(n, addon, width, local):
    click.echo()
    click.echo(f'{n:{width}}: {addon["UIName"]} (id {addon["UID"]}){"  [installed]" if local else ""}')
    pfx = ' ' * (width + 2)
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
    click.echo()
    click.echo(f'{n:{width}}: {folder.metadata["Title"]} (id not found)  [installed]')
    pfx = ' ' * (width + 1)
    sep = ' |  '
    # Based on verbosity level, only click.echo a number of those:
    click.echo(pfx, sep.join([f'Author: {folder.metadata["UIAuthorName"]}', f'Version: {folder["Version"]}']))
    if 'Description' in folder.metadata:
        click.echo(pfx, f'Description: {folder.metadata["Description"]}')


def _display_unknown(n, folder, width):
    click.echo()
    click.echo(f'{n:{width}}: {folder} (id not found)')


def _display(results, installed=None):
    if installed is None:
        installed = Folder.scan(root, live)

    width = max(1, math.ceil(math.log(len(results), 10)))
    for n, addon in enumerate(results, 1):
        if isinstance(addon, Folder):
            _display_folder(n, addon, width)
        elif isinstance(addon, str):
            _display_unknown(n, addon, width)
        else:
            local = Folder.find_installed(addon['slug'], installed)
            _display_addon(n, addon, width, local)

    click.echo()


def _prompt_addon(results, installed=None):
    click.echo(f'No exact matches. {len(results)} possible results:')
    _display(results, installed=installed)

    answer = click.prompt(f'Select (1-{len(results)}, 0 cancels)', prompt_suffix=':\n> ', type=int)

    click.echo()
    try:
        answer = int(answer)
    except ValueError:
        return None

    if 1 <= answer <= len(results):
        return results[answer - 1]


def _confirm(query):
    answer = click.confirm(query, prompt_suffix=':\n> ')
    click.echo()
    return answer[:1].lower() == 'y'


@click.group()
def main():
    # TODO: load/set conf
    pass


@main.command()
def check_releases():
    try:
        alpha = API(stable=False)
        if alpha.globalconf['api']['version'] != 'ALPHA':
            click.echo(f'Version {API.version + 1} seems to have come out of alpha')
    except:
        pass

    if live.globalconf['API']['Version'] != 'LIVE':
        click.echo(f'Error: Version {API.version} has been retired')


@main.command()
@click.argument('term')
@click.option('-m', '--max', 'max_', help='max number of matches', default=10)
def search(term, max_=10):
    search = live.search(term, maxlen=max_)

    if search:
        click.echo(len(search), 'results:')
        _display(search)
    else:
        click.echo('No matches.')


@main.command('list')
def list_():
    installed = Folder.scan(root, live)

    _display([live.addon(folder.id) for folder in installed if hasattr(folder, 'id')], installed=installed)


@main.command()
@click.argument('addon')
@click.option('--auto-deps/--no-auto-deps')
def install(addon, auto_deps=False):
    installed = Folder.scan(root, live)

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
        folder = Folder(root / addon['slug'])
        folder.id = int(addon['UID'])

    template = 'https://cdn.esoui.com/downloads/file{id}/'
    folder.unpack(template, lambda size, message: click.progressbar(length=size, label=message))
    click.echo(f'Done installing {addon["UIName"]}')


@main.command()
@click.argument('addon')
def remove(addon):
    installed = Folder.scan(root, live)

    addon = live.find(addon, installed, local_only=True)
    if not addon:
        click.echo('No corresponding addon found')
        click.echo()
    if isinstance(addon, list):
        addon = _prompt_addon(addon, installed=installed)
    if not addon:
        return

    folder = Folder.find_installed(addon['slug'], installed)

    # Try to reuse an existing install dir
    if folder is None:
        click.echo('Addon not found among installed addons')
    elif not _confirm(f'Addon found at {folder.root}, confirm removal?'):
        pass
    else:
        shutil.rmtree(folder.root)


@main.command()
def missing_deps():
    installed = Folder.scan(root, live)
    missing = {}
    for addon in installed:
        deps = addon.missing_deps(installed, opt=False)
        missing.update({dep: max(ver, missing.get(dep, ver)) for dep, ver in deps.items()})

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

    if found and _confirm('Install missing dependencies?'):
        template = 'https://cdn.esoui.com/downloads/file{id}/'
        for addon in found:
            folder = Folder(root / addon['slug'])
            folder.id = int(addon['UID'])
            folder.unpack(template, lambda size, message: click.progressbar(length=size, label=message))
            click.echo(f'Done installing {addon["UIName"]}')
    # TODO: make this have a proper _display() + offer to install missing deps


@main.command()
def clear_cache():
    API.reset()
