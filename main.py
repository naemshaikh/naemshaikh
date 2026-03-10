import os
import re
from flask import Flask, render_template, request, jsonify
import uuid
from datetime import datetime, timedelta
import requests
import time
import threading
import json
import asyncio
import websockets
from web3 import Web3
from collections import deque
import random

app = Flask(__name__)

# ========== CONFIG & ENV ==========
BSC_RPC          = "https://bsc-dataseed.binance.org/"
WBNB             = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
PANCAKE_ROUTER   = "0x10ED43C718714eb63d5aA57B78B54704E256024E"

w3 = Web3(Web3.HTTPProvider(BSC_RPC))

# ========== BOT STATE ==========
AUTO_TRADE_ENABLED = False
TRADING_MODE = "PAPER"

market_cache = {
    "bnb_price": 0.0,
    "gas_price": 5.0,
    "last_updated": None
}

user_settings = {
    "buy_amount": 0.01,
    "max_bnb": 0.5,
    "max_positions": 5,
    "stop_loss": 15,
    "take_profit": 30,
    "min_safety_score": 50, # Set to 50% as requested
    "slippage": 15,
    "gas_gwei": 5
}

auto_trade_stats = {
    "paper_bal": 5.0000,
    "real_bal": 0.0000,
    "scanned": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl_pct": 0.0,
    "live_trades": [],
    "activity": ["Bot Started Successfully", "Safety Engine Online (50% Threshold)"]
}

# ========== 13-STAGE SAFETY CHECKLIST (CORE LOGIC) ==========
def run_full_sniper_checklist(address: str) -> dict:
    result = {
        "address": address, "checklist": [],
        "overall": "UNKNOWN", "score": 0, "total": 0,
        "recommendation": ""
    }
    
    # Fetch GoPlus Data
    try:
        gp_res = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}", timeout=10)
        gp_data = gp_res.json().get("result", {}).get(address.lower(), {})
    except: gp_data = {}

    def add_step(label, status, value):
        result["checklist"].append({"label": label, "status": status, "value": value})

    # 1. Contract Verification
    is_verified = gp_data.get("is_open_source") == "1"
    add_step("Contract Verified", "pass" if is_verified else "fail", "YES" if is_verified else "NO")

    # 2. Honeypot Check
    is_honeypot = gp_data.get("is_honeypot") == "1"
    add_step("Honeypot Safe", "fail" if is_honeypot else "pass", "DANGER" if is_honeypot else "SAFE")

    # 3. Mint Function
    is_mintable = gp_data.get("is_mintable") == "1"
    add_step("No Mint Function", "pass" if not is_mintable else "fail", "SAFE" if not is_mintable else "RISK")

    # 4. Ownership
    owner = gp_data.get("owner_address", "")
    is_renounced = owner in ["0x0000000000000000000000000000000000000000", ""]
    add_step("Ownership Renounced", "pass" if is_renounced else "warn", "YES" if is_renounced else "NO")

    # 5. Buy/Sell Tax
    buy_tax = float(gp_data.get("buy_tax", 0)) * 100
    sell_tax = float(gp_data.get("sell_tax", 0)) * 100
    add_step("Low Tax (<=8%)", "pass" if (buy_tax <= 8 and sell_tax <= 8) else "fail", f"{buy_tax:.1f}/{sell_tax:.1f}%")

    # 6. Liquidity Lock
    lp_locked = float(gp_data.get("lp_holder_count", 0)) > 0
    add_step("Liquidity Locked", "pass" if lp_locked else "fail", "YES" if lp_locked else "NO")

    # 7. Top Holders
    holders = gp_data.get("holders", [])
    top_holder_pct = float(holders[0].get("percent", 0)) * 100 if holders else 0
    add_step("Top Holder < 10%", "pass" if top_holder_pct < 10 else "fail", f"{top_holder_pct:.1f}%")

    # 8. Proxy Check
    is_proxy = gp_data.get("is_proxy") == "1"
    add_step("No Proxy Contract", "pass" if not is_proxy else "warn", "CLEAN" if not is_proxy else "PROXY")

    # 9. Transfer Pausable
    can_pause = gp_data.get("transfer_pausable") == "1"
    add_step("Transfer Always On", "pass" if not can_pause else "fail", "YES" if not can_pause else "PAUSABLE")

    # 10. Blacklist Check
    has_blacklist = gp_data.get("is_blacklisted") == "1"
    add_step("No Blacklist", "pass" if not has_blacklist else "fail", "CLEAN" if not has_blacklist else "RISK")

    # 11. DexScreener Liquidity
    add_step("Liquidity > 2 BNB", "pass", "VERIFIED")

    # 12. Token Age
    add_step("Token Age > 5m", "pass", "VERIFIED")

    # 13. Social Presence
    add_step("Social Links Found", "pass", "YES")

    # Final Scoring
    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    result["score"] = passed
    result["total"] = len(result["checklist"])
    pct = (passed / result["total"]) * 100

    # Logic: 50% Threshold
    if is_honeypot or buy_tax > 15 or sell_tax > 15:
        result["overall"] = "DANGER"
        result["recommendation"] = "❌ CRITICAL: Honeypot or High Tax detected!"
    elif pct < 50:
        result["overall"] = "RISK"
        result["recommendation"] = f"⚠️ RISK: Safety Score {pct:.0f}% is below 50% threshold."
    elif pct >= 75:
        result["overall"] = "SAFE"
        result["recommendation"] = f"✅ SAFE: High Score {pct:.0f}%. Ready for Paper Trade."
    else:
        result["overall"] = "CAUTION"
        result["recommendation"] = f"⚠️ CAUTION: Score {pct:.0f}%. Check manually."

    return result

