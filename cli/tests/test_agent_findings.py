import json
import subprocess
import sys
import struct
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cli.main import ModkitGroup, cli
from creation_lib.mod.git_ops import git_commit


def test_entry_point_preserves_literal_patterns(monkeypatch):
    @click.group(cls=ModkitGroup)
    def root():
        pass

    @root.command()
    @click.argument("pattern")
    def match(pattern):
        return pattern

    monkeypatch.setattr(sys, "argv", ["modkit", "match", "*.md"])
    assert root.main(standalone_mode=False) == "*.md"


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    for name in ("selected.txt", "other.txt", "deleted.txt"):
        (tmp_path / name).write_text("before")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial")
    (tmp_path / "selected.txt").write_text("after")
    (tmp_path / "other.txt").write_text("unrelated")
    _git(tmp_path, "add", "other.txt")
    return tmp_path


def test_selective_commit_excludes_unrelated_staged_changes(repository):
    (repository / "literal[1].txt").write_text("new")
    (repository / "literal1.txt").write_text("unselected")
    (repository / "deleted.txt").unlink()
    sha = git_commit(repository, "Test", paths=["selected.txt", "literal[1].txt", "deleted.txt"],
                     message="Selected changes", push=False)
    assert sha == _git(repository, "rev-parse", "HEAD")
    assert set(_git(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == {
        "selected.txt", "literal[1].txt", "deleted.txt"}
    assert _git(repository, "diff", "--cached", "--name-only") == "other.txt"
    assert _git(repository, "show", "HEAD:other.txt") == "before"


@pytest.mark.parametrize("paths", [[], [".."], [".git/config"], ["."]])
def test_selective_commit_rejects_unsafe_selection(repository, paths):
    before = _git(repository, "status", "--porcelain")
    with pytest.raises(ValueError):
        git_commit(repository, "Test", paths=paths, push=False)
    assert _git(repository, "status", "--porcelain") == before


def test_commit_push_failure_is_not_reported_as_success(repository, monkeypatch):
    monkeypatch.setattr("creation_lib.mod.git_ops._push", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(RuntimeError, match="Committed .*push failed"):
        git_commit(repository, "Test", paths=["selected.txt"])
    assert _git(repository, "show", "HEAD:selected.txt") == "after"


def test_commit_cli_requires_explicit_selection():
    result = CliRunner().invoke(cli, ["git", "commit", "Test"])
    assert result.exit_code == 2
    assert "Select one or more" in result.output


@pytest.mark.parametrize("detected,override,version", [("og", "auto", 1), ("nextgen", "auto", 8), ("nextgen", "og", 1)])
def test_pack_mod_detects_archive_version(tmp_path, monkeypatch, detected, override, version):
    from creation_lib.build.packer import pack_mod
    mod_dir = tmp_path / "mods" / "B21_Test"
    assets = mod_dir / "data" / "Meshes"
    assets.mkdir(parents=True)
    (assets / "test.nif").write_bytes(b"test")
    calls = []

    def detect(root):
        calls.append(root)
        return detected, "1.10.163.0" if detected == "og" else "1.10.984.0"

    monkeypatch.setattr("creation_lib.core.fo4_version.detect_ba2_target", detect)
    pack_mod("B21_Test", game="fo4", project_root=tmp_path, game_dir="install", fo4_ba2_target=override)
    data = (mod_dir / "B21_Test - Main.ba2").read_bytes()
    assert data[:4] == b"BTDX"
    assert int.from_bytes(data[4:8], "little") == version
    assert calls == (["install"] if override == "auto" else [])


@pytest.mark.parametrize("full_load", [False, True])
def test_counts_include_nested_records(tmp_path, full_load):
    from cli.tests.test_inspection_reports import placed_plugin
    path = tmp_path / "B21_Count.esp"
    placed_plugin(path)
    suffix = ["--full-load"] if full_load else []
    result = CliRunner().invoke(cli, ["esp", "count", str(path), *suffix])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["record_count"] == 7
    assert {r["signature"]: r["count"] for r in report["signatures"]} == {"STAT": 2, "CELL": 1, "REFR": 4}
    result = CliRunner().invoke(cli, ["esp", "list-records", str(path), "--type", "REFR", *suffix])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["records"]) == 4


def test_check_errors_reports_duplicate_form_ids(tmp_path):
    from creation_lib.esp import Plugin, PluginHeader, Group, Record, Subrecord
    path = tmp_path / "B21_Duplicate.esp"
    rows = [Record("KYWD", 0x800, subrecords=[Subrecord("EDID", name)]) for name in (b"First\0", b"Second\0")]
    with Plugin(plugin_name=path.name, game="fo4", header=PluginHeader(masters=[]),
                root_items=[Group(b"KYWD", 0, children=rows)]) as plugin:
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "check-errors", str(path)])
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert any(issue["category"] == "duplicate_form_id" for issue in report["issues"])


