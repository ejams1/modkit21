"""modkit swf -- SWF inspection, extraction, and packing commands.

Command surface:
    modkit swf inspect <swf>            # dump SWF structure
    modkit swf extract <swf> -o <dir>   # export shapes as SVGs
    modkit swf pack <swfproj> -o <swf>  # assemble SWF from project
    modkit swf index                    # build shape library from extracted SWFs
    modkit swf symbols list <swf>       # byte-exact SymbolClass export list (native)
    modkit swf symbols validate <swf>   # SymbolClass names with no backing DoABC class
    modkit swf symbols inject ...       # splice named symbols src -> dst (native)
    modkit swf abc dump <swf>           # ABC constant-pool string table (class names)
    modkit swf abc markers <swf>        # which canonical marker classes the SWF has
    modkit swf markers build ...        # inject FO76 region marker icons into FO4 SWFs
    modkit swf markers table            # dump the canonical FO76->FO4 marker table

`inspect`/`extract`/`pack` use the pure-Python (byte-lossy) codec for Pip-Boy icon
authoring; `symbols`/`abc`/`markers` use the native byte-exact reader and must be
used for inspecting/editing real menu SWFs.
"""
from __future__ import annotations

from pathlib import Path

import click

from cli._output import JSON_FORMATS, output


@click.group()
def swf():
    """SWF inspection and shape library commands."""


@swf.command()
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--sprites", "show_sprites", is_flag=True,
              help="Also list every sprite's character id and timeline frame count")
@click.pass_context
def inspect(ctx: click.Context, swf_path: Path, show_sprites: bool):
    """Print summary of SWF structure.

    Shows: version, canvas size, FPS, frame count, shape count,
    sprite count, background color, tag breakdown.
    """
    from creation_lib.swf.parser import parse_swf_file
    from creation_lib.swf.tags import RawTag

    try:
        doc = parse_swf_file(swf_path)
    except Exception as exc:
        click.echo(f"error: failed to parse {swf_path}: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    h = doc.header

    # Count tags by type
    tag_counts: dict[str, int] = {}
    for tag in doc.tags:
        name = type(tag).__name__
        tag_counts[name] = tag_counts.get(name, 0) + 1

    summary = {
        "file": str(swf_path),
        "version": h.version,
        "compression": h.compression,
        "canvas": f"{h.frame_size.width_px}x{h.frame_size.height_px}",
        "fps": h.fps,
        "frame_count": h.frame_count,
        "background": doc.background_color.to_hex(),
        "shape_count": len(doc.shapes),
        "sprite_count": len(doc.sprites),
        "export_count": len(doc.symbols),
        "total_tags": len(doc.tags),
        "raw_tags": len(doc.raw_tags),
        "tag_breakdown": tag_counts,
    }
    if show_sprites:
        summary["sprites"] = [
            {"character_id": sid, "frames": sprite.timeline.frame_count}
            for sid, sprite in sorted(doc.sprites.items())
        ]

    if fmt in JSON_FORMATS:
        output(summary, fmt)
    else:
        click.echo(f"SWF: {swf_path.name}")
        click.echo(f"  Version: {h.version} ({h.compression})")
        click.echo(f"  Canvas:  {h.frame_size.width_px}x{h.frame_size.height_px}")
        click.echo(f"  FPS:     {h.fps}")
        click.echo(f"  Frames:  {h.frame_count}")
        click.echo(f"  BG:      {doc.background_color.to_hex()}")
        click.echo(f"  Shapes:  {len(doc.shapes)}")
        click.echo(f"  Sprites: {len(doc.sprites)}")
        click.echo(f"  Exports: {len(doc.symbols)}")
        click.echo(f"  Tags:    {len(doc.tags)} ({len(doc.raw_tags)} unparsed)")
        for name, count in sorted(tag_counts.items()):
            click.echo(f"    {name}: {count}")
        if show_sprites:
            click.echo("  Sprite timelines:")
            for sid, sprite in sorted(doc.sprites.items()):
                click.echo(f"    sprite {sid}: {sprite.timeline.frame_count} frames")


@swf.command()
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Output directory for SVGs (default: <swf_name>_shapes/)")
@click.option("--background/--no-background", default=True,
              help="Draw a preview background rect behind each shape. Pass "
                   "--no-background when the SVGs will be re-packed, or the rect "
                   "imports as an opaque quad over the art.")
@click.pass_context
def extract(ctx: click.Context, swf_path: Path, output: Path | None, background: bool):
    """Export all shapes from a SWF as individual SVG files."""
    from creation_lib.swf.parser import parse_swf_file
    from creation_lib.swf.svg_io import shape_to_svg

    try:
        doc = parse_swf_file(swf_path)
    except Exception as exc:
        click.echo(f"error: failed to parse {swf_path}: {exc}", err=True)
        ctx.exit(2)
        return

    out_dir = output or (swf_path.parent / f"{swf_path.stem}_shapes")
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for shape_id, shape in doc.shapes.items():
        svg = shape_to_svg(shape, background="#333333" if background else None)
        svg_path = out_dir / f"shape_{shape_id}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        count += 1

    click.echo(f"Extracted {count} shapes to {out_dir}")


@swf.command()
@click.argument("project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), required=True,
              help="Output SWF path")
@click.pass_context
def pack(ctx: click.Context, project_path: Path, output: Path):
    """Assemble a SWF from a .swfproj project file.

    The project declares shapes (imported from SVG), sprites whose timelines
    place that art frame by frame, the main-timeline stage, and the SymbolClass
    exports that name characters for the outside world. Each export also gets an
    AS3 class synthesized into a DoABC tag, and the result is refused if any
    export name is left unbacked. See `creation_lib.swf.project` for the schema.
    """
    from creation_lib.swf import native_runtime
    from creation_lib.swf.project import load_project_file
    from creation_lib.swf.writer import write_swf

    try:
        doc = load_project_file(project_path)
        data = write_swf(doc)
        unbacked = native_runtime.unbacked_symbol_classes(data)
    except Exception as exc:
        click.echo(f"error: failed to build project: {exc}", err=True)
        ctx.exit(2)
        return

    if unbacked:
        click.echo(
            f"error: {len(unbacked)} SymbolClass export(s) have no AS3 class behind "
            f"them, so the engine could not construct them: {', '.join(unbacked)}",
            err=True,
        )
        ctx.exit(2)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    click.echo(
        f"Packed SWF: {output} "
        f"({len(doc.shapes)} shapes, {len(doc.sprites)} sprites, "
        f"{len(doc.symbols)} exports, {doc.header.frame_count} frames)"
    )


@swf.command("index")
@click.pass_context
def index_cmd(ctx: click.Context):
    """Build shape library from extracted FO4 SWF files.

    Equivalent to: modkit index build --domain swf
    """
    from app.paths import get_app_root, get_db_dir
    from cli.index_commands import _resolve_game_paths
    from creation_lib.db.index_builder import build_domain_index
    game = ctx.obj.get("game", "fo4") if ctx.obj else "fo4"
    extracted_dir, game_dir = _resolve_game_paths(game)
    try:
        build_domain_index(
            game,
            "swf",
            extracted_dir=extracted_dir,
            game_dir=game_dir,
            project_root=get_app_root(),
            db_dir=get_db_dir(),
            on_progress=click.echo,
        )
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)


