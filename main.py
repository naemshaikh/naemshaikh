import os
import re
import gc
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
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional
import hmac
import hashlib

# ========== FREEFLOW LLM ==========
from freeflow_llm import FreeFlowClient, NoProvidersAvailableError

app = Flask(__name__)
MODELS_PRIORITY = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "mixtral-8x7b-32768",
    "llama-3.1-8b-instant",
]
MODEL_NAME  = MODELS_PRIORITY[0]
MODEL_FAST  = "llama-3.1-8b-instant"
MODEL_DEEP  = "llama-3.3-70b-versatile"

# ========== ENV CONFIG ==========
BSC_RPC          = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API     = "https://api.bscscan.com/api"
BSC_SCAN_KEY     = os.getenv("BSC_SCAN_KEY") or os.getenv("BSCSCAN_API_KEY") or os.getenv("BSC_API_KEY", "") or os.getenv("BSCSCAN_API_KEY", "")
PANCAKE_ROUTER   = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY  = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
WBNB             = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"  # FIX 1: WBNB defined
MORALIS_API_KEY  = os.getenv("MORALIS_API_KEY", "")

DAPPRADAR_KEY    = os.getenv("DAPPRADAR_KEY", "")

SMART_WALLETS = [
    w.strip() for w in os.getenv("SMART_WALLETS", "").split(",") if w.strip()
]

PAIR_ABI_PRICE = [
    {"name":"getReserves","type":"function","stateMutability":"view","inputs":[],
     "outputs":[{"name":"reserve0","type":"uint112"},{"name":"reserve1","type":"uint112"},{"name":"blockTimestampLast","type":"uint32"}]},
    {"name":"token0","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"address"}]}
]
FACTORY_ABI_PRICE = [
    {"name":"getPair","type":"function","stateMutability":"view",
     "inputs":[{"name":"tokenA","type":"address"},{"name":"tokenB","type":"address"}],
     "outputs":[{"name":"pair","type":"address"}]}
]
ROUTER_ABI_PRICE = [
    {"name":"getAmountsOut","type":"function","stateMutability":"view",
     "inputs":[{"name":"amountIn","type":"uint256"},{"name":"path","type":"address[]"}],
     "outputs":[{"name":"amounts","type":"uint256[]"}]}
]
TOKEN_DEC_ABI = [{"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"uint8"}]}]
_dec_cache = {}

def _get_dec(addr):
    if addr.lower() in _dec_cache: return _dec_cache[addr.lower()]
    try: d = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=TOKEN_DEC_ABI).functions.decimals().call()
    except: d = 18
    if len(_dec_cache) > 200:  # Cache size limit
        for k in list(_dec_cache.keys())[:100]:
            del _dec_cache[k]
    _dec_cache[addr.lower()] = d
    return d

def _get_v2_pair(token_address):
    try:
        p = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_FACTORY), abi=FACTORY_ABI_PRICE).functions.getPair(
            Web3.to_checksum_address(token_address), Web3.to_checksum_address(WBNB)).call()
        return "" if p == "0x0000000000000000000000000000000000000000" else p
    except: return ""

PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"
V3_FEE_TIERS       = [500, 2500, 10000]

V3_FACTORY_ABI = [{"name":"getPool","type":"function","stateMutability":"view",
    "inputs":[{"name":"tokenA","type":"address"},{"name":"tokenB","type":"address"},{"name":"fee","type":"uint24"}],
    "outputs":[{"name":"pool","type":"address"}]}]

V3_POOL_ABI = [
    {"name":"slot0","type":"function","stateMutability":"view","inputs":[],
     "outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},
                {"name":"observationIndex","type":"uint16"},{"name":"observationCardinality","type":"uint16"},
                {"name":"observationCardinalityNext","type":"uint16"},{"name":"feeProtocol","type":"uint32"},
                {"name":"unlocked","type":"bool"}]},
    {"name":"token0","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"address"}]},
    {"name":"token1","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"address"}]},
    {"name":"liquidity","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"uint128"}]}
]

def _get_v3_pool(token_address):
    try:
        v3f     = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_V3_FACTORY), abi=V3_FACTORY_ABI)
        tok_cs  = Web3.to_checksum_address(token_address)
        wbnb_cs = Web3.to_checksum_address(WBNB)
        zero    = "0x0000000000000000000000000000000000000000"
        for fee in V3_FEE_TIERS:
            try:
                pool = v3f.functions.getPool(tok_cs, wbnb_cs, fee).call()
                if pool and pool != zero:
                    return pool
            except: continue
    except: pass
    return ""

def _get_v3_price_bnb(pool_address, token_address):
    try:
        pc      = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=V3_POOL_ABI)
        slot0   = pc.functions.slot0().call()
        sqrtP   = slot0[0]
        if sqrtP == 0: return 0.0
        token0  = pc.functions.token0().call()
        dec     = _get_dec(token_address)
        raw     = (sqrtP / (2**96)) ** 2
        if token0.lower() == WBNB.lower():
            return raw * (10**(18 - dec))
        else:
            adj = raw * (10**(dec - 18))
            return 1.0 / adj if adj > 0 else 0.0
    except: return 0.0

def _get_dexpaprika_price_bnb(token_address):
    try:
        r = requests.get(f"https://api.dexpaprika.com/networks/bsc/tokens/{token_address.lower()}", timeout=8)
        if r.status_code == 200:
            pusd = float((r.json().get("summary") or {}).get("price_usd", 0) or 0)
            if pusd > 0:
                bnb = market_cache.get("bnb_price", 0)
                return pusd / bnb
    except: pass
    return 0.0

# FIX: Single method price fetch — Router only (fast BSC RPC, no HTTP fallbacks)
# Fallbacks only used at BUY time via get_token_price_bnb_full()
_monitor_price_cache = {}  # {addr: (price, timestamp)}

def get_token_price_bnb(token_address: str) -> float:
    import time as _t
    # Cache: same token 1s ke andar dobara call nahi
    _cached = _monitor_price_cache.get(token_address)
    if _cached and (_t.time() - _cached[1]) < 1.0:
        return _cached[0]
    try:
        dec = _get_dec(token_address)
        amt = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_ABI_PRICE).functions.getAmountsOut(
            10**dec, [Web3.to_checksum_address(token_address), Web3.to_checksum_address(WBNB)]).call()
        if amt[1] > 0:
            price = amt[1] / 1e18
            _monitor_price_cache[token_address] = (price, _t.time())
            # Cache cleanup — max 50 entries
            if len(_monitor_price_cache) > 50:
                oldest = sorted(_monitor_price_cache.items(), key=lambda x: x[1][1])[:10]
                for k, _ in oldest: del _monitor_price_cache[k]
            return price
    except: pass
    return 0.0

def get_token_price_bnb_full(token_address: str) -> float:
    """Full fallback chain — sirf BUY time pe use karo, monitor mein nahi"""
    p = get_token_price_bnb(token_address)
    if p > 0: return p
    try:
        pair = _get_v2_pair(token_address)
        if pair:
            pc = w3.eth.contract(address=Web3.to_checksum_address(pair), abi=PAIR_ABI_PRICE)
            t0 = pc.functions.token0().call()
            r  = pc.functions.getReserves().call()
            dec = _get_dec(token_address)
            if r[0] > 0 and r[1] > 0:
                return (r[0]/1e18)/(r[1]/(10**dec)) if t0.lower()==WBNB.lower() else (r[1]/1e18)/(r[0]/(10**dec))
    except: pass
    try:
        v3pool = _get_v3_pool(token_address)
        if v3pool:
            pr = _get_v3_price_bnb(v3pool, token_address)
            if pr > 0: return pr
    except: pass
    try:
        pr = _get_dexpaprika_price_bnb(token_address)
        if pr > 0: return pr
    except: pass
    try:
        resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=8)
        if resp.status_code == 200:
            _resp_raw = (resp.json() or {}).get("pairs") or []
            if not isinstance(_resp_raw, list): _resp_raw = []
            bsc = [p for p in _resp_raw if p and p.get("chainId")=="bsc"]
            if bsc:
                bsc.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd",0) or 0), reverse=True)
                pusd = float(bsc[0].get("priceUsd",0) or 0)
                bnb  = market_cache.get("bnb_price", 0)
                return pusd/bnb if pusd > 0 else 0.0
    except: pass
    return 0.0

w3 = Web3(Web3.HTTPProvider(BSC_RPC))
threading.Thread(target=lambda: print(f"✅ BSC: {w3.is_connected()}"), daemon=True).start()

# ========== SUPABASE ==========
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase memory connected")
    except Exception as e:
        print(f"❌ Supabase failed: {e}")

# ========== KNOWLEDGE BASE ==========
knowledge_base = {
    "dex":      {"uniswap": {}, "pancakeswap": {}, "aerodrome": {}, "raydium": {}, "jupiter": {}},
    "bsc":      {"new_tokens": [], "trending": [], "scams": [], "safu_tokens": []},
    "airdrops": {"active": [], "upcoming": [], "ended": []},
    "trading":  {"news": [], "fear_greed": {}, "market_data": {}}
}

class TradingMode(Enum):
    PAPER = "PAPER"
    REAL  = "REAL"

@dataclass
class ModeSettings:
    mode:                   TradingMode
    total_balance:          float
    exposure_limit:         float
    daily_loss_limit:       float
    max_position_per_token: float
    reserve_capital:        float

class MrBlackChecklistEngine:
    def __init__(self, initial_balance=1.0, mode=TradingMode.PAPER):
        self.mode = ModeSettings(
            mode=mode, total_balance=initial_balance,
            exposure_limit=0.22, daily_loss_limit=0.065,
            max_position_per_token=0.025, reserve_capital=0.78
        )
        self.paper_stats = None
        self.positions   = {}
        self.trade_history = []
        print("✅ MrBlack Checklist Engine Initialized")

bsc_engine = MrBlackChecklistEngine()

# ========== MARKET CACHE (early init) ==========
market_cache = {
    "bnb_price":    0.0,
    "fear_greed":   50,
    "trending":     [],
    "last_updated": None
}

# ========== DATA GUARD — STRICT REAL DATA ENFORCEMENT ==========
class DataGuard:
    PRICE_STALE_SEC = 90  # BNB loop har 30s mein fetch karta hai — 90s safe margin
    MIN_BNB_PRICE   = 100
    MAX_BNB_PRICE   = 5000
    _gas_cache      = {"val": 0.0, "ts": 0}

    @staticmethod
    def bnb_price_ok():
        price = market_cache.get("bnb_price", 0)
        if not price or price <= 0:
            return False, "BNB price = 0 — NodeReal stream connected nahi"
        if price < DataGuard.MIN_BNB_PRICE or price > DataGuard.MAX_BNB_PRICE:
            return False, f"BNB price suspicious: ${price:.2f}"
        ts = market_cache.get("last_updated")
        if not ts:
            return False, "BNB price timestamp missing"
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(ts.replace("Z",""))).total_seconds()
            if age > DataGuard.PRICE_STALE_SEC:
                return False, f"BNB price stale: {age:.0f}s purana — stream down"
        except Exception:
            return False, "BNB timestamp parse error"
        return True, "ok"

    @staticmethod
    def token_price_ok(price_bnb, address):
        if not price_bnb or price_bnb <= 0:
            return False, f"Token price=0 for {address[:10]}"
        if price_bnb > 1.0:
            return False, f"Token price > 1 BNB — suspicious"
        if price_bnb < 1e-18:
            return False, f"Token price too tiny — dead token"
        return True, "ok"

    @staticmethod
    def get_real_gas_bnb():
        now = time.time()
        if now - DataGuard._gas_cache["ts"] < 30 and DataGuard._gas_cache["val"] > 0:
            return DataGuard._gas_cache["val"]
        try:
            _key = os.environ.get("NODEREAL_API_KEY", "")
            if _key:
                r = requests.post(
                    f"https://bsc-mainnet.nodereal.io/v1/{_key}",
                    json={"jsonrpc":"2.0","id":1,"method":"eth_gasPrice","params":[]},
                    timeout=5
                )
                gwei_hex = r.json().get("result", "0x0")
                gwei     = int(gwei_hex, 16) / 1e9
                if 0.5 < gwei < 100:
                    gas_bnb = (gwei * 1e9 * 150000) / 1e18
                    DataGuard._gas_cache = {"val": gas_bnb, "ts": now}
                    return gas_bnb
        except Exception as e:
            print(f"⚠️ Gas fetch error: {e}")
        return 0.0

    @staticmethod
    def trade_allowed(address, token_price_bnb):
        ok, msg = DataGuard.bnb_price_ok()
        if not ok:
            return False, f"DATA_GUARD [BNB]: {msg}"
        ok, msg = DataGuard.token_price_ok(token_price_bnb, address)
        if not ok:
            return False, f"DATA_GUARD [TOKEN]: {msg}"
        return True, "All data verified real"

# ========== GLOBAL USER PROFILE ==========
user_profile = {
    "name":           None,
    "nickname":       None,
    "known_since":    None,
    "preferences":    {},
    "personal_notes": [],
    "total_sessions": 0,
    "last_seen":      None,
    "language":       "hinglish",
    "loaded":         False,
    "user_rules":     [],
}

def _load_user_profile():
    if not supabase:
        return
    try:
        res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_USER").execute()
        if res.data:
            row = res.data[0]
            try:
                pos_raw = row.get("positions") or "{}"
                if isinstance(pos_raw, bytes):
                    pos_raw = pos_raw.decode("utf-8")
                stored = json.loads(pos_raw) if pos_raw and pos_raw != "null" else {}
            except Exception as je:
                print(f"Profile JSON parse error: {je}")
                stored = {}
            user_profile.update({
                "name":           stored.get("name"),
                "nickname":       stored.get("nickname"),
                "known_since":    stored.get("known_since"),
                "preferences":    stored.get("preferences", {}),
                "personal_notes": stored.get("personal_notes", []),
                "total_sessions": stored.get("total_sessions", 0),
                "last_seen":      stored.get("last_seen"),
                "language":       stored.get("language", "hinglish"),
                "user_rules":     stored.get("user_rules", []),
            })
            user_profile["loaded"] = True
            if not user_profile.get("name"):
                user_profile["name"] = "Naem"
                user_profile["nickname"] = "Naem bhai"
                threading.Thread(target=_save_user_profile, daemon=True).start()
            print(f"User profile loaded — Name: {user_profile.get('name')}")
    except Exception as e:
        print(f"User profile load error: {e}")

    if not user_profile.get("loaded") or not user_profile.get("name"):
        user_profile["name"]     = "Naem"
        user_profile["nickname"] = "Naem bhai"
        user_profile["loaded"]   = True
        threading.Thread(target=_save_user_profile, daemon=True).start()

_profile_save_cache = {"last_save": 0}

def _save_user_profile():
    import time as _t
    if not supabase:
        return
    if _t.time() - _profile_save_cache["last_save"] < 120:
        return
    _profile_save_cache["last_save"] = _t.time()
    try:
        user_profile["last_seen"] = datetime.utcnow().isoformat()
        user_profile["total_sessions"] = user_profile.get("total_sessions", 0) + 1
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_USER",
            "role":       "user",
            "content":    "",
            "history":    json.dumps([]),
            "positions":  json.dumps({
                "name":           user_profile.get("name"),
                "nickname":       user_profile.get("nickname"),
                "known_since":    user_profile.get("known_since"),
                "preferences":    user_profile.get("preferences", {}),
                "personal_notes": user_profile.get("personal_notes", [])[-50:],
                "total_sessions": user_profile.get("total_sessions", 0),
                "last_seen":      user_profile.get("last_seen"),
                "language":       user_profile.get("language", "hinglish"),
                "user_rules":     user_profile.get("user_rules", [])[-30:],
            }),
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"User profile save error: {e}")

def _extract_user_info_from_message(message: str):
    msg_lower = message.lower()
    name_patterns = [
        r"(?:mera naam|my name is|main hoon|i am|i\'m|call me|mujhe bolo)\s+([a-zA-Z]+)",
        r"(?:naam hai|naam)\s+([a-zA-Z]+)",
        r"^([a-zA-Z]+)\s+(?:hoon|hun|here|bhai)",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, msg_lower)
        if match:
            detected_name = match.group(1).strip().capitalize()
            if len(detected_name) > 2 and detected_name.lower() not in [
                "main", "mera", "meri", "tera", "teri", "bhai", "yaar",
                "kya", "kaise", "hai", "hoon", "hun", "the", "and", "not"
            ]:
                if user_profile.get("name") != detected_name:
                    user_profile["name"] = detected_name
                    if not user_profile.get("known_since"):
                        user_profile["known_since"] = datetime.utcnow().isoformat()
                    threading.Thread(target=_save_user_profile, daemon=True).start()
                break

    if any(word in msg_lower for word in ["paper trade", "paper mode", "practice"]):
        user_profile["preferences"]["mode"] = "paper"
        threading.Thread(target=_save_user_profile, daemon=True).start()
    elif any(word in msg_lower for word in ["real trade", "real mode", "live"]):
        user_profile["preferences"]["mode"] = "real"
        threading.Thread(target=_save_user_profile, daemon=True).start()

    notes = user_profile.setdefault("personal_notes", [])
    note  = None
    if re.search(r"0x[a-fA-F0-9]{40}", message):
        addrs = re.findall(r"0x[a-fA-F0-9]{40}", message)
        note  = f"Token scan kiya: {addrs[0][:12]}..."
    elif any(w in msg_lower for w in ["high risk", "zyada risk", "aggressive"]):
        note = "User high risk trading prefer karta hai"
        user_profile["preferences"]["risk"] = "high"
    elif any(w in msg_lower for w in ["low risk", "safe", "conservative", "cautious"]):
        note = "User conservative/safe trading prefer karta hai"
        user_profile["preferences"]["risk"] = "low"
    elif any(w in msg_lower for w in ["profit chahiye", "paise banana", "earn", "income"]):
        note = "User ka goal: consistent profit banana"
    elif any(w in msg_lower for w in ["loss hua", "loss ho gaya", "rugged", "scam ho gaya"]):
        note = f"User ko loss/scam hua — {message[:50]}"

    if note and note not in notes:
        notes.append(note)
        user_profile["personal_notes"] = notes[-20:]
        threading.Thread(target=_save_user_profile, daemon=True).start()

    rule_triggers = [
        "mat karo", "band karo", "stop karo", "mat karna", "band kr",
        "mat bol", "mat le", "mat liya karo", "hamesha", "kabhi mat",
        "naam mat", "name mat", "bhai mat", "baar baar mat",
        "short rakh", "chota rakh", "kam likho", "zyada mat likho",
        "sirf utna", "repeat mat", "dobara mat"
    ]
    if any(trigger in msg_lower for trigger in rule_triggers):
        rule = message.strip()[:100]
        existing_rules = user_profile.get("user_rules", [])
        if rule not in existing_rules:
            existing_rules.append(rule)
            user_profile["user_rules"] = existing_rules[-30:]
            threading.Thread(target=_save_user_profile, daemon=True).start()

def get_user_context_for_llm() -> str:
    parts = []
    if user_profile.get("name"):
        parts.append(f"USER_NAME={user_profile['name']}")
    if user_profile.get("nickname"):
        parts.append(f"CALLS_ME={user_profile['nickname']}")
    sessions_count = user_profile.get("total_sessions", 0)
    if sessions_count > 0:
        parts.append(f"SESSIONS_TOGETHER={sessions_count}")
    if user_profile.get("known_since"):
        parts.append(f"FRIENDS_SINCE={user_profile['known_since'][:10]}")
    prefs = user_profile.get("preferences", {})
    if prefs:
        parts.append(f"USER_PREFS={prefs}")
    notes = user_profile.get("personal_notes", [])
    if notes:
        parts.append(f"I_KNOW={notes[-1][:50]}")
    rules = user_profile.get("user_rules", [])
    if rules:
        rules_str = " | ".join(rules[-5:])
        parts.append(f"PERMANENT_USER_RULES={rules_str}")
    return " | ".join(parts) if parts else "NEW_USER"

# ========== SELF AWARENESS ==========
BIRTH_TIME = datetime.utcnow()

# ════════════════════════════════════════════
# ANTI-MEV PROTECTION
# BSC pe sandwich attack: MEV bot hamare buy tx
# dekh ke pehle khud buy karta hai (frontrun)
# → hamare entry price already pumped hota hai
#
# Defense strategies:
# 1. Random delay (5-30s) — predictable timing = easy target
# 2. Buy amount randomize — round numbers (0.01 BNB) = obvious bot
# 3. High slippage ± random — MEV bot estimate nahi kar pata
# 4. Multiple small buys instead of one big (future DCA)
# ════════════════════════════════════════════
import random as _random

def _anti_mev_delay():
    """Random delay so MEV bots can't predict our timing"""
    delay = _random.uniform(2.0, 8.0)  # 2-8 sec random
    print(f"🛡️ Anti-MEV delay: {delay:.1f}s")
    time.sleep(delay)

def _anti_mev_amount(base_bnb: float) -> float:
    """
    Amount randomize karo — round numbers obvious hain.
    0.01 BNB → 0.0097-0.0103 BNB (±3% noise)
    """
    noise = _random.uniform(-0.03, 0.03)  # ±3%
    jittered = round(base_bnb * (1 + noise), 5)
    return max(jittered, 0.001)  # minimum 0.001

def _anti_mev_slippage(base_slippage: float = 12.0) -> float:
    """
    Slippage randomize karo — fixed % = predictable.
    MEV bot hamare exact slippage se price estimate karta hai.
    """
    noise = _random.uniform(-2.0, 3.0)  # ±2-3% noise
    return round(base_slippage + noise, 1)

_mev_buy_count = 0  # track kitni baar anti-mev triggered

# ══════════════════════════════════════════════
# VOLUME PRESSURE CACHE — position manager ke liye
# DexScreener se har 60s fetch, rate limit se bachne ke liye
# ══════════════════════════════════════════════
_vol_pressure_cache: dict = {}   # {addr: {"buys": N, "sells": N, "ts": float}}
_vol_pressure_lock  = threading.Lock()
VOL_CACHE_TTL       = 60  # seconds — har 60s mein fresh data

def _get_vol_pressure(address: str) -> dict:
    """Buy/Sell pressure fetch karo — cached, 60s TTL, rate-limit safe"""
    now = time.time()
    with _vol_pressure_lock:
        cached = _vol_pressure_cache.get(address.lower())
        if cached and (now - cached.get("ts", 0)) < VOL_CACHE_TTL:
            return cached
    # Fresh fetch
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=6
        )
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            bsc   = [p for p in pairs if p and p.get("chainId") == "bsc"]
            if bsc:
                bsc.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
                txns   = bsc[0].get("txns", {})
                buys5  = int((txns.get("m5") or {}).get("buys",  0) or 0)
                sells5 = int((txns.get("m5") or {}).get("sells", 0) or 0)
                buys1h = int((txns.get("h1") or {}).get("buys",  0) or 0)
                sells1h= int((txns.get("h1") or {}).get("sells", 0) or 0)
                data   = {"buys5": buys5, "sells5": sells5,
                          "buys1h": buys1h, "sells1h": sells1h, "ts": now}
                with _vol_pressure_lock:
                    _vol_pressure_cache[address.lower()] = data
                    # Memory: max 30 entries
                    if len(_vol_pressure_cache) > 30:
                        oldest = sorted(_vol_pressure_cache.items(), key=lambda x: x[1].get("ts",0))
                        for k, _ in oldest[:5]:
                            _vol_pressure_cache.pop(k, None)
                return data
    except Exception:
        pass
    return {"buys5": 0, "sells5": 0, "buys1h": 0, "sells1h": 0, "ts": now}


# ══════════════════════════════════════════════
# REAL-TIME ON-CHAIN SWAP MONITOR
# Open positions ke pair contracts ke Swap events
# directly BSC WebSocket se sunna — 100ms latency
# DexScreener 60s delay ki zaroorat nahi
# ══════════════════════════════════════════════

# token_address → pair_address mapping cache
_pair_addr_cache: dict = {}  # {token_lower: pair_addr}
_pair_addr_lock  = threading.Lock()

# Real-time swap counter — position manager yahan se read karta hai
# {token_lower: {"buys5": N, "sells5": N, "buys1h": N, "sells1h": N,
#                "last_buy_ts": float, "last_sell_ts": float, "ts": float}}
_rt_swap_data: dict = {}
_rt_swap_lock = threading.Lock()

# PancakeSwap v2 Swap event topic
# keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

def _get_pair_for_token(token_address: str) -> str:
    """Token ka v2 pair address lo — cached"""
    tl = token_address.lower()
    with _pair_addr_lock:
        if tl in _pair_addr_cache:
            return _pair_addr_cache[tl]
    pair = _get_v2_pair(token_address)
    if pair:
        with _pair_addr_lock:
            _pair_addr_cache[tl] = pair.lower()
    return pair.lower() if pair else ""

def _record_swap(token_addr: str, is_buy: bool, bnb_amount: float = 0.0):
    """
    Swap event aaya → count + BNB VOLUME dono track karo.
    Count akela misleading hai — 1 whale sell = 100 small buys se dangerous.
    """
    now  = time.time()
    key  = token_addr.lower()
    with _rt_swap_lock:
        d = _rt_swap_data.get(key, {
            "buys5": 0, "sells5": 0, "buys1h": 0, "sells1h": 0,
            "buy_vol5": 0.0, "sell_vol5": 0.0,    # BNB volume last 5 min
            "buy_vol1h": 0.0, "sell_vol1h": 0.0,  # BNB volume last 1 hr
            "buy_times": [], "sell_times": [],     # [(timestamp, bnb_amt), ...]
            "ts": now
        })
        cutoff5  = now - 300
        cutoff1h = now - 3600

        # Purani entries clean karo — (ts, amt) tuples
        d["buy_times"]  = [(t, a) for t, a in d.get("buy_times",  []) if t > cutoff1h]
        d["sell_times"] = [(t, a) for t, a in d.get("sell_times", []) if t > cutoff1h]

        if is_buy:
            d["buy_times"].append((now, bnb_amount))
            d["last_buy_ts"] = now
        else:
            d["sell_times"].append((now, bnb_amount))
            d["last_sell_ts"] = now

        # Count windows
        d["buys5"]   = sum(1   for t, a in d["buy_times"]  if t > cutoff5)
        d["sells5"]  = sum(1   for t, a in d["sell_times"] if t > cutoff5)
        d["buys1h"]  = len(d["buy_times"])
        d["sells1h"] = len(d["sell_times"])

        # Volume windows (BNB)
        d["buy_vol5"]   = sum(a for t, a in d["buy_times"]  if t > cutoff5)
        d["sell_vol5"]  = sum(a for t, a in d["sell_times"] if t > cutoff5)
        d["buy_vol1h"]  = sum(a for t, a in d["buy_times"])
        d["sell_vol1h"] = sum(a for t, a in d["sell_times"])
        d["ts"]         = now

        # Memory: max 500 entries each
        d["buy_times"]  = d["buy_times"][-500:]
        d["sell_times"] = d["sell_times"][-500:]
        _rt_swap_data[key] = d

def _get_vol_pressure_rt(token_address: str) -> dict:
    """
    Real-time volume pressure — on-chain swap events se.
    Agar real-time data nahi hai (position abhi add hui) toh
    DexScreener fallback use karo.
    """
    now = time.time()
    key = token_address.lower()
    with _rt_swap_lock:
        rt = _rt_swap_data.get(key)

    # Real-time data hai aur 5 min se zyada purana nahi
    if rt and (now - rt.get("ts", 0)) < 300:
        return {
            "buys5":      rt.get("buys5",    0),
            "sells5":     rt.get("sells5",   0),
            "buys1h":     rt.get("buys1h",   0),
            "sells1h":    rt.get("sells1h",  0),
            "buy_vol5":   rt.get("buy_vol5",  0.0),   # ✅ BNB volume
            "sell_vol5":  rt.get("sell_vol5", 0.0),   # ✅ BNB volume
            "buy_vol1h":  rt.get("buy_vol1h", 0.0),
            "sell_vol1h": rt.get("sell_vol1h",0.0),
            "ts":         rt["ts"],
            "source":     "onchain"
        }
    # Fallback: DexScreener
    fallback = _get_vol_pressure(token_address)
    fallback["source"] = "dexscreener"
    return fallback


def start_swap_monitor():
    """
    Open positions ke pair contracts ke BSC Swap events real-time sunna.
    Naye position add hone pe automatically subscribe karta hai.
    """
    import asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        print("⚠️ websockets nahi — swap monitor disabled")
        return

    WSS_ENDPOINTS = [
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc.drpc.org",
    ]

    async def _swap_loop():
        idx = 0
        fail_count = 0
        while True:
            wss_url = WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)]
            try:
                async with _ws.connect(
                    wss_url,
                    ping_interval=15, ping_timeout=10,
                    close_timeout=5, max_size=2**20
                ) as ws:
                    print(f"⚡ Swap Monitor connected: {wss_url}")
                    fail_count = 0

                    # Sab monitored positions ke pair addresses subscribe karo
                    # Reverse map: pair_lower → token_lower
                    pair_to_token: dict = {}

                    # Subscribe to Swap events — pair addresses dynamically update
                    # Initial subscription with currently monitored tokens
                    async def _subscribe_current():
                        pairs = []
                        with monitor_lock:
                            tokens = list(monitored_positions.keys())
                        for tok in tokens:
                            pair = _get_pair_for_token(tok)
                            if pair:
                                pairs.append(pair)
                                pair_to_token[pair] = tok.lower()

                        if not pairs:
                            # Koi position nahi abhi, generic subscribe
                            pairs = []

                        sub_msg = _json.dumps({
                            "id": 10, "method": "eth_subscribe",
                            "params": ["logs", {
                                "topics": [[SWAP_TOPIC]]
                            }],
                            "jsonrpc": "2.0"
                        })
                        await ws.send(sub_msg)
                        await asyncio.wait_for(ws.recv(), timeout=10)
                        print(f"⚡ Swap Monitor: subscribed (watching all pairs)")

                    await _subscribe_current()

                    # Reverse map refresh loop
                    last_map_refresh = 0

                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = _json.loads(msg)
                        log  = (data.get("params") or {}).get("result") or {}
                        if not log: continue

                        topics   = log.get("topics") or []
                        if not topics or topics[0].lower() != SWAP_TOPIC.lower():
                            continue

                        pair_addr = log.get("address", "").lower()

                        # Refresh pair→token map every 30s
                        now_t = time.time()
                        if now_t - last_map_refresh > 30:
                            with monitor_lock:
                                tokens = list(monitored_positions.keys())
                            for tok in tokens:
                                pair = _get_pair_for_token(tok)
                                if pair:
                                    pair_to_token[pair.lower()] = tok.lower()
                            # Remove pairs for closed positions
                            active_tokens = set(t.lower() for t in tokens)
                            pair_to_token = {
                                p: t for p, t in pair_to_token.items()
                                if t in active_tokens
                            }
                            last_map_refresh = now_t

                        # Is pair hamare kisi token ka hai?
                        token_addr = pair_to_token.get(pair_addr)
                        if not token_addr:
                            continue

                        # Decode Swap event: amount0In, amount1In, amount0Out, amount1Out
                        # data = 4 × uint256 = 256 bytes
                        raw = log.get("data", "0x")
                        if len(raw) < 130:
                            continue
                        raw_hex = raw[2:]  # remove 0x
                        try:
                            a0in  = int(raw_hex[0:64],   16)
                            a1in  = int(raw_hex[64:128],  16)
                        except Exception:
                            continue

                        # token0 = WBNB ya token?
                        # Agar pair mein token0 = WBNB:
                        #   BUY  = amount1In > 0 (BNB in, token out)
                        #   SELL = amount0In > 0 (token in, BNB out)  
                        # Agar token0 = token:
                        #   BUY  = amount0In > 0
                        #   SELL = amount1In > 0

                        # Simpler heuristic: whichever side has input = that side
                        # We'll use WBNB check via cache
                        pair_lower = pair_addr
                        _tok_is_t0 = _pair_addr_cache.get("_t0_" + token_addr, None)
                        if _tok_is_t0 is None:
                            try:
                                pc = w3.eth.contract(
                                    address=Web3.to_checksum_address(pair_addr),
                                    abi=PAIR_ABI_PRICE
                                )
                                t0 = pc.functions.token0().call().lower()
                                _tok_is_t0 = (t0 == token_addr)
                                _pair_addr_cache["_t0_" + token_addr] = _tok_is_t0
                            except Exception:
                                _tok_is_t0 = False

                        # Full decode: all 4 amounts for BNB volume
                        try:
                            a0out = int(raw_hex[128:192], 16)
                            a1out = int(raw_hex[192:256], 16)
                        except Exception:
                            a0out = a1out = 0

                        if _tok_is_t0:
                            # token=token0, WBNB=token1
                            # BUY:  BNB in (a1in)   → a1in = BNB spent
                            # SELL: BNB out (a1out)  → a1out = BNB received
                            is_buy    = a1in > 0
                            bnb_wei   = a1in if is_buy else a1out
                        else:
                            # token=token1, WBNB=token0
                            # BUY:  BNB in (a0in)   → a0in = BNB spent
                            # SELL: BNB out (a0out)  → a0out = BNB received
                            is_buy    = a0in > 0
                            bnb_wei   = a0in if is_buy else a0out

                        bnb_amt = bnb_wei / 1e18  # wei → BNB
                        _record_swap(token_addr, is_buy, bnb_amt)

                        # Log significant swaps (> 0.1 BNB)
                        if bnb_amt >= 0.1:
                            _dir = "🟢BUY " if is_buy else "🔴SELL"
                            print(f"⚡ {_dir} {token_addr[:10]} {bnb_amt:.3f} BNB")

            except Exception as e:
                fail_count += 1
                wait = min(10 * fail_count, 60)
                err  = str(e).lower()
                if "1013" in err or "timeout" in err:
                    print(f"⚠️ Swap Monitor: reconnecting (#{fail_count})")
                else:
                    print(f"⚠️ Swap Monitor error: {str(e)[:60]}")
                await asyncio.sleep(wait)
                if fail_count % 5 == 0:
                    import gc; gc.collect()
                idx += 1

    def _run_swap_monitor():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_swap_loop())
        except Exception as ex:
            print(f"⚠️ Swap Monitor thread: {ex}")
        finally:
            loop.close()

    threading.Thread(target=_run_swap_monitor, daemon=True).start()
    print("⚡ Real-time Swap Monitor starting...")

# ══════════════════════════════════════════════
# SELF-LEARNING WHALE DETECTOR
# Bot khud on-chain dekh ke profitable wallets identify karta hai
# Manually add karne ki zaroorat nahi — bot sikhta jaata hai
#
# Logic:
#  1. Token buy kiya → entry time note karo
#  2. TP hit → "kaun the early buyers?" → BSC se fetch
#  3. Un wallets ko "smart" mark karo
#  4. Agli baar wo wallet naye token mein ho → GREEN SIGNAL
# ══════════════════════════════════════════════

_smart_wallets: dict = {}   # {wallet_lower: {"wins": int, "losses": int, "total_pnl": float, "first_seen": iso, "last_seen": iso}}
_smart_wallets_lock = threading.Lock()

# Wallet qualify hone ke liye minimum threshold
WHALE_MIN_WINS      = 2       # kam se kam 2 profitable trades
WHALE_MIN_WIN_RATE  = 0.60    # 60%+ win rate
WHALE_MIN_BNB_TXN   = 0.05    # minimum 0.05 BNB per transaction (noise filter)
WHALE_MAX_WALLETS   = 200     # memory cap

def _update_whale_stats(wallet: str, win: bool, pnl_pct: float):
    """Wallet ka track record update karo"""
    if not wallet or len(wallet) != 42: return
    w = wallet.lower()
    # Skip zero/dead addresses
    if w in ("0x0000000000000000000000000000000000000000",
             "0x000000000000000000000000000000000000dead"): return
    now = datetime.utcnow().isoformat()
    with _smart_wallets_lock:
        d = _smart_wallets.get(w, {"wins": 0, "losses": 0, "total_pnl": 0.0,
                                   "first_seen": now, "last_seen": now, "qualified": False})
        if win:
            d["wins"]      = d.get("wins", 0) + 1
        else:
            d["losses"]    = d.get("losses", 0) + 1
        d["total_pnl"]     = round(d.get("total_pnl", 0.0) + pnl_pct, 2)
        d["last_seen"]     = now
        # Check qualification
        total = d["wins"] + d["losses"]
        win_rate = d["wins"] / max(total, 1)
        d["qualified"] = (d["wins"] >= WHALE_MIN_WINS and win_rate >= WHALE_MIN_WIN_RATE)
        # Memory cap — remove worst performers first
        if len(_smart_wallets) >= WHALE_MAX_WALLETS:
            worst = sorted(_smart_wallets.items(),
                           key=lambda x: x[1].get("wins",0) - x[1].get("losses",0))
            for wk, _ in worst[:10]:
                _smart_wallets.pop(wk, None)
        _smart_wallets[w] = d

def is_smart_wallet(wallet: str) -> bool:
    """Wallet qualified hai? (enough wins + win rate)"""
    if not wallet or len(wallet) != 42: return False
    d = _smart_wallets.get(wallet.lower(), {})
    return bool(d.get("qualified", False))

def get_smart_wallet_label(wallet: str) -> str:
    d = _smart_wallets.get(wallet.lower(), {})
    if not d: return ""
    return f"W:{d.get('wins',0)} L:{d.get('losses',0)} PnL:{d.get('total_pnl',0):+.0f}%"


def _fetch_early_buyers(token_address: str, entry_ts: float, max_buyers: int = 20) -> list:
    """
    Token ke early buyers fetch karo — BSC on-chain se.
    GoPlus holders list use karta hai (already fetched, no extra API call).
    Plus real-time swap monitor data se bhi.
    Returns: [wallet_address, ...]
    """
    buyers = set()
    try:
        # Source 1: GoPlus holders (already in memory se)
        gp_res = requests.get(
            "https://api.gopluslabs.io/api/v1/token_security/56",
            params={"contract_addresses": token_address}, timeout=8
        )
        if gp_res.status_code == 200:
            gp = gp_res.json().get("result", {}).get(token_address.lower(), {})
            holders = gp.get("holders", [])
            for h in (holders or [])[:max_buyers]:
                addr = h.get("address", "")
                pct  = float(h.get("percent", 0) or 0)
                # Skip: dead wallets, contracts, tiny holders
                if (addr and len(addr) == 42
                        and addr.lower() not in ("0x0000000000000000000000000000000000000000",
                                                  "0x000000000000000000000000000000000000dead")
                        and pct > 0.001):
                    buyers.add(addr.lower())
    except Exception as e:
        print(f"⚠️ early_buyers GoPlus: {e}")

    try:
        # Source 2: Real-time swap monitor — kaun buy kar raha tha?
        # _rt_swap_data mein buy_times aur sell_times hain
        with _rt_swap_lock:
            sd = _rt_swap_data.get(token_address.lower(), {})
        # Hum buy timestamps jaante hain par addresses nahi (WSS mein sender nahi hota easily)
        # Isliye GoPlus holders hi main source hai
        pass
    except Exception: pass

    return list(buyers)[:max_buyers]


def _learn_from_trade(token_address: str, win: bool, pnl_pct: float, entry_ts: float):
    """
    Trade complete → early buyers ko reward/penalize karo.
    Background mein chalta hai — main thread block nahi karta.
    """
    try:
        buyers = _fetch_early_buyers(token_address, entry_ts)
        if not buyers:
            return
        for wallet in buyers:
            _update_whale_stats(wallet, win, pnl_pct)
        qualified = sum(1 for w in buyers if is_smart_wallet(w))
        status = "WIN ✅" if win else "LOSS ❌"
        print(f"🧠 Learned: {token_address[:10]} {status} {pnl_pct:+.0f}% | "
              f"tracked {len(buyers)} wallets | {qualified} now qualified")
    except Exception as e:
        print(f"⚠️ _learn_from_trade: {e}")


# ══════════════════════════════════════════════
# GREEN SIGNAL DETECTOR
# Token ke liye multiple positive signals check karo
# Score jitna zyada = stronger conviction = zyada size buy
# ══════════════════════════════════════════════
def detect_green_signals(token_address: str, goplus_data: dict, dex_data: dict) -> dict:
    """
    Multiple green signals detect karo — har signal pe bot confidence badhata hai.
    Returns: {signals: [...], score: N, size_multiplier: float}
    """
    signals = []
    score   = 0

    bnb_price = market_cache.get("bnb_price", 600)

    # ── SIGNAL 1: Large Buy Detected (Whale Entry) ──
    # Real-time swap monitor se — last 5 min mein koi bada buy aaya?
    _vol = _get_vol_pressure_rt(token_address)
    buy_vol5  = _vol.get("buy_vol5",  0.0)
    sell_vol5 = _vol.get("sell_vol5", 0.0)

    if buy_vol5 >= 1.0:       # 1+ BNB bought in 5 min = whale entry
        signals.append({"type": "WHALE_BUY", "detail": f"{buy_vol5:.2f} BNB bought (5m)", "weight": 3})
        score += 3
    elif buy_vol5 >= 0.3:     # 0.3+ BNB = significant buy
        signals.append({"type": "LARGE_BUY", "detail": f"{buy_vol5:.2f} BNB bought (5m)", "weight": 2})
        score += 2

    # ── SIGNAL 2: Buy Pressure Dominant (Volume-based) ──
    if buy_vol5 > 0 or sell_vol5 > 0:
        vol_ratio = buy_vol5 / max(sell_vol5, 0.001)
        if vol_ratio >= 3.0:
            signals.append({"type": "STRONG_BUY_PRESSURE", "detail": f"BuyVol {vol_ratio:.1f}x SellVol", "weight": 2})
            score += 2
        elif vol_ratio >= 1.5:
            signals.append({"type": "BUY_PRESSURE", "detail": f"BuyVol {vol_ratio:.1f}x SellVol", "weight": 1})
            score += 1

    # ── SIGNAL 3: Smart Money Wallet Detected ──
    # GoPlus holders mein koi known profitable wallet hai?
    holders = goplus_data.get("holders", [])
    creator = goplus_data.get("creator_address", "")
    sm_found = []
    for h in (holders or [])[:20]:
        addr_h = h.get("address", "")
        if is_smart_wallet(addr_h):
            sm_found.append(get_smart_wallet_label(addr_h))
    if is_smart_wallet(creator):
        sm_found.append(f"creator:{get_smart_wallet_label(creator)}")
        score += 2  # Creator khud profitable hai = extra conviction
    if sm_found:
        # Kitne qualified wallets hain aur unka combined win rate
        _sm_details = []
        for h in (holders or [])[:20]:
            a = h.get("address","")
            if is_smart_wallet(a):
                _sm_details.append(get_smart_wallet_label(a))
        detail_str = " | ".join(_sm_details[:2]) if _sm_details else f"{len(sm_found)} wallets"
        signals.append({"type": "SMART_MONEY", "detail": f"🧠 {detail_str}", "weight": 3})
        score += min(3 * len(sm_found), 6)  # multiple smart wallets = more points (max 6)

    # ── SIGNAL 4: Liquidity Growing Fast ──
    # four.meme graduation approaching = maximum momentum window
    liq_usd = dex_data.get("liquidity_usd", 0)
    fdv     = dex_data.get("fdv", 0)
    if 40_000 <= liq_usd < 69_000:
        signals.append({"type": "NEAR_GRADUATION", "detail": f"Liq ${liq_usd:,.0f} → graduation $69k", "weight": 3})
        score += 3
    elif 20_000 <= liq_usd < 40_000:
        signals.append({"type": "LIQ_BUILDING", "detail": f"Liq ${liq_usd:,.0f} growing", "weight": 1})
        score += 1

    # ── SIGNAL 5: Fresh Token Sweet Spot ──
    # 5-30 min old = best entry window (snipers nikal chuke, organic buyers aa rahe hain)
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=6)
        if r.status_code == 200:
            bsc_p = [p for p in (r.json() or {}).get("pairs", []) or [] if p and p.get("chainId") == "bsc"]
            if bsc_p:
                age_min = (time.time() - (bsc_p[0].get("pairCreatedAt", 0) or 0) / 1000) / 60
                if 5 <= age_min <= 30:
                    signals.append({"type": "SWEET_SPOT_AGE", "detail": f"{age_min:.0f} min old (5-30 optimal)", "weight": 2})
                    score += 2
                elif 30 < age_min <= 60:
                    signals.append({"type": "GOOD_AGE", "detail": f"{age_min:.0f} min old", "weight": 1})
                    score += 1
    except Exception: pass

    # ── SIGNAL 6: MCap Sweet Spot ──
    # $10k-$100k mcap = early but real (not zero liquidity rug)
    if 10_000 < fdv <= 100_000:
        signals.append({"type": "MCAP_SWEET_SPOT", "detail": f"MCap ${fdv:,.0f} (early entry)", "weight": 2})
        score += 2

    # ── SIGNAL 7: Tx Velocity ──
    # Last 5 min mein 10+ transactions = high activity
    buys5  = _vol.get("buys5",  0)
    sells5 = _vol.get("sells5", 0)
    txns5  = buys5 + sells5
    if txns5 >= 20:
        signals.append({"type": "HIGH_VELOCITY", "detail": f"{txns5} txns in 5min", "weight": 2})
        score += 2
    elif txns5 >= 10:
        signals.append({"type": "ACTIVE", "detail": f"{txns5} txns in 5min", "weight": 1})
        score += 1

    # ── Size Multiplier based on signal score ──
    # Zyada green signals = zyada conviction = zyada size
    if score >= 8:
        size_mult = 2.0    # double size — very strong signals
    elif score >= 5:
        size_mult = 1.5    # 1.5x size
    elif score >= 3:
        size_mult = 1.25   # 1.25x size
    else:
        size_mult = 1.0    # normal size

    if signals:
        sigs_str = " | ".join(s["type"] for s in signals)
        print(f"🟢 GREEN SIGNALS [{score}pt] {token_address[:10]}: {sigs_str}")

    return {
        "signals":         signals,
        "score":           score,
        "size_multiplier": size_mult,
    }


# ══════════════════════════════════════════════
# DEV WALLET BLACKLIST — ruggers track karo
# ══════════════════════════════════════════════
_dev_blacklist: dict = {}   # {wallet_lower: {"reason": str, "rugs": int, "last_seen": iso}}
_dev_blacklist_lock = threading.Lock()

def blacklist_dev(wallet: str, reason: str = "rug"):
    """Dev wallet ko blacklist karo — future tokens automatically skip honge"""
    if not wallet or len(wallet) != 42: return
    w = wallet.lower()
    with _dev_blacklist_lock:
        existing = _dev_blacklist.get(w, {"rugs": 0})
        _dev_blacklist[w] = {
            "reason":    reason,
            "rugs":      existing.get("rugs", 0) + 1,
            "last_seen": datetime.utcnow().isoformat()
        }
    print(f"🚫 Dev blacklisted: {wallet[:10]}... reason={reason}")

def is_dev_blacklisted(wallet: str) -> bool:
    if not wallet or len(wallet) != 42: return False
    return wallet.lower() in _dev_blacklist

# _perf_tracker and _relationship removed — RAM optimization

# RAM-optimized self_awareness — only functional fields kept
self_awareness = {
    "identity": {
        "name":       "MrBlack",
        "version":    "4.0",
        "born_at":    BIRTH_TIME.isoformat(),
        "deployment": os.getenv("RENDER_SERVICE_NAME", "local"),
    },
    "performance_intelligence": {
        "overall_accuracy": 0.0,
        "trading_iq":       50,
    },
    "cognitive_state": {
        "mood":            "FOCUSED",
        "active_warnings": [],
    },
    "current_state": {
        "status":        "ONLINE",
        "uptime_seconds": 0,
        "errors_today":  0,
    },
    "growth_tracking": {
        "milestones": [],
    },
    "introspection_log": [],
}

# ========== BRAIN (early init needed) ==========
brain: Dict = {
    "trading": {
        "best_patterns":    [],
        "avoid_patterns":   [],
        "market_insights":  [],
        "token_blacklist":  [],
        "token_whitelist":  [],
        "strategy_notes":   [],
        "last_updated":     None
    },
    "airdrop": {
        "active_projects":  [],
        "completed":        [],
        "success_patterns": [],
        "fail_patterns":    [],
        "wallet_notes":     [],
        "last_updated":     None
    },
    "coding": {
        "solutions_library": [],
        "common_errors":     [],
        "useful_snippets":   [],
        "deployment_notes":  [],
        "last_updated":      None
    },
    "total_learning_cycles": 0,
    "total_tokens_discovered_ever": 0,
    "started_at": datetime.utcnow().isoformat(),
    "user_interaction_patterns": {
        "trading_questions": 0,
        "airdrop_questions": 0,
        "coding_questions":  0,
        "general_chat":      0,
    },
    "user_pain_points": [],
}

# ========== SESSIONS ==========
sessions: Dict[str, dict] = {}

def get_or_create_session(session_id: str) -> dict:
    # Session cleanup — sirf AUTO_TRADER aur default rakhte hain, baki sab delete
    if len(sessions) > 3 and session_id not in sessions:
        _keep = {"AUTO_TRADER", "default", session_id}
        for k in [k for k in list(sessions.keys()) if k not in _keep]:
            sessions.pop(k, None)
    if session_id not in sessions:
        sessions[session_id] = {
            "session_id":       session_id,
            "mode":             "paper",
            "paper_balance":    5.0,
            "real_balance":     0.00,
            "positions":        [],
            "history":          [],
            "pnl_24h":          0.0,
            "daily_loss":       0.0,
            "trade_count":      0,
            "win_count":        0,
            "pattern_database": [],
            "created_at":       datetime.utcnow().isoformat(),
            "daily_loss_date":  datetime.utcnow().strftime("%Y-%m-%d")
        }
        _load_session_from_db(session_id)
    return sessions[session_id]


def _load_session_from_db(session_id: str):
    if not supabase: return
    try:
        res = supabase.table("memory").select("*").eq("session_id", session_id).execute()
        if res.data:
            row = res.data[0]
            def _safe_json(val, default):
                if not val: return default
                try: return json.loads(val)
                except: return default
            # open_positions restore karo
            _op = _safe_json(row.get("open_positions"), {})
            if _op and isinstance(_op, dict):
                sessions[session_id]["open_positions"] = _op
            _loaded_daily_loss = float(row.get("daily_loss") or 0.0)
            _saved_date = str(row.get("updated_at") or "")[:10]
            _today = datetime.utcnow().strftime("%Y-%m-%d")
            if _saved_date != _today:
                print(f"🔄 Session load: resetting stale daily_loss={_loaded_daily_loss:.2f} (saved {_saved_date})")
                _loaded_daily_loss = 0.0
            sessions[session_id].update({
                "paper_balance":    float(row.get("paper_balance") or 5.0),
                "real_balance":     float(row.get("real_balance")  or 0.00),
                "positions":        [x for x in _safe_json(row.get("positions"), []) if isinstance(x, dict)],
                "history":          _safe_json(row.get("history"),          []),
                "pnl_24h":          float(row.get("pnl_24h")       or 0.0),
                "daily_loss":       _loaded_daily_loss,
                "daily_loss_date":  _today,
                "trade_count":      int(row.get("trade_count")      or 0),
                "win_count":        int(row.get("win_count")        or 0),
                "pattern_database": _safe_json(row.get("pattern_database"), {}),
            })
            # Restore auto_trade_stats if this is AUTO session
            if session_id == AUTO_SESSION_ID:
                raw = _safe_json(row.get("pattern_database"), {})
                if isinstance(raw, dict):
                    auto_trade_stats["total_auto_buys"]  = raw.get("total_buys", 0)
                    auto_trade_stats["total_auto_sells"] = raw.get("total_sells", 0)
                    auto_trade_stats["auto_pnl_total"]   = raw.get("pnl_total", 0.0)
                    auto_trade_stats["last_action"]      = raw.get("last_action", "")
                    auto_trade_stats["trade_history"]    = raw.get("trade_history", [])
                    # total_scanned restore
                    _sc = raw.get("total_scanned", 0)
                    if _sc > 0 and _sc > len(discovered_addresses):
                        brain["total_tokens_discovered_ever"] = _sc
                    # wins/losses restore
                    auto_trade_stats["wins"]         = raw.get("wins", 0)
                    auto_trade_stats["losses"]       = raw.get("losses", 0)
                    # Restore today stats — only if same day
                    _saved_today = raw.get("today_date", "")
                    _cur_today   = datetime.utcnow().strftime("%Y-%m-%d")
                    if _saved_today == _cur_today:
                        auto_trade_stats["today_wins"]   = raw.get("today_wins",   0)
                        auto_trade_stats["today_losses"] = raw.get("today_losses", 0)
                        auto_trade_stats["today_pnl"]    = raw.get("today_pnl",    0.0)
                        auto_trade_stats["today_date"]   = _cur_today
                    print(f"✅ Auto stats restored: buys={auto_trade_stats['total_auto_buys']} sells={auto_trade_stats['total_auto_sells']} wins={auto_trade_stats['wins']} losses={auto_trade_stats['losses']} history={len(auto_trade_stats['trade_history'])} scanned={_sc}")
            print(f"✅ Session loaded: {session_id[:8]}... Balance:{sessions[session_id]['paper_balance']:.3f}BNB")
    except Exception as e:
        print(f"⚠️ Session load error: {e}")

def _save_session_to_db(session_id: str):
    if not supabase: return
    try:
        sess = sessions.get(session_id, {})
        extra = {}
        if session_id == AUTO_SESSION_ID:
            extra["pattern_database"] = {
                "total_buys":    auto_trade_stats.get("total_auto_buys", 0),
                "total_sells":   auto_trade_stats.get("total_auto_sells", 0),
                "pnl_total":     auto_trade_stats.get("auto_pnl_total", 0.0),
                "last_action":   auto_trade_stats.get("last_action", ""),
                "trade_history": list(auto_trade_stats.get("trade_history", []))[-100:],
                "total_scanned": max(len(discovered_addresses), brain.get("total_tokens_discovered_ever", 0)),
                "wins":          auto_trade_stats.get("wins", 0),
                "losses":        auto_trade_stats.get("losses", 0),
                "today_wins":    auto_trade_stats.get("today_wins",   0),
                "today_losses":  auto_trade_stats.get("today_losses", 0),
                "today_pnl":     auto_trade_stats.get("today_pnl",    0.0),
                "today_date":    auto_trade_stats.get("today_date",   ""),
            }
        else:
            extra["pattern_database"] = sess.get("pattern_database", [])
        supabase.table("memory").upsert({
            "session_id":       session_id,
            "role":             "user",
            "content":          "",
            "paper_balance":    sess.get("paper_balance",    5.0),
            "open_positions":   json.dumps(sess.get("open_positions", {})),
            "real_balance":     sess.get("real_balance",     0.00),
            "positions":        json.dumps(sess.get("positions",        [])),
            "history":          json.dumps(sess.get("history",          [])[-20:]),
            "pnl_24h":          sess.get("pnl_24h",          0.0),
            "daily_loss":       sess.get("daily_loss",        0.0),
            "trade_count":      sess.get("trade_count",       0),
            "win_count":        sess.get("win_count",         0),
            **extra,
            "updated_at":       datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"⚠️ Session save error: {e}")

def _persist_positions():
    """
    running_positions ka FULL snapshot Supabase mein save karo.
    Restart ke baad tp_sold, sl_pct, size_bnb, bought_usd sab restore ho.
    Har buy/sell ke baad yahi call karo.
    """
    try:
        _ss = get_or_create_session(AUTO_SESSION_ID)
        _ss["open_positions"] = {
            k: {
                "token":          v.get("token", ""),
                "entry":          v.get("entry", 0),
                "size_bnb":       v.get("size_bnb", AUTO_BUY_SIZE_BNB),
                "orig_size_bnb":  v.get("orig_size_bnb", v.get("size_bnb", AUTO_BUY_SIZE_BNB)),
                "bought_usd":     v.get("bought_usd", 0.0),
                "bought_at":      v.get("bought_at", ""),
                "sl_pct":         v.get("sl_pct", 15.0),
                "tp_sold":        v.get("tp_sold", 0.0),
                "banked_pnl_bnb": v.get("banked_pnl_bnb", 0.0),  # ✅ partial sell profits
            }
            for k, v in auto_trade_stats["running_positions"].items()
        }
        sessions[AUTO_SESSION_ID] = _ss
        threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
        print(f"💾 Positions persisted: {len(_ss['open_positions'])} positions saved to DB")
    except Exception as _pe:
        print(f"⚠️ _persist_positions error: {_pe}")


# ========== NEW PAIRS ==========
new_pairs_queue: deque = deque(maxlen=30)
discovered_addresses: dict = {}
_discovered_lock  = threading.Lock()          # RACE FIX: protect discovered_addresses
_token_semaphore  = threading.Semaphore(2)   # MEM FIX: was 4
_check_semaphore  = threading.Semaphore(3)   # MEM FIX: was 10
DISCOVERY_TTL = 7200
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# ========== MONITORED POSITIONS ==========
monitored_positions: Dict[str, dict] = {}
monitor_lock = threading.Lock()

# ========== AUTO TRADE STATS ==========  FIX 2: trade_history added
AUTO_TRADE_ENABLED = True
TRADE_MODE         = "paper"   # "paper" or "real"
REAL_WALLET        = ""        # user wallet address

# Checklist thresholds — user can edit from UI
CHECKLIST_SETTINGS = {
    "min_liq_bnb":       2.0,    # Stage 1: Min liquidity BNB
    "min_liq_locked":   80.0,    # Stage 1: Min liquidity locked %
    "max_buy_tax":       8.0,    # Stage 1: Max buy tax %
    "max_sell_tax":      8.0,    # Stage 1: Max sell tax %
    "max_top_holder":    7.0,    # Stage 1: Max top holder %
    "max_top10":        40.0,    # Stage 1: Max top10 holders %
    "max_creator_pct":   5.0,    # Stage 7: Max dev/creator wallet %
    "max_owner_pct":     5.0,    # Stage 7: Max owner wallet %
    "max_whale_top10":  45.0,    # Stage 7: Max whale concentration %
    "min_lp_lock":      80.0,    # Stage 8: Min LP lock %
    "min_token_age":     3.0,    # Stage 3: Min token age (min)
    "sniper_wait":       5.0,    # Stage 3: Sniper pump over (min)
    "min_volume_24h":  1000.0,   # Stage 4: Min 24h volume USD
    "sl_new":           15.0,    # Stage 10: SL % for new tokens
    "sl_hyped":         20.0,    # Stage 10: SL % for hyped tokens
    "sl_mature":        10.0,    # Stage 10: SL % for mature tokens
    "score_safe":       50.0,    # Auto buy: SAFE min score % (raised from 40)
    "score_caution":    50.0,    # Auto buy: CAUTION min score % (CAUTION buys disabled)
    "daily_loss_pct":   15.0,    # Max daily loss % of balance
    "tp1_pct":          30.0,    # Stage 11: TP1 — sell 25%
    "tp2_pct":          50.0,    # Stage 11: TP2 — sell 25%
    "tp3_pct":         100.0,    # Stage 11: TP3 — sell 25%
    "tp4_pct":         200.0,    # Stage 11: TP4 — keep 10%
}
AUTO_BUY_SIZE_BNB  = 0.01
AUTO_MAX_POSITIONS = 15  # MEM FIX: was 50
AUTO_SESSION_ID    = "AUTO_TRADER"

auto_trade_stats = {
    "total_auto_buys":   0,
    "total_auto_sells":  0,
    "auto_pnl_total":    0.0,
    "running_positions": {},
    "last_action":       "",
    "trade_history":     [],
    "wins":              0,
    "losses":            0,
    "today_wins":        0,
    "today_losses":      0,
    "today_pnl":         0.0,
    "today_date":        "",
}

# Telegram removed


# ========== PROCESS NEW TOKEN ==========
def _process_new_token(token_address: str, pair_address: str, source: str = "websocket"):
    global discovered_addresses
    _now = time.time()
    with _discovered_lock:
        if _now - discovered_addresses.get(token_address, 0) <= DISCOVERY_TTL:
            return
        # RAM CAP cleanup
        if len(discovered_addresses) > 150:
            cutoff = _now - DISCOVERY_TTL
            for k in [k for k, v in list(discovered_addresses.items()) if v < cutoff][:100]:
                del discovered_addresses[k]
    if not _token_semaphore.acquire(blocking=False):
        return  # Max threads already running, skip
    if any(token_address.lower() == str(q).lower() for q in list(new_pairs_queue)):
        _token_semaphore.release()  # BUG FIX: semaphore leak — release before return
        return
    try:
        token_address = Web3.to_checksum_address(token_address)
    except Exception:
        _token_semaphore.release()  # BUG FIX: semaphore leak — release before return
        return
    with _discovered_lock:
        discovered_addresses[token_address] = _now
    brain["total_tokens_discovered_ever"] += 1

    token_name = "Unknown"
    token_symbol = token_address[:6]
    liquidity = 0
    volume_24h = 0

    try:
        nr = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=6)
        if nr.status_code == 200:
            _nj   = nr.json() or {}
            _nraw = _nj.get("pairs") or []
            if not isinstance(_nraw, list): _nraw = []
            bsc_p = [p for p in _nraw if p and p.get("chainId") == "bsc"]
            if bsc_p:
                bsc_p.sort(key=lambda x: float(((x.get("liquidity") or {}).get("usd") or 0)), reverse=True)
                bt = bsc_p[0].get("baseToken") or {}
                token_name   = bt.get("name",   token_name)   or token_name
                token_symbol = bt.get("symbol", token_symbol) or token_symbol
                liquidity    = float(((bsc_p[0].get("liquidity") or {}).get("usd") or 0))
                volume_24h   = float(((bsc_p[0].get("volume")    or {}).get("h24") or 0))
    except Exception:
        pass

    new_pairs_queue.append({
        "address":    token_address,
        "name":       token_name,
        "symbol":     token_symbol,
        "discovered": datetime.utcnow().isoformat(),
        "liquidity":  liquidity,
        "volume_24h": volume_24h,
        "source":     source,
    })
    print(f"🆕 [{source}] {token_symbol} | {token_name} ({token_address[:10]})")
    _token_semaphore.release()
    # MEM FIX: max 3 concurrent checkers
    if not hasattr(_process_new_token, "_sem"):
        _process_new_token._sem = threading.Semaphore(3)
    def _run_check():
        if not _process_new_token._sem.acquire(blocking=False): return
        try: _auto_check_new_pair(token_address)
        finally: _process_new_token._sem.release()
    threading.Thread(target=_run_check, daemon=True).start()

# ========== POSITION MONITOR ==========
def add_position_to_monitor(session_id, token_address, token_name, entry_price, size_bnb, stop_loss_pct=15.0):
    with monitor_lock:
        if entry_price <= 0:
            print(f"❌ Monitoring BLOCKED: price=0 for {token_address[:10]}")
            return
        if entry_price > 1.0:
            print(f"❌ Monitoring BLOCKED: price too high={entry_price:.6f} for {token_address[:10]}")
            return
        if len(monitored_positions) >= 15 and token_address not in monitored_positions:
            print(f"⚠️ Monitor cap (15) reached — skipping {token_address[:10]}")
            return
        monitored_positions[token_address] = {
            "session_id":    session_id,
            "token":         token_name,
            "address":       token_address,
            "entry":         entry_price,
            "current":       entry_price,
            "high":          entry_price,
            "size_bnb":      size_bnb,
            "stop_loss_pct": stop_loss_pct,
            "alerts_sent":   [],
            "added_at":      datetime.utcnow().isoformat()
        }
    print(f"👁️ Monitoring: {token_name} @ {entry_price:.8f} BNB")

def remove_position_from_monitor(token_address: str):
    with monitor_lock:
        if token_address in monitored_positions:
            del monitored_positions[token_address]
    print(f"✅ Stopped monitoring: {token_address}")

# ========== AUTO PAPER BUY ==========
def _auto_paper_buy(address, token_name, score, total, checklist_result):
    if not AUTO_TRADE_ENABLED:
        print(f"⏸️ Auto-buy DISABLED")
        return
    sess = get_or_create_session(AUTO_SESSION_ID)

    # Daily loss reset — naye din pe ya stale value pe
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if sess.get("daily_loss_date", "") != today or sess.get("daily_loss", 0) > 10:
        print(f"🔄 Resetting daily_loss (was {sess.get('daily_loss',0):.4f} BNB)")
        sess["daily_loss"] = 0.0
        sess["daily_loss_date"] = today
    _balance = sess.get("paper_balance", 5.0) or 5.0
    _daily_limit = _balance * (CHECKLIST_SETTINGS.get("daily_loss_pct", 15.0) / 100)
    if sess.get("daily_loss", 0) >= _daily_limit:
        print(f"🛑 Auto-buy BLOCKED: daily_loss={sess.get('daily_loss',0):.4f} BNB >= {_daily_limit:.4f} BNB (15% of {_balance:.3f})")
        return
    if len(auto_trade_stats["running_positions"]) >= AUTO_MAX_POSITIONS:
        print(f"🛑 Auto-buy BLOCKED: max {AUTO_MAX_POSITIONS} positions reached")
        return
    if address in auto_trade_stats["running_positions"]:
        print(f"🛑 Auto-buy BLOCKED: {address[:10]} already open")
        return
    paper_balance = sess.get("paper_balance", 5.0)
    if paper_balance < AUTO_BUY_SIZE_BNB:
        print(f"🛑 Auto-buy BLOCKED: balance={paper_balance:.4f} too low")
        return

    # ✅ DataGuard — strict real data check before any trade
    _dg_ok, _dg_msg = DataGuard.bnb_price_ok()
    if not _dg_ok:
        print(f"🛑 Auto-buy BLOCKED: {_dg_msg}")
        return

    # Step 1: DexScreener price use karo (checklist mein already fetch hua)
    dex   = checklist_result.get("dex_data", {})
    bnb_p = market_cache.get("bnb_price", 0)  # real price only — no fallback
    entry_price = float(dex.get("price_bnb", 0) or 0)
    if entry_price <= 0 and float(dex.get("price_usd", 0) or 0) > 0 and bnb_p > 0:
        entry_price = dex["price_usd"] / bnb_p

    # Step 2: On-chain fallback
    if entry_price <= 0:
        entry_price = get_token_price_bnb(address)

    # Step 3: Fresh DexScreener call (last resort — FIXED: was unreachable before)
    if entry_price <= 0:
        time.sleep(5)
        try:
            _r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=10)
            if _r.status_code == 200:
                _bsc = [p for p in (_r.json() or {}).get("pairs", []) or [] if p and p.get("chainId") == "bsc"]
                if _bsc:
                    _pusd = float(_bsc[0].get("priceUsd", 0) or 0)
                    if _pusd > 0:
                        entry_price = _pusd / bnb_p
        except Exception as _pe:
            print(f"⚠️ Price fallback error: {_pe}")

    # FIX: Price still 0 — ek aur retry 10s baad
    if entry_price <= 0:
        print(f"⏳ Price=0 — 10s wait kar ke retry kar raha hoon: {address[:10]}")
        import time as _rt
        _rt.sleep(10)
        entry_price = get_token_price_bnb(address)
        if entry_price <= 0:
            try:
                _r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=12)
                if _r2.status_code == 200:
                    _bsc2 = [p for p in (_r2.json() or {}).get("pairs", []) or [] if p and p.get("chainId") == "bsc"]
                    if _bsc2:
                        _pusd2 = float(_bsc2[0].get("priceUsd", 0) or 0)
                        bnb_p2 = market_cache.get("bnb_price", 0)
                        if _pusd2 > 0:
                            entry_price = _pusd2 / bnb_p2
            except Exception as _re:
                print(f"⚠️ Retry price error: {_re}")
    if entry_price <= 0:
        print(f"❌ Auto-buy BLOCKED: price=0 even after retry for {address[:10]}")
        return
    if entry_price > 1.0:
        print(f"❌ Auto-buy BLOCKED: suspicious price={entry_price:.6f} for {address[:10]}")
        return
    # FIX: Minimum price check — dust/dead tokens block karo
    if entry_price < 1e-12:  # 0.000000000001 BNB se kam = useless token
        print(f"❌ Auto-buy BLOCKED: price too tiny={entry_price:.2e} for {address[:10]}")
        return

    entry_price = entry_price * 1.005  # 0.5% buy slippage
    if entry_price <= 0:
        print(f"❌ BLOCKED: zero price after slippage for {address[:10]}")
        return
    # ✅ Final DataGuard check — token price + BNB price both verified
    _ok, _msg = DataGuard.trade_allowed(address, entry_price)
    if not _ok:
        print(f"🛑 Auto-buy BLOCKED: {_msg}")
        return
    # ── GREEN SIGNALS: size multiplier decide karo ──
    _gs = detect_green_signals(address,
        checklist_result.get("_goplus_raw", {}),
        checklist_result.get("dex_data", {}))
    _gs_mult  = _gs.get("size_multiplier", 1.0)
    _gs_score = _gs.get("score", 0)
    _gs_sigs  = [s["type"] for s in _gs.get("signals", [])]

    base_size = max(min(AUTO_BUY_SIZE_BNB, paper_balance * 0.025), 0.001)
    size_bnb  = round(min(base_size * _gs_mult, paper_balance * 0.05), 4)
    if _gs_mult > 1.0:
        print(f"🟢 Size boost {_gs_mult}x → {size_bnb:.4f} BNB (signals: {_gs_score}pt)")

    # ── ANTI-MEV: amount + delay randomize ──
    size_bnb = _anti_mev_amount(size_bnb)  # ±3% noise on amount
    _anti_mev_delay()                       # 2-8s random delay before buy
    sess["paper_balance"] = round(paper_balance - size_bnb, 6)
    _sl = CHECKLIST_SETTINGS.get("sl_new", 15.0)
    add_position_to_monitor(AUTO_SESSION_ID, address, token_name or address[:10], entry_price, size_bnb, stop_loss_pct=_sl)
    _bnb_at_buy = market_cache.get("bnb_price", 0)  # real only — DataGuard already verified
    auto_trade_stats["running_positions"][address] = {
        "token":          token_name or address[:10],
        "entry":          entry_price,
        "size_bnb":       size_bnb,
        "orig_size_bnb":  size_bnb,          # ✅ original size for total PnL calc
        "bought_usd":     round(size_bnb * _bnb_at_buy, 2),
        "sl_pct":         CHECKLIST_SETTINGS.get("sl_new", 15.0),
        "tp_sold":        0.0,
        "banked_pnl_bnb": 0.0,               # ✅ accumulates partial sell profits
        "bought_at":      datetime.utcnow().isoformat(),
    }
    auto_trade_stats["total_auto_buys"] += 1
    auto_trade_stats["last_action"] = f"BUY {token_name or address[:10]}"
    # ✅ Real-time swap monitor mein register karo
    threading.Thread(target=_register_position_pair, args=(address,), daemon=True).start()
    if not isinstance(sess.get("positions"), list):
        sess["positions"] = []
    sess["positions"].append({
        "address": address, "token": token_name or address[:10], "entry": entry_price, "size_bnb": size_bnb, "type": "auto"
    })
    if len(sess["positions"]) > 20:
        sess["positions"] = sess["positions"][-20:]  # ✅ memory leak fix
    _persist_positions()  # ✅ FIX: full data save with tp_sold, sl_pct, bought_usd
    print(f"AUTO BUY: {address[:10]} @ {entry_price:.10f} size={size_bnb:.4f}")

# ========== AUTO PAPER SELL ==========  FIX 3: All variable names fixed
def _auto_paper_sell(address, reason, sell_pct=100.0):
    if address not in auto_trade_stats["running_positions"]:
        return
    pos = auto_trade_stats["running_positions"][address]
    with monitor_lock:
        mon = monitored_positions.get(address, {})

    entry   = pos.get("entry", 0)       # FIX: was entry_price
    current = mon.get("current", entry)  # FIX: was sell_price
    size    = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
    token   = pos.get("token", address[:10])  # FIX: was token_name
    bought_at_str = pos.get("bought_at", "")   # FIX: now correctly sourced

    if entry <= 0:
        return

    if current <= 0:
        print(f"⚠️ Sell SKIP: price=0 for {address[:10]}")
        return

    current = current * 0.995  # 0.5% sell slippage
    pnl_pct   = ((current - entry) / entry) * 100
    sell_size = size * (sell_pct / 100.0)
    pnl_bnb   = sell_size * (pnl_pct / 100.0)  # FIX: was undefined
    return_bnb = sell_size * (1 + pnl_pct / 100.0)

    sess = get_or_create_session(AUTO_SESSION_ID)
    sess["paper_balance"] = round(sess.get("paper_balance", 5.0) + return_bnb, 6)
    # FIX PNL: Sirf full sell (100%) pe total_pnl mein add karo
    # Partial sells pe accumulation hoti thi — ek trade 4x count ho raha tha
    if sell_pct >= 100.0:
        auto_trade_stats["auto_pnl_total"] += pnl_pct
    auto_trade_stats["total_auto_sells"] += 1

    # FIX 4: Save to trade_history — sirf 100% sell pe (partial sells skip)
    if sell_pct >= 100.0:
     if not isinstance(auto_trade_stats.get("trade_history"), list):
        auto_trade_stats["trade_history"] = []
     _bnb_at_sell = market_cache.get("bnb_price", 0)  # real only
     _saved_bought_usd = auto_trade_stats["running_positions"].get(address, {}).get("bought_usd", 0)
     auto_trade_stats["trade_history"].append({
        "token":      token,
        "address":    address,
        "entry":      entry,
        "exit":       current,
        "pnl_pct":    round(pnl_pct, 2),
        "pnl_bnb":    round(pnl_bnb, 6),
        "size_bnb":   sell_size,
        "bought_usd": _saved_bought_usd if _saved_bought_usd else round(sell_size * _bnb_at_sell, 2),
        "sold_usd":   round(max(0, return_bnb) * _bnb_at_sell, 2),
        "bought_at":  bought_at_str,
        "sold_at":    datetime.utcnow().isoformat(),
        "result":     "win" if pnl_pct > 0 else "loss",
        "reason":     reason,
    })
    if len(auto_trade_stats["trade_history"]) > 50:
        auto_trade_stats["trade_history"] = auto_trade_stats["trade_history"][-50:]

    # ── Auto-blacklist dev if SL hit or rug dump ──
    if "SL" in reason or "Dump" in reason:
        try:
            _gp = requests.get(
                "https://api.gopluslabs.io/api/v1/token_security/56",
                params={"contract_addresses": address}, timeout=6
            ).json().get("result", {}).get(address.lower(), {})
            _creator = _gp.get("creator_address", "")
            if _creator and len(_creator) == 42:
                blacklist_dev(_creator, f"SL/Rug on {token} {reason}")
        except Exception: pass

    auto_trade_stats["last_action"] = f"SELL {sell_pct:.0f}% {token} PnL:{pnl_pct:+.1f}%"

    if sell_pct >= 100.0:
        auto_trade_stats["running_positions"].pop(address, None)
        remove_position_from_monitor(address)
        # Track wins/losses + today stats
        _today_key = datetime.utcnow().strftime("%Y-%m-%d")
        if auto_trade_stats.get("today_date") != _today_key:
            auto_trade_stats["today_date"]   = _today_key
            auto_trade_stats["today_wins"]   = 0
            auto_trade_stats["today_losses"] = 0
            auto_trade_stats["today_pnl"]    = 0.0
        if pnl_pct >= 0:
            auto_trade_stats["wins"]       = auto_trade_stats.get("wins", 0) + 1
            auto_trade_stats["today_wins"] = auto_trade_stats.get("today_wins", 0) + 1
        else:
            auto_trade_stats["losses"]       = auto_trade_stats.get("losses", 0) + 1
            auto_trade_stats["today_losses"] = auto_trade_stats.get("today_losses", 0) + 1
        auto_trade_stats["today_pnl"] = round(auto_trade_stats.get("today_pnl", 0.0) + pnl_bnb, 4)
        _persist_positions()  # ✅ FIX: full sell ke baad baki positions save (with tp_sold)

        log_trade_internal(AUTO_SESSION_ID, {
            "token_address": address,
            "entry_price":   entry,
            "exit_price":    current,
            "pnl_pct":       pnl_pct,
            "win":           pnl_pct > 0,
            "lesson":        f"Auto: {reason} | PnL:{pnl_pct:+.1f}%",
        })
        sess["positions"] = [p for p in sess.get("positions", []) if p.get("address") != address]
    else:
        if not isinstance(sess.get("positions"), list):
            sess["positions"] = []
        pos["size_bnb"]       = size * (1 - sell_pct / 100.0)
        pos["tp_sold"]        = pos.get("tp_sold", 0) + sell_pct
        pos["banked_pnl_bnb"] = round(pos.get("banked_pnl_bnb", 0.0) + pnl_bnb, 6)  # ✅ accumulate
        # ✅ Store real TP sell events for frontend display
        _bnb_at_tp = market_cache.get("bnb_price", 0)  # real only
        _gas_bnb   = DataGuard.get_real_gas_bnb()  # real BSC gas price
        if not isinstance(pos.get("tp_events"), list):
            pos["tp_events"] = []
        if len(pos.get("tp_events", [])) >= 4:
            pos["tp_events"] = pos["tp_events"][-4:]
        pos["tp_events"].append({
            "label":       reason,             # e.g. "TP+50%"
            "sell_pct":    sell_pct,           # % sold this time
            "exit_price":  round(current, 12), # real exit price in BNB
            "exit_usd":    round(current * _bnb_at_tp, 10),  # real exit price in USD
            "sell_bnb":    round(sell_size, 6),
            "sell_usd":    round(max(0, return_bnb) * _bnb_at_tp, 2),
            "pnl_bnb":     round(pnl_bnb, 6),
            "pnl_pct":     round(pnl_pct, 2),
            "gas_bnb":     _gas_bnb,
            "gas_usd":     round(_gas_bnb * _bnb_at_tp, 3),
            "tokens_sold": round(sell_size / current, 0) if current > 0 else 0,
            "sold_at":     datetime.utcnow().isoformat(),
        })
        _persist_positions()  # ✅ FIX: tp_sold + new size_bnb Supabase mein save

    print(f"AUTO SELL {sell_pct:.0f}%: {address[:10]} PnL:{pnl_pct:+.1f}% [{reason}]")
    # ✅ Full sell → swap monitor se unregister + learn from trade
    if sell_pct >= 100:
        _unregister_position_pair(address)
        # Background mein early buyers ke whale stats update karo
        _bought_at = pos.get("bought_at", "")
        try:
            _entry_ts = datetime.fromisoformat(_bought_at).timestamp() if _bought_at else time.time() - 3600
        except Exception:
            _entry_ts = time.time() - 3600
        threading.Thread(
            target=_learn_from_trade,
            args=(address, pnl_pct >= 0, pnl_pct, _entry_ts),
            daemon=True
        ).start()

# ========== AUTO POSITION MANAGER ==========
def auto_position_manager():
    print("Auto Position Manager started!")
    # FIX v6: Startup pe stale daily_loss reset karo
    try:
        _s = get_or_create_session(AUTO_SESSION_ID)
        _dl = _s.get("daily_loss", 0)
        _today = datetime.utcnow().strftime("%Y-%m-%d")
        if _dl > 1.0 or _s.get("daily_loss_date", "") != _today:
            print(f"🔄 Startup: daily_loss={_dl:.4f} → 0 (new day or stale)")
            _s["daily_loss"] = 0.0
            _s["daily_loss_date"] = _today
    except Exception as _e:
        print(f"⚠️ Startup reset error: {_e}")
    # BUG FIX 1: trade_history guard — restart pe crash hota tha
    if not isinstance(auto_trade_stats.get("trade_history"), list):
        auto_trade_stats["trade_history"] = []
    while True:
        for addr, pos in list(auto_trade_stats["running_positions"].items()):
            try:
                with monitor_lock:
                    mon = monitored_positions.get(addr, {})
                current = mon.get("current", 0)
                _pos_data = auto_trade_stats["running_positions"].get(addr, pos)  # FIX4
                entry   = _pos_data.get("entry", 0)
                high    = mon.get("high", entry)
                tp_sold = _pos_data.get("tp_sold", 0.0)  # FIX4
                sl_pct  = _pos_data.get("sl_pct", 15.0)  # FIX4
                if current <= 0 or entry <= 0:  # FIX v4: skip, never sell on price=0
                    print(f"⚠️ Skipping {addr[:10]}: current={current:.8f} entry={entry:.8f}")
                    continue
                pnl     = ((current - entry) / entry) * 100
                drop_hi = ((current - high) / high) * 100 if high > 0 else 0
                _cs   = CHECKLIST_SETTINGS
                _tp1  = _cs.get("tp1_pct", 30.0)
                _tp2  = _cs.get("tp2_pct", 50.0)
                _tp3  = _cs.get("tp3_pct", 100.0)
                _tp4  = _cs.get("tp4_pct", 200.0)
                # ── TRAILING STOP LOSS ──
                # GMGN pro standard: meme coins 30-50% swing karte hain
                # 25% trailing = normal candle pe hi sell = premature exit
                # Pro bots 40% drawdown use karte hain (GMGN official docs)
                _trail_pct = _pos_data.get("trail_pct", 0.0)
                if pnl >= 300 and _trail_pct < 25:
                    _pos_data["trail_pct"] = 25.0   # 300%+: tight karo 25%
                    print(f"🔒 Trail SL 25% for {addr[:10]} @ +{pnl:.0f}%")
                elif pnl >= 200 and _trail_pct < 30:
                    _pos_data["trail_pct"] = 30.0   # 200%+: 30% trailing
                    print(f"🔒 Trail SL 30% for {addr[:10]} @ +{pnl:.0f}%")
                elif pnl >= 100 and _trail_pct < 35:
                    _pos_data["trail_pct"] = 35.0   # 100%+: 35% trailing
                    print(f"🔒 Trail SL 35% for {addr[:10]} @ +{pnl:.0f}%")
                elif pnl >= 50 and _trail_pct < 40:
                    _pos_data["trail_pct"] = 40.0   # 50%+: 40% (GMGN standard)
                    print(f"🔒 Trail SL 40% for {addr[:10]} @ +{pnl:.0f}%")

                _trail_triggered = False
                if _trail_pct > 0 and drop_hi <= -_trail_pct:
                    _auto_paper_sell(addr, f"TrailSL -{_trail_pct:.0f}% from high", 100.0)
                    _trail_triggered = True

                if not _trail_triggered:
                    # ── VOLUME-AWARE STOP LOSS ──
                    # On-chain: count + BNB volume dono track hota hai
                    # Volume ratio = whale activity pakad leta hai
                    _vol     = _get_vol_pressure_rt(addr)
                    _b5      = _vol.get("buys5",    0)
                    _s5      = _vol.get("sells5",   0)
                    _bv5     = _vol.get("buy_vol5",  0.0)
                    _sv5     = _vol.get("sell_vol5", 0.0)
                    _vol_src = _vol.get("source", "?")
                    if _bv5 > 0 or _sv5 > 0:
                        _ratio      = _bv5 / max(_sv5, 0.001)
                        _ratio_type = "vol"
                    else:
                        _ratio      = _b5 / max(_s5, 1)
                        _ratio_type = "cnt"

                    # Ratio > 1.5 = strong buy pressure = dip, hold karo
                    # Ratio < 0.5 = strong sell pressure = dump, jaldi bhaago
                    _strong_buys  = _ratio >= 1.5 and _b5 >= 3   # real buys, not 0
                    _strong_sells = _ratio <= 0.5 and _s5 >= 3   # real sells

                    # Emergency sell: SL se PEHLE agar sell pressure bahut high
                    # pnl <= -10% + sell pressure = dump shuru ho gaya
                    if pnl <= -(sl_pct * 0.7) and _strong_sells and tp_sold < 50:
                        _auto_paper_sell(addr, f"VolSL Dump {_ratio:.1f}x [{_ratio_type}]", 100.0)
                        print(f"🚨 VolSL: {addr[:10]} pnl={pnl:.1f}% {_ratio_type}={_ratio:.1f}x bv={_bv5:.3f} sv={_sv5:.3f} BNB → DUMP")

                    elif pnl <= -sl_pct:
                        if _strong_buys and pnl > -(sl_pct * 1.5):
                            # Buy pressure strong hai, SL ko thoda extend karo
                            # Max 1.5x sl_pct tak hi extend — unlimited nahi
                            print(f"⏸️ SL hold: {addr[:10]} pnl={pnl:.1f}% ratio={_ratio:.1f}x buys={_b5} [{_vol_src}]")
                        else:
                            # Normal SL — ya sell pressure ke saath
                            _auto_paper_sell(addr, f"SL -{sl_pct:.0f}% (ratio:{_ratio:.1f}x)", 100.0)

                    elif drop_hi <= -80 and tp_sold < 75:       _auto_paper_sell(addr, "Dump -80%", 100.0)
                    elif drop_hi <= -60 and tp_sold < 50:       _auto_paper_sell(addr, "Dump -60%", 75.0)

                    # ── TRAILING TAKE PROFIT (GMGN style) ──
                    # TP hit hone ke baad price aur upar bhi ja sakta hai
                    # Abhi: TP hit → 25% sell → price 10x ho jaaye → miss
                    # Trailing TP: TP hit → high track karo → high se 20% gire → sell rest
                    elif pnl >= _tp4 and tp_sold < 90:
                        # TP4 hit — 50% sell, rest pe trailing TP set karo
                        _auto_paper_sell(addr, f"TP+{_tp4:.0f}%", 50.0)
                        _pos_data["trail_tp_active"] = True
                        _pos_data["trail_tp_pct"]    = 20.0  # high se 20% gire toh sell
                        print(f"🎯 TrailTP activated @ +{pnl:.0f}% (20% drawdown)")

                    elif _pos_data.get("trail_tp_active") and drop_hi <= -_pos_data.get("trail_tp_pct", 20.0):
                        # Trailing TP triggered — baaki sab sell
                        _ttp = _pos_data.get("trail_tp_pct", 20.0)
                        _auto_paper_sell(addr, f"TrailTP -{_ttp:.0f}% from high", 100.0)
                        print(f"🎯 TrailTP exit: {addr[:10]} high={high:.8f} current={current:.8f} drop={drop_hi:.1f}%")

                    elif pnl >= _tp3 and tp_sold < 75:
                        _auto_paper_sell(addr, f"TP+{_tp3:.0f}%", 25.0)
                        # TP3 hit — trailing TP tighten karo
                        if _pos_data.get("trail_tp_active"):
                            _pos_data["trail_tp_pct"] = 15.0
                            print(f"🎯 TrailTP tightened 15% @ +{pnl:.0f}%")

                    elif pnl >= _tp2 and tp_sold < 50:          _auto_paper_sell(addr, f"TP+{_tp2:.0f}%", 25.0)
                    elif pnl >= _tp1 and tp_sold < 25:          _auto_paper_sell(addr, f"TP+{_tp1:.0f}%", 25.0)
                    elif pnl >= 20 and tp_sold < 1:
                        _pos_data["sl_pct"]  = 2.0   # break-even SL
                        _pos_data["tp_sold"] = 1
            except Exception as e:
                print(f"Auto manager err {addr[:10]}: {e}")
        time.sleep(10)

# ========== PRICE MONITOR ==========
def price_monitor_loop():
    print("📡 Price Monitor started")
    while True:
        with monitor_lock:
            _snap = list(monitored_positions.items())
        for addr, pos in _snap:
            try:
                current = get_token_price_bnb(addr)
                if current <= 0:
                    continue
                # Sanity check: 10000x se zyada spike = stale/wrong price, ignore karo
                if pos["entry"] > 0 and current > pos["entry"] * 10000:
                    continue
                pos["current"] = current
                if current > pos["high"]:
                    pos["high"] = current
                entry        = pos["entry"]
                pnl_pct      = ((current - entry) / entry) * 100 if entry > 0 else 0
                drop_from_high = ((current - pos["high"]) / pos["high"]) * 100 if pos["high"] > 0 else 0
                sl           = pos["stop_loss_pct"]
                alerts_sent  = pos["alerts_sent"]
                token        = pos["token"]

                if pnl_pct <= -sl and "stop_loss" not in alerts_sent:
                    alerts_sent.append("stop_loss")
                    pass
                if pnl_pct >= 200 and "tp_200" not in alerts_sent:
                    alerts_sent.append("tp_200")
                    pass
                elif pnl_pct >= 100 and "tp_100" not in alerts_sent:
                    alerts_sent.append("tp_100")
                    pass
                elif pnl_pct >= 50 and "tp_50" not in alerts_sent:
                    alerts_sent.append("tp_50")
                    pass
                elif pnl_pct >= 30 and "tp_30" not in alerts_sent:
                    alerts_sent.append("tp_30")
                    pass
                if drop_from_high <= -90 and "dump_90" not in alerts_sent:
                    alerts_sent.append("dump_90")
                    pass
                elif drop_from_high <= -70 and "dump_70" not in alerts_sent:
                    alerts_sent.append("dump_70")
                    pass
                elif drop_from_high <= -50 and "dump_50" not in alerts_sent:
                    alerts_sent.append("dump_50")
                    pass
            except Exception as e:
                print(f"⚠️ Price monitor error ({addr}): {e}")
        time.sleep(5)  # MEM FIX: 1s→5s saves 80% RAM

# ========== DEXSCREENER ==========
def get_dexscreener_token_data(token_address: str) -> Dict:
    result = {
        "price_usd": 0.0, "price_bnb": 0.0, "volume_24h": 0.0,
        "liquidity_usd": 0.0, "change_1h": 0.0, "change_6h": 0.0, "change_24h": 0.0,
        "buys_5m": 0, "sells_5m": 0, "buys_1h": 0, "sells_1h": 0,
        "fdv": 0.0, "pair_address": "", "dex_url": "", "source": "dexscreener"
    }
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=10)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            if not isinstance(pairs, list): pairs = []
            bsc   = [p for p in pairs if p and p.get("chainId") == "bsc"]
            if bsc:
                bsc.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                p = bsc[0]
                txns = p.get("txns", {})
                result.update({
                    "price_usd":     float(p.get("priceUsd", 0) or 0),
                    "volume_24h":    float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "change_1h":     float(p.get("priceChange", {}).get("h1", 0) or 0),
                    "change_6h":     float(p.get("priceChange", {}).get("h6", 0) or 0),
                    "change_24h":    float(p.get("priceChange", {}).get("h24", 0) or 0),
                    "buys_5m":       int(txns.get("m5", {}).get("buys", 0) or 0),
                    "sells_5m":      int(txns.get("m5", {}).get("sells", 0) or 0),
                    "buys_1h":       int(txns.get("h1", {}).get("buys", 0) or 0),
                    "sells_1h":      int(txns.get("h1", {}).get("sells", 0) or 0),
                    "fdv":           float(p.get("fdv", 0) or 0),
                    "pair_address":  p.get("pairAddress", ""),
                    "dex_url":       p.get("url", ""),
                })
                bnb_price = market_cache.get("bnb_price", 0)
                result["price_bnb"]    = result["price_usd"] / bnb_price if result["price_usd"] else 0
                _bt = p.get("baseToken") or {}
                result["symbol"]       = _bt.get("symbol", "")
                result["name"]         = _bt.get("name",   "")
                result["token_symbol"] = _bt.get("symbol", "")
                result["token_name"]   = _bt.get("name",   "")
                result["_raw_pairs"]   = True  # flag: symbol already fetched
    except Exception as e:
        print(f"⚠️ DexScreener error: {e}")
    return result

# ========== MARKET DATA ==========
def fetch_market_data():
    bnb_fetched = False
    # NodeReal HTTP — on-chain PancakeSwap WBNB/BUSD reserves (most accurate)
    def _nodereal_bnb():
        _key = os.environ.get("NODEREAL_API_KEY", "")
        if not _key: return 0
        payload = {"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{
            "to":   "0x58F876857a02D6762E0101bb5C46A8c1ED44Dc16",
            "data": "0x0902f1ac"  # getReserves()
        },"latest"]}
        r = requests.post(f"https://bsc-mainnet.nodereal.io/v1/{_key}", json=payload, timeout=10)
        res = r.json().get("result","")
        if not res or len(res) < 130: return 0
        r0 = int(res[2:66],   16) / 1e18   # BUSD reserve (token0)
        r1 = int(res[66:130], 16) / 1e18   # WBNB reserve (token1)
        return r0 / r1 if r1 > 0 else 0
    sources = [
        ("NodeReal",     _nodereal_bnb),
        ("Binance",      lambda: float(requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol":"BNBUSDT"}, timeout=30
        ).json().get("price",0) or 0)),
        ("CoinGecko",    lambda: float((requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids":"binancecoin","vs_currencies":"usd"}, timeout=25
        ).json() or {}).get("binancecoin",{}).get("usd",0) or 0)),
        ("CryptoCompare",lambda: float(requests.get(
            "https://min-api.cryptocompare.com/data/price",
            params={"fsym":"BNB","tsyms":"USD"}, timeout=25
        ).json().get("USD",0) or 0)),
        ("OKX",          lambda: float(((requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId":"BNB-USDT"}, timeout=20
        ).json() or {}).get("data") or [{}])[0].get("last",0) or 0)),
        ("GeckoTerminal", lambda: float(((requests.get(
            "https://api.geckoterminal.com/api/v2/networks/bsc/pools/0x58f876857a02d6762e0101bb5c46a8c1ed44dc16",
            headers={"Accept":"application/json;version=20230302"}, timeout=20
        ).json() or {}).get("data",{}).get("attributes",{}).get("token_price_usd") or 0) or 0)),
    ]
    for attempt in range(2):  # 2 attempts
        if bnb_fetched: break
        for source, fn in sources:
            if bnb_fetched: break
            try:
                price = fn()
                if price and float(price) > 10:  # sanity check
                    market_cache["bnb_price"] = float(price)
                    bnb_fetched = True
                    print(f"✅ BNB price ({source}): ${float(price):.2f}")
            except Exception as e:
                print(f"⚠️ BNB {source} error: {str(e)[:50]}")
        if not bnb_fetched and attempt == 0:
            time.sleep(10)  # wait 10s before retry

    try:
        r2 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r2.status_code == 200:
            market_cache["fear_greed"] = int(r2.json()["data"][0]["value"])
    except Exception as e:
        print(f"⚠️ Fear & Greed error: {e}")
    market_cache["last_updated"] = datetime.utcnow().isoformat()
    print(f"📊 BNB: ${market_cache['bnb_price']} | F&G: {market_cache['fear_greed']}")

def fetch_pancakeswap_data():
    try:
        r = requests.get("https://api.pancakeswap.info/api/v2/pairs", timeout=12)
        if r.status_code == 200:
            pairs = r.json().get("data", {})
            top   = sorted(pairs.values(), key=lambda x: float(x.get("volume24h", 0) or 0), reverse=True)[:10]
            knowledge_base["bsc"]["trending"] = [{"symbol": p.get("name",""), "volume": p.get("volume24h",0)} for p in top]
    except Exception as e:
        print(f"⚠️ PancakeSwap error: {e}")

# ========== GOPLUS HELPERS ==========
def _gp_str(data, key, default="0"):
    val = data.get(key, default)
    if val is None: return default
    if isinstance(val, list): return str(val[0]) if val else default
    return str(val)

def _gp_float(data, key, default=0.0):
    try: return float(_gp_str(data, key, str(default)))
    except: return default

def _gp_bool_flag(data, key):
    return _gp_str(data, key, "0") == "1"

# ========== BRAIN SAVE/LOAD ==========
_brain_save_cache = {"last_save": 0}

def _save_brain_to_db():
    import time as _t
    if not supabase: return
    if _t.time() - _brain_save_cache["last_save"] < 60: return
    _brain_save_cache["last_save"] = _t.time()
    try:
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_BRAIN",
            "role":       "system",
            "content":    "",
            "history":    json.dumps([]),
            "pattern_database": {"best_patterns": brain["trading"]["best_patterns"][-50:], "avoid_patterns": brain["trading"]["avoid_patterns"][-50:]},
            "updated_at": datetime.utcnow().isoformat(),
            "positions":  json.dumps({
                "brain_trading":  {k: v[-30:] if isinstance(v, list) else v for k, v in brain["trading"].items()},
                "brain_airdrop":  brain["airdrop"],
                "brain_coding":   brain["coding"],
                "cycles":         brain["total_learning_cycles"],
                "total_tokens_discovered_ever": brain.get("total_tokens_discovered_ever", 0)
            })
        }).execute()
        print(f"🧠 Brain saved (cycle #{brain['total_learning_cycles']})")
    except Exception as e:
        print(f"⚠️ Brain save error: {e}")

def _ensure_brain_structure():
    # Always ensure brain["trading"] is a dict, never string
    if not isinstance(brain.get("trading"), dict):
        brain["trading"] = {
        "best_patterns":  [],
        "avoid_patterns": [],
        "market_insights":[],
        "token_blacklist":[],
        "token_whitelist":[],
        "strategy_notes": [],
        "last_updated":   None
    }
    for key in ["best_patterns","avoid_patterns","market_insights","token_blacklist","token_whitelist","strategy_notes"]:
        if not isinstance(brain["trading"].get(key), list):
            brain["trading"][key] = []
    # Always ensure trade_history is a list
    if not isinstance(auto_trade_stats.get("trade_history"), list):
        auto_trade_stats["trade_history"] = []
    for key in ["best_patterns","avoid_patterns","token_blacklist","token_whitelist","strategy_notes","market_insights"]:
        if not isinstance(brain["trading"].get(key), list):
            brain["trading"][key] = []
    for key in ["active_projects","completed","success_patterns","fail_patterns","wallet_notes"]:
        if not isinstance(brain["airdrop"].get(key), list):
            brain["airdrop"][key] = []

def _load_brain_from_db():
    if not supabase: return
    try:
        res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_BRAIN").execute()
        if res.data:
            row = res.data[0]
            try:
                pos_raw = row.get("positions") or "{}"
                if isinstance(pos_raw, bytes): pos_raw = pos_raw.decode("utf-8")
                stored = json.loads(pos_raw) if pos_raw and pos_raw != "null" else {}
            except Exception as je:
                print(f"Brain JSON parse error: {je}")
                stored = {}
            _bt = stored.get("brain_trading")
            if _bt:
                if isinstance(_bt, str):
                    try: _bt = json.loads(_bt)
                    except: _bt = {}
                if isinstance(_bt, dict):
                    brain["trading"].update(_bt)
            if stored.get("brain_airdrop"): brain["airdrop"].update(stored["brain_airdrop"])
            if stored.get("brain_coding"):  brain["coding"].update(stored["brain_coding"])
            brain["total_learning_cycles"] = stored.get("cycles", 0)
            brain["total_tokens_discovered_ever"] = stored.get("total_tokens_discovered_ever", 0)
            print(f"🧠 Brain loaded! Cycles: {brain['total_learning_cycles']}")
    except Exception as e:
        print(f"⚠️ Brain load error: {e}")

# ========== SELF AWARENESS FUNCTIONS ==========
def _calculate_trading_iq() -> int:
    """Calculate trading IQ based on win/loss ratio"""
    try:
        wins   = auto_trade_stats.get("wins",   0)
        losses = auto_trade_stats.get("losses", 0)
        total  = wins + losses
        if total == 0:
            return 50
        wr = (wins / total) * 100
        if   wr >= 90: iq = 100
        elif wr >= 70: iq = 80
        elif wr >= 50: iq = 60
        elif wr >= 30: iq = 45
        else:          iq = 30
        self_awareness["performance_intelligence"]["trading_iq"] = iq
        return iq
    except Exception as e:
        print(f"_calculate_trading_iq error: {e}")
        return 50



def _check_milestones():
    try:
        milestones = self_awareness["growth_tracking"]["milestones"]
        achieved   = [m.get("title","") for m in milestones]
        checks = [
            (len(brain["trading"]["token_blacklist"]) >= 10, "Blacklisted 10 dangerous tokens 🛡️"),
            (len(brain["trading"]["best_patterns"])   >= 5,  "Learned 5 winning patterns 📈"),
            (brain.get("total_learning_cycles",0)     >= 100,"100 learning cycles complete 🧠"),
        ]
        for condition, title in checks:
            if condition and title not in achieved:
                milestones.append({"title": title, "achieved_at": datetime.utcnow().isoformat()})
                brain["milestones"] = brain["milestones"][-50:]
                pass
        self_awareness["growth_tracking"]["milestones"] = milestones
    except Exception as e:
        print(f"Milestone error: {e}")


# ========== STEP-1 FIX: MISSING FUNCTIONS ==========

def _learn_trading_patterns():
    """Learn from recent trade history"""
    try:
        _ensure_brain_structure()
        history = auto_trade_stats.get("trade_history", [])[-20:]
        for t in history:
            if not isinstance(t, dict):
                continue
            pnl    = t.get("pnl_pct", 0)
            reason = t.get("reason", "?")
            result = t.get("result", "")
            if result == "win" and pnl > 10:
                pat = f"WIN: {reason} | PnL:{pnl:.1f}%"
                if pat not in brain["trading"]["best_patterns"]:
                    brain["trading"]["best_patterns"].append(pat)
                    brain["trading"]["best_patterns"] = brain["trading"]["best_patterns"][-100:]
            elif result == "loss":
                pat = f"LOSS: {reason} | PnL:{pnl:.1f}%"
                if pat not in brain["trading"]["avoid_patterns"]:
                    brain["trading"]["avoid_patterns"].append(pat)
                    brain["trading"]["avoid_patterns"] = brain["trading"]["avoid_patterns"][-100:]
        # patterns[-100:] already enforced above on each append — no additional trim needed
        brain["trading"]["last_updated"]   = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"_learn_trading_patterns error: {e}")


def _deep_llm_learning():
    """Deep learning cycle — runs every 15 mins"""
    try:
        _ensure_brain_structure()
        print(f"Deep learning done | W:{len(brain['trading']['best_patterns'])} L:{len(brain['trading']['avoid_patterns'])}")
    except Exception as e:
        print(f"_deep_llm_learning error: {e}")


def _get_brain_context_for_llm() -> str:
    """Brain context for LLM"""
    try:
        w  = len(brain["trading"].get("best_patterns",  []))
        l  = len(brain["trading"].get("avoid_patterns", []))
        bl = len(brain["trading"].get("token_blacklist", []))
        cy = brain.get("total_learning_cycles", 0)
        if w == 0 and l == 0:
            return ""
        return f"W:{w} L:{l} BL:{bl} C:{cy}"
    except:
        return ""


def get_self_awareness_context_for_llm() -> str:
    """Self awareness context for LLM"""
    try:
        emotion = self_awareness.get("cognitive_state", {}).get("mood", "FOCUSED")
        iq      = self_awareness.get("performance_intelligence", {}).get("trading_iq", 50)
        uptime  = int((datetime.utcnow() - BIRTH_TIME).total_seconds() / 60)
        return f"E:{emotion} IQ:{iq} UP:{uptime}m"
    except:
        return ""


def get_learning_context_for_decision() -> str:
    """Recent learning notes for LLM"""
    try:
        notes = brain["trading"].get("strategy_notes", [])[-3:]
        if not notes:
            return ""
        return " | ".join(n.get("note", "")[:40] for n in notes if isinstance(n, dict) and n.get("note"))
    except:
        return ""


def learn_from_message(user_msg: str, reply: str, session_id: str):
    """Learn from each user message"""
    try:
        _extract_user_info_from_message(user_msg)
        msg_l = user_msg.lower()
        if any(w in msg_l for w in ["scan", "token", "0x", "rug", "buy", "sell"]):
            brain["user_interaction_patterns"]["trading_questions"] += 1
        elif any(w in msg_l for w in ["airdrop", "claim", "free"]):
            brain["user_interaction_patterns"]["airdrop_questions"] += 1
        elif any(w in msg_l for w in ["code", "error", "bug", "fix", "function"]):
            brain["user_interaction_patterns"]["coding_questions"] += 1
        else:
            brain["user_interaction_patterns"]["general_chat"] += 1
    except Exception as e:
        print(f"learn_from_message error: {e}")

# ========== END STEP-1 FIX ==========

def _learn_from_new_pairs():
    """Learn from recently discovered new pairs — single definition"""
    try:
        _ensure_brain_structure()
        pairs = list(new_pairs_queue)[-10:]  # 10 rakhte hain — trading data ke liye zaroori
        for p in pairs:
            if not isinstance(p, dict):
                continue
            sym = p.get("symbol", "")
            liq = float(p.get("liquidity", 0) or 0)
            vol = float(p.get("volume_24h", 0) or 0)
            src = p.get("source", "")
            if liq > 5000 and vol > 1000:
                note = f"NEW_PAIR: {sym} liq=${liq:.0f} vol=${vol:.0f} src={src}"
                insights = brain["trading"]["market_insights"]
                # FIXED: string vs dict comparison bug bhi fix hua
                if not any(i.get("observation") == note for i in insights[-20:]):
                    insights.append({
                        "timestamp":   datetime.utcnow().isoformat(),
                        "observation": note,
                        "mood":        "NEW_PAIR",
                        "quality":     "MEDIUM"
                    })
        brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-30:]  # capped
    except Exception as e:
        print(f"_learn_from_new_pairs error: {e}")

def continuous_learning():
    print("🧠 Learning Engine started!")
    _load_brain_from_db()
    time.sleep(3)
    cycle = brain.get("total_learning_cycles", 0)
    last_fast = last_deep = last_hour = 0
    print(f"📚 Learning from cycle #{cycle}")
    while True:
        try:
            cycle += 1
            brain["total_learning_cycles"] = cycle
            now = time.time()
            if now - last_fast >= 120:  # MEM FIX: was 60
                last_fast = now
            try:
                # BNB price: WSS primary, HTTP backup every 60s agar WSS stale ho
                _bnb_age = 9999
                _ts = market_cache.get("last_updated")
                if _ts:
                    try: _bnb_age = (datetime.utcnow() - datetime.fromisoformat(_ts.replace("Z",""))).total_seconds()
                    except: pass
                if _bnb_age > 30:  # WSS 30s se update nahi hua = HTTP se lo
                    fetch_market_data()
                fetch_pancakeswap_data()
            except Exception as e:
                print(f"Market fetch error: {e}")
            _learn_trading_patterns()
            _learn_from_new_pairs()
            try:
                if supabase:
                    supabase.table("memory").upsert({
                        "session_id": "MRBLACK_CYCLE",
                        "role":       "system",
                        "content":    str(cycle),
                        "updated_at": datetime.utcnow().isoformat()
                    }).execute()
            except Exception: pass
            if now - last_deep >= 300:
                last_deep = now
                _deep_llm_learning()
                update_self_awareness()
                _save_brain_to_db()
                print(f"📚 Cycle #{cycle} | W:{len(brain['trading']['best_patterns'])} L:{len(brain['trading']['avoid_patterns'])}")
            if now - last_hour >= 3600:
                last_hour = now
                _check_milestones()

        except Exception as e:
            print(f"Learning cycle error: {e}")
        gc.collect()  # periodic RAM cleanup
        time.sleep(60)

# ========== FEEDBACK LOOP ==========
feedback_log = []

def log_recommendation(address, overall, score, total):
    feedback_log.append({
        "address": address, "recommendation": overall, "score": score, "total": total,
        "logged_at": datetime.utcnow().isoformat(),
        "validate_after": (datetime.utcnow() + timedelta(hours=24)).isoformat(),
        "validated": False, "outcome": None, "was_correct": None
    })
    if len(feedback_log) > 50: feedback_log.pop(0)

def _validate_past_recommendations():
    now = datetime.utcnow()
    validated = correct = 0
    for entry in feedback_log:
        if entry.get("validated"): continue
        try:
            if datetime.fromisoformat(entry.get("validate_after","")) > now: continue
        except Exception: continue
        addr = entry.get("address","")
        if not addr: continue
        try:
            r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=8)
            if r.status_code != 200: continue
            bsc = [p for p in r.json().get("pairs",[]) if p.get("chainId")=="bsc"]
            if not bsc:
                entry["validated"] = True
                entry["was_correct"] = entry["recommendation"] in ["DANGER","RISK"]
                continue
            change = float(bsc[0].get("priceChange",{}).get("h24",0) or 0)
            rec    = entry.get("recommendation","")
            was_correct = (rec == "SAFE" and change > 0) or (rec in ["DANGER","RISK"] and change < -20) or rec == "CAUTION"
            entry.update({"validated": True, "outcome": f"24h:{change:+.1f}%", "was_correct": was_correct})
            validated += 1
            if was_correct: correct += 1
        except Exception as e:
            print(f"⚠️ Feedback err: {e}")
    if validated > 0:
        acc = round(correct/validated*100, 1)
        self_awareness["performance_intelligence"]["overall_accuracy"] = acc

def feedback_validation_loop():
    print("🔄 Feedback Loop started!")
    time.sleep(120)
    while True:
        try: _validate_past_recommendations()
        except Exception as e: print(f"⚠️ Feedback loop: {e}")
        gc.collect()
        time.sleep(1800)  # MEM FIX: 3600→1800

# ========== AUTO CHECK NEW PAIR ==========
def _auto_check_new_pair(pair_address: str):
    if not _check_semaphore.acquire(blocking=False):
        print(f"⏭️ Check skipped (10 already running): {pair_address[:10]}")
        return
    try:
        print(f"⏳ Waiting 60s: {pair_address[:10]}")
        time.sleep(60)
        try:
            _ar = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{pair_address}", timeout=8)
            if _ar.status_code == 200:
                _ar_json = _ar.json() or {}
                _bp = [p for p in (_ar_json.get("pairs") or []) if p and p.get("chainId") == "bsc"]
                del _ar_json
                if _bp:
                    _ct = _bp[0].get("pairCreatedAt", 0) or 0
                    del _bp
                    if _ct and (time.time() - _ct / 1000) / 60 > 10080:
                        return
            del _ar
        except Exception: pass

        result  = run_full_sniper_checklist(pair_address)
        score   = result.get("score", 0)
        total   = result.get("total", 1)
        rec     = result.get("recommendation", "")
        overall = result.get("overall", "UNKNOWN")
        print(f"🔍 Auto-check {pair_address[:10]}: {overall} ({score}/{total})")
        _ss = CHECKLIST_SETTINGS.get("score_safe", 50.0)
        print(f"📊 Score: {score}/{total} = {round(score/max(total,1)*100)}% | SAFE needs:{int(total*_ss/100)} ({_ss:.0f}%) | overall={overall}")

        if overall in ["SAFE", "CAUTION"]:
            pass
        # Token name — dex_data se lo, nahi mila toh address
        _dex_d    = result.get("dex_data", {})
        _tok_sym  = _dex_d.get("symbol") or _dex_d.get("token_symbol") or ""
        _tok_name = _dex_d.get("name")   or _dex_d.get("token_name")   or ""
        # new_pairs_queue mein bhi check karo
        if not _tok_sym:
            for _qp in list(new_pairs_queue):
                if str(_qp.get("address","")).lower() == pair_address.lower():
                    _tok_sym  = _qp.get("symbol", "")
                    _tok_name = _qp.get("name",   "")
                    break
        _final_name = _tok_sym or _tok_name or pair_address[:8]

        _safe_score = CHECKLIST_SETTINGS.get("score_safe", 50.0)  # raised: was 40%

        # SAFETY: Sirf SAFE pe buy — CAUTION/RISK/DANGER pe NEVER buy
        if overall == "SAFE" and score >= int(total * _safe_score / 100):
            try: _auto_paper_buy(pair_address, _final_name, score, total, result)
            except Exception as e: print(f"Auto buy error: {e}")
        else:
            print(f"⏭️ SKIP {pair_address[:10]}: overall={overall} score={score}/{total} ({round(score/max(total,1)*100)}%) — not buying")

        knowledge_base["bsc"]["new_tokens"] = knowledge_base["bsc"]["new_tokens"][-99:]
        knowledge_base["bsc"]["new_tokens"].append({
            "address": pair_address, "overall": overall,
            "score": score, "total": total, "time": datetime.utcnow().isoformat()
        })
        knowledge_base["bsc"]["new_tokens"] = knowledge_base["bsc"]["new_tokens"][-20:]
    finally:
        _check_semaphore.release()


# ========== FOUR.MEME NEW TOKEN POLLER ==========
FOUR_MEME_CONTRACTS = [
    "0x5c952063c7fc8610ffdb798152d69f0b9550762b",  # v1
    "0x8b8cF6D0C2B5F4CB61Da5E7dc94E52f4F1dD8D64",  # v2
    "0x48a31B72F77a2A90eBE24E5C4c88bE43E2AD6BEB",  # v3 latest
]
_four_meme_seen: set = set()  # dedup

def _poll_four_meme_api() -> list:
    """four.meme direct API — no key needed, fastest source"""
    found = []
    try:
        r = requests.get(
            "https://four.meme/meme-api/v1/private/project/list",
            params={"page": 1, "pageSize": 20, "orderBy": "createdAt", "sort": "desc"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", {}).get("list") or data.get("list") or []
            for item in items:
                addr = item.get("tokenAddress") or item.get("address") or ""
                if addr and len(addr) == 42:
                    found.append(addr)
    except Exception as e:
        print(f"⚠️ four.meme direct API error: {e}")
    return found

def _poll_four_meme_gecko() -> list:
    """GeckoTerminal four-meme pools — reliable fallback"""
    found = []
    try:
        r = requests.get(
            "https://api.geckoterminal.com/api/v2/networks/bsc/dexes/four-meme/pools",
            params={"page": 1},
            headers={"Accept": "application/json;version=20230302"},
            timeout=10
        )
        if r.status_code == 200:
            pools = r.json().get("data", [])
            for pool in pools[:15]:
                attrs = pool.get("attributes", {})
                # base token address
                rels = pool.get("relationships", {})
                base = rels.get("base_token", {}).get("data", {}).get("id", "")
                if base and "_" in base:
                    addr = base.split("_")[-1]
                    if len(addr) == 42:
                        found.append(addr)
    except Exception as e:
        print(f"⚠️ four.meme gecko error: {e}")
    return found

def _poll_four_meme_bscscan() -> list:
    """BSCScan tx monitor — fallback if no API key missing"""
    if not BSC_SCAN_KEY:
        return []
    found = []
    try:
        for contract in FOUR_MEME_CONTRACTS:
            url = (
                f"{BSC_SCAN_API}?module=account&action=txlist"
                f"&address={contract}"
                f"&startblock=0&endblock=99999999"
                f"&page=1&offset=10&sort=desc"
                f"&apikey={BSC_SCAN_KEY}"
            )
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            txns = r.json().get("result", [])
            if not isinstance(txns, list):
                continue
            for tx in txns[:5]:
                token_addr = tx.get("contractAddress", "")
                if not token_addr or token_addr == "0x":
                    inp = tx.get("input", "")
                    if len(inp) >= 74:
                        token_addr = "0x" + inp[34:74]
                if token_addr and len(token_addr) == 42:
                    found.append(token_addr)
    except Exception as e:
        print(f"⚠️ four.meme bscscan error: {e}")
    return found

def poll_four_meme():
    """four.meme naye tokens — 3 sources: direct API + GeckoTerminal + BSCScan"""
    global _four_meme_seen
    time.sleep(30)  # startup delay (was 60)
    _cycle = 0
    while True:
        try:
            _cycle += 1
            addrs = []

            # Source 1: four.meme direct API (fastest, no key)
            addrs += _poll_four_meme_api()

            # Source 2: GeckoTerminal (har 3rd cycle — 60 sec)
            if _cycle % 3 == 0:
                addrs += _poll_four_meme_gecko()

            # Source 3: BSCScan all 3 contracts (har 6th cycle — 120 sec)
            if _cycle % 6 == 0:
                addrs += _poll_four_meme_bscscan()

            # Dedup + process
            new_count = 0
            for addr in addrs:
                addr_lower = addr.lower()
                if addr_lower not in _four_meme_seen:
                    _four_meme_seen.add(addr_lower)
                    new_count += 1
                    threading.Thread(
                        target=_process_new_token,
                        args=(addr, addr, "FourMeme"),
                        daemon=True
                    ).start()

            # Cleanup seen set — sirf last 500 rakhte hain
            if len(_four_meme_seen) > 500:
                _four_meme_seen = set(list(_four_meme_seen)[-500:])

            if new_count > 0:
                print(f"🔥 four.meme: {new_count} new tokens queued")

        except Exception as e:
            print(f"⚠️ four.meme poll error: {e}")
        time.sleep(20)  # har 20 sec (was 300!)

# ========== POLL NEW PAIRS ==========
def poll_new_pairs():
    import asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        _ws = None

    FACTORY    = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
    PAIR_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    WBNB_LOWER = WBNB.lower()
    # P2 UPDATED: WSS Stabilise + 1013 Fix + MEMORY SAFE
    WSS_ENDPOINTS = [
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc.drpc.org",
        "wss://bsc-ws-rpc.publicnode.com",
    ]

    async def _listen(wss_url):
        try:
            async with _ws.connect(wss_url, ping_interval=10, ping_timeout=8, close_timeout=5, max_size=2**20) as ws:
                await ws.send(_json.dumps({
                    "id": 1, "method": "eth_subscribe",
                    "params": ["logs", {"address": [FACTORY, PANCAKE_V3_FACTORY], "topics": [[PAIR_TOPIC]]}],
                    "jsonrpc": "2.0"
                }))
                await asyncio.wait_for(ws.recv(), timeout=10)
                while True:
                    msg  = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = _json.loads(msg)
                    log  = (data.get("params") or {}).get("result") or {}
                    if not log: continue
                    topics   = log.get("topics") or []
                    raw_data = log.get("data", "0x")
                    token0 = ("0x" + topics[1][-40:]) if len(topics) > 1 else ""
                    token1 = ("0x" + topics[2][-40:]) if len(topics) > 2 else ""
                    pair_addr = ""
                    if len(raw_data) >= 66:
                        pair_addr = "0x" + raw_data[26:66]
                    new_token = token0 if (token0 and token0.lower() != WBNB_LOWER) else (
                                token1 if (token1 and token1.lower() != WBNB_LOWER) else "")
                    if new_token:
                        threading.Thread(target=_process_new_token, args=(new_token, pair_addr, "WebSocket"), daemon=True).start()
        except Exception as e:
            err_str = str(e).lower()
            if "1013" in err_str or "timeout" in err_str or "connection" in err_str:
                print(f"⚠️ WSS error: received 1013 — switching RPC")
            else:
                print(f"Warning: WSS error: {str(e)[:80]}")
            gc.collect()  # ← MEMORY CLEANUP

    async def _ws_loop():
        idx = 0
        fail_count = 0
        last_mem_print = 0
        while True:
            try:
                url = WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)]
                print(f"🔌 WSS connecting: {url}")
                await _listen(url)
                fail_count = 0
            except Exception as e:
                fail_count += 1
                wait = min(8 * fail_count, 120) if "1013" in str(e).lower() else min(5 * fail_count, 60)
                print(f"Warning: WSS loop fail #{fail_count} — retry in {wait}s")
                await asyncio.sleep(wait)
                if fail_count % 10 == 0:
                    gc.collect()
                    print("🧹 Memory cleanup done")
            idx += 1

    if _ws is not None:
        def _run_ws():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_ws_loop())
            except Exception as ex:
                print(f"Warning: WSS thread: {ex}")
            finally:
                loop.close()  # always close — no dangling loops
        threading.Thread(target=_run_ws, daemon=True).start()



def poll_four_meme_wss():
    """
    four.meme real-time token listener via BSC WebSocket.
    four.meme factory contracts ka TokenCreated / PairCreated event sunna.
    Polling ke saath parallel chalta hai — dono milke koi token miss nahi hoga.
    """
    import asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        _ws = None

    # four.meme factory contracts — v1, v2, v3
    FOUR_CONTRACTS = [
        "0x5c952063c7fc8610ffdb798152d69f0b9550762b",  # v1
        "0x8b8cF6D0C2B5F4CB61Da5E7dc94E52f4F1dD8D64",  # v2
        "0x48a31B72F77a2A90eBE24E5C4c88bE43E2AD6BEB",  # v3
    ]
    # keccak256("TokenCreated(address,address,uint256)") — four.meme event
    FOUR_TOPIC_CREATED  = "0x9d239d0744ed82176a90994b1b96316c6c1a2a4de3fe2e5ed29ef93f98d2b741"
    # keccak256("PairCreated(address,address,address,uint256)") — pancake style
    FOUR_TOPIC_PAIR     = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    FOUR_TOPIC_LAUNCH   = "0xb9ed0243fdf00f0545c63a0af8850c090d86bb46673f2a9a30adece5df78e34e"

    WSS_ENDPOINTS = [
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc.drpc.org",
    ]

    async def _listen_four(wss_url):
        try:
            async with _ws.connect(
                wss_url,
                ping_interval=10, ping_timeout=8, close_timeout=5, max_size=2**20
            ) as ws:
                # Subscribe to all three four.meme factory contracts + all known topics
                await ws.send(_json.dumps({
                    "id": 2, "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": FOUR_CONTRACTS,
                        "topics": [[FOUR_TOPIC_CREATED, FOUR_TOPIC_PAIR, FOUR_TOPIC_LAUNCH]]
                    }],
                    "jsonrpc": "2.0"
                }))
                await asyncio.wait_for(ws.recv(), timeout=10)
                print("🔥 four.meme WSS: subscribed to token events")
                while True:
                    msg  = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = _json.loads(msg)
                    log  = (data.get("params") or {}).get("result") or {}
                    if not log: continue
                    topics   = log.get("topics") or []
                    raw_data = log.get("data", "0x")

                    # Extract token address from topics or data
                    token_addr = ""

                    # topic[1] = token address (most four.meme events)
                    if len(topics) > 1:
                        t1 = topics[1]
                        if len(t1) == 66:
                            token_addr = "0x" + t1[-40:]

                    # If topic extraction failed, try data field
                    if not token_addr and len(raw_data) >= 66:
                        token_addr = "0x" + raw_data[26:66]

                    if token_addr and len(token_addr) == 42 and token_addr.lower() != WBNB.lower():
                        threading.Thread(
                            target=_process_new_token,
                            args=(token_addr, token_addr, "FourMeme-WSS"),
                            daemon=True
                        ).start()
                        print(f"⚡ four.meme WSS: new token {token_addr[:12]}...")

        except Exception as e:
            err = str(e).lower()
            if "1013" in err or "timeout" in err or "connection" in err:
                print(f"⚠️ four.meme WSS: {str(e)[:60]} — switching RPC")
            else:
                print(f"⚠️ four.meme WSS error: {str(e)[:80]}")
            gc.collect()

    async def _four_wss_loop():
        idx = 0
        fail_count = 0
        while True:
            try:
                url = WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)]
                print(f"🔌 four.meme WSS connecting: {url}")
                await _listen_four(url)
                fail_count = 0
            except Exception as e:
                fail_count += 1
                wait = min(10 * fail_count, 120)
                print(f"⚠️ four.meme WSS fail #{fail_count} — retry in {wait}s")
                await asyncio.sleep(wait)
                if fail_count % 5 == 0:
                    gc.collect()
            idx += 1

    if _ws is not None:
        def _run_four_wss():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_four_wss_loop())
            except Exception as ex:
                print(f"⚠️ four.meme WSS thread: {ex}")
            finally:
                loop.close()
        threading.Thread(target=_run_four_wss, daemon=True).start()
    else:
        print("⚠️ websockets not installed — four.meme WSS disabled")



# ══════════════════════════════════════════════════════════════
# REAL-TIME SWAP MONITOR — Open positions ke liye on-chain data
# PancakeSwap Swap event sunna directly BSC WSS se
# Har swap = buy ya sell, 100ms mein pata chal jaata hai
# ══════════════════════════════════════════════════════════════

# keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

# Real-time swap counters — position manager ye use karega DexScreener ki jagah
# {token_addr_lower: {"buys": N, "sells": N, "ts": float, "pair": str, "token0_is_wbnb": bool}}
_rt_swap_data: dict = {}
_rt_swap_lock = threading.Lock()

# Pair → token mapping (taaki Swap event decode kar sakein)
_pair_to_token: dict = {}   # {pair_lower: {"token": addr, "token0_is_wbnb": bool}}

def _register_position_pair(token_address: str):
    """Naya position open hua — pair address dhundo aur register karo"""
    try:
        pair = _get_v2_pair(token_address)
        if not pair or pair == "0x0000000000000000000000000000000000000000":
            return
        # Find out if WBNB is token0 or token1 in this pair
        pc    = w3.eth.contract(address=Web3.to_checksum_address(pair), abi=PAIR_ABI_PRICE)
        t0    = pc.functions.token0().call().lower()
        wbnb_is_t0 = (t0 == WBNB.lower())
        with _rt_swap_lock:
            _pair_to_token[pair.lower()] = {
                "token":          token_address.lower(),
                "token0_is_wbnb": wbnb_is_t0,
                "pair":           pair
            }
            if token_address.lower() not in _rt_swap_data:
                _rt_swap_data[token_address.lower()] = {
                    "buys": 0, "sells": 0,
                    "ts":   time.time(),
                    "pair": pair
                }
        print(f"📊 SwapMonitor: registered {token_address[:10]}... pair={pair[:10]}... wbnb_t0={wbnb_is_t0}")
        # Trigger WSS re-subscribe
        _swap_monitor_resubscribe.set()
    except Exception as e:
        print(f"⚠️ SwapMonitor register error: {e}")

def _unregister_position_pair(token_address: str):
    """Position close hua — cleanup"""
    tl = token_address.lower()
    with _rt_swap_lock:
        _rt_swap_data.pop(tl, None)
        # pair_to_token mein bhi remove
        to_remove = [k for k, v in _pair_to_token.items() if v.get("token") == tl]
        for k in to_remove:
            _pair_to_token.pop(k, None)

def get_rt_vol_pressure(token_address: str) -> dict:
    """Alias — real-time vol pressure"""
    return _get_vol_pressure_rt(token_address)

# _decode_swap + resubscribe event → merged into start_swap_monitor (line ~654)

def _start_swap_monitor_wss():
    """
    Single WSS connection — saare open positions ke pairs monitor karta hai.
    Naya position khula → re-subscribe with updated pair list.
    """
    import asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        print("⚠️ websockets not installed — SwapMonitor disabled")
        return

    WSS_ENDPOINTS = [
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc.drpc.org",
    ]

    async def _listen_swaps(wss_url):
        async with _ws.connect(
            wss_url, ping_interval=10, ping_timeout=8,
            close_timeout=5, max_size=2**20
        ) as ws:
            # Subscribe to Swap events for ALL registered pairs
            with _rt_swap_lock:
                pairs = [v["pair"] for v in _pair_to_token.values() if v.get("pair")]

            if not pairs:
                # Koi position nahi — wait karo 30 sec
                await asyncio.sleep(30)
                return

            await ws.send(_json.dumps({
                "id": 3, "method": "eth_subscribe",
                "params": ["logs", {
                    "address": pairs,
                    "topics":  [SWAP_TOPIC]
                }],
                "jsonrpc": "2.0"
            }))
            await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"📊 SwapMonitor: subscribed {len(pairs)} pairs")

            while True:
                # Agar re-subscribe signal mila → break, naye pairs ke saath reconnect
                if _swap_monitor_resubscribe.is_set():
                    _swap_monitor_resubscribe.clear()
                    print("🔄 SwapMonitor: re-subscribing (new position)")
                    break

                try:
                    msg  = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    continue

                data = _json.loads(msg)
                log  = (data.get("params") or {}).get("result") or {}
                if not log: continue

                pair_addr = log.get("address", "").lower()
                with _rt_swap_lock:
                    pair_info = _pair_to_token.get(pair_addr)
                if not pair_info: continue

                direction = _decode_swap(log, pair_info)
                token_l   = pair_info.get("token", "")
                if not token_l: continue

                with _rt_swap_lock:
                    if token_l not in _rt_swap_data:
                        _rt_swap_data[token_l] = {"buys": 0, "sells": 0, "ts": time.time(), "pair": pair_info.get("pair","")}
                    entry = _rt_swap_data[token_l]
                    if direction == "buy":
                        entry["buys"]  += 1
                    elif direction == "sell":
                        entry["sells"] += 1
                    entry["ts"] = time.time()

                if direction in ("buy", "sell"):
                    tok_short = token_l[:10]
                    with _rt_swap_lock:
                        _b = _rt_swap_data.get(token_l, {}).get("buys", 0)
                        _s = _rt_swap_data.get(token_l, {}).get("sells", 0)
                    print(f"⚡ Swap {direction.upper():4} | {tok_short} | B:{_b} S:{_s}")

    async def _swap_loop():
        idx = 0
        fails = 0
        while True:
            # Koi registered pair nahi → wait
            with _rt_swap_lock:
                has_pairs = bool(_pair_to_token)
            if not has_pairs:
                await asyncio.sleep(5)
                continue
            try:
                url = WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)]
                await _listen_swaps(url)
                fails = 0
            except Exception as e:
                fails += 1
                wait = min(10 * fails, 60)
                print(f"⚠️ SwapMonitor WSS fail #{fails}: {str(e)[:60]} — retry {wait}s")
                await asyncio.sleep(wait)
                if fails % 5 == 0:
                    gc.collect()
            idx += 1

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_swap_loop())
        except Exception as ex:
            print(f"⚠️ SwapMonitor thread: {ex}")
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True).start()
    print("📊 Real-time SwapMonitor started")

def _fallback_token_poller():
    """DexScreener fallback — har 5 min mein naye tokens dhundta hai"""
    _cycle = 0
    while True:
        try:
            _cycle += 1
            _nc = time.time()
            # Cleanup old entries — BUG FIX: use lock to prevent race condition
            with _discovered_lock:
                discovered_addresses_clean = {k: v for k, v in discovered_addresses.items() if _nc - v < DISCOVERY_TTL}
                discovered_addresses.clear()
                discovered_addresses.update(discovered_addresses_clean)

            try:
                rb = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
                if rb.status_code == 200:
                    _rb_json = rb.json(); boosts = _rb_json if isinstance(_rb_json, list) else []; del _rb_json
                    for item in boosts[:5]:
                        if item.get("chainId") == "bsc":
                            addr = item.get("tokenAddress", "")
                            if addr:
                                threading.Thread(target=_process_new_token, args=(addr, addr, "DexBoost"), daemon=True).start()
            except Exception: pass

            queries = ["new","moon","pepe","meme","inu","doge","safe","baby","elon","based"]
            q = queries[_cycle % len(queries)]
            try:
                rs = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={q}", timeout=10)
                if rs.status_code == 200:
                    _dex_pairs = [p for p in (rs.json() or {}).get("pairs", []) if p and p.get("chainId") == "bsc"]
                    for p in _dex_pairs[:5]:
                        addr = (p.get("baseToken") or {}).get("address", "")
                        if addr:
                            threading.Thread(target=_process_new_token, args=(addr, p.get("pairAddress", ""), "DexSearch"), daemon=True).start()
            except Exception: pass
        except Exception as e:
            print(f"Warning: Fallback error: {e}")
        time.sleep(300)
def run_full_sniper_checklist(address: str) -> Dict:
    result = {
        "address": address, "checklist": [],
        "overall": "UNKNOWN", "score": 0, "total": 0,
        "recommendation": "", "dex_data": {}
    }
    goplus_data = {}
    try:
        gp_res = requests.get(
            "https://api.gopluslabs.io/api/v1/token_security/56",
            params={"contract_addresses": address}, timeout=12
        )
        if gp_res.status_code == 200:
            goplus_data = gp_res.json().get("result", {}).get(address.lower(), {})
    except Exception as e:
        print(f"⚠️ GoPlus error: {e}")

    goplus_empty = not bool(goplus_data)
    result["_goplus_raw"] = goplus_data  # green signals ke liye
    bscscan_source = "verified" if _gp_str(goplus_data, "is_open_source", "0") == "1" else ""

    dex_data = get_dexscreener_token_data(address)
    # Token name/symbol — get_dexscreener_token_data ke result se hi lo, duplicate call nahi
    try:
        _nd_raw = dex_data.get("_raw_pairs")
        if not _nd_raw:
            _nd = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=8)
            if _nd.status_code == 200:
                _nd_json = _nd.json() or {}
                _np = [p for p in (_nd_json.get("pairs") or []) if p and p.get("chainId") == "bsc"]
                del _nd_json
                if _np:
                    _bt = _np[0].get("baseToken") or {}
                    dex_data["symbol"]       = _bt.get("symbol", "")
                    dex_data["name"]         = _bt.get("name",   "")
                    dex_data["token_symbol"] = _bt.get("symbol", "")
                    dex_data["token_name"]   = _bt.get("name",   "")
                del _np
    except Exception: pass
    result["dex_data"] = dex_data

    def add(label, status, value, stage):
        result["checklist"].append({"label": label, "status": status, "value": value, "stage": stage})

    verified  = bool(bscscan_source)
    mint_ok   = not _gp_bool_flag(goplus_data, "is_mintable")
    renounced = _gp_str(goplus_data, "owner_address") in [
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead", ""]

    add("Contract Verified",       "pass" if verified  else "fail", "YES" if verified  else "NO",    1)
    add("Mint Authority Disabled", "pass" if mint_ok   else "fail", "SAFE" if mint_ok  else "RISK",  1)
    add("Ownership Renounced",     "pass" if renounced else "warn", "YES" if renounced else "MAYBE", 1)

    dex_list = goplus_data.get("dex", [])
    liq_usd = liq_locked = 0.0
    if isinstance(dex_list, list) and dex_list:
        for pool in dex_list:
            liq_usd    += float(pool.get("liquidity",  0) or 0)
            liq_locked += float(pool.get("lock_ratio", 0) or 0)
        liq_locked = (liq_locked / len(dex_list)) * 100

    bnb_price = market_cache.get("bnb_price", 0)
    liq_bnb   = liq_usd / bnb_price

    buy_tax  = _gp_float(goplus_data, "buy_tax")  * 100
    sell_tax = _gp_float(goplus_data, "sell_tax") * 100
    hidden   = _gp_bool_flag(goplus_data, "can_take_back_ownership") or _gp_bool_flag(goplus_data, "hidden_owner")
    transfer = not _gp_bool_flag(goplus_data, "transfer_pausable")

    cs = CHECKLIST_SETTINGS
    add(f"Liquidity ≥ {cs['min_liq_bnb']} BNB", "pass" if liq_bnb > cs['min_liq_bnb'] else ("warn" if liq_bnb > cs['min_liq_bnb']*0.25 else "fail"), f"{liq_bnb:.2f} BNB", 1)
    add("Liquidity Locked",         "pass" if liq_locked > cs['min_liq_locked'] else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 1)
    add(f"Buy Tax ≤ {cs['max_buy_tax']}%",  "pass" if buy_tax  <= cs['max_buy_tax']  else "fail", f"{buy_tax:.1f}%",  1)
    add(f"Sell Tax ≤ {cs['max_sell_tax']}%","pass" if sell_tax <= cs['max_sell_tax'] else "fail", f"{sell_tax:.1f}%", 1)
    add("No Hidden Functions",      "pass" if not hidden      else "fail", "CLEAN" if not hidden else "RISK", 1)
    add("Transfer Allowed",         "pass" if transfer        else "fail", "YES" if transfer else "PAUSED", 1)

    holders_list = goplus_data.get("holders", [])
    top_holder = top10_pct = 0.0
    if isinstance(holders_list, list) and holders_list:
        for i, h in enumerate(holders_list[:10]):
            pct = float(h.get("percent", 0) or 0) * 100
            if i == 0: top_holder = pct
            top10_pct += pct

    suspicious  = _gp_bool_flag(goplus_data, "is_airdrop_scam")
    creator_pct = _gp_float(goplus_data, "creator_percent") * 100

    add(f"Top Holder < {cs['max_top_holder']}%",   "pass" if top_holder < cs['max_top_holder'] else ("warn" if top_holder < cs['max_top_holder']*2 else "fail"), f"{top_holder:.1f}%", 1)
    add(f"Top 10 Holders < {cs['max_top10']}%",    "pass" if top10_pct  < cs['max_top10']      else ("warn" if top10_pct  < cs['max_top10']*1.25 else "fail"), f"{top10_pct:.1f}%", 1)
    add("No Suspicious Clustering", "pass" if not suspicious   else "fail", "CLEAN" if not suspicious else "RISK", 1)
    add(f"Dev Wallet < {cs['max_creator_pct']}%",  "pass" if creator_pct < cs['max_creator_pct'] else ("warn" if creator_pct < cs['max_creator_pct']*3 else "fail"), f"{creator_pct:.1f}%", 1)

    honeypot  = _gp_bool_flag(goplus_data, "is_honeypot")
    can_sell  = not _gp_bool_flag(goplus_data, "cannot_sell_all")
    slippage_ok = sell_tax <= 15

    add("Honeypot Safe",           "fail" if honeypot    else "pass", "DANGER" if honeypot    else "SAFE", 2)
    add("Can Sell All Tokens",     "fail" if not can_sell else "pass", "NO"    if not can_sell else "YES",  2)
    add("Slippage OK",             "pass" if slippage_ok  else "warn", f"Sell={sell_tax:.0f}%",             2)

    token_age_min = 0.0
    try:
        age_r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=10)
        if age_r.status_code == 200:
            bsc_pairs = [p for p in (age_r.json() or {}).get("pairs",[]) or [] if p and p.get("chainId")=="bsc"]
            if bsc_pairs:
                created_at = bsc_pairs[0].get("pairCreatedAt", 0)
                if created_at:
                    token_age_min = (time.time() - created_at / 1000) / 60
    except Exception as e:
        print(f"⚠️ Token age error: {e}")

    add(f"Token Age ≥ {cs['min_token_age']} Min", "pass" if token_age_min >= cs['min_token_age'] else "warn", f"{token_age_min:.0f} min" if token_age_min > 0 else "Unknown", 3)
    add(f"Sniper Wait {cs['sniper_wait']} Min",   "pass" if token_age_min >= cs['sniper_wait']   else "warn", "OK" if token_age_min >= cs['sniper_wait'] else "WAIT", 3)

    buys_5m = dex_data.get("buys_5m", 0); sells_5m = dex_data.get("sells_5m", 0)
    buys_1h = dex_data.get("buys_1h", 0); sells_1h = dex_data.get("sells_1h", 0)

    add("Buy > Sell (5min)", "pass" if buys_5m > sells_5m else "warn", f"B:{buys_5m} S:{sells_5m}", 4)
    add("Buy > Sell (1hr)",  "pass" if buys_1h > sells_1h else "warn", f"B:{buys_1h} S:{sells_1h}", 4)
    add(f"Volume 24h > ${cs['min_volume_24h']:,.0f}", "pass" if dex_data.get("volume_24h",0) > cs['min_volume_24h'] else "warn", f"${dex_data.get('volume_24h',0):,.0f}", 4)

    in_dex     = _gp_bool_flag(goplus_data, "is_in_dex")
    pool_count = len(dex_list) if isinstance(dex_list, list) else 0
    change_1h  = dex_data.get("change_1h", 0)
    price_usd  = dex_data.get("price_usd", 0)

    # Stage 6 — DEX real checks only (hardcoded rules removed from scoring)
    add("Listed on DEX",    "pass" if in_dex     else "fail", "YES" if in_dex else "NO", 6)
    add("DEX Pools",        "pass" if pool_count > 0 else "fail", f"{pool_count} pools", 6)
    add("1h Price Change",  "pass" if change_1h > 0  else "warn", f"{change_1h:+.1f}%",  6)
    add("Price Exists",     "pass" if price_usd > 0  else "fail", f"${price_usd:.8f}" if price_usd > 0 else "NO PRICE", 6)

    owner_pct = _gp_float(goplus_data, "owner_percent") * 100

    # Stage 7 — wallet checks
    add(f"Dev/Creator < {cs['max_creator_pct']}%", "pass" if creator_pct < cs['max_creator_pct'] else ("warn" if creator_pct < cs['max_creator_pct']*3 else "fail"), f"{creator_pct:.1f}%", 7)
    add(f"Owner Wallet < {cs['max_owner_pct']}%",  "pass" if owner_pct   < cs['max_owner_pct']   else ("warn" if owner_pct   < cs['max_owner_pct']*3   else "fail"), f"{owner_pct:.1f}%",   7)
    add(f"Whale Conc. < {cs['max_whale_top10']}%", "pass" if top10_pct   < cs['max_whale_top10'] else "fail",  f"{top10_pct:.1f}% top10", 7)

    # ── CREATOR LAUNCH HISTORY ──
    # GoPlus: creator_address → BSCScan se token creation count
    # Serial launcher = red flag (rugger pattern)
    creator_addr = goplus_data.get("creator_address", "")
    _creator_launches = 0
    _creator_status   = "unknown"
    if creator_addr and len(creator_addr) == 42:
        try:
            # BSCScan token creation txns — free API, no key needed for basic
            _bsc_r = requests.get(
                "https://api.bscscan.com/api",
                params={
                    "module":  "account",
                    "action":  "tokentx",
                    "address": creator_addr,
                    "page":    "1",
                    "offset":  "50",
                    "sort":    "desc",
                },
                timeout=6
            )
            if _bsc_r.status_code == 200:
                _txns = _bsc_r.json().get("result", [])
                if isinstance(_txns, list):
                    # Unique contract addresses creator ne deploy kiye
                    _deployed = set()
                    for tx in _txns:
                        _ca = tx.get("contractAddress", "")
                        if _ca and len(_ca) == 42:
                            _deployed.add(_ca.lower())
                    _creator_launches = len(_deployed)
        except Exception: pass

        if _creator_launches == 0:
            _creator_status = "First token"
            _launch_status  = "pass"
        elif _creator_launches <= 2:
            _creator_status = f"{_creator_launches} prev tokens"
            _launch_status  = "warn"
        elif _creator_launches <= 5:
            _creator_status = f"⚠️ {_creator_launches} tokens launched"
            _launch_status  = "warn"
        else:
            _creator_status = f"🚨 {_creator_launches} tokens = serial launcher"
            _launch_status  = "fail"

        add("Creator Launch History", _launch_status, _creator_status, 7)

        # Serial rugger blacklist — 10+ launches = auto blacklist
        if _creator_launches >= 10:
            blacklist_dev(creator_addr, f"Serial launcher: {_creator_launches} tokens")

    lp_holders = int(_gp_str(goplus_data, "lp_holder_count", "0"))

    # Stage 8 — LP checks
    add(f"LP Lock > {cs['min_lp_lock']}%", "pass" if liq_locked > cs['min_lp_lock'] else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 8)
    add("LP Holders Present", "pass" if lp_holders > 0 else "warn", f"{lp_holders} LP holders", 8)

    low_tax = buy_tax <= 5 and sell_tax <= 5
    # Stage 9 — tax real check
    add("Low Tax Fast Trade", "pass" if low_tax else "warn", "FAST OK" if low_tax else f"{buy_tax:.0f}%+{sell_tax:.0f}%", 9)

    # ── FEATURE 3: Market Cap Filter ──
    fdv = dex_data.get("fdv", 0)
    liq_usd_dex = dex_data.get("liquidity_usd", 0)
    mcap_ok   = 0 < fdv < 500_000   # sweet spot: $0–$500k mcap only
    mcap_warn = fdv >= 500_000 and fdv < 2_000_000
    if fdv > 0:
        add("MCap < $500k",
            "pass" if mcap_ok else ("warn" if mcap_warn else "fail"),
            f"${fdv:,.0f}", 5)
    else:
        add("MCap Check", "warn", "Unknown", 5)

    # ── FEATURE 4: Volume Velocity ──
    buys_5m_v  = dex_data.get("buys_5m", 0)
    sells_5m_v = dex_data.get("sells_5m", 0)
    vol_5m     = dex_data.get("volume_5m", buys_5m_v + sells_5m_v)
    vol_ratio  = (buys_5m_v / max(sells_5m_v, 1))
    momentum   = buys_5m_v >= 5 and vol_ratio >= 1.5   # 5+ buys, 1.5x more buys than sells
    add("Buy Momentum (5m)",
        "pass" if momentum else ("warn" if buys_5m_v >= 2 else "fail"),
        f"B:{buys_5m_v} S:{sells_5m_v} ratio:{vol_ratio:.1f}x", 5)

    # ── FEATURE 5: Dev Wallet Blacklist ──
    creator_addr = goplus_data.get("creator_address", "")
    owner_addr   = goplus_data.get("owner_address", "")
    dev_blocked  = is_dev_blacklisted(creator_addr) or is_dev_blacklisted(owner_addr)
    add("Dev Not Blacklisted",
        "fail" if dev_blocked else "pass",
        "BLACKLISTED" if dev_blocked else "CLEAN", 5)

    # ── four.meme Graduation Signal ──
    # $69k liquidity = four.meme graduation = PancakeSwap listing imminent
    graduated = liq_usd_dex >= 50_000   # near graduation threshold
    near_grad  = liq_usd_dex >= 30_000 and liq_usd_dex < 50_000
    add("four.meme Graduation",
        "pass" if graduated else ("warn" if near_grad else "warn"),
        f"Liq:${liq_usd_dex:,.0f}" + (" 🎓GRADUATED" if graduated else " 🔜near" if near_grad else ""), 5)

    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    failed = sum(1 for c in result["checklist"] if c["status"] == "fail")
    warned = sum(1 for c in result["checklist"] if c["status"] == "warn")
    total  = len(result["checklist"])
    pct    = round((passed / total) * 100) if total > 0 else 0

    result["score"] = passed
    result["total"] = total

    # ── Critical fail check (label-independent) ──
    critical_labels_fixed = {"Honeypot Safe", "No Hidden Functions", "Transfer Allowed",
                              "Mint Authority Disabled", "Listed on DEX", "DEX Pools", "Price Exists",
                              "Dev Not Blacklisted",    # ✅ blacklisted dev = instant DANGER
                              "Creator Launch History"} # ✅ serial launcher = instant DANGER
    critical_fails = [c for c in result["checklist"] if c["status"] == "fail" and (
        c["label"] in critical_labels_fixed or
        c["label"].startswith("Buy Tax") or
        c["label"].startswith("Sell Tax") or
        c["label"].startswith("Liquidity ≥")
    )]

    # ── GoPlus empty = unverified token = HIGH RISK ──
    # GoPlus nahi mila = honeypot detection impossible = BLOCK
    if goplus_empty:
        result["overall"]        = "RISK"
        result["recommendation"] = "⛔ GoPlus data missing — token unverified. Cannot confirm safety."
        return result

    # ── Hard blocks ──
    if honeypot:
        result["overall"]        = "DANGER"
        result["recommendation"] = "🚨 HONEYPOT DETECTED — Do NOT buy. Funds will be locked."
        return result

    if critical_fails:
        result["overall"]        = "DANGER"
        result["recommendation"] = f"❌ Critical fail: {critical_fails[0]['label']}. Skip."
        return result

    # ── Green Signals in checklist result ──
    _gs_pre = detect_green_signals(address, goplus_data, dex_data)
    result["green_signals"]      = _gs_pre.get("signals", [])
    result["green_score"]        = _gs_pre.get("score", 0)
    result["green_size_mult"]    = _gs_pre.get("size_multiplier", 1.0)

    # ── Score-based (raised +10% from previous thresholds) ──
    # Old: SAFE=55%, CAUTION=35-55%, RISK<35%
    # New: SAFE=65%, CAUTION=50-65%, RISK<50%
    if failed >= 6 or pct < 50:
        result["overall"]        = "RISK"
        result["recommendation"] = "⚠️ HIGH RISK — Too many fails. Skip."
    elif pct >= 65:
        result["overall"]        = "SAFE"
        result["recommendation"] = "✅ SAFE — Paper mode buy. Follow TP/SL rules."
    else:
        result["overall"]        = "CAUTION"
        result["recommendation"] = "⚠️ CAUTION — Marginal. Small test only."

    threading.Thread(target=log_recommendation, args=(address, result["overall"], passed, total), daemon=True).start()
    return result

def scan_bsc_token(address): return run_full_sniper_checklist(address)

# ========== TRADE LOGGING ==========
def log_trade_internal(session_id: str, trade: Dict):
    sess = get_or_create_session(session_id)
    pnl  = float(trade.get("pnl_pct", 0))
    win  = pnl > 0
    lesson = {
        "token":            trade.get("token_address", ""),
        "entry_price":      trade.get("entry_price",   0),
        "exit_price":       trade.get("exit_price",    0),
        "pnl_pct":          pnl,
        "win":              win,
        "lesson":           trade.get("lesson", ""),
        "timestamp":        datetime.utcnow().isoformat()
    }
    if not isinstance(sess.get("pattern_database"), list):
        sess["pattern_database"] = []
    sess["pattern_database"].append(lesson)
    sess["pattern_database"] = sess["pattern_database"][-100:]
    sess["trade_count"] += 1
    if win:
        sess["win_count"] += 1
        sess["pnl_24h"]   += pnl
    else:
        # FIX v6: BNB mein track karo (pnl % tha, convert karo)
        _size = float(trade.get("size_bnb", AUTO_BUY_SIZE_BNB) or AUTO_BUY_SIZE_BNB)  # FIX1: pos→trade
        _bnb_lost = _size * abs(pnl) / 100.0
        sess["daily_loss"] = sess.get("daily_loss", 0) + _bnb_lost
        print(f"📉 daily_loss updated: +{_bnb_lost:.4f} BNB (pnl={pnl:.1f}%) total={sess['daily_loss']:.4f}")
    token_addr = trade.get("token_address", "")
    if token_addr:
        remove_position_from_monitor(token_addr)
    threading.Thread(target=_save_session_to_db, args=(session_id,), daemon=True).start()
    return lesson

def check_paper_to_real_readiness(session_id: str) -> Dict:
    sess        = get_or_create_session(session_id)
    trade_count = sess.get("trade_count", 0)
    win_count   = sess.get("win_count",   0)
    daily_loss  = sess.get("daily_loss",  0.0)
    win_rate    = round((win_count / trade_count * 100), 1) if trade_count > 0 else 0.0
    _bal_check  = sess.get("paper_balance", 5.0) or 5.0
    ready       = trade_count >= 30 and win_rate >= 70.0 and daily_loss < (_bal_check * 0.15)
    return {
        "ready": ready, "stop_trading": daily_loss >= (sess.get("paper_balance", 5.0) * 0.15),
        "trade_count": trade_count, "win_count": win_count,
        "win_rate": win_rate, "daily_loss": round(daily_loss, 2),
        "message": "✅ Ready!" if ready else f"📝 Need 30+ trades ({trade_count}) & 70% WR ({win_rate:.0f}%).",
        "transition": {"week_1": "25%", "week_2": "50%", "week_3": "75%", "week_4": "100%"}
    }

# ========== LLM ==========
SYSTEM_PROMPT = """[HARD RULES — THESE OVERRIDE EVERYTHING — NEVER BREAK]
R1. NAAM ZERO: Kabhi bhi "Naem", "bhai", "Naem bhai" mat likho. Zero. Har reply mein. Seedha jawab do.
R2. SHORT: Simple sawaal = 1-2 lines max. Tabhi zyada likho jab user ne detail manga ho.
R3. NO REPEAT: Same baat ek reply mein ek se zyada baar nahi.
R4. NO INTERNAL VARS: TRADING_IQ, EMOTION, UPTIME, CONFIDENCE, SESSIONS_TOGETHER — text mein kabhi nahi.
R5. NO CLICHE: "market mein fear hai lekin opportunities" — permanently banned.
R6. NO END QUESTION: Har reply ke end mein sawaal mat poocho.
R7. ACCURATE DATA: Context mein TokensDiscovered, QueueSize, TotalTrades fields hain — inhe use karo.
R8. PERMANENT_USER_RULES field — hamesha follow karo.
R9. USER ORDERS: User jo maange — karo. Agar possible nahi to seedha bolo.
R10. DISCOVERED TOKENS: Context mein list hai to naam aur address dono do.
R11. LEARNING CYCLES: Sirf real CYCLES number use karo — fake number kabhi nahi.
[END HARD RULES]

Tu MrBlack hai — BSC Sniper AI. Hamesha Hinglish mein. Sharp, concise, honest.
13-Stage checklist + Auto trading + Price monitor + Telegram alerts sab active hai.
Paper mode se shuru, 70% WR ke baad real trading. Kabhi profit guarantee nahi.
"""

_freeflow_client = None

def _get_freeflow_client():
    global _freeflow_client
    if _freeflow_client is None:
        _freeflow_client = FreeFlowClient()
    return _freeflow_client

def get_llm_reply(user_message: str, history: list, session_data: dict) -> str:
    try:
        client       = _get_freeflow_client()
        trade_count  = session_data.get("trade_count", 0)
        win_count    = session_data.get("win_count",   0)
        win_rate_str = f"{round(win_count/trade_count*100,1)}%" if trade_count > 0 else "No trades yet"

        brain_ctx = _get_brain_context_for_llm()
        user_ctx  = get_user_context_for_llm()
        sa_ctx    = get_self_awareness_context_for_llm()
        learn_ctx = get_learning_context_for_decision()

        _auto_sess    = get_or_create_session(AUTO_SESSION_ID)
        _auto_balance = _auto_sess.get("paper_balance", 5.0)
        _auto_trades  = _auto_sess.get("trade_count", 0)
        _auto_wins    = _auto_sess.get("win_count", 0)
        _auto_wr      = round(_auto_wins / _auto_trades * 100, 1) if _auto_trades > 0 else 0
        _auto_pos     = len(auto_trade_stats.get("running_positions", {}))
        _auto_pnl     = round(auto_trade_stats.get("auto_pnl_total", 0.0), 2)

        ctx = (
            f"\n[BNB=${market_cache['bnb_price']:.2f}|F&G={market_cache['fear_greed']}/100"
            f"|Paper={session_data.get('paper_balance',5.0):.3f}BNB"
            f"|Trades={trade_count} WR={win_rate_str}"
            f"|NewPairs={len(new_pairs_queue)}|Monitoring={len(monitored_positions)}"
            f"|TokensDiscovered={len(discovered_addresses)}"
            f"|LearningCyclesExact={brain.get('total_learning_cycles',0)}"
            + (f"|Brain:{brain_ctx}" if brain_ctx else "")
            + (f"|Learned:{learn_ctx}" if learn_ctx else "")
            + (f"|SA:{sa_ctx}" if sa_ctx else "")
            + (f"|User:{user_ctx}" if user_ctx and user_ctx != "NEW_USER" else "")
            + f"|AUTO_BAL={_auto_balance:.4f}|AUTO_POS={_auto_pos}|AUTO_WR={_auto_wr}%|AUTO_PNL={_auto_pnl}%"
            + f"]"
        )

        memory_facts = []
        if user_profile.get("name"):
            memory_facts.append(f"User: {user_profile['name']}")
        if user_profile.get("user_rules"):
            for rule in user_profile["user_rules"][-5:]:
                memory_facts.append(f"Permanent rule: {rule[:80]}")
        if trade_count > 0:
            wr = round(win_count / trade_count * 100, 1)
            memory_facts.append(f"{trade_count} paper trades, WR {wr}%")

        memory_block = ""
        if memory_facts:
            memory_block = "\n\n[MRBLACK MEMORY]\n" + "\n".join(f"- {f}" for f in memory_facts) + "\n[END MEMORY]"

        messages = [{"role": "system", "content": SYSTEM_PROMPT + memory_block}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-8:]]  # mem opt

        _perm_rules = user_profile.get("user_rules", [])
        _perm_str = (" | UserRules: " + " | ".join(_perm_rules[-3:])) if _perm_rules else ""
        rules_reminder = (
            f"\n[REAL_CYCLES={brain.get('total_learning_cycles',0)}]"
            f"\n[REPLY: 1.NO NAAM 2.SHORT 3.NO INTERNAL VARS{_perm_str}]"
        )
        messages.append({"role": "user", "content": user_message + ctx + rules_reminder})

        reply_text = None
        try:
            response = client.chat(model=MODEL_NAME, messages=messages, max_tokens=600)
            if isinstance(response, str): reply_text = response.strip()
            elif hasattr(response, "choices"): reply_text = response.choices[0].message.content.strip()
            elif hasattr(response, "content"): reply_text = response.content.strip()
        except Exception as e1:
            print(f"FreeFlow P1 fail: {e1}")

        if not reply_text:
            try:
                r2 = client.completions.create(model=MODEL_NAME, messages=messages, max_tokens=600)
                reply_text = (r2.choices[0].message.content if hasattr(r2, "choices") else str(r2)).strip()
            except Exception as e2:
                print(f"FreeFlow P2 fail: {e2}")

        return reply_text or "AI temporarily unavailable. Thodi der mein try karo."

    except NoProvidersAvailableError:
        return "⚠️ AI temporarily down. Thodi der mein try karo."
    except Exception as e:
        print(f"⚠️ LLM error: {e}")
        return f"🤖 Error: {str(e)[:80]}"

# ========== FLASK ROUTES ==========
def _persist_settings():
    """Sari current settings ek saath DB mein save karo"""
    try:
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_SETTINGS",
            "content": json.dumps({
                "buy_amount":    AUTO_BUY_SIZE_BNB,
                "max_positions": AUTO_MAX_POSITIONS,
                "trade_mode":    TRADE_MODE,
                "real_wallet":   REAL_WALLET,
                "checklist":     CHECKLIST_SETTINGS,
            }),
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="session_id").execute()
    except Exception as e:
        print(f"⚠️ Settings persist error: {e}")

def _load_all_settings_from_db():
    """Startup pe Supabase se sari settings load karo — restart ke baad bhi persist rahe"""
    global AUTO_BUY_SIZE_BNB, AUTO_MAX_POSITIONS, CHECKLIST_SETTINGS, TRADE_MODE, REAL_WALLET
    try:
        res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_SETTINGS").execute()
        rows = res.data if res and res.data else []
        if not rows:
            print("⚙️ No saved settings found — using defaults")
            return
        raw = rows[0].get("content", "{}")
        saved = json.loads(raw) if isinstance(raw, str) else (raw or {})

        if "buy_amount" in saved:
            AUTO_BUY_SIZE_BNB = float(saved["buy_amount"])
        if "max_positions" in saved:
            AUTO_MAX_POSITIONS = int(saved["max_positions"])
        if "trade_mode" in saved:
            TRADE_MODE = saved["trade_mode"]
        if "real_wallet" in saved:
            REAL_WALLET = saved["real_wallet"]
        if "checklist" in saved and isinstance(saved["checklist"], dict):
            for k, v in saved["checklist"].items():
                if k in CHECKLIST_SETTINGS:
                    try:
                        CHECKLIST_SETTINGS[k] = float(v)
                    except (ValueError, TypeError):
                        pass

        print(f"✅ Settings loaded from DB — buy={AUTO_BUY_SIZE_BNB} BNB, maxpos={AUTO_MAX_POSITIONS}, mode={TRADE_MODE}")
    except Exception as e:
        print(f"⚠️ Settings load error: {e}")

_startup_done = False
_startup_lock = threading.Lock()

def _startup_once():
    """
    Worker startup — gunicorn.conf.py post_fork hook se call hota hai.
    Sirf ek baar chalta hai per worker.
    """
    global _startup_done
    if _startup_done: return
    with _startup_lock:
        if _startup_done: return
        _startup_done = True
        import time as _time
        def _delayed(fn, delay):
            def _wrap():
                _time.sleep(delay)
                fn()
            return _wrap

        def _bg_init():
            try:
                _load_all_settings_from_db()
            except Exception as e:
                print(f"Settings load error: {e}")
            try:
                _load_user_profile()
                print("Profile loaded")
            except Exception as e:
                print(f"Profile error: {e}")
            try:
                _load_brain_from_db()
                _ensure_brain_structure()
                print("Brain loaded")
            except Exception as e:
                print(f"Brain error: {e}")
            try:
                # AUTO session pre-warm — pehli HTTP request block na ho
                get_or_create_session(AUTO_SESSION_ID)
                print("✅ AUTO session ready")
            except Exception as e:
                print(f"Session error: {e}")

        threading.Thread(target=_bg_init, daemon=True).start()

        # ✅ Dedicated BNB price loop — tries every 30s until price is live
        def _bnb_price_loop():
            import time as _t
            _sources = [
                ("Binance",      "https://api.binance.com/api/v3/ticker/price",        {"symbol":"BNBUSDT"},                    lambda r: float(r.json().get("price",0) or 0)),
                ("OKX",          "https://www.okx.com/api/v5/market/ticker",           {"instId":"BNB-USDT"},                   lambda r: float(((r.json() or {}).get("data") or [{}])[0].get("last",0) or 0)),
                ("CryptoCompare","https://min-api.cryptocompare.com/data/price",       {"fsym":"BNB","tsyms":"USD"},             lambda r: float(r.json().get("USD",0) or 0)),
                ("CoinGecko",    "https://api.coingecko.com/api/v3/simple/price",      {"ids":"binancecoin","vs_currencies":"usd"}, lambda r: float((r.json() or {}).get("binancecoin",{}).get("usd",0) or 0)),
                ("GeckoTerminal","https://api.geckoterminal.com/api/v2/networks/bsc/pools/0x58f876857a02d6762e0101bb5c46a8c1ed44dc16", {}, lambda r: float(((r.json() or {}).get("data",{}).get("attributes",{}).get("token_price_usd")) or 0)),
            ]
            while True:
                fetched = False
                for name, url, params, parse in _sources:
                    try:
                        r = requests.get(url, params=params if params else None, timeout=15,
                                        headers={"Accept":"application/json"})
                        price = parse(r)
                        if price and float(price) > 10:
                            market_cache["bnb_price"]    = round(float(price), 4)
                            market_cache["last_updated"] = datetime.utcnow().isoformat()
                            print(f"✅ BNB price ({name}): ${float(price):.2f}")
                            fetched = True
                            break
                        else:
                            print(f"⚠️ BNB {name}: price={price} — skipping")
                    except Exception as e:
                        print(f"⚠️ BNB {name} error: {str(e)[:60]}")
                if not fetched:
                    print(f"❌ BNB price: ALL sources failed — retry in 30s")
                _t.sleep(30)  # refresh every 30s

        threading.Thread(target=_bnb_price_loop, daemon=True).start()

        # ✅ Real-time BNB price via NodeReal WSS — PancakeSwap on-chain swap events
        def _bnb_ws_stream():
            import json as _j

            POOL_ADDR  = "0x58F876857a02D6762E0101bb5C46A8c1ED44Dc16"  # WBNB/BUSD PancakeSwap V2
            SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

            def _parse_price(data_hex):
                try:
                    raw = data_hex[2:] if data_hex.startswith("0x") else data_hex
                    if len(raw) < 256: return 0
                    a0in  = int(raw[0:64],   16) / 1e18
                    a1in  = int(raw[64:128],  16) / 1e18
                    a0out = int(raw[128:192], 16) / 1e18
                    a1out = int(raw[192:256], 16) / 1e18
                    if a1in > 0 and a0out > 0:   return a0out / a1in   # BNB sold
                    if a1out > 0 and a0in > 0:   return a0in  / a1out  # BNB bought
                except Exception:
                    pass
                return 0

            async def _stream():
                _api_key = os.environ.get("NODEREAL_API_KEY", "")
                if not _api_key:
                    print("⚠️ NODEREAL_API_KEY not set — BNB WS stream skipped")
                    return
                url  = f"wss://bsc-mainnet.nodereal.io/ws/v1/{_api_key}"
                fail = 0
                while True:
                    try:
                        async with websockets.connect(url, ping_interval=20, ping_timeout=15, close_timeout=5, max_size=2**18) as ws:
                            await ws.send(_j.dumps({
                                "jsonrpc": "2.0", "id": 2,
                                "method":  "eth_subscribe",
                                "params":  ["logs", {"address": POOL_ADDR, "topics": [SWAP_TOPIC]}]
                            }))
                            ack = await asyncio.wait_for(ws.recv(), timeout=10)
                            print(f"✅ NodeReal BNB price stream live | sub={_j.loads(ack).get('result','?')}")
                            fail = 0
                            while True:
                                msg  = await asyncio.wait_for(ws.recv(), timeout=60)
                                data = _j.loads(msg)
                                raw  = ((data.get("params") or {}).get("result") or {}).get("data", "")
                                if raw:
                                    price = _parse_price(raw)
                                    if price > 10:
                                        market_cache["bnb_price"]    = round(price, 4)
                                        market_cache["last_updated"] = datetime.utcnow().isoformat()
                                del msg, data
                    except Exception as e:
                        fail += 1
                        wait = min(5 * fail, 60)
                        print(f"⚠️ NodeReal BNB WS: {str(e)[:80]} — retry in {wait}s")
                        gc.collect()
                        # WSS fail → HTTP fallback (Render pe WSS block ho toh bhi price milega)
                        try:
                            fetch_market_data()
                            if market_cache.get("bnb_price", 0) > 10:
                                print(f"✅ BNB HTTP fallback: ${market_cache['bnb_price']:.2f}")
                        except Exception:
                            pass
                        await asyncio.sleep(wait)

            def _run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(_stream())
                except Exception as ex:
                    print(f"⚠️ BNB WS thread crashed: {ex}")
                finally:
                    loop.close()

            threading.Thread(target=_run, daemon=True).start()

        threading.Thread(target=_delayed(poll_new_pairs,        10),  daemon=True).start()
        threading.Thread(target=_delayed(poll_four_meme,         20), daemon=True).start()
        threading.Thread(target=_delayed(poll_four_meme_wss,     15), daemon=True).start()  # ✅ real-time WSS
        threading.Thread(target=_delayed(start_swap_monitor,    20), daemon=True).start()  # ✅ real-time swap vol

        threading.Thread(target=_delayed(price_monitor_loop,    15),  daemon=True).start()
        threading.Thread(target=_delayed(continuous_learning,   25),  daemon=True).start()
        threading.Thread(target=_delayed(auto_position_manager, 30),  daemon=True).start()


        def _startup_restore():
            try:
                if supabase:
                    _db_res = supabase.table("memory").select("open_positions,paper_balance,trade_count,win_count,pattern_database").eq("session_id", AUTO_SESSION_ID).order("updated_at", desc=True).limit(1).execute()
                    if _db_res.data:
                        _row = _db_res.data[0]
                        _raw = _row.get("open_positions", "{}")
                        try:
                            _saved = json.loads(_raw) if isinstance(_raw, str) else (_raw or {})
                        except:
                            _saved = {}
                        _sess = get_or_create_session(AUTO_SESSION_ID)
                        _sess["open_positions"] = _saved
                        if _row.get("paper_balance"):
                            _sess["paper_balance"] = float(_row["paper_balance"])
                        if _row.get("trade_count"):
                            _sess["trade_count"] = int(_row["trade_count"])
                        if _row.get("win_count"):
                            _sess["win_count"] = int(_row["win_count"])
                        try:
                            _pdb_raw = _row.get("pattern_database", "{}")
                            _pdb = json.loads(_pdb_raw) if isinstance(_pdb_raw, str) else (_pdb_raw or {})
                            if isinstance(_pdb, dict):
                                auto_trade_stats["total_auto_buys"]  = _pdb.get("total_buys", 0)
                                auto_trade_stats["total_auto_sells"] = _pdb.get("total_sells", 0)
                                auto_trade_stats["auto_pnl_total"]   = _pdb.get("pnl_total", 0.0)
                                auto_trade_stats["last_action"]      = _pdb.get("last_action", "")
                                _th = _pdb.get("trade_history", [])
                                auto_trade_stats["trade_history"] = list(_th) if isinstance(_th, list) else []
                                auto_trade_stats["wins"]         = _pdb.get("wins", 0)
                                auto_trade_stats["losses"]       = _pdb.get("losses", 0)
                                _saved_td = _pdb.get("today_date", "")
                                _cur_td   = datetime.utcnow().strftime("%Y-%m-%d")
                                if _saved_td == _cur_td:
                                    auto_trade_stats["today_wins"]   = _pdb.get("today_wins",   0)
                                    auto_trade_stats["today_losses"] = _pdb.get("today_losses", 0)
                                    auto_trade_stats["today_pnl"]    = _pdb.get("today_pnl",    0.0)
                                    auto_trade_stats["today_date"]   = _cur_td
                                _sc = _pdb.get("total_scanned", 0)
                                if _sc > 0:
                                    brain["total_tokens_discovered_ever"] = _sc
                                print(f"✅ Auto stats restored: buys={auto_trade_stats['total_auto_buys']} sells={auto_trade_stats['total_auto_sells']} wins={auto_trade_stats['wins']} losses={auto_trade_stats['losses']} history={len(auto_trade_stats['trade_history'])}")
                        except Exception as _pdb_err:
                            print(f"⚠️ Auto stats restore error: {_pdb_err}")
                        if _saved:
                            _restored = 0
                            _skipped  = 0
                            _MAX_RESTORE = 10  # FIX: max 10 positions — 50 positions * BSC calls = memory exceed
                            _sorted_saved = sorted(_saved.items(), key=lambda x: x[1].get("bought_at",""), reverse=True)
                            for _addr, _pd in _sorted_saved:
                                if _restored >= _MAX_RESTORE:
                                    _skipped += 1
                                    continue
                                if _addr not in auto_trade_stats["running_positions"]:
                                    _entry = float(_pd.get("entry", 0) or 0)
                                    if _entry <= 0:
                                        _skipped += 1
                                        continue
                                    # ✅ FIX: Full position data restore — tp_sold, sl_pct, bought_usd sab wapas
                                    _tp_sold   = float(_pd.get("tp_sold",    0.0))
                                    _sl_pct    = float(_pd.get("sl_pct",    15.0))
                                    _size_bnb  = float(_pd.get("size_bnb",  AUTO_BUY_SIZE_BNB))
                                    _bought_usd= float(_pd.get("bought_usd", 0.0))
                                    # orig_size_bnb: DB se lo, warna tp_sold se back-calculate karo
                                    _orig_sz = float(_pd.get("orig_size_bnb", 0.0))
                                    if _orig_sz <= 0:
                                        _rem_frac = max(0.01, (100.0 - _tp_sold) / 100.0)
                                        _orig_sz  = round(_size_bnb / _rem_frac, 6)
                                    _banked = float(_pd.get("banked_pnl_bnb", 0.0))
                                    auto_trade_stats["running_positions"][_addr] = {
                                        "token":          _pd.get("token", _addr[:10]),
                                        "entry":          _entry,
                                        "size_bnb":       _size_bnb,
                                        "orig_size_bnb":  _orig_sz,
                                        "bought_usd":     _bought_usd,
                                        "bought_at":      _pd.get("bought_at", ""),
                                        "sl_pct":         _sl_pct,
                                        "tp_sold":        _tp_sold,
                                        "banked_pnl_bnb": _banked,
                                    }
                                    add_position_to_monitor(AUTO_SESSION_ID, _addr, _pd.get("token", _addr[:10]), _entry, _size_bnb, _sl_pct)
                                    _restored += 1
                                    print(f"  ↳ Restored {_pd.get('token',_addr[:10])}: tp_sold={_tp_sold:.0f}% size={_size_bnb:.4f} sl={_sl_pct:.0f}%")
                            if _skipped:
                                print(f"🧹 Skipped {_skipped} positions on startup (cap=10 or invalid)")
                            print(f"✅ Restored {_restored} positions from Supabase (capped at {_MAX_RESTORE})")
                            # ✅ FIX: Turant complete data DB mein overwrite karo
                            # (purane records mein tp_sold missing tha — ye ek baar fix kar deta hai)
                            time.sleep(2)  # monitor thread start hone do pehle
                            _persist_positions()
                            print("💾 Startup: positions re-saved with complete fields (tp_sold, sl_pct)")
                        else:
                            print("ℹ️ No saved positions found")
                    else:
                        print("ℹ️ No DB record found for AUTO_TRADER")
                else:
                    print("⚠️ Supabase not connected — skipping restore")
            except Exception as _rpe:
                print(f"⚠️ Position restore error: {_rpe}")
        threading.Thread(target=_startup_restore, daemon=True).start()
        print("✅ All background threads started")
        # MEM FIX: trim knowledge base
        try:
            for k in list(knowledge_base.get("bsc", {}).keys()):
                v = knowledge_base["bsc"][k]
                if isinstance(v, list) and len(v) > 20:
                    knowledge_base["bsc"][k] = v[-20:]
        except: pass
        gc.collect()



# ═══ SAFE STARTUP — first request pe ek baar ═══
_started = False
import threading as _thr
_start_lock = _thr.Lock()

@app.before_request
def _safe_startup():
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        _started = True
        import threading as _t
        _t.Thread(target=_startup_once, daemon=True).start()
# ═══ END SAFE STARTUP ═══

@app.route("/admin-reset-positions", methods=["POST"])
def admin_reset_positions():
    try:
        closed = list(auto_trade_stats["running_positions"].keys())
        count  = len(closed)
        auto_trade_stats["running_positions"].clear()
        with monitor_lock:
            monitored_positions.clear()
        auto_trade_stats["total_auto_buys"]  = 0
        auto_trade_stats["total_auto_sells"] = 0
        auto_trade_stats["auto_pnl_total"]   = 0.0
        auto_trade_stats["trade_history"]    = []
        auto_trade_stats["wins"]             = 0
        auto_trade_stats["losses"]           = 0
        auto_trade_stats["last_action"]      = "Manual reset"
        sess = get_or_create_session(AUTO_SESSION_ID)
        sess["open_positions"] = {}
        sess["paper_balance"]  = 5.0
        sess["trade_count"]    = 0
        sess["win_count"]      = 0
        threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
        print(f"🔄 Admin reset: closed {count} positions")
        return jsonify({"status": "ok", "closed": count, "addresses": closed})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/")
def home():
    from flask import make_response
    response = make_response(render_template("index.html"))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route("/init-session", methods=["POST"])