def test_copy_retargets_nested_conditions_and_vmad(tmp_path):
    from creation_lib.esp import Plugin
    from cli.tests.test_inspection_queries import string, script
    source_path, target_path = tmp_path / "B21_Source.esp", tmp_path / "B21_Target.esp"
    conditions = [struct.pack("<B3xfH2xIIIII", 0, 1., function, 0x1000810, 0, 2, 0x1000820, 0)
                  for function in (72, 36)]
    prop = string("Target") + bytes([1, 1]) + struct.pack("<HhI", 0, -1, 0x1000830)
    vmad = struct.pack("<HHH", 6, 2, 1) + script("B21_Script", prop, 1)
    with Plugin.new(source_path.name, game="fo4", masters=["Fallout4.esm", "DLCRobot.esm"]) as source:
        record = source.new_record("COBJ", form_id=0xFF000800)
        record.editor_id = "B21_Copy"
        record.add_subrecord("VMAD", vmad)
        for condition in conditions:
            record.add_subrecord("CTDA", condition)
        source.add_record(record)
        source.save(source_path)
    with Plugin.load(source_path, game="fo4") as source, Plugin.new(
        target_path.name, game="fo4", masters=["DLCRobot.esm", "Fallout4.esm"]
    ) as target:
        target.copy_record(source.get_record_by_form_id(0x2000800), source)
        target.save(target_path)
    with Plugin.load(target_path, game="fo4") as target:
        from creation_lib.esp.native_runtime import plugin_handle_record_subrecords
        subrecords = plugin_handle_record_subrecords(target._rust_handle, 0x3000800)
        copied = [data for sig, data, _ in subrecords if sig == "CTDA"]
        assert struct.unpack_from("<I", copied[0], 12)[0] == 0x810
        assert struct.unpack_from("<I", copied[1], 12)[0] == 0x1000810
        assert all(struct.unpack_from("<I", c, 24)[0] == 0x820 for c in copied)
        actual = next(data for sig, data, _ in subrecords if sig == "VMAD")
        assert actual.endswith(struct.pack("<I", 0x830))


@pytest.mark.parametrize("authoring", [False, True])
def test_get_records_returns_all_134_large_records(tmp_path, authoring):
    from creation_lib.esp import Plugin
    path = tmp_path / "B21_Bulk.esp"
    with Plugin.new(path.name, game="fo4", masters=["Fallout4.esm", "DLCRobot.esm"]) as plugin:
        for index in range(134):
            record = plugin.new_record("REFR", form_id=0xFF000800 + index)
            record.editor_id = "B21_" + str(index)
            record.add_subrecord("NAME", struct.pack("<I", 0x1000ABC))
            record.add_subrecord("FULL", ("Large record " * 2000).encode() + b"\0")
            plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "get-records", str(path),
        *[f"{0x800 + i:06X}" for i in range(134)], *(["--authoring"] if authoring else [])])
    assert result.exit_code == 0, result.output[:300]
    report = json.loads(result.stdout)
    assert report["found"] == report["requested"] == len(report["records"]) == 134
    assert not report["missing"]
    if authoring:
        assert all(row["fields"][0]["Base"]["reference"] == {"plugin": "DLCRobot.esm", "object_id": "000ABC"}
                   for row in report["records"])


