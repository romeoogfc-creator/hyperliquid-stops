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
                    print(f"[WARN] API rate limit hit. Pausing {delay}s before retry ({attempt+1}/{retries})...", flush=True)
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

def calculate_gaussian_channel(closes, poles=4, period=144, mult=1.414):
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

def calculate_atr(highs, lows, closes, period=14):
    try:
        if len(highs) < period + 1:
            return highs[-1] - lows[-1] if len(highs) > 0 else 1.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
        return np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs)
    except Exception:
        return 1.0

def calculate_moonshot_ratchet_stop(entry_px, is_long, current_px, atr_val, peak_roe=0.0):
    """
    1H ATR Volatility Buffer + Galactic Moonshot Ratchet (+300%+ Cap)
    """
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    atr_roe_buffer = (atr_val * 1.5) / entry_px if entry_px > 0 else 0.020
    atr_roe_buffer = max(0.015, min(0.040, atr_roe_buffer))  # Clamped between 1.5% and 4.0% ROE

    leash_status = f"1H ATR Noise Buffer (-{atr_roe_buffer*100:.2f}%)"
    
    # Galactic Moonshot & Parabolic Ratchet Ladder (+300% to Moon Cap)
    if peak_roe >= 3.00:  # +300%+ MOONSHOT RUNNER
        target_floor_roe = max(peak_roe * 0.95, peak_roe - 0.20)
        leash_status = f"🚀 GALACTIC MOONSHOT 95% Lock [{peak_roe*100:.0f}% Peak -> +{target_floor_roe*100:.0f}% Floor]"
    elif peak_roe >= 1.50:  # +150% Parabolic Wave
        target_floor_roe = max(peak_roe * 0.92, peak_roe - 0.12)
        leash_status = f"🌌 Parabolic Wave 92% Lock [{peak_roe*100:.0f}% Peak -> +{target_floor_roe*100:.0f}% Floor]"
    elif peak_roe >= 0.75:  # +75% Major Runner
        target_floor_roe = peak_roe * 0.90
        leash_status = f"🌕 Major Runner 90% Lock [{peak_roe*100:.0f}% Peak -> +{target_floor_roe*100:.0f}% Floor]"
    elif peak_roe >= 0.30:  # +30% Strong Trend
        target_floor_roe = max(peak_roe * 0.88, peak_roe - 0.05)
        leash_status = f"📈 Strong Trend 88% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.15:  # +15% Breakout
        target_floor_roe = peak_roe * 0.85
        leash_status = f"🚀 Breakout 85% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.03:  # +3% Winner
        target_floor_roe = max(0.02, peak_roe * 0.80)
        leash_status = f"🎯 80% Peak Lock [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
    elif peak_roe >= 0.015:  # +1.5% Winner
        target_floor_roe = 0.010
        leash_status = "🔒 Winner Lock (+1.0% Floor)"
    elif peak_roe >= 0.005:  # +0.5% Micro Breakout
        target_floor_roe = 0.0025
        leash_status = "🛡️ Scratch Lock (+0.25% Floor)"
    else:
        target_floor_roe = -atr_roe_buffer

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
    else:
        stop_px = entry_px * (1 - target_floor_roe)

    return stop_px, roe, target_floor_roe, leash_status

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
                if any(code in err_str for code in ["404", "503", "429", "UNAVAILABLE", "NOT_FOUND", "RESOURCE_EXHAUSTED"]):
                    continue
                raise m_err

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

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23 Engine Started (All-Weather Moonshot Ratchet Mode).")

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
    now_ms = int(now_ts * 1000)

    current_gm_min = time.gmtime(now_ts).tm_min
    last_scan_ts = float(state.get("last_scan_timestamp", 0))
    minutes_since_last_scan = (now_ts - last_scan_ts) / 60.0
    
    is_1h_scan_window = (current_gm_min in [0, 1, 2, 3]) or (minutes_since_last_scan >= 50.0)

    gemini_risk, gemini_briefing = check_gemini_macro_shield(state, now_ts, is_1h_scan_window)
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] {gemini_briefing}")

    btc_regime, btc_change_pct = get_btc_regime(info, now_ms)
    audit_logs.append(f"BTC Directional Shield: Daily Candle is {btc_regime} ({btc_change_pct:+.2f}%).")

    effective_regime = btc_regime
    if effective_regime == "NEUTRAL":
        effective_regime = "GREEN" if btc_change_pct >= 0 else "RED"

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
        state["closed_trades_ledger"] = sorted(state["closed_trades_ledger"], key=lambda x: x.get("timestamp", ""), reverse=True)[:20]
        save_state(state)
        return

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

            current_roe = (((current_px - entry_px) / entry_px) * 1.0) if is_long else (((entry_px - current_px) / entry_px) * 1.0)
            
            should_exit = False
            exit_reason = ""
            atr_val = 1.0

            try:
                c_candles = api_retry(info.candles_snapshot, name=coin, interval="1h", startTime=now_ms - 86400000 * 5, endTime=now_ms)
                closes = [float(c["c"]) for c in c_candles]
                highs = [float(c["h"]) for c in c_candles]
                lows = [float(c["l"]) for c in c_candles]
                
                atr_val = calculate_atr(highs, lows, closes)
                upper_band, lower_band, filter_line = calculate_gaussian_channel(closes)

                # 1H Gaussian Band Invalidation Exit
                if is_long and current_px < upper_band:
                    should_exit = True
                    exit_reason = f"📉 Gaussian Upper Band Invalidation (${current_px:.4f} < ${upper_band:.4f})"
                elif (not is_long) and current_px > filter_line:
                    should_exit = True
                    exit_reason = f"📈 Gaussian Filter Invalidation (${current_px:.4f} > ${filter_line:.4f})"
            except Exception as e:
                audit_logs.append(f"Channel calculation warning on {coin}: {e}")

            if btc_regime == "RED" and is_long:
                should_exit = True
                exit_reason = "🛑 BTC Daily Bearish Flip Purge"
            elif btc_regime == "GREEN" and (not is_long):
                should_exit = True
                exit_reason = "🟢 BTC Daily Bullish Flip Purge"

            # Track Peak ROE exclusively after live entry
            prev_peak = current_active_cache.get(coin, {}).get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            stop_px_calc, current_roe, target_floor_roe, leash_status = calculate_moonshot_ratchet_stop(
                entry_px, is_long, current_px, atr_val, peak_roe=peak_roe
            )

            if is_long and current_px <= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"
            elif (not is_long) and current_px >= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"

            if should_exit:
                try:
                    audit_logs.append(f"🎯 EXIT TRIGGERED: Closing {coin} {'LONG' if is_long else 'SHORT'} @ ${current_px:.5f} ({exit_reason}).")
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": current_roe * 100, "side": "LONG" if is_long else "SHORT",
                        "exit_reason": exit_reason, "timestamp": timestamp
                    })
                    trade_closed_this_run = True
                    continue
                except Exception as e:
                    audit_logs.append(f"Market close failed on {coin}: {e}")

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, 
                "margin": margin_used, "side": "LONG" if is_long else "SHORT",
                "peak_roe": peak_roe
            }

            positions_data.append({
                "bot_title": "TR-GC-Crypto-LS-23", "coin": coin,
                "side": "LONG" if is_long else "SHORT", "sz": abs(szi),
                "entry": entry_px, "current": current_px, "leverage": 1,
                "collateral": margin_used, "position_usd": pos_equity,
                "pnl": unrealized_pnl, "roe": current_roe * 100,
                "stop": round_sig_figs(stop_px_calc, 5),
                "status": leash_status
            })

    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    universe = [asset["name"] for asset in meta.get("universe", [])][:100]
    market_candidates = []
    scanned_count = 0

    MAX_CRYPTO_SLOTS = 2
    available_slots = MAX_CRYPTO_SLOTS - active_count

    if gemini_risk == "HIGH":
        base_sizing_usd = 10.0
    else:
        base_sizing_usd = 15.0

    if is_1h_scan_window and available_slots > 0:
        state["last_scan_timestamp"] = now_ts
        audit_logs.append(f"⏰ 1-Hour Candle Boundary Reached: Scanning (BTC Tilt: {effective_regime}, Target Size: ${base_sizing_usd:.0f})...")

        for coin in universe:
            if coin in active_coins or coin in ["USDC", "USDT"]:
                continue
            try:
                px = float(all_mids.get(coin, 0))
                if px <= 0:
                    continue
                
                time.sleep(0.15)
                candles = api_retry(info.candles_snapshot, name=coin, interval="1h", startTime=now_ms - 86400000 * 5, endTime=now_ms)
                if not candles or len(candles) < 50:
                    continue
                scanned_count += 1

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]

                ci_1h = calculate_choppiness_index(highs[:-1], lows[:-1], closes[:-1])
                if ci_1h > 58.0:
                    continue

                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else volumes[-3]
                comp_vol = volumes[-2]
                vol_ratio = comp_vol / avg_vol if avg_vol > 0 else 1.0
                if vol_ratio < 1.1:
                    continue

                upper, lower, filter_band = calculate_gaussian_channel(closes[:-1])
                comp_close = closes[-2]
                prev_comp_close = closes[-3]

                if effective_regime == "GREEN":
                    if comp_close > upper and prev_comp_close <= upper:
                        market_candidates.append({
                            "coin": coin, "close": comp_close, "is_long": True, 
                            "score": (comp_close - upper) / upper, "ci": ci_1h, "vol_ratio": vol_ratio
                        })
                        audit_logs.append(f"1H BREAKOUT MATCH: {coin} @ ${comp_close:.4f} (VolRatio: {vol_ratio:.2f}x, CI: {ci_1h:.1f})")

                if effective_regime == "RED":
                    if comp_close < filter_band and prev_comp_close >= filter_band:
                        market_candidates.append({
                            "coin": coin, "close": comp_close, "is_long": False, 
                            "score": (filter_band - comp_close) / filter_band, "ci": ci_1h, "vol_ratio": vol_ratio
                        })
                        audit_logs.append(f"1H BREAKDOWN MATCH: {coin} @ ${comp_close:.4f} (VolRatio: {vol_ratio:.2f}x, CI: {ci_1h:.1f})")

            except Exception:
                continue

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)

    trades_executed = False
    if available_slots > 0 and market_candidates:
        for candidate in market_candidates[:available_slots]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            
            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = base_sizing_usd / px
            sz = round(raw_sz, decimals)
            if decimals == 0:
                sz = int(sz)

            try:
                try:
                    exchange.update_leverage(coin, 1, True)
                except Exception:
                    pass

                res = exchange.market_open(coin, is_long, sz, px * (1.01 if is_long else 0.99))
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    trades_executed = True
                    audit_logs.append(f"1H EXECUTION SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {coin} (Size: {sz} ~${base_sizing_usd:.2f})")

            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")

    if trades_executed:
        time.sleep(2.5)

    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    last_email_ts = float(state.get("last_email_timestamp", 0))
    elapsed_minutes = (now_ts - last_email_ts) / 60.0
    
    is_time_for_periodic_email = (elapsed_minutes >= 25.0)
    should_send_email = is_time_for_periodic_email or trades_executed or trade_closed_this_run or is_1h_scan_window

    if should_send_email:
        state["last_email_timestamp"] = now_ts
        save_state(state)

        cutoff_ts = now_ts - 86400
        trades_24h = []
        for t in state.get("closed_trades_ledger", []):
            t_str = str(t.get("timestamp", timestamp)).replace("Z", "").replace("T", " ")
            try:
                t_ts = time.mktime(time.strptime(t_str, "%Y-%m-%d %H:%M:%S"))
                if t_ts >= cutoff_ts:
                    trades_24h.append(t)
            except Exception:
                trades_24h.append(t)

        total_24h = len(trades_24h)
        wins_24h = [t for t in trades_24h if float(t.get("pnl_usd", 0)) > 0]
        losses_24h = [t for t in trades_24h if float(t.get("pnl_usd", 0)) <= 0]
        
        win_count = len(wins_24h)
        loss_count = len(losses_24h)
        win_rate_24h = (win_count / total_24h * 100) if total_24h > 0 else 0.0
        
        avg_win_roe = (sum(float(t.get("roe_pct", 0)) for t in wins_24h) / win_count) if win_count > 0 else 0.0
        avg_win_usd = (sum(float(t.get("pnl_usd", 0)) for t in wins_24h) / win_count) if win_count > 0 else 0.0
        
        avg_loss_roe = (sum(float(t.get("roe_pct", 0)) for t in losses_24h) / loss_count) if loss_count > 0 else 0.0
        avg_loss_usd = (sum(float(t.get("pnl_usd", 0)) for t in losses_24h) / loss_count) if loss_count > 0 else 0.0
        
        net_24h_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_24h)

        text_fallback = f"TR-GC-Crypto-LS-23 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/2"

        summary_card_html = f"""
        <div class="summary-card">
          <div class="summary-title">📊 24-Hour Performance Test Summary (Moonshot Ratchet Mode)</div>
          <table style="width: 100%; border-collapse: collapse; margin-bottom: 8px;">
            <tr>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Total Trades (24h): <b style="color: #0f172a;">{total_24h}</b></td>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Win Ratio: <b style="color: #0f172a;">{win_count}W / {loss_count}L ({win_rate_24h:.1f}%)</b></td>
            </tr>
            <tr>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Avg Win: <b style="color: #15803d;">+{avg_win_roe:.2f}% (${avg_win_usd:+.2f})</b></td>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Avg Loss: <b style="color: #b91c1c;">{avg_loss_roe:.2f}% (${avg_loss_usd:+.2f})</b></td>
            </tr>
          </table>
          <div class="summary-net">
            Net 24H Realized P&L: <span class="{'win-color' if net_24h_usd >= 0 else 'loss-color'}">${net_24h_usd:+.2f}</span>
          </div>
        </div>
        """

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

        closed_ledger = sorted(state.get("closed_trades_ledger", []), key=lambda x: x.get("timestamp", ""), reverse=True)
        
        parsed_closed_rows = []
        total_realized_pnl = 0.0

        for t in closed_ledger[:10]:
            entry_p = float(t.get("entry_price", 0.0))
            exit_p = float(t.get("exit_price", 0.0))
            side = t.get("side", "LONG")
            
            pnl_val = float(t.get("pnl_usd", 0.0))
            roe_val = float(t.get("roe_pct", 0.0))
            total_realized_pnl += pnl_val

            parsed_closed_rows.append(
                f"<tr>"
                f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>{t['coin']}<br><span style='font-size: 10px; color: {'#2e7d32' if side == 'LONG' else '#c62828'}; font-weight: 600;'>({side})</span></td>"
                f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 10px;'>${round_sig_figs(entry_p, 5)}<br>&rarr; ${round_sig_figs(exit_p, 5)}</td>"
                f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: {'#2e7d32' if pnl_val >= 0 else '#c62828'}; font-weight: bold;'>${pnl_val:+.2f}<br><span style='font-size: 10px;'>({roe_val:+.2f}%)</span></td>"
                f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 10px;'><span style='color: #b45309; font-weight: 600;'>{t['exit_reason']}</span><br><span style='color: #94a3b8; font-size: 9px; font-family: monospace;'>{t.get('timestamp', '')}</span></td>"
                f"</tr>"
            )

        closed_rows = "".join(parsed_closed_rows) if parsed_closed_rows else "<tr><td colspan='4' style='padding: 12px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"

        positions_rows = "".join([
            f"<tr>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['coin']}<br><span style='font-size: 10px; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']} ({p['leverage']}x)</span></td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 11px; font-weight: 600;'>${p['position_usd']:.2f}<br><span style='font-size: 9px; color: #64748b; font-weight: normal;'>Cost: ${p['collateral']:.2f}</span></td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f}<br><span style='font-size: 10px;'>({p['roe']:+.2f}%)</span></td>"
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 10px;'><span style='color: #b45309; font-weight: bold; font-family: monospace;'>${p['stop']}</span><br><span style='color: #2e7d32; font-weight: 600;'>{p['status']}</span></td>"
            f"</tr>"
            for p in positions_data
        ])

        if not positions_data:
            positions_rows = "<tr><td colspan='4' style='padding: 12px; text-align: center; color: #666;'>No active positions found.</td></tr>"

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
              .net-worth-subtitle {{ font-size: 10px; color: #64748b; margin-top: 4px; }}
              
              .summary-card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 12px; margin-bottom: 12px; }}
              .summary-title {{ font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 0.5px; }}
              .summary-net {{ font-size: 11px; font-weight: 700; color: #166534; border-top: 1px dashed #bbf7d0; padding-top: 6px; margin-top: 2px; }}
              
              .win-color {{ color: #15803d !important; }}
              .loss-color {{ color: #b91c1c !important; }}

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
                <h2>TR-GC-Crypto-LS-23 | Telemetry Dashboard</h2>
                <p>Timestamp: {timestamp} (Moonshot Ratchet + ATR Buffer Active)</p>
              </div>
              <div class="content">
                <div class="net-worth-card">
                  <div class="net-worth-title">Total Net Worth</div>
                  <div class="net-worth-value">USD ${account_value:.2f}</div>
                  <div class="net-worth-subtitle">Reserve: <b>${static_usdc:.2f}</b> &bull; Margin: <b>{margin_util_pct:.1f}%</b></div>
                </div>

                {summary_card_html}

                <div class="rules-card">
                  <div class="rules-title">&#9989; Active Guardrails (Moonshot Ratchet Engine)</div>
                  &bull; <b>Galactic Moonshot Ratchet (+300%+ Cap):</b> Locks 80% to 95% of peak gains on runners<br>
                  &bull; <b>1H ATR Volatility Buffer (1.5x ATR):</b> Provides noise room while cutting real losses<br>
                  &bull; <b>Strict Chop Filter (CI &le; 58.0):</b> Rejects coins in sideways ranging consolidation<br>
                  &bull; <b>Unblocked Micro Sizing ($10–$15):</b> Keeps test loss capped at pennies ($\approx \$0.15$)
                </div>

                <div class="section-title">Positions per Bot (USD)</div>
                <div class="table-responsive">
                  <table>
                    <thead>
                      <tr>
                        <th style="width: 25%;">Asset</th>
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
                        <th style="width: 22%;">Asset</th>
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
