import inspect
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from click.testing import CliRunner


def test_ck_animdata_uses_standard_deploy_before_generation(tmp_path, monkeypatch):
    from cli.main import cli
    from app import paths
    from cli import ck_commands
    from creation_lib.build import deployer
    from creation_lib.ck import automation

    app_root = tmp_path
    mod_dir = app_root / "mods" / "B21_Test"
    game_dir = tmp_path / "Fallout 4"
    game_data_dir = game_dir / "Data"
    mod_dir.mkdir(parents=True)
    game_data_dir.mkdir(parents=True)
    (mod_dir / ".game").write_text("fo4", encoding="utf-8")
    (mod_dir / "B21_Test.esp").write_text("plugin", encoding="utf-8")
    stale_animtext = mod_dir / "data" / "meshes" / "AnimTextData"
    stale_animtext.mkdir(parents=True)
    (stale_animtext / "stale.txt").write_text("old", encoding="utf-8")

    calls: list[tuple[str, dict]] = []
    deploy_signature = inspect.signature(deployer.deploy_mod)

    def fake_deploy_mod(name, **kwargs):
        deploy_signature.bind(name, **kwargs)
        assert not stale_animtext.exists()
        calls.append(("deploy", {"name": name, **kwargs}))

    def fake_check_plugin_errors(plugin_file, game):
        calls.append(("validate", {"plugin_file": plugin_file, "game": game}))

    def fake_generate_anim_data(name, **kwargs):
        calls.append(("animdata", {"name": name, **kwargs}))
        out_dir = mod_dir / "data" / "meshes" / "AnimTextData"
        out_dir.mkdir(parents=True)
        return out_dir

    monkeypatch.setattr(paths, "get_app_root", lambda: app_root)
    monkeypatch.setattr(paths, "get_db_dir", lambda: app_root / "data")
    monkeypatch.setattr(paths, "get_resource_dir", lambda: app_root / "resource")
    monkeypatch.setattr(ck_commands, "_resolve_game_dir", lambda _game: game_dir)
    monkeypatch.setattr(deployer, "deploy_mod", fake_deploy_mod)
    monkeypatch.setattr(ck_commands, "_check_plugin_errors_before_animdata", fake_check_plugin_errors)
    monkeypatch.setattr(automation, "generate_anim_data", fake_generate_anim_data)

    result = CliRunner().invoke(cli, ["--game", "fo4", "ck", "animdata", "B21_Test"])

    assert result.exit_code == 0, result.output
    assert [call[0] for call in calls] == ["deploy", "validate", "animdata"]
    assert calls[0][1]["skip_pack"] is False
    assert calls[0][1]["esp_only"] is False
    assert calls[0][1]["game_data_dir"] == game_data_dir
    assert calls[1][1]["plugin_file"] == mod_dir / "B21_Test.esp"
    assert calls[1][1]["game"] == "fo4"
    assert calls[2][1]["game_data_dir"] == game_data_dir
    assert calls[2][1]["deploy_loose_data"] is False


@pytest.mark.parametrize("severity", ["warning", "error"])
def test_animdata_validation_warns_but_only_errors_block(monkeypatch, capsys, severity):
    from cli import ck_commands, esp_commands
    from creation_lib.esp import editor

    closed = []
    session = SimpleNamespace(
        load=lambda *_args, **_kwargs: SimpleNamespace(handle=1),
        close_all=lambda: closed.append(True),
    )
    issue = SimpleNamespace(
        severity=SimpleNamespace(value=severity),
        message="fixture issue",
        form_id=0x800,
        plugin_name="B21_Test.esp",
    )
    monkeypatch.setattr(editor, "EditorSession", lambda **_kwargs: session)
    monkeypatch.setattr(editor, "validate", lambda *_args, **_kwargs: [issue])
    monkeypatch.setattr(esp_commands, "_esp_master_search_paths", lambda *_args: [])
    if severity == "error":
        with pytest.raises(click.ClickException, match="ESP validation failed.*"):
            ck_commands._check_plugin_errors_before_animdata(Path("B21_Test.esp"), "fo4")
    else:
        ck_commands._check_plugin_errors_before_animdata(Path("B21_Test.esp"), "fo4")
        assert "WARNING: fixture issue" in capsys.readouterr().out
    assert closed == [True]


