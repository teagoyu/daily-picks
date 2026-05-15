#!/usr/bin/env python3
"""
Automated daily stock picks report generator.

Pulls market data from Longbridge CLI, scores stocks by turnover/momentum/valuation,
generates an HTML report, and uploads to Tencent COS.

Usage:
    python scripts/daily_picks.py                  # Generate + upload
    python scripts/daily_picks.py --no-upload       # Generate only
    python scripts/daily_picks.py --dry-run          # Preview scoring, no HTML
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily_picks")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WATCHLIST = {
    "US": [
        ("NVDA.US", "英伟达", "AI芯片"),
        ("TSLA.US", "特斯拉", "电动车/AI"),
        ("AAPL.US", "苹果", "消费电子"),
        ("MSFT.US", "微软", "云/AI"),
        ("AMZN.US", "亚马逊", "电商/云"),
        ("GOOGL.US", "谷歌", "广告/AI"),
        ("META.US", "Meta", "社交/AI"),
        ("AMD.US", "AMD", "芯片"),
        ("MU.US", "美光", "存储芯片"),
        ("ORCL.US", "甲骨文", "企业软件"),
        ("AVGO.US", "博通", "芯片/网络"),
        ("NFLX.US", "奈飞", "流媒体"),
        ("SNDK.US", "闪迪", "存储"),
        ("CRM.US", "Salesforce", "SaaS"),
        ("PLTR.US", "Palantir", "数据/AI"),
        ("COIN.US", "Coinbase", "加密货币"),
        ("INTC.US", "英特尔", "芯片"),
        ("UBER.US", "Uber", "出行"),
        ("TSM.US", "台积电", "晶圆代工"),
        ("SMCI.US", "超微电脑", "AI服务器"),
    ],
    "HK": [
        ("700.HK", "腾讯控股", "互联网"),
        ("9988.HK", "阿里巴巴", "电商"),
        ("1810.HK", "小米集团", "消费电子"),
        ("9992.HK", "泡泡玛特", "潮玩消费"),
        ("1211.HK", "比亚迪", "电动车"),
        ("981.HK", "中芯国际", "晶圆代工"),
        ("3690.HK", "美团", "本地生活"),
        ("9618.HK", "京东", "电商"),
        ("9888.HK", "百度", "AI/搜索"),
        ("2318.HK", "中国平安", "保险"),
        ("1024.HK", "快手", "短视频"),
        ("6869.HK", "长飞光纤", "光通信"),
        ("2382.HK", "舜宇光学", "光学"),
        ("9999.HK", "网易", "游戏"),
        ("268.HK", "金蝶国际", "企业软件"),
        ("2269.HK", "药明生物", "CXO"),
        ("175.HK", "吉利汽车", "汽车"),
        ("1898.HK", "中煤能源", "煤炭"),
        ("241.HK", "阿里健康", "医疗"),
        ("2015.HK", "理想汽车", "电动车"),
    ],
    "CN": [
        ("600519.SH", "贵州茅台", "白酒"),
        ("300750.SZ", "宁德时代", "锂电池"),
        ("601318.SH", "中国平安", "保险"),
        ("000858.SZ", "五粮液", "白酒"),
        ("603259.SH", "药明康德", "CXO"),
        ("601012.SH", "隆基绿能", "光伏"),
        ("300059.SZ", "东方财富", "券商"),
        ("300570.SZ", "太辰光", "光通信"),
        ("002475.SZ", "立讯精密", "消费电子"),
        ("601985.SH", "中国核电", "核电"),
        ("000977.SZ", "浪潮信息", "AI服务器"),
        ("002594.SZ", "比亚迪", "电动车"),
        ("601888.SH", "中国中免", "免税"),
        ("600036.SH", "招商银行", "银行"),
        ("300274.SZ", "阳光电源", "光伏逆变器"),
        ("002371.SZ", "北方华创", "半导体设备"),
        ("300253.SZ", "卫宁健康", "医疗IT"),
        ("688981.SH", "中芯国际", "晶圆代工"),
        ("601127.SH", "赛力斯", "电动车"),
        ("002049.SZ", "紫光国微", "芯片"),
    ],
}

MARKET_INDICES = {
    "US": [
        ("SPX.US", "标普500"),
        ("NDX.US", "纳斯达克100"),
        ("DJI.US", "道琼斯"),
    ],
    "HK": [
        ("HSI.HK", "恒生指数"),
        ("HSTECH.HK", "恒生科技"),
    ],
    "CN": [
        ("000001.SH", "上证指数"),
        ("399006.SZ", "创业板指"),
    ],
}

COS_CMD = "/Users/yuhao/Library/Python/3.9/bin/coscmd"
COS_REMOTE_PREFIX = "daily_picks/"
COS_PUBLIC_BASE = "https://bloom-1300867387.cos.ap-guangzhou.myqcloud.com"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/ee33d24c-f707-485b-a67f-78fabb257c94"

CONCURRENCY = 6
KLINE_DAYS = 25
TOP_N_DETAIL = 10  # fetch kline for top N by turnover
PICKS_PER_MARKET = 2

WEEKDAY_NAMES_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ---------------------------------------------------------------------------
# Longbridge CLI wrappers
# ---------------------------------------------------------------------------

def run_lb(args: list[str], timeout: int = 30) -> str | None:
    cmd = ["longbridge"] + args + ["--format", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.warning("longbridge %s failed: %s", " ".join(args[:3]), r.stderr.strip()[:200])
            return None
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("longbridge %s timed out", " ".join(args[:3]))
        return None
    except Exception as e:
        log.warning("longbridge %s error: %s", " ".join(args[:3]), e)
        return None


def parse_json(raw: str | None) -> list | dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def fetch_quotes(symbols: list[str]) -> dict:
    """Batch-fetch real-time quotes. Returns {symbol: {...}}."""
    raw = run_lb(["quote"] + symbols, timeout=60)
    items = parse_json(raw)
    if not items:
        return {}
    if isinstance(items, dict):
        items = [items]
    return {it["symbol"]: it for it in items if "symbol" in it}


def fetch_calc_index(symbols: list[str], indices: str = "pe,pb,turnover_rate,total_market_value") -> dict:
    """Batch-fetch calculated indices. Returns {symbol: {...}}."""
    raw = run_lb(["calc-index"] + symbols + ["--fields", indices], timeout=60)
    items = parse_json(raw)
    if not items:
        return {}
    if isinstance(items, dict):
        items = [items]
    return {it["symbol"]: it for it in items if "symbol" in it}


def fetch_kline(symbol: str, count: int = KLINE_DAYS) -> list[dict]:
    """Fetch daily kline for a single symbol."""
    raw = run_lb(["kline", symbol, "--period", "day", "--count", str(count)], timeout=30)
    items = parse_json(raw)
    if not items or not isinstance(items, list):
        return []
    return items


def fetch_news(symbol: str, count: int = 5) -> list[dict]:
    """Fetch latest news headlines for a symbol."""
    raw = run_lb(["news", symbol], timeout=30)
    items = parse_json(raw)
    if not items or not isinstance(items, list):
        return []
    return items[:count]


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_market_data(market: str, watchlist: list[tuple]) -> list[dict]:
    """Collect quote + calc-index for all symbols in a market."""
    symbols = [w[0] for w in watchlist]
    name_map = {w[0]: w[1] for w in watchlist}
    sector_map = {w[0]: w[2] for w in watchlist}

    log.info("[%s] Fetching quotes for %d symbols...", market, len(symbols))
    quotes = fetch_quotes(symbols)

    log.info("[%s] Fetching calc-index...", market)
    indices = fetch_calc_index(symbols)

    stocks = []
    for sym in symbols:
        q = quotes.get(sym)
        idx = indices.get(sym, {})
        if not q:
            log.warning("[%s] No quote for %s, skipping", market, sym)
            continue

        last = safe_float(q.get("last"))
        prev = safe_float(q.get("prev_close"))
        turnover = safe_float(q.get("turnover"))

        if not last or not turnover:
            continue

        change_pct = ((last - prev) / prev * 100) if prev else 0

        stocks.append({
            "symbol": sym,
            "name": name_map[sym],
            "sector": sector_map[sym],
            "market": market,
            "last": last,
            "prev_close": prev,
            "high": safe_float(q.get("high")),
            "low": safe_float(q.get("low")),
            "open": safe_float(q.get("open")),
            "change_pct": change_pct,
            "turnover": turnover,
            "volume": safe_float(q.get("volume")),
            "pe": safe_float(idx.get("pe")),
            "pb": safe_float(idx.get("pb")),
            "market_cap": safe_float(idx.get("total_market_value")),
            "turnover_rate": safe_float(idx.get("turnover_rate")),
        })

    stocks.sort(key=lambda x: x["turnover"], reverse=True)
    for i, s in enumerate(stocks):
        s["turnover_rank"] = i + 1

    return stocks


def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Calculate RSI from a list of closing prices (oldest first)."""
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


