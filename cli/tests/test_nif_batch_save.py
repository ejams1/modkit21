import json
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli


def test_batch_saves_distinct_template_variants_without_changing_source(tmp_path, monkeypatch):
    from cli import _session

    monkeypatch.setattr(_session, "SESSION_DIR", str(tmp_path / "sessions"))
    template = Path(__file__).resolve().parents[2] / "mods/B21_TalesFromAppalachia/tools/workshop_icon_plane.nif"
    original = template.read_bytes()
    runner = CliRunner()
    opened = runner.invoke(cli, ["nif", "open", str(template)])
    assert opened.exit_code == 0, opened.output
    session_id = json.loads(opened.output)["session_id"]
    commands = []
    for name in ("first", "second"):
        commands.extend([
            {"tool": "modify", "args": {"session_id": session_id, "block_id": 2,
             "fields": {"Source Texture": f"Textures\\{name}.dds"}}},
            {"tool": "save", "args": {"session_id": session_id,
             "path": str(tmp_path / "variants" / f"{name}.nif")}},
        ])
    command_file = tmp_path / "commands.json"
    command_file.write_text(json.dumps(commands), encoding="utf-8")
    result = runner.invoke(cli, ["nif", "batch", "--file", str(command_file)])
    assert result.exit_code == 0, result.output
    assert all("error" not in row["result"] for row in json.loads(result.output)), result.output
    for name in ("first", "second"):
        inspected = runner.invoke(cli, ["nif", "inspect", "--path",
            str(tmp_path / "variants" / f"{name}.nif"), "--block", "2"])
        assert json.loads(inspected.output)["fields"]["Source Texture"] == f"Textures\\{name}.dds"
    assert template.read_bytes() == original
    runner.invoke(cli, ["nif", "close", session_id])
