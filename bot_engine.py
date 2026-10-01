import os
import sys
import json
import time
import math
from datetime import datetime, timezone

# ==============================================================================
# CONFIGURATION & CONSTANTS (TR-GC-Crypto-LS-23-V2)
# ==============================================================================
STATE_FILE = "state.json"

# Core Operational Limits
MAX_SLOTS_DEFAULT = 2
MIN_NAV_FOR_TWO_SLOTS = 20.0  # USD
SLOT_FLOOR_USD = 10.0         # USD minimum position size

# Orderbook & Execution Gates
MAX_SPREAD_PCT = 0.0030       # 0.30%
MIN_DEPTH_USD = 10000.0       # $10,000 depth required within 0.5%
MAX_SLIPPAGE_PCT = 0.0020     # 0.20% max slippage buffer

# Circuit Breakers & Cooldowns
LOSS_COOLDOWN_SEC = 86400     # 24-hour blocklist on loss
ROLLING_WINDOW_SEC = 3600     # 60-minute window for loss counting
MAX_ROLLING_LOSSES = 3        # Trigger circuit breaker if 3 losses in window
HIBERNATION_SEC = 43200       # 12-hour emergency hibernation

# Filters & Indicator Thresholds
MAX_CHOPPINESS_INDEX = 52.0
MIN_VOLUME_RATIO = 1.3


# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load state.json: {e}. Reinitializing.")
    return {
        "active_positions": {},
        "loss_cooldowns": {},      # { symbol: timestamp_of_loss }
        "recent_losses": [],       # [ timestamp1, timestamp2, ... ]
        "hibernation_until": 0,    # timestamp
        "last_traded_candle": {}   # { symbol: candle_timestamp }
    }


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save state.json: {e}")


# ==============================================================================
# TECHNICAL INDICATOR CALCULATIONS
# ==============================================================================
def calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period


