"""modkit mod — mod lifecycle commands (create, import, deploy, undeploy)."""

import os

import click


def _resolve_game_data_dir(game: str) -> "Path":
    """Resolve game Data/ directory from env vars."""
    from pathlib import Path
    game_dir_var = f"{game.upper()}_DIR"
    game_dir = os.environ.get(game_dir_var, "")
    if not game_dir:
        from app.paths import get_app_root
        for root in (get_app_root(), Path.cwd()):
            env_path = root / ".env"
            if not env_path.is_file():
                continue
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{game_dir_var}="):
                    game_dir = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
            if game_dir:
                break
    if not game_dir:
        raise click.ClickException(f"{game_dir_var} is not set in .env or environment")
    return Path(game_dir) / "Data"


def _resolve_mod_game(ctx, mod_name: str) -> str:
    """Return game for mod_name: explicit --game flag > .game file > DEFAULT_GAME fallback."""
    if ctx.obj.get("game_explicit"):
        return ctx.obj["game"]
    from app.paths import get_app_root
    game_file = get_app_root() / "mods" / mod_name / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip()
    return ctx.obj["game"]


def _get_env(key: str, default: str = "") -> str:
    """Read env var, falling back to .env file."""
    val = os.environ.get(key, "")
    if val:
        return val
    from app.paths import get_app_root
    env_path = get_app_root() / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def _resolve_gitea_org(game: str) -> str:
    """Resolve Gitea org for a game: GITEA_ORG (explicit override) > GITEA_ORG_<GAME> > "".

    .env defines per-game orgs (GITEA_ORG_FO4, GITEA_ORG_SKYRIMSE, ...). The
    plain GITEA_ORG var is set dynamically by the UI when invoking subcommands.
    """
    explicit = _get_env("GITEA_ORG")
    if explicit:
        return explicit
    if game:
        return _get_env(f"GITEA_ORG_{game.upper()}")
    return ""


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def mod(ctx, game):
    """Mod lifecycle: create, import, deploy, undeploy."""
    if game is not None:
        ctx.obj["game"] = game


@mod.command()
@click.argument("name")
@click.option("--plugin-type", type=click.Choice(["esl", "esp", "esm"]), default="esl",
              help="Plugin type (default: esl)")
@click.option("--mod-prefix", default=None, help="Author prefix (default: MOD_PREFIX from .env)")
@click.option("--no-git", is_flag=True, help="Skip git/Gitea repo creation")
@click.pass_context
def create(ctx, name, plugin_type, mod_prefix, no_git):
    """Create a new mod with full directory structure."""
    from creation_lib.mod.scaffold import create_mod
    from app.paths import get_app_root

    game = ctx.obj["game"]
    if mod_prefix is None:
        mod_prefix = _get_env("MOD_PREFIX", "B21")

    try:
        mod_dir = create_mod(
            name,
            game=game,
            mod_prefix=mod_prefix,
            plugin_ext=plugin_type,
            init_git=not no_git,
            gitea_url=_get_env("GITEA_URL"),
            gitea_user=_get_env("GITEA_USER"),
            gitea_org=_resolve_gitea_org(game),
            gitea_token=_get_env("GITEA_TOKEN"),
            project_root=get_app_root(),
            on_progress=click.echo,
        )
        click.echo(f"\nNext steps:")
        click.echo(f"  1. Write YAML records in yaml/")
        click.echo(f"  2. Place assets in data/ (matching game Data/ layout)")
        click.echo(f"  3. Deploy: modkit mod deploy {name}")
    except (ValueError, FileExistsError) as e:
        raise click.ClickException(str(e))


@mod.command(name="import")
@click.argument("source_dir")
@click.argument("name", required=False, default=None)
@click.option("--mod-prefix", default=None, help="Author prefix (default: MOD_PREFIX from .env)")
@click.option("--no-git", is_flag=True, help="Skip git/Gitea repo creation")
@click.pass_context
def import_mod(ctx, source_dir, name, mod_prefix, no_git):
    """Import an external mod into the project."""
    from pathlib import Path
    from creation_lib.mod.scaffold import migrate_mod
    from app.paths import get_app_root

    game = ctx.obj["game"]
    if mod_prefix is None:
        mod_prefix = _get_env("MOD_PREFIX", "B21")

    try:
        mod_dir = migrate_mod(
            Path(source_dir),
            mod_name=name,
            game=game,
            game_dir=os.environ.get(f"{game.upper()}_DIR", ""),
            mod_prefix=mod_prefix,
            init_git=not no_git,
            gitea_url=_get_env("GITEA_URL"),
            gitea_user=_get_env("GITEA_USER"),
            gitea_org=_resolve_gitea_org(game),
            gitea_token=_get_env("GITEA_TOKEN"),
            project_root=get_app_root(),
            on_progress=click.echo,
        )
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise click.ClickException(str(e))


