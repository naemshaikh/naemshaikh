from flask import Flask, render_template_string, request, jsonify
import os
from openai import OpenAI

app = Flask(__name__)

# OpenAI client (Render pe environment variable se key le rahe hain)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ==================== Beautiful Chat Interface ====================
CHAT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>MrBlack AI Chat</title>
    <style>
        body { margin:0; font-family: 'Segoe UI', sans-serif; background: linear-gradient(to bottom, #e0f7fa, #ffffff); height:100vh; display:flex; flex-direction:column; }
        #header { background:#00796b; color:white; padding:12px; text-align:center; font-weight:bold; font-size:1.3rem; box-shadow:0 2px 5px rgba(0,0,0,0.2); }
        #chat-container { flex:1; overflow-y:auto; padding:15px; display:flex; flex-direction:column; gap:12px; }
        .message { max-width:80%; padding:12px 16px; border-radius:18px; line-height:1.4; word-wrap:break-word; }
        .user { align-self:flex-end; background:#dcf8c6; color:#000; }
        .bot { align-self:flex-start; background:#ffffff; color:#000; border:1px solid #e0e0e0; box-shadow:0 1px 3px rgba(0,0,0,0.1); }
        #input-area { display:flex; padding:12px; background:#f5f5f5; border-top:1px solid #ddd; gap:8px; }
        #user-input { flex:1; padding:12px; border:1px solid #ccc; border-radius:24px; font-size:1rem; outline:none; }
        #send-btn { background:#00796b; color:white; border:none; border-radius:50%; width:48px; height:48px; font-size:1.3rem; cursor:pointer; }
        #send-btn:hover { background:#004d40; }
        #typing { font-style:italic; color:#777; padding-left:16px; display:none; }
    </style>
</head>
<body>
    <div id="header">MrBlack AI Chat 🚀</div>

    <div id="chat-container"></div>

    <div id="input-area">
        <input id="user-input" placeholder="Type your message..." autocomplete="off" />
        <button id="send-btn">➤</button>
    </div>

    <div id="typing">Bot is typing...</div>

    <script>
        const chatContainer = document.getElementById("chat-container");
        const userInput = document.getElementById("user-input");
        const sendBtn = document.getElementById("send-btn");
        const typing = document.getElementById("typing");

        function addMessage(text, isUser = false) {
            const msg = document.createElement("div");
            msg.className = "message " + (isUser ? "user" : "bot");
            msg.textContent = text;
            chatContainer.appendChild(msg);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        async function sendMessage() {
            const text = userInput.value.trim();
            if (!text) return;

            addMessage(text, true);
            userInput.value = "";
            typing.style.display = "block";

            try {
                const res = await fetch("/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ message: text })
                });

                const data = await res.json();
                typing.style.display = "none";
                addMessage(data.reply || "Sorry, kuch samajh nahi aaya...");
            } catch (err) {
                typing.style.display = "none";
                addMessage("Error: " + err.message);
            }
        }

        sendBtn.onclick = sendMessage;
        userInput.addEventListener("keypress", e => {
            if (e.key === "Enter") sendMessage();
        });
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(CHAT_HTML)

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        user_message = data.get("message", "").strip()

        if not user_message:
            return jsonify({"reply": "Kuch to bol bhai... 😅"})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are MrBlack, a cool, friendly and helpful AI assistant. Reply in Hindi-English mix, casual style like a friend."},
                {"role": "user", "content": user_message}
            ],
            max_tokens=300,
            temperature=0.8
        )

        reply = response.choices[0].message.content.strip()
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"reply": f"Oops! Error aa gaya: {str(e)}"})

@app.route("/health")
def health():
    return jsonify({"health": "good"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
