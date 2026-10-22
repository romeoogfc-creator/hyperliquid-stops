"""
TR-GC-Crypto-LS-15 PAPER TRADER  (no orders are ever sent - no keys are even loaded)

Rebuilds the SIGNUM "TR-GC-Crypto-LS-15" long/short Gaussian Channel strategy on Hyperliquid daily candles and
runs it on two virtual accounts side by side:
  Book A  "LS-15 Original"      - the SIGNUM rules as written
  Book B  "LS-15 + Profit Lock" - same entries/top-ups/exits, plus the profit-lock ladder (keep 80% -> 97% of peak)
  Book C  "LS-15 + Account Guard" - same as A, but if the ACCOUNT gives back 40% of a +4% run-up, close everything
                                     (the $500 -> $530 -> $495 problem: lock ~$518 instead)
  Book D  "LS-15 + Long Lock"     - profit lock on LONGS only; shorts run on the original rules (backtest: the lock
                                     helped longs +$149 -> +$259 but cut shorts +$229 -> +$48)
  Book E  "LS-15 + Long Lock + Guard" - Book D plus the account guard
  Book F  "LS-15 @ 60% Size"      - Book A with every position 60% as big (less pain without touching the exits)
  Book G  "LS-15 Max 3 @ 15%"     - at most 3 positions (long or short), 15% of NAV each, filled in rank order
  Book H  "LS-15 Max 3 @ 15% + Long Lock" - Book G plus the long-only profit lock (the proposed live combination)

Modes (env MODE):
  paper    (default) process every daily candle closed since the last run, update both books, email the report
  backtest replay the same rules over the last BACKTEST_DAYS days and email the results (state is not touched)

Differences from SIGNUM that cannot be avoided (shown in every email):
  - Gaussian Channel computed here (DonovanWall: hlc3, 4 poles, period 144, multiplier 1.414), not SIGNUM's Trend Radar
  - "Market rank" = Hyperliquid open-interest value (Hyperliquid has no market cap)
  - "breakoutDate" = first daily close above the upper band since the channel last turned Green
"""
import os
import json
import math
import time
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import numpy as np

STRATEGY_ID = "TR-GC-Crypto-LS-15"
ENGINE_VERSION = "GC-PAPER-1.4"
MODE = os.getenv("MODE", "paper").strip().lower()
BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "365") or 365)
STATE_FILE = "gc_paper_state.json"
START_NAV = float(os.getenv("START_NAV", "1000") or 1000)   # starting balance of each NEW book (Run workflow option)
MIN_ORDER_USD = 10.0                   # Hyperliquid rejects orders under ~$10: entries / top-ups below this are skipped

# ---------------- Gaussian Channel (DonovanWall defaults) ----------------
GC_POLES = 4
GC_PERIOD = 144
GC_MULT = 1.414
GC_WARMUP_BARS = 200                   # recursive filter needs history before its values are trustworthy

# ---------------- LS-15 rules ----------------
UNIVERSE_SIZE = 100                    # TRSORTORDER: first 100 by rank
DRIFT_OUT_RANK = 120                   # paper mode: exit when rank falls below 120 (hysteresis so rank #99/#101 doesn't churn)
LONG_SIZE_RECENT = 0.08                # breakout <= 25 days ago
LONG_SIZE_OLD = 0.02
BREAKOUT_RECENT_DAYS = 25
MIN_TRADE_NAV = 0.01                   # skip entries/top-ups below 1% of NAV
TOPUP_MIN_ROE = 0.10
TOPUP_TARGET = 0.055
HEDGE_SHORT_SIZE = 0.03
BEAR_SHORT_SIZE = 0.05
BEAR_SHORT_TOUCH = 0.98                # high >= 0.98 x filter
SHORT_TP_RATIO = 0.65                  # close <= 0.65 x entry
FEE_RATE = 0.00045                     # Hyperliquid taker per side (funding not modelled)

# ---------------- Book B profit-lock ladder (price gain at 1x, peak from daily highs/lows) ----------------
# (peak gain reached, share of peak to keep). Below 10%: after +5% the stop moves to +1% so a winner never turns red.
LOCK_TIERS = [(1.00, 0.97), (0.60, 0.93), (0.35, 0.90), (0.20, 0.85), (0.10, 0.80)]
LOCK_FIRST_TRIGGER = 0.05
LOCK_FIRST_FLOOR = 0.01

# Book C account guard: once the book's value is GUARD_TRIGGER above its base (value when it was last flat / last reset),
# close all positions if it falls back to keeping only GUARD_KEEP of that run-up. $500 -> $530 -> exit near $518.
GUARD_TRIGGER = 0.04
GUARD_KEEP = 0.60

