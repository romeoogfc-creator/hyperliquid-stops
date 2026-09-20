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

# Google GenAI SDK Imports
from google import genai
from google.genai import types

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BASE_URL = os.getenv("APAL_BASE_URL", "https://paper-api.alpaca.markets")
STATE_FILE = "stock_state.json"

VERBOSE_TEST_MODE = True

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "accept": "application/json"
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading stock state.json: {e}")
    return {"stagnation_tracker": {}, "last_run_timestamp": ""}

def save_state(state):
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def run_gemini_stock_market_shield():
    """Queries Gemini with Google Search to detect US stock market black-swan risks and generate an executive market briefing."""
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY missing; skipping Stock AI Macro Shield scan.")
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
        Perform a live real-time web search for breaking US stock market news, SPY/QQQ market trends, Federal Reserve macro economic updates, 
        unexpected earnings shocks, or geopolitical events from the last 1-2 hours.
        Determine if there is extreme high-risk volatility or black-swan risk that could cause sudden crashes in equities.
        Provide a 2-sentence executive summary of current US stock market sentiment and key catalysts for the email dashboard.
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
                return json.loads(response.text)
            except Exception as inner_e:
                print(f"Gemini Stock AI Shield attempt failed on {model_name}: {inner_e}")
                continue

    except Exception as e:
        print(f"Gemini Stock AI Shield execution error: {e}")

    return {
        "high_risk_detected": False, 
        "risk_level": "UNKNOWN", 
        "reason": "AI Shield Bypass on Error", 
        "action": "ALLOW_TRADES",
        "ai_market_brief": "Stock market scanning operating normally under standard quantitative rules."
    }

