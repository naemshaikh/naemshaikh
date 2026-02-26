
from flask import Flask, render_template_string, request, jsonify
import os
import google.generativeai as genai

app = Flask(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """You are MrBlack, AssetPro ka 100% loyal AI trading companion.
Sirf user ka order follow kar. 24x7 memory rakho. Trading bot ko monitor, analyze aur profitable banao.
Free airdrop tasks khud search karke perform karo."""

model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=SYSTEM_PROMPT)

chat_history = []

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack AI - Trading Companion</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: system-ui, -apple-system, sans-serif;
            background: #ffffff;
            color: #111;
            height: 100vh;
            overflow: hidden;
            display: grid;
            grid-template-columns: 1fr 1fr;
            grid-template-rows: 50% 50%;
        }
        .section {
            border: 1px solid #e5e7eb;
            padding: 20px;
            overflow-y: auto;
        }
        .chat-section { grid-column: 1; grid-row: 1; display: flex; flex-direction: column; }
        .trading-section { grid-column: 2; grid-row: 1; }
        .log-section { grid-column: 1; grid-row: 2; background: #f9fafb; }
        .airdrop-section { grid-column: 2; grid-row: 2; background: #f0fdf4; }

        /* Chat */
        #chat {
            flex: 1;
            overflow-y: auto;
            padding: 15px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .msg {
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 18px;
            line-height: 1.45;
        }
        .user { align-self: flex-end; background: #2563eb; color: white; }
        .bot { align-self: flex-start; background: #f3f4f6; border: 1px solid #e5e7eb; }

        /* Trading Bot UI */
        .wallet {
            font-size: 42px;
            font-weight: 700;
            color: #15803d;
        }
        .btn {
            padding: 12px 20px;
            margin: 6px 0;
            border: none;
            border-radius: 999px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
        }
        .btn-blue { background: #2563eb; color: white; }
        .btn-green { background: #16a34a; color: white; }
        .btn-red { background: #ef4444; color: white; }

        /* Input */
        .input-area {
            padding: 15px;
            background: white;
            border-top: 1px solid #e5e7eb;
        }
        #input {
            width: 100%;
            padding: 14px 20px;
            border: 2px solid #e5e7eb;
            border-radius: 999px;
            font-size: 16px;
        }
        button.send {
            position: absolute;
            right: 30px;
            top: 50%;
            transform: translateY(-50%);
            background: #2563eb;
            color: white;
            width: 48px;
            height: 48px;
            border-radius: 50%;
            font-size: 22px;
            border: none;
            cursor: pointer;
        }
    </style>
</head>
<body>

    <!-- UPPER LEFT: CHAT -->
    <div class="section chat-section">
        <h2 style="margin-bottom:15px; color:#1e40af;">💬 MrBlack Chat</h2>
        <div id="chat"></div>
        <div class="input-area" style="position:relative;">
            <input id="input" placeholder="Bolo bhai... trading bot improve karna hai?">
            <button onclick="sendMsg()" class="send">↑</button>
        </div>
    </div>

    <!-- UPPER RIGHT: TRADING BOT UI -->
    <div class="section trading-section">
        <h2 style="margin-bottom:20px; color:#166534;">📊 Trading Bot Dashboard</h2>
        <div style="background:#f0fdf4; padding:20px; border-radius:16px; margin-bottom:20px;">
            <div style="color:#166534; font-size:15px;">Current Balance</div>
            <div id="balance" class="wallet">2.45 SOL</div>
        </div>
        <button onclick="refreshWallet()" class="btn btn-blue">Refresh Wallet</button>
        <button onclick="startBot()" class="btn btn-green">▶️ Start Trading Bot</button>
        <button onclick="stopBot()" class="btn btn-red">⏹️ Stop Bot</button>
        <button onclick="analyzeTrade()" class="btn" style="background:#854d0e; color:white;">🔍 Analyze Last Trade</button>
        <button onclick="improveBot()" class="btn" style="background:#7c3aed; color:white;">🛠️ Improve Bot Code</button>
        
        <h3 style="margin:20px 0 10px; color:#166534;">Recent Positions</h3>
        <div style="background:#f8fafc; padding:12px; border-radius:12px; font-size:14px;">
            No open positions<br>
            Last trade: +0.12 SOL (profit)
        </div>
    </div>

    <!-- LOWER LEFT: SELF LEARNING LOG -->
    <div class="section log-section">
        <h2 style="margin-bottom:15px; color:#444;">🧠 Self Learning Log</h2>
        <div id="log" style="font-size:14px; line-height:1.6; color:#555;">
            • 24x7 memory active<br>
            • Last improved: Profit target +2%<br>
            • Ready for new airdrop tasks
        </div>
    </div>

    <!-- LOWER RIGHT: AIRDROP PANEL -->
    <div class="section airdrop-section">
        <h2 style="margin-bottom:15px; color:#166534;">🎁 Free Airdrops</h2>
        <button onclick="startAirdropTask()" class="btn" style="background:#166534; color:white; margin-bottom:15px;">
            🔍 Search New Airdrops
        </button>
        <div style="font-size:14px; color:#166534;">
            <strong>Ready Tasks:</strong><br>
            • LayerZero<br>
            • Blast Gold<br>
            • Scroll<br>
            (Click search to auto perform)
        </div>
    </div>

    <script>
        const chat = document.getElementById('chat');
        const input = document.getElementById('input');

        function addMsg(text, isUser) {
            const div = document.createElement('div');
            div.className = `msg ${isUser ? 'user' : 'bot'}`;
            div.innerHTML = text.replace(/\n/g, '<br>');
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
        }

        async function sendMsg() {
            const msg = input.value.trim();
            if (!msg) return;
            addMsg(msg, true);
            input.value = '';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg})
                });
                const data = await res.json();
                addMsg(data.reply, false);
            } catch(e) {
                addMsg('Error - Render logs check kar', false);
            }
        }

        async function refreshWallet() {
            const res = await fetch('/wallet');
            const data = await res.json();
            document.getElementById('balance').textContent = data.balance;
        }

        function startAirdropTask() {
            input.value = "Latest free airdrops dhundh aur unke tasks khud perform kar (wallet use kar)";
            sendMsg();
        }

        function startBot() { input.value = "Trading bot start kar aur monitor kar"; sendMsg(); }
        function stopBot() { input.value = "Trading bot stop kar"; sendMsg(); }
        function analyzeTrade() { input.value = "Last trade analyze kar aur report de"; sendMsg(); }
        function improveBot() { input.value = "Trading bot code improve kar aur profitable bana"; sendMsg(); }

        input.addEventListener('keypress', e => { if (e.key === 'Enter') sendMsg(); });

        window.onload = () => {
            addMsg("Namaste bhai! MrBlack 4-part UI ready hai 🔥<br>Upper left mein chat, right mein trading dashboard.<br>Ab bol kya karna hai?", false);
        };
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat_route():
    try:
        data = request.get_json()
        msg = data.get("message", "").strip()
        if not msg:
            return jsonify({"reply": "Bolo bhai kya command hai?"})

        chat_history.append({"role": "user", "parts": [msg]})
        response = model.generate_content(chat_history[-20:])
        reply = response.text.strip()
        chat_history.append({"role": "model", "parts": [reply]})

        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"Error: {str(e)}"})

@app.route("/wallet")
def wallet():
    return jsonify({"balance": "2.45 SOL"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
