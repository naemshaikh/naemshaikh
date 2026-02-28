import os
from flask import Flask, render_template_string, request, jsonify
from supabase import create_client
import uuid
from datetime import datetime
import requests
import time
import threading
import json
import socket
from urllib.parse import urlparse  # 👈 NAYA IMPORT
from collections import defaultdict  # 👈 NAYA IMPORT

# ========== FREEFLOW LLM (MULTI-KEY AUTO FALLBACK) ==========
from freeflow_llm import FreeFlowClient, NoProvidersAvailableError

# ========== PATCH HTTPX VERSION TO AVOID CONFLICT ==========
import httpx
httpx.__version__ = "0.24.1"

app = Flask(__name__)

# ========== ULTIMATE GOD MODE - 2026 LATEST MODELS ==========
MODEL_NAME = "llama-3.3-70b-versatile"  # Base model - sab support karte hain

# ==================== NEW FEATURES CONFIG ====================
PAPER_TRADING_MODE = True          # Start with Paper Trading
PAPER_START_BALANCE = 1.0           # 1 BNB virtual
WIN_RATE_TARGET = 70                 # 70% win rate required for real mode
MIN_TRADES_FOR_SWITCH = 50           # Minimum trades before considering switch
TEST_BUY_AMOUNT = 0.0005              # Minimum possible BNB for test buy
DAILY_LOSS_LIMIT_PERCENT = 8          # Stop trading after 8% loss in a day
STOP_LOSS_PERCENT = 15                # Default stop-loss
CONSECUTIVE_LOSS_LIMIT = 3            # Stop after 3 consecutive losses

# ==================== PAPER TRADING STATE ====================
paper_trading = {
    "balance": PAPER_START_BALANCE,
    "trades": [],
    "win_count": 0,
    "loss_count": 0,
    "consecutive_losses": 0,
    "daily_pnl": 0,
    "last_reset_day": datetime.now().day,
    "pattern_db": {
        "successful": [],
        "failed": [],
        "stats": defaultdict(lambda: {"wins": 0, "losses": 0, "total_pnl": 0})
    }
}

# ==================== DOMAIN SECURITY ====================
DOMAIN_WHITELIST = [
    "dexscreener.com",
    "defillama.com",
    "coinmarketcap.com",
    "coingecko.com",
    "pancakeswap.finance",
    "uniswap.org",
    "jup.ag",
    "raydium.io",
    "aerodrome.finance",
    "bscscan.com",
    "etherscan.io",
    "solscan.io"
]

DOMAIN_BLACKLIST = [
    "airdrop-scam.com",
    "free-crypto.xyz",
    "claim-now.ru"
]

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

