import os
from flask import Flask, render_template_string, request, jsonify
from supabase import create_client
import uuid
from datetime import datetime
import requests
import time
import threading
import json

# ========== FREEFLOW LLM (MULTI-KEY AUTO FALLBACK) ==========
from freeflow_llm import FreeFlowClient, NoProvidersAvailableError

app = Flask(__name__)

MODEL_NAME = "llama-3.1-8b-instant"  # High limit model

# SUPABASE MEMORY
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase memory connected")
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        supabase = None

# ==================== GLOBAL KNOWLEDGE BASE ====================
knowledge_base = {
    "dex": {
        "uniswap": {},
        "pancakeswap": {},
        "aerodrome": {},
        "raydium": {},
        "jupiter": {}
    },
    "coding": {
        "github": [],
        "stackoverflow": [],
        "medium": [],
        "youtube": []
    },
    "airdrops": {
        "active": [],
        "upcoming": [],
        "ended": []
    },
    "trading": {
        "news": [],
        "fear_greed": {},
        "market_data": {}
    }
}

# ==================== DEX DATA FETCHERS ====================
def fetch_uniswap_data():
    """Uniswap V3 data"""
    try:
        url = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
        query = """
        {
          pools(first: 10, orderBy: totalValueLockedUSD, orderDirection: desc) {
            id
            token0 { symbol name }
            token1 { symbol name }
            token0Price
            token1Price
            volumeUSD
            totalValueLockedUSD
          }
        }
        """
        response = requests.post(url, json={'query': query})
        data = response.json()
        knowledge_base["dex"]["uniswap"] = {
            "top_pools": data.get('data', {}).get('pools', []),
            "timestamp": datetime.utcnow().isoformat()
        }
        print("✅ Uniswap data fetched")
    except Exception as e:
        print(f"❌ Uniswap error: {e}")

def fetch_pancakeswap_data():
    """PancakeSwap data"""
    try:
        url = "https://api.thegraph.com/subgraphs/name/pancakeswap/exchange"
        query = """
        {
          pairs(first: 10, orderBy: reserveUSD, orderDirection: desc) {
            id
            token0 { symbol }
            token1 { symbol }
            reserveUSD
            volumeUSD
          }
        }
        """
        response = requests.post(url, json={'query': query})
        data = response.json()
        knowledge_base["dex"]["pancakeswap"] = {
            "top_pairs": data.get('data', {}).get('pairs', []),
            "timestamp": datetime.utcnow().isoformat()
        }
        print("✅ PancakeSwap data fetched")
    except Exception as e:
        print(f"❌ PancakeSwap error: {e}")

def fetch_aerodrome_data():
    """Aerodrome data via DEX Screener"""
    try:
        response = requests.get("https://api.dexscreener.com/latest/dex/search?q=aerodrome")
        if response.status_code == 200:
            knowledge_base["dex"]["aerodrome"] = {
                "pairs": response.json().get('pairs', [])[:5],
                "timestamp": datetime.utcnow().isoformat()
            }
            print("✅ Aerodrome data fetched")
    except Exception as e:
        print(f"❌ Aerodrome error: {e}")

def fetch_raydium_data():
    """Raydium data"""
    try:
        response = requests.get("https://api.raydium.io/v2/main/pools")
        if response.status_code == 200:
            knowledge_base["dex"]["raydium"] = {
                "pools": response.json()[:5],
                "timestamp": datetime.utcnow().isoformat()
            }
            print("✅ Raydium data fetched")
    except Exception as e:
        print(f"❌ Raydium error: {e}")

def fetch_jupiter_data():
    """Jupiter aggregator data"""
    try:
        response = requests.get("https://quote-api.jup.ag/v6/price?ids=SOL,USDC,RAY,BONK,JUP")
        if response.status_code == 200:
            knowledge_base["dex"]["jupiter"] = {
                "prices": response.json(),
                "timestamp": datetime.utcnow().isoformat()
            }
            print("✅ Jupiter data fetched")
    except Exception as e:
        print(f"❌ Jupiter error: {e}")

