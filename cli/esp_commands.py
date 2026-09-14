"""modkit esp — binary-first ESP import/export workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from cli._output import format_json_text, output


def _resolve_mod_game(ctx, mod_name: str) -> str:
    """Return game for mod_name: explicit --game flag > .game file > DEFAULT_GAME fallback."""
    if ctx.obj.get("game_explicit"):
        return ctx.obj["game"]
    from app.paths import get_app_root, get_db_dir
    game_file = get_app_root() / "mods" / mod_name / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip()
    return ctx.obj["game"]


def _resolve_game_data_dir(game: str) -> Path | None:
    """Resolve game Data/ directory from env vars."""
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
        return None
    return Path(game_dir) / "Data"


def _load_plugin(
    path: Path,
    *,
    game: str | None,
    strings_dir: str | None,
    language: str | None,
    backend: str,
    lazy_index: bool = False,
):
    from creation_lib.esp import Plugin

    return Plugin.load(
        path,
        game=game,
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=lazy_index,
    )


def _require_native(plugin, command: str):
    handle = getattr(plugin, "_rust_handle", None)
    if handle is None:
        raise click.ClickException(f"{command} requires the native ESP backend.")
    return handle


def _detect_text_format(source: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    suffix = source.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return "json"


def _esp_master_search_paths(game: str | None, plugin_file: Path) -> list[Path]:
    paths = [plugin_file.parent]
    if game is not None:
        data_dir = _resolve_game_data_dir(game)
        if data_dir is not None and data_dir not in paths:
            paths.append(data_dir)
    return paths


def _resolve_master_size(name: str, search_paths: list[Path]) -> int:
    for directory in search_paths:
        candidate = directory / name
        if candidate.is_file():
            return candidate.stat().st_size
    return 0


def _resolve_record_id(plugin, record_id: str) -> int | None:
    """Resolve a local hex FormID or an EditorID (case-insensitive) to a form_id.

    Hex is tried first so a FormID lookup never builds the EditorID index, which
    costs hundreds of MB on a master-sized plugin. An existing FormID wins over an
    EditorID spelled as the same hex digits.

    Returns None when the value is neither parseable hex nor a known EditorID, or
    when an EditorID matches more than one record (ambiguous).
    """
    try:
        form_id = int(record_id, 16)
    except ValueError:
        pass
    else:
        # record_context resolves through the lazy store's early-exit walk.
        # get_record_by_form_id would go via ensure_core_section, indexing the
        # whole plugin for a lookup that needs one record.
        handle = getattr(plugin, "_rust_handle", None)
        if handle is not None:
            from creation_lib.esp import native_runtime as _nr

            if _nr.plugin_handle_call(handle, "record_context_for_form_id", form_id):
                return form_id
        elif plugin.get_record_by_form_id(form_id) is not None:
            return form_id

    idx = plugin.eid_index()
    hit = idx.get(record_id)
    if hit is None:
        lowered = record_id.lower()
        hit = next((v for k, v in idx.items() if k.lower() == lowered), None)
    if hit is not None:
        if len(hit) != 1:
            return None
        return int(hit[0].split(":")[-1], 16)
    # A well-formed but absent FormID still returns a number, so the caller
    # reports "record not found" rather than "not a FormID or EditorID".
    try:
        return int(record_id, 16)
    except ValueError:
        return None


_PLACED_RECORD_SIGNATURES = (
    "REFR",
    "ACHR",
    "PGRE",
    "PHZD",
    "PMIS",
    "PARW",
    "PBAR",
    "PBEA",
    "PCON",
    "PFLA",
)


def _form_key_object_id(form_key: str) -> int | None:
    try:
        return int(str(form_key).rsplit(":", 1)[1], 16) & 0x00FFFFFF
    except ValueError:
        return None
    except IndexError:
        return None


def _xloc_level(subrecords: list[tuple[str, bytes, str | None]] | None) -> int | None:
    for signature, data, _semantic_type in subrecords or ():
        if signature == "XLOC":
            return data[0] if data else None
    return None


def _record_model_paths(
    subrecords: list[tuple[str, bytes, str | None]] | None,
) -> list[str]:
    paths: list[str] = []
    for signature, data, _semantic_type in subrecords or ():
        if signature != "MODL":
            continue
        path = bytes(data).split(b"\0", 1)[0].decode("cp1252", errors="replace")
        if path and path not in paths:
            paths.append(path)
    return paths


# Well-known FO4 master RACE records so `--race HumanRace` resolves without the
# game-data index (master races aren't present in the converted plugin, so their
# EditorIDs can't be read back from it).
_FO4_KNOWN_RACES_LOWER = {
    "fallout4.esm:013746": "HumanRace",
    "fallout4.esm:0eafb6": "GhoulRace",
}
_FO4_RACE_ALIASES = {name.upper(): fk for fk, name in _FO4_KNOWN_RACES_LOWER.items()}


def _looks_like_form_key(spec: str) -> bool:
    return ":" in spec and _form_key_object_id(spec) is not None


def _resolve_race_specs(plugin, handle, race_specs) -> dict[str, str]:
    """Resolve each --race spec to a lowercased RACE form key.

    Accepts a form key (``Fallout4.esm:013746``), a known master-race alias
    (``HumanRace``/``GhoulRace``), or an EditorID defined in this plugin
    (creature races like ``MoleMinerRace``). Returns {lower_form_key: display}.
    """
    from creation_lib.esp import native_runtime

    resolved: dict[str, str] = {}
    own_race_index: dict[str, str] | None = None
    for spec in race_specs:
        s = str(spec).strip()
        if not s:
            continue
        form_key: str | None = None
        if _looks_like_form_key(s):
            form_key = s
        elif s.upper() in _FO4_RACE_ALIASES:
            form_key = _FO4_RACE_ALIASES[s.upper()]
        else:
            if own_race_index is None:
                own_race_index = {}
                for rid in native_runtime.plugin_handle_record_form_ids(handle, ["RACE"]):
                    summary = native_runtime.plugin_handle_record_summary(handle, rid)
                    if summary is not None and summary.editor_id:
                        fk = f"{plugin.plugin_name}:{rid & 0x00FFFFFF:06X}"
                        own_race_index[summary.editor_id.upper()] = fk
            form_key = own_race_index.get(s.upper())
        if form_key is None:
            raise click.ClickException(
                f"Could not resolve --race '{spec}'. Pass a form key "
                "(Fallout4.esm:013746), an alias (HumanRace/GhoulRace), or a "
                "RACE EditorID defined in this plugin."
            )
        low = form_key.lower()
        resolved[low] = _FO4_KNOWN_RACES_LOWER.get(low, s)
    return resolved


def _npc_base_race_form_key(handle, base_form_key: str, cache: dict[str, str | None]) -> str | None:
    if base_form_key in cache:
        return cache[base_form_key]
    from creation_lib.esp import native_runtime

    refs = native_runtime.plugin_handle_get_referenced_form_keys_by_subrecord(
        handle, base_form_key, "RNAM"
    )
    race_fk = refs[0] if refs else None
    cache[base_form_key] = race_fk
    return race_fk


def _race_display_name(handle, plugin, own_plugin: str, race_form_key: str) -> str | None:
    low = race_form_key.lower()
    if low in _FO4_KNOWN_RACES_LOWER:
        return _FO4_KNOWN_RACES_LOWER[low]
    if race_form_key.split(":", 1)[0].lower() == own_plugin:
        from creation_lib.esp import native_runtime

        oid = _form_key_object_id(race_form_key)
        if oid is not None:
            summary = native_runtime.plugin_handle_record_summary(handle, oid)
            if summary is not None and summary.editor_id:
                return summary.editor_id
    return None


def _resolve_search_mode(mode_substring: bool, mode_regex: bool) -> str:
    """Map the --substring / --regex flags to a search mode (default glob)."""
    if mode_substring and mode_regex:
        raise click.ClickException("--substring and --regex are mutually exclusive.")
    if mode_regex:
        return "regex"
    if mode_substring:
        return "substring"
    return "glob"


def _search_flags(f):
    """Shared matcher options for commands that select records by pattern."""
    f = click.option("--substring", "mode_substring", is_flag=True, default=False, help="Match PATTERN as a case-insensitive substring instead of glob.")(f)
    f = click.option("--regex", "mode_regex", is_flag=True, default=False, help="Match PATTERN as a regular expression instead of glob.")(f)
    f = click.option("--full", "match_full", is_flag=True, default=False, help="Also match against each record's full/display name.")(f)
    f = click.option("--type", "record_type", default=None, help="Restrict to a record type, e.g. WEAP or a display alias like Weapons.")(f)
    f = click.option("--case-sensitive", is_flag=True, default=False, help="Case-sensitive matching (default is case-insensitive).")(f)
    return f


def _run_search(plugin, pattern, *, mode, match_full, read_full, record_type, case_sensitive, limit):
    from creation_lib.esp.record_types import record_type_signature

    sigs = [record_type_signature(record_type)] if record_type else None
    return plugin.search_records(
        pattern,
        mode=mode,
        match_full=match_full,
        read_full=read_full,
        signatures=sigs,
        case_sensitive=case_sensitive,
        limit=limit,
    )


def _format_match(match: dict, *, detail: bool) -> dict:
    out = {
        "form_id": f"{match['form_id'] & 0x00FFFFFF:06X}",
        "signature": match["signature"],
        "editor_id": match["editor_id"],
    }
    if detail:
        out["full_name"] = match.get("full_name")
    return out


def _parse_field_value(value_text: str):
    """Parse --value as JSON, falling back to the raw string when it isn't valid JSON."""
    import json

    try:
        return json.loads(value_text)
    except json.JSONDecodeError:
        return value_text


def _set_field_in_fields(fields: list, field_label: str, value, *, add_missing: bool):
    """Replace every `{field_label: ...}` entry's value in a record's fields list.

    Returns (changed, found, old_values). Appends the field when missing and add_missing.
    """
    found = False
    changed = False
    old_values = []
    for item in fields:
        if isinstance(item, dict) and len(item) == 1 and field_label in item:
            found = True
            old_values.append(item[field_label])
            if item[field_label] != value:
                item[field_label] = value
                changed = True
    if not found and add_missing:
        fields.append({field_label: value})
        changed = True
    return changed, found, old_values


def _eid_transformer(prefix_spec, regex_sub, rename_to, match_count: int):
    """Build an EditorID -> new EditorID function from exactly one rename option."""
    import re

    chosen = [opt for opt in (prefix_spec, regex_sub, rename_to) if opt is not None]
    if len(chosen) != 1:
        raise click.ClickException("rename needs exactly one of --prefix, --regex-sub, or --to.")
    if rename_to is not None:
        if match_count > 1:
            raise click.ClickException(f"--to renames a single record, but {match_count} matched. Use --prefix or --regex-sub.")
        return lambda eid: rename_to
    if prefix_spec is not None:
        if "=" not in prefix_spec:
            raise click.ClickException("--prefix must be OLD=NEW.")
        old, new = prefix_spec.split("=", 1)
        if not old:
            raise click.ClickException("--prefix OLD part must be non-empty.")
        return lambda eid: (new + eid[len(old):]) if eid.startswith(old) else eid
    if "=" not in regex_sub:
        raise click.ClickException("--regex-sub must be PATTERN=REPLACEMENT.")
    pattern, replacement = regex_sub.split("=", 1)
    compiled = re.compile(pattern)
    return lambda eid: compiled.sub(replacement, eid)


def _upsert_authoring(plugin, record: dict, record_type: str | None, *, dry_run: bool) -> dict:
    """Resolve a record's signature, then upsert it (unless dry_run). Returns a summary."""
    from creation_lib.esp.record_types import record_type_signature

    existing_fid = None
    fid_text = str(record.get("form_id") or "").strip()
    if fid_text:
        try:
            existing_fid = int(fid_text.split(":")[0], 16) & 0x00FFFFFF
        except ValueError:
            existing_fid = None
    if not existing_fid and record.get("eid"):
        existing_fid = _resolve_record_id(plugin, str(record["eid"]))
    existing_summary = plugin.get_record_by_form_id(existing_fid) if existing_fid else None
    if record_type:
        record["signature"] = record_type_signature(record_type)
    if not record.get("signature"):
        if existing_summary is not None and existing_summary.signature:
            record["signature"] = existing_summary.signature
        else:
            raise click.ClickException("record type required for a new record (pass --type, e.g. --type WEAP).")
    result = {"signature": record["signature"], "created": existing_summary is None}
    if dry_run:
        result["upserted"] = record.get("eid") or fid_text or None
    else:
        result["upserted"] = plugin.upsert_authoring_record(record)
    return result


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def esp(ctx, game):
    """Binary-first ESP/ESM/ESL inspection, export, import, and validation."""
    if game is not None:
        ctx.obj["game"] = game


@esp.command()
@click.argument("plugin_path")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def inspect(ctx, plugin_path, strings_dir, language, backend):
    """Inspect a plugin and print a compact structural summary."""
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        from creation_lib.esp import native_runtime as _native_runtime

        handle = getattr(plugin, "_rust_handle", None)
        groups = (
            _native_runtime.plugin_handle_group_signatures(handle)
            if handle is not None
            else []
        )
        signatures = _native_runtime.plugin_handle_record_counts(handle) if handle is not None else {}
        result = {
            "plugin": plugin.plugin_name,
            "game": plugin.game,
            "header_size": plugin.header_size,
            "record_count": plugin.record_count,
            "group_count": len(groups),
            "masters": plugin.header.masters,
            "localized": plugin.header.is_localized,
            "localized_string_count": len(plugin.localized_strings),
            "signatures": [
                {"signature": signature, "count": count}
                for signature, count in sorted(signatures.items(), key=lambda item: (-item[1], item[0]))
            ],
        }
        output(result, ctx.obj.get("fmt", "json"))


