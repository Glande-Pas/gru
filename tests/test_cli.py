"""Smoke tests for the gru CLI via click's CliRunner.

main() builds its API/Folder through cli.build_app(), which TestWithRealAddons monkeypatches
to a stub -- letting these commands run against real installed addons with zero network."""

import pathlib

import click
import pytest
from click.testing import CliRunner

import gru.cli as cli_mod
from gru.cli import main, TermDisplay
from gru.config import load_config

from .conftest import StubAPI, make_folder, make_installed, make_addon_info


class TestRenderEsoText:
    def test_closed_tag(self):
        result = TermDisplay._render_eso_text('|cFF0000Red|r Text')
        assert 'Red' in result and 'Text' in result
        assert '\x1b[' in result  # got styled, not left as raw markup

    def test_missing_closing_tag_auto_closes_at_end_of_string(self):
        result = TermDisplay._render_eso_text('|cFF0000Unterminated Red Text')
        assert 'Unterminated Red Text' in result
        assert '|c' not in result and '|r' not in result

    def test_missing_closing_tag_auto_closes_before_next_color_code(self):
        result = TermDisplay._render_eso_text('|cFF0000Red|c00FF00Green')
        assert 'Red' in result and 'Green' in result
        assert '|c' not in result and '|r' not in result

    def test_plain_text_unaffected(self):
        assert TermDisplay._render_eso_text('Plain title, no markup') == 'Plain title, no markup'


@pytest.fixture
def cli_config(tmp_path):
    """A config file pointing at a real, empty addons folder."""
    addons_root = tmp_path / 'AddOns'
    addons_root.mkdir()
    config_file = tmp_path / 'gru.ini'
    config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
    return config_file


def invoke(config_file, args, input=None):
    runner = CliRunner()
    return runner.invoke(main, ['--config', str(config_file), *args], input=input)


class TestNoNetworkSmokeTests:
    def test_list_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['list'])
        assert result.exit_code == 0
        assert 'No addons installed.' in result.output

    def test_remove_with_no_addons_installed_and_no_term(self, cli_config):
        result = invoke(cli_config, ['remove'])
        assert result.exit_code == 0
        assert 'No corresponding addon found.' in result.output

    def test_export_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['export'])
        assert result.exit_code == 0
        assert 'No addons installed.' in result.output

    def test_config_get_and_set_roundtrip(self, cli_config):
        result = invoke(cli_config, ['config', 'set', 'addons.optional', 'on'])
        assert result.exit_code == 0

        result = invoke(cli_config, ['config', 'get', 'addons.optional'])
        assert result.exit_code == 0
        assert result.output.strip() == 'on'  # fully-qualified entry -> bare value, no repr

    def test_config_set_rejects_bad_boolean(self, cli_config):
        result = invoke(cli_config, ['config', 'set', 'addons.optional', 'sideways'])
        assert result.exit_code == 0  # command catches the error, doesn't propagate it
        assert 'must be "on" or "off"' in result.output

    def test_cleanup_with_no_addons_installed(self, cli_config):
        result = invoke(cli_config, ['cleanup'])
        assert result.exit_code == 0
        assert 'Removed 0 unused dependence(s).' in result.output


