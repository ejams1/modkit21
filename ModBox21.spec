# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ModBox21 — Bethesda Modding Toolkit.

Build with:
  pyinstaller ModBox21.spec

Or use build_toolkit.ps1 which handles pre-build DBs and packaging.
"""

import os
import sys
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None
project_root = os.path.abspath(globals().get("SPECPATH", os.getcwd()))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

exe_name = os.environ.get("MODBOX21_EXE_NAME", "ModBox21")
dist_name = os.environ.get("MODBOX21_DIST_NAME", exe_name)
icon_path = os.environ.get("MODBOX21_ICON", "resource/icon.ico")
is_nif_build = dist_name.lower().endswith("-nif") or exe_name.lower().endswith("-nif")
onefile = os.environ.get("MODBOX21_ONEFILE", "").lower() not in ("", "0", "false", "no")

# Onefile mode has no folder beside the EXE, so resource/ (tool binaries, icons)
# must be bundled into the archive. Exclude spriggit (large, unused by the
# converter). In onedir mode the build script copies resource/ alongside instead.
excluded_resource_entries = {"spriggit", "steam_api64.dll", "steam_appid.txt"}
onefile_resource_datas = []
if onefile and os.path.isdir("resource"):
    for _entry in os.listdir("resource"):
        if _entry.lower() in excluded_resource_entries:
            continue
        _src = os.path.join("resource", _entry)
        _dest = f"resource/{_entry}" if os.path.isdir(_src) else "resource"
        onefile_resource_datas.append((_src, _dest))

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

# Collect imgui_bundle data and dynamic libs (native .pyd files)
imgui_datas = collect_data_files("imgui_bundle")
imgui_libs = collect_dynamic_libs("imgui_bundle")
imgui_mods = collect_submodules("imgui_bundle")

# Collect creation_lib runtime modules that are imported lazily by workspace code.
creation_lib_mods = collect_submodules(
    "creation_lib",
    filter=lambda name: ".tests" not in name
    and not name.rsplit(".", 1)[-1].startswith("test"),
)

# Collect every ui submodule so PyInstaller analyzes their imports
# (workspaces import lazily via importlib.import_module — without this,
# transitive deps like watchdog and sounddevice are missed).
ui_mods = collect_submodules(
    "ui",
    filter=lambda name: ".tests" not in name
    and not name.rsplit(".", 1)[-1].startswith("test"),
)
ui_datas = collect_data_files(
    "ui", include_py_files=True,
    excludes=["**/__pycache__/**", "**/tests/**", "**/*.pyc", "**/*.pyo"],
)

# File watchers are imported by lazily loaded UI workspaces.
watchdog_mods = collect_submodules("watchdog")

# Collect numpy dynamic libs
numpy_libs = collect_dynamic_libs("numpy")

# Collect PIL data
pil_datas = collect_data_files("PIL")

# Collect sqlite_vec extension
sqlite_vec_libs = collect_dynamic_libs("sqlite_vec")

# Collect winpty (pywinpty) native DLLs for the AI terminal PTY backend
winpty_libs = collect_dynamic_libs("winpty")
winpty_datas = collect_data_files("winpty")

# Collect Autodesk FBX SDK bindings (.pyd + runtime DLL)
import glob as _glob
_fbx_pyd = _glob.glob(os.path.join(
    os.path.dirname(sys.executable), "..", "Lib", "site-packages", "fbx*.pyd"
))
if not _fbx_pyd:
    # Also check venv site-packages
    _fbx_pyd = _glob.glob(os.path.join(".venv", "Lib", "site-packages", "fbx*.pyd"))
_fbx_binaries = [(p, ".") for p in _fbx_pyd]

# FBX SDK runtime DLL
_fbx_dll = r"C:\Program Files\Autodesk\FBX\FBX SDK\2020.3.9\lib\x64\release\libfbxsdk.dll"
if os.path.isfile(_fbx_dll):
    _fbx_binaries.append((_fbx_dll, "."))

if is_nif_build:
    creation_resource_datas = [
        ("py_creation_lib/python/creation_lib/resources/xWMAEncode.exe", "creation_lib/resources"),
        ("py_creation_lib/python/creation_lib/resources/BmlFuzEncode.exe", "creation_lib/resources"),
        ("py_creation_lib/python/creation_lib/resources/xtexconv.exe", "creation_lib/resources"),
        ("py_creation_lib/python/creation_lib/resources/classxml", "creation_lib/resources/classxml"),
        ("py_creation_lib/python/creation_lib/resources/classxml_2012", "creation_lib/resources/classxml_2012"),
        ("py_creation_lib/python/creation_lib/resources/classxml_2015", "creation_lib/resources/classxml_2015"),
    ]
else:
    creation_resource_datas = [
        ("py_creation_lib/python/creation_lib/resources", "creation_lib/resources"),
    ]

nif_excludes = [
    "trimesh",
] if is_nif_build else []

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[
        *imgui_libs,
        *numpy_libs,
        *sqlite_vec_libs,
        *winpty_libs,
        *_fbx_binaries,
    ],
    datas=[
        *imgui_datas,
        *pil_datas,
        *winpty_datas,
        # App code and data
        ("py_creation_lib/python/creation_lib/ba2", "py_creation_lib/python/creation_lib/ba2"),
        ("py_creation_lib/python/creation_lib/db", "py_creation_lib/python/creation_lib/db"),
        ("py_creation_lib/python/creation_lib/fbx", "py_creation_lib/python/creation_lib/fbx"),
        ("py_creation_lib/python/creation_lib/nif", "py_creation_lib/python/creation_lib/nif"),
        ("py_creation_lib/python/creation_lib/nif/nif_xml", "creation_lib/nif/nif_xml"),
        *creation_resource_datas,
        ("py_creation_lib/python/creation_lib/renderer/shaders", "creation_lib/renderer/shaders"),
        ("py_creation_lib/python/creation_lib/renderer/assets", "creation_lib/renderer/assets"),
        *ui_datas,
        *([ ("configs", "configs") ] if os.path.isdir("configs") else []),
        ("VERSION", "."),
        *onefile_resource_datas,
    ],
    hiddenimports=[
        *imgui_mods,
        *creation_lib_mods,
        *ui_mods,
        *watchdog_mods,
        "imgui_bundle",
        "imgui_bundle.imgui",
        "imgui_bundle.hello_imgui",
        "imgui_bundle.immapp",
        "imgui_bundle.imgui_md",
        "numpy",
        "PIL",
        "PIL.Image",
        "sqlite3",
        "sqlite_vec",
        "winreg",
        "fbx",
        "creation_lib.fbx",
        "creation_lib.fbx.nif_to_fbx",
        "ctypes",
        "multiprocessing",
        "xml.etree.ElementTree",
        # AI terminal PTY backend
        "pyte",
        "pyte.screens",
        "pyte.streams",
        "pyte.modes",
        "winpty",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large ML libraries — not needed (FTS5 keyword search only)
        "sentence_transformers",
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "tokenizers",
        "safetensors",
        "huggingface_hub",
        # Hunyuan3D local inference — local model feature, not bundled in release
        "hy3dgen",
        "hy3dshape",
        "diffusers",
        "accelerate",
        "torchvision",
        # MCP server frameworks — not needed in standalone
        "fastmcp",
        "pygls",
        "lark",
        "mcp",
        # Dev/test tools
        "pytest",
        "mypy",
        "black",
        "ruff",
        "isort",
        # Other unused
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib",
        "pandas",
        "cv2",
        *nif_excludes,
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# PYZ (compressed Python modules)
# ---------------------------------------------------------------------------

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------

# UPX is DISABLED: UPX 5.x corrupts several bundled native DLLs (python312.dll,
# glfw3.dll, numpy OpenBLAS), which crashes the frozen app at startup with a
# native access violation in ntdll (heap corruption, no Python traceback).
_UPX = False

if onefile:
    # Single self-contained EXE (binaries + datas folded into the archive).
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name=exe_name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=_UPX,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_path,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=exe_name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=_UPX,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_path,
    )

    # COLLECT (onedir mode) — skipped entirely for onefile builds.
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=_UPX,
        upx_exclude=[],
        name=dist_name,
    )
