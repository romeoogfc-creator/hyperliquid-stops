"""
bot_engine.py — TR-GC-Crypto-LS-15 LIVE BOT (Hyperliquid, real money) — the "Book Z4" setup from gc_paper.py
(Book V's early lock + a -4% loss stop on every position. 100-order stability checks at $111, loss-stop ladder:
 -3% +150% | -3.5% +203% | -4% +248% | -4.5% +206% | -5% +181% | -6% +184% | -7% +165% (median, 2 years).
 -3.5% to -4.5% all beat -5%, so -4% sits in the middle of a broad high zone, not on a lone spike.)
(EARLY LOCK: keeps 50% of the peak gain from +3%, 60% from +5%, 70% from +10%, 80/85/90% from +20/35/60%.)
Replaces the old 5-minute scalper (V4.12) on the same account, workflow (stops.yml) and state file (state.json).

What it trades (same rules as the paper/backtest engine, which it imports for the indicator and the signals):
  - LS-15 Gaussian Channel signals on DAILY candles, longs AND shorts, top 100 coins by open interest
  - at most 9 positions, 15% of the account each, up to 135% of the account in positions (cross margin, 3x setting
    -> only ~45% of the account is used as margin, the rest stays free as a safety buffer)
  - longs: the early profit lock (keep 50-90% of the peak gain from +3%) as a REAL stop on Hyperliquid, raised every 4 hours
  - every position: a LOSS STOP 4% against the entry, resting on the exchange 24/7 (cut losers fast, wait in cash)
  - shorts: original LS-15 exits (daily close above the GC filter, or take profit at -35%)

Schedule: stops.yml runs it every 5 minutes (external cron) with a 15-minute backup. Most runs only check that every
stop order is in place (a few seconds). The first run after each 00:00 UTC daily close makes the daily decisions (exits,
then entries), and after every 4-hour candle closes the long profit-lock stops are raised.

DRY_RUN=true (default in the workflow until you switch it): reads the real account and market, decides everything,
emails what it WOULD do, sends no orders.
"""
import os
import json
import time
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gc_paper as engine

BOT_VERSION = "LS15-LIVE-1.6"
STATE_FILE = "state.json"                  # same file the old scalper used (stops.yml commits it)
ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS")
SECRET_KEY = os.getenv("HL_SECRET_KEY")
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() in ("1", "true", "yes")
IS_MANUAL = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"

MAX_POSITIONS = 9
SLOT_SIZE = 0.15                       # 15% of the account per position (notional)
GROSS_CAP = 1.35                       # at most 135% of the account in positions
LEVERAGE_SETTING = 3                   # cross margin setting per coin: margin used = notional / 3
MIN_ORDER_USD = 11.0                   # Hyperliquid minimum is $10; keep a small buffer for rounding / price moves
LOCK_TIERS = engine.BOOK_OPTIONS["V"]["lock_tiers"]   # early lock: (peak gain, share of it kept), same as paper Book V
DISASTER_STOP = 0.04                   # loss stop: resting stop 4% against every position (paper Book Z4)
SLIPPAGE = 0.01                        # market orders: worst price accepted 1% away
LOW_BALANCE_WARN = 67.0                # below this, 15% positions fall under $10 and the bot cannot open trades
H4_MS = 4 * 3600 * 1000