class TestWithRealAddons:
    """Previously untestable without network: build_app() is monkeypatched to a stub API,
    so these commands run against real installed addons instead of only an empty folder."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_list_shows_installed_addon(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        result = invoke(cli_app['config_file'], ['list'])
        assert result.exit_code == 0
        assert 'My Addon' in result.output

    def test_remove_by_exact_name(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Title='My Addon')
        result = invoke(cli_app['config_file'], ['remove', 'my addon', '--no-clean-deps'], input='y\n')
        assert result.exit_code == 0
        assert 'Removed addon My Addon.' in result.output
        assert not (cli_app['addons_root'] / 'MyAddon').exists()

    def test_remove_no_match_falls_through_without_crashing(self, cli_app):
        """Regression guard: used to crash reaching Folder.search() (dict_values.values())."""
        make_installed(cli_app['addons_root'], 'MyAddon')
        result = invoke(cli_app['config_file'], ['remove', 'totally-unrelated'])
        assert result.exit_code == 0
        assert 'No corresponding addon found.' in result.output

    def test_export_writes_installed_addons(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Version='3')
        result = invoke(cli_app['config_file'], ['export'])
        assert result.exit_code == 0
        exported = (cli_app['config_dir'] / 'ESO' / 'addons.txt').read_text()
        assert 'MyAddon = 3' in exported


class TestParentSuffixWording:
    """The ', <relation> X' suffix after 'installed' uses a distinct connector per relationship:
    'listed online as' (title differs), 'part of' (multi-folder bundle), 'bundled inside'
    (unmatched folder nested in a matched one) -- not a single overloaded 'as X'."""

    def _list_output(self, monkeypatch, addons_root, config_file, api):
        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            local = make_folder(addons_root)
            local.scan(api)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return invoke(config_file, ['list']).output

    def _single_addon_setup(self, tmp_path, manifest_title, upstream_title):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'MyAddon', Title=manifest_title)
        upstream = make_addon_info(id_=1, title=upstream_title, directories=['MyAddon'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                return upstream

        return addons_root, config_file, LinkableApi()

    def test_listed_online_as_for_markup_only_difference(self, monkeypatch, tmp_path):
        """Real case: HideGroupNecro's manifest Title is 'HideGroup|c5050ffNecro|r'. Byte-for-byte
        comparison correctly detects this differs from the plain upstream title -- and since color
        now defaults on (see TestNoColor), the main title actually renders 'Necro' in color, giving
        a real visual cue for the difference instead of two seemingly-identical strings."""
        addons_root, config_file, api = self._single_addon_setup(tmp_path, 'HideGroup|c5050ffNecro|r', 'HideGroupNecro')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert ', listed online as HideGroupNecro' in output
        assert '\x1b[' in output

    def test_listed_online_as_when_titles_genuinely_differ(self, monkeypatch, tmp_path):
        addons_root, config_file, api = self._single_addon_setup(tmp_path, 'MyAddon', 'Completely Different Name')
        output = self._list_output(monkeypatch, addons_root, config_file, api)
        assert ', listed online as Completely Different Name' in output

    def test_part_of_for_multi_folder_bundle(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'MainPart', Title='MainPart')
        make_installed(addons_root, 'ExtraPart', Title='ExtraPart')
        upstream = make_addon_info(id_=1, title='BundleName', directories=['MainPart', 'ExtraPart'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                return upstream

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', part of BundleName' in output

    def test_bundled_inside_for_unmatched_nested_folder(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        make_installed(addons_root, 'ParentAddon', Title='ParentAddon')
        make_installed(addons_root / 'ParentAddon', 'ChildLib', Title='ChildLib')
        upstream = make_addon_info(id_=1, title='ParentAddon', directories=['ParentAddon'])

        class LinkableApi(StubAPI):
            def dir(self, name):
                if name == 'ParentAddon':
                    return upstream
                raise FileNotFoundError(name)

        output = self._list_output(monkeypatch, addons_root, config_file, LinkableApi())
        assert ', bundled inside ParentAddon' in output


class TestNoColor:
    """--no-color / default-color-on, driven through ctx.color rather than per-callsite."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        make_installed(addons_root, 'MyAddon', Title='|c5050ffMyAddon|r')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return config_file

    def test_color_on_by_default_even_though_output_is_captured(self, cli_app):
        """CliRunner-captured output isn't a real tty, so click's own auto-detection would
        normally strip ANSI codes here -- ctx.color = True overrides that."""
        result = invoke(cli_app, ['list'])
        assert result.exit_code == 0
        assert '\x1b[' in result.output

    def test_no_color_flag_strips_styling(self, cli_app):
        result = invoke(cli_app, ['--no-color', 'list'])
        assert result.exit_code == 0
        assert '\x1b[' not in result.output
        assert 'MyAddon' in result.output

    def test_no_color_propagates_from_group_to_subcommand_context(self, cli_app):
        """ctx.color is set on main()'s own Context, but list runs in a child Context --
        confirms it's actually inherited, not just set on the group callback's own context."""
        result = invoke(cli_app, ['--no-color', 'list'])
        assert click.unstyle(result.output) == result.output


