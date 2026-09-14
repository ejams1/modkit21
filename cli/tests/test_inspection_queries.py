from __future__ import annotations

import json
import struct

from click.testing import CliRunner

from cli.main import cli
from creation_lib.esp import Plugin


def invoke(*args):
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, result.output or repr(result.exception)
    return json.loads(result.stdout)


def string(value):
    data = value.encode()
    return struct.pack("<H", len(data)) + data


def script(name, properties=b"", count=0):
    return string(name) + struct.pack("<BH", 0, count) + properties


def make_plugin(path):
    plugin = Plugin.new(path.name, game="fo4", masters=[])
    prop = string("Message") + bytes([2, 1]) + string("B21_NotAnAttachment")
    vmad = struct.pack("<HHH", 6, 2, 1) + script("B21_ActorScript", prop, 1)
    rec = plugin.new_record("NPC_", form_id=0xFF000800)
    rec.editor_id = "B21_Actor"
    rec.flags |= 0x40000
    rec.add_subrecord("VMAD", vmad)
    plugin.add_record(rec)
    quest = plugin.new_record("QUST", form_id=0xFF000801)
    quest.editor_id = "B21_Quest"
    tail = bytes([3]) + struct.pack("<H", 1) + script("B21_QuestFragments")
    tail += struct.pack("<Hhib", 20, 0, 0, 0) + string("B21_QuestFragments") + string("Fragment_0")
    tail += struct.pack("<H", 1) + struct.pack("<HhIhhH", 0, 7, 0x00000801, 6, 2, 1) + script("B21_AliasScript")
    quest.add_subrecord("VMAD", struct.pack("<HHH", 6, 2, 0) + tail)
    plugin.add_record(quest)
    bad = plugin.new_record("ACTI", form_id=0xFF000802)
    bad.editor_id = "B21_Bad"
    bad.add_subrecord("VMAD", b"\x06\x00")
    plugin.add_record(bad)
    plugin.save(path)
    plugin.close()


def test_vmad_exact_bindings_compressed_record_alias_and_fragment(tmp_path):
    path = tmp_path / "B21_Inspect.esp"
    make_plugin(path)
    report = invoke("esp", "vmad", str(path))
    rows = report["data"]
    assert {(r["script"], r["scope"]) for r in rows} == {
        ("B21_ActorScript", "record"), ("B21_QuestFragments", "fragment_script"),
        ("B21_QuestFragments", "fragment"), ("B21_AliasScript", "alias"),
    }
    actor = next(row for row in rows if row["script"] == "B21_ActorScript")
    assert actor["properties"][0]["Value"] == "B21_NotAnAttachment"
    assert actor["properties"][0]["Type"] == "String"
    assert not report["meta"]["complete"]
    assert report["meta"]["issues"][0]["editor_id"] == "B21_Bad"
    assert invoke("esp", "vmad", str(path), "B21_Actor", "--script", "b21_actorscript")["meta"]["complete"]
    assert not invoke("esp", "vmad", str(path), "--script", "B21_NotAnAttachment")["data"]
    result = CliRunner().invoke(cli, ["esp", "vmad", str(path), "--fail-on-incomplete"])
    assert result.exit_code == 1


def test_query_projection_filter_paging_and_grouping(tmp_path):
    path = tmp_path / "B21_Query.esp"
    make_plugin(path)
    report = invoke("--fields", "eid,signature", "--where", "eid~B21_*", "--limit", "1", "--offset", "1", "esp", "query", str(path))
    assert report["meta"]["matched"] == 3
    assert report["meta"]["returned"] == 1
    assert report["meta"]["next_offset"] == 2
    assert set(report["data"][0]) == {"eid", "signature"}
    grouped = invoke("--group-by", "scope", "esp", "vmad", str(path))
    assert {r["value"] for r in grouped["data"]} == {"record", "fragment_script", "fragment", "alias"}
    counted = invoke("--count-only", "esp", "vmad", str(path), "--script-glob", "*Alias*")
    assert counted["data"] == []
    assert counted["meta"]["matched"] == 1


def test_jsonl_artifact_and_structured_errors(tmp_path):
    path = tmp_path / "B21_Output.esp"
    make_plugin(path)
    out = tmp_path / "rows.jsonl"
    result = invoke("--format", "jsonl", "--output", str(out), "esp", "vmad", str(path))
    assert result["artifact"]["path"] == str(out.resolve())
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [row["kind"] for row in lines] == ["row"] * 4 + ["meta"]
    error = CliRunner().invoke(cli, ["--where", "broken", "esp", "query", str(path)])
    assert error.exit_code == 2
    assert json.loads(error.stderr)["error"]["code"] == "INVALID_ARGUMENT"
    assert error.stdout == ""
    error = CliRunner().invoke(cli, ["esp", "vmad", str(path), "MissingRecord"])
    assert error.exit_code == 1
    assert json.loads(error.stderr)["error"]["code"] == "COMMAND_FAILED"
    before = path.read_bytes()
    error = CliRunner().invoke(cli, ["--output", str(path), "esp", "vmad", str(path)])
    assert error.exit_code == 2
    assert path.read_bytes() == before
