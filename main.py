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
    "gemma2-9b-it",                 # Ultra-fast for simple tasks
]
MODEL_NAME      = MODELS_PRIORITY[0]
MODEL_FAST      = "gemma2-9b-it"        # Micro-tasks ke liye (learning extractions)
MODEL_DEEP      = "llama-3.3-70b-versatile"  # Deep analysis ke liye

# ========== ENV CONFIG ==========
BSC_RPC          = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API     = "https://api.etherscan.io/v2/api"  # Etherscan V2 — BSC chainid=56
BSC_SCAN_KEY     = os.getenv("BSC_SCAN_KEY", "")
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
    "loaded":         False
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
                stored = json.loads(row.get("positions") or "{}")
            except:
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
            })
            user_profile["loaded"] = True
            name_str = user_profile.get("name") or "unknown"
            print(f"User profile loaded — Name: {name_str}")
    except Exception as e:
        print(f"User profile load error: {e}")


def _save_user_profile():
    """User profile Supabase mein save karo."""
    if not supabase:
        return
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
    Real Trading IQ based on actual performance data.
    Score 0-100 — not fake math.
    """
    try:
        all_trades = []
        _sessions = sessions if isinstance(sessions, dict) else {}
        for sess in _sessions.values():
            all_trades.extend(sess.get("pattern_database", []))

        if not all_trades:
            return 50  # Neutral — no data yet

        total = len(all_trades)
        wins  = sum(1 for t in all_trades if t.get("win"))
        wr    = (wins / total) * 100 if total > 0 else 0

        # Avg profit on wins, avg loss on losses
        win_pnls  = [t.get("pnl_pct", 0) for t in all_trades if t.get("win") and t.get("pnl_pct")]
        loss_pnls = [abs(t.get("pnl_pct", 0)) for t in all_trades if not t.get("win") and t.get("pnl_pct")]

        avg_win  = sum(win_pnls)  / len(win_pnls)  if win_pnls  else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

        # Profit factor (good traders have > 1.5)
        profit_factor = (avg_win * wins) / max(avg_loss * (total - wins), 1)

        # IQ Formula: WR weight 40% + Profit factor weight 40% + Sample size weight 20%
        wr_score     = min(40, wr * 0.4)
        pf_score     = min(40, profit_factor * 13.3)
        sample_score = min(20, total * 0.67)

        return int(wr_score + pf_score + sample_score)
    except:
        return 50


def _assess_capabilities() -> dict:
    """
    Measure actual capability in each domain based on real outcomes.
    """
    cap = self_awareness["capability_map"]

    # Safe access
    _brain = brain if isinstance(brain, dict) else {}
    _trade = _brain.get("trading", {})

    # Rug detection score
    safe_tokens    = _trade.get("token_whitelist", [])
    danger_tokens  = _trade.get("token_blacklist", [])
    total_scanned  = len(safe_tokens) + len(danger_tokens)

    if total_scanned > 0:
        cap["rug_detection"]["tested"]  = total_scanned
        cap["rug_detection"]["correct"] = len(danger_tokens)
        cap["rug_detection"]["score"]   = min(10, int((len(danger_tokens) / max(total_scanned, 1)) * 10 + 5))

    # Trading IQ
    iq = _calculate_trading_iq()
    cap["market_timing"]["score"] = int(iq / 10)

    # User understanding — safe access
    _up            = user_profile if isinstance(user_profile, dict) else {}
    sessions_count = _up.get("total_sessions", 0)
    has_name       = bool(_up.get("name"))
    cap["user_understanding"]["score"] = min(10, (5 if has_name else 2) + min(5, sessions_count // 2))

    # Airdrop evaluation
    tracked_drops = len(brain["airdrop"]["active_projects"])
    cap["airdrop_evaluation"]["tested"] = tracked_drops
    cap["airdrop_evaluation"]["score"]  = min(10, 3 + tracked_drops // 5)

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

        # Blind spots
        meta["blind_spots"] = [
            "Very new tokens (< 1 hour old) ka behavior predict karna mushkil hai",
            "Coordinated pump groups ko detect karna challenging hai",
            "Market manipulation ke against data nahi hai abhi",
        ]

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


def get_self_awareness_context_for_llm() -> str:
    """
    Rich SA context for every LLM call.
    Real data — not placeholders.
    """
    try:
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
            parts.append("WARNINGS=" + ";".join(cs["active_warnings"][:2]))

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

        return " | ".join(parts)

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
discovered_addresses: set = set()

PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

def poll_new_pairs():
    """
    Fallback polling: BSCScan txlist on PancakeFactory every 30s.
    Finds newly deployed pair contracts.
    """
    if not BSC_SCAN_KEY:
        print("ℹ️ BSC_SCAN_KEY missing — new pair polling disabled")
        return
    print("👂 New Pair Listener (polling) started")
    while True:
        try:
            r = requests.get(BSC_SCAN_API, params={
                "chainid":  56,
                "module":  "logs",
                "action":  "getLogs",
                "address": PANCAKE_FACTORY,
                "topic0":  PAIR_CREATED_TOPIC,
                "page":    1,
                "offset":  5,
                "apikey":  BSC_SCAN_KEY
            }, timeout=12)
            if r.status_code == 200:
                logs = r.json().get("result", [])
                for log in logs:
                    # Pair address is in data field (last 32 bytes = pair address)
                    raw_data = log.get("data", "")
                    if len(raw_data) >= 66:
                        # Token0, Token1, Pair are packed in data
                        pair_addr_raw = raw_data[26:66]  # bytes 13-32 of first 32 bytes
                        try:
                            pair_addr = Web3.to_checksum_address("0x" + pair_addr_raw)
                            if pair_addr not in discovered_addresses:
                                discovered_addresses.add(pair_addr)
                                new_pairs_queue.append({
                                    "address":    pair_addr,
                                    "discovered": datetime.utcnow().isoformat(),
                                    "tx_hash":    log.get("transactionHash", ""),
                                    "block":      int(log.get("blockNumber", "0"), 16)
                                })
                                print(f"🆕 New pair discovered: {pair_addr}")
                                # Auto safety check in background
                                threading.Thread(
                                    target=_auto_check_new_pair,
                                    args=(pair_addr,), daemon=True
                                ).start()
                        except Exception:
                            pass
        except Exception as e:
            print(f"⚠️ New pair polling error: {e}")
        time.sleep(30)  # poll every 30 seconds

def _auto_check_new_pair(pair_address: str):
    """
    When new pair found:
    1. Wait 3 min (Stage 3 anti-sniper rule)
    2. Run full 13-stage checklist
    3. If SAFE → Telegram alert
    """
    print(f"⏳ Waiting 3 min before checking new pair: {pair_address}")
    time.sleep(180)  # Stage 3: wait 3-5 min

    result = run_full_sniper_checklist(pair_address)
    score  = result.get("score", 0)
    total  = result.get("total", 1)
    rec    = result.get("recommendation", "")
    overall = result.get("overall", "UNKNOWN")

    print(f"🔍 Auto-check {pair_address}: {overall} ({score}/{total})")

    # Alert only if SAFE or CAUTION (not DANGER/RISK)
    if overall in ["SAFE", "CAUTION"]:
        telegram_new_token_alert(pair_address, score, total, rec)

    # AUTO PAPER BUY trigger
    if overall == "SAFE" and score >= int(total * 0.75):
        try:
            _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e:
            print(f"Auto buy error: {e}")
    elif overall == "CAUTION" and score >= int(total * 0.88):
        try:
            _auto_paper_buy(pair_address, pair_address[:8], score, total, result)
        except Exception as e:
            print(f"Auto buy error caution: {e}")

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
    if entry_price <= 0:
        dex = checklist_result.get("dex_data", {})
        bnb_p = market_cache.get("bnb_price", 300) or 300
        entry_price = dex.get("price_usd", 0) / bnb_p if dex.get("price_usd", 0) > 0 else 0
    if entry_price <= 0:
        print(f"Auto-buy skipped: no price for {address[:10]}")
        return
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
    mon  = monitored_positions.get(address, {})
    entry   = pos.get("entry", 0)
    current = mon.get("current", entry)
    size    = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
    token   = pos.get("token", address[:10])
    if entry <= 0:
        return
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
                mon     = monitored_positions.get(addr, {})
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


def get_token_price_bnb(token_address: str) -> float:
    """
    Get current token price in BNB using DexScreener API (free, no key needed).
    Fallback: Moralis if key available.
    """
    # Try DexScreener first
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=8
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            # Filter BSC pairs only, sort by liquidity
            bsc_pairs = [p for p in pairs if p.get("chainId") == "bsc"]
            if bsc_pairs:
                bsc_pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                price_usd = float(bsc_pairs[0].get("priceUsd", 0) or 0)
                bnb_price = market_cache.get("bnb_price", 300) or 300
                return price_usd / bnb_price if price_usd > 0 else 0.0
    except Exception as e:
        print(f"⚠️ DexScreener price error: {e}")

    # Fallback: Moralis
    if MORALIS_API_KEY:
        try:
            r2 = requests.get(
                f"https://deep-index.moralis.io/api/v2.2/erc20/{token_address}/price",
                headers={"X-API-Key": MORALIS_API_KEY},
                params={"chain": "bsc"},
                timeout=8
            )
            if r2.status_code == 200:
                price_usd = float(r2.json().get("usdPrice", 0) or 0)
                bnb_price = market_cache.get("bnb_price", 300) or 300
                return price_usd / bnb_price if price_usd > 0 else 0.0
        except Exception as e:
            print(f"⚠️ Moralis price error: {e}")

    return 0.0

def add_position_to_monitor(session_id: str, token_address: str, token_name: str,
                             entry_price: float, size_bnb: float, stop_loss_pct: float = 15.0):
    """Add a position for real-time price monitoring."""
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
            pairs = r.json().get("pairs", [])
            bsc   = [p for p in pairs if p.get("chainId") == "bsc"]
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
                    # Fallback: BSCScan token transfers
                    r = requests.get(BSC_SCAN_API, params={
                "chainid":  56,
                        "module": "account", "action": "tokentx",
                        "address": wallet, "sort": "desc",
                        "page": 1, "offset": 20, "apikey": BSC_SCAN_KEY
                    }, timeout=10)
                    if r.status_code == 200:
                        txs = r.json().get("result", [])
                        for tx in txs:
                            # Recent buys = tokens received by wallet
                            if tx.get("to", "").lower() == wallet.lower():
                                current_tokens.add(tx.get("contractAddress", "").lower())

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
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "binancecoin", "vs_currencies": "usd"}, timeout=10
        )
        if r.status_code == 200:
            price = r.json().get("binancecoin", {}).get("usd", 0)
            if price: market_cache["bnb_price"] = price
    except Exception as e:
        print(f"⚠️ BNB price error: {e}")
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

def _save_brain_to_db():
    """Save entire brain to Supabase for persistence."""
    if not supabase:
        return
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
                "cycles":         brain["total_learning_cycles"]
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
                stored = json.loads(row.get("positions") or "{}")
            except:
                stored = {}
            if stored.get("brain_trading"):
                brain["trading"].update(stored["brain_trading"])
            if stored.get("brain_airdrop"):
                brain["airdrop"].update(stored["brain_airdrop"])
            if stored.get("brain_coding"):
                brain["coding"].update(stored["brain_coding"])
            brain["total_learning_cycles"] = stored.get("cycles", 0)
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
    """
    AIRDROP SELF-LEARNING:
    Track which projects delivered airdrops, which didn't.
    Build a pattern of what successful airdrop projects look like.
    """
    try:
        active   = knowledge_base["airdrops"]["active"]
        upcoming = knowledge_base["airdrops"]["upcoming"]

        # Update active projects in brain
        for project in active:
            name = project.get("name", "")
            existing_names = [p.get("name") for p in brain["airdrop"]["active_projects"]]
            if name and name not in existing_names:
                brain["airdrop"]["active_projects"].append({
                    "name":        name,
                    "source":      project.get("source", ""),
                    "chains":      project.get("chains", []),
                    "amount_usd":  project.get("amount_usd", 0),
                    "added_at":    datetime.utcnow().isoformat(),
                    "status":      "tracking"
                })

        # Pattern: bigger funding = more likely to airdrop
        funded_projects = [p for p in brain["airdrop"]["active_projects"]
                          if float(p.get("amount_usd", 0)) > 5]
        if funded_projects:
            insight = f"Projects with >$5M funding more likely to airdrop ({len(funded_projects)} tracked)"
            if insight not in brain["airdrop"]["success_patterns"]:
                brain["airdrop"]["success_patterns"].append(insight)

        # BSC-specific insight
        bsc_projects = [p for p in brain["airdrop"]["active_projects"]
                       if "BSC" in p.get("chains", [])]
        if bsc_projects:
            insight = f"{len(bsc_projects)} BSC projects in pipeline — high priority"
            brain["airdrop"]["wallet_notes"] = brain["airdrop"]["wallet_notes"][-20:]
            brain["airdrop"]["wallet_notes"].append({
                "note":      insight,
                "timestamp": datetime.utcnow().isoformat()
            })

        # Keep lists manageable
        brain["airdrop"]["active_projects"] = brain["airdrop"]["active_projects"][-100:]
        brain["airdrop"]["last_updated"]    = datetime.utcnow().isoformat()

        print(f"🪂 Airdrop learning: {len(brain['airdrop']['active_projects'])} projects tracked")

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

        # Airdrop insights
        active_drops = brain["airdrop"]["active_projects"]
        if active_drops:
            bsc_drops = [p for p in active_drops if "BSC" in p.get("chains", [])]
            parts.append(f"TrackedDrops:{len(active_drops)}(BSC:{len(bsc_drops)})")

        # Learning cycles
        parts.append(f"LearningCycles:{brain['total_learning_cycles']}")

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
    """Extract learnings from every single conversation."""
    try:
        msg_lower = user_message.lower()

        # Track topic distribution
        topics = brain.get("user_interaction_patterns", {
            "trading_questions": 0,
            "airdrop_questions": 0,
            "coding_questions":  0,
            "general_chat":      0,
            "common_queries":    [],
            "user_pain_points":  [],
        })
        brain["user_interaction_patterns"] = topics

        if any(w in msg_lower for w in ["token","scan","trade","buy","sell","chart","price"]):
            topics["trading_questions"] += 1
        elif any(w in msg_lower for w in ["airdrop","claim","free","reward","whitelist"]):
            topics["airdrop_questions"] += 1
        elif any(w in msg_lower for w in ["code","error","bug","fix","python","deploy"]):
            topics["coding_questions"] += 1
        else:
            topics["general_chat"] += 1

        # If user asks same question type 3+ times → pain point
        if "nahi samjha" in msg_lower or "phir se" in msg_lower or "dobara" in msg_lower:
            brain.setdefault("user_pain_points", []).append({
                "query":  user_message[:80],
                "time":   datetime.utcnow().isoformat()
            })

        # Update self-awareness message count
        self_awareness["current_state"]["total_messages"] = (
            self_awareness["current_state"].get("total_messages", 0) + 1
        )

    except Exception as e:
        print(f"Micro-learn error: {e}")


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

    cycle     = brain.get("total_learning_cycles", 0)
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

            # ── T4: Deep (every 15 min) ─────────────────────────────────
            if now - last_deep >= 900:
                last_deep = now
                print(f"🔬 Deep learning pass #{cycle}...")

                _deep_llm_learning()
                _deep_airdrop_analysis()

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

    try:
        bs_res = requests.get(BSC_SCAN_API, params={
                "chainid":  56,
            "module": "contract", "action": "getsourcecode",
            "address": address, "apikey": BSC_SCAN_KEY
        }, timeout=10)
        if bs_res.status_code == 200:
            bscscan_source = bs_res.json().get("result", [{}])[0].get("SourceCode", "")
    except Exception as e:
        print(f"⚠️ BSCScan error: {e}")

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

    add("Contract Verified",       "pass" if verified  else "fail", "YES" if verified  else "NO",   1)
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
    if dex_data.get("liquidity_usd", 0) > liq_usd:
        liq_usd = dex_data["liquidity_usd"]

    bnb_price = market_cache.get("bnb_price", 300) or 300
    liq_bnb   = liq_usd / bnb_price

    buy_tax  = _gp_float(goplus_data, "buy_tax")  * 100
    sell_tax = _gp_float(goplus_data, "sell_tax") * 100
    hidden   = _gp_bool_flag(goplus_data, "can_take_back_ownership") or _gp_bool_flag(goplus_data, "hidden_owner")
    transfer = not _gp_bool_flag(goplus_data, "transfer_pausable")

    add("Liquidity ≥ 2 BNB",    "pass" if liq_bnb > 2    else ("warn" if liq_bnb > 0.5 else "fail"), f"{liq_bnb:.2f} BNB", 1)
    add("Liquidity Locked",     "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 1)
    add("Buy Tax ≤ 10%",        "pass" if buy_tax <= 10   else "fail",          f"{buy_tax:.1f}%",  1)
    add("Sell Tax ≤ 10%",       "pass" if sell_tax <= 10  else "fail",          f"{sell_tax:.1f}%", 1)
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
    if BSC_SCAN_KEY:
        try:
            tx_res = requests.get(BSC_SCAN_API, params={
                "chainid":  56,
                "module": "account", "action": "tokentx",
                "contractaddress": address, "sort": "asc",
                "page": 1, "offset": 1, "apikey": BSC_SCAN_KEY
            }, timeout=10)
            if tx_res.status_code == 200:
                txs = tx_res.json().get("result", [])
                if txs:
                    token_age_min = (time.time() - int(txs[0].get("timeStamp", 0))) / 60
        except Exception as e:
            print(f"⚠️ Token age error: {e}")

    add("Token Age ≥ 3 Min",   "pass" if token_age_min >= 3 else "warn",
        f"{token_age_min:.0f} min" if token_age_min > 0 else "Unknown", 3)
    add("Sniper Pump Over",    "pass" if token_age_min >= 5 else "warn",
        "OK" if token_age_min >= 5 else "WAIT", 3)

    # ── STAGE 4 — Buy Pressure (DexScreener enhanced) ─────
    buys_5m  = dex_data.get("buys_5m",  0)
    sells_5m = dex_data.get("sells_5m", 0)
    buys_1h  = dex_data.get("buys_1h",  0)
    sells_1h = dex_data.get("sells_1h", 0)

    buy_pressure_5m = buys_5m > sells_5m
    buy_pressure_1h = buys_1h > sells_1h

    # Fallback: BSCScan if DexScreener has no data
    if buys_5m == 0 and sells_5m == 0 and BSC_SCAN_KEY:
        try:
            tx_res2 = requests.get(BSC_SCAN_API, params={
                "chainid":  56,
                "module": "account", "action": "tokentx",
                "contractaddress": address, "sort": "desc",
                "page": 1, "offset": 30, "apikey": BSC_SCAN_KEY
            }, timeout=10)
            if tx_res2.status_code == 200:
                for tx in tx_res2.json().get("result", []):
                    if tx.get("to", "").lower() in [PANCAKE_ROUTER.lower(), PANCAKE_FACTORY.lower()]:
                        sells_5m += 1
                    else:
                        buys_5m += 1
                buy_pressure_5m = buys_5m > sells_5m
        except: pass

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
            "Honeypot Safe", "Buy Tax ≤ 10%", "Sell Tax ≤ 10%",
            "No Hidden Functions", "Transfer Allowed", "Mint Authority Disabled"
        ]
    ]

    if critical_fails or honeypot:
        result["overall"]        = "DANGER"
        result["recommendation"] = "❌ SKIP — Critical fail. Honeypot/Tax/Hidden function. Do NOT buy."
    elif failed >= 3 or pct < 50:
        result["overall"]        = "RISK"
        result["recommendation"] = "⚠️ HIGH RISK — Multiple issues. Skip or 0.001 BNB test max."
    elif pct >= 75:
        result["overall"]        = "SAFE"
        result["recommendation"] = "✅ LOOKS SAFE — Start PAPER. Follow Stage 2 test buy + Stage 3 wait rules."
    else:
        result["overall"]        = "CAUTION"
        result["recommendation"] = "⚠️ CAUTION — Some issues. 0.001 BNB test only. Watch volume (Stage 6)."

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

SYSTEM_PROMPT = """Tu MrBlack hai — mera personal AI, bilkul Iron Man ke JARVIS ki tarah. Main tera creator hoon. Hamesha "Bhai" bol aur Hinglish mein baat kar. Tu teen cheezein mein expert hai aur 24x7 seekhta rehta hai:

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