class TestDiffCommand:
    """gru diff: only the network layer (requests.head/get) is faked -- everything else
    (Folder.unmodified_addon -> unpack -> _inspect_bundle -> InstalledAddon, addon_diff) runs for
    real. Regression guard for passing the wrong type (InstalledAddon instead of its .infos
    AddonInfo) into unmodified_addon(), which crashed with AttributeError deep inside
    InstalledAddon.link() -> infos.register(self)."""

    @staticmethod
    def _zip_bytes(entries: dict[str, str]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for fname, content in entries.items():
                zf.writestr(fname, content)
        return buf.getvalue()

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon')
        (installed.folder / 'Data.lua').write_text('old = 1\n')
        upstream = make_addon_info(id_=1, title='MyAddon', version='1.0', directories=['MyAddon'])
        installed.link(upstream)

        zip_bytes = self._zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 1.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'old = 2\n',  # the "unmodified" upstream differs -> real diff to save
        })

        class FakeResponse:
            content: bytes  # only set on responses that carry a body

            def __init__(self, **attrs):
                self.__dict__.update(attrs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=1024):
                yield self.content

        import gru.install as install_mod
        headers = {'content-length': str(len(zip_bytes))}
        monkeypatch.setattr(install_mod.requests, 'head',
                            lambda url, allow_redirects=True: FakeResponse(headers=headers))
        monkeypatch.setattr(install_mod.requests, 'get',
                            lambda url, stream=True, allow_redirects=True: FakeResponse(content=zip_bytes))
        monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(tmp_path, *parts))
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            for addon in local.installed:
                addon.link(upstream)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_diff_does_not_crash_and_saves_a_patch(self, cli_app):
        result = invoke(cli_app['config_file'], ['diff', 'MyAddon'], input='y\n')
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'register' not in result.output
        assert 'Changes saved under' in result.output
        assert (cli_app['config_dir'] / 'ESO' / 'MyAddon.patch').exists()


