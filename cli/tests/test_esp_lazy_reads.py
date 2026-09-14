"""CLI tests for the lazy read path on `modkit esp` query commands."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.main import cli
from creation_lib.esp.model import Record
from creation_lib.esp.plugin import Plugin


@pytest.fixture()
def plugin_path(tmp_path: Path) -> Path:
    plugin = Plugin.new("B21_LazyCli.esp", game="fo4")
    try:
        for index, editor_id in enumerate(["B21_First", "B21_Second", "B21_Third"]):
            record = Record("MISC", 0xFF000800 + index)
            record.add_subrecord("EDID", editor_id.encode("cp1252") + b"\x00")
            plugin.add_record(record)
        target = tmp_path / "B21_LazyCli.esp"
        plugin.save(target)
    finally:
        plugin.close()
    return target


def test_get_record_by_form_id_does_not_build_the_editor_id_index(
    plugin_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1 routing.

    _resolve_record_id called plugin.eid_index() before it ever tried parsing
    the argument as hex, so a plain FormID lookup built the whole EditorID index
    - hundreds of MB on a master-sized plugin, for a lookup needing none of it.
    """
    calls = []
    original = Plugin.eid_index

    def spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Plugin, "eid_index", spy)

    result = CliRunner().invoke(cli, ["esp", "get-record", str(plugin_path), "000800"])
    assert result.exit_code == 0, result.output
    assert "B21_First" in result.output
    assert not calls, "a hex FormID lookup must not touch the EditorID index"


def test_get_record_by_editor_id_still_resolves(plugin_path: Path) -> None:
    result = CliRunner().invoke(cli, ["esp", "get-record", str(plugin_path), "B21_Second"])
    assert result.exit_code == 0, result.output
    assert "B21_Second" in result.output


def test_get_record_by_editor_id_is_case_insensitive(plugin_path: Path) -> None:
    result = CliRunner().invoke(cli, ["esp", "get-record", str(plugin_path), "b21_second"])
    assert result.exit_code == 0, result.output
    assert "B21_Second" in result.output


def test_full_load_escape_hatch_returns_identical_output(plugin_path: Path) -> None:
    runner = CliRunner()
    lazy = runner.invoke(cli, ["esp", "get-record", str(plugin_path), "000801"])
    full = runner.invoke(cli, ["esp", "get-record", str(plugin_path), "000801", "--full-load"])
    assert lazy.exit_code == 0 and full.exit_code == 0, (lazy.output, full.output)
    assert lazy.output == full.output


def test_missing_form_id_reports_not_found_not_a_parse_error(plugin_path: Path) -> None:
    """A well-formed but absent FormID must not fall through to 'not a FormID'.

    Hex-first resolution returns the parsed number even when no record has it, so
    the caller reports a missing record rather than an unparseable argument.
    """
    result = CliRunner().invoke(cli, ["esp", "get-record", str(plugin_path), "0F0F0F"])
    assert "record not found" in result.output
    assert "not a FormID or EditorID" not in result.output


def test_list_records_and_count_agree_across_load_modes(plugin_path: Path) -> None:
    runner = CliRunner()
    for args in (["esp", "count", str(plugin_path)], ["esp", "list-records", str(plugin_path)]):
        lazy = runner.invoke(cli, args)
        full = runner.invoke(cli, [*args, "--full-load"])
        assert lazy.exit_code == 0 and full.exit_code == 0, (lazy.output, full.output)
        assert lazy.output == full.output, f"{args[1]} diverged between load modes"
