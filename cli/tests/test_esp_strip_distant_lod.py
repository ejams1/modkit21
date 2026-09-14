"""CLI tests for `modkit esp strip-distant-lod`.

The command drops every MNAM ("Distant LOD") subrecord and clears the 0x8000
("Has Distant LOD") record-header flag. The round-trip test needs the native
`plugin_handle_record_flags` / `plugin_handle_set_record_flags` functions and
skips without them; the pure-transform unit test always runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.esp_commands import _DISTANT_LOD_FLAG, _strip_distant_lod_from_record
from cli.main import cli
from creation_lib.esp.plugin import Plugin
import creation_lib.esp.native_runtime as nr

_HAS_FLAG_NATIVE = nr.native_function_available(
    "plugin_handle_record_flags"
) and nr.native_function_available("plugin_handle_set_record_flags")


def test_strip_transform_drops_mnam_and_clears_flag() -> None:
    subrecords = [
        ("EDID", b"B21_LodStat\x00", None),
        ("MNAM", b"\x01\x02\x03\x04", None),
        ("mnam", b"\x05\x06", None),  # case-insensitive
        ("OBND", b"\x00" * 12, None),
    ]
    kept, mnam_removed, new_flags, flag_cleared = _strip_distant_lod_from_record(
        subrecords, 0x8000 | 0x0040
    )
    assert mnam_removed == 2
    assert flag_cleared is True
    assert new_flags == 0x0040  # only 0x8000 cleared, other bits preserved
    assert [sig for sig, _, _ in kept] == ["EDID", "OBND"]


def test_strip_transform_noop_when_no_mnam_no_flag() -> None:
    subrecords = [("EDID", b"B21_PlainStat\x00", None)]
    kept, mnam_removed, new_flags, flag_cleared = _strip_distant_lod_from_record(
        subrecords, 0x0000
    )
    assert mnam_removed == 0
    assert flag_cleared is False
    assert new_flags == 0x0000
    assert kept == subrecords


def test_strip_transform_clears_flag_without_mnam() -> None:
    subrecords = [("EDID", b"B21_Straggler\x00", None)]
    kept, mnam_removed, new_flags, flag_cleared = _strip_distant_lod_from_record(
        subrecords, _DISTANT_LOD_FLAG
    )
    assert mnam_removed == 0
    assert flag_cleared is True
    assert new_flags == 0
    assert kept == subrecords


def _make_plugin(tmp_path: Path) -> Path:
    path = tmp_path / "B21_DistantLod.esp"
    p = Plugin.new("B21_DistantLod.esp", game="fo4", masters=[])
    lod = p.new_record("STAT", form_id=0x00000800)
    lod.add_subrecord("EDID", b"B21_LodStat\x00")
    lod.add_subrecord("MNAM", b"\x01\x02\x03\x04")
    lod.flags = _DISTANT_LOD_FLAG
    p.add_record(lod)
    plain = p.new_record("STAT", form_id=0x00000801)
    plain.add_subrecord("EDID", b"B21_PlainStat\x00")
    p.add_record(plain)
    # Non-LOD record that also carries header bit 0x8000 (a different flag here).
    # The default scan must NOT touch it.
    refr = p.new_record("REFR", form_id=0x00000802)
    refr.add_subrecord("NAME", (0x00000800).to_bytes(4, "little"), semantic_type="formid")
    refr.flags = _DISTANT_LOD_FLAG
    p.add_record(refr)
    p.save(path)
    p.close()
    return path


def _run(*args: str):
    return CliRunner().invoke(cli, ["--game", "fo4", "esp", "strip-distant-lod", *args])


def _read_record(path: Path, signature: str, object_id: int) -> tuple[list[str], int]:
    r = Plugin.load(path, game="fo4")
    try:
        handle = r._rust_handle
        form_id = next(
            fid
            for fid in nr.plugin_handle_record_form_ids(handle, [signature])
            if (fid & 0x00FFFFFF) == object_id
        )
        subs = nr.plugin_handle_record_subrecords(handle, form_id) or []
        flags = nr.plugin_handle_record_flags(handle, form_id) or 0
        return [sig.upper() for sig, _, _ in subs], flags
    finally:
        r.close()


@pytest.mark.skipif(not _HAS_FLAG_NATIVE, reason="native flag funcs require _native.pyd rebuild")
def test_strip_distant_lod_round_trip(tmp_path: Path) -> None:
    path = _make_plugin(tmp_path)
    result = _run(str(path))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["records_changed"] == 1
    assert payload["mnam_removed"] == 1
    assert payload["flags_cleared"] == 1

    lod_subs, lod_flags = _read_record(path, "STAT", 0x800)
    assert "MNAM" not in lod_subs
    assert lod_flags & _DISTANT_LOD_FLAG == 0

    plain_subs, plain_flags = _read_record(path, "STAT", 0x801)
    assert plain_subs == ["EDID"]
    assert plain_flags == 0

    # Non-LOD REFR with 0x8000 set is outside the default sig set → untouched.
    refr_subs, refr_flags = _read_record(path, "REFR", 0x802)
    assert refr_subs == ["NAME"]
    assert refr_flags & _DISTANT_LOD_FLAG == _DISTANT_LOD_FLAG


@pytest.mark.skipif(not _HAS_FLAG_NATIVE, reason="native flag funcs require _native.pyd rebuild")
def test_strip_distant_lod_dry_run_does_not_save(tmp_path: Path) -> None:
    path = _make_plugin(tmp_path)
    before = path.read_bytes()
    result = _run(str(path), "--dry-run")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["records_changed"] == 1
    assert path.read_bytes() == before
