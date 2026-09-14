from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

import click

from cli._output import output
from cli._query import select_rows
from cli.esp_commands import esp, _load_plugin


def _open(path, game):
    return _load_plugin(Path(path), game=game, strings_dir=None, language=None, backend="native", lazy_index=True)


def _ids(plugin, record_ids):
    from creation_lib.inspection.records import record_catalog
    if not record_ids:
        return []
    catalog = record_catalog(plugin)
    result = []
    for value in record_ids:
        text = value.strip()
        try:
            number = int(text, 16)
        except ValueError:
            matches = [r for r in catalog if r["editor_id"].casefold() == text.casefold() or r["form_key"].casefold() == text.casefold()]
        else:
            if len(text.removeprefix("0x")) > 6:
                matches = [r for r in catalog if r["form_id"] == number]
            else:
                matches = [r for r in catalog if r["form_id"] & 0xFFFFFF == number]
                owned = [r for r in matches if r["form_key"].split(":", 1)[0].casefold() == plugin.plugin_name.casefold()]
                matches = owned or matches
        if len(matches) != 1:
            raise click.ClickException(f"Record not found or ambiguous: {value}")
        result.append(matches[0]["form_id"])
    return result


def _types(signatures):
    from creation_lib.esp.record_types import record_type_signature
    return [record_type_signature(s) for s in signatures]


@esp.command("race-subgraphs")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("record_id", required=False)
@click.pass_context
def race_subgraphs(ctx, plugin_path, record_id):
    """Group RACE behavior paths, keywords and flags with the native AnimTextData parser."""
    from creation_lib.inspection.animation import race_subgraphs
    form_id = None
    if record_id:
        with _open(plugin_path, ctx.obj["game"]) as plugin:
            form_id = _ids(plugin, [record_id])[0]
    output(race_subgraphs(plugin_path, ctx.obj["game"], form_id), ctx.obj["fmt"], collection="races")


@esp.command("query")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--record", "record_ids", multiple=True, help="EditorID or local hex FormID; repeatable.")
@click.option("--type", "signatures", multiple=True)
@click.option("--match", default="*", help="EditorID glob.")
@click.pass_context
def query(ctx, plugin_path, record_ids, signatures, match):
    """Query decoded record fields without a full plugin export.

    Global --fields/--where/--limit/--offset/--count-only/--group-by precede esp.
    Example: modkit --fields eid,fields --limit 5 esp query B21_Example.esp --type ACTI
    """
    from creation_lib.inspection.records import record_rows
    with _open(plugin_path, ctx.obj["game"]) as plugin:
        rows = record_rows(plugin, signatures=_types(signatures), pattern=match, record_ids=_ids(plugin, record_ids))
        report = select_rows(rows, ctx.obj["report_options"])
        report["meta"].update(plugin=str(Path(plugin_path).resolve()), game=plugin.game)
        output(report, ctx.obj["fmt"], queried=True)


@esp.command("vmad")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("record_id", required=False)
@click.option("--type", "signatures", multiple=True)
@click.option("--script", default=None, help="Exact, case-insensitive attached script name.")
@click.option("--script-glob", default=None, help="Case-insensitive script glob.")
@click.option("--property", "property_name", default=None, help="Require an exact property name.")
@click.option("--fail-on-incomplete", is_flag=True, help="Exit 1 if any candidate VMAD could not be fully decoded.")
@click.pass_context
def vmad(ctx, plugin_path, record_id, signatures, script, script_glob, property_name, fail_on_incomplete):
    """Find decoded script bindings, typed properties, aliases and fragments.

    Example: modkit --fields editor_id,script,scope,properties esp vmad B21_Example.esp --script B21_Activator
    """
    from creation_lib.inspection.records import vmad_rows
    with _open(plugin_path, ctx.obj["game"]) as plugin:
        diagnostics = {}
        rows = vmad_rows(plugin, diagnostics, signatures=_types(signatures),
                         record_ids=_ids(plugin, [record_id]) if record_id else (),
                         script=script, script_glob=script_glob, property_name=property_name)
        report = select_rows(rows, ctx.obj["report_options"])
        report["meta"].update(plugin=str(Path(plugin_path).resolve()), game=plugin.game, **diagnostics)
        output(report, ctx.obj["fmt"], queried=True)
        if fail_on_incomplete and not diagnostics["complete"]:
            ctx.exit(1)