# ==================== CODING LEARNING SOURCES ====================
def fetch_coding_data():
    """GitHub, StackOverflow, Medium se coding seekho"""
    try:
        # GitHub trending repos
        github = requests.get("https://api.github.com/search/repositories?q=blockchain+crypto+web3+python&sort=stars&per_page=5")
        if github.status_code == 200:
            knowledge_base["coding"]["github"] = github.json().get('items', [])
        
        # Stack Overflow
        stack = requests.get("https://api.stackexchange.com/2.3/questions?order=desc&sort=activity&tagged=python;solidity;web3&site=stackoverflow")
        if stack.status_code == 200:
            knowledge_base["coding"]["stackoverflow"] = stack.json().get('items', [])[:5]
        
        print("✅ Coding data fetched")
    except Exception as e:
        print(f"❌ Coding error: {e}")

# ==================== AIRDROP HUNTING SOURCES ====================
def fetch_airdrops_data():
    """Latest airdrops hunt karo"""
    try:
        # DEX Screener se naye tokens
        dex_response = requests.get("https://api.dexscreener.com/latest/dex/search?q=new+pairs")
        
        # Airdrop alert (simulated - real API nahi hai to example data)
        airdrops = [
            {"name": "zkSync Era", "status": "Active", "value": "$1000+", "end": "March 2025"},
            {"name": "LayerZero", "status": "Upcoming", "value": "TBA", "end": "Q2 2025"},
            {"name": "Eclipse", "status": "Active", "value": "$500+", "end": "April 2025"},
            {"name": "StarkNet", "status": "Active", "value": "$2000+", "end": "March 2025"},
            {"name": "Scroll", "status": "Upcoming", "value": "TBA", "end": "Q2 2025"}
        ]
        
        knowledge_base["airdrops"]["active"] = airdrops
        knowledge_base["airdrops"]["new_tokens"] = dex_response.json().get('pairs', [])[:5] if dex_response.status_code == 200 else []
        
        print("✅ Airdrop data fetched")
    except Exception as e:
        print(f"❌ Airdrop error: {e}")

# ==================== TRADING LEARNING SOURCES ====================
def fetch_trading_data():
    """Trading signals aur market data"""
    try:
        # Crypto news
        news = requests.get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN&limit=5")
        
        # Fear & Greed Index
        fear_greed = requests.get("https://api.alternative.me/fng/?limit=1")
        
        knowledge_base["trading"]["news"] = news.json().get('Data', []) if news.status_code == 200 else []
        knowledge_base["trading"]["fear_greed"] = fear_greed.json().get('data', []) if fear_greed.status_code == 200 else []
        
        print("✅ Trading data fetched")
    except Exception as e:
        print(f"❌ Trading error: {e}")

# ==================== 24x7 LEARNING ENGINE ====================
def continuous_learning():
    """Main learning loop - 24x7 sab seekho"""
    while True:
        print("\n🤖 24x7 LEARNING CYCLE STARTED...")
        
        # DEX data
        fetch_uniswap_data()
        fetch_pancakeswap_data()
        fetch_aerodrome_data()
        fetch_raydium_data()
        fetch_jupiter_data()
        
        # Coding data
        fetch_coding_data()
        
        # Airdrop data
        fetch_airdrops_data()
        
        # Trading data
        fetch_trading_data()
        
        # Save to Supabase
        if supabase:
            try:
                supabase.table("knowledge").insert({
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": knowledge_base
                }).execute()
                print("📚 All knowledge saved to database")
            except:
                pass
        
        print("😴 Sleeping for 5 minutes...")
        time.sleep(300)  # 5 minutes

# Start learning thread
learning_thread = threading.Thread(target=continuous_learning, daemon=True)
learning_thread.start()
print("🚀 24x7 LEARNING ENGINE STARTED!")

