#!/usr/bin/env python3
"""A-share closing summary collector: Longbridge CLI -> JSON -> Tencent COS."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("closing_summary")

LONGBRIDGE = "/opt/homebrew/bin/longbridge"
COSCMD = "/Users/yuhao/Library/Python/3.9/bin/coscmd"
COS_REMOTE = "closing/latest.json"
TMP_OUT = Path("/tmp/closing_latest.json")

INDEX_SYMBOLS = ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"]
INDEX_NAMES = {"000001.SH": "上证指数", "399001.SZ": "深成指", "399006.SZ": "创业板", "000300.SH": "沪深300"}

SECTOR_STOCKS = {
    "AI/科技": ["600519.SH", "300750.SZ", "000977.SZ", "002594.SZ"],
    "半导体": ["688981.SH", "603986.SH", "300529.SZ", "688012.SH"],
    "新能源": ["601012.SH", "002594.SZ", "300750.SZ", "688599.SH"],
    "机器人": ["300496.SZ", "688169.SH", "002920.SZ", "300024.SZ"],
    "医药/CXO": ["603259.SH", "300122.SZ", "600276.SH", "002032.SZ"],
    "消费": ["600519.SH", "000858.SZ", "002304.SZ", "603288.SH"],
    "金融": ["601318.SH", "600036.SH", "000001.SZ", "600030.SH"],
    "军工": ["600760.SH", "002985.SZ", "688005.SH", "002402.SZ"],
}

TOP_CN_STOCKS = [
    "600519.SH", "300750.SZ", "601318.SH", "000858.SZ", "603259.SH",
    "601012.SH", "300059.SZ", "300570.SZ", "002475.SZ", "601985.SH",
    "000977.SZ", "002594.SZ", "601888.SH", "600036.SH", "300274.SZ",
    "002371.SZ", "300253.SZ", "688981.SH", "601127.SH", "002049.SZ",
]

SYMBOL_NAMES = {
    "600519.SH": "贵州茅台", "300750.SZ": "宁德时代", "601318.SH": "中国平安",
    "000858.SZ": "五粮液", "603259.SH": "药明康德", "601012.SH": "隆基绿能",
    "300059.SZ": "东方财富", "300570.SZ": "太辰光", "002475.SZ": "立讯精密",
    "601985.SH": "中国核电", "000977.SZ": "浪潮信息", "002594.SZ": "比亚迪",
    "601888.SH": "中国中免", "600036.SH": "招商银行", "300274.SZ": "阳光电源",
    "002371.SZ": "北方华创", "300253.SZ": "卫宁健康", "688981.SH": "中芯国际",
    "601127.SH": "赛力斯", "002049.SZ": "紫光国微", "603986.SH": "兆易创新",
    "300529.SZ": "健帆生物", "688012.SH": "中微公司", "300496.SZ": "中科创达",
    "688169.SH": "石头科技", "002920.SZ": "德赛西威", "300024.SZ": "机器人",
    "300122.SZ": "智飞生物", "600276.SH": "恒瑞医药", "002032.SZ": "苏泊尔",
    "002304.SZ": "洋河股份", "603288.SH": "海天味业", "000001.SZ": "平安银行",
    "600030.SH": "中信证券", "600760.SH": "中航沈飞", "002985.SZ": "北摩高科",
    "688005.SH": "容百科技", "002402.SZ": "和而泰", "688599.SH": "天合光能",
}

SYMBOL_TO_SECTOR = {}
for sector, syms in SECTOR_STOCKS.items():
    for s in syms:
        SYMBOL_TO_SECTOR.setdefault(s, sector)

CONCURRENCY = 8


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
        return None


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def change_pct_from_quote(q: dict) -> float | None:
    for key in ("change_rate", "change_percentage", "change_pct"):
        v = safe_float(q.get(key))
        if v is not None:
            return round(v, 2)
    last = safe_float(q.get("last"))
    prev = safe_float(q.get("prev_close"))
    if last is not None and prev:
        return round((last - prev) / prev * 100, 2)
    return None


def fmt_yi(val: float | None) -> str:
    if val is None:
        return "—"
    yi = val / 1e8
    return f"{yi:.0f}亿" if abs(yi) >= 100 else f"{yi:.2f}亿"


def fmt_yi_signed(val: float | None) -> str:
    if val is None:
        return "—"
    yi = val / 1e8
    return f"{'+' if yi >= 0 else ''}{yi:.2f}亿"


def sector_class(chg: float) -> str:
    if chg >= 1.0:
        return "bull"
    if chg >= 0:
        return "bull-light"
    if chg >= -1.0:
        return "neutral"
    return "bear"


def tone_class(tone: str) -> str:
    if "多头" in tone:
        return "bull"
    if "下跌" in tone:
        return "bear"
    return "neutral"


def fetch_quotes_batch(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    raw = run_lb(["quote"] + symbols, timeout=60)
    data = parse_json(raw)
    if not data:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {it["symbol"]: it for it in data if isinstance(it, dict) and it.get("symbol")}


def fetch_indices(quotes: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sym in INDEX_SYMBOLS:
        q = quotes.get(sym, {})
        chg = change_pct_from_quote(q) or 0.0
        turnover_raw = safe_float(q.get("turnover"))
        out[sym] = {
            "name": INDEX_NAMES[sym],
            "last": round(safe_float(q.get("last")) or 0, 2),
            "change_pct": chg,
            "turnover": fmt_yi(turnover_raw) if turnover_raw else "—",
        }
    return out


def compute_sector_perf(quotes: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for sector, syms in SECTOR_STOCKS.items():
        changes = [change_pct_from_quote(quotes[s]) for s in syms if quotes.get(s)]
        changes = [c for c in changes if c is not None]
        if not changes:
            continue
        avg = round(sum(changes) / len(changes), 2)
        rows.append({"name": sector, "change_pct": avg, "class": sector_class(avg)})
    rows.sort(key=lambda x: x["change_pct"], reverse=True)
    return rows


def parse_capital_net(data: Any) -> float | None:
    if not data or not isinstance(data, dict):
        return None
    for key in ("net_inflow", "net", "capital_net", "main_net_inflow", "net_main_in", "main_inflow"):
        v = safe_float(data.get(key))
        if v is not None:
            return v
    ci, co = data.get("capital_in"), data.get("capital_out")
    if isinstance(ci, dict) and isinstance(co, dict):
        return sum(safe_float(v) or 0 for v in ci.values()) - sum(safe_float(v) or 0 for v in co.values())
    return None


def fetch_capital_one(symbol: str) -> float | None:
    return parse_capital_net(parse_json(run_lb(["capital", symbol], timeout=30)))


def collect_capital_flows(symbols: list[str], quotes: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []

    def _job(sym: str) -> dict | None:
        net = fetch_capital_one(sym)
        if net is None:
            return None
        q = quotes.get(sym, {})
        return {
            "symbol": sym,
            "name": SYMBOL_NAMES.get(sym, q.get("name") or sym),
            "net_inflow_raw": net,
            "net_inflow": fmt_yi_signed(net),
            "change_pct": change_pct_from_quote(q) or 0.0,
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        for row in pool.map(_job, symbols):
            if row:
                rows.append(row)
    rows.sort(key=lambda x: x["net_inflow_raw"], reverse=True)
    inflow = [{k: v for k, v in r.items() if k != "net_inflow_raw"} for r in rows[:5]]
    outflow = [{k: v for k, v in r.items() if k != "net_inflow_raw"} for r in reversed(rows[-5:])]
    return inflow, outflow


def fetch_kline(symbol: str, count: int = 5) -> list[dict]:
    items = parse_json(run_lb(["kline", symbol, "--period", "day", "--count", str(count)], timeout=35))
    return items if isinstance(items, list) else []


def is_limit_up(chg_pct: float, symbol: str) -> bool:
    threshold = 19.5 if symbol.startswith(("688", "300")) else 9.9
    return chg_pct >= threshold


def count_limit_streak(klines: list[dict], symbol: str) -> int:
    if len(klines) < 2:
        return 0
    sorted_k = sorted(klines, key=lambda k: str(k.get("timestamp") or k.get("time") or ""))
    streak = 0
    for i in range(len(sorted_k) - 1, 0, -1):
        close, prev = safe_float(sorted_k[i].get("close")), safe_float(sorted_k[i - 1].get("close"))
        if close is None or not prev:
            break
        if is_limit_up((close - prev) / prev * 100, symbol):
            streak += 1
        else:
            break
    return streak


def stock_row(sym: str, quotes: dict[str, dict]) -> dict | None:
    q = quotes.get(sym)
    if not q:
        return None
    chg = change_pct_from_quote(q)
    if chg is None:
        return None
    return {
        "symbol": sym,
        "name": SYMBOL_NAMES.get(sym, q.get("name") or sym),
        "change_pct": chg,
        "sector": SYMBOL_TO_SECTOR.get(sym, "—"),
    }


def collect_gainers_limit_streak(quotes: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    all_syms = list({s for syms in SECTOR_STOCKS.values() for s in syms})
    rows = [stock_row(s, quotes) for s in all_syms]
    rows = [r for r in rows if r]
    limit_up = [r for r in rows if is_limit_up(r["change_pct"], r["symbol"])]
    rows.sort(key=lambda x: x["change_pct"], reverse=True)
    top_losers = sorted(rows, key=lambda x: x["change_pct"])[:5]

    streak_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(count_limit_streak, fetch_kline(sym, 5), sym): sym for sym in all_syms}
        for fut in as_completed(futures):
            sym = futures[fut]
            streak = fut.result()
            if streak >= 2:
                q = quotes.get(sym, {})
                streak_rows.append({
                    "symbol": sym,
                    "name": SYMBOL_NAMES.get(sym, sym),
                    "change_pct": change_pct_from_quote(q) or 0,
                    "sector": SYMBOL_TO_SECTOR.get(sym, "—"),
                    "streak": streak,
                })
    streak_rows.sort(key=lambda x: (x["streak"], x["change_pct"]), reverse=True)
    return rows[:10], top_losers, limit_up, streak_rows[:8]


def fetch_hot_news() -> list[dict]:
    rows: list[dict] = []

    def _news(args: list[str]) -> list[dict]:
        items = parse_json(run_lb(args, timeout=30))
        if not items:
            return []
        if isinstance(items, dict):
            items = [items]
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or it.get("headline") or "").strip()
            if title:
                out.append({
                    "title": title,
                    "source": str(it.get("source") or it.get("publisher") or ""),
                    "published_at": str(it.get("published_at") or it.get("time") or ""),
                    "url": str(it.get("url") or it.get("link") or ""),
                })
        return out

    rows.extend(_news(["news", "000001.SH", "--count", "10", "--lang", "zh-CN"]))
    rows.extend(_news(["news", "search", "A股 主线 涨停", "--lang", "zh-CN", "--count", "5"]))
    seen, deduped = set(), []
    for r in rows:
        key = r["title"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped[:15]


def compute_breadth(quotes: dict[str, dict], limit_up: list[dict]) -> dict[str, int]:
    all_syms = list({s for syms in SECTOR_STOCKS.values() for s in syms})
    up = down = flat = 0
    for sym in all_syms:
        chg = change_pct_from_quote(quotes.get(sym, {}))
        if chg is None:
            continue
        if chg > 0.05:
            up += 1
        elif chg < -0.05:
            down += 1
        else:
            flat += 1
    return {"up": up, "down": down, "flat": flat, "limit_up": len(limit_up)}


def generate_summary(indices: dict, sector_perf: list[dict]) -> tuple[str, str]:
    sh_chg = float(indices.get("000001.SH", {}).get("change_pct") or 0)
    if sh_chg >= 1.0:
        tone = "多头强势"
    elif sh_chg >= 0:
        tone = "震荡偏强"
    elif sh_chg >= -1.0:
        tone = "震荡偏弱"
    else:
        tone = "调整下跌"
    if not sector_perf:
        return f"今日市场{tone}，上证{sh_chg:+.2f}%；板块数据暂缺。", tone
    top_sector, worst_sector = sector_perf[0]["name"], sector_perf[-1]["name"]
    mood = "偏积极" if sh_chg > 0 else "偏谨慎"
    return (
        f"今日市场{tone}，上证{sh_chg:+.2f}%；{top_sector}板块领涨，{worst_sector}承压；市场情绪{mood}。",
        tone,
    )


def identify_themes(sector_perf: list[dict], limit_up: list[dict]) -> tuple[list[str], list[str], list[str]]:
    main, secondary, weak = [], [], []
    for row in sector_perf:
        if row["change_pct"] >= 1.0 and len(main) < 2:
            main.append(row["name"])
        elif row["change_pct"] >= 0 and len(secondary) < 2:
            secondary.append(row["name"])
        elif row["change_pct"] < -0.5:
            weak.append(row["name"])
    for lu in limit_up:
        sec = lu.get("sector")
        if sec and sec not in main and sec not in secondary:
            (main if len(main) < 2 else secondary).append(sec)
    if not main and sector_perf:
        main = [sector_perf[0]["name"]]
    if not secondary and len(sector_perf) > 1:
        secondary = [sector_perf[1]["name"]]
    if not weak and sector_perf:
        weak = [sector_perf[-1]["name"]]
    return main[:2], secondary[:3], weak[:3]


def assess_risk(tone: str, breadth: dict, sh_chg: float) -> tuple[str, str]:
    if "下跌" in tone or sh_chg <= -1.5:
        return "偏高", "指数明显走弱，注意控制仓位与止损纪律"
    if breadth.get("down", 0) > breadth.get("up", 0) and sh_chg < 0:
        return "中等", "跌多涨少，结构性机会与风险并存"
    if breadth.get("limit_up", 0) >= 3 and sh_chg > 0:
        return "偏低", "涨停活跃、指数偏强，短线情绪尚可"
    return "中等", "市场震荡，宜精选主线、避免追高"


def next_day_observations(main: list[str], secondary: list[str], weak: list[str], streak_rows: list[dict]) -> list[str]:
    obs: list[str] = []
    if main:
        obs.append(f"关注{main[0]}板块是否延续强势")
    if len(main) > 1:
        obs.append(f"{main[1]}跟进力度决定主线持续性")
    elif secondary:
        obs.append(f"{secondary[0]}能否接力成为新热点")
    if weak:
        obs.append(f"留意{weak[0]}资金是否继续流出")
    if streak_rows:
        top = streak_rows[0]
        obs.append(f"{top['name']}({top['symbol']}) {top.get('streak', 2)}连板高度需观察")
    while len(obs) < 3:
        obs.append("关注量能变化与北向/主力资金方向")
    return obs[:3]


def build_payload() -> dict[str, Any]:
    all_sector_syms = list({s for syms in SECTOR_STOCKS.values() for s in syms})
    all_quote_syms = list(set(INDEX_SYMBOLS + all_sector_syms + TOP_CN_STOCKS))

    log.info("Fetching quotes for %d symbols...", len(all_quote_syms))
    quotes = fetch_quotes_batch(all_quote_syms)
    indices = fetch_indices(quotes)
    sector_perf = compute_sector_perf(quotes)

    log.info("Capital flows...")
    capital_inflow, capital_outflow = collect_capital_flows(TOP_CN_STOCKS, quotes)

    log.info("Gainers / limit-up / streaks...")
    top_gainers, top_losers, limit_up_stocks, streak_rows = collect_gainers_limit_streak(quotes)
    hot_news = fetch_hot_news()

    summary, tone = generate_summary(indices, sector_perf)
    breadth = compute_breadth(quotes, limit_up_stocks)
    main_themes, secondary_themes, weak_sectors = identify_themes(sector_perf, limit_up_stocks)
    sh_chg = float(indices.get("000001.SH", {}).get("change_pct") or 0)
    risk_level, risk_desc = assess_risk(tone, breadth, sh_chg)

    return {
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tone": tone,
        "tone_class": tone_class(tone),
        "summary": summary,
        "indices": indices,
        "breadth": breadth,
        "sector_perf": sector_perf,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "limit_up_stocks": limit_up_stocks,
        "streak_leaders": streak_rows,
        "capital_inflow": capital_inflow,
        "capital_outflow": capital_outflow,
        "hot_news": hot_news,
        "main_themes": main_themes,
        "secondary_themes": secondary_themes,
        "weak_sectors": weak_sectors,
        "risk_level": risk_level,
        "risk_desc": risk_desc,
        "next_day_observations": next_day_observations(main_themes, secondary_themes, weak_sectors, streak_rows),
    }


def upload_cos(local_path: Path) -> bool:
    try:
        r = subprocess.run([COSCMD, "upload", str(local_path), COS_REMOTE], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log.error("coscmd upload failed: %s", (r.stderr or r.stdout or "").strip()[:400])
            return False
        log.info("Uploaded to COS: %s", COS_REMOTE)
        return True
    except Exception as e:
        log.error("coscmd error: %s", e)
        return False


def main() -> int:
    payload = build_payload()
    TMP_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d bytes)", TMP_OUT, TMP_OUT.stat().st_size)
    preview = {
        "date": payload["date"],
        "tone": payload["tone"],
        "summary": payload["summary"],
        "indices": {k: v["change_pct"] for k, v in payload["indices"].items()},
        "sector_top3": payload["sector_perf"][:3],
        "limit_up_count": len(payload["limit_up_stocks"]),
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0 if upload_cos(TMP_OUT) else 1


if __name__ == "__main__":
    sys.exit(main())
