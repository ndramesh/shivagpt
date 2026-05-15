import sys

with open("server.py", "r") as f:
    code = f.read()

# Add 'score' to Stocktwits output so it counts towards engagement
old_stocktwits = '''                        results.append({
                            "ticker": ticker, "title": s.get("title", ticker),
                            "watchlist_count": s.get("watchlist_count", 0), "source": "stocktwits",
                        })'''
new_stocktwits = '''                        results.append({
                            "ticker": ticker, "title": s.get("title", ticker),
                            "score": s.get("watchlist_count", 0),
                            "watchlist_count": s.get("watchlist_count", 0), "source": "stocktwits",
                        })'''
code = code.replace(old_stocktwits, new_stocktwits)

# Upgrade the sorting logic to use a smart composite rank instead of just mentions
old_sort = '''    # ------------------------------------------------------------------
    # 3. Pre-sort and pick top N candidates by mention count
    # ------------------------------------------------------------------
    candidates = sorted(
        ticker_agg.values(),
        key=lambda x: x["mention_count"], reverse=True,
    )[:limit]'''

new_sort = '''    # ------------------------------------------------------------------
    # 3. Pre-sort and pick top N candidates by composite score
    # ------------------------------------------------------------------
    # Calculate a composite ranking score:
    # Engagement (total_score) acts as the base, multiplied by the number of 
    # distinct corroborating platforms (sources) to heavily reward cross-platform virality,
    # with a baseline boost for the raw mention count to break ties.
    for x in ticker_agg.values():
        x["composite_rank"] = (x["total_score"] + (x["mention_count"] * 50)) * len(x["sources"])

    candidates = sorted(
        ticker_agg.values(),
        key=lambda x: x["composite_rank"], reverse=True,
    )[:limit]'''
code = code.replace(old_sort, new_sort)

with open("server.py", "w") as f:
    f.write(code)
