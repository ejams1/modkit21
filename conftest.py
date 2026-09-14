"""Repo-root pytest configuration — suite-wide safety guards.

A native file/folder dialog opening during a test is always a bug: on a headless
CI runner it never closes, so the run blocks in ``pick_folder._wait`` until the
job times out. The autouse fixture replaces the pfd-backed helpers in
``creation_lib.ui.widgets.pick_folder`` with functions that raise.

The patch targets the submodule from ``import_module``: the ``widgets`` package
re-exports the helpers as names, so attribute lookup on the dotted path returns
the function. Callers import the helpers at call time, so they see the patch.
Tests that patch these helpers themselves layer on top and are restored after.
"""
import importlib

import pytest


@pytest.fixture(autouse=True)
def _block_native_file_dialogs(monkeypatch):
    try:
        pick_folder_mod = importlib.import_module("creation_lib.ui.widgets.pick_folder")
    except Exception:
        yield
        return

    def _blocked(*_args, **_kwargs):
        raise RuntimeError("native dialog opened during tests")

    for name in ("pick_folder", "pick_file", "pick_save_file"):
        monkeypatch.setattr(pick_folder_mod, name, _blocked)
    yield
