import streamlit as st
import requests
import time
import threading
import os
import random
from datetime import datetime, timedelta
from google import genai

# --- 1. CONFIGURATION & SECRETS ---
FINNHUB_KEY = os.getenv("FINNHUB_KEY") or st.secrets.get("FINNHUB_KEY")
NTFY_TOPIC = os.getenv("NTFY_TOPIC") or st.secrets.get("NTFY_TOPIC")
GEMINI_KEY = os.getenv("GEMINI_KEY") or st.secrets.get("GEMINI_KEY")

if not GEMINI_KEY:
    st.error("GEMINI_KEY not found! Please check your Streamlit Cloud secrets.")
    st.stop()

client = genai.Client(api_key=GEMINI_KEY)

MY_PORTFOLIO = [
    {"symbol": "MU"},
    {"symbol": "VUSA.L"},
    {"symbol": "AIAI.L"}
    # Add the rest of your stocks here...
]

# --- 2. CORE FUNCTIONS ---
def get_timestamp():
    """Helper to get a clean timestamp for the logs."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

def send_ntfy(title, message):
    print(f"[{get_timestamp()}] [NTFY] Attempting to send ping to phone: {title}")
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={"Title": title, "Tags": "rotating_light"}
        )
        if response.status_code == 200:
            print(f"[{get_timestamp()}] [NTFY] SUCCESS - Notification sent.")
        else:
            print(f"[{get_timestamp()}] [NTFY] ERROR - Server responded with code {response.status_code}")
    except Exception as e:
        print(f"[{get_timestamp()}] [NTFY] CRITICAL ERROR - {e}")

def analyze_news(headline, symbol):
    print(f"[{get_timestamp()}] [GATEKEEPER] Analyzing headline for {symbol}: '{headline}'")
    keywords = ["earnings", "dividend", "upgrade", "downgrade", "acquisition", "merger", "ceo", "guidance", "sec", "filed"]
    
    is_actionable = any(word in headline.lower() for word in keywords)
    
    if not is_actionable:
        print(f"[{get_timestamp()}] [GATEKEEPER] DROPPED - No action keywords found in: '{headline}'")
        return 

    print(f"[{get_timestamp()}] [GATEKEEPER] PASSED - Keyword found! Sending to Gemini AI...")
    prompt = f"Analyze this market-moving headline: '{headline}'. Tell me if this is a BUY, SELL, or HOLD. Give 2 sentences max."
    
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        print(f"[{get_timestamp()}] [AI] Analysis complete. Triggering phone alert.")
        send_ntfy(f"🚨 {symbol} ACTIONABLE NEWS", response.text.strip())
    except Exception as e:
        print(f"[{get_timestamp()}] [AI] ERROR - Gemini API failed: {e}")

# --- 3. THE MASTER BACKGROUND PATROL ---
def master_patrol():
    print(f"[{get_timestamp()}] [SYSTEM] Master patrol background thread INITIALIZED and running.")
    last_brief_date = None

    while True:
        if not st.session_state.skimmer_active and not st.session_state.brief_active:
            print(f"[{get_timestamp()}] [SYSTEM] Both features PAUSED. Sleeping for 30 seconds...")
            time.sleep(30)
            continue
            
        now = datetime.now()
        current_portfolio = MY_PORTFOLIO
        
        # FEATURE 1: 6 AM MORNING BRIEF
        if st.session_state.brief_active:
            print(f"[{get_timestamp()}] [BRIEF] Checking clock... Current UTC hour is {now.hour}.")
            # 5 AM UTC = 6 AM