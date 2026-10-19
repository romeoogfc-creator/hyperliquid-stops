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
    
    utc_now = datetime.now(timezone.utc).replace(tzinfo=timezone.utc).replace(tzinfo=None)
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
        "daily_starting_equity": {},
        "seen_fill_hashes": []
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

def check_liquidity_and_spread(info, coin, max_spread=0.0008, min_depth_usd=2000.0):
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

        sl_orders = [o for o in coin_trigger_orders if (isinstance(o.get("orderType"), dict) and o["orderType"].get("trigger", {}).get("tpsl") == "sl")]
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
                audit_logs.append(f"🛡️ NATIVE SL SYNC [{coin}]: Hard Stop @ ${clean_stop_px:.5f}")

        tp_orders = [o for o in coin_trigger_orders if (isinstance(o.get("orderType"), dict) and o["orderType"].get("trigger", {}).get("tpsl") == "tp")]
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
                    audit_logs.append(f"🎯 NATIVE TP SYNC [{coin}]: Target @ ${clean_tp_px:.5f}")
    except Exception as e:
        if audit_logs is not None:
            audit_logs.append(f"⚠️ TPSL sync warning on {coin}: {e}")

def sync_closed_trades_from_exchange(info, account_address, state, audit_logs, ct_midnight_ts):
    try:
        fills = api_retry(info.user_fills, account_address)
        if not fills:
            return

        seen_hashes = set(state.get("seen_fill_hashes", []))
        new_entries = []
        now_ts = time.time()
        cutoff_ms = max((now_ts - 86400) * 1000, ct_midnight_ts * 1000)
        cooldown_added = False

        for f in fills:
            fill_time_ms = int(f.get("time", 0))
            if fill_time_ms < cutoff_ms:
                continue

            f_hash = str(f.get("hash") or f"{f.get('coin')}_{f.get('tid')}_{f.get('time')}")
            if f_hash in seen_hashes:
                continue

            closed_pnl = float(f.get("closedPnl", 0.0))
            f_dir = str(f.get("dir", "")).lower()

            if closed_pnl != 0.0 or "close" in f_dir:
                coin = f.get("coin")
                px = float(f.get("px", 0.0))
                sz = float(f.get("sz", 0.0))

                dt_fill = datetime.fromtimestamp(fill_time_ms / 1000.0, tz=timezone.utc)
                ct_dt = dt_fill.astimezone(ZoneInfo("America/Chicago")) if ZoneInfo else dt_fill
                ts_str = ct_dt.strftime('%Y-%m-%d %H:%M:%S')

                side = "LONG" if ("close long" in f_dir or "sell" in f_dir) else "SHORT"
                entry_px = px - (closed_pnl / sz) if sz > 0 else px
                roe_pct = (closed_pnl / (sz * entry_px / 5.0) * 100.0) if (sz > 0 and entry_px > 0) else 0.0

                if closed_pnl <= 0:
                    state["cooldown_blocklist"][coin] = now_ts + 14400
                    cooldown_added = True

                reason = "🎯 Native Exchange Orderbook Fill (Profit Target)" if closed_pnl > 0 else "🛡️ Native Exchange Orderbook Fill (Penny Stop)"

                entry_card = {
                    "coin": coin,
                    "entry_price": round_sig_figs(entry_px, 5),
                    "exit_price": round_sig_figs(px, 5),
                    "pnl_usd": round(closed_pnl, 2),
                    "roe_pct": round(roe_pct, 2),
                    "side": side,
                    "exit_reason": reason,
                    "timestamp": ts_str,
                    "ts_sec": fill_time_ms / 1000.0
                }
                new_entries.append((fill_time_ms, entry_card, f_hash))

        if new_entries:
            new_entries.sort(key=lambda x: x[0], reverse=True)
            existing_ledger = state.get("closed_trades_ledger", [])

            for fill_time_ms, entry_card, f_hash in new_entries:
                existing_ledger.insert(0, entry_card)
                seen_hashes.add(f_hash)
                if (now_ts - (fill_time_ms / 1000.0)) <= 900:
                    audit_logs.append(f"✅ EXCHANGED FILL SYNCED [{entry_card['coin']}]: {entry_card['side']} Exit @ ${entry_card['exit_price']} | PnL: ${entry_card['pnl_usd']:+.2f} ({entry_card['roe_pct']:+.2f}% ROE)")

            dedup_map = {}
            for t in existing_ledger:
                k = f"{t.get('coin')}_{t.get('ts_sec', 0)}"
                if k not in dedup_map:
                    dedup_map[k] = t
            
            sorted_ledger = sorted(list(dedup_map.values()), key=lambda x: float(x.get("ts_sec", 0)), reverse=True)
            state["closed_trades_ledger"] = sorted_ledger[:10]
            state["seen_fill_hashes"] = list(seen_hashes)[-100:]
            
            if cooldown_added:
                save_state(state)
    except Exception as e:
        audit_logs.append(f"⚠️ Direct fill sync notice: {e}")