def test_loose_data_deploy_overwrites_stale_files_and_restores_them(tmp_path):
    from creation_lib.ck.automation import _cleanup_loose_data, _deploy_loose_data

    mod_dir = tmp_path / "mod"
    game_data_dir = tmp_path / "Data"
    (mod_dir / "data" / "meshes").mkdir(parents=True)
    game_meshes = game_data_dir / "meshes"
    game_meshes.mkdir(parents=True)

    (mod_dir / "data" / "meshes" / "same.hkx").write_text("new", encoding="utf-8")
    (mod_dir / "data" / "meshes" / "missing.hkx").write_text("new", encoding="utf-8")
    (game_meshes / "same.hkx").write_text("old", encoding="utf-8")

    deployed = _deploy_loose_data(mod_dir, game_data_dir)

    assert (game_meshes / "same.hkx").read_text(encoding="utf-8") == "new"
    assert (game_meshes / "missing.hkx").read_text(encoding="utf-8") == "new"

    _cleanup_loose_data(deployed)

    assert (game_meshes / "same.hkx").read_text(encoding="utf-8") == "old"
    assert not (game_meshes / "missing.hkx").exists()


def test_loose_data_deploy_can_stage_only_requested_roots(tmp_path):
    from creation_lib.ck.automation import _cleanup_loose_data, _deploy_loose_data

    mod_dir = tmp_path / "mod"
    game_data_dir = tmp_path / "Data"
    (mod_dir / "data" / "Meshes").mkdir(parents=True)
    (mod_dir / "data" / "Textures").mkdir(parents=True)
    (mod_dir / "data" / "Meshes" / "a.hkx").write_text("mesh", encoding="utf-8")
    (mod_dir / "data" / "Textures" / "a.dds").write_text("texture", encoding="utf-8")

    deployed = _deploy_loose_data(mod_dir, game_data_dir, roots=("Meshes",))

    assert (game_data_dir / "Meshes" / "a.hkx").read_text(encoding="utf-8") == "mesh"
    assert not (game_data_dir / "Textures" / "a.dds").exists()

    _cleanup_loose_data(deployed)

    assert not (game_data_dir / "Meshes" / "a.hkx").exists()


def test_generate_anim_data_uses_current_plugin_even_when_data_copy_exists(
    tmp_path, monkeypatch
):
    from creation_lib.ck import automation

    mod_dir = tmp_path / "mods" / "B21_Test"
    game_dir = tmp_path / "Fallout 4"
    game_data_dir = game_dir / "Data"
    mod_dir.mkdir(parents=True)
    game_data_dir.mkdir(parents=True)
    (mod_dir / "B21_Test.esp").write_text("new plugin", encoding="utf-8")
    deployed_plugin = game_data_dir / "B21_Test.esp"
    deployed_plugin.write_text("old plugin", encoding="utf-8")

    class FakeCkEnv:
        def __init__(self, ck_exe: Path):
            self.ck_exe = ck_exe

        def __enter__(self) -> Path:
            return self.ck_exe

        def __exit__(self, *_exc) -> None:
            return None

    def fake_run(_args, **_kwargs):
        assert deployed_plugin.read_text(encoding="utf-8") == "new plugin"
        out_dir = mod_dir / "data" / "meshes" / "AnimTextData"
        out_dir.mkdir(parents=True)
        (out_dir / "generated.txt").write_text("ok", encoding="utf-8")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(automation, "ck_safe_env", lambda _game_dir: FakeCkEnv(game_dir / "CreationKit.exe"))
    monkeypatch.setattr(automation.subprocess, "run", fake_run)

    result = automation.generate_anim_data(
        "B21_Test",
        game="fo4",
        game_dir=game_dir,
        game_data_dir=game_data_dir,
        mod_dir=mod_dir,
    )

    assert result == mod_dir / "data" / "meshes" / "AnimTextData"
    assert deployed_plugin.read_text(encoding="utf-8") == "old plugin"


