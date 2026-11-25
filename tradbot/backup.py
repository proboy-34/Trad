"""Backups of the bot's memory (paper account, strategies, models).

* daily automatic backup -> data/backups/tradbot-YYYY-MM-DD.zip (keeps the last N days)
* "Download backup" in the dashboard -> the same zip, to keep on Google Drive / USB
* "Restore backup" -> the uploaded zip is checked, then applied on the next start
  (the current data is backed up first, so a restore can always be undone)

Price history is not included - it is simply downloaded again.
"""
from __future__ import annotations

import io
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from .config import DATA_DIR, STATE_DB
from .log import get_logger

log = get_logger("backup")

BACKUP_DIR = DATA_DIR / "backups"
PENDING_DIR = DATA_DIR / "restore_pending"
FILES = ["strategies.json", "trade_filter.json", "backtest.json"]
FOLDERS = ["models"]
MAX_UPLOAD = 50 * 1024 * 1024


def _allowed(name: str) -> bool:
    name = name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return False
    return (name == "tradbot.db" or name in FILES
            or (name.startswith("models/") and name.endswith((".pkl", ".txt", ".json"))))


def build_zip(data_dir: Path = DATA_DIR, db_path: Path = STATE_DB) -> bytes:
    """Zip of the bot's memory. The database is copied safely even while in use."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="tradbot-backup-") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if db_path.exists():
                src = sqlite3.connect(str(db_path))
                dst = sqlite3.connect(str(tmp / "tradbot.db"))
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                z.write(tmp / "tradbot.db", "tradbot.db")
            for f in FILES:
                if (data_dir / f).exists():
                    z.write(data_dir / f, f)
            for d in FOLDERS:
                for p in sorted((data_dir / d).glob("*")) if (data_dir / d).is_dir() else []:
                    if p.is_file():
                        z.write(p, f"{d}/{p.name}")
            z.writestr("README.txt", "TradBot backup - restore it in the dashboard: "
                                     "Settings -> Backups -> Restore backup.\n")
    return buf.getvalue()


def daily(keep_days: int = 7, data_dir: Path = DATA_DIR, db_path: Path = STATE_DB) -> Path | None:
    """Creates today's backup if it doesn't exist yet and removes old ones."""
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"tradbot-{time.strftime('%Y-%m-%d')}.zip"
    made = None
    if not target.exists() and db_path.exists():
        target.write_bytes(build_zip(data_dir, db_path))
        made = target
        log.info("Daily backup saved: %s", target.name)
    dailies = sorted(out_dir.glob("tradbot-????-??-??.zip"))
    for old in dailies[:-keep_days]:
        old.unlink(missing_ok=True)
    return made


def last_backup_time(data_dir: Path = DATA_DIR) -> float | None:
    files = list((data_dir / "backups").glob("*.zip")) if (data_dir / "backups").is_dir() else []
    return max((f.stat().st_mtime for f in files), default=None)


def stage_restore(zip_bytes: bytes, data_dir: Path = DATA_DIR) -> str:
    """Checks an uploaded backup and prepares it. It is applied on the next start."""
    if len(zip_bytes) > MAX_UPLOAD:
        raise ValueError("file too big for a TradBot backup")
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("this is not a zip file")
    names = [n for n in z.namelist() if not n.endswith("/")]
    if "tradbot.db" not in names:
        raise ValueError("this zip is not a TradBot backup (tradbot.db is missing)")
    pending = data_dir / "restore_pending"
    shutil.rmtree(pending, ignore_errors=True)
    pending.mkdir(parents=True)
    for n in names:
        if _allowed(n):
            dest = pending / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(n))
    # make sure the database is really a working SQLite file
    try:
        con = sqlite3.connect(str(pending / "tradbot.db"))
        con.execute("SELECT * FROM account").fetchone()
        con.close()
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise ValueError("the database inside this backup is damaged")
    return "Backup checked. It will be restored when the bot restarts."


def apply_pending(data_dir: Path = DATA_DIR, db_path: Path = STATE_DB) -> bool:
    """Called at start-up, before the bot opens its database."""
    pending = data_dir / "restore_pending"
    if not (pending / "tradbot.db").exists():
        return False
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():          # keep the current data, so the restore can be undone
        (out_dir / f"before-restore-{time.strftime('%Y%m%d-%H%M%S')}.zip").write_bytes(
            build_zip(data_dir, db_path))
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.rmtree(data_dir / "models", ignore_errors=True)
    for p in sorted(pending.rglob("*")):
        if p.is_file():
            rel = p.relative_to(pending)
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
    shutil.rmtree(pending, ignore_errors=True)
    log.info("Backup restored")
    return True
