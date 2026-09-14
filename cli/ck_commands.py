"""modkit ck — Creation Kit automation commands."""

import os
import shutil

import click


def _resolve_mod_game(ctx, mod_name: str) -> str:
    """Return game for mod_name: explicit --game flag > .game file > DEFAULT_GAME fallback."""
    if ctx.obj.get("game_explicit"):
        return ctx.obj["game"]
    from app.paths import get_app_root
    from pathlib import Path
    game_file = get_app_root() / "mods" / mod_name / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip()
    return ctx.obj["game"]


def _resolve_game_dir(game: str) -> "Path":
    """Resolve game install directory from env vars."""
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
    return Path(game_dir)


def _clear_local_animtextdata(mod_dir: "Path") -> bool:
    animtext_dir = mod_dir / "data" / "meshes" / "AnimTextData"
    if not animtext_dir.exists():
        return False
    shutil.rmtree(animtext_dir)
    return True


def _check_plugin_errors_before_animdata(plugin_file: "Path", game: str) -> None:
    from cli.esp_commands import _esp_master_search_paths
    from creation_lib.esp.editor import EditorSession, validate

    session = EditorSession(
        default_game=game,
        auto_scan_conflicts=False,
        master_search_paths=_esp_master_search_paths(game, plugin_file),
    )
    try:
        loaded = session.load(plugin_file, game=game)
        report = validate(session, handle=loaded.handle)
        for issue in report:
            if issue.severity.value != "error":
                click.echo(f"{issue.severity.value.upper()}: {issue.message}")
        report = [issue for issue in report if issue.severity.value == "error"]
        if report:
            lines = []
            for issue in list(report)[:10]:
                form_id = (
                    f"{int(issue.form_id):08X}"
                    if issue.form_id is not None
                    else "--------"
                )
                lines.append(
                    f"{issue.severity.value.upper()} {issue.plugin_name} "
                    f"{form_id}: {issue.message}"
                )
            extra = "" if len(report) <= 10 else f"\n... {len(report) - 10} more issue(s)"
            raise click.ClickException(
                "ESP validation failed before AnimTextData generation:\n"
                + "\n".join(lines)
                + extra
            )
    finally:
        session.close_all()


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def ck(ctx, game):
    """Creation Kit automation: previs, dialogue export, anim data."""
    if game is not None:
        ctx.obj["game"] = game


@ck.command()
@click.argument("name")
@click.option(
    "--clean/--no-clean",
    "clean_output",
    default=True,
    help="Remove existing generated PreCombined/Vis/CDX/CSG outputs from the mod before collecting new ones.",
)
@click.pass_context
def previs(ctx, name, clean_output):
    """Generate PreCombines and PreVis data via Creation Kit."""
    from pathlib import Path
    from app.paths import get_app_root
    from creation_lib.ck.automation import run_previs

    game = _resolve_mod_game(ctx, name)
    game_dir = _resolve_game_dir(game)
    mod_dir = get_app_root() / "mods" / name

    if not mod_dir.is_dir():
        raise click.ClickException(f"Mod directory not found: {mod_dir}")

    try:
        run_previs(
            name,
            game=game,
            game_dir=game_dir,
            game_data_dir=game_dir / "Data",
            mod_dir=mod_dir,
            clean_output=clean_output,
            on_progress=click.echo,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))


@ck.command()
@click.argument("name")
@click.pass_context
def dialogue(ctx, name):
    """Export dialogue lines via Creation Kit."""
    from pathlib import Path
    from app.paths import get_app_root
    from creation_lib.ck.automation import export_dialogue

    game = _resolve_mod_game(ctx, name)
    game_dir = _resolve_game_dir(game)
    mod_dir = get_app_root() / "mods" / name

    if not mod_dir.is_dir():
        raise click.ClickException(f"Mod directory not found: {mod_dir}")

    try:
        result = export_dialogue(
            name,
            game=game,
            game_dir=game_dir,
            game_data_dir=game_dir / "Data",
            mod_dir=mod_dir,
            on_progress=click.echo,
        )
        if result:
            click.echo(f"Output: {result}")
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))


@ck.command()
@click.argument("name")
@click.pass_context
def animdata(ctx, name):
    """Generate AnimTextData via Creation Kit.

    Deploys the mod through the standard pack-and-deploy pipeline so CK can
    resolve the same plugin and archives used in-game, then runs
    CK -GenerateAnimInfo.
    """
    from app.paths import get_app_root, get_resource_dir
    from creation_lib.build.deployer import deploy_mod
    from creation_lib.ck.automation import generate_anim_data

    game = _resolve_mod_game(ctx, name)
    game_dir = _resolve_game_dir(game)
    game_data_dir = game_dir / "Data"
    mod_dir = get_app_root() / "mods" / name

    if not mod_dir.is_dir():
        raise click.ClickException(f"Mod directory not found: {mod_dir}")

    try:
        if _clear_local_animtextdata(mod_dir):
            click.echo(
                f"Cleared stale AnimTextData before deploy: "
                f"{mod_dir / 'data' / 'meshes' / 'AnimTextData'}"
            )
        deploy_mod(
            name,
            game=game,
            game_data_dir=game_data_dir,
            skip_build=False,
            skip_pack=False,
            esp_only=False,
            no_esp=False,
            xbox=False,
            project_root=get_app_root(),
            resource_dir=get_resource_dir(),
            on_progress=click.echo,
        )
        _check_plugin_errors_before_animdata(mod_dir / f"{name}.esp", game)
        click.echo("ESP error check passed before AnimTextData generation.")
        result = generate_anim_data(
            name,
            game=game,
            game_dir=game_dir,
            game_data_dir=game_data_dir,
            mod_dir=mod_dir,
            deploy_loose_data=False,
            on_progress=click.echo,
        )
        if result:
            click.echo(f"Output: {result}")
        else:
            click.echo("No AnimTextData was generated — mod may not have animation bindings")
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))
