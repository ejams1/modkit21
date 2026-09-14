from __future__ import annotations

from cli.esp_commands import _record_model_paths, _xloc_level


def test_xloc_level_reads_first_byte() -> None:
    assert (
        _xloc_level([("NAME", b"\x01\x02", None), ("XLOC", b"\x4b" + b"\0" * 15, None)])
        == 75
    )


def test_xloc_level_handles_absent_or_empty_payload() -> None:
    assert _xloc_level([("NAME", b"\0" * 4, None)]) is None
    assert _xloc_level([("XLOC", b"", None)]) is None


def test_record_model_paths_decodes_unique_modl_values() -> None:
    assert _record_model_paths(
        [
            ("EDID", b"TestStatic\0", None),
            ("MODL", b"Architecture\\Test\\One.NIF\0", None),
            ("MODL", b"Architecture\\Test\\One.NIF\0", None),
            ("MODL", b"Architecture\\Test\\Two.NIF\0ignored", None),
        ]
    ) == ["Architecture\\Test\\One.NIF", "Architecture\\Test\\Two.NIF"]
