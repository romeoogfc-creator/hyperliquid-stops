import os
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import log10, floor
import requests
import pandas as pd
import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

BOT_VERSION = "V4.9.0"
BOT_TITLE = f"TR-GC-Equities-LS-01-{BOT_VERSION}"

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
BASE_URL = os.getenv("APAL_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL = "https://data.alpaca.markets"
DATA_FEED = os.getenv("APAL_DATA_FEED", "iex")   # free plan = iex; set to sip if you pay for full market data
# DRY_RUN=1 -> reads live data, logs what it WOULD do, sends no orders, writes stock_state_dryrun.json, no email.
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
STATE_FILE = "stock_state_dryrun.json" if DRY_RUN else "stock_state.json"
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "100000"))

VERBOSE_TEST_MODE = True

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "accept": "application/json"
}

# ---------------- Session timing (New York time; Alpaca's clock handles holidays & half days) ----------------
NO_ENTRY_FIRST_MINS = 15          # no entries 9:30-9:45 ET (8:30-8:45 CT): opening-bar volume fakes every RVOL signal
NO_NEW_ENTRIES_BEFORE_CLOSE_MINS = 30   # last new entry 3:30 PM ET (2:30 PM CT)
EOD_FLATTEN_BEFORE_CLOSE_MINS = 10      # flatten to cash at 3:50 PM ET (2:50 PM CT); 12:50 PM ET on half days

# ---------------- Risk / strategy settings ----------------
MAX_STOCK_SLOTS = 3
MAX_NEW_ENTRIES_PER_RUN = 1
POSITION_FRACTION = 0.15          # of equity per position
MIN_POSITION_USD = 100.0

HARD_STOP_PCT = -0.0150           # -1.5% price cap
FEE_FLOOR_TRIGGER = 0.0050        # after +0.5% peak...
FEE_FLOOR_LOCK = 0.0015           # ...lock +0.15%
EXPANSION_TRIGGER = 0.0150
MOONSHOT_TRIGGER = 0.0300
CLIMAX_TRIGGER = 0.0150

FLAT_EXIT_MINS = 30.0
FLAT_EXIT_PCT = 0.0010            # exit if still under +0.10% after 30m and never reached the fee-floor trigger

LOSS_COOLDOWN_SEC = 1800
WIN_COOLDOWN_SEC = 5400
LEDGER_LOOKBACK_DAYS = 14         # trade history window (calendar days; weekends count)

CB_MAX_LOSSES = 3                 # 3 losses within...
CB_WINDOW_SEC = 3600              # ...60 minutes -> no new entries for the rest of the day
DAILY_LOSS_LIMIT_PCT = 0.020      # equity down 2% vs yesterday's close -> flatten to cash, done for the day

BLOCK_ENTRIES_WHEN_RISK_OFF = True
EMA9_MAX_EXTENSION = 0.0100
HOD_PROXIMITY_PCT = 0.0050
MAX_SPREAD_PCT = 0.0015

WATCHLIST = [
    "NVDA", "TSLA", "AMD", "PLTR", "COIN", "MSTR", "HOOD", "SMCI", "AAPL", "MSFT",
    "AMZN", "GOOGL", "META", "AVGO", "QCOM", "ARM", "AMAT", "LRCX", "ORCL", "CRM",
    "NOW", "PANW", "FTNT", "XYZ", "PYPL", "NET", "SNOW", "SHOP", "UBER", "ABNB",
    "RBLX", "DKNG", "NFLX"
]  # SQ was renamed XYZ (Block, Inc.) in 2025


def _tz(name):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return None


ET = _tz("America/New_York")
CT = _tz("America/Chicago")


def now_et():
    return datetime.now(ET) if ET else datetime.now(timezone.utc) - timedelta(hours=4)


def fmt_ct(dt_or_ts):
    dt = datetime.fromtimestamp(dt_or_ts, tz=timezone.utc) if isinstance(dt_or_ts, (int, float)) else dt_or_ts
    return (dt.astimezone(CT) if CT else dt).strftime('%Y-%m-%d %H:%M:%S CT')


def parse_ts(s):
    ts = pd.Timestamp(s)
    return (ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")).to_pydatetime()


def load_state():
    default_state = {
        "closed_trades_ledger": [],
        "active_position_cache": {},
        "last_run_timestamp": "",
        "last_traded_candle": {},
        "cooldown_blocklist": {},
        "order_reasons": {},
        "order_type_cache": {},
        "entries_paused_day": "",
        "halted_day": "",
        "last_email_timestamp": 0
    }
    data = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error loading {STATE_FILE}: {e}", flush=True)
    for k, v in default_state.items():
        if k not in data:
            data[k] = v
    data.pop("daily_starting_equity", None)
    data.pop("previous_active_symbols", None)
    return data


def save_state(state):
    state["last_run_timestamp"] = fmt_ct(time.time())
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def api_retry(func, *args, retries=4, delay=2.0, **kwargs):
    """Retries only on network errors, 429 and 5xx. A 4xx (rejected order, no buying power...) is final;
    re-sending it just repeats the rejection or, worse, duplicates an order."""
    res = None
    for attempt in range(retries):
        try:
            res = func(*args, timeout=20, **kwargs)
            if res.status_code == 429 or res.status_code >= 500:
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2.0
                    continue
            return res
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2.0
            else:
                raise e
    return res


class _DryResp:
    status_code = 200
    text = "dry-run"

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"id": f"dry-{uuid.uuid4().hex[:8]}", "status": "accepted"}

    def json(self):
        return self._payload


class Alpaca:
    def __init__(self, audit_logs):
        self.audit_logs = audit_logs

    def get(self, path, params=None, data=False):
        url = (DATA_URL if data else BASE_URL) + path
        return api_retry(requests.get, url, params=params, headers=HEADERS)

    def _write(self, method, path, payload=None, params=None):
        if DRY_RUN:
            self.audit_logs.append(f"🧪 [DRY RUN] would {method.upper()} {path} {json.dumps(payload) if payload else ''}{params or ''}")
            return _DryResp([] if path == "/v2/positions" and method == "delete" else None)
        fn = getattr(requests, method)
        kw = {"headers": HEADERS}
        if payload is not None:
            kw["json"] = payload
        if params is not None:
            kw["params"] = params
        return api_retry(fn, BASE_URL + path, **kw)

    def post(self, path, payload):
        return self._write("post", path, payload)

    def patch(self, path, payload):
        return self._write("patch", path, payload)

    def delete(self, path, params=None):
        return self._write("delete", path, params=params)


