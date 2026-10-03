import os
import json
import time
from datetime import datetime, timedelta
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
        "cooldown_blocklist": {},      # 24H Post-loss cooldown: {coin: expire_timestamp}
        "last_traded_candle": {},      # Single-candle entry lock: {coin: candle_timestamp}
        "hibernating_until": 0,        # Emergency 12H hibernation lock
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

def check_liquidity_and_spread(info, coin, max_spread=0.0030, min_depth_usd=6000.0):
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

        bid_depth = sum(float(b["px"]) * float(b["sz"]) for b in bids if float(b["px"]) >= mid_px * 0.995)
        ask_depth = sum(float(a["px"]) * float(a["sz"]) for a in asks if float(a["px"]) <= mid_px * 1.005)
        total_depth = min(bid_depth, ask_depth)

        if total_depth < min_depth_usd:
            return False, f"Low Orderbook Depth (${total_depth:,.0f} < ${min_depth_usd:,.0f})"

        return True, f"OK (Spread: {spread_pct*100:.2f}%, Depth: ${total_depth:,.0f})"
    except Exception as e:
        return False, f"Liquidity Check Error: {e}"

# ==============================================================================
# NATIVE EXCHANGE TRIGGER ORDER MANAGERS
# ==============================================================================
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

def sync_native_trigger_stop(exchange, info, coin, is_long, sz, stop_px, account_address, audit_logs=None):
    try:
        clean_stop_px = float(round_sig_figs(stop_px, 5))
        is_buy = not is_long  # To close LONG -> SELL (False); to close SHORT -> BUY (True)

        open_orders = api_retry(info.frontend_open_orders, account_address)
        coin_trigger_orders = [
            o for o in open_orders 
            if o.get("coin") == coin and (o.get("isTrigger") or "trigger" in str(o.get("orderType", "")).lower())
        ]

        needs_update = True
        for o in coin_trigger_orders:
            existing_px = float(o.get("triggerPx", 0.0))
            if abs(existing_px - clean_stop_px) / max(clean_stop_px, 1e-8) < 0.0005:
                needs_update = False
                break
            else:
                try:
                    exchange.cancel(coin, int(o["oid"]))
                except Exception:
                    pass

        if needs_update:
            order_type = {"trigger": {"isMarket": True, "triggerPx": clean_stop_px, "tpsl": "sl"}}
            res = exchange.order(coin, is_buy, sz, clean_stop_px, order_type, reduce_only=True)
            if audit_logs is not None:
                audit_logs.append(f"🛡️ NATIVE ORDERBOOK TPSL SYNC [{coin}]: Placed Resting Trigger Stop @ ${clean_stop_px:.5f}")
    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠️ Native TPSL sync warning on {coin}: {e}")

# ==============================================================================
# TECHNICAL INDICATORS
# ==============================================================================
def calculate_vwap(candles):
    try:
        pv_sum = sum(((float(c['h']) + float(c['l']) + float(c['c'])) / 3.0) * float(c.get('v', 1.0)) for c in candles)
        v_sum = sum(float(c.get('v', 1.0)) for c in candles)
        return pv_sum / v_sum if v_sum > 0 else float(candles[-1]['c'])
    except Exception:
        return float(candles[-1]['c'])

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

def calculate_adx(highs, lows, closes, period=14):
    try:
        if len(closes) < period * 2:
            return 20.0
        df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
        df["up"] = df["high"] - df["high"].shift(1)
        df["down"] = df["low"].shift(1) - df["low"]
        df["+dm"] = np.where((df["up"] > df["down"]) & (df["up"] > 0), df["up"], 0.0)
        df["-dm"] = np.where((df["down"] > df["up"]) & (df["down"] > 0), df["down"], 0.0)
        
        df["tr"] = np.maximum(df["high"] - df["low"], 
                   np.maximum(abs(df["high"] - df["close"].shift(1)), 
                              abs(df["low"] - df["close"].shift(1))))
        
        atr = df["tr"].ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * (df["+dm"].ewm(alpha=1/period, adjust=False).mean() / atr)
        minus_di = 100 * (df["-dm"].ewm(alpha=1/period, adjust=False).mean() / atr)
        
        dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8))
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        return float(adx.iloc[-1])
    except Exception:
        return 20.0

