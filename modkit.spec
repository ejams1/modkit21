# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for modkit CLI — lightweight console tool.

Build with:
  pyinstaller modkit.spec --noconfirm
  or: build_modkit.bat
"""

import glob as _glob
import os
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs
from cli._build_info import write_build_info

_build_info = write_build_info(os.getcwd(), "build/modkit_build_info.json")

block_cipher = None

# Collect numpy and sqlite_vec native libs
numpy_libs = collect_dynamic_libs("numpy")
sqlite_vec_libs = collect_dynamic_libs("sqlite_vec")
# Collect PyO3 native extensions. Native code now ships as creation_lib._native.
creation_lib_native_libs = collect_dynamic_libs("creation_lib")
_creation_lib_native_pyd = _glob.glob(os.path.join(
    ".venv", "Lib", "site-packages", "creation_lib", "_native*.pyd"
))
creation_lib_native_libs += [(p, "creation_lib") for p in _creation_lib_native_pyd]
esp_authoring_core_libs = collect_dynamic_libs("esp_authoring_core")
bsarchive_native_libs = collect_dynamic_libs("bsarchive_native")
directxtex_native_libs = collect_dynamic_libs("directxtex_native")
nif_core_native_libs = collect_dynamic_libs("nif_core_native")

# Collect Autodesk FBX SDK bindings (.pyd + runtime DLL)
_fbx_pyd = _glob.glob(os.path.join(
    os.path.dirname(sys.executable), "..", "Lib", "site-packages", "fbx*.pyd"
))
if not _fbx_pyd:
    # Also check the project venv directly.
    _fbx_pyd = _glob.glob(os.path.join(".venv", "Lib", "site-packages", "fbx*.pyd"))
_fbx_binaries = [(p, ".") for p in _fbx_pyd]

_fbx_dll = r"C:\Program Files\Autodesk\FBX\FBX SDK\2020.3.9\lib\x64\release\libfbxsdk.dll"
if os.path.isfile(_fbx_dll):
    _fbx_binaries.append((_fbx_dll, "."))

a = Analysis(
    ["cli/main.py"],
    pathex=["."],
    binaries=[
        *numpy_libs,
        *sqlite_vec_libs,
        *creation_lib_native_libs,
        *esp_authoring_core_libs,
        *bsarchive_native_libs,
        *directxtex_native_libs,
        *nif_core_native_libs,
        *_fbx_binaries,
    ],
    datas=[
        # NIF schema XML (required by creation_lib.nif)
        ("py_creation_lib/python/creation_lib/nif/nif_xml", "creation_lib/nif/nif_xml"),
        # Runtime helper binaries used via creation_lib.paths.get_resource_dir()
        ("py_creation_lib/python/creation_lib/resources", "creation_lib/resources"),
        # lib source code needed at runtime
        ("py_creation_lib/python/creation_lib", "py_creation_lib/python/creation_lib"),
        # VERSION file
        ("VERSION", "."),
        (_build_info, "."),
    ],
    hiddenimports=[
        "numpy",
        "PIL.Image",
        "sqlite3",
        "sqlite_vec",
        "click",
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        "cli.setup_commands",
        "cli.setup_gui",
        "app.env_sync",
        "ui.toolkit.settings",
        "creation_lib.core.path_detector",
        "cli.archive_commands",
        "xml.etree.ElementTree",
        "creation_lib.nif",
        "creation_lib.nif.nif_file",
        "creation_lib.nif.schema",
        "creation_lib.nif.types",
        "creation_lib.nif.binary_io",
        "creation_lib.nif.operations.collision",
        "cli.mod_commands",
        "cli.spriggit_commands",
        "cli.ck_commands",
        "cli.git_commands",
        "cli.index_commands",
        "creation_lib.creation_data",
        "creation_lib.creation_data.search",
        "creation_lib.creation_data.content",
        "creation_lib.creation_data.records",
        "creation_lib.creation_data.scripts",
        "creation_lib.creation_data.listing",
        "creation_lib.creation_data.batch",
        "creation_lib.creation_data._db_resolver",
        "creation_lib.creation_data._config",
        "creation_lib.db",
        "creation_lib._native",
        "creation_lib.core.game_profiles",
        "creation_lib.skinning",
        "creation_lib.esp.spriggit",
        "creation_lib.build.deployer",
        "creation_lib.mod.scaffold",
        "creation_lib.ck.automation",
        "creation_lib.mod.git_ops",
        "creation_lib.db.index_builder",
        "creation_lib.build.packer",
        "directxtex_native",
        "creation_lib.preprocessor.records",
        "creation_lib.preprocessor.scripts",
        "creation_lib.preprocessor.wiki",
        "creation_lib.preprocessor.nifs",
        "creation_lib.preprocessor.havok",
        "creation_lib.preprocessor.external",
        "creation_lib.preprocessor.swf",
        "creation_lib.preprocessor.extraction",
        "creation_lib.esp.validate",
        "creation_lib.hkxpack",
        "creation_lib.nif.previs_merge",
        "fbx",
        "creation_lib.fbx",
        "creation_lib.fbx.nif_to_fbx",
        # mod inspect / PEX decompiler chain
        "creation_lib.mod.inspector",
        "creation_lib.pex",
        "creation_lib.pex.decompiler",
        "creation_lib.pex.parser",
        "creation_lib.pex.opcodes",
        "creation_lib.pex.types",
        "creation_lib.papyrus_lsp",
        "creation_lib.papyrus_lsp.ast_nodes",
        "creation_lib.papyrus_lsp.parser",
        "creation_lib.papyrus_lsp.emitter",
        "creation_lib.papyrus_lsp.completions",
        "creation_lib.papyrus_lsp.definition",
        "creation_lib.papyrus_lsp.resolver",
        "creation_lib.papyrus_lsp.script_db",
        # lark parser (used by papyrus_lsp.parser)
        "lark",
        "lark.visitors",
        "lark.lexer",
        "lark.parsers",
        "lark.grammar",
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
        "diffusers",
        "accelerate",
        # UI libraries — not needed
        "imgui_bundle",
        "hello_imgui",
        "immapp",
        "moderngl",
        "PyGLM",
        "pygltflib",
        "watchdog",
        "pywinpty",
        "pyte",
        "cv2",
        "imagequant",
        "pedalboard",
        "sounddevice",
        "soundfile",
        # MCP server — not needed in CLI
        "fastmcp",
        "mcp",
        "uvicorn",
        "pygls",
        # Dev/test
        "pytest",
        "mypy",
        "black",
        "ruff",
        "isort",
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="modkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # CLI tool — needs console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resource/cli.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["*_native.pyd"],
    name="modkit",
)