@swf.group()
def symbols():
    """Byte-exact SymbolClass inspection / injection (native splicer)."""


@symbols.command("list")
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def symbols_list(ctx: click.Context, swf_path: Path):
    """List every SymbolClass export (character_id, name) in file order.

    Unlike `swf inspect`, this reads via the native byte-exact splitter — the
    same view FO4 uses to bind REFR.TNAM marker types to icons.
    """
    from creation_lib.swf import native_runtime

    try:
        syms = native_runtime.list_symbols(swf_path.read_bytes())
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output([{"character_id": cid, "name": name} for cid, name in syms], fmt)
    else:
        for cid, name in syms:
            click.echo(f"  {cid:>5}  {name}")
        click.echo(f"{len(syms)} symbols")


@symbols.command("validate")
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--no-fail", is_flag=True, help="Report unbacked names but exit 0")
@click.pass_context
def symbols_validate(ctx: click.Context, swf_path: Path, no_fail: bool):
    """Report SymbolClass export names that no DoABC in the file defines.

    A dangling binding names a class the engine cannot construct, so the symbol
    comes back null wherever it is loaded. Exits 1 when any name is unbacked.
    """
    from creation_lib.swf import native_runtime

    try:
        data = swf_path.read_bytes()
        defined = native_runtime.abc_class_names(data)
        symbol_names = [name for _, name in native_runtime.list_symbols(data)]
        unbacked = native_runtime.unbacked_symbol_classes(data)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output({"symbols": len(symbol_names), "defined_classes": len(defined),
                "unbacked": unbacked}, fmt)
    else:
        click.echo(f"{len(symbol_names)} SymbolClass export(s), "
                   f"{len(defined)} AS3 class(es) defined")
        for name in unbacked:
            click.echo(f"  UNBACKED  {name}")
        click.echo(f"{len(unbacked)} unbacked")
    if unbacked and not no_fail:
        ctx.exit(1)


