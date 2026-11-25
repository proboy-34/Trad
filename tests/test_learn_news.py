"""Lessons from trades, learned trade sizes, and news scoring (no network)."""
import time

import pytest

from tradbot import news as news_mod
from tradbot.config import DEFAULTS, _merge
from tradbot.learn import learn, lesson_for, size_multiplier
from tradbot.news import NewsWatcher, coins_in, keyword_score, parse_rss
from tradbot.paper import PaperAccount


def trade(reason, entry=100, exit_=97, low=None, high=None, pnl=-3, **kw):
    return {"entry_price": entry, "exit_price": exit_, "low_seen": low or min(entry, exit_),
            "high_seen": high or max(entry, exit_), "stop": 97, "take": 106, "reason": reason,
            "pnl": pnl, "symbol": kw.get("symbol", "BTC/USDT"),
            "strategy_id": kw.get("sid", "S1"), "tags": kw.get("tags", "")}


def test_lessons():
    assert "worked" in lesson_for(trade("take-profit", exit_=106, pnl=6))[0]
    text, tags = lesson_for(trade("stop-loss", high=104.5))
    assert tags == ["gave_back"] and "reversed" in text
    assert lesson_for(trade("stop-loss", high=100.2))[1] == ["bad_entry"]
    assert lesson_for(trade("stop-loss", high=101.5))[1] == ["stopped"]
    assert lesson_for(trade("time limit", exit_=99, pnl=-1))[1] == ["slow_loss"]
    t = trade("take-profit", exit_=106, pnl=6)
    t["news_score"] = -0.4
    assert "news_negative" in lesson_for(t)[1]


def test_learning_cuts_size_of_losers_only():
    trades = ([trade("stop-loss", symbol="DOGE/USDT", tags="bad_entry") for _ in range(5)]
              + [trade("take-profit", exit_=106, pnl=6, symbol="BTC/USDT", sid="S2") for _ in range(5)])
    adj = learn(trades)
    assert adj["coins"]["DOGE/USDT"]["mult"] == 0.5
    assert adj["coins"]["BTC/USDT"]["mult"] == 1.0
    assert size_multiplier(adj, "DOGE/USDT", "S2") == 0.5
    assert size_multiplier(adj, "BTC/USDT", "S2") == 1.0
    assert adj["mistakes"] == {"bad_entry": 5}
    assert any("DOGE/USDT keeps losing" in x for x in adj["insights"])
    assert learn([])["insights"]                      # always something to show


def test_paper_db_records_extremes_and_lesson(tmp_path):
    acc = PaperAccount(DEFAULTS, db_path=tmp_path / "a.db")
    p = acc.open_position("BTC/USDT", {"id": "S1"}, 100, 1000, 97, 106, 1e12, news_score=0.1)
    acc.update_extremes("BTC/USDT", 104)
    acc.update_extremes("BTC/USDT", 98.5)
    t = acc.close_position(p["id"], 97, "stop-loss")
    assert t["high_seen"] == 104 and t["low_seen"] == 98.5 and t["news_score"] == 0.1
    text, tags = lesson_for(t)
    acc.set_lesson(t["trade_id"], text, tags, -3.0, 4.0)
    row = acc.trades()[0]
    assert row["tags"] == "gave_back" and row["lesson"] == text


RSS = f"""<?xml version="1.0"?><rss><channel>
<item><title>Major exchange hacked, $200M in Ethereum stolen</title><link>https://ex.com/a</link>
<pubDate>{time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}</pubDate></item>
<item><title>Bitcoin ETF inflows hit record as BTC rallies</title><link>https://ex.com/b</link></item>
<item><title>Old story</title><pubDate>Mon, 01 Jan 2018 00:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_rss_parsing_and_keywords():
    items = parse_rss(RSS)
    assert [i["title"][:5] for i in items] == ["Major", "Bitco"]          # old story dropped
    assert coins_in(items[0]["title"]) == ["ETH"]
    assert coins_in(items[1]["title"]) == ["BTC"]
    assert keyword_score(items[0]["title"]) < -0.5
    assert keyword_score(items[1]["title"]) > 0.3
    assert parse_rss("not xml") == []


class FakeResp:
    status_code = 200
    text = RSS


def test_news_blocks_buys_on_bad_news(monkeypatch):
    monkeypatch.setattr(news_mod.requests, "get", lambda *a, **k: FakeResp())
    cfg = _merge(DEFAULTS, {"symbols": ["BTC/USDT", "ETH/USDT"]})
    w = NewsWatcher(cfg)
    msgs = w.refresh()
    assert w.source == "keywords" and "ETH" in msgs[0]
    assert w.blocks("ETH/USDT") and "hacked" in w.blocks("ETH/USDT")
    assert w.blocks("BTC/USDT") is None
    assert not w.due()
