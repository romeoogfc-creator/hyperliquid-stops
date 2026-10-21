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

# Hyperliquid & Environment Credentials
HL_ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS")
HL_SECRET_KEY = os.getenv("HL_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STATE_FILE = "state.json"
BASE_URL = "https://api.hyperliquid.xyz/info"

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

def load_state():
    default_state = {
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "cooldown_blocklist": {},
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
            print(f"Error loading state.json: {e}", flush=True)
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

def fetch_gemini_ai_risk_shield():
    if not GEMINI_API_KEY:
        return "HIGH", "Gemini API key missing. Defaulting to HIGH risk shield."
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        prompt = (
            "Analyze current cryptocurrency macro market conditions. Respond strictly in JSON format: "
            '{"risk_level": "LOW" | "MEDIUM" | "HIGH", "reasoning": "brief explanation"}'
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        res = api_retry(requests.post, url, json=payload, headers={"Content-Type": "application/json"})
        if res and res.status_code == 200:
            text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            text_clean = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text_clean)
            return parsed.get("risk_level", "HIGH"), parsed.get("reasoning", "Parsed response successfully.")
    except Exception as e:
        print(f"Gemini AI Shield Error: {e}", flush=True)
    return "HIGH", "Fallback to HIGH risk due to connection or parsing issue."

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
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Crypto telemetry email sent successfully.", flush=True)
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)

def round_sig_figs(val, sig_figs=5):
    if val == 0 or val is None:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_15m_indicators(candles):
    if len(candles) < 20:
        return None

    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    vols = [float(c["v"]) for c in candles]

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
        "macd_hist": macd_hist
    }