def enrich_with_kline(stocks: list[dict], top_n: int = TOP_N_DETAIL):
    """Fetch kline data for top-N stocks and calculate MAs, RSI, volume ratio."""
    targets = stocks[:top_n]
    log.info("Fetching kline for top %d stocks...", len(targets))

    def _fetch(stock):
        klines = fetch_kline(stock["symbol"])
        if len(klines) >= 5:
            closes = [safe_float(k["close"]) for k in klines]
            volumes = [safe_float(k["volume"]) for k in klines]
            stock["ma5"] = sum(closes[-5:]) / 5
            stock["ma10"] = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
            stock["ma20"] = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
            highs = [safe_float(k["high"]) for k in klines]
            lows = [safe_float(k["low"]) for k in klines]
            stock["period_high"] = max(highs) if highs else None
            stock["period_low"] = min(lows) if lows else None

            # RSI-14
            stock["rsi"] = calc_rsi(closes)

            # Volume ratio: today's volume / 5-day average volume
            if len(volumes) >= 6 and volumes[-1] and all(volumes[-6:-1]):
                vol_ma5 = sum(volumes[-6:-1]) / 5
                stock["vol_ratio"] = round(volumes[-1] / vol_ma5, 2) if vol_ma5 > 0 else None
            else:
                stock["vol_ratio"] = None
        return stock

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_fetch, s): s for s in targets}
        for f in as_completed(futures):
            f.result()


def enrich_with_news(stocks: list[dict], top_n: int = 5):
    """Fetch news headlines for top-N stocks."""
    targets = stocks[:top_n]
    log.info("Fetching news for top %d stocks...", len(targets))

    def _fetch(stock):
        news = fetch_news(stock["symbol"])
        stock["news"] = [n.get("title", "") for n in news if n.get("title")]
        return stock

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(_fetch, s): s for s in targets}
        for f in as_completed(futures):
            f.result()


