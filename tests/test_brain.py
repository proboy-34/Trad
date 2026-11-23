"""AI brain picks a working free Gemini model (no real network used)."""
import json

import pytest

import tradbot.brain as brain_mod
from tradbot.brain import Brain
from tradbot.config import DEFAULTS


class FakeResp:
    def __init__(self, code, data=None, text=None):
        self.status_code = code
        self._data = data or {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


MODELS = {"models": [
    {"name": "models/gemini-omni-1.2-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.0-pro", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-flash-lite-latest", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.0-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]},
]}
OK = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(brain_mod, "save_env_value", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_PROVIDER", raising=False)
    cfg = {**DEFAULTS, "brain": {**DEFAULTS["brain"], "gemini_model": "gemini-2.5-flash"}}
    b = Brain(cfg)
    b.saved = saved
    return b


def test_candidate_order(brain, monkeypatch):
    monkeypatch.setattr(brain_mod.requests, "get", lambda *a, **k: FakeResp(200, MODELS))
    c = brain._gemini_candidates({})
    assert c[0] == "gemini-flash-latest" and c[1] == "gemini-3.0-flash"
    assert "gemini-omni-1.2-flash" not in c and "gemini-3.0-flash-image" not in c
    assert c[-1] == "gemini-3.0-pro"


def test_retired_and_non_free_models_are_skipped(brain, monkeypatch):
    """Like the user's PC: 2.5-flash retired (404), next one has no free quota (429)."""
    calls = []

    def post(url, **kw):
        model = url.split("/models/")[1].split(":")[0]
        calls.append(model)
        if model == "gemini-2.5-flash":
            return FakeResp(404, text="not found")
        if model == "gemini-flash-latest":
            return FakeResp(429, text="Quota exceeded ... limit: 0")
        return FakeResp(200, OK)

    monkeypatch.setattr(brain_mod.requests, "get", lambda *a, **k: FakeResp(200, MODELS))
    monkeypatch.setattr(brain_mod.requests, "post", post)
    assert brain.ask("hi") == "hello"
    assert calls == ["gemini-2.5-flash", "gemini-flash-latest", "gemini-3.0-flash"]
    assert brain.saved == {"GEMINI_MODEL": "gemini-3.0-flash"}     # remembered
    # once a model works, a normal 429 means "wait", not "switch model"
    monkeypatch.setattr(brain_mod.requests, "post",
                        lambda url, **kw: FakeResp(429, text="rate limited, retry in 30s"))
    assert brain.ask("hi again") is None
    assert "limit reached" in brain.last_error
    assert brain._gemini_model == "gemini-3.0-flash"


def test_news_tool_rejected_falls_back(brain, monkeypatch):
    bodies = []

    def post(url, json=None, **kw):
        bodies.append(json)
        return FakeResp(400, text="tool not supported") if "tools" in json else FakeResp(200, OK)

    monkeypatch.setattr(brain_mod.requests, "post", post)
    assert brain.ask("news?", news=True) == "hello"
    assert brain.news_ok is False and len(bodies) == 2