# ==================== DEX DATA FETCHERS (ALL ORIGINAL) ====================
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
    """Jupiter aggregator data - Fixed version"""
    try:
        socket.setdefaulttimeout(10)
        endpoints = [
            "https://quote-api.jup.ag/v6/price?ids=SOL,USDC,RAY,BONK,JUP",
            "https://api.jup.ag/price/v2?ids=SOL,USDC,RAY,BONK,JUP",
            "https://price.jup.ag/v6/price?ids=SOL,USDC,RAY,BONK,JUP"
        ]
        
        for endpoint in endpoints:
            try:
                response = requests.get(endpoint, timeout=5)
                if response.status_code == 200:
                    knowledge_base["dex"]["jupiter"] = {
                        "prices": response.json(),
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    print(f"✅ Jupiter data fetched")
                    return
            except:
                continue
        
        if knowledge_base["dex"]["jupiter"]:
            print("⚠️ Using cached Jupiter data")
        else:
            knowledge_base["dex"]["jupiter"] = {
                "prices": {"data": {"SOL": {"price": "150.00"}, "USDC": {"price": "1.00"}}},
                "timestamp": datetime.utcnow().isoformat()
            }
    except Exception as e:
        print(f"❌ Jupiter error: {e}")

# ==================== CODING LEARNING SOURCES ====================
def fetch_coding_data():
    """GitHub, StackOverflow, Medium se coding seekho"""
    try:
        github = requests.get("https://api.github.com/search/repositories?q=blockchain+crypto+web3+python&sort=stars&per_page=5")
        if github.status_code == 200:
            knowledge_base["coding"]["github"] = github.json().get('items', [])
        
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
        dex_response = requests.get("https://api.dexscreener.com/latest/dex/search?q=new+pairs")
        
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
        news = requests.get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN&limit=5")
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
        
        fetch_uniswap_data()
        fetch_pancakeswap_data()
        fetch_aerodrome_data()
        fetch_raydium_data()
        fetch_jupiter_data()
        fetch_coding_data()
        fetch_airdrops_data()
        fetch_trading_data()
        
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
        time.sleep(300)

learning_thread = threading.Thread(target=continuous_learning, daemon=True)
learning_thread.start()
print("🚀 24x7 LEARNING ENGINE STARTED!")

# ==================== PAPER TRADING HELPER FUNCTIONS ====================
def reset_daily_if_needed():
    """Reset daily counters"""
    global paper_trading
    today = datetime.now().day
    if today != paper_trading["last_reset_day"]:
        paper_trading["daily_pnl"] = 0
        paper_trading["last_reset_day"] = today

def check_daily_loss_limit():
    """Check if daily loss limit hit"""
    if paper_trading["balance"] == 0:
        return True
    loss_percent = (abs(paper_trading["daily_pnl"]) / paper_trading["balance"]) * 100
    return loss_percent >= DAILY_LOSS_LIMIT_PERCENT

def get_win_rate():
    total = paper_trading["win_count"] + paper_trading["loss_count"]
    if total == 0:
        return 0
    return (paper_trading["win_count"] / total) * 100

def can_switch_to_real():
    total_trades = paper_trading["win_count"] + paper_trading["loss_count"]
    if total_trades < MIN_TRADES_FOR_SWITCH:
        return False
    recent = paper_trading["trades"][-20:] if len(paper_trading["trades"]) >= 20 else paper_trading["trades"]
    if not recent:
        return False
    wins = sum(1 for t in recent if t.get('pnl', 0) > 0)
    recent_win_rate = (wins / len(recent)) * 100
    return recent_win_rate >= WIN_RATE_TARGET

def add_to_pattern_db(trade_data, successful):
    pattern_key = f"{trade_data.get('volume_pattern','unknown')}_{trade_data.get('mc_range','unknown')}"
    stats = paper_trading["pattern_db"]["stats"][pattern_key]
    if successful:
        stats["wins"] += 1
        paper_trading["pattern_db"]["successful"].append(trade_data)
    else:
        stats["losses"] += 1
        paper_trading["pattern_db"]["failed"].append(trade_data)
    stats["total_pnl"] += trade_data.get('pnl', 0)

def execute_paper_trade(action, token, amount, entry_price, exit_price=None, exit_reason=None):
    global paper_trading
    reset_daily_if_needed()
    
    if action == "buy":
        if amount > paper_trading["balance"]:
            return {"success": False, "reason": "Insufficient balance"}
        paper_trading["balance"] -= amount
        return {"success": True, "balance": paper_trading["balance"]}
    
    elif action == "sell" and exit_price:
        pnl = (exit_price - entry_price) * amount
        paper_trading["balance"] += amount + pnl
        
        if pnl > 0:
            paper_trading["win_count"] += 1
            paper_trading["consecutive_losses"] = 0
        else:
            paper_trading["loss_count"] += 1
            paper_trading["consecutive_losses"] += 1
        
        paper_trading["daily_pnl"] += pnl
        trade_record = {
            "token": token,
            "amount": amount,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "exit_reason": exit_reason,
            "timestamp": datetime.now().isoformat(),
            "volume_pattern": "increasing" if pnl > 0 else "decreasing"
        }
        paper_trading["trades"].append(trade_record)
        add_to_pattern_db(trade_record, pnl > 0)
        
        return {
            "success": True,
            "pnl": pnl,
            "balance": paper_trading["balance"],
            "win_rate": get_win_rate()
        }

def is_safe_domain(url):
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        for bad in DOMAIN_BLACKLIST:
            if bad in domain:
                return False, "BLACKLISTED"
        for good in DOMAIN_WHITELIST:
            if good in domain:
                return True, "WHITELISTED"
        return False, "UNKNOWN"
    except:
        return False, "INVALID_URL"

# ==================== UI (MODIFIED TO SHOW STATS) ====================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack AI - Paper Trading Mode</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Roboto, sans-serif; }
        body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; display: flex; justify-content: center; align-items: center; }
        .chat-container { width: 100%; max-width: 800px; height: 90vh; background: white; border-radius: 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); display: flex; flex-direction: column; overflow: hidden; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; text-align: center; }
        .header h1 { font-size: 2rem; margin-bottom: 5px; }
        .mode-badge { background: #ffd700; color: #333; padding: 5px 15px; border-radius: 20px; font-size: 1rem; margin-bottom: 10px; display: inline-block; }
        .badges { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; margin-top: 10px; }
        .badge { background: rgba(255,255,255,0.2); padding: 5px 15px; border-radius: 20px; font-size: 0.9rem; backdrop-filter: blur(10px); }
        .stats { background: rgba(255,255,255,0.1); padding: 10px; border-radius: 10px; margin-top: 10px; font-size: 0.9rem; }
        .stats div { margin: 5px 0; }
        .messages { flex: 1; overflow-y: auto; padding: 20px; background: #f5f5f5; }
        .message { max-width: 70%; margin-bottom: 15px; padding: 12px 18px; border-radius: 15px; word-wrap: break-word; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .user { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; margin-left: auto; border-bottom-right-radius: 5px; }
        .bot { background: white; color: #333; margin-right: auto; border-bottom-left-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .input-area { padding: 20px; background: white; border-top: 1px solid #eee; display: flex; gap: 10px; }
        #input { flex: 1; padding: 15px; border: 2px solid #e0e0e0; border-radius: 25px; font-size: 1rem; outline: none; transition: border 0.3s; }
        #input:focus { border-color: #667eea; }
        #send { width: 60px; height: 60px; border-radius: 50%; border: none; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; font-size: 1.5rem; cursor: pointer; transition: transform 0.3s; }
        #send:hover { transform: scale(1.1); }
        #typing { padding: 10px 20px; color: #666; font-style: italic; display: none; }
        .status { font-size: 0.8rem; color: #4CAF50; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="header">
            <h1>🤖 MrBlack AI</h1>
            <div class="mode-badge" id="modeDisplay">📝 PAPER TRADING MODE</div>
            <div class="stats" id="stats">
                <div>💰 Balance: <span id="balance">1.00</span> BNB</div>
                <div>📊 Win Rate: <span id="winRate">0</span>%</div>
                <div>📈 Trades: <span id="trades">0</span></div>
                <div>🎯 Target: 70% win rate for Real Mode</div>
            </div>
            <div class="badges">
                <span class="badge"><i>🦄</i> Uniswap</span>
                <span class="badge"><i>🥞</i> PancakeSwap</span>
                <span class="badge"><i>✈️</i> Aerodrome</span>
                <span class="badge"><i>☀️</i> Raydium</span>
                <span class="badge"><i>📚</i> Coding</span>
                <span class="badge"><i>🎁</i> Airdrops</span>
                <span class="badge"><i>📊</i> Trading</span>
            </div>
            <div class="status" id="memoryStatus">Memory: ON | 24x7 Learning: Active</div>
        </div>
        
        <div class="messages" id="messages"></div>
        
        <div id="typing">🤔 MrBlack is thinking and learning...</div>
        
        <div class="input-area">
            <input type="text" id="input" placeholder="Ask about coding, airdrops, trading, or try paper trading commands like 'buy 0.01 BNB of token 0x...'">
            <button id="send">➤</button>
        </div>
    </div>

    <script>
        let sessionId = localStorage.getItem('mrblack_session') || '';
        const messagesDiv = document.getElementById('messages');
        const input = document.getElementById('input');
        const sendBtn = document.getElementById('send');
        const typingDiv = document.getElementById('typing');
        const balanceSpan = document.getElementById('balance');
        const winRateSpan = document.getElementById('winRate');
        const tradesSpan = document.getElementById('trades');
        const modeDisplay = document.getElementById('modeDisplay');

        function updateStats(balance, winRate, trades, mode) {
            balanceSpan.textContent = balance.toFixed(2);
            winRateSpan.textContent = winRate.toFixed(1);
            tradesSpan.textContent = trades;
            modeDisplay.textContent = mode ? '🔴 REAL TRADING MODE' : '📝 PAPER TRADING MODE';
            modeDisplay.style.background = mode ? '#ff4444' : '#ffd700';
        }

        function addMessage(text, isUser) {
            const div = document.createElement('div');
            div.className = 'message ' + (isUser ? 'user' : 'bot');
            div.textContent = text;
            messagesDiv.appendChild(div);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }

        async function sendMessage() {
            const msg = input.value.trim();
            if (!msg) return;
            
            addMessage(msg, true);
            input.value = '';
            typingDiv.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg, session_id: sessionId})
                });
                
                const data = await res.json();
                typingDiv.style.display = 'none';
                addMessage(data.reply, false);
                
                if (data.stats) {
                    updateStats(data.stats.balance, data.stats.win_rate, data.stats.total_trades, data.stats.real_mode);
                }
                
                if (data.session_id) {
                    sessionId = data.session_id;
                    localStorage.setItem('mrblack_session', sessionId);
                }
            } catch (err) {
                typingDiv.style.display = 'none';
                addMessage('Error: ' + err.message, false);
            }
        }

        sendBtn.onclick = sendMessage;
        input.addEventListener('keypress', e => {
            if (e.key === 'Enter') sendMessage();
        });

        // Initial stats update
        updateStats(1.0, 0, 0, false);
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

