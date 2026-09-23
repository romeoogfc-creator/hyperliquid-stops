def calculate_crypto_stop_price(entry_px, is_long, current_px, leverage=5.0, choppiness_index=50.0, is_ballistic=False, vol_ratio=1.0, peak_roe=0.0):
    if is_long:
        roe = ((current_px - entry_px) / entry_px) * leverage
    else:
        roe = ((entry_px - current_px) / entry_px) * leverage

    leash_status = "Standard Sniper Stop"
    
    # --- SMART VOLUME-ADAPTIVE TRAILING PEAK FOLLOWER ---
    # High volume (>= 1.5x) widens the trail so true runners run to the top; stalling volume (< 0.8x) tightens the lock instantly.
    if vol_ratio >= 1.5:
        vol_multiplier = 1.6  # Give runners breathing room (+60% buffer expansion)
        vol_tag = "🚀 Smart Runner (High Vol)"
    elif vol_ratio < 0.8:
        vol_multiplier = 0.7  # Tighter lock on volume stall
        vol_tag = "⚠️ Volume Stall Lock"
    else:
        vol_multiplier = 1.0
        vol_tag = "Balanced Peak Floor"

    if peak_roe >= 0.004:
        base_buffer = 0.005
        adjusted_buffer = base_buffer * vol_multiplier
        target_floor_roe = peak_roe - adjusted_buffer
        leash_status = f"{vol_tag} [{vol_ratio:.1f}x Vol] ({target_floor_roe*100:+.2f}%)"
    else:
        if roe >= 0.01:
            target_floor_roe = 0.0
            leash_status = "Break-Even Lock (+1.0% Trigger)"
        elif choppiness_index > 58.0:
            target_floor_roe = -0.012   # Choppy Stop to -1.2% ROE
            leash_status = "Choppy Defense Stop"
        elif is_ballistic:
            target_floor_roe = -0.020   # Ballistic Stop to -2.0% ROE
            leash_status = "Ballistic Stop"
        else:
            target_floor_roe = -0.015   # Trend Stop to -1.5% ROE
            leash_status = "Trend Defense Stop"

    if is_long:
        stop_px = entry_px * (1 + (target_floor_roe / leverage))
        is_buy_order = False
    else:
        stop_px = entry_px * (1 - (target_floor_roe / leverage))
        is_buy_order = True

    return stop_px, roe, target_floor_roe, is_buy_order, leash_status
