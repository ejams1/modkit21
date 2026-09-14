import click

from cli._output import output


@click.group()
def anim():
    """Animation cache identifiers and metadata."""


@anim.group("subgraph-id")
def subgraph_id():
    """Encode or split the two CRC32 words of a SubgraphIdentifier."""


def _id_report(value):
    return {"id": str(value), "hex": f"{value:016X}", "behavior_crc32": f"{value & 0xFFFFFFFF:08X}",
            "path_chain_crc32": f"{value >> 32:08X}", "filename": f"{value}.txt"}


@subgraph_id.command("encode")
@click.argument("behavior")
@click.option("--path", "paths", multiple=True, help="SAPT path, self first then parents; repeatable. Preserve authored case.")
@click.pass_context
def encode(ctx, behavior, paths):
    """Calculate an identifier using the native CK-compatible algorithm."""
    from creation_lib.inspection.animation import subgraph_id
    output({**_id_report(subgraph_id(behavior, paths)), "behavior": behavior, "paths": list(paths)}, ctx.obj["fmt"])


@subgraph_id.command("decode")
@click.argument("value")
@click.pass_context
def decode(ctx, value):
    """Split a decimal or 0x-prefixed ID. Hashes cannot recover original paths."""
    try:
        number = int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError:
        raise click.UsageError("Expected a decimal or 0x-prefixed 64-bit identifier") from None
    if not 0 <= number < 1 << 64:
        raise click.UsageError("Identifier must be an unsigned 64-bit integer")
    output({**_id_report(number), "reversible": False}, ctx.obj["fmt"])
