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

ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS")
SECRET_KEY = os.getenv("HL_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STATE_FILE = "state.json"

VERBOSE_TEST_MODE = True

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

def get_central_midnight_timestamp():
    ct_now = get_central_time()
    ct_midnight = datetime(ct_now.year, ct_now.month, ct_now.day, 0, 0, 0)
    
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    year = utc_now.year
    dst_start = datetime(year, 3, 8, 7, 0, 0)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1, 6, 0, 0)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    
    is_dst = dst_start <= utc_now < dst_end
    offset_hours = 5 if is_dst else 6
    
    utc_midnight = ct_midnight + timedelta(hours=offset_hours)
    return utc_midnight.replace(tzinfo=timezone.utc).timestamp()

def load_state():
    default_state = {
        "cooldown_blocklist": {},
        "last_traded_candle": {},
        "hibernating_until": 0,
        "stagnation_tracker": {},
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_coins": [],
        "last_run_timestamp": "",
        "last_email_timestamp": 0,
        "last_scan_timestamp": 0,
        "gemini_cache": {},
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
            print(f"Error loading state.json: {e}", flush=True)
    return default_state

def save_state(state):
    ct_now = get_central_time()
    state["last_run_timestamp"] = ct_now.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def api_retry(func, *args, retries=5, delay=4.0, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                print(f"[WARN] API Call exception ({e}). Retrying in {delay:.1f}s ({attempt+1}/{retries})...", flush=True)
                time.sleep(delay)
                delay *= 2.0
            else:
                raise e

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
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Telemetry email successfully sent to {receiver_email}", flush=True)
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)

def round_sig_figs(val, sig_figs=5):
    if val == 0 or val is None:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def check_liquidity_and_spread(info, coin, max_spread=0.0012, min_depth_usd=12000.0):
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

def sync_native_trigger_orders(exchange, info, coin, is_long, sz, stop_px, tp_px, account_address, audit_logs=None):
    try:
        clean_stop_px = float(round_sig_figs(stop_px, 5)) if stop_px else None
        clean_tp_px = float(round_sig_figs(tp_px, 5)) if tp_px else None
        is_buy = not is_long

        open_orders = api_retry(info.frontend_open_orders, account_address)
        coin_trigger_orders = []
        
        for o in open_orders:
            if o.get("coin") == coin:
                o_type = o.get("orderType", {})
                is_trig = o.get("isTrigger", False)
                if is_trig or (isinstance(o_type, dict) and "trigger" in o_type):
                    coin_trigger_orders.append(o)
                elif isinstance(o_type, str) and "trigger" in o_type.lower():
                    coin_trigger_orders.append(o)

        sl_orders = []
        for o in coin_trigger_orders:
            o_type = o.get("orderType", {})
            if isinstance(o_type, dict):
                t_info = o_type.get("trigger", {})
                if t_info.get("tpsl") == "sl":
                    sl_orders.append(o)
            elif isinstance(o_type, str) and "sl" in o_type.lower():
                sl_orders.append(o)

        needs_sl_update = True
        for o in sl_orders:
            existing_px = float(o.get("triggerPx", 0.0))
            if clean_stop_px and abs(existing_px - clean_stop_px) / max(clean_stop_px, 1e-8) < 0.0005:
                needs_sl_update = False
            else:
                try:
                    exchange.cancel(coin, int(o["oid"]))
                except Exception:
                    pass

        if needs_sl_update and clean_stop_px:
            order_type = {"trigger": {"isMarket": True, "triggerPx": clean_stop_px, "tpsl": "sl"}}
            exchange.order(coin, is_buy, sz, clean_stop_px, order_type, reduce_only=True)
            if audit_logs is not None:
                audit_logs.append(f"🛡️ NATIVE SL SYNC [{coin}]: Stop @ ${clean_stop_px:.5f}")

        tp_orders = []
        for o in coin_trigger_orders:
            o_type = o.get("orderType", {})
            if isinstance(o_type, dict):
                t_info = o_type.get("trigger", {})
                if t_info.get("tpsl") == "tp":
                    tp_orders.append(o)
            elif isinstance(o_type, str) and "tp" in o_type.lower():
                tp_orders.append(o)

        if clean_tp_px is None:
            for o in tp_orders:
                try:
                    exchange.cancel(coin, int(o["oid"]))
                except Exception:
                    pass
        else:
            needs_tp_update = True
            for o in tp_orders:
                existing_px = float(o.get("triggerPx", 0.0))
                if abs(existing_px - clean_tp_px) / max(clean_tp_px, 1e-8) < 0.0005:
                    needs_tp_update = False
                else:
                    try:
                        exchange.cancel(coin, int(o["oid"]))
                    except Exception:
                        pass

            if needs_tp_update:
                order_type = {"trigger": {"isMarket": True, "triggerPx": clean_tp_px, "tpsl": "tp"}}
                exchange.order(coin, is_buy, sz, clean_tp_px, order_type, reduce_only=True)
                if audit_logs is not None:
                    audit_logs.append(f"🎯 NATIVE TP SYNC [{coin}]: Target @ ${clean_tp_px:.5f} (2:1 R:R)")
    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠️ TPSL sync warning on {coin}: {e}")

def calculate_5m_ross_indicators(candles):
    closes = np.array([float(c["c"]) for c in candles])
    highs = np.array([float(c["h"]) for c in candles])
    lows = np.array([float(c["l"]) for c in candles])
    vols = np.array([float(c.get("v", 0.0)) for c in candles])

    if len(closes) < 30:
        return None

    df_c = pd.Series(closes)
    ema9 = df_c.ewm(span=9, adjust=False).mean().iloc[-1]

    typical_prices = (highs + lows + closes) / 3.0
    tp_v = typical_prices * vols
    cum_tp_v = np.sum(tp_v[-288:])
    cum_v = np.sum(vols[-288:])
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
        "macd_line": macd_line.iloc[-1],
        "signal_line": signal_line.iloc[-1]
    }

