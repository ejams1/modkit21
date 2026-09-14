"""modkit build — archive extraction, packing, and validation commands."""

import os
import sys

import click


def _resolve_mod_game(ctx, mod_name: str) -> str:
    """Return game for mod_name: explicit --game flag > .game file > DEFAULT_GAME fallback."""
    if ctx.obj.get("game_explicit"):
        return ctx.obj["game"]
    from app.paths import get_app_root
    game_file = get_app_root() / "mods" / mod_name / ".game"
    if game_file.is_file():
        return game_file.read_text(encoding="utf-8").strip()
    return ctx.obj["game"]


@click.group()
@click.option("--game", default=None, help="Game profile (overrides global --game).")
@click.pass_context
def build(ctx, game):
    """Build pipeline: extract game data, pack archives, validate mods."""
    if game is not None:
        ctx.obj["game"] = game


@build.command()
@click.option("--install-dir", default=None, help="Game install directory (overrides .env)")
@click.option("--output-dir", default=None, help="Output directory (default: extracted/<game>/)")
@click.option("--smart", is_flag=True, help="Skip if archives haven't changed since last run")
@click.option("--workers", type=int, default=None, help="Archives to extract in parallel (default: min(4, cpu_count))")
@click.option("--file-workers", type=int, default=8, help="Threads per archive (default: 8)")
@click.pass_context
def extract(ctx, install_dir, output_dir, smart, workers, file_workers):
    """Extract BSA/BA2 archives from a game's Data/ folder."""
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from creation_lib.preprocessor.extraction import (
        resolve_install_dir, find_archives, group_archives_by_update_phase, extract_one,
        load_manifest, build_manifest, manifest_matches, save_manifest,
        resolve_papyrus_source_dir, sync_papyrus_sources, plan_archive_extraction_batches,
    )
    from creation_lib.core.game_profiles import get_profile
    from app.paths import get_app_root

    game = ctx.obj["game"]
    profile = get_profile(game)
    click.echo(f"Extracting game data for {profile.display_name} ({game})")

    resolved = resolve_install_dir(
        game,
        install_dir,
        game_dir=os.environ.get(f"{game.upper()}_DIR", ""),
        steam_dir=os.environ.get("STEAM_DIR", ""),
    )
    if resolved is None:
        raise click.ClickException(f"Could not find install directory for {profile.display_name}")

    data_dir = resolved / "Data"
    if not data_dir.is_dir():
        raise click.ClickException(f"Data directory not found: {data_dir}")

    click.echo(f"Install dir: {resolved}")
    click.echo(f"Data dir:    {data_dir}")
    papyrus_source_dir = resolve_papyrus_source_dir(resolved, game)
    if papyrus_source_dir is not None:
        click.echo(f"Papyrus src: {papyrus_source_dir}")

    if output_dir:
        out = Path(output_dir)
    else:
        out = get_app_root() / "extracted" / game
    out.mkdir(parents=True, exist_ok=True)
    click.echo(f"Output dir:  {out}")

    archives = find_archives(data_dir, profile.archive_format)
    if not archives:
        raise click.ClickException(f"No {profile.archive_format.upper()} archives found in {data_dir}")

    click.echo(f"\nFound {len(archives)} {profile.archive_format.upper()} archive(s):")
    for a in archives:
        click.echo(f"  {a.name}")

    # Smart extract check
    if smart:
        existing = load_manifest(out)
        if manifest_matches(existing, data_dir, archives, papyrus_source_dir):
            click.echo(f"\nSmart check: {len(archives)} archive(s) unchanged — skipping.")
            return

    if workers is None:
        workers = min(4, os.cpu_count() or 1)
    workers = max(1, workers)

    click.echo(f"\nExtracting with {workers} worker(s)...\n")

    total_files = 0
    n = len(archives)
    completed = 0
    archive_groups = group_archives_by_update_phase(archives)
    if len(archive_groups) > 1:
        click.echo(f"Using {len(archive_groups)} ordered overwrite phases.\n")

    def _progress_callback(archive_name: str):
        last_reported = 0

        def _progress(event: dict) -> bool:
            nonlocal last_reported
            completed_files = int(event.get("completed", 0) or 0)
            total_archive_files = int(event.get("total", 0) or 0)
            if completed_files == total_archive_files or completed_files - last_reported >= 5_000:
                last_reported = completed_files
                click.echo(
                    f"  {archive_name}: {completed_files:,}/{total_archive_files:,} file(s)"
                )
            return True

        return _progress

    for phase_idx, archive_group in enumerate(archive_groups, start=1):
        if len(archive_groups) > 1:
            click.echo(
                f"Phase {phase_idx}/{len(archive_groups)}: "
                f"{len(archive_group)} archive(s), {workers} total worker(s)"
            )
        for batch in plan_archive_extraction_batches(archive_group, workers):
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {
                    pool.submit(
                        extract_one,
                        task.archive,
                        out,
                        profile.archive_format,
                        min(file_workers, task.file_workers) if file_workers else task.file_workers,
                        _progress_callback(task.archive.name),
                    ): task.archive
                    for task in batch
                }
                for future in as_completed(futures):
                    archive, count, error = future.result()
                    completed += 1
                    if error:
                        click.echo(f"[{completed}/{n}] ERROR {archive.name}: {error}")
                    else:
                        click.echo(f"[{completed}/{n}] {archive.name}: {count} files")
                        total_files += count

    click.echo(f"\nExtraction complete: {total_files} files from {len(archives)} archives")

    if papyrus_source_dir is not None:
        papyrus_files = sync_papyrus_sources(papyrus_source_dir, out)
        click.echo(f"Papyrus sources mirrored: {papyrus_files} file(s)")

    click.echo(f"Output: {out}")

    manifest = build_manifest(game, data_dir, archives, papyrus_source_dir)
    save_manifest(out, manifest)
    click.echo(f"Manifest saved: {out / '.ba2_manifest.json'}")


