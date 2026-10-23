"""
bot_engine_stocks.py — TR-GC-Equities-LS-15 "Book F" (Alpaca)

Replaces the old 15-minute scalper (V5.0.0) on the same account, workflow (stock_bot.yml) and state file (stock_state.json).
The rules are Book F from gc_paper.py (stocks backtest: beat SPY in 100/100 coin orders over 2 years, long-only, 1x):
  - Gaussian Channel (hlc3, 4 poles, period 144, x1.414) on DAILY bars, top 100 of ~110 large US stocks by dollar volume
  - BUY when the daily close crosses above the upper band: 4.8% of the account if the breakout is 25 days old or less,
    otherwise 1.2% (LS-15 sizes x 0.6)
  - ADD up to 3.3% while the stock is +10% or more and still above the band
  - SELL when the daily close is back below the upper band. No profit lock (it cut the stock trends short in the backtest)
  - long-only, cash only (no margin, no shorts) — exactly what a $1,000 account at Alpaca can do

Timing: stock_bot.yml runs every 5 minutes on weekdays. Only ONE run per day acts: the first run in the last 30 minutes
before the close (normally 3:30-3:55 PM ET / 2:30-2:55 PM CT). It uses the price at that moment as "today's close", so the
orders fill close to the real close, like in the backtest. Positions are HELD OVERNIGHT (days to weeks). All other runs exit.
"""
import os
import json
import math
import time
import uuid
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

import gc_paper as engine

BOT_VERSION = "V6.0.0"
BOT_TITLE = f"TR-GC-Equities-LS-15-F {BOT_VERSION}"
STATE_VERSION = "GCSTK-F-1"

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
BASE_URL = os.getenv("APAL_BASE_URL") or "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
STATE_FILE = "stock_state_dryrun.json" if DRY_RUN else "stock_state.json"
IS_MANUAL = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"

SIZE_MULT = 0.60                              # Book F = LS-15 sizes x 0.6
LONG_SIZE_RECENT = engine.LONG_SIZE_RECENT * SIZE_MULT    # 4.8% (breakout 25 days old or less)
LONG_SIZE_OLD = engine.LONG_SIZE_OLD * SIZE_MULT          # 1.2%
TOPUP_TARGET = engine.TOPUP_TARGET * SIZE_MULT            # 3.3%
TOPUP_MIN_ROE = engine.TOPUP_MIN_ROE                      # +10%
MIN_TRADE_NAV = engine.MIN_TRADE_NAV                      # skip anything under 1% of the account
MIN_ORDER_USD = 1.0                           # Alpaca fractional orders go down to $1
UNIVERSE_SIZE = 100
DECISION_WINDOW_MINS = (5, 30)                # act when the close is 5-30 minutes away

HEADERS = {"APCA-API-KEY-ID": API_KEY or "", "APCA-API-SECRET-KEY": SECRET_KEY or "", "accept": "application/json"}


# ======================================================================================
# Alpaca helpers
# ======================================================================================
def call(method, path, data=False, **kw):
    """Retries network errors, 429 and 5xx only. A 4xx (rejected order) is final: re-sending could duplicate it."""
    url = (DATA_URL if data else BASE_URL) + path
    delay, res = 2.0, None
    for attempt in range(4):
        try:
            res = getattr(requests, method)(url, headers=HEADERS, timeout=20, **kw)
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            return res
        except Exception:
            if attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
    return res


def ok(res):
    return res is not None and res.status_code in (200, 201, 202, 204, 207)


