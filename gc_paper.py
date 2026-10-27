"""
TR-GC-Crypto-LS-15 PAPER TRADER  (no orders are ever sent - no keys are even loaded)

Rebuilds the SIGNUM "TR-GC-Crypto-LS-15" long/short Gaussian Channel strategy on Hyperliquid daily candles and
runs it on two virtual accounts side by side:
  Book A  "LS-15 Original"      - the SIGNUM rules as written
  Book B  "LS-15 + Profit Lock" - same entries/top-ups/exits, plus the profit-lock ladder (keep 80% -> 97% of peak)
  Book C  "LS-15 + Account Guard" - same as A, but if the ACCOUNT gives back 40% of a +4% run-up, close everything
                                     (the $500 -> $530 -> $495 problem: lock ~$518 instead)
  Book D  "LS-15 + Long Lock"     - profit lock on LONGS only; shorts run on the original rules (backtest: the lock
                                     helped longs +$149 -> +$259 but cut shorts +$229 -> +$48)
  Book E  "LS-15 + Long Lock + Guard" - Book D plus the account guard
  Book F  "LS-15 @ 60% Size"      - Book A with every position 60% as big (less pain without touching the exits)
  Book G  "LS-15 Max 3 @ 15%"     - at most 3 positions (long or short), 15% of NAV each, filled in rank order
  Book H  "LS-15 Max 3 @ 15% + Long Lock" - Book G plus the long-only profit lock (the proposed live combination)
  Book I  "LS-15 3L+3S @ 15%"     - separate slot pools: up to 3 longs AND up to 3 shorts, 15% of NAV each
  Book J  "LS-15 3L+3S @ 15% + Long Lock" - Book I plus the long-only profit lock
  Book K  "LS-15 Max 6 @ 20%"     - at most 6 positions (long or short), 20% of NAV each (up to 120% in use = ~1.2x leverage)
  Book L  "LS-15 Max 6 @ 20% + Long Lock" - Book K plus the long-only profit lock
  Book M  "LS-15 + Long Lock (4h)" - Book D, but the profit lock is ratcheted every 4 hours (6x a day) instead of once a day
  Book N  "Max 6 @ 15% + Long Lock (4h)"  - at most 6 positions, 15% each (90% max in use), 4-hour lock
  Book O  "Max 10 @ 15% + Long Lock (4h)" - at most 10 positions, 15% each (up to 150% in use = ~1.5x margin), 4-hour lock

4-HOUR LOCK (Books M/N/O): entries and exits are still decided on the DAILY candle. Only the profit-lock stop is managed
intraday: it walks through the day's six 4-hour candles in order - a candle whose low touches the stop sells there, otherwise
its high raises the peak and the stop is moved up before the next candle. Live, that is a stop order on the exchange that
the bot raises every 4 hours.

SMART TRAIL (Books P/Q): instead of keeping a fixed % of the peak, the stop trails the highest price by 3x the coin's daily
ATR (average true range = its normal daily swing). A coin that swings 10% a day gets ~30% room, a calm one ~6%, and the room
grows with the price, so a 10,000% runner is not cut by normal pullbacks. Never goes red: after +5% the stop is at least +1%.
Book Q also tightens to 1.5x ATR when the move goes parabolic (close more than 2 ATR above the upper band) to sell near the top.

NO-MARGIN 10-SLOT BOOKS (1x, nothing borrowed): Book R = max 10 @ 15% (cash runs out after ~6 full slots, later entries are
downsized or skipped), Book S = max 10 @ 10% (exactly fits 100%). Both with the 4-hour long lock. S is Book O scaled to 1x.

Book T "D @ 2x" = Book D with every LS-15 size doubled (16% / 4% longs, 6% / 10% shorts, top-ups to 11%) and up to 200% of the
account in positions (2x margin). Built for small accounts: on $111 the doubled sizes clear Hyperliquid's $10 minimum more often.

Book U "LIVE SETUP" = exactly what ls15_live.py trades: max 9 positions @ 15% (up to 135% in use, ~1.35x), 4-hour long lock,
plus a disaster stop 35% against every position (long below entry x0.65 / short above entry x1.35).

Profit-protection variants of the live setup U (same 9 slots, 4-hour lock, disaster stop):
  Book V "EARLY LOCK"  - keeps 50% of the peak gain from +3%, 60% from +5%, 70% from +10%, 80/85/90% from +20/35/60%
  Book W "EARLY LOCK + SELLERS" - Book V, plus: once a long has been +3%, sell on a red day with volume >= 1.5x its
                         20-day average that closes in the bottom third of the day's range (sellers took over)
  Book X "TAKE HALF"   - Book U, plus: sell HALF at +10% (resting take-profit), the other half runs on U's lock
  Book Y  = V with the lock starting at +1% (keeps 50% of any gain from +1%)
  Book Z  = V with a -5% loss stop on every position (instead of the -35% disaster stop) -> cut losers fast, sit in cash
  Book Z3 = V with a -3% loss stop.   Book YZ = Y + the -5% loss stop
  Stock-bot variants of Book F (daily, no margin): F5 / F8 = F + a -5% / -8% loss stop; FZ = F + early lock + -5% stop

Every email also has a GROWTH CALCULATOR: each book's return so far, turned into average $ per day at $1k / $10k / $50k /
$200k and the capital that would have been needed for $200 and $400 a day.

MARKET (env MARKET): crypto (default, Hyperliquid) or stocks (Alpaca daily bars, ~100 large US stocks, SPY plays BTC's role
for the bear-short filter and the buy & hold comparison). Stocks: backtest and robust modes only for now. Accounts under
$2,000 at Alpaca cannot short or use margin, so LONG_ONLY=auto makes stock backtests under $2,000 long-only at 1x.

Modes (env MODE):
  robust   stability check: replay chosen books ROBUST_RUNS times over the same history, each time considering the coins in a
           different random order (which coin fills a slot first). Shows the typical and worst result, so a lucky ordering
           cannot pass for a real edge.
  paper    (default) process every daily candle closed since the last run, update both books, email the report
  backtest replay the same rules over the last BACKTEST_DAYS days and email the results (state is not touched)

Differences from SIGNUM that cannot be avoided (shown in every email):
  - Gaussian Channel computed here (DonovanWall: hlc3, 4 poles, period 144, multiplier 1.414), not SIGNUM's Trend Radar
  - "Market rank" = Hyperliquid open-interest value (Hyperliquid has no market cap)
  - "breakoutDate" = first daily close above the upper band since the channel last turned Green
"""
import os
import json
import math
import time
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import numpy as np

MARKET = os.getenv("MARKET", "crypto").strip().lower()
STOCKS = MARKET == "stocks"
STRATEGY_ID = "TR-GC-Equities-LS-15" if STOCKS else "TR-GC-Crypto-LS-15"
BENCH = "SPY" if STOCKS else "BTC"      # market filter for bear shorts + buy & hold comparison
ENGINE_VERSION = "GC-PAPER-1.19"
MODE = os.getenv("MODE", "paper").strip().lower()
BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "365") or 365)
STATE_FILE = "gc_paper_state.json"
START_NAV = float(os.getenv("START_NAV", "1000") or 1000)   # starting balance of each NEW book (Run workflow option)
ROBUST_RUNS = int(os.getenv("ROBUST_RUNS", "20") or 20)
ROBUST_BOOKS = [c.strip().upper() for c in os.getenv("ROBUST_BOOKS", "V,Y,Z,Z3,YZ").split(",") if c.strip()]
MIN_ORDER_USD = 1.0 if STOCKS else 10.0   # Hyperliquid rejects orders under ~$10; Alpaca fractional orders go down to $1
_LONG_ONLY_ENV = os.getenv("LONG_ONLY", "auto").strip().lower()
LONG_ONLY = (_LONG_ONLY_ENV in ("1", "true", "yes")) or (_LONG_ONLY_ENV == "auto" and STOCKS and START_NAV < 2000)

# Stocks universe: large, liquid US names (ranked each run by average dollar volume; the top 100 are traded).
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V", "MA", "UNH", "XOM", "JNJ", "PG", "HD", "COST",
    "ABBV", "MRK", "CVX", "LLY", "PEP", "KO", "BAC", "WMT", "ADBE", "CRM", "NFLX", "AMD", "ORCL", "CSCO", "ACN", "TMO", "MCD",
    "ABT", "INTC", "DIS", "WFC", "QCOM", "TXN", "DHR", "VZ", "CMCSA", "PFE", "NKE", "PM", "UPS", "NEE", "RTX", "HON", "INTU",
    "LOW", "AMGN", "IBM", "SPGI", "CAT", "GS", "UNP", "BA", "MS", "AMAT", "ISRG", "GE", "BKNG", "LMT", "PLD", "BLK", "MDT", "DE",
    "SYK", "ADP", "GILD", "TJX", "CB", "AXP", "SBUX", "MDLZ", "ADI", "LRCX", "C", "NOW", "REGN", "VRTX", "PANW", "MU", "SCHW",
    "CI", "ELV", "ZTS", "SO", "DUK", "BSX", "MO", "CL", "PYPL", "UBER", "PLTR", "COIN", "MSTR", "HOOD", "SHOP", "SNOW", "ABNB",
    "ARM", "SMCI", "MRVL", "CRWD", "QQQ", "SPY",
]

# ---------------- Gaussian Channel (DonovanWall defaults) ----------------
GC_POLES = 4
GC_PERIOD = 144
GC_MULT = 1.414
GC_WARMUP_BARS = 200                   # recursive filter needs history before its values are trustworthy