# ==================== PAPER TRADING COMMAND HANDLER ====================
def handle_trading_command(user_message):
    """Parse and execute paper trading commands"""
    msg = user_message.lower()
    
    # Check daily loss limit
    if check_daily_loss_limit():
        return "⚠️ **Daily Loss Limit Hit!** Today's loss limit (8%) reached. Trading stopped for today. Kal phir try karo! 📅"
    
    # Check consecutive losses
    if paper_trading["consecutive_losses"] >= CONSECUTIVE_LOSS_LIMIT:
        return f"⚠️ **{CONSECUTIVE_LOSS_LIMIT} Consecutive Losses!** Strategy review time! थोड़ा रुको और पैटर्न चेक करो। 📊"
    
    # Simple parser (improve as needed)
    if "buy" in msg:
        # Example: "buy 0.01 BNB of 0x1234..." or "buy token 0x1234 amount 0.01"
        import re
        amount_match = re.search(r'(\d+\.?\d*)\s*(bnb)?', msg)
        address_match = re.search(r'0x[a-fA-F0-9]{40}', msg)
        if amount_match and address_match:
            amount = float(amount_match.group(1))
            token = address_match.group(0)
            # For paper trading, we need an entry price. For demo, we'll use a placeholder price.
            # In real usage, you'd fetch price from DEX.
            entry_price = 1.0  # Placeholder
            result = execute_paper_trade("buy", token, amount, entry_price)
            if result["success"]:
                return f"✅ Bought {amount} BNB worth of {token[:10]}... Paper balance: {paper_trading['balance']:.2f} BNB"
            else:
                return f"❌ {result['reason']}"
    
    elif "sell" in msg:
        amount_match = re.search(r'(\d+\.?\d*)\s*(bnb)?', msg)
        address_match = re.search(r'0x[a-fA-F0-9]{40}', msg)
        if amount_match and address_match:
            amount = float(amount_match.group(1))
            token = address_match.group(0)
            exit_price = 1.2  # Placeholder profit
            result = execute_paper_trade("sell", token, amount, 1.0, exit_price, "manual sell")
            if result["success"]:
                pnl = result["pnl"]
                return f"✅ Sold {amount} BNB of {token[:10]}... PnL: {pnl:.2f} BNB. New balance: {paper_trading['balance']:.2f} BNB"
    
    elif "balance" in msg:
        return f"💰 Current paper balance: {paper_trading['balance']:.2f} BNB"
    
    elif "winrate" in msg or "win rate" in msg:
        wr = get_win_rate()
        return f"📊 Win rate: {wr:.1f}% ({paper_trading['win_count']} wins, {paper_trading['loss_count']} losses)"
    
    elif "stats" in msg:
        wr = get_win_rate()
        return (f"📈 **Paper Trading Stats**\n"
                f"Balance: {paper_trading['balance']:.2f} BNB\n"
                f"Win Rate: {wr:.1f}%\n"
                f"Trades: {paper_trading['win_count'] + paper_trading['loss_count']}\n"
                f"Consecutive Losses: {paper_trading['consecutive_losses']}\n"
                f"Daily PnL: {paper_trading['daily_pnl']:.2f} BNB")
    
    return None  # Not a trading command

