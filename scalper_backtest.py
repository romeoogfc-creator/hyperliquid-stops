"""
SCALPER BACKTEST  (paper only - no keys are loaded, no orders can be sent)

Replays the live 15-minute crypto scalper on Hyperliquid's own history and compares it, over the SAME window, with
the daily LS-15 Gaussian Channel strategy. One email, one chart, one table.

  S1  "Scalper V4.12 (live)"     - the rules running in bot_engine.py today. Stop / profit-lock math is imported
                                   straight from bot_engine.py, so it cannot drift from the live bot.
  S2  "Scalper V4.8.12 (orig)"   - the version that went 5W/1L: top 20 gainers, no volatility filter, +0.4% first lock
  A   "LS-15 Original"           - daily Gaussian Channel (gc_paper.py)
  D   "LS-15 + Long Lock"        - daily Gaussian Channel, profit lock on longs only (gc_paper.py)

Hyperliquid only serves the latest 5,000 candles per size, so 15-minute history = about 52 days.
"""
import os
import time
import math
from datetime import datetime, timezone

import warnings
import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning)   # nan-means on coins with gaps are expected

try:
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")
except Exception:
    CT = None

import bot_engine as live          # the live bot: same constants and the same stop / profit-lock function
import gc_paper as ls15            # the LS-15 paper engine

START_NAV = 1000.0
BAR_MS = 15 * 60 * 1000
BARS_PER_DAY = 96
WINDOW = 32                        # 8h of 15m candles, as the live bot
TAKER_FEE = 0.00045
MAKER_FEE = 0.00015
MAX_COINS = 160                    # most-traded perps today (the scan picks the top gainers among them each bar)


# ======================================================================================
# V4.8.12 original stop / profit lock (copied from the V4.8.12 file you shared)
# ======================================================================================
def v4812_targets(entry_px, current_px, peak_roe, upper_wick_pct):
    roe = (current_px - entry_px) / entry_px * 5.0
    if peak_roe >= 0.12:
        floor = max(peak_roe * 0.93, peak_roe - 0.008)
    elif peak_roe >= 0.08:
        floor = max(peak_roe * 0.90, peak_roe - 0.010)
    elif peak_roe >= 0.05:
        floor = max(peak_roe * 0.85, peak_roe - 0.012)
    elif peak_roe >= 0.025:
        floor = max(peak_roe * 0.80, 0.008)
    elif peak_roe >= 0.012:
        floor = 0.004
    else:
        floor = -0.035
    if peak_roe >= 0.045 and (peak_roe - roe >= 0.006 or upper_wick_pct >= 0.45):
        floor = max(floor, peak_roe - 0.004)
    return entry_px * (1 + floor / 5.0)


BOOKS = [
    {"code": "S1", "name": "Scalper V4.12 (live)", "top_n": live.SCAN_TOP_N, "skip_majors_first": True,
     "min_change": live.MIN_24H_CHANGE_PCT, "atr": (live.MIN_ATR_PCT, live.MAX_ATR_PCT), "targets": "live"},
    {"code": "S2", "name": "Scalper V4.8.12 (orig)", "top_n": 20, "skip_majors_first": False,
     "min_change": None, "atr": None, "targets": "v4812"},
]


# ======================================================================================
# Data
# ======================================================================================
def fetch(info):
    meta, ctxs = info.meta_and_asset_ctxs()
    names, listed_idx, vol = [], {}, []
    for i, (a, c) in enumerate(zip(meta.get("universe", []), ctxs)):
        if a.get("isDelisted"):
            continue
        listed_idx[a["name"]] = i
        vol.append((float(c.get("dayNtlVlm", 0) or 0), a["name"]))
    vol.sort(reverse=True)
    names = [n for _, n in vol[:MAX_COINS]]
    if "BTC" not in names:
        names.insert(0, "BTC")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 5000 * BAR_MS
    series = {}
    for n in names:
        time.sleep(0.4)
        candles = None
        for attempt in range(3):
            try:
                candles = info.candles_snapshot(n, "15m", start_ms, now_ms)
                break
            except Exception:
                time.sleep(3 + attempt * 3)
        if candles:
            series[n] = {int(k["t"]): (float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]))
                         for k in candles if int(k["t"]) + BAR_MS <= now_ms}
    times = sorted(series.get("BTC", {}))
    coins = [n for n in names if n in series]
    T, N = len(times), len(coins)
    arr = {k: np.full((N, T), np.nan) for k in "ohlcv"}
    pos = {t: j for j, t in enumerate(times)}
    for i, n in enumerate(coins):
        for t, row in series[n].items():
            j = pos.get(t)
            if j is not None:
                for k, v in zip("ohlcv", row):
                    arr[k][i, j] = v
    return coins, listed_idx, times, arr


