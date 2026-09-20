#!/usr/bin/env python3
"""
TR-GC-Crypto-LS-23 Master Script
Hyperliquid Perpetual Futures Automated Execution Engine & Telemetry Dashboard
Standardized on google-genai SDK with prioritized model fallback cascade.
"""

import os
import sys
import json
import time
import math
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Hyperliquid & Ethereum SDK Imports
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

# Google GenAI SDK Imports
from google import genai
from google.genai import types

# ==============================================================================
# CONFIGURATION & GUARDRAILS SETUP
# ==============================================================================
MAX_SLOTS = 6
CI_ENTRY_MAX = 62.0
STAGNATION_MAX_RUNS = 48  # 24 Hours @ 30-min cron runs
MIN_POSITION_NAV_FLOOR = 50.0  # $50 USD Minimum Position Floor

GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash"
]

STATE_FILE = "hyperliquid_bot_state.json"

# ==============================================================================
# EXCHANGE INITIALIZATION & SAFE KEY LOADER
# ==============================================================================
def initialize_exchange():
    # Sanitize inputs by stripping spaces, quotes, and newlines
    raw_key = os.environ.get("HYPERLIQUID_SECRET_KEY", "").strip().strip('"').strip("'")
    wallet_address = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip().strip('"').strip("'")

    if not raw_key or raw_key == "YOUR_PRIVATE_KEY":
        raise ValueError(
            "CRITICAL: HYPERLIQUID_SECRET_KEY is missing or unassigned! "
            "Please check GitHub Repository Secrets and ensure 'HYPERLIQUID_SECRET_KEY' "
            "is explicitly passed in the workflow file env section."
        )

    if not wallet_address or wallet_address == "YOUR_WALLET_ADDRESS":
        raise ValueError(
            "CRITICAL: HYPERLIQUID_ACCOUNT_ADDRESS is missing or unassigned! "
            "Please check GitHub Repository Secrets and ensure 'HYPERLIQUID_ACCOUNT_ADDRESS' "
            "is explicitly passed in the workflow file env section."
        )

    account = Account.from_key(raw_key)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=wallet_address)
    return info, exchange, account.address

# ==============================================================================
# STATE PERSISTENCE HELPERS
# ==============================================================================
def load_bot_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load local state: {e}")
    return {"positions": {}}

def save_bot_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save state file: {e}")

# ==============================================================================
# GEMINI AI MACRO SHIELD & RISK ENGINE
# ==============================================================================
def run_gemini_macro_shield():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        return {"risk_level": "MODERATE", "assessment": "Gemini API key missing. Operating on standard risk parameters.", "recommendation": "Maintain standard execution."}

    client = genai.Client(api_key=api_key)
    prompt = (
        "Perform a real-time global crypto market risk assessment. Scan recent news for macro interest rate actions, "
        "regulatory actions (e.g., CLARITY Act updates), black swan events, or exchange solvency risks. "
        "Return a raw JSON object with keys: 'risk_level' ('LOW', 'MODERATE', 'HIGH'), "
        "'assessment' (2-3 sentences max), and 'recommendation' (1 sentence)."
    )

    for model_name in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"[WARN] Gemini model {model_name} failed: {e}. Cascading to next fallback...")
            continue

    return {"risk_level": "MODERATE", "assessment": "Market scanning fallback triggered.", "recommendation": "Maintain default risk settings."}