def semantic_diff(ctx, a_path, b_path, record_type, record_ids, fields, exclude, normalize):
    from creation_lib.esp import native_runtime
    from creation_lib.inspection.records import named_fields, normalize_references, field_differences, record_catalog

    def identity(plugin, row):
        owner, oid = row["form_key"].rsplit(":", 1)
        return ("$self" if owner.casefold() == plugin.plugin_name.casefold() else owner.casefold(), int(oid, 16))

    def decoded(plugin, row):
        if row is None:
            return {}
        inspected = native_runtime.plugin_handle_inspect_record(plugin._rust_handle, row["form_id"])
        return {**named_fields(inspected["record"]), "signature": inspected["signature"]}

    with _open(a_path, ctx.obj["game"]) as left, _open(b_path, ctx.obj["game"]) as right:
        types = _types([record_type]) if record_type else None
        a_index = {identity(left, row): row for row in record_catalog(left, types or ())}
        b_index = {identity(right, row): row for row in record_catalog(right, types or ())}
        selected = set(a_index) | set(b_index)
        if record_ids:
            selected = set()
            for value in record_ids:
                found = False
                for plugin, index in ((left, a_index), (right, b_index)):
                    try:
                        fid = _ids(plugin, [value])[0]
                    except click.ClickException:
                        continue
                    for key, row in index.items():
                        if row["form_id"] == fid:
                            selected.add(key)
                            found = True
                if not found:
                    raise click.ClickException(f"Record not found in either plugin: {value}")
        changes, counts = [], {"added": 0, "removed": 0, "changed": 0}
        for key in sorted(selected):
            a_row, b_row = a_index.get(key), b_index.get(key)
            before, after = decoded(left, a_row), decoded(right, b_row)
            if normalize:
                before, after = normalize_references(before, left.plugin_name), normalize_references(after, right.plugin_name)
            differences = field_differences(before, after, fields=fields, exclude=exclude)
            if not differences:
                continue
            status = "added" if a_row is None else "removed" if b_row is None else "changed"
            counts[status] += 1
            row = b_row or a_row
            changes.append({"object_id": f"{key[1]:06X}", "form_key": row["form_key"], "signature": row["signature"],
                            "editor_id": row["editor_id"], "status": status, "changes": differences})
        report = {"plugin_a": str(a_path.resolve()), "plugin_b": str(b_path.resolve()),
                  "semantic": True, "normalized_references": normalize, "counts": counts, "changes": changes}
        output(report, ctx.obj["fmt"], collection="changes")


