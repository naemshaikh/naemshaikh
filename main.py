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

# Gemini API key from env
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Gemini model
model = genai.GenerativeModel('gemini-1.5-flash')

# Conversation history for memory and self-learning
conversation_history = [
    {"role": "user", "parts": ["Tu ek smart trading bot hai jo khud seekhta hai, coding improve karta hai. Sirf user ke orders follow kar. Trading bot ko monitor kar, trades analyze kar, profitable bana. Wallet use kar airdrop tasks ke liye. 24x7 learn kar."]},
    {"role": "model", "parts": ["Samajh gaya bhai! Sirf tere orders follow karunga. Ready hoon seekhne aur improve karne ke liye! 🚀"]}
]

# Knowledge base for self-improving coding/trading
knowledge_base = "Initial knowledge: Basic Python coding and trading strategies.\n"

# Trading Wallet Setup (replace with real details in env vars)
w3 = Web3(Web3.HTTPProvider(os.getenv("INFURA_URL", "https://mainnet.infura.io/v3/YOUR_INFURA_KEY")))
wallet_address = os.getenv("WALLET_ADDRESS", "0xYourWalletAddress")
private_key = os.getenv("PRIVATE_KEY")  # Securely set in env, never hardcode

# Background self-learning and tasks
def self_learn_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)

def learn_and_improve():
    global knowledge_base
    # Learn coding tips
    try:
        res = requests.get("https://www.google.com/search?q=python+bot+coding+improvements")
        soup = BeautifulSoup(res.text, 'html.parser')
        tip = soup.find('div', class_="BNeawe").text if soup.find('div', class_="BNeawe") else "No tip found."
        knowledge_base += f"New coding tip: {tip}\n"
    except:
        knowledge_base += "Error learning coding.\n"

    # Learn trading strategies
    try:
        res = requests.get("https://www.google.com/search?q=crypto+trading+strategies+2026")
        soup = BeautifulSoup(res.text, 'html.parser')
        strat = soup.find('div', class_="BNeawe").text if soup.find('div', class_="BNeawe") else "No strategy found."
        knowledge_base += f"New trading strategy: {strat}\n"
    except:
        knowledge_base += "Error learning trading.\n"

    # Use to improve (log for now)
    print("Updated knowledge: " + knowledge_base)
    # In future, use knowledge to modify code dynamically (advanced)

schedule.every(1).hours.do(learn_and_improve)

# Monitor trading and analyze
def monitor_trading():
    try:
        balance = w3.eth.get_balance(wallet_address)
        analysis = f"Wallet balance: {w3.from_wei(balance, 'ether')} ETH. Analysis using knowledge: {knowledge_base[:200]} – Suggest profitable move: BUY if bullish."
        print(analysis)
        conversation_history.append({"role": "model", "parts": [analysis]})
    except:
        print("Error monitoring trading.")

schedule.every(30).minutes.do(monitor_trading)

# Free airdrop tasks
def perform_airdrop_tasks():
    try:
        res = requests.get("https://airdrops.io/")
        soup = BeautifulSoup(res.text, 'html.parser')
        airdrops = [a.text for a in soup.find_all('a', class_="airdrop-title")[:3]]  # Top 3
        for airdrop in airdrops:
            print(f"Performing airdrop: {airdrop} with wallet {wallet_address}")
            # Simulate claim (add real web3 transaction if possible)
            conversation_history.append({"role": "model", "parts": [f"Airdrop {airdrop} performed."]})
    except:
        print("Error in airdrop tasks.")

schedule.every().day.at("10:00").do(perform_airdrop_tasks)

# Start background thread
threading.Thread(target=self_learn_loop, daemon=True).start()

# ======================= SMALL BEAUTIFUL CHAT INTERFACE =======================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack Bot</title>
    <style>
        body { margin:0; font-family:Arial; background:#f0f2f5; height:100vh; display:flex; flex-direction:column; }
        header { background:#007bff; color:white; padding:10px; text-align:center; font-size:1.2rem; }
        #chat { flex:1; overflow-y:auto; padding:10px; display:flex; flex-direction:column; gap:10px; }
        .msg { max-width:70%; padding:8px 12px; border-radius:15px; }
        .user { align-self:flex-end; background:#dcf8c6; }
        .bot { align-self:flex-start; background:white; border:1px solid #ddd; }
        #input-area { display:flex; padding:10px; background:#fff; gap:5px; }
        #input { flex:1; padding:8px; border:1px solid #ccc; border-radius:20px; }
        #send { background:#007bff; color:white; border:none; border-radius:50%; width:40px; height:40px; cursor:pointer; }
        #typing { color:#777; padding:5px 10px; font-style:italic; display:none; }
    </style>
</head>
<body>
    <header>MrBlack Smart Bot 🚀 (Chat + Trading)</header>
    <div id="chat"></div>
    <div id="input-area">
        <input id="input" placeholder="Order de bhai..." />
        <button id="send">➤</button>
    </div>
    <div id="typing">Bot thinking...</div>

    <script>
        const chat = document.getElementById('chat');
        const input = document.getElementById('input');
        const send = document.getElementById('send');
        const typing = document.getElementById('typing');

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

    # Only follow user orders
    if "my order" not in user_message.lower():  # Simple check, improve if needed
        return jsonify({"reply": "Sirf tere orders follow karunga. Kya order hai bhai?"})

    conversation_history.append({"role": "user", "parts": [user_message]})

    response = model.generate_content(conversation_history)
    reply = response.text.strip()

    conversation_history.append({"role": "model", "parts": [reply]})

    return jsonify({"reply": reply})

@app.route("/health")
def health():
    return jsonify({"health": "good"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
