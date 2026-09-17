import os
import json
import time
from math import log10, floor
import eth_account
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

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_stop_price(entry_px, is_long, current_px, leverage=1.0):
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

    # TR-GC-Crypto-LS-23 Step-Up & Milestone Floor Rules
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

    asset_positions = user_state.get("assetPositions", [])
    if not asset_positions:
        print(f"[{timestamp}] No active open positions found.")
        save_state(state)
        return

    for pos_item in asset_positions:
        pos = pos_item.get("position", {})
        coin = pos.get("coin")
        szi = float(pos.get("szi", 0))
        if not coin or szi == 0:
            continue

        is_long = szi > 0
        sz = abs(szi)
        entry_px = float(pos.get("entryPx", 0))
        current_px = float(all_mids.get(coin, entry_px))

        leverage_info = pos.get("leverage", {})
        leverage = float(leverage_info.get("value", 1.0)) if isinstance(leverage_info, dict) else 1.0
        if leverage <= 0:
            leverage = 1.0

        stop_px_raw, current_roe, target_floor = calculate_stop_price(entry_px, is_long, current_px, leverage)
        px = round_sig_figs(stop_px_raw, 5)

        is_buy_order = not is_long

        # Track stagnation runs (<1% ROE)
        if current_roe < 0.01:
            state["stagnation_tracker"][coin] = state["stagnation_tracker"].get(coin, 0) + 1
        else:
            state["stagnation_tracker"][coin] = 0

        # Cancel stale trigger orders
        for order in open_orders:
            if order.get("coin") == coin and order.get("isTrigger"):
                exchange.cancel(coin, order["oid"])
                print(f"[{coin}] Cancelled stale trigger order (OID: {order['oid']})")

        # Place updated native trigger order on-chain
        res = exchange.order(
            coin,
            is_buy_order,
            sz,
            px,
            {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True
        )

        status_text = f"[{coin}] {'LONG' if is_long else 'SHORT'} ({sz}) | Entry: {entry_px} | ROE: {current_roe*100:+.2f}% | Stop: {px} ({target_floor*100:+.1f}% floor) -> Status: {res.get('status')}"
        print(status_text)

    save_state(state)
    print(f"[{timestamp}] Engine run complete. State successfully persisted.")

if __name__ == "__main__":
    try:
        execute_engine()
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Engine execution error: {e}")
        raise e