@build.command()
@click.argument("mod_name")
@click.option("--pc", is_flag=True, default=False, help="Create PC archives")
@click.option("--xbox", is_flag=True, default=False, help="Create Xbox archives")
@click.option("--ps", is_flag=True, default=False, help="Create PlayStation archives")
@click.option("--pc-max-res", type=int, default=0, help="Max texture dimension for PC (0 = no resize)")
@click.option("--pc-effects-max-res", type=int, default=None,
              help="Max texture dimension for Textures/Effects on PC (defaults to --pc-max-res)")
@click.option("--xbox-max-res", type=int, default=0, help="Max texture dimension for Xbox (0 = no resize)")
@click.option("--xbox-effects-max-res", type=int, default=None,
              help="Max texture dimension for Textures/Effects on Xbox (defaults to --xbox-max-res)")
@click.option("--ps-max-res", type=int, default=0, help="Max texture dimension for PlayStation (0 = no resize)")
@click.option("--ps-effects-max-res", type=int, default=None,
              help="Max texture dimension for Textures/Effects on PlayStation (defaults to --ps-max-res)")
@click.option("--use-archive2", is_flag=True, default=False, help="Use Archive2.exe instead of the native packer")
@click.option("--archive-max-size-gb", type=float, default=16.0,
              help="Maximum expanded archive size in GiB before splitting (default 16.0)")
@click.option("--expanded-archives", is_flag=True, default=False,
              help="Use family archive labels such as Meshes and Sounds instead of Main + Textures when possible.")
@click.option("--ba2-target", type=click.Choice(["auto", "og", "nextgen"]), default="auto", show_default=True,
              help="FO4 archive version: detect the install, force v1 (og), or v8 (nextgen).")
@click.pass_context
def pack(ctx, mod_name, pc, xbox, ps, pc_max_res, pc_effects_max_res, xbox_max_res, xbox_effects_max_res, ps_max_res, ps_effects_max_res, use_archive2, archive_max_size_gb, expanded_archives, ba2_target):
    """Pack BA2/BSA archives for a mod."""
    from creation_lib.build.archive_plan import gib_to_bytes
    from creation_lib.build.packer import pack_mod
    from app.paths import get_app_root, get_resource_dir
    from cli._output import output

    game = _resolve_mod_game(ctx, mod_name)
    try:
        archive_max_bytes = gib_to_bytes(archive_max_size_gb)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    # Default to --pc if neither specified
    if not pc and not xbox and not ps:
        pc = True
    result = pack_mod(mod_name, pc=pc, xbox=xbox, ps=ps,
             pc_max_res=pc_max_res, pc_effects_max_res=pc_effects_max_res,
             xbox_max_res=xbox_max_res, xbox_effects_max_res=xbox_effects_max_res,
             ps_max_res=ps_max_res, ps_effects_max_res=ps_effects_max_res,
             game=game, use_archive2=use_archive2,
             game_dir=os.environ.get(f"{game.upper()}_DIR", ""),
             project_root=get_app_root(),
             resource_dir=get_resource_dir(),
             archive_max_bytes=archive_max_bytes,
             fo4_ba2_target=ba2_target,
             expanded_archives=expanded_archives)
    output({"mod": mod_name, "game": game, "requested_ba2_target": ba2_target, "result": result}, ctx.obj["fmt"])


