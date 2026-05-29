import streamlit as st
import requests
import time
import threading
import os
import random
from datetime import datetime, timedelta
from google import genai

# --- 1. CONFIGURATION & SECRETS ---
# Load API Keys securely from Streamlit Cloud Secrets (or local .env)
FINNHUB_KEY = os.getenv("FINNHUB_KEY") or st.secrets.get("FINNHUB_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC") or st.secrets.get("NTFY_TOPIC")
GEMINI_KEY = os.getenv("GEMINI_KEY") or st.secrets.get("GEMINI_KEY")

if not GEMINI_KEY:
    st.error("GEMINI_KEY not found! Please check your Streamlit Cloud secrets.")
    st.stop()

# Initialize AI Client
client = genai.Client(api_key=GEMINI_KEY)

# Define your static portfolio here
MY_PORTFOLIO = [
    {"symbol": "MU"},
    {"symbol": "VUSA.L"},
    {"symbol": "AIAI.L"}
    # Add the rest of your stocks here...
]

# --- 2. CORE FUNCTIONS ---
def send_ntfy(title, message):
    """Sends a push notification to your phone via ntfy."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={"Title": title, "Tags": "rotating_light"}
        )
    except Exception as e:
        print(f"[DEBUG] Failed to send ntfy alert: {e}")

def analyze_news(headline):
    """The Gatekeeper: Only sends actionable news to the AI."""
    # Only news with these words gets sent to the AI
    keywords = ["earnings", "dividend", "upgrade", "downgrade", "acquisition", "merger", "ceo", "guidance", "sec", "filed"]
    
    is_actionable = any(word in headline.lower() for word in keywords)
    
    if not is_actionable:
        return  # Silent drop: AI is not bothered, no notification sent

    prompt = f"Analyze this market-moving headline: '{headline}'. Tell me if this is a BUY, SELL, or HOLD. Give 2 sentences max."
    
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        send_ntfy(f"🚨 ACTIONABLE NEWS: {headline}", response.text.strip())
    except Exception as e:
        print(f"[DEBUG] AI busy, skipping: {e}")

# --- 3. THE MASTER BACKGROUND PATROL ---
def master_patrol():
    """Runs 24/7 in the background, managing both the morning brief and 10-minute skims."""
    print("[DEBUG] Master patrol background thread initialized.")
    last_brief_date = None

    while True:
        # If both switches are turned off, sleep briefly and loop until one opens
        if not st.session_state.skimmer_active and not st.session_state.brief_active:
            time.sleep(5)
            continue
            
        now = datetime.now()
        current_portfolio = MY_PORTFOLIO
        
        # --- FEATURE 1: 6 AM MORNING BRIEF ---
        if st.session_state.brief_active:
            # 5 AM UTC = 6 AM BST
            if now.hour == 5 and now.date() != last_brief_date:
                print("[DEBUG] Generating Morning Brief...")
                target_stocks = random.sample(current_portfolio, min(3, len(current_portfolio)))
                yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                
                for item in target_stocks:
                    symbol = item['symbol']
                    try:
                        url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday_str}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                        news = requests.get(url).json()
                        
                        if news:
                            headline = news[0]['headline']
                            prompt = f"Give a 1-sentence morning summary for {symbol} based on this headline: '{headline}'"
                            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
                            send_ntfy(f"🌅 {symbol} Morning Brief", response.text.strip())
                    except Exception as e:
                        print(f"[DEBUG] Brief skip for {symbol}: {e}")
                
                last_brief_date = now.date()

        # --- FEATURE 2: 10-MINUTE SKIMMER ---
        if st.session_state.skimmer_active:
            print("[DEBUG] Running routine 10-minute skim...")
            for item in current_portfolio:
                symbol = item['symbol']
                try:
                    url = f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={now.strftime('%Y-%m-%d')}&to={now.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
                    news_items = requests.get(url).json()
                    
                    if news_items:
                        analyze_news(news_items[0]['headline'])
                except Exception as e:
                    print(f"[DEBUG] News patrol skip for {symbol}: {e}")
        
        # Rest for 10 minutes before checking conditions again
        time.sleep(600)

# --- 4. STREAMLIT USER INTERFACE ---
st.title("📈 Notif-ISA Trading Bot")

# Initialize session state flags safely
if 'skimmer_active' not in st.session_state:
    st.session_state.skimmer_active = False
if 'brief_active' not in st.session_state:
    st.session_state.brief_active = False
if 'master_thread_running' not in st.session_state:
    st.session_state.master_thread_running = False

# Start the background thread once
if not st.session_state.master_thread_running:
    t = threading.Thread(target=master_patrol, daemon=True)
    t.start()
    st.session_state.master_thread_running = True

# GUI Control Panel
st.markdown("---")
st.subheader("⚙️ Bot Control Panel")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### 🌅 Morning Brief (6 AM)")
    if st.session_state.get('brief_active', False):
        st.success("Active")
        if st.button("🔴 Stop Brief", key="stop_brief", use_container_width=True):
            st.session_state.brief_active = False
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Brief", key="start_brief", use_container_width=True):
            st.session_state.brief_active = True
            st.rerun()

with col2:
    st.markdown("#### 🔍 10-Min Skimmer")
    if st.session_state.get('skimmer_active', False):
        st.success("Active")
        if st.button("🔴 Stop Skimmer", key="stop_skim", use_container_width=True):
            st.session_state.skimmer_active = False
            st.rerun()
    else:
        st.warning("Paused")
        if st.button("🟢 Start Skimmer", key="start_skim", use_container_width=True):
            st.session_state.skimmer_active = True
            st.rerun()