# ==============================================================================
# HYPERLIQUID DATA & LEDGER CALCULATIONS
# ==============================================================================
def fetch_net_worth_ledger(info, user_address):
    user_state = info.user_state(user_address)
    spot_state = info.spot_user_state(user_address)
    all_mids = info.all_mids()

    # Exact Signum Net Worth Replication
    spot_value = 0.0
    for balance in spot_state.get("balances", []):
        coin = balance["coin"]
        total_qty = float(balance["total"])
        if coin in all_mids:
            spot_value += total_qty * float(all_mids[coin])
        elif coin == "USDC":
            spot_value += total_qty

    margin_summary = user_state.get("marginSummary", {})
    total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))
    total_raw_usd = float(margin_summary.get("accountValue", 0.0))

    total_net_worth = spot_value + total_raw_usd
    unallocated_cash = max(0.0, total_net_worth - total_margin_used)

    return {
        "total_net_worth": total_net_worth,
        "total_margin_used": total_margin_used,
        "unallocated_cash": unallocated_cash,
        "user_state": user_state,
        "all_mids": all_mids
    }

def get_btc_regime(info):
    try:
        candles = info.candles_snapshot(name="BTC", interval="1d", startTime=int((time.time() - 86400 * 3) * 1000), endTime=int(time.time() * 1000))
        if candles:
            latest = candles[-1]
            open_price = float(latest["o"])
            close_price = float(latest["c"])
            return "GREEN" if close_price >= open_price else "RED"
    except Exception as e:
        print(f"[ERROR] BTC Regime Check failed: {e}")
    return "GREEN"  # Default fallback

# ==============================================================================
# SCANNER & QUEUE ENGINE
# ==============================================================================
def calculate_ci(candles, period=14):
    if len(candles) < period + 1:
        return 50.0
    
    total_tr = 0.0
    max_high = -float('inf')
    min_low = float('inf')

    for i in range(len(candles) - period, len(candles)):
        h = float(candles[i]["h"])
        l = float(candles[i]["l"])
        c_prev = float(candles[i-1]["c"])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        total_tr += tr
        max_high = max(max_high, h)
        min_low = min(min_low, l)

    denom = max_high - min_low
    if denom == 0:
        return 50.0
    
    ci = 100 * (math.log10(total_tr / denom) / math.log10(period))
    return ci