@build.command()
@click.argument("mod_name")
@click.option("--verbose", is_flag=True, help="Show detailed validation info")
@click.pass_context
def validate(ctx, mod_name, verbose):
    """Validate FormKey references in a mod's YAML files."""
    from creation_lib.esp.validate import validate_authoring

    _ = _resolve_mod_game(ctx, mod_name)
    click.echo(f"Validating {mod_name}...")

    from app.paths import get_app_root
    yaml_dir = get_app_root() / "mods" / mod_name / "yaml"
    errors, checked = validate_authoring(yaml_dir)
    if verbose:
        click.echo(f"Scanned {checked} FormKey reference(s) under {yaml_dir}")

    if errors:
        click.echo(f"\nVALIDATION ERRORS ({len(errors)}):\n")
        for err in errors:
            click.echo(f"  {err['file']}:{err['line']}")
            click.echo(f"    {err['formkey']} — {err['reason']}")
            click.echo()
        click.echo(f"{len(errors)} error(s) in {checked} FormKey references checked. Build aborted.")
        sys.exit(1)
    else:
        click.echo(f"OK — {checked} FormKey references validated, no errors.")


@build.command("strip-master")
@click.argument("esp_path")
@click.argument("master_name")
@click.option("--output", default=None, help="Output path (default: overwrites input)")
def strip_master(esp_path, master_name, output):
    """Strip a master reference from an ESP/ESM file header (experimental)."""
    from creation_lib.preprocessor.strip_master import strip_master as _strip_master

    if not _strip_master(esp_path, master_name, output):
        sys.exit(1)


@build.command("pack-hkx")
@click.argument("xml_path")
@click.argument("hkx_path")
@click.option("--verify", is_flag=True, help="Decode before writing and require identical normalized XML fields (including bindings).")
@click.pass_context
def pack_hkx(ctx, xml_path, hkx_path, verify):
    """Pack an XML behavior file to binary HKX."""
    from pathlib import Path
    from creation_lib.havok.native_runtime import load_native_module
    from cli._output import output

    try:
        xml = Path(xml_path).read_text(encoding="utf-8")
        native = load_native_module()
        if native is None:
            raise RuntimeError("Havok native backend unavailable")
        binary = native.xml_to_hkx(xml)
        if verify:
            from creation_lib.inspection.havok import graph_differences
            differences = graph_differences(xml, native.hkx_to_xml(binary))
            if differences:
                raise ValueError(f"HKX verification failed: {len(differences)} decoded field differences; first: {differences[0]}. Output was not written.")
        Path(hkx_path).write_bytes(binary)
        output({"output": str(Path(hkx_path).resolve()), "bytes": len(binary), "verified": verify}, ctx.obj["fmt"])
    except Exception as exc:
        raise click.ClickException(str(exc))


@build.command("unpack-hkx")
@click.argument("hkx_path")
@click.argument("xml_path", required=False, default=None)
def unpack_hkx(hkx_path, xml_path):
    """Unpack a binary HKX file to XML. Output path defaults to <stem>.xml beside the source."""
    from pathlib import Path
    from creation_lib._native.havok_native import hkx_to_xml

    try:
        xml = hkx_to_xml(Path(hkx_path).read_bytes())
        out = xml_path or str(Path(hkx_path).with_suffix(".xml"))
        Path(out).write_text(xml, encoding="utf-8")
        click.echo(f"Unpacked: {out}")
    except Exception as exc:
        raise click.ClickException(str(exc))


@build.command("gen-classxml")
def gen_classxml():
    """Generate per-version Havok classxml directories from FO4 descriptors + SDK patches."""
    # Offline asset-DB build script; not part of any user-facing production flow.
    from app.paths import get_app_root, get_resource_dir
    from creation_lib.havok.gen_classxml import main as _main
    _main(get_app_root(), get_resource_dir())