# ---------------- LS-15 rules ----------------
UNIVERSE_SIZE = 100                    # TRSORTORDER: first 100 by rank
DRIFT_OUT_RANK = 120                   # paper mode: exit when rank falls below 120 (hysteresis so rank #99/#101 doesn't churn)
LONG_SIZE_RECENT = 0.08                # breakout <= 25 days ago
LONG_SIZE_OLD = 0.02
BREAKOUT_RECENT_DAYS = 25
MIN_TRADE_NAV = 0.01                   # skip entries/top-ups below 1% of NAV
TOPUP_MIN_ROE = 0.10
TOPUP_TARGET = 0.055
HEDGE_SHORT_SIZE = 0.03
BEAR_SHORT_SIZE = 0.05
BEAR_SHORT_TOUCH = 0.98                # high >= 0.98 x filter
SHORT_TP_RATIO = 0.65                  # close <= 0.65 x entry
FEE_RATE = 0.0005 if STOCKS else 0.00045   # stocks: no commission, 0.05%/side for spread + slippage; crypto: Hyperliquid taker

# ---------------- Book B profit-lock ladder (price gain at 1x, peak from daily highs/lows) ----------------
# (peak gain reached, share of peak to keep). Below 10%: after +5% the stop moves to +1% so a winner never turns red.
LOCK_TIERS = [(1.00, 0.97), (0.60, 0.93), (0.35, 0.90), (0.20, 0.85), (0.10, 0.80)]
LOCK_FIRST_TRIGGER = 0.05
LOCK_FIRST_FLOOR = 0.01

# Book C account guard: once the book's value is GUARD_TRIGGER above its base (value when it was last flat / last reset),
# close all positions if it falls back to keeping only GUARD_KEEP of that run-up. $500 -> $530 -> exit near $518.
GUARD_TRIGGER = 0.04
GUARD_KEEP = 0.60

# (code, name, profit lock: False / "all" / "long", account guard)
BOOKS = [
    ("A", "LS-15 Original", False, False),
    ("B", "LS-15 + Profit Lock", "all", False),
    ("C", "LS-15 + Account Guard", False, True),
    ("D", "LS-15 + Long Lock", "long", False),
    ("E", "LS-15 + Long Lock + Guard", "long", True),
    ("F", "LS-15 @ 60% Size", False, False),
    ("G", "LS-15 Max 3 @ 15%", False, False),
    ("H", "LS-15 Max 3 @ 15% + Long Lock", "long", False),
    ("I", "LS-15 3L+3S @ 15%", False, False),
    ("J", "LS-15 3L+3S @ 15% + Long Lock", "long", False),
    ("K", "LS-15 Max 6 @ 20%", False, False),
    ("L", "LS-15 Max 6 @ 20% + Long Lock", "long", False),
    ("M", "LS-15 + Long Lock (4h)", "long", False),
    ("N", "Max 6 @ 15% + Long Lock (4h)", "long", False),
    ("O", "Max 10 @ 15% + Long Lock (4h)", "long", False),
    ("P", "LS-15 + Smart Trail (4h)", "long", False),
    ("Q", "LS-15 + Smart Trail + Climax (4h)", "long", False),
    ("R", "Max 10 @ 15% + Long Lock (4h), 1x", "long", False),
    ("S", "Max 10 @ 10% + Long Lock (4h), 1x", "long", False),
    ("T", "LS-15 + Long Lock @ 2x (D doubled)", "long", False),
    ("U", "LIVE SETUP: Max 9 @ 15% + Long Lock (4h) + Disaster Stop", "long", False),
    ("V", "U + Early Lock (keep 50-90% from +3%)", "long", False),
    ("W", "U + Early Lock + Sellers-Took-Over Exit", "long", False),
    ("X", "U + Take Half at +10%", "long", False),
    ("Y", "V + Lock from +1%", "long", False),
    ("Z", "V + -5% Loss Stop", "long", False),
    ("Z3", "V + -3% Loss Stop", "long", False),
    ("YZ", "V + Lock from +1% + -5% Loss Stop", "long", False),
    ("F5", "F + -5% Loss Stop", False, False),
    ("F8", "F + -8% Loss Stop", False, False),
    ("FZ", "F + Early Lock + -5% Loss Stop", "long", False),
]
# Extra per-book settings (books not listed use the LS-15 defaults).
#   size_mult: scales every entry and the top-up target.  max_pos / slot_size: at most N positions, each slot_size of NAV.
BOOK_OPTIONS = {
    "F": {"size_mult": 0.60},
    "G": {"max_pos": 3, "slot_size": 0.15},     # 15% each (14-17% range): $16.65 per position on a $111 account
    "H": {"max_pos": 3, "slot_size": 0.15},
    "I": {"max_long": 3, "max_short": 3, "slot_size": 0.15},   # longs and shorts don't compete for slots
    "J": {"max_long": 3, "max_short": 3, "slot_size": 0.15},
    # 6 x 20% = up to 120% of the account in use: perps allow it with margin (~1.2x, e.g. 2x isolated per position).
    # gross_cap = most of NAV that may be in positions at once (cash may go negative = borrowed margin).
    "K": {"max_pos": 6, "slot_size": 0.20, "gross_cap": 1.20},
    "L": {"max_pos": 6, "slot_size": 0.20, "gross_cap": 1.20},
    # lock_intraday: the profit-lock stop is checked and raised on every 4-hour candle instead of once a day.
    "M": {"lock_intraday": True},
    "N": {"max_pos": 6, "slot_size": 0.15, "lock_intraday": True},
    "O": {"max_pos": 10, "slot_size": 0.15, "gross_cap": 1.50, "lock_intraday": True},
    # trail: ATR trailing stop (chandelier) instead of the % ladder. k = ATRs below the peak; k_climax used when parabolic.
    "P": {"lock_intraday": True, "trail": {"k": 3.0}},
    "Q": {"lock_intraday": True, "trail": {"k": 3.0, "k_climax": 1.5}},
    # 1x (no gross_cap = only cash can be used, nothing borrowed)
    "R": {"max_pos": 10, "slot_size": 0.15, "lock_intraday": True},
    "S": {"max_pos": 10, "slot_size": 0.10, "lock_intraday": True},
    "T": {"size_mult": 2.0, "gross_cap": 2.0},                # Book D with 2x sizes and up to 2x margin
    # U mirrors ls15_live.py: 9 slots x 15% (135% max), 4h long lock, disaster stop 35% from entry on every position.
    "U": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.35},
    "V": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.35,
          "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "W": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.35,
          "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]],
          "seller_exit": {"after_gain": 0.03, "vol_mult": 1.5, "close_in_bottom": 0.35}},
    "X": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.35,
          "take_half": {"at": 0.10, "frac": 0.5}},
    # "disaster" doubles as the loss stop: a resting stop that far against the entry, long or short.
    "Y": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.35,
          "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50], [0.01, 0.50]]},
    "Z": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.05,
          "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "Z3": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.03,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "F5": {"size_mult": 0.60, "disaster": 0.05},
    "F8": {"size_mult": 0.60, "disaster": 0.08},
    "FZ": {"size_mult": 0.60, "disaster": 0.05,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "YZ": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.05,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50], [0.01, 0.50]]},
}
VOL_AVG_DAYS = 20                      # seller-exit: today's volume vs the previous 20 days
ATR_PERIOD = 14
CLIMAX_ATR = 2.0                       # "parabolic" = daily close more than 2 ATR above the upper band
INTRADAY_INTERVAL = "4h"               # candles used by lock_intraday books (Hyperliquid keeps the last 5000 = ~833 days)
INTRADAY_MS = 4 * 3600 * 1000
INTRADAY_MAX_DAYS = 830

# Growth calculator (every email): account sizes to scale results to, and the daily-profit targets to size for.
GROWTH_SIZES = [1000, 10000, 50000, 200000]
GROWTH_TARGETS = [200, 400]
GROWTH_MIN_DAYS = 30                   # below this, annualising a few days of results is meaningless


def lock_applies(book, side):
    """Profit lock on all positions ("all", or True from older saved state), on longs only ("long"), or off."""
    mode = book.get("lock")
    return mode is True or mode == "all" or (mode == "long" and side == "LONG")


# ======================================================================================
# Indicator
# ======================================================================================
def gaussian_filter(values, alpha, poles):
    """N-pole Gaussian filter (Ehlers / DonovanWall f_filt9x):
    f[t] = a^N * src[t] + sum_k (-1)^(k+1) * C(N,k) * (1-a)^k * f[t-k].  Equivalent to N cascaded EMAs with the same alpha."""
    x = 1.0 - alpha
    coefs = [((-1) ** (k + 1)) * math.comb(poles, k) * x ** k for k in range(1, poles + 1)]
    out = np.zeros(len(values))
    for t, v in enumerate(values):
        f = (alpha ** poles) * v
        for k, ck in enumerate(coefs, start=1):
            if t - k >= 0:
                f += ck * out[t - k]
        out[t] = f
    return out


def gaussian_channel(bars):
    """bars: list of dicts with o,h,l,c. Returns per-bar filter, upper band, trend ('Green'/'Red'/'Grey')."""
    h = np.array([b["h"] for b in bars], dtype=float)
    l = np.array([b["l"] for b in bars], dtype=float)
    c = np.array([b["c"] for b in bars], dtype=float)
    src = (h + l + c) / 3.0
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    beta = (1 - math.cos(4 * math.asin(1) / GC_PERIOD)) / (math.pow(1.414, 2.0 / GC_POLES) - 1)
    alpha = -beta + math.sqrt(beta ** 2 + 2 * beta)
    filt = gaussian_filter(src, alpha, GC_POLES)
    filt_tr = gaussian_filter(tr, alpha, GC_POLES)
    upper = filt + filt_tr * GC_MULT
    trend = ["Grey"] + ["Green" if filt[i] > filt[i - 1] else "Red" if filt[i] < filt[i - 1] else "Grey" for i in range(1, len(filt))]
    return filt, upper, trend