def ok(res):
    return res is not None and res.status_code in (200, 201, 202, 204, 207)


def send_html_dashboard_email(subject, html_content, text_fallback):
    if DRY_RUN:
        print(f"[DRY RUN] Email skipped: {subject}\n{text_fallback}", flush=True)
        return

    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("[WARN] Email credentials missing in environment variables. Skipping email dispatch.", flush=True)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email

    msg.attach(MIMEText(text_fallback, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Equities telemetry email sent successfully.", flush=True)
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)


def round_sig_figs(val, sig_figs=5):
    if val == 0 or val is None:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)


def round_stock_px(px):
    """US equities: penny increments at/above $1, 4 decimals below $1. Anything else is rejected."""
    return round(px, 2) if px >= 1.0 else round(px, 4)


# ---------------- Market data ----------------

def fetch_bars(api, symbols, timeframe, start_dt, end_dt=None):
    out = {}
    params = {
        "symbols": ",".join(symbols), "timeframe": timeframe, "feed": DATA_FEED, "limit": 10000,
        "adjustment": "raw", "start": start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    }
    if end_dt:
        params["end"] = end_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    page_token = None
    for _ in range(20):
        if page_token:
            params["page_token"] = page_token
        res = api.get("/v2/stocks/bars", params=params, data=True)
        if not ok(res):
            break
        body = res.json()
        for sym, bl in (body.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bl)
        page_token = body.get("next_page_token")
        if not page_token:
            break
    return out


def bar_et(b):
    dt = parse_ts(b["t"])
    return dt.astimezone(ET) if ET else dt


def is_rth_bar(b):
    t = bar_et(b)
    m = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= m < 16 * 60


def calculate_15m_ross_indicators(rth_bars, session_bars):
    """EMA/MACD on regular-hours 15m bars (multi-day); VWAP is TODAY's session VWAP (resets at 9:30 ET)."""
    closes = [float(b["c"]) for b in rth_bars]
    if len(closes) < 30 or not session_bars:
        return None

    df_c = pd.Series(closes)
    ema9_series = df_c.ewm(span=9, adjust=False).mean()

    s_h = np.array([float(b["h"]) for b in session_bars])
    s_l = np.array([float(b["l"]) for b in session_bars])
    s_c = np.array([float(b["c"]) for b in session_bars])
    s_v = np.array([float(b["v"]) for b in session_bars])
    cum_v = s_v.sum()
    vwap = float(((s_h + s_l + s_c) / 3.0 * s_v).sum() / cum_v) if cum_v > 0 else s_c[-1]

    ema12 = df_c.ewm(span=12, adjust=False).mean()
    ema26 = df_c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    return {
        "ema9": ema9_series.iloc[-1],
        "vwap": vwap,
        "macd_hist": (macd_line - signal_line).iloc[-1],
        "prev_close": closes[-2],
        "prev_ema9": ema9_series.iloc[-2]
    }


def time_of_day_rvol(rth_bars, now_utc):
    """Paced volume of the live 15m bar vs. the SAME 15m slot on previous sessions (true RVOL).
    Comparing 9:45 volume against yesterday's 3:45 volume is meaningless."""
    cur = rth_bars[-1]
    cur_et = bar_et(cur)
    slot = (cur_et.hour, cur_et.minute)
    elapsed_mins = max(0.5, (now_utc - parse_ts(cur["t"])).total_seconds() / 60.0)
    paced = float(cur["v"]) * min(3.0, 15.0 / elapsed_mins)

    same_slot = [float(b["v"]) for b in rth_bars[:-1] if (bar_et(b).hour, bar_et(b).minute) == slot and bar_et(b).date() != cur_et.date()]
    if len(same_slot) >= 2:
        base = float(np.mean(same_slot[-5:]))
    else:
        vols = [float(b["v"]) for b in rth_bars]
        base = float(np.mean(vols[-12:-2])) if len(vols) >= 12 else float(np.mean(vols[:-1]))
    return (paced / base) if base > 0 else 1.0, elapsed_mins


def check_spy_regime(api, now_utc):
    """RISK_OFF: SPY below yesterday's close AND below today's VWAP -> no new longs.
    CAUTION: one of the two -> strict gates. RISK_ON: both fine.
    (Old check compared two daily closes; mid-session that's just 'is SPY green', and its labels said Open/Close.)"""
    try:
        bars = fetch_bars(api, ["SPY"], "15Min", now_utc - timedelta(days=6)).get("SPY", [])
        rth = [b for b in bars if is_rth_bar(b)]
        if not rth:
            return "CAUTION", "No SPY data — using strict gates"
        today = bar_et(rth[-1]).date()
        session = [b for b in rth if bar_et(b).date() == today]
        prior = [b for b in rth if bar_et(b).date() < today]
        if not prior or not session:
            return "CAUTION", "Insufficient SPY history — using strict gates"
        prev_close = float(prior[-1]["c"])
        last = float(rth[-1]["c"])
        v = sum(float(b["v"]) for b in session)
        vwap = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in session) / v if v > 0 else last
        desc = f"SPY ${last:.2f} | prev close ${prev_close:.2f} ({(last/prev_close-1)*100:+.2f}%) | VWAP ${vwap:.2f}"
        below_close, below_vwap = last < prev_close, last < vwap
        if below_close and below_vwap:
            return "RISK_OFF", desc
        if below_close or below_vwap:
            return "CAUTION", desc
        return "RISK_ON", desc
    except Exception as e:
        return "CAUTION", f"SPY regime check failed ({e}) — using strict gates"


def check_spread(api, symbols):
    """Returns {symbol: (ok, msg)}. Missing/one-sided IEX quotes pass (IEX shows only its own book)."""
    out = {s: (True, "no quote") for s in symbols}
    try:
        res = api.get("/v2/stocks/quotes/latest", params={"symbols": ",".join(symbols), "feed": DATA_FEED}, data=True)
        if ok(res):
            for s, q in (res.json().get("quotes") or {}).items():
                bp, ap = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
                if bp > 0 and ap > 0 and ap >= bp:
                    spread = (ap - bp) / ((ap + bp) / 2.0)
                    out[s] = (spread <= MAX_SPREAD_PCT, f"spread {spread*100:.3f}%")
    except Exception:
        pass
    return out


