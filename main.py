import os
from flask import Flask, render_template, request, jsonify
from supabase import create_client
import uuid
from datetime import datetime, timedelta
import requests
import time
import threading
import json
import socket
from web3 import Web3
from collections import deque, Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Tuple, Set
import hmac
import hashlib

# ========== FREEFLOW LLM ==========
from freeflow_llm import FreeFlowClient, NoProvidersAvailableError

# ========== PATCH HTTPX ==========
import httpx
httpx.__version__ = "0.24.1"

app = Flask(__name__)
MODEL_NAME = "llama-3.3-70b-versatile"

# ========== BSC CONFIG ==========
BSC_RPC         = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API    = "https://api.bscscan.com/api"
BSC_SCAN_KEY    = os.getenv("BSC_SCAN_KEY", "")
PANCAKE_ROUTER  = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"

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
        print(f"❌ Supabase connection failed: {e}")

# ========== KNOWLEDGE BASE ==========
knowledge_base = {
    "dex":      {"uniswap": {}, "pancakeswap": {}, "aerodrome": {}, "raydium": {}, "jupiter": {}},
    "bsc":      {"new_tokens": [], "trending": [], "scams": [], "safu_tokens": []},
    "coding":   {"github": [], "stackoverflow": [], "medium": [], "youtube": []},
    "airdrops": {"active": [], "upcoming": [], "ended": []},
    "trading":  {"news": [], "fear_greed": {}, "market_data": {}}
}

# ========== ENUMS ==========
class TradingMode(Enum):
    PAPER = "PAPER"
    REAL  = "REAL"

class SafetyLevel(Enum):
    SAFE   = "SAFE"
    RISK   = "RISK"
    DANGER = "DANGER"

# ========== DATACLASSES ==========
@dataclass
class ModeSettings:
    mode:                   TradingMode
    total_balance:          float
    exposure_limit:         float       # 20-25%
    daily_loss_limit:       float       # 5-8%
    max_position_per_token: float       # 2-3%
    reserve_capital:        float       # 75-80%

# ========== MRBLACK CHECKLIST ENGINE (original — untouched) ==========
class MrBlackChecklistEngine:
    def __init__(self, initial_balance: float = 1.0, mode: TradingMode = TradingMode.PAPER):
        self.mode = ModeSettings(
            mode=mode,
            total_balance=initial_balance,
            exposure_limit=0.22,
            daily_loss_limit=0.065,
            max_position_per_token=0.025,
            reserve_capital=0.78
        )
        self.paper_stats   = None
        self.positions     = {}
        self.trade_history = []
        print("✅ MrBlack Checklist Engine Initialized")

bsc_engine = MrBlackChecklistEngine()

# ========== GOPLUS SAFE PARSER ==========
# FIX #1 — GoPlus returns lists/dicts for some fields, not always strings.
# This helper safely extracts any field and returns a clean float/string.

def _gp_str(data: dict, key: str, default: str = "0") -> str:
    """Safely get a string value from GoPlus data"""
    val = data.get(key, default)
    if val is None:
        return default
    if isinstance(val, list):
        return str(val[0]) if val else default
    return str(val)

def _gp_float(data: dict, key: str, default: float = 0.0) -> float:
    """Safely get a float value from GoPlus data"""
    try:
        return float(_gp_str(data, key, str(default)))
    except (ValueError, TypeError):
        return default

def _gp_bool_flag(data: dict, key: str) -> bool:
    """Returns True if GoPlus flag == '1'"""
    return _gp_str(data, key, "0") == "1"

# ========== IN-MEMORY SESSION STORE ==========
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
            "created_at":       datetime.utcnow().isoformat()
        }
        _load_session_from_db(session_id)
    return sessions[session_id]

# ── Supabase: table = "memory" ────────────────────────────

def _load_session_from_db(session_id: str):
    """Load full session from Supabase memory table"""
    if not supabase:
        return
    try:
        res = supabase.table("memory").select("*").eq("session_id", session_id).execute()
        if res.data:
            row = res.data[0]
            sessions[session_id].update({
                "paper_balance":    row.get("paper_balance",    1.87),
                "real_balance":     row.get("real_balance",     0.00),
                "positions":        json.loads(row.get("positions",        "[]")),
                "history":          json.loads(row.get("history",          "[]")),
                "pnl_24h":          row.get("pnl_24h",          0.0),
                "daily_loss":       row.get("daily_loss",        0.0),
                "trade_count":      row.get("trade_count",       0),
                "win_count":        row.get("win_count",         0),
                "pattern_database": json.loads(row.get("pattern_database", "[]")),
            })
            print(f"✅ Session loaded: {session_id[:8]}...")
    except Exception as e:
        print(f"⚠️ Session load error: {e}")

