import os
import json
import time
from datetime import datetime, timedelta, timezone
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import log10, floor
import eth_account
import pandas as pd
import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

BOT_VERSION = "V4.12.0"
BOT_TITLE = f"TR-GC-Crypto-LS-23-{BOT_VERSION}"

ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS")
SECRET_KEY = os.getenv("HL_SECRET_KEY")
# DRY_RUN=1 -> reads live data, logs what it WOULD do, sends no orders, writes state_dryrun.json, no email.
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
STATE_FILE = "state_dryrun.json" if DRY_RUN else "state.json"
LEDGER_VERSION = 2

# ---------------- Risk / strategy settings ----------------
LEVERAGE = 5
MAX_CRYPTO_SLOTS = 3
POSITION_FRACTION = 0.25 * 0.85        # notional per trade as a fraction of account value
MIN_NOTIONAL_USD = 10.50

# Trade management = the V4.8.12 "profit-lock" rules that won 5/6 on the night of Oct 20-21.
HARD_CAP_ROE = -0.0350                 # fixed -3.5% ROE stop (-0.7% price at 5x): room to breathe, no candle-low stop
FIRST_LOCK_TRIGGER_ROE = 0.0120        # once peak hits +1.2% ROE...
FIRST_LOCK_ROE = 0.0050                # ...lock +0.5% ROE (V4.8.12 used +0.4%, which is below round-trip fees of ~0.43% ROE)
# Peak ROE tier -> keep this share of the peak (or give back at most this much ROE, whichever locks more)
LOCK_TIERS = [
    (0.1200, 0.93, 0.0080, "🔒 DEEP LOCK 93%"),
    (0.0800, 0.90, 0.0100, "🚀 STRONG LOCK 90%"),
    (0.0500, 0.85, 0.0120, "🎯 GOOD LOCK 85%"),
    (0.0250, 0.80, None,   "🛡 EARLY LOCK 80%"),   # +2.5% peak: keep 80%, never below +0.8% ROE
]
EARLY_LOCK_MIN_ROE = 0.0080
CLIMAX_TRIGGER_ROE = 0.0450            # peak >= +4.5% and a 0.6% pullback or 45% upper wick -> give back only 0.4%
CLIMAX_DRAWDOWN_ROE = 0.0060
CLIMAX_GIVEBACK_ROE = 0.0040

SCRATCH_MINS = 15.0                    # 15m negative scratch: still below -0.5% ROE after 15 minutes -> out
SCRATCH_ROE = -0.0050
FLAT_EXIT_MINS = 30.0                  # 30m stagnation: still below +0.4% ROE after 30 minutes -> out
FLAT_EXIT_ROE = 0.0040

LOSS_COOLDOWN_SEC = 1800
WIN_COOLDOWN_SEC = 14400

CB_MAX_LOSSES = 3                      # this many losing trades...
CB_WINDOW_SEC = 3600                   # ...inside this window -> pause new entries
CB_HIBERNATE_SEC = 43200
DAILY_LOSS_LIMIT_PCT = 0.03            # realized net loss today >= 3% of start equity -> no new entries until CT midnight

BLOCK_ENTRIES_WHEN_RISK_OFF = True     # long-only alt scalps rarely work while BTC trends down
SKIP_COINS = set()                     # add tickers here to never trade them
MIN_24H_VOLUME_USD = 500000.0
SCAN_TOP_N = 60                        # wide net: top 60 24h gainers (V4.8.12 used 20, which rarely triggers)...
SKIP_FIRST_LISTED = 20                 # ...never the first 20 coins Hyperliquid listed (BTC, ETH, SOL, BNB, ...)...
MIN_24H_CHANGE_PCT = 2.0               # ...must actually be moving today...
# ...and must FIT the trade plan: the stop is 0.7% below entry and the first lock needs +0.24%.
# Typical 15m candle range (ATR%) below 0.30% = too slow to reach the lock before the 15/30m time exits (BNB, GRAM).
# Above 1.20% = ordinary noise is bigger than the 0.7% stop, so wicks stop you out (USELESS-type -5%).
MIN_ATR_PCT = 0.30
MAX_ATR_PCT = 1.20
ATR_CANDLES = 14
CANDLE_LOOKBACK_MS = 8 * 3600000       # V4.8.12: indicators and "day high" on the last 8h of 15m candles

# ---------------- Shadow shorts (PAPER ONLY: never sends an order) ----------------
# While BTC is RISK_OFF or CAUTION, scan the top losers for mirrored short setups and track each pretend trade with the same
# stop / profit-lock / time-exit rules on live prices. Results show up as a scorecard in the email.
SHADOW_SHORTS_ENABLED = True
SHADOW_REGIMES = ("RISK_OFF", "CAUTION")   # paper-short whenever BTC isn't clearly rising; each trade is tagged
SHADOW_MAX_OPEN = 3
SHADOW_SCAN_TOP_N = 60
SHADOW_MIN_24H_DROP_PCT = 2.0          # coin must be down at least 2% on the day
SHADOW_LOD_PROXIMITY = 0.0100          # breakdown = within 1% of the 8h low
SHADOW_RVOL = 1.80                     # RISK_OFF is the favourable regime for shorts -> normal (not strict) gates
SHADOW_VWAP_CAP = 1.0080               # not more than 0.8% below VWAP (mirror of the long VWAP stretch cap)
SHADOW_TAKER_FEE = 0.000432            # per side, as shown on Hyperliquid
SHADOW_HISTORY_DAYS = 14

# Scorecard for the real (long) trades of the current strategy: V4.10.1 went live 2026-10-21 18:05 CT (23:05 UTC).
# Earlier trades came from older rule sets and would blur the numbers.
SCORECARD_SINCE_TS = datetime(2026, 10, 21, 23, 5, tzinfo=timezone.utc).timestamp()
NEAR_MISS_MAX_FAILS = 2                # log coins that missed a setup by this many rules or fewer
NEAR_MISS_SHOW = 3                     # how many near misses to list per run


def ct_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("America/Chicago")
        except Exception:
            pass
    return None


def _fallback_ct_offset_hours(utc_now):
    year = utc_now.year
    dst_start = datetime(year, 3, 8, 8, 0, 0)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1, 7, 0, 0)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    return -5 if dst_start <= utc_now < dst_end else -6


def get_central_time():
    tz = ct_tz()
    if tz is not None:
        return datetime.now(tz).replace(tzinfo=None)
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    return utc_now + timedelta(hours=_fallback_ct_offset_hours(utc_now))


def get_central_midnight_timestamp():
    tz = ct_tz()
    if tz is not None:
        now_ct = datetime.now(tz)
        return now_ct.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    offset = _fallback_ct_offset_hours(utc_now)
    ct_now = utc_now + timedelta(hours=offset)
    ct_midnight = datetime(ct_now.year, ct_now.month, ct_now.day)
    return (ct_midnight - timedelta(hours=offset)).replace(tzinfo=timezone.utc).timestamp()


def format_ct(ts_sec):
    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    tz = ct_tz()
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def load_state():
    default_state = {
        "cooldown_blocklist": {},
        "last_traded_candle": {},
        "hibernating_until": 0,
        "closed_trades_ledger": [],
        "active_position_cache": {},
        "previous_active_coins": [],
        "last_run_timestamp": "",
        "last_email_timestamp": 0,
        "last_scan_timestamp": 0,
        "daily_starting_equity": {},
        "seen_fill_ids": {},
        "open_fee_accum": {},
        "entry_fill_times": {},
        "pending_exit_reasons": {},
        "ledger_version": 0
    }
    data = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Error loading {STATE_FILE}: {e}", flush=True)
    if data is None:
        data = {}
    for k, v in default_state.items():
        if k not in data:
            data[k] = v

    # Ledgers written by V4.8.x double-counted bot exits and ignored fees. Rebuild from exchange fills.
    if data.get("ledger_version") != LEDGER_VERSION:
        data["closed_trades_ledger"] = []
        data["seen_fill_ids"] = {}
        data["open_fee_accum"] = {}
        data["ledger_version"] = LEDGER_VERSION
    for legacy_key in ("seen_fill_hashes", "gemini_cache", "stagnation_tracker"):
        data.pop(legacy_key, None)
    return data


def save_state(state):
    state["last_run_timestamp"] = get_central_time().strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def api_retry(func, *args, retries=4, delay=2.0, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                print(f"[WARN] API Call exception ({e}). Retrying in {delay:.1f}s...", flush=True)
                time.sleep(delay)
                delay *= 1.5
            else:
                raise e


class DryRunExchange:
    """Stands in for Exchange when DRY_RUN is set: logs every write action instead of sending it."""

    def __init__(self, audit_logs):
        self.audit_logs = audit_logs

    def _log(self, msg):
        self.audit_logs.append(f"🧪 [DRY RUN] would {msg}")
        return {"status": "ok", "response": {"type": "dry_run", "data": {"statuses": [{"resting": {"oid": 0}}]}}}

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False, **kw):
        return self._log(f"order {coin} {'BUY' if is_buy else 'SELL'} {sz} @ {px} {order_type} reduce_only={reduce_only}")

    def bulk_orders(self, reqs, grouping="na", **kw):
        return self._log(f"bulk_orders[{grouping}] " + " | ".join(f"{r['coin']} {'BUY' if r['is_buy'] else 'SELL'} {r['sz']} @ {r['limit_px']} {r['order_type']}" for r in reqs))

    def cancel(self, coin, oid):
        return self._log(f"cancel {coin} #{oid}")

    def market_close(self, coin, **kw):
        return self._log(f"market_close {coin}")

    def update_leverage(self, lev, coin, is_cross=True):
        return self._log(f"set leverage {coin} {lev}x")


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
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Telemetry email successfully sent to {receiver_email}", flush=True)
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)


def round_sig_figs(val, sig_figs=5):
    if val == 0 or val is None:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)


def round_px(px, sz_decimals):
    """Hyperliquid perp prices: max 5 significant figures AND max (6 - szDecimals) decimals.
    5 sig figs alone is rejected on cheap coins (e.g. PENGU 0.0081940 has 7 decimals, limit is 6)."""
    if not px or px <= 0:
        return 0.0
    return round(float(f"{px:.5g}"), max(0, 6 - int(sz_decimals)))


def order_result(res):
    """Returns (ok, list_of_statuses, error_text). The API can return status 'ok' with per-order errors inside."""
    if not isinstance(res, dict) or res.get("status") != "ok":
        return False, [], f"{res}"
    statuses = (res.get("response", {}) or {}).get("data", {}).get("statuses", []) or []
    errors = [s.get("error") for s in statuses if isinstance(s, dict) and s.get("error")]
    if errors:
        return False, statuses, "; ".join(errors)
    return True, statuses, ""