# ---------------- Ledger (rebuilt every run from Alpaca fills -> never duplicates) ----------------

def fetch_fill_activities(api, after_dt):
    fills, page_token = [], None
    for _ in range(20):
        params = {"after": after_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "direction": "asc", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        res = api.get("/v2/account/activities/FILL", params=params)
        if not ok(res):
            break
        batch = res.json() or []
        fills.extend(batch)
        if len(batch) < 100:
            break
        page_token = batch[-1].get("id")
    return fills


def rebuild_ledger(api, state, now_ts):
    """FIFO-matches buy and sell fills from the last LEDGER_LOOKBACK_DAYS days into closed trades, one per sell order.
    Covers bot exits, native stop fills and manual sells alike."""
    fills = fetch_fill_activities(api, datetime.fromtimestamp(now_ts, tz=timezone.utc) - timedelta(days=LEDGER_LOOKBACK_DAYS))
    lots, sells = {}, {}
    for f in sorted(fills, key=lambda x: x.get("transaction_time", "")):
        sym = f.get("symbol")
        qty = float(f.get("qty", 0) or 0)
        px = float(f.get("price", 0) or 0)
        ts = parse_ts(f["transaction_time"]).timestamp()
        if f.get("side") == "buy":
            lots.setdefault(sym, []).append([qty, px])
            continue
        if f.get("side") not in ("sell",):
            continue
        matched_qty, cost = 0.0, 0.0
        remaining = qty
        q = lots.get(sym, [])
        while remaining > 1e-9 and q:
            take = min(remaining, q[0][0])
            matched_qty += take
            cost += take * q[0][1]
            q[0][0] -= take
            remaining -= take
            if q[0][0] <= 1e-9:
                q.pop(0)
        if matched_qty <= 0:
            continue  # bought before the lookback window
        g = sells.setdefault(f.get("order_id"), {"symbol": sym, "qty": 0.0, "cost": 0.0, "proceeds": 0.0, "ts": ts})
        g["qty"] += matched_qty
        g["cost"] += cost
        g["proceeds"] += matched_qty * px
        g["ts"] = max(g["ts"], ts)

    reasons = state.setdefault("order_reasons", {})
    type_cache = state.setdefault("order_type_cache", {})
    ledger = []
    for oid, g in sells.items():
        reason = (reasons.get(oid) or [None])[0]
        if not reason:
            o_type = type_cache.get(oid)
            if o_type is None:
                res = api.get(f"/v2/orders/{oid}")
                o_type = res.json().get("type", "unknown") if ok(res) else "unknown"
                type_cache[oid] = o_type
            reason = "🛡️ Native stop hit (exchange-side)" if o_type in ("stop", "stop_limit", "trailing_stop") else f"Manual / external {o_type} sell"
        pnl = g["proceeds"] - g["cost"]
        entry_px = g["cost"] / g["qty"]
        exit_px = g["proceeds"] / g["qty"]
        ledger.append({
            "symbol": g["symbol"], "qty": g["qty"],
            "entry_price": round_sig_figs(entry_px, 6), "exit_price": round_sig_figs(exit_px, 6),
            "realized_pnl": round(pnl, 2), "pct": round((exit_px / entry_px - 1) * 100, 2),
            "exit_reason": reason, "timestamp": fmt_ct(g["ts"]), "ts_sec": g["ts"]
        })

    ledger.sort(key=lambda t: t["ts_sec"], reverse=True)
    state["closed_trades_ledger"] = ledger[:30]

    cutoff = now_ts - 7 * 86400
    state["order_reasons"] = {k: v for k, v in reasons.items() if isinstance(v, list) and v[1] >= cutoff}
    state["order_type_cache"] = {k: v for k, v in type_cache.items() if k in sells}

    # Cooldowns from actual exit time.
    for t in ledger:
        until = t["ts_sec"] + (WIN_COOLDOWN_SEC if t["realized_pnl"] > 0 else LOSS_COOLDOWN_SEC)
        if until > now_ts:
            state["cooldown_blocklist"][t["symbol"]] = max(float(state["cooldown_blocklist"].get(t["symbol"], 0)), until)
    state["cooldown_blocklist"] = {s: u for s, u in state["cooldown_blocklist"].items() if u > now_ts and u > cutoff}
    return ledger


# ---------------- Orders ----------------

def open_stop_orders(open_orders, symbol):
    return [o for o in open_orders if o.get("symbol") == symbol and o.get("side") == "sell"
            and o.get("type") in ("stop", "stop_limit")]


def close_position(api, open_orders, symbol, reason, state, audit_logs):
    """Cancel the symbol's resting orders first (a stop holds the shares, so a sell would be rejected), then close."""
    for o in open_orders:
        if o.get("symbol") == symbol:
            api.delete(f"/v2/orders/{o['id']}")
    time.sleep(0.3)
    res = api.delete(f"/v2/positions/{symbol}")
    if ok(res):
        oid = (res.json() or {}).get("id")
        if oid:
            state["order_reasons"][oid] = [reason, time.time()]
        audit_logs.append(f"✅ CLOSE SENT [{symbol}]: {reason}")
        return True
    audit_logs.append(f"🚨 CLOSE FAILED [{symbol}] ({res.status_code if res is not None else 'ERR'}): {res.text if res is not None else ''}")
    return False


def flatten_all(api, reason, state, audit_logs):
    res = api.delete("/v2/positions", params={"cancel_orders": "true"})
    if ok(res):
        for item in (res.json() or []):
            body = item.get("body") or {}
            if body.get("id"):
                state["order_reasons"][body["id"]] = [reason, time.time()]
            audit_logs.append(f"✅ FLATTEN [{item.get('symbol')}] status {item.get('status')}")
        return True
    audit_logs.append(f"🚨 FLATTEN FAILED ({res.status_code if res is not None else 'ERR'}): {res.text if res is not None else ''}")
    return False


def sync_stop_order(api, open_orders, symbol, qty, stop_px, audit_logs):
    """Exactly one sell stop for the full position at stop_px. Moves it with PATCH (replace), so there's no
    window without a stop. Falls back to cancel + re-place if the replace is rejected."""
    stop_px = round_stock_px(stop_px)
    qty_str = str(int(qty)) if float(qty).is_integer() else str(qty)
    stops = open_stop_orders(open_orders, symbol)

    keep = None
    for o in stops:
        if keep is None and abs(float(o.get("stop_price") or 0) - stop_px) < 0.005 and abs(float(o.get("qty") or 0) - qty) < 1e-6:
            keep = o
    if keep is None and stops:
        target = stops[0]
        res = api.patch(f"/v2/orders/{target['id']}", {"qty": qty_str, "stop_price": str(stop_px)})
        if ok(res):
            keep = {"id": (res.json() or {}).get("id", target["id"])}
            stops = stops[1:]
            audit_logs.append(f"🛡️ STOP MOVED [{symbol}] -> ${stop_px} ({qty_str} sh)")
        else:
            audit_logs.append(f"⚠️ Stop replace rejected [{symbol}] ({res.text if res is not None else 'ERR'}) — re-placing")
    for o in stops:
        if keep is None or o["id"] != keep.get("id"):
            api.delete(f"/v2/orders/{o['id']}")
    if keep is None:
        time.sleep(0.3)
        res = api.post("/v2/orders", {"symbol": symbol, "qty": qty_str, "side": "sell", "type": "stop",
                                      "stop_price": str(stop_px), "time_in_force": "day",
                                      "client_order_id": f"stop-{symbol}-{uuid.uuid4().hex[:10]}"})
        if ok(res):
            audit_logs.append(f"🛡️ STOP PLACED [{symbol}] @ ${stop_px} ({qty_str} sh)")
        else:
            audit_logs.append(f"🚨 STOP PLACEMENT FAILED [{symbol}] @ ${stop_px}: {res.text if res is not None else 'ERR'}")


def place_entry_with_stop(api, symbol, qty, is_market, limit_px, stop_px, audit_logs):
    """OTO order: the stop is created with the entry and activates the moment it fills."""
    payload = {
        "symbol": symbol, "qty": str(qty), "side": "buy",
        "type": "market" if is_market else "limit", "time_in_force": "day",
        "order_class": "oto", "stop_loss": {"stop_price": str(round_stock_px(stop_px))},
        "client_order_id": f"entry-{symbol}-{uuid.uuid4().hex[:10]}"
    }
    if not is_market:
        payload["limit_price"] = str(round_stock_px(limit_px))
    res = api.post("/v2/orders", payload)
    if ok(res):
        audit_logs.append(f"⚡ ENTRY {'MARKET' if is_market else 'LIMIT @ $' + payload['limit_price']} [{symbol}] {qty} sh + stop @ ${payload['stop_loss']['stop_price']} attached")
        return True
    audit_logs.append(f"⚠️ OTO entry rejected [{symbol}] ({res.status_code if res is not None else 'ERR'}: {res.text if res is not None else ''}) — trying plain entry")
    payload.pop("order_class")
    payload.pop("stop_loss")
    payload["client_order_id"] = f"entry-{symbol}-{uuid.uuid4().hex[:10]}"
    res = api.post("/v2/orders", payload)
    if ok(res):
        audit_logs.append(f"⚡ ENTRY [{symbol}] {qty} sh (stop will be placed next run once filled)")
        return True
    audit_logs.append(f"ORDER REJECTED BY ALPACA [{res.status_code if res is not None else 'ERR'}] on {symbol}: {res.text if res is not None else 'No response'}")
    return False


def calculate_stock_retention_targets(entry_px, current_px, entry_candle_low, peak_pct=0.0, upper_wick_pct=0.0):
    """Long-only. Same ladder as before, plus the crypto bot's failed-V structure stop.
    Fix: the old code did max(stop, entry_candle_low) unconditionally, so a candle low ABOVE the entry put the
    stop above the entry price (instant loss exit)."""
    pct = (current_px - entry_px) / entry_px
    drawdown_from_peak = peak_pct - pct
    is_exhaustion = (peak_pct >= CLIMAX_TRIGGER) and (drawdown_from_peak >= 0.0030 or upper_wick_pct >= 0.45)

    if is_exhaustion:
        floor_pct = max(peak_pct - 0.0020, pct)
        status = f"⚡ CLIMAX EXHAUSTION LOCK [Peak +{peak_pct*100:.2f}% -> Stop +{floor_pct*100:.2f}%]"
    elif peak_pct >= MOONSHOT_TRIGGER:
        floor_pct = max(peak_pct * 0.88, peak_pct - 0.0050)
        status = f"🚀 MOONSHOT (88% LOCKED) [Peak +{peak_pct*100:.2f}% -> Stop +{floor_pct*100:.2f}%]"
    elif peak_pct >= EXPANSION_TRIGGER:
        floor_pct = max(peak_pct * 0.80, peak_pct - 0.0040)
        status = f"🎯 EXPANSION RATCHET (80% LOCKED) [Peak +{peak_pct*100:.2f}% -> Stop +{floor_pct*100:.2f}%]"
    elif peak_pct >= FEE_FLOOR_TRIGGER:
        floor_pct = FEE_FLOOR_LOCK
        status = f"🛡 PROFIT FLOOR LOCKED (+{FEE_FLOOR_LOCK*100:.2f}%)"
    else:
        floor_pct = None
        status = f"🛡 HARD STOP ({HARD_STOP_PCT*100:.2f}%)"

    cap_px = entry_px * (1 + HARD_STOP_PCT)
    try:
        structure_low = float(entry_candle_low) if entry_candle_low else 0.0
    except (TypeError, ValueError):
        structure_low = 0.0
    protective_px = max(structure_low * 0.9995, cap_px) if 0 < structure_low < entry_px else cap_px
    if floor_pct is None:
        stop_px = protective_px
        if protective_px > cap_px * 1.0001:
            status = f"🛡 FAILED-V LOW @ ${protective_px:.2f} (cap {HARD_STOP_PCT*100:.2f}%)"
    else:
        stop_px = max(entry_px * (1 + floor_pct), protective_px)
    return stop_px, status


def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    now_utc = datetime.now(timezone.utc)
    et_now = now_et()
    today_et = et_now.date()
    today_str = today_et.isoformat()

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    audit_logs = []
    api = Alpaca(audit_logs)
    state = load_state()

    # Alpaca's clock knows weekends, holidays and early closes.
    clock_res = api.get("/v2/clock")
    if not ok(clock_res):
        raise Exception(f"Failed to fetch Alpaca clock: {clock_res.text if clock_res is not None else 'No Response'}")
    clock = clock_res.json()
    is_open = bool(clock.get("is_open"))
    close_dt = parse_ts(clock["next_close"])
    open_dt = (datetime(today_et.year, today_et.month, today_et.day, 9, 30, tzinfo=ET) if ET else now_utc).astimezone(timezone.utc)

    positions_res = api.get("/v2/positions")
    positions_list = positions_res.json() if ok(positions_res) else []

    if not is_open and not positions_list:
        print(f"[{timestamp}] Market CLOSED ({et_now.strftime('%a %H:%M')} ET / {fmt_ct(now_ts)}). 0 positions. Skipping run silently.", flush=True)
        return

    audit_logs.append(f"[{timestamp}] {BOT_TITLE} Engine Started{' (DRY RUN — no orders sent)' if DRY_RUN else ''}. NY {et_now.strftime('%H:%M')} ET / TX {fmt_ct(now_ts)[11:16]} CT")

    account_res = api.get("/v2/account")
    if not ok(account_res):
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text if account_res is not None else 'No Response'}")
    account_data = account_res.json()
    equity = float(account_data.get("equity", 0.0))
    last_equity = float(account_data.get("last_equity", equity) or equity)   # equity at yesterday's close
    cash = float(account_data.get("cash", 0.0))
    today_total_gain = equity - last_equity
    lifetime_cumulative_pnl = equity - STARTING_CAPITAL

    minutes_to_close = (close_dt - now_utc).total_seconds() / 60.0
    minutes_since_open = (now_utc - open_dt).total_seconds() / 60.0
    is_eod = is_open and minutes_to_close <= EOD_FLATTEN_BEFORE_CLOSE_MINS
    entries_window = is_open and minutes_since_open >= NO_ENTRY_FIRST_MINS and minutes_to_close > NO_NEW_ENTRIES_BEFORE_CLOSE_MINS

    orders_res = api.get("/v2/orders", params={"status": "open", "limit": 500})
    open_orders = orders_res.json() if ok(orders_res) else []

    # 1) Cancel last run's unfilled entries (Day limit orders used to rest for hours, fill on a stale setup,
    #    and weren't counted toward the 3 slots). Cancelling an OTO parent also cancels its stop.
    if is_open:
        purged = False
        for o in open_orders:
            if o.get("side") == "buy" and o.get("type") in ("limit", "market"):
                api.delete(f"/v2/orders/{o['id']}")
                purged = True
                audit_logs.append(f"🧹 PURGED UNFILLED ENTRY [{o.get('symbol')}] {o.get('qty')} @ {o.get('limit_price') or 'MKT'}")
        if purged and not DRY_RUN:
            time.sleep(1.0)
            positions_res = api.get("/v2/positions")
            positions_list = positions_res.json() if ok(positions_res) else positions_list
            orders_res = api.get("/v2/orders", params={"status": "open", "limit": 500})
            open_orders = orders_res.json() if ok(orders_res) else open_orders

    # 2) Ledger straight from Alpaca fills (includes native stop fills between runs).
    ledger = rebuild_ledger(api, state, now_ts)
    trades_today = [t for t in ledger if t["ts_sec"] >= open_dt.timestamp() - 6 * 3600]
    trade_closed_this_run = False
    trades_executed = False

    # 3) Breakers.
    entries_blocked_reason = ""
    if state.get("halted_day") == today_str:
        entries_blocked_reason = "Daily loss limit hit earlier today"
    elif last_equity > 0 and today_total_gain <= -DAILY_LOSS_LIMIT_PCT * last_equity:
        state["halted_day"] = today_str
        entries_blocked_reason = f"Daily loss limit: ${today_total_gain:+,.2f} ≤ -{DAILY_LOSS_LIMIT_PCT*100:.1f}% of ${last_equity:,.2f}"
        audit_logs.append(f"🚨 DAILY LOSS LIMIT: {entries_blocked_reason}. Flattening to cash, done for today.")
        if positions_list and is_open:
            flatten_all(api, "🚨 Daily loss limit flatten", state, audit_logs)
            positions_list, open_orders = [], []
            trade_closed_this_run = True
        send_html_dashboard_email("🚨 Alpaca: Daily loss limit hit — flat for today", f"<h3>{entries_blocked_reason}</h3>", entries_blocked_reason)

    recent_losses = [t for t in ledger if t["realized_pnl"] < 0 and t["ts_sec"] >= now_ts - CB_WINDOW_SEC]
    if not entries_blocked_reason and len(recent_losses) >= CB_MAX_LOSSES and state.get("entries_paused_day") != today_str:
        state["entries_paused_day"] = today_str
        msg = f"🚨 ROLLING CIRCUIT BREAKER: {len(recent_losses)} losses in {CB_WINDOW_SEC // 60}m. No new entries for the rest of today."
        audit_logs.append(msg)
        send_html_dashboard_email("🚨 Alpaca: Circuit breaker — entries paused today", f"<h3>{msg}</h3><p>Open positions are still managed.</p>", msg)
    if not entries_blocked_reason and state.get("entries_paused_day") == today_str:
        entries_blocked_reason = "Rolling circuit breaker (3 losses / 60m) — paused for today"

    # Pattern-day-trader guard: under $25k a 4th day trade in 5 days gets the account restricted.
    if not entries_blocked_reason and equity < 25000 and int(account_data.get("daytrade_count", 0) or 0) >= 3:
        entries_blocked_reason = f"PDT guard: {account_data.get('daytrade_count')} day trades in 5 days with equity < $25k"

    # 4) End of day: flatten everything to cash (handles half days via Alpaca's clock).
    if is_eod and positions_list:
        audit_logs.append(f"🔔 EOD SQUARE-OFF ({minutes_to_close:.0f}m to close): liquidating all positions to cash.")
        flatten_all(api, f"🔔 EOD square-off ({EOD_FLATTEN_BEFORE_CLOSE_MINS}m before close)", state, audit_logs)
        positions_list, open_orders = [], []
        trade_closed_this_run = True

    if not is_open and positions_list:
        audit_logs.append(f"⚠️ MARKET CLOSED WITH {len(positions_list)} OPEN POSITION(S): {', '.join(p['symbol'] for p in positions_list)}. They will be closed at the next open.")

    # 5) Manage open positions.
    active_count = 0
    positions_data = []
    active_symbols = set()
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}

    for pos in positions_list:
        symbol = pos.get("symbol")
        qty = abs(float(pos.get("qty", 0)))
        is_long = pos.get("side", "long") == "long"
        entry_px = float(pos.get("avg_entry_price", 0))
        current_px = float(pos.get("current_price", entry_px))
        market_value = float(pos.get("market_value", 0))
        unrealized_pnl = float(pos.get("unrealized_pl", 0))
        current_pct = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)

        cache_info = current_active_cache.get(symbol, {})
        entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9930)
        entry_ts = float(cache_info.get("entry_time_sec", now_ts))
        held_mins = (now_ts - entry_ts) / 60.0

        if not is_open:
            active_symbols.add(symbol)
            active_count += 1
            new_active_cache[symbol] = cache_info
            positions_data.append({"symbol": symbol, "side": "LONG" if is_long else "SHORT", "qty": qty, "entry": entry_px,
                                   "current": current_px, "market_value": market_value, "pnl": unrealized_pnl,
                                   "roe": current_pct * 100, "stop": "—", "status": "⚠️ Held while market closed"})
            continue

        if not is_long:
            close_position(api, open_orders, symbol, "Unexpected SHORT position closed (bot is long-only)", state, audit_logs)
            trade_closed_this_run = True
            continue

        # Peak from real 1-minute highs since entry (not just the prices seen at run times).
        peak_px = current_px
        upper_wick_ratio = 0.0
        try:
            since = datetime.fromtimestamp(max(entry_ts, open_dt.timestamp()), tz=timezone.utc)
            m1 = fetch_bars(api, [symbol], "1Min", since).get(symbol, [])
            if m1:
                peak_px = max([float(b["h"]) for b in m1] + [current_px])
            b15 = fetch_bars(api, [symbol], "15Min", now_utc - timedelta(hours=1)).get(symbol, [])
            if b15:
                c_high, c_low, c_open = float(b15[-1]["h"]), float(b15[-1]["l"]), float(b15[-1]["o"])
                upper_wick_ratio = (c_high - max(c_open, current_px)) / max(1e-8, c_high - c_low)
        except Exception:
            pass
        peak_pct = max(current_pct, (peak_px - entry_px) / entry_px, float(cache_info.get("peak_roe", current_pct)))

        stop_px_calc, leash_status = calculate_stock_retention_targets(
            entry_px, current_px, entry_candle_low, peak_pct=peak_pct, upper_wick_pct=upper_wick_ratio
        )

        exit_reason = ""
        if entry_ts < open_dt.timestamp() and cache_info:
            exit_reason = "Overnight leftover — closing at open"
        elif current_px <= stop_px_calc:
            exit_reason = f"🎯 Stop/ratchet hit ({current_pct*100:+.2f}%, peak +{peak_pct*100:.2f}%)"
        elif held_mins >= FLAT_EXIT_MINS and current_pct < FLAT_EXIT_PCT and peak_pct < FEE_FLOOR_TRIGGER:
            exit_reason = f"⏱ {FLAT_EXIT_MINS:.0f}m flat exit ({current_pct*100:+.2f}% after {held_mins:.0f}m)"

        if exit_reason:
            audit_logs.append(f"🎯 EXIT TRIGGERED [{symbol}] @ ${current_px:.2f}: {exit_reason}")
            if close_position(api, open_orders, symbol, f"🤖 {exit_reason}", state, audit_logs):
                cd = WIN_COOLDOWN_SEC if unrealized_pnl > 0 else LOSS_COOLDOWN_SEC
                state["cooldown_blocklist"][symbol] = max(float(state["cooldown_blocklist"].get(symbol, 0)), now_ts + cd)
                trade_closed_this_run = True
                continue

        sync_stop_order(api, open_orders, symbol, qty, stop_px_calc, audit_logs)

        active_count += 1
        active_symbols.add(symbol)
        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "peak_roe": peak_pct,
            "entry_candle_low": entry_candle_low, "entry_time_sec": entry_ts,
            "strategy": cache_info.get("strategy", "UNKNOWN")
        }
        positions_data.append({
            "symbol": symbol, "side": "LONG", "qty": qty, "entry": entry_px, "current": current_px,
            "market_value": market_value, "pnl": unrealized_pnl, "roe": current_pct * 100,
            "stop": round_stock_px(stop_px_calc), "status": leash_status
        })

    state["active_position_cache"] = new_active_cache

    # 6) Scan.
    regime, regime_desc = ("—", "market closed")
    market_candidates = []
    scanned_count = 0
    if is_open:
        regime, regime_desc = check_spy_regime(api, now_utc)
        audit_logs.append(f"📈 SPY Regime: **{regime}** — {regime_desc}")
        if not entries_blocked_reason and regime == "RISK_OFF" and BLOCK_ENTRIES_WHEN_RISK_OFF:
            entries_blocked_reason = "SPY RISK_OFF (below prior close and VWAP)"

    strict = regime != "RISK_ON"
    required_vol_ratio = 2.50 if strict else 1.80
    max_vwap_extension = 1.0040 if strict else 1.0080

    if not entries_window and is_open and not is_eod:
        if minutes_since_open < NO_ENTRY_FIRST_MINS:
            entries_blocked_reason = entries_blocked_reason or f"Opening {NO_ENTRY_FIRST_MINS}m — no entries until 9:{30 + NO_ENTRY_FIRST_MINS} ET"
        else:
            entries_blocked_reason = entries_blocked_reason or f"Last {NO_NEW_ENTRIES_BEFORE_CLOSE_MINS}m before close — no new entries"
    if entries_blocked_reason:
        audit_logs.append(f"⛔ NEW ENTRIES BLOCKED: {entries_blocked_reason}")

    available_slots = MAX_STOCK_SLOTS - active_count
    if entries_window and not entries_blocked_reason and available_slots > 0:
        audit_logs.append(f"⚡ {BOT_VERSION}: RVOL gate {required_vol_ratio}x (time-of-day) | VWAP cap +{(max_vwap_extension-1)*100:.2f}% | EMA9 cap +{EMA9_MAX_EXTENSION*100:.1f}% | Hard stop {HARD_STOP_PCT*100:.1f}%")
        symbols_to_scan = [s for s in WATCHLIST if s not in active_symbols]
        all_bars = fetch_bars(api, symbols_to_scan, "15Min", now_utc - timedelta(days=7))

        for symbol in symbols_to_scan:
            cooldown_until = float(state["cooldown_blocklist"].get(symbol, 0))
            if now_ts < cooldown_until:
                audit_logs.append(f"🔍 [{symbol}] Skipped: Cooldown ({(cooldown_until - now_ts) / 60:.0f}m left)")
                continue
            rth = [b for b in all_bars.get(symbol, []) if is_rth_bar(b)]
            if len(rth) < 30:
                continue
            session = [b for b in rth if bar_et(b).date() == today_et]
            if not session:
                continue
            if state.get("last_traded_candle", {}).get(symbol) == rth[-1]["t"]:
                continue
            scanned_count += 1

            try:
                ind = calculate_15m_ross_indicators(rth, session)
                if not ind:
                    continue
                closes = [float(b["c"]) for b in rth]
                highs = [float(b["h"]) for b in rth]
                lows = [float(b["l"]) for b in rth]
                curr_px = closes[-1]
                ema9, vwap, macd_hist = ind["ema9"], ind["vwap"], ind["macd_hist"]
                day_high = max(float(b["h"]) for b in session)

                extension = (curr_px - ema9) / ema9
                if extension > EMA9_MAX_EXTENSION:
                    audit_logs.append(f"🔍 [{symbol}] Skipped: Too extended from EMA9 (+{extension*100:.2f}%)")
                    continue

                vol_ratio, _ = time_of_day_rvol(rth, now_utc)
                vwap_dist = curr_px / vwap if vwap > 0 else 1.0
                above_ema9, above_vwap, macd_pos = curr_px > ema9, curr_px > vwap, macd_hist > 0.0
                vwap_ok = vwap_dist <= max_vwap_extension

                is_breakout = curr_px >= day_high * (1 - HOD_PROXIMITY_PCT) and vol_ratio >= required_vol_ratio and vwap_ok and above_ema9 and above_vwap and macd_pos

                reclaim_cross = (ind["prev_close"] <= ind["prev_ema9"] or ind["prev_close"] <= vwap) and above_ema9 and above_vwap
                is_reclaim = reclaim_cross and curr_px <= ema9 * 1.0025 and vol_ratio >= required_vol_ratio and vwap_ok and macd_pos

                prev_45 = min(lows[-4:-1])
                c_range = max(1e-8, highs[-1] - lows[-1])
                lower_wick = (min(float(rth[-1]["o"]), curr_px) - lows[-1]) / c_range
                is_v_bottom = lows[-1] <= prev_45 and lower_wick >= 0.45 and vol_ratio >= required_vol_ratio and vwap_ok and above_ema9 and macd_pos

                if not (is_breakout or is_reclaim or is_v_bottom):
                    continue
                if is_v_bottom:
                    setup, score = "V_BOTTOM_RECLAIM", vol_ratio * 3.2
                elif is_reclaim:
                    setup, score = "BULLISH_RECLAIM", vol_ratio * 2.4
                else:
                    setup, score = "HOD_BREAKOUT", vol_ratio * 1.0

                market_candidates.append({
                    "symbol": symbol, "close": curr_px, "limit_px": min(curr_px * 0.9985, max(ema9, vwap)),
                    "score": score, "entry_candle_low": min(lows[-1], lows[-2]), "strategy": setup, "candle_ts": rth[-1]["t"]
                })
                audit_logs.append(f"🔥 TRIGGER [{setup}] {symbol} @ ${curr_px:.2f} (RVOL {vol_ratio:.2f}x, req {required_vol_ratio}x)")
            except Exception as e:
                audit_logs.append(f"⚠️ [{symbol}] scan error: {e}")

        audit_logs.append(f"ℹ️ SCAN COMPLETE: {scanned_count} symbols evaluated, {len(market_candidates)} trigger(s).")

        market_candidates.sort(key=lambda x: x["score"], reverse=True)
        if market_candidates:
            spreads = check_spread(api, [c["symbol"] for c in market_candidates[:5]])
            entries = 0
            buying_power = float(account_data.get("buying_power", equity) or equity)
            for c in market_candidates:
                if entries >= min(MAX_NEW_ENTRIES_PER_RUN, available_slots):
                    break
                sp_ok, sp_msg = spreads.get(c["symbol"], (True, "unchecked"))
                if not sp_ok:
                    audit_logs.append(f"🔍 [{c['symbol']}] Skipped: {sp_msg}")
                    continue
                is_market = c["strategy"] == "HOD_BREAKOUT"
                entry_px = c["close"] if is_market else c["limit_px"]
                target_usd = min(max(MIN_POSITION_USD, equity * POSITION_FRACTION), buying_power * 0.95)
                qty = int(target_usd / entry_px)
                if qty < 1:
                    audit_logs.append(f"🔍 [{c['symbol']}] Skipped: not enough buying power for 1 share")
                    continue
                stop_px, _ = calculate_stock_retention_targets(entry_px, entry_px, c["entry_candle_low"])
                if place_entry_with_stop(api, c["symbol"], qty, is_market, c["limit_px"], stop_px, audit_logs):
                    entries += 1
                    trades_executed = True
                    state.setdefault("last_traded_candle", {})[c["symbol"]] = c["candle_ts"]
                    state["active_position_cache"][c["symbol"]] = {
                        "entry_px": entry_px, "entry_candle_low": c["entry_candle_low"], "qty": qty,
                        "strategy": c["strategy"], "entry_time_sec": now_ts
                    }

    if trade_closed_this_run and not DRY_RUN:
        time.sleep(1.5)
        ledger = rebuild_ledger(api, state, time.time())
        trades_today = [t for t in ledger if t["ts_sec"] >= open_dt.timestamp() - 6 * 3600]

    # 7) Email (every 15 min, or immediately on any entry/exit, or on manual runs).
    last_email_ts = float(state.get("last_email_timestamp", 0))
    is_manual_run = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"
    should_send_email = is_manual_run or (now_ts - last_email_ts) / 60.0 >= 15.0 or trades_executed or trade_closed_this_run
    if should_send_email:
        state["last_email_timestamp"] = now_ts
    save_state(state)
    if not should_send_email:
        print(f"[{timestamp}] Cycle complete. Skipping email dispatch.", flush=True)
        for line in audit_logs:
            print(line, flush=True)
        return

    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0
    total_today = len(trades_today)
    win_count = len([t for t in trades_today if t["realized_pnl"] > 0])
    loss_count = total_today - win_count
    win_rate_today = (win_count / total_today * 100) if total_today else 0.0
    realized_today = sum(t["realized_pnl"] for t in trades_today)

    positions_cards = ""
    if positions_data:
        for p in positions_data:
            pnl_color = '#2e7d32' if p['pnl'] >= 0 else '#c62828'
            positions_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{p['symbol']}</span>
                  <span style="font-size: 10px; color: #2e7d32; font-weight: 700; margin-left: 6px;">{p['side']} {p['qty']:g} sh</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${p['pnl']:+.2f} ({p['roe']:+.2f}%)</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Val:</b> ${p['market_value']:.2f} &bull; <b>Buy:</b> ${p['entry']:.2f} &bull; <b>Stop:</b> ${p['stop']}
              </div>
              <div style="font-size: 10px; color: #2e7d32; font-weight: 600;">
                <b>Status:</b> {p['status']}
              </div>
            </div>
            """
    else:
        positions_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No active positions (100% Cash Flat).</div>'

    closed_cards = ""
    recent = ledger[:10]
    total_realized_pnl = sum(t["realized_pnl"] for t in recent)
    if recent:
        for t in recent:
            pnl_color = '#2e7d32' if t["realized_pnl"] >= 0 else '#c62828'
            closed_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{t['symbol']} <span style="font-size: 10px; color: #64748b;">{t['qty']:g} sh</span></span>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${t['realized_pnl']:+.2f} ({t['pct']:+.2f}%)</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Entry:</b> ${t['entry_price']} &bull; <b>Exit:</b> ${t['exit_price']}
              </div>
              <div style="font-size: 10px; color: #b45309; margin-bottom: 2px; word-break: break-word;">
                <b>Reason:</b> {t['exit_reason']}
              </div>
              <div style="font-size: 10px; color: #94a3b8; font-family: monospace;">{t['timestamp']}</div>
            </div>
            """
    else:
        closed_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No recent exits recorded yet.</div>'
    total_pnl_color = '#2e7d32' if total_realized_pnl >= 0 else '#c62828'

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 8px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 10px; color: #475569; white-space: pre-wrap; word-break: break-all;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div style="font-size: 11px; text-transform: uppercase; color: #d97706; margin: 14px 0 6px 0; font-weight: 700;">Live Telemetry & Audit Log</div>
        <div style="width: 100%; border: 1px solid #fde68a; border-radius: 6px; background-color: #fffbeb; margin-bottom: 15px; box-sizing: border-box;">
          <table style="width: 100%; font-size: 11px; border-collapse: collapse;"><tbody>{audit_rows}</tbody></table>
        </div>
        """

    blocked_banner = ""
    if entries_blocked_reason:
        blocked_banner = f'<div style="background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #b91c1c; font-weight: 700;">⛔ New entries blocked: {entries_blocked_reason}</div>'

    close_ct = fmt_ct(close_dt)[11:16]
    text_fallback = (f"{BOT_TITLE}\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Gain: USD ${today_total_gain:+.2f}\n"
                     f"Lifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}\n\n" + "\n".join(audit_logs))

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  </head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333; -webkit-text-size-adjust: 100%;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); box-sizing: border-box;">

      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff; margin: 0;">TR-GC-Equities-LS-01 | 15M Scalper ({BOT_VERSION}){' — DRY RUN' if DRY_RUN else ''}</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">{timestamp} UTC &bull; {'OPEN' if is_open else 'CLOSED'} &bull; closes {close_ct} CT &bull; SPY: {regime}</div>
      </div>

      <div style="padding: 12px; box-sizing: border-box;">
        {blocked_banner}
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px; box-sizing: border-box;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 2px;">Total Account Equity</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${equity:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px; line-height: 1.6;">
            <div>Invested: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain (vs yesterday's close): <span style="background-color: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
            <div>Lifetime P&amp;L: <b style="color: #0f172a;">${lifetime_cumulative_pnl:+,.2f}</b></div>
          </div>
        </div>

        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 10px; margin-bottom: 14px;">
          <div style="font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase;">📊 Today's Realized ({today_str})</div>
          <div style="font-size: 11px; color: #166534;">Trades: <b>{total_today}</b> &bull; Win Rate: <b>{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></div>
          <div style="font-size: 11px; color: #166534; font-weight: 700; margin-top: 4px;">Realized P&amp;L: <span style="color: {'#15803d' if realized_today >= 0 else '#b91c1c'};">${realized_today:+.2f}</span></div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4; box-sizing: border-box;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active {BOT_VERSION} Rules</div>
          <div>&bull; <b>Hours:</b> Mon–Fri 8:30–3:00 CT (9:30–4:00 ET); entries 8:45–2:30 CT; flat by 2:50 CT; holidays/half days via Alpaca clock</div>
          <div>&bull; <b>Stop attached to entry</b> (OTO), moved by replace — never unprotected</div>
          <div>&bull; <b>Failed-V low or {HARD_STOP_PCT*100:.1f}% cap</b>, ratchet +{FEE_FLOOR_LOCK*100:.2f}% / 80% / 88% locks, climax lock, {FLAT_EXIT_MINS:.0f}m flat exit</div>
          <div>&bull; <b>Setups:</b> HOD breakout, EMA9/VWAP reclaim, V-bottom — session VWAP, time-of-day RVOL</div>
          <div>&bull; <b>Breakers:</b> {CB_MAX_LOSSES} losses/{CB_WINDOW_SEC // 60}m → paused today; -{DAILY_LOSS_LIMIT_PCT*100:.1f}% day → flat for today; SPY RISK_OFF blocks longs</div>
        </div>

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades (from Alpaca fills)</div>
        {closed_cards}

        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          LAST 10 TRADES P&amp;L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        {audit_section}

      </div>

      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Alpaca {BOT_VERSION} Equities Scalper &bull; Managed via GitHub Actions
      </div>
    </div>
  </body>
</html>
"""

    send_html_dashboard_email(f"Alpaca Quantitative Report — USD ${equity:,.2f}{' [DRY RUN]' if DRY_RUN else ''}", html_content, text_fallback)
    print(f"[{timestamp}] {BOT_VERSION} Equities telemetry report complete.", flush=True)


if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Equities Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