@mod.command()
@click.argument("name")
@click.option("--skip-build", is_flag=True, help="Skip .esp build (use existing)")
@click.option("--skip-pack", is_flag=True, help="Skip BA2 packing")
@click.option("--skip-compile", "--skip-papyrus-compile", "skip_papyrus_compile", is_flag=True,
              help="Skip Papyrus (.psc → .pex) compilation (use existing .pex)")
@click.option("--esp-only", is_flag=True, help="Deploy only the .esp")
@click.option("--source", default=None, help="Plugin binary, whole-plugin YAML/JSON, or authoring directory inside the mod folder (relative to that folder).")
@click.option("--preserve-xse-inis", is_flag=True, help="Keep existing XSE .ini files in the deploy target; copy missing INIs normally.")
@click.option("--no-esp", is_flag=True, help="Mod has no .esp (XSE-plugin-only); deploys mods/<name>/<XSE>/ to game Data/<XSE>/ (XSE = F4SE|SKSE|SFSE|NVSE|FOSE per the mod's .game)")
@click.option("--xbox", is_flag=True, help="Also create Xbox-format archives")
@click.option("--ps", is_flag=True, help="Also create PlayStation-format archives")
@click.option("--pc-max-res", type=int, default=0, help="Max texture resolution for PC (0 = unlimited)")
@click.option("--xbox-max-res", type=int, default=0, help="Max texture resolution for Xbox (0 = unlimited)")
@click.option("--ps-max-res", type=int, default=0, help="Max texture resolution for PlayStation (0 = unlimited)")
@click.option("--data-dir", default=None, help="Override game Data/ directory")
@click.option("--archive-max-size-gb", type=float, default=16.0,
              help="Maximum expanded archive size in GiB before splitting (default 16.0)")
@click.option("--expanded-archives", is_flag=True, default=False,
              help="Use family archive labels such as Meshes and Sounds instead of Main + Textures when possible.")
@click.option("--ba2-target", type=click.Choice(["auto", "og", "nextgen"]), default="auto", show_default=True,
              help="FO4 archive version: detect the deploy install, force v1 (og), or v8 (nextgen).")
