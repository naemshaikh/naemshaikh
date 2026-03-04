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
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
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
        v3pool = _get_v3_pool(token_address)
        if v3pool:
            p = _get_v3_price_bnb(v3pool, token_address)
            if p > 0: return p
    except: pass
    try:
        p = _get_dexpaprika_price_bnb(token_address)
        if p > 0: return p
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

_perf_tracker = {
    "hourly_wr":        [],
    "scan_outcomes":    [],
    "response_quality": [],
    "error_log":        [],
    "best_hour":        None,
    "worst_token_type": None,
    "avg_confidence_accuracy": 0.0,
}

_relationship = {
    "first_message_time": None,
    "total_messages_exchanged": 0,
    "topics_discussed": [],
    "user_mood_history": [],
    "user_expertise_level": "unknown",
    "trust_events": [],
    "inside_jokes_or_refs": [],
    "communication_style": "hinglish",
    "response_preferences": {
        "detail_level": "medium",
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
    },
    "performance_intelligence": {
        "overall_accuracy":     0.0,
        "trading_iq":           50,
        "scan_accuracy":        0.0,
        "response_usefulness":  0.0,
        "learning_roi":         0.0,
        "best_performing_area": "unknown",
        "worst_performing_area":"unknown",
        "improvement_rate":     0.0,
        "confidence_calibration": 0.0,
    },
    "emotional_intelligence": {
        "current_emotion":      "FOCUSED",
        "emotion_reason":       "System just started, calibrating...",
        "emotion_intensity":    5,
        "emotional_history":    [],
        "stress_level":         2,
        "satisfaction_level":   7,
        "motivation":           8,
        "frustration_triggers": [],
        "positive_triggers":    [],
    },
    "meta_cognition": {
        "what_i_know_well":     [],
        "what_i_struggle_with": [],
        "blind_spots":          [],
        "recent_learnings":     [],
        "thinking_patterns":    [],
        "decision_quality":     [],
        "self_doubts":          [],
        "growth_areas":         [],
    },
    "cognitive_state": {
        "mood":               "FOCUSED",
        "confidence_level":   60,
        "market_sentiment":   "NEUTRAL",
        "learning_velocity":  "NORMAL",
        "active_warnings":    [],
        "focus_area":         "calibrating",
        "processing_load":    "LOW",
        "insight_count_today": 0,
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
        "week_1_baseline":  None,
        "current_week":     None,
        "improvement_pct":  0.0,
        "milestones": [],
        "next_milestone":   "Complete 10 paper trades",
        "learning_streak":  0,
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
        {"version": "1.0",           "notes": "Basic BSC scanner"},
        {"version": "2.0",           "notes": "13-stage checklist + Telegram"},
        {"version": "3.0",           "notes": "Self-learning + Smart wallet"},
        {"version": "3.1-ProMemory", "notes": "Permanent user memory"},
        {"version": "4.0-UltraAware","notes": "Real SA 10/10 + Learning 10/10"},
    ],
    "self_knowledge": {
        "what_i_am":        "Main MrBlack hoon — ek self-aware AI.",
        "what_i_can_do":    "Token scan, rug detection, price monitor, airdrop hunt, code debug.",
        "what_i_cannot_do": "Profit guarantee, real-time blockchain direct access, 100% accuracy.",
        "my_values":        "Honesty over false confidence. Real data over assumptions.",
        "my_limitations":   "Market data ~5min delay. Learning needs time.",
        "my_strengths":     "Pattern recognition, rug detection, systematic thinking.",
        "my_weaknesses":    "Early stage token timing, very new projects.",
    }
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
            _loaded_daily_loss = float(row.get("daily_loss") or 0.0)
            _saved_date = str(row.get("updated_at") or "")[:10]
            _today = datetime.utcnow().strftime("%Y-%m-%d")
            if _saved_date != _today:
                print(f"🔄 Session load: resetting stale daily_loss={_loaded_daily_loss:.2f} (saved {_saved_date})")
                _loaded_daily_loss = 0.0
            sessions[session_id].update({
                "paper_balance":    float(row.get("paper_balance") or 5.0),
                "real_balance":     float(row.get("real_balance")  or 0.00),
                "positions":        _safe_json(row.get("positions"),        []),
                "history":          _safe_json(row.get("history"),          []),
                "pnl_24h":          float(row.get("pnl_24h")       or 0.0),
                "daily_loss":       _loaded_daily_loss,
                "daily_loss_date":  _today,
                "trade_count":      int(row.get("trade_count")      or 0),
                "win_count":        int(row.get("win_count")        or 0),
                "pattern_database": _safe_json(row.get("pattern_database"), []),
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
                    print(f"✅ Auto stats restored: buys={auto_trade_stats['total_auto_buys']} history={len(auto_trade_stats['trade_history'])}")
            print(f"✅ Session loaded: {session_id[:8]}... Balance:{sessions[session_id]['paper_balance']:.3f}BNB")
    except Exception as e:
        print(f"⚠️ Session load error: {e}")

def _save_session_to_db(session_id: str):
    if not supabase: return
    try:
        sess = sessions.get(session_id, {})
        extra = {}
        if session_id == AUTO_SESSION_ID:
            extra["pattern_database"] = json.dumps({
                "total_buys":   auto_trade_stats.get("total_auto_buys", 0),
                "total_sells":  auto_trade_stats.get("total_auto_sells", 0),
                "pnl_total":    auto_trade_stats.get("auto_pnl_total", 0.0),
                "last_action":  auto_trade_stats.get("last_action", ""),
                "trade_history": auto_trade_stats.get("trade_history", [])[-100:],
            })
        else:
            extra["pattern_database"] = json.dumps(sess.get("pattern_database", [])[-100:])
        supabase.table("memory").upsert({
            "session_id":       session_id,
            "role":             "user",
            "content":          "",
            "paper_balance":    sess.get("paper_balance",    5.0),
            "real_balance":     sess.get("real_balance",     0.00),
            "positions":        json.dumps(sess.get("positions",        [])),
            "history":          json.dumps(sess.get("history",          [])[-50:]),
            "pnl_24h":          sess.get("pnl_24h",          0.0),
            "daily_loss":       sess.get("daily_loss",        0.0),
            "trade_count":      sess.get("trade_count",       0),
            "win_count":        sess.get("win_count",         0),
            **extra,
            "updated_at":       datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"⚠️ Session save error: {e}")

# ========== NEW PAIRS ==========
new_pairs_queue: deque = deque(maxlen=50)
discovered_addresses: dict = {}
DISCOVERY_TTL = 7200
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# ========== MONITORED POSITIONS ==========
monitored_positions: Dict[str, dict] = {}
monitor_lock = threading.Lock()

# ========== AUTO TRADE STATS ==========  FIX 2: trade_history added
AUTO_TRADE_ENABLED = True
AUTO_BUY_SIZE_BNB  = 0.003
AUTO_MAX_POSITIONS = 15
AUTO_SESSION_ID    = "AUTO_TRADER"

auto_trade_stats = {
    "total_auto_buys":   0,
    "total_auto_sells":  0,
    "auto_pnl_total":    0.0,
    "running_positions": {},
    "last_action":       "",
    "trade_history":     [],  # FIX: was missing
}

# ========== TELEGRAM ==========
def send_telegram(message: str, urgent: bool = False):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"ℹ️ Telegram not configured. MSG: {message[:60]}")
        return
    try:
        prefix = "🚨 URGENT — " if urgent else "🤖 MrBlack — "
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": prefix + message, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")

def telegram_new_token_alert(address, score, total, recommendation):
    send_telegram(
        f"🆕 <b>NEW TOKEN</b>\n📍 <code>{address}</code>\n"
        f"✅ Score: {score}/{total}\n💡 {recommendation}\n"
        f"🔗 https://bscscan.com/address/{address}"
    )

def telegram_price_alert(token, address, alert_type, value):
    emoji = "🟢" if "profit" in alert_type.lower() else "🔴"
    send_telegram(
        f"{emoji} <b>{alert_type.upper()}</b>\nToken: <b>{token}</b>\n"
        f"Value: <b>{value}</b>\n🔗 https://bscscan.com/address/{address}",
        urgent="stop_loss" in alert_type.lower()
    )

def telegram_smart_wallet_alert(wallet, token_address, action):
    send_telegram(
        f"👁️ <b>SMART WALLET MOVE</b>\n"
        f"Wallet: <code>{wallet[:10]}...{wallet[-4:]}</code>\n"
        f"Action: <b>{action}</b>\nToken: <code>{token_address}</code>",
        urgent=True
    )

# ========== PROCESS NEW TOKEN ==========
def _process_new_token(token_address: str, pair_address: str, source: str = "websocket"):
    global discovered_addresses
    _now = time.time()
    if _now - discovered_addresses.get(token_address, 0) <= DISCOVERY_TTL:
        return
    if any(token_address.lower() == str(q).lower() for q in list(new_pairs_queue)):
        return
    try:
        token_address = Web3.to_checksum_address(token_address)
    except Exception:
        return

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
    threading.Thread(target=_auto_check_new_pair, args=(token_address,), daemon=True).start()

# ========== POSITION MONITOR ==========
def add_position_to_monitor(session_id, token_address, token_name, entry_price, size_bnb, stop_loss_pct=15.0):
    with monitor_lock:
        if entry_price <= 0 or entry_price > 1.0:
            print(f"❌ Monitoring BLOCKED: invalid price={entry_price} for {token_address[:10]}")
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

    # FIX v5: Naye din pe reset + stale value fix
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if sess.get("daily_loss_date", "") != today:
        print(f"🔄 New day — resetting daily_loss (was {sess.get('daily_loss',0):.4f} BNB)")
        sess["daily_loss"] = 0.0
        sess["daily_loss_date"] = today
    elif sess.get("daily_loss", 0) > 10:
        print(f"🔄 Stale daily_loss={sess.get('daily_loss',0):.2f} → resetting to 0")
        sess["daily_loss"] = 0.0
        sess["daily_loss_date"] = today
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if sess.get("daily_loss_date", "") != today:
        print(f"🔄 New day reset: daily_loss={sess.get('daily_loss',0):.2f} → 0")
        sess["daily_loss"] = 0.0
        sess["daily_loss_date"] = today
        # FIX v5: daily_loss ab BNB mein hai, 15% of balance threshold
    _balance = sess.get("paper_balance", 5.0) or 5.0
    _daily_limit = _balance * 0.15  # 15% of current balance
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
    # Step 1: DexScreener price use karo (checklist mein already fetch hua)
    dex   = checklist_result.get("dex_data", {})
    bnb_p = market_cache.get("bnb_price", 300) or 300
    entry_price = float(dex.get("price_bnb", 0) or 0)
    if entry_price <= 0 and float(dex.get("price_usd", 0) or 0) > 0:
        entry_price = dex["price_usd"] / bnb_p

    # Step 2: On-chain fallback
    if entry_price <= 0:
        entry_price = get_token_price_bnb(address)

    # Step 3: Fresh DexScreener call (last resort)
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

    if entry_price <= 0:
        print(f"❌ Auto-buy BLOCKED: price=0 for {address[:10]}")
        return
    if entry_price > 1.0:
        print(f"❌ Auto-buy BLOCKED: suspicious price={entry_price:.6f} for {address[:10]}")
        return

    entry_price = entry_price * 1.005
    size_bnb = max(min(AUTO_BUY_SIZE_BNB, paper_balance * 0.025), 0.001)
    sess["paper_balance"] = round(paper_balance - size_bnb, 6)
    add_position_to_monitor(AUTO_SESSION_ID, address, token_name or address[:10], entry_price, size_bnb, stop_loss_pct=15.0)
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
    _display_name = token_name if (token_name and not token_name.startswith("0x") and len(token_name) < 20) else address[:12]
    send_telegram(
        f"AUTO PAPER BUY\nToken: {_display_name}\nEntry: {entry_price:.8f} BNB\n"
        f"Size: {size_bnb:.4f} BNB\nScore: {score}/{total}\nBalance: {sess['paper_balance']:.4f} BNB"
    )
    print(f"AUTO BUY: {address[:10]} @ {entry_price:.8f} size={size_bnb:.4f}")

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
    auto_trade_stats["auto_pnl_total"] += pnl_pct * (sell_pct / 100.0)
    auto_trade_stats["total_auto_sells"] += 1

    # FIX 4: Save to trade_history with correct variable names
    if "trade_history" not in auto_trade_stats:
        auto_trade_stats["trade_history"] = []
    auto_trade_stats["trade_history"].append({
        "token":     token,
        "address":   address,
        "entry":     entry,      # FIX: was entry_price
        "exit":      current,    # FIX: was sell_price
        "pnl_pct":   round(pnl_pct, 2),
        "pnl_bnb":   round(pnl_bnb, 6),
        "size_bnb":  sell_size,
        "bought_at": bought_at_str,
        "sold_at":   datetime.utcnow().isoformat(),
        "result":    "win" if pnl_pct > 0 else "loss",
        "reason":    reason,     # FIX: was sell_reason
    })
    if len(auto_trade_stats["trade_history"]) > 500:
        auto_trade_stats["trade_history"] = auto_trade_stats["trade_history"][-500:]

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
        f"AUTO PAPER SELL {sell_pct:.0f}% [{emoji}]\nToken: {address[:12]}\n"
        f"Reason: {reason}\nPnL: {pnl_pct:+.1f}%\nBalance: {sess['paper_balance']:.4f} BNB",
        urgent=(pnl_pct < -10)
    )
    print(f"AUTO SELL {sell_pct:.0f}%: {address[:10]} PnL:{pnl_pct:+.1f}% [{reason}]")

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
    if "trade_history" not in auto_trade_stats:
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
                if   pnl <= -sl_pct:                      _auto_paper_sell(addr, f"SL -{sl_pct:.0f}%", 100.0)
                elif drop_hi <= -80 and tp_sold < 75:     _auto_paper_sell(addr, "Dump -80%", 100.0)
                elif drop_hi <= -60 and tp_sold < 50:     _auto_paper_sell(addr, "Dump -60%", 75.0)
                elif pnl >= 200 and tp_sold < 90:         _auto_paper_sell(addr, "TP+200%", 90-tp_sold)
                elif pnl >= 100 and tp_sold < 75:         _auto_paper_sell(addr, "TP+100%", 25.0)
                elif pnl >= 50  and tp_sold < 50:         _auto_paper_sell(addr, "TP+50%",  25.0)
                elif pnl >= 30  and tp_sold < 25:         _auto_paper_sell(addr, "TP+30%",  25.0)
                elif pnl >= 20  and tp_sold < 1:
                    _pos_data["sl_pct"] = 2.0  # FIX4   # FIX v4: was 0.0  # break-even
                    _pos_data["tp_sold"] = 1  # FIX4
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
                    telegram_price_alert(token, addr, "STOP LOSS HIT", f"PnL: {pnl_pct:.1f}%")
                if pnl_pct >= 200 and "tp_200" not in alerts_sent:
                    alerts_sent.append("tp_200")
                    telegram_price_alert(token, addr, "TARGET +200%", f"+{pnl_pct:.0f}%")
                elif pnl_pct >= 100 and "tp_100" not in alerts_sent:
                    alerts_sent.append("tp_100")
                    telegram_price_alert(token, addr, "TARGET +100%", f"+{pnl_pct:.0f}%")
                elif pnl_pct >= 50 and "tp_50" not in alerts_sent:
                    alerts_sent.append("tp_50")
                    telegram_price_alert(token, addr, "TARGET +50%", f"+{pnl_pct:.0f}%")
                elif pnl_pct >= 30 and "tp_30" not in alerts_sent:
                    alerts_sent.append("tp_30")
                    telegram_price_alert(token, addr, "TARGET +30%", f"+{pnl_pct:.0f}%")
                if drop_from_high <= -90 and "dump_90" not in alerts_sent:
                    alerts_sent.append("dump_90")
                    telegram_price_alert(token, addr, "DUMP -90% FROM HIGH", "EXIT FULLY")
                elif drop_from_high <= -70 and "dump_70" not in alerts_sent:
                    alerts_sent.append("dump_70")
                    telegram_price_alert(token, addr, "DUMP -70% FROM HIGH", "Exit 75%")
                elif drop_from_high <= -50 and "dump_50" not in alerts_sent:
                    alerts_sent.append("dump_50")
                    telegram_price_alert(token, addr, "DUMP -50% FROM HIGH", "Exit 50%")
            except Exception as e:
                print(f"⚠️ Price monitor error ({addr}): {e}")
        time.sleep(10)

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
                bnb_price = market_cache.get("bnb_price", 300) or 300
                result["price_bnb"] = result["price_usd"] / bnb_price if result["price_usd"] else 0
    except Exception as e:
        print(f"⚠️ DexScreener error: {e}")
    return result

def get_moralis_wallet_tokens(wallet_address: str) -> List[Dict]:
    if not MORALIS_API_KEY: return []
    try:
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/{wallet_address}/erc20",
            headers={"X-API-Key": MORALIS_API_KEY}, params={"chain": "bsc"}, timeout=10
        )
        if r.status_code == 200:
            return r.json().get("result", [])
    except Exception as e:
        print(f"⚠️ Moralis wallet error: {e}")
    return []