def trigger_kind(o):
    """'sl', 'tp' or None. frontendOpenOrders returns orderType as a string ('Stop Market', 'Take Profit Market', 'Limit', ...)."""
    o_type = o.get("orderType", "")
    if isinstance(o_type, dict):
        tpsl = o_type.get("trigger", {}).get("tpsl")
        return tpsl if tpsl in ("sl", "tp") else None
    if not o.get("isTrigger", False):
        return None
    t = str(o_type).lower()
    if "take profit" in t:
        return "tp"
    if "stop" in t:
        return "sl"
    return None


def check_liquidity_and_spread(info, coin, max_spread=0.0010, min_depth_usd=1200.0):
    try:
        l2_book = api_retry(info.l2_snapshot, name=coin)
        levels = l2_book.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []
        asks = levels[1] if len(levels) > 1 else []

        if not bids or not asks:
            return False, "Empty Orderbook"

        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])

        if best_bid <= 0 or best_ask <= 0:
            return False, "Invalid Orderbook Prices"

        mid_px = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid_px

        if spread_pct > max_spread:
            return False, f"Wide Spread ({spread_pct*100:.2f}% > {max_spread*100:.2f}%)"

        bid_depth = sum(float(b["px"]) * float(b["sz"]) for b in bids if float(b["px"]) >= mid_px * 0.9985)
        ask_depth = sum(float(a["px"]) * float(a["sz"]) for a in asks if float(a["px"]) <= mid_px * 1.0015)
        total_depth = min(bid_depth, ask_depth)

        if total_depth < min_depth_usd:
            return False, f"Low Orderbook Depth (${total_depth:,.0f} < ${min_depth_usd:,.0f})"

        return True, f"OK (Spread: {spread_pct*100:.2f}%, Depth: ${total_depth:,.0f})"
    except Exception as e:
        return False, f"Liquidity Check Error: {e}"


def purge_unfilled_entry_orders(exchange, open_orders, audit_logs):
    """Cancels resting non-trigger, non-reduce-only orders (unfilled entries from the previous run).
    Note: this also cancels any manual limit entries you place on the account."""
    for o in open_orders:
        if o.get("isTrigger", False) or o.get("reduceOnly", False):
            continue
        coin = o.get("coin")
        oid = int(o["oid"])
        try:
            exchange.cancel(coin, oid)
            audit_logs.append(f"🧹 PURGED UNFILLED LIMIT ENTRY [{coin}] Order #{oid}")
        except Exception as e:
            audit_logs.append(f"⚠️ Could not purge [{coin}] #{oid}: {e}")


def cancel_orphan_trigger_orders(exchange, open_orders, position_coins, audit_logs):
    """Stops/TPs left on coins with no open position (e.g. children of a purged entry)."""
    for o in open_orders:
        coin = o.get("coin")
        if coin in position_coins or not o.get("isTrigger", False):
            continue
        try:
            exchange.cancel(coin, int(o["oid"]))
            audit_logs.append(f"🧹 CANCELLED ORPHAN TRIGGER [{coin}] #{o['oid']} ({o.get('orderType')})")
        except Exception:
            pass


def cancel_native_trigger_orders(exchange, info, coin, account_address):
    try:
        open_orders = api_retry(info.frontend_open_orders, account_address)
        for o in open_orders:
            if o.get("coin") == coin:
                try:
                    exchange.cancel(coin, int(o["oid"]))
                except Exception:
                    pass
    except Exception:
        pass


def sync_native_trigger_orders(exchange, open_orders, coin, sz, stop_px, sz_decimals, audit_logs):
    """Keeps exactly one reduce-only stop on a LONG position at stop_px. Places the new stop BEFORE cancelling
    old ones so the position is never unprotected while the trail moves up."""
    try:
        clean_stop_px = round_px(stop_px, sz_decimals)
        if clean_stop_px <= 0:
            return

        coin_orders = [o for o in open_orders if o.get("coin") == coin]
        sl_orders = [o for o in coin_orders if trigger_kind(o) == "sl"]
        tp_orders = [o for o in coin_orders if trigger_kind(o) == "tp"]

        keep_oid = None
        for o in sl_orders:
            existing_px = float(o.get("triggerPx", 0.0) or 0.0)
            existing_sz = float(o.get("sz", 0.0) or 0.0)
            px_match = abs(existing_px - clean_stop_px) / max(clean_stop_px, 1e-12) < 0.0005
            sz_match = abs(existing_sz - sz) <= max(sz * 0.001, 1e-9)
            if keep_oid is None and px_match and sz_match:
                keep_oid = o["oid"]

        if keep_oid is None:
            order_type = {"trigger": {"isMarket": True, "triggerPx": clean_stop_px, "tpsl": "sl"}}
            res = exchange.order(coin, False, sz, clean_stop_px, order_type, reduce_only=True)
            ok, _, err = order_result(res)
            if not ok:
                audit_logs.append(f"🚨 STOP PLACEMENT FAILED [{coin}] @ ${clean_stop_px}: {err} — keeping old stops")
                return
            audit_logs.append(f"🛡️ NATIVE SL SYNC [{coin}]: Hard Stop @ ${clean_stop_px}")

        # Remove duplicates/stale stops and any TP (this strategy runs uncapped).
        for o in sl_orders + tp_orders:
            if o["oid"] == keep_oid:
                continue
            try:
                exchange.cancel(coin, int(o["oid"]))
            except Exception:
                pass
    except Exception as e:
        audit_logs.append(f"⚠️ TPSL sync warning on {coin}: {e}")


def sync_closed_trades_from_exchange(info, account_address, state, audit_logs, now_ts):
    """Single source of truth for the trade ledger. Groups close fills by order, subtracts the close fee and the
    matching share of the open fee, and stamps cooldowns from the actual fill time."""
    try:
        fills = api_retry(info.user_fills, account_address)
    except Exception as e:
        audit_logs.append(f"⚠️ Fill sync notice: {e}")
        return
    if not fills:
        return

    seen = state.setdefault("seen_fill_ids", {})
    open_fees = state.setdefault("open_fee_accum", {})
    entry_times = state.setdefault("entry_fill_times", {})
    pending_reasons = state.setdefault("pending_exit_reasons", {})
    cutoff_ms = (now_ts - 2 * 86400) * 1000

    close_groups = {}
    for f in sorted(fills, key=lambda x: int(x.get("time", 0))):
        fill_time_ms = int(f.get("time", 0))
        if fill_time_ms < cutoff_ms:
            continue
        # tid is unique per fill; hash is per transaction and is shared by partial fills of one order.
        fid = str(f.get("tid") or f"{f.get('hash')}_{f.get('oid')}_{f.get('px')}_{f.get('sz')}_{fill_time_ms}")
        if fid in seen:
            continue
        seen[fid] = fill_time_ms

        coin = f.get("coin")
        f_dir = str(f.get("dir", "")).lower()
        fee = float(f.get("fee", 0.0) or 0.0)
        sz = float(f.get("sz", 0.0) or 0.0)
        px = float(f.get("px", 0.0) or 0.0)
        start_pos = float(f.get("startPosition", 0.0) or 0.0)
        closed_pnl = float(f.get("closedPnl", 0.0) or 0.0)

        if f_dir.startswith("open"):
            if abs(start_pos) < 1e-12:
                open_fees[coin] = 0.0
                entry_times[coin] = fill_time_ms / 1000.0
            open_fees[coin] = float(open_fees.get(coin, 0.0)) + fee
            continue

        if "close" not in f_dir and closed_pnl == 0.0:
            continue

        key = (coin, f.get("oid"))
        g = close_groups.setdefault(key, {
            "coin": coin, "sz": 0.0, "notional": 0.0, "closed_pnl": 0.0, "fee": 0.0,
            "start_pos": start_pos, "time_ms": fill_time_ms, "is_long": "long" in f_dir or start_pos > 0
        })
        g["sz"] += sz
        g["notional"] += sz * px
        g["closed_pnl"] += closed_pnl
        g["fee"] += fee
        g["time_ms"] = max(g["time_ms"], fill_time_ms)

    ledger = state.get("closed_trades_ledger", [])
    for g in sorted(close_groups.values(), key=lambda x: x["time_ms"]):
        coin = g["coin"]
        sz = g["sz"]
        if sz <= 0:
            continue
        exit_px = g["notional"] / sz
        start_abs = abs(g["start_pos"]) if abs(g["start_pos"]) > 0 else sz
        close_frac = min(1.0, sz / start_abs)
        open_fee_share = float(open_fees.get(coin, 0.0)) * close_frac
        open_fees[coin] = float(open_fees.get(coin, 0.0)) - open_fee_share
        fully_closed = close_frac >= 0.999

        net_pnl = g["closed_pnl"] - g["fee"] - open_fee_share
        per_unit = g["closed_pnl"] / sz
        entry_px = (exit_px - per_unit) if g["is_long"] else (exit_px + per_unit)
        margin = sz * entry_px / LEVERAGE if entry_px > 0 else 0.0
        roe_pct = (net_pnl / margin * 100.0) if margin > 0 else 0.0
        fill_ts = g["time_ms"] / 1000.0

        reason = pending_reasons.pop(coin, None) if fully_closed else None
        if not reason:
            reason = "🎯 Exchange fill (trail / profit)" if net_pnl > 0 else "🛡️ Exchange fill (stop)"

        cooldown_until = fill_ts + (WIN_COOLDOWN_SEC if net_pnl > 0 else LOSS_COOLDOWN_SEC)
        if cooldown_until > now_ts:
            state["cooldown_blocklist"][coin] = max(float(state["cooldown_blocklist"].get(coin, 0)), cooldown_until)

        if fully_closed:
            open_fees.pop(coin, None)
            entry_times.pop(coin, None)

        card = {
            "coin": coin,
            "entry_price": round_sig_figs(entry_px, 5),
            "exit_price": round_sig_figs(exit_px, 5),
            "gross_pnl_usd": round(g["closed_pnl"], 4),
            "fees_usd": round(g["fee"] + open_fee_share, 4),
            "pnl_usd": round(net_pnl, 4),
            "roe_pct": round(roe_pct, 2),
            "side": "LONG" if g["is_long"] else "SHORT",
            "exit_reason": reason,
            "timestamp": format_ct(fill_ts),
            "ts_sec": fill_ts
        }
        ledger.insert(0, card)
        if now_ts - fill_ts <= 900:
            audit_logs.append(f"✅ FILL SYNCED [{coin}]: {card['side']} exit @ ${card['exit_price']} | Net ${net_pnl:+.3f} (gross ${g['closed_pnl']:+.3f}, fees ${card['fees_usd']:.3f}) | {roe_pct:+.2f}% ROE")

    state["closed_trades_ledger"] = sorted(ledger, key=lambda x: float(x.get("ts_sec", 0)), reverse=True)[:50]
    state["seen_fill_ids"] = {k: v for k, v in seen.items() if v >= cutoff_ms}


