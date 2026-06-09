#!/usr/bin/env python3
"""Watchlist collector: pull from Longbridge watchlist groups → JSON → COS."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

COS_CMD = "/Users/yuhao/Library/Python/3.9/bin/coscmd"
OUTPUT_PATH = Path("/tmp/watchlist_latest.json")
COS_KEY = "watchlist/latest.json"

# Watchlist group names to include (order matters → display order)
WATCHLIST_GROUPS = ["美股自选", "港股自选", "cn"]
GROUP_DISPLAY_NAMES = {"cn": "A股"}

# ---------------------------------------------------------------------------
# Longbridge CLI helpers
# ---------------------------------------------------------------------------

def run_lb(args: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            ["longbridge"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception as e:
        log.warning("longbridge %s failed: %s", " ".join(args), e)
        return ""


def parse_json(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        results = []
        for ln in lines:
            try:
                results.append(json.loads(ln))
            except Exception:
                pass
        return results if len(results) > 1 else (results[0] if results else None)


def safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fetch watchlist groups from Longbridge
# ---------------------------------------------------------------------------

def fetch_watchlist_groups() -> dict[str, list[dict]]:
    """Returns {group_name: [{"symbol": ..., "name": ...}, ...]}"""
    raw = run_lb(["watchlist", "--lang", "zh-CN", "--format", "json"], timeout=15)
    data = parse_json(raw)
    if not data:
        return {}
    groups = data if isinstance(data, list) else data.get("groups", [])
    result: dict[str, list[dict]] = {}
    for g in groups:
        name = g.get("name", "")
        if name in WATCHLIST_GROUPS:
            securities = [
                {"symbol": s["symbol"], "name": s.get("name", s["symbol"])}
                for s in g.get("securities", [])
                if s.get("symbol") and not s["symbol"].startswith(".")
            ]
            result[name] = securities
    return result


# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

def fetch_quotes_batch(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    raw = run_lb(["quote"] + symbols + ["--format", "json"], timeout=60)
    data = parse_json(raw)
    if not data:
        return {}
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return {}
    return {item["symbol"]: item for item in data if isinstance(item, dict) and item.get("symbol")}


def change_pct_from_quote(q: dict) -> float | None:
    for key in ("last_done_chg_pct", "change_rate", "change_pct", "chg_pct", "change_percentage"):
        v = safe_float(q.get(key))
        if v is not None:
            return round(v, 2)
    last = safe_float(q.get("last_done") or q.get("last"))
    prev = safe_float(q.get("prev_close") or q.get("prev_close_price"))
    if last and prev and prev != 0:
        return round((last - prev) / prev * 100, 2)
    return None


# ---------------------------------------------------------------------------
# KLine → MA + RSI
# ---------------------------------------------------------------------------

def compute_ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        (gains if diff >= 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def ma_signal(price: float, ma5: float | None, ma20: float | None, ma60: float | None) -> str:
    if ma20 and ma60 and price > ma20 > ma60:
        return "多头排列"
    if ma20 and ma60 and price < ma20 < ma60:
        return "空头排列"
    if ma5 and ma20:
        if ma5 > ma20:
            return "短期偏强"
        return "短期偏弱"
    return "震荡"


def fetch_kline_data(symbol: str) -> dict:
    raw = run_lb(["kline", symbol, "--period", "day", "--count", "65", "--format", "json"], timeout=35)
    items = parse_json(raw)
    if not isinstance(items, list) or not items:
        return {}
    closes = [safe_float(k.get("close")) for k in items]
    closes = [c for c in closes if c is not None]
    if not closes:
        return {}
    price = closes[-1]
    ma5 = compute_ma(closes, 5)
    ma20 = compute_ma(closes, 20)
    ma60 = compute_ma(closes, 60)
    rsi = compute_rsi(closes, 14)
    rsi_signal = "超买" if rsi and rsi > 70 else ("超卖" if rsi and rsi < 30 else "")
    return {
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "ma_signal": ma_signal(price, ma5, ma20, ma60),
        "rsi14": rsi,
        "rsi_signal": rsi_signal,
    }


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def fetch_news(symbol: str) -> list[dict]:
    raw = run_lb(["news", symbol, "--count", "2", "--lang", "zh-CN", "--format", "json"], timeout=20)
    items = parse_json(raw)
    if not isinstance(items, list):
        return []
    result = []
    for it in items[:2]:
        title = it.get("title") or it.get("headline", "")
        pub_time = it.get("pub_time") or it.get("timestamp") or it.get("published_at", "")
        if title:
            result.append({"title": title, "pub_time": pub_time})
    return result


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def fmt_market_cap(val: Any) -> str | None:
    v = safe_float(val)
    if v is None:
        return None
    if v >= 1e8:
        return f"{v/1e8:.1f}亿"
    if v >= 1e4:
        return f"{v/1e4:.0f}万"
    return str(int(v))


def fetch_valuation(symbol: str) -> dict:
    raw = run_lb(["valuation", symbol, "--format", "json"], timeout=20)
    data = parse_json(raw)
    if not isinstance(data, dict):
        if isinstance(data, list) and data:
            data = data[0]
        else:
            return {}
    pe = safe_float(data.get("pe_ttm") or data.get("pe") or data.get("price_earnings_ratio"))
    pb = safe_float(data.get("pb") or data.get("price_book_ratio"))
    mc_raw = data.get("market_cap") or data.get("total_market_value")
    mc = fmt_market_cap(mc_raw)
    return {
        "pe": round(pe, 1) if pe else None,
        "pb": round(pb, 2) if pb else None,
        "market_cap": mc,
    }


# ---------------------------------------------------------------------------
# Per-stock enrichment
# ---------------------------------------------------------------------------

def enrich_stock(stock: dict, quotes: dict[str, dict]) -> dict:
    symbol = stock["symbol"]
    q = quotes.get(symbol, {})
    price = safe_float(q.get("last_done") or q.get("last"))
    chg = change_pct_from_quote(q)

    result: dict[str, Any] = {
        "symbol": symbol,
        "name": stock["name"],
        "price": round(price, 2) if price else None,
        "change_pct": chg,
    }

    # KLine (MA + RSI)
    kl = fetch_kline_data(symbol)
    result.update(kl)

    # News
    result["news"] = fetch_news(symbol)

    # Valuation
    val = fetch_valuation(symbol)
    result.update(val)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_payload() -> dict:
    log.info("Fetching watchlist groups from Longbridge...")
    groups = fetch_watchlist_groups()
    if not groups:
        log.error("No watchlist groups found")
        return {}

    all_symbols = [s["symbol"] for stocks in groups.values() for s in stocks]
    log.info("Total %d stocks across %d groups", len(all_symbols), len(groups))

    log.info("Fetching quotes batch...")
    quotes = fetch_quotes_batch(all_symbols)

    output_groups = []
    for group_name in WATCHLIST_GROUPS:
        stocks = groups.get(group_name)
        if not stocks:
            continue
        display_name = GROUP_DISPLAY_NAMES.get(group_name, group_name)
        log.info("Enriching group '%s' (%d stocks)...", display_name, len(stocks))

        enriched = []
        # Use concurrency=3 to avoid WebSocket limits
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(enrich_stock, s, quotes): s for s in stocks}
            for fut in as_completed(futures):
                try:
                    enriched.append(fut.result())
                except Exception as e:
                    log.warning("Failed to enrich %s: %s", futures[fut]["symbol"], e)

        # Sort by absolute change_pct descending
        enriched.sort(key=lambda x: abs(x.get("change_pct") or 0), reverse=True)

        output_groups.append({
            "group": display_name,
            "stocks": enriched,
        })

        # Stagger between groups to reduce connection pressure
        time.sleep(3)

    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "groups": output_groups,
    }


def main():
    payload = build_payload()
    if not payload:
        sys.exit(1)

    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info("Wrote %s (%d bytes)", OUTPUT_PATH, OUTPUT_PATH.stat().st_size)

    # Print summary
    for g in payload.get("groups", []):
        print(f"\n=== {g['group']} ({len(g['stocks'])} stocks) ===")
        for s in g["stocks"][:3]:
            chg = s.get("change_pct")
            chg_str = f"{chg:+.2f}%" if chg is not None else "—"
            print(f"  {s['symbol']} {s['name']:12s} {chg_str:>8s}  {s.get('ma_signal','—')}")

    # Upload to COS
    r = subprocess.run([COS_CMD, "upload", str(OUTPUT_PATH), COS_KEY], capture_output=True, text=True)
    if r.returncode == 0:
        log.info("Uploaded to COS: %s", COS_KEY)
    else:
        log.error("COS upload failed: %s", r.stderr)


if __name__ == "__main__":
    main()
