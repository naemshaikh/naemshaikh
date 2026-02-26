from flask import Flask, jsonify, request
import os
from TradingBot.my_trading_bot import trading_bot_bp  # ← yahan import ho raha hai

app = Flask(__name__)

# Blueprints register kar rahe hain taaki sab routes kaam karein
app.register_blueprint(trading_bot_bp)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Bot is running successfully 🚀 Trading + Chat both live!"
    })

@app.route("/health")
def health():
    return jsonify({"health": "good"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