@click.option("--all", "deploy_all", is_flag=True, help="Deploy main + all patch plugins")
@click.option("--patch", "patch_name", default=None, help="Deploy a specific patch plugin only")
@click.option("--loose", is_flag=True, help="Deploy as loose files (no BA2 packing); writes .loose_manifest.json")
@click.option("--dry-run", is_flag=True, help="Plan builds, copies, preserved INIs and removals without writing files.")
@click.pass_context
def deploy(ctx, name, skip_build, skip_pack, skip_papyrus_compile, esp_only, preserve_xse_inis, source, no_esp, xbox, ps, pc_max_res, xbox_max_res, ps_max_res, data_dir, archive_max_size_gb, expanded_archives, ba2_target, deploy_all, patch_name, loose, dry_run):
    """Deploy a mod: build .esp, pack BA2, copy to game Data.

    Use ``--loose`` to copy assets as loose files instead of packing BA2s. A
    ``.loose_manifest.json`` is written to the mod folder so a later
    ``modkit mod undeploy --loose`` knows exactly what to remove.

    Use ``--no-esp`` for XSE-plugin-only mods (no .esp, just a DLL under
    ``mods/<name>/<XSE>/Plugins/``). Skips the esp/papyrus/BA2 pipeline entirely
    and copies the <XSE>/ tree directly to the game's Data/<XSE>/. The
    ``<XSE>`` directory is F4SE/SKSE/SFSE/NVSE/FOSE per the mod's .game file.
    """
    from pathlib import Path
    from dataclasses import asdict
    from cli._output import output
    from creation_lib.build.archive_plan import gib_to_bytes
    from creation_lib.build.deployer import deploy_mod
    from app.paths import get_app_root, get_resource_dir

    game = _resolve_mod_game(ctx, name)
    game_data = Path(data_dir) if data_dir else _resolve_game_data_dir(game)
    try:
        archive_max_bytes = gib_to_bytes(archive_max_size_gb)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    if no_esp and source:
        raise click.ClickException("--source cannot be combined with --no-esp")
    if loose and (deploy_all or patch_name or no_esp):
        raise click.ClickException("--loose does not support --all/--patch/--no-esp")
    if dry_run:
        from creation_lib.build.deploy_plan import plan_deploy
        from cli._output import output
        output(plan_deploy(get_app_root() / "mods" / name, game_data, game=game, source=source,
            no_esp=no_esp, esp_only=esp_only and not loose, loose=loose, skip_build=skip_build, skip_pack=skip_pack,
            skip_papyrus_compile=skip_papyrus_compile, preserve_xse_inis=preserve_xse_inis,
            patches=["all"] if deploy_all else [patch_name] if patch_name else None, fo4_ba2_target=ba2_target),
            ctx.obj["fmt"], collection="operations")
        return

    if loose:
        from creation_lib.build.loose_deploy import deploy_loose_assets
        if deploy_all or patch_name or no_esp:
            raise click.ClickException("--loose does not support --all/--patch/--no-esp")
        try:
            result = deploy_loose_assets(
                name,
                game=game,
                game_data_dir=game_data,
                skip_build=skip_build,
                skip_papyrus_compile=skip_papyrus_compile,
                preserve_xse_inis=preserve_xse_inis,
                source=source,
                project_root=get_app_root(),
                on_progress=lambda message: click.echo(message, err=True),
            )
        except (FileNotFoundError, RuntimeError) as e:
            raise click.ClickException(str(e))
        output(asdict(result), ctx.obj["fmt"])
        return

    patches = None
    if deploy_all:
        patches = ["all"]
    elif patch_name:
        patches = [patch_name]

    try:
        result = deploy_mod(
            name,
            game=game,
            game_data_dir=game_data,
            skip_build=skip_build,
            skip_pack=skip_pack,
            skip_papyrus_compile=skip_papyrus_compile,
            esp_only=esp_only,
            no_esp=no_esp,
            preserve_xse_inis=preserve_xse_inis,
            source=source,
            xbox=xbox,
            ps=ps,
            pc_max_res=pc_max_res,
            xbox_max_res=xbox_max_res,
            ps_max_res=ps_max_res,
            patches=patches,
            project_root=get_app_root(),
            resource_dir=get_resource_dir(),
            archive_max_bytes=archive_max_bytes,
            expanded_archives=expanded_archives,
            fo4_ba2_target=ba2_target,
            on_progress=lambda message: click.echo(message, err=True),
        )
    except (FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))
    output(asdict(result), ctx.obj["fmt"])


@mod.command("deploy-loose-file")
@click.argument("name")
@click.argument("asset_path", type=click.Path(path_type=str))
@click.option("--data-dir", default=None, help="Override game Data/ directory")
@click.pass_context
def deploy_loose_file_command(ctx, name, asset_path, data_dir):
    """Deploy one mod asset or asset directory as tracked loose files."""
    from pathlib import Path
    from creation_lib.build.loose_deploy import deploy_loose_file
    from app.paths import get_app_root

    game = _resolve_mod_game(ctx, name)
    game_data = Path(data_dir) if data_dir else _resolve_game_data_dir(game)
    try:
        deploy_loose_file(
            name,
            asset_path,
            game=game,
            game_data_dir=game_data,
            project_root=get_app_root(),
            on_progress=click.echo,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc))


@mod.command()
@click.argument("name")
@click.option("--data-dir", default=None, help="Override game Data/ directory")
@click.option("--verify-stock", is_flag=True, help="Require stock Papyrus compiler acceptance before replacing any PEX output.")
@click.pass_context
def compile(ctx, name, data_dir, verify_stock):
    """Compile Papyrus scripts (.psc → .pex) for a mod into data/Scripts/."""
    from pathlib import Path
    from app.paths import get_app_root
    from creation_lib.build.deployer import compile_papyrus
    from cli._output import output

    game = _resolve_mod_game(ctx, name)
    game_data = Path(data_dir) if data_dir else _resolve_game_data_dir(game)
    mod_dir = get_app_root() / "mods" / name

    if not mod_dir.is_dir():
        raise click.ClickException(f"Mod not found: {mod_dir}")

    try:
        compiled = compile_papyrus(mod_dir, game, game_data, verify_stock=verify_stock,
                                   on_progress=lambda message: click.echo(message, err=True))
    except (FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))

    output({"mod": name, "compiled": compiled, "stock_verified": compiled if verify_stock else 0}, ctx.obj["fmt"])