# ======================================================================================
# Indicators for every coin at bar t (vectorised across coins, same windowed EMA as the live bot)
# ======================================================================================
def window_ema(x, span):
    """pandas ewm(span, adjust=False) over each row of x (coins x window), seeded with the first value."""
    a = 2.0 / (span + 1.0)
    out = np.empty_like(x)
    out[:, 0] = x[:, 0]
    for j in range(1, x.shape[1]):
        out[:, j] = a * x[:, j] + (1 - a) * out[:, j - 1]
    return out


_IND_CACHE = {}


def indicators_at(arr, t):
    """Indicators are the same for every book at a given bar, so compute once and share."""
    if t not in _IND_CACHE:
        _IND_CACHE[t] = _indicators_at(arr, t)
    return _IND_CACHE[t]


def _indicators_at(arr, t):
    sl = slice(t - WINDOW + 1, t + 1)
    o, h, l, c, v = (arr[k][:, sl] for k in "ohlcv")
    ok = ~np.isnan(c).any(axis=1) & ~np.isnan(arr["c"][:, t - BARS_PER_DAY]) & ~np.isnan(v).any(axis=1)
    c_f = np.nan_to_num(c, nan=1.0)
    ema9 = window_ema(c_f, 9)
    macd = window_ema(c_f, 12) - window_ema(c_f, 26)
    hist = macd[:, -1] - window_ema(macd, 9)[:, -1]
    tp = (h + l + c) / 3.0
    vsum = np.nansum(v, axis=1)
    vwap = np.where(vsum > 0, np.nansum(tp * v, axis=1) / np.where(vsum > 0, vsum, 1), c[:, -1])
    prev24 = arr["c"][:, t - BARS_PER_DAY]
    vol24 = np.nansum(arr["v"][:, t - BARS_PER_DAY + 1:t + 1] * arr["c"][:, t - BARS_PER_DAY + 1:t + 1], axis=1)
    avg_vol = np.nanmean(v[:, -12:-2], axis=1)
    rng = np.maximum(h[:, -1] - l[:, -1], 1e-12)
    done = slice(t - live.ATR_CANDLES, t)
    atr = np.nanmean((arr["h"][:, done] - arr["l"][:, done]) / arr["c"][:, done], axis=1) * 100
    return {
        "ok": ok, "close": c[:, -1], "prev_close": c[:, -2], "ema9": ema9[:, -1], "prev_ema9": ema9[:, -2], "hist": hist,
        "vwap": vwap, "day_high": np.nanmax(h, axis=1), "chg24": (c[:, -1] / prev24 - 1) * 100, "vol24": vol24,
        "rvol": np.where(avg_vol > 0, v[:, -1] / np.where(avg_vol > 0, avg_vol, 1), 0.0),
        "lower_wick": (np.minimum(o[:, -1], c[:, -1]) - l[:, -1]) / rng,
        "upper_wick": (h[:, -1] - np.maximum(o[:, -1], c[:, -1])) / rng,
        "cap_low": l[:, -1] <= np.nanmin(l[:, -4:-1], axis=1), "low": l[:, -1], "prev_low": l[:, -2], "atr": atr,
    }


def btc_regimes(arr, coins, times):
    """Same rule as the live check_btc_regime, on 1h closes built from BTC's 15m candles."""
    b = coins.index("BTC")
    out = ["CAUTION"] * len(times)
    closes_1h = []
    for j, t in enumerate(times):
        if (t + BAR_MS) % 3600000 == 0 and not np.isnan(arr["c"][b, j]):
            closes_1h.append(arr["c"][b, j])
        if len(closes_1h) >= 55:
            s = np.array(closes_1h[-80:])
            e20, e50 = window_ema(s[None, :], 20)[0, -1], window_ema(s[None, :], 50)[0, -1]
            last, chg4h = s[-1], (s[-1] / s[-5] - 1) * 100
            out[j] = "RISK_OFF" if (last < e50 and e20 < e50) else "CAUTION" if (last < e50 or chg4h < -0.5) else "RISK_ON"
    return out


