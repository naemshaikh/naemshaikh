from flask import Flask, render_template_string, request, jsonify
import os
import google.genai as genai

app = Flask(__name__)

# Gemini setup
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# Simple chat UI
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
    <header>MrBlack Chat 🚀</header>
    <div id="chat"></div>
    <div id="input-area">
        <input id="input" placeholder="Type message..." autocomplete="off"/>
        <button id="send">➤</button>
    </div>
    <div id="typing">Thinking...</div>

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
            const msg = input.value.trim();
            if (!msg) return;
            addMessage(msg, true);
            input.value = '';
            typing.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg})
                });
                const data = await res.json();
                typing.style.display = 'none';
                addMessage(data.reply || 'Sorry, samajh nahi aaya...');
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

    try:
        response = model.generate_content(user_message)
        reply = response.text.strip()
    except Exception as e:
        reply = f"Error: {str(e)}"

    return jsonify({"reply": reply})

@app.route("/health")
def health():
    return jsonify({"health": "good"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
