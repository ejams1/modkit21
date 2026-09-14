"""modkit nif — NIF mesh manipulation CLI commands."""

import json
import os
import sys
from pathlib import Path

import click

from cli._output import output
from cli._session import (
    open_session, load_session, save_session, close_session, cleanup_stale,
)

# Lazy imports to avoid slow startup
_nif_loaded = False


def _ensure_nif():
    global _nif_loaded
    if not _nif_loaded:
        _nif_loaded = True


def _error(msg: str) -> dict:
    return {"error": msg}


def _dds_header_dump(path: Path) -> dict:
    data = path.read_bytes()[:148]
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("Not a valid DDS file")

    def u32(offset):
        return int.from_bytes(data[offset : offset + 4], "little")

    four_cc = data[84:88].rstrip(b"\0").decode("ascii", errors="replace")
    dump = {
        "flags": u32(8),
        "height": u32(12),
        "width": u32(16),
        "pitch_or_linear_size": u32(20),
        "depth": u32(24),
        "mip_map_count": u32(28),
        "pixel_format_flags": u32(80),
        "four_cc": four_cc,
        "rgb_bit_count": u32(88),
        "red_mask": u32(92),
        "green_mask": u32(96),
        "blue_mask": u32(100),
        "alpha_mask": u32(104),
        "caps": u32(108),
        "caps2": u32(112),
    }
    if four_cc in {"DX10", "XBOX"} and len(data) >= 148:
        dump["dx10"] = {
            "dxgi_format": u32(128),
            "resource_dimension": u32(132),
            "misc_flag": u32(136),
            "array_size": u32(140),
            "misc_flags2": u32(144),
        }
    return dump


def _load_nif_or_error(session_id: str):
    """Load NIF from session, return (nif, original_path) or print error and exit."""
    try:
        return load_session(session_id)
    except FileNotFoundError:
        print(f"Error: No session '{session_id}'", file=sys.stderr)
        sys.exit(1)


@click.group()
@click.option("--format", "fmt", type=click.Choice(["json", "pretty", "compact", "table", "jsonl"]), default=None, help="Output format (overrides global --format).")
@click.pass_context
def nif(ctx, fmt):
    """Inspect and edit NIF mesh files."""
    _ensure_nif()
    cleanup_stale()
    if fmt is not None:
        ctx.obj["fmt"] = fmt


@nif.command("validate")
@click.argument("path")
@click.option(
    "--fix",
    is_flag=True,
    help="Apply safe game-aware fixes. Files are modified in place unless --output is used.",
)
@click.option(
    "--output",
    "output_path",
    default="",
    help="Output file or directory for fixed NIFs.",
)
@click.option(
    "--recursive/--no-recursive",
    default=True,
    show_default=True,
    help="Recurse when PATH is a directory.",
)
@click.option("--jobs", default=0, type=int, show_default=True, help="Parallel NIF jobs. 0 = auto.")
@click.option("--report", "report_path", default="", help="Write complete per-file JSON results.")
@click.option(
    "--include-optional",
    is_flag=True,
    help="Enable NIF checks that are off by default because they may report intentional content.",
)
@click.option(
    "--check",
    "checks",
    multiple=True,
    help="Run only the named NIF check ID. Repeat to select several checks.",
)
@click.option(
    "--shape",
    "shape_id",
    default=None,
    type=int,
    help="Validate weights on one shape in an open session.",
)
@click.pass_context
def validate_cmd(
    ctx,
    path,
    fix,
    output_path,
    recursive,
    jobs,
    report_path,
    include_optional,
    checks,
    shape_id,
):
    """Audit NIF, KF, and DDS files and optionally apply safe fixes.

    PATH may be one supported file or a directory. Validation never modifies
    files unless --fix is supplied. NIF/KF headers select the Morrowind,
    Oblivion, FO3/FNV, Skyrim, Skyrim SE, FO4, FO76, or Starfield rule set.
    DDS files are audited but not rewritten.
    """
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    from creation_lib.nif import native_runtime

    fmt = ctx.obj["fmt"]
    selected_checks = set(checks)
    if selected_checks:
        catalog = native_runtime.nif_features_raw()["checks"]
        known_checks = {check["id"]: check for check in catalog}
        unknown_checks = sorted(selected_checks - known_checks.keys())
        if unknown_checks:
            output(
                _error(f"Unknown NIF check ID(s): {', '.join(unknown_checks)}"), fmt
            )
            return
        include_optional = include_optional or any(
            known_checks[check]["optional"] for check in selected_checks
        )
    source = Path(path).resolve()
    if not source.exists():
        if fix or output_path or report_path:
            output(_error(f"Path not found: {source}"), fmt)
            return
        try:
            nif_file, _ = load_session(path)
        except FileNotFoundError:
            output(_error(f"Path or session not found: {path}"), fmt)
            return
        from cli._nif_skinning import validate_weights
        from creation_lib.nif.validation import validate_nif

        result = (
            validate_nif(nif_file)
            if shape_id is None
            else validate_weights(nif_file, shape_id=shape_id)
        )
        output(result, fmt)
        return
    if output_path and not fix:
        output(_error("--output requires --fix"), fmt)
        return

    supported_extensions = {".nif", ".kf", ".dds"}
    if source.is_file():
        if source.suffix.lower() not in supported_extensions:
            output(_error(f"Not a NIF, KF, or DDS file: {source}"), fmt)
            return
        files = [source]
        source_root = source.parent
    else:
        candidates = source.rglob("*") if recursive else source.glob("*")
        files = sorted(
            file
            for file in candidates
            if file.is_file() and file.suffix.lower() in supported_extensions
        )
        source_root = source
    if not files:
        output(_error(f"No NIF, KF, or DDS files found: {source}"), fmt)
        return

    destination = Path(output_path).resolve() if output_path else None

    def validate_one(file_path):
        target = None
        if fix:
            if destination is None:
                target = file_path
            elif source.is_file():
                target = destination / file_path.name if destination.is_dir() else destination
            else:
                target = destination / file_path.relative_to(source_root)
        try:
            if file_path.suffix.lower() == ".dds":
                from creation_lib.dds import native_runtime as dds_native_runtime

                report = dds_native_runtime.validate_dds_file_raw(
                    str(file_path), include_optional=include_optional
                )
                if target is not None and target != file_path:
                    import shutil

                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, target)
            elif include_optional:
                report = native_runtime.validate_nif_file_raw(
                    str(file_path),
                    str(target) if target is not None else None,
                    fix,
                    True,
                )
            else:
                report = native_runtime.validate_nif_file_raw(
                    str(file_path),
                    str(target) if target is not None else None,
                    fix,
                )
            if selected_checks:
                report["findings"] = [
                    finding
                    for finding in report["findings"]
                    if finding.get("check", finding["rule"]) in selected_checks
                ]
            return {
                "path": str(file_path),
                "output": str(target) if target is not None else "",
                "success": True,
                **report,
            }
        except Exception as exc:
            return {
                "path": str(file_path),
                "output": str(target) if target is not None else "",
                "success": False,
                "error": str(exc),
                "changed": False,
                "changes": [],
                "warnings": [],
                "findings": [],
            }

    job_count = jobs if jobs > 0 else min(8, max(1, (os.cpu_count() or 1) - 1))
    results = []
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        futures = [executor.submit(validate_one, file_path) for file_path in files]
        for completed, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if len(files) > 25 and (completed == len(files) or completed % 100 == 0):
                click.echo(f"nif validate: completed {completed}/{len(files)}", err=True)
    results.sort(key=lambda item: item["path"].lower())

    severities = Counter()
    rules = Counter()
    sample_findings = []
    for result in results:
        for finding in result["findings"]:
            severities[finding["severity"]] += 1
            rules[finding["rule"]] += 1
            if len(sample_findings) < 100:
                sample_findings.append({"path": result["path"], **finding})

    summary = {
        "path": str(source),
        "files": len(results),
        "valid": sum(
            1
            for result in results
            if result["success"]
            and not any(finding["severity"] == "error" for finding in result["findings"])
        ),
        "files_with_findings": sum(1 for result in results if result["findings"]),
        "fixed_files": sum(1 for result in results if result["changed"]),
        "changes": sum(len(result["changes"]) for result in results),
        "games": dict(
            sorted(
                Counter(
                    result.get("game", "unknown")
                    for result in results
                    if result["success"]
                ).items()
            )
        ),
        "findings": dict(sorted(severities.items())),
        "rules": dict(sorted(rules.items())),
        "failures": [
            {"path": result["path"], "error": result.get("error", "")}
            for result in results
            if not result["success"]
        ],
        "sample_findings": sample_findings,
        "report": str(Path(report_path).resolve()) if report_path else "",
        "jobs": job_count,
        "include_optional": include_optional,
        "checks": sorted(selected_checks),
    }
    if report_path:
        report_file = Path(report_path).resolve()
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(
            json.dumps({"summary": summary, "results": results}, indent=2),
            encoding="utf-8",
        )
    elif len(results) == 1:
        summary["result"] = results[0]
    output(summary, fmt)


@nif.command("features")
@click.option(
    "--category",
    type=click.Choice(["NIF", "Report", "Animation", "Collision", "Shader"], case_sensitive=False),
    default=None,
    help="Only show one NIF processor category.",
)
@click.option(
    "--parity",
    type=click.Choice(["complete", "partial", "missing"]),
    default=None,
    help="Only show processors with this parity state.",
)
@click.pass_context
def nif_features_cmd(ctx, category, parity):
    """List the registered NIF processor and validation-check contract."""
    from creation_lib.nif import native_runtime

    result = native_runtime.nif_features_raw()
    processors = result["processors"]
    if category:
        processors = [
            processor
            for processor in processors
            if processor["category"].lower() == category.lower()
        ]
    if parity:
        processors = [
            processor for processor in processors if processor["parity"] == parity
        ]
    result["processors"] = processors
    result["summary"] = {
        "processors": len(processors),
        "checks": len(result["checks"]),
        "parity": dict(
            sorted(
                {
                    state: sum(
                        processor["parity"] == state for processor in processors
                    )
                    for state in ("complete", "partial", "missing")
                }.items()
            )
        ),
        "commands": dict(
            sorted(
                {
                    command: sum(
                        processor.get("command") == command for processor in processors
                    )
                    for command in ("validate", "report", "process")
                }.items()
            )
        ),
    }
    output(result, ctx.obj["fmt"])