def analyze_live_falling_knife(curr_px, c_open, c_high, c_low, vol_ratio, is_long):
    c_range = c_high - c_low if c_high > c_low else 1e-8
    body_sz = abs(curr_px - c_open)
    body_ratio = body_sz / c_range

    if is_long:
        dist_to_low_pct = (curr_px - c_low) / c_range if c_range > 0 else 1.0
        lower_wick_ratio = (min(c_open, curr_px) - c_low) / c_range
        
        if lower_wick_ratio >= 0.40:
            return False, "Liquidity Absorption Wick Detected (Holding)"
            
        is_full_red_body = (curr_px < c_open) and (body_ratio >= 0.65) and (dist_to_low_pct <= 0.15)
        if is_full_red_body and vol_ratio >= 1.5:
            return True, f"🚨 True 5M Falling Knife (Red Body: {body_ratio*100:.0f}%, Vol: {vol_ratio:.1f}x)"
    else:
        dist_to_high_pct = (c_high - curr_px) / c_range if c_range > 0 else 1.0
        upper_wick_ratio = (c_high - max(c_open, curr_px)) / c_range
        
        if upper_wick_ratio >= 0.40:
            return False, "Liquidity Absorption Wick Detected (Holding)"
            
        is_full_green_body = (curr_px > c_open) and (body_ratio >= 0.65) and (dist_to_high_pct <= 0.15)
        if is_full_green_body and vol_ratio >= 1.5:
            return True, f"🚨 True 5M Rising Knife (Green Body: {body_ratio*100:.0f}%, Vol: {vol_ratio:.1f}x)"
            
    return False, "Normal Price Action"

def calculate_smart_exchange_targets(entry_px, is_long, current_px, entry_candle_low, entry_candle_high, peak_roe=0.0, coin="ETH"):
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    if is_long:
        # Technical Pivot Stop Anchored directly to entry_candle_low
        raw_struct = (entry_candle_low / entry_px) - 1.0 if entry_px > 0 else -0.0080
        initial_floor_roe = min(-0.0060, max(-0.0125, raw_struct))
    else:
        raw_struct = 1.0 - (entry_candle_high / entry_px) if entry_px > 0 else -0.0080
        initial_floor_roe = min(-0.0060, max(-0.0125, raw_struct))

    activation_roe = 0.0080 
    be_floor_roe = 0.0040    

    if peak_roe >= activation_roe:
        if peak_roe < 0.0150:      
            target_floor_roe = max(be_floor_roe, peak_roe * 0.50)
            leash_status = f"⚡ FEE SHIELD [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
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
            target_floor_roe = peak_roe * 0.97
            leash_status = f"🏆 MOONSHOT LOCK (97%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"🛡 STRUCTURAL STOP ({target_floor_roe*100:.2f}%)"

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
        risk_dist = abs(entry_px - stop_px)
        tp_px = entry_px + (2.0 * risk_dist)
    else:
        stop_px = entry_px * (1 - target_floor_roe)
        risk_dist = abs(entry_px - stop_px)
        tp_px = entry_px - (2.0 * risk_dist)

    return stop_px, tp_px, roe, target_floor_roe, leash_status

