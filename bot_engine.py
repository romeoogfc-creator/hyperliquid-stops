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

VERBOSE_TEST_MODE = True

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
        print(f"Telemetry report successfully sent to {receiver_email}")
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
        is_buy_order = False
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))
        is_buy_order = True

    return stop_px, roe, target_floor_roe, is_buy_order

def check_btc_daily_candle(info):
    try:
        now_ms = int(time.time() * 1000)
        candles = info.candles_snapshot(name="BTC", interval="1d", startTime=now_ms - 86400000 * 5, endTime=now_ms)
        if candles and len(candles) > 0:
            latest = candles[-1]
            o = float(latest.get("o", 0))
            c = float(latest.get("c", 0))
            return c >= o, o, c
    except Exception as e:
        print(f"Error checking BTC daily candle: {e}")
    return True, 0.0, 0.0

def execute_engine():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    audit_logs = []
    audit_logs.append(f"[{timestamp}] Test Telemetry Engine Started (Institutional Grade Mode).")

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

    # Fetch institutional asset contexts for live funding rates
    funding_map = {}
    try:
        meta_and_contexts = info.meta_and_asset_ctxs()
        universe_meta = meta_and_contexts[0]["universe"]
        asset_ctxs = meta_and_contexts[1]
        for u, ctx in zip(universe_meta, asset_ctxs):
            c_name = u["name"]
            f_rate = float(ctx.get("funding", 0.0)) * 100  # Hourly funding percentage
            funding_map[c_name] = f_rate
    except Exception as e:
        audit_logs.append(f"Warning: Could not fetch asset contexts for funding: {e}")

    margin_summary = user_state.get("marginSummary", {})
    account_value = float(margin_summary.get("accountValue", 500.0))
    total_margin_used = float(margin_summary.get("totalMarginUsed", 0.0))
    margin_util_pct = (total_margin_used / account_value * 100) if account_value > 0 else 0.0

    spot_usdc = 0.0
    for b in spot_state.get("balances", []):
        if b.get("coin") == "USDC":
            spot_usdc = float(b.get("total", 0))

    total_nav = spot_usdc if spot_usdc > 0 else account_value

    asset_positions = user_state.get("assetPositions", [])
    active_count = 0
    positions_data = []
    active_coins = set()
    total_positions_value = 0.0

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
            pos_equity = margin_used + unrealized_pnl
            total_positions_value += pos_equity

            leverage_info = pos.get("leverage", {})
            leverage = float(leverage_info.get("value", 1.0)) if isinstance(leverage_info, dict) else 1.0
            if leverage <= 0:
                leverage = 1.0

            stop_px_raw, current_roe, target_floor, is_buy_order = calculate_stop_price(entry_px, is_long, current_px, leverage)
            px = round_sig_figs(stop_px_raw, 5)
            funding_val = funding_map.get(coin, 0.0)

            if current_roe < 0.01:
                state["stagnation_tracker"][coin] = state["stagnation_tracker"].get(coin, 0) + 1
            else:
                state["stagnation_tracker"][coin] = 0

            stag_count = state["stagnation_tracker"].get(coin, 0)
            stag_hours = (stag_count * 30) / 60
            audit_logs.append(f"Position: {coin} | ROE: {current_roe*100:+.2f}% | Stop: ${px} | Funding: {funding_val:+.4f}%/h | Stagnation: {stag_count}/48 ({stag_hours:.1f}h)")

            for order in open_orders:
                if order.get("coin") == coin and order.get("isTrigger"):
                    exchange.cancel(coin, order["oid"])

            exchange.order(
                coin,
                is_buy_order,
                sz,
                px,
                {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True
            )

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
                "funding": funding_val,
                "stop": px,
                "floor": target_floor * 100,
                "status": "Active"
            })

    remaining_usdc = max(0.0, total_nav - total_positions_value)
    assets_map = {
        "USDC": {"balance": remaining_usdc, "balance_usd": remaining_usdc}
    }
    for p in positions_data:
        coin = p["coin"]
        pos_val = p["collateral"] + p["pnl"]
        assets_map[coin] = {"balance": p["sz"], "balance_usd": pos_val}

    btc_green, btc_open, btc_close = check_btc_daily_candle(info)
    regime_str = f"GREEN (Open: ${btc_open:.2f}, Close: ${btc_close:.2f}) -> LONGs Allowed" if btc_green else f"RED (Open: ${btc_open:.2f}, Close: ${btc_close:.2f}) -> SHORTs Allowed"
    audit_logs.append(f"BTC Regime Check: {regime_str}")

    universe = [asset["name"] for asset in meta.get("universe", [])][:100]
    market_candidates = []
    scanned_count = 0
    now_ms = int(time.time() * 1000)

    for coin in universe:
        if coin in active_coins or coin in ["USDC", "USDT"]:
            continue
        try:
            px = float(all_mids.get(coin, 0))
            if px <= 0:
                continue
            candles = info.candles_snapshot(name=coin, interval="1h", startTime=now_ms - 86400000 * 7, endTime=now_ms)
            if not candles or len(candles) < 50:
                continue
            scanned_count += 1
            closes = [float(c["c"]) for c in candles]
            upper, lower, filter_band = calculate_gaussian_channel(closes)
            current_close = closes[-1]
            
            highs = [float(c["h"]) for c in candles[-14:]]
            lows = [float(c["l"]) for c in candles[-14:]]
            atr = np.mean([h - l for h, l in zip(highs, lows)])

            if btc_green and current_close > upper and current_close <= upper * 1.025:
                is_ballistic = current_close > (upper + 1.5 * atr)
                market_candidates.append({"coin": coin, "close": current_close, "is_long": True, "is_ballistic": is_ballistic})
                audit_logs.append(f"MATCH LONG: {coin} @ ${current_close:.4f}")
            elif not btc_green and current_close < lower and current_close >= lower * 0.975:
                is_ballistic = current_close < (lower - 1.5 * atr)
                market_candidates.append({"coin": coin, "close": current_close, "is_long": False, "is_ballistic": is_ballistic})
                audit_logs.append(f"MATCH SHORT: {coin} @ ${current_close:.4f}")
        except Exception:
            continue

    audit_logs.append(f"Scan Complete: Evaluated {scanned_count} assets. Found {len(market_candidates)} breakouts.")

    if active_count < 6 and market_candidates:
        for candidate in market_candidates[: (6 - active_count)]:
            coin = candidate["coin"]
            px = candidate["close"]
            is_long = candidate["is_long"]
            is_ballistic = candidate["is_ballistic"]
            
            target_pct = 0.12 if is_ballistic else 0.09
            target_usd = max(45.0, total_nav * target_pct)
            sz = round(target_usd / px, 4)
            
            side_str = "LONG" if is_long else "SHORT"
            try:
                if is_long:
                    res = exchange.market_open(coin, True, sz, px * 1.01)
                else:
                    res = exchange.market_open(coin, False, sz, px * 0.99)
                    
                if res.get("status") == "ok":
                    active_count += 1
                    active_coins.add(coin)
                    audit_logs.append(f"EXECUTION SUCCESS: Opened {side_str} on {coin}")
            except Exception as e:
                audit_logs.append(f"EXECUTION FAILED on {coin}: {e}")
    else:
        audit_logs.append(f"Execution Gate: Active slots ({active_count}/6). No new market entries triggered.")

    if active_count == 6:
        unprotected_trades = [p for p in positions_data if p["roe"] < 1.5]
        if unprotected_trades:
            stagnant_trade = max(unprotected_trades, key=lambda p: state["stagnation_tracker"].get(p["coin"], 0))
            coin_to_rotate = stagnant_trade["coin"]
            if state["stagnation_tracker"].get(coin_to_rotate, 0) >= 48:
                try:
                    exchange.market_close(coin_to_rotate)
                    state["stagnation_tracker"][coin_to_rotate] = 0
                    active_count -= 1
                    audit_logs.append(f"ROTATION TRIGGERED: Closed stagnant trade {coin_to_rotate} after full 24h stagnation.")
                except Exception as e:
                    audit_logs.append(f"Rotation Failed on {coin_to_rotate}: {e}")

    save_state(state)

    audit_section = ""
    if VERBOSE_TEST_MODE:
        audit_rows = "".join([f"<tr><td style='padding: 6px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-size: 11px; color: #475569;'>{log}</td></tr>" for log in audit_logs])
        audit_section = f"""
        <div class="section-title" style="color: #d97706;">Live Test Telemetry & Audit Log (Institutional Grade)</div>
        <div class="table-responsive">
          <table style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; width: 100%;">
            <tbody>{audit_rows}</tbody>
          </table>
        </div>
        """

    text_fallback = f"TR-GC-Crypto-LS-23 | Telemetry Dashboard\nTimestamp: {timestamp}\nTotal Net Worth: USD ${total_nav:.2f} (Margin Util: {margin_util_pct:.1f}%)\nActive Positions: {active_count}/6"

    funds_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${d['balance_usd']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #555;'>{d['balance']:.4f}</td></tr>" for f, d in assets_map.items() if d['balance_usd'] > 0.01])
    positions_rows = "".join([f"<tr><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['bot_title']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-weight: bold;'>{p['coin']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>{p['leverage']}x</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['side'] == 'LONG' else '#c62828'}; font-weight: 600;'>{p['side']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee;'>${p['collateral']:.2f}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['pnl'] >= 0 else '#c62828'}; font-weight: bold;'>${p['pnl']:+.2f} ({p['roe']:+.2f}%)</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: {'#2e7d32' if p['funding'] <= 0 else '#c62828'}; font-family: monospace;'>{p['funding']:+.4f}%</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; font-family: monospace; font-weight: bold; color: #b45309;'>${p['stop']}</td><td style='padding: 9px 10px; border-bottom: 1px solid #eee; color: #2e7d32; font-weight: 600;'>{p['status']}</td></tr>" for p in positions_data]) or "<tr><td colspan='9' style='padding: 15px; text-align: center; color: #666;'>No active positions found.</td></tr>"

    mode_label = 'DEBUG / INSTITUTIONAL GRADE' if VERBOSE_TEST_MODE else 'PRODUCTION'

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
        </style>
      </head>
      <body>
        <div class="container">
          <div class="header">
            <h2>TR-GC-Crypto-LS-23 | Telemetry Dashboard</h2>
            <p>Timestamp: {timestamp} &bull; Mode: {mode_label}</p>
          </div>
          <div class="content">
            <div class="net-worth-card">
              <div class="net-worth-title">Total Net Worth</div>
              <div class="net-worth-value">USD ${total_nav:.2f}</div>
              <div class="net-worth-subtitle">Based on current Spot Assets & Positions &bull; <b>Margin Utilization: {margin_util_pct:.1f}%</b></div>
            </div>

            <div class="rules-card">
              <div class="rules-title">&#9989; Active Bot Rule Deck & Guardrails</div>
              &bull; <b>Execution Engine:</b> 30-Min GitHub Cron &bull; <b>Max Slots:</b> 6/6 Active<br>
              &bull; <b>Hard Stop:</b> -4.0% ROE (Native Hyperliquid 24/7 On-Chain Order)<br>
              &bull; <b>Profit Ratchet Ladders:</b> +1.5% ROE (BE Floor) &bull; +2.0% ROE (Tier 1) &bull; +3.5% ROE (Tier 2) &bull; +10% ROE (+5% Floor)<br>
              &bull; <b>Stagnation Rotation:</b> 24 Hours (48 Runs) max hold for ROE &lt; +1.5%<br>
              &bull; <b>Sizing Tier:</b> Standard 8%–10% ($40-$50 floor) / Ballistic 12%–14% on ATR Breakout
            </div>

            <div class="section-title">Funds (USD)</div>
            <div class="table-responsive">
              <table><thead><tr><th>Asset</th><th>Balance USD</th><th>Balance</th></tr></thead><tbody>{funds_rows}</tbody></table>
            </div>

            <div class="section-title">Positions per Bot (USD)</div>
            <div class="table-responsive">
              <table><thead><tr><th>Bot Title</th><th>Asset</th><th>Leverage</th><th>Side</th><th>Collateral USD</th><th>Unrealized P&L USD</th><th>Funding (Hourly)</th><th>Stop Price</th><th>Bot Status</th></tr></thead><tbody>{positions_rows}</tbody></table>
            </div>

            {audit_section}

          </div>
          <div class="footer">Hyperliquid Autonomous Engine &bull; Managed via GitHub Actions</div>
        </div>
      </body>
    </html>
    """

    send_html_dashboard_email(f"Hyperliquid Report — USD ${total_nav:.2f}", html_content, text_fallback)
    print(f"[{timestamp}] Institutional-grade telemetry update complete.")

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}"
        print(err_msg)
        send_html_dashboard_email("Hyperliquid Bot ERROR Alert", f"<h3>Error</h3><pre>{err_msg}</pre>", err_msg)
        raise e
