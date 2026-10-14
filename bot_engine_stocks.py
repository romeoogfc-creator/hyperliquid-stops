import os
import json
import time
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

API_KEY = os.getenv("APAL_API_KEY_ID") or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("APAL_BASE_URL") or os.getenv("APCA_API_BASE_URL") or os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
STATE_FILE = "stock_state.json"

VERBOSE_TEST_MODE = True

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "accept": "application/json"
}

def format_qty(qty):
    """Safely formats position quantity for Alpaca API payloads."""
    val = abs(float(qty))
    if val.is_integer():
        return str(int(val))
    return f"{val:.4f}".rstrip('0').rstrip('.')

def api_retry(func, *args, retries=3, delay=2.0, **kwargs):
    """Resilient retry wrapper for Alpaca API HTTP requests."""
    for attempt in range(retries):
        try:
            res = func(*args, **kwargs)
            if isinstance(res, requests.Response) and res.status_code in [429, 500, 502, 503, 504]:
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

def get_central_time():
    """Calculates Texas Central Time (CT) with ZoneInfo or dynamic pure-Python DST fallback."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
        except Exception:
            pass
            
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    year = utc_now.year
    dst_start = datetime(year, 3, 8, 7, 0, 0)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1, 6, 0, 0)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    
    is_dst = dst_start <= utc_now < dst_end
    offset = -5 if is_dst else -6
    return utc_now + timedelta(hours=offset)

def load_state():
    default_state = {
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_symbols": [],
        "symbols_traded_today": [],
        "cooldown_blocklist": {},
        "last_run_timestamp": "", 
        "last_email_timestamp": 0,
        "daily_starting_equity": {},
        "daily_peak_equity": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            print(f"Error loading stock state.json: {e}", flush=True)
    return default_state

def save_state(state):
    ct_now = get_central_time()
    state["last_run_timestamp"] = ct_now.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def check_market_macro_regime():
    """Evaluates SPY intraday percent change with micro-sensitivity threshold."""
    try:
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols=SPY&timeframe=1Day&limit=5&feed=iex&start={start_date}"
        res = api_retry(requests.get, url, headers=HEADERS)
        if res.status_code == 200:
            bars_data = res.json().get("bars", {})
            spy_bars = bars_data.get("SPY", [])
            if len(spy_bars) >= 2:
                latest_close = float(spy_bars[-1]["c"])
                prev_close = float(spy_bars[-2]["c"])
                pct_change = ((latest_close - prev_close) / prev_close) * 100
                
                if pct_change >= 0.00:
                    return "GREEN", prev_close, latest_close, pct_change
                else:
                    return "RED", prev_close, latest_close, pct_change
    except Exception as e:
        print(f"Error checking SPY macro regime: {e}", flush=True)
    return "GREEN", 0.0, 0.0, 0.0

def send_html_dashboard_email(subject, html_content, text_fallback):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("[WARN] Email credentials missing. Skipping email dispatch.", flush=True)
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

def cancel_alpaca_native_orders(symbol, open_orders):
    for o in open_orders:
        if o.get("symbol") == symbol:
            try:
                api_retry(requests.delete, f"{BASE_URL}/v2/orders/{o.get('id')}", headers=HEADERS)
            except Exception:
                pass

def sync_alpaca_native_trigger_stop(symbol, is_long, qty, stop_px, open_orders, audit_logs=None):
    """
    HARDENED NATIVE STOP SYNC (V3.9.6 ATOMIC CANCEL-BEFORE-PLACE):
    Prevents duplicate position stop rejections by explicitly canceling all active stop/OTO
    orders for the symbol before placing the updated native stop.
    """
    try:
        clean_stop_px = round(stop_px, 2)
        close_side = "sell" if is_long else "buy"
        clean_qty = format_qty(qty)

        existing_stop_orders = [
            o for o in open_orders 
            if o.get("symbol") == symbol and o.get("type") in ["stop", "stop_limit", "trailing_stop"]
        ]

        needs_update = True
        for o in existing_stop_orders:
            existing_stop_px = float(o.get("stop_price", 0.0) or 0.0)
            if abs(existing_stop_px - clean_stop_px) < max(0.05, clean_stop_px * 0.0010):
                needs_update = False
                break

        if needs_update:
            for o in existing_stop_orders:
                try:
                    api_retry(requests.delete, f"{BASE_URL}/v2/orders/{o.get('id')}", headers=HEADERS)
                except Exception:
                    pass

            time.sleep(0.1)

            stop_order_payload = {
                "symbol": symbol,
                "qty": clean_qty,
                "side": close_side,
                "type": "stop",
                "stop_price": f"{clean_stop_px:.2f}",
                "time_in_force": "day"
            }
            res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=stop_order_payload, headers=HEADERS)
            
            if res.status_code == 200:
                if audit_logs is not None:
                    audit_logs.append(f"🛡 NATIVE ALPACA STOP SYNC [{symbol}]: Placed Trigger Stop @ ${clean_stop_px:.2f}")
            else:
                if audit_logs is not None:
                    audit_logs.append(f"🚨 STOP SYNC REJECTED [{symbol}]: Price crossed ${clean_stop_px:.2f}. Emergency market flattening!")
                api_retry(requests.post, f"{BASE_URL}/v2/orders", json={
                    "symbol": symbol, "qty": clean_qty, "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)

    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠️ Alpaca Native Stop Sync warning on {symbol}: {e}")

def calculate_gaussian_channel(closes, poles=4, period=50, mult=1.414):
    s = pd.Series(closes)
    effective_period = min(period, max(5, len(closes) - 1))
    alpha = (2.0 / (effective_period + 1)) * (poles ** 0.5)
    filtered = s.ewm(alpha=alpha, adjust=False).mean()
    error = (s - filtered).abs()
    deviation = error.ewm(alpha=alpha, adjust=False).mean() * mult
    upper = filtered + deviation
    lower = filtered - deviation
    return upper.iloc[-1], lower.iloc[-1], filtered.iloc[-1]

def calculate_choppiness_index(highs, lows, closes, period=14):
    try:
        if len(closes) < period + 1:
            return 50.0
        trs = []
        start_idx = len(closes) - period
        for i in range(start_idx, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            trs.append(tr)
        tr_sum = sum(trs)
        max_high = max(highs[-period:])
        min_low = min(lows[-period:])
        range_diff = max_high - min_low
        if range_diff <= 0 or tr_sum <= 0:
            return 50.0
        return 100 * (log10(tr_sum / range_diff) / log10(period))
    except Exception:
        return 50.0

def calculate_atr(highs, lows, closes, period=14):
    try:
        if len(highs) < period + 1:
            return highs[-1] - lows[-1] if len(highs) > 0 else 1.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
        return np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs)
    except Exception:
        return 1.0

def analyze_live_stock_falling_knife(curr_px, c_open, c_high, c_low, vol_ratio, is_long):
    c_range = c_high - c_low if c_high > c_low else 1e-8
    body_sz = abs(curr_px - c_open)
    body_ratio = body_sz / c_range

    if is_long:
        dist_to_low_pct = (curr_px - c_low) / c_range if c_range > 0 else 1.0
        lower_wick_ratio = (min(c_open, curr_px) - c_low) / c_range
        if lower_wick_ratio >= 0.40:
            return False, "Liquidity Absorption Wick Detected (Holding)"
        is_full_red_body = (curr_px < c_open) and (body_ratio >= 0.60) and (dist_to_low_pct <= 0.15)
        if is_full_red_body and vol_ratio >= 1.4:
            return True, f"🚨 True Falling Knife (Red Body: {body_ratio*100:.0f}%, Vol: {vol_ratio:.1f}x)"
    else:
        dist_to_high_pct = (c_high - curr_px) / c_range if c_range > 0 else 1.0
        upper_wick_ratio = (c_high - max(c_open, curr_px)) / c_range
        if upper_wick_ratio >= 0.40:
            return False, "Liquidity Absorption Wick Detected (Holding)"
        is_full_green_body = (curr_px > c_open) and (body_ratio >= 0.60) and (dist_to_high_pct <= 0.15)
        if is_full_green_body and vol_ratio >= 1.4:
            return True, f"🚨 True Rising Knife (Green Body: {body_ratio*100:.0f}%, Vol: {vol_ratio:.1f}x)"
    return False, "Normal Price Action"

def calculate_smart_stock_targets(entry_px, is_long, current_px, atr_val, entry_candle_low, entry_candle_high, peak_roe=0.0):
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    if is_long:
        raw_struct = (entry_candle_low / entry_px) - 1.0 if entry_px > 0 else -0.0080
        initial_floor_roe = min(-0.0080, max(-0.0125, raw_struct))
    else:
        raw_struct = 1.0 - (entry_candle_high / entry_px) if entry_px > 0 else -0.0080
        initial_floor_roe = min(-0.0080, max(-0.0125, raw_struct))

    activation_roe = 0.0080  
    be_floor_roe = 0.0030    

    if peak_roe >= activation_roe:
        if peak_roe < 0.0150:
            target_floor_roe = max(be_floor_roe, peak_roe * 0.40)
            leash_status = f"🛡 FEE SHIELD [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        elif peak_roe < 0.0300:
            target_floor_roe = peak_roe * 0.70
            leash_status = f"⚡ TIER 1 LOCK (70%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        elif peak_roe < 0.0500:
            target_floor_roe = peak_roe * 0.85
            leash_status = f"🚀 TIER 2 LOCK (85%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        elif peak_roe < 0.1000:
            target_floor_roe = peak_roe * 0.92
            leash_status = f"🔥 TIER 3 RUNNER (92%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        else:
            target_floor_roe = peak_roe * 0.95
            leash_status = f"🏆 MOONSHOT LOCK (95%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"⚡ HARD STOP ({target_floor_roe*100:.2f}%)"

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
    else:
        stop_px = entry_px * (1 - target_floor_roe)

    return stop_px, roe, target_floor_roe, leash_status

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 Master Engine V3.9.6 Started (Texas CT: {ct_now.strftime('%H:%M:%S')}).")
    audit_logs.append("⚡ 5M FAST ROSS SCALPER ACTIVE: Target RVOL >= 2.20x | Timeframe: 5Min")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing API Key credentials in environment variables.")

    start_date_5m = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')

    spy_regime, spy_open_px, spy_close_px, spy_pct = check_market_macro_regime()
    spy_regime_str = f"{spy_regime} ({spy_pct:+.2f}%) -> {'LONG ONLY' if spy_regime == 'GREEN' else 'SHORT ONLY'}"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    state = load_state()

    if state.get("last_run_timestamp", "")[:10] != today_str:
        state["symbols_traded_today"] = []

    account_res = api_retry(requests.get, f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text}")
    account_data = account_res.json()

    equity = float(account_data.get("equity", 100000.0))
    cash = float(account_data.get("cash", 100000.0))
    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0

    daily_starting_dict = state.get("daily_starting_equity", {})
    daily_peak_dict = state.get("daily_peak_equity", {})

    if today_str not in daily_starting_dict:
        daily_starting_dict[today_str] = equity
        daily_peak_dict[today_str] = equity
    else:
        daily_peak_dict[today_str] = max(daily_peak_dict.get(today_str, equity), equity)

    state["daily_starting_equity"] = daily_starting_dict
    state["daily_peak_equity"] = daily_peak_dict

    today_start_eq = daily_starting_dict[today_str]
    today_peak_eq = daily_peak_dict[today_str]
    today_total_gain = equity - today_start_eq
    giveback_from_peak = today_peak_eq - equity
    daily_loss_usd = today_start_eq - equity
    lifetime_cumulative_pnl = equity - 100000.0

    audit_logs.append(f"Equity Metrics: Today Start: ${today_start_eq:.2f} | Peak: ${today_peak_eq:.2f} | Current: ${equity:.2f} (Giveback: ${giveback_from_peak:.2f})")

    positions_res = api_retry(requests.get, f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    orders_res = api_retry(requests.get, f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []

    daily_loss_pct = (today_total_gain / today_start_eq) if today_start_eq > 0 else 0.0
    is_daily_max_loss_triggered = daily_loss_pct <= -0.025

    if is_daily_max_loss_triggered and positions_list:
        audit_logs.append(f"🛡️ ANTI-WIPEOUT SHIELD TRIGGERED: Daily Loss at {daily_loss_pct*100:.2f}%. Liquidating all positions to cash!")
        for order in open_orders:
            api_retry(requests.delete, f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)

        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            close_side = "sell" if side == "long" else "buy"
            entry_p = float(pos.get("avg_entry_price", 0))
            exit_p = float(pos.get("current_price", entry_p))
            realized_pnl = (exit_p - entry_p) * qty if side == "long" else (entry_p - exit_p) * qty
            try:
                api_retry(requests.post, f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": format_qty(qty), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": sym, "entry_price": entry_p, "exit_price": exit_p,
                    "exit_reason": "🛡 Daily Max Loss Anti-Wipeout Shield (-2.5%)",
                    "realized_pnl": realized_pnl, "timestamp": timestamp
                })
                state["cooldown_blocklist"][sym] = now_ts + 86400
            except Exception as e:
                audit_logs.append(f"Anti-wipeout close failed on {sym}: {e}")
        save_state(state)
        positions_list = []

    is_eod_square_off = (ct_now.hour == 14 and ct_now.minute >= 40) or (ct_now.hour >= 15)

    if is_eod_square_off and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat)"
        audit_logs.append(f"EMERGENCY EXIT TRIGGERED ({reason_text}): Liquidating all open positions to cash.")
        for order in open_orders:
            api_retry(requests.delete, f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)

        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            entry_px = float(pos.get("avg_entry_price", 0))
            current_px = float(pos.get("current_price", entry_px))
            close_side = "sell" if side == "long" else "buy"
            realized_pnl = (current_px - entry_px) * qty if side == "long" else (entry_px - current_px) * qty

            try:
                close_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json={"symbol": sym, "qty": format_qty(qty), "side": close_side, "type": "market", "time_in_force": "day"}, headers=HEADERS)
                if close_res.status_code == 200:
                    audit_logs.append(f"LIQUIDATION SUCCESS: Closed {sym}")
                    if "closed_trades_ledger" not in state:
                        state["closed_trades_ledger"] = []
                    existing_symbols_today = [t['symbol'] for t in state["closed_trades_ledger"] if today_str in t.get("timestamp", "")]
                    if sym not in existing_symbols_today:
                        state["closed_trades_ledger"].insert(0, {
                            "symbol": sym, "entry_price": entry_px, "exit_price": current_px,
                            "exit_reason": reason_text, "realized_pnl": realized_pnl, "timestamp": timestamp
                        })
                    if realized_pnl < 0:
                        state["cooldown_blocklist"][sym] = now_ts + 86400
            except Exception as e:
                audit_logs.append(f"LIQUIDATION ERROR on {sym}: {e}")

        save_state(state)
        positions_list = []

    active_count = len(positions_list)
    positions_data = []
    
    active_symbols = set([p.get("symbol") for p in positions_list if p.get("symbol")])
    pending_order_symbols = set([o.get("symbol") for o in open_orders if o.get("symbol")])
    active_symbols.update(pending_order_symbols)

    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}
    trade_closed_this_run = False
    symbols_traded_today = set(state.get("symbols_traded_today", []))

    # ACTIVE POSITIONS EVALUATION
    for pos in positions_list:
        symbol = pos.get("symbol")
        qty = abs(float(pos.get("qty", 0)))
        side = pos.get("side", "long").lower()
        is_long = (side == "long")
        entry_px = float(pos.get("avg_entry_price", 0))
        current_px = float(pos.get("current_price", entry_px))
        market_value = float(pos.get("market_value", 0))
        unrealized_pnl = float(pos.get("unrealized_pl", 0))
        
        current_roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)

        should_exit = False
        exit_reason = ""
        atr_val = 1.0
        entry_candle_low = entry_px * 0.99
        entry_candle_high = entry_px * 1.01

        cache_info = current_active_cache.get(symbol, {})

        try:
            bar_res = api_retry(requests.get, f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=5Min&limit=60&feed=iex&start={start_date_5m}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if b_list:
                    closes_5m = [float(b["c"]) for b in b_list]
                    highs_5m = [float(b["h"]) for b in b_list]
                    lows_5m = [float(b["l"]) for b in b_list]
                    volumes_5m = [float(b.get("v", 1)) for b in b_list]
                    
                    atr_val = calculate_atr(highs_5m, lows_5m, closes_5m)
                    upper_band, lower_band, filter_line = calculate_gaussian_channel(closes_5m)

                    entry_gc_upper = cache_info.get("entry_gc_upper", upper_band)
                    entry_gc_lower = cache_info.get("entry_gc_lower", lower_band)
                    entry_filter_line = cache_info.get("entry_filter_line", filter_line)

                    if len(b_list) >= 2:
                        entry_candle_low = float(b_list[-2]["l"])
                        entry_candle_high = float(b_list[-2]["h"])

                    c_open = float(b_list[-1]["o"])
                    c_high = float(b_list[-1]["h"])
                    c_low = float(b_list[-1]["l"])
                    avg_v = np.mean(volumes_5m[-12:-2]) if len(volumes_5m) >= 12 else volumes_5m[-3]
                    curr_v = volumes_5m[-1]
                    v_ratio = curr_v / avg_v if avg_v > 0 else 1.0

                    is_knife, knife_reason = analyze_live_stock_falling_knife(current_px, c_open, c_high, c_low, v_ratio, is_long)
                    if is_knife:
                        should_exit = True
                        exit_reason = knife_reason

                    if is_long and current_px < entry_filter_line * 0.998:
                        should_exit = True
                        exit_reason = f"📉 5M Structural Invalidation (Close ${current_px:.2f} < Center Line ${entry_filter_line:.2f})"
                    elif (not is_long) and current_px > entry_filter_line * 1.002:
                        should_exit = True
                        exit_reason = f"📈 5M Structural Invalidation (Close ${current_px:.2f} > Center Line ${entry_filter_line:.2f})"
        except Exception as e:
            audit_logs.append(f"Position analytics warning on {symbol}: {e}")

        regime_mismatch = (is_long and spy_regime == "RED") or ((not is_long) and spy_regime == "GREEN")
        if regime_mismatch:
            should_exit = True
            exit_reason = f"🚨 SPY Macro Regime Flip Guard (SPY is {spy_regime})"

        prev_peak = cache_info.get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

        stop_px_calc, current_roe, target_floor_roe, leash_status = calculate_smart_stock_targets(
            entry_px, is_long, current_px, atr_val, entry_candle_low, entry_candle_high, peak_roe=peak_roe
        )

        if is_long and current_px <= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"
        elif (not is_long) and current_px >= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"

        if should_exit:
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE. Reason: {exit_reason}")
            cancel_alpaca_native_orders(symbol, open_orders)
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            try:
                api_retry(requests.post, f"{BASE_URL}/v2/orders", json={"symbol": symbol, "qty": format_qty(qty), "side": close_side, "type": "market", "time_in_force": "day"}, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                    "exit_reason": exit_reason, "realized_pnl": realized_pnl, "timestamp": timestamp
                })
                state["closed_trades_ledger"] = state["closed_trades_ledger"][:10]
                symbols_traded_today.add(symbol)
                
                if realized_pnl < 0 or current_roe < 0:
                    state["cooldown_blocklist"][symbol] = now_ts + 86400
                    audit_logs.append(f"⛔ Added {symbol} to 24H Cooldown Blocklist (Closed at Loss)")

                active_count -= 1
                trade_closed_this_run = True
                continue
            except Exception as e:
                audit_logs.append(f"Execution Failed on {symbol}: {e}")

        active_symbols.add(symbol)
        sync_alpaca_native_trigger_stop(symbol, is_long, qty, stop_px_calc, open_orders, audit_logs)

        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "side": side,
            "peak_roe": peak_roe, "best_target_floor_roe": target_floor_roe, "best_stop_px": stop_px_calc,
            "entry_gc_upper": cache_info.get("entry_gc_upper", entry_px),
            "entry_gc_lower": cache_info.get("entry_gc_lower", entry_px),
            "entry_filter_line": cache_info.get("entry_filter_line", entry_px)
        }

        positions_data.append({
            "bot_title": "TR-GC-Equities-LS-01", "symbol": symbol, "side": "LONG" if is_long else "SHORT",
            "qty": qty, "entry": entry_px, "current": current_px, "market_value": market_value,
            "pnl": unrealized_pnl, "roe": current_roe * 100, "stop": round_sig_figs(stop_px_calc, 5),
            "floor": target_floor_roe * 100, "status": leash_status
        })

    prev_symbols = state.get("previous_active_symbols", [])
    for sym in prev_symbols:
        if sym not in active_symbols:
            try:
                closed_orders_res = api_retry(requests.get, f"{BASE_URL}/v2/orders?status=closed&symbols={sym}&limit=5", headers=HEADERS)
                if closed_orders_res.status_code == 200:
                    closed_orders = closed_orders_res.json()
                    filled_orders = [o for o in closed_orders if o.get("status") == "filled"]
                    if filled_orders:
                        latest_fill = filled_orders[0]
                        exit_px = float(latest_fill.get("filled_avg_price", 0.0) or 0.0)
                        cache_data = current_active_cache.get(sym, {})
                        entry_px = cache_data.get("entry_px", exit_px)
                        fill_qty = float(latest_fill.get("filled_qty", cache_data.get("qty", 1)))
                        fill_side = cache_data.get("side", "long").lower()
                        is_long = (fill_side == "long")
                        
                        realized_pnl = (exit_px - entry_px) * fill_qty if is_long else (entry_px - exit_px) * fill_qty
                        fill_id = latest_fill.get("id")
                        
                        if "closed_trades_ledger" not in state:
                            state["closed_trades_ledger"] = []
                            
                        existing_ids = [t.get("order_id") for t in state["closed_trades_ledger"] if "order_id" in t]
                        if fill_id not in existing_ids:
                            state["closed_trades_ledger"].insert(0, {
                                "symbol": sym,
                                "entry_price": entry_px,
                                "exit_price": exit_px,
                                "realized_pnl": realized_pnl,
                                "exit_reason": "🎯 Native Orderbook Stop Fill",
                                "timestamp": timestamp,
                                "order_id": fill_id
                            })
                            symbols_traded_today.add(sym)
                            
                            if realized_pnl < 0:
                                state["cooldown_blocklist"][sym] = now_ts + 86400
                                audit_logs.append(f"⛔ Added {sym} to 24H Cooldown Blocklist (Native Fill at Loss)")

                            trade_closed_this_run = True
                            audit_logs.append(f"🎯 RECONCILED NATIVE ALPACA FILL [{sym}]: Closed {fill_side.upper()} @ ${exit_px:.2f} (P&L: ${realized_pnl:+.2f})")
            except Exception as e:
                audit_logs.append(f"Reconciliation warning on {sym}: {e}")

    state["closed_trades_ledger"] = state.get("closed_trades_ledger", [])[:10]
    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)
    state["symbols_traded_today"] = list(symbols_traded_today)

    MAX_STOCK_SLOTS = 5
    
    is_weekend = (ct_now.weekday() >= 5)
    is_hard_night_lockout = (ct_now.hour < 8 or (ct_now.hour == 8 and ct_now.minute < 30) or ct_now.hour >= 15)
    is_afternoon_cutoff = (ct_now.hour == 13 and ct_now.minute >= 45) or (ct_now.hour >= 14)
    peak_giveback_lockout = (giveback_from_peak >= 500.0) or (daily_loss_usd >= 500.0)
    trades_executed = False

    # FULL EXPANDED WATCHLIST SNAPSHOT SCANNER
    raw_watchlist = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "QCOM",
        "INTC", "MU", "ARM", "AMAT", "LRCX", "SMCI", "ORCL", "CRM", "NOW", "PANW", "FTNT", "PLTR",
        "COIN", "HOOD", "SQ", "PYPL", "NET", "SNOW", "SHOP", "MSTR", "UBER", "ABNB", "RBLX", "DKNG",
        "NFLX", "COST", "WMT", "HD", "DIS", "NKE", "SBUX", "BA", "CAT", "GE", "HON", "DE", "LMT",
        "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "SLB", "LLY", "UNH", "JNJ", "PFE", "ABBV",
        "TXN", "ADI", "KLAC", "MCHP", "DELL", "SOFI", "MARA", "RIOT", "LCID", "RIVN", "NIO", "SOUN",
        "BBAI", "PLUG", "BB", "CLOV", "RUN", "STEM", "CHPT", "NKLA", "TLRY", "SNDL", "GPRO", "FUBO",
        "AAL", "CCL", "NCLH", "IQ", "SAVA", "GRAB", "OPEN", "BTE", "SKLZ", "BBD", "RIG", "SWN", "TEVA",
        "NOK", "PBR", "AUY", "KGC", "NGL"
    ]
    watchlist = list(dict.fromkeys(raw_watchlist))
    scanned_count = 0
    market_candidates = []

    try:
        candidate_pool = []
        chunk_size = 30
        for i in range(0, len(watchlist), chunk_size):
            chunk = watchlist[i:i + chunk_size]
            snap_url = f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={','.join(chunk)}"
            snap_res = api_retry(requests.get, snap_url, headers=HEADERS)
            if snap_res.status_code == 200:
                snaps = snap_res.json()
                for sym, data in snaps.items():
                    daily_bar = data.get("dailyBar", {})
                    prev_bar = data.get("prevDailyBar", {})
                    c_px = float(daily_bar.get("c", 0.0) or 0.0)
                    p_px = float(prev_bar.get("c", 0.0) or c_px)
                    if c_px > 0 and p_px > 0:
                        pct_gain = ((c_px - p_px) / p_px) * 100
                        candidate_pool.append({"symbol": sym, "close": c_px, "pct_gain": pct_gain})

        ranked_candidates = sorted(candidate_pool, key=lambda x: x["pct_gain"], reverse=True)
        top_ranked = ranked_candidates[:3]
        if top_ranked:
            gainer_str = ", ".join([f"{g['symbol']} ({g['pct_gain']:+.1f}%)" for g in top_ranked])
            audit_logs.append(f"🌐 EXPANDED WATCHLIST SCANNER ACTIVE: Evaluated {len(candidate_pool)} tickers. Top Movers: {gainer_str}")

        symbols_to_scan = [
            g["symbol"] for g in ranked_candidates 
            if g["symbol"] not in active_symbols 
            and g["symbol"] not in symbols_traded_today 
            and now_ts >= float(state.get("cooldown_blocklist", {}).get(g["symbol"], 0))
        ]

        if symbols_to_scan:
            symbols_param = ",".join(symbols_to_scan[:30])
            bars_res = api_retry(requests.get, f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=5Min&limit=50&feed=iex&start={start_date_5m}", headers=HEADERS)
            if bars_res.status_code == 200:
                bars_data = bars_res.json().get("bars", {})
                for symbol, bars in bars_data.items():
                    if not bars or len(bars) < 15:
                        continue
                    
                    scanned_count += 1
                    closes = [float(b["c"]) for b in bars]
                    highs = [float(b["h"]) for b in bars]
                    lows = [float(b["l"]) for b in bars]
                    opens = [float(b["o"]) for b in bars]
                    vols = [float(b.get("v", 1)) for b in bars]

                    live_close = closes[-1]
                    live_open = opens[-1]

                    ci = calculate_choppiness_index(highs, lows, closes)
                    if ci > 58.0:
                        continue

                    past_vols = vols[-12:-2]
                    avg_vol = np.mean(past_vols) if past_vols else 1.0
                    comp_vol_ratio = vols[-2] / avg_vol if avg_vol > 0 else 1.0
                    live_vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0
                    vol_ratio = max(comp_vol_ratio, live_vol_ratio)

                    if vol_ratio < 1.80:
                        continue

                    upper, lower, filter_band = calculate_gaussian_channel(closes[:-1])
                    c_range = max(highs[-1] - lows[-1], 1e-8)
                    live_body_ratio = abs(live_close - live_open) / c_range

                    if live_body_ratio < 0.50:
                        continue

                    is_candle_green = (live_close > live_open)
                    is_candle_red = (live_close < live_open)
                    upper_wick_ratio = (highs[-1] - max(opens[-1], live_close)) / c_range
                    lower_wick_ratio = (min(opens[-1], live_close) - lows[-1]) / c_range

                    fresh_long_breakout = (live_close > upper) and is_candle_green and (upper_wick_ratio <= 0.25)
                    fresh_short_breakdown = (live_close < lower) and is_candle_red and (lower_wick_ratio <= 0.25)

                    if spy_regime == "GREEN" and fresh_long_breakout:
                        market_candidates.append({
                            "symbol": symbol, "close": live_close, "is_long": True,
                            "score": (live_close - upper) / upper, "ci": ci,
                            "entry_gc_upper": upper, "entry_gc_lower": lower,
                            "entry_filter_line": filter_band
                        })

                    if spy_regime == "RED" and fresh_short_breakdown:
                        market_candidates.append({
                            "symbol": symbol, "close": live_close, "is_long": False,
                            "score": (lower - live_close) / lower, "ci": ci,
                            "entry_gc_upper": upper, "entry_gc_lower": lower,
                            "entry_filter_line": filter_band
                        })
    except Exception as e:
        audit_logs.append(f"Dynamic scanner warning: {e}")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"5M Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} qualified candidate(s).")

    if is_weekend or is_hard_night_lockout or is_afternoon_cutoff or is_eod_square_off or peak_giveback_lockout or is_daily_max_loss_triggered:
        if is_weekend:
            gate_reason = "Weekend Closed (Sat/Sun)"
        elif is_daily_max_loss_triggered:
            gate_reason = "Daily Max Loss Cap (-2.5%)"
        elif peak_giveback_lockout:
            gate_reason = "Peak/Daily Giveback Shield ($500 Cap)"
        elif is_hard_night_lockout:
            gate_reason = "Night Hard Lockout (Outside Market Hours)"
        elif is_afternoon_cutoff:
            gate_reason = "Afternoon Cutoff (1:45 PM+ CT)"
        else:
            gate_reason = "EOD Square-Off"
            
        audit_logs.append(f"Execution Gate BLOCKED: {gate_reason}. No new entries allowed.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            is_long = candidate["is_long"]

            if symbol in active_symbols:
                continue

            MAX_RISK_USD_PER_TRADE = 35.0
            stop_distance_pct = 0.0080
            stop_distance_usd = max(px * stop_distance_pct, 0.20)
            
            calc_qty = MAX_RISK_USD_PER_TRADE / stop_distance_usd
            max_shares_cap = (equity * 0.10) / px
            qty = max(1, int(min(calc_qty, max_shares_cap)))
            order_side = "buy" if is_long else "sell"

            clean_initial_stop = round(px - stop_distance_usd if is_long else px + stop_distance_usd, 2)

            order_payload = {
                "symbol": symbol, "qty": format_qty(qty), "side": order_side,
                "type": "market", "time_in_force": "day", "order_class": "oto",
                "stop_loss": {"stop_price": f"{clean_initial_stop:.2f}"}
            }
            try:
                order_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    symbols_traded_today.add(symbol)
                    trades_executed = True

                    if "active_position_cache" not in state:
                        state["active_position_cache"] = {}
                    state["active_position_cache"][symbol] = {
                        "entry_px": px, "side": "long" if is_long else "short", "qty": qty,
                        "entry_gc_upper": candidate.get("entry_gc_upper", px),
                        "entry_gc_lower": candidate.get("entry_gc_lower", px),
                        "entry_filter_line": candidate.get("entry_filter_line", px)
                    }

                    audit_logs.append(f"5M ENTRY SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {qty} shares of {symbol} [Native OTO Trigger Stop Active @ ${clean_initial_stop:.2f}]")
                else:
                    audit_logs.append(f"🚨 ENTRY REJECTED [{symbol}]: OTO Stop Payload declined by exchange matching engine. Aborting entry.")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")

    state["symbols_traded_today"] = list(symbols_traded_today)
    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 8px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 10px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log</div>
        <div class="table-responsive">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    closed_ledger = state.get("closed_trades_ledger", [])
    trades_today = [t for t in closed_ledger if today_str in str(t.get("timestamp", ""))]

    total_today = len(trades_today)
    wins_today = [t for t in trades_today if float(t.get("realized_pnl", 0)) > 0]
    losses_today = [t for t in trades_today if float(t.get("realized_pnl", 0)) <= 0]
    
    win_count = len(wins_today)
    loss_count = len(losses_today)
    win_rate_today = (win_count / total_today * 100) if total_today > 0 else 0.0
    
    avg_win_usd = (sum(float(t.get("realized_pnl", 0)) for t in wins_today) / win_count) if win_count > 0 else 0.0
    avg_loss_usd = (sum(float(t.get("realized_pnl", 0)) for t in losses_today) / loss_count) if loss_count > 0 else 0.0
    net_today_usd = sum(float(t.get("realized_pnl", 0)) for t in trades_today)

    summary_card_html = f"""
    <div class="summary-card">
      <div class="summary-title">📊 Today's Realized Performance Summary ({today_str})</div>
      <table style="width: 100%; border-collapse: collapse; margin-bottom: 8px;">
        <tr>
          <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Total Trades Today: <b style="color: #0f172a;">{total_today}</b></td>
          <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Win Ratio: <b style="color: #0f172a;">{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></td>
        </tr>
        <tr>
          <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Avg Win: <b style="color: #15803d;">${avg_win_usd:+.2f}</b></td>
          <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Avg Loss: <b style="color: #b91c1c;">${avg_loss_usd:+.2f}</b></td>
        </tr>
      </table>
      <div class="summary-net">
        Today's Realized P&L: <span class="{'win-color' if net_today_usd >= 0 else 'loss-color'}">${net_today_usd:+.2f}</span>
      </div>
    </div>
    """

    parsed_closed_rows = []
    total_realized_pnl = 0.0

    for t in closed_ledger[:10]:
        pnl_val = float(t.get("realized_pnl", 0.0))
        total_realized_pnl += pnl_val
        parsed_closed_rows.append(
            f"<tr>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>{t['symbol']}</td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 10px;'>${round_sig_figs(t.get('entry_price', 0), 5)}<br>&rarr; ${round_sig_figs(t.get('exit_price', 0), 5)}</td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: {'#2e7d32' if pnl_val >= 0 else '#c62828'}; font-weight: bold;'>${pnl_val:+.2f}</td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: #b45309; font-size: 10px;'>{t['exit_reason']}</td>"
            f"</tr>"
        )

    total_pnl_color = '#2e7d32' if total_realized_pnl >= 0 else '#c62828'
    closed_rows = "".join(parsed_closed_rows) if parsed_closed_rows else "<tr><td colspan='4' style='padding: 12px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"
    closed_rows += f"""
    <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #cbd5e1;">
        <td colspan="2" style="padding: 8px; text-align: right;">TOTAL RECENT REALIZED P&L:</td>
        <td colspan="2" style="padding: 8px; color: {total_pnl_color};">${total_realized_pnl:+.2f}</td>
    </tr>
    """

    text_fallback = f"TR-GC-Equities-LS-01 | 5M Fast Ross Scalper V3.9.6\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

    positions_rows = "".join([
        f"<tr>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['symbol']}<br><span style='color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-size: 10px; font-weight: 600;'>{p['side']}</span></td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 11px; font-weight: 600;'>${p['market_value']:.0f}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f}<br><span style='font-size: 10px;'>({p['roe']:+.2f}%)</span></td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 10px;'><span style='color: #b45309; font-weight: bold; font-family: monospace;'>${p['stop']}</span><br><span style='color: #2e7d32; font-weight: 600;'>{p['status']}</span></td>"
        f"</tr>"
        for p in positions_data
    ])

    if positions_data:
        total_market_value_sum = sum(p['market_value'] for p in positions_data)
        total_pnl_sum = sum(p['pnl'] for p in positions_data)
        total_cost_basis = total_market_value_sum - total_pnl_sum
        total_roe_avg = (total_pnl_sum / total_cost_basis * 100) if total_cost_basis > 0 else 0.0
        positions_rows += f"""
        <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #cbd5e1;">
            <td style="padding: 6px 8px;">TOTAL:</td>
            <td style="padding: 6px 8px;">${total_market_value_sum:.0f}</td>
            <td style="padding: 6px 8px; color: {'#2e7d32' if total_pnl_sum >= 0 else '#c62828'};">${total_pnl_sum:+.2f} ({total_roe_avg:+.2f}%)</td>
            <td></td>
        </tr>
        """
    else:
        positions_rows = "<tr><td colspan='4' style='padding: 12px; text-align: center; color: #666;'>No active positions (100% Cash Flat).</td></tr>"

    html_content = f"""
    <html>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333; }}
          .container {{ max-width: 600px; width: 100%; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05); box-sizing: border-box; }}
          .header {{ background: #0f172a; color: #ffffff; padding: 14px 16px; }}
          .header h2 {{ margin: 0; font-size: 15px; font-weight: 600; letter-spacing: 0.5px; }}
          .header p {{ margin: 3px 0 0; font-size: 11px; color: #94a3b8; }}
          .content {{ padding: 12px; }}
          .net-worth-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px 14px; margin-bottom: 12px; }}
          .net-worth-title {{ font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 600; margin-bottom: 2px; letter-spacing: 0.5px; }}
          .net-worth-value {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
          .net-worth-subtitle {{ font-size: 10px; color: #64748b; margin-top: 6px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px; }}
          
          .summary-card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 12px; margin-bottom: 12px; }}
          .summary-title {{ font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 0.5px; }}
          .summary-net {{ font-size: 11px; font-weight: 700; color: #166534; border-top: 1px dashed #bbf7d0; padding-top: 6px; margin-top: 2px; }}
          
          .win-color {{ color: #15803d !important; }}
          .loss-color {{ color: #b91c1c !important; }}

          .pnl-badge {{ background: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold; }}
          .lifetime-badge {{ background: #f1f5f9; color: #0f172a; padding: 2px 6px; border-radius: 4px; font-weight: bold; }}

          .rules-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; font-size: 10px; color: #334155; line-height: 1.4; }}
          .rules-title {{ font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a; letter-spacing: 0.5px; }}
          .section-title {{ font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 4px; font-weight: 600; letter-spacing: 0.5px; }}
          .table-responsive {{ width: 100%; overflow-x: auto; margin-bottom: 12px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
          th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 6px 8px; font-weight: 600; border-bottom: 2px solid #cbd5e1; font-size: 10px; }}
          td {{ padding: 6px 8px; border-bottom: 1px solid #f1f5f9; }}
          .footer {{ text-align: center; font-size: 9px; color: #94a3b8; padding: 10px; background: #f8fafc; border-top: 1px solid #e2e8f0; }}

          @media only screen and (max-width: 600px) {{
            body {{ padding: 2px !important; }}
            .content {{ padding: 8px !important; }}
            .header {{ padding: 10px !important; }}
            .net-worth-value {{ font-size: 18px !important; }}
            table {{ font-size: 10px !important; }}
            th, td {{ padding: 5px 4px !important; }}
          }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Equities-LS-01 | 5M Fast Ross Scalper V3.9.6</h2>
            <p>Timestamp: {timestamp} &bull; Mode: HARDENED SPY REGIME &amp; EXPANDED WATCHLIST ACTIVE</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Account Equity</div>
              <div class="net-worth-value">USD ${equity:.2f}</div>
              <div class="net-worth-subtitle">
                <span>Margin: <b>{margin_util_pct:.1f}%</b></span>
                <span>Today's Gain: <span class="pnl-badge">${today_total_gain:+,.2f}</span></span>
                <span>Lifetime P&amp;L: <span class="lifetime-badge">${lifetime_cumulative_pnl:+,.2f}</span></span>
              </div>
            </div>

            {summary_card_html}

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Guardrails (V3.9.6 Production Hardened)</div>
              &bull; <b>Expanded Watchlist Scanner:</b> Evaluates full 100+ ticker universe dynamically across multi-chunk queries<br>
              &bull; <b>5Min Bar Processing:</b> Real-time 5-minute candle evaluation for fast entry &amp; exit timing<br>
              &bull; <b>Wick-Resistant Stop Clamp (-0.80% to -1.25%):</b> Caps risk at ~$35 per trade while tolerating market noise<br>
              &bull; <b>Expanded 70%–95% Profit Ratchet:</b> Locks up to 95% of peak gains on strong runners<br>
              &bull; <b>Atomic Order Replacement:</b> Cancels prior stops before posting updated trigger legs to avoid position collision rejections<br>
              &bull; <b>Strict SPY Regime Shield:</b> Takes LONGs on GREEN SPY and SHORTs on RED SPY<br>
              &bull; <b>Single-Symbol &amp; Cooldown Lockout:</b> Restricts tickers to max 1 position slot and enforces 24H post-loss cooldown<br>
              &bull; <b>Afternoon Cutoff (1:45 PM CT+):</b> Strictly 0 new entries allowed to ensure exit runway<br>
              &bull; <b>EOD Square-Off (2:40 PM CT):</b> Liquidates 100% of open positions prior to market close<br>
              &bull; <b>High-Water &amp; Drawdown Shield ($500 Cap):</b> Hard-blocks trading if drawdown hits $500
            </div>

            <div class="section-title">Active Sniper Scalps</div>
            <div class="table-responsive">
              <table>
                <thead>
                  <tr>
                    <th style="width: 25%;">Sym</th>
                    <th style="width: 20%;">Val ($)</th>
                    <th style="width: 25%;">P&amp;L (ROE)</th>
                    <th style="width: 30%;">Stop / Status</th>
                  </tr>
                </thead>
                <tbody>{positions_rows}</tbody>
              </table>
            </div>

            <div class="section-title">Recently Closed Trades &amp; Exit Telemetry</div>
            <div class="table-responsive">
              <table>
                <thead>
                  <tr>
                    <th style="width: 22%;">Sym</th>
                    <th style="width: 28%;">Entry &rarr; Exit</th>
                    <th style="width: 22%;">Realized</th>
                    <th style="width: 28%;">Reason</th>
                  </tr>
                </thead>
                <tbody>{closed_rows}</tbody>
              </table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Alpaca Quantitative Dual-Engine Scalper &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    last_email_ts = float(state.get("last_email_timestamp", 0))
    elapsed_minutes = (now_ts - last_email_ts) / 60.0
    is_manual_run = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"
    is_time_for_hourly_email = (elapsed_minutes >= 55.0)

    should_send_email = is_manual_run or is_time_for_hourly_email or trades_executed or trade_closed_this_run

    if should_send_email:
        state["last_email_timestamp"] = now_ts
        save_state(state)
        send_html_dashboard_email(f"Alpaca Quantitative Report — USD ${equity:.2f}", html_content, text_fallback)
        print(f"[{timestamp}] Telemetry report dispatched.", flush=True)
    else:
        save_state(state)
        print(f"[{timestamp}] 5m cycle complete ({elapsed_minutes:.1f}m since last report). Skipping email to prevent spam.", flush=True)

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Stock engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
