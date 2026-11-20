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
  Loss-stop ladder: Z1 / Z2 / Z4 = V + a -1% / -2% / -4% loss stop (crypto); F1 / F2 / F3 / F4 = F + -1..-4% (stocks)
  Falling-knife exits on top of Z4 (the live setup), checked on every 4-hour candle once a long has been +3%:
  Book KN1 = sell at the close of a red 4h candle on >= 2x the volume of the previous 6 candles that closes in its bottom 25%
  Book KN2 = more sensitive: >= 1.5x volume, closes in the bottom 35%
  Book KN3 = no volume check: a red 4h candle that falls >= 3% open-to-close and closes in its bottom 25%
  Book KN4 = only the extreme ones: >= 3x volume, closes in the bottom 25%
  Profit lock on SHORTS too (Z4 locks longs only):
  Book ZS  = Z4 with the early lock on shorts as well (keep 50% of the peak gain from +3% ... 90% from +60%)
  Book ZSY = Z4 with the lock from +1% on longs AND shorts (keep 50% of any gain from +1%)
  Volume-confirmed entries (skip a breakout / breakdown that comes on ordinary volume):
  Book ZV15 = Z4, but a new long or short needs the signal day's volume >= 1.5x its 20-day average
  Book ZV2  = Z4 with >= 2x volume.   Book ZSV = ZS (lock on shorts too) + the 1.5x volume filter
  Faster trailing (stop raised every HOUR instead of every 4 hours; Hyperliquid keeps ~200 days of 1h candles,
  so test these with backtest_days 200):
  Book Z4H  = Z4 with the lock raised every hour
  Book ZSYH = lock from +1% on longs and shorts, raised every hour
  Book ZSYF = ZSYH + VOLUME-FADE exit: in profit, and 6 hourly candles in a row on under half the volume of the
              move's busiest stretch without a new high (low for shorts) -> take the profit at that candle's close
  Book ZSF  = early lock (from +3%) on longs and shorts, hourly, + the same volume-fade exit
  Smarter exits on 4h candles (longs AND shorts, on top of the early lock and the -4% stop):
  Book RM   = RUNNER MODE: once a trade is +5% and the 4h trend line (EMA20) points its way, the tight lock is swapped
              for a wide stop 3x the 4h ATR behind the best price; the run ends on a 4h close back through the EMA20
  Book RMF  = RM + volume-fade exit (3 quiet 4h candles in a row, no new extreme, still in profit)
  Book SW   = STRUCTURE trail: from +3% the stop follows the lowest low (highest high for shorts) of the last 6 4h candles
  Book RS   = STRENGTH vs BTC: in profit and the coin lags BTC by 3%+ over the last 24 hours (leads it, for shorts) -> exit
  Book EX   = EXHAUSTION: after +10%, a 4h candle 2x its ATR wide with a 60%+ wick against the trade -> exit at its close
  Book ALL  = everything together: lock from +1% both sides, runner mode, volume fade, exhaustion, strength vs BTC and
              volume-confirmed entries (1.5x)
  Book ZSF4 = ZSF on 4h candles (so it can be replayed over 2 years): early lock on longs and shorts + volume-fade
              exit after 2 quiet 4h candles
  Long-side-only versions (shorts keep Z4's rules, so only the new exit differs from Z4):
  Book RML / RMFL / RSL / EXL = RM / RMF / RS / EX with the lock and the smart exit on LONGS only
  Book Z4F  = Z4 + the volume-fade exit on longs (2 quiet 4h candles)
  Book Z4HF = Z4H (lock raised every hour, longs) + the volume-fade exit on longs (6 quiet hourly candles)
  STOCK big-winner locks (Book F5 + a loose profit lock on longs; nothing changes below the first step):
  Book FL5  = keep 40% of the gain from +5%, 50% from +10%, 60% from +20%, 70% from +35%
  Book FL10 = keep 50% from +10%, 60% from +20%, 70% from +35%, 80% from +60%
  Book FL15 = keep 50% from +15%, 65% from +30%, 75% from +50%
  Book FLH  = FL10, with the lock checked on hourly bars instead of once a day
  The crypto setup on stocks (F5 + the early lock: keep 50% of the gain from +3% ... 90% from +60%):
  Book FZ   = checked once a day (already above).   Book FZH = checked on hourly bars
  Book FZHF = FZH + the volume-fade exit (6 quiet hourly bars after +3% -> take the profit)
  LOCK + RE-ENTRY (fixes the locks' weak spot: after a lock sells, the bot used to wait for a brand-new breakout, so
  a winner that dipped and kept climbing was missed). These books buy back when the stock CLOSES ABOVE ITS OLD PEAK
  while still above the upper band; the chance expires once a close falls under the band:
  Book FL10R = FL10 + re-entry.   Book FL5R = FL5 + re-entry.   Book FZR = FZ (crypto-style lock) + re-entry
  BEST OF BOTH (lock mode "adaptive"): longs always get the lock + volume fade (Option A). Shorts get the lock too
  (Option B) EXCEPT while BTC is in a STRONG downtrend, when shorts run free to catch the big slide (Option A).
  The regime is read from the PREVIOUS daily close (no peeking):
  Book AB1 / AB1H = strong downtrend = BTC down 10%+ over the last 30 days       (4h candles / hourly candles)
  Book AB2 / AB2H = strong downtrend = BTC closed 5%+ under its GC filter         (4h candles / hourly candles)
  Switch-point ladder (does the 5% line sit in a broad good zone?): AB2 with the line at 2/3/4/6/8% under the filter
  Book AB2_2 / AB2_3 / AB2_4 / AB2_6 / AB2_8 (4h, for 2 years) and AB2H_2 ... AB2H_8 (hourly, for 200 days)
  Book VB   = Z4 + V-BOTTOM longs: a coin 15%+ under its 10-day high makes a new 10-day low, then closes in the top 30%
              of the day's range, green, on 1.5x volume -> buy 15% (exits: lock / -4% stop; exempt from the band exit)

Every email also has a GROWTH CALCULATOR: each book's return so far, turned into average $ per day at $1k / $10k / $50k /
$200k and the capital that would have been needed for $200 and $400 a day.

MARKET (env MARKET): crypto (default, Hyperliquid) or stocks (Alpaca daily bars, ~100 large US stocks, SPY plays BTC's role
for the bear-short filter and the buy & hold comparison). Stocks: backtest and robust modes only for now. Accounts under
$2,000 at Alpaca cannot short or use margin, so LONG_ONLY=auto makes stock backtests under $2,000 long-only at 1x.

Modes (env MODE):
  ross     the old 15-minute Ross scalper (V4.8.12 rules) replayed on 5m / 15m / 30m / 1h / 4h candles (env ROSS_TIMEFRAMES)
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
STOCKS = MARKET in ("stocks", "stocks_wide")
# 1.53 stocks_wide: the universe is picked by rule from every active Alpaca US stock (not a hand list): price >= $5,
# average daily dollar volume >= $5M, top WIDE_SIZE by dollar volume. ETFs / funds are skipped by name.
WIDE = MARKET == "stocks_wide"
ALL_COINS = MARKET == "crypto_all"     # every Hyperliquid perp with open interest, not just the top 100 (backtest / robust)
WIDE_SIZE = int(os.getenv("WIDE_SIZE", "1000") or 1000)
TOP_OI = set()                         # crypto: today's top 100 by open interest (C100), fixed before any shuffle
LARGE_CAPS = set()                     # the top 100 of the wide universe by dollar volume (filled when data loads)
STRATEGY_ID = "TR-GC-Equities-LS-15" if STOCKS else "TR-GC-Crypto-LS-15"
BENCH = "SPY" if STOCKS else "BTC"      # market filter for bear shorts + buy & hold comparison
ENGINE_VERSION = "GC-PAPER-1.77"
MODE = os.getenv("MODE", "paper").strip().lower()
# "730" = the last 730 days. "730-365" = from 730 days ago up to 365 days ago (a hold-out check on the older year).
_BD = (os.getenv("BACKTEST_DAYS", "365") or "365").replace(" ", "")
BACKTEST_DAYS = int(_BD.split("-")[0])
BACKTEST_SKIP_DAYS = int(_BD.split("-")[1]) if "-" in _BD else 0
# 1.46 funding: backtest / robust runs (crypto) charge Hyperliquid funding every day a position is held. The rate is a
# market average from FUNDING_COINS coins spread across the universe (hourly fundingHistory, ~2.4 s per request).
FUNDING = os.getenv("FUNDING", "on").strip().lower() != "off"
FUNDING_COINS = int(os.getenv("FUNDING_COINS", "12") or 12)
FUNDING_DAY = {}                       # day -> average hourly funding rate (positive = longs pay shorts)
FUNDING_NOTE = ""
STATE_FILE = "gc_paper_state.json"
START_NAV = float(os.getenv("START_NAV", "1000") or 1000)   # starting balance of each NEW book (Run workflow option)
ROBUST_RUNS = int(os.getenv("ROBUST_RUNS", "20") or 20)
ROBUST_BOOKS = [c.strip().upper() for c in os.getenv("ROBUST_BOOKS", "V,Y,Z,Z3,YZ").split(",") if c.strip()]
# Daily paper email + plain backtest only run the LIVE rules (crypto F25L5 = bot 1.10.2, stocks F5 = V6.2.2). Every other book stays defined in this
# file and can still be tested any time with mode=robust and its code in robust_books.
PAPER_BOOKS = [c.strip().upper() for c in os.getenv("PAPER_BOOKS", "").split(",") if c.strip()] or (
    ["F5"] if MARKET.startswith("stocks") else ["F25L5"])
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
UNIVERSE_SIZE = WIDE_SIZE if (WIDE or ALL_COINS) else 100   # TRSORTORDER: first 100 by rank (stocks_wide: the whole picked list)
DRIFT_OUT_RANK = 10 ** 4 if ALL_COINS else 120                   # paper mode: exit when rank falls below 120 (hysteresis so rank #99/#101 doesn't churn)
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
    ("Z1", "V + -1% Loss Stop", "long", False),
    ("Z2", "V + -2% Loss Stop", "long", False),
    ("Z4", "V + -4% Loss Stop", "long", False),
    ("Z35", "V + -3.5% Loss Stop", "long", False),
    ("Z45", "V + -4.5% Loss Stop", "long", False),
    ("Z6", "V + -6% Loss Stop", "long", False),
    ("Z7", "V + -7% Loss Stop", "long", False),
    ("F1", "F + -1% Loss Stop", False, False),
    ("F2", "F + -2% Loss Stop", False, False),
    ("F3", "F + -3% Loss Stop", False, False),
    ("F4", "F + -4% Loss Stop", False, False),
    ("F45", "F + -4.5% Loss Stop", False, False),
    ("F55", "F + -5.5% Loss Stop", False, False),
    ("F6", "F + -6% Loss Stop", False, False),
    ("F7", "F + -7% Loss Stop", False, False),
    ("KN1", "Z4 + Knife Exit (4h red, 2x volume)", "long", False),
    ("KN2", "Z4 + Knife Exit (4h red, 1.5x volume)", "long", False),
    ("KN3", "Z4 + Knife Exit (4h drop >= 3%)", "long", False),
    ("KN4", "Z4 + Knife Exit (4h red, 3x volume)", "long", False),
    ("ZS", "Z4 + Early Lock on Shorts too", "all", False),
    ("ZSY", "Z4 + Lock from +1% on Longs and Shorts", "all", False),
    ("ZV15", "Z4 + Volume-Confirmed Entries (1.5x)", "long", False),
    ("ZV2", "Z4 + Volume-Confirmed Entries (2x)", "long", False),
    ("ZSV", "ZS + Volume-Confirmed Entries (1.5x)", "all", False),
    ("Z4H", "Z4 + Lock Raised Every Hour", "long", False),
    ("ZSYH", "Lock from +1% Both Sides, Every Hour", "all", False),
    ("ZSYF", "ZSYH + Volume-Fade Exit", "all", False),
    ("ZSF", "Early Lock Both Sides, Every Hour + Volume-Fade Exit", "all", False),
    ("RM", "Runner Mode (wide ATR stop while trending)", "all", False),
    ("RMF", "Runner Mode + Volume-Fade Exit", "all", False),
    ("SW", "Structure Trail (last 6 4h swing low/high)", "all", False),
    ("RS", "Exit When Lagging BTC", "all", False),
    ("EX", "Exhaustion-Candle Exit", "all", False),
    ("ALL", "All Smart Exits + Volume Entries", "all", False),
    ("VB", "Z4 + V-Bottom Longs", "long", False),
    ("ZSF4", "Early Lock Both Sides + Volume-Fade Exit (4h)", "all", False),
    ("RML", "Z4 + Runner Mode (longs only)", "long", False),
    ("RMFL", "Z4 + Runner Mode + Volume Fade (longs only)", "long", False),
    ("RSL", "Z4 + Exit When Lagging BTC (longs only)", "long", False),
    ("EXL", "Z4 + Exhaustion-Candle Exit (longs only)", "long", False),
    ("Z4F", "Z4 + Volume-Fade Exit (longs only)", "long", False),
    ("Z4HF", "Z4 + Hourly Lock + Volume Fade (longs only)", "long", False),
    ("AB1", "Best of Both: shorts run free when BTC -10%/30d (4h)", "adaptive", False),
    ("AB2", "Best of Both: shorts run free when BTC 5% under filter (4h)", "adaptive", False),
    ("AB1H", "Best of Both: shorts run free when BTC -10%/30d (hourly)", "adaptive", False),
    ("AB2H", "Best of Both: shorts run free when BTC 5% under filter (hourly)", "adaptive", False),
    ("AB2HF", "AB2H + big-winner floor on free shorts (keep 60-80% from +15%)", "adaptive", False),
    ("AB2HF10", "AB2H + floor on free shorts from +10% (keep 50-80%)", "adaptive", False),
    ("AB2HF5", "AB2H + floor on free shorts from +5% (keep 50-80%)", "adaptive", False),
    ("AB2HFV", "AB2HF + volume-fade exit on free shorts too", "adaptive", False),
    ("AB2HT20", "AB2H + bank HALF of a free short at +20%, the rest runs free", "adaptive", False),
    ("AB2HT15", "AB2H + bank HALF of a free short at +15%, the rest runs free", "adaptive", False),
    ("AB2HS", "AB2H + squeeze guard: shorts locked again once BTC bounces 8% off its 10-day low", "adaptive", False),
    ("AB2HS5", "AB2H + squeeze guard: shorts locked again once BTC bounces 5% off its 10-day low", "adaptive", False),
    ("AB2HF25", "AB2H + floor on free shorts only from +25% (keep 60-80%) — LIVE 1.8.1", "adaptive", False),
    ("F25T", "AB2HF25 + bank half of a free short at +20%", "adaptive", False),
    ("F25R", "AB2HF25 + re-short after a lock exit when the coin makes a new low", "adaptive", False),
    ("F25N", "AB2HF25 + no new longs while BTC is in a strong downtrend", "adaptive", False),
    ("F25W", "AB2HF25 + 12 slots / 180% in use while BTC is in a strong downtrend", "adaptive", False),
    ("F25P", "AB2HF25 + add 7.5% to a free short once it is +10%", "adaptive", False),
    ("F25E", "F25T + crowded days: cleanest trends (10-day efficiency) get the slots first", "adaptive", False),
    ("F25V", "F25T + size by volatility (7.5-22.5% per slot, same risk per trade)", "adaptive", False),
    ("F25X", "F25T + guardrail on free shorts: keep 35% from +18%", "adaptive", False),
    ("F25L5", "LIVE RULES (bot 1.10.2): F25T + bank half of a LONG at +5%", "adaptive", False),
    ("F25L10", "F25T + bank half of a LONG at +10%", "adaptive", False),
    ("F25L20", "F25T + bank half of a LONG at +20%", "adaptive", False),
    ("F25T5", "F25T + bank a THIRD of a LONG at +5% (two thirds keep running)", "adaptive", False),
    ("F25B15", "F25L5, but while BTC closes 5%+ ABOVE its filter the long half-bank waits until +15%", "adaptive", False),
    ("F25B20", "F25L5, but while BTC closes 5%+ ABOVE its filter the long half-bank waits until +20%", "adaptive", False),
    ("F25VT", "F25L5 + volatility target: new slots shrink (down to 55%) when BTC's 10-day volatility runs above its 60-day norm", "adaptive", False),
    ("F25S10", "F25L5, but a free-running short banks HALF at +10% (not +20%)", "adaptive", False),
    ("F25S15", "F25L5, but a free-running short banks HALF at +15% (not +20%)", "adaptive", False),
    ("F25C15", "F25L5 + volatility trail on free shorts: from +10% the stop trails the low by 1.5 x the coin's daily ATR, never worse than breakeven", "adaptive", False),
    ("F25C20", "F25L5 + volatility trail on free shorts: from +10% the stop trails the low by 2.0 x the coin's daily ATR, never worse than breakeven", "adaptive", False),
    ("F25C30", "F25L5 + volatility trail on free shorts: from +10% the stop trails the low by 3.0 x the coin's daily ATR, never worse than breakeven", "adaptive", False),
    ("F25D1", "F25L5, but a free short banks a QUARTER at +8% and another quarter at +15% (half keeps running)", "adaptive", False),
    ("F25D2", "F25L5, but a free short banks a FIFTH at +5%, +10% and +15% (40% keeps running)", "adaptive", False),
    ("F25DM5", "F25L5 normally; while BTC is 5%+ off its 10-day low (a bounce) free shorts use the ladder (quarter at +8%, quarter at +15%)", "adaptive", False),
    ("F25DM8", "F25L5 normally; while BTC is 8%+ off its 10-day low (a bounce) free shorts use the ladder (quarter at +8%, quarter at +15%)", "adaptive", False),
    ("F25DR", "F25L5 normally; free shorts on the SMALLER coins (open-interest rank 51-100 at entry) use the ladder (quarter at +8%, quarter at +15%)", "adaptive", False),
    ("F25DV", "F25L5 normally; free shorts on the MORE VOLATILE coins (daily ATR % above that day's median at entry) use the ladder", "adaptive", False),
    ("F25DRB", "F25L5 normally; the ladder only for SMALLER coins (rank 51-100) AND only while BTC is 8%+ off its 10-day low", "adaptive", False),
    ("F25RE0", "F25L5 + re-short: after a free short is stopped out by its lock, short the coin again when it closes below that low (control)", "adaptive", False),
    ("F25RE1", "F25RE0 + a TIGHTER floor on free shorts (keeps 50% from +10%, 60% from +15%, 70% from +20%, 75% from +25%)", "adaptive", False),
    ("F25RE2", "F25RE0 + a medium floor on free shorts (keeps 60% from +15%, 70% from +25%, 75% from +35%)", "adaptive", False),
    ("F25TD", "F25L5 + time-decay ratchet: from +25% the free-short floor keeps +2.5% more of the peak for every 6 hours without a new low (cap 85%)", "adaptive", False),
    ("F25G7", "F25L5 + max-giveback leash: from +20% a free short never gives back more than 7 points of gain (or keeps 60% of the peak, whichever is higher)", "adaptive", False),
    ("F25BR", "F25L5 + BTC relief guard: while BTC's last close is above the previous day's high, free shorts up +10% or more keep 80% of the peak", "adaptive", False),
    ("F25BE10", "F25L5 + break-even step: once a free short has been +10% its stop moves from -4% to +0.5% (it can no longer turn into a loss)", "adaptive", False),
    ("F25BE12", "F25L5 + break-even step: once a free short has been +12% its stop moves from -4% to +0.5%", "adaptive", False),
    ("F25DN15", "F25L5 + wide net: from a +15% peak up to +25% a free short's stop trails 14 points behind the peak (then the normal 60-80% floor)", "adaptive", False),
    ("F25BE14", "F25L5 + break-even step: once a free short has been +14% its stop moves from -4% to +0.5%", "adaptive", False),
    ("F25BE15", "F25L5 + break-even step: once a free short has been +15% its stop moves from -4% to +0.5%", "adaptive", False),
    ("F25SL10", "F25BE15 + ramp: the free-short stop slides from -4% towards +0.5% as the peak climbs, reaching +0.5% at a +10.5% peak (10-point leash)", "adaptive", False),
    ("F25SL12", "F25BE15 + ramp: the free-short stop slides from -4% towards +0.5% as the peak climbs, reaching +0.5% at a +12.5% peak (12-point leash)", "adaptive", False),
    ("K70", "LIVE 1.10 rules, free-short floor from +25% keeps 70-85%", "adaptive", False),
    ("K80", "LIVE 1.10 rules, free-short floor from +25% keeps 80-90%", "adaptive", False),
    ("K90", "LIVE 1.10 rules, free-short floor from +25% keeps 90%", "adaptive", False),
    ("G1", "LIVE 1.10 + stall exit: no new peak for 6 x 4h after +6%, still >=68% of peak", "adaptive", False),
    ("G2", "LIVE 1.10 + shelf stop: quiet 4h bar after +5% -> stop just past the 2-bar shelf", "adaptive", False),
    ("G3", "LIVE 1.10 + churn exit: 6 x 4h efficiency < 0.18 after +6%, close on the weak side", "adaptive", False),
    ("G4", "LIVE 1.10 + rejection exit: 2 x 4h closes back past the peak candle's middle after +5%", "adaptive", False),
    ("CB", "LIVE 1.10 rules on Coinbase US perps only (17 coins)", "adaptive", False),
    ("CB5", "CB with 5 slots x 25% (fewer, bigger positions)", "adaptive", False),
    ("CBM", "Coinbase mix: longs on all coins (spot), shorts only on the 17 US perps", "adaptive", False),
    ("CBL", "Coinbase spot only: longs on all coins, no shorts", "adaptive", False),
    ("F5L5", "F5 + bank half at +5%, the rest rides the GC band (stocks)", False, False),
    ("F5L10", "F5 + bank half at +10%, the rest rides the GC band (stocks)", False, False),
    ("F5LF7", "F5L5, but only for FAST pops: bank half at +5% if reached within 7 days of the buy, else let it run (stocks)", False, False),
    ("F5LF14", "F5L5, but only for FAST pops: bank half at +5% if reached within 14 days of the buy, else let it run (stocks)", False, False),
    ("F5V", "F5 + volume filter: a breakout needs the day's volume >= 1.5x its 20-day average (stocks)", False, False),
    ("F5W", "F5 + sellers-took-over exit: once +3%, sell on a red day with 1.5x+ volume that closes near its low (stocks)", False, False),
    ("F5LV", "F5L5, but half is only banked at +5% on a heavy-volume day (>= 1.5x average): blow-off volume = take it, else let it run (stocks)", False, False),
    ("F5LM", "F5L5, but half is only banked at +5% while SPY's last close is under its GC filter (weak / choppy market); in a strong market it runs full size (stocks)", False, False),
    ("F5M", "F5 + buy only when SPY is above its filter (stocks)", False, False),
    ("F5A", "F5 + add to +10% winners up to 6.6% (stocks)", False, False),
    ("F5X", "F5 without SPY / QQQ as tradable stocks", False, False),
    ("WALL", "F5 rules on the WIDE stock list (up to 1000 stocks)", False, False),
    ("WMID", "F5 rules on mid-size stocks only (wide list minus its top 100)", False, False),
    ("WTOP", "F5 rules on today's top 100 of the wide list (hindsight pick)", False, False),
    ("WDAY", "F5 rules on the top 100 by dollar volume AS OF EACH DAY (no hindsight)", False, False),
    ("C100", "LIVE RULES on today's top 100 coins by open interest (what we test now)", "adaptive", False),
    ("CALL", "LIVE RULES on EVERY Hyperliquid coin", "adaptive", False),
    ("CDAY", "LIVE RULES on the top 100 coins by dollar volume AS OF EACH DAY (no hindsight)", "adaptive", False),
    ("CALLV", "LIVE RULES on every coin trading $1M+/day (skips coins too thin to fill well)", "adaptive", False),
    ("CALLS", "LIVE RULES on every coin, small coins pay 0.5% extra slippage (honest CALL)", "adaptive", False),
    ("CS1", "Small coins: half size, max 2, lock 60-92% from +3%, bank half at +10%", "adaptive", False),
    ("CS2", "Small coins: half size, max 2, lock 55-85% from +5%, bank half at +20% (let flyers fly)", "adaptive", False),
    ("CS3", "Small coins: FULL size, max 3, lock 60-92% from +3%, bank half at +10%", "adaptive", False),
    ("FL5", "F5 + Loose Lock from +5% (stocks)", "long", False),
    ("FL10", "F5 + Big-Winner Lock from +10% (stocks)", "long", False),
    ("FL15", "F5 + Big-Winner Lock from +15% (stocks)", "long", False),
    ("FLH", "FL10 checked hourly (stocks)", "long", False),
    ("FZH", "F5 + Early Lock checked hourly (stocks)", "long", False),
    ("FZHF", "F5 + Early Lock hourly + Volume Fade (stocks)", "long", False),
    ("FL10R", "FL10 + Re-entry on a new high (stocks)", "long", False),
    ("FL5R", "FL5 + Re-entry on a new high (stocks)", "long", False),
    ("FZR", "FZ (early lock) + Re-entry on a new high (stocks)", "long", False),
    ("FLM", "FL10 + buy only when SPY is above its filter (stocks)", "long", False),
    ("FLV", "FL10 + buy only on 1.5x volume breakouts (stocks)", "long", False),
    ("FLT", "FL10 + sell if still red after 3 days (stocks)", "long", False),
    ("FL4", "FL10 with a -4% loss stop (stocks)", "long", False),
    ("FL3", "FL10 with a -3% loss stop (stocks)", "long", False),
    ("FLX", "FL10 + SPY filter + volume + 3-day rule (stocks)", "long", False),
    ("FL2", "FL10 with a -2% loss stop (stocks)", "long", False),
    ("FLS", "FL10 + Smart Cut: sell at -1.5% on a heavy-volume red day (stocks)", "long", False),
    *[(f"AB2{h}_{n}", f"Best of Both, shorts free when BTC {n}% under filter ({'hourly' if h else '4h'})", "adaptive", False)
      for h in ("", "H") for n in (2, 3, 4, 6, 8)],
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
    **{f"Z{n}": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": n / 100,
                 "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]} for n in (1, 2, 4, 6, 7)},
    **{code: {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": d,
              "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]}
       for code, d in (("Z35", 0.035), ("Z45", 0.045))},
    **{f"F{n}": {"size_mult": 0.60, "disaster": n / 100} for n in (1, 2, 3, 4, 6, 7)},
    "F45": {"size_mult": 0.60, "disaster": 0.045},
    "F55": {"size_mult": 0.60, "disaster": 0.055},
    "FZ": {"size_mult": 0.60, "disaster": 0.05,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "YZ": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.05,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50], [0.01, 0.50]]},
    # knife: on top of Z4, sell a long at the close of a 4h candle that looks like a falling knife (see knife_hit).
    **{code: {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04,
              "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]],
              "knife": {"after_gain": 0.03, "vol_mult": vm, "close_in_bottom": cb, "drop": dr}}
       for code, vm, cb, dr in (("KN1", 2.0, 0.25, None), ("KN2", 1.5, 0.35, None), ("KN3", None, 0.25, 0.03), ("KN4", 3.0, 0.25, None))},
    # ZS / ZSY: the book's lock mode is "all", so the same ladder also protects shorts (4h candles, stop above the low).
    "ZS": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04,
           "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]},
    "ZSY": {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04,
            "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50], [0.01, 0.50]]},
    # vol_filter: a new entry (long or short) needs the signal day's volume >= vol_filter x its 20-day average.
    **{code: {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04, "vol_filter": vf,
              "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]]}
       for code, vf in (("ZV15", 1.5), ("ZV2", 2.0), ("ZSV", 1.5))},
    # lock_interval "1h": the lock walks hourly candles. fade: see fade_out().
    **{code: {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04, "lock_interval": "1h",
              "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]] + ([[0.01, 0.50]] if early else []),
              **({"fade": {"after_gain": 0.01 if early else 0.03, "bars": 6, "vol_ratio": 0.5}} if fade else {})}
       for code, early, fade in (("Z4H", False, False), ("ZSYH", True, False), ("ZSYF", True, True), ("ZSF", False, True))},
    # Smart-exit books (4h candles). runner / swing / rs / exhaust: see smart_exits(). vbottom: see vbottom_signal().
    **{code: {"max_pos": 9, "slot_size": 0.15, "gross_cap": 1.35, "lock_intraday": True, "disaster": 0.04,
              "lock_tiers": [[0.60, 0.90], [0.35, 0.85], [0.20, 0.80], [0.10, 0.70], [0.05, 0.60], [0.03, 0.50]] + ([[0.01, 0.50]] if code == "ALL" else []),
              **extra}
       for code, extra in (
           ("RM", {"runner": {"after_gain": 0.05, "atr_mult": 3.0}}),
           ("RMF", {"runner": {"after_gain": 0.05, "atr_mult": 3.0}, "fade": {"after_gain": 0.03, "bars": 3, "vol_ratio": 0.5}}),
           ("SW", {"swing": {"after_gain": 0.03}}),
           ("RS", {"rs": {"after_gain": 0.02, "lag": 0.03}}),
           ("EX", {"exhaust": {"after_gain": 0.10, "range_atr": 2.0, "wick": 0.6}}),
           ("ALL", {"runner": {"after_gain": 0.05, "atr_mult": 3.0}, "fade": {"after_gain": 0.01, "bars": 3, "vol_ratio": 0.5},
                    "rs": {"after_gain": 0.02, "lag": 0.03}, "exhaust": {"after_gain": 0.10, "range_atr": 2.0, "wick": 0.6},
                    "vol_filter": 1.5}),
           ("VB", {"vbottom": {"drop": 0.15, "close_top": 0.30, "vol": 1.5}}),
           ("ZSF4", {"fade": {"after_gain": 0.03, "bars": 2, "vol_ratio": 0.5}}))},
}
# The long-only versions share the settings; the "long" lock mode in BOOKS keeps shorts on Z4's rules.
BOOK_OPTIONS.update({**{c: {**BOOK_OPTIONS["ZSF4"], "regime": rg} for c, rg in (("AB1", "r30"), ("AB2", "under_filter"))},
                     **{c: {**BOOK_OPTIONS["ZSF"], "regime": rg} for c, rg in (("AB1H", "r30"), ("AB2H", "under_filter"))}})
_FL_TIERS = {"FL5": [[0.35, 0.70], [0.20, 0.60], [0.10, 0.50], [0.05, 0.40]],
             "FL10": [[0.60, 0.80], [0.35, 0.70], [0.20, 0.60], [0.10, 0.50]],
             "FL15": [[0.50, 0.75], [0.30, 0.65], [0.15, 0.50]]}
BOOK_OPTIONS.update({c: {"size_mult": 0.60, "disaster": 0.05, "lock_tiers": t} for c, t in _FL_TIERS.items()})
BOOK_OPTIONS["FLH"] = {**BOOK_OPTIONS["FL10"], "lock_intraday": True}
BOOK_OPTIONS.update({c + "R": {**BOOK_OPTIONS[c], "reenter": True} for c in ("FL10", "FL5", "FZ")})
BOOK_OPTIONS.update({f"AB2{h}_{n}": {**BOOK_OPTIONS["AB2" + h], "regime_pct": n / 100} for h in ("", "H") for n in (2, 3, 4, 6, 8)})
BOOK_OPTIONS["FZH"] = {**BOOK_OPTIONS["FZ"], "lock_intraday": True}
BOOK_OPTIONS["FZHF"] = {**BOOK_OPTIONS["FZH"], "fade": {"after_gain": 0.03, "bars": 6, "vol_ratio": 0.5}}
# 1.39 smarter-loser tests on top of FL10. mkt_filter: new longs only while the benchmark (SPY) closes above its GC filter.
# time_stop: sell a long still below its entry after N trading days (no follow-through).
BOOK_OPTIONS.update({
    "FLM": {**BOOK_OPTIONS["FL10"], "mkt_filter": True},
    "FLV": {**BOOK_OPTIONS["FL10"], "vol_filter": 1.5},
    "FLT": {**BOOK_OPTIONS["FL10"], "time_stop": 3},
    "FL4": {**BOOK_OPTIONS["FL10"], "disaster": 0.04},
    "FL3": {**BOOK_OPTIONS["FL10"], "disaster": 0.03},
    "FLX": {**BOOK_OPTIONS["FL10"], "mkt_filter": True, "vol_filter": 1.5, "time_stop": 3},
    "FL2": {**BOOK_OPTIONS["FL10"], "disaster": 0.02},
    # smart_cut: cut a long early (at the daily close) only when it is down `loss` AND sellers clearly control the day.
    "FLS": {**BOOK_OPTIONS["FL10"], "smart_cut": {"loss": 0.015, "vol_mult": 1.2, "close_in_bottom": 0.4}},
})
# free_tiers: while shorts "run free" (adaptive books, strong BTC downtrend) big winners still get this wide floor.
# 1.44: the live bot uses the +25% ladder (AB2HF25 beat plain AB2H in 95 of 100 runs; the +15% ladder lost to it).
FREE_TIERS = [[0.50, 0.80], [0.35, 0.70], [0.25, 0.60]]
BOOK_OPTIONS["AB2HF"] = {**BOOK_OPTIONS["AB2H"], "free_tiers": [[0.50, 0.80], [0.30, 0.75], [0.20, 0.70], [0.15, 0.60]]}
BOOK_OPTIONS["AB2HF10"] = {**BOOK_OPTIONS["AB2H"], "free_tiers": [[0.50, 0.80], [0.30, 0.75], [0.20, 0.70], [0.10, 0.50]]}
BOOK_OPTIONS["AB2HFV"] = {**BOOK_OPTIONS["AB2HF"], "free_fade": True}
# free_half: bank `frac` of a free-running short once it is up `at`; the rest keeps running free (-4% stop only).
# squeeze: the strong-downtrend switch turns OFF (shorts locked again) once BTC's close is `squeeze` above its 10-day low close.
BOOK_OPTIONS.update({
    "AB2HT20": {**BOOK_OPTIONS["AB2H"], "free_half": {"at": 0.20, "frac": 0.5}},
    "AB2HT15": {**BOOK_OPTIONS["AB2H"], "free_half": {"at": 0.15, "frac": 0.5}},
    "AB2HS": {**BOOK_OPTIONS["AB2H"], "squeeze": 0.08},
    "AB2HS5": {**BOOK_OPTIONS["AB2H"], "squeeze": 0.05},
    "AB2HF25": {**BOOK_OPTIONS["AB2H"], "free_tiers": FREE_TIERS},
})
# 1.45 ideas on top of AB2HF25 (see step_day): reenter_short, no_long_crash, crash_slots, short_add.
BOOK_OPTIONS.update({
    "F25T": {**BOOK_OPTIONS["AB2HF25"], "free_half": {"at": 0.20, "frac": 0.5}},
    "F25R": {**BOOK_OPTIONS["AB2HF25"], "reenter_short": True},
    "F25N": {**BOOK_OPTIONS["AB2HF25"], "no_long_crash": True},
    "F25W": {**BOOK_OPTIONS["AB2HF25"], "crash_slots": {"max_pos": 12, "gross_cap": 1.80}},
    "F25P": {**BOOK_OPTIONS["AB2HF25"], "short_add": {"at": 0.10, "size": 0.075}},
})
# 1.46 (Gemini's ideas) on top of F25T, the best book of 1.45: er_rank, vol_size, a low-keep guardrail.
BOOK_OPTIONS.update({
    "F25E": {**BOOK_OPTIONS["F25T"], "er_rank": True},
    "F25V": {**BOOK_OPTIONS["F25T"], "vol_size": {"risk": 0.006, "atr_mult": 0.8, "min": 0.075, "max": 0.225}},
    "F25X": {**BOOK_OPTIONS["F25T"], "free_tiers": FREE_TIERS + [[0.18, 0.35]]},
})
# 1.47: bank half of a long (Book X's take_half) on top of F25T; the rest keeps the normal hourly lock.
BOOK_OPTIONS.update({f"F25L{n}": {**BOOK_OPTIONS["F25T"], "take_half": {"at": n / 100, "frac": 0.5}} for n in (5, 10, 20)})
# 1.63 (tests of two ideas, not live): F25T5 banks a third instead of half; F25B15 / F25B20 keep the +5% half-bank except in a strong BTC UPtrend
# (BTC closed 5%+ above its filter on the previous day), where longs run full size until +15% / +20%.
BOOK_OPTIONS["F25T5"] = {**BOOK_OPTIONS["F25T"], "take_half": {"at": 0.05, "frac": 1 / 3}}
BOOK_OPTIONS.update({f"F25B{n}": {**BOOK_OPTIONS["F25L5"], "bull_half": {"pct": 0.05, "at": n / 100}} for n in (15, 20)})
# 1.64 (test, not live): market volatility target. Slot size x clamp(60-day BTC volatility / 10-day BTC volatility, floor, 1). Only ever smaller than normal.
BOOK_OPTIONS["F25VT"] = {**BOOK_OPTIONS["F25L5"], "vol_target": {"short": 10, "long": 60, "floor": 0.55}}
# 1.69 (tests, not live): protect free-running shorts earlier. S = bank half at +10% / +15%. C = a volatility (ATR) trailing stop for free shorts:
# once the short is up `start`, the stop trails the lowest price by k x the coin's own daily ATR (a calm coin gets a tight stop, a wild one a
# loose stop, so ordinary bounces do not trigger it) and is never worse than breakeven. It works together with the +25% floor (the tighter one wins).
BOOK_OPTIONS.update({f"F25S{n}": {**BOOK_OPTIONS["F25L5"], "free_half": {"at": n / 100, "frac": 0.5}} for n in (10, 15)})
BOOK_OPTIONS.update({f"F25C{int(k * 10)}": {**BOOK_OPTIONS["F25L5"], "free_trail": {"start": 0.10, "k": k}} for k in (1.5, 2.0, 3.0)})
# 1.70 (tests, not live): a LADDER for free-running shorts. Each rung banks `frac0` of the ORIGINAL position when the short is up `at`; the rest keeps
# running free with the +25% floor. Replaces the single +20% bank-half (free_half is switched off for these books).
BOOK_OPTIONS["F25D1"] = {**BOOK_OPTIONS["F25L5"], "free_half": None, "free_ladder": [{"at": 0.08, "frac0": 0.25}, {"at": 0.15, "frac0": 0.25}]}
# 1.71: the two books work as a team. Normal days = F25L5 (bank half at +20%). While BTC bounces (closed `ladder_when_bounce` above its 10-day low
# as of the previous day) the ladder rungs of F25D1 are switched on; once a rung has fired the single +20% half-bank is skipped for that position.
BOOK_OPTIONS.update({f"F25DM{n}": {**BOOK_OPTIONS["F25L5"], "free_ladder": BOOK_OPTIONS["F25D1"]["free_ladder"], "ladder_when_bounce": n / 100}
                     for n in (5, 8)})
# 1.72: team books with a COIN condition. The trait is measured when the position is opened (rank by open interest, or the coin's daily ATR % against the
# median of all coins that day), never picked by name.
BOOK_OPTIONS["F25DR"] = {**BOOK_OPTIONS["F25L5"], "free_ladder": BOOK_OPTIONS["F25D1"]["free_ladder"], "ladder_rank_min": 51}
BOOK_OPTIONS["F25DV"] = {**BOOK_OPTIONS["F25L5"], "free_ladder": BOOK_OPTIONS["F25D1"]["free_ladder"], "ladder_hivol": True}
BOOK_OPTIONS["F25DRB"] = {**BOOK_OPTIONS["F25DR"], "ladder_when_bounce": 0.08}
# 1.73: protect free shorts EARLIER but win the continuation back: when the lock stops a free short out, the coin may be shorted again as soon as it closes
# below that low (the re-entry mechanism already in the engine, `reenter_short`). RE0 = re-entry only; RE1 / RE2 add tighter floors (tiers are best-first).
BOOK_OPTIONS["F25RE0"] = {**BOOK_OPTIONS["F25L5"], "reenter_short": True}
# 1.74: three conditional tighteners for free shorts (ideas from Gemini). TD: the floor tightens while the coin goes sideways and loosens again on a new low.
# G7: a cap on the giveback in points. BR: tighter floor while BTC bounces (daily proxy: BTC's last close above the previous day's high).
# 1.75 (ideas from Gemini): close the gap between the -4% stop and the +25% floor. BE = a one-time break-even step; DN = a wide trailing net (peak - 14 points).
BOOK_OPTIONS["F25BE10"] = {**BOOK_OPTIONS["F25L5"], "free_be": {"start": 0.10, "lock": 0.005}}
BOOK_OPTIONS["F25BE12"] = {**BOOK_OPTIONS["F25L5"], "free_be": {"start": 0.12, "lock": 0.005}}
BOOK_OPTIONS["F25DN15"] = {**BOOK_OPTIONS["F25L5"], "free_net": {"start": 0.15, "cap": 0.14}}
BOOK_OPTIONS.update({f"F25BE{n}": {**BOOK_OPTIONS["F25L5"], "free_be": {"start": n / 100, "lock": 0.005}} for n in (14, 15)})
# 1.77: protect the SMALLER winners too. Ramp: lock = peak gain - lead (never above +0.5%), so a short that peaked at +8% is stopped at about -2% instead of -4%.
BOOK_OPTIONS.update({f"F25SL{n}": {**BOOK_OPTIONS["F25BE15"], "free_slope": {"lead": n / 100}} for n in (10, 12)})   # 1.76: neighbours of BE12
BOOK_OPTIONS["F25TD"] = {**BOOK_OPTIONS["F25L5"], "free_decay": {"start": 0.25, "step": 0.025, "every": 6, "cap": 0.85}}
BOOK_OPTIONS["F25G7"] = {**BOOK_OPTIONS["F25L5"], "free_giveback": {"start": 0.20, "cap": 0.07, "keep": 0.60}}
BOOK_OPTIONS["F25BR"] = {**BOOK_OPTIONS["F25L5"], "relief_guard": {"start": 0.10, "keep": 0.80}}
BOOK_OPTIONS["F25RE1"] = {**BOOK_OPTIONS["F25RE0"], "free_tiers": [[0.50, 0.85], [0.35, 0.80], [0.25, 0.75], [0.20, 0.70], [0.15, 0.60], [0.10, 0.50]]}
BOOK_OPTIONS["F25RE2"] = {**BOOK_OPTIONS["F25RE0"], "free_tiers": [[0.50, 0.85], [0.35, 0.75], [0.25, 0.70], [0.15, 0.60]]}
BOOK_OPTIONS["F25D2"] = {**BOOK_OPTIONS["F25L5"], "free_half": None,
                         "free_ladder": [{"at": 0.05, "frac0": 0.20}, {"at": 0.10, "frac0": 0.20}, {"at": 0.15, "frac0": 0.20}]}
# 1.48: same start (+25%) for the free-short floor, but it keeps MORE of the peak (live = F25L5 = 60/70/80%).
BOOK_OPTIONS.update({
    "K70": {**BOOK_OPTIONS["F25L5"], "free_tiers": [[0.50, 0.85], [0.35, 0.80], [0.25, 0.70]]},
    "K80": {**BOOK_OPTIONS["F25L5"], "free_tiers": [[0.50, 0.90], [0.35, 0.85], [0.25, 0.80]]},
    "K90": {**BOOK_OPTIONS["F25L5"], "free_tiers": [[0.25, 0.90]]},
})
# 1.50 Coinbase backup test: only the coins Coinbase lets US accounts trade as perps (CDE), Hyperliquid names.
COINBASE_US_PERPS = {"BTC", "ETH", "ZEC", "XRP", "NEAR", "SOL", "PAXG", "HYPE", "kPEPE", "AAVE", "LINK", "ONDO", "ENA",
                     "BNB", "LTC", "DOT", "kSHIB"}
BOOK_OPTIONS_LATE = {
    # lists, not sets: book settings are saved in gc_paper_state.json (1.52.1)
    "CB": {"coins": sorted(COINBASE_US_PERPS)},
    "CB5": {"coins": sorted(COINBASE_US_PERPS), "max_pos": 5, "slot_size": 0.25},
    "CBM": {"short_coins": sorted(COINBASE_US_PERPS)},
    "CBL": {"short_coins": []},
}
# 1.49 Gemini's exit ideas on 4h candles, on top of the live rules (see exit_rules()).
BOOK_OPTIONS.update({
    "G1": {**BOOK_OPTIONS["F25L5"], "exit_rule": {"kind": "stall", "min_peak": 0.06, "bars": 6, "retain": 0.68}},
    "G2": {**BOOK_OPTIONS["F25L5"], "exit_rule": {"kind": "shelf", "min_peak": 0.05, "ratio": 0.55, "buffer": 0.002}},
    "G3": {**BOOK_OPTIONS["F25L5"], "exit_rule": {"kind": "churn", "min_peak": 0.06, "window": 6, "max_er": 0.18}},
    "G4": {**BOOK_OPTIONS["F25L5"], "exit_rule": {"kind": "reject", "min_peak": 0.05, "confirm": 2}},
})
BOOK_OPTIONS.update({c: {**BOOK_OPTIONS["F25L5"], **o} for c, o in BOOK_OPTIONS_LATE.items()})
# 1.52 stock ideas on top of F5 (Gemini review): bank half without a lock, SPY filter, bigger top-ups, no index ETFs.
BOOK_OPTIONS.update({
    "F5L5": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.05, "frac": 0.5}},
    "F5L10": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.10, "frac": 0.5}},
    # 1.65 (test, not live): bank half only when the +5% came FAST (a spike, which tends to fade); a slow steady climb keeps full size.
    "F5LF7": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.05, "frac": 0.5, "max_days": 7}},
    "F5LF14": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.05, "frac": 0.5, "max_days": 14}},
    # 1.66 (tests, not live): three volume ideas on top of F5.
    "F5V": {**BOOK_OPTIONS["F5"], "vol_filter": 1.5},
    "F5W": {**BOOK_OPTIONS["F5"], "seller_exit": {"after_gain": 0.03, "vol_mult": 1.5, "close_in_bottom": 0.35}},
    "F5LV": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.05, "frac": 0.5, "vol_mult": 1.5}},
    # 1.67 (test, not live): bank half only while the benchmark (SPY) is weak.
    "F5LM": {**BOOK_OPTIONS["F5"], "take_half": {"at": 0.05, "frac": 0.5, "only_weak_mkt": True}},
    "F5M": {**BOOK_OPTIONS["F5"], "mkt_filter": True},
    "F5A": {**BOOK_OPTIONS["F5"], "topup_target": 0.066},
    "F5X": {**BOOK_OPTIONS["F5"], "exclude": ["SPY", "QQQ"]},
    "WALL": {**BOOK_OPTIONS["F5"], "exclude": ["SPY", "QQQ"]},
    "WMID": {**BOOK_OPTIONS["F5"], "exclude": ["SPY", "QQQ"], "size_group": "mid"},
    "WTOP": {**BOOK_OPTIONS["F5"], "exclude": ["SPY", "QQQ"], "size_group": "large"},
    "WDAY": {**BOOK_OPTIONS["F5"], "exclude": ["SPY", "QQQ"], "size_group": "daily_top"},
    "C100": {**BOOK_OPTIONS["F25L5"], "size_group": "top100"},
    "CALL": {**BOOK_OPTIONS["F25L5"]},
    "CDAY": {**BOOK_OPTIONS["F25L5"], "size_group": "daily_top"},
    "CALLV": {**BOOK_OPTIONS["F25L5"], "min_dv": 1_000_000},
    "CALLS": {**BOOK_OPTIONS["F25L5"], "small": {"slip": 0.005, "size": 1.0, "max": 99}},
    "CS1": {**BOOK_OPTIONS["F25L5"], "small": {"slip": 0.005, "size": 0.5, "max": 2, "take_half": {"at": 0.10, "frac": 0.5},
            "lock_tiers": [[1.00, 0.92], [0.50, 0.88], [0.25, 0.85], [0.10, 0.80], [0.05, 0.70], [0.03, 0.60]]}},
    "CS2": {**BOOK_OPTIONS["F25L5"], "small": {"slip": 0.005, "size": 0.5, "max": 2, "take_half": {"at": 0.20, "frac": 0.5},
            "lock_tiers": [[1.00, 0.85], [0.50, 0.80], [0.25, 0.75], [0.10, 0.65], [0.05, 0.55]]}},
    "CS3": {**BOOK_OPTIONS["F25L5"], "small": {"slip": 0.005, "size": 1.0, "max": 3, "take_half": {"at": 0.10, "frac": 0.5},
            "lock_tiers": [[1.00, 0.92], [0.50, 0.88], [0.25, 0.85], [0.10, 0.80], [0.05, 0.70], [0.03, 0.60]]}},
})
BOOK_OPTIONS["AB2HF5"] = {**BOOK_OPTIONS["AB2H"], "free_tiers": [[0.50, 0.80], [0.30, 0.75], [0.20, 0.70], [0.10, 0.60], [0.05, 0.50]]}
BOOK_OPTIONS.update({"RML": BOOK_OPTIONS["RM"], "RMFL": BOOK_OPTIONS["RMF"], "RSL": BOOK_OPTIONS["RS"], "EXL": BOOK_OPTIONS["EX"],
                     "Z4F": BOOK_OPTIONS["ZSF4"], "Z4HF": BOOK_OPTIONS["ZSF"]})
