"""AI brain - an LLM (Gemini or Claude) that thinks like a human trader.

What it is ALLOWED to do:
  * suggest new strategy ideas  -> they still must pass the Strategy Lab tests
  * review a trade and BLOCK it -> it can never force a trade
  * write a plain-language daily report
What it can NEVER do: skip the Risk Manager, change risk limits, move money.

If the AI is unavailable (no key, no internet, free limit reached), the bot
simply continues without it.
"""
from __future__ import annotations

import collections
import json
import os
import re
import time

import requests

from .features import FEATURES
from .log import get_logger
from .strategy import Strategy, validate_strategy_dict

log = get_logger("brain")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM = (
    "You are the careful analyst inside an automated crypto spot-trading bot "
    "(long only, no leverage, paper trading with fake money). Be skeptical and "
    "risk-aware. Never invent numbers you were not given. Keep answers short and "
    "in simple English that a beginner understands."
)


def _extract_json(text: str):
    """Finds the first JSON object/array in a model's answer."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    for opener, closer in (("[", "]"), ("{", "}")):
        a, b = text.find(opener), text.rfind(closer)
        if a != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                continue
    return None


class Brain:
    def __init__(self, cfg: dict, on_message=None):
        self.cfg = cfg["brain"]
        # the dashboard Settings page can override config.yaml (saved in .env)
        self.provider = (os.environ.get("BRAIN_PROVIDER") or self.cfg.get("provider") or "none").lower()
        self.on_message = on_message or (lambda kind, text: None)
        self.calls = collections.deque()
        self.news_ok = bool(self.cfg.get("use_news", True))
        self.last_error = ""
        self._gemini_model = self.cfg.get("gemini_model", "gemini-2.5-flash")
        self._claude = None

    # ----------------------------------------------------------- status
    def key(self) -> str:
        if self.provider == "gemini":
            return os.environ.get("GEMINI_API_KEY", "")
        if self.provider == "claude":
            return os.environ.get("ANTHROPIC_API_KEY", "")
        return ""

    def available(self) -> bool:
        return self.provider in ("gemini", "claude") and bool(self.key())

    def status(self) -> dict:
        return {"provider": self.provider, "ready": self.available(),
                "calls_last_hour": len(self.calls), "last_error": self.last_error,
                "model": self._gemini_model if self.provider == "gemini" else self.cfg.get("claude_model")}

    def _rate_ok(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > 3600:
            self.calls.popleft()
        if len(self.calls) >= int(self.cfg.get("max_calls_per_hour", 20)):
            return False
        self.calls.append(now)
        return True

    # ------------------------------------------------------------- core
    def ask(self, prompt: str, news: bool = False, max_tokens: int = 2000) -> str | None:
        if not self.available():
            return None
        if not self._rate_ok():
            self.last_error = "hourly AI call limit reached (protecting free tier)"
            return None
        try:
            if self.provider == "gemini":
                text = self._ask_gemini(prompt, news and self.news_ok)
            else:
                text = self._ask_claude(prompt, max_tokens)
            self.last_error = ""
            return text
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning("AI brain call failed: %s", self.last_error)
            return None

    def _ask_gemini(self, prompt: str, news: bool) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4},
        }
        if news:
            body["tools"] = [{"google_search": {}}]
        headers = {"x-goog-api-key": self.key(), "Content-Type": "application/json"}
        url = f"{GEMINI_URL}/models/{self._gemini_model}:generateContent"
        r = requests.post(url, json=body, headers=headers, timeout=60)
        if r.status_code == 404:
            # model name retired? pick the newest available "flash" model
            new = self._find_gemini_model(headers)
            if new and new != self._gemini_model:
                log.info("Gemini model %s not found, switching to %s", self._gemini_model, new)
                self._gemini_model = new
                return self._ask_gemini(prompt, news)
        if r.status_code in (400, 401, 403) and ("API key not valid" in r.text
                                                 or "API_KEY_INVALID" in r.text):
            raise RuntimeError("Gemini says the API key is not valid - copy it again "
                               "from aistudio.google.com and save it in Settings")
        if r.status_code == 400 and "location is not supported" in r.text.lower():
            raise RuntimeError("Gemini's free API is not available in your country/region")
        if r.status_code == 400 and news:
            # web search not allowed for this key/model - continue without it
            self.news_ok = False
            log.info("Gemini web search not available, continuing without news")
            return self._ask_gemini(prompt, False)
        if r.status_code == 429:
            raise RuntimeError("Gemini free-tier limit reached, will retry later")
        if r.status_code in (401, 403):
            raise RuntimeError("Gemini rejected the API key - check it in Settings")
        r.raise_for_status()
        data = r.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()

    def _find_gemini_model(self, headers) -> str | None:
        try:
            r = requests.get(f"{GEMINI_URL}/models", headers=headers, timeout=20)
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            flash = [n for n in names if "flash" in n and "lite" not in n and "preview" not in n
                     and "exp" not in n]
            return sorted(flash)[-1] if flash else (names[0] if names else None)
        except Exception:
            return None

    def _ask_claude(self, prompt: str, max_tokens: int) -> str | None:
        import anthropic   # optional dependency, only needed for provider: claude
        if self._claude is None:
            self._claude = anthropic.Anthropic(api_key=self.key(), max_retries=2, timeout=120)
        resp = self._claude.beta.messages.create(
            model=self.cfg.get("claude_model", "claude-opus-5-5"),
            max_tokens=max(max_tokens, 4000),
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low"},          # short, cheap answers
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                      # retry on another model if declined
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    # ------------------------------------------------------------ tasks
    def suggest_strategies(self, active: list, levels: dict, n: int = 8) -> list[Strategy]:
        feats = "\n".join(
            f"- {k}: {FEATURES[k][0]} (typical range {v[0]:.4g} to {v[-1]:.4g}, middle {v[len(v)//2]:.4g})"
            for k, v in levels.items())
        current = json.dumps([a["strategy"] for a in active][:5])
        prompt = f"""Suggest {n} NEW long-only strategy ideas for 1-hour crypto candles (BTC/USDT, ETH/USDT).