@symbols.command("inject")
@click.option("--src", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Donor SWF (e.g. FO76 mapmarkerlibrary.swf)")
@click.option("--dst", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Destination SWF to inject into")
@click.option("-n", "--name", "names", multiple=True, required=True,
              help="SymbolClass export name to inject (repeatable)")
@click.option("-o", "--output", required=True, type=click.Path(path_type=Path),
              help="Output SWF path")
@click.pass_context
def symbols_inject(ctx: click.Context, src: Path, dst: Path, names: tuple[str, ...], output: Path):
    """Splice named symbols (with their full character closures) from src into dst."""
    from creation_lib.swf import native_runtime

    try:
        out = native_runtime.inject_symbols(src.read_bytes(), dst.read_bytes(), list(names))
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(out)
    click.echo(f"Injected {len(names)} symbol(s) into {output.name} ({len(out)} bytes)")


@swf.group()
def abc():
    """ActionScript Byte Code inspection (read-only constant-pool view)."""


@abc.command("dump")
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--strings/--no-strings", default=False, help="Print the full string table")
@click.pass_context
def abc_dump(ctx: click.Context, swf_path: Path, strings: bool):
    """Dump each DoABC tag's constant-pool string table (where class names live)."""
    from creation_lib.swf import native_runtime

    try:
        pools = native_runtime.abc_string_pools(swf_path.read_bytes())
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    report = []
    for code, minor, major, ic, uc, dc, strs in pools:
        entry = {
            "tag_code": code,
            "version": f"{major}.{minor}",
            "int_count": ic,
            "uint_count": uc,
            "double_count": dc,
            "string_count": len(strs),
        }
        if strings:
            entry["strings"] = strs
        report.append(entry)
    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    output(report, fmt if fmt in JSON_FORMATS else "json")


@abc.command("markers")
@click.argument("swf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def abc_markers(ctx: click.Context, swf_path: Path):
    """Report which canonical FO76 marker export names already exist as AS3 classes
    in the SWF's ABC (i.e. already have a backing class)."""
    from creation_lib.swf.markers import abc_class_name_presence, marker_icon_table

    try:
        table = marker_icon_table()
        presence = abc_class_name_presence(swf_path.read_bytes(), table)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    present = sorted(n for n, ok in presence.items() if ok)
    absent = sorted(n for n, ok in presence.items() if not ok)
    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output({"total": len(presence), "present": present, "absent": absent}, fmt)
    else:
        click.echo(f"present ({len(present)}): {', '.join(present) or '-'}")
        click.echo(f"absent  ({len(absent)}): {', '.join(absent) or '-'}")


@swf.group()
def markers():
    """FO76 -> FO4 map-marker icon injection (UI-integration phase)."""


@markers.command("table")
@click.pass_context
def markers_table(ctx: click.Context):
    """Dump the canonical FO76->FO4 marker icon table (fo76_type, fo4_byte, symbol)."""
    from creation_lib.swf.markers import marker_icon_table

    try:
        table = marker_icon_table()
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    if fmt in JSON_FORMATS:
        output(
            [
                {
                    "fo76_type": m.fo76_type,
                    "fo4_byte": m.fo4_byte,
                    "symbol": m.symbol,
                    "source_symbol": m.source_symbol,
                }
                for m in table
            ],
            fmt,
        )
    else:
        for m in table:
            renamed = "" if m.symbol == m.source_symbol else f"  (from {m.source_symbol})"
            click.echo(f"  {m.fo76_type:>3} -> {m.fo4_byte:>3}  {m.symbol}{renamed}")
        click.echo(f"{len(table)} icons")


@markers.command("build")
@click.option("--fo76-lib", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="FO76 mapmarkerlibrary.swf (symbol donor)")
@click.option("--fo4-swf", "fo4_swfs", multiple=True, required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="FO4 menu SWF to inject into (repeatable)")
@click.option("-o", "--output", "out_dir", required=True, type=click.Path(path_type=Path),
              help="Output directory for injected SWFs + marker_injection.json")
@click.pass_context
def markers_build(ctx: click.Context, fo76_lib: Path, fo4_swfs: tuple[Path, ...], out_dir: Path):
    """Inject all 42 FO76 region marker icons into each FO4 menu SWF (deterministic)."""
    from creation_lib.swf.markers import build_marker_swfs

    try:
        summary = build_marker_swfs(fo76_lib, list(fo4_swfs), out_dir)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(2)
        return

    fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
    output(summary, fmt if fmt in JSON_FORMATS else "json")
