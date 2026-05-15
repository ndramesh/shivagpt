#!/usr/bin/env python3
"""Probe every candidate /trader source in parallel and report which work.

Run on kailash:
    ~/shivagpt/.venv/bin/python ~/shivagpt/scripts/trader_probe.py

Pure stdlib + httpx (already in the shivagpt venv). Each scanner runs once,
no LLM calls, no aggregation — just "did the source return useful data?"
Sources are tried in parallel; total runtime is bounded by the slowest one.

Paste the output back to me and I'll fold the working sources into the
default /trader source list.
"""

from __future__ import annotations
import asyncio
import json
import re
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run with the shivagpt venv:")
    print("  ~/shivagpt/.venv/bin/python scripts/trader_probe.py")
    sys.exit(1)


UA = "ShivaGPT/1.0 (social sentiment scanner)"
HEADERS = {"User-Agent": UA}
TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=5.0, pool=5.0)

# Ticker extraction — same as the server-side helper, lifted here so the
# script is self-contained.
_BLACKLIST = {
    "I", "A", "THE", "AND", "FOR", "BUT", "NOT", "YOU", "ALL", "OUR", "WAS",
    "ONE", "NEW", "ITS", "NOW", "OUT", "TWO", "WHO", "GET", "HAS", "HAD",
    "HAVE", "WILL", "WHEN", "FROM", "THIS", "THAT", "WITH", "BEEN", "INTO",
    "MAKE", "TAKE", "COME", "KNOW", "WANT", "GIVE", "YEAR", "LAST", "NEXT",
    "EACH", "HIGH", "OPEN", "HOPE", "HUGE", "POST", "SURE", "ZERO", "HALF",
    "PURE", "FAST", "SLOW", "EASY", "HARD", "TRUE", "FAKE", "BUY", "SELL",
    "PUT", "CALL", "LONG", "SHORT", "BEAR", "BULL", "PUMP", "DUMP", "HOLD",
    "GAIN", "LOSS", "MOON", "DIPS", "BAGS", "CASH", "DEBT", "LOAN", "BOND",
    "CEO", "CFO", "CTO", "COO", "IPO", "ETF", "ATH", "ATL", "DD", "IMO",
    "YOLO", "FOMO", "FYI", "EPS", "GDP", "CPI", "PPI", "RSI", "MACD", "PE",
    "AI", "API", "IT", "UK", "US", "USA", "EU", "SEC", "FDA", "FED", "FOMC",
    "IV", "DTE", "OTM", "ITM", "ATM", "OI", "EOD", "AH", "ER", "PT", "QE",
    "YTD", "QOQ", "MOM", "WOW", "DOW", "LOL", "WTF", "OMG", "TBH", "TLDR",
    "PSA", "LFG", "NFT", "RIP", "PDT", "IRA", "ETH", "BTC", "USD", "EUR",
}
_TICKER_DOLLAR = re.compile(r"\$([A-Z]{2,5})\b")
_TICKER_PLAIN = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str) -> list[str]:
    found = set()
    for m in _TICKER_DOLLAR.finditer(text or ""):
        t = m.group(1).upper()
        if t not in _BLACKLIST:
            found.add(t)
    if not found:
        for m in _TICKER_PLAIN.finditer(text or ""):
            t = m.group(1).upper()
            if t not in _BLACKLIST and len(t) >= 2:
                found.add(t)
    return sorted(found)


# --------------------------------------------------------------------------
# Probes — each returns {ok, items, tickers, note?} or raises.
# --------------------------------------------------------------------------

