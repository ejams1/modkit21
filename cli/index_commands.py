"""modkit index — search index building and game scaffolding."""

import os

import click

from app.paths import get_app_root, get_db_dir
from creation_lib.core.game_profiles import get_profile


def _resolve_game_paths(game: str) -> tuple[str | None, str | None]:
    """Read env for the given game, return (extracted_dir, game_dir).

    CLI 'index build' is a game-workflow command — reading env here is the
    intended boundary per the lib-decoupling policy. Returns (None, None)
    if the vars are unset.
    """
    profile = get_profile(game)
    extracted = os.environ.get(profile.env_var_name) or None
    game_dir = os.environ.get(f"{game.upper()}_DIR") or None
    return extracted, game_dir


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def index(ctx, game):
    """Search index operations: build, add-library, add-game."""
    if game is not None:
        ctx.obj["game"] = game


VALID_DOMAINS = (
    "records",
    "scripts",
    "wiki",
    "ck",
    "nifs",
    "behaviors",
    "havok",
    "external",
    "swf",
)


@index.command()
@click.option(
    "--game", default=None, help="Game profile (overrides index/global --game)."
)
@click.option("--embeddings", is_flag=True, help="Also build embedding indexes")
@click.option(
    "--domain",
    default=None,
    type=click.Choice(VALID_DOMAINS, case_sensitive=False),
    help="Rebuild a single domain instead of all",
)
@click.pass_context
def build(ctx, game, embeddings, domain):
    """Build search indexes for a game (all domains, or a specific --domain)."""
    from creation_lib.db.index_builder import build_game_index, build_domain_index

    if game is not None:
        ctx.obj["game"] = game

    game = ctx.obj["game"]
    try:
        if domain:
            build_domain_index(
                game,
                domain,
                extracted_dir=_resolve_game_paths(game)[0],
                game_dir=_resolve_game_paths(game)[1],
                project_root=get_app_root(),
                db_dir=get_db_dir(),
                embeddings=embeddings,
                on_progress=click.echo,
            )
        else:
            extracted_dir, game_dir = _resolve_game_paths(game)
            results = build_game_index(
                game,
                extracted_dir=extracted_dir,
                game_dir=game_dir,
                project_root=get_app_root(),
                db_dir=get_db_dir(),
                embeddings=embeddings,
                on_progress=click.echo,
            )
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))


@index.command("regen-yaml")
@click.option("--game", default=None, help="Game profile (overrides index/global --game).")
@click.option("--data-dir", default=None, help="Override game Data/ directory.")
@click.option(
    "--plugin",
    "plugins",
    multiple=True,
    help="Limit to specific plugin file names (repeat for multiple). Defaults to all *.esm.",
)
@click.option("--fresh", is_flag=True, help="Clear each plugin's cache subdirectory before re-export.")
@click.option("--workers", type=click.IntRange(min=1), default=1, show_default=True, help="Plugins to export in parallel; each worker loads a plugin.")
@click.pass_context
def regen_yaml(ctx, game, data_dir, plugins, fresh, workers):
    """Re-export the game's master plugins into ``data/<game>_esm_yaml/``.

    Drives the records search index. Run before ``modkit index build --domain records``
    when the game has been patched or the cache is empty.
    """
    from pathlib import Path
    from creation_lib.db.index_builder import regenerate_esm_yaml_cache
    from cli._output import output

    if game is not None:
        ctx.obj["game"] = game
    game = ctx.obj["game"]

    if data_dir:
        resolved = Path(data_dir)
    else:
        _, game_dir_str = _resolve_game_paths(game)
        if not game_dir_str:
            raise click.ClickException(
                f"{game.upper()}_DIR is not set in env or .env. "
                f"Pass --data-dir or set the game directory."
            )
        resolved = Path(game_dir_str) / "Data"

    try:
        results = regenerate_esm_yaml_cache(
            game,
            game_data_dir=resolved,
            project_root=get_app_root(),
            db_dir=ctx.obj["db_dir"],
            plugins=list(plugins) if plugins else None,
            fresh=fresh,
            workers=workers,
            on_progress=lambda message: click.echo(message, err=True),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e))
    output({"game": game, "workers": workers, "plugins": results}, ctx.obj["fmt"])
    if any(status.startswith("error:") for status in results.values()):
        ctx.exit(1)


@index.command("add-library")
@click.argument("mod_name")
@click.pass_context
def add_library(ctx, mod_name):
    """Migrate an inspected mod to the external reference library."""
    from creation_lib.db.index_builder import add_to_library

    try:
        add_to_library(
            mod_name,
            project_root=get_app_root(),
            db_dir=get_db_dir(),
            on_progress=click.echo,
        )
    except (FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e))


@index.command("add-game")
@click.argument("game_id")
@click.argument("display_name")
@click.pass_context
def add_game(ctx, game_id, display_name):
    """Scaffold directories and skill stubs for a new game."""
    from creation_lib.db.index_builder import add_game_scaffold

    try:
        checklist = add_game_scaffold(
            game_id,
            display_name,
            project_root=get_app_root(),
            on_progress=click.echo,
        )
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e))


@index.command("download-geck-wiki")
@click.option(
    "--output-dir", default=None, help="Output directory (default: Wiki/fo3_nv_wiki/)"
)
def download_geck_wiki(output_dir):
    """Download the GECK Wiki (geckwiki.com) for Fallout 3 / New Vegas reference."""
    import asyncio
    from pathlib import Path
    from app.paths import get_app_root
    from creation_lib.preprocessor.wiki_downloader import main as _main

    out = Path(output_dir) if output_dir else get_app_root() / "Wiki" / "fo3_nv_wiki"
    asyncio.run(_main(out))