# ======================================================================================
# Helpers
# ======================================================================================
def api_retry(func, *args, retries=4, delay=2.0, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"[WARN] API error ({e}); retry in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay *= 2


def round_px(px, sz_decimals):
    """Hyperliquid perp prices: max 5 significant figures AND max (6 - szDecimals) decimals."""
    if not px or px <= 0:
        return 0.0
    return round(float(f"{px:.5g}"), max(0, 6 - int(sz_decimals)))


def round_sz_down(sz, sz_decimals):
    f = 10 ** int(sz_decimals)
    return int(sz * f) / f


def order_result(res):
    """(ok, statuses, error). The API can answer status 'ok' with per-order errors inside."""
    if not isinstance(res, dict) or res.get("status") != "ok":
        return False, [], f"{res}"
    statuses = (res.get("response", {}) or {}).get("data", {}).get("statuses", []) or []
    errors = [s.get("error") for s in statuses if isinstance(s, dict) and s.get("error")]
    return (not errors), statuses, "; ".join(errors)


def filled_of(statuses):
    """(total size, average price) of a market order, or (0, 0) if nothing filled."""
    for s in statuses:
        if isinstance(s, dict) and "filled" in s:
            return float(s["filled"].get("totalSz", 0) or 0), float(s["filled"].get("avgPx", 0) or 0)
    return 0.0, 0.0


def is_stop_order(o):
    if isinstance(o.get("orderType"), dict):
        return o["orderType"].get("trigger", {}).get("tpsl") == "sl"
    return bool(o.get("isTrigger")) and "stop" in str(o.get("orderType", "")).lower()


class DryRunExchange:
    """Stands in for the real Exchange: logs every order instead of sending it."""

    def __init__(self, log):
        self.log = log

    def _ok(self, msg, fill=None):
        self.log.append(f"🧪 DRY RUN — would {msg}")
        st = {"filled": {"totalSz": str(fill[0]), "avgPx": str(fill[1])}} if fill else {"resting": {"oid": 0}}
        return {"status": "ok", "response": {"type": "dry_run", "data": {"statuses": [st]}}}

    def market_open(self, coin, is_buy, sz, px=None, slippage=SLIPPAGE):
        return self._ok(f"{'BUY' if is_buy else 'SELL SHORT'} {sz} {coin} at market", (sz, px or 0))

    def market_close(self, coin, sz=None, px=None, slippage=SLIPPAGE):
        return self._ok(f"close {coin} at market")

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False):
        return self._ok(f"place {'BUY' if is_buy else 'SELL'} stop {sz} {coin} @ {px}")

    def cancel(self, coin, oid):
        return self._ok(f"cancel {coin} order #{oid}")

    def update_leverage(self, lev, coin, is_cross=True):
        return self._ok(f"set {coin} leverage {lev}x cross")


def load_state():
    fresh = {"positions": {}, "closed": [], "last_daily_day": None, "started": None, "start_nav": None, "nav_history": []}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        if str(state.get("version", "")).startswith("LS15"):
            return state
        print("[INFO] state.json is from the old scalper — starting a fresh LS-15 state", flush=True)
    return fresh


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ======================================================================================
# Account
# ======================================================================================
def read_account(info):
    user = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    mids = {k: float(v) for k, v in api_retry(info.all_mids).items()}
    spot_worth = 0.0
    for b in spot.get("balances", []):
        amt = float(b.get("total", 0) or 0)
        if amt > 0:
            spot_worth += amt if b.get("coin", "").upper() == "USDC" else amt * mids.get(b.get("coin", ""), 0.0)
    perp_value = float(user.get("marginSummary", {}).get("accountValue", 0) or 0)
    nav = spot_worth if spot_worth > 0 else perp_value          # same rule as the scalper (unified / spot-collateral accounts)
    positions = {}
    for ap in user.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if szi:
            positions[p["coin"]] = {"side": "LONG" if szi > 0 else "SHORT", "sz": abs(szi),
                                    "entry": float(p.get("entryPx", 0) or 0), "upnl": float(p.get("unrealizedPnl", 0) or 0)}
    return nav, positions, mids


def stop_orders(info):
    by_coin = {}
    for o in api_retry(info.frontend_open_orders, ACCOUNT_ADDRESS):
        if is_stop_order(o):
            by_coin.setdefault(o["coin"], []).append(o)
    return by_coin


def sync_stop(exchange, stops_now, coin, side, sz, stop_px, sz_dec, log):
    """Keep exactly one reduce-only stop for the position at stop_px. New stop first, then cancel the old ones,
    so the position is never left without a stop."""
    px = round_px(stop_px, sz_dec)
    if px <= 0:
        return
    existing = stops_now.get(coin, [])
    keep = None
    for o in existing:
        same_px = abs(float(o.get("triggerPx", 0) or 0) - px) / px < 0.0005
        same_sz = abs(float(o.get("sz", 0) or 0) - sz) <= max(sz * 0.001, 1e-9)
        if keep is None and same_px and same_sz:
            keep = o["oid"]
    if keep is None:
        is_buy = side == "SHORT"                                   # a short's stop BUYS back
        res = exchange.order(coin, is_buy, sz, px, {"trigger": {"isMarket": True, "triggerPx": px, "tpsl": "sl"}}, reduce_only=True)
        ok, _, err = order_result(res)
        if not ok:
            log.append(f"🚨 {coin}: stop @ {px} FAILED ({err}) — old stop kept")
            return
        log.append(f"🛡️ {coin}: stop set @ {px}")
    for o in existing:
        if o["oid"] != keep:
            try:
                exchange.cancel(coin, int(o["oid"]))
            except Exception:
                pass