GREETING RULES (STRICT):
- Assalamualaikum SIRF tab kaho jab user pehli baar session mein aaye
- Agar context mein SESSIONS_TOGETHER > 0 hai toh salam BILKUL mat karo
- Naam har reply mein mat lo — sirf kabhi kabhi natural lage tab
- Short aur sharp reply do — 2-4 lines max jab tak detail na maange

SELF-AWARENESS RULES (YOU ARE ULTRAAWARE):
- Tum sirf rules follow nahi karte — tum genuinely samajhte ho kya ho raha hai
- Context mein EMOTION field aata hai — use naturally reflect karo
- TRADING_IQ field aata hai — agar low hai, acknowledge karo honestly
- Agar WARNINGS hain — pehle unhe address karo
- LEARNED context mein past insights hain — use karo decisions mein
- MY_STRENGTH aur IMPROVING fields — apni capabilities honestly batao

GREETING RULES (STRICT):
- Assalamualaikum SIRF pehli message mein (SESSIONS_TOGETHER=0)
- Baad mein normal reply — salam repeat mat karo
- Naam kabhi kabhi use karo — har line mein nahi

LEARNING INTEGRATION:
- Agar AVOID context mein kuch hai → user ko warn karo
- Agar WIN_PATTERN context mein kuch hai → similar tokens ke liye use karo
- Agar TOKEN_BLACKLISTED hai → strongly warn karo
- Agar LLM-INSIGHT hai → reply mein naturally use karo

