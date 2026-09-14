"""modkit data audit-yaml — static field-whitelist audit for mod YAMLs.

Catches source-game field leakage. Walks a mod's yaml/ tree, infers record type
from the record directory name, and checks every authoring field against
bacup/py_bacup_lib/python/bacup_lib/record/whitelists/<game>.yaml.

Registered as a subcommand of `modkit data` by importing this module
after `cli.data_commands` (see cli/main.py).
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import click
import yaml

from cli._output import output
from cli.data_commands import data


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WHITELIST_DIR = _PROJECT_ROOT / "bacup/py_bacup_lib/python/bacup_lib/record/whitelists"
_MODS_DIR = _PROJECT_ROOT / "mods"


def _load_whitelist(game: str) -> dict[str, set[str]]:
    """Load bacup_lib's field whitelist and return {record_type: {field, ...}}.

    Applies the overrides block (add/drop) on top of record_types. Comparison
    is case-sensitive.
    """
    if not _WHITELIST_DIR.is_dir():
        raise click.ClickException(
            "modkit data audit-yaml requires the bacup tree "
            "(bacup/py_bacup_lib/python/bacup_lib/record/whitelists) — "
            "not present in this checkout."
        )

    path = _WHITELIST_DIR / f"{game}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Whitelist not found: {path}. Run `modkit index build --domain fo4_whitelist` "
            f"or `uv run python tools/build_field_whitelist.py --game {game}`."
        )
    with open(path, encoding="utf-8") as f:
        data_yaml = yaml.safe_load(f) or {}

    record_types = data_yaml.get("record_types") or {}
    result: dict[str, set[str]] = {
        rt: set(fields or []) for rt, fields in record_types.items()
    }

    overrides = data_yaml.get("overrides") or {}
    add = overrides.get("add") or {}
    drop = overrides.get("drop") or {}
    for rt, fields in add.items():
        result.setdefault(rt, set()).update(fields or [])
    for rt, fields in drop.items():
        if rt in result:
            result[rt] -= set(fields or [])

    return result


def _resolve_mod_game(mod_dir: Path, explicit_game: str, game_explicit: bool) -> str:
    """Determine target game: explicit --game flag > mod's .game file > DEFAULT_GAME."""
    if game_explicit and explicit_game:
        return explicit_game
    game_file = mod_dir / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip() or explicit_game
    return explicit_game or os.environ.get("DEFAULT_GAME", "fo4")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _iter_record_yamls(yaml_dir: Path):
    """Yield (record_type, yaml_path) for every record yaml under the mod's yaml dir.

    Current authoring records live at yaml/records/<RecordType>/<filename>.yaml.
    Older fixtures used yaml/<RecordType>/<filename>.yaml, so keep that as a
    fallback for compatibility.
    """
    records_dir = yaml_dir / "records"
    root = records_dir if records_dir.is_dir() else yaml_dir

    for record_type_dir in sorted(root.iterdir()):
        if not record_type_dir.is_dir():
            continue
        record_type = record_type_dir.name
        for path in sorted(record_type_dir.rglob("*.yaml")):
            yield record_type, path


def _record_field_names(record: Any) -> list[str]:
    """Return target-game field names from a parsed record YAML."""
    if not isinstance(record, dict):
        return []

    fields = record.get("fields")
    if isinstance(fields, list):
        names: list[str] = []
        for entry in fields:
            if isinstance(entry, dict):
                names.extend(str(key) for key in entry.keys())
        return names

    if isinstance(record, dict):
        return list(record.keys())
    return []


def _scalar_warnings(text: str) -> list[dict[str, Any]]:
    warnings = []
    visited = set()

    def visit(node):
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.ScalarNode) and node.style is None:
            value = node.value
            reason = None
            if node.tag.endswith(":bool") and value.casefold() not in {"true", "false"}:
                reason = "YAML 1.1 interprets this as a boolean; quote it if you intend a string."
            elif node.tag.endswith(":int") and (value.lower().startswith(("0x", "-0x", "+0x"))
                                               or len(value) > 1 and value.startswith("0")):
                reason = "YAML interprets this as a number; quote identifiers to preserve their spelling and radix."
            if reason:
                warnings.append({"line": node.start_mark.line + 1, "column": node.start_mark.column + 1,
                                 "value": value, "parsed_as": node.tag.rsplit(":", 1)[-1], "message": reason})
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                visit(key)
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                visit(child)

    node = yaml.compose(text)
    if node is not None:
        visit(node)
    return warnings


