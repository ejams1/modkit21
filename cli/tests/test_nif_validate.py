import json
from pathlib import Path

from click.testing import CliRunner

from cli.main import cli
from creation_lib.dds import native_runtime as dds_native_runtime
from creation_lib.nif import native_runtime


def test_nif_validate_reports_native_findings_without_writing(tmp_path, monkeypatch):
    nif_path = tmp_path / "mesh.nif"
    nif_path.write_bytes(b"not parsed by mocked native runtime")
    calls = []

    def fake_validate(path, output_path=None, fix=False):
        calls.append((path, output_path, fix))
        return {
            "game": "fo76",
            "changed": False,
            "changes": [],
            "warnings": [],
            "findings": [
                {
                    "severity": "error",
                    "rule": "bsx-flags",
                    "block_id": 1,
                    "block_type": "BSXFlags",
                    "field": "Integer Data",
                    "message": "Flags require normalization",
                }
            ],
        }

    monkeypatch.setattr(native_runtime, "validate_nif_file_raw", fake_validate)
    result = CliRunner().invoke(
        cli, ["--format", "json", "nif", "validate", str(nif_path)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files"] == 1
    assert payload["valid"] == 0
    assert payload["games"] == {"fo76": 1}
    assert payload["findings"] == {"error": 1}
    assert calls == [(str(nif_path.resolve()), None, False)]


def test_nif_validate_fixes_directory_to_parallel_output(tmp_path, monkeypatch):
    source = tmp_path / "source"
    output_root = tmp_path / "fixed"
    report_path = tmp_path / "validate.json"
    first = source / "actors" / "first.nif"
    second = source / "props" / "second.NIF"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    calls = []

    def fake_validate(path, output_path=None, fix=False):
        calls.append((Path(path), Path(output_path), fix))
        return {
            "game": "fo4",
            "changed": True,
            "changes": ["Shader flags normalized"],
            "warnings": [],
            "findings": [],
        }

    monkeypatch.setattr(native_runtime, "validate_nif_file_raw", fake_validate)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "validate",
            str(source),
            "--fix",
            "--output",
            str(output_root),
            "--report",
            str(report_path),
            "--jobs",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["fixed_files"] == 2
    assert payload["changes"] == 2
    assert report_path.is_file()
    assert set(calls) == {
        (first.resolve(), (output_root / "actors" / "first.nif").resolve(), True),
        (second.resolve(), (output_root / "props" / "second.NIF").resolve(), True),
    }


def test_nif_validate_routes_kf_and_dds_with_optional_checks(tmp_path, monkeypatch):
    source = tmp_path / "assets"
    kf_path = source / "animation.kf"
    dds_path = source / "texture.dds"
    source.mkdir()
    kf_path.write_bytes(b"kf")
    dds_path.write_bytes(b"dds")
    nif_calls = []
    dds_calls = []

    def fake_validate_nif(path, output_path=None, fix=False, include_optional=False):
        nif_calls.append((Path(path), output_path, fix, include_optional))
        return {
            "game": "oblivion",
            "changed": False,
            "changes": [],
            "warnings": [],
            "findings": [],
        }

    def fake_validate_dds(path, *, include_optional=False):
        dds_calls.append((Path(path), include_optional))
        return {
            "game": "dds",
            "changed": False,
            "changes": [],
            "warnings": [],
            "findings": [
                {
                    "severity": "error",
                    "rule": "sse-unsupported-texture-format",
                    "block_id": None,
                    "block_type": None,
                    "field": None,
                    "message": "unsupported",
                }
            ],
        }

    monkeypatch.setattr(native_runtime, "validate_nif_file_raw", fake_validate_nif)
    monkeypatch.setattr(dds_native_runtime, "validate_dds_file_raw", fake_validate_dds)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "validate",
            str(source),
            "--include-optional",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files"] == 2
    assert payload["games"] == {"dds": 1, "oblivion": 1}
    assert payload["include_optional"] is True
    assert nif_calls == [(kf_path.resolve(), None, False, True)]
    assert dds_calls == [(dds_path.resolve(), True)]


def test_nif_nif_features_filters_the_native_catalog(monkeypatch):
    monkeypatch.setattr(
        native_runtime,
        "nif_features_raw",
        lambda: {
            "processors": [
                {
                    "id": "check-for-errors",
                    "category": "Report",
                    "parity": "partial",
                },
                {
                    "id": "analyze-mesh",
                    "category": "Report",
                    "parity": "missing",
                },
                {
                    "id": "soft-particles",
                    "category": "Shader",
                    "parity": "missing",
                },
            ],
            "checks": [{"id": "invalid-geometry"}],
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "features",
            "--category",
            "report",
            "--parity",
            "missing",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [processor["id"] for processor in payload["processors"]] == ["analyze-mesh"]
    assert payload["summary"] == {
        "processors": 1,
        "checks": 1,
        "parity": {"complete": 0, "missing": 1, "partial": 0},
        "commands": {"process": 0, "report": 0, "validate": 0},
    }


def test_nif_report_routes_processor_options_to_native(tmp_path, monkeypatch):
    nif_path = tmp_path / "mesh.nif"
    nif_path.write_bytes(b"nif")
    calls = []

    def fake_report(path, processor, options=None):
        calls.append((Path(path), processor, options))
        return {
            "processor": processor,
            "path": path,
            "game": "skyrimse",
            "data": {"entries": []},
        }

    monkeypatch.setattr(native_runtime, "nif_report_raw", fake_report)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "report",
            "find-uvs",
            str(nif_path),
            "--u-min",
            "-0.25",
            "--v-max",
            "2.0",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files"] == 1
    assert payload["failures"] == 0
    assert calls[0][0] == nif_path.resolve()
    assert calls[0][1] == "find-uvs"
    assert calls[0][2]["u_min"] == -0.25
    assert calls[0][2]["v_max"] == 2.0


def test_nif_validate_selects_one_optional_nif_check(tmp_path, monkeypatch):
    nif_path = tmp_path / "mesh.nif"
    nif_path.write_bytes(b"nif")
    calls = []
    monkeypatch.setattr(
        native_runtime,
        "nif_features_raw",
        lambda: {
            "processors": [],
            "checks": [
                {"id": "clamped-tiling-uvs", "optional": True},
                {"id": "vertex-colors", "optional": False},
            ],
        },
    )

    def fake_validate(path, output_path=None, fix=False, include_optional=False):
        calls.append(include_optional)
        return {
            "game": "skyrim",
            "changed": False,
            "changes": [],
            "warnings": [],
            "findings": [
                {
                    "severity": "warning",
                    "rule": "clamped-tiling-uvs",
                    "check": "clamped-tiling-uvs",
                    "block_id": 1,
                    "block_type": "NiTriShapeData",
                    "field": None,
                    "message": "clamped",
                },
                {
                    "severity": "info",
                    "rule": "all-white-vertex-colors",
                    "check": "vertex-colors",
                    "block_id": 1,
                    "block_type": "NiTriShapeData",
                    "field": None,
                    "message": "white",
                },
            ],
        }

    monkeypatch.setattr(native_runtime, "validate_nif_file_raw", fake_validate)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "validate",
            str(nif_path),
            "--check",
            "clamped-tiling-uvs",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["include_optional"] is True
    assert payload["checks"] == ["clamped-tiling-uvs"]
    assert payload["rules"] == {"clamped-tiling-uvs": 1}
    assert calls == [True]


def test_nif_find_textures_filters_and_copies_matches(tmp_path, monkeypatch):
    source = tmp_path / "textures"
    output_root = tmp_path / "matches"
    matching = source / "actors" / "matching.dds"
    skipped = source / "skipped.dds"
    matching.parent.mkdir(parents=True)
    matching.write_bytes(b"matching")
    skipped.write_bytes(b"skipped")

    def fake_texdiag(path):
        is_matching = Path(path).name == "matching.dds"
        return {
            "width": 256 if is_matching else 300,
            "height": 256,
            "mip_levels": 9,
            "dxgi_format": 98 if is_matching else 71,
            "format": "BC7_UNORM" if is_matching else "BC1_UNORM",
            "bits_per_pixel": 8,
            "has_alpha": True,
            "is_cubemap": False,
            "is_compressed": True,
            "is_xbox": False,
            "is_power_of_two": is_matching,
        }

    monkeypatch.setattr(dds_native_runtime, "texdiag_info", fake_texdiag)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "report",
            "find-textures",
            str(source),
            "--resolution",
            "gte-256",
            "--texture-format",
            "BC7_UNORM",
            "--compressed",
            "yes",
            "--copy-to",
            str(output_root),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files"] == 2
    assert payload["matched"] == 1
    assert (output_root / "actors" / "matching.dds").read_bytes() == b"matching"
    assert not (output_root / "skipped.dds").exists()


def test_nif_find_textures_can_include_dds_header_dump(tmp_path, monkeypatch):
    texture = tmp_path / "texture.dds"
    header = bytearray(148)
    header[:4] = b"DDS "
    header[4:8] = (124).to_bytes(4, "little")
    header[12:16] = (512).to_bytes(4, "little")
    header[16:20] = (1024).to_bytes(4, "little")
    header[84:88] = b"DX10"
    header[128:132] = (98).to_bytes(4, "little")
    header[140:144] = (1).to_bytes(4, "little")
    texture.write_bytes(header)
    monkeypatch.setattr(
        dds_native_runtime,
        "texdiag_info",
        lambda path: {
            "width": 1024,
            "height": 512,
            "mip_levels": 1,
            "dxgi_format": 98,
            "format": "BC7_UNORM",
            "bits_per_pixel": 8,
            "has_alpha": True,
            "is_cubemap": False,
            "is_compressed": True,
            "is_xbox": False,
            "is_power_of_two": True,
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "report",
            "find-textures",
            str(texture),
            "--header-dump",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["results"][0]["data"]
    assert data["header"]["width"] == 1024
    assert data["header"]["height"] == 512
    assert data["header"]["dx10"]["dxgi_format"] == 98


def test_nif_process_routes_replace_assets_options(tmp_path, monkeypatch):
    source = tmp_path / "mesh.nif"
    target = tmp_path / "fixed.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo3/fnv",
            "changed": True,
            "changes": ["replaced"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "replace-assets",
            str(source),
            "--output",
            str(target),
            "--search",
            "textures/old",
            "--replace",
            "textures/new",
            "--case-sensitive",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["changed"] == 1
    assert calls[0][0] == source.resolve()
    assert calls[0][1] == target.resolve()
    assert calls[0][2] == "replace-assets"
    assert calls[0][3]["search"] == "textures/old"
    assert calls[0][3]["replace"] == "textures/new"
    assert calls[0][3]["case_sensitive"] is True


def test_nif_process_replace_assets_accepts_material_files(tmp_path, monkeypatch):
    source = tmp_path / "material.bgsm"
    target = tmp_path / "fixed.bgsm"
    source.write_bytes(b"bgsm")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo4-material",
            "changed": True,
            "changes": ["replaced"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "replace-assets",
            str(source),
            "--output",
            str(target),
            "--replacement",
            "textures\\old",
            "textures\\new",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == source.resolve()
    assert calls[0][2] == "replace-assets"


def test_nif_process_uses_nif_processor_defaults(tmp_path, monkeypatch):
    source = tmp_path / "mesh.nif"
    target = tmp_path / "fixed.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo4",
            "changed": False,
            "changes": [],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    for processor in ("attach-parent", "optimize-mesh"):
        result = CliRunner().invoke(
            cli,
            [
                "--format",
                "json",
                "nif",
                "process",
                processor,
                str(source),
                "--output",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output

    attach_options = calls[0][1]
    assert attach_options["find_name"] == "##SightingNode"
    assert attach_options["parent_name"] == "##ISControl"
    optimize_options = calls[1][1]
    assert optimize_options["vertex_cache"] is True
    assert optimize_options["overdraw"] is True
    assert optimize_options["vertex_fetch"] is True


def test_nif_process_accepts_symbolic_havok_materials(tmp_path, monkeypatch):
    source = tmp_path / "mesh.nif"
    target = tmp_path / "fixed.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append(options)
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "skyrimse",
            "changed": True,
            "changes": ["replaced"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "search-havok-material",
            str(source),
            "--output",
            str(target),
            "--material-search",
            "SKY_HAV_MAT_BOTTLE",
            "--material-replace",
            "SKY_HAV_MAT_BONE_ACTOR",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["material_search"] == "SKY_HAV_MAT_BOTTLE"
    assert calls[0]["material_replace"] == "SKY_HAV_MAT_BONE_ACTOR"


def test_nif_process_replace_assets_report_only_needs_no_destination(
    tmp_path, monkeypatch
):
    source = tmp_path / "mesh.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo4",
            "report_only": True,
            "changed": True,
            "changes": ["would replace"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "replace-assets",
            str(source),
            "--replacement",
            "textures\\old",
            "textures\\new",
            "--fix-absolute",
            "--report-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == source.resolve()
    assert calls[0][1] == source.resolve()
    assert calls[0][3]["pairs"] == [["textures\\old", "textures\\new"]]
    assert calls[0][3]["fix_absolute"] is True
    assert calls[0][3]["report_only"] is True


def test_nif_process_routes_tangent_options(tmp_path, monkeypatch):
    source = tmp_path / "mesh.nif"
    target = tmp_path / "fixed.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "skyrimse",
            "changed": True,
            "changes": ["updated"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "update-tangents",
            str(source),
            "--output",
            str(target),
            "--add-if-missing",
            "--face-normals",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "update-tangents"
    assert calls[0][1]["add_if_missing"] is True
    assert calls[0][1]["face_normals"] is True


def test_nif_process_routes_universal_tweaker_options(tmp_path, monkeypatch):
    source = tmp_path / "animation.kf"
    source.write_bytes(b"kf")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fnv",
            "report_only": True,
            "changed": True,
            "changes": ["would tweak"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "universal-tweaker",
            str(source),
            "--report-only",
            "--block",
            "NiControllerSequence",
            "--field-path",
            "Controlled Blocks\\[*]\\Priority",
            "--value",
            "10",
            "--old-value-check",
            "--old-path",
            "Node Name",
            "--old-mode",
            "regex",
            "--old-value",
            "Neck|Head",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == source.resolve()
    assert calls[0][1] == "universal-tweaker"
    assert calls[0][2]["blocks"] == ["NiControllerSequence"]
    assert calls[0][2]["field_path"] == "Controlled Blocks\\[*]\\Priority"
    assert calls[0][2]["old_mode"] == "regex"
    assert calls[0][2]["report_only"] is True


def test_nif_process_json_converter_uses_nif_output_names(tmp_path, monkeypatch):
    source = tmp_path / "input"
    destination = tmp_path / "output"
    source.mkdir()
    destination.mkdir()
    (source / "mesh.nif").write_bytes(b"nif")
    (source / "animation.kf.json").write_text("{}")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fnv",
            "changed": True,
            "changes": ["converted"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "json-converter",
            str(source),
            "--output",
            str(destination),
            "--decimal-digits",
            "12",
        ],
    )

    assert result.exit_code == 0, result.output
    assert {(path.name, target.name) for path, target, _ in calls} == {
        ("mesh.nif", "mesh.nif.json"),
        ("animation.kf.json", "animation.kf"),
    }
    assert all(options["decimal_digits"] == 12 for _, _, options in calls)


def test_nif_process_parses_shader_flag_masks(tmp_path, monkeypatch):
    source = tmp_path / "mesh.nif"
    target = tmp_path / "fixed.nif"
    source.write_bytes(b"nif")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((processor, options))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo4",
            "changed": True,
            "changes": ["updated"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "update-shader-flags",
            str(source),
            "--output",
            str(target),
            "--flags1",
            "0x80",
            "--flags2",
            "4",
            "--flag-mode",
            "remove",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "update-shader-flags"
    assert calls[0][1]["flags1"] == 0x80
    assert calls[0][1]["flags2"] == 4
    assert calls[0][1]["mode"] == "remove"


def test_nif_process_routes_kf_animation_processor(tmp_path, monkeypatch):
    source = tmp_path / "animation.kf"
    target = tmp_path / "fixed.kf"
    source.write_bytes(b"kf")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), processor))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "fo3/fnv",
            "changed": True,
            "changes": ["fixed"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "fix-exported-kf",
            str(source),
            "--output",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(source.resolve(), target.resolve(), "fix-exported-kf")]


def test_nif_process_remove_unused_nodes_accepts_kfm(tmp_path, monkeypatch):
    source = tmp_path / "model.kfm"
    target = tmp_path / "fixed.kfm"
    source.write_bytes(b"kfm")
    calls = []

    def fake_process(path, output_path, processor, options=None):
        calls.append((Path(path), Path(output_path), processor))
        return {
            "processor": processor,
            "path": path,
            "output": output_path,
            "game": "oblivion",
            "changed": True,
            "changes": ["removed"],
        }

    monkeypatch.setattr(native_runtime, "nif_process_raw", fake_process)
    result = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "nif",
            "process",
            "remove-unused-nodes",
            str(source),
            "--output",
            str(target),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(source.resolve(), target.resolve(), "remove-unused-nodes")]
