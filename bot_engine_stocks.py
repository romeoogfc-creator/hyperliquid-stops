import datetime
import typing

class TRGCEquitiesV4SuperBot:
    """
    TR-GC-Equities-LS-01 V4.0 Super Bot Engine
    Incorporates Ross Cameron's 13-Video Masterclass Rules:
    - 5 Pillars Stock Selection
    - Pre-Market Sprint Window & Exchange Safety
    - Crossing Candle Entry & Pullback Low Stop
    - Orderbook & Topping Tail Micro-Exits
    - 50% Peak Giveback & Drawdown Step-Down Sizing
    """
    def __init__(self, base_position_pct: float = 0.15, daily_max_loss: float = 200.0):
        self.base_position_pct = base_position_pct  # Standard 15% NAV
        self.daily_max_loss = daily_max_loss
        self.peak_intraday_pnl = 0.0
        self.peak_nav = 0.0
        self.vwap_rejection_tracker = {}
        self.disfavored_exchanges = ["NYSE", "AMEX", "NYSE American"]

    # =========================================================================
    # I. STOCK SELECTION & 5 PILLARS GATE
    # =========================================================================
    def evaluate_5_pillars_and_profile(
        self, 
        price: float, 
        float_shares: float, 
        pct_gain: float, 
        rvol: float, 
        has_news: bool, 
        exchange: str,
        market_cap: float,
        is_recent_reverse_split: bool,
        market_regime: str = "HOT"
    ) -> typing.Tuple[bool, str]:
        
        pillar_count = 0
        details = []

        # Pillar 1: Price $2.00 - $20.00
        if 2.00 <= price <= 20.00:
            pillar_count += 1
            details.append("Pillar 1 (Price $2-$20) OK")

        # Pillar 2: Float < 20M
        if float_shares < 20_000_000:
            pillar_count += 1
            details.append("Pillar 2 (Float <20M) OK")

        # Pillar 3: Gain >= +10.0%
        if pct_gain >= 0.10:
            pillar_count += 1
            details.append("Pillar 3 (Gain >= 10%) OK")

        # Pillar 4: RVOL >= 5.0x
        if rvol >= 5.0:
            pillar_count += 1
            details.append("Pillar 4 (RVOL >= 5.0x) OK")

        # Pillar 5: Catalyst / Sector Match
        if has_news:
            pillar_count += 1
            details.append("Pillar 5 (News Catalyst) OK")

        # Pillar Flexibility Check
        required_pillars = 5 if market_regime == "COLD" else 4
        if pillar_count < required_pillars:
            return False, f"REJECT: Met {pillar_count}/5 Pillars in {market_regime} market (Required: {required_pillars}). Details: {details}"

        # Exchange Safety Guard (Flying Blind NYSE/AMEX Halts)
        if exchange in self.disfavored_exchanges:
            return False, f"REJECT: {exchange} listed. Prefer NASDAQ to avoid 'Flying Blind' hidden halt quotes."

        # Sub-$5M Market Cap Defense Shield
        dilution_shield = "ACTIVE" if (market_cap < 5_000_000 and is_recent_reverse_split) else "INACTIVE"

        return True, f"APPROVED_5_PILLARS ({pillar_count}/5) | Exchange: {exchange} | Sub-$5M Shield: {dilution_shield}"

    # =========================================================================
    # II. ENTRY MECHANICS: CROSSING CANDLE & MACRO GUARDS
    # =========================================================================
    def validate_crossing_candle_entry(
        self, 
        df_1m: typing.Any, 
        df_daily: typing.Any, 
        current_price: float
    ) -> typing.Tuple[bool, typing.Optional[float], str]:
        
        # 1. Daily 200 SMA Ceiling Check ($DSY Loss Guard)
        if 'sma_200' in df_daily.columns:
            sma_200 = df_daily['sma_200'].iloc[-1]
            if current_price < sma_200 and (sma_200 - current_price) / current_price <= 0.05:
                return False, None, f"REJECT: Price ${current_price:.2f} within 5% below Daily 200 SMA ceiling (${sma_200:.2f})."

        # 2. Back-Side Move Guard (>30% drawdown from HOD)
        day_high = df_1m['high'].max()
        if (day_high - current_price) / day_high > 0.30:
            return False, None, "REJECT: Ticker is down >30% from HOD. Defined as Back-Side move."

        # 3. Crossing Candle Mechanics
        if len(df_1m) < 3:
            return False, None, "REJECT: Insufficient 1M candle history."

        current_candle = df_1m.iloc[-1]
        prev_pullback_candle = df_1m.iloc[-2]

        is_pullback = (prev_pullback_candle['close'] < prev_pullback_candle['open']) or \
                      ((prev_pullback_candle['high'] - max(prev_pullback_candle['open'], prev_pullback_candle['close'])) > 0.05)

        has_crossed = current_candle['high'] > prev_pullback_candle['high']

        if is_pullback and has_crossed:
            defined_stop_price = prev_pullback_candle['low']  # Anchor Stop to Pullback Low
            return True, defined_stop_price, f"APPROVED_CROSSING_CANDLE: Stop anchored at pullback low (${defined_stop_price:.2f})."

        return False, None, "REJECT: No crossing candle structure present."

    # =========================================================================
    # III. MICRO-EXITS & TAPE READING GUARDS
    # =========================================================================
    def check_micro_exit_triggers(
        self, 
        ticker: str, 
        current_price: float, 
        vwap_price: float, 
        level2_ask_wall_shares: int, 
        df_1m: typing.Any
    ) -> typing.Tuple[bool, str]:
        
        # 1. Level 2 Large Ask Wall Guard (>50k shares)
        if level2_ask_wall_shares >= 50_000:
            return True, f"MICRO_EXIT: Level 2 Ask Wall detected ({level2_ask_wall_shares:,} shares). High resistance."

        # 2. Topping Tail / Upper Wick Guard (>40% of candle range)
        latest_candle = df_1m.iloc[-1]
        c_range = latest_candle['high'] - latest_candle['low']
        upper_wick = latest_candle['high'] - max(latest_candle['open'], latest_candle['close'])
        if c_range > 0 and (upper_wick / c_range) >= 0.40:
            return True, "MICRO_EXIT: Topping Tail detected (>40% upper wick). Institutional distribution."

        # 3. Two-Strike VWAP Rejection Tracker
        if ticker not in self.vwap_rejection_tracker:
            self.vwap_rejection_tracker[ticker] = 0

        prev_candle = df_1m.iloc[-2]
        if prev_candle['high'] > vwap_price and latest_candle['close'] < vwap_price:
            self.vwap_rejection_tracker[ticker] += 1

        if self.vwap_rejection_tracker[ticker] >= 2:
            return True, f"MICRO_EXIT: {ticker} failed VWAP reclaim 2 times. Blacklisted."

        return False, "HOLD_POSITION_OK"

    # =========================================================================
    # IV. RISK & DRAWDOWN DISCIPLINE GUARDS
    # =========================================================================
    def enforce_drawdown_and_giveback_controls(
        self, 
        current_nav: float, 
        current_realized_pnl: float, 
        current_unrealized_pnl: float
    ) -> typing.Tuple[float, bool, str]:
        
        total_pnl = current_realized_pnl + current_unrealized_pnl

        # Track Peak Intraday Profit
        if total_pnl > self.peak_intraday_pnl:
            self.peak_intraday_pnl = total_pnl

        # Track Peak NAV
        if current_nav > self.peak_nav:
            self.peak_nav = current_nav

        # 1. Daily Max Loss Limit
        if total_pnl <= -self.daily_max_loss:
            return 0.0, True, f"SHUTDOWN: Daily Max Loss hit (-${abs(total_pnl):.2f}). Session Terminated."

        # 2. 50% Peak Giveback Shield
        if self.peak_intraday_pnl >= 100.0:
            giveback_floor = self.peak_intraday_pnl * 0.50
            if total_pnl <= giveback_floor:
                return 0.0, True, f"SHUTDOWN: 50% Peak Giveback Triggered (Peak: ${self.peak_intraday_pnl:.2f}, Current: ${total_pnl:.2f})."

        # 3. Post-Drawdown Step-Down Sizing Clamp (50% Size until 50% Recovery)
        drawdown_amount = self.peak_nav - current_nav
        if drawdown_amount > 0:
            recovery_target = self.peak_nav - (drawdown_amount * 0.50)
            if current_nav < recovery_target:
                clamped_size = self.base_position_pct * 0.50
                return clamped_size, False, f"DRAWDOWN_CLAMP: Position size clamped to {clamped_size*100:.1f}% NAV until 50% drawdown recovered."

        return self.base_position_pct, False, "NORMAL_OPERATING_STATE"
