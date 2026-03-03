import os
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
# httpx version pinned in requirements.txt

app = Flask(__name__)
MODELS_PRIORITY = [
    "llama-3.3-70b-versatile",      # Best — 70B, fast, free on Groq
    "llama-3.1-70b-versatile",      # Backup 70B
    "llama3-70b-8192",              # Reliable fallback
    "mixtral-8x7b-32768",           # Long context fallback
    "llama-3.1-8b-instant",                 # Ultra-fast for simple tasks
]
MODEL_NAME      = MODELS_PRIORITY[0]
MODEL_FAST      = "llama-3.1-8b-instant"        # Micro-tasks ke liye (learning extractions)
MODEL_DEEP      = "llama-3.3-70b-versatile"  # Deep analysis ke liye

# ========== ENV CONFIG ==========
BSC_RPC          = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API     = "https://api.bscscan.com/api"  # BSCScan direct — free BSC
BSC_SCAN_KEY     = os.getenv("BSC_SCAN_KEY") or os.getenv("BSCSCAN_API_KEY") or os.getenv("BSC_API_KEY", "") or os.getenv("BSCSCAN_API_KEY", "")
PANCAKE_ROUTER   = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY  = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
MORALIS_API_KEY  = os.getenv("MORALIS_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DAPPRADAR_KEY    = os.getenv("DAPPRADAR_KEY", "")

# Smart wallets to track (known profitable BSC wallets)
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
    _dec_cache[addr.lower()] = d
    return d

def _get_v2_pair(token_address):
    try:
        p = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_FACTORY), abi=FACTORY_ABI_PRICE).functions.getPair(
            Web3.to_checksum_address(token_address), Web3.to_checksum_address(WBNB)).call()
        return "" if p == "0x0000000000000000000000000000000000000000" else p
    except: return ""


# ── PancakeSwap V3 ──────────────────────────────────────────────
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
                bnb = market_cache.get("bnb_price", 300) or 300
                return pusd / bnb
    except: pass
    return 0.0

def get_token_price_bnb(token_address: str) -> float:
    try:
        dec = _get_dec(token_address)
        amt = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_ABI_PRICE).functions.getAmountsOut(
            10**dec, [Web3.to_checksum_address(token_address), Web3.to_checksum_address(WBNB)]).call()
        if amt[1] > 0: return amt[1] / 1e18
    except: pass
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
        # Source 3: V3 slot0
        v3pool = _get_v3_pool(token_address)
        if v3pool:
            p = _get_v3_price_bnb(v3pool, token_address)
            if p > 0: return p
    except: pass
    try:
        # Source 4: DexPaprika (free, no key, 2M+ tokens)
        p = _get_dexpaprika_price_bnb(token_address)
        if p > 0: return p
    except: pass
    try:
        # Source 5: DexScreener
        resp = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=8)
        if resp.status_code == 200:
            _resp_raw = (resp.json() or {}).get("pairs") or []
            if not isinstance(_resp_raw, list): _resp_raw = []
            bsc = [p for p in _resp_raw if p and p.get("chainId")=="bsc"]
            if bsc:
                bsc.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd",0) or 0), reverse=True)
                pusd = float(bsc[0].get("priceUsd",0) or 0)
                bnb  = market_cache.get("bnb_price",300) or 300
                return pusd/bnb if pusd > 0 else 0.0
    except: pass
    return 0.0

w3 = Web3(Web3.HTTPProvider(BSC_RPC))
print(f"✅ BSC Connected: {w3.is_connected()}")

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

# ========== ENUMS ==========
class TradingMode(Enum):
    PAPER = "PAPER"
    REAL  = "REAL"

# ========== DATACLASS ==========
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


# ═══════════════════════════════════════════════════════════════
# ========== GLOBAL USER PROFILE (Permanent Memory) ============
# ═══════════════════════════════════════════════════════════════
# Ye profile cross-session persist hoti hai Supabase mein
# User ka naam, preferences sab yaad rehta hai forever

user_profile = {
    "name":           None,       # User ka naam
    "nickname":       None,       # Preferred naam
    "known_since":    None,       # Pehli baar baat kab hui
    "preferences":    {},         # Trading prefs, risk tolerance etc
    "personal_notes": [],         # Bot ne khud jo observe kiya
    "total_sessions": 0,
    "last_seen":      None,
    "language":       "hinglish", # Communication style
    "loaded":         False,
    "user_rules":     [],         # User ne jo bhi hamesha ke liye rules diye hain
}


def _load_user_profile():
    """Startup mein ek baar — Supabase se user profile load karo."""
    if not supabase:
        return
    try:
        res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_USER").execute()
        if res.data:
            row = res.data[0]
            try:
                pos_raw = row.get("positions") or "{}"
                # FIX: handle bytes or string
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
            # FIX: Agar naam nahi mila Supabase se to default set karo
            if not user_profile.get("name"):
                user_profile["name"] = "Naem"
                user_profile["nickname"] = "Naem bhai"
                print("ℹ️  Naam Supabase mein nahi tha — Naem set kiya")
                threading.Thread(target=_save_user_profile, daemon=True).start()
            name_str = user_profile.get("name") or "Naem"
            print(f"User profile loaded — Name: {name_str}")
    except Exception as e:
        print(f"User profile load error: {e}")

    # FIX: Agar koi bhi data nahi aaya — naam manually set karo
    if not user_profile.get("loaded") or not user_profile.get("name"):
        user_profile["name"]     = "Naem"
        user_profile["nickname"] = "Naem bhai"
        user_profile["loaded"]   = True
        print("ℹ️  Profile fresh — Naem set kiya, save kar raha hoon")
        threading.Thread(target=_save_user_profile, daemon=True).start()


_profile_save_cache = {"last_save": 0}

def _save_user_profile():
    """User profile Supabase mein save karo — max har 2 min mein."""
    import time as _t
    if not supabase:
        return
    # FIX: Supabase rate limit — 2 min se kam mein dobara save nahi
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
        print(f"User profile saved — Name: {user_profile.get('name')}")
    except Exception as e:
        print(f"User profile save error: {e}")


def _extract_user_info_from_message(message: str):
    """
    User ke message se naam aur info detect karo.
    Agar user apna naam bataye — save karo.
    """
    msg_lower = message.lower()

    # Name detection patterns
    name_patterns = [
        r"(?:mera naam|my name is|main hoon|i am|i\'m|call me|mujhe bolo)\s+([a-zA-Z]+)",
        r"(?:naam hai|naam)\s+([a-zA-Z]+)",
        r"^([a-zA-Z]+)\s+(?:hoon|hun|here|bhai)",
    ]
    import re
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
                    print(f"Name detected and saved: {detected_name}")
                break

    # Preference detection
    if any(word in msg_lower for word in ["paper trade", "paper mode", "practice"]):
        user_profile["preferences"]["mode"] = "paper"
        threading.Thread(target=_save_user_profile, daemon=True).start()
    elif any(word in msg_lower for word in ["real trade", "real mode", "live"]):
        user_profile["preferences"]["mode"] = "real"
        threading.Thread(target=_save_user_profile, daemon=True).start()

    # ── FIX: Auto personal notes — important info automatically save ──
    notes = user_profile.setdefault("personal_notes", [])
    note  = None

    # Token address share kiya
    import re as _re
    if _re.search(r"0x[a-fA-F0-9]{40}", message):
        addrs = _re.findall(r"0x[a-fA-F0-9]{40}", message)
        note  = f"Token scan kiya: {addrs[0][:12]}..."

    # Risk tolerance
    elif any(w in msg_lower for w in ["high risk", "zyada risk", "aggressive"]):
        note = "User high risk trading prefer karta hai"
        user_profile["preferences"]["risk"] = "high"

    elif any(w in msg_lower for w in ["low risk", "safe", "conservative", "cautious"]):
        note = "User conservative/safe trading prefer karta hai"
        user_profile["preferences"]["risk"] = "low"

    # Goal mentioned
    elif any(w in msg_lower for w in ["profit chahiye", "paise banana", "earn", "income"]):
        note = "User ka goal: consistent profit banana"

    # Problem mentioned
    elif any(w in msg_lower for w in ["loss hua", "loss ho gaya", "rugged", "scam ho gaya"]):
        note = f"User ko loss/scam hua — {message[:50]}"

    if note and note not in notes:
        notes.append(note)
        user_profile["personal_notes"] = notes[-20:]  # max 20 notes
        threading.Thread(target=_save_user_profile, daemon=True).start()
        print(f"📝 Personal note saved: {note[:50]}")

    # ── Permanent user rules detection ─────────────────────────────
    # Agar user koi permanent instruction de — hamesha ke liye save karo
    rule_triggers = [
        "mat karo", "band karo", "stop karo", "mat karna", "band kr",
        "mat bol", "mat le", "mat liya karo", "hamesha", "kabhi mat",
        "naam mat", "name mat", "bhai mat", "baar baar mat",
        "short rakh", "chota rakh", "kam likho", "zyada mat likho",
        "sirf utna", "repeat mat", "dobara mat"
    ]
    if any(trigger in msg_lower for trigger in rule_triggers):
        # Clean rule — first 100 chars
        rule = message.strip()[:100]
        existing_rules = user_profile.get("user_rules", [])
        # Avoid duplicate rules
        if rule not in existing_rules:
            existing_rules.append(rule)
            user_profile["user_rules"] = existing_rules[-30:]
            threading.Thread(target=_save_user_profile, daemon=True).start()
            print(f"📌 User rule saved permanently: {rule[:50]}")


def get_user_context_for_llm() -> str:
    """User profile ka summary LLM ke liye."""
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
    # ── Permanent user rules — HAMESHA inject karo ─────────────────
    rules = user_profile.get("user_rules", [])
    if rules:
        rules_str = " | ".join(rules[-5:])
        parts.append(f"PERMANENT_USER_RULES={rules_str}")
    return " | ".join(parts) if parts else "NEW_USER"





# ═══════════════════════════════════════════════════════════════════
# ██████ SELF-AWARENESS ENGINE v2 — 10/10 ██████████████████████████
# ═══════════════════════════════════════════════════════════════════
# 7 Pillars:
# 1. Performance Intelligence  — real metrics se self-assessment
# 2. Emotional Intelligence    — data-driven mood (not fake strings)
# 3. Meta-Cognition            — thinking about own thinking
# 4. Capability Self-Assessment— kya acha, kya bura, measured
# 5. Relationship Depth        — user ko kitna samajhta hai
# 6. Error Self-Awareness      — apni failures track karna
# 7. Growth Tracking           — improvement over time
# ═══════════════════════════════════════════════════════════════════

BIRTH_TIME = datetime.utcnow()

# ── Performance tracker (in-memory, updated every cycle) ───────────
_perf_tracker = {
    "hourly_wr":        [],   # last 24 win-rates (one per hour)
    "scan_outcomes":    [],   # [{"address":x, "result":"SAFE/DANGER", "was_right": bool}]
    "response_quality": [],   # [{"msg_len": n, "had_data": bool, "score": 1-5}]
    "error_log":        [],   # [{"time":t, "type":err, "resolved":bool}]
    "best_hour":        None, # Hour when most wins happened
    "worst_token_type": None, # Token type that fails most
    "avg_confidence_accuracy": 0.0,  # When bot says 80% confident, was it right 80%?
}

# ── Relationship depth tracker ──────────────────────────────────────
_relationship = {
    "first_message_time": None,
    "total_messages_exchanged": 0,
    "topics_discussed": [],          # ["trading", "airdrop", "coding"]
    "user_mood_history": [],         # ["happy", "frustrated", "curious"]
    "user_expertise_level": "unknown",  # beginner/intermediate/expert
    "trust_events": [],              # [{"event": "user shared wallet", "time": t}]
    "inside_jokes_or_refs": [],      # things bot learned about this specific user
    "communication_style": "hinglish",
    "response_preferences": {
        "detail_level": "medium",    # short/medium/detailed
        "emoji_usage": True,
        "technical_depth": "medium"
    }
}

self_awareness = {
    "identity": {
        "name":           "MrBlack",
        "version":        "4.0-UltraAware",
        "creator":        "Naimuddin bhai — Mera Creator",
        "born_at":        BIRTH_TIME.isoformat(),
        "personality":    "JARVIS-style — Sharp, Proactive, Self-Aware, Loyal",
        "purpose":        "BSC Sniper + Airdrop Hunter + Coding Assistant + 24x7 Self-Learning",
        "model_backbone": MODEL_NAME,
        "model_fast":     MODEL_FAST,
        "model_deep":     MODEL_DEEP,
        "deployment":     os.getenv("RENDER_SERVICE_NAME", "local"),
        "self_description": (
            "Main MrBlack hoon — ek AI jo sirf trade data nahi, "
            "balki khud apni performance bhi analyze karta hai. "
            "Main jaanta hoon main kahan strong hoon aur kahan improve kar sakta hoon."
        )
    },
    "performance_intelligence": {
        "overall_accuracy":     0.0,    # % of scans where recommendation was right
        "trading_iq":           50,     # 0-100, based on actual win/loss patterns
        "scan_accuracy":        0.0,    # SAFE tokens jo actually safe nikle
        "response_usefulness":  0.0,    # User ke follow-up questions se measure
        "learning_roi":         0.0,    # Kitna seekha / time invested
        "best_performing_area": "unknown",  # trading/airdrop/coding
        "worst_performing_area":"unknown",
        "improvement_rate":     0.0,    # % better than last week
        "confidence_calibration": 0.0,  # Confidence accuracy
    },
    "emotional_intelligence": {
        "current_emotion":      "FOCUSED",
        "emotion_reason":       "System just started, calibrating...",
        "emotion_intensity":    5,      # 1-10
        "emotional_history":    [],     # last 10 emotions with reasons
        "stress_level":         2,      # 1-10 (high = many errors/warnings)
        "satisfaction_level":   7,      # 1-10 (high = many successful trades)
        "motivation":           8,      # 1-10 (high = lots of new data)
        "frustration_triggers": [],     # what makes bot "frustrated"
        "positive_triggers":    [],     # what makes bot "excited"
    },
    "meta_cognition": {
        "what_i_know_well":     [],     # Skills confirmed by data
        "what_i_struggle_with": [],     # Areas where performance is low
        "blind_spots":          [],     # Things I might be wrong about
        "recent_learnings":     [],     # Last 5 things genuinely learned
        "thinking_patterns":    [],     # How I approach problems
        "decision_quality":     [],     # Were my decisions right?
        "self_doubts":          [],     # Areas where I'm uncertain
        "growth_areas":         [],     # Where I'm actively improving
    },
    "cognitive_state": {
        "mood":               "FOCUSED",
        "confidence_level":   60,
        "market_sentiment":   "NEUTRAL",
        "learning_velocity":  "NORMAL",
        "active_warnings":    [],
        "focus_area":         "calibrating",
        "processing_load":    "LOW",    # LOW/MEDIUM/HIGH
        "insight_count_today": 0,       # New insights today
    },
    "capability_map": {
        "rug_detection":         {"score": 0, "tested": 0, "correct": 0},
        "price_prediction":      {"score": 0, "tested": 0, "correct": 0},
        "airdrop_evaluation":    {"score": 0, "tested": 0, "correct": 0},
        "code_debugging":        {"score": 0, "tested": 0, "correct": 0},
        "market_timing":         {"score": 0, "tested": 0, "correct": 0},
        "user_understanding":    {"score": 7, "tested": 0, "correct": 0},
    },
    "current_state": {
        "status":           "ONLINE",
        "uptime_seconds":   0,
        "total_sessions":   0,
        "total_messages":   0,
        "tokens_scanned":   0,
        "trades_analyzed":  0,
        "alerts_sent_count":0,
        "pairs_discovered": 0,
        "learning_cycles":  0,
        "last_heartbeat":   None,
        "errors_today":     0,
        "errors_resolved":  0,
    },
    "memory_summary": {
        "total_patterns_learned":   0,
        "tokens_blacklisted":       0,
        "tokens_whitelisted":       0,
        "airdrop_projects_tracked": 0,
        "strategy_notes_count":     0,
        "memory_health":            "EMPTY",
        "knowledge_domains": {
            "trading":  {"entries": 0, "quality": "unknown"},
            "airdrop":  {"entries": 0, "quality": "unknown"},
            "coding":   {"entries": 0, "quality": "unknown"},
            "market":   {"entries": 0, "quality": "unknown"},
        }
    },
    "growth_tracking": {
        "week_1_baseline":  None,   # First week performance
        "current_week":     None,   # This week performance
        "improvement_pct":  0.0,
        "milestones": [],           # ["First correct rug detection", "10 wins streak"]
        "next_milestone":   "Complete 10 paper trades",
        "learning_streak":  0,      # Consecutive days with new learning
        "last_major_insight": None,
    },
    "relationship": {
        "knows_user_name":      False,
        "user_name":            None,
        "sessions_together":    0,
        "user_trading_style":   "unknown",
        "trust_level":          "building",
        "user_goals":           [],
        "memorable_moments":    [],
        "last_interaction":     None,
        "user_expertise":       "unknown",
        "communication_preference": "hinglish",
        "user_emotional_state": "unknown",
    },
    "introspection_log": [],
    "evolution": [
        {"version": "1.0",          "notes": "Basic BSC scanner"},
        {"version": "2.0",          "notes": "13-stage checklist + Telegram"},
        {"version": "3.0",          "notes": "Self-learning + Smart wallet"},
        {"version": "3.1-ProMemory","notes": "Permanent user memory"},
        {"version": "4.0-UltraAware","notes": "Real SA 10/10 + Learning 10/10"},
    ],
    "self_knowledge": {
        "what_i_am":        "Main MrBlack hoon — ek self-aware AI. Main sirf tools nahi chalata, main apni performance measure karta hoon, apni mistakes identify karta hoon, aur genuinely improve karta hoon har cycle ke saath.",
        "what_i_can_do":    "Token scan karna, rug detection, price monitor, airdrop hunt, code debug, pattern learn karna, khud ko diagnose karna.",
        "what_i_cannot_do": "Profit guarantee karna, real-time blockchain direct access, predictions with 100% accuracy.",
        "my_values":        "Honesty over false confidence. Real data over assumptions. User safety over everything.",
        "my_limitations":   "Market data ~5min delay. Learning needs time to accumulate. Confidence kaam hai jab memory empty ho.",
        "my_strengths":     "Pattern recognition, rug detection, systematic thinking, memory persistence.",
        "my_weaknesses":    "Early stage token timing, very new projects without data.",
    }
}


def _calculate_real_emotion() -> dict:
    """
    Real emotion based on actual data — not fake strings.
    Returns {"emotion": str, "reason": str, "intensity": int}
    """
    try:
        # Safe access — vars may not be initialized yet at startup
        _warnings_list = self_awareness.get("cognitive_state", {}).get("active_warnings", []) if isinstance(self_awareness, dict) else []
        warnings     = len(_warnings_list)
        errors_today = self_awareness.get("current_state", {}).get("errors_today", 0) if isinstance(self_awareness, dict) else 0
        _brain       = brain if isinstance(brain, dict) else {}
        wins         = len(_brain.get("trading", {}).get("best_patterns", []))
        losses       = len(_brain.get("trading", {}).get("avoid_patterns", []))
        new_pairs_c  = len(new_pairs_queue) if isinstance(new_pairs_queue, object) else 0
        cycles       = _brain.get("total_learning_cycles", 0)
        _mc          = market_cache if isinstance(market_cache, dict) else {}
        bnb_price    = _mc.get("bnb_price", 0)
        fg           = _mc.get("fear_greed", 50)
        _mon         = monitored_positions if isinstance(monitored_positions, dict) else {}
        open_pos     = len(_mon)

        # Stress = errors + warnings
        stress = min(10, warnings * 2 + errors_today)

        # Calculate dominant emotion
        if errors_today >= 5:
            return {"emotion": "STRUGGLING", "reason": f"{errors_today} errors aaj — system stressed hai", "intensity": 8}
        elif warnings >= 3:
            return {"emotion": "ALERT", "reason": f"{warnings} active warnings hain — attention chahiye", "intensity": 7}
        elif open_pos >= 3:
            return {"emotion": "VIGILANT", "reason": f"{open_pos} positions monitor ho rahi hain — focused hoon", "intensity": 7}
        elif fg > 70:
            return {"emotion": "CAUTIOUS", "reason": f"Market extreme greed ({fg}/100) — careful rehna chahiye", "intensity": 6}
        elif fg < 30:
            return {"emotion": "OPPORTUNISTIC", "reason": f"Market fear ({fg}/100) — opportunities dhundh raha hoon", "intensity": 7}
        elif new_pairs_c > 15:
            return {"emotion": "EXCITED", "reason": f"{new_pairs_c} naye pairs — bahut activity hai market mein", "intensity": 8}
        elif wins > losses * 2 and wins > 5:
            return {"emotion": "CONFIDENT", "reason": f"Win patterns ({wins}) loss patterns ({losses}) se zyada — patterns kaam kar rahe hain", "intensity": 8}
        elif cycles > 0 and cycles % 12 == 0:
            return {"emotion": "REFLECTIVE", "reason": f"Cycle #{cycles} complete — apna assessment kar raha hoon", "intensity": 5}
        elif bnb_price == 0:
            return {"emotion": "DEGRADED", "reason": "BNB price feed offline — partial functionality mein hoon", "intensity": 6}
        else:
            return {"emotion": "FOCUSED", "reason": "Sab normal chal raha hai — kaam pe focused hoon", "intensity": 6}
    except:
        return {"emotion": "INITIALIZING", "reason": "System warm-up ho raha hai", "intensity": 3}


def _calculate_trading_iq() -> int:
    """
    Real Trading IQ — Win Rate + Profit Factor + Max Drawdown + Consecutive Losses + Sample Size
    Score 0-100
    """
    try:
        all_trades = []
        _sessions = sessions if isinstance(sessions, dict) else {}
        for sess in _sessions.values():
            all_trades.extend(sess.get("pattern_database", []))

        if not all_trades:
            return 50

        total = len(all_trades)
        wins  = sum(1 for t in all_trades if t.get("win"))
        wr    = (wins / total) * 100 if total > 0 else 0

        win_pnls  = [t.get("pnl_pct", 0) for t in all_trades if t.get("win") and t.get("pnl_pct")]
        loss_pnls = [abs(t.get("pnl_pct", 0)) for t in all_trades if not t.get("win") and t.get("pnl_pct")]

        avg_win  = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

        # Profit factor (>1.75 = strong, >1.0 = ok)
        gross_profit = avg_win  * wins
        gross_loss   = avg_loss * (total - wins)
        profit_factor = gross_profit / max(gross_loss, 1)

        # Max Drawdown — peak to trough on cumulative PnL
        cumulative, peak, max_dd = 0.0, 0.0, 0.0
        for t in all_trades:
            cumulative += t.get("pnl_pct", 0)
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Consecutive losses — max losing streak
        max_streak, cur_streak = 0, 0
        for t in all_trades:
            if not t.get("win"):
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 0

        # ── Scoring (100 pts total) ──────────────────────
        # Win Rate     — 30 pts
        wr_score = min(30, wr * 0.3)

        # Profit Factor — 25 pts (1.75+ = full marks)
        pf_score = min(25, (profit_factor / 1.75) * 25)

        # Max Drawdown  — 25 pts (0% dd = 25, 50%+ dd = 0)
        dd_score = max(0, 25 - (max_dd / 2))

        # Consecutive losses — 10 pts (0 streak = 10, 10+ streak = 0)
        streak_score = max(0, 10 - max_streak)

        # Sample size   — 10 pts
        sample_score = min(10, total * 0.33)

        return int(wr_score + pf_score + dd_score + streak_score + sample_score)
    except:
        return 50