def calculate_choppiness_index(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 100.0
    
    tr_sum = 0.0
    for i in range(len(closes) - period, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_sum += tr
    
    max_high = max(highs[-period:])
    min_low = min(lows[-period:])
    
    range_diff = max_high - min_low
    if range_diff <= 0 or tr_sum <= 0:
        return 100.0
    
    ci = 100.0 * (math.log10(tr_sum / range_diff) / math.log10(period))
    return ci


def calculate_volume_ratio(volumes: list[float], period: int = 20) -> float:
    if len(volumes) < period + 1:
        return 0.0
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    current_vol = volumes[-1]
    return current_vol / avg_vol if avg_vol > 0 else 0.0


# ==============================================================================
# REGIME SHIELD & GATES
# ==============================================================================
def get_btc_regime(btc_daily_candle: dict) -> str:
    """
    Returns 'GREEN' (Daily Close > Open), 'RED' (Daily Close < Open), or 'NEUTRAL'.
    - GREEN blocks SHORTs
    - RED blocks LONGs
    - NEUTRAL blocks ALL trades
    """
    open_price = btc_daily_candle.get("open", 0.0)
    close_price = btc_daily_candle.get("close", 0.0)
    
    if close_price > open_price:
        return "GREEN"
    elif close_price < open_price:
        return "RED"
    return "NEUTRAL"


def check_orderbook_gate(bid: float, ask: float, depth_05_usd: float) -> bool:
    if bid <= 0 or ask <= 0:
        return False
    spread = (ask - bid) / ((ask + bid) / 2.0)
    if spread > MAX_SPREAD_PCT:
        print(f"[REJECT] Spread {spread:.4%} exceeds max {MAX_SPREAD_PCT:.4%}")
        return False
    if depth_05_usd < MIN_DEPTH_USD:
        print(f"[REJECT] Depth ${depth_05_usd:,.2f} below minimum ${MIN_DEPTH_USD:,.2f}")
        return False
    return True


def check_circuit_breaker(state: dict, now: float) -> bool:
    # 1. Active Hibernation Check
    if now < state.get("hibernation_until", 0):
        remaining_min = (state["hibernation_until"] - now) / 60
        print(f"[PAUSE] Bot in 12H Hibernation. {remaining_min:.1f} mins remaining.")
        return False

    # 2. Cleanup Old Losses (> 60m)
    recent = [t for t in state.get("recent_losses", []) if (now - t) <= ROLLING_WINDOW_SEC]
    state["recent_losses"] = recent

    # 3. Trigger New Hibernation if >= 3 losses
    if len(recent) >= MAX_ROLLING_LOSSES:
        state["hibernation_until"] = now + HIBERNATION_SEC
        print(f"[ALERT] {MAX_ROLLING_LOSSES} losses in rolling 60m. Entering 12H Hibernation!")
        save_state(state)
        return False

    return True


# ==============================================================================
# DYNAMIC RATINGS & PROFIT RATCHETS
# ==============================================================================
def get_galactic_moonshot_floor(peak_roe: float, initial_adaptive_stop: float) -> float:
    """
    Calculates the trailing stop floor based on peak Return-on-Equity (ROE).
    - Peak < +0.5%   : Dynamic Adaptive Stop (-1.5% to -4.0% floor)
    - Peak >= +0.5%  : +0.25% ROE
    - Peak >= +1.5%  : +1.00% ROE
    - Peak >= +3.0%  : Scales from 80% lock up to 95% lock at +300%+ ROE
    """
    if peak_roe < 0.5:
        return initial_adaptive_stop
    elif peak_roe < 1.5:
        return 0.25
    elif peak_roe < 3.0:
        return 1.00
    else:
        # Scale lock ratio from 80% (at 3% ROE) to 95% (at 300% ROE)
        lock_ratio = 0.80 + min(0.15, (peak_roe - 3.0) / 297.0 * 0.15)
        return peak_roe * lock_ratio


def calculate_adaptive_stop(atr_1h: float, price: float, leverage: float = 1.0) -> float:
    """
    Sets initial downside stop derived from 1.5x ATR 1H volatility buffer.
    Bounded between -1.5% and -4.0% ROE floor.
    """
    volatility_roe = (1.5 * atr_1h / price) * leverage * 100.0
    adaptive_stop = -max(1.5, min(4.0, volatility_roe))
    return adaptive_stop


# ==============================================================================
# EXECUTION ENGINE & POSITION MANAGEMENT
# ==============================================================================
def process_active_positions(state: dict, exchange_api, now: float):
    positions = state.get("active_positions", {})
    closed_symbols = []

    for symbol, pos in list(positions.items()):
        current_price = exchange_api.get_current_price(symbol)
        side = pos["side"] # "LONG" or "SHORT"
        entry_price = pos["entry_price"]
        leverage = pos.get("leverage", 1.0)

        # Raw Price ROE calculation
        if side == "LONG":
            raw_roe = ((current_price - entry_price) / entry_price) * leverage * 100.0
        else:
            raw_roe = ((entry_price - current_price) / entry_price) * leverage * 100.0

        # Track Peak ROE
        pos["peak_roe"] = max(pos.get("peak_roe", raw_roe), raw_roe)
        
        # Determine Stop ROE Floor
        stop_roe_floor = get_galactic_moonshot_floor(pos["peak_roe"], pos["initial_stop_roe"])

        # Stop-Loss / Lock Violation Check
        if raw_roe <= stop_roe_floor:
            print(f"[EXIT] {symbol} Triggered Stop! Raw ROE: {raw_roe:.2f}%, Floor: {stop_roe_floor:.2f}%")
            exchange_api.close_position(symbol)
            
            is_loss = raw_roe < 0.0
            if is_loss:
                state["loss_cooldowns"][symbol] = now
                state["recent_losses"].append(now)
                print(f"[COOLDOWN] {symbol} added to 24H loss blocklist.")

            closed_symbols.append(symbol)

    for sym in closed_symbols:
        del state["active_positions"][sym]


def evaluate_new_entries(state: dict, exchange_api, now: float):
    nav = exchange_api.get_net_worth()
    
    # Calculate Active Slots
    max_slots = 1 if nav < MIN_NAV_FOR_TWO_SLOTS else MAX_SLOTS_DEFAULT
    active_count = len(state.get("active_positions", {}))
    
    if active_count >= max_slots:
        return

    slot_nav_usd = max(SLOT_FLOOR_USD, nav / max_slots)
    btc_candle = exchange_api.get_btc_daily_candle()
    btc_regime = get_btc_regime(btc_candle)

    if btc_regime == "NEUTRAL":
        print("[SKIP] BTC Regime is NEUTRAL. All entries blocked.")
        return

    candidates = exchange_api.get_top_100_volume_candidates()

    for candidate in candidates:
        symbol = candidate["symbol"]

        # 1. Slot Check
        if len(state["active_positions"]) >= max_slots:
            break

        # 2. 24H Cooldown Gate
        last_loss_time = state["loss_cooldowns"].get(symbol, 0)
        if (now - last_loss_time) < LOSS_COOLDOWN_SEC:
            continue

        # 3. Single-Candle Lockout Gate
        current_candle_ts = candidate["candle_timestamp"]
        if state["last_traded_candle"].get(symbol) == current_candle_ts:
            continue

        # 4. Filter Signals (Chop & Volume)
        if candidate["ci"] > MAX_CHOPPINESS_INDEX or candidate["vol_ratio"] < MIN_VOLUME_RATIO:
            continue

        # 5. BTC Regime Shield Filter
        proposed_side = candidate["signal_side"] # "LONG" or "SHORT"
        if btc_regime == "GREEN" and proposed_side == "SHORT":
            continue
        if btc_regime == "RED" and proposed_side == "LONG":
            continue

        # 6. Orderbook Spread & Depth Gate
        bid, ask = candidate["bid"], candidate["ask"]
        depth_05 = candidate["depth_05_usd"]
        if not check_orderbook_gate(bid, ask, depth_05):
            continue

        # 7. Execute Order with Slippage Cap Buffer
        mid_price = (bid + ask) / 2.0
        limit_price = mid_price * (1 + MAX_SLIPPAGE_PCT) if proposed_side == "LONG" else mid_price * (1 - MAX_SLIPPAGE_PCT)
        
        success = exchange_api.place_order(
            symbol=symbol,
            side=proposed_side,
            amount_usd=slot_nav_usd,
            price=limit_price
        )

        if success:
            initial_stop = calculate_adaptive_stop(candidate["atr_1h"], mid_price, candidate["leverage"])
            state["active_positions"][symbol] = {
                "side": proposed_side,
                "entry_price": mid_price,
                "leverage": candidate["leverage"],
                "peak_roe": 0.0,
                "initial_stop_roe": initial_stop,
                "opened_at": now
            }
            state["last_traded_candle"][symbol] = current_candle_ts
            print(f"[ENTRY] Opened {proposed_side} on {symbol} @ ${mid_price:.4f} | Initial Stop: {initial_stop:.2f}%")


# ==============================================================================
# MAIN EXECUTION CYCLE
# ==============================================================================
def main():
    now = time.time()
    state = load_state()

    print(f"--- Running TR-GC-Crypto-LS-23-V2 | {datetime.now(timezone.utc).isoformat()} ---")

    # 1. Check Circuit Breakers / Emergency Hibernation
    if not check_circuit_breaker(state, now):
        save_state(state)
        return

    # Mock Exchange Interface - Interface with actual Hyperliquid SDK/API client here
    class ExchangeClientMock:
        def get_net_worth(self): return 100.0
        def get_btc_daily_candle(self): return {"open": 64000.0, "close": 65200.0}
        def get_current_price(self, sym): return 100.0
        def get_top_100_volume_candidates(self): return []
        def close_position(self, sym): return True
        def place_order(self, symbol, side, amount_usd, price): return True

    exchange_api = ExchangeClientMock()

    # 2. Manage Existing Positions & Apply Ratchet Ladder
    process_active_positions(state, exchange_api, now)

    # 3. Evaluate Universe for New Entries
    evaluate_new_entries(state, exchange_api, now)

    # 4. Save Updated State
    save_state(state)
    print("--- Cycle Complete ---")


if __name__ == "__main__":
    main()
