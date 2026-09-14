# ModBox21 (modkit21)

ModBox21 is a Bethesda modding toolkit: a desktop app built on ImGui, a
unified `modkit` CLI, and a native Python/Rust service layer for building,
inspecting, and deploying mods.

The project is Windows-first and centers on Fallout 4 workflows, with
read/reference support for Fallout 76, Skyrim Special Edition, Starfield,
Fallout 3, and Fallout: New Vegas.

Cross-game conversion (porting records and assets between Creation Engine
games; Fallout 76 -> Fallout 4 is the most complete pair) lives in the sibling
[bacup](https://github.com/Bryant-21/bacup) app. Both apps build on the shared
record/NIF/archive/material engine in
[py-creation-lib](https://github.com/Bryant-21/py-creation-lib), included here
as a submodule.

## Contents

- [What It Does](#what-it-does)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Daily Mod Workflow](#daily-mod-workflow)
- [Desktop Workspaces](#desktop-workspaces) — all 42 toolkit workspaces
- [modkit CLI](#modkit-cli) — all 14 command groups, 146 subcommands
- [Project Layout](#project-layout)
- [Building](#building)
- [Troubleshooting](#troubleshooting)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## What It Does

- **Unified CLI**: `modkit` is the primary interface for mod lifecycle
  commands, game data search, ESP/ESM/ESL authoring, NIF editing, archive
  work, SWF/Pipboy icon tools, Havok cloth, Creation Kit automation, and
  index building — see [modkit CLI](#modkit-cli) for the full reference.
- **Desktop toolkit**: `app.py` launches the ModBox21 desktop app, a
  single ImGui shell hosting 42 switchable workspaces for mod building,
  NIF/mesh editing, ESP editing, materials/textures, audio tools,
  animation/Havok, worldspace/LOD generation, and asset utilities. See
  [Desktop Workspaces](#desktop-workspaces) for the full list.
- **Native ESP pipeline**: plugin authoring is binary-first and
  native-backed through `creation_lib._native`. Authoring directories under
  `mods/<ModName>/yaml/` build through `modkit esp build-mod`.
- **Game data search**: SQLite/FTS indexes cover records, scripts, Papyrus
  docs, Creation Kit wiki pages, behaviors, NIFs, SWFs, and inspected
  external mod libraries.

## Requirements

- Windows
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for Python
  dependency management
- Python 3.11+ for development; packaged builds use Python 3.12+
- Rust toolchain + MSVC, only if you're changing code inside the
  `py_creation_lib` submodule — `uv sync` builds its native extension via
  `maturin` automatically otherwise
- One or more supported Bethesda game installs
- Fallout 4 Creation Kit, if you're compiling Papyrus or running CK
  automation

Install `uv` on Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Quick Start

```powershell
git clone --recursive https://github.com/Bryant-21/modkit21.git
cd modkit21
copy .env.example .env
uv sync
uv run python app.py             # launch the desktop toolkit
uv run python -m cli.main --help # modkit CLI, dev mode
```

If you cloned without `--recursive`, fetch the submodule with
`git submodule update --init`.

Edit `.env` before building or deploying mods. Key values:

```ini
MOD_PREFIX="B21"
FO4_DIR="C:/Program Files (x86)/Steam/steamapps/common/Fallout 4"
FO4_EXTRACTED_DIR=""   # optional loose-file override for behavior/asset indexing
FO76_EXTRACTED_DIR=""  # optional, for reference-only FO76 data
SKYRIM_DIR=""
STARFIELD_DIR=""
```

The CLI resolves the active game as `--game` flag > mod's `.game` file >
`DEFAULT_GAME` env var > `fo4`, so you can set `DEFAULT_GAME` in `.env` if
you don't want to pass `--game` on every command.

You can also run the first-run setup UI:

```powershell
.\modkit.exe setup --force
```

## Daily Mod Workflow

```powershell
# Create a mod folder under mods/ (yaml/, data/, Scripts/Source/User/)
.\modkit.exe --game fo4 mod create B21_MyMod

# Build the plugin from mods/B21_MyMod/yaml/
.\modkit.exe esp build-mod B21_MyMod

# Compile Papyrus scripts for the mod, if any
.\modkit.exe mod compile B21_MyMod

# Build plugin, compile scripts, pack BA2s, and copy outputs to the game
.\modkit.exe mod deploy B21_MyMod

# Remove deployed files from the game Data folder
.\modkit.exe mod undeploy B21_MyMod
```

Patch plugins live under `mods/<ModName>/patches/<PatchName>/yaml/` and
build with:

```powershell
.\modkit.exe esp build-mod B21_MyMod --patch PatchName
.\modkit.exe esp build-mod B21_MyMod --all
```

## Desktop Workspaces

<!-- Screenshots live under docs/screenshots/<workspace-slug>.png. Each
     subsection below has a commented-out image line ready to activate
     once a screenshot exists. -->

`app.py` launches a single ImGui shell (`ui/toolkit/`) that hosts all 42
workspaces below as switchable tabs in one shared docking layout. One
workspace is active at a time; its panels replace the previous workspace's
in the same left/center/right/bottom docks.

### Core Workspaces

#### Mod Manager

Primary hub for building and packaging a mod end-to-end: browse mods in a
side list and drive the full record/asset/voice/packaging workflow from the
main panel. Settings expose an auto-transcription fallback (Parakeet or
Whisper) for voice takes missing transcripts, and a max archive size for
splitting BA2/BSA packs.

<!-- ![Mod Manager](docs/screenshots/mod-manager.png) -->

#### ESP Editor

Browse and edit Bethesda plugins (`.esp`/`.esm`/`.esl`) in an xEdit-style
nav-tree, record view, and info-tabs layout. Runs built-in error-checking
and conflict scanning, and builds patch plugins by copying winning
overrides or auto-merging conflicting records from a scanned load order.

<!-- ![ESP Editor](docs/screenshots/esp-editor.png) -->

#### NIF Editor

Full 3D editor for NIF meshes: scene-tree, properties, texture-set,
skeleton-tools, validation, batch-operations, and animation/particle panels
around a viewport with wireframe/UV-checker/normals display modes, standard
camera views, lighting, grid, collision display, connect points, and
animation playback.

<!-- ![NIF Editor](docs/screenshots/nif-editor.png) -->

#### Omni Search

Searches game records, Papyrus scripts, Creation Kit wiki content, and
inspected external-mod data from one search panel, showing full matched
entries in a content panel. Per-index toggles (records, NIFs, behaviors)
live in toolkit settings.

<!-- ![Omni Search](docs/screenshots/omni-search.png) -->

#### Papyrus

Papyrus script IDE: a file tree over per-game script source roots, a tabbed
editor with find/replace and adjustable font scale/line spacing, and an
LSP-backed diagnostics panel. Opens `.psc` source directly, or decompiles a
`.pex` into a temporary read-only buffer.

<!-- ![Papyrus](docs/screenshots/papyrus.png) -->

### Texture Workspaces

#### Palette

Generates FO4 remap and gradient palette textures used for color-swap
material variants, with automatic and manual generation tabs and a debug
preview.

<!-- ![Palette](docs/screenshots/palette.png) -->

#### Materials

BGSM/BGEM material property editor — create, open, and edit FO4 material
files with undo/redo and save/save-as.

<!-- ![Materials](docs/screenshots/materials.png) -->

#### Material Copier

Bulk-copies BGSM/BGEM material files across a folder tree.

<!-- ![Material Copier](docs/screenshots/material-copier.png) -->

#### DDS Inspector

Views a DDS texture's file properties: format, dimensions, mip count, and
more.

<!-- ![DDS Inspector](docs/screenshots/dds-inspector.png) -->

#### DDS to PNG

Batch-converts DDS textures to PNG.

<!-- ![DDS to PNG](docs/screenshots/dds-to-png.png) -->

#### DDS Resizer

Batch-resizes DDS textures.

<!-- ![DDS Resizer](docs/screenshots/dds-resizer.png) -->

#### Color Report

Analyzes texture color usage across an image or folder and reports it.

<!-- ![Color Report](docs/screenshots/color-report.png) -->

#### Image Utils

Converts images to SVG or ICO format.

<!-- ![Image Utils](docs/screenshots/image-utils.png) -->

#### Image Upscaler

AI-upscales images via an external chaiNNer model pipeline.

<!-- ![Image Upscaler](docs/screenshots/image-upscaler.png) -->

#### Image Quantizer

Reduces an image to a fixed N-color palette — useful for paint-swap and
remap-friendly textures.

<!-- ![Image Quantizer](docs/screenshots/image-quantizer.png) -->

### Audio Workspaces

#### Recorder

Records and processes voice takes through a filter/effects chain: manages
input/output devices, presets, a filter builder, bulk batch processing of
WAV folders, and a recordings browser.

<!-- ![Recorder](docs/screenshots/voice-recorder.png) -->

#### Voice Browser

Browses a game's cached voice-line reference index: filter by plugin,
voice type, or text; preview audio inline (auto-converting FUZ/XWM/OGG/WEM
to WAV); export single lines or whole voice-type groups as FUZ, WAV, or
WAV+LIP.

<!-- ![Voice Browser](docs/screenshots/voice-browser.png) -->

#### Audio Extractor

Batch-extracts playable WAV audio from Bethesda FUZ/XWM voice files.

<!-- ![Audio Extractor](docs/screenshots/audio-extractor.png) -->

#### Gun Fire Generator

Turns one single-shot WAV into a full auto-fire loop: writes one output WAV
per requested RPM with per-shot pitch/gain/tone variation, tail trimming,
jitter, and loop/cue markers for multi-shot bursts.

<!-- ![Gun Fire Generator](docs/screenshots/gun-fire-generator.png) -->

#### Laser Beam Generator

Generates looping energy-weapon beam sound effects.

<!-- ![Laser Beam Generator](docs/screenshots/laser-beam-generator.png) -->

### Mesh Workspaces

#### Weights

Paints and auto-transfers skin weights onto an imported mesh from a loaded
reference body, with weight-heatmap/segment/shaded/vertex-color viewport
modes, mirroring, and NIF export.

<!-- ![Weights](docs/screenshots/weight-painter.png) -->

#### Cloth

Views and authors Havok cloth physics (particles, constraints, capsules,
pin markers) on an imported NIF, with viewer/parameters/preview/authoring
panels and NIF export.

<!-- ![Cloth](docs/screenshots/cloth-maker.png) -->

#### SWF Editor

Vector-shape editor for Scaleform SWF UI assets: pen/rect/ellipse/line/fill
/eyedropper tools, layers, a timeline, and SWF export.

<!-- ![SWF Editor](docs/screenshots/swf-editor.png) -->

### Animation Workspaces

#### Bulk Editor

Applies bone/pose edits to a reference body and pushes them across many
meshes in bulk, with pose/animation playback (play/loop/stop) and
undo/redo.

<!-- ![Bulk Editor](docs/screenshots/bone-editor.png) -->

#### Scope Aligner

Aligns scope/optic attachment offsets against a weapon mesh and animation
in a 3D viewport, with preset-body loading and offset/output panels.

<!-- ![Scope Aligner](docs/screenshots/scope-aligner.png) -->

### Havok Workspaces

#### Behavior Graph

Node-based editor for Havok behavior graphs (`.hkx`): drag nodes from a
palette onto a canvas, wire connections, edit node properties, and
import/export HKX or XML.

<!-- ![Behavior Graph](docs/screenshots/behavior-graph.png) -->

#### Annotation Extractor

Extracts annotation event lists from converted HKX-to-XML animation data.

<!-- ![Annotation Extractor](docs/screenshots/annotation-extractor.png) -->

#### HKX Viewer

Unpacks a Havok HKX file to XML and displays the converted text for
inspection, with open/save-as for HKX or XML.

<!-- ![HKX Viewer](docs/screenshots/hkx-viewer.png) -->

#### HKX Packer

Bulk packs XML into HKX or unpacks HKX to XML across a folder.

<!-- ![HKX Packer](docs/screenshots/hkx-packer.png) -->

#### HKX Converter

Converts HKX behavior/animation files between Havok versions, e.g. across
game generations.

<!-- ![HKX Converter](docs/screenshots/hkx-converter.png) -->

### NIF Tool Workspaces

#### NIF Collision Generator

Bulk-generates per-part collision (capsule, cylinder, sphere, box, convex
hull, mesh, and more, FO4 layer-tagged) for NIF meshes.

<!-- ![NIF Collision Generator](docs/screenshots/nif-collision-generator.png) -->

#### NIF to FBX

Batch-converts NIF meshes to FBX for external 3D tools.

<!-- ![NIF to FBX](docs/screenshots/nif-to-fbx.png) -->

#### Worldspace Export

Loads a plugin's worldspace/cell placements and exports the placed static
geometry as a single composed FBX scene, with per-cell or
whole-worldspace selection and origin normalization.

<!-- ![Worldspace Export](docs/screenshots/worldspace-export.png) -->

#### World Viewer

3D preview of a loaded worldspace's placed object instances, with layer
visibility, selection, render-setting, and live stats (visible-instance
count, culling time) panels.

<!-- ![World Viewer](docs/screenshots/world-viewer.png) -->

#### LOD Generator

Generates terrain, object, and tree LOD (level-of-detail) meshes and
textures for a worldspace, with per-category panels and preset-driven
generation.

<!-- ![LOD Generator](docs/screenshots/lod-generator.png) -->

### Mod Tool Workspaces

#### BSA Viewer

Opens BSA/BA2 archives, browses and searches file listings, previews audio
and DDS textures inline, and extracts single files or whole archives.

<!-- ![BSA Viewer](docs/screenshots/bsa-viewer.png) -->

#### SubGraph Maker

Generates Havok subgraph overlay text files for animation mods
(Human/Power Armor/Super Mutant race templates) by find/replacing target
animation events and folders in a bundled template.

<!-- ![SubGraph Maker](docs/screenshots/subgraph-maker.png) -->

#### BSA Extractor

Extracts and packs Bethesda archives (BSA/BA2) across many game formats —
FO4, FO4 old-gen, FO76, Skyrim SE/LE, Starfield, Oblivion, FO3, and FNV —
plus DDS-only variants.

<!-- ![BSA Extractor](docs/screenshots/bsa-extractor.png) -->

#### Mass BSA

Batch-packs loose asset folders across many Mod Organizer 2 mods into
native BA2/BSA archives in one pass, then removes the packed loose
folders.

<!-- ![Mass BSA](docs/screenshots/mass-bsa.png) -->

#### Archlist Creator

Generates an `.archlist` manifest by scanning a folder tree and writing
Data-relative file entries.

<!-- ![Archlist Creator](docs/screenshots/archlist-creator.png) -->

#### Folder Renamer

Duplicates a folder tree while applying string find/replace across folder
names, file names, and the contents of recognized text-based files
(scripts, configs, markup, etc.).

<!-- ![Folder Renamer](docs/screenshots/folder-renamer.png) -->

#### Modlist Merger

Merges two Mod Organizer 2 profiles' `modlist.txt`/`loadorder.txt`/
`archives.txt` into one combined profile.

<!-- ![Modlist Merger](docs/screenshots/modlist-merger.png) -->

## modkit CLI

Global options:

```powershell
.\modkit.exe --game fo4 --format table --db-dir data <command>
```

Command groups (146 subcommands total; `setup` is a standalone command,
not a group):

| Group | Subcommands | Purpose |
| --- | --- | --- |
| `data` | 18 | Search records, scripts, wiki pages, behaviors, NIFs, SWFs, and external mod libraries |
| `esp` | 44 | Inspect, export, import, validate, and build ESP/ESM/ESL files and authoring dirs |
| `mod` | 7 | Create, import, compile, deploy, and undeploy mods |
| `nif` | 20 | Inspect/edit NIF files with file-backed sessions, block edits, skinning, collision, and validation |
| `archive` | 3 | Browse and extract BSA/BA2 archives |
| `build` | 7 | Extract game data, pack archives, validate mods, and pack/unpack HKX files |
| `cloth` | 15 | Inspect, extract, import, bake, validate, and solve FO4 Havok cloth |
| `ck` | 3 | Run Creation Kit automation for previs, dialogue export, and AnimTextData |
| `index` | 5 | Build search indexes and scaffold or add indexed game/reference libraries |
| `swf` | 10 | Inspect, extract, index, and pack SWF/Scaleform assets |
| `texture` | 5 | Recolor and manipulate textures |
| `world` | 3 | Inspect and render static worldspaces |
| `git` | 5 | Per-mod git helpers for local mod repositories |
| `setup` | — | Open first-run setup and write settings plus `.env` |

### Command Reference

Every subcommand, grouped the same way. Click a group to expand it.

<details>
<summary><code>data</code> — 18 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `data search` | Full-text search across game data |
| `data semantic` | AI semantic search (natural language queries) |
| `data get` | Get full content by ID |
| `data list` | List or count items in a domain |
| `data record` | Get a game record by FormKey |
| `data refs` | Find all records referencing a FormKey |
| `data lookup` | Look up records by EditorID (case-insensitive) |
| `data keyword` | Find records with a specific keyword |
| `data keywords` | Get all keywords for a record, resolved to EditorIDs |
| `data count-refs` | Count references to a FormKey (fast) |
| `data function` | Look up a Papyrus function by name |
| `data functions` | List all functions for a Papyrus script type |
| `data api` | Get the full API page for a Papyrus script type |
| `data hierarchy` | Walk the extends chain for a script type |
| `data behavior` | Get the raw XML content of a behavior file |
| `data batch` | Execute multiple queries from a JSON batch |
| `data trace` | Print provenance ancestry for an asset or record in a converted mod |
| `data audit-yaml` | Audit a mod's YAML against the target-game field whitelist |

</details>

<details>
<summary><code>esp</code> — 44 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `esp inspect` | Inspect a plugin and print a compact structural summary |
| `esp list-records` | List a plugin's records as EditorID + local FormID pairs |
| `esp collect-assets` | Collect asset paths referenced by records in a plugin |
| `esp search` | Search records by EditorID with glob/substring/regex matching |
| `esp get-record` | Dump a single record as a JSON object |
| `esp get-records` | Dump multiple records while opening the plugin only once |
| `esp set-record` | Insert or replace one or many records from authoring-schema JSON |
| `esp delete-record` | Delete a record by EditorID or local hex FormID |
| `esp copy-record` | Copy a record (or all `--match` matches) between plugins |
| `esp copy` | Copy a single record into a target plugin (`--mode new|override`) |
| `esp merge` | Bulk-copy a source plugin's records into a target plugin |
| `esp diff` | Record-level diff of two plugins (read-only; `--detail`/`--type`) |
| `esp masters list` | List a plugin's masters with size and whether each is referenced |
| `esp masters add` | Add one or more masters to a plugin (idempotent) |
| `esp masters remove` | Remove one or more masters from a plugin |
| `esp masters reorder` | Reorder a plugin's masters to a given permutation |
| `esp header` | Show a plugin's header, or edit it when a field/flag option is given |
| `esp compact-esl` | Renumber a plugin's owned records into the ESL object-id window |
| `esp clean` | Remove ITM records and undelete-and-disable deleted references (UDR) |
| `esp count` | Report record counts: total, per-signature breakdown, optional `--match` count |
| `esp set-field` | Set one field to the same value across every record matching `--match` |
| `esp remove-formid-subrecord` | Remove an exact FormID subrecord payload from matching records |
| `esp repair-term-marker-parameters` | Restore FO4 TERM marker rows from FO76 source rows |
| `esp delete-matching` | Delete every record matching `--match` |
| `esp delete-placed-by-base` | Delete placed refs whose base resolves to a requested base type |
| `esp strip-record-subrecords` | Remove raw subrecords from specific records |
| `esp retain-race-subgraphs` | Keep only RACE subgraph blocks containing a self-owned selector |
| `esp disable-quest-autostart` | Clear the Start Game Enabled bit on every quest |
| `esp strip-distant-lod` | Strip FO4 object-LOD "Distant LOD" data from a plugin |
| `esp strip-empty-refr-xrgd` | Remove empty FO76 bone-rows from placed references |
| `esp strip-cell-achr-pose` | Remove pose subrecords from a cell's placed actors |
| `esp delete-cell-children` | Delete the placed children of a single cell (interior or exterior) |
| `esp rename` | Rename the EditorIDs of records matching `--match` |
| `esp export` | Export a plugin to lossless or semantic JSON/YAML |
| `esp export-authoring` | Export a plugin to the native directory-based authoring format |
| `esp import` | Import a JSON/YAML export and save it back to plugin binary |
| `esp build-authoring` | Build a plugin `.esp` from a YAML/JSON authoring directory |
| `esp build` | Build plugin bytes from native JSON/YAML authoring data |
| `esp new` | Create a new empty plugin (type inferred from the output path) |
| `esp check-errors` | Check an ESP/ESM/ESL for xEdit-style validation errors |
| `esp check-runtime-hazards` | Check an ESP/ESM/ESL for known FO4 loader-crash hazards |
| `esp build-mod` | Build a mod's `.esp` from its `yaml/` authoring directory (validates first) |
| `esp inspect-mod` | Serialize a plugin to a mod-shaped `yaml/` directory under `mods/<name>/` |
| `esp import-mod` | Re-serialize a deployed plugin back into a mod's `yaml/` source dir (CK round-trip) |

</details>

<details>
<summary><code>mod</code> — 7 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `mod create` | Create a new mod with full directory structure |
| `mod import` | Import an external mod into the project |
| `mod deploy` | Deploy a mod: build `.esp`, pack BA2, copy to game Data |
| `mod compile` | Compile Papyrus scripts (`.psc` -> `.pex`) for a mod |
| `mod inspect` | Inspect a mod: extract BA2s, decompile scripts, serialize `.esp` to YAML, catalog assets |
| `mod import-loose` | Pull CK changes back from a loose deployment into the mod folder |
| `mod undeploy` | Remove a deployed mod from the game Data folder |

</details>

<details>
<summary><code>nif</code> — 20 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `nif open` | Open a NIF file for editing (returns a session ID) |
| `nif new` | Create a new empty NIF file (returns a session ID) |
| `nif save` | Save a NIF session to disk |
| `nif close` | Close a NIF session and free resources |
| `nif inspect` | Inspect a NIF file or specific block |
| `nif modify` | Update fields on an existing block |
| `nif copy` | Copy blocks (with dependency trees) between NIFs |
| `nif add` | Create a new block |
| `nif remove` | Remove blocks from a NIF (updates all reference indices) |
| `nif collision` | Generate a collision hierarchy on a node |
| `nif rm-collision` | Remove a collision subtree from a node |
| `nif strip-all-collision` | Remove every collision subtree from a NIF file |
| `nif strip-all-collision-batch` | Remove all collision from many NIFs listed in a JSON manifest |
| `nif port-cell-nifs` | Convert a cell's placed base NIFs from source to target game and deploy them loose |
| `nif auto-skin` | Auto-skin a mesh using reference body weights |
| `nif transfer` | Transfer bone weights between shapes |
| `nif partitions` | Generate dismemberment partition assignments |
| `nif validate` | Validate NIF structure, references, materials, and optional shape weights |
| `nif normalize` | Normalize bone weights (enforce max bones, sum to 1.0) |
| `nif batch` | Execute multiple NIF commands in one call |

</details>

<details>
<summary><code>archive</code> — 3 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `archive list` | Return archive metadata and file listing |
| `archive extract` | Extract a single file from an archive |
| `archive extract-all` | Extract an entire archive |

</details>

<details>
<summary><code>build</code> — 7 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `build extract` | Extract BSA/BA2 archives from a game's Data folder |
| `build pack` | Pack BA2/BSA archives for a mod |
| `build validate` | Validate FormKey references in a mod's YAML files |
| `build strip-master` | Strip a master reference from an ESP/ESM header (experimental) |
| `build pack-hkx` | Pack an XML behavior file to binary HKX |
| `build unpack-hkx` | Unpack a binary HKX file to XML |
| `build gen-classxml` | Generate per-version Havok classxml directories from FO4 descriptors + SDK patches |

</details>

<details>
<summary><code>cloth</code> — 15 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `cloth inspect` | Print a summary of the cloth data in a NIF |
| `cloth extract` | Extract the raw HCL packfile bytes from a NIF to a `.hkx` file |
| `cloth pack` | Embed an HKX packfile into a NIF's cloth data block |
| `cloth solve` | Run headless XPBD cloth simulation and report performance |
| `cloth bake` | Bake a setup JSON into a NIF's cloth data |
| `cloth import` | Import cloth data from a NIF as an editable setup JSON |
| `cloth dump` | Dump the full HCL object graph as JSON |
| `cloth validate` | Validate a cloth graph for common issues |
| `cloth tweak` | Tweak cloth parameters in a NIF without re-baking |
| `cloth template list` | List all available cloth templates |
| `cloth template show` | Show detailed information about a cloth template |
| `cloth template apply` | Apply a cloth template to a NIF mesh |
| `cloth region topologies` | List all available topology presets |
| `cloth region show` | Show detailed info about a topology preset |
| `cloth region generate` | Generate cloth from a region setup JSON |

</details>

<details>
<summary><code>ck</code> — 3 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `ck previs` | Generate PreCombines and PreVis data via Creation Kit |
| `ck dialogue` | Export dialogue lines via Creation Kit |
| `ck animdata` | Generate AnimTextData via Creation Kit (deploys the mod, then runs CK) |

</details>

<details>
<summary><code>index</code> — 5 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `index build` | Build search indexes for a game (all domains, or one `--domain`) |
| `index regen-yaml` | Re-export a game's master plugins into authoring YAML |
| `index add-library` | Migrate an inspected mod to the external reference library |
| `index add-game` | Scaffold directories and skill stubs for a new game |
| `index download-geck-wiki` | Download the GECK Wiki for Fallout 3 / New Vegas reference |

</details>

<details>
<summary><code>swf</code> — 10 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `swf inspect` | Print a summary of SWF structure (version, canvas, FPS, frames, shapes, tags) |
| `swf extract` | Export all shapes from a SWF as individual SVG files |
| `swf pack` | Assemble a SWF from a `.swfproj` project file |
| `swf index` | Build a shape library from extracted FO4 SWF files |
| `swf symbols list` | List every SymbolClass export (character ID, name) in file order |
| `swf symbols inject` | Splice named symbols, with their full character closures, from one SWF into another |
| `swf abc dump` | Dump each DoABC tag's constant-pool string table |
| `swf abc markers` | Report which canonical FO76 marker export names already exist as AS3 classes |
| `swf markers table` | Dump the canonical FO76-to-FO4 marker icon table |
| `swf markers build` | Inject all FO76 region marker icons into each FO4 menu SWF (deterministic) |

</details>

<details>
<summary><code>texture</code> — 5 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `texture recolor hue-shift` | Rotate all pixel hues by a given number of degrees |
| `texture recolor tint` | Multiply RGB by a color (best for gray/white textures) |
| `texture recolor colorize` | Force a hue and saturation while preserving luminance |
| `texture recolor gradient` | Generate a gradient palette strip |
| `texture recolor analyze` | Analyze texture colors without writing an output file |

</details>

<details>
<summary><code>world</code> — 3 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `world list-worldspaces` | List worldspaces available across given plugins/data paths/archives |
| `world inspect` | Load a worldspace's cell-bounds range and print scene stats |
| `world render` | Render a worldspace cell-bounds range to an offscreen image, optionally writing a JSON report |

</details>

<details>
<summary><code>git</code> — 5 subcommands</summary>

| Command | Purpose |
| --- | --- |
| `git commit` | Commit the given paths (or `--all`) and push unless `--no-push` |
| `git push` | Push to remote |
| `git pull` | Pull the latest changes from origin |
| `git checkout` | Discard all local changes (hard reset + clean) |
| `git init` | Initialize a git repo and push to Gitea |

</details>

Examples:

```powershell
.\modkit.exe data search records "combat shotgun" -n 5
.\modkit.exe --format table data lookup WorkbenchChemistry
.\modkit.exe data function AddItem --script-type Actor

.\modkit.exe nif inspect --path meshes/weapons/example.nif
.\modkit.exe nif open meshes/weapons/example.nif
.\modkit.exe nif save <session_id>

.\modkit.exe esp inspect-mod SomeMod.esp --game fo4
.\modkit.exe --game fo4 esp list-records SomeMod.esp --type WEAP
.\modkit.exe --game fo4 esp get-record SomeMod.esp crGraftonOilBombWeapon
.\modkit.exe --game fo4 esp export-authoring Fallout4.esm --output-dir out\fo4-master --format yaml
.\modkit.exe build validate B21_MyMod
.\modkit.exe index build --domain records --game fo4
.\modkit.exe swf inspect path\to\file.swf
```

For Git Bash, the repository also includes `modkit.sh`, which dispatches to
a built `modkit.exe` next to it, or falls back to
`uv run python -m cli.main` in dev mode.

## Project Layout

```text
modkit21/
|-- app.py                   # Desktop app entry point
|-- app/                     # App path/env boundary helpers
|-- cli/                     # Click-based modkit command groups
|-- py_creation_lib/         # Submodule: Python service layer + Rust native crates
|-- ui/                      # Desktop toolkit workspaces (NIF, ESP, cloth, materials, search, ...)
|-- resource/                # Bundled executables, icons, and fixtures
|-- modkit.sh                # Git Bash launcher / dev fallback for modkit
|-- modkit.spec              # PyInstaller spec: modkit CLI
|-- ModBox21.spec             # PyInstaller spec: desktop app (variant-selectable by env var)
|-- build_modkit_cli.bat     # Build and promote modkit.exe
|-- build_nif_ui.ps1         # Build the standalone NIF-editor release variant
|-- .env.example             # Copy to .env and fill in your game paths
|-- VERSION
`-- pyproject.toml
```

`mods/` is created at runtime by `modkit mod create` and isn't part of the
repo. Generated folders such as `.venv/`, `build/`, `dist/`, `target/`,
`logs/`, and `.pytest_cache/` are normal development artifacts.

## Building

Build only the CLI:

```powershell
.\build_modkit_cli.bat
```

This builds `dist/modkit/`, promotes `modkit.exe` and `_internal/` to the
repo root, and copies the CLI bundle to `%USERPROFILE%\.local\bin`.

For the desktop app, run it from source (`uv run python app.py`) or build a
release exe. `ModBox21.spec` supports multiple named variants (full
toolkit, NIF-only, and others) selected via the `MODBOX21_EXE_NAME` /
`MODBOX21_DIST_NAME` / `MODBOX21_ICON` environment variables; this repo
ships a ready-made script for the NIF-only variant:

```powershell
powershell -ExecutionPolicy Bypass -File build_nif_ui.ps1
```

`.github/workflows/build.yml` builds and zips the `modkit` CLI, the full
`ModBox21` desktop toolkit, and the `ModBox21-NIF` variant on tagged
releases.

## Troubleshooting

| Problem | Check |
| --- | --- |
| `modkit` cannot find game data | Confirm `.env`, `DEFAULT_GAME`, and the relevant `<GAME>_DIR` or extracted dir |
| CLI output is empty or stale | Rebuild the relevant index with `modkit index build` |
| Native behavior changed but Python still sees old code | Rerun `uv sync`; for faster incremental Rust rebuilds see the py-creation-lib README |
| Papyrus compile fails | Confirm Fallout 4 Creation Kit and compiler paths in `.env` |
| ESP build cannot resolve a reference | Run `modkit build validate <ModName>` and check YAML FormKeys/masters |
| Packed/deployed files look wrong | Undeploy, rebuild with `modkit esp build-mod`, then redeploy |

## Acknowledgements

- [NifSkope](https://github.com/fo76utils/nifskope) for essential NIF format
  reference work.
- [PyNifly](https://github.com/BadDogSkyrim/PyNifly) for NIF parsing
  reference material and `nif.xml` definitions.
- [xEdit](https://github.com/TES5Edit/TES5Edit) for record inspection and
  offline schema research workflows.
- [Mutagen](https://github.com/Mutagen-Modding/Mutagen) and
  [Spriggit](https://github.com/Mutagen-Modding/Spriggit) for plugin
  tooling references and earlier authoring workflows.
- [fallout.wiki](https://fallout.wiki) and the Creation Kit/Papyrus wiki
  archives for documentation used by the search indexes.
- [hkxpack](https://github.com/Dexesttp/hkxpack) for Havok HKX reference
  work.
- [DirectXTex](https://github.com/microsoft/DirectXTex) for DDS and texture
  tooling references, and for its bundled `texassemble` command-line tool.
- [imgui-bundle](https://github.com/pthom/imgui_bundle) for the desktop UI
  framework used by toolkit workspaces.
- [Pedalboard](https://github.com/spotify/pedalboard) for the audio effects
  engine behind the Recorder workspace's filter chain.
- [vtracer](https://github.com/visioncortex/vtracer) for the
  bitmap-to-vector tracing used by the SWF Editor's raster import.
- [FaceFXWrapper](https://github.com/Nukem9/FaceFXWrapper) for Fallout 4
  LIP (lip-sync) file generation, bundled in the release audio pipeline.
- BmlFuzTools' BmlFuzDecode/BmlFuzEncode utilities, bundled alongside
  FaceFXWrapper for FUZ encode/decode in the release audio pipeline.
- Microsoft's xWMAEncode, part of the legacy DirectX SDK, for XWM audio
  encode/decode.
- [Click](https://github.com/pallets/click), the CLI framework `modkit` is
  built on.
- [PyO3](https://github.com/PyO3/pyo3) and
  [maturin](https://github.com/PyO3/maturin) for the Rust/Python bindings
  and native-extension build tooling behind the shared library.
- [PyInstaller](https://github.com/pyinstaller/pyinstaller) for packaging
  `modkit.exe` and the desktop app builds.
- [uv](https://github.com/astral-sh/uv) for Python dependency and
  environment management across the project.
- The Fallout 4 and wider Bethesda modding communities for years of shared
  documentation, reverse engineering, and practical examples.

## License

GPL-3.0 — see LICENSE. Family: [py-creation-lib](https://github.com/Bryant-21/py-creation-lib) ·
[bacup](https://github.com/Bryant-21/bacup)
