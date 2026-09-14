from __future__ import annotations

import json
import struct
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli
from creation_lib.esp import Group, Plugin, PluginHeader, Record, Subrecord
from creation_lib.esp import native_runtime
from creation_lib.esp.editor.validate import Issue, IssueCategory, Severity, ValidationReport


def _make_plugin(path: Path) -> Plugin:
    plugin = Plugin.new(path.name, game="fo4")
    plugin.header.author = "CLI Test"
    record = plugin.new_record("MISC")
    record.editor_id = "CliRecord"
    record.full_name = "CLI Record"
    plugin.add_record(record)
    plugin.save(path)
    return plugin


def _make_cell_plugin(path: Path) -> None:
    cell_form_id = 0x01000800
    header = PluginHeader(masters=["Fallout4.esm"], master_sizes=[0])
    cell = Record(
        "CELL",
        cell_form_id,
        subrecords=[Subrecord("EDID", b"CliCell\0")],
    )
    placed = Record("REFR", 0x01000801)
    label = cell_form_id.to_bytes(4, "little")
    plugin = Plugin(
        plugin_name=path.name,
        file_path=path,
        game="fo4",
        header=header,
        root_items=[
            Group(
                b"CELL",
                0,
                children=[
                    Group(
                        (0).to_bytes(4, "little", signed=True),
                        2,
                        children=[
                            Group(
                                (0).to_bytes(4, "little", signed=True),
                                3,
                                children=[
                                    cell,
                                    Group(
                                        label,
                                        6,
                                        children=[Group(label, 9, children=[placed])],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    plugin.save(path)


def _make_world_plugin(path: Path) -> None:
    world_form_id = 0x01000900
    cell_form_id = 0x01000910
    header = PluginHeader(masters=["Fallout4.esm"], master_sizes=[0])
    world = Record(
        "WRLD",
        world_form_id,
        subrecords=[Subrecord("EDID", b"CliWorld\0")],
    )
    cell = Record(
        "CELL",
        cell_form_id,
        subrecords=[
            Subrecord("EDID", b"CliWorld_0_0\0"),
            Subrecord("XCLC", struct.pack("<iiI", 0, 0, 0)),
        ],
    )
    land = Record("LAND", 0x01000930)
    cell_label = cell_form_id.to_bytes(4, "little")
    cell_children = Group(
        cell_label,
        6,
        children=[Group(cell_label, 9, children=[land])],
    )
    world_children = Group(
        world_form_id.to_bytes(4, "little"),
        1,
        children=[
            Group(
                struct.pack("<hh", 0, 0),
                4,
                children=[
                    Group(
                        struct.pack("<hh", 0, 0),
                        5,
                        children=[cell, cell_children],
                    )
                ],
            )
        ],
    )
    plugin = Plugin(
        plugin_name=path.name,
        file_path=path,
        game="fo4",
        header=header,
        root_items=[Group(b"WRLD", 0, children=[world, world_children])],
    )
    plugin.save(path)


def test_esp_export_and_import_roundtrip(tmp_path) -> None:
    plugin_path = tmp_path / "CliTest.esp"
    original = _make_plugin(plugin_path)
    export_path = tmp_path / "CliTest.lossless.yaml"
    rebuilt_path = tmp_path / "CliTest.rebuilt.esp"

    runner = CliRunner()
    export_result = runner.invoke(
        cli,
        ["--game", "fo4", "esp", "export", str(plugin_path), "--mode", "lossless", "--output", str(export_path)],
    )
    assert export_result.exit_code == 0, export_result.output
    assert export_path.is_file()

    import_result = runner.invoke(
        cli,
        ["esp", "import", str(export_path), "--output", str(rebuilt_path)],
    )
    assert import_result.exit_code == 0, import_result.output
    rebuilt = Plugin.load(rebuilt_path, game="fo4")
    assert rebuilt.to_bytes() == original.to_bytes()


def _make_plugin_with_records(path: Path) -> Plugin:
    plugin = Plugin.new(path.name, game="fo4")
    for editor_id in ("CliRecordOne", "CliRecordTwo"):
        record = plugin.new_record("MISC")
        record.editor_id = editor_id
        record.full_name = editor_id
        plugin.add_record(record)
    plugin.save(path)
    return plugin


def test_esp_list_records_emits_eid_and_local_form_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliList.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MISC"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["type"] == "MISC"
    assert payload["count"] == 2
    editor_ids = {rec["editor_id"] for rec in payload["records"]}
    assert editor_ids == {"CliRecordOne", "CliRecordTwo"}
    for rec in payload["records"]:
        assert rec["signature"] == "MISC"
        assert len(rec["form_id"]) == 6
        int(rec["form_id"], 16)  # 6-hex local object id


def test_esp_list_records_accepts_display_alias(tmp_path) -> None:
    plugin_path = tmp_path / "CliAlias.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MiscItems"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["type"] == "MISC"
    assert payload["count"] == 2


def test_esp_list_records_filters_and_emits_subrecord_data(tmp_path) -> None:
    plugin_path = tmp_path / "CliSubrecords.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "esp",
            "list-records",
            str(plugin_path),
            "--type",
            "MISC",
            "--type",
            "KYWD",
            "--has-subrecord",
            "EDID",
            "--include-subrecord-data",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 2
    assert payload["type"] is None
    assert payload["types"] == ["MISC", "KYWD"]
    assert payload["subrecords"] == ["EDID"]
    assert all(record["subrecord_data"]["EDID"] for record in payload["records"])


def test_esp_cell_children_lazy_matches_eager_for_plugin_with_master(tmp_path) -> None:
    plugin_path = tmp_path / "CliCell.esp"
    _make_cell_plugin(plugin_path)
    runner = CliRunner()

    lazy_result = runner.invoke(
        cli,
        ["--game", "fo4", "esp", "cell-children", str(plugin_path), "CliCell", "--lazy"],
    )
    eager_result = runner.invoke(
        cli,
        ["--game", "fo4", "esp", "cell-children", str(plugin_path), "CliCell", "--eager"],
    )

    assert lazy_result.exit_code == 0, lazy_result.output
    assert eager_result.exit_code == 0, eager_result.output
    lazy_payload = json.loads(lazy_result.output)
    eager_payload = json.loads(eager_result.output)
    assert lazy_payload["cell_form_id"] == "01000800"
    assert lazy_payload["children"] == eager_payload["children"], (
        lazy_payload,
        eager_payload,
    )
    assert lazy_payload["children"] == [
        {
            "form_id": 0x01000801,
            "form_key": "CliCell.esp:000801",
            "signature": "REFR",
            "group_type": 9,
        }
    ]


def test_esp_cell_slice_roots_selects_world_coordinate_bounds(tmp_path) -> None:
    plugin_path = tmp_path / "CliWorld.esp"
    _make_world_plugin(plugin_path)

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "esp",
            "cell-slice-roots",
            str(plugin_path),
            "CliWorld",
            "--min-x",
            "-1",
            "--min-y",
            "-1",
            "--max-x",
            "1",
            "--max-y",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["worldspace_form_keys"] == ["CliWorld.esp:000900"]
    assert payload["cell_form_keys"] == ["CliWorld.esp:000910"]
    assert payload["cell_grids"] == {
        "CliWorld.esp:000910": {"x": 0, "y": 0}
    }
    assert payload["cell_children"]["CliWorld.esp:000910"] == {
        "Persistent": [],
        "Temporary": [],
    }


def test_esp_get_record_by_editor_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliGetEid.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "get-record", str(plugin_path), "CliRecordOne"])
    assert result.exit_code == 0, result.output
    assert "CliRecordOne" in result.output
    assert "MISC" in result.output


def test_esp_get_record_by_local_form_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliGetFid.esp"
    _make_plugin_with_records(plugin_path)

    listed = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MISC"])
    assert listed.exit_code == 0, listed.output
    first = json.loads(listed.output)["records"][0]

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "get-record", str(plugin_path), first["form_id"]])
    assert result.exit_code == 0, result.output
    assert first["editor_id"] in result.output


def test_esp_get_record_not_found_exits_nonzero(tmp_path) -> None:
    plugin_path = tmp_path / "CliGetMiss.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "get-record", str(plugin_path), "NoSuchThing"])
    assert result.exit_code == 1, result.output
    assert "not a FormID or EditorID" in result.output or "not found" in result.output


def test_esp_get_records_reads_multiple_records_with_one_open(tmp_path) -> None:
    plugin_path = tmp_path / "CliGetMany.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "esp",
            "get-records",
            str(plugin_path),
            "CliRecordOne",
            "NoSuchThing",
            "CliRecordTwo",
            "--authoring",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["requested"] == 3
    assert payload["found"] == 2
    assert payload["missing"] == ["NoSuchThing"]
    assert {record["eid"] for record in payload["records"]} == {
        "CliRecordOne",
        "CliRecordTwo",
    }


def test_esp_inspect_outputs_summary(tmp_path) -> None:
    plugin_path = tmp_path / "CliInspect.esp"
    _make_plugin(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "inspect", str(plugin_path)])
    assert result.exit_code == 0, result.output
    assert "CliInspect.esp" in result.output
    assert "record_count" in result.output


def test_esp_export_semantic_includes_record_semantics(tmp_path) -> None:
    plugin_path = tmp_path / "CliSemantic.esp"
    plugin = Plugin.new(plugin_path.name, game="fo4")
    quest = plugin.new_record("QUST")
    quest.editor_id = "QuestSemantic"
    quest.full_name = "Semantic Quest"
    quest.add_subrecord("INDX", b"\x0A\x00")
    quest.add_subrecord("CNAM", b"Quest Log\x00")
    plugin.add_record(quest)
    plugin.save(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "export", str(plugin_path), "--mode", "semantic"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["items"][0]["children"][0]["semantic"]["kind"] == "quest"


def test_esp_export_authoring_and_build_roundtrip(tmp_path) -> None:
    plugin_path = tmp_path / "CliAuthoring.esp"
    plugin = Plugin.new(plugin_path.name, game="fo4")
    record = plugin.new_record("DOBJ")
    record.add_subrecord("DNAM", b"\x41\x41\x41\x43\x45\x23\x01\x00")
    plugin.add_record(record)
    plugin.save(plugin_path)

    authoring_path = tmp_path / "CliAuthoring.yaml"
    rebuilt_path = tmp_path / "CliAuthoring.rebuilt.esp"
    runner = CliRunner()

    export_result = runner.invoke(
        cli,
        ["--game", "fo4", "esp", "export", str(plugin_path), "--mode", "authoring", "--output", str(authoring_path)],
    )
    assert export_result.exit_code == 0, export_result.output
    assert authoring_path.is_file()
    text = authoring_path.read_text(encoding="utf-8")
    assert "row_array" in text
    assert "AAAC" in text

    build_result = runner.invoke(
        cli,
        ["esp", "build", str(authoring_path), "--output", str(rebuilt_path)],
    )
    assert build_result.exit_code == 0, build_result.output
    rebuilt = Plugin.load(rebuilt_path, game="fo4")
    assert rebuilt.to_bytes() == plugin.to_bytes()


def test_esp_export_authoring_dir_and_build_authoring_roundtrip(tmp_path) -> None:
    plugin_path = tmp_path / "CliAuthoringDir.esp"
    plugin = Plugin.new(plugin_path.name, game="fo4")
    record = plugin.new_record("DOBJ")
    record.editor_id = "CliAuthoringDir"
    record.add_subrecord("DNAM", b"\x41\x41\x41\x43\x45\x23\x01\x00")
    plugin.add_record(record)
    plugin.save(plugin_path)

    authoring_dir = tmp_path / "authoring"
    rebuilt_path = tmp_path / "CliAuthoringDir.rebuilt.esp"
    runner = CliRunner()

    export_result = runner.invoke(
        cli,
        ["--game", "fo4", "esp", "export-authoring", str(plugin_path), "--output-dir", str(authoring_dir), "--jobs", "2", "--format", "yaml"],
    )
    assert export_result.exit_code == 0, export_result.output
    assert (authoring_dir / "plugin.yaml").is_file()
    assert not (authoring_dir / "structure.yaml").exists()
    record_files = list((authoring_dir / "records").rglob("*.yaml"))
    assert len(record_files) == 1
    record_text = record_files[0].read_text(encoding="utf-8")
    assert "CliAuthoringDir" in record_text
    assert "codec:" not in record_text
    assert "preservation_mode: typed" not in record_text

    build_result = runner.invoke(
        cli,
        ["esp", "build-authoring", str(authoring_dir), "--output", str(rebuilt_path)],
    )
    assert build_result.exit_code == 0, build_result.output
    rebuilt = Plugin.load(rebuilt_path, game="fo4")
    assert rebuilt.to_bytes() == plugin.to_bytes()


def test_esp_check_errors_outputs_validation_report_and_fails_on_issues(tmp_path, monkeypatch) -> None:
    plugin_path = tmp_path / "Bad.esp"
    plugin_path.write_bytes(b"TES4")
    loaded = type("Loaded", (), {"handle": 42, "plugin_name": "Bad.esp", "game": "fo4"})()

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            assert "toolkit_settings" not in kwargs or kwargs["toolkit_settings"] is None
            assert kwargs["master_search_paths"] == [plugin_path.parent]

        def load(self, path, *, game=None, as_master=None):
            assert Path(path) == plugin_path
            assert game == "fo4"
            return loaded

        def close_all(self):
            self.closed = True

    def fake_validate(session, *, handle=None):
        assert handle == loaded.handle
        report = ValidationReport()
        report.add(
            Issue(
                severity=Severity.ERROR,
                category=IssueCategory.FIELD_ERROR,
                plugin_handle=loaded.handle,
                plugin_name=loaded.plugin_name,
                form_id=0x02000802,
                message="RACE \\ VTCK - Voices -> Found a NULL reference, expected: VTYP",
            )
        )
        return report

    monkeypatch.setattr("creation_lib.esp.editor.EditorSession", FakeSession)
    monkeypatch.setattr("creation_lib.esp.editor.validate", fake_validate)
    monkeypatch.setattr("cli.esp_commands._resolve_game_data_dir", lambda _game: None)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "check-errors", str(plugin_path)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["plugin"] == "Bad.esp"
    assert payload["issue_count"] == 1
    assert "Found a NULL reference" in result.output


def test_esp_check_errors_limits_reported_errors(tmp_path, monkeypatch) -> None:
    plugin_path = tmp_path / "Bad.esp"
    plugin_path.write_bytes(b"TES4")
    loaded = type("Loaded", (), {"handle": 42, "plugin_name": "Bad.esp", "game": "fo4"})()

    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs["master_search_paths"] == [plugin_path.parent]

        def load(self, path, *, game=None, as_master=None):
            return loaded

        def close_all(self):
            pass

    report = ValidationReport()
    for index in range(3):
        report.add(
            Issue(
                severity=Severity.ERROR,
                category=IssueCategory.FIELD_ERROR,
                plugin_handle=loaded.handle,
                plugin_name=loaded.plugin_name,
                form_id=0x02000800 + index,
                message=f"Error {index + 1}",
            )
        )
    report.add(
        Issue(
            severity=Severity.WARNING,
            category=IssueCategory.FIELD_ERROR,
            plugin_handle=loaded.handle,
            plugin_name=loaded.plugin_name,
            form_id=None,
            message="Warning 1",
        )
    )
    monkeypatch.setattr("creation_lib.esp.editor.EditorSession", FakeSession)
    monkeypatch.setattr("creation_lib.esp.editor.validate", lambda _session, *, handle=None: report)
    monkeypatch.setattr("cli.esp_commands._resolve_game_data_dir", lambda _game: None)

    result = CliRunner().invoke(cli, ["esp", "check-errors", str(plugin_path), "--max-errors", "2"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["issue_count"] == 4
    assert payload["error_count"] == 3
    assert payload["warning_count"] == 1
    assert payload["max_errors"] == 2
    assert payload["displayed_issue_count"] == 2
    assert payload["omitted_error_count"] == 1
    assert payload["omitted_warning_count"] == 1
    assert [issue["message"] for issue in payload["issues"]] == ["Error 1", "Error 2"]


def test_esp_check_errors_can_succeed_with_no_fail(tmp_path, monkeypatch) -> None:
    plugin_path = tmp_path / "Bad.esp"
    plugin_path.write_bytes(b"TES4")
    loaded = type("Loaded", (), {"handle": 42, "plugin_name": "Bad.esp", "game": "fo4"})()

    class FakeSession:
        def __init__(self, **kwargs):
            assert "toolkit_settings" not in kwargs or kwargs["toolkit_settings"] is None
            assert kwargs["master_search_paths"] == [plugin_path.parent]

        def load(self, path, *, game=None, as_master=None):
            return loaded

        def close_all(self):
            pass

    report = ValidationReport()
    report.add(
        Issue(
            severity=Severity.WARNING,
            category=IssueCategory.FIELD_ERROR,
            plugin_handle=loaded.handle,
            plugin_name=loaded.plugin_name,
            message="Warning: Unused data",
            form_id=None,
        )
    )
    monkeypatch.setattr("creation_lib.esp.editor.EditorSession", FakeSession)
    monkeypatch.setattr("creation_lib.esp.editor.validate", lambda _session, *, handle=None: report)
    monkeypatch.setattr("cli.esp_commands._resolve_game_data_dir", lambda _game: None)

    result = CliRunner().invoke(cli, ["esp", "check-errors", str(plugin_path), "--no-fail"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["warning_count"] == 1


# --- set-record / delete-record ----------------------------------------------


def _local_object_id(plugin: Plugin, editor_id: str) -> str:
    return plugin.eid_index()[editor_id.lower()][0].split(":")[-1]


def test_esp_delete_record_by_form_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliDelFid.esp"
    _make_plugin_with_records(plugin_path)
    form_id = _local_object_id(Plugin.load(plugin_path, game="fo4", backend="native"), "CliRecordOne")

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "delete-record", str(plugin_path), form_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["form_id"] == form_id

    listed = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MISC"])
    editor_ids = {rec["editor_id"] for rec in json.loads(listed.output)["records"]}
    assert editor_ids == {"CliRecordTwo"}


def test_esp_delete_record_by_editor_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliDelEid.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "delete-record", str(plugin_path), "CliRecordOne"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["deleted"] == "CliRecordOne"

    listed = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MISC"])
    editor_ids = {rec["editor_id"] for rec in json.loads(listed.output)["records"]}
    assert editor_ids == {"CliRecordTwo"}


def test_esp_delete_record_not_found_exits_nonzero(tmp_path) -> None:
    plugin_path = tmp_path / "CliDelMiss.esp"
    _make_plugin_with_records(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "delete-record", str(plugin_path), "000FFF"])
    assert result.exit_code == 1, result.output
    assert "record not found" in result.output


def test_esp_set_record_inserts_new_record(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetIns.esp"
    _make_plugin(plugin_path)
    source = tmp_path / "kw.json"
    source.write_text(json.dumps({"eid": "CliInsertedKw", "fields": []}), encoding="utf-8")

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "set-record", str(plugin_path), str(source), "--type", "KYWD"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["signature"] == "KYWD"

    listed = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "KYWD"])
    editor_ids = {rec["editor_id"] for rec in json.loads(listed.output)["records"]}
    assert "CliInsertedKw" in editor_ids


def test_esp_set_record_replaces_existing_record(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetRep.esp"
    _make_plugin_with_records(plugin_path)
    form_id = _local_object_id(Plugin.load(plugin_path, game="fo4", backend="native"), "CliRecordOne")

    # Pull the authoring shape, rename it, write it back over the same FormID.
    dump = CliRunner().invoke(cli, ["--game", "fo4", "esp", "get-record", str(plugin_path), form_id, "--authoring"])
    assert dump.exit_code == 0, dump.output
    record = json.loads(dump.output)
    record["eid"] = "CliRenamedRecord"
    source = tmp_path / "rec.json"
    source.write_text(json.dumps(record), encoding="utf-8")

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "set-record", str(plugin_path), str(source), "--type", "MISC"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["created"] is False

    listed = CliRunner().invoke(cli, ["--game", "fo4", "esp", "list-records", str(plugin_path), "--type", "MISC"])
    editor_ids = {rec["editor_id"] for rec in json.loads(listed.output)["records"]}
    assert editor_ids == {"CliRenamedRecord", "CliRecordTwo"}


def test_esp_set_record_new_record_requires_type(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetNoType.esp"
    _make_plugin(plugin_path)
    source = tmp_path / "kw.json"
    source.write_text(json.dumps({"eid": "CliNoType", "fields": []}), encoding="utf-8")

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "set-record", str(plugin_path), str(source)])
    assert result.exit_code != 0
    assert "record type required" in result.output


def test_retain_race_subgraph_blocks_preserves_non_subgraph_bytes() -> None:
    from cli.esp_commands import _retain_race_subgraph_blocks

    selector = 0x07568776
    other = 0x0757519F
    prefix = [("EDID", b"Race\0", None), ("SADD", b"base", None)]
    selected = [
        ("STKD", selector.to_bytes(4, "little"), None),
        ("SGNM", b"graph\0", None),
        ("SAPT", b"path\0", None),
        ("SRAF", b"flags", None),
    ]
    rejected = [
        ("STKD", other.to_bytes(4, "little"), None),
        ("SGNM", b"other\0", None),
        ("SRAF", b"flags", None),
    ]
    modifier_selected = [
        ("STKD", (selector + 1).to_bytes(4, "little"), None),
        ("STKD", selector.to_bytes(4, "little"), None),
        ("SGNM", b"graph\0", None),
        ("SRAF", b"flags", None),
    ]
    suffix = [("NAM1", b"untouched", None)]

    filtered, total, kept = _retain_race_subgraph_blocks(
        prefix + selected + rejected + modifier_selected + suffix,
        selector,
    )

    assert total == 3
    assert kept == 2
    assert filtered == prefix + selected + modifier_selected + suffix


def _make_cascade_plugin(path: Path) -> tuple[str, str]:
    """Build a plugin where a LVLI (list entry) and a COBJ (scalar slot) both
    reference a target KYWD. Returns (target_object_id, keep_object_id)."""
    plugin = Plugin.new(path.name, game="fo4")
    for editor_id in ("CascTarget", "CascKeep"):
        record = plugin.new_record("KYWD")
        record.editor_id = editor_id
        plugin.add_record(record)
    plugin.save(path)

    loaded = Plugin.load(path, game="fo4", backend="native")
    target = _local_object_id(loaded, "CascTarget")
    keep = _local_object_id(loaded, "CascKeep")
    name = loaded.plugin_name
    loaded.upsert_authoring_record({
        "eid": "CascList",
        "signature": "LVLI",
        "fields": [
            {"Count": 2},
            {"LVLO": {"Level": 1, "Item": {"reference": {"plugin": name, "object_id": target}}, "Count": 1}},
            {"LVLO": {"Level": 1, "Item": {"reference": {"plugin": name, "object_id": keep}}, "Count": 1}},
        ],
    })
    loaded.upsert_authoring_record({
        "eid": "CascRecipe",
        "signature": "COBJ",
        "fields": [{"CreatedObject": {"reference": {"plugin": name, "object_id": target}}}],
    })
    loaded.save(path)
    return target, keep


def test_esp_delete_record_cascade_drops_list_entry_and_nulls_scalar(tmp_path) -> None:
    plugin_path = tmp_path / "CliCascade.esp"
    target, keep = _make_cascade_plugin(plugin_path)

    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", "delete-record", str(plugin_path), "CascTarget", "--cascade"])
    assert result.exit_code == 0, result.output
    cascade = json.loads(result.output)["cascade"]
    assert cascade == {"records_modified": 2, "refs_removed": 2}

    plugin = Plugin.load(plugin_path, game="fo4", backend="native")
    assert plugin.get_record_by_form_id(int(target, 16)) is None

    lvli = plugin.read_authoring_record(int(_local_object_id(plugin, "CascList"), 16))
    llct = [f["Count"] for f in lvli["fields"] if set(f) == {"Count"}]
    remaining = [f["LVLO"]["Item"]["reference"]["object_id"] for f in lvli["fields"] if "LVLO" in f]
    assert llct == [1]  # LLCT decremented in lockstep with the dropped entry
    assert remaining == [keep]  # only the surviving entry is kept

    cobj = plugin.read_authoring_record(int(_local_object_id(plugin, "CascRecipe"), 16))
    created_object = next(f["CreatedObject"] for f in cobj["fields"] if "CreatedObject" in f)
    assert created_object is None  # scalar reference slot nulled


def test_esp_delete_record_cascade_keeps_output_clean(tmp_path) -> None:
    plugin_path = tmp_path / "CliCascadeClean.esp"
    _make_cascade_plugin(plugin_path)

    CliRunner().invoke(cli, ["--game", "fo4", "esp", "delete-record", str(plugin_path), "CascTarget", "--cascade"])
    check = CliRunner().invoke(cli, ["--game", "fo4", "esp", "check-errors", str(plugin_path), "--no-fail"])
    assert check.exit_code == 0, check.output
    assert json.loads(check.output)["error_count"] == 0


# --- search / bulk fixtures + tests ------------------------------------------

def _make_varied_plugin(path: Path) -> Plugin:
    """A mixed-type plugin with EditorIDs and full names that differ, for matching."""
    plugin = Plugin.new(path.name, game="fo4")
    for signature, editor_id, full in (
        ("WEAP", "B21_PlasmaGun", "Plasma Gun"),
        ("WEAP", "B21_LaserGun", "Laser Gun"),
        ("ARMO", "B21_Helmet", "Combat Helmet"),
        ("MISC", "B21_Junk", "Scrap Junk"),
    ):
        record = plugin.new_record(signature)
        record.editor_id = editor_id
        record.full_name = full
        plugin.add_record(record)
    plugin.save(path)
    return plugin


def _esp(*args, code=0):
    result = CliRunner().invoke(cli, ["--game", "fo4", "esp", *args])
    assert result.exit_code == code, result.output
    return result


def _eids(payload) -> set:
    return {rec["editor_id"] for rec in payload["records"]}


def _make_quest_autostart_plugin(path: Path) -> None:
    plugin = Plugin.new(path.name, game="fo4")
    for editor_id, flags in (
        ("B21_StartQuest", 0x0021),
        ("B21_DisabledQuest", 0x0012),
    ):
        record = plugin.new_record("QUST")
        record.editor_id = editor_id
        record.add_subrecord("DNAM", flags.to_bytes(2, "little") + b"\xAA\xBB")
        plugin.add_record(record)
    no_dnam = plugin.new_record("QUST")
    no_dnam.editor_id = "B21_NoDnamQuest"
    plugin.add_record(no_dnam)
    plugin.save(path)


def _quest_dnam_flags(path: Path) -> dict[str, int]:
    handle = native_runtime.load_plugin_native(str(path), game="fo4")
    try:
        flags = {}
        for form_id in native_runtime.plugin_handle_record_form_ids(handle, ["QUST"]):
            summary = native_runtime.plugin_handle_record_summary(handle, form_id)
            if summary is None or not summary.editor_id:
                continue
            for sig, data, _semantic_type in native_runtime.plugin_handle_record_subrecords(handle, form_id) or []:
                if sig == "DNAM" and len(data) >= 2:
                    flags[summary.editor_id] = int.from_bytes(data[:2], "little")
        return flags
    finally:
        native_runtime.plugin_handle_close(handle)


def test_esp_disable_quest_autostart_clears_start_game_enabled(tmp_path) -> None:
    plugin_path = tmp_path / "CliQuestAutostart.esp"
    _make_quest_autostart_plugin(plugin_path)

    out = json.loads(_esp("disable-quest-autostart", str(plugin_path)).output)

    assert out["quests_scanned"] == 3
    assert out["records_with_dnam"] == 2
    assert out["records_missing_dnam"] == 1
    assert out["records_changed"] == 1
    assert out["dnam_subrecords_changed"] == 1
    assert out["records"][0]["editor_id"] == "B21_StartQuest"
    assert _quest_dnam_flags(plugin_path) == {
        "B21_StartQuest": 0x0020,
        "B21_DisabledQuest": 0x0012,
    }


def test_esp_disable_quest_autostart_dry_run_does_not_save(tmp_path) -> None:
    plugin_path = tmp_path / "CliQuestAutostartDry.esp"
    _make_quest_autostart_plugin(plugin_path)

    out = json.loads(_esp("disable-quest-autostart", str(plugin_path), "--dry-run").output)

    assert out["dry_run"] is True
    assert out["records_changed"] == 1
    assert _quest_dnam_flags(plugin_path)["B21_StartQuest"] == 0x0021


def test_esp_disable_quest_autostart_keeps_first_enabled_quests_by_form_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliQuestAutostartBisect.esp"
    plugin = Plugin.new(plugin_path.name, game="fo4")
    for editor_id in ("B21_First", "B21_Second", "B21_Third", "B21_Fourth"):
        record = plugin.new_record("QUST")
        record.editor_id = editor_id
        record.add_subrecord("DNAM", (0x0021).to_bytes(2, "little") + b"\xAA\xBB")
        plugin.add_record(record)
    plugin.save(plugin_path)

    out = json.loads(
        _esp(
            "disable-quest-autostart",
            str(plugin_path),
            "--keep-first",
            "2",
        ).output
    )

    assert out["records_kept_enabled"] == 2
    assert out["records_changed"] == 2
    assert [row["editor_id"] for row in out["kept_records"]] == [
        "B21_First",
        "B21_Second",
    ]
    assert _quest_dnam_flags(plugin_path) == {
        "B21_First": 0x0021,
        "B21_Second": 0x0021,
        "B21_Third": 0x0020,
        "B21_Fourth": 0x0020,
    }


def test_esp_disable_quest_autostart_keeps_exact_editor_id(tmp_path) -> None:
    plugin_path = tmp_path / "CliQuestAutostartKeepEditorID.esp"
    plugin = Plugin.new(plugin_path.name, game="fo4")
    for editor_id in ("B21_First", "B21_Suspect", "B21_Third"):
        record = plugin.new_record("QUST")
        record.editor_id = editor_id
        record.add_subrecord("DNAM", (0x0021).to_bytes(2, "little") + b"\xAA\xBB")
        plugin.add_record(record)
    plugin.save(plugin_path)

    out = json.loads(
        _esp(
            "disable-quest-autostart",
            str(plugin_path),
            "--keep-editor-id",
            "b21_suspect",
        ).output
    )

    assert out["records_kept_enabled"] == 1
    assert out["records_changed"] == 2
    assert out["kept_records"][0]["editor_id"] == "B21_Suspect"
    assert _quest_dnam_flags(plugin_path) == {
        "B21_First": 0x0020,
        "B21_Suspect": 0x0021,
        "B21_Third": 0x0020,
    }


def test_esp_search_glob(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchGlob.esp"
    _make_varied_plugin(plugin_path)
    payload = json.loads(_esp("search", str(plugin_path), "*Gun").output)
    assert payload["mode"] == "glob"
    assert _eids(payload) == {"B21_PlasmaGun", "B21_LaserGun"}


def test_esp_search_substring(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchSub.esp"
    _make_varied_plugin(plugin_path)
    payload = json.loads(_esp("search", str(plugin_path), "plasma", "--substring").output)
    assert _eids(payload) == {"B21_PlasmaGun"}


def test_esp_search_regex(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchRe.esp"
    _make_varied_plugin(plugin_path)
    payload = json.loads(_esp("search", str(plugin_path), "^B21_L", "--regex").output)
    assert _eids(payload) == {"B21_LaserGun"}


def test_esp_search_full_name_only_hit(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchFull.esp"
    _make_varied_plugin(plugin_path)
    # "Combat" is in the helmet's full name but not its EditorID.
    without_full = json.loads(_esp("search", str(plugin_path), "*Combat*").output)
    assert without_full["count"] == 0
    with_full = json.loads(_esp("search", str(plugin_path), "*Combat*", "--full").output)
    assert _eids(with_full) == {"B21_Helmet"}
    assert with_full["records"][0]["full_name"] == "Combat Helmet"


def test_esp_search_type_scope_and_limit(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchType.esp"
    _make_varied_plugin(plugin_path)
    scoped = json.loads(_esp("search", str(plugin_path), "*", "--type", "WEAP").output)
    assert _eids(scoped) == {"B21_PlasmaGun", "B21_LaserGun"}
    limited = json.loads(_esp("search", str(plugin_path), "*", "--limit", "1").output)
    assert limited["count"] == 1


def test_esp_search_substring_regex_mutex(tmp_path) -> None:
    plugin_path = tmp_path / "CliSearchMutex.esp"
    _make_varied_plugin(plugin_path)
    result = _esp("search", str(plugin_path), "*", "--substring", "--regex", code=1)
    assert "mutually exclusive" in result.output


def test_esp_list_records_match(tmp_path) -> None:
    plugin_path = tmp_path / "CliListMatch.esp"
    _make_varied_plugin(plugin_path)
    payload = json.loads(_esp("list-records", str(plugin_path), "--match", "*Gun").output)
    assert _eids(payload) == {"B21_PlasmaGun", "B21_LaserGun"}


def test_esp_set_field_across_matches(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetField.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp(
        "set-field", str(plugin_path), "--match", "*Gun", "--type", "WEAP",
        "--field", "FULL", "--value", '"Energy Weapon"',
    ).output)
    assert out["modified"] == 2
    record = json.loads(_esp("get-record", str(plugin_path), "B21_PlasmaGun", "--authoring").output)
    assert {"FULL": "Energy Weapon"} in record["fields"]


def test_esp_set_field_dry_run_does_not_save(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetFieldDry.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp(
        "set-field", str(plugin_path), "--match", "*Gun", "--field", "FULL",
        "--value", '"Untouched"', "--dry-run",
    ).output)
    assert out["dry_run"] is True and out["modified"] == 2 and out["output"] is None
    record = json.loads(_esp("get-record", str(plugin_path), "B21_PlasmaGun", "--authoring").output)
    assert {"FULL": "Plasma Gun"} in record["fields"]


def test_esp_delete_matching(tmp_path) -> None:
    plugin_path = tmp_path / "CliDeleteMatch.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp("delete-matching", str(plugin_path), "--match", "*Gun").output)
    assert out["deleted"] == 2
    remaining = _eids(json.loads(_esp("list-records", str(plugin_path)).output))
    assert remaining == {"B21_Helmet", "B21_Junk"}


def test_esp_delete_matching_dry_run(tmp_path) -> None:
    plugin_path = tmp_path / "CliDeleteMatchDry.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp("delete-matching", str(plugin_path), "--match", "*Gun", "--dry-run").output)
    assert out["dry_run"] is True and out["deleted"] == 2
    remaining = _eids(json.loads(_esp("list-records", str(plugin_path)).output))
    assert {"B21_PlasmaGun", "B21_LaserGun"} <= remaining


def test_esp_rename_prefix(tmp_path) -> None:
    plugin_path = tmp_path / "CliRenamePrefix.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp("rename", str(plugin_path), "--match", "*", "--prefix", "B21_=MOD_").output)
    assert out["renamed"] == 4
    eids = _eids(json.loads(_esp("list-records", str(plugin_path)).output))
    assert all(eid.startswith("MOD_") for eid in eids)


def test_esp_rename_regex_sub(tmp_path) -> None:
    plugin_path = tmp_path / "CliRenameRe.esp"
    _make_varied_plugin(plugin_path)
    out = json.loads(_esp("rename", str(plugin_path), "--match", "*Gun", "--regex-sub", "Gun$=Rifle").output)
    assert out["renamed"] == 2
    eids = _eids(json.loads(_esp("list-records", str(plugin_path), "--type", "WEAP").output))
    assert eids == {"B21_PlasmaRifle", "B21_LaserRifle"}


def test_esp_rename_collision_aborts(tmp_path) -> None:
    plugin_path = tmp_path / "CliRenameCollide.esp"
    _make_varied_plugin(plugin_path)
    result = _esp("rename", str(plugin_path), "--match", "B21_PlasmaGun", "--to", "B21_LaserGun", code=1)
    assert "collision" in result.output


def test_esp_set_record_array_bulk_add(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetArray.esp"
    _make_plugin(plugin_path)
    source = tmp_path / "kw.json"
    source.write_text(json.dumps([
        {"eid": "B21_KwA", "fields": []},
        {"eid": "B21_KwB", "fields": []},
    ]), encoding="utf-8")
    out = json.loads(_esp("set-record", str(plugin_path), str(source), "--type", "KYWD").output)
    assert out["count"] == 2 and out["created"] == 2
    eids = _eids(json.loads(_esp("list-records", str(plugin_path), "--type", "KYWD").output))
    assert eids == {"B21_KwA", "B21_KwB"}


def test_esp_set_record_array_dry_run(tmp_path) -> None:
    plugin_path = tmp_path / "CliSetArrayDry.esp"
    _make_plugin(plugin_path)
    source = tmp_path / "kw.json"
    source.write_text(json.dumps([{"eid": "B21_KwA", "fields": []}]), encoding="utf-8")
    out = json.loads(_esp("set-record", str(plugin_path), str(source), "--type", "KYWD", "--dry-run").output)
    assert out["dry_run"] is True and out["output"] is None
    listed = json.loads(_esp("list-records", str(plugin_path), "--type", "KYWD").output)
    assert listed["count"] == 0


def test_esp_count(tmp_path) -> None:
    plugin_path = tmp_path / "CliCount.esp"
    _make_varied_plugin(plugin_path)
    total = json.loads(_esp("count", str(plugin_path)).output)
    assert total["record_count"] == 4
    assert {s["signature"]: s["count"] for s in total["signatures"]}["WEAP"] == 2
    scoped = json.loads(_esp("count", str(plugin_path), "--type", "WEAP", "--match", "*Gun").output)
    assert scoped["type_count"] == 2 and scoped["match_count"] == 2


def test_esp_copy_record(tmp_path) -> None:
    source_path = tmp_path / "CliCopySrc.esp"
    target_path = tmp_path / "CliCopyDst.esp"
    _make_varied_plugin(source_path)
    _make_plugin(target_path)
    out = json.loads(_esp("copy-record", str(source_path), str(target_path), "B21_PlasmaGun").output)
    assert out["copied"] == 1
    eids = _eids(json.loads(_esp("list-records", str(target_path)).output))
    assert "B21_PlasmaGun" in eids


def test_esp_copy_record_override_keeps_form_id(tmp_path) -> None:
    source_path = tmp_path / "CliCopyOvSrc.esp"
    target_path = tmp_path / "CliCopyOvDst.esp"
    _make_varied_plugin(source_path)
    _make_plugin(target_path)
    out = json.loads(_esp("copy-record", str(source_path), str(target_path), "B21_PlasmaGun", "--override").output)
    assert out["records"][0]["new_form_id"] == out["records"][0]["source_form_id"]


def test_esp_copy_record_requires_exactly_one_selector(tmp_path) -> None:
    source_path = tmp_path / "CliCopyErrSrc.esp"
    target_path = tmp_path / "CliCopyErrDst.esp"
    _make_varied_plugin(source_path)
    _make_plugin(target_path)
    result = _esp("copy-record", str(source_path), str(target_path), code=1)
    assert "exactly one" in result.output
