"""Dashboard password protection (for hosting on a server)."""
import base64
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tradbot import dashboard


class FakeAPI:
    def state(self, q):
        return {"ok": True}

    def post(self, action, body):
        return {"ok": True}


@pytest.fixture
def server(monkeypatch):
    api = FakeAPI()
    for name in ("equity", "trades", "messages", "logs", "strategies", "backtest", "candles",
                 "health", "whatsnew"):
        setattr(api, name, api.state)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(api))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url, pw=None):
    req = urllib.request.Request(url)
    if pw is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{pw}".encode()).decode())
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_no_password_on_own_pc(server, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert get(server + "/api/state") == 200


def test_password_required_when_set(server, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    assert get(server + "/api/state") == 401
    assert get(server + "/api/state", "wrong") == 401
    assert get(server + "/api/state", "s3cret") == 200
    assert get(server + "/", "s3cret") == 200


def test_never_public_without_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert dashboard.is_local("127.0.0.1") and not dashboard.is_local("0.0.0.0")
