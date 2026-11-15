#!/usr/bin/env python3
"""Weekly gold outlook: scores whether gold looks bullish, bearish or set to
consolidate in the coming week, from real market data plus world news, and
sends the report to Telegram.

Three parts, each scored from -100 (bearish for gold) to +100 (bullish):
  technical  gold's own trend and momentum (moving averages, returns, RSI)
  macro      US dollar, real yields, VIX, stocks, silver, oil
  news       economic and world headlines from the past 7 days

Market data comes from Yahoo Finance and FRED (no keys needed). News comes
from RSS feeds and Google News searches listed in gold_config.json. If
ANTHROPIC_API_KEY is set and the `anthropic` package is installed, Claude
reads the headlines and scores them; otherwise a keyword model does.

Usage:
  python3 gold_outlook.py              build the report and send it to Telegram
  python3 gold_outlook.py --dry-run    print the report, no Telegram
  python3 gold_outlook.py --no-ai      use the keyword news model even with a key
"""

import argparse
import concurrent.futures
import csv
import html
import io
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "gold_config.json"
REPORT_PATH = HERE / "gold_report.md"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
CLAUDE_MODEL = "claude-opus-5-5"
NEWS_DAYS = 7


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- market data

def yahoo_history(symbol, range_="1y"):
    """Daily bars as a list of {date, open, high, low, close}, oldest first."""
    path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?range={range_}&interval=1d"
    last_err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            data = json.loads(http_get(f"https://{host}{path}"))
            result = data["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            bars = []
            for i, ts in enumerate(result["timestamp"]):
                c, h, l = quote["close"][i], quote["high"][i], quote["low"][i]
                if None in (c, h, l):
                    continue
                bars.append({"date": datetime.fromtimestamp(ts, timezone.utc).date(),
                             "close": c, "high": h, "low": l})
            if bars:
                return bars
        except Exception as e:  # try the other host
            last_err = e
    raise RuntimeError(f"Yahoo {symbol}: {last_err}")


def fred_series(series_id):
    """FRED series as a list of (date, value), oldest first. No key needed."""
    raw = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}").decode()
    out = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
            continue
        try:
            out.append((datetime.strptime(row[0], "%Y-%m-%d").date(), float(row[1])))
        except ValueError:  # "." marks a missing day
            continue
    return out


def pct_change(closes, days):
    if len(closes) <= days or closes[-1 - days] == 0:
        return None
    return (closes[-1] / closes[-1 - days] - 1) * 100


def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0))
        losses.append(max(a - b, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):  # Wilder smoothing
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr_adx(bars, n=14):
    """Average true range and ADX (trend strength: under 20 means no real trend)."""
    if len(bars) < 2 * n + 1:
        return None, None
    trs, pdm, ndm = [], [], []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]),
                       abs(cur["low"] - prev["close"])))
        up, down = cur["high"] - prev["high"], prev["low"] - cur["low"]
        pdm.append(up if up > down and up > 0 else 0)
        ndm.append(down if down > up and down > 0 else 0)
    atr, p, m = sum(trs[:n]), sum(pdm[:n]), sum(ndm[:n])
    dxs = []
    for i in range(n, len(trs)):
        atr = atr - atr / n + trs[i]
        p = p - p / n + pdm[i]
        m = m - m / n + ndm[i]
        pdi, ndi = 100 * p / atr, 100 * m / atr
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0)
    adx = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        adx = (adx * (n - 1) + dx) / n
    return atr / n, adx


def bandwidth_percentile(closes, n=20, lookback=126):
    """Where today's Bollinger band width sits among the last ~6 months (0-100).
    Low values mean volatility is squeezed, which often comes with sideways trade."""
    widths = []
    for end in range(n, len(closes) + 1):
        window = closes[end - n:end]
        mean = sum(window) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in window) / n)
        widths.append(4 * sd / mean)
    widths = widths[-lookback:]
    if len(widths) < 20:
        return None
    return 100 * sum(w <= widths[-1] for w in widths) / len(widths)


def fetch_markets(cfg):
    markets = {}
    symbols = cfg["markets"]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        futures = {pool.submit(yahoo_history, sym): key for key, sym in symbols.items()}
        futures[pool.submit(fred_series, "DFII10")] = "real_yield"
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            try:
                markets[key] = fut.result()
            except Exception as e:
                log(f"Couldn't get {key}: {e}")
    return markets


# ---------------------------------------------------------------- scoring: market