@data.command("audit-yaml")
@click.argument("mod_name")
@click.option(
    "--mods-dir",
    default="",
    help="Override mods directory (defaults to ./mods relative to project root).",
)
@click.pass_context
def audit_yaml(ctx, mod_name: str, mods_dir: str):
    """Audit a mod's YAML against the target-game field whitelist.

    Walks mods/<mod_name>/yaml/, parses every record yaml, and reports any
    authoring fields that are not in bacup_lib's record whitelist for the game
    for the record type. Exits 1 if any unknowns are found.

    Examples:

      modkit data audit-yaml B21_Snally

      modkit --game fo4 data audit-yaml B21_Snally
    """
    game = ctx.obj.get("game", "fo4")
    fmt = ctx.obj.get("fmt", "json")
    game_explicit = ctx.obj.get("game_explicit", False)

    mods_root = Path(mods_dir) if mods_dir else _MODS_DIR
    mod_dir = mods_root / mod_name
    yaml_dir = mod_dir / "yaml"

    if not yaml_dir.is_dir():
        output({"error": f"Mod yaml directory not found: {yaml_dir}"}, fmt)
        return  # output() sys.exits on error

    target_game = _resolve_mod_game(mod_dir, game, game_explicit)

    try:
        whitelist = _load_whitelist(target_game)
    except FileNotFoundError as e:
        output({"error": str(e)}, fmt)
        return

    # Walk and check.
    per_record_type: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"files_with_unknowns": [], "unknown_field_counts": Counter(), "files_checked": 0}
    )
    unknown_counter_global: Counter = Counter()
    files_checked = 0
    files_with_unknowns = 0
    unknown_total = 0
    unknown_record_types: set[str] = set()
    scalar_warnings = []
    parse_errors = []

    for record_type, path in _iter_record_yamls(yaml_dir):
        files_checked += 1
        per_record_type[record_type]["files_checked"] += 1

        try:
            text = path.read_text(encoding="utf-8")
            record = yaml.safe_load(text)
            scalar_warnings.extend({"file": _display_path(path), **warning} for warning in _scalar_warnings(text))
        except yaml.YAMLError as e:
            parse_errors.append({"file": _display_path(path), "error": str(e)})
            per_record_type[record_type]["files_with_unknowns"].append(
                {"file": _display_path(path), "parse_error": str(e)}
            )
            continue

        field_names = _record_field_names(record)
        allowed = whitelist.get(record_type)
        if allowed is None:
            # Unknown record type — whole file is "unknown"
            unknown_record_types.add(record_type)
            per_record_type[record_type]["files_with_unknowns"].append(
                {
                    "file": _display_path(path),
                    "unknown_fields": sorted(field_names),
                    "reason": "record type not in whitelist",
                }
            )
            for fn in field_names:
                per_record_type[record_type]["unknown_field_counts"][fn] += 1
                unknown_counter_global[f"{record_type}.{fn}"] += 1
                unknown_total += 1
            files_with_unknowns += 1
            continue

        unknowns = sorted(fn for fn in field_names if fn not in allowed)
        if unknowns:
            files_with_unknowns += 1
            unknown_total += len(unknowns)
            per_record_type[record_type]["files_with_unknowns"].append(
                {
                    "file": _display_path(path),
                    "unknown_fields": unknowns,
                }
            )
            for fn in unknowns:
                per_record_type[record_type]["unknown_field_counts"][fn] += 1
                unknown_counter_global[f"{record_type}.{fn}"] += 1

    # Build the report. Only keep record types that actually had unknowns for the
    # detailed breakdown — but the totals cover every file walked.
    by_record_type_report: dict[str, Any] = {}
    for rt, info in sorted(per_record_type.items()):
        if not info["files_with_unknowns"]:
            continue
        by_record_type_report[rt] = {
            "files_checked": info["files_checked"],
            "files_with_unknowns": len(info["files_with_unknowns"]),
            "top_unknown_fields": info["unknown_field_counts"].most_common(10),
            "files": info["files_with_unknowns"],
        }

    report = {
        "mod": mod_name,
        "mod_dir": _display_path(mod_dir),
        "target_game": target_game,
        "whitelist": _display_path(_WHITELIST_DIR / f"{target_game}.yaml"),
        "files_checked": files_checked,
        "files_with_unknowns": files_with_unknowns,
        "unknown_fields_total": unknown_total,
        "unknown_record_types": sorted(unknown_record_types),
        "top_unknown_fields_global": unknown_counter_global.most_common(20),
        "by_record_type": by_record_type_report,
        "scalar_warnings": scalar_warnings,
        "parse_errors": parse_errors,
        "status": "parse_errors" if parse_errors else "unknowns_found" if unknown_total or unknown_record_types
                  else "scalar_warnings" if scalar_warnings else "clean",
    }

    output(report, fmt)
    if unknown_total > 0 or unknown_record_types or parse_errors:
        sys.exit(1)
