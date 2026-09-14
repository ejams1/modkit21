from __future__ import annotations

import json
import struct

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.tests.test_inspection_queries import string, script
from creation_lib.esp import Plugin


@pytest.mark.parametrize("command", ["record", "refs", "count-refs", "keywords"])
def test_reversed_data_formkey_explains_correct_order(command):
    result = CliRunner().invoke(cli, ["data", command, "Fallout4.esm:004822"])
    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert "004822:Fallout4.esm" in error["message"]
    assert "reversed" in error["message"]


def test_texture_inspect_uses_native_metadata_and_projection(tmp_path):
    from creation_lib.dds.native_runtime import write_dds_rgba
    path = tmp_path / "B21_Mipped.dds"
    assert write_dds_rgba(str(path), 8, 4, bytes([128, 128, 255, 255] * 32),
                          format="BC5_UNORM", generate_mips=True, use_gpu=False)
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["texture", "inspect", str(path)])
    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    assert (info["width"], info["height"], info["format"], info["mip_levels"]) == (8, 4, "BC5_UNORM", 4)
    assert info["is_compressed"] and info["bytes"] == len(before)
    result = CliRunner().invoke(cli, ["--fields", "width,height,format,mip_levels", "texture", "inspect", str(path)])
    assert json.loads(result.stdout)["data"] == [{"width": 8, "height": 4, "format": "BC5_UNORM", "mip_levels": 4}]
    assert path.read_bytes() == before
    path.write_bytes(b"DDS invalid")
    failed = CliRunner().invoke(cli, ["texture", "inspect", str(path)])
    assert failed.exit_code == 1
    assert json.loads(failed.stderr)["error"]["code"] == "COMMAND_FAILED"


def make_nested_plugin(path, *, reference=0x02000800):
    with Plugin.new(path.name, game="fo4", masters=["B21_Unused.esm", "B21_Master.esm"]) as plugin:
        base = plugin.new_record("MISC", form_id=0x02000800)
        base.editor_id = "B21_Target"
        plugin.add_record(base)
        container = plugin.new_record("CONT", form_id=0x02000801)
        container.editor_id = "B21_Container"
        container.add_subrecord("CNTO", struct.pack("<II", reference, reference))
        plugin.add_record(container)
        levelled = plugin.new_record("LVLI", form_id=0x02000802)
        levelled.editor_id = "B21_Levelled"
        levelled.add_subrecord("LVLO", struct.pack("<H2xIH2x", 1, reference, 1))
        plugin.add_record(levelled)
        actor = plugin.new_record("NPC_", form_id=0x02000803)
        actor.editor_id = "B21_Scripted"
        actor.flags |= 0x40000
        props = string("Target") + bytes([1, 1]) + struct.pack("<HhI", 0, -1, reference)
        props += string("Number") + bytes([3, 1]) + struct.pack("<I", reference)
        actor.add_subrecord("VMAD", struct.pack("<HHH", 6, 2, 1) + script("B21_Script", props, 2))
        plugin.add_record(actor)
        plugin.save(path)


def test_masters_remove_remaps_nested_disk_references(tmp_path):
    from creation_lib.esp import native_runtime
    path = tmp_path / "B21_Nested.esp"
    make_nested_plugin(path)
    result = CliRunner().invoke(cli, ["esp", "masters", "remove", str(path), "B21_Unused.esm"])
    assert result.exit_code == 0, result.output
    with Plugin.load(path, game="fo4") as plugin:
        assert plugin.header.masters == ["B21_Master.esm"]
        for fid, signature, offset in [(0x01000801, "CNTO", 0), (0x01000802, "LVLO", 4)]:
            rows = native_runtime.plugin_handle_record_subrecords(plugin._rust_handle, fid)
            data = next(data for sig, data, _ in rows if sig == signature)
            assert struct.unpack_from("<I", data, offset)[0] == 0x01000800
            if signature == "CNTO":
                assert struct.unpack_from("<I", data, 4)[0] == 0x02000800
    report = CliRunner().invoke(cli, ["esp", "vmad", str(path), "B21_Scripted"])
    properties = json.loads(report.stdout)["data"][0]["properties"]
    assert properties[0]["Value"]["FormID"]["reference"]["plugin"] == path.name
    assert properties[1]["Value"] == 0x02000800