def enrich(bars):
    """Adds filter/upper/trend and the LS-15 breakout date to each bar (in place). Returns bars."""
    if len(bars) < 3:
        return bars
    filt, upper, trend = gaussian_channel(bars)
    atr = None
    breakout = None
    for i, b in enumerate(bars):
        b["filter"], b["upper"], b["trend"] = float(filt[i]), float(upper[i]), trend[i]
        if i > 0 and trend[i] == "Green" and trend[i - 1] != "Green":
            breakout = None                                   # a new uptrend starts: wait for its first band break
        if i > 0 and breakout is None and b["c"] > b["upper"] and bars[i - 1]["c"] <= bars[i - 1]["upper"]:
            breakout = b["date"]
        b["breakout"] = breakout
        b["valid"] = i >= GC_WARMUP_BARS
        b["_prev"] = bars[i - 1] if i > 0 else None
        past = [x.get("v", 0) for x in bars[max(0, i - VOL_AVG_DAYS):i]]
        b["vavg"] = (sum(past) / len(past)) if len(past) >= VOL_AVG_DAYS and sum(past) > 0 else None
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        atr = tr if atr is None else atr + (tr - atr) / ATR_PERIOD          # Wilder's ATR
        b["atr"] = atr
    return bars


# ======================================================================================
# Paper book
# ======================================================================================
def new_book(code, name, lock, guard=False):
    book = {"code": code, "name": name, "lock": lock, "guard": guard, "cash": START_NAV, "positions": {}, "closed": [], "equity": [],
            "eq_base": START_NAV, "eq_hwm": START_NAV, "guard_hits": 0, "start_nav": START_NAV}
    book.update(BOOK_OPTIONS.get(code, {}))
    return book


def buying_power(book, nav):
    """Cash available for a new position. Books with gross_cap may borrow margin up to gross_cap x NAV in positions."""
    cap = None if LONG_ONLY else book.get("gross_cap")       # long-only (small stock account) = cash only, no margin
    if not cap:
        return book["cash"]
    return book["cash"] + max(0.0, (cap - 1.0) * nav)


def slotted(book):
    return bool(book.get("max_pos") or book.get("max_long") or book.get("max_short"))


def entry_size(book, nav, base_fraction):
    """Position size in USD: a fixed slot (max-N books) or the LS-15 size scaled by size_mult."""
    if slotted(book):
        return nav * book["slot_size"]
    return nav * base_fraction * book.get("size_mult", 1.0)


def slots_full(book, side):
    """Max-N books: one shared pool (max_pos) or separate long / short pools (max_long / max_short)."""
    if book.get("max_pos"):
        return len(book["positions"]) >= book["max_pos"]
    cap = book.get("max_long" if side == "LONG" else "max_short")
    return bool(cap) and sum(1 for p in book["positions"].values() if p["side"] == side) >= cap


def slot_cap(book, side):
    return book.get("max_pos") or book.get("max_long" if side == "LONG" else "max_short")


def pos_value(p, px):
    if p["side"] == "LONG":
        return p["qty"] * px
    return p["collateral"] + (p["entry"] - px) * p["qty"]


def book_nav(book, prices):
    return book["cash"] + sum(pos_value(p, prices.get(c, p["last_px"])) for c, p in book["positions"].items())


def lock_floor(peak_gain, tiers=None):
    """Locked gain for a peak gain. Default = the 80-97% ladder with the +5% -> +1% floor; a book's own lock_tiers
    (e.g. Book V: keep 50% from +3%) replace both."""
    if tiers:
        for trigger, keep in tiers:
            if peak_gain >= trigger:
                return peak_gain * keep
        return None
    for trigger, keep in LOCK_TIERS:
        if peak_gain >= trigger:
            return peak_gain * keep
    if peak_gain >= LOCK_FIRST_TRIGGER:
        return LOCK_FIRST_FLOOR
    return None


def open_position(book, coin, side, usd, px, date, reason, log, budget=None):
    budget = book["cash"] if budget is None else budget
    fee = usd * FEE_RATE
    if usd + fee > budget:
        usd = max(0.0, budget / (1 + FEE_RATE))
        fee = usd * FEE_RATE
    qty = usd / px
    book["cash"] -= usd + fee
    book["positions"][coin] = {"side": side, "qty": qty, "entry": px, "cost": usd, "collateral": usd if side == "SHORT" else 0.0,
                               "fees": fee, "opened": date, "peak_px": px, "stop": None, "last_px": px, "reason": reason}
    log.append(("ENTRY", coin, f"{side} {reason} — ${usd:,.2f} @ {px:.6g}"))


def topup_position(book, coin, usd, px, date, log):
    p = book["positions"][coin]
    fee = usd * FEE_RATE
    qty = usd / px
    p["entry"] = (p["entry"] * p["qty"] + px * qty) / (p["qty"] + qty)
    p["qty"] += qty
    p["cost"] += usd
    p["fees"] += fee
    book["cash"] -= usd + fee
    log.append(("TOP-UP", coin, f"+${usd:,.2f} @ {px:.6g} (avg entry now {p['entry']:.6g})"))


def close_position(book, coin, px, date, reason, log):
    p = book["positions"].pop(coin)
    value = pos_value(p, px)
    fee = p["qty"] * px * FEE_RATE
    book["cash"] += value - fee
    move = (p["entry"] - px) if p["side"] == "SHORT" else (px - p["entry"])
    pnl = move * p["qty"] - p["fees"] - fee
    gain = (px / p["entry"] - 1) if p["side"] == "LONG" else (1 - px / p["entry"])
    peak_gain = (p["peak_px"] / p["entry"] - 1) if p["side"] == "LONG" else (1 - p["peak_px"] / p["entry"])
    book["closed"].append({"coin": coin, "side": p["side"], "opened": p["opened"], "closed": date, "entry": p["entry"], "exit": px,
                           "pnl": round(pnl, 4), "pct": round(gain * 100, 2), "peak_pct": round(max(peak_gain, 0) * 100, 2),
                           "reason": reason, "why_in": p["reason"]})
    log.append(("EXIT", coin, f"{p['side']} {reason} — {gain*100:+.2f}% (peak {max(peak_gain,0)*100:+.2f}%), ${pnl:+,.2f}"))


def lock_label(book):
    return "📈 Trailing stop" if book.get("trail") else "🔒 Profit lock"


def set_lock_stop(book, p, high, low):
    """Raise the position's peak with a candle's high (long) / low (short) and move the profit-lock stop to match."""
    trail = book.get("trail")
    if trail and not p.get("atr"):
        p["peak_px"] = max(p["peak_px"], high) if p["side"] == "LONG" else min(p["peak_px"], low)
        return                                                # no ATR yet (first day): just track the peak
    if trail:
        if p["side"] == "LONG":
            p["peak_px"] = max(p["peak_px"], high)
            if p["peak_px"] / p["entry"] - 1 >= LOCK_FIRST_TRIGGER:
                k = trail["k_climax"] if trail.get("k_climax") and p.get("climax") else trail["k"]
                room = k * p.get("atr", 0.0)
                new = max(p["entry"] * (1 + LOCK_FIRST_FLOOR), p["peak_px"] - room)
                p["stop"] = new if p["stop"] is None else max(p["stop"], new)      # a trailing stop only moves up
        else:
            p["peak_px"] = min(p["peak_px"], low)
            if 1 - p["peak_px"] / p["entry"] >= LOCK_FIRST_TRIGGER:
                k = trail["k_climax"] if trail.get("k_climax") and p.get("climax") else trail["k"]
                new = min(p["entry"] * (1 - LOCK_FIRST_FLOOR), p["peak_px"] + k * p.get("atr", 0.0))
                p["stop"] = new if p["stop"] is None else min(p["stop"], new)
        return
    if p["side"] == "LONG":
        p["peak_px"] = max(p["peak_px"], high)
        fl = lock_floor(p["peak_px"] / p["entry"] - 1, book.get("lock_tiers"))
        p["stop"] = None if fl is None else p["entry"] * (1 + fl)
    else:
        p["peak_px"] = min(p["peak_px"], low)
        fl = lock_floor(1 - p["peak_px"] / p["entry"], book.get("lock_tiers"))
        p["stop"] = None if fl is None else p["entry"] * (1 - fl)


def partial_close(book, coin, px, frac, date, reason, log):
    """Sell `frac` of a long at px (Book X take-half). The rest of the position keeps running."""
    p = book["positions"][coin]
    q = p["qty"] * frac
    fee = q * px * FEE_RATE
    fees_part = p["fees"] * frac
    book["cash"] += q * px - fee
    pnl = (px - p["entry"]) * q - fees_part - fee
    p["qty"] -= q
    p["cost"] *= (1 - frac)
    p["fees"] -= fees_part
    p["half_taken"] = True
    gain = px / p["entry"] - 1
    book["closed"].append({"coin": coin, "side": "LONG", "opened": p["opened"], "closed": date, "entry": p["entry"], "exit": px,
                           "pnl": round(pnl, 4), "pct": round(gain * 100, 2), "peak_pct": round(max(p["peak_px"] / p["entry"] - 1, gain) * 100, 2),
                           "reason": reason, "why_in": p["reason"]})
    log.append(("EXIT", coin, f"LONG {reason} — {frac*100:.0f}% sold at {gain*100:+.2f}%, ${pnl:+,.2f}"))


def sellers_took_over(book, p, b):
    """Book W: the long has been up `after_gain`, and today is a red day on heavy volume closing near its low."""
    se = book.get("seller_exit")
    if not se or p["side"] != "LONG" or p["opened"] == b.get("date") or not b.get("vavg"):
        return False
    rng = b["h"] - b["l"]
    return (max(p["peak_px"], b["h"]) / p["entry"] - 1 >= se["after_gain"] and b["c"] < b["o"] and b.get("v", 0) >= se["vol_mult"] * b["vavg"]
            and rng > 0 and (b["c"] - b["l"]) <= se["close_in_bottom"] * rng)


def stop_label(book):
    return "✂️ Loss stop" if book.get("disaster", 1) <= 0.10 else "🧯 Disaster stop"


def take_half_hit(book, p, high):
    th = book.get("take_half")
    return bool(th and p["side"] == "LONG" and not p.get("half_taken") and high >= p["entry"] * (1 + th["at"]))