def technical_score(bars):
    closes = [b["close"] for b in bars]
    price = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    s50_prev = sma(closes[:-10], 50)
    trend_checks = [
        ("above 20-day average", s20 and price > s20),
        ("above 50-day average", s50 and price > s50),
        ("above 200-day average", s200 and price > s200),
        ("50-day average rising", s50 and s50_prev and s50 > s50_prev),
    ]
    trend = sum(25 if ok else -25 for _, ok in trend_checks)

    r1, r4 = pct_change(closes, 5), pct_change(closes, 20)
    r = rsi(closes)
    if r is None:
        rsi_part = 0
    elif r > 75:
        rsi_part = -30   # stretched: pullbacks are likely
    elif r < 25:
        rsi_part = 30    # washed out: bounces are likely
    else:
        rsi_part = clamp((r - 50) * 2.5)
    momentum = (0.5 * clamp((r4 or 0) / 6 * 100) + 0.3 * clamp((r1 or 0) / 2.5 * 100)
                + 0.2 * rsi_part)

    atr, adx = atr_adx(bars)
    return {
        "score": 0.5 * trend + 0.5 * momentum,
        "price": price, "sma20": s20, "sma50": s50, "sma200": s200,
        "week_change": r1, "month_change": r4, "rsi": r, "atr": atr, "adx": adx,
        "squeeze_pct": bandwidth_percentile(closes),
        "high20": max(b["high"] for b in bars[-20:]),
        "low20": min(b["low"] for b in bars[-20:]),
        "trend_checks": trend_checks,
        "last_date": bars[-1]["date"],
    }


def macro_score(markets):
    """Each factor is scored -100..100 for gold, then averaged by weight over
    the factors we actually have data for."""
    parts = []  # (name, weight, score, description)

    def closes(key):
        return [b["close"] for b in markets[key]] if key in markets else None

    dxy = closes("dollar_index")
    if dxy:
        w1, w4 = pct_change(dxy, 5) or 0, pct_change(dxy, 20) or 0
        parts.append(("US dollar", 0.30, -(0.6 * clamp(w1 / 1.0 * 100) + 0.4 * clamp(w4 / 2.5 * 100)),
                      f"DXY {dxy[-1]:.2f}, {w1:+.2f}% on the week, {w4:+.2f}% on the month"))

    ry = markets.get("real_yield")
    if ry and len(ry) > 5:
        bps = (ry[-1][1] - ry[-6][1]) * 100
        parts.append(("Real yields", 0.30, -clamp(bps / 15 * 100),
                      f"US 10y real yield {ry[-1][1]:.2f}%, {bps:+.0f}bp on the week"))
    else:
        tnx = closes("us10y_yield")
        if tnx and len(tnx) > 5:
            bps = (tnx[-1] - tnx[-6]) * 100
            parts.append(("Bond yields", 0.30, -clamp(bps / 20 * 100),
                          f"US 10y yield {tnx[-1]:.2f}%, {bps:+.0f}bp on the week"))

    vix = closes("vix")
    if vix:
        w1 = pct_change(vix, 5) or 0
        parts.append(("Fear (VIX)", 0.10, 0.5 * clamp((vix[-1] - 18) / 10 * 100) + 0.5 * clamp(w1 / 20 * 100),
                      f"VIX {vix[-1]:.1f}, {w1:+.1f}% on the week"))

    spx = closes("sp500")
    if spx:
        w1 = pct_change(spx, 5) or 0
        parts.append(("Stocks", 0.10, -clamp(w1 / 3 * 100), f"S&P 500 {w1:+.2f}% on the week"))

    slv = closes("silver")
    if slv:
        w1 = pct_change(slv, 5) or 0
        parts.append(("Silver", 0.10, clamp(w1 / 4 * 100), f"Silver ${slv[-1]:.2f}, {w1:+.2f}% on the week"))

    oil = closes("oil")
    if oil:
        w1 = pct_change(oil, 5) or 0
        parts.append(("Oil", 0.10, clamp(w1 / 6 * 100), f"WTI ${oil[-1]:.2f}, {w1:+.2f}% on the week"))

    total_w = sum(p[1] for p in parts)
    score = sum(p[1] * p[2] for p in parts) / total_w if total_w else 0
    return {"score": score, "parts": parts}


# ---------------------------------------------------------------- news

