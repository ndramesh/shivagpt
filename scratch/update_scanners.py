import re

with open("server.py", "r") as f:
    code = f.read()

# 1. TRADER_DISCLAIMER
code = re.sub(
    r'# Social Sentiment Scanner \(/api/trader\).*?TRADER_DISCLAIMER = \([\s\S]*?\)',
    lambda m: '''# Social Sentiment Scanner (/api/trader)
#
# Aggregates stock mentions and sentiment from Reddit (r/wallstreetbets,
# r/stocks, r/investing, r/options, r/stockmarket), Stocktwits trending,
# Apewisdom, CNBC, and Hacker News.  Extracts tickers,
# counts mentions, runs LLM sentiment analysis, cross-references with
# existing /stock technicals, and returns a scored & ranked list.
#
# NOT financial advice.  Social media sentiment can be manipulated — this
# is a research/entertainment tool only.
# ---------------------------------------------------------------------------

TRADER_DISCLAIMER = (
    "_Social sentiment aggregated from public APIs (Reddit, Stocktwits, "
    "Apewisdom, CNBC, Hacker News). NOT financial advice. Social-media sentiment can be "
    "manipulated. Always do your own research._"
)''',
    code,
    flags=re.MULTILINE
)

# 2. Replace the Scanners block (from _scan_reddit to the start of _llm_sentiment)
scanners_code = '''
import concurrent.futures

def _scan_reddit(subreddits: list[str] | None = None, limit: int = 50) -> list[dict]:
    subs = subreddits or _REDDIT_SUBS
    headers = {"User-Agent": "ShivaGPT/1.0 (social sentiment scanner)"}
    def _one_sub(sub: str) -> list[dict]:
        items = []
        try:
            with httpx.Client(timeout=15.0, headers=headers) as cli:
                r = cli.get(f"https://www.reddit.com/r/{sub}/hot.json", params={"limit": str(min(limit, 100)), "raw_json": "1"})
                if r.status_code != 200: return items
                data = r.json()
                for p in data.get("data", {}).get("children", []):
                    d = p.get("data", {})
                    title = d.get("title", "")
                    selftext = (d.get("selftext") or "")[:500]
                    tickers = _extract_tickers(f"{title} {selftext}")
                    for ticker in tickers:
                        items.append({
                            "ticker": ticker, "title": title[:200], "score": d.get("score", 0),
                            "comments": d.get("num_comments", 0), "sub": sub,
                            "url": f"https://reddit.com{d.get('permalink', '')}", "text": selftext[:300],
                            "source": "reddit", "created": d.get("created_utc", 0),
                        })
        except Exception: pass
        return items

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(subs))) as exe:
        for items in exe.map(_one_sub, subs):
            results.extend(items)
    return results


def _scan_stocktwits() -> list[dict]:
    results = []
    try:
        with httpx.Client(timeout=15.0) as cli:
            r = cli.get("https://api.stocktwits.com/api/2/trending/symbols.json")
            if r.status_code == 200:
                for s in r.json().get("symbols", []):
                    ticker = s.get("symbol", "").upper()
                    if ticker and ticker not in _TICKER_BLACKLIST:
                        results.append({
                            "ticker": ticker, "title": s.get("title", ticker),
                            "watchlist_count": s.get("watchlist_count", 0), "source": "stocktwits",
                        })
    except Exception: pass
    return results


def _scan_apewisdom() -> list[dict]:
    items = []
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": "ShivaGPT/1.0 (social sentiment scanner)"}) as cli:
            r = cli.get("https://apewisdom.io/api/v1.0/filter/all/page/1")
            if r.status_code == 200:
                for entry in (r.json().get("results") or [])[:30]:
                    ticker = (entry.get("ticker") or "").upper().strip()
                    if not ticker or ticker in _TICKER_BLACKLIST: continue
                    mentions = int(entry.get("mentions", 0) or 0)
                    rank = entry.get("rank")
                    rank_24h = entry.get("rank_24h_ago")
                    signal = None
                    if rank and rank_24h and rank_24h > rank: signal = "rising"
                    elif rank and rank_24h and rank_24h < rank: signal = "falling"
                    items.append({
                        "ticker": ticker, "title": entry.get("name") or ticker,
                        "score": mentions, "ape_mentions": mentions,
                        "ape_sentiment": entry.get("sentiment_score"),
                        "rank": rank, "rank_24h_ago": rank_24h, "source": "apewisdom", "signal": signal,
                    })
    except Exception: pass
    return items


def _scan_hackernews() -> list[dict]:
    queries = ["stocks", "earnings", "IPO", "stock market"]
    headers = {"User-Agent": "ShivaGPT/1.0 (social sentiment scanner)"}
    def _one(q: str) -> list[dict]:
        local = []
        try:
            with httpx.Client(timeout=15.0, headers=headers) as cli:
                r = cli.get("https://hn.algolia.com/api/v1/search_by_date", params={"query": q, "tags": "story", "hitsPerPage": "20", "numericFilters": "points>10"})
                if r.status_code == 200:
                    for hit in (r.json().get("hits") or []):
                        title = hit.get("title") or ""
                        body = (hit.get("story_text") or "")[:500]
                        tickers = _extract_tickers(f"{title} {body}")
                        obj_id = hit.get("objectID", "")
                        url = f"https://news.ycombinator.com/item?id={obj_id}" if obj_id else ""
                        for ticker in tickers:
                            local.append({
                                "ticker": ticker, "title": title[:200], "text": body,
                                "score": int(hit.get("points", 0) or 0),
                                "comments": int(hit.get("num_comments", 0) or 0),
                                "sub": "hackernews", "url": url, "source": "hackernews",
                            })
        except Exception: pass
        return local

    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as exe:
        for local in exe.map(_one, queries):
            items.extend(local)
    return items


def _scan_cnbc() -> list[dict]:
    feeds = {
        "cnbc_markets": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
        "cnbc_earnings": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
        "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    }
    headers = {"User-Agent": "ShivaGPT/1.0 (social sentiment scanner)"}
    def _one(args) -> list[dict]:
        label, url = args
        local = []
        try:
            with httpx.Client(timeout=15.0, headers=headers, follow_redirects=True) as cli:
                r = cli.get(url)
                if r.status_code == 200:
                    for m in re.finditer(r"<item>([\\s\\S]*?)</item>", r.text):
                        inner = m.group(1)
                        tm = re.search(r"<title>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?</title>", inner)
                        lm = re.search(r"<link>([\\s\\S]*?)</link>", inner)
                        dm = re.search(r"<description>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?</description>", inner)
                        if not tm: continue
                        title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
                        desc = re.sub(r"<[^>]+>", "", (dm.group(1) if dm else "")).strip()[:400]
                        tickers = _extract_tickers(f"{title} {desc}")
                        for ticker in tickers:
                            local.append({
                                "ticker": ticker, "title": title[:200], "text": desc,
                                "url": (lm.group(1).strip() if lm else ""), "score": 5,
                                "source": "cnbc", "sub": label.replace("cnbc_", "cnbc/"),
                            })
        except Exception: pass
        return local

    items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(feeds)) as exe:
        for local in exe.map(_one, feeds.items()):
            items.extend(local)
    return items

async def _llm_sentiment'''

