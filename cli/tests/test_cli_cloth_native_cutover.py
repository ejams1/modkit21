"""Smoke tests for Slice B2: cli/cloth_commands.py native cutover.

These tests verify that the four cut-over commands (extract, pack, validate,
bake) correctly delegate to creation_lib._native.havok_native.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from click.testing import CliRunner

from cli.main import cli


FIXTURES_DIR = Path(__file__).parents[2] / "py_creation_lib" / "tests" / "fixtures" / "cloth"
BATHROBE_NIF = FIXTURES_DIR / "bathrobe_outfitm.nif"


def _bathrobe_or_skip() -> Path:
    if not BATHROBE_NIF.is_file():
        pytest.skip(f"bathrobe fixture not present: {BATHROBE_NIF}")
    return BATHROBE_NIF


# ---------------------------------------------------------------------------
# test_cloth_extract_emits_blob
# ---------------------------------------------------------------------------

def test_cloth_extract_emits_blob(tmp_path: Path):
    """cloth extract writes a non-empty HKX blob file and exits 0."""
    nif = _bathrobe_or_skip()
    out = tmp_path / "blob.hkx"

    runner = CliRunner()
    result = runner.invoke(cli, ["cloth", "extract", str(nif), "-o", str(out)])

    assert result.exit_code == 0, f"stderr:\n{result.output}"
    assert out.is_file(), "output blob file was not created"
    assert out.stat().st_size > 0, "output blob is empty"


# ---------------------------------------------------------------------------
# test_cloth_pack_round_trips
# ---------------------------------------------------------------------------

def test_cloth_pack_round_trips(tmp_path: Path):
    """Extract blob then re-pack unchanged; resulting NIF must be BYTE-IDENTICAL to original."""
    nif = _bathrobe_or_skip()

    blob_path = tmp_path / "blob.hkx"
    runner = CliRunner()
    r1 = runner.invoke(cli, ["cloth", "extract", str(nif), "-o", str(blob_path)])
    assert r1.exit_code == 0, f"extract failed:\n{r1.output}"

    out_nif = tmp_path / "repacked.nif"
    r2 = runner.invoke(
        cli,
        ["cloth", "pack", str(blob_path), "--into", str(nif), "-o", str(out_nif)],
    )
    assert r2.exit_code == 0, f"pack failed:\n{r2.output}"
    assert out_nif.is_file()

    original = nif.read_bytes()
    repacked = out_nif.read_bytes()
    assert original == repacked, (
        f"Byte-identity FAILED: original={len(original)} bytes, "
        f"repacked={len(repacked)} bytes"
    )


# ---------------------------------------------------------------------------
# test_cloth_validate_reports_ok
# ---------------------------------------------------------------------------

def test_cloth_validate_reports_ok(tmp_path: Path):
    """cloth validate on the bathrobe fixture exits 0 (valid cloth data)."""
    nif = _bathrobe_or_skip()

    runner = CliRunner()
    result = runner.invoke(cli, ["cloth", "validate", str(nif)])

    assert result.exit_code == 0, f"validate exited {result.exit_code}:\n{result.output}"


def test_cloth_inspect_resolves_numeric_pointers_without_object_names(monkeypatch, tmp_path: Path):
    nif = tmp_path / "cloth.nif"
    nif.write_bytes(b"nif")
    graph = {
        "objects": [
            {
                "name": "",
                "class": "hclClothData",
                "members": {
                    "name": "ConvertedCloth",
                    "simClothDatas": ["#0001"],
                    "operators": ["#0002"],
                },
            },
            {
                "name": "",
                "class": "hclSimClothData",
                "members": {
                    "particleDatas": [{}, {}],
                    "fixedParticles": [0],
                    "staticConstraintSets": ["#0003"],
                    "perInstanceCollidables": [],
                },
            },
            {"name": "", "class": "hclSimulateOperator", "members": {}},
            {"name": "", "class": "hclStandardLinkConstraintSet", "members": {}},
            {"name": "", "class": "hclClothState", "members": {}},
        ]
    }
    fake_havok_native = SimpleNamespace(
        cloth_inspect_blob_json=lambda _blob: json.dumps(graph),
    )
    fake_nif_native = SimpleNamespace(cloth_extract_blob=lambda _data: b"blob")
    monkeypatch.setitem(
        sys.modules,
        "creation_lib._native",
        SimpleNamespace(havok_native=fake_havok_native, nif_core_native=fake_nif_native),
    )

    result = CliRunner().invoke(cli, ["--format", "json", "cloth", "inspect", str(nif)])

    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["num_sim_cloths"] == 1
    assert summary["total_particles"] == 2
    assert summary["num_constraint_sets"] == 1
    assert summary["num_operators"] == 1
    assert summary["num_cloth_states"] == 1


def test_cloth_solve_csv_frame_zero_uses_full_particle_set(monkeypatch, tmp_path: Path):
    nif = tmp_path / "cloth.nif"
    nif.write_bytes(b"nif")
    out = tmp_path / "trace.csv"

    def simulate(_blob, _frames, _config):
        return json.dumps({
            "positions": [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            "n_particles": 2,
            "fixed_count": 1,
        })

    fake_havok_native = SimpleNamespace(
        cloth_simulate_from_blob=simulate,
    )
    fake_nif_native = SimpleNamespace(cloth_extract_blob=lambda _data: b"blob")
    monkeypatch.setitem(
        sys.modules,
        "creation_lib._native",
        SimpleNamespace(havok_native=fake_havok_native, nif_core_native=fake_nif_native),
    )
    monkeypatch.setattr(np, "ones", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback mask")))

    result = CliRunner().invoke(cli, ["cloth", "solve", str(nif), "-n", "0", "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").splitlines() == [
        "frame,com_x,com_y,com_z",
        "0,1.0000,0.0000,0.0000",
    ]