# (code, name, profit lock: False / "all" / "long", account guard)
BOOKS = [
    ("A", "LS-15 Original", False, False),
    ("B", "LS-15 + Profit Lock", "all", False),
    ("C", "LS-15 + Account Guard", False, True),
    ("D", "LS-15 + Long Lock", "long", False),
    ("E", "LS-15 + Long Lock + Guard", "long", True),
    ("F", "LS-15 @ 60% Size", False, False),
    ("G", "LS-15 Max 3 @ 15%", False, False),
    ("H", "LS-15 Max 3 @ 15% + Long Lock", "long", False),
]
# Extra per-book settings (books not listed use the LS-15 defaults).
#   size_mult: scales every entry and the top-up target.  max_pos / slot_size: at most N positions, each slot_size of NAV.
BOOK_OPTIONS = {
    "F": {"size_mult": 0.60},
    "G": {"max_pos": 3, "slot_size": 0.15},     # 15% each (14-17% range): $16.65 per position on a $111 account
    "H": {"max_pos": 3, "slot_size": 0.15},
}


def lock_applies(book, side):
    """Profit lock on all positions ("all", or True from older saved state), on longs only ("long"), or off."""
    mode = book.get("lock")
    return mode is True or mode == "all" or (mode == "long" and side == "LONG")


# ======================================================================================
# Indicator
# ======================================================================================
def gaussian_filter(values, alpha, poles):
    """N-pole Gaussian filter (Ehlers / DonovanWall f_filt9x):
    f[t] = a^N * src[t] + sum_k (-1)^(k+1) * C(N,k) * (1-a)^k * f[t-k].  Equivalent to N cascaded EMAs with the same alpha."""
    x = 1.0 - alpha
    coefs = [((-1) ** (k + 1)) * math.comb(poles, k) * x ** k for k in range(1, poles + 1)]
    out = np.zeros(len(values))
    for t, v in enumerate(values):
        f = (alpha ** poles) * v
        for k, ck in enumerate(coefs, start=1):
            if t - k >= 0:
                f += ck * out[t - k]
        out[t] = f
    return out


def gaussian_channel(bars):
    """bars: list of dicts with o,h,l,c. Returns per-bar filter, upper band, trend ('Green'/'Red'/'Grey')."""
    h = np.array([b["h"] for b in bars], dtype=float)
    l = np.array([b["l"] for b in bars], dtype=float)
    c = np.array([b["c"] for b in bars], dtype=float)
    src = (h + l + c) / 3.0
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    beta = (1 - math.cos(4 * math.asin(1) / GC_PERIOD)) / (math.pow(1.414, 2.0 / GC_POLES) - 1)
    alpha = -beta + math.sqrt(beta ** 2 + 2 * beta)
    filt = gaussian_filter(src, alpha, GC_POLES)
    filt_tr = gaussian_filter(tr, alpha, GC_POLES)
    upper = filt + filt_tr * GC_MULT
    trend = ["Grey"] + ["Green" if filt[i] > filt[i - 1] else "Red" if filt[i] < filt[i - 1] else "Grey" for i in range(1, len(filt))]
    return filt, upper, trend


def enrich(bars):
    """Adds filter/upper/trend and the LS-15 breakout date to each bar (in place). Returns bars."""
    if len(bars) < 3:
        return bars
    filt, upper, trend = gaussian_channel(bars)
    breakout = None
    for i, b in enumerate(bars):
        b["filter"], b["upper"], b["trend"] = float(filt[i]), float(upper[i]), trend[i]
        if i > 0 and trend[i] == "Green" and trend[i - 1] != "Green":
            breakout = None                                   # a new uptrend starts: wait for its first band break
        if i > 0 and breakout is None and b["c"] > b["upper"] and bars[i - 1]["c"] <= bars[i - 1]["upper"]:
            breakout = b["date"]
        b["breakout"] = breakout
        b["valid"] = i >= GC_WARMUP_BARS
    return bars


# ======================================================================================
# Paper book
# ======================================================================================
def new_book(code, name, lock, guard=False):
    book = {"code": code, "name": name, "lock": lock, "guard": guard, "cash": START_NAV, "positions": {}, "closed": [], "equity": [],
            "eq_base": START_NAV, "eq_hwm": START_NAV, "guard_hits": 0, "start_nav": START_NAV}
    book.update(BOOK_OPTIONS.get(code, {}))
    return book


def entry_size(book, nav, base_fraction):
    """Position size in USD: a fixed slot (max-N books) or the LS-15 size scaled by size_mult."""
    if book.get("max_pos"):
        return nav * book["slot_size"]
    return nav * base_fraction * book.get("size_mult", 1.0)


def slots_full(book):
    return bool(book.get("max_pos")) and len(book["positions"]) >= book["max_pos"]


def pos_value(p, px):
    if p["side"] == "LONG":
        return p["qty"] * px
    return p["collateral"] + (p["entry"] - px) * p["qty"]


def book_nav(book, prices):
    return book["cash"] + sum(pos_value(p, prices.get(c, p["last_px"])) for c, p in book["positions"].items())


def lock_floor(peak_gain):
    for trigger, keep in LOCK_TIERS:
        if peak_gain >= trigger:
            return peak_gain * keep
    if peak_gain >= LOCK_FIRST_TRIGGER:
        return LOCK_FIRST_FLOOR
    return None