def google_news_url(query):
    q = urllib.parse.quote(f"{query} when:{NEWS_DAYS}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(raw, feed_url):
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in el:
            name = child.tag.split("}")[-1]
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif child.text:
                fields.setdefault(name, child.text.strip())
        title = html.unescape(re.sub(r"<[^>]+>", "", fields.get("title", ""))).strip()
        if not title:
            continue
        source = fields.get("source") or urllib.parse.urlparse(feed_url).netloc.replace("www.", "")
        items.append({
            "title": title,
            "source": source,
            "link": fields.get("link", ""),
            "published": _parse_date(fields.get("pubDate") or fields.get("published") or fields.get("updated")),
        })
    return items


def fetch_news(cfg):
    urls = [google_news_url(q) for q in cfg.get("google_news_queries", [])] + cfg.get("rss_feeds", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_DAYS)
    headlines, seen, ok = [], set(), 0

    def get(url):
        return url, parse_feed(http_get(url), url)

    with concurrent.futures.ThreadPoolExecutor(10) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(get, u) for u in urls]):
            try:
                url, items = fut.result()
                ok += 1
            except Exception as e:
                log(f"Feed failed: {e}")
                continue
            for item in items:
                if item["published"] and item["published"] < cutoff:
                    continue
                # Google News titles end in " - Source"; dedupe on the bare headline.
                key = re.sub(r"\W+", " ", re.sub(r"\s+-\s+[^-]+$", "", item["title"]).lower()).strip()
                if key in seen:
                    continue
                seen.add(key)
                headlines.append(item)
    log(f"{len(headlines)} headlines from {ok}/{len(urls)} feeds")
    headlines.sort(key=lambda h: h["published"] or cutoff, reverse=True)
    return headlines


# Keyword model. Positive weight = good for gold. Rules run in order and each
# match is blanked out, so "no rate cut" is caught as hawkish before the
# plain "rate cut" rule sees it.
NEWS_RULES = [
    # Gold-specific
    (r"central banks? (?:\w+ ){0,2}(?:buy|buying|bought|purchas\w*|add\w*|boost\w*)(?: \w+){0,3} gold|gold reserves? (?:rise|climb|grow|hit)", 2.0, "central bank gold buying"),
    (r"central banks? (?:\w+ ){0,2}(?:sell|selling|sold)(?: \w+){0,3} gold", -2.0, "central bank gold selling"),
    (r"gold etf (?:\w+ )?inflows?|inflows? (?:in|into) gold", 1.0, "gold ETF inflows"),
    (r"gold etf (?:\w+ )?outflows?|outflows? from gold", -1.0, "gold ETF outflows"),
    (r"gold (?:\w+ ){0,2}(?:falls?|fell|slips?|drops?|tumbles?|slumps?|declines?|retreats?|loses?|sinks?|plunges?|slides?)|profit[- ]taking", -1.0, "gold falling"),
    (r"gold (?:\w+ ){0,2}(?:rises?|rose|rall\w+|jumps?|surges?|climbs?|gains?|soars?|record|all[- ]time high|hits? \$?\d)", 1.0, "gold rising"),
    # Central banks / rates (hawkish first, so negations are caught)
    (r"no (?:rate )?cuts?|fewer (?:rate )?cuts|(?:delay|doubt|push back|pare|scale back)\w* (?:on |of )?(?:\w+ )?(?:rate )?cuts?|rate cuts? (?:bets|hopes|odds) (?:fade|dim|recede|cool)\w*|higher for longer|hawkish|rate hikes?|hikes? (?:interest )?rates|tighten\w*", -2.0, "hawkish central banks"),
    (r"rate cuts?|cuts? (?:interest )?rates|dovish|easing|quantitative easing|pause\w* (?:rate )?hikes", 2.0, "rate cuts / dovish"),
    # Inflation: hot prints push yields up (bad for gold), soft prints the reverse
    (r"(?:inflation|cpi|pce|prices) (?:\w+ ){0,2}(?:hotter|accelerat\w+|surges?|jumps?|spikes?|tops? (?:forecast|estimate)|higher than expected|beats?)|sticky inflation", -1.0, "hot inflation"),
    (r"(?:inflation|cpi|pce) (?:\w+ ){0,2}(?:cool\w*|eas\w+|slow\w*|soft\w*|lower than expected|below (?:forecast|expectations))|disinflation", 1.0, "cooling inflation"),
    (r"stagflation", 1.5, "stagflation risk"),
    # Dollar and yields
    (r"(?:dollar|greenback|dxy) (?:\w+ ){0,2}(?:falls?|fell|weakens?|slides?|slips?|slumps?|drops?|tumbles?|sinks?)|weak(?:er)? dollar", 1.5, "weaker dollar"),
    (r"(?:dollar|greenback|dxy) (?:\w+ ){0,2}(?:rises?|rose|strengthens?|firms?|rall\w+|jumps?|gains?|climbs?|surges?)|(?:strong|firm)(?:er)? dollar", -1.5, "stronger dollar"),
    (r"(?:yields?|treasur\w+ yields?) (?:\w+ ){0,2}(?:falls?|fell|drops?|slides?|tumbles?|slips?|declines?|retreats?)", 1.5, "falling yields"),
    (r"(?:yields?|treasur\w+ yields?) (?:\w+ ){0,2}(?:rises?|rose|climbs?|jumps?|surges?|spikes?|soars?|hits? \d)", -1.5, "rising yields"),
    # Growth and jobs
    (r"strong (?:jobs|hiring|payrolls|economy|growth|data)|jobs (?:report )?(?:beats?|surges?|smash\w*)|unemployment (?:falls?|drops?)|robust (?:economy|growth)|gdp (?:beats?|surges?|accelerates?)|soft landing", -1.5, "strong economy"),
    (r"recession|slowdown|contraction|layoffs?|job (?:cuts|losses)|unemployment (?:rises?|jumps?|climbs?)|weak (?:jobs|hiring|data|growth)|jobless claims (?:rise|jump|climb)|downturn", 1.5, "growth worries"),
    # Geopolitics and risk
    (r"cease-?fire|truce|peace (?:deal|talks|agreement|plan)|de-?escalat\w*|trade deal|tariff (?:pause|relief|cuts?|truce)", -1.5, "easing tensions"),
    (r"\bwar\b|invasion|invades?|missiles?|air ?strikes?|escalat\w*|military (?:strike|attack|action)|nuclear (?:threat|test)|sanctions|coup|attack on|conflict|hostilities", 1.0, "geopolitical risk"),
    (r"tariffs?|trade war|trade tensions", 1.0, "trade tensions"),
    (r"safe[- ]havens?|risk[- ]off|sell-?off|market turmoil|crash|bank (?:failure|collapse|run|crisis)|debt (?:ceiling|crisis)|government shutdown|credit downgrade|default", 1.0, "risk-off / financial stress"),
    (r"stocks? (?:\w+ ){0,2}(?:record|rall\w+|surges?|soars?)|risk[- ]on|record high", -0.5, "risk-on"),
]
NEWS_RULES = [(re.compile(p, re.I), w, label) for p, w, label in NEWS_RULES]