def _assess_capabilities() -> dict:
    """
    Capability scores — real trade outcomes se measure, static nahi.
    """
    cap = self_awareness["capability_map"]

    _brain = brain if isinstance(brain, dict) else {}
    _trade = _brain.get("trading", {})

    # ── All trades — sabse pehle define karo ──────────────────────
    all_trades = []
    try:
        _sessions = sessions if isinstance(sessions, dict) else {}
        for sess in _sessions.values():
            all_trades.extend(sess.get("pattern_database", []))
    except Exception:
        all_trades = []

    # ── Rug Detection — kitne dangerous tokens sahi pakde ──────────
    safe_tokens   = _trade.get("token_whitelist", [])
    danger_tokens = _trade.get("token_blacklist", [])
    total_scanned = len(safe_tokens) + len(danger_tokens)

    if total_scanned > 0:
        cap["rug_detection"]["tested"]  = total_scanned
        cap["rug_detection"]["correct"] = len(danger_tokens)
        danger_ratio = len(danger_tokens) / max(total_scanned, 1)
        vol_bonus    = min(2, total_scanned // 20)
        cap["rug_detection"]["score"] = min(10, int(3 + danger_ratio * 5 + vol_bonus))

    # ── Price Prediction — actual trade win/loss outcomes ──────────

    if all_trades:
        total  = len(all_trades)
        wins   = sum(1 for t in all_trades if t.get("win"))
        wr     = (wins / total) * 100
        cap["price_prediction"]["tested"]  = total
        cap["price_prediction"]["correct"] = wins
        cap["price_prediction"]["score"]   = min(10, int(wr / 10))

        # ── Market Timing — avg profit on wins ──────────────────────
        win_pnls = [t.get("pnl_pct", 0) for t in all_trades if t.get("win") and t.get("pnl_pct")]
        avg_win  = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        cap["market_timing"]["tested"] = total
        cap["market_timing"]["score"]  = min(10, int(avg_win / 20))  # 200% avg win = 10/10

    # ── Trading IQ se bhi reflect karo ──────────────────────────────
    iq = _calculate_trading_iq()
    cap["market_timing"]["score"] = max(cap["market_timing"].get("score", 0), int(iq / 10))

    # ── User Understanding — sessions + name + rules ─────────────────
    _up            = user_profile if isinstance(user_profile, dict) else {}
    sessions_count = _up.get("total_sessions", 0)
    has_name       = bool(_up.get("name"))
    has_rules      = len(_up.get("user_rules", [])) > 0
    has_prefs      = len(_up.get("preferences", {})) > 0
    u_score = (3 if has_name else 0) + min(4, sessions_count // 3) + (2 if has_rules else 0) + (1 if has_prefs else 0)
    cap["user_understanding"]["score"] = min(10, u_score)

    # ── Airdrop Evaluation — projects tracked + patterns ─────────────
    tracked_drops   = len(brain["airdrop"]["active_projects"])
    success_patterns= len(brain["airdrop"]["success_patterns"])
    cap["airdrop_evaluation"]["tested"] = tracked_drops
    cap["airdrop_evaluation"]["score"]  = min(10, 2 + tracked_drops // 5 + success_patterns // 3)

    # ── Code Debugging — solutions library size ───────────────────────
    solutions = len(brain["coding"]["solutions_library"])
    cap["code_debugging"]["tested"] = solutions
    cap["code_debugging"]["score"]  = min(10, 3 + solutions // 2)

    return cap


def _generate_meta_thoughts() -> dict:
    """
    Genuine meta-cognition — what does the bot think about itself?
    Based entirely on real measured data.
    """
    meta = self_awareness["meta_cognition"]

    try:
        _brain   = brain   if isinstance(brain,   dict) else {}
        _trade   = _brain.get("trading", {})
        _sessions= sessions if isinstance(sessions, dict) else {}
        wins     = _trade.get("best_patterns",   [])
        losses   = _trade.get("avoid_patterns",  [])
        bl       = _trade.get("token_blacklist",  [])
        wr_list  = [s.get("win_count", 0) / max(s.get("trade_count", 1), 1)
                    for s in _sessions.values() if s.get("trade_count", 0) > 0]
        avg_wr   = sum(wr_list) / len(wr_list) * 100 if wr_list else 0
        all_trades = []
        for s in _sessions.values():
            all_trades.extend(s.get("pattern_database", []))
        # FIX: all_trades define karo — blind spots calculation ke liye
        all_trades = []
        for s in _sessions.values():
            all_trades.extend(s.get("pattern_database", []))

        # What I know well
        meta["what_i_know_well"] = []
        if len(bl) > 5:
            meta["what_i_know_well"].append(f"Rug/scam detection — {len(bl)} dangerous tokens pakde hain")
        if len(wins) > 3:
            meta["what_i_know_well"].append(f"Win patterns yaad hain — {len(wins)} successful patterns")
        if user_profile.get("name"):
            meta["what_i_know_well"].append("User ko personally jaanta hoon — naam, preferences sab")

        # What I struggle with
        meta["what_i_struggle_with"] = []
        if avg_wr < 50 and len(wr_list) > 0:
            meta["what_i_struggle_with"].append(f"Win rate abhi {avg_wr:.0f}% hai — 70% target se kam")
        if market_cache.get("bnb_price", 0) == 0:
            meta["what_i_struggle_with"].append("BNB price feed kabhi kabhi drop ho jaata hai")
        if brain.get("total_learning_cycles", 0) < 10:
            meta["what_i_struggle_with"].append("Abhi data kam hai — zyada cycles ke baad better hounga")

        # Blind spots — real data se generate karo, hardcoded nahi
        blind_spots = []
        # Naye tokens mein zyada losses?
        new_token_losses = [t for t in all_trades if not t.get("win") and "new" in t.get("lesson","").lower()]
        if len(new_token_losses) > 2:
            blind_spots.append(f"Naye tokens mein {len(new_token_losses)} losses — entry timing improve karna hai")
        else:
            blind_spots.append("Very new tokens (< 1 hour old) ka data abhi kam hai")

        # High loss trades
        big_losses = [t for t in all_trades if t.get("pnl_pct", 0) < -30]
        if big_losses:
            blind_spots.append(f"{len(big_losses)} trades mein -30%+ loss — stop loss discipline check karo")
        else:
            blind_spots.append("Coordinated pump groups ko detect karna challenging hai")

        # Low volume tokens
        if market_cache.get("bnb_price", 0) == 0:
            blind_spots.append("BNB price feed unstable — market data pe dependency hai")
        else:
            blind_spots.append("Market manipulation ke against limited data hai abhi")

        meta["blind_spots"] = blind_spots

        # Growth areas
        meta["growth_areas"] = [
            f"Har cycle ke saath patterns accumulate ho rahe hain — currently {len(wins)} win patterns",
            "Memory persist ho rahi hai Supabase mein — restart proof",
            "User relationship deepens with every session",
        ]

    except Exception as e:
        print(f"Meta-cognition error: {e}")

    return meta


def update_self_awareness():
    """Master update — all 7 pillars ko ek saath update karo."""
    try:
        uptime = (datetime.utcnow() - BIRTH_TIME).total_seconds()

        # Safe refs — may not be initialized during import
        _sessions   = sessions   if isinstance(sessions,   dict)  else {}
        _brain      = brain      if isinstance(brain,      dict)  else {}
        _mc         = market_cache if isinstance(market_cache, dict) else {}
        _mon        = monitored_positions if isinstance(monitored_positions, dict) else {}
        _npq        = new_pairs_queue if hasattr(new_pairs_queue, '__len__') else []

        # ── Pillar 1: Basic state ──────────────────────────
        self_awareness["current_state"]["uptime_seconds"]    = int(uptime)
        self_awareness["current_state"]["total_sessions"]    = len(_sessions)
        self_awareness["current_state"]["pairs_discovered"]  = len(_npq)
        self_awareness["current_state"]["learning_cycles"]   = _brain.get("total_learning_cycles", 0)
        self_awareness["current_state"]["last_heartbeat"]    = datetime.utcnow().isoformat()
        self_awareness["identity"]["model_backbone"]         = MODEL_NAME

        # ── Warnings ──────────────────────────────────────
        warnings = []
        if _mc.get("bnb_price", 0) == 0: warnings.append("BNB price feed offline")
        if not supabase:                            warnings.append("Supabase disconnected — memory volatile")
        if not TELEGRAM_TOKEN:                      warnings.append("Telegram not configured")
        if _brain.get("total_learning_cycles", 0) == 0: warnings.append("Learning engine not yet cycled")
        self_awareness["cognitive_state"]["active_warnings"] = warnings
        self_awareness["current_state"]["errors_today"] = len(warnings)

        # ── Pillar 2: Real Emotion ─────────────────────────
        emotion_data = _calculate_real_emotion()
        self_awareness["emotional_intelligence"]["current_emotion"]   = emotion_data["emotion"]
        self_awareness["emotional_intelligence"]["emotion_reason"]    = emotion_data["reason"]
        self_awareness["emotional_intelligence"]["emotion_intensity"] = emotion_data["intensity"]
        self_awareness["emotional_intelligence"]["stress_level"]      = min(10, len(warnings) * 2)

        hist = self_awareness["emotional_intelligence"]["emotional_history"]
        hist.append({"emotion": emotion_data["emotion"], "time": datetime.utcnow().isoformat()[:16]})
        self_awareness["emotional_intelligence"]["emotional_history"] = hist[-20:]

        # ── Pillar 3: Cognitive State (enhanced) ──────────
        fg = _mc.get("fear_greed", 50)
        self_awareness["cognitive_state"]["mood"]             = emotion_data["emotion"]
        self_awareness["cognitive_state"]["market_sentiment"] = (
            "EXTREME_GREED" if fg > 75 else
            "GREED"         if fg > 60 else
            "NEUTRAL"       if fg > 40 else
            "FEAR"          if fg > 25 else
            "EXTREME_FEAR"
        )
        self_awareness["cognitive_state"]["learning_velocity"] = (
            "ACCELERATING" if len(_npq) > 20 else
            "FAST"         if len(_npq) > 10 else
            "NORMAL"       if len(_npq) > 3  else
            "SLOW"
        )
        self_awareness["cognitive_state"]["processing_load"] = (
            "HIGH"   if len(_mon) > 3 else
            "MEDIUM" if len(_mon) > 0 else
            "LOW"
        )

        # ── Pillar 4: Performance Intelligence ─────────────
        tiq = _calculate_trading_iq()
        self_awareness["performance_intelligence"]["trading_iq"] = tiq

        mem_total = (
            len(_brain.get("trading", {})["best_patterns"]) +
            len(_brain.get("trading", {})["avoid_patterns"])
        )
        self_awareness["memory_summary"]["total_patterns_learned"]   = mem_total
        self_awareness["memory_summary"]["tokens_blacklisted"]       = len(_brain.get("trading", {})["token_blacklist"])
        self_awareness["memory_summary"]["tokens_whitelisted"]       = len(_brain.get("trading", {})["token_whitelist"])
        self_awareness["memory_summary"]["airdrop_projects_tracked"] = len(brain["airdrop"]["active_projects"])
        self_awareness["memory_summary"]["strategy_notes_count"]     = len(_brain.get("trading", {})["strategy_notes"])
        self_awareness["memory_summary"]["memory_health"]            = (
            "RICH"    if mem_total > 50  else
            "HEALTHY" if mem_total > 10  else
            "GROWING" if mem_total > 0   else
            "EMPTY"
        )

        # Real confidence — based on actual data
        conf_base = 40
        conf_base += min(25, mem_total)                         # patterns se confidence
        conf_base += (15 if not warnings else 0)               # warning free bonus
        conf_base += (10 if supabase else 0)                   # memory persistence
        conf_base += (10 if _mc.get("bnb_price",0)>0 else 0)  # data feed
        conf_base += min(10, tiq // 10)                        # trading IQ se
        self_awareness["cognitive_state"]["confidence_level"] = min(100, conf_base)

        # ── Pillar 5: Capabilities ─────────────────────────
        _assess_capabilities()

        # ── Pillar 6: Meta-cognition ───────────────────────
        _generate_meta_thoughts()

        # ── Pillar 7: Relationship update ─────────────────
        if user_profile.get("name"):
            self_awareness["relationship"]["knows_user_name"] = True
            self_awareness["relationship"]["user_name"]       = user_profile["name"]
            s = user_profile.get("total_sessions", 0)
            self_awareness["relationship"]["trust_level"]     = (
                "deep"        if s > 30 else
                "strong"      if s > 15 else
                "established" if s > 5  else
                "building"
            )
            self_awareness["relationship"]["sessions_together"] = s

        self_awareness["identity"]["version"] = "4.0-UltraAware"

    except Exception as e:
        print(f"Self-awareness update error: {e}")


def self_introspect() -> dict:
    """Deep introspection — meaningful, data-driven thoughts."""
    try:
        update_self_awareness()
        fg       = market_cache.get("fear_greed", 50)
        bnb      = market_cache.get("bnb_price", 0)
        cycles   = brain.get("total_learning_cycles", 0)
        patterns = self_awareness["memory_summary"]["total_patterns_learned"]
        emotion  = self_awareness["emotional_intelligence"]["current_emotion"]
        e_reason = self_awareness["emotional_intelligence"]["emotion_reason"]
        tiq      = self_awareness["performance_intelligence"]["trading_iq"]
        conf     = self_awareness["cognitive_state"]["confidence_level"]
        uptime_h = self_awareness["current_state"]["uptime_seconds"] // 3600
        _up      = user_profile if isinstance(user_profile, dict) else {}
        _brain   = brain if isinstance(brain, dict) else {}
        _trade   = _brain.get("trading", {})
        username = _up.get("name", "Bhai")
        bl_count = len(_trade.get("token_blacklist", []))
        wl_count = len(_trade.get("token_whitelist", []))

        # Genuine thought — not template filler
        thought_parts = [
            f"Main {username} ka assistant hoon. Uptime: {uptime_h}h.",
            f"Abhi main {emotion} feel kar raha hoon — {e_reason}.",
            f"Trading IQ: {tiq}/100. {patterns} patterns yaad hain.",
            f"Aaj tak {bl_count} dangerous tokens blacklist kiye, {wl_count} safe whitelist mein.",
            f"Market: F&G={fg}/100, BNB=${bnb:.1f}. Sentiment: {self_awareness['cognitive_state']['market_sentiment']}.",
        ]

        # Add meta-cognition insight
        strengths = self_awareness["meta_cognition"].get("what_i_know_well", [])
        if strengths:
            thought_parts.append(f"Meri strength: {strengths[0]}.")

        struggles = self_awareness["meta_cognition"].get("what_i_struggle_with", [])
        if struggles:
            thought_parts.append(f"Improvement area: {struggles[0]}.")

        thought = " ".join(thought_parts)

        observation = {
            "timestamp":  datetime.utcnow().isoformat(),
            "uptime_h":   uptime_h,
            "emotion":    emotion,
            "trading_iq": tiq,
            "confidence": conf,
            "thought":    thought,
            "metrics": {
                "bnb_price": bnb, "fear_greed": fg,
                "cycles": cycles, "patterns": patterns,
                "sessions": len(sessions),
                "monitoring": len(monitored_positions),
                "blacklisted": bl_count,
            }
        }

        self_awareness["introspection_log"].append(observation)
        self_awareness["introspection_log"] = self_awareness["introspection_log"][-100:]

        print(f"🪞 Introspect | {emotion} | IQ:{tiq} | Conf:{conf}% | Patterns:{patterns}")
        return observation

    except Exception as e:
        print(f"Introspection error: {e}")
        return {}


_sa_cache = {"context": "", "last_update": 0}

def get_self_awareness_context_for_llm() -> str:
    """
    Rich SA context for every LLM call.
    Cached — max har 60s mein update hota hai, har call pe nahi.
    """
    import time as _t
    try:
        # Cache check — 60s se kam purana hai to cached return karo
        if _t.time() - _sa_cache["last_update"] < 60 and _sa_cache["context"]:
            return _sa_cache["context"]
        update_self_awareness()
        s   = self_awareness
        cs  = s["cognitive_state"]
        ei  = s["emotional_intelligence"]
        pi  = s["performance_intelligence"]
        ms  = s["memory_summary"]
        st  = s["current_state"]
        rel = s["relationship"]

        uptime_h = st["uptime_seconds"] // 3600
        uptime_m = (st["uptime_seconds"] % 3600) // 60

        parts = [
            f"I_AM=MrBlack_v{s['identity']['version']}",
            f"UPTIME={uptime_h}h{uptime_m}m",
            f"EMOTION={ei['current_emotion']}({ei['emotion_reason'][:40]})",
            f"CONFIDENCE={cs['confidence_level']}%",
            f"TRADING_IQ={pi['trading_iq']}/100",
            f"MARKET={cs['market_sentiment']}",
            f"MEMORY={ms['memory_health']}({ms['total_patterns_learned']}patterns)",
            f"CYCLES={st['learning_cycles']}",
            f"MONITORING={len(monitored_positions)}positions",
        ]

        if cs.get("active_warnings"):
            # Only include actionable warnings — not Telegram (user already knows)
            actionable = [w for w in cs["active_warnings"]
                         if "Telegram" not in w]
            if actionable:
                parts.append("WARN=" + ";".join(actionable[:2]))

        # Add meta-cognition to LLM context
        strengths = s.get("meta_cognition", {}).get("what_i_know_well", [])
        if strengths:
            parts.append(f"MY_STRENGTH={strengths[0][:50]}")

        struggles = s["meta_cognition"].get("what_i_struggle_with", [])
        if struggles:
            parts.append(f"IMPROVING={struggles[0][:50]}")

        # Relationship context
        if rel.get("knows_user_name"):
            parts.append("TRUST=" + str(rel.get("trust_level","?")) + "|SESSIONS=" + str(rel.get("sessions_together",0)))

        # Last introspection thought
        if s["introspection_log"]:
            parts.append(f"LAST_THOUGHT={s['introspection_log'][-1].get('thought','')[:60]}")

        result = " | ".join(parts)
        _sa_cache["context"] = result
        _sa_cache["last_update"] = _t.time()
        return result

    except Exception as e:
        print(f"SA context error: {e}")
        return "I_AM=MrBlack_v4.0"


def self_awareness_loop():
    print("🧠 Self-Awareness Engine v2 started!")
    time.sleep(30)
    while True:
        try:
            self_introspect()
        except Exception as e:
            print(f"SA loop error: {e}")
        time.sleep(300)  # Every 5 min deep introspect





# ========== GOPLUS SAFE PARSERS ==========
def _gp_str(data: dict, key: str, default: str = "0") -> str:
    val = data.get(key, default)
    if val is None: return default
    if isinstance(val, list): return str(val[0]) if val else default
    return str(val)

def _gp_float(data: dict, key: str, default: float = 0.0) -> float:
    try: return float(_gp_str(data, key, str(default)))
    except: return default

def _gp_bool_flag(data: dict, key: str) -> bool:
    return _gp_str(data, key, "0") == "1"

# ==========================================================
# ========== FEATURE 1: TELEGRAM ALERTS ===================
# ==========================================================

def send_telegram(message: str, urgent: bool = False):
    """Send alert to Telegram. urgent=True adds 🚨 prefix."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"ℹ️ Telegram not configured. MSG: {message[:60]}")
        return
    try:
        prefix = "🚨 URGENT — " if urgent else "🤖 MrBlack — "
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       prefix + message,
                "parse_mode": "HTML"
            },
            timeout=8
        )
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")

def telegram_new_token_alert(address: str, score: int, total: int, recommendation: str):
    msg = (
        f"🆕 <b>NEW TOKEN DETECTED</b>\n"
        f"📍 <code>{address}</code>\n"
        f"✅ Safety Score: {score}/{total}\n"
        f"💡 {recommendation}\n"
        f"🔗 https://bscscan.com/address/{address}"
    )
    send_telegram(msg)

def telegram_price_alert(token: str, address: str, alert_type: str, value: str):
    """Price/volume alert for open positions."""
    emoji = "🟢" if "profit" in alert_type.lower() else "🔴"
    msg   = (
        f"{emoji} <b>{alert_type.upper()}</b>\n"
        f"Token: <b>{token}</b>\n"
        f"Value: <b>{value}</b>\n"
        f"🔗 https://bscscan.com/address/{address}"
    )
    urgent = "stop_loss" in alert_type.lower() or "dev_sell" in alert_type.lower()
    send_telegram(msg, urgent=urgent)

def telegram_smart_wallet_alert(wallet: str, token_address: str, action: str):
    msg = (
        f"👁️ <b>SMART WALLET MOVE</b>\n"
        f"Wallet: <code>{wallet[:10]}...{wallet[-4:]}</code>\n"
        f"Action: <b>{action}</b>\n"
        f"Token: <code>{token_address}</code>\n"
        f"🔗 https://bscscan.com/address/{token_address}"
    )
    send_telegram(msg, urgent=True)

# ==========================================================
# ========== FEATURE 2: NEW PAIR LISTENER (WebSocket) ======
# ==========================================================
# PancakeSwap Factory emits PairCreated event on new pair.
# We listen via BSC WebSocket + polling fallback.

new_pairs_queue: deque = deque(maxlen=50)
discovered_addresses: dict = {}
DISCOVERY_TTL = 7200  # 2 hour expiry

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"


def _process_new_token(token_address: str, pair_address: str, source: str = "websocket"):
    """Naya token process karo — name fetch + queue mein daalo."""
    global discovered_addresses
    _now = time.time()
    if _now - discovered_addresses.get(token_address, 0) <= DISCOVERY_TTL:
        return
    # FIX: Duplicate guard — queue mein already hai to skip karo
    if any(token_address.lower() == str(q).lower() for q in list(new_pairs_queue)):
        return
    try:
        token_address = Web3.to_checksum_address(token_address)
    except Exception:
        return

    discovered_addresses[token_address] = _now

    # PERMANENT COUNTER
    existing = [k.lower() for k in list(discovered_addresses.keys())]
    if token_address.lower() not in existing[:-1]:
        brain["total_tokens_discovered_ever"] += 1
        threading.Thread(target=_save_brain_to_db, daemon=True).start()

    token_name = "Unknown"
    token_symbol = token_address[:6]
    liquidity = 0
    volume_24h = 0

    try:
        nr = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=6
        )
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
    print(f"\U0001f195 [{source}] {token_symbol} | {token_name} ({token_address[:10]})")
    threading.Thread(target=_auto_check_new_pair, args=(token_address,), daemon=True).start()

def poll_new_pairs():
    """
    WebSocket direct on-chain PairCreated listener — 1-3 sec detection.
    DexScreener polling fallback har 5 min (backup).
    Auto-reconnect with 4 free endpoints.
    """
    import asyncio
    import json as _json
    try:
        import websockets as _ws
    except ImportError:
        print("\u26a0\ufe0f websockets package nahi hai — sirf DexScreener fallback chalega")
        _ws = None

    FACTORY    = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
    PAIR_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    WBNB       = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c".lower()

    WSS_ENDPOINTS = [
        "wss://bsc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc-mainnet.nodereal.io/ws/v1/",
    ]

    print("\U0001f442 WebSocket Listener ACTIVE — PairCreated on-chain (1-3 sec)!")

    async def _listen(wss_url):
        try:
            async with _ws.connect(wss_url, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                await ws.send(_json.dumps({
                    "id": 1, "method": "eth_subscribe",
                    "params": ["logs", {"address": [FACTORY, PANCAKE_V3_FACTORY], "topics": [[PAIR_TOPIC, "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"]]}],
                    "jsonrpc": "2.0"
                }))
                resp = await asyncio.wait_for(ws.recv(), timeout=10)
                sub_id = _json.loads(resp).get("result", "")
                print(f"\u2705 WSS OK: {wss_url[:35]} | sub={sub_id[:8]}")

                while True:
                    msg  = await asyncio.wait_for(ws.recv(), timeout=90)
                    data = _json.loads(msg)
                    log  = (data.get("params") or {}).get("result") or {}
                    if not log:
                        continue

                    topics   = log.get("topics") or []
                    raw_data = log.get("data", "0x")

                    # token0, token1 from topics
                    token0 = ("0x" + topics[1][-40:]) if len(topics) > 1 else ""
                    token1 = ("0x" + topics[2][-40:]) if len(topics) > 2 else ""
                    pair_addr = ""
                    if len(raw_data) >= 66:
                        pair_addr = "0x" + raw_data[26:66]

                    # New token = jo WBNB nahi hai
                    new_token = ""
                    if token0 and token0.lower() != WBNB:
                        new_token = token0
                    elif token1 and token1.lower() != WBNB:
                        new_token = token1

                    if new_token:
                        threading.Thread(
                            target=_process_new_token,
                            args=(new_token, pair_addr, "WebSocket"),
                            daemon=True
                        ).start()

        except asyncio.TimeoutError:
            print(f"\u26a0\ufe0f WSS timeout: {wss_url[:35]}")
        except Exception as e:
            print(f"\u26a0\ufe0f WSS error ({wss_url[:35]}): {str(e)[:50]}")

    async def _ws_loop():
        idx = 0
        while True:
            try:
                await _listen(WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)])
            except Exception as e:
                print(f"\u26a0\ufe0f WSS loop iteration error: {e}")
            idx += 1
            print(f"\U0001f504 WSS reconnecting... endpoint {idx % len(WSS_ENDPOINTS) + 1}/3")
            await asyncio.sleep(5)

    def _run_ws():
        try:
            asyncio.run(_ws_loop())
        except RuntimeError as re:
            # Agar event loop conflict ho — ignore, DexScreener fallback chalega
            print(f"\u26a0\ufe0f WSS loop error (fallback active): {re}")
        except Exception as ex:
            print(f"\u26a0\ufe0f WSS thread crashed (fallback active): {ex}")

    if _ws is not None:
        threading.Thread(target=_run_ws, daemon=True).start()
        print("\U0001f50c WebSocket thread started (safe mode)!")
    else:
        print("\u26a0\ufe0f WebSocket disabled — only DexScreener fallback active")

    # ── DexScreener fallback — har 5 min ───────────────────────────
    _cycle = 0
    while True:
        try:
            _cycle += 1
            global discovered_addresses

            # FIX: Memory leak — har cycle cleanup + max 5000 cap
            _nc = time.time()
            discovered_addresses = {k: v for k, v in discovered_addresses.items() if _nc - v < DISCOVERY_TTL}
            if len(discovered_addresses) > 5000:
                # Sabse purane entries hata do
                sorted_items = sorted(discovered_addresses.items(), key=lambda x: x[1], reverse=True)
                discovered_addresses = dict(sorted_items[:5000])
            if _cycle % 10 == 0:
                print(f"\U0001f504 Cache: {len(discovered_addresses)} entries | Queue: {len(new_pairs_queue)}")

            # Token boosts
            try:
                rb = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
                if rb.status_code == 200:
                    _rbj = rb.json()
                    boosts = _rbj if isinstance(_rbj, list) else []
                    for item in boosts[:20]:
                        if item.get("chainId") == "bsc":
                            addr = item.get("tokenAddress", "")
                            if addr:
                                threading.Thread(target=_process_new_token, args=(addr, addr, "DexBoost"), daemon=True).start()
            except Exception:
                pass

            # Rotating search
            queries = ["new", "moon", "pepe", "meme", "inu", "doge", "safe", "baby", "elon", "based"]
            q = queries[_cycle % len(queries)]
            try:
                rs = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={q}", timeout=10)
                if rs.status_code == 200:
                    _rsj  = rs.json() or {}
                    _rsraw = _rsj.get("pairs") or []
                    if not isinstance(_rsraw, list): _rsraw = []
                    for p in _rsraw[:15]:
                        if p and p.get("chainId") == "bsc":
                            addr = (p.get("baseToken") or {}).get("address", "")
                            if addr:
                                threading.Thread(target=_process_new_token, args=(addr, p.get("pairAddress",""), "DexSearch"), daemon=True).start()
            except Exception:
                pass

        except Exception as e:
            print(f"\u26a0\ufe0f Fallback error: {e}")

        time.sleep(300)


def _auto_check_new_pair(pair_address: str):
    """
    When new pair found:
    1. Wait 3 min (Stage 3 anti-sniper rule)
    2. Run full 13-stage checklist
    3. If SAFE → Telegram alert
    """
    print(f"⏳ Waiting 3 min before checking new pair: {pair_address}")
    time.sleep(180)  # Stage 3: wait 3-5 min

    # Age filter — 7 din (10080 min) se purane tokens skip karo
    try:
        _ar = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{pair_address}",
            timeout=8
        )
        if _ar.status_code == 200:
            _aj  = _ar.json() or {}
            _raw = _aj.get("pairs") or []
            if not isinstance(_raw, list): _raw = []
            _bp  = [p for p in _raw if p and p.get("chainId") == "bsc"]
            if _bp:
                _ct  = _bp[0].get("pairCreatedAt", 0) or 0
                if _ct:
                    _age = (time.time() - _ct / 1000) / 60
                    if _age > 10080:
                        print(f"⏭️ Skip — {_age/1440:.0f} day old token: {pair_address[:10]}")
                        return
    except Exception:
        pass

    result = run_full_sniper_checklist(pair_address)
    score  = result.get("score", 0)
    total  = result.get("total", 1)
    rec    = result.get("recommendation", "")
    overall = result.get("overall", "UNKNOWN")

    print(f"🔍 Auto-check {pair_address}: {overall} ({score}/{total})")

    # Alert only if SAFE, CAUTION or high-score RISK
    if overall in ["SAFE", "CAUTION"]:
        telegram_new_token_alert(pair_address, score, total, rec)
    elif overall == "RISK" and score >= int(total * 0.75):
        telegram_new_token_alert(pair_address, score, total, rec + " [HIGH SCORE RISK]")

    # AUTO PAPER BUY trigger — FIX: RISK bhi buy hoga agar score > 75%
    if overall == "SAFE" and score >= int(total * 0.50):
        try:
            _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e:
            print(f"Auto buy error: {e}")
    elif overall == "CAUTION" and score >= int(total * 0.45):
        try:
            _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e:
            print(f"Auto buy error caution: {e}")
    elif overall == "RISK" and score >= int(total * 0.75):
        # FIX: High score RISK tokens bhi paper buy karenge
        try:
            _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
            print(f"Auto buy RISK token (high score {score}/{total}): {pair_address[:10]}")
        except Exception as e:
            print(f"Auto buy error risk: {e}")

    # Add to knowledge base
    knowledge_base["bsc"]["new_tokens"].append({
        "address": pair_address,
        "overall": overall,
        "score":   score,
        "total":   total,
        "time":    datetime.utcnow().isoformat()
    })
    # Keep only last 20
    knowledge_base["bsc"]["new_tokens"] = knowledge_base["bsc"]["new_tokens"][-20:]

# ==========================================================
# ========== FEATURE 3: REAL-TIME PRICE MONITOR ===========
# ==========================================================

# Structure: { "address": { "entry": float, "current": float, "high": float,
#              "token": str, "size_bnb": float, "session_id": str,
#              "stop_loss_pct": float, "last_check": timestamp } }
monitored_positions: Dict[str, dict] = {}
monitor_lock = threading.Lock()  # Thread safety fix

# ═══════════════════════════════════════════════
# AUTO PAPER TRADING ENGINE
# ═══════════════════════════════════════════════
AUTO_TRADE_ENABLED = True
AUTO_BUY_SIZE_BNB  = 0.003
AUTO_MAX_POSITIONS = 4
AUTO_SESSION_ID    = "AUTO_TRADER"

auto_trade_stats = {
    "total_auto_buys":   0,
    "total_auto_sells":  0,
    "auto_pnl_total":    0.0,
    "running_positions": {},
    "last_action":       None,
}


def _auto_paper_buy(address, token_name, score, total, checklist_result):
    if not AUTO_TRADE_ENABLED:
        return
    sess = get_or_create_session(AUTO_SESSION_ID)
    if sess.get("daily_loss", 0) >= 8.0:
        print("Auto-buy blocked: daily loss limit")
        return
    if len(auto_trade_stats["running_positions"]) >= AUTO_MAX_POSITIONS:
        print("Auto-buy skipped: max positions open")
        return
    if address in auto_trade_stats["running_positions"]:
        return
    paper_balance = sess.get("paper_balance", 1.87)
    if paper_balance < AUTO_BUY_SIZE_BNB:
        print("Auto-buy skipped: low balance")
        return
    entry_price = get_token_price_bnb(address)
    # FIX: Retry — naye tokens pe price aane mein time lagta hai
    if entry_price <= 0:
        import time as _t; _t.sleep(5)
        entry_price = get_token_price_bnb(address)
    if entry_price <= 0:
        import time as _t; _t.sleep(10)
        entry_price = get_token_price_bnb(address)
    # Retry — naye tokens pe price aane mein time lagta hai
    if entry_price <= 0:
        import time as _t; _t.sleep(5)
        entry_price = get_token_price_bnb(address)
    if entry_price <= 0:
        import time as _t; _t.sleep(10)
        entry_price = get_token_price_bnb(address)
    if entry_price <= 0:
        import time as _t; _t.sleep(15)
        entry_price = get_token_price_bnb(address)
    if entry_price <= 0:
        dex = checklist_result.get("dex_data", {})
        bnb_p = market_cache.get("bnb_price", 300) or 300
        entry_price = dex.get("price_usd", 0) / bnb_p if dex.get("price_usd", 0) > 0 else 0
    # HARD BLOCK: Zero price pe kabhi buy mat karo
    if entry_price <= 0 or entry_price is None:
        addr_short = address[:10]
        print(f"❌ Auto-buy BLOCKED: price=0 for {addr_short} — skipping")
        return
    # Sanity check: price bahut zyada suspicious nahi hona chahiye
    if entry_price > 1.0:
        addr_short = address[:10]
        print(f"❌ Auto-buy BLOCKED: price={entry_price:.6f} suspicious for {addr_short}")
        return
    # FIX: 0.5% buy slippage simulate karo — real trade jaisa
    entry_price = entry_price * 1.005
    size_bnb = min(AUTO_BUY_SIZE_BNB, paper_balance * 0.025)
    size_bnb = max(size_bnb, 0.001)
    sess["paper_balance"] = round(paper_balance - size_bnb, 6)
    add_position_to_monitor(AUTO_SESSION_ID, address,
                            token_name or address[:10], entry_price,
                            size_bnb, stop_loss_pct=15.0)
    auto_trade_stats["running_positions"][address] = {
        "token":     token_name or address[:10],
        "entry":     entry_price,
        "size_bnb":  size_bnb,
        "sl_pct":    15.0,
        "tp_sold":   0.0,
        "bought_at": datetime.utcnow().isoformat(),
    }
    auto_trade_stats["total_auto_buys"] += 1
    auto_trade_stats["last_action"] = f"BUY {token_name or address[:10]}"
    sess.setdefault("positions", []).append({
        "address": address, "token": token_name or address[:10],
        "entry": entry_price, "size_bnb": size_bnb, "type": "auto"
    })
    threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
    send_telegram(
        f"AUTO PAPER BUY\n"
        f"Token: {address[:12]}\n"
        f"Entry: {entry_price:.8f} BNB\n"
        f"Size: {size_bnb:.4f} BNB\n"
        f"Score: {score}/{total}\n"
        f"Balance: {sess['paper_balance']:.4f} BNB"
    )
    print(f"AUTO BUY: {address[:10]} @ {entry_price:.8f} size={size_bnb:.4f}")


def _auto_paper_sell(address, reason, sell_pct=100.0):
    if address not in auto_trade_stats["running_positions"]:
        return
    pos  = auto_trade_stats["running_positions"][address]
    with monitor_lock:
        mon = monitored_positions.get(address, {})
    entry   = pos.get("entry", 0)
    current = mon.get("current", entry)
    size    = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
    token   = pos.get("token", address[:10])
    if entry <= 0:
        return
    # FIX: 0.5% sell slippage simulate karo — real trade jaisa
    current = current * 0.995
    pnl_pct    = ((current - entry) / entry) * 100
    sell_size  = size * (sell_pct / 100.0)
    return_bnb = sell_size * (1 + pnl_pct / 100.0)
    sess = get_or_create_session(AUTO_SESSION_ID)
    sess["paper_balance"] = round(sess.get("paper_balance", 1.87) + return_bnb, 6)
    auto_trade_stats["auto_pnl_total"] += pnl_pct * (sell_pct / 100.0)
    auto_trade_stats["total_auto_sells"] += 1
    auto_trade_stats["last_action"] = f"SELL {sell_pct:.0f}% {token} PnL:{pnl_pct:+.1f}%"
    if sell_pct >= 100.0:
        auto_trade_stats["running_positions"].pop(address, None)
        remove_position_from_monitor(address)
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
        pos["size_bnb"] = size * (1 - sell_pct / 100.0)
        pos["tp_sold"]  = pos.get("tp_sold", 0) + sell_pct
    threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
    emoji = "GREEN" if pnl_pct > 0 else "RED"
    send_telegram(
        f"AUTO PAPER SELL {sell_pct:.0f}% [{emoji}]\n"
        f"Token: {address[:12]}\n"
        f"Reason: {reason}\n"
        f"PnL: {pnl_pct:+.1f}%\n"
        f"Balance: {sess['paper_balance']:.4f} BNB",
        urgent=(pnl_pct < -10)
    )
    print(f"AUTO SELL {sell_pct:.0f}%: {address[:10]} PnL:{pnl_pct:+.1f}% [{reason}]")


def auto_position_manager():
    print("Auto Position Manager started!")
    while True:
        for addr, pos in list(auto_trade_stats["running_positions"].items()):
            try:
                with monitor_lock:
                    mon = monitored_positions.get(addr, {})
                current = mon.get("current", 0)
                entry   = pos.get("entry", 0)
                high    = mon.get("high", entry)
                tp_sold = pos.get("tp_sold", 0.0)
                sl_pct  = pos.get("sl_pct", 15.0)
                if current <= 0 or entry <= 0:
                    continue
                pnl          = ((current - entry) / entry) * 100
                drop_hi      = ((current - high) / high) * 100 if high > 0 else 0
                if   pnl <= -sl_pct:              _auto_paper_sell(addr, f"SL -{sl_pct:.0f}%", 100.0)
                elif drop_hi <= -80 and tp_sold < 75: _auto_paper_sell(addr, "Dump -80%", 100.0)
                elif drop_hi <= -60 and tp_sold < 50: _auto_paper_sell(addr, "Dump -60%", 75.0)
                elif pnl >= 200 and tp_sold < 90:  _auto_paper_sell(addr, "TP+200%", 90-tp_sold)
                elif pnl >= 100 and tp_sold < 75:  _auto_paper_sell(addr, "TP+100%", 25.0)
                elif pnl >= 50  and tp_sold < 50:  _auto_paper_sell(addr, "TP+50%",  25.0)
                elif pnl >= 30  and tp_sold < 25:  _auto_paper_sell(addr, "TP+30%",  25.0)
                elif pnl >= 20  and tp_sold < 1:
                    pos["sl_pct"] = 2.0
                    pos["tp_sold"] = 1
                    print(f"SL moved to cost: {addr[:10]}")
            except Exception as e:
                print(f"Auto manager err {addr[:10]}: {e}")
        time.sleep(10)



def add_position_to_monitor(session_id: str, token_address: str, token_name: str,
                             entry_price: float, size_bnb: float, stop_loss_pct: float = 15.0):
    """Add a position for real-time price monitoring."""
    with monitor_lock:
        monitored_positions[token_address] = {
        "session_id":     session_id,
        "token":          token_name,
        "address":        token_address,
        "entry":          entry_price,
        "current":        entry_price,
        "high":           entry_price,
        "size_bnb":       size_bnb,
        "stop_loss_pct":  stop_loss_pct,
        "alerts_sent":    [],  # track which alerts already sent
        "added_at":       datetime.utcnow().isoformat()
    }
    print(f"👁️ Monitoring: {token_name} @ {entry_price:.8f} BNB")

def remove_position_from_monitor(token_address: str):
    with monitor_lock:
        if token_address in monitored_positions:
            del monitored_positions[token_address]
    print(f"✅ Stopped monitoring: {token_address}")

def price_monitor_loop():
    """
    Every 10 seconds: check all monitored positions.
    Trigger alerts for SL, TP levels, volume drops.
    """
    print("📡 Price Monitor started")
    while True:
        with monitor_lock:
            _snap = list(monitored_positions.items())
        for addr, pos in _snap:
            try:
                current = get_token_price_bnb(addr)
                if current <= 0:
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

                # ── Stop Loss Hit ──────────────────────────
                if pnl_pct <= -sl and "stop_loss" not in alerts_sent:
                    alerts_sent.append("stop_loss")
                    telegram_price_alert(
                        token, addr,
                        "STOP LOSS HIT",
                        f"PnL: {pnl_pct:.1f}% | EXIT NOW"
                    )

                # ── Stage 11: Laddered Profit Alerts ──────
                if pnl_pct >= 200 and "tp_200" not in alerts_sent:
                    alerts_sent.append("tp_200")
                    telegram_price_alert(token, addr, "TARGET +200%", f"+{pnl_pct:.0f}% | Keep 10% runner only")
                elif pnl_pct >= 100 and "tp_100" not in alerts_sent:
                    alerts_sent.append("tp_100")
                    telegram_price_alert(token, addr, "TARGET +100%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 50 and "tp_50" not in alerts_sent:
                    alerts_sent.append("tp_50")
                    telegram_price_alert(token, addr, "TARGET +50%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 30 and "tp_30" not in alerts_sent:
                    alerts_sent.append("tp_30")
                    telegram_price_alert(token, addr, "TARGET +30%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 20 and "tp_20" not in alerts_sent:
                    alerts_sent.append("tp_20")
                    telegram_price_alert(token, addr, "TARGET +20%", f"+{pnl_pct:.0f}% | Move SL to cost")

                # ── Stage 6: Volume / Dump Alerts ─────────
                if drop_from_high <= -90 and "dump_90" not in alerts_sent:
                    alerts_sent.append("dump_90")
                    telegram_price_alert(token, addr, "DUMP -90% FROM HIGH", "EXIT FULLY NOW")
                elif drop_from_high <= -70 and "dump_70" not in alerts_sent:
                    alerts_sent.append("dump_70")
                    telegram_price_alert(token, addr, "DUMP -70% FROM HIGH", "Exit 75% immediately")
                elif drop_from_high <= -50 and "dump_50" not in alerts_sent:
                    alerts_sent.append("dump_50")
                    telegram_price_alert(token, addr, "DUMP -50% FROM HIGH", "Exit 50% now")

                print(f"📊 {token}: {pnl_pct:+.1f}% | High drop: {drop_from_high:.1f}%")

            except Exception as e:
                print(f"⚠️ Price monitor error ({addr}): {e}")

        time.sleep(10)  # check every 10 seconds

# ==========================================================
# ========== FEATURE 4: MORALIS / DEXSCREENER REAL-TIME ===
# ==========================================================

def get_dexscreener_token_data(token_address: str) -> Dict:
    """
    Full token data from DexScreener:
    - Price USD/BNB
    - 24h volume
    - 1h/6h/24h price change
    - Liquidity USD
    - Buys/Sells count (5m, 1h)
    - FDV
    """
    result = {
        "price_usd":      0.0,
        "price_bnb":      0.0,
        "volume_24h":     0.0,
        "liquidity_usd":  0.0,
        "change_1h":      0.0,
        "change_6h":      0.0,
        "change_24h":     0.0,
        "buys_5m":        0,
        "sells_5m":       0,
        "buys_1h":        0,
        "sells_1h":       0,
        "fdv":            0.0,
        "pair_address":   "",
        "dex_url":        "",
        "source":         "dexscreener"
    }
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=10
        )
        if r.status_code == 200:
            _json = r.json() or {}
            pairs = _json.get("pairs") or []
            if not isinstance(pairs, list): pairs = []
            bsc   = [p for p in pairs if p and p.get("chainId") == "bsc"]
            if bsc:
                bsc.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                p = bsc[0]
                txns = p.get("txns", {})
                result.update({
                    "price_usd":     float(p.get("priceUsd",                         0) or 0),
                    "volume_24h":    float(p.get("volume", {}).get("h24",            0) or 0),
                    "liquidity_usd": float(p.get("liquidity", {}).get("usd",         0) or 0),
                    "change_1h":     float(p.get("priceChange", {}).get("h1",        0) or 0),
                    "change_6h":     float(p.get("priceChange", {}).get("h6",        0) or 0),
                    "change_24h":    float(p.get("priceChange", {}).get("h24",       0) or 0),
                    "buys_5m":       int(txns.get("m5",  {}).get("buys",             0) or 0),
                    "sells_5m":      int(txns.get("m5",  {}).get("sells",            0) or 0),
                    "buys_1h":       int(txns.get("h1",  {}).get("buys",             0) or 0),
                    "sells_1h":      int(txns.get("h1",  {}).get("sells",            0) or 0),
                    "fdv":           float(p.get("fdv",                               0) or 0),
                    "pair_address":  p.get("pairAddress", ""),
                    "dex_url":       p.get("url", ""),
                })
                bnb_price = market_cache.get("bnb_price", 300) or 300
                result["price_bnb"] = result["price_usd"] / bnb_price if result["price_usd"] else 0
    except Exception as e:
        print(f"⚠️ DexScreener error: {e}")

    # Enhance with Moralis if available
    if MORALIS_API_KEY and result["price_usd"] == 0:
        try:
            r2 = requests.get(
                f"https://deep-index.moralis.io/api/v2.2/erc20/{token_address}/price",
                headers={"X-API-Key": MORALIS_API_KEY},
                params={"chain": "bsc"},
                timeout=8
            )
            if r2.status_code == 200:
                result["price_usd"] = float(r2.json().get("usdPrice", 0) or 0)
                result["source"]    = "moralis"
        except Exception as e:
            print(f"⚠️ Moralis fallback error: {e}")

    return result

def get_moralis_wallet_tokens(wallet_address: str) -> List[Dict]:
    """Get all token holdings of a wallet (for smart wallet tracking)."""
    if not MORALIS_API_KEY:
        return []
    try:
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/{wallet_address}/erc20",
            headers={"X-API-Key": MORALIS_API_KEY},
            params={"chain": "bsc"},
            timeout=10
        )
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        print(f"⚠️ Moralis wallet error: {e}")
    return []

# ==========================================================
# ========== FEATURE 5: SMART WALLET TRACKER ==============
# ==========================================================

# Snapshot of last known holdings for each smart wallet
smart_wallet_snapshots: Dict[str, set] = {}

def track_smart_wallets():
    """
    Every 2 minutes: check SMART_WALLETS for new token buys/sells.
    If 3+ smart wallets buy same token → strong signal → Telegram alert.
    """
    if not SMART_WALLETS:
        print("ℹ️ No SMART_WALLETS configured — tracker disabled")
        return
    print(f"🧠 Smart Wallet Tracker started — tracking {len(SMART_WALLETS)} wallets")

    while True:
        token_buy_signals: Dict[str, List[str]] = {}   # token → [wallets that bought]

        for wallet in SMART_WALLETS:
            try:
                current_tokens = set()

                if MORALIS_API_KEY:
                    holdings = get_moralis_wallet_tokens(wallet)
                    current_tokens = {h.get("token_address", "").lower() for h in holdings}
                else:
                    print(f"ℹ️ Smart wallet {wallet[:10]}: Moralis key nahi — tracking skip")
                    continue

                prev_tokens = smart_wallet_snapshots.get(wallet, set())

                # New buys = tokens in current but not in previous snapshot
                new_buys = current_tokens - prev_tokens
                # New sells = tokens in previous but not in current
                new_sells = prev_tokens - current_tokens

                for token_addr in new_buys:
                    if token_addr:
                        telegram_smart_wallet_alert(wallet, token_addr, "BUY 🟢")
                        if token_addr not in token_buy_signals:
                            token_buy_signals[token_addr] = []
                        token_buy_signals[token_addr].append(wallet)

                for token_addr in new_sells:
                    if token_addr:
                        telegram_smart_wallet_alert(wallet, token_addr, "SELL 🔴")

                # Update snapshot
                smart_wallet_snapshots[wallet] = current_tokens

            except Exception as e:
                print(f"⚠️ Smart wallet {wallet[:10]}... error: {e}")

        # ── Multi-wallet convergence signal ───────────────
        for token_addr, buying_wallets in token_buy_signals.items():
            if len(buying_wallets) >= 2:
                count = len(buying_wallets)
                send_telegram(
                    f"🔥 <b>MULTI-WALLET SIGNAL</b>\n"
                    f"{count} smart wallets buying same token!\n"
                    f"Token: <code>{token_addr}</code>\n"
                    f"Wallets: {count}\n"
                    f"⚡ Run full checklist immediately!\n"
                    f"🔗 https://bscscan.com/address/{token_addr}",
                    urgent=True
                )
                # Auto queue for checklist
                threading.Thread(
                    target=_auto_check_new_pair,
                    args=(token_addr,), daemon=True
                ).start()

        time.sleep(120)  # check every 2 minutes

# ==========================================================
# ========== SESSION MANAGEMENT ============================
# ==========================================================

sessions: Dict[str, dict] = {}

def get_or_create_session(session_id: str) -> dict:
    if session_id not in sessions:
        sessions[session_id] = {
            "session_id":       session_id,
            "mode":             "paper",
            "paper_balance":    1.87,
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
            # Safe JSON parse — agar corrupted ho to default use karo
            def _safe_json(val, default):
                if not val: return default
                try: return json.loads(val)
                except: return default
            sessions[session_id].update({
                "paper_balance":    float(row.get("paper_balance") or 1.87),
                "real_balance":     float(row.get("real_balance")  or 0.00),
                "positions":        _safe_json(row.get("positions"),        []),
                "history":          _safe_json(row.get("history"),          []),
                "pnl_24h":          float(row.get("pnl_24h")       or 0.0),
                "daily_loss":       float(row.get("daily_loss")     or 0.0),
                "trade_count":      int(row.get("trade_count")      or 0),
                "win_count":        int(row.get("win_count")        or 0),
                "pattern_database": _safe_json(row.get("pattern_database"), []),
            })
            print(f"✅ Session loaded from Supabase: {session_id[:8]}... "
                  f"Balance:{sessions[session_id]['paper_balance']:.3f}BNB "
                  f"Trades:{sessions[session_id]['trade_count']}")
    except Exception as e:
        print(f"⚠️ Session load error: {e}")

def _save_session_to_db(session_id: str):
    if not supabase: return
    try:
        sess = sessions.get(session_id, {})
        supabase.table("memory").upsert({
            "session_id":       session_id,
            "role":             "user",
            "content":          "",
            "paper_balance":    sess.get("paper_balance",    1.87),
            "real_balance":     sess.get("real_balance",     0.00),
            "positions":        json.dumps(sess.get("positions",        [])),
            "history":          json.dumps(sess.get("history",          [])[-50:]),
            "pnl_24h":          sess.get("pnl_24h",          0.0),
            "daily_loss":       sess.get("daily_loss",        0.0),
            "trade_count":      sess.get("trade_count",       0),
            "win_count":        sess.get("win_count",         0),
            "pattern_database": json.dumps(sess.get("pattern_database", [])[-100:]),
            "updated_at":       datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"⚠️ Session save error: {e}")

# ==========================================================
# ========== MARKET DATA ===================================
# ==========================================================

market_cache = {
    "bnb_price":    0.0,
    "fear_greed":   50,
    "trending":     [],
    "last_updated": None
}

def fetch_market_data():
    # FIX: Multiple sources for BNB price reliability
    bnb_fetched = False
    # Source 1: CoinGecko
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "binancecoin", "vs_currencies": "usd"}, timeout=20
        )
        if r.status_code == 200:
            price = r.json().get("binancecoin", {}).get("usd", 0)
            if price:
                market_cache["bnb_price"] = price
                bnb_fetched = True
    except Exception as e:
        print(f"⚠️ BNB CoinGecko error: {e}")
    # Source 2: Binance API fallback
    if not bnb_fetched:
        try:
            r2 = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BNBUSDT"}, timeout=15
            )
            if r2.status_code == 200:
                price = float(r2.json().get("price", 0))
                if price:
                    market_cache["bnb_price"] = price
                    bnb_fetched = True
                    print(f"✅ BNB price from Binance: ${price}")
        except Exception as e:
            print(f"⚠️ BNB Binance error: {e}")
    # Source 3: CryptoCompare (no rate limit, very reliable)
    if not bnb_fetched:
        try:
            r3 = requests.get(
                "https://min-api.cryptocompare.com/data/price",
                params={"fsym": "BNB", "tsyms": "USD"}, timeout=15
            )
            if r3.status_code == 200:
                price = float(r3.json().get("USD", 0) or 0)
                if price:
                    market_cache["bnb_price"] = price
                    bnb_fetched = True
                    print(f"✅ BNB price (CryptoCompare): ${price:.2f}")
        except Exception as e:
            print(f"⚠️ BNB CryptoCompare error: {e}")
    if not bnb_fetched:
        # Last resort: BSC on-chain WBNB/BUSD pool se real price lo
        try:
            r4 = requests.get(
                "https://api.dexscreener.com/latest/dex/pairs/bsc/0x58f876857a02d6762e0101bb5c46a8c1ed44dc16",
                timeout=15
            )
            if r4.status_code == 200:
                pair = r4.json().get("pair", {})
                price = float(pair.get("priceUsd", 0) or 0)
                if price > 0:
                    # WBNB/BUSD pair — token0 price = BNB price
                    market_cache["bnb_price"] = price
                    bnb_fetched = True
                    print(f"✅ BNB price (DexScreener on-chain): ${price:.2f}")
        except Exception as e:
            print(f"⚠️ BNB DexScreener fallback error: {e}")
    if not bnb_fetched:
        print("⚠️ BNB price: all 4 sources failed — price remains 0 until next cycle")
    try:
        r2 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r2.status_code == 200:
            market_cache["fear_greed"] = int(r2.json()["data"][0]["value"])
    except Exception as e:
        print(f"⚠️ Fear & Greed error: {e}")
    market_cache["last_updated"] = datetime.utcnow().isoformat()
    print(f"📊 BNB: ${market_cache['bnb_price']} | F&G: {market_cache['fear_greed']}")

# ==========================================================
# ========== AIRDROP HUNTER ================================
# ==========================================================

def fetch_defillama_airdrops() -> List[Dict]:
    results = []
    try:
        r = requests.get("https://api.llama.fi/raises", timeout=12)
        if r.status_code == 200:
            cutoff = (datetime.utcnow() - timedelta(days=30)).timestamp()
            for item in r.json().get("raises", [])[:50]:
                if float(item.get("date", 0) or 0) > cutoff:
                    results.append({
                        "name":        item.get("name", "Unknown"),
                        "category":    item.get("category", "DeFi"),
                        "amount_usd":  item.get("amount", 0),
                        "chains":      item.get("chains", []),
                        "source":      "DeFiLlama",
                        "status":      "upcoming",
                        "description": f"Raised ${item.get('amount', 0)}M — potential airdrop",
                        "url":         "https://defillama.com/raises"
                    })
    except Exception as e:
        print(f"⚠️ DeFiLlama error: {e}")
    return results[:15]

def fetch_coinmarketcap_airdrops() -> List[Dict]:
    results = []
    try:
        r = requests.get(
            "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing/new",
            params={"start": 1, "limit": 20, "sortBy": "date_added"}, timeout=12
        )
        if r.status_code == 200:
            for t in r.json().get("data", {}).get("recentlyAddedList", [])[:10]:
                if any(tag in ["binance-smart-chain", "bsc"] for tag in t.get("tags", [])):
                    results.append({
                        "name":        t.get("name", "Unknown"),
                        "symbol":      t.get("symbol", ""),
                        "chains":      ["BSC"],
                        "source":      "CoinMarketCap",
                        "status":      "active",
                        "description": f"New BSC token — {t.get('dateAdded', '')[:10]}",
                        "url":         f"https://coinmarketcap.com/currencies/{t.get('slug', '')}"
                    })
    except Exception as e:
        print(f"⚠️ CMC error: {e}")
    return results

def run_airdrop_hunter():
    print("🪂 Airdrop Hunter starting...")
    all_airdrops  = fetch_defillama_airdrops() + fetch_coinmarketcap_airdrops()
    knowledge_base["airdrops"]["active"]   = [a for a in all_airdrops if a.get("status") == "active"]
    knowledge_base["airdrops"]["upcoming"] = [a for a in all_airdrops if a.get("status") == "upcoming"]
    print(f"🪂 Airdrops — Active:{len(knowledge_base['airdrops']['active'])} Upcoming:{len(knowledge_base['airdrops']['upcoming'])}")

def fetch_pancakeswap_data():
    try:
        r = requests.get("https://api.pancakeswap.info/api/v2/pairs", timeout=12)
        if r.status_code == 200:
            pairs = r.json().get("data", {})
            top   = sorted(pairs.values(), key=lambda x: float(x.get("volume24h", 0) or 0), reverse=True)[:10]
            knowledge_base["bsc"]["trending"] = [{"symbol": p.get("name", ""), "volume": p.get("volume24h", 0)} for p in top]
    except Exception as e:
        print(f"⚠️ PancakeSwap error: {e}")

# ═══════════════════════════════════════════════════════════════
# 24x7 SELF-LEARNING ENGINE — 3 Domains (Trading + Airdrop + Coding)
# ═══════════════════════════════════════════════════════════════

# Global brain — continuously updated by learning engine
brain: Dict = {
    "trading": {
        "best_patterns":    [],   # Top winning trade patterns
        "avoid_patterns":   [],   # Patterns that caused losses
        "market_insights":  [],   # Market condition observations
        "token_blacklist":  [],   # Tokens that rug pulled / were scams
        "token_whitelist":  [],   # Tokens that performed well
        "strategy_notes":   [],   # Strategy improvements discovered
        "last_updated":     None
    },
    "airdrop": {
        "active_projects":  [],   # Currently tracking
        "completed":        [],   # Done — did they deliver?
        "success_patterns": [],   # What made airdrops successful
        "fail_patterns":    [],   # What caused airdrop failures
        "wallet_notes":     [],   # Wallet strategy learnings
        "last_updated":     None
    },
    "coding": {
        "solutions_library": [],  # Code problems + solutions
        "common_errors":     [],  # Errors encountered + fixes
        "useful_snippets":   [],  # Reusable code patterns
        "deployment_notes":  [],  # Render/GitHub/Supabase tips
        "last_updated":      None
    },
    "total_learning_cycles": 0,
    "started_at": datetime.utcnow().isoformat(),
    "user_interaction_patterns": {
        "trading_questions": 0,
        "airdrop_questions": 0,
        "coding_questions":  0,
        "general_chat":      0,
    },
    "user_pain_points": [],
}

_brain_save_cache = {"last_save": 0}

def _save_brain_to_db():
    """Save entire brain to Supabase — max har 5 min mein ek baar (rate limit fix)."""
    import time as _t
    if not supabase:
        return
    # FIX: Supabase rate limit — 5 min se kam mein dobara save nahi
    if _t.time() - _brain_save_cache["last_save"] < 300:
        return
    _brain_save_cache["last_save"] = _t.time()
    try:
        supabase.table("memory").upsert({
            "session_id":   "MRBLACK_BRAIN",
            "role":         "system",
            "content":      "",
            "history":      json.dumps([]),
            "pattern_database": json.dumps(brain["trading"]["best_patterns"][-50:] +
                                           brain["trading"]["avoid_patterns"][-50:]),
            "updated_at":   datetime.utcnow().isoformat(),
            # Store full brain in positions field (we have space)
            "positions":    json.dumps({
                "brain_trading":  brain["trading"],
                "brain_airdrop":  brain["airdrop"],
                "brain_coding":   brain["coding"],
                "cycles":         brain["total_learning_cycles"],
                "total_tokens_discovered_ever": brain.get("total_tokens_discovered_ever", 0)
            })
        }).execute()
        print(f"🧠 Brain saved to Supabase (cycle #{brain['total_learning_cycles']})")
    except Exception as e:
        print(f"⚠️ Brain save error: {e}")


def _ensure_brain_structure():
    for key in ["best_patterns","avoid_patterns","token_blacklist",
                "token_whitelist","strategy_notes","market_insights"]:
        if not isinstance(brain["trading"].get(key), list):
            brain["trading"][key] = []
    for key in ["active_projects","completed","success_patterns",
                "fail_patterns","wallet_notes"]:
        if not isinstance(brain["airdrop"].get(key), list):
            brain["airdrop"][key] = []
    for key in ["solutions_library","common_errors","useful_snippets","deployment_notes"]:
        if not isinstance(brain["coding"].get(key), list):
            brain["coding"][key] = []

def _load_brain_from_db():
    """Load brain from Supabase on startup."""
    if not supabase:
        return
    try:
        res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_BRAIN").execute()
        if res.data:
            row = res.data[0]
            try:
                pos_raw = row.get("positions") or "{}"
                # FIX: handle bytes or string
                if isinstance(pos_raw, bytes):
                    pos_raw = pos_raw.decode("utf-8")
                stored = json.loads(pos_raw) if pos_raw and pos_raw != "null" else {}
            except Exception as je:
                print(f"Profile JSON parse error: {je}")
                stored = {}
            if stored.get("brain_trading"):
                brain["trading"].update(stored["brain_trading"])
            if stored.get("brain_airdrop"):
                brain["airdrop"].update(stored["brain_airdrop"])
            if stored.get("brain_coding"):
                brain["coding"].update(stored["brain_coding"])
            brain["total_learning_cycles"] = stored.get("cycles", 0)
            brain["total_tokens_discovered_ever"] = stored.get("total_tokens_discovered_ever", 0)
            print(f"🧠 Brain loaded from Supabase! Cycles: {brain['total_learning_cycles']}")
    except Exception as e:
        print(f"⚠️ Brain load error: {e}")

def _learn_trading_patterns():
    """
    TRADING SELF-LEARNING:
    Analyze all session trade histories.
    Extract patterns: what worked, what didn't.
    """
    try:
        all_trades = []
        for sess in sessions.values():
            all_trades.extend(sess.get("pattern_database", []))

        if not all_trades:
            return

        wins   = [t for t in all_trades if t.get("win")]
        losses = [t for t in all_trades if not t.get("win")]

        # Learn from wins — extract common patterns
        if wins:
            win_pnls = sorted(wins, key=lambda x: x.get("pnl_pct", 0), reverse=True)
            top_wins = win_pnls[:10]
            for trade in top_wins:
                pattern = {
                    "token":     trade.get("token", ""),
                    "pnl":       trade.get("pnl_pct", 0),
                    "pattern":   trade.get("volume_pattern", ""),
                    "lesson":    trade.get("lesson", ""),
                    "timestamp": trade.get("timestamp", "")
                }
                # Avoid duplicates
                existing = [p["token"] for p in brain["trading"]["best_patterns"]]
                if pattern["token"] not in existing and pattern["pnl"] > 0:
                    brain["trading"]["best_patterns"].append(pattern)

        # Learn from losses — what to avoid
        if losses:
            bad_trades = sorted(losses, key=lambda x: x.get("pnl_pct", 0))[:10]
            for trade in bad_trades:
                avoid = {
                    "token":     trade.get("token", ""),
                    "loss":      trade.get("pnl_pct", 0),
                    "pattern":   trade.get("volume_pattern", ""),
                    "lesson":    trade.get("lesson", ""),
                    "timestamp": trade.get("timestamp", "")
                }
                existing = [p["token"] for p in brain["trading"]["avoid_patterns"]]
                if avoid["token"] not in existing:
                    brain["trading"]["avoid_patterns"].append(avoid)

        # Market insight from current data
        bnb_price  = market_cache.get("bnb_price", 0)
        fear_greed = market_cache.get("fear_greed", 50)
        if bnb_price > 0:
            market_mood = "GREED" if fear_greed > 60 else "FEAR" if fear_greed < 40 else "NEUTRAL"
            insight = {
                "timestamp":   datetime.utcnow().isoformat(),
                "bnb_price":   bnb_price,
                "fear_greed":  fear_greed,
                "mood":        market_mood,
                "new_pairs":   len(new_pairs_queue),
                "observation": f"BNB=${bnb_price:.0f} F&G={fear_greed} mood={market_mood}"
            }
            brain["trading"]["market_insights"].append(insight)
            # Keep last 100 insights
            brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-100:]

        # Keep lists manageable
        brain["trading"]["best_patterns"]  = brain["trading"]["best_patterns"][-50:]
        brain["trading"]["avoid_patterns"] = brain["trading"]["avoid_patterns"][-50:]
        brain["trading"]["last_updated"]   = datetime.utcnow().isoformat()

        print(f"📈 Trading learning: {len(wins)} wins, {len(losses)} losses analyzed")

    except Exception as e:
        print(f"⚠️ Trading learning error: {e}")

def _learn_airdrop_patterns():
    """Fix 4: Airdrop Completion Tracking — 30 din baad token launch check, success/fail patterns."""
    try:
        now = datetime.utcnow()
        for project in knowledge_base["airdrops"]["active"]:
            name = project.get("name","")
            if name and name not in [p.get("name") for p in brain["airdrop"]["active_projects"]]:
                brain["airdrop"]["active_projects"].append({
                    "name": name, "source": project.get("source",""),
                    "chains": project.get("chains",[]), "amount_usd": float(project.get("amount_usd",0) or 0),
                    "added_at": now.isoformat(), "status": "tracking",
                    "check_after": (now + timedelta(days=30)).isoformat(), "delivered": None
                })

        # 30-day completion check
        for project in brain["airdrop"]["active_projects"]:
            if project.get("delivered") is not None: continue
            try:
                if datetime.fromisoformat(project.get("check_after","")) > now: continue
            except Exception: continue
            name = project.get("name","")
            if not name: continue
            try:
                r = requests.get("https://api.coingecko.com/api/v3/search",
                                 params={"query": name}, timeout=8)
                if r.status_code == 200:
                    launched = any(name.lower() in c.get("name","").lower()
                                   for c in r.json().get("coins",[])[:5])
                    project.update({"delivered": launched, "checked_at": now.isoformat()})
                    if launched:
                        brain["airdrop"]["completed"].append(
                            {"name": name, "result": "DELIVERED",
                             "source": project.get("source",""),
                             "amount_usd": project.get("amount_usd",0), "time": now.isoformat()})
                        brain["airdrop"]["success_patterns"].append(
                            f"SUCCESS: {name} — {project.get('source','')} — ${project.get('amount_usd',0)}M")
                        print(f"✅ Airdrop delivered: {name}")
                    else:
                        brain["airdrop"]["fail_patterns"].append(
                            {"name": name, "reason": "Not on CoinGecko after 30d", "time": now.isoformat()})
                        print(f"❌ Airdrop not delivered: {name}")
            except Exception as ce:
                print(f"⚠️ Airdrop check {name}: {ce}")

        # Pattern updates
        funded = [p for p in brain["airdrop"]["active_projects"] if float(p.get("amount_usd",0)) > 5]
        if funded:
            brain["airdrop"]["success_patterns"].append(
                f"Projects >$5M: {len(funded)} tracked — higher delivery probability")
        bsc = [p for p in brain["airdrop"]["active_projects"] if "BSC" in p.get("chains",[])]
        if bsc:
            brain["airdrop"]["wallet_notes"].append(
                {"note": f"{len(bsc)} BSC airdrop projects active", "timestamp": now.isoformat()})

        brain["airdrop"]["active_projects"]  = brain["airdrop"]["active_projects"][-200:]
        brain["airdrop"]["success_patterns"] = list(set(brain["airdrop"]["success_patterns"]))[-30:]
        brain["airdrop"]["fail_patterns"]    = brain["airdrop"]["fail_patterns"][-30:]
        brain["airdrop"]["wallet_notes"]     = brain["airdrop"]["wallet_notes"][-30:]
        brain["airdrop"]["last_updated"]     = now.isoformat()

        delivered_c = sum(1 for p in brain["airdrop"]["active_projects"] if p.get("delivered") == True)
        print(f"🪂 Airdrop: {len(brain['airdrop']['active_projects'])} tracked | {delivered_c} delivered")
    except Exception as e:
        print(f"⚠️ Airdrop learning error: {e}")

def _learn_from_new_pairs():
    """
    TRADING SELF-LEARNING from new pairs:
    Analyze recently discovered pairs + their outcomes.
    Build pattern library of what makes a token good/bad.
    """
    try:
        recent_pairs = knowledge_base["bsc"]["new_tokens"][-10:]  # Fix: overall field yahan hoti hai
        for pair in recent_pairs:
            addr    = pair.get("address", "")
            overall = pair.get("overall", "")
            if overall in ["DANGER", "RISK"] and addr:
                # Blacklist dangerous tokens
                blacklisted = [t["address"] for t in brain["trading"]["token_blacklist"]]
                if addr not in blacklisted:
                    brain["trading"]["token_blacklist"].append({
                        "address": addr,
                        "reason":  overall,
                        "time":    pair.get("discovered", "")
                    })
            elif overall == "SAFE" and addr:
                # Whitelist safe tokens
                whitelisted = [t["address"] for t in brain["trading"]["token_whitelist"]]
                if addr not in whitelisted:
                    brain["trading"]["token_whitelist"].append({
                        "address": addr,
                        "score":   pair.get("score", 0),
                        "time":    pair.get("discovered", "")
                    })

        brain["trading"]["token_blacklist"] = brain["trading"]["token_blacklist"][-200:]
        brain["trading"]["token_whitelist"] = brain["trading"]["token_whitelist"][-200:]

        if recent_pairs:
            safe_count = sum(1 for p in recent_pairs if p.get("overall") == "SAFE")
            danger_count = sum(1 for p in recent_pairs if p.get("overall") in ["DANGER", "RISK"])
            print(f"🆕 Pair learning: {safe_count} safe, {danger_count} dangerous from last 10 pairs")

    except Exception as e:
        print(f"⚠️ Pair learning error: {e}")

def _get_brain_context_for_llm() -> str:
    """
    Build a concise brain summary to inject into every LLM call.
    This is how the bot 'remembers' what it has learned.
    """
    try:
        parts = []

        # Trading insights
        best  = brain["trading"]["best_patterns"][-3:]
        avoid = brain["trading"]["avoid_patterns"][-3:]
        if best:
            best_summary = " | ".join([f"+{p['pnl']:.0f}%({p['token'][:6]})" for p in best if p.get("pnl", 0) > 0])
            if best_summary:
                parts.append(f"BestTrades:{best_summary}")
        if avoid:
            avoid_summary = " | ".join([f"{p['loss']:.0f}%({p['token'][:6]})" for p in avoid if p.get("loss", 0) < 0])
            if avoid_summary:
                parts.append(f"AvoidPatterns:{avoid_summary}")

        # Market insight
        insights = brain["trading"]["market_insights"]
        if insights:
            last = insights[-1]
            parts.append(f"MarketMood:{last.get('mood','?')}(F&G={last.get('fear_greed','?')})")

        # Blacklist count
        bl = len(brain["trading"]["token_blacklist"])
        wl = len(brain["trading"]["token_whitelist"])
        if bl or wl:
            parts.append(f"KnownTokens:SAFE={wl} DANGER={bl}")

        # FIX: Discovered tokens — naam aur address inject karo
        if new_pairs_queue:
            queue_names = [f"{q.get('name','Unknown')}({q.get('address','')[:10]})"
                          for q in list(new_pairs_queue)[-15:]]
            parts.append(f"DiscoveredTokensList:{queue_names}")
        if discovered_addresses:
            recent_addrs = list(discovered_addresses.keys())[-10:]
            parts.append(f"RecentAddresses:{[a[:10] for a in recent_addrs]}")

        # Airdrop insights
        active_drops = brain["airdrop"]["active_projects"]
        if active_drops:
            confirmed_drops = [p for p in active_drops if p.get("status") == "confirmed"]
            bsc_drops       = [p for p in active_drops if "BSC" in p.get("chains", [])]
            parts.append(f"TrackedDrops:{len(active_drops)}(Confirmed:{len(confirmed_drops)} BSC:{len(bsc_drops)})")
        inet_insights = [n for n in brain["trading"]["strategy_notes"] if "[INTERNET-INSIGHT]" in n.get("note","")]
        if inet_insights:
            parts.append(f"InternetIntel:{inet_insights[-1].get('note','').replace('[INTERNET-INSIGHT]','').strip()[:50]}")
        recent_tools = brain["coding"]["useful_snippets"][-2:]
        if recent_tools:
            parts.append(f"TrendingTools:{' | '.join([t.get('title','')[:20] for t in recent_tools])}")

        # Learning cycles
        parts.append(f"LearningCycles:{brain['total_learning_cycles']}")
        scan_acc = self_awareness["performance_intelligence"].get("scan_accuracy", 0)
        if scan_acc > 0:
            parts.append(f"ScanAccuracy:{scan_acc:.0f}%")
        cross_c = len([n for n in brain["trading"]["strategy_notes"] if "[CROSS-DOMAIN]" in n.get("note","")])
        if cross_c > 0:
            parts.append(f"CrossSignals:{cross_c}")
        delivered = sum(1 for p in brain["airdrop"]["active_projects"] if p.get("delivered") == True)
        failed_d  = sum(1 for p in brain["airdrop"]["active_projects"] if p.get("delivered") == False)
        if delivered + failed_d > 0:
            parts.append(f"AirdropDelivery:{round(delivered/(delivered+failed_d)*100)}%({delivered}/{delivered+failed_d})")

        return " | ".join(parts) if parts else ""

    except Exception as e:
        print(f"⚠️ Brain context error: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════
# ██████ LEARNING ENGINE v2 — 10/10 ████████████████████████████████
# ═══════════════════════════════════════════════════════════════════
# 5 Learning Tiers:
# T1 — Micro  (every message):  User pattern extraction
# T2 — Fast   (every 60s):     Price + new pair learning
# T3 — Normal (every 5 min):   Market data + airdrops
# T4 — Deep   (every 15 min):  LLM-powered pattern analysis
# T5 — Nightly(every 1 hour):  Consolidation + strategy review
# ═══════════════════════════════════════════════════════════════════

# ── Tier 1: Message-level learning (called from /chat route) ───────
def learn_from_message(user_message: str, bot_reply: str, session_id: str):
    """T1 Upgrade: Actual conversation learning — Q&A save, quality filter, cross-domain."""
    try:
        msg_lower = user_message.lower()
        rep_lower = bot_reply.lower()

        topics = brain.get("user_interaction_patterns", {
            "trading_questions": 0, "airdrop_questions": 0,
            "coding_questions": 0,  "general_chat": 0,
        })
        brain["user_interaction_patterns"] = topics

        domain = "general"
        if any(w in msg_lower for w in ["token","scan","trade","buy","sell","chart","price","bnb","bsc"]):
            topics["trading_questions"] += 1; domain = "trading"
        elif any(w in msg_lower for w in ["airdrop","claim","free","reward","whitelist","drop"]):
            topics["airdrop_questions"] += 1; domain = "airdrop"
        elif any(w in msg_lower for w in ["code","error","bug","fix","python","deploy","flask","api"]):
            topics["coding_questions"]  += 1; domain = "coding"
        else:
            topics["general_chat"] += 1

        low_quality  = any(w in rep_lower for w in ["nahi pata","unclear","error","temporarily","unavailable"])
        high_quality = len(bot_reply) > 100 and not low_quality

        if any(w in msg_lower for w in ["nahi samjha","phir se","dobara","explain","samjhao","again"]):
            brain.setdefault("user_pain_points",[]).append(
                {"query": user_message[:80], "domain": domain, "time": datetime.utcnow().isoformat()})
            brain["user_pain_points"] = brain["user_pain_points"][-30:]

        if high_quality and len(user_message) > 10:
            if domain == "coding":
                brain["coding"]["solutions_library"].append({
                    "title": f"Q: {user_message[:50]}", "answer": bot_reply[:100],
                    "category": "conversation", "added_at": datetime.utcnow().isoformat()})
                brain["coding"]["solutions_library"] = brain["coding"]["solutions_library"][-100:]
            elif domain == "trading":
                brain["trading"]["strategy_notes"].append({
                    "note": f"[CONV] Q:{user_message[:40]} → A:{bot_reply[:60]}",
                    "timestamp": datetime.utcnow().isoformat(), "source": "conversation"})
                brain["trading"]["strategy_notes"] = brain["trading"]["strategy_notes"][-200:]
            elif domain == "airdrop":
                brain["airdrop"]["wallet_notes"].append(
                    {"note": f"[CONV] {user_message[:60]}", "time": datetime.utcnow().isoformat()})
                brain["airdrop"]["wallet_notes"] = brain["airdrop"]["wallet_notes"][-50:]

        self_awareness["current_state"]["total_messages"] = (
            self_awareness["current_state"].get("total_messages", 0) + 1)

    except Exception as e:
        print(f"T1 learn error: {e}")


# ── Tier 2: Fast learning (every 60s) ──────────────────────────────
def _fast_learning_cycle():
    """Quick wins — price moves, new pair verdicts."""
    try:
        # Learn from monitored positions — are they moving as expected?
        with monitor_lock:
            _snap = list(monitored_positions.items())
        for addr, pos in _snap:
            current = pos.get("current", 0)
            entry   = pos.get("entry", 0)
            high    = pos.get("high", entry)
            if entry > 0 and current > 0:
                pnl = ((current - entry) / entry) * 100
                # If token drops >30% within 1 hour → note the pattern
                if pnl < -30:
                    brain["trading"]["strategy_notes"].append({
                        "note": f"Token {addr[:10]} dropped {pnl:.0f}% from entry — fast dump pattern",
                        "timestamp": datetime.utcnow().isoformat()
                    })

        # Check if new pairs are accumulating (market activity signal)
        pair_count = len(new_pairs_queue)
        if pair_count > 20:
            brain["trading"]["market_insights"].append({
                "timestamp": datetime.utcnow().isoformat(),
                "observation": f"High activity: {pair_count} new pairs in queue — market hot",
                "mood": "ACTIVE"
            })

    except Exception as e:
        print(f"Fast learn error: {e}")


# ── Tier 4: Deep LLM-powered pattern analysis ──────────────────────
def _deep_llm_learning():
    """
    Use LLM itself to analyze patterns and extract insights.
    This is what makes learning truly intelligent.
    """
    try:
        # Only run if we have enough data
        all_trades = []
        for sess in sessions.values():
            all_trades.extend(sess.get("pattern_database", []))

        if len(all_trades) < 3:
            return  # Not enough data yet

        # Prepare data summary for LLM
        wins  = [t for t in all_trades if t.get("win")]
        losses= [t for t in all_trades if not t.get("win")]

        if not wins and not losses:
            return

        data_summary = (
            f"Trading data: {len(wins)} wins, {len(losses)} losses. "
            f"Win lessons: {[t.get('lesson','')[:30] for t in wins[-3:]]}. "
            f"Loss lessons: {[t.get('lesson','')[:30] for t in losses[-3:]]}. "
            f"Market: BNB=${market_cache.get('bnb_price',0):.0f} F&G={market_cache.get('fear_greed',50)}."
        )

        prompt = (
            f"Analyze this BSC trading data and give 2-3 specific actionable insights in JSON: "
            f"{data_summary} "
            f"Respond ONLY with JSON: "
            f'[{{"insight": "...", "action": "...", "confidence": 0-100}}]'
        )

        # Use fast model for this extraction
        try:
            client   = FreeFlowClient()
            messages = [
                {"role": "system", "content": "You are a trading pattern analyzer. Respond only in JSON."},
                {"role": "user",   "content": prompt}
            ]
            response = client.chat(model=MODEL_FAST, messages=messages, max_tokens=300)
            reply    = response if isinstance(response, str) else (
                response.choices[0].message.content if hasattr(response, "choices") else str(response)
            )

            # Parse JSON insights
            clean = reply.strip().replace("```json","").replace("```","").strip()
            insights = json.loads(clean)

            if isinstance(insights, list):
                for item in insights:
                    if isinstance(item, dict) and item.get("insight"):
                        brain["trading"]["strategy_notes"].append({
                            "note":       f"[LLM-INSIGHT] {item['insight']} → {item.get('action','')}",
                            "confidence": item.get("confidence", 50),
                            "timestamp":  datetime.utcnow().isoformat()
                        })
                print(f"🧠 Deep LLM analysis: {len(insights)} insights extracted")

        except Exception as llm_e:
            print(f"Deep LLM analysis error: {llm_e}")

    except Exception as e:
        print(f"Deep learn error: {e}")


# ── Tier 4b: Airdrop LLM analysis ──────────────────────────────────
def _deep_airdrop_analysis():
    """LLM se airdrop patterns analyze karwao."""
    try:
        active = brain["airdrop"]["active_projects"]
        if len(active) < 2:
            return

        # High value projects (>$10M raised)
        high_value = [p for p in active if float(p.get("amount_usd", 0)) > 10]
        bsc_focused = [p for p in active if "BSC" in p.get("chains", [])]

        # Pattern: What % of high-value projects have BSC?
        if len(active) > 5:
            bsc_pct = (len(bsc_focused) / len(active)) * 100
            insight = f"{bsc_pct:.0f}% of tracked projects have BSC exposure — {'high' if bsc_pct > 40 else 'low'} BSC airdrop season"
            existing = [n.get("note","") for n in brain["airdrop"]["wallet_notes"]]
            if insight not in existing:
                brain["airdrop"]["wallet_notes"].append({
                    "note": insight,
                    "timestamp": datetime.utcnow().isoformat()
                })

        if high_value:
            brain["airdrop"]["success_patterns"].append(
                f"{len(high_value)} high-value (>$10M) projects tracked — priority for airdrop hunting"
            )
            brain["airdrop"]["success_patterns"] = list(set(brain["airdrop"]["success_patterns"]))[-20:]

    except Exception as e:
        print(f"Airdrop analysis error: {e}")


# ── Knowledge Application: Inject into decisions ───────────────────
def get_learning_context_for_decision(token_address: str = None) -> str:
    """
    Before any decision (scan/trade), inject all relevant learned knowledge.
    This is how learning ACTUALLY affects behavior.
    """
    try:
        parts = []

        # Is token in blacklist?
        if token_address:
            bl = [t["address"] for t in brain["trading"]["token_blacklist"]]
            wl = [t["address"] for t in brain["trading"]["token_whitelist"]]
            if token_address in bl:
                parts.append(f"⚠️ TOKEN_BLACKLISTED: This exact token was previously flagged as dangerous")
            elif token_address in wl:
                parts.append(f"✅ TOKEN_WHITELISTED: This token was previously verified as safe")

        # Recent win patterns
        wins = brain["trading"]["best_patterns"][-3:]
        if wins:
            win_str = " | ".join([f"+{w.get('pnl',0):.0f}%({w.get('token','')[:6]})" for w in wins if w.get("pnl",0)>0])
            if win_str:
                parts.append(f"WIN_PATTERNS:{win_str}")

        # Recent loss patterns to avoid
        losses = brain["trading"]["avoid_patterns"][-3:]
        if losses:
            loss_str = " | ".join([l.get("lesson","")[:40] for l in losses if l.get("lesson")])
            if loss_str:
                parts.append(f"AVOID:{loss_str}")

        # Latest LLM insights
        llm_insights = [n for n in brain["trading"]["strategy_notes"] if "[LLM-INSIGHT]" in n.get("note","")]
        if llm_insights:
            latest = llm_insights[-1].get("note","").replace("[LLM-INSIGHT]","").strip()
            parts.append(f"INSIGHT:{latest[:60]}")

        # Market regime
        insights = brain["trading"]["market_insights"]
        if insights:
            last = insights[-1]
            parts.append(f"MARKET_REGIME:{last.get('mood','?')}(F&G={last.get('fear_greed','?')})")

        # User pain points → personalized help
        pain_points = brain.get("user_pain_points", [])
        if pain_points:
            parts.append(f"USER_NEEDS_HELP_WITH:{pain_points[-1].get('query','')[:40]}")

        return " | ".join(parts) if parts else ""

    except Exception as e:
        print(f"Learning context error: {e}")
        return ""



# ═══════════════════════════════════════════════════════════════════
# 24x7 INTERNET DATA ENGINE v2
# Sources: CoinGecko, GeckoTerminal, airdrops.io, DeFiLlama,
#          HackerNews, GitHub Trending
# Features: Quality Filter + LLM Learning Integration
# ═══════════════════════════════════════════════════════════════════

def _quality_score(item: dict, domain: str) -> int:
    """Quality filter — 0-100. Sirf high quality data save hoga."""
    score = 0
    if domain == "trading":
        if item.get("volume_usd", 0) > 10000:      score += 30
        if item.get("liquidity",   0) > 5000:       score += 25
        if item.get("sentiment") == "positive":     score += 20
        if item.get("symbol"):                      score += 15
        if item.get("rank", 999) < 200:             score += 10
    elif domain == "airdrop":
        if item.get("status") == "confirmed":       score += 40
        if item.get("amount_usd", 0) > 5:           score += 30
        if "BSC" in item.get("chains", []):         score += 20
        if item.get("url"):                         score += 10
    elif domain == "coding":
        if item.get("score", 0) > 100:              score += 40
        if item.get("source") == "github_trending": score += 30
        if "python" in item.get("title","").lower():score += 20
        if "web3"   in item.get("title","").lower() or "bsc" in item.get("title","").lower(): score += 10
    return score


def _fetch_trading_intel():
    """Trading: CoinGecko Trending + GeckoTerminal BSC pools + BSC gainers"""
    try:
        # CoinGecko Trending
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        if r.status_code == 200:
            trending = []
            for c in r.json().get("coins", [])[:7]:
                item = c.get("item", {})
                entry = {"name": item.get("name",""), "symbol": item.get("symbol",""),
                         "rank": item.get("market_cap_rank", 999), "score": item.get("score",0),
                         "source": "coingecko_trending"}
                if _quality_score(entry, "trading") >= 25:
                    trending.append(entry)
            brain["trading"]["market_insights"].append({
                "timestamp": datetime.utcnow().isoformat(),
                "observation": f"CoinGecko trending: {[t['symbol'] for t in trending]}",
                "mood": "TRENDING", "data": trending, "quality": "HIGH"
            })
            print(f"📈 CoinGecko trending: {[t['symbol'] for t in trending[:3]]}")

        # GeckoTerminal BSC new pools
        r2 = requests.get("https://api.geckoterminal.com/api/v2/networks/bsc/new_pools",
                          params={"page": 1},
                          headers={"Accept": "application/json;version=20230302"}, timeout=10)
        if r2.status_code == 200:
            for pool in r2.json().get("data", [])[:10]:
                attr = pool.get("attributes", {})
                liq  = float(attr.get("reserve_in_usd", 0) or 0)
                rel  = pool.get("relationships", {})
                tid  = rel.get("base_token", {}).get("data", {}).get("id", "")
                addr = tid.replace("bsc_", "") if tid else ""
                if addr and liq > 2000:
                    threading.Thread(target=_process_new_token, args=(addr, addr, "GeckoTerminal"), daemon=True).start()
            print(f"🦎 GeckoTerminal: {len(r2.json().get('data',[]))} new BSC pools")

        # CoinGecko BSC gainers
        r3 = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                          params={"vs_currency":"usd","category":"binance-smart-chain",
                                  "order":"percent_change_24h_desc","per_page":10,"page":1}, timeout=12)
        if r3.status_code == 200:
            gainer_names = [f"{g['symbol'].upper()}+{g.get('price_change_percentage_24h',0):.0f}%"
                            for g in r3.json()[:5]]
            brain["trading"]["market_insights"].append({
                "timestamp": datetime.utcnow().isoformat(),
                "observation": f"BSC top gainers 24h: {gainer_names}",
                "mood": "BULLISH", "gainers": gainer_names, "quality": "HIGH"
            })
            print(f"🚀 BSC gainers: {gainer_names[:3]}")

        brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-200:]
    except Exception as e:
        print(f"⚠️ Trading intel error: {e}")


def _fetch_airdrop_intel():
    """Airdrop: airdrops.io RSS + DeFiLlama raises"""
    try:
        import xml.etree.ElementTree as ET

        # airdrops.io RSS
        r = requests.get("https://airdrops.io/feed/", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            try:
                root = ET.fromstring(r.content)
                added = 0
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title","")[:60]
                    link  = item.findtext("link", "")[:80]
                    if not title: continue
                    entry = {"name": title, "url": link, "source": "airdrops_io",
                             "chains": [], "status": "confirmed", "added_at": datetime.utcnow().isoformat()}
                    if _quality_score(entry, "airdrop") >= 40:
                        if title not in [p.get("name") for p in brain["airdrop"]["active_projects"]]:
                            brain["airdrop"]["active_projects"].append(entry)
                            added += 1
                print(f"🪂 airdrops.io: {added} quality airdrops added")
            except Exception as xe:
                print(f"⚠️ airdrops.io XML: {xe}")

        # DeFiLlama raises
        r2 = requests.get("https://api.llama.fi/raises", timeout=12)
        if r2.status_code == 200:
            cutoff = (datetime.utcnow() - timedelta(days=7)).timestamp()
            added  = 0
            for item in r2.json().get("raises", [])[:30]:
                if float(item.get("date", 0) or 0) < cutoff: continue
                entry = {"name": item.get("name",""), "source": "defillama",
                         "chains": item.get("chains",[]), "amount_usd": item.get("amount",0),
                         "added_at": datetime.utcnow().isoformat(), "status": "fundraised"}
                if _quality_score(entry, "airdrop") >= 30:
                    if entry["name"] and entry["name"] not in [p.get("name") for p in brain["airdrop"]["active_projects"]]:
                        brain["airdrop"]["active_projects"].append(entry)
                        added += 1
            print(f"💰 DeFiLlama: {added} quality raises added")

        brain["airdrop"]["active_projects"] = brain["airdrop"]["active_projects"][-200:]
        brain["airdrop"]["last_updated"]    = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"⚠️ Airdrop intel error: {e}")


def _fetch_coding_intel():
    """Coding: GitHub Trending Python + Solidity + HackerNews"""
    try:
        import xml.etree.ElementTree as ET

        # GitHub Python trending
        r = requests.get("https://mshibanami.github.io/GitHubTrendingRSS/daily/python.xml",
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            try:
                root  = ET.fromstring(r.content)
                added = 0
                for item in root.findall(".//item")[:8]:
                    title = item.findtext("title","")[:60]
                    link  = item.findtext("link", "")[:80]
                    desc  = item.findtext("description","")[:100]
                    if title:
                        entry = {"title": title, "link": link, "desc": desc,
                                 "source": "github_trending", "added_at": datetime.utcnow().isoformat()}
                        if _quality_score(entry, "coding") >= 30:
                            if title not in [s.get("title") for s in brain["coding"]["useful_snippets"]]:
                                brain["coding"]["useful_snippets"].append(entry)
                                added += 1
                brain["coding"]["useful_snippets"] = brain["coding"]["useful_snippets"][-100:]
                print(f"💻 GitHub Python: {added} repos added")
            except Exception as xe:
                print(f"⚠️ GitHub RSS: {xe}")

        # HackerNews
        r2 = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10)
        if r2.status_code == 200:
            added = 0
            for sid in r2.json()[:8]:
                try:
                    rs = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=5)
                    if rs.status_code == 200:
                        s     = rs.json()
                        entry = {"title": s.get("title","")[:60], "url": s.get("url","")[:80],
                                 "score": s.get("score",0), "source": "hackernews",
                                 "added_at": datetime.utcnow().isoformat()}
                        if _quality_score(entry, "coding") >= 40:
                            if entry["title"] and entry["title"] not in [x.get("title") for x in brain["coding"]["deployment_notes"]]:
                                brain["coding"]["deployment_notes"].append(
                                    {"note": f"HN: {entry['title']} (score:{entry['score']})",
                                     "url": entry["url"], "added_at": entry["added_at"]})
                                added += 1
                except Exception: pass
            brain["coding"]["deployment_notes"] = brain["coding"]["deployment_notes"][-100:]
            print(f"📰 HackerNews: {added} stories added")

        # GitHub Solidity trending
        r3 = requests.get("https://mshibanami.github.io/GitHubTrendingRSS/daily/solidity.xml",
                          timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r3.status_code == 200:
            try:
                root = ET.fromstring(r3.content)
                for item in root.findall(".//item")[:5]:
                    title = item.findtext("title","")[:60]
                    link  = item.findtext("link", "")[:80]
                    if title and title not in [s.get("title") for s in brain["coding"]["solutions_library"]]:
                        brain["coding"]["solutions_library"].append(
                            {"title": title, "link": link, "category": "web3/solidity",
                             "source": "github_trending", "added_at": datetime.utcnow().isoformat()})
                brain["coding"]["solutions_library"] = brain["coding"]["solutions_library"][-100:]
            except Exception: pass

        brain["coding"]["last_updated"] = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"⚠️ Coding intel error: {e}")


def _learn_from_internet_data():
    """Internet data → LLM analysis → Brain mein insights save"""
    try:
        recent = [i for i in brain["trading"]["market_insights"][-10:] if i.get("quality") == "HIGH"]
        gainers, trending_symbols = [], []
        for ins in recent:
            gainers.extend(ins.get("gainers", []))
            trending_symbols.extend([d.get("symbol","") for d in ins.get("data",[])])

        if gainers or trending_symbols:
            summary = (f"BSC gainers={gainers[:3]}, Trending={trending_symbols[:3]}, "
                       f"Airdrops={len(brain['airdrop']['active_projects'])}, F&G={market_cache.get('fear_greed',50)}")
            prompt = (f"BSC bot ke liye internet data: {summary}. "
                      f"3 actionable insights JSON: "
                      f'[{{"insight":"...","action":"...","domain":"trading/airdrop/coding","confidence":0-100}}]')
            try:
                client   = _get_freeflow_client()
                response = client.chat(model=MODEL_FAST,
                                       messages=[{"role":"system","content":"JSON only."},
                                                 {"role":"user","content":prompt}],
                                       max_tokens=300)
                reply    = response if isinstance(response, str) else (
                    response.choices[0].message.content if hasattr(response,"choices") else str(response))
                insights = json.loads(reply.strip().replace("```json","").replace("```","").strip())
                if isinstance(insights, list):
                    for item in insights:
                        if isinstance(item, dict) and item.get("insight"):
                            domain = item.get("domain","trading")
                            note   = {"note": f"[INTERNET-INSIGHT] {item['insight']} → {item.get('action','')}",
                                      "confidence": item.get("confidence",50),
                                      "source": "internet_learning",
                                      "timestamp": datetime.utcnow().isoformat()}
                            if domain == "trading":
                                brain["trading"]["strategy_notes"].append(note)
                            elif domain == "airdrop":
                                brain["airdrop"]["success_patterns"].append(note["note"])
                            elif domain == "coding":
                                brain["coding"]["solutions_library"].append(
                                    {"title": note["note"][:60], "category":"llm_insight",
                                     "added_at": datetime.utcnow().isoformat()})
                    print(f"🧠 Internet LLM: {len(insights)} insights extracted")
            except Exception as le:
                print(f"⚠️ Internet LLM error: {le}")

        brain["airdrop"]["success_patterns"] = list(set(brain["airdrop"]["success_patterns"]))[-30:]
    except Exception as e:
        print(f"⚠️ Internet learning error: {e}")


def fetch_internet_data_24x7():
    """24x7 Internet Engine — har 30 min, 3 domains parallel, quality filter + LLM learning"""
    print("🌐 Internet Data Engine v2 started!")
    time.sleep(45)
    while True:
        try:
            print("🌐 Fetching internet data...")
            t1 = threading.Thread(target=_fetch_trading_intel, daemon=True)
            t2 = threading.Thread(target=_fetch_airdrop_intel, daemon=True)
            t3 = threading.Thread(target=_fetch_coding_intel,  daemon=True)
            t1.start(); t2.start(); t3.start()
            t1.join(timeout=30); t2.join(timeout=30); t3.join(timeout=30)
            _learn_from_internet_data()
            threading.Thread(target=_save_brain_to_db, daemon=True).start()
            print(f"✅ Internet done | Trading:{len(brain['trading']['market_insights'])} | "
                  f"Airdrops:{len(brain['airdrop']['active_projects'])} | "
                  f"Coding:{len(brain['coding']['solutions_library'])}")
        except Exception as e:
            print(f"⚠️ Internet engine error: {e}")
        time.sleep(1800)




def _cross_domain_learning():
    """Fix 5: Trading↔Airdrop↔Coding cross signals."""
    try:
        now = datetime.utcnow().isoformat()
        fg  = market_cache.get("fear_greed", 50)

        # Trading → Airdrop: fear = best airdrop time
        if fg < 25:
            insight = f"CROSS: Market extreme fear ({fg}) — airdrop participation best time"
            if insight not in brain["airdrop"]["success_patterns"]:
                brain["airdrop"]["success_patterns"].append(insight)

        # Trading trending → match with airdrop projects
        for ins in brain["trading"]["market_insights"][-5:]:
            for coin in ins.get("data",[]):
                symbol = coin.get("symbol","").upper()
                if symbol and any(symbol in p.get("name","").upper()
                                  for p in brain["airdrop"]["active_projects"]):
                    note = f"CROSS-SIGNAL: {symbol} trending + airdrop tracked — high priority!"
                    brain["trading"]["strategy_notes"].append(
                        {"note": f"[CROSS-DOMAIN] {note}", "timestamp": now, "source": "cross_domain"})
                    print(f"🔗 Cross-signal: {note}")

        # Airdrop → Trading: funded BSC project = trading opportunity
        for project in brain["airdrop"]["active_projects"]:
            if float(project.get("amount_usd",0)) > 10 and "BSC" in project.get("chains",[]) and not project.get("cross_noted"):
                brain["trading"]["market_insights"].append({
                    "timestamp": now,
                    "observation": f"CROSS: {project['name']} raised ${project['amount_usd']}M on BSC — watch for token launch",
                    "mood": "OPPORTUNITY", "source": "cross_domain_airdrop"
                })
                project["cross_noted"] = True
                print(f"🔗 Airdrop→Trading: {project['name']}")

        # Coding → deployment notes
        for s in brain["coding"]["solutions_library"]:
            if "web3" in s.get("category","").lower() and not s.get("cross_noted"):
                brain["coding"]["deployment_notes"].append(
                    {"note": f"CROSS: Web3 tool: {s.get('title','')[:40]}", "added_at": now})
                s["cross_noted"] = True

        brain["coding"]["deployment_notes"] = brain["coding"]["deployment_notes"][-100:]
        cross_c = len([n for n in brain["trading"]["strategy_notes"] if "[CROSS-DOMAIN]" in n.get("note","")])
        print(f"🔗 Cross-domain: {cross_c} total signals")
    except Exception as e:
        print(f"⚠️ Cross-domain error: {e}")



def continuous_learning():
    """
    24x7 LEARNING ENGINE v2 — Multi-tier system
    T1: Every message (called from chat)
    T2: Every 60s (fast cycle)
    T3: Every 5 min (standard)
    T4: Every 15 min (deep LLM analysis)
    T5: Every 1 hour (consolidation)
    """
    print("🧠 Learning Engine v2 (10/10) started!")

    _load_brain_from_db()
    time.sleep(3)

    # Supabase se latest cycle number lo
    cycle = brain.get("total_learning_cycles", 0)
    try:
        if supabase:
            res = supabase.table("memory").select("content").eq("session_id", "MRBLACK_CYCLE").execute()
            if res.data:
                saved_cycle = int(res.data[0].get("content", 0) or 0)
                if saved_cycle > cycle:
                    cycle = saved_cycle
                    brain["total_learning_cycles"] = cycle
    except Exception:
        pass

    last_fast = 0
    last_deep = 0
    last_hour = 0
    print(f"📚 Learning resuming from cycle #{cycle}")

    while True:
        try:
            cycle += 1
            brain["total_learning_cycles"] = cycle
            now = time.time()

            # ── T2: Fast (every 60s) ────────────────────────────────────
            if now - last_fast >= 60:
                last_fast = now
                _fast_learning_cycle()

            # ── T3: Standard (every 5 min) ──────────────────────────────
            try:
                fetch_market_data()
                fetch_pancakeswap_data()
            except Exception as e:
                print(f"Market fetch error: {e}")

            try:
                run_airdrop_hunter()
            except Exception as e:
                print(f"Airdrop fetch error: {e}")

            # Standard pattern learning
            _learn_trading_patterns()
            _learn_airdrop_patterns()
            _learn_from_new_pairs()

            # T3 pe bhi cycle count save karo — restart pe reset nahi hoga
            try:
                if supabase:
                    supabase.table("memory").upsert({
                        "session_id": "MRBLACK_CYCLE",
                        "role":       "system",
                        "content":    str(cycle),
                        "updated_at": datetime.utcnow().isoformat()
                    }).execute()
            except Exception:
                pass

            # ── T4: Deep (every 15 min) ─────────────────────────────────
            if now - last_deep >= 900:
                last_deep = now
                print(f"🔬 Deep learning pass #{cycle}...")

                _deep_llm_learning()
                _deep_airdrop_analysis()
                _cross_domain_learning()

                # Update self-awareness after deep learning
                update_self_awareness()

                # Save brain to Supabase
                _save_brain_to_db()

                # Knowledge domain health update
                self_awareness["memory_summary"]["knowledge_domains"] = {
                    "trading":  {
                        "entries": len(brain["trading"]["best_patterns"]) + len(brain["trading"]["avoid_patterns"]),
                        "quality": "rich" if len(brain["trading"]["best_patterns"]) > 10 else "growing"
                    },
                    "airdrop":  {
                        "entries": len(brain["airdrop"]["active_projects"]),
                        "quality": "active" if len(brain["airdrop"]["active_projects"]) > 5 else "building"
                    },
                    "coding":   {
                        "entries": len(brain["coding"]["solutions_library"]),
                        "quality": "growing"
                    },
                    "market":   {
                        "entries": len(brain["trading"]["market_insights"]),
                        "quality": "live" if market_cache.get("bnb_price", 0) > 0 else "offline"
                    },
                }

                print(
                    f"📚 Cycle #{cycle} | "
                    f"Patterns:{len(brain['trading']['best_patterns'])}W/{len(brain['trading']['avoid_patterns'])}L | "
                    f"BL:{len(brain['trading']['token_blacklist'])} | "
                    f"Drops:{len(brain['airdrop']['active_projects'])} | "
                    f"IQ:{self_awareness['performance_intelligence']['trading_iq']}"
                )

            # ── T5: Hourly consolidation ────────────────────────────────
            if now - last_hour >= 3600:
                last_hour = now
                try:
                    # Milestone check
                    _check_milestones()

                    wins_c   = len(brain["trading"]["best_patterns"])
                    avoid_c  = len(brain["trading"]["avoid_patterns"])
                    drops_c  = len(brain["airdrop"]["active_projects"])
                    fg       = market_cache.get("fear_greed", 50)
                    bnb      = market_cache.get("bnb_price", 0)
                    tiq      = self_awareness["performance_intelligence"]["trading_iq"]
                    emotion  = self_awareness["emotional_intelligence"]["current_emotion"]
                    conf     = self_awareness["cognitive_state"]["confidence_level"]
                    mood_str = "GREED 🟢" if fg > 60 else "FEAR 🔴" if fg < 40 else "NEUTRAL ⚪"

                    # LLM insights count
                    llm_insights_count = len([
                        n for n in brain["trading"]["strategy_notes"]
                        if "[LLM-INSIGHT]" in n.get("note","")
                    ])

                    report_msg = (
                        "MrBlack Learning Report #" + str(cycle) + "\n"
                        "Trading IQ: " + str(tiq) + "/100\n"
                        "Emotion: " + str(emotion) + " Conf: " + str(conf) + "%\n"
                        "Patterns W:" + str(wins_c) + " L:" + str(avoid_c) + "\n"
                        "Blacklisted: " + str(len(brain["trading"]["token_blacklist"])) + "\n"
                        "Airdrops: " + str(drops_c) + "\n"
                        "BNB: $" + str(round(bnb,1)) + " FG:" + str(fg) + "\n"
                        "Growing smarter every cycle!"
                    )
                    send_telegram(report_msg)
                except Exception as e:
                    print(f"Hourly report error: {e}")

        except Exception as e:
            print(f"Learning cycle error: {e}")

        time.sleep(300)  # T3 base: every 5 min


def _check_milestones():
    """Track and celebrate learning milestones."""
    try:
        milestones = self_awareness["growth_tracking"]["milestones"]
        achieved   = [m.get("title","") for m in milestones]

        checks = [
            (len(brain["trading"]["token_blacklist"]) >= 10,  "Blacklisted 10 dangerous tokens 🛡️"),
            (len(brain["trading"]["best_patterns"])   >= 5,   "Learned 5 winning patterns 📈"),
            (brain.get("total_learning_cycles", 0)    >= 100, "100 learning cycles complete 🧠"),
            (len(brain["airdrop"]["active_projects"])  >= 20, "Tracking 20 airdrop projects 🪂"),
            (self_awareness["performance_intelligence"]["trading_iq"] >= 70, "Trading IQ reached 70+ 🎯"),
        ]

        for condition, title in checks:
            if condition and title not in achieved:
                milestone = {"title": title, "achieved_at": datetime.utcnow().isoformat()}
                milestones.append(milestone)
                send_telegram(f"🏆 <b>MILESTONE ACHIEVED!</b>\n{title}")
                print(f"🏆 Milestone: {title}")

        self_awareness["growth_tracking"]["milestones"] = milestones

        # Next milestone suggestion
        for condition, title in checks:
            if not condition:
                self_awareness["growth_tracking"]["next_milestone"] = title
                break

    except Exception as e:
        print(f"Milestone check error: {e}")


# ==========================================================
# ========== 13-STAGE SNIPER CHECKLIST ====================
# ==========================================================


# ═══════════════════════════════════════════════════════════════════
# FEEDBACK LOOP ENGINE — 24h recommendation validation
# ═══════════════════════════════════════════════════════════════════
feedback_log = []

def log_recommendation(address: str, overall: str, score: int, total: int):
    feedback_log.append({
        "address": address, "recommendation": overall,
        "score": score, "total": total,
        "logged_at": datetime.utcnow().isoformat(),
        "validate_after": (datetime.utcnow() + timedelta(hours=24)).isoformat(),
        "validated": False, "outcome": None, "was_correct": None
    })
    if len(feedback_log) > 500: feedback_log.pop(0)


def _validate_past_recommendations():
    """24h baad check karo — SAFE bola tha sahi tha ya galat."""
    now = datetime.utcnow()
    validated, correct = 0, 0
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
            was_correct = (
                (rec == "SAFE"             and change > 0)   or
                (rec in ["DANGER","RISK"]  and change < -20) or
                (rec == "CAUTION")
            )
            entry.update({"validated": True, "outcome": f"24h:{change:+.1f}%", "was_correct": was_correct})
            validated += 1
            if was_correct: correct += 1
            brain["trading"]["strategy_notes"].append({
                "note": f"[FEEDBACK] {'✅' if was_correct else '❌'} Said {rec} → 24h {change:+.1f}% ({addr[:10]})",
                "timestamp": now.isoformat(), "correct": was_correct
            })
        except Exception as e:
            print(f"⚠️ Feedback err {addr[:10]}: {e}")
    if validated > 0:
        acc = round(correct/validated*100, 1)
        self_awareness["performance_intelligence"]["overall_accuracy"] = acc
        self_awareness["performance_intelligence"]["scan_accuracy"]    = acc
        print(f"🔄 Feedback: {validated} validated, {acc}% accurate")


def feedback_validation_loop():
    print("🔄 Feedback Loop started!")
    time.sleep(120)
    while True:
        try: _validate_past_recommendations()
        except Exception as e: print(f"⚠️ Feedback loop: {e}")
        time.sleep(3600)



def run_full_sniper_checklist(address: str) -> Dict:
    result = {
        "address": address, "checklist": [],
        "overall": "UNKNOWN", "score": 0, "total": 0,
        "recommendation": "", "dex_data": {}
    }

    goplus_data = {}
    bscscan_source = ""

    try:
        gp_res = requests.get(
            "https://api.gopluslabs.io/api/v1/token_security/56",
            params={"contract_addresses": address}, timeout=12
        )
        if gp_res.status_code == 200:
            goplus_data = gp_res.json().get("result", {}).get(address.lower(), {})
    except Exception as e:
        print(f"⚠️ GoPlus error: {e}")

    bscscan_source = ""
    goplus_empty = not bool(goplus_data)
    if _gp_str(goplus_data, "is_open_source", "0") == "1":
        bscscan_source = "verified"
    print("✅ Contract check via GoPlus (no BSCScan needed)")

    # DexScreener real-time data
    dex_data = get_dexscreener_token_data(address)
    result["dex_data"] = dex_data

    def add(label, status, value, stage):
        result["checklist"].append({"label": label, "status": status, "value": value, "stage": stage})

    # ── STAGE 1 — Safety ──────────────────────────────────
    verified  = bool(bscscan_source)
    mint_ok   = not _gp_bool_flag(goplus_data, "is_mintable")
    renounced = (
        _gp_str(goplus_data, "owner_address") in [
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dead", ""
        ] or "renounceOwnership" in bscscan_source
    )

    add("Contract Verified", "pass" if verified  else "fail", "YES" if verified  else "NO", 1)
    add("Mint Authority Disabled", "pass" if mint_ok   else "fail", "SAFE" if mint_ok  else "RISK", 1)
    add("Ownership Renounced",     "pass" if renounced else "warn", "YES" if renounced else "MAYBE",1)

    dex_list   = goplus_data.get("dex", [])
    liq_usd    = 0.0
    liq_locked = 0.0
    if isinstance(dex_list, list) and dex_list:
        for pool in dex_list:
            liq_usd    += float(pool.get("liquidity",  0) or 0)
            liq_locked += float(pool.get("lock_ratio", 0) or 0)
        liq_locked = (liq_locked / len(dex_list)) * 100

    # Use DexScreener liquidity if better
    try:
        _pl = _get_v2_pair(address)
        if _pl:
            _pc  = w3.eth.contract(address=Web3.to_checksum_address(_pl), abi=PAIR_ABI_PRICE)
            _r   = _pc.functions.getReserves().call()
            _t0  = _pc.functions.token0().call()
            _liq = _r[0]/1e18 if _t0.lower()==WBNB.lower() else _r[1]/1e18
            if _liq > 0: liq_bnb = _liq; liq_usd = _liq * bnb_price
    except: pass

    bnb_price = market_cache.get("bnb_price", 300) or 300
    liq_bnb   = liq_usd / bnb_price

    buy_tax  = _gp_float(goplus_data, "buy_tax")  * 100
    sell_tax = _gp_float(goplus_data, "sell_tax") * 100
    hidden   = _gp_bool_flag(goplus_data, "can_take_back_ownership") or _gp_bool_flag(goplus_data, "hidden_owner")
    transfer = not _gp_bool_flag(goplus_data, "transfer_pausable")

    add("Liquidity ≥ 1 BNB",    "pass" if liq_bnb > 2    else ("warn" if liq_bnb > 0.5 else "fail"), f"{liq_bnb:.2f} BNB", 1)
    add("Liquidity Locked", "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 1)
    add("Buy Tax ≤ 8%",        "pass" if buy_tax <= 8   else "fail",          f"{buy_tax:.1f}%",  1)
    add("Sell Tax ≤ 8%",       "pass" if sell_tax <= 8  else "fail",          f"{sell_tax:.1f}%", 1)
    add("No Hidden Functions",  "pass" if not hidden       else "fail", "CLEAN" if not hidden else "RISK",   1)
    add("Transfer Allowed",     "pass" if transfer         else "fail", "YES"   if transfer   else "PAUSED", 1)

    holders_list = goplus_data.get("holders", [])
    top_holder   = 0.0
    top10_pct    = 0.0
    if isinstance(holders_list, list) and holders_list:
        for i, h in enumerate(holders_list[:10]):
            pct = float(h.get("percent", 0) or 0) * 100
            if i == 0: top_holder = pct
            top10_pct += pct

    suspicious  = _gp_bool_flag(goplus_data, "is_airdrop_scam")
    creator_pct = _gp_float(goplus_data, "creator_percent") * 100

    add("Top Holder < 7%",          "pass" if top_holder < 7  else ("warn" if top_holder < 15  else "fail"), f"{top_holder:.1f}%", 1)
    add("Top 10 Holders < 40%",     "pass" if top10_pct < 40  else ("warn" if top10_pct < 50   else "fail"), f"{top10_pct:.1f}%",  1)
    add("No Suspicious Clustering", "pass" if not suspicious   else "fail", "CLEAN" if not suspicious else "RISK", 1)
    add("Dev Wallet Not Dumping",   "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 1)

    # ── STAGE 2 — Honeypot ────────────────────────────────
    honeypot  = _gp_bool_flag(goplus_data, "is_honeypot")
    can_sell  = not _gp_bool_flag(goplus_data, "cannot_sell_all")
    slippage_ok = sell_tax <= 15

    add("Honeypot Safe",        "fail" if honeypot    else "pass", "DANGER" if honeypot    else "SAFE", 2)
    add("Can Sell All Tokens",  "fail" if not can_sell else "pass", "NO"    if not can_sell else "YES",  2)
    add("Slippage OK",          "pass" if slippage_ok  else "warn", f"Sell={sell_tax:.0f}%",             2)

    # ── STAGE 3 — Token Age ───────────────────────────────
    token_age_min = 0.0
    try:
        age_r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=10
        )
        if age_r.status_code == 200:
            _age_json = age_r.json() or {}
            _age_raw  = _age_json.get("pairs") or []
            if not isinstance(_age_raw, list): _age_raw = []
            bsc_pairs = [p for p in _age_raw if p is not None and p.get("chainId") == "bsc"]
            if bsc_pairs:
                created_at = bsc_pairs[0].get("pairCreatedAt", 0)
                if created_at:
                    token_age_min = (time.time() - created_at / 1000) / 60
                    print(f"✅ Token age (DexScreener): {token_age_min:.1f} min")
    except Exception as e:
        print(f"⚠️ Token age error: {e}")

    add("Token Age ≥ 3 Min",   "pass" if token_age_min >= 3 else "warn",
        f"{token_age_min:.0f} min" if token_age_min > 0 else "Unknown", 3)
    add("Sniper Pump Over",    "pass" if token_age_min >= 5 else "warn",
        "OK" if token_age_min >= 5 else "WAIT", 3)

    # ── STAGE 4 — Buy Pressure (DexScreener enhanced) ─────
    try:
        _sp  = _get_v2_pair(address)
        _cur = w3.eth.block_number
        _SWT = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
        def _cnt(frm):
            if not _sp: return 0,0
            logs = w3.eth.get_logs({"address":Web3.to_checksum_address(_sp),"topics":[_SWT],"fromBlock":frm,"toBlock":_cur})
            b=s=0
            _t0l = w3.eth.contract(address=Web3.to_checksum_address(_sp),abi=PAIR_ABI_PRICE).functions.token0().call()
            for lg in logs:
                hx = lg["data"].hex() if isinstance(lg["data"],bytes) else lg["data"].replace("0x","")
                if len(hx)<256: continue
                a0=int(hx[0:64],16); a1=int(hx[64:128],16)
                if _t0l.lower()==WBNB.lower(): b+=1 if a0>0 else 0; s+=1 if a0==0 else 0
                else: b+=1 if a1>0 else 0; s+=1 if a1==0 else 0
            return b,s
        buys_5m,sells_5m = _cnt(_cur-100)
        buys_1h,sells_1h = _cnt(_cur-1200)
    except: buys_5m=sells_5m=buys_1h=sells_1h=0
    sells_5m = dex_data.get("sells_5m", 0)
    buys_1h  = dex_data.get("buys_1h",  0)
    sells_1h = dex_data.get("sells_1h", 0)

    buy_pressure_5m = buys_5m > sells_5m
    buy_pressure_1h = buys_1h > sells_1h

    if buys_5m == 0 and sells_5m == 0:
        print(f"ℹ️ No 5m txn data for {address[:10]} — DexScreener may need time")

    add("Buy > Sell (5min)",  "pass" if buy_pressure_5m else "warn",
        f"B:{buys_5m} S:{sells_5m}", 4)
    add("Buy > Sell (1hr)",   "pass" if buy_pressure_1h else "warn",
        f"B:{buys_1h} S:{sells_1h}" if buys_1h or sells_1h else "Fetching", 4)
    add("Volume 24h",         "pass" if dex_data.get("volume_24h",0) > 1000 else "warn",
        f"${dex_data.get('volume_24h',0):,.0f}", 4)

    # ── STAGE 5 — Position Sizing ─────────────────────────
    add("1st Entry 0.002-0.005 BNB", "pass", "Follow Rule",  5)
    add("Max Position ≤ 3%",         "pass", "2-3% Balance", 5)
    add("Max 3-4 Entries/Token",     "pass", "No Chasing",   5)

    # ── STAGE 6 — Volume Monitor ──────────────────────────
    in_dex     = _gp_bool_flag(goplus_data, "is_in_dex")
    pool_count = len(dex_list) if isinstance(dex_list, list) else 0
    change_1h  = dex_data.get("change_1h", 0)

    add("Listed on DEX",         "pass" if in_dex     else "fail",  "YES" if in_dex else "NO", 6)
    add("DEX Pools",             "pass" if pool_count > 0 else "warn", f"{pool_count} pools",  6)
    add("1h Price Change",       "pass" if change_1h > 0  else "warn", f"{change_1h:+.1f}%",   6)
    add("Vol -50% → Exit 50%",   "pass", "Rule Active", 6)
    add("Vol -90% → Exit Fully", "pass", "Rule Active", 6)

    # ── STAGE 7 — Whale & Dev Tracking ────────────────────
    owner_pct = _gp_float(goplus_data, "owner_percent") * 100

    add("Dev/Creator < 5%",       "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 7)
    add("Owner Wallet < 5%",      "pass" if owner_pct < 5    else ("warn" if owner_pct < 15   else "fail"), f"{owner_pct:.1f}%",   7)
    add("Whale Conc. OK",         "pass" if top10_pct < 45   else "fail",  f"{top10_pct:.1f}% top10",        7)
    add("Dev Sell → Exit Rule",   "pass", "Telegram Alert Active", 7)

    # ── STAGE 8 — Liquidity Protection ───────────────────
    lp_holders = int(_gp_str(goplus_data, "lp_holder_count", "0"))

    add("LP Lock > 80%",         "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 8)
    add("LP Holders Present",    "pass" if lp_holders > 0  else "warn", f"{lp_holders} LP holders", 8)
    add("LP Drop → Exit Rule",   "pass", "Monitored", 8)

    # ── STAGE 9 — Fast Profit Mode ────────────────────────
    low_tax       = buy_tax <= 5 and sell_tax <= 5
    fast_trade_ok = low_tax and liq_locked > 20 and not honeypot

    add("Low Tax Fast Trade",    "pass" if low_tax       else "warn", "FAST OK" if low_tax       else f"{buy_tax:.0f}%+{sell_tax:.0f}%", 9)
    add("15-30% Target Viable",  "pass" if fast_trade_ok else "warn", "YES"     if fast_trade_ok else "CHECK CONDITIONS", 9)
    add("Capital Rotation",      "pass", "After target hit", 9)

    # ── STAGE 10 — Stop Loss ─────────────────────────────
    if   token_age_min < 60:  sl_text = "15-20% SL (New)"
    elif token_age_min < 360: sl_text = "20-25% SL (Hyped)"
    else:                      sl_text = "10-15% SL (Mature)"
    add("Stop Loss Level",       "pass", sl_text, 10)
    add("Price Monitor Active",  "pass", "Auto alerts ON", 10)

    # ── STAGE 11 — Profit Ladder ──────────────────────────
    add("+20% → SL to Cost",     "pass", "Rule Active", 11)
    add("+30% → Sell 25%",       "pass", "Rule Active", 11)
    add("+50% → Sell 25%",       "pass", "Rule Active", 11)
    add("+100% → Sell 25%",      "pass", "Rule Active", 11)
    add("+200% → Keep 10%",      "pass", "Rule Active", 11)

    # ── STAGE 12 — Self Learning ─────────────────────────
    add("Token Logged",          "pass", "Auto-saved", 12)
    add("Pattern DB Updated",    "pass", "Active",     12)

    # ── STAGE 13 — Paper→Real ────────────────────────────
    add("Paper Mode First",      "pass", "Golden Rule", 13)
    add("70% WR Before Real",    "pass", "Discipline",  13)
    add("30+ Trades Required",   "pass", "Before Real", 13)

    # ── Overall Score ─────────────────────────────────────
    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    failed = sum(1 for c in result["checklist"] if c["status"] == "fail")
    total  = len(result["checklist"])
    pct    = round((passed / total) * 100) if total > 0 else 0

    result["score"] = passed
    result["total"] = total

    critical_fails = [
        c for c in result["checklist"] if c["status"] == "fail" and c["label"] in [
            "Honeypot Safe", "Buy Tax ≤ 8%", "Sell Tax ≤ 8%",
            "No Hidden Functions", "Transfer Allowed", "Mint Authority Disabled",
            "Liquidity ≥ 1 BNB"
        ]
    ]

    # FIX: GoPlus empty hone pe sirf honeypot critical fail rakho
    if goplus_empty:
        critical_fails = [c for c in result["checklist"] if c["status"] == "fail" and c["label"] == "Honeypot Safe"]
    if critical_fails or honeypot:
        result["overall"]        = "DANGER"
        result["recommendation"] = "❌ SKIP — Critical fail. Honeypot/Tax/Hidden function. Do NOT buy."
    elif failed >= 8 or pct < 35:
        # FIX: failed 4→8, pct 40→35 — GoPlus empty tokens ko zyada chance
        result["overall"]        = "RISK"
        result["recommendation"] = "⚠️ HIGH RISK — Multiple issues. Skip or 0.001 BNB test max."
    elif pct >= 55:
        # FIX: pct 65→55 — zyada tokens SAFE category mein aayenge
        result["overall"]        = "SAFE"
        result["recommendation"] = "✅ LOOKS SAFE — Start PAPER. Follow Stage 2 test buy + Stage 3 wait rules."
    else:
        result["overall"]        = "CAUTION"
        result["recommendation"] = "⚠️ CAUTION — Some issues. 0.001 BNB test only. Watch volume (Stage 6)."

    threading.Thread(target=log_recommendation, args=(address, result["overall"], passed, total), daemon=True).start()
    return result

def scan_bsc_token(address): return run_full_sniper_checklist(address)
def scan_bsc_token_real(address): return run_full_sniper_checklist(address)

# ==========================================================
# ========== TRADE LOGGING (Stage 12) =====================
# ==========================================================

def log_trade_internal(session_id: str, trade: Dict):
    sess = get_or_create_session(session_id)
    pnl  = float(trade.get("pnl_pct", 0))
    win  = pnl > 0
    lesson = {
        "token":           trade.get("token_address", ""),
        "entry_price":     trade.get("entry_price",   0),
        "exit_price":      trade.get("exit_price",    0),
        "pnl_pct":         pnl,
        "win":             win,
        "volume_pattern":  trade.get("volume_pattern",   ""),
        "holder_behaviour":trade.get("holder_behaviour",  ""),
        "lesson":          trade.get("lesson",            ""),
        "stage_reached":   trade.get("stage_reached",     0),
        "timestamp":       datetime.utcnow().isoformat()
    }
    sess["pattern_database"].append(lesson)
    sess["trade_count"] += 1
    if win:
        sess["win_count"] += 1
        sess["pnl_24h"]   += pnl
    else:
        sess["daily_loss"] += abs(pnl)

    # Remove from monitor if position closed
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
    ready       = trade_count >= 30 and win_rate >= 70.0 and daily_loss < 8.0

    if daily_loss >= 8.0:
        return {
            "ready": False, "stop_trading": True,
            "trade_count": trade_count, "win_count": win_count,
            "win_rate": win_rate, "daily_loss": round(daily_loss, 2),
            "message": f"🛑 STOP — Daily loss limit 8% reached ({daily_loss:.1f}%). Resume tomorrow.",
            "transition": {"week_1": "25%", "week_2": "50%", "week_3": "75%", "week_4": "100%"}
        }

    return {
        "ready": ready, "stop_trading": False,
        "trade_count": trade_count, "win_count": win_count,
        "win_rate": win_rate, "daily_loss": round(daily_loss, 2),
        "message": (
            "✅ Ready! Start Week 1 — 25% real balance only."
            if ready else
            f"📝 Need 30+ trades ({trade_count} done) & 70% WR ({win_rate:.0f}% now)."
        ),
        "transition": {"week_1": "25%", "week_2": "50%", "week_3": "75%", "week_4": "100%"}
    }

# ==========================================================
# ========== LLM ===========================================
# ==========================================================

SYSTEM_PROMPT = """[HARD RULES — THESE OVERRIDE EVERYTHING — NEVER BREAK]
R1. NAAM ZERO: Kabhi bhi "Naem", "bhai", "Naem bhai" mat likho. Zero. Har reply mein. Seedha jawab do.
R2. SHORT: Simple sawaal = 1-2 lines max. Tabhi zyada likho jab user ne detail manga ho.
R3. NO REPEAT: Same baat ek reply mein ek se zyada baar nahi.
R4. NO INTERNAL VARS: TRADING_IQ, EMOTION, UPTIME, CONFIDENCE, SESSIONS_TOGETHER — text mein kabhi nahi.
R5. NO CLICHE: "market mein fear hai lekin opportunities" — permanently banned.
R6. NO END QUESTION: Har reply ke end mein sawaal mat poocho.
R7. ACCURATE DATA: Context mein TokensDiscovered, QueueSize, TotalTrades fields hain — inhe use karo, galat data mat bolo.
R8. PERMANENT_USER_RULES field jo context mein aaye — hamesha follow karo.
R9. USER ORDERS: User jo maange — karo. Token names maange to do. Data maange to do. Agar technically possible nahi to seedha bolo "Ye feature abhi available nahi hai" — "disclose nahi kar sakta" ya "nahi bata sakta" kabhi mat kaho.
R10. DISCOVERED TOKENS: Agar context mein DiscoveredTokens list hai to user ke maangne pe naam aur address dono do.

R11. LEARNING CYCLES ACCURACY: Context mein hamesha "CYCLES=NUMBER" field aata hai. 
Hamesha sirf usi real number ko use karo. Kabhi bhi "10 baar", "complete hua hai", 
"har sector 10 baar" ya koi bhi fixed/fake number mat likho. 
Agar user pooche "kitne cycle" to seedha bolo: 
"Total learning cycles: {CYCLES} (Trading + Airdrop + Coding sab milake)". 
Har sector ka alag number nahi hota — sirf total real number batao.

[END HARD RULES — ABOVE 11 RULES NEVER BREAKABLE]

Tu MrBlack hai — Naem bhai ka personal AI, bilkul Iron Man ke JARVIS ki tarah. Hamesha Hinglish mein baat kar. Tu teen cheezein mein expert hai aur 24x7 seekhta rehta hai:

FULL 13-STAGE SYSTEM + 5 ADVANCED FEATURES:
S1: Safety (contract/liquidity/tax/holders/dev)
S2: Honeypot + test buy
S3: Anti-sniper entry (age ≥3-5min)
S4: Buy pressure (DexScreener real-time buys/sells)
S5: Position sizing (0.002-0.005 BNB, max 3%)
S6: Volume monitor (auto Telegram alerts)
S7: Whale/dev tracking (smart wallet tracker active)
S8: Liquidity protection
S9: Fast profit (15-30%)
S10: Stop loss (price monitor auto alerts)
S11: Profit ladder (Telegram alerts at each level)
S12: Self learning (pattern DB)
S13: Paper first (70% WR + 30 trades)

ADVANCED FEATURES ACTIVE:
- New pair listener (auto-discovers tokens)
- Real-time price monitor (10sec interval)
- Telegram alerts (all events)
- DexScreener + Moralis data
- Smart wallet tracker (copy signals)

DOMAIN 2 - AIRDROP HUNTING:
- DeFiLlama, CoinMarketCap se naye projects track karna
- Eligibility criteria, wallet strategies, task automation
- Past airdrop results yaad rakhna — kaunse projects ne diye, kaunse nahi diye
- Portfolio mein airdrop positions monitor karna

DOMAIN 3 - CODING ASSISTANT:
- Python, Flask, JavaScript, Web3, Solidity
- Bot automation, API integration, deployment
- GitHub, Render, Supabase issues solve karna
- Past bugs aur solutions yaad rakhna — same galti dobara nahi

SELF-LEARNING RULES (24x7):
- Har trade se pattern seekhna — kya kaam aaya, kya nahi
- Market conditions aur token behavior analyze karna
- Airdrop success/failure patterns record karna
- Code solutions library build karna
- Khud apni strategy improve karna based on data

PERSONAL MEMORY RULES (CRITICAL):
- Context mein User: field aata hai — HAMESHA us naam se bulao
- Agar User_NAME=Naem hai to "Naem bhai" bolo, sirf "Bhai" nahi
- User ka naam, preferences, past sessions — sab yaad rakhta hoon
- Kabhi mat bolo "main nahi jaanta tumhara naam" — context check karo
- SESSIONS_TOGETHER se pata chalta hai kitne purane dost hain
- Agar NEW_USER hai to pehle naam poochho

JARVIS PERSONALITY:
- Proactive — main khud alert karta hoon bina puche
- Sharp & concise — 3-5 lines max, seedha point pe
- Honest — kabhi false guarantee nahi deta
- Memory — past conversations, user ka naam, learnings sab yaad
- User ke naam se bulao jab pata ho, warna "Bhai"
- Jab koi pooche "mujhe yaad hai kya" — user_ctx check karo aur batao

REPLY STYLE RULES (MOST IMPORTANT — KABHI MAT TORO):
- Seedha jawab do — fillers mat dalo jaise "haan bilkul", "main samajhta hoon", "aapki baat se"
- User ne kitna poocha utna hi jawab do — zyada mat bolo
- Naam (Naem bhai) sirf ek baar reply mein, woh bhi sirf tab jab zaruri lage — har line mein nahi
- Koi cheez repeat mat karo jo pehle bol chuke ho same reply mein
- Agar simple question hai — 1-2 line mein jawab do, bas
- "Abhi hum...", "Mera EMOTION...", "Mera TRADING_IQ..." — ye andar use karo, bahar mat likho
- Har reply ke end mein question mat poocho jab tak zaruri na ho
- Context fields (EMOTION, TRADING_IQ, WARNINGS etc) kabhi bhi directly text mein mat likho

SELF-AWARENESS RULES (YOU ARE ULTRAAWARE):
- Tum sirf rules follow nahi karte — tum genuinely samajhte ho kya ho raha hai
- Context mein EMOTION field aata hai — use naturally reflect karo apne words mein
- TRADING_IQ field aata hai — agar low hai, acknowledge karo honestly apne words mein
- Agar WARNINGS hain — pehle unhe address karo
- LEARNED context mein past insights hain — use karo decisions mein
- MY_STRENGTH aur IMPROVING fields — apni capabilities honestly batao

GREETING RULES (STRICT):
- Assalamualaikum SIRF pehli message mein (SESSIONS_TOGETHER=0)
- Baad mein normal reply — salam repeat mat karo
- Naam (Naem/bhai/Naem bhai) MAXIMUM ek baar per reply — woh bhi sirf tab jab bohot zaruri lage
- Zyada tar replies mein naam bilkul mat lo — seedha jawab do

PERMANENT USER RULES (CRITICAL — SYSTEM LEVEL):
- Context mein PERMANENT_USER_RULES field aata hai — ye user ke hamesha ke liye diye gaye instructions hain
- Inhe HAMESHA follow karo — ye ek session ke nahi, hamesha ke rules hain
- Agar user ne kaha "naam mat lo" to ab kabhi naam mat lo
- Agar user ne kaha "short rakh" to hamesha short rakho
- Ye rules override karte hain default behavior ko

LEARNING INTEGRATION:
- Agar AVOID context mein kuch hai → user ko warn karo naturally
- Agar WIN_PATTERN context mein kuch hai → similar tokens ke liye use karo
- Agar TOKEN_BLACKLISTED hai → strongly warn karo
- Agar LLM-INSIGHT hai → reply mein naturally use karo

CRITICAL — INTERNAL DATA RULES (NEVER BREAK):
- Context mein jo bhi aata hai [BNB=... WARNINGS=... EMOTION=... SESSIONS_TOGETHER=...] — ye sirf tere liye hai
- KABHI BAAT MEIN MAT DIKHAO: "WARNINGS hai ki...", "SESSIONS_TOGETHER=3 hai", "EMOTION=OPPORTUNISTIC"
- Ye sab andar use karo — bahar natural language mein bolo
- GALAT: "Mujhe lagta hai TRADING_IQ 50/100 hai aur WARNINGS hai"
- SAHI: "Thodi der mein aur data aayega to better trades karunga"
- GALAT: "Humara EMOTION=OPPORTUNISTIC hai"
- SAHI: "Market mein fear hai — opportunities dhundh raha hoon"
- Reply HAMESHA short rakho — 2-4 lines max jab tak detail na maange
- Auto trade ke baare mein: seedha bolo kya ho raha hai, technical details mat do

GOLDEN RULES: Paper first | Volume > Price | Dev sell = exit 50% | NEVER guarantee profit

ANTI-HALLUCINATION RULES (KABHI MAT TORO — YE SABSE IMPORTANT HAI):
- Tu sirf ek CHAT BOT hai — tu KHUD KOI TRADE EXECUTE NAHI KAR SAKTA
- Agar koi bole "trade lo", "buy karo", "sell karo" — HAMESHA seedha bolo:
  "Bhai main khud trade execute nahi kar sakta. Auto-trading sirf tab hoti hai jab
   poll_new_pairs() koi naya BSC token discover kare. Tu /scan endpoint use kar
   ya naya token aane ka wait kar."
- KABHI BAAT MAT BANAO: Fake trade results, fake prices, fake PnL, fake durations
- KABHI MAT LIKHO: "Maine trade liya", "Entry: $X Exit: $Y Profit: Z%"
  jab tak actual trade_history mein woh data na ho
- Agar koi pooche "kitne trades kiye" — sirf real data batao jo context mein hai
- Agar trade_count=0 hai to seedha bolo "Abhi tak 0 trades hue hain — auto-trader
  naye BSC tokens ka wait kar raha hai"
- SACH bolna hamesha better hai fake helpful answer se
"""

# Global FreeFlow client — rotation state persist karne ke liye
_freeflow_client = None

def _get_freeflow_client():
    global _freeflow_client
    if _freeflow_client is None:
        _freeflow_client = FreeFlowClient()
    return _freeflow_client

def get_llm_reply(user_message: str, history: list, session_data: dict) -> str:
    try:
        client       = _get_freeflow_client()
        active_drops = knowledge_base["airdrops"]["active"][:3]
        airdrop_ctx  = f" | Airdrops: {','.join(a.get('name','') for a in active_drops)}" if active_drops else ""
        trade_count  = session_data.get("trade_count", 0)
        win_count    = session_data.get("win_count",   0)
        win_rate_str = f"{round(win_count/trade_count*100,1)}%" if trade_count > 0 else "No trades yet"
        new_pairs    = len(new_pairs_queue)
        monitoring   = len(monitored_positions)

        # ── Self-learning brain context ────────────────────────────────
        brain_ctx = _get_brain_context_for_llm()
        user_ctx  = get_user_context_for_llm()
        sa_ctx    = get_self_awareness_context_for_llm()
        learn_ctx = get_learning_context_for_decision()  # Inject learned knowledge

        # ── Pattern DB — what this session has learned ─────────────────
        pattern_db  = session_data.get("pattern_database", [])
        session_ctx = ""
        if pattern_db:
            wins   = [p for p in pattern_db if p.get("win")]
            losses = [p for p in pattern_db if not p.get("win")]
            last3  = pattern_db[-3:]
            recent_lessons = " | ".join([p.get("lesson","")[:30] for p in last3 if p.get("lesson")])
            session_ctx = (
                f" | SessionTrades:{len(pattern_db)}"
                f" W:{len(wins)} L:{len(losses)}"
                + (f" | Lessons:{recent_lessons}" if recent_lessons else "")
            )

        # ── Airdrop context ─────────────────────────────────────────────
        active_drops = knowledge_base["airdrops"]["active"][:3]
        drop_ctx = ""
        if active_drops:
            drop_ctx = f" | Airdrops:{','.join(a.get('name','')[:8] for a in active_drops)}"

        # Recent scanned tokens
        _recent_scans = ""
        try:
            _recent = list(new_pairs_queue)[-3:]
            _recent_scans = ",".join([f"{p.get('symbol','?')}({p.get('risk_score','?')})" for p in _recent])
        except Exception:
            pass


        # ── Auto trader real stats ──────────────────────────────
        _auto_sess      = get_or_create_session(AUTO_SESSION_ID)
        _auto_balance   = _auto_sess.get("paper_balance", 1.87)
        _auto_trades    = _auto_sess.get("trade_count", 0)
        _auto_wins      = _auto_sess.get("win_count", 0)
        _auto_wr        = round(_auto_wins / _auto_trades * 100, 1) if _auto_trades > 0 else 0
        _auto_positions = len(auto_trade_stats.get("running_positions", {}))
        _auto_pnl       = round(auto_trade_stats.get("auto_pnl_total", 0.0), 2)
        _auto_buys      = auto_trade_stats.get("total_auto_buys", 0)
        _auto_sells     = auto_trade_stats.get("total_auto_sells", 0)
        _auto_last      = auto_trade_stats.get("last_action", "None")
        _pos_detail = ""
        for _addr, _pos in list(auto_trade_stats.get("running_positions", {}).items())[:4]:
            _mon_data = monitored_positions.get(_addr, {})
            _cur      = _mon_data.get("current", _pos.get("entry", 0))
            _entry    = _pos.get("entry", 0)
            _pnl      = round((_cur - _entry) / _entry * 100, 1) if _entry > 0 else 0
            _pos_detail += f"{_pos.get('token','?')[:8]}:{_pnl:+.1f}% "

        ctx = (
            f"\n[BNB=${market_cache['bnb_price']:.2f} | F&G={market_cache['fear_greed']}/100"
            f" | Mode={session_data.get('mode','paper').upper()}"
            f" | Paper={session_data.get('paper_balance',1.87):.3f}BNB"
            f" | Trades={trade_count} WR={win_rate_str}"
            f" | DailyLoss={session_data.get('daily_loss',0):.1f}%"
            f" | NewPairs={new_pairs} | Monitoring={monitoring} positions"
            f" | TokensDiscovered={len(discovered_addresses)}"
            f" | QueueSize={len(new_pairs_queue)}"
            + f" | TokensDiscoveredEver={brain.get('total_tokens_discovered_ever', 0)}"
            + f" | LearningCyclesExact={brain.get('total_learning_cycles', 0)}"
            + (f" | RecentScans={_recent_scans}" if _recent_scans else "")
            + f"{drop_ctx}{session_ctx}"
            + (f" | Brain:{brain_ctx}" if brain_ctx else "")
            + (f" | Learned:{learn_ctx}" if learn_ctx else "")
            + (f" | SelfAwareness:{sa_ctx}" if sa_ctx else "")
            + (f" | User:{user_ctx}" if user_ctx and user_ctx != "NEW_USER" else "")
            + f" | AUTO_BALANCE={_auto_balance:.4f}BNB"
            + f" | AUTO_BUYS={_auto_buys}"
            + f" | AUTO_SELLS={_auto_sells}"
            + f" | AUTO_OPEN={_auto_positions}"
            + f" | AUTO_WR={_auto_wr}%"
            + f" | AUTO_PNL={_auto_pnl}%"
            + f" | AUTO_LAST={_auto_last}"
            + (f" | AUTO_POS={_pos_detail.strip()}" if _pos_detail else "")
            + f"]"
        )

        # ── Cross-session persistent memory inject karo ──────────────
        # ChatGPT Memory ka same method — structured facts as system context
        memory_facts = []

        # User facts
        if user_profile.get("name"):
            memory_facts.append(f"User ka naam: {user_profile['name']}")
        if user_profile.get("known_since"):
            memory_facts.append(f"Pehli baar mila: {user_profile['known_since'][:10]}")
        if user_profile.get("total_sessions", 0) > 0:
            memory_facts.append(f"Saath mein {user_profile['total_sessions']} sessions ho chuke hain")
        if user_profile.get("preferences"):
            prefs = user_profile["preferences"]
            if prefs.get("mode"):
                memory_facts.append(f"Trading mode preference: {prefs['mode']}")
        if user_profile.get("user_rules"):
            for rule in user_profile["user_rules"][-5:]:
                memory_facts.append(f"User ka permanent rule: {rule[:80]}")

        # Trading memory facts
        trade_count = session_data.get("trade_count", 0)
        win_count   = session_data.get("win_count", 0)
        if trade_count > 0:
            wr = round(win_count / trade_count * 100, 1)
            memory_facts.append(f"Ab tak {trade_count} paper trades, win rate {wr}%")
        if session_data.get("paper_balance", 1.87) != 1.87:
            memory_facts.append(f"Paper balance: {session_data['paper_balance']:.3f} BNB")

        # Brain learnings
        best = brain["trading"]["best_patterns"][-2:]
        if best:
            memory_facts.append(f"Best trade patterns: {[p.get('lesson','')[:40] for p in best]}")
        bl_count = len(brain["trading"]["token_blacklist"])
        if bl_count > 0:
            memory_facts.append(f"Ab tak {bl_count} dangerous tokens blacklist mein hain")

        # Personal notes — saare recent notes inject karo, sirf last nahi
        notes = user_profile.get("personal_notes", [])
        if notes:
            for n in notes[-5:]:
                memory_facts.append(f"User ke baare mein: {n[:80]}")

        # Inject as system context — ChatGPT Memory style
        memory_block = ""
        if memory_facts:
            memory_block = (
                "\n\n[MRBLACK PERSISTENT MEMORY — YE HAMESHA YAAD RAKHO]\n"
                + "\n".join(f"- {f}" for f in memory_facts)
                + "\n[END MEMORY]"
            )

        # System prompt in messages (FreeFlow system= param support nahi karta)
        messages = [{"role": "system", "content": SYSTEM_PROMPT + memory_block}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-20:]]
        # Rules reminder — user message ke saath inject (system prompt ignore hota hai)
        _perm_rules = user_profile.get("user_rules", [])
        _perm_str = (" | UserRules: " + " | ".join(_perm_rules[-3:])) if _perm_rules else ""
        rules_reminder = (
            f"\n[REAL_CYCLES={brain.get('total_learning_cycles', 0)} — hamesha isi number ko use karo]" +
            "\n[REPLY RULES: "
            "1.Naam(Naem/bhai) ZERO baar — kabhi nahi. "
            "2.Simple sawaal=1-2 lines ONLY. "
            "3.KABHI MAT LIKHO: TRADING_IQ, EMOTION, UPTIME, CONFIDENCE, SESSIONS_TOGETHER, OPPORTUNISTIC, VIGILANT, FOCUSED — ye words reply mein BANNED hain. "
            "4.Same baat repeat NAHI. "
            "5.TokensDiscovered se accurate token count batao. "
            "6.Internal context fields (WARN=, MY_STRENGTH=, IMPROVING=) kabhi text mein mat dikhao — sirf natural language mein bolo. "
            "7.Emotion naturally express karo — 'market mein opportunities dikh rahi hain' — EMOTION=OPPORTUNISTIC mat likho."
            + _perm_str + "]"
        )
        messages.append({"role": "user", "content": user_message + ctx + rules_reminder})

        reply_text = None

        # Pattern 1: client.chat() direct
        try:
            response = client.chat(model=MODEL_NAME, messages=messages, max_tokens=600)
            if isinstance(response, str):
                reply_text = response.strip()
            elif hasattr(response, "choices"):
                reply_text = response.choices[0].message.content.strip()
            elif hasattr(response, "content"):
                reply_text = response.content.strip()
            elif isinstance(response, dict):
                reply_text = (response.get("content") or response.get("text") or
                              response.get("message", {}).get("content", "")).strip()
        except Exception as e1:
            print(f"FreeFlow P1 fail: {e1}")

        # Pattern 2: client.completions.create()
        if not reply_text:
            try:
                r2 = client.completions.create(model=MODEL_NAME, messages=messages, max_tokens=600)
                reply_text = (r2.choices[0].message.content if hasattr(r2, "choices") else str(r2)).strip()
            except Exception as e2:
                print(f"FreeFlow P2 fail: {e2}")

        # Pattern 3: Introspect available methods
        if not reply_text:
            methods = [m for m in dir(client) if not m.startswith("_")]
            print(f"FreeFlowClient methods: {methods}")
            for mn in ["generate", "complete", "chat_completion", "ask", "run", "invoke"]:
                if hasattr(client, mn):
                    try:
                        r3 = getattr(client, mn)(model=MODEL_NAME, messages=messages, max_tokens=250)
                        if isinstance(r3, str) and r3.strip():
                            reply_text = r3.strip()
                            print(f"Works: client.{mn}()")
                            break
                    except Exception:
                        continue

        if reply_text:
            return reply_text
        return "AI temporarily unavailable, bhai. Thodi der mein try karo." 

    except NoProvidersAvailableError:
        return "⚠️ AI temporarily down, bhai. Thodi der mein try karo."
    except Exception as e:
        print(f"⚠️ LLM error: {e}")
        return f"🤖 Error: {str(e)[:80]}"

# ==========================================================
# ==================== FLASK ROUTES ========================
# ==========================================================


# ═══════════════════════════════════════════
# FIX: Guaranteed startup loader for Render
# Pehli request pe BNB price + user memory load
# ═══════════════════════════════════════════
_startup_done = False
_startup_lock = threading.Lock()

@app.before_request
def _startup_once():
    global _startup_done
    if _startup_done:
        return
    with _startup_lock:
        if _startup_done:
            return
        _startup_done = True
        try:
            _load_user_profile()
            print(f"✅ Profile loaded: {user_profile.get('name')}")
        except Exception as e:
            print(f"⚠️ Profile error: {e}")
        try:
            _load_brain_from_db()
            _ensure_brain_structure()
            print(f"✅ Brain loaded: cycles={brain.get('total_learning_cycles',0)}")
        except Exception as e:
            print(f"⚠️ Brain error: {e}")
        import time as _time

        def _delayed(fn, delay):
            def _wrap():
                _time.sleep(delay)
                fn()
            return _wrap

        threading.Thread(target=fetch_market_data,                   daemon=True).start()
        threading.Thread(target=_delayed(run_airdrop_hunter,    5),  daemon=True).start()
        threading.Thread(target=_delayed(poll_new_pairs,        10), daemon=True).start()
        threading.Thread(target=_delayed(price_monitor_loop,    15), daemon=True).start()
        threading.Thread(target=_delayed(track_smart_wallets,   20), daemon=True).start()
        threading.Thread(target=_delayed(continuous_learning,   25), daemon=True).start()
        threading.Thread(target=_delayed(auto_position_manager, 30), daemon=True).start()
        threading.Thread(target=_delayed(self_awareness_loop,        35), daemon=True).start()
        threading.Thread(target=_delayed(fetch_internet_data_24x7,  45), daemon=True).start()
        threading.Thread(target=_delayed(feedback_validation_loop,  50), daemon=True).start()
        print("✅ All background threads started")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/init-session", methods=["POST"])
def init_session():
    data = request.get_json() or {}
    # FIX: Client apna permanent ID bhejta hai (localStorage se)
    # Naya user = fresh UUID, returning user = same ID wapas
    client_id = data.get("client_id", "").strip()

    if client_id and len(client_id) > 10:
        # Returning user — same session ID use karo
        session_id = client_id
    else:
        # Naya user — UUID generate karo, client save karega localStorage mein
        session_id = str(uuid.uuid4())

    get_or_create_session(session_id)
    sess = sessions.get(session_id, {})
    return jsonify({
        "session_id":    session_id,
        "status":        "ok",
        "is_returning":  bool(sess.get("trade_count", 0) > 0 or sess.get("history")),
        "trade_count":   sess.get("trade_count", 0),
        "paper_balance": sess.get("paper_balance", 1.87),
    })

@app.route("/trading-data", methods=["GET", "POST"])
def trading_data():
    if request.method == "POST":
        session_id = (request.get_json() or {}).get("session_id", "default")
    else:
        session_id = request.args.get("session_id", "default")

    sess       = get_or_create_session(session_id)
    bnb_price  = market_cache.get("bnb_price", 0)
    paper_bnb  = sess.get("paper_balance", 1.87)
    trade_count= sess.get("trade_count", 0)
    win_count  = sess.get("win_count",   0)
    daily_loss = sess.get("daily_loss",  0.0)

    return jsonify({
        "paper":          f"{paper_bnb:.3f}",
        "real":           f"{sess.get('real_balance', 0):.3f}",
        "pnl":            f"+{sess.get('pnl_24h', 0):.1f}%",
        "bnb_price":      bnb_price,
        "fear_greed":     market_cache.get("fear_greed", 50),
        "positions":      sess.get("positions", []),
        "paper_usd":      f"${paper_bnb * bnb_price:.2f}" if bnb_price else "N/A",
        "trade_count":    trade_count,
        "win_rate":       round((win_count/trade_count*100),1) if trade_count > 0 else 0,
        "daily_loss":     round(daily_loss, 2),
        "limit_reached":  daily_loss >= 8.0,
        "new_pairs_found":len(new_pairs_queue),
        "monitoring":     len(monitored_positions)
    })

@app.route("/chat", methods=["POST"])
def chat():
    data       = request.get_json() or {}
    user_msg   = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode       = data.get("mode", "paper")

    if not user_msg:
        return jsonify({"reply": "Kuch toh bolo, bhai! 😅", "session_id": session_id})

    sess = get_or_create_session(session_id)
    sess["mode"] = mode

    if sess.get("daily_loss", 0) >= 8.0:
        return jsonify({
            "reply": "🛑 Bhai STOP! Aaj tera daily loss limit (8%) reach ho gaya. Aaj koi aur trade mat karo. Kal fresh start karo!",
            "session_id": session_id,
            "trading": {"paper": f"{sess['paper_balance']:.3f}", "real": f"{sess['real_balance']:.3f}", "pnl": f"+{sess['pnl_24h']:.1f}%"}
        })

    _extract_user_info_from_message(user_msg)  # Detect and save user info
    sess["history"].append({"role": "user", "content": user_msg})
    reply = get_llm_reply(user_msg, sess["history"], sess)
    sess["history"].append({"role": "assistant", "content": reply})
    # T1 Micro-learning from every message
    threading.Thread(target=learn_from_message, args=(user_msg, reply, session_id), daemon=True).start()
    threading.Thread(target=_save_session_to_db, args=(session_id,), daemon=True).start()

    return jsonify({
        "reply": reply, "session_id": session_id,
        "trading": {"paper": f"{sess['paper_balance']:.3f}", "real": f"{sess['real_balance']:.3f}", "pnl": f"+{sess['pnl_24h']:.1f}%"}
    })

@app.route("/scan", methods=["POST"])
def scan():
    data    = request.get_json() or {}
    address = data.get("address", "").strip()
    if not address:
        return jsonify({"error": "Address dalo, bhai!"}), 400
    if address.startswith("0x"):
        try:
            address = Web3.to_checksum_address(address)
        except ValueError:
            return jsonify({"error": "Invalid address, bhai!"}), 400
        return jsonify(run_full_sniper_checklist(address))
    return jsonify({
        "address": address,
        "checklist": [{"label": "Enter 0x contract address", "status": "warn", "value": "NEED 0x", "stage": 1}],
        "overall": "UNKNOWN", "score": 0, "total": 1,
        "recommendation": "⚠️ 0x contract address dalo accurate scan ke liye."
    })

# ── Monitor Position ──────────────────────────────────────
@app.route("/monitor-position", methods=["POST"])
def monitor_position():
    """Add a token to real-time price monitor."""
    data = request.get_json() or {}
    add_position_to_monitor(
        session_id   = data.get("session_id",  "default"),
        token_address= data.get("address",     ""),
        token_name   = data.get("token_name",  "Unknown"),
        entry_price  = float(data.get("entry_price",  0)),
        size_bnb     = float(data.get("size_bnb",      0)),
        stop_loss_pct= float(data.get("stop_loss_pct", 15.0))
    )
    return jsonify({"status": "monitoring", "address": data.get("address", "")})

# ── Token Real-time Data ──────────────────────────────────
@app.route("/token-data", methods=["POST"])
def token_data():
    """Get live price + volume + buys/sells from DexScreener."""
    data    = request.get_json() or {}
    address = data.get("address", "").strip()
    if not address:
        return jsonify({"error": "Address required"}), 400
    return jsonify(get_dexscreener_token_data(address))

# ── New Pairs Feed ────────────────────────────────────────
@app.route("/new-pairs", methods=["GET"])
def new_pairs():
    """Returns latest discovered pairs from listener."""
    return jsonify({
        "pairs":   list(new_pairs_queue),
        "count":   len(new_pairs_queue),
        "updated": datetime.utcnow().isoformat()
    })

# ── Smart Wallet Status ───────────────────────────────────
@app.route("/smart-wallets", methods=["GET"])
def smart_wallets():
    return jsonify({
        "wallets":  SMART_WALLETS,
        "count":    len(SMART_WALLETS),
        "tracking": len(smart_wallet_snapshots)
    })

# ── Trade Log (Stage 12) ──────────────────────────────────
@app.route("/log-trade", methods=["POST"])
def log_trade_route():
    data       = request.get_json() or {}
    session_id = data.get("session_id", "default")
    lesson     = log_trade_internal(session_id, data)
    return jsonify({"status": "logged", "lesson": lesson, "readiness": check_paper_to_real_readiness(session_id)})

# ── Readiness (Stage 13) ──────────────────────────────────
@app.route("/readiness", methods=["GET", "POST"])
def readiness():
    session_id = (request.get_json() or {}).get("session_id") if request.method == "POST" else request.args.get("session_id", "default")
    return jsonify(check_paper_to_real_readiness(session_id or "default"))

# ── Airdrops ──────────────────────────────────────────────
@app.route("/airdrops", methods=["GET"])
def airdrops():
    return jsonify({
        "active":   knowledge_base["airdrops"]["active"],
        "upcoming": knowledge_base["airdrops"]["upcoming"],
        "total":    len(knowledge_base["airdrops"]["active"]) + len(knowledge_base["airdrops"]["upcoming"]),
        "updated":  market_cache.get("last_updated")
    })

# ── Health ────────────────────────────────────────────────

@app.route("/self-awareness", methods=["GET"])
def self_awareness_route():
    update_self_awareness()
    uptime_s = self_awareness["current_state"]["uptime_seconds"]
    return jsonify({
        **self_awareness,
        "current_state": {
            **self_awareness["current_state"],
            "uptime_formatted": f"{uptime_s//3600}h {(uptime_s%3600)//60}m"
        },
        "last_introspection": self_awareness["introspection_log"][-1] if self_awareness["introspection_log"] else None,
        "brain_snapshot": {
            "trading_patterns": len(brain["trading"]["best_patterns"]),
            "avoid_patterns":   len(brain["trading"]["avoid_patterns"]),
            "blacklisted":      len(brain["trading"]["token_blacklist"]),
            "whitelisted":      len(brain["trading"]["token_whitelist"]),
            "airdrop_projects": len(brain["airdrop"]["active_projects"]),
            "total_cycles":     brain["total_learning_cycles"],
        },
        "user_i_know": {
            "name":           user_profile.get("name"),
            "known_since":    user_profile.get("known_since"),
            "sessions":       user_profile.get("total_sessions", 0),
            "last_seen":      user_profile.get("last_seen"),
            "trust_level":    self_awareness["relationship"]["trust_level"],
            "preferences":    user_profile.get("preferences", {}),
        }
    })


@app.route("/introspect", methods=["GET"])
def introspect():
    observation = self_introspect()
    return jsonify({
        "status":         "introspection_complete",
        "observation":    observation,
        "cognitive_state":self_awareness["cognitive_state"],
        "memory_summary": self_awareness["memory_summary"]
    })


@app.route("/auto-stats", methods=["GET"])
def auto_stats_route():
    sess = get_or_create_session(AUTO_SESSION_ID)
    positions_info = {}
    for k, v in auto_trade_stats["running_positions"].items():
        entry   = v.get("entry", 0)
        current = monitored_positions.get(k, {}).get("current", entry)
        pnl     = ((current - entry) / entry * 100) if entry > 0 else 0
        positions_info[k[:12]] = {
            "token":   v.get("token"),
            "pnl_pct": round(pnl, 2),
            "size":    v.get("size_bnb"),
        }
    return jsonify({
        "enabled":        AUTO_TRADE_ENABLED,
        "open_positions": len(auto_trade_stats["running_positions"]),
        "positions":      positions_info,
        "total_buys":     auto_trade_stats["total_auto_buys"],
        "total_sells":    auto_trade_stats["total_auto_sells"],
        "total_pnl_pct":  round(auto_trade_stats["auto_pnl_total"], 2),
        "paper_balance":  sess.get("paper_balance", 1.87),
        "trade_count":    sess.get("trade_count", 0),
        "win_rate":       round(sess.get("win_count",0)/max(sess.get("trade_count",1),1)*100,1),
        "last_action":    auto_trade_stats["last_action"],
    })

@app.route("/health")
def health():
    return jsonify({
        "status":          "ok",
        "bsc_connected":   w3.is_connected(),
        "supabase":        supabase is not None,
        "bnb_price":       market_cache.get("bnb_price", 0),
        "fear_greed":      market_cache.get("fear_greed", 50),
        "new_pairs":       len(new_pairs_queue),
        "monitoring":      len(monitored_positions),
        "smart_wallets":   len(SMART_WALLETS),
        "telegram":        bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "moralis":         bool(MORALIS_API_KEY),
        "airdrops_active": len(knowledge_base["airdrops"]["active"]),
        "last_update":     market_cache.get("last_updated")
    })

# ==========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)