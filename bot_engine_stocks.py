import os
import json
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import log10, floor
import requests
import pandas as pd
import numpy as np

API_KEY = os.getenv("APAL_API_KEY_ID")
SECRET_KEY = os.getenv("APAL_SECRET_KEY")
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
    alpha = (2.0 / (period + 1)) * (poles ** 0.5)
    filtered = s.ewm(alpha=alpha, adjust=False).mean()
    error = (s - filtered).abs()
    deviation = error.ewm(alpha=alpha, adjust=False).mean() * mult
    upper = filtered + deviation
    lower = filtered - deviation
    return upper.iloc[-1], lower.iloc[-1], filtered.iloc[-1]

def calculate_stop_price(entry_px, is_long, current_px):
    if is_long:
        roe = (current_px - entry_px) / entry_px
    else:
        roe = (entry_px - current_px) / entry_px

    if roe >= 0.30:
        target_floor_roe = 0.22
    elif roe >= 0.20:
        target_floor_roe = 0.12
    elif roe >= 0.10:
        target_floor_roe = 0.05
    elif roe >= 0.035:
        target_floor_roe = 0.02
    elif roe >= 0.020:
        target_floor_roe = 0.01
    elif roe >= 0.015:
        target_floor_roe = 0.00
    else:
        target_floor_roe = -0.04

    if is_long:
        stop_px = entry_px * (1 + target_floor_roe)
    else:
        stop_px = entry_px * (1 - target_floor_roe)

    return stop_px, roe, target_floor_roe

def execute_stock_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    audit_logs = []
    audit_logs.append(f"[{timestamp}] Alpaca Stock Engine Started (Equities LS-01 Mode).")

    if not API_KEY or not SECRET_KEY:
        raise ValueError("Missing APAL_API_KEY_ID or APAL_SECRET_KEY environment variables.")

    state = load_state()

    # 1. Fetch Account Details & Positions from Alpaca
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

        stop_px_raw, current_roe, target_floor = calculate_stop_price(entry_px, is_long, current_px)
        px = round_sig_figs(stop_px_raw, 5)

        if current_roe < 0.01:
            state["stagnation_tracker"][symbol] = state["stagnation_tracker"].get(symbol, 0) + 1
        else:
            state["stagnation_tracker"][symbol] = 0

        stag_count = state["stagnation_tracker"].get(symbol, 0)
        audit_logs.append(f"Stock Position: {symbol} | ROE: {current_roe*100:+.2f}% | Stop: ${px} | Stagnation: {stag_count}/48")

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

    # 2. Stock Universe Scan (High-Liquidity Equities)
    watchlist = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "PLTR", "COIN", "SPY", "QQQ"]
    market_candidates = []
    scanned_count = 0

    for symbol in watchlist:
        if symbol in active_symbols:
            continue
        try:
            bars_res = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1H&limit=100",
                headers=HEADERS
            )
            if bars_res.status_code != 200:
                continue
            bars = bars_res.json().get("bars", [])
            if not bars or len(bars) < 50:
                continue
            scanned_count += 1
            closes = [float(b["c"]) for b in bars]
            upper, lower, filter_band = calculate_gaussian_channel(closes)
            current_close = closes[-1]

            highs = [float(b["h"]) for b in bars[-14:]]
            lows = [float(b["l"]) for b in bars[-14:]]
            atr = np.mean([h - l for h, l in zip(highs, lows)])

            if current_close > upper and current_close <= upper * 1.025:
                is_ballistic = current_close > (upper + 1.5 * atr)
                market_candidates.append({"symbol": symbol, "close": current_close, "is_long": True, "is_ballistic": is_ballistic})
                audit_logs.append(f"EQUITY MATCH LONG: {symbol} @ ${current_close:.2f}")
        except Exception as e:
            continue

    audit_logs.append(f"Stock Scan Complete: Evaluated {scanned_count} symbols. Found {len(market_candidates)} breakouts.")

    # 3. Execution Gate (Max 5 concurrent positions for stocks)
    MAX_STOCK_SLOTS = 5
    if active_count < MAX_STOCK_SLOTS and market_candidates:
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

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 11px; color: #475569;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Stock Engine)</div>
        <div class="table-responsive">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    text_fallback = f"TR-GC-Equities-LS-01 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Equity: USD ${equity:.2f} (Margin Util: {margin_util_pct:.1f}%)\nActive Positions: {active_count}/{MAX_STOCK_SLOTS}"

    funds_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${d['balance_usd']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #555;'>{d['balance']:.4f}</td></tr>" for f, d in assets_map.items() if d['balance_usd'] > 0.01])
    positions_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['symbol']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${p['market_value']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #b45309;'>${p['stop']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['status']}</td></tr>" for p in positions_data]) or "<tr><td colspan='7' style='padding: 15px; text-align: center; color: #666;'>No active stock positions found.</td></tr>"

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
          .rules-card {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 12px 15px; margin-bottom: 20px; font-size: 11px; color: #166534; }}
          .rules-title {{ font-weight: 700; text-transform: uppercase; margin-bottom: 6px; font-size: 12px; color: #15803d; }}
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

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Stock Rule Deck & Guardrails</div>
              &bull; <b>Market Hours Cron:</b> Mon-Fri US Trading Hours &bull; <b>Max Slots:</b> 5 Active<br>
              &bull; <b>Hard Stop:</b> -4.0% ROE Floor<br>
              &bull; <b>Profit Ratchet Ladders:</b> +1.5% ROE (BE) &bull; +2.0% (Tier 1) &bull; +3.5% (Tier 2) &bull; +10% (+5% Floor)<br>
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
