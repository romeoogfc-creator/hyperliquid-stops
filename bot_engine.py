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

# Google GenAI SDK Imports
from google import genai
from google.genai import types

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
        "ai_shield_cache": {}
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
            print(f"Error loading state.json: {e}")
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
                    print(f"[WARN] Hyperliquid API rate limit hit. Pausing {delay}s before retry ({attempt+1}/{retries})...")
                    time.sleep(delay)
                    delay *= 2.0
                else:
                    raise e
            else:
                raise e

def run_gemini_market_shield():
    state = load_state()
    cached_shield = state.get("ai_shield_cache", {})
    current_time = time.time()
    cache_epoch = cached_shield.get("cache_epoch", 0)
    
    # Adaptive TTL: Re-check every 2 hours if HIGH risk, or every 4 hours if LOW/MODERATE risk.
    current_risk = cached_shield.get("risk_level", "UNKNOWN")
    ttl_seconds = 7200 if current_risk == "HIGH" else 14400

    if cached_shield and (current_time - cache_epoch < ttl_seconds) and "risk_level" in cached_shield:
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
        Perform a live real-time web search for breaking cryptocurrency market news, BTC price momentum, regulatory actions, 
        FOMC/interest rate updates, or sudden exchange incidents from the last 1-2 hours.
        Determine if there is extreme high-risk volatility or black-swan risk that could cause sudden whipsaws in perp futures.
        Provide a 2-sentence executive summary of current market sentiment and key catalysts for the email dashboard.
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
                result["cache_epoch"] = time.time()
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
        "ai_market_brief": "Market scanning operating normally under standard quantitative rules."
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

def calculate_crypto_stop_price(entry_px, is_long, current_px, leverage=5.0, choppiness_index=50.0, is_ballistic=False, vol_ratio=1.0, peak_roe=0.0, btc_regime_green=True):
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

    leash_status = "Tiered Sniper Stop"
    
    # --- MACRO REGIME MISMATCH GUARD (Blood-Bath Protection) ---
    regime_mismatch = (is_long and not btc_regime_green) or (not is_long and btc_regime_green)

    if regime_mismatch:
        target_floor_roe = max(peak_roe - 0.001, 0.0) if peak_roe > 0 else 0.0
        leash_status = "🚨 Macro Regime Micro-Lock (Blood-Bath Defense)"
    else:
        # --- TIERED MEGA-RUNNER TRAILING PEAK FOLLOWER ---
        if peak_roe >= 0.15:
            target_floor_roe = peak_roe - 0.010
            leash_status = f"🚀 Mega-Runner Leash [{peak_roe*100:.1f}% Peak]"
        elif peak_roe >= 0.05:
            target_floor_roe = peak_roe - 0.005
            leash_status = f"📈 Mid-Trend Peak Floor [{peak_roe*100:.1f}% Peak]"
        elif peak_roe >= 0.004:
            if vol_ratio < 0.8:
                target_floor_roe = max(peak_roe - 0.001, 0.0)
                leash_status = "⚡ Volume Stall Snap (+0.1% Buffer)"
            else:
                target_floor_roe = peak_roe - 0.003
                leash_status = f"🎯 Quick Profit Lock [{peak_roe*100:.1f}% Peak]"
        else:
            if roe >= 0.01:
                target_floor_roe = 0.0
                leash_status = "Break-Even Lock (+1.0% Trigger)"
            elif choppiness_index > 58.0:
                target_floor_roe = -0.010
                leash_status = "Tight Choppy Defense Stop"
            elif is_ballistic:
                target_floor_roe = -0.015
                leash_status = "Ballistic Stop"
            else:
                target_floor_roe = -0.010
                leash_status = "Tight Trend Defense Stop"

    if is_long:
        stop_px = entry_px * (1 + (target_floor_roe / leverage))
        is_buy_order = False
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))
        is_buy_order = True

    return stop_px, roe, target_floor_roe, is_buy_order, leash_status

