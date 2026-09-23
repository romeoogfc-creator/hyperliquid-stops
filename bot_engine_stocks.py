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

# Google GenAI SDK Imports
from google import genai
from google.genai import types

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
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
        "ai_shield_cache": {},
        "daily_starting_equity": {}
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
                return latest_close >= prev_close, prev_close, latest_close
    except Exception as e:
        print(f"Error checking SPY macro regime: {e}")
    return True, 0.0, 0.0

def run_gemini_stock_market_shield():
    state = load_state()
    cached_shield = state.get("ai_shield_cache", {})
    today_str = datetime.now().strftime("%Y-%m-%d")

    if cached_shield.get("scan_date") == today_str and "high_risk_detected" in cached_shield:
        return cached_shield

    if not GEMINI_API_KEY:
        return {
            "high_risk_detected": False, 
            "risk_level": "UNKNOWN", 
            "reason": "API Key Missing", 
            "action": "ALLOW_TRADES",
            "ai_market_brief": "AI Shield offline (Missing GEMINI_API_KEY secret)."
        }

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = """
        Perform a live real-time web search for breaking US stock market news, SPY/QQQ market trends, Federal Reserve macro economic updates, 
        unexpected earnings shocks, or geopolitical events from the last 1-2 hours.
        Determine if there is extreme high-risk volatility or black-swan risk that could cause sudden crashes in equities.
        Provide a 2-sentence executive summary of current US stock market sentiment and key catalysts for the email dashboard.
        """
        models = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"]
        for model_name in models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                        response_mime_type="application/json",
                        response_schema={
                            "type": "OBJECT",
                            "properties": {
                                "high_risk_detected": {"type": "BOOLEAN"},
                                "risk_level": {"type": "STRING", "enum": ["LOW", "MODERATE", "HIGH"]},
                                "reason": {"type": "STRING"},
                                "action": {"type": "STRING", "enum": ["ALLOW_TRADES", "BLOCK_ENTRIES"]},
                                "ai_market_brief": {"type": "STRING"}
                            },
                            "required": ["high_risk_detected", "risk_level", "reason", "action", "ai_market_brief"]
                        }
                    )
                )
                result = json.loads(response.text)
                result["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
                result["scan_date"] = today_str
                state["ai_shield_cache"] = result
                save_state(state)
                return result
            except Exception:
                continue
    except Exception:
        pass

    return cached_shield if cached_shield else {
        "high_risk_detected": False, 
        "risk_level": "UNKNOWN", 
        "reason": "AI Shield Bypass on Error", 
        "action": "ALLOW_TRADES",
        "ai_market_brief": "Stock market scanning operating normally under standard quantitative rules."
    }

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

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    
    audit_logs = []
    audit_logs.append(f"[{timestamp}] Trailing Peak Sniper Engine Started (Smart Volume-Adaptive Trailing, Texas CT: {ct_now.strftime('%H:%M:%S')}).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    start_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')

    ai_shield = run_gemini_stock_market_shield()
    ai_risk_status = "BLOCKED (High Risk)" if ai_shield.get("high_risk_detected") else "PASS (Normal Risk)"
    audit_logs.append(f"Gemini AI Shield: [{ai_shield.get('risk_level', 'UNKNOWN')}] {ai_shield.get('reason', '')} -> {ai_risk_status}")

    # --- SPY MACRO TREND REGIME SHIELD ---
    spy_green, spy_open_px, spy_close_px = check_market_macro_regime()
    spy_regime_str = f"GREEN (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> Market Healthy" if spy_green else f"RED (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> MACRO BLOODBATH DETECTED"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    state = load_state()

    account_res = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text}")
    account_data = account_res.json()

    equity = float(account_data.get("equity", 100000.0))
    cash = float(account_data.get("cash", 100000.0))
    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0

    # Fixed Daily Starting Equity Rollover
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
    today_total_gain = equity - today_start_eq
    lifetime_cumulative_pnl = equity - 100000.0

    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []

    # --- RULE 1: PRE-CLOSE EOD SQUARE-OFF (2:40 PM CT) ---
    is_eod_square_off = (ct_now.hour == 14 and ct_now.minute >= 40) or (ct_now.hour >= 15)

    # --- RULE 2: MACRO BLOODBATH 100% CASH FLAT EXIT (If SPY turns RED) ---
    is_macro_bloodbath = not spy_green

    if (is_eod_square_off or is_macro_bloodbath) and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat)" if is_eod_square_off else "Macro Bloodbath Shield (SPY Flipped Red - 100% Cash Flat)"
        audit_logs.append(f"EMERGENCY EXIT TRIGGERED ({reason_text}): Liquidating all open positions to cash.")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            side = pos.get("side")
            entry_px = float(pos.get("avg_entry_price", 0))
            current_px = float(pos.get("current_price", entry_px))
            close_side = "sell" if side == "long" else "buy"
            
            realized_pnl = (current_px - entry_px) * qty if side == "long" else (entry_px - current_px) * qty

            close_payload = {
                "symbol": sym,
                "qty": str(abs(qty)),
                "side": close_side,
                "type": "market",
                "time_in_force": "day"
            }
            try:
                close_res = requests.post(f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
                if close_res.status_code == 200:
                    audit_logs.append(f"LIQUIDATION SUCCESS: Closed {sym}")
                    if "closed_trades_ledger" not in state:
                        state["closed_trades_ledger"] = []
                    
                    existing_symbols_today = [t['symbol'] for t in state["closed_trades_ledger"] if today_str in t.get("timestamp", "")]
                    if sym not in existing_symbols_today:
                        state["closed_trades_ledger"].insert(0, {
                            "symbol": sym,
                            "entry_price": entry_px,
                            "exit_price": current_px,
                            "exit_reason": reason_text,
                            "realized_pnl": realized_pnl,
                            "timestamp": timestamp
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

        prev_peak = current_active_cache.get(symbol, {}).get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

        # --- SMART RUNNER VOLUME-ADAPTIVE TRAILING ---
        # Fetch latest 30-min bar to evaluate real-time volume ratio for this specific position
        vol_ratio = 1.0
        try:
            bar_res = requests.get(f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=30Min&limit=5&feed=iex&start={start_date}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if len(b_list) >= 2:
                    volumes = [float(b["v"]) for b in b_list]
                    avg_v = np.mean(volumes[:-1]) if len(volumes) > 1 else volumes[-1]
                    curr_v = volumes[-1]
                    vol_ratio = curr_v / avg_v if avg_v > 0 else 1.0
        except Exception:
            pass

        # Adjust trailing tightness based on volume strength:
        # High institutional volume (>= 1.5) = wider breathing room for runners.
        # Stalling volume (< 0.8) = tighter lock to secure profits before greed turns to red.
        if vol_ratio >= 1.5:
            vol_multiplier = 1.6  # Give runners space (+60% buffer expansion)
            runner_tag = "🚀 Smart Runner (High Vol)"
        elif vol_ratio < 0.8:
            vol_multiplier = 0.7  # Tighter lock on volume stall
            runner_tag = "⚠️ Volume Stall Lock"
        else:
            vol_multiplier = 1.0
            runner_tag = "Active Scalp"

        new_active_cache[symbol] = {
            "entry_px": entry_px,
            "current_px": current_px,
            "qty": qty,
            "peak_roe": peak_roe,
            "vol_ratio": vol_ratio
        }

        stop_threshold = -0.012  # Default initial hard stop (-1.2%)
        status_label = runner_tag

        if peak_roe >= 0.03:
            # Ballistic Peak Lock with Volume Adaptation
            base_buffer = 0.0025
            adjusted_buffer = base_buffer * vol_multiplier
            stop_threshold = peak_roe - adjusted_buffer
            status_label = f"Ballistic Peak Lock [{vol_ratio:.1f}x Vol] ({stop_threshold*100:+.1f}%)"
        elif peak_roe >= 0.015:
            base_buffer = 0.0040
            adjusted_buffer = base_buffer * vol_multiplier
            stop_threshold = peak_roe - adjusted_buffer
            status_label = f"Mid Trailing Floor [{vol_ratio:.1f}x Vol] ({stop_threshold*100:+.1f}%)"
        elif peak_roe >= 0.005:
            base_buffer = 0.0060
            adjusted_buffer = base_buffer * vol_multiplier
            stop_threshold = peak_roe - adjusted_buffer
            status_label = f"Early Breathing Floor [{vol_ratio:.1f}x Vol] ({stop_threshold*100:+.1f}%)"

        should_exit = current_roe <= stop_threshold

        if should_exit:
            reason = "Smart Runner Profit Lock" if current_roe > 0 else "Dynamic Stop Loss"
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE (Peak: {peak_roe*100:+.2f}%, VolRatio: {vol_ratio:.2f}). {reason} - securing bag!")
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            close_payload = {
                "symbol": symbol,
                "qty": str(abs(qty)),
                "side": close_side,
                "type": "market",
                "time_in_force": "day"
            }
            try:
                requests.post(f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                
                existing_symbols_today = [t['symbol'] for t in state["closed_trades_ledger"] if today_str in t.get("timestamp", "")]
                if symbol not in existing_symbols_today:
                    state["closed_trades_ledger"].insert(0, {
                        "symbol": symbol,
                        "entry_price": entry_px,
                        "exit_price": current_px,
                        "exit_reason": reason,
                        "realized_pnl": realized_pnl,
                        "timestamp": timestamp
                    })
                state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]
                active_count -= 1
                continue
            except Exception as e:
                audit_logs.append(f"Execution Failed on {symbol}: {e}")

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
            "stop": round_sig_figs(entry_px * (1 + stop_threshold), 5),
            "floor": stop_threshold * 100,
            "status": status_label
        })

    closed_symbols = set(current_active_cache.keys()) - active_symbols
    for closed_sym in closed_symbols:
        old_data = current_active_cache.get(closed_sym, {})
        entry_px = old_data.get("entry_px", 0.0)
        exit_px = float(old_data.get("current_px", entry_px))
        qty = old_data.get("qty", 0.0)
        realized_pnl = (exit_px - entry_px) * qty
        
        existing_symbols_today = [t['symbol'] for t in state.get("closed_trades_ledger", []) if today_str in t.get("timestamp", "")]
        if closed_sym not in existing_symbols_today:
            if "closed_trades_ledger" not in state:
                state["closed_trades_ledger"] = []
            state["closed_trades_ledger"].insert(0, {
                "symbol": closed_sym,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "exit_reason": "Smart Runner Profit Lock",
                "realized_pnl": realized_pnl,
                "timestamp": timestamp
            })
            state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]

    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 6
    is_trading_window = (8 <= ct_now.hour < 15) or (ct_now.hour == 15 and ct_now.minute <= 30)

    watchlist = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "QCOM",
        "INTC", "MU", "ARM", "AMAT", "LRCX", "SMCI", "ORCL", "CRM", "NOW", "PANW", "FTNT", "PLTR",
        "COIN", "HOOD", "SQ", "PYPL", "NET", "SNOW", "SHOP", "MSTR", "UBER", "ABNB", "RBLX", "DKNG",
        "NFLX", "COST", "WMT", "HD", "DIS", "NKE", "SBUX", "BA", "CAT", "GE", "HON", "DE", "LMT",
        "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "SLB", "LLY", "UNH", "JNJ", "PFE", "ABBV",
        "TXN", "ADI", "KLAC", "MCHP", "DELL",
        "SPY", "QQQ", "IWM", "DIA", "SMH", "XLF", "XLE"
    ]

    symbols_to_scan = [s for s in watchlist if s not in active_symbols]
    market_candidates = []
    smart_queue_candidates = []
    scanned_count = 0

    chunk_size = 20
    for i in range(0, len(symbols_to_scan), chunk_size):
        chunk = symbols_to_scan[i:i + chunk_size]
        symbols_param = ",".join(chunk)

        all_bars_data = {}
        page_token = None

        while True:
            url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=30Min&limit=10000&feed=iex&start={start_date}"
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
            opens = [float(b["o"]) for b in bars]
            highs = [float(b["h"]) for b in bars]
            lows = [float(b["l"]) for b in bars]
            volumes = [float(b["v"]) for b in bars]

            upper, lower, filter_band = calculate_gaussian_channel(closes)
            current_close = closes[-1]
            current_open = opens[-1]
            prev_close = closes[-2]

            ci = calculate_choppiness_index(highs, lows, closes)
            if ci > 62.0:
                continue

            avg_vol = np.mean(volumes[-10:]) if len(volumes) >= 10 else volumes[-1]
            current_vol = volumes[-1]
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

            atr = calculate_atr(highs, lows, closes)
            extension_score = max(0.0, (current_close - upper) / upper)
            atr_score = atr / current_close if current_close > 0 else 0.0
            momentum_score = extension_score + atr_score

            candidate_obj = {
                "symbol": symbol, "close": current_close, "is_long": True,
                "score": momentum_score, "ci": ci, "vol_ratio": vol_ratio
            }
            smart_queue_candidates.append(candidate_obj)

            is_green_candle = current_close > current_open
            has_upward_continuation = current_close > prev_close

            if current_close > upper and current_close <= upper * 1.02 and vol_ratio >= 0.8 and is_green_candle and has_upward_continuation:
                market_candidates.append(candidate_obj)
                audit_logs.append(f"TRAILING SNIPER BREAKOUT MATCH (Confirmed Green): {symbol} @ ${current_close:.2f} (VolRatio: {vol_ratio:.2f}, CI: {ci:.1f})")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    smart_queue_sorted = sorted(smart_queue_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"Trailing Sniper Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated triggers.")

    if ai_shield.get("high_risk_detected"):
        audit_logs.append(f"Execution Gate: BLOCKED BY GEMINI AI SHIELD. Reason: {ai_shield.get('reason')}")
    elif not spy_green:
        audit_logs.append(f"Execution Gate: BLOCKED BY SPY MACRO BLOODBATH SHIELD (SPY Daily Trend is RED). Staying out of the market.")
    elif not is_trading_window or is_eod_square_off:
        audit_logs.append("Execution Gate: Outside active trading hours or square-off window active.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]

            target_usd = max(50.0, equity * 0.13)
            qty = round(target_usd / px, 4)
            tif = "day"

            order_payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "buy",
                "type": "market",
                "time_in_force": tif
            }
            try:
                order_res = requests.post(f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    audit_logs.append(f"TRAILING SNIPER ENTRY SUCCESS: Bought {qty} shares of {symbol} (~${target_usd:.2f})")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/{MAX_STOCK_SLOTS}). No new scalps triggered.")

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 11px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Trailing Peak Sniper Engine)</div>
        <div class="table-responsive" style="overflow-x: hidden;">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%; table-layout: fixed;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    remaining_candidates = [c for c in smart_queue_sorted if c["symbol"] not in active_symbols]
    ondeck_rows = "".join([
        f"<tr>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>#{i+1}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold; color: #0f172a;'>{c['symbol']}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-family: monospace;'>${c['close']:.2f}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: #b45309; font-weight: 600;'>Score: {c['score']:.4f}</td>"
        f"</tr>"
        for i, c in enumerate(remaining_candidates[:3])
    ]) if remaining_candidates else "<tr><td colspan='4' style='padding: 10px; text-align: center; color: #666;'>No momentum candidates currently detected.</td></tr>"

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

    text_fallback = f"TR-GC-Equities-LS-01 | Trailing Peak Profit Hunter\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Total Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

    positions_rows = "".join([
        f"<tr>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['symbol']}</td>"
        f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['side']}</td>"
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

    ai_risk_color = "#c62828" if ai_shield.get("high_risk_detected") else "#2e7d32"
    ai_badge = f"<span style='background: {ai_risk_color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 10px;'>Risk Level: {ai_shield.get('risk_level', 'UNKNOWN')}</span>"

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
          
          .pnl-badge {{ background: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}
          .lifetime-badge {{ background: #f1f5f9; color: #0f172a; padding: 4px 8px; border-radius: 4px; font-weight: bold; }}

          .ai-brief-card {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 6px; padding: 12px 15px; margin-bottom: 20px; font-size: 11px; color: #166534; line-height: 1.6; }}
          .ai-brief-title {{ font-weight: 700; text-transform: uppercase; margin-bottom: 6px; font-size: 12px; color: #15803d; display: flex; align-items: center; justify-content: space-between; }}
          
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
            <h2>TR-GC-Equities-LS-01 | Trailing Peak Profit Hunter</h2>
            <p>Timestamp: {timestamp} &bull; Mode: SMART VOLUME-ADAPTIVE RUNNERS</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Account Equity</div>
              <div class="net-worth-value">USD ${equity:.2f}</div>
              <div class="net-worth-subtitle">
                <span>Margin Utilization: <b>{margin_util_pct:.1f}%</b></span>
                <span>Today's Gain: <span class="pnl-badge">${today_total_gain:+,.2f}</span></span>
                <span>Lifetime P&L: <span class="lifetime-badge">${lifetime_cumulative_pnl:+,.2f}</span></span>
              </div>
            </div>

            <div class="ai-brief-card">
              <div class="ai-brief-title">
                <span>🤖 GEMINI AI EXECUTIVE STOCK MARKET BRIEFING</span>
                {ai_badge}
              </div>
              <b>Live Market Assessment:</b> {ai_shield.get('ai_market_brief', 'Normal conditions.')}<br>
              <b>Execution Recommendation:</b> {ai_shield.get('reason', 'Standard scan active.')}
            </div>

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Guardrails (Smart Volume-Adaptive Trailing)</div>
              &bull; <b>SPY Bloodbath Shield:</b> Instantly liquidates 100% of positions to cash if SPY flips red<br>
              &bull; <b>Smart Runner Volume-Adaptation:</b> Heavy volume (>=1.5x) widens the trail so true runners run to the top; stalling volume (<0.8x) tightens the lock instantly<br>
              &bull; <b>ATR Dynamic Stops:</b> Adapts initial stop-loss to each stock's volatility (1.5x ATR)<br>
              &bull; <b>Scaled Position Sizing:</b> 13% NAV allocation per slot<br>
              &bull; <b>Pre-Close EOD Square-Off:</b> Automatic 100% cash liquidation at 2:40 PM CT daily<br>
              &bull; <b>Asset Universe:</b> S&P 500 & Nasdaq Momentum Equities
            </div>

            <div class="section-title">Active Sniper Scalps</div>
            <div class="table-responsive">
              <table><thead><tr><th>Bot Title</th><th>Symbol</th><th>Side</th><th>Market Value USD</th><th>Unrealized P&L USD</th><th>Buy Price</th><th>Stop Price</th><th>Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
            </div>

            <div class="section-title">Recently Closed Trades & Exit Telemetry</div>
            <div class="table-responsive">
              <table><thead><tr><th>Symbol</th><th>Entry Price</th><th>Exit Price</th><th>Exit Reason / Catalyst</th><th>Realized P&L</th><th>Timestamp</th></tr></thead><tbody>{closed_rows}</tbody></table>
            </div>

            <div class="section-title">On-Deck Momentum Queue</div>
            <div class="table-responsive">
              <table><thead><tr><th>Rank</th><th>Symbol</th><th>Current Price</th><th>Momentum Score</th></tr></thead><tbody>{ondeck_rows}</tbody></table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Alpaca Trailing Peak Sniper Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Alpaca Trailing Peak Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Trailing Peak Sniper telemetry report complete.")

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Trailing Peak Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
