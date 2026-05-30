import streamlit as st
import requests
import time
import threading
import os
import random
import traceback
from datetime import datetime, timedelta
import yfinance as yf
from groq import Groq

st.set_page_config(page_title="ISA Trading Bot", layout="wide", page_icon="📈")

# --- 1. CONFIGURATION & SECRETS ---
FINNHUB_KEY = os.getenv("FINNHUB_KEY") or st.secrets.get("FINNHUB_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC") or st.secrets.get("NTFY_TOPIC")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or st.secrets.get("GROQ_API_KEY")

if not GROQ_API_KEY:
    st.error("GROQ_API_KEY not found! Please check your secrets.")
    st.stop()

groq_client = Groq(api_key=GROQ_API_KEY)


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

    return SharedState()


shared_state = get_shared_state()


# --- 3. CORE FUNCTIONS (Top Level - Zero Indentation) ---
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
    api_key_id = os.getenv("T212_API_KEY_ID") or st.secrets.get("T212_API_KEY_ID")
    api_secret = os.getenv("T212_API_SECRET") or st.secrets.get("T212_API_SECRET")
    url = "https://live.trading212.com/api/v0/equity/portfolio"

    try:
        response = requests.get(url, auth=(api_key_id, api_secret), timeout=10)
        if response.status_code == 200:
            print(f"[{get_timestamp()}] [SYSTEM] T212 Portfolio Sync Successful!")

            clean_portfolio = []
            for item in response.json():
                raw_ticker = item["ticker"]

                if "_US_EQ" in raw_ticker:
                    clean_ticker = raw_ticker.replace("_US_EQ", "")
                elif "l_EQ" in raw_ticker:  # Fixed lowercase tag mapping
                    clean_ticker = raw_ticker.replace("l_EQ", ".L")
                else:
                    clean_ticker = raw_ticker.replace("_EQ", "")

                clean_portfolio.append({"symbol": clean_ticker})

            return clean_portfolio
        else:
            print(
                f"[{get_timestamp()}] [SYSTEM] T212 API Error {response.status_code}: {response.text}"
            )
    except Exception as e:
        print(f"[{get_timestamp()}] [SYSTEM] Failed to fetch T212 portfolio: {e}")

    return [{"symbol": "MU"}, {"symbol": "AVGO"}]


# Initialize the portfolio globally right after the function definition
MY_PORTFOLIO = get_portfolio_from_t212()


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
        return

    if headline in state.processed_headlines:
        print(
            f"[{get_timestamp()}] [GATEKEEPER] BLOCKED: Already analyzed '{headline}' today."
        )
        return

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
        print(f"[{get_timestamp()}] [GATEKEEPER] REJECTED: No keywords found.")
        state.processed_headlines.add(headline)
        return

    print(f"[{get_timestamp()}] [GATEKEEPER] APPROVED: Sending to Groq API...")
    prompt = (
        f"Analyze this market-moving headline: '{headline}' for {symbol}. "
        f"1. State clearly if this is a BUY, SELL, or HOLD. "
        f"2. Suggest whether the user should use a 'Limit Order' or a 'Stop-Limit Order' to execute this, based on the expected market volatility of the news. "
        f"Keep it to 2 short sentences. maximum 5 sentences."
    )

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.2,
        )
        ai_response_text = chat_completion.choices[0].message.content.strip()

        state.daily_ai_calls += 1
        log_event(state, f"RAW GROQ RESPONSE: {ai_response_text}")
        send_ntfy(f"🚨 {symbol} ACTIONABLE NEWS", ai_response_text)
        state.processed_headlines.add(headline)

    except Exception as e:
        log_event(state, f"AI CRASH: \n{traceback.format_exc()}", is_error=True)
        send_ntfy(f"⚠️ Skimmer AI Failed: {symbol}", f"Error: {str(e)}")


# --- 4. BACKGROUND RADAR ENGINES ---
def price_patrol(state):
    print(f"[{get_timestamp()}] [SYSTEM] Real-Time Price Radar booted.")
    high_water_marks = {}

    while True:
        if not state.price_monitor_active:
            time.sleep(10)
            continue

        for item in MY_PORTFOLIO:
            symbol = item["symbol"]
            try:
                ticker = yf.Ticker(symbol)
                current_price = ticker.fast_info["lastPrice"]

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

            except Exception as e:
                pass

        time.sleep(60)


