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
        "last_run_timestamp": "", 
        "daily_starting_equity": {},
        "last_traded_candle": {}
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
            print(f"Error loading stock_state.json: {e}", flush=True)
    return default_state

def save_state(state):
    ct_now = get_central_time()
    state["last_run_timestamp"] = ct_now.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def api_retry(func, *args, retries=4, delay=2.0, **kwargs):
    for attempt in range(retries):
        try:
            res = func(*args, **kwargs)
            if isinstance(res, requests.Response):
                if res.status_code in [200, 201, 202, 204]:
                    return res
                elif attempt < retries - 1:
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
    return None

def check_market_macro_regime():
    try:
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://data.alpaca.markets/v2/stocks/bars?symbols=SPY&timeframe=1Day&limit=5&feed=iex&start={start_date}"
        res = api_retry(requests.get, url, headers=HEADERS)
        if res and res.status_code == 200:
            bars_data = res.json().get("bars", {})
            spy_bars = bars_data.get("SPY", [])
            if len(spy_bars) >= 2:
                latest_close = float(spy_bars[-1]["c"])
                prev_close = float(spy_bars[-2]["c"])
                return latest_close >= prev_close, prev_close, latest_close
    except Exception as e:
        print(f"Error checking SPY macro regime: {e}", flush=True)
    return True, 0.0, 0.0

def send_html_dashboard_email(subject, html_content, text_fallback):
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

def calculate_15m_ross_indicators(closes, highs, lows, vols):
    if len(closes) < 20:
        return None

    df_c = pd.Series(closes)
    ema9 = df_c.ewm(span=9, adjust=False).mean().iloc[-1]

    typical_prices = (np.array(highs) + np.array(lows) + np.array(closes)) / 3.0
    tp_v = typical_prices * np.array(vols)
    cum_tp_v = np.sum(tp_v[-32:])
    cum_v = np.sum(vols[-32:])
    vwap = (cum_tp_v / cum_v) if cum_v > 0 else closes[-1]

    ema12 = df_c.ewm(span=12, adjust=False).mean()
    ema26 = df_c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = (macd_line - signal_line).iloc[-1]

    return {
        "ema9": ema9,
        "vwap": vwap,
        "macd_hist": macd_hist,
        "prev_close": closes[-2],
        "prev_ema9": df_c.ewm(span=9, adjust=False).mean().iloc[-2]
    }

