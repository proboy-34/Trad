"""Cloud backup for servers that lose their files on restart (Hugging Face Spaces).

Every 30 minutes the bot copies its memory (paper account, strategies,
models, trade-filter report) to a PRIVATE Hugging Face dataset. After a
restart it downloads that copy first, so it continues where it stopped
(at most ~30 minutes are lost). Price history is not backed up - it is
simply downloaded again.

Turned on automatically when these environment variables / Space secrets exist:
  HF_TOKEN       a Hugging Face access token with WRITE permission
  BACKUP_REPO    optional, e.g. "yourname/tradbot-data"
                 (default inside a Space: "<space id>-data")
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .config import DATA_DIR, STATE_DB
from .log import get_logger

log = get_logger("backup")

FILES = ["tradbot.db", "strategies.json", "lab_runs.json", "trade_filter.json", "backtest.json"]
FOLDERS = ["models"]


def repo_id() -> str | None:
    if os.environ.get("BACKUP_REPO"):
        return os.environ["BACKUP_REPO"].strip()
    space = os.environ.get("SPACE_ID")          # set automatically inside a Hugging Face Space
    return f"{space}-data" if space else None


def enabled() -> bool:
    return bool(os.environ.get("HF_TOKEN") and repo_id())


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ["HF_TOKEN"])


class CloudSync:
    def __init__(self, data_dir: Path = DATA_DIR, db_path: Path = STATE_DB, api=None,
                 every_minutes: float = 30):
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)
        self._api = api
        self.every = every_minutes * 60
        self.status = {"enabled": enabled(), "repo": repo_id(), "last_backup": None,
                       "last_restore": None, "error": ""}
        self._lock = threading.Lock()

    @property
    def api(self):
        if self._api is None:
            self._api = _api()
        return self._api

    # ---------------------------------------------------------- restore
    def restore(self) -> bool:
        """Downloads the last backup if this server has no data yet."""
        if not self.status["enabled"]:
            return False
        if self.db_path.exists():
            log.info("Local data found - no restore needed")
            return False
        try:
            repo = self.status["repo"]
            self.api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
            files = self.api.list_repo_files(repo, repo_type="dataset")
            wanted = [f for f in files if f in FILES or f.split("/")[0] in FOLDERS]
            if not wanted:
                log.info("No backup yet in %s - starting fresh", repo)
                return False
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for f in wanted:
                local = self.api.hf_hub_download(repo, f, repo_type="dataset")
                dest = self.data_dir / f
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local, dest)
            self.status["last_restore"] = time.time()
            log.info("Restored %d files from backup %s - continuing where the bot stopped",
                     len(wanted), repo)
            return True
        except Exception as e:
            self.status["error"] = f"restore failed: {e}"
            log.error("Backup restore failed: %s (starting fresh)", e)
            return False

    # ----------------------------------------------------------- backup
    def backup(self) -> bool:
        if not self.status["enabled"]:
            return False
        with self._lock:
            stage = Path(tempfile.mkdtemp(prefix="tradbot-backup-"))
            try:
                if self.db_path.exists():
                    # safe copy of a database that is in use (SQLite backup API)
                    src = sqlite3.connect(str(self.db_path))
                    dst = sqlite3.connect(str(stage / "tradbot.db"))
                    with dst:
                        src.backup(dst)
                    src.close()
                    dst.close()
                for f in FILES[1:]:
                    if (self.data_dir / f).exists():
                        shutil.copyfile(self.data_dir / f, stage / f)
                for d in FOLDERS:
                    if (self.data_dir / d).is_dir():
                        shutil.copytree(self.data_dir / d, stage / d)
                repo = self.status["repo"]
                self.api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
                self.api.upload_folder(repo_id=repo, folder_path=str(stage), repo_type="dataset",
                                       commit_message="TradBot automatic backup")
                self.status.update(last_backup=time.time(), error="")
                log.info("Backup saved to %s", repo)
                return True
            except Exception as e:
                self.status["error"] = f"backup failed: {e}"
                log.warning("Backup failed: %s (will retry)", e)
                return False
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def start(self, stop_event: threading.Event | None = None):
        if not self.status["enabled"]:
            return
        stop_event = stop_event or threading.Event()

        def loop():
            while not stop_event.wait(self.every):
                self.backup()
        threading.Thread(target=loop, daemon=True, name="backup").start()
        log.info("Automatic backup every %d min to private dataset %s",
                 self.every // 60, self.status["repo"])
