"""One-click update from a TradBot ZIP (the GitHub "Download ZIP" file).

Safe by design:
  * only TradBot's own code files are replaced - never data/, .env or .venv
  * your config.yaml is kept as config.yaml.bak if the new version changes it
  * the current code is saved to data/backups/code-<version>.zip first (easy undo)
  * TradBot.bat can't be replaced while it is running, so the new one is written as
    TradBot.bat.new and swapped in by TradBot.bat itself during the restart
"""
from __future__ import annotations

import io
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import DATA_DIR, ROOT
from .log import get_logger

log = get_logger("updater")

MAX_ZIP = 30 * 1024 * 1024
TOP_FILES = {"main.py", "requirements.txt", "requirements-optional.txt", "requirements-dev.txt",
             "config.yaml", "README.md", "CHANGELOG.md", "Dockerfile", ".dockerignore",
             ".gitignore", ".gitattributes", "TradBot.bat"}
CODE_DIRS = {"tradbot", "docs", "tests"}
NEVER = {"data", ".env", ".venv", "venv", ".git"}


def _safe(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return not (name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0])


def inspect(zip_bytes: bytes) -> dict:
    """Finds the TradBot folder inside the zip and reads its version."""
    if len(zip_bytes) > MAX_ZIP:
        raise ValueError("file is too big to be a TradBot update")
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("this is not a zip file")
    names = [n for n in z.namelist() if _safe(n)]
    roots = [n[: -len("tradbot/__init__.py")] for n in names if n.endswith("tradbot/__init__.py")]
    root = next((r for r in roots if r + "main.py" in names), None)
    if root is None:
        raise ValueError("this zip is not TradBot (main.py / tradbot folder not found)")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', z.read(root + "tradbot/__init__.py").decode())
    return {"zip": z, "root": root, "version": m.group(1) if m else "?",
            "files": [n for n in names if n.startswith(root) and not n.endswith("/")]}


def _backup_current_code(root: Path):
    out = root / "data" / "backups"
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"code-{__version__}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in TOP_FILES:
            if (root / name).is_file():
                z.write(root / name, name)
        for d in CODE_DIRS:
            for p in (root / d).rglob("*") if (root / d).is_dir() else []:
                if p.is_file() and "__pycache__" not in p.parts:
                    z.write(p, str(p.relative_to(root)).replace("\\", "/"))
    return target


def apply(zip_bytes: bytes, root: Path = ROOT) -> dict:
    info = inspect(zip_bytes)
    z, prefix = info["zip"], info["root"]
    _backup_current_code(root)
    written, kept_config = 0, False
    new_code_files = set()
    for name in info["files"]:
        rel = name[len(prefix):]
        top = rel.split("/")[0]
        if top in NEVER or not (rel in TOP_FILES or top in CODE_DIRS):
            continue
        data = z.read(name)
        dest = root / rel
        if rel == "TradBot.bat":
            if dest.exists() and dest.read_bytes() == data:
                continue                             # unchanged - nothing to swap
            dest = root / "TradBot.bat.new"         # swapped in by TradBot.bat on restart
        if rel == "config.yaml" and dest.exists() and dest.read_bytes() != data:
            shutil.copyfile(dest, root / "config.yaml.bak")
            kept_config = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written += 1
        if top == "tradbot":
            new_code_files.add(rel)
    # remove code files that no longer exist in the new version (only inside tradbot/)
    removed = 0
    for p in (root / "tradbot").rglob("*"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if p.is_file() and "__pycache__" not in p.parts and rel not in new_code_files:
            p.unlink()
            removed += 1
    log.info("Update installed: %s -> %s (%d files, %d old files removed)",
             __version__, info["version"], written, removed)
    return {"from": __version__, "to": info["version"], "files": written, "removed": removed,
            "config_backup": kept_config}