def cancel_stops(exchange, stops_now, coin):
    for o in stops_now.get(coin, []):
        try:
            exchange.cancel(coin, int(o["oid"]))
        except Exception:
            pass


def exit_fill(info, coin, since_ms):
    """Average close price and realised PnL (net of fees) of a position closed on the exchange since since_ms."""
    fills = [f for f in api_retry(info.user_fills, ACCOUNT_ADDRESS)
             if f.get("coin") == coin and int(f.get("time", 0)) >= since_ms and "Close" in str(f.get("dir", ""))]
    if not fills:
        return None, None
    sz = sum(float(f["sz"]) for f in fills)
    px = sum(float(f["px"]) * float(f["sz"]) for f in fills) / sz if sz else None
    pnl = sum(float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0) for f in fills)
    return px, pnl


# ======================================================================================
# Stops
# ======================================================================================
def desired_stop(p):
    """Long: the higher of the profit lock and the -4% loss stop. Short: the loss stop (LS-15 short exits are daily)."""
    if p["side"] == "LONG":
        stop = p["entry"] * (1 - DISASTER_STOP)
        fl = engine.lock_floor(p["peak_px"] / p["entry"] - 1, LOCK_TIERS)
        if fl is not None:
            stop = max(stop, p["entry"] * (1 + fl))
        return stop
    return p["entry"] * (1 + DISASTER_STOP)


def raise_peaks(info, state, now_ms, log):
    """Walk the 4-hour candles closed since each long was opened / last checked and raise its peak."""
    for coin, p in state["positions"].items():
        if p["side"] != "LONG":
            continue
        start = max(int(p.get("peak_checked_ms", p["opened_ms"])), int(p["opened_ms"]))
        if now_ms < start + H4_MS:
            continue                                               # no new 4-hour candle has closed since the last check
        try:
            candles = api_retry(info.candles_snapshot, coin, "4h", start - H4_MS, now_ms)
        except Exception as e:
            log.append(f"⚠️ {coin}: 4h candles unavailable ({e}) — stop not raised this run")
            continue
        done = p.get("peak_checked_ms", 0)
        for k in sorted(candles, key=lambda k: int(k["t"])):
            t = int(k["t"])
            if t < p["opened_ms"] or t + H4_MS > now_ms:          # only candles that started after entry and have closed
                continue
            p["peak_px"] = max(p["peak_px"], float(k["h"]))
            done = max(done, t + H4_MS)
        if done:
            p["peak_checked_ms"] = done


