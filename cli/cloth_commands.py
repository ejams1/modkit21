"""modkit cloth: FO4 Havok cloth authoring commands."""
from __future__ import annotations

import json
from pathlib import Path

import click

from cli._output import JSON_FORMATS, output


@click.group()
def cloth():
    """Fallout 4 Havok cloth authoring commands."""


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def inspect(ctx: click.Context, nif_path: Path):
    """Print a summary of the cloth data in a NIF.

    Shows: cloth name, sim cloth count, particle count, constraint
    set counts by type, collidable count, operator count, state count.
    """
    from creation_lib._native import havok_native, nif_core_native

    try:
        blob = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
        raw_json = havok_native.cloth_inspect_blob_json(blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    graph = json.loads(raw_json)
    objects = graph.get("objects", [])

    # Index objects by position for pointer resolution (pointers stored as "#NNNN" strings).
    obj_by_pos = {i: o for i, o in enumerate(objects)}
    # Build name→index map for pointer resolution by name.
    name_to_idx = {o.get("name", ""): i for i, o in enumerate(objects) if o.get("name")}

    def resolve_ptr(ptr_str: str) -> dict | None:
        if not isinstance(ptr_str, str) or not ptr_str.startswith("#"):
            return None
        try:
            target = int(ptr_str[1:])
        except ValueError:
            target = name_to_idx.get(ptr_str)
        return obj_by_pos.get(target) if target is not None else None

    def count_class(cls: str) -> int:
        return sum(1 for o in objects if o.get("class") == cls)

    # Find root hclClothData
    cloth_obj = next((o for o in objects if o.get("class") == "hclClothData"), None)
    if cloth_obj is None:
        click.echo("error: NIF has BSClothExtraData but no hclClothData inside", err=True)
        ctx.exit(3)
        return

    cloth_name = cloth_obj.get("members", {}).get("name", "")

    # Resolve sim cloth datas
    sim_cloth_ptrs = cloth_obj.get("members", {}).get("simClothDatas", []) or []
    sim_cloths = [resolve_ptr(p) for p in sim_cloth_ptrs if resolve_ptr(p) is not None]

    def _particle_count(sc: dict) -> int:
        pd = sc.get("members", {}).get("particleDatas")
        return len(pd) if isinstance(pd, list) else 0

    def _fixed_count(sc: dict) -> int:
        fp = sc.get("members", {}).get("fixedParticles")
        return len(fp) if isinstance(fp, list) else 0

    def _constraint_sets(sc: dict) -> list[dict]:
        ptrs = sc.get("members", {}).get("staticConstraintSets") or []
        return [c for p in ptrs for c in [resolve_ptr(p)] if c is not None]

    def _collidables(sc: dict) -> list:
        ptrs = sc.get("members", {}).get("perInstanceCollidables") or []
        return [resolve_ptr(p) for p in ptrs if resolve_ptr(p) is not None]

    total_particles = sum(_particle_count(s) for s in sim_cloths)
    total_fixed = sum(_fixed_count(s) for s in sim_cloths)
    cset_counts: dict[str, int] = {}
    for sc in sim_cloths:
        for cs in _constraint_sets(sc):
            cn = cs.get("class", "unknown")
            cset_counts[cn] = cset_counts.get(cn, 0) + 1
    constraint_set_count = sum(cset_counts.values())
    collidable_count = sum(len(_collidables(sc)) for sc in sim_cloths)

    # Operators referenced from hclClothData
    op_ptrs = cloth_obj.get("members", {}).get("operators", []) or []
    operator_objs = [resolve_ptr(p) for p in op_ptrs if resolve_ptr(p) is not None]
    op_counts: dict[str, int] = {}
    for op in operator_objs:
        cn = op.get("class", "unknown")
        op_counts[cn] = op_counts.get(cn, 0) + 1

    state_count = count_class("hclClothState")
    buf_count = count_class("hclBufferDefinition")
    xform_count = count_class("hclTransformSetDefinition")

    summary = {
        "nif_path": str(nif_path),
        "class_name": "hclClothData",
        "cloth_name": cloth_name,
        "num_sim_cloths": len(sim_cloths),
        "total_particles": total_particles,
        "total_fixed_particles": total_fixed,
        "num_constraint_sets": constraint_set_count,
        "constraint_set_counts_by_class": cset_counts,
        "num_collidables": collidable_count,
        "num_cloth_states": state_count,
        "num_operators": len(operator_objs),
        "operator_counts_by_class": op_counts,
        "num_buffer_definitions": buf_count,
        "num_transform_set_definitions": xform_count,
    }

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output(summary, fmt)
    else:  # table / default
        click.echo(f"NIF:              {summary['nif_path']}")
        click.echo(f"Cloth class:      {summary['class_name']}")
        click.echo(f"Cloth name:       {summary['cloth_name']!r}")
        click.echo(f"Sim cloths:       {summary['num_sim_cloths']}")
        click.echo(f"Total particles:  {summary['total_particles']}")
        click.echo(f"  pinned:         {summary['total_fixed_particles']}")
        click.echo(f"Constraint sets:  {summary['num_constraint_sets']}")
        for cls, n in sorted(cset_counts.items()):
            click.echo(f"  {cls}: {n}")
        click.echo(f"Collidables:      {summary['num_collidables']}")
        click.echo(f"Cloth states:     {summary['num_cloth_states']}")
        click.echo(f"Operators:        {summary['num_operators']}")
        for cls, n in sorted(op_counts.items()):
            click.echo(f"  {cls}: {n}")
        click.echo(f"Buffer defs:      {summary['num_buffer_definitions']}")
        click.echo(f"Transform defs:   {summary['num_transform_set_definitions']}")


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o", "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination HKX packfile path.",
)
@click.pass_context
def extract(ctx: click.Context, nif_path: Path, out: Path):
    """Extract the raw HCL packfile bytes from a NIF to a .hkx file."""
    from creation_lib._native import nif_core_native

    try:
        blob = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    click.echo(f"Wrote {len(blob)} bytes to {out}")