def write(method, path, log, **kw):
    if DRY_RUN:
        log.append(f"🧪 DRY RUN — would {method.upper()} {path} {json.dumps(kw.get('json')) if kw.get('json') else ''}")
        return None
    return call(method, path, **kw)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("version") == STATE_VERSION:
                return state
            print("[INFO] stock_state.json is from the old scalper — starting a fresh Book F state", flush=True)
        except Exception as e:
            print(f"[WARN] could not read {STATE_FILE}: {e}", flush=True)
    return {"version": STATE_VERSION, "positions": {}, "closed": [], "last_decision_day": None, "started": None,
            "start_equity": None, "start_spy": None, "nav_history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ======================================================================================
# Market data: daily bars up to yesterday (SIP history) + today's bar so far (IEX snapshot)
# ======================================================================================
def build_market(now_utc, today_et):
    midnight_et = datetime(today_et.year, today_et.month, today_et.day, tzinfo=engine.ET_ZONE).astimezone(timezone.utc)
    end = min(now_utc - timedelta(minutes=16), midnight_et)
    raw = engine.alpaca_bars(engine.STOCK_UNIVERSE, "1Day", now_utc - timedelta(days=700), end)
    snaps = {}
    syms = list(engine.STOCK_UNIVERSE)
    for i in range(0, len(syms), 50):
        res = call("get", "/v2/stocks/snapshots", data=True, params={"symbols": ",".join(syms[i:i + 50]), "feed": "iex"})
        if ok(res):
            snaps.update(res.json() or {})
    today = today_et.strftime("%Y-%m-%d")
    data, dollar_vol, last_px = {}, {}, {}
    for sym, bl in raw.items():
        rows = []
        for k in sorted(bl, key=lambda k: k["t"]):
            d = engine._et(k["t"]).strftime("%Y-%m-%d")
            if d < today:
                rows.append({"date": d, "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v", 0))})
        sn = snaps.get(sym) or {}
        px = float((sn.get("latestTrade") or {}).get("p") or 0)
        db = sn.get("dailyBar") or {}
        if px > 0 and db and engine._et(db["t"]).strftime("%Y-%m-%d") == today:
            rows.append({"date": today, "o": float(db["o"]), "h": max(float(db["h"]), px), "l": min(float(db["l"]), px), "c": px,
                         "v": float(db.get("v", 0))})
            last_px[sym] = px
        if len(rows) < 3:
            continue
        engine.enrich(rows)
        data[sym] = {r["date"]: r for r in rows}
        dollar_vol[sym] = sum(r["c"] * r["v"] for r in rows[-21:-1]) / max(1, len(rows[-21:-1]))
    ranked = sorted(data, key=lambda s: dollar_vol.get(s, 0), reverse=True)
    return ranked, data, last_px


# ======================================================================================
# Orders
# ======================================================================================
def buy(sym, usd, px, log):
    """Fractional (notional) market order; whole shares if the stock is not fractionable."""
    payload = {"symbol": sym, "notional": f"{usd:.2f}", "side": "buy", "type": "market", "time_in_force": "day",
               "client_order_id": f"gcf-{sym}-{uuid.uuid4().hex[:10]}"}
    res = write("post", "/v2/orders", log, json=payload)
    if DRY_RUN or ok(res):
        return True
    qty = math.floor(usd / px) if px > 0 else 0
    if qty >= 1:
        payload.pop("notional")
        payload["qty"] = str(qty)
        payload["client_order_id"] = f"gcf-{sym}-{uuid.uuid4().hex[:10]}"
        res = write("post", "/v2/orders", log, json=payload)
        if ok(res):
            return True
    log.append(f"🚨 {sym}: buy ${usd:,.2f} REJECTED ({res.status_code if res is not None else 'ERR'}: {res.text[:160] if res is not None else ''})")
    return False


def sell_all(sym, open_orders, log):
    for o in open_orders:                                       # an open order can hold the shares: cancel first
        if o.get("symbol") == sym:
            write("delete", f"/v2/orders/{o['id']}", log)
    res = write("delete", f"/v2/positions/{sym}", log)
    if DRY_RUN or ok(res):
        return True
    log.append(f"🚨 {sym}: sell REJECTED ({res.status_code if res is not None else 'ERR'}: {res.text[:160] if res is not None else ''})")
    return False