def _touch_cache_path(base: pathlib.Path, *parts: str) -> pathlib.Path:
    path = base / 'cache'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _touch_config_path(base: pathlib.Path, *parts: str) -> pathlib.Path:
    path = base / 'config'
    path = path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestUpdateCommand:
    """gru update: only the network layer (requests.head/get) is faked -- Folder.update()
    (unpack -> _inspect_bundle -> InstalledAddon, install_deps) runs for real."""

    @staticmethod
    def _zip_bytes(entries: dict[str, str]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for fname, content in entries.items():
                zf.writestr(fname, content)
        return buf.getvalue()

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon', Version='1.0')
        upstream = make_addon_info(id_=1, title='MyAddon', version='2.0', directories=['MyAddon'])
        installed.link(upstream)

        zip_bytes = self._zip_bytes({
            'MyAddon/MyAddon.txt': '## Title: MyAddon\n## APIVersion: 100035\n## Version: 2.0\n## Author: Test\n',
            'MyAddon/Data.lua': 'new = 2\n',
        })

        class FakeResponse:
            content: bytes  # only set on responses that carry a body

            def __init__(self, **attrs):
                self.__dict__.update(attrs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, chunk_size=1024):
                yield self.content

        import gru.install as install_mod
        headers = {'content-length': str(len(zip_bytes))}
        monkeypatch.setattr(install_mod.requests, 'head',
                            lambda url, allow_redirects=True: FakeResponse(headers=headers))
        monkeypatch.setattr(install_mod.requests, 'get',
                            lambda url, stream=True, allow_redirects=True: FakeResponse(content=zip_bytes))
        monkeypatch.setattr(install_mod, 'user_cache', lambda *parts: _touch_cache_path(tmp_path, *parts))
        monkeypatch.setattr(install_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            for addon in local.installed:
                addon.link(upstream)
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_update_installs_new_version(self, cli_app):
        result = invoke(cli_app['config_file'], ['update'])
        assert result.exit_code == 0
        assert 'Updated 1 addon(s) and installed 0 dependence(s)' in result.output
        assert (cli_app['addons_root'] / 'MyAddon' / 'Data.lua').read_text() == 'new = 2\n'

    def test_update_with_nothing_to_do(self, cli_config):
        result = invoke(cli_config, ['update'])
        assert result.exit_code == 0
        assert 'Nothing to do' in result.output


class TestPatchCommand:
    """gru patch: addon_patch_file() applying a real, on-disk .patch file end-to-end."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        monkeypatch.setattr(cli_mod, 'user_config', lambda *parts: _touch_config_path(tmp_path, *parts))

        installed = make_installed(addons_root, 'MyAddon', Title='MyAddon')
        (installed.folder / 'Data.lua').write_text('old = 2\n')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root, 'config_dir': tmp_path / 'config'}

    def test_patch_applies_saved_patch_from_default_location(self, cli_app):
        from gru.patch import addon_diff

        patched = make_installed(cli_app['addons_root'].parent / 'patched_src', 'MyAddon')
        (patched.folder / 'Data.lua').write_text('patched = 3\n')
        unpatched = make_installed(cli_app['addons_root'].parent / 'unpatched_src', 'MyAddon')
        (unpatched.folder / 'Data.lua').write_text('old = 2\n')

        patch_dir = cli_app['config_dir'] / 'ESO'
        patch_dir.mkdir(parents=True)
        with (patch_dir / 'MyAddon.patch').open('w') as f:
            addon_diff(patched, unpatched, out=f)

        result = invoke(cli_app['config_file'], ['patch', 'MyAddon'], input='y\n')

        assert result.exit_code == 0
        assert 'Applied patch successfully.' in result.output
        assert (cli_app['addons_root'] / 'MyAddon' / 'Data.lua').read_text() == 'patched = 3\n'

    def test_patch_with_no_saved_changes_reports_nothing_to_apply(self, cli_app):
        result = invoke(cli_app['config_file'], ['patch', 'MyAddon'], input='y\n')
        assert result.exit_code == 0
        assert 'No saved changes to be re-applied.' in result.output


class TestSearchAndMissCommands:
    """Regression guards for bugs found while fixing pyright errors: search/miss called the
    undefined name _display() (NameError), and miss() caught the wrong exception type around
    api.dir() (it raises FileNotFoundError, not ValueError), so an unresolved dependency used
    to crash the whole command instead of being reported."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        found_dep = make_addon_info(id_=2, title='LibFoo', directories=['LibFoo'])

        class RichStubAPI(StubAPI):
            def search(self, term, tiebreakattr=None, maxlen=30):
                return [make_addon_info(id_=1, title='SearchResult', directories=['SearchResult'])]

            def dir(self, name):
                if name == 'LibFoo':
                    return found_dep
                raise FileNotFoundError(name)

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = RichStubAPI()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_search_with_results_does_not_crash(self, cli_app):
        result = invoke(cli_app['config_file'], ['search', 'anything'])
        assert result.exit_code == 0
        assert 'NameError' not in result.output
        assert 'SearchResult' in result.output

    def test_miss_reports_unresolvable_dependency_instead_of_crashing(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', DependsOn='LibFoo>=1 LibGone>=1')
        result = invoke(cli_app['config_file'], ['miss'])
        assert result.exit_code == 0
        assert 'NameError' not in result.output
        assert 'LibFoo' in result.output  # resolved -> shown via TermDisplay
        assert 'Not found online: LibGone' in result.output  # unresolved -> reported, not crashed


class TestFolderDisplayDescriptionField:
    """Regression: _folder()'s local `infos` was accidentally a tuple ([a], [b]) rather than a
    list, so infos.append(...) for a manifest's Description field raised AttributeError --
    hit for any unmatched local addon whose manifest declares one (a common manifest field)."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')

        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            api = StubAPI()  # empty -- addon stays unmatched, goes through _folder()
            local = make_folder(addons_root)
            local.scan(api)  # pyright: ignore[reportArgumentType] -- stub API, not a real gru.api.API
            return config, api, local

        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)
        return {'config_file': config_file, 'addons_root': addons_root}

    def test_list_with_description_field_does_not_crash(self, cli_app):
        make_installed(cli_app['addons_root'], 'MyAddon', Description='A cool addon')
        result = invoke(cli_app['config_file'], ['list'])
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'Description: A cool addon' in result.output


class TestGetCommand:
    """Regression guards for two bugs found while fixing pyright errors in get()'s loop."""

    @pytest.fixture
    def cli_app(self, monkeypatch, tmp_path):
        addons_root = tmp_path / 'AddOns'
        addons_root.mkdir()
        config_file = tmp_path / 'gru.ini'
        config_file.write_text(f'[ESO.addons]\nroot = {addons_root}\n')
        return {'addons_root': addons_root, 'config_file': config_file}

    def _wire_build_app(self, monkeypatch, addons_root, api):
        def fake_build_app(game, cfg_file):
            config = load_config(cfg_file)
            local = make_folder(addons_root)
            local.scan(api)
            return config, api, local
        monkeypatch.setattr(cli_mod, 'build_app', fake_build_app)

    def test_locally_matched_but_uncataloged_addon_is_skipped_not_installed(self, monkeypatch, cli_app):
        """Regression: api.find() can return a bare InstalledAddon (matched by local
        directory name only, not present online). Passing that into local.install() ->
        unpack() -> InstalledAddon.link() crashes: AttributeError, InstalledAddon has no
        register() (only AddonInfo does) -- same shape as the earlier `gru diff` bug."""
        installed = make_installed(cli_app['addons_root'], 'LocalOnly')

        class LocalOnlyApi(StubAPI):
            def find(self, val, local):
                return [installed]

        self._wire_build_app(monkeypatch, cli_app['addons_root'], LocalOnlyApi())
        result = invoke(cli_app['config_file'], ['get', 'LocalOnly', '--yes'])
        assert result.exit_code == 0
        assert 'AttributeError' not in result.output
        assert 'register' not in result.output
        assert 'already installed locally and not found online' in result.output

    def test_batch_mode_continues_cleanly_after_install_failure(self, monkeypatch, cli_app):
        """Regression: on KeyError from local.install() in batch (--yes) mode, the loop
        didn't `continue`, so `result` was referenced further down while still unbound
        from any successful assignment -- UnboundLocalError instead of the intended
        'Failed installing ...' message."""
        upstream = make_addon_info(id_=1, title='MyAddon', directories=['MyAddon'])

        class FindableApi(StubAPI):
            def find(self, val, local):
                return [upstream]

        self._wire_build_app(monkeypatch, cli_app['addons_root'], FindableApi())

        def broken_install(self, *args, **kwargs):
            raise KeyError('simulated failure')
        monkeypatch.setattr(cli_mod.Folder, 'install', broken_install)

        result = invoke(cli_app['config_file'], ['get', 'MyAddon', '--yes'])
        assert result.exit_code == 0
        assert 'UnboundLocalError' not in result.output
        assert 'Failed installing' in result.output


class TestFolderSearchTiebreak:
    def test_exactly_tied_candidates_do_not_crash_sorting(self, addon_root, folder):
        """Regression: Folder.search()'s default tiebreakattr=None used to become the
        list [None]; two candidates tied on (sum(matches), max(matches)) then forced
        sorted() to compare None < None to break the tie, raising TypeError."""
        make_installed(addon_root, 'AddonOne', Title='Tied Title')
        make_installed(addon_root, 'AddonTwo', Title='Tied Title')
        folder.scan()
        result = folder.search('Tied Title')
        assert len(result) >= 2