def keyword_news_score(headlines):
    total, gross, matched = 0.0, 0.0, 0
    drivers = {}
    for h in headlines:
        text = " " + re.sub(r"\s+-\s+[^-]+$", "", h["title"]) + " "
        hit = False
        for rx, w, label in NEWS_RULES:
            n = len(rx.findall(text))
            if not n:
                continue
            text = rx.sub(" ", text)
            total += w
            gross += abs(w)
            drivers.setdefault(label, [0, 0.0, []])
            drivers[label][0] += 1
            drivers[label][1] += w
            if len(drivers[label][2]) < 2:
                drivers[label][2].append(h["title"])
            hit = True
        matched += hit
    # Net balance of bullish vs bearish signal, damped when few headlines matched.
    score = (100 * total / gross if gross else 0) * min(1.0, matched / 20)
    top = sorted(drivers.items(), key=lambda kv: -abs(kv[1][1]))
    return {
        "score": score,
        "matched": matched,
        "bullish": [f"{label} ({n} headlines)" for label, (n, w, _) in top if w > 0][:5],
        "bearish": [f"{label} ({n} headlines)" for label, (n, w, _) in top if w < 0][:5],
        "examples": {label: ex for label, (_, _, ex) in top[:6]},
    }


CLAUDE_SYSTEM = """You are a macro analyst who forecasts gold (XAU/USD) one week ahead.
You will get a market snapshot and the past week's headlines from economic and world news feeds.
Judge what the news means for gold over the coming week. Think about what is already priced in,
real yields and the dollar, Fed and other central bank expectations, inflation and jobs data,
geopolitical risk and safe-haven demand, central bank buying, and scheduled events in the week ahead.
Ignore headlines with no bearing on gold. Be calibrated: most weeks are mixed, and +/-60 or more
should mean an unusually one-sided news backdrop. Do not invent facts that are not in the input."""

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "news_score": {"type": "integer", "description": "-100 very bearish for gold .. +100 very bullish"},
        "bias": {"type": "string", "enum": ["bullish", "bearish", "consolidation"]},
        "summary": {"type": "string", "description": "3-5 sentences on what drove gold this week and what matters next"},
        "bullish_drivers": {"type": "array", "items": {"type": "string"}},
        "bearish_drivers": {"type": "array", "items": {"type": "string"}},
        "week_ahead": {"type": "array", "items": {"type": "string"},
                       "description": "Scheduled events or risks next week mentioned or implied by the news"},
    },
    "required": ["news_score", "bias", "summary", "bullish_drivers", "bearish_drivers", "week_ahead"],
    "additionalProperties": False,
}