def open_position(book, coin, side, usd, px, date, reason, log):
    fee = usd * FEE_RATE
    if usd + fee > book["cash"]:
        usd = max(0.0, book["cash"] / (1 + FEE_RATE))
        fee = usd * FEE_RATE
    qty = usd / px
    book["cash"] -= usd + fee
    book["positions"][coin] = {"side": side, "qty": qty, "entry": px, "cost": usd, "collateral": usd if side == "SHORT" else 0.0,
                               "fees": fee, "opened": date, "peak_px": px, "stop": None, "last_px": px, "reason": reason}
    log.append(("ENTRY", coin, f"{side} {reason} — ${usd:,.2f} @ {px:.6g}"))


def topup_position(book, coin, usd, px, date, log):
    p = book["positions"][coin]
    fee = usd * FEE_RATE
    qty = usd / px
    p["entry"] = (p["entry"] * p["qty"] + px * qty) / (p["qty"] + qty)
    p["qty"] += qty
    p["cost"] += usd
    p["fees"] += fee
    book["cash"] -= usd + fee
    log.append(("TOP-UP", coin, f"+${usd:,.2f} @ {px:.6g} (avg entry now {p['entry']:.6g})"))


def close_position(book, coin, px, date, reason, log):
    p = book["positions"].pop(coin)
    value = pos_value(p, px)
    fee = p["qty"] * px * FEE_RATE
    book["cash"] += value - fee
    move = (p["entry"] - px) if p["side"] == "SHORT" else (px - p["entry"])
    pnl = move * p["qty"] - p["fees"] - fee
    gain = (px / p["entry"] - 1) if p["side"] == "LONG" else (1 - px / p["entry"])
    peak_gain = (p["peak_px"] / p["entry"] - 1) if p["side"] == "LONG" else (1 - p["peak_px"] / p["entry"])
    book["closed"].append({"coin": coin, "side": p["side"], "opened": p["opened"], "closed": date, "entry": p["entry"], "exit": px,
                           "pnl": round(pnl, 4), "pct": round(gain * 100, 2), "peak_pct": round(max(peak_gain, 0) * 100, 2),
                           "reason": reason, "why_in": p["reason"]})
    log.append(("EXIT", coin, f"{p['side']} {reason} — {gain*100:+.2f}% (peak {max(peak_gain,0)*100:+.2f}%), ${pnl:+,.2f}"))