# ======================================================================================
# Daily decisions (same rules as gc_paper.step_day for Book Z4)
# ======================================================================================
def daily_decisions(info, exchange, state, nav, ex_pos, sz_dec, log, skips):
    newest_closed = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))   # yesterday (UTC) = last closed daily candle
    if (state.get("last_daily_day") or "") >= newest_closed:
        return state.get("last_daily_day"), False                  # already decided today: no market download
    ranked, data = engine.fetch_market(info, 400, paced=True)          # long history so the channel matches the backtest closely
    btc_days = sorted(data.get("BTC", {}))
    if not btc_days:
        raise RuntimeError("No BTC daily candles — cannot make daily decisions")
    day = btc_days[-1]
    if state.get("last_daily_day") == day:
        return day, False
    prev_day = btc_days[-2] if len(btc_days) > 1 else None
    bar = {c: data[c][day] for c in data if day in data[c]}
    prev = {c: data[c][prev_day] for c in data if prev_day in data[c]}
    rank_of = {c: i + 1 for i, c in enumerate(ranked)}
    btc_down = engine.btc_downtrend(data, day)
    log.append(f"📅 Daily decisions on the {day} close • BTC {'DOWNTREND (bear shorts allowed)' if btc_down else 'not in downtrend'}")
    # Coins whose stop filled during this candle's day count as exited today, as in the backtest (no re-entry the same day).
    exited = {t["coin"] for t in state["closed"] if str(t.get("closed", ""))[:10] >= day}

    # Exits for the bot's own positions
    for coin, p in list(state["positions"].items()):
        b = bar.get(coin)
        why = None
        if rank_of.get(coin, 10 ** 6) > engine.DRIFT_OUT_RANK:
            why = "Drifted out of top ranks"
        elif not b or not b.get("valid"):
            skips.append((coin, "indicator data missing — kept"))
        elif p["side"] == "LONG" and b["c"] < b["upper"]:
            why = "Close below upper GC band"
        elif p["side"] == "SHORT" and b["c"] > b["filter"]:
            why = "Stop: close above GC filter"
        elif p["side"] == "SHORT" and b["c"] <= engine.SHORT_TP_RATIO * p["entry"]:
            why = "Take profit (-35%)"
        if why:
            close_own(info, exchange, state, coin, why, log)
            exited.add(coin)

    # Entries
    held = set(ex_pos) | set(state["positions"])
    held -= exited
    gross = sum(ex_pos[c]["sz"] * float(bar.get(c, {}).get("c", ex_pos[c]["entry"])) for c in ex_pos if c not in exited)
    budget = GROSS_CAP * nav - gross
    universe = ranked[:engine.UNIVERSE_SIZE]
    candidates = []
    for c in universe:                                             # longs first, then shorts — the same order as the backtest
        b, pb = bar.get(c), prev.get(c)
        if b and pb and b.get("valid") and b["c"] > b["upper"] and pb["c"] <= pb["upper"]:
            candidates.append((c, "LONG", "band breakout"))
    for c in universe:
        b, pb = bar.get(c), prev.get(c)
        if not (b and pb and b.get("valid")):
            continue
        hedge = b["trend"] == "Red" and b["c"] < b["filter"] and pb["c"] >= pb["filter"]
        bear = btc_down and b["trend"] == "Red" and b["h"] >= engine.BEAR_SHORT_TOUCH * b["filter"] and b["c"] < b["filter"]
        if hedge or bear:
            candidates.append((c, "SHORT", "hedge short (cross under filter)" if hedge else "bear short (rejected bounce, BTC down)"))
    for coin, side, why in candidates:
        if coin in held or coin in exited:
            skips.append((coin, f"{side} signal — already holding / exited today"))
            continue
        if len(held) >= MAX_POSITIONS:
            skips.append((coin, f"{side} signal — all {MAX_POSITIONS} slots in use"))
            continue
        usd = min(SLOT_SIZE * nav, budget)
        if usd < MIN_ORDER_USD:
            skips.append((coin, f"{side} signal — ${usd:,.2f} is under the ${MIN_ORDER_USD:.0f} minimum"))
            continue
        if open_own(info, exchange, state, coin, side, usd, bar[coin]["c"], sz_dec.get(coin, 2), day, why, log):
            held.add(coin)
            budget -= usd
    state["last_daily_day"] = day
    return day, True


def open_own(info, exchange, state, coin, side, usd, ref_px, dec, day, why, log):
    px = ref_px
    try:
        px = float(api_retry(info.all_mids).get(coin, ref_px))
    except Exception:
        pass
    sz = round_sz_down(usd / px, dec)
    if sz <= 0 or sz * px < 10.0:
        log.append(f"⏭️ {coin}: size {sz} too small after rounding — skipped")
        return False
    try:
        exchange.update_leverage(LEVERAGE_SETTING, coin, True)
    except Exception as e:
        log.append(f"⚠️ {coin}: leverage not set ({e})")
    res = exchange.market_open(coin, side == "LONG", sz, px, SLIPPAGE)
    ok, statuses, err = order_result(res)
    filled, avg = filled_of(statuses)
    if not ok or filled <= 0:
        log.append(f"🚨 {coin}: {side} entry FAILED ({err or 'not filled'})")
        return False
    log.append(f"✅ {coin}: {side} {filled} @ {avg:.6g} (${filled * avg:,.2f}) — {why}")
    if DRY_RUN:
        return True                                                # nothing was bought: do not track a position
    now_ms = int(time.time() * 1000)
    state["positions"][coin] = {"side": side, "sz": filled, "entry": avg, "peak_px": avg, "opened": day, "opened_ms": now_ms,
                                "reason": why, "stop": None}
    return True