def check_gemini_macro_shield(state, now_ts, is_scan_window):
    gemini_cache = state.get("gemini_cache", {})
    if not is_scan_window and "risk" in gemini_cache:
        elapsed_mins = (now_ts - float(gemini_cache.get("timestamp", 0))) / 60.0
        return gemini_cache["risk"], f"{gemini_cache['briefing']} (Refreshed {elapsed_mins:.0f}m ago)"

    if not GEMINI_API_KEY:
        return "LOW", "Gemini API key not set — Macro shield bypassed."

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        model_cascade = ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite', 'gemini-3.6-flash']
        
        for model_name in model_cascade:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents='Perform a 1-sentence risk assessment for crypto markets right now. State Risk Level as LOW, MODERATE, or HIGH.'
                )
                text = response.text if response and response.text else "LOW Risk"
                risk_level = "HIGH" if "HIGH" in text.upper() else ("MODERATE" if "MODERATE" in text.upper() else "LOW")
                
                state["gemini_cache"] = {
                    "timestamp": now_ts,
                    "risk": risk_level,
                    "briefing": text.strip()
                }
                return risk_level, text.strip()
            except Exception:
                continue

        if "risk" in gemini_cache:
            return gemini_cache["risk"], f"{gemini_cache['briefing']} (Cached fallback)"
        return "LOW", "Gemini Shield active — default PASS"
    except Exception as e:
        if "risk" in gemini_cache:
            return gemini_cache["risk"], f"{gemini_cache['briefing']} (Cached fallback)"
        return "LOW", f"Gemini Shield active — default PASS ({e})"

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    ct_midnight_ts = get_central_midnight_timestamp()

    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23-V4.3 Engine Started (Limit-Clamp & Pivot-Stop Active).")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()

    last_scan_ts = float(state.get("last_scan_timestamp", 0))
    minutes_since_last_scan = (now_ts - last_scan_ts) / 60.0
    is_5m_scan_window = (minutes_since_last_scan >= 3.5)

    if now_ts < float(state.get("hibernating_until", 0)):
        remaining_hrs = (float(state["hibernating_until"]) - now_ts) / 3600.0
        print(f"[{timestamp}] 🚨 BOT IN 12H EMERGENCY HIBERNATION ({remaining_hrs:.1f}h remaining). Execution halted.", flush=True)
        return

    one_hour_ago = now_ts - 3600
    recent_losses = [
        t for t in state.get("closed_trades_ledger", [])
        if float(t.get("pnl_usd", 0)) < 0 and float(t.get("ts_sec", 0)) >= one_hour_ago
    ]
    net_rolling_loss = sum(float(t.get("pnl_usd", 0)) for t in recent_losses)

    if len(recent_losses) >= 3 and net_rolling_loss <= -1.00:
        state["hibernating_until"] = now_ts + 43200
        save_state(state)
        err_body = f"🚨 ROLLING CIRCUIT BREAKER TRIGGERED: 3+ losses totaling ${net_rolling_loss:.2f} recorded in last 60m. Bot entering 12H hibernation."
        print(f"[{timestamp}] {err_body}", flush=True)
        send_html_dashboard_email("🚨 EMERGENCY HALT: Rolling Circuit Breaker Active", f"<h3>{err_body}</h3>", err_body)
        return

    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = api_retry(Exchange, wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    
    meta_and_ctxs = api_retry(info.meta_and_asset_ctxs)
    meta = meta_and_ctxs[0]
    asset_ctxs = meta_and_ctxs[1]
    
    all_mids = api_retry(info.all_mids)
    now_ms = int(now_ts * 1000)

    gemini_risk, gemini_briefing = check_gemini_macro_shield(state, now_ts, is_5m_scan_window)
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] {gemini_briefing}")

    is_sunday = (ct_now.weekday() == 6)
    is_sunday_pre_close = is_sunday and (14 <= ct_now.hour < 19 or (ct_now.hour == 19 and ct_now.minute < 30))

    if is_sunday_pre_close:
        required_vol_ratio = 2.80
        audit_logs.append(f"🛡️ SUNDAY WEEKLY CLOSE SHIELD ACTIVE (2:00PM-7:30PM CT): Requiring RVOL >= {required_vol_ratio:.2f}x to enter.")
    else:
        required_vol_ratio = 2.40 if gemini_risk == "HIGH" else (2.10 if gemini_risk == "MODERATE" else 1.80)
        audit_logs.append(f"⚡ Pure Ross 5M Scalper Active: Target RVOL >= {required_vol_ratio:.2f}x | 9 EMA + VWAP + MACD Active")

    sz_decimals_map = {}
    for asset in meta.get("universe", []):
        coin_name = asset.get("name")
        sz_decimals_map[coin_name] = asset.get("szDecimals", 4)

    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []
    active_coins = set()
    total_margin_used = 0.0
    total_unrealized_pnl = 0.0
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}
    trade_closed_this_run = False
    closed_coins_this_run = set()

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
                px = float(all_mids.get(coin, 0.0))
                total_spot_net_worth += (total_amt * px)

    margin_summary = user_state.get("marginSummary", {})
    fallback_val = float(margin_summary.get("accountValue", 0.0))
    account_value = total_spot_net_worth if total_spot_net_worth > 0 else fallback_val

    daily_starting_dict = state.get("daily_starting_equity", {})
    if today_str not in daily_starting_dict:
        prev_days = [d for d in daily_starting_dict.keys() if d < today_str]
        if prev_days:
            daily_starting_dict[today_str] = daily_starting_dict[max(prev_days)]
        else:
            daily_starting_dict[today_str] = account_value
        state["daily_starting_equity"] = daily_starting_dict

    today_start_eq = daily_starting_dict[today_str]
    today_total_gain = account_value - today_start_eq

    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            total_unrealized_pnl += unrealized_pnl

    portfolio_pnl_pct = (total_unrealized_pnl / account_value) if account_value > 0 else 0.0
    if portfolio_pnl_pct <= -0.035:
        audit_logs.append(f"🚨 PORTFOLIO CIRCUIT BREAKER TRIGGERED: Unrealized P&L at {portfolio_pnl_pct*100:.2f}%. Flattening all positions!")
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if coin and szi != 0:
                try:
                    entry_px = float(pos.get("entryPx", 0))
                    exit_px = float(all_mids.get(coin, 0))
                    pnl = float(pos.get("unrealizedPnl", 0))
                    roe = (pnl / float(pos.get("marginUsed", 1))) * 100
                    
                    cancel_native_trigger_orders(exchange, info, coin, ACCOUNT_ADDRESS)
                    exchange.market_close(coin, slippage=0.01)
                    closed_coins_this_run.add(coin)

                    if pnl < 0:
                        state["cooldown_blocklist"][coin] = now_ts + 86400

                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": exit_px,
                        "pnl_usd": pnl, "roe_pct": roe, "side": "LONG" if szi > 0 else "SHORT",
                        "exit_reason": "🚨 Portfolio Circuit Breaker (-3.5%)", "timestamp": timestamp, "ts_sec": now_ts
                    })
                    trade_closed_this_run = True
                except Exception as e:
                    audit_logs.append(f"Circuit breaker close failed on {coin}: {e}")
        state["closed_trades_ledger"] = sorted(state["closed_trades_ledger"], key=lambda x: x.get("timestamp", ""), reverse=True)[:20]
        save_state(state)
        return

    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if not coin or szi == 0 or coin in closed_coins_this_run:
                continue

            is_long = szi > 0
            entry_px = float(pos.get("entryPx", 0))
            current_px = float(all_mids.get(coin, entry_px))
            margin_used = float(pos.get("marginUsed", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            pos_equity = abs(szi) * current_px

            current_roe = (((current_px - entry_px) / entry_px) * 1.0) if is_long else (((entry_px - current_px) / entry_px) * 1.0)
            
            should_exit = False
            exit_reason = ""
            
            cache_info = current_active_cache.get(coin, {})
            entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9920)
            entry_candle_high = cache_info.get("entry_candle_high", entry_px * 1.0080)

            try:
                c_candles = api_retry(info.candles_snapshot, name=coin, interval="5m", startTime=now_ms - 86400000, endTime=now_ms)
                closes = [float(c["c"]) for c in c_candles]
                volumes = [float(c.get("v", 0)) for c in c_candles]
                
                c_open = float(c_candles[-1]["o"])
                c_high = float(c_candles[-1]["h"])
                c_low = float(c_candles[-1]["l"])
                avg_v = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)
                v_ratio = volumes[-1] / avg_v if avg_v > 0 else 1.0

                ross_ind = calculate_5m_ross_indicators(c_candles)
                if ross_ind:
                    if is_long and (current_px < ross_ind["ema9"] or ross_ind["macd_hist"] < 0):
                        should_exit = True
                        exit_reason = f"🚨 Ross Exit Signal (Price < 9 EMA or MACD Hist < 0)"

                is_knife, knife_reason = analyze_live_falling_knife(current_px, c_open, c_high, c_low, v_ratio, is_long)
                if is_knife:
                    should_exit = True
                    exit_reason = knife_reason
            except Exception as e:
                audit_logs.append(f"Indicator calculation warning on {coin}: {e}")

            stag_map = state.get("stagnation_tracker", {})
            curr_stag = stag_map.get(coin, 0)
            if current_roe < -0.0080:
                curr_stag += 1
                if "stagnation_tracker" not in state:
                    state["stagnation_tracker"] = {}
                state["stagnation_tracker"][coin] = curr_stag
                if curr_stag >= 3:
                    should_exit = True
                    exit_reason = f"🗡️ 5M Stagnation Cut ({current_roe*100:.2f}% after {curr_stag} 5m runs)"
            else:
                if "stagnation_tracker" in state and coin in state["stagnation_tracker"]:
                    state["stagnation_tracker"][coin] = 0

            prev_peak = cache_info.get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            stop_px_calc, tp_px_calc, current_roe, target_floor_roe, leash_status = calculate_smart_exchange_targets(
                entry_px, is_long, current_px, entry_candle_low, entry_candle_high, peak_roe=peak_roe, coin=coin
            )

            if is_long and current_px <= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"

            if should_exit:
                try:
                    audit_logs.append(f"🎯 EXIT TRIGGERED: Closing {coin} LONG @ ${current_px:.5f} ({exit_reason}).")
                    cancel_native_trigger_orders(exchange, info, coin, ACCOUNT_ADDRESS)
                    time.sleep(0.15)

                    live_user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
                    live_positions = {p.get("position", {}).get("coin"): float(p.get("position", {}).get("szi", 0)) for p in live_user_state.get("assetPositions", [])}

                    if live_positions.get(coin, 0.0) == 0.0:
                        closed_coins_this_run.add(coin)
                        continue

                    exchange.market_close(coin, slippage=0.01)
                    closed_coins_this_run.add(coin)

                    if "stagnation_tracker" in state:
                        state["stagnation_tracker"].pop(coin, None)

                    if unrealized_pnl < 0 or current_roe < 0:
                        state["cooldown_blocklist"][coin] = now_ts + 86400

                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": current_roe * 100, "side": "LONG" if is_long else "SHORT",
                        "exit_reason": exit_reason, "timestamp": timestamp, "ts_sec": now_ts
                    })
                    trade_closed_this_run = True
                    continue
                except Exception as e:
                    audit_logs.append(f"Market close failed on {coin}: {e}")

            sync_native_trigger_orders(exchange, info, coin, is_long, abs(szi), stop_px_calc, tp_px_calc, ACCOUNT_ADDRESS, audit_logs)

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, 
                "margin": margin_used, "side": "LONG" if is_long else "SHORT",
                "peak_roe": peak_roe, "strategy": cache_info.get("strategy", "5M_ROSS_PULLBACK"),
                "entry_candle_low": entry_candle_low, "entry_candle_high": entry_candle_high
            }

            positions_data.append({
                "bot_title": "TR-GC-Crypto-LS-23-V4.3", "coin": coin,
                "side": "LONG" if is_long else "SHORT", "sz": abs(szi),
                "entry": entry_px, "current": current_px, "leverage": 5,
                "collateral": margin_used, "position_usd": pos_equity,
                "pnl": unrealized_pnl, "roe": current_roe * 100,
                "stop": round_sig_figs(stop_px_calc, 5),
                "tp_target": f"${round_sig_figs(tp_px_calc, 5)} (2:1 R:R)",
                "status": leash_status
            })

    prev_cache = state.get("active_position_cache", {})
    for coin, cache_data in list(prev_cache.items()):
        if coin in closed_coins_this_run or coin in active_coins:
            continue
        try:
            user_fills = api_retry(info.user_fills, ACCOUNT_ADDRESS)
            coin_fills = [f for f in user_fills if f.get("coin") == coin]
            if coin_fills:
                latest_fill = coin_fills[0]
                fill_hash = f"{coin}_{latest_fill.get('tid', latest_fill.get('time', 0))}"
                existing_hashes = [t.get("fill_hash") for t in state.get("closed_trades_ledger", []) if "fill_hash" in t]
                if fill_hash in existing_hashes:
                    continue

                exit_px = float(latest_fill.get("px", 0.0))
                entry_px = cache_data.get("entry_px", exit_px)
                side = cache_data.get("side", "LONG")
                is_long = (side == "LONG")
                pnl_usd = float(latest_fill.get("closedPnl", 0.0))
                roe_pct = ((exit_px - entry_px) / entry_px * 100) if is_long else ((entry_px - exit_px) / entry_px * 100)

                state["closed_trades_ledger"].insert(0, {
                    "coin": coin, "entry_price": entry_px, "exit_price": exit_px,
                    "pnl_usd": pnl_usd, "roe_pct": roe_pct, "side": side,
                    "exit_reason": "🎯 Native Orderbook TPSL Fill", "timestamp": timestamp, "ts_sec": now_ts,
                    "fill_hash": fill_hash
                })
                trade_closed_this_run = True
        except Exception:
            pass

    state["closed_trades_ledger"] = sorted(state.get("closed_trades_ledger", []), key=lambda x: x.get("timestamp", ""), reverse=True)[:20]
    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    market_candidates = []
    MAX_CRYPTO_SLOTS = 3
    available_slots = MAX_CRYPTO_SLOTS - active_count
    min_notional_usd = 10.50

    if is_5m_scan_window and available_slots > 0:
        state["last_scan_timestamp"] = now_ts
        audit_logs.append(f"🌐 PURE ROSS 5M SCANNER ACTIVE: Scanning Top Gappers (#20-#300+)...")

        scored_universe = []
        for idx, asset in enumerate(meta.get("universe", [])):
            coin = asset.get("name")
            if not coin or coin in active_coins or coin in ["USDC", "USDT"]:
                continue
            if now_ts < float(state.get("cooldown_blocklist", {}).get(coin, 0)) or idx < 20:
                continue

            try:
                ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                prev_px = float(ctx.get("prevDayPx", 0.0))
                mark_px = float(ctx.get("markPx", 0.0))
                
                if mark_px > 20.0:
                    continue

                if prev_px > 0 and mark_px > 0:
                    change_24h = ((mark_px - prev_px) / prev_px) * 100
                    if change_24h >= 8.0:
                        scored_universe.append({"coin": coin, "change_24h": change_24h, "rank": idx, "mark_px": mark_px})
            except Exception:
                continue

        scored_universe = sorted(scored_universe, key=lambda x: x["change_24h"], reverse=True)

        top_runners_str = ", ".join([f"{item['coin']} (+{item['change_24h']:.2f}%)" for item in scored_universe[:5]])
        if top_runners_str:
            audit_logs.append(f"📊 TOP EXPLOSIVE GAPPERS (+8%+): {top_runners_str}")
        else:
            audit_logs.append("📊 TOP GAPPERS: No coins currently meet the +8.0% daily gain threshold.")

        for item in scored_universe[:20]:
            coin = item["coin"]
            chg_24h = item["change_24h"]
            try:
                curr_live_px = float(all_mids.get(coin, 0))
                if curr_live_px <= 0 or curr_live_px > 20.0:
                    continue
                
                time.sleep(0.08)
                candles = api_retry(info.candles_snapshot, name=coin, interval="5m", startTime=now_ms - 86400000, endTime=now_ms)
                if not candles or len(candles) < 30:
                    continue

                current_candle_ts = candles[-1]["t"]
                if state.get("last_traded_candle", {}).get(coin) == current_candle_ts:
                    continue

                # TIGHT SPREAD CEILING: Max 0.12% spread cap (filters friction)
                is_liquid, spread_msg = check_liquidity_and_spread(info, coin, max_spread=0.0012, min_depth_usd=12000.0)
                if not is_liquid:
                    continue

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]

                ross_ind = calculate_5m_ross_indicators(candles)
                if not ross_ind:
                    continue

                ema9 = ross_ind["ema9"]
                vwap = ross_ind["vwap"]
                macd_hist = ross_ind["macd_hist"]

                candle_start_ms = candles[-1]["t"]
                elapsed_mins = (now_ms - candle_start_ms) / 60000.0
                live_volume_raw = float(candles[-1].get("v", 0))
                paced_volume = live_volume_raw * (5.0 / max(1.0, elapsed_mins)) if elapsed_mins >= 2.5 else live_volume_raw * 2.0
                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)
                vol_ratio = paced_volume / avg_vol if avg_vol > 0 else 1.0

                is_above_ema9 = curr_live_px > ema9
                is_above_vwap = curr_live_px > vwap
                is_macd_frontside = macd_hist > 0.0

                prev_high = highs[-2]
                prev_low = lows[-2]
                
                # LIMIT CLAMP RULE: Live price must be breaking prev_high, but <= +0.20% extended past it
                is_trigger_break = (curr_live_px > prev_high) and (curr_live_px <= prev_high * 1.0020)

                # PULLBACK REQUIREMENT: Previous candle (-2) must be a RED or Light Volume candle
                is_prev_pullback = (closes[-2] < float(candles[-2]["o"])) or (volumes[-2] <= avg_vol * 0.85)

                recent_green_vols = [volumes[i] for i in range(-6, -1) if closes[i] > float(candles[i]["o"])]
                recent_red_vols = [volumes[i] for i in range(-3, -1) if closes[i] < float(candles[i]["o"])]
                avg_green_v = np.mean(recent_green_vols) if recent_green_vols else 1.0
                avg_red_v = np.mean(recent_red_vols) if recent_red_vols else 0.0
                is_light_red_vol = (avg_red_v <= avg_green_v * 0.85)

                pullback_count = sum(1 for i in range(-4, -1) if closes[i] < float(candles[i]["o"]))
                is_valid_pullback_count = (pullback_count <= 2)

                c_range = max(highs[-1] - lows[-1], 1e-8)
                upper_wick_ratio = (highs[-1] - max(float(candles[-1]["o"]), curr_live_px)) / c_range
                is_clean_structure = (upper_wick_ratio <= 0.25)

                if item in scored_universe[:3]:
                    status_ema = "PASS" if is_above_ema9 else "FAIL"
                    status_vwap = "PASS" if is_above_vwap else "FAIL"
                    status_macd = "PASS" if is_macd_frontside else "FAIL"
                    status_rvol = "PASS" if vol_ratio >= required_vol_ratio else "FAIL"
                    audit_logs.append(f"🔍 [{coin}] Px:${curr_live_px:.4f} | 9EMA:{status_ema} | VWAP:{status_vwap} | MACD:{status_macd} | RVOL:{vol_ratio:.2f}x ({status_rvol})")

                if vol_ratio >= required_vol_ratio and is_above_ema9 and is_above_vwap and is_macd_frontside and is_trigger_break and is_prev_pullback and is_light_red_vol and is_valid_pullback_count and is_clean_structure:
                    score = vol_ratio * (1.0 + (macd_hist / max(curr_live_px, 1e-8)))

                    market_candidates.append({
                        "coin": coin, "close": curr_live_px, "is_long": True, 
                        "score": score, "candle_ts": current_candle_ts,
                        "strategy": "5M_ROSS_PULLBACK",
                        "entry_candle_low": prev_low,
                        "entry_candle_high": prev_high
                    })
            except Exception:
                continue

        if not market_candidates:
            audit_logs.append(f"ℹ️ SCAN COMPLETED: 0 coins met full Ross Cameron trigger rules on this 5M candle.")
        else:
            audit_logs.append(f"🚀 SCAN COMPLETED: {len(market_candidates)} candidate(s) triggered entry setup!")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    trades_executed = False

    if available_slots > 0 and market_candidates:
        for candidate in market_candidates[:available_slots]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            candle_ts = candidate["candle_ts"]
            strat_used = candidate["strategy"]
            e_candle_low = candidate["entry_candle_low"]
            e_candle_high = candidate["entry_candle_high"]
            
            target_notional_usd = max(min_notional_usd, account_value * 0.25 * 0.85)
            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = target_notional_usd / px

            if decimals == 0:
                sz = int(np.ceil(raw_sz))
            else:
                sz = round(np.ceil(raw_sz * (10 ** decimals)) / (10 ** decimals), decimals)

            if (sz * px) < min_notional_usd:
                needed_sz = min_notional_usd / px
                sz = int(np.ceil(needed_sz)) if decimals == 0 else round(np.ceil(needed_sz * (10 ** decimals)) / (10 ** decimals), decimals)

            if sz <= 0:
                continue

            try:
                try:
                    exchange.update_leverage(coin, 5, True)
                except Exception:
                    pass

                # Strict Limit-Clamp Fill to Eliminate Chasing Slippage
                res = exchange.market_open(coin, is_long, sz, px, slippage=0.002)
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    trades_executed = True

                    if "last_traded_candle" not in state:
                        state["last_traded_candle"] = {}
                    state["last_traded_candle"][coin] = candle_ts

                    if "active_position_cache" not in state:
                        state["active_position_cache"] = {}
                    state["active_position_cache"][coin] = {
                        "strategy": strat_used, "entry_px": px, "side": "LONG" if is_long else "SHORT",
                        "entry_candle_low": e_candle_low, "entry_candle_high": e_candle_high
                    }

                    initial_stop_px, initial_tp_px, _, _, initial_status = calculate_smart_exchange_targets(
                        px, is_long, px, e_candle_low, e_candle_high, peak_roe=0.0, coin=coin
                    )
                    sync_native_trigger_orders(exchange, info, coin, is_long, sz, initial_stop_px, initial_tp_px, ACCOUNT_ADDRESS, audit_logs)

                    positions_data.append({
                        "bot_title": "TR-GC-Crypto-LS-23-V4.3", "coin": coin,
                        "side": "LONG" if is_long else "SHORT", "sz": sz,
                        "entry": px, "current": px, "leverage": 5,
                        "collateral": (sz * px) / 5.0, "position_usd": (sz * px),
                        "pnl": 0.0, "roe": 0.0,
                        "stop": round_sig_figs(initial_stop_px, 5),
                        "tp_target": f"${round_sig_figs(initial_tp_px, 5)} (2:1 R:R)",
                        "status": initial_status
                    })
            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")

    if trades_executed:
        time.sleep(2.5)

    total_margin_used = sum(float(p.get("collateral", 0.0)) for p in positions_data)
    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    last_email_ts = float(state.get("last_email_timestamp", 0))
    elapsed_minutes = (now_ts - last_email_ts) / 60.0
    is_manual_run = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"
    should_send_email = is_manual_run or (elapsed_minutes >= 15.0) or trades_executed or trade_closed_this_run

    if should_send_email:
        state["last_email_timestamp"] = now_ts
        save_state(state)

        trades_today = [
            t for t in state.get("closed_trades_ledger", []) 
            if float(t.get("ts_sec", 0)) >= ct_midnight_ts
        ]
        total_today = len(trades_today)
        wins_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) > 0]
        losses_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) <= 0]
        
        win_count = len(wins_today)
        loss_count = len(losses_today)
        win_rate_today = (win_count / total_today * 100) if total_today > 0 else 0.0
        net_today_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_today)

        text_fallback = f"TR-GC-Crypto-LS-23-V4.3 | Pure Ross Cameron 5M Scalper\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/3"

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
        closed_ledger = sorted(state.get("closed_trades_ledger", []), key=lambda x: x.get("timestamp", ""), reverse=True)
        total_realized_pnl = 0.0

        if closed_ledger:
            for t in closed_ledger[:10]:
                pnl_val = float(t.get('pnl_usd', 0.0))
                roe_val = float(t.get('roe_pct', 0.0))
                total_realized_pnl += pnl_val
                pnl_color = '#2e7d32' if pnl_val >= 0 else '#c62828'
                side_color = '#2e7d32' if t.get('side') == 'LONG' else '#c62828'
                closed_cards += f"""
                <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px;">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <div>
                      <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{t['coin']}</span>
                      <span style="font-size: 10px; color: {side_color}; font-weight: 700; margin-left: 6px;">{t.get('side', 'LONG')}</span>
                    </div>
                    <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${pnl_val:+.2f} ({roe_val:+.2f}%)</span>
                  </div>
                  <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                    <b>Entry:</b> ${round_sig_figs(t.get('entry_price', 0), 5)} &bull; <b>Exit:</b> ${round_sig_figs(t.get('exit_price', 0), 5)}
                  </div>
                  <div style="font-size: 10px; color: #b45309; margin-bottom: 2px;">
                    <b>Reason:</b> {t.get('exit_reason', 'N/A')}
                  </div>
                  <div style="font-size: 10px; color: #94a3b8; font-family: monospace;">
                    {t.get('timestamp', '')}
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
            <div style="font-size: 12px; text-transform: uppercase; color: #d97706; margin: 18px 0 6px 0; font-weight: 700;">Live Telemetry & Audit Log</div>
            <div style="width: 100%; border: 1px solid #fde68a; border-radius: 6px; background-color: #fffbeb; margin-bottom: 15px;">
              <table style="width: 100%; font-size: 11px; border-collapse: collapse;">
                <tbody>{audit_rows}</tbody>
              </table>
            </div>
            """

        html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  </head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333; -webkit-text-size-adjust: 100%;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); box-sizing: border-box;">
      
      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff; margin: 0;">TR-GC-Crypto-LS-23 | Pure Ross Cameron 5M Scalper (V4.3)</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; Limit-Clamp &amp; Pivot-Stop Active</div>
      </div>

      <div style="padding: 12px;">
        
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 2px;">Total Net Worth</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${account_value:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px;">
            <div style="margin-bottom: 3px;">Reserve: <b style="color: #0f172a;">${static_usdc:.2f}</b> &bull; Margin: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain: <span style="background-color: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
          </div>
        </div>

        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 10px; margin-bottom: 14px;">
          <div style="font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase; margin-bottom: 4px;">📊 Today's Realized Performance ({today_str})</div>
          <div style="font-size: 11px; color: #166534; margin-bottom: 3px;">Trades: <b>{total_today}</b> &bull; Win Rate: <b>{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></div>
          <div style="font-size: 11px; font-weight: 700; color: #166534; border-top: 1px dashed #bbf7d0; padding-top: 4px; margin-top: 4px;">
            Realized P&amp;L: <span style="color: {'#15803d' if net_today_usd >= 0 else '#b91c1c'};">${net_today_usd:+.2f}</span>
          </div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active Ross Cameron V4.3 Execution Rules</div>
          <div>&bull; <b>Limit-Clamp Entry (0.20% Max):</b> Prevents buying extended candle spikes</div>
          <div>&bull; <b>Pivot Low Stop Anchor:</b> Stops anchored to candle structure; avoids artificial noise stops</div>
          <div>&bull; <b>Required Closed Pullback:</b> Forces 1 light-red pullback candle before entering</div>
          <div>&bull; <b>Spread Floor (0.12% Cap):</b> Rejects orderbooks that incur high friction</div>
        </div>

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades (Last 10 Trades)</div>
        {closed_cards}
        
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          TOTAL RECENT REALIZED P&L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        {audit_section}

      </div>
      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Hyperliquid 5M Fast Scalper Autonomous Engine &bull; V4.3 Active
      </div>
    </div>
  </body>
</html>
        """

        send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}", html_content, text_fallback)
    else:
        save_state(state)
        print(f"[{timestamp}] Execution cycle complete. Skipping email dispatch.", flush=True)

if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing 5M Pure Ross Scalper cycle...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 5M cycle completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