def scan_market_and_build_queue(info, existing_assets):
    all_mids = info.all_mids()
    candidates = []

    # Bidirectional Top 100 Scan
    for coin, mid_str in list(all_mids.items())[:100]:
        if coin in existing_assets or coin in ["BTC", "ETH"]:
            continue
        try:
            candles = info.candles_snapshot(name=coin, interval="1h", startTime=int((time.time() - 86400) * 1000), endTime=int(time.time() * 1000))
            if len(candles) < 15:
                continue
            
            ci = calculate_ci(candles)
            if ci > CI_ENTRY_MAX:
                continue  # Skip trade entries if Choppiness Index (CI) > 62

            curr_price = float(mid_str)
            prev_price = float(candles[-12]["c"])
            extension = (curr_price - prev_price) / prev_price
            
            highs = [float(c["h"]) for c in candles[-14:]]
            lows = [float(c["l"]) for c in candles[-14:]]
            atr_expansion = (max(highs) - min(lows)) / curr_price

            momentum_score = abs(extension) + atr_expansion

            candidates.append({
                "coin": coin,
                "price": curr_price,
                "score": round(momentum_score, 4),
                "ci": round(ci, 1),
                "is_ballistic": atr_expansion > 0.08
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

# ==============================================================================
# RATCHET & POSITION RENEWAL ENGINE
# ==============================================================================
def process_active_positions(info, exchange, user_address, state, logs):
    user_state = info.user_state(user_address)
    positions = user_state.get("assetPositions", [])
    all_mids = info.all_mids()
    active_coins = []

    for pos_wrapper in positions:
        pos = pos_wrapper.get("position", {})
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0.0))
        if szi == 0.0:
            continue

        active_coins.append(coin)
        entry_price = float(pos.get("entryPx", 1.0))
        curr_price = float(all_mids.get(coin, entry_price))
        is_long = szi > 0

        # Calculate ROE
        raw_pnl = (curr_price - entry_price) / entry_price if is_long else (entry_price - curr_price) / entry_price
        roe_pct = raw_pnl * 100.0

        pos_state = state["positions"].get(coin, {"stagnation_runs": 0, "peak_roe": roe_pct, "stop_price": 0.0})
        pos_state["stagnation_runs"] += 1
        pos_state["peak_roe"] = max(pos_state.get("peak_roe", roe_pct), roe_pct)

        # Micro-Ratchet Ladder Logic
        peak = pos_state["peak_roe"]
        new_stop_floor = None

        if peak >= 3.5:
            new_stop_floor = entry_price * 1.02 if is_long else entry_price * 0.98
        elif peak >= 2.0:
            new_stop_floor = entry_price * 1.01 if is_long else entry_price * 0.99
        elif peak >= 1.0:
            new_stop_floor = entry_price  # Break-Even
        elif peak >= 0.5:
            new_stop_floor = entry_price * 0.995 if is_long else entry_price * 1.005

        # Dynamic Trailing (+5% to +300% ROE)
        if peak >= 5.0:
            step_increments = math.floor((peak - 5.0) / 2.5)
            buffer = 0.01 + (step_increments * 0.005)
            trailing_stop = curr_price * (1.0 - buffer) if is_long else curr_price * (1.0 + buffer)
            new_stop_floor = max(new_stop_floor or 0, trailing_stop) if is_long else min(new_stop_floor or float('inf'), trailing_stop)

        if new_stop_floor:
            pos_state["stop_price"] = new_stop_floor
            # Auto-place/update active trigger stop-market order directly on exchange
            try:
                exchange.order(
                    coin, not is_long, abs(szi), new_stop_floor,
                    {"trigger": {"isMarket": True, "triggerPx": str(new_stop_floor), "tpsl": "sl"}},
                    reduce_only=True
                )
                logs.append(f"RATCHET UPDATED: {coin} Stop set to ${new_stop_floor:.4f} (Peak ROE: +{peak:.2f}%)")
            except Exception as e:
                logs.append(f"RATCHET TRIGGER ERR [{coin}]: {e}")

        # Stagnation Rotation (24h / 48 runs @ ROE < +1.5%)
        if pos_state["stagnation_runs"] >= STAGNATION_MAX_RUNS and roe_pct < 1.5:
            logs.append(f"STAGNATION ROTATION: Closing {coin} (Held {pos_state['stagnation_runs']} runs with ROE < +1.5%)")
            try:
                exchange.market_close(coin)
                if coin in state["positions"]:
                    del state["positions"][coin]
                continue
            except Exception as e:
                logs.append(f"STAGNATION CLOSE ERR [{coin}]: {e}")

        state["positions"][coin] = pos_state

    # Clean up closed positions in state
    for coin in list(state["positions"].keys()):
        if coin not in active_coins:
            del state["positions"][coin]

    save_bot_state(state)
    return active_coins

# ==============================================================================
# TRADE EXECUTION ENGINE
# ==============================================================================
def execute_open_entries(exchange, candidates, available_slots, net_worth, btc_regime, logs):
    if available_slots <= 0 or not candidates:
        return

    for candidate in candidates[:available_slots]:
        coin = candidate["coin"]
        price = candidate["price"]
        ci = candidate["ci"]
        is_ballistic = candidate["is_ballistic"]

        if btc_regime != "GREEN":
            logs.append(f"ENTRY BLOCKED [{coin}]: BTC Daily Candle is RED (Regime Shield Active)")
            continue

        # Sizing Rules
        size_pct = 0.16 if is_ballistic else 0.13
        allocated_usd = max(MIN_POSITION_NAV_FLOOR, net_worth * size_pct)
        sz = round(allocated_usd / price, 4)

        # Smart Downside Adaptive Stop Assignment
        if is_ballistic:
            stop_loss_pct = 0.035
            stop_type = "Ballistic (-3.5%)"
        elif ci > 58:
            stop_loss_pct = 0.012
            stop_type = "Choppy (-1.2%)"
        else:
            stop_loss_pct = 0.020
            stop_type = "Clean Trend (-2.0%)"

        stop_price = round(price * (1.0 - stop_loss_pct), 4)

        try:
            # 1. Market Order Entry
            entry_res = exchange.market_open(coin, is_buy=True, sz=sz)
            
            # 2. Native Trigger Order Placement directly on exchange orderbook
            exchange.order(
                coin, is_buy=False, sz=sz, limit_px=stop_price,
                order_type={"trigger": {"isMarket": True, "triggerPx": str(stop_price), "tpsl": "sl"}},
                reduce_only=True
            )
            
            logs.append(f"EXECUTION SUCCESS: Opened LONG on {coin} @ ${price} | Size: ${allocated_usd:.2f} | Stop: ${stop_price} ({stop_type})")
        except Exception as e:
            logs.append(f"EXECUTION FAILED [{coin}]: {e}")