def resolution_options(function):
    function = click.option("--asset-root", "asset_roots", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path), help="Data or asset-category root. Later roots take precedence.")(function)
    function = click.option("--archive", "archive_paths", multiple=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="BA2/BSA to search. Later archives take precedence.")(function)
    function = click.option("--master-search-path", "master_paths", multiple=True, type=click.Path(exists=True, file_okay=False, path_type=Path))(function)
    function = click.option("--load-order", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Ordered plugin paths/names, one per line. Read-only; * prefixes allowed.")(function)
    return function


@contextmanager
def _graph(ctx, plugin_path, master_paths, load_order):
    from creation_lib.inspection.graph import PluginGraph
    roots = [Path(plugin_path).resolve().parent, *master_paths]
    if load_order:
        roots.append(load_order.resolve().parent)
    with PluginGraph(ctx.obj["game"], roots) as graph:
        if load_order:
            for line in load_order.read_text(encoding="utf-8-sig").splitlines():
                name = line.strip().lstrip("*")
                if not name or name.startswith("#"):
                    continue
                path = graph.find(name)
                if path is None:
                    raise click.ClickException(f"Load-order plugin not found: {name}")
                graph.load(path)
        plugin = graph.load(plugin_path)
        yield graph, plugin


def _resolver(plugin_path, asset_roots, archive_paths):
    from creation_lib.inspection.assets import AssetResolver
    roots = list(asset_roots)
    if not roots:
        root = Path(plugin_path).resolve().parent
        roots = [root]
        if (root / "data").is_dir():
            roots.append(root / "data")
    return AssetResolver(roots, archive_paths)


@esp.command("explain")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("record_id")
@resolution_options
@click.option("--depth", type=click.IntRange(min=0), default=2, show_default=True)
@click.option("--max-records", type=click.IntRange(min=1), default=100, show_default=True)
@click.option("--max-assets", type=click.IntRange(min=1), default=500, show_default=True)
@click.pass_context
def explain(ctx, plugin_path, record_id, asset_roots, archive_paths, master_paths, load_order, depth, max_records, max_assets):
    """Follow record references, VMAD scripts, NIF materials and textures; show override winners and missing assets."""
    from creation_lib.inspection.graph import explain_record
    from creation_lib.inspection.records import form_key
    with _graph(ctx, plugin_path, master_paths, load_order) as (graph, plugin):
        fid = _ids(plugin, [record_id])[0]
        matches = [row for row in graph.index(plugin).values() if row["form_id"] == fid]
        if not matches:
            matches = [row for row in graph.index(plugin).values() if row["form_id"] & 0xFFFFFF == fid & 0xFFFFFF]
        if len(matches) != 1:
            raise click.ClickException(f"Ambiguous record: {record_id}; specify a raw FormID")
        key = form_key(plugin, matches[0]["form_id"])
        report = explain_record(graph, key, _resolver(plugin_path, asset_roots, archive_paths),
                                depth=depth, max_records=max_records, max_assets=max_assets)
        report["load_order"] = [p.plugin_name for p in graph.plugins]
        output(report, ctx.obj["fmt"])


def _placements(plugin, cell=None, worldspace=None):
    from creation_lib.esp import native_runtime
    from cli.esp_commands import _PLACED_RECORD_SIGNATURES, _resolve_cell_raw_form_id
    from creation_lib.inspection.records import record_catalog
    if cell and worldspace:
        raise click.UsageError("--cell and --worldspace are mutually exclusive")
    if cell:
        fid = _resolve_cell_raw_form_id(plugin._rust_handle, cell)
        return native_runtime.plugin_handle_collect_cell_children(plugin._rust_handle, fid)
    rows = record_catalog(plugin, _PLACED_RECORD_SIGNATURES)
    if worldspace:
        from creation_lib.inspection.records import form_key
        roots = native_runtime.plugin_handle_collect_cell_slice_roots(plugin._rust_handle,
            worldspace_editor_id=worldspace, min_x=-(2**31), min_y=-(2**31), max_x=2**31-1, max_y=2**31-1,
            include_worldspace_persistent_cell=True)
        if not roots.get("worldspace_form_keys"):
            raise click.ClickException(f"Worldspace not found: {worldspace}")
        wanted = {key.casefold() for key in roots.get("placed_form_keys", [])}
        rows = [row for row in rows if form_key(plugin, row["form_id"]).casefold() in wanted]
    return rows


@esp.command("placed-models")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--cell", default=None)
@click.option("--worldspace", default=None)
@click.option("--base-type", "base_types", multiple=True)
@click.option("--fail-on-missing", is_flag=True)
@resolution_options
@click.pass_context
def placed_models(ctx, plugin_path, cell, worldspace, base_types, fail_on_missing, asset_roots, archive_paths, master_paths, load_order):
    """Audit placed base models against loose files and archives, including malformed paths and unresolved bases."""
    from creation_lib.inspection.placed import placed_rows, model_census
    with _graph(ctx, plugin_path, master_paths, load_order) as (graph, plugin):
        resolver = _resolver(plugin_path, asset_roots, archive_paths)
        report = model_census(placed_rows(graph, plugin, _placements(plugin, cell, worldspace), resolver, base_types=_types(base_types)))
        report.update(plugin=str(plugin_path.resolve()), cell=cell, worldspace=worldspace, issues=graph.issues, resolution=resolver.describe())
        failed = bool(graph.issues) or any(row["status"] != "available" for row in report["records"])
        output(report, ctx.obj["fmt"], collection="records")
        if fail_on_missing and failed:
            ctx.exit(1)


@esp.command("cell-collision")
@click.argument("plugin_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("cell")
@click.option("--base-type", "base_types", multiple=True)
@resolution_options
@click.pass_context
def cell_collision(ctx, plugin_path, cell, base_types, asset_roots, archive_paths, master_paths, load_order):
    """Inventory a cell's placed models and decoded collision bodies, layers and masses."""
    from creation_lib.inspection.placed import placed_rows
    with _graph(ctx, plugin_path, master_paths, load_order) as (graph, plugin):
        resolver = _resolver(plugin_path, asset_roots, archive_paths)
        rows = placed_rows(graph, plugin, _placements(plugin, cell=cell), resolver, base_types=_types(base_types), collision=True)
        report = select_rows(rows, ctx.obj["report_options"])
        report["meta"].update(plugin=str(plugin_path.resolve()), cell=cell, issues=graph.issues, resolution=resolver.describe())
        output(report, ctx.obj["fmt"], queried=True)


def register_asset_commands(cli):
    from cli.nif_commands import nif

    @nif.command("collision-report")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.pass_context
    def nif_collision_report(ctx, path):
        """Read collision blocks and embedded Havok bodies without editing the NIF."""
        from creation_lib.inspection.collision import collision_report
        output({"path": str(path.resolve()), **collision_report(path.read_bytes())}, ctx.obj["fmt"], collection="blocks")

    @cli.group("behavior")
    def behavior():
        """Inspect local HKX behavior graphs and their XML sources."""

    @behavior.command("report")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.pass_context
    def report(ctx, path):
        """Describe events, variables, states, transitions, clips and unresolved graph references."""
        from creation_lib.inspection.behavior import behavior_report
        output(behavior_report(path), ctx.obj["fmt"])