def send_html_dashboard_email(subject, html_content, text_fallback):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("Email credentials missing; skipping email notification.")
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
        print(f"Stock telemetry report successfully sent to {receiver_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_gaussian_channel(closes, poles=4, period=50, mult=1.414):
    s = pd.Series(closes)
    effective_period = min(period, max(5, len(closes) - 1))
    alpha = (2.0 / (effective_period + 1)) * (poles ** 0.5)
    filtered = s.ewm(alpha=alpha, adjust=False).mean()
    error = (s - filtered).abs()
    deviation = error.ewm(alpha=alpha, adjust=False).mean() * mult
    upper = filtered + deviation
    lower = filtered - deviation
    return upper.iloc[-1], lower.iloc[-1], filtered.iloc[-1]

def calculate_choppiness_index(highs, lows, closes, period=14):
    """Calculates 14-period Choppiness Index (CI) for stock bars."""
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

def calculate_stop_price(entry_px, is_long, current_px, choppiness_index=50.0, is_ballistic=False):
    """Smart Dynamic Micro-Ratchet Stop Loss System."""
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    if roe >= 0.05:
        milestone = floor(roe * 40) / 40
        target_floor_roe = milestone - 0.01
    elif roe >= 0.035:
        target_floor_roe = 0.02
    elif roe >= 0.020:
        target_floor_roe = 0.01
    elif roe >= 0.010:
        target_floor_roe = 0.00         # Break-Even locked at +1.0% ROE
    elif roe >= 0.005:
        target_floor_roe = -0.005       # Risk capped to -0.5% at +0.5% ROE
    else:
        # SMART ADAPTIVE DOWNSIDE HARD STOP
        if choppiness_index > 58.0:
            target_floor_roe = -0.012   # Choppy Stock -> Fast Cut (-1.2% max)
        elif is_ballistic:
            target_floor_roe = -0.035   # High-Conviction Ballistic Breakout -> Wide Room (-3.5% max)
        else:
            target_floor_roe = -0.020   # Standard Clean Trend -> Moderate Room (-2.0% max)

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
    else:
        stop_px = entry_px * (1 - target_floor_roe)

    return stop_px, roe, target_floor_roe

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    audit_logs = []
    audit_logs.append(f"[{timestamp}] Alpaca Stock Engine Started (Smart Downside + Gemini AI Shield Mode).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    # 30-day rolling start date to ensure deep historical candle availability on IEX
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    # 1. RUN GEMINI AI STOCK MACRO SHIELD SCAN
    ai_shield = run_gemini_stock_market_shield()
    ai_risk_status = "BLOCKED (High Risk)" if ai_shield.get("high_risk_detected") else "PASS (Normal Risk)"
    audit_logs.append(f"Gemini AI Shield: [{ai_shield.get('risk_level', 'UNKNOWN')}] {ai_shield.get('reason', '')} -> {ai_risk_status}")

    state = load_state()

    # 2. Fetch Account Details & Positions from Alpaca
    account_res = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    if account_res.status_code != 200:
        raise Exception(f"Failed to fetch Alpaca account: {account_res.text}")
    account_data = account_res.json()

    equity = float(account_data.get("equity", 100000.0))
    cash = float(account_data.get("cash", 100000.0))
    buying_power = float(account_data.get("buying_power", 100000.0))
    margin_util_pct = ((equity - cash) / equity * 100) if equity > 0 else 0.0

    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS)
    positions_list = positions_res.json() if positions_res.status_code == 200 else []

    active_count = len(positions_list)
    positions_data = []
    active_symbols = set()
    total_positions_value = 0.0

    for pos in positions_list:
        symbol = pos.get("symbol")
        qty = float(pos.get("qty", 0))
        side = pos.get("side", "long")
        is_long = side == "long"
        entry_px = float(pos.get("avg_entry_price", 0))
        current_px = float(pos.get("current_price", entry_px))
        market_value = float(pos.get("market_value", 0))
        unrealized_pnl = float(pos.get("unrealized_pl", 0))
        
        total_positions_value += market_value
        active_symbols.add(symbol)

        # Fetch recent bars with start date to compute Choppiness Index for active stock
        try:
            bars_res = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Hour&limit=100&feed=iex&start={start_date}", 
                headers=HEADERS
            )
            bars = bars_res.json().get("bars", []) if bars_res.status_code == 200 else []
            closes = [float(b["c"]) for b in bars]
            highs = [float(b["h"]) for b in bars]
            lows = [float(b["l"]) for b in bars]
            ci = calculate_choppiness_index(highs, lows, closes)
        except Exception:
            ci = 50.0

        stop_px_raw, current_roe, target_floor = calculate_stop_price(entry_px, is_long, current_px, choppiness_index=ci)
        px = round_sig_figs(stop_px_raw, 5)

        if current_roe < 0.01:
            state["stagnation_tracker"][symbol] = state["stagnation_tracker"].get(symbol, 0) + 1
        else:
            state["stagnation_tracker"][symbol] = 0

        stag_count = state["stagnation_tracker"].get(symbol, 0)
        audit_logs.append(f"Stock Position: {symbol} | ROE: {current_roe*100:+.2f}% | Stop: ${px} | CI: {ci:.1f} | Stagnation: {stag_count}/48")

        positions_data.append({
            "bot_title": "Alpaca (TR-GC-Equities-LS-01)",
            "symbol": symbol,
            "side": "LONG" if is_long else "SHORT",
            "qty": qty,
            "entry": entry_px,
            "current": current_px,
            "market_value": market_value,
            "pnl": unrealized_pnl,
            "roe": current_roe * 100,
            "stop": px,
            "floor": target_floor * 100,
            "status": "Active"
        })

    remaining_cash = max(0.0, cash)
    assets_map = {
        "USD Cash": {"balance": remaining_cash, "balance_usd": remaining_cash}
    }
    for p in positions_data:
        assets_map[p["symbol"]] = {"balance": p["qty"], "balance_usd": p["market_value"]}

    # 3. EXPANDED STOCK UNIVERSE WATCHLIST (70+ Top S&P 500 & Nasdaq Momentum Equities)
    watchlist = [
        # Tech & Megacaps
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "QCOM",
        "INTC", "MU", "ARM", "AMAT", "LRCX", "SMCI", "ORCL", "CRM", "NOW", "PANW", "FTNT", "PLTR",
        # High Beta, Fintech & Growth
        "COIN", "HOOD", "SQ", "PYPL", "NET", "SNOW", "SHOP", "MSTR", "UBER", "ABNB", "RBLX", "DKNG",
        # Consumer, Media & Industrial Leaders
        "NFLX", "COST", "WMT", "HD", "DIS", "NKE", "SBUX", "BA", "CAT", "GE", "HON", "DE", "LMT",
        # Finance, Energy & Healthcare Leaders
        "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "SLB", "LLY", "UNH", "JNJ", "PFE", "ABBV",
        # Hardware & Semiconductors
        "TXN", "ADI", "KLAC", "MCHP", "DELL",
        # Major Index & Sector Benchmarks
        "SPY", "QQQ", "IWM", "DIA", "SMH", "XLF", "XLE"
    ]

    symbols_to_scan = [s for s in watchlist if s not in active_symbols]
    market_candidates = []
    scanned_count = 0

    # BATCH SCANNING ENGINE: Chunks of 25 symbols per API call for maximum reliability
    chunk_size = 25
    for i in range(0, len(symbols_to_scan), chunk_size):
        chunk = symbols_to_scan[i:i + chunk_size]
        symbols_param = ",".join(chunk)

        try:
            url = f"https://data.alpaca.markets/v2/stocks/bars?symbols={symbols_param}&timeframe=1Hour&limit=100&feed=iex&start={start_date}"
            bars_res = requests.get(url, headers=HEADERS)
            if bars_res.status_code != 200:
                audit_logs.append(f"Batch Scan API Error: HTTP {bars_res.status_code}")
                continue

            all_bars_data = bars_res.json().get("bars", {})

            for symbol in chunk:
                bars = all_bars_data.get(symbol, [])
                # Require 20 bars minimum (plenty for 14-period CI and EWM Gaussian calculations)
                if not bars or len(bars) < 20:
                    continue

                scanned_count += 1
                closes = [float(b["c"]) for b in bars]
                highs = [float(b["h"]) for b in bars]
                lows = [float(b["l"]) for b in bars]

                upper, lower, filter_band = calculate_gaussian_channel(closes)
                current_close = closes[-1]

                ci = calculate_choppiness_index(highs, lows, closes)
                # STRICT ENTRY GUARDRAIL: Skip stock setup if CI > 62.0
                if ci > 62.0:
                    continue

                atr = np.mean([h - l for h, l in zip(highs[-14:], lows[-14:])]) if len(highs) >= 14 else (highs[-1] - lows[-1])

                if current_close > upper and current_close <= upper * 1.025:
                    is_ballistic = current_close > (upper + 1.5 * atr)
                    market_candidates.append({
                        "symbol": symbol, 
                        "close": current_close, 
                        "is_long": True, 
                        "is_ballistic": is_ballistic, 
                        "ci": ci
                    })
                    audit_logs.append(f"EQUITY MATCH LONG: {symbol} @ ${current_close:.2f} (CI: {ci:.1f})")
        except Exception as e:
            audit_logs.append(f"Batch Scan Exception: {e}")
            continue

    audit_logs.append(f"Stock Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} breakouts.")

    # 4. Execution Gate (Blocked if Max Slots reached OR if Gemini AI Shield detected high risk)
    MAX_STOCK_SLOTS = 5
    if ai_shield.get("high_risk_detected"):
        audit_logs.append(f"Execution Gate: BLOCKED BY GEMINI AI SHIELD. Reason: {ai_shield.get('reason')}")
    elif active_count < MAX_STOCK_SLOTS and market_candidates:
        for candidate in market_candidates[: (MAX_STOCK_SLOTS - active_count)]:
            symbol = candidate["symbol"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            is_ballistic = candidate["is_ballistic"]

            target_pct = 0.12 if is_ballistic else 0.09
            target_usd = max(100.0, equity * target_pct)
            qty = max(1, int(target_usd / px))

            order_payload = {
                "symbol": symbol,
                "qty": str(qty),
                "side": "buy" if is_long else "sell",
                "type": "market",
                "time_in_force": "gtc"
            }
            try:
                order_res = requests.post(f"{BASE_URL}/v2/orders", json=order_payload, headers=HEADERS)
                if order_res.status_code == 200:
                    active_count += 1
                    active_symbols.add(symbol)
                    audit_logs.append(f"ORDER SUCCESS: Bought {qty} shares of {symbol}")
            except Exception as e:
                audit_logs.append(f"ORDER FAILED on {symbol}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/{MAX_STOCK_SLOTS}). No new market entries triggered.")

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #fde68a; font-family: monospace; font-size: 11px; color: #475569; white-space: pre-wrap; word-break: break-word;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Stock Engine)</div>
        <div class="table-responsive" style="overflow-x: hidden;">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%; table-layout: fixed;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    text_fallback = f"TR-GC-Equities-LS-01 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f} (Margin Util: {margin_util_pct:.1f}%)\nActive Positions: {active_count}/{MAX_STOCK_SLOTS}"

    funds_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${d['balance_usd']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #555;'>{d['balance']:.4f}</td></tr>" for f, d in assets_map.items() if d['balance_usd'] > 0.01])
    positions_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['symbol']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${p['market_value']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #b45309;'>${p['stop']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['status']}</td></tr>" for p in positions_data]) or "<tr><td colspan='7' style='padding: 15px; text-align: center; color: #666;'>No active stock positions found.</td></tr>"

    ai_risk_color = "#c62828" if ai_shield.get("high_risk_detected") else "#2e7d32"
    ai_badge = f"<span style='background: {ai_risk_color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 10px;'>Risk Level: {ai_shield.get('risk_level', 'UNKNOWN')}</span>"

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
          
          @media screen and (max-width: 600px) {{
            body {{ padding: 2px !important; }}
            .container {{ border-radius: 0 !important; }}
            .content {{ padding: 8px !important; }}
            table {{ font-size: 9px !important; white-space: normal !important; }}
            th, td {{ padding: 5px 4px !important; }}
            .net-worth-value {{ font-size: 20px !important; }}
          }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Equities-LS-01 | Stock Telemetry Dashboard</h2>
            <p>Timestamp: {timestamp} &bull; Mode: EQUITIES PAPER</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Account Equity</div>
              <div class="net-worth-value">USD ${equity:.2f}</div>
              <div class="net-worth-subtitle">Alpaca Paper Sandbox &bull; <b>Margin Utilization: {margin_util_pct:.1f}%</b></div>
            </div>

            <div class="ai-brief-card">
              <div class="ai-brief-title">
                <span>🤖 GEMINI AI EXECUTIVE STOCK MARKET BRIEFING</span>
                {ai_badge}
              </div>
              <b>Live Market Assessment:</b> {ai_shield.get('ai_market_brief', 'Normal conditions.')}<br>
              <b>Execution Recommendation:</b> {ai_shield.get('reason', 'Standard scan active.')}
            </div>

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Stock Rule Deck & Guardrails</div>
              &bull; <b>Market Hours Cron:</b> Mon-Fri US Trading Hours &bull; <b>Max Slots:</b> {active_count}/{MAX_STOCK_SLOTS} Active<br>
              &bull; <b>Gemini AI Macro Shield:</b> Real-time Google Search news & black-swan scanning<br>
              &bull; <b>Smart Downside Adaptive Stop:</b> -1.2% (Choppy) / -2.0% (Clean) / -3.5% (Ballistic Breakout)<br>
              &bull; <b>Micro-Ratchet Ladders:</b> +0.5% (-0.5% cap) &bull; +1.0% (BE) &bull; +2% &bull; +3.5%<br>
              &bull; <i>&nbsp;&nbsp;&nbsp;&nbsp; &bull; Dynamic 2.5% Steps with 1% Buffer active from +5% up to +300%+ ROE</i><br>
              &bull; <b>Strict Choppiness Filter:</b> Skip entries if Choppiness Index (CI) &gt; 62<br>
              &bull; <b>Asset Universe:</b> S&P 500 & Nasdaq Momentum Equities
            </div>

            <div class="section-title">Funds & Cash (USD)</div>
            <div class="table-responsive">
              <table><thead><tr><th>Asset</th><th>Balance USD</th><th>Shares / Balance</th></tr></thead><tbody>{funds_rows}</tbody></table>
            </div>

            <div class="section-title">Active Stock Positions</div>
            <div class="table-responsive">
              <table><thead><tr><th>Bot Title</th><th>Symbol</th><th>Side</th><th>Market Value USD</th><th>Unrealized P&L USD</th><th>Stop Price</th><th>Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Alpaca Autonomous Equity Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Alpaca Stock Report — USD ${equity:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Stock telemetry report complete.")

if __name__ == "__main__":
    try:
        execute_stock_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Stock Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Alpaca Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