def fetch_index_data() -> dict:
    """Fetch major index data for market overview bar."""
    result = {}
    all_symbols = []
    label_map = {}
    for mkt, indices in MARKET_INDICES.items():
        for sym, label in indices:
            all_symbols.append(sym)
            label_map[sym] = (label, mkt)

    quotes = fetch_quotes(all_symbols)
    for sym, (label, mkt) in label_map.items():
        q = quotes.get(sym)
        if not q:
            continue
        last = safe_float(q.get("last"))
        prev = safe_float(q.get("prev_close"))
        change = ((last - prev) / prev * 100) if prev and last else 0
        result[sym] = {
            "label": label,
            "market": mkt,
            "last": last,
            "change_pct": change,
        }
    return result


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def score_stock(stock: dict, max_turnover: float) -> float:
    """
    Composite score (0-100). Higher = more attractive.

    Weights:
      Turnover rank    15%  (higher volume = more market interest)
      Daily momentum   10%  (positive change)
      PE attractiveness 15%  (reasonable PE range)
      MA trend         20%  (bullish MA alignment)
      Value gap        15%  (distance from period high = opportunity)
      Volume ratio     12%  (today vol vs 5-day avg — confirms breakouts)
      RSI zone         13%  (filters overbought, rewards healthy momentum)
    """
    score = 0.0

    # Turnover score (0-15): normalized by market's max turnover
    if max_turnover > 0:
        score += (stock["turnover"] / max_turnover) * 15

    # Momentum (0-10): positive change is good, capped
    chg = stock.get("change_pct", 0)
    momentum = max(0, min(chg, 12)) / 12 * 10
    score += momentum

    # PE attractiveness (0-15)
    pe = stock.get("pe")
    if pe and pe > 0:
        if 8 <= pe <= 25:
            score += 15
        elif 25 < pe <= 40:
            score += 11
        elif 40 < pe <= 60:
            score += 6
        elif 60 < pe <= 100:
            score += 3
        else:
            score += 1
    else:
        score += 4  # unknown PE gets neutral

    # MA trend (0-20)
    last = stock.get("last", 0)
    ma5 = stock.get("ma5")
    ma10 = stock.get("ma10")
    ma20 = stock.get("ma20")
    if ma5 and ma10 and ma20 and last:
        if last > ma5 > ma10 > ma20:
            score += 20  # perfect bullish alignment
        elif last > ma20 and ma5 > ma10:
            score += 16
        elif last > ma20:
            score += 12
        elif last > ma10:
            score += 8
        else:
            score += 2
    elif ma5:
        score += 8 if last > ma5 else 2

    # Value gap (0-15): sweet spot is -5% to -25% from period high
    ph = stock.get("period_high")
    if ph and last and ph > 0:
        gap = (last - ph) / ph * 100
        if -25 <= gap <= -5:
            score += 15
        elif -5 < gap <= 0:
            score += 10
        elif -35 <= gap < -25:
            score += 7
        else:
            score += 3
    else:
        score += 6

    # Volume ratio (0-12): today's volume / 5-day avg volume
    # >1.5 = strong confirmation, 1.0~1.5 = healthy, <0.7 = weak/no conviction
    vr = stock.get("vol_ratio")
    if vr is not None:
        if vr >= 2.0:
            score += 12  # heavy volume breakout
        elif vr >= 1.5:
            score += 10
        elif vr >= 1.0:
            score += 7
        elif vr >= 0.7:
            score += 4
        else:
            score += 1  # shrinking volume, weak signal
    else:
        score += 4

    # RSI zone (0-13): healthy momentum without overbought risk
    # Sweet spot: RSI 40-65 (healthy uptrend), penalize >75 (overbought) and <30 (falling knife)
    rsi = stock.get("rsi")
    if rsi is not None:
        if 45 <= rsi <= 65:
            score += 13  # ideal zone: uptrend with room to run
        elif 35 <= rsi < 45:
            score += 11  # slightly oversold, potential reversal
        elif 65 < rsi <= 75:
            score += 9   # strong but approaching hot
        elif 30 <= rsi < 35:
            score += 7   # oversold bounce candidate
        elif 75 < rsi <= 85:
            score += 4   # overbought, chase risk
        else:
            score += 1   # extreme: >85 or <30
    else:
        score += 5

    return round(score, 1)


def pick_top(stocks: list[dict], n: int = PICKS_PER_MARKET) -> list[dict]:
    """Return top N scored stocks."""
    for s in stocks:
        max_to = stocks[0]["turnover"] if stocks else 1
        s["score"] = score_stock(s, max_to)
    stocks.sort(key=lambda x: x["score"], reverse=True)
    return stocks[:n]


# ---------------------------------------------------------------------------
# MA trend description
# ---------------------------------------------------------------------------

def describe_ma(stock: dict) -> str:
    last = stock.get("last", 0)
    ma5, ma10, ma20 = stock.get("ma5"), stock.get("ma10"), stock.get("ma20")
    if not ma5:
        return "—"
    if ma10 and ma20:
        if last > ma5 > ma10 > ma20:
            return "多头排列"
        if last > ma20 and ma5 > ma10:
            return "偏多"
        if last > ma20:
            return "站上MA20"
        if last > ma10:
            return "站上MA10"
        return "偏弱"
    return "站上MA5" if last > ma5 else "偏弱"


def describe_rsi(stock: dict) -> tuple[str, str]:
    """Return (label, color) for RSI display."""
    rsi = stock.get("rsi")
    if rsi is None:
        return "—", "var(--muted)"
    label = f"{rsi:.0f}"
    if rsi >= 75:
        return label, "var(--red)"      # overbought
    if rsi <= 35:
        return label, "var(--cyan)"     # oversold
    if 45 <= rsi <= 65:
        return label, "var(--green)"    # healthy
    return label, "var(--text)"


