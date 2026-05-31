import streamlit as st
import requests
import time
import threading
import os
import base64
import random
import traceback
from datetime import datetime, timedelta
import yfinance as yf
from groq import Groq
import json
import sqlite3
import pandas as pd
import pytz

st.set_page_config(page_title="ISA Trading Bot", layout="wide", page_icon="📈")

# --- STRATEGY PROFILES ---
STRATEGIES = {
    "AGGRESSIVE": {
        "stop_multiplier": 1.5,  # Tight stop for high volatility
        "harvest_threshold": 5.0,  # Harvest profits quickly
        "reinvest_mode": "MOMENTUM",
    },
    "GROWTH": {
        "stop_multiplier": 2.5,  # Balanced breathing room
        "harvest_threshold": 15.0,
        "reinvest_mode": "BALANCED",
    },
    "DEFENSIVE": {
        "stop_multiplier": 4.0,  # Wide stop to avoid getting shaken out
        "harvest_threshold": 50.0,  # Don't touch, just let it compound
        "reinvest_mode": "DIVIDEND",
    },
}
# --- 1. CONFIGURATION & SECRETS ---
FINNHUB_KEY = os.getenv("FINNHUB_KEY") or st.secrets.get("FINNHUB_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC") or st.secrets.get("NTFY_TOPIC")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or st.secrets.get("GROQ_API_KEY")

if not GROQ_API_KEY:
    st.error("GROQ_API_KEY not found! Please check your secrets.")
    st.stop()

groq_client = Groq(api_key=GROQ_API_KEY)

WATCHLIST_FILE = "watchlist.json"


def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                return json.load(f)
        except:
            return []
    return []


def save_watchlist(watchlist):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f)


def init_db():
    conn = sqlite3.connect("bot_brain.db", check_same_thread=False)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS ai_decisions
                 (timestamp TEXT, symbol TEXT, action TEXT, confidence INTEGER, profit REAL, reason TEXT)"""
    )
    conn.commit()
    conn.close()


def log_ai_decision(symbol, action, confidence, profit, reason):
    try:
        conn = sqlite3.connect("bot_brain.db", timeout=10)
        c = conn.cursor()
        c.execute(
            "INSERT INTO ai_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (get_timestamp(), symbol, action, int(confidence), float(profit), reason),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[{get_timestamp()}] [DB ERROR] Failed to log decision: {e}")


# --- 2. GLOBAL SHARED STATE ---
@st.cache_resource
def get_shared_state():
    class SharedState:
        def __init__(self):
            self.skimmer_active = False
            self.brief_active = False
            self.thread_running = False
            self.processed_headlines = set()
            self.daily_ai_calls = 0
            self.price_monitor_active = False
            self.stop_loss_pct = 5.0
            self.price_thread_running = False
            self.logs = []
            self.custom_watchlist = load_watchlist()
            self.auto_harvest_active = False
            self.harvest_threshold = 10.0
            self.last_harvest_date = None
            self.per_stock_thresholds = {}
            self.pending_rotations = []
            self.daily_cooldowns = set()
            self.trading_enabled = True
            self.master_heartbeat = datetime.now()
            self.heartbeat_alert_sent = False

    return SharedState()


shared_state = get_shared_state()
init_db()


# --- 3. CORE FUNCTIONS (Top Level - Zero Indentation) ---
def verify_price_integrity(symbol, primary_price, state):
    """
    Cross-references Yahoo Finance data with Finnhub's live ticker.
    If the two APIs disagree by more than 5%, it flags a data glitch.
    """
    try:
        # Finnhub requires standard US tickers, strip the .L for UK stocks just for the price check
        check_symbol = symbol.replace(".L", "") if symbol.endswith(".L") else symbol

        url = (
            f"https://finnhub.io/api/v1/quote?symbol={check_symbol}&token={FINNHUB_KEY}"
        )
        res = requests.get(url, timeout=5)

        if res.status_code == 200:
            finnhub_data = res.json()
            secondary_price = finnhub_data.get(
                "c", 0
            )  # 'c' is the current price in Finnhub's JSON

            if secondary_price > 0:
                # Calculate the percentage difference between the two data feeds
                diff_pct = abs(primary_price - secondary_price) / primary_price * 100

                if diff_pct > 5.0:  # 5% discrepancy threshold
                    log_event(
                        state,
                        f"DATA GLITCH DETECTED: {symbol} YF Price (${primary_price:.2f}) differs massively from Finnhub (${secondary_price:.2f}).",
                        is_error=True,
                    )
                    return False
        return True

    except Exception as e:
        # If Finnhub is down, don't paralyze the bot. Log it and trust the primary data.
        log_event(state, f"Price verification fallback for {symbol}: {e}")
        return True


def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_ntfy(title, message):
    print(
        f"[{get_timestamp()}] [NTFY] ACTION: Sending payload to Topic: '{NTFY_TOPIC}'..."
    )
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Tags": "rotating_light"},
        )
        if response.status_code == 200:
            print(f"[{get_timestamp()}] [NTFY] VERDICT: Successful transmission.")
            return True, f"HTTP {response.status_code}: {response.text}"
        else:
            print(f"[{get_timestamp()}] [NTFY] VERDICT: Server rejected payload.")
            return False, f"HTTP {response.status_code}: {response.text}"
    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"[{get_timestamp()}] [NTFY] ERROR: {error_trace}")
        return False, f"Exception: {str(e)}"


@st.cache_data(ttl=3600)
def get_portfolio_from_t212():
    raw_key = os.getenv("T212_API_KEY") or st.secrets.get("T212_API_KEY")
    raw_secret = os.getenv("T212_API_SECRET") or st.secrets.get("T212_API_SECRET")

    if not raw_key or not raw_secret:
        st.error(
            "🚨 Missing Credentials! Ensure both T212_API_KEY and T212_API_SECRET are in your secrets."
        )
        return []

    # Clean the keys to prevent whitespace errors
    api_key = str(raw_key).strip()
    api_secret = str(raw_secret).strip()

    # --- FORCED BASE64 ENCRYPTION ---
    credentials_string = f"{api_key}:{api_secret}"
    encoded_credentials = base64.b64encode(credentials_string.encode("utf-8")).decode(
        "utf-8"
    )
    headers = {"Authorization": f"Basic {encoded_credentials}"}
    # --------------------------------

    # Ensure this matches your account type!
    # Use "https://demo.trading212.com..." if you generated a Practice Mode key.
    url = "https://live.trading212.com/api/v0/equity/portfolio"

    try:
        # Notice we are passing 'headers=headers' now, NOT 'auth='
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            print(f"[{get_timestamp()}] [SYSTEM] T212 Sync Successful!")
            clean_portfolio = []
            for item in response.json():
                raw_ticker = item.get("ticker", "")
                if "_US_EQ" in raw_ticker:
                    clean_ticker = raw_ticker.replace("_US_EQ", "")
                elif "l_EQ" in raw_ticker:
                    clean_ticker = raw_ticker.replace("l_EQ", ".L")
                else:
                    clean_ticker = raw_ticker.replace("_EQ", "")

                clean_portfolio.append(
                    {
                        "symbol": clean_ticker,
                        "shares": item.get("quantity", 0),
                        "profit": item.get("ppl", 0),
                    }
                )
            return clean_portfolio
        else:
            st.error(
                f"🚨 T212 Blocked You! Status {response.status_code}: {response.text}"
            )
            return []

    except Exception as e:
        st.error(f"🚨 Network Error: {e}")
        return []


MY_PORTFOLIO = get_portfolio_from_t212()


def is_market_open():
    """Checks if US markets are currently open (Mon-Fri, 9:30 AM - 4:00 PM EST)."""
    tz = pytz.timezone("US/Eastern")
    now_est = datetime.now(tz)

    # Block Weekends (5 = Sat, 6 = Sun)
    if now_est.weekday() >= 5:
        return False

    # Define 9:30 AM and 4:00 PM boundaries
    market_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)

    return market_open <= now_est <= market_close


def execute_t212_order(symbol, amount, action, state):
    """
    Executes a fractional market order by monetary value on Trading 212.
    action must be "BUY" or "SELL".
    """
    # --- 🛑 SAFETY NET: HARD MAX SPEND ---
    MAX_TRADE_VALUE = 50.00  # Set your absolute maximum transaction limit here

    if float(amount) > MAX_TRADE_VALUE:
        log_event(
            state,
            f"FATAL ALARM: Attempted order of £{amount:.2f} exceeds £{MAX_TRADE_VALUE} limit.",
            is_error=True,
        )
        send_ntfy(
            "🚨 SAFETY BREACH PREVENTED",
            f"Blocked an abnormal order size of £{amount:.2f} for {symbol}.",
        )
        return False, "Order exceeded hardcoded safety ceiling."
    # --------------------------------------
    # --- 🛑 SAFETY NET: MARKET HOURS ONLY ---
    if not is_market_open():
        log_event(
            state,
            f"ORDER BLOCKED: US Markets are closed. Prevented {action} for {symbol}.",
        )
        return False, "Market is currently closed."
    # ----------------------------------------
    raw_key = os.getenv("T212_API_KEY") or st.secrets.get("T212_API_KEY")
    raw_secret = os.getenv("T212_API_SECRET") or st.secrets.get("T212_API_SECRET")

    if not raw_key or not raw_secret:
        log_event(
            state,
            f"EXECUTION FAILED: Missing T212 Credentials for {action} {symbol}",
            is_error=True,
        )
        return False, "Missing API Credentials"

    # Base64 Encryption matching your existing auth method
    api_key = str(raw_key).strip()
    api_secret = str(raw_secret).strip()
    credentials_string = f"{api_key}:{api_secret}"
    encoded_credentials = base64.b64encode(credentials_string.encode("utf-8")).decode(
        "utf-8"
    )

    headers = {
        "Authorization": f"Basic {encoded_credentials}",
        "Content-Type": "application/json",
    }

    # Format the ticker for T212 (Assumes US equities for standard tickers, adjust if trading UK/EU)
    t212_ticker = (
        f"{symbol}_US_EQ" if not symbol.endswith(".L") else symbol.replace(".L", "l_EQ")
    )

    url = "https://live.trading212.com/api/v0/equity/orders/market"

    # --- T212 REQUIREMENT: Value for Buys, Quantity (Shares) for Sells ---
    if action == "SELL":
        try:
            live_price = yf.Ticker(symbol).fast_info["lastPrice"]
            shares_to_sell = float(amount) / float(live_price)
            payload = {
                "ticker": t212_ticker,
                "quantity": -abs(shares_to_sell),  # Negative indicates SELL
                "timeValidity": "DAY",
            }
        except Exception as e:
            return False, f"Failed to calculate sell quantity: {e}"
    else:
        payload = {
            "ticker": t212_ticker,
            "value": float(amount),  # Positive value indicates BUY
            "timeValidity": "DAY",
        }
    # ---------------------------------------------------------------------

    print(
        f"[{get_timestamp()}] [API] Transmitting {action} order for £{amount} of {t212_ticker}..."
    )

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)

        if response.status_code == 200:
            order_data = response.json()
            log_event(
                state,
                f"ORDER SUCCESS: {action} £{amount} of {symbol}. ID: {order_data.get('id', 'N/A')}",
            )
            return True, "Executed Successfully"

        elif response.status_code == 429:
            # --- 🛑 SAFETY NET: RATE LIMIT BACKOFF ---
            log_event(
                state,
                f"RATE LIMIT HIT: Trading 212 (HTTP 429). Cooldown initiated.",
                is_error=True,
            )
            time.sleep(60)  # Force the entire script to pause for 1 minute
            return False, "HTTP 429: Too Many Requests. Initiated 60s cooldown."
        else:
            error_msg = f"HTTP {response.status_code}: {response.text}"
            log_event(state, f"ORDER REJECTED: {symbol} - {error_msg}", is_error=True)
            return False, error_msg

    except Exception as e:
        log_event(state, f"ORDER CRASH: {symbol} - {str(e)}", is_error=True)
        return False, str(e)


def get_reinvestment_advice(portfolio, watchlist, state):
    if state.daily_ai_calls >= 500:
        # --- THE FIX: Added "CASH" as the target fallback ---
        return "CASH", 0, "⚠️ Groq daily limit reached. Cannot generate strategy."

    profitable = [p for p in portfolio if p.get("profit", 0) > 0]
    if not profitable:
        # --- THE FIX: Added "CASH" as the target fallback ---
        return (
            "CASH",
            0,
            "No profitable positions available to skim from right now. Hold steady.",
        )

    portfolio_summary = ", ".join(
        [f"{p['symbol']} (+£{p['profit']:.2f})" for p in profitable]
    )
    watchlist_summary = ", ".join(watchlist) if watchlist else "None"

    # --- UPGRADED QUANTITATIVE PROMPT ---
    prompt = f"""My current profitable stock holdings are: {portfolio_summary}.
