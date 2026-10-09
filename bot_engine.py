import os
import json
import time
from datetime import datetime, timedelta
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
STATE_FILE = "state.json"

VERBOSE_TEST_MODE = True

def get_central_time():
    utc_now = datetime.utcnow()
    year = utc_now.year
    dst_start = datetime(year, 3, 8)
    dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)
    dst_end = datetime(year, 11, 1)
    dst_end += timedelta(days=(6 - dst_end.weekday()) % 7)
    is_dst = dst_start <= utc_now < dst_end
    offset = -5 if is_dst else -6
    return utc_now + timedelta(hours=offset)

def load_state():
    default_state = {
        "cooldown_blocklist": {},      # 24H Post-loss cooldown: {coin: expire_timestamp}
        "last_traded_candle": {},      # Single-candle entry lock: {coin: candle_timestamp}
        "hibernating_until": 0,        # Emergency 12H hibernation lock
        "stagnation_tracker": {},      # Falling knife tracker: {coin: consecutive_negative_runs}
        "closed_trades_ledger": [], 
        "active_position_cache": {},
        "previous_active_coins": [],
        "last_run_timestamp": "",
        "last_email_timestamp": 0,
        "last_scan_timestamp": 0,
        "gemini_cache": {},
        "daily_starting_equity": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            print(f"Error loading state.json: {e}", flush=True)
    return default_state

def save_state(state):
    state["last_run_timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def api_retry(func, *args, retries=5, delay=3.0, **kwargs):
    """Universal resilient API retry decorator for exchange and web queries."""
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                print(f"[WARN] API Call exception ({e}). Retrying in {delay}s ({attempt+1}/{retries})...", flush=True)
                time.sleep(delay)
                delay *= 2.0
            else:
                raise e

def send_html_dashboard_email(subject, html_content, text_fallback):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    receiver_email = os.getenv("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("[WARN] Email credentials missing in environment variables. Skipping email dispatch.", flush=True)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email

    msg.attach(MIMEText(text_fallback, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    try:
