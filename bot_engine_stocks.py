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
            print(f"Error loading stock state.json: {e}")
    return default_state

def save_state(state):
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def check_market_macro_regime():
    try:
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols=SPY&timeframe=1Day&limit=5&feed=iex&start={start_date}"
        res = requests.get(url, headers=HEADERS)
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
        print(f"Error checking SPY macro regime: {e}")
    return "NEUTRAL", 0.0, 0.0, 0.0

def send_html_dashboard_email(subject, html_content, text_fallback):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
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
    except Exception as e:
        print(f"Failed to send email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

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

def calculate_adaptive_stock_stop(entry_px, is_long, current_px, atr_val, peak_roe=0.0):
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    atr_roe_buffer = (atr_val * 1.5) / entry_px if entry_px > 0 else 0.015
    atr_roe_buffer = max(0.012, min(0.035, atr_roe_buffer))

    leash_status = f"1H ATR Noise Buffer (-{atr_roe_buffer*100:.2f}%)"
    
    if peak_roe >= 0.30:
        target_floor_roe = max(peak_roe * 0.95, peak_roe - 0.05)
        leash_status = f"🌌 Parabolic 95%-97% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.15:
        target_floor_roe = peak_roe * 0.90
        leash_status = f"🚀 Mega Runner 90% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.08:
        target_floor_roe = peak_roe * 0.85
        leash_status = f"📈 Trend Lock 85% [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.03:
        target_floor_roe = max(0.02, peak_roe * 0.80)
        leash_status = f"🎯 80% Peak Lock [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
    elif peak_roe >= 0.015:
        target_floor_roe = max(0.010, peak_roe * 0.60)
        leash_status = f"🔒 Winner Lock [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
    elif peak_roe >= 0.005:
        target_floor_roe = max(0.0025, peak_roe * 0.50)
        leash_status = f"🛡️ 50% High-Water Lock [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
    else:
        target_floor_roe = -atr_roe_buffer

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
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 Engine Started (Texas CT: {ct_now.strftime('%H:%M:%S')}).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    start_date = (datetime.now() - timedelta(days=20)).strftime('%Y-%m-%d')

    spy_regime, spy_open_px, spy_close_px, spy_pct = check_market_macro_regime()
    spy_regime_str = f"{spy_regime} ({spy_pct:+.2f}%) -> {'LONG Tilt' if spy_regime == 'GREEN' else ('SHORT Tilt' if spy_regime == 'RED' else 'NEUTRAL Mode')}"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    state = load_state()

    account_res = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text}")
    account_data = account_res.json()

    equity = float(account_data.get("equity", 100000.0))
    cash = float(account_data.get("cash", 100000.0))
    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0

    # --- TRACK DAILY PEAK EQUITY & STARTING EQUITY ---
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
    lifetime_cumulative_pnl = equity - 100000.0

    audit_logs.append(f"Equity Metrics: Today Start: ${today_start_eq:.2f} | Peak: ${today_peak_eq:.2f} | Current: ${equity:.2f} (Giveback: ${giveback_from_peak:.2f})")

    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []

    # --- ANTI-WIPEOUT SHIELD #1: DAILY MAX LOSS CIRCUIT BREAKER (-2.5%) ---
    daily_loss_pct = (today_total_gain / today_start_eq) if today_start_eq > 0 else 0.0
    is_daily_max_loss_triggered = daily_loss_pct <= -0.025

    if is_daily_max_loss_triggered and positions_list:
        audit_logs.append(f"🛡️ ANTI-WIPEOUT SHIELD TRIGGERED: Daily Loss at {daily_loss_pct*100:.2f}%. Liquidating all positions to cash!")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            close_side = "sell" if side == "long" else "buy"
            entry_p = float(pos.get("avg_entry_price", 0))
            exit_p = float(pos.get("current_price", entry_p))
            realized_pnl = (exit_p - entry_p) * qty if side == "long" else (entry_p - exit_p) * qty
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": sym, "entry_price": entry_p, "exit_price": exit_p,
                    "exit_reason": "🛡️ Daily Max Loss Anti-Wipeout Shield (-2.5%)",
                    "realized_pnl": realized_pnl, "timestamp": timestamp
                })
            except Exception as e:
                audit_logs.append(f"Anti-wipeout close failed on {sym}: {e}")
        for order in open_orders:
            requests.delete(f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)
        save_state(state)
        positions_list = []

    # --- ANTI-WIPEOUT SHIELD #2: UNREALIZED PORTFOLIO CIRCUIT BREAKER (-3.5%) ---
    total_unrealized_pnl = sum([float(p.get("unrealized_pl", 0)) for p in positions_list])
    portfolio_pnl_pct = (total_unrealized_pnl / equity) if equity > 0 else 0.0
    if portfolio_pnl_pct <= -0.035 and positions_list:
        audit_logs.append(f"🚨 PORTFOLIO DRAWDOWN BREAKER TRIGGERED ({portfolio_pnl_pct*100:.2f}%). Flattening to cash!")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            close_side = "sell" if side == "long" else "buy"
            entry_p = float(pos.get("avg_entry_price", 0))
            exit_p = float(pos.get("current_price", entry_p))
            realized_pnl = (exit_p - entry_p) * qty if side == "long" else (entry_p - exit_p) * qty
            try:
                requests.post(f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": sym, "entry_price": entry_p, "exit_price": exit_p,
                    "exit_reason": "🚨 Portfolio Drawdown Circuit Breaker (-3.5%)",
                    "realized_pnl": realized_pnl, "timestamp": timestamp
                })
            except Exception as e:
                audit_logs.append(f"Circuit breaker close failed on {sym}: {e}")
        for order in open_orders:
            requests.delete(f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)
        save_state(state)
        positions_list = []

    # --- MANDATORY EOD SQUARE-OFF (2:40 PM CT) ---
    is_eod_square_off = (ct_now.hour == 14 and ct_now.minute >= 40) or (ct_now.hour >= 15)

    if is_eod_square_off and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat)"
        audit_logs.append(f"EMERGENCY EXIT TRIGGERED ({reason_text}): Liquidating all open positions to cash.")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            entry_px = float(pos.get("avg_entry_price", 0))
            current_px = float(pos.get("current_price", entry_px))
            close_side = "sell" if side == "long" else "buy"
            realized_pnl = (current_px - entry_px) * qty if side == "long" else (entry_px - current_px) * qty

            try:
                close_res = requests.post(f"{BASE_URL}/v2/orders", json={"symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"}, headers=HEADERS)
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

    # --- 1:30 PM CT MIDDAY STAGNATION CLEAN-UP (Closes floating negative trades early) ---
    is_midday_clean_up = (ct_now.hour == 13 and ct_now.minute >= 30 and ct_now.minute < 40)
    if is_midday_clean_up and positions_list:
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = abs(float(pos.get("qty", 0)))
            side = pos.get("side", "long").lower()
            is_long = (side == "long")
            entry_px = float(pos.get("avg_entry_price", 0))
            current_px = float(pos.get("current_price", entry_px))
            current_roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)

            if current_roe < 0:
                audit_logs.append(f"🧹 MIDDAY STAGNATION EXIT: Closing losing position on {sym} at {current_roe*100:.2f}% ROE before afternoon chop.")
                close_side = "sell" if is_long else "buy"
                realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty
                try:
                    requests.post(f"{BASE_URL}/v2/orders", json={"symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"}, headers=HEADERS)
                    if "closed_trades_ledger" not in state:
                        state["closed_trades_ledger"] = []
                    state["closed_trades_ledger"].insert(0, {
                        "symbol": sym, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": f"🧹 Midday Stagnation Clean-up ({current_roe*100:.2f}%)", "realized_pnl": realized_pnl, "timestamp": timestamp
                    })
                except Exception as e:
                    audit_logs.append(f"Midday exit failed on {sym}: {e}")

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
        
        active_symbols.add(symbol)
        current_roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)

        should_exit = False
        exit_reason = ""
        atr_val = 1.0

        try:
            start_date_1h = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            bar_res = requests.get(f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=1Hour&limit=50&feed=iex&start={start_date_1h}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if b_list:
                    closes_1h = [float(b["c"]) for b in b_list]
                    highs_1h = [float(b["h"]) for b in b_list]
                    lows_1h = [float(b["l"]) for b in b_list]
                    
                    atr_val = calculate_atr(highs_1h, lows_1h, closes_1h)
                    upper_band, lower_band, filter_line = calculate_gaussian_channel(closes_1h)

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

        stop_px_calc, current_roe, target_floor_roe, leash_status = calculate_adaptive_stock_stop(
            entry_px, is_long, current_px, atr_val, peak_roe=peak_roe
        )

        if is_long and current_px <= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"
        elif (not is_long) and current_px >= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"

        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "side": side,
            "peak_roe": peak_roe, "best_target_floor_roe": target_floor_roe, "best_stop_px": stop_px_calc
        }

        if should_exit:
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE. Reason: {exit_reason}")
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            try:
                requests.post(f"{BASE_URL}/v2/orders", json={"symbol": symbol, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"}, headers=HEADERS)
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

        positions_data.append({
            "bot_title": "TR-GC-Equities-LS-01", "symbol": symbol, "side": "LONG" if is_long else "SHORT",
            "qty": qty, "entry": entry_px, "current": current_px, "market_value": market_value,
            "pnl": unrealized_pnl, "roe": current_roe * 100, "stop": round_sig_figs(stop_px_calc, 5),
            "floor": target_floor_roe * 100, "status": leash_status
        })

    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 5
    
    # --- TIME-WINDOWED ENTRY REGULATION ---
    is_prime_morning_window = (8 <= ct_now.hour < 11) or (ct_now.hour == 11 and ct_now.minute <= 30)  # 8:00 AM - 11:30 AM CT
    is_midday_window = (ct_now.hour == 11 and ct_now.minute > 30) or (ct_now.hour == 12) or (ct_now.hour == 13 and ct_now.minute < 30)  # 11:30 AM - 1:30 PM CT
    is_afternoon_lockout = (ct_now.hour == 13 and ct_now.minute >= 30) or (ct_now.hour >= 14)  # 1:30 PM CT onwards -> NO NEW ENTRIES

    # --- DAILY PEAK HIGH-WATER LOCK SHIELD ($150 GIVEBACK CAP) ---
    peak_giveback_lockout = giveback_from_peak >= 150.0 and (today_peak_eq > today_start_eq)
    if peak_giveback_lockout:
        audit_logs.append(f"🛡️ HIGH-WATER SHIELD ACTIVE: Gave back ${giveback_from_peak:.2f} from intra-day peak (${today_peak_eq:.2f}). Blocking new trades to preserve gains.")

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

    # --- 1-HOUR ENTRY SCANNER ---
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
                bars_res = requests.get(url, headers=HEADERS)
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

            ci = calculate_choppiness_index(highs, lows, closes)
            if ci > 58.0:
                continue

            upper, lower, filter_band = calculate_gaussian_channel(closes)
            current_close = closes[-1]
            prev_close = closes[-2]

            if current_close > upper * 1.025:
                continue

            candidate_obj = None
            if spy_regime in ["GREEN", "NEUTRAL"]:
                if current_close > upper and prev_close <= upper:
                    candidate_obj = {
                        "symbol": symbol, "close": current_close, "is_long": True,
                        "score": (current_close - upper) / upper, "ci": ci
                    }

            if spy_regime in ["RED", "NEUTRAL"] and candidate_obj is None:
                if current_close < lower and prev_close >= lower:
                    candidate_obj = {
                        "symbol": symbol, "close": current_close, "is_long": False,
                        "score": (lower - current_close) / lower, "ci": ci
                    }

            if candidate_obj:
                market_candidates.append(candidate_obj)

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"1H Adaptive Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated triggers.")

    # --- EXECUTION GATE WITH TIME-WINDOWED CAPITAL REGULATION ---
    if is_afternoon_lockout or is_eod_square_off or peak_giveback_lockout or is_daily_max_loss_triggered:
        gate_reason = "Daily Max Loss Cap" if is_daily_max_loss_triggered else ("Afternoon Cutoff (1:30 PM+ CT)" if is_afternoon_lockout else ("Peak Giveback Lockout" if peak_giveback_lockout else "EOD Square-Off"))
        audit_logs.append(f"Execution Gate BLOCKED: {gate_reason}. No new entries allowed.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            cand_ci = candidate.get("ci", 50.0)

            # --- DYNAMIC CAPITAL ALLOCATION RULE ---
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

            order_payload = {
                "symbol": symbol, "qty": str(qty), "side": order_side, "type": "market", "time_in_force": "day"
            }
            try:
                order_res = requests.post(f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    audit_logs.append(f"1H ENTRY SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {qty} shares of {symbol} (~${(qty * px):.2f})")
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
    
    # --- STRICT CALENDAR DAY RESET (Clears score to 0 every morning) ---
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

    text_fallback = f"TR-GC-Equities-LS-01 | 1H Adaptive Engine\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

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
            <h2>TR-GC-Equities-LS-01 | 1H Master Engine</h2>
            <p>Timestamp: {timestamp} &bull; Mode: TIME-REGULATED POWER & ANTI-WIPEOUT</p>
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
              &bull; <b>1-Hour Timeframe & Hard CI Gate (&le;58.0):</b> Eliminates noise & rejects choppy stocks<br>
              &bull; <b>SPY Macro Regime Shield:</b> Enforces broad market direction alignment<br>
              &bull; <b>1H Trend Invalidation & ATR Buffer:</b> Cuts losses fast on reversals with proper noise room<br>
              &bull; <b>50% High-Water & Peak Ratchet:</b> Locks 50% of micro-gains and 80%–97% on major runners<br>
              &bull; <b>Morning Power Window (8:00–11:30 AM CT):</b> Full 10% NAV (~$10k) sizing on clean trends<br>
              &bull; <b>Midday Micro Window (11:30 AM–1:30 PM CT):</b> Capped at $1,000 Micro Sizing<br>
              &bull; <b>Afternoon Lockout (1:30 PM CT+):</b> Strictly 0 new entries allowed<br>
              &bull; <b>1:30 PM Stagnation Clean-up:</b> Exits floating losing trades early before EOD chop<br>
              &bull; <b>High-Water Giveback Shield:</b> Blocks trading if giving back >$150 from intra-day peak<br>
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
    print(f"[{timestamp}] 1H Master Engine report complete.")

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Stock engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