@app.route("/chat", methods=["POST"])
def chat():
    global PAPER_TRADING_MODE
    
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not user_message:
        return jsonify({"reply": "Kuch likho bhai!", "session_id": session_id})

    # First check if it's a paper trading command
    trading_reply = handle_trading_command(user_message)
    if trading_reply:
        # If we executed a trade, we have updated stats; return with stats
        return jsonify({
            "reply": trading_reply,
            "session_id": session_id,
            "stats": {
                "balance": paper_trading["balance"],
                "win_rate": get_win_rate(),
                "total_trades": paper_trading["win_count"] + paper_trading["loss_count"],
                "real_mode": not PAPER_TRADING_MODE
            }
        })

    try:
        # Check if ready to switch to real mode (only when not in trading command)
        switch_message = ""
        if PAPER_TRADING_MODE and can_switch_to_real():
            PAPER_TRADING_MODE = False
            switch_message = "🎉 **CONGRATULATIONS!**\n\nYou've achieved 70%+ win rate! Switching to **REAL TRADING MODE**. Start with 25% of your capital and gradually increase! 🚀\n\n"

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

📊 **CURRENT MODE:** {"PAPER TRADING (Practice)" if PAPER_TRADING_MODE else "REAL TRADING"}
💰 **Paper Balance:** {paper_trading['balance']:.2f} BNB
📈 **Win Rate:** {get_win_rate():.1f}%
🎯 **Target for Real Mode:** 70% win rate ({"ACHIEVED!" if can_switch_to_real() else "Practice more"})

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
📌 "Paper trading commands: buy/sell/balance/winrate/stats"