@cloth.command()
@click.argument("hkx_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--into", "source_nif",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Source NIF to copy structure from. Its BSClothExtraData will be replaced.",
)
@click.option(
    "-o", "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination NIF path.",
)
@click.pass_context
def pack(ctx: click.Context, hkx_path: Path, source_nif: Path, out: Path):
    """Embed an HKX packfile into a NIF's BSClothExtraData block.

    Copies the source NIF to a new file, replacing the cloth blob
    with the bytes from <hkx_path>. The HKX bytes are written
    verbatim — no validation that they're a valid HCL packfile.
    """
    from creation_lib._native import nif_core_native

    blob = hkx_path.read_bytes()
    if len(blob) == 0:
        click.echo("error: HKX file is empty", err=True)
        ctx.exit(2)
        return

    try:
        new_nif_bytes = nif_core_native.cloth_pack_blob(source_nif.read_bytes(), blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(3)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_nif_bytes)
    click.echo(f"Wrote {out} ({len(blob)} bytes of cloth data)")


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-n", "--frames", type=int, default=120, help="Number of frames to simulate.")
@click.option("--substeps", type=int, default=4, help="Substeps per frame.")
@click.option("--iterations", type=int, default=8, help="Constraint iterations per substep.")
@click.option("--gravity-z", type=float, default=-686.7, help="Gravity Z component.")
@click.option("--wind", type=(float, float, float), default=(0, 0, 0),
              help="Wind vector (X Y Z).")