def step_day(book, day, data, ranked, btc_down, drift_exits, log, skips, data4h=None):
    """Runs one LS-15 decision cycle on the daily candle `day` (YYYY-MM-DD) for one book."""
    bar = {c: data[c][day] for c in data if day in data[c]}
    prev = {c: b["_prev"] for c, b in bar.items() if b.get("_prev")}   # previous bar (previous TRADING day for stocks)
    closes = {c: b["c"] for c, b in bar.items()}
    rank_of = {c: i + 1 for i, c in enumerate(ranked)}
    universe = ranked[:UNIVERSE_SIZE]
    exited, entered = set(), set()

    for c, p in book["positions"].items():
        if c in closes:
            p["last_px"] = closes[c]

    # Profit-lock books: resting stop (set at the end of yesterday) can fill during today's candle.
    # lock_intraday books walk the day's 4-hour candles instead, raising the stop after each one.
    if book.get("lock"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            if not b or not lock_applies(book, p["side"]):
                continue
            intraday = (data4h or {}).get(c, {}).get(day) if book.get("lock_intraday") else None
            if intraday:
                for k in intraday:
                    # The loss / disaster stop rests on the exchange too: inside the day it competes with the lock,
                    # candle by candle (checking it only against the daily low let a dip-then-rally day exit at a profit).
                    loss_px = p["entry"] * (1 - book["disaster"]) if book.get("disaster") and p["side"] == "LONG" and p["opened"] != day else None
                    if p["side"] == "LONG" and loss_px and (p["stop"] is None or loss_px > p["stop"]) and k["l"] <= loss_px:
                        close_position(book, c, min(k["o"], loss_px), day, f"{stop_label(book)} (-{book['disaster']*100:.0f}%, 4h)", log)
                        exited.add(c)
                        break
                    if p["stop"] is not None and p["side"] == "LONG" and k["l"] <= p["stop"]:
                        close_position(book, c, min(k["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g}, 4h)", log)
                        exited.add(c)
                        break
                    if p["stop"] is not None and p["side"] == "SHORT" and k["h"] >= p["stop"]:
                        close_position(book, c, max(k["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g}, 4h)", log)
                        exited.add(c)
                        break
                    if take_half_hit(book, p, k["h"]):
                        tp = p["entry"] * (1 + book["take_half"]["at"])
                        partial_close(book, c, max(k["o"], tp), book["take_half"]["frac"], day,
                                      f"💵 Take half at +{book['take_half']['at']*100:.0f}%", log)
                    set_lock_stop(book, p, k["h"], k["l"])    # this candle's high/low raises the peak, stop moves up
                continue
            if take_half_hit(book, p, b["h"]) and not (p["stop"] is not None and b["l"] <= p["stop"]):
                tp = p["entry"] * (1 + book["take_half"]["at"])
                partial_close(book, c, max(b["o"], tp), book["take_half"]["frac"], day, f"💵 Take half at +{book['take_half']['at']*100:.0f}%", log)
            if p["stop"] is None:
                continue
            if p["side"] == "LONG" and b["l"] <= p["stop"]:
                close_position(book, c, min(b["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g})", log)
                exited.add(c)
            elif p["side"] == "SHORT" and b["h"] >= p["stop"]:
                close_position(book, c, max(b["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g})", log)
                exited.add(c)

    # Disaster stop (Book U / live bot): a resting stop far from entry on every position, long or short.
    if book.get("disaster"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            if not b or p["opened"] == day:
                continue
            if p["side"] == "LONG" and b["l"] <= p["entry"] * (1 - book["disaster"]):
                px = p["entry"] * (1 - book["disaster"])
                close_position(book, c, min(b["o"], px), day, f"{stop_label(book)} (-{book['disaster']*100:.0f}%)", log)
                exited.add(c)
            elif p["side"] == "SHORT" and b["h"] >= p["entry"] * (1 + book["disaster"]):
                px = p["entry"] * (1 + book["disaster"])
                close_position(book, c, max(b["o"], px), day, f"{stop_label(book)} (+{book['disaster']*100:.0f}% against)", log)
                exited.add(c)

    nav = book_nav(book, closes)

    # Book C: account guard on today's close.
    if book.get("guard"):
        if not book["positions"]:
            book["eq_base"] = book["eq_hwm"] = nav            # flat: measure the next run-up from here
        book["eq_hwm"] = max(book["eq_hwm"], nav)
        run_up = book["eq_hwm"] - book["eq_base"]
        if book["positions"] and run_up >= GUARD_TRIGGER * book["eq_base"] and nav <= book["eq_base"] + GUARD_KEEP * run_up:
            why = (f"🛡️ Account guard: value ${nav:,.2f} gave back {(book['eq_hwm']-nav)/run_up*100:.0f}% of the "
                   f"${book['eq_base']:,.2f} → ${book['eq_hwm']:,.2f} run-up")
            for c in list(book["positions"]):
                b = bar.get(c)
                close_position(book, c, b["c"] if b else book["positions"][c]["last_px"], day, why, log)
                exited.add(c)
            book["guard_hits"] += 1
            nav = book_nav(book, closes)
            book["eq_base"] = book["eq_hwm"] = nav

    # 4) LONG EXITS
    for c in list(book["positions"]):
        p = book["positions"][c]
        if p["side"] != "LONG":
            continue
        b = bar.get(c)
        if drift_exits and rank_of.get(c, 10 ** 6) > DRIFT_OUT_RANK:
            close_position(book, c, b["c"] if b else p["last_px"], day, "Drifted out of top ranks", log)
            exited.add(c)
        elif not b or not b.get("valid"):
            skips.append((c, "Long held but indicator data missing — kept"))
        elif sellers_took_over(book, p, b):
            close_position(book, c, b["c"], day, f"🐻 Sellers took over (red day, volume {b['v']/b['vavg']:.1f}x avg, closed near the low)", log)
            exited.add(c)
        elif b["c"] < b["upper"]:
            close_position(book, c, b["c"], day, "Close below upper GC band", log)
            exited.add(c)

    # 5) SHORT EXITS
    for c in list(book["positions"]):
        p = book["positions"][c]
        if p["side"] != "SHORT":
            continue
        b = bar.get(c)
        if drift_exits and rank_of.get(c, 10 ** 6) > DRIFT_OUT_RANK:
            close_position(book, c, b["c"] if b else p["last_px"], day, "Drifted out of top ranks", log)
            exited.add(c)
        elif not b or not b.get("valid"):
            skips.append((c, "Short held but indicator data missing — kept"))
        elif b["c"] > b["filter"]:
            close_position(book, c, b["c"], day, "Stop: close above GC filter", log)
            exited.add(c)
        elif b["c"] <= SHORT_TP_RATIO * p["entry"]:
            close_position(book, c, b["c"], day, "Take profit (-35%)", log)
            exited.add(c)

    nav = book_nav(book, closes)

    # 6) LONG ENTRIES (rank order)
    for c in universe:
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid"):
            continue
        if not (b["c"] > b["upper"] and pb["c"] <= pb["upper"]):
            continue
        if c in exited:
            skips.append((c, "Long signal, but exited earlier this run")); continue
        if c in book["positions"]:
            skips.append((c, f"Long signal, already holding {book['positions'][c]['side']}")); continue
        if slots_full(book, "LONG"):
            skips.append((c, f"Long signal, all {slot_cap(book, 'LONG')} long slots in use")); continue
        recent = b.get("breakout") and (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(b["breakout"], "%Y-%m-%d")).days <= BREAKOUT_RECENT_DAYS
        want = entry_size(book, nav, LONG_SIZE_RECENT if recent else LONG_SIZE_OLD)
        power = buying_power(book, nav)
        usd = min(want, power / (1 + FEE_RATE))
        if usd < MIN_ORDER_USD:
            skips.append((c, f"Long signal, ${usd:,.2f} is below Hyperliquid's ${MIN_ORDER_USD:.0f} minimum order")); continue
        if usd < nav * MIN_TRADE_NAV:
            skips.append((c, f"Long signal, not enough cash (${usd:,.2f} < 1% NAV)")); continue
        tag = f"band breakout ({'recent' if recent else 'old'} breakout, {want / nav * 100:.0f}% NAV{', downsized' if usd < want - 0.01 else ''})"
        open_position(book, c, "LONG", usd, b["c"], day, tag, log, power)
        entered.add(c)

    # 7) LONG TOP-UPS (rank order, free cash only)
    for c in universe:
        p, b = book["positions"].get(c), bar.get(c)
        if not p or p["side"] != "LONG" or c in entered or c in exited or not b or not b.get("valid") or slotted(book):
            continue                                          # max-N books already hold full-size slots: no top-ups
        if b["c"] <= b["upper"]:
            continue
        roe = (b["c"] - p["entry"]) / p["entry"]
        held = p["qty"] * b["c"]
        target = nav * TOPUP_TARGET * book.get("size_mult", 1.0)
        if roe < TOPUP_MIN_ROE or held >= target:
            continue
        usd = min(target - held, buying_power(book, nav) / (1 + FEE_RATE))
        if usd < max(nav * MIN_TRADE_NAV, MIN_ORDER_USD):
            skips.append((c, f"Top-up wanted (ROE {roe*100:+.1f}%), ${usd:,.2f} is below 1% NAV / the ${MIN_ORDER_USD:.0f} minimum")); continue
        topup_position(book, c, usd, b["c"], day, log)

    # 8) SHORT ENTRIES (rank order)
    for c in ([] if LONG_ONLY else universe):
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid") or c in book["positions"]:
            continue
        hedge = b["trend"] == "Red" and b["c"] < b["filter"] and pb["c"] >= pb["filter"]
        bear = btc_down and b["trend"] == "Red" and b["h"] >= BEAR_SHORT_TOUCH * b["filter"] and b["c"] < b["filter"]
        if not (hedge or bear):
            continue
        if c in exited:
            skips.append((c, "Short signal, but exited earlier this run")); continue
        if slots_full(book, "SHORT"):
            skips.append((c, f"Short signal, all {slot_cap(book, 'SHORT')} short slots in use")); continue
        want = entry_size(book, nav, HEDGE_SHORT_SIZE if hedge else BEAR_SHORT_SIZE)
        power = buying_power(book, nav)
        usd = min(want, power / (1 + FEE_RATE))
        if STOCKS:                                            # stock shorts: whole shares only
            usd = math.floor(usd / b["c"]) * b["c"]
            if usd <= 0:
                skips.append((c, f"Short signal, one share (${b['c']:,.2f}) is more than the position size")); continue
        if usd < MIN_ORDER_USD:
            skips.append((c, f"Short signal, ${usd:,.2f} is below the ${MIN_ORDER_USD:.0f} minimum order")); continue
        if usd < nav * MIN_TRADE_NAV:
            skips.append((c, f"Short signal, not enough cash (${usd:,.2f} < 1% NAV)")); continue
        open_position(book, c, "SHORT", usd, b["c"], day, "hedge short (cross under filter)" if hedge else f"bear short (rejected bounce, {BENCH} down)", log, power)

    # Update peaks with today's candle and (profit-lock books) set tomorrow's resting stop.
    for c, p in book["positions"].items():
        b = bar.get(c)
        if b and b.get("atr"):
            p["atr"] = b["atr"]                               # trail room for the next day comes from today's ATR
            p["climax"] = p["side"] == "LONG" and b["c"] - b["upper"] > CLIMAX_ATR * b["atr"]
        if not b or p["opened"] == day:
            continue                                          # entered at today's close: today's high/low came BEFORE the entry
        if lock_applies(book, p["side"]):
            set_lock_stop(book, p, b["h"], b["l"])
        elif p["side"] == "LONG":
            p["peak_px"] = max(p["peak_px"], b["h"])
        else:
            p["peak_px"] = min(p["peak_px"], b["l"])

    point = [day, round(book_nav(book, closes), 2)]
    if book["equity"] and book["equity"][-1][0] == day:
        book["equity"][-1] = point                            # never record the same day twice
    else:
        book["equity"].append(point)


# ======================================================================================
# Data
# ======================================================================================
def fetch_market(info, history_days, paced=False):
    """Returns (ranked coin list, {coin: {date: bar}}). Rank = open-interest value, majors included.
    paced=True (live bot): waits long enough between coins to stay well under Hyperliquid's 1200 weight/minute."""
    meta, ctxs = info.meta_and_asset_ctxs()
    rows = []
    for asset, ctx in zip(meta.get("universe", []), ctxs):
        if asset.get("isDelisted"):
            continue
        try:
            oi_usd = float(ctx.get("openInterest", 0) or 0) * float(ctx.get("markPx", 0) or 0)
        except (TypeError, ValueError):
            continue
        if oi_usd > 0:
            rows.append((oi_usd, asset["name"]))
    rows.sort(reverse=True)
    ranked = [c for _, c in rows]
    if "BTC" not in ranked[:UNIVERSE_SIZE]:
        ranked.insert(0, "BTC")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (history_days + GC_WARMUP_BARS + 30) * 86400000
    data = {}
    for coin in ranked[:DRIFT_OUT_RANK]:
        time.sleep(max(0.5, (20 + (history_days + GC_WARMUP_BARS + 30) / 60) * 0.05) if paced else 0.5)
        candles = None
        for attempt in range(3):
            try:
                candles = info.candles_snapshot(coin, "1d", start_ms, now_ms)
                break
            except Exception:
                time.sleep(2 + attempt * 3)
        if not candles:
            continue
        bars = []
        for k in candles:
            t = int(k["t"])
            if t + 86400000 > now_ms:          # still-forming daily candle: not CLOSED, ignore
                continue
            bars.append({"date": datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                         "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v", 0) or 0)})
        enrich(bars)
        data[coin] = {b["date"]: b for b in bars}
    return ranked, data


def fetch_intraday(info, coins, history_days):
    """{coin: {date: [4h bars in time order]}} for the lock_intraday books. Paced for Hyperliquid's 1200 weight/minute."""
    days = min(history_days + 2, INTRADAY_MAX_DAYS)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400000
    weight = 20 + days * 6 / 60                               # candleSnapshot weight = 20 + candles/60
    out = {}
    for coin in coins:
        time.sleep(max(0.5, weight / 1000 * 60))               # stay under ~1000 weight/minute
        candles = None
        for attempt in range(4):
            try:
                candles = info.candles_snapshot(coin, INTRADAY_INTERVAL, start_ms, now_ms)
                break
            except Exception:
                time.sleep(15 * (attempt + 1))                 # 429 / network: back off
        if not candles:
            continue
        by_day = {}
        for k in sorted(candles, key=lambda k: int(k["t"])):
            t = int(k["t"])
            if t + INTRADAY_MS > now_ms:
                continue
            d = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(d, []).append({"o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
        out[coin] = by_day
    return out


# ---------------- Stocks (Alpaca market data; read-only, no orders) ----------------
STOCK_NOTE = ("STOCKS MODE: Alpaca daily bars (split/dividend adjusted), ~100 large US stocks ranked by average dollar volume, "
              "SPY replaces BTC as the market filter and the benchmark. 0.05% per side for spread/slippage (no commission). "
              "Accounts under $2,000 cannot short or use margin at Alpaca, so those backtests are long-only at 1x. Stock shorts use "
              "whole shares. Overnight gaps fill stops at the open. The 4-hour lock uses regular-hours hourly bars.")
ALPACA_DATA = "https://data.alpaca.markets/v2/stocks/bars"
_feed = {"name": os.getenv("APAL_HIST_FEED", "sip")}       # free plan: full SIP history except the last 15 minutes


def _et_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone(timedelta(hours=-4))


ET_ZONE = _et_zone()


def alpaca_bars(symbols, timeframe, start_dt, end_dt):
    import requests
    headers = {"APCA-API-KEY-ID": os.getenv("APAL_API_KEY_ID", ""), "APCA-API-SECRET-KEY": os.getenv("APAL_SECRET_KEY", ""),
               "accept": "application/json"}
    if not headers["APCA-API-KEY-ID"]:
        raise RuntimeError("APAL_API_KEY_ID / APAL_SECRET_KEY missing — stock data needs the Alpaca keys")
    out = {}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "limit": 10000, "adjustment": "all", "feed": _feed["name"],
              "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
    pages = 0
    while pages < 500:
        res = None
        for attempt in range(5):
            try:
                res = requests.get(ALPACA_DATA, params=params, headers=headers, timeout=30)
            except Exception:
                time.sleep(5 * (attempt + 1))
                continue
            if res.status_code == 429 or res.status_code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            break
        if res is None:
            raise RuntimeError("Alpaca data API unreachable")
        if res.status_code == 403 and _feed["name"] == "sip":
            print("[WARN] SIP feed not allowed on this plan — falling back to IEX", flush=True)
            _feed["name"] = params["feed"] = "iex"
            continue
        if res.status_code != 200:
            raise RuntimeError(f"Alpaca bars {timeframe} failed ({res.status_code}): {res.text[:200]}")
        body = res.json()
        for sym, bl in (body.get("bars") or {}).items():
            out.setdefault(sym, []).extend(bl)
        pages += 1
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
        time.sleep(0.35)                                       # free plan: 200 requests/minute
    return out


def _et(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET_ZONE)


def fetch_stock_market(history_days):
    """Same shape as fetch_market: (ranked symbols, {symbol: {date: bar}}). Rank = average dollar volume, last 20 days."""
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=16)
    start = now - timedelta(days=history_days + 340)          # + ~230 trading days for the channel warm-up
    raw = alpaca_bars(STOCK_UNIVERSE, "1Day", start, end)
    today_et = now.astimezone(ET_ZONE)
    data, dollar_vol = {}, {}
    for sym, bl in raw.items():
        rows = []
        for k in sorted(bl, key=lambda k: k["t"]):
            d = _et(k["t"]).strftime("%Y-%m-%d")
            if d == today_et.strftime("%Y-%m-%d") and (today_et.hour, today_et.minute) < (16, 15):
                continue                                       # today's bar is still forming
            rows.append({"date": d, "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k.get("v", 0))})
        if len(rows) < 3:
            continue
        enrich(rows)
        data[sym] = {r["date"]: r for r in rows}
        dollar_vol[sym] = float(np.mean([r["c"] * r["v"] for r in rows[-20:]]))
    ranked = sorted(data, key=lambda s: dollar_vol.get(s, 0), reverse=True)
    print(f"Stocks: {len(data)} symbols with daily bars (feed {_feed['name']})", flush=True)
    return ranked, data


def fetch_stock_intraday(coins, history_days):
    """{symbol: {date: [regular-hours hourly bars in order]}} for the lock_intraday books."""
    now = datetime.now(timezone.utc)
    raw = alpaca_bars(list(coins), "1Hour", now - timedelta(days=history_days + 3), now - timedelta(minutes=16))
    out = {}
    for sym, bl in raw.items():
        by_day = {}
        for k in sorted(bl, key=lambda k: k["t"]):
            t = _et(k["t"])
            if not (9 <= t.hour <= 15):                          # regular session (the 9:00 bar holds the 9:30 open)
                continue
            by_day.setdefault(t.strftime("%Y-%m-%d"), []).append({"o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
        out[sym] = by_day
    return out


def needs_intraday(books):
    return any(b.get("lock_intraday") for b in books)


def btc_downtrend(data, day):
    b = data.get(BENCH, {}).get(day)
    return bool(b and b.get("valid") and b["c"] < b["filter"])


# ======================================================================================
# Reporting
# ======================================================================================
def book_start(book):
    return float(book.get("start_nav", 1000.0))           # books saved before GC-PAPER-1.3 started at $1,000


def book_stats(book, start_nav=None):
    start_nav = start_nav or book_start(book)
    closed = book["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    eq = [e[1] for e in book["equity"]] or [start_nav]
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    capture = [t["pct"] / t["peak_pct"] for t in wins if t["peak_pct"] > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    return {
        "nav": eq[-1], "ret": (eq[-1] / start_nav - 1) * 100, "mdd": mdd * 100, "trades": len(closed),
        "wins": len(wins), "losses": len(losses), "winrate": len(wins) / len(closed) * 100 if closed else 0.0,
        "avg_w": np.mean([t["pct"] for t in wins]) if wins else 0.0, "avg_l": np.mean([t["pct"] for t in losses]) if losses else 0.0,
        "pf": gross_w / gross_l if gross_l > 0 else float("inf") if gross_w > 0 else 0.0,
        "capture": np.mean(capture) * 100 if capture else 0.0,
        "long_pnl": sum(t["pnl"] for t in closed if t["side"] == "LONG"),
        "short_pnl": sum(t["pnl"] for t in closed if t["side"] == "SHORT"),
        "longs": sum(1 for t in closed if t["side"] == "LONG"), "shorts": sum(1 for t in closed if t["side"] == "SHORT"),
    }


def make_chart(books, btc_curve, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=110)
    for book, color in zip(books, ["#2563eb", "#16a34a", "#9333ea", "#0891b2", "#db2777", "#64748b", "#ea580c", "#000000", "#65a30d", "#7c3aed", "#be123c", "#0d9488", "#a16207", "#4f46e5", "#15803d", "#c026d3", "#0369a1", "#9f1239", "#047857", "#854d0e", "#1d4ed8", "#be185d", "#0f766e", "#b45309"] * 3):
        if book["equity"]:
            ax.plot([datetime.strptime(d, "%Y-%m-%d") for d, _ in book["equity"]], [v / book_start(book) * 100 - 100 for _, v in book["equity"]],
                    label=f"{book['code']}: {book['name']}", color=color, linewidth=1.6)
    if btc_curve:
        ax.plot([datetime.strptime(d, "%Y-%m-%d") for d, _ in btc_curve], [v for _, v in btc_curve], label=f"{BENCH} buy & hold",
                color="#f59e0b", linewidth=1, linestyle="--")
    ax.axhline(0, color="#94a3b8", linewidth=0.8)
    ax.set_ylabel("Return %")
    ax.set_title(title, fontsize=10)
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    path = "gc_chart.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def fmt_usd_short(v):
    return f"${v/1e6:,.2f}M" if v >= 1e6 else f"${v/1e3:,.0f}k" if v >= 1e4 else f"${v:,.0f}"


def growth_section(books, prices):
    """Growth calculator: each book's results scaled to bigger accounts, and the capital a $/day target would need."""
    rows = ""
    for book in books:
        start = book_start(book)
        s = book_stats(book)
        nav = book_nav(book, prices) if prices else s["nav"]
        days = len(book["equity"])
        if not days or start <= 0:
            continue
        growth = nav / start
        if days >= GROWTH_MIN_DAYS and growth > 0:
            yearly = growth ** (365.0 / days) - 1
            per_day = yearly / 365                             # average daily profit per $1 over a year at this pace
            pace = f"{yearly*100:+.0f}%/yr"
        else:
            per_day = (growth - 1) / days                      # too early to annualise: plain average so far
            pace = f"<span style='color:#b45309;'>too early ({days}d)</span>"
        size_cells = "".join(f"<td style='color:{pct_color(per_day)};'>{'+' if per_day >= 0 else '-'}${abs(size*per_day):,.{2 if abs(size*per_day) < 10 else 0}f}</td>"
                             for size in GROWTH_SIZES)
        need = "".join(f"<td>{fmt_usd_short(t/per_day) if per_day > 0 else '—'}</td>" for t in GROWTH_TARGETS)
        dip = (f"{s['mdd']:.0f}%" + (f" (≈-{fmt_usd_short(abs(s['mdd'])/100*GROWTH_TARGETS[0]/per_day)})" if per_day > 0 else ""))
        rows += (f"<tr><td><b>{book['code']}</b></td><td style='color:{pct_color(growth-1)};'>{(growth-1)*100:+.1f}%</td><td>{pace}</td>"
                 f"{size_cells}{need}<td style='color:#b91c1c;'>{dip}</td></tr>")
    if not rows:
        return ""
    heads = "".join(f"<th align='left'>$/day @ {fmt_usd_short(x)}</th>" for x in GROWTH_SIZES)
    heads += "".join(f"<th align='left'>Capital for ${t}/day</th>" for t in GROWTH_TARGETS)
    return f"""
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:14px;overflow-x:auto;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">💰 Growth calculator</div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
        <tr style="background:#f1f5f9;"><th align="left">Book</th><th align="left">Return</th><th align="left">Pace</th>{heads}<th align="left">Worst drop (at the $200/day size)</th></tr>
        {rows}</table>
      <div style="color:#64748b;margin-top:6px;">$/day = average over a year at this book's pace (30+ days of results), or the plain average so far
        if it is younger. Real profit is lumpy: most days are flat or red and a few big winners make the month. Bigger accounts also get
        worse fills on small coins. The last column shows the worst account drop in dollars at the size needed for $200/day.</div>
    </div>"""


def fmt_stop(p):
    return "—" if p.get("stop") is None else f"{p['stop']:.6g}"


def pct_color(v):
    return "#15803d" if v >= 0 else "#b91c1c"


def real_nav():
    """Paper emails show every book scaled to the REAL live account (the live bot's starting balance in state.json)."""
    try:
        with open("state.json") as f:
            v = float(json.load(f).get("start_nav") or 0)
        if v > 0:
            return v
    except Exception:
        pass
    return float(os.getenv("REAL_NAV", "111") or 111)


def render_email(title, subtitle, books, day_logs, skips, prices, btc_curve, regime_line, chart_cid, notes, growth="", real=None):
    cards = ""
    for book in books:
        s = book_stats(book)
        nav = book_nav(book, prices) if prices else s["nav"]
        long_usd = sum(pos_value(p, prices.get(c, p["last_px"])) for c, p in book["positions"].items() if p["side"] == "LONG")
        short_usd = sum(p["qty"] * prices.get(c, p["last_px"]) for c, p in book["positions"].items() if p["side"] == "SHORT")
        flat_usd = max(book["cash"], 0.0)                     # negative cash = margin in use (Books K/L)
        tot = max(long_usd + short_usd + flat_usd, 1e-9)
        bar = (f"<div style='display:flex;height:12px;border-radius:4px;overflow:hidden;margin:6px 0;'>"
               f"<div style='width:{long_usd/tot*100:.1f}%;background:#16a34a;'></div>"
               f"<div style='width:{flat_usd/tot*100:.1f}%;background:#cbd5e1;'></div>"
               f"<div style='width:{short_usd/tot*100:.1f}%;background:#dc2626;'></div></div>")
        pos_rows = "".join(
            f"<tr><td>{c}</td><td style='color:{'#16a34a' if p['side']=='LONG' else '#dc2626'};'>{p['side']}</td><td>{p['entry']:.6g}</td>"
            f"<td>{prices.get(c, p['last_px']):.6g}</td>"
            f"<td style='color:{pct_color(((prices.get(c,p['last_px'])/p['entry']-1) if p['side']=='LONG' else (1-prices.get(c,p['last_px'])/p['entry'])))};'>"
            f"{(((prices.get(c,p['last_px'])/p['entry']-1) if p['side']=='LONG' else (1-prices.get(c,p['last_px'])/p['entry']))*100):+.2f}%</td>"
            f"<td>{fmt_stop(p)}</td><td>{p['opened']}</td></tr>"
            for c, p in sorted(book["positions"].items())) or "<tr><td colspan='7' style='color:#64748b;'>No open positions</td></tr>"
        recent = "".join(
            f"<tr><td>{t['closed']}</td><td>{t['coin']}</td><td>{t['side']}</td>"
            f"<td style='color:{pct_color(t['pct'])};'>{t['pct']:+.2f}%</td><td>{t['peak_pct']:+.2f}%</td><td>{t['reason']}</td></tr>"
            for t in list(reversed(book["closed"]))[:8]) or "<tr><td colspan='6' style='color:#64748b;'>No closed trades yet</td></tr>"
        cards += f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin-bottom:14px;background:#fff;">
          <div style="font-weight:700;font-size:14px;color:#0f172a;">Book {book['code']} — {book['name']}</div>
          <div style="font-size:22px;font-weight:700;color:{pct_color(s['ret'])};margin-top:4px;">{f"${real * nav / book_start(book):,.2f}" if real else f"${nav:,.2f}"} <span style="font-size:13px;">({(nav/book_start(book)-1)*100:+.2f}%){f" on your real ${real:,.2f} • paper book ${nav:,.2f} of ${book_start(book):,.0f}" if real else ""}</span></div>
          <div style="font-size:11px;color:#475569;">Trades {s['trades']} ({s['wins']}W/{s['losses']}L, {s['winrate']:.0f}%) &bull;
            Avg win {s['avg_w']:+.2f}% / avg loss {s['avg_l']:+.2f}% &bull; Profit factor {s['pf']:.2f} &bull;
            Biggest account give-back from a high {s['mdd']:.1f}% &bull;{f" Account guard fired {book.get('guard_hits', 0)}x &bull;" if book.get("guard") else ""}
            <b>Kept {s['capture']:.0f}% of peak</b> on winners &bull; Longs {s['longs']} (${s['long_pnl']:+,.2f}) / Shorts {s['shorts']} (${s['short_pnl']:+,.2f})</div>
          <div style="font-size:11px;color:#334155;margin-top:6px;">Positioning: <span style="color:#16a34a;">Long ${long_usd:,.0f}</span> &bull;
            <span style="color:#64748b;">Flat ${flat_usd:,.0f}</span> &bull; <span style="color:#dc2626;">Short ${short_usd:,.0f}</span></div>{bar}
          <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
            <tr style="background:#f1f5f9;"><th align="left">Coin</th><th align="left">Side</th><th align="left">Entry</th><th align="left">Now</th><th align="left">P&amp;L</th><th align="left">Lock stop</th><th align="left">Since</th></tr>{pos_rows}</table>
          <div style="font-size:11px;font-weight:700;margin-top:8px;color:#475569;">Recent exits</div>
          <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
            <tr style="background:#f1f5f9;"><th align="left">Date</th><th align="left">Coin</th><th align="left">Side</th><th align="left">Result</th><th align="left">Peak</th><th align="left">Why</th></tr>{recent}</table>
        </div>"""
    activity = ""
    for day, entries in day_logs:
        if not entries:
            activity += f"<div style='color:#64748b;'>{day}: no signals</div>"
            continue
        activity += f"<div style='font-weight:700;margin-top:6px;'>{day}</div>"
        for code, kind, coin, msg in entries:
            color = {"ENTRY": "#2563eb", "EXIT": "#b45309", "TOP-UP": "#7c3aed"}.get(kind, "#334155")
            activity += f"<div>&bull; [{code}] <b style='color:{color};'>{kind}</b> {coin}: {msg}</div>"
    skip_html = "".join(f"<div>&bull; {c}: {why}</div>" for c, why in skips) or "<div style='color:#64748b;'>None</div>"
    chart = f"<img src='cid:{chart_cid}' style='width:100%;max-width:640px;border-radius:6px;'/>" if chart_cid else ""
    notes_html = "".join(f"<div>&bull; {n}</div>" for n in notes)
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
  <div style="background:#0f172a;color:#fff;padding:14px;">
    <div style="font-size:15px;font-weight:700;">{title}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{subtitle}</div>
  </div>
  <div style="padding:12px;">
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin-bottom:12px;">
      📝 PAPER TRADING ONLY — this script holds no keys and cannot place orders. {regime_line}
      {f"<br><b>Big numbers are scaled to your real account (${real:,.2f})</b>: each paper book runs on $1,000, so its % result is applied to ${real:,.2f}. "
       "Position values and $ in the tables below are still per $1,000 book." if real else ""}</div>
    {chart}
    {growth}
    {cards}
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:12px;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">Activity (all books)</div>{activity or "<div style='color:#64748b;'>No new candles processed</div>"}</div>
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:12px;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">Skipped / not executed (and why)</div>{skip_html}</div>
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:10px;color:#64748b;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;">How this paper test works</div>{notes_html}</div>
  </div>
</div></body></html>"""


def send_email(subject, html, text, chart_path):
    sender, password, to = os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"), os.getenv("RECEIVER_EMAIL")
    if not (sender and password and to):
        print("[WARN] Email credentials missing; report printed only.\n" + text, flush=True)
        return
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)
    if chart_path and os.path.exists(chart_path):
        with open(chart_path, "rb") as f:
            img = MIMEImage(f.read(), "png")
        img.add_header("Content-ID", "<gcchart>")
        img.add_header("Content-Disposition", "inline", filename="gc_chart.png")
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    print(f"Email sent: {subject}", flush=True)


NOTES = [
    "Virtual books (each with its own starting balance), all taking the same LS-15 entry signals. Book A = SIGNUM LS-15 rules as written. Book B = the same, plus a per-trade profit lock: "
    "after +5% the stop moves to +1%; after +10/20/35/60/100% it keeps 80/85/90/93/97% of the peak (resting stop, checked against each day's low/high).",
    "Book C = Book A plus an ACCOUNT guard: once the book is up 4%+ from where it was last flat, it closes everything if it falls back to keeping "
    "only 60% of that run-up (your $500 → $530 example would lock about $518 instead of $495).",
    "Book D = profit lock on LONGS only (shorts keep the original stop / -35% take-profit). Book E = Book D plus the account guard.",
    "Book F = Book A with every position 60% as big. Book G = at most 3 positions (long or short), 15% of NAV each, filled in rank order, no top-ups. "
    "Book H = Book G plus the long-only profit lock (the proposed live setup: LS-15 signals, max 3 positions, profit lock).",
    "Book I = separate pools: up to 3 longs AND up to 3 shorts, 15% of NAV each (up to ~90% of the account in use). Book J = Book I plus the long-only profit lock.",
    "Book K = at most 6 positions (long or short), 20% of NAV each, no top-ups: up to 120% of the account in use, which needs ~1.2x leverage "
    "(margin; 'Flat' shows $0 while borrowed margin is in use). Book L = Book K plus the long-only profit lock.",
    "Book M = Book D, but the profit-lock stop is checked and raised on every 4-hour candle (6x a day) instead of once a day — entries and "
    "exits are still decided on the daily close. Book N = max 6 positions @ 15% with the 4-hour lock. Book O = max 10 @ 15% (up to 150% in use, "
    "~1.5x margin) with the 4-hour lock. Within each 4-hour candle the stop is checked before the candle's high raises it (conservative).",
    "Book P = LS-15 + SMART TRAIL on longs: stop = highest price since entry minus 3x the coin's daily ATR (its normal daily swing), raised every "
    "4 hours, never below +1% once the trade has been +5%. Volatile coins get more room, and the room grows as the price grows, so big runners "
    "are not cut by normal pullbacks. Book Q = Book P, but it tightens to 1.5x ATR when the coin goes parabolic (close > 2 ATR above the upper band).",
    "Books R and S = 1x, NO margin: max 10 positions with the 4-hour long lock. R = 15% each (cash runs out after ~6 full positions; later "
    "entries are downsized or skipped). S = 10% each (10 positions fit exactly in 100%) — Book O scaled down to 1x.",
    "Book T = Book D with every size doubled and up to 2x margin (200% of the account in positions) — gains AND losses roughly double.",
    "Book U = THE LIVE SETUP (ls15_live.py): max 9 positions @ 15% (up to 135% in use, ~1.35x), 4-hour long lock, and a disaster stop "
    "35% from entry on every position.",
    "Book V = U with an EARLY lock: keeps 50% of the peak gain from +3%, 60% from +5%, 70% from +10%, 80/85/90% from +20/35/60%. "
    "Book W = V plus a SELLERS-TOOK-OVER exit: once a long has been +3%, it sells on a red day whose volume is 1.5x+ the 20-day average "
    "and that closes in the bottom third of its range. Book X = U plus TAKE HALF: sells half at +10%, the other half runs on U's lock.",
    "Book Y = V with the lock starting at +1% (keeps 50% of any gain from +1%). Book Z = V with a -5% loss stop on every position "
    "(replaces the -35% disaster stop). Book Z3 = V with a -3% loss stop. Book YZ = Y plus the -5% loss stop. Loss stops are resting "
    "stops checked against each day's low/high (fill at the stop, or at the open if it gapped through).",
    "Books F5 / F8 = Book F (60% sizes, no lock) plus a -5% / -8% loss stop. Book FZ = Book F plus the early lock and the -5% loss stop "
    "(daily checks). Built to test loss stops for the stock bot.",
    "Gaussian Channel computed here from Hyperliquid daily candles (hlc3, 4 poles, period 144, x1.414) — close to SIGNUM's Trend Radar, not identical.",
    "Rank = Hyperliquid open-interest value (no market cap on Hyperliquid). breakoutDate = first close above the upper band since the channel turned Green.",
    "Fills at the daily close (lock stops at the stop price, or the open if it gapped through). 0.045% fee per side. Funding is not modelled.",
    "Orders under $10 are skipped, as Hyperliquid would reject them (matters for small accounts: 2% of $111 = $2.22).",
]


def btc_buy_hold(data, days):
    btc = data.get(BENCH, {})
    pts = [(d, btc[d]["c"]) for d in days if d in btc]
    if not pts:
        return []
    base = pts[0][1]
    return [(d, (c / base - 1) * 100) for d, c in pts]


# ======================================================================================
# Modes
# ======================================================================================
def backtest_days(all_days):
    """Candles of the last BACKTEST_DAYS calendar days (crypto: one per day; stocks: trading days only)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=BACKTEST_DAYS)).strftime("%Y-%m-%d")
    return [d for d in all_days if d >= cutoff]


def load_market(info, history_days):
    return fetch_stock_market(history_days) if STOCKS else fetch_market(info, history_days)


def load_intraday(info, coins, history_days):
    return fetch_stock_intraday(coins, history_days) if STOCKS else fetch_intraday(info, coins, history_days)


def run_backtest(info):
    ranked, data = load_market(info, BACKTEST_DAYS)
    universe = ranked[:UNIVERSE_SIZE]
    all_days = sorted({d for c in data for d in data[c]})
    days = backtest_days(all_days)
    books = [new_book(code, name, lock, guard) for code, name, lock, guard in BOOKS]
    data4h = load_intraday(info, universe, BACKTEST_DAYS) if needs_intraday(books) else {}
    min_order_skips = {b["code"]: 0 for b in books}
    for day in days:
        btc_down = btc_downtrend(data, day)
        for book in books:
            skips = []
            step_day(book, day, data, universe, btc_down, False, [], skips, data4h)
            min_order_skips[book["code"]] += sum(1 for _, why in skips if "minimum order" in why or "minimum" in why)
    last = days[-1] if days else ""
    prices = {c: data[c][last]["c"] for c in data if last in data[c]}
    btc_curve = btc_buy_hold(data, days)
    chart = make_chart(books, btc_curve, f"Backtest {days[0] if days else ''} → {last} (all books vs {BENCH})")
    title = f"{STRATEGY_ID} BACKTEST — {len(days)} days from ${START_NAV:,.0f}"
    btc_ret = btc_curve[-1][1] if btc_curve else 0.0
    summary = " | ".join(f"{b['code']}: {book_stats(b)['ret']:+.1f}% (DD {book_stats(b)['mdd']:.1f}%)" for b in books) + f" | {BENCH} {btc_ret:+.1f}%"
    blocked = ", ".join(f"{k}: {v}" for k, v in min_order_skips.items() if v)
    notes = ([STOCK_NOTE] if STOCKS else []) + NOTES + [f"Starting balance ${START_NAV:,.2f} per book. Signals skipped because the order would be under ${MIN_ORDER_USD:.0f}: "
                     f"{blocked or 'none'}.", f"Backtest universe = today's top {UNIVERSE_SIZE} by open interest (coins that died earlier are missing, which flatters results slightly). "
                     "No drift-out exits in backtest (no historical ranks). Open positions are valued at the last close."]
    html = render_email(title, f"{days[0] if days else ''} → {last} • {summary}", books, [], [], prices, btc_curve,
                        f"{BENCH} buy & hold over the same period: {btc_ret:+.1f}%.", "gcchart" if chart else None, notes, growth_section(books, prices))
    text = f"{title}\n{summary}\n" + "\n".join(f"{b['code']}: {book_stats(b)}" for b in books)
    print(text, flush=True)
    send_email(f"📊 {title} — {summary}", html, text, chart)


def run_robust(info):
    """Same history, ROBUST_RUNS different coin orders. Run 1 = the normal open-interest order."""
    ranked, data = load_market(info, BACKTEST_DAYS)
    universe = ranked[:UNIVERSE_SIZE]
    all_days = sorted({d for c in data for d in data[c]})
    days = backtest_days(all_days)
    specs = [b for b in BOOKS if b[0] in ROBUST_BOOKS]
    if not specs:
        raise RuntimeError(f"ROBUST_BOOKS {ROBUST_BOOKS} matches no book")
    data4h = load_intraday(info, universe, BACKTEST_DAYS) if any(BOOK_OPTIONS.get(c, {}).get("lock_intraday") for c, *_ in specs) else {}
    btc_down = {d: btc_downtrend(data, d) for d in days}
    last = days[-1]
    prices = {c: data[c][last]["c"] for c in data if last in data[c]}
    results = {code: [] for code, *_ in specs}               # per run: (return %, worst drop %)
    for run in range(ROBUST_RUNS):
        order = list(universe)
        if run:
            random.Random(run).shuffle(order)                  # fixed seeds: the same check gives the same answer
        for code, name, lock, guard in specs:
            book = new_book(code, name, lock, guard)
            for day in days:
                step_day(book, day, data, order, btc_down[day], False, [], [], data4h)
            st = book_stats(book)
            results[code].append(((book_nav(book, prices) / book_start(book) - 1) * 100, st["mdd"]))
        print(f"run {run + 1}/{ROBUST_RUNS}: " + " | ".join(f"{c} {r[-1][0]:+.1f}%" for c, r in results.items()), flush=True)
    btc_curve = btc_buy_hold(data, days)
    btc_ret = btc_curve[-1][1] if btc_curve else 0.0
    ref = "D" if "D" in results else None
    rows, summary = "", []
    for code, name, *_ in specs:
        rets = [r for r, _ in results[code]]
        dds = [d for _, d in results[code]]
        med = float(np.median(rets))
        beat_ref = (sum(1 for a, b in zip(rets, results[ref]) if a > b[0]) if ref and code != ref else None)
        rows += (f"<tr><td><b>{code}</b> {name}</td><td style='color:{pct_color(rets[0])};'>{rets[0]:+.1f}%</td>"
                 f"<td style='color:{pct_color(med)};font-weight:700;'>{med:+.1f}%</td>"
                 f"<td style='color:{pct_color(min(rets))};'>{min(rets):+.1f}%</td><td style='color:{pct_color(max(rets))};'>{max(rets):+.1f}%</td>"
                 f"<td>{float(np.median(dds)):.1f}%</td><td style='color:#b91c1c;'>{min(dds):.1f}%</td>"
                 f"<td>{sum(1 for r in rets if r > btc_ret)}/{len(rets)}</td>"
                 f"<td>{'—' if beat_ref is None else f'{beat_ref}/{len(rets)}'}</td></tr>")
        summary.append(f"{code}: median {med:+.0f}% (worst {min(rets):+.0f}%, DD {min(dds):.0f}%)")
    every = "".join(f"<div><b>{code}</b>: " + ", ".join(f"{r:+.0f}%" for r, _ in results[code]) + "</div>" for code, *_ in specs)
    title = f"{STRATEGY_ID} STABILITY CHECK — {len(days)} days from ${START_NAV:,.0f}, {ROBUST_RUNS} coin orders"
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
  <div style="background:#0f172a;color:#fff;padding:14px;"><div style="font-size:15px;font-weight:700;">{title}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{days[0]} → {last} • {BENCH} buy &amp; hold {btc_ret:+.1f}%</div></div>
  <div style="padding:12px;font-size:12px;">
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin-bottom:12px;">
      📝 PAPER ONLY. Same coins, same candles, same rules — only the ORDER in which same-day signals are considered changes
      (run 1 = normal open-interest order, runs 2-{ROBUST_RUNS} = shuffled). A real edge keeps a good MEDIAN and a tolerable WORST case;
      a lucky one swings wildly.</div>
    <div style="overflow-x:auto;"><table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="4">
      <tr style="background:#f1f5f9;"><th align="left">Book</th><th align="left">Normal order</th><th align="left">Median</th><th align="left">Worst</th>
      <th align="left">Best</th><th align="left">Median drop</th><th align="left">Worst drop</th><th align="left">Beat {BENCH}</th><th align="left">Beat D (same order)</th></tr>
      {rows}</table></div>
    <div style="font-weight:700;margin-top:12px;">Every run</div><div style="font-size:11px;color:#475569;">{every}</div>
  </div></div></body></html>"""
    text = f"{title}\n" + "\n".join(summary)
    print(text, flush=True)
    send_email(f"🎲 {title} — " + " | ".join(summary), html, text, None)


def run_paper(info):
    if STOCKS:
        raise RuntimeError("Stocks: only backtest and robust modes are available so far (no stock paper books yet)")
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    newest_closed = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    manual = os.getenv("GITHUB_EVENT_NAME", "").lower() == "workflow_dispatch"
    if not manual and (state.get("last_processed_day") or "") >= newest_closed:
        print(f"Paper books already processed {state.get('last_processed_day')} — nothing new (no email)", flush=True)
        return                                                # the 5-minute trigger: quiet until the next daily candle
    books = state.get("books") or [new_book(code, name, lock, guard) for code, name, lock, guard in BOOKS]
    for code, name, lock, guard in BOOKS:                     # books added in later versions join with a fresh balance
        if code not in {b["code"] for b in books}:
            books.append(new_book(code, name, lock, guard))
    for b in books:                                           # settings changes (e.g. slot size) apply to saved books too
        b.update(BOOK_OPTIONS.get(b["code"], {}))
        b["name"] = next((n for c, n, _, _ in BOOKS if c == b["code"]), b["name"])
    last_done = state.get("last_processed_day")
    ranked, data = fetch_market(info, 60)
    btc_days = sorted(data.get("BTC", {}))
    if not btc_days:
        raise RuntimeError("No BTC daily candles returned — cannot run")
    latest = btc_days[-1]
    todo = [d for d in btc_days if (last_done is None and d == latest) or (last_done is not None and d > last_done)]
    if len(todo) > 7:
        todo = todo[-7:]                       # catch up at most a week after an outage
    day_logs, all_skips = [], []
    held = {c for b in books for c in b["positions"]}
    data4h = (fetch_intraday(info, [c for c in ranked[:DRIFT_OUT_RANK] if c in data or c in held], 10)
              if todo and needs_intraday(books) else {})
    for day in todo:
        btc_down = btc_downtrend(data, day)
        entries = []
        for book in books:
            log, skips = [], []
            step_day(book, day, data, ranked, btc_down, True, log, skips, data4h)
            entries += [(book["code"], k, c, m) for k, c, m in log]
            all_skips += [(f"[{book['code']}] {day} {c}", why) for c, why in skips]
        day_logs.append((day, entries))
    state.update({"books": books, "last_processed_day": todo[-1] if todo else last_done, "engine": ENGINE_VERSION,
                  "started": state.get("started") or (todo[0] if todo else latest)})
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)

    prices = {c: data[c][latest]["c"] for c in data if latest in data[c]}
    btc_down = btc_downtrend(data, latest)
    btc_bar = data["BTC"][latest]
    regime_line = (f"BTC {latest} close ${btc_bar['c']:,.0f} vs GC filter ${btc_bar['filter']:,.0f} → "
                   f"{'DOWNTREND (bear shorts allowed)' if btc_down else 'not in downtrend'}.")
    eq_days = sorted({d for d, _ in books[0]["equity"]})
    btc_curve = btc_buy_hold(data, eq_days) if len(eq_days) > 1 else []
    chart = make_chart(books, btc_curve, f"Paper since {state['started']}") if len(eq_days) > 1 else None
    title = f"{STRATEGY_ID} PAPER — {latest}"
    real = real_nav()
    summary = " | ".join(f"{b['code']} ${real * book_nav(b, prices) / book_start(b):,.2f} ({(book_nav(b, prices)/book_start(b)-1)*100:+.2f}%)" for b in books)
    html = render_email(title, f"Paper since {state['started']} • processed: {', '.join(todo) or 'nothing new'} • {summary}", books, day_logs,
                        all_skips, prices, btc_curve, regime_line, "gcchart" if chart else None, NOTES, growth_section(books, prices), real)
    text = f"{title}\n{summary}\n{regime_line}\n" + "\n".join(f"{d}: {e}" for d, e in day_logs)
    print(text, flush=True)
    send_email(f"📝 {title} — {summary}", html, text, chart)


def main():
    if STOCKS:                                                # Alpaca data only: no Hyperliquid connection needed
        if MODE == "backtest":
            return run_backtest(None)
        if MODE == "robust":
            return run_robust(None)
        return run_paper(None)
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    info = None
    for attempt in range(3):
        try:
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            break
        except Exception:
            time.sleep(10)
    if info is None:
        raise RuntimeError("Could not reach Hyperliquid")
    if MODE == "backtest":
        run_backtest(info)
    elif MODE == "robust":
        run_robust(info)
    else:
        run_paper(info)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"{STRATEGY_ID} paper engine error: {e}"
        print(msg, flush=True)
        try:
            send_email(f"⚠️ {msg}", f"<pre>{msg}</pre>", msg, None)
        except Exception:
            pass
        raise