def test_generate_anim_data_accepts_explicit_esm_plugin_name(tmp_path, monkeypatch):
    from creation_lib.ck import automation

    mod_dir = tmp_path / "mods" / "SeventySix"
    game_dir = tmp_path / "Fallout 4"
    game_data_dir = game_dir / "Data"
    mod_dir.mkdir(parents=True)
    game_data_dir.mkdir(parents=True)
    (mod_dir / "SeventySix.esm").write_text("plugin", encoding="utf-8")

    class FakeCkEnv:
        def __init__(self, ck_exe: Path):
            self.ck_exe = ck_exe

        def __enter__(self) -> Path:
            return self.ck_exe

        def __exit__(self, *_exc) -> None:
            return None

    seen_args = []

    def fake_run(args, **_kwargs):
        seen_args.append(args)
        out_dir = mod_dir / "data" / "meshes" / "AnimTextData"
        out_dir.mkdir(parents=True)
        (out_dir / "generated.txt").write_text("ok", encoding="utf-8")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(
        automation,
        "ck_safe_env",
        lambda _game_dir: FakeCkEnv(game_dir / "CreationKit.exe"),
    )
    monkeypatch.setattr(automation.subprocess, "run", fake_run)

    result = automation.generate_anim_data(
        "WrongName",
        game="fo4",
        game_dir=game_dir,
        game_data_dir=game_data_dir,
        mod_dir=mod_dir,
        plugin_name="SeventySix.esm",
        deploy_loose_data=False,
    )

    assert result == mod_dir / "data" / "meshes" / "AnimTextData"
    assert seen_args[0][1] == "-GenerateAnimInfo:SeventySix.esm"


def test_generate_anim_data_starts_from_clean_animtextdata(tmp_path, monkeypatch):
    from creation_lib.ck import automation

    mod_dir = tmp_path / "mods" / "B21_Test"
    game_dir = tmp_path / "Fallout 4"
    game_data_dir = game_dir / "Data"
    local_animtext = mod_dir / "data" / "meshes" / "AnimTextData"
    game_animtext = game_data_dir / "meshes" / "AnimTextData"
    local_animtext.mkdir(parents=True)
    game_animtext.mkdir(parents=True)
    game_data_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "B21_Test.esp").write_text("plugin", encoding="utf-8")
    (local_animtext / "stale-local.txt").write_text("old", encoding="utf-8")
    (game_animtext / "stale-game.txt").write_text("old", encoding="utf-8")

    class FakeCkEnv:
        def __init__(self, ck_exe: Path):
            self.ck_exe = ck_exe

        def __enter__(self) -> Path:
            return self.ck_exe

        def __exit__(self, *_exc) -> None:
            return None

    def fake_run(_args, **_kwargs):
        assert not (local_animtext / "stale-local.txt").exists()
        assert not game_animtext.exists()
        local_animtext.mkdir(parents=True)
        (local_animtext / "generated.txt").write_text("ok", encoding="utf-8")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr(automation, "ck_safe_env", lambda _game_dir: FakeCkEnv(game_dir / "CreationKit.exe"))
    monkeypatch.setattr(automation.subprocess, "run", fake_run)

    result = automation.generate_anim_data(
        "B21_Test",
        game="fo4",
        game_dir=game_dir,
        game_data_dir=game_data_dir,
        mod_dir=mod_dir,
    )

    assert result == local_animtext
    assert (local_animtext / "generated.txt").read_text(encoding="utf-8") == "ok"
    assert (game_animtext / "stale-game.txt").read_text(encoding="utf-8") == "old"