def describe_vol_ratio(stock: dict) -> tuple[str, str]:
    """Return (label, color) for volume ratio display."""
    vr = stock.get("vol_ratio")
    if vr is None:
        return "—", "var(--muted)"
    label = f"{vr:.1f}x"
    if vr >= 1.5:
        return label, "var(--green)"    # strong volume
    if vr >= 1.0:
        return label, "var(--text)"     # normal
    return label, "var(--red)"          # shrinking


def describe_gap(stock: dict) -> str:
    ph = stock.get("period_high")
    last = stock.get("last")
    if not ph or not last or ph == 0:
        return "—"
    gap = (last - ph) / ph * 100
    if gap >= -1:
        return "接近新高"
    return f"{gap:.1f}%"


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def fmt_turnover(val: float, market: str) -> str:
    if market == "US":
        b = val / 1e9
        return f"${b:.0f}亿" if b >= 1 else f"${val/1e6:.0f}M"
    else:
        yi = val / 1e8
        return f"{yi:.1f}亿"


def fmt_price(val: float, market: str) -> str:
    if not val:
        return "—"
    if market == "US":
        return f"${val:,.2f}"
    if market == "HK":
        return f"HK${val:,.2f}"
    return f"¥{val:,.2f}"


def fmt_market_cap(val: float, market: str) -> str:
    if not val:
        return "—"
    if market == "US":
        b = val / 1e9
        return f"${b:,.0f}亿" if b < 1000 else f"${b/1e3:,.1f}万亿"
    yi = val / 1e8
    return f"{yi:,.0f}亿" if yi < 10000 else f"{yi/10000:,.1f}万亿"


def change_class(pct: float) -> str:
    return "up" if pct >= 0 else "down"


def build_miniapp_json(picks: dict, all_data: dict, indices: dict, report_date: datetime) -> dict:
    """Build structured JSON payload for the WeChat Mini Program."""
    def stock_to_card(s: dict) -> dict:
        # turnover_rate: US stocks use volume ratio as a proxy since Longbridge doesn't provide it
        tr = s.get("turnover_rate")
        if tr is None and s.get("vol_ratio"):
            # Expose vol_ratio as a proxy indicator, clearly labeled
            tr = None  # keep null; show vol_ratio in dedicated field instead
        return {
            "symbol": s.get("symbol", ""),
            "name": s.get("name", ""),
            "score": round(s.get("score", 0), 1),
            "change": round(s.get("change_pct", 0), 2),
            "price": round(s.get("last", 0), 2),
            "pe": round(s.get("pe"), 2) if s.get("pe") and s.get("pe") > 0 else None,
            "pb": round(s.get("pb"), 2) if s.get("pb") else None,
            "turnover_rate": round(tr, 2) if tr else None,
            "rsi": round(s.get("rsi"), 1) if s.get("rsi") else None,
            "ma_signal": describe_ma(s),          # compute on-the-fly, was always ""
            "vol_ratio": round(s.get("vol_ratio"), 1) if s.get("vol_ratio") else None,
            "sector": s.get("sector", ""),
        }

    markets = {}
    for mkt in ("US", "HK", "CN"):
        pick_symbols = {p["symbol"] for p in picks.get(mkt, [])}
        all_stocks = all_data.get(mkt, [])
        markets[mkt] = {
            "picks": [stock_to_card(s) for s in all_stocks if s["symbol"] in pick_symbols],
            "all": [stock_to_card(s) for s in sorted(all_stocks, key=lambda x: x.get("score", 0), reverse=True)],
        }

    return {
        "date": report_date.strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "markets": markets,
    }