def calculate_stock_retention_targets(entry_px, is_long, current_px, entry_candle_low, peak_roe=0.0, upper_wick_pct=0.0):
    # Stock Scale Calibration (1x Unleveraged Equity)
    roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)
    initial_floor_roe = -0.0150  # -1.50% Px Hard Stop Buffer
    drawdown_from_peak = peak_roe - roe

    # Climax Exhaustion Lock: Peak >= +1.50% with a 45%+ upper wick
    is_exhaustion = (peak_roe >= 0.0150) and (drawdown_from_peak >= 0.0030 or upper_wick_pct >= 0.45)

    if is_exhaustion:
        target_floor_roe = max(peak_roe - 0.0020, roe)
        leash_status = f"⚡ CLIMAX EXHAUSTION LOCK [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0300:  # +3.00% Px Gain (Stock Moonshot)
        target_floor_roe = max(peak_roe * 0.88, peak_roe - 0.0050)
        leash_status = f"🚀 UNCAPPED MOONSHOT (88% LOCKED) [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0150:  # +1.50% Px Gain (Stock Expansion)
        target_floor_roe = max(peak_roe * 0.80, peak_roe - 0.0040)
        leash_status = f"🎯 EXPANSION RATCHET (80% LOCKED) [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0050:  # +0.50% Px Gain (Stock Fee Floor)
        target_floor_roe = 0.0015  # Lock +0.15% profit floor
        leash_status = f"🛡 FEE FLOOR LOCKED (+0.15% Px)"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"🛡 STRUCTURAL STOCK STOP ({target_floor_roe*100:.2f}% Px)"

    stop_px = entry_px * (1 + target_floor_roe) if is_long else entry_px * (1 - target_floor_roe)
    return max(stop_px, entry_candle_low) if is_long else min(stop_px, entry_candle_low), leash_status

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    now_ts = time.time()
    
    # Market Trading Hours Gate (Mon-Fri: 8:30 AM CT to 3:00 PM CT)
    is_weekday = ct_now.weekday() < 5
    mins_from_midnight = ct_now.hour * 60 + ct_now.minute
    
    market_open_mins = 8 * 60 + 30     # 8:30 AM CT
    market_close_mins = 15 * 60        # 3:00 PM CT
    eod_square_off_mins = 14 * 60 + 50 # 2:50 PM CT (3:50 PM ET)
    
    is_trading_window = is_weekday and (market_open_mins <= mins_from_midnight < market_close_mins)
    is_eod_square_off = is_weekday and (mins_from_midnight >= eod_square_off_mins)

    state = load_state()
    positions_res = api_retry(requests.get, f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res and positions_res.status_code == 200 else []

    # Quiet Hours Gate: After 3:05 PM CT or before 8:25 AM CT with 0 positions -> Exit silently
    if not is_trading_window and not positions_list and (mins_from_midnight >= 15 * 60 + 5 or mins_from_midnight < 8 * 60 + 25 or not is_weekday):
        print(f"[{timestamp}] Market is CLOSED (Central Time: {ct_now.strftime('%H:%M:%S')}). 0 active positions. Skipping run silently.", flush=True)
        return

    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 Engine Started (V4.8.9 Hybrid 15M Calibrated Engine Active).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    start_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')

    spy_green, spy_open_px, spy_close_px = check_market_macro_regime()
    spy_regime_str = f"GREEN (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> LONGs Allowed" if spy_green else f"RED (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> SHORTs Allowed"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    account_res = api_retry(requests.get, f"{BASE_URL}/v2/account", headers=HEADERS)
    if not account_res or account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text if account_res else 'No Response'}")
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
    today_total_gain = equity - today_start_eq
    lifetime_cumulative_pnl = equity - 100000.0

    orders_res = api_retry(requests.get, f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res and orders_res.status_code == 200 else []

    # --- PORTFOLIO DRAWDOWN CIRCUIT BREAKER (-3.5% Loss Check) ---
    total_unrealized_pnl = sum([float(p.get("unrealized_pl", 0)) for p in positions_list])
    portfolio_pnl_pct = (total_unrealized_pnl / equity) if equity > 0 else 0.0
    if portfolio_pnl_pct <= -0.035 and positions_list:
        audit_logs.append(f"🚨 PORTFOLIO CIRCUIT BREAKER TRIGGERED: Unrealized P&L at {portfolio_pnl_pct*100:.2f}%. Emergency flattening all equities to cash!")
        for pos in positions_list:
            sym = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            side = pos.get("side")
            close_side = "sell" if side == "long" else "buy"
            try:
                api_retry(requests.post, f"{BASE_URL}/v2/orders", json={
                    "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
                }, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                state["closed_trades_ledger"].insert(0, {
                    "symbol": sym, "entry_price": float(pos.get("avg_entry_price", 0)),
                    "exit_price": float(pos.get("current_price", 0)),
                    "exit_reason": "🚨 Portfolio Drawdown Circuit Breaker (-3.5%)",
                    "realized_pnl": float(pos.get("unrealized_pl", 0)), "timestamp": timestamp
                })
            except Exception as e:
                audit_logs.append(f"Circuit breaker close failed on {sym}: {e}")
        for order in open_orders:
            api_retry(requests.delete, f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)
        save_state(state)
        positions_list = []

    # --- EOD SQUARE-OFF (2:50 PM CT / 3:50 PM ET) ---
    if is_eod_square_off and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat at 2:50 PM CT)"
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
                "symbol": sym, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
            }
            try:
                close_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
                if close_res and close_res.status_code == 200:
                    audit_logs.append(f"LIQUIDATION SUCCESS: Closed {sym}")
                    if "closed_trades_ledger" not in state:
                        state["closed_trades_ledger"] = []
                    
                    state["closed_trades_ledger"].insert(0, {
                        "symbol": sym, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": reason_text, "realized_pnl": realized_pnl, "timestamp": timestamp
                    })
            except Exception as e:
                audit_logs.append(f"LIQUIDATION ERROR on {sym}: {e}")
        
        for order in open_orders:
            api_retry(requests.delete, f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)

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

        cache_info = current_active_cache.get(symbol, {})
        entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9930)
        prev_peak = cache_info.get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

        upper_wick_ratio = 0.0
        try:
            bar_res = api_retry(requests.get, f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=15Min&limit=5&feed=iex&start={start_date}", headers=HEADERS)
            if bar_res and bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if b_list:
                    last_c = b_list[-1]
                    c_high, c_low, c_open = float(last_c["h"]), float(last_c["l"]), float(last_c["o"])
                    c_range = max(1e-8, c_high - c_low)
                    body_top = max(c_open, current_px)
                    upper_wick_ratio = (c_high - body_top) / c_range
        except Exception:
            pass

        stop_px_calc, leash_status = calculate_stock_retention_targets(
            entry_px, is_long, current_px, entry_candle_low, peak_roe=peak_roe, upper_wick_pct=upper_wick_ratio
        )

        should_exit = False
        exit_reason = ""

        if is_long and current_px <= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Retention Ratchet Triggered ({current_roe*100:+.2f}% ROE)"

        if should_exit:
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE. Reason: {exit_reason}")
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            close_payload = {
                "symbol": symbol, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
            }
            try:
                api_retry(requests.post, f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
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

        new_active_cache[symbol] = {
            "entry_px": entry_px, "current_px": current_px, "qty": qty, "peak_roe": peak_roe,
            "entry_candle_low": entry_candle_low
        }

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
            "status": leash_status
        })

    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 3
    # High-Beta Momentum Equity Scope
    watchlist = [
        "NVDA", "TSLA", "AMD", "PLTR", "COIN", "MSTR", "HOOD", "SMCI", "AAPL", "MSFT",
        "AMZN", "GOOGL", "META", "AVGO", "QCOM", "ARM", "AMAT", "LRCX", "ORCL", "CRM",
        "NOW", "PANW", "FTNT", "SQ", "PYPL", "NET", "SNOW", "SHOP", "UBER", "ABNB",
        "RBLX", "DKNG", "NFLX"
    ]

    symbols_to_scan = [s for s in watchlist if s not in active_symbols]
    market_candidates = []
    scanned_count = 0

    if is_trading_window and not is_eod_square_off and active_count < MAX_STOCK_SLOTS:
        chunk_size = 20
        for i in range(0, len(symbols_to_scan), chunk_size):
            chunk = symbols_to_scan[i:i + chunk_size]
            symbols_param = ",".join(chunk)

            all_bars_data = {}
            page_token = None

            while True:
                url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=15Min&limit=1000&feed=iex&start={start_date}"
                if page_token:
                    url += f"&page_token={page_token}"

                try:
                    bars_res = api_retry(requests.get, url, headers=HEADERS)
                    if not bars_res or bars_res.status_code != 200:
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
                if not bars or len(bars) < 20:
                    continue

                scanned_count += 1
                closes = [float(b["c"]) for b in bars]
                highs = [float(b["h"]) for b in bars]
                lows = [float(b["l"]) for b in bars]
                vols = [float(b["v"]) for b in bars]

                curr_px = closes[-1]
                prev_high = highs[-2]
                prev_low = lows[-2]

                # Intrabar Paced RVOL Formula (15M Candle Pacing)
                bar_start_dt = datetime.fromisoformat(bars[-1]["t"].replace("Z", "+00:00"))
                elapsed_mins = max(0.5, (datetime.now(timezone.utc) - bar_start_dt).total_seconds() / 60.0)
                extrap_mult = min(3.0, 15.0 / elapsed_mins)
                paced_volume = vols[-1] * extrap_mult

                avg_vol = np.mean(vols[-12:-2]) if len(vols) >= 12 else np.mean(vols[:-1])
                vol_ratio = paced_volume / avg_vol if avg_vol > 0 else 1.0

                ross_ind = calculate_15m_ross_indicators(closes, highs, lows, vols)
                if not ross_ind:
                    continue

                ema9 = ross_ind["ema9"]
                vwap = ross_ind["vwap"]
                macd_hist = ross_ind["macd_hist"]

                required_vol_ratio = 1.80
                is_above_ema9 = curr_px > ema9
                is_above_vwap = curr_px > vwap
                is_macd_positive = macd_hist > 0.0

                is_breakout = (curr_px >= prev_high) and (vol_ratio >= required_vol_ratio)
                is_reclaim = (closes[-2] <= ema9 or closes[-2] <= vwap) and (curr_px > ema9 and curr_px > vwap) and (vol_ratio >= 1.30)

                if spy_green and is_above_ema9 and is_above_vwap and is_macd_positive and (is_breakout or is_reclaim):
                    setup_type = "HOD_BREAKOUT" if is_breakout else "BULLISH_RECLAIM"
                    score = vol_ratio * (1.5 if setup_type == "HOD_BREAKOUT" else 1.0)
                    target_limit_px = min(curr_px * 0.9985, max(ema9, vwap))

                    market_candidates.append({
                        "symbol": symbol, "close": curr_px, "limit_px": target_limit_px,
                        "is_long": True, "score": score, "entry_candle_low": prev_low, "strategy": setup_type
                    })
                    audit_logs.append(f"🔥 V4.8.9 TRIGGER [{setup_type}]: {symbol} @ ${curr_px:.2f} (Paced RVOL: {vol_ratio:.2f}x, 9EMA: PASS, VWAP: PASS)")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"V4.8.9 15M Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated triggers.")

    if not is_trading_window or is_eod_square_off:
        audit_logs.append("Execution Gate: Outside active market hours (8:30 AM - 3:00 PM CT) or EOD Square-Off active.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            limit_px = candidate["limit_px"]
            is_long = candidate["is_long"]
            e_candle_low = candidate["entry_candle_low"]
            strat_used = candidate["strategy"]

            target_usd = max(100.0, equity * 0.15)
            entry_calc_px = px if strat_used == "HOD_BREAKOUT" else limit_px
            qty = max(1, int(target_usd / entry_calc_px))
            order_side = "buy" if is_long else "sell"

            order_payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": order_side,
                "type": "market" if strat_used == "HOD_BREAKOUT" else "limit",
                "time_in_force": "day"
            }
            if strat_used != "HOD_BREAKOUT":
                order_payload["limit_price"] = str(round(limit_px, 2))

            try:
                order_res = api_retry(requests.post, f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res and order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    state["active_position_cache"][symbol] = {
                        "entry_px": entry_calc_px, "entry_candle_low": e_candle_low, "qty": qty, "strategy": strat_used
                    }
                    audit_logs.append(f"V4.8.9 ENTRY SUCCESS [{strat_used}]: Opened LONG on {qty} shares of {symbol} (~${(qty * entry_calc_px):.2f})")
                else:
                    audit_logs.append(f"ORDER REJECTED BY ALPACA [{order_res.status_code if order_res else 'ERR'}] on {symbol}: {order_res.text if order_res else 'No response'}")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")

    save_state(state)

    positions_cards = ""
    if positions_data:
        for p in positions_data:
            pnl_color = '#2e7d32' if p['pnl'] >= 0 else '#c62828'
            side_color = '#2e7d32' if p['side'] == 'LONG' else '#c62828'
            entry_px_formatted = round_sig_figs(p['entry'], 5)
            positions_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{p['symbol']}</span>
                  <span style="font-size: 10px; color: {side_color}; font-weight: 700; margin-left: 6px;">{p['side']}</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${p['pnl']:+.2f} ({p['roe']:+.2f}%)</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Val:</b> ${p['market_value']:.2f} &bull; <b>Buy:</b> ${entry_px_formatted} &bull; <b>Stop:</b> ${p['stop']}
              </div>
              <div style="font-size: 10px; color: #2e7d32; font-weight: 600;">
                <b>Status:</b> {p['status']}
              </div>
            </div>
            """
    else:
        positions_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No active positions (100% Cash Flat).</div>'

    closed_cards = ""
    closed_ledger = state.get("closed_trades_ledger", [])
    total_realized_pnl = 0.0

    if closed_ledger:
        for t in closed_ledger[:10]:
            pnl_val = float(t.get('realized_pnl', t.get('pnl_usd', 0.0)))
            total_realized_pnl += pnl_val
            pnl_color = '#2e7d32' if pnl_val >= 0 else '#c62828'
            symbol = t.get('symbol', 'N/A')
            entry_px_formatted = round_sig_figs(t.get('entry_price', 0), 5)
            exit_px_formatted = round_sig_figs(t.get('exit_price', 0), 5)
            reason = t.get('exit_reason', 'N/A')
            ts_str = t.get('timestamp', '')

            closed_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{symbol}</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${pnl_val:+.2f}</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Entry:</b> ${entry_px_formatted} &bull; <b>Exit:</b> ${exit_px_formatted}
              </div>
              <div style="font-size: 10px; color: #b45309; margin-bottom: 2px; word-break: break-word;">
                <b>Reason:</b> {reason}
              </div>
              <div style="font-size: 10px; color: #94a3b8; font-family: monospace;">
                {ts_str}
              </div>
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
          <table style="width: 100%; font-size: 11px; border-collapse: collapse;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    text_fallback = f"TR-GC-Equities-LS-01 | V4.8.9 15M Calibrated Scalper\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Total Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  </head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333; -webkit-text-size-adjust: 100%;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); box-sizing: border-box;">
      
      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff; margin: 0;">TR-GC-Equities-LS-01 | V4.8.9 15M Scalper</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; V4.8.9 Equities Active</div>
      </div>

      <div style="padding: 12px; box-sizing: border-box;">
        
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px; box-sizing: border-box;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 2px;">Total Account Equity</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${equity:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px; line-height: 1.6;">
            <div>Margin Utilization: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain: <span style="background-color: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
            <div>Lifetime P&amp;L: <b style="color: #0f172a;">${lifetime_cumulative_pnl:+,.2f}</b></div>
          </div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4; box-sizing: border-box;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active V4.8.9 Stock Calibrations</div>
          <div>&bull; <b>Stock-Calibrated Scale:</b> Fee Floor (+0.15%), Expansion (+1.50%), Moonshot (+3.00%)</div>
          <div>&bull; <b>High-Beta Watchlist:</b> Scans liquid momentum equities (MSTR, NVDA, TSLA, PLTR, COIN)</div>
          <div>&bull; <b>Hybrid Routing:</b> Market orders on breakouts; limit orders on pullbacks</div>
          <div>&bull; <b>Retention Ratchet:</b> 80%-88% peak profit locks + Climax Exhaustion Lock</div>
          <div>&bull; <b>EOD Square-Off:</b> Auto 100% cash liquidation at 2:50 PM CT</div>
        </div>

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades &amp; Exit Telemetry</div>
        {closed_cards}
        
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          TOTAL RECENT REALIZED P&amp;L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        {audit_section}

      </div>

      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Alpaca V4.8.9 Equities Scalper &bull; Managed via GitHub Actions
      </div>
    </div>
  </body>
</html>
"""

    send_html_dashboard_email(f"Alpaca Quantitative Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] V4.8.9 Equities telemetry report complete.", flush=True)

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Equities Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