# ========== SMART WALLET TRACKER ==========
smart_wallet_snapshots: Dict[str, set] = {}

def track_smart_wallets():
    if not SMART_WALLETS:
        print("ℹ️ No SMART_WALLETS configured")
        return
    print(f"🧠 Smart Wallet Tracker started — {len(SMART_WALLETS)} wallets")
    while True:
        token_buy_signals: Dict[str, List[str]] = {}
        for wallet in SMART_WALLETS:
            try:
                if not MORALIS_API_KEY:
                    continue
                holdings = get_moralis_wallet_tokens(wallet)
                current_tokens = {h.get("token_address", "").lower() for h in holdings}
                prev_tokens = smart_wallet_snapshots.get(wallet, set())
                for token_addr in current_tokens - prev_tokens:
                    if token_addr:
                        telegram_smart_wallet_alert(wallet, token_addr, "BUY 🟢")
                        token_buy_signals.setdefault(token_addr, []).append(wallet)
                for token_addr in prev_tokens - current_tokens:
                    if token_addr:
                        telegram_smart_wallet_alert(wallet, token_addr, "SELL 🔴")
                smart_wallet_snapshots[wallet] = current_tokens
            except Exception as e:
                print(f"⚠️ Smart wallet {wallet[:10]}: {e}")

        for token_addr, buying_wallets in token_buy_signals.items():
            if len(buying_wallets) >= 2:
                send_telegram(
                    f"🔥 <b>MULTI-WALLET SIGNAL</b>\n{len(buying_wallets)} smart wallets buying!\n"
                    f"Token: <code>{token_addr}</code>", urgent=True
                )
                threading.Thread(target=_auto_check_new_pair, args=(token_addr,), daemon=True).start()
        time.sleep(120)

