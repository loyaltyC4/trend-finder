#!/usr/bin/env python3
"""
trend_finder.py – 2025‑07‑16 rev‑J (syntax fix)

This version corrects a syntax error where `items = resp.json()...` was invalid. It now properly extracts `items`.
Features
--------
* Flexible eBay App‑ID: CLI flag, env var, or prompt.
* Demo mode (`--demo`) for Trends only.
* Google Trends fallback: `daily_trends` → `trending_searches`.
* Filters: price ≤ $25, margin ≥ 30%, listings ≤ 250.
* CSV output + console preview.
* Unit tests (`--run-tests`).

Usage
-----
# Install deps:
#   pip install requests pytrends pandas python-dateutil

# Demo mode:
#   python3 trend_finder.py --demo

# Normal mode:
#   export EBAY_APP_ID=YourAppID
#   python3 trend_finder.py
# or:
#   python3 trend_finder.py --ebay-app-id YourAppID

# Tests:
#   python3 trend_finder.py --run-tests
"""

from __future__ import annotations
import argparse, os, sys, time, random
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from pytrends import exceptions as pytrend_exc
from pytrends.request import TrendReq
from requests.exceptions import ConnectionError as ReqConnErr, RequestException

# Config
MAX_SELL_PRICE, MIN_MARGIN = 25.0, 0.30
MAX_LISTINGS, TOP_N_TRENDS = 250, 20
MAX_RETRIES, BACKOFF_BASE_SEC = 3, 2

GOOGLE_GEOS = {"AU":"AU","UK":"GB","US":"US"}
GOOGLE_PN  = {"AU":"australia","UK":"united_kingdom","US":"united_states"}
EBAY_GLOBAL_IDS = {"AU":"EBAY-AU","UK":"EBAY-GB","US":"EBAY-US"}
EBAY_APP_ID: Optional[str] = None

