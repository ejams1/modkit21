"""modkit data — creation-data CLI commands."""

import json
import sys

import click

import creation_lib.creation_data as cd
from cli._output import output


def _ctx_args(ctx):
    """Extract game, fmt, db_dir from click context."""
    return ctx.obj["game"], ctx.obj["fmt"], ctx.obj["db_dir"]


# Common option decorators
def filter_options(f):
    """Add common filter options for search/list commands."""
    f = click.option("-t", "--type", "record_type", default="", help="Filter by record type (e.g. Weapons)")(f)
    f = click.option("-s", "--source", default="", help="Filter by source (e.g. Fallout4.esm)")(f)
    f = click.option("--category", default="", help="Filter by category")(f)
    f = click.option("--extends", default="", help="Filter by parent script type")(f)
    f = click.option("--mod", "mod_name", default="", help="Filter by external mod name")(f)
    f = click.option("-e", "--external", is_flag=True, help="Include external mod data")(f)
    return f


def result_options(f):
    """Add max-results option."""
    f = click.option("-n", "--max-results", default=10, type=int, help="Max results (default 10)")(f)
    return f


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.option("--format", "fmt", type=click.Choice(["json", "pretty", "compact", "table", "jsonl"]), default=None, help="Output format (overrides global --format).")
@click.pass_context
def data(ctx, game, fmt):
    """Search and query Bethesda game data — records, scripts, wiki, behaviors, NIFs."""
    if game is not None:
        ctx.obj["game"] = game
    if fmt is not None:
        ctx.obj["fmt"] = fmt


@data.command("schema")
@click.argument("field")
@click.option("--form-version", type=click.IntRange(0, 65535), default=None)
@click.pass_context
def schema(ctx, field, form_version):
    """Show native schema offsets and widths for SIG.SUB, e.g. RACE.DATA."""
    from creation_lib.esp.native_runtime import schema_field_layout
    parts = field.upper().split(".")
    if len(parts) != 2 or any(len(part) != 4 or not part.isascii() for part in parts):
        raise click.UsageError("Expected SIG.SUB, for example RACE.DATA")
    output(schema_field_layout(ctx.obj["game"], *parts, form_version), ctx.obj["fmt"], collection="fields")


@data.command()
@click.argument("domain")
@click.argument("query", default="")
@filter_options
@click.option("--full-text", is_flag=True, help="Full-text search for NIFs domain")
@click.option("--cross-game", is_flag=True, help="Search across all games (behaviors/nifs)")
@result_options
@click.pass_context
def search(ctx, domain, query, record_type, source, category, extends, mod_name, external, full_text, cross_game, max_results):
    """Full-text search across game data.

    DOMAIN: records, scripts, wiki, behaviors, nifs, ext_records, ext_scripts

    Examples:

      modkit data search records "combat shotgun"

      modkit data search scripts "OnDeath" --extends Quest

      modkit data search nifs "laser" --full-text
    """
    game, fmt, db_dir = _ctx_args(ctx)
    # Map external flag to include_external for records/scripts, or mod_name for ext_ domains
    result = cd.search(
        domain=domain, query=query, record_type=record_type, source=source,
        category=category, extends=extends, mod_name=mod_name, full_text=full_text,
        cross_game=cross_game, max_results=max_results, game=game, db_dir=db_dir,
    )
    output(result, fmt)


@data.command()
@click.argument("domain")
@click.argument("query")
@filter_options
@click.option("--cross-game", is_flag=True, help="Search across all games (behaviors/nifs)")
@result_options
@click.pass_context
def semantic(ctx, domain, query, record_type, source, category, extends, mod_name, external, cross_game, max_results):
    """AI semantic search (natural language queries).

    Examples:

      modkit data semantic records "place where you craft chems"

      modkit data semantic scripts "script that handles death"
    """
    game, fmt, db_dir = _ctx_args(ctx)
    try:
        result = cd.semantic_search(
            domain=domain, query=query, record_type=record_type, source=source,
            category=category, extends=extends, mod_name=mod_name, cross_game=cross_game,
            max_results=max_results, game=game, db_dir=db_dir, model_ready=True,
        )
    except ImportError:
        print("Error: semantic search requires sentence-transformers. Install with: uv pip install sentence-transformers", file=sys.stderr)
        sys.exit(1)
    output(result, fmt)


@data.command()
@click.argument("domain")
@click.argument("id")
@click.pass_context
def get(ctx, domain, id):
    """Get full content by ID.

    DOMAIN: records, scripts, wiki, behaviors, nifs, ext_records, ext_scripts

    Examples:

      modkit data get records "004822:Fallout4.esm"

      modkit data get scripts Actor
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_content(domain=domain, id=id, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command("list")
@click.argument("domain")
@filter_options
@result_options
@click.pass_context
def list_cmd(ctx, domain, record_type, source, category, extends, mod_name, external, max_results):
    """List or count items in a domain.

    DOMAIN: records, record_types, scripts, extends_types, script_types,
    wiki_categories, wiki_record_types, behaviors, nifs, nif_categories, ext_mods

    Examples:

      modkit data list record_types

      modkit data list scripts --extends Quest -n 50
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.list_items(
        domain=domain, record_type=record_type, source=source, category=category,
        extends=extends, mod_name=mod_name, max_results=max_results, game=game, db_dir=db_dir,
    )
    output(result, fmt)


def _validate_data_form_key(ctx, param, value):
    import re
    if value:
        match = re.fullmatch(r"(.+\.es[mpl]):((?:0x)?[0-9a-f]+)", value, re.IGNORECASE)
        if match:
            plugin, object_id = match.groups()
            raise click.BadParameter(f"FormKey parts are reversed; use '{object_id}:{plugin}' (ObjectID:Plugin).", ctx=ctx, param=param)
    return value