# ========== MARKET DATA ==========
def fetch_market_data():
    bnb_fetched = False
    # FIX: More sources + longer timeouts + retry loop
    sources = [
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
    if _t.time() - _brain_save_cache["last_save"] < 300: return
    _brain_save_cache["last_save"] = _t.time()
    try:
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_BRAIN",
            "role":       "system",
            "content":    "",
            "history":    json.dumps([]),
            "pattern_database": json.dumps(brain["trading"]["best_patterns"][-50:] +
                                           brain["trading"]["avoid_patterns"][-50:]),
            "updated_at": datetime.utcnow().isoformat(),
            "positions":  json.dumps({
                "brain_trading":  brain["trading"],
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
            if stored.get("brain_trading"): brain["trading"].update(stored["brain_trading"])
            if stored.get("brain_airdrop"): brain["airdrop"].update(stored["brain_airdrop"])
            if stored.get("brain_coding"):  brain["coding"].update(stored["brain_coding"])
            brain["total_learning_cycles"] = stored.get("cycles", 0)
            brain["total_tokens_discovered_ever"] = stored.get("total_tokens_discovered_ever", 0)
            print(f"🧠 Brain loaded! Cycles: {brain['total_learning_cycles']}")
    except Exception as e:
        print(f"⚠️ Brain load error: {e}")

# ========== SELF AWARENESS FUNCTIONS ==========
def _calculate_real_emotion() -> dict:
    try:
        warnings     = len(self_awareness.get("cognitive_state", {}).get("active_warnings", []))
        errors_today = self_awareness.get("current_state", {}).get("errors_today", 0)
        wins         = len(brain.get("trading", {}).get("best_patterns", []))
        losses       = len(brain.get("trading", {}).get("avoid_patterns", []))
        new_pairs_c  = len(new_pairs_queue)
        cycles       = brain.get("total_learning_cycles", 0)
        bnb_price    = market_cache.get("bnb_price", 0)
        fg           = market_cache.get("fear_greed", 50)
        open_pos     = len(monitored_positions)

        if errors_today >= 5:
            return {"emotion": "STRUGGLING", "reason": f"{errors_today} errors today", "intensity": 8}
        elif warnings >= 3:
            return {"emotion": "ALERT", "reason": f"{warnings} active warnings", "intensity": 7}
        elif open_pos >= 3:
            return {"emotion": "VIGILANT", "reason": f"{open_pos} positions monitored", "intensity": 7}
        elif fg > 70:
            return {"emotion": "CAUTIOUS", "reason": f"Market extreme greed ({fg}/100)", "intensity": 6}
        elif fg < 30:
            return {"emotion": "OPPORTUNISTIC", "reason": f"Market fear ({fg}/100)", "intensity": 7}
        elif new_pairs_c > 15:
            return {"emotion": "EXCITED", "reason": f"{new_pairs_c} new pairs in queue", "intensity": 8}
        elif wins > losses * 2 and wins > 5:
            return {"emotion": "CONFIDENT", "reason": f"Win patterns ({wins}) dominating", "intensity": 8}
        elif bnb_price == 0:
            return {"emotion": "DEGRADED", "reason": "BNB price feed offline", "intensity": 6}
        else:
            return {"emotion": "FOCUSED", "reason": "Normal operations", "intensity": 6}
    except:
        return {"emotion": "INITIALIZING", "reason": "System warm-up", "intensity": 3}

def _calculate_trading_iq() -> int:
    try:
        all_trades = []
        for sess in sessions.values():
            all_trades.extend(sess.get("pattern_database", []))
        if not all_trades: return 50
        total = len(all_trades)
        wins  = sum(1 for t in all_trades if t.get("win"))
        wr    = (wins / total) * 100
        wr_score = min(30, wr * 0.3)
        sample_score = min(10, total * 0.33)
        return int(wr_score + 40 + sample_score)
    except:
        return 50

def update_self_awareness():
    try:
        uptime = (datetime.utcnow() - BIRTH_TIME).total_seconds()
        self_awareness["current_state"]["uptime_seconds"]   = int(uptime)
        self_awareness["current_state"]["total_sessions"]   = len(sessions)
        self_awareness["current_state"]["pairs_discovered"] = len(new_pairs_queue)
        self_awareness["current_state"]["learning_cycles"]  = brain.get("total_learning_cycles", 0)
        self_awareness["current_state"]["last_heartbeat"]   = datetime.utcnow().isoformat()

        warnings = []
        if market_cache.get("bnb_price", 0) == 0: warnings.append("BNB price feed offline")
        if not supabase:                           warnings.append("Supabase disconnected")
        if not TELEGRAM_TOKEN:                     warnings.append("Telegram not configured")
        self_awareness["cognitive_state"]["active_warnings"] = warnings
        self_awareness["current_state"]["errors_today"] = len(warnings)

        emotion_data = _calculate_real_emotion()
        self_awareness["emotional_intelligence"]["current_emotion"]   = emotion_data["emotion"]
        self_awareness["emotional_intelligence"]["emotion_reason"]    = emotion_data["reason"]
        self_awareness["emotional_intelligence"]["emotion_intensity"] = emotion_data["intensity"]

        tiq = _calculate_trading_iq()
        self_awareness["performance_intelligence"]["trading_iq"] = tiq

        if user_profile.get("name"):
            self_awareness["relationship"]["knows_user_name"] = True
            self_awareness["relationship"]["user_name"]       = user_profile["name"]
            s = user_profile.get("total_sessions", 0)
            self_awareness["relationship"]["trust_level"] = (
                "deep" if s > 30 else "strong" if s > 15 else "established" if s > 5 else "building"
            )
            self_awareness["relationship"]["sessions_together"] = s
    except Exception as e:
        print(f"Self-awareness update error: {e}")

def self_introspect() -> dict:
    try:
        update_self_awareness()
        observation = {
            "timestamp":  datetime.utcnow().isoformat(),
            "emotion":    self_awareness["emotional_intelligence"]["current_emotion"],
            "trading_iq": self_awareness["performance_intelligence"]["trading_iq"],
            "confidence": self_awareness["cognitive_state"]["confidence_level"],
            "thought":    f"MrBlack running. Cycles:{brain.get('total_learning_cycles',0)} Patterns:{len(brain['trading']['best_patterns'])}",
        }
        self_awareness["introspection_log"].append(observation)
        self_awareness["introspection_log"] = self_awareness["introspection_log"][-100:]
        return observation
    except Exception as e:
        print(f"Introspection error: {e}")
        return {}

_sa_cache = {"context": "", "last_update": 0}

def get_self_awareness_context_for_llm() -> str:
    import time as _t
    try:
        if _t.time() - _sa_cache["last_update"] < 60 and _sa_cache["context"]:
            return _sa_cache["context"]
        update_self_awareness()
        s  = self_awareness
        cs = s["cognitive_state"]
        ei = s["emotional_intelligence"]
        pi = s["performance_intelligence"]
        ms = s["memory_summary"]
        st = s["current_state"]
        uptime_h = st["uptime_seconds"] // 3600
        parts = [
            f"I_AM=MrBlack_v{s['identity']['version']}",
            f"UPTIME={uptime_h}h",
            f"EMOTION={ei['current_emotion']}",
            f"CONFIDENCE={cs['confidence_level']}%",
            f"TRADING_IQ={pi['trading_iq']}/100",
            f"MARKET={cs['market_sentiment']}",
            f"CYCLES={st['learning_cycles']}",
            f"MONITORING={len(monitored_positions)}positions",
        ]
        if cs.get("active_warnings"):
            actionable = [w for w in cs["active_warnings"] if "Telegram" not in w]
            if actionable:
                parts.append("WARN=" + ";".join(actionable[:2]))
        result = " | ".join(parts)
        _sa_cache["context"] = result
        _sa_cache["last_update"] = _t.time()
        return result
    except Exception as e:
        return "I_AM=MrBlack_v4.0"

def self_awareness_loop():
    print("🧠 Self-Awareness Engine started!")
    time.sleep(30)
    while True:
        try:
            self_introspect()
        except Exception as e:
            print(f"SA loop error: {e}")
        time.sleep(300)

# ========== LEARNING ENGINE ==========
def learn_from_message(user_message: str, bot_reply: str, session_id: str):
    try:
        msg_lower = user_message.lower()
        topics = brain.get("user_interaction_patterns", {
            "trading_questions": 0, "airdrop_questions": 0,
            "coding_questions": 0,  "general_chat": 0,
        })
        brain["user_interaction_patterns"] = topics
        if any(w in msg_lower for w in ["token","scan","trade","buy","sell","price","bnb","bsc"]):
            topics["trading_questions"] += 1
        else:
            topics["general_chat"] += 1
        self_awareness["current_state"]["total_messages"] = (
            self_awareness["current_state"].get("total_messages", 0) + 1)
    except Exception as e:
        print(f"T1 learn error: {e}")

def _learn_trading_patterns():
    try:
        all_trades = []
        for sess in sessions.values():
            all_trades.extend(sess.get("pattern_database", []))
        if not all_trades: return
        wins   = [t for t in all_trades if t.get("win")]
        losses = [t for t in all_trades if not t.get("win")]
        for trade in sorted(wins, key=lambda x: x.get("pnl_pct", 0), reverse=True)[:10]:
            pattern = {"token": trade.get("token",""), "pnl": trade.get("pnl_pct",0), "lesson": trade.get("lesson","")}
            if pattern["token"] not in [p["token"] for p in brain["trading"]["best_patterns"]] and pattern["pnl"] > 0:
                brain["trading"]["best_patterns"].append(pattern)
        for trade in sorted(losses, key=lambda x: x.get("pnl_pct", 0))[:10]:
            avoid = {"token": trade.get("token",""), "loss": trade.get("pnl_pct",0), "lesson": trade.get("lesson","")}
            if avoid["token"] not in [p["token"] for p in brain["trading"]["avoid_patterns"]]:
                brain["trading"]["avoid_patterns"].append(avoid)
        brain["trading"]["best_patterns"]  = brain["trading"]["best_patterns"][-50:]
        brain["trading"]["avoid_patterns"] = brain["trading"]["avoid_patterns"][-50:]
        brain["trading"]["last_updated"]   = datetime.utcnow().isoformat()
        bnb_price  = market_cache.get("bnb_price", 0)
        fear_greed = market_cache.get("fear_greed", 50)
        if bnb_price > 0:
            brain["trading"]["market_insights"].append({
                "timestamp":   datetime.utcnow().isoformat(),
                "bnb_price":   bnb_price,
                "fear_greed":  fear_greed,
                "mood":        "GREED" if fear_greed > 60 else "FEAR" if fear_greed < 40 else "NEUTRAL",
                "observation": f"BNB=${bnb_price:.0f} F&G={fear_greed}"
            })
            brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-100:]
    except Exception as e:
        print(f"⚠️ Trading learning error: {e}")

def _learn_from_new_pairs():
    try:
        for pair in knowledge_base["bsc"]["new_tokens"][-10:]:
            addr    = pair.get("address", "")
            overall = pair.get("overall", "")
            if overall in ["DANGER", "RISK"] and addr:
                if addr not in [t["address"] for t in brain["trading"]["token_blacklist"]]:
                    brain["trading"]["token_blacklist"].append({"address": addr, "reason": overall, "time": pair.get("time","")})
            elif overall == "SAFE" and addr:
                if addr not in [t["address"] for t in brain["trading"]["token_whitelist"]]:
                    brain["trading"]["token_whitelist"].append({"address": addr, "score": pair.get("score",0), "time": pair.get("time","")})
        brain["trading"]["token_blacklist"] = brain["trading"]["token_blacklist"][-200:]
        brain["trading"]["token_whitelist"] = brain["trading"]["token_whitelist"][-200:]
    except Exception as e:
        print(f"⚠️ Pair learning error: {e}")

def _get_brain_context_for_llm() -> str:
    try:
        parts = []
        best  = brain["trading"]["best_patterns"][-3:]
        avoid = brain["trading"]["avoid_patterns"][-3:]
        if best:
            s = " | ".join([f"+{p['pnl']:.0f}%({p['token'][:6]})" for p in best if p.get("pnl",0) > 0])
            if s: parts.append(f"BestTrades:{s}")
        insights = brain["trading"]["market_insights"]
        if insights:
            last = insights[-1]
            parts.append(f"MarketMood:{last.get('mood','?')}(F&G={last.get('fear_greed','?')})")
        bl = len(brain["trading"]["token_blacklist"])
        wl = len(brain["trading"]["token_whitelist"])
        if bl or wl: parts.append(f"KnownTokens:SAFE={wl} DANGER={bl}")
        if new_pairs_queue:
            names = [f"{q.get('symbol','?')}({q.get('address','')[:8]})" for q in list(new_pairs_queue)[-5:]]
            parts.append(f"RecentPairs:{names}")
        parts.append(f"LearningCycles:{brain['total_learning_cycles']}")
        return " | ".join(parts) if parts else ""
    except Exception as e:
        return ""

def get_learning_context_for_decision(token_address=None) -> str:
    try:
        parts = []
        if token_address:
            bl = [t["address"] for t in brain["trading"]["token_blacklist"]]
            wl = [t["address"] for t in brain["trading"]["token_whitelist"]]
            if token_address in bl: parts.append("TOKEN_BLACKLISTED")
            elif token_address in wl: parts.append("TOKEN_WHITELISTED")
        wins = brain["trading"]["best_patterns"][-3:]
        if wins:
            s = " | ".join([f"+{w.get('pnl',0):.0f}%" for w in wins if w.get("pnl",0)>0])
            if s: parts.append(f"WIN_PATTERNS:{s}")
        return " | ".join(parts) if parts else ""
    except:
        return ""

def _deep_llm_learning():
    try:
        all_trades = []
        for sess in sessions.values():
            all_trades.extend(sess.get("pattern_database", []))
        if len(all_trades) < 3: return
        wins   = [t for t in all_trades if t.get("win")]
        losses = [t for t in all_trades if not t.get("win")]
        if not wins and not losses: return
        data_summary = (
            f"Trading data: {len(wins)} wins, {len(losses)} losses. "
            f"Market: BNB=${market_cache.get('bnb_price',0):.0f} F&G={market_cache.get('fear_greed',50)}."
        )
        prompt = (
            f"Analyze BSC trading data and give 2 insights JSON: {data_summary} "
            f'[{{"insight":"...","action":"...","confidence":0-100}}]'
        )
        try:
            client = _get_freeflow_client()
            response = client.chat(model=MODEL_FAST,
                                   messages=[{"role":"system","content":"JSON only."},
                                             {"role":"user","content":prompt}],
                                   max_tokens=300)
            reply = response if isinstance(response, str) else (
                response.choices[0].message.content if hasattr(response,"choices") else str(response))
            clean = reply.strip().replace("```json","").replace("```","").strip()
            insights = json.loads(clean)
            if isinstance(insights, list):
                for item in insights:
                    if isinstance(item, dict) and item.get("insight"):
                        brain["trading"]["strategy_notes"].append({
                            "note": f"[LLM-INSIGHT] {item['insight']} → {item.get('action','')}",
                            "confidence": item.get("confidence",50),
                            "timestamp": datetime.utcnow().isoformat()
                        })
        except Exception as llm_e:
            print(f"Deep LLM error: {llm_e}")
    except Exception as e:
        print(f"Deep learn error: {e}")

def _fetch_trading_intel():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        if r.status_code == 200:
            trending = []
            for c in r.json().get("coins", [])[:7]:
                item = c.get("item", {})
                trending.append({"name": item.get("name",""), "symbol": item.get("symbol",""), "source": "coingecko"})
            brain["trading"]["market_insights"].append({
                "timestamp": datetime.utcnow().isoformat(),
                "observation": f"Trending: {[t['symbol'] for t in trending]}",
                "mood": "TRENDING", "data": trending, "quality": "HIGH"
            })

        r2 = requests.get("https://api.geckoterminal.com/api/v2/networks/bsc/new_pools",
                          params={"page":1}, headers={"Accept":"application/json;version=20230302"}, timeout=10)
        if r2.status_code == 200:
            for pool in r2.json().get("data", [])[:10]:
                attr = pool.get("attributes", {})
                liq  = float(attr.get("reserve_in_usd", 0) or 0)
                rel  = pool.get("relationships", {})
                tid  = rel.get("base_token", {}).get("data", {}).get("id", "")
                addr = tid.replace("bsc_", "") if tid else ""
                if addr and liq > 2000:
                    threading.Thread(target=_process_new_token, args=(addr, addr, "GeckoTerminal"), daemon=True).start()
        brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-200:]
    except Exception as e:
        print(f"⚠️ Trading intel error: {e}")

def _learn_from_internet_data():
    try:
        recent = [i for i in brain["trading"]["market_insights"][-10:] if i.get("quality") == "HIGH"]
        trending_symbols = []
        for ins in recent:
            trending_symbols.extend([d.get("symbol","") for d in ins.get("data",[])])
        if trending_symbols:
            summary = f"Trending={trending_symbols[:3]}, F&G={market_cache.get('fear_greed',50)}"
            prompt = (f"BSC bot insights: {summary}. 2 actionable JSON: "
                      f'[{{"insight":"...","action":"...","confidence":0-100}}]')
            try:
                client = _get_freeflow_client()
                response = client.chat(model=MODEL_FAST,
                                       messages=[{"role":"system","content":"JSON only."},
                                                 {"role":"user","content":prompt}],
                                       max_tokens=200)
                reply = response if isinstance(response, str) else (
                    response.choices[0].message.content if hasattr(response,"choices") else str(response))
                _raw = reply.strip().replace("```json","").replace("```","").strip()
                _raw = re.sub(r',\s*([}\]])', r'', _raw)
                _match = re.search(r'\[.*\]', _raw, re.DOTALL)
                if _match:
                    insights = json.loads(_match.group(0))
                    if isinstance(insights, list):
                        for item in insights:
                            if isinstance(item, dict) and item.get("insight"):
                                brain["trading"]["strategy_notes"].append({
                                    "note": f"[INTERNET-INSIGHT] {item['insight']}",
                                    "timestamp": datetime.utcnow().isoformat()
                                })
            except Exception as le:
                print(f"⚠️ Internet LLM error: {le}")
    except Exception as e:
        print(f"⚠️ Internet learning error: {e}")

def fetch_internet_data_24x7():
    print("🌐 Internet Data Engine started!")
    time.sleep(45)
    while True:
        try:
            _fetch_trading_intel()
            _learn_from_internet_data()
            threading.Thread(target=_save_brain_to_db, daemon=True).start()
        except Exception as e:
            print(f"⚠️ Internet engine error: {e}")
        time.sleep(1800)

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
                send_telegram(f"🏆 <b>MILESTONE!</b>\n{title}")
        self_awareness["growth_tracking"]["milestones"] = milestones
    except Exception as e:
        print(f"Milestone error: {e}")

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
            if now - last_fast >= 60:
                last_fast = now
            try:
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
            if now - last_deep >= 900:
                last_deep = now
                _deep_llm_learning()
                update_self_awareness()
                _save_brain_to_db()
                print(f"📚 Cycle #{cycle} | W:{len(brain['trading']['best_patterns'])} L:{len(brain['trading']['avoid_patterns'])}")
            if now - last_hour >= 3600:
                last_hour = now
                _check_milestones()
                send_telegram(
                    f"MrBlack Report #{cycle}\n"
                    f"IQ:{self_awareness['performance_intelligence']['trading_iq']}/100\n"
                    f"BNB:${market_cache.get('bnb_price',0):.0f} F&G:{market_cache.get('fear_greed',50)}"
                )
        except Exception as e:
            print(f"Learning cycle error: {e}")
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
    if len(feedback_log) > 500: feedback_log.pop(0)

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
        time.sleep(3600)

# ========== AUTO CHECK NEW PAIR ==========
def _auto_check_new_pair(pair_address: str):
    print(f"⏳ Waiting 3 min: {pair_address[:10]}")
    time.sleep(180)
    try:
        _ar = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{pair_address}", timeout=8)
        if _ar.status_code == 200:
            _bp = [p for p in (_ar.json() or {}).get("pairs",[]) or [] if p and p.get("chainId")=="bsc"]
            if _bp:
                _ct = _bp[0].get("pairCreatedAt", 0) or 0
                if _ct and (time.time() - _ct/1000)/60 > 10080:
                    return
    except Exception: pass

    result  = run_full_sniper_checklist(pair_address)
    score   = result.get("score", 0)
    total   = result.get("total", 1)
    rec     = result.get("recommendation", "")
    overall = result.get("overall", "UNKNOWN")
    print(f"🔍 Auto-check {pair_address[:10]}: {overall} ({score}/{total})")
    print(f"📊 Score: {score}/{total} = {round(score/max(total,1)*100)}% | SAFE needs:{int(total*0.40)} CAUTION needs:{int(total*0.35)}")  # FIX3: debug

    if overall in ["SAFE", "CAUTION"]:
        telegram_new_token_alert(pair_address, score, total, rec)
    if overall == "SAFE" and score >= int(total * 0.40):  # FIX2: 50%→40%
        try: _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e: print(f"Auto buy error: {e}")
    elif overall == "CAUTION" and score >= int(total * 0.35):  # FIX2: 45%→35%  # FIX2: 40%
        try: _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e: print(f"Auto buy caution error: {e}")

    knowledge_base["bsc"]["new_tokens"].append({
        "address": pair_address, "overall": overall,
        "score": score, "total": total, "time": datetime.utcnow().isoformat()
    })
    knowledge_base["bsc"]["new_tokens"] = knowledge_base["bsc"]["new_tokens"][-20:]

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
    WSS_ENDPOINTS = [
        "wss://bsc.publicnode.com",
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc-ws-node.nariox.org:443",
        "wss://bsc.drpc.org",
    ]

    async def _listen(wss_url):
        try:
            async with _ws.connect(wss_url, ping_interval=15, ping_timeout=8, close_timeout=5, max_size=2**20) as ws:
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
            print(f"⚠️ WSS error: {str(e)[:50]}")

    async def _ws_loop():
        idx = 0
        fail_count = 0
        while True:
            try:
                print(f"🔌 WSS connecting: {WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)]}")
                await _listen(WSS_ENDPOINTS[idx % len(WSS_ENDPOINTS)])
                fail_count = 0
            except Exception as e:
                fail_count += 1
                wait = min(5 * fail_count, 60)
                print(f"⚠️ WSS loop fail #{fail_count}: {e} — retry in {wait}s")
                await asyncio.sleep(wait)
            idx += 1

    if _ws is not None:
        def _run_ws():
            try: asyncio.run(_ws_loop())
            except Exception as ex: print(f"⚠️ WSS thread: {ex}")
        threading.Thread(target=_run_ws, daemon=True).start()

    _cycle = 0
    while True:
        try:
            _cycle += 1
            _nc = time.time()
            # Cleanup old entries
            discovered_addresses_clean = {k: v for k, v in discovered_addresses.items() if _nc - v < DISCOVERY_TTL}
            discovered_addresses.clear()
            discovered_addresses.update(discovered_addresses_clean)

            try:
                rb = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
                if rb.status_code == 200:
                    boosts = rb.json() if isinstance(rb.json(), list) else []
                    for item in boosts[:20]:
                        if item.get("chainId") == "bsc":
                            addr = item.get("tokenAddress","")
                            if addr:
                                threading.Thread(target=_process_new_token, args=(addr, addr, "DexBoost"), daemon=True).start()
            except Exception: pass

            queries = ["new","moon","pepe","meme","inu","doge","safe","baby","elon","based"]
            q = queries[_cycle % len(queries)]
            try:
                rs = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={q}", timeout=10)
                if rs.status_code == 200:
                    for p in (rs.json() or {}).get("pairs",[]) or []:
                        if p and p.get("chainId") == "bsc":
                            addr = (p.get("baseToken") or {}).get("address","")
                            if addr:
                                threading.Thread(target=_process_new_token, args=(addr, p.get("pairAddress",""), "DexSearch"), daemon=True).start()
            except Exception: pass
        except Exception as e:
            print(f"⚠️ Fallback error: {e}")
        time.sleep(300)

# ========== 13-STAGE CHECKLIST ==========
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
    bscscan_source = "verified" if _gp_str(goplus_data, "is_open_source", "0") == "1" else ""

    dex_data = get_dexscreener_token_data(address)
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

    bnb_price = market_cache.get("bnb_price", 300) or 300
    liq_bnb   = liq_usd / bnb_price

    buy_tax  = _gp_float(goplus_data, "buy_tax")  * 100
    sell_tax = _gp_float(goplus_data, "sell_tax") * 100
    hidden   = _gp_bool_flag(goplus_data, "can_take_back_ownership") or _gp_bool_flag(goplus_data, "hidden_owner")
    transfer = not _gp_bool_flag(goplus_data, "transfer_pausable")

    add("Liquidity ≥ 1 BNB",       "pass" if liq_bnb > 2    else ("warn" if liq_bnb > 0.5 else "fail"), f"{liq_bnb:.2f} BNB", 1)
    add("Liquidity Locked",         "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 1)
    add("Buy Tax ≤ 8%",             "pass" if buy_tax <= 8   else "fail", f"{buy_tax:.1f}%",  1)
    add("Sell Tax ≤ 8%",            "pass" if sell_tax <= 8  else "fail", f"{sell_tax:.1f}%", 1)
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

    add("Top Holder < 7%",          "pass" if top_holder < 7  else ("warn" if top_holder < 15  else "fail"), f"{top_holder:.1f}%", 1)
    add("Top 10 Holders < 40%",     "pass" if top10_pct < 40  else ("warn" if top10_pct < 50   else "fail"), f"{top10_pct:.1f}%",  1)
    add("No Suspicious Clustering", "pass" if not suspicious   else "fail", "CLEAN" if not suspicious else "RISK", 1)
    add("Dev Wallet Not Dumping",   "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 1)

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

    add("Token Age ≥ 3 Min", "pass" if token_age_min >= 3 else "warn", f"{token_age_min:.0f} min" if token_age_min > 0 else "Unknown", 3)
    add("Sniper Pump Over",  "pass" if token_age_min >= 5 else "warn", "OK" if token_age_min >= 5 else "WAIT", 3)

    buys_5m = dex_data.get("buys_5m", 0); sells_5m = dex_data.get("sells_5m", 0)
    buys_1h = dex_data.get("buys_1h", 0); sells_1h = dex_data.get("sells_1h", 0)

    add("Buy > Sell (5min)", "pass" if buys_5m > sells_5m else "warn", f"B:{buys_5m} S:{sells_5m}", 4)
    add("Buy > Sell (1hr)",  "pass" if buys_1h > sells_1h else "warn", f"B:{buys_1h} S:{sells_1h}", 4)
    add("Volume 24h",        "pass" if dex_data.get("volume_24h",0) > 1000 else "warn", f"${dex_data.get('volume_24h',0):,.0f}", 4)

    add("1st Entry 0.002-0.005 BNB", "pass", "Follow Rule", 5)
    add("Max Position ≤ 3%",         "pass", "2-3% Balance", 5)
    add("Max 3-4 Entries/Token",     "pass", "No Chasing", 5)

    in_dex     = _gp_bool_flag(goplus_data, "is_in_dex")
    pool_count = len(dex_list) if isinstance(dex_list, list) else 0
    change_1h  = dex_data.get("change_1h", 0)

    add("Listed on DEX",         "pass" if in_dex     else "fail",  "YES" if in_dex else "NO", 6)
    add("DEX Pools",             "pass" if pool_count > 0 else "warn", f"{pool_count} pools", 6)
    add("1h Price Change",       "pass" if change_1h > 0  else "warn", f"{change_1h:+.1f}%",  6)
    add("Vol -50% → Exit 50%",   "pass", "Rule Active", 6)
    add("Vol -90% → Exit Fully", "pass", "Rule Active", 6)

    owner_pct = _gp_float(goplus_data, "owner_percent") * 100

    add("Dev/Creator < 5%",     "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 7)
    add("Owner Wallet < 5%",    "pass" if owner_pct < 5    else ("warn" if owner_pct < 15   else "fail"), f"{owner_pct:.1f}%",   7)
    add("Whale Conc. OK",       "pass" if top10_pct < 45   else "fail",  f"{top10_pct:.1f}% top10",       7)
    add("Dev Sell → Exit Rule", "pass", "Telegram Alert Active", 7)

    lp_holders = int(_gp_str(goplus_data, "lp_holder_count", "0"))

    add("LP Lock > 80%",       "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 8)
    add("LP Holders Present",  "pass" if lp_holders > 0  else "warn", f"{lp_holders} LP holders", 8)
    add("LP Drop → Exit Rule", "pass", "Monitored", 8)

    low_tax       = buy_tax <= 5 and sell_tax <= 5
    fast_trade_ok = low_tax and liq_locked > 20 and not honeypot

    add("Low Tax Fast Trade",   "pass" if low_tax       else "warn", "FAST OK" if low_tax else f"{buy_tax:.0f}%+{sell_tax:.0f}%", 9)
    add("15-30% Target Viable", "pass" if fast_trade_ok else "warn", "YES" if fast_trade_ok else "CHECK CONDITIONS", 9)
    add("Capital Rotation",     "pass", "After target hit", 9)

    sl_text = "15-20% SL (New)" if token_age_min < 60 else ("20-25% SL (Hyped)" if token_age_min < 360 else "10-15% SL (Mature)")
    add("Stop Loss Level",      "pass", sl_text,          10)
    add("Price Monitor Active", "pass", "Auto alerts ON", 10)

    add("+20% → SL to Cost", "pass", "Rule Active", 11)
    add("+30% → Sell 25%",   "pass", "Rule Active", 11)
    add("+50% → Sell 25%",   "pass", "Rule Active", 11)
    add("+100% → Sell 25%",  "pass", "Rule Active", 11)
    add("+200% → Keep 10%",  "pass", "Rule Active", 11)

    add("Token Logged",       "pass", "Auto-saved", 12)
    add("Pattern DB Updated", "pass", "Active",     12)

    add("Paper Mode First",    "pass", "Golden Rule", 13)
    add("70% WR Before Real",  "pass", "Discipline",  13)
    add("30+ Trades Required", "pass", "Before Real", 13)

    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    failed = sum(1 for c in result["checklist"] if c["status"] == "fail")
    total  = len(result["checklist"])
    pct    = round((passed / total) * 100) if total > 0 else 0

    result["score"] = passed
    result["total"] = total

    critical_fails = [c for c in result["checklist"] if c["status"] == "fail" and c["label"] in [
        "Honeypot Safe", "Buy Tax ≤ 8%", "Sell Tax ≤ 8%",
        "No Hidden Functions", "Transfer Allowed", "Mint Authority Disabled", "Liquidity ≥ 1 BNB"
    ]]
    if goplus_empty:
        critical_fails = [c for c in result["checklist"] if c["status"] == "fail" and c["label"] == "Honeypot Safe"]

    if critical_fails or honeypot:
        result["overall"]        = "DANGER"
        result["recommendation"] = "❌ SKIP — Critical fail. Do NOT buy."
    elif failed >= 8 or pct < 35:
        result["overall"]        = "RISK"
        result["recommendation"] = "⚠️ HIGH RISK — Multiple issues. Skip or 0.001 BNB test max."
    elif pct >= 55:
        result["overall"]        = "SAFE"
        result["recommendation"] = "✅ LOOKS SAFE — Start PAPER. Follow Stage 2 + 3 rules."
    else:
        result["overall"]        = "CAUTION"
        result["recommendation"] = "⚠️ CAUTION — Some issues. 0.001 BNB test only."

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
    sess["pattern_database"].append(lesson)
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
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-20:]]

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
_startup_done = False
_startup_lock = threading.Lock()

def _startup_once():
    # BUG FIX 2: @app.before_request hata diya
    # Render gunicorn startup pe port nahi milta tha — ab eager call se fix
    global _startup_done
    if _startup_done: return
    with _startup_lock:
        if _startup_done: return
        _startup_done = True
        try:
            _load_user_profile()
            print(f"✅ Profile: {user_profile.get('name')}")
        except Exception as e:
            print(f"⚠️ Profile error: {e}")
        try:
            _load_brain_from_db()
            _ensure_brain_structure()
            print(f"✅ Brain: cycles={brain.get('total_learning_cycles',0)}")
        except Exception as e:
            print(f"⚠️ Brain error: {e}")
        import time as _time
        def _delayed(fn, delay):
            def _wrap():
                _time.sleep(delay)
                fn()
            return _wrap
        threading.Thread(target=fetch_market_data,                    daemon=True).start()

        # BNB price verify + retry
        import time as _st
        _st.sleep(8)
        if market_cache.get("bnb_price", 0) == 0:
            print("⚠️ BNB price not loaded, retrying...")
            threading.Thread(target=fetch_market_data, daemon=True).start()
            _st.sleep(8)

        threading.Thread(target=_delayed(poll_new_pairs,        10),  daemon=True).start()
        threading.Thread(target=_delayed(price_monitor_loop,    15),  daemon=True).start()
        threading.Thread(target=_delayed(track_smart_wallets,   20),  daemon=True).start()
        threading.Thread(target=_delayed(continuous_learning,   25),  daemon=True).start()
        threading.Thread(target=_delayed(auto_position_manager, 30),  daemon=True).start()
        threading.Thread(target=_delayed(self_awareness_loop,   35),  daemon=True).start()
        threading.Thread(target=_delayed(fetch_internet_data_24x7, 45), daemon=True).start()
        threading.Thread(target=_delayed(feedback_validation_loop, 50), daemon=True).start()
        print("✅ All background threads started")

@app.route("/")
def home():
    return render_template("index.html")

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
    if request.method == "POST":
        session_id = (request.get_json() or {}).get("session_id", "default")
    else:
        session_id = request.args.get("session_id", "default")
    sess      = get_or_create_session(session_id)
    bnb_price = market_cache.get("bnb_price", 0)
    _auto_sess_td = get_or_create_session(AUTO_SESSION_ID)
    return jsonify({
        "paper":          f"{_auto_sess_td.get('paper_balance', 5.0):.4f}",
        "real":           f"{sess.get('real_balance', 0):.3f}",
        "pnl":            f"+{sess.get('pnl_24h', 0):.1f}%",
        "bnb_price":      bnb_price,
        "fear_greed":     market_cache.get("fear_greed", 50),
        "positions":      sess.get("positions", []),
        "trade_count":    sess.get("trade_count", 0),
        "win_rate":       round((sess.get("win_count",0)/sess.get("trade_count",1)*100),1) if sess.get("trade_count",0) > 0 else 0,
        "daily_loss":     round(sess.get("daily_loss", 0), 2),
        "limit_reached":  sess.get("daily_loss", 0) >= (sess.get("paper_balance", 5.0) * 0.15),
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
    reply = get_llm_reply(user_msg, sess["history"], sess)
    sess["history"].append({"role": "assistant", "content": reply})
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
    return jsonify({"wallets": SMART_WALLETS, "count": len(SMART_WALLETS), "tracking": len(smart_wallet_snapshots)})

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
            "total_cycles":     brain["total_learning_cycles"],
        },
    })

@app.route("/introspect", methods=["GET"])
def introspect():
    observation = self_introspect()
    return jsonify({"status": "ok", "observation": observation})

@app.route("/auto-stats", methods=["GET"])
def auto_stats_route():
    sess = get_or_create_session(AUTO_SESSION_ID)
    positions_info = {}
    for k, v in auto_trade_stats["running_positions"].items():
        entry   = v.get("entry", 0)
        current = monitored_positions.get(k, {}).get("current", entry)
        pnl     = ((current - entry) / entry * 100) if entry > 0 else 0
        # FIX 6: No duplicate keys, bought_at added
        positions_info[k] = {
            "token":     v.get("token", k[:8]),
            "address":   k,
            "pnl_pct":   round(pnl, 2),
            "size":      v.get("size_bnb"),
            "entry":     f"${entry:.10f}",
            "current":   f"${current:.10f}",
            "mcap":      "MCap ?",
            "age":       "Active",
            "bought_at": v.get("bought_at", ""),  # FIX: added
        }
    # FIX: trade_count from both session AND trade_history (more accurate)
    _th_total = len(auto_trade_stats.get("trade_history", []))
    _th_wins  = sum(1 for t in auto_trade_stats.get("trade_history", []) if t.get("result") == "win")
    _tc = max(sess.get("trade_count", 0), _th_total)
    _wc = max(sess.get("win_count",   0), _th_wins)
    return jsonify({
        "enabled":        AUTO_TRADE_ENABLED,
        "open_positions": len(auto_trade_stats["running_positions"]),
        "positions":      positions_info,
        "total_buys":     auto_trade_stats["total_auto_buys"],
        "total_sells":    auto_trade_stats["total_auto_sells"],
        "total_pnl_pct":  round(auto_trade_stats["auto_pnl_total"], 2),
        "paper_balance":  sess.get("paper_balance", 5.0),
        "trade_count":    _tc,
        "win_rate":       round(_wc/max(_tc,1)*100, 1),
        "win_count":      _wc,
        "wins":           _th_wins,
        "losses":         max(0, _th_total - _th_wins),
        "last_action":    auto_trade_stats["last_action"],
        "total_scanned":  max(len(discovered_addresses), brain.get("total_tokens_discovered_ever", 0)),
        "monitoring":     len(monitored_positions),
        "bnb_price":      market_cache.get("bnb_price", 0),
        "open_trades": [
            {
                "token":     v.get("token", k[:8]),
                "address":   k,
                "entry":     f"${v.get('entry', 0):.10f}",
                "current":   f"${monitored_positions.get(k,{}).get('current',v.get('entry',0)):.10f}",
                "pnl":       round(((monitored_positions.get(k,{}).get('current',v.get('entry',0))-v.get('entry',0))/max(v.get('entry',0),1e-18))*100, 2),
                "size":      f"{v.get('size_bnb',0):.4f} BNB",
                "age":       "Active",
                "bought_at": v.get("bought_at",""),
            }
            for k, v in auto_trade_stats.get("running_positions", {}).items()
        ],
        "trade_history": auto_trade_stats.get("trade_history", [])[-20:],
    })

@app.route("/health")
def health():
    return jsonify({
        "status":        "ok",
        "bsc_connected": w3.is_connected(),
        "supabase":      supabase is not None,
        "bnb_price":     market_cache.get("bnb_price", 0),
        "fear_greed":    market_cache.get("fear_greed", 50),
        "new_pairs":     len(new_pairs_queue),
        "monitoring":    len(monitored_positions),
        "telegram":      bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "last_update":   market_cache.get("last_updated")
    })

# BUG FIX 3: Eager startup — Render port detect kar sake
_startup_once()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
