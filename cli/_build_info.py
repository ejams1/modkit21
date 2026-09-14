from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def source_fingerprint(root):
    root = Path(root)
    paths = [root / "VERSION", root / "modkit.spec"]
    paths.extend((root / "cli").glob("*.py"))
    paths.extend(path for path in (root / "py_creation_lib/python/creation_lib").rglob("*.py") if "tests" not in path.parts)
    digest = hashlib.sha256()
    count = 0
    for path in sorted(paths):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            count += 1
    return {"sha256": digest.hexdigest(), "files": count,
            "scope": "cli/*.py, creation_lib/**/*.py (excluding tests), modkit.spec, VERSION"}


def create_build_info(root):
    root = Path(root)
    revision = None
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            revision = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"built_at": datetime.now(timezone.utc).isoformat(), "git_revision": revision,
            "version": (root / "VERSION").read_text(encoding="utf-8").strip(),
            "source": source_fingerprint(root)}


def write_build_info(root, destination):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(create_build_info(root)), encoding="utf-8")
    return str(path)