@esp.command(name="audit-topology")
@click.argument(
    "source_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False, readable=True),
)
@click.argument(
    "output_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False, readable=True),
)
@click.option(
    "--source-game",
    type=click.Choice(["fo3", "fnv", "fo4", "fo76", "skyrimse", "starfield"]),
    default=None,
    help="Source plugin game profile. Defaults to the active --game.",
)
@click.option(
    "--target-game",
    type=click.Choice(["fo3", "fnv", "fo4", "fo76", "skyrimse", "starfield"]),
    default=None,
    help="Converted plugin game profile. Defaults to the active --game.",
)
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["json", "pretty", "compact", "markdown"]),
    default=None,
    help="Report format. Overrides the global --format for this command.",
)
@click.pass_context
def audit_topology(ctx, source_path, output_path, source_game, target_game, report_format):
    """Compare nested exterior topology between source and converted plugins.

    The native scanner walks GRUP and record headers directly and only decodes
    WRLD EditorIDs and CELL coordinates, so large masters do not require a full
    authoring export or a materialized record tree.
    """
    from creation_lib.esp.topology_audit import audit_topology_pair, render_topology_report

    active_game = ctx.obj.get("game")
    try:
        report = audit_topology_pair(
            source_path,
            output_path,
            source_game=source_game or active_game,
            target_game=target_game or active_game,
        )
        rendered = render_topology_report(
            report,
            report_format or ctx.obj.get("fmt", "json"),
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(rendered, nl=not rendered.endswith("\n"))


@esp.command(name="list-records")
@click.argument("plugin_path")
@click.option("--type", "record_types", multiple=True, help="Record type to list, e.g. WRLD or a display alias like Worldspaces. Repeatable; omit to list every record.")
@click.option("--match", "match_pattern", default=None, help="Only list records whose EditorID matches this glob (or --substring/--regex) pattern.")
@click.option("--substring", "mode_substring", is_flag=True, default=False, help="Treat --match as a case-insensitive substring instead of glob.")
@click.option("--regex", "mode_regex", is_flag=True, default=False, help="Treat --match as a regular expression instead of glob.")
@click.option("--full", "match_full", is_flag=True, default=False, help="Also match --match against each record's full/display name.")
@click.option("--case-sensitive", is_flag=True, default=False, help="Case-sensitive --match (default is case-insensitive).")
@click.option("--has-subrecord", "subrecord_signatures", multiple=True, help="Only list records containing this four-character subrecord. Repeatable.")
@click.option("--include-subrecord-data", is_flag=True, default=False, help="Include matching --has-subrecord payloads as hex.")
@click.option("--max-results", type=int, default=None, help="Limit the number of records returned.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def list_records(ctx, plugin_path, record_types, match_pattern, mode_substring, mode_regex, match_full, case_sensitive, subrecord_signatures, include_subrecord_data, max_results, strings_dir, language, backend, full_load):
    """List a plugin's records as EditorID + local FormID pairs.

    FormIDs are plugin-local object IDs (e.g. 000800, not 01000800). Pass --match
    to filter by an EditorID pattern (see `esp search`). For very large plugins
    (converted ESMs with millions of records) prefer the indexed `modkit data`
    commands instead.
    """
    from creation_lib.esp.record_types import record_type_signature

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    signatures = [record_type_signature(value) for value in record_types]
    subrecord_signatures = tuple(value.upper() for value in subrecord_signatures)
    if any(len(value) != 4 or not value.isascii() for value in subrecord_signatures):
        raise click.ClickException("--has-subrecord values must be four-character ASCII signatures")
    if include_subrecord_data and not subrecord_signatures:
        raise click.ClickException("--include-subrecord-data requires --has-subrecord")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("list-records requires the native ESP backend.")
        if match_pattern is None or (match_pattern == "*" and not mode_substring and not mode_regex):
            from creation_lib.esp import native_runtime
            matches = [{"form_id": row[4], "signature": row[2], "editor_id": row[1] or None}
                       for row in native_runtime.plugin_handle_record_index_rows(handle, signatures=signatures or None)]
            if match_full and match_pattern is not None:
                names = {row["form_id"]: row.get("full_name") for row in plugin.search_records(
                    "*", match_full=True, read_full=True, signatures=signatures or None)}
                for match in matches:
                    match["full_name"] = names.get(match["form_id"])
        else:
            matches = plugin.search_records(
                match_pattern,
                mode=_resolve_search_mode(mode_substring, mode_regex),
                match_full=match_full,
                read_full=match_full,
                signatures=signatures or None,
                case_sensitive=case_sensitive,
                limit=None if subrecord_signatures else max_results,
            )
        if subrecord_signatures:
            from creation_lib.esp import native_runtime

            form_ids = set(
                native_runtime.plugin_handle_record_form_ids_with_subrecords(
                    handle, list(subrecord_signatures)
                )
            )
            matches = [match for match in matches if int(match["form_id"]) in form_ids]
        if max_results is not None:
            matches = matches[:max_results]
        records = []
        for match in matches:
            record = _format_match(match, detail=match_full and match_pattern is not None)
            if include_subrecord_data:
                subrecords = native_runtime.plugin_handle_record_subrecords(
                    handle, int(match["form_id"])
                )
                record["subrecord_data"] = {
                    signature: [
                        data.hex().upper()
                        for actual_signature, data, _semantic_type in subrecords or []
                        if actual_signature == signature
                    ]
                    for signature in subrecord_signatures
                }
            records.append(record)
        result = {
            "plugin": plugin.plugin_name,
            "type": signatures[0] if len(signatures) == 1 else None,
            "types": signatures,
            "subrecords": list(subrecord_signatures),
            "count": len(records),
            "records": records,
        }
        output(result, ctx.obj.get("fmt", "json"))


@esp.command(name="cell-children")
@click.argument("plugin_path")
@click.argument("cell_id")
@click.option("--temporary-only", is_flag=True, default=False, help="Return only records in the CELL Temporary group (type 9).")
@click.option("--lazy/--eager", default=True, show_default=True, help="Use the index-only reader for large plugins.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.pass_context
def cell_children(ctx, plugin_path, cell_id, temporary_only, lazy, strings_dir, language):
    """List records nested under a CELL's child group.

    CELL_ID is a local hexadecimal object ID (for example 0036CD) or a CELL
    EditorID. Results retain each record's immediate group type so callers can
    distinguish Persistent (8), Temporary (9), and direct type-6 records.
    """
    from creation_lib.esp import native_runtime

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend="native",
        lazy_index=lazy,
    ) as plugin:
        handle = _require_native(plugin, "cell-children")
        cell_form_id = _resolve_cell_raw_form_id(handle, cell_id)
        children = native_runtime.plugin_handle_collect_cell_children(handle, cell_form_id)
        if temporary_only:
            children = [child for child in children if int(child.get("group_type", -1)) == 9]
        output(
            {
                "plugin": plugin.plugin_name,
                "cell_form_id": f"{cell_form_id:08X}",
                "cell_object_id": f"{cell_form_id & 0x00FFFFFF:06X}",
                "lazy": lazy,
                "temporary_only": temporary_only,
                "count": len(children),
                "children": children,
            },
            ctx.obj.get("fmt", "json"),
        )


@esp.command(name="cell-slice-roots")
@click.argument("plugin_path")
@click.argument("worldspace_editor_id")
@click.option("--min-x", type=int, required=True)
@click.option("--min-y", type=int, required=True)
@click.option("--max-x", type=int, required=True)
@click.option("--max-y", type=int, required=True)
@click.option(
    "--include-persistent-cell",
    is_flag=True,
    default=False,
    help="Include the worldspace persistent CELL and its placed children.",
)
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.pass_context
def cell_slice_roots(
    ctx,
    plugin_path,
    worldspace_editor_id,
    min_x,
    min_y,
    max_x,
    max_y,
    include_persistent_cell,
    strings_dir,
    language,
):
    """Collect CELLs and dependency roots inside one WRLD coordinate slice."""
    from creation_lib.esp import native_runtime

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    if min_x > max_x or min_y > max_y:
        raise click.ClickException("slice minimum coordinates must not exceed maxima")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend="native",
    ) as plugin:
        handle = _require_native(plugin, "cell-slice-roots")
        result = native_runtime.plugin_handle_collect_cell_slice_roots(
            handle,
            worldspace_editor_id=worldspace_editor_id,
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            include_worldspace_persistent_cell=include_persistent_cell,
        )
        result["plugin"] = plugin.plugin_name
        result["worldspace_editor_id"] = worldspace_editor_id
        result["bounds"] = {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
        }
        output(result, ctx.obj.get("fmt", "json"))


@esp.command(name="collect-assets")
@click.argument("plugin_path")
@click.option("--kind", "asset_kinds", multiple=True, help="Asset kind to include, e.g. nif. Repeatable.")
@click.option("--type", "record_types", multiple=True, help="Record signature to include, e.g. MISC. Repeatable.")
@click.option("--form-key", "form_keys", multiple=True, help="Specific FormKey to scan. Repeatable.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def collect_assets(ctx, plugin_path, asset_kinds, record_types, form_keys, strings_dir, language, backend, full_load):
    """Collect asset paths referenced by records in a plugin."""
    from creation_lib.esp import native_runtime
    from creation_lib.esp.record_types import record_type_signature

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    signatures = [
        record_type_signature(record_type)
        for record_type in record_types
        if str(record_type).strip()
    ]
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("collect-assets requires the native ESP backend.")
        assets = native_runtime.plugin_handle_collect_assets(
            [handle],
            [],
            asset_kinds=list(asset_kinds) or None,
            signatures=signatures or None,
            form_keys=list(form_keys) or None,
        )
    result = {
        "plugin": plugin_file.name,
        "asset_kinds": list(asset_kinds),
        "record_types": signatures,
        "form_keys": list(form_keys),
        "count": len(assets),
        "assets": assets,
    }
    output(result, ctx.obj.get("fmt", "json"))


@esp.command(name="search")
@click.argument("plugin_path")
@click.argument("pattern")
@_search_flags
@click.option("--limit", type=int, default=None, help="Limit the number of matches returned.")
@click.option("--detail", is_flag=True, default=False, help="Include each match's full/display name in the output.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def search(ctx, plugin_path, pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, limit, detail, strings_dir, language, backend, full_load):
    """Search records by EditorID with glob/substring/regex matching.

    PATTERN is a shell-style glob by default (e.g. '*Plasma*', 'B21_??Gun'). Use
    --substring or --regex to switch modes, --full to also match the display name,
    and --type to scope to a record type. Matching is case-insensitive unless
    --case-sensitive is given.
    """
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    mode = _resolve_search_mode(mode_substring, mode_regex)
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("search requires the native ESP backend.")
        matches = _run_search(
            plugin,
            pattern,
            mode=mode,
            match_full=match_full,
            read_full=detail,
            record_type=record_type,
            case_sensitive=case_sensitive,
            limit=limit,
        )
        show_full = detail or match_full
        result = {
            "plugin": plugin.plugin_name,
            "mode": mode,
            "pattern": pattern,
            "count": len(matches),
            "records": [_format_match(match, detail=show_full) for match in matches],
        }
        output(result, ctx.obj.get("fmt", "json"))


@esp.command(name="get-record")
@click.argument("plugin_path")
@click.argument("record_id")
@click.option("--authoring", is_flag=True, default=False, help="Emit the authoring-schema shape that set-record consumes (single-key fields, structured references) instead of the lossless dump.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def get_record(ctx, plugin_path, record_id, authoring, strings_dir, language, backend, full_load):
    """Dump a single record as a JSON object.

    RECORD_ID is an EditorID or a plugin-local FormID in hex (e.g. 000800).
    Pass --authoring to get the shape `set-record` round-trips.
    """
    import json

    from creation_lib.esp import native_runtime

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("get-record requires the native ESP backend.")
        form_id = _resolve_record_id(plugin, record_id)
        if form_id is None:
            output({"error": f"not a FormID or EditorID: {record_id}"}, ctx.obj.get("fmt", "json"))
            return
        if authoring:
            record = plugin.read_authoring_record(form_id)
            if record is None:
                output({"error": f"record not found: {record_id}"}, ctx.obj.get("fmt", "json"))
                return
            output(record, ctx.obj.get("fmt", "json"))
            return
        try:
            text = native_runtime.plugin_handle_call(handle, "export_record_text", form_id, "json")
        except (KeyError, RuntimeError):
            output({"error": f"record not found: {record_id}"}, ctx.obj.get("fmt", "json"))
            return
        output(json.loads(text), ctx.obj.get("fmt", "json"))


@esp.command(name="get-records")
@click.argument("plugin_path")
@click.argument("record_ids", nargs=-1, required=True)
@click.option("--authoring", is_flag=True, default=False, help="Emit the authoring-schema shape that set-record consumes.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def get_records(ctx, plugin_path, record_ids, authoring, strings_dir, language, backend, full_load):
    """Dump multiple records while opening PLUGIN_PATH only once.

    RECORD_IDS are EditorIDs or plugin-local FormIDs in hex. Missing records are
    reported in the response without preventing the remaining records from loading.
    """
    import json

    from creation_lib.esp import native_runtime

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("get-records requires the native ESP backend.")
        records = []
        missing = []
        for record_id in record_ids:
            form_id = _resolve_record_id(plugin, record_id)
            if form_id is None:
                missing.append(record_id)
                continue
            if authoring:
                record = plugin.read_authoring_record(form_id)
                if record is None:
                    missing.append(record_id)
                    continue
                records.append(record)
                continue
            try:
                text = native_runtime.plugin_handle_call(
                    handle, "export_record_text", form_id, "json"
                )
            except (KeyError, RuntimeError):
                missing.append(record_id)
                continue
            records.append(json.loads(text))
        output(
            {
                "plugin": plugin.plugin_name,
                "requested": len(record_ids),
                "found": len(records),
                "records": records,
                "missing": missing,
            },
            ctx.obj.get("fmt", "json"),
        )


@esp.command(name="set-record")
@click.argument("plugin_path")
@click.argument("source")
@click.option("--type", "record_type", default=None, help="Record type for new records, e.g. WEAP or a display alias like Weapons. Inferred from the existing record when updating. Applies to every record in an array.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be upserted without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def set_record(ctx, plugin_path, source, record_type, dry_run, output_path, strings_dir, language, backend):
    """Insert or replace one or many records from authoring-schema JSON.

    SOURCE is a JSON file (the shape `get-record` emits) or - for stdin, holding either a
    single record object or an array of record objects (bulk upsert). A record whose FormID
    already exists is fully replaced; otherwise it is inserted, with a FormID allocated when
    form_id is absent or "000000".
    """
    import json
    import sys

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid record JSON: {exc}")
    if isinstance(data, dict):
        records = [data]
    elif isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            raise click.ClickException("Record JSON array must contain only objects.")
        records = data
    else:
        raise click.ClickException("Record JSON must be an object or an array of objects.")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("set-record requires the native ESP backend.")
        results = [_upsert_authoring(plugin, record, record_type, dry_run=dry_run) for record in records]
        if results and not dry_run:
            plugin.save(target, backend=backend)
        if isinstance(data, dict):
            single = results[0]
            payload = {
                "plugin": plugin.plugin_name,
                "signature": single["signature"],
                "upserted": single["upserted"],
                "created": single["created"],
                "output": None if dry_run else str(target),
            }
            if dry_run:
                payload["dry_run"] = True
            output(payload, fmt)
        else:
            output(
                {
                    "plugin": plugin.plugin_name,
                    "count": len(results),
                    "created": sum(1 for r in results if r["created"]),
                    "dry_run": dry_run,
                    "output": None if dry_run else str(target),
                    "records": results,
                },
                fmt,
            )


@esp.command(name="delete-record")
@click.argument("plugin_path")
@click.argument("record_id")
@click.option("--cascade", is_flag=True, default=False, help="Also strip references to the deleted record (leveled-list/container/form-list entries and scalar reference slots).")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be deleted without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def delete_record(ctx, plugin_path, record_id, cascade, dry_run, output_path, strings_dir, language, backend):
    """Delete a record by EditorID or local hex FormID (e.g. 000800).

    With --cascade, also remove every reference to it from other records before
    deleting (leveled lists, form lists, container contents, and scalar slots).
    Use --dry-run to confirm the record resolves without modifying anything.
    """
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("delete-record requires the native ESP backend.")
        form_id = _resolve_record_id(plugin, record_id)
        if form_id is None:
            output({"error": f"not a FormID or EditorID: {record_id}"}, fmt)
            return
        if plugin.get_record_by_form_id(form_id) is None:
            output({"error": f"record not found: {record_id}"}, fmt)
            return
        delete_report = None
        if not dry_run:
            from creation_lib.esp import native_runtime

            delete_report = native_runtime.plugin_handle_delete_records(
                handle,
                [form_id],
                cascade=cascade,
            )
            if delete_report["removed"] != 1:
                output({"error": f"failed to remove record: {record_id}"}, fmt)
                return
            plugin.save(target, backend=backend)
        result = {
            "plugin": plugin.plugin_name,
            "deleted": record_id,
            "form_id": f"{form_id & 0x00FFFFFF:06X}",
            "dry_run": dry_run,
            "output": None if dry_run else str(target),
        }
        if cascade and delete_report is not None:
            result["cascade"] = {
                "records_modified": delete_report["records_modified"],
                "refs_removed": delete_report["refs_removed"],
            }
        output(result, fmt)


@esp.command(name="copy-record")
@click.argument("source_plugin")
@click.argument("target_plugin")
@click.argument("record_id", required=False)
@click.option("--match", "match_pattern", default=None, help="Copy every record matching this pattern instead of a single RECORD_ID.")
@_search_flags
@click.option("--override", is_flag=True, default=False, help="Copy as an override (keep the source FormID) instead of allocating a new FormID in the target.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be copied without saving the target.")
@click.option("--output", "output_path", default=None, help="Write the modified target here instead of overwriting TARGET_PLUGIN.")
@click.option("--strings-dir", default=None, help="Override localized strings directory (applied to both plugins).")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def copy_record(ctx, source_plugin, target_plugin, record_id, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, override, dry_run, output_path, strings_dir, language, backend):
    """Copy a record (or all --match matches) from SOURCE_PLUGIN into TARGET_PLUGIN.

    Provide either a single RECORD_ID (EditorID or local hex FormID) or --match PATTERN.
    By default a new FormID is allocated in the target; --override keeps the source FormID.
    """
    fmt = ctx.obj.get("fmt", "json")
    if (record_id is None) == (match_pattern is None):
        raise click.ClickException("Provide exactly one of RECORD_ID or --match.")
    src_file = Path(source_plugin)
    dst_file = Path(target_plugin)
    if not src_file.is_file():
        raise click.ClickException(f"Source plugin not found: {src_file}")
    if not dst_file.is_file():
        raise click.ClickException(f"Target plugin not found: {dst_file}")
    out_path = Path(output_path) if output_path else dst_file
    game = ctx.obj.get("game")
    with _load_plugin(src_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as source, \
         _load_plugin(dst_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as dest:
        if getattr(source, "_rust_handle", None) is None or getattr(dest, "_rust_handle", None) is None:
            raise click.ClickException("copy-record requires the native ESP backend.")
        if record_id is not None:
            fid = _resolve_record_id(source, record_id)
            if fid is None or source.get_record_by_form_id(fid) is None:
                output({"error": f"record not found in source: {record_id}"}, fmt)
                return
            source_fids = [fid]
        else:
            matches = _run_search(
                source,
                match_pattern,
                mode=_resolve_search_mode(mode_substring, mode_regex),
                match_full=match_full,
                read_full=False,
                record_type=record_type,
                case_sensitive=case_sensitive,
                limit=None,
            )
            source_fids = [match["form_id"] for match in matches]
        copied = []
        for fid in source_fids:
            summary = source.get_record_by_form_id(fid)
            if summary is None:
                continue
            entry = {
                "editor_id": getattr(summary, "editor_id", None),
                "source_form_id": f"{int(summary.form_id) & 0x00FFFFFF:06X}",
            }
            if not dry_run:
                result = dest.copy_override(summary, source) if override else dest.copy_record(summary, source)
                if result is None:
                    continue
                entry["new_form_id"] = f"{int(result.form_id) & 0x00FFFFFF:06X}"
            copied.append(entry)
        if copied and not dry_run:
            dest.save(out_path, backend=backend)
        output(
            {
                "source": source.plugin_name,
                "target": dest.plugin_name,
                "copied": len(copied),
                "override": override,
                "dry_run": dry_run,
                "output": None if dry_run else str(out_path),
                "records": copied,
            },
            fmt,
        )


@esp.command(name="copy")
@click.argument("source_plugin")
@click.argument("target_plugin")
@click.argument("record_id")
@click.option("--mode", type=click.Choice(["new", "override"]), default="new", show_default=True,
              help="new: allocate a fresh FormID in the target. override: keep the source FormID and add the source as a master.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be copied without saving the target.")
@click.option("--output", "output_path", default=None, help="Write the modified target here instead of overwriting TARGET_PLUGIN.")
@click.option("--strings-dir", default=None, help="Override localized strings directory (applied to both plugins).")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def copy(ctx, source_plugin, target_plugin, record_id, mode, dry_run, output_path, strings_dir, language, backend):
    """Copy RECORD_ID from SOURCE_PLUGIN into TARGET_PLUGIN.

    RECORD_ID is an EditorID or local hex FormID. --mode new (default) allocates a
    fresh FormID from the target's high-water mark; --mode override keeps the source
    FormID, adding SOURCE_PLUGIN as a master so the record overrides the source.
    """
    fmt = ctx.obj.get("fmt", "json")
    src_file = Path(source_plugin)
    dst_file = Path(target_plugin)
    if not src_file.is_file():
        raise click.ClickException(f"Source plugin not found: {src_file}")
    if not dst_file.is_file():
        raise click.ClickException(f"Target plugin not found: {dst_file}")
    out_path = Path(output_path) if output_path else dst_file
    game = ctx.obj.get("game")
    with _load_plugin(src_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as source, \
         _load_plugin(dst_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as dest:
        if getattr(source, "_rust_handle", None) is None or getattr(dest, "_rust_handle", None) is None:
            raise click.ClickException("copy requires the native ESP backend.")
        fid = _resolve_record_id(source, record_id)
        summary = source.get_record_by_form_id(fid) if fid is not None else None
        if summary is None:
            output({"error": f"record not found in source: {record_id}"}, fmt)
            return
        entry = {
            "editor_id": getattr(summary, "editor_id", None),
            "source_form_id": f"{int(summary.form_id) & 0x00FFFFFF:06X}",
        }
        if not dry_run:
            try:
                result = dest.copy_override(summary, source) if mode == "override" else dest.copy_record(summary, source)
            except ValueError as exc:
                output({"error": str(exc)}, fmt)
                return
            if result is None:
                output({"error": f"copy failed for record: {record_id}"}, fmt)
                return
            entry["new_form_id"] = f"{int(result.form_id) & 0x00FFFFFF:06X}"
            dest.save(out_path, backend=backend)
        output(
            {
                "source": source.plugin_name,
                "target": dest.plugin_name,
                "mode": mode,
                "dry_run": dry_run,
                "output": None if dry_run else str(out_path),
                "record": entry,
            },
            fmt,
        )


@esp.command(name="merge")
@click.argument("source_plugin")
@click.argument("target_plugin")
@click.option("--mode", type=click.Choice(["new", "override"]), default="new", show_default=True,
              help="new: allocate fresh FormIDs in the target. override: keep source FormIDs and add SOURCE as a master.")
@click.option("--match", "match_pattern", default=None, help="Only merge records matching this pattern (default: every top-level record).")
@_search_flags
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be merged without saving the target.")
@click.option("--output", "output_path", default=None, help="Write the modified target here instead of overwriting TARGET_PLUGIN.")
@click.option("--strings-dir", default=None, help="Override localized strings directory (applied to both plugins).")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def merge(ctx, source_plugin, target_plugin, mode, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, dry_run, output_path, strings_dir, language, backend):
    """Bulk-copy SOURCE_PLUGIN's records into TARGET_PLUGIN.

    Merges every top-level record (or only --match matches / --type records).
    --mode override keeps source FormIDs and adds SOURCE as a master; --mode new
    allocates fresh FormIDs. Record-granular like `copy` — CELL/WRLD child
    re-parenting is not handled, and in --mode new, references between merged
    records are not re-pointed at their new FormIDs (use --mode override to keep
    references intact).
    """
    fmt = ctx.obj.get("fmt", "json")
    src_file = Path(source_plugin)
    dst_file = Path(target_plugin)
    if not src_file.is_file():
        raise click.ClickException(f"Source plugin not found: {src_file}")
    if not dst_file.is_file():
        raise click.ClickException(f"Target plugin not found: {dst_file}")
    out_path = Path(output_path) if output_path else dst_file
    game = ctx.obj.get("game")
    record_cap = 100
    with _load_plugin(src_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as source, \
         _load_plugin(dst_file, game=game, strings_dir=strings_dir, language=language, backend=backend) as dest:
        if getattr(source, "_rust_handle", None) is None or getattr(dest, "_rust_handle", None) is None:
            raise click.ClickException("merge requires the native ESP backend.")
        if match_pattern is not None:
            matches = _run_search(
                source,
                match_pattern,
                mode=_resolve_search_mode(mode_substring, mode_regex),
                match_full=match_full,
                read_full=False,
                record_type=record_type,
                case_sensitive=case_sensitive,
                limit=None,
            )
            source_fids = [match["form_id"] for match in matches]
        else:
            from creation_lib.esp import native_runtime as _native_runtime
            signatures = [record_type] if record_type else None
            source_fids = list(_native_runtime.plugin_handle_record_form_ids(source._rust_handle, signatures))
        override = mode == "override"
        copied = []
        for fid in source_fids:
            summary = source.get_record_by_form_id(fid)
            if summary is None:
                continue
            entry = {
                "editor_id": getattr(summary, "editor_id", None),
                "source_form_id": f"{int(summary.form_id) & 0x00FFFFFF:06X}",
            }
            if not dry_run:
                result = dest.copy_override(summary, source) if override else dest.copy_record(summary, source)
                if result is None:
                    continue
                entry["new_form_id"] = f"{int(result.form_id) & 0x00FFFFFF:06X}"
            copied.append(entry)
        if copied and not dry_run:
            dest.save(out_path, backend=backend)
        output(
            {
                "source": source.plugin_name,
                "target": dest.plugin_name,
                "mode": mode,
                "merged": len(copied),
                "dry_run": dry_run,
                "output": None if dry_run else str(out_path),
                "records": copied[:record_cap],
                "records_truncated": max(0, len(copied) - record_cap),
            },
            fmt,
        )


def _diff_index(plugin, record_type) -> dict:
    """Map object-id → record summary (signature, editor_id, payload hash)."""
    from creation_lib.esp import native_runtime as _native_runtime

    handle = plugin._rust_handle
    signatures = [record_type] if record_type else None
    index = {}
    for fid in _native_runtime.plugin_handle_record_form_ids(handle, signatures):
        summary = plugin.get_record_by_form_id(fid)
        if summary is None:
            continue
        obj = int(fid) & 0x00FFFFFF
        index[obj] = {
            "object_id": f"{obj:06X}",
            "signature": summary.signature,
            "editor_id": getattr(summary, "editor_id", None),
            "hash": _native_runtime.plugin_handle_record_payload_hash(handle, fid),
            "form_id": int(fid),
        }
    return index


def _diff_view(entry: dict) -> dict:
    return {k: entry[k] for k in ("object_id", "signature", "editor_id")}


def _subrecord_delta(a_json: dict, b_json: dict) -> dict:
    import json as _json

    def by_signature(record: dict) -> dict:
        grouped: dict = {}
        for field in record.get("fields", []):
            grouped.setdefault(field.get("signature"), []).append(
                _json.dumps(field.get("value"), sort_keys=True)
            )
        return grouped

    a, b = by_signature(a_json), by_signature(b_json)
    added, removed, changed = [], [], []
    for sig in sorted(set(a) | set(b)):
        av, bv = a.get(sig), b.get(sig)
        if av is None:
            added.append(sig)
        elif bv is None:
            removed.append(sig)
        elif sorted(av) != sorted(bv):
            changed.append(sig)
    return {
        "eid_changed": a_json.get("eid") != b_json.get("eid"),
        "subrecords_added": added,
        "subrecords_removed": removed,
        "subrecords_changed": changed,
    }


@esp.command(name="diff")
@click.argument("plugin_a")
@click.argument("plugin_b")
@click.option("--detail", is_flag=True, default=False, help="Also report per-subrecord deltas for changed records.")
@click.option("--type", "record_type", default=None, help="Restrict the diff to a record type, e.g. WEAP.")
@click.option("--semantic", is_flag=True, help="Compare decoded field paths and values.")
@click.option("--record", "record_ids", multiple=True, help="Semantic diff: only these EditorIDs or local hex IDs.")
@click.option("--field", "fields", multiple=True, help="Semantic diff: include this dotted path/prefix/glob.")
@click.option("--exclude", multiple=True, help="Semantic diff: ignore this dotted path/prefix/glob.")
@click.option("--normalize-references/--raw-references", default=True, help="Semantic diff: canonicalize structured FormKeys and self-plugin names.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def diff(ctx, plugin_a, plugin_b, detail, record_type, semantic, record_ids, fields, exclude, normalize_references, backend):
    """Record-level diff of PLUGIN_A and PLUGIN_B (read-only).

    Reports records added (in B only), removed (in A only), and changed (in both,
    by differing payload). Records are keyed by local object-id, so this is most
    meaningful between two versions of the same plugin. --detail lists the
    changed/added/removed subrecord signatures for each changed record.
    """
    fmt = ctx.obj.get("fmt", "json")
    a_file = Path(plugin_a)
    b_file = Path(plugin_b)
    if not a_file.is_file():
        raise click.ClickException(f"Plugin not found: {a_file}")
    if not b_file.is_file():
        raise click.ClickException(f"Plugin not found: {b_file}")
    if semantic:
        from cli.inspection_commands import semantic_diff
        return semantic_diff(ctx, a_file, b_file, record_type, record_ids, fields, exclude, normalize_references)
    if record_ids or fields or exclude:
        raise click.UsageError("--record, --field and --exclude require --semantic")
    game = ctx.obj.get("game")
    with _load_plugin(a_file, game=game, strings_dir=None, language=None, backend=backend) as plugin_a_obj, \
         _load_plugin(b_file, game=game, strings_dir=None, language=None, backend=backend) as plugin_b_obj:
        _require_native(plugin_a_obj, "diff")
        _require_native(plugin_b_obj, "diff")
        a_idx = _diff_index(plugin_a_obj, record_type)
        b_idx = _diff_index(plugin_b_obj, record_type)
        a_keys, b_keys = set(a_idx), set(b_idx)
        added = sorted(b_keys - a_keys)
        removed = sorted(a_keys - b_keys)
        changed = sorted(k for k in (a_keys & b_keys) if a_idx[k]["hash"] != b_idx[k]["hash"])

        changed_records = []
        for key in changed:
            record = _diff_view(b_idx[key])
            if detail:
                from creation_lib.esp import native_runtime as _native_runtime
                a_json = json.loads(_native_runtime.plugin_handle_call(
                    plugin_a_obj._rust_handle, "export_record_text", a_idx[key]["form_id"], "json"))
                b_json = json.loads(_native_runtime.plugin_handle_call(
                    plugin_b_obj._rust_handle, "export_record_text", b_idx[key]["form_id"], "json"))
                record["detail"] = _subrecord_delta(a_json, b_json)
            changed_records.append(record)

        output(
            {
                "plugin_a": plugin_a_obj.plugin_name,
                "plugin_b": plugin_b_obj.plugin_name,
                "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)},
                "added": [_diff_view(b_idx[k]) for k in added],
                "removed": [_diff_view(a_idx[k]) for k in removed],
                "changed": changed_records,
            },
            fmt,
        )


@esp.group("masters")
@click.pass_context
def masters(ctx):
    """Inspect and edit a plugin's master (MAST) list."""


@masters.command("list")
@click.argument("plugin_path")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def masters_list(ctx, plugin_path, backend):
    """List PLUGIN_PATH's masters with size and whether each is referenced."""
    from creation_lib.esp import native_runtime as _native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=None, language=None, backend=backend) as plugin:
        handle = _require_native(plugin, "masters list")
        names = list(plugin.header.masters)
        sizes = list(plugin.header.master_sizes)
        used = {int(i) for i in _native_runtime.plugin_handle_call(handle, "used_master_indices")}
        output(
            {
                "plugin": plugin.plugin_name,
                "count": len(names),
                "masters": [
                    {
                        "index": index,
                        "name": name,
                        "size": sizes[index] if index < len(sizes) else 0,
                        "used": index in used,
                    }
                    for index, name in enumerate(names)
                ],
            },
            fmt,
        )


@masters.command("add")
@click.argument("plugin_path")
@click.argument("master_names", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def masters_add(ctx, plugin_path, master_names, dry_run, output_path, backend):
    """Add one or more MASTER_NAMES to PLUGIN_PATH (idempotent)."""
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    game = ctx.obj.get("game")
    search_paths = _esp_master_search_paths(game, plugin_file)
    with _load_plugin(plugin_file, game=game, strings_dir=None, language=None, backend=backend) as plugin:
        _require_native(plugin, "masters add")
        existing = {m.lower() for m in plugin.header.masters}
        added = []
        for name in master_names:
            if name.lower() in existing:
                continue
            size = _resolve_master_size(name, search_paths)
            if not dry_run:
                plugin.add_master(name, size=size)
            added.append({"name": name, "size": size})
            existing.add(name.lower())
        if added and not dry_run:
            plugin.save(out_path, backend=backend)
        output(
            {
                "plugin": plugin.plugin_name,
                "added": added,
                "masters": list(plugin.header.masters),
                "dry_run": dry_run,
                "output": None if dry_run else str(out_path),
            },
            fmt,
        )


@masters.command("remove")
@click.argument("plugin_path")
@click.argument("master_names", nargs=-1, required=True)
@click.option("--force", is_flag=True, default=False, help="Remove even when referenced: nulls dangling refs and drops overrides of the master.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def masters_remove(ctx, plugin_path, master_names, force, dry_run, output_path, backend):
    """Remove one or more MASTER_NAMES from PLUGIN_PATH.

    Refuses when a target master is still referenced unless --force, which nulls
    the dangling references and drops any records overriding that master.
    """
    from creation_lib.esp import native_runtime as _native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    game = ctx.obj.get("game")
    with _load_plugin(plugin_file, game=game, strings_dir=None, language=None, backend=backend) as plugin:
        handle = _require_native(plugin, "masters remove")
        names = list(plugin.header.masters)
        sizes = list(plugin.header.master_sizes)
        lower_to_index = {name.lower(): index for index, name in enumerate(names)}
        remove_indices = set()
        for name in master_names:
            index = lower_to_index.get(name.lower())
            if index is None:
                raise click.ClickException(f"Not a master of {plugin.plugin_name}: {name}")
            remove_indices.add(index)
        used = {int(i) for i in _native_runtime.plugin_handle_call(handle, "used_master_indices")}
        blocked = sorted(index for index in remove_indices if index in used)
        if blocked and not force:
            raise click.ClickException(
                "Referenced master(s) require --force to remove: "
                + ", ".join(names[index] for index in blocked)
            )
        result = {
            "plugin": plugin.plugin_name,
            "removed": [names[index] for index in sorted(remove_indices)],
            "forced": bool(blocked and force),
            "refs_nulled": 0,
            "overrides_dropped": 0,
            "dry_run": dry_run,
            "masters": [name for index, name in enumerate(names) if index not in remove_indices],
            "output": None,
        }
        if not dry_run:
            if blocked and force:
                form_ids = list(_native_runtime.plugin_handle_record_form_ids(handle))
                for index in blocked:
                    for form_id in form_ids:
                        if ((form_id >> 24) & 0xFF) == index and plugin.remove_record_by_form_id(form_id):
                            result["overrides_dropped"] += 1
                    result["refs_nulled"] += _native_runtime.plugin_handle_null_refs_to_master(handle, index)
            survivors = [
                (name, sizes[index] if index < len(sizes) else 0)
                for index, name in enumerate(names)
                if index not in remove_indices
            ]
            plugin.set_masters(survivors)
            plugin.save(out_path, backend=backend)
            result["masters"] = list(plugin.header.masters)
            result["output"] = str(out_path)
        output(result, fmt)


@masters.command("reorder")
@click.argument("plugin_path")
@click.argument("master_names", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def masters_reorder(ctx, plugin_path, master_names, dry_run, output_path, backend):
    """Reorder PLUGIN_PATH's masters to the given order (a permutation of the current list)."""
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    game = ctx.obj.get("game")
    with _load_plugin(plugin_file, game=game, strings_dir=None, language=None, backend=backend) as plugin:
        _require_native(plugin, "masters reorder")
        names = list(plugin.header.masters)
        sizes = list(plugin.header.master_sizes)
        if sorted(n.lower() for n in master_names) != sorted(n.lower() for n in names):
            raise click.ClickException(
                "reorder requires a permutation of the current masters: " + ", ".join(names)
            )
        size_by_lower = {name.lower(): (sizes[index] if index < len(sizes) else 0) for index, name in enumerate(names)}
        ordered = [(name, size_by_lower[name.lower()]) for name in master_names]
        if not dry_run:
            plugin.set_masters(ordered)
            plugin.save(out_path, backend=backend)
        output(
            {
                "plugin": plugin.plugin_name,
                "masters": [name for name, _ in ordered],
                "dry_run": dry_run,
                "output": None if dry_run else str(out_path),
            },
            fmt,
        )


def _header_flag_states(handle) -> dict:
    from creation_lib.esp.editor import header_flags

    return {
        "esm": header_flags.is_master(handle),
        "esl": header_flags.is_light(handle),
        "medium": header_flags.is_medium(handle),
        "update": bool(header_flags.get_flags(handle) & header_flags.FLAG_UPDATE),
        "localized": header_flags.is_localized(handle),
    }


def _header_snapshot(plugin, handle) -> dict:
    return {
        "author": plugin.header.author,
        "description": plugin.header.description,
        "version": plugin.header.version,
        "next_object_id": f"{int(plugin.header.next_object_id) & 0x00FFFFFF:06X}",
        "flags": _header_flag_states(handle),
        "masters": len(plugin.header.masters),
    }


@esp.command(name="header")
@click.argument("plugin_path")
@click.option("--author", default=None, help="Set the author string.")
@click.option("--description", default=None, help="Set the description string.")
@click.option("--version", "header_version", type=float, default=None, help="Set the HEDR version.")
@click.option("--next-object-id", "next_object_id", default=None, help="Set the next-object-id high-water mark (decimal or 0x-hex).")
@click.option("--esm/--no-esm", "set_master", default=None, help="Toggle the Master (ESM) flag.")
@click.option("--esl/--no-esl", "set_light", default=None, help="Toggle the Light (ESL) flag.")
@click.option("--medium/--no-medium", "set_medium", default=None, help="Toggle the Medium-plugin flag (Starfield).")
@click.option("--update/--no-update", "set_update", default=None, help="Toggle the Update-plugin flag (Starfield).")
@click.option("--localized/--no-localized", "set_localized", default=None, help="Toggle the Localized flag.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def header(ctx, plugin_path, author, description, header_version, next_object_id, set_master, set_light, set_medium, set_update, set_localized, output_path, backend):
    """Show PLUGIN_PATH's header, or edit it when any field/flag option is given.

    With no options the current header is printed. Any --author/--description/
    --version/--next-object-id or flag toggle edits the header and saves.
    """
    from creation_lib.esp.editor import header_flags

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    field_edits = {
        "author": author,
        "description": description,
        "version": header_version,
        "next_object_id": next_object_id,
    }
    flag_edits = {
        header_flags.set_master: set_master,
        header_flags.set_light: set_light,
        header_flags.set_medium: set_medium,
        header_flags.set_update: set_update,
        header_flags.set_localized: set_localized,
    }
    editing = any(v is not None for v in field_edits.values()) or any(v is not None for v in flag_edits.values())
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=None, language=None, backend=backend) as plugin:
        handle = _require_native(plugin, "header")
        if not editing:
            output({"plugin": plugin.plugin_name, **_header_snapshot(plugin, handle)}, fmt)
            return
        if author is not None:
            plugin.header.author = author
        if description is not None:
            plugin.header.description = description
        if header_version is not None:
            plugin.header.version = header_version
        if next_object_id is not None:
            try:
                plugin.header.next_object_id = int(next_object_id, 0)
            except ValueError:
                raise click.ClickException(f"--next-object-id must be decimal or 0x-hex: {next_object_id}")
        for setter, value in flag_edits.items():
            if value is not None:
                setter(handle, value)
        plugin.save(out_path, backend=backend)
        output(
            {
                "plugin": plugin.plugin_name,
                **_header_snapshot(plugin, handle),
                "output": str(out_path),
            },
            fmt,
        )


@esp.command(name="compact-esl")
@click.argument("plugin_path")
@click.option("--floor", "floor_text", default="0x800", show_default=True, help="Lowest object-id of the ESL window (decimal or 0x-hex).")
@click.option("--set-esl/--no-set-esl", "set_esl", default=True, show_default=True, help="Set the Light (ESL) flag after compacting.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def compact_esl(ctx, plugin_path, floor_text, set_esl, dry_run, output_path, backend):
    """Renumber PLUGIN_PATH's owned records into the ESL object-id window.

    Packs owned object-ids into [FLOOR, 0x1000) so the plugin can be a light
    (ESL) plugin, remapping every internal reference in lockstep, then sets the
    Light flag (unless --no-set-esl). Records already inside the window keep
    their id to minimize churn. Refuses when the plugin owns more records than
    the window can hold.
    """
    from creation_lib.esp.editor import header_flags
    from creation_lib.esp import native_runtime as _native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    try:
        floor = int(floor_text, 0) & 0x00FFFFFF
    except ValueError:
        raise click.ClickException(f"--floor must be decimal or 0x-hex: {floor_text}")
    if not 0 < floor < 0x1000:
        raise click.ClickException(f"--floor must be in (0x000, 0x1000): {floor_text}")
    capacity = 0x1000 - floor
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=None, language=None, backend=backend) as plugin:
        handle = _require_native(plugin, "compact-esl")
        own_index = len(plugin.header.masters)
        owned = sorted(_native_runtime.plugin_handle_owned_object_ids(handle))
        if len(owned) > capacity:
            raise click.ClickException(
                f"too many records for ESL: {len(owned)} owned > {capacity} slots in "
                f"[0x{floor:03X}, 0x1000)"
            )
        in_window = [o for o in owned if floor <= o < 0x1000]
        out_window = [o for o in owned if not (floor <= o < 0x1000)]
        free = (slot for slot in range(floor, 0x1000) if slot not in set(in_window))
        mapping = {old: next(free) for old in out_window}
        final_ids = set(in_window) | set(mapping.values())
        new_next = (max(final_ids) + 1) if final_ids else floor
        result = {
            "plugin": plugin.plugin_name,
            "owned": len(owned),
            "remapped": len(mapping),
            "floor": f"0x{floor:03X}",
            "next_object_id": f"{new_next & 0x00FFFFFF:06X}",
            "esl_flag": bool(set_esl),
            "dry_run": dry_run,
            "output": None,
        }
        if not dry_run:
            if mapping:
                _native_runtime.plugin_handle_apply_object_id_mapping(handle, own_index, own_index, mapping)
            plugin.header.next_object_id = new_next
            if set_esl:
                header_flags.set_light(handle, True)
            plugin.save(out_path, backend=backend)
            result["output"] = str(out_path)
        output(result, fmt)


@esp.command(name="clean")
@click.argument("plugin_path")
@click.option("--itm/--no-itm", "do_itm", default=True, show_default=True, help="Remove identical-to-master (ITM) records.")
@click.option("--udr/--no-udr", "do_udr", default=True, show_default=True, help="Undelete-and-disable deleted references (UDR).")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be cleaned without saving.")
@click.option("--output", "output_path", default=None, help="Write here instead of overwriting PLUGIN_PATH.")
@click.pass_context
def clean(ctx, plugin_path, do_itm, do_udr, dry_run, output_path):
    """Remove ITM records and undelete-and-disable deleted references (UDR).

    ITM detection compares overrides against their masters, so the plugin's
    masters must be resolvable on disk; when they are not, ITM is skipped (UDR
    is flag-based and always runs). Mirrors xEdit's "Remove identical to master"
    and "Undelete and disable references".
    """
    from creation_lib.esp.editor import EditorSession, cleanup
    from creation_lib.esp.editor.validate import validate, IssueCategory
    from creation_lib.esp import native_runtime as _native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    out_path = Path(output_path) if output_path else plugin_file
    game = ctx.obj.get("game")
    session = EditorSession(
        default_game=game,
        auto_scan_conflicts=False,
        master_search_paths=_esp_master_search_paths(game, plugin_file),
    )
    try:
        loaded = session.load(plugin_file, game=game)
        handle = loaded.handle
        target_masters = list(_native_runtime.plugin_handle_get(handle, "masters") or [])
        loaded_names = {p.plugin_name.lower() for p in session.plugins}
        missing_masters = [m for m in target_masters if m.lower() not in loaded_names]
        itm_possible = do_itm and not missing_masters
        result = {
            "plugin": loaded.plugin_name,
            "itm_removed": [],
            "udr_fixed": [],
            "missing_masters": missing_masters,
            "itm_skipped": bool(do_itm and missing_masters),
            "dry_run": dry_run,
            "output": None,
        }
        if dry_run:
            report = validate(session, handle=handle)
            if itm_possible:
                result["itm_removed"] = [
                    f"{i.form_id:08X}" for i in report.by_category(IssueCategory.ITM) if i.form_id is not None
                ]
            if do_udr:
                result["udr_fixed"] = [
                    f"{i.form_id:08X}" for i in report.by_category(IssueCategory.UDR) if i.form_id is not None
                ]
        else:
            if itm_possible:
                result["itm_removed"] = [
                    f"{fid:08X}" for fid in cleanup.remove_itm_records(session, handles=[handle])
                ]
            if do_udr:
                result["udr_fixed"] = [
                    f"{fid:08X}" for fid in cleanup.undelete_and_disable_refs(session, handles=[handle])
                ]
            _native_runtime.plugin_handle_call(handle, "save", str(out_path))
            result["output"] = str(out_path)
        output(result, fmt)
    finally:
        session.close_all()


@esp.command(name="count")
@click.argument("plugin_path")
@click.option("--match", "match_pattern", default=None, help="Also report how many records match this EditorID pattern.")
@_search_flags
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.option("--full-load", is_flag=True, default=False, help="Load the whole plugin instead of resolving records lazily. Diagnostic escape hatch; slower and far more memory.")
@click.pass_context
def count(ctx, plugin_path, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, strings_dir, language, backend, full_load):
    """Report record counts: total, per-signature breakdown, and an optional --match count.

    Totals include records in nested groups. Only headers are scanned;
    --match also scans EditorIDs (see `esp search`).
    """
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
        lazy_index=not full_load,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("count requires the native ESP backend.")
        from creation_lib.esp import native_runtime
        sig_counts = native_runtime.plugin_handle_record_counts(plugin._rust_handle)
        result = {"plugin": plugin.plugin_name, "record_count": sum(sig_counts.values())}
        sig = record_type_signature(record_type) if record_type else None
        if sig:
            result["type"] = sig
            result["type_count"] = sig_counts.get(sig, 0)
        else:
            result["signatures"] = [
                {"signature": name, "count": c}
                for name, c in sorted(sig_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
        if match_pattern is not None:
            matches = _run_search(
                plugin,
                match_pattern,
                mode=_resolve_search_mode(mode_substring, mode_regex),
                match_full=match_full,
                read_full=False,
                record_type=record_type,
                case_sensitive=case_sensitive,
                limit=None,
            )
            result["match_pattern"] = match_pattern
            result["match_count"] = len(matches)
        output(result, fmt)


@esp.command(name="set-field")
@click.argument("plugin_path")
@click.option("--match", "match_pattern", required=True, help="EditorID pattern selecting records to edit (see `esp search`).")
@_search_flags
@click.option("--field", "field_label", required=True, help="Authoring field label to set, e.g. FULL (the key `get-record --authoring` shows; it can vary by record type).")
@click.option("--value", "value_text", required=True, help="New value as JSON (e.g. '100' or '\"Name\"'); falls back to a raw string.")
@click.option("--add-missing", is_flag=True, default=False, help="Append the field to matched records that don't already have it.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def set_field(ctx, plugin_path, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, field_label, value_text, add_missing, dry_run, output_path, strings_dir, language, backend):
    """Set one field to the same value across every record matching --match.

    FIELD is the authoring label (run `get-record --authoring` to see it; note labels
    such as FULL vs Name can differ by record type). VALUE is parsed as JSON, so use
    '"text"' for a string and '100' for a number. To rename EditorIDs use `esp rename`.
    """
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    value = _parse_field_value(value_text)
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("set-field requires the native ESP backend.")
        matches = _run_search(
            plugin,
            match_pattern,
            mode=_resolve_search_mode(mode_substring, mode_regex),
            match_full=match_full,
            read_full=False,
            record_type=record_type,
            case_sensitive=case_sensitive,
            limit=None,
        )
        changes = []
        for match in matches:
            record = plugin.read_authoring_record(match["form_id"])
            if not isinstance(record, dict) or not isinstance(record.get("fields"), list):
                continue
            changed, found, old_values = _set_field_in_fields(
                record["fields"], field_label, value, add_missing=add_missing
            )
            if not changed:
                continue
            changes.append({
                "form_id": f"{match['form_id'] & 0x00FFFFFF:06X}",
                "editor_id": match["editor_id"],
                "old": old_values[0] if old_values else None,
                "added": not found,
            })
            if not dry_run:
                record["signature"] = match["signature"]
                plugin.upsert_authoring_record(record)
        if changes and not dry_run:
            plugin.save(target, backend=backend)
        output(
            {
                "plugin": plugin.plugin_name,
                "field": field_label,
                "value": value,
                "matched": len(matches),
                "modified": len(changes),
                "dry_run": dry_run,
                "output": None if dry_run else str(target),
                "changes": changes,
            },
            fmt,
        )


@esp.command(name="remove-formid-subrecord")
@click.argument("plugin_path")
@click.option("--type", "record_type", required=True, help="Record type to scan, e.g. TERM.")
@click.option("--subrecord", "subrecord_signature", required=True, help="Four-character FormID subrecord signature, e.g. SNAM.")
@click.option("--form-id", "target_form_id", required=True, help="Exact eight-digit hexadecimal FormID payload to remove.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def remove_formid_subrecord(ctx, plugin_path, record_type, subrecord_signature, target_form_id, dry_run, output_path, strings_dir, language, backend):
    """Remove an exact FormID SUBRECORD payload from TYPE records."""
    from creation_lib.esp import native_runtime as _native_runtime
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    signature = record_type_signature(record_type)
    if not signature:
        raise click.ClickException(f"Unknown record type: {record_type}")
    subrecord_signature = subrecord_signature.strip().upper()
    if len(subrecord_signature) != 4:
        raise click.ClickException("--subrecord must be a four-character signature")
    form_id_text = target_form_id.strip()
    if form_id_text.lower().startswith("0x"):
        form_id_text = form_id_text[2:]
    try:
        target_form_id = int(form_id_text, 16)
    except ValueError as exc:
        raise click.ClickException("--form-id must be an eight-digit hexadecimal FormID") from exc
    if len(form_id_text) != 8 or not 0 <= target_form_id <= 0xFFFFFFFF:
        raise click.ClickException("--form-id must be an eight-digit hexadecimal FormID")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("remove-formid-subrecord requires the native ESP backend.")
        changes = _native_runtime.plugin_handle_remove_formid_subrecords(
            plugin._rust_handle,
            signature,
            subrecord_signature,
            target_form_id,
            dry_run=dry_run,
        )
        if changes and not dry_run:
            plugin.save(target, backend=backend)
        records = [
            {
                "form_id": f"{change['form_id'] & 0x00FFFFFF:06X}",
                "editor_id": change["editor_id"],
                "removed": change["removed"],
            }
            for change in changes
        ]
        output(
            {
                "plugin": plugin.plugin_name,
                "type": signature,
                "subrecord": subrecord_signature,
                "target_form_id": f"{target_form_id:08X}",
                "modified": len(changes),
                "removed": sum(change["removed"] for change in changes),
                "dry_run": dry_run,
                "output": None if dry_run else str(target),
                "records": records[:100],
                "records_truncated": max(0, len(changes) - 100),
            },
            fmt,
        )


@esp.command(name="repair-term-marker-parameters")
@click.argument("plugin_path")
@click.option("--source", "source_path", required=True, help="FO76 source ESM supplying TERM ZNAM marker rows.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory for the target plugin.")
@click.option("--language", default=None, help="Preferred localized strings language for the target plugin.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def repair_term_marker_parameters(ctx, plugin_path, source_path, dry_run, output_path, strings_dir, language, backend):
    """Restore FO4 TERM marker SNAM rows from FO76 source ZNAM rows."""
    from creation_lib.esp import native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    source_file = Path(source_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    if not source_file.is_file():
        raise click.ClickException(f"Source plugin not found: {source_file}")
    if plugin_file.resolve() == source_file.resolve():
        raise click.ClickException("PLUGIN_PATH and --source must be different files")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        source_file,
        game="fo76",
        strings_dir=None,
        language=None,
        backend=backend,
    ) as source_plugin:
        source_handle = _require_native(source_plugin, "repair-term-marker-parameters")
        with _load_plugin(
            plugin_file,
            game=ctx.obj.get("game"),
            strings_dir=strings_dir,
            language=language,
            backend=backend,
        ) as plugin:
            target_handle = _require_native(plugin, "repair-term-marker-parameters")
            changes = native_runtime.plugin_handle_repair_term_marker_parameters_from_source(
                target_handle,
                source_handle,
                dry_run=dry_run,
            )
            if changes and not dry_run:
                if output_path:
                    target.parent.mkdir(parents=True, exist_ok=True)
                plugin.save(target, backend=backend)
            records = [
                {
                    "form_id": f"{change['form_id'] & 0x00FFFFFF:06X}",
                    "editor_id": change["editor_id"],
                    "removed": change["removed"],
                    "inserted": change["inserted"],
                }
                for change in changes
            ]
            output(
                {
                    "plugin": plugin.plugin_name,
                    "source": str(source_file),
                    "modified": len(changes),
                    "removed": sum(change["removed"] for change in changes),
                    "inserted": sum(change["inserted"] for change in changes),
                    "dry_run": dry_run,
                    "output": None if dry_run else str(target),
                    "records": records[:100],
                    "records_truncated": max(0, len(records) - 100),
                },
                fmt,
            )


@esp.command(name="delete-matching")
@click.argument("plugin_path")
@click.option("--match", "match_pattern", required=True, help="EditorID pattern selecting records to delete (see `esp search`).")
@_search_flags
@click.option("--cascade", is_flag=True, default=False, help="Also strip references to each deleted record from other records.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be deleted without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def delete_matching(ctx, plugin_path, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, cascade, dry_run, output_path, strings_dir, language, backend):
    """Delete every record matching --match (see `esp search` for the pattern syntax).

    With --cascade, strip references to each deleted record first (leveled lists, form
    lists, container contents, scalar slots). Use --dry-run to preview the match set.
    """
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("delete-matching requires the native ESP backend.")
        matches = _run_search(
            plugin,
            match_pattern,
            mode=_resolve_search_mode(mode_substring, mode_regex),
            match_full=match_full,
            read_full=False,
            record_type=record_type,
            case_sensitive=case_sensitive,
            limit=None,
        )
        deleted = [
            {
                "form_id": f"{match['form_id'] & 0x00FFFFFF:06X}",
                "editor_id": match["editor_id"],
            }
            for match in matches
        ]
        delete_report = None
        if deleted and not dry_run:
            from creation_lib.esp import native_runtime

            delete_report = native_runtime.plugin_handle_delete_records(
                plugin._rust_handle,
                [match["form_id"] for match in matches],
                cascade=cascade,
            )
            plugin.save(target, backend=backend)
        result = {
            "plugin": plugin.plugin_name,
            "matched": len(matches),
            "deleted": len(deleted) if dry_run else (delete_report or {}).get("removed", 0),
            "dry_run": dry_run,
            "output": None if dry_run else str(target),
            "records": deleted,
        }
        if cascade:
            result["cascade"] = {
                "records_modified": (delete_report or {}).get("records_modified", 0),
                "refs_removed": (delete_report or {}).get("refs_removed", 0),
            }
        output(result, fmt)


@esp.command(name="delete-placed-by-base")
@click.argument("plugin_path")
@click.option("--base-type", "base_types", multiple=True, help="Base record signature to delete placements for, e.g. ACTI. Repeat for multiple; use ALL for every placed record.")
@click.option("--race", "race_specs", multiple=True, help="Only delete placed actor refs whose base NPC_ RACE matches: a form key (Fallout4.esm:013746), an alias (HumanRace/GhoulRace), or a RACE EditorID in this plugin. Repeatable. Implies --base-type NPC_ and scopes the scan to ACHR.")
@click.option("--worldspace", default=None, help="Restrict the census/deletion to one exterior WRLD EditorID, including its persistent cell.")
@click.option("--details", is_flag=True, default=False, help="Include matched base records, model paths, placement counts, and sample placed FormIDs.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be deleted without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def delete_placed_by_base(ctx, plugin_path, base_types, race_specs, worldspace, details, dry_run, output_path, strings_dir, language, backend):
    """Delete placed refs whose NAME base resolves to the requested base type.

    With --race, restrict to placed actors (ACHR) whose base NPC_ uses a matching
    RACE — e.g. `--race HumanRace` removes every placed human NPC. The dry-run
    report always breaks NPC_ placements down by race, so it doubles as a census
    of which actor races populate the plugin.
    """
    from creation_lib.esp import native_runtime
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    requested = []
    delete_all = False
    for base_type in base_types:
        text = str(base_type).strip().upper()
        if text in {"ALL", "*"}:
            delete_all = True
            continue
        sig = record_type_signature(text).upper()
        if len(sig) != 4:
            raise click.ClickException(f"Base record type must be a 4-character signature or ALL: {base_type}")
        requested.append(sig)
    requested_set = set(requested)
    race_filter_specs = [str(s).strip() for s in race_specs if str(s).strip()]
    if race_filter_specs:
        if delete_all:
            raise click.ClickException("--race cannot be combined with --base-type ALL; race filtering only applies to NPC_ actors.")
        requested_set.add("NPC_")
    if not delete_all and not requested_set:
        raise click.ClickException("Pass at least one --base-type, or --base-type ALL.")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("delete-placed-by-base requires the native ESP backend.")
        own_plugin = plugin.plugin_name.lower()
        race_filter = _resolve_race_specs(plugin, handle, race_filter_specs) if race_filter_specs else {}
        if worldspace:
            roots = native_runtime.plugin_handle_collect_cell_slice_roots(
                handle,
                worldspace_editor_id=worldspace,
                min_x=-(2**31),
                min_y=-(2**31),
                max_x=2**31 - 1,
                max_y=2**31 - 1,
                include_worldspace_persistent_cell=True,
            )
            if not roots.get("worldspace_form_keys"):
                warnings = "; ".join(str(value) for value in roots.get("warnings", []))
                raise click.ClickException(warnings or f"Worldspace not found: {worldspace}")
            placed_ids = [
                object_id
                for form_key in roots.get("placed_form_keys", [])
                if (object_id := _form_key_object_id(form_key)) is not None
            ]
        else:
            # Race filtering only concerns placed actors, so scan ACHR alone — far
            # cheaper than resolving NAME for every REFR on a 1 GB master.
            scan_signatures = ["ACHR"] if race_filter_specs else list(_PLACED_RECORD_SIGNATURES)
            placed_ids = native_runtime.plugin_handle_record_form_ids(handle, scan_signatures)
        xloc_form_ids = set(
            native_runtime.plugin_handle_record_form_ids_with_subrecords(handle, ["XLOC"])
        )
        matched: list[int] = []
        matched_base_keys: dict[int, str] = {}
        matched_race_keys: dict[int, str] = {}
        matched_base_records: dict[str, dict[str, object]] = {}
        by_base: dict[str, dict[str, int]] = {
            sig: {"matched": 0, "with_lock_data": 0, "deleted": 0}
            for sig in sorted(requested_set)
        }
        by_race: dict[str, dict[str, int]] = {}
        matched_lock_levels: dict[int | None, int] = {}
        matched_locked_bases: dict[str, dict[str, object]] = {}
        placed_signature_counts = {sig: 0 for sig in _PLACED_RECORD_SIGNATURES}
        base_signature_cache: dict[str, str | None] = {}
        base_race_cache: dict[str, str | None] = {}
        unresolved_bases = 0
        external_bases = 0
        missing_name = 0
        for form_id in placed_ids:
            summary = native_runtime.plugin_handle_record_summary(handle, form_id)
            if race_filter_specs and (summary is None or summary.signature != "ACHR"):
                continue
            if summary is not None and summary.signature in placed_signature_counts:
                placed_signature_counts[summary.signature] += 1
            base_signature = None
            base_form_key = None
            if delete_all:
                base_key = "ALL"
                counts = by_base.setdefault(
                    base_key, {"matched": 0, "with_lock_data": 0, "deleted": 0}
                )
                counts["matched"] += 1
                if form_id in xloc_form_ids:
                    counts["with_lock_data"] += 1
                matched.append(form_id)
                matched_base_keys[form_id] = base_key
                continue
            form_key = f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}"
            name_refs = native_runtime.plugin_handle_get_referenced_form_keys_by_subrecord(
                handle,
                form_key,
                "NAME",
            )
            if not name_refs:
                missing_name += 1
            else:
                base_key = name_refs[0]
                base_form_key = base_key
                base_plugin = base_key.split(":", 1)[0]
                base_object_id = _form_key_object_id(base_key)
                if base_object_id is None:
                    unresolved_bases += 1
                elif base_plugin is not None and str(base_plugin).lower() != own_plugin:
                    external_bases += 1
                else:
                    if base_key not in base_signature_cache:
                        base_summary = native_runtime.plugin_handle_record_summary(handle, base_object_id)
                        base_signature_cache[base_key] = None if base_summary is None else base_summary.signature
                    base_signature = base_signature_cache[base_key]
                    if base_signature is None:
                        unresolved_bases += 1
            base_key = base_signature or "UNRESOLVED"
            if not delete_all and base_signature not in requested_set:
                continue
            race_key = None
            if base_signature == "NPC_" and base_form_key is not None:
                race_key = _npc_base_race_form_key(handle, base_form_key, base_race_cache) or "<no RNAM>"
            if race_filter:
                if race_key is None or race_key == "<no RNAM>" or race_key.lower() not in race_filter:
                    continue
            counts = by_base.setdefault(
                base_key, {"matched": 0, "with_lock_data": 0, "deleted": 0}
            )
            counts["matched"] += 1
            if form_id in xloc_form_ids:
                counts["with_lock_data"] += 1
                level = _xloc_level(
                    native_runtime.plugin_handle_record_subrecords(handle, form_id)
                )
                matched_lock_levels[level] = matched_lock_levels.get(level, 0) + 1
                if base_form_key is not None:
                    base_lock_counts = matched_locked_bases.setdefault(
                        base_form_key, {"count": 0, "levels": {}, "form_ids": []}
                    )
                    base_lock_counts["count"] += 1
                    base_lock_counts["form_ids"].append(form_id & 0x00FFFFFF)
                    levels = base_lock_counts["levels"]
                    levels[level] = levels.get(level, 0) + 1
            matched.append(form_id)
            matched_base_keys[form_id] = base_key
            if details and base_form_key is not None:
                base_counts = matched_base_records.setdefault(
                    base_form_key,
                    {"placement_count": 0, "sample_placed_form_ids": []},
                )
                base_counts["placement_count"] += 1
                sample_form_ids = base_counts["sample_placed_form_ids"]
                if len(sample_form_ids) < 10:
                    sample_form_ids.append(f"{form_id & 0x00FFFFFF:06X}")
            if race_key is not None:
                by_race.setdefault(race_key, {"matched": 0, "deleted": 0})["matched"] += 1
                matched_race_keys[form_id] = race_key
        deleted = 0
        if matched and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            deleted = native_runtime.plugin_handle_remove_records(handle, matched)
            for form_id in matched:
                by_base[matched_base_keys[form_id]]["deleted"] += 1
                race_key = matched_race_keys.get(form_id)
                if race_key is not None:
                    by_race[race_key]["deleted"] += 1
            if deleted:
                plugin.save(target, backend=backend)
        locked_base_records = []
        for base_form_key, counts in matched_locked_bases.items():
            object_id = _form_key_object_id(base_form_key)
            summary = (
                None
                if object_id is None
                else native_runtime.plugin_handle_record_summary(handle, object_id)
            )
            locked_base_records.append(
                {
                    "form_key": base_form_key,
                    "editor_id": None if summary is None else summary.editor_id,
                    "count": counts["count"],
                    "form_ids": [f"{form_id:06X}" for form_id in counts["form_ids"]],
                    "levels": [
                        {"level": level, "count": count}
                        for level, count in sorted(
                            counts["levels"].items(),
                            key=lambda item: (-1 if item[0] is None else item[0]),
                        )
                    ],
                }
            )
        locked_base_records.sort(key=lambda row: (-row["count"], row["form_key"]))
        detailed_base_records = []
        for base_form_key, counts in matched_base_records.items():
            object_id = _form_key_object_id(base_form_key)
            summary = (
                None
                if object_id is None
                else native_runtime.plugin_handle_record_summary(handle, object_id)
            )
            detailed_base_records.append(
                {
                    "form_key": base_form_key,
                    "editor_id": None if summary is None else summary.editor_id,
                    "signature": None if summary is None else summary.signature,
                    "model_paths": (
                        []
                        if object_id is None
                        else _record_model_paths(
                            native_runtime.plugin_handle_record_subrecords(handle, object_id)
                        )
                    ),
                    **counts,
                }
            )
        detailed_base_records.sort(
            key=lambda row: (-row["placement_count"], row["form_key"])
        )
        result = {
            "plugin": plugin.plugin_name,
            "worldspace": worldspace,
            "base_types": ["ALL"] if delete_all else sorted(requested_set),
            "placed_scanned": len(placed_ids),
            "placed_with_lock_data": len(xloc_form_ids),
            "matched": len(matched),
            "matched_with_lock_data": sum(
                counts["with_lock_data"] for counts in by_base.values()
            ),
            "matched_lock_levels": [
                {"level": level, "count": count}
                for level, count in sorted(
                    matched_lock_levels.items(),
                    key=lambda item: (-1 if item[0] is None else item[0]),
                )
            ],
            "locked_base_records": locked_base_records,
            "matched_base_records": detailed_base_records,
            "deleted": deleted,
            "dry_run": dry_run,
            "output": str(target),
            "race_filter": [
                {"form_key": fk, "display": disp} for fk, disp in race_filter.items()
            ],
            "by_base_signature": [
                {"signature": sig, **counts}
                for sig, counts in sorted(by_base.items())
                if counts["matched"] or sig in requested_set
            ],
            "by_race": [
                {
                    "race": race_key,
                    "editor_id": (
                        None
                        if race_key == "<no RNAM>"
                        else _race_display_name(handle, plugin, own_plugin, race_key)
                    ),
                    **counts,
                }
                for race_key, counts in sorted(
                    by_race.items(), key=lambda kv: (-kv[1]["matched"], kv[0])
                )
            ],
            "placed_signature_counts": [
                {"signature": sig, "count": count}
                for sig, count in placed_signature_counts.items()
                if count
            ],
            "external_bases": external_bases,
            "unresolved_bases": unresolved_bases,
            "missing_name": missing_name,
        }
        output(result, fmt)


def _resolve_cell_object_id(handle, plugin, cell_id: str) -> int:
    from creation_lib.esp import native_runtime

    text = str(cell_id).strip()
    try:
        return int(text, 16) & 0x00FFFFFF
    except ValueError:
        pass
    for _form_key, editor_id, _signature, object_id, _raw_form_id in (
        native_runtime.plugin_handle_record_index_rows(handle, signatures=["CELL"])
    ):
        if editor_id and editor_id.lower() == text.lower():
            return object_id & 0x00FFFFFF
    raise click.ClickException(f"Could not resolve CELL '{cell_id}' (pass a local hex FormID like 6240BB or a CELL EditorID).")


def _resolve_cell_raw_form_id(handle, cell_id: str) -> int:
    from creation_lib.esp import native_runtime

    text = str(cell_id).strip()
    rows = native_runtime.plugin_handle_record_index_rows(handle, signatures=["CELL"])
    try:
        parsed = int(text, 16) & 0xFFFFFFFF
    except ValueError:
        matches = [row for row in rows if row[1] and row[1].lower() == text.lower()]
    else:
        if len(text.removeprefix("0x")) > 6:
            return parsed
        matches = [row for row in rows if row[3] == parsed]
    if len(matches) == 1:
        return matches[0][4]
    if len(matches) > 1:
        choices = ", ".join(f"{row[4]:08X}" for row in matches[:8])
        raise click.ClickException(f"CELL '{cell_id}' is ambiguous; pass one of these raw FormIDs: {choices}")
    raise click.ClickException(
        f"Could not resolve CELL '{cell_id}' (pass a local/raw hex FormID or a CELL EditorID)."
    )


def _resolve_raw_edit_record_form_id(plugin, handle, record_id: str, signatures: list[str] | None) -> int:
    from creation_lib.esp import native_runtime

    wanted = [sig.upper() for sig in signatures] if signatures else None
    form_ids = native_runtime.plugin_handle_record_form_ids(handle, wanted)
    text = str(record_id).strip()
    parsed = _resolve_record_id(plugin, text)
    if parsed is None:
        raise click.ClickException(f"Could not resolve record '{record_id}' (pass an EditorID or hex FormID).")
    parsed &= 0xFFFFFFFF
    matches = [fid for fid in form_ids if fid == parsed]
    if not matches:
        object_id = parsed & 0x00FFFFFF
        matches = [fid for fid in form_ids if (fid & 0x00FFFFFF) == object_id]
    if not matches:
        type_hint = f" of type {', '.join(wanted)}" if wanted else ""
        raise click.ClickException(f"Record not found{type_hint}: {record_id}")
    if len(matches) > 1:
        choices = ", ".join(f"{fid:08X}" for fid in matches[:8])
        raise click.ClickException(f"Record '{record_id}' is ambiguous: {choices}")
    return matches[0]


@esp.command(name="strip-record-subrecords")
@click.argument("plugin_path")
@click.option("--record", "record_ids", multiple=True, required=True, help="Record EditorID or local/full hex FormID to edit. Repeatable.")
@click.option("--subrecord", "subrecord_sigs", multiple=True, required=True, help="Raw 4CC subrecord signature to remove, e.g. XRGD. Repeatable.")
@click.option("--type", "record_type", default=None, help="Optional record signature/type filter, e.g. ACHR.")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of removed subrecords here.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be stripped without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def strip_record_subrecords(ctx, plugin_path, record_ids, subrecord_sigs, record_type, manifest_path, dry_run, output_path, strings_dir, language, backend):
    """Remove raw subrecords from specific records.

    This is intended for narrow crash bisections where authoring-field round trips
    would be too broad. It preserves all non-matching raw subrecords in order.
    """
    from creation_lib.esp import native_runtime
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    remove_sigs = {str(sig).strip().upper() for sig in subrecord_sigs if str(sig).strip()}
    bad_sigs = sorted(sig for sig in remove_sigs if len(sig) != 4)
    if bad_sigs:
        raise click.ClickException(f"Subrecord signatures must be raw 4CC values: {', '.join(bad_sigs)}")
    record_sigs = [record_type_signature(str(record_type).strip()).upper()] if record_type else None
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "strip-record-subrecords")
        rows: list[dict] = []
        total_removed = 0
        for record_id in record_ids:
            form_id = _resolve_raw_edit_record_form_id(plugin, handle, record_id, record_sigs)
            subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
            if subrecords is None:
                raise click.ClickException(f"Could not read subrecords for record '{record_id}'")
            kept = []
            removed = []
            for sig, data, semantic_type in subrecords:
                if sig.upper() in remove_sigs:
                    removed.append({
                        "signature": sig,
                        "bytes": len(data),
                        "semantic_type": semantic_type,
                    })
                else:
                    kept.append((sig, data, semantic_type))
            if removed and not dry_run:
                native_runtime.plugin_handle_set_record_subrecords(handle, form_id, kept)
            summary = native_runtime.plugin_handle_record_summary(handle, form_id)
            rows.append({
                "input": str(record_id),
                "form_id": f"{form_id:08X}",
                "form_key": f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}",
                "signature": None if summary is None else summary.signature,
                "editor_id": None if summary is None else summary.editor_id,
                "removed": removed,
                "removed_count": len(removed),
            })
            total_removed += len(removed)
        if total_removed and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        manifest = {
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "dry_run": dry_run,
            "record_type": record_sigs[0] if record_sigs else None,
            "subrecords": sorted(remove_sigs),
            "records": rows,
            "records_changed": sum(1 for row in rows if row["removed_count"]),
            "subrecords_removed": total_removed,
        }
        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        output(manifest, fmt)


_RACE_SUBGRAPH_SIGNATURES = {"STKD", "SAKD", "SGNM", "SAPT", "SRAF"}


def _retain_race_subgraph_blocks(subrecords, selector_form_id: int):
    anchor_indexes = [
        index for index, (sig, _, _) in enumerate(subrecords) if sig.upper() == "SADD"
    ]
    if len(anchor_indexes) != 1:
        raise ValueError(f"expected one RACE SADD anchor, found {len(anchor_indexes)}")
    anchor = anchor_indexes[0]
    sraf_indexes = [
        index
        for index, (sig, _, _) in enumerate(subrecords[anchor + 1 :], start=anchor + 1)
        if sig.upper() == "SRAF"
    ]
    if not sraf_indexes:
        raise ValueError("RACE has no subgraph blocks after SADD")
    last_sraf = sraf_indexes[-1]

    kept_blocks = []
    block = []
    total_blocks = 0
    for row in subrecords[anchor + 1 : last_sraf + 1]:
        sig, data, _ = row
        if sig.upper() not in _RACE_SUBGRAPH_SIGNATURES:
            raise ValueError(f"unexpected {sig} inside RACE subgraph block range")
        block.append(row)
        if sig.upper() != "SRAF":
            continue
        total_blocks += 1
        if any(
            item_sig.upper() == "STKD"
            and len(item_data) == 4
            and int.from_bytes(item_data, "little") == selector_form_id
            for item_sig, item_data, _ in block
        ):
            kept_blocks.extend(block)
        block = []

    kept_count = sum(1 for sig, _, _ in kept_blocks if sig.upper() == "SRAF")
    if kept_count == 0:
        raise ValueError(f"no subgraph block contains STKD 0x{selector_form_id:08X}")
    return (
        subrecords[: anchor + 1] + kept_blocks + subrecords[last_sraf + 1 :],
        total_blocks,
        kept_count,
    )


def _replace_editor_id_subrecord(subrecords, editor_id: str):
    replacement = editor_id.encode("utf-8") + b"\0"
    replaced = False
    rows = []
    for sig, data, semantic_type in subrecords:
        if sig.upper() == "EDID" and not replaced:
            rows.append((sig, replacement, semantic_type))
            replaced = True
        else:
            rows.append((sig, data, semantic_type))
    if not replaced:
        raise ValueError("record has no EDID subrecord")
    return rows


@esp.command(name="retain-race-subgraphs")
@click.argument("plugin_path")
@click.option("--record", "record_id", required=True, help="RACE EditorID or local/full hex FormID to edit.")
@click.option("--selector-object-id", required=True, help="Self-owned STKD selector object ID, e.g. 568776.")
@click.option("--editor-id", default=None, help="Optional replacement EditorID for the filtered RACE.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def retain_race_subgraphs(ctx, plugin_path, record_id, selector_object_id, editor_id, dry_run, output_path, strings_dir, language, backend):
    """Keep only RACE subgraph blocks containing a self-owned STKD selector."""
    from creation_lib.esp import native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    try:
        selector_object_id_value = int(str(selector_object_id).strip().removeprefix("0x"), 16)
    except ValueError as exc:
        raise click.ClickException(f"Invalid selector object ID: {selector_object_id}") from exc
    if not 0 < selector_object_id_value <= 0x00FFFFFF:
        raise click.ClickException("Selector object ID must be between 000001 and FFFFFF")

    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "retain-race-subgraphs")
        form_id = _resolve_raw_edit_record_form_id(plugin, handle, record_id, ["RACE"])
        subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
        if subrecords is None:
            raise click.ClickException(f"Could not read subrecords for RACE '{record_id}'")
        self_mod_index = len(plugin.masters)
        if self_mod_index > 0xFF:
            raise click.ClickException("Plugin has too many masters to encode a self-owned selector")
        selector_form_id = (self_mod_index << 24) | selector_object_id_value
        try:
            filtered, total_blocks, kept_blocks = _retain_race_subgraph_blocks(
                subrecords, selector_form_id
            )
            if editor_id:
                filtered = _replace_editor_id_subrecord(filtered, editor_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        if not dry_run:
            native_runtime.plugin_handle_set_record_subrecords(handle, form_id, filtered)
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        output({
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "dry_run": dry_run,
            "form_id": f"{form_id:08X}",
            "editor_id": editor_id,
            "selector_form_id": f"{selector_form_id:08X}",
            "blocks_before": total_blocks,
            "blocks_after": kept_blocks,
            "blocks_removed": total_blocks - kept_blocks,
        }, fmt)


_QUEST_START_GAME_ENABLED_FLAG = 0x0001


@esp.command(name="audit-fnv-quests")
@click.argument(
    "plugin_paths",
    nargs=-1,
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write the deterministic quest-foundation manifest to this JSON file.",
)
@click.option(
    "--max-closure-records",
    type=click.IntRange(min=1),
    default=100_000,
    show_default=True,
    help="Fail-closed traversal cap for the shared quest dependency closure.",
)
@click.pass_context
def audit_fnv_quests(ctx, plugin_paths, report_path, max_closure_records):
    """Inventory every FNV quest and its startup/dependency evidence."""
    from contextlib import ExitStack

    from bacup_lib.fnv_quest_foundation import audit_fnv_quest_plugins

    with ExitStack() as stack:
        plugins = [
            stack.enter_context(
                _load_plugin(
                    plugin_path,
                    game="fnv",
                    strings_dir=None,
                    language=None,
                    backend="native",
                )
            )
            for plugin_path in plugin_paths
        ]
        report = audit_fnv_quest_plugins(
            plugins, max_closure_records=max_closure_records
        )
    payload = report.to_dict()
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report.to_json() + "\n", encoding="utf-8")
    output(payload, ctx.obj.get("fmt", "json"))


def _clear_quest_start_game_enabled(subrecords):
    kept = []
    changes = []
    dnam_count = 0
    short_dnam_count = 0
    for sig, data, semantic_type in subrecords:
        if sig.upper() != "DNAM":
            kept.append((sig, data, semantic_type))
            continue
        dnam_count += 1
        blob = bytes(data)
        if len(blob) < 2:
            short_dnam_count += 1
            kept.append((sig, data, semantic_type))
            continue
        old_flags = int.from_bytes(blob[:2], "little")
        if old_flags & _QUEST_START_GAME_ENABLED_FLAG:
            new_flags = old_flags & ~_QUEST_START_GAME_ENABLED_FLAG
            blob = new_flags.to_bytes(2, "little") + blob[2:]
            changes.append({
                "old_flags": f"0x{old_flags:04X}",
                "new_flags": f"0x{new_flags:04X}",
                "bytes": len(data),
            })
        kept.append((sig, blob, semantic_type))
    return kept, changes, dnam_count, short_dnam_count


@esp.command(name="disable-quest-autostart")
@click.argument("plugin_path")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of changed QUST DNAM flags here.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--keep-first", type=click.IntRange(min=0), default=0, show_default=True, help="Keep the first N enabled quests, ordered by FormID, for deterministic bisection.")
@click.option("--keep-editor-id", "keep_editor_ids", multiple=True, help="Keep an enabled quest with this exact EditorID. Repeat for multiple quests.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def disable_quest_autostart(ctx, plugin_path, manifest_path, dry_run, output_path, keep_first, keep_editor_ids, strings_dir, language, backend):
    """Clear the Start Game Enabled bit on every QUST DNAM subrecord."""
    from creation_lib.esp import native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "disable-quest-autostart")
        rows: list[dict] = []
        kept_rows: list[dict] = []
        quest_ids = sorted(native_runtime.plugin_handle_record_form_ids(handle, ["QUST"]))
        records_with_dnam = 0
        records_with_short_dnam = 0
        dnam_subrecords_changed = 0
        enabled_quests_seen = 0
        kept_editor_ids = {editor_id.casefold() for editor_id in keep_editor_ids}
        for form_id in quest_ids:
            subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
            if subrecords is None:
                continue
            kept, changes, dnam_count, short_dnam_count = _clear_quest_start_game_enabled(subrecords)
            if dnam_count:
                records_with_dnam += 1
            if short_dnam_count:
                records_with_short_dnam += 1
            if not changes:
                continue
            summary = native_runtime.plugin_handle_record_summary(handle, form_id)
            row = {
                "form_id": f"{form_id:08X}",
                "form_key": f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}",
                "editor_id": None if summary is None else summary.editor_id,
                "dnam": changes,
            }
            enabled_quests_seen += 1
            if enabled_quests_seen <= keep_first or (
                row["editor_id"] is not None
                and row["editor_id"].casefold() in kept_editor_ids
            ):
                kept_rows.append(row)
                continue
            dnam_subrecords_changed += len(changes)
            if not dry_run:
                native_runtime.plugin_handle_set_record_subrecords(handle, form_id, kept)
            rows.append(row)
        if rows and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        manifest = {
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "dry_run": dry_run,
            "quests_scanned": len(quest_ids),
            "records_with_dnam": records_with_dnam,
            "records_missing_dnam": len(quest_ids) - records_with_dnam,
            "records_with_short_dnam": records_with_short_dnam,
            "records_kept_enabled": len(kept_rows),
            "records_changed": len(rows),
            "dnam_subrecords_changed": dnam_subrecords_changed,
            "kept_records": kept_rows,
            "records": rows,
        }
        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        output(manifest, fmt)


_DISTANT_LOD_FLAG = 0x8000

# Record-header bit 0x8000 means "Has Distant LOD" only on these LOD-capable
# base signatures (exactly what synthesize_object_lod touches). On other record
# types (REFR, CELL, NPC_, ...) bit 15 is a different flag, so the default scan
# is restricted to these — never the whole plugin.
_DISTANT_LOD_SIGNATURES = ["STAT", "SCOL", "MSTT", "TREE", "FLOR", "ACTI"]


def _strip_distant_lod_from_record(subrecords, flags):
    """Drop every MNAM subrecord and clear the 0x8000 (Has Distant LOD) header bit.

    Returns (kept_subrecords, mnam_removed, new_flags, flag_cleared).
    """
    kept = []
    mnam_removed = 0
    for sig, data, semantic_type in subrecords:
        if sig.upper() == "MNAM":
            mnam_removed += 1
        else:
            kept.append((sig, data, semantic_type))
    flag_cleared = bool(flags & _DISTANT_LOD_FLAG)
    new_flags = flags & ~_DISTANT_LOD_FLAG
    return kept, mnam_removed, new_flags, flag_cleared


@esp.command(name="strip-distant-lod")
@click.argument("plugin_path")
@click.option("--type", "record_type", default=None, help="Restrict to a single record signature, e.g. STAT. Default: STAT, SCOL, MSTT, TREE, FLOR, ACTI.")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of changed records here.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def strip_distant_lod(ctx, plugin_path, record_type, manifest_path, dry_run, output_path, strings_dir, language, backend):
    """Strip FO4 object-LOD "Distant LOD" data from a plugin.

    Removes every MNAM ("Distant LOD") subrecord and clears the 0x8000
    ("Has Distant LOD") record-header flag. Intended to revert a
    synthesize_object_lod pass without re-running the conversion.

    By default only the LOD-capable base signatures (STAT, SCOL, MSTT, TREE,
    FLOR, ACTI) are scanned in a single pass - bit 0x8000 means something else
    on other record types, so they are never touched. Pass --type SIG to
    restrict to one signature.
    """
    from creation_lib.esp import native_runtime
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    wanted = (
        [record_type_signature(str(record_type).strip()).upper()]
        if record_type
        else list(_DISTANT_LOD_SIGNATURES)
    )
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "strip-distant-lod")
        rows: list[dict] = []
        records_scanned = 0
        mnam_removed = 0
        flags_cleared = 0
        for form_id in native_runtime.plugin_handle_record_form_ids(handle, wanted):
            records_scanned += 1
            subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
            if subrecords is None:
                continue
            flags = native_runtime.plugin_handle_record_flags(handle, form_id)
            if flags is None:
                flags = 0
            kept, rec_mnam_removed, new_flags, flag_cleared = _strip_distant_lod_from_record(subrecords, flags)
            if not rec_mnam_removed and not flag_cleared:
                continue
            if not dry_run:
                if rec_mnam_removed:
                    native_runtime.plugin_handle_set_record_subrecords(handle, form_id, kept)
                if flag_cleared:
                    native_runtime.plugin_handle_set_record_flags(handle, form_id, new_flags)
            summary = native_runtime.plugin_handle_record_summary(handle, form_id)
            rows.append({
                "form_id": f"{form_id:08X}",
                "form_key": f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}",
                "signature": None if summary is None else summary.signature,
                "editor_id": None if summary is None else summary.editor_id,
                "mnam_removed": rec_mnam_removed,
                "flag_cleared": flag_cleared,
                "old_flags": f"0x{flags:08X}",
                "new_flags": f"0x{new_flags:08X}",
            })
            mnam_removed += rec_mnam_removed
            if flag_cleared:
                flags_cleared += 1
        if rows and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        manifest = {
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "dry_run": dry_run,
            "record_type": wanted[0] if record_type else None,
            "signatures_scanned": wanted,
            "records_scanned": records_scanned,
            "records_changed": len(rows),
            "mnam_removed": mnam_removed,
            "flags_cleared": flags_cleared,
            "records": rows,
        }
        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        output(manifest, fmt)


_ACHR_POSE_SUBRECORDS = {
    "XRGD": "Bones",
    "XRGB": "BipedRotation",
}


def _xrgd_is_empty_bone_payload(data) -> bool:
    row_len = 28
    blob = bytes(data)
    if not blob:
        return True
    if len(blob) % row_len:
        return False
    import struct
    for offset in range(0, len(blob), row_len):
        row = blob[offset:offset + row_len]
        if any(row[:4]):
            return False
        for field_offset in (4, 8, 12, 16, 20, 24):
            if struct.unpack_from("<f", row, field_offset)[0] != 0.0:
                return False
    return True


@esp.command(name="strip-empty-refr-xrgd")
@click.argument("plugin_path")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of stripped REFR XRGD subrecords here.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be stripped without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def strip_empty_refr_xrgd(ctx, plugin_path, manifest_path, dry_run, output_path, strings_dir, language, backend):
    """Remove empty FO76 XRGD bone rows from REFR records."""
    from creation_lib.esp import native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "strip-empty-refr-xrgd")
        rows: list[dict] = []
        total_removed = 0
        for form_id in native_runtime.plugin_handle_record_form_ids(handle, ["REFR"]):
            subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
            if subrecords is None:
                continue
            kept = []
            removed = []
            for sig, data, semantic_type in subrecords:
                if sig.upper() == "XRGD" and _xrgd_is_empty_bone_payload(data):
                    removed.append({
                        "signature": sig,
                        "bytes": len(data),
                        "semantic_type": semantic_type,
                    })
                else:
                    kept.append((sig, data, semantic_type))
            if not removed:
                continue
            if not dry_run:
                native_runtime.plugin_handle_set_record_subrecords(handle, form_id, kept)
            rows.append({
                "form_id": f"{form_id:08X}",
                "form_key": f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}",
                "removed": removed,
                "removed_count": len(removed),
            })
            total_removed += len(removed)
        if total_removed and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        manifest = {
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "dry_run": dry_run,
            "records_changed": len(rows),
            "subrecords_removed": total_removed,
            "records": rows,
        }
        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        output(manifest, fmt)


@esp.command(name="strip-cell-achr-pose")
@click.argument("plugin_path")
@click.argument("cell_id")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of stripped ACHR pose subrecords here.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be stripped without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def strip_cell_achr_pose(ctx, plugin_path, cell_id, manifest_path, dry_run, output_path, strings_dir, language, backend):
    """Remove Bones/BipedRotation pose subrecords from ACHR children of one CELL."""
    from creation_lib.esp import native_runtime

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "strip-cell-achr-pose")
        own_plugin = plugin.plugin_name.lower()
        cell_oid = _resolve_cell_object_id(handle, plugin, cell_id)
        children = native_runtime.plugin_handle_collect_cell_children(handle, cell_oid)
        rows: list[dict] = []
        total_removed = 0
        changed_records = 0
        subrecord_counts = {sig: 0 for sig in _ACHR_POSE_SUBRECORDS}
        for child in children:
            if str(child.get("signature", "")).upper() != "ACHR":
                continue
            form_id = int(child.get("form_id", 0)) & 0xFFFFFFFF
            form_key = str(child.get("form_key") or f"{plugin.plugin_name}:{form_id & 0x00FFFFFF:06X}")
            name_refs = native_runtime.plugin_handle_get_referenced_form_keys_by_subrecord(handle, form_key, "NAME")
            base_fk = name_refs[0] if name_refs else None
            base_sig = None
            base_eid = None
            if base_fk is not None and base_fk.split(":", 1)[0].lower() == own_plugin:
                oid = _form_key_object_id(base_fk)
                summary = native_runtime.plugin_handle_record_summary(handle, oid) if oid is not None else None
                if summary is not None:
                    base_sig = summary.signature
                    base_eid = summary.editor_id
            subrecords = native_runtime.plugin_handle_record_subrecords(handle, form_id)
            if subrecords is None:
                continue
            kept = []
            removed = []
            for sig, data, semantic_type in subrecords:
                raw_sig = str(sig).upper()
                if raw_sig in _ACHR_POSE_SUBRECORDS:
                    removed.append({
                        "signature": raw_sig,
                        "field": _ACHR_POSE_SUBRECORDS[raw_sig],
                        "bytes": len(data),
                        "semantic_type": semantic_type,
                    })
                    subrecord_counts[raw_sig] += 1
                else:
                    kept.append((sig, data, semantic_type))
            if removed:
                changed_records += 1
                total_removed += len(removed)
                if not dry_run:
                    native_runtime.plugin_handle_set_record_subrecords(handle, form_id, kept)
            rows.append({
                "form_key": form_key,
                "base_form_key": base_fk,
                "base_signature": base_sig,
                "base_editor_id": base_eid,
                "removed": removed,
                "removed_count": len(removed),
            })
        if total_removed and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            plugin.save(target, backend=backend)
        manifest = {
            "plugin": plugin.plugin_name,
            "input": str(plugin_file),
            "output": str(target),
            "cell": f"{cell_oid:06X}",
            "dry_run": dry_run,
            "total_children": len(children),
            "achr_children": len(rows),
            "records_changed": changed_records,
            "subrecords_removed": total_removed,
            "subrecords": [
                {"signature": sig, "field": field, "removed": subrecord_counts[sig]}
                for sig, field in _ACHR_POSE_SUBRECORDS.items()
            ],
            "records": rows,
        }
        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        output(manifest, fmt)


@esp.command(name="delete-cell-children")
@click.argument("plugin_path")
@click.argument("cell_id")
@click.option("--base-type", "base_types", multiple=True, help="Only delete children whose NAME base resolves to this signature, e.g. STAT. Repeatable; omit to match any base.")
@click.option("--keep-base-type", "keep_base_types", multiple=True, help="Additive/inverse mode: delete every placed child EXCEPT those whose base resolves to one of these signatures. e.g. `--keep-base-type STAT --keep-base-type SCOL` leaves only statics + collections. Mutually exclusive with --base-type.")
@click.option("--keep-external", is_flag=True, default=False, help="In --keep-base-type mode, also keep refs whose external base could not be resolved from available masters.")
@click.option("--child-type", "child_types", multiple=True, help="Only delete children of this record signature, e.g. REFR or ACHR. Repeatable; omit for every placed child.")
@click.option("--race", "race_specs", multiple=True, help="For NPC_ bases, only delete actors whose RACE matches (form key, HumanRace/GhoulRace alias, or in-plugin EditorID).")
@click.option("--keep-race", "keep_race_specs", multiple=True, help="Inverse race mode: delete actors EXCEPT those whose NPC_ RACE matches. Repeatable; mutually exclusive with --race.")
@click.option("--manifest", "manifest_path", default=None, help="Write a JSON manifest of every matched record (form key, base EditorID, race) here — a record of exactly what was removed. Works with --dry-run too.")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would be deleted without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def delete_cell_children(ctx, plugin_path, cell_id, base_types, keep_base_types, keep_external, child_types, race_specs, keep_race_specs, manifest_path, dry_run, output_path, strings_dir, language, backend):
    """Delete the placed children of a single CELL (interior or exterior).

    CELL_ID is a local hex FormID (e.g. 6240BB) or a CELL EditorID. Scoped removal
    for bisecting a cell that won't load: drop a category (e.g. `--base-type STAT`),
    a race (`--race HumanRace`), or everything except a race (`--keep-race HumanRace`)
    from just that cell and retest. The dry-run report breaks the cell down by
    base and child signature, so it doubles as a census of the cell's contents.
    """
    from creation_lib.esp import native_runtime
    from creation_lib.esp.record_types import record_type_signature

    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    base_filter = {record_type_signature(str(b).strip()).upper() for b in base_types if str(b).strip()}
    keep_base = {record_type_signature(str(b).strip()).upper() for b in keep_base_types if str(b).strip()}
    if base_filter and keep_base:
        raise click.ClickException("--base-type and --keep-base-type are mutually exclusive.")
    child_filter = {record_type_signature(str(c).strip()).upper() for c in child_types if str(c).strip()}
    race_filter_specs = [str(s).strip() for s in race_specs if str(s).strip()]
    keep_race_filter_specs = [str(s).strip() for s in keep_race_specs if str(s).strip()]
    if race_filter_specs and keep_race_filter_specs:
        raise click.ClickException("--race and --keep-race are mutually exclusive.")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(plugin_file, game=ctx.obj.get("game"), strings_dir=strings_dir, language=language, backend=backend) as plugin:
        handle = _require_native(plugin, "delete-cell-children")
        own_plugin = plugin.plugin_name.lower()
        game = ctx.obj.get("game")
        master_search_paths = _esp_master_search_paths(game, plugin_file)
        master_handle_cache: dict[str, object | None] = {}

        def master_path(master_name: str) -> Path | None:
            for search_path in master_search_paths:
                for candidate in (search_path / master_name, search_path / "Data" / master_name):
                    if candidate.is_file():
                        return candidate
            return None

        def master_handle(master_name: str):
            key = master_name.lower()
            if key in master_handle_cache:
                return master_handle_cache[key]
            path = master_path(master_name)
            if path is None:
                master_handle_cache[key] = None
                return None
            loaded = native_runtime.plugin_handle_load(
                str(path),
                game=game,
                strings_dir=strings_dir,
                language=language,
            )
            master_handle_cache[key] = loaded
            return loaded

        def base_summary(base_form_key: str):
            oid = _form_key_object_id(base_form_key)
            if oid is None:
                return None
            plugin_name = base_form_key.rsplit(":", 1)[0]
            if plugin_name.lower() == own_plugin:
                return native_runtime.plugin_handle_record_summary(handle, oid)
            source_handle = master_handle(plugin_name)
            if source_handle is None:
                return None
            return native_runtime.plugin_handle_record_summary(source_handle, oid)

        race_filter = _resolve_race_specs(plugin, handle, race_filter_specs) if race_filter_specs else {}
        keep_race_filter = _resolve_race_specs(plugin, handle, keep_race_filter_specs) if keep_race_filter_specs else {}
        cell_oid = _resolve_cell_object_id(handle, plugin, cell_id)
        children = native_runtime.plugin_handle_collect_cell_children(handle, cell_oid)

        base_sig_cache: dict[str, str | None] = {}
        base_eid_cache: dict[str, str | None] = {}
        base_race_cache: dict[str, str | None] = {}
        by_child_sig: dict[str, int] = {}
        by_base_sig: dict[str, dict[str, int]] = {}
        matched: list[dict] = []
        for child in children:
            child_sig = str(child.get("signature", "")).upper()
            by_child_sig[child_sig] = by_child_sig.get(child_sig, 0) + 1
            child_fk = child.get("form_key", "")
            name_refs = native_runtime.plugin_handle_get_referenced_form_keys_by_subrecord(handle, child_fk, "NAME")
            base_fk = name_refs[0] if name_refs else None
            base_sig = None
            base_eid = None
            if base_fk is not None:
                if base_fk not in base_sig_cache:
                    bs = base_summary(base_fk)
                    base_sig_cache[base_fk] = None if bs is None else bs.signature
                    base_eid_cache[base_fk] = None if bs is None else bs.editor_id
                    if bs is None and base_fk.rsplit(":", 1)[0].lower() != own_plugin:
                        base_sig_cache[base_fk] = "<external>"
                base_sig = base_sig_cache[base_fk]
                base_eid = base_eid_cache[base_fk]
            base_key = base_sig or "<no-base>"
            by_base_sig.setdefault(base_key, {"matched": 0, "deleted": 0})
            if child_filter and child_sig not in child_filter:
                continue
            if base_filter and base_sig not in base_filter:
                continue
            if keep_base and base_sig in keep_base:
                continue
            if keep_external and base_sig == "<external>":
                continue
            race_disp = None
            if base_sig == "NPC_" and base_fk is not None:
                race_fk = _npc_base_race_form_key(handle, base_fk, base_race_cache)
                race_key = race_fk.lower() if race_fk else None
                if race_filter and (race_key is None or race_key not in race_filter):
                    continue
                if keep_race_filter and race_key in keep_race_filter:
                    continue
                race_disp = (_race_display_name(handle, plugin, own_plugin, race_fk) or race_fk) if race_fk else None
            elif race_filter:
                continue
            child_form_id = child.get("form_id")
            if child_form_id is None:
                continue
            by_base_sig[base_key]["matched"] += 1
            matched.append({
                "form_id": int(child_form_id),
                "form_key": child_fk,
                "signature": child_sig,
                "base_signature": base_sig,
                "base_form_key": base_fk,
                "base_editor_id": base_eid,
                "race": race_disp,
                "base_key": base_key,
            })

        deleted = 0
        if matched and not dry_run:
            if output_path:
                target.parent.mkdir(parents=True, exist_ok=True)
            deleted = native_runtime.plugin_handle_remove_records(handle, [row["form_id"] for row in matched])
            # Batch remove is all-or-nothing per id; every matched id exists (it
            # came from the live enumeration), so credit each matched base.
            for row in matched:
                row["deleted"] = True
                by_base_sig[row["base_key"]]["deleted"] += 1
            if deleted:
                plugin.save(target, backend=backend)
        else:
            for row in matched:
                row["deleted"] = False

        if manifest_path:
            manifest_file = Path(manifest_path)
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest = {
                "plugin": plugin.plugin_name,
                "cell": f"{cell_oid:06X}",
                "dry_run": dry_run,
                "filters": {
                    "base_type": sorted(base_filter),
                    "keep_base_type": sorted(keep_base),
                    "child_type": sorted(child_filter),
                    "race": [disp for disp in race_filter.values()],
                    "keep_race": [disp for disp in keep_race_filter.values()],
                },
                "count": len(matched),
                "records": [
                    {
                        "form_key": row["form_key"],
                        "signature": row["signature"],
                        "base": row["base_editor_id"] or row["base_form_key"],
                        "base_signature": row["base_signature"],
                        "race": row["race"],
                        "deleted": row.get("deleted", False),
                    }
                    for row in sorted(matched, key=lambda r: (str(r["base_signature"]), str(r["base_editor_id"] or r["base_form_key"]), r["form_key"]))
                ],
            }
            manifest_file.write_text(json.dumps(manifest, indent=2))
        result = {
            "plugin": plugin.plugin_name,
            "cell": f"{cell_oid:06X}",
            "total_children": len(children),
            "matched": len(matched),
            "deleted": deleted,
            "dry_run": dry_run,
            "output": str(target),
            "manifest": str(Path(manifest_path)) if manifest_path else None,
            "filters": {
                "base_type": sorted(base_filter),
                "keep_base_type": sorted(keep_base),
                "child_type": sorted(child_filter),
                "race": [{"form_key": fk, "display": disp} for fk, disp in race_filter.items()],
                "keep_race": [{"form_key": fk, "display": disp} for fk, disp in keep_race_filter.items()],
            },
            "by_child_signature": [
                {"signature": sig, "count": count} for sig, count in sorted(by_child_sig.items(), key=lambda kv: -kv[1])
            ],
            "by_base_signature": [
                {"signature": sig, **counts}
                for sig, counts in sorted(by_base_sig.items(), key=lambda kv: -kv[1]["matched"] if kv[1]["matched"] else 0)
            ],
        }
        output(result, fmt)


@esp.command(name="rename")
@click.argument("plugin_path")
@click.option("--match", "match_pattern", required=True, help="EditorID pattern selecting records to rename (see `esp search`).")
@_search_flags
@click.option("--prefix", "prefix_spec", default=None, help="Rewrite the EditorID prefix: OLD=NEW.")
@click.option("--regex-sub", "regex_sub", default=None, help="Rewrite the EditorID via PATTERN=REPLACEMENT (regex substitution).")
@click.option("--to", "rename_to", default=None, help="Set the EditorID to this exact value (only when a single record matches).")
@click.option("--dry-run", is_flag=True, default=False, help="Report the renames without saving.")
@click.option("--output", "output_path", default=None, help="Write the modified plugin here instead of overwriting PLUGIN_PATH.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def rename(ctx, plugin_path, match_pattern, mode_substring, mode_regex, match_full, record_type, case_sensitive, prefix_spec, regex_sub, rename_to, dry_run, output_path, strings_dir, language, backend):
    """Rename the EditorIDs of records matching --match.

    Pick exactly one rewrite: --prefix OLD=NEW, --regex-sub PATTERN=REPLACEMENT, or --to
    NEWEID (single match only). Aborts if a new EditorID would collide with an existing
    one (or another rename in the batch). Use --dry-run to preview.
    """
    fmt = ctx.obj.get("fmt", "json")
    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    target = Path(output_path) if output_path else plugin_file
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if getattr(plugin, "_rust_handle", None) is None:
            raise click.ClickException("rename requires the native ESP backend.")
        matches = _run_search(
            plugin,
            match_pattern,
            mode=_resolve_search_mode(mode_substring, mode_regex),
            match_full=match_full,
            read_full=False,
            record_type=record_type,
            case_sensitive=case_sensitive,
            limit=None,
        )
        transform = _eid_transformer(prefix_spec, regex_sub, rename_to, len(matches))
        taken = {key.lower() for key in plugin.eid_index()}
        renames = []
        for match in matches:
            old_eid = match["editor_id"]
            if not old_eid:
                continue
            new_eid = transform(old_eid)
            if not new_eid or new_eid == old_eid:
                continue
            taken.discard(old_eid.lower())
            if new_eid.lower() in taken:
                raise click.ClickException(f"rename collision: '{new_eid}' already exists (from '{old_eid}').")
            taken.add(new_eid.lower())
            renames.append((match, old_eid, new_eid))
        for match, _old_eid, new_eid in renames:
            if dry_run:
                continue
            record = plugin.read_authoring_record(match["form_id"])
            if not isinstance(record, dict):
                continue
            record["eid"] = new_eid
            record["signature"] = match["signature"]
            plugin.upsert_authoring_record(record)
        if renames and not dry_run:
            plugin.save(target, backend=backend)
        output(
            {
                "plugin": plugin.plugin_name,
                "matched": len(matches),
                "renamed": len(renames),
                "dry_run": dry_run,
                "output": None if dry_run else str(target),
                "changes": [
                    {"form_id": f"{m['form_id'] & 0x00FFFFFF:06X}", "from": old, "to": new}
                    for m, old, new in renames
                ],
            },
            fmt,
        )


@esp.command()
@click.argument("plugin_path")
@click.option("--mode", type=click.Choice(["lossless", "semantic", "authoring"]), default="lossless", show_default=True)
@click.option("--encoding", "text_format", type=click.Choice(["json", "yaml"]), default=None, help="Output text format. Defaults from --output suffix or json.")
@click.option("--output", default="-", help="Output file path, or - for stdout.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def export(ctx, plugin_path, mode, text_format, output: str, strings_dir, language, backend):
    """Export a plugin to lossless or semantic JSON/YAML."""
    from creation_lib.esp import export_json, export_yaml

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    output_path = None if output == "-" else Path(output)
    resolved_format = _detect_text_format(output_path or plugin_file, text_format)
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        if resolved_format == "yaml":
            text = export_yaml(plugin, mode=mode, backend=backend)
        else:
            text = format_json_text(
                export_json(plugin, mode=mode, backend=backend),
                ctx.obj.get("fmt", "json"),
            )
        if output_path is None:
            click.echo(text)
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        click.echo(f"Exported {plugin.plugin_name} to {output_path}")


@esp.command(name="export-authoring")
@click.argument("plugin_path")
@click.option("--output-dir", required=True, help="Target authoring directory.")
@click.option("--jobs", type=int, default=None, help="Record export worker count. Defaults to CPU count.")
@click.option("--strings-dir", default=None, help="Override localized strings directory.")
@click.option("--language", default=None, help="Preferred localized strings language.")
@click.option("--format", "output_format", type=click.Choice(["json", "yaml"]), default="json", help="Output text format (default: json).")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def export_authoring(ctx, plugin_path, output_dir, jobs, strings_dir, language, output_format, backend):
    """Export a plugin to the native directory-based authoring format."""
    from creation_lib.esp import export_authoring_dir

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")
    output_path = Path(output_dir)
    with _load_plugin(
        plugin_file,
        game=ctx.obj.get("game"),
        strings_dir=strings_dir,
        language=language,
        backend=backend,
    ) as plugin:
        export_authoring_dir(plugin, output_path, jobs=jobs, format=output_format, backend=backend)
        click.echo(f"Exported {plugin.plugin_name} to {output_path}")


@esp.command(name="import")
@click.argument("source_path")
@click.option("--format", "text_format", type=click.Choice(["json", "yaml"]), default=None, help="Input text format. Defaults from source suffix.")
@click.option("--output", "output_path", required=True, help="Target plugin path.")
@click.option("--game", "override_game", default=None, help="Override game stored in the export payload.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
def import_cmd(source_path, text_format, output_path, override_game, backend):
    """Import a JSON/YAML export and save it back to plugin binary."""
    from creation_lib.esp import import_json, import_yaml

    source = Path(source_path)
    if not source.is_file():
        raise click.ClickException(f"Export file not found: {source}")
    resolved_format = _detect_text_format(source, text_format)
    text = source.read_text(encoding="utf-8")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        import_yaml(text, backend=backend) if resolved_format == "yaml" else import_json(text, backend=backend)
    ) as plugin:
        if override_game is not None:
            plugin.game = override_game
        plugin.save(target, backend=backend)
        click.echo(f"Saved plugin to {target}")


@esp.command(name="build-authoring")
@click.argument("source_dir")
@click.option("--output", "output_path", required=True, help="Target plugin path.")
@click.option("--game", "override_game", default=None, help="Override game stored in the authoring metadata.")
def build_authoring(source_dir, output_path, override_game):
    """Build a plugin .esp from a YAML/JSON authoring directory.

    Streams records record-by-record to the output file without materializing
    the full plugin in RAM. Bounded peak memory (~1-3 GB) regardless of plugin
    size — safe on Starfield-scale plugins where the materialized path OOM'd
    at 60+ GB.
    """
    from creation_lib.esp import build_authoring_dir

    source = Path(source_dir)
    if not source.is_dir():
        raise click.ClickException(f"Authoring directory not found: {source}")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    build_authoring_dir(source, target, game=override_game)
    click.echo(f"Built plugin to {target}")


@esp.command()
@click.argument("source_path")
@click.option("--format", "text_format", type=click.Choice(["json", "yaml"]), default=None, help="Input text format. Defaults from source suffix.")
@click.option("--output", "output_path", required=True, help="Target plugin path.")
@click.option("--game", "override_game", default=None, help="Override game stored in the export payload.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
def build(source_path, text_format, output_path, override_game, backend):
    """Build plugin bytes from native JSON/YAML authoring data."""
    source = Path(source_path)
    if not source.is_file():
        raise click.ClickException(f"Export file not found: {source}")
    resolved_format = _detect_text_format(source, text_format)
    text = source.read_text(encoding="utf-8")
    from creation_lib.esp import import_json, import_yaml

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (import_yaml(text, backend=backend) if resolved_format == "yaml" else import_json(text, backend=backend)) as plugin:
        if override_game is not None:
            plugin.game = override_game
        plugin.save(target, backend=backend)
        click.echo(f"Built plugin to {target}")


@esp.command(name="new")
@click.argument("output_path")
@click.option("--esm/--no-esm", "set_master", default=None, help="Force the Master (ESM) bit on/off. Auto-on for a .esm output.")
@click.option("--light/--no-light", "set_light", default=None, help="Force the Light (ESL) bit on/off. Auto-on for a .esl output.")
@click.option("--esl", "esl_alias", is_flag=True, default=False, help="Alias for --light: set the Light/ESL bit.")
@click.option("--medium", is_flag=True, default=False, help="Set the Medium-plugin bit (Starfield).")
@click.option("--update", is_flag=True, default=False, help="Set the Update-plugin bit (Starfield).")
@click.option("--localized", is_flag=True, default=False, help="Set the Localized bit.")
@click.option("--master", "extra_masters", multiple=True, help="Add a master file (repeatable).")
@click.option("--no-masters", is_flag=True, default=False, help="Start with zero masters (skip the game's base ESM).")
@click.option("--force", is_flag=True, default=False, help="Overwrite OUTPUT_PATH if it already exists.")
@click.option("--backend", type=click.Choice(["auto", "native", "python"]), default="auto", show_default=True, help="ESP runtime backend.")
@click.pass_context
def new(ctx, output_path, set_master, set_light, esl_alias, medium, update, localized, extra_masters, no_masters, force, backend):
    """Create a new empty plugin. The type comes from OUTPUT_PATH's extension.

    OUTPUT_PATH ends in .esp, .esm, or .esl; .esm auto-sets the Master bit and
    .esl auto-sets the Light bit. The game's base master (e.g. Fallout4.esm) is
    seeded automatically — use --no-masters to start bare. Game/header version
    come from the inherited --game.
    """
    from creation_lib.esp.authoring import new_plugin_file

    out = Path(output_path)
    extension = out.suffix.lower().lstrip(".")
    if extension not in {"esp", "esm", "esl"}:
        raise click.ClickException(
            f"Output extension must be .esp, .esm, or .esl (got {out.suffix or 'none'!r})."
        )
    resolved_light = True if esl_alias else set_light
    try:
        summary = new_plugin_file(
            out,
            game=ctx.obj.get("game"),
            extension=extension,
            masters=list(extra_masters),
            include_base_master=not no_masters,
            set_master=set_master,
            set_light=resolved_light,
            set_medium=medium,
            set_update=update,
            set_localized=localized,
            force=force,
            backend=backend,
        )
    except FileExistsError as exc:
        raise click.ClickException(str(exc))
    output(summary, ctx.obj.get("fmt", "json"))


@esp.command(name="check-errors")
@click.argument("plugin_path")
@click.option("--no-fail", is_flag=True, help="Exit 0 even when validation finds issues.")
@click.option("--max-errors", type=click.IntRange(min=1), default=None, help="Maximum error issues to include in output.")
@click.pass_context
def check_errors(ctx, plugin_path, no_fail, max_errors):
    """Check an ESP/ESM/ESL for xEdit-style validation errors."""
    from creation_lib.esp.editor import EditorSession, validate

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")

    game = ctx.obj.get("game")
    session = EditorSession(
        default_game=game,
        auto_scan_conflicts=False,
        master_search_paths=_esp_master_search_paths(game, plugin_file),
        lazy_masters=True,
    )
    try:
        loaded = session.load(plugin_file, game=game)
        report = validate(session, handle=loaded.handle)
        report_issues = list(report)
        error_count = sum(1 for issue in report_issues if issue.severity.value == "error")
        warning_count = sum(1 for issue in report_issues if issue.severity.value == "warning")
        output_issues, omitted_error_count, omitted_warning_count = _select_output_issues(report_issues, max_errors)
        result = {
            "plugin": loaded.plugin_name,
            "game": loaded.game,
            "issue_count": len(report_issues),
            "error_count": error_count,
            "warning_count": warning_count,
            "issues": [_validation_issue_to_dict(issue) for issue in output_issues],
        }
        if max_errors is not None:
            result["max_errors"] = max_errors
            result["displayed_issue_count"] = len(output_issues)
            result["omitted_error_count"] = omitted_error_count
            result["omitted_warning_count"] = omitted_warning_count
        output(result, ctx.obj.get("fmt", "json"))
        if len(report_issues) and not no_fail:
            ctx.exit(1)
    finally:
        session.close_all()


@esp.command(name="check-runtime-hazards")
@click.argument("plugin_path")
@click.option("--no-fail", is_flag=True, help="Exit 0 even when runtime hazards are found.")
@click.option("--max-hazards", type=click.IntRange(min=1), default=None, help="Maximum hazards to include in output.")
@click.option(
    "--profile",
    type=click.Choice(["fo76-to-fo4", "fo4-target-shape"]),
    default="fo76-to-fo4",
    show_default=True,
    help="Runtime-hazard rule profile to apply.",
)
@click.pass_context
def check_runtime_hazards(ctx, plugin_path, no_fail, max_hazards, profile):
    """Check an ESP/ESM/ESL for known FO4 loader-crash hazards."""
    from creation_lib.esp.editor import EditorSession
    from creation_lib.esp.editor.runtime_hazards import scan_runtime_hazards

    plugin_file = Path(plugin_path)
    if not plugin_file.is_file():
        raise click.ClickException(f"Plugin not found: {plugin_file}")

    game = ctx.obj.get("game")
    session = EditorSession(
        default_game=game,
        auto_scan_conflicts=False,
        master_search_paths=_esp_master_search_paths(game, plugin_file),
    )
    try:
        loaded = session.load(plugin_file, game=game)
        report = scan_runtime_hazards(session, handle=loaded.handle, profile=profile)
        output(report.to_dict(max_hazards=max_hazards), ctx.obj.get("fmt", "json"))
        if len(report) and not no_fail:
            ctx.exit(1)
    finally:
        session.close_all()


def _select_output_issues(report_issues: list[object], max_errors: int | None) -> tuple[list[object], int, int]:
    if max_errors is None:
        return report_issues, 0, 0

    issues = []
    error_count = 0
    warning_count = 0
    for issue in report_issues:
        if issue.severity.value == "warning":
            warning_count += 1
            continue
        if issue.severity.value != "error":
            continue
        error_count += 1
        if error_count <= max_errors:
            issues.append(issue)
    return issues, max(error_count - max_errors, 0), warning_count


def _validation_issue_to_dict(issue) -> dict:
    form_id = issue.form_id
    return {
        "severity": issue.severity.value,
        "category": issue.category.value,
        "plugin": issue.plugin_name,
        "form_id": form_id,
        "form_id_hex": f"{int(form_id):08X}" if form_id is not None else None,
        "message": issue.message,
    }


@esp.command(name="build-mod")
@click.argument("name")
@click.option("--skip-validate", is_flag=True, help="Skip FormKey validation before build")
@click.option("--all", "build_all", is_flag=True, help="Build main + all patch plugins")
@click.option("--patch", "patch_name", default=None, help="Build a specific patch plugin only")
@click.pass_context
def build_mod(ctx, name, skip_validate, build_all, patch_name):
    """Build a mod's .esp from its yaml/ authoring directory (validates first)."""
    from app.paths import get_app_root
    from creation_lib.esp.authoring import deserialize, get_plugin_ext
    from creation_lib.esp.validate import validate_authoring
    from creation_lib.mod.patches import list_patches, get_patch_yaml_dir, get_patch_plugin_name

    game = _resolve_mod_game(ctx, name)
    mod_dir = get_app_root() / "mods" / name
    data_folder = _resolve_game_data_dir(game)

    if not patch_name:
        if not (mod_dir / "yaml").is_dir():
            raise click.ClickException(f"{mod_dir / 'yaml'} not found")

        if not skip_validate:
            click.echo(f"Validating {name}...")
            errors, checked = validate_authoring(mod_dir / "yaml")
            if errors:
                for err in errors:
                    click.echo(f"  ERROR: {err}", err=True)
                raise click.ClickException(f"Validation found {len(errors)} error(s)")
            click.echo(f"  OK ({checked} FormKeys checked)")

        plugin_ext = get_plugin_ext(mod_dir)
        output_path = mod_dir / f"{name}.{plugin_ext}"

        try:
            deserialize(
                mod_dir / "yaml", output_path,
                game=game, data_folder=data_folder,
                on_progress=click.echo,
            )
            click.echo(f"Built: {output_path}")
        except (RuntimeError, FileNotFoundError) as e:
            raise click.ClickException(str(e))

    if build_all or patch_name:
        patches_to_build = [patch_name] if patch_name else list_patches(mod_dir)
        if not patches_to_build:
            click.echo("No patches found.")
            return

        for pname in patches_to_build:
            patch_yaml = get_patch_yaml_dir(mod_dir, pname)
            if not patch_yaml.is_dir():
                click.echo(f"WARNING: {patch_yaml} not found — skipping", err=True)
                continue

            plugin_file = get_patch_plugin_name(mod_dir, pname)
            patch_output = mod_dir / plugin_file

            if not skip_validate:
                click.echo(f"Validating patch {pname}...")
                try:
                    errors, checked = validate_authoring(patch_yaml)
                    if errors:
                        for err in errors:
                            click.echo(f"  ERROR: {err}", err=True)
                        click.echo(f"WARNING: Patch {pname} has {len(errors)} validation error(s)")
                    else:
                        click.echo(f"  OK ({checked} FormKeys checked)")
                except Exception as e:
                    click.echo(f"WARNING: Validation failed for {pname}: {e}", err=True)

            try:
                deserialize(
                    patch_yaml, patch_output,
                    game=game, data_folder=data_folder,
                    on_progress=click.echo,
                )
                click.echo(f"Built patch: {patch_output}")
            except (RuntimeError, FileNotFoundError) as e:
                click.echo(f"ERROR building patch {pname}: {e}", err=True)


@esp.command(name="inspect-mod")
@click.argument("esp_path")
@click.option("--name", "output_name", default=None, help="Output mod name (defaults to plugin stem)")
@click.option("--output-dir", default=None, help="Output directory (defaults to mods/<name>/)")
@click.pass_context
def inspect_mod(ctx, esp_path, output_name, output_dir):
    """Serialize an .esp to a mod-shaped yaml/ directory under mods/<name>/."""
    from app.paths import get_app_root
    from creation_lib.esp.authoring import serialize

    game = ctx.obj["game"]
    esp_file = Path(esp_path)

    if not esp_file.is_file():
        raise click.ClickException(f"Plugin not found: {esp_path}")

    if not output_name:
        output_name = esp_file.stem

    out = Path(output_dir) if output_dir else get_app_root() / "mods" / output_name
    data_folder = _resolve_game_data_dir(game)

    try:
        yaml_dir = serialize(
            esp_file, out,
            game=game, data_folder=data_folder,
            on_progress=click.echo,
        )
        click.echo(f"Serialized to: {yaml_dir}")
    except (RuntimeError, FileNotFoundError) as e:
        raise click.ClickException(str(e))


@esp.command(name="import-mod")
@click.argument("name")
@click.option("--plugin-path", default=None, help="Path to plugin (defaults to game Data/<Name>.<ext>)")
@click.option("--patch", "patch_name", default=None, help="Import CK changes for a specific patch")
@click.pass_context
def import_mod(ctx, name, plugin_path, patch_name):
    """Re-serialize a deployed plugin back into a mod's yaml/ source dir (CK-roundtrip)."""
    import shutil
    from app.paths import get_app_root
    from creation_lib.esp.authoring import serialize, get_plugin_ext

    game = _resolve_mod_game(ctx, name)
    mod_dir = get_app_root() / "mods" / name

    if patch_name:
        from creation_lib.mod.patches import get_patch_yaml_dir, get_patch_plugin_name
        target_yaml = get_patch_yaml_dir(mod_dir, patch_name)
        if not target_yaml.is_dir():
            raise click.ClickException(f"{target_yaml} not found — patch not set up")
        plugin_file = get_patch_plugin_name(mod_dir, patch_name)
    else:
        target_yaml = mod_dir / "yaml"
        if not target_yaml.is_dir():
            raise click.ClickException(f"{target_yaml} not found — not a known mod")
        plugin_ext = get_plugin_ext(mod_dir)
        plugin_file = f"{name}.{plugin_ext}"

    if plugin_path:
        esp_file = Path(plugin_path)
    else:
        data_folder = _resolve_game_data_dir(game)
        if not data_folder:
            raise click.ClickException(f"Cannot resolve game Data/ dir for {game}")
        esp_file = data_folder / plugin_file

    if not esp_file.is_file():
        raise click.ClickException(
            f"{esp_file} not found. Deploy the mod first, edit in CK, then import."
        )

    temp_dir = mod_dir / "_import_tmp"
    if temp_dir.is_dir():
        shutil.rmtree(temp_dir)

    try:
        click.echo(f"Importing CK changes from {esp_file}...")
        yaml_dir = serialize(
            esp_file, temp_dir,
            game=game, data_folder=_resolve_game_data_dir(game),
            on_progress=click.echo,
        )
        if target_yaml.is_dir():
            shutil.rmtree(target_yaml)
        shutil.move(str(yaml_dir), str(target_yaml))
    except Exception as e:
        raise click.ClickException(str(e))
    finally:
        if temp_dir.is_dir():
            shutil.rmtree(temp_dir, ignore_errors=True)

    click.echo(f"Imported CK changes to: {target_yaml}")
    click.echo("Run 'git diff' to review what changed.")