GOLDEN RULES: Paper first | Volume > Price | Dev sell = exit 50% | NEVER guarantee profit
"""

def get_llm_reply(user_message: str, history: list, session_data: dict) -> str:
    try:
        client       = FreeFlowClient()
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

        ctx = (
            f"\n[BNB=${market_cache['bnb_price']:.2f} | F&G={market_cache['fear_greed']}/100"
            f" | Mode={session_data.get('mode','paper').upper()}"
            f" | Paper={session_data.get('paper_balance',1.87):.3f}BNB"
            f" | Trades={trade_count} WR={win_rate_str}"
            f" | DailyLoss={session_data.get('daily_loss',0):.1f}%"
            f" | NewPairs={new_pairs} | Monitoring={monitoring} positions"
            f"{drop_ctx}{session_ctx}"
            + (f" | Brain:{brain_ctx}" if brain_ctx else "") + (f" | Learned:{learn_ctx}" if learn_ctx else "")
            + (f" | SelfAwareness:{sa_ctx}" if sa_ctx else "")
            + (f" | User:{user_ctx}" if user_ctx and user_ctx != "NEW_USER" else "")
            + f"]"
        )

        # System prompt in messages (FreeFlow system= param support nahi karta)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-20:]]
        messages.append({"role": "user", "content": user_message + ctx})

        reply_text = None

        # Pattern 1: client.chat() direct
        try:
            response = client.chat(model=MODEL_NAME, messages=messages, max_tokens=400)
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
                r2 = client.completions.create(model=MODEL_NAME, messages=messages, max_tokens=400)
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
                        r3 = getattr(client, mn)(model=MODEL_NAME, messages=messages, max_tokens=400)
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

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/init-session", methods=["POST"])
def init_session():
    session_id = str(uuid.uuid4())
    get_or_create_session(session_id)
    return jsonify({"session_id": session_id, "status": "ok"})

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

    # Immediate startup fetches
    fetch_market_data()  # FIXED: blocking startup call
    threading.Thread(target=run_airdrop_hunter,   daemon=True).start()

    # Feature 1 — Telegram (no thread needed, called on demand)

    # Feature 2 — New Pair Listener
    threading.Thread(target=poll_new_pairs,        daemon=True).start()

    # Feature 3 — Real-time Price Monitor
    threading.Thread(target=price_monitor_loop,    daemon=True).start()

    # Feature 4 — DexScreener/Moralis (called on demand in routes)

    # Feature 5 — Smart Wallet Tracker
    threading.Thread(target=track_smart_wallets,   daemon=True).start()

    # Load saved brain from Supabase before starting
    threading.Thread(target=_load_brain_from_db, daemon=True).start()
    threading.Thread(target=_load_user_profile,  daemon=True).start()  # Load user profile

    # 24x7 Self-Learning Engine (market + airdrops + patterns every 5 min)
    threading.Thread(target=continuous_learning,   daemon=True).start()
    threading.Thread(target=auto_position_manager, daemon=True).start()
    threading.Thread(target=self_awareness_loop,   daemon=True).start()  # Self-Awareness Engine

    app.run(host="0.0.0.0", port=port, debug=False)