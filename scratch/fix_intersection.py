import re

with open("server.py", "r") as f:
    code = f.read()

old_logic = '''    # Intersect all data together
    if source_tickers:
        intersected = set.intersection(*source_tickers.values())
        all_items = [item for item in all_items if item.get("ticker", "").upper() in intersected]'''

new_logic = '''    # Intersect all data together: require corroboration from multiple sources
    if source_tickers:
        # Instead of requiring a ticker to be in ALL 5 sources (which is too strict and returns empty),
        # we intersect by requiring it to be present in at least 2 distinct sources.
        from collections import Counter
        ticker_counts = Counter()
        for source_set in source_tickers.values():
            ticker_counts.update(source_set)
            
        intersected = {t for t, c in ticker_counts.items() if c >= 2}
        all_items = [item for item in all_items if item.get("ticker", "").upper() in intersected]'''

code = code.replace(old_logic, new_logic)

with open("server.py", "w") as f:
    f.write(code)
