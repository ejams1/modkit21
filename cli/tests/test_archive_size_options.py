from __future__ import annotations

from click.testing import CliRunner

from cli.main import cli
from creation_lib.build.deployer import DeployResult


def test_build_pack_forwards_archive_max_size_option(monkeypatch, tmp_path):
    calls: list[dict] = []

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.packer.pack_mod",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    result = CliRunner().invoke(
        cli,
        ["--game", "fo4", "build", "pack", "B21_Test", "--archive-max-size-gb", "2"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["archive_max_bytes"] == 2 * 1024**3
    assert calls[0]["expanded_archives"] is False


def test_build_pack_defaults_archive_max_size_to_16_gib(monkeypatch, tmp_path):
    calls: list[dict] = []

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.packer.pack_mod",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    result = CliRunner().invoke(cli, ["--game", "fo4", "build", "pack", "B21_Test"])

    assert result.exit_code == 0, result.output
    assert calls[0]["archive_max_bytes"] == 16 * 1024**3
    assert calls[0]["xbox_max_res"] == 0
    assert calls[0]["ps_max_res"] == 0


def test_build_pack_forwards_playstation_options(monkeypatch, tmp_path):
    calls: list[dict] = []

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.packer.pack_mod",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "build",
            "pack",
            "B21_Test",
            "--ps",
            "--ps-max-res",
            "2048",
            "--ps-effects-max-res",
            "1024",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["pc"] is False
    assert calls[0]["ps"] is True
    assert calls[0]["ps_max_res"] == 2048
    assert calls[0]["ps_effects_max_res"] == 1024


def test_build_pack_forwards_expanded_archives_option(monkeypatch, tmp_path):
    calls: list[dict] = []

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.packer.pack_mod",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    result = CliRunner().invoke(
        cli,
        ["--game", "fo4", "build", "pack", "B21_Test", "--expanded-archives"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["expanded_archives"] is True


def test_mod_deploy_forwards_archive_max_size_option(monkeypatch, tmp_path):
    calls: list[dict] = []
    game_data_dir = tmp_path / "Game" / "Data"

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.deployer.deploy_mod",
        lambda *args, **kwargs: calls.append(kwargs) or DeployResult(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "mod",
            "deploy",
            "B21_Test",
            "--skip-build",
            "--skip-pack",
            "--data-dir",
            str(game_data_dir),
            "--archive-max-size-gb",
            "2.5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["archive_max_bytes"] == int(2.5 * 1024**3)
    assert calls[0]["expanded_archives"] is False


def test_mod_deploy_defaults_archive_max_size_to_16_gib(monkeypatch, tmp_path):
    calls: list[dict] = []
    game_data_dir = tmp_path / "Game" / "Data"

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.deployer.deploy_mod",
        lambda *args, **kwargs: calls.append(kwargs) or DeployResult(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "mod",
            "deploy",
            "B21_Test",
            "--skip-build",
            "--skip-pack",
            "--data-dir",
            str(game_data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["archive_max_bytes"] == 16 * 1024**3


def test_mod_deploy_forwards_playstation_options(monkeypatch, tmp_path):
    calls: list[dict] = []
    game_data_dir = tmp_path / "Game" / "Data"

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.deployer.deploy_mod",
        lambda *args, **kwargs: calls.append(kwargs) or DeployResult(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "mod",
            "deploy",
            "B21_Test",
            "--skip-build",
            "--skip-pack",
            "--data-dir",
            str(game_data_dir),
            "--ps",
            "--ps-max-res",
            "4096",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["ps"] is True
    assert calls[0]["ps_max_res"] == 4096


def test_mod_deploy_forwards_expanded_archives_option(monkeypatch, tmp_path):
    calls: list[dict] = []
    game_data_dir = tmp_path / "Game" / "Data"

    monkeypatch.setattr("app.paths.get_app_root", lambda: tmp_path)
    monkeypatch.setattr("app.paths.get_resource_dir", lambda: tmp_path / "resource")
    monkeypatch.setattr(
        "creation_lib.build.deployer.deploy_mod",
        lambda *args, **kwargs: calls.append(kwargs) or DeployResult(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--game",
            "fo4",
            "mod",
            "deploy",
            "B21_Test",
            "--skip-build",
            "--skip-pack",
            "--data-dir",
            str(game_data_dir),
            "--expanded-archives",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["expanded_archives"] is True