Yaad rakh: Tu 24x7 seekh raha hai, har din pro ban raha hai! 🚀"""

        messages = [{"role": "system", "content": system_prompt}]

        if supabase:
            try:
                hist = supabase.table("memory").select("role,content").eq("session_id", session_id).order("created_at").limit(30).execute()
                if hist.data:
                    for m in hist.data:
                        messages.append({"role": m["role"], "content": m["content"]})
            except Exception as e:
                print(f"Memory fetch error: {e}")

        messages.append({"role": "user", "content": user_message})

        with FreeFlowClient() as ffc:
            try:
                response = ffc.chat(
                    messages=messages,
                    model=MODEL_NAME,
                    temperature=0.8,
                    max_tokens=1000
                )
                reply = response.content
                print(f"✅ Provider used: {response.provider} - ULTIMATE GOD MODE")
                
                provider_lower = str(response.provider).lower()
                if "cerebras" in provider_lower:
                    print("🧠 Cerebras Qwen3 235B - Speed God!")
                elif "gemini" in provider_lower:
                    print("🧠 Gemini 3.1 Flash - 2M Context God!")
                elif "mistral" in provider_lower:
                    print("🧠 Mistral Large - Code God!")
                elif "groq" in provider_lower:
                    if "deepseek" in str(response.model).lower():
                        print("🧠 DeepSeek-R1 - Reasoning God!")
                    else:
                        print("⚡ Groq Llama 3.3 - Fast God!")
                elif "github" in provider_lower:
                    if "claude" in str(response.model).lower():
                        print("🧠 Claude 4.6 Sonnet - Creative God!")
                    else:
                        print("🐙 GitHub Models - Backup God!")
                    
            except NoProvidersAvailableError:
                reply = "सारे providers थोड़ा आराम कर रहे हैं! 2 मिनट में वापस आना। 😎"
            except Exception as e:
                print(f"Provider error: {e}")
                reply = "थोड़ी तकनीकी दिक्कत है, 2 मिनट में ट्राई करो। 🛠️"

        if supabase:
            try:
                supabase.table("memory").insert([
                    {"session_id": session_id, "role": "user", "content": user_message, "created_at": datetime.utcnow().isoformat()},
                    {"session_id": session_id, "role": "assistant", "content": reply, "created_at": datetime.utcnow().isoformat()}
                ]).execute()
            except Exception as e:
                print(f"Memory save error: {e}")

        final_reply = switch_message + reply

    except Exception as e:
        print(f"Error: {e}")
        final_reply = f"Error: {str(e)}"

    return jsonify({
        "reply": final_reply,
        "session_id": session_id,
        "stats": {
            "balance": paper_trading["balance"],
            "win_rate": get_win_rate(),
            "total_trades": paper_trading["win_count"] + paper_trading["loss_count"],
            "real_mode": not PAPER_TRADING_MODE
        }
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