Available indicators (use the exact names, thresholds inside the typical range):
{feats}

Strategies currently in use (try different ideas): {current}

Answer ONLY with a JSON array. Each item:
{{"entry": [{{"feature": "<name>", "op": "<" or ">", "value": <number>}}, ...1-3 items],
  "stop_atr": <0.5-5>, "take_atr": <0.5-10>, "max_hold": <candles 6-120>,
  "exit": [], "why": "<one short sentence>"}}"""
        text = self.ask(prompt)
        data = _extract_json(text or "")
        out = []
        if isinstance(data, list):
            for d in data:
                if isinstance(d, dict):
                    why = d.pop("why", "")
                    s = validate_strategy_dict(d)
                    if s:
                        out.append(s)
                        if why:
                            log.info("AI idea %s: %s", s.id, why)
        if out:
            self.on_message("ideas", f"Suggested {len(out)} new strategy ideas for the Strategy Lab.")
        return out

    def review_trade(self, ctx: dict) -> tuple[bool, str]:
        """Returns (approve, reason). On any problem: approve (rules already passed)."""
        prompt = f"""The bot's tested rules want to BUY {ctx['symbol']} now (paper trade).
Facts: {json.dumps(ctx, default=str)}
{"Also quickly check today's crypto news for anything dangerous (exchange hacks, major crashes, regulation shocks)." if self.news_ok else ""}
Should this trade be BLOCKED? Only block with a clear, concrete reason.
Answer ONLY with JSON: {{"decision": "approve" or "block", "reason": "<one short sentence>"}}"""
        text = self.ask(prompt, news=True, max_tokens=1000)
        data = _extract_json(text or "")
        if not isinstance(data, dict):
            return True, "AI review unavailable - rules decided"
        decision = str(data.get("decision", "approve")).lower()
        reason = str(data.get("reason", ""))[:300]
        self.on_message("review", f"{ctx['symbol']} {ctx.get('strategy')}: {decision.upper()} - {reason}")
        return decision != "block", reason

    def daily_report(self, summary: dict) -> str | None:
        prompt = f"""Write today's short report (max 150 words) for the bot owner, who is a beginner.
Explain what happened, why, and whether anything needs attention. Data:
{json.dumps(summary, default=str)}"""
        text = self.ask(prompt, max_tokens=1500)
        if text:
            self.on_message("report", text)
        return text