@mod.command()
@click.argument("mod_path")
@click.option("--force", is_flag=True, help="Re-extract/re-decompile even if output exists")
@click.option("--extract-textures", is_flag=True, help="Also extract texture BA2 archives")
@click.option("--skip-authoring-yaml", is_flag=True, help="Skip .esp -> YAML serialization")
@click.option("--all-plugins", is_flag=True, help="Serialize all plugins (including optional patches)")
@click.option("--data-dir", default=None, help="Override game Data/ directory for YAML serialization")
@click.pass_context
def inspect(ctx, mod_path, force, extract_textures, skip_authoring_yaml, all_plugins, data_dir):
    """Inspect a mod: extract BA2s, decompile scripts, serialize .esp to YAML, catalog assets."""
    from pathlib import Path
    from creation_lib.mod.inspector import inspect_mod

    game = ctx.obj["game"]
    path = Path(mod_path).resolve()

    if not path.is_dir():
        raise click.ClickException(f"Not a directory: {mod_path}")

    data_folder = Path(data_dir) if data_dir else None
    if data_folder is None and not skip_authoring_yaml:
        try:
            data_folder = _resolve_game_data_dir(game)
        except click.ClickException:
            pass  # native serialize will run without an explicit data folder

    inspect_mod(
        path,
        game=game,
        force=force,
        extract_textures=extract_textures,
        skip_authoring_yaml=skip_authoring_yaml,
        all_plugins=all_plugins,
        data_folder=data_folder,
        on_progress=click.echo,
    )


@mod.command(name="import-loose")
@click.argument("name")
@click.option("--data-dir", default=None, help="Override game Data/ directory")
@click.pass_context
def import_loose(ctx, name, data_dir):
    """Pull CK changes back from a loose deployment into the mod folder."""
    from pathlib import Path
    from creation_lib.build.loose_deploy import import_loose_assets
    from app.paths import get_app_root

    game = _resolve_mod_game(ctx, name)
    game_data = Path(data_dir) if data_dir else _resolve_game_data_dir(game)

    try:
        summary = import_loose_assets(
            name,
            game_data_dir=game_data,
            project_root=get_app_root(),
            on_progress=click.echo,
        )
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    click.echo(
        f"Done — {summary['changed']} changed, "
        f"{summary['new']} new, {summary['missing']} missing"
    )


@mod.command()
@click.argument("name")
@click.option("--data-dir", default=None, help="Override game Data/ directory")
@click.option("--all", "undeploy_all", is_flag=True, help="Undeploy main + all patch plugins")
@click.option("--patch", "patch_name", default=None, help="Undeploy a specific patch plugin only")
@click.option("--loose", is_flag=True, help="Undeploy a loose deployment (uses .loose_manifest.json)")
@click.option("--no-esp", is_flag=True, help="Undeploy an XSE-plugin-only mod (mirrors `deploy --no-esp`)")
@click.option("--dry-run", is_flag=True, help="List files that would be removed without deleting anything.")
@click.option("--source", default=None, help="Identify a plugin whose filename differs from the mod folder; same selection as deploy --source.")
@click.pass_context
def undeploy(ctx, name, data_dir, undeploy_all, patch_name, loose, no_esp, dry_run, source):
    """Remove a deployed mod from the game Data folder."""
    from pathlib import Path
    from creation_lib.build.deployer import undeploy_mod
    from cli._output import output
    from app.paths import get_app_root

    game = _resolve_mod_game(ctx, name)
    game_data = Path(data_dir) if data_dir else _resolve_game_data_dir(game)

    if source and (loose or no_esp):
        raise click.UsageError("--source is only used when undeploying a plugin without --loose/--no-esp")

    if loose:
        from creation_lib.build.loose_deploy import undeploy_loose_assets
        if undeploy_all or patch_name or no_esp:
            raise click.ClickException("--loose does not support --all/--patch/--no-esp")
        removed = undeploy_loose_assets(
            name,
            game_data_dir=game_data,
            project_root=get_app_root(),
            dry_run=dry_run,
            on_progress=None if dry_run else lambda message: click.echo(message, err=True),
        )
        if not removed and not dry_run:
            raise click.ClickException(f"No loose deployment found for {name}")
        output({"mod": name, "dry_run": dry_run, "files": removed}, ctx.obj["fmt"], collection="files")
        return

    patches = None
    if undeploy_all:
        patches = ["all"]
    elif patch_name:
        patches = [patch_name]

    removed = undeploy_mod(
        name,
        game=game,
        game_data_dir=game_data,
        no_esp=no_esp,
        patches=patches,
        project_root=get_app_root(),
        dry_run=dry_run,
        source=source,
        on_progress=None if dry_run else lambda message: click.echo(message, err=True),
    )
    if not removed and not dry_run:
        raise click.ClickException(f"No deployed files found for {name}")
    output({"mod": name, "dry_run": dry_run, "files": removed}, ctx.obj["fmt"], collection="files")