def calculate_rsi(closes, period=14):
    try:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        seed = deltas[:period]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        rs = up / down if down != 0 else 0
        rsi = np.zeros_like(closes)
        rsi[:period] = 100. - 100. / (1. + rs)

        for i in range(period, len(closes)):
            delta = deltas[i - 1]
            if delta > 0:
                upval = delta
                downval = 0.
            else:
                upval = 0.
                downval = -delta

            up = (up * (period - 1) + upval) / period
            down = (down * (period - 1) + downval) / period
            rs = up / down if down != 0 else 0
            rsi[i] = 100. - 100. / (1. + rs)

        return float(rsi[-1])
    except Exception:
        return 50.0

def calculate_bollinger_bands(closes, period=20, num_std=2.0):
    try:
        s = pd.Series(closes)
        sma = s.rolling(window=period).mean().iloc[-1]
        std = s.rolling(window=period).std().iloc[-1]
        upper = sma + (std * num_std)
        lower = sma - (std * num_std)
        return float(upper), float(lower), float(sma)
    except Exception:
        c = closes[-1]
        return c * 1.02, c * 0.98, c

def calculate_atr(highs, lows, closes, period=14):
    try:
        if len(highs) < period + 1:
            return highs[-1] - lows[-1] if len(highs) > 0 else 1.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
        return np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs)
    except Exception:
        return 1.0

