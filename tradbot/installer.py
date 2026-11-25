"""Installs the libraries TradBot needs - safely and only when needed.

* core libraries (requirements.txt) must install, or TradBot cannot run
* optional libraries (requirements-optional.txt) are installed ONE BY ONE; if one
  fails (for example on a brand-new Python version) the bot still starts and only
  that feature is switched off. Failed ones are retried once a week.
* nothing is downloaded when everything is already installed (fast start)

Uses only the Python standard library, so it works before anything is installed.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_FILE = ROOT / "requirements.txt"
OPTIONAL_FILE = ROOT / "requirements-optional.txt"
STATE_FILE = ROOT / "data" / "install_state.json"
RETRY_FAILED_AFTER = 7 * 86400

# pip name -> import name (when they differ)
IMPORT_NAMES = {"pyyaml": "yaml", "scikit-learn": "sklearn"}


def read_requirements(path: Path) -> list[str]:
    reqs = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split(" #")[0].strip()
            if line and not line.startswith(("#", "-")):
                reqs.append(line)
    return reqs


def import_name(req: str) -> str:
    name = re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0].strip().lower()
    return IMPORT_NAMES.get(name, name.replace("-", "_"))


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _fingerprint() -> str:
    h = hashlib.sha1()
    for f in (CORE_FILE, OPTIONAL_FILE):
        h.update(f.read_bytes() if f.exists() else b"")
    h.update(sys.version.encode())
    return h.hexdigest()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def plan() -> dict:
    """What needs installing right now (nothing is installed by this function)."""
    state = _load_state()
    core = read_requirements(CORE_FILE)
    optional = read_requirements(OPTIONAL_FILE)
    failed = set(state.get("failed", []))
    retry = time.time() - state.get("time", 0) > RETRY_FAILED_AFTER
    missing_core = [r for r in core if not has_module(import_name(r))]
    missing_optional = [r for r in optional if not has_module(import_name(r))
                        and (r not in failed or retry)]
    changed = state.get("fingerprint") != _fingerprint()
    return {"core": core, "optional": optional, "missing_core": missing_core,
            "missing_optional": missing_optional, "changed": changed,
            "needed": bool(missing_core or missing_optional or changed)}


def _pip(*args) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "--no-cache-dir", *args]
    return subprocess.call(cmd) == 0


def install(force: bool = False, out=print) -> dict:
    """Installs what is missing. Raises RuntimeError only if CORE libraries fail."""
    p = plan()
    if not (force or p["needed"]):
        return {"installed": False, "failed": _load_state().get("failed", [])}
    out("Installing libraries (first time or after an update - this can take a few minutes)...")
    if p["core"] and (force or p["missing_core"] or p["changed"]):
        if not _pip("-r", str(CORE_FILE)):
            raise RuntimeError("Installing the core libraries failed. Check your internet "
                               "connection and try again.")
    failed = []
    for req in p["optional"]:
        if not force and has_module(import_name(req)) and not p["changed"]:
            continue
        out(f"  optional: {req}")
        if not _pip(req):
            failed.append(req)
            out(f"  (could not install {req} - TradBot will run without it)")
    importlib.invalidate_caches()
    _save_state({"fingerprint": _fingerprint(), "failed": failed, "time": time.time(),
                 "python": sys.version.split()[0]})
    return {"installed": True, "failed": failed}


def ensure(out=print) -> None:
    """Called before every start: installs only if something is missing or changed."""
    try:
        if plan()["needed"]:
            install(out=out)
    except RuntimeError:
        raise
    except Exception as e:     # never block the start because of the installer itself
        out(f"Library check skipped: {e}")
