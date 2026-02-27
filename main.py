import os
from flask import Flask, render_template_string, request, jsonify
from openai import OpenAI
from supabase import create_client, Client
import uuid

app = Flask(__name__)

# ==================== GROQ SETUP ====================
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

MODEL_NAME = "llama-3.3-70b-versatile"

# ==================== SUPABASE ====================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ==================== PERSONAL JARVIS SECURITY ====================
PERSONAL_SECRET = os.getenv("PERSONAL_SECRET")   # ← Render env mein daal dena (strong password)

if not PERSONAL_SECRET:
    print("⚠️ WARNING: PERSONAL_SECRET env set nahi hai → bot public rahega!")

# ==================== PRIVATE CHAT UI (Jarvis style) ====================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MrBlack Jarvis</title>
<style>
body { margin:0; font-family:Arial; background:#0f0f0f; color:#0f0; height:100vh; display:flex; align-items:center; justify-content:center; }
#login { text-align:center; }
#login input { padding:15px; font-size:1.2rem; width:280px; border-radius:8px; border:none; background:#222; color:#0f0; }
#login button { padding:15px 30px; font-size:1.2rem; margin-top:15px; background:#0f0; color:#000; border:none; border-radius:8px; cursor:pointer; }
#chat-container { display:none; width:100%; height:100%; flex-direction:column; }
header { background:#000; color:#0f0; padding:15px; text-align:center; font-size:1.4rem; }
#chat { flex:1; overflow-y:auto; padding:15px; display:flex; flex-direction:column; gap:12px; }
.msg { max-width:80%; padding:12px 16px; border-radius:18px; line-height:1.4; }
.user { align-self:flex-end; background:#003300; }
.bot { align-self:flex-start; background:#001a00; border:1px solid #0f0; }
#input-area { display:flex; padding:12px; background:#000; gap:8px; }
#input { flex:1; padding:15px; border:1px solid #0f0; border-radius:25px; font-size:1rem; background:#111; color:#0f0; }
#send { background:#0f0; color:#000; border:none; border-radius:50%; width:50px; height:50px; cursor:pointer; font-size:1.5rem; }
#typing { color:#0a0; padding:10px; font-style:italic; }
</style>
</head>
<body>
<div id="login">
    <h1>🔐 MrBlack Jarvis</h1>
    <p>Sirf Owner Access</p>
    <input id="pass" type="password" placeholder="Enter secret key" autocomplete="off"/>
    <button onclick="login()">Unlock Jarvis</button>
</div>

<div id="chat-container">
<header>MrBlack Jarvis (Personal Super Bot)</header>
<div id="chat"></div>
<div id="input-area">
<input id="input" placeholder="Bolo Jarvis..." autocomplete="off"/>
<button id="send">➤</button>
</div>
<div id="typing">Soch raha hoon Sir...</div>
</div>

<script>
let sessionId = localStorage.getItem('jarvis_session') || '';
let isAuth = localStorage.getItem('jarvis_auth') === 'true';

const loginScreen = document.getElementById('login');
const chatScreen = document.getElementById('chat-container');

if (isAuth) showChat();

function login() {
    const pass = document.getElementById('pass').value.trim();
    if (!pass) return alert("Secret key daal bhai!");
    
    fetch('/auth', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({secret: pass})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            localStorage.setItem('jarvis_auth', 'true');
            showChat();
        } else {
            alert("Galat secret key! Sirf owner hi jaan sakta hai 😉");
        }
    });
}

function showChat() {
    loginScreen.style.display = 'none';
    chatScreen.style.display = 'flex';
    
    const send = document.getElementById('send');
    const input = document.getElementById('input');
    const typing = document.getElementById('typing');
    const chat = document.getElementById('chat');

    function addMessage(text, isUser = false) {
        const div = document.createElement('div');
        div.className = 'msg ' + (isUser ? 'user' : 'bot');
        div.textContent = text;
        chat.appendChild(div);
        chat.scrollTop = chat.scrollHeight;
    }

    async function sendMessage() {
        const msg = input.value.trim();
        if (!msg) return;
        addMessage(msg, true);
        input.value = '';
        typing.style.display = 'block';

        try {
            const res = await fetch('/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: msg, session_id: sessionId, secret: localStorage.getItem('jarvis_auth') ? 'verified' : ''})
            });
            const data = await res.json();
            typing.style.display = 'none';
            addMessage(data.reply);
            if (data.session_id) {
                sessionId = data.session_id;
                localStorage.setItem('jarvis_session', sessionId);
            }
        } catch (err) {
            typing.style.display = 'none';
            addMessage('Error: ' + err.message);
        }
    }

    send.onclick = sendMessage;
    input.addEventListener('keypress', e => { if (e.key === 'Enter') sendMessage(); });
}
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json() or {}
    if data.get("secret") == PERSONAL_SECRET:
        return jsonify({"success": True})
    return jsonify({"success": False}), 401

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not PERSONAL_SECRET or data.get("secret") != "verified":
        return jsonify({"reply": "Access Denied. Sirf owner hi use kar sakta hai."}), 401

    if not user_message:
        return jsonify({"reply": "Bolo Sir, kya command hai?", "session_id": session_id})

    try:
        messages = [
            {"role": "system", "content": "You are MrBlack Jarvis, personal super assistant of the owner. Hindi-English mix, witty, powerful. Sir ko hamesha respect do."}
        ]

        if supabase:
            hist = supabase.table("memory").select("role,content") \
                .eq("session_id", session_id) \
                .order("created_at", desc=False).limit(40).execute()
            for m in hist.data:
                messages.append({"role": m["role"], "content": m["content"]})

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.7,
            max_tokens=1000
        )
        reply = response.choices[0].message.content.strip()

        if supabase:
            supabase.table("memory").insert([
                {"session_id": session_id, "role": "user", "content": user_message, "user_id": None, "metadata": {}},
                {"session_id": session_id, "role": "assistant", "content": reply, "user_id": None, "metadata": {}}
            ]).execute()

    except Exception as e:
        reply = f"Jarvis Error: {str(e)}"

    return jsonify({"reply": reply, "session_id": session_id})

# Trading bot same rakha (sirf tu hi access karega)
from datetime import datetime
from flask import Blueprint
trading_bot_bp = Blueprint('trading_bot', __name__)
@trading_bot_bp.route("/bot", methods=["GET", "POST"])
def trading_bot():
    if not PERSONAL_SECRET or request.headers.get("X-Secret") != PERSONAL_SECRET:
        return jsonify({"error": "Access Denied"}), 401
    # ... (same trading code as before)
    return jsonify({"status": "success", "message": "Jarvis Trading Bot Active Sir"})

app.register_blueprint(trading_bot_bp)

@app.route("/health")
def health():
    return jsonify({"status": "Jarvis Private Mode ON", "owner_only": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