# eBay App ID resolver
def get_ebay_app_id(cli_arg: Optional[str]) -> str:
    if cli_arg: return cli_arg.strip()
    env = os.getenv("EBAY_APP_ID")
    if env: return env.strip()
    try:
        val = input("Enter your eBay App ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    if val: return val
    print("ERROR: eBay App ID required. Provide via flag, env var, or prompt.")
    sys.exit(1)

# Google Trends fetch

def fetch_daily_trends(region: str) -> List[str]:
    geo = GOOGLE_GEOS[region]; pn = GOOGLE_PN[region]
    # Try daily_trends
    try:
        tr = TrendReq(hl="en-US", tz=0)
        if hasattr(tr, 'daily_trends'):
            data = tr.daily_trends(geo=geo)
            return [
                item['title']['query'].lower()
                for day in data.get('trendingSearchesDays', [])
                for item in day.get('trendingSearches', [])
            ]
    except (AttributeError, pytrend_exc.ResponseError, ReqConnErr, RequestException) as e:
        print(f"  ! daily_trends failed for {region}, fallback: {e}")
    # Fallback
    try:
        tr = TrendReq(hl="en-US", tz=0)
        df = tr.trending_searches()
        return [kw.lower() for kw in df[0].tolist()]
    except Exception as e:
        print(f"  ! trending_searches failed for {region}: {e}")
        return []

def get_trending_keywords(region: str, top_n: int) -> List[str]:
    kws = fetch_daily_trends(region)
    seen: List[str] = []
    for kw in kws:
        if kw and kw not in seen:
            seen.append(kw)
        if len(seen) >= top_n: break
    return seen

# eBay API call

def ebay_search_stats(keyword: str, gid: str) -> Dict[str, Any]:
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    params = {
        "SECURITY-APPNAME": EBAY_APP_ID,
        "OPERATION-NAME": "findItemsAdvanced",
        "RESPONSE-DATA-FORMAT": "JSON",
        "global-id": gid,
        "keywords": keyword,
        "paginationInput.entriesPerPage": "100",
        "itemFilter(0).name": "MaxPrice",
        "itemFilter(0).value": str(MAX_SELL_PRICE),
        "itemFilter(0).paramName": "Currency",
        "itemFilter(0).paramValue": "USD",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    items = (
        resp.json()
            .get("findItemsAdvancedResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
    )
    prices = [
        float(it["sellingStatus"][0]["currentPrice"][0]["__value__"])
        for it in items if it.get("sellingStatus")
    ]
    return {"count": len(prices), "avg_price": sum(prices)/len(prices) if prices else None}

# AliExpress cost

def aliexpress_lowest_price(keyword: str) -> Optional[float]:
    ak, tid = os.getenv("ALIEXPRESS_APP_KEY"), os.getenv("ALIEXPRESS_TRACKING_ID")
    if not (ak and tid): return None
    url = f"https://gw.api.alibaba.com/openapi/param2/2/portals.open/api.listPromotionProduct/{ak}"
    params = {"fields":"productTitle,originalPrice,salePrice","keywords":keyword,"sort":"salePrice_asc","pageSize":"20","trackingId":tid}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
    except RequestException:
        return None
    data = r.json()
    if not data.get("success"): return None
    prices: List[float] = []
    for prod in data.get("result", {}).get("products", []):
        txt = prod.get("salePrice") or prod.get("originalPrice")
        if txt:
            try:
                prices.append(float(txt.replace("US $", "")))
            except ValueError:
                pass
    return min(prices) if prices else None

# Scoring

def evaluate_keyword(keyword: str, region: str) -> Optional[Dict[str, Any]]:
    stats = ebay_search_stats(keyword, EBAY_GLOBAL_IDS[region])
    avg = stats.get("avg_price")
    if not avg or avg > MAX_SELL_PRICE or stats.get("count", 0) > MAX_LISTINGS:
        return None
    cost = aliexpress_lowest_price(keyword)
    margin = (avg - cost)/avg if cost else None
    if cost is not None and margin < MIN_MARGIN:
        return None
    return {
        "keyword": keyword,
        "region": region,
        "sell_price": round(avg, 2),
        "cost_price": round(cost, 2) if cost else "n/a",
        "margin_pct": round(margin*100, 1) if margin else "n/a",
        "ebay_listings": stats.get("count", 0)
    }

# Discovery

def discover_products() -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for reg in GOOGLE_GEOS:
        print(f"Checking trends for {reg}…")
        for kw in get_trending_keywords(reg, TOP_N_TRENDS):
            try:
                res = evaluate_keyword(kw, reg)
                if res:
                    rows.append(res)
                    print(f"  ✓ {kw}({reg}) → listings {res['ebay_listings']}, margin {res['margin_pct']}")
            except Exception as e:
                print(f"  ! err {kw}: {e}", file=sys.stderr)
    return pd.DataFrame(rows)

# Tests

def _run_tests():
    import unittest
    from unittest import mock
    class T(unittest.TestCase):
        def test_cli_env(self):
            with mock.patch.dict(os.environ, {"EBAY_APP_ID": "X"}, clear=True):
                self.assertEqual(get_ebay_app_id(None), "X")
        def test_cli_flag(self):
            self.assertEqual(get_ebay_app_id("Y"), "Y")
        def test_prompt(self):
            with mock.patch("builtins.input", return_value="Z"):
                self.assertEqual(get_ebay_app_id(None), "Z")
        def test_exit(self):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("builtins.input", return_value=""):
                    with self.assertRaises(SystemExit):
                        get_ebay_app_id(None)
    unittest.main(argv=[sys.argv[0]], exit=False)

# CLI

def main():
    parser = argparse.ArgumentParser(description="Find trending low-ticket products.")
    parser.add_argument("--ebay-app-id", help="eBay App ID (overrides env)")
    parser.add_argument("--demo", action="store_true", help="Demo mode: skip marketplace calls")
    parser.add_argument("--run-tests", action="store_true", help="Run unit tests and exit")
    args = parser.parse_args()
    if args.run_tests:
        _run_tests()
        return
    if args.demo:
        for reg in GOOGLE_GEOS:
            kws = get_trending_keywords(reg, TOP_N_TRENDS)
            print(f"{reg} trends: {', '.join(kws)}")
        return
    global EBAY_APP_ID
    EBAY_APP_ID = get_ebay_app_id(args.ebay_app_id)
    df = discover_products()
    if df.empty:
        print("No products met criteria.")
        return
    df = df.sort_values(by="margin_pct", ascending=False)
    fname = f"product_candidates_{datetime.utcnow():%Y%m%d_%H%M}.csv"
    df.to_csv(fname, index=False)
    print(f"Saved {len(df)} products → {fname}")
    print(df.head(20).to_string(index=False))

if __name__ == "__main__":
    main()