VOL_AVG_DAYS = 20                      # seller-exit: today's volume vs the previous 20 days
ATR_PERIOD = 14
CLIMAX_ATR = 2.0                       # "parabolic" = daily close more than 2 ATR above the upper band
INTRADAY_INTERVAL = "4h"               # candles used by lock_intraday books (Hyperliquid keeps the last 5000 = ~833 days)
INTRADAY_MS = 4 * 3600 * 1000
INTRADAY_MAX_DAYS = 830
INTERVAL_HOURS = {"4h": 4, "1h": 1}
DATA_1H = {}                           # hourly candles for lock_interval "1h" books (crypto), loaded only when needed
KNIFE_LOOKBACK = 6                     # knife exit: a 4h candle's volume vs the previous 6 candles (one day)

# Growth calculator (every email): account sizes to scale results to, and the daily-profit targets to size for.
GROWTH_SIZES = [1000, 10000, 50000, 200000]
GROWTH_TARGETS = [200, 400]
GROWTH_MIN_DAYS = 30                   # below this, annualising a few days of results is meaningless


def is_small(book, coin):
    """CS books: a coin outside today's top 100 by open interest gets the book's own small-coin rules (book["small"])."""
    return bool(book.get("small")) and coin not in TOP_OI


def fee_of(book, p=None, coin=None):
    """Fee per side; small coins also pay the book's extra slippage (thin order books fill worse than the candle price)."""
    small = p.get("small") if p is not None else is_small(book, coin)
    return FEE_RATE + (book["small"].get("slip", 0.0) if small and book.get("small") else 0.0)


