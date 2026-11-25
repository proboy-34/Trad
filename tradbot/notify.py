"""Phone alerts through Telegram (optional, free).

Setup (2 minutes): in Telegram talk to @BotFather -> /newbot -> copy the token ->
paste it in the dashboard (Settings -> Phone alerts) -> send /start to your new bot ->
click Connect. The bot finds your chat by itself and sends a test message.

Messages are sent in the background (never slows the bot) and are rate-limited.
"""
from __future__ import annotations

import os
import queue
import threading
import time

import requests

from .config import save_env_value
from .log import get_logger

log = get_logger("notify")
API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, cfg: dict):
        self.nc = {"trades": True, "lessons": False, "daily": True, "alerts": True,
                   **(cfg.get("notify") or {})}
        self.q: queue.Queue = queue.Queue(maxsize=50)
        self.sent = 0
        self.last_error = ""
        self._worker = None

    @staticmethod
    def token() -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "")

    @staticmethod
    def chat() -> str:
        return os.environ.get("TELEGRAM_CHAT_ID", "")

    def enabled(self) -> bool:
        return bool(self.token() and self.chat())

    def status(self) -> dict:
        return {"enabled": self.enabled(), "token_saved": bool(self.token()), "sent": self.sent,
                "last_error": self.last_error,
                "types": [k for k, v in self.nc.items() if v is True]}

    # ------------------------------------------------------------ send
    def send(self, text: str, kind: str = "trades") -> bool:
        if not self.enabled() or not self.nc.get(kind, False):
            return False
        try:
            self.q.put_nowait(text[:3900])
        except queue.Full:
            return False
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, daemon=True, name="telegram")
            self._worker.start()
        return True

    def _post(self, method: str, **payload):
        r = requests.post(API.format(token=self.token(), method=method), json=payload, timeout=15)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok", False):
            raise RuntimeError(data.get("description") or f"HTTP {r.status_code}")
        return data

    def _run(self):
        while True:
            try:
                text = self.q.get(timeout=60)
            except queue.Empty:
                return
            for attempt in range(3):
                try:
                    self._post("sendMessage", chat_id=self.chat(), text=text,
                               disable_web_page_preview=True)
                    self.sent += 1
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = str(e)[:200]
                    time.sleep(5 * (attempt + 1))
            time.sleep(1.1)            # Telegram allows about 1 message per second

    # --------------------------------------------------------- connect
    def connect(self, token: str | None = None) -> tuple[bool, str]:
        """Saves the token, finds the chat where you sent /start, sends a test message."""
        if token:
            save_env_value("TELEGRAM_BOT_TOKEN", token.strip())
        if not self.token():
            return False, "Paste the bot token from @BotFather first."
        try:
            me = self._post("getMe")["result"]
            updates = self._post("getUpdates")["result"]
        except Exception as e:
            return False, f"Telegram did not accept the token ({e})."
        chats = [u.get("message", {}).get("chat", {}).get("id") for u in updates
                 if u.get("message", {}).get("chat", {}).get("type") == "private"]
        chats = [c for c in chats if c]
        if not chats:
            return False, (f"Token OK (bot @{me.get('username')}). Now open Telegram, send /start "
                           "to your bot, then click Connect again.")
        save_env_value("TELEGRAM_CHAT_ID", str(chats[-1]))
        try:
            self._post("sendMessage", chat_id=chats[-1],
                       text="TradBot connected. You will get alerts here (paper trading, fake money).")
        except Exception as e:
            return False, f"Connected, but the test message failed: {e}"
        return True, f"Connected to @{me.get('username')}. A test message was sent to your Telegram."

    def disconnect(self):
        save_env_value("TELEGRAM_CHAT_ID", "")
        save_env_value("TELEGRAM_BOT_TOKEN", "")