# ======================================================================================
# Daily decisions (Book F rules, same order as gc_paper.step_day: exits, entries, top-ups)
# ======================================================================================
def decide(state, equity, cash, positions, open_orders, today, log, skips):
    now_utc = datetime.now(timezone.utc)
    ranked, data, last_px = build_market(now_utc, now_utc.astimezone(engine.ET_ZONE))
    universe = ranked[:UNIVERSE_SIZE]
    bar = {s: data[s][today] for s in data if today in data[s]}
    log.append(f"📅 Daily decisions on {today} ({len(bar)} stocks priced; price at {now_utc.astimezone(engine.ET_ZONE).strftime('%H:%M')} ET used as the close)")
    exited, entered = set(), set()

    # Exits: close back below the upper band
    for sym, p in list(positions.items()):
        b = bar.get(sym)
        if not b or not b.get("valid"):
            skips.append((sym, "held, but no usable bar today — kept"))
            continue
        if b["c"] < b["upper"]:
            if sell_all(sym, open_orders, log):
                exited.add(sym)
                meta = state["positions"].pop(sym, {})
                pnl, pct = float(p.get("unrealized_pl", 0) or 0), float(p.get("unrealized_plpc", 0) or 0) * 100
                state["closed"].append({"symbol": sym, "opened": meta.get("opened", "?"), "closed": today, "entry": float(p["avg_entry_price"]),
                                        "exit": b["c"], "pct": round(pct, 2), "pnl": round(pnl, 2), "reason": "Close below upper GC band"})
                log.append(f"{'💰' if pnl > 0 else '🔻'} SELL {sym} @ ~${b['c']:.2f}: {pct:+.2f}% (${pnl:+,.2f}) — close below upper band")
                cash += float(p.get("market_value", 0) or 0)
    state["closed"] = state["closed"][-200:]

    # Entries: breakout above the upper band, in dollar-volume order, cash only
    for sym in universe:
        b = bar.get(sym)
        pb = b.get("_prev") if b else None
        if not b or not pb or not b.get("valid") or not (b["c"] > b["upper"] and pb["c"] <= pb["upper"]):
            continue
        if sym in positions or sym in exited:
            skips.append((sym, "breakout, but already held / sold today"))
            continue
        recent = b.get("breakout") and (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(b["breakout"], "%Y-%m-%d")).days <= engine.BREAKOUT_RECENT_DAYS
        want = equity * (LONG_SIZE_RECENT if recent else LONG_SIZE_OLD)
        usd = min(want, cash * 0.995)
        if usd < max(MIN_ORDER_USD, equity * MIN_TRADE_NAV):
            skips.append((sym, f"breakout, not enough cash (${usd:,.2f})"))
            continue
        if buy(sym, usd, b["c"], log):
            entered.add(sym)
            cash -= usd
            state["positions"][sym] = {"opened": today, "breakout": b.get("breakout")}
            log.append(f"✅ BUY {sym} ${usd:,.2f} @ ~${b['c']:.2f} — band breakout ({'recent' if recent else 'old'}, {want/equity*100:.1f}% of the account)")

    # Top-ups: winners still above the band
    for sym in universe:
        p, b = positions.get(sym), bar.get(sym)
        if not p or sym in exited or sym in entered or not b or not b.get("valid") or b["c"] <= b["upper"]:
            continue
        entry = float(p["avg_entry_price"])
        roe = b["c"] / entry - 1
        held = float(p.get("qty", 0)) * b["c"]
        target = equity * TOPUP_TARGET
        if roe < TOPUP_MIN_ROE or held >= target:
            continue
        usd = min(target - held, cash * 0.995)
        if usd < max(MIN_ORDER_USD, equity * MIN_TRADE_NAV):
            continue
        if buy(sym, usd, b["c"], log):
            cash -= usd
            log.append(f"➕ ADD {sym} ${usd:,.2f} (up {roe*100:+.1f}%, still above the band)")
    return last_px.get("SPY")


# ======================================================================================
# Report
# ======================================================================================
def send_email(subject, html, text):
    sender, password, to = os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECEIVER_EMAIL")
    if DRY_RUN or not (sender and password and to):
        print(f"[email not sent] {subject}\n{text}", flush=True)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent: {subject}", flush=True)


