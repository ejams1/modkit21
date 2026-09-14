from __future__ import annotations

import struct

from click.testing import CliRunner

from cli.main import cli
from cli.tests.test_inspection_queries import invoke
from creation_lib.esp import Group, Plugin, PluginHeader, Record, Subrecord
from creation_lib.nif import native_runtime as nif_native


def write_nif(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = nif_native.new_nif_raw("fo4")
    path.write_bytes(nif_native.nif_to_bytes_raw(payload))


def placed_plugin(path):
    base = Record("STAT", 0x00000800, subrecords=[Subrecord("EDID", b"B21_Base\0"), Subrecord("MODL", b"test\\available.nif\0")])
    missing = Record("STAT", 0x00000801, subrecords=[Subrecord("EDID", b"B21_Missing\0"), Subrecord("MODL", b"test\\missing.nif\0")])
    cell = Record("CELL", 0x00000810, subrecords=[Subrecord("EDID", b"B21_Cell\0")])
    refs = [Record("REFR", 0x00000820 + i, subrecords=[Subrecord("NAME", struct.pack("<I", base_id))])
            for i, base_id in enumerate([0x800, 0x800, 0x801, 0x999])]
    label = struct.pack("<I", 0x810)
    plugin = Plugin(plugin_name=path.name, file_path=path, game="fo4", header=PluginHeader(masters=[]),
        root_items=[Group(b"STAT", 0, children=[base, missing]),
                    Group(b"CELL", 0, children=[Group(bytes(4), 2, children=[Group(bytes(4), 3, children=[cell,
                        Group(label, 6, children=[Group(label, 9, children=refs)])])])])])
    plugin.save(path)
    plugin.close()


def test_placed_models_and_cell_collision_are_read_only(tmp_path):
    plugin = tmp_path / "B21_Placed.esp"
    placed_plugin(plugin)
    write_nif(tmp_path / "Meshes/test/available.nif")
    before = plugin.read_bytes()
    report = invoke("esp", "placed-models", str(plugin), "--cell", "B21_Cell")
    assert report["summary"]["placements"] == 4
    assert report["summary"]["status_counts"] == {"available": 1, "missing": 1, "unresolved_base": 1}
    available = next(row for row in report["records"] if row["status"] == "available")
    assert available["placement_count"] == 2
    report = invoke("esp", "cell-collision", str(plugin), "B21_Cell")
    assert len(report["data"]) == 4
    available = next(row for row in report["data"] if row["status"] == "available")
    assert available["collision"][0]["complete"]
    assert available["collision"][0]["has_collision"] is False
    failed = CliRunner().invoke(cli, ["esp", "placed-models", str(plugin), "--fail-on-missing"])
    assert failed.exit_code == 1
    assert plugin.read_bytes() == before


def test_asset_resolution_loose_archive_precedence_and_malformed(tmp_path, monkeypatch):
    from creation_lib.inspection.assets import AssetResolver
    from creation_lib.ba2 import native_runtime
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        path = root / "Meshes/Test/a.nif"
        path.parent.mkdir(parents=True)
        path.write_bytes(root.name.encode())
    archive = tmp_path / "B21_Assets.ba2"
    archive.write_bytes(b"fixture")
    monkeypatch.setattr(native_runtime, "list_archive", lambda path: ["meshes/test/a.nif", "textures/test/a.dds"])
    monkeypatch.setattr(native_runtime, "extract_one", lambda path, member: b"archive data")
    resolver = AssetResolver([first, second], [archive])
    exact = resolver.resolve("test/a.nif")
    assert exact["status"] == "available"
    assert len(exact["locations"]) == 3
    assert resolver.read(exact) == b"second"
    assert resolver.read(resolver.resolve("textures/test/a.dds")) == b"archive data"
    malformed = resolver.resolve("test /a.nif")
    assert malformed["status"] == "malformed_path"
    assert malformed["winner"] is None
    assert resolver.resolve("../outside.nif")["status"] == "invalid_path"


def test_behavior_report_resolves_transitions_and_missing_links(tmp_path):
    path = tmp_path / "behavior.xml"
    path.write_text('''<hkpackfile><hksection>
      <hkobject name="#strings" class="hkbBehaviorGraphStringData"><hkparam name="eventNames"><hkcstring>Start</hkcstring></hkparam><hkparam name="variableNames"><hkcstring>Enabled</hkcstring></hkparam></hkobject>
      <hkobject name="#machine" class="hkbStateMachine"><hkparam name="name">Main</hkparam><hkparam name="startStateId">0</hkparam><hkparam name="states">#state</hkparam></hkobject>
      <hkobject name="#state" class="hkbStateMachineStateInfo"><hkparam name="name">Idle</hkparam><hkparam name="stateId">0</hkparam><hkparam name="generator">#clip</hkparam><hkparam name="transitions">#transitions</hkparam></hkobject>
      <hkobject name="#transitions" class="hkbStateMachineTransitionInfoArray"><hkparam name="transitions"><hkobject><hkparam name="eventId">0</hkparam><hkparam name="toStateId">0</hkparam></hkobject></hkparam></hkobject>
      <hkobject name="#clip" class="hkbClipGenerator"><hkparam name="name">Idle</hkparam><hkparam name="animationName">idle.hkx</hkparam><hkparam name="triggers">#missing</hkparam></hkobject>
      </hksection></hkpackfile>''', encoding="utf-8")
    report = invoke("behavior", "report", str(path))
    assert report["transitions"][0]["event"] == "Start"
    assert report["transitions"][0]["to_name"] == "Idle"
    assert report["clips"][0]["animation"] == "idle.hkx"
    assert report["unresolved_references"] == [{"from": "#clip", "to": "#missing", "field": "triggers"}]
    rows = invoke("--items", "transitions", "--fields", "event,to_name", "behavior", "report", str(path))
    assert rows["data"] == [{"event": "Start", "to_name": "Idle"}]


def test_explain_follows_references_and_assets(tmp_path):
    path = tmp_path / "B21_Explain.esp"
    placed_plugin(path)
    write_nif(tmp_path / "Meshes/test/available.nif")
    report = invoke("esp", "explain", str(path), "820")
    assert {row["signature"] for row in report["records"]} == {"REFR", "STAT"}
    assert any(row["status"] == "available" and row["path"].endswith("available.nif") for row in report["assets"])
    assert not report["issues"]


def test_explain_exact_master_override_with_colliding_local_id(tmp_path):
    master_path = tmp_path / "B21_Master.esm"
    with Plugin.new(master_path.name, game="fo4", masters=[]) as master:
        record = master.new_record("STAT", form_id=0xFF000800)
        record.editor_id = "B21_MasterBase"
        record.add_subrecord("MODL", b"test/master.nif\0")
        master.add_record(record)
        master.save(master_path)
    patch_path = tmp_path / "B21_Override.esp"
    with Plugin.new(patch_path.name, game="fo4", masters=[master_path.name]) as patch:
        for fid, eid, model in [(0x00000800, "B21_OverrideBase", "test/override.nif"),
                                (0xFF000800, "B21_OwnBase", "test/own.nif")]:
            record = patch.new_record("STAT", form_id=fid)
            record.editor_id = eid
            record.add_subrecord("MODL", model.encode() + b"\0")
            patch.add_record(record)
        placed = patch.new_record("REFR", form_id=0xFF000810)
        placed.add_subrecord("NAME", struct.pack("<I", 0x800))
        patch.add_record(placed)
        patch.save(patch_path)
    order = tmp_path / "loadorder.txt"
    order.write_text(f"{master_path.name}\n{patch_path.name}\n", encoding="utf-8")
    report = invoke("esp", "explain", str(master_path), "800", "--load-order", str(order))
    root = report["records"][0]
    assert root["winner"] == patch_path.name
    assert root["editor_id"] == "B21_OverrideBase"
    assert root["overrides"] == [master_path.name, patch_path.name]
    assert any(row["path"].endswith("override.nif") for row in report["assets"])
    assert not any(row["path"].endswith("own.nif") for row in report["assets"])
    for selector in ("00000800", "B21_Master.esm:000800"):
        rows = invoke("esp", "query", str(patch_path), "--record", selector)["data"]
        assert rows[0]["eid"] == "B21_OverrideBase"
    assert invoke("esp", "query", str(patch_path), "--record", "800")["data"][0]["eid"] == "B21_OwnBase"
    census = invoke("esp", "placed-models", str(patch_path))
    assert census["records"][0]["editor_id"] == "B21_OverrideBase"
    assert census["records"][0]["models"][0]["path"] == "test/override.nif"


def test_explain_decodes_nif_material_texture_chain(tmp_path):
    from creation_lib.nif import NifFile
    from py_creation_lib.tests.test_nif_material_textures import _write_bgsm
    plugin = tmp_path / "B21_Materials.esp"
    placed_plugin(plugin)
    mesh = tmp_path / "meshes/test/available.nif"
    mesh.parent.mkdir(parents=True)
    nif = NifFile.new("fo4")
    nif.add_block("BSLightingShaderProperty", {"Name": "Materials/test/example.bgsm"})
    nif.save(str(mesh))
    material = tmp_path / "materials/test/example.bgsm"
    material.parent.mkdir(parents=True)
    _write_bgsm(material, "test/diffuse.dds", "test/normal.dds")
    texture = tmp_path / "textures/test/diffuse.dds"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"texture presence fixture")
    report = invoke("esp", "explain", str(plugin), "820")
    statuses = {row["asset_key"]: row["status"] for row in report["assets"]}
    assert statuses == {"meshes/test/available.nif": "available", "materials/test/example.bgsm": "available",
                        "textures/test/diffuse.dds": "available", "textures/test/normal.dds": "missing"}
    assert not report["issues"]