My current watchlist for buying is: {watchlist_summary}.
Act as a ruthless, strategic trading assistant.

You MUST respond using EXACTLY this 3-line format:
TARGET: [The single ticker symbol of the best watchlist stock to buy]
CONFIDENCE: [1-100]
ADVICE: [3 punchy sentences telling me EXACTLY which stock profits to skim, the exact total £ amount to rotate, and why the target is the best place to put that specific cash.]"""

    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
        )
        state.daily_ai_calls += 1
        response = chat.choices[0].message.content.strip()

        # Parse the 3 exact outputs
        try:
            target = response.split("TARGET:")[1].split("\n")[0].strip().upper()
            confidence = int(response.split("CONFIDENCE:")[1].split("\n")[0].strip())
            advice = response.split("ADVICE:")[1].strip()
        except:
            target = watchlist[0] if watchlist else "VUSA.L"
            confidence = 50
            advice = response

        return target, confidence, advice

    except Exception as e:
        # --- THE FIX: Added "CASH" as the target fallback ---
        return "CASH", 0, f"Error contacting AI: {str(e)}"


def evaluate_harvest_timing(symbol, profit, state):
    if state.daily_ai_calls >= 500:
        return True, 50, "API limit reached. Defaulting to safe harvest."

    try:
        # 1. Fetch Live Context and Technicals
        ticker = yf.Ticker(symbol)
        live_price = ticker.fast_info["lastPrice"]

        history = ticker.history(period="60d")
        close_prices = history["Close"]

        # Calculate Moving Averages
        sma_50 = close_prices.rolling(window=50).mean().iloc[-1]
        sma_20 = close_prices.rolling(window=20).mean().iloc[-1]

        # Calculate 14-Day RSI
        delta = close_prices.diff()
        gains = delta.where(delta > 0, 0).rolling(window=14).mean()
        losses = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gains / losses
        rsi = (100 - (100 / (1 + rs))).iloc[-1]

        # 2. Fetch the latest news headline for fundamental context
        url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        news_req = requests.get(url, timeout=5)
        if news_req.status_code == 200:
            news = news_req.json()
            headline = (
                news[0]["headline"]
                if (isinstance(news, list) and len(news) > 0)
                else "No major news today."
            )
        else:
            headline = "News feed temporarily unavailable."

        # 3. Force the AI into a quantitative momentum decision
        prompt = f"""I have a profitable position (+£{profit:.2f}) on {symbol}. My threshold is met, but I only want to sell if the momentum is dying or the market is turning.
Current Price: ${live_price:.2f}
20-Day SMA (Short Trend): ${sma_20:.2f}
50-Day SMA (Medium Trend): ${sma_50:.2f}
14-Day RSI: {rsi:.1f}
Latest Headline: '{headline}'

Analyze the technicals:
- If RSI is high (>70), it is overbought and a great time to HARVEST profits before a pullback.
- If price is falling below the 20-Day SMA, short-term momentum is breaking down: HARVEST.
- If RSI is healthy (40-60) and price is climbing above SMAs, the run isn't over: HOLD.

You MUST respond using EXACTLY this 2-line format:
VERDICT: [HARVEST or HOLD] | CONFIDENCE: [1-100]
REASONING: [2-3 short sentences explaining the technical and fundamental reasons to harvest now or let it ride.]"""

        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
        )
        state.daily_ai_calls += 1
        response = chat.choices[0].message.content.strip()

        # 4. Parse the strict output format
        try:
            first_line = response.split("\n")[0]
            verdict_part, conf_part = first_line.split("|")
            verdict = verdict_part.split(":")[1].strip().upper()
            confidence = int(conf_part.split(":")[1].strip())
            reasoning = (
                response.split("REASONING:")[1].strip()
                if "REASONING:" in response
                else response
            )
        except Exception:
            # Failsafe if AI breaks formatting
            verdict = "HARVEST"
            confidence = 50
            reasoning = (
                "Format parsing failed. Defaulting to taking profits to be safe."
            )

        should_sell = verdict == "HARVEST"
        return should_sell, confidence, reasoning

    except Exception as e:
        return (
            True,
            50,
            f"Error calculating technicals ({e}). Defaulting to safe harvest to lock in capital.",
        )


def is_earnings_imminent(symbol):
    """Checks if earnings are scheduled within the next 48 hours."""
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar

        if isinstance(cal, dict) and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if dates and len(dates) > 0:
                # Extract the next upcoming date
                next_earnings = dates[0].date()
                days_away = (next_earnings - datetime.now().date()).days

                # Trigger freeze if report is today, tomorrow, or the day after
                if 0 <= days_away <= 2:
                    return True, days_away
    except Exception:
        # Fail gracefully if data is missing; do not crash the main thread
        pass

    return False, -1


def log_event(state, message, is_error=False):
    timestamp = get_timestamp()
    prefix = "[ERROR]" if is_error else "[SYSTEM]"
    full_message = f"[{timestamp}] {prefix} {message}"
    print(full_message)
    state.logs.append(full_message)


def system_health_check():
    print(f"[{get_timestamp()}] [SYSTEM] Initiating Pre-Flight Diagnostics...")
    errors = []

    try:
        print(f"[{get_timestamp()}] [SYSTEM] Testing Groq API...")
        groq_client.chat.completions.create(
            messages=[{"role": "user", "content": "Reply 'OK'"}],
            model="llama-3.3-70b-versatile",
            max_tokens=5,
        )
    except Exception as e:
        errors.append(f"Groq API failure: {str(e)[:50]}")

    try:
        print(f"[{get_timestamp()}] [SYSTEM] Testing Finnhub API...")
        url = f"https://finnhub.io/api/v1/company-news?symbol=MU&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
        res = requests.get(url)
        if res.status_code != 200:
            errors.append(f"Finnhub HTTP {res.status_code}")
    except Exception as e:
        errors.append(f"Finnhub API failure: {str(e)[:50]}")

    if not errors:
        print(
            f"[{get_timestamp()}] [SYSTEM] All systems nominal. Sending boot notification."
        )
        send_ntfy(
            "✅ Notif-ISA Online", "All APIs operational. Master patrol is ready."
        )
    else:
        error_msg = " | ".join(errors)
        print(f"[{get_timestamp()}] [SYSTEM] CRITICAL BOOT FAILURE: {error_msg}")
        send_ntfy(
            "❌ Notif-ISA Boot Error",
            f"System failed to start properly. Details: {error_msg}",
        )


def analyze_news(headline, symbol, state):
    if state.daily_ai_calls >= 500:
        log_event(
            state, f"BLOCKED: Groq daily limit reached. Skipping AI for {symbol}."
        )
        return False

    if headline in state.processed_headlines:
        return False

    print(f"[{get_timestamp()}] [GATEKEEPER] Inspecting: '{headline}' for {symbol}")
    keywords = [
        "earnings",
        "dividend",
        "upgrade",
        "downgrade",
        "acquisition",
        "merger",
        "ceo",
        "guidance",
        "sec",
        "filed",
        "rally",
        "growth",
        "ai",
        "revenue",
    ]

    is_actionable = any(word in headline.lower() for word in keywords)
    if not is_actionable:
        state.processed_headlines.add(headline)
        return False

    print(f"[{get_timestamp()}] [GATEKEEPER] APPROVED: Sending to Groq API...")

    # --- NEW: Fetch live price grounding data ---
    try:
        live_price = yf.Ticker(symbol).fast_info["lastPrice"]
        price_context = (
            f"The current live market price for {symbol} is ${live_price:.2f}."
        )
    except Exception:
        live_price = None
        price_context = ""
    # --------------------------------------------

    # --- UPGRADED PRICE-AWARE PROMPT ---
    prompt = f"""Analyze this market-moving headline: '{headline}' for {symbol}. 
{price_context}
You MUST respond using EXACTLY this 2-line format:
VERDICT: [BUY, SELL, or HOLD] | CONFIDENCE: [1-100]
REASONING: [2-5 short sentences explaining why, and suggesting a realistic Limit or Stop-Limit order based strictly on the live price provided]"""

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.1,  # Lower temp = more consistent formatting
        )
        ai_response_text = chat_completion.choices[0].message.content.strip()
        state.daily_ai_calls += 1

        # --- PARSE THE CONFIDENCE SCORE ---
        try:
            # Attempt to rip the verdict and score out of the AI's text
            first_line = ai_response_text.split("\n")[0]
            verdict_part, confidence_part = first_line.split("|")
            verdict = verdict_part.split(":")[1].strip().upper()
            confidence = int(confidence_part.split(":")[1].strip())
            reasoning = (
                ai_response_text.split("REASONING:")[1].strip()
                if "REASONING:" in ai_response_text
                else ai_response_text
            )
        except Exception as parse_error:
            # Fault tolerance: If the AI hallucinates the format, default to safety
            verdict = "HOLD"
            confidence = 50
            reasoning = ai_response_text
            log_event(
                state,
                f"Format Parse Error: {parse_error}. Raw: {ai_response_text}",
                is_error=True,
            )

        log_event(
            state, f"QUANT SIGNAL -> {symbol}: {verdict} (Score: {confidence}/100)"
        )

        # --- Live Sentiment Adjustment ---
        base_profile = "GROWTH"
        if isinstance(state.custom_watchlist, dict):
            base_profile = state.custom_watchlist.get(symbol, "GROWTH")

        live_params = get_sentiment_adjusted_params(
            symbol, ai_response_text, base_profile
        )

        # Map color emojis to confidence tiers
        if confidence >= 80:
            score_emoji = "🔥"
        elif confidence >= 60:
            score_emoji = "📊"
        else:
            score_emoji = "⚠️"

        alert_title = f"{score_emoji} {symbol} {verdict} Signal ({confidence}/100)"
        if live_params["status"] != "NORMAL":
            log_event(
                state, f"Strategy shift triggered for {symbol}: {live_params['status']}"
            )

        send_ntfy(alert_title, reasoning)
        state.processed_headlines.add(headline)
        return True

    except Exception as e:
        log_event(state, f"AI CRASH: \n{traceback.format_exc()}", is_error=True)
        send_ntfy(f"⚠️ Skimmer AI Failed: {symbol}", f"Error: {str(e)}")
        return False


def evaluate_reinvestment_confidence(symbol, state):
    """Evaluates target asset using live technical indicators to generate a true quantitative confidence score."""
    if state.daily_ai_calls >= 500:
        return "BALANCED", 50, "Groq limit reached. Defaulting to standard allocation."

    try:
        ticker = yf.Ticker(symbol)
        live_price = ticker.fast_info["lastPrice"]

        # --- NEW: Technical Analysis Engine ---
        history = ticker.history(period="60d")
        close_prices = history["Close"]

        # Calculate 50-Day Moving Average
        sma_50 = close_prices.rolling(window=50).mean().iloc[-1]

        # Calculate 14-Day RSI
        delta = close_prices.diff()
        gains = delta.where(delta > 0, 0).rolling(window=14).mean()
        losses = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gains / losses
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        # --------------------------------------

        base_profile = (
            state.custom_watchlist.get(symbol, "GROWTH")
            if isinstance(state.custom_watchlist, dict)
            else "GROWTH"
        )
        default_mode = STRATEGIES.get(base_profile, STRATEGIES["GROWTH"])[
            "reinvest_mode"
        ]

        prompt = f"""Evaluate deploying fresh harvested capital into {symbol}.