def th_of(book, p):
    """Take-half rule for this position: small coins use the small rule when the book has one."""
    if p.get("small") and (book.get("small") or {}).get("take_half"):
        return book["small"]["take_half"]
    th = book.get("take_half")
    if th and book.get("bull_half") and book.get("_strong_up") and p["side"] == "LONG":
        return {**th, "at": book["bull_half"]["at"]}      # strong BTC uptrend: longs run full size until the higher level
    return th


def small_tiers(book, p):
    """The small-coin profit ladder for this position, or None (= the book's normal rules)."""
    return (book.get("small") or {}).get("lock_tiers") if p.get("small") else None


def lock_applies(book, side):
    """Profit lock on all positions ("all", or True from older saved state), on longs only ("long"), off, or
    "adaptive": longs always, shorts unless step_day flagged a strong BTC downtrend for today (book["_strong_down"])."""
    mode = book.get("lock")
    if mode == "adaptive":
        return side == "LONG" or not book.get("_strong_down")
    return mode is True or mode == "all" or (mode == "long" and side == "LONG")


def free_floor(book, p):
    """AB2HF: a short that 'runs free' (adaptive lock off for shorts today) still gets the wide big-winner floor."""
    return bool((book.get("free_tiers") or book.get("free_half")) and p["side"] == "SHORT" and book.get("lock") == "adaptive"
                and book.get("_strong_down"))


def free_ladder_next(book, p, low):
    """1.70: the next unfilled rung of a free-short ladder that a candle's low has reached, or None."""
    lad = book.get("free_ladder")
    i = p.get("rung", 0)
    if book.get("ladder_when_bounce") and not book.get("_bounce"):
        return None                                           # 1.71: ladder only while BTC is bouncing
    if book.get("ladder_rank_min") and (p.get("rank") or 0) < book["ladder_rank_min"]:
        return None                                           # 1.72: ladder only for the smaller coins (rank 51-100)
    if book.get("ladder_hivol") and not p.get("hi_vol"):
        return None                                           # 1.72: ladder only for the more volatile coins
    if lad and free_floor(book, p) and i < len(lad) and low <= p["entry"] * (1 - lad[i]["at"]):
        return lad[i]
    return None


def ladder_exec(book, p, coin, open_px, low, day, log):
    """Sell every rung the candle's low reached, one after the other (fills at the rung price, or the open if it gapped through)."""
    rung = free_ladder_next(book, p, low)
    while rung:
        p.setdefault("qty0", p["qty"])
        frac = min(1.0, rung["frac0"] * p["qty0"] / p["qty"]) if p["qty"] > 0 else 1.0
        tp = p["entry"] * (1 - rung["at"])
        partial_close(book, coin, min(open_px, tp), frac, day, f"💵 Banked {rung['frac0']*100:.0f}% at +{rung['at']*100:.0f}% (free short ladder)", log)
        p["rung"] = p.get("rung", 0) + 1
        rung = free_ladder_next(book, p, low)


def free_half_hit(book, p, low):
    """AB2HT books: a free-running short reached the bank-half level (checked against a candle's low)."""
    fh = book.get("free_half")
    return bool(fh and free_floor(book, p) and not p.get("half_taken") and not p.get("rung") and low <= p["entry"] * (1 - fh["at"]))


def vol_mult(book, data, day):
    """1.64: slot-size multiplier from the benchmark's (BTC / SPY) recent volatility, using bars up to the PREVIOUS close only. 1.0 = normal size."""
    vt = book.get("vol_target")
    if not vt:
        return 1.0
    b = data.get(BENCH, {}).get(day)
    x = b.get("_prev") if b else None
    closes = []
    while x is not None and len(closes) < vt["long"] + 1:
        closes.append(x["c"]); x = x.get("_prev")
    if len(closes) < vt["long"] + 1:
        return 1.0                                            # not enough history yet: normal size
    rets = [math.log(closes[i] / closes[i + 1]) for i in range(len(closes) - 1) if closes[i] > 0 and closes[i + 1] > 0]   # newest first
    if len(rets) < vt["long"]:
        return 1.0

    def sd(v):
        m = sum(v) / len(v)
        return (sum((r - m) ** 2 for r in v) / len(v)) ** 0.5
    s_short, s_long = sd(rets[:vt["short"]]), sd(rets[:vt["long"]])
    if s_short <= 0:
        return 1.0
    return max(vt["floor"], min(1.0, s_long / s_short))


def btc_bounce(book, data, day):
    """1.71: has BTC closed at least ladder_when_bounce above its lowest close of the last 10 days, as of the PREVIOUS day?"""
    pct = book.get("ladder_when_bounce")
    b = data.get(BENCH, {}).get(day)
    pb = b.get("_prev") if b else None
    if not pct or not pb or not pb.get("valid"):
        return False
    closes, x = [], pb
    while x is not None and len(closes) < 10:
        closes.append(x["c"]); x = x.get("_prev")
    return pb["c"] >= min(closes) * (1 + pct)


