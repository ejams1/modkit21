"""modkit — unified CLI for Bethesda modding tools."""

import multiprocessing
import os
import sys

import click

class ModkitGroup(click.Group):
    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True, **extra):
        arguments = list(sys.argv[1:] if args is None else args)
        fmt = "json"
        for index, argument in enumerate(arguments):
            if argument == "--format" and index + 1 < len(arguments):
                fmt = arguments[index + 1]
            elif argument.startswith("--format="):
                fmt = argument.split("=", 1)[1]
        try:
            extra["windows_expand_args"] = False
            result = super().main(arguments, prog_name, complete_var, standalone_mode=False, **extra)
            if standalone_mode and isinstance(result, int):
                raise SystemExit(result)
            return result
        except click.ClickException as error:
            if not standalone_mode:
                raise
            emit_error(error.format_message(), fmt,
                       code="INVALID_ARGUMENT" if isinstance(error, click.UsageError) else "COMMAND_FAILED",
                       exit_code=error.exit_code)
            raise SystemExit(error.exit_code) from error
        except OSError as error:
            if not standalone_mode:
                raise
            emit_error(error, fmt, code="IO_ERROR")
            raise SystemExit(1) from error
        except (ValueError, RuntimeError, ImportError) as error:
            if not standalone_mode:
                raise
            emit_error(error, fmt, code="BACKEND_UNAVAILABLE" if isinstance(error, ImportError) else "COMMAND_FAILED")
            raise SystemExit(1) from error

# Ensure project root is on sys.path for lib imports
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from cli._output import emit_error

