"""News watcher - reads crypto news headlines and scores the mood per coin.

Every 15 minutes it reads the public RSS feeds of big crypto news sites.
Each coin gets a mood score from -1 (very bad news) to +1 (very good news):
  * with the AI brain on: Gemini/Claude reads the headlines and scores them
  * without it: a simple keyword list (hack, ban, lawsuit = bad; ETF approval = good)

How it is used: a SAFETY FILTER. Very negative news on a coin (or on the
whole market) blocks NEW buys. It never forces a buy - news can't be
backtested honestly (there is no clean history of old headlines), so the
strategies themselves are still built only from price data.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from .log import get_logger

log = get_logger("news")

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]
COIN_WORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
}
NEGATIVE = {
    "hack": -0.8, "hacked": -0.8, "exploit": -0.7, "stolen": -0.7, "drained": -0.7,
    "ban": -0.6, "bans": -0.6, "banned": -0.6, "lawsuit": -0.5, "sues": -0.5, "charged": -0.5,
    "crash": -0.6, "crashes": -0.6, "plunge": -0.5, "plunges": -0.5, "collapse": -0.7,
    "liquidation": -0.4, "liquidations": -0.4, "bankrupt": -0.8, "bankruptcy": -0.8,
    "fraud": -0.6, "delist": -0.6, "delisting": -0.6, "outage": -0.4, "halts": -0.5,
    "selloff": -0.4, "sell-off": -0.4, "dump": -0.4, "fears": -0.3, "warning": -0.3,
    "investigation": -0.4, "probe": -0.4, "slump": -0.4, "tumbles": -0.4,
}
POSITIVE = {
    "approval": 0.5, "approved": 0.5, "approves": 0.5, "etf": 0.2, "adoption": 0.4,
    "partnership": 0.3, "surge": 0.4, "surges": 0.4, "rally": 0.4, "rallies": 0.4,
    "record": 0.3, "upgrade": 0.3, "inflows": 0.4, "bullish": 0.4, "soars": 0.4,
    "gains": 0.2, "jumps": 0.3, "launch": 0.2, "launches": 0.2,
}


def parse_rss(xml_text: str, max_age_hours: float = 24) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    now = time.time()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        ts = now
        pub = it.findtext("pubDate")
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                pass
        if title and now - ts <= max_age_hours * 3600:
            items.append({"title": title, "link": link if link.startswith("http") else "",
                          "time": ts})
    return items


def coins_in(title: str) -> list[str]:
    words = set(re.findall(r"[a-z0-9\-]+", title.lower()))
    return [c for c, keys in COIN_WORDS.items() if words & set(keys)]


def keyword_score(title: str) -> float:
    words = re.findall(r"[a-z\-]+", title.lower())
    s = sum(NEGATIVE.get(w, 0) + POSITIVE.get(w, 0) for w in words)
    return max(-1.0, min(1.0, s))


class NewsWatcher:
    def __init__(self, cfg: dict, brain=None):
        self.cfg = {"enabled": True, "refresh_minutes": 15, "block_below": -0.5,
                    "market_block_below": -0.6, **(cfg.get("news") or {})}
        self.symbols = cfg["symbols"]
        self.brain = brain
        self.items: list[dict] = []
        self.mood: dict[str, dict] = {}      # "BTC" / "market" -> {score, reason, n}
        self.updated = 0.0
        self.source = "none"
        self.error = ""

    def due(self) -> bool:
        return (self.cfg["enabled"]
                and time.time() - self.updated > self.cfg["refresh_minutes"] * 60)

    def refresh(self) -> list[str]:
        """Downloads headlines and re-scores. Returns readable messages."""
        self.updated = time.time()
        items, errors = [], 0
        for url in FEEDS:
            try:
                r = requests.get(url, timeout=12, headers={"User-Agent": "tradbot/1.0"})
                if r.status_code == 200:
                    items += parse_rss(r.text)
                else:
                    errors += 1
            except requests.RequestException:
                errors += 1
        if not items:
            self.error = "could not read any news feed" if errors else "no recent headlines"
            return []
        self.error = ""
        seen, uniq = set(), []
        for it in sorted(items, key=lambda x: -x["time"]):
            key = it["title"].lower()[:80]
            if key not in seen:
                seen.add(key)
                it["coins"] = coins_in(it["title"])
                it["score"] = keyword_score(it["title"])
                uniq.append(it)
        self.items = uniq[:60]
        self.mood, self.source = self._score_with_ai() or (self._score_keywords(), "keywords")
        bad = [f"{k} ({v['score']:+.1f})" for k, v in self.mood.items()
               if v["score"] <= self.cfg["block_below"]]
        msg = (f"News checked: {len(self.items)} headlines ({self.source}). "
               + (f"Bad news - no new buys on: {', '.join(bad)}." if bad else "No dangerous news."))
        return [msg]

    def _score_keywords(self) -> dict:
        mood = {}
        for key in [s.split("/")[0] for s in self.symbols] + ["market"]:
            rel = [it for it in self.items
                   if (key in it["coins"]) or (key == "market" and not it["coins"])]
            if rel:
                avg = sum(it["score"] for it in rel[:15]) / max(3, len(rel[:15]))
                worst = min(rel, key=lambda x: x["score"])
                # one really bad headline (hack, ban, crash) must count, not be averaged away
                score = 0.6 * worst["score"] + 0.4 * avg if worst["score"] < 0 else avg
                mood[key] = {"score": round(max(-1, min(1, score)), 2), "n": len(rel),
                             "reason": worst["title"] if worst["score"] < 0 else rel[0]["title"]}
            else:
                mood[key] = {"score": 0.0, "n": 0, "reason": "no news"}
        return mood

    def _score_with_ai(self):
        if not self.brain or not self.brain.available():
            return None
        coins = [s.split("/")[0] for s in self.symbols]
        heads = "\n".join(f"- {it['title']}" for it in self.items[:40])
        text = self.brain.ask(
            f"Recent crypto news headlines (last 24h):\n{heads}\n\n"
            f"For each of {coins} and for the overall crypto 'market', give a mood score from -1 "
            "(dangerous news: hacks, bans, crashes) to +1 (very positive). Use 0 when there is "
            "no relevant news. Answer ONLY with JSON like "
            '{"BTC": {"score": 0.1, "reason": "short reason"}, ..., "market": {...}}',
            max_tokens=1500)
        from .brain import _extract_json
        data = _extract_json(text or "")
        if not isinstance(data, dict):
            return None
        mood = {}
        for key in coins + ["market"]:
            v = data.get(key) or {}
            try:
                score = max(-1.0, min(1.0, float(v.get("score", 0))))
            except (TypeError, ValueError):
                score = 0.0
            mood[key] = {"score": round(score, 2), "reason": str(v.get("reason", ""))[:200],
                         "n": sum(key in it["coins"] for it in self.items)}
        return mood, "AI"

    def blocks(self, symbol: str) -> str | None:
        """Reason to block a new buy, or None."""
        coin = symbol.split("/")[0]
        m, mk = self.mood.get(coin), self.mood.get("market")
        if m and m["score"] <= self.cfg["block_below"]:
            return f"bad news on {coin} ({m['score']:+.1f}): {m['reason'][:120]}"
        if mk and mk["score"] <= self.cfg["market_block_below"]:
            return f"bad news for the whole market ({mk['score']:+.1f}): {mk['reason'][:120]}"
        return None

    def score(self, symbol: str) -> float | None:
        m = self.mood.get(symbol.split("/")[0])
        return m["score"] if m else None

    def status(self) -> dict:
        return {"updated": self.updated, "source": self.source, "error": self.error,
                "mood": self.mood, "headlines": self.items[:15]}
