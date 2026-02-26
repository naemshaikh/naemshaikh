
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

@app.route("/")
def home():
    return jsonify({"status": "ok", "bot_state": bot_state})

