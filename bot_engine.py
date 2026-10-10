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
        "stagnation_tracker": {},      # Falling knife tracker: {coin: consecutive_negative_runs}
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

def api_retry(func, *args, retries=5, delay=4.0, **kwargs):
    """Universal resilient API retry decorator for exchange and web queries."""
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

def check_liquidity_and_spread(info, coin, max_spread=0.0040, min_depth_usd=3000.0):
    """Micro-Cap Orderbook Gate: Optimized for low-float penny perps."""
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
# NATIVE EXCHANGE TRIGGER ORDER MANAGERS (24/7 ON-CHAIN ORDERBOOK SHIELD)
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

def sync_native_trigger_orders(exchange, info, coin, is_long, sz, stop_px, tp_px, account_address, audit_logs=None):
    """Syncs resting Stop-Loss and resting Take-Profit orders directly on Hyperliquid orderbook."""
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

        # 1. Sync Stop-Loss Order
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
            res = exchange.order(coin, is_buy, sz, clean_stop_px, order_type, reduce_only=True)
            if audit_logs is not None:
                audit_logs.append(f"🛡️ NATIVE ORDERBOOK SL SYNC [{coin}]: Placed Resting Trigger Stop @ ${clean_stop_px:.5f}")

        # 2. Sync Take-Profit Order
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
                    if audit_logs is not None:
                        audit_logs.append(f"🚀 MOONSHOT UNCAP [{coin}]: Removed fixed TP target on Hyperliquid for infinite upside")
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
                res = exchange.order(coin, is_buy, sz, clean_tp_px, order_type, reduce_only=True)
                if audit_logs is not None:
                    audit_logs.append(f"🎯 NATIVE ORDERBOOK TP SYNC [{coin}]: Placed Resting Take-Profit @ ${clean_tp_px:.5f}")

    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠️ Native TPSL sync warning on {coin}: {e}")

# ==============================================================================
# TECHNICAL INDICATORS (FAST 5M TIMEFRAME TUNED)
# ==============================================================================
def calculate_vwap(candles):
    try:
        pv_sum = sum(((float(c['h']) + float(c['l']) + float(c['c'])) / 3.0) * float(c.get('v', 1.0)) for c in candles)
        v_sum = sum(float(c.get('v', 1.0)) for c in candles)
        return pv_sum / v_sum if v_sum > 0 else float(candles[-1]['c'])
    except Exception:
        return float(candles[-1]['c'])

def calculate_gaussian_channel(closes, poles=4, period=24, mult=1.20):
    """Fast 5M period (24 periods = 2 hours of 5m bars)."""
    s = pd.Series(closes)
    alpha = (2.0 / (period + 1)) * (poles ** 0.5)
    filtered = s.ewm(alpha=alpha, adjust=False).mean()
    error = (s - filtered).abs()
    deviation = error.ewm(alpha=alpha, adjust=False).mean() * mult
    upper = filtered + deviation
    lower = filtered - deviation
    return upper.iloc[-1], lower.iloc[-1], filtered.iloc[-1]

def calculate_choppiness_index(highs, lows, closes, period=14):
    """Clamped Choppiness Index (0.0 to 100.0)."""
    try:
        if len(closes) < period + 1:
            return 50.0
        tr_sum = sum([max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, period + 1)])
        max_high = max(highs[-period:])
        min_low = min(lows[-period:])
        range_diff = max_high - min_low
        if range_diff <= 0 or tr_sum <= 0:
            return 50.0
        
        ratio = max(tr_sum / range_diff, 1.0)
        ci = 100 * (log10(ratio) / log10(period))
        return float(np.clip(ci, 0.0, 100.0))
    except Exception:
        return 50.0

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

def calculate_atr(highs, lows, closes, period=14):
    try:
        if len(highs) < period + 1:
            return highs[-1] - lows[-1] if len(highs) > 0 else 1.0
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
        return np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs)
    except Exception:
        return 1.0

# ==============================================================================
# FAST 5M ROSS SCALPER ENGINE (QUICK 3% - 10% PROFIT RATCHET)
# ==============================================================================
def analyze_live_falling_knife(curr_px, c_open, c_high, c_low, vol_ratio, is_long):
    """Analyzes live 5m candle structure to prevent buying falling knives."""
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