def calculate_15m_ross_indicators(candles):
    closes = np.array([float(c["c"]) for c in candles])
    highs = np.array([float(c["h"]) for c in candles])
    lows = np.array([float(c["l"]) for c in candles])
    vols = np.array([float(c.get("v", 0.0)) for c in candles])

    if len(closes) < 30:
        return None

    df_c = pd.Series(closes)
    ema9_series = df_c.ewm(span=9, adjust=False).mean()

    typical_prices = (highs + lows + closes) / 3.0
    tp_v = typical_prices * vols
    cum_tp_v = np.sum(tp_v[-32:])
    cum_v = np.sum(vols[-32:])
    vwap = (cum_tp_v / cum_v) if cum_v > 0 else closes[-1]

    ema12 = df_c.ewm(span=12, adjust=False).mean()
    ema26 = df_c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = (macd_line - signal_line).iloc[-1]

    return {
        "ema9": ema9_series.iloc[-1],
        "vwap": vwap,
        "macd_hist": macd_hist,
        "macd_line": macd_line.iloc[-1],
        "signal_line": signal_line.iloc[-1],
        "prev_close": closes[-2],
        "prev_ema9": ema9_series.iloc[-2]
    }


def rolling_15m_volume(info, coin, now_ms):
    """Volume of the last 15 minutes from 1m candles, scaled to exactly 15 minutes.
    Unlike the live 15m candle, this doesn't depend on how far into the candle the run happens to start
    (cron runs land 1-5 min after the candle opens, where the old 3x pacing cap hid ~2/3 of the volume)."""
    m1 = api_retry(info.candles_snapshot, name=coin, interval="1m", startTime=now_ms - 16 * 60000, endTime=now_ms)
    if not m1:
        return None
    window_start = now_ms - 15 * 60000
    vol, first_t = 0.0, None
    for c in m1:
        t = int(c["t"])
        if t < window_start:
            continue
        first_t = t if first_t is None else min(first_t, t)
        vol += float(c.get("v", 0) or 0)
    if first_t is None:
        return None
    covered_mins = max(1.0, (now_ms - first_t) / 60000.0)
    return vol * 15.0 / covered_mins if covered_mins < 15.0 else vol


def setup_misses(checks):
    """checks: [(passed, rule_name, detail)] for one setup. Returns (rule_name, detail) for each failed rule."""
    return [(name, detail) for passed, name, detail in checks if not passed]


def record_blockers(state, today_str, labels, coin, stats_key="near_miss_stats"):
    """Daily tally of which rule blocked a coin, counting each coin ONCE per rule per day (kept 7 days).
    Counting every 5-minute run made 4 coins look like 164 blocked opportunities."""
    stats = state.setdefault(stats_key, {})
    day = stats.setdefault(today_str, {})
    for key in labels:
        coins = day.get(key)
        if not isinstance(coins, list):      # old format stored a run counter
            coins = []
        if coin not in coins:
            coins.append(coin)
        day[key] = coins
    for d in sorted(stats)[:-7]:
        stats.pop(d, None)


def blockers_summary(state, today_str, stats_key="near_miss_stats"):
    day = state.get(stats_key, {}).get(today_str, {})
    rows = [(k, v) for k, v in day.items() if isinstance(v, list) and v]
    rows.sort(key=lambda kv: -len(kv[1]))
    return " &bull; ".join(f"{k}: <b>{len(v)}</b> ({', '.join(v[:4])}{'…' if len(v) > 4 else ''})" for k, v in rows) or "None yet today"


def calculate_smart_exchange_targets(entry_px, is_long, current_px, entry_candle_low, peak_roe=0.0, upper_wick_pct=0.0):
    """V4.8.12 profit-lock ladder. Fixed -3.5% ROE stop until the trade works; then lock 80-93% of the peak.
    entry_candle_low is kept for logging only: the tight candle-low stop (V4.8.14+) caused noise stop-outs."""
    roe = ((current_px - entry_px) / entry_px * LEVERAGE) if is_long else ((entry_px - current_px) / entry_px * LEVERAGE)

    target_floor_roe = HARD_CAP_ROE
    leash_status = f"🛡 HARD STOP ({HARD_CAP_ROE*100:.2f}% ROE) until +{FIRST_LOCK_TRIGGER_ROE*100:.1f}% peak"
    for min_peak, keep_share, max_giveback, label in LOCK_TIERS:
        if peak_roe >= min_peak:
            if max_giveback is None:
                target_floor_roe = max(peak_roe * keep_share, EARLY_LOCK_MIN_ROE)
            else:
                target_floor_roe = max(peak_roe * keep_share, peak_roe - max_giveback)
            leash_status = f"{label} [Peak +{peak_roe*100:.2f}% → Floor +{target_floor_roe*100:.2f}%]"
            break
    else:
        if peak_roe >= FIRST_LOCK_TRIGGER_ROE:
            target_floor_roe = FIRST_LOCK_ROE
            leash_status = f"🛡 FEE LOCK (+{FIRST_LOCK_ROE*100:.2f}% ROE)"

    drawdown_from_peak = peak_roe - roe
    if peak_roe >= CLIMAX_TRIGGER_ROE and (drawdown_from_peak >= CLIMAX_DRAWDOWN_ROE or upper_wick_pct >= 0.45):
        target_floor_roe = max(target_floor_roe, peak_roe - CLIMAX_GIVEBACK_ROE)
        leash_status = f"⚡ CLIMAX EXHAUSTION LOCK [Peak +{peak_roe*100:.2f}% → Floor +{target_floor_roe*100:.2f}%]"

    stop_px = entry_px * (1.0 + target_floor_roe / LEVERAGE) if is_long else entry_px * (1.0 - target_floor_roe / LEVERAGE)
    return stop_px, roe, leash_status


def shadow_stop_label(peak_roe):
    return "🎯 Profit lock" if peak_roe >= FIRST_LOCK_TRIGGER_ROE else "🛡️ Hard stop"


def close_shadow_short(state, coin, pos, exit_px, reason, now_ts, audit_logs):
    """Records a paper short result, net of estimated taker fees on both sides."""
    entry = pos["entry_px"]
    move = (entry - exit_px) / entry
    pnl = pos["notional"] * move - pos["notional"] * SHADOW_TAKER_FEE * 2
    roe_pct = pnl / (pos["notional"] / LEVERAGE) * 100.0 if pos["notional"] > 0 else 0.0
    shadow = state.setdefault("shadow_shorts", {"open": {}, "closed": []})
    shadow["closed"].insert(0, {
        "coin": coin, "setup": pos.get("setup", "?"), "regime": pos.get("regime", "RISK_OFF"), "entry_px": entry, "exit_px": exit_px,
        "pnl_usd": round(pnl, 4), "roe_pct": round(roe_pct, 2), "reason": reason,
        "opened": format_ct(pos["opened_ts"]), "timestamp": format_ct(now_ts), "ts_sec": now_ts
    })
    shadow["closed"] = [t for t in shadow["closed"] if t["ts_sec"] >= now_ts - SHADOW_HISTORY_DAYS * 86400][:100]
    shadow["open"].pop(coin, None)
    cd = WIN_COOLDOWN_SEC if pnl > 0 else LOSS_COOLDOWN_SEC
    shadow.setdefault("cooldown", {})[coin] = now_ts + cd
    audit_logs.append(f"👻 SHADOW SHORT CLOSED [{coin}] @ ${exit_px:.6g}: {reason} | est. net ${pnl:+.3f} ({roe_pct:+.2f}% ROE)")


def manage_shadow_shorts(info, state, all_mids, now_ms, now_ts, audit_logs):
    """Follows open paper shorts. A stop that 'rests on the exchange' between runs is simulated by checking the
    1m highs since the last run against the stop that was in force at that time."""
    shadow = state.setdefault("shadow_shorts", {"open": {}, "closed": []})
    for coin, pos in list(shadow["open"].items()):
        try:
            cur = float(all_mids.get(coin, 0) or 0)
            if cur <= 0:
                continue
            entry = pos["entry_px"]
            m1 = api_retry(info.candles_snapshot, name=coin, interval="1m",
                           startTime=max(int(pos["opened_ts"] * 1000), now_ms - 6 * 3600000), endTime=now_ms) or []
            since_last = [c for c in m1 if int(c["t"]) >= int(pos["last_check_ms"]) - 60000]
            high_since = max([float(c["h"]) for c in since_last] + [cur])
            if high_since >= pos["stop_px"]:
                close_shadow_short(state, coin, pos, pos["stop_px"],
                                   f"{shadow_stop_label(pos['peak_roe'])} hit (simulated resting stop)", now_ts, audit_logs)
                continue

            low_since_entry = min([float(c["l"]) for c in m1 if int(c["t"]) >= int(pos["opened_ts"] * 1000)] + [cur])
            peak_roe = max(pos["peak_roe"], (entry - low_since_entry) / entry * LEVERAGE)
            roe = (entry - cur) / entry * LEVERAGE
            held_mins = (now_ts - pos["opened_ts"]) / 60.0

            lower_wick = 0.0
            c15 = api_retry(info.candles_snapshot, name=coin, interval="15m", startTime=now_ms - 1800000, endTime=now_ms)
            if c15:
                h, l, o = float(c15[-1]["h"]), float(c15[-1]["l"]), float(c15[-1]["o"])
                lower_wick = (min(o, cur) - l) / max(1e-12, h - l)
            stop_px, _, status = calculate_smart_exchange_targets(entry, False, cur, 0, peak_roe=peak_roe, upper_wick_pct=lower_wick)

            reason = ""
            if held_mins >= SCRATCH_MINS and roe < SCRATCH_ROE:
                reason = f"⏱️ {SCRATCH_MINS:.0f}M negative scratch ({roe*100:.2f}% ROE)"
            elif held_mins >= FLAT_EXIT_MINS and roe < FLAT_EXIT_ROE:
                reason = f"⏱️ {FLAT_EXIT_MINS:.0f}M flat exit ({roe*100:.2f}% ROE)"
            elif cur >= stop_px:
                reason = f"{shadow_stop_label(peak_roe)} hit ({roe*100:.2f}% ROE, peak {peak_roe*100:.2f}%)"
            if reason:
                close_shadow_short(state, coin, pos, cur, reason, now_ts, audit_logs)
                continue

            pos.update({"stop_px": stop_px, "peak_roe": peak_roe, "last_check_ms": now_ms,
                        "current_px": cur, "roe": roe, "status": status})
        except Exception as e:
            audit_logs.append(f"⚠️ Shadow short tracking error [{coin}]: {e}")