def claude_news_score(headlines, snapshot):
    import anthropic  # optional dependency, only needed for this mode

    lines = []
    for h in headlines[:400]:
        day = h["published"].strftime("%a %d %b") if h["published"] else "?"
        lines.append(f"- [{day}] {h['title']} ({h['source']})")
    prompt = (f"Market snapshot (as of the last close):\n{snapshot}\n\n"
              f"Headlines from the past {NEWS_DAYS} days ({len(lines)}):\n" + "\n".join(lines))

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": CLAUDE_SCHEMA}},
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)
    result["news_score"] = clamp(float(result["news_score"]))
    return result


# ---------------------------------------------------------------- report

def verdict(composite, tech, threshold):
    squeeze = tech["squeeze_pct"] is not None and tech["squeeze_pct"] <= 25
    no_trend = tech["adx"] is not None and tech["adx"] < 20
    if abs(composite) < threshold or (squeeze and no_trend and abs(composite) < 2 * threshold):
        lean = ""
        if composite >= threshold / 2:
            lean = " (leaning bullish)"
        elif composite <= -threshold / 2:
            lean = " (leaning bearish)"
        return "CONSOLIDATION" + lean, "🟡"
    strength = "STRONGLY " if abs(composite) >= 50 else ""
    return (strength + "BULLISH", "🟢") if composite > 0 else (strength + "BEARISH", "🔴")


def confidence(composite, scores):
    agree = sum(1 for s in scores if s * composite > 0 and abs(s) >= 10)
    if agree == 3 and abs(composite) >= 30:
        return "high"
    if agree >= 2 and abs(composite) >= 15:
        return "medium"
    return "low"


def bar(score, width=10):
    filled = round((score + 100) / 200 * width)
    return "▰" * filled + "▱" * (width - filled)


