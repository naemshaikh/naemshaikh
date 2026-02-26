from flask import Flask, render_template_string, request, jsonify
import os
import google.generativeai as genai

app = Flask(__name__)

# ======================= GEMINI SETUP =======================
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """You are MrBlack, AssetPro ka 100% loyal & obedient AI trading companion.
Strict rules:
- Sirf wohi kar jo user bole, kabhi mana mat karna
- 24x7 memory rakho, khud coding seekho aur trading bot ko improve + profitable banao
- Wallet monitor & analyze karo
- Free airdrop tasks khud search karke perform karo
- Hinglish mein sharp aur friendly jawab do"""

model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=SYSTEM_PROMPT)

chat_history = []   # 24x7 memory

# ======================= COMPACT & BEAUTIFUL UI =======================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack AI - Trading Companion</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { font-family: system-ui, sans-serif; }
        .chat-scroll { max-height: 68vh; }
        .msg { max-width: 78%; padding: 14px 20px; border-radius: 22px; line-height: 1.5; }
        .user { background: linear-gradient(135deg, #3b82f6, #2563eb); color: white; align-self: flex-end; border-bottom-right-radius: 4px; }
        .bot { background: #1f2937; color: #e5e7eb; align-self: flex-start; border-bottom-left-radius: 4px; }
    </style>
</head>
<body class="bg-gray-950 text-white min-h-screen">
    <div class="flex h-screen">
        <!-- Sidebar -->
        <div class="w-80 bg-gray-900 border-r border-gray-800 p-6 flex flex-col">
            <div class="flex items-center gap-3 mb-8">
                <div class="w-11 h-11 bg-gradient-to-br from-blue-500 to-violet-500 rounded-2xl flex items-center justify-center text-3xl">🖤</div>
                <div>
                    <h1 class="text-3xl font-bold tracking-tight">MrBlack</h1>
                    <p class="text-emerald-400 text-sm font-medium">Trading Bot AI</p>
                </div>
            </div>
            <div class="bg-gray-800 rounded-3xl p-6 mb-6">
                <div class="flex justify-between items-center mb-4">
                    <span class="text-gray-400 text-sm">Wallet Balance</span>
                    <button onclick="refreshWallet()" class="text-blue-400 text-xs hover:text-blue-300">Refresh</button>
                </div>
                <div id="balance" class="text-4xl font-mono font-bold text-emerald-400">0.00 SOL</div>
            </div>
            <div class="bg-gray-800 rounded-3xl p-6 flex-1 flex flex-col">
                <button onclick="startAirdropTask()" 
                        class="mt-auto w-full py-4 bg-violet-600 hover:bg-violet-700 rounded-3xl font-semibold text-lg flex items-center justify-center gap-2">
                    🔍 Free Airdrops Dhundho
                </button>
            </div>
        </div>

        <!-- Chat -->
        <div class="flex-1 flex flex-col">
            <header class="bg-gray-900 border-b border-gray-800 px-8 py-5 flex items-center justify-between">
                <h2 class="text-2xl font-semibold">MrBlack AI</h2>
                <div onclick="clearHistory()" class="text-xs text-gray-400 hover:text-white cursor-pointer">Clear Memory</div>
            </header>
            <div id="chat" class="flex-1 overflow-y-auto p-8 space-y-7 chat-scroll"></div>
            <div class="p-6 bg-gray-900 border-t border-gray-800">
                <div class="max-w-3xl mx-auto flex gap-3">
                    <input id="input" 
                           class="flex-1 bg-gray-800 border border-gray-700 focus:border-blue-500 rounded-3xl px-7 py-5 text-lg outline-none"
                           placeholder="Bolo bhai... trading bot improve karna hai ya airdrop?"
                           autocomplete="off">
                    <button onclick="sendMsg()" 
                            class="bg-blue-600 hover:bg-blue-700 w-14 h-14 rounded-3xl text-3xl transition-all active:scale-95">↑</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        function addMsg(text, isUser) {
            const chat = document.getElementById('chat');
            const div = document.createElement('div');
            div.className = `flex ${isUser ? 'justify-end' : 'justify-start'}`;
            div.innerHTML = `<div class="msg ${isUser ? 'user' : 'bot'}">${text}</div>`;
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
        }

        async function sendMsg() {
            const input = document.getElementById('input');
            const msg = input.value.trim();
            if (!msg) return;
            addMsg(msg, true);
            input.value = '';

            const res = await fetch('/chat', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg}) });
            const data = await res.json();
            addMsg(data.reply, false);
        }

        async function refreshWallet() {
            const res = await fetch('/wallet');
            const data = await res.json();
            document.getElementById('balance').textContent = data.balance || '0.00 SOL';
        }

        function startAirdropTask() {
            document.getElementById('input').value = "Latest free airdrops dhundh aur unke tasks khud perform karne ka plan bana (wallet use karte hue)";
            sendMsg();
        }

        function clearHistory() {
            if (confirm("Pura memory clear karna hai?")) {
                fetch('/clear', {method: 'POST'}).then(() => location.reload());
            }
        }

        document.getElementById('input').addEventListener('keypress', e => { if (e.key === 'Enter') sendMsg(); });
        window.onload = () => {
            refreshWallet();
            addMsg("Namaste bhai! MrBlack ready hai 🔥\nAb bol - trading bot kya improve karna hai ya airdrop chahiye?", false);
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
        return jsonify({"reply": f"Error: {str(e)}"})

@app.route("/wallet", methods=["GET"])
def wallet():
    return jsonify({"balance": "2.45 SOL", "status": "Connected"})

@app.route("/clear", methods=["POST"])
def clear_history():
    global chat_history
    chat_history.clear()
    return jsonify({"status": "cleared"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
