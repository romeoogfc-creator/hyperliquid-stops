import os
import json
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import log10, floor
import eth_account
import pandas as pd
import numpy as np

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS")
SECRET_KEY = os.getenv("HL_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STATE_FILE = "state.json"

VERBOSE_TEST_MODE = True

LARGE_CAP_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "LINK", "SUI", "NEAR", "APT"}

def load_state():
    default_state = {
        "cooldown_blocklist": {}, 
        "stagnation_tracker": {}, 
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_coins": [],
        "last_run_timestamp": "",
        "last_email_timestamp": 0,
        "last_scan_timestamp": 0,
        "gemini_cache": {}
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
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def api_retry(func, *args, retries=5, delay=3.0, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "Rate limit" in str(e) or "Timeout" in str(e):
                if attempt < retries - 1:
                    print(f"[WARN] Hyperliquid API rate limit hit. Pausing {delay}s before retry ({attempt+1}/{retries})...", flush=True)
                    time.sleep(delay)
                    delay *= 2.0
                else:
                    raise e
            else:
                raise e

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
        print(f"Failed to send email: {e}", flush=True)

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_gaussian_channel(closes, poles=4, period=323, mult=1.414):
    s = pd.Series(closes)
    alpha = (2.0 / (period + 1)) * (poles ** 0.5)
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

def calculate_adx(highs, lows, closes, period=14):
    try:
        df = pd.DataFrame({'high': highs, 'low': lows, 'close': closes})
        df['tr0'] = df['high'] - df['low']
        df['tr1'] = (df['high'] - df['close'].shift(1)).abs()
        df['tr2'] = (df['low'] - df['close'].shift(1)).abs()
        df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)

        df['up'] = df['high'] - df['high'].shift(1)
        df['down'] = df['low'].shift(1) - df['low']

        df['pos_dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0.0)
        df['neg_dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0.0)

        tr_smooth = df['tr'].ewm(alpha=1/period, adjust=False).mean()
        pos_dm_smooth = df['pos_dm'].ewm(alpha=1/period, adjust=False).mean()
        neg_dm_smooth = df['neg_dm'].ewm(alpha=1/period, adjust=False).mean()

        pos_di = 100 * (pos_dm_smooth / tr_smooth)
        neg_di = 100 * (neg_dm_smooth / tr_smooth)
        
        dx = 100 * (pos_di - neg_di).abs() / (pos_di + neg_di)
        adx = dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
        return adx if not np.isnan(adx) else 15.0
    except Exception:
        return 15.0

def check_gemini_macro_shield(state, now_ts, is_scan_window):
    """
    Refreshes Gemini AI assessment on 30-minute entry scan windows.
    Cascades across models on 404, 503 (high demand), or 429 (rate limit) errors.
    Reuses cached risk if Google API capacity is fully exhausted.
    """
    gemini_cache = state.get("gemini_cache", {})
    
    # 1. Reuse cache on inter-candle 5m trailing stop runs
    if not is_scan_window and "risk" in gemini_cache:
        elapsed_mins = (now_ts - float(gemini_cache.get("timestamp", 0))) / 60.0
        return gemini_cache["risk"], f"{gemini_cache['briefing']} (Refreshed {elapsed_mins:.0f}m ago)"

    if not GEMINI_API_KEY:
        return "LOW", "Gemini API key not set — Macro shield bypassed."

    # 2. Fresh check on 30m scan windows with multi-model cascade
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        model_cascade = ['gemini-3.1-flash-lite', 'gemini-3.5-flash-lite', 'gemini-3.8-flash']
        
        last_err_msg = ""
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
            except Exception as m_err:
                last_err_msg = str(m_err)
                err_str = str(m_err).upper()
                # Cascade to next model on 404, 503 (high demand), or 429 (rate limit)
                if any(code in err_str for code in ["404", "503", "429", "UNAVAILABLE", "NOT_FOUND", "RESOURCE_EXHAUSTED"]):
                    continue
                raise m_err

        # If all API models hit high demand, preserve existing cached risk level
        if "risk" in gemini_cache:
            return gemini_cache["risk"], f"{gemini_cache['briefing']} (Cached fallback during API demand spike)"
        
        return "LOW", f"Gemini Shield active — default PASS ({last_err_msg})"
    except Exception as e:
        if "risk" in gemini_cache:
            return gemini_cache["risk"], f"{gemini_cache['briefing']} (Cached fallback during API error)"
        return "LOW", f"Gemini Shield active — default PASS ({e})"

def get_btc_regime(info, now_ms):
    try:
        btc_candles = api_retry(info.candles_snapshot, name="BTC", interval="1d", startTime=now_ms - 86400000 * 5, endTime=now_ms)
        if not btc_candles or len(btc_candles) < 2:
            return "NEUTRAL", 0.0
        latest = btc_candles[-1]
        o_px = float(latest["o"])
        c_px = float(latest["c"])
        pct_change = ((c_px - o_px) / o_px) * 100
        
        if pct_change >= 0.15:
            return "GREEN", pct_change
        elif pct_change <= -0.15:
            return "RED", pct_change
        return "NEUTRAL", pct_change
    except Exception as e:
        print(f"[WARN] Failed to fetch BTC daily regime: {e}", flush=True)
        return "NEUTRAL", 0.0

def calculate_crypto_stop_price(entry_px, is_long, current_px, leverage=3.0, choppiness_index=50.0, is_ballistic=False, vol_ratio=1.0, peak_roe=0.0):
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

    leash_status = "Tight Initial Stop (-1.0% ROE)"
    
    if peak_roe >= 0.15:
        target_floor_roe = peak_roe * 0.95
        leash_status = f"⚡ Ultra-Runner 95% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.08:
        target_floor_roe = peak_roe * 0.90
        leash_status = f"🚀 Mega-Runner 90% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif vol_ratio < 0.85 and roe >= 0.010:
        target_floor_roe = max(0.001, roe - 0.001)
        leash_status = f"🔒 Volume Stall Lock (+0.1% Buffer) [{roe*100:.1f}% ROE]"
    elif peak_roe >= 0.05:
        target_floor_roe = 0.030
        leash_status = "📈 Mid-Runner Lock (+3.0% Floor)"
    elif peak_roe >= 0.035:
        target_floor_roe = 0.015
        leash_status = "🎯 Winner Lock (+1.5% Net Floor)"
    elif peak_roe >= 0.010:
        target_floor_roe = 0.000
        leash_status = "🛡️ Break-Even Lock (0.0% Floor)"
    else:
        target_floor_roe = -0.010

    if is_long:
        stop_px = entry_px * (1 + (target_floor_roe / leverage))
        is_buy_order = False
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))
        is_buy_order = True

    return stop_px, roe, target_floor_roe, is_buy_order, leash_status

def verify_5m_micro_structure(info, coin, now_ms, is_long):
    try:
        m5_candles = api_retry(info.candles_snapshot, name=coin, interval="5m", startTime=now_ms - 3600000 * 4, endTime=now_ms)
        if not m5_candles or len(m5_candles) < 6:
            return True, 50.0
        m_closes = [float(c["c"]) for c in m5_candles]
        m_opens = [float(c["o"]) for c in m5_candles]
        m_highs = [float(c["h"]) for c in m5_candles]
        m_lows = [float(c["l"]) for c in m5_candles]
        
        ci_5m = calculate_choppiness_index(m_highs, m_lows, m_closes)

        last_completed_high = m_highs[-2]
        last_completed_low = m_lows[-2]
        last_completed_close = m_closes[-2]
        last_completed_open = m_opens[-2]
        
        candle_range = last_completed_high - last_completed_low
        if candle_range > 0:
            if is_long:
                upper_wick_ratio = (last_completed_high - max(last_completed_open, last_completed_close)) / candle_range
                if upper_wick_ratio > 0.35:
                    return False, ci_5m
            else:
                lower_wick_ratio = (min(last_completed_open, last_completed_close) - last_completed_low) / candle_range
                if lower_wick_ratio > 0.35:
                    return False, ci_5m

        if is_long:
            net_progress = m_closes[-2] >= m_closes[-5]
            return net_progress, ci_5m
        else:
            net_progress = m_closes[-2] <= m_closes[-5]
            return net_progress, ci_5m

    except Exception:
        return True, 50.0

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23 Engine Started (5m Stop Ratchet / 30m Entry Scanner).")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()
    wallet = eth_account.Account.from_key(SECRET_KEY)
    
    exchange = api_retry(Exchange, wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    all_mids = api_retry(info.all_mids)
    meta = api_retry(info.meta)
    open_orders = api_retry(info.frontend_open_orders, ACCOUNT_ADDRESS)
    now_ms = int(now_ts * 1000)

    # --- 30-MINUTE CANDLE BOUNDARY CHECK FOR NEW ENTRY SCANNING ---
    current_gm_min = time.gmtime(now_ts).tm_min
    last_scan_ts = float(state.get("last_scan_timestamp", 0))
    minutes_since_last_scan = (now_ts - last_scan_ts) / 60.0
    is_30m_scan_window = (current_gm_min in [0, 1, 2, 30, 31, 32]) or (minutes_since_last_scan >= 25.0)

    # --- GEMINI MACRO SHIELD ---
    gemini_risk, gemini_briefing = check_gemini_macro_shield(state, now_ts, is_30m_scan_window)
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] {gemini_briefing}")

    # --- BTC REGIME SHIELD ---
    btc_regime, btc_change_pct = get_btc_regime(info, now_ms)
    audit_logs.append(f"BTC Directional Shield: Daily Candle is {btc_regime} ({btc_change_pct:+.2f}%).")

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

    # --- PORTFOLIO DRAWDOWN CIRCUIT BREAKER (-3.5% Loss Check) ---
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
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": exit_px,
                        "pnl_usd": pnl, "roe_pct": roe, "side": "LONG" if szi > 0 else "SHORT",
                        "exit_reason": "🚨 Portfolio Circuit Breaker (-3.5%)", "timestamp": timestamp
                    })
                    trade_closed_this_run = True
                except Exception as e:
                    audit_logs.append(f"Circuit breaker close failed on {coin}: {e}")
        state["closed_trades_ledger"] = sorted(state["closed_trades_ledger"], key=lambda x: x.get("timestamp", ""), reverse=True)[:10]
        save_state(state)
        return

    # --- EVERY 5-MIN RUN: ACTIVE POSITION TRAILING STOP MANAGEMENT ---
    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if not coin or szi == 0:
                continue

            is_long = szi > 0
            entry_px = float(pos.get("entryPx", 0))
            current_px = float(all_mids.get(coin, entry_px))
            margin_used = float(pos.get("marginUsed", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            pos_equity = margin_used + unrealized_pnl

            # STRICT DIRECTIONAL PURGE
            if btc_regime == "RED" and is_long:
                try:
                    audit_logs.append(f"🛑 DIRECTIONAL PURGE: Closing LONG on {coin} because BTC Daily is RED.")
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": ((current_px - entry_px)/entry_px*3*100), "side": "LONG",
                        "exit_reason": "🛑 BTC Bearish Flip Directional Purge", "timestamp": timestamp
                    })
                    trade_closed_this_run = True
                    continue
                except Exception as e:
                    audit_logs.append(f"Directional purge failed on {coin}: {e}")

            if btc_regime == "GREEN" and (not is_long):
                try:
                    audit_logs.append(f"🟢 DIRECTIONAL PURGE: Closing SHORT on {coin} because BTC Daily is GREEN.")
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": ((entry_px - current_px)/entry_px*3*100), "side": "SHORT",
                        "exit_reason": "🟢 BTC Bullish Flip Directional Purge", "timestamp": timestamp
                    })
                    trade_closed_this_run = True
                    continue
                except Exception as e:
                    audit_logs.append(f"Directional purge failed on {coin}: {e}")

            leverage_info = pos.get("leverage", {})
            leverage = float(leverage_info.get("value", 1.0)) if isinstance(leverage_info, dict) else 1.0
            if leverage <= 0:
                leverage = 1.0

            current_roe = (((current_px - entry_px) / entry_px) * leverage) if is_long else (((entry_px - current_px) / entry_px) * leverage)
            prev_peak = current_active_cache.get(coin, {}).get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            try:
                time.sleep(0.05)
                c_candles = api_retry(info.candles_snapshot, name=coin, interval="30m", startTime=now_ms - 86400000 * 2, endTime=now_ms)
                highs = [float(c["h"]) for c in c_candles]
                lows = [float(c["l"]) for c in c_candles]
                closes = [float(c["c"]) for c in c_candles]
                vols = [float(c.get("v", 0)) for c in c_candles]
                
                if is_long and len(highs) > 0:
                    candle_max_roe = (((max(highs[-2:]) - entry_px) / entry_px) * leverage)
                    peak_roe = max(peak_roe, candle_max_roe)
                elif (not is_long) and len(lows) > 0:
                    candle_min_roe = (((entry_px - min(lows[-2:])) / entry_px) * leverage)
                    peak_roe = max(peak_roe, candle_min_roe)

                ci = calculate_choppiness_index(highs, lows, closes)
                vol_ratio = (vols[-1] / np.mean(vols[-14:])) if len(vols) >= 14 and np.mean(vols[-14:]) > 0 else 1.0
            except Exception:
                ci = 50.0
                vol_ratio = 1.0

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, "peak_roe": peak_roe, "margin": margin_used, "leverage": leverage, "side": "LONG" if is_long else "SHORT"
            }

            stop_px_raw, current_roe, target_floor, is_buy_order, leash_status = calculate_crypto_stop_price(
                entry_px, is_long, current_px, leverage, choppiness_index=ci, vol_ratio=vol_ratio, peak_roe=peak_roe
            )
            px = round_sig_figs(stop_px_raw, 5)

            initial_risk_ref = 0.01
            r_multiple = current_roe / initial_risk_ref if initial_risk_ref > 0 else 0.0

            if current_roe < 0.01:
                state["stagnation_tracker"][coin] = state["stagnation_tracker"].get(coin, 0) + 1
            else:
                state["stagnation_tracker"][coin] = 0

            stag_count = state["stagnation_tracker"].get(coin, 0)
            audit_logs.append(f"Crypto Position: {coin} | ROE: {current_roe*100:+.2f}% (Peak: {peak_roe*100:+.2f}%) | Stop: ${px} | [{leash_status}] | Stg: {stag_count}/48")

            for order in open_orders:
                if order.get("coin") == coin and order.get("isTrigger"):
                    exchange.cancel(coin, order["oid"])

            exchange.order(
                coin, is_buy_order, abs(szi), px,
                {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True
            )

            positions_data.append({
                "bot_title": "TR-GC-Crypto-LS-23", "coin": coin,
                "side": "LONG" if is_long else "SHORT", "sz": abs(szi),
                "entry": entry_px, "current": current_px, "leverage": int(leverage),
                "collateral": margin_used, "position_usd": pos_equity,
                "pnl": unrealized_pnl, "roe": current_roe * 100,
                "r_multiple": r_multiple, "stop": px, "floor": target_floor * 100,
                "status": leash_status
            })

    previous_cache = state.get("active_position_cache", {})
    closed_coins = set(previous_cache.keys()) - active_coins

    for closed_coin in closed_coins:
        old_data = previous_cache.get(closed_coin, {})
        entry_px = old_data.get("entry_px", 0.0)
        exit_px = float(all_mids.get(closed_coin, entry_px))
        margin = old_data.get("margin", 50.0)
        lev = old_data.get("leverage", 3.0)
        szi = old_data.get("szi", 1.0)
        is_long = szi > 0 if isinstance(szi, (int, float)) else True
        
        raw_pnl = ((exit_px - entry_px) / entry_px * margin * lev) if is_long else ((entry_px - exit_px) / entry_px * margin * lev)
        roe_pct = (raw_pnl / margin * 100) if margin > 0 else 0.0

        already_logged = any(t["coin"] == closed_coin for t in state["closed_trades_ledger"][:2])
        if not already_logged:
            reason = f"🎯 Profit Lock (+{roe_pct:.1f}%)" if raw_pnl >= 0 else f"🛡️ Tight Stop Loss ({roe_pct:.1f}%)"
            state["closed_trades_ledger"].insert(0, {
                "coin": closed_coin, "entry_price": entry_px, "exit_price": exit_px,
                "pnl_usd": raw_pnl, "roe_pct": roe_pct, "side": "LONG" if is_long else "SHORT",
                "exit_reason": reason, "timestamp": timestamp
            })
            state["closed_trades_ledger"] = sorted(state["closed_trades_ledger"], key=lambda x: x.get("timestamp", ""), reverse=True)[:10]
            trade_closed_this_run = True

    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    universe = [asset["name"] for asset in meta.get("universe", [])][:100]
    market_candidates = []
    scanned_count = 0

    if is_30m_scan_window and btc_regime in ["GREEN", "RED"] and gemini_risk != "HIGH":
        state["last_scan_timestamp"] = now_ts
        audit_logs.append("⏰ 30-Minute Candle Boundary Reached: Launching Full Market Entry Scanner...")

        for coin in universe:
            if coin in active_coins or coin in ["USDC", "USDT"]:
                continue
            try:
                px = float(all_mids.get(coin, 0))
                if px <= 0:
                    continue
                
                time.sleep(0.25)
                candles = api_retry(info.candles_snapshot, name=coin, interval="30m", startTime=now_ms - 86400000 * 3, endTime=now_ms)
                if not candles or len(candles) < 50:
                    continue
                scanned_count += 1

                closes = [float(c["c"]) for c in candles]
                opens = [float(c["o"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]

                adx_30m = calculate_adx(highs[:-1], lows[:-1], closes[:-1])
                if adx_30m < 22.0:
                    continue

                upper, lower, filter_band = calculate_gaussian_channel(closes[:-1])
                comp_close = closes[-2]
                comp_open = opens[-2]
                prev_comp_close = closes[-3]
                prev_comp_open = opens[-3]
                comp_high = highs[-2]
                comp_low = lows[-2]
                
                ci_30m = calculate_choppiness_index(highs[:-1], lows[:-1], closes[:-1])
                if ci_30m > 55.0:
                    continue

                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else volumes[-3]
                comp_vol = volumes[-2]
                vol_ratio = comp_vol / avg_vol if avg_vol > 0 else 1.0

                atr = np.mean([h - l for h, l in zip(highs[-15:-1], lows[-15:-1])])

                s_closes = pd.Series(closes[:-1])
                ema20 = s_closes.ewm(span=20, adjust=False).mean().iloc[-1]
                ema50 = s_closes.ewm(span=50, adjust=False).mean().iloc[-1]

                is_green_candle = comp_close > comp_open
                recent_red_to_green = (prev_comp_close <= prev_comp_open) and is_green_candle
                has_upward_continuation = comp_close > prev_comp_close
                candle_range = comp_high - comp_low
                upper_wick_ok = True
                if candle_range > 0:
                    upper_wick = (comp_high - comp_close) / candle_range
                    if upper_wick > 0.35:
                        upper_wick_ok = False

                if btc_regime == "GREEN" and ema20 > ema50:
                    if comp_close > upper and comp_close <= upper * 1.04 and vol_ratio >= 2.2 and (recent_red_to_green or has_upward_continuation) and upper_wick_ok:
                        is_ballistic = comp_close > (upper + 1.5 * atr)
                        extension_score = max(0.0, (comp_close - upper) / upper)
                        atr_score = atr / comp_close
                        momentum_score = (extension_score + (1.5 * atr_score)) if is_ballistic else (extension_score + atr_score)

                        pass_5m, ci_5m = verify_5m_micro_structure(info, coin, now_ms, is_long=True)
                        if pass_5m and ci_5m <= 55.0:
                            candidate_obj = {
                                "coin": coin, "close": comp_close, "is_long": True, "is_ballistic": is_ballistic,
                                "score": momentum_score, "ci": ci_30m, "vol_ratio": vol_ratio, "adx": adx_30m
                            }
                            market_candidates.append(candidate_obj)
                            audit_logs.append(f"HIGH-CONVICTION LONG MATCH: {coin} @ ${comp_close:.4f} (VolRatio: {vol_ratio:.2f}, ADX: {adx_30m:.1f}, 30m CI: {ci_30m:.1f})")

                is_red_candle = comp_close < comp_open
                recent_green_to_red = (prev_comp_close >= prev_comp_open) and is_red_candle
                has_downward_continuation = comp_close < prev_comp_close
                lower_wick_ok = True
                if candle_range > 0:
                    lower_wick = (comp_close - comp_low) / candle_range
                    if lower_wick > 0.35:
                        lower_wick_ok = False

                if btc_regime == "RED" and ema20 < ema50:
                    if comp_close < lower and comp_close >= lower * 0.96 and vol_ratio >= 2.2 and (recent_green_to_red or has_downward_continuation) and lower_wick_ok:
                        is_ballistic = comp_close < (lower - 1.5 * atr)
                        extension_score = max(0.0, (lower - comp_close) / lower)
                        atr_score = atr / comp_close
                        momentum_score = (extension_score + (1.5 * atr_score)) if is_ballistic else (extension_score + atr_score)

                        pass_5m, ci_5m = verify_5m_micro_structure(info, coin, now_ms, is_long=False)
                        if pass_5m and ci_5m <= 55.0:
                            candidate_obj = {
                                "coin": coin, "close": comp_close, "is_long": False, "is_ballistic": is_ballistic,
                                "score": momentum_score, "ci": ci_30m, "vol_ratio": vol_ratio, "adx": adx_30m
                            }
                            market_candidates.append(candidate_obj)
                            audit_logs.append(f"HIGH-CONVICTION SHORT MATCH: {coin} @ ${comp_close:.4f} (VolRatio: {vol_ratio:.2f}, ADX: {adx_30m:.1f}, 30m CI: {ci_30m:.1f})")

            except Exception:
                continue
    else:
        audit_logs.append("⏸️ Inter-candle run (5m interval): Position trailing stops updated. Entry scan sleeping until next 30m mark.")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    if is_30m_scan_window:
        audit_logs.append(f"30m Scan Complete: Evaluated {scanned_count} assets. Found {len(market_candidates)} breakouts.")

    trades_executed = False
    available_slots = 6 - active_count
    if available_slots > 0 and market_candidates:
        for candidate in market_candidates[:available_slots]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            is_ballistic = candidate["is_ballistic"]
            
            assigned_leverage = 1 if coin in LARGE_CAP_COINS else 3

            target_pct = np.random.uniform(0.15, 0.17) if is_ballistic else np.random.uniform(0.12, 0.14)
            target_usd = max(50.0, account_value * target_pct)
            
            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = target_usd / px
            sz = round(raw_sz, decimals)
            if decimals == 0:
                sz = int(sz)

            try:
                try:
                    exchange.update_leverage(coin, assigned_leverage, True)
                except Exception:
                    pass

                res = exchange.market_open(coin, is_long, sz, px * (1.01 if is_long else 0.99))
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    trades_executed = True
                    audit_logs.append(f"EXECUTION SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {coin} ({assigned_leverage}x Leverage, Size: {sz})")

            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")

    if trades_executed:
        time.sleep(2.5)

    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    last_email_ts = float(state.get("last_email_timestamp", 0))
    elapsed_minutes = (now_ts - last_email_ts) / 60.0
    
    is_time_for_periodic_email = (elapsed_minutes >= 25.0)
    should_send_email = is_time_for_periodic_email or trades_executed or trade_closed_this_run

    if should_send_email:
        state["last_email_timestamp"] = now_ts
        save_state(state)

        audit_section = ""
        if VERBOSE_TEST_MODE:
            audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 11px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
            audit_section = f"""
            <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (5m Stop Ratchet / 30m Entry Scan)</div>
            <div class="table-responsive" style="overflow-x: hidden;">
              <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%; table-layout: fixed;">
                <tbody>{audit_rows}</tbody>
              </table>
            </div>
            """

        closed_ledger = sorted(state.get("closed_trades_ledger", []), key=lambda x: x.get("timestamp", ""), reverse=True)
        
        parsed_closed_rows = []
        total_realized_pnl = 0.0

        for t in closed_ledger[:10]:
            entry_p = float(t.get("entry_price", 0.0))
            exit_p = float(t.get("exit_price", 0.0))
            side = t.get("side", "LONG")
            
            if "pnl_usd" in t:
                pnl_val = float(t["pnl_usd"])
                roe_val = float(t.get("roe_pct", 0.0))
            else:
                if entry_p > 0:
                    pnl_val = ((exit_p - entry_p) / entry_p * 50.0 * 3.0) if side == "LONG" else ((entry_p - exit_p) / entry_p * 50.0 * 3.0)
                    roe_val = (pnl_val / 50.0) * 100
                else:
                    pnl_val = 0.0
                    roe_val = 0.0
            
            total_realized_pnl += pnl_val

            parsed_closed_rows.append(
                f"<tr>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{t['coin']}</td>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(entry_p, 5)}</td>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(exit_p, 5)}</td>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-weight: bold; color: {'#2e7d32' if pnl_val >= 0 else '#c62828'};'>${pnl_val:+.2f} ({roe_val:+.2f}%)</td>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; color: #b45309;'>{t['exit_reason']}</td>"
                f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 10px;'>{t['timestamp']}</td>"
                f"</tr>"
            )

        closed_rows = "".join(parsed_closed_rows) if parsed_closed_rows else "<tr><td colspan='6' style='padding: 10px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"

        if parsed_closed_rows:
            closed_rows += f"""
            <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #cbd5e1;">
                <td colspan="3" style="padding: 9px 10px; text-align: right;">TOTAL REALIZED P&L:</td>
                <td style="padding: 9px 10px; color: {'#2e7d32' if total_realized_pnl >= 0 else '#c62828'};">${total_realized_pnl:+.2f}</td>
                <td colspan="2"></td>
            </tr>
            """

        text_fallback = f"TR-GC-Crypto-LS-23 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/6"

        positions_rows = "".join([
            f"<tr>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['coin']}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['leverage']}x</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${p['collateral']:.2f}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: 600; color: #0f172a;'>${p['position_usd']:.2f}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f} ({p['roe']:+.2f}% / {p['r_multiple']:+.1f}R)</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #334155;'>${round_sig_figs(p['entry'], 5)}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #b45309;'>${p['stop']}</td>"
            f"<td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['status']}</td>"
            f"</tr>"
            for p in positions_data
        ])

        if positions_data:
            total_collateral_sum = sum(p['collateral'] for p in positions_data)
            total_position_usd_sum = sum(p['position_usd'] for p in positions_data)
            total_pnl_sum = sum(p['pnl'] for p in positions_data)
            total_roe_avg = (total_pnl_sum / total_collateral_sum * 100) if total_collateral_sum > 0 else 0.0
            positions_rows += f"""
            <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #cbd5e1;">
                <td colspan="4" style="padding: 9px 10px; text-align: right;">TOTAL UNREALIZED:</td>
                <td style="padding: 9px 10px;">${total_collateral_sum:.2f}</td>
                <td style="padding: 9px 10px;">${total_position_usd_sum:.2f}</td>
                <td style="padding: 9px 10px; color: {'#2e7d32' if total_pnl_sum >= 0 else '#c62828'};">${total_pnl_sum:+.2f} ({total_roe_avg:+.2f}%)</td>
                <td colspan="3"></td>
            </tr>
            """
        else:
            positions_rows = "<tr><td colspan='10' style='padding: 15px; text-align: center; color: #666;'>No active positions found.</td></tr>"

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
              .net-worth-subtitle {{ font-size: 11px; color: #64748b; margin-top: 4px; }}
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
                <h2>TR-GC-Crypto-LS-23 | Telemetry Dashboard</h2>
                <p>Timestamp: {timestamp} (Hybrid 5m/30m Mode Active)</p>
              </div>
              <div class="content">
                <div class="net-worth-card">
                  <div class="net-worth-title">Total Net Worth</div>
                  <div class="net-worth-value">USD ${account_value:.2f}</div>
                  <div class="net-worth-subtitle">Static Unallocated USDC Reserve: <b>${static_usdc:.2f}</b> &bull; Margin Utilization: <b>{margin_util_pct:.1f}%</b></div>
                </div>

                <div class="rules-card">
                  <div class="rules-title">&#9989; Active Guardrails (Dual-Speed Engine)</div>
                  &bull; <b>5-Minute Trailing Ratchet:</b> Evaluates open positions every 5 minutes to lock in 90%–95% of peak ROE spikes before pullbacks.<br>
                  &bull; <b>30-Minute Entry Boundary:</b> Restricts new trade scans strictly to completed 30-minute candles to eliminate entry noise.<br>
                  &bull; <b>Candle-High Peak Tracking:</b> Remembers the highest wick reached during the 30m candle to prevent giving back top profits.<br>
                  &bull; <b>Tight Initial Risk Cap:</b> Max initial stop capped at -1.0% ROE.<br>
                  &bull; <b>Dynamic Safe Leverage Profile:</b> 1x leverage on large-caps, 3x on altcoin runners.
                </div>

                <div class="section-title">Positions per Bot (USD)</div>
                <div class="table-responsive">
                  <table><thead><tr><th>Bot Title</th><th>Asset</th><th>Leverage</th><th>Side</th><th>Collateral USD</th><th>Position USD</th><th>Unrealized P&L USD</th><th>Buy Price</th><th>Stop Price</th><th>Bot Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
                </div>

                <div class="section-title">Recently Closed Trades & Exit Telemetry</div>
                <div class="table-responsive">
                  <table><thead><tr><th>Asset</th><th>Entry Price</th><th>Exit Price</th><th>Realized P&L USD ($)</th><th>Exit Reason / Catalyst</th><th>Timestamp</th></tr></thead><tbody>{closed_rows}</tbody></table>
                </div>

                {audit_section}

              </div>
              <div class="footer">Hyperliquid Autonomous Engine &bull; Managed via GitHub Actions</div>
            </div>
          </body>
        </html>
        """

        send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}", html_content, text_fallback)
    else:
        save_state(state)
        print(f"[{timestamp}] Hybrid cycle complete ({elapsed_minutes:.1f}m since last report). Skipping email dispatch.", flush=True)

if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing single-run dual-speed precision cycle...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cycle execution completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