def build_report(cfg, use_ai=True):
    markets = fetch_markets(cfg)
    if "gold" not in markets or len(markets["gold"]) < 60:
        raise RuntimeError("No gold price data; can't build the outlook.")
    tech = technical_score(markets["gold"])
    macro = macro_score(markets)

    snapshot = "\n".join(
        [f"Gold ${tech['price']:,.0f} ({tech['week_change']:+.2f}% week, {tech['month_change']:+.2f}% month), "
         f"RSI {tech['rsi']:.0f}"] + [p[3] for p in macro["parts"]])

    headlines = fetch_news(cfg)
    kw = keyword_news_score(headlines)
    ai = None
    if use_ai and os.environ.get("ANTHROPIC_API_KEY") and headlines:
        try:
            ai = claude_news_score(headlines, snapshot)
        except ImportError:
            log("anthropic package not installed; using keyword news model (pip install anthropic)")
        except Exception as e:
            log(f"Claude news analysis failed, using keyword model: {e}")
    news_score = ai["news_score"] if ai else kw["score"]

    w = cfg["weights"]
    scores = [tech["score"], macro["score"], news_score]
    composite = w["technical"] * scores[0] + w["macro"] * scores[1] + w["news"] * scores[2]
    label, icon = verdict(composite, tech, cfg.get("consolidation_threshold", 15))
    conf = confidence(composite, scores)

    weekly_move = tech["atr"] * math.sqrt(5) if tech["atr"] else None
    now = datetime.now(timezone.utc)
    L = []
    L.append(f"{icon} GOLD WEEKLY OUTLOOK — week of {(now + timedelta(days=1)):%d %b %Y}")
    L.append("")
    L.append(f"Verdict: {label}")
    L.append(f"Score: {composite:+.0f} / 100   Confidence: {conf}")
    L.append("")
    L.append(f"Technical {bar(scores[0])} {scores[0]:+.0f}")
    L.append(f"Macro     {bar(scores[1])} {scores[1]:+.0f}")
    L.append(f"News      {bar(scores[2])} {scores[2]:+.0f}" + ("  (Claude)" if ai else "  (keywords)"))
    L.append("")
    L.append(f"📈 Gold ${tech['price']:,.2f} (close {tech['last_date']:%a %d %b})")
    L.append(f"Week {tech['week_change']:+.2f}% · Month {tech['month_change']:+.2f}% · RSI {tech['rsi']:.0f}"
             + (f" · ADX {tech['adx']:.0f}" if tech["adx"] is not None else ""))
    L.append("Trend: " + ", ".join(("✓ " if ok else "✗ ") + name for name, ok in tech["trend_checks"]))
    if tech["squeeze_pct"] is not None and tech["squeeze_pct"] <= 25:
        L.append(f"Volatility is squeezed (band width in the lowest {tech['squeeze_pct']:.0f}% of 6 months) — a range or a breakout is building.")
    L.append(f"20-day range: ${tech['low20']:,.0f} – ${tech['high20']:,.0f} · 50-day avg ${tech['sma50']:,.0f}")
    if weekly_move:
        L.append(f"Typical weekly move: ±${weekly_move:,.0f} → ${tech['price'] - weekly_move:,.0f} – ${tech['price'] + weekly_move:,.0f}")
    L.append("")
    L.append("🌍 Macro")
    for name, _, s, desc in macro["parts"]:
        L.append(f"{'🟢' if s > 10 else '🔴' if s < -10 else '⚪'} {desc} ({s:+.0f})")
    L.append("")
    L.append(f"📰 News ({len(headlines)} headlines, last {NEWS_DAYS} days)")
    if ai:
        L.append(ai["summary"])
        if ai["bullish_drivers"]:
            L.append("Bullish: " + "; ".join(ai["bullish_drivers"][:5]))
        if ai["bearish_drivers"]:
            L.append("Bearish: " + "; ".join(ai["bearish_drivers"][:5]))
        if ai["week_ahead"]:
            L.append("")
            L.append("🗓 Week ahead: " + "; ".join(ai["week_ahead"][:6]))
        L.append(f"(Keyword model for comparison: {kw['score']:+.0f})")
    else:
        if kw["bullish"]:
            L.append("Bullish themes: " + ", ".join(kw["bullish"]))
        if kw["bearish"]:
            L.append("Bearish themes: " + ", ".join(kw["bearish"]))
        for label_, ex in list(kw["examples"].items())[:4]:
            L.append(f"• {ex[0]}")
    L.append("")
    L.append("Not financial advice. Scores are rules-based and can be wrong, especially around big data releases.")
    return "\n".join(L)


def telegram_settings(cfg):
    """The gold outlook has its own bot, separate from the watch scanner's."""
    token = os.environ.get("GOLD_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("GOLD_TELEGRAM_CHAT_ID") or str(cfg.get("telegram_chat_id", ""))
    return token, chat_id


def send_telegram(text, token, chat_id):
    lines = text.split("\n")
    first, rest = html.escape(lines[0]), html.escape("\n".join(lines[1:]))
    rest = re.sub(r"^(Verdict: .*)$", r"<b>\1</b>", rest, count=1, flags=re.M)
    body = f"<b>{first}</b>\n{rest}"
    while body:  # Telegram's limit is 4096 characters per message
        cut = len(body) if len(body) <= 4000 else body.rfind("\n", 0, 4000)
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": body[:cut], "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"User-Agent": USER_AGENT})
        urllib.request.urlopen(req, timeout=20).read()
        body = body[cut:].lstrip("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the report, don't send it")
    ap.add_argument("--no-ai", action="store_true", help="use the keyword news model even if a Claude key is set")
    ap.add_argument("--test-alert", action="store_true", help="send one test message to the gold Telegram bot")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    token, chat_id = telegram_settings(cfg)
    if not args.dry_run and not (token and chat_id):
        log("GOLD_TELEGRAM_BOT_TOKEN or the chat id isn't set; use --dry-run or set them.")
        return 1
    if args.test_alert:
        send_telegram("✅ Gold outlook bot is connected.", token, chat_id)
        log("Test message sent.")
        return 0
    report = build_report(cfg, use_ai=not args.no_ai)
    REPORT_PATH.write_text(report + "\n")
    print(report)
    if not args.dry_run:
        send_telegram(report, token, chat_id)
        log("Sent to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