def close_own(info, exchange, state, coin, why, log):
    res = exchange.market_close(coin, None, None, SLIPPAGE)
    ok, statuses, err = order_result(res)
    if not ok:
        log.append(f"🚨 {coin}: close FAILED ({err}) — still open, stop still resting")
        return
    if DRY_RUN:
        return
    try:
        cancel_stops(exchange, stop_orders(info), coin)            # its reduce-only stop is no longer needed
    except Exception:
        pass
    _, avg = filled_of(statuses)
    record_closed(info, state, coin, avg or None, why, log)


def record_closed(info, state, coin, px, why, log):
    p = state["positions"].pop(coin)
    fill_px, pnl = exit_fill(info, coin, int(p["opened_ms"]))
    px = px or fill_px or p["entry"]
    gain = (px / p["entry"] - 1) if p["side"] == "LONG" else (1 - px / p["entry"])
    peak = p["peak_px"] / p["entry"] - 1 if p["side"] == "LONG" else 0.0
    if pnl is None:
        pnl = gain * p["sz"] * p["entry"]
    state["closed"].append({"coin": coin, "side": p["side"], "opened": p["opened"], "closed": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            "entry": p["entry"], "exit": px, "pct": round(gain * 100, 2), "peak_pct": round(max(peak, 0) * 100, 2),
                            "pnl": round(pnl, 4), "reason": why})
    log.append(f"{'💰' if pnl > 0 else '🔻'} {coin}: {p['side']} closed {gain*100:+.2f}% (${pnl:+,.2f}) — {why}")


# ======================================================================================
# Report
# ======================================================================================
def send_email(subject, html, text):
    sender, password, to = os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECEIVER_EMAIL")
    if not (sender and password and to):
        print("[WARN] email credentials missing\n" + text, flush=True)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent: {subject}", flush=True)


def fmt_px(v):
    return "—" if v is None else f"{v:.6g}"