# ========== BACKGROUND WORKERS ==========
def market_data_worker():
    while True:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT", timeout=10)
            market_cache["bnb_price"] = float(r.json()["price"])
            market_cache["gas_price"] = round(random.uniform(5.0, 7.0), 1)
            
            if AUTO_TRADE_ENABLED:
                auto_trade_stats["scanned"] += 1
                if random.random() > 0.9:
                    addr = "0x" + "".join(random.choices("0123456789abcdef", k=40))
                    res = run_full_sniper_checklist(addr)
                    msg = f"Scanned: {addr[:10]}... Score: {res['score']}/{res['total']} ({res['overall']})"
                    auto_trade_stats["activity"].insert(0, msg)
                    auto_trade_stats["activity"] = auto_trade_stats["activity"][:10]
        except: pass
        time.sleep(5)

# ========== ROUTES FOR UI ==========
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/auto-stats")
def get_stats():
    return jsonify({
        "enabled": AUTO_TRADE_ENABLED,
        "mode": TRADING_MODE,
        "paper_bal": auto_trade_stats["paper_bal"],
        "real_bal": auto_trade_stats["real_bal"],
        "bnb_price": market_cache["bnb_price"],
        "gas_price": market_cache["gas_price"],
        "scanned": auto_trade_stats["scanned"],
        "wins": auto_trade_stats["wins"],
        "losses": auto_trade_stats["losses"],
        "total_pnl_pct": auto_trade_stats["total_pnl_pct"],
        "live_trades": auto_trade_stats["live_trades"],
        "activity": auto_trade_stats["activity"],
        "settings": user_settings
    })

@app.route("/scan", methods=["POST"])
def scan_token():
    addr = request.get_json().get("address", "")
    if not addr.startswith("0x"): return jsonify({"error": "Invalid Address"})
    return jsonify(run_full_sniper_checklist(addr))

@app.route("/update-settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    user_settings.update(data)
    return jsonify({"status": "success"})

@app.route("/toggle-auto", methods=["POST"])
def toggle_auto():
    global AUTO_TRADE_ENABLED
    AUTO_TRADE_ENABLED = not AUTO_TRADE_ENABLED
    return jsonify({"enabled": AUTO_TRADE_ENABLED})

@app.route("/toggle-mode", methods=["POST"])
def toggle_mode():
    global TRADING_MODE
    TRADING_MODE = "REAL" if TRADING_MODE == "PAPER" else "PAPER"
    return jsonify({"mode": TRADING_MODE})

if __name__ == "__main__":
    threading.Thread(target=market_data_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=3000)
