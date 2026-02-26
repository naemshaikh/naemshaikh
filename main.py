
from flask import Flask, jsonify
import os

app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Bot is running successfully 🚀"
    })

@app.route("/health")
def health():
    return jsonify({"health": "good"})

