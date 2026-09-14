from __future__ import annotations

import importlib
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from cli._build_info import create_build_info, source_fingerprint
from cli._output import output


def command_catalog(root):
    commands = []

    def walk(command, parts):
        context = click.Context(command, info_name=parts[-1] if parts else "modkit")
        commands.append({"command": " ".join(["modkit", *parts]),
                         "description": command.help or command.short_help or "",
                         "parameters": [{key: p.get(key) for key in ("name", "param_type_name", "opts", "secondary_opts", "type", "required", "nargs", "multiple", "default", "help")}
                                        for p in (param.to_info_dict() for param in command.get_params(context))],
                         "group": isinstance(command, click.Group)})
        if isinstance(command, click.Group):
            for name in command.list_commands(context):
                walk(command.get_command(context, name), [*parts, name])

    walk(root, [])
    return commands


def register(cli):
    @cli.command("capabilities")
    @click.option("--command", "command_filter", default=None, help="Restrict to a command path, e.g. esp vmad.")
    @click.pass_context
    def capabilities(ctx, command_filter):
        """Describe commands and parameters supported by this running executable."""
        from creation_lib.core.game_profiles import GAME_PROFILES
        rows = command_catalog(cli)
        if command_filter:
            path = "modkit " + command_filter.removeprefix("modkit ")
            rows = [row for row in rows if row["command"] == path or row["command"].startswith(path + " ")]
            if not rows:
                raise click.ClickException(f"Unknown command path: {command_filter}")
        output({"contract_version": 1, "executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False)),
                "games": sorted(GAME_PROFILES), "commands": rows,
                "report_options": "Global options precede the command; query flags select output rows, not mutation targets.",
                "jsonl": "One kind=row object per result followed by one kind=meta object.",
                "limitations": ["Legacy commands that print progress/text directly may not support report projection or report files.",
                                "data search/ref result limits are applied by the index before report filters; use esp query for exhaustive plugin queries."]}, ctx.obj["fmt"], collection="commands")

    @cli.group("help")
    def help_group():
        """Find a command by the question you need to answer."""

    @help_group.command("search")
    @click.argument("query")
    @click.pass_context
    def help_search(ctx, query):
        """Search available command names, descriptions and parameter help."""
        words = query.casefold().split()
        rows = []
        for row in command_catalog(cli):
            corpus = " ".join([row["command"], row["description"], *[p["help"] or "" for p in row["parameters"]]]).casefold()
            if all(word in corpus for word in words):
                rows.append(row)
        output({"query": query, "matches": rows}, ctx.obj["fmt"], collection="matches")

    @cli.command("doctor")
    @click.option("--workspace", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None,
                  help="Source checkout to compare with the executable's build receipt.")
    @click.option("--source", "sources", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
                  help="Inputs whose modification times should be compared with search indexes.")
    @click.pass_context
    def doctor(ctx, workspace, sources):
        """Report executable build freshness, loaded native packages and search index timestamps."""
        from app.paths import get_app_root
        root = workspace or get_app_root()
        frozen = bool(getattr(sys, "frozen", False))
        receipt_path = Path(getattr(sys, "_MEIPASS", root)) / "modkit_build_info.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if frozen and receipt_path.is_file() else None
        current = source_fingerprint(root) if (root / "cli/main.py").is_file() else None
        if not frozen:
            receipt = create_build_info(root) if current else None
        freshness = "source" if not frozen else "unknown"
        if frozen and receipt and current:
            freshness = "current" if current["sha256"] == receipt["source"]["sha256"] else "stale"
        native = []
        for package, module_name in (("py-creation-lib", "creation_lib._native"),):
            entry = {"package": package, "module": module_name, "source_freshness": "unknown"}
            try:
                module = importlib.import_module(module_name)
                path = Path(module.__file__)
                entry.update(available=True, path=str(path), bytes=path.stat().st_size,
                             modified_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
                entry["features"] = {"exact_record_inspection": callable(getattr(getattr(module, "esp_authoring_core", None), "plugin_handle_inspect_record", None))}
                try:
                    entry["version"] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    entry["version"] = getattr(module, "__version__", None)
            except (ImportError, OSError) as error:
                entry.update(available=False, error=str(error))
            native.append(entry)
        db_dir = Path(ctx.obj["db_dir"])
        indexes = []
        newest_source = max((p.stat().st_mtime for p in sources), default=None)
        if db_dir.is_dir():
            for path in sorted(db_dir.iterdir()):
                if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    stat = path.stat()
                    indexes.append({"path": str(path.resolve()), "bytes": stat.st_size,
                                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                                    "freshness": "unknown" if newest_source is None else "older_than_inputs" if stat.st_mtime < newest_source else "not_older_than_inputs"})
        output({"executable": sys.executable, "frozen": frozen, "workspace": str(root), "build": receipt,
                "cli_source_freshness": freshness, "native": native, "db_dir": str(db_dir), "indexes": indexes,
                "index_freshness_method": "Modification-time comparison only; does not prove index content matches inputs."}, ctx.obj["fmt"])