def generate_html(
    indices: dict,
    us_stocks: list[dict],
    hk_stocks: list[dict],
    cn_stocks: list[dict],
    us_picks: list[dict],
    hk_picks: list[dict],
    cn_picks: list[dict],
    report_date: datetime,
) -> str:
    date_str = report_date.strftime("%Y年%-m月%-d日")
    weekday = WEEKDAY_NAMES_ZH[report_date.weekday()]
    file_date = report_date.strftime("%Y-%m-%d")

    pick_symbols = {p["symbol"] for p in us_picks + hk_picks + cn_picks}

    def _index_items():
        parts = []
        for sym in [s for mkt in ["US", "HK", "CN"] for s, _ in MARKET_INDICES.get(mkt, [])]:
            d = indices.get(sym)
            if not d:
                continue
            cls = change_class(d["change_pct"])
            sign = "+" if d["change_pct"] >= 0 else ""
            parts.append(
                f'<div class="market-item"><div class="label">{d["label"]}</div>'
                f'<div class="value {cls}">{sign}{d["change_pct"]:.2f}%</div></div>'
            )
        return "\n  ".join(parts)

    def _volume_table(stocks: list[dict], market: str, fill_cls: str):
        max_to = stocks[0]["turnover"] if stocks else 1
        rows = []
        for i, s in enumerate(stocks[:10]):
            pct = s["turnover"] / max_to * 100
            cls = change_class(s["change_pct"])
            sign = "+" if s["change_pct"] >= 0 else ""
            hl = ' class="highlight-row"' if s["symbol"] in pick_symbols else ""
            star = ' style="color:var(--gold)"> ★' if s["symbol"] in pick_symbols else ">"
            ticker_display = s["symbol"].replace(f".{market}", "").replace(".SH", "").replace(".SZ", "")
            rows.append(
                f'<tr{hl}><td>{i+1}</td>'
                f'<td class="ticker"{star}{ticker_display}</td>'
                f'<td class="name-cell">{s["name"]}</td>'
                f'<td style="text-align:right">{fmt_price(s["last"], market)}</td>'
                f'<td style="text-align:right" class="{cls}">{sign}{s["change_pct"]:.2f}%</td>'
                f'<td><div>{fmt_turnover(s["turnover"], market)}</div>'
                f'<div class="turnover-bar"><div class="turnover-fill {fill_cls}" style="width:{pct:.0f}%"></div></div></td></tr>'
            )
        return "\n      ".join(rows)

    def _pick_card(stock: dict, rank: int, market: str, rank_cls: str):
        ticker_display = stock["symbol"].replace(f".{market}", "").replace(".SH", "").replace(".SZ", "")
        cls = change_class(stock["change_pct"])
        sign = "+" if stock["change_pct"] >= 0 else ""
        pe_str = f'{stock["pe"]:.1f}x' if stock.get("pe") and stock["pe"] > 0 else "—"
        pb_str = f'{stock["pb"]:.2f}' if stock.get("pb") and stock["pb"] > 0 else "—"
        pe_color = 'var(--green)' if stock.get("pe") and 0 < stock["pe"] <= 30 else 'var(--text)'
        ma_desc = describe_ma(stock)
        ma_color = 'var(--green)' if '多头' in ma_desc or '站上' in ma_desc or '偏多' in ma_desc else 'var(--muted)'
        gap_desc = describe_gap(stock)
        rsi_str, rsi_color = describe_rsi(stock)
        vr_str, vr_color = describe_vol_ratio(stock)

        news_items = stock.get("news", [])
        catalysts = "\n".join(f"<li>{n}</li>" for n in news_items[:5]) if news_items else "<li>暂无最新资讯</li>"

        last = stock["last"]
        entry_lo = last * 0.97
        entry_hi = last * 1.01
        stop = last * 0.92
        target_lo = last * 1.12
        target_hi = last * 1.20

        return f"""
  <div class="pick-card">
    <div class="pick-header">
      <div style="display:flex;align-items:center;gap:14px">
        <div class="pick-rank {rank_cls}">{rank}</div>
        <div class="pick-info">
          <div class="pick-ticker">{ticker_display} <span style="font-size:13px;color:var(--muted);font-weight:400">{stock["name"]}</span></div>
          <div class="pick-name">{stock["sector"]} · 综合评分 {stock.get("score", 0)}</div>
        </div>
      </div>
      <div class="pick-price-block">
        <div class="pick-price">{fmt_price(last, market)}</div>
        <div class="pick-change {cls}">{sign}{stock["change_pct"]:.2f}%</div>
      </div>
    </div>
    <div class="pick-body">
      <div class="metrics-grid">
        <div class="metric"><div class="label">PE</div><div class="val" style="color:{pe_color}">{pe_str}</div></div>
        <div class="metric"><div class="label">PB</div><div class="val">{pb_str}</div></div>
        <div class="metric"><div class="label">市值</div><div class="val">{fmt_market_cap(stock.get("market_cap", 0), market)}</div></div>
        <div class="metric"><div class="label">距高点</div><div class="val" style="color:var(--gold)">{gap_desc}</div></div>
        <div class="metric"><div class="label">MA趋势</div><div class="val" style="color:{ma_color}">{ma_desc}</div></div>
        <div class="metric"><div class="label">RSI</div><div class="val" style="color:{rsi_color}">{rsi_str}</div></div>
        <div class="metric"><div class="label">量比</div><div class="val" style="color:{vr_color}">{vr_str}</div></div>
      </div>
      <div class="catalysts"><h4>最新资讯</h4>
        <ul class="catalyst-list">{catalysts}</ul>
      </div>
      <div class="strategy-box"><h4>参考策略</h4><p>
        <span class="entry">买入：{fmt_price(entry_lo, market)}–{fmt_price(entry_hi, market)}</span> ·
        <span class="stop">止损：{fmt_price(stop, market)}</span> ·
        <span class="target">目标：{fmt_price(target_lo, market)}–{fmt_price(target_hi, market)}</span>
      </p></div>
    </div>
  </div>"""

    market_sections = []

    for label, flag, desc_key, stocks, picks, fill_cls, rank_cls_list in [
        ("美股精选", "US", "US", us_stocks, us_picks, "fill-us", ["rank-1", "rank-2"]),
        ("港股精选", "HK", "HK", hk_stocks, hk_picks, "fill-hk", ["rank-hk", "rank-hk"]),
        ("A股精选", "CN", "CN", cn_stocks, cn_picks, "fill-cn", ["rank-cn", "rank-cn"]),
    ]:
        flag_cls = f"flag-{flag.lower()}"
        vol_table = _volume_table(stocks, flag, fill_cls)
        cards = ""
        for i, p in enumerate(picks):
            rc = rank_cls_list[i] if i < len(rank_cls_list) else rank_cls_list[-1]
            cards += _pick_card(p, i + 1, flag, rc)

        market_sections.append(f"""
<div class="section">
  <div class="market-section-title"><span class="market-flag {flag_cls}">{flag}</span> {label}</div>
  <table class="volume-table">
    <thead><tr><th>#</th><th>代码</th><th>名称</th><th style="text-align:right">收盘</th><th style="text-align:right">涨跌</th><th>成交额</th></tr></thead>
    <tbody>
      {vol_table}
    </tbody>
  </table>
  {cards}
</div>
<hr class="divider">""")

    summary_rows = []
    for mkt_label, flag, picks in [("US", "US", us_picks), ("HK", "HK", hk_picks), ("CN", "CN", cn_picks)]:
        flag_cls = f"flag-{flag.lower()}"
        for p in picks:
            ticker_display = p["symbol"].replace(f".{flag}", "").replace(".SH", "").replace(".SZ", "")
            pe_str = f'{p["pe"]:.1f}x' if p.get("pe") and p["pe"] > 0 else "—"
            pe_color = 'color:var(--green)' if p.get("pe") and 0 < p["pe"] <= 30 else ""
            ma_desc = describe_ma(p)
            summary_rows.append(
                f'<tr><td><span class="market-flag {flag_cls}" style="font-size:11px">{flag}</span></td>'
                f'<td class="ticker">{ticker_display} {p["name"]}</td>'
                f'<td>{p["sector"]}</td>'
                f'<td style="text-align:right;{pe_color}">{pe_str}</td>'
                f'<td style="color:var(--green)">{ma_desc}</td>'
                f'<td style="text-align:right">{p.get("score", 0)}</td></tr>'
            )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日精选 · {date_str}</title>