def scan_shadow_shorts(info, state, meta, asset_ctxs, all_mids, now_ms, now_ts, account_value, audit_logs, regime, today_str):
    """Mirror of the long scan on the top LOSERS. Opens at most one paper short per run. Never touches the exchange."""
    shadow = state.setdefault("shadow_shorts", {"open": {}, "closed": []})
    if len(shadow["open"]) >= SHADOW_MAX_OPEN:
        return
    cooldowns = shadow.setdefault("cooldown", {})
    for c, until in list(cooldowns.items()):
        if until <= now_ts:
            cooldowns.pop(c, None)

    losers = []
    for idx, asset in enumerate(meta.get("universe", [])):
        coin = asset.get("name")
        if not coin or idx < SKIP_FIRST_LISTED or asset.get("isDelisted") or coin in SKIP_COINS or coin in shadow["open"]:
            continue
        try:
            ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
            prev_px, mark_px = float(ctx.get("prevDayPx", 0) or 0), float(ctx.get("markPx", 0) or 0)
            if float(ctx.get("dayNtlVlm", 0) or 0) < MIN_24H_VOLUME_USD or prev_px <= 0 or mark_px <= 0:
                continue
            chg = (mark_px - prev_px) / prev_px * 100
            if chg <= -SHADOW_MIN_24H_DROP_PCT:
                losers.append((chg, coin))
        except Exception:
            continue
    losers.sort()
    audit_logs.append(f"👻 SHADOW SHORT SCAN (paper only, {regime}): {len(losers)} losers down {SHADOW_MIN_24H_DROP_PCT:.0f}%+ — "
                      + (", ".join(f"{c} ({p:+.1f}%)" for p, c in losers[:5]) or "none"))

    candidates, near = [], []
    for chg, coin in losers[:SHADOW_SCAN_TOP_N]:
        if now_ts < float(cooldowns.get(coin, 0)):
            continue
        try:
            px = float(all_mids.get(coin, 0) or 0)
            if px <= 0:
                continue
            candles = api_retry(info.candles_snapshot, name=coin, interval="15m", startTime=now_ms - CANDLE_LOOKBACK_MS, endTime=now_ms)
            if not candles or len(candles) < 32:
                continue
            done = candles[-(ATR_CANDLES + 1):-1]
            atr_pct = float(np.mean([(float(c["h"]) - float(c["l"])) / float(c["c"]) for c in done])) * 100.0
            if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
                record_blockers(state, today_str, ["Too quiet (ATR)" if atr_pct < MIN_ATR_PCT else "Too wild (ATR)"], coin, "shadow_near_miss_stats")
                continue
            ind = calculate_15m_ross_indicators(candles)
            if not ind:
                continue
            ema9, vwap, macd_hist = ind["ema9"], ind["vwap"], ind["macd_hist"]
            stretch_below = (ema9 - px) / ema9
            if stretch_below > 0.0100:
                record_blockers(state, today_str, ["Too extended below EMA9"], coin, "shadow_near_miss_stats")
                continue   # already dumped: shorting here is chasing (mirror of the long chase guard)
            highs = [float(c["h"]) for c in candles]
            lows = [float(c["l"]) for c in candles]
            day_low = min(lows)
            below_ema9, below_vwap, macd_neg = px < ema9, px < vwap, macd_hist < 0
            vwap_ok = (vwap / px) <= SHADOW_VWAP_CAP
            near_lod = px <= day_low * (1 + SHADOW_LOD_PROXIMITY)
            rejection_cross = (ind["prev_close"] >= ind["prev_ema9"] or ind["prev_close"] >= vwap) and below_ema9 and below_vwap
            near_ema = px >= ema9 * 0.9975
            fresh_high = highs[-1] >= max(highs[-4:-1])
            c_range = max(1e-12, highs[-1] - lows[-1])
            upper_wick = (highs[-1] - max(float(candles[-1]["o"]), px)) / c_range

            pre = vwap_ok and below_ema9 and macd_neg and ((near_lod and below_vwap) or (rejection_cross and near_ema) or (fresh_high and upper_wick >= 0.45))
            rvol, rvol_measured = 0.0, False
            if pre:
                avg_vol = np.mean([float(c.get("v", 0)) for c in candles][-12:-2])
                rolling = rolling_15m_volume(info, coin, now_ms)
                if rolling is not None and avg_vol > 0:
                    rvol, rvol_measured = rolling / avg_vol, True

            if not pre or rvol < SHADOW_RVOL:
                # Near-miss diagnostics (log only). Volume is only measured when everything else passed.
                rvol_c = (rvol >= SHADOW_RVOL or not rvol_measured, "RVOL", f"RVOL {rvol:.2f}x (need {SHADOW_RVOL}x)")
                vwap_c = (vwap_ok, "VWAP stretch", f"{(vwap/px-1)*100:.2f}% below VWAP (max {(SHADOW_VWAP_CAP-1)*100:.2f}%)")
                ema_c = (below_ema9, "Above EMA9", f"above EMA9 ({(px/ema9-1)*100:+.2f}%)")
                macd_c = (macd_neg, "MACD positive", "MACD histogram positive")
                setups = {
                    "LOD_BREAKDOWN": setup_misses([
                        (near_lod, "Not near day low", f"not near day low ({(px/day_low-1)*100:+.2f}% vs LOD)"),
                        rvol_c, vwap_c, ema_c, (below_vwap, "Above VWAP", "above VWAP"), macd_c]),
                    "BEARISH_REJECTION": setup_misses([
                        (rejection_cross, "No rejection cross", "no EMA9/VWAP rejection cross"),
                        (near_ema, "Not at EMA9", f"too far below EMA9 for a rejection ({-stretch_below*100:+.2f}%)"),
                        rvol_c, vwap_c, ema_c, macd_c]),
                    "A_TOP_REJECTION": setup_misses([
                        (fresh_high, "No fresh high", "no fresh 45m high"),
                        (upper_wick >= 0.45, "Wick too small", f"upper wick {upper_wick*100:.0f}% (need 45%)"),
                        rvol_c, vwap_c, ema_c, macd_c]),
                }
                best_setup, best_fails = min(setups.items(), key=lambda kv: len(kv[1]))
                if len(best_fails) <= NEAR_MISS_MAX_FAILS:
                    near.append((len(best_fails), -rvol, coin, best_setup, [d for _, d in best_fails]))
                    if len(best_fails) == 1:
                        record_blockers(state, today_str, [best_fails[0][0]], coin, "shadow_near_miss_stats")
                continue

            if fresh_high and upper_wick >= 0.45:
                setup, score = "A_TOP_REJECTION", rvol * 3.2
            elif rejection_cross and near_ema:
                setup, score = "BEARISH_REJECTION", rvol * 2.4
            else:
                setup, score = "LOD_BREAKDOWN", rvol * 1.0
            candidates.append((score, coin, px, setup, rvol, atr_pct))
        except Exception as e:
            audit_logs.append(f"⚠️ Shadow scan error [{coin}]: {e}")

    for n_fail, _, coin, setup_name, fails in sorted(near)[:NEAR_MISS_SHOW]:
        tag = "everything else passed" if n_fail == 1 else f"{n_fail} rules short"
        audit_logs.append(f"👻 SHORT NEAR MISS [{coin}] {setup_name}: {'; '.join(fails)} — {tag}")
    if not candidates:
        audit_logs.append("👻 Shadow scan: no short setup on this candle")
        return
    score, coin, px, setup, rvol, atr_pct = max(candidates)
    stop_px, _, status = calculate_smart_exchange_targets(px, False, px, 0)
    shadow["open"][coin] = {
        "entry_px": px, "setup": setup, "opened_ts": now_ts, "last_check_ms": now_ms,
        "stop_px": stop_px, "peak_roe": 0.0, "notional": max(MIN_NOTIONAL_USD, account_value * POSITION_FRACTION),
        "current_px": px, "roe": 0.0, "status": status, "regime": regime
    }
    audit_logs.append(f"👻 SHADOW SHORT OPENED [{coin}] {setup} ({regime}) @ ${px:.6g} (RVOL {rvol:.2f}x, ATR {atr_pct:.2f}%, paper stop ${stop_px:.6g}) — NO ORDER SENT")


def check_btc_regime(info, now_ms):
    """Replaces the Gemini 'risk' prompt (the LLM had no market data, so its answer was effectively random).
    RISK_OFF: BTC below its 1h EMA50 and EMA20 < EMA50 (downtrend).
    CAUTION: BTC below 1h EMA50, or down >0.5% over the last 4h. Uses the strict entry gates.
    RISK_ON: everything else."""
    try:
        candles = api_retry(info.candles_snapshot, name="BTC", interval="1h", startTime=now_ms - 80 * 3600000, endTime=now_ms)
        closes = pd.Series([float(c["c"]) for c in candles])
        if len(closes) < 55:
            return "CAUTION", "Not enough BTC 1h history — using strict gates"
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
        last = closes.iloc[-1]
        chg_4h = (last / closes.iloc[-5] - 1.0) * 100.0
        desc = f"BTC ${last:,.0f} | 1h EMA20 ${ema20:,.0f} / EMA50 ${ema50:,.0f} | 4h {chg_4h:+.2f}%"
        if last < ema50 and ema20 < ema50:
            return "RISK_OFF", desc
        if last < ema50 or chg_4h < -0.5:
            return "CAUTION", desc
        return "RISK_ON", desc
    except Exception as e:
        return "CAUTION", f"BTC regime check failed ({e}) — using strict gates"