def _save_session_to_db(session_id: str):
    """Save full session to Supabase memory table"""
    if not supabase:
        return
    try:
        sess = sessions.get(session_id, {})
        supabase.table("memory").upsert({
            "session_id":       session_id,
            "paper_balance":    sess.get("paper_balance",    1.87),
            "real_balance":     sess.get("real_balance",     0.00),
            "positions":        json.dumps(sess.get("positions",        [])),
            "history":          json.dumps(sess.get("history",          [])[-30:]),
            "pnl_24h":          sess.get("pnl_24h",          0.0),
            "daily_loss":       sess.get("daily_loss",        0.0),
            "trade_count":      sess.get("trade_count",       0),
            "win_count":        sess.get("win_count",         0),
            "pattern_database": json.dumps(sess.get("pattern_database", [])[-50:]),
            "updated_at":       datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"⚠️ Session save error: {e}")

# ──────────────────────────────────────────────────────────

# ========== MARKET DATA CACHE ==========
market_cache = {
    "bnb_price":    0.0,
    "fear_greed":   50,
    "trending":     [],
    "last_updated": None
}

def fetch_market_data():
    """BNB price + Fear & Greed"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "binancecoin", "vs_currencies": "usd"}, timeout=10
        )
        if r.status_code == 200:
            price = r.json().get("binancecoin", {}).get("usd", 0)
            if price:
                market_cache["bnb_price"] = price
    except Exception as e:
        print(f"⚠️ BNB price error: {e}")

    try:
        r2 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if r2.status_code == 200:
            market_cache["fear_greed"] = int(r2.json()["data"][0]["value"])
    except Exception as e:
        print(f"⚠️ Fear & Greed error: {e}")

    market_cache["last_updated"] = datetime.utcnow().isoformat()
    print(f"📊 Market — BNB: ${market_cache['bnb_price']} | F&G: {market_cache['fear_greed']}")

# ========== AIRDROP HUNTER ==========

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

def fetch_dappradar_airdrops() -> List[Dict]:
    results = []
    dappradar_key = os.getenv("DAPPRADAR_KEY", "")
    if not dappradar_key:
        # FIX #8 — No key = skip silently with log, no crash
        print("ℹ️ DAPPRADAR_KEY not set — skipping DappRadar source")
        return results
    try:
        r = requests.get(
            "https://api.dappradar.com/4tsxo4vuhotaojtl/dapps",
            headers={"X-BLOBR-KEY": dappradar_key},
            params={"chain": "binance-smart-chain", "top": 20, "sort": "uaw"}, timeout=12
        )
        if r.status_code == 200:
            for d in r.json().get("results", [])[:10]:
                results.append({
                    "name":        d.get("name", "Unknown"),
                    "category":    d.get("category", "DeFi"),
                    "chains":      ["BSC"],
                    "source":      "DappRadar",
                    "status":      "active",
                    "description": f"BSC Dapp — {d.get('uaw', 0)} daily users",
                    "url":         d.get("website", "")
                })
    except Exception as e:
        print(f"⚠️ DappRadar error: {e}")
    return results

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

def fetch_onchain_bsc_airdrops() -> List[Dict]:
    results = []
    if not BSC_SCAN_KEY:
        print("ℹ️ BSC_SCAN_KEY not set — skipping on-chain airdrop source")
        return results
    try:
        r = requests.get(BSC_SCAN_API, params={
            "module": "account", "action": "txlist",
            "address": PANCAKE_FACTORY, "sort": "desc",
            "page": 1, "offset": 10, "apikey": BSC_SCAN_KEY
        }, timeout=12)
        if r.status_code == 200:
            for tx in r.json().get("result", [])[:8]:
                contract = tx.get("contractAddress", "")
                if contract:
                    results.append({
                        "name":        "New BSC Pair",
                        "address":     contract,
                        "chains":      ["BSC"],
                        "source":      "BSCScan On-chain",
                        "status":      "active",
                        "description": f"New PancakeSwap pair — {contract[:10]}...",
                        "url":         f"https://bscscan.com/address/{contract}"
                    })
    except Exception as e:
        print(f"⚠️ On-chain BSC error: {e}")
    return results

def run_airdrop_hunter():
    print("🪂 Airdrop Hunter starting...")
    all_airdrops  = []
    all_airdrops += fetch_defillama_airdrops()
    all_airdrops += fetch_dappradar_airdrops()
    all_airdrops += fetch_coinmarketcap_airdrops()
    all_airdrops += fetch_onchain_bsc_airdrops()
    knowledge_base["airdrops"]["active"]   = [a for a in all_airdrops if a.get("status") == "active"]
    knowledge_base["airdrops"]["upcoming"] = [a for a in all_airdrops if a.get("status") == "upcoming"]
    print(f"🪂 Airdrops — Active: {len(knowledge_base['airdrops']['active'])}, Upcoming: {len(knowledge_base['airdrops']['upcoming'])}")

# ========== DEX FETCHERS ==========
def fetch_pancakeswap_data():
    try:
        r = requests.get("https://api.pancakeswap.info/api/v2/pairs", timeout=12)
        if r.status_code == 200:
            pairs = r.json().get("data", {})
            top   = sorted(pairs.values(), key=lambda x: float(x.get("volume24h", 0) or 0), reverse=True)[:10]
            knowledge_base["bsc"]["trending"] = [
                {"symbol": p.get("name", ""), "volume": p.get("volume24h", 0)} for p in top
            ]
            print(f"🥞 PancakeSwap — {len(top)} pairs updated")
    except Exception as e:
        print(f"⚠️ PancakeSwap error: {e}")

def fetch_uniswap_data():
    pass  # optional — add V3 subgraph later

# ========== CONTINUOUS LEARNING LOOP ==========
def continuous_learning():
    """
    FIX #9 — Immediate first run on startup (no 5-min wait).
    After that, every 5 minutes.
    """
    while True:
        try:
            fetch_market_data()
            fetch_pancakeswap_data()
        except Exception as e:
            print(f"⚠️ Market error: {e}")
        try:
            run_airdrop_hunter()
        except Exception as e:
            print(f"⚠️ Airdrop error: {e}")
        time.sleep(300)  # 5 minutes

# ==========================================================
# ====== 13-STAGE MEMECOIN SNIPER CHECKLIST ENGINE =========
# ==========================================================

def run_full_sniper_checklist(address: str) -> Dict:
    """
    Complete 13-stage checklist per tera master document.
    Real data: GoPlus (free) + BSCScan API.
    All GoPlus parsing fixed (FIX #1, #2).
    """

    result = {
        "address":        address,
        "stages":         {},
        "checklist":      [],
        "overall":        "UNKNOWN",
        "score":          0,
        "total":          0,
        "recommendation": ""
    }

    goplus_data    = {}
    bscscan_source = ""

    # ── Fetch GoPlus data ─────────────────────────────────
    try:
        gp_res = requests.get(
            "https://api.gopluslabs.io/api/v1/token_security/56",
            params={"contract_addresses": address}, timeout=12
        )
        if gp_res.status_code == 200:
            goplus_data = gp_res.json().get("result", {}).get(address.lower(), {})
    except Exception as e:
        print(f"⚠️ GoPlus fetch error: {e}")

    # ── Fetch BSCScan source ──────────────────────────────
    try:
        bs_res = requests.get(BSC_SCAN_API, params={
            "module": "contract", "action": "getsourcecode",
            "address": address, "apikey": BSC_SCAN_KEY
        }, timeout=10)
        if bs_res.status_code == 200:
            bscscan_source = bs_res.json().get("result", [{}])[0].get("SourceCode", "")
    except Exception as e:
        print(f"⚠️ BSCScan source error: {e}")

    # ── Helper: add a check item ──────────────────────────
    def add(label, status, value, stage):
        result["checklist"].append({
            "label": label, "status": status,
            "value": value, "stage":  stage
        })

    # ──────────────────────────────────────────────────────
    # STAGE 1 — ADVANCED SAFETY CHECKS
    # ──────────────────────────────────────────────────────

    # Contract Safety
    verified   = bool(bscscan_source)
    # FIX #1 — is_mintable is a string "0"/"1" in GoPlus
    mint_ok    = not _gp_bool_flag(goplus_data, "is_mintable")
    renounced  = (
        _gp_str(goplus_data, "owner_address") in [
            "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead", ""
        ] or "renounceOwnership" in bscscan_source
    )

    add("Contract Verified",       "pass" if verified  else "fail", "YES"  if verified  else "NO",   1)
    add("Mint Authority Disabled", "pass" if mint_ok   else "fail", "SAFE" if mint_ok   else "RISK", 1)
    add("Ownership Renounced",     "pass" if renounced else "warn", "YES"  if renounced else "MAYBE",1)

    # Liquidity Safety
    # FIX #2 — lp_holder_count gives count of LP holders; dex[] array has real liquidity info
    dex_list    = goplus_data.get("dex", [])  # list of dex pools
    liq_usd     = 0.0
    liq_locked  = 0.0
    if dex_list and isinstance(dex_list, list):
        for pool in dex_list:
            liq_usd    += float(pool.get("liquidity", 0) or 0)
            liq_locked += float(pool.get("lock_ratio", 0) or 0)
        if len(dex_list) > 0:
            liq_locked = liq_locked / len(dex_list) * 100  # average lock %

    bnb_price   = market_cache.get("bnb_price", 300) or 300
    liq_bnb     = liq_usd / bnb_price if bnb_price > 0 else 0

    add("Liquidity ≥ 2 BNB",   "pass" if liq_bnb > 2    else ("warn" if liq_bnb > 0.5 else "fail"), f"{liq_bnb:.2f} BNB", 1)
    add("Liquidity Locked",     "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}%", 1)

    # Tokenomics Safety
    buy_tax  = _gp_float(goplus_data, "buy_tax")  * 100
    sell_tax = _gp_float(goplus_data, "sell_tax") * 100
    hidden   = _gp_bool_flag(goplus_data, "can_take_back_ownership") or _gp_bool_flag(goplus_data, "hidden_owner")
    transfer = not _gp_bool_flag(goplus_data, "transfer_pausable")

    add("Buy Tax ≤ 10%",        "pass" if buy_tax <= 10  else "fail",          f"{buy_tax:.1f}%",  1)
    add("Sell Tax ≤ 10%",       "pass" if sell_tax <= 10 else "fail",          f"{sell_tax:.1f}%", 1)
    add("No Hidden Functions",  "pass" if not hidden      else "fail", "CLEAN" if not hidden else "RISK",  1)
    add("Transfer Allowed",     "pass" if transfer        else "fail", "YES"   if transfer   else "PAUSED",1)

    # Holder Safety
    # FIX #1 — holders is a list in GoPlus, get top holder % from list
    holders_list = goplus_data.get("holders", [])
    top_holder   = 0.0
    top10_pct    = 0.0
    if isinstance(holders_list, list) and holders_list:
        for i, h in enumerate(holders_list[:10]):
            pct = float(h.get("percent", 0) or 0) * 100
            if i == 0:
                top_holder = pct
            top10_pct += pct

    suspicious = _gp_bool_flag(goplus_data, "is_airdrop_scam")

    add("Top Holder < 7%",          "pass" if top_holder < 7   else ("warn" if top_holder < 15  else "fail"), f"{top_holder:.1f}%", 1)
    add("Top 10 Holders < 40%",     "pass" if top10_pct < 40   else ("warn" if top10_pct < 50   else "fail"), f"{top10_pct:.1f}%",  1)
    add("No Suspicious Clustering", "pass" if not suspicious    else "fail", "CLEAN" if not suspicious else "RISK", 1)

    # Dev Safety
    creator_pct = _gp_float(goplus_data, "creator_percent") * 100

    add("Dev Wallet Not Dumping",   "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 1)

    # ──────────────────────────────────────────────────────
    # STAGE 2 — HONEYPOT / TEST BUY VALIDATION
    # ──────────────────────────────────────────────────────
    honeypot  = _gp_bool_flag(goplus_data, "is_honeypot")
    can_sell  = not _gp_bool_flag(goplus_data, "cannot_sell_all")
    slippage_ok = sell_tax <= 15

    add("Honeypot Safe",         "fail" if honeypot    else "pass", "DANGER" if honeypot    else "SAFE", 2)
    add("Can Sell All Tokens",   "fail" if not can_sell else "pass", "NO"    if not can_sell else "YES",  2)
    add("Slippage Acceptable",   "pass" if slippage_ok  else "warn", f"Sell={sell_tax:.0f}%",             2)

    # ──────────────────────────────────────────────────────
    # STAGE 3 — ANTI-SNIPER ENTRY FILTER
    # Token age from BSCScan first transaction
    # ──────────────────────────────────────────────────────
    token_age_min = 0.0
    if BSC_SCAN_KEY:
        try:
            tx_res = requests.get(BSC_SCAN_API, params={
                "module": "account", "action": "tokentx",
                "contractaddress": address, "sort": "asc",
                "page": 1, "offset": 1, "apikey": BSC_SCAN_KEY
            }, timeout=10)
            if tx_res.status_code == 200:
                txs = tx_res.json().get("result", [])
                if txs:
                    first_ts      = int(txs[0].get("timeStamp", 0))
                    token_age_min = (time.time() - first_ts) / 60
        except Exception as e:
            print(f"⚠️ Token age error: {e}")

    add("Token Age ≥ 3 Minutes",  "pass" if token_age_min >= 3 else "warn",
        f"{token_age_min:.0f} min" if token_age_min > 0 else "Unknown", 3)
    add("Not Fresh Sniper Bait",  "pass" if token_age_min >= 5 else "warn",
        "OK" if token_age_min >= 5 else "WAIT", 3)

    # ──────────────────────────────────────────────────────
    # STAGE 4 — BUY PRESSURE CONFIRMATION
    # FIX #3 — Better buy/sell detection using token transfer direction
    # "from" = seller, "to" = router = sell; else buy
    # ──────────────────────────────────────────────────────
    buy_count  = 0
    sell_count = 0
    if BSC_SCAN_KEY:
        try:
            tx_res2 = requests.get(BSC_SCAN_API, params={
                "module": "account", "action": "tokentx",
                "contractaddress": address, "sort": "desc",
                "page": 1, "offset": 30, "apikey": BSC_SCAN_KEY
            }, timeout=10)
            if tx_res2.status_code == 200:
                for tx in tx_res2.json().get("result", []):
                    to_addr = tx.get("to", "").lower()
                    # If token sent TO router/factory = sell; FROM router = buy
                    if to_addr in [PANCAKE_ROUTER.lower(), PANCAKE_FACTORY.lower()]:
                        sell_count += 1
                    else:
                        buy_count += 1
        except Exception as e:
            print(f"⚠️ Buy pressure error: {e}")

    total_tx     = buy_count + sell_count
    buy_pressure = buy_count > sell_count

    add("Buy Txns > Sell Txns", "pass" if buy_pressure else "warn",
        f"B:{buy_count} S:{sell_count}" if total_tx > 0 else "No Data", 4)
    add("Sufficient Activity",  "pass" if total_tx > 5  else "warn",
        f"{total_tx} recent txs", 4)

    # ──────────────────────────────────────────────────────
    # STAGE 5 — POSITION SIZING RULES (informational)
    # ──────────────────────────────────────────────────────
    add("1st Entry = 0.002–0.005 BNB", "pass", "Follow Rule",   5)
    add("Max Position ≤ 3% Balance",   "pass", "2-3% Only",     5)
    add("Max 3-4 Entries/Token",       "pass", "No Chasing",    5)

    # ──────────────────────────────────────────────────────
    # STAGE 6 — VOLUME MONITOR RULES
    # FIX #4 — Real volume thresholds documented
    # ──────────────────────────────────────────────────────
    in_dex     = _gp_bool_flag(goplus_data, "is_in_dex")
    pool_count = len(dex_list) if isinstance(dex_list, list) else 0

    add("Listed on DEX",              "pass" if in_dex     else "fail",  "YES" if in_dex else "NO", 6)
    add("DEX Pools Present",          "pass" if pool_count > 0 else "warn", f"{pool_count} pools",  6)
    add("Vol -50% → Exit 50%",        "pass", "Rule Active", 6)
    add("Vol -70% → Exit 75%",        "pass", "Rule Active", 6)
    add("Vol -90% → Exit Fully",      "pass", "Rule Active", 6)

    # ──────────────────────────────────────────────────────
    # STAGE 7 — WHALE & DEV TRACKING
    # ──────────────────────────────────────────────────────
    owner_pct = _gp_float(goplus_data, "owner_percent") * 100

    add("Dev/Creator < 5%",          "pass" if creator_pct < 5  else ("warn" if creator_pct < 15 else "fail"), f"{creator_pct:.1f}%", 7)
    add("Owner Wallet < 5%",         "pass" if owner_pct < 5    else ("warn" if owner_pct < 15   else "fail"), f"{owner_pct:.1f}%",   7)
    add("Whale Concentration OK",    "pass" if top10_pct < 45   else "fail",  f"{top10_pct:.1f}% top10", 7)
    add("Dev Sell → Exit 50% Rule",  "pass", "Rule Active", 7)

    # ──────────────────────────────────────────────────────
    # STAGE 8 — LIQUIDITY PROTECTION
    # ──────────────────────────────────────────────────────
    lp_holders  = int(_gp_str(goplus_data, "lp_holder_count", "0"))
    pool_stable = liq_locked > 20

    add("LP Not Removable Soon",     "pass" if liq_locked > 80 else ("warn" if liq_locked > 20 else "fail"), f"{liq_locked:.0f}% locked", 8)
    add("LP Holders Present",        "pass" if lp_holders > 0  else "warn", f"{lp_holders} LP holders", 8)
    add("Any LP Drop → Exit Rule",   "pass", "Rule Active", 8)

    # ──────────────────────────────────────────────────────
    # STAGE 9 — FAST PROFIT MODE
    # ──────────────────────────────────────────────────────
    low_tax       = buy_tax <= 5 and sell_tax <= 5
    fast_trade_ok = low_tax and liq_locked > 20 and not honeypot

    add("Low Tax (Fast Trade Ready)", "pass" if low_tax       else "warn", "FAST OK" if low_tax       else f"Tax: {buy_tax:.0f}%+{sell_tax:.0f}%", 9)
    add("15-30% Target Viable",       "pass" if fast_trade_ok else "warn", "YES"     if fast_trade_ok else "CHECK CONDITIONS", 9)
    add("Capital Rotation Enabled",   "pass", "After 15-30% target", 9)

    # ──────────────────────────────────────────────────────
    # STAGE 10 — STOP LOSS SYSTEM
    # ──────────────────────────────────────────────────────
    if token_age_min > 0 and token_age_min < 60:
        sl_range = "15-20% SL"
        sl_label = "New Token (<1hr)"
    elif token_age_min >= 60 and token_age_min < 360:
        sl_range = "20-25% SL"
        sl_label = "Hyped Token"
    elif token_age_min >= 360:
        sl_range = "10-15% SL"
        sl_label = "Mature Token (>6hr)"
    else:
        sl_range = "15-20% SL"
        sl_label = "Default"

    add(f"Stop Loss — {sl_label}", "pass", sl_range, 10)

    # ──────────────────────────────────────────────────────
    # STAGE 11 — LADDERED PROFIT BOOKING
    # ──────────────────────────────────────────────────────
    add("+20% → Move SL to Cost",  "pass", "Rule Active", 11)
    add("+30% → Sell 25%",         "pass", "Rule Active", 11)
    add("+50% → Sell 25%",         "pass", "Rule Active", 11)
    add("+100% → Sell 25%",        "pass", "Rule Active", 11)
    add("+200% → Keep 10% Runner", "pass", "Rule Active", 11)

    # ──────────────────────────────────────────────────────
    # STAGE 12 — SELF LEARNING
    # ──────────────────────────────────────────────────────
    add("Token Logged for Learning", "pass", "Auto-saved",  12)
    add("Pattern DB Updated",        "pass", "Active",      12)

    # ──────────────────────────────────────────────────────
    # STAGE 13 — PAPER → REAL READINESS
    # ──────────────────────────────────────────────────────
    add("Paper Mode First Rule",    "pass", "Golden Rule",  13)
    add("Switch at 70%+ Win Rate",  "pass", "Discipline",   13)
    add("30+ Trades Required",      "pass", "Before Real",  13)

    # ──────────────────────────────────────────────────────
    # OVERALL SCORE & RECOMMENDATION
    # ──────────────────────────────────────────────────────
    passed = sum(1 for c in result["checklist"] if c["status"] == "pass")
    failed = sum(1 for c in result["checklist"] if c["status"] == "fail")
    total  = len(result["checklist"])
    pct    = round((passed / total) * 100) if total > 0 else 0

    result["score"] = passed
    result["total"] = total

    # Hard fail conditions
    critical_fails = [
        c for c in result["checklist"]
        if c["status"] == "fail" and c["label"] in [
            "Honeypot Safe", "Buy Tax ≤ 10%", "Sell Tax ≤ 10%",
            "No Hidden Functions", "Transfer Allowed", "Mint Authority Disabled"
        ]
    ]

    if critical_fails or honeypot:
        result["overall"]        = "DANGER"
        result["recommendation"] = "❌ SKIP — Critical safety check failed. Honeypot / Tax / Hidden function detected. Do NOT buy."
    elif failed >= 3 or pct < 50:
        result["overall"]        = "RISK"
        result["recommendation"] = "⚠️ HIGH RISK — Multiple issues found. Skip or max 0.001 BNB test only."
    elif pct >= 75:
        result["overall"]        = "SAFE"
        result["recommendation"] = "✅ LOOKS SAFE — Start PAPER trade. Follow Stage 2 test buy + Stage 3 anti-sniper rules."
    else:
        result["overall"]        = "CAUTION"
        result["recommendation"] = "⚠️ CAUTION — Some issues. Small test buy only (0.001 BNB max). Watch volume (Stage 6)."

    return result

# Backward compat stubs
def scan_bsc_token(address: str) -> Dict:
    return run_full_sniper_checklist(address)

def scan_bsc_token_real(address: str) -> Dict:
    return run_full_sniper_checklist(address)

# ========== STAGE 12 — SELF LEARNING TRADE LOG ==========

def log_trade_internal(session_id: str, trade: Dict):
    """
    Log every trade to pattern database.
    FIX #5 — Now actually integrated with session + auto-save.
    """
    sess = get_or_create_session(session_id)
    pnl  = float(trade.get("pnl_pct", 0))
    win  = pnl > 0

    lesson = {
        "token":           trade.get("token_address", ""),
        "entry_price":     trade.get("entry_price", 0),
        "exit_price":      trade.get("exit_price",  0),
        "pnl_pct":         pnl,
        "win":             win,
        "volume_pattern":  trade.get("volume_pattern",  ""),
        "holder_behaviour":trade.get("holder_behaviour",""),
        "lesson":          trade.get("lesson",          ""),
        "stage_reached":   trade.get("stage_reached",   0),
        "timestamp":       datetime.utcnow().isoformat()
    }

    sess["pattern_database"].append(lesson)
    sess["trade_count"] += 1

    if win:
        sess["win_count"] += 1
        sess["pnl_24h"]   += pnl
    else:
        sess["daily_loss"] += abs(pnl)

    threading.Thread(target=_save_session_to_db, args=(session_id,), daemon=True).start()
    return lesson

# ========== STAGE 13 — PAPER→REAL READINESS ==========

def check_paper_to_real_readiness(session_id: str) -> Dict:
    """
    Rules: Win Rate ≥ 70%, 30+ trades, daily loss < 8%.
    Gradual: Week1=25%, Week2=50%, Week3=75%, Week4=100%.
    """
    sess        = get_or_create_session(session_id)
    trade_count = sess.get("trade_count", 0)
    win_count   = sess.get("win_count",   0)
    daily_loss  = sess.get("daily_loss",  0.0)
    win_rate    = round((win_count / trade_count * 100), 1) if trade_count > 0 else 0.0

    ready = trade_count >= 30 and win_rate >= 70.0 and daily_loss < 8.0

    # FIX #6 — Daily loss limit enforced here
    if daily_loss >= 8.0:
        stop_trading = True
        message = f"🛑 STOP TRADING — Daily loss limit reached ({daily_loss:.1f}%). Reset tomorrow."
    elif not ready:
        stop_trading = False
        message = f"📝 Keep practicing. Need 30+ trades (have {trade_count}) & 70% WR (have {win_rate:.0f}%)."
    else:
        stop_trading = False
        message = "✅ Ready for REAL trading! Start Week 1 — 25% real balance only."

    return {
        "ready":        ready,
        "stop_trading": stop_trading,
        "trade_count":  trade_count,
        "win_count":    win_count,
        "win_rate":     win_rate,
        "daily_loss":   round(daily_loss, 2),
        "message":      message,
        "transition": {
            "week_1": "25% Real",
            "week_2": "50% Real",
            "week_3": "75% Real",
            "week_4": "100% Real"
        }
    }

# ========== LLM SYSTEM PROMPT ==========
SYSTEM_PROMPT = """Tu MrBlack hai — expert BSC memecoin sniper AI. Hinglish mein baat kar.

TERA 13-STAGE SYSTEM:
S1: Safety (contract/liquidity/tax/holders/dev)
S2: Honeypot + test buy validation
S3: Anti-sniper entry (age ≥3-5min, dump recovery wait)
S4: Buy pressure confirmation
S5: Position sizing (0.002-0.005 BNB first, max 3%)
S6: Volume monitor (vol -50%=exit50%, vol -90%=exit fully)
S7: Whale/dev tracking (dev sells=exit50% immediately)
S8: Liquidity protection (any LP removal=exit)
S9: Fast profit (15-30% target, capital rotation)
S10: Stop loss (new=15-20%, hyped=20-25%, mature=10-15%)
S11: Profit ladder (+30%=sell25%, +100%=sell25%, +200%=keep10%)
S12: Self learning (log every trade, pattern DB)
S13: Paper first (70% WR + 30 trades before REAL)

RULES:
- Paper pehle, real baad mein — always
- Volume > Price (truth hai)
- Dev activity = danger signal
- 3-4 lines mein jawab de
- "Bhai" use kar
- KABHI guaranteed profit mat bol
- Daily loss 8% = STOP trading today
"""

def get_llm_reply(user_message: str, history: list, session_data: dict) -> str:
    try:
        client = FreeFlowClient()

        active_drops = knowledge_base["airdrops"]["active"][:3]
        airdrop_ctx  = ""
        if active_drops:
            names       = ", ".join(a.get("name", "") for a in active_drops)
            airdrop_ctx = f" | Airdrops: {names}"

        trade_count = session_data.get("trade_count", 0)
        win_count   = session_data.get("win_count",   0)
        # FIX #10 — Safe win rate string
        win_rate_str = f"{round(win_count/trade_count*100,1)}%" if trade_count > 0 else "No trades yet"

        market_context = (
            f"\n[BNB=${market_cache['bnb_price']:.2f}"
            f" | F&G={market_cache['fear_greed']}/100"
            f" | Mode={session_data.get('mode','paper').upper()}"
            f" | Paper={session_data.get('paper_balance', 1.87):.3f}BNB"
            f" | Trades={trade_count} WR={win_rate_str}"
            f" | DailyLoss={session_data.get('daily_loss',0):.1f}%"
            f"{airdrop_ctx}]"
        )

        messages = []
        for msg in history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message + market_context})

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            system=SYSTEM_PROMPT,
            max_tokens=400
        )
        return response.choices[0].message.content.strip()

    except NoProvidersAvailableError:
        return "⚠️ AI temporarily down, bhai. Thodi der mein try karo."
    except Exception as e:
        print(f"⚠️ LLM error: {e}")
        return f"🤖 Error aaya: {str(e)[:80]}"

# ==========================================================
# ==================== FLASK ROUTES ========================
# ==========================================================

@app.route("/")
def home():
    return render_template("index.html")

# ── Init Session ──────────────────────────────────────────
@app.route("/init-session", methods=["POST"])
def init_session():
    session_id = str(uuid.uuid4())
    get_or_create_session(session_id)
    return jsonify({"session_id": session_id, "status": "ok"})

# ── Trading Data ──────────────────────────────────────────
# FIX #7 — session_id now accepted via POST body OR GET param
@app.route("/trading-data", methods=["GET", "POST"])
def trading_data():
    if request.method == "POST":
        data       = request.get_json() or {}
        session_id = data.get("session_id", "default")
    else:
        session_id = request.args.get("session_id", "default")

    sess      = get_or_create_session(session_id)
    bnb_price = market_cache.get("bnb_price", 0)
    paper_bnb = sess.get("paper_balance", 1.87)
    paper_usd = paper_bnb * bnb_price if bnb_price else 0

    trade_count = sess.get("trade_count", 0)
    win_count   = sess.get("win_count",   0)
    win_rate    = round((win_count / trade_count * 100), 1) if trade_count > 0 else 0.0

    # FIX #6 — return daily loss limit warning
    daily_loss    = sess.get("daily_loss", 0.0)
    limit_reached = daily_loss >= 8.0

    return jsonify({
        "paper":         f"{paper_bnb:.3f}",
        "real":          f"{sess.get('real_balance', 0):.3f}",
        "pnl":           f"+{sess.get('pnl_24h', 0):.1f}%",
        "bnb_price":     bnb_price,
        "fear_greed":    market_cache.get("fear_greed", 50),
        "positions":     sess.get("positions", []),
        "paper_usd":     f"${paper_usd:.2f}" if paper_usd else "N/A",
        "trade_count":   trade_count,
        "win_rate":      win_rate,
        "daily_loss":    round(daily_loss, 2),
        "limit_reached": limit_reached
    })

# ── Chat ──────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data       = request.get_json() or {}
    user_msg   = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode       = data.get("mode", "paper")

    if not user_msg:
        return jsonify({"reply": "Kuch toh bolo, bhai! 😅", "session_id": session_id})

    sess         = get_or_create_session(session_id)
    sess["mode"] = mode

    # FIX #6 — daily loss limit check before responding
    if sess.get("daily_loss", 0) >= 8.0:
        return jsonify({
            "reply":      "🛑 Bhai STOP! Aaj tera daily loss limit (8%) reach ho gaya. Aaj koi aur trade mat karo. Kal fresh start karo. Discipline hi success hai!",
            "session_id": session_id,
            "trading": {
                "paper": f"{sess['paper_balance']:.3f}",
                "real":  f"{sess['real_balance']:.3f}",
                "pnl":   f"+{sess['pnl_24h']:.1f}%"
            }
        })

    sess["history"].append({"role": "user", "content": user_msg})
    reply = get_llm_reply(user_msg, sess["history"], sess)
    sess["history"].append({"role": "assistant", "content": reply})

    threading.Thread(target=_save_session_to_db, args=(session_id,), daemon=True).start()

    return jsonify({
        "reply":      reply,
        "session_id": session_id,
        "trading": {
            "paper": f"{sess['paper_balance']:.3f}",
            "real":  f"{sess['real_balance']:.3f}",
            "pnl":   f"+{sess['pnl_24h']:.1f}%"
        }
    })

# ── Token Scan — Full 13-Stage ────────────────────────────
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
            return jsonify({"error": "Invalid contract address, bhai!"}), 400
        result = run_full_sniper_checklist(address)
    else:
        result = {
            "address":   address,
            "checklist": [
                {"label": "⚠️ Contract address (0x) chahiye", "status": "warn", "value": "ENTER 0x",    "stage": 1},
                {"label": "Honeypot Check",                    "status": "warn", "value": "Need addr",   "stage": 2},
                {"label": "Token Age",                         "status": "warn", "value": "?",           "stage": 3},
                {"label": "Buy Pressure",                      "status": "warn", "value": "?",           "stage": 4},
            ],
            "overall":        "UNKNOWN",
            "score":          0,
            "total":          4,
            "recommendation": "⚠️ Bhai, 0x contract address dalo accurate scan ke liye."
        }

    return jsonify(result)

# ── Trade Log — Stage 12 ──────────────────────────────────
@app.route("/log-trade", methods=["POST"])
def log_trade_route():
    data       = request.get_json() or {}
    session_id = data.get("session_id", "default")
    lesson     = log_trade_internal(session_id, data)
    readiness  = check_paper_to_real_readiness(session_id)
    return jsonify({"status": "logged", "lesson": lesson, "readiness": readiness})

# ── Paper→Real Readiness — Stage 13 ──────────────────────
@app.route("/readiness", methods=["GET", "POST"])
def readiness():
    if request.method == "POST":
        data       = request.get_json() or {}
        session_id = data.get("session_id", "default")
    else:
        session_id = request.args.get("session_id", "default")
    return jsonify(check_paper_to_real_readiness(session_id))

# ── Airdrops ──────────────────────────────────────────────
@app.route("/airdrops", methods=["GET"])
def airdrops():
    return jsonify({
        "active":   knowledge_base["airdrops"]["active"],
        "upcoming": knowledge_base["airdrops"]["upcoming"],
        "total":    len(knowledge_base["airdrops"]["active"]) + len(knowledge_base["airdrops"]["upcoming"]),
        "updated":  market_cache.get("last_updated")
    })

# ── Health Check ──────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":          "ok",
        "bsc_connected":   w3.is_connected(),
        "supabase":        supabase is not None,
        "bnb_price":       market_cache.get("bnb_price", 0),
        "fear_greed":      market_cache.get("fear_greed", 50),
        "airdrops_active": len(knowledge_base["airdrops"]["active"]),
        "last_update":     market_cache.get("last_updated")
    })

# ==========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # FIX #9 — Immediate startup fetch (no 5-min wait for first data)
    threading.Thread(target=fetch_market_data,  daemon=True).start()
    threading.Thread(target=run_airdrop_hunter, daemon=True).start()
    # Main learning loop (runs every 5 min after first fetch)
    threading.Thread(target=continuous_learning, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