def step_day(book, day, data, ranked, btc_down, drift_exits, log, skips):
    """Runs one LS-15 decision cycle on the daily candle `day` (YYYY-MM-DD) for one book."""
    bar = {c: data[c][day] for c in data if day in data[c]}
    prev_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    prev = {c: data[c][prev_day] for c in data if prev_day in data[c]}
    closes = {c: b["c"] for c, b in bar.items()}
    rank_of = {c: i + 1 for i, c in enumerate(ranked)}
    universe = ranked[:UNIVERSE_SIZE]
    exited, entered = set(), set()

    for c, p in book["positions"].items():
        if c in closes:
            p["last_px"] = closes[c]

    # Profit-lock books: resting stop (set at the end of yesterday) can fill during today's candle.
    if book.get("lock"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            if not b or p["stop"] is None or not lock_applies(book, p["side"]):
                continue
            if p["side"] == "LONG" and b["l"] <= p["stop"]:
                close_position(book, c, min(b["o"], p["stop"]), day, f"🔒 Profit lock (stop {p['stop']:.6g})", log)
                exited.add(c)
            elif p["side"] == "SHORT" and b["h"] >= p["stop"]:
                close_position(book, c, max(b["o"], p["stop"]), day, f"🔒 Profit lock (stop {p['stop']:.6g})", log)
                exited.add(c)

    nav = book_nav(book, closes)

    # Book C: account guard on today's close.
    if book.get("guard"):
        if not book["positions"]:
            book["eq_base"] = book["eq_hwm"] = nav            # flat: measure the next run-up from here
        book["eq_hwm"] = max(book["eq_hwm"], nav)
        run_up = book["eq_hwm"] - book["eq_base"]
        if book["positions"] and run_up >= GUARD_TRIGGER * book["eq_base"] and nav <= book["eq_base"] + GUARD_KEEP * run_up:
            why = (f"🛡️ Account guard: value ${nav:,.2f} gave back {(book['eq_hwm']-nav)/run_up*100:.0f}% of the "
                   f"${book['eq_base']:,.2f} → ${book['eq_hwm']:,.2f} run-up")
            for c in list(book["positions"]):
                b = bar.get(c)
                close_position(book, c, b["c"] if b else book["positions"][c]["last_px"], day, why, log)
                exited.add(c)
            book["guard_hits"] += 1
            nav = book_nav(book, closes)
            book["eq_base"] = book["eq_hwm"] = nav

    # 4) LONG EXITS
    for c in list(book["positions"]):
        p = book["positions"][c]
        if p["side"] != "LONG":
            continue
        b = bar.get(c)
        if drift_exits and rank_of.get(c, 10 ** 6) > DRIFT_OUT_RANK:
            close_position(book, c, b["c"] if b else p["last_px"], day, "Drifted out of top ranks", log)
            exited.add(c)
        elif not b or not b.get("valid"):
            skips.append((c, "Long held but indicator data missing — kept"))
        elif b["c"] < b["upper"]:
            close_position(book, c, b["c"], day, "Close below upper GC band", log)
            exited.add(c)

    # 5) SHORT EXITS
    for c in list(book["positions"]):
        p = book["positions"][c]
        if p["side"] != "SHORT":
            continue
        b = bar.get(c)
        if drift_exits and rank_of.get(c, 10 ** 6) > DRIFT_OUT_RANK:
            close_position(book, c, b["c"] if b else p["last_px"], day, "Drifted out of top ranks", log)
            exited.add(c)
        elif not b or not b.get("valid"):
            skips.append((c, "Short held but indicator data missing — kept"))
        elif b["c"] > b["filter"]:
            close_position(book, c, b["c"], day, "Stop: close above GC filter", log)
            exited.add(c)
        elif b["c"] <= SHORT_TP_RATIO * p["entry"]:
            close_position(book, c, b["c"], day, "Take profit (-35%)", log)
            exited.add(c)

    nav = book_nav(book, closes)

    # 6) LONG ENTRIES (rank order)
    for c in universe:
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid"):
            continue
        if not (b["c"] > b["upper"] and pb["c"] <= pb["upper"]):
            continue
        if c in exited:
            skips.append((c, "Long signal, but exited earlier this run")); continue
        if c in book["positions"]:
            skips.append((c, f"Long signal, already holding {book['positions'][c]['side']}")); continue
        if slots_full(book):
            skips.append((c, f"Long signal, all {book['max_pos']} slots in use")); continue
        recent = b.get("breakout") and (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(b["breakout"], "%Y-%m-%d")).days <= BREAKOUT_RECENT_DAYS
        want = entry_size(book, nav, LONG_SIZE_RECENT if recent else LONG_SIZE_OLD)
        usd = min(want, book["cash"] / (1 + FEE_RATE))
        if usd < MIN_ORDER_USD:
            skips.append((c, f"Long signal, ${usd:,.2f} is below Hyperliquid's ${MIN_ORDER_USD:.0f} minimum order")); continue
        if usd < nav * MIN_TRADE_NAV:
            skips.append((c, f"Long signal, not enough cash (${usd:,.2f} < 1% NAV)")); continue
        tag = f"band breakout ({'recent' if recent else 'old'} breakout, {want / nav * 100:.0f}% NAV{', downsized' if usd < want - 0.01 else ''})"
        open_position(book, c, "LONG", usd, b["c"], day, tag, log)
        entered.add(c)

    # 7) LONG TOP-UPS (rank order, free cash only)
    for c in universe:
        p, b = book["positions"].get(c), bar.get(c)
        if not p or p["side"] != "LONG" or c in entered or c in exited or not b or not b.get("valid") or book.get("max_pos"):
            continue                                          # max-N books already hold full-size slots: no top-ups
        if b["c"] <= b["upper"]:
            continue
        roe = (b["c"] - p["entry"]) / p["entry"]
        held = p["qty"] * b["c"]
        target = nav * TOPUP_TARGET * book.get("size_mult", 1.0)
        if roe < TOPUP_MIN_ROE or held >= target:
            continue
        usd = min(target - held, book["cash"] / (1 + FEE_RATE))
        if usd < max(nav * MIN_TRADE_NAV, MIN_ORDER_USD):
            skips.append((c, f"Top-up wanted (ROE {roe*100:+.1f}%), ${usd:,.2f} is below 1% NAV / the ${MIN_ORDER_USD:.0f} minimum")); continue
        topup_position(book, c, usd, b["c"], day, log)

    # 8) SHORT ENTRIES (rank order)
    for c in universe:
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid") or c in book["positions"]:
            continue
        hedge = b["trend"] == "Red" and b["c"] < b["filter"] and pb["c"] >= pb["filter"]
        bear = btc_down and b["trend"] == "Red" and b["h"] >= BEAR_SHORT_TOUCH * b["filter"] and b["c"] < b["filter"]
        if not (hedge or bear):
            continue
        if c in exited:
            skips.append((c, "Short signal, but exited earlier this run")); continue
        if slots_full(book):
            skips.append((c, f"Short signal, all {book['max_pos']} slots in use")); continue
        want = entry_size(book, nav, HEDGE_SHORT_SIZE if hedge else BEAR_SHORT_SIZE)
        usd = min(want, book["cash"] / (1 + FEE_RATE))
        if usd < MIN_ORDER_USD:
            skips.append((c, f"Short signal, ${usd:,.2f} is below Hyperliquid's ${MIN_ORDER_USD:.0f} minimum order")); continue
        if usd < nav * MIN_TRADE_NAV:
            skips.append((c, f"Short signal, not enough cash (${usd:,.2f} < 1% NAV)")); continue
        open_position(book, c, "SHORT", usd, b["c"], day, "hedge short (cross under filter)" if hedge else "bear short (rejected bounce, BTC down)", log)

    # Update peaks with today's candle and (profit-lock books) set tomorrow's resting stop.
    for c, p in book["positions"].items():
        b = bar.get(c)
        if not b:
            continue
        if p["side"] == "LONG":
            p["peak_px"] = max(p["peak_px"], b["h"])
            peak_gain = p["peak_px"] / p["entry"] - 1
        else:
            p["peak_px"] = min(p["peak_px"], b["l"])
            peak_gain = 1 - p["peak_px"] / p["entry"]
        if lock_applies(book, p["side"]):
            fl = lock_floor(peak_gain)
            p["stop"] = None if fl is None else (p["entry"] * (1 + fl) if p["side"] == "LONG" else p["entry"] * (1 - fl))

    point = [day, round(book_nav(book, closes), 2)]
    if book["equity"] and book["equity"][-1][0] == day:
        book["equity"][-1] = point                            # never record the same day twice
    else:
        book["equity"].append(point)


# ======================================================================================
# Data
# ======================================================================================
def fetch_market(info, history_days):
    """Returns (ranked coin list, {coin: {date: bar}}). Rank = open-interest value, majors included."""
    meta, ctxs = info.meta_and_asset_ctxs()
    rows = []
    for asset, ctx in zip(meta.get("universe", []), ctxs):
        if asset.get("isDelisted"):
            continue
        try:
            oi_usd = float(ctx.get("openInterest", 0) or 0) * float(ctx.get("markPx", 0) or 0)
        except (TypeError, ValueError):
            continue
        if oi_usd > 0:
            rows.append((oi_usd, asset["name"]))
    rows.sort(reverse=True)
    ranked = [c for _, c in rows]
    if "BTC" not in ranked[:UNIVERSE_SIZE]:
        ranked.insert(0, "BTC")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (history_days + GC_WARMUP_BARS + 30) * 86400000
    data = {}
    for coin in ranked[:DRIFT_OUT_RANK]:
        time.sleep(0.5)                        # ~120 coins: stay well inside Hyperliquid's request-weight limit
        candles = None
        for attempt in range(3):
            try:
                candles = info.candles_snapshot(coin, "1d", start_ms, now_ms)
                break
            except Exception:
                time.sleep(2 + attempt * 3)
        if not candles:
            continue
        bars = []
        for k in candles:
            t = int(k["t"])
            if t + 86400000 > now_ms:          # still-forming daily candle: not CLOSED, ignore
                continue
            bars.append({"date": datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                         "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
        enrich(bars)
        data[coin] = {b["date"]: b for b in bars}
    return ranked, data


def btc_downtrend(data, day):
    b = data.get("BTC", {}).get(day)
    return bool(b and b.get("valid") and b["c"] < b["filter"])


# ======================================================================================
# Reporting
# ======================================================================================
def book_start(book):
    return float(book.get("start_nav", 1000.0))           # books saved before GC-PAPER-1.3 started at $1,000


def book_stats(book, start_nav=None):
    start_nav = start_nav or book_start(book)
    closed = book["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    eq = [e[1] for e in book["equity"]] or [start_nav]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    capture = [t["pct"] / t["peak_pct"] for t in wins if t["peak_pct"] > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    return {
        "nav": eq[-1], "ret": (eq[-1] / start_nav - 1) * 100, "mdd": mdd * 100, "trades": len(closed),
        "wins": len(wins), "losses": len(losses), "winrate": len(wins) / len(closed) * 100 if closed else 0.0,
        "avg_w": np.mean([t["pct"] for t in wins]) if wins else 0.0, "avg_l": np.mean([t["pct"] for t in losses]) if losses else 0.0,
        "pf": gross_w / gross_l if gross_l > 0 else float("inf") if gross_w > 0 else 0.0,
        "capture": np.mean(capture) * 100 if capture else 0.0,
        "long_pnl": sum(t["pnl"] for t in closed if t["side"] == "LONG"),
        "short_pnl": sum(t["pnl"] for t in closed if t["side"] == "SHORT"),
        "longs": sum(1 for t in closed if t["side"] == "LONG"), "shorts": sum(1 for t in closed if t["side"] == "SHORT"),
    }


def make_chart(books, btc_curve, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=110)
    for book, color in zip(books, ["#2563eb", "#16a34a", "#9333ea", "#0891b2", "#db2777", "#64748b", "#ea580c", "#000000"]):
        if book["equity"]:
            ax.plot([datetime.strptime(d, "%Y-%m-%d") for d, _ in book["equity"]], [v / book_start(book) * 100 - 100 for _, v in book["equity"]],
                    label=f"{book['code']}: {book['name']}", color=color, linewidth=1.6)
    if btc_curve:
        ax.plot([datetime.strptime(d, "%Y-%m-%d") for d, _ in btc_curve], [v for _, v in btc_curve], label="BTC buy & hold",
                color="#f59e0b", linewidth=1, linestyle="--")
    ax.axhline(0, color="#94a3b8", linewidth=0.8)
    ax.set_ylabel("Return %")
    ax.set_title(title, fontsize=10)
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    path = "gc_chart.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def fmt_stop(p):
    return "—" if p.get("stop") is None else f"{p['stop']:.6g}"


def pct_color(v):
    return "#15803d" if v >= 0 else "#b91c1c"


def render_email(title, subtitle, books, day_logs, skips, prices, btc_curve, regime_line, chart_cid, notes):
    cards = ""
    for book in books:
        s = book_stats(book)
        nav = book_nav(book, prices) if prices else s["nav"]
        long_usd = sum(pos_value(p, prices.get(c, p["last_px"])) for c, p in book["positions"].items() if p["side"] == "LONG")
        short_usd = sum(p["qty"] * prices.get(c, p["last_px"]) for c, p in book["positions"].items() if p["side"] == "SHORT")
        flat_usd = book["cash"]
        tot = max(long_usd + short_usd + flat_usd, 1e-9)
        bar = (f"<div style='display:flex;height:12px;border-radius:4px;overflow:hidden;margin:6px 0;'>"
               f"<div style='width:{long_usd/tot*100:.1f}%;background:#16a34a;'></div>"
               f"<div style='width:{flat_usd/tot*100:.1f}%;background:#cbd5e1;'></div>"
               f"<div style='width:{short_usd/tot*100:.1f}%;background:#dc2626;'></div></div>")
        pos_rows = "".join(
            f"<tr><td>{c}</td><td style='color:{'#16a34a' if p['side']=='LONG' else '#dc2626'};'>{p['side']}</td><td>{p['entry']:.6g}</td>"
            f"<td>{prices.get(c, p['last_px']):.6g}</td>"
            f"<td style='color:{pct_color(((prices.get(c,p['last_px'])/p['entry']-1) if p['side']=='LONG' else (1-prices.get(c,p['last_px'])/p['entry'])))};'>"
            f"{(((prices.get(c,p['last_px'])/p['entry']-1) if p['side']=='LONG' else (1-prices.get(c,p['last_px'])/p['entry']))*100):+.2f}%</td>"
            f"<td>{fmt_stop(p)}</td><td>{p['opened']}</td></tr>"
            for c, p in sorted(book["positions"].items())) or "<tr><td colspan='7' style='color:#64748b;'>No open positions</td></tr>"
        recent = "".join(
            f"<tr><td>{t['closed']}</td><td>{t['coin']}</td><td>{t['side']}</td>"
            f"<td style='color:{pct_color(t['pct'])};'>{t['pct']:+.2f}%</td><td>{t['peak_pct']:+.2f}%</td><td>{t['reason']}</td></tr>"
            for t in list(reversed(book["closed"]))[:8]) or "<tr><td colspan='6' style='color:#64748b;'>No closed trades yet</td></tr>"
        cards += f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin-bottom:14px;background:#fff;">
          <div style="font-weight:700;font-size:14px;color:#0f172a;">Book {book['code']} — {book['name']}</div>
          <div style="font-size:22px;font-weight:700;color:{pct_color(s['ret'])};margin-top:4px;">${nav:,.2f} <span style="font-size:13px;">({(nav/book_start(book)-1)*100:+.2f}%)</span></div>
          <div style="font-size:11px;color:#475569;">Trades {s['trades']} ({s['wins']}W/{s['losses']}L, {s['winrate']:.0f}%) &bull;
            Avg win {s['avg_w']:+.2f}% / avg loss {s['avg_l']:+.2f}% &bull; Profit factor {s['pf']:.2f} &bull;
            Biggest account give-back from a high {s['mdd']:.1f}% &bull;{f" Account guard fired {book.get('guard_hits', 0)}x &bull;" if book.get("guard") else ""}
            <b>Kept {s['capture']:.0f}% of peak</b> on winners &bull; Longs {s['longs']} (${s['long_pnl']:+,.2f}) / Shorts {s['shorts']} (${s['short_pnl']:+,.2f})</div>
          <div style="font-size:11px;color:#334155;margin-top:6px;">Positioning: <span style="color:#16a34a;">Long ${long_usd:,.0f}</span> &bull;
            <span style="color:#64748b;">Flat ${flat_usd:,.0f}</span> &bull; <span style="color:#dc2626;">Short ${short_usd:,.0f}</span></div>{bar}
          <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
            <tr style="background:#f1f5f9;"><th align="left">Coin</th><th align="left">Side</th><th align="left">Entry</th><th align="left">Now</th><th align="left">P&amp;L</th><th align="left">Lock stop</th><th align="left">Since</th></tr>{pos_rows}</table>
          <div style="font-size:11px;font-weight:700;margin-top:8px;color:#475569;">Recent exits</div>
          <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
            <tr style="background:#f1f5f9;"><th align="left">Date</th><th align="left">Coin</th><th align="left">Side</th><th align="left">Result</th><th align="left">Peak</th><th align="left">Why</th></tr>{recent}</table>
        </div>"""
    activity = ""
    for day, entries in day_logs:
        if not entries:
            activity += f"<div style='color:#64748b;'>{day}: no signals</div>"
            continue
        activity += f"<div style='font-weight:700;margin-top:6px;'>{day}</div>"
        for code, kind, coin, msg in entries:
            color = {"ENTRY": "#2563eb", "EXIT": "#b45309", "TOP-UP": "#7c3aed"}.get(kind, "#334155")
            activity += f"<div>&bull; [{code}] <b style='color:{color};'>{kind}</b> {coin}: {msg}</div>"
    skip_html = "".join(f"<div>&bull; {c}: {why}</div>" for c, why in skips) or "<div style='color:#64748b;'>None</div>"
    chart = f"<img src='cid:{chart_cid}' style='width:100%;max-width:640px;border-radius:6px;'/>" if chart_cid else ""
    notes_html = "".join(f"<div>&bull; {n}</div>" for n in notes)
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
  <div style="background:#0f172a;color:#fff;padding:14px;">
    <div style="font-size:15px;font-weight:700;">{title}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{subtitle}</div>
  </div>
  <div style="padding:12px;">
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin-bottom:12px;">
      📝 PAPER TRADING ONLY — this script holds no keys and cannot place orders. {regime_line}</div>
    {chart}
    {cards}
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:12px;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">Activity (all books)</div>{activity or "<div style='color:#64748b;'>No new candles processed</div>"}</div>
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:12px;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">Skipped / not executed (and why)</div>{skip_html}</div>
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:10px;color:#64748b;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;">How this paper test works</div>{notes_html}</div>
  </div>
</div></body></html>"""


def send_email(subject, html, text, chart_path):
    sender, password, to = os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECEIVER_EMAIL")
    if not (sender and password and to):
        print("[WARN] Email credentials missing; report printed only.\n" + text, flush=True)
        return
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)
    if chart_path and os.path.exists(chart_path):
        with open(chart_path, "rb") as f:
            img = MIMEImage(f.read(), "png")
        img.add_header("Content-ID", "<gcchart>")
        img.add_header("Content-Disposition", "inline", filename="gc_chart.png")
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent: {subject}", flush=True)


NOTES = [
    "Virtual books (each with its own starting balance), all taking the same LS-15 entry signals. Book A = SIGNUM LS-15 rules as written. Book B = the same, plus a per-trade profit lock: "
    "after +5% the stop moves to +1%; after +10/20/35/60/100% it keeps 80/85/90/93/97% of the peak (resting stop, checked against each day's low/high).",
    "Book C = Book A plus an ACCOUNT guard: once the book is up 4%+ from where it was last flat, it closes everything if it falls back to keeping "
    "only 60% of that run-up (your $500 → $530 example would lock about $518 instead of $495).",
    "Book D = profit lock on LONGS only (shorts keep the original stop / -35% take-profit). Book E = Book D plus the account guard.",
    "Book F = Book A with every position 60% as big. Book G = at most 3 positions (long or short), 15% of NAV each, filled in rank order, no top-ups. "
    "Book H = Book G plus the long-only profit lock (the proposed live setup: LS-15 signals, max 3 positions, profit lock).",
    "Gaussian Channel computed here from Hyperliquid daily candles (hlc3, 4 poles, period 144, x1.414) — close to SIGNUM's Trend Radar, not identical.",
    "Rank = Hyperliquid open-interest value (no market cap on Hyperliquid). breakoutDate = first close above the upper band since the channel turned Green.",
    "Fills at the daily close (lock stops at the stop price, or the open if it gapped through). 0.045% fee per side. Funding is not modelled.",
    "Orders under $10 are skipped, as Hyperliquid would reject them (matters for small accounts: 2% of $111 = $2.22).",
]


def btc_buy_hold(data, days):
    btc = data.get("BTC", {})
    pts = [(d, btc[d]["c"]) for d in days if d in btc]
    if not pts:
        return []
    base = pts[0][1]
    return [(d, (c / base - 1) * 100) for d, c in pts]


# ======================================================================================
# Modes
# ======================================================================================
def run_backtest(info):
    ranked, data = fetch_market(info, BACKTEST_DAYS)
    universe = ranked[:UNIVERSE_SIZE]
    all_days = sorted({d for c in data for d in data[c]})
    days = all_days[-BACKTEST_DAYS:]
    books = [new_book(code, name, lock, guard) for code, name, lock, guard in BOOKS]
    min_order_skips = {b["code"]: 0 for b in books}
    for day in days:
        btc_down = btc_downtrend(data, day)
        for book in books:
            skips = []
            step_day(book, day, data, universe, btc_down, False, [], skips)
            min_order_skips[book["code"]] += sum(1 for _, why in skips if "minimum order" in why or "minimum" in why)
    last = days[-1] if days else ""
    prices = {c: data[c][last]["c"] for c in data if last in data[c]}
    btc_curve = btc_buy_hold(data, days)
    chart = make_chart(books, btc_curve, f"Backtest {days[0] if days else ''} → {last} (all books vs BTC)")
    title = f"{STRATEGY_ID} BACKTEST — {len(days)} days from ${START_NAV:,.0f}"
    btc_ret = btc_curve[-1][1] if btc_curve else 0.0
    summary = " | ".join(f"{b['code']}: {book_stats(b)['ret']:+.1f}% (DD {book_stats(b)['mdd']:.1f}%)" for b in books) + f" | BTC {btc_ret:+.1f}%"
    blocked = ", ".join(f"{k}: {v}" for k, v in min_order_skips.items() if v)
    notes = NOTES + [f"Starting balance ${START_NAV:,.2f} per book. Signals skipped because the order would be under ${MIN_ORDER_USD:.0f}: "
                     f"{blocked or 'none'}.", f"Backtest universe = today's top {UNIVERSE_SIZE} by open interest (coins that died earlier are missing, which flatters results slightly). "
                     "No drift-out exits in backtest (no historical ranks). Open positions are valued at the last close."]
    html = render_email(title, f"{days[0] if days else ''} → {last} • {summary}", books, [], [], prices, btc_curve,
                        f"BTC buy & hold over the same period: {btc_ret:+.1f}%.", "gcchart" if chart else None, notes)
    text = f"{title}\n{summary}\n" + "\n".join(f"{b['code']}: {book_stats(b)}" for b in books)
    print(text, flush=True)
    send_email(f"📊 {title} — {summary}", html, text, chart)


def run_paper(info):
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    books = state.get("books") or [new_book(code, name, lock, guard) for code, name, lock, guard in BOOKS]
    for code, name, lock, guard in BOOKS:                     # books added in later versions join with a fresh balance
        if code not in {b["code"] for b in books}:
            books.append(new_book(code, name, lock, guard))
    for b in books:                                           # settings changes (e.g. slot size) apply to saved books too
        b.update(BOOK_OPTIONS.get(b["code"], {}))
        b["name"] = next((n for c, n, _, _ in BOOKS if c == b["code"]), b["name"])
    last_done = state.get("last_processed_day")
    ranked, data = fetch_market(info, 60)
    btc_days = sorted(data.get("BTC", {}))
    if not btc_days:
        raise RuntimeError("No BTC daily candles returned — cannot run")
    latest = btc_days[-1]
    todo = [d for d in btc_days if (last_done is None and d == latest) or (last_done is not None and d > last_done)]
    if len(todo) > 7:
        todo = todo[-7:]                       # catch up at most a week after an outage
    day_logs, all_skips = [], []
    for day in todo:
        btc_down = btc_downtrend(data, day)
        entries = []
        for book in books:
            log, skips = [], []
            step_day(book, day, data, ranked, btc_down, True, log, skips)
            entries += [(book["code"], k, c, m) for k, c, m in log]
            all_skips += [(f"[{book['code']}] {day} {c}", why) for c, why in skips]
        day_logs.append((day, entries))
    state.update({"books": books, "last_processed_day": todo[-1] if todo else last_done, "engine": ENGINE_VERSION,
                  "started": state.get("started") or (todo[0] if todo else latest)})
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)

    prices = {c: data[c][latest]["c"] for c in data if latest in data[c]}
    btc_down = btc_downtrend(data, latest)
    btc_bar = data["BTC"][latest]
    regime_line = (f"BTC {latest} close ${btc_bar['c']:,.0f} vs GC filter ${btc_bar['filter']:,.0f} → "
                   f"{'DOWNTREND (bear shorts allowed)' if btc_down else 'not in downtrend'}.")
    eq_days = sorted({d for d, _ in books[0]["equity"]})
    btc_curve = btc_buy_hold(data, eq_days) if len(eq_days) > 1 else []
    chart = make_chart(books, btc_curve, f"Paper since {state['started']}") if len(eq_days) > 1 else None
    title = f"{STRATEGY_ID} PAPER — {latest}"
    summary = " | ".join(f"{b['code']} ${book_nav(b, prices):,.2f} ({(book_nav(b, prices)/book_start(b)-1)*100:+.2f}%)" for b in books)
    html = render_email(title, f"Paper since {state['started']} • processed: {', '.join(todo) or 'nothing new'} • {summary}", books, day_logs,
                        all_skips, prices, btc_curve, regime_line, "gcchart" if chart else None, NOTES)
    text = f"{title}\n{summary}\n{regime_line}\n" + "\n".join(f"{d}: {e}" for d, e in day_logs)
    print(text, flush=True)
    send_email(f"📝 {title} — {summary}", html, text, chart)


def main():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = None
    for attempt in range(3):
        try:
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            break
        except Exception:
            time.sleep(10)
    if info is None:
        raise RuntimeError("Could not reach Hyperliquid")
    if MODE == "backtest":
        run_backtest(info)
    else:
        run_paper(info)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"{STRATEGY_ID} paper engine error: {e}"
        print(msg, flush=True)
        try:
            send_email(f"⚠️ {msg}", f"<pre>{msg}</pre>", msg, None)
        except Exception:
            pass
        raise
