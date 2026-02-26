
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/chat", methods=["POST"])
def chat():
    msg = request.json.get("message")
    return jsonify({"reply": f"Bot reply to: {msg}"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
