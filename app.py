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
import json

st.set_page_config(page_title="ISA Trading Bot", layout="wide", page_icon="📈")

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

    if not api_key_id or not api_secret:
        st.error(
            "🚨 API Keys Missing! Please add both T212_API_KEY_ID and T212_API_SECRET to your secrets."
        )
        return []

    # If using a Practice account, change 'live' to 'demo' in the URL below
    url = "https://live.trading212.com/api/v0/equity/portfolio"

    try:
        response = requests.get(url, auth=(api_key_id, api_secret), timeout=10)

        if response.status_code == 200:
            print(f"[{get_timestamp()}] [SYSTEM] T212 Portfolio Sync Successful!")
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
                        "avg_price": item.get("averagePrice", 0),
                        "current_price": item.get("currentPrice", 0),
                        "profit": item.get("ppl", 0),
                    }
                )
            return clean_portfolio
        else:
            st.error(
                f"🚨 T212 Connection Rejected! Status {response.status_code}: {response.text}"
            )
            return []
    except Exception as e:
        st.error(f"🚨 Network Error: {e}")
        return []


MY_PORTFOLIO = get_portfolio_from_t212()


def get_reinvestment_advice(portfolio, watchlist, state):
    if state.daily_ai_calls >= 500:
        return "⚠️ Groq daily limit reached. Cannot generate strategy."

    profitable = [p for p in portfolio if p.get("profit", 0) > 0]
    if not profitable:
        return "No profitable positions available to skim from right now. Hold steady."

    portfolio_summary = ", ".join(
        [
            f"{p['symbol']} (+£{p['profit']:.2f} on {p['shares']} shares)"
            for p in profitable
        ]
    )
    watchlist_summary = ", ".join(watchlist) if watchlist else "None"

    prompt = (
        f"My current profitable stock holdings are: {portfolio_summary}. "
        f"My current watchlist for buying is: {watchlist_summary}. "
        "Act as a ruthless, strategic trading assistant handling fractional shares. "
        "STRICT RULES: "
        "1. ONLY recommend 'skimming the profit'. Tell me to sell the exact monetary value of the profit, leaving the principal investment perfectly intact. "
        "2. EXCEPTION: You may recommend selling 100% of a holding ONLY IF the stock is performing poorly OR it is highly strategic to rotate all that capital into a specific watchlist stock. "
        "3. Tell me exactly which watchlist stock to roll the money into. Keep the response to 3 punchy, actionable sentences."
    )

    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.2,
        )
        state.daily_ai_calls += 1
        return chat.choices[0].message.content.strip()
    except Exception as e:
        return f"Error contacting AI: {str(e)}"


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

    # --- FIXED: The Gatekeeper Block ---
    is_actionable = any(word in headline.lower() for word in keywords)
    if not is_actionable:
        state.processed_headlines.add(headline)
        return False
    # -----------------------------------

    print(f"[{get_timestamp()}] [GATEKEEPER] APPROVED: Sending to Groq API...")
    prompt = f"Analyze this market-moving headline: '{headline}' for {symbol}. 1. State clearly if this is a BUY, SELL, or HOLD. 2. Suggest whether the user should use a 'Limit Order' or a 'Stop-Limit Order'. Keep it to 2-5 short sentences."

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

        return True  # <--- Triggers the reinvestment strategy

    except Exception as e:
        log_event(state, f"AI CRASH: \n{traceback.format_exc()}", is_error=True)
        send_ntfy(f"⚠️ Skimmer AI Failed: {symbol}", f"Error: {str(e)}")
        return False


# --- 4. BACKGROUND ENGINES ---
def price_patrol(state):
    print(f"[{get_timestamp()}] [SYSTEM] Real-Time Price Radar booted.")
    high_water_marks = {}
    while True:
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
            now = datetime.now()
            if now.date() != last_memory_flush_date:
                print(
                    f"[{get_timestamp()}] [SYSTEM] Midnight protocol: Flushing memory."
                )
                state.processed_headlines.clear()
                state.daily_ai_calls = 0
                last_memory_flush_date = now.date()

            if not state.skimmer_active and not state.brief_active:
                time.sleep(10)
                continue

            combined_portfolio = MY_PORTFOLIO.copy()
            for t in state.custom_watchlist:
                if not any(item["symbol"] == t for item in combined_portfolio):
                    combined_portfolio.append({"symbol": t})

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
st.markdown("#### 📝 Discovery Watchlist")
st.caption(
    "Bulk add symbols below. You can separate them by commas or paste them as a list (one per line)."
)

# Upgraded to a large, multi-line text box
watchlist_input = st.text_area(
    "Watchlist Symbols:", value="\n".join(shared_state.custom_watchlist), height=250
)

if watchlist_input is not None:
    # Safely handle both commas and new-lines from copy/pasting
    clean_input = watchlist_input.replace("\n", ",")

    # Strip whitespace, capitalize, and automatically remove any duplicate tickers
    new_watchlist = []
    for t in clean_input.split(","):
        t = t.strip().upper()
        if t and t not in new_watchlist:
            new_watchlist.append(t)

    if new_watchlist != shared_state.custom_watchlist:
        shared_state.custom_watchlist = new_watchlist
        save_watchlist(new_watchlist)

        send_ntfy(
            "📝 Watchlist Merged",
            f"Now tracking {len(new_watchlist)} custom stocks alongside your main T212 portfolio.",
        )

        st.rerun()
st.markdown("---")
st.markdown("#### 📊 Live Portfolio Holdings")
st.dataframe(MY_PORTFOLIO, use_container_width=True)

st.markdown("---")
st.markdown("#### 🧠 AI Profit Skimmer & Reinvestment Strategy")
st.caption(
    "Analyzes your live T212 profits and your Discovery Watchlist to suggest capital rotation."
)

if st.button(
    "Calculate Reinvestment Strategy", key="reinvest_btn", use_container_width=True
):
    with st.spinner("AI is analyzing your live profits and watchlist targets..."):
        advice = get_reinvestment_advice(
            MY_PORTFOLIO, shared_state.custom_watchlist, shared_state
        )
        st.success("Strategy Generated!")
        st.info(advice)
        send_ntfy("🧠 Reinvestment Strategy", advice)

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
