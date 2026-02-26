from flask import Flask, jsonify
import os
import pickle

app = Flask(__name__)

# Load bot state
bot_state_path = os.path.join(os.path.dirname(__file__), "TradingBot", "bot_state.pkl")

if os.path.exists(bot_state_path):
    with open(bot_state_path, "rb") as f:
        bot_state = pickle.load(f)
else:
    bot_state = {}

# 🔥 Make everything JSON safe (handles tuple keys, nested dict, list etc.)
def make_json_safe(data):
    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            # Convert tuple keys to string
            if isinstance(k, tuple):
                k = str(k)
            new_dict[str(k)] = make_json_safe(v)
        return new_dict
    elif isinstance(data, list):
        return [make_json_safe(i) for i in data]
    else:
        return data

@app.route("/")
def home():
    safe_state = make_json_safe(bot_state)
    return jsonify({
        "status": "ok",
        "bot_state": safe_state
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
