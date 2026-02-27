from flask import Flask, render_template_string, request, jsonify
import os
import google.generativeai as genai
from web3 import Web3
import threading
import schedule
import time
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# Gemini setup
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# Memory (conversation history)
conversation_history = []

# Self-learning knowledge base
knowledge_base = "Initial knowledge: Basic trading and coding.\n"

# Wallet setup
w3 = Web3(Web3.HTTPProvider(os.getenv("INFURA_URL")))
wallet_address = os.getenv("WALLET_ADDRESS")
private_key = os.getenv("PRIVATE_KEY")

# Background tasks
def background_tasks():
    while True:
        schedule.run_pending()
        time.sleep(1)

def learn_and_improve():
    global knowledge_base
    # Learn coding
    try:
        res = requests.get("https://www.google.com/search?q=advanced+python+bot+coding")
        soup = BeautifulSoup(res.text, 'html.parser')
        tip = soup.find('div', class_="BNeawe").text if soup.find('div', class_="BNeawe") else "No tip."
        knowledge_base += f"Coding tip: {tip}\n"
    except:
        pass

    # Learn trading
    try:
        res = requests.get("https://www.google.com/search?q=profitable+crypto+strategies+2026")
        soup = BeautifulSoup(res.text, 'html.parser')
        strat = soup.find('div', class_="BNeawe").text if soup.find('div', class_="BNeawe") else "No strategy."
        knowledge_base += f"Trading strategy: {strat}\n"
    except:
        pass

schedule.every(1).hours.do(learn_and_improve)

def monitor_and_analyze_trades():
    balance = w3.eth.get_balance(wallet_address)
    analysis = f"Balance: {w3.from_wei(balance, 'ether')} ETH. Using knowledge: {knowledge_base[:100]}. Suggestion: BUY if bullish."
    conversation_history.append({"role": "model", "parts": [analysis]})
    # Improve profitability (dummy - add advanced logic from your Colab)
    print("Trades analyzed and optimized.")

schedule.every(30).minutes.do(monitor_and_analyze_trades)

def airdrop_tasks():
    try:
        res = requests.get("https://airdrops.io/")
        soup = BeautifulSoup(res.text, 'html.parser')
        airdrops = [a.text for a in soup.find_all('a', class_="airdrop-title")[:3]]
        for airdrop in airdrops:
            print(f"Performing airdrop: {airdrop}")
            # Dummy claim - add real transaction from your advanced Colab code
            conversation_history.append({"role": "model", "parts": [f"Airdrop {airdrop} done."]})
    except:
        pass

schedule.every().day.at("12:00").do(airdrop_tasks)

# Start background
threading.Thread(target=background_tasks, daemon=True).start()

# ======================= SMALL BEAUTIFUL CHAT + TRADING UI =======================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack Bot</title>
    <style>
        body { margin:0; font-family:Arial; background:#f0f2f5; height:100vh; display:flex; }
        #chat-section { width:50%; border-right:1px solid #ddd; display:flex; flex-direction:column; }
        #trading-section { width:50%; padding:15px; }
        header { background:#007bff; color:white; padding:8px; text-align:center; font-size:1rem; }
        #chat { flex:1; overflow-y:auto; padding:10px; display:flex; flex-direction:column; gap:8px; }
        .msg { max-width:70%; padding:8px 12px; border-radius:15px; font-size:0.95rem; }
        .user { align-self:flex-end; background:#dcf8c6; }
        .bot { align-self:flex-start; background:white; border:1px solid #ddd; }
        #input-area { display:flex; padding:10px; background:#fff; gap:5px; }
        #input { flex:1; padding:8px; border:1px solid #ccc; border-radius:20px; font-size:0.95rem; }
        #send { background:#007bff; color:white; border:none; border-radius:50%; width:40px; height:40px; cursor:pointer; }
        #typing { color:#777; padding:5px 10px; font-style:italic; display:none; font-size:0.9rem; }
        #trading-info { margin-top:10px; background:#fff; padding:10px; border-radius:10px; box-shadow:0 2px 5px rgba(0,0,0,0.1); font-size:0.95rem; }
    </style>
</head>
<body>
    <div id="chat-section">
        <header>Chat Bot</header>
        <div id="chat"></div>
        <div id="input-area">
            <input id="input" placeholder="Order de bhai..." />
            <button id="send">➤</button>
        </div>
        <div id="typing">Bot thinking...</div>
    </div>
    <div id="trading-section">
        <header>Trading Bot</header>
        <button onclick="getSignal()">Get Signal</button>
        <div id="trading-info">Trading signals and analysis here.</div>
    </div>

    <script>
        const chat = document.getElementById('chat');
        const input = document.getElementById('input');
        const send = document.getElementById('send');
        const typing = document.getElementById('typing');
        const tradingInfo = document.getElementById('trading-info');

        function addMessage(text, isUser = false) {
            const div = document.createElement('div');
            div.className = 'msg ' + (isUser ? 'user' : 'bot');
            div.textContent = text;
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
        }

        async function sendMessage() {
            const message = input.value.trim();
            if (!message) return;

            addMessage(message, true);
            input.value = '';
            typing.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message})
                });
                const data = await res.json();
                typing.style.display = 'none';
                addMessage(data.reply);
            } catch (err) {
                typing.style.display = 'none';
                addMessage('Error: ' + err.message);
            }
        }

        async function getSignal() {
            tradingInfo.textContent = 'Fetching...';
            try {
                const res = await fetch('/bot');
                const data = await res.json();
                tradingInfo.textContent = `Symbol: ${data.signal.symbol}\nAction: ${data.signal.action}\nReason: ${data.signal.reason}`;
            } catch (err) {
                tradingInfo.textContent = 'Error: ' + err.message;
            }
        }

        send.onclick = sendMessage;
        input.addEventListener('keypress', e => { if (e.key === 'Enter') sendMessage(); });
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"reply": "Kuch to bol bhai... 😅"})

    # Orders follow (simple check)
    if "order" not in user_message.lower():
        return jsonify({"reply": "Sirf orders follow karunga. Kya order hai?"})

    conversation_history.append({"role": "user", "parts": [user_message]})

    response = model.generate_content(conversation_history)
    reply = response.text.strip()

    conversation_history.append({"role": "model", "parts": [reply]})

    return jsonify({"reply": reply})

@app.route("/bot", methods=["GET"])
def trading_bot():
    balance = w3.eth.get_balance(wallet_address)
    signal = {
        "symbol": "BTCUSDT",
        "action": "BUY" if "bullish" in knowledge_base else "SELL",
        "reason": "Based on learned knowledge: " + knowledge_base[:100]
    }
    return jsonify({"status": "success", "signal": signal})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