def ct_day(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (dt.astimezone(CT) if CT else dt).strftime("%Y-%m-%d")


# ======================================================================================
# Simulation
# ======================================================================================
def simulate(cfg, coins, listed_idx, times, arr, regimes):
    T = len(times)
    nav0 = START_NAV
    realized = 0.0
    positions = {}            # coin -> dict
    pending = None            # resting limit entry from the previous bar
    closed, equity = [], []
    cooldown, last_bar, hibernate_until = {}, {}, -1
    day_start = {}
    fees = 0.0
    coin_i = {c: i for i, c in enumerate(coins)}
    majors = {c for c in coins if listed_idx.get(c, 999) < live.SKIP_FIRST_LISTED}

    def nav_at(j):
        u = 0.0
        for c, p in positions.items():
            px = arr["c"][coin_i[c], j]
            if not np.isnan(px):
                u += p["notional"] * (px / p["entry"] - 1)
        return nav0 + realized + u

    def close(c, j, px, reason, fee_rate=TAKER_FEE):
        nonlocal realized, fees
        p = positions.pop(c)
        gross = p["notional"] * (px / p["entry"] - 1)
        f = p["notional"] * (px / p["entry"]) * fee_rate
        realized += gross - f
        fees += f
        pnl = gross - f - p["entry_fee"]
        roe = pnl / (p["notional"] / live.LEVERAGE) * 100
        closed.append({"coin": c, "t": times[j], "pnl": pnl, "roe": roe, "reason": reason, "setup": p["setup"]})
        cooldown[c] = j + (live.WIN_COOLDOWN_SEC if pnl > 0 else live.LOSS_COOLDOWN_SEC) // 900

    start = max(WINDOW, BARS_PER_DAY + 1, live.ATR_CANDLES + 1)
    for j in range(start, T):
        day = ct_day(times[j])
        if day not in day_start:
            day_start[day] = nav_at(j - 1)

        # resting limit entry placed last bar: fills if this bar trades through it (maker fee), else purged
        if pending is not None:
            c, lim, setup, low0 = pending
            i = coin_i[c]
            if c not in positions and len(positions) < live.MAX_CRYPTO_SLOTS and arr["l"][i, j] <= lim:
                open_pos(positions, c, lim, j, setup, low0, nav_at(j), MAKER_FEE, cfg)
                realized -= positions[c]["entry_fee"]
                fees += positions[c]["entry_fee"]
            pending = None

        # manage open positions on this bar
        for c in list(positions):
            p, i = positions[c], coin_i[c]
            o, h, l, cl = (arr[k][i, j] for k in "ohlc")
            if np.isnan(cl):
                continue
            if l <= p["stop"]:                                   # resting exchange stop
                close(c, j, min(o, p["stop"]), "🛡️ Stop / lock fill (resting)")
                continue
            p["peak_px"] = max(p["peak_px"], h)
            peak_roe = (p["peak_px"] / p["entry"] - 1) * live.LEVERAGE
            roe = (cl / p["entry"] - 1) * live.LEVERAGE
            held = (j - p["j"]) * 15
            wick = (h - max(o, cl)) / max(h - l, 1e-12)
            stop = (live.calculate_smart_exchange_targets(p["entry"], True, cl, p["low0"], peak_roe=peak_roe, upper_wick_pct=wick)[0]
                    if cfg["targets"] == "live" else v4812_targets(p["entry"], cl, peak_roe, wick))
            if held >= live.SCRATCH_MINS and roe < live.SCRATCH_ROE:
                close(c, j, cl, "⏱️ 15m scratch")
            elif held >= live.FLAT_EXIT_MINS and roe < live.FLAT_EXIT_ROE:
                close(c, j, cl, "⏱️ 30m flat")
            elif cl <= stop:
                close(c, j, cl, "🎯 Profit lock" if peak_roe >= live.FIRST_LOCK_TRIGGER_ROE else "🛡️ Hard stop")
            else:
                p["stop"] = stop

        # circuit breakers
        recent = [x for x in closed if x["pnl"] < 0 and times[j] - x["t"] <= live.CB_WINDOW_SEC * 1000]
        if len(recent) >= live.CB_MAX_LOSSES and j >= hibernate_until:
            hibernate_until = j + live.CB_HIBERNATE_SEC // 900
        today_real = sum(x["pnl"] for x in closed if ct_day(x["t"]) == day)
        blocked = j < hibernate_until or today_real <= -live.DAILY_LOSS_LIMIT_PCT * day_start[day]
        regime = regimes[j]
        if regime == "RISK_OFF" and live.BLOCK_ENTRIES_WHEN_RISK_OFF:
            blocked = True

        # scan
        if not blocked and len(positions) < live.MAX_CRYPTO_SLOTS and j < T - 1:
            ind = indicators_at(arr, j)
            strict = regime != "RISK_ON"
            req_rvol = 2.80 if strict else 1.80
            vcap = 1.0040 if strict else 1.0080
            elig = [(ind["chg24"][i], c) for c, i in coin_i.items()
                    if ind["ok"][i] and ind["vol24"][i] >= live.MIN_24H_VOLUME_USD and c not in positions]
            if cfg["skip_majors_first"]:
                elig = [e for e in elig if e[1] not in majors and (cfg["min_change"] is None or e[0] >= cfg["min_change"])]
            elig.sort(reverse=True)
            scan = elig[:cfg["top_n"]]
            if not cfg["skip_majors_first"]:
                scan = [e for e in scan if e[1] not in majors]      # V4.8.12 quirk: cut to 20 first, then drop majors
            best = None
            for _, c in scan:
                i = coin_i[c]
                if cooldown.get(c, -1) > j or last_bar.get(c) == j:
                    continue
                if cfg["atr"] and not (cfg["atr"][0] <= ind["atr"][i] <= cfg["atr"][1]):
                    continue
                px, e9, vw = ind["close"][i], ind["ema9"][i], ind["vwap"][i]
                if (px - e9) / e9 > 0.0100:
                    continue
                rv, vdist, macd_ok = ind["rvol"][i], px / vw, ind["hist"][i] > 0
                common = rv >= req_rvol and vdist <= vcap and macd_ok and px > e9
                brk = common and px >= ind["day_high"][i] * 0.99 and px > vw
                rec = common and (ind["prev_close"][i] <= ind["prev_ema9"][i] or ind["prev_close"][i] <= vw) and px > vw and px <= e9 * 1.0025
                vb = common and ind["cap_low"][i] and ind["lower_wick"][i] >= 0.45
                if not (brk or rec or vb):
                    continue
                setup, score = ("V_BOTTOM", rv * 3.2) if vb else ("RECLAIM", rv * 2.4) if rec else ("BREAKOUT", rv * 1.0)
                if best is None or score > best[0]:
                    best = (score, c, setup, px, min(px * 0.999, max(e9, vw)), min(ind["low"][i], ind["prev_low"][i]))
            if best:
                _, c, setup, px, lim, low0 = best
                last_bar[c] = j
                if setup == "BREAKOUT":
                    open_pos(positions, c, px, j, setup, low0, nav_at(j), TAKER_FEE, cfg)
                    realized -= positions[c]["entry_fee"]
                    fees += positions[c]["entry_fee"]
                else:
                    pending = (c, lim, setup, low0)

        equity.append((times[j], nav_at(j)))

    for c in list(positions):                                     # value open trades at the last close
        close(c, T - 1, arr["c"][coin_i[c], T - 1], "Open at end (marked to market)")
    equity.append((times[-1], nav0 + realized))
    return {"equity": equity, "closed": closed, "fees": fees}


def open_pos(positions, c, px, j, setup, low0, nav, fee_rate, cfg):
    notional = max(live.MIN_NOTIONAL_USD, nav * live.POSITION_FRACTION)
    stop = (live.calculate_smart_exchange_targets(px, True, px, low0)[0] if cfg["targets"] == "live"
            else v4812_targets(px, px, 0.0, 0.0))
    positions[c] = {"entry": px, "notional": notional, "j": j, "setup": setup, "low0": low0, "peak_px": px,
                    "stop": stop, "entry_fee": notional * fee_rate}


# ======================================================================================
# Report
# ======================================================================================
def stats_from(equity_vals, trades, fees, days):
    eq = [v for _, v in equity_vals]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    return {"ret": (eq[-1] / START_NAV - 1) * 100, "mdd": mdd * 100, "trades": len(trades), "per_day": len(trades) / max(days, 1),
            "wins": len(wins), "losses": len(losses), "winrate": len(wins) / len(trades) * 100 if trades else 0,
            "avg_w": np.mean([t["roe"] for t in wins]) if wins else 0.0, "avg_l": np.mean([t["roe"] for t in losses]) if losses else 0.0,
            "pf": gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0), "fees": fees}