def btc_relief(book, data, day):
    """1.74: BTC bounce proxy on daily bars: its last close (as of the PREVIOUS day) is above the day-before's high."""
    if not book.get("relief_guard"):
        return False
    b = data.get(BENCH, {}).get(day)
    pb = b.get("_prev") if b else None
    pp = pb.get("_prev") if pb else None
    return bool(pb and pp and pb.get("valid") and pb["c"] > pp["h"])


def strong_uptrend(book, data, day):
    """1.63: is BTC more than bull_half['pct'] ABOVE its filter as of the PREVIOUS daily close?"""
    b = data.get(BENCH, {}).get(day)
    pb = b.get("_prev") if b else None
    if not pb or not pb.get("valid") or not book.get("bull_half"):
        return False
    return pb["c"] > pb["filter"] * (1 + book["bull_half"]["pct"])


def strong_downtrend(book, data, day):
    """Adaptive books: is BTC in a strong downtrend as of the PREVIOUS daily close?"""
    b = data.get(BENCH, {}).get(day)
    pb = b.get("_prev") if b else None
    if not pb or not pb.get("valid"):
        return False
    if book.get("regime") == "r30":
        return pb.get("r30") is not None and pb["r30"] <= -0.10
    strong = pb["c"] < pb["filter"] * (1 - book.get("regime_pct", 0.05))
    if strong and book.get("squeeze"):
        closes, x = [], pb
        while x is not None and len(closes) < 10:
            closes.append(x["c"]); x = x.get("_prev")
        if pb["c"] >= min(closes) * (1 + book["squeeze"]):
            return False                                      # BTC is bouncing hard: lock the shorts again
    return strong


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
        dv = [x["c"] * x.get("v", 0) for x in bars[max(0, i - 20):i]]      # dollar volume of the 20 days BEFORE this one
        b["dv20"] = sum(dv) / len(dv) if len(dv) >= 10 else None           # (known that morning: no peeking ahead)
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        atr = tr if atr is None else atr + (tr - atr) / ATR_PERIOD          # Wilder's ATR
        b["atr"] = atr
        b["r30"] = b["c"] / bars[i - 30]["c"] - 1 if i >= 30 else None
        if i >= 10:                                           # Kaufman efficiency ratio: 1 = straight line, 0 = chop
            path = sum(abs(bars[j]["c"] - bars[j - 1]["c"]) for j in range(i - 9, i + 1))
            b["er10"] = abs(b["c"] - bars[i - 10]["c"]) / path if path else 0.0
        prior = bars[max(0, i - 10):i]
        b["hh10"] = max(x["h"] for x in prior) if len(prior) == 10 else None
        b["ll10"] = min(x["l"] for x in prior) if len(prior) == 10 else None
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
        vs, natr = book.get("vol_size"), book.get("_natr")
        if vs and natr:
            return nav * min(vs["max"], max(vs["min"], vs["risk"] / (vs["atr_mult"] * natr)))
        return nav * book["slot_size"] * book.get("_vol_mult", 1.0)
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
    rate = fee_of(book, coin=coin)
    fee = usd * rate
    if usd + fee > budget:
        usd = max(0.0, budget / (1 + rate))
        fee = usd * rate
    qty = usd / px
    book["cash"] -= usd + fee
    book["positions"][coin] = {"side": side, "qty": qty, "entry": px, "cost": usd, "collateral": usd if side == "SHORT" else 0.0,
                               "fees": fee, "opened": date, "peak_px": px, "stop": None, "last_px": px, "reason": reason,
                               "small": is_small(book, coin)}
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
    if book.get("reenter") and p["side"] == "LONG" and reason.startswith(lock_label(book)):
        book.setdefault("relock", {})[coin] = p["peak_px"]          # may buy back above this peak (see LONG ENTRIES)
    if book.get("reenter_short") and p["side"] == "SHORT" and reason.startswith(lock_label(book)):
        book.setdefault("relock_s", {})[coin] = p["peak_px"]        # may short again below this low (see SHORT ENTRIES)
    value = pos_value(p, px)
    fee = p["qty"] * px * fee_of(book, p)
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


