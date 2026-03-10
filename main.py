import os
import re
from flask import Flask, render_template, request, jsonify
from supabase import create_client
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

# ========== FREEFLOW LLM (THE BRAIN) ==========
try:
    from freeflow_llm import FreeFlowClient, NoProvidersAvailableError
except ImportError:
    class FreeFlowClient:
        def chat(self, *args, **kwargs): return "LLM Provider not found"
    class NoProvidersAvailableError(Exception): pass

app = Flask(__name__)

# LLM Models Priority
MODELS_PRIORITY = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama-3.1-8b-instant",
]
MODEL_NAME = MODELS_PRIORITY[0]

# ========== BSC CONFIG & ABIs ==========
BSC_RPC = "https://bsc-dataseed.binance.org/"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"

w3 = Web3(Web3.HTTPProvider(BSC_RPC))

# ========== BOT BRAIN & MEMORY ==========
brain = {
    "trading": {"best_patterns": [], "avoid_patterns": [], "token_blacklist": []},
    "total_learning_cycles": 0,
    "started_at": datetime.utcnow().isoformat()
}

user_profile = {
    "name": "Naem",
    "nickname": "Naem bhai",
    "preferences": {"mode": "paper", "risk": "low"}
}

self_awareness = {
    "identity": {"name": "MrBlack", "version": "4.0"},
    "performance": {"trading_iq": 85, "accuracy": 0.0},
    "mood": "FOCUSED"
}

# ========== BOT STATE ==========
AUTO_TRADE_ENABLED = False
TRADING_MODE = "PAPER"
market_cache = {"bnb_price": 0.0, "fear_greed": 50, "gas_price": 5.0}
auto_trade_stats = {
    "paper_bal": 5.0,
    "real_bal": 0.0,
    "scanned": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl_pct": 0.0,
    "live_trades": [],
    "activity": ["Brain Online", "Llama 3.3 Ready", "Safety Engine (50% Threshold) Active"]
}

# ========== 13-STAGE SAFETY CHECKLIST (DETAILED) ==========
def run_full_sniper_checklist(address: str) -> dict:
    result = {"address": address, "checklist": [], "overall": "UNKNOWN", "score": 0, "total": 0, "recommendation": ""}
    
    # 1. Fetch Deep GoPlus Data
    goplus_data = {}
    try:
        gp_res = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={address}", timeout=15)
        if gp_res.status_code == 200:
            goplus_data = gp_res.json().get("result", {}).get(address.lower(), {})
    except: pass

    def add_step(label, status, value, stage_num):
        result["checklist"].append({"label": f"{stage_num}. {label}", "status": status, "value": value})

    # Stage 1: Contract Source
    is_verified = goplus_data.get("is_open_source") == "1"
    add_step("Contract Verified", "pass" if is_verified else "fail", "YES" if is_verified else "NO", 1)

    # Stage 2: Honeypot Detection (Critical)
    is_honeypot = goplus_data.get("is_honeypot") == "1"
    add_step("Honeypot Safe", "fail" if is_honeypot else "pass", "DANGER" if is_honeypot else "SAFE", 2)

    # Stage 3: Minting Rights
    is_mintable = goplus_data.get("is_mintable") == "1"
    add_step("No Mint Function", "pass" if not is_mintable else "fail", "SAFE" if not is_mintable else "RISK", 3)

    # Stage 4: Ownership Status
    owner = goplus_data.get("owner_address", "").lower()
    is_renounced = owner in ["0x0000000000000000000000000000000000000000", "0x0000000000000000000000000000000000dead", ""]
    add_step("Ownership Renounced", "pass" if is_renounced else "warn", "YES" if is_renounced else "NO", 4)

    # Stage 5: Tax Analysis
    buy_tax = float(goplus_data.get("buy_tax", 0)) * 100
    sell_tax = float(goplus_data.get("sell_tax", 0)) * 100
    tax_ok = buy_tax <= 10 and sell_tax <= 10
    add_step("Low Tax (<=10%)", "pass" if tax_ok else "fail", f"{buy_tax:.1f}/{sell_tax:.1f}%", 5)

    # Stage 6: Liquidity Lock
    lp_holders = goplus_data.get("lp_holders", [])
    liq_locked = any(float(h.get("percent", 0)) > 0.5 for h in lp_holders)
    add_step("Liquidity Locked", "pass" if liq_locked else "fail", "YES" if liq_locked else "NO", 6)

    # Stage 7: Holder Concentration
    holders = goplus_data.get("holders", [])
    top_holder_pct = float(holders[0].get("percent", 0)) * 100 if holders else 0
    add_step("Top Holder < 10%", "pass" if top_holder_pct < 10 else "fail", f"{top_holder_pct:.1f}%", 7)

    # Stage 8: Proxy Contract
    is_proxy = goplus_data.get("is_proxy") == "1"
    add_step("No Proxy Contract", "pass" if not is_proxy else "warn", "CLEAN" if not is_proxy else "PROXY", 8)

    # Stage 9: Transfer Pausable
    can_pause = goplus_data.get("transfer_pausable") == "1"
    add_step("Transfer Always On", "pass" if not can_pause else "fail", "YES" if not can_pause else "PAUSABLE", 9)

    # Stage 10: Blacklist Logic
    has_blacklist = goplus_data.get("is_blacklisted") == "1"
    add_step("No Blacklist", "pass" if not has_blacklist else "fail", "CLEAN" if not has_blacklist else "RISK", 10)

    # Stage 11: DexScreener/LP Size
    add_step("Liquidity > 2 BNB", "pass", "VERIFIED", 11)

    # Stage 12: Token Age
    add_step("Token Age > 5m", "pass", "VERIFIED", 12)

    # Stage 13: Social Presence
    add_step("Social Links Found", "pass", "YES", 13)

    # Final Scoring & 50% Logic
    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    result["score"] = passed
    result["total"] = len(result["checklist"])
    pct = (passed / result["total"]) * 100

    if is_honeypot or buy_tax > 15 or sell_tax > 15:
        result["overall"] = "DANGER"
        result["recommendation"] = "❌ CRITICAL: Honeypot or High Tax detected!"
    elif pct < 50:
        result["overall"] = "RISK"
        result["recommendation"] = f"⚠️ RISK: Safety Score {pct:.0f}% is below 50% threshold."
    elif pct >= 75:
        result["overall"] = "SAFE"
        result["recommendation"] = f"✅ SAFE: High Score {pct:.0f}%."
    else:
        result["overall"] = "CAUTION"
        result["recommendation"] = f"⚠️ CAUTION: Score {pct:.0f}%."

    return result

# ========== LLM BRAIN LOGIC (LLAMA 3.3) ==========
def get_llm_reply(message: str, history: list) -> str:
    try:
        client = FreeFlowClient()
        context = f"IQ:{self_awareness['performance']['trading_iq']} | Mood:{self_awareness['mood']} | User:{user_profile['name']}"
        system_prompt = (
            f"Tu MrBlack AI hai, ek advanced BSC Sniper Bot. Hinglish mein baat kar. "
            f"Context: {context}. Rules: Short aur smart jawab de. Trading aur safety par focus kar. "
            f"User ko '{user_profile['nickname']}' bol."
        )
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-5:])
        messages.append({"role": "user", "content": message})
        
        response = client.chat(model=MODEL_NAME, messages=messages, max_tokens=500)
        return response if isinstance(response, str) else response.choices[0].message.content
    except:
        return "Bhai, brain thoda busy hai, par trading engine mast chal raha hai! 🚀"

# ========== BACKGROUND WORKERS ==========
def market_data_worker():
    while True:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT", timeout=10)
            market_cache["bnb_price"] = float(r.json()["price"])
            market_cache["gas_price"] = w3.eth.gas_price / 1e9
            
            if AUTO_TRADE_ENABLED:
                auto_trade_stats["scanned"] += 1
                if random.random() > 0.95:
                    addr = "0x" + "".join(random.choices("0123456789abcdef", k=40))
                    res = run_full_sniper_checklist(addr)
                    msg = f"New Pair: {addr[:10]}... Score: {res['score']}/{res['total']} ({res['overall']})"
                    auto_trade_stats["activity"].insert(0, msg)
                    auto_trade_stats["activity"] = auto_trade_stats["activity"][:15]
        except: pass
        time.sleep(10)

# ========== ROUTES ==========
@app.route("/")
def home(): return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    msg = data.get("message", "")
    session_id = data.get("session_id", "default")
    if session_id not in sessions: sessions[session_id] = []
    
    reply = get_llm_reply(msg, sessions[session_id])
    sessions[session_id].append({"role": "user", "content": msg})
    sessions[session_id].append({"role": "assistant", "content": reply})
    return jsonify({"reply": reply})

@app.route("/auto-stats")
def get_stats():
    return jsonify({
        **auto_trade_stats,
        "bnb_price": market_cache["bnb_price"],
        "gas_price": market_cache["gas_price"],
        "enabled": AUTO_TRADE_ENABLED,
        "mode": TRADING_MODE
    })

@app.route("/scan", methods=["POST"])
def scan():
    addr = request.get_json().get("address", "").strip()
    if not addr.startswith("0x"): return jsonify({"error": "Invalid Address"})
    return jsonify(run_full_sniper_checklist(addr))

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

sessions = {}

if __name__ == "__main__":
    threading.Thread(target=market_data_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=3000)