def place_entry_with_stop(exchange, coin, sz, entry_px, is_market, stop_px, sz_decimals, audit_logs):
    """Entry and its stop go out in ONE request (grouping='normalTpsl'): the stop activates the moment the entry
    fills, so there is no unprotected gap until the next run. Falls back to entry + immediate separate stop."""
    stop_px = round_px(stop_px, sz_decimals)
    if is_market:
        entry_limit = round_px(entry_px * 1.003, sz_decimals)
        entry_type = {"limit": {"tif": "Ioc"}}
    else:
        entry_limit = round_px(entry_px, sz_decimals)
        entry_type = {"limit": {"tif": "Gtc"}}

    reqs = [
        {"coin": coin, "is_buy": True, "sz": sz, "limit_px": entry_limit, "order_type": entry_type, "reduce_only": False},
        {"coin": coin, "is_buy": False, "sz": sz, "limit_px": stop_px,
         "order_type": {"trigger": {"isMarket": True, "triggerPx": stop_px, "tpsl": "sl"}}, "reduce_only": True},
    ]
    try:
        res = exchange.bulk_orders(reqs, grouping="normalTpsl")
        ok, statuses, err = order_result(res)
        if ok:
            first = statuses[0] if statuses else {}
            if "filled" in first:
                f = first["filled"]
                audit_logs.append(f"⚡ ENTRY FILLED [{coin}] {f.get('totalSz')} @ ${f.get('avgPx')} + stop @ ${stop_px} attached")
            else:
                audit_logs.append(f"⏳ ENTRY {'SENT' if is_market else 'RESTING'} [{coin}] {sz} @ ${entry_limit} + stop @ ${stop_px} attached")
            return True
        if is_market and statuses and isinstance(statuses[0], dict) and statuses[0].get("error"):
            # IOC entry did not fill (e.g. price ran past the 0.3% slippage). Nothing to protect.
            audit_logs.append(f"ℹ️ MARKET ENTRY NOT FILLED [{coin}]: {err}")
            return False
        audit_logs.append(f"⚠️ Grouped entry rejected [{coin}] ({err}) — falling back to separate orders")
    except Exception as e:
        audit_logs.append(f"⚠️ Grouped entry error [{coin}] ({e}) — falling back to separate orders")

    res = exchange.order(coin, True, sz, entry_limit, entry_type, reduce_only=False)
    ok, statuses, err = order_result(res)
    if not ok:
        audit_logs.append(f"ORDER PLACEMENT FAILED on {coin}: {err}")
        return False
    first = statuses[0] if statuses else {}
    if "filled" in first:
        filled_sz = float(first["filled"].get("totalSz", sz))
        stop_res = exchange.order(coin, False, filled_sz, stop_px,
                                  {"trigger": {"isMarket": True, "triggerPx": stop_px, "tpsl": "sl"}}, reduce_only=True)
        s_ok, _, s_err = order_result(stop_res)
        audit_logs.append(f"⚡ ENTRY FILLED [{coin}] {filled_sz} — stop @ ${stop_px} {'placed' if s_ok else 'FAILED: ' + s_err}")
    else:
        audit_logs.append(f"⏳ RESTING LIMIT PLACED [{coin}] {sz} @ ${entry_limit} (stop will be placed next run if filled)")
    return True


