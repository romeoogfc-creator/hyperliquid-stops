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

API_KEY = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID") or os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or os.getenv("APAL_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL") or os.getenv("ALPACA_BASE_URL") or os.getenv("APAL_BASE_URL", "https://paper-api.alpaca.markets")
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
    """Calculates Texas Central Time (CT) with dynamic Daylight Saving Time offset."""
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    year = utc_now.year
    dst_start = datetime(year, 3, 8)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    
    is_dst = dst_start <= utc_now < dst_end
    offset = -5 if is_dst else -6
    return utc_now + timedelta(hours=offset)

def load_state():
    default_state = {
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_symbols": [],
        "last_run_timestamp": "", 
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
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def check_market_macro_regime():
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
                
                if pct_change >= 0.15:
                    return "GREEN", prev_close, latest_close, pct_change
                elif pct_change <= -0.15:
                    return "RED", prev_close, latest_close, pct_change
                else:
                    return "NEUTRAL", prev_close, latest_close, pct_change
    except Exception as e:
        print(f"Error checking SPY macro regime: {e}", flush=True)
    return "NEUTRAL", 0.0, 0.0, 0.0

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

# ==============================================================================
# NATIVE ALPACA ORDERBOOK TRIGGER STOP MANAGERS
# ==============================================================================
def cancel_alpaca_native_orders(symbol, open_orders):
    """Cancels open resting orders for a symbol on Alpaca."""
    for o in open_orders:
        if o.get("symbol") == symbol:
            try:
                api_retry(requests.delete, f"{BASE_URL}/v2/orders/{o.get('id')}", headers=HEADERS)
            except Exception:
                pass

def sync_alpaca_native_trigger_stop(symbol, is_long, qty, stop_px, open_orders, audit_logs=None):
    """Submits and syncs a native resting stop order on Alpaca's matching engine."""
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
            if abs(existing_stop_px - clean_stop_px) < 0.01:
                needs_update = False
                break
            else:
                try:
                    api_retry(requests.delete, f"{BASE_URL}/v2/orders/{o.get('id')}", headers=HEADERS)
                except Exception:
                    pass

        if needs_update:
            stop_order_payload = {
                "symbol": symbol,
                "qty": clean_qty,
                "side": close_side,
                "type": "stop",
                "stop_price": f"{clean_stop_px:.2f}",
                "time_in_force": "day"
            }
            res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=stop_order_payload, headers=HEADERS)
            if res.status_code == 200 and audit_logs is not None:
                audit_logs.append(f"🛡 NATIVE ALPACA STOP SYNC [{symbol}]: Placed Resting Trigger Stop @ ${clean_stop_px:.2f}")
            elif res.status_code != 200 and audit_logs is not None:
                audit_logs.append(f"⚠️ Alpaca Native Stop Sync Warning [{symbol}] ({res.status_code}): {res.text}")
    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠ Alpaca Native Stop Sync warning on {symbol}: {e}")

