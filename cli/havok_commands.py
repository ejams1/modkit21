from pathlib import Path

import click

from cli._output import output


@click.group()
def havok():
    """Inspect and compare HKX behavior graphs or their XML exports."""


@havok.command("inspect")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def inspect(ctx, path):
    """Report states, named transitions, variables and variable bindings."""
    from creation_lib.inspection.behavior import behavior_report
    output(behavior_report(path), ctx.obj["fmt"])


@havok.command("diff")
@click.argument("a", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("b", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def diff(ctx, a, b):
    """Compare decoded graph fields, following references independently of object numbering."""
    from creation_lib.inspection.behavior import read_behavior_xml
    from creation_lib.inspection.havok import graph_differences
    changes = graph_differences(read_behavior_xml(a), read_behavior_xml(b))
    output({"a": str(a.resolve()), "b": str(b.resolve()), "equal": not changes, "changes": changes,
            "comparison": "Decoded XML fields; defaults and numeric spellings may differ. Unreachable objects are compared separately."},
           ctx.obj["fmt"], collection="changes")


@havok.command("classes")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--check-exe", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.pass_context
def classes(ctx, path, check_exe):
    """List graph classes; optionally look for their exact names in an executable."""
    import mmap
    from creation_lib.inspection.behavior import behavior_report
    report = behavior_report(path)
    rows = [{"class": name, "count": count} for name, count in report["classes"].items()]
    if check_exe:
        with check_exe.open("rb") as stream:
            if check_exe.stat().st_size:
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
                    for row in rows:
                        row["name_found"] = binary.find(row["class"].encode() + b"\0") >= 0
            else:
                for row in rows:
                    row["name_found"] = False
    output({"path": str(path.resolve()), "executable": str(check_exe.resolve()) if check_exe else None,
            "classes": rows, "method": "Exact null-terminated class-name string search; presence does not prove runtime compatibility."},
           ctx.obj["fmt"], collection="classes")