def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    now_ms = int(now_ts * 1000)
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    ct_midnight_ts = get_central_midnight_timestamp()

    audit_logs = []
    audit_logs.append(f"[{timestamp}] {BOT_TITLE} Engine Started{' (DRY RUN — no orders sent)' if DRY_RUN else ''}.")

    if not ACCOUNT_ADDRESS or (not SECRET_KEY and not DRY_RUN):
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()

    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)
    if DRY_RUN:
        exchange = DryRunExchange(audit_logs)
    else:
        wallet = eth_account.Account.from_key(SECRET_KEY)
        exchange = api_retry(Exchange, wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)

    # 1) Clean up last run's unfilled entries, then record any fills (native stops, trails) since last run.
    open_orders = api_retry(info.frontend_open_orders, ACCOUNT_ADDRESS)
    purge_unfilled_entry_orders(exchange, open_orders, audit_logs)
    sync_closed_trades_from_exchange(info, ACCOUNT_ADDRESS, state, audit_logs, now_ts)

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    meta_and_ctxs = api_retry(info.meta_and_asset_ctxs)
    meta = meta_and_ctxs[0]
    asset_ctxs = meta_and_ctxs[1]
    all_mids = api_retry(info.all_mids)

    sz_decimals_map = {a.get("name"): a.get("szDecimals", 4) for a in meta.get("universe", [])}

    asset_positions = user_state.get("assetPositions", [])
    position_coins = {p.get("position", {}).get("coin") for p in asset_positions if float(p.get("position", {}).get("szi", 0) or 0) != 0}
    open_orders = api_retry(info.frontend_open_orders, ACCOUNT_ADDRESS)
    cancel_orphan_trigger_orders(exchange, open_orders, position_coins, audit_logs)

    # 2) Account value / daily equity.
    spot_usdc = 0.0
    total_spot_net_worth = 0.0
    for b in spot_state.get("balances", []):
        coin = b.get("coin", "").upper()
        total_amt = float(b.get("total", 0.0))
        if total_amt > 0:
            if coin == "USDC":
                spot_usdc = total_amt
                total_spot_net_worth += total_amt
            else:
                total_spot_net_worth += total_amt * float(all_mids.get(coin, 0.0))

    margin_summary = user_state.get("marginSummary", {})
    fallback_val = float(margin_summary.get("accountValue", 0.0))
    account_value = total_spot_net_worth if total_spot_net_worth > 0 else fallback_val

    daily_starting_dict = state.get("daily_starting_equity", {})
    if today_str not in daily_starting_dict:
        daily_starting_dict[today_str] = account_value
    state["daily_starting_equity"] = {d: v for d, v in daily_starting_dict.items() if d >= (ct_now - timedelta(days=14)).strftime('%Y-%m-%d')}
    today_start_eq = state["daily_starting_equity"][today_str]
    today_total_gain = account_value - today_start_eq

    # 3) Circuit breakers. These block NEW entries only; open positions keep being managed.
    entries_blocked_reason = ""
    ledger = state.get("closed_trades_ledger", [])
    recent_losses = [t for t in ledger if float(t.get("pnl_usd", 0)) < 0 and float(t.get("ts_sec", 0)) >= now_ts - CB_WINDOW_SEC]
    trades_today = [t for t in ledger if float(t.get("ts_sec", 0)) >= ct_midnight_ts]
    net_today_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_today)

    if len(recent_losses) >= CB_MAX_LOSSES and now_ts >= float(state.get("hibernating_until", 0)):
        state["hibernating_until"] = now_ts + CB_HIBERNATE_SEC
        msg = f"🚨 ROLLING CIRCUIT BREAKER: {len(recent_losses)} losses (${sum(float(t['pnl_usd']) for t in recent_losses):+.2f}) in the last {CB_WINDOW_SEC // 60}m. New entries paused {CB_HIBERNATE_SEC // 3600}h."
        audit_logs.append(msg)
        send_html_dashboard_email("🚨 Circuit Breaker: new entries paused", f"<h3>{msg}</h3><p>Open positions are still managed and protected.</p>", msg)
    if now_ts < float(state.get("hibernating_until", 0)):
        entries_blocked_reason = f"Circuit breaker hibernation ({(float(state['hibernating_until']) - now_ts) / 3600.0:.1f}h left)"
    elif today_start_eq > 0 and net_today_usd <= -DAILY_LOSS_LIMIT_PCT * today_start_eq:
        entries_blocked_reason = f"Daily loss limit hit (${net_today_usd:+.2f} ≤ -{DAILY_LOSS_LIMIT_PCT*100:.1f}% of ${today_start_eq:.2f}) until CT midnight"

    regime, regime_desc = check_btc_regime(info, now_ms)
    audit_logs.append(f"₿ BTC Regime: **{regime}** — {regime_desc}")
    if not entries_blocked_reason and regime == "RISK_OFF" and BLOCK_ENTRIES_WHEN_RISK_OFF:
        entries_blocked_reason = "BTC RISK_OFF regime (downtrend on 1h)"

    strict = regime != "RISK_ON"
    required_vol_ratio = 2.80 if strict else 1.80
    max_vwap_extension = 1.0040 if strict else 1.0080
    hod_proximity_pct = 0.0100
    audit_logs.append(f"⚡ {BOT_VERSION}: Stop {HARD_CAP_ROE*100:.1f}% ROE | Lock from +{FIRST_LOCK_TRIGGER_ROE*100:.1f}% peak (80-93%) | Scratch {SCRATCH_MINS:.0f}m / Flat {FLAT_EXIT_MINS:.0f}m | RVOL Gate {required_vol_ratio}x (rolling 15m) | VWAP Cap +{(max_vwap_extension-1)*100:.2f}%")
    if entries_blocked_reason:
        audit_logs.append(f"⛔ NEW ENTRIES BLOCKED: {entries_blocked_reason}")

    # 4) Manage open positions.
    active_count = 0
    positions_data = []
    active_coins = set()
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}
    trade_closed_this_run = False

    for pos_item in asset_positions:
        pos = pos_item.get("position", {})
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0))
        if not coin or szi == 0:
            continue

        is_long = szi > 0
        side_str = "LONG" if is_long else "SHORT"
        sz_dec = sz_decimals_map.get(coin, 4)
        entry_px = float(pos.get("entryPx", 0))
        current_px = float(all_mids.get(coin, entry_px))
        margin_used = float(pos.get("marginUsed", 0))
        unrealized_pnl = float(pos.get("unrealizedPnl", 0))
        pos_equity = abs(szi) * current_px

        current_roe = (((current_px - entry_px) / entry_px) * LEVERAGE) if is_long else (((entry_px - current_px) / entry_px) * LEVERAGE)

        cache_info = current_active_cache.get(coin, {})
        entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9930)
        strat_type = cache_info.get("strategy", "UNKNOWN")
        # Real fill time (from the opening fill) beats order-placement time for resting limit entries.
        entry_ts = float(state.get("entry_fill_times", {}).get(coin) or cache_info.get("entry_time_sec", now_ts))
        held_duration_mins = (now_ts - entry_ts) / 60.0

        # Peak from actual 1m highs since entry, not just from the prices seen at run times.
        peak_px = current_px
        try:
            since_ms = max(int(entry_ts * 1000), now_ms - 6 * 3600000)
            m1 = api_retry(info.candles_snapshot, name=coin, interval="1m", startTime=since_ms, endTime=now_ms)
            post_entry = [float(c["h"]) if is_long else float(c["l"]) for c in (m1 or []) if int(c["t"]) >= since_ms]
            if post_entry:
                peak_px = max(post_entry + [current_px]) if is_long else min(post_entry + [current_px])
        except Exception:
            pass
        candle_peak_roe = ((peak_px - entry_px) / entry_px * LEVERAGE) if is_long else ((entry_px - peak_px) / entry_px * LEVERAGE)
        peak_roe = max(current_roe, candle_peak_roe, float(cache_info.get("peak_roe", current_roe)))

        upper_wick_ratio = 0.0
        try:
            c_snap = api_retry(info.candles_snapshot, name=coin, interval="15m", startTime=now_ms - 1800000, endTime=now_ms)
            if c_snap:
                last_c = c_snap[-1]
                c_high, c_low, c_open = float(last_c["h"]), float(last_c["l"]), float(last_c["o"])
                c_range = max(1e-12, c_high - c_low)
                upper_wick_ratio = (c_high - max(c_open, current_px)) / c_range
        except Exception:
            pass

        stop_px_calc, _, leash_status = calculate_smart_exchange_targets(
            entry_px, is_long, current_px, entry_candle_low, peak_roe=peak_roe, upper_wick_pct=upper_wick_ratio
        )

        should_exit = False
        exit_reason = ""
        if held_duration_mins >= SCRATCH_MINS and current_roe < SCRATCH_ROE:
            should_exit = True
            exit_reason = f"⏱️ {SCRATCH_MINS:.0f}M negative scratch ({current_roe*100:.2f}% ROE after {held_duration_mins:.0f}m)"
        elif held_duration_mins >= FLAT_EXIT_MINS and current_roe < FLAT_EXIT_ROE:
            should_exit = True
            exit_reason = f"⏱️ {FLAT_EXIT_MINS:.0f}M flat stagnation exit ({current_roe*100:.2f}% ROE after {held_duration_mins:.0f}m)"

        if (is_long and current_px <= stop_px_calc) or ((not is_long) and current_px >= stop_px_calc):
            should_exit = True
            if peak_roe >= FIRST_LOCK_TRIGGER_ROE:
                exit_reason = f"🎯 Profit lock hit ({current_roe*100:.2f}% ROE, peak {peak_roe*100:.2f}%)"
            else:
                exit_reason = f"🛡️ Hard stop hit ({current_roe*100:.2f}% ROE)"

        if should_exit:
            audit_logs.append(f"🎯 EXIT TRIGGERED: Closing {coin} {side_str} @ ${current_px:.6g} ({exit_reason}).")
            state["pending_exit_reasons"][coin] = f"🤖 {exit_reason}"
            try:
                res = exchange.market_close(coin, slippage=0.01)
                ok, _, err = order_result(res) if res is not None else (False, [], "position already closed")
                if ok:
                    cancel_native_trigger_orders(exchange, info, coin, ACCOUNT_ADDRESS)
                    cooldown = WIN_COOLDOWN_SEC if unrealized_pnl > 0 else LOSS_COOLDOWN_SEC
                    state["cooldown_blocklist"][coin] = max(float(state["cooldown_blocklist"].get(coin, 0)), now_ts + cooldown)
                    trade_closed_this_run = True
                    continue
                audit_logs.append(f"🚨 Market close failed on {coin}: {err} — native stop left in place")
            except Exception as e:
                audit_logs.append(f"🚨 Market close failed on {coin}: {e} — native stop left in place")

        sync_native_trigger_orders(exchange, open_orders, coin, abs(szi), stop_px_calc, sz_dec, audit_logs)

        active_count += 1
        active_coins.add(coin)

        new_active_cache[coin] = {
            "entry_px": entry_px, "current_px": current_px, "szi": szi,
            "margin": margin_used, "side": side_str,
            "peak_roe": peak_roe, "strategy": strat_type,
            "entry_candle_low": entry_candle_low, "entry_time_sec": entry_ts
        }

        positions_data.append({
            "coin": coin, "side": side_str, "sz": abs(szi),
            "entry": entry_px, "current": current_px, "leverage": LEVERAGE,
            "collateral": margin_used, "position_usd": pos_equity,
            "pnl": unrealized_pnl, "roe": current_roe * 100,
            "stop": round_px(stop_px_calc, sz_dec),
            "status": leash_status
        })

    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    # 5) Scan for new entries.
    market_candidates = []
    available_slots = MAX_CRYPTO_SLOTS - active_count

    if available_slots > 0 and not entries_blocked_reason:
        state["last_scan_timestamp"] = now_ts

        all_eligible_coins = []
        for idx, asset in enumerate(meta.get("universe", [])):
            coin = asset.get("name")
            if not coin or coin in active_coins or coin in SKIP_COINS or asset.get("isDelisted"):
                continue
            try:
                ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                prev_px = float(ctx.get("prevDayPx", 0.0) or 0.0)
                mark_px = float(ctx.get("markPx", 0.0) or 0.0)
                volume_24h_usd = float(ctx.get("dayNtlVlm", 0.0) or 0.0)
                if volume_24h_usd < MIN_24H_VOLUME_USD:
                    continue
                if prev_px > 0 and mark_px > 0:
                    change_24h = ((mark_px - prev_px) / prev_px) * 100
                    all_eligible_coins.append({"coin": coin, "change_24h": change_24h, "mark_px": mark_px, "vlm_24h": volume_24h_usd, "rank": idx})
            except Exception:
                continue

        all_eligible_coins = sorted([c for c in all_eligible_coins
                                     if c["rank"] >= SKIP_FIRST_LISTED and c["change_24h"] >= MIN_24H_CHANGE_PCT],
                                    key=lambda x: x["change_24h"], reverse=True)
        top_gapper_str = ", ".join([f"{g['coin']} ({g['change_24h']:+.2f}%)" for g in all_eligible_coins[:5]])
        audit_logs.append(f"🔥 TOP GAPPERS (top {SCAN_TOP_N} up {MIN_24H_CHANGE_PCT:.0f}%+, majors skipped, {len(all_eligible_coins)} qualify): {top_gapper_str if top_gapper_str else 'None'}")

        near_misses = []
        atr_skips = {"quiet": [], "wild": []}
        for item in all_eligible_coins[:SCAN_TOP_N]:
            coin = item["coin"]

            cooldown_until = float(state.get("cooldown_blocklist", {}).get(coin, 0))
            if now_ts < cooldown_until:
                audit_logs.append(f"🔍 [{coin}] Skipped: Active Cooldown ({(cooldown_until - now_ts) / 60.0:.0f}m remaining)")
                continue

            try:
                curr_live_px = float(all_mids.get(coin, 0))
                if curr_live_px <= 0:
                    continue

                candles = api_retry(info.candles_snapshot, name=coin, interval="15m", startTime=now_ms - CANDLE_LOOKBACK_MS, endTime=now_ms)
                if not candles or len(candles) < 32:
                    continue

                # Volatility fit: average high-low range of the last completed 15m candles, as % of price.
                done = candles[-(ATR_CANDLES + 1):-1]
                atr_pct = float(np.mean([(float(c["h"]) - float(c["l"])) / float(c["c"]) for c in done])) * 100.0 if done else 0.0
                if atr_pct < MIN_ATR_PCT:
                    atr_skips["quiet"].append(f"{coin} {atr_pct:.2f}%")
                    record_blockers(state, today_str, ["Too quiet (ATR)"], coin)
                    continue
                if atr_pct > MAX_ATR_PCT:
                    atr_skips["wild"].append(f"{coin} {atr_pct:.2f}%")
                    record_blockers(state, today_str, ["Too wild (ATR)"], coin)
                    continue

                current_candle_ts = candles[-1]["t"]
                if state.get("last_traded_candle", {}).get(coin) == current_candle_ts:
                    continue

                elapsed_mins = (now_ms - candles[-1]["t"]) / 60000.0

                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]
                day_high = max(highs)   # 8h high, as in V4.8.12

                ross_ind = calculate_15m_ross_indicators(candles)
                if not ross_ind:
                    continue

                ema9 = ross_ind["ema9"]
                vwap = ross_ind["vwap"]
                macd_hist = ross_ind["macd_hist"]
                prev_close = ross_ind["prev_close"]
                prev_ema9 = ross_ind["prev_ema9"]

                extension_from_ema = (curr_live_px - ema9) / ema9
                if extension_from_ema > 0.0100:
                    audit_logs.append(f"🔍 [{coin}] Skipped: Too extended from EMA9 (+{extension_from_ema*100:.2f}%)")
                    record_blockers(state, today_str, ["Too extended from EMA9"], coin)
                    continue

                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)

                is_above_ema9 = curr_live_px > ema9
                is_above_vwap = curr_live_px > vwap
                is_macd_positive = macd_hist > 0.0

                prev_low = lows[-2]
                vwap_dist = curr_live_px / vwap
                is_near_hod = (curr_live_px >= day_high * (1.0 - hod_proximity_pct))

                # RVOL = last 15 minutes of real volume (rolling, from 1m candles) vs. the average 15m candle.
                # Only fetched when a coin passes every non-volume rule of some setup, to stay inside API limits.
                vwap_ok_pre = vwap_dist <= max_vwap_extension
                prev_45_min = min(lows[-4:-1])
                candle_range = max(1e-12, highs[-1] - lows[-1])
                lower_wick_ratio = (min(float(candles[-1]["o"]), curr_live_px) - lows[-1]) / candle_range
                reclaim_pre = (prev_close <= prev_ema9 or prev_close <= vwap) and is_above_ema9 and is_above_vwap and curr_live_px <= ema9 * 1.0025
                needs_volume = vwap_ok_pre and is_above_ema9 and is_macd_positive and (
                    (is_near_hod and is_above_vwap) or reclaim_pre or (lows[-1] <= prev_45_min and lower_wick_ratio >= 0.45))
                rolling_vol = rolling_15m_volume(info, coin, now_ms) if needs_volume else None
                rvol_measured = rolling_vol is not None
                if rolling_vol is None:
                    rolling_vol = volumes[-1] * min(3.0, 15.0 / max(0.5, elapsed_mins))   # old estimate (logs only)
                vol_ratio = rolling_vol / avg_vol if avg_vol > 0 else 1.0

                is_breakout = is_near_hod and (vol_ratio >= required_vol_ratio) and (vwap_dist <= max_vwap_extension) and is_above_ema9 and is_above_vwap and is_macd_positive

                is_reclaim_cross = (prev_close <= prev_ema9 or prev_close <= vwap) and (curr_live_px > ema9 and curr_live_px > vwap)
                is_pullback_proximity = (curr_live_px <= ema9 * 1.0025)
                is_bullish_reclaim = (is_reclaim_cross and is_pullback_proximity and vol_ratio >= required_vol_ratio and (vwap_dist <= max_vwap_extension) and is_above_ema9 and is_macd_positive)

                is_capitulation_low = (lows[-1] <= prev_45_min)

                is_v_bottom = (is_capitulation_low and lower_wick_ratio >= 0.45 and vol_ratio >= required_vol_ratio and (vwap_dist <= max_vwap_extension) and curr_live_px > ema9 and is_macd_positive)

                if not (is_breakout or is_bullish_reclaim or is_v_bottom):
                    # Near-miss diagnostics only; does not change what the bot trades.
                    # Volume is only measured for coins that passed every other rule; unmeasured = not blamed.
                    rvol_c = (vol_ratio >= required_vol_ratio or not rvol_measured, "RVOL", f"RVOL {vol_ratio:.2f}x (need {required_vol_ratio}x)")
                    vwap_c = (vwap_dist <= max_vwap_extension, "VWAP stretch", f"VWAP stretch +{(vwap_dist-1)*100:.2f}% (max +{(max_vwap_extension-1)*100:.2f}%)")
                    ema_c = (is_above_ema9, "Below EMA9", f"below EMA9 ({(curr_live_px/ema9-1)*100:+.2f}%)")
                    macd_c = (is_macd_positive, "MACD negative", "MACD histogram negative")
                    setups = {
                        "HOD_BREAKOUT": setup_misses([
                            (is_near_hod, "Not near day high", f"not near day high ({(curr_live_px/day_high-1)*100:+.2f}% vs HOD)"),
                            rvol_c, vwap_c, ema_c, (is_above_vwap, "Below VWAP", "below VWAP"), macd_c]),
                        "BULLISH_RECLAIM": setup_misses([
                            (is_reclaim_cross, "No reclaim cross", "no EMA9/VWAP reclaim cross"),
                            (is_pullback_proximity, "Not a pullback", f"too far above EMA9 for a pullback (+{extension_from_ema*100:.2f}%)"),
                            rvol_c, vwap_c, ema_c, macd_c]),
                        "V_BOTTOM_RECLAIM": setup_misses([
                            (is_capitulation_low, "No fresh low", "no fresh 45m low"),
                            (lower_wick_ratio >= 0.45, "Wick too small", f"lower wick {lower_wick_ratio*100:.0f}% (need 45%)"),
                            rvol_c, vwap_c, ema_c, macd_c]),
                    }
                    best_setup, best_fails = min(setups.items(), key=lambda kv: len(kv[1]))
                    if len(best_fails) <= NEAR_MISS_MAX_FAILS:
                        near_misses.append((len(best_fails), -vol_ratio, coin, best_setup, [d for _, d in best_fails]))
                        if len(best_fails) == 1:
                            record_blockers(state, today_str, [best_fails[0][0]], coin)
                    continue

                if is_breakout or is_bullish_reclaim or is_v_bottom:
                    is_liquid, spread_msg = check_liquidity_and_spread(info, coin, max_spread=0.0008, min_depth_usd=1500.0)
                    if not is_liquid:
                        audit_logs.append(f"🔍 [{coin}] Skipped: Liquidity/Spread Check Failed ({spread_msg})")
                        continue

                    if is_v_bottom:
                        setup_type, score = "V_BOTTOM_RECLAIM", vol_ratio * 3.2
                    elif is_bullish_reclaim:
                        setup_type, score = "BULLISH_RECLAIM", vol_ratio * 2.4
                    else:
                        setup_type, score = "HOD_BREAKOUT", vol_ratio * 1.0

                    target_limit_px = min(curr_live_px * 0.9990, max(ema9, vwap))
                    audit_logs.append(f"🔥 [{coin}] TRIGGER [{setup_type}] @ ${curr_live_px:.6g} (RVOL {vol_ratio:.2f}x, req {required_vol_ratio}x, ATR {atr_pct:.2f}%)")

                    market_candidates.append({
                        "coin": coin, "close": curr_live_px, "limit_px": target_limit_px,
                        "score": score, "candle_ts": current_candle_ts,
                        "strategy": setup_type, "entry_candle_low": min(lows[-1], prev_low)
                    })
            except Exception as e:
                audit_logs.append(f"⚠️ [{coin}] scan error: {e}")
                continue

        if atr_skips["quiet"] or atr_skips["wild"]:
            audit_logs.append(f"🔇 Volatility filter ({MIN_ATR_PCT:.2f}-{MAX_ATR_PCT:.2f}% ATR): {len(atr_skips['quiet'])} too quiet"
                              f"{' (' + ', '.join(atr_skips['quiet'][:4]) + ')' if atr_skips['quiet'] else ''}, {len(atr_skips['wild'])} too wild"
                              f"{' (' + ', '.join(atr_skips['wild'][:4]) + ')' if atr_skips['wild'] else ''}")
        for n_fail, _, coin, setup_name, fails in sorted(near_misses)[:NEAR_MISS_SHOW]:
            tag = "everything else passed" if n_fail == 1 else f"{n_fail} rules short"
            audit_logs.append(f"🎯 NEAR MISS [{coin}] {setup_name}: {'; '.join(fails)} — {tag}")

        if not market_candidates:
            audit_logs.append(f"ℹ️ SCAN COMPLETED: 0 coins met {BOT_VERSION} trigger rules on this candle.")
        else:
            audit_logs.append(f"🚀 SCAN COMPLETED: {len(market_candidates)} candidate(s) triggered!")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    trades_executed = False

    if available_slots > 0 and market_candidates and not entries_blocked_reason:
        best = market_candidates[0]
        coin = best["coin"]
        strat_used = best["strategy"]
        is_market = strat_used == "HOD_BREAKOUT"
        decimals = sz_decimals_map.get(coin, 4)

        entry_calc_px = best["close"] if is_market else best["limit_px"]
        target_notional_usd = max(MIN_NOTIONAL_USD, account_value * POSITION_FRACTION)
        raw_sz = max(target_notional_usd, MIN_NOTIONAL_USD) / entry_calc_px
        sz = int(np.ceil(raw_sz)) if decimals == 0 else round(np.ceil(raw_sz * (10 ** decimals)) / (10 ** decimals), decimals)

        initial_stop_px, _, _ = calculate_smart_exchange_targets(entry_calc_px, True, entry_calc_px, best["entry_candle_low"])

        if sz > 0:
            try:
                try:
                    exchange.update_leverage(LEVERAGE, coin, True)
                except Exception:
                    pass

                if place_entry_with_stop(exchange, coin, sz, entry_calc_px, is_market, initial_stop_px, decimals, audit_logs):
                    trades_executed = True
                    state.setdefault("last_traded_candle", {})[coin] = best["candle_ts"]
                    state["active_position_cache"][coin] = {
                        "strategy": strat_used, "entry_px": entry_calc_px, "side": "LONG",
                        "entry_candle_low": best["entry_candle_low"], "entry_time_sec": now_ts
                    }
            except Exception as e:
                audit_logs.append(f"ORDER PLACEMENT FAILED on {coin}: {e}")

    # 5b) Shadow shorts: paper only. These functions are never given the exchange object.
    if SHADOW_SHORTS_ENABLED:
        manage_shadow_shorts(info, state, all_mids, now_ms, now_ts, audit_logs)
        if regime in SHADOW_REGIMES:
            scan_shadow_shorts(info, state, meta, asset_ctxs, all_mids, now_ms, now_ts, account_value, audit_logs, regime, today_str)
        else:
            audit_logs.append(f"👻 Shadow shorts idle: BTC {regime} (scans during {' / '.join(SHADOW_REGIMES)})")

    # 6) Record this run's own exits (IOC closes fill immediately) so the ledger and email are current.
    if trade_closed_this_run and not DRY_RUN:
        time.sleep(1.0)
        sync_closed_trades_from_exchange(info, ACCOUNT_ADDRESS, state, audit_logs, time.time())

    total_margin_used = sum(float(p.get("collateral", 0.0)) for p in positions_data)
    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    last_email_ts = float(state.get("last_email_timestamp", 0))
    elapsed_minutes = (now_ts - last_email_ts) / 60.0
    is_manual_run = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"
    should_send_email = is_manual_run or (elapsed_minutes >= 15.0) or trades_executed or trade_closed_this_run

    if not should_send_email:
        save_state(state)
        print(f"[{timestamp}] Execution cycle complete. Skipping email dispatch.", flush=True)
        for line in audit_logs:
            print(line, flush=True)
        return

    state["last_email_timestamp"] = now_ts
    save_state(state)

    ledger = state.get("closed_trades_ledger", [])
    trades_today = [t for t in ledger if float(t.get("ts_sec", 0)) >= ct_midnight_ts]
    total_today = len(trades_today)
    win_count = len([t for t in trades_today if float(t.get("pnl_usd", 0)) > 0])
    loss_count = total_today - win_count
    win_rate_today = (win_count / total_today * 100) if total_today > 0 else 0.0
    net_today_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_today)
    fees_today_usd = sum(float(t.get("fees_usd", 0)) for t in trades_today)

    # Scorecard of real trades under the current strategy (since V4.10.1 went live).
    cur_trades = [t for t in ledger if float(t.get("ts_sec", 0)) >= SCORECARD_SINCE_TS]
    sc_wins = [t for t in cur_trades if float(t.get("pnl_usd", 0)) > 0]
    sc_losses = [t for t in cur_trades if float(t.get("pnl_usd", 0)) <= 0]
    sc_avg_w = sum(float(t["roe_pct"]) for t in sc_wins) / len(sc_wins) if sc_wins else 0.0
    sc_avg_l = sum(float(t["roe_pct"]) for t in sc_losses) / len(sc_losses) if sc_losses else 0.0
    sc_net = sum(float(t.get("pnl_usd", 0)) for t in cur_trades)
    exit_kinds = {}
    for t in cur_trades:
        r = str(t.get("exit_reason", ""))
        kind = ("15m scratch" if "scratch" in r.lower() else "30m flat" if "flat" in r.lower()
                else "Profit lock" if ("lock" in r.lower() or "trail" in r.lower()) else "Hard stop" if "stop" in r.lower() else "Other")
        exit_kinds[kind] = exit_kinds.get(kind, 0) + 1
    exits_line = " &bull; ".join(f"{k}: {v}" for k, v in sorted(exit_kinds.items(), key=lambda kv: -kv[1])) or "—"
    since_label = format_ct(SCORECARD_SINCE_TS)[:16]

    shadow = state.get("shadow_shorts", {"open": {}, "closed": []})
    sh_closed = [t for t in shadow.get("closed", []) if t["ts_sec"] >= now_ts - SHADOW_HISTORY_DAYS * 86400]
    sh_wins = [t for t in sh_closed if t["pnl_usd"] > 0]
    sh_losses = [t for t in sh_closed if t["pnl_usd"] <= 0]
    sh_avg_w = sum(t["roe_pct"] for t in sh_wins) / len(sh_wins) if sh_wins else 0.0
    sh_avg_l = sum(t["roe_pct"] for t in sh_losses) / len(sh_losses) if sh_losses else 0.0
    sh_net = sum(t["pnl_usd"] for t in sh_closed)
    sh_by_regime = ""
    for rg in SHADOW_REGIMES:
        g = [t for t in sh_closed if t.get("regime", "RISK_OFF") == rg]
        if g:
            w = sum(1 for t in g if t["pnl_usd"] > 0)
            sh_by_regime += f"<div>&nbsp;&nbsp;{rg}: {len(g)} trades &bull; {w}W/{len(g)-w}L &bull; net ${sum(t['pnl_usd'] for t in g):+.3f}</div>"
    sh_rows = "".join(f"<div>&bull; OPEN {c} {p.get('setup', '')} [{p.get('regime', '')}] @ ${p['entry_px']:.6g} → now {p.get('roe', 0)*100:+.2f}% ROE ({p.get('status', '')})</div>"
                      for c, p in shadow.get("open", {}).items())
    sh_rows += "".join(f"<div>&bull; {t['coin']} {t['setup']} [{t.get('regime', 'RISK_OFF')}]: <b style=\"color: {'#15803d' if t['pnl_usd'] > 0 else '#b91c1c'};\">${t['pnl_usd']:+.3f} ({t['roe_pct']:+.2f}%)</b> — {t['reason']} &bull; {t['timestamp']}</div>"
                       for t in sh_closed[:5])
    shadow_blockers_line = blockers_summary(state, today_str, "shadow_near_miss_stats")
    shadow_section = f"""
        <div style="background-color: #f5f3ff; border: 1px solid #ddd6fe; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #4c1d95; line-height: 1.5;">
          <div style="font-weight: 700; text-transform: uppercase; font-size: 10px;">👻 Shadow Shorts — paper only, no orders (last {SHADOW_HISTORY_DAYS} days)</div>
          <div>Trades: <b>{len(sh_closed)}</b> &bull; {len(sh_wins)}W / {len(sh_losses)}L &bull; avg win {sh_avg_w:+.2f}% / avg loss {sh_avg_l:+.2f}% ROE &bull; est. net after fees <b style="color: {'#15803d' if sh_net >= 0 else '#b91c1c'};">${sh_net:+.3f}</b></div>
          {sh_by_regime}
          <div style="margin-top: 4px;"><b>Short blockers today:</b> {shadow_blockers_line}</div>
          {sh_rows or f'<div>No shadow shorts yet (scans while BTC is {" or ".join(SHADOW_REGIMES)}).</div>'}
        </div>
    """ if SHADOW_SHORTS_ENABLED else ""

    blockers_line = blockers_summary(state, today_str)

    text_fallback = (f"{BOT_TITLE}\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\n"
                     f"Active Positions: {active_count}/{MAX_CRYPTO_SLOTS}\nToday net (after fees): ${net_today_usd:+.2f}\n\n" + "\n".join(audit_logs))

    positions_cards = ""
    if positions_data:
        for p in positions_data:
            pnl_color = '#2e7d32' if p['pnl'] >= 0 else '#c62828'
            side_color = '#2e7d32' if p['side'] == 'LONG' else '#c62828'
            positions_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{p['coin']}</span>
                  <span style="font-size: 10px; color: {side_color}; font-weight: 700; margin-left: 6px;">{p['side']} ({p['leverage']}x)</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${p['pnl']:+.2f} ({p['roe']:+.2f}%)</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Val:</b> ${p['position_usd']:.2f} &bull; <b>Cost:</b> ${p['collateral']:.2f} &bull; <b>Stop:</b> ${p['stop']}
              </div>
              <div style="font-size: 10px; color: #2e7d32; font-weight: 600;">
                <b>Status:</b> {p['status']}
              </div>
            </div>
            """
    else:
        positions_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No active positions.</div>'

    closed_cards = ""
    recent_closed = ledger[:10]
    total_realized_pnl = sum(float(t.get('pnl_usd', 0.0)) for t in recent_closed)

    if recent_closed:
        for t in recent_closed:
            pnl_val = float(t.get('pnl_usd', 0.0))
            roe_val = float(t.get('roe_pct', 0.0))
            pnl_color = '#2e7d32' if pnl_val >= 0 else '#c62828'
            side_color = '#2e7d32' if t.get('side') == 'LONG' else '#c62828'
            closed_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{t['coin']}</span>
                  <span style="font-size: 10px; color: {side_color}; font-weight: 700; margin-left: 6px;">{t.get('side', 'LONG')}</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${pnl_val:+.3f} ({roe_val:+.2f}%)</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 3px;">
                <b>Entry:</b> ${t.get('entry_price', 0)} &bull; <b>Exit:</b> ${t.get('exit_price', 0)} &bull; <b>Fees:</b> ${float(t.get('fees_usd', 0)):.3f}
              </div>
              <div style="font-size: 10px; color: #64748b; margin-bottom: 3px;">
                <b>Time:</b> {t.get('timestamp', 'N/A')}
              </div>
              <div style="font-size: 10px; color: #b45309;">
                <b>Reason:</b> {t.get('exit_reason', 'N/A')}
              </div>
            </div>
            """
    else:
        closed_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No recent exits.</div>'

    total_pnl_color = '#2e7d32' if total_realized_pnl >= 0 else '#c62828'

    audit_rows = "".join([f"<tr><td style='padding: 6px 8px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 10px; color: #475569;'>{log}</td></tr>" for log in audit_logs])
    audit_section = f"""
    <div style="font-size: 12px; text-transform: uppercase; color: #d97706; margin: 18px 0 6px 0; font-weight: 700;">Live Telemetry & Audit Log</div>
    <div style="width: 100%; border: 1px solid #fde68a; border-radius: 6px; background-color: #fffbeb; margin-bottom: 15px;">
      <table style="width: 100%; font-size: 11px; border-collapse: collapse;">
        <tbody>{audit_rows}</tbody>
      </table>
    </div>
    """

    blocked_banner = ""
    if entries_blocked_reason:
        blocked_banner = f'<div style="background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #b91c1c; font-weight: 700;">⛔ New entries blocked: {entries_blocked_reason}</div>'

    html_content = f"""<!DOCTYPE html>
<html>
  <head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">

      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff;">TR-GC-Crypto-LS-23 | Profit-Lock Scalper ({BOT_VERSION}){' — DRY RUN' if DRY_RUN else ''}</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; BTC regime: {regime}</div>
      </div>

      <div style="padding: 12px;">
        {blocked_banner}
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700;">Total Net Worth</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${account_value:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px;">
            <div>Reserve: <b style="color: #0f172a;">${static_usdc:.2f}</b> &bull; Margin: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain (equity): <span style="color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
          </div>
        </div>

        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 10px; margin-bottom: 14px;">
          <div style="font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase;">📊 Today's Realized Performance ({today_str})</div>
          <div style="font-size: 11px; color: #166534;">Trades: <b>{total_today}</b> &bull; Win Rate: <b>{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></div>
          <div style="font-size: 11px; color: #166534; font-weight: 700; margin-top: 4px;">Net P&amp;L after fees: <span style="color: {'#15803d' if net_today_usd >= 0 else '#b91c1c'};">${net_today_usd:+.3f}</span> &bull; Fees paid: ${fees_today_usd:.3f}</div>
        </div>

        <div style="background-color: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #7c2d12; line-height: 1.5;">
          <div style="font-weight: 700; text-transform: uppercase; font-size: 10px;">⚖️ Strategy Scorecard — real trades since {since_label} CT ({BOT_VERSION} rules)</div>
          <div>Trades: <b>{len(cur_trades)}</b> &bull; {len(sc_wins)}W / {len(sc_losses)}L &bull; avg win <b>{sc_avg_w:+.2f}%</b> / avg loss <b>{sc_avg_l:+.2f}%</b> ROE &bull; net after fees <b style="color: {'#15803d' if sc_net >= 0 else '#b91c1c'};">${sc_net:+.3f}</b></div>
          <div>Exits: {exits_line}</div>
        </div>

        <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #1e3a8a; line-height: 1.5;">
          <div style="font-weight: 700; text-transform: uppercase; font-size: 10px;">🎯 Today's Blockers (coins stopped by a single rule today, each coin counted once)</div>
          <div>{blockers_line}</div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active {BOT_VERSION} Rules</div>
          <div>&bull; <b>Stop attached to entry:</b> protected from the moment of fill</div>
          <div>&bull; <b>Universe:</b> top {SCAN_TOP_N} gainers up {MIN_24H_CHANGE_PCT:.0f}%+ (majors skipped) that FIT the plan: 15m ATR {MIN_ATR_PCT:.2f}–{MAX_ATR_PCT:.2f}%</div>
          <div>&bull; <b>V4.8.12 trade rules:</b> 8h candles, hard stop {HARD_CAP_ROE*100:.1f}% ROE</div>
          <div>&bull; <b>Profit lock:</b> +{FIRST_LOCK_ROE*100:.2f}% after +{FIRST_LOCK_TRIGGER_ROE*100:.1f}% peak, then 80% / 85% / 90% / 93% of peak (peak from 1m highs), climax lock</div>
          <div>&bull; <b>Time exits:</b> {SCRATCH_MINS:.0f}m negative scratch (&lt;{SCRATCH_ROE*100:.1f}% ROE), {FLAT_EXIT_MINS:.0f}m flat (&lt;+{FLAT_EXIT_ROE*100:.1f}% ROE)</div>
          <div>&bull; <b>BTC regime filter</b> replaces Gemini; RISK_OFF blocks new longs</div>
          <div>&bull; <b>👻 Shadow shorts (paper only):</b> during {" / ".join(SHADOW_REGIMES)}, top losers down {SHADOW_MIN_24H_DROP_PCT:.0f}%+ scanned for breakdown / rejection / A-top; tracked with the same stops, locks and time exits. No orders are ever sent.</div>
          <div>&bull; <b>Breakers:</b> {CB_MAX_LOSSES} losses/{CB_WINDOW_SEC // 60}m or -{DAILY_LOSS_LIMIT_PCT*100:.1f}% day → entries paused, positions still managed</div>
        </div>

        {shadow_section}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades (net of fees)</div>
        {closed_cards}

        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          LAST 10 TRADES NET P&amp;L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.3f}</span>
        </div>

        {audit_section}

      </div>
      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Hyperliquid 15M Scalper Autonomous Engine &bull; {BOT_VERSION}
      </div>
    </div>
  </body>
</html>"""

    send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}{' [DRY RUN]' if DRY_RUN else ''}", html_content, text_fallback)


if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing Profit-Lock Scalper cycle ({BOT_VERSION}){' [DRY RUN]' if DRY_RUN else ''}...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {BOT_VERSION} cycle completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