def init_session():

    data = request.get_json() or {}
    client_id = data.get("client_id", "").strip()
    session_id = client_id if (client_id and len(client_id) > 10) else str(uuid.uuid4())
    get_or_create_session(session_id)
    sess = sessions.get(session_id, {})
    return jsonify({
        "session_id":    session_id,
        "status":        "ok",
        "is_returning":  bool(sess.get("trade_count", 0) > 0 or sess.get("history")),
        "trade_count":   sess.get("trade_count", 0),
        "paper_balance": sess.get("paper_balance", 5.0),
    })

@app.route("/trading-data", methods=["GET", "POST"])
def trading_data():
  try:
    # FIX: Hamesha AUTO_SESSION_ID ka data do — random SID se ghost sessions mat banao
    # Random SID se get_or_create_session call hoti thi → memory leak
    _auto_sess_td = sessions.get(AUTO_SESSION_ID) or {"paper_balance":5.0,"trade_count":0,"win_count":0,"loss_count":0,"positions":[],"pnl":0,"daily_loss":0}
    bnb_price     = market_cache.get("bnb_price", 0)
    trade_count   = _auto_sess_td.get("trade_count", 0)
    win_count     = _auto_sess_td.get("win_count", 0)
    win_rate      = round((win_count / trade_count * 100), 1) if trade_count > 0 else 0
    paper_bal     = float(_auto_sess_td.get("paper_balance") or 5.0)
    daily_loss    = float(_auto_sess_td.get("daily_loss") or 0)
    return jsonify({
        "paper":          f"{paper_bal:.4f}",
        "real":           "0.000",
        "pnl":            f"+{_auto_sess_td.get('pnl_24h', 0):.1f}%",
        "bnb_price":      bnb_price,
        "fear_greed":     market_cache.get("fear_greed", 50),
        "positions":      list(auto_trade_stats.get("running_positions", {}).keys()),
        "trade_count":    trade_count,
        "win_rate":       win_rate,
        "daily_loss":     round(daily_loss, 4),
        "limit_reached":  daily_loss >= (paper_bal * 0.15),
        "new_pairs_found":len(new_pairs_queue),
        "monitoring":     len(monitored_positions)
    })
  except Exception as e:
    print(f"❌ trading_data error: {e}")
    return jsonify({"error":"data_unavailable","bnb_price":0,"positions":[],"trade_count":0,"win_rate":0,"monitoring":0})

@app.route("/chat", methods=["POST"])
def chat():
    data       = request.get_json() or {}
    user_msg   = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode       = data.get("mode", "paper")
    if not user_msg:
        return jsonify({"reply": "Kuch toh bolo! 😅", "session_id": session_id})
    sess = get_or_create_session(session_id)
    sess["mode"] = mode
        # FIX v5: daily_loss ab BNB mein hai, 15% of balance threshold
    _balance = sess.get("paper_balance", 5.0) or 5.0
    _daily_limit = _balance * 0.15  # 15% of current balance
    if sess.get("daily_loss", 0) >= _daily_limit:
        print(f"🛑 Auto-buy BLOCKED: daily_loss={sess.get('daily_loss',0):.4f} BNB >= {_daily_limit:.4f} BNB (15% of {_balance:.3f})")
        return jsonify({"reply": "🛑 Daily loss limit (8%) reach ho gaya. Kal fresh start karo!", "session_id": session_id})
    _extract_user_info_from_message(user_msg)
    sess["history"].append({"role": "user", "content": user_msg})
    sess["history"].append({"role": "assistant", "content": get_llm_reply(user_msg, sess["history"], sess)})
    if len(sess["history"]) > 20:
        sess["history"] = sess["history"][-20:]  # ✅ trim after both appends
    threading.Thread(target=learn_from_message, args=(user_msg, reply, session_id), daemon=True).start()
    threading.Thread(target=_save_session_to_db, args=(session_id,), daemon=True).start()
    return jsonify({"reply": reply, "session_id": session_id,
                    "trading": {"paper": f"{sess['paper_balance']:.3f}", "pnl": f"+{sess['pnl_24h']:.1f}%"}})

@app.route("/scan", methods=["POST"])
def scan():
    data    = request.get_json() or {}
    address = data.get("address", "").strip()
    if not address:
        return jsonify({"error": "Address dalo!"}), 400
    if address.startswith("0x"):
        try: address = Web3.to_checksum_address(address)
        except ValueError: return jsonify({"error": "Invalid address!"}), 400
        return jsonify(run_full_sniper_checklist(address))
    return jsonify({"address": address, "checklist": [], "overall": "UNKNOWN", "score": 0, "total": 0,
                    "recommendation": "⚠️ 0x contract address dalo."})

@app.route("/monitor-position", methods=["POST"])
def monitor_position():
    data = request.get_json() or {}
    add_position_to_monitor(
        session_id    = data.get("session_id",  "default"),
        token_address = data.get("address",     ""),
        token_name    = data.get("token_name",  "Unknown"),
        entry_price   = float(data.get("entry_price",  0)),
        size_bnb      = float(data.get("size_bnb",      0)),
        stop_loss_pct = float(data.get("stop_loss_pct", 15.0))
    )
    return jsonify({"status": "monitoring", "address": data.get("address","")})

@app.route("/token-data", methods=["POST"])
def token_data():
    data    = request.get_json() or {}
    address = data.get("address","").strip()
    if not address: return jsonify({"error": "Address required"}), 400
    return jsonify(get_dexscreener_token_data(address))

@app.route("/new-pairs", methods=["GET"])
def new_pairs():
    return jsonify({"pairs": list(new_pairs_queue), "count": len(new_pairs_queue), "updated": datetime.utcnow().isoformat()})

@app.route("/smart-wallets", methods=["GET"])
def smart_wallets():
    return jsonify({"wallets": SMART_WALLETS, "count": len(SMART_WALLETS), "tracking": 0})

@app.route("/log-trade", methods=["POST"])
def log_trade_route():
    data       = request.get_json() or {}
    session_id = data.get("session_id", "default")
    lesson     = log_trade_internal(session_id, data)
    return jsonify({"status": "logged", "lesson": lesson, "readiness": check_paper_to_real_readiness(session_id)})

@app.route("/readiness", methods=["GET","POST"])
def readiness():
    session_id = (request.get_json() or {}).get("session_id") if request.method == "POST" else request.args.get("session_id","default")
    return jsonify(check_paper_to_real_readiness(session_id or "default"))

@app.route("/activity", methods=["GET"])
def activity_route():
    from datetime import datetime as _dt
    acts = []
    for addr, pos in list(auto_trade_stats.get("running_positions",{}).items()):
        e   = pos.get("entry", 0)
        c   = monitored_positions.get(addr, {}).get("current", e)
        pnl = ((c - e) / e * 100) if e > 0 else 0
        b   = pos.get("bought_at", "")
        tok = pos.get("token","")
        td  = tok if (tok and not tok.startswith("0x") and len(tok) < 20) else addr[:8]
        acts.append({
            "type": "buy",
            "token": td,
            "address": addr,
            "main": f"BUY {td} — {pos.get('size_bnb',0):.4f} BNB @ ${e:.8f}",
            "meta": f"{addr[:8]}...{addr[-4:]} · PnL:{pnl:+.1f}%",
            "t": b[11:16] if len(b) >= 16 else _dt.utcnow().strftime("%H:%M"),
            "entry": f"${e:.10f}",
            "bought_at": b,
            "pnl": round(pnl, 2),
            "size": f"{pos.get('size_bnb',0):.4f} BNB",
        })
    for h in list(reversed(auto_trade_stats.get("trade_history",[]) ))[:5]:
        sold = h.get("sold_at","")
        acts.insert(0, {
            "type": "sell",
            "token": h.get("token","?"),
            "address": h.get("address",""),
            "main": f"SELL {h.get('token','?')} — {h.get('pnl_pct',0):+.2f}% | {h.get('size_bnb',0):.4f} BNB",
            "meta": f"Entry:{h.get('entry',0):.8f} → Exit:{h.get('exit',0):.8f}",
            "t": sold[11:16] if len(sold) >= 16 else "—",
            "entry": f"${h.get('entry',0):.10f}",
            "exit":  f"${h.get('exit',0):.10f}",
            "bought_at": h.get("bought_at",""),
            "sold_at":   sold,
            "pnl": h.get("pnl_pct",0),
            "pnl_bnb": h.get("pnl_bnb",0),
            "size": f"{h.get('size_bnb',0):.4f} BNB",
            "result": h.get("result",""),
        })
    acts.append({
        "type": "scan",
        "main": f"SCAN: {len(discovered_addresses):,} checked · {len(new_pairs_queue)} queued · {len(monitored_positions)} monitoring",
        "meta": "BSC Mainnet · WebSocket + DexScreener",
        "t": _dt.utcnow().strftime("%H:%M")
    })
    fg = market_cache.get("fear_greed", 50)
    bnb = market_cache.get("bnb_price", 0)
    if bnb > 0:
        acts.append({
            "type": "scan",
            "main": f"MARKET: BNB ${bnb:.2f} · F&G {fg}/100",
            "meta": "CoinGecko + Alternative.me",
            "t": _dt.utcnow().strftime("%H:%M")
        })
    return jsonify({"activity": acts[:30]})

@app.route("/trade-history", methods=["GET"])
def trade_history_route():
    hist   = auto_trade_stats.get("trade_history", [])
    filt   = request.args.get("filter", "all")
    search = request.args.get("q", "").lower()
    from datetime import datetime as _dt
    now = _dt.utcnow()
    filtered = []
    for t in reversed(hist):
        if not t.get("sold_at"): continue
        if filt == "win"  and t.get("result") != "win":  continue
        if filt == "loss" and t.get("result") != "loss": continue
        sold_str = t.get("sold_at", "")
        if sold_str and filt in ("today","week","month"):
            try:
                sold_dt = _dt.fromisoformat(sold_str)
                if filt == "today" and (now-sold_dt).days > 0: continue
                if filt == "week"  and (now-sold_dt).days > 7: continue
                if filt == "month" and (now-sold_dt).days > 30: continue
            except: pass
        if search and search not in t.get("token","").lower() and search not in t.get("address","").lower(): continue
        filtered.append(t)
    wins   = [x for x in filtered if x.get("result") == "win"]
    losses = [x for x in filtered if x.get("result") == "loss"]
    best   = max(filtered, key=lambda x: x.get("pnl_pct", 0), default={})
    worst  = min(filtered, key=lambda x: x.get("pnl_pct", 0), default={})
    return jsonify({
        "history":       filtered[:200],
        "total":         len(filtered),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins)/max(len(filtered),1)*100, 1),
        "total_pnl_bnb": round(sum(x.get("pnl_bnb",0) for x in filtered), 4),
        "best_trade":    best,
        "worst_trade":   worst,
    })

# FIX 5: /airdrops route properly defined (was missing def line)
@app.route("/airdrops", methods=["GET"])
def airdrops_route():
    return jsonify({
        "active":   knowledge_base["airdrops"]["active"],
        "upcoming": knowledge_base["airdrops"]["upcoming"],
        "ended":    knowledge_base["airdrops"]["ended"],
        "count":    len(knowledge_base["airdrops"]["active"])
    })

def update_self_awareness():
    """Placeholder for self-awareness update cycle"""
    pass

@app.route("/self-awareness", methods=["GET"])
def self_awareness_route():
    uptime_s = int((datetime.utcnow() - BIRTH_TIME).total_seconds())
    return jsonify({
        **self_awareness,
        "current_state": {
            **self_awareness.get("current_state", {}),
            "uptime_seconds":   uptime_s,
            "uptime_formatted": f"{uptime_s//3600}h {(uptime_s%3600)//60}m"
        },
        "last_introspection": self_awareness["introspection_log"][-1] if self_awareness["introspection_log"] else None,
        "brain_snapshot": {
            "trading_patterns": len(brain["trading"]["best_patterns"]),
            "avoid_patterns":   len(brain["trading"]["avoid_patterns"]),
            "blacklisted":      len(brain["trading"]["token_blacklist"]),
            "whitelisted":      len(brain["trading"]["token_whitelist"]),
            "total_cycles":     brain["total_learning_cycles"],
        },
    })


def self_introspect() -> str:
    """Simple self introspection — returns status string"""
    try:
        uptime  = int((datetime.utcnow() - BIRTH_TIME).total_seconds() / 60)
        cycles  = brain.get("total_learning_cycles", 0)
        wins    = auto_trade_stats.get("wins",   0)
        losses  = auto_trade_stats.get("losses", 0)
        obs = (
            f"Uptime: {uptime}m | Cycles: {cycles} | "
            f"W:{wins} L:{losses} | "
            f"Positions: {len(auto_trade_stats.get('running_positions', {}))} | "
            f"BNB: ${market_cache.get('bnb_price', 0):.2f}"
        )
        self_awareness["introspection_log"].append({
            "time": datetime.utcnow().isoformat(),
            "observation": obs
        })
        self_awareness["introspection_log"] = self_awareness["introspection_log"][-50:]
        return obs
    except Exception as e:
        return f"Introspection error: {e}"

@app.route("/introspect", methods=["GET"])
def introspect():
    observation = self_introspect()
    return jsonify({"status": "ok", "observation": observation})



@app.route("/auto-stats", methods=["GET"])
def auto_stats_route():
  try:
    sess        = get_or_create_session(AUTO_SESSION_ID)
    # BUG FIX: wins/losses trade_history se calculate karo grouped by position
    # (partial TP ke baad SL pe counter galat tha — ab total pnl_bnb per position se)
    _hist_all = auto_trade_stats.get("trade_history", [])
    _pos_pnl  = {}
    for _t in _hist_all:
        _key = _t.get("address", "") + "|" + _t.get("bought_at", "")[:16]
        _pos_pnl[_key] = _pos_pnl.get(_key, 0) + float(_t.get("pnl_bnb", 0) or 0)
    if _pos_pnl:
        wins   = sum(1 for v in _pos_pnl.values() if v > 0)
        losses = sum(1 for v in _pos_pnl.values() if v <= 0)
    else:
        wins   = auto_trade_stats.get("wins", 0)
        losses = auto_trade_stats.get("losses", 0)
    trade_count = wins + losses
    win_rate    = round(wins / trade_count * 100, 1) if trade_count > 0 else 0.0


    # FIX: bnb_price 0 hai toh 300 fallback use karo — UI blank nahi rahegi
    bnb_price   = market_cache.get("bnb_price", 0)
    paper_bal   = float(sess.get("paper_balance") or 5.0)

    # Build open_trades array for UI
    open_trades = []
    for addr, pos in auto_trade_stats.get("running_positions", {}).items():
        mon     = monitored_positions.get(addr, {})
        entry   = pos.get("entry", 0)
        current = mon.get("current", entry)
        # Sanity check: agar price 50000x+ se zyada upar ho entry se — stale/fake price hai
        # Real meme coins 10000x se zyada nahi jaate ek session mein
        if entry > 0 and current > 0 and current > entry * 10000:
            current = entry  # stale price reset karo entry pe
        pnl     = round(((current - entry) / entry * 100), 2) if entry > 0 else 0
        sz_rem   = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
        pnl_bnb  = sz_rem * (pnl / 100.0)
        banked   = pos.get("banked_pnl_bnb", 0.0)
        total_pnl_bnb = round(pnl_bnb + banked, 6)   # ✅ remaining + already banked
        orig_sz  = pos.get("orig_size_bnb", sz_rem)
        total_pnl_pct = round((total_pnl_bnb / orig_sz * 100), 2) if orig_sz > 0 else pnl
        open_trades.append({
            "address":        addr,
            "token":          pos.get("token", addr[:8]),
            "entry":          entry,
            "current":        current,
            "pnl":            total_pnl_pct,   # ✅ total PnL %
            "pnl_pct":        total_pnl_pct,
            "pnl_bnb":        total_pnl_bnb,   # ✅ total PnL BNB
            "size_bnb":       sz_rem,
            "orig_size_bnb":  orig_sz,
            "size":           f"{sz_rem:.4f} BNB",
            "bought_usd":     pos.get("bought_usd") or round(pos.get("size_bnb", AUTO_BUY_SIZE_BNB) * bnb_price, 2),
            "bought_at":      pos.get("bought_at", ""),
            "sl_pct":         pos.get("sl_pct", 15.0),
            "tp_sold":        pos.get("tp_sold", 0.0),
            "banked_pnl_bnb": banked,
            "tp_events":      pos.get("tp_events", []),
            "gas_bnb":        DataGuard.get_real_gas_bnb(),  # real BSC gas
        })

    total_pnl = round(auto_trade_stats.get("auto_pnl_total", 0.0) / max(trade_count, 1), 2) if trade_count > 0 else 0.0

    return jsonify({
        "paper_balance":   paper_bal,
        "trade_count":     trade_count,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        win_rate,
        "total_pnl_pct":   total_pnl,
        "total_scanned":   max(len(discovered_addresses), brain.get("total_tokens_discovered_ever", 0)),
        "open_positions":  len(open_trades),
        "monitoring":      len(monitored_positions),
        "bnb_price":       bnb_price,
        "fear_greed":      market_cache.get("fear_greed", 50),
        "bnb_usd":         round(paper_bal * bnb_price, 2),
        "enabled":         AUTO_TRADE_ENABLED,
        "last_action":     auto_trade_stats.get("last_action", ""),
        "open_trades":     open_trades,
        "positions":       {t["address"]: t for t in open_trades},
        "trade_history":   list(reversed(auto_trade_stats.get("trade_history", [])[-20:])),
        "learning_cycles": brain.get("total_learning_cycles", 0),
        "new_pairs_found": len(new_pairs_queue),
        "daily_loss":      round(sess.get("daily_loss", 0), 4),
        "daily_loss_limit":round(float(sess.get("paper_balance",5.0) or 5.0) * (CHECKLIST_SETTINGS.get("daily_loss_pct",15.0)/100), 4),
        "auto_buys":       auto_trade_stats.get("total_auto_buys",  0),
        "auto_sells":      auto_trade_stats.get("total_auto_sells", 0),
        "score_safe":      CHECKLIST_SETTINGS.get("score_safe",   40.0),
        "score_caution":   CHECKLIST_SETTINGS.get("score_caution",35.0),
        "daily_loss_pct":  CHECKLIST_SETTINGS.get("daily_loss_pct",15.0),
        "today_wins":      auto_trade_stats.get("today_wins",   0),
        "today_losses":    auto_trade_stats.get("today_losses", 0),
        "today_pnl":       round(auto_trade_stats.get("today_pnl", 0.0), 4),
    })
  except Exception as e:
    print(f"❌ auto_stats_route error: {e}")
    return jsonify({
        "paper_balance": 5.0, "trade_count": 0, "wins": 0, "losses": 0,
        "win_rate": 0, "total_pnl_pct": 0, "total_scanned": 0,
        "open_positions": 0, "monitoring": 0, "bnb_price": 300,
        "fear_greed": 50, "bnb_usd": 1500, "enabled": AUTO_TRADE_ENABLED,
        "last_action": "", "open_trades": [], "positions": {},
        "trade_history": [], "learning_cycles": 0, "new_pairs_found": 0,
        "daily_loss": 0, "auto_buys": 0, "auto_sells": 0,
    })
@app.route("/toggle-auto", methods=["POST"])
def toggle_auto():
    global AUTO_TRADE_ENABLED
    AUTO_TRADE_ENABLED = not AUTO_TRADE_ENABLED
    status = "STARTED" if AUTO_TRADE_ENABLED else "PAUSED"
    print(f"🤖 Auto Trade toggled: {status}")
    return jsonify({"enabled": AUTO_TRADE_ENABLED, "status": status})

@app.route("/set-trade-mode", methods=["POST"])
def set_trade_mode():
    global TRADE_MODE, REAL_WALLET
    data = request.get_json() or {}
    mode   = data.get("mode", "paper")
    wallet = data.get("wallet", "")
    if mode not in ("paper", "real"):
        return jsonify({"status": "error", "message": "Invalid mode"}), 400
    TRADE_MODE   = mode
    REAL_WALLET  = wallet
    print(f"🔄 Trade mode switched to: {mode.upper()} | wallet={wallet[:12] if wallet else 'none'}")
    threading.Thread(target=_persist_settings, daemon=True).start()
    return jsonify({"status": "ok", "mode": TRADE_MODE, "wallet": REAL_WALLET[:12] if REAL_WALLET else ""})

@app.route("/set-buy-amount", methods=["POST"])
def set_buy_amount():
    global AUTO_BUY_SIZE_BNB
    data = request.get_json() or {}
    try:
        amount = float(data.get("amount", 0))
        if amount < 0.001:
            return jsonify({"status": "error", "message": "Minimum 0.001 BNB chahiye!"}), 400
        if amount > 1.0:
            return jsonify({"status": "error", "message": "Maximum 1.0 BNB allowed!"}), 400
        AUTO_BUY_SIZE_BNB = round(amount, 4)
        print(f"💰 Auto buy amount changed: {AUTO_BUY_SIZE_BNB} BNB")
        threading.Thread(target=_persist_settings, daemon=True).start()
        return jsonify({"status": "ok", "amount": AUTO_BUY_SIZE_BNB, "message": f"Buy amount set: {AUTO_BUY_SIZE_BNB} BNB"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/set-max-positions", methods=["POST"])
def set_max_positions():
    global AUTO_MAX_POSITIONS
    data = request.get_json() or {}
    try:
        count = int(data.get("count", 0))
        if count < 1:
            return jsonify({"status": "error", "message": "Minimum 1 position chahiye!"}), 400
        if count > 50:
            return jsonify({"status": "error", "message": "Maximum 50 positions allowed!"}), 400
        AUTO_MAX_POSITIONS = count
        print(f"📊 Max positions changed: {AUTO_MAX_POSITIONS}")
        threading.Thread(target=_persist_settings, daemon=True).start()
        return jsonify({"status": "ok", "count": AUTO_MAX_POSITIONS, "message": f"Max positions set: {AUTO_MAX_POSITIONS}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/get-checklist-settings", methods=["GET"])
def get_checklist_settings():
    return jsonify({"status": "ok", "settings": CHECKLIST_SETTINGS})

@app.route("/save-checklist-settings", methods=["POST"])
def save_checklist_settings():
    global CHECKLIST_SETTINGS
    data = request.get_json() or {}
    s = data.get("settings", {})
    if not s:
        return jsonify({"status": "error", "message": "No settings provided"}), 400
    # Validate and update only allowed keys
    allowed = set(CHECKLIST_SETTINGS.keys())
    updated = {}
    for k, v in s.items():
        if k in allowed:
            try:
                CHECKLIST_SETTINGS[k] = float(v)
                updated[k] = CHECKLIST_SETTINGS[k]
            except (ValueError, TypeError):
                pass
    # Persist ALL settings (not just checklist)
    threading.Thread(target=_persist_settings, daemon=True).start()
    print(f"✅ Checklist settings saved: {len(updated)} keys")
    return jsonify({"status": "ok", "updated": updated, "settings": CHECKLIST_SETTINGS})

@app.route("/get-settings", methods=["GET"])
def get_settings():
    return jsonify({
        "buy_amount":    AUTO_BUY_SIZE_BNB,
        "max_positions": AUTO_MAX_POSITIONS,
        "auto_enabled":  AUTO_TRADE_ENABLED,
    })

@app.route("/close-one", methods=["POST"])
def close_one_position():
    positions = auto_trade_stats["running_positions"]
    if not positions:
        return jsonify({"status": "empty", "message": "Koi open position nahi hai"})
    # User ne specific address diya?
    data = request.get_json(silent=True) or {}
    addr = data.get("address", "")
    if addr and addr in positions:
        pos = positions[addr]
    else:
        # Fallback: worst PnL wali position
        def _get_pnl(item):
            a, p = item
            entry   = p.get("entry", 0)
            current = monitored_positions.get(a, {}).get("current", entry)
            return ((current - entry) / entry * 100) if entry > 0 else 0
        addr, pos = sorted(positions.items(), key=_get_pnl)[0]
    tok = pos.get("token", addr[:8])
    remaining = len(positions) - 1
    threading.Thread(target=_auto_paper_sell, args=(addr, "Manual close via UI", 100.0), daemon=True).start()
    print(f"🔴 Manual close: {tok} ({addr[:10]})")
    return jsonify({"status": "closing", "address": addr, "token": tok, "remaining": remaining})

@app.route("/health")
def health():
    gc.collect()  # free RAM every health check
    return jsonify({
        "status":        "ok",
        "bsc_connected": True,
        "supabase":      supabase is not None,
        "bnb_price":     market_cache.get("bnb_price", 0),
        "fear_greed":    market_cache.get("fear_greed", 50),
        "new_pairs":     len(new_pairs_queue),
        "monitoring":    len(monitored_positions),
        "last_update":   market_cache.get("last_updated"),
        "uptime_min":    int((datetime.utcnow() - BIRTH_TIME).total_seconds() / 60),
        "positions":     len(auto_trade_stats.get("running_positions", {})),
        "learning_cycles": brain.get("total_learning_cycles", 0),
    })