async def probe_reddit(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get(
        "https://www.reddit.com/r/wallstreetbets/hot.json",
        params={"limit": "25", "raw_json": "1"},
    )
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    posts = r.json().get("data", {}).get("children", [])
    tickers: set[str] = set()
    for p in posts:
        d = p.get("data", {})
        tickers.update(extract_tickers(f"{d.get('title','')} {d.get('selftext','')}"))
    return {"ok": len(posts) > 0, "items": len(posts), "tickers": sorted(tickers)[:10]}


async def probe_stocktwits(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get("https://api.stocktwits.com/api/2/trending/symbols.json")
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    symbols = r.json().get("symbols", [])
    return {
        "ok": len(symbols) > 0,
        "items": len(symbols),
        "tickers": [s.get("symbol", "").upper() for s in symbols[:10]],
    }


async def probe_yahoo() -> dict[str, Any]:
    # yfinance is sync — wrap in thread
    def _do():
        try:
            import yfinance as yf
        except ImportError:
            return {"ok": False, "items": 0, "note": "yfinance not installed"}
        try:
            s = yf.Screener()
            s.set_default_body("day_gainers")
            resp = s.response
            quotes = []
            if isinstance(resp, dict):
                fin = resp.get("finance", {})
                results = fin.get("result", [])
                if results:
                    quotes = results[0].get("quotes", [])
            return {
                "ok": len(quotes) > 0,
                "items": len(quotes),
                "tickers": [(q.get("symbol") or "").upper() for q in quotes[:10]],
            }
        except Exception as e:
            return {"ok": False, "items": 0, "note": f"{e.__class__.__name__}: {e}"}

    return await asyncio.to_thread(_do)


async def probe_apewisdom(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get("https://apewisdom.io/api/v1.0/filter/all/page/1")
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    res = r.json().get("results", [])
    return {
        "ok": len(res) > 0,
        "items": len(res),
        "tickers": [(x.get("ticker") or "").upper() for x in res[:10]],
    }


async def probe_seekingalpha(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get(
        "https://seekingalpha.com/market_currents.xml",
        follow_redirects=True,
    )
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    items = re.findall(r"<item>([\s\S]*?)</item>", r.text)
    tickers: set[str] = set()
    for it in items[:30]:
        tm = re.search(r"<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>", it)
        if tm:
            tickers.update(extract_tickers(tm.group(1)))
    return {"ok": len(items) > 0, "items": len(items), "tickers": sorted(tickers)[:10]}


async def probe_finviz() -> dict[str, Any]:
    def _do():
        try:
            from finvizfinance.screener.overview import Overview
        except ImportError as e:
            return {"ok": False, "items": 0, "note": f"finvizfinance not installed: {e}"}
        try:
            o = Overview()
            o.set_filter(signal="Top Gainers")
            df = o.screener_view(limit=10, order="Volume", verbose=0)
            if df is None or len(df) == 0:
                return {"ok": False, "items": 0, "note": "empty dataframe"}
            tickers = [str(row.get("Ticker", "")).upper() for _, row in df.iterrows()]
            return {"ok": True, "items": len(df), "tickers": tickers[:10]}
        except Exception as e:
            return {"ok": False, "items": 0, "note": f"{e.__class__.__name__}: {e}"}
    return await asyncio.to_thread(_do)


async def probe_bluesky(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
        params={"q": "stocks earnings", "limit": "25", "sort": "top"},
    )
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    posts = r.json().get("posts", [])
    tickers: set[str] = set()
    for p in posts:
        rec = p.get("record") or {}
        tickers.update(extract_tickers(rec.get("text") or ""))
    return {"ok": len(posts) > 0, "items": len(posts), "tickers": sorted(tickers)[:10]}


async def probe_hackernews(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": "stocks", "tags": "story",
                "hitsPerPage": "20", "numericFilters": "points>10"},
    )
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    hits = r.json().get("hits", [])
    tickers: set[str] = set()
    for h in hits:
        tickers.update(extract_tickers(f"{h.get('title','')} {h.get('story_text','')}"))
    return {"ok": len(hits) > 0, "items": len(hits), "tickers": sorted(tickers)[:10]}


async def probe_cnbc(cli: httpx.AsyncClient) -> dict[str, Any]:
    r = await cli.get(
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        follow_redirects=True,
    )
    if r.status_code != 200:
        return {"ok": False, "items": 0, "note": f"HTTP {r.status_code}"}
    items = re.findall(r"<item>([\s\S]*?)</item>", r.text)
    tickers: set[str] = set()
    for it in items[:30]:
        tm = re.search(r"<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>", it)
        if tm:
            tickers.update(extract_tickers(tm.group(1)))
    return {"ok": len(items) > 0, "items": len(items), "tickers": sorted(tickers)[:10]}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

PROBES_NEEDING_CLIENT = [
    ("reddit", probe_reddit),
    ("stocktwits", probe_stocktwits),
    ("apewisdom", probe_apewisdom),
    ("seekingalpha", probe_seekingalpha),
    ("bluesky", probe_bluesky),
    ("hackernews", probe_hackernews),
    ("cnbc", probe_cnbc),
]
PROBES_STANDALONE = [
    ("yahoo", probe_yahoo),
    ("finviz", probe_finviz),
]


async def main() -> int:
    print("Probing trader sources in parallel — total time should be < 30s.\n")
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as cli:
        async def _wrap(name, fn, needs_client):
            t = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    fn(cli) if needs_client else fn(),
                    timeout=25,
                )
                ms = int((time.monotonic() - t) * 1000)
                return {"source": name, "ms": ms, **result}
            except asyncio.TimeoutError:
                return {"source": name, "ok": False, "items": 0,
                        "ms": int((time.monotonic() - t) * 1000),
                        "note": "timeout > 25s"}
            except Exception as e:
                return {"source": name, "ok": False, "items": 0,
                        "ms": int((time.monotonic() - t) * 1000),
                        "note": f"{e.__class__.__name__}: {e}"}

        tasks = [_wrap(n, f, True) for n, f in PROBES_NEEDING_CLIENT]
        tasks += [_wrap(n, f, False) for n, f in PROBES_STANDALONE]
        results = await asyncio.gather(*tasks)

    total_ms = int((time.monotonic() - t0) * 1000)
    print(f"{'SOURCE':<14} {'OK':<4} {'ITEMS':>6} {'MS':>6}  TICKERS / NOTE")
    print("-" * 78)
    for r in sorted(results, key=lambda x: x["source"]):
        ok = "✓" if r.get("ok") else "✗"
        items = r.get("items", 0)
        ms = r.get("ms", 0)
        tail = ""
        if r.get("ok") and r.get("tickers"):
            tail = ", ".join(r["tickers"])
        elif r.get("note"):
            tail = r["note"]
        print(f"{r['source']:<14} {ok:<4} {items:>6} {ms:>6}  {tail}")
    print("-" * 78)
    available = [r["source"] for r in results if r.get("ok") and r.get("items", 0) > 0]
    print(f"\nTotal probe time: {total_ms} ms")
    print(f"Working sources ({len(available)}): {', '.join(available) or '(none)'}")
    print()
    print("Paste this whole block back to me.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
