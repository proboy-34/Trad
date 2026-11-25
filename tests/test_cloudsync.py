"""Backup to / restore from a Hugging Face dataset (fake API, no network)."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from tradbot import cloudsync
from tradbot.cloudsync import CloudSync


class FakeHub:
    """Stores 'uploaded' files in a local folder like a dataset repo."""

    def __init__(self, root: Path):
        self.root = root
        self.created = []

    def create_repo(self, repo, repo_type, private, exist_ok):
        assert private is True and repo_type == "dataset"
        self.created.append(repo)

    def upload_folder(self, repo_id, folder_path, repo_type, commit_message):
        shutil.copytree(folder_path, self.root, dirs_exist_ok=True)

    def list_repo_files(self, repo, repo_type):
        return [str(p.relative_to(self.root)).replace("\\", "/")
                for p in self.root.rglob("*") if p.is_file()]

    def hf_hub_download(self, repo, filename, repo_type):
        return str(self.root / filename)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "x")
    monkeypatch.setenv("SPACE_ID", "me/tradbot")
    monkeypatch.delenv("BACKUP_REPO", raising=False)


def make_data(d: Path):
    d.mkdir(parents=True)
    db = sqlite3.connect(str(d / "tradbot.db"))
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (42)")
    db.commit()                      # left OPEN on purpose, like the running bot
    (d / "strategies.json").write_text('{"pool": [1, 2, 3]}')
    (d / "models").mkdir()
    (d / "models" / "BTCUSDT.pkl").write_bytes(b"model")
    (d / "candles").mkdir()
    (d / "candles" / "BTCUSDT_1h.csv").write_text("big price file")
    return db


def test_backup_then_restore_on_a_fresh_server(tmp_path):
    hub = FakeHub(tmp_path / "hub")
    old = tmp_path / "old"
    db = make_data(old)
    assert cloudsync.repo_id() == "me/tradbot-data"
    assert CloudSync(old, old / "tradbot.db", api=hub).backup()
    db.close()
    uploaded = set(hub.list_repo_files("r", "dataset"))
    assert {"tradbot.db", "strategies.json", "models/BTCUSDT.pkl"} <= uploaded
    assert not any(f.startswith("candles") for f in uploaded)   # prices are re-downloaded

    new = tmp_path / "new"                                       # restarted server: empty
    assert CloudSync(new, new / "tradbot.db", api=hub).restore()
    assert sqlite3.connect(str(new / "tradbot.db")).execute("SELECT x FROM t").fetchone() == (42,)
    assert (new / "strategies.json").read_text() == '{"pool": [1, 2, 3]}'
    assert (new / "models" / "BTCUSDT.pkl").read_bytes() == b"model"


def test_no_restore_over_existing_data_and_off_without_token(tmp_path, monkeypatch):
    hub = FakeHub(tmp_path / "hub")
    d = tmp_path / "d"
    make_data(d).close()
    assert not CloudSync(d, d / "tradbot.db", api=hub).restore()     # never overwrite local data
    monkeypatch.delenv("HF_TOKEN")
    s = CloudSync(d, d / "tradbot.db", api=hub)
    assert not s.status["enabled"] and not s.backup()