def test_collision_report_decodes_serialized_fo4_physics(tmp_path):
    from creation_lib.nif import NifFile
    from creation_lib.havok.native_runtime import fo4_polytope_collision_blob_raw
    vertices = [[x, y, z] for x in (0., 10.) for y in (0., 10.) for z in (0., 10.)]
    blob = fo4_polytope_collision_blob_raw(vertices, 0.5, 0.1, 4, 5.0)
    nif = NifFile.new("fo4")
    nif.add_block("bhkPhysicsSystem", {"Binary Data": {"Data Size": len(blob), "Data": list(blob)}})
    path = tmp_path / "physics.nif"
    nif.save(str(path))
    report = invoke("nif", "collision-report", str(path))
    assert report["has_collision"]
    assert report["complete"], report["issues"]
    physics = next(row for row in report["blocks"] if row["type"] == "bhkPhysicsSystem")
    assert "hknpConvexPolytopeShape" in physics["shape_classes"]
    assert physics["systems"][0]["bodies"][0]["collisionLayer"] == 4


def test_invalid_behavior_xml_is_structured_error(tmp_path):
    import json
    path = tmp_path / "invalid.xml"
    path.write_text("<hkpackfile>", encoding="utf-8")
    result = CliRunner().invoke(cli, ["behavior", "report", str(path)])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["message"].startswith("Invalid behavior XML")