@click.option("--damping", type=float, default=0.999, help="Velocity damping factor.")
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Output path (.obj for mesh export, .csv for position trace).")
@click.pass_context
def solve(ctx: click.Context, nif_path: Path, frames: int, substeps: int,
          iterations: int, gravity_z: float, wind: tuple, damping: float,
          out: Path | None):
    """Run headless XPBD cloth simulation and report performance.

    Loads cloth data from a NIF, builds a solver, runs N frames,
    and prints timing statistics. Optionally exports the final
    particle positions as OBJ or a position trace as CSV.

    Examples:

        modkit cloth solve outfit.nif -n 300

        modkit cloth solve outfit.nif -n 300 --gravity-z -686.7 -o preview.obj

        modkit cloth solve outfit.nif -n 60 --wind 100 0 0 -o trace.csv
    """
    import json
    import time
    import numpy as np
    from creation_lib._native import havok_native, nif_core_native

    # Extract cloth blob from NIF
    try:
        blob_bytes = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
    except Exception as exc:
        click.echo(f"error extracting cloth blob: {exc}", err=True)
        ctx.exit(2)
        return

    config_obj = {
        "substeps": substeps,
        "constraint_iterations": iterations,
        "gravity": [0.0, 0.0, gravity_z],
        "wind": list(wind),
        "damping": damping,
    }
    config_json_str = json.dumps(config_obj)

    # Run full simulation batch and time it
    t0 = time.perf_counter()
    try:
        result_json = havok_native.cloth_simulate_from_blob(blob_bytes, frames, config_json_str)
    except Exception as exc:
        click.echo(f"error simulating cloth: {exc}", err=True)
        ctx.exit(3)
        return
    t1 = time.perf_counter()

    result = json.loads(result_json)
    positions = np.array(result["positions"], dtype=np.float32)
    n_particles = result["n_particles"]
    fixed_count = result["fixed_count"]

    # NaN check
    if np.any(np.isnan(positions)):
        click.echo("WARNING: NaN detected in final positions!", err=True)

    total_ms = (t1 - t0) * 1000.0
    avg = total_ms / frames if frames > 0 else 0.0
    fps = 1000.0 / avg if avg > 0 else 0

    click.echo(f"Solver ready: {n_particles} particles ({fixed_count} fixed)")
    click.echo(f"Config: substeps={substeps}, iterations={iterations}, gravity_z={gravity_z}")
    click.echo(f"\nSimulated {frames} frames (batch):")
    click.echo(f"  Total: {total_ms:.1f} ms")
    click.echo(f"  Avg:  {avg:.2f} ms/frame ({fps:.0f} FPS)")

    if fps >= 50:
        click.echo("  Performance: OK")
    elif fps >= 30:
        click.echo("  Performance: SLOW (consider reducing substeps/iterations)")
    else:
        click.echo("  Performance: TOO SLOW")

    # Write output if requested
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".obj":
            # Export final positions as OBJ point cloud
            with open(out, "w") as f:
                f.write(f"# Cloth solver output: {n_particles} particles, {frames} frames\n")
                for i in range(n_particles):
                    p = positions[i]
                    f.write(f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n")
            click.echo(f"\nWrote OBJ to {out}")
        elif out.suffix.lower() == ".csv":
            # Collect COM trace by re-simulating at 10-frame checkpoints
            # Approximate fixed mask: particles that don't move under gravity
            pos0 = np.array(
                json.loads(havok_native.cloth_simulate_from_blob(blob_bytes, 0, config_json_str))["positions"],
                dtype=np.float32,
            )
            # Fixed particles stay at T=0 position; movable particles drift
            traces = []
            checkpoints = list(range(0, frames + 1, 10))
            if frames not in checkpoints:
                checkpoints.append(frames)
            for frame in checkpoints:
                if frame == 0:
                    pos_f = pos0
                else:
                    pos_f = np.array(
                        json.loads(havok_native.cloth_simulate_from_blob(blob_bytes, frame, config_json_str))["positions"],
                        dtype=np.float32,
                    )
                if frame == 0:
                    com = pos_f.mean(axis=0)
                    traces.append((frame, float(com[0]), float(com[1]), float(com[2])))
                    continue
                # Approximate movable mask: particles that moved from T=0
                diff = np.linalg.norm(pos_f - pos0, axis=1)
                mask = diff > 0.01
                if not np.any(mask):
                    mask = slice(None)
                com = pos_f[mask].mean(axis=0)
                traces.append((frame, float(com[0]), float(com[1]), float(com[2])))
            with open(out, "w") as f:
                f.write("frame,com_x,com_y,com_z\n")
                for row in traces:
                    f.write(f"{row[0]},{row[1]:.4f},{row[2]:.4f},{row[3]:.4f}\n")
            click.echo(f"\nWrote position trace to {out}")


def _dump_member_value(member) -> object:
    """Extract a JSON-serializable value from an HKXMember."""
    if hasattr(member, "contents"):
        return _dump_value(member.contents)
    if hasattr(member, "value"):
        return _dump_value(member.value)
    return None


def _dump_value(value) -> object:
    """Convert an HKXObject member value into something json-serializable."""
    if value is None:
        return None
    # HKXObject -> dump its members recursively
    if hasattr(value, "class_name") and hasattr(value, "members"):
        result: dict = {"$class": value.class_name}
        for m in value.members:
            result[m.name] = _dump_member_value(m)
        return result
    # Lists: recurse
    if isinstance(value, (list, tuple)):
        # For large arrays (>50 items), truncate to avoid blowup
        items = value[:50]
        out = [_dump_value(v) for v in items]
        if len(value) > 50:
            out.append(f"... ({len(value)} total, showing 50)")
        return out
    # dicts: recurse
    if isinstance(value, dict):
        return {str(k): _dump_value(v) for k, v in value.items()}
    # bytes -> hex
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes_len": len(value), "$bytes_head": value[:32].hex()}
    # scalars
    if isinstance(value, (int, float, bool, str)):
        return value
    # Fallback
    return repr(value)


def _dump_hkx_object(obj) -> dict:
    """Recursively dump an HKXObject's members as a JSON-friendly dict."""
    result: dict = {"class": obj.class_name}
    for member in obj.members:
        result[member.name] = _dump_member_value(member)
    return result


def _apply_material_preset(setup_data: dict, preset_name: str) -> dict:
    """Patch a setup dict in-place with the named material preset values.

    Returns the same dict (mutated) so the caller can pass it straight to
    json.dumps() before handing off to havok_native.cloth_bake().
    """
    from creation_lib._native import havok_native as _hn
    patched_json = _hn.cloth_material_apply(json.dumps(setup_data), preset_name)
    setup_data.clear()
    setup_data.update(json.loads(patched_json))
    return setup_data


@cloth.command()
@click.argument("setup_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--into", "source_nif",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Source NIF to copy structure from. Its BSClothExtraData will be replaced.",
)
@click.option(
    "-o", "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination NIF path.",
)
@click.option(
    "--material",
    type=str,
    default=None,
    help="Apply a material preset before baking (e.g., Cotton, Silk, Denim).",
)
@click.pass_context
def bake(ctx: click.Context, setup_json: Path, source_nif: Path, out: Path, material: str | None):
    """Bake a setup JSON into a NIF's BSClothExtraData.

    Reads a ClothSetupObject JSON file (from 'modkit cloth import'),
    bakes it into runtime HKX data, and embeds it into a copy of the
    source NIF.

    Examples:

        modkit cloth bake bathrobe_setup.json --into outfit.nif -o baked.nif

        modkit cloth bake setup.json --into outfit.nif -o baked.nif --material Cotton
    """
    from creation_lib._native import havok_native, nif_core_native

    # Load setup as dict for JSON-level manipulation
    try:
        json_text = setup_json.read_text(encoding="utf-8")
        setup_data = json.loads(json_text)
    except Exception as exc:
        click.echo(f"error: failed to load setup JSON: {exc}", err=True)
        ctx.exit(2)
        return

    # Apply material preset (Python-side dict patch) before handing to native bake
    if material:
        try:
            _apply_material_preset(setup_data, material)
        except KeyError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(2)
            return

    # Bake via native
    try:
        blob = havok_native.cloth_bake(json.dumps(setup_data))
    except Exception as exc:
        click.echo(f"error: bake failed: {exc}", err=True)
        ctx.exit(3)
        return

    # Embed blob into NIF via native
    try:
        new_nif_bytes = nif_core_native.cloth_pack_blob(source_nif.read_bytes(), blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(5)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_nif_bytes)

    sc_count = len(setup_data.get("sim_cloth_setups", []))
    click.echo(f"Baked {setup_json.name}: {sc_count} sim cloth(s), {len(blob)} bytes -> {out}")
    if material:
        click.echo(f"  Material preset: {material}")


@cloth.command("import")
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "-o", "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Output JSON file path for the setup data.",
)
@click.pass_context
def import_cloth(ctx: click.Context, nif_path: Path, out: Path):
    """Import cloth data from a NIF as an editable setup JSON.

    Reads runtime cloth data, reverses it to the setup model,
    and exports as JSON for editing or re-baking.
    """
    from creation_lib._native import havok_native as _hn
    from creation_lib._native import nif_core_native as _nif

    try:
        blob = _nif.cloth_extract_blob(nif_path.read_bytes())
        json_str = _hn.cloth_reverse_to_setup(blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json_str, encoding="utf-8")

    setup_data = json.loads(json_str)
    sc_count = len(setup_data.get("sim_cloth_setups", []))
    particle_count = sum(
        sc.get("simulation_mesh", {}).get("num_particles", 0)
        for sc in setup_data.get("sim_cloth_setups", [])
    )
    click.echo(
        f"Imported {nif_path.name}: {sc_count} sim cloth(s), "
        f"{particle_count} particles -> {out}"
    )


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def dump(ctx: click.Context, nif_path: Path):
    """Dump the full HCL object graph as JSON.

    Emits every HKXObject in the cloth data with all its members.
    Nested structs are expanded inline; cross-object references
    (e.g. '#0003') appear as strings. Large arrays are truncated
    to 50 entries.
    """
    from creation_lib._native import havok_native, nif_core_native

    try:
        blob = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
        raw_json = havok_native.cloth_inspect_blob_json(blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    graph = json.loads(raw_json)
    out_data = {
        "nif_path": str(nif_path),
        "havok_version": graph.get("havok_version", "unknown"),
        "object_count": graph.get("object_count", 0),
        "objects": graph.get("objects", []),
    }

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    output(out_data, fmt if fmt in JSON_FORMATS else "json")


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def validate(ctx: click.Context, nif_path: Path):
    """Validate a cloth graph for common issues.

    Checks for missing constraints, zero-mass particles, empty
    collidable lists, missing poses, out-of-range indices, and
    other problems that cause crashes or poor simulation.

    Exit codes: 0 = valid, 1 = warnings only, 2 = errors found,
    3 = load failure.

    Examples:

        modkit cloth validate outfit.nif

        modkit cloth validate outfit.nif --format json
    """
    from creation_lib._native import havok_native, nif_core_native

    try:
        blob = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
        result_json = havok_native.cloth_validate(blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(3)
        return

    result = json.loads(result_json)

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        result["nif_path"] = str(nif_path)
        output(result, fmt)
    else:
        status = "VALID" if result["valid"] else "INVALID"
        click.echo(f"{nif_path}: {status}")
        click.echo(f"  {result['errors']} error(s), "
                    f"{result['warnings']} warning(s), "
                    f"{result['info']} info(s)")
        for issue in result.get("issues", []):
            icon = {"error": "X", "warning": "!", "info": "-"}.get(issue.get("severity", "info"), "?")
            click.echo(f"  [{icon}] {issue.get('code', '?')}: {issue.get('message', '')}")

    if result["errors"]:
        ctx.exit(2)
    elif result["warnings"]:
        ctx.exit(1)


@cloth.group()
def template():
    """Cloth template commands — list, show, and apply vanilla-derived templates."""


@template.command("list")
@click.pass_context
def template_list(ctx: click.Context):
    """List all available cloth templates.

    Shows name, description, particle count, bone grid, and material
    for each built-in template.

    Examples:

        modkit cloth template list

        modkit cloth template list --format table
    """
    from creation_lib._native import havok_native as _hn
    from creation_lib._native import nif_core_native as _nif

    summaries = json.loads(_hn.cloth_template_list())
    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"

    if fmt in JSON_FORMATS:
        output(summaries, fmt)
    else:
        for s in summaries:
            click.echo(f"{s['name']:15s} {s['bone_grid']:5s}  {s['num_particles']:3d}p  "
                        f"{s['num_fixed']:2d} fixed  {s['num_capsules']} caps  "
                        f"mat={s['material_preset']:8s}  {s['description']}")


@template.command("show")
@click.argument("name")
@click.pass_context
def template_show(ctx: click.Context, name: str):
    """Show detailed information about a cloth template.

    Examples:

        modkit cloth template show Bathrobe

        modkit cloth template show Cape --format json
    """
    from creation_lib._native import havok_native as _hn

    try:
        summary = json.loads(_hn.cloth_template_get(name))
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    # Enrich summary with bone_names from grid config
    rows = summary.get("rows", 1)
    cols = summary.get("cols", 1)
    prefix = summary.get("bone_prefix", "Cloth_BN")
    row_labels = [chr(65 + i) for i in range(rows)]
    col_labels = [f"{i + 1:03d}" for i in range(cols)]
    summary["bone_names"] = [f"{prefix}_{r}_{c}" for r in row_labels for c in col_labels]
    summary["capsule_names"] = [cap["name"] for cap in summary.get("capsules", [])]
    # Estimate triangle count: 2 triangles per quad cell in grid
    summary.setdefault("num_triangles", 2 * (rows - 1) * (cols - 1))

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output(summary, fmt)
    else:
        click.echo(f"Template:     {summary['name']}")
        click.echo(f"Description:  {summary['description']}")
        click.echo(f"Material:     {summary['material_preset']}")
        click.echo(f"Parent bone:  {summary['parent_bone']}")
        click.echo(f"Bone grid:    {summary['bone_grid']}")
        click.echo(f"Particles:    {summary['num_particles']} ({summary['num_fixed']} fixed)")
        click.echo(f"Triangles:    {summary['num_triangles']}")
        click.echo(f"Capsules:     {', '.join(summary['capsule_names']) or 'none'}")
        click.echo(f"Stiffness:    std={summary['standard_link_stiffness']} "
                    f"stretch={summary['stretch_link_stiffness']} "
                    f"bend={summary['bend_stiffness']}")
        click.echo(f"Gravity:      {summary.get('gravity', [0, 0, -686.7, 0])}")
        click.echo(f"Damping:      {summary.get('global_damping', 0.1)}")
        click.echo(f"Bones:")
        for bn in summary["bone_names"]:
            click.echo(f"  {bn}")


@template.command("apply")
@click.argument("name")
@click.option("--into", "source_nif",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True,
              help="Source NIF to embed cloth data into.")
@click.option("-o", "--out",
              type=click.Path(dir_okay=False, path_type=Path),
              required=True,
              help="Destination NIF path.")
@click.option("--material", type=str, default=None,
              help="Override the template's default material preset.")
@click.pass_context
def template_apply(ctx: click.Context, name: str, source_nif: Path, out: Path,
                   material: str | None):
    """Apply a cloth template to a NIF mesh.

    Full apply flow: load template -> relax particles (30 XPBD steps,
    zero gravity) -> generate Cloth_BN_* bones -> auto-skin -> bake
    -> embed into NIF.

    Examples:

        modkit cloth template apply Bathrobe --into outfit.nif -o clothed.nif

        modkit cloth template apply Cape --into cloak.nif -o clothed.nif --material Silk
    """
    from creation_lib._native import havok_native as _hn

    args: dict = {}
    if material:
        args["material"] = material

    try:
        source_nif_bytes = source_nif.read_bytes()
        nif_bytes = bytes(_nif.cloth_template_apply(name, source_nif_bytes, json.dumps(args)))
    except (ValueError, KeyError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return
    except Exception as exc:
        click.echo(f"error: template apply failed: {exc}", err=True)
        ctx.exit(3)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(nif_bytes)

    tmpl_info = json.loads(_hn.cloth_template_get(name))
    mat_name = material or tmpl_info.get("material_preset", "")
    click.echo(f"Applied template '{name}' -> {out}")
    click.echo(f"  Particles: {tmpl_info['num_particles']}, "
               f"Bones: {tmpl_info['bone_rows']}x{tmpl_info['bone_cols']}, "
               f"Material: {mat_name}, "
               f"Size: {len(nif_bytes)} bytes")


# -------------------------------------------------------------------------
# Region commands
# -------------------------------------------------------------------------

@cloth.group()
def region():
    """Region-based cloth authoring — topology presets, generation."""


@region.command("topologies")
@click.pass_context
def region_topologies(ctx: click.Context):
    """List all available topology presets.

    Examples:

        modkit cloth region topologies

        modkit cloth region topologies --format table
    """
    from creation_lib._native import havok_native as _hn

    summaries = json.loads(_hn.cloth_topology_list())
    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"

    if fmt in JSON_FORMATS:
        output(summaries, fmt)
    else:
        for s in summaries:
            constraints = ", ".join(s["constraints"])
            click.echo(f"{s['name']:15s} mat={s['default_material']:10s}  "
                        f"constraints=[{constraints}]")
            click.echo(f"  {s['description']}")


@region.command("show")
@click.argument("name")
@click.pass_context
def region_show(ctx: click.Context, name: str):
    """Show detailed info about a topology preset.

    Examples:

        modkit cloth region show thin_cloth

        modkit cloth region show soft_body --format json
    """
    from creation_lib._native import havok_native as _hn

    try:
        summary = json.loads(_hn.cloth_topology_get(name))
    except (ValueError, KeyError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output(summary, fmt)
    else:
        click.echo(f"Topology:     {summary['name']}")
        click.echo(f"Description:  {summary.get('description', '')}")
        click.echo(f"Material:     {summary.get('default_material', '')}")
        click.echo(f"Constraints:  {', '.join(summary.get('constraints', []))}")
        stiffness = summary.get("stiffness", {})
        click.echo(f"Stiffness:    std={stiffness.get('standard_link', 0)} "
                    f"stretch={stiffness.get('stretch_link', 0)} "
                    f"bend={stiffness.get('bend', 0)}")
        sim = summary.get("simulation", {})
        click.echo(f"Substeps:     {sim.get('num_substeps', 0)}")
        click.echo(f"Iterations:   {sim.get('num_solve_iterations', 0)}")
        capsule_bones = summary.get("auto_capsule_bones", [])
        if capsule_bones:
            click.echo(f"Capsule bones: {', '.join(capsule_bones)}")


@region.command("generate")
@click.argument("setup_json", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--topology", "topology_name", type=str, default="thin_cloth",
              help="Topology preset name (thin_cloth, thick_cloth, chain, skirt_flaps, soft_body).")
@click.option("--material", type=str, default=None,
              help="Material preset override (default: topology's default).")
@click.option("--into", "source_nif",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True,
              help="Source NIF to embed cloth data into.")
@click.option("-o", "--out",
              type=click.Path(dir_okay=False, path_type=Path),
              required=True,
              help="Destination NIF path.")
@click.option("--parent-bone", type=str, default="COM",
              help="Parent bone for cloth bones.")
@click.option("--bone-rows", type=int, default=4, help="Bone grid rows.")
@click.option("--bone-cols", type=int, default=4, help="Bone grid columns.")
@click.pass_context
def region_generate(ctx: click.Context, setup_json: Path, topology_name: str,
                    material: str | None, source_nif: Path, out: Path,
                    parent_bone: str, bone_rows: int, bone_cols: int):
    """Generate cloth from a region setup JSON.

    Reads a region definition JSON (positions, triangles, fixed indices),
    applies a topology preset + material, generates bones, auto-skins,
    bakes, and embeds into a NIF.

    The input JSON should contain:
        {"positions": [[x,y,z,0],...], "triangles": [[i,j,k],...],
         "fixed_indices": [i,...], "name": "region_name"}

    Examples:

        modkit cloth region generate region.json --topology thin_cloth --into outfit.nif -o clothed.nif

        modkit cloth region generate region.json --topology soft_body --material Squishy --into bag.nif -o bag_cloth.nif
    """
    from creation_lib._native import havok_native as _hn

    # Load region definition
    try:
        json_text = setup_json.read_text(encoding="utf-8")
        region_def = json.loads(json_text)
    except Exception as exc:
        click.echo(f"error: failed to load region JSON: {exc}", err=True)
        ctx.exit(2)
        return

    region_name = region_def.get("name", "Region")
    positions = region_def.get("positions", [])
    triangles = region_def.get("triangles", [])
    fixed_indices = region_def.get("fixed_indices", [])

    if not positions or not triangles:
        click.echo("error: region JSON must contain non-empty 'positions' and 'triangles'", err=True)
        ctx.exit(2)
        return

    click.echo(f"Region '{region_name}': {len(positions)} particles, "
               f"{len(triangles)} triangles, {len(fixed_indices)} fixed")

    region_input = {
        "name": region_name,
        "positions": positions,
        "triangles": triangles,
        "fixed_indices": fixed_indices,
        "topology_name": topology_name,
        "parent_bone": parent_bone,
        "bone_rows": bone_rows,
        "bone_cols": bone_cols,
    }
    if material:
        region_input["material_name"] = material

    try:
        result = json.loads(_hn.cloth_region_generate(json.dumps(region_input)))
    except (ValueError, KeyError) as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(3)
        return
    except Exception as exc:
        click.echo(f"error: region generate failed: {exc}", err=True)
        ctx.exit(3)
        return

    setup_data = result["setup"]
    topo_name_used = result.get("topology_name", topology_name)
    mat_name_used = result.get("material_name", material or "")
    bone_names = result.get("bone_names", [])
    constraint_count = result.get("constraint_count", 0)

    click.echo(f"  Topology: {topo_name_used}, Material: {mat_name_used}")
    click.echo(f"  Bones: {len(bone_names)}, Constraints: {constraint_count}")

    # Bake via native
    try:
        blob = _hn.cloth_bake(json.dumps(setup_data))
    except Exception as exc:
        click.echo(f"error: bake failed: {exc}", err=True)
        ctx.exit(4)
        return

    # Embed blob into NIF via native
    try:
        from creation_lib._native import nif_core_native as _nif
        new_nif_bytes = bytes(_nif.cloth_pack_blob(source_nif.read_bytes(), blob))
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(5)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_nif_bytes)
    click.echo(f"Generated region cloth -> {out} ({len(new_nif_bytes)} bytes)")


@cloth.command()
@click.argument("nif_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), required=True,
              help="Output NIF path.")
@click.option("--mass", type=float, default=None, help="Set particle mass for all movable particles.")
@click.option("--radius", type=float, default=None, help="Set particle radius for all particles.")
@click.option("--friction", type=float, default=None, help="Set particle friction for all particles.")
@click.option("--stiffness-scale", type=float, default=None,
              help="Scale stiffness of all constraint links by this factor.")
@click.option("--stiffness-class", type=str, default=None,
              help="Only scale stiffness for this constraint class (standard/stretch/bend).")
@click.option("--gravity-z", type=float, default=None, help="Set gravity Z component.")
@click.option("--damping", type=float, default=None, help="Set global damping per second.")
@click.option("--collision-tolerance", type=float, default=None, help="Set collision tolerance.")
@click.option("--substeps", type=int, default=None, help="Set simulate operator substeps.")
@click.option("--iterations", type=int, default=None, help="Set solver iterations.")
@click.option("--capsule-radius-scale", type=float, default=None,
              help="Scale all capsule radii by this factor.")
@click.pass_context
def tweak(ctx: click.Context, nif_path: Path, out: Path, **kwargs):
    """Tweak cloth parameters in a NIF without re-baking.

    Modifies runtime graph fields in place and re-serializes.
    All options are independent — combine as needed.

    Examples:

        modkit cloth tweak outfit.nif -o tweaked.nif --mass 0.08 --stiffness-scale 0.5

        modkit cloth tweak outfit.nif -o tweaked.nif --gravity-z -400 --substeps 4

        modkit cloth tweak outfit.nif -o tweaked.nif --stiffness-scale 0.3 --stiffness-class bend
    """
    from creation_lib._native import havok_native, nif_core_native

    try:
        blob = nif_core_native.cloth_extract_blob(nif_path.read_bytes())
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    changes = []

    try:
        if kwargs["mass"] is not None:
            blob, n = havok_native.cloth_set_particle_mass_all(blob, float(kwargs["mass"]))
            changes.append(f"mass={kwargs['mass']:.4f} ({n} particles)")

        if kwargs["radius"] is not None:
            blob, n = havok_native.cloth_set_particle_radius_all(blob, float(kwargs["radius"]))
            changes.append(f"radius={kwargs['radius']:.4f} ({n} particles)")

        if kwargs["friction"] is not None:
            blob, n = havok_native.cloth_set_particle_friction_all(blob, float(kwargs["friction"]))
            changes.append(f"friction={kwargs['friction']:.4f} ({n} particles)")

        if kwargs["stiffness_scale"] is not None:
            cls = kwargs["stiffness_class"] or None
            blob, n = havok_native.cloth_scale_stiffness(blob, cls, float(kwargs["stiffness_scale"]))
            label = cls or "all"
            changes.append(f"stiffness x{kwargs['stiffness_scale']:.2f} ({label}, {n} links)")

        if kwargs["gravity_z"] is not None:
            blob = havok_native.cloth_set_gravity(blob, [0.0, 0.0, float(kwargs["gravity_z"]), 1.0])
            changes.append(f"gravity_z={kwargs['gravity_z']:.1f}")

        if kwargs["damping"] is not None:
            blob = havok_native.cloth_set_damping(blob, float(kwargs["damping"]))
            changes.append(f"damping={kwargs['damping']:.6f}")

        if kwargs["collision_tolerance"] is not None:
            blob = havok_native.cloth_set_collision_tolerance(blob, float(kwargs["collision_tolerance"]))
            changes.append(f"collision_tolerance={kwargs['collision_tolerance']:.2f}")

        if kwargs["substeps"] is not None:
            blob = havok_native.cloth_set_substeps(blob, int(kwargs["substeps"]))
            changes.append(f"substeps={kwargs['substeps']}")

        if kwargs["iterations"] is not None:
            blob = havok_native.cloth_set_solver_iterations(blob, int(kwargs["iterations"]))
            changes.append(f"iterations={kwargs['iterations']}")

        if kwargs["capsule_radius_scale"] is not None:
            blob, n = havok_native.cloth_scale_all_capsule_radii(blob, float(kwargs["capsule_radius_scale"]))
            changes.append(f"capsule_radius x{kwargs['capsule_radius_scale']:.2f} ({n} capsules)")

    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(3)
        return

    if not changes:
        click.echo("No changes specified — use options like --mass, --stiffness-scale, etc.")
        ctx.exit(1)
        return

    # Embed mutated blob back into NIF and write.
    try:
        new_nif_bytes = nif_core_native.cloth_pack_blob(nif_path.read_bytes(), blob)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(4)
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_nif_bytes)

    click.echo(f"Tweaked {nif_path.name} -> {out}")
    for c in changes:
        click.echo(f"  {c}")