# Suppress noisy logging from libraries
import logging

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _configure_stdio():
    """Use UTF-8 for Click output to avoid Windows help-text encoding crashes."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _bootstrap_environment():
    """Load workspace .env at the CLI boundary."""
    from app.paths import load_dotenv_into_environ

    load_dotenv_into_environ()


@click.group(cls=ModkitGroup)
@click.option(
    "--game",
    default="",
    help="Game profile (fo4, fo76, skyrimse, starfield). Defaults to DEFAULT_GAME env or fo4.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "pretty", "compact", "table", "jsonl"]),
    default="json",
    help="Output format.",
)
@click.option(
    "--db-dir",
    default="",
    help="Path to database directory. Defaults to ./data/ relative to exe.",
)
@click.option("--fields", default=None, help="Report projection: comma-separated dotted field paths.")
@click.option("--where", multiple=True, help="Report predicate FIELD=VALUE; !=, >=, <=, >, <, ~ glob also supported. Repeat for AND.")
@click.option("--items", default=None, help="Array inside a result to query, e.g. records or bindings.")
@click.option("--limit", type=click.IntRange(min=0), default=None, help="Maximum report rows after filtering.")
@click.option("--offset", type=click.IntRange(min=0), default=0, help="Report rows to skip after filtering.")
@click.option("--count-only", is_flag=True, help="Return report counts without row payloads.")
@click.option("--group-by", default=None, help="Count matching report rows by a dotted field path.")
@click.option("--output", type=click.Path(dir_okay=False), default=None, help="Write report to this file and print its artifact summary. Put before the command.")
@click.pass_context
def cli(ctx, game: str, fmt: str, db_dir: str, fields, where, items, limit, offset, count_only, group_by, output):
    """modkit — Bethesda modding toolkit CLI.

    Search game data, manipulate NIF meshes, and more.
    """
    # Also configured in main(), but the `modkit` console script entry point is
    # `cli` and so never reaches it. Piping any record carrying CJK strings — most
    # of SeventySix.esm — then dies on a cp1252 stdout, which is how every
    # generator that shells out to modkit.exe silently broke.
    _configure_stdio()

    ctx.ensure_object(dict)

    # Resolve game
    ctx.obj["game_explicit"] = bool(game)
    if not game:
        game = os.environ.get("DEFAULT_GAME", "fo4")
    ctx.obj["game"] = game
    ctx.obj["fmt"] = fmt
    from cli._query import parse_predicate
    for predicate in where:
        parse_predicate(predicate)
    ctx.obj["report_options"] = dict(fields=fields, where=where, items=items, limit=limit,
                                     offset=offset, count_only=count_only, group_by=group_by, output=output)

    # Resolve db_dir
    if not db_dir:
        db_dir = os.environ.get("MODKIT_DB_DIR", "")
    if not db_dir:
        # Use the shared app path resolver so frozen builds can discover the
        # workspace root even when the executable lives under dist/.
        from app.paths import get_db_dir

        db_dir = str(get_db_dir())
    ctx.obj["db_dir"] = db_dir


@cli.command()
@click.option("--workspace", type=click.Path(exists=True, file_okay=False), default=None,
              help="Source checkout to compare with this executable's build receipt.")
def version(workspace):
    """Print version and exit."""
    version_file = os.path.join(_PROJECT_ROOT, "VERSION")
    if not os.path.isfile(version_file) and getattr(sys, "frozen", False):
        version_file = os.path.join(sys._MEIPASS, "VERSION")
    if os.path.isfile(version_file):
        with open(version_file) as f:
            click.echo(f"modkit {f.read().strip()}")
    else:
        click.echo("modkit (dev)")
    if getattr(sys, "frozen", False):
        import json
        from pathlib import Path
        from app.paths import get_app_root
        from cli._build_info import source_fingerprint
        root = Path(workspace) if workspace else get_app_root()
        receipt = Path(sys._MEIPASS) / "modkit_build_info.json"
        if receipt.is_file() and (root / "cli/main.py").is_file():
            built = json.loads(receipt.read_text(encoding="utf-8"))
            if source_fingerprint(root)["sha256"] != built["source"]["sha256"]:
                click.echo("Warning: this modkit.exe differs from the workspace source. Rebuild it; use modkit doctor for details.", err=True)


# Register command groups
from cli.data_commands import data  # noqa: E402
from cli.nif_commands import nif  # noqa: E402
from cli import audit_commands as _audit_commands  # noqa: E402, F401 — registers `data audit-yaml` as a side effect

cli.add_command(data)
cli.add_command(nif)

from cli.cloth_commands import cloth  # noqa: E402

cli.add_command(cloth)

from cli.havok_commands import havok

cli.add_command(havok)

from cli.anim_commands import anim

cli.add_command(anim)

from cli.build_commands import build  # noqa: E402
from cli.archive_commands import archive  # noqa: E402
from cli.texture_commands import texture  # noqa: E402
from cli.mod_commands import mod  # noqa: E402
from cli.esp_commands import esp  # noqa: E402
from cli.ck_commands import ck  # noqa: E402
from cli.git_commands import git  # noqa: E402
from cli.index_commands import index  # noqa: E402
from cli.swf_commands import swf  # noqa: E402
from cli.setup_commands import setup  # noqa: E402
from cli.world_commands import world  # noqa: E402

cli.add_command(build)
cli.add_command(archive)
cli.add_command(texture)
cli.add_command(mod)
cli.add_command(esp)
cli.add_command(ck)
cli.add_command(git)
cli.add_command(index)
cli.add_command(swf)
cli.add_command(setup)
cli.add_command(world)

from cli import inspection_commands as _inspection_commands  # noqa: E402, F401
from cli.discovery_commands import register as _register_discovery  # noqa: E402

_register_discovery(cli)
_inspection_commands.register_asset_commands(cli)


def _normalize_exit_code(code):
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def _force_exit(code):
    """Terminate immediately, skipping interpreter finalization.

    Frozen (PyInstaller) builds can hang at exit when a non-daemon Python thread
    or a native PyO3/rayon thread never joins, leaving modkit.exe resident and
    holding memory long after the command finished and printed its output. The
    work is already done by the time we get here, so flush user-visible output
    and hand off to the OS, which reclaims everything at once.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def main():
    multiprocessing.freeze_support()
    _configure_stdio()
    _bootstrap_environment()

    # Dev runs (uv run python / pytest) keep normal teardown; only the frozen
    # exe needs the hard exit that fixes the lingering-process bug.
    if not getattr(sys, "frozen", False):
        cli()
        return

    # An aborted/timed-out request kills the agent's shell but not modkit.exe;
    # tear ourselves down when that parent dies instead of orphaning native
    # worker threads that keep holding memory.
    from cli._watchdog import install_parent_death_watchdog

    install_parent_death_watchdog(on_exit=_force_exit)

    code = 0
    try:
        cli()
    except SystemExit as exc:
        code = _normalize_exit_code(exc.code)
    except BaseException:
        import traceback

        traceback.print_exc()
        code = 1
    finally:
        _force_exit(code)


if __name__ == "__main__":
    main()