def calculate_smart_exchange_targets(entry_px, is_long, current_px, atr_val, entry_candle_low, entry_candle_high, peak_roe=0.0, coin="ETH"):
    """
    V3.9 FAST 5M ROSS SCALPER: Quick activation (+1.0% ROE) to capture fast 3% - 10% gains.
    """
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    is_major = coin.upper() in ["BTC", "ETH", "SOL"]
    
    # 1. DOWNSIDE: Strict Hard Noise Shield (-1.50% ROE Cap for Alts)
    atr_roe_buffer = (atr_val * 1.5) / entry_px if entry_px > 0 else 0.012
    min_required_stop = -0.0150  # Hard Cap Altcoins at strictly -1.50% ROE max loss (~$0.22)

    if is_long:
        structure_stop = (entry_candle_low / entry_px) - 1.0 if entry_px > 0 else min_required_stop
        initial_floor_roe = max(min_required_stop, structure_stop) if not is_major else min(min_required_stop, structure_stop)
    else:
        structure_stop = 1.0 - (entry_candle_high / entry_px) if entry_px > 0 else min_required_stop
        initial_floor_roe = max(min_required_stop, structure_stop) if not is_major else min(min_required_stop, structure_stop)

    # 2. FAST 5M PROFIT RATCHET (+1.00% ACTIVATION FOR QUICK SCALPS)
    activation_roe = 0.0100  # Activates at +1.00% ROE (~+$0.15)
    be_floor_roe = 0.0030      # +0.30% ROE floor (covers fees)

    if peak_roe >= activation_roe:
        if peak_roe < 0.0200:   # +1.00% to +2.00% ROE -> Break-Even Floor
            target_floor_roe = be_floor_roe
            leash_status = f"🛡 BREAK-EVEN SHIELD [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        elif peak_roe < 0.0400:  # +2.00% to +4.00% ROE -> Lock 60% of Gains
            target_floor_roe = peak_roe * 0.60
            leash_status = f"⚡ 5M SCALP LOCK (60%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
        else:                   # +4.00%+ ROE -> Lock 85% of Gains (Super-Trend)
            target_floor_roe = peak_roe * 0.85
            leash_status = f"🔥 5M MOONSHOT LOCK (85%) [Peak +{peak_roe*100:.2f}% -> Floor +{target_floor_roe*100:.2f}%]"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"🛡 STRUCTURAL STOP ({target_floor_roe*100:.2f}%)"

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
    else:
        stop_px = entry_px * (1 - target_floor_roe)

    tp_px = None  # Uncapped TP for infinite upside

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
            return "NEUTRAL", 0.0, 0.0
        latest = btc_candles[-1]
        o_px = float(latest["o"])
        c_px = float(latest["c"])
        l_px = float(latest["l"])
        pct_change = ((c_px - o_px) / o_px) * 100
        intraday_bounce_pct = ((c_px - l_px) / l_px) * 100 if l_px > 0 else 0.0
        
        if pct_change >= 0.15:
            return "GREEN", pct_change, intraday_bounce_pct
        elif pct_change <= -0.15:
            return "RED", pct_change, intraday_bounce_pct
        return "NEUTRAL", pct_change, intraday_bounce_pct
    except Exception as e:
        print(f"[WARN] Failed to fetch BTC daily regime: {e}", flush=True)
        return "NEUTRAL", 0.0, 0.0

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    now_ts = time.time()
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')

    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23-V3.9.1 Master Engine Started (5M Fast Scalper Active).")

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

    # HARDENED CIRCUIT BREAKER (Requires 3+ Losses AND Cumulative Rolling Loss <= -$1.00 USD)
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

    btc_regime, btc_change_pct, btc_bounce_pct = get_btc_regime(info, now_ms)
    audit_logs.append(f"BTC Directional Shield: Daily Candle is {btc_regime} ({btc_change_pct:+.2f}%, Intraday Bounce: +{btc_bounce_pct:.2f}%).")

    # ADAPTIVE VOLUME GATE FOR 5M SCALPING
    required_vol_ratio = 1.80 if gemini_risk == "HIGH" else 1.60
    audit_logs.append(f"⚡ 5M Fast Scalper Active: Target RVOL >= {required_vol_ratio:.2f}x | Sizing: 0.85x")

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

    # Position Management & Dynamic Exits (5M Candles)
    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if not coin or szi == 0:
                continue

            if coin in closed_coins_this_run:
                continue

            is_long = szi > 0
            is_altcoin = coin.upper() not in ["BTC", "ETH", "SOL"]
            entry_px = float(pos.get("entryPx", 0))
            current_px = float(all_mids.get(coin, entry_px))
            margin_used = float(pos.get("marginUsed", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            pos_equity = abs(szi) * current_px

            current_roe = (((current_px - entry_px) / entry_px) * 1.0) if is_long else (((entry_px - current_px) / entry_px) * 1.0)
            
            should_exit = False
            exit_reason = ""
            atr_val = 1.0
            entry_candle_low = entry_px * 0.985
            entry_candle_high = entry_px * 1.015
            c_candles = []

            if is_long and btc_regime == "RED":
                should_exit = True
                exit_reason = f"🚨 BTC Daily Bearish Flip Purge (BTC Daily is RED {btc_change_pct:+.2f}%)"

            try:
                # FAST 5M CANDLE SNAPSHOT
                c_candles = api_retry(info.candles_snapshot, name=coin, interval="5m", startTime=now_ms - 86400000, endTime=now_ms)
                closes = [float(c["c"]) for c in c_candles]
                highs = [float(c["h"]) for c in c_candles]
                lows = [float(c["l"]) for c in c_candles]
                volumes = [float(c.get("v", 0)) for c in c_candles]
                
                atr_val = calculate_atr(highs, lows, closes)

                if len(c_candles) >= 2:
                    entry_candle_low = float(c_candles[-2]["l"])
                    entry_candle_high = float(c_candles[-2]["h"])

                c_open = float(c_candles[-1]["o"])
                c_high = float(c_candles[-1]["h"])
                c_low = float(c_candles[-1]["l"])
                avg_v = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)
                curr_v = volumes[-1]
                v_ratio = curr_v / avg_v if avg_v > 0 else 1.0

                is_knife, knife_reason = analyze_live_falling_knife(current_px, c_open, c_high, c_low, v_ratio, is_long)
                if is_knife:
                    should_exit = True
                    exit_reason = knife_reason
            except Exception as e:
                audit_logs.append(f"Indicator calculation warning on {coin}: {e}")

            stag_map = state.get("stagnation_tracker", {})
            curr_stag = stag_map.get(coin, 0)
            if current_roe < -0.0150:
                curr_stag += 1
                if "stagnation_tracker" not in state:
                    state["stagnation_tracker"] = {}
                state["stagnation_tracker"][coin] = curr_stag
                if curr_stag >= 3:  # 3 consecutive 5m runs (~15 mins)
                    should_exit = True
                    exit_reason = f"🗡️ 5M Stagnation Cut ({current_roe*100:.2f}% after {curr_stag} 5m runs)"
            else:
                if "stagnation_tracker" in state and coin in state["stagnation_tracker"]:
                    state["stagnation_tracker"][coin] = 0

            prev_peak = current_active_cache.get(coin, {}).get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            stop_px_calc, tp_px_calc, current_roe, target_floor_roe, leash_status = calculate_smart_exchange_targets(
                entry_px, is_long, current_px, atr_val, entry_candle_low, entry_candle_high, peak_roe=peak_roe, coin=coin
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
                        audit_logs.append(f"ℹ️ Position on {coin} already flattened on-chain. Skipping duplicate cut.")
                        closed_coins_this_run.add(coin)
                        continue

                    exchange.market_close(coin, slippage=0.01)
                    closed_coins_this_run.add(coin)

                    if "stagnation_tracker" in state:
                        state["stagnation_tracker"].pop(coin, None)

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

            sync_native_trigger_orders(exchange, info, coin, is_long, abs(szi), stop_px_calc, tp_px_calc, ACCOUNT_ADDRESS, audit_logs)

            active_count += 1
            active_coins.add(coin)
            total_margin_used += margin_used

            new_active_cache[coin] = {
                "entry_px": entry_px, "current_px": current_px, "szi": szi, 
                "margin": margin_used, "side": "LONG" if is_long else "SHORT",
                "peak_roe": peak_roe, "strategy": current_active_cache.get(coin, {}).get("strategy", "5M_BREAKOUT")
            }

            positions_data.append({
                "bot_title": "TR-GC-Crypto-LS-23-V3.9.1", "coin": coin,
                "side": "LONG" if is_long else "SHORT", "sz": abs(szi),
                "entry": entry_px, "current": current_px, "leverage": 5,
                "collateral": margin_used, "position_usd": pos_equity,
                "pnl": unrealized_pnl, "roe": current_roe * 100,
                "stop": round_sig_figs(stop_px_calc, 5),
                "tp_target": round_sig_figs(tp_px_calc, 5) if tp_px_calc else "UNCAPPED 🚀",
                "status": leash_status
            })

    # DEDUPLICATED NATIVE EXCHANGE FILL RECONCILIATION
    prev_cache = state.get("active_position_cache", {})
    for coin, cache_data in list(prev_cache.items()):
        if coin in closed_coins_this_run:
            continue

        if coin not in active_coins:
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
                    
                    if "closed_trades_ledger" not in state:
                        state["closed_trades_ledger"] = []
                        
                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": exit_px,
                        "pnl_usd": pnl_usd, "roe_pct": roe_pct, "side": side,
                        "exit_reason": "🎯 Native Orderbook TPSL Fill", "timestamp": timestamp, "ts_sec": now_ts,
                        "fill_hash": fill_hash
                    })

                    if "stagnation_tracker" in state:
                        state["stagnation_tracker"].pop(coin, None)
                    
                    if pnl_usd < 0:
                        state["cooldown_blocklist"][coin] = now_ts + 86400
                        audit_logs.append(f"⛔ Added {coin} to 24H Cooldown Blocklist (Native Fill at Loss)")
                    
                    audit_logs.append(f"🎯 RECONCILED NATIVE FILL [{coin}]: Closed {side} @ ${exit_px:.5f} (P&L: ${pnl_usd:+.2f}, ROE: {roe_pct:+.2f}%)")
                    trade_closed_this_run = True
            except Exception as e:
                audit_logs.append(f"Reconciliation warning on {coin}: {e}")

    state["closed_trades_ledger"] = sorted(state.get("closed_trades_ledger", []), key=lambda x: x.get("timestamp", ""), reverse=True)[:20]
    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    market_candidates = []

    MAX_CRYPTO_SLOTS = 3
    available_slots = MAX_CRYPTO_SLOTS - active_count
    min_notional_usd = 10.50

    if is_5m_scan_window and available_slots > 0:
        state["last_scan_timestamp"] = now_ts

        audit_logs.append(f"🌐 5M FAST ROSS SCANNER ACTIVE: Scanning Micro-Cap Penny Perps (#30-#200+)...")

        # ZERO RATE-LIMIT BULK EXTRACTION
        scored_universe = []
        for idx, asset in enumerate(meta.get("universe", [])):
            coin = asset.get("name")
            if not coin or coin in active_coins or coin in ["USDC", "USDT"]:
                continue
            if now_ts < float(state.get("cooldown_blocklist", {}).get(coin, 0)):
                continue

            # Focus strictly on low-float micro-caps (skips heavy BTC/ETH/SOL)
            if idx < 20:
                continue

            try:
                ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                prev_px = float(ctx.get("prevDayPx", 0.0))
                mark_px = float(ctx.get("markPx", 0.0))
                
                if prev_px > 0 and mark_px > 0:
                    change_24h = ((mark_px - prev_px) / prev_px) * 100
                    if change_24h > 0.0:
                        scored_universe.append((coin, change_24h, idx))
            except Exception:
                continue

        scored_universe = sorted(scored_universe, key=lambda x: x[1], reverse=True)
        prioritized_universe = [item[0] for item in scored_universe[:30]]

        audit_logs.append(f"📊 Top Penny Gainers Ranked: {', '.join([f'{c} (+{g:.1f}%)' for c, g, i in scored_universe[:8]])}")

        for coin in prioritized_universe:
            try:
                curr_live_px = float(all_mids.get(coin, 0))
                if curr_live_px <= 0:
                    continue
                
                time.sleep(0.08)
                # FAST 5M CANDLE SNAPSHOT
                candles = api_retry(info.candles_snapshot, name=coin, interval="5m", startTime=now_ms - 86400000, endTime=now_ms)
                if not candles or len(candles) < 30:
                    continue

                current_candle_ts = candles[-1]["t"]
                if state.get("last_traded_candle", {}).get(coin) == current_candle_ts:
                    continue

                is_liquid, liq_reason = check_liquidity_and_spread(info, coin, max_spread=0.0040, min_depth_usd=3000.0)
                if not is_liquid:
                    continue

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]

                coin_24h_change = ((closes[-1] - closes[-288]) / closes[-288]) * 100 if len(closes) >= 288 else 0.0

                c_2_high = float(candles[-2]["h"])
                c_2_close = float(candles[-2]["c"])
                c_1_open = float(candles[-1]["o"])

                # 5M HIGH-OF-DAY (HOD) BREAKOUT CONDITION
                c_high_live = float(candles[-1]["h"])
                c_low_live = float(candles[-1]["l"])
                c_range = max(c_high_live - c_low_live, 1e-8)
                live_body_ratio = abs(curr_live_px - c_1_open) / c_range

                # Live 5m candle breaking above previous 5m high with strong body
                is_5m_hod_breakout = (curr_live_px > c_2_high) and (curr_live_px > c_1_open) and (live_body_ratio >= 0.50)

                candle_start_ms = candles[-1]["t"]
                elapsed_mins = max(0.5, (now_ms - candle_start_ms) / 60000.0)
                live_volume_raw = float(candles[-1].get("v", 0))
                paced_volume = live_volume_raw * (5.0 / elapsed_mins)
                
                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)
                vol_ratio = paced_volume / avg_vol if avg_vol > 0 else 1.0

                if vol_ratio < required_vol_ratio:
                    continue

                ci_5m = calculate_choppiness_index(highs[:-1], lows[:-1], closes[:-1])

                if ci_5m <= 58.0:
                    if is_5m_hod_breakout and (effective_regime in ["GREEN", "NEUTRAL"]):
                        market_candidates.append({
                            "coin": coin, "close": curr_live_px, "is_long": True, 
                            "score": vol_ratio * (curr_live_px / c_2_high), "candle_ts": current_candle_ts,
                            "strategy": "5M_HOD_BREAKOUT"
                        })
                        audit_logs.append(f"🚀 5M HOD BREAKOUT (LONG): {coin} @ ${curr_live_px:.4f} (24h Gain: {coin_24h_change:+.1f}%, Vol: {vol_ratio:.2f}x)")

            except Exception:
                continue

        audit_logs.append(f"🌐 5M Scan complete: {len(market_candidates)} qualified candidate(s).")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)

    trades_executed = False
    if available_slots > 0 and market_candidates:
        for candidate in market_candidates[:available_slots]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            candle_ts = candidate["candle_ts"]
            strat_used = candidate["strategy"]
            
            base_alloc_pct = 0.25
            risk_multiplier = 0.85
            slot_alloc_pct = base_alloc_pct * risk_multiplier
            target_notional_usd = max(min_notional_usd, account_value * slot_alloc_pct)

            decimals = sz_decimals_map.get(coin, 4)
            raw_sz = target_notional_usd / px

            if decimals == 0:
                sz = int(np.ceil(raw_sz))
            else:
                sz = round(np.ceil(raw_sz * (10 ** decimals)) / (10 ** decimals), decimals)

            if (sz * px) < min_notional_usd:
                needed_sz = min_notional_usd / px
                if decimals == 0:
                    sz = int(np.ceil(needed_sz))
                else:
                    sz = round(np.ceil(needed_sz * (10 ** decimals)) / (10 ** decimals), decimals)

            if sz <= 0:
                continue

            try:
                try:
                    exchange.update_leverage(coin, 5, True)
                except Exception:
                    pass

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
                    state["active_position_cache"][coin] = {
                        "strategy": strat_used, "entry_px": px, "side": "LONG" if is_long else "SHORT"
                    }

                    initial_stop_px = px * 0.9850 if is_long else px * 1.0150
                    initial_tp_px = None

                    sync_native_trigger_orders(exchange, info, coin, is_long, sz, initial_stop_px, initial_tp_px, ACCOUNT_ADDRESS, audit_logs)

                    alloc_label = "5M Fast Scalp (25% NAV)"
                    positions_data.append({
                        "bot_title": "TR-GC-Crypto-LS-23-V3.9.1", "coin": coin,
                        "side": "LONG" if is_long else "SHORT", "sz": sz,
                        "entry": px, "current": px, "leverage": 5,
                        "collateral": (sz * px) / 5.0, "position_usd": (sz * px),
                        "pnl": 0.0, "roe": 0.0,
                        "stop": round_sig_figs(initial_stop_px, 5),
                        "tp_target": "UNCAPPED 🚀",
                        "status": f"🛡️ {alloc_label}"
                    })

                    audit_logs.append(f"⚡ 5M SCALP EXECUTION SUCCESS: Opened LONG on {coin} (Size: {sz} ~${(sz * px):.2f})")

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
    is_time_for_periodic_email = (elapsed_minutes >= 15.0)
    should_send_email = is_manual_run or is_time_for_periodic_email or trades_executed or trade_closed_this_run

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

        text_fallback = f"TR-GC-Crypto-LS-23-V3.9.1 | 5M Fast Ross Scalper\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/3"

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
            <div class="section-title" style="color: #d97706;">Live Telemetry & Audit Log</div>
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
            f"<td style='padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 10px;'><span style='color: #b45309; font-weight: bold; font-family: monospace;'>SL: ${p['stop']}<br>TP: ${p['tp_target']}</span><br><span style='color: #2e7d32; font-weight: 600;'>{p['status']}</span></td>"
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
                <h2>TR-GC-Crypto-LS-23-V3.9.1 | 5M Fast Ross Scalper</h2>
                <p>Timestamp: {timestamp} (5M Candles &amp; 5M Scanning Active)</p>
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
                  <div class="rules-title">&#9989; Active Guardrails (V3.9.1 Fast Scalper Active)</div>
                  &bull; <b>5M High-of-Day (HOD) Scalper:</b> Detects fresh 5m candle breakouts as volume surges<br>
                  &bull; <b>Micro-Cap Penny Perp Scope (#20-#200+):</b> Focuses on low-float micro-caps while skipping heavy mega-caps<br>
                  &bull; <b>Zero Rate-Limit Bulk Extraction:</b> Fetches 150+ asset contexts in 1 single bulk call (`meta_and_asset_ctxs`)<br>
                  &bull; <b>Fast RVOL Gate (&ge; 1.6x - 1.8x):</b> Captures fresh 5m volume spikes early<br>
                  &bull; <b>Quick +1.00% ROE Profit Ratchet:</b> Locks break-even (+0.30% ROE) immediately at +1.0% gain<br>
                  &bull; <b>5-Minute Scan Cadence:</b> Runs every 5 minutes natively via GitHub Actions to hunt high-ranking movers<br>
                  &bull; <b>Strict Altcoin Loss Cap (-1.50% ROE):</b> Hard-caps altcoin losses at max -$0.22<br>
                  &bull; <b>24/7 Native Orderbook Sync:</b> Posts resting trigger orders directly on Hyperliquid orderbook<br>
                  &bull; <b>BTC Directional Shield:</b> Aligns market direction with daily candle (GREEN = LONGs only)<br>
                  &bull; <b>24H Post-Loss Cooldown Blocklist:</b> Auto-bans any coin closed at a loss for 24 hours<br>
                  &bull; <b>Hardened Circuit Breaker:</b> Hibernates 12H strictly if 3+ losses totaling &le; -$1.00 occur within rolling 60m
                </div>

                <div class="section-title">Active Positions (USD)</div>
                <div class="table-responsive">
                  <table>
                    <thead>
                      <tr>
                        <th style="width: 25%;">Asset</th>
                        <th style="width: 20%;">Val ($)</th>
                        <th style="width: 25%;">P&amp;L (ROE)</th>
                        <th style="width: 30%;">Stop / Status</th>
                      </tr>
                    </thead>
                    <tbody>{positions_rows}</tbody>
                  </table>
                </div>

                <div class="section-title">Recently Closed Trades &amp; Exit Telemetry</div>
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
              <div class="footer">Hyperliquid 5M Fast Scalper Autonomous Engine &bull; Managed via GitHub Actions</div>
            </div>
          </body>
        </html>
        """

        send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}", html_content, text_fallback)
    else:
        save_state(state)
        print(f"[{timestamp}] Execution cycle complete ({elapsed_minutes:.1f}m since last report). Skipping email dispatch.", flush=True)

if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing 5M Fast Ross Scalper cycle...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 5M cycle execution completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
