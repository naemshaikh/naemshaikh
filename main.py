import os
from flask import Flask, render_template_string, request, jsonify
from openai import OpenAI
from supabase import create_client
import uuid

app = Flask(__name__)

# GROQ SETUP
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

MODEL_NAME = "llama-3.3-70b-versatile"

# SUPABASE MEMORY
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase memory connected")
else:
    print("⚠️ Supabase env missing → memory off")

# ORIGINAL UI
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack Chat</title>
    <style>
        body { margin:0; font-family:Arial; background:#f0f2f5; height:100vh; display:flex; flex-direction:column; }
        header { background:#007bff; color:white; padding:15px; text-align:center; font-size:1.3rem; }
        #chat { flex:1; overflow-y:auto; padding:15px; display:flex; flex-direction:column; gap:12px; }
        .msg { max-width:75%; padding:12px 16px; border-radius:18px; line-height:1.4; }
        .user { align-self:flex-end; background:#dcf8c6; }
        .bot { align-self:flex-start; background:white; border:1px solid #ddd; }
        #input-area { display:flex; padding:12px; background:white; border-top:1px solid #ddd; gap:8px; }
        #input { flex:1; padding:12px; border:1px solid #ccc; border-radius:25px; font-size:1rem; outline:none; }
        #send { background:#007bff; color:white; border:none; border-radius:50%; width:48px; height:48px; cursor:pointer; font-size:1.3rem; }
        #send:hover { background:#0056b3; }
        #typing { color:#777; padding:10px 15px; font-style:italic; display:none; }
    </style>
</head>
<body>
    <header>MrBlack Chat (Memory ON)</header>
    <div id="chat"></div>
    <div id="input-area">
        <input id="input" placeholder="Type message..." autocomplete="off"/>
        <button id="send">➤</button>
    </div>
    <div id="typing">Thinking...</div>

    <script>
        let sessionId = localStorage.getItem('mrblack_session') || '';
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
            const msg = input.value.trim();
            if (!msg) return;
            addMessage(msg, true);
            input.value = '';
            typing.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg, session_id: sessionId})
                });
                const data = await res.json();
                typing.style.display = 'none';
                addMessage(data.reply);
                if (data.session_id) {
                    sessionId = data.session_id;
                    localStorage.setItem('mrblack_session', sessionId);
                }
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
    data = request.get_json() or {}
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not user_message:
        return jsonify({"reply": "Kuch likho bhai!", "session_id": session_id})

    try:
        messages = [
            {"role": "system", "content": "You are MrBlack, a smart, witty and helpful Indian trading + general assistant. Baat karte time thoda Hindi-English mix karo aur mazedaar rehna."}
        ]

        if supabase:
            hist = supabase.table("memory").select("role,content") \
                .eq("session_id", session_id) \
                .order("created_at", desc=False).limit(30).execute()
            for m in hist.data:
                messages.append({"role": m["role"], "content": m["content"]})

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.75,
            max_tokens=900
        )
        reply = response.choices[0].message.content.strip()

        if supabase:
            supabase.table("memory").insert([
                {"session_id": session_id, "role": "user", "content": user_message, "user_id": None, "metadata": {}},
                {"session_id": session_id, "role": "assistant", "content": reply, "user_id": None, "metadata": {}}
            ]).execute()

    except Exception as e:
        reply = f"Error: {str(e)}"

    return jsonify({"reply": reply, "session_id": session_id})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