code = re.sub(r'async def _scan_reddit.*?async def _llm_sentiment', lambda m: scanners_code, code, flags=re.MULTILINE | re.DOTALL)


# 3. Replace _run_trader_scan body
run_trader_scan_pattern = r'async def _run_trader_scan\(body: dict\) -> dict\[str, Any\]:([\s\S]*?)    # ------------------------------------------------------------------\n    # 3. Pre-sort'

new_run_trader_scan = '''
    focus = (body.get("focus") or "").strip().lower()
    sector = (body.get("sector") or "").strip().lower()
    single_ticker = (body.get("ticker") or "").strip().upper()
    limit = max(1, min(int(body.get("limit") or 20), 50))
    enabled_sources = (body.get("sources")
                       or ["reddit", "stocktwits", "apewisdom", "cnbc", "hackernews"])
    sentiment_model = (body.get("sentiment_model")
                       or TRADER_SENTIMENT_MODEL)

    t0 = time.monotonic()
    log.info("trader: scan starting focus=%r sector=%r ticker=%r "
             "sources=%s", focus, sector, single_ticker, enabled_sources)

    # ------------------------------------------------------------------
    # 1. Collect data from all sources in parallel using ThreadPoolExecutor
    # ------------------------------------------------------------------
    tasks = {}
    if "reddit" in enabled_sources: tasks["reddit"] = _scan_reddit
    if "stocktwits" in enabled_sources: tasks["stocktwits"] = _scan_stocktwits
    if "apewisdom" in enabled_sources: tasks["apewisdom"] = _scan_apewisdom
    if "hackernews" in enabled_sources or "hn" in enabled_sources: tasks["hackernews"] = _scan_hackernews
    if "cnbc" in enabled_sources: tasks["cnbc"] = _scan_cnbc

    source_names = list(tasks.keys())
    
    def _run_all():
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as exe:
            futures = {name: exe.submit(fn) for name, fn in tasks.items()}
            for name in source_names:
                try:
                    results.append(futures[name].result())
                except Exception as e:
                    results.append(e)
        return results

    raw_results = await asyncio.to_thread(_run_all)

    all_items: list[dict] = []
    sources_ok: list[str] = []
    source_tickers = {}
    for name, result in zip(source_names, raw_results):
        if isinstance(result, Exception):
            log.warning("trader: %s failed: %s", name, result)
        elif result:
            all_items.extend(result)
            sources_ok.append(name)
            source_tickers[name] = {item.get("ticker", "").upper() for item in result if item.get("ticker")}
            log.info("trader: %s returned %d items", name, len(result))

    if not all_items:
        return {
            "tickers": [],
            "sources": sources_ok,
            "scan_seconds": round(time.monotonic() - t0, 2),
            "disclaimer": TRADER_DISCLAIMER,
            "error": "No data returned from any source.",
        }

    # Intersect all data together
    if source_tickers:
        intersected = set.intersection(*source_tickers.values())
        all_items = [item for item in all_items if item.get("ticker", "").upper() in intersected]
        
    if not all_items:
        return {
            "tickers": [],
            "sources": sources_ok,
            "scan_seconds": round(time.monotonic() - t0, 2),
            "disclaimer": TRADER_DISCLAIMER,
            "error": "No tickers found that appear in all active sources (empty intersection).",
        }

    # ------------------------------------------------------------------
    # 2. Aggregate by ticker
    # ------------------------------------------------------------------
    from collections import defaultdict
    ticker_agg: dict[str, dict] = defaultdict(lambda: {
        "ticker": "", "mention_count": 0, "total_score": 0,
        "total_comments": 0, "sources": set(), "posts": [],
        "price": None, "change_pct": None, "vol_ratio": None,
        "volume": None, "avg_volume": None, "market_cap": None,
        "signals": [],
    })

    for item in all_items:
        ticker = item.get("ticker", "").upper()
        if not ticker or len(ticker) < 2:
            continue
        if single_ticker and ticker != single_ticker:
            continue

        agg = ticker_agg[ticker]
        agg["ticker"] = ticker
        agg["mention_count"] += 1
        agg["sources"].add(item.get("source", "?"))
        agg["total_score"] += item.get("score", 0)
        agg["total_comments"] += item.get("comments", 0)

        if len(agg["posts"]) < 20:
            agg["posts"].append(item)

        # Merge price/volume data (first-write wins)
        if item.get("price") and agg["price"] is None:
            agg["price"] = item["price"]
        if item.get("change_pct") is not None and agg["change_pct"] is None:
            try:
                v = item["change_pct"]
                if isinstance(v, str):
                    v = float(v.replace("%", "").replace("+", ""))
                agg["change_pct"] = float(v)
            except (ValueError, TypeError):
                pass
        if item.get("vol_ratio") and (
                agg["vol_ratio"] is None
                or item["vol_ratio"] > agg["vol_ratio"]):
            agg["vol_ratio"] = item["vol_ratio"]
        if item.get("volume"):
            agg["volume"] = item["volume"]
        if item.get("avg_volume"):
            agg["avg_volume"] = item["avg_volume"]
        if item.get("market_cap"):
            agg["market_cap"] = item["market_cap"]
        if item.get("signal"):
            agg["signals"].append(item["signal"])

    if not ticker_agg:
        return {
            "tickers": [], "sources": sources_ok,
            "total_mentions": len(all_items),
            "scan_seconds": round(time.monotonic() - t0, 2),
            "disclaimer": TRADER_DISCLAIMER,
        }

    # ------------------------------------------------------------------
    # 3. Pre-sort'''

code = re.sub(run_trader_scan_pattern, lambda m: r'async def _run_trader_scan(body: dict) -> dict[str, Any]:' + new_run_trader_scan, code)

# 4. TRADER_SCANNERS
trader_scanners_pattern = r'TRADER_SCANNERS = \{[\s\S]*?\}'
new_trader_scanners = '''TRADER_SCANNERS = {
    "reddit":       _scan_reddit,
    "stocktwits":   _scan_stocktwits,
    "apewisdom":    _scan_apewisdom,
    "hackernews":   _scan_hackernews,
    "cnbc":         _scan_cnbc,
}'''
code = re.sub(trader_scanners_pattern, lambda m: new_trader_scanners, code)

# 5. trader_probe logic
probe_pattern = r'items = await asyncio\.wait_for\(fn\(\), timeout=30\)'
new_probe = r'items = await asyncio.wait_for(asyncio.to_thread(fn), timeout=30)'
code = re.sub(probe_pattern, new_probe, code)

with open("server.py", "w") as f:
    f.write(code)