def main():
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    coins, listed_idx, times, arr = fetch(info)
    regimes = btc_regimes(arr, coins, times)
    start_ms, end_ms = times[max(WINDOW, BARS_PER_DAY + 1)], times[-1]
    days = (end_ms - start_ms) / 86400000

    results = {}
    for cfg in BOOKS:
        r = simulate(cfg, coins, listed_idx, times, arr, regimes)
        exits = {}
        for t in r["closed"]:
            exits[t["reason"]] = exits.get(t["reason"], 0) + 1
        results[cfg["code"]] = {"name": cfg["name"], "equity": r["equity"], "stats": stats_from(r["equity"], r["closed"], r["fees"], days),
                                "exits": exits, "closed": r["closed"]}

    # LS-15 over the same window (daily candles)
    window_days = int(math.ceil(days))
    ranked, data = ls15.fetch_market(info, window_days)
    first_day = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    all_days = sorted({d for c in data for d in data[c] if d >= first_day})
    for code, name, lock, guard in [b for b in ls15.BOOKS if b[0] in ("A", "D")]:
        book = ls15.new_book(code, name, lock, guard)
        for d in all_days:
            ls15.step_day(book, d, data, ranked[:ls15.UNIVERSE_SIZE], ls15.btc_downtrend(data, d), False, [], [])
        last = all_days[-1]
        prices = {c: data[c][last]["c"] for c in data if last in data[c]}
        eq = [(int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000), v) for d, v in book["equity"]]
        eq[-1] = (eq[-1][0], ls15.book_nav(book, prices))
        trades = [{"pnl": t["pnl"], "roe": t["pct"]} for t in book["closed"]]
        results["LS15-" + code] = {"name": f"LS-15 {code}: {name.replace('LS-15 ', '')}", "equity": eq,
                                   "stats": stats_from(eq, trades, 0.0, days), "exits": {}, "closed": book["closed"]}

    report(results, start_ms, end_ms, days, regimes)