# ==================== UI ====================
HTML = """ ... """  # (same as user's original HTML, no changes needed)

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not user_message:
        return jsonify({"reply": "Kuch likho bhai!", "session_id": session_id})

    try:
        # Create powerful system prompt with all current knowledge
        system_prompt = f"""Tu MrBlack hai - ek self-learning PRO bot jo 24x7 teeno fields seekhta hai:

📚 CURRENT KNOWLEDGE (Real-time data):

1. DEX TRADING:
   - Uniswap: {len(knowledge_base['dex']['uniswap'].get('top_pools', []))} top pools tracked
   - PancakeSwap: {len(knowledge_base['dex']['pancakeswap'].get('top_pairs', []))} top pairs
   - Aerodrome: {len(knowledge_base['dex']['aerodrome'].get('pairs', []))} active pairs
   - Raydium: {len(knowledge_base['dex']['raydium'].get('pools', []))} SOL pools
   - Jupiter: Latest SOL prices available

2. CODING (From GitHub & StackOverflow):
   - Trending: {knowledge_base['coding']['github'][0]['name'] if knowledge_base['coding']['github'] else 'Loading...'}
   - Latest discussions: {len(knowledge_base['coding']['stackoverflow'])} active topics

3. AIRDROP HUNTING:
   - Active airdrops: {len(knowledge_base['airdrops']['active'])} hunting now
   - New tokens: {len(knowledge_base['airdrops'].get('new_tokens', []))} just launched

4. TRADING SIGNALS:
   - Latest news: {len(knowledge_base['trading']['news'])} crypto updates
   - Market sentiment: {knowledge_base['trading']['fear_greed'][0].get('value', 'N/A') if knowledge_base['trading']['fear_greed'] else 'Loading...'}/100

TERI SPECIALIZATIONS:
🦄 Uniswap Expert - pools, fees, yields
🥞 PancakeSwap Pro - farming, CAKE, BSC
✈️ Aerodrome Master - Base chain, AERO
☀️ Raydium Specialist - Solana, Serum
📚 Coding Guru - Python, Solidity, Web3
🎁 Airdrop Hunter - find, qualify, claim
📊 Trading Coach - TA, risk management

SEEKHNE KA TARIQA:
- Har 5 minute mein naya data fetch
- Previous conversations se improve
- Beginner se pro tak gradually
- Real examples ke saath sikhao
- Paper trading practice

TERA STYLE:
- Hinglish mein baat
- Confident but friendly
- "Abhi maine ye seekha" batao
- Step-by-step guide do
- Copy-paste mat karo

AVAILABLE HELP:
📌 "Uniswap top pools dikhao"
📌 "PancakeSwap farming kaise karein"
📌 "Naye airdrops batayo"
📌 "Python coding seekhna hai"
📌 "Bitcoin ka trend kya hai"
📌 "Raydium par SOL/USDC price"
📌 "Aerodrome fees kitni hai"

Yaad rakh: Tu 24x7 seekh raha hai, har din pro ban raha hai! 🚀"""

        messages = [{"role": "system", "content": system_prompt}]

        # Fetch memory
        if supabase:
            try:
                hist = supabase.table("memory").select("role,content").eq("session_id", session_id).order("created_at").limit(30).execute()
                if hist.data:
                    for m in hist.data:
                        messages.append({"role": m["role"], "content": m["content"]})
            except Exception as e:
                print(f"Memory fetch error: {e}")

        messages.append({"role": "user", "content": user_message})

        # ========== USE FREEFLOW CLIENT ==========
        with FreeFlowClient() as ffc:
            try:
                response = ffc.chat(
                    messages=messages,
                    model=MODEL_NAME,
                    temperature=0.8,
                    max_tokens=1000
                )
                reply = response.content
                print(f"✅ Provider used: {response.provider}")
            except NoProvidersAvailableError:
                reply = "Sab providers exhausted! Thodi der mein try karo."
            except Exception as e:
                reply = f"Error: {str(e)}"

        # Save memory
        if supabase:
            try:
                supabase.table("memory").insert([
                    {"session_id": session_id, "role": "user", "content": user_message, "created_at": datetime.utcnow().isoformat()},
                    {"session_id": session_id, "role": "assistant", "content": reply, "created_at": datetime.utcnow().isoformat()}
                ]).execute()
            except Exception as e:
                print(f"Memory save error: {e}")

    except Exception as e:
        print(f"Error: {e}")
        reply = f"Error: {str(e)}"

    return jsonify({"reply": reply, "session_id": session_id})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