def master_patrol(state):
    print(f"[{get_timestamp()}] [SYSTEM] Background engine booted.")
    system_health_check()

    last_brief_date = None
    last_memory_flush_date = datetime.now().date()

    try:
        while True:
            now = datetime.now()

            if now.date() != last_memory_flush_date:
                print(
                    f"[{get_timestamp()}] [SYSTEM] Midnight protocol: Flushing memory & resetting Groq quota."
                )
                state.processed_headlines.clear()
                state.daily_ai_calls = 0
                last_memory_flush_date = now.date()

            if not state.skimmer_active and not state.brief_active:
                time.sleep(10)
                continue

            current_portfolio = MY_PORTFOLIO

            # FEATURE 1: 6 AM MORNING BRIEF
            if state.brief_active:
                if now.hour == 5 and now.date() != last_brief_date:
                    print(f"[{get_timestamp()}] [BRIEF] INITIATING 6AM SEQUENCE...")
                    target_stocks = random.sample(
                        current_portfolio, min(3, len(current_portfolio))
                    )
                    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

                    for item in target_stocks:
                        symbol = item["symbol"]
                        try:
                            print(
                                f"[{get_timestamp()}] [BRIEF] Fetching Finnhub for {symbol}..."
                            )
                            url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday_str}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                            response = requests.get(url)
                            news = response.json()

                            if isinstance(news, list) and len(news) > 0:
                                if state.daily_ai_calls >= 500:
                                    log_event(
                                        state,
                                        f"Skipping Brief for {symbol}: Groq Limit Reached.",
                                    )
                                    continue

                                headline = news[0]["headline"]
                                prompt = f"Give a 1-sentence morning summary for {symbol} based on this headline: '{headline}'"

                                chat_completion = groq_client.chat.completions.create(
                                    messages=[{"role": "user", "content": prompt}],
                                    model="llama-3.3-70b-versatile",
                                    temperature=0.2,
                                )
                                ai_response_text = chat_completion.choices[
                                    0
                                ].message.content.strip()

                                state.daily_ai_calls += 1
                                send_ntfy(
                                    f"🌅 {symbol} Morning Brief", ai_response_text
                                )
                            else:
                                print(
                                    f"[{get_timestamp()}] [BRIEF] PASS: No valid market news for {symbol}."
                                )
                        except Exception as e:
                            error_trace = traceback.format_exc()
                            log_event(
                                state,
                                f"BRIEF CRASH on {symbol}: \n{error_trace}",
                                is_error=True,
                            )
                            send_ntfy(f"⚠️ Brief Failed: {symbol}", f"Error: {str(e)}")

                    last_brief_date = now.date()

            # FEATURE 2: 10-MINUTE SKIMMER (Smart Router)
            if state.skimmer_active:
                if now.hour == 5 and state.brief_active:
                    log_event(
                        state, "Skimmer skipped to prevent duplicate morning alerts."
                    )
                else:
                    log_event(state, "Executing smart portfolio sweep...")
                    for item in current_portfolio:
                        symbol = item["symbol"]
                        try:
                            if symbol.endswith(".L"):
                                log_event(
                                    state, f"Routing {symbol} to Yahoo Finance..."
                                )
                                ticker = yf.Ticker(symbol)
                                news = ticker.news
                                if news and isinstance(news, list):
                                    analyze_news(news[0]["title"], symbol, state)
                            else:
                                url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={now.strftime('%Y-%m-%d')}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                                response = requests.get(url)
                                news_items = response.json()
                                if isinstance(news_items, list) and len(news_items) > 0:
                                    analyze_news(
                                        news_items[0]["headline"], symbol, state
                                    )
                        except Exception as e:
                            log_event(
                                state,
                                f"SKIMMER CRASH on {symbol}: \n{traceback.format_exc()}",
                                is_error=True,
                            )
                            send_ntfy(
                                f"⚠️ Skimmer Failed: {symbol}", f"Error: {str(e)}"
                            )

            log_event(state, "Sweep cycle finished. Sleeping 600s (10 minutes).")
            time.sleep(600)

    except Exception as fatal_error:
        error_details = str(fatal_error)
        log_event(state, f"FATAL ENGINE CRASH: {error_details}", is_error=True)
        send_ntfy(
            "🚨 BOT OFFLINE: Fatal Crash",
            f"The master loop died. Reason: {error_details}",
        )
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

calls_made = shared_state.daily_ai_calls
calls_remaining = 500 - calls_made
st.progress(
    min(calls_made / 500.0, 1.0),
    text=f"🧠 Groq Llama 3 Quota: {calls_made} / 500 (Remaining: {calls_remaining})",
)
st.write("")

col1, col2, col3 = st.columns(3)

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
st.markdown("#### 🛠️ Diagnostics")
if st.button("🔔 Send Manual Test Notification"):
    success, details = send_ntfy(
        "✅ Dashboard Connected", "Diagnostic tracing enabled."
    )
    if success:
        st.success(f"Signal fired successfully! Server responded: {details}")
    else:
        st.error(f"Signal failed! Error details: {details}")

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

if st.button("🔄 Refresh Logs", key="force_refresh_logs_btn"):
    st.rerun()