def test_masters_remove_detects_and_nulls_nested_references(tmp_path):
    path = tmp_path / "B21_Forced.esp"
    make_nested_plugin(path, reference=0x00000900)
    before = path.read_bytes()
    args = ["esp", "masters", "remove", str(path), "B21_Unused.esm"]
    refused = CliRunner().invoke(cli, args)
    assert refused.exit_code == 1
    assert "--force" in refused.stderr
    assert path.read_bytes() == before
    forced = CliRunner().invoke(cli, [*args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["refs_nulled"] == 3
    report = CliRunner().invoke(cli, ["esp", "vmad", str(path), "B21_Scripted"])
    properties = json.loads(report.stdout)["data"][0]["properties"]
    assert properties[0]["Value"]["FormID"] is None
    assert properties[1]["Value"] == 0x00000900


def test_masters_remove_remaps_condition_unions_without_touching_numbers(tmp_path):
    from creation_lib.esp import native_runtime
    path = tmp_path / "B21_Conditions.esp"
    reference = 0x01000800
    with Plugin.new(path.name, game="fo4", masters=["B21_Unused.esm"]) as plugin:
        record = plugin.new_record("COBJ", form_id=0x01000801)
        for flags, function in [(4, 277), (0, 660)]:
            record.add_subrecord("CTDA", struct.pack("<B3xIH2xIIIIi", flags, reference, function,
                                                    reference, reference, 2, reference, -1))
        plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "masters", "remove", str(path), "B21_Unused.esm"])
    assert result.exit_code == 0, result.output
    with Plugin.load(path, game="fo4") as plugin:
        rows = native_runtime.plugin_handle_record_subrecords(plugin._rust_handle, 0x801)
        conditions = [data for sig, data, _ in rows if sig == "CTDA"]
        assert struct.unpack_from("<I", conditions[0], 4)[0] == 0x800
        assert struct.unpack_from("<I", conditions[0], 12)[0] == 0x800
        assert struct.unpack_from("<I", conditions[0], 24)[0] == 0x800
        assert struct.unpack_from("<I", conditions[1], 4)[0] == reference
        assert struct.unpack_from("<I", conditions[1], 12)[0] == reference
        assert struct.unpack_from("<I", conditions[1], 24)[0] == 0x800


def test_masters_remove_remaps_omod_references_without_touching_values(tmp_path):
    from creation_lib.esp import native_runtime
    path = tmp_path / "B21_ObjectMod.esp"
    reference = 0x01000800
    data = struct.pack("<IIBBIBBI", 1, 2, 0, 0, int.from_bytes(b"WEAP", "little"), 0, 0, reference)
    data += struct.pack("<III", 1, reference, 0)
    data += struct.pack("<IBBB", reference, 0, 0, 0)
    for value_type in [4, 0]:
        data += struct.pack("<B3xB3xH2xIIf", value_type, 0, 31, reference, reference, 0)
    with Plugin.new(path.name, game="fo4", masters=["B21_Unused.esm"]) as plugin:
        record = plugin.new_record("OMOD", form_id=0x01000801)
        record.add_subrecord("DATA", data)
        plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "masters", "remove", str(path), "B21_Unused.esm"])
    assert result.exit_code == 0, result.output
    with Plugin.load(path, game="fo4") as plugin:
        rows = native_runtime.plugin_handle_record_subrecords(plugin._rust_handle, 0x801)
        rebuilt = next(data for sig, data, _ in rows if sig == "DATA")
        for offset in [16, 24, 32, 39 + 12]:
            assert struct.unpack_from("<I", rebuilt, offset)[0] == 0x800
        for offset in [39 + 16, 63 + 12, 63 + 16]:
            assert struct.unpack_from("<I", rebuilt, offset)[0] == reference


def test_masters_refuses_opaque_vmad_without_changing_plugin(tmp_path):
    path = tmp_path / "B21_Opaque.esp"
    with Plugin.new(path.name, game="fo4", masters=["B21_Unused.esm"]) as plugin:
        record = plugin.new_record("NPC_", form_id=0x01000800)
        record.add_subrecord("VMAD", b"bad")
        plugin.add_record(record)
        plugin.save(path)
    before = path.read_bytes()
    result = CliRunner().invoke(cli, ["esp", "masters", "remove", str(path), "B21_Unused.esm", "--force"])
    assert result.exit_code == 1
    assert "VMAD" in result.stderr
    assert path.read_bytes() == before


def test_set_masters_refuses_referenced_removal_atomically(tmp_path):
    path = tmp_path / "B21_Atomic.esp"
    make_nested_plugin(path, reference=0x00000900)
    with Plugin.load(path, game="fo4") as plugin:
        with pytest.raises(ValueError, match="still used"):
            plugin.set_masters([("B21_Master.esm", 0)])
        assert plugin.header.masters == ["B21_Unused.esm", "B21_Master.esm"]
        assert plugin.get_record_by_form_id(0x02000801) is not None


@pytest.mark.parametrize("loose", [False, True])
def test_mod_deploy_cli_accepts_whole_source_and_preserves_ini(tmp_path, monkeypatch, loose):
    from creation_lib.esp import export_json
    mod = tmp_path / "mods" / "B21_Whole"
    (mod / "yaml").mkdir(parents=True)
    (mod / ".game").write_text("fo4", encoding="utf-8")
    with Plugin.new("B21_Actual.esp", game="fo4", masters=[]) as plugin:
        (mod / "yaml" / "whole.json").write_text(export_json(plugin), encoding="utf-8")
    source_ini = mod / "F4SE" / "Plugins" / "B21_Test.ini"
    source_ini.parent.mkdir(parents=True)
    source_ini.write_text("new", encoding="utf-8")
    target = tmp_path / "Data"
    target_ini = target / "F4SE" / "Plugins" / "B21_Test.ini"
    target_ini.parent.mkdir(parents=True)
    target_ini.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    result = CliRunner().invoke(cli, ["mod", "deploy", "B21_Whole", "--source", "yaml/whole.json",
                                      "--data-dir", str(target), "--preserve-xse-inis",
                                      *( ["--loose"] if loose else ["--skip-pack"] )])
    assert result.exit_code == 0, result.output
    assert (target / "B21_Actual.esp").is_file()
    assert target_ini.read_text() == "keep"