Current Price: ${live_price:.2f}
50-Day Moving Average: ${sma_50:.2f}
14-Day RSI: {rsi:.1f}
Default Profile: {base_profile} (Standard Allocation Mode: {default_mode})

Analyze the technicals: An RSI below 30 is oversold (prime buying), above 70 is overbought (terrible buying).
You MUST respond using EXACTLY this 2-line format:
REINVEST_MODE: [MOMENTUM, BALANCED, or DIVIDEND] | CONFIDENCE: [1-100]
REASONING: [2-3 brief sentences explaining if this is a high-conviction entry point based strictly on the RSI and SMA context]"""

        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
        )
        ai_text = chat_completion.choices[0].message.content.strip()
        state.daily_ai_calls += 1

        first_line = ai_text.split("\n")[0]
        mode_part, conf_part = first_line.split("|")
        chosen_mode = mode_part.split(":")[1].strip().upper()
        confidence = int(conf_part.split(":")[1].strip())
        reasoning = (
            ai_text.split("REASONING:")[1].strip()
            if "REASONING:" in ai_text
            else ai_text
        )

        return chosen_mode, confidence, reasoning

    except Exception as e:
        return "BALANCED", 50, f"Reinvestment calculation fallback triggered: {str(e)}"


def get_market_regime():
    """Pulls live VIX data from Yahoo Finance to determine institutional fear levels."""
    try:
        # Fast info is lighter and faster than pulling historical history
        vix_price = yf.Ticker("^VIX").fast_info["lastPrice"]

        if vix_price >= 30.0:
            return "BUNKER_MODE", vix_price
        elif vix_price >= 20.0:
            return "ELEVATED_RISK", vix_price
        else:
            return "NORMAL", vix_price
    except Exception as e:
        # Failsafe: If Yahoo Finance is offline, default to neutral
        return "NORMAL", 0.0


def get_dynamic_params(regime, base_strategy):
    """Adjusts your risk parameters on the fly based on the VIX regime."""
    params = STRATEGIES.get(base_strategy, STRATEGIES["GROWTH"]).copy()

    if regime == "BUNKER_MODE":
        # Total defense: microscopic stops, hyper-aggressive harvesting
        params["stop_multiplier"] = 0.5
        params["harvest_threshold"] = 2.0
        params["status"] = "🚨 BUNKER MODE"
    elif regime == "ELEVATED_RISK":
        # Caution: tighter stops, quicker harvesting
        params["stop_multiplier"] = 1.5
        params["harvest_threshold"] = params["harvest_threshold"] * 0.5
        params["status"] = "⚠️ CAUTION"
    else:
        # Risk-on: Standard user-defined parameters
        params["status"] = "🟢 NORMAL"

    return params


def get_sentiment_adjusted_params(symbol, ai_text, base_strategy):
    params = STRATEGIES.get(base_strategy, STRATEGIES["GROWTH"]).copy()
    text_upper = ai_text.upper()

    if "SELL" in text_upper or "BEARISH" in text_upper:
        params["stop_multiplier"] = 1.2
        params["harvest_threshold"] = 2.0
        params["status"] = "🚨 PROTECTIVE MODE"
    elif "BUY" in text_upper or "BULLISH" in text_upper:
        params["stop_multiplier"] = 3.5
        params["harvest_threshold"] = params["harvest_threshold"] * 1.5
        params["status"] = "🚀 MOMENTUM MODE"
    else:
        params["status"] = "NORMAL"

    return params


# --- 4. BACKGROUND ENGINES ---
def price_patrol(state):
    print(f"[{get_timestamp()}] [SYSTEM] Real-Time Price Radar booted.")
    high_water_marks = {}
    while True:
        # --- 🛑 SAFETY NET: HEARTBEAT WATCHDOG ---
        time_since_beat = (datetime.now() - state.master_heartbeat).total_seconds()
        if time_since_beat > 600 and not state.heartbeat_alert_sent:  # 10 minutes dead
            send_ntfy(
                "🚨 FATAL SYSTEM CRASH",
                "Master Engine has flatlined for 10 minutes. Check the server immediately!",
            )
            state.heartbeat_alert_sent = True
        if not state.price_monitor_active:
            time.sleep(10)
            continue

        combined_portfolio = MY_PORTFOLIO.copy()
        for t in state.custom_watchlist:
            if not any(item["symbol"] == t for item in combined_portfolio):
                combined_portfolio.append({"symbol": t})

        for item in combined_portfolio:
            symbol = item["symbol"]
            try:
                current_price = yf.Ticker(symbol).fast_info["lastPrice"]
                if symbol not in high_water_marks:
                    high_water_marks[symbol] = current_price
                elif current_price > high_water_marks[symbol]:
                    high_water_marks[symbol] = current_price

                drop_pct = (
                    (high_water_marks[symbol] - current_price)
                    / high_water_marks[symbol]
                ) * 100
                if drop_pct >= state.stop_loss_pct:
                    alert_msg = f"{symbol} dropped {drop_pct:.1f}% from its recent high! Current Price: ${current_price:.2f}"
                    log_event(state, f"PRICE ALARM: {alert_msg}", is_error=True)
                    send_ntfy(f"🚨 📉 {symbol} CRASH ALERT", alert_msg)
                    high_water_marks[symbol] = current_price
            except:
                pass
        time.sleep(60)


def master_patrol(state):
    print(f"[{get_timestamp()}] [SYSTEM] Background engine booted.")
    system_health_check()
    last_brief_date = None
    last_memory_flush_date = datetime.now().date()

    try:
        while True:
            state.master_heartbeat = datetime.now()
            state.heartbeat_alert_sent = False  # Reset if recovering from a crash
            if not state.trading_enabled:
                time.sleep(10)
                continue  # Freezes the entire auto-harvester engine
            now = datetime.now()
            if now.date() != last_memory_flush_date:
                print(
                    f"[{get_timestamp()}] [SYSTEM] Midnight protocol: Flushing memory."
                )
                state.processed_headlines.clear()
                state.daily_ai_calls = 0
                state.daily_cooldowns.clear()  # <-- NEW: Wipes the cooldowns at midnight
                last_memory_flush_date = now.date()

            if not state.skimmer_active and not state.brief_active:
                time.sleep(10)
                continue

            # 1. Build the full tracking list first
            combined_portfolio = MY_PORTFOLIO.copy()
            for t in state.custom_watchlist:
                if not any(item["symbol"] == t for item in combined_portfolio):
                    combined_portfolio.append({"symbol": t})

            # 2. Get the market status ONCE (Unindented out of the loop above)
            market_regime, current_vix = get_market_regime()

            # If the market is panicking, log it and alert the phone exactly once per cycle
            if market_regime == "BUNKER_MODE":
                log_event(
                    state,
                    f"MACRO ALERT: VIX spiked to {current_vix:.2f}. BUNKER MODE ENGAGED.",
                    is_error=True,
                )

            # 3. Loop through your positions with clean alignment
            for item in combined_portfolio:
                sym = item["symbol"]
                base_prof = "GROWTH"

                if isinstance(state.custom_watchlist, dict):
                    base_prof = state.custom_watchlist.get(sym, "GROWTH")

                current_runtime_params = get_dynamic_params(market_regime, base_prof)

                # --- Rest of your automated harvesting/processing logic goes here ---
            if state.brief_active:
                if now.hour == 5 and now.date() != last_brief_date:
                    target_stocks = random.sample(
                        combined_portfolio, min(3, len(combined_portfolio))
                    )
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    for item in target_stocks:
                        symbol = item["symbol"]
                        try:
                            url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday_str}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                            news = requests.get(url).json()
                            if (
                                isinstance(news, list)
                                and len(news) > 0
                                and state.daily_ai_calls < 500
                            ):
                                prompt = f"Give a 1-sentence morning summary for {symbol} based on this headline: '{news[0]['headline']}'"
                                chat_completion = groq_client.chat.completions.create(
                                    messages=[{"role": "user", "content": prompt}],
                                    model="llama-3.3-70b-versatile",
                                    temperature=0.2,
                                )
                                state.daily_ai_calls += 1
                                send_ntfy(
                                    f"🌅 {symbol} Morning Brief",
                                    chat_completion.choices[0].message.content.strip(),
                                )
                        except Exception as e:
                            send_ntfy(f"⚠️ Brief Failed: {symbol}", f"Error: {str(e)}")
                    last_brief_date = now.date()

            # --- 🌾 AUTO-HARVESTER ENGINE WITH PER-STOCK OVERRIDES ---
            if state.auto_harvest_active and now.date() != state.last_harvest_date:
                # Ensure the dictionary is initialized safely background-side
                if not hasattr(state, "per_stock_thresholds"):
                    state.per_stock_thresholds = {}

                ripe_profits = []
                for p in combined_portfolio:
                    symbol = p.get("symbol")
                    profit = p.get("profit", 0)

                    if symbol in state.daily_cooldowns:
                        continue  # Skip this stock entirely until tomorrow

                    # Dynamic threshold matching: Check specific override map, then fall back to global
                    specific_threshold = state.per_stock_thresholds.get(
                        symbol, state.harvest_threshold
                    )

                    if profit >= specific_threshold:
                        # --- 🛑 DATA INTEGRITY GUARD: PRICE SANITY CHECK ---
                        try:
                            # Pull the live price that triggered the threshold
                            primary_price = yf.Ticker(symbol).fast_info["lastPrice"]

                            # Force the consensus check
                            if verify_price_integrity(symbol, primary_price, state):
                                ripe_profits.append(p)
                            else:
                                send_ntfy(
                                    "⚠️ False Alarm Prevented",
                                    f"Data glitch detected for {symbol}. API feeds disagree on price. Harvest aborted to protect capital.",
                                )
                                state.daily_cooldowns.add(
                                    symbol
                                )  # Lock it for the day to prevent spam
                        except Exception as e:
                            # Failsafe: If the price check crashes, assume it's ripe but log the error
                            log_event(
                                state, f"Integrity check bypassed for {symbol}: {e}"
                            )
                            ripe_profits.append(p)
                        # ---------------------------------------------------
                if ripe_profits:
                    log_event(
                        state, "Harvester Triggered! AI is evaluating exit momentum..."
                    )

                    for p in ripe_profits:
                        symbol = p["symbol"]
                        profit = p["profit"]

                        # --- EARNINGS FREEZE PROTOCOL ---
                        earnings_imminent, days_away = is_earnings_imminent(symbol)

                        if earnings_imminent:
                            # Override the AI: Force a hold and maximize confidence
                            should_sell = False
                            confidence = 100
                            reason = f"EARNINGS FREEZE: Corporate report drops in {days_away} days. Holding capital for volatility play."
                            log_event(
                                state, f"🛡️ Earnings Freeze activated for {symbol}."
                            )
                        else:
                            # If no earnings are pending, ask the AI to evaluate standard momentum
                            should_sell, confidence, reason = evaluate_harvest_timing(
                                symbol, profit, state
                            )
                        # --------------------------------

                        # --- SQLITE LOGGING HOOK ---
                        action_str = "HARVEST" if should_sell else "HOLD"
                        log_ai_decision(symbol, action_str, confidence, profit, reason)
                        # ---------------------------

                        log_event(
                            state,
                            f"AI Decision Logged -> {symbol}: {action_str} ({confidence}%)",
                        )

                        if should_sell:
                            # Unpack the 3 variables including the target
                            target_asset, advice_conf, advice_text = (
                                get_reinvestment_advice(
                                    [p], state.custom_watchlist, state
                                )
                            )

                            # Package the target into the queue
                            rotation_task = {
                                "source": symbol,
                                "target": target_asset,  # <-- CRITICAL FIX
                                "amount": profit,
                                "harvest_conf": confidence,
                                "harvest_reason": reason,
                                "advice_conf": advice_conf,
                                "advice_text": advice_text,
                                "timestamp": get_timestamp(),
                            }

                            # Ensure the queue exists before appending
                            if not hasattr(state, "pending_rotations"):
                                state.pending_rotations = []

                            state.pending_rotations.append(rotation_task)

                            log_event(
                                state,
                                f"⏳ Rotation queued for approval: {symbol} (+£{profit:.2f})",
                            )
                            send_ntfy(
                                f"⏳ Action Required: {symbol} Ready",
                                f"AI wants to harvest £{profit:.2f} and rotate it (Score: {confidence}/100).\nOpen Dashboard to Review & Approve.",
                            )
                        else:
                            send_ntfy(
                                f"💎 {symbol} Diamond Hands (Score: {confidence}/100)",
                                f"Decision: HOLD.\nReason: {reason}",
                            )
                    # Lock the engine for the day to prevent spam
                    state.last_harvest_date = now.date()

            if state.skimmer_active:
                if now.hour != 5 or not state.brief_active:
                    for item in combined_portfolio:
                        symbol = item["symbol"]
                        try:
                            if symbol.endswith(".L"):
                                news = yf.Ticker(symbol).news
                                if news and isinstance(news, list):
                                    analyze_news(news[0]["title"], symbol, state)
                            else:
                                url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={now.strftime('%Y-%m-%d')}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                                news_items = requests.get(url).json()
                                if isinstance(news_items, list) and len(news_items) > 0:
                                    analyze_news(
                                        news_items[0]["headline"], symbol, state
                                    )
                        except Exception as e:
                            send_ntfy(
                                f"⚠️ Skimmer Failed: {symbol}", f"Error: {str(e)}"
                            )

            time.sleep(600)

    except Exception as fatal_error:
        send_ntfy("🚨 BOT OFFLINE: Fatal Crash", str(fatal_error))
        state.thread_running = False


# --- 5. STREAMLIT USER INTERFACE ---
if not shared_state.thread_running:
    t = threading.Thread(target=master_patrol, args=(shared_state,), daemon=True)
    t.start()
    shared_state.thread_running = True

if not shared_state.price_thread_running:
    pt = threading.Thread(target=price_patrol, args=(shared_state,), daemon=True)
    pt.start()
    shared_state.price_thread_running = True

st.title("📈 Notif-ISA Trading Bot")
st.markdown("---")
st.subheader("⚙️ Bot Control Panel")

# --- GLOBAL KILL SWITCH ---
if shared_state.trading_enabled:
    if st.button(
        "🛑 ENGAGE GLOBAL KILL SWITCH", type="primary", use_container_width=True
    ):
        shared_state.trading_enabled = False
        log_event(
            shared_state, "🚨 KILL SWITCH ENGAGED. All trading halted.", is_error=True
        )
        send_ntfy("🛑 TRADING HALTED", "The Global Kill Switch has been activated.")
        st.rerun()
else:
    st.error("🚨 SYSTEM IS CURRENTLY HALTED.")
    if st.button("🟢 Disengage Kill Switch & Resume Trading", use_container_width=True):
        shared_state.trading_enabled = True
        log_event(shared_state, "✅ Kill Switch disengaged. Trading resumed.")
        send_ntfy("🟢 SYSTEM ONLINE", "The Global Kill Switch was disengaged.")
        st.rerun()
st.markdown("---")
# --- MACRO VIX DISPLAY ---
try:
    live_vix = yf.Ticker("^VIX").fast_info["lastPrice"]
    if live_vix >= 30:
        vix_color = "🔴"
        vix_status = "BUNKER MODE ENGAGED"
    elif live_vix >= 20:
        vix_color = "🟠"
        vix_status = "ELEVATED RISK"
    else:
        vix_color = "🟢"
        vix_status = "NORMAL"
    st.caption(f"{vix_color} **Global Macro VIX:** {live_vix:.2f} — {vix_status}")
except:
    pass
# -------------------------
calls_made = shared_state.daily_ai_calls
calls_remaining = 500 - calls_made
st.progress(
    min(calls_made / 500.0, 1.0),
    text=f"🧠 Groq Llama 3 Quota: {calls_made} / 500 (Remaining: {calls_remaining})",
)
st.write("")

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown("#### 🌅 Morning Brief")
    if shared_state.brief_active:
        st.success("Active")
        if st.button("🔴 Stop Brief", use_container_width=True):
            shared_state.brief_active = False
            log_event(shared_state, "User Override: Morning Brief paused.")
            send_ntfy(
                "⏸️ Morning Brief Paused",
                "The Morning Brief has been manually stopped via the dashboard.",
            )
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Brief", use_container_width=True):
            shared_state.brief_active = True
            log_event(shared_state, "User Override: Morning Brief activated.")
            send_ntfy(
                "🌅 Morning Brief Active", "The Morning Brief has been turned on."
            )
            st.rerun()

with col2:
    st.markdown("#### 🔍 AI Skimmer")
    if shared_state.skimmer_active:
        st.success("Active")
        if st.button("🔴 Stop Skimmer", use_container_width=True):
            shared_state.skimmer_active = False
            log_event(shared_state, "User Override: AI Skimmer paused.")
            send_ntfy(
                "⏸️ Skimmer Paused",
                "The 10-Minute Skimmer has been manually stopped via the dashboard.",
            )
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Skimmer", use_container_width=True):
            shared_state.skimmer_active = True
            log_event(shared_state, "User Override: AI Skimmer activated.")
            send_ntfy("🔍 Skimmer Active", "The 10-Minute Skimmer has been turned on.")
            st.rerun()

with col3:
    st.markdown("#### 📉 Live Price Radar")
    new_limit = st.slider(
        "Max Drop %",
        min_value=1.0,
        max_value=15.0,
        value=shared_state.stop_loss_pct,
        step=0.5,
    )
    if new_limit != shared_state.stop_loss_pct:
        shared_state.stop_loss_pct = new_limit

    if shared_state.price_monitor_active:
        st.success(f"Active (Alert at -{shared_state.stop_loss_pct}%)")
        if st.button("🔴 Stop Radar", use_container_width=True):
            shared_state.price_monitor_active = False
            log_event(shared_state, "User Override: Live Price Radar paused.")
            send_ntfy(
                "⏸️ Price Radar Paused", "Live Price Radar has been manually stopped."
            )
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Radar", use_container_width=True):
            shared_state.price_monitor_active = True
            log_event(
                shared_state,
                f"User Override: Live Price Radar activated at {shared_state.stop_loss_pct}% limit.",
            )
            send_ntfy(
                "📉 Price Radar Active",
                f"Live Price Radar is monitoring at a -{shared_state.stop_loss_pct}% trigger.",
            )
            st.rerun()

st.markdown("---")
st.markdown("#### 📝 Discovery Watchlist")
st.caption("Manage your custom tracking list. These run alongside your T212 portfolio.")

with col4:
    st.markdown("#### 🌾 Auto-Harvester")
    new_thresh = st.number_input(
        "Take-Profit (£)",
        min_value=1.0,
        value=float(shared_state.harvest_threshold),
        step=1.0,
    )
    if new_thresh != shared_state.harvest_threshold:
        shared_state.harvest_threshold = new_thresh

    if shared_state.auto_harvest_active:
        st.success(f"Active (Trigger: £{shared_state.harvest_threshold})")
        if st.button("🔴 Stop Harvester", key="stop_harv", width="stretch"):
            shared_state.auto_harvest_active = False
            log_event(shared_state, "User Override: Auto-Harvester paused.")

            # 🔔 Trigger Disarmed Notification
            send_ntfy("🌾 Auto-Harvester Update", "⏸️ Auto-Harvester has been PAUSED.")
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Harvester", key="start_harv", width="stretch"):
            shared_state.auto_harvest_active = True
            log_event(
                shared_state,
                f"User Override: Harvester activated at £{shared_state.harvest_threshold}.",
            )

            # 🔔 Trigger Armed Notification
            send_ntfy(
                "🌾 Auto-Harvester Update",
                f"🚀 Auto-Harvester is now ACTIVE.\nTarget: £{shared_state.harvest_threshold}",
            )
            st.rerun()

# --- 1. THE ADD BAR ---
col1, col2 = st.columns([3, 1])
with col1:
    new_symbol = (
        st.text_input("Add new symbol:", placeholder="e.g. AAPL").strip().upper()
    )
with col2:
    st.write("")
    st.write("")
    if st.button("➕ Add", key="add_btn", width="stretch"):
        if new_symbol:
            # --- VALIDATION ENGINE ---
            with st.spinner(f"Verifying {new_symbol}..."):
                ticker = yf.Ticker(new_symbol)
                # We check 'longName' because invalid tickers return an empty dict or None
                if ticker.info.get("longName"):
                    if new_symbol not in shared_state.custom_watchlist:
                        shared_state.custom_watchlist.append(new_symbol)
                        save_watchlist(shared_state.custom_watchlist)
                        st.success(f"Added {new_symbol}!")
                        send_ntfy(
                            "📝 Watchlist Updated", f"Added {new_symbol} to tracking."
                        )
                        st.rerun()
                    else:
                        st.warning("Stock already in list.")
                else:
                    # This triggers if the ticker is fake/invalid
                    st.error(
                        f"❌ '{new_symbol}' is not a valid stock ticker. Please try again."
                    )

# --- 2. THE REMOVE BUTTONS ---
st.write("**Currently Tracking:**")
if shared_state.custom_watchlist:
    for ticker in shared_state.custom_watchlist:
        c1, c2 = st.columns([4, 1])
        with c1:
            st.info(f"🎯 **{ticker}**")
        with c2:
            if st.button("❌ Remove", key=f"del_{ticker}", width="stretch"):
                shared_state.custom_watchlist.remove(ticker)
                save_watchlist(shared_state.custom_watchlist)
                st.rerun()
else:
    st.info("Your watchlist is currently empty.")
    # (Removed the rogue st.rerun() that was causing the infinite loop!)

st.markdown("### 🎯 Per-Stock Harvest Thresholds")
with st.expander("Configure Individual Position Targets", expanded=False):
    st.caption("Leave at default to use your global strategy profile threshold.")

    # Safely ensure the dictionary exists in state
    if not hasattr(shared_state, "per_stock_thresholds"):
        shared_state.per_stock_thresholds = {}

    # Generate an independent slider for every active holding
    for item in MY_PORTFOLIO:
        symbol = item["symbol"]
        current_profit = item["profit"]

        # Determine baseline default starting position
        default_val = float(shared_state.harvest_threshold)
        saved_val = shared_state.per_stock_thresholds.get(symbol, default_val)

        # Ensure slider boundaries contain the current state gracefully
        max_slider_bound = max(50.0, float(current_profit) * 2.0)

        val = st.slider(
            label=f"{symbol} Target (Current Profit: £{current_profit:,.2f})",
            min_value=0.0,
            max_value=float(max_slider_bound),
            value=float(saved_val),
            step=0.5,
            key=f"thresh_{symbol}",
        )
        # Record the setting
        shared_state.per_stock_thresholds[symbol] = val

st.markdown("---")
st.markdown("#### 📊 Live Portfolio Holdings")
st.dataframe(MY_PORTFOLIO, width="stretch")

st.markdown("---")
st.markdown("#### 🧠 AI Profit Skimmer & Reinvestment Strategy")
st.caption(
    "Analyzes your live T212 profits and your Discovery Watchlist to suggest capital rotation."
)

if st.button("Calculate Reinvestment Strategy", key="reinvest_btn", width="stretch"):
    with st.spinner("AI is analyzing your live profits and watchlist targets..."):

        # --- THE FIX: Unpack all 3 variables ---
        target_asset, confidence, advice = get_reinvestment_advice(
            MY_PORTFOLIO, shared_state.custom_watchlist, shared_state
        )

        # Display color-coded conviction tiers
        if confidence >= 80:
            st.success(f"🔥 High Conviction Strategy (Score: {confidence}/100)")
        elif confidence >= 60:
            st.info(f"📊 Standard Strategy (Score: {confidence}/100)")
        elif confidence > 0:
            st.warning(f"⚠️ Low Conviction Strategy (Score: {confidence}/100)")
        else:
            st.error("Strategy Generation Failed.")

        # Show the target asset and the advice text
        st.write(f"**Target Asset:** {target_asset}")
        st.write(advice)

        send_ntfy(
            f"🧠 Reinvestment Strategy ({confidence}/100)",
            f"Target: {target_asset}\n{advice}",
        )

st.markdown("---")
st.markdown("#### 🛠️ Diagnostics")
if st.button("🔔 Send Manual Test Notification", width="stretch"):
    success, details = send_ntfy(
        "✅ Dashboard Connected", "Diagnostic tracing enabled."
    )
    if success:
        st.success(f"Signal fired successfully! Server responded: {details}")
    else:
        st.error(f"Signal failed! Error details: {details}")

st.markdown("---")
st.markdown("#### 🧠 AI Decision Ledger (Quantitative Tracker)")
st.caption(
    "A permanent record of every HARVEST/HOLD decision made by the AI. Use this to track accuracy over time."
)

try:
    conn = sqlite3.connect("bot_brain.db")
    df = pd.read_sql_query(
        "SELECT * FROM ai_decisions ORDER BY timestamp DESC LIMIT 50", conn
    )
    conn.close()

    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("The ledger is currently empty. Waiting for the first AI decision...")
except Exception as e:
    st.error(f"Could not load ledger: {e}")

st.markdown("---")
st.markdown("### ⏳ Pending AI Executions (Awaiting Approval)")
st.caption("Review and authorize capital rotations drafted by the Auto-Harvester.")

if not hasattr(shared_state, "pending_rotations"):
    shared_state.pending_rotations = []

if not shared_state.pending_rotations:
    st.info("No pending actions. The AI is watching the market.")
else:
    for i, task in enumerate(shared_state.pending_rotations):
        with st.expander(
            f"Rotate £{task['amount']:.2f} from {task['source']}", expanded=True
        ):
            st.write(
                f"**1. Harvest Rationale** (Confidence: {task['harvest_conf']}/100):"
            )
            st.caption(f"{task['harvest_reason']}")

            st.write(
                f"**2. Reinvestment Strategy** (Confidence: {task['advice_conf']}/100):"
            )
            st.caption(f"{task['advice_text']}")

            st.caption(f"Drafted: {task['timestamp']}")

            c1, c2 = st.columns(2)

            # --- THE CLEAN APPROVAL BLOCK ---
            if c1.button(
                "✅ Approve & Execute", key=f"approve_{i}", use_container_width=True
            ):
                # 1. HARVEST: Execute the SELL order first
                with st.spinner(
                    f"Harvesting £{task['amount']:.2f} of {task['source']}..."
                ):
                    sell_success, sell_msg = execute_t212_order(
                        task["source"], task["amount"], "SELL", shared_state
                    )

                # 2. VERIFY & CLEAR: Only proceed if the broker confirmed the sell
                if sell_success:
                    # --- SETTLEMENT PAUSE ---
                    with st.spinner("Broker clearing funds..."):
                        time.sleep(3)

                    # 3. REINVEST: Execute the BUY order with the cleared cash
                    with st.spinner(f"Reinvesting into {task['target']}..."):
                        buy_success, buy_msg = execute_t212_order(
                            task["target"], task["amount"], "BUY", shared_state
                        )
                    # 3. REINVEST: Check allocation limits before buying
                    MAX_POSITION_SIZE = 500.00  # Max total £ allowed in a single asset

                    with st.spinner(
                        f"Verifying allocation limits for {task['target']}..."
                    ):
                        # Check how much of the target asset we already own
                        current_holding = next(
                            (p for p in MY_PORTFOLIO if p["symbol"] == task["target"]),
                            None,
                        )
                        current_value = 0
                        if current_holding:
                            try:
                                live_price = yf.Ticker(task["target"]).fast_info[
                                    "lastPrice"
                                ]
                                current_value = current_holding["shares"] * live_price
                            except:
                                pass  # If price check fails, allow the small buy to proceed

                    if (current_value + task["amount"]) > MAX_POSITION_SIZE:
                        st.error(
                            f"Allocation limit reached! You already have ~£{current_value:.2f} in {task['target']}."
                        )
                        send_ntfy(
                            "🛡️ Allocation Blocked",
                            f"Successfully sold {task['source']}, but cash was parked. Buying {task['target']} would exceed your £{MAX_POSITION_SIZE} limit.",
                        )
                    else:
                        with st.spinner(f"Reinvesting into {task['target']}..."):
                            buy_success, buy_msg = execute_t212_order(
                                task["target"], task["amount"], "BUY", shared_state
                            )

                        if buy_success:
                            log_event(
                                shared_state,
                                f"USER APPROVED: Harvested {task['source']} and rotated to {task['target']}.",
                            )
                            send_ntfy(
                                "✅ Reallocation Complete",
                                f"Successfully secured profits and bought {task['target']}.",
                            )
                            st.success("Capital rotation executed perfectly.")
                        else:
                            st.error(f"Failed to buy target asset: {buy_msg}")
                            send_ntfy(
                                "⚠️ Rotation Incomplete",
                                f"Sold {task['source']}, but failed to buy {task['target']}. Cash is parked.",
                            )

                # --- ENGAGE THE DAILY LOCK ---
                shared_state.daily_cooldowns.add(task["source"])
                shared_state.pending_rotations.pop(i)
                time.sleep(2)
                st.rerun()

            # --- THE REJECTION BLOCK ---
            if c2.button(
                "❌ Reject & Hold Position", key=f"reject_{i}", use_container_width=True
            ):
                log_event(
                    shared_state,
                    f"USER REJECTED: Rotation for {task['source']} cancelled.",
                )
                send_ntfy(
                    "❌ Execution Cancelled",
                    f"Profits from {task['source']} retained in current position.",
                )

                # --- ENGAGE THE DAILY LOCK ---
                shared_state.daily_cooldowns.add(task["source"])
                shared_state.pending_rotations.pop(i)
                st.rerun()

st.markdown("---")
st.subheader("🖥️ System Logs & Live Events")

if not hasattr(shared_state, "logs"):
    shared_state.logs = []

if shared_state.logs:
    recent_logs = shared_state.logs[-30:]
    log_output = "\n".join(recent_logs)
    st.code(log_output, language="bash")
else:
    st.info("System standing by. Waiting for engine output...")

if st.button("🔄 Refresh Logs", key="force_refresh_logs_btn", width="stretch"):
    st.rerun()

st.markdown("---")
st.markdown("#### 🧪 Strategy Simulation")
if st.button("🔴 Simulate Emergency Bearish Event (NVDA Test)", width="stretch"):
    with st.spinner("Injecting panic event into skimmer pipeline..."):
        # Simulated high-impact bearish headline
        test_headline = "Nvidia faces severe hardware supply chain bottlenecks and sudden regulatory fines."

        # Manually route it through your upgraded news engine
        analyze_news(test_headline, "NVDA", shared_state)
        st.success("Simulation sent! Check your system logs below and your phone.")

st.markdown("---")
st.markdown("#### 🕵️ Master API Diagnostics")
st.caption(
    "Pings all external gateways to verify active status and credential validity."
)

if st.button("Run Full Network Diagnostics", width="stretch"):
    with st.spinner("Pinging API Gateways..."):

        # --- 1. TRADING 212 TEST ---
        st.write("**1. Trading 212 Status**")
        raw_key = os.getenv("T212_API_KEY") or st.secrets.get("T212_API_KEY")
        raw_secret = os.getenv("T212_API_SECRET") or st.secrets.get("T212_API_SECRET")

        if raw_key and raw_secret:
            api_key = str(raw_key).strip()
            api_secret = str(raw_secret).strip()

            # --- BASE64 ENCRYPTION FOR DIAGNOSTICS ---
            credentials_string = f"{api_key}:{api_secret}"
            encoded_credentials = base64.b64encode(
                credentials_string.encode("utf-8")
            ).decode("utf-8")
            headers = {"Authorization": f"Basic {encoded_credentials}"}

            # Test Live
            try:
                res_live = requests.get(
                    "https://live.trading212.com/api/v0/equity/portfolio",
                    headers=headers,
                    timeout=5,
                )
                if res_live.status_code == 200:
                    st.success("✅ Live Environment: Connected & Authenticated.")
                elif res_live.status_code == 401:
                    st.error(
                        "❌ Live Environment: 401 Unauthorized (Invalid Key, Secret, or Permissions)."
                    )
                else:
                    st.warning(f"⚠️ Live Environment: HTTP {res_live.status_code}")
            except Exception as e:
                st.error(f"Live Environment Network Error: {e}")

            # Test Demo
            try:
                res_demo = requests.get(
                    "https://demo.trading212.com/api/v0/equity/portfolio",
                    headers=headers,
                    timeout=5,
                )
                if res_demo.status_code == 200:
                    st.success("✅ Demo Environment: Connected & Authenticated.")
                elif res_demo.status_code == 401:
                    st.info(
                        "ℹ️ Demo Environment: 401 Unauthorized (Normal & expected if using a Live key)."
                    )
                else:
                    st.warning(f"⚠️ Demo Environment: HTTP {res_demo.status_code}")
            except Exception as e:
                st.error(f"Demo Environment Network Error: {e}")
        else:
            st.error("❌ Trading 212: API Key or Secret missing from secrets.")

        # --- 2. FINNHUB TEST ---
        st.write("**2. Finnhub News Data Status**")
        if FINNHUB_KEY:
            try:
                url = f"https://finnhub.io/api/v1/company-news?symbol=AAPL&from={datetime.now().strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                res_finn = requests.get(url, timeout=5)
                if res_finn.status_code == 200:
                    st.success("✅ Finnhub: Connected & Streaming.")
                else:
                    st.error(f"❌ Finnhub Error: HTTP {res_finn.status_code}")
            except Exception as e:
                st.error(f"Finnhub Network Error: {e}")
        else:
            st.error("❌ Finnhub: API Key missing.")

        # --- 3. GROQ AI TEST ---
        st.write("**3. Groq Llama-3 AI Status**")
        if GROQ_API_KEY:
            try:
                groq_client.chat.completions.create(
                    messages=[{"role": "user", "content": "Reply 'OK'"}],
                    model="llama-3.3-70b-versatile",
                    max_tokens=5,
                )
                st.success("✅ Groq AI: Connected & Processing.")
            except Exception as e:
                st.error(f"❌ Groq Error: {str(e)[:100]}")
        else:
            st.error("❌ Groq: API Key missing.")
