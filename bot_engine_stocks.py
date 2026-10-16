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
    return utc_now + timedelta(hours=offset)

def load_state():
    default_state = {
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_symbols": [],
        "last_run_timestamp": "", 
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
            server.login(sender_email, server.sendmail(sender_email, receiver_email, msg.as_string()))
    except Exception as e:
        print(f"Failed to send email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0 or val is None:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_5m_ross_indicators(closes, highs, lows, vols):
    if len(closes) < 20:
        return None

    df_c = pd.Series(closes)
    ema9 = df_c.ewm(span=9, adjust=False).mean().iloc[-1]

    typical_prices = (np.array(highs) + np.array(lows) + np.array(closes)) / 3.0
    tp_v = typical_prices * np.array(vols)
    cum_tp_v = np.sum(tp_v)
    cum_v = np.sum(vols)
    vwap = (cum_tp_v / cum_v) if cum_v > 0 else closes[-1]

    ema12 = df_c.ewm(span=12, adjust=False).mean()
    ema26 = df_c.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = (macd_line - signal_line).iloc[-1]

    return {
        "ema9": ema9,
        "vwap": vwap,
        "macd_hist": macd_hist
    }

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Equities-LS-01 Engine Started (Pure Ross Cameron 5M Scalper, Texas CT: {ct_now.strftime('%H:%M:%S')}).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    start_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')

    spy_green, spy_open_px, spy_close_px = check_market_macro_regime()
    spy_regime_str = f"GREEN (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> LONGs Allowed" if spy_green else f"RED (Open: ${spy_open_px:.2f}, Close: ${spy_close_px:.2f}) -> SHORTs Allowed"
    audit_logs.append(f"SPY Macro Regime Shield: {spy_regime_str}")

    state = load_state()

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
    today_total_gain = equity - today_start_eq
    lifetime_cumulative_pnl = equity - 100000.0

    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open", headers=HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []

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
                requests.post(f"{BASE_URL}/v2/orders", json={
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
            requests.delete(f"{BASE_URL}/v2/orders/{order.get('id')}", headers=HEADERS)
        save_state(state)
        positions_list = []

    # --- RULE 1: PRE-CLOSE EOD SQUARE-OFF (2:40 PM CT) ---
    is_eod_square_off = (ct_now.hour == 14 and ct_now.minute >= 40) or (ct_now.hour >= 15)

    if is_eod_square_off and positions_list:
        reason_text = "EOD Square-Off (100% Cash Flat)"
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
                close_res = requests.post(f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
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

        should_exit = False
        exit_reason = ""

        cache_info = current_active_cache.get(symbol, {})
        entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9920)

        try:
            bar_res = requests.get(f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbol}&timeframe=5Min&limit=30&feed=iex&start={start_date}", headers=HEADERS)
            if bar_res.status_code == 200:
                b_list = bar_res.json().get("bars", {}).get(symbol, [])
                if len(b_list) >= 20:
                    closes_pos = [float(b["c"]) for b in b_list]
                    highs_pos = [float(b["h"]) for b in b_list]
                    lows_pos = [float(b["l"]) for b in b_list]
                    vols_pos = [float(b["v"]) for b in b_list]

                    ross_ind = calculate_5m_ross_indicators(closes_pos, highs_pos, lows_pos, vols_pos)
                    if ross_ind:
                        if is_long and (current_px < ross_ind["ema9"] or ross_ind["macd_hist"] < 0):
                            should_exit = True
                            exit_reason = f"🚨 Ross Exit Signal (Price < 9 EMA or MACD Hist < 0)"
        except Exception as e:
            audit_logs.append(f"Indicator calculation warning on {symbol}: {e}")

        if current_px <= entry_candle_low and is_long:
            should_exit = True
            exit_reason = f"🎯 Pivot Low Stop Triggered (${entry_candle_low:.2f})"

        if should_exit:
            audit_logs.append(f"EXIT TRIGGERED on {symbol} at {current_roe*100:+.2f}% ROE. Reason: {exit_reason}")
            close_side = "sell" if is_long else "buy"
            realized_pnl = (current_px - entry_px) * qty if is_long else (entry_px - current_px) * qty

            close_payload = {
                "symbol": symbol, "qty": str(abs(int(qty))), "side": close_side, "type": "market", "time_in_force": "day"
            }
            try:
                requests.post(f"{BASE_URL}/v2/orders", json=close_payload, headers=HEADERS)
                if "closed_trades_ledger" not in state:
                    state["closed_trades_ledger"] = []
                
                existing_symbols_today = [t['symbol'] for t in state["closed_trades_ledger"] if today_str in t.get("timestamp", "")]
                if symbol not in existing_symbols_today:
                    state["closed_trades_ledger"].insert(0, {
                        "symbol": symbol, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": exit_reason, "realized_pnl": realized_pnl, "timestamp": timestamp
                    })
                state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]
                active_count -= 1
                continue
            except Exception as e:
                audit_logs.append(f"Execution Failed on {symbol}: {e}")

        prev_peak = cache_info.get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

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
            "stop": round_sig_figs(entry_candle_low, 5),
            "status": "Active 5M Ross Scalp"
        })

    state["active_position_cache"] = new_active_cache
    state["previous_active_symbols"] = list(active_symbols)

    MAX_STOCK_SLOTS = 3
    is_trading_window = (8 <= ct_now.hour < 14) or (ct_now.hour == 14 and ct_now.minute < 35)

    watchlist = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "QCOM",
        "INTC", "MU", "ARM", "AMAT", "LRCX", "SMCI", "ORCL", "CRM", "NOW", "PANW", "FTNT", "PLTR",
        "COIN", "HOOD", "SQ", "PYPL", "NET", "SNOW", "SHOP", "MSTR", "UBER", "ABNB", "RBLX", "DKNG",
        "NFLX", "COST", "WMT", "HD", "DIS", "NKE", "SBUX", "BA", "CAT", "GE", "HON", "DE", "LMT",
        "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "SLB", "LLY", "UNH", "JNJ", "PFE", "ABBV",
        "TXN", "ADI", "KLAC", "MCHP", "DELL"
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
            url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=5Min&limit=50&feed=iex&start={start_date}"
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
            if not bars or len(bars) < 20:
                continue

            scanned_count += 1
            closes = [float(b["c"]) for b in bars]
            opens = [float(b["o"]) for b in bars]
            highs = [float(b["h"]) for b in bars]
            lows = [float(b["l"]) for b in bars]
            vols = [float(b["v"]) for b in bars]

            curr_px = closes[-1]
            prev_close = closes[-2]
            prev_high = highs[-2]
            prev_low = lows[-2]

            ross_ind = calculate_5m_ross_indicators(closes, highs, lows, vols)
            if not ross_ind:
                continue

            ema9 = ross_ind["ema9"]
            vwap = ross_ind["vwap"]
            macd_hist = ross_ind["macd_hist"]

            avg_vol = np.mean(vols[-12:-2]) if len(vols) >= 12 else np.mean(vols[:-1])
            vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0

            required_vol_ratio = 2.00
            is_above_ema9 = curr_px > ema9
            is_above_vwap = curr_px > vwap
            is_macd_frontside = macd_hist > 0.0

            is_trigger_break = (curr_px > prev_high) and (curr_px <= prev_high * 1.0020)
            is_prev_pullback = (closes[-2] < opens[-2]) or (vols[-2] <= avg_vol * 0.85)

            if spy_green and vol_ratio >= required_vol_ratio and is_above_ema9 and is_above_vwap and is_macd_frontside and is_trigger_break and is_prev_pullback:
                score = vol_ratio * (1.0 + (macd_hist / max(curr_px, 1e-8)))
                market_candidates.append({
                    "symbol": symbol, "close": curr_px, "is_long": True,
                    "score": score, "entry_candle_low": prev_low
                })
                audit_logs.append(f"🔥 ROSS 5M BREAKOUT TRIGGER: {symbol} @ ${curr_px:.2f} (RVOL: {vol_ratio:.2f}x, 9EMA: PASS, VWAP: PASS)")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"Pure Ross 5M Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} validated Ross triggers.")

    if not is_trading_window or is_eod_square_off:
        audit_logs.append("Execution Gate: Outside active trading hours or square-off window active.")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            e_candle_low = candidate["entry_candle_low"]

            target_usd = max(50.0, equity * 0.13)
            raw_qty = target_usd / px
            qty = max(1, int(raw_qty))
            order_side = "buy" if is_long else "sell"

            order_payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": order_side,
                "type": "market",
                "time_in_force": "day"
            }
            try:
                order_res = requests.post(f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    state["active_position_cache"][symbol] = {
                        "entry_px": px, "entry_candle_low": e_candle_low, "qty": qty
                    }
                    audit_logs.append(f"ROSS 5M ENTRY SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {qty} shares of {symbol} (~${(qty * px):.2f})")
                else:
                    audit_logs.append(f"ORDER REJECTED BY ALPACA [{order_res.status_code}] on {symbol}: {order_res.text}")
            except Exception as e:
                audit_logs.append(f"ORDER EXCEPTION on {symbol}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/{MAX_STOCK_SLOTS}). No new scalps triggered.")

    save_state(state)

    # --- MOBILE-FRIENDLY HTML DASHBOARD RENDERER ---
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
        <div style="font-size: 11px; text-transform: uppercase; color: #d97706; margin: 14px 0 6px 0; font-weight: 700;">Live Test Telemetry & Audit Log</div>
        <div style="width: 100%; border: 1px solid #fde68a; border-radius: 6px; background-color: #fffbeb; margin-bottom: 15px; box-sizing: border-box;">
          <table style="width: 100%; font-size: 11px; border-collapse: collapse;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    text_fallback = f"TR-GC-Equities-LS-01 | Pure Ross Cameron 5M Scalper\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f}\nToday's Total Gain: USD ${today_total_gain:+.2f}\nLifetime P&L: USD ${lifetime_cumulative_pnl:+.2f}"

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  </head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333; -webkit-text-size-adjust: 100%;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); box-sizing: border-box;">
      
      <!-- Header Banner -->
      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff; margin: 0;">TR-GC-Equities-LS-01 | Pure Ross Cameron 5M Scalper</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; Pure Ross Cameron 5M Active</div>
      </div>

      <div style="padding: 12px; box-sizing: border-box;">
        
        <!-- Total Account Equity Card -->
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px; box-sizing: border-box;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 2px;">Total Account Equity</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${equity:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px; line-height: 1.6;">
            <div>Margin Utilization: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain: <span style="background-color: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
            <div>Lifetime P&amp;L: <b style="color: #0f172a;">${lifetime_cumulative_pnl:+,.2f}</b></div>
          </div>
        </div>

        <!-- Guardrails Card -->
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4; box-sizing: border-box;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active Guardrails (Pure Ross Cameron 5M Engine)</div>
          <div>&bull; <b>Limit-Clamp Entry (0.20% Max):</b> Prevents buying extended candle spikes</div>
          <div>&bull; <b>Pivot Low Stop Anchor:</b> Stops anchored to candle structure; avoids artificial noise stops</div>
          <div>&bull; <b>Required Closed Pullback:</b> Forces 1 light-red pullback candle before entering</div>
          <div>&bull; <b>Ross Exit Signal:</b> Immediate exit if price &lt; 9 EMA or MACD Hist &lt; 0</div>
          <div>&bull; <b>Pre-Close EOD Square-Off:</b> Automatic 100% cash liquidation at 2:40 PM CT daily</div>
          <div>&bull; <b>Portfolio Drawdown Circuit Breaker:</b> Instantly flattens 100% to cash if open loss hits -3.5%</div>
        </div>

        <!-- Active Positions Section -->
        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Sniper Scalps</div>
        {positions_cards}

        <!-- Recently Closed Trades Section -->
        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades &amp; Exit Telemetry</div>
        {closed_cards}
        
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          TOTAL RECENT REALIZED P&amp;L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        <!-- Live Test Telemetry -->
        {audit_section}

      </div>

      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Alpaca Pure Ross Cameron 5M Engine &bull; Managed via GitHub Actions
      </div>
    </div>
  </body>
</html>
"""

    send_html_dashboard_email(f"Alpaca Quantitative Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Pure Ross Cameron 5M stock telemetry report complete.")

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Pure Ross Cameron Stock Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