<style>
  :root {{
    --bg: #0f1117; --card: #1a1d28; --border: #2a2d3a; --text: #e4e4e7;
    --muted: #8b8d97; --green: #22c55e; --red: #ef4444; --blue: #3b82f6;
    --purple: #a78bfa; --gold: #f59e0b; --cyan: #06b6d4; --orange: #f97316;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6; min-height: 100vh;
  }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 0 24px; }}
  .header {{
    background: linear-gradient(135deg, #1e1b4b 0%, #312e81 40%, #1e3a5f 100%);
    padding: 48px 0 40px; border-bottom: 1px solid var(--border);
    position: relative; overflow: hidden;
  }}
  .header::before {{
    content: ''; position: absolute; top: -50%; right: -20%;
    width: 400px; height: 400px;
    background: radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
  }}
  .header-label {{ font-size: 13px; letter-spacing: 3px; text-transform: uppercase; color: var(--purple); margin-bottom: 8px; }}
  .header h1 {{
    font-size: 36px; font-weight: 700;
    background: linear-gradient(90deg, #e0e7ff, #c7d2fe, #a5b4fc);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 6px;
  }}
  .header-date {{ font-size: 15px; color: var(--muted); }}
  .header-sub {{ margin-top: 16px; font-size: 14px; color: #94a3b8; max-width: 700px; }}
  .market-bar {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; padding: 24px 0; }}
  .market-item {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; text-align: center; }}
  .market-item .label {{ font-size: 11px; color: var(--muted); margin-bottom: 3px; }}
  .market-item .value {{ font-size: 17px; font-weight: 600; }}
  .market-item .change {{ font-size: 12px; margin-top: 2px; color: var(--muted); }}
  .up {{ color: var(--green); }} .down {{ color: var(--red); }}
  .section {{ padding: 28px 0; }}
  .section-title {{
    font-size: 13px; letter-spacing: 2px; text-transform: uppercase;
    color: var(--purple); margin-bottom: 18px; padding-bottom: 10px; border-bottom: 1px solid var(--border);
  }}
  .market-section-title {{
    font-size: 20px; font-weight: 700; margin-bottom: 6px; display: flex; align-items: center; gap: 10px;
  }}
  .market-flag {{ font-size: 14px; padding: 2px 10px; border-radius: 6px; font-weight: 600; }}
  .flag-us {{ background: rgba(59,130,246,0.15); color: var(--blue); }}
  .flag-hk {{ background: rgba(239,68,68,0.15); color: var(--red); }}
  .flag-cn {{ background: rgba(245,158,11,0.15); color: var(--gold); }}
  .pick-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 16px;
    overflow: hidden; margin-bottom: 24px; transition: border-color 0.2s;
  }}
  .pick-card:hover {{ border-color: var(--purple); }}
  .pick-header {{ display: flex; align-items: center; justify-content: space-between; padding: 22px 24px 14px; flex-wrap: wrap; gap: 12px; }}
  .pick-rank {{ width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 15px; flex-shrink: 0; }}
  .rank-1 {{ background: linear-gradient(135deg, #f59e0b, #d97706); color: #1a1d28; }}
  .rank-2 {{ background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff; }}
  .rank-hk {{ background: linear-gradient(135deg, #ef4444, #dc2626); color: #fff; }}
  .rank-cn {{ background: linear-gradient(135deg, #f59e0b, #ea580c); color: #fff; }}
  .pick-info {{ flex: 1; min-width: 180px; }}
  .pick-ticker {{ font-size: 22px; font-weight: 700; }}
  .pick-name {{ font-size: 13px; color: var(--muted); }}
  .pick-price-block {{ text-align: right; }}
  .pick-price {{ font-size: 26px; font-weight: 700; }}
  .pick-change {{ font-size: 13px; }}
  .pick-body {{ padding: 0 24px 22px; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin-bottom: 16px; }}
  .metric {{ background: rgba(255,255,255,0.03); border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-align: center; }}
  .metric .label {{ font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }}
  .metric .val {{ font-size: 16px; font-weight: 600; margin-top: 3px; }}
  .catalysts {{ margin: 14px 0; }}
  .catalysts h4 {{ font-size: 13px; color: var(--cyan); margin-bottom: 6px; }}
  .catalyst-list {{ list-style: none; }}
  .catalyst-list li {{ font-size: 13px; color: #94a3b8; padding: 5px 0 5px 18px; position: relative; border-bottom: 1px solid rgba(255,255,255,0.03); }}
  .catalyst-list li::before {{ content: '▸'; position: absolute; left: 0; color: var(--cyan); }}
  .strategy-box {{ background: rgba(34,197,94,0.06); border: 1px solid rgba(34,197,94,0.2); border-radius: 10px; padding: 14px 18px; margin-top: 14px; }}
  .strategy-box h4 {{ font-size: 13px; color: var(--green); margin-bottom: 6px; }}
  .strategy-box p {{ font-size: 13px; color: #94a3b8; }}
  .strategy-box .entry {{ color: var(--green); font-weight: 600; }}
  .strategy-box .stop {{ color: var(--red); font-weight: 600; }}
  .strategy-box .target {{ color: var(--gold); font-weight: 600; }}
  .volume-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 24px; }}
  .volume-table th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  .volume-table td {{ padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.03); }}
  .volume-table tr:hover td {{ background: rgba(255,255,255,0.02); }}
  .volume-table .ticker {{ font-weight: 700; }}
  .volume-table .name-cell {{ color: var(--muted); }}
  .turnover-bar {{ height: 5px; border-radius: 3px; background: var(--border); overflow: hidden; margin-top: 3px; }}
  .turnover-fill {{ height: 100%; border-radius: 3px; }}
  .fill-us {{ background: linear-gradient(90deg, var(--purple), var(--blue)); }}
  .fill-hk {{ background: linear-gradient(90deg, #ef4444, #f97316); }}
  .fill-cn {{ background: linear-gradient(90deg, #f59e0b, #22c55e); }}
  .highlight-row td {{ background: rgba(99,102,241,0.05); }}
  .divider {{ border: none; border-top: 1px solid var(--border); margin: 8px 0 28px; }}
  .disclaimer {{ background: rgba(239,68,68,0.05); border: 1px solid rgba(239,68,68,0.15); border-radius: 10px; padding: 14px 18px; margin: 24px 0; font-size: 12px; color: #9ca3af; text-align: center; }}
  .footer {{ padding: 28px 0; text-align: center; font-size: 11px; color: #4b5563; border-top: 1px solid var(--border); }}
</style>
</head>
<body>
<div class="header">
  <div class="container">
    <div class="header-label">Daily Stock Picks · US · HK · A-Share</div>
    <h1>每日精选买入</h1>
    <div class="header-date">{date_str}（{weekday}）· 自动生成</div>
    <div class="header-sub">覆盖美股、港股、A股三大市场。基于成交额排名，综合估值、动量、均线趋势与价值空间自动评分，每市场精选两只标的。</div>
  </div>
</div>
<div class="container">
<div class="market-bar">
  {_index_items()}
</div>
{"".join(market_sections)}

<div class="section">
  <div class="section-title">今日精选一览</div>
  <table class="volume-table">
    <thead><tr><th>市场</th><th>标的</th><th>行业</th><th style="text-align:right">PE</th><th>趋势</th><th style="text-align:right">评分</th></tr></thead>
    <tbody>
      {"".join(summary_rows)}
    </tbody>
  </table>
</div>

<div class="disclaimer">
  本报告由算法自动生成，仅供参考，不构成投资建议。股市有风险，入市需谨慎。数据来源：Longbridge API。
</div>
<div class="footer">Generated by Turtle Investment Framework · Powered by Longbridge CLI · {file_date}</div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# COS upload
# ---------------------------------------------------------------------------

def upload_to_cos(local_path: Path, remote_key: str) -> bool:
    if not Path(COS_CMD).exists():
        log.warning("coscmd not found at %s, skipping upload", COS_CMD)
        return False
    cmd = [COS_CMD, "upload", str(local_path), remote_key]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.info("Uploaded to COS: %s", remote_key)
            return True
        log.error("COS upload failed: %s", r.stderr.strip()[:300])
        return False
    except Exception as e:
        log.error("COS upload error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def send_feishu(picks: dict, indices: dict, report_url: str, report_date: datetime) -> bool:
    """Send a summary card to Feishu group bot."""
    if not FEISHU_WEBHOOK:
        log.warning("No Feishu webhook configured, skipping")
        return False

    date_str = report_date.strftime("%Y年%-m月%-d日")
    weekday = WEEKDAY_NAMES_ZH[report_date.weekday()]

    # Build index overview line
    idx_parts = []
    for sym in [s for mkt in ["US", "HK", "CN"] for s, _ in MARKET_INDICES.get(mkt, [])]:
        d = indices.get(sym)
        if not d:
            continue
        sign = "+" if d["change_pct"] >= 0 else ""
        emoji = "🟢" if d["change_pct"] >= 0 else "🔴"
        idx_parts.append(f'{emoji} {d["label"]} {sign}{d["change_pct"]:.2f}%')
    idx_line = "  |  ".join(idx_parts)

    # Build pick rows
    pick_rows = []
    market_labels = {"US": "🇺🇸", "HK": "🇭🇰", "CN": "🇨🇳"}
    for mkt in ["US", "HK", "CN"]:
        for p in picks.get(mkt, []):
            ticker = p["symbol"].replace(f".{mkt}", "").replace(".SH", "").replace(".SZ", "")
            sign = "+" if p["change_pct"] >= 0 else ""
            pe_str = f'PE {p["pe"]:.0f}x' if p.get("pe") and p["pe"] > 0 else ""
            ma_desc = describe_ma(p)
            pick_rows.append(
                f'{market_labels.get(mkt, "")} **{ticker}** {p["name"]}  '
                f'{sign}{p["change_pct"]:.2f}%  {pe_str}  {ma_desc}  '
                f'评分 **{p.get("score", 0):.0f}**'
            )

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 每日精选买入 · {date_str}（{weekday}）"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**大盘概览**\n{idx_line}",
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**今日精选**\n" + "\n".join(pick_rows),
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "📄 查看完整报告"},
                            "url": report_url,
                            "type": "primary",
                        }
                    ],
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "⚠️ 仅供参考，不构成投资建议 · Turtle Investment Framework"},
                    ],
                },
            ],
        },
    }

    payload_text = json.dumps(card, ensure_ascii=False)
    payload = payload_text.encode("utf-8")
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            body = json.loads(raw)
            if body.get("code") == 0 or body.get("StatusCode") == 0:
                log.info(
                    "Feishu notification sent successfully (status=%s, picks=%s, bytes=%s)",
                    getattr(resp, "status", "?"),
                    sum(len(v) for v in picks.values()),
                    len(payload),
                )
                return True
            log.error(
                "Feishu API error: status=%s body=%s payload_preview=%s",
                getattr(resp, "status", "?"),
                raw[:1000],
                payload_text[:1000],
            )
            return False
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        log.error(
            "Feishu send failed: http_status=%s reason=%s body=%s payload_preview=%s",
            e.code,
            e.reason,
            error_body[:2000],
            payload_text[:1000],
        )
        return False
    except Exception as e:
        log.exception(
            "Feishu send failed: %s | payload_preview=%s",
            e,
            payload_text[:1000],
        )
        return False


def safe_float(val) -> float | None:
    if val is None or val == "-" or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Daily stock picks report generator")
    parser.add_argument("--no-upload", action="store_true", help="Skip COS upload")
    parser.add_argument("--no-notify", action="store_true", help="Skip Feishu notification")
    parser.add_argument("--dry-run", action="store_true", help="Only show scoring, no HTML")
    parser.add_argument("--date", help="Override report date (YYYY-MM-DD)")
    args = parser.parse_args()

    report_date = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now()
    file_date = report_date.strftime("%Y-%m-%d")
    log.info("=== Daily Picks Report: %s ===", file_date)

    # 1. Fetch index data
    log.info("--- Fetching market indices ---")
    indices = fetch_index_data()

    # 2. Collect market data for each market
    all_data = {}
    for market, watchlist in WATCHLIST.items():
        log.info("--- Processing %s market ---", market)
        stocks = collect_market_data(market, watchlist)
        if not stocks:
            log.warning("No data for %s market", market)
            continue

        # Enrich top stocks with kline (MA calculation)
        enrich_with_kline(stocks, TOP_N_DETAIL)

        # Enrich top stocks with news
        enrich_with_news(stocks, 5)

        all_data[market] = stocks

    # 3. Score and pick
    picks = {}
    for market, stocks in all_data.items():
        top = pick_top(stocks, PICKS_PER_MARKET)
        picks[market] = top
        log.info("[%s] Picks: %s", market, [(p["symbol"], p["score"]) for p in top])

    if args.dry_run:
        for market, stocks in all_data.items():
            print(f"\n=== {market} Top 10 ===")
            for s in stocks[:10]:
                rsi = s.get('rsi')
                rsi_s = f"{rsi:.0f}" if rsi else "-"
                vr = s.get('vol_ratio')
                vr_s = f"{vr:.1f}x" if vr else "-"
                print(f"  {s['turnover_rank']:2d}. {s['symbol']:12s} {s['name']:8s} "
                      f"Chg={s['change_pct']:+6.2f}%  TO={s['turnover']/1e8:8.0f}亿  "
                      f"PE={s.get('pe') or '-':>8}  MA={describe_ma(s):6s}  "
                      f"RSI={rsi_s:>4}  Vol={vr_s:>5}  "
                      f"Score={s.get('score', 0):5.1f}")
        return

    # 4. Generate HTML
    log.info("--- Generating HTML ---")
    html = generate_html(
        indices=indices,
        us_stocks=all_data.get("US", []),
        hk_stocks=all_data.get("HK", []),
        cn_stocks=all_data.get("CN", []),
        us_picks=picks.get("US", []),
        hk_picks=picks.get("HK", []),
        cn_picks=picks.get("CN", []),
        report_date=report_date,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"daily_picks_{file_date}.html"
    out_path.write_text(html, encoding="utf-8")
    log.info("Report saved: %s", out_path)

    # 5a. Build and save JSON for Mini Program
    json_data = build_miniapp_json(picks, all_data, indices, report_date)
    json_path = OUTPUT_DIR / f"daily_picks_{file_date}.json"
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("JSON saved: %s", json_path)

    # 5b. Upload to COS
    report_url = ""
    if not args.no_upload:
        remote_key = f"{COS_REMOTE_PREFIX}{file_date}.html"
        upload_to_cos(out_path, remote_key)
        report_url = f"{COS_PUBLIC_BASE}/{remote_key}"
        log.info("Public URL: %s", report_url)
        # Upload JSON as both dated and latest (for Mini Program)
        upload_to_cos(json_path, f"{COS_REMOTE_PREFIX}{file_date}.json")
        upload_to_cos(json_path, f"{COS_REMOTE_PREFIX}latest.json")
        log.info("Mini Program JSON: %s", f"{COS_PUBLIC_BASE}/{COS_REMOTE_PREFIX}latest.json")
        # Upload static website
        web_html = Path(__file__).parent / "web" / "index.html"
        if web_html.exists():
            upload_to_cos(web_html, "index.html")
            log.info("Website: %s", f"{COS_PUBLIC_BASE}/index.html")
    else:
        log.info("Upload skipped (--no-upload)")

    # 6. Send Feishu notification
    if not args.no_notify:
        send_feishu(picks, indices, report_url or f"file://{out_path}", report_date)
    else:
        log.info("Feishu notification skipped (--no-notify)")

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