def test_yaml_scalar_diagnostics_do_not_flag_quoted_values_or_n():
    from cli.audit_commands import _scalar_warnings
    report = _scalar_warnings('key: on\nother: "on"\nid: 001234\nhex: 0x001234\nliteral: "0x001234"\nn: n\nflag: true\n')
    assert [(row["line"], row["value"], row["parsed_as"]) for row in report] == [
        (1, "on", "bool"), (3, "001234", "int"), (4, "0x001234", "int")]


def test_yaml_syntax_error_fails_audit(tmp_path, monkeypatch):
    from cli import audit_commands
    root = tmp_path / "B21_Test" / "yaml" / "records" / "GLOB"
    root.mkdir(parents=True)
    (root / "bad.yaml").write_text("fields: [\n")
    monkeypatch.setattr(audit_commands, "_load_whitelist", lambda game: {"GLOB": {"Value"}})
    result = CliRunner().invoke(cli, ["data", "audit-yaml", "B21_Test", "--mods-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "parse_errors"


def test_havok_diff_ignores_connected_object_order_and_reports_field_change():
    from creation_lib.inspection.havok import graph_differences
    before = '<hkpackfile toplevelobject="#root"><hksection><hkobject class="Root" name="#root"><hkparam name="child">#child</hkparam></hkobject><hkobject class="Clip" name="#child"><hkparam name="name">Idle</hkparam></hkobject></hksection></hkpackfile>'
    after = '<hkpackfile toplevelobject="#20"><hksection><hkobject class="Clip" name="#40"><hkparam name="name">Idle</hkparam></hkobject><hkobject class="Root" name="#20"><hkparam name="child">#40</hkparam></hkobject></hksection></hkpackfile>'
    assert not graph_differences(before, after)
    assert len(graph_differences(before, after.replace("Idle", "Walk"))) == 1


def test_havok_bindings_survive_native_roundtrip(tmp_path):
    from creation_lib.havok.native_runtime import load_native_module
    from creation_lib.inspection.behavior import behavior_report
    native = load_native_module()
    xml = '''<hkpackfile classversion="8" contentsversion="hk_2014.1.0-r1" toplevelobject="#0001"><hksection name="__data__">
      <hkobject name="#0001" class="hkbVariableBindingSet">
        <hkparam name="bindings" numelements="1"><hkobject>
          <hkparam name="memberPath">enable</hkparam><hkparam name="variableIndex">0</hkparam>
          <hkparam name="bitIndex">-1</hkparam><hkparam name="bindingType">1</hkparam>
        </hkobject></hkparam><hkparam name="indexOfBindingToEnable">0</hkparam>
      </hkobject></hksection></hkpackfile>'''
    path = tmp_path / "binding.xml"
    decoded = native.hkx_to_xml(native.xml_to_hkx(xml))
    path.write_text(decoded, encoding="utf-8")
    binding = behavior_report(path)["bindings"][0]
    assert binding["binding_type_explicit"]
    assert binding["binding_type"] in {"1", "BINDING_TYPE_CHARACTER_PROPERTY"}
    result = CliRunner().invoke(cli, ["build", "pack-hkx", str(path), str(tmp_path / "verified.hkx"), "--verify"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["verified"]


def test_hkx_verify_refuses_to_overwrite_when_decoder_loses_a_field(tmp_path, monkeypatch):
    from types import SimpleNamespace
    path, output = tmp_path / "in.xml", tmp_path / "out.hkx"
    before = '<hkpackfile><hkobject name="#1" class="Binding"><hkparam name="bindingType">1</hkparam></hkobject></hkpackfile>'
    after = before.replace('<hkparam name="bindingType">1</hkparam>', '')
    path.write_text(before)
    output.write_bytes(b"original")
    monkeypatch.setattr("creation_lib.havok.native_runtime.load_native_module",
                        lambda: SimpleNamespace(xml_to_hkx=lambda xml: b"new", hkx_to_xml=lambda data: after))
    result = CliRunner().invoke(cli, ["build", "pack-hkx", str(path), str(output), "--verify"])
    assert result.exit_code == 1
    assert "verification failed" in result.output
    assert output.read_bytes() == b"original"


def test_schema_layout_is_versioned_and_rejects_unknown_signature():
    runner = CliRunner()
    old = runner.invoke(cli, ["--game", "fo76", "data", "schema", "RACE.DATA", "--form-version", "131"])
    new = runner.invoke(cli, ["--game", "fo76", "data", "schema", "RACE.DATA", "--form-version", "188"])
    assert old.exit_code == new.exit_code == 0, (old.output, new.output)
    before, after = json.loads(old.stdout), json.loads(new.stdout)
    assert before["layout_available"] and after["layout_available"]
    assert before["fields"] != after["fields"]
    assert runner.invoke(cli, ["data", "schema", "AAAA.BBBB"]).exit_code == 1


def test_subgraph_ids_match_native_ck_ground_truth():
    runner = CliRunner()
    result = runner.invoke(cli, ["anim", "subgraph-id", "encode",
        r"Actors\Snallygaster\Behaviors\SnallygasterCoreBehavior.hkx", "--path", r"Actors\Snallygaster\Animations"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["id"] == "16837539554263781675"
    split = runner.invoke(cli, ["anim", "subgraph-id", "decode", report["id"]])
    assert json.loads(split.stdout)["behavior_crc32"] == report["behavior_crc32"]
    assert json.loads(split.stdout)["reversible"] is False


def test_race_subgraphs_use_native_block_boundaries(tmp_path):
    from creation_lib.esp import Plugin
    path = tmp_path / "B21_Races.esp"
    with Plugin.new(path.name, game="fo4") as plugin:
        record = plugin.new_record("RACE", form_id=0xFF000800)
        record.editor_id = "B21_Race"
        for sig, data in [("STKD", struct.pack("<I", 0x123)), ("SGNM", b"first.hkx\0"),
                          ("SAPT", b"first\0"), ("SAPT", b"parent\0"), ("SRAF", bytes(4)),
                          ("STKD", struct.pack("<I", 0x456)), ("SGNM", b"second.hkx\0"), ("SRAF", bytes(4))]:
            record.add_subrecord(sig, data)
        plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "race-subgraphs", str(path), "B21_Race"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["races"][0]["subgraphs"]
    assert [row["behavior"] for row in rows] == ["first.hkx", "second.hkx"]
    assert rows[0]["paths"] == ["first", "parent"]
    assert rows[0]["target_keywords"] == ["B21_Races.esp:000123"]
    assert rows[1]["target_keywords"] == ["B21_Races.esp:000456"]
    checked = CliRunner().invoke(cli, ["esp", "check-errors", str(path), "--no-fail"])
    assert checked.exit_code == 0, checked.output
    assert not [issue for issue in json.loads(checked.stdout)["issues"] if "out of order" in issue["message"]]


def _tree_snapshot(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_deploy_dry_run_preserves_files_and_undeploy_dry_run_agrees(tmp_path, monkeypatch):
    mod_dir, target = tmp_path / "mods/B21_Test", tmp_path / "Game/Data"
    for relative in ["F4SE/Plugins/test.ini", "F4SE/Plugins/test.dll", "PrismaUI_F4/views/test.html"]:
        src, dst = mod_dir / relative, target / relative
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    runner = CliRunner()
    before = _tree_snapshot(tmp_path)
    result = runner.invoke(cli, ["mod", "deploy", "B21_Test", "--no-esp", "--preserve-xse-inis", "--dry-run", "--data-dir", str(target)])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert plan["file_inventory_complete"]
    assert [op["action"] for op in plan["operations"]].count("preserve") == 1
    args = ["mod", "undeploy", "B21_Test", "--no-esp", "--data-dir", str(target)]
    result = runner.invoke(cli, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    planned = json.loads(result.stdout)["files"]
    assert _tree_snapshot(tmp_path) == before
    actual = runner.invoke(cli, args)
    assert actual.exit_code == 0, actual.output
    assert json.loads(actual.stdout)["files"] == planned


def test_deploy_dry_run_reports_pending_builds_without_creating_outputs(tmp_path, monkeypatch):
    mod_dir = tmp_path / "mods/B21_Test"
    mod_dir.mkdir(parents=True)
    (mod_dir / "plugin.json").write_text(json.dumps({"plugin": "Whole.esm", "items": []}))
    (mod_dir / "data").mkdir()
    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    before = _tree_snapshot(tmp_path)
    result = CliRunner().invoke(cli, ["mod", "deploy", "B21_Test", "--dry-run", "--data-dir", str(tmp_path / "Game/Data")])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert not report["file_inventory_complete"]
    assert {step["action"] for step in report["pending_build_steps"]} == {"build_plugin", "pack_archives"}
    assert _tree_snapshot(tmp_path) == before
    assert not (tmp_path / "Game").exists()


def test_loose_undeploy_dry_run_keeps_manifest_and_files(tmp_path):
    from creation_lib.build.loose_deploy import undeploy_loose_assets
    root, target = tmp_path / "mods/B21_Test", tmp_path / "Data"
    root.mkdir(parents=True)
    target.mkdir()
    (target / "test.esp").write_bytes(b"plugin")
    (root / ".loose_manifest.json").write_text(json.dumps({"game_data_dir": str(target), "files": [{"rel": "test.esp"}]}))
    before = _tree_snapshot(tmp_path)
    assert undeploy_loose_assets("B21_Test", project_root=tmp_path, dry_run=True) == ["test.esp"]
    assert _tree_snapshot(tmp_path) == before


def _roundtrip_record(tmp_path, signature, subrecords):
    from creation_lib.esp import Plugin
    from creation_lib.esp.native_runtime import plugin_handle_record_subrecords
    path = tmp_path / "B21_Roundtrip.esp"
    with Plugin.new(path.name, game="fo4") as plugin:
        record = plugin.new_record(signature, form_id=0xFF000800)
        record.editor_id = "B21_Roundtrip"
        for sig, data in subrecords:
            record.add_subrecord(sig, data)
        plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "get-record", str(path), "800", "--authoring"])
    assert result.exit_code == 0, result.output[:300]
    document = tmp_path / "record.json"
    document.write_text(result.stdout, encoding="utf-8")
    result = CliRunner().invoke(cli, ["esp", "set-record", str(path), str(document)])
    assert result.exit_code == 0, result.output[:300]
    with Plugin.load(path, game="fo4") as plugin:
        return [(sig, data) for sig, data, _ in plugin_handle_record_subrecords(plugin._rust_handle, 0x800) if sig != "EDID"]


@pytest.mark.parametrize("fixture", sorted((Path(__file__).resolve().parents[2] / "py_creation_lib/native/esp/src/nvnm/tests/fixtures").glob("*_fo4.nvnm.bin")), ids=lambda path: path.name)
def test_set_record_preserves_complete_nvnm_payload(tmp_path, fixture):
    rows = [("NVNM", fixture.read_bytes())]
    assert _roundtrip_record(tmp_path, "NAVM", rows) == rows


def test_set_record_does_not_duplicate_race_body_models(tmp_path):
    rows = [("NAM1", b""), ("MNAM", b""), ("INDX", struct.pack("<I", 0)), ("MODL", b"male.nif\0"),
            ("FNAM", b""), ("INDX", struct.pack("<I", 0)), ("MODL", b"female.nif\0")]
    assert _roundtrip_record(tmp_path, "RACE", rows) == rows


@pytest.mark.parametrize("valid", [True, False])
def test_xcri_checks_declared_variable_length_instead_of_fixed_header(tmp_path, valid):
    from creation_lib.esp import Plugin
    path = tmp_path / "B21_Xcri.esp"
    data = struct.pack("<IIIII", 1, 2, 7, 0x800, 7)
    if not valid:
        data += b"trailing"
    with Plugin.new(path.name, game="fo4") as plugin:
        record = plugin.new_record("CELL")
        record.add_subrecord("XCRI", data)
        plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "check-errors", str(path), "--no-fail"])
    assert result.exit_code == 0, result.output
    messages = [issue["message"] for issue in json.loads(result.stdout)["issues"] if "XCRI" in issue["message"]]
    assert messages == ([] if valid else ["Invalid XCRI counts or payload length"])


@pytest.mark.parametrize("returncode,produced,accepted", [(0, True, True), (1, True, False), (0, False, False)])
def test_stock_verification_requires_success_and_output(tmp_path, monkeypatch, returncode, produced, accepted):
    from creation_lib.build.papyrus_verification import verify_stock_sources
    compiler, source = tmp_path / "PapyrusCompiler.exe", tmp_path / "B21_Test.psc"
    compiler.write_bytes(b"fixture")
    source.write_text("Scriptname B21_Test")
    outputs = []

    def run(command, **kwargs):
        destination = Path(next(argument[3:] for argument in command if argument.startswith("-o=")))
        outputs.append(destination)
        assert kwargs["cwd"] == str(tmp_path)
        assert f"-i={tmp_path.resolve()}" in command
        if produced:
            (destination / "B21_Test.pex").write_bytes(b"fixture")
        return subprocess.CompletedProcess(command, returncode, "diagnostics", "")

    monkeypatch.setattr("creation_lib.build.papyrus_verification.subprocess.run", run)
    if accepted:
        verify_stock_sources([source], compiler=compiler, game_root=tmp_path, imports=[tmp_path])
    else:
        with pytest.raises(RuntimeError, match="Stock Papyrus verification failed"):
            verify_stock_sources([source], compiler=compiler, game_root=tmp_path, imports=[tmp_path])
    assert outputs and not outputs[0].exists()


def test_stock_rejection_preserves_existing_pex(tmp_path, monkeypatch):
    from creation_lib.build.deployer import compile_papyrus
    mod_dir, game_data = tmp_path / "mods/B21_Test", tmp_path / "Game/Data"
    source = mod_dir / "Scripts/Source/User/B21_Test.psc"
    pex = mod_dir / "data/Scripts/B21_Test.pex"
    source.parent.mkdir(parents=True)
    pex.parent.mkdir(parents=True)
    source.write_text("Scriptname B21_Test")
    pex.write_bytes(b"old")
    base = game_data / "Scripts/Source/Base"
    base.mkdir(parents=True)
    (base / "ObjectReference.psc").write_text("Scriptname ObjectReference")

    def reject(*args, **kwargs):
        raise RuntimeError("stock rejected")

    monkeypatch.setattr("creation_lib.build.papyrus_verification.verify_stock_sources", reject)
    monkeypatch.setattr("creation_lib.pex.native_runtime.compile_psc", lambda *a, **kw: pytest.fail("Native compiler ran before stock acceptance"))
    with pytest.raises(RuntimeError, match="stock rejected"):
        compile_papyrus(mod_dir, "fo4", game_data, verify_stock=True)
    assert pex.read_bytes() == b"old"


def test_parallel_yaml_exports_keep_separate_destinations_and_report_errors(tmp_path, monkeypatch):
    from threading import Barrier
    from creation_lib.db.index_builder import regenerate_esm_yaml_cache
    data = tmp_path / "Game/Data"
    data.mkdir(parents=True)
    for name in ("First.esm", "Second.esm"):
        (data / name).write_bytes(b"fixture")
    barrier = Barrier(2, timeout=10)

    def export(source, destination, **kwargs):
        barrier.wait()
        if Path(source).stem == "Second":
            raise RuntimeError("fixture export failure")
        Path(destination, "plugin.yaml").write_text(Path(source).name)

    monkeypatch.setattr("creation_lib.esp.native_runtime.export_authoring_dir_native", export)
    result = regenerate_esm_yaml_cache("fo4", game_data_dir=data, project_root=tmp_path,
        db_dir=tmp_path / "cache", plugins=["First.esm", "Second.esm"], workers=2)
    assert list(result) == ["First.esm", "Second.esm"]
    assert result == {"First.esm": "exported", "Second.esm": "error: fixture export failure"}
    assert list((tmp_path / "cache").rglob("plugin.yaml"))[0].read_text() == "First.esm"
    with pytest.raises(ValueError, match="share a cache directory"):
        regenerate_esm_yaml_cache("fo4", game_data_dir=data, project_root=tmp_path,
            db_dir=tmp_path / "cache", plugins=["First.esm", "First.esp"], workers=2)


def test_whole_plugin_undeploy_uses_selected_filename(tmp_path, monkeypatch):
    mod_dir, target = tmp_path / "mods/B21_Test", tmp_path / "Game/Data"
    mod_dir.mkdir(parents=True)
    target.mkdir(parents=True)
    (mod_dir / "Whole.esm").write_bytes(b"plugin")
    for name in ("Whole.esm", "Whole - Main.ba2", "Unrelated.esp"):
        (target / name).write_bytes(b"installed")
    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    args = ["mod", "undeploy", "B21_Test", "--source", "Whole.esm", "--data-dir", str(target)]
    runner = CliRunner()
    result = runner.invoke(cli, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    files = json.loads(result.stdout)["files"]
    assert set(files) == {"Whole.esm", "Whole - Main.ba2"}
    assert (target / "Whole.esm").is_file()
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["files"] == files
    assert [path.name for path in target.iterdir()] == ["Unrelated.esp"]


def test_loose_deploy_preview_rejects_colliding_asset_sources(tmp_path):
    from creation_lib.build.deploy_plan import plan_deploy
    mod_dir = tmp_path / "B21_Test"
    for relative in ("B21_Test.esp", "Meshes/test.nif", "data/Meshes/test.nif"):
        path = mod_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="duplicate loose deployment path"):
        plan_deploy(mod_dir, tmp_path / "Data", game="fo4", loose=True)
    assert not (tmp_path / "Data").exists()


def test_wildcard_listing_includes_unnamed_records_and_display_names(tmp_path):
    from creation_lib.esp import Plugin
    path = tmp_path / "B21_Names.esp"
    with Plugin.new(path.name, game="fo4") as plugin:
        for i, named in enumerate((True, False)):
            record = plugin.new_record("STAT", form_id=0xFF000800 + i)
            if named:
                record.editor_id = "B21_Named"
                record.add_subrecord("FULL", b"Display name\0")
            plugin.add_record(record)
        plugin.save(path)
    result = CliRunner().invoke(cli, ["esp", "list-records", str(path), "--match", "*", "--full"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["records"]
    assert len(rows) == 2
    assert rows[0]["full_name"] == "Display name"
    assert rows[1]["editor_id"] is None


def test_version_warns_on_changed_workspace(tmp_path, monkeypatch):
    from cli._build_info import write_build_info
    (tmp_path / "cli").mkdir()
    source = tmp_path / "cli/main.py"
    source.write_text("before")
    (tmp_path / "VERSION").write_text("test")
    write_build_info(tmp_path, tmp_path / "modkit_build_info.json")
    source.write_text("after")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    result = CliRunner().invoke(cli, ["version", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "differs from the workspace source" in result.stderr


def test_parallel_yaml_cli_initializes_native_before_workers(tmp_path):
    from cli.tests.test_inspection_reports import placed_plugin
    placed_plugin(tmp_path / "First.esm")
    placed_plugin(tmp_path / "Second.esm")
    cache = tmp_path / "selected-cache"
    result = subprocess.run([sys.executable, "-c", "from cli.main import cli; cli()",
        "--db-dir", str(cache), "index", "regen-yaml", "--game", "fo4", "--data-dir", str(tmp_path),
        "--plugin", "First.esm", "--plugin", "Second.esm", "--workers", "2"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["plugins"] == {"First.esm": "exported", "Second.esm": "exported"}
    assert (cache / "fo4_esm_yaml/First/plugin.yaml").is_file()
    assert (cache / "fo4_esm_yaml/Second/plugin.yaml").is_file()