# ==============================================================================
# TELEMETRY DASHBOARD & EMAIL GENERATOR
# ==============================================================================
def build_and_send_telemetry(ledger_data, macro_shield, btc_regime, active_positions_info, queue_candidates, logs):
    email_sender = os.environ.get("EMAIL_SENDER", "").strip().strip('"').strip("'")
    email_password = os.environ.get("EMAIL_PASSWORD", "").strip().strip('"').strip("'")
    email_recipient = os.environ.get("EMAIL_RECIPIENT", email_sender).strip().strip('"').strip("'")

    net_worth = ledger_data["total_net_worth"]
    unallocated = ledger_data["unallocated_cash"]
    margin_used = ledger_data["total_margin_used"]
    margin_util = (margin_used / net_worth * 100.0) if net_worth > 0 else 0.0

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Active Positions Table Rows
    pos_rows = ""
    for p in active_positions_info:
        pnl_color = "#2e7d32" if p["pnl_usd"] >= 0 else "#c62828"
        
        pos_rows += f"""
        <tr>
            <td style="padding:10px; border-bottom:1px solid #eee;">TR-GC-Crypto-LS-23</td>
            <td style="padding:10px; border-bottom:1px solid #eee;"><b>{p['coin']}</b></td>
            <td style="padding:10px; border-bottom:1px solid #eee;">1x</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">LONG</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">${p['collateral']:.2f}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">${p['position_usd']:.2f}</td>
            <td style="padding:10px; border-bottom:1px solid #eee; color:{pnl_color}; font-weight:bold;">
                +${p['pnl_usd']:.2f} (+{p['roe']:.2f}%)
            </td>
            <td style="padding:10px; border-bottom:1px solid #eee;">${p['buy_price']}</td>
            <td style="padding:10px; border-bottom:1px solid #eee; color:#d32f2f;">${p['stop_price']}</td>
            <td style="padding:10px; border-bottom:1px solid #eee; color:#2e7d32; font-weight:bold;">Active</td>
        </tr>
        """

    if not pos_rows:
        pos_rows = "<tr><td colspan='10' style='padding:15px; text-align:center; color:#777;'>No Active Positions</td></tr>"

    # Queue Table Rows
    queue_rows = ""
    for idx, c in enumerate(queue_candidates[:3], 1):
        queue_rows += f"""
        <tr>
            <td style="padding:10px; border-bottom:1px solid #eee;">#{idx}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;"><b>{c['coin']}</b></td>
            <td style="padding:10px; border-bottom:1px solid #eee;">${c['price']}</td>
            <td style="padding:10px; border-bottom:1px solid #eee; color:#e65100; font-weight:bold;">Score: {c['score']}</td>
        </tr>
        """

    log_lines = "<br>".join(logs)

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; margin:0; padding:20px;">
        <div style="max-width: 1100px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08);">
            
            <div style="background: #0d1b2a; color: #ffffff; padding: 20px;">
                <h2 style="margin:0; font-size: 20px;">TR-GC-Crypto-LS-23 | Telemetry Dashboard</h2>
                <p style="margin:5px 0 0 0; font-size: 12px; color: #8d99ae;">Timestamp: {timestamp} | Mode: LIVE / REAL-TIME UPDATE</p>
            </div>

            <div style="padding: 20px;">
                <p style="font-size: 12px; color: #555; text-transform: uppercase; margin:0;">Total Net Worth</p>
                <h1 style="margin: 5px 0; color: #1b263b;">USD ${net_worth:.2f}</h1>
                <p style="font-size: 12px; color: #666; margin:0;">
                    Static Unallocated USDC Reserve: <b>${unallocated:.2f}</b> | Margin Utilization: <b>{margin_util:.1f}%</b>
                </p>

                <div style="background: #e8f5e9; border-left: 4px solid #2e7d32; padding: 15px; margin-top: 20px; border-radius: 4px;">
                    <span style="background: #2e7d32; color: #fff; padding: 3px 8px; font-size: 11px; font-weight: bold; border-radius: 3px;">
                        RISK LEVEL: {macro_shield.get('risk_level', 'MODERATE')}
                    </span>
                    <p style="margin: 8px 0 0 0; font-size: 13px; color: #1b5e20;">
                        <b>Live Market Assessment:</b> {macro_shield.get('assessment', '')}
                    </p>
                    <p style="margin: 4px 0 0 0; font-size: 12px; color: #2e7d32;">
                        <b>Execution Recommendation:</b> {macro_shield.get('recommendation', '')} | BTC Regime: <b>{btc_regime}</b>
                    </p>
                </div>

                <h3 style="margin-top: 30px; color: #1b263b; font-size: 15px;">POSITIONS PER BOT (USD)</h3>
                <table style="width:100%; border-collapse: collapse; font-size: 13px;">
                    <thead>
                        <tr style="background: #f0f4f8; text-align: left; color: #333;">
                            <th style="padding:10px;">Bot Title</th>
                            <th style="padding:10px;">Asset</th>
                            <th style="padding:10px;">Leverage</th>
                            <th style="padding:10px;">Side</th>
                            <th style="padding:10px;">Collateral USD</th>
                            <th style="padding:10px;">Position USD</th>
                            <th style="padding:10px;">Unrealized P&L USD</th>
                            <th style="padding:10px;">Buy Price</th>
                            <th style="padding:10px;">Stop Price</th>
                            <th style="padding:10px;">Bot Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {pos_rows}
                    </tbody>
                </table>

                <h3 style="margin-top: 30px; color: #1b263b; font-size: 15px;">ON-DECK SMART QUEUE (TOP 3 WAITING RUNNERS)</h3>
                <table style="width:100%; border-collapse: collapse; font-size: 13px;">
                    <thead>
                        <tr style="background: #f0f4f8; text-align: left; color: #333;">
                            <th style="padding:10px;">Rank</th>
                            <th style="padding:10px;">Asset</th>
                            <th style="padding:10px;">Current Price</th>
                            <th style="padding:10px;">Momentum Score</th>
                        </tr>
                    </thead>
                    <tbody>
                        {queue_rows}
                    </tbody>
                </table>

                <h3 style="margin-top: 30px; color: #1b263b; font-size: 15px;">LIVE TEST TELEMETRY & AUDIT LOG</h3>
                <div style="background: #fffde7; border: 1px solid #fff59d; padding: 15px; font-family: monospace; font-size: 12px; color: #333; border-radius: 4px;">
                    {log_lines}
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    if email_sender and email_password:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Hyperliquid Report — USD ${net_worth:.2f}"
            msg["From"] = email_sender
            msg["To"] = email_recipient
            msg.attach(MIMEText(html_content, "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(email_sender, email_password)
                server.sendmail(email_sender, email_recipient, msg.as_string())
            print("[INFO] Telemetry email dispatched successfully.")
        except Exception as e:
            print(f"[ERROR] Failed to send email: {e}")

# ==============================================================================
# MAIN PIPELINE EXECUTION
# ==============================================================================
def main():
    print("==================================================================")
    print("Starting TR-GC-Crypto-LS-23 Pipeline Execution...")
    print("==================================================================")

    logs = []
    logs.append(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] Telemetry Engine Started.")

    state = load_bot_state()
    info, exchange, user_address = initialize_exchange()

    # 1. AI Shield & Macro Checks
    macro_shield = run_gemini_macro_shield()
    btc_regime = get_btc_regime(info)
    logs.append(f"Gemini AI Shield: [{macro_shield.get('risk_level')}] {macro_shield.get('assessment')}")
    logs.append(f"BTC Regime Check: {btc_regime} -> {'LONGs Allowed' if btc_regime == 'GREEN' else 'LONGs Blocked'}")

    # 2. Process active position ratchets & close stagnated trades
    active_coins = process_active_positions(info, exchange, user_address, state, logs)

    # 3. Market Scan & Smart Queue Building
    queue_candidates = scan_market_and_build_queue(info, active_coins)
    logs.append(f"Scan Complete: Found {len(queue_candidates)} valid breakouts.")

    # 4. EXECUTE TRADES FIRST FOR OPEN SLOTS
    open_slots = MAX_SLOTS - len(active_coins)
    if open_slots > 0 and queue_candidates:
        logs.append(f"Open slots available ({open_slots}/{MAX_SLOTS}). Triggering order placements...")
        
        initial_ledger = fetch_net_worth_ledger(info, user_address)
        execute_open_entries(exchange, queue_candidates, open_slots, initial_ledger["total_net_worth"], btc_regime, logs)

        # 5. API Settlement Cooldown (2.0 Seconds)
        logs.append("Pausing 2.0s for Hyperliquid REST orderbook settlement...")
        time.sleep(2.0)

    # 6. RE-FETCH ACCOUNT STATE POST-EXECUTION (Real-time email accuracy fix)
    post_trade_ledger = fetch_net_worth_ledger(info, user_address)
    user_state = post_trade_ledger["user_state"]
    all_mids = post_trade_ledger["all_mids"]

    # Re-build Active Positions Data for Email Report
    active_positions_info = []
    current_active_coins = []
    for pos_wrapper in user_state.get("assetPositions", []):
        pos = pos_wrapper.get("position", {})
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0.0))
        if szi == 0.0:
            continue

        current_active_coins.append(coin)
        entry_price = float(pos.get("entryPx", 1.0))
        curr_price = float(all_mids.get(coin, entry_price))
        position_usd = abs(szi) * curr_price
        collateral = position_usd / 1.0
        
        raw_pnl = (curr_price - entry_price) / entry_price if szi > 0 else (entry_price - curr_price) / entry_price
        pnl_usd = collateral * raw_pnl
        roe_pct = raw_pnl * 100.0

        pos_state = state["positions"].get(coin, {})
        stop_price = pos_state.get("stop_price", round(entry_price * 0.98, 4))

        active_positions_info.append({
            "coin": coin,
            "collateral": collateral,
            "position_usd": position_usd,
            "pnl_usd": pnl_usd,
            "roe": roe_pct,
            "buy_price": entry_price,
            "stop_price": stop_price
        })

    # Filter On-Deck Queue to exclude assets that were just purchased
    remaining_queue = [c for c in queue_candidates if c["coin"] not in current_active_coins]

    # 7. BUILD & SEND TELEMETRY EMAIL DASHBOARD
    build_and_send_telemetry(
        post_trade_ledger,
        macro_shield,
        btc_regime,
        active_positions_info,
        remaining_queue,
        logs
    )

    print("==================================================================")
    print("Execution complete. Active slots occupied:", len(active_positions_info))
    print("==================================================================")

if __name__ == "__main__":
    main()