def set_lock_stop(book, p, high, low, hours=1.0):
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
        fl = lock_floor(p["peak_px"] / p["entry"] - 1, small_tiers(book, p) or book.get("lock_tiers"))
        p["stop"] = None if fl is None else p["entry"] * (1 + fl)
    else:
        prev_low = p["peak_px"]
        p["peak_px"] = min(p["peak_px"], low)
        p["calm_h"] = 0.0 if low < prev_low else p.get("calm_h", 0.0) + hours      # 1.74: hours since the coin last made a new low
        if small_tiers(book, p):                              # small coins: their own ladder, free-running or not
            fl = lock_floor(1 - p["peak_px"] / p["entry"], small_tiers(book, p))
            p["stop"] = None if fl is None else p["entry"] * (1 - fl)
            return
        if free_floor(book, p) and not book.get("free_tiers"):
            p["stop"] = None                                  # free short with no floor (AB2HT): only the -4% stop
            return
        tiers = book["free_tiers"] if free_floor(book, p) else book.get("lock_tiers")
        gain = 1 - p["peak_px"] / p["entry"]
        fl = lock_floor(gain, tiers)
        if free_floor(book, p):                               # 1.74: conditional tighteners (each only ever raises the locked gain)
            fd, fg, rg = book.get("free_decay"), book.get("free_giveback"), book.get("relief_guard")
            if fd and fl is not None and gain >= fd["start"]:
                fl = gain * min(fd["cap"], fl / gain + fd["step"] * int(p.get("calm_h", 0.0) // fd["every"]))
            if fg and gain >= fg["start"]:
                fl = max(fl or 0.0, gain * fg["keep"], gain - fg["cap"])
            if rg and book.get("_relief") and gain >= rg["start"]:
                fl = max(fl or 0.0, gain * rg["keep"])
            fsl = book.get("free_slope")                                 # 1.77: ramp from the loss stop up to break-even
            if fsl and gain - fsl["lead"] > -book.get("disaster", 0.04):
                fl = max(fl if fl is not None else -9.0, min(gain - fsl["lead"], 0.005))
            fb, fn = book.get("free_be"), book.get("free_net")          # 1.75
            if fb and gain >= fb["start"]:
                fl = max(fl or 0.0, fb["lock"])
            if fn and gain >= fn["start"] and gain - fn["cap"] > 0:
                fl = max(fl or 0.0, gain - fn["cap"])
        p["stop"] = None if fl is None else p["entry"] * (1 - fl)
        ft = book.get("free_trail")
        if ft and free_floor(book, p):                        # 1.69: ATR trailing stop for free-running shorts (only ever tightens)
            if p.get("atr") and 1 - p["peak_px"] / p["entry"] >= ft["start"]:
                new = min(p["peak_px"] + ft["k"] * p["atr"], p["entry"])
                p["chand"] = new if p.get("chand") is None else min(p["chand"], new)
            if p.get("chand") is not None:
                p["stop"] = p["chand"] if p["stop"] is None else min(p["stop"], p["chand"])


def partial_close(book, coin, px, frac, date, reason, log):
    """Close `frac` of a position at px (Book X take-half on longs, AB2HT bank-half on free shorts). The rest keeps running."""
    p = book["positions"][coin]
    q = p["qty"] * frac
    fee = q * px * fee_of(book, p)
    fees_part = p["fees"] * frac
    book["cash"] += pos_value(p, px) * frac - fee
    short = p["side"] == "SHORT"
    pnl = ((p["entry"] - px) if short else (px - p["entry"])) * q - fees_part - fee
    p["qty"] -= q
    p["cost"] *= (1 - frac)
    p["collateral"] = p.get("collateral", 0.0) * (1 - frac)
    p["fees"] -= fees_part
    p["half_taken"] = True
    gain = (1 - px / p["entry"]) if short else (px / p["entry"] - 1)
    peak = (1 - p["peak_px"] / p["entry"]) if short else (p["peak_px"] / p["entry"] - 1)
    book["closed"].append({"coin": coin, "side": p["side"], "opened": p["opened"], "closed": date, "entry": p["entry"], "exit": px,
                           "pnl": round(pnl, 4), "pct": round(gain * 100, 2), "peak_pct": round(max(peak, gain) * 100, 2),
                           "reason": reason, "why_in": p["reason"]})
    log.append(("EXIT", coin, f"{p['side']} {reason} — {frac*100:.0f}% closed at {gain*100:+.2f}%, ${pnl:+,.2f}"))


def sellers_took_over(book, p, b):
    """Book W: the long has been up `after_gain`, and today is a red day on heavy volume closing near its low."""
    se = book.get("seller_exit")
    if not se or p["side"] != "LONG" or p["opened"] == b.get("date") or not b.get("vavg"):
        return False
    rng = b["h"] - b["l"]
    return (max(p["peak_px"], b["h"]) / p["entry"] - 1 >= se["after_gain"] and b["c"] < b["o"] and b.get("v", 0) >= se["vol_mult"] * b["vavg"]
            and rng > 0 and (b["c"] - b["l"]) <= se["close_in_bottom"] * rng)


def smart_cut(book, p, b):
    """FLS: the long is down at least `loss` from entry on a red day with heavy volume that closes near its low."""
    sc = book.get("smart_cut")
    if not sc or p["side"] != "LONG" or p["opened"] == b.get("date") or not b.get("vavg"):
        return False
    rng = b["h"] - b["l"]
    return (b["c"] <= p["entry"] * (1 - sc["loss"]) and b["c"] < b["o"] and b.get("v", 0) >= sc["vol_mult"] * b["vavg"]
            and rng > 0 and (b["c"] - b["l"]) <= sc["close_in_bottom"] * rng)


def knife_hit(book, p, k):
    """Knife books: the long has been up `after_gain`, and this 4h candle is red, closes near its low, and either came on
    heavy volume (vol_mult x the previous KNIFE_LOOKBACK candles) or fell at least `drop` open-to-close."""
    kn = book.get("knife")
    if not kn or p["side"] != "LONG" or p["peak_px"] / p["entry"] - 1 < kn["after_gain"] or k["c"] >= k["o"]:
        return False
    rng = k["h"] - k["l"]
    if rng <= 0 or (k["c"] - k["l"]) > kn["close_in_bottom"] * rng:
        return False
    if kn.get("vol_mult") and not (k.get("va") and k.get("v", 0) >= kn["vol_mult"] * k["va"]):
        return False
    if kn.get("drop") and (k["o"] - k["c"]) / k["o"] < kn["drop"]:
        return False
    return True


def smart_exits(book, p, k):
    """Runner mode, structure trail, strength vs BTC, exhaustion. Called after set_lock_stop for each intraday candle.
    May loosen (runner) or raise (swing) p["stop"]; returns an exit reason to close at this candle's close, or None."""
    long_ = p["side"] == "LONG"
    sign = 1 if long_ else -1
    peak_gain = sign * (p["peak_px"] / p["entry"] - 1)
    now_gain = sign * (k["c"] / p["entry"] - 1)
    rm = book.get("runner")
    if rm and k.get("ema") is not None and k.get("ema_prev") is not None and k.get("atr"):
        trending = (k["c"] > k["ema"] > k["ema_prev"]) if long_ else (k["c"] < k["ema"] < k["ema_prev"])
        if not p.get("runner") and peak_gain >= rm["after_gain"] and trending:
            p["runner"] = True
        if p.get("runner"):
            if (k["c"] < k["ema"]) if long_ else (k["c"] > k["ema"]):
                return "🏃 Runner ended (4h close back through the EMA20)"
            floor = p["entry"] * (1 + sign * rm["after_gain"] / 2)           # a runner never gives back more than half of +5%
            room = p["peak_px"] - sign * rm["atr_mult"] * k["atr"]
            wide = max(floor, room) if long_ else min(floor, room)
            prev = p.get("runner_stop")
            p["runner_stop"] = wide if prev is None else (max(prev, wide) if long_ else min(prev, wide))
            p["stop"] = p["runner_stop"]                                     # wide stop replaces the tight lock
    sw = book.get("swing")
    if sw and peak_gain >= sw["after_gain"] and k.get("ll6") is not None:
        level = k["ll6"] if long_ else k["hh6"]
        floor = p["entry"] * (1 + sign * 0.01)
        cand = max(level, floor) if long_ else min(level, floor)
        prev = p.get("swing_stop")
        p["swing_stop"] = cand if prev is None else (max(prev, cand) if long_ else min(prev, cand))
        p["stop"] = p["swing_stop"]
    rs = book.get("rs")
    if rs and k.get("rs") is not None and peak_gain >= rs["after_gain"] and now_gain > 0 and not p.get("runner"):
        if (k["rs"] < -rs["lag"]) if long_ else (k["rs"] > rs["lag"]):
            return f"🐢 {'Lagging' if long_ else 'Beating'} BTC by {abs(k['rs'])*100:.1f}% over 24h"
    ex = book.get("exhaust")
    if ex and k.get("atr") and peak_gain >= ex["after_gain"]:
        rng = k["h"] - k["l"]
        wick = (k["h"] - max(k["o"], k["c"])) if long_ else (min(k["o"], k["c"]) - k["l"])
        if rng >= ex["range_atr"] * k["atr"] and rng > 0 and wick >= ex["wick"] * rng:
            return f"🌋 Exhaustion candle ({rng / k['atr']:.1f}x ATR, {wick / rng * 100:.0f}% wick)"
    return None


def vbottom_signal(book, b):
    """VB books: sharp drop to a new 10-day low that closes strong (top of the range, green) on heavy volume."""
    vb = book.get("vbottom")
    if not vb or not b.get("hh10") or not b.get("ll10") or not b.get("vavg"):
        return False
    rng = b["h"] - b["l"]
    return (b["l"] <= b["ll10"] and b["l"] <= b["hh10"] * (1 - vb["drop"]) and rng > 0 and b["c"] > b["o"]
            and (b["h"] - b["c"]) <= vb["close_top"] * rng and b.get("v", 0) >= vb["vol"] * b["vavg"])


def fade_out(book, p, k):
    """Fade books: the move has run out of fuel. Called after set_lock_stop for each intraday candle. Tracks the busiest
    volume stretch of the trade (the candles' rolling average) and counts candles in a row that trade under vol_ratio of
    it without making a new high (long) / low (short). True = take the profit at this candle's close."""
    fd = book.get("fade")
    if not fd:
        return False
    p["v_ref"] = max(p.get("v_ref") or 0.0, k.get("va") or 0.0)
    new_extreme = k["h"] >= p["peak_px"] if p["side"] == "LONG" else k["l"] <= p["peak_px"]
    quiet = p["v_ref"] > 0 and k.get("v", 0) < fd["vol_ratio"] * p["v_ref"]
    p["fade_n"] = (p.get("fade_n") or 0) + 1 if quiet and not new_extreme else 0
    sign = 1 if p["side"] == "LONG" else -1
    peak_gain = sign * (p["peak_px"] / p["entry"] - 1)
    now_gain = sign * (k["c"] / p["entry"] - 1)
    return p["fade_n"] >= fd["bars"] and peak_gain >= fd["after_gain"] and now_gain > 0


def weak_volume(book, b):
    """vol_filter books: True when the signal day's volume is below vol_filter x the 20-day average (entry skipped)."""
    vf = book.get("vol_filter")
    return bool(vf and b.get("vavg") and b.get("v", 0) < vf * b["vavg"])


def stop_label(book):
    return "✂️ Loss stop" if book.get("disaster", 1) <= 0.10 else "🧯 Disaster stop"


def exit_rules(rule, p, candles):
    """Gemini's four exit ideas, kept as running state in p['rs'] so each candle is O(1). Returns (reason, price) or None."""
    long_ = p["side"] == "LONG"
    rs = p.setdefault("rs", {"peak": p["entry"], "since": 0, "mid": None, "n": 0, "rsum": 0.0, "last": [], "stop": None})
    for k in candles:
        # a shelf stop set on an earlier candle can fill inside this one
        if rs["stop"] is not None and ((long_ and k["l"] <= rs["stop"]) or (not long_ and k["h"] >= rs["stop"])):
            px = min(k["o"], rs["stop"]) if long_ else max(k["o"], rs["stop"])
            return ("🧱 Shelf stop (4h consolidation broke)", px)
        new_ext = (k["h"] > rs["peak"]) if long_ else (k["l"] < rs["peak"])
        if new_ext:
            rs["peak"], rs["since"], rs["mid"] = (k["h"] if long_ else k["l"]), 0, 0.5 * (k["o"] + k["c"])
        else:
            rs["since"] += 1
        peak_gain = (rs["peak"] / p["entry"] - 1) if long_ else (1 - rs["peak"] / p["entry"])
        now_gain = (k["c"] / p["entry"] - 1) if long_ else (1 - k["c"] / p["entry"])
        rng = max(k["h"] - k["l"], 1e-12)
        avg_prev = rs["rsum"] / rs["n"] if rs["n"] else None
        rs["last"] = (rs["last"] + [k])[-max(6, rule.get("window", 6), rule.get("confirm", 2)):]
        kind, hit = rule["kind"], None
        if peak_gain >= rule["min_peak"]:
            if kind == "stall" and rs["since"] >= rule["bars"] and now_gain >= peak_gain * rule["retain"]:
                hit = (f"⏳ Stalled: no new {'high' if long_ else 'low'} for {rs['since']} x 4h (kept {now_gain*100:.1f}% of a "
                       f"{peak_gain*100:.1f}% peak)", k["c"])
            elif kind == "shelf" and avg_prev and rng < rule["ratio"] * avg_prev and len(rs["last"]) >= 2:
                two = rs["last"][-2:]
                cand = (min(b["l"] for b in two) * (1 - rule["buffer"])) if long_ else (max(b["h"] for b in two) * (1 + rule["buffer"]))
                if long_ and cand < k["c"] and (rs["stop"] is None or cand > rs["stop"]):
                    rs["stop"] = cand
                elif not long_ and cand > k["c"] and (rs["stop"] is None or cand < rs["stop"]):
                    rs["stop"] = cand
            elif kind == "churn" and len(rs["last"]) >= rule["window"]:
                win = rs["last"][-rule["window"]:]
                path = sum(abs(b["c"] - b["o"]) for b in win)
                er = abs(win[-1]["c"] - win[0]["o"]) / path if path else 1.0
                mid = 0.5 * (max(b["h"] for b in win) + min(b["l"] for b in win))
                weak = (k["c"] < mid) if long_ else (k["c"] > mid)
                if er < rule["max_er"] and weak and now_gain > 0:
                    hit = (f"🌀 Churn: 6 x 4h efficiency {er:.2f}, closed on the weak side", k["c"])
            elif kind == "reject" and rs["mid"] is not None and rs["since"] >= rule["confirm"] and now_gain > 0:
                recent = rs["last"][-rule["confirm"]:]
                if all((b["c"] < rs["mid"]) if long_ else (b["c"] > rs["mid"]) for b in recent):
                    hit = (f"↩️ Rejected: {rule['confirm']} x 4h closes back past the peak candle's middle", k["c"])
        rs["n"] += 1
        rs["rsum"] += rng
        if hit:
            return hit
    return None


def take_half_hit(book, p, high, day=None, bar=None):
    th = th_of(book, p)
    if th and th.get("only_weak_mkt") and not book.get("_mkt_weak"):
        return False                                          # 1.67: strong market = let the winner run at full size
    if th and th.get("vol_mult") and bar is not None:         # 1.66: only bank on a heavy-volume day (blow-off), else let it run
        if not bar.get("vavg") or bar.get("v", 0) < th["vol_mult"] * bar["vavg"]:
            return False
    if th and th.get("max_days") and day:                     # 1.65: only a FAST move (within max_days calendar days of the buy) is banked
        try:
            if (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(p["opened"][:10], "%Y-%m-%d")).days > th["max_days"]:
                return False
        except ValueError:
            pass
    return bool(th and p["side"] == "LONG" and not p.get("half_taken") and high >= p["entry"] * (1 + th["at"]))


def step_day(book, day, data, ranked, btc_down, drift_exits, log, skips, data4h=None):
    """Runs one LS-15 decision cycle on the daily candle `day` (YYYY-MM-DD) for one book."""
    if (book.get("take_half") or {}).get("only_weak_mkt"):    # 1.67: benchmark regime as of the PREVIOUS close (no peeking)
        mb = data.get(BENCH, {}).get(day)
        pmb = mb.get("_prev") if mb else None
        book["_mkt_weak"] = bool(pmb and pmb.get("valid") and pmb["c"] <= pmb["filter"])
    if book.get("lock") == "adaptive":
        book["_strong_down"] = strong_downtrend(book, data, day)
        book["_strong_up"] = strong_uptrend(book, data, day)
        book["_vol_mult"] = vol_mult(book, data, day)
        book["_bounce"] = btc_bounce(book, data, day)
        book["_relief"] = btc_relief(book, data, day)
    if book.get("crash_slots"):
        cs, base = book["crash_slots"], BOOK_OPTIONS["AB2H"]
        book["max_pos"] = cs["max_pos"] if book.get("_strong_down") else base["max_pos"]
        book["gross_cap"] = cs["gross_cap"] if book.get("_strong_down") else base["gross_cap"]
    bar = {c: data[c][day] for c in data if day in data[c]}
    prev = {c: b["_prev"] for c, b in bar.items() if b.get("_prev")}   # previous bar (previous TRADING day for stocks)
    closes = {c: b["c"] for c, b in bar.items()}
    rank_of = {c: i + 1 for i, c in enumerate(ranked)}
    universe = ranked[:UNIVERSE_SIZE]
    if book.get("exclude"):
        universe = [c for c in universe if c not in book["exclude"]]
    if book.get("size_group") == "mid":                       # WM: skip the 100 biggest names
        universe = [c for c in universe if c not in LARGE_CAPS]
    elif book.get("size_group") == "large":                   # WL: only the 100 biggest names
        universe = [c for c in universe if c in LARGE_CAPS]
    elif book.get("size_group") == "top100":                  # C100: today's top 100 by open interest only
        universe = [c for c in universe if c in TOP_OI]
    elif book.get("size_group") == "daily_top":               # WDAY: the 100 most-traded names as of that day
        top = set(sorted((c for c in universe if (bar.get(c) or {}).get("dv20")), key=lambda c: -bar[c]["dv20"])[:100])
        universe = [c for c in universe if c in top]          # keep the run's (shuffled) order: only the membership changes
    if book.get("min_dv"):                                    # CALLV: only coins that traded enough the 20 days before
        universe = [c for c in universe if ((bar.get(c) or {}).get("dv20") or 0) >= book["min_dv"]]
    if book.get("coins"):                                     # restricted coin list (CB books: Coinbase US perps only)
        universe = [c for c in universe if c in book["coins"]]
    if book.get("er_rank"):                                   # crowded days: the cleanest trends claim the slots first
        universe = sorted(universe, key=lambda c: -((bar.get(c) or {}).get("er10") or 0.0))
    exited, entered = set(), set()

    rate = FUNDING_DAY.get(day)                               # funding for positions held through this day
    if rate:
        for c, p in book["positions"].items():
            if p["opened"] == day:
                continue
            px = (prev.get(c) or {}).get("c") or p["last_px"]
            pay = p["qty"] * px * rate * 24 * (1 if p["side"] == "LONG" else -1)
            book["cash"] -= pay
            book["funding"] = book.get("funding", 0.0) + pay

    for c, p in book["positions"].items():
        if c in closes:
            p["last_px"] = closes[c]
            if p["opened"] != day:
                p["held"] = p.get("held", 0) + 1                 # trading days held (time_stop books)

    # 1.52: bank-half on books WITHOUT a profit lock (F5L5 / F5L10): the daily high reaching the level sells half there.
    if not book.get("lock") and book.get("take_half"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            if b and p["opened"] != day and take_half_hit(book, p, b["h"], day, b):
                if book.get("disaster") and b["l"] <= p["entry"] * (1 - book["disaster"]):
                    continue                                  # both levels hit in one day: assume the loss stop came first
                tp = p["entry"] * (1 + th_of(book, p)["at"])
                partial_close(book, c, max(b["o"], tp), th_of(book, p)["frac"], day,
                              f"💵 Take half at +{th_of(book, p)['at']*100:.0f}%", log)

    # Profit-lock books: resting stop (set at the end of yesterday) can fill during today's candle.
    # lock_intraday books walk the day's 4-hour candles instead, raising the stop after each one.
    if book.get("lock"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            free = free_floor(book, p)
            if not b or not (lock_applies(book, p["side"]) or free):
                continue
            hourly = book.get("lock_interval") == "1h" and DATA_1H
            iv = "1h" if hourly or (book.get("lock_interval") == "1h" and STOCKS) else "4h"
            intraday = ((DATA_1H if hourly else data4h) or {}).get(c, {}).get(day) if book.get("lock_intraday") else None
            if intraday:
                for k in intraday:
                    # The loss / disaster stop rests on the exchange too: inside the day it competes with the lock,
                    # candle by candle (checking it only against the daily low let a dip-then-rally day exit at a profit).
                    loss_px = p["entry"] * (1 - book["disaster"]) if book.get("disaster") and p["side"] == "LONG" and p["opened"] != day else None
                    if p["side"] == "LONG" and loss_px and (p["stop"] is None or loss_px > p["stop"]) and k["l"] <= loss_px:
                        close_position(book, c, min(k["o"], loss_px), day, f"{stop_label(book)} (-{book['disaster']*100:g}%, {iv})", log)
                        exited.add(c)
                        break
                    # Shorts too: a locked short's loss stop must be checked candle by candle, or a rise-then-fall day
                    # would exit at the lock (a profit) when the exchange had already stopped it out at a loss.
                    loss_s = p["entry"] * (1 + book["disaster"]) if book.get("disaster") and p["side"] == "SHORT" and p["opened"] != day else None
                    if loss_s and (p["stop"] is None or loss_s < p["stop"]) and k["h"] >= loss_s:
                        close_position(book, c, max(k["o"], loss_s), day, f"{stop_label(book)} (+{book['disaster']*100:g}% against, {iv})", log)
                        exited.add(c)
                        break
                    if p["stop"] is not None and p["side"] == "LONG" and k["l"] <= p["stop"]:
                        close_position(book, c, min(k["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g}, {iv})", log)
                        exited.add(c)
                        break
                    if p["stop"] is not None and p["side"] == "SHORT" and k["h"] >= p["stop"]:
                        close_position(book, c, max(k["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g}, {iv})", log)
                        exited.add(c)
                        break
                    if take_half_hit(book, p, k["h"]):
                        tp = p["entry"] * (1 + th_of(book, p)["at"])
                        partial_close(book, c, max(k["o"], tp), th_of(book, p)["frac"], day,
                                      f"💵 Take half at +{th_of(book, p)['at']*100:.0f}%", log)
                    if free_half_hit(book, p, k["l"]):
                        tp = p["entry"] * (1 - book["free_half"]["at"])
                        partial_close(book, c, min(k["o"], tp), book["free_half"]["frac"], day,
                                      f"💵 Banked half at +{book['free_half']['at']*100:.0f}% (free short)", log)
                    ladder_exec(book, p, c, k["o"], k["l"], day, log)
                    set_lock_stop(book, p, k["h"], k["l"], 1.0 if iv == "1h" else 4.0)    # this candle's high/low raises the peak, stop moves up
                    if free and not book.get("free_fade"):
                        continue                              # free-running short: only the wide floor, no fade / smart exits
                    why_smart = smart_exits(book, p, k)
                    if why_smart:
                        close_position(book, c, k["c"], day, f"{why_smart}, {iv}", log)
                        exited.add(c)
                        break
                    if fade_out(book, p, k):
                        close_position(book, c, k["c"], day, f"💤 Volume faded ({book['fade']['bars']} {iv} candles under "
                                       f"{book['fade']['vol_ratio']*100:.0f}% of the move's volume, no new {'high' if p['side'] == 'LONG' else 'low'})", log)
                        exited.add(c)
                        break
                    if knife_hit(book, p, k):
                        vol = f", volume {k['v']/k['va']:.1f}x" if k.get("va") else ""
                        close_position(book, c, k["c"], day, f"🔪 Falling knife (4h red candle {(k['c']/k['o']-1)*100:+.1f}%{vol}, closed near the low)", log)
                        exited.add(c)
                        break
                continue
            if free_half_hit(book, p, b["l"]):
                tp = p["entry"] * (1 - book["free_half"]["at"])
                partial_close(book, c, min(b["o"], tp), book["free_half"]["frac"], day,
                              f"💵 Banked half at +{book['free_half']['at']*100:.0f}% (free short)", log)
            ladder_exec(book, p, c, b["o"], b["l"], day, log)
            if take_half_hit(book, p, b["h"]) and not (p["stop"] is not None and b["l"] <= p["stop"]):
                tp = p["entry"] * (1 + th_of(book, p)["at"])
                partial_close(book, c, max(b["o"], tp), th_of(book, p)["frac"], day, f"💵 Take half at +{th_of(book, p)['at']*100:.0f}%", log)
            if p["stop"] is None:
                continue
            if p["side"] == "LONG" and b["l"] <= p["stop"]:
                close_position(book, c, min(b["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g})", log)
                exited.add(c)
            elif p["side"] == "SHORT" and b["h"] >= p["stop"]:
                close_position(book, c, max(b["o"], p["stop"]), day, f"{lock_label(book)} (stop {p['stop']:.6g})", log)
                exited.add(c)

    # 1.49 exit-rule books: walk today's 4h candles in order (no look-ahead) for each position opened before today.
    if book.get("exit_rule"):
        for c in list(book["positions"]):
            p = book["positions"][c]
            if p["opened"] == day:
                continue
            why = exit_rules(book["exit_rule"], p, (data4h or {}).get(c, {}).get(day) or [])
            if why:
                close_position(book, c, why[1], day, why[0], log)
                exited.add(c)

    # Disaster stop (Book U / live bot): a resting stop far from entry on every position, long or short.
    if book.get("disaster"):
        for c in list(book["positions"]):
            p, b = book["positions"][c], bar.get(c)
            if not b or p["opened"] == day:
                continue
            if p["side"] == "LONG" and b["l"] <= p["entry"] * (1 - book["disaster"]):
                px = p["entry"] * (1 - book["disaster"])
                close_position(book, c, min(b["o"], px), day, f"{stop_label(book)} (-{book['disaster']*100:g}%)", log)
                exited.add(c)
            elif p["side"] == "SHORT" and b["h"] >= p["entry"] * (1 + book["disaster"]):
                px = p["entry"] * (1 + book["disaster"])
                close_position(book, c, max(b["o"], px), day, f"{stop_label(book)} (+{book['disaster']*100:g}% against)", log)
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
        elif smart_cut(book, p, b):
            close_position(book, c, b["c"], day, f"🔪 Smart cut (down {(1 - b['c'] / p['entry']) * 100:.1f}%, red day on {b['v']/b['vavg']:.1f}x volume)", log)
            exited.add(c)
        elif b["c"] < b["upper"] and not str(p.get("reason", "")).startswith("V-bottom"):
            close_position(book, c, b["c"], day, "Close below upper GC band", log)
            exited.add(c)
        elif book.get("time_stop") and p.get("held", 0) >= book["time_stop"] and b["c"] < p["entry"]:
            close_position(book, c, b["c"], day, f"⏱️ No follow-through (still red after {book['time_stop']} days)", log)
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
    relock = book.get("relock") or {}
    for c in list(relock):                                    # re-entry chance ends once the trend breaks
        b = bar.get(c)
        if b and b.get("valid") and b["c"] < b["upper"]:
            relock.pop(c)
    mb = data.get(BENCH, {}).get(day) if book.get("mkt_filter") else None
    mkt_weak = bool(mb and mb.get("valid") and mb["c"] <= mb["filter"])
    for c in universe:
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid"):
            continue
        reentry = c in relock and c not in book["positions"] and b["c"] > b["upper"] and b["c"] > relock[c]
        if not (b["c"] > b["upper"] and pb["c"] <= pb["upper"]) and not reentry:
            continue
        if weak_volume(book, b):
            skips.append((c, f"Long signal on weak volume ({b['v'] / b['vavg']:.1f}x the 20-day average)")); continue
        if mkt_weak:
            skips.append((c, f"Long signal, but {BENCH} is below its GC filter (choppy/weak market)")); continue
        if book.get("no_long_crash") and book.get("_strong_down"):
            skips.append((c, "Long signal, but BTC is in a strong downtrend (no new longs)")); continue
        if c in exited:
            skips.append((c, "Long signal, but exited earlier this run")); continue
        if c in book["positions"]:
            skips.append((c, f"Long signal, already holding {book['positions'][c]['side']}")); continue
        if slots_full(book, "LONG"):
            skips.append((c, f"Long signal, all {slot_cap(book, 'LONG')} long slots in use")); continue
        if is_small(book, c) and sum(1 for q in book["positions"].values() if q.get("small")) >= book["small"]["max"]:
            skips.append((c, f"Long signal on a small coin, all {book['small']['max']} small-coin slots in use")); continue
        recent = b.get("breakout") and (datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(b["breakout"], "%Y-%m-%d")).days <= BREAKOUT_RECENT_DAYS
        book["_natr"] = (b["atr"] / b["c"]) if b.get("atr") and b["c"] else None
        want = entry_size(book, nav, LONG_SIZE_RECENT if recent else LONG_SIZE_OLD) * (book["small"]["size"] if is_small(book, c) else 1.0)
        power = buying_power(book, nav)
        usd = min(want, power / (1 + FEE_RATE))
        if usd < MIN_ORDER_USD:
            skips.append((c, f"Long signal, ${usd:,.2f} is below Hyperliquid's ${MIN_ORDER_USD:.0f} minimum order")); continue
        if usd < nav * MIN_TRADE_NAV:
            skips.append((c, f"Long signal, not enough cash (${usd:,.2f} < 1% NAV)")); continue
        tag = f"band breakout ({'recent' if recent else 'old'} breakout, {want / nav * 100:.0f}% NAV{', downsized' if usd < want - 0.01 else ''})"
        if reentry:
            tag = f"re-entry: new high after a lock exit ({want / nav * 100:.0f}% NAV)"
            relock.pop(c, None)
        open_position(book, c, "LONG", usd, b["c"], day, tag, log, power)
        entered.add(c)

    # 6b) V-BOTTOM LONGS (VB books, rank order): a sharp drop that closes strong on heavy volume
    for c in (universe if book.get("vbottom") else []):
        b = bar.get(c)
        if not b or not b.get("valid") or c in book["positions"] or c in exited or not vbottom_signal(book, b):
            continue
        if slots_full(book, "LONG"):
            skips.append((c, "V-bottom signal, all long slots in use")); continue
        power = buying_power(book, nav)
        usd = min(entry_size(book, nav, LONG_SIZE_RECENT), power / (1 + FEE_RATE))
        if usd < max(MIN_ORDER_USD, nav * MIN_TRADE_NAV):
            skips.append((c, f"V-bottom signal, ${usd:,.2f} is too small")); continue
        open_position(book, c, "LONG", usd, b["c"], day, f"V-bottom bounce ({(1 - b['l'] / b['hh10']) * 100:.0f}% under the 10-day high, "
                      f"volume {b['v'] / b['vavg']:.1f}x)", log, power)
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
        target = nav * book.get("topup_target", TOPUP_TARGET * book.get("size_mult", 1.0))
        if roe < TOPUP_MIN_ROE or held >= target:
            continue
        usd = min(target - held, buying_power(book, nav) / (1 + FEE_RATE))
        if usd < max(nav * MIN_TRADE_NAV, MIN_ORDER_USD):
            skips.append((c, f"Top-up wanted (ROE {roe*100:+.1f}%), ${usd:,.2f} is below 1% NAV / the ${MIN_ORDER_USD:.0f} minimum")); continue
        topup_position(book, c, usd, b["c"], day, log)

    # 8) SHORT ENTRIES (rank order)
    relock_s = book.get("relock_s") or {}
    for c in list(relock_s):                                  # re-short chance ends once the coin closes above its filter
        b = bar.get(c)
        if b and b.get("valid") and b["c"] > b["filter"]:
            relock_s.pop(c)
    for c in ([] if LONG_ONLY else universe):
        b, pb = bar.get(c), prev.get(c)
        if not b or not pb or not b.get("valid") or c in book["positions"]:
            continue
        if book.get("short_coins") is not None and c not in book["short_coins"]:
            continue                                          # CBM / CBL: shorts only where the venue allows them
        hedge = b["trend"] == "Red" and b["c"] < b["filter"] and pb["c"] >= pb["filter"]
        bear = btc_down and b["trend"] == "Red" and b["h"] >= BEAR_SHORT_TOUCH * b["filter"] and b["c"] < b["filter"]
        reshort = btc_down and c in relock_s and b["c"] < relock_s[c]
        if reshort:
            relock_s.pop(c, None)
        if not (hedge or bear or reshort):
            continue
        if weak_volume(book, b):
            skips.append((c, f"Short signal on weak volume ({b['v'] / b['vavg']:.1f}x the 20-day average)")); continue
        if c in exited:
            skips.append((c, "Short signal, but exited earlier this run")); continue
        if slots_full(book, "SHORT"):
            skips.append((c, f"Short signal, all {slot_cap(book, 'SHORT')} short slots in use")); continue
        if is_small(book, c) and sum(1 for q in book["positions"].values() if q.get("small")) >= book["small"]["max"]:
            skips.append((c, f"Short signal on a small coin, all {book['small']['max']} small-coin slots in use")); continue
        book["_natr"] = (b["atr"] / b["c"]) if b.get("atr") and b["c"] else None
        want = entry_size(book, nav, HEDGE_SHORT_SIZE if hedge else BEAR_SHORT_SIZE) * (book["small"]["size"] if is_small(book, c) else 1.0)
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
        why = "hedge short (cross under filter)" if hedge else (f"bear short (rejected bounce, {BENCH} down)" if bear else "re-short: new low after a lock exit")
        open_position(book, c, "SHORT", usd, b["c"], day, why, log, power)

    # 8b) ADD TO FREE-RUNNING SHORTS (F25P): once per short, when it is up short_add["at"]
    sa = book.get("short_add")
    for c, p in (list(book["positions"].items()) if sa else []):
        b = bar.get(c)
        if not b or p["side"] != "SHORT" or p.get("added") or not book.get("_strong_down") or 1 - b["c"] / p["entry"] < sa["at"]:
            continue
        nav = book_nav(book, closes)
        usd = min(nav * sa["size"], buying_power(book, nav) / (1 + FEE_RATE))
        if usd < max(MIN_ORDER_USD, nav * MIN_TRADE_NAV):
            continue
        fee, qty = usd * FEE_RATE, usd / b["c"]
        p["entry"] = (p["entry"] * p["qty"] + b["c"] * qty) / (p["qty"] + qty)
        p["qty"] += qty; p["cost"] += usd; p["collateral"] += usd; p["fees"] += fee; p["added"] = True
        book["cash"] -= usd + fee
        log.append(("TOP-UP", c, f"SHORT +${usd:,.2f} @ {b['c']:.6g} (winner add, avg entry now {p['entry']:.6g})"))

    # Update peaks with today's candle and (profit-lock books) set tomorrow's resting stop.
    natrs = sorted(x["atr"] / x["c"] for x in bar.values() if x.get("atr") and x.get("c")) if book.get("ladder_hivol") else []
    med_natr = natrs[len(natrs) // 2] if natrs else None
    for c, p in book["positions"].items():
        b = bar.get(c)
        if "rank" not in p:                                   # 1.72: tag the coin once, on the day it is opened
            p["rank"] = rank_of.get(c, 10 ** 6)
            p["hi_vol"] = bool(b and b.get("atr") and b.get("c") and med_natr and b["atr"] / b["c"] >= med_natr)
        if b and b.get("atr"):
            p["atr"] = b["atr"]                               # trail room for the next day comes from today's ATR
            p["climax"] = p["side"] == "LONG" and b["c"] - b["upper"] > CLIMAX_ATR * b["atr"]
        if not b or p["opened"] == day:
            continue                                          # entered at today's close: today's high/low came BEFORE the entry
        if lock_applies(book, p["side"]) or free_floor(book, p):
            set_lock_stop(book, p, b["h"], b["l"], 0.0 if book.get("lock_intraday") else 24.0)
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
    TOP_OI.clear()
    TOP_OI.update(ranked[:100])
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


def fetch_intraday(info, coins, history_days, interval=INTRADAY_INTERVAL):
    """{coin: {date: [4h (or 1h) bars in time order]}} for the lock_intraday books. Paced for Hyperliquid's 1200 weight/minute."""
    hours = INTERVAL_HOURS[interval]
    days = min(history_days + 2, int(4990 * hours / 24))      # Hyperliquid keeps the last 5000 candles
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400000
    weight = 20 + days * 24 / hours / 60                      # candleSnapshot weight = 20 + candles/60
    out = {}
    for coin in coins:
        time.sleep(max(0.5, weight / 1000 * 60))               # stay under ~1000 weight/minute
        candles = None
        for attempt in range(4):
            try:
                candles = info.candles_snapshot(coin, interval, start_ms, now_ms)
                break
            except Exception:
                time.sleep(15 * (attempt + 1))                 # 429 / network: back off
        if not candles:
            continue
        by_day, vols = {}, []
        for k in sorted(candles, key=lambda k: int(k["t"])):
            t = int(k["t"])
            if t + hours * 3600000 > now_ms:
                continue
            d = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            v = float(k.get("v", 0) or 0)
            va = sum(vols[-KNIFE_LOOKBACK:]) / KNIFE_LOOKBACK if len(vols) >= KNIFE_LOOKBACK else None
            vols.append(v)
            by_day.setdefault(d, []).append({"t": t, "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": v, "va": va})
        out[coin] = by_day
    add_intraday_indicators(out)
    return out


SMART_BARS = 6                         # smart exits look back 6 intraday candles (24h of 4h candles)


def add_intraday_indicators(out):
    """Per intraday candle: EMA20 (+ its previous value), Wilder ATR14, lowest low / highest high of the last 6 candles,
    6-candle return, and that return minus BTC's over the same candles (rs)."""
    for coin, by_day in out.items():
        bars = [k for d in sorted(by_day) for k in by_day[d]]
        ema = atr = None
        for i, k in enumerate(bars):
            k["ema_prev"] = ema
            ema = k["c"] if ema is None else ema + (k["c"] - ema) * 2 / 21
            k["ema"] = ema if i >= 20 else None
            if k["ema_prev"] is not None and i <= 20:
                k["ema_prev"] = None
            tr = k["h"] - k["l"] if i == 0 else max(k["h"] - k["l"], abs(k["h"] - bars[i - 1]["c"]), abs(k["l"] - bars[i - 1]["c"]))
            atr = tr if atr is None else atr + (tr - atr) / ATR_PERIOD
            k["atr"] = atr if i >= ATR_PERIOD else None
            win = bars[max(0, i - SMART_BARS + 1):i + 1]
            k["ll6"], k["hh6"] = min(x["l"] for x in win), max(x["h"] for x in win)
            k["r6"] = k["c"] / bars[i - SMART_BARS]["c"] - 1 if i >= SMART_BARS else None
    btc = {k["t"]: k["r6"] for d in out.get("BTC", {}).values() for k in d}
    for coin, by_day in out.items():
        for d in by_day.values():
            for k in d:
                b = btc.get(k["t"])
                k["rs"] = k["r6"] - b if k.get("r6") is not None and b is not None else None


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


ALPACA_TRADE = os.getenv("APCA_TRADE_URL", "https://paper-api.alpaca.markets")
FUND_WORDS = ("ETF", "ETN", " FUND", "TRUST", "ISHARES", "PROSHARES", "SPDR", "INVESCO", "DIREXION", "VANGUARD", "WISDOMTREE",
              "GRANITESHARES", "LEVERAGED", " 2X", " 3X", "ULTRA", "INVERSE", "BITCOIN", "ETHER", "ACQUISITION CORP")


def wide_symbols(now):
    """stocks_wide: pick the universe by rule from every active, tradable Alpaca US stock on a main exchange."""
    import requests
    headers = {"APCA-API-KEY-ID": os.getenv("APAL_API_KEY_ID", ""), "APCA-API-SECRET-KEY": os.getenv("APAL_SECRET_KEY", "")}
    res = requests.get(f"{ALPACA_TRADE}/v2/assets", params={"status": "active", "asset_class": "us_equity"}, headers=headers, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"Alpaca assets list failed ({res.status_code}): {res.text[:200]}")
    cands = sorted({a["symbol"] for a in res.json()
                    if a.get("tradable") and a.get("exchange") in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
                    and a["symbol"].isalpha() and not any(w in (a.get("name") or "").upper() for w in FUND_WORDS)})
    print(f"Wide stocks: {len(cands)} candidate symbols from Alpaca", flush=True)
    dv = {}
    for i in range(0, len(cands), 200):                       # last ~30 days only, to rank by liquidity
        raw = alpaca_bars(cands[i:i + 200], "1Day", now - timedelta(days=45), now - timedelta(minutes=16))
        for sym, bl in raw.items():
            bl = sorted(bl, key=lambda k: k["t"])[-20:]
            if len(bl) >= 15 and float(bl[-1]["c"]) >= 5:
                dv[sym] = float(np.mean([float(k["c"]) * float(k.get("v", 0)) for k in bl]))
    picked = [s for s in sorted(dv, key=dv.get, reverse=True) if dv[s] >= 5e6][:WIDE_SIZE]
    print(f"Wide stocks: {len(picked)} picked (price >= $5, dollar volume >= $5M/day, top {WIDE_SIZE})", flush=True)
    return picked


def fetch_stock_market(history_days):
    """Same shape as fetch_market: (ranked symbols, {symbol: {date: bar}}). Rank = average dollar volume, last 20 days."""
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=16)
    start = now - timedelta(days=history_days + 340)          # + ~230 trading days for the channel warm-up
    if WIDE:
        syms = wide_symbols(now)
        syms = syms + [BENCH] if BENCH not in syms else syms
        raw = {}
        for i in range(0, len(syms), 100):
            raw.update(alpaca_bars(syms[i:i + 100], "1Day", start, end))
    else:
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
    if WIDE:
        LARGE_CAPS.clear()
        LARGE_CAPS.update([s for s in ranked if s not in ("SPY", "QQQ")][:100])
    print(f"Stocks: {len(data)} symbols with daily bars (feed {_feed['name']})", flush=True)
    return ranked, data


def fetch_stock_intraday(coins, history_days):
    """{symbol: {date: [regular-hours hourly bars in order]}} for the lock_intraday books."""
    now = datetime.now(timezone.utc)
    raw, coins = {}, list(coins)
    for i in range(0, len(coins), 8):                        # small batches: a long history cannot hit the page limit
        raw.update(alpaca_bars(coins[i:i + 8], "1Hour", now - timedelta(days=history_days + 3), now - timedelta(minutes=16)))
    print(f"Hourly stock bars for {sum(1 for c in coins if raw.get(c))}/{len(coins)} symbols", flush=True)
    out = {}
    for sym, bl in raw.items():
        by_day, vols = {}, []
        for k in sorted(bl, key=lambda k: k["t"]):
            t = _et(k["t"])
            if not (9 <= t.hour <= 15):                          # regular session (the 9:00 bar holds the 9:30 open)
                continue
            v = float(k.get("v", 0) or 0)
            va = sum(vols[-KNIFE_LOOKBACK:]) / KNIFE_LOOKBACK if len(vols) >= KNIFE_LOOKBACK else None
            vols.append(v)
            by_day.setdefault(t.strftime("%Y-%m-%d"), []).append({"o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]),
                                                                  "v": v, "va": va})
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
    """1.68: measured results only (no projections). Replaces the old growth calculator, which scaled a few days of results to
    $10k-$200k accounts and 'capital needed for $200/day' - guesses, not facts."""
    rows, min_days = "", None
    for book in books:
        start = book_start(book)
        s = book_stats(book)
        nav = book_nav(book, prices) if prices else s["nav"]
        days = len(book["equity"])
        if not days or start <= 0:
            continue
        min_days = days if min_days is None else min(min_days, days)
        realised = sum(t["pnl"] for t in book["closed"])
        open_pnl = nav - start - realised                       # what the open positions are worth right now
        rows += (f"<tr><td><b>{book['code']}</b></td><td>{days}</td>"
                 f"<td style='color:{pct_color(nav / start - 1)};'>{(nav / start - 1) * 100:+.2f}%</td>"
                 f"<td>{s['trades']} ({s['wins']}W / {s['losses']}L)</td>"
                 f"<td>{s['avg_w']:+.2f}% / {s['avg_l']:+.2f}%</td>"
                 f"<td style='color:{pct_color(realised)};'>${realised:+,.2f}</td>"
                 f"<td style='color:{pct_color(open_pnl)};'>${open_pnl:+,.2f}</td>"
                 f"<td style='color:#b91c1c;'>{s['mdd']:.1f}%</td></tr>")
    if not rows:
        return ""
    young = (f"<div style='color:#b45309;margin-top:6px;'>Only {min_days} day(s) of results so far, too few trades to judge the strategy.</div>"
             if min_days is not None and min_days < GROWTH_MIN_DAYS else "")
    return f"""
    <div style="border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:11px;margin-bottom:14px;overflow-x:auto;">
      <div style="font-weight:700;text-transform:uppercase;font-size:10px;color:#0f172a;">📊 Results so far (measured, no projections)</div>
      <table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="3">
        <tr style="background:#f1f5f9;"><th align="left">Book</th><th align="left">Days</th><th align="left">Return</th><th align="left">Closed trades</th>
        <th align="left">Avg win / loss</th><th align="left">Realised $</th><th align="left">Open positions $</th><th align="left">Worst drop so far</th></tr>
        {rows}</table>
      <div style="color:#64748b;margin-top:6px;">Per paper book ($ amounts are on the book's own balance). Only what has actually happened is shown: no yearly pace,
        no what-if account sizes. Fills at the daily close, fees included, funding not modelled.</div>{young}
    </div>"""


def fmt_stop(p):
    return "—" if p.get("stop") is None else f"{p['stop']:.6g}"


def pct_color(v):
    return "#15803d" if v >= 0 else "#b91c1c"


def real_nav():
    """Paper emails show every book scaled to the REAL live account (the live bot's starting balance in state.json)."""
    try:
        with open("state.json") as f:
            st = json.load(f)
            v = float(st.get("start_nav") or 0) + float(st.get("net_deposits") or 0)   # real money in (start + deposits)
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


NOTES = ([
    "Book F5 is the paper twin of the Alpaca stock bot: daily Gaussian Channel on ~100 large US stocks, buy a close above the upper band "
    "(4.8% of the account if the breakout is 25 days old or less, else 1.2%), add up to 3.3% on +10% winners, sell on a close back below the band, "
    "-5% loss stop, long-only, cash only. Fills at the daily close.",
    "All other books stay in gc_paper.py for tests (mode robust); they are not shown here.",
] if STOCKS else [
    "Book F25L5 is the paper twin of the live crypto bot (LS15-LIVE-1.10.2): LS-15 daily Gaussian Channel signals on the top 100 coins by open "
    "interest, up to 9 positions of 15% each, up to 135% of the account in use. Early profit lock (keeps 50-90% of the peak gain from +3%), "
    "volume-fade exit, -4% loss stop on every position. In a strong BTC downtrend shorts run free (half banked at +20%, floor from +25%). "
    "A long banks half at +5%.",
    "It only takes signals from its own start date on, so it does not hold positions the live bot opened earlier.",
    "Gaussian Channel from Hyperliquid daily candles (hlc3, 4 poles, period 144, x1.414). Rank = open interest. Fills at the daily close "
    "(lock stops at the stop price, or at the open if it gapped through), 0.045% fee per side, funding not modelled. Orders under $10 are skipped.",
    "All other books stay in gc_paper.py for tests (mode robust); they are not shown here.",
])


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
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=BACKTEST_DAYS)).strftime("%Y-%m-%d")
    end = (now - timedelta(days=BACKTEST_SKIP_DAYS)).strftime("%Y-%m-%d") if BACKTEST_SKIP_DAYS else "9999"
    return [d for d in all_days if cutoff <= d < end]


def load_market(info, history_days):
    return fetch_stock_market(history_days) if STOCKS else fetch_market(info, history_days)


def load_hourly(info, coins, history_days, codes):
    """Fill DATA_1H when a book uses the hourly lock (crypto only; stock 'intraday' bars are hourly already)."""
    DATA_1H.clear()
    if not STOCKS and any(BOOK_OPTIONS.get(c, {}).get("lock_interval") == "1h" for c in codes):
        DATA_1H.update(fetch_intraday(info, coins, history_days, "1h"))


def load_intraday(info, coins, history_days):
    return fetch_stock_intraday(coins, history_days) if STOCKS else fetch_intraday(info, coins, history_days)


def load_funding(info, universe, data, days):
    """Fill FUNDING_DAY with the average hourly funding rate per day of FUNDING_COINS coins spread across the universe."""
    global FUNDING_NOTE
    FUNDING_DAY.clear()
    if STOCKS or not FUNDING or not days:
        FUNDING_NOTE = "" if STOCKS else "Funding not modelled (FUNDING=off)."
        return
    step = max(1, len(universe) // FUNDING_COINS)
    sample = [c for c in universe[::step] if c != BENCH][:FUNDING_COINS]
    start0 = int(datetime.strptime(days[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) - 86400000
    end_ms = int(datetime.strptime(days[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) + 2 * 86400000
    per_day, got = {}, []
    for coin in sample:
        start, rows = start0, 0
        while start < end_ms:
            res = None
            for attempt in range(5):
                try:
                    res = info.funding_history(coin, start, end_ms)
                    break
                except Exception as e:
                    print(f"funding {coin}: {e} (retry {attempt + 1})", flush=True)
                    time.sleep(10 * (attempt + 1))
            time.sleep(2.4)                                   # stay under Hyperliquid's request-weight limit
            if not res:
                break
            for r in res:
                d = datetime.fromtimestamp(int(r["time"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
                per_day.setdefault(d, {}).setdefault(coin, []).append(float(r["fundingRate"]))
            rows += len(res)
            start = int(res[-1]["time"]) + 1
            if len(res) < 2:
                break
        print(f"funding {coin}: {rows} hourly rates", flush=True)
        if rows:
            got.append(coin)
    for d, by_coin in per_day.items():
        means = [sum(v) / len(v) for v in by_coin.values() if v]
        if means:
            FUNDING_DAY[d] = sum(means) / len(means)
    vals = [FUNDING_DAY[d] for d in days if d in FUNDING_DAY]
    crash = [FUNDING_DAY[d] for d in days if d in FUNDING_DAY and (data.get(BENCH, {}).get(d) or {}).get("_prev")
             and data[BENCH][d]["_prev"].get("valid") and data[BENCH][d]["_prev"]["c"] < data[BENCH][d]["_prev"]["filter"] * 0.95]
    avg = lambda v: sum(v) / len(v) * 24 * 100 if v else 0.0
    FUNDING_NOTE = (f"Funding modelled: average of {len(got)} coins ({', '.join(got)}), {len(vals)} of {len(days)} days covered. "
                    f"Average {avg(vals):+.3f}%/day (longs pay when positive, shorts when negative); on strong-downtrend days "
                    f"{(format(avg(crash), '+.3f') + '%/day') if crash else 'n/a'}. Coin-specific spikes are not included (a crowded coin can cost more).")
    print(FUNDING_NOTE, flush=True)


def run_backtest(info):
    ranked, data = load_market(info, BACKTEST_DAYS)
    universe = ranked[:UNIVERSE_SIZE]
    all_days = sorted({d for c in data for d in data[c]})
    days = backtest_days(all_days)
    books = [new_book(code, name, lock, guard) for code, name, lock, guard in
             sorted((b for b in BOOKS if b[0] in PAPER_BOOKS), key=lambda b: PAPER_BOOKS.index(b[0]))]
    data4h = load_intraday(info, universe, BACKTEST_DAYS) if needs_intraday(books) else {}
    load_hourly(info, universe, BACKTEST_DAYS, [b["code"] for b in books])
    load_funding(info, universe, data, days)
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
    unknown = [c for c in ROBUST_BOOKS if c not in {b[0] for b in BOOKS}]
    if not specs:
        raise RuntimeError(f"ROBUST_BOOKS {ROBUST_BOOKS} matches no book")
    data4h = load_intraday(info, universe, BACKTEST_DAYS) if any(BOOK_OPTIONS.get(c, {}).get("lock_intraday") for c, *_ in specs) else {}
    load_hourly(info, universe, BACKTEST_DAYS, [c for c, *_ in specs])
    load_funding(info, universe, data, days)
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
    ref = "Z4" if "Z4" in results else ("D" if "D" in results else None)
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
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{days[0]} → {last} • {BENCH} buy &amp; hold {btc_ret:+.1f}% • market {MARKET}: {len(universe)} {'stocks' if STOCKS else 'coins'} in the test • {ENGINE_VERSION}</div></div>
  <div style="padding:12px;font-size:12px;">
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin-bottom:12px;">
      📝 PAPER ONLY. Same coins, same candles, same rules — only the ORDER in which same-day signals are considered changes
      (run 1 = normal open-interest order, runs 2-{ROBUST_RUNS} = shuffled). A real edge keeps a good MEDIAN and a tolerable WORST case;
      a lucky one swings wildly.</div>
    <div style="overflow-x:auto;"><table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="4">
      <tr style="background:#f1f5f9;"><th align="left">Book</th><th align="left">Normal order</th><th align="left">Median</th><th align="left">Worst</th>
      <th align="left">Best</th><th align="left">Median drop</th><th align="left">Worst drop</th><th align="left">Beat {BENCH}</th><th align="left">Beat {ref or "D"} (same order)</th></tr>
      {rows}</table></div>
    {f"<div style='background:#fee2e2;border:1px solid #fecaca;border-radius:6px;padding:8px;color:#991b1b;margin-top:10px;'>⚠️ Books not found in this gc_paper.py ({ENGINE_VERSION}): <b>{', '.join(unknown)}</b> — skipped. Is the newest gc_paper.py committed?</div>" if unknown else ""}
    {f"<div style='font-size:11px;color:#475569;margin-top:8px;'>💸 {FUNDING_NOTE}</div>" if FUNDING_NOTE else ""}
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
    keep = sorted((b for b in BOOKS if b[0] in PAPER_BOOKS), key=lambda b: PAPER_BOOKS.index(b[0]))
    saved = {b["code"]: b for b in state.get("books") or [] if b["code"] in PAPER_BOOKS}   # retired books are dropped
    books = [saved.get(code) or new_book(code, name, lock, guard) for code, name, lock, guard in keep]
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
    if todo:
        load_hourly(info, [c for c in ranked[:DRIFT_OUT_RANK] if c in data or c in held], 10, [b["code"] for b in books])
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
        json.dump(state, f, indent=1, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o))

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


# ---------------- ROSS scalper test (MODE=ross): the old V4.8.12 rules replayed on 5m / 15m / 30m / 1h / 4h ----------------
# Paper only, like everything in this file. Entries are the V4.8.12 rules exactly (top 20 gainers outside the first 20 listed
# coins, $500k+ 24h volume, HOD breakout / bullish reclaim / V-bottom with RVOL 1.8x, VWAP, EMA9 and MACD), checked at each
# candle close. Two exit books per timeframe:
#   R  = V4.8.12 exits as written: 5x, -3.5% ROE stop (-0.7% price), lock 80/85/90/93% of the peak ROE from +1.2% ROE,
#        climax lock, 15-minute scratch (< -0.5% ROE) and 30-minute flat exit (< +0.4% ROE)
#   RS = the same exits stretched to the timeframe: every distance x sqrt(timeframe / 15m) (price swings grow with the square
#        root of time), so a 4h candle gets 4x the room of a 15m one (stop -2.8% price instead of -0.7%)
ROSS_TFS = [t.strip() for t in os.getenv("ROSS_TIMEFRAMES", "5m,15m,30m,1h,4h").split(",") if t.strip()]
ROSS_TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
ROSS_COINS = int(os.getenv("ROSS_COINS", "50") or 50)  # today's most-traded coins outside the first 20 listed (majors)
ROSS_LEV = 5
ROSS_SIZE = 0.25 * 0.85                                 # 21.25% of the account per trade, as V4.8.12
ROSS_SLOTS = 3
ROSS_MIN_ORDER = 1.0 if STOCKS else 10.5
ROSS_WINDOW = 32                                        # V4.8.12 read 32 candles (8 hours of 15m)
ROSS_RVOL, ROSS_VWAP_CAP, ROSS_HOD = 1.80, 1.0080, 0.0100
MAKER_FEE = 0.0001 if STOCKS else 0.00015               # limit entries (stocks: no commission, a little spread)
# Stocks: Alpaca keeps years of intraday bars; use about 5,000 regular-hours candles per size (as crypto), at most 2 years.
ROSS_STOCK_DAYS = {"5m": 90, "15m": 270, "30m": 540, "1h": 730, "4h": 730}


def _ema_weights(n, span):
    """Weights w so that w @ x[-n:] = pandas ewm(span, adjust=False).mean() of the n values, at the last one."""
    a = 2.0 / (span + 1)
    w = np.array([a * (1 - a) ** (n - 1 - k) for k in range(n)])
    w[0] = (1 - a) ** (n - 1)
    return w


def _ema_matrix(n, span):
    """Row i = weights of the ewm(adjust=False) value at position i of an n-long window (lower triangular)."""
    return np.array([np.concatenate([_ema_weights(i + 1, span), np.zeros(n - i - 1)]) for i in range(n)])


_N = ROSS_WINDOW
_W_EMA9 = _ema_weights(_N, 9)
_W_EMA9_PREV = np.concatenate([_ema_weights(_N - 1, 9), [0.0]])
_W_HIST = (np.eye(_N) - _ema_matrix(_N, 9))[-1] @ (_ema_matrix(_N, 12) - _ema_matrix(_N, 26))   # MACD histogram is linear


def ross_fetch(info, coins, tf):
    """{coin: dict of numpy arrays t,o,h,l,c,v} - the last 5,000 candles (all Hyperliquid keeps), paced like fetch_intraday."""
    mins = ROSS_TF_MIN[tf]
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 5000 * mins * 60000
    out = {}
    for coin in coins:
        time.sleep(6.3)                                        # 20 + 5000/60 weight per call: stay under ~1000 weight/minute
        candles = None
        for attempt in range(5):
            try:
                candles = info.candles_snapshot(coin, tf, start_ms, now_ms)
                break
            except Exception:
                time.sleep(15 * (attempt + 1))
        if not candles:
            continue
        ks = [k for k in sorted(candles, key=lambda k: int(k["t"])) if int(k["t"]) + mins * 60000 <= now_ms]
        if len(ks) < ROSS_WINDOW + 300:
            continue
        out[coin] = {f: np.array([float(k[f]) for k in ks]) for f in "ohlcv"}
        out[coin]["t"] = np.array([int(k["t"]) for k in ks], dtype=np.int64)
    return out


def ross_fetch_stocks(symbols, tf):
    """Same shape as ross_fetch, from Alpaca: regular-hours bars only. 4h = two half-day bars (9:30-13:00, 13:00-16:00)."""
    mins = ROSS_TF_MIN[tf]
    now = datetime.now(timezone.utc)
    tf_api = {"5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour", "4h": "1Hour"}[tf]
    raw = {}
    symbols = list(symbols)
    for i in range(0, len(symbols), 8):                       # small batches: one request series per batch, so a long
        raw.update(alpaca_bars(symbols[i:i + 8], tf_api,     # history can never run into the page limit and drop symbols
                               now - timedelta(days=ROSS_STOCK_DAYS[tf]), now - timedelta(minutes=16)))
    missing = [s for s in symbols if not raw.get(s)]
    print(f"{tf}: Alpaca bars for {len(symbols) - len(missing)}/{len(symbols)} symbols" + (f", missing {missing}" if missing else ""), flush=True)
    out = {}
    for sym, bl in raw.items():
        rows = []
        for k in sorted(bl, key=lambda k: k["t"]):
            t = _et(k["t"])
            hm = t.hour * 60 + t.minute
            if tf_api == "1Hour":
                if not 9 <= t.hour <= 15:                    # the 9:00 bar holds the 9:30 open
                    continue
            elif not 570 <= hm < 960:
                continue
            row = {"t": int(t.timestamp() * 1000), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]),
                   "c": float(k["c"]), "v": float(k.get("v", 0))}
            if tf == "4h":
                key = (t.strftime("%Y-%m-%d"), t.hour >= 13)
                if rows and rows[-1]["key"] == key:
                    r = rows[-1]
                    r["h"], r["l"], r["c"], r["v"] = max(r["h"], row["h"]), min(r["l"], row["l"]), row["c"], r["v"] + row["v"]
                    continue
                row["key"] = key
                row["t"] = int(t.replace(hour=13 if key[1] else 9, minute=0 if key[1] else 30).timestamp() * 1000)
            rows.append(row)
        if len(rows) < ROSS_WINDOW + 300:
            continue
        out[sym] = {f: np.array([r[f] for r in rows]) for f in "ohlcv"}
        out[sym]["t"] = np.array([r["t"] for r in rows], dtype=np.int64)
    return out


def ross_align(raw):
    """Put every coin on one time grid (NaN where a coin has no candle)."""
    grid = np.array(sorted({int(t) for d in raw.values() for t in d["t"]}), dtype=np.int64)
    pos = {t: i for i, t in enumerate(grid)}
    out = {}
    for coin, d in raw.items():
        idx = np.array([pos[int(t)] for t in d["t"]])
        out[coin] = {}
        for f in "ohlcv":
            a = np.full(len(grid), np.nan)
            a[idx] = d[f]
            out[coin][f] = a
    return grid, out


def ross_signals(d, mins, b24=None):
    """Per-candle V4.8.12 entry signals for one coin (arrays over the time grid)."""
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    n = len(c)
    sig = {k: np.full(n, np.nan) for k in ("ema9", "vwap", "score", "chg24", "vol24")}
    sig["setup"] = np.zeros(n, dtype=np.int8)                 # 1 breakout, 2 reclaim, 3 V-bottom
    if n <= _N + 2:
        return sig
    from numpy.lib.stride_tricks import sliding_window_view
    win = sliding_window_view(c, _N)                          # row j = candles j .. j+N-1, i.e. ending at t = j+N-1
    t_idx = np.arange(_N - 1, n)
    ema9 = win @ _W_EMA9
    prev_ema9 = win @ _W_EMA9_PREV
    hist = win @ _W_HIST
    tp = (h + l + c) / 3.0
    vw_num = sliding_window_view(tp * v, _N).sum(axis=1)
    vw_den = sliding_window_view(v, _N).sum(axis=1)
    vwap = np.where(vw_den > 0, vw_num / np.where(vw_den > 0, vw_den, 1), c[t_idx])
    day_high = sliding_window_view(h, _N).max(axis=1)
    cc = c[t_idx]
    prev_close = c[t_idx - 1]
    vavg = np.array([np.mean(v[t - 11:t - 1]) if t >= 11 else np.nan for t in t_idx])   # live: volumes[-12:-2]
    vr = np.where(vavg > 0, v[t_idx] / np.where(vavg > 0, vavg, 1), 1.0)
    min3 = np.array([np.min(l[t - 3:t]) for t in t_idx])
    rng = np.maximum(h[t_idx] - l[t_idx], 1e-12)
    lower_wick = (np.minimum(o[t_idx], cc) - l[t_idx]) / rng
    with np.errstate(invalid="ignore"):
        ext_ok = (cc - ema9) / ema9 <= 0.0100
        above_ema, above_vwap, macd_pos = cc > ema9, cc > vwap, hist > 0
        vwap_ok = cc / vwap <= ROSS_VWAP_CAP
        vol_ok = vr >= ROSS_RVOL
        breakout = (cc >= day_high * (1 - ROSS_HOD)) & vol_ok & vwap_ok & above_ema & above_vwap & macd_pos
        reclaim = (((prev_close <= prev_ema9) | (prev_close <= vwap)) & above_ema & above_vwap & (cc <= ema9 * 1.0025)
                   & vol_ok & vwap_ok & macd_pos)
        vbottom = (l[t_idx] <= min3) & (lower_wick >= 0.45) & vol_ok & vwap_ok & above_ema & macd_pos
    setup = np.where(vbottom, 3, np.where(reclaim, 2, np.where(breakout, 1, 0))) * ext_ok
    score = np.where(setup == 3, vr * 3.2, np.where(setup == 2, vr * 2.4, vr))
    b24 = b24 or max(1, 1440 // mins)                         # candles in one day (stocks: one trading day)
    chg = np.full(n, np.nan)
    chg[b24:] = c[b24:] / c[:-b24] - 1
    usd = np.nan_to_num(c * v)
    cs = np.concatenate([[0.0], np.cumsum(usd)])
    vol24 = np.full(n, np.nan)
    vol24[b24 - 1:] = cs[b24:] - cs[:-b24]
    bad = np.isnan(win).any(axis=1)
    setup = np.where(bad, 0, setup)
    sig["ema9"][t_idx], sig["vwap"][t_idx], sig["score"][t_idx] = ema9, vwap, score
    sig["setup"][t_idx] = setup
    sig["chg24"], sig["vol24"] = chg, vol24
    return sig


def ross_stop(entry, close, peak_roe, wick, s):
    """V4.8.12 calculate_smart_exchange_targets for a long, with every ROE distance multiplied by s (R: s = 1)."""
    roe = (close - entry) / entry * ROSS_LEV
    if peak_roe >= 0.12 * s:
        floor = max(peak_roe * 0.93, peak_roe - 0.008 * s)
    elif peak_roe >= 0.08 * s:
        floor = max(peak_roe * 0.90, peak_roe - 0.010 * s)
    elif peak_roe >= 0.05 * s:
        floor = max(peak_roe * 0.85, peak_roe - 0.012 * s)
    elif peak_roe >= 0.025 * s:
        floor = max(peak_roe * 0.80, 0.008 * s)
    elif peak_roe >= 0.012 * s:
        floor = 0.004 * s
    else:
        floor = -0.035 * s
    if peak_roe >= 0.045 * s and (peak_roe - roe >= 0.006 * s or wick >= 0.45):
        floor = max(floor, peak_roe - 0.004 * s)
    return entry * (1 + floor / ROSS_LEV), floor


def ross_simulate(grid, data, sigs, mins, s):
    """One book over one timeframe. Returns (equity curve, closed trades, fees paid)."""
    coins = list(data)
    n = len(grid)
    cash = START_NAV                                          # realised account value (margin is not tracked: 3 x 21% at 5x)
    pos, pending, trades, fees = {}, None, [], 0.0
    cooldown, recent_losses, paused_until = {}, [], 0
    scratch_bars = max(1, round(15 / mins))
    flat_bars = max(2, round(30 / mins))
    curve = []
    for t in range(n):
        now = int(grid[t])
        # 1) yesterday's resting limit entry: fills if this candle trades down to it, else it is purged (as the live bot did)
        if pending:
            cn, lim, setup = pending
            d = data[cn]
            if not np.isnan(d["l"][t]) and d["l"][t] <= lim and len(pos) < ROSS_SLOTS:
                px = min(d["o"][t], lim)
                usd = max(ROSS_MIN_ORDER, (curve[-1] if curve else cash) * ROSS_SIZE)
                fee = usd * MAKER_FEE
                cash -= fee
                fees += fee
                pos[cn] = {"entry": px, "qty": usd / px, "peak": 0.0, "stop": px * (1 - 0.035 * s / ROSS_LEV), "t0": t,
                           "setup": setup, "fee_in": fee, "last": px}
            pending = None
        # 2) manage open longs on this candle
        for cn in list(pos):
            p, d = pos[cn], data[cn]
            if t <= p["t0"] or np.isnan(d["c"][t]):
                continue
            o_, h_, l_, c_ = d["o"][t], d["h"][t], d["l"][t], d["c"][t]
            p["last"] = c_
            exit_px, why = None, None
            if l_ <= p["stop"]:
                exit_px = min(o_, p["stop"])
                why = "Stop / profit lock (resting)" if p["peak"] >= 0.012 * s else "Hard stop"
            else:
                p["peak"] = max(p["peak"], (h_ - p["entry"]) / p["entry"] * ROSS_LEV)
                rng = max(h_ - l_, 1e-12)
                wick = (h_ - max(o_, c_)) / rng
                stop, _ = ross_stop(p["entry"], c_, p["peak"], wick, s)
                p["stop"] = max(p["stop"], stop)
                roe = (c_ - p["entry"]) / p["entry"] * ROSS_LEV
                held = t - p["t0"]
                if c_ <= p["stop"]:
                    exit_px, why = c_, "Profit lock (closed under the new stop)"
                elif held >= scratch_bars and roe < -0.005 * s:
                    exit_px, why = c_, "15-minute scratch"
                elif held >= flat_bars and roe < 0.004 * s:
                    exit_px, why = c_, "30-minute flat exit"
            if exit_px is None:
                continue
            fee = p["qty"] * exit_px * FEE_RATE
            pnl = p["qty"] * (exit_px - p["entry"]) - fee - p["fee_in"]
            cash += p["qty"] * (exit_px - p["entry"]) - fee
            fees += fee
            trades.append({"coin": cn, "pnl": pnl, "pct": (exit_px / p["entry"] - 1) * 100, "why": why, "setup": p["setup"], "t": now})
            cooldown[cn] = now + (4 * 3600000 if pnl > 0 else 1800000)
            if pnl < 0:
                recent_losses.append((now, pnl))
            del pos[cn]
        # 3) circuit breaker: 3+ losses totalling -$1 or worse inside 60 minutes -> 12 hours without new entries
        recent_losses = [(tt, pl) for tt, pl in recent_losses if tt >= now - 3600000]
        if len(recent_losses) >= 3 and sum(pl for _, pl in recent_losses) <= -1.0:
            paused_until, recent_losses = now + 12 * 3600000, []
        # 4) scan at this candle's close: top 20 gainers ($500k+ day volume), best setups fill the free slots
        if now >= paused_until and len(pos) < ROSS_SLOTS:
            ranked = sorted((c for c in coins if not np.isnan(sigs[c]["chg24"][t]) and (sigs[c]["vol24"][t] or 0) >= 500000),
                            key=lambda c: sigs[c]["chg24"][t], reverse=True)[:20]
            cands = sorted((c for c in ranked if sigs[c]["setup"][t] and c not in pos and cooldown.get(c, 0) <= now),
                           key=lambda c: sigs[c]["score"][t], reverse=True)
            for cn in cands[:ROSS_SLOTS - len(pos)]:
                d, sg = data[cn], sigs[cn]
                c_ = d["c"][t]
                if sg["setup"][t] == 1:                       # HOD breakout: market order at the close
                    usd = max(ROSS_MIN_ORDER, (curve[-1] if curve else cash) * ROSS_SIZE)
                    fee = usd * FEE_RATE
                    cash -= fee
                    fees += fee
                    pos[cn] = {"entry": c_, "qty": usd / c_, "peak": 0.0, "stop": c_ * (1 - 0.035 * s / ROSS_LEV), "t0": t,
                               "setup": 1, "fee_in": fee, "last": c_}
                elif pending is None:                         # reclaim / V-bottom: resting limit near EMA9 / VWAP
                    pending = (cn, min(c_ * 0.999, max(sg["ema9"][t], sg["vwap"][t])), int(sg["setup"][t]))
        eq = cash + sum(p["qty"] * (p["last"] - p["entry"]) for p in pos.values())
        curve.append(eq)
    return curve, trades, fees


def ross_stats(curve, trades, fees, days):
    eq = np.array(curve) if curve else np.array([START_NAV])
    peak = np.maximum.accumulate(eq)
    mdd = float(np.min(eq / peak - 1) * 100)
    mid = len(eq) // 2
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in loss)
    why = {}
    for t in trades:
        why[t["why"]] = why.get(t["why"], 0) + 1
    by_setup = {}
    for t in trades:
        k = {1: "Breakout", 2: "Reclaim", 3: "V-bottom"}.get(t["setup"], "?")
        by_setup[k] = by_setup.get(k, 0.0) + t["pnl"]
    return {"ret": (eq[-1] / START_NAV - 1) * 100, "mdd": mdd, "n": len(trades), "per_day": len(trades) / max(days, 1e-9),
            "win": len(wins) / len(trades) * 100 if trades else 0.0,
            "avg_w": float(np.mean([t["pct"] for t in wins])) if wins else 0.0,
            "avg_l": float(np.mean([t["pct"] for t in loss])) if loss else 0.0,
            "pf": gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0), "fees": fees,
            "pnl_before_fees": eq[-1] - START_NAV + fees,
            "h1": (eq[mid] / START_NAV - 1) * 100, "h2": (eq[-1] / eq[mid] - 1) * 100, "why": why, "setup": by_setup}


def run_ross(info):
    if STOCKS:
        coins = [c for c in STOCK_UNIVERSE if c != BENCH]
    else:
        meta, ctxs = info.meta_and_asset_ctxs()
        uni = meta.get("universe", [])
        pool = [(uni[i]["name"], float(ctxs[i].get("dayNtlVlm", 0) or 0)) for i in range(20, min(len(uni), len(ctxs)))
                if not uni[i].get("isDelisted")]
        coins = [c for c, _ in sorted(pool, key=lambda x: x[1], reverse=True)[:ROSS_COINS]]
    print(f"Ross test: {len(coins)} coins, timeframes {ROSS_TFS}", flush=True)
    rows, summary, notes_tf = "", [], []
    for tf in ROSS_TFS:
        mins = ROSS_TF_MIN[tf]
        raw = ross_fetch_stocks(coins + [BENCH], tf) if STOCKS else ross_fetch(info, coins + [BENCH], tf)
        btc = raw.pop(BENCH, None)
        if not raw:
            notes_tf.append(f"{tf}: no candles returned")
            continue
        grid, data = ross_align(raw)
        sigs = {c: ross_signals(data[c], mins, ({"4h": 2}.get(tf) or 390 // mins) if STOCKS else None) for c in data}
        days = (grid[-1] - grid[0]) / 86400000 + (1 if STOCKS else mins / 1440)
        btc_ret = (btc["c"][-1] / btc["c"][min(int(np.searchsorted(btc["t"], grid[0])), len(btc["c"]) - 1)] - 1) * 100 if btc is not None else None
        first = datetime.fromtimestamp(grid[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        n_sig = int(sum(int((sigs[c]["setup"] > 0).sum()) for c in sigs))
        for book, s in (("R", 1.0), ("RS", math.sqrt(mins / 15))):
            curve, trades, fees = ross_simulate(grid, data, sigs, mins, s)
            st = ross_stats(curve, trades, fees, days)
            pf = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
            why = ", ".join(f"{k} {v}" for k, v in sorted(st["why"].items(), key=lambda x: -x[1]))
            setups = ", ".join(f"{k} ${v:+,.0f}" for k, v in st["setup"].items())
            good = st["ret"] > 0 and st["h1"] > 0 and st["h2"] > 0 and st["pf"] > 1.2
            rows += (f"<tr style='background:{'#dcfce7' if good else '#fff'};'><td><b>{tf} {book}</b><br><span style='color:#64748b;'>{days:.0f} days from {first}</span></td>"
                     f"<td style='color:{pct_color(st['ret'])};font-weight:700;'>{st['ret']:+.1f}%</td>"
                     f"<td style='color:{pct_color(st['h1'])};'>{st['h1']:+.1f}%</td><td style='color:{pct_color(st['h2'])};'>{st['h2']:+.1f}%</td>"
                     f"<td style='color:#b91c1c;'>{st['mdd']:.1f}%</td><td>{st['n']} ({st['per_day']:.1f}/day)</td><td>{st['win']:.0f}%</td>"
                     f"<td>{st['avg_w']:+.2f}% / {st['avg_l']:+.2f}%</td><td>{pf}</td><td>${st['fees']:,.2f}</td>"
                     f"<td style='color:{pct_color(st['pnl_before_fees'])};'>${st['pnl_before_fees']:+,.2f}</td><td>{'—' if btc_ret is None else f'{btc_ret:+.1f}%'}</td></tr>"
                     f"<tr><td colspan='12' style='font-size:10px;color:#64748b;border-bottom:1px solid #e2e8f0;'>Exits: {why or 'none'} • "
                     f"P&amp;L by setup: {setups or 'none'}</td></tr>")
            summary.append(f"{tf} {book}: {st['ret']:+.1f}% ({st['n']} trades, PF {pf})")
            print(summary[-1], flush=True)
        notes_tf.append(f"{tf}: {len(data)} coins, {days:.0f} days, {n_sig} raw entry signals")
    title = f"ROSS SCALPER TEST{' — STOCKS' if STOCKS else ''} (V4.8.12 rules) — {', '.join(ROSS_TFS)} from ${START_NAV:,.0f}"
    market_note = (f"STOCKS: the ~{len(coins)} large US stocks of the stock bot (Alpaca, regular hours only; 4h = two half-day "
                   "candles). The ladder keeps the same PRICE distances (stop -0.7%), at 1x: 21% of the account per trade, "
                   "max 3, no margin. Day trades are NOT limited here: the real pattern-day-trader rule allows only 3 day trades "
                   "per 5 days under $25,000, so on a $1,000 account most of these trades could not be placed. "
                   "'Top 20 gainers' = biggest move over the last trading day. Spread/slippage 0.05% per market order. "
                   "Ross himself trades small-cap gappers ($2-20 stocks with news), which this universe does not include."
                   if STOCKS else
                   f"Coins = today's {ROSS_COINS} most-traded outside the first 20 listed, so coins that faded away are missing. "
                   "Fees 0.045% market / 0.015% limit entries.")
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f8;margin:0;padding:8px;color:#333;">
<div style="max-width:760px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
  <div style="background:#0f172a;color:#fff;padding:14px;"><div style="font-size:15px;font-weight:700;">{title}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{ENGINE_VERSION} • paper only, no keys, no orders</div></div>
  <div style="padding:12px;font-size:12px;">
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin-bottom:12px;">
      <b>R</b> = V4.8.12 exits as written (5x, -3.5% ROE stop = -0.7% price, keep 80-93% of the peak from +1.2% ROE, climax lock,
      15-minute scratch and 30-minute flat exits). <b>RS</b> = the same exits stretched to the timeframe (every distance x
      &radic;(timeframe / 15m): 5m x0.58, 30m x1.41, 1h x2, 4h x4). A green row = made money overall AND in both halves of the test
      with profit factor above 1.2. "Before fees" shows whether there is any edge at all before the exchange takes its cut.
      Longer timeframes cover more days: about 5,000 candles per size{' (at most 2 years)' if STOCKS else ' (all Hyperliquid keeps)'}.</div>
    <div style="overflow-x:auto;"><table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="4">
      <tr style="background:#f1f5f9;"><th align="left">Timeframe / book</th><th align="left">Return</th><th align="left">1st half</th>
      <th align="left">2nd half</th><th align="left">Worst drop</th><th align="left">Trades</th><th align="left">Win rate</th>
      <th align="left">Avg win / loss</th><th align="left">Profit factor</th><th align="left">Fees</th><th align="left">P&amp;L before fees</th>
      <th align="left">{BENCH} hold</th></tr>{rows}</table></div>
    <div style="font-size:10px;color:#64748b;margin-top:10px;">{' • '.join(notes_tf)}<br>
      Not modelled: the Gemini risk shield (normal thresholds used: RVOL 1.8x, VWAP +0.8%), the order-book spread/depth check,
      funding. Signals are checked once per candle close (the live bot looked inside the forming candle every 5-15 minutes).
      {market_note}</div>
  </div></div></body></html>"""
    text = f"{title}\n" + "\n".join(summary)
    print(text, flush=True)
    send_email(f"⚡ {title} — " + " | ".join(summary), html, text, None)


# ---------------- STOCK ROTATION test (MODE=rotation, market stocks_wide): two well-documented, low-trading ideas vs SPY ----------------
# Paper only. No hindsight in the picks: every decision uses only closes up to the PREVIOUS day and trades at today's close.
#   T200 = hold SPY while SPY closed above its 200-day average, otherwise sit in cash
#   M10 / M20 = once a month (every 21 trading days) hold the 10 / 20 stocks with the best 12-month return (skipping the last
#               month), picked from the 500 most-traded stocks AS OF THAT DAY, equal weight
#   M10T / M20T = the same, but everything goes to cash on days SPY is below its 200-day average
# Robust check: the monthly rebalance day is moved through all 21 possible days (21 runs); T200 and SPY have one answer.
ROT_UNIVERSE = 500
ROT_EVERY = 21
ROT_BOOKS = [("T200", "SPY, but cash when SPY is under its 200-day average", 0, True),
             ("M10", "Momentum: best 10 of the 500 most-traded stocks, monthly", 10, False),
             ("M20", "Momentum: best 20 of the 500 most-traded stocks, monthly", 20, False),
             ("M10T", "M10, but cash when SPY is under its 200-day average", 10, True),
             ("M20T", "M20, but cash when SPY is under its 200-day average", 20, True)]


def rot_simulate(data, all_days, start_i, end_i, top, trend, offset, spy_ok):
    """Returns (daily NAV list, number of trades). top=0: hold SPY. Decisions on day i use closes up to i-1."""
    cash, held, nav_curve, trades, last_px = 1000.0, {}, [], 0, {}
    for i in range(start_i, end_i):
        d, j = all_days[i], all_days[i - 1]
        for s in list(held) + (["SPY"] if top == 0 else []):
            b = data.get(s, {}).get(d)
            if b:
                last_px[s] = b["c"]
        nav = cash + sum(q * last_px[s] for s, q in held.items())
        on = spy_ok.get(j, False) if trend else True
        rebal = i == start_i or (i - start_i) % ROT_EVERY == offset or (trend and on and not held)
        target = None
        if not on and held:
            target = {}
        elif on and rebal:
            if top == 0:
                target = {"SPY": 1.0}
            else:
                pool = [s for s in data if s not in ("SPY", "QQQ") and (data[s].get(j) or {}).get("dv20") and data[s][j]["c"] >= 5]
                pool = sorted(pool, key=lambda s: -data[s][j]["dv20"])[:ROT_UNIVERSE]
                k12, k1 = all_days[i - 1 - 252] if i - 1 - 252 >= 0 else None, all_days[i - 1 - 21]
                score = {}
                for s in pool:
                    a, b_ = (data[s].get(k12) or {}).get("c") if k12 else None, (data[s].get(k1) or {}).get("c")
                    if a and b_ and d in data[s]:
                        score[s] = b_ / a - 1
                picks = sorted(score, key=lambda s: -score[s])[:top]
                target = {s: 1.0 / len(picks) for s in picks} if picks else {}
        if target is not None:
            for s in list(held):                               # sell what is no longer wanted (or trim)
                want = nav * target.get(s, 0.0)
                have = held[s] * last_px[s]
                if have > want + 1e-9:
                    sell = have - want
                    cash += sell * (1 - FEE_RATE)
                    held[s] -= sell / last_px[s]
                    trades += 1
                    if held[s] * last_px[s] < 0.01:
                        held.pop(s)
            for s, w in target.items():                        # then buy up to the target weights
                b = data.get(s, {}).get(d)
                if not b:
                    continue
                last_px[s] = b["c"]
                have = held.get(s, 0.0) * b["c"]
                buy = min(nav * w - have, cash / (1 + FEE_RATE))
                if buy > 1.0:
                    cash -= buy * (1 + FEE_RATE)
                    held[s] = held.get(s, 0.0) + buy / b["c"]
                    trades += 1
        nav_curve.append(cash + sum(q * last_px[s] for s, q in held.items()))
    return nav_curve, trades


def _mdd(curve):
    peak, worst = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def run_rotation():
    if not STOCKS:
        raise RuntimeError("rotation mode is a stock test: choose market stocks_wide")
    ranked, data = fetch_stock_market(BACKTEST_DAYS + 160)     # ~1 extra year of history for the 12-month return
    if "SPY" not in data:
        raise RuntimeError("No SPY candles returned — cannot run")
    all_days = sorted(data["SPY"])
    test = backtest_days(all_days)
    start_i, end_i = all_days.index(test[0]), all_days.index(test[-1]) + 1
    if start_i < 254:
        raise RuntimeError(f"Only {start_i} trading days of history before the test start — need 254 for the 12-month return")
    spy_c = [data["SPY"][d]["c"] for d in all_days]
    spy_ok = {all_days[i]: spy_c[i] > sum(spy_c[i - 199:i + 1]) / 200 for i in range(199, len(all_days))}
    mid = (end_i - start_i) // 2

    def halves(curve):
        return (curve[mid - 1] / 1000 - 1) * 100, (curve[-1] / curve[mid - 1] - 1) * 100

    spy_curve = [1000 * data["SPY"][all_days[i]]["c"] / data["SPY"][all_days[start_i - 1]]["c"] for i in range(start_i, end_i)]
    spy_ret, spy_dd, (spy_h1, spy_h2) = (spy_curve[-1] / 1000 - 1) * 100, _mdd(spy_curve), halves(spy_curve)
    rows, summary = "", [f"SPY {spy_ret:+.0f}% (DD {spy_dd:.0f}%)"]
    cell = lambda v: f"<td style='color:{pct_color(v)};'>{v:+.1f}%</td>"
    rows += (f"<tr style='background:#f8fafc;'><td><b>SPY</b> buy &amp; hold (the bar to beat)</td>{cell(spy_ret)}<td>—</td><td>—</td>"
             f"{cell(spy_h1)}{cell(spy_h2)}<td style='color:#b91c1c;'>{spy_dd:.1f}%</td><td>—</td><td>1</td></tr>")
    for code, name, top, trend in ROT_BOOKS:
        runs = [rot_simulate(data, all_days, start_i, end_i, top, trend, off, spy_ok) for off in (range(ROT_EVERY) if top else [0])]
        rets = [(c[-1] / 1000 - 1) * 100 for c, _ in runs]
        dds = [_mdd(c) for c, _ in runs]
        h1 = float(np.median([halves(c)[0] for c, _ in runs]))
        h2 = float(np.median([halves(c)[1] for c, _ in runs]))
        med = float(np.median(rets))
        rows += (f"<tr><td><b>{code}</b> {name}</td>{cell(med)}{cell(min(rets))}{cell(max(rets))}{cell(h1)}{cell(h2)}"
                 f"<td style='color:#b91c1c;'>{min(dds):.1f}%</td><td>{sum(1 for r in rets if r > spy_ret)}/{len(rets)}</td>"
                 f"<td>{int(np.median([t for _, t in runs]))}</td></tr>")
        summary.append(f"{code}: {med:+.0f}% (worst {min(rets):+.0f}%, DD {min(dds):.0f}%)")
        print(f"{code}: median {med:+.1f}% worst {min(rets):+.1f}% DD {min(dds):.1f}%", flush=True)
    title = f"TR-GC-Equities ROTATION TEST — {test[0]} → {test[-1]}"
    html = f"""<html><body style="margin:0;background:#f1f5f9;font-family:Arial,sans-serif;">
  <div style="max-width:900px;margin:0 auto;padding:12px;"><div style="background:#fff;border-radius:10px;padding:14px;">
    <div style="font-size:17px;font-weight:700;">{title}</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:3px;">market {MARKET}: {len(data)} stocks with candles • {ENGINE_VERSION}</div>
    <div style="background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px;font-size:11px;color:#92400e;margin:10px 0;">
      📝 PAPER ONLY. Every pick uses only data up to the day before and trades at that day's close, with 0.05% cost per side.
      Momentum books are run once for each of the 21 possible monthly rebalance days (Median / Worst / Best of those 21).
      Stocks that no longer exist are missing from the list, which flatters every book a little, momentum included.</div>
    <div style="overflow-x:auto;"><table style="width:100%;font-size:11px;border-collapse:collapse;" cellpadding="4">
      <tr style="background:#f1f5f9;"><th align="left">Book</th><th align="left">Median</th><th align="left">Worst</th><th align="left">Best</th>
      <th align="left">Older half</th><th align="left">Newer half</th><th align="left">Worst drop</th><th align="left">Beat SPY</th><th align="left">Trades</th></tr>
      {rows}</table></div>
  </div></div></body></html>"""
    text = f"{title}\n" + "\n".join(summary)
    print(text, flush=True)
    send_email(f"🔄 {title} — " + " | ".join(summary), html, text, None)


def main():
    if STOCKS:                                                # Alpaca data only: no Hyperliquid connection needed
        if MODE == "backtest":
            return run_backtest(None)
        if MODE == "ross":
            return run_ross(None)
        if MODE == "robust":
            return run_robust(None)
        if MODE == "rotation":
            return run_rotation()
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
    elif MODE == "ross":
        run_ross(info)
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
