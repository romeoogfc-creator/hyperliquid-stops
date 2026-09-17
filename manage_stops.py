import eth_account
import time
from math import log10, floor
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

ACCOUNT_ADDRESS = "0x31eD4bAA2e0c8b7348E7DD7A19A6A7406a675951"
SECRET_KEY = "0x27f75be595c08e758d9b945fbb0bb64d251a74a4f1b013bbbc230cfa37c8d25d"

def round_sig_figs(val, sig_figs=5):
    if val == 0:
        return 0
    return round(val, sig_figs - int(floor(log10(abs(val)))) - 1)

def calculate_stop_price(entry_px, is_long, current_px, leverage=1.0):
    # Calculate current ROE
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

    # TR-GC-Crypto-LS-23 Step-Up & Milestone Floor Rules
    if roe >= 0.30:        # Milestone +30% peak -> +22.0% floor
        target_floor_roe = 0.22
    elif roe >= 0.20:      # Milestone +20% peak -> +12.0% floor
        target_floor_roe = 0.12
    elif roe >= 0.10:      # Milestone +10% peak -> +5.0% floor
        target_floor_roe = 0.05
    elif roe >= 0.035:     # Tier 2 floor (+3.5% ROE -> +2.0% floor)
        target_floor_roe = 0.02
    elif roe >= 0.020:     # Tier 1 floor (+2.0% ROE -> +1.0% floor)
        target_floor_roe = 0.01
    elif roe >= 0.015:     # Soft BE (+1.5% ROE -> 0.0% floor)
        target_floor_roe = 0.00
    else:                  # Initial Hard Stop (-4.0% ROE)
        target_floor_roe = -0.04

    # Convert target floor ROE back to price
    if is_long:
        stop_px = entry_px * (1 + (target_floor_roe / leverage))
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))

    return stop_px, roe, target_floor_roe

def run_stop_sync():
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print("\n" + "="*60)
    print(f"[{timestamp}] Executing Native Stop-Sync Cycle...")
    
    wallet = eth_account.Account.from_key(SECRET_KEY)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    user_state = info.user_state(ACCOUNT_ADDRESS)
    open_orders = info.frontend_open_orders(ACCOUNT_ADDRESS)
    all_mids = info.all_mids()

    asset_positions = user_state.get("assetPositions", [])
    if not asset_positions:
        no_pos_msg = f"[{timestamp}] No active open positions found."
        print(no_pos_msg)
        with open("bot_log.txt", "a") as f:
            f.write(no_pos_msg + "\n")
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

        # Calculate exact strategy stop level
        stop_px_raw, current_roe, target_floor = calculate_stop_price(entry_px, is_long, current_px, leverage)
        px = round_sig_figs(stop_px_raw, 5)

        # LONG position stop = Reduce-Only SELL (is_buy=False)
        # SHORT position stop = Reduce-Only BUY (is_buy=True)
        is_buy_order = not is_long

        # 1. Cancel existing stale trigger orders for this coin
        for order in open_orders:
            if order.get("coin") == coin and order.get("isTrigger"):
                exchange.cancel(coin, order["oid"])
                print(f"[{coin}] Cancelled stale trigger order (OID: {order['oid']})")

        # 2. Place updated native trigger order directly on-chain
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
        
        # Append timestamped log entry to bot_log.txt
        with open("bot_log.txt", "a") as f:
            f.write(f"[{timestamp}] {status_text}\n")

if __name__ == "__main__":
    print("--- Starting Hyperliquid TR-GC-Crypto-LS-23 Fully Automated Stop Sync Daemon ---")
    while True:
        try:
            run_stop_sync()
        except Exception as e:
            err_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Error during stop sync execution: {e}"
            print(err_msg)
            with open("bot_log.txt", "a") as f:
                f.write(err_msg + "\n")
        
        print("\n[+] Cycle complete. Waiting 1 hour for next automated run...")
        time.sleep(3600)