@nif.command("report")
@click.argument(
    "processor",
    type=click.Choice(
        [
            "analyze-mesh",
            "transform-information",
            "havok-information",
            "find-unwelded-vertices",
            "find-excessive-draw-calls",
            "find-uvs",
            "find-textures",
        ]
    ),
)
@click.argument("path")
@click.option("--recursive/--no-recursive", default=True, show_default=True)
@click.option("--cache-size", default=16, show_default=True, type=int)
@click.option("--per-shape", is_flag=True)
@click.option("--threshold/--no-threshold", default=True, show_default=True)
@click.option("--acmr", default=1.5, show_default=True, type=float)
@click.option("--atvr", default=1.5, show_default=True, type=float)
@click.option("--vertices", default=0, show_default=True, type=int)
@click.option("--translation/--no-translation", default=True, show_default=True)
@click.option("--rotation/--no-rotation", default=True, show_default=True)
@click.option("--scale/--no-scale", default=True, show_default=True)
@click.option("--skip-empty/--include-empty", default=True, show_default=True)
@click.option("--per-object/--summary-only", default=True, show_default=True)
@click.option("--field", "fields", multiple=True, help="Havok rigid-body field to include.")
@click.option("--distance", default=0.1, show_default=True, type=float)
@click.option("--skip-same", is_flag=True)
@click.option("--report-vertices", is_flag=True)
@click.option("--draw-call-threshold", default=10, show_default=True, type=int)
@click.option("--u-min", default=0.0, type=float, show_default=True)
@click.option("--u-max", default=None, type=float)
@click.option("--v-min", default=0.0, type=float, show_default=True)
@click.option("--v-max", default=None, type=float)
@click.option("--texture-format", "texture_formats", multiple=True)
@click.option(
    "--header-dump/--no-header-dump",
    default=False,
    show_default=True,
    help="Include the parsed DDS header in find-textures results.",
)
@click.option(
    "--resolution",
    type=click.Choice(
        [
            "not-power-of-two",
            "lt-128",
            "lt-256",
            "lt-512",
            "lt-1024",
            "lt-2048",
            "lt-4096",
            "gte-128",
            "gte-256",
            "gte-512",
            "gte-1024",
            "gte-2048",
            "gte-4096",
        ]
    ),
    default=None,
)
@click.option("--bits-per-pixel", default=None, type=int)
@click.option("--mipmaps", type=click.Choice(["yes", "no"]), default=None)
@click.option("--has-alpha", type=click.Choice(["yes", "no"]), default=None)
@click.option("--cubemap", type=click.Choice(["yes", "no"]), default=None)
@click.option("--compressed", type=click.Choice(["yes", "no"]), default=None)
@click.option("--dx10-supported", type=click.Choice(["yes", "no"]), default=None)
@click.option("--xbox", type=click.Choice(["yes", "no"]), default=None)
@click.option("--copy-to", default="", help="Copy matching textures under this directory.")
@click.pass_context
def nif_report_cmd(
    ctx,
    processor,
    path,
    recursive,
    cache_size,
    per_shape,
    threshold,
    acmr,
    atvr,
    vertices,
    translation,
    rotation,
    scale,
    skip_empty,
    per_object,
    fields,
    distance,
    skip_same,
    report_vertices,
    draw_call_threshold,
    u_min,
    u_max,
    v_min,
    v_max,
    texture_formats,
    header_dump,
    resolution,
    bits_per_pixel,
    mipmaps,
    has_alpha,
    cubemap,
    compressed,
    dx10_supported,
    xbox,
    copy_to,
):
    """Run one of NIF's read-only report processors."""
    from pathlib import Path

    source = Path(path).resolve()
    if not source.exists():
        output(_error(f"Path not found: {source}"), ctx.obj["fmt"])
        return
    extension = ".dds" if processor == "find-textures" else ".nif"
    if source.is_file():
        files = [source] if source.suffix.lower() == extension else []
    else:
        candidates = source.rglob("*") if recursive else source.glob("*")
        files = sorted(
            file
            for file in candidates
            if file.is_file() and file.suffix.lower() == extension
        )
    if not files:
        output(_error(f"No {extension} files found: {source}"), ctx.obj["fmt"])
        return
    if copy_to and processor != "find-textures":
        output(_error("--copy-to is only valid for find-textures"), ctx.obj["fmt"])
        return

    options = {
        "cache_size": cache_size,
        "per_shape": per_shape,
        "threshold": threshold,
        "acmr": acmr,
        "atvr": atvr,
        "vertices": vertices,
        "translation": translation,
        "rotation": rotation,
        "scale": scale,
        "skip_empty": skip_empty,
        "per_object": per_object,
        "fields": list(fields),
        "distance": distance,
        "skip_same": skip_same,
        "report_vertices": report_vertices,
        "draw_call_threshold": draw_call_threshold,
        "u_min": u_min,
        "u_max": u_max,
        "v_min": v_min,
        "v_max": v_max,
    }
    results = []
    examined = 0
    copy_root = Path(copy_to).resolve() if copy_to else None
    for file in files:
        examined += 1
        try:
            if processor == "find-textures":
                from creation_lib.dds import native_runtime as dds_native_runtime

                data = dds_native_runtime.texdiag_info(str(file))
                if data is None:
                    raise RuntimeError("directxtex_native.texdiag_info is not available")
                if header_dump:
                    data["header"] = _dds_header_dump(file)
                if texture_formats and not any(
                    selected.upper() in {
                        str(data["format"]).upper(),
                        str(data["dxgi_format"]),
                    }
                    for selected in texture_formats
                ):
                    continue
                max_resolution = max(data["width"], data["height"])
                if resolution == "not-power-of-two" and data["is_power_of_two"]:
                    continue
                if resolution and resolution.startswith("lt-"):
                    if max_resolution >= int(resolution[3:]):
                        continue
                if resolution and resolution.startswith("gte-"):
                    if max_resolution < int(resolution[4:]):
                        continue
                texture_bools = [
                    (mipmaps, data["mip_levels"] > 1),
                    (has_alpha, data["has_alpha"]),
                    (cubemap, data["is_cubemap"]),
                    (compressed, data["is_compressed"]),
                    (dx10_supported, data["dxgi_format"] != 0),
                    (xbox, data["is_xbox"]),
                ]
                if any(
                    selected is not None
                    and ((selected == "yes") != actual)
                    for selected, actual in texture_bools
                ):
                    continue
                if bits_per_pixel is not None and data["bits_per_pixel"] != bits_per_pixel:
                    continue
                copied_to = ""
                if copy_root is not None:
                    import shutil

                    relative = file.name if source.is_file() else file.relative_to(source)
                    target = copy_root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
                    copied_to = str(target)
                result = {
                    "processor": processor,
                    "path": str(file),
                    "game": "dds",
                    "data": data,
                    "copied_to": copied_to,
                }
            else:
                from creation_lib.nif import native_runtime

                native_options = dict(options)
                native_options["threshold"] = (
                    draw_call_threshold
                    if processor == "find-excessive-draw-calls"
                    else threshold
                )
                result = native_runtime.nif_report_raw(
                    str(file), processor, native_options
                )
            results.append({"success": True, **result})
        except Exception as exc:
            results.append(
                {
                    "success": False,
                    "processor": processor,
                    "path": str(file),
                    "error": str(exc),
                }
            )
    output(
        {
            "processor": processor,
            "path": str(source),
            "files": examined,
            "matched": len(results),
            "failures": sum(not result["success"] for result in results),
            "results": results,
        },
        ctx.obj["fmt"],
    )


