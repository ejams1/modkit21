"""modkit texture — texture manipulation commands."""
import tempfile
from pathlib import Path

import click

from cli._output import JSON_FORMATS, output


@click.group()
@click.pass_context
def texture(ctx):
    """Texture manipulation tools."""
    pass


@texture.command("inspect")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def inspect(ctx, path):
    """Read DDS dimensions, format, mip levels and array/cubemap metadata."""
    from creation_lib.dds.native_runtime import texdiag_info

    info = texdiag_info(str(path.resolve()))
    if info is None:
        raise ImportError("Native DDS metadata inspection is unavailable; rebuild the creation native extension.")
    output({"path": str(path.resolve()), "bytes": path.stat().st_size, **info,
            "srgb": "SRGB" in str(info.get("format", "")).upper()}, ctx.obj["fmt"])


@texture.group(name="recolor")
@click.pass_context
def recolor(ctx):
    """Recolor DDS/PNG textures."""
    pass


@recolor.command(name="hue-shift")
@click.argument("input_path", type=click.Path(exists=True))
@click.argument("output_path", type=click.Path())
@click.option("--degrees", type=float, required=True, help="Degrees to shift hue")
@click.option("--format", "fmt", type=click.Choice(["diffuse", "emissive", "normal", "grayscale", "raw"]),
              default="diffuse", help="DDS output format")
def hue_shift_cmd(input_path, output_path, degrees, fmt):
    """Rotate all pixel hues by degrees."""
    from creation_lib.textures.recolor import load_input, save_output, hue_shift

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        img = load_input(Path(input_path), work)
        result = hue_shift(img, degrees)
        save_output(result, Path(output_path), work, fmt)
        click.echo(f"Hue shifted by {degrees}\u00b0 -> {output_path}")


@recolor.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.argument("output_path", type=click.Path())
@click.option("--color", required=True, help="Target color: 'R,G,B' (0-255) or '#RRGGBB'")
@click.option("--format", "fmt", type=click.Choice(["diffuse", "emissive", "normal", "grayscale", "raw"]),
              default="diffuse", help="DDS output format")
def tint(input_path, output_path, color, fmt):
    """Multiply RGB by a color (best for gray/white textures)."""
    from creation_lib.textures.recolor import load_input, save_output, tint as tint_fn, parse_color

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        img = load_input(Path(input_path), work)
        result = tint_fn(img, parse_color(color))
        save_output(result, Path(output_path), work, fmt)
        click.echo(f"Tinted with {color} -> {output_path}")


@recolor.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.argument("output_path", type=click.Path())
@click.option("--color", required=True, help="Target color: 'R,G,B' (0-255) or '#RRGGBB'")
@click.option("--format", "fmt", type=click.Choice(["diffuse", "emissive", "normal", "grayscale", "raw"]),
              default="diffuse", help="DDS output format")
def colorize(input_path, output_path, color, fmt):
    """Force hue+saturation, preserve luminance."""
    from creation_lib.textures.recolor import load_input, save_output, colorize as colorize_fn, parse_color

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        img = load_input(Path(input_path), work)
        result = colorize_fn(img, parse_color(color))
        save_output(result, Path(output_path), work, fmt)
        click.echo(f"Colorized to {color} -> {output_path}")


@recolor.command()
@click.argument("output_path", type=click.Path())
@click.option("--from", "color_from", required=True, help="Start color: 'R,G,B' or '#RRGGBB'")
@click.option("--to", "color_to", required=True, help="End color: 'R,G,B' or '#RRGGBB'")
@click.option("--width", type=int, default=256, help="Width in pixels (default: 256)")
@click.option("--height", type=int, default=16, help="Height in pixels (default: 16)")
@click.option("--format", "fmt", type=click.Choice(["diffuse", "emissive", "normal", "grayscale", "raw"]),
              default="diffuse", help="DDS output format")
def gradient(output_path, color_from, color_to, width, height, fmt):
    """Generate a gradient palette strip."""
    from creation_lib.textures.recolor import make_gradient, save_output, parse_color

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        cf = parse_color(color_from)
        ct = parse_color(color_to)
        img = make_gradient(cf, ct, width, height)
        save_output(img, Path(output_path), work, fmt)
        click.echo(f"Gradient {cf} -> {ct} saved to {output_path}")


@recolor.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.pass_context
def analyze(ctx, input_path):
    """Analyze texture colors (no output file)."""
    from creation_lib.textures.recolor import load_input, analyze as analyze_fn

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)
        img = load_input(Path(input_path), work)
        info = analyze_fn(img)
        fmt = ctx.obj.get("fmt", "json") if ctx.obj else "json"
        output(info, fmt if fmt in JSON_FORMATS else "json")