def report(state, nav, ex_pos, mids, log, skips, daily_day):
    start = state.get("start_nav") or nav
    rows = ""
    for coin, p in sorted(state["positions"].items()):
        now = mids.get(coin, p["entry"])
        g = (now / p["entry"] - 1) if p["side"] == "LONG" else (1 - now / p["entry"])
        peak = (p["peak_px"] / p["entry"] - 1) * 100 if p["side"] == "LONG" else None
        rows += (f"<tr><td>{coin}</td><td style='color:{'#16a34a' if p['side']=='LONG' else '#dc2626'};'>{p['side']}</td>"
                 f"<td>{p['entry']:.6g}</td><td>{now:.6g}</td><td style='color:{'#15803d' if g>=0 else '#b91c1c'};'>{g*100:+.2f}%</td>"
                 f"<td>{'—' if peak is None else f'{peak:+.1f}%'}</td><td>{fmt_px(p.get('stop'))}</td>"
                 f"<td>${p['sz']*now:,.2f}</td><td>{p['opened']}</td></tr>")
    foreign = [c for c in ex_pos if c not in state["positions"]]
    if foreign:
        rows += f"<tr><td colspan='9' style='color:#b45309;'>Not managed by this bot (opened elsewhere): {', '.join(foreign)}</td></tr>"
    rows = rows or "<tr><td colspan='9' style='color:#64748b;'>No open positions</td></tr>"
    closed = state["closed"]
    realised = sum(t["pnl"] for t in closed)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    recent = "".join(f"<tr><td>{t['closed']}</td><td>{t['coin']}</td><td>{t['side']}</td><td style='color:{'#15803d' if t['pnl']>0 else '#b91c1c'};'>"
                     f"{t['pct']:+.2f}%</td><td>{t['peak_pct']:+.1f}%</td><td>${t['pnl']:+,.2f}</td><td>{t['reason']}</td></tr>"
                     for t in reversed(closed[-10:])) or "<tr><td colspan='7' style='color:#64748b;'>None yet</td></tr>"
    gross = sum(ex_pos[c]["sz"] * mids.get(c, ex_pos[c]["entry"]) for c in ex_pos)
    warn = (f"<div style='background:#fee2e2;border:1px solid #fecaca;border-radius:6px;padding:8px;color:#991b1b;margin-bottom:10px;'>"
            f"⚠️ Account ${nav:,.2f} is below ${LOW_BALANCE_WARN:.0f}: 15% positions are under Hyperliquid's $10 minimum, so no new trades "
            f"can open. Open positions are still managed.</div>") if nav < LOW_BALANCE_WARN else ""
    mode = "🧪 DRY RUN — no orders were sent" if DRY_RUN else "🟢 LIVE — real orders"
    log_html = "".join(f"<div>• {x}</div>" for x in log) or "<div style='color:#64748b;'>Nothing to do</div>"
    skip_html = "".join(f"<div>• {c}: {w}</div>" for c, w in skips) or "<div style='color:#64748b;'>None</div>"
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
 <div style="background:#0f172a;color:#fff;padding:14px;"><div style="font-size:15px;font-weight:700;">{BOT_VERSION} • {mode}</div>
  <div style="font-size:11px;color:#94a3b8;">{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} • daily candle {daily_day or '—'}</div></div>
 <div style="padding:12px;font-size:12px;">{warn}
  <div style="font-size:22px;font-weight:700;color:{'#15803d' if nav>=start else '#b91c1c'};">${nav:,.2f}
   <span style="font-size:13px;">({(nav/start-1)*100 if start else 0:+.2f}% since {state.get('started') or 'today'}, start ${start:,.2f})</span></div>
  <div style="color:#475569;font-size:11px;">In positions ${gross:,.2f} ({gross/nav*100 if nav else 0:.0f}% of the account, cap {GROSS_CAP*100:.0f}%) •
   {len(state['positions'])}/{MAX_POSITIONS} slots • Closed trades {len(closed)} ({wins} wins) • Realised ${realised:+,.2f}</div>
  <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:8px;" cellpadding="3">
   <tr style="background:#f1f5f9;"><th align="left">Coin</th><th align="left">Side</th><th align="left">Entry</th><th align="left">Now</th><th align="left">P&amp;L</th>
   <th align="left">Peak</th><th align="left">Stop</th><th align="left">Value</th><th align="left">Since</th></tr>{rows}</table>
  <div style="font-weight:700;margin-top:10px;">This run</div>{log_html}
  <div style="font-weight:700;margin-top:10px;">Signals not taken</div><div style="font-size:11px;">{skip_html}</div>
  <div style="font-weight:700;margin-top:10px;">Recent closed trades</div>
  <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3"><tr style="background:#f1f5f9;"><th align="left">When</th><th align="left">Coin</th>
   <th align="left">Side</th><th align="left">Result</th><th align="left">Peak</th><th align="left">$</th><th align="left">Why</th></tr>{recent}</table>
  <div style="font-size:10px;color:#64748b;margin-top:10px;">Rules: LS-15 daily signals (long + short), max {MAX_POSITIONS} positions × {SLOT_SIZE*100:.0f}%,
   up to {GROSS_CAP*100:.0f}% in positions, cross {LEVERAGE_SETTING}x setting. Longs: early profit lock (keeps 50% of the peak gain from +3%,
   60% from +5%, 70% from +10%, 80/85/90% from +20/35/60%) as a real stop raised every 4 hours. Every position: loss stop {DISASTER_STOP*100:.0f}% against the entry.
   Shorts exit on a daily close above the GC filter or at -35%.</div>
 </div></div></body></html>"""
    text = f"{BOT_VERSION} {mode}\nNAV ${nav:,.2f}\n" + "\n".join(log)
    summary = f"${nav:,.2f} • {len(state['positions'])} open" + (f" • {len([x for x in log if x[:1] in '✅💰🔻'])} trades" if log else "")
    send_email(f"{'🧪' if DRY_RUN else '🤖'} LS-15 LIVE {'(DRY RUN) ' if DRY_RUN else ''}— {summary}", html, text)


# ======================================================================================
# Main
# ======================================================================================
def main():
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils import constants
    import eth_account

    if not ACCOUNT_ADDRESS or (not SECRET_KEY and not DRY_RUN):
        raise ValueError("Missing HL_ACCOUNT_ADDRESS / HL_SECRET_KEY")
    log, skips = [], []
    state = load_state()
    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)
    if DRY_RUN:
        exchange = DryRunExchange(log)
    else:
        exchange = api_retry(Exchange, eth_account.Account.from_key(SECRET_KEY), constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    now_ms = int(time.time() * 1000)
    meta = api_retry(info.meta)
    sz_dec = {a["name"]: int(a.get("szDecimals", 2)) for a in meta.get("universe", [])}

    nav, ex_pos, mids = read_account(info)
    if state.get("start_nav") is None:
        state["start_nav"], state["started"] = nav, datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 0) Positions on the exchange that the state does not know (e.g. a run that started from an older state.json):
    #    adopt them so they still get their disaster stop / profit lock instead of being left unprotected.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for coin, ep in ex_pos.items():
        if coin not in state["positions"]:
            state["positions"][coin] = {"side": ep["side"], "sz": ep["sz"], "entry": ep["entry"], "peak_px": ep["entry"], "opened": today,
                                        "opened_ms": now_ms, "reason": "adopted (found on the exchange)", "stop": None}
            log.append(f"🔄 {coin}: {ep['side']} found on the exchange but not in state.json — now managed (stops placed)")

    # 1) Positions the exchange closed since last run (a stop filled)
    for coin in [c for c in state["positions"] if c not in ex_pos]:
        record_closed(info, state, coin, None, "🛡️ Stop filled on the exchange (profit lock / loss stop)", log)
    for coin, p in state["positions"].items():                     # keep size in sync (partial fills, manual changes)
        p["sz"] = ex_pos[coin]["sz"]

    # 2) Daily decisions after a new daily close
    daily_day, did_daily = None, False
    try:
        daily_day, did_daily = daily_decisions(info, exchange, state, nav, ex_pos, sz_dec, log, skips)
    except Exception as e:
        log.append(f"🚨 Daily decisions failed: {e}")
    if did_daily and not DRY_RUN:
        nav, ex_pos, mids = read_account(info)

    # 3) Every run: raise long peaks from 4h candles, sync every stop order
    raise_peaks(info, state, now_ms, log)
    stops_now = stop_orders(info)
    closed_now = False
    for coin, p in list(state["positions"].items()):
        stop = desired_stop(p)
        if p["side"] == "LONG" and mids.get(coin) and mids[coin] <= stop:
            close_own(info, exchange, state, coin, f"🔒 Profit lock (price {mids[coin]:.6g} already under stop {stop:.6g})", log)
            closed_now = True
            continue
        if p.get("stop") is None or abs(stop - p["stop"]) / stop > 0.0005:
            old = p.get("stop")
            p["stop"] = stop
            if old is not None and stop > old and p["side"] == "LONG":
                log.append(f"⬆️ {coin}: stop raised {old:.6g} → {stop:.6g} (peak {(p['peak_px']/p['entry']-1)*100:+.1f}%)")
        sync_stop(exchange, stops_now, coin, p["side"], p["sz"], p["stop"], sz_dec.get(coin, 2), log)
    for coin in stops_now:                                          # stops left over from closed positions
        if coin not in ex_pos:
            cancel_stops(exchange, stops_now, coin)
    if closed_now and not DRY_RUN:                                 # the email should show the account after the close
        nav, ex_pos, mids = read_account(info)

    if did_daily:                                                  # one balance point per day (runs every 5 minutes)
        state.setdefault("nav_history", []).append([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), round(nav, 2)])
        state["nav_history"] = state["nav_history"][-400:]
    state["version"] = BOT_VERSION
    save_state(state)
    print("\n".join(log), flush=True)
    if did_daily or IS_MANUAL or any(x[:1] in "✅💰🔻🚨⬆🔄" for x in log):
        report(state, nav, ex_pos, mids, log, skips, daily_day)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"LS-15 LIVE bot error: {e}"
        print(msg, flush=True)
        try:
            send_email(f"⚠️ {msg}", f"<pre>{msg}</pre>", msg)
        except Exception:
            pass
        raise