def report(state, equity, positions, spy_now, log, skips, decided, next_window):
    start = state.get("start_equity") or equity
    ret = (equity / start - 1) * 100 if start else 0.0
    spy_ret = ((spy_now / state["start_spy"] - 1) * 100) if spy_now and state.get("start_spy") else None
    rows = ""
    for sym, p in sorted(positions.items(), key=lambda kv: -float(kv[1].get("market_value", 0) or 0)):
        pct = float(p.get("unrealized_plpc", 0) or 0) * 100
        rows += (f"<tr><td><b>{sym}</b></td><td>{float(p['qty']):g}</td><td>${float(p['avg_entry_price']):,.2f}</td>"
                 f"<td>${float(p.get('current_price', 0) or 0):,.2f}</td><td style='color:{'#15803d' if pct >= 0 else '#b91c1c'};'>{pct:+.2f}%</td>"
                 f"<td>${float(p.get('market_value', 0) or 0):,.2f}</td><td>{state['positions'].get(sym, {}).get('opened', '?')}</td></tr>")
    rows = rows or "<tr><td colspan='7' style='color:#64748b;'>No open positions</td></tr>"
    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    recent = "".join(f"<tr><td>{t['closed']}</td><td>{t['symbol']}</td><td style='color:{'#15803d' if t['pnl'] > 0 else '#b91c1c'};'>{t['pct']:+.2f}%</td>"
                     f"<td>${t['pnl']:+,.2f}</td><td>{t['opened']}</td></tr>" for t in reversed(closed[-10:])) or \
        "<tr><td colspan='5' style='color:#64748b;'>None yet</td></tr>"
    invested = sum(float(p.get("market_value", 0) or 0) for p in positions.values())
    log_html = "".join(f"<div>• {x}</div>" for x in log) or "<div style='color:#64748b;'>Nothing to do</div>"
    skip_html = "".join(f"<div>• {s}: {w}</div>" for s, w in skips) or "<div style='color:#64748b;'>None</div>"
    vs_spy = "" if spy_ret is None else f" • SPY since start {spy_ret:+.2f}%"
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
 <div style="background:#0f172a;color:#fff;padding:14px;"><div style="font-size:15px;font-weight:700;">{BOT_TITLE}{' • 🧪 DRY RUN' if DRY_RUN else ''}</div>
  <div style="font-size:11px;color:#94a3b8;">{datetime.now(engine.ET_ZONE).strftime('%Y-%m-%d %H:%M ET')} • {'daily decisions made' if decided else f'report only — next decisions {next_window}'}</div></div>
 <div style="padding:12px;font-size:12px;">
  <div style="font-size:22px;font-weight:700;color:{'#15803d' if ret >= 0 else '#b91c1c'};">${equity:,.2f}
   <span style="font-size:13px;">({ret:+.2f}% since {state.get('started') or 'today'}{vs_spy})</span></div>
  <div style="color:#475569;font-size:11px;">Invested ${invested:,.2f} ({invested/equity*100 if equity else 0:.0f}%) • {len(positions)} positions •
   Closed {len(closed)} ({len(wins)} wins) • Realised ${sum(t['pnl'] for t in closed):+,.2f}</div>
  <table style="width:100%;font-size:11px;border-collapse:collapse;margin-top:8px;" cellpadding="3">
   <tr style="background:#f1f5f9;"><th align="left">Stock</th><th align="left">Shares</th><th align="left">Entry</th><th align="left">Now</th><th align="left">P&amp;L</th>
   <th align="left">Value</th><th align="left">Since</th></tr>{rows}</table>
  <div style="font-weight:700;margin-top:10px;">This run</div>{log_html}
  <div style="font-weight:700;margin-top:10px;">Signals not taken</div><div style="font-size:11px;">{skip_html}</div>
  <div style="font-weight:700;margin-top:10px;">Recent sells</div>
  <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3"><tr style="background:#f1f5f9;"><th align="left">Sold</th>
   <th align="left">Stock</th><th align="left">Result</th><th align="left">$</th><th align="left">Bought</th></tr>{recent}</table>
  <div style="font-size:10px;color:#64748b;margin-top:10px;">Book F rules: daily Gaussian Channel on ~100 large US stocks. Buy on a close above the
   upper band (4.8% of the account if the breakout is ≤25 days old, else 1.2%), add up to 3.3% on +10% winners, sell on a close back below
   the band. Long-only, cash only, held overnight. Decisions once a day in the last 30 minutes before the close.</div>
 </div></div></body></html>"""
    text = f"{BOT_TITLE}\nEquity ${equity:,.2f} ({ret:+.2f}%)\n" + "\n".join(log)
    n_trades = sum(1 for x in log if x[:1] in "✅💰🔻➕")
    send_email(f"📈 Alpaca GC-F — ${equity:,.2f} ({ret:+.2f}%){f' • {n_trades} trades' if decided else ''}{' [DRY RUN]' if DRY_RUN else ''}", html, text)


# ======================================================================================
# Main
# ======================================================================================
def main():
    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY")
    clock = call("get", "/v2/clock")
    if not ok(clock):
        raise RuntimeError(f"Alpaca clock failed: {clock.text if clock is not None else 'no response'}")
    clock = clock.json()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(engine.ET_ZONE).strftime("%Y-%m-%d")
    mins_to_close = (parse_ts(clock["next_close"]) - now_utc).total_seconds() / 60
    state = load_state()
    in_window = bool(clock.get("is_open")) and DECISION_WINDOW_MINS[0] <= mins_to_close <= DECISION_WINDOW_MINS[1]
    due = in_window and state.get("last_decision_day") != today
    if not due and not IS_MANUAL:
        print(f"Nothing to do ({'market closed' if not clock.get('is_open') else f'{mins_to_close:.0f} min to close'}; "
              f"decided today: {state.get('last_decision_day') == today})", flush=True)
        return

    log, skips = [], []
    acct = call("get", "/v2/account").json()
    equity, cash = float(acct.get("equity", 0)), max(0.0, float(acct.get("cash", 0)))
    positions = {p["symbol"]: p for p in (call("get", "/v2/positions").json() or []) if p.get("side", "long") == "long"}
    open_orders = call("get", "/v2/orders", params={"status": "open", "limit": 500}).json() or []
    if state.get("start_equity") is None:
        state["start_equity"], state["started"] = equity, today
    for sym in positions:                                        # positions the state doesn't know (e.g. old bot): manage them
        if sym not in state["positions"]:
            state["positions"][sym] = {"opened": "before " + today, "breakout": None}
            log.append(f"🔄 {sym}: found in the account — now managed by the Book F rules")
    for sym in list(state["positions"]):
        if sym not in positions:
            state["positions"].pop(sym)

    spy_now = None
    if due:
        for o in open_orders:                                    # leftovers from the old scalper (stops, limits)
            if not str(o.get("client_order_id", "")).startswith("gcf-"):
                write("delete", f"/v2/orders/{o['id']}", log)
                log.append(f"🧹 cancelled old order {o.get('symbol')} {o.get('type')} {o.get('side')}")
        open_orders = [o for o in open_orders if str(o.get("client_order_id", "")).startswith("gcf-")]
        spy_now = decide(state, equity, cash, positions, open_orders, today, log, skips)
        state["last_decision_day"] = today
        if state.get("start_spy") is None and spy_now:
            state["start_spy"] = spy_now
        state["nav_history"] = (state.get("nav_history", []) + [[today, round(equity, 2)]])[-400:]
        if not DRY_RUN:
            time.sleep(3)
            acct = call("get", "/v2/account").json()
            equity = float(acct.get("equity", equity))
            positions = {p["symbol"]: p for p in (call("get", "/v2/positions").json() or []) if p.get("side", "long") == "long"}
    save_state(state)
    print("\n".join(log), flush=True)
    next_window = "today in the last 30 min before the close" if clock.get("is_open") and state.get("last_decision_day") != today else \
        f"next trading day, 30 min before the close (next open {parse_ts(clock['next_open']).astimezone(engine.ET_ZONE).strftime('%a %m-%d %H:%M ET')})"
    report(state, equity, positions, spy_now, log, skips, due, next_window)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"{BOT_TITLE} error: {e}"
        print(msg, flush=True)
        try:
            send_email(f"⚠️ {msg}", f"<pre>{msg}</pre>", msg)
        except Exception:
            pass
        raise