def calculate_15m_ross_indicators(candles):
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
    cum_tp_v = np.sum(tp_v[-96:]) 
    cum_v = np.sum(vols[-96:])
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
        "signal_line": signal_line.iloc[-1],
        "prev_close": closes[-2],
        "prev_ema9": df_c.ewm(span=9, adjust=False).mean().iloc[-2]
    }

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
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23-V4.7.3 Engine Started (Dynamic Parabolic Trail Active).")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()
    is_15m_scan_window = True

    if now_ts < float(state.get("hibernating_until", 0)):
        remaining_hrs = (float(state["hibernating_until"]) - now_ts) / 3600.0
        print(f"[{timestamp}] 🚨 BOT IN 12H EMERGENCY HIBERNATION ({remaining_hrs:.1f}h remaining). Execution halted.", flush=True)
        return

    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = api_retry(Exchange, wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = api_retry(Info, constants.MAINNET_API_URL, skip_ws=True)

    sync_closed_trades_from_exchange(info, ACCOUNT_ADDRESS, state, audit_logs, ct_midnight_ts)

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

    user_state = api_retry(info.user_state, ACCOUNT_ADDRESS)
    spot_state = api_retry(info.spot_user_state, ACCOUNT_ADDRESS)
    
    meta_and_ctxs = api_retry(info.meta_and_asset_ctxs)
    meta = meta_and_ctxs[0]
    asset_ctxs = meta_and_ctxs[1]
    
    all_mids = api_retry(info.all_mids)
    now_ms = int(now_ts * 1000)

    gemini_risk, gemini_briefing = check_gemini_macro_shield(state, now_ts, is_15m_scan_window)
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] {gemini_briefing}")

    required_vol_ratio = 1.65 if gemini_risk == "HIGH" else 1.20
    hod_proximity_pct = 0.0150
    
    audit_logs.append(f"⚡ V4.7.3 Engine: Dynamic Parabolic Leash | Depth >= $2,000 | Spread <= 0.08% | 3.5m Pacing Gate Active")

    sz_decimals_map = {}
    for asset in meta.get("universe", []):
        coin_name = asset.get("name")
        sz_decimals_map[coin_name] = asset.get("szDecimals", 4)

    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []
    active_coins = set()
    total_margin_used = 0.0
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

            current_roe = (((current_px - entry_px) / entry_px) * 5.0) if is_long else (((entry_px - current_px) / entry_px) * 5.0)
            
            should_exit = False
            exit_reason = ""
            
            cache_info = current_active_cache.get(coin, {})
            entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9920)
            strat_type = cache_info.get("strategy", "BULLISH_RECLAIM")

            stag_map = state.get("stagnation_tracker", {})
            curr_stag = stag_map.get(coin, 0)
            if current_roe < 0.0050:
                curr_stag += 1
                if "stagnation_tracker" not in state:
                    state["stagnation_tracker"] = {}
                state["stagnation_tracker"][coin] = curr_stag
                if curr_stag >= 3:
                    should_exit = True
                    exit_reason = f"⏱️ 45M Range Stagnation Cut ({current_roe*100:.2f}% ROE)"
            else:
                if "stagnation_tracker" in state and coin in state["stagnation_tracker"]:
                    state["stagnation_tracker"][coin] = 0

            prev_peak = cache_info.get("peak_roe", current_roe)
            peak_roe = max(current_roe, prev_peak)

            stop_px_calc, tp_px_calc, current_roe_norm, target_floor_roe, leash_status = calculate_smart_exchange_targets(
                entry_px, is_long, current_px, entry_candle_low, peak_roe=peak_roe, coin=coin, setup_type=strat_type
            )

            if is_long and current_px <= stop_px_calc:
                should_exit = True
                exit_reason = f"🎯 Retention Ratchet Triggered ({current_roe*100:.2f}% ROE)"

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

                    if unrealized_pnl <= 0 or current_roe <= 0:
                        state["cooldown_blocklist"][coin] = now_ts + 14400
                    else:
                        state["cooldown_blocklist"][coin] = now_ts + 7200

                    state["closed_trades_ledger"].insert(0, {
                        "coin": coin, "entry_price": entry_px, "exit_price": current_px,
                        "pnl_usd": unrealized_pnl, "roe_pct": current_roe * 100, "side": "LONG" if is_long else "SHORT",
                        "exit_reason": exit_reason, "timestamp": timestamp, "ts_sec": now_ts
                    })
                    save_state(state)
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
                "peak_roe": peak_roe, "strategy": strat_type,
                "entry_candle_low": entry_candle_low
            }

            positions_data.append({
                "bot_title": "TR-GC-Crypto-LS-23-V4.7.3", "coin": coin,
                "side": "LONG" if is_long else "SHORT", "sz": abs(szi),
                "entry": entry_px, "current": current_px, "leverage": 5,
                "collateral": margin_used, "position_usd": pos_equity,
                "pnl": unrealized_pnl, "roe": current_roe * 100,
                "stop": round_sig_figs(stop_px_calc, 5),
                "tp_target": "UNCAPPED (80-88% Parabolic Trail)",
                "status": leash_status
            })

    state["active_position_cache"] = new_active_cache
    state["previous_active_coins"] = list(active_coins)

    market_candidates = []
    MAX_CRYPTO_SLOTS = 3
    available_slots = MAX_CRYPTO_SLOTS - active_count
    min_notional_usd = 10.50

    if available_slots > 0:
        state["last_scan_timestamp"] = now_ts

        all_eligible_coins = []
        for idx, asset in enumerate(meta.get("universe", [])):
            coin = asset.get("name")
            if not coin or coin in active_coins or coin in ["USDC", "USDT"]:
                continue

            try:
                ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                prev_px = float(ctx.get("prevDayPx", 0.0))
                mark_px = float(ctx.get("markPx", 0.0))

                if prev_px > 0 and mark_px > 0:
                    change_24h = ((mark_px - prev_px) / prev_px) * 100
                    all_eligible_coins.append({"coin": coin, "change_24h": change_24h, "rank": idx, "mark_px": mark_px})
            except Exception:
                continue

        all_eligible_coins = sorted(all_eligible_coins, key=lambda x: x["change_24h"], reverse=True)
        top_gapper_str = ", ".join([f"{g['coin']} ({g['change_24h']:+.2f}%)" for g in all_eligible_coins[:5]])
        audit_logs.append(f"🔥 TOP EXPLOSIVE GAPPERS (#20-#300+): {top_gapper_str if top_gapper_str else 'None'}")

        for item in all_eligible_coins[:35]:
            coin = item["coin"]
            idx = item["rank"]

            if idx < 20:
                continue

            cooldown_until = float(state.get("cooldown_blocklist", {}).get(coin, 0))
            if now_ts < cooldown_until:
                rem_mins = (cooldown_until - now_ts) / 60.0
                audit_logs.append(f"🔍 [{coin}] Skipped: Active Cooldown Blocklist ({rem_mins:.0f}m remaining)")
                continue

            try:
                curr_live_px = float(all_mids.get(coin, 0))
                if curr_live_px <= 0:
                    continue
                
                # Quality Liquidity Gate: $2,000 Depth & Max 0.08% Spread
                is_liquid, spread_msg = check_liquidity_and_spread(info, coin, max_spread=0.0008, min_depth_usd=2000.0)
                if not is_liquid:
                    audit_logs.append(f"🔍 [{coin}] Skipped: Liquidity/Spread Check Failed ({spread_msg})")
                    continue

                time.sleep(0.08)
                candles = api_retry(info.candles_snapshot, name=coin, interval="15m", startTime=now_ms - 86400000, endTime=now_ms)
                if not candles or len(candles) < 32:
                    continue

                current_candle_ts = candles[-1]["t"]
                if state.get("last_traded_candle", {}).get(coin) == current_candle_ts:
                    continue

                candle_start_ms = candles[-1]["t"]
                elapsed_mins = (now_ms - candle_start_ms) / 60000.0

                # 3.5-Minute Candle Pacing Gate
                if elapsed_mins < 3.5:
                    audit_logs.append(f"🔍 [{coin}] Skipped: Candle Pacing Gate ({elapsed_mins:.1f}m < 3.5m)")
                    continue

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                volumes = [float(c.get("v", 0)) for c in candles]

                day_high = max(highs[-96:]) if len(highs) >= 96 else max(highs)

                ross_ind = calculate_15m_ross_indicators(candles)
                if not ross_ind:
                    continue

                ema9 = ross_ind["ema9"]
                vwap = ross_ind["vwap"]
                macd_hist = ross_ind["macd_hist"]
                prev_close = ross_ind["prev_close"]
                prev_ema9 = ross_ind["prev_ema9"]

                extension_pct = (curr_live_px - ema9) / ema9
                if extension_pct > 0.0040:
                    audit_logs.append(f"🔍 [{coin}] Skipped: Extended Peak Guard (+{extension_pct*100:.2f}% > +0.40% limit)")
                    continue

                live_volume_raw = float(candles[-1].get("v", 0))
                extrap_mult = min(3.0, 15.0 / elapsed_mins)
                paced_volume = live_volume_raw * extrap_mult

                avg_vol = np.mean(volumes[-12:-2]) if len(volumes) >= 12 else (np.mean(volumes[:-1]) if len(volumes) > 1 else 1.0)
                vol_ratio = paced_volume / avg_vol if avg_vol > 0 else 1.0

                is_above_ema9 = curr_live_px > ema9
                is_above_vwap = curr_live_px > vwap
                is_macd_positive = macd_hist >= 0.0

                prev_low = lows[-2]

                # Setup 1: High of Day Breakout (SUPPRESSED IN HIGH RISK REGIME)
                is_near_hod = (curr_live_px >= day_high * (1.0 - hod_proximity_pct))
                is_breakout = (gemini_risk != "HIGH") and (is_near_hod and vol_ratio >= required_vol_ratio and is_above_ema9 and is_above_vwap and is_macd_positive)

                # Setup 2: Bullish Retracement Support Reclaim (SUPPRESSED IN HIGH RISK REGIME)
                is_reclaim_cross = (prev_close <= prev_ema9 or prev_close <= vwap) and (curr_live_px > ema9 and curr_live_px > vwap)
                is_pullback_proximity = (curr_live_px <= ema9 * 1.0025)
                is_bullish_reclaim = (gemini_risk != "HIGH") and (is_reclaim_cross and is_pullback_proximity and vol_ratio >= required_vol_ratio and is_macd_positive)

                # Setup 3: Pure V-Bottom Capitulation Sweep (100% EXCLUSIVE SETUP IN HIGH RISK REGIME)
                prev_30_min = min(lows[-31:-1])
                is_capitulation_low = (lows[-1] <= prev_30_min)
                candle_range = max(1e-8, highs[-1] - lows[-1])
                body_bottom = min(float(candles[-1]["o"]), curr_live_px)
                lower_wick_ratio = (body_bottom - lows[-1]) / candle_range
                
                is_v_bottom = (is_capitulation_low and lower_wick_ratio >= 0.55 and vol_ratio >= required_vol_ratio and curr_live_px > ema9)

                if is_breakout or is_bullish_reclaim or is_v_bottom:
                    setup_type = "V_BOTTOM_RECLAIM" if is_v_bottom else ("HOD_BREAKOUT" if is_breakout else "BULLISH_RECLAIM")
                    score = vol_ratio * (3.0 if setup_type == "V_BOTTOM_RECLAIM" else (1.5 if setup_type == "BULLISH_RECLAIM" else 1.0))

                    audit_logs.append(f"🔥 [{coin}] TRIGGERED [{setup_type}] @ ${curr_live_px:.4f} | RVOL:{vol_ratio:.2f}x | Ext:{extension_pct*100:+.2f}%")

                    market_candidates.append({
                        "coin": coin, "close": curr_live_px, "is_long": True, 
                        "score": score, "candle_ts": current_candle_ts,
                        "strategy": setup_type,
                        "entry_candle_low": prev_low
                    })
            except Exception:
                continue

        if not market_candidates:
            audit_logs.append(f"ℹ️ SCAN COMPLETED: 0 coins met V4.7.3 trigger rules on this candle.")
        else:
            audit_logs.append(f"🚀 SCAN COMPLETED: {len(market_candidates)} candidate(s) triggered entry setup!")

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    trades_executed = False

    # Enforce Single-Slot Entry Stagger (Max 1 Entry Per Pass)
    if available_slots > 0 and market_candidates:
        single_best_candidate = market_candidates[0]
        coin = single_best_candidate["coin"]
        px = single_best_candidate["close"]
        is_long = single_best_candidate["is_long"]
        candle_ts = single_best_candidate["candle_ts"]
        strat_used = single_best_candidate["strategy"]
        e_candle_low = single_best_candidate["entry_candle_low"]
        
        target_notional_usd = max(min_notional_usd, account_value * 0.25 * 0.85)
        decimals = sz_decimals_map.get(coin, 4)
        raw_sz = target_notional_usd / px

        sz = int(np.ceil(raw_sz)) if decimals == 0 else round(np.ceil(raw_sz * (10 ** decimals)) / (10 ** decimals), decimals)

        if (sz * px) < min_notional_usd:
            needed_sz = min_notional_usd / px
            sz = int(np.ceil(needed_sz)) if decimals == 0 else round(np.ceil(needed_sz * (10 ** decimals)) / (10 ** decimals), decimals)

        if sz > 0:
            try:
                try:
                    exchange.update_leverage(coin, 5, True)
                except Exception:
                    pass

                res = exchange.market_open(coin, is_long, sz, px, slippage=0.005)
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
                        "entry_candle_low": e_candle_low
                    }

                    initial_stop_px, initial_tp_px, _, _, initial_status = calculate_smart_exchange_targets(
                        px, is_long, px, e_candle_low, peak_roe=0.0, coin=coin, setup_type=strat_used
                    )
                    sync_native_trigger_orders(exchange, info, coin, is_long, sz, initial_stop_px, initial_tp_px, ACCOUNT_ADDRESS, audit_logs)

                    positions_data.append({
                        "bot_title": "TR-GC-Crypto-LS-23-V4.7.3", "coin": coin,
                        "side": "LONG" if is_long else "SHORT", "sz": sz,
                        "entry": px, "current": px, "leverage": 5,
                        "collateral": (sz * px) / 5.0, "position_usd": (sz * px),
                        "pnl": 0.0, "roe": 0.0,
                        "stop": round_sig_figs(initial_stop_px, 5),
                        "tp_target": "UNCAPPED (80-88% Parabolic Trail)",
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

        trades_today = [t for t in state.get("closed_trades_ledger", []) if float(t.get("ts_sec", 0)) >= ct_midnight_ts]
        total_today = len(trades_today)
        wins_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) > 0]
        losses_today = [t for t in trades_today if float(t.get("pnl_usd", 0)) <= 0]
        
        win_count = len(wins_today)
        loss_count = len(losses_today)
        win_rate_today = (win_count / total_today * 100) if total_today > 0 else 0.0
        net_today_usd = sum(float(t.get("pnl_usd", 0)) for t in trades_today)

        text_fallback = f"TR-GC-Crypto-LS-23-V4.7.3 | Parabolic Scalper\nTimestamp: {timestamp}\nTotal Net Worth: USD ${account_value:.2f}\nActive Positions: {active_count}/3"

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
        closed_ledger = sorted(state.get("closed_trades_ledger", []), key=lambda x: float(x.get("ts_sec", 0)), reverse=True)
        total_realized_pnl = sum(float(t.get('pnl_usd', 0.0)) for t in closed_ledger[:10])

        if closed_ledger:
            for t in closed_ledger[:10]:
                pnl_val = float(t.get('pnl_usd', 0.0))
                roe_val = float(t.get('roe_pct', 0.0))
                pnl_color = '#2e7d32' if pnl_val >= 0 else '#c62828'
                side_color = '#2e7d32' if t.get('side') == 'LONG' else '#c62828'
                ts_display = t.get('timestamp', 'N/A')
                closed_cards += f"""
                <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px;">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <div>
                      <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{t['coin']}</span>
                      <span style="font-size: 10px; color: {side_color}; font-weight: 700; margin-left: 6px;">{t.get('side', 'LONG')}</span>
                    </div>
                    <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">${pnl_val:+.2f} ({roe_val:+.2f}%)</span>
                  </div>
                  <div style="font-size: 11px; color: #475569; margin-bottom: 3px;">
                    <b>Entry:</b> ${round_sig_figs(t.get('entry_price', 0), 5)} &bull; <b>Exit:</b> ${round_sig_figs(t.get('exit_price', 0), 5)}
                  </div>
                  <div style="font-size: 10px; color: #64748b; margin-bottom: 3px;">
                    <b>Time:</b> {ts_display}
                  </div>
                  <div style="font-size: 10px; color: #b45309;">
                    <b>Reason:</b> {t.get('exit_reason', 'N/A')}
                  </div>
                </div>
                """
        else:
            closed_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No recent exits.</div>'

        total_pnl_color = '#2e7d32' if total_realized_pnl >= 0 else '#c62828'

        audit_rows = "".join([f"<tr><td style='padding: 6px 8px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 10px; color: #475569;'>{log}</td></tr>" for log in audit_logs])
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
  <head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06);">
      
      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff;">TR-GC-Crypto-LS-23 | Dynamic Parabolic Scalper (V4.7.3)</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; Forced Scan Active</div>
      </div>

      <div style="padding: 12px;">
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700;">Total Net Worth</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${account_value:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px;">
            <div>Reserve: <b style="color: #0f172a;">${static_usdc:.2f}</b> &bull; Margin: <b style="color: #0f172a;">{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain: <span style="color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
          </div>
        </div>

        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 10px; margin-bottom: 14px;">
          <div style="font-size: 11px; font-weight: 700; color: #15803d; text-transform: uppercase;">📊 Today's Realized Performance ({today_str})</div>
          <div style="font-size: 11px; color: #166534;">Trades: <b>{total_today}</b> &bull; Win Rate: <b>{win_count}W / {loss_count}L ({win_rate_today:.1f}%)</b></div>
          <div style="font-size: 11px; color: #166534; font-weight: 700; margin-top: 4px;">Realized P&amp;L: <span style="color: {'#15803d' if net_today_usd >= 0 else '#b91c1c'};">${net_today_usd:+.2f}</span></div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active V4.7.3 Parabolic Rules</div>
          <div>&bull; <b>88% Retention Cushion:</b> Locks 88% of gains with a guaranteed 1.80% ROE cushion below peak</div>
          <div>&bull; <b>Volume Decay Detection:</b> Distinguishes low-volume pullback wicks from real trend reversals</div>
          <div>&bull; <b>Quality Liquidity Gate:</b> Requires $2,000 depth and max 0.08% spread cap</div>
          <div>&bull; <b>Single-Slot Stagger:</b> Max 1 entry per scan pass to eliminate cluster entries</div>
        </div>

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades</div>
        {closed_cards}
        
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          TOTAL RECENT REALIZED P&L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        {audit_section}

      </div>
      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Hyperliquid 15M Scalper Autonomous Engine &bull; V4.7.3 Forced Scan Active
      </div>
    </div>
  </body>
</html>"""

        send_html_dashboard_email(f"Hyperliquid Report — USD ${account_value:.2f}", html_content, text_fallback)
    else:
        save_state(state)
        print(f"[{timestamp}] Execution cycle complete. Skipping email dispatch.", flush=True)

if __name__ == "__main__":
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] Executing 15M Parabolic Dynamic Scalper cycle...", flush=True)
    try:
        execute_engine()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 15M cycle completed successfully.", flush=True)
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
