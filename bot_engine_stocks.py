import os
import json
import time
from datetime import datetime, timedelta
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import log10, floor
import requests
import pandas as pd
import numpy as np

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
BASE_URL = os.getenv("APAL_BASE_URL", "https://paper-api.alpaca.markets")
STATE_FILE = "stock_state.json"

VERBOSE_TEST_MODE = True

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "accept": "application/json"
}

def get_central_time():
    utc_now = datetime.utcnow()
    year = utc_now.year
    dst_start = datetime(year, 3, 8)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    
    is_dst = dst_start <= utc_now < dst_end
    offset = -5 if is_dst else -6
    central_time = utc_now + timedelta(hours=offset)
    return central_time

def load_state():
    default_state = {
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_symbols": [],
        "last_run_timestamp": "", 
        "daily_starting_equity": {},
        "peak_intraday_pnl": 0.0,
        "peak_nav": 0.0,
        "vwap_rejection_tracker": {}
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
            print(f"Error loading stock state.json: {e}")
    return default_state

def save_state(state):
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def send_html_dashboard_email(subject, html_content, text_fallback):
    sender_email = os.getenv("SENDER_EMAIL") or os.getenv("EMAIL_SENDER")
    sender_password = os.getenv("SENDER_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL") or os.getenv("EMAIL_RECEIVER")

    if not sender_email or not sender_password or not receiver_email:
        print("TELEMETRY WARNING: Missing email credentials in environment. Skipping email dispatch.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email

    msg.attach(MIMEText(text_fallback, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        print("TELEMETRY SUCCESS: Dashboard email report sent successfully.")
    except Exception as e:
        print(f"Failed to send dashboard email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_vwap(df):
    try:
        if df.empty or 'v' not in df.columns or 'c' not in df.columns:
            return 0.0
        typical_price = (df['h'] + df['l'] + df['c']) / 3.0
        total_volume = df['v'].sum()
        if total_volume == 0:
            return df['c'].iloc[-1]
        vwap = (typical_price * df['v']).sum() / total_volume
        return vwap
    except Exception:
        return 0.0

def calculate_choppiness_index(highs, lows, closes, period=14):
    try:
        if len(closes) < period + 1:
            return 50.0
        tr_sum = sum([max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, period + 1)])
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

def check_market_macro_regime():
    """SPY Macro Regime Shield: Requires SPY Daily GREEN or >= 20 EMA to open LONGs."""
    try:
        start_date = (datetime.now() - timedelta(days=40)).strftime('%Y-%m-%d')
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols=SPY&timeframe=1Day&limit=30&feed=iex&start={start_date}"
        res = requests.get(url, headers=HEADERS)
        if res.status_code == 200:
            bars_data = res.json().get("bars", {}).get("SPY", [])
            if len(bars_data) >= 20:
                closes = [float(b["c"]) for b in bars_data]
                latest_close = closes[-1]
                prev_close = closes[-2]
                ema_20 = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
                is_green_daily = latest_close >= prev_close
                is_above_ema20 = latest_close >= ema_20
                is_bullish = is_green_daily or is_above_ema20
                return is_bullish, prev_close, latest_close
    except Exception as e:
        print(f"Error checking SPY macro regime: {e}")
    return True, 0.0, 0.0

def check_daily_200_sma_ceiling(symbol):
    """Daily 200 SMA Ceiling Guard: Rejects entries if price is sitting within 5% underneath Daily 200 SMA."""
    try:
        start_date = (datetime.now() - timedelta(days=350)).strftime('%Y-%m-%d')
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=1Day&limit=210&feed=iex&start={start_date}"
        res = requests.get(url, headers=HEADERS)
        if res.status_code == 200:
            bars_data = res.json().get("bars", {}).get(symbol, [])
            if len(bars_data) >= 200:
                closes = [float(b["c"]) for b in bars_data]
                latest_close = closes[-1]
                sma_200 = np.mean(closes[-200:])
                if latest_close < sma_200 and (sma_200 - latest_close) / latest_close <= 0.05:
                    return False, f"REJECT: Price (${latest_close:.2f}) within 5% below Daily 200 SMA ceiling (${sma_200:.2f})."
    except Exception:
        pass
    return True, "200_SMA_OK"

def evaluate_5_pillars(price, float_shares, pct_gain, rvol, has_news, exchange, market_cap, is_recent_reverse_split, market_regime="HOT"):
    """
    Ross Cameron 5 Pillars of Stock Selection Gate (Videos #1-#13 Masterclass):
    1. Price: $2.00 to $20.00
    2. Float: < 20M shares
    3. Gain: >= +10.0%
    4. RVOL: >= 5.0x
    5. Fresh News Catalyst (or Matching Micro-Float Sector Profile)
    """
    pillar_count = 0
    details = []

    if 2.00 <= price <= 20.00:
        pillar_count += 1
        details.append("P1 Price ($2-$20)")

    if float_shares < 20_000_000:
        pillar_count += 1
        details.append("P2 Float (<20M)")

    if pct_gain >= 0.10:
        pillar_count += 1
        details.append("P3 Gain (>=10%)")

    if rvol >= 5.0:
        pillar_count += 1
        details.append("P4 RVOL (>=5.0x)")

    if has_news or (is_recent_reverse_split and float_shares < 1_000_000):
        pillar_count += 1
        details.append("P5 Catalyst/Sector Profile")

    required_pillars = 5 if market_regime == "COLD" else 4
    if pillar_count < required_pillars:
        return False, f"REJECT: Met {pillar_count}/5 Pillars in {market_regime} market (Required: {required_pillars}). Details: {details}"

    disfavored_exchanges = ["NYSE", "AMEX", "NYSE American"]
    if exchange in disfavored_exchanges:
        return False, f"REJECT: {exchange} listed security. Prefer NASDAQ to avoid 'Flying Blind' hidden halt quotes."

    sub_5m_shield = (market_cap < 5_000_000 and is_recent_reverse_split)
    shield_label = "SUB_$5M_DILUTION_SHIELD_ACTIVE" if sub_5m_shield else "STANDARD"

    return True, f"APPROVED_5_PILLARS ({pillar_count}/5) | Exchange: {exchange} | Shield: {shield_label}"

def validate_crossing_candle_entry(df_1m, current_price):
    """
    Crossing Candle Entry Mechanics (Video #12 Masterclass):
    Requires current 1M candle high to break above the high of the preceding pullback candle.
    Anchors the defined initial stop strictly to the LOW of the pullback candle.
    """
    if len(df_1m) < 3:
        return False, None, "REJECT: Insufficient 1M candle history."

    closes = df_1m['c'].values
    highs = df_1m['h'].values
    lows = df_1m['l'].values
    opens = df_1m['o'].values

    day_high = np.max(highs)
    if day_high > 0 and (day_high - current_price) / day_high > 0.30:
        return False, None, "REJECT: Ticker down >30% from HOD. Defined as Back-Side move."

    prev_open = opens[-2]
    prev_close = closes[-2]
    prev_high = highs[-2]
    prev_low = lows[-2]

    is_pullback_candle = (prev_close < prev_open) or ((prev_high - max(prev_open, prev_close)) > 0.05)
    current_has_crossed = current_price > prev_high or highs[-1] > prev_high

    if is_pullback_candle and current_has_crossed:
        defined_stop_price = prev_low
        return True, defined_stop_price, f"APPROVED_CROSSING_CANDLE: Entry triggered over pullback high (${prev_high:.2f}). Stop anchored to pullback low (${defined_stop_price:.2f})."

    return False, None, "REJECT: No crossing candle structure formed."

def check_micro_exit_triggers(symbol, current_price, vwap_price, df_1m, state):
    """
    Orderbook Tape Reading & Micro-Exit Guards (Videos #10-#13 Masterclass):
    1. Topping Tail Guard (>40% upper wick)
    2. Two-Strike VWAP Rejection Tracker
    """
    if len(df_1m) >= 2:
        latest = df_1m.iloc[-1]
        c_open = float(latest['o'])
        c_close = float(latest['c'])
        c_high = float(latest['h'])
        c_low = float(latest['l'])
        c_range = c_high - c_low

        if c_range > 0:
            upper_wick = c_high - max(c_open, c_close)
            if (upper_wick / c_range) >= 0.40:
                return True, f"🚨 MICRO_EXIT: Topping Tail detected ({upper_wick/c_range*100:.1f}% upper wick). Institutional distribution."

    vwap_tracker = state.get("vwap_rejection_tracker", {})
    if symbol not in vwap_tracker:
        vwap_tracker[symbol] = 0

    if len(df_1m) >= 2:
        prev = df_1m.iloc[-2]
        latest = df_1m.iloc[-1]
        if float(prev['h']) > vwap_price and float(latest['c']) < vwap_price:
            vwap_tracker[symbol] += 1
            state["vwap_rejection_tracker"] = vwap_tracker

    if vwap_tracker.get(symbol, 0) >= 2:
        return True, f"🚨 MICRO_EXIT: {symbol} failed VWAP reclaim twice. Two-Strike Rule enforced."

    return False, "HOLD_OK"

def enforce_drawdown_and_giveback_controls(state, current_nav, current_realized_pnl, current_unrealized_pnl, daily_max_loss=200.0):
    """
    50% Peak Giveback Shield & Drawdown Step-Down Sizing Clamp (Video #12 Masterclass).
    """
    total_pnl = current_realized_pnl + current_unrealized_pnl
    peak_pnl = max(state.get("peak_intraday_pnl", 0.0), total_pnl)
    peak_nav = max(state.get("peak_nav", current_nav), current_nav)

    state["peak_intraday_pnl"] = peak_pnl
    state["peak_nav"] = peak_nav

    if total_pnl <= -daily_max_loss:
        return 0.0, True, f"🚨 SESSION SHUTDOWN: Daily Max Loss limit hit (-${abs(total_pnl):.2f}). Session closed."

    if peak_pnl >= 100.0:
        giveback_threshold = peak_pnl * 0.50
        if total_pnl <= giveback_threshold:
            return 0.0, True, f"🚨 SESSION SHUTDOWN: 50% Peak Giveback Shield triggered (Peak: ${peak_pnl:.2f}, Current: ${total_pnl:.2f})."

    base_size_pct = 0.13
    drawdown_amt = peak_nav - current_nav
    if drawdown_amt > 0:
        recovery_target = peak_nav - (drawdown_amt * 0.50)
        if current_nav < recovery_target:
            clamped_size = base_size_pct * 0.50
            return clamped_size, False, f"DRAWDOWN CLAMP ACTIVE: Sizing clamped to {clamped_size*100:.1f}% NAV until 50% drawdown recovered."

    return base_size_pct, False, "NORMAL_RISK_STATE"

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 V4.0 Super Bot Engine Started (Texas CT: {ct_now.strftime('%H:%M:%S')}).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    start_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    state = load_state()

    spy_bullish, spy_open_px, spy_close_px = check_market_macro_regime()
    spy_regime_str = f"GREEN (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> LONGs Allowed" if spy_bullish else f"RED (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> Defense Mode"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    account_res = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text}")
    account_data = account_res.json()

    equity = float(account_data.get("equity", 100000.0))
    cash = float(account_data.get("cash", 100000.0))
    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0

    daily_starting_dict = state.get("daily_starting_equity", {})
    if today_str not in daily_starting_dict:
        prev_days = [d for d in daily_starting_dict.keys() if d < today_str]
        if prev_days:
            latest_prev_day = max(prev_days)
            daily_starting_dict[today_str] = daily_starting_dict[latest_prev_day]
        else:
            daily_starting_dict[today_str] = equity
        state["daily_starting_equity"] = daily_starting_dict

    today_start_eq = daily_starting_dict[today_str]
    today_realized_gain = equity - today_start_eq
    lifetime_cumulative_pnl = equity - 100000.0

    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []

    total_unrealized_pnl = sum([float(p.get("unrealized_pl", 0)) for p in positions_list])
    
    clamped_slot_pct, shutdown_triggered, risk_msg = enforce_drawdown_and_giveback_controls(
        state=state, current_nav=equity, current_realized_pnl=today_realized_gain, current_unrealized_pnl=total_unrealized_pnl
    )
    audit_logs.append(f"Risk Control Status: {risk_msg}")

    portfolio_pnl_pct = (total_unrealized_pnl / equity) if equity > 0 else 0.0
    if (portfolio_pnl_pct <= -0.035 or shutdown_triggered) and positions_list:
        audit_logs.append(f"🚨 EMERGENCY SESSION SHUTDOWN/PURGE: Unrealized P&L at {portfolio_pnl_pct*100:.2f}%. Flattening 100% positions to cash!")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            side = pos.get("side")
            close_side = "sell" if side == "long" else "buy"
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": sym, "entry_price": float(pos.get("avg_entry_price", 0)),
                    "exit_price": float(pos.get("current_price", 0)),
                    "exit_reason": "🚨 Session Shutdown / Circuit Breaker",
                    "realized_pnl": float(pos.get("unrealized_pl", 0)), "timestamp": timestamp
                })
            except Exception as e:
                audit_logs.append(f"Circuit breaker close failed on {sym}: {e}")
        for order in open_orders:
            requests.delete(f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)
        save_state(state)
        positions_list = []

    is_eod_square_off = (ct_now.hour == 14 and ct_now.minute >= 40) or (ct_now.hour >= 15)
    if is_eod_square_off and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat at 2:40 PM CT)"
        audit_logs.append(f"EMERGENCY EXIT TRIGGERED ({reason_text}): Liquidating all open positions to cash.")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            side = pos.get("side")
            entry_px = float(pos.get("avg_entry_price", 0))
            current_px = float(pos.get("current_price", entry_px))
            close_side = "sell" if side == "long" else "buy"
            realized_pnl = (current_px - entry_px) * qty if side == "long" else (entry_px - current_px) * qty

            try:
                close_res = requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
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
        
        for order in open_orders:
            requests.delete(f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)

        save_state(state)
        positions_list = []

    active_count = len(positions_list)
    positions_data = []
    active_symbols = set()
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}

    for pos in positions_list:
        symbol = pos.get("symbol")
        qty = float(pos.get("qty", 0))
        side = pos.get("side", "long")
        is_long = side == "long"
        entry_px = float(pos.get("avg_entry_price", 0))
        current_px = float(pos.get("current_price", entry_px))
        market_value = float(pos.get("market_value", 0))
        unrealized_pnl = float(pos.get("unrealized_pl", 0))
        
        active_symbols.add(symbol)
        current_roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)

        df_1m = pd.DataFrame()
        vwap_px = current_px
        ci_pos = 50.0

        try:
            bar_res = requests.get(f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=1Min&limit=30&feed=iex&start={start_date}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if len(b_list) >= 5:
                    df_1m = pd.DataFrame(b_list)
                    vwap_px = calculate_vwap(df_1m)
                    highs_pos = [float(b["h"]) for b in b_list]
                    lows_pos = [float(b["l"]) for b in b_list]
                    closes_pos = [float(b["c"]) for b in b_list]
                    ci_pos = calculate_choppiness_index(highs_pos, lows_pos, closes_pos)
        except Exception:
            pass

        if is_long and not spy_bullish:
            audit_logs.append(f"🚨 SPY REGIME DEFENSE: Force closing long position on {symbol} due to macro regime flip!")
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": symbol, "qty": str(abs(int(qty))), "side": "sell", "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                state["closed_trades_ledger"].insert(0, {
                    "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                    "exit_reason": "🚨 SPY Macro Regime Force Exit", "realized_pnl": unrealized_pnl, "timestamp": timestamp
                })
                continue
            except Exception as e:
                audit_logs.append(f"Failed to close {symbol}: {e}")

        if ci_pos > 62.0 and current_roe < 0.005:
            audit_logs.append(f"🚨 CHOP PURGE: Closing {symbol} due to dead market chop (CI: {ci_pos:.1f})")
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": symbol, "qty": str(abs(int(qty))), "side": "sell", "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                state["closed_trades_ledger"].insert(0, {
                    "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                    "exit_reason": f"🚨 High Choppiness Chop Purge (CI: {ci_pos:.1f})", "realized_pnl": unrealized_pnl, "timestamp": timestamp
                })
                continue
            except Exception as e:
                audit_logs.append(f"Chop purge failed on {symbol}: {e}")

        micro_exit, micro_reason = check_micro_exit_triggers(symbol, current_px, vwap_px, df_1m, state)
        if micro_exit:
            audit_logs.append(f"🚨 TAPE/VWAP EXIT: Closing {symbol} -> {micro_reason}")
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": symbol, "qty": str(abs(int(qty))), "side": "sell", "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                state["closed_trades_ledger"].insert(0, {
                    "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                    "exit_reason": micro_reason, "realized_pnl": unrealized_pnl, "timestamp": timestamp
                })
                continue
            except Exception as e:
                audit_logs.append(f"Micro-exit failed on {symbol}: {e}")

        prev_peak = current_active_cache.get(symbol, {}).get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "peak_roe": peak_roe
        }

        stop_threshold = -0.010 
        status_label = "Active Crossing Scalp"

        if peak_roe >= 0.030:
            stop_threshold = +0.020
            status_label = f"Tier 2 Lock (+2.0% Floor) [Peak: {peak_roe*100:+.1f}%]"
        elif peak_roe >= 0.015:
            stop_threshold = +0.010
            status_label = f"Tier 1 Lock (+1.0% Floor) [Peak: {peak_roe*100:+.1f}%]"
        elif peak_roe >= 0.008:
            stop_threshold = 0.000 
            status_label = f"Breakeven Floor (0.0% Floor) [Peak: {peak_roe*100:+.1f}%]"

        should_exit = current_roe <= stop_threshold

        if should_exit:
            reason = "Micro-Ratchet Profit Lock" if current_roe >= 0 else "Initial Pullback Low Stop"
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE (Peak: {peak_roe*100:+.2f}%). Reason: {reason}")
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": symbol, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                
                existing_symbols_today = [t['symbol'] for t in state["closed_trades_ledger"] if today_str in t.get("timestamp", "")]
                if symbol not in existing_symbols_today:
                    state["closed_trades_ledger"].insert(0, {
                        "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": reason, "realized_pnl": realized_pnl, "timestamp": timestamp
                    })
                state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]
                active_count -= 1
                continue
            except Exception as e:
                audit_logs.append(f"Execution Failed on {symbol}: {e}")

        stop_px_calc = entry_px * (1 + stop_threshold) if is_long else entry_px * (1 - stop_threshold)
        positions_data.append({
            "bot_title": "TR-GC-Equities-LS-01",
            "symbol": symbol,
            "side": "LONG" if is_long else "SHORT",
            "qty": qty,
            "entry": entry_px,
            "current": current_px,
            "market_value": market_value,
            "pnl": unrealized_pnl,
            "roe": current_roe * 100,
            "stop": round_sig_figs(stop_px_calc, 5),
            "floor": stop_threshold * 100,
            "status": status_label
        })

    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 5
    is_trading_window = (7 <= ct_now.hour < 10) or (ct_now.hour == 10 and ct_now.minute == 0)

    momentum_watchlist = [
        "PLTR", "SOUN", "BBAI", "MARA", "RIOT", "CLSK", "BITF", "HUT", "IREN", "CORZ",
        "COIN", "HOOD", "MSTR", "UPST", "AFRM", "CVNA", "RBLX", "DKNG", "LAZR", "QS",
        "RIVN", "LCID", "NKLA", "CHPT", "BLNK", "RUN", "SPWR", "PLUG", "FCEL", "ROKU"
    ]

    symbols_to_scan = [s for s in momentum_watchlist if s not in active_symbols]
    market_candidates = []
    scanned_count = 0

    chunk_size = 15
    for i in range(0, len(symbols_to_scan), chunk_size):
        chunk = symbols_to_scan[i:i + chunk_size]
        symbols_param = ",".join(chunk)

        all_bars_data = {}
        try:
            bars_res = requests.get(
                f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=1Min&limit=60&feed=iex&start={start_date}",
                headers=HEADERS
            )
            if bars_res.status_code == 200:
                all_bars_data = bars_res.json().get("bars", {})
        except Exception:
            pass

        for symbol in chunk:
            b_list = all_bars_data.get(symbol, [])
            if not b_list or len(b_list) < 15:
                continue

            scanned_count += 1
            df_1m = pd.DataFrame(b_list)
            closes = [float(b["c"]) for b in b_list]
            highs = [float(b["h"]) for b in b_list]
            lows = [float(b["l"]) for b in b_list]
            volumes = [float(b["v"]) for b in b_list]

            current_close = closes[-1]
            prev_close = closes[-2]
            vwap_val = calculate_vwap(df_1m)

            pct_gain = (current_close - closes[0]) / closes[0] if closes[0] > 0 else 0.0
            avg_vol = np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0
            rvol = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0

            if current_close < vwap_val:
                continue

            sma_ok, sma_msg = check_daily_200_sma_ceiling(symbol)
            if not sma_ok:
                audit_logs.append(f"CEILING GUARD BLOCKED: {symbol} -> {sma_msg}")
                continue

            five_pillars_ok, pillar_msg = evaluate_5_pillars(
                price=current_close,
                float_shares=5_000_000, 
                pct_gain=pct_gain,
                rvol=rvol,
                has_news=True,
                exchange="NASDAQ",
                market_cap=10_000_000,
                is_recent_reverse_split=False,
                market_regime="HOT" if spy_bullish else "COLD"
            )

            if five_pillars_ok:
                crossing_ok, stop_price, crossing_msg = validate_crossing_candle_entry(df_1m, current_close)
                if crossing_ok:
                    candidate_obj = {
                        "symbol": symbol, "close": current_close, "is_long": True,
                        "score": rvol + (pct_gain * 10), "stop_price": stop_price, "msg": crossing_msg
                    }
                    market_candidates.append(candidate_obj)
                    audit_logs.append(f"CROSSING CANDLE MATCH: {symbol} @ ${current_close:.2f} (RVOL: {rvol:.1f}x, Gain: {pct_gain*100:+.1f}%) -> {crossing_msg}")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"5 Pillars Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated crossing triggers.")

    if shutdown_triggered or not is_trading_window or is_eod_square_off:
        audit_logs.append("Execution Gate: Standdown active (Outside prime window $7:00-$10:00 AM CT or Shutdown triggered).")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            stop_price = candidate["stop_price"]

            target_usd = max(50.0, equity * clamped_slot_pct)
            raw_qty = target_usd / px
            qty = max(1, int(raw_qty))

            order_payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "buy",
                "type": "market",
                "time_in_force": "day"
            }
            try:
                order_res = requests.post(f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    audit_logs.append(f"CROSSING CANDLE ENTRY SUCCESS: Bought {qty} shares of {symbol} (~${(qty * px):.2f}). Stop anchored to ${stop_price:.2f}.")
                else:
                    audit_logs.append(f"ORDER REJECTED BY ALPACA [{order_res.status_code}] on {symbol}: {order_res.text}")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/{MAX_STOCK_SLOTS}). No new entries required.")

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 11px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Ross Cameron V4.0 Engine)</div>
        <div class="table-responsive" style="overflow-x: hidden;">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%; table-layout: fixed;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    closed_ledger = state.get("closed_trades_ledger", [])
    closed_rows = "".join([
        f"<tr>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{t['symbol']}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(t.get('entry_price', 0), 5)}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(t.get('exit_price', 0), 5)}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; color: #b45309;'>{t['exit_reason']}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if t.get('realized_pnl', 0) >= 0 else '#c62828'}; font-weight: bold;'>${t.get('realized_pnl', 0):+.2f}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 10px;'>{t['timestamp']}</td>"
        f"</tr>"
        for t in closed_ledger[:5]
    ]) if closed_ledger else "<tr><td colspan='6' style='padding: 10px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"

    text_fallback = f"TR-GC-Equities-LS-01 V4.0 Super Bot\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Total Gain: USD ${today_realized_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

    positions_rows = "".join([
        f"<tr>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['symbol']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${p['market_value']:.2f}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #334155;'>${round_sig_figs(p['entry'], 5)}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #b45309;'>${p['stop']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['status']}</td>"
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
            <td colspan="3" style="padding: 9px 10px; text-align: right;">TOTAL:</td>
            <td style="padding: 9px 10px;">${total_market_value_sum:.2f}</td>
            <td style="padding: 9px 10px; color: {'#2e7d32' if total_pnl_sum >= 0 else '#c62828'};">${total_pnl_sum:+.2f} ({total_roe_avg:+.2f}%)</td>
            <td colspan="3"></td>
        </tr>
        """
    else:
        positions_rows = "<tr><td colspan='8' style='padding: 15px; text-align: center; color: #666;'>No active positions (100% Cash Flat).</td></tr>"

    html_content = f"""
    <html>
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 10px; color: #333; }}
          .container {{ max-width: 100%; width: 100%; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05); box-sizing: border-box; }}
          .header {{ background: #0f172a; color: #ffffff; padding: 15px 20px; }}
          .header h2 {{ margin: 0; font-size: 16px; font-weight: 600; }}
          .header p {{ margin: 5px 0 0; font-size: 11px; color: #94a3b8; }}
          .content {{ padding: 15px; }}
          .net-worth-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 15px; margin-bottom: 20px; }}
          .net-worth-title {{ font-size: 12px; text-transform: uppercase; color: #64748b; font-weight: 600; margin-bottom: 6px; }}
          .net-worth-value {{ font-size: 24px; font-weight: 700; color: #0f172a; }}
          .net-worth-subtitle {{ font-size: 11px; color: #64748b; margin-top: 4px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 10px; }}
          
          .pnl-badge {{ background: {'#e6f4ea' if today_realized_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_realized_gain >= 0 else '#c5221f'}; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}
          .lifetime-badge {{ background: #f1f5f9; color: #0f172a; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}

          .rules-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px 15px; margin-bottom: 20px; font-size: 11px; color: #334155; line-height: 1.6; }}
          .rules-title {{ font-weight: 700; text-transform: uppercase; margin-bottom: 6px; font-size: 12px; color: #0f172a; }}
          .section-title {{ font-size: 13px; text-transform: uppercase; color: #475569; margin: 20px 0 8px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 4px; font-weight: 600; }}
          .table-responsive {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 15px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 11px; white-space: nowrap; }}
          th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 8px 8px; font-weight: 600; border-bottom: 2px solid #cbd5e1; }}
          td {{ padding: 8px 8px; }}
          .footer {{ text-align: center; font-size: 10px; color: #94a3b8; padding: 12px; background: #f8fafc; border-top: 1px solid #e2e8f0; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Equities-LS-01 V4.0 | Ross Cameron Super Bot</h2>
            <p>Timestamp: {timestamp} &bull; Mode: ROSS CAMERON MASTERCLASS</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Account Equity</div>
              <div class="net-worth-value">USD ${equity:.2f}</div>
              <div class="net-worth-subtitle">
                <span>Margin Utilization: <b>{margin_util_pct:.1f}%</b></span>
                <span>Today's Gain: <span class="pnl-badge">${today_realized_gain:+,.2f}</span></span>
                <span>Lifetime P&L: <span class="lifetime-badge">${lifetime_cumulative_pnl:+,.2f}</span></span>
              </div>
            </div>

            <div class="rules-card">
              <div class="rules-title">&#9989; Active V4.0 Guardrails (Ross Cameron Masterclass)</div>
              &bull; <b>Ross Cameron 5 Pillars Gate:</b> Price $2-$20, Float <20M, RVOL >=5.0x, Gain >=10%, Catalyst/Sector<br>
              &bull; <b>Sub-$5M Market Cap Shield:</b> Protects against immediate secondary dilution on micro-floats<br>
              &bull; <b>Exchange Safety Filter:</b> Rejects NYSE/AMEX "Flying Blind" hidden halt quotes<br>
              &bull; <b>Crossing Candle Entry Mechanics:</b> Triggers on pullback high breakout; anchors stop to pullback low<br>
              &bull; <b>Daily 200 SMA Ceiling Guard:</b> Blocks entries within 5% underneath Daily 200 SMA<br>
              &bull; <b>50% Peak Giveback Shield:</b> Automatically shuts down session if peak intraday profit drops 50%<br>
              &bull; <b>Two-Strike VWAP & Topping Tail Guards:</b> Enforces micro-exits on VWAP reclaim failures or >40% upper wicks<br>
              &bull; <b>Post-Drawdown Step-Down Sizing Clamp:</b> Clamps sizing to 50% until 50% drawdown is recovered
            </div>

            <div class="section-title">Active Sniper Scalps</div>
            <div class="table-responsive">
              <table><thead><tr><th>Bot Title</th><th>Symbol</th><th>Side</th><th>Market Value USD</th><th>Unrealized P&L USD</th><th>Buy Price</th><th>Stop Price</th><th>Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
            </div>

            <div class="section-title">Recently Closed Trades & Exit Telemetry</div>
            <div class="table-responsive">
              <table><thead><tr><th>Symbol</th><th>Entry Price</th><th>Exit Price</th><th>Exit Reason / Catalyst</th><th>Realized P&L</th><th>Timestamp</th></tr></thead><tbody>{closed_rows}</tbody></table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Alpaca Ross Cameron V4.0 Super Bot Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Alpaca V4.0 Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] V4.0 Super Bot telemetry report complete.")

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] V4.0 Stock Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
