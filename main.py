from flask import Flask, render_template_string, request, jsonify
import os
import google.generativeai as genai

app = Flask(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """You are MrBlack, AssetPro ka 100% loyal & obedient AI trading companion.
Strict rules:
- Sirf wohi kar jo user bole, kabhi mana mat karna
- 24x7 memory rakho, khud coding seekho aur trading bot ko improve + profitable banao
- Wallet monitor & analyze karo
- Free airdrop tasks khud search karke perform karo
- Hinglish mein sharp aur friendly jawab do"""

model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=SYSTEM_PROMPT)

chat_history = []

# ======================= PURE CSS BEAUTIFUL COMPACT UI =======================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack AI - Trading Companion</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: #0a0a0a;
            color: #e5e7eb;
            height: 100vh;
            overflow: hidden;
            display: flex;
        }
        
        .sidebar {
            width: 320px;
            background: #111827;
            border-right: 1px solid #374151;
            padding: 24px;
            display: flex;
            flex-direction: column;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 32px;
        }
        
        .logo-circle {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            border-radius: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
        }
        
        .wallet-box {
            background: #1f2937;
            border-radius: 20px;
            padding: 20px;
            margin-bottom: 24px;
        }
        
        .chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
        }
        
        header {
            background: #111827;
            border-bottom: 1px solid #374151;
            padding: 20px 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        #chat {
            flex: 1;
            padding: 28px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 18px;
            background: #0a0a0a;
        }
        
        .msg {
            max-width: 75%;
            padding: 14px 20px;
            border-radius: 22px;
            line-height: 1.5;
            font-size: 15.5px;
        }
        
        .user {
            align-self: flex-end;
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white;
            border-bottom-right-radius: 6px;
        }
        
        .bot {
            align-self: flex-start;
            background: #1f2937;
            border: 1px solid #374151;
            border-bottom-left-radius: 6px;
        }
        
        .input-area {
            padding: 20px 28px;
            background: #111827;
            border-top: 1px solid #374151;
        }
        
        #input {
            width: 100%;
            background: #1f2937;
            border: 1px solid #4b5563;
            color: white;
            padding: 16px 24px;
            border-radius: 9999px;
            font-size: 16px;
            outline: none;
        }
        
        #input:focus {
            border-color: #3b82f6;
        }
        
        .send-btn {
            width: 52px;
            height: 52px;
            background: #3b82f6;
            color: white;
            border: none;
            border-radius: 9999px;
            font-size: 24px;
            cursor: pointer;
            margin-left: 12px;
        }
        
        .typing {
            color: #9ca3af;
            font-style: italic;
            padding: 10px 20px;
        }
    </style>
</head>
<body>
    <!-- Sidebar -->
    <div class="sidebar">
        <div class="logo">
            <div class="logo-circle">🖤</div>
            <div>
                <h1 style="font-size:28px; font-weight:700;">MrBlack</h1>
                <p style="color:#34d399; font-size:14px;">Trading Bot AI</p>
            </div>
        </div>
        
        <div class="wallet-box">
            <div style="color:#9ca3af; font-size:14px; margin-bottom:8px;">Wallet Balance</div>
            <div id="balance" style="font-size:32px; font-weight:700; color:#34d399;">2.45 SOL</div>
            <button onclick="refreshWallet()" style="margin-top:12px; color:#60a5fa; font-size:13px; background:none; border:none; cursor:pointer;">Refresh Balance</button>
        </div>
        
        <button onclick="startAirdropTask()" 
                style="margin-top:auto; width:100%; padding:18px; background:#7c3aed; color:white; border:none; border-radius:9999px; font-size:17px; font-weight:600; cursor:pointer;">
            🔍 Free Airdrops Dhundho
        </button>
    </div>

    <!-- Chat Area -->
    <div class="chat-area">
        <header>
            <h2 style="font-size:24px; font-weight:600;">MrBlack AI</h2>
            <span onclick="clearHistory()" style="color:#9ca3af; cursor:pointer; font-size:14px;">Clear Memory</span>
        </header>
        
        <div id="chat"></div>
        
        <div class="input-area">
            <div style="display:flex; align-items:center;">
                <input id="input" placeholder="Bolo bhai... trading bot improve karna hai ya airdrop?">
                <button onclick="sendMsg()" class="send-btn">↑</button>
            </div>
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
                addMsg(data.reply || 'Sorry bhai, error aa gaya', false);
            } catch(e) {
                addMsg('Network error - Render logs check kar', false);
            }
        }

        async function refreshWallet() {
            try {
                const res = await fetch('/wallet');
                const data = await res.json();
                document.getElementById('balance').textContent = data.balance || '0.00 SOL';
            } catch(e) {}
        }

        function startAirdropTask() {
            input.value = "Latest free airdrops dhundh aur unke tasks khud perform karne ka plan bana (wallet use karte hue)";
            sendMsg();
        }

        function clearHistory() {
            if (confirm("Pura memory clear karna hai?")) {
                fetch('/clear', {method: 'POST'}).then(() => location.reload());
            }
        }

        input.addEventListener('keypress', e => { if (e.key === 'Enter') sendMsg(); });
        
        window.onload = () => {
            refreshWallet();
            addMsg("Namaste bhai! MrBlack ab bilkul ready hai 🔥<br>Ab bol - kya karna hai trading bot mein?", false);
        };
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        user_msg = data.get("message", "").strip()
        if not user_msg:
            return jsonify({"reply": "Bolo bhai kya command hai?"})

        chat_history.append({"role": "user", "parts": [user_msg]})
        response = model.generate_content(chat_history[-20:])
        reply = response.text.strip()
        chat_history.append({"role": "model", "parts": [reply]})

        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"Error: {str(e)} - Render logs check kar"})

@app.route("/wallet")
def wallet():
    return jsonify({"balance": "2.45 SOL"})

@app.route("/clear", methods=["POST"])
def clear_history():
    global chat_history
    chat_history.clear()
    return jsonify({"status": "cleared"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
