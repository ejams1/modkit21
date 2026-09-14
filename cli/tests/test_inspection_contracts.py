from __future__ import annotations

import json
import os

from click.testing import CliRunner

from cli.main import cli
from cli.tests.test_inspection_queries import invoke
from cli.tests.test_esp_diff import _plugin
from creation_lib.inspection.records import field_differences, normalize_references


def test_semantic_diff_filters_decoded_fields_and_exclusions(tmp_path):
    left, right = tmp_path / "B21_A.esp", tmp_path / "B21_B.esp"
    _plugin(left, [(0x800, "WEAP", "B21_Item", b"Old name\0"), (0x801, "WEAP", "B21_Other", b"First name\0")])
    _plugin(right, [(0x800, "WEAP", "B21_Item", b"New name\0"), (0x801, "WEAP", "B21_Other", b"Second name\0")])
    report = invoke("esp", "diff", str(left), str(right), "--semantic", "--record", "B21_Item")
    assert report["counts"] == {"added": 0, "removed": 0, "changed": 1}
    changes = report["changes"][0]["changes"]
    assert any(change["before"] == "Old name" and change["after"] == "New name" for change in changes)
    field = next(change["path"] for change in changes if change["after"] == "New name")
    selected = invoke("esp", "diff", str(left), str(right), "--semantic", "--record", "800", "--field", field)
    assert [change["path"] for change in selected["changes"][0]["changes"]] == [field]
    ignored = invoke("esp", "diff", str(left), str(right), "--semantic", "--exclude", field)
    assert ignored["counts"]["changed"] == 0


def test_semantic_presence_array_entries_and_reference_normalization():
    changes = field_differences({"fields": {}}, {"fields": {"Value": None}}, fields=["fields.Value"])
    assert changes == [{"path": "fields.Value", "before": None, "after": None, "before_present": False, "after_present": True}]
    changes = field_differences({"array": [1]}, {"array": [1, 2]})
    assert changes[0]["path"] == "array.1"
    assert not changes[0]["before_present"]
    assert field_differences({}, {"empty": {}})[0]["path"] == "empty"
    assert field_differences({"value": True}, {"value": 1})[0]["path"] == "value"
    first = {"reference": {"plugin": "B21_A.esp", "object_id": "800"}}
    second = {"reference": {"plugin": "B21_B.esp", "object_id": "000800"}}
    assert normalize_references(first, "B21_A.esp") == normalize_references(second, "B21_B.esp")


def test_discovery_and_doctor_report_real_commands_and_input_age(tmp_path):
    catalog = invoke("capabilities", "--command", "esp vmad")
    assert [row["command"] for row in catalog["commands"]] == ["modkit esp vmad"]
    matches = invoke("help", "search", "script bindings")
    assert [row["command"] for row in matches["matches"]] == ["modkit esp vmad"]
    source, index = tmp_path / "B21_Source.esp", tmp_path / "records.db"
    source.write_bytes(b"source timestamp fixture")
    index.write_bytes(b"index timestamp fixture")
    cache = tmp_path / "exported-cache"
    cache.mkdir()
    (cache / "unrelated.db").write_bytes(b"not a configured index")
    os.utime(index, (10, 10))
    doctor = invoke("--db-dir", str(tmp_path), "doctor", "--source", str(source))
    assert doctor["cli_source_freshness"] == "source"
    assert doctor["indexes"][0]["freshness"] == "older_than_inputs"
    assert len(doctor["indexes"]) == 1
    assert doctor["native"][0]["available"]


def test_trace_is_json_and_can_be_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    mod = tmp_path / "mods/B21_Trace"
    mod.mkdir(parents=True)
    (mod / "asset_provenance.jsonl").write_text(json.dumps({"asset_path": "meshes/test.nif", "added_by_record_eid": "B21_Owner"}) + "\n", encoding="utf-8")
    report = invoke("data", "trace", "B21_Trace", "--asset", "test.nif")
    assert report["matches"][0]["asset_path"] == "meshes/test.nif"
    summary = invoke("--fields", "editor_id,asset_count", "data", "trace", "B21_Trace")
    assert summary["data"] == [{"editor_id": "B21_Owner", "asset_count": 1}]


def test_report_projection_applies_to_existing_inspection_commands(tmp_path):
    path = tmp_path / "B21_Legacy.esp"
    _plugin(path, [(0x800, "WEAP", "B21_First", None), (0x801, "WEAP", "B21_Second", None)])
    result = invoke("--fields", "editor_id", "--limit", "1", "esp", "list-records", str(path))
    assert result["data"] == [{"editor_id": "B21_First"}]
    assert result["meta"]["truncated"]
    failed = CliRunner().invoke(cli, ["--format", "table", "esp", "get-record", str(path), "NotFound"])
    assert failed.exit_code == 1
    assert failed.stderr.startswith("Error:")