def report(results, start_ms, end_ms, days, regimes):
    period = f"{datetime.fromtimestamp(start_ms/1000, tz=timezone.utc):%Y-%m-%d} → {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc):%Y-%m-%d} ({days:.0f} days)"
    chart = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 3.4), dpi=110)
        for (code, r), color in zip(results.items(), ["#dc2626", "#f59e0b", "#2563eb", "#0891b2"]):
            ax.plot([datetime.fromtimestamp(t / 1000) for t, _ in r["equity"]], [v / START_NAV * 100 - 100 for _, v in r["equity"]],
                    label=r["name"], color=color, linewidth=1.5)
        ax.axhline(0, color="#94a3b8", linewidth=0.8)
        ax.set_ylabel("Return %")
        ax.set_title(f"Same window: {period}", fontsize=9)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(alpha=0.25)
        fig.autofmt_xdate()
        fig.tight_layout()
        chart = "scalper_chart.png"
        fig.savefig(chart)
        plt.close(fig)
    except Exception as e:
        print(f"chart skipped: {e}")

    def col(v):
        return "#15803d" if v >= 0 else "#b91c1c"

    rows = ""
    for code, r in results.items():
        s = r["stats"]
        pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        fees = f"${s['fees']:,.2f}" if code.startswith("S") else "small"
        rows += (f"<tr><td><b>{r['name']}</b></td><td style='color:{col(s['ret'])};font-weight:700;'>{s['ret']:+.1f}%</td>"
                 f"<td>{s['mdd']:.1f}%</td><td>{s['trades']} ({s['per_day']:.1f}/day)</td><td>{s['winrate']:.0f}%</td>"
                 f"<td>{s['avg_w']:+.2f}% / {s['avg_l']:+.2f}%</td><td>{pf}</td><td>{fees}</td></tr>")
    exit_html = ""
    for code, r in results.items():
        if r["exits"]:
            exit_html += f"<div><b>{r['name']}</b>: " + " &bull; ".join(f"{k}: {v}" for k, v in sorted(r['exits'].items(), key=lambda kv: -kv[1])) + "</div>"
    setups = ""
    for code, r in results.items():
        if code.startswith("S") and r["closed"]:
            by = {}
            for t in r["closed"]:
                g = by.setdefault(t["setup"], [0, 0, 0.0])
                g[0] += 1
                g[1] += t["pnl"] > 0
                g[2] += t["pnl"]
            setups += f"<div><b>{r['name']}</b>: " + " &bull; ".join(
                f"{k} {v[0]} trades, {v[1]}W, ${v[2]:+,.2f}" for k, v in by.items()) + "</div>"
    reg = {k: regimes.count(k) for k in ("RISK_ON", "CAUTION", "RISK_OFF")}
    tot = max(sum(reg.values()), 1)
    best_ret = max(results.values(), key=lambda r: r["stats"]["ret"])
    best_ratio = max(results.values(), key=lambda r: r["stats"]["ret"] / max(abs(r["stats"]["mdd"]), 1.0))
    verdict = (f"Highest return: {best_ret['name']} ({best_ret['stats']['ret']:+.1f}%). "
               f"Best return per unit of drawdown: {best_ratio['name']}.")
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:700px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
  <div style="background:#0f172a;color:#fff;padding:14px;">
    <div style="font-size:15px;font-weight:700;">Scalper vs LS-15 — Backtest</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{period} • each book starts at $1,000 • paper only, no orders</div>
  </div>
  <div style="padding:12px;font-size:12px;">
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px;margin-bottom:12px;color:#1e3a8a;"><b>{verdict}</b></div>
    {"<img src='cid:scalperchart' style='width:100%;max-width:660px;'/>" if chart else ""}
    <table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:8px;" cellpadding="4">
      <tr style="background:#f1f5f9;"><th align="left">Strategy</th><th align="left">Return</th><th align="left">Worst drop</th><th align="left">Trades</th>
      <th align="left">Win rate</th><th align="left">Avg win / loss</th><th align="left">Profit factor</th><th align="left">Fees</th></tr>{rows}</table>
    <div style="margin-top:6px;font-size:10px;color:#64748b;">Scalper avg win / loss are ROE at 5x. LS-15 avg win / loss are price % at 1x.</div>
    <div style="margin-top:12px;font-weight:700;">How scalper trades ended</div>{exit_html or "<div>No trades</div>"}
    <div style="margin-top:10px;font-weight:700;">Scalper results by setup</div>{setups or "<div>No trades</div>"}
    <div style="margin-top:10px;font-weight:700;">BTC regime over the window</div>
    <div>RISK_ON {reg['RISK_ON']/tot*100:.0f}% &bull; CAUTION {reg['CAUTION']/tot*100:.0f}% &bull; RISK_OFF {reg['RISK_OFF']/tot*100:.0f}% of the time</div>
    <div style="margin-top:12px;border-top:1px solid #e2e8f0;padding-top:8px;font-size:10px;color:#64748b;">
      • Scalper replayed on 15m candles (Hyperliquid keeps ~52 days). Live bot checks every 5 minutes inside the forming candle; here signals use each closed 15m candle, peaks use 15m highs, resting stops fill at the stop (or the open if gapped through).<br>
      • S1 uses bot_engine.py's own stop / profit-lock function. Breakout entries fill at the close (taker 0.045%); reclaim / V-bottom entries are limit orders that fill on the next candle only if price trades through (maker 0.015%), else they are cancelled — like the live purge.<br>
      • No order-book spread/depth check (no history) and no funding. Universe = today's {MAX_COINS} most-traded perps (coins that died are missing).<br>
      • S2 uses the same BTC regime filter as S1 (V4.8.12's Gemini answers can't be replayed). LS-15 = daily Gaussian Channel from gc_paper.py over the same dates.
    </div>
  </div>
</div></body></html>"""
    text = "Scalper vs LS-15 backtest " + period + "\n" + "\n".join(
        f"{r['name']}: {r['stats']['ret']:+.1f}% (DD {r['stats']['mdd']:.1f}%), {r['stats']['trades']} trades" for r in results.values())
    print(text, flush=True)
    subject = "⚔️ Scalper vs LS-15 — " + " | ".join(f"{code}: {r['stats']['ret']:+.1f}%" for code, r in results.items())
    send(subject, html, text, chart)


def send(subject, html, text, chart):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage
    sender, password, to = os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECEIVER_EMAIL")
    if not (sender and password and to):
        print("[WARN] Email credentials missing; results printed only.")
        return
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)
    if chart and os.path.exists(chart):
        with open(chart, "rb") as f:
            img = MIMEImage(f.read(), "png")
        img.add_header("Content-ID", "<scalperchart>")
        img.add_header("Content-Disposition", "inline", filename=chart)
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent: {subject}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"Scalper backtest error: {e}"
        print(msg, flush=True)
        try:
            send(f"⚠️ {msg}", f"<pre>{msg}</pre>", msg, None)
        except Exception:
            pass
        raise