@nif.command("process")
@click.argument(
    "processor",
    type=click.Choice(
        [
            "update-tangents",
            "optimize-mesh",
            "json-converter",
            "universal-tweaker",
            "universal-fixer",
            "update-bounds",
            "replace-assets",
            "remove-unused-nodes",
            "convert-block-type",
            "set-missing-names",
            "unskin-mesh",
            "update-shader-flags",
            "walls-reflection-flag",
            "soft-particles",
            "update-ragdoll-constraint",
            "update-havok-settings",
            "update-havok-inertia",
            "search-havok-material",
            "copy-controlled-blocks",
            "copy-priorities",
            "remove-controlled-blocks",
            "quadratic-to-linear",
            "fix-exported-kf",
            "optimize-animations",
            "add-transform-data",
            "add-headtracking-anim",
            "add-facial-anim",
            "weijiesen-blow-up",
            "add-skeleton-blocks",
            "update-mopp-code",
            "remove-nodes",
            "attach-parent",
            "adjust-transform",
            "copy-geometry-blocks",
            "merge-properties",
            "group-shapes",
            "vertex-paint",
            "merge-shapes",
            "apply-transform",
            "add-root-collision-node",
            "add-bounding-box",
            "add-lod-node",
        ]
    ),
)
@click.argument("path")
@click.option("--output", "output_path", default="", help="Output file or directory.")
@click.option("--in-place", is_flag=True, help="Modify input files in place.")
@click.option("--recursive/--no-recursive", default=True, show_default=True)
@click.option("--search", default=None, help="Text to find for replace-assets.")
@click.option("--replace", default=None, help="Replacement text for replace-assets.")
@click.option(
    "--replacement",
    "replacement_pairs",
    type=(str, str),
    multiple=True,
    metavar="SEARCH REPLACE",
    help="NIF replacement pair; repeat for multiple pairs.",
)
@click.option("--case-sensitive", is_flag=True)
@click.option("--regex", "use_regex", is_flag=True, help="Treat search strings as regex.")
@click.option("--fix-absolute", is_flag=True, help="Truncate absolute paths through Data\\.")
@click.option("--report-only", is_flag=True, help="Report replacements without saving.")
@click.option(
    "--json-direction",
    type=click.Choice(["to-json", "from-json"]),
    default=None,
    help="JSON Converter direction; inferred from each file extension when omitted.",
)
@click.option(
    "--default-extension",
    default="nif",
    show_default=True,
    help="Extension for from-json inputs whose pre-.json name has no extension.",
)
@click.option("--decimal-digits", type=click.IntRange(6, 16), default=8, show_default=True)
@click.option(
    "--rotation-output",
    type=click.Choice(["angle-axis", "euler", "matrix"]),
    default="angle-axis",
    show_default=True,
    help="Rotation text representation; matrix is a compatibility alias for angle-axis.",
)
@click.option(
    "--block",
    "tweak_blocks",
    multiple=True,
    help="Universal Tweaker block type or linked block path; repeat for several types.",
)
@click.option("--inherited", is_flag=True, help="Include descendants of --block types.")
@click.option("--field-path", default=None, help="Universal Tweaker field path.")
@click.option("--value", "tweak_value", default="", help="Universal Tweaker value.")
@click.option(
    "--value-mode",
    type=click.Choice(
        [
            "set",
            "add",
            "multiply",
            "replace",
            "prepend",
            "append",
            "and",
            "and-not",
            "or",
            "remove",
            "round",
            "multiply-round",
        ]
    ),
    default="set",
    show_default=True,
)
@click.option("--old-value-check", is_flag=True)
@click.option("--old-path", default="", help="Field checked before tweaking; defaults to --field-path.")
@click.option(
    "--old-mode",
    type=click.Choice(
        [
            "equal",
            "not-equal",
            "greater",
            "lesser",
            "contains",
            "doesnt-contain",
            "starts-with",
            "ends-with",
            "and",
            "and-not",
            "regex",
        ]
    ),
    default="equal",
    show_default=True,
)
@click.option("--old-value", default="")
@click.option("--add-if-missing", is_flag=True, help="Add tangent data when absent.")
@click.option("--face-normals", is_flag=True, help="Recalculate normals before tangents.")
@click.option("--triangulate", is_flag=True)
@click.option("--stripify", is_flag=True)
@click.option("--vertex-cache/--no-vertex-cache", default=True, show_default=True)
@click.option("--overdraw/--no-overdraw", default=True, show_default=True)
@click.option("--vertex-fetch/--no-vertex-fetch", default=True, show_default=True)
@click.option("--flags1", default=None, help="Shader Flags 1 mask (decimal or 0xhex).")
@click.option("--flags2", default=None, help="Shader Flags 2 mask (decimal or 0xhex).")
@click.option(
    "--flag-mode",
    type=click.Choice(["add", "set", "remove"]),
    default="add",
    show_default=True,
)
@click.option("--map-scale", default=0.8, type=float, show_default=True)
@click.option("--normal-intensity", default=None, type=float)
@click.option("--blend-intensity", default=None, type=float)
@click.option("--soft-scale", default=0.05, type=float, show_default=True)
@click.option("--convert-to-malleable", is_flag=True)
@click.option(
    "--setting",
    "havok_settings",
    multiple=True,
    metavar="NAME=VALUE",
    help="Havok setting; repeat for multiple fields. Enum names and numeric values are accepted.",
)
@click.option("--update-inertia/--no-update-inertia", default=True, show_default=True)
@click.option("--update-center/--no-update-center", default=True, show_default=True)
@click.option("--update-penetration", is_flag=True)
@click.option("--penetration-statics", is_flag=True)
@click.option("--depth-multiplier", type=float, default=0.2, show_default=True)
@click.option(
    "--body-part-mult",
    "body_part_multipliers",
    multiple=True,
    metavar="PART=MULTIPLIER",
)
@click.option(
    "--material-search",
    default=None,
    help="Havok material name or numeric value to find.",
)
@click.option(
    "--material-replace",
    default=None,
    help="Replacement Havok material name or numeric value.",
)
@click.option("--skip-root-collision", is_flag=True)
@click.option(
    "--source-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="NIF source directory containing matching relative-path files.",
)
@click.option(
    "--source-file",
    "copy_source_file",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Single source NIF used by copy-geometry-blocks.",
)
@click.option("--copy-geometry/--no-copy-geometry", default=True, show_default=True)
@click.option("--copy-transform", is_flag=True)
@click.option("--copy-shader", is_flag=True)
@click.option("--copy-texture-set", is_flag=True)
@click.option("--name", "controlled_names", multiple=True)
@click.option("--exact-match/--partial-match", default=None)
@click.option("--not-matching", is_flag=True)
@click.option(
    "--rotation-keys/--no-rotation-keys",
    "add_rotation",
    default=True,
    show_default=True,
)
@click.option(
    "--translation-keys/--no-translation-keys",
    "add_translation",
    default=True,
    show_default=True,
)
@click.option("--cycle-clamp-only", is_flag=True)
@click.option("--head-key-value-14", "key_value_14", type=float, default=0.0, show_default=True)
@click.option("--head-key-value-23", "key_value_23", type=float, default=100.0, show_default=True)
@click.option("--head-key-time-2", "key_time_2", type=float, default=20.0, show_default=True)
@click.option("--head-key-time-3", "key_time_3", type=float, default=80.0, show_default=True)
@click.option("--remove-existing-facial", is_flag=True)
@click.option(
    "--facial-mod",
    "facial_mods",
    multiple=True,
    metavar='"PRIORITY MODIFIER TIME VALUE [...]"',
    help="Facial animation row; repeat for each modifier.",
)
@click.option(
    "--no-facial-mods",
    is_flag=True,
    help="Do not use NIF's default facial animation rows.",
)
@click.option("--node-type", default=None, help="Remove blocks of this exact NIF type.")
@click.option(
    "--find-name",
    default="##SightingNode",
    show_default=True,
    help="Existing node name for attach-parent.",
)
@click.option(
    "--parent-name",
    default="##ISControl",
    show_default=True,
    help="New parent node name for attach-parent.",
)
@click.option(
    "--transform-mode",
    type=click.Choice(["add", "multiply", "set"]),
    default="add",
    show_default=True,
)
@click.option("--translate-x", type=float, default=None)
@click.option("--translate-y", type=float, default=None)
@click.option("--translate-z", type=float, default=None)
@click.option("--yaw", type=float, default=None, help="Yaw adjustment in degrees.")
@click.option("--pitch", type=float, default=None, help="Pitch adjustment in degrees.")
@click.option("--roll", type=float, default=None, help="Roll adjustment in degrees.")
@click.option("--scale", type=float, default=None)
@click.option(
    "--property-type",
    "property_types",
    multiple=True,
    help="Property block type to deduplicate; repeat for several types.",
)
@click.option(
    "--ignore-name/--compare-name",
    default=True,
    show_default=True,
    help="Ignore Name when comparing properties.",
)
@click.option("--split", is_flag=True, help="Split groups at the 16-bit vertex/triangle limit.")
@click.option(
    "--all-features",
    is_flag=True,
    help="Use every texture slot when grouping shapes.",
)
@click.option(
    "--paint-mode",
    type=click.Choice(["set", "adjust", "remove", "replace"]),
    default="set",
    show_default=True,
)
@click.option("--shape-name", default="", help="Only paint shapes containing this name.")
@click.option("--color", default="FFFFFFFF", show_default=True, help="RRGGBBAA color.")
@click.option("--replacement-color", default="FFFFFFFF", show_default=True)
@click.option("--skip-color", default=None, help="RRGGBBAA color to leave unchanged.")
@click.option("--all-white", is_flag=True, help="Only remove colors when all match --color.")
@click.option(
    "--adjust-mode",
    type=click.Choice(["multiply", "add"]),
    default="multiply",
    show_default=True,
)
@click.option("--adjust-h", type=float, default=None)
@click.option("--adjust-s", type=float, default=None)
@click.option("--adjust-l", type=float, default=None)
@click.option("--adjust-a", type=float, default=None)
@click.option("--apply-skinned", is_flag=True, help="Also bake skinned nodes and bones.")
@click.option("--apply-animated", is_flag=True, help="Also bake animated nodes.")
@click.option("--apply-collision", is_flag=True, help="Also bake nodes with collision.")
@click.option("--apply-root", is_flag=True, help="Also bake a skinned mesh root.")
@click.option(
    "--apply-controller-manager",
    is_flag=True,
    help="Also bake meshes containing NiControllerManager.",
)
@click.option("--bounding-flags", default=12, type=int, show_default=True)
@click.option("--center", type=(float, float, float), default=None)
@click.option("--extent", type=(float, float, float), default=None)
@click.option(
    "--lod-data",
    type=click.Choice(["range", "screen"]),
    default="range",
    show_default=True,
)
@click.option("--lod-extent", "lod_extents", multiple=True, type=float)
@click.option("--lod-proportion", "lod_proportions", multiple=True, type=float)
@click.option("--single-root/--multiple-roots", default=True, show_default=True)
@click.option("--from", "from_type", default=None, help="Source NIF block type.")
@click.option("--to", "to_type", default=None, help="Destination NIF block type.")
@click.option("--root-only", is_flag=True)
@click.option("--rename-root/--keep-root-name", default=True, show_default=True)
@click.pass_context
def nif_process_cmd(
    ctx,
    processor,
    path,
    output_path,
    in_place,
    recursive,
    search,
    replace,
    replacement_pairs,
    case_sensitive,
    use_regex,
    fix_absolute,
    report_only,
    json_direction,
    default_extension,
    decimal_digits,
    rotation_output,
    tweak_blocks,
    inherited,
    field_path,
    tweak_value,
    value_mode,
    old_value_check,
    old_path,
    old_mode,
    old_value,
    add_if_missing,
    face_normals,
    triangulate,
    stripify,
    vertex_cache,
    overdraw,
    vertex_fetch,
    flags1,
    flags2,
    flag_mode,
    map_scale,
    normal_intensity,
    blend_intensity,
    soft_scale,
    convert_to_malleable,
    havok_settings,
    update_inertia,
    update_center,
    update_penetration,
    penetration_statics,
    depth_multiplier,
    body_part_multipliers,
    material_search,
    material_replace,
    skip_root_collision,
    source_dir,
    copy_source_file,
    copy_geometry,
    copy_transform,
    copy_shader,
    copy_texture_set,
    controlled_names,
    exact_match,
    not_matching,
    add_rotation,
    add_translation,
    cycle_clamp_only,
    key_value_14,
    key_value_23,
    key_time_2,
    key_time_3,
    remove_existing_facial,
    facial_mods,
    no_facial_mods,
    node_type,
    find_name,
    parent_name,
    transform_mode,
    translate_x,
    translate_y,
    translate_z,
    yaw,
    pitch,
    roll,
    scale,
    property_types,
    ignore_name,
    split,
    all_features,
    paint_mode,
    shape_name,
    color,
    replacement_color,
    skip_color,
    all_white,
    adjust_mode,
    adjust_h,
    adjust_s,
    adjust_l,
    adjust_a,
    apply_skinned,
    apply_animated,
    apply_collision,
    apply_root,
    apply_controller_manager,
    bounding_flags,
    center,
    extent,
    lod_data,
    lod_extents,
    lod_proportions,
    single_root,
    from_type,
    to_type,
    root_only,
    rename_root,
):
    """Run a NIF-compatible mutating processor on NIF files."""
    from pathlib import Path

    if processor == "search-havok-material" and material_replace is None:
        report_only = True
    default_names = {
        "quadratic-to-linear": ("Head", "Neck"),
        "remove-controlled-blocks": ("Head", "Neck"),
        "add-skeleton-blocks": ("Weapon", "HeadAnims"),
        "merge-shapes": (".dds",),
    }
    if not controlled_names:
        controlled_names = default_names.get(processor, ())
    if exact_match is None:
        exact_match = processor != "merge-shapes"
    if processor == "add-facial-anim" and not facial_mods and not no_facial_mods:
        facial_mods = (
            "99 Aah 0.466667 0 1.499999 1",
            "99 Eh 1.500000 1",
            "99 BigAah 1.5 1 1.833333 0.1 3.333333 0.5 4.033333 1",
        )
    if processor == "json-converter" and in_place:
        output(_error("json-converter requires --output"), ctx.obj["fmt"])
        return
    if report_only and processor not in {
        "replace-assets",
        "universal-tweaker",
        "update-shader-flags",
        "search-havok-material",
    }:
        output(
            _error(
                "--report-only is not valid for this processor"
            ),
            ctx.obj["fmt"],
        )
        return
    if not report_only and in_place == bool(output_path):
        output(_error("Select exactly one of --output or --in-place"), ctx.obj["fmt"])
        return
    if report_only and (in_place or output_path):
        output(_error("--report-only does not accept --output or --in-place"), ctx.obj["fmt"])
        return
    source = Path(path).resolve()
    if not source.exists():
        output(_error(f"Path not found: {source}"), ctx.obj["fmt"])
        return
    extensions = {
        "replace-assets": {".nif", ".bgsm", ".bgem"},
        "remove-unused-nodes": {".nif", ".kf", ".kfm"},
        "remove-controlled-blocks": {".nif", ".kf"},
        "quadratic-to-linear": {".kf"},
        "fix-exported-kf": {".kf"},
        "optimize-animations": {".nif", ".kf"},
        "add-transform-data": {".kf"},
        "add-headtracking-anim": {".kf"},
        "add-facial-anim": {".kf"},
        "copy-controlled-blocks": {".kf"},
        "copy-priorities": {".kf"},
        "add-skeleton-blocks": {".kf"},
        "remove-nodes": {".nif", ".kf"},
        "adjust-transform": {".nif", ".kf"},
        "universal-tweaker": {".nif", ".kf", ".bgsm", ".bgem"},
    }.get(processor, {".nif"})
    if processor == "json-converter":
        extensions = {
            "to-json": {".nif", ".kf"},
            "from-json": {".json"},
            None: {".nif", ".kf", ".json"},
        }[json_direction]
    if source.is_file():
        files = [source] if source.suffix.lower() in extensions else []
        source_root = source.parent
    else:
        candidates = source.rglob("*") if recursive else source.glob("*")
        files = sorted(
            file
            for file in candidates
            if file.is_file() and file.suffix.lower() in extensions
        )
        source_root = source
    if not files:
        expected = ", ".join(sorted(extensions))
        output(_error(f"No {expected} files found: {source}"), ctx.obj["fmt"])
        return
    if processor in {"copy-controlled-blocks", "copy-priorities"}:
        if source_dir is None:
            output(_error("--source-dir is required for this processor"), ctx.obj["fmt"])
            return
    if processor == "copy-geometry-blocks":
        if (source_dir is None) == (copy_source_file is None):
            output(
                _error("Select exactly one of --source-dir or --source-file"),
                ctx.obj["fmt"],
            )
            return
        if source_dir is not None:
            source_dir = source_dir.resolve()
            if not source_dir.is_dir():
                output(_error(f"Source directory not found: {source_dir}"), ctx.obj["fmt"])
                return
        else:
            copy_source_file = copy_source_file.resolve()
            if not copy_source_file.is_file():
                output(_error(f"Source file not found: {copy_source_file}"), ctx.obj["fmt"])
                return

    destination = Path(output_path).resolve() if output_path else None
    try:
        flags1_value = int(flags1, 0) if flags1 is not None else None
        flags2_value = int(flags2, 0) if flags2 is not None else None
    except ValueError as exc:
        output(_error(f"Invalid numeric option: {exc}"), ctx.obj["fmt"])
        return

    def numeric_or_symbolic(value):
        if value is None:
            return None
        try:
            return int(value, 0)
        except ValueError:
            return value.strip()

    material_search_value = numeric_or_symbolic(material_search)
    material_replace_value = numeric_or_symbolic(material_replace)
    settings = {}
    for setting in havok_settings:
        if "=" not in setting:
            output(_error(f"Invalid --setting {setting!r}; expected NAME=VALUE"), ctx.obj["fmt"])
            return
        name, value = setting.split("=", 1)
        name = name.strip().lower().replace("-", "_").replace(" ", "_")
        try:
            settings[name] = float(value) if any(c in value.lower() for c in ".e") else int(value, 0)
        except ValueError:
            settings[name] = value.strip()
    inertia_multipliers = None
    if body_part_multipliers:
        inertia_multipliers = {}
        for value in body_part_multipliers:
            if "=" not in value:
                output(_error(f"Invalid --body-part-mult {value!r}; expected PART=MULTIPLIER"), ctx.obj["fmt"])
                return
            part, multiplier = value.split("=", 1)
            try:
                inertia_multipliers[str(int(part, 0))] = float(multiplier)
            except ValueError:
                output(_error(f"Invalid --body-part-mult {value!r}"), ctx.obj["fmt"])
                return
    options = {
        "search": search,
        "replace": replace,
        "pairs": [list(pair) for pair in replacement_pairs],
        "case_sensitive": case_sensitive,
        "regex": use_regex,
        "fix_absolute": fix_absolute,
        "report_only": report_only,
        "json_direction": json_direction,
        "default_extension": default_extension.lstrip("."),
        "decimal_digits": decimal_digits,
        "rotation_output": rotation_output,
        "blocks": list(tweak_blocks),
        "inherited": inherited,
        "field_path": field_path,
        "value": tweak_value,
        "value_mode": value_mode,
        "old_value_check": old_value_check,
        "old_path": old_path,
        "old_mode": old_mode,
        "old_value": old_value,
        "add_if_missing": add_if_missing,
        "face_normals": face_normals,
        "triangulate": triangulate,
        "stripify": stripify,
        "vertex_cache": vertex_cache,
        "overdraw": overdraw,
        "vertex_fetch": vertex_fetch,
        "flags1": flags1_value,
        "flags2": flags2_value,
        "mode": flag_mode,
        "map_scale": map_scale,
        "normal_intensity": normal_intensity,
        "blend_intensity": blend_intensity,
        "soft_scale": soft_scale,
        "convert_to_malleable": convert_to_malleable,
        "settings": settings,
        "update_inertia": update_inertia,
        "update_center": update_center,
        "update_penetration": update_penetration,
        "penetration_statics": penetration_statics,
        "depth_multiplier": depth_multiplier,
        "body_part_multipliers": inertia_multipliers,
        "material_search": material_search_value,
        "material_replace": material_replace_value,
        "skip_root": skip_root_collision,
        "copy_geometry": copy_geometry,
        "copy_transform": copy_transform,
        "copy_shader": copy_shader,
        "copy_texture_set": copy_texture_set,
        "names": list(controlled_names),
        "exact_match": exact_match,
        "not_matching": not_matching,
        "add_rotation": add_rotation,
        "add_translation": add_translation,
        "cycle_clamp_only": cycle_clamp_only,
        "key_value_14": key_value_14,
        "key_value_23": key_value_23,
        "key_time_2": key_time_2,
        "key_time_3": key_time_3,
        "remove_existing_facial": remove_existing_facial,
        "facial_mods": list(facial_mods) if facial_mods or no_facial_mods else None,
        "node_type": node_type,
        "find_name": find_name,
        "parent_name": parent_name,
        "transform_mode": transform_mode,
        "translate_x": translate_x,
        "translate_y": translate_y,
        "translate_z": translate_z,
        "yaw": yaw,
        "pitch": pitch,
        "roll": roll,
        "scale": scale,
        "property_types": list(property_types) if property_types else None,
        "ignore_name": ignore_name,
        "split": split,
        "all_features": all_features,
        "paint_mode": paint_mode,
        "shape_name": shape_name,
        "color": color,
        "replacement_color": replacement_color,
        "skip_color": skip_color,
        "skip_color_enabled": skip_color is not None,
        "all_white": all_white,
        "adjust_mode": adjust_mode,
        "adjust_h": adjust_h,
        "adjust_s": adjust_s,
        "adjust_l": adjust_l,
        "adjust_a": adjust_a,
        "apply_skinned": apply_skinned,
        "apply_animated": apply_animated,
        "apply_collision": apply_collision,
        "apply_root": apply_root,
        "apply_controller_manager": apply_controller_manager,
        "bounding_flags": bounding_flags,
        "center": list(center) if center else None,
        "extent": list(extent) if extent else None,
        "lod_data": lod_data,
        "extents": list(lod_extents) if lod_extents else None,
        "proportions": list(lod_proportions) if lod_proportions else None,
        "single_root": single_root,
        "from": from_type,
        "to": to_type,
        "root_only": root_only,
        "rename_root": rename_root,
    }
    results = []
    from creation_lib.nif import native_runtime

    for file in files:
        if report_only or in_place:
            target = file
        elif source.is_file():
            target = destination / file.name if destination.is_dir() else destination
        else:
            target = destination / file.relative_to(source_root)
        if processor == "json-converter":
            direction = json_direction or (
                "from-json" if file.suffix.lower() == ".json" else "to-json"
            )
            if direction == "to-json":
                target_name = file.name + ".json"
            else:
                target_name = file.stem
                if not Path(target_name).suffix:
                    target_name += "." + default_extension.lstrip(".")
            if source.is_dir():
                target = destination / file.relative_to(source_root).parent / target_name
            elif destination.is_dir():
                target = destination / target_name
        file_options = dict(options)
        if processor in {"copy-controlled-blocks", "copy-priorities"}:
            relative = file.name if source.is_file() else file.relative_to(source_root)
            file_options["source_file"] = str(source_dir / relative)
        elif processor == "copy-geometry-blocks":
            if copy_source_file is not None:
                file_options["source_file"] = str(copy_source_file)
            else:
                relative = file.name if source.is_file() else file.relative_to(source_root)
                file_options["source_file"] = str(source_dir / relative)
        try:
            result = native_runtime.nif_process_raw(
                str(file), str(target), processor, file_options
            )
            results.append({"success": True, **result})
        except Exception as exc:
            results.append(
                {
                    "success": False,
                    "processor": processor,
                    "path": str(file),
                    "output": str(target),
                    "error": str(exc),
                    "changed": False,
                    "changes": [],
                }
            )
    output(
        {
            "processor": processor,
            "path": str(source),
            "files": len(results),
            "changed": sum(result["changed"] for result in results),
            "failures": sum(not result["success"] for result in results),
            "results": results,
        },
        ctx.obj["fmt"],
    )


