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
        print(f"SIGNUM-style HTML dashboard email successfully sent to {receiver_email}")
    except Exception as e:
        print(f"Failed to send email: {e}")

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_gaussian_channel(closes, poles=4, period=323, mult=1.414):
    """Calculates Ehlers N-Pole Gaussian Channel upper and filter bands."""
    s = pd.Series(closes)
    alpha = (2.0 / (period + 1)) * (poles ** 0.5)
    filtered = s.ewm(alpha=alpha, adjust=False).mean()
    error = (s - filtered).abs()
    deviation = error.ewm(alpha=alpha, adjust=False).mean() * mult
    upper = filtered + deviation
    filter_band = filtered
    return upper.iloc[-1], filter_band.iloc[-1]

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

def check_btc_daily_candle(info):
    """Checks if BTC daily candle is Green (True) or Red (False)."""
    try:
        candles = info.candles_snapshot(name="BTC", interval="1d", startTime=int(time.time()*1000) - 86400000*3)
        if candles and len(candles) > 0:
            latest = candles[-1]
            o = float(latest.get("o", 0))
            c = float(latest.get("c", 0))
            return c >= o
    except Exception as e:
        print(f"Error checking BTC daily candle: {e}")
    return True

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print("\n" + "="*60)
    print(f"[{timestamp}] Executing TR-GC-Crypto-LS-23 Full Master Engine...")

    if not SECRET_KEY or not ACCOUNT_ADDRESS:
        raise ValueError("Missing HL_SECRET_KEY or HL_ACCOUNT_ADDRESS environment variables.")

    state = load_state()
    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    user_state = info.user_state(ACCOUNT_ADDRESS)
    spot_state = info.spot_user_state(ACCOUNT_ADDRESS)
    open_orders = info.frontend_open_orders(ACCOUNT_ADDRESS)
    all_mids = info.all_mids()
    meta = info.meta()

    # 1. Process Spot Balances & Valuations (Funds USD)
    assets_map = {}
    for b in spot_state.get("balances", []):
        coin = b.get("coin")
        total_bal = float(b.get("total", 0))
        if total_bal <= 0:
            continue
        price = 1.0 if coin == "USDC" else float(all_mids.get(coin, 0))
        val_usd = total_bal * price
        if coin not in assets_map:
            assets_map[coin] = {"balance": 0.0, "balance_usd": 0.0}
        assets_map[coin]["balance"] += total_bal
        assets_map[coin]["balance_usd"] += val_usd

    # 2. Process Perpetual Positions & Stops
    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []
    active_coins = set()

    if asset_positions:
        for pos_item in asset_positions:
            pos = pos_item.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0))
            if not coin or szi == 0:
                continue

            active_count += 1
            active_coins.add(coin)
            is_long = szi > 0
            sz = abs(szi)
            entry_px = float(pos.get("entryPx", 0))
            current_px = float(all_mids.get(coin, entry_px))
            margin_used = float(pos.get("marginUsed", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))
            position_equity = margin_used + unrealized_pnl

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

            if coin not in assets_map:
                assets_map[coin] = {"balance": 0.0, "balance_usd": 0.0}
            assets_map[coin]["balance"] += sz
            assets_map[coin]["balance_usd"] += position_equity

            positions_data.append({
                "bot_title": "Hyperliquid (TR-GC-Crypto-LS-23)",
                "coin": coin,
                "side": "LONG" if is_long else "SHORT",
                "sz": sz,
                "entry": entry_px,
                "current": current_px,
                "leverage": int(leverage),
                "collateral": margin_used,
                "pnl": unrealized_pnl,
                "roe": current_roe * 100,
                "stop": px,
                "floor": target_floor * 100,
                "status": "Active"
            })

    # 3. Autonomous Top 100 Scanner & Entry Logic
    btc_green = check_btc_daily_candle(info)
    print(f"BTC Daily Candle Status: {'GREEN (LONGs Allowed)' if btc_green else 'RED (LONGs Blocked)'}")

    universe = [asset["name"] for asset in meta.get("universe", [])]
    market_data = []
    for coin in universe:
        if coin in active_coins or coin in ["USDC", "USDT"]:
            continue
        try:
            px = float(all_mids.get(coin, 0))
            if px <= 0:
                continue
            candles = info.candles_snapshot(name=coin, interval="1h", startTime=int(time.time()*1000) - 86400000*7)
            if not candles or len(candles) < 50:
                continue
            closes = [float(c["c"]) for c in candles]
            upper, filter_band = calculate_gaussian_channel(closes)
            current_close = closes[-1]
            
            if current_close > upper and current_close <= upper * 1.025:
                highs = [float(c["h"]) for c in candles[-14:]]
                lows = [float(c["l"]) for c in candles[-14:]]
                atr = np.mean([h - l for h, l in zip(highs, lows)])
                is_ballistic = current_close > (upper + 1.5 * atr)
                market_data.append({
                    "coin": coin,
                    "close": current_close,
                    "upper": upper,
                    "is_ballistic": is_ballistic
                })
        except Exception:
            continue

    if active_count < 6 and btc_green and market_data:
        for candidate in market_data[: (6 - active_count)]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_ballistic = candidate["is_ballistic"]
            
            margin_summary = user_state.get("marginSummary", {})
            total_nav = float(margin_summary.get("accountValue", 100.0))
            target_pct = 0.12 if is_ballistic else 0.09
            target_usd = max(45.0, total_nav * target_pct)
            sz = round(target_usd / px, 4)
            
            print(f"Executing Autonomous LONG Entry on {coin} at ${px:.4f} (Size: {sz}, Ballistic: {is_ballistic})")
            try:
                res = exchange.market_open(coin, True, sz, px * 1.01)
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
            except Exception as e:
                print(f"Failed to open position on {coin}: {e}")

    # 4. 6/6 Rotation Check
    if active_count == 6:
        unprotected_trades = [p for p in positions_data if p["roe"] < 1.5]
        if unprotected_trades:
            stagnant_trade = max(unprotected_trades, key=lambda p: state["stagnation_tracker"].get(p["coin"], 0))
            coin_to_rotate = stagnant_trade["coin"]
            if state["stagnation_tracker"].get(coin_to_rotate, 0) >= 2:
                print(f"Rotating stagnant trade {coin_to_rotate}")
                try:
                    exchange.market_close(coin_to_rotate)
                    state["stagnation_tracker"][coin_to_rotate] = 0
                    active_count -= 1
                except Exception as e:
                    print(f"Failed to close stagnant trade {coin_to_rotate}: {e}")

    save_state(state)

    # Calculate Total Net Worth precisely
    total_nav = 0.0
    funds_list = []
    for coin, data in assets_map.items():
        if data["balance_usd"] > 0.01:
            funds_list.append({
                "asset": coin,
                "balance": data["balance"],
                "balance_usd": data["balance_usd"]
            })
            total_nav += data["balance_usd"]

    funds_list.sort(key=lambda x: x["balance_usd"], reverse=True)

    text_fallback = f"TR-GC-Crypto-LS-23 | Bot #25900 Full Engine\nTimestamp: {timestamp}\nTotal Net Worth: USD ${total_nav:.2f}\nActive Positions: {active_count}/6"

    funds_rows = ""
    for f in funds_list:
        funds_rows += f"""
        <tr>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;">{f['asset']}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee;">${f['balance_usd']:.2f}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; color: #555;">{f['balance']:.4f}</td>
        </tr>
        """

    spot_bot_rows = ""
    for f in funds_list:
        if f['asset'] == 'USDC':
            spot_bot_rows += f"""
            <tr>
                <td style="padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: 500;">Hyperliquid (TR-GC-Crypto-LS-23)</td>
                <td style="padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;">{f['asset']}</td>
                <td style="padding: 9px 10px; border-bottom: 1px solid #eee;">${f['balance_usd']:.2f}</td>
                <td style="padding: 9px 10px; border-bottom: 1px solid #eee;">{f['balance']:.4f}</td>
                <td style="padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;">Active</td>
            </tr>
            """

    positions_rows = ""
    for p in positions_data:
        pnl_color = "#2e7d32" if p["pnl"] >= 0 else "#c62828"
        positions_rows += f"""
        <tr>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: 500;">{p['bot_title']}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;">{p['coin']}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee;">{p['leverage']}x</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;">{p['side']}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee;">${p['collateral']:.2f}</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; color: {pnl_color}; font-weight: bold;">${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td>
            <td style="padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;">{p['status']}</td>
        </tr>
        """

    if not positions_rows:
        positions_rows = "<tr><td colspan='7' style='padding: 15px; text-align: center; color: #666;'>No active positions found.</td></tr>"

    html_content = f"""
    <html>
      <head>
        <style>
          body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 20px; color: #333; }}
          .container {{ max-width: 750px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
          .header {{ background: #0f172a; color: #ffffff; padding: 20px 25px; }}
          .header h2 {{ margin: 0; font-size: 18px; font-weight: 600; }}
          .header p {{ margin: 5px 0 0; font-size: 12px; color: #94a3b8; }}
          .content {{ padding: 25px; }}
          .net-worth-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 20px; margin-bottom: 25px; }}
          .net-worth-title {{ font-size: 13px; text-transform: uppercase; color: #64748b; font-weight: 600; margin-bottom: 8px; }}
          .net-worth-value {{ font-size: 28px; font-weight: 700; color: #0f172a; }}
          .net-worth-subtitle {{ font-size: 12px; color: #64748b; margin-top: 5px; }}
          .section-title {{ font-size: 14px; text-transform: uppercase; color: #475569; margin: 25px 0 10px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 5px; font-weight: 600; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 15px; }}
          th {{ background: #f1f5f9; color: #475569; text-align: left; padding: 9px 10px; font-weight: 600; border-bottom: 2px solid #cbd5e1; }}
          .footer {{ text-align: center; font-size: 11px; color: #94a3b8; padding: 15px; background: #f8fafc; border-top: 1px solid #e2e8f0; }}
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Crypto-LS-23 | Bot #25900 Full Engine</h2>
            <p>Automated Run Timestamp: {timestamp} &bull; BTC Trend: {'GREEN' if btc_green else 'RED'}</p>
          </div>
          <div class="content">
            
            <div class="net-worth-card">
              <div class="net-worth-title">Total Net Worth</div>
              <div class="net-worth-value">USD ${total_nav:.2f}</div>
              <div class="net-worth-subtitle">Based on current Spot Assets & Positions (incl. unrealized P&L).</div>
            </div>

            <div class="section-title">Funds (USD)</div>
            <table>
              <thead>
                <tr>
                  <th>Asset</th>
                  <th>Balance USD</th>
                  <th>Balance</th>
                </tr>
              </thead>
              <tbody>
                {funds_rows}
              </tbody>
            </table>

            <div class="section-title">Spot Assets per Bot (USD)</div>
            <table>
              <thead>
                <tr>
                  <th>Bot Title</th>
                  <th>Asset</th>
                  <th>Balance USD</th>
                  <th>Balance</th>
                  <th>Bot Status</th>
                </tr>
              </thead>
              <tbody>
                {spot_bot_rows}
              </tbody>
            </table>

            <div class="section-title">Positions per Bot (USD)</div>
            <table>
              <thead>
                <tr>
                  <th>Bot Title</th>
                  <th>Asset</th>
                  <th>Leverage</th>
                  <th>Side</th>
                  <th>Collateral USD</th>
                  <th>Unrealized P&L USD</th>
                  <th>Bot Status</th>
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

    send_html_dashboard_email(f"Hyperliquid Full Engine Report — USD ${total_nav:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Full engine scan and management complete. Email dispatched.")

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Full engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