def calculate_retention_targets(entry_px, is_long, current_px, entry_candle_low, leverage=5.0, peak_roe=0.0, upper_wick_pct=0.0):
    roe = (((current_px - entry_px) / entry_px) * leverage) if is_long else (((entry_px - current_px) / entry_px) * leverage)
    initial_floor_roe = -0.0350  # -3.50% ROE (-0.70% Px drop @ 5x)
    drawdown_from_peak = peak_roe - roe

    # Climax Exhaustion Lock
    is_exhaustion = (peak_roe >= 0.0450) and (drawdown_from_peak >= 0.0080 or upper_wick_pct >= 0.45)

    if is_exhaustion:
        target_floor_roe = max(peak_roe - 0.0035, roe)
        leash_status = f"⚡ CLIMAX EXHAUSTION LOCK [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0800:
        target_floor_roe = max(peak_roe * 0.88, peak_roe - 0.0150)
        leash_status = f"🚀 UNCAPPED MOONSHOT (88% LOCKED) [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0400:
        target_floor_roe = max(peak_roe * 0.80, peak_roe - 0.0100)
        leash_status = f"🎯 EXPANSION RATCHET (80% LOCKED) [Peak +{peak_roe*100:.2f}% -> Stop +{target_floor_roe*100:.2f}%]"
    elif peak_roe >= 0.0120:
        target_floor_roe = 0.0030
        leash_status = f"🛡 FEE FLOOR LOCKED (+0.30% ROE)"
    else:
        target_floor_roe = initial_floor_roe
        leash_status = f"🛡 STRUCTURAL 15M STOP ({target_floor_roe*100:.2f}% ROE)"

    stop_px = entry_px * (1 + (target_floor_roe / leverage)) if is_long else entry_px * (1 - (target_floor_roe / leverage))
    return max(stop_px, entry_candle_low) if is_long else min(stop_px, entry_candle_low), leash_status

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    ct_now = get_central_time()
    today_str = ct_now.strftime('%Y-%m-%d')
    now_ts = time.time()

    audit_logs = []
    audit_logs.append(f"[{timestamp}] TR-GC-Crypto-LS-23-V4.8.9 Engine Started (15M Calibrated Engine Active).")

    gemini_risk, gemini_reason = fetch_gemini_ai_risk_shield()
    audit_logs.append(f"Gemini AI Shield: [{gemini_risk}] Risk Level: **{gemini_risk}** — {gemini_reason}")

    state = load_state()

    # Clean active cooldown blocklist
    cooldowns = state.get("cooldown_blocklist", {})
    active_cooldowns = {c: t for c, t in cooldowns.items() if t > now_ts}
    state["cooldown_blocklist"] = active_cooldowns

    # Fetch User Account State from Hyperliquid
    user_state_res = api_retry(requests.post, BASE_URL, json={"type": "clearinghouseState", "user": HL_ACCOUNT_ADDRESS})
    if not user_state_res or user_state_res.status_code != 200:
        raise Exception(f"Failed to fetch clearinghouseState: {user_state_res.text if user_state_res else 'No Response'}")
    
    user_data = user_state_res.json()
    margin_summary = user_data.get("marginSummary", {})
    equity = float(margin_summary.get("accountValue", 100.0))
    total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))
    reserve = max(0.0, equity - total_margin_used)
    margin_util_pct = (total_margin_used / equity * 100) if equity > 0 else 0.0

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

    positions_list = user_data.get("assetPositions", [])
    active_positions = [p["position"] for p in positions_list if float(p["position"]["szi"]) != 0]
    active_count = len(active_positions)

    positions_data = []
    active_coins = set()
    current_active_cache = state.get("active_position_cache", {})
    new_active_cache = {}

    for pos in active_positions:
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0))
        is_long = szi > 0
        entry_px = float(pos.get("entryPx", 0))
        mark_px = float(pos.get("positionValue", 0)) / abs(szi) if abs(szi) > 0 else entry_px
        margin_used = float(pos.get("marginUsed", 0))
        unrealized_pnl = float(pos.get("returnOnEquity", 0)) * margin_used
        leverage = float(pos.get("leverage", {}).get("value", 5.0))
        
        active_coins.add(coin)
        current_roe = (((mark_px - entry_px) / entry_px) * leverage) if is_long else (((entry_px - mark_px) / entry_px) * leverage)

        cache_info = current_active_cache.get(coin, {})
        entry_candle_low = cache_info.get("entry_candle_low", entry_px * 0.9930)
        entry_time = cache_info.get("entry_time", now_ts)
        prev_peak = cache_info.get("peak_roe", current_roe)
        peak_roe = max(current_roe, prev_peak)

        upper_wick_ratio = 0.0
        try:
            candle_res = api_retry(requests.post, BASE_URL, json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": int((now_ts - 3600) * 1000)}})
            if candle_res and candle_res.status_code == 200:
                c_list = candle_res.json()
                if c_list:
                    last_c = c_list[-1]
                    c_high, c_low, c_open = float(last_c["h"]), float(last_c["l"]), float(last_c["o"])
                    c_range = max(1e-8, c_high - c_low)
                    body_top = max(c_open, mark_px)
                    upper_wick_ratio = (c_high - body_top) / c_range
        except Exception:
            pass

        stop_px_calc, leash_status = calculate_retention_targets(
            entry_px, is_long, mark_px, entry_candle_low, leverage=leverage, peak_roe=peak_roe, upper_wick_pct=upper_wick_ratio
        )

        audit_logs.append(f"NATIVE SL SYNC [{coin}]: Hard Stop @ ${stop_px_calc:.5f}")

        # FIX 3: 15-Minute Negative Stagnation Scratch
        held_duration_mins = (now_ts - entry_time) / 60.0
        should_exit = False
        exit_reason = ""

        if is_long and mark_px <= stop_px_calc:
            should_exit = True
            exit_reason = f"🎯 Retention Ratchet Triggered ({current_roe*100:+.2f}% ROE)"
        elif held_duration_mins >= 15.0 and current_roe < 0.0000:
            should_exit = True
            exit_reason = f"⏱️ 15M Negative Stagnation Scratch ({current_roe*100:.2f}% ROE after {held_duration_mins:.0f}m)"
        elif held_duration_mins >= 30.0 and current_roe < 0.0030:
            should_exit = True
            exit_reason = f"⏱️ 30M Flat Stagnation Exit ({current_roe*100:.2f}% ROE after {held_duration_mins:.0f}m)"

        if should_exit:
            audit_logs.append(f"EXIT TRIGGERED: Closing {coin} LONG @ ${mark_px:.5f} ({exit_reason}).")
            
            # FIX 4: 4-Hour Post-Win Lockout vs 30-Min Loss Cooldown
            if unrealized_pnl >= 0.10 or current_roe >= 0.0150:
                state["cooldown_blocklist"][coin] = now_ts + 14400  # 4-Hour Post-Win Lockout
                audit_logs.append(f"🔒 POST-WIN LOCKOUT: {coin} locked for 4 hours to prevent re-entering local top.")
            else:
                state["cooldown_blocklist"][coin] = now_ts + 1800   # 30-Min Loss Cooldown

            if "closed_trades_ledger" not in state:
                state["closed_trades_ledger"] = []
            
            state["closed_trades_ledger"].insert(0, {
                "coin": coin, "entry_price": entry_px, "exit_price": mark_px,
                "exit_reason": exit_reason, "realized_pnl": unrealized_pnl, "timestamp": timestamp
            })
            state["closed_trades_ledger"] = state["closed_trades_ledger"][:20]
            active_count -= 1
            continue

        new_active_cache[coin] = {
            "entry_px": entry_px, "mark_px": mark_px, "szi": szi, "peak_roe": peak_roe,
            "entry_candle_low": entry_candle_low, "entry_time": entry_time
        }

        positions_data.append({
            "bot_title": "TR-GC-Crypto-LS-23",
            "coin": coin,
            "side": "LONG" if is_long else "SHORT",
            "val": abs(szi) * mark_px,
            "cost": margin_used,
            "roe": current_roe * 100,
            "stop": round_sig_figs(stop_px_calc, 5),
            "status": leash_status
        })

    state["active_position_cache"] = new_active_cache

    # Gapper Scanning Pipeline
    MAX_SLOTS = 3
    market_candidates = []
    
    if active_count < MAX_SLOTS:
        # Fetch Top Gappers Context
        meta_res = api_retry(requests.post, BASE_URL, json={"type": "metaAndAssetCtxs"})
        if meta_res and meta_res.status_code == 200:
            meta_data = meta_res.json()
            universe = meta_data[0]["universe"]
            asset_ctxs = meta_data[1]

            gappers = []
            for idx, ctx in enumerate(asset_ctxs[:200]):
                coin_name = universe[idx]["name"]
                day_change = float(ctx.get("dayNtlVlm", 0))
                prev_day_px = float(ctx.get("prevDayPx", 1.0))
                mark_px = float(ctx.get("markPx", 1.0))
                pct_change = ((mark_px - prev_day_px) / prev_day_px) * 100.0

                if pct_change >= 5.0 and coin_name not in active_coins:
                    # Check active cooldowns
                    cd_until = state.get("cooldown_blocklist", {}).get(coin_name, 0)
                    if cd_until > now_ts:
                        rem_mins = int((cd_until - now_ts) / 60)
                        audit_logs.append(f"[{coin_name}] Skipped: Active Cooldown Blocklist ({rem_mins}m remaining)")
                        continue
                    gappers.append({"coin": coin_name, "change": pct_change, "mark_px": mark_px})

            gappers = sorted(gappers, key=lambda x: x["change"], reverse=True)[:20]
            top_gapper_names = [f"{g['coin']} (+{g['change']:.2f}%)" for g in gappers[:5]]
            audit_logs.append(f"TOP EXPLOSIVE GAPPERS (#20-#300+): {', '.join(top_gapper_names)}")

            # Evaluate Candle Triggers for Candidates
            for gapper in gappers:
                coin_name = gapper["coin"]
                candle_res = api_retry(requests.post, BASE_URL, json={"type": "candleSnapshot", "req": {"coin": coin_name, "interval": "15m", "startTime": int((now_ts - 28800) * 1000)}})
                if not candle_res or candle_res.status_code != 200:
                    continue
                
                candles = candle_res.json()
                if len(candles) < 20:
                    continue

                closes = [float(c["c"]) for c in candles]
                highs = [float(c["h"]) for c in candles]
                lows = [float(c["l"]) for c in candles]
                vols = [float(c["v"]) for c in candles]

                curr_px = closes[-1]
                prev_high = highs[-2]
                prev_low = lows[-2]

                # Intrabar Paced RVOL Formula
                bar_start_dt = datetime.fromtimestamp(candles[-1]["t"] / 1000.0, tz=timezone.utc)
                elapsed_mins = max(0.5, (datetime.now(timezone.utc) - bar_start_dt).total_seconds() / 60.0)
                extrap_mult = min(3.0, 15.0 / elapsed_mins)
                paced_volume = vols[-1] * extrap_mult

                avg_vol = np.mean(vols[-12:-2]) if len(vols) >= 12 else np.mean(vols[:-1])
                vol_ratio = paced_volume / avg_vol if avg_vol > 0 else 1.0

                ind = calculate_15m_indicators(candles)
                if not ind:
                    continue

                ema9 = ind["ema9"]
                vwap = ind["vwap"]
                macd_hist = ind["macd_hist"]

                # FIX 1 & 2: HIGH Risk RVOL Gate & VWAP Extension Cap
                required_vol_ratio = 2.50 if gemini_risk == "HIGH" else 1.50
                max_vwap_extension = 1.0050 if gemini_risk == "HIGH" else 1.0150
                vwap_dist = curr_px / vwap

                is_above_ema9 = curr_px > ema9
                is_above_vwap = curr_px > vwap
                is_macd_frontside = macd_hist > 0.0

                is_breakout = (curr_px >= prev_high) and (vol_ratio >= required_vol_ratio) and (vwap_dist <= max_vwap_extension)

                if spy_green if 'spy_green' in locals() else True and is_above_ema9 and is_above_vwap and is_macd_frontside and is_breakout:
                    score = vol_ratio * 1.5
                    market_candidates.append({
                        "coin": coin_name, "close": curr_px, "score": score, "entry_candle_low": prev_low
                    })
                    audit_logs.append(
                        f"🔥 HIGH-CONVICTION TRIGGER: {coin_name} @ ${curr_px:.4f} "
                        f"(Paced RVOL: {vol_ratio:.2f}x [Req: {required_vol_ratio:.2f}x], VWAP Ext: {vwap_dist:.4f}x)"
                    )

    market_candidates = sorted(market_candidates, key=lambda x: x["score"], reverse=True)
    audit_logs.append(f"SCAN COMPLETED: {len(market_candidates)} coins met V4.8.9 trigger rules on this candle.")

    save_state(state)

    # HTML Dashboard Rendering
    positions_cards = ""
    if positions_data:
        for p in positions_data:
            pnl_color = '#2e7d32' if p['roe'] >= 0 else '#c62828'
            positions_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{p['coin']}</span>
                  <span style="font-size: 10px; color: #2e7d32; font-weight: 700; margin-left: 6px;">{p['side']} (5x)</span>
                </div>
                <span style="font-weight: 700; font-size: 13px; color: {pnl_color};">{p['roe']:+.2f}%</span>
              </div>
              <div style="font-size: 11px; color: #475569; margin-bottom: 4px;">
                <b>Val:</b> ${p['val']:.2f} &bull; <b>Cost:</b> ${p['cost']:.2f} &bull; <b>Stop:</b> ${p['stop']}
              </div>
              <div style="font-size: 10px; color: #2e7d32; font-weight: 600;">
                <b>Status:</b> {p['status']}
              </div>
            </div>
            """
    else:
        positions_cards = '<div style="padding: 12px; text-align: center; color: #666666; font-size: 11px; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;">No active positions (100% Cash Reserve).</div>'

    closed_cards = ""
    closed_ledger = state.get("closed_trades_ledger", [])
    total_realized_pnl = 0.0

    if closed_ledger:
        for t in closed_ledger[:10]:
            pnl_val = float(t.get('realized_pnl', 0.0))
            total_realized_pnl += pnl_val
            pnl_color = '#2e7d32' if pnl_val >= 0 else '#c62828'
            coin = t.get('coin', 'N/A')
            entry_px_formatted = round_sig_figs(t.get('entry_price', 0), 5)
            exit_px_formatted = round_sig_figs(t.get('exit_price', 0), 5)
            reason = t.get('exit_reason', 'N/A')
            ts_str = t.get('timestamp', '')

            closed_cards += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 8px; box-sizing: border-box;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                <div>
                  <span style="font-weight: 700; font-size: 13px; color: #0f172a;">{coin} LONG</span>
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

    text_fallback = f"TR-GC-Crypto-LS-23 | V4.8.9 15M Calibrated Scalper\nTimestamp: {timestamp}\nTotal Net Worth: USD ${equity:.2f}\nToday's Gain: USD ${today_total_gain:+.2f}"

    html_content = f"""<!DOCTYPE html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
  </head>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 8px; color: #333333; -webkit-text-size-adjust: 100%;">
    <div style="max-width: 500px; width: 100%; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); box-sizing: border-box;">
      
      <div style="background-color: #0f172a; color: #ffffff; padding: 14px;">
        <div style="font-size: 15px; font-weight: 700; color: #ffffff; margin: 0;">TR-GC-Crypto-LS-23 | 15M Calibrated Scalper (V4.8.9)</div>
        <div style="font-size: 11px; color: #94a3b8; margin-top: 3px;">Timestamp: {timestamp} &bull; V4.8.9 15M Active</div>
      </div>

      <div style="padding: 12px; box-sizing: border-box;">
        
        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 12px; margin-bottom: 14px; box-sizing: border-box;">
          <div style="font-size: 10px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 2px;">Total Net Worth</div>
          <div style="font-size: 22px; font-weight: 700; color: #0f172a;">USD ${equity:,.2f}</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; border-top: 1px solid #e2e8f0; padding-top: 6px; line-height: 1.6;">
            <div>Reserve: <b>${reserve:,.2f}</b> &bull; Margin: <b>{margin_util_pct:.1f}%</b></div>
            <div>Today's Gain: <span style="background-color: {'#e6f4ea' if today_total_gain >= 0 else '#fce8e6'}; color: {'#137333' if today_total_gain >= 0 else '#c5221f'}; padding: 2px 6px; border-radius: 4px; font-weight: bold;">${today_total_gain:+,.2f}</span></div>
          </div>
        </div>

        <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; margin-bottom: 14px; font-size: 11px; color: #334155; line-height: 1.4; box-sizing: border-box;">
          <div style="font-weight: 700; text-transform: uppercase; margin-bottom: 4px; font-size: 10px; color: #0f172a;">✅ Active V4.8.9 15M Calibrations</div>
          <div>&bull; <b>HIGH-Risk Gate:</b> Requires RVOL &ge; 2.50x during HIGH macro risk</div>
          <div>&bull; <b>VWAP Extension Cap:</b> Clamps market entries to max +0.50% above VWAP</div>
          <div>&bull; <b>15M Negative Scratch:</b> Instantly cuts negative trades after 15 minutes</div>
          <div>&bull; <b>4-Hour Post-Win Lockout:</b> Blocks re-entering top exhaustion on winning assets</div>
        </div>

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 12px 0 6px 0; font-weight: 700;">Active Positions</div>
        {positions_cards}

        <div style="font-size: 11px; text-transform: uppercase; color: #475569; margin: 14px 0 6px 0; font-weight: 700;">Recently Closed Trades</div>
        {closed_cards}
        
        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; margin-top: 8px; text-align: right; font-weight: bold; font-size: 11px;">
          TOTAL RECENT REALIZED P&amp;L: <span style="color: {total_pnl_color};">${total_realized_pnl:+.2f}</span>
        </div>

        {audit_section}

      </div>

      <div style="text-align: center; font-size: 10px; color: #94a3b8; padding: 10px; background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
        Hyperliquid 15M Scalper Autonomous Engine &bull; V4.8.9 Active
      </div>
    </div>
  </body>
</html>
"""

    send_html_dashboard_email(f"Hyperliquid Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] V4.8.9 Crypto engine execution complete.", flush=True)

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Crypto Engine execution error: {e}"
        print(err_msg, flush=True)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