@nif.command("open")
@click.argument("path")
@click.pass_context
def open_cmd(ctx, path):
    """Open a NIF file for editing. Returns session_id.

    Examples:

      modkit nif open meshes/weapon.nif
    """
    from creation_lib.nif.nif_file import NifFile

    fmt = ctx.obj["fmt"]
    path = os.path.normpath(os.path.abspath(path))
    if not os.path.isfile(path):
        output(_error(f"File not found: {path}"), fmt)
        return

    try:
        nif_file = NifFile.load(path)
        sid = open_session(nif_file, path)
        h = nif_file.header
        root = nif_file.blocks[0] if nif_file.blocks else None
        type_counts: dict[str, int] = {}
        for b in nif_file.blocks:
            type_counts[b.type_name] = type_counts.get(b.type_name, 0) + 1
        output({
            "session_id": sid,
            "version": f"{h.version[0]}.{h.version[1]}.{h.version[2]}.{h.version[3]}",
            "bs_version": h.bs_version,
            "block_count": len(nif_file.blocks),
            "root": {
                "id": root.block_id if root else -1,
                "type": root.type_name if root else "",
                "name": root.get_field("Name") or "" if root else "",
            },
            "block_types": type_counts,
        }, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("new")
@click.option("--game", default="FO4", help="Target game for NIF header (default FO4)")
@click.argument("path")
@click.pass_context
def new_cmd(ctx, game, path):
    """Create a new empty NIF file. Returns session_id.

    Examples:

      modkit nif new meshes/my_weapon.nif
    """
    from creation_lib.nif.nif_file import NifFile

    fmt = ctx.obj["fmt"]
    try:
        nif_file = NifFile.new(game)
        sid = open_session(nif_file, os.path.normpath(os.path.abspath(path)))
        output({
            "session_id": sid,
            "game": game,
            "root_block_id": 0,
            "path": path,
        }, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("save")
@click.argument("session_id")
@click.option("--path", default="", help="Save to this path instead of original")
@click.pass_context
def save_cmd(ctx, session_id, path):
    """Save a NIF session to disk.

    Examples:

      modkit nif save abc123

      modkit nif save abc123 --path output/weapon.nif
    """
    fmt = ctx.obj["fmt"]
    nif_file, original_path = _load_nif_or_error(session_id)
    try:
        save_path = path or original_path
        if not save_path:
            output(_error("No path specified and no original path in session"), fmt)
            return
        save_path = os.path.normpath(os.path.abspath(save_path))
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        nif_file.save(save_path)
        output({"saved": save_path}, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("close")
@click.argument("session_id")
@click.pass_context
def close_cmd(ctx, session_id):
    """Close a NIF session and free resources.

    Examples:

      modkit nif close abc123
    """
    fmt = ctx.obj["fmt"]
    if close_session(session_id):
        output({"closed": session_id}, fmt)
    else:
        output(_error(f"No session '{session_id}'"), fmt)


@nif.command()
@click.option("--path", default="", help="NIF file path (stateless, no session needed)")
@click.option("--session", "session_id", default="", help="Session ID (for open sessions)")
@click.option("--block", "block_id", default=-1, type=int, help="Block ID (-1 for hierarchy tree)")
@click.option("--format", "fmt", type=click.Choice(["json", "pretty", "compact", "table"]), default=None, help="Output format (overrides global --format).")
@click.pass_context
def inspect(ctx, path, session_id, block_id, fmt):
    """Inspect a NIF file or specific block.

    Use --path for read-only inspection (no session needed).
    Use --session for inspecting during an edit session.

    Examples:

      modkit nif inspect --path meshes/weapon.nif

      modkit nif inspect --path meshes/weapon.nif --block 0

      modkit nif inspect --session abc123 --block 5
    """
    from creation_lib.nif.nif_file import NifFile
    from creation_lib.nif.types import to_json

    effective_fmt = fmt if fmt is not None else ctx.obj["fmt"]
    try:
        if path:
            p = os.path.normpath(os.path.abspath(path))
            if not os.path.isfile(p):
                output(_error(f"File not found: {p}"), effective_fmt)
                return
            nif_file = NifFile.load(p)
        elif session_id:
            nif_file, _ = _load_nif_or_error(session_id)
        else:
            output(_error("Provide either --path (stateless) or --session (open session)"), effective_fmt)
            return

        result = _inspect_nif(nif_file, block_id)
        output(result, effective_fmt)
    except Exception as e:
        output(_error(str(e)), effective_fmt)


def _inspect_nif(nif_file, block_id: int) -> dict:
    """Core inspect logic."""
    from creation_lib.nif.types import to_json

    if block_id == -1:
        return nif_file.get_hierarchy()

    block = nif_file.get_block(block_id)
    if block is None:
        return _error(f"Block {block_id} not found (0-{len(nif_file.blocks)-1})")

    from creation_lib.nif.schema import build_field_def_map
    schema = nif_file.schema
    field_defs = build_field_def_map(schema, block.type_name)

    display_fields = {}
    for name, val in block.fields:
        display_name = name.split(":")[0] if ":" in name else name
        fdef = field_defs.get(name)
        if fdef and fdef.type in schema.enums and isinstance(val, int):
            enum_def = schema.enums[fdef.type]
            enum_name = next((o.name for o in enum_def.options if o.value == val), str(val))
            display_fields[display_name] = enum_name
        elif fdef and fdef.type in schema.bitflags and isinstance(val, int):
            bf_def = schema.bitflags[fdef.type]
            flags = [o.name for o in bf_def.options if val & (1 << o.value)]
            display_fields[display_name] = flags
        else:
            display_fields[display_name] = to_json(val)

    return {"block_id": block_id, "type": block.type_name, "fields": display_fields}


@nif.command()
@click.argument("session_id")
@click.argument("block_id", type=int)
@click.argument("fields_json")
@click.pass_context
def modify(ctx, session_id, block_id, fields_json):
    """Update fields on an existing block.

    FIELDS_JSON: JSON dict of field_name -> new_value.

    Examples:

      modkit nif modify abc123 0 '{"Name": "MyWeapon"}'
    """
    fmt = ctx.obj["fmt"]
    nif_file, _ = _load_nif_or_error(session_id)
    try:
        fields = json.loads(fields_json)
        block = nif_file.get_block(block_id)
        if block is None:
            output(_error(f"Block {block_id} not found"), fmt)
            return
        for name, val in fields.items():
            block.set_field(name, val)
        save_session(session_id, nif_file)
        output({"block_id": block_id, "updated_fields": list(fields.keys())}, fmt)
    except json.JSONDecodeError as e:
        output(_error(f"Invalid JSON: {e}"), fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("source_session")
@click.argument("block_ids")
@click.argument("target_session")
@click.option("--attach-to", default=-1, type=int, help="Attach copied blocks as children of this block")
@click.pass_context
def copy(ctx, source_session, block_ids, target_session, attach_to):
    """Copy blocks (with dependency trees) between NIFs.

    BLOCK_IDS: comma-separated block IDs (e.g. "3,5,7").

    Handles Ref/Ptr remapping automatically. Copies BSTriShapes with their
    materials, textures, skin data, and animation controllers.

    Examples:

      modkit nif copy src123 "3,5" tgt456

      modkit nif copy src123 "3" tgt456 --attach-to 0
    """
    from creation_lib.nif.operations.copy import copy_blocks as ops_copy_blocks

    fmt = ctx.obj["fmt"]
    src_nif, _ = _load_nif_or_error(source_session)
    tgt_nif, _ = _load_nif_or_error(target_session)
    try:
        ids = [int(x.strip()) for x in block_ids.split(",")]
        id_map = ops_copy_blocks(
            src_nif, ids, tgt_nif,
            attach_to=attach_to if attach_to >= 0 else None,
        )
        save_session(target_session, tgt_nif)
        output({"id_map": {str(k): v for k, v in id_map.items()}}, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("session_id")
@click.argument("type_name")
@click.option("--fields-json", default="{}", help="Initial field values as JSON")
@click.option("--attach-to", default=-1, type=int, help="Add as child of this block")
@click.pass_context
def add(ctx, session_id, type_name, fields_json, attach_to):
    """Create a new block.

    Examples:

      modkit nif add abc123 NiNode

      modkit nif add abc123 BSTriShape --attach-to 0
    """
    fmt = ctx.obj["fmt"]
    nif_file, _ = _load_nif_or_error(session_id)
    try:
        fields = json.loads(fields_json) if fields_json != "{}" else None
        block = nif_file.add_block(type_name, fields)
        if attach_to >= 0:
            parent = nif_file.get_block(attach_to)
            if parent:
                children = parent.get_field("Children")
                if isinstance(children, list):
                    children.append(block.block_id)
                    parent.set_field("Children", children)
                num = parent.get_field("Num Children")
                if isinstance(num, int):
                    parent.set_field("Num Children", num + 1)
        save_session(session_id, nif_file)
        output({"block_id": block.block_id, "type": type_name, "fields": block.to_json()}, fmt)
    except json.JSONDecodeError as e:
        output(_error(f"Invalid JSON: {e}"), fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("session_id")
@click.argument("block_ids")
@click.pass_context
def remove(ctx, session_id, block_ids):
    """Remove blocks from a NIF. Updates all Ref/Ptr indices.

    BLOCK_IDS: comma-separated block IDs (e.g. "3,5,7").

    Examples:

      modkit nif remove abc123 "3,5"
    """
    fmt = ctx.obj["fmt"]
    nif_file, _ = _load_nif_or_error(session_id)
    try:
        ids = [int(x.strip()) for x in block_ids.split(",")]
        before = len(nif_file.blocks)
        nif_file.remove_blocks(ids)
        after = len(nif_file.blocks)
        save_session(session_id, nif_file)
        output({"removed": before - after}, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("session_id")
@click.option("--node", "node_block_id", default=0, type=int, help="Node block ID (default 0)")
@click.option(
    "--shape-type", default="convex_hull",
    type=click.Choice([
        "convex_hull", "convex_fit", "box", "sphere", "capsule", "cylinder",
        "list", "auto", "mesh", "compressed_mesh", "auto_compressed_mesh",
    ]),
    help="Collision shape type",
)
@click.option("--source-blocks", default="", help="Comma-separated source block IDs (auto-detect if empty)")
@click.option("--layer", default="STATIC", help="Collision layer")
@click.option("--mass", default=0.0, type=float)
@click.option("--friction", default=0.5, type=float)
@click.option("--restitution", default=0.4, type=float)
@click.option("--radius", default=0.05, type=float)
@click.option("--replace/--no-replace", default=True, help="Replace existing collision")
@click.pass_context
def collision(ctx, session_id, node_block_id, shape_type, source_blocks, layer,
              mass, friction, restitution, radius, replace):
    """Generate collision hierarchy on a node.

    Shape types:
      convex_hull, convex_fit, box, sphere, capsule, cylinder — single convex shape
      list, auto — compound (one hull per mesh component)
      mesh, compressed_mesh — full triangle mesh (FO4: hknpCompressedMeshShape)
      auto_compressed_mesh — simplified triangle mesh capped for compressed mesh

    Examples:

      modkit nif collision abc123

      modkit nif collision abc123 --shape-type box --layer STATIC

      modkit nif collision abc123 --shape-type auto_compressed_mesh --layer CLUTTER

      modkit nif collision abc123 --shape-type compressed_mesh --layer CLUTTER
    """
    from creation_lib.nif.operations.collision import generate_collision as gen_coll
    from creation_lib.core.game_profiles import detect_game

    fmt = ctx.obj["fmt"]
    nif_file, _ = _load_nif_or_error(session_id)
    try:
        source_block_ids = None
        if source_blocks:
            source_block_ids = [int(x.strip()) for x in source_blocks.split(",")]

        profile = getattr(nif_file, 'detected_game', None)
        if profile is None and hasattr(nif_file, 'header'):
            profile = detect_game(nif_file.header.bs_version)

        result = gen_coll(
            nif_file, node_block_id, shape_type=shape_type,
            source_block_ids=source_block_ids, layer=layer,
            mass=mass, friction=friction, restitution=restitution,
            radius=radius, replace=replace, profile=profile,
        )
        save_session(session_id, nif_file)
        output({
            "success": result.success,
            "description": result.description,
            "modified_block_ids": result.modified_block_ids,
            "warnings": result.warnings,
        }, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("rm-collision")
@click.argument("session_id")
@click.option("--node", "node_block_id", default=0, type=int, help="Node block ID (default 0)")
@click.pass_context
def rm_collision(ctx, session_id, node_block_id):
    """Remove collision subtree from a node.

    Examples:

      modkit nif rm-collision abc123
    """
    from creation_lib.nif.operations.collision import remove_collision as rem_coll

    fmt = ctx.obj["fmt"]
    nif_file, _ = _load_nif_or_error(session_id)
    try:
        result = rem_coll(nif_file, node_block_id)
        save_session(session_id, nif_file)
        output({
            "success": result.success,
            "description": result.description,
            "warnings": result.warnings,
        }, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("strip-all-collision")
@click.argument("path")
@click.option("--output", "output_path", default="", help="Output path. Defaults to overwriting PATH.")
@click.pass_context
def strip_all_collision(ctx, path, output_path):
    """Remove every collision subtree from a NIF file."""
    from creation_lib.nif.nif_file import NifFile

    fmt = ctx.obj["fmt"]
    src = os.path.normpath(os.path.abspath(path))
    dst = os.path.normpath(os.path.abspath(output_path or path))
    if not os.path.isfile(src):
        output(_error(f"File not found: {src}"), fmt)
        return

    try:
        nif_file = NifFile.load(src)
        before = len(nif_file.blocks)
        removed = _strip_all_collision_subtrees(nif_file)
        bsx_cleared = _clear_havok_bsx_flag_if_no_collision(nif_file)
        nif_file.save(dst)
        output({
            "path": src,
            "output": dst,
            "removed_collision_roots": removed,
            "cleared_bsx_flags": bsx_cleared,
            "blocks_before": before,
            "blocks_after": len(nif_file.blocks),
        }, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command("strip-all-collision-batch")
@click.argument("manifest_path")
@click.option("--jobs", default=0, type=int, show_default=True, help="Parallel NIF jobs. 0 = auto.")
@click.option("--backup-root", default="", help="Optional root for backups of existing output files.")
@click.option("--manifest-out", default="", help="Optional JSON path for the per-file result manifest.")
@click.pass_context
def strip_all_collision_batch(ctx, manifest_path, jobs, backup_root, manifest_out):
    """Remove all collision from many NIFs listed in a JSON manifest.

    The manifest must be a JSON array, or an object with an ``items`` array.
    Each item needs ``source`` and ``output``/``target``. ``model`` is optional
    and is used for backup paths and reporting.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path
    import shutil

    from creation_lib.nif.nif_file import NifFile

    fmt = ctx.obj["fmt"]
    manifest_file = os.path.normpath(os.path.abspath(manifest_path))
    if not os.path.isfile(manifest_file):
        output(_error(f"Manifest not found: {manifest_file}"), fmt)
        return

    try:
        payload = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            output(_error("Manifest must be a JSON array or an object with an items array"), fmt)
            return
    except Exception as e:
        output(_error(f"Could not read manifest: {e}"), fmt)
        return

    job_count = jobs if jobs and jobs > 0 else min(8, max(1, (os.cpu_count() or 1) - 1))
    backup_base = Path(backup_root) if backup_root else None

    def process_one(item):
        model = str(item.get("model") or "")
        source = item.get("source")
        output_path = item.get("output") or item.get("target")
        if not source or not output_path:
            return {"model": model, "source": source or "", "output": output_path or "", "success": False, "error": "missing source/output"}
        src = Path(source)
        dst = Path(output_path)
        if not src.is_file():
            return {"model": model, "source": str(src), "output": str(dst), "success": False, "error": "source missing"}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            backup = ""
            if backup_base is not None and dst.exists():
                rel = Path(model.replace("\\", os.sep).replace("/", os.sep)) if model else Path(dst.name)
                backup_path = backup_base / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                if not backup_path.exists():
                    shutil.copy2(dst, backup_path)
                backup = str(backup_path)

            nif_file = NifFile.load(str(src))
            blocks_before = len(nif_file.blocks)
            removed = _strip_all_collision_subtrees(nif_file)
            bsx_cleared = _clear_havok_bsx_flag_if_no_collision(nif_file)
            has_collision = _has_havok_collision_blocks(nif_file)
            nif_file.save(str(dst))
            return {
                "model": model,
                "source": str(src),
                "output": str(dst),
                "backup": backup,
                "success": not has_collision,
                "removed_collision_roots": removed,
                "cleared_bsx_flags": bsx_cleared,
                "blocks_before": blocks_before,
                "blocks_after": len(nif_file.blocks),
                "error": "collision remains after strip" if has_collision else "",
            }
        except Exception as exc:
            return {"model": model, "source": str(src), "output": str(dst), "success": False, "error": str(exc)}

    results = []
    completed = 0
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        futures = [executor.submit(process_one, item) for item in items]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            if completed == len(futures) or completed % 25 == 0:
                click.echo(
                    f"strip-all-collision-batch: completed {completed}/{len(futures)} errors={sum(1 for row in results if not row.get('success'))}",
                    err=True,
                )

    results.sort(key=lambda row: str(row.get("model") or row.get("output") or ""))
    if manifest_out:
        out_path = Path(manifest_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary = {
        "count": len(results),
        "success": sum(1 for row in results if row.get("success")),
        "errors": [row for row in results if not row.get("success")],
        "manifest_out": manifest_out,
        "jobs": job_count,
    }
    output(summary, fmt)


def _find_model_path(obj):
    """Recursively locate the first .nif model path in an authoring-record dict."""
    if isinstance(obj, str):
        return obj if obj.lower().endswith(".nif") else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "model" in str(k).lower():
                hit = _find_model_path(v)
                if hit:
                    return hit
        for v in obj.values():
            hit = _find_model_path(v)
            if hit:
                return hit
    if isinstance(obj, list):
        for v in obj:
            hit = _find_model_path(v)
            if hit:
                return hit
    return None


def _normalize_model_filter(model: str) -> str:
    text = str(model or "").replace("\\", "/").strip().lstrip("/")
    if text.lower().startswith("meshes/"):
        text = text[7:]
    return text.lower()


def _strip_all_collision_subtrees(nif_file) -> int:
    """Remove every collision subtree in the NIF. Re-scans after each removal
    because remove_collision shifts block indices. Returns the count removed."""
    from creation_lib.nif.operations.collision import remove_collision, _find_collision_object

    removed = 0
    for _ in range(64):
        target = None
        for bid in range(len(nif_file.blocks)):
            if _find_collision_object(nif_file, bid) is not None:
                target = bid
                break
        if target is None:
            break
        result = remove_collision(nif_file, target)
        if not getattr(result, "success", False):
            break
        removed += 1
    return removed


def _has_havok_collision_blocks(nif_file) -> bool:
    return any(getattr(block, "type_name", "").startswith("bhk") for block in nif_file.blocks)


def _clear_havok_bsx_flag_if_no_collision(nif_file) -> int:
    if _has_havok_collision_blocks(nif_file):
        return 0
    cleared = 0
    for block in nif_file.blocks:
        if getattr(block, "type_name", "") != "BSXFlags":
            continue
        value = block.get_field("Integer Data")
        if not isinstance(value, int) or (value & 0x02) == 0:
            continue
        block.set_field("Integer Data", value & ~0x02)
        cleared += 1
    return cleared


@nif.command("port-cell-nifs")
@click.argument("plugin_path")
@click.argument("cell_id")
@click.option("--base-type", "base_types", multiple=True, required=True, help="Base record signature(s) whose placed NIFs to port, e.g. MISC. Repeatable.")
@click.option("--source-dir", required=True, help="Source-game extracted root (contains Meshes/). Source NIF = <source-dir>/Meshes/<model>.")
@click.option("--output-dir", required=True, help="Target Data root. Writes <output-dir>/Meshes/<model> as a loose override.")
@click.option("--source-game", default="fo76", show_default=True)
@click.option("--target-game", default="fo4", show_default=True)
@click.option("--strip-collision", is_flag=True, default=False, help="Remove ALL collision from each converted NIF (debug: isolate whether converted collision freezes/crashes a cell).")
@click.option("--include-external", is_flag=True, default=False, help="Also port bases that live in a master (default: only this plugin's own bases).")
@click.option("--model", "model_filters", multiple=True, help="Only convert matching model path(s). Repeatable; accepts paths with or without Meshes/.")
@click.option("--jobs", default=0, type=int, show_default=True, help="Parallel NIF conversion jobs. 0 = auto.")
@click.pass_context
def port_cell_nifs(ctx, plugin_path, cell_id, base_types, source_dir, output_dir, source_game, target_game, strip_collision, include_external, model_filters, jobs):
    """Convert a cell's placed base NIFs from source->target game and deploy them loose.

    Enumerates CELL_ID's placed children whose NAME base resolves to one of --base-type,
    reads each unique base's model path, converts the source NIF to the target game, and
    writes it under <output-dir>/Meshes/<model> as a loose override. With --strip-collision
    every converted NIF has all collision removed -- a fast way to isolate whether the
    converted collision is what freezes/crashes a cell.

    Example:

      modkit --game fo4 nif port-cell-nifs mods/SeventySix/SeventySix.esm 62781C
        --base-type MISC --source-dir extracted/fo76
        --output-dir "N:/Steam Games/steamapps/common/Fallout 4/Data" --strip-collision
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    from cli.esp_commands import _load_plugin, _resolve_cell_object_id
    from creation_lib.esp import native_runtime as esp_nr
    from creation_lib.nif import native_runtime as nif_nr

    fmt = ctx.obj["fmt"]
    wanted = {t.strip().upper() for t in base_types}
    wanted_models = {_normalize_model_filter(model) for model in model_filters}
    job_count = jobs if jobs and jobs > 0 else min(8, max(1, (os.cpu_count() or 1) - 1))
    src_root = Path(source_dir)
    out_root = Path(output_dir)

    models: dict[str, str] = {}
    base_form_keys: list[str] = []
    seen_base_form_keys: set[str] = set()
    with _load_plugin(Path(plugin_path), game=ctx.obj.get("game"), strings_dir=None, language=None, backend="auto") as plugin:
        handle = getattr(plugin, "_rust_handle", None)
        if handle is None:
            raise click.ClickException("port-cell-nifs requires the native ESP backend.")
        own = plugin.plugin_name.lower()
        coid = _resolve_cell_object_id(handle, plugin, cell_id)
        for child in esp_nr.plugin_handle_collect_cell_children(handle, coid):
            refs = esp_nr.plugin_handle_get_referenced_form_keys_by_subrecord(handle, child.get("form_key", ""), "NAME")
            bfk = refs[0] if refs else None
            if not bfk:
                continue
            if (not include_external) and bfk.split(":", 1)[0].lower() != own:
                continue
            if bfk in seen_base_form_keys:
                continue
            seen_base_form_keys.add(bfk)
            base_form_keys.append(bfk)

        click.echo(
            f"port-cell-nifs: found {len(base_form_keys)} unique placed base FormKeys; collecting NIF assets...",
            err=True,
        )
        assets = esp_nr.plugin_handle_collect_assets(
            [handle],
            [],
            asset_kinds=["nif"],
            signatures=sorted(wanted),
            form_keys=base_form_keys,
        )
        for asset in assets:
            model = asset.get("source_path", "")
            if model:
                models.setdefault(model, asset.get("source_form_key", ""))

    if wanted_models:
        models = {
            model: oid
            for model, oid in models.items()
            if _normalize_model_filter(model) in wanted_models
        }
    matched_models = {_normalize_model_filter(model) for model in models}
    missing_model_filters = sorted(wanted_models - matched_models)

    click.echo(
        f"port-cell-nifs: converting {len(models)} unique NIF(s) with {job_count} job(s)"
        + (" and stripping collision" if strip_collision else ""),
        err=True,
    )

    def convert_one(item):
        model, oid = item
        rel = model.replace("/", os.sep).replace("\\", os.sep).lstrip(os.sep)
        if rel.lower().startswith("meshes" + os.sep):
            rel = rel[len("meshes") + 1:]
        src = src_root / "Meshes" / rel
        dst = out_root / "Meshes" / rel
        if not src.is_file():
            return {"model": model, "owner": oid, "converted": False, "stripped": 0, "error": f"source NIF missing: {src}"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = nif_nr.convert_nif_file_raw(
                str(src), str(dst), source_game, target_game, None,
                {
                    "source_path": model,
                    "addon_index_map": {},
                    "asset_prefix": "",
                },
            )
            if not report.get("supported"):
                return {"model": model, "owner": oid, "converted": False, "stripped": 0, "error": "; ".join(report.get("errors", []) or ["unsupported"])}
            n_removed = 0
            if strip_collision:
                from creation_lib.nif.nif_file import NifFile

                nif_file = NifFile.load(str(dst))
                n_removed = _strip_all_collision_subtrees(nif_file)
                nif_file.save(str(dst))
            return {"model": model, "owner": oid, "converted": True, "stripped": n_removed, "error": ""}
        except Exception as exc:
            return {"model": model, "owner": oid, "converted": False, "stripped": 0, "error": str(exc)}

    converted = 0
    stripped = 0
    errors: list[dict] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=job_count) as executor:
        futures = [executor.submit(convert_one, item) for item in models.items()]
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if result["converted"]:
                converted += 1
            if result["stripped"]:
                stripped += 1
            if result["error"]:
                errors.append({"model": result["model"], "owner": result["owner"], "error": result["error"]})
            if completed == len(futures) or completed % 10 == 0:
                click.echo(
                    f"port-cell-nifs: completed {completed}/{len(futures)} converted={converted} errors={len(errors)}",
                    err=True,
                )

    result = {
        "cell": cell_id,
        "base_types": sorted(wanted),
        "model_filters": sorted(wanted_models),
        "missing_model_filters": missing_model_filters,
        "jobs": job_count,
        "unique_models": len(models),
        "converted": converted,
        "strip_collision": strip_collision,
        "stripped_collision": stripped,
        "error_count": len(errors),
        "errors": errors[:25],
    }
    output(result, fmt)
    if missing_model_filters or errors:
        problems = []
        if missing_model_filters:
            problems.append(f"{len(missing_model_filters)} model filter(s) did not match placed NIFs")
        if errors:
            problems.append(f"{len(errors)} NIF conversion(s) failed")
        raise click.ClickException("; ".join(problems))


@nif.command("auto-skin")
@click.argument("session_id")
@click.option("--shape", "shape_id", default=0, type=int, help="BSTriShape block ID")
@click.option("--reference", "reference_path", default="", help="Path to reference body NIF")
@click.option("--method", default="hybrid", type=click.Choice(["barycentric", "proximity", "hybrid"]))
@click.option("--game", default="fo4", help="Target game for reference body")
@click.option("--gender", default="female", type=click.Choice(["male", "female"]))
@click.pass_context
def auto_skin(ctx, session_id, shape_id, reference_path, method, game, gender):
    """Auto-skin a mesh using reference body weights.

    Examples:

      modkit nif auto-skin abc123

      modkit nif auto-skin abc123 --shape 3 --method barycentric --gender male
    """
    from cli._nif_skinning import auto_skin as do_auto_skin

    fmt = ctx.obj["fmt"]
    nif_file, original_path = _load_nif_or_error(session_id)
    try:
        result = do_auto_skin(
            nif_file, shape_id=shape_id,
            reference_path=reference_path, method=method,
            game=game, gender=gender,
        )
        save_session(session_id, nif_file)
        output(result, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("source_session")
@click.argument("source_shape_id", type=int)
@click.argument("target_session")
@click.argument("target_shape_id", type=int)
@click.option("--method", default="hybrid", type=click.Choice(["barycentric", "proximity", "hybrid"]))
@click.option("--radius", "search_radius", default=10.0, type=float, help="Search radius for proximity method")
@click.pass_context
def transfer(ctx, source_session, source_shape_id, target_session, target_shape_id, method, search_radius):
    """Transfer bone weights between shapes.

    Examples:

      modkit nif transfer src123 3 tgt456 5

      modkit nif transfer src123 3 tgt456 5 --method proximity --radius 5.0
    """
    from cli._nif_skinning import transfer_weights as do_transfer

    fmt = ctx.obj["fmt"]
    src_nif, src_path = _load_nif_or_error(source_session)
    tgt_nif, tgt_path = _load_nif_or_error(target_session)
    try:
        result = do_transfer(
            src_nif, source_shape_id,
            tgt_nif, target_shape_id,
            method=method, search_radius=search_radius,
        )
        save_session(target_session, tgt_nif)
        output(result, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("session_id")
@click.option("--shape", "shape_id", default=0, type=int, help="BSTriShape block ID")
@click.option("--reference", "reference_path", default="", help="Reference NIF for from_reference method")
@click.option("--method", default="from_bones", type=click.Choice(["from_bones", "from_reference"]))
@click.pass_context
def partitions(ctx, session_id, shape_id, reference_path, method):
    """Generate dismemberment partition assignments.

    Examples:

      modkit nif partitions abc123

      modkit nif partitions abc123 --shape 3 --method from_reference --reference body.nif
    """
    from cli._nif_skinning import generate_partitions as do_partitions

    fmt = ctx.obj["fmt"]
    nif_file, original_path = _load_nif_or_error(session_id)
    try:
        result = do_partitions(
            nif_file, shape_id=shape_id,
            reference_path=reference_path, method=method,
        )
        output(result, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("session_id")
@click.option("--shape", "shape_id", default=0, type=int, help="BSTriShape block ID")
@click.option("--max-bones", default=4, type=int, help="Max bone influences per vertex")
@click.pass_context
def normalize(ctx, session_id, shape_id, max_bones):
    """Normalize bone weights (enforce max bones, sum to 1.0).

    Examples:

      modkit nif normalize abc123

      modkit nif normalize abc123 --shape 3 --max-bones 4
    """
    from cli._nif_skinning import normalize_weights as do_normalize

    fmt = ctx.obj["fmt"]
    nif_file, original_path = _load_nif_or_error(session_id)
    try:
        result = do_normalize(nif_file, shape_id=shape_id, max_bones=max_bones)
        save_session(session_id, nif_file)
        output(result, fmt)
    except Exception as e:
        output(_error(str(e)), fmt)


@nif.command()
@click.argument("commands_json", required=False)
@click.option(
    "--file",
    "commands_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read commands JSON from a file instead of the command line.",
)
@click.pass_context
def batch(ctx, commands_json, commands_file):
    """Execute multiple NIF commands in one call.

    Commands run sequentially — later commands see earlier changes.

    Examples:

      modkit nif batch '[{"tool":"inspect","args":{"path":"weapon.nif","block_id":0}}]'
    """
    fmt = ctx.obj["fmt"]
    if (commands_json is None) == (commands_file is None):
        output(_error("Provide exactly one of COMMANDS_JSON or --file"), fmt)
        return
    if commands_file is not None:
        try:
            with open(commands_file, encoding="utf-8") as stream:
                commands_json = stream.read()
        except OSError as e:
            output(_error(f"Could not read commands file: {e}"), fmt)
            return
    try:
        commands = json.loads(commands_json)
    except json.JSONDecodeError as e:
        output(_error(f"Invalid JSON: {e}"), fmt)
        return

    from creation_lib.nif.nif_file import NifFile
    from creation_lib.nif.types import to_json
    from creation_lib.nif.operations.copy import copy_blocks as ops_copy_blocks
    from creation_lib.nif.operations.collision import (
        generate_collision as gen_coll,
        remove_collision as rem_coll,
    )
    from creation_lib.core.game_profiles import detect_game
    from cli._nif_skinning import (
        auto_skin as do_auto_skin,
        transfer_weights as do_transfer,
        generate_partitions as do_partitions,
        validate_weights as do_validate,
        normalize_weights as do_normalize,
    )

    # Batch dispatch — operates on in-memory NifFile objects via sessions
    sessions: dict[str, dict] = {}

    def _get_or_load(sid: str) -> NifFile:
        if sid not in sessions:
            nif_file, path = load_session(sid)
            sessions[sid] = {"nif": nif_file, "path": path}
        return sessions[sid]["nif"]

    def _batch_inspect(args):
        if args.get("path"):
            p = os.path.normpath(os.path.abspath(args["path"]))
            nif_file = NifFile.load(p)
        elif args.get("session_id"):
            nif_file = _get_or_load(args["session_id"])
        else:
            return _error("Provide path or session_id")
        return _inspect_nif(nif_file, args.get("block_id", -1))

    def _batch_modify(args):
        nif_file = _get_or_load(args["session_id"])
        block = nif_file.get_block(args["block_id"])
        if block is None:
            return _error(f"Block {args['block_id']} not found")
        for name, val in args.get("fields", {}).items():
            block.set_field(name, val)
        return {"block_id": args["block_id"], "updated_fields": list(args.get("fields", {}).keys())}

    def _batch_save(args):
        sid = args["session_id"]
        nif_file = _get_or_load(sid)
        path = args.get("path") or sessions[sid]["path"]
        if not path:
            return _error("Provide path for a session without an original path")
        path = os.path.normpath(os.path.abspath(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nif_file.save(path)
        return {"saved": path}

    def _batch_copy_blocks(args):
        src = _get_or_load(args["source_session"])
        tgt = _get_or_load(args["target_session"])
        attach = args.get("attach_to", -1)
        id_map = ops_copy_blocks(
            src, args["block_ids"], tgt,
            attach_to=attach if attach >= 0 else None,
        )
        return {"id_map": {str(k): v for k, v in id_map.items()}}

    def _batch_add_block(args):
        nif_file = _get_or_load(args["session_id"])
        block = nif_file.add_block(args["type_name"], args.get("fields"))
        attach = args.get("attach_to", -1)
        if attach >= 0:
            parent = nif_file.get_block(attach)
            if parent:
                children = parent.get_field("Children")
                if isinstance(children, list):
                    children.append(block.block_id)
                    parent.set_field("Children", children)
                num = parent.get_field("Num Children")
                if isinstance(num, int):
                    parent.set_field("Num Children", num + 1)
        return {"block_id": block.block_id, "type": args["type_name"], "fields": block.to_json()}

    def _batch_remove_blocks(args):
        nif_file = _get_or_load(args["session_id"])
        before = len(nif_file.blocks)
        nif_file.remove_blocks(args["block_ids"])
        return {"removed": before - len(nif_file.blocks)}

    def _batch_generate_collision(args):
        nif_file = _get_or_load(args["session_id"])
        profile = getattr(nif_file, 'detected_game', None)
        if profile is None and hasattr(nif_file, 'header'):
            profile = detect_game(nif_file.header.bs_version)
        result = gen_coll(
            nif_file, args.get("node_block_id", 0),
            shape_type=args.get("shape_type", "convex_hull"),
            source_block_ids=args.get("source_block_ids"),
            layer=args.get("layer", "STATIC"),
            mass=args.get("mass", 0.0),
            friction=args.get("friction", 0.5),
            restitution=args.get("restitution", 0.4),
            radius=args.get("radius", 0.05),
            replace=args.get("replace", True),
            profile=profile,
        )
        return {"success": result.success, "description": result.description,
                "modified_block_ids": result.modified_block_ids, "warnings": result.warnings}

    def _batch_remove_collision(args):
        nif_file = _get_or_load(args["session_id"])
        result = rem_coll(nif_file, args.get("node_block_id", 0))
        return {"success": result.success, "description": result.description, "warnings": result.warnings}

    def _batch_auto_skin(args):
        nif_file = _get_or_load(args["session_id"])
        return do_auto_skin(nif_file, **{k: v for k, v in args.items() if k != "session_id"})

    def _batch_transfer_weights(args):
        src = _get_or_load(args["source_session"])
        tgt = _get_or_load(args["target_session"])
        return do_transfer(src, args["source_shape_id"], tgt, args["target_shape_id"],
                           method=args.get("method", "hybrid"), search_radius=args.get("search_radius", 10.0))

    def _batch_generate_partitions(args):
        nif_file = _get_or_load(args["session_id"])
        return do_partitions(nif_file, **{k: v for k, v in args.items() if k != "session_id"})

    def _batch_validate_weights(args):
        nif_file = _get_or_load(args["session_id"])
        return do_validate(nif_file, **{k: v for k, v in args.items() if k != "session_id"})

    def _batch_normalize_weights(args):
        nif_file = _get_or_load(args["session_id"])
        return do_normalize(nif_file, **{k: v for k, v in args.items() if k != "session_id"})

    dispatch = {
        "inspect": _batch_inspect,
        "modify": _batch_modify,
        "save": _batch_save,
        "copy_blocks": _batch_copy_blocks,
        "add_block": _batch_add_block,
        "remove_blocks": _batch_remove_blocks,
        "generate_collision": _batch_generate_collision,
        "remove_collision": _batch_remove_collision,
        "auto_skin": _batch_auto_skin,
        "transfer_weights": _batch_transfer_weights,
        "generate_partitions": _batch_generate_partitions,
        "validate_weights": _batch_validate_weights,
        "normalize_weights": _batch_normalize_weights,
    }

    results = []
    for cmd in commands:
        tool_name = cmd.get("tool", "")
        args = cmd.get("args", {})
        fn = dispatch.get(tool_name)
        if fn is None:
            results.append({"tool": tool_name, "args": args,
                            "result": {"error": f"Unknown tool '{tool_name}'. Valid: {', '.join(sorted(dispatch))}"}})
            continue
        try:
            result = fn(args)
        except Exception as e:
            result = {"error": str(e)}
        results.append({"tool": tool_name, "args": args, "result": result})

    # Save any modified sessions back
    for sid in sessions:
        save_session(sid, sessions[sid]["nif"])

    output(results, fmt)
