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
STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading state.json: {e}")
    return {"cooldown_blocklist": {}, "stagnation_tracker": {}, "last_run_timestamp": ""}

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
        print(f"Dashboard HTML email successfully sent to {receiver_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_stop_price(entry_px, is_long, current_px, leverage=1.0):
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

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
        stop_px = entry_px * (1 + (target_floor_roe / leverage))
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))

    return stop_px, roe, target_floor_roe

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print("\n" + "="*60)
    print(f"[{timestamp}] Executing TR-GC-Crypto-LS-23 Master Engine...")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()
    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    user_state = info.user_state(ACCOUNT_ADDRESS)
    open_orders = info.frontend_open_orders(ACCOUNT_ADDRESS)
    all_mids = info.all_mids()

    margin_summary = user_state.get("marginSummary", {})
    total_nav = float(margin_summary.get("accountValue", 0))
    total_margin_used = float(margin_summary.get("totalMarginUsed", 0))
    free_usdc = total_nav - total_margin_used

    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []

    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if not coin or szi == 0:
                continue

            active_count += 1
            is_long = szi > 0
            sz = abs(szi)
            entry_px = float(pos.get("entryPx", 0))
            current_px = float(all_mids.get(coin, entry_px))
            margin_used = float(pos.get("marginUsed", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))

            leverage_info = pos.get("leverage", {})
            leverage = float(leverage_info.get("value", 1.0)) if isinstance(leverage_info, dict) else 1.0
            if leverage <= 0:
                leverage = 1.0

            stop_px_raw, current_roe, target_floor = calculate_stop_price(entry_px, is_long, current_px, leverage)
            px = round_sig_figs(stop_px_raw, 5)
            is_buy_order = not is_long

            if current_roe < 0.01:
                state["stagnation_tracker"][coin] = state["stagnation_tracker"].get(coin, 0) + 1
            else:
                state["stagnation_tracker"][coin] = 0

            for order in open_orders:
                if order.get("coin") == coin and order.get("isTrigger"):
                    exchange.cancel(coin, order["oid"])

            res = exchange.order(
                coin,
                is_buy_order,
                sz,
                px,
                {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True
            )

            positions_data.append({
                "coin": coin,
                "side": "LONG" if is_long else "SHORT",
                "sz": sz,
                "entry": entry_px,
                "current": current_px,
                "leverage": int(leverage),
                "margin": margin_used,
                "pnl": unrealized_pnl,
                "roe": current_roe * 100,
                "stop": px,
                "floor": target_floor * 100,
                "status": res.get("status")
            })

    save_state(state)

    # Build Plain Text Fallback
    text_fallback = f"TR-GC-Crypto-LS-23 | Bot #25900 Routine Run\nTimestamp: {timestamp}\nTotal NAV: ${total_nav:.2f} | Free USDC: ${free_usdc:.2f}\nActive Positions: {active_count}/6"

    # Build Rich HTML Dashboard Email
    positions_rows = ""
    for p in positions_data:
        pnl_color = "#2e7d32" if p["pnl"] >= 0 else "#c62828"
        positions_rows += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #eee; font-weight: bold;">{p['coin']}</td>
            <td style="padding: 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'};">{p['side']} ({p['leverage']}x)</td>
            <td style="padding: 10px; border-bottom: 1px solid #eee;">${p['margin']:.2f}</td>
            <td style="padding: 10px; border-bottom: 1px solid #eee; color: {pnl_color}; font-weight: bold;">${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td>
            <td style="padding: 10px; border-bottom: 1px solid #eee;">{p['stop']} ({p['floor']:+.1f}% floor)</td>
        </tr>
        """

    if not positions_rows:
        positions_rows = "<tr><td colspan='5' style='padding: 15px; text-align: center; color: #666;'>No active positions found.</td></tr>"

    html_content = f"""
    <html>
      <head>
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 20px; color: #333; }}
          .container {{ max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
          .header {{ background: #0f172a; color: #ffffff; padding: 20px 25px; }}
          .header h2 {{ margin: 0; font-size: 18px; font-weight: 600; }}
          .header p {{ margin: 5px 0 0; font-size: 12px; color: #94a3b8; }}
          .content {{ padding: 25px; }}
          .card-grid {{ display: flex; gap: 15px; margin-bottom: 25px; }}
          .card {{ flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 15px; text-align: center; }}
          .card-title {{ font-size: 12px; text-transform: uppercase; color: #64748b; font-weight: 600; margin-bottom: 5px; }}
          .card-value {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
          h3 {{ font-size: 14px; text-transform: uppercase; color: #475569; margin-bottom: 10px; border-bottom: 2px solid #e2e8f0; padding-bottom: 5px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 20px; }}
          th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 10px; font-weight: 600; border-bottom: 2px solid #cbd5e1; }}
          .footer {{ text-align: center; font-size: 11px; color: #94a3b8; padding: 15px; background: #f8fafc; border-top: 1px solid #e2e8f0; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Crypto-LS-23 | Bot #25900 Dashboard</h2>
            <p>Automated Run Timestamp: {timestamp}</p>
          </div>
          <div class="content">
            <div class="card-grid">
              <div class="card">
                <div class="card-title">Total Net Worth (NAV)</div>
                <div class="card-value">${total_nav:.2f}</div>
              </div>
              <div class="card">
                <div class="card-title">Free USDC Cash</div>
                <div class="card-value">${free_usdc:.2f}</div>
              </div>
            </div>

            <h3>Active Positions ({active_count}/6 Slots Used)</h3>
            <table>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th>Side</th>
                  <th>Collateral</th>
                  <th>Unrealized P&L</th>
                  <th>Stop & Floor</th>
                </tr>
              </thead>
              <tbody>
                {positions_rows}
              </tbody>
            </table>
          </div>
          <div class="footer">
            Hyperliquid Autonomous Engine &bull; Managed via GitHub Actions
          </div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Hyperliquid Dashboard Report — ${total_nav:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Engine run complete. HTML dashboard email dispatched.")

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