@data.command()
@click.argument("form_key", callback=_validate_data_form_key)
@click.option("--content", "include_content", is_flag=True, help="Include full YAML content")
@click.pass_context
def record(ctx, form_key, include_content):
    """Get a game record by FormKey.

    Examples:

      modkit data record "004822:Fallout4.esm"

      modkit data record "004822:Fallout4.esm" --content
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_record(form_key=form_key, include_content=include_content, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("form_key", callback=_validate_data_form_key)
@click.option("-t", "--type", "record_type", default="", help="Filter by record type")
@result_options
@click.pass_context
def refs(ctx, form_key, record_type, max_results):
    """Find all records referencing a FormKey.

    Examples:

      modkit data refs "067384:Fallout4.esm"

      modkit data refs "067384:Fallout4.esm" --type Weapons
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_references(form_key=form_key, record_type=record_type, max_results=max_results, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("editor_id")
@click.pass_context
def lookup(ctx, editor_id):
    """Look up records by EditorID (case-insensitive).

    Examples:

      modkit data lookup WorkbenchChemistry
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.lookup_editor_id(editor_id=editor_id, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("keyword")
@click.option("-t", "--type", "record_type", default="", help="Filter by record type")
@result_options
@click.pass_context
def keyword(ctx, keyword, record_type, max_results):
    """Find records with a specific keyword.

    Examples:

      modkit data keyword HasReceiver

      modkit data keyword HasReceiver --type Weapons
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.search_by_keyword(keyword=keyword, record_type=record_type, max_results=max_results, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("form_key", callback=_validate_data_form_key)
@click.pass_context
def keywords(ctx, form_key):
    """Get all keywords for a record, resolved to EditorIDs.

    Examples:

      modkit data keywords "004822:Fallout4.esm"
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.resolve_keywords(form_key=form_key, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command("count-refs")
@click.argument("form_key", callback=_validate_data_form_key)
@click.pass_context
def count_refs(ctx, form_key):
    """Count references to a FormKey (fast).

    Examples:

      modkit data count-refs "067384:Fallout4.esm"
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.count_references(form_key=form_key, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("name")
@click.option("--script-type", default="", help="Filter by parent script (e.g. Actor)")
@click.pass_context
def function(ctx, name, script_type):
    """Look up a Papyrus function by name.

    Examples:

      modkit data function AddItem

      modkit data function AddItem --script-type Actor
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_function(function_name=name, script_type=script_type, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("script_type")
@click.pass_context
def functions(ctx, script_type):
    """List all functions for a Papyrus script type.

    Examples:

      modkit data functions Actor
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.list_functions(script_type=script_type, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("script_type")
@click.pass_context
def api(ctx, script_type):
    """Get full API page for a Papyrus script type.

    Examples:

      modkit data api Actor
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_script_api(script_type=script_type, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("script_type")
@click.pass_context
def hierarchy(ctx, script_type):
    """Walk the extends chain for a script type.

    Examples:

      modkit data hierarchy Actor
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_script_hierarchy(script_type=script_type, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("behavior_id")
@click.pass_context
def behavior(ctx, behavior_id):
    """Get raw XML content of a behavior file.

    Examples:

      modkit data behavior "fo4/UniqueBehaviors/FlamerFX/Behavior"
    """
    game, fmt, db_dir = _ctx_args(ctx)
    result = cd.get_behavior_xml(behavior_id=behavior_id, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("commands_json")
@click.pass_context
def batch(ctx, commands_json):
    """Execute multiple queries from JSON.

    Examples:

      modkit data batch '[{"tool":"search","args":{"domain":"records","query":"shotgun"}}]'
    """
    game, fmt, db_dir = _ctx_args(ctx)
    try:
        commands = json.loads(commands_json)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    result = cd.batch(commands=commands, game=game, db_dir=db_dir)
    output(result, fmt)


@data.command()
@click.argument("mod_name")
@click.option("--asset", "asset_path", default="", help="Asset path substring to trace.")
@click.option("--record", "record_fk", default="", help="FormKey or EditorID substring to trace.")
@click.pass_context
def trace(ctx, mod_name, asset_path, record_fk):
    """Read conversion provenance as structured records or an asset-owner summary."""
    from collections import Counter
    from app.paths import get_app_root

    if asset_path and record_fk:
        raise click.UsageError("--asset and --record are mutually exclusive")
    root = (get_app_root() / "mods").resolve()
    mod_dir = (root / mod_name).resolve()
    if not mod_dir.is_relative_to(root):
        raise click.BadParameter("Mod name must stay inside mods/", param_hint="mod_name")
    if not mod_dir.is_dir():
        raise click.ClickException(f"Mod directory not found: {mod_dir}")
    path = mod_dir / ("record_provenance.jsonl" if record_fk else "asset_provenance.jsonl")
    if not path.is_file():
        raise click.ClickException(f"{path} not found — run conversion first.")
    matches, counts = [], Counter()
    query = (asset_path or record_fk).casefold()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise click.ClickException(f"{path}:{line_number}: {error.msg}") from error
            if query:
                keys = ("asset_path",) if asset_path else ("form_key", "editor_id")
                if any(query in str(entry.get(key) or "").casefold() for key in keys):
                    matches.append(entry)
            else:
                counts[(entry.get("added_by_record_eid") or "(unknown)", entry.get("added_by_record_fk") or "")] += 1
    if not query:
        matches = [{"editor_id": eid, "form_key": fk, "asset_count": count}
                   for (eid, fk), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    output({"mod": mod_name, "provenance_file": str(path), "mode": "records" if record_fk else "assets" if asset_path else "summary",
            "matches": matches}, ctx.obj["fmt"], collection="matches")
