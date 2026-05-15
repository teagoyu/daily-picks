#!/usr/bin/env python3
"""
Pre-market briefing collector: Longbridge CLI → JSON → Tencent COS (briefing/latest.json).

Usage:
    cd /Users/yuhao/trade/daily-picks && python briefing_collector.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("briefing_collector")

LONGBRIDGE = "/opt/homebrew/bin/longbridge"
COSCMD = "/Users/yuhao/Library/Python/3.9/bin/coscmd"
COS_REMOTE = "briefing/latest.json"
TMP_OUT = Path("/tmp/briefing_latest.json")

US_TECH_SYMBOLS = [
    "NVDA.US",
    "TSLA.US",
    "META.US",
    "AAPL.US",
    "MSFT.US",
    "PLTR.US",
    "AMZN.US",
]

HK_SYMBOLS = [
    "700.HK",
    "9988.HK",
    "1810.HK",
    "9992.HK",
    "1211.HK",
    "3690.HK",
    # 半导体
    "981.HK",    # 中芯国际
    "2382.HK",   # 舜宇光学
    "522.HK",    # ASM Pacific
    "6963.HK",   # 中微公司
]

CN_SYMBOLS = [
    "600519.SH",
    "300750.SZ",
    "601318.SH",
    "002594.SZ",
    "300059.SZ",
]

# Symbols used for technical analysis (all markets)
TECH_SYMBOLS = US_TECH_SYMBOLS + HK_SYMBOLS + CN_SYMBOLS

# Symbols used for news collection
NEWS_SYMBOLS = US_TECH_SYMBOLS + HK_SYMBOLS[:3]

SYMBOL_NAMES: dict[str, str] = {
    # US
    "NVDA.US": "英伟达",
    "TSLA.US": "特斯拉",
    "META.US": "Meta",
    "AAPL.US": "苹果",
    "MSFT.US": "微软",
    "PLTR.US": "Palantir",
    "AMZN.US": "亚马逊",
    # HK
    "700.HK": "腾讯控股",
    "9988.HK": "阿里巴巴",
    "1810.HK": "小米集团",
    "9992.HK": "泡泡玛特",
    "1211.HK": "比亚迪",
    "3690.HK": "美团",
    "981.HK": "中芯国际",
    "2382.HK": "舜宇光学",
    "522.HK": "ASM Pacific",
    "6963.HK": "中微公司",
    # CN
    "600519.SH": "贵州茅台",
    "300750.SZ": "宁德时代",
    "601318.SH": "中国平安",
    "002594.SZ": "比亚迪A",
    "300059.SZ": "东方财富",
}

CONCURRENCY = 6


def run_lb(args: list[str], timeout: int = 45) -> str | None:
    cmd = [LONGBRIDGE] + args
    if "--format" not in args:
        cmd.extend(["--format", "json"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.warning("longbridge %s failed: %s", " ".join(args[:4]), (r.stderr or "").strip()[:240])
            return None
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("longbridge %s timed out", " ".join(args[:4]))
        return None
    except Exception as e:
        log.warning("longbridge %s error: %s", " ".join(args[:4]), e)
        return None


def parse_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("JSON decode failed for snippet: %s", raw[:120])
        return None


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _translate_temp_label(score: int | None) -> str:
    if score is None:
        return "—"
    if score >= 80:
        return "极度贪婪"
    if score >= 65:
        return "偏多贪婪"
    if score >= 50:
        return "温和偏多"
    if score >= 35:
        return "中性震荡"
    if score >= 20:
        return "偏空恐慌"
    return "极度恐慌"


def fetch_market_temp() -> dict[str, Any]:
    raw = run_lb(["market-temp"], timeout=20)
    data = parse_json(raw)
    default: dict[str, Any] = {"score": None, "label": "—"}

    if data is None:
        return default

    score_i: int | None = None

    if isinstance(data, dict):
        score = data.get("score") or data.get("temperature") or data.get("value")
        try:
            score_i = int(round(float(score))) if score is not None else None
        except (TypeError, ValueError):
            score_i = None

    elif isinstance(data, list):
        fields: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            key = str(item.get("field", "")).lower().strip()
            val = item.get("value")
            fields[key] = "" if val is None else str(val).strip()
        score_raw = fields.get("temperature") or fields.get("score")
        try:
            score_i = int(round(float(score_raw))) if score_raw else None
        except (TypeError, ValueError):
            score_i = None

    return {"score": score_i, "label": _translate_temp_label(score_i)}


def fetch_news_symbol(symbol: str, limit: int = 3) -> list[dict[str, Any]]:
    raw = run_lb(["news", symbol, "--count", str(limit), "--lang", "zh-CN"], timeout=30)
    items = parse_json(raw)
    if not items:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or it.get("headline") or "").strip()
        if not title:
            continue
        nid = str(it.get("id") or it.get("news_id") or it.get("article_id") or "")
        pub = it.get("published_at") or it.get("published_time") or it.get("time") or ""
        src = str(it.get("source") or it.get("publisher") or it.get("provider") or "")
        url = it.get("url") or it.get("link") or ""
        out.append({
            "id": nid or title[:48],
            "title": title,
            "symbol": symbol,
            "published_at": str(pub) if pub else "",
            "source": src,
            "url": str(url) if url else "",
        })
    return out


def collect_hot_news() -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(fetch_news_symbol, sym): sym for sym in NEWS_SYMBOLS}
        for fut in as_completed(futures):
            try:
                all_rows.extend(fut.result())
            except Exception as e:
                log.warning("news worker error: %s", e)

    seen: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        key = row["title"].strip().lower()
        if key not in seen:
            seen[key] = row

    def sort_key(r: dict[str, Any]) -> float:
        t = r.get("published_at") or ""
        try:
            ts = str(t).replace(" ", "T")
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts)
            return dt.timestamp()
        except Exception:
            return 0.0

    merged = list(seen.values())
    merged.sort(key=sort_key, reverse=True)
    return merged[:20]


def _parse_kv_list(data_kv: list) -> dict[str, str]:
    """Extract type→value mapping from Longbridge data_kv array."""
    out: dict[str, str] = {}
    for item in data_kv:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        v = str(item.get("value") or "").strip()
        if t:
            out[t] = v
    return out


def _counter_id_to_symbol(counter_id: str) -> str:
    """Convert 'ST/US/HD' → 'HD.US', 'ST/HK/700' → '700.HK'."""
    parts = counter_id.strip().split("/")
    if len(parts) >= 3:
        market = parts[1].upper()
        code = parts[2]
        return f"{code}.{market}"
    return counter_id


def _extract_date_from_raw(date_str: str) -> str:
    """Extract YYYY-MM-DD from strings like '2026.05.14 (美东)' or '2026-05-14'."""
    import re
    m = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", date_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return date_str[:10]


def _time_from_raw(date_str: str) -> str:
    """Extract 盘前/盘后 from raw date string like '2026.05.14 (美东)  盘前'."""
    if "盘前" in date_str:
        return "盘前"
    if "盘后" in date_str:
        return "盘后"
    return "—"


def fetch_finance_calendar(start: str, end: str) -> list[dict[str, Any]]:
    """Fetch and flatten finance-calendar report entries into normalized rows."""
    raw = run_lb(
        ["finance-calendar", "report", "--market", "US",
         "--start", start, "--end", end, "--count", "50"],
        timeout=45,
    )
    data = parse_json(raw)
    if data is None:
        return []

    results: list[dict[str, Any]] = []

    # Top-level structure: {"date": "...", "list": [{"count": N, "date": "...", "infos": [...]}]}
    if isinstance(data, dict):
        day_list = data.get("list") or []
        for day_group in day_list:
            if not isinstance(day_group, dict):
                continue
            infos = day_group.get("infos") or []
            for info in infos:
                if not isinstance(info, dict):
                    continue
                kv = _parse_kv_list(info.get("data_kv") or [])
                eps_est = kv.get("estimate_eps", "")
                rev_est = kv.get("estimate_revenue", "")
                # skip rows with no EPS estimate
                if not eps_est or eps_est in ("--", "—", "待公布", ""):
                    continue
                raw_date = str(info.get("date") or "")
                counter_id = str(info.get("counter_id") or "")
                symbol = _counter_id_to_symbol(counter_id) if counter_id else ""
                results.append({
                    "symbol": symbol,
                    "name": str(info.get("counter_name") or "").strip(),
                    "date_str": _extract_date_from_raw(raw_date),
                    "time": _time_from_raw(raw_date),
                    "eps_est": eps_est,
                    "rev_est": rev_est,
                    "content": str(info.get("content") or ""),
                })
        return results

    # Fallback: flat list
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    return []


def split_earnings(rows: list[dict[str, Any]], today: date, tomorrow: date) -> tuple[list[dict], list[dict]]:
    today_s = today.isoformat()
    tomorrow_s = tomorrow.isoformat()
    today_list: list[dict[str, Any]] = []
    tomorrow_list: list[dict[str, Any]] = []

    for row in rows:
        ds = row.get("date_str") or ""
        sym = row.get("symbol") or ""
        if not sym:
            continue
        entry = {
            "symbol": sym,
            "name": row.get("name") or SYMBOL_NAMES.get(sym, ""),
            "eps_est": row.get("eps_est", ""),
            "rev_est": row.get("rev_est", ""),
            "time": row.get("time", "—"),
            "content": row.get("content", ""),
        }
        if ds == today_s:
            today_list.append(entry)
        elif ds == tomorrow_s:
            tomorrow_list.append(entry)

    return today_list, tomorrow_list


def fetch_kline(symbol: str, count: int = 60) -> list[dict]:
    raw = run_lb(["kline", symbol, "--period", "day", "--count", str(count)], timeout=35)
    items = parse_json(raw)
    if not items or not isinstance(items, list):
        return []
    return items


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def ma_last(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def classify_signal(ma5: float | None, ma20: float | None, ma60: float | None) -> tuple[str, str]:
    if ma5 is None or ma20 is None or ma60 is None:
        return "震荡", "neutral"
    if ma5 > ma20 > ma60:
        return "多头排列", "bull"
    if ma5 < ma20 < ma60:
        return "空头排列", "bear"
    return "震荡", "neutral"


def fetch_quote_one(symbol: str) -> dict[str, Any] | None:
    raw = run_lb(["quote", symbol], timeout=25)
    data = parse_json(raw)
    if not data:
        return None
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("symbol") == symbol:
                return it
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def technical_for_symbol(symbol: str) -> dict[str, Any] | None:
    q = fetch_quote_one(symbol)
    klines = fetch_kline(symbol, 60)
    if not klines:
        log.warning("No kline for %s", symbol)
        return None

    closes_f = [safe_float(k.get("close")) for k in klines]
    closes = [c for c in closes_f if c is not None]
    if len(closes) < 20:
        log.warning("Insufficient closes for %s", symbol)
        return None

    ma5 = ma_last(closes, 5)
    ma20 = ma_last(closes, 20)
    ma60 = ma_last(closes, 60)
    rsi = calc_rsi(closes, 14)
    sig_txt, sig_cls = classify_signal(ma5, ma20, ma60)

    last = safe_float(q.get("last")) if q else None
    prev = safe_float(q.get("prev_close")) if q else None
    chg_rate = safe_float(q.get("change_rate")) if q else None
    if chg_rate is None and last is not None and prev:
        chg_rate = round((last - prev) / prev * 100, 2)

    name = SYMBOL_NAMES.get(symbol, "")
    return {
        "symbol": symbol,
        "name": name,
        "price": round(last, 2) if last is not None else None,
        "change_rate": chg_rate,
        "ma5": round(ma5, 2) if ma5 is not None else None,
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma60": round(ma60, 2) if ma60 is not None else None,
        "rsi": rsi,
        "signal": sig_txt,
        "signal_class": sig_cls,
    }


def collect_technicals() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def _job(sym: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return sym, technical_for_symbol(sym)
        except Exception as e:
            log.warning("technical %s: %s", sym, e)
            return sym, None

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(_job, s) for s in TECH_SYMBOLS]
        for fut in as_completed(futures):
            sym, row = fut.result()
            if row:
                out[sym] = row
    return out


def upload_cos(local_path: Path) -> bool:
    try:
        r = subprocess.run(
            [COSCMD, "upload", str(local_path), COS_REMOTE],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            log.error("coscmd upload failed: %s", (r.stderr or r.stdout or "").strip()[:400])
            return False
        log.info("Uploaded to COS: %s", COS_REMOTE)
        return True
    except Exception as e:
        log.error("coscmd error: %s", e)
        return False


def build_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    today = date.today()
    tomorrow = today + timedelta(days=1)
    start_s = today.isoformat()
    end_s = tomorrow.isoformat()

    log.info("Market temperature...")
    market_temp = fetch_market_temp()

    log.info("Hot news (%d symbols)...", len(NEWS_SYMBOLS))
    hot_news = collect_hot_news()

    log.info("Finance calendar %s .. %s", start_s, end_s)
    fc_rows = fetch_finance_calendar(start_s, end_s)
    earnings_today, earnings_tomorrow = split_earnings(fc_rows, today, tomorrow)

    log.info("Technicals (%d symbols)...", len(US_TECH_SYMBOLS))
    technicals = collect_technicals()

    # Group technicals by market
    tech_by_market: dict[str, dict] = {"US": {}, "HK": {}, "CN": {}}
    for sym, data in technicals.items():
        if sym.endswith(".US"):
            tech_by_market["US"][sym] = data
        elif sym.endswith(".HK"):
            tech_by_market["HK"][sym] = data
        elif sym.endswith(".SH") or sym.endswith(".SZ"):
            tech_by_market["CN"][sym] = data

    return {
        "generated_at": generated_at,
        "market_temp": market_temp,
        "hot_news": hot_news,
        "earnings_today": earnings_today,
        "earnings_tomorrow": earnings_tomorrow,
        "technicals": technicals,
        "tech_by_market": tech_by_market,
    }


def main() -> int:
    payload = build_payload()
    TMP_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d bytes)", TMP_OUT, TMP_OUT.stat().st_size)

    preview = {
        "generated_at": payload["generated_at"],
        "market_temp": payload["market_temp"],
        "hot_news_count": len(payload["hot_news"]),
        "earnings_today": len(payload["earnings_today"]),
        "earnings_tomorrow": len(payload["earnings_tomorrow"]),
        "technicals_count": len(payload["technicals"]),
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))

    if not upload_cos(TMP_OUT):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
