"""`modkit swf pack` must not write a SWF whose SymbolClass names dangle.

Emitting the classes and validating that they were emitted are deliberately
separate: the check is what turns a one-off fix into a rule, so a future
regression in the emitter fails the pack instead of shipping a widget the engine
cannot construct.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli

SQUARE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<path d="M 0 0 L 10 0 L 10 10 L 0 10 Z" fill="#ffffff"/>'
    "</svg>"
)

PROJECT = {
    "canvas": [90, 16],
    "shapes": [{"id": 1, "svg": "square.svg"}],
    "sprites": [{"id": 10, "frames": [
        {"label": "stars0", "place": [{"depth": 1, "character": 1}]},
    ]}],
    "stage": [{"place": [{"depth": 1, "character": 10, "name": "starRow"}]}],
    "exports": [{"character": 10, "class": "B21_StarRow"},
                {"character": 0, "class": "B21_StarWidget"}],
}


def _project_dir(tmp_path: Path) -> Path:
    (tmp_path / "square.svg").write_text(SQUARE_SVG, encoding="utf-8")
    (tmp_path / "widget.swfproj").write_text(json.dumps(PROJECT), encoding="utf-8")
    return tmp_path


def test_pack_writes_a_swf_whose_exports_are_all_backed(tmp_path: Path) -> None:
    project = _project_dir(tmp_path) / "widget.swfproj"
    out = tmp_path / "widget.swf"

    result = CliRunner().invoke(cli, ["swf", "pack", str(project), "-o", str(out)])

    assert result.exit_code == 0, result.output
    validated = CliRunner().invoke(cli, ["swf", "symbols", "validate", str(out)])
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output) == {
        "symbols": 2, "defined_classes": 2, "unbacked": [],
    }


def test_pack_refuses_when_the_emitted_classes_do_not_match_the_exports(
    tmp_path: Path, monkeypatch
) -> None:
    from creation_lib.swf import native_runtime

    real = native_runtime.build_movieclip_class_doabc
    monkeypatch.setattr(native_runtime, "build_movieclip_class_doabc",
                        lambda names: real(["B21_DriftedName"]))

    project = _project_dir(tmp_path) / "widget.swfproj"
    out = tmp_path / "widget.swf"

    result = CliRunner().invoke(cli, ["swf", "pack", str(project), "-o", str(out)])

    assert result.exit_code == 2, result.output
    assert "B21_StarRow" in result.output and "B21_StarWidget" in result.output
    assert not out.exists(), "a SWF with dangling bindings must not reach disk"


def test_symbols_validate_exits_nonzero_on_an_unbacked_name(tmp_path: Path) -> None:
    from creation_lib.swf.project import build_document
    from creation_lib.swf.tags import TAG_DO_ABC, RawTag
    from creation_lib.swf.writer import write_swf

    (tmp_path / "square.svg").write_text(SQUARE_SVG, encoding="utf-8")
    doc = build_document(PROJECT, tmp_path)
    doc.tags = [t for t in doc.tags
                if not (isinstance(t, RawTag) and t.tag_id == TAG_DO_ABC)]
    broken = tmp_path / "broken.swf"
    broken.write_bytes(write_swf(doc))

    result = CliRunner().invoke(cli, ["swf", "symbols", "validate", str(broken)])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["unbacked"] == ["B21_StarRow", "B21_StarWidget"]
