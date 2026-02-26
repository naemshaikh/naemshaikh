from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>MRBLACK AI</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background-color: #0f172a;
            color: white;
        }

        .header {
            padding: 15px;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            background: #1e293b;
            letter-spacing: 2px;
        }

        .container {
            display: flex;
            height: calc(100vh - 60px);
        }

        .left, .right {
            padding: 20px;
        }

        .left {
            width: 60%;
            border-right: 1px solid #334155;
        }

        .right {
            width: 40%;
            display: flex;
            flex-direction: column;
        }

        .card {
            background: #1e293b;
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 20px;
        }

        .balance {
            font-size: 22px;
            color: #00f5c4;
        }

        button {
            padding: 10px 20px;
            margin-right: 10px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-weight: bold;
        }

        .start {
            background: linear-gradient(to right, #00f5c4, #00c2ff);
            color: black;
        }

        .stop {
            background: #ef4444;
            color: white;
        }

        .logs {
            background: black;
            padding: 10px;
            border-radius: 10px;
            height: 200px;
            overflow-y: auto;
            font-size: 12px;
        }

        .chat-box {
            flex: 1;
            background: #1e293b;
            padding: 15px;
            border-radius: 12px;
            overflow-y: auto;
            margin-bottom: 10px;
        }

        .message {
            margin-bottom: 10px;
        }

        .user {
            text-align: right;
            color: #00f5c4;
        }

        .bot {
            text-align: left;
            color: #ffffff;
        }

        .chat-input {
            display: flex;
        }

        .chat-input input {
            flex: 1;
            padding: 10px;
            border-radius: 8px;
            border: none;
            outline: none;
        }

        .chat-input button {
            margin-left: 10px;
            background: linear-gradient(to right, #00f5c4, #00c2ff);
            color: black;
        }
    </style>
</head>
<body>

<div class="header">
    MRBLACK AI TRADING SYSTEM
</div>

<div class="container">
    <!-- LEFT SIDE -->
    <div class="left">
        <div class="card">
            <div>Wallet Balance</div>
            <div class="balance">2.45 BNB</div>
        </div>

        <div class="card">
            <button class="start">Start Bot</button>
            <button class="stop">Stop Bot</button>
        </div>

        <div class="card">
            <div>Live Logs</div>
            <div class="logs">
                Bot initialized...<br>
                Waiting for signal...<br>
            </div>
        </div>
    </div>

    <!-- RIGHT SIDE -->
    <div class="right">
        <div class="chat-box" id="chatBox">
            <div class="message bot">AI: Hello Trader 👋</div>
        </div>

        <div class="chat-input">
            <input type="text" id="userInput" placeholder="Ask something...">
            <button onclick="sendMessage()">Send</button>
        </div>
    </div>
</div>

<script>
function sendMessage() {
    let input = document.getElementById("userInput");
    let message = input.value;
    if (message.trim() === "") return;

    let chatBox = document.getElementById("chatBox");

    chatBox.innerHTML += '<div class="message user">You: ' + message + '</div>';
    chatBox.innerHTML += '<div class="message bot">AI: Processing...</div>';

    input.value = "";
    chatBox.scrollTop = chatBox.scrollHeight;
}
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(debug=True)