def check_btc_daily_candle(info):
    try:
        now_ms = int(time.time() * 1000)
        candles = api_retry(info.candles_snapshot, name="BTC", interval="1d", startTime=now_ms - 86400000 * 5, endTime=now_ms)
        if candles and len(candles) > 0:
            latest = candles[-1]
            o = float(latest.get("o", 0))
            c = float(latest.get("c", 0))
            return c >= o, o, c
    except Exception:
        pass
    return True, 0.0, 0.0

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23 Engine Started (All-Weather Resilient Guard Active).")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    ai_shield = run_gemini_market_shield()
    risk_level = ai_shield.get("risk_level", "UNKNOWN")
    
    is_high_risk = (risk_level == "HIGH")
    ai_risk_status = f"BLOCKED (High Risk)" if is_high_risk else f"PASS ({risk_level} Risk - Trading Active)"
    audit_logs.append(f"Gemini AI Shield: [{risk_level}] {ai_shield.get('reason', '')} -> {ai_risk_status}")

    state = load_state()
    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    all_mids = api_retry(info.all_mids)
    meta = api_retry(info.meta)
    open_orders = api_retry(info.frontend_open_orders, ACCOUNT_ADDRESS)
    now_ms = int(time.time() * 1000)

    sz_decimals_map = {}
    for asset in meta.get("universe", []):
        coin_name = asset.get("name")
        sz_decimals_map[coin_name] = asset.get("szDecimals", 4)

    btc_green, btc_open, btc_close = check_btc_daily_candle(info)
    regime_str = f"GREEN (Open: ${btc_open:.2f}, Close: ${btc_close:.2f}) -> LONGs Allowed" if btc_green else f"RED (Open: ${btc_open:.2f}, Close: ${btc_close:.2f}) -> SHORTs Allowed"
    audit_logs.append(f"BTC Regime Check: {regime_str}")

    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []
    active_coins = set()
    total_margin_used = 0.0
    total_unrealized_pnl = 0.0
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}

    # Pre-calculate account equity for circuit breaker check
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

    # --- NEW LAYER 1: GLOBAL PORTFOLIO DRAWDOWN CIRCUIT BREAKER (-3.5% Loss Check) ---
    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            total_unrealized_pnl += unrealized_pnl

    portfolio_pnl_pct = (total_unrealized_pnl / account_value) if account_value > 0 else 0.0
    if portfolio_pnl_pct <= -0.035:
        audit_logs.append(f"🚨 PORTFOLIO CIRCUIT BREAKER TRIGGERED: Unrealized P&L at {portfolio_pnl_pct*100:.2f}%. Emergency flattening all positions to cash!")
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if coin and szi != 0:
                try:
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": float(pos.get("entryPx", 0)),
                        "exit_price": float(all_mids.get(coin, 0)),
                        "exit_reason": "🚨 Portfolio Drawdown Circuit Breaker (-3.5%)",
                        "timestamp": timestamp
                    })
                except Exception as e:
                    audit_logs.append(f"Circuit breaker close failed on {coin}: {e}")
        save_state(state)
        return

    # --- STANDARD POSITION PROCESSING & NEW LAYERS ---
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

            leverage_info = pos.get("leverage", {})
            leverage = float(leverage_info.get("value", 1.0)) if isinstance(leverage_info, dict) else 1.0
            if leverage <= 0:
                leverage = 1.0

            current_roe = ((current_px - entry_px) / entry_px) if is_long else ((entry_px - current_px) / entry_px)
            prev_peak = current_active_cache.get(coin, {}).get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            # --- FETCH TECHNICAL METRICS (CI & Volume) ---
            try:
                time.sleep(0.05)
                c_candles = api_retry(info.candles_snapshot, name=coin, interval="30m", startTime=now_ms - 86400000 * 2, endTime=now_ms)
                highs = [float(c["h"]) for c in c_candles]
                lows = [float(c["l"]) for c in c_candles]
                closes = [float(c["c"]) for c in c_candles]
                vols = [float(c.get("v", 0)) for c in c_candles]
                ci = calculate_choppiness_index(highs, lows, closes)
                vol_ratio = (vols[-1] / np.mean(vols[-14:])) if len(vols) >= 14 and np.mean(vols[-14:]) > 0 else 1.0
            except Exception:
                ci = 50.0
                vol_ratio = 1.0

            # --- INSTANT BLOOD-BATH FORCE-CLOSE GUARD ---
            regime_mismatch = (is_long and not btc_green) or (not is_long and btc_green)
            if regime_mismatch:
                try:
                    audit_logs.append(f"🚨 BLOOD-BATH DEFENSE: Forcing immediate market close on {coin} due to BTC regime flip!")
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": "🚨 Blood-Bath Regime Force Exit", "timestamp": timestamp
                    })
                    state["closed_trades_ledger"] = state["closed_trades_ledger"][:10]
                    continue 
                except Exception as e:
                    audit_logs.append(f"Failed to force close {coin}: {e}")

            # --- NEW LAYER 2: ACTIVE POSITION CHOP PURGE (CI > 60.0 and Flat/Negative) ---
            if ci > 60.0 and current_roe < 0.005:
                try:
                    audit_logs.append(f"🚨 CHOP PURGE: Closing {coin} immediately due to dead chop (CI: {ci:.1f}, ROE: {current_roe*100:+.2f}%)")
                    exchange.market_close(coin)
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "exit_reason": f"🚨 High Choppiness Chop Purge (CI: {ci:.1f})", "timestamp": timestamp
                    })
                    state["closed_trades_ledger"] = state["closed_trades_ledger"][:10]
                    continue
                except Exception as e:
                    audit_logs.append(f"Chop purge failed on {coin}: {e}")

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, "peak_roe": peak_roe
            }

            stop_px_raw, current_roe, target_floor, is_buy_order, leash_status = calculate_crypto_stop_price(
                entry_px, is_long, current_px, leverage, choppiness_index=ci, vol_ratio=vol_ratio, peak_roe=peak_roe, btc_regime_green=btc_green
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
        
        already_logged = any(t["coin"] == closed_coin for t in state["closed_trades_ledger"][:2])
        if not already_logged:
            state["closed_trades_ledger"].insert(0, {
                "coin": closed_coin, "entry_price": entry_px, "exit_price": exit_px,
                "exit_reason": "Tiered Profit Lock / Rinse & Repeat", "timestamp": timestamp
            })
            state["closed_trades_ledger"] = state["closed_trades_ledger"][:10]

    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    universe = [asset["name"] for asset in meta.get("universe", [])][:100]
    market_candidates = []
    smart_queue_candidates = []
    scanned_count = 0

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

            atr = np.mean([h - l for h, l in zip(highs[-14:], lows[-14:])])

            if btc_green:
                is_ballistic = current_close > (upper + 1.5 * atr)
                extension_score = max(0.0, (current_close - upper) / upper)
                atr_score = atr / current_close
                momentum_score = (extension_score + (1.5 * atr_score)) if is_ballistic else (extension_score + atr_score)
                
                candidate_obj = {
                    "coin": coin, "close": current_close, "is_long": True, "is_ballistic": is_ballistic,
                    "score": momentum_score, "ci": ci, "vol_ratio": vol_ratio
                }
                smart_queue_candidates.append(candidate_obj)

                is_green_candle = current_close > current_open
                has_upward_continuation = current_close > prev_close

                if current_close > upper and current_close <= upper * 1.04 and vol_ratio >= 0.7 and is_green_candle and has_upward_continuation:
                    market_candidates.append(candidate_obj)
                    audit_logs.append(f"CRYPTO MATCH LONG (Confirmed): {coin} @ ${current_close:.4f} (VolRatio: {vol_ratio:.2f}, CI: {ci:.1f})")
            else:
                is_ballistic = current_close < (lower - 1.5 * atr)
                extension_score = max(0.0, (lower - current_close) / lower)
                atr_score = atr / current_close
                momentum_score = (extension_score + (1.5 * atr_score)) if is_ballistic else (extension_score + atr_score)

                candidate_obj = {
                    "coin": coin, "close": current_close, "is_long": False, "is_ballistic": is_ballistic,
                    "score": momentum_score, "ci": ci, "vol_ratio": vol_ratio
                }
                smart_queue_candidates.append(candidate_obj)

                is_red_candle = current_close < current_open
                has_downward_continuation = current_close < prev_close

                if current_close < lower and current_close >= lower * 0.96 and vol_ratio >= 0.7 and is_red_candle and has_downward_continuation:
                    market_candidates.append(candidate_obj)
                    audit_logs.append(f"CRYPTO MATCH SHORT (Confirmed): {coin} @ ${current_close:.4f} (VolRatio: {vol_ratio:.2f}, CI: {ci:.1f})")
        except Exception:
            continue

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    smart_queue_sorted = sorted(smart_queue_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"Crypto Scan Complete (30m Interval): Evaluated {scanned_count} assets. Found {len(market_candidates)} confirmed breakouts.")

    trades_executed = False
    if is_high_risk:
        audit_logs.append(f"Execution Gate: BLOCKED BY GEMINI AI SHIELD (High Risk Detected).")
    elif active_count < 6 and market_candidates:
        for candidate in market_candidates[: (6 - active_count)]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            is_ballistic = candidate["is_ballistic"]
            
            target_pct = np.random.uniform(0.15, 0.17) if is_ballistic else np.random.uniform(0.12, 0.14)
            target_usd = max(50.0, account_value * target_pct)
            
            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = target_usd / px
            sz = round(raw_sz, decimals)
            if decimals == 0:
                sz = int(sz)

            try:
                try:
                    exchange.update_leverage(coin, 5, True)
                except Exception:
                    pass

                res = exchange.market_open(coin, is_long, sz, px * (1.01 if is_long else 0.99))
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    trades_executed = True
                    audit_logs.append(f"EXECUTION SUCCESS: Opened {'LONG' if is_long else 'SHORT'} on {coin} (Size: {sz})")
            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")

    if trades_executed:
        time.sleep(2.5)

    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    # --- NEW LAYER 3: ACCELERATED STAGNATION ROTATION (Cut after 6 runs / 3 hours if flat) ---
    if active_count == 6:
        unprotected_trades = [p for p in positions_data if p["roe"] < 1.0]
        if unprotected_trades:
            stagnant_trade = max(unprotected_trades, key=lambda p: state["stagnation_tracker"].get(p["coin"], 0))
            coin_to_rotate = stagnant_trade["coin"]
            if state["stagnation_tracker"].get(coin_to_rotate, 0) >= 6:
                try:
                    exchange.market_close(coin_to_rotate)
                    state["stagnation_tracker"][coin_to_rotate] = 0
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin_to_rotate, "entry_price": stagnant_trade["entry"],
                        "exit_price": stagnant_trade["current"],
                        "exit_reason": "⚡ Accelerated Stagnation Rotation (3h Dead Capital)",
                        "timestamp": timestamp
                    })
                    state["closed_trades_ledger"] = state["closed_trades_ledger"][:10]
                    active_count -= 1
                    audit_logs.append(f"ROTATION TRIGGERED: Closed stagnant crypto {coin_to_rotate} after 3 hours of dead capital.")
                except Exception as e:
                    audit_logs.append(f"Crypto Rotation Failed on {coin_to_rotate}: {e}")

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 11px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Crypto Engine - All-Weather Guard)</div>
        <div class="table-responsive" style="overflow-x: hidden;">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%; table-layout: fixed;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    remaining_candidates = [c for c in smart_queue_sorted if c["coin"] not in active_coins]
    ondeck_rows = "".join([
        f"<tr>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold;'>#{i+1}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-weight: bold; color: #0f172a;'>{c['coin']}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-family: monospace;'>${c['close']:.4f}</td>"
        f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; color: #b45309; font-weight: 600;'>Score: {c['score']:.4f}</td>"
        f"</tr>"
        for i, c in enumerate(remaining_candidates[:3])
    ]) if remaining_candidates else "<tr><td colspan='4' style='padding: 10px; text-align: center; color: #666;'>No momentum candidates currently detected.</td></tr>"

    closed_ledger = state.get("closed_trades_ledger", [])
    closed_rows = "".join([
        f"<tr>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{t['coin']}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(t.get('entry_price', 0), 5)}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace;'>${round_sig_figs(t.get('exit_price', 0), 5)}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; color: #b45309;'>{t['exit_reason']}</td>"
        f"<td style='padding: 8px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 10px;'>{t['timestamp']}</td>"
        f"</tr>"
        for t in closed_ledger[:5]
    ]) if closed_ledger else "<tr><td colspan='5' style='padding: 10px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"

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
            <td colspan="4" style="padding: 9px 10px; text-align: right;">TOTAL:</td>
            <td style="padding: 9px 10px;">${total_collateral_sum:.2f}</td>
            <td style="padding: 9px 10px;">${total_position_usd_sum:.2f}</td>
            <td style="padding: 9px 10px; color: {'#2e7d32' if total_pnl_sum >= 0 else '#c62828'};">${total_pnl_sum:+.2f} ({total_roe_avg:+.2f}%)</td>
            <td colspan="3"></td>
        </tr>
        """
    else:
        positions_rows = "<tr><td colspan='10' style='padding: 15px; text-align: center; color: #666;'>No active positions found.</td></tr>"

    ai_risk_color = "#c62828" if is_high_risk else "#2e7d32"
    ai_badge = f"<span style='background: {ai_risk_color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 10px;'>Risk Level: {risk_level}</span>"

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
            <h2>TR-GC-Crypto-LS-23 | Telemetry Dashboard</h2>
            <p>Timestamp: {timestamp} (All-Weather Resilient Guard Active)</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Net Worth</div>
              <div class="net-worth-value">USD ${account_value:.2f}</div>
              <div class="net-worth-subtitle">Static Unallocated USDC Reserve: <b>${static_usdc:.2f}</b> &bull; Margin Utilization: <b>{margin_util_pct:.1f}%</b></div>
            </div>

            <div class="ai-brief-card">
              <div class="ai-brief-title">
                <span>🤖 GEMINI AI EXECUTIVE MARKET BRIEFING</span>
                {ai_badge}
              </div>
              <b>Live Market Assessment:</b> {ai_shield.get('ai_market_brief', 'Normal conditions.')}<br>
              <b>Execution Recommendation:</b> {ai_shield.get('reason', 'Standard scan active.')}
            </div>

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Guardrails (All-Weather Resilient Engine)</div>
              &bull; <b>Execution Engine:</b> 30-Min 24/7 GitHub Cron &bull; <b>Max Slots:</b> {active_count}/6 Active<br>
              &bull; <b>Instant Blood-Bath Force-Close:</b> Instantly market-closes positions if trend flips against open side<br>
              &bull; <b>BTC Regime Shield:</b> Block LONGs if daily candle is RED; block SHORTs if daily candle is GREEN<br>
              &bull; <b>Portfolio Drawdown Circuit Breaker:</b> Instantly flattens 100% to cash if total open loss hits -3.5%<br>
              &bull; <b>Active Chop Purge:</b> Automatically closes positions if market Choppiness Index (CI > 60.0) turns dead<br>
              &bull; <b>Accelerated Stagnation Rotation:</b> Cuts dead or flat capital after 3 hours (6 runs)<br>
              &bull; <b>Tiered Trailing Leash:</b> 0.3% buffer for early green, 0.5% for mid-trend, 1.0% for mega-runners<br>
              &bull; <b>Leverage Profile: Optimized 5x Safe Max Leverage</b>
            </div>

            <div class="section-title">Positions per Bot (USD)</div>
            <div class="table-responsive">
              <table><thead><tr><th>Bot Title</th><th>Asset</th><th>Leverage</th><th>Side</th><th>Collateral USD</th><th>Position USD</th><th>Unrealized P&L USD</th><th>Buy Price</th><th>Stop Price</th><th>Bot Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
            </div>

            <div class="section-title">Recently Closed Trades & Exit Telemetry</div>
            <div class="table-responsive">
              <table><thead><tr><th>Asset</th><th>Entry Price</th><th>Exit Price</th><th>Exit Reason / Catalyst</th><th>Timestamp</th></tr></thead><tbody>{closed_rows}</tbody></table>
            </div>

            <div class="section-title">On-Deck Smart Queue (Top 3 Waiting Runners)</div>
            <div class="table-responsive">
              <table><thead><tr><th>Rank</th><th>Asset</th><th>Current Price</th><th>Momentum Score</th></tr></thead><tbody>{ondeck_rows}</tbody></table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Hyperliquid Autonomous Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}", html_content, text_fallback)

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