# ==============================================================================
# NOISE-TUNED BREATHING-ROOM RATCHET ENGINE (ANTI-WICK)
# ==============================================================================
def calculate_moonshot_ratchet_stop(entry_px, is_long, current_px, atr_val, peak_roe=0.0):
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    # ANTI-WICK BREATHING ROOM BUFFER: -1.80% to -2.50% max ROE loss (Survives normal hourly wicks)
    atr_roe_buffer = (atr_val * 1.2) / entry_px if entry_px > 0 else 0.020
    atr_roe_buffer = max(0.0180, min(0.0250, atr_roe_buffer))

    # 1. Galactic & Parabolic Moonshots (+3.00+ to +1000%+ ROE - Infinite Upside)
    if peak_roe >= 3.00:
        target_floor_roe = max(peak_roe * 0.95, peak_roe - 0.20)
        leash_status = f"🚀 GALACTIC MOONSHOT 95% Lock [{peak_roe*100:.0f}% Peak -> +{target_floor_roe*100:.0f}% Floor]"
    elif peak_roe >= 0.75:
        target_floor_roe = peak_roe * 0.90
        leash_status = f"🌕 Major Runner 90% Lock [{peak_roe*100:.0f}% Peak -> +{target_floor_roe*100:.0f}% Floor]"
    elif peak_roe >= 0.30:
        target_floor_roe = max(peak_roe * 0.88, peak_roe - 0.05)
        leash_status = f"📈 Strong Trend 88% Lock [{peak_roe*100:.1f}% Peak -> +{target_floor_roe*100:.1f}% Floor]"
    elif peak_roe >= 0.0150:
        target_floor_roe = peak_roe * 0.85
        leash_status = f"🎯 Core Profit 85% Lock [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
        
    # 2. Fast Profit Locks
    elif peak_roe >= 0.0080:
        target_floor_roe = peak_roe * 0.80
        leash_status = f"⚡ Fast Lock 80% [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"
    elif peak_roe >= 0.0035:
        target_floor_roe = peak_roe * 0.75
        leash_status = f"📈 Micro Lock 75% [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"

    # 3. Micro Break-Even Shield (+0.15% Peak ROE -> Soft BE Floor +0.03%)
    elif peak_roe >= 0.0015:
        target_floor_roe = 0.0003  # +0.03% ROE Floor (Risk-Free Scratch/Profit)
        leash_status = f"🛡️ Micro Break-Even Shield [{peak_roe*100:.2f}% Peak -> +{target_floor_roe*100:.2f}% Floor]"

    else:
        target_floor_roe = -atr_roe_buffer
        leash_status = f"🛡 Anti-Wick Buffer (-{atr_roe_buffer*100:.2f}%)"

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
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')

    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23 Engine Started (Anti-Wick Breathing Buffer Active).")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()

    # Guardrail: Hibernation Check
    if now_ts < float(state.get("hibernating_until", 0)):
        remaining_hrs = (float(state["hibernating_until"]) - now_ts) / 3600.0
        print(f"[{timestamp}] 🚨 BOT IN 12H EMERGENCY HIBERNATION ({remaining_hrs:.1f}h remaining). Execution halted.", flush=True)
        return

    # Guardrail: Rolling 1-Hour Loss Circuit Breaker
    one_hour_ago = now_ts - 3600
    recent_losses = [
        t for t in state.get("closed_trades_ledger", [])
        if float(t.get("pnl_usd", 0)) < 0 and float(t.get("ts_sec", 0)) >= one_hour_ago
    ]

    if len(recent_losses) >= 3:
        state["hibernating_until"] = now_ts + 43200  # 12-Hour Hibernation
        save_state(state)
        err_body = f"🚨 ROLLING CIRCUIT BREAKER TRIGGERED: 3 losses recorded within the last 60 minutes. Bot entering 12-hour hibernation."
        print(f"[{timestamp}] {err_body}", flush=True)
        send_html_dashboard_email("🚨 EMERGENCY HALT: Rolling Circuit Breaker Active", f"<h3>{err_body}</h3>", err_body)
        return

    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = api_retry(Exchange, wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    all_mids = api_retry(info.all_mids)
    meta = api_retry(info.meta)
    now_ms = int(now_ts * 1000)

    is_1h_scan_window = True

    gemini_risk, gemini_briefing = check_gemini_macro_shield(state, now_ts, is_1h_scan_window)
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] {gemini_briefing}")

    if gemini_risk == "HIGH":
        required_vol_ratio = 1.25
        audit_logs.append(f"⚠ Gemini Macro Risk HIGH: Balanced breakout volume gate >= {required_vol_ratio:.2f}x")
    elif gemini_risk == "MODERATE":
        required_vol_ratio = 1.18
        audit_logs.append(f"ℹ️ Gemini Macro Risk MODERATE: Balanced breakout volume gate >= {required_vol_ratio:.2f}x")
    else:
        required_vol_ratio = 1.12
        audit_logs.append(f"✅ Gemini Macro Risk LOW: Standard breakout volume gate >= {required_vol_ratio:.2f}x active")

    btc_regime, btc_change_pct = get_btc_regime(info, now_ms)
    audit_logs.append(f"BTC Directional Shield: Daily Candle is {btc_regime} ({btc_change_pct:+.2f}%).")

    effective_regime = btc_regime

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

    daily_starting_dict = state.get("daily_starting_equity", {})
    if today_str not in daily_starting_dict:
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

    # Position Management & Exits (LONG & SHORT Bidirectional Evaluation)
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
                bb_upper, bb_lower, bb_mid = calculate_bollinger_bands(closes)
                vwap_val = calculate_vwap(c_candles[-24:])

                strat_type = current_active_cache.get(coin, {}).get("strategy", "BREAKOUT")
                if strat_type == "MEAN_REVERSION":
                    target_tp = max(bb_mid, vwap_val) if is_long else min(bb_mid, vwap_val)
                    if is_long and current_px >= target_tp:
                        should_exit = True
                        exit_reason = f"🎯 RANGING Fair-Value TP Target (${current_px:.4f} >= ${target_tp:.4f})"
                    elif (not is_long) and current_px <= target_tp:
                        should_exit = True
                        exit_reason = f"🎯 RANGING Fair-Value TP Target (${current_px:.4f} <= ${target_tp:.4f})"
            except Exception as e:
                audit_logs.append(f"Indicator calculation warning on {coin}: {e}")

            if btc_regime == "RED" and is_long:
                should_exit = True
                exit_reason = "🛑 BTC Daily Bearish Flip Purge"
            elif btc_regime == "GREEN" and (not is_long):
                should_exit = True
                exit_reason = "🟢 BTC Daily Bullish Flip Purge"

            prev_peak = current_active_cache.get(coin, {}).get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            stop_px_calc, current_roe, target_floor_roe, leash_status = calculate_moonshot_ratchet_stop(
                entry_px, is_long, current_px, atr_val, peak_roe=peak_roe
            )

            # BIDIRECTIONAL STOP / PROFIT LATCH TRIGGER
            if is_long and current_px <= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"
            elif (not is_long) and current_px >= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Stop/Profit Lock Triggered ({current_roe*100:.2f}%)"

            if should_exit:
                try:
                    audit_logs.append(f"🎯 EXIT TRIGGERED: Closing {coin} {'LONG' if is_long else 'SHORT'} @ ${current_px:.5f} ({exit_reason}).")
                    
                    cancel_native_trigger_orders(exchange, info, coin, ACCOUNT_ADDRESS)
                    exchange.market_close(coin, slippage=0.01)

                    if unrealized_pnl < 0 or current_roe < 0:
                        state["cooldown_blocklist"][coin] = now_ts + 86400
                        audit_logs.append(f"⛔ Added {coin} to 24H Cooldown Blocklist (Closed at Loss)")

                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": current_roe * 100, "side": "LONG" if is_long else "SHORT",
                        "exit_reason": exit_reason, "timestamp": timestamp, "ts_sec": now_ts
                    })
                    trade_closed_this_run = True
                    continue
                except Exception as e:
                    audit_logs.append(f"Market close failed on {coin}: {e}")

            # POSITION REMAINS ACTIVE -> SYNC RESTING NATIVE TPSL TRIGGER ORDER ON HYPERLIQUID ORDERBOOK
            sync_native_trigger_stop(exchange, info, coin, is_long, abs(szi), stop_px_calc, ACCOUNT_ADDRESS, audit_logs)

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, 
                "margin": margin_used, "side": "LONG" if is_long else "SHORT",
                "peak_roe": peak_roe, "strategy": current_active_cache.get(coin, {}).get("strategy", "BREAKOUT")
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

    MAX_CRYPTO_SLOTS = 1  # Strictly capped at 1 active trade as requested
    available_slots = MAX_CRYPTO_SLOTS - active_count

    base_sizing_usd = 10.0

    # ALL-WEATHER BALANCED SCANNER: Allows smooth scanning without overly restrictive barriers
    if is_1h_scan_window and available_slots > 0:
        if effective_regime == "NEUTRAL":
            required_vol_ratio = max(required_vol_ratio, 1.15)
            audit_logs.append(f"ℹ BTC Neutral Regime: Balanced All-Weather Scan (Vol Gate >= {required_vol_ratio:.2f}x)")

        state["last_scan_timestamp"] = now_ts

        btc_candles_5d = api_retry(info.candles_snapshot, name="BTC", interval="1h", startTime=now_ms - 86400000 * 5, endTime=now_ms)
        btc_closes = [float(c["c"]) for c in btc_candles_5d]
        btc_highs = [float(c["h"]) for c in btc_candles_5d]
        btc_lows = [float(c["l"]) for c in btc_candles_5d]
        
        btc_ci = calculate_choppiness_index(btc_highs, btc_lows, btc_closes)
        btc_adx = calculate_adx(btc_highs, btc_lows, btc_closes)

        # Sweet spot: ADX > 21.0 allows clean trending capture without skipping moderate moves
        if btc_ci < 48.0 and btc_adx > 21.0:
            market_mode = "TRENDING"
        elif btc_ci > 62.0:
            market_mode = "CHOP_HOLD"
        else:
            market_mode = "RANGING"

        audit_logs.append(f"🌐 MARKET REGIME CLASSIFIER: Mode = [{market_mode}] (BTC CI: {btc_ci:.1f}, ADX: {btc_adx:.1f}). Scanning candidates...")

        if market_mode != "CHOP_HOLD":
            for coin in universe:
                if coin in active_coins or coin in ["USDC", "USDT"]:
                    continue

                cooldown_expiry = float(state.get("cooldown_blocklist", {}).get(coin, 0))
                if now_ts < cooldown_expiry:
                    continue

                try:
                    curr_live_px = float(all_mids.get(coin, 0))
                    if curr_live_px <= 0:
                        continue
                    
                    time.sleep(0.12)
                    candles = api_retry(info.candles_snapshot, name=coin, interval="1h", startTime=now_ms - 86400000 * 5, endTime=now_ms)
                    if not candles or len(candles) < 50:
                        continue

                    current_candle_ts = candles[-1]["t"]
                    if state.get("last_traded_candle", {}).get(coin) == current_candle_ts:
                        continue

                    is_liquid, liq_reason = check_liquidity_and_spread(info, coin, max_spread=0.0030, min_depth_usd=6000.0)
                    if not is_liquid:
                        continue

                    closes = [float(c["c"]) for c in candles]
                    highs = [float(c["h"]) for c in candles]
                    lows = [float(c["l"]) for c in candles]
                    volumes = [float(c.get("v", 0)) for c in candles]

                    # TRUE BODY STRENGTH & MOMENTUM GATE (NO WOBBLY WICKS)
                    c_open = float(candles[-1]["o"])
                    c_high = float(candles[-1]["h"])
                    c_low = float(candles[-1]["l"])
                    c_range = c_high - c_low if c_high > c_low else 1e-8
                    live_body = abs(curr_live_px - c_open)

                    prev_open = float(candles[-2]["o"])
                    prev_close = float(candles[-2]["c"])

                    is_true_green = (curr_live_px > c_open) and (live_body / c_range >= 0.35) and (prev_close > prev_open)
                    is_true_red = (curr_live_px < c_open) and (live_body / c_range >= 0.35) and (prev_close < prev_open)

                    rsi_1h = calculate_rsi(closes)
                    bb_upper, bb_lower, bb_mid = calculate_bollinger_bands(closes)
                    vwap_val = calculate_vwap(candles[-24:])
                    
                    avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else volumes[-3]
                    comp_vol = volumes[-2]
                    vol_ratio = comp_vol / avg_vol if avg_vol > 0 else 1.0

                    upper, lower, filter_band = calculate_gaussian_channel(closes[:-1])
                    comp_close = closes[-2]

                    is_holding_breakout = curr_live_px >= comp_close
                    is_holding_breakdown = curr_live_px <= comp_close

                    # STRATEGY A: TRENDING BREAKOUT / BREAKDOWN ENGINE (LONG & SHORT)
                    if market_mode == "TRENDING" or (market_mode == "RANGING" and vol_ratio >= required_vol_ratio):
                        ci_1h = calculate_choppiness_index(highs[:-1], lows[:-1], closes[:-1])
                        if ci_1h <= 52.0 and vol_ratio >= required_vol_ratio:
                            if effective_regime in ["GREEN", "NEUTRAL"]:
                                # LONG Entry Gate: True Green & holding breakout
                                if comp_close > upper and comp_close <= (upper * 1.030):
                                    if is_true_green and is_holding_breakout:
                                        extension_pct = ((comp_close - upper) / upper) * 100
                                        market_candidates.append({
                                            "coin": coin, "close": curr_live_px, "is_long": True, 
                                            "score": (curr_live_px - upper) / upper, "candle_ts": current_candle_ts,
                                            "strategy": "BREAKOUT"
                                        })
                                        audit_logs.append(f"1H TRUE GREEN BREAKOUT MATCH (LONG): {coin} @ ${curr_live_px:.4f} (True Green Body, Ext: +{extension_pct:.2f}%, VolRatio: {vol_ratio:.2f}x)")

                            if effective_regime in ["RED", "NEUTRAL"]:
                                # SHORT Entry Gate: True Red & holding breakdown
                                if comp_close < lower and comp_close >= (lower * 0.970):
                                    if is_true_red and is_holding_breakdown:
                                        extension_pct = ((lower - comp_close) / lower) * 100
                                        market_candidates.append({
                                            "coin": coin, "close": curr_live_px, "is_long": False, 
                                            "score": (lower - curr_live_px) / lower, "candle_ts": current_candle_ts,
                                            "strategy": "BREAKOUT"
                                        })
                                        audit_logs.append(f"1H TRUE RED BREAKDOWN MATCH (SHORT): {coin} @ ${curr_live_px:.4f} (True Red Body, Ext: -{extension_pct:.2f}%, VolRatio: {vol_ratio:.2f}x)")

                    # STRATEGY B: RANGING MEAN-REVERSION (LONG & SHORT)
                    if market_mode == "RANGING":
                        if effective_regime in ["GREEN", "NEUTRAL"]:
                            # LONG Dip Buy Gate: True Green bounce
                            if curr_live_px <= bb_lower * 1.005 and curr_live_px < vwap_val and rsi_1h <= 48.0 and is_true_green:
                                market_candidates.append({
                                    "coin": coin, "close": curr_live_px, "is_long": True,
                                    "score": (vwap_val - curr_live_px) / vwap_val, "candle_ts": current_candle_ts,
                                    "strategy": "MEAN_REVERSION"
                                })
                                audit_logs.append(f"1H VWAP TRUE DIP BOUNCE (LONG): {coin} @ ${curr_live_px:.4f} (True Green Bounce Below VWAP, RSI: {rsi_1h:.1f})")

                        if effective_regime in ["RED", "NEUTRAL"]:
                            # SHORT Fade High Gate: True Red reject
                            if curr_live_px >= bb_upper * 0.995 and curr_live_px > vwap_val and rsi_1h >= 52.0 and is_true_red:
                                market_candidates.append({
                                    "coin": coin, "close": curr_live_px, "is_long": False,
                                    "score": (curr_live_px - vwap_val) / vwap_val, "candle_ts": current_candle_ts,
                                    "strategy": "MEAN_REVERSION"
                                })
                                audit_logs.append(f"1H VWAP TRUE SHORT FADE (SHORT): {coin} @ ${curr_live_px:.4f} (True Red Reject Above VWAP, RSI: {rsi_1h:.1f})")

                except Exception:
                    continue

            audit_logs.append(f"🌐 Scan complete: {len(market_candidates)} candidate(s) qualified out of {len(universe)} coins scanned.")
        else:
            audit_logs.append(f"⏳ Market is in EXTREME CHOP (CI > 62.0). Skipping scans to preserve cash.")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)

    trades_executed = False
    if available_slots > 0 and market_candidates:
        for candidate in market_candidates[:available_slots]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            candle_ts = candidate["candle_ts"]
            strat_used = candidate["strategy"]
            
            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = base_sizing_usd / px
            sz = round(raw_sz, decimals)
            if decimals == 0:
                sz = int(sz)

            if sz <= 0:
                audit_logs.append(f"⚠️ Sizing guard skipped {coin}: calculated size {sz} <= 0 (Price: ${px:.2f})")
                continue

            try:
                try:
                    exchange.update_leverage(coin, 1, True)
                except Exception:
                    pass

                # GUARANTEED TAKER MARKET ORDER ENTRY
                res = exchange.market_open(coin, is_long, sz, px, slippage=0.01)

                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    trades_executed = True

                    if "last_traded_candle" not in state:
                        state["last_traded_candle"] = {}
                    state["last_traded_candle"][coin] = candle_ts

                    if "active_position_cache" not in state:
                        state["active_position_cache"] = {}
                    state["active_position_cache"][coin] = {"strategy": strat_used}

                    initial_stop_px = px * 0.982 if is_long else px * 1.018
                    sync_native_trigger_stop(exchange, info, coin, is_long, sz, initial_stop_px, ACCOUNT_ADDRESS, audit_logs)

                    positions_data.append({
                        "bot_title": "TR-GC-Crypto-LS-23", "coin": coin,
                        "side": "LONG" if is_long else "SHORT", "sz": sz,
                        "entry": px, "current": px, "leverage": 1,
                        "collateral": base_sizing_usd, "position_usd": base_sizing_usd,
                        "pnl": 0.0, "roe": 0.0,
                        "stop": round_sig_figs(initial_stop_px, 5),
                        "status": "🛡️ Anti-Wick Buffer Active (Native Orderbook TPSL)"
                    })

                    audit_logs.append(f"1H EXECUTION SUCCESS [{strat_used}]: Opened {'LONG' if is_long else 'SHORT'} on {coin} (Size: {sz} ~${base_sizing_usd:.2f})")

            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")

    if trades_executed:
        time.sleep(2.5)

    static_usdc = max(0.0, account_value - total_margin_used)
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    should_send_email = True

    if should_send_email:
        state["last_email_timestamp"] = now_ts
        save_state(state)

        trades_today = [
            t for t in state.get("closed_trades_ledger", [])
            if today_str in str(t.get("timestamp", ""))
        ]

        total_today = len(trades_today)
        wins_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) > 0]
        losses_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) <= 0]
        
        win_count = len(wins_today)
        loss_count = len(losses_today)
        win_rate_today = (win_count / total_today * 100) if total_today > 0 else 0.0
        
        avg_win_roe = (sum(float(t.get("roe_pct", 0)) for t in wins_today) / win_count) if win_count > 0 else 0.0
        avg_win_usd = (sum(float(t.get("pnl_usd", 0)) for t in wins_today) / win_count) if win_count > 0 else 0.0
        
        avg_loss_roe = (sum(float(t.get("roe_pct", 0)) for t in losses_today) / loss_count) if loss_count > 0 else 0.0
        avg_loss_usd = (sum(float(t.get("pnl_usd", 0)) for t in losses_today) / loss_count) if loss_count > 0 else 0.0
        
        net_today_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_today)

        text_fallback = f"TR-GC-Crypto-LS-23 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/1"

        summary_card_html = f"""
        <div class="summary-card">
          <div class="summary-title">📊 Today's Realized Performance Summary ({today_str})</div>
          <table style="width: 100%; border-collapse: collapse; margin-bottom: 8px;">
            <tr>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Total Trades Today: <b style="color: #0f172a;">{total_today}</b></td>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Win Ratio: <b style="color: #0f172a;">{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></td>
            </tr>
            <tr>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase;">Avg Win: <b style="color: #15803d;">+{avg_win_roe:.2f}% (${avg_win_usd:+.2f})</b></td>
              <td style="padding: 4px; font-size: 10px; color: #166534; font-weight: 600; text-transform: uppercase; text-align: right;">Avg Loss: <b style="color: #b91c1c;">{avg_loss_roe:.2f}% (${avg_loss_usd:+.2f})</b></td>
            </tr>
          </table>
          <div class="summary-net">
            Today's Realized P&L: <span class="{'win-color' if net_today_usd >= 0 else 'loss-color'}">${net_today_usd:+.2f}</span>
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

        total_pnl_color = '#2e7d32' if total_realized_pnl >= 0 else '#c62828'
        closed_rows = "".join(parsed_closed_rows) if parsed_closed_rows else "<tr><td colspan='4' style='padding: 12px; text-align: center; color: #666;'>No recent exits recorded yet.</td></tr>"
        closed_rows += f"""
        <tr style="background: #f8fafc; font-weight: bold; border-top: 2px solid #cbd5e1;">
            <td colspan="2" style="padding: 8px; text-align: right;">TOTAL RECENT REALIZED P&L:</td>
            <td colspan="2" style="padding: 8px; color: {total_pnl_color};">${total_realized_pnl:+.2f}</td>
        </tr>
        """

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
              .net-worth-subtitle {{ font-size: 10px; color: #64748b; margin-top: 4px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px; }}
              
              .summary-card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 12px; margin-bottom: 12px; }}
              .summary-title {{ font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase; margin-bottom: 8px; letter-spacing: 0.5px; }}
              .summary-net {{ font-size: 11px; font-weight: 700; color: #166534; border-top: 1px dashed #bbf7d0; padding-top: 6px; margin-top: 2px; }}
              
              .win-color {{ color: #15803d !important; }}
              .loss-color {{ color: #b91c1c !important; }}

              .pnl-badge {{ background: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold; }}

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
                <h2>TR-GC-Crypto-LS-23-V2 | Telemetry Dashboard</h2>
                <p>Timestamp: {timestamp} (Anti-Wick Breathing Engine Active)</p>
              </div>
              <div class="content">
                <div class="net-worth-card">
                  <div class="net-worth-title">Total Net Worth</div>
                  <div class="net-worth-value">USD ${account_value:.2f}</div>
                  <div class="net-worth-subtitle">
                    <span>Reserve: <b>${static_usdc:.2f}</b></span>
                    <span>Margin: <b>{margin_util_pct:.1f}%</b></span>
                    <span>Today's Gain: <span class="pnl-badge">${today_total_gain:+,.2f}</span></span>
                  </div>
                </div>

                {summary_card_html}

                <div class="rules-card">
                  <div class="rules-title">&#9989; Active Guardrails (Full Crypto Strategy Display)</div>
                  &bull; <b>Anti-Wick Breathing Buffer:</b> Gives trades -1.80% to -2.50% room to breathe past normal hourly wicks<br>
                  &bull; <b>True Body Momentum Gate:</b> Requires solid candle bodies (&gt;35% range) and multi-candle commitment<br>
                  &bull; <b>Native Orderbook Trigger Stop-Market Orders:</b> Auto-places & ratchets resting TPSL directly on exchange orderbook<br>
                  &bull; <b>Bidirectional Live Candle Confirmation Gate:</b> Green for LONGs, Red for SHORTs with 1H Hold Confirmation<br>
                  &bull; <b>100% Market Execution:</b> All exits execute via direct Taker Market Orders<br>
                  &bull; <b>Ultra-Tight Micro-Ratchet Ladder:</b> Micro BE at +0.15%, 75% at +0.35%, 80% at +0.80%, 85% at +1.50%<br>
                  &bull; <b>BTC Directional Shield:</b> Enforces broad market alignment (GREEN = LONGs only, RED = SHORTs only, NEUTRAL = All-Weather High Conviction)<br>
                  &bull; <b>Adaptive Gemini Volume Gate:</b> Dynamically scales volume confirmation (LOW: 1.12x, MODERATE: 1.18x, HIGH: 1.25x)<br>
                  &bull; <b>Unrestricted Scanner:</b> 100-coin scanning universe remains 100% open for moonshot detection<br>
                  &bull; <b>Uncapped Moonshot Upside:</b> Zero take-profit caps—lets parabolic runners fly infinitely<br>
                  &bull; <b>Single-Slot Capital Preservation:</b> Strictly capped at 1 active trade ($10 floor)<br>
                  &bull; <b>Optimal Orderbook Gate:</b> Rejects spread &gt; 0.30% or 0.5% depth &lt; $6,000 USD<br>
                  &bull; <b>24H Post-Loss Cooldown Blocklist:</b> Bans any coin closed at a loss for 24 hours in state.json<br>
                  &bull; <b>Single-Candle Lockout:</b> Restricts assets to max 1 entry per candle bar<br>
                  &bull; <b>Rolling Loss Circuit Breaker:</b> Triggers 12-hour hibernation if 3 losses occur within rolling 60m
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

if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing single-run Anti-Wick cycle...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cycle execution completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
