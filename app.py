"""ModBox21 — Bethesda Modding Toolkit."""

# OpenCV reads OPENCV_IO_ENABLE_OPENEXR when cv2 is first imported. The Starfield
# renderer needs EXR for its HDR probe (resource/monochrome_studio_02_1k.exr), so it
# is set here, before any transitive cv2 import.
import os
import sys
from pathlib import Path
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

from ui.toolkit.__main__ import main, run_toolkit_variant
from ui.toolkit.variants import variant_id_from_exe_name

if __name__ == "__main__":
    variant = os.environ.get("MODBOX21_VARIANT", "").strip()
    if not variant and getattr(sys, "frozen", False):
        variant = variant_id_from_exe_name(Path(sys.executable).stem) or ""
    if variant:
        args = list(sys.argv[1:])
        run_toolkit_variant(variant, launch_path=args[0] if args else None)
    else:
        main()