# ==============================================================================
# TECHNICAL INDICATORS & SMART PROTECTION ENGINE
# ==============================================================================
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
        ci = 100 * (log10(tr_sum / range_diff) / log10(period))
        return ci
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
    """Analyzes live equity candle structure to detect true dumps vs absorption wicks."""
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
    """
    Computes continuous dynamic stop-loss and uncapped targets for stocks.
    Retains 70%-95% of peak gains while using an ATR Noise Shield to prevent wick-outs.
    """
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    # 1. DOWNSIDE: Dynamic ATR Volatility Buffer (0.50% to 1.20% bounds) & Entry Candle Structural Protection
    atr_roe_buffer = (atr_val * 1.2) / entry_px if entry_px > 0 else 0.008
    atr_roe_buffer = max(0.0050, min(0.0120, atr_roe_buffer))

    if is_long:
        structure_stop = (entry_candle_low / entry_px) - 1.0 if entry_px > 0 else -atr_roe_buffer
        initial_floor_roe = max(-atr_roe_buffer, structure_stop)
    else:
        structure_stop = 1.0 - (entry_candle_high / entry_px) if entry_px > 0 else -atr_roe_buffer
        initial_floor_roe = max(-atr_roe_buffer, structure_stop)

    # 2. UPSIDE: CONTINUOUS DYNAMIC WATERMARK RATCHET (70% -> 95% RETENTION)
    if peak_roe >= 0.0025:  # Activates at +0.25% ROE (~$25 gain on $10k position)
        if peak_roe < 0.0030:
            # +0.25% to +0.30% ROE: Fee Cover Shield
            target_floor_roe = 0.0010
            leash_status = f"🛡 BREAK-EVEN SHIELD [Peak +{peak_roe*100:.2f}% -> Floor +0.10%]"
        else:
            # Smooth Continuous Scaling Curve
            if peak_roe < 0.0100:     # +0.30% to +1.00%: Retention scales 70% -> 85%
                retention = 0.70 + ((peak_roe - 0.003) / 0.007) * 0.15
            elif peak_roe < 0.0300:   # +1.00% to +3.00%: Retention scales 85% -> 92%
                retention = 0.85 + ((peak_roe - 0.010) / 0.020) * 0.07
            else:                     # +3.00%+: Continuous 95% Retention Cap
                retention = 0.95

            target_floor_roe = peak_roe * retention
            leash_status = f"🛡 DYNAMIC {retention*100:.1f}% LOCK [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"⚡ STRUCTURAL STOP ({target_floor_roe*100:.2f}%)"

    # 3. ATR NOISE SHIELD: Enforces minimum breathing room from peak price
    peak_px = entry_px * (1 + peak_roe) if is_long else entry_px * (1 - peak_roe)
    min_atr_distance = atr_val * 0.35  # Enforces at least 0.35 ATR distance from peak

    if is_long:
        calc_stop_px = entry_px * (1 + target_floor_roe)
        max_safe_stop_px = peak_px - min_atr_distance
        if peak_roe > 0.0030 and calc_stop_px > max_safe_stop_px:
            stop_px = max(max_safe_stop_px, entry_px * 1.0010)
        else:
            stop_px = calc_stop_px
    else:
        calc_stop_px = entry_px * (1 - target_floor_roe)
        max_safe_stop_px = peak_px + min_atr_distance
        if peak_roe > 0.0030 and calc_stop_px < max_safe_stop_px:
            stop_px = min(max_safe_stop_px, entry_px * 0.9990)
        else:
            stop_px = calc_stop_px

    return stop_px, roe, target_floor_roe, leash_status

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 Master Engine Started (Texas CT: {ct_now.strftime('%H:%M:%S')}).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing API Key credentials in environment variables.")

    start_date = (datetime.now() - timedelta(days=20)).strftime('%Y-%m-%d')

    spy_regime, spy_open_px, spy_close_px, spy_pct = check_market_macro_regime()
    spy_regime_str = f"{spy_regime} ({spy_pct:+.2f}%) -> {'LONG Tilt' if spy_regime == 'GREEN' else ('SHORT Tilt' if spy_regime == 'RED' else 'NEUTRAL Mode')}"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    state = load_state()

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

    # ANTI-WIPEOUT SHIELD #1: DAILY MAX LOSS CIRCUIT BREAKER (-2.5%)
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
            except Exception as e:
                audit_logs.append(f"Anti-wipeout close failed on {sym}: {e}")
        save_state(state)
        positions_list = []

    # MANDATORY EOD SQUARE-OFF (2:40 PM CT / 3:40 PM ET)
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
            except Exception as e:
                audit_logs.append(f"LIQUIDATION ERROR on {sym}: {e}")

        save_state(state)
        positions_list = []

    active_count = len(positions_list)
    positions_data = []
    active_symbols = set()
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}

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

        try:
            start_date_1h = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            bar_res = api_retry(requests.get, f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=1Hour&limit=50&feed=iex&start={start_date_1h}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if b_list:
                    closes_1h = [float(b["c"]) for b in b_list]
                    highs_1h = [float(b["h"]) for b in b_list]
                    lows_1h = [float(b["l"]) for b in b_list]
                    volumes_1h = [float(b.get("v", 1)) for b in b_list]
                    
                    atr_val = calculate_atr(highs_1h, lows_1h, closes_1h)
                    upper_band, lower_band, filter_line = calculate_gaussian_channel(closes_1h)

                    if len(b_list) >= 2:
                        entry_candle_low = float(b_list[-2]["l"])
                        entry_candle_high = float(b_list[-2]["h"])

                    # Live Candle Falling Knife Detection
                    c_open = float(b_list[-1]["o"])
                    c_high = float(b_list[-1]["h"])
                    c_low = float(b_list[-1]["l"])
                    avg_v = np.mean(volumes_1h[-12:-2]) if len(volumes_1h) >= 12 else volumes_1h[-3]
                    curr_v = volumes_1h[-1]
                    v_ratio = curr_v / avg_v if avg_v > 0 else 1.0

                    is_knife, knife_reason = analyze_live_stock_falling_knife(current_px, c_open, c_high, c_low, v_ratio, is_long)
                    if is_knife:
                        should_exit = True
                        exit_reason = knife_reason

                    if is_long and current_px < upper_band:
                        should_exit = True
                        exit_reason = f"📉 1H Trend Invalidation (Close ${current_px:.2f} < Upper Band ${upper_band:.2f})"
                    elif (not is_long) and current_px > lower_band:
                        should_exit = True
                        exit_reason = f"📈 1H Trend Invalidation (Close ${current_px:.2f} > Lower Band ${lower_band:.2f})"
        except Exception as e:
            audit_logs.append(f"Position analytics warning on {symbol}: {e}")

        regime_mismatch = (is_long and spy_regime == "RED") or ((not is_long) and spy_regime == "GREEN")
        if regime_mismatch:
            should_exit = True
            exit_reason = "🚨 SPY Macro Regime Flip Guard"

        prev_peak = current_active_cache.get(symbol, {}).get("peak_roe", current_roe)
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
                state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]
                active_count -= 1
                continue
            except Exception as e:
                audit_logs.append(f"Execution Failed on {symbol}: {e}")

        # Sync resting native stop directly on Alpaca matching engine
        active_symbols.add(symbol)
        sync_alpaca_native_trigger_stop(symbol, is_long, qty, stop_px_calc, open_orders, audit_logs)

        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "side": side,
            "peak_roe": peak_roe, "best_target_floor_roe": target_floor_roe, "best_stop_px": stop_px_calc
        }

        positions_data.append({
            "bot_title": "TR-GC-Equities-LS-01", "symbol": symbol, "side": "LONG" if is_long else "SHORT",
            "qty": qty, "entry": entry_px, "current": current_px, "market_value": market_value,
            "pnl": unrealized_pnl, "roe": current_roe * 100, "stop": round_sig_figs(stop_px_calc, 5),
            "floor": target_floor_roe * 100, "status": leash_status
        })

    # NATIVE EXCHANGE FILL RECONCILIATION FOR ALPACA
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
                            audit_logs.append(f"🎯 RECONCILED NATIVE ALPACA FILL [{sym}]: Closed {fill_side.upper()} @ ${exit_px:.2f} (P&L: ${realized_pnl:+.2f})")
            except Exception as e:
                audit_logs.append(f"Reconciliation warning on {sym}: {e}")

    state["closed_trades_ledger"] = state.get("closed_trades_ledger", [])[:20]
    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 5
    
    # TIME-WINDOWED ENTRY REGULATION
    is_premarket_lockout = (ct_now.hour < 8) or (ct_now.hour == 8 and ct_now.minute < 30)
    is_opening_15m_window = (ct_now.hour == 8 and 30 <= ct_now.minute < 45)  # 8:30-8:45 AM CT (9:30-9:45 AM ET)
    is_prime_morning_window = (ct_now.hour == 8 and ct_now.minute >= 30) or (9 <= ct_now.hour < 11) or (ct_now.hour == 11 and ct_now.minute <= 30)
    is_midday_window = (ct_now.hour == 11 and ct_now.minute > 30) or (ct_now.hour == 12) or (ct_now.hour == 13 and ct_now.minute < 30)
    is_afternoon_lockout = (ct_now.hour == 13 and ct_now.minute >= 30) or (ct_now.hour >= 14)
    is_post_1030_ct = (ct_now.hour > 10) or (ct_now.hour == 10 and ct_now.minute >= 30)

    # HIGH-WATER & DRAWDOWN LOCKOUT SHIELD ($500 Cap for $98k Account)
    peak_giveback_lockout = (giveback_from_peak >= 500.0) or (daily_loss_usd >= 500.0)
    if peak_giveback_lockout:
        audit_logs.append(f"🛡️ HIGH-WATER SHIELD ACTIVE: Drawdown/Giveback exceeds $500 cap (Loss: ${daily_loss_usd:.2f}, Giveback: ${giveback_from_peak:.2f}). Blocking new trades.")

    watchlist = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "QCOM",
        "INTC", "MU", "ARM", "AMAT", "LRCX", "SMCI", "ORCL", "CRM", "NOW", "PANW", "FTNT", "PLTR",
        "COIN", "HOOD", "SQ", "PYPL", "NET", "SNOW", "SHOP", "MSTR", "UBER", "ABNB", "RBLX", "DKNG",
        "NFLX", "COST", "WMT", "HD", "DIS", "NKE", "SBUX", "BA", "CAT", "GE", "HON", "DE", "LMT",
        "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "SLB", "LLY", "UNH", "JNJ", "PFE", "ABBV",
        "TXN", "ADI", "KLAC", "MCHP", "DELL", "SPY", "QQQ", "IWM", "DIA", "SMH", "XLF", "XLE"
    ]

    symbols_to_scan = [s for s in watchlist if s not in active_symbols]
    market_candidates = []
    scanned_count = 0

    chunk_size = 20
    for i in range(0, len(symbols_to_scan), chunk_size):
        chunk = symbols_to_scan[i:i + chunk_size]
        symbols_param = ",".join(chunk)

        all_bars_data = {}
        page_token = None

        while True:
            url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=1Hour&limit=10000&feed=iex&start={start_date}"
            if page_token:
                url += f"&page_token={page_token}"

            try:
                bars_res = api_retry(requests.get, url, headers=HEADERS)
                if bars_res.status_code != 200:
                    break
                res_json = bars_res.json()
                returned_bars = res_json.get("bars", {})
                for sym, b_list in returned_bars.items():
                    if sym not in all_bars_data:
                        all_bars_data[sym] = []
                    all_bars_data[sym].extend(b_list)
                page_token = res_json.get("next_page_token")
                if not page_token:
                    break
            except Exception:
                break

        for symbol in chunk:
            bars = all_bars_data.get(symbol, [])
            if not bars or len(bars) < 15:
                continue

            scanned_count += 1
            closes = [float(b["c"]) for b in bars]
            highs = [float(b["h"]) for b in bars]
            lows = [float(b["l"]) for b in bars]
            opens = [float(b["o"]) for b in bars]

            ci = calculate_choppiness_index(highs, lows, closes)
            if ci > 58.0:
                continue

            volume_series = [float(b.get("v", 1)) for b in bars[-11:-1]]
            avg_vol = np.mean(volume_series) if volume_series else 1.0
            latest_vol = float(bars[-1].get("v", 0))
            vol_ratio = latest_vol / avg_vol if avg_vol > 0 else 1.0

            # STRICT OPENING 15-MINUTE VOLUME GATE (8:30-8:45 AM CT / 9:30-9:45 AM ET)
            if is_opening_15m_window and vol_ratio < 1.80:
                continue

            if is_post_1030_ct and not is_opening_15m_window:
                if vol_ratio < 1.40:
                    continue

            upper, lower, filter_band = calculate_gaussian_channel(closes[:-1])
            comp_close = closes[-2]
            prev_close = closes[-3]
            live_close = closes[-1]
            live_open = opens[-1]

            c_range = max(highs[-1] - lows[-1], 1e-8)
            live_body_ratio = abs(live_close - live_open) / c_range

            # STRICT OPENING 15-MINUTE CANDLE BODY GATE (Solid body >= 65% of range to filter fakeouts)
            if is_opening_15m_window and live_body_ratio < 0.65:
                continue

            is_candle_green = (live_close > live_open) and (live_close >= comp_close)
            is_candle_red = (live_close < live_open) and (live_close <= comp_close)

            if live_close > upper * 1.015 or live_close < lower * 0.985:
                continue

            candidate_obj = None
            if spy_regime in ["GREEN", "NEUTRAL"]:
                if comp_close > upper and prev_close <= upper and is_candle_green:
                    candidate_obj = {
                        "symbol": symbol, "close": live_close, "is_long": True,
                        "score": (live_close - upper) / upper, "ci": ci
                    }

            if spy_regime in ["RED", "NEUTRAL"] and candidate_obj is None:
                if comp_close < lower and prev_close >= lower and is_candle_red:
                    candidate_obj = {
                        "symbol": symbol, "close": live_close, "is_long": False,
                        "score": (lower - live_close) / lower, "ci": ci
                    }

            if candidate_obj:
                market_candidates.append(candidate_obj)

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"1H Adaptive Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated triggers.")

    if is_premarket_lockout or is_afternoon_lockout or is_eod_square_off or peak_giveback_lockout or is_daily_max_loss_triggered:
        gate_reason = (
            "Daily Max Loss Cap (-2.5%)" if is_daily_max_loss_triggered else (
            "Peak/Daily Giveback Shield ($500 Cap)" if peak_giveback_lockout else (
            "Premarket Lockout (Before 8:30 AM CT)" if is_premarket_lockout else (
            "Afternoon Cutoff (1:30 PM+ CT)" if is_afternoon_lockout else "EOD Square-Off"
            )))
        )
        audit_logs.append(f"Execution Gate BLOCKED: {gate_reason}. No new entries allowed.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            cand_ci = candidate.get("ci", 50.0)

            if is_prime_morning_window:
                if cand_ci > 52.0 or spy_regime == "NEUTRAL":
                    target_usd = 1000.0
                    audit_logs.append(f"MORNING MICRO SIZING for {symbol}: ${target_usd} (Chop/Neutral)")
                else:
                    target_usd = max(1000.0, equity * 0.10)
                    audit_logs.append(f"⚡ MORNING POWER SIZING for {symbol}: ${target_usd:.2f} (10% NAV)")
            elif is_midday_window:
                target_usd = 1000.0
                audit_logs.append(f"🛡️ MIDDAY MICRO CAPPED SIZING for {symbol}: ${target_usd}")

            raw_qty = target_usd / px
            qty = max(1, int(raw_qty))
            order_side = "buy" if is_long else "sell"

            initial_stop_px = px * 0.9920 if is_long else px * 1.0080
            clean_initial_stop = round(initial_stop_px, 2)

            order_payload = {
                "symbol": symbol,
                "qty": format_qty(qty),
                "side": order_side,
                "type": "market",
                "time_in_force": "day",
                "order_class": "oto",
                "stop_loss": {
                    "stop_price": f"{clean_initial_stop:.2f}"
                }
            }
            try:
                order_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    audit_logs.append(f"1H ENTRY SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {qty} shares of {symbol} (~${(qty * px):.2f}) [Native OTO Trigger Stop Active @ ${clean_initial_stop:.2f}]")
                else:
                    fallback_payload = {
                        "symbol": symbol, "qty": format_qty(qty), "side": order_side, "type": "market", "time_in_force": "day"
                    }
                    fallback_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=fallback_payload, headers=HEADERS)
                    if fallback_res.status_code == 200:
                        active_count += 1
                        active_symbols.add(symbol)
                        audit_logs.append(f"1H ENTRY SUCCESS (Standard): Opened {'LONG' if is_long else 'SHORT'} on {qty} shares of {symbol} (~${(qty * px):.2f}) [Stop will sync on next run]")
                    else:
                        audit_logs.append(f"ORDER REJECTED BY ALPACA [{order_res.status_code}] on {symbol}: {order_res.text}")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/{MAX_STOCK_SLOTS}). No new entries triggered.")

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
    trades_today = [
        t for t in closed_ledger 
        if today_str in str(t.get("timestamp", ""))
    ]

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

    for t in closed_ledger[:5]:
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

    text_fallback = f"TR-GC-Equities-LS-01 | 1H Adaptive Engine V3\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

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
            <h2>TR-GC-Equities-LS-01 | 1H Master Engine V3</h2>
            <p>Timestamp: {timestamp} &bull; Mode: TIME-REGULATED POWER & SMART PROTECTION</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Account Equity</div>
              <div class="net-worth-value">USD ${equity:.2f}</div>
              <div class="net-worth-subtitle">
                <span>Margin: <b>{margin_util_pct:.1f}%</b></span>
                <span>Today's Gain: <span class="pnl-badge">${today_total_gain:+,.2f}</span></span>
                <span>Lifetime P&L: <span class="lifetime-badge">${lifetime_cumulative_pnl:+,.2f}</span></span>
              </div>
            </div>

            {summary_card_html}

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Guardrails (Full Strategy Display)</div>
              &bull; <b>Live Stock Anatomy Falling-Knife Detector:</b> Distinguishes absorption wicks from solid dumps (&gt;60% body, &gt;1.4x vol)<br>
              &bull; <b>Opening 15m Gated Window (8:30–8:45 AM CT / 9:30–9:45 AM ET):</b> Active with strict &gt;=1.80x volume surge &amp; &gt;=65% body gate<br>
              &bull; <b>Dynamic ATR Volatility Buffer:</b> Replaces flat -0.35% cap with 1.2x ATR breathing room<br>
              &bull; <b>Continuous Dynamic High-Watermark Ratchet (70%–95% Lock):</b> Smoothly ratchets profit floor from +0.30% to +3.00%+ ROE with ATR Noise Shield<br>
              &bull; <b>Native Alpaca Orderbook Trigger Stops:</b> Resting stop orders placed directly on Alpaca matching engine<br>
              &bull; <b>1-Hour Timeframe & Hard CI Gate (&le;58.0):</b> Eliminates noise & rejects choppy stocks<br>
              &bull; <b>SPY Macro Regime Shield:</b> Enforces broad market direction alignment<br>
              &bull; <b>Premarket Lockout (Before 8:30 AM CT / 9:30 AM ET):</b> Strictly blocks premarket entries<br>
              &bull; <b>Morning Power Window (8:30–11:30 AM CT):</b> Full 10% NAV (~$10k) sizing on clean completed trends<br>
              &bull; <b>Post-10:30 AM CT Volume & Expansion Filter:</b> Enforces &gt;1.4x Volume surge to enter late morning trades<br>
              &bull; <b>Afternoon Lockout (1:30 PM CT+):</b> Strictly 0 new entries allowed<br>
              &bull; <b>EOD Square-Off (2:40 PM CT / 3:40 PM ET):</b> Liquidates 100% of open positions prior to market close<br>
              &bull; <b>High-Water & Drawdown Shield ($500 Cap):</b> Hard-blocks trading if drawdown or giveback hits $500<br>
              &bull; <b>Daily Anti-Wipeout Shield (-2.5% Cap):</b> Emergency flattens account if daily loss hits -2.5%
            </div>

            <div class="section-title">Active Sniper Scalps</div>
            <div class="table-responsive">
              <table>
                <thead>
                  <tr>
                    <th style="width: 25%;">Sym</th>
                    <th style="width: 20%;">Val ($)</th>
                    <th style="width: 25%;">P&L (ROE)</th>
                    <th style="width: 30%;">Stop / Status</th>
                  </tr>
                </thead>
                <tbody>{positions_rows}</tbody>
              </table>
            </div>

            <div class="section-title">Recently Closed Trades & Exit Telemetry</div>
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
          <div class="footer">Alpaca Pure Quantitative Sniper Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Alpaca Quantitative Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] 1H Master Engine V3 report complete.", flush=True)

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Stock engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
