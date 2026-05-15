import sys
sys.path.append('.')
from server import _scan_stocktwits, _scan_apewisdom, _scan_reddit, _scan_cnbc, _scan_hackernews

print("stocktwits:", len(_scan_stocktwits()))
print("apewisdom:", len(_scan_apewisdom()))
print("reddit:", len(_scan_reddit()))
print("cnbc:", len(_scan_cnbc()))
print("hackernews:", len(_scan_hackernews()))
