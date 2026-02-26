from flask import Flask, render_template_string, request, jsonify
import os
import google.generativeai as genai

app = Flask(__name__)

# Gemini API setup (Render pe Environment Variable se key le)
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel('gemini-2.5-flash')  # Free tier model (Flash sabse achha free mein)

# ==================== Simple & Nice Chat Interface ====================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack Gemini Chat</title>
    <style>
        body { margin:0; font-family:Arial; background:#f0f7ff; height:100vh; display:flex; flex-direction:column; }
        #header { background:#4285f4; color:white; padding:15px; text-align:center; font-size:1.4rem; box-shadow:0 2px 5px rgba(0,0,0,0.2); }
        #chat { flex:1; overflow-y:auto; padding:15px; display:flex; flex-direction:column; gap:12px; }
        .msg { max-width:75%; padding:12px 16px; border-radius:18px; line-height:1.4; }
        .user { align-self:flex-end; background:#dcf8c6; }
        .bot { align-self:flex-start; background:white; border:1px solid #ddd; }
        #input { display:flex; padding:12px; background:#fff; border-top:1px solid #ddd; gap:8px; }
        #text { flex:1; padding:12px; border:1px solid #ccc; border-radius:24px; font-size:1rem; }
        #send { background:#4285f4; color:white; border:none; border-radius:50%; width:48px; height:48px; cursor:pointer; font-size:1.2rem; }
        #typing { color:#777; padding:10px; display:none; font-style:italic; }
    </style>
</head>
<body>
    <div id="header">MrBlack Gemini Chat 🚀 (Free AI)</div>
    <div id="chat"></div>
    <div id="input">
        <input id="text" placeholder="Type your message..." />
        <button id="send">➤</button>
    </div>
    <div id="typing">Gemini typing...</div>

    <script>
        const chat = document.getElementById('chat');
        const text = document.getElementById('text');
        const send = document.getElementById('send');
        const typing = document.getElementById('typing');

        function addMsg(txt, isUser=false) {
            const div = document.createElement('div');
            div.className = 'msg ' + (isUser ? 'user' : 'bot');
            div.textContent = txt;
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
        }

        async function sendMsg() {
            const msg = text.value.trim();
            if (!msg) return;
            addMsg(msg, true);
            text.value = '';
            typing.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg})
                });
                const data = await res.json();
                typing.style.display = 'none';
                addMsg(data.reply || 'Sorry, kuch samajh nahi aaya...');
            } catch(e) {
                typing.style.display = 'none';
                addMsg('Error: ' + e.message);
            }
        }

        send.onclick = sendMsg;
        text.addEventListener('keypress', e => { if(e.key === 'Enter') sendMsg(); });
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
            return jsonify({"reply": "Kuch to bol bhai... 😅"})

        # Gemini call (free tier model)
        response = model.generate_content(user_msg)
        reply = response.text.strip()

        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"reply": f"Error aa gaya: {str(e)} (check API key ya limit)"})

@app.route("/health")
def health():
    return jsonify({"health": "good"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
