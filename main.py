import os
import signal as _signal

# Global shutdown flag — SIGTERM pe set hoga, momentum threads gracefully exit honge
_BOT_SHUTDOWN = False

def _handle_sigterm(signum, frame):
    global _BOT_SHUTDOWN
    _BOT_SHUTDOWN = True
    print("⚠️ SIGTERM received — graceful shutdown started")

_signal.signal(_signal.SIGTERM, _handle_sigterm)
import re
import gc
from flask import Flask, render_template, request, jsonify
from supabase import create_client
import uuid
from datetime import datetime, timedelta, timezone
import requests

_IST = timezone(timedelta(hours=5, minutes=30))

def _to_ist(dt_str: str) -> str:
    try:
        if len(dt_str) >= 16:
            d = datetime.fromisoformat(dt_str.replace("Z",""))
            d_ist = d.replace(tzinfo=timezone.utc).astimezone(_IST)
            return d_ist.strftime("%I:%M %p")
    except:
        pass
    return dt_str[11:16] if len(dt_str) >= 16 else "—"

def _now_ist() -> str:
    return datetime.now(_IST).strftime("%I:%M %p")
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

# ========== GROQ DIRECT CLIENT — Infinite Round-Robin ==========
import groq as _groq_module

class NoProvidersAvailableError(Exception):
    pass

class FreeFlowClient:
    def __init__(self):
        _raw = os.getenv("GROQ_API_KEY", "")
        self._keys = [k.strip() for k in _raw.split(",") if k.strip()]
        self._idx  = 0
        if not self._keys:
            print("⚠️ GROQ_API_KEY not set — chat disabled")
        else:
            print(f"✅ Groq ready — {len(self._keys)} keys loaded")

    def chat(self, model: str, messages: list, max_tokens: int = 600) -> str:
        if not self._keys:
            raise NoProvidersAvailableError("No GROQ_API_KEY set")
        total    = len(self._keys)
        last_err = None
        for attempt in range(total):
            key_idx = (self._idx + attempt) % total
            key     = self._keys[key_idx]
            try:
                client = _groq_module.Groq(api_key=key)
                resp   = client.chat.completions.create(
                    model=model, messages=messages,
                    max_tokens=max_tokens, timeout=30,
                )
                self._idx = (key_idx + 1) % total
                return resp.choices[0].message.content.strip()
            except Exception as e:
                last_err = e
                print(f"⚠️ Groq key[{key_idx+1}/{total}] fail: {str(e)[:60]} — next try")
                continue
        raise NoProvidersAvailableError(f"All {total} Groq keys failed. Last: {str(last_err)[:100]}")

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
_NR_KEY          = os.getenv("NODEREAL_API_KEY", "")
_CS_RPC          = os.getenv("BSC_RPC", "")   # Chainstack ya custom RPC
_CS_WSS          = os.getenv("BSC_WSS", "")   # Chainstack WSS
BSC_RPC          = _CS_RPC if _CS_RPC else (f"https://bsc-mainnet.nodereal.io/v1/{_NR_KEY}" if _NR_KEY else "https://rpc.ankr.com/bsc")
BSC_SCAN_API     = "https://api.bscscan.com/api"
BSC_SCAN_KEY     = os.getenv("BSC_SCAN_KEY") or os.getenv("BSCSCAN_API_KEY") or os.getenv("BSC_API_KEY", "") or os.getenv("BSCSCAN_API_KEY", "")
BSC_WALLET       = os.getenv("BSC_WALLET", "")   # Real wallet address for balance display
SITE_PASSWORD    = os.getenv("SITE_PASSWORD", "mrblack2024")  # Site lock password
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

# GoPlus cache — 5 min TTL, max 100 tokens
# Contract security nahi badalti 5 min mein — API calls bachao
_goplus_cache: dict = {}  # {addr_lower: {"data": {...}, "ts": float}}
_GOPLUS_TTL = 300  # 5 minutes

def _get_goplus(token_address: str) -> dict:
    """GoPlus security data — cached 5 min | rate limit: 30/min = max 2 parallel"""
    key = token_address.lower()
    now = time.time()
    cached = _goplus_cache.get(key)
    if cached and (now - cached["ts"]) < _GOPLUS_TTL:
        return cached["data"]
    # Rate limit protection — max 2 parallel GoPlus calls
    if not _goplus_sem.acquire(blocking=True, timeout=15):
        print(f"⚠️ GoPlus sem timeout — returning empty for {token_address[:10]}")
        return {}
    try:
        r = requests.get(
            "https://api.gopluslabs.io/api/v1/token_security/56",
            params={"contract_addresses": token_address}, timeout=12
        )
        if r.status_code == 200:
            data = r.json().get("result", {}).get(key, {})
            _goplus_cache[key] = {"data": data, "ts": now}
            if len(_goplus_cache) > 100:
                oldest = sorted(_goplus_cache.items(), key=lambda x: x[1]["ts"])[:20]
                for k, _ in oldest:
                    _goplus_cache.pop(k, None)
            return data
    except Exception as e:
        print(f"⚠️ GoPlus error: {e}")
    finally:
        _goplus_sem.release()
    return {}

# Honeypot.is cache — 5 min TTL, max 100 tokens
_honeypot_cache: dict = {}  # {addr_lower: {"data": {...}, "ts": float}}
_HONEYPOT_TTL = 300  # 5 minutes

def _get_honeypot(token_address: str) -> dict:
    """Honeypot.is on-chain simulation — cached 5 min"""
    key = token_address.lower()
    now = time.time()
    cached = _honeypot_cache.get(key)
    if cached and (now - cached["ts"]) < _HONEYPOT_TTL:
        return cached["data"]
    try:
        r = requests.get(
            "https://api.honeypot.is/v2/IsHoneypot",
            params={"address": token_address, "chainID": "56"}, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            _honeypot_cache[key] = {"data": data, "ts": now}
            if len(_honeypot_cache) > 100:
                oldest = sorted(_honeypot_cache.items(), key=lambda x: x[1]["ts"])[:20]
                for k, _ in oldest:
                    _honeypot_cache.pop(k, None)
            return data
    except Exception as e:
        print(f"⚠️ Honeypot.is error: {e}")
    return {}

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
    _cached = _monitor_price_cache.get(token_address)
    if _cached and (_t.time() - _cached[1]) < 0.3:
        return _cached[0]

    _USDT = "0x55d398326f99059ff775485246999027b3197955"
    _BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    router   = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_ABI_PRICE)
    dec      = _get_dec(token_address)
    token_cs = Web3.to_checksum_address(token_address)
    wbnb_cs  = Web3.to_checksum_address(WBNB)

    def _cache_return(price):
        _monitor_price_cache[token_address] = (price, _t.time())
        if len(_monitor_price_cache) > 50:
            oldest = sorted(_monitor_price_cache.items(), key=lambda x: x[1][1])[:10]
            for k, _ in oldest: del _monitor_price_cache[k]
        return price

    # Path 1: Direct Token→BNB
    try:
        amt = router.functions.getAmountsOut(10**dec, [token_cs, wbnb_cs]).call()
        if amt[1] > 0: return _cache_return(amt[1] / 1e18)
    except: pass

    # Path 2: Token→USDT→BNB
    try:
        amt2 = router.functions.getAmountsOut(10**dec, [token_cs, Web3.to_checksum_address(_USDT), wbnb_cs]).call()
        if amt2[2] > 0: return _cache_return(amt2[2] / 1e18)
    except: pass

    # Path 3: Token→BUSD→BNB
    try:
        amt3 = router.functions.getAmountsOut(10**dec, [token_cs, Web3.to_checksum_address(_BUSD), wbnb_cs]).call()
        if amt3[2] > 0: return _cache_return(amt3[2] / 1e18)
    except: pass

    # Path 4: Bonding curve fallback (FM tokens not on PancakeSwap yet)
    try:
        info = _fm_get_token_info(token_address)
        if info and info.get("lastPrice", 0) > 0 and not info.get("liquidityAdded"):
            _bnb_p = market_cache.get("bnb_price", 640)
            # lastPrice = price in quote token (BNB or USDT)
            _lp = info["lastPrice"] / 1e18
            _quote = info.get("quote","").lower()
            _USDT_L = "0x55d398326f99059ff775485246999027b3197955"
            _BUSD_L = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
            if _quote in [_USDT_L, _BUSD_L]:
                _lp = _lp / _bnb_p if _bnb_p else _lp
            return _cache_return(_lp)
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

# ── BSC RPC: Chainstack primary + Ankr fallback ──
w3 = Web3(Web3.HTTPProvider(BSC_RPC, request_kwargs={"timeout": 5}))
if not w3.is_connected():
    print(f"⚠️ Primary RPC failed — trying Ankr fallback...")
    w3 = Web3(Web3.HTTPProvider("https://bsc-rpc.publicnode.com", request_kwargs={"timeout": 5}))
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
    "bnb_price":    300.0,  # fallback until real price fetched
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
        # w3 se gas fetch karo — NodeReal nahi hai toh bhi sahi value milegi
        try:
            _gp = w3.eth.gas_price  # wei mein
            _gwei = _gp / 1e9
            if 0.05 < _gwei < 100:
                _gas_bnb = (_gp * 150000) / 1e18
                DataGuard._gas_cache = {"val": _gas_bnb, "ts": now}
                return _gas_bnb
        except Exception:
            pass
        # Last resort fallback — 1 gwei x 150k gas
        return round((1e9 * 150000) / 1e18, 6)  # ~0.00015 BNB

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
    "user_rules":     []
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
                "user_rules":     stored.get("user_rules", [])
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
                "user_rules":     user_profile.get("user_rules", [])[-30:]
}),
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="session_id").execute()
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

# ── Scanner Stats — Per Min/Hour/Day tracking ────────────
_scanner_stats = {
    # PancakeSwap
    "pc_discovered":     0,
    "pc_prefilter_pass": 0,
    "pc_prefilter_fail": 0,
    "pc_checklist_pass": 0,
    "pc_checklist_fail": 0,
    "pc_bought":         0,
    # FourMeme
    "fm_discovered":     0,
    "fm_bought":         0,
    # Speed tracking (ms)
    "pc_speed_total":    0.0,
    "pc_speed_count":    0,
    "fm_speed_total":    0.0,
    "fm_speed_count":    0,
    # Rejection breakdown
    "rej_low_liq":       0,
    "rej_high_liq":      0,
    "rej_honeypot":      0,
    "rej_danger":        0,
    "rej_too_old":       0,
    "rej_blacklist":     0,
    # Per-minute history — last 60 entries
    "history":           [],
    "_last_min_ts":      0.0,
    "_last_min_pc_disc": 0,
    "_last_min_fm_disc": 0,
    "_last_min_pc_buy":  0,
    "_last_min_fm_buy":  0
}

def _scanner_tick():
    """Har minute snapshot save karo"""
    import time as _t
    now = _t.time()
    if now - _scanner_stats["_last_min_ts"] >= 60:
        snap = {
            "ts":      now,
            "pc_disc": _scanner_stats["pc_discovered"] - _scanner_stats["_last_min_pc_disc"],
            "fm_disc": _scanner_stats["fm_discovered"] - _scanner_stats["_last_min_fm_disc"],
            "pc_buy":  _scanner_stats["pc_bought"]     - _scanner_stats["_last_min_pc_buy"],
            "fm_buy":  _scanner_stats["fm_bought"]     - _scanner_stats["_last_min_fm_buy"]
}
        _scanner_stats["history"].append(snap)
        _scanner_stats["history"] = _scanner_stats["history"][-1440:]  # 24h max
        _scanner_stats["_last_min_ts"]      = now
        _scanner_stats["_last_min_pc_disc"] = _scanner_stats["pc_discovered"]
        _scanner_stats["_last_min_fm_disc"] = _scanner_stats["fm_discovered"]
        _scanner_stats["_last_min_pc_buy"]  = _scanner_stats["pc_bought"]
        _scanner_stats["_last_min_fm_buy"]  = _scanner_stats["fm_bought"]

# ════════════════════════════════════════════
# ANTI-MEV + REAL TRADING ENGINE
# Real mode ke liye actual BSC transactions
# Paper mode: simulation only
# ════════════════════════════════════════════
import random as _random

# PancakeSwap v2 Router ABI — swapExactETHForTokens
ROUTER_SWAP_ABI = [
    {
        "name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountOutMin",  "type": "uint256"},
            {"name": "path",          "type": "address[]"},
            {"name": "to",            "type": "address"},
            {"name": "deadline",      "type": "uint256"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    },
    {
        "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn",     "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path",         "type": "address[]"},
            {"name": "to",           "type": "address"},
            {"name": "deadline",     "type": "uint256"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path",     "type": "address[]"}
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}]
    }
]

# ERC20 approve ABI
ERC20_ABI_APPROVE = [
    {"name": "approve",  "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf","type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance","type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]}
]

REAL_PRIVATE_KEY = os.environ.get("WALLET_PRIVATE_KEY", "") or os.environ.get("PRIVATE_KEY", "") or os.environ.get("REAL_PRIVATE_KEY", "")

def _anti_mev_amount(base_bnb: float) -> float:
    """Amount randomize ±3% — round numbers MEV bots ko obvious lagte hain"""
    noise    = _random.uniform(-0.03, 0.03)
    jittered = round(base_bnb * (1 + noise), 5)
    return max(jittered, 0.001)


# Fix #4: Dynamic gas estimate

# Fix #5: Sell slippage separate function
def _anti_mev_slippage_sell(buy_tax: float = 0.0, sell_tax: float = 0.0) -> int:
    """FIX3: Sell slippage — sirf sell_tax relevant, buy_tax nahi"""
    base = max(sell_tax + 5.0, 20.0)  # FIX3: buy_tax remove kiya
    noise = _random.uniform(0.5, 3.0)
    return min(round(base + noise), 25)  # max 25%


def _anti_mev_slippage(buy_tax: float = 0.0, sell_tax: float = 0.0) -> int:
    """
    Smart slippage calculation:
    Base: tax + buffer + random noise
    MEV sandwich profitable tabhi hota hai jab slippage tight ho.
    High random slippage = MEV bot ke liye unprofitable.
    """
    base    = max(buy_tax + sell_tax + 3.0, 15.0)   # min 15%
    noise   = _random.uniform(0.5, 2.0)              # 0.5-2% random noise (anti-MEV)
    slippage = min(round(base + noise), 20)           # max 20%
    return int(slippage)



def _get_dynamic_gas_price() -> int:
    """
    Dynamic gas price in wei — NodeReal ya w3 se fetch karo.
    BSC real buy/sell/approve transactions ke liye gasPrice field.
    Cached 30s via DataGuard._gas_cache (consistent).
    Returns: int (wei) — e.g. 1_000_000_000 = 1 gwei
    """
    # Primary: w3 direct gas_price (already connected RPC)
    try:
        gp   = w3.eth.gas_price  # wei
        gwei = gp / 1e9
        if 0.5 < gwei < 100:
            return gp
    except Exception as _e:
        print(f"⚠️ _get_dynamic_gas_price w3 error: {_e}")

    # Fallback 1: NodeReal JSON-RPC
    try:
        _key = os.environ.get("NODEREAL_API_KEY", "")
        if _key:
            r = requests.post(
                f"https://bsc-mainnet.nodereal.io/v1/{_key}",
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
                timeout=5
            )
            gp_hex = r.json().get("result", "0x0")
            gp     = int(gp_hex, 16)
            gwei   = gp / 1e9
            if 0.5 < gwei < 100:
                return gp
    except Exception as _e:
        print(f"⚠️ _get_dynamic_gas_price NodeReal error: {_e}")

    # Fallback 2: BSC public RPC
    try:
        import requests as _req
        _pub_rpc = "https://bsc-dataseed.bnbchain.org"
        r2 = _req.post(
            _pub_rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
            timeout=5
        )
        gp_hex2 = r2.json().get("result", "0x0")
        gp2     = int(gp_hex2, 16)
        gwei2   = gp2 / 1e9
        if 0.5 < gwei2 < 100:
            return gp2
    except Exception as _e:
        print(f"⚠️ _get_dynamic_gas_price public RPC error: {_e}")

    # Last resort: BSC safe minimum 1 gwei
    print("⚠️ _get_dynamic_gas_price — all sources failed, using 1 gwei fallback")
    return int(1e9)  # 1 gwei in wei


def _pre_approve_after_buy(token_addr):
    """Fix #10: Pre-approve after successful buy"""
    try:
        time.sleep(2)
        token_c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI_APPROVE)
        wallet = w3.eth.account.from_key(REAL_PRIVATE_KEY).address
        allowance = token_c.functions.allowance(wallet, PANCAKE_ROUTER).call()
        if allowance < 2**256 - 1:
            approve_tx = token_c.functions.approve(PANCAKE_ROUTER, 2**256 - 1).build_transaction({
                "from": wallet, "gas": 100000,
                "gasPrice": _get_dynamic_gas_price(),
                "nonce": w3.eth.get_transaction_count(wallet, "pending")
            })
            signed = w3.eth.account.sign_transaction(approve_tx, REAL_PRIVATE_KEY)
            w3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"✅ Pre-approved {token_addr[:10]}")
    except Exception as e:
        print(f"⚠️ Pre-approve error: {e}")

def real_buy_token(token_address: str, bnb_amount: float,
                   buy_tax: float = 0.0, sell_tax: float = 0.0) -> dict:
    """
    Real BSC buy transaction with full anti-MEV protection.
    Returns: {success, tx_hash, tokens_received, entry_price, gas_used, error}
    """
    result = {"success": False, "tx_hash": "", "tokens_received": 0,
              "entry_price": 0.0, "gas_used": 0, "error": ""}

    if not REAL_PRIVATE_KEY:
        result["error"] = "WALLET_PRIVATE_KEY env nahi set — real trading disabled"
        return result

    # Real wallet balance check — buy se pehle
    try:
        _wallet_addr = w3.eth.account.from_key(REAL_PRIVATE_KEY).address
        _wallet_bal  = float(w3.eth.get_balance(_wallet_addr)) / 1e18
        _gas_est     = 0.002  # ~0.002 BNB gas reserve
        if _wallet_bal < bnb_amount + _gas_est:
            result["error"] = f"Insufficient balance: {_wallet_bal:.4f} BNB < {bnb_amount + _gas_est:.4f} BNB needed"
            print(f"🛑 REAL BUY BLOCKED: low balance {_wallet_bal:.4f} BNB")
            _push_notif("critical", "🔴 Insufficient Balance", f"Balance: {_wallet_bal:.4f} BNB | Needed: {bnb_amount + _gas_est:.4f} BNB — Top up wallet!", token_address[:10], token_address)
            return result
    except Exception as _be:
        print(f"⚠️ Balance check error: {_be}")

    try:
        account  = w3.eth.account.from_key(REAL_PRIVATE_KEY)
        wallet   = account.address
        router   = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_SWAP_ABI)
        token_cs = Web3.to_checksum_address(token_address)
        wbnb_cs  = Web3.to_checksum_address(WBNB)

        # Anti-MEV: amount noise
        bnb_wei  = w3.to_wei(_anti_mev_amount(bnb_amount), "ether")

        # Slippage: tax-aware + random
        slippage_pct = _anti_mev_slippage(buy_tax, sell_tax)

        _USDT_CS = Web3.to_checksum_address("0x55d398326f99059ff775485246999027b3197955")
        _BUSD_CS = Web3.to_checksum_address("0xe9e7cea3dedca5984780bafc599bd69add087d56")

        # Multi-path: best path dhundo
        _buy_path      = None
        _amount_out_min = 0

        # Path 1: Direct BNB→Token
        try:
            _out1 = router.functions.getAmountsOut(bnb_wei, [wbnb_cs, token_cs]).call()
            if _out1[1] > 0:
                _buy_path       = [wbnb_cs, token_cs]
                _amount_out_min = int(_out1[1] * (1 - slippage_pct / 100))
                print(f"🛣️ Path: BNB→Token direct")
        except Exception:
            pass

        # Path 2: BNB→USDT→Token
        if not _buy_path:
            try:
                _out2 = router.functions.getAmountsOut(bnb_wei, [wbnb_cs, _USDT_CS, token_cs]).call()
                if _out2[2] > 0:
                    _buy_path       = [wbnb_cs, _USDT_CS, token_cs]
                    _amount_out_min = int(_out2[2] * (1 - slippage_pct / 100))
                    print(f"🛣️ Path: BNB→USDT→Token")
            except Exception:
                pass

        # Path 3: BNB→BUSD→Token
        if not _buy_path:
            try:
                _out3 = router.functions.getAmountsOut(bnb_wei, [wbnb_cs, _BUSD_CS, token_cs]).call()
                if _out3[2] > 0:
                    _buy_path       = [wbnb_cs, _BUSD_CS, token_cs]
                    _amount_out_min = int(_out3[2] * (1 - slippage_pct / 100))
                    print(f"🛣️ Path: BNB→BUSD→Token")
            except Exception:
                pass

        if not _buy_path:
            result["error"] = "No valid swap path found"
            print(f"❌ REAL BUY: no path for {token_address[:10]}")
            return result

        amount_out_min = _amount_out_min

        # Deadline: 60 sec
        deadline  = int(time.time()) + 60
        nonce     = w3.eth.get_transaction_count(wallet, "pending")
        gas_price = _get_dynamic_gas_price()

        txn = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            amount_out_min,
            _buy_path,
            wallet,
            deadline
        ).build_transaction({
            "from":     wallet,
            "value":    bnb_wei,
            "gas":      350000,
            "gasPrice": gas_price,
            "nonce":    nonce,
            "chainId":  56
        })

        signed  = w3.eth.account.sign_transaction(txn, REAL_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        print(f"🔴 REAL BUY TX: {tx_hash.hex()[:20]}... slippage={slippage_pct}%")

        # Wait for receipt (30 sec max)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] == 1:
            result["success"]      = True
            result["tx_hash"]      = tx_hash.hex()
            result["gas_used"]     = receipt["gasUsed"]
            # Entry price from on-chain
            result["entry_price"]  = get_token_price_bnb(token_address)
            print(f"✅ REAL BUY confirmed: {tx_hash.hex()[:20]}... gas={receipt['gasUsed']}")
        else:
            result["error"] = "Transaction reverted"
            print(f"❌ REAL BUY reverted: {tx_hash.hex()[:20]}")
            _push_notif("critical", "🔴 Buy Reverted", f"Transaction reverted on-chain | TX: {tx_hash.hex()[:16]}...", token_address[:10], token_address)

    except Exception as e:
        result["error"] = str(e)[:200]
        print(f"❌ REAL BUY error: {e}")
        _err_str = str(e)[:100]
        if "timeout" in _err_str.lower() or "timed out" in _err_str.lower():
            _push_notif("critical", "🔴 Buy Timeout", f"Transaction stuck — not confirmed in 30s | {_err_str}", token_address[:10], token_address)
        elif "nonce" in _err_str.lower():
            _push_notif("critical", "🔴 Nonce Error", f"Nonce conflict — pending transaction stuck | {_err_str}", token_address[:10], token_address)
        elif "gas" in _err_str.lower() or "fee" in _err_str.lower():
            _push_notif("warning", "🟡 Gas Error", f"Gas fee issue on buy | {_err_str}", token_address[:10], token_address)
        elif "slippage" in _err_str.lower() or "INSUFFICIENT_OUTPUT" in _err_str:
            _push_notif("warning", "🟡 Slippage Too High", f"Price moved too fast — buy failed | {_err_str}", token_address[:10], token_address)
        else:
            _push_notif("critical", "🔴 Buy Failed", f"{_err_str}", token_address[:10], token_address)

    return result


def real_sell_token(token_address: str, sell_pct: float = 100.0,
                    buy_tax: float = 0.0, sell_tax: float = 0.0) -> dict:
    result = {"success": False, "tx_hash": "", "bnb_received": 0.0,
              "gas_used": 0, "error": ""}

    if not REAL_PRIVATE_KEY:
        result["error"] = "WALLET_PRIVATE_KEY not set"
        return result

    try:
        account  = w3.eth.account.from_key(REAL_PRIVATE_KEY)
        wallet   = account.address
        router   = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_ROUTER), abi=ROUTER_SWAP_ABI)
        token_cs = Web3.to_checksum_address(token_address)
        wbnb_cs  = Web3.to_checksum_address(WBNB)
        token_c  = w3.eth.contract(address=token_cs, abi=ERC20_ABI_APPROVE)

        # FIX 4: Gas balance check before sell
        try:
            _gas_bal = w3.eth.get_balance(wallet) / 1e18
            if _gas_bal < 0.0015:  # minimum ~0.0015 BNB for sell gas
                result["error"] = f"insufficient wallet balance {_gas_bal:.4f} BNB"
                _push_notif("critical", "🔴 Low Balance",
                    f"insufficient wallet balance {_gas_bal:.4f} BNB — wallet top up karo!",
                    token_address[:10], token_address)
                return result
        except Exception:
            pass  # gas check fail toh proceed

        balance   = token_c.functions.balanceOf(wallet).call()
        sell_amt  = int(balance * sell_pct / 100)
        if sell_amt <= 0:
            result["error"] = "Zero balance"
            return result

        allowance = token_c.functions.allowance(wallet, Web3.to_checksum_address(PANCAKE_ROUTER)).call()
        if allowance < sell_amt:
            nonce_a = w3.eth.get_transaction_count(wallet, "pending")
            # GAS FIX: Approve bhi 3x — approve slow toh sell delay hogi
            approve_txn = token_c.functions.approve(
                Web3.to_checksum_address(PANCAKE_ROUTER),
                2**256 - 1
            ).build_transaction({
                "from": wallet, "gas": 100000,
                "gasPrice": int(_get_dynamic_gas_price() * 1.5),  # FIX v23: was 3.0x
                "nonce": nonce_a, "chainId": 56
            })
            signed_a = w3.eth.account.sign_transaction(approve_txn, REAL_PRIVATE_KEY)
            tx_hash_a = w3.eth.send_raw_transaction(signed_a.rawTransaction)
            w3.eth.wait_for_transaction_receipt(tx_hash_a, timeout=30)
            print(f"✅ Approved token for sell")

        slippage_pct = _anti_mev_slippage_sell(buy_tax, sell_tax)
        expected_bnb = router.functions.getAmountsOut(sell_amt, [token_cs, wbnb_cs]).call()
        min_bnb      = int(expected_bnb[1] * (1 - slippage_pct / 100))
        deadline     = int(time.time()) + 60
        nonce        = w3.eth.get_transaction_count(wallet, "pending")

        # GAS FIX: Sell pe 3x gas — rug se pehle fast niklo
        _sell_gas_price = int(_get_dynamic_gas_price() * 1.2)  # FIX v23: was 3.0x
        txn = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            sell_amt, min_bnb,
            [token_cs, wbnb_cs],
            wallet, deadline
        ).build_transaction({
            "from": wallet, "gas": 300000,
            "gasPrice": _sell_gas_price,
            "nonce": nonce, "chainId": 56
        })

        signed  = w3.eth.account.sign_transaction(txn, REAL_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        print(f"🔴 REAL SELL TX: {tx_hash.hex()[:20]}... slippage={slippage_pct}%")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] == 1:
            result["success"]      = True
            result["tx_hash"]      = tx_hash.hex()
            result["gas_used"]     = receipt["gasUsed"]
            result["gas_price"]    = txn.get("gasPrice", 0)
            
            # FIX 2: Parse actual BNB received from logs
            # WBNB Withdrawal event WBNB contract se aata hai, Router se nahi
            _WBNB_ADDR = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
            bnb_received = min_bnb / 1e18
            for log in receipt["logs"]:
                # Withdrawal(address,uint256) — topic0 = keccak256("Withdrawal(address,uint256)")
                _is_wbnb    = log["address"].lower() == _WBNB_ADDR.lower()
                _withdrawal = len(log.get("topics", [])) >= 2 and log["topics"][0].hex().startswith("7fcf532c")
                if _is_wbnb and _withdrawal:
                    try:
                        bnb_received = int(log["data"].hex() if isinstance(log["data"], bytes) else log["data"], 16) / 1e18
                        break
                    except Exception:
                        pass
            # Fallback — swap event se parse karo
            if bnb_received == min_bnb / 1e18:
                for log in receipt["logs"]:
                    _d = log.get("data", "")
                    _d = _d.hex() if isinstance(_d, bytes) else _d
                    if len(_d) >= 130:
                        raw_hex = _d[2:] if _d.startswith("0x") else _d
                        try:
                            a0out = int(raw_hex[128:192], 16)
                            a1out = int(raw_hex[192:256], 16)
                            _candidate = max(a0out, a1out) / 1e18
                            if 0 < _candidate < 100:  # sanity check
                                bnb_received = _candidate
                                break
                        except Exception:
                            pass
            result["bnb_received"] = bnb_received
            print(f"✅ REAL SELL confirmed: {tx_hash.hex()[:20]}... BNB received: {bnb_received:.6f}")
        else:
            result["error"] = "Sell reverted"
            _push_notif("critical", "🔴 Sell Reverted", f"Sell transaction reverted — position still open!", token_address[:10], token_address)

    except Exception as e:
        result["error"] = str(e)[:200]
        print(f"❌ REAL SELL error: {e}")
        _sell_err = str(e)[:100]
        if "timeout" in _sell_err.lower():
            _push_notif("critical", "🔴 Sell Timeout", f"Sell stuck — position may still be open! | {_sell_err}", token_address[:10], token_address)
        else:
            _push_notif("critical", "🔴 Sell Failed", f"{_sell_err}", token_address[:10], token_address)

    return result
# ── Vol Pressure cache — globals (missing fix) ──
VOL_CACHE_TTL       = 60          # 60s TTL — DexScreener rate limit safe
_vol_pressure_cache = {}          # {addr_lower: {"buys5":N, "sells5":N, ...,"ts":float}}
_vol_pressure_lock  = threading.Lock()

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

# PancakeSwap v2 Burn event topic — LP remove hone pe fire hota hai
# keccak256("Burn(address,uint256,uint256,address)")
# Yeh dev ke removeLiquidity() call pe turant fire hota hai — PEHLE price drop se
BURN_TOPIC = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"

# LP Burn alert set — kaunse tokens ke liye burn detect hua
_lp_burn_alerts: set = set()
_lp_burn_lock = threading.Lock()

def _get_pair_for_token(token_address: str) -> str:
    """Token ka v2 pair address lo — cached"""
    tl = token_address.lower()
    with _pair_addr_lock:
        if tl in _pair_addr_cache:
            return _pair_addr_cache[tl]
    pair = _get_v2_pair(token_address)
    if pair:
        with _pair_addr_lock:
            if len(_pair_addr_cache) > 500:
                # oldest 100 entries remove karo
                for _k in list(_pair_addr_cache.keys())[:100]:
                    del _pair_addr_cache[_k]
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
    FIX v37 B: Real-time volume pressure — sirf onchain RT data.
    DexScreener fallback HATAYA — 60s stale data se MomDead late fire hota tha.
    RT data nahi = koi buyers nahi = vol_dead assume karo (safe/fast exit).
    """
    now = time.time()
    key = token_address.lower()
    with _rt_swap_lock:
        rt = _rt_swap_data.get(key)

    # RT data hai aur 5 min se zyada purana nahi — use karo
    if rt and (now - rt.get("ts", 0)) < 300:
        return {
            "buys5":      rt.get("buys5",    0),
            "sells5":     rt.get("sells5",   0),
            "buys1h":     rt.get("buys1h",   0),
            "sells1h":    rt.get("sells1h",  0),
            "buy_vol5":   rt.get("buy_vol5",  0.0),
            "sell_vol5":  rt.get("sell_vol5", 0.0),
            "buy_vol1h":  rt.get("buy_vol1h", 0.0),
            "sell_vol1h": rt.get("sell_vol1h",0.0),
            "ts":         rt["ts"],
            "source":     "onchain"
        }
    # FIX v37 B: DexScreener fallback NAHI — RT data nahi = vol dead
    # Agar koi swap event nahi aaya = koi buyer nahi = bv5=0
    return {
        "buys5":      0,
        "sells5":     0,
        "buys1h":     0,
        "sells1h":    0,
        "buy_vol5":   0.0,
        "sell_vol5":  0.0,
        "buy_vol1h":  0.0,
        "sell_vol1h": 0.0,
        "ts":         now,
        "source":     "no_rt_data"
    }


def start_swap_monitor():
    """
    Hot-standby Swap Monitor:
    - Primary chal raha hai → Backup 1 & 2 so rahe hain
    - Primary gira → Backup 1 milliseconds mein le leta hai
    - Primary wapas aaya → Backup fir so jaata hai
    - Loop kabhi nahi tootega
    """
    import asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        print("⚠️ websockets nahi — swap monitor disabled")
        return

    # 1 Primary + 2 Backup — priority order
    _SM_PRIMARY = "wss://bsc-rpc.publicnode.com"
    _SM_BACKUP1 = "wss://bsc.publicnode.com"
    _SM_BACKUP2 = "wss://bsc.drpc.org"

    # Shared active flag — kaun abhi active hai
    # 0 = koi nahi, 1 = primary, 2 = backup1, 3 = backup2
    _sm_active = {"slot": 0, "ts": 0.0}
    _sm_lock   = threading.Lock()

    def _set_active(slot):
        with _sm_lock:
            _sm_active["slot"] = slot
            _sm_active["ts"]   = time.time()

    def _get_active():
        with _sm_lock:
            return _sm_active["slot"]

    async def _run_connection(wss_url, my_slot, label):
        """
        Single WSS connection loop — agar higher priority slot active hai
        toh yeh so jaata hai. Primary gire toh backup turant jaag jaata hai.
        """
        pair_to_token: dict = {}
        last_map_refresh    = 0.0

        while True:
            # ── Higher priority already active hai? So jao ──
            current = _get_active()
            if current != 0 and current < my_slot:
                await asyncio.sleep(2)  # 2s check — primary alive hai?
                continue

            # ── Connect karo ──
            try:
                async with _ws.connect(
                    wss_url,
                    ping_interval=20, ping_timeout=15,
                    close_timeout=5,  max_size=2**20
                ) as ws:
                    # Subscribe
                    sub_msg = _json.dumps({
                        "id": 10, "method": "eth_subscribe",
                        "params": ["logs", {
                            "topics": [[SWAP_TOPIC, BURN_TOPIC]]
                        }],
                        "jsonrpc": "2.0"
                    })
                    await ws.send(sub_msg)
                    _ack = await asyncio.wait_for(ws.recv(), timeout=10)
                    _ack_data = _json.loads(_ack)
                    if _ack_data.get("error"):
                        raise Exception(f"Sub rejected: {_ack_data['error']}")

                    # Active mark karo
                    _set_active(my_slot)
                    print(f"⚡ SwapMonitor [{label}] connected: {wss_url}")

                    # Pair map initialize
                    with monitor_lock:
                        tokens = list(monitored_positions.keys())
                    for tok in tokens:
                        pair = _get_pair_for_token(tok)
                        if pair:
                            pair_to_token[pair.lower()] = tok.lower()

                    # ── Event loop ──
                    while True:
                        # Higher priority wapas aa gaya? Gracefully exit
                        if _get_active() < my_slot and _get_active() != 0:
                            print(f"⚡ SwapMonitor [{label}] stepping back — primary recovered")
                            _set_active(0)
                            break

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=25)
                        except asyncio.TimeoutError:
                            continue  # Timeout = normal, loop chalta rahe

                        data        = _json.loads(msg)
                        log         = (data.get("params") or {}).get("result") or {}
                        if not log: continue

                        topics      = log.get("topics") or []
                        if not topics: continue
                        event_topic = topics[0].lower()
                        pair_addr   = log.get("address", "").lower()

                        # ── LP BURN — Rug detect ──
                        if event_topic == BURN_TOPIC.lower():
                            with monitor_lock:
                                tokens_now = list(monitored_positions.keys())
                            for tok in tokens_now:
                                pair = _get_pair_for_token(tok)
                                if pair:
                                    pair_to_token[pair.lower()] = tok.lower()
                            token_addr = pair_to_token.get(pair_addr)
                            if token_addr:
                                already_alerted = False
                                with _lp_burn_lock:
                                    if token_addr in _lp_burn_alerts:
                                        already_alerted = True
                                    else:
                                        _lp_burn_alerts.add(token_addr)
                                if not already_alerted:
                                    print(f"🚨 LP BURN [{label}]: {token_addr[:10]} — INSTANT SELL!")
                                    _log("sell", token_addr[:10], "LP Burn — Rug Incoming 🚨", token_addr)
                                    _auto_paper_sell(token_addr, "LP Burn 🚨 Rug Detected", 100.0)
                            continue

                        if event_topic != SWAP_TOPIC.lower():
                            continue

                        # ── Pair map refresh every 30s ──
                        now_t = time.time()
                        if now_t - last_map_refresh > 30:
                            with monitor_lock:
                                tokens = list(monitored_positions.keys())
                            for tok in tokens:
                                pair = _get_pair_for_token(tok)
                                if pair:
                                    pair_to_token[pair.lower()] = tok.lower()
                            active_tokens = set(t.lower() for t in tokens)
                            pair_to_token = {
                                p: t for p, t in pair_to_token.items()
                                if t in active_tokens
                            }
                            last_map_refresh = now_t

                        token_addr = pair_to_token.get(pair_addr)
                        if not token_addr:
                            continue

                        # ── Swap decode ──
                        raw = log.get("data", "0x")
                        if len(raw) < 130:
                            continue
                        raw_hex = raw[2:]
                        try:
                            a0in  = int(raw_hex[0:64],   16)
                            a1in  = int(raw_hex[64:128], 16)
                            a0out = int(raw_hex[128:192], 16)
                            a1out = int(raw_hex[192:256], 16)
                        except Exception:
                            continue

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

                        if _tok_is_t0:
                            is_buy  = a1in > 0
                            bnb_wei = a1in if is_buy else a1out
                        else:
                            is_buy  = a0in > 0
                            bnb_wei = a0in if is_buy else a0out

                        bnb_amt = bnb_wei / 1e18
                        _record_swap(token_addr, is_buy, bnb_amt)

                        if bnb_amt >= 0.1:
                            _dir = "🟢BUY " if is_buy else "🔴SELL"
                            print(f"⚡ [{label}] {_dir} {token_addr[:10]} {bnb_amt:.3f} BNB")

            except Exception as e:
                # Primary gira → active = 0 → backup jaag jayega
                if _get_active() == my_slot:
                    _set_active(0)
                err = str(e).lower()
                if "1013" in err or "close frame" in err or "timeout" in err:
                    print(f"⚠️ SwapMonitor [{label}] disconnect — backup le raha hai")
                else:
                    print(f"⚠️ SwapMonitor [{label}] error: {str(e)[:60]}")
                await asyncio.sleep(2)  # 2s baad reconnect try

    async def _master():
        # Teeno connections parallel start karo
        # Primary = slot 1, Backup1 = slot 2, Backup2 = slot 3
        await asyncio.gather(
            _run_connection(_SM_PRIMARY, 1, "PRIMARY"),
            _run_connection(_SM_BACKUP1, 2, "BACKUP1"),
            _run_connection(_SM_BACKUP2, 3, "BACKUP2"),
        )

    def _run_swap_monitor():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_master())
        except Exception as ex:
            print(f"⚠️ Swap Monitor thread: {ex}")
        finally:
            loop.close()

    threading.Thread(target=_run_swap_monitor, daemon=True).start()
    print("⚡ Real-time Swap Monitor starting (Hot-Standby mode)...")

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
WHALE_MAX_WALLETS   = 500   # memory cap

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
        # Source 1: GoPlus holders (cached)
        gp = _get_goplus(token_address)
        if gp:
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
# WHALE FOLLOW SYSTEM
# Qualified whale wallets ki recent activity monitor karo
# Agar whale ne koi naya token kharida → bot bhi us token ko scan kare
# ══════════════════════════════════════════════
_whale_last_checked: dict = {}  # {wallet_lower: last_check_timestamp}
_whale_follow_seen:  set  = set()  # tokens already queued via whale follow (dedup)
_WHALE_CHECK_INTERVAL = 300  # har 5 min ek whale check karo (BSCScan rate limit safe)

def _get_whale_recent_tokens(wallet: str) -> list:
    """
    BSCScan se wallet ki last 10 BEP20 transfers fetch karo.
    Returns: [token_address, ...] — unique token addresses
    """
    if not BSC_SCAN_KEY:
        return []
    try:
        r = requests.get(BSC_SCAN_API, params={
            "module":     "account",
            "action":     "tokentx",
            "address":    wallet,
            "page":       1,
            "offset":     20,
            "sort":       "desc",
            "apikey":     BSC_SCAN_KEY
}, timeout=8)
        if r.status_code != 200:
            return []
        txns = r.json().get("result", [])
        if not isinstance(txns, list):
            return []
        # Sirf BSC pe, sirf BUY (wallet = to), last 30 min mein
        cutoff = time.time() - 1800
        tokens = []
        seen   = set()
        for tx in txns:
            if str(tx.get("to", "")).lower() != wallet.lower(): continue
            ts = int(tx.get("timeStamp", 0) or 0)
            if ts < cutoff: continue
            ca = tx.get("contractAddress", "")
            if ca and ca not in seen:
                seen.add(ca)
                tokens.append(ca)
        return tokens[:5]  # max 5 naye tokens per whale
    except Exception as e:
        print(f"⚠️ whale_recent_tokens {wallet[:10]}: {e}")
        return []

def _whale_follow_loop():
    """
    Background: qualified whales ki recent buys check karo.
    Agar whale ne koi naya token kharida → auto-check queue mein dalo.
    """
    time.sleep(120)  # startup delay
    while True:
        try:
            now = time.time()
            with _smart_wallets_lock:
                qualified = [
                    (w, d) for w, d in _smart_wallets.items()
                    if d.get("qualified", False)
                ]

            if not qualified:
                time.sleep(60)
                continue

            # Sabse zyada wins wale whales pehle check karo
            qualified.sort(key=lambda x: x[1].get("wins", 0), reverse=True)
            checked = 0

            for wallet, wdata in qualified[:20]:  # max 20 qualified whales monitor
                # Rate limit — har whale ko sirf har 5 min check karo
                if now - _whale_last_checked.get(wallet, 0) < _WHALE_CHECK_INTERVAL:
                    continue

                _whale_last_checked[wallet] = now
                # Max 200 entries — purane timestamps hatao
                if len(_whale_last_checked) > 200:
                    oldest = sorted(_whale_last_checked.items(), key=lambda x: x[1])[:50]
                    for k, _ in oldest:
                        _whale_last_checked.pop(k, None)
                tokens = _get_whale_recent_tokens(wallet)
                checked += 1

                for token_addr in tokens:
                    ta = token_addr.lower()
                    if ta in _whale_follow_seen:
                        continue
                    if ta in discovered_addresses:
                        continue  # already processed
                    _whale_follow_seen.add(ta)
                    if len(_whale_follow_seen) > 500:
                        # cleanup old entries
                        _whale_follow_seen.clear()

                    w_label = get_smart_wallet_label(wallet)
                    print(f"🐋 Whale Follow: {wallet[:10]} ({w_label}) bought {token_addr[:10]} → auto-scan")
                    # Directly auto_check queue mein dalo
                    threading.Thread(
                        args=(token_addr,),
                        kwargs={"whale_triggered": True, "whale_wallet": wallet},
                        daemon=True
                    ).start()

                time.sleep(1)  # BSCScan rate limit

            if checked > 0:
                print(f"🐋 Whale monitor: checked {checked} wallets, {len(qualified)} qualified total")

        except Exception as e:
            print(f"⚠️ whale_follow_loop: {e}")
        time.sleep(60)  # har 60s ek cycle


def _count_whales_in_token(token_address: str, goplus_data: dict) -> int:
    """
    Token ke holders mein kitne qualified whales hain — score ke liye.
    """
    holders = goplus_data.get("holders", []) or []
    count   = 0
    for h in holders[:30]:
        addr = h.get("address", "")
        if addr and is_smart_wallet(addr):
            count += 1
    return count



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

    # ── SIGNAL 3: Smart Money / Whale Wallet Detected ──
    # GoPlus holders mein koi known profitable whale hai?
    holders = goplus_data.get("holders", [])
    creator = goplus_data.get("creator_address", "")
    sm_found = []
    for h in (holders or [])[:30]:
        addr_h = h.get("address", "")
        if is_smart_wallet(addr_h):
            sm_found.append(get_smart_wallet_label(addr_h))
    if is_smart_wallet(creator):
        sm_found.append(f"creator:{get_smart_wallet_label(creator)}")
        score += 2  # Creator khud profitable hai = extra conviction
    if sm_found:
        _sm_details = []
        for h in (holders or [])[:30]:
            a = h.get("address","")
            if is_smart_wallet(a):
                _sm_details.append(get_smart_wallet_label(a))
        detail_str = " | ".join(_sm_details[:3]) if _sm_details else f"{len(sm_found)} wallets"
        whale_count = len(sm_found)

        # Whale count ke hisaab se signal strength
        if whale_count >= 3:
            signals.append({"type": "MULTI_WHALE", "detail": f"🐋🐋🐋 {whale_count} whales in: {detail_str}", "weight": 5})
            score += 5  # 3+ whales = very strong conviction
        elif whale_count == 2:
            signals.append({"type": "DOUBLE_WHALE", "detail": f"🐋🐋 2 whales: {detail_str}", "weight": 4})
            score += 4
        else:
            signals.append({"type": "SMART_MONEY", "detail": f"🧠 {detail_str}", "weight": 3})
            score += 3

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
    # dex_data mein pairCreatedAt already hai — no extra API call
    try:
        _pair_created = dex_data.get("pair_created_at", 0) or 0
        if not _pair_created:
            # DexScreener raw data se try karo
            _raw = dex_data.get("_raw_pair_created", 0) or 0
            _pair_created = _raw
        if _pair_created:
            age_min = (time.time() - _pair_created / 1000) / 60
            if 5 <= age_min <= 30:
                signals.append({"type": "SWEET_SPOT_AGE", "detail": f"{age_min:.0f} min old (5-30 optimal)", "weight": 2})
                score += 2
            elif 30 < age_min <= 60:
                signals.append({"type": "GOOD_AGE", "detail": f"{age_min:.0f} min old", "weight": 1})
                score += 1
    except Exception: pass

    # ── SIGNAL 8: Price Momentum ──
    change_1h = dex_data.get("change_1h", 0) or 0
    if change_1h >= 50:
        signals.append({"type": "STRONG_MOMENTUM", "detail": f"+{change_1h:.0f}% in 1h", "weight": 2})
        score += 2
    elif change_1h >= 20:
        signals.append({"type": "MOMENTUM", "detail": f"+{change_1h:.0f}% in 1h", "weight": 1})
        score += 1

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
        "size_multiplier": size_mult
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
        # Max 300 entries — purane (1 rug wale) hatao
        if len(_dev_blacklist) > 300:
            _single = [k for k, v in _dev_blacklist.items() if v.get("rugs", 1) <= 1]
            for k in _single[:50]:
                _dev_blacklist.pop(k, None)
    print(f"🚫 Dev blacklisted: {wallet[:10]}... reason={reason}")

def is_dev_blacklisted(wallet: str) -> bool:
    if not wallet or len(wallet) != 42: return False
    return wallet.lower() in _dev_blacklist

# ══════════════════════════════════════════════
# TOKEN BLACKLIST — rug/SL tokens 24h block
# ══════════════════════════════════════════════
_token_blacklist: dict = {}  # {addr_lower: {"reason": str, "ts": float}}
_TOKEN_BL_TTL = 691200  # 8 days blacklist TTL

def blacklist_token(token_address: str, reason: str = "rug"):
    """Token ko 8 days ke liye blacklist karo — 6h+ tokens bot skip karta hai"""
    if not token_address: return
    _token_blacklist[token_address.lower()] = {
        "reason": reason,
        "ts":     time.time()
    }
    # Max 8000 entries — 8 day TTL ke saath cleanup
    if len(_token_blacklist) > 8000:
        cutoff = time.time() - _TOKEN_BL_TTL
        stale = [k for k, v in _token_blacklist.items() if v["ts"] < cutoff]
        for k in stale:
            _token_blacklist.pop(k, None)
    print(f"🚫 Token blacklisted 8d: {token_address[:10]}... reason={reason}")

def is_token_blacklisted(token_address: str) -> bool:
    if not token_address: return False
    entry = _token_blacklist.get(token_address.lower())
    if not entry: return False
    # TTL check — expired toh remove
    if time.time() - entry["ts"] > _TOKEN_BL_TTL:
        _token_blacklist.pop(token_address.lower(), None)
        return False
    return True

# ══════════════════════════════════════════════
# RUG DNA SYSTEM — rug fingerprint learn karo
# Creator + tax pattern + liq = unique rug signature
# Same DNA wala naya token → auto reject
# ========== TRADE HISTORY — SUPABASE PERMANENT STORAGE ==========
_TRADES_SESSION_ID      = "MRBLACK_TRADE_HISTORY"       # paper trades — memory table
_REAL_TRADES_TABLE      = "real_trade_history"           # real trades — alag table

def _save_trade_history_to_db():
    """Har trade ke baad Supabase mein permanently save karo — 3 retries"""
    if not supabase: return
    for _attempt in range(3):
        try:
            all_hist = auto_trade_stats.get("trade_history", [])
            supabase.table("memory").upsert({
                "session_id":    _TRADES_SESSION_ID,
                "role":          "user",
                "content":       "",
                "trade_history": json.dumps(all_hist[-10000:]),
                "updated_at":    datetime.utcnow().isoformat()
}, on_conflict="session_id").execute()
            # FIX v30: mode None wali entries bhi sahi count mein dalo
            paper_hist = [t for t in all_hist if (t.get("mode") or "paper") == "paper"]
            real_hist  = [t for t in all_hist if (t.get("mode") or "") == "real"]
            print(f"💾 Trade history saved: {len(all_hist)} total | {len(paper_hist)} paper | {len(real_hist)} real")
            return  # success
        except Exception as e:
            if _attempt < 2:
                time.sleep(2)  # 2s wait before retry
            else:
                print(f"⚠️ Trade history save error (3 retries failed): {e}")

def _load_trade_history_from_db():
    # FIX v45: real_trade_history table bhi load karo — pehle sirf memory table tha
    try:
        _REAL_TABLE = "real_trade_history"
        _real_res = supabase.table(_REAL_TABLE).select("*").order("id", desc=True).limit(500).execute()
        if _real_res.data:
            _real_rows = []
            for row in _real_res.data:
                try:
                    import json as _rj
                    _entry = _rj.loads(row.get("data", "{}")) if isinstance(row.get("data"), str) else row
                    if isinstance(_entry, dict) and _entry.get("token"):
                        _entry["mode"] = "real"
                        _real_rows.append(_entry)
                except Exception:
                    pass
            if _real_rows:
                _existing = auto_trade_stats.get("trade_history", [])
                _existing_addrs = {(t.get("address","").lower(), t.get("sold_at","")) for t in _existing}
                _new_real = [t for t in _real_rows
                             if (t.get("address","").lower(), t.get("sold_at","")) not in _existing_addrs]
                auto_trade_stats["trade_history"] = _existing + _new_real
                print(f"✅ [v45] Real trades loaded from DB: {len(_new_real)} new entries")
    except Exception as _rle:
        print(f"⚠️ [v45] Real trade history load skip: {str(_rle)[:60]}")

def _load_trade_history_from_db_ORIGINAL():
    """Startup pe Supabase se history load karo"""
    if not supabase: return
    try:
        res = supabase.table("memory").select("trade_history").eq("session_id", _TRADES_SESSION_ID).execute()
        if res.data:
            raw = res.data[0].get("trade_history")
            if raw:
                hist = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(hist, list) and hist:
                    auto_trade_stats["trade_history"] = hist
                    # FIX v30: mode default "paper" tha — real history load pe count galat tha
                    wins   = sum(1 for t in hist if t.get("result") == "win" and (t.get("mode") or TRADE_MODE) == TRADE_MODE)
                    losses = sum(1 for t in hist if t.get("result") == "loss" and (t.get("mode") or TRADE_MODE) == TRADE_MODE)
                    auto_trade_stats["trade_count"] = len(hist)
                    auto_trade_stats["win_count"]   = wins
                    auto_trade_stats["loss_count"]  = losses
                    print(f"✅ Trade history loaded: {len(hist)} trades (wins={wins} losses={losses})")
    except Exception as e:
        print(f"⚠️ Trade history load error: {e}")

# ══════════════════════════════════════════════
_rug_dna: list = []

# Stage2 info cache — 0.5s TTL for faster polling
_info_cache: dict = {}  # {addr_lower: {'data': {...}, 'ts': float}}
   # [{"creator": str, "buy_tax": float, "sell_tax": float, "liq_usd": float, "ts": float}]
_RUG_DNA_MAX = 2000  # memory cap

def _record_rug_dna(token_address: str, creator: str, buy_tax: float, sell_tax: float, liq_usd: float, reason: str = "", pnl_pct: float = 0.0):
    """Rug token ka DNA fingerprint save karo — smart dedup + smart cleanup"""
    if not creator or len(creator) != 42: return
    creator_l = creator.lower()
    token_l   = token_address.lower()

    # Agar same creator + same token already hai → update karo, duplicate mat banao
    for existing in _rug_dna:
        if existing.get("creator") == creator_l and existing.get("token") == token_l:
            existing["rug_count"] = existing.get("rug_count", 1) + 1
            existing["last_pnl"]  = round(pnl_pct, 1)
            existing["ts"]        = time.time()
            existing["reason"]    = reason or existing.get("reason", "SL/Rug")
            print(f"🧬 Rug DNA updated: creator={creator[:10]} rugs={existing['rug_count']}")
            return

    dna = {
        "token":     token_l,
        "creator":   creator_l,
        "buy_tax":   round(buy_tax,  1),
        "sell_tax":  round(sell_tax, 1),
        "liq_band":  _liq_band(liq_usd),
        "reason":    reason or "SL/Rug",
        "pnl_pct":   round(pnl_pct, 1),
        "rug_count": 1,
        "ts":        time.time()
    }
    _rug_dna.append(dna)

    # Smart cleanup — sirf 1-rug wale creators ki oldest entry hatao
    # Serial ruggers (2+ rugs) ke records hamesha safe rahenge
    if len(_rug_dna) > _RUG_DNA_MAX:
        # Sirf 1 rug wale entries sort by oldest
        single_rug = [(i, d) for i, d in enumerate(_rug_dna) if d.get("rug_count", 1) == 1]
        if single_rug:
            # Sabse purana single-rug entry hatao
            oldest_idx = min(single_rug, key=lambda x: x[1].get("ts", 0))[0]
            _rug_dna.pop(oldest_idx)
        else:
            # Sab serial ruggers hain — sabse purana hatao (last resort)
            _rug_dna.pop(0)
    print(f"🧬 Rug DNA recorded: creator={creator[:10]} tax={buy_tax:.0f}/{sell_tax:.0f}% liq_band={dna['liq_band']}")

def _liq_band(liq_usd: float) -> str:
    """Liquidity ko band mein group karo — rough match ke liye"""
    if liq_usd < 1_000:    return "micro"
    if liq_usd < 5_000:    return "tiny"
    if liq_usd < 15_000:   return "small"
    if liq_usd < 50_000:   return "medium"
    return "large"

def _check_rug_dna(creator: str, buy_tax: float, sell_tax: float, liq_usd: float) -> dict:
    """
    Naye token ka DNA existing rug fingerprints se match karo.
    Returns: {"match": bool, "confidence": int, "reason": str}
    """
    if not creator or not _rug_dna:
        return {"match": False}

    creator_l  = creator.lower()
    liq_b      = _liq_band(liq_usd)
    bt         = round(buy_tax,  1)
    st         = round(sell_tax, 1)

    creator_rugs = [d for d in _rug_dna if d["creator"] == creator_l]
    tax_matches  = [d for d in _rug_dna
                    if abs(d["buy_tax"] - bt) <= 2.0          # ±2% tax tolerance
                    and abs(d["sell_tax"] - st) <= 2.0
                    and d["liq_band"] == liq_b]

    # Creator ne pehle rug kiya — sabse strong signal
    if creator_rugs:
        return {
            "match":      True,
            "confidence": 95,
            "reason":     f"Creator ne {len(creator_rugs)}x pehle rug kiya ({creator[:10]})"
        }

    # Same tax + same liq band = suspicious pattern
    if len(tax_matches) >= 2:
        return {
            "match":      True,
            "confidence": 70,
            "reason":     f"Rug DNA match: {len(tax_matches)} tokens same tax={bt}/{st}% liq={liq_b}"
        }

    return {"match": False}



# _perf_tracker and _relationship removed — RAM optimization

# RAM-optimized self_awareness — only functional fields kept
self_awareness = {
    "identity": {
        "name":       "MrBlack",
        "version":    "4.0",
        "born_at":    BIRTH_TIME.isoformat(),
        "deployment": os.getenv("RENDER_SERVICE_NAME", "local")
},
    "performance_intelligence": {
        "overall_accuracy": 0.0,
        "trading_iq":       50
},
    "cognitive_state": {
        "mood":            "FOCUSED",
        "active_warnings": []
},
    "current_state": {
        "status":        "ONLINE",
        "uptime_seconds": 0,
        "errors_today":  0
},
    "growth_tracking": {
        "milestones": []
},
    "introspection_log": []
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
        "general_chat":      0
},
    "user_pain_points": [],
    "milestones": []
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
                "pattern_database": _safe_json(row.get("pattern_database"), {})
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
    for _attempt in range(3):
        try:
            sess = sessions.get(session_id, {})
            extra = {}
            if session_id == AUTO_SESSION_ID:
                extra["pattern_database"] = {
                    "total_buys":    auto_trade_stats.get("total_auto_buys", 0),
                    "total_sells":   auto_trade_stats.get("total_auto_sells", 0),
                    "pnl_total":     auto_trade_stats.get("auto_pnl_total", 0.0),
                    "last_action":   auto_trade_stats.get("last_action", ""),
                    "trade_history": list(auto_trade_stats.get("trade_history", []))[-500:],
                    "total_scanned": max(len(discovered_addresses), brain.get("total_tokens_discovered_ever", 0)),
                    "wins":          auto_trade_stats.get("wins", 0),
                    "losses":        auto_trade_stats.get("losses", 0),
                    "today_wins":    auto_trade_stats.get("today_wins",   0),
                    "today_losses":  auto_trade_stats.get("today_losses", 0),
                    "today_pnl":     auto_trade_stats.get("today_pnl",    0.0),
                    "today_date":    auto_trade_stats.get("today_date",   "")
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
            }, on_conflict="session_id").execute()
            return  # success
        except Exception as e:
            if _attempt < 2:
                time.sleep(2)
            else:
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
                "mode":           v.get("mode", "paper")
}
            for k, v in auto_trade_stats["running_positions"].items()
        }
        sessions[AUTO_SESSION_ID] = _ss
        threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
        print(f"💾 Positions persisted: {len(_ss['open_positions'])} positions saved to DB")
    except Exception as _pe:
        print(f"⚠️ _persist_positions error: {_pe}")


# ========== NEW PAIRS ==========

# ── Real-time Bot Event Log ──
# Har action yahan log hoga — UI pe live dikhega
_bot_log: deque = deque(maxlen=100)  # last 100 events
new_pairs_queue: deque = deque(maxlen=30)

# ── NOTIFICATION SYSTEM ──
import uuid as _uuid
_notifications: list = []  # [{id, type, severity, title, detail, token, address, ts, read}]
_NOTIF_MAX = 200

def _push_notif(severity: str, title: str, detail: str, token: str = "", address: str = ""):
    """
    severity: "critical" (red blink) | "warning" (yellow blink) | "success" (green) | "info" (blue)
    """
    notif = {
        "id":       str(_uuid.uuid4())[:8],
        "severity": severity,
        "title":    title,
        "detail":   detail,
        "token":    token,
        "address":  address,
        "ts":       _now_ist(),
        "ts_epoch": __import__("time").time(),
        "read":     False
}
    _notifications.insert(0, notif)
    if len(_notifications) > _NOTIF_MAX:
        _notifications.pop()
    # Save to Supabase in background
    threading.Thread(target=_save_notifs_to_db, daemon=True).start()

def _save_notifs_to_db():
    if not supabase: return
    try:
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_NOTIFICATIONS",
            "role":       "system",
            "content":    "",
            "history":    __import__("json").dumps(_notifications[:_NOTIF_MAX]),
            "updated_at": __import__("datetime").datetime.utcnow().isoformat()
}, on_conflict="session_id").execute()
    except Exception as _e:
        pass  # Silent fail — notifications are best-effort

def _load_notifs_from_db():
    global _notifications
    if not supabase: return
    try:
        res = supabase.table("memory").select("history").eq("session_id", "MRBLACK_NOTIFICATIONS").execute()
        if res.data and res.data[0].get("history"):
            raw = res.data[0]["history"]
            loaded = __import__("json").loads(raw) if isinstance(raw, str) else raw
            if isinstance(loaded, list):
                _notifications = loaded[:_NOTIF_MAX]
                print(f"🔔 Notifications loaded: {len(_notifications)}")
    except Exception as _e:
        pass

def _log(event_type: str, token: str, detail: str, address: str = ""):
    """Bot event log mein entry add karo"""
    _bot_log.appendleft({
        "type":    event_type,   # discover|reject|pass|buy|sell|whale|rug
        "token":   token,
        "detail":  detail,
        "address": address,
        "ts":      _now_ist()
})
discovered_addresses: dict = {}
_discovered_lock  = threading.Lock()          # RACE FIX: protect discovered_addresses

# ── Queue-based Engine — Zero Drop ──────────────────────────
import queue as _queue_module
_discovery_queue  = _queue_module.Queue()   # PC pre-filter queue — infinite, zero drop
_checklist_queue  = _queue_module.Queue()   # PC checklist queue — infinite, zero drop
_fm_queue         = _queue_module.Queue()   # FM snipe queue — infinite, zero drop
# GoPlus semaphore removed — parallel chalega Honeypot ke saath
_goplus_sem       = threading.Semaphore(5)  # GoPlus: 5 parallel safe (was 2)
DISCOVERY_TTL = 7200
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

# ========== MONITORED POSITIONS ==========
monitored_positions: Dict[str, dict] = {}
monitor_lock = threading.Lock()

# ========== AUTO TRADE STATS ==========  FIX 2: trade_history added
AUTO_TRADE_ENABLED = True
FM_SNIPER_ENABLED  = True   # FM Bonding Curve sniper

# Stop events for full WSS shutdown
_fm_stop_event = threading.Event()
_pc_stop_event = threading.Event()

def _load_sniper_state():
    """Supabase se sniper state load karo — restart pe same state rahega"""
    global FM_SNIPER_ENABLED
    if not supabase: return
    try:
        res = supabase.table("memory").select("positions").eq("session_id", "SNIPER_STATE").execute()
        if res.data:
            state = json.loads(res.data[0].get("positions") or "{}")
            FM_SNIPER_ENABLED = state.get("fm", True)
            print(f"✅ Sniper state loaded — FM:{FM_SNIPER_ENABLED}")
    except Exception as e:
        print(f"⚠️ Sniper state load error: {e}")

def _save_sniper_state():
    """Sniper state Supabase mein save karo"""
    if not supabase: return
    try:
        supabase.table("memory").upsert({
            "session_id": "SNIPER_STATE",
            "role": "system",
            "content": "",
            "history": json.dumps([]),
            "positions": json.dumps({"pc": False, "fm": FM_SNIPER_ENABLED}),
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="session_id").execute()
    except Exception as e:
        print(f"⚠️ Sniper state save error: {e}")
TRADE_MODE         = "paper"   # "paper" or "real"
REAL_WALLET        = ""        # user wallet address

# Checklist thresholds — user can edit from UI
CHECKLIST_SETTINGS = {
    "min_liq_bnb":       2.0,    # Stage 1: Min liquidity BNB
    "min_liq_locked":   80.0,    # Stage 1: Min liquidity locked %
    "max_buy_tax":       8.0,    # Stage 1: Max buy tax %
    "max_sell_tax":      8.0,    # Stage 1: Max sell tax %
    "max_top_holder":   20.0,    # Stage 1: Max top holder % (loosened for new tokens)
    "max_top10":        45.0,    # Stage 1: Max top10 holders % (loosened for new tokens)
    "max_creator_pct":  10.0,    # Stage 7: Max dev/creator wallet %
    "max_owner_pct":    10.0,    # Stage 7: Max owner wallet %
    "max_whale_top10":  45.0,    # Stage 7: Max whale concentration %
    "min_lp_lock":      80.0,    # Stage 8: Min LP lock %
    "min_token_age":     3.0,    # Stage 3: Min token age (min)
    "sniper_wait":       5.0,    # Stage 3: Sniper pump over (min)
    "min_volume_24h":  1000.0,   # Stage 4: Min 24h volume USD
    "sl_new":           10.0,    # Stage 10: SL % for new tokens — tighter exit
    "sl_hyped":         20.0,    # Stage 10: SL % for hyped tokens
    "sl_mature":        10.0,    # Stage 10: SL % for mature tokens
    "score_safe":       50.0,    # Auto buy: SAFE min score % (raised from 40)
    "score_caution":    50.0,    # Auto buy: CAUTION min score % (CAUTION buys disabled)
    "daily_loss_pct":   50.0,    # Max daily loss % of balance
    "tp1_pct":          30.0,    # Stage 11: TP1 — sell 25%
    "tp2_pct":          50.0,    # Stage 11: TP2 — sell 25%
    "tp3_pct":         100.0,    # Stage 11: TP3 — sell 25%
    "tp4_pct":         200.0,    # Stage 11: TP4 — keep 10%
}
AUTO_BUY_SIZE_BNB  = 0.01
AUTO_MAX_POSITIONS = 50  # max concurrent positions

# FM Filter settings — manually adjustable from UI
_fm_filters = {
    "mc_max":             15000,
    "mc_max_enabled":     True,
    "dev_wallet_max":     10,
    "dev_wallet_enabled": True,
    "vol_min":            0.4,               # v48: 0.8 → 0.4 BNB (faster entry)
    "vol_min_enabled":    True,
    "buyers_min":         5,                 # FIX v33: unchanged — 74% winners pass
    "buyers_min_enabled": True,
    "price_min":          0.05,
    "price_min_enabled":  True,
    "pump_max":           100,
    "pump_max_enabled":   False,
    "stop_loss":          20,
    "stop_loss_enabled":  True,
    "momentum_min":       12,               # v48: 25 → 12% (faster entry confirmation)
    "momentum_min_enabled": True
}
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
    "vol_weak_count":    {},  # FIX v43: consecutive weak vol counter
    "entry_guard_count": {}   # FIX v44: entry confirmation window counter
}

# Telegram removed


# ========== PROCESS NEW TOKEN ==========
def _process_new_token(token_address: str, pair_address: str, source: str = "websocket"):
    global discovered_addresses
    _now = time.time()
    _t_process_start = _now
    _liq_bnb = 0.0
    # ── Already traded token — dobara buy mat karo ──
    try:
        _addr_lower = token_address.lower()
        _already_traded = any(
            t.get("address", "").lower() == _addr_lower
            for t in auto_trade_stats.get("trade_history", [])
        )
        if _already_traded:
            print(f"⏭️ Already traded — skip: {token_address[:10]}")
            return
    except Exception:
        pass
    # ── Checksum pehle karo — lock ke bahar ──
    try:
        token_address = Web3.to_checksum_address(token_address)
    except Exception:
        return

    # ── Check + Set ATOMICALLY ek hi lock mein ──
    # Race condition fix: dono WSS threads same token detect karte the
    with _discovered_lock:
        if _now - discovered_addresses.get(token_address, 0) <= DISCOVERY_TTL:
            return  # Already processing — dusra thread handle kar raha hai
        # Turant set karo — lock ke andar — koi doosra thread pass nahi hoga
        discovered_addresses[token_address] = _now
        # RAM CAP cleanup
        if len(discovered_addresses) > 150:
            cutoff = _now - DISCOVERY_TTL
            for k in [k for k, v in list(discovered_addresses.items()) if v < cutoff][:100]:
                del discovered_addresses[k]
    brain["total_tokens_discovered_ever"] += 1

    token_name   = token_address[:6]
    token_symbol = token_address[:6]

    new_pairs_queue.append({
        "address":    token_address,
        "name":       token_name,
        "symbol":     token_symbol,
        "discovered": datetime.utcnow().isoformat(),
        "liquidity":  0,
        "volume_24h": 0,
        "source":     source
})
    print(f"🆕 [{source}] {token_address[:10]}")

    # DexScreener naam — background mein, detection block nahi hoga
    def _bg_name():
        try:
            nr = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                timeout=5
            )
            if nr.status_code == 200:
                _bsc = [p for p in (nr.json().get("pairs") or []) if p and p.get("chainId") == "bsc"]
                if _bsc:
                    bt = _bsc[0].get("baseToken") or {}
                    _name = bt.get("symbol") or bt.get("name") or token_address[:6]
                    # Update queue entry name
                    for q in new_pairs_queue:
                        if q.get("address") == token_address:
                            q["name"]   = _name
                            q["symbol"] = _name
                            break
        except Exception:
            pass
    threading.Thread(target=_bg_name, daemon=True).start()
    # Scanner stats
    if "fourmeme" not in source.lower() and "FM" not in source:
        _scanner_stats["pc_discovered"] += 1
    _scanner_tick()
    # Queue mein daalo — zero drop, no semaphore blocking
    _discovery_queue.put(token_address)

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
    if TRADE_MODE == "real":
        print(f"🚫 Paper buy BLOCKED — Real mode active")
        return
    sess = get_or_create_session(AUTO_SESSION_ID)

    # Daily loss reset — naye din pe ya stale value pe
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if sess.get("daily_loss_date", "") != today or sess.get("daily_loss", 0) > 10:
        print(f"🔄 Resetting daily_loss (was {sess.get('daily_loss',0):.4f} BNB)")
        sess["daily_loss"] = 0.0
        sess["daily_loss_date"] = today
    _balance = sess.get("paper_balance", 5.0) or 5.0
    _daily_limit = _balance * (CHECKLIST_SETTINGS.get("daily_loss_pct", 50.0) / 100)
    if sess.get("daily_loss", 0) >= _daily_limit:
        print(f"🛑 Auto-buy BLOCKED: daily_loss={sess.get('daily_loss',0):.4f} BNB >= {_daily_limit:.4f} BNB")
        _push_notif("warning", "🟡 Daily Loss Limit", f"Daily loss limit reached: {sess.get('daily_loss',0):.4f} BNB — Trading paused till midnight UTC")
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
        _log("reject", token_name or address[:8], f"⚠️ Low balance: {paper_balance:.4f} BNB — trade skipped", address)
        return

    # ── 80% Capital Cap — 20% gas reserve ──
    _total_invested = sum(p.get("size_bnb", 0) for p in auto_trade_stats["running_positions"].values())
    _max_deploy     = paper_balance * 0.80  # 80% max, 20% gas reserve
    if _total_invested >= _max_deploy:
        print(f"🛑 Auto-buy BLOCKED: capital cap hit — invested={_total_invested:.4f} BNB >= 80% of {paper_balance:.4f} BNB")
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
    # ── BUY SIZE: seedha AUTO_BUY_SIZE_BNB use karo ──
    size_bnb = round(AUTO_BUY_SIZE_BNB, 4)

    # ── ANTI-MEV: amount randomize (both modes) ──
    size_bnb = _anti_mev_amount(size_bnb)  # ±3% noise

    # ── REAL vs PAPER execution ──
    _buy_tax  = float((checklist_result.get("dex_data") or {}).get("buy_tax",  0) or 0)
    _sell_tax = float((checklist_result.get("dex_data") or {}).get("sell_tax", 0) or 0)

    if TRADE_MODE == "real":
        _real_result = real_buy_token(address, size_bnb, _buy_tax, _sell_tax)
        if not _real_result.get("success"):
            print(f"❌ REAL BUY failed: {_real_result.get('error','?')}")
            return  # real buy fail → position nahi kholte
        # Real buy successful → entry price on-chain se lo
        if _real_result.get("entry_price", 0) > 0:
            entry_price = _real_result["entry_price"]
        print(f"✅ REAL BUY executed: tx={_real_result.get('tx_hash','')[:20]}")
    # Paper mode: balance simulate karo (real mode mein skip)
    if TRADE_MODE != "real":
        sess["paper_balance"] = round(paper_balance - size_bnb, 6)
    _sl = CHECKLIST_SETTINGS.get("sl_new", 15.0)
    add_position_to_monitor(AUTO_SESSION_ID, address, token_name or address[:10], entry_price, size_bnb, stop_loss_pct=_sl)
    _bnb_at_buy = market_cache.get("bnb_price", 0)  # real only — DataGuard already verified
    # ── Buy Reasoning — kyu buy kiya ──
    _dex_d = checklist_result.get("dex_data", {}) or {}
    _buy_signals = [s["type"] for s in (checklist_result.get("green_signals") or [])]
    _buy_reasoning = {
        "score":       score,
        "total":       total,
        "score_pct":   round(score / max(total, 1) * 100, 1),
        "signals":     _buy_signals,
        "mc_usd":      round(float(_dex_d.get("fdv", 0) or 0), 0),
        "liq_usd":     round(float(_dex_d.get("liquidity_usd", 0) or 0), 0),
        "buys_5m":     int(_dex_d.get("buys_5m", 0) or 0),
        "sells_5m":    int(_dex_d.get("sells_5m", 0) or 0),
        "assumption":  f"Score {score}/{total} SAFE, signals: {', '.join(_buy_signals[:3]) if _buy_signals else 'checklist only'}",
        "ts":          datetime.utcnow().isoformat()
}
    auto_trade_stats["running_positions"][address] = {
        "token":          token_name or address[:10],
        "entry":          entry_price,
        "size_bnb":       size_bnb,
        "orig_size_bnb":  size_bnb,
        "bought_usd":     round(size_bnb * _bnb_at_buy, 2),
        "sl_pct":         CHECKLIST_SETTINGS.get("sl_new", 15.0),
        "trail_pct":      20.0,
        "tp_sold":        0.0,
        "banked_pnl_bnb": 0.0,
        "bought_at":      datetime.utcnow().isoformat(),
        "mode":           TRADE_MODE,
        "buy_reasoning":  _buy_reasoning,
        "buy_tax":        _buy_tax,   # FIX2: sell slippage ke liye zaroori
        "sell_tax":       _sell_tax,  # FIX2: sell slippage ke liye zaroori
    }
    auto_trade_stats["total_auto_buys"] += 1
    auto_trade_stats["last_action"] = f"BUY {token_name or address[:10]}"
    _push_notif("success", f"🟢 Buy Executed", f"{token_name or address[:10]} @ {entry_price:.2e} BNB | Size: {size_bnb:.4f} BNB", token_name or address[:10], address)
    _log("buy", token_name or address[:10], f"🟢 BUY {size_bnb:.4f} BNB @ ${entry_price:.8f}", address)
    # ✅ Pair register karo — known pair pass karo taaki BSC call na ho (instant!)
    _known_pair = (checklist_result.get("dex_data") or {}).get("pair_address", "")
    _register_position_pair(address, known_pair=_known_pair if _known_pair else None)
    if not isinstance(sess.get("positions"), list):
        sess["positions"] = []
    sess["positions"].append({
        "address": address, "token": token_name or address[:10], "entry": entry_price, "size_bnb": size_bnb, "type": "auto"
    })
    if len(sess["positions"]) > 20:
        sess["positions"] = sess["positions"][-20:]  # ✅ memory leak fix
    _persist_positions()  # ✅ FIX: full data save with tp_sold, sl_pct, bought_usd
    _scanner_stats["pc_bought"] += 1
    print(f"AUTO BUY: {address[:10]} @ {entry_price:.10f} size={size_bnb:.4f}")

# ========== AUTO PAPER SELL ==========  FIX 3: All variable names fixed
def _auto_paper_sell(address, reason, sell_pct=100.0):
    if address not in auto_trade_stats["running_positions"]:
        return
    pos = auto_trade_stats["running_positions"][address]
    with monitor_lock:
        mon = monitored_positions.get(address, {})

    entry   = pos.get("entry", 0)
    current = mon.get("current", entry)
    size    = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
    token   = pos.get("token", address[:10])
    bought_at_str = pos.get("bought_at", "")

    if entry <= 0:
        return

    if current <= 0:
        current = 0
        pnl_pct    = -100.0
        sell_size  = size * (sell_pct / 100.0)
        pnl_bnb    = -sell_size
        return_bnb = 0.0
    else:
        current = current * 0.995
        pnl_pct    = ((current - entry) / entry) * 100
        sell_size  = size * (sell_pct / 100.0)
        pnl_bnb    = sell_size * (pnl_pct / 100.0)
        return_bnb = sell_size * (1 + pnl_pct / 100.0)

    sess = get_or_create_session(AUTO_SESSION_ID)
    
    # ========== FIX #7: REAL SELL PEHLE ==========
    real_sell_success = False
    real_sell_result = None
    
    if TRADE_MODE == "real":
        _buy_tax_s  = float(pos.get("buy_tax",  0) or 0)
        _sell_tax_s = float(pos.get("sell_tax", 0) or 0)
        _source     = pos.get("source", "") or pos.get("buy_reasoning", {}).get("source", "")

        if "FM_BC" in _source:
            _w3_sell   = _get_w3q() or _fm_get_w3()
            _graduated = False
            _fm_factory = pos.get("fm_factory", _FM_FACTORY_ADDR)
            if _w3_sell:
                try:
                    _info_sell = _fm_get_token_info(address, _w3_sell)
                    if _info_sell:
                        _graduated = _info_sell.get("liquidityAdded", False)
                        # FIX: har token ka alag tokenManager hota hai
                        # tokenManager = actual sell contract, factory nahi
                        _tm = _info_sell.get("tokenManager", "")
                        if _tm and _tm != "0x0000000000000000000000000000000000000000":
                            _fm_factory = _tm
                            print(f"✅ [FM] tokenManager: {_tm[:10]} for {address[:10]}")
                except Exception as _te:
                    print(f"⚠️ [FM] tokenInfo sell error: {str(_te)[:40]}")

            if not _graduated:
                _real_sell = _fm_real_sell_bc(address, sell_pct, _fm_factory, _w3_sell)
            else:
                _real_sell = real_sell_token(address, sell_pct, _buy_tax_s, _sell_tax_s)
        else:
            _real_sell = real_sell_token(address, sell_pct, _buy_tax_s, _sell_tax_s)
        
        if _real_sell.get("success"):
            real_sell_success = True
            real_sell_result = _real_sell
        else:
            _fail_err = _real_sell.get("error", "?")
            print(f"❌ REAL SELL FAILED — paper state NOT updated: {_fail_err}")
            _push_notif("critical", "🔴 Sell Failed",
                       f"Real sell failed: {_fail_err} — position still open", token, address)
            # FIX v24: Failed sell bhi history mein save karo
            try:
                if not isinstance(auto_trade_stats.get("trade_history"), list):
                    auto_trade_stats["trade_history"] = []
                _orig_sz_f  = pos.get("orig_size_bnb", size)
                _buy_rsn_f  = pos.get("buy_reasoning", {}) or {}
                auto_trade_stats["trade_history"].append({
                    "token":       token,
                    "address":     address,
                    "entry":       entry,
                    "exit":        current,
                    "exit_price":  current,
                    "pnl_pct":     round(pnl_pct, 2),
                    "pnl_bnb":     round(pnl_bnb, 6),
                    "size_bnb":    _orig_sz_f,
                    "bought_at":   bought_at_str,
                    "sold_at":     datetime.utcnow().isoformat(),
                    "result":      "sell_failed",
                    "exit_reason": f"SELL FAILED: {_fail_err}",
                    "mode":        "real",
                    "tx_hash":     "",
                    "buy_reasoning": _buy_rsn_f
})
                import threading as _th24
                _th24.Thread(target=_save_trade_history_to_db, daemon=True).start()
            except Exception as _fe:
                print(f"⚠️ [v24] Failed sell history save error: {_fe}")
            return  # Early exit — position still open
    
    # ========== AB PAPER STATE UPDATE (agar real success ya paper mode) ==========
    if TRADE_MODE != "real" or real_sell_success:
        sess["paper_balance"] = round(sess.get("paper_balance", 5.0) + return_bnb, 6)
        
        if sell_pct >= 100.0:
            auto_trade_stats["auto_pnl_total"] += pnl_pct
            # FIX: sirf final 100% close pe count karo
            auto_trade_stats["total_auto_sells"] += 1
        
        _banked = pos.get("banked_pnl_bnb", 0.0)
        pos["banked_pnl_bnb"] = round(_banked + pnl_bnb, 6)
        
        if sell_pct >= 100.0:
            if not isinstance(auto_trade_stats.get("trade_history"), list):
                auto_trade_stats["trade_history"] = []
            _bnb_at_sell = market_cache.get("bnb_price", 0)
            _saved_bought_usd = auto_trade_stats["running_positions"].get(address, {}).get("bought_usd", 0)
            _orig_sz = pos.get("orig_size_bnb", size)
            _total_pnl_bnb_trade = round(pos.get("banked_pnl_bnb", 0.0), 6)
            _total_pnl_pct_trade = round((_total_pnl_bnb_trade / _orig_sz * 100), 2) if _orig_sz > 0 else pnl_pct
            _gas_bnb_sell = DataGuard.get_real_gas_bnb()
            
            _buy_rsn = pos.get("buy_reasoning", {}) or {}
            _assumption = _buy_rsn.get("assumption", "N/A")
            _signals_used = _buy_rsn.get("signals", [])
            if _total_pnl_pct_trade > 0:
                _post_mortem = (f"WIN +{_total_pnl_pct_trade:.1f}% | Exit: {reason} | "f"Entry: {entry:.2e} BNB | ExitPrice: {current:.2e} BNB | "f"HoldTime: {round((datetime.utcnow() - datetime.fromisoformat(bought_at_str[:19])).total_seconds()/60, 1) if bought_at_str else 0:.0f}min | "f"Signals used: {', '.join(_signals_used[:3]) if _signals_used else 'checklist only'} | "f"BNB at sell: {market_cache.get('bnb_price', 0):.2f} | Mode: {TRADE_MODE}")
            else:
                _post_mortem = (f"LOSS {_total_pnl_pct_trade:.1f}% | Exit: {reason} | "f"Entry: {entry:.2e} BNB | ExitPrice: {current:.2e} BNB | "f"HoldTime: {round((datetime.utcnow() - datetime.fromisoformat(bought_at_str[:19])).total_seconds()/60, 1) if bought_at_str else 0:.0f}min | "f"Assumption: {_assumption[:80]} | "f"Signals used: {', '.join(_signals_used[:3]) if _signals_used else 'none'} | "f"BNB at sell: {market_cache.get('bnb_price', 0):.2f} | Mode: {TRADE_MODE}")
            auto_trade_stats["trade_history"].append({
                "token":        token,
                "address":      address,
                "entry":        entry,
                "exit":         current,
                "exit_price":   current,
                "pnl_pct":      _total_pnl_pct_trade,
                "pnl_bnb":      _total_pnl_bnb_trade,
                "size_bnb":     _orig_sz,
                "gas_bnb":      _gas_bnb_sell,
                "bought_usd":   _saved_bought_usd if _saved_bought_usd else round(_orig_sz * _bnb_at_sell, 2),
                "sold_usd":     round(max(0.0, (_saved_bought_usd / _bnb_at_sell if _bnb_at_sell > 0 else _orig_sz) + _total_pnl_bnb_trade) * _bnb_at_sell, 2) if _bnb_at_sell > 0 else 0,
                "bought_at":    bought_at_str,
                "sold_at":      datetime.utcnow().isoformat(),
                "result":       "win" if _total_pnl_pct_trade > 0 else "loss",
                "exit_reason":  reason,
                "reason":       reason,
                "mode":         pos.get("mode", TRADE_MODE),
                "tp_events":    pos.get("tp_events", []),
                "buy_reasoning":_buy_rsn,
                "post_mortem":  _post_mortem,
                "signals_used": _signals_used,
                "snipe_source": _buy_rsn.get("source", "checklist"),
                "snipe_strategy": _buy_rsn.get("strategy", "Normal_Checklist"),
                "ath_price":    pos.get("ath_price") or monitored_positions.get(address, {}).get("high", current),
                "ath_pct":      round(((pos.get("ath_price") or monitored_positions.get(address, {}).get("high", current)) - entry) / entry * 100, 1) if entry > 0 else 0,
                "pnl_high":     pos.get("pnl_high", 0.0),
                "hold_minutes": round((datetime.utcnow() - datetime.fromisoformat(bought_at_str[:19])).total_seconds() / 60, 1) if bought_at_str else 0
})
        if len(auto_trade_stats["trade_history"]) > 500:
            # FIX v50: in-memory 500 kaafi — Supabase mein full history hai
            auto_trade_stats["trade_history"] = auto_trade_stats["trade_history"][-500:]
        
        if sell_pct >= 100.0:
            try:
                _hold_min  = round((datetime.utcnow() - datetime.fromisoformat(bought_at_str[:19])).total_seconds() / 60, 1) if bought_at_str else 0
                _peak      = monitored_positions.get(address, {}).get("peak_price", current)
                _left_pct  = round((_peak - current) / _peak * 100, 1) if _peak and _peak > 0 and current > 0 else 0
                _exit_type = ("tp_hit"   if ("TP" in reason or "Profit" in reason)
                              else "sl_hit"   if ("SL" in reason or "HardSL" in reason)
                              else "mom_dead" if "MomDead" in reason
                              else "rug"      if ("Rug" in reason or "Dump" in reason or "LP Burn" in reason)
                              else "manual")
                _fg_sell   = market_cache.get("fear_greed", 50)
                _mkt_sell  = "bullish" if _fg_sell >= 60 else ("bearish" if _fg_sell <= 35 else "neutral")
                threading.Thread(target=_save_bot_decision, args=({
                    "token_address":      address,
                    "token_name":         token,
                    "decision":           "SELL",
                    "reason":             reason,
                    "thought":            _post_mortem,
                    "pnl_pct":            _total_pnl_pct_trade,
                    "exit_reason":        reason,
                    "exit_type":          _exit_type,
                    "hold_time_min":      _hold_min,
                    "peak_price":         _peak,
                    "left_on_table_pct":  _left_pct,
                    "entry_price":        entry,
                    "exit_price":         current,
                    "bnb_price_at_entry": market_cache.get("bnb_price", 0),
                    "fear_greed_at_entry":_fg_sell,
                    "market_condition":   _mkt_sell,
                    "token_type":         "meme",
                    "pnl_high":           pos.get("pnl_high", 0.0),
                    "exit_zone":          ("moonbag" if pos.get("tp_sold", 0) >= 80 else ("post_tp1" if pos.get("tp_sold", 0) >= 50 else "pre_tp"))
},), daemon=True).start()
            except Exception as _de:
                print(f"⚠️ sell decision log error: {_de}")
        
        threading.Thread(target=_save_trade_history_to_db, daemon=True).start()
        
        if "VolRug" in reason or "LP Burn" in reason or "LiqDrop" in reason or "Dump" in reason:
            try:
                _gp       = _get_goplus(address)
                _creator  = _gp.get("creator_address", "")
                _buy_tax  = float(_gp.get("buy_tax",  0) or 0)
                _sell_tax = float(_gp.get("sell_tax", 0) or 0)
                _liq_usd  = float(auto_trade_stats["running_positions"].get(address, pos).get("bought_usd", 0) or 0)
                if _creator and len(_creator) == 42:
                    blacklist_dev(_creator, f"SL/Rug on {token} {reason}")
                blacklist_token(address, f"{reason} pnl={pnl_pct:.0f}%")
                _record_rug_dna(address, _creator or "unknown", _buy_tax, _sell_tax, _liq_usd, reason=reason, pnl_pct=pnl_pct)
            except Exception: pass
        
        auto_trade_stats["last_action"] = f"SELL {sell_pct:.0f}% {token} PnL:{pnl_pct:+.1f}%"
        if "SL" in reason or "Rug" in reason or "Dump" in reason:
            _push_notif("warning", f"🟡 Stop Loss Hit", f"{token} | PnL: {pnl_pct:+.1f}% | Reason: {reason}", token, address)
        elif pnl_pct >= 0:
            _push_notif("success", f"🟢 Take Profit", f"{token} sold {sell_pct:.0f}% | PnL: {pnl_pct:+.1f}% | {reason}", token, address)
        _emoji = "🟢" if pnl_pct >= 0 else "🔴"
        _log("sell", token, f"{_emoji} SELL {sell_pct:.0f}% · PnL {pnl_pct:+.1f}% · {reason}", address)
        
        if sell_pct >= 100.0:
            auto_trade_stats["running_positions"].pop(address, None)
            auto_trade_stats["vol_weak_count"].pop(address, None)
            auto_trade_stats["entry_guard_count"].pop(address, None)
            remove_position_from_monitor(address)
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
            _persist_positions()
            log_trade_internal(AUTO_SESSION_ID, {
                "token_address": address,
                "entry_price":   entry,
                "exit_price":    current,
                "pnl_pct":       pnl_pct,
                "sell_pct":      sell_pct,
                "size_bnb":      size,
                "win":           pnl_pct > 0,
                "lesson":        f"Auto: {reason} | PnL:{pnl_pct:+.1f}%"
})
            sess["positions"] = [p for p in sess.get("positions", []) if p.get("address") != address]
        else:
            if not isinstance(sess.get("positions"), list):
                sess["positions"] = []
            pos["size_bnb"]       = size * (1 - sell_pct / 100.0)
            pos["tp_sold"]        = pos.get("tp_sold", 0) + sell_pct
            pos["banked_pnl_bnb"] = round(pos.get("banked_pnl_bnb", 0.0) + pnl_bnb, 6)
            _bnb_at_tp = market_cache.get("bnb_price", 0) or market_cache.get("last_bnb_price", 660)
            _gas_bnb   = DataGuard.get_real_gas_bnb()
            if not isinstance(pos.get("tp_events"), list):
                pos["tp_events"] = []
            if len(pos.get("tp_events", [])) >= 4:
                pos["tp_events"] = pos["tp_events"][-4:]
            pos["tp_events"].append({
                "label":       reason,
                "sell_pct":    sell_pct,
                "exit_price":  round(current, 12),
                "exit_usd":    round(current * _bnb_at_tp, 10),
                "sell_bnb":    round(sell_size, 6),
                "sell_usd":    round(max(0, return_bnb) * _bnb_at_tp, 2),
                "pnl_bnb":     round(pnl_bnb, 6),
                "pnl_pct":     round(pnl_pct, 2),
                "gas_bnb":     _gas_bnb,
                "gas_usd":     round(_gas_bnb * _bnb_at_tp, 3),
                "tokens_sold": round(sell_size / current, 0) if current > 0 else 0,
                "sold_at":     datetime.utcnow().isoformat()
})
            _persist_positions()
        
        if TRADE_MODE == "real" and real_sell_success:
            _actual_gas_used = real_sell_result.get("gas_used", 0)
            _gas_price_wei   = real_sell_result.get("gas_price", 0)
            if _actual_gas_used and _gas_price_wei:
                _actual_gas_bnb = (_actual_gas_used * _gas_price_wei) / 1e18
                _actual_gas_usd = round(_actual_gas_bnb * market_cache.get("bnb_price", 0), 4)
                if pos.get("tp_events"):
                    pos["tp_events"][-1]["gas_bnb"] = round(_actual_gas_bnb, 8)
                    pos["tp_events"][-1]["gas_usd"]  = _actual_gas_usd
                    pos["tp_events"][-1]["tx_hash"]  = real_sell_result.get("tx_hash", "")[:20]
                    _persist_positions()
        
        print(f"AUTO SELL {sell_pct:.0f}%: {address[:10]} PnL:{pnl_pct:+.1f}% [{reason}]")
        threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
        
        if sell_pct >= 100:
            _unregister_position_pair(address)
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
_brain_loaded_from_db = False  # startup pe Supabase se load hua?

def _save_brain_to_db():
    import time as _t
    if not supabase: return
    if _t.time() - _brain_save_cache["last_save"] < 20: return
    _brain_save_cache["last_save"] = _t.time()
    try:
        with _smart_wallets_lock:
            _sw_snapshot = dict(_smart_wallets)
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
                "total_tokens_discovered_ever": brain.get("total_tokens_discovered_ever", 0),
                "smart_wallets":  _sw_snapshot,
                "fm_filters":     _fm_filters,
                "rug_dna":        _rug_dna[-10000:],
                "dev_blacklist":  dict(_dev_blacklist),
                "scanner_stats":  {k: v for k, v in _scanner_stats.items() if not k.startswith("_") and k != "history"}
})
        }, on_conflict="session_id").execute()
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
            # Load smart wallets
            _sw = stored.get("smart_wallets", {})
            if isinstance(_sw, dict) and _sw:
                with _smart_wallets_lock:
                    _smart_wallets.update(_sw)
                print(f"🐋 Smart wallets loaded: {len(_sw)}")
            # Load rug DNA — clear first to avoid duplicates on reload
            _rd = stored.get("rug_dna", [])
            if isinstance(_rd, list) and _rd:
                _rug_dna.clear()
                _rug_dna.extend(_rd)
                print(f"☠️ Rug DNA loaded: {len(_rd)}")
            # Load dev blacklist — permanent
            _db = stored.get("dev_blacklist", {})
            if isinstance(_db, dict) and _db:
                with _dev_blacklist_lock:
                    _dev_blacklist.update(_db)
                print(f"🚫 Dev blacklist loaded: {len(_db)} devs")
            # Load fm_filters — restart pe persist
            _ff = stored.get("fm_filters", {})
            if isinstance(_ff, dict) and _ff:
                global _fm_filters
                for _fk, _fv in _ff.items():
                    if _fk in _fm_filters:
                        _fm_filters[_fk] = _fv
                print(f"⚙️ FM Filters loaded: {_ff}")
            # Force correct values — DB mein purane values ho sakte hain
            _fm_filters["vol_min"]              = 0.4
            _fm_filters["vol_min_enabled"]      = True
            _fm_filters["momentum_min"]         = 12
            _fm_filters["momentum_min_enabled"] = True
            _fm_filters["mc_max"]               = 15000
            _fm_filters["pump_max"]             = 100
            _fm_filters["pump_max_enabled"]     = False
            _fm_filters["stop_loss_enabled"]    = True
            print(f"⚙️ FM Filters forced: vol_min={_fm_filters['vol_min']} momentum_min={_fm_filters['momentum_min']} mc_max={_fm_filters['mc_max']}")
            # Load scanner stats — cumulative counts restore
            _ss = stored.get("scanner_stats", {})
            if isinstance(_ss, dict) and _ss:
                for _k, _v in _ss.items():
                    if _k in _scanner_stats and isinstance(_v, (int, float)):
                        _scanner_stats[_k] = _v
                print(f"📊 Scanner stats loaded: pc={_scanner_stats.get('pc_discovered',0)} fm={_scanner_stats.get('fm_discovered',0)}")
            global _brain_loaded_from_db
            _brain_loaded_from_db = True
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
            token  = t.get("token", "")
            ts     = t.get("sold_at", "")[:10]
            if result == "win" and pnl > 10:
                pat = {
                    "token":    token,
                    "pnl_pct":  pnl,
                    "hold_min": t.get("hold_minutes", 0),
                    "reason":   reason,
                    "signals":  t.get("signals_used", []),
                    "post_mortem": t.get("post_mortem", ""),
                    "ts":       ts
}
                best = brain["trading"]["best_patterns"]
                # String entries clean karo + duplicate check
                brain["trading"]["best_patterns"] = [p for p in best if isinstance(p, dict)]
                if not any(p.get("token") == token and p.get("ts") == ts for p in brain["trading"]["best_patterns"]):
                    brain["trading"]["best_patterns"].append(pat)
                    brain["trading"]["best_patterns"] = brain["trading"]["best_patterns"][-500:]
            elif result == "loss":
                pat = {
                    "token":    token,
                    "pnl_pct":  pnl,
                    "hold_min": t.get("hold_minutes", 0),
                    "reason":   reason,
                    "signals":  t.get("signals_used", []),
                    "post_mortem": t.get("post_mortem", ""),
                    "ts":       ts
}
                avoid = brain["trading"]["avoid_patterns"]
                # String entries clean karo + duplicate check
                brain["trading"]["avoid_patterns"] = [p for p in avoid if isinstance(p, dict)]
                if not any(p.get("token") == token and p.get("ts") == ts for p in brain["trading"]["avoid_patterns"]):
                    brain["trading"]["avoid_patterns"].append(pat)
                    brain["trading"]["avoid_patterns"] = brain["trading"]["avoid_patterns"][-500:]
        brain["trading"]["last_updated"] = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"_learn_trading_patterns error: {e}")


def _deep_llm_learning():
    """Deep learning cycle — trade history se patterns analyze karo"""
    try:
        _ensure_brain_structure()
        history = [t for t in auto_trade_stats.get("trade_history", []) if isinstance(t, dict)]
        if len(history) < 3:
            return  # not enough data yet

        wins   = [t for t in history if t.get("pnl_pct", 0) > 0]
        losses = [t for t in history if t.get("pnl_pct", 0) <= 0]
        total  = len(history)
        win_rate = round(len(wins) / total * 100, 1) if total > 0 else 0

        # ── Win patterns ──
        for t in wins[-20:]:
            pat = {
                "token":    t.get("token", ""),
                "pnl_pct":  t.get("pnl_pct", 0),
                "hold_min": t.get("hold_minutes", 0),
                "ts":       t.get("sold_at", "")[:10]
}
            best = brain["trading"]["best_patterns"]
            if not any(p.get("token") == pat["token"] for p in best):
                best.append(pat)
        brain["trading"]["best_patterns"] = brain["trading"]["best_patterns"][-500:]

        # ── Loss patterns ──
        for t in losses[-20:]:
            pat = {
                "token":    t.get("token", ""),
                "pnl_pct":  t.get("pnl_pct", 0),
                "hold_min": t.get("hold_minutes", 0),
                "reason":   t.get("exit_reason", ""),
                "ts":       t.get("sold_at", "")[:10]
}
            avoid = brain["trading"]["avoid_patterns"]
            if not any(p.get("token") == pat["token"] for p in avoid):
                avoid.append(pat)
        brain["trading"]["avoid_patterns"] = brain["trading"]["avoid_patterns"][-500:]

        # ── Market insights ──
        avg_win_hold  = round(sum(t.get("hold_minutes",0) for t in wins)  / max(len(wins),1),  1)
        avg_loss_hold = round(sum(t.get("hold_minutes",0) for t in losses) / max(len(losses),1), 1)
        avg_win_pnl   = round(sum(t.get("pnl_pct",0) for t in wins)   / max(len(wins),1),   1)
        avg_loss_pnl  = round(sum(t.get("pnl_pct",0) for t in losses) / max(len(losses),1), 1)

        insight = {
            "ts":           datetime.utcnow().isoformat()[:16],
            "total_trades": total,
            "win_rate":     win_rate,
            "avg_win_hold": avg_win_hold,
            "avg_loss_hold":avg_loss_hold,
            "avg_win_pnl":  avg_win_pnl,
            "avg_loss_pnl": avg_loss_pnl
}
        brain["trading"]["market_insights"].append(insight)
        brain["trading"]["market_insights"] = brain["trading"]["market_insights"][-200:]

        # ── Strategy note ──
        note_text = (f"WR={win_rate}% | AvgWin={avg_win_pnl:+.0f}% in {avg_win_hold}m | "
                     f"AvgLoss={avg_loss_pnl:+.0f}% in {avg_loss_hold}m | Trades={total}")
        brain["trading"]["strategy_notes"].append({
            "note": note_text,
            "ts":   datetime.utcnow().isoformat()[:16]
})
        brain["trading"]["strategy_notes"] = brain["trading"]["strategy_notes"][-100:]

        brain["total_learning_cycles"] = brain.get("total_learning_cycles", 0) + 1
        _save_brain_to_db()
        print(f"🧠 Learning cycle #{brain['total_learning_cycles']} | WR={win_rate}% | W:{len(wins)} L:{len(losses)}")
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
    last_fast = last_deep = last_hour = last_bnb_check = 0
    print(f"📚 Learning from cycle #{cycle}")
    _last_price_update = 0  # FIX4: price update tracker

    while True:
        try:
            cycle += 1
            brain["total_learning_cycles"] = cycle
            now = time.time()

            # ══ FIX4: monitored_positions price update — har 3s ══
            # Root cause: WSS swap events se price update nahi hota
            # Yahan har 3s pe get_token_price_bnb se fresh price lo
            if now - _last_price_update >= 3:
                _last_price_update = now
                try:
                    with monitor_lock:
                        _mon_snap = list(monitored_positions.items())
                    for _addr, _mon in _mon_snap:
                        try:
                            # FM BC tokens — getTokenInfo se lastPrice
                            _pos_data = auto_trade_stats.get("running_positions", {}).get(_addr, {})
                            _src = _pos_data.get("source", "") or _pos_data.get("buy_reasoning", {}).get("source", "")
                            if "FM_BC" in _src:
                                try:
                                    _w3f = _fm_get_w3()
                                    if _w3f:
                                        _info = _fm_get_token_info(_addr, _w3f)
                                        if _info and _info.get("lastPrice", 0) > 0:
                                            _bnb_p = market_cache.get("bnb_price", 0)
                                            _quote = str(_info.get("quote", "")).lower()
                                            if "usdt" in _quote or "busd" in _quote or "usd" in _quote:
                                                _price = (_info["lastPrice"] / 1e18) / _bnb_p if _bnb_p > 0 else 0
                                            else:
                                                _price = _info["lastPrice"] / 1e18
                                            if _price > 0:
                                                with monitor_lock:
                                                    if _addr in monitored_positions:
                                                        monitored_positions[_addr]["current"] = _price
                                                        if _price > monitored_positions[_addr].get("high", 0):
                                                            monitored_positions[_addr]["high"] = _price
                                                            _rp = auto_trade_stats["running_positions"].get(_addr)
                                                            if _rp:
                                                                _rp["ath_price"] = _price
                                except Exception:
                                    pass
                            else:
                                # PancakeSwap tokens — on-chain price
                                _price = get_token_price_bnb(_addr)
                                if _price > 0:
                                    with monitor_lock:
                                        if _addr in monitored_positions:
                                            monitored_positions[_addr]["current"] = _price
                                            if _price > monitored_positions[_addr].get("high", 0):
                                                monitored_positions[_addr]["high"] = _price
                                                _rp2 = auto_trade_stats["running_positions"].get(_addr)
                                                if _rp2:
                                                    _rp2["ath_price"] = _price
                        except Exception:
                            pass
                except Exception as _pe:
                    pass
            # ══ FIX4 END ══

            # BNB price backup — har 30s check karo (dedicated loop se alag)
            if now - last_bnb_check >= 30:
                last_bnb_check = now
                try:
                    _bnb_age = 9999
                    _ts = market_cache.get("last_updated")
                    if _ts:
                        try: _bnb_age = (datetime.utcnow() - datetime.fromisoformat(_ts.replace("Z",""))).total_seconds()
                        except: pass
                    if _bnb_age > 30:  # dedicated loop fail hua — backup se lo
                        fetch_market_data()
                except Exception as e:
                    print(f"BNB backup fetch error: {e}")

            # PancakeSwap trending — har 10 min (BNB fetch se alag)
            if now - last_deep >= 600:
                try:
                    fetch_pancakeswap_data()
                except Exception: pass

            # Brain patterns learn — har 2 min
            if now - last_fast >= 120:
                last_fast = now
                _learn_trading_patterns()
                _learn_from_new_pairs()
                try:
                    if supabase:
                        supabase.table("memory").upsert({
                            "session_id": "MRBLACK_CYCLE",
                            "role":       "system",
                            "content":    str(cycle),
                            "updated_at": datetime.utcnow().isoformat()
                        }, on_conflict="session_id").execute()
                except Exception: pass

            # Deep LLM + brain save — har 10 min
            if now - last_deep >= 600:
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
        gc.collect()
        time.sleep(30)  # har 30s — BNB backup check ke liye zaroori

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

_pair_to_token: dict = {}  # {pair_lower: {token, pair, ts}}

def auto_position_manager():
    print("Auto Position Manager started!")
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
    if not isinstance(auto_trade_stats.get("trade_history"), list):
        auto_trade_stats["trade_history"] = []
    while True:
        for addr, pos in list(auto_trade_stats["running_positions"].items()):
            try:
                with monitor_lock:
                    mon = monitored_positions.get(addr, {})
                current = mon.get("current", 0)
                _pos_data = auto_trade_stats["running_positions"].get(addr, pos)
                entry   = _pos_data.get("entry", 0)
                high    = mon.get("high", entry)
                # ── FIX v36 Bug1: ATH race condition — price_monitor ka wait mat karo ──
                # Fast dump coins mein monitor pehla update karne se pehle HardSL fire
                # hota tha. Same iteration mein high sync karo.
                if current > 0 and current > high:
                    high = current
                    with monitor_lock:
                        if addr in monitored_positions:
                            monitored_positions[addr]["high"] = current
                    _rp3 = auto_trade_stats["running_positions"].get(addr)
                    if _rp3:
                        _rp3["ath_price"] = current
                tp_sold = _pos_data.get("tp_sold", 0.0)
                sl_pct  = _pos_data.get("sl_pct", 15.0)
                if entry <= 0:
                    continue

                if current <= 0:
                    _zero_count = _pos_data.get("_zero_price_count", 0) + 1
                    _pos_data["_zero_price_count"] = _zero_count
                    print(f"⚠️ Price=0: {addr[:10]} count={_zero_count}/3")
                    if _zero_count >= 3:
                        print(f"🚨 RUG: {addr[:10]} price=0 x3 → force close")
                        _auto_paper_sell(addr, "🚨 RUG price=0", 100.0)
                    continue
                else:
                    _pos_data["_zero_price_count"] = 0

                pnl     = ((current - entry) / entry) * 100
                drop_hi = ((current - high) / high) * 100 if high > 0 else 0
                _cs   = CHECKLIST_SETTINGS
                _tp3  = _cs.get("tp3_pct", 100.0)
                _tp4  = _cs.get("tp4_pct", 200.0)

                _vol     = _get_vol_pressure_rt(addr)
                _bv5     = _vol.get("buy_vol5",  0.0)
                _sv5     = _vol.get("sell_vol5", 0.0)
                _b5      = _vol.get("buys5",     0)
                _s5      = _vol.get("sells5",    0)
                _has_vol = (_bv5 > 0 or _sv5 > 0 or _b5 > 0 or _s5 > 0)

                _trail_triggered = False

                with _lp_burn_lock:
                    _burn_detected = addr.lower() in _lp_burn_alerts
                if _burn_detected:
                    print(f"🚨 LP Burn confirmed sell: {addr[:10]}")
                    _auto_paper_sell(addr, "LP Burn 🚨 Rug Confirmed", 100.0)
                    with _lp_burn_lock:
                        _lp_burn_alerts.discard(addr.lower())
                    continue

                _now_t = time.time()
                _last_res_t = _pos_data.get("_last_res_t", 0)
                if _now_t - _last_res_t >= 3:
                    _pos_data["_last_res_t"] = _now_t
                    try:
                        _pair_addr = _get_pair_for_token(addr)
                        if _pair_addr:
                            _pc = w3.eth.contract(
                                address=Web3.to_checksum_address(_pair_addr),
                                abi=PAIR_ABI_PRICE
                            )
                            _res = _pc.functions.getReserves().call()
                            _t0  = _pc.functions.token0().call().lower()
                            _wbnb_res = _res[0] if _t0 == WBNB.lower() else _res[1]
                            _wbnb_bnb = _wbnb_res / 1e18
                            _prev_wbnb = _pos_data.get("_wbnb_reserve", 0)
                            if _prev_wbnb <= 0:
                                _pos_data["_wbnb_reserve"] = _wbnb_bnb
                            else:
                                _drop_pct = ((_wbnb_bnb - _prev_wbnb) / _prev_wbnb) * 100
                                if _drop_pct <= -50:
                                    print(f"🚨 RESERVES DROP: {addr[:10]} WBNB {_prev_wbnb:.3f}→{_wbnb_bnb:.3f} ({_drop_pct:.0f}%) → SELL!")
                                    _auto_paper_sell(addr, f"LiqDrop {abs(_drop_pct):.0f}% 🚨 Rug", 100.0)
                                    continue
                                elif _wbnb_bnb > _prev_wbnb:
                                    _pos_data["_wbnb_reserve"] = _wbnb_bnb
                    except Exception:
                        pass

                if _has_vol:
                    if _bv5 > 0 or _sv5 > 0:
                        _ratio = _sv5 / max(_bv5, 0.0001)
                    else:
                        _ratio = _s5 / max(_b5, 1)
                    if _ratio >= 5.0 and _s5 >= 5 and pnl <= -8:
                        _auto_paper_sell(addr, f"VolRug {_ratio:.1f}x 🚨", 100.0)
                        print(f"🚨 VolRug: {addr[:10]} ratio={_ratio:.1f}x sv={_sv5:.3f} bv={_bv5:.3f}")
                        _trail_triggered = True
                    elif _ratio >= 3.0 and _s5 >= 5 and pnl <= -10:
                        _auto_paper_sell(addr, f"VolDump {_ratio:.1f}x", 100.0)
                        print(f"⚠️ VolDump: {addr[:10]} ratio={_ratio:.1f}x pnl={pnl:.1f}%")
                        _trail_triggered = True
                    # FIX 1: _auto_paper_sell real mode bhi handle karta hai internally

                if not _trail_triggered:
                    # ── FIX v36 Bug2: pnl_high track karo (ATH bug fix ke baad sahi kaam karega) ──
                    _pnl_high = _pos_data.get("pnl_high", 0.0)
                    if pnl > _pnl_high:
                        _pos_data["pnl_high"] = pnl
                        _pnl_high = pnl
                        # FIX v62: New high bana → timestamp reset karo
                        _pos_data["_last_high_ts"] = time.time()

                    _entry_sl = _pos_data.get("sl_pct", 15.0)

                    # ── MomDead pre-calc: HardSL se pehle calculate karo taaki priority sahi rahe ──
                    _vol_live   = _get_vol_pressure_rt(addr)
                    _bv5_live   = _vol_live.get("buy_vol5", 0.0)
                    _b5_live    = _vol_live.get("buys5",    0)
                    _s5_live    = _vol_live.get("sells5",   0)

                    _bought_str  = _pos_data.get("bought_at", "")
                    try:
                        _hold_secs = (datetime.utcnow() - datetime.fromisoformat(_bought_str[:19])).total_seconds() if _bought_str else 999
                    except Exception:
                        _hold_secs = 999

                    # ── FM BC tokens: PancakeSwap pair nahi hota → bv5 hamesha 0 ──
                    # Isliye FM BC ke liye price momentum check karo instead of bv5
                    _src_check = _pos_data.get("source", "") or _pos_data.get("buy_reasoning", {}).get("source", "")
                    _is_fm_bc  = "FM_BC" in _src_check

                    _vwc = auto_trade_stats["vol_weak_count"]

                    if _is_fm_bc:
                        _fm_price_hist = _pos_data.get("_fm_price_hist", [])
                        _fm_price_hist.append(current)
                        if len(_fm_price_hist) > 6: _fm_price_hist.pop(0)
                        _pos_data["_fm_price_hist"] = _fm_price_hist

                        # Funds: FM price monitor thread se parallel update hota hai
                        _fm_funds_hist = _pos_data.get("_fm_funds_hist", [])

                        # ── LEADING: funds sudden drop ──
                        _instant_dump = False
                        if len(_fm_funds_hist) >= 2:
                            _f_prev = _fm_funds_hist[-2]
                            _f_curr = _fm_funds_hist[-1]
                            _f_drop = (_f_prev - _f_curr) / _f_prev * 100 if _f_prev > 0 else 0
                            if _f_drop >= 20:
                                _instant_dump = True
                                print(f"🚨 [FM] Funds -{_f_drop:.1f}% instant dump: {addr[:10]}")
                            elif _f_drop >= 5:
                                _vwc[addr] = _vwc.get(addr, 0) + 1

                        # ── LAGGING: price + funds both declining ──
                        if not _instant_dump:
                            if len(_fm_price_hist) >= 4:
                                _fm_peak = max(_fm_price_hist)
                                _fm_drawdown_pct = (_fm_peak - current) / _fm_peak * 100 if _fm_peak > 0 else 0
                                _fm_declining = sum(
                                    1 for i in range(1, len(_fm_price_hist))
                                    if _fm_price_hist[i] < _fm_price_hist[i-1] * 0.985
                                )
                                _funds_also_dying = False
                                if len(_fm_funds_hist) >= 3:
                                    _funds_declining = sum(
                                        1 for i in range(1, len(_fm_funds_hist))
                                        if _fm_funds_hist[i] < _fm_funds_hist[i-1]
                                    )
                                    _funds_also_dying = _funds_declining >= 2
                                _real_dump = (_fm_declining >= 3 or (_fm_declining >= 2 and _fm_drawdown_pct > 15)) and _funds_also_dying
                                if _real_dump:
                                    _vwc[addr] = _vwc.get(addr, 0) + 1
                                else:
                                    _vwc[addr] = 0
                            else:
                                _vwc[addr] = 0
                    else:
                        # PancakeSwap tokens: original bv5 logic
                        if _bv5_live < 0.5:
                            _vwc[addr] = _vwc.get(addr, 0) + 1
                        else:
                            _vwc[addr] = 0

                    # FM BC: 8 readings — consolidation survive kare, genuine downtrend exit
                    # PC: 3 readings + 20s hold
                    _vol_dying = _vwc.get(addr, 0) >= (6 if _is_fm_bc else 3)
                    if _is_fm_bc:
                        _fm_peak2 = max(_pos_data.get("_fm_price_hist", [current]), default=current)
                        _drawdown_from_high = (_fm_peak2 - current) / _fm_peak2 * 100 if _fm_peak2 > 0 else 0
                        if (_pnl_high > 35 or tp_sold >= 50) and _drawdown_from_high < 25:
                            _mom_dead = _instant_dump  # TP1 ke baad runner — sirf instant dump pe exit
                        else:
                            _mom_dead = _instant_dump or _vol_dying
                    else:
                        _mom_dead = _vol_dying and _hold_secs > 20

                    # ── ENTRY GUARD v44: TP1 se pehle only, fake signal protection ──
                    # Case 1: Seedha neeche — 3 consecutive no-buyer readings → min loss exit
                    # Case 2: Thoda upar fir girna — tight trail, sell volume confirm
                    # TP1 ke baad bilkul touch nahi — wo alag logic hai
                    if tp_sold == 0:
                        _egc = auto_trade_stats["entry_guard_count"]

                        # Case 1: No pump at all, buyers absent
                        if _pnl_high < 3.0 and pnl <= -2.0:
                            # FastDump: FM BC pe price declining, PC pe sell dominant
                            _fm_price_dead = _is_fm_bc and _vwc.get(addr, 0) >= 4
                            _sv5_fd = _get_vol_pressure_rt(addr).get("sell_vol5", 0.0) if not _is_fm_bc else 0.0
                            _sell_dominant = _sv5_fd > _bv5_live * 2 and _sv5_fd > 0.001
                            _fast_dump = (_sell_dominant or _fm_price_dead) and pnl <= -5
                            if _fast_dump:
                                _reason = "FMPriceDead" if _fm_price_dead else f"SellDom sv={_sv5_fd:.3f}"
                                _auto_paper_sell(addr, f"FastDump -{abs(pnl):.1f}% 🔵", 100.0)
                                _egc.pop(addr, None)
                                _trail_triggered = True
                                print(f"🔵 FastDump [{_reason}]: {addr[:10]} pnl={pnl:.1f}% hold={_hold_secs:.0f}s")
                                continue

                            # FM BC: bv5=0 hamesha — price history use karo
                            if _is_fm_bc:
                                _eg_hist = _pos_data.get("_eg_price_hist", [])
                                _eg_hist.append(current)
                                if len(_eg_hist) > 5: _eg_hist.pop(0)
                                _pos_data["_eg_price_hist"] = _eg_hist
                                if len(_eg_hist) >= 3:
                                    _eg_down = sum(1 for i in range(1, len(_eg_hist)) if _eg_hist[i] < _eg_hist[i-1] * 0.99)
                                    if _eg_down >= 2: _egc[addr] = _egc.get(addr, 0) + 1
                                    else: _egc[addr] = 0
                                else: _egc[addr] = 0
                            else:
                                if _bv5_live < 0.3: _egc[addr] = _egc.get(addr, 0) + 1
                                else: _egc[addr] = 0

                            if _egc.get(addr, 0) >= 4:
                                _auto_paper_sell(addr, f"EntryGuard NoMom -{abs(pnl):.1f}% 🔵", 100.0)
                                _egc.pop(addr, None)
                                _trail_triggered = True
                                print(f"🔵 EntryGuard Case1: {addr[:10]} pnl={pnl:.1f}% fm_bc={_is_fm_bc}")
                                continue

                        # Case 2: Pumped then fading
                        elif _pnl_high >= 3.0 and pnl < (_pnl_high - 8):
                            if _is_fm_bc:
                                _eg_hist2 = _pos_data.get("_eg_price_hist", [])
                                _eg_hist2.append(current)
                                if len(_eg_hist2) > 5: _eg_hist2.pop(0)
                                _pos_data["_eg_price_hist"] = _eg_hist2
                                if len(_eg_hist2) >= 3:
                                    _eg_down2 = sum(1 for i in range(1, len(_eg_hist2)) if _eg_hist2[i] < _eg_hist2[i-1] * 0.99)
                                    if _eg_down2 >= 2: _egc[addr] = _egc.get(addr, 0) + 1
                                    else: _egc[addr] = 0
                                else: _egc[addr] = 0
                            else:
                                _sv5_live = _get_vol_pressure_rt(addr).get("sell_vol5", 0.0)
                                if _bv5_live < 0.3 and _sv5_live > _bv5_live: _egc[addr] = _egc.get(addr, 0) + 1
                                else: _egc[addr] = 0

                            if _egc.get(addr, 0) >= 4:
                                _auto_paper_sell(addr, f"EntryGuard Faded -{abs(pnl):.1f}% 🔵", 100.0)
                                _egc.pop(addr, None)
                                _trail_triggered = True
                                print(f"🔵 EntryGuard Case2: {addr[:10]} pnl={pnl:.1f}% high={_pnl_high:.1f}% fm_bc={_is_fm_bc}")
                                continue

                    # ── Hard SL: absolute exit at sl_pct%, no conditions ──
                    # MomDead handles early exits (any %)
                    # HardSL = hard floor, fires regardless of momentum
                    if pnl <= -_entry_sl:
                        _auto_paper_sell(addr, f"HardSL -{_entry_sl:.0f}% 🔴", 100.0)
                        blacklist_token(addr, f"HardSL rebuy block")
                        _trail_triggered = True
                        print(f"🔴 HardSL: {addr[:10]} pnl={pnl:.1f}%")
                        continue

                    # Emergency SL: 20s mein -8% + no pump = fast exit
                    _emergency_sl = (
                        _hold_secs > 20
                        and pnl <= -8
                        and not _mom_dead
                        and _pnl_high < 5.0
                    )

                    # ── FIX v37 A: TP1/TP2 independent — MomDead same iteration check ──
                    # Pehle: elif chain — TP1 fire kiya toh MomDead skip
                    # Ab: TP1/TP2 alag if, MomDead/EmergSL alag if — dono same iteration

                    # ── TP1: +40% → 50% sell ──
                    if pnl >= 40 and tp_sold < 50:
                        _auto_paper_sell(addr, f"TP1 +40% 🔒", 50.0)
                        print(f"🔒 TP1: {addr[:10]} pnl={pnl:.1f}%")
                        # tp_sold ab 50 ho gaya — turant update karo is iteration ke liye
                        tp_sold = auto_trade_stats["running_positions"].get(addr, {}).get("tp_sold", 50.0)

                    # ── TP2: +150% → 30% sell (total 80% sold) ──
                    elif pnl >= 150 and tp_sold < 80:
                        _auto_paper_sell(addr, f"TP2 +150% 🔥", 30.0)
                        print(f"🔥 TP2: {addr[:10]} pnl={pnl:.1f}%")
                        tp_sold = auto_trade_stats["running_positions"].get(addr, {}).get("tp_sold", 80.0)

                    # ── FIX v62: Momentum Stall — TP1 ke baad new high nahi bana X seconds mein ──
                    # Scalper logic: momentum khatam = exit, fixed % nahi
                    _last_high_ts = _pos_data.get("_last_high_ts", 0)
                    _stall_secs = time.time() - _last_high_ts if _last_high_ts > 0 else 0

                    # Stall threshold: TP ke baad zyada strict
                    # Pre-TP: 45s stall → exit (early coin, momentum fast hoti hai)
                    # Post-TP1: 30s stall → exit (50% book ho chuka, protect karo)
                    # Post-TP2 (moonbag): 20s stall → exit (max profit capture)
                    if tp_sold >= 80:
                        _stall_threshold = 45
                    elif tp_sold >= 50:
                        _stall_threshold = 90  # Post-TP1: zyada room do
                    else:
                        _stall_threshold = 45

                    # Sirf tab trigger karo jab meaningful profit ho (noise se bachao)
                    _mom_stall = (
                        _stall_secs >= _stall_threshold
                        and _pnl_high >= 20.0   # min 20% high hona chahiye
                        and _last_high_ts > 0
                    )

                    # ── MomDead + EmergSL: INDEPENDENT if — TP1/TP2 ke baad bhi check ──
                    if addr in auto_trade_stats["running_positions"] and not _trail_triggered:
                        if _mom_dead or _mom_stall:
                            _reason = "MomStall" if _mom_stall and not _mom_dead else "MomDead"
                            _zone = "Moonbag" if tp_sold >= 80 else ("Post-TP1" if tp_sold >= 50 else "Pre-TP")
                            _auto_paper_sell(addr, f"{_reason} {_zone} 📉", 100.0)
                            _trail_triggered = True
                            _vwc_cnt = auto_trade_stats["vol_weak_count"].get(addr, 0)
                            print(f"📉 {_reason} [{_zone}]: {addr[:10]} pnl={pnl:.1f}% high={_pnl_high:.1f}% stall={_stall_secs:.0f}s hold={_hold_secs:.0f}s")
                            auto_trade_stats["vol_weak_count"].pop(addr, None)  # FIX v43: cleanup
                            auto_trade_stats["entry_guard_count"].pop(addr, None)  # memory fix

                        elif _emergency_sl:
                            _auto_paper_sell(addr, f"EmergSL -20% 🚨", 100.0)
                            blacklist_token(addr, "EmergSL rebuy block")
                            _trail_triggered = True
                            print(f"🚨 EmergSL: {addr[:10]} pnl={pnl:.1f}% hold={_hold_secs:.0f}s bv5={_bv5_live:.3f}")

            except Exception as e:
                print(f"Auto manager err {addr[:10]}: {e}")

        _positions = auto_trade_stats["running_positions"]
        if not _positions:
            _sleep = 30
        elif any(
            ((monitored_positions.get(a, {}).get("current", 0) - v.get("entry", 1e-18)) / max(v.get("entry", 1e-18), 1e-18)) * 100 >= 10
            for a, v in list(_positions.items())
        ):
            _sleep = 0.3
        else:
            _sleep = 0.05  # FIX v37 C: 0.2s → 0.05s — 4x faster, ~20 checks/sec
        time.sleep(_sleep)


def price_monitor_loop():
    print("📡 Price Monitor started")
    while True:
        with monitor_lock:
            _snap = list(monitored_positions.items())
        for addr, pos in _snap:
            try:
                if pos.get("buy_reasoning", {}).get("source") == "FM_BC_v2":
                    try:
                        _fm_info = _fm_get_token_info(addr, _fm_get_w3())
                        if _fm_info and _fm_info.get("lastPrice", 0) > 0:
                            _bnb_p = market_cache.get("bnb_price", 640)
                            _quote = _fm_info.get("quote", "").lower()
                            _USDT_L = "0x55d398326f99059ff775485246999027b3197955"
                            _BUSD_L = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
                            if _quote in [_USDT_L, _BUSD_L]:
                                current = (_fm_info["lastPrice"] / 1e18) / _bnb_p if _bnb_p > 0 else 0
                            else:
                                current = _fm_info["lastPrice"] / 1e18
                            # funds track karo — actual buyer pressure
                            _fn = float(_fm_info.get("funds", 0))
                            if _fn > 0:
                                _fh = pos.get("_fm_funds_hist", [])
                                _fh.append(_fn)
                                if len(_fh) > 6: _fh.pop(0)
                                pos["_fm_funds_hist"] = _fh
                        else:
                            current = 0
                    except:
                        current = 0
                else:
                    current = get_token_price_bnb(addr)
                if current <= 0:
                    continue
                if pos["entry"] > 0 and current > pos["entry"] * 10000:
                    continue
                pos["current"] = current
                if current > pos["high"]:
                    pos["high"] = current
                entry          = pos["entry"]
                pnl_pct        = ((current - entry) / entry) * 100 if entry > 0 else 0
                drop_from_high = ((current - pos["high"]) / pos["high"]) * 100 if pos["high"] > 0 else 0
                sl             = pos["stop_loss_pct"]
                alerts_sent    = pos["alerts_sent"]

                if pnl_pct <= -sl and "stop_loss" not in alerts_sent:
                    alerts_sent.append("stop_loss")
                if pnl_pct >= 200 and "tp_200" not in alerts_sent:
                    alerts_sent.append("tp_200")
                elif pnl_pct >= 100 and "tp_100" not in alerts_sent:
                    alerts_sent.append("tp_100")
                elif pnl_pct >= 50 and "tp_50" not in alerts_sent:
                    alerts_sent.append("tp_50")
                elif pnl_pct >= 30 and "tp_30" not in alerts_sent:
                    alerts_sent.append("tp_30")
                if drop_from_high <= -90 and "dump_90" not in alerts_sent:
                    alerts_sent.append("dump_90")
                elif drop_from_high <= -70 and "dump_70" not in alerts_sent:
                    alerts_sent.append("dump_70")
                elif drop_from_high <= -50 and "dump_50" not in alerts_sent:
                    alerts_sent.append("dump_50")
            except Exception as e:
                print(f"⚠️ Price monitor error ({addr}): {e}")
        _sleep = 0.2 if monitored_positions else 15  # FIX v37 C: 1s → 0.2s price monitor
        time.sleep(_sleep)


def _memory_cleanup_loop():
    """Periodic cleanup — _pair_to_token + _rt_swap_data orphan entries remove karo"""
    time.sleep(60)
    while True:
        try:
            now = time.time()
            with _rt_swap_lock:
                # Active position addresses
                active = set(auto_trade_stats.get("running_positions", {}).keys())
                active.update(monitored_positions.keys())

                # _pair_to_token: sirf active positions ke entries rakhna
                stale_pairs = [k for k, v in _pair_to_token.items()
                               if v.get("token", "") not in active]
                for k in stale_pairs:
                    _pair_to_token.pop(k, None)

                # _rt_swap_data: active + last 10 min mein updated entries rakhna
                stale_rt = [k for k, v in _rt_swap_data.items()
                            if k not in active and (now - v.get("ts", 0)) > 600]
                for k in stale_rt:
                    _rt_swap_data.pop(k, None)

            if stale_pairs or stale_rt:
                print(f"🧹 MemClean: removed {len(stale_pairs)} pairs, {len(stale_rt)} rt_swap entries")
            gc.collect()
        except Exception as e:
            print(f"⚠️ MemCleanup error: {e}")
        time.sleep(300)  # har 5 minute

# ========== PC FAST SNIPER ==========
# QuickNode WSS → PairCreated detect
# Flow: Blacklist → Liq check → Parallel(sim + tax) → BUY
# Speed: ~700ms total
# ═══════════════════════════════════════════

# Already sniped set — deduplicate
_pc_sniped:      set            = set()
_pc_sniped_lock: threading.Lock = threading.Lock()

# Semaphore — max 5 parallel snipes
_pc_sem = threading.Semaphore(5)

# Global QuickNode w3 — ek baar banao, baar baar nahi
_w3q_global = None
_w3q_lock   = threading.Lock()

# FIX v29: Stage 1 RPC pool — har snipe pe naya Web3 nahi
_stage1_w3_pool = {}
_stage1_pool_lock = threading.Lock()

def _get_stage1_w3():
    """Stage 1 ke liye RPC pool — connection reuse karo"""
    _rpcs = [
        "https://bsc-rpc.publicnode.com",
        "https://bsc.drpc.org",
        "https://1rpc.io/bnb",
    ]
    with _stage1_pool_lock:
        for rpc in _rpcs:
            if rpc in _stage1_w3_pool:
                try:
                    # Quick check — block_number ping nahi, sirf object check
                    if _stage1_w3_pool[rpc] is not None:
                        return _stage1_w3_pool[rpc]
                except: pass
        # Pool mein nahi — naya banao aur save karo
        for rpc in _rpcs:
            try:
                _w = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
                _stage1_w3_pool[rpc] = _w
                return _w
            except: continue
    return None

def _get_w3q():
    global _w3q_global
    # FIX v29: is_connected() = RPC ping — har call pe waste, hatao
    if _w3q_global is not None:
        return _w3q_global
    with _w3q_lock:
        qn = os.getenv("QUICKNODE_HTTP", "")
        if not qn: return None
        _w3q_global = Web3(Web3.HTTPProvider(qn, request_kwargs={"timeout": 10}))
        return _w3q_global

def _onchain_sim(token_address: str, w3_instance=None) -> dict:
    """
    Honeypot check via getAmountsOut — fast ~100ms, no encode_abi needed
    Token sell path check: token → WBNB
    Fail/revert = honeypot, success = safe
    """
    result = {"safe": False, "reason": ""}
    try:
        _w3      = w3_instance or w3
        token_cs = Web3.to_checksum_address(token_address)
        wbnb_cs  = Web3.to_checksum_address(WBNB)
        _ROUTER_ABI = [{
            "name": "getAmountsOut",
            "type": "function",
            "stateMutability": "view",
            "inputs": [
                {"name": "amountIn", "type": "uint256"},
                {"name": "path",     "type": "address[]"}
            ],
            "outputs": [{"name": "amounts", "type": "uint256[]"}]
        }]
        router = _w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_ROUTER),
            abi=_ROUTER_ABI
        )
        # Small amount sell simulate: 1000 tokens → WBNB
        # Honeypot = revert, Safe = returns value
        amt_in = 10 ** 9  # 1000 tokens (9 decimals)
        out = router.functions.getAmountsOut(
            amt_in, [token_cs, wbnb_cs]
        ).call(block_identifier="latest")

        if out and len(out) >= 2 and out[-1] > 0:
            result["safe"]   = True
            result["reason"] = "sell path ok"
        else:
            result["reason"] = "sell path returns 0 — likely honeypot"
        return result
    except Exception as e:
        err = str(e)
        if "INSUFFICIENT_LIQUIDITY" in err:
            result["reason"] = "no liquidity"
        elif "execution reverted" in err or "revert" in err.lower():
            result["reason"] = "sell revert: honeypot"
        else:
            result["reason"] = f"sim error: {err[:80]}"
        return result


def _pc_get_tax(token_address: str, w3_inst) -> float:
    """
    getAmountsOut tax check — on-chain, no API
    0.001 BNB buy simulate → sell back → tax = difference %
    Returns: tax % (0-100), -1 = error
    """
    try:
        _ROUTER_ABI = [{
            "name": "getAmountsOut",
            "type": "function",
            "stateMutability": "view",
            "inputs": [
                {"name": "amountIn",  "type": "uint256"},
                {"name": "path",      "type": "address[]"}
            ],
            "outputs": [{"name": "amounts", "type": "uint256[]"}]
        }]
        router = w3_inst.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_ROUTER),
            abi=_ROUTER_ABI
        )
        wbnb_cs  = Web3.to_checksum_address(WBNB)
        token_cs = Web3.to_checksum_address(token_address)
        amt_in   = int(0.001 * 1e18)  # 0.001 BNB

        # Buy: BNB → Token
        buy_out = router.functions.getAmountsOut(
            amt_in, [wbnb_cs, token_cs]
        ).call(block_identifier="latest")
        tokens_out = buy_out[-1]
        if tokens_out <= 0:
            return -1

        # Sell: Token → BNB
        sell_out = router.functions.getAmountsOut(
            tokens_out, [token_cs, wbnb_cs]
        ).call(block_identifier="latest")
        bnb_back = sell_out[-1]
        if bnb_back <= 0:
            return 100.0

        tax = (1 - bnb_back / amt_in) * 100
        return round(max(0, tax), 1)
    except Exception:
        return -1


_FM_WBNB         = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
_FM_WSS = []
_FM_RPC = []

_FM_FACTORY_ADDRS = [
    "0x5c952063c7fc8610ffdb798152d69f0b9550762b",
    "0x8b8cf6d0c2b5f4cb61da5e7dc94e52f4f1dd8d64",
    "0x48a31b72f77a2a90ebe24e5c4c88be43e2ad6beb",
]
_FM_FACTORY_ADDR = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"

# ── Gas price cache — buy ke time fast ──
_fm_gas_cache = {"price": 0, "ts": 0}
_fm_gas_lock  = threading.Lock()

def _fm_get_cached_gas(w3):
    """Har 10s mein gas price update — buy ke time 0ms"""
    import time as _t
    _FM_MIN_GAS = 1_000_000_000  # 1 gwei minimum — FourMeme 'GW' error avoid
    with _fm_gas_lock:
        if _t.time() - _fm_gas_cache["ts"] < 10 and _fm_gas_cache["price"] > 0:
            return max(_fm_gas_cache["price"], _FM_MIN_GAS)
    try:
        gp = w3.eth.gas_price
        gp = max(gp, _FM_MIN_GAS)
        with _fm_gas_lock:
            _fm_gas_cache["price"] = gp
            _fm_gas_cache["ts"]    = _t.time()
        return gp
    except:
        return 3_000_000_000  # FIX v22: dead duplicate body removed

# ── ABIs ──
# Official helper contract — TokenManagerHelper3
_FM_HELPER_ADDR = "0xF251F83e40a78868FcfA3FA4599Dad6494E46034"

_FM_BC_ABI = [
    {"name":"buyTokenAMAP","type":"function","stateMutability":"payable",
     "inputs":[{"name":"token","type":"address"},
               {"name":"funds","type":"uint256"},{"name":"minAmount","type":"uint256"}],
     "outputs":[{"name":"","type":"uint256"}]},
    # V2: 7 params
    {"name":"sellToken","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"origin","type":"uint256"},
               {"name":"token","type":"address"},
               {"name":"from","type":"address"},
               {"name":"amount","type":"uint256"},
               {"name":"minFunds","type":"uint256"},
               {"name":"feeRate","type":"uint256"},
               {"name":"feeRecipient","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}]},
]

# V1 ABI — 6 params (no from)
_FM_BC_ABI_V1 = [
    {"name":"buyTokenAMAP","type":"function","stateMutability":"payable",
     "inputs":[{"name":"token","type":"address"},
               {"name":"funds","type":"uint256"},{"name":"minAmount","type":"uint256"}],
     "outputs":[{"name":"","type":"uint256"}]},
    {"name":"sellToken","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"origin","type":"uint256"},
               {"name":"token","type":"address"},
               {"name":"amount","type":"uint256"},
               {"name":"minFunds","type":"uint256"},
               {"name":"feeRate","type":"uint256"},
               {"name":"feeRecipient","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}]},
]

# Official getTokenInfo ABI — 12 return values
_FM_HELPER_ABI = [
    {"name":"getTokenInfo","type":"function","stateMutability":"view",
     "inputs":[{"name":"token","type":"address"}],
     "outputs":[
         {"name":"version","type":"uint256"},
         {"name":"tokenManager","type":"address"},
         {"name":"quote","type":"address"},
         {"name":"lastPrice","type":"uint256"},
         {"name":"tradingFeeRate","type":"uint256"},
         {"name":"minTradingFee","type":"uint256"},
         {"name":"launchTime","type":"uint256"},
         {"name":"offers","type":"uint256"},
         {"name":"maxOffers","type":"uint256"},
         {"name":"funds","type":"uint256"},
         {"name":"maxFunds","type":"uint256"},
         {"name":"liquidityAdded","type":"bool"}
     ]},
]
_FM_ERC20_ABI = [
    {"name":"approve","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "outputs":[{"name":"","type":"bool"}]},
    {"name":"allowance","type":"function","stateMutability":"view",
     "inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}]},
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"account","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}]},
    {"name":"totalSupply","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"name","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"string"}]},
    {"name":"symbol","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"string"}]},
]

def _fm_get_unique_buyers(token_addr, w3=None):
    """Token pe unique buyers + recent buys (last 20 blocks ~60s) count karo"""
    try:
        if not w3: w3 = _fm_get_w3()
        if not w3: return 0, 0, {}
        TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        current = w3.eth.block_number
        logs = w3.eth.get_logs({
            "address": Web3.to_checksum_address(token_addr),
            "topics": [TRANSFER_TOPIC],
            "fromBlock": current - 20,  # ~60s, QuickNode safe
            "toBlock": "latest"
})
        _ZERO = "0x0000000000000000000000000000000000000000"
        _DEAD = "0x000000000000000000000000000000000000dead"
        _FM_FACTORY_L = _FM_FACTORY_ADDR.lower()
        buyers = set()
        recent_buys = 0
        block_wallets = {}
        for log in logs:
            if len(log["topics"]) < 3: continue
            from_addr = "0x" + log["topics"][1].hex()[-40:].lower()
            to_addr   = "0x" + log["topics"][2].hex()[-40:].lower()
            # Sirf factory se receive karne wale = actual buyers
            if from_addr == _FM_FACTORY_L and to_addr not in [_ZERO, _DEAD]:
                buyers.add(to_addr)
                if log["blockNumber"] >= current - 20:
                    recent_buys += 1
                blk = log["blockNumber"]
                if blk not in block_wallets:
                    block_wallets[blk] = set()
                block_wallets[blk].add(to_addr)
        return len(buyers), recent_buys, block_wallets
    except Exception as e:
        print(f"⚠️ [FM] buyers fetch error: {str(e)[:50]}")
        return 0, 0, {}

_fm_sniped      = set()
_fm_sniped_lock = threading.Lock()
_fm_sniped_ts:  dict = {}  # FIX v50: timestamp per entry — stale cleanup ke liye

# FIX v15: Sell dedup — ek token pe ek hi sell ek waqt mein
_fm_selling_set  = set()
# FIX v46: In-memory approve cache — sell pe allowance RPC skip karne ke liye
# Key: "token_addr_lower:spender_addr_lower" → True agar approve confirmed hai
_fm_approved_cache: dict = {}
_fm_selling_lock = threading.Lock()

# Dev history cache — on-chain results cache karo
_fm_dev_cache      = {}
_fm_dev_cache_lock = threading.Lock()

# Global FM w3 — PC wala hi reuse karo
def _fm_get_w3():
    """Free RPC — polling aur filters ke liye"""
    for rpc in ["https://bsc-rpc.publicnode.com", "https://bsc.drpc.org", "https://1rpc.io/bnb"]:
        try:
            return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
        except: continue
    return None

def _fm_get_token_info(token_addr, w3=None):
    """Official getTokenInfo via TokenManagerHelper3"""
    if not w3: w3 = _fm_get_w3()
    if not w3: return None
    try:
        helper = w3.eth.contract(
            address=Web3.to_checksum_address(_FM_HELPER_ADDR),
            abi=_FM_HELPER_ABI
        )
        info = helper.functions.getTokenInfo(Web3.to_checksum_address(token_addr)).call()
        # info: version, tokenManager, quote, lastPrice, tradingFeeRate,
        #       minTradingFee, launchTime, offers, maxOffers, funds, maxFunds, liquidityAdded
        token_manager = info[1]
        return {
            "version":        info[0],
            "tokenManager":   token_manager,
            "quote":          info[2],
            "lastPrice":      info[3],
            "tradingFeeRate": info[4],
            "launchTime":     info[6],
            "offers":         info[7],
            "maxOffers":      info[8],
            "funds":          info[9],
            "maxFunds":       info[10],
            "liquidityAdded": info[11],
            "factory":        token_manager
}
    except Exception as e:
        print(f"⚠️ [FM] getTokenInfo error: {str(e)[:60]}")
    return None

def _fm_calc_progress(info):
    """Progress % = raised/max * 100"""
    if not info or info["maxFunds"] <= 0: return -1
    return round((info["funds"] / info["maxFunds"]) * 100, 2)

def _fm_dev_history_onchain(dev_addr, w3=None):
    """
    Dev ka on-chain history check karo — fully on-chain, no API
    Factory pe Transfer(mint) events filter by dev address
    Returns: {"total": N, "rugged": N, "graduated": N}
    """
    # Cache check
    dev_lower = dev_addr.lower()
    with _fm_dev_cache_lock:
        if dev_lower in _fm_dev_cache:
            return _fm_dev_cache[dev_lower]

    result = {"total": 0, "rugged": 0, "graduated": 0}
    try:
        if not w3: w3 = _fm_get_w3()
        if not w3: return result

        # Transfer(mint) topic — from=0x000 means new token created
        TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        ZERO_PADDED    = "0x" + "0" * 64

        current_block = w3.eth.block_number
        from_block    = max(0, current_block - 7200)  # sirf last 6 hours (~7200 blocks)

        # Sirf primary factory check karo — speed ke liye
        try:
            logs = w3.eth.get_logs({
                "address":   Web3.to_checksum_address(_FM_FACTORY_ADDRS[0]),
                "topics":    [TRANSFER_TOPIC, ZERO_PADDED,
                              "0x" + dev_lower[2:].zfill(64)],  # dev address filter
                "fromBlock": from_block,
                "toBlock":   "latest"
})
            dev_tokens = [log["address"] for log in logs]
            result["total"] = len(dev_tokens)

            # Max 5 check karo
            for tok in dev_tokens[:5]:
                try:
                    info = _fm_get_token_info(tok, w3)
                    if not info: continue
                    if info["liquidityAdded"]:
                        result["graduated"] += 1
                    elif info["funds"] == 0:
                        result["rugged"] += 1
                except: continue
        except: pass

    except Exception as e:
        pass

    # Cache result
    with _fm_dev_cache_lock:
        if len(_fm_dev_cache) > 300:
            for _k in list(_fm_dev_cache.keys())[:100]:
                del _fm_dev_cache[_k]
        _fm_dev_cache[dev_lower] = result

    return result

def _fm_confirm_close(token_addr, sell_pct, reason, tx_hash_hex):
    """
    FM sell confirmed onchain — sirf paper state close karo.
    Real sell ho chuka hai — dobara nahi karenge.
    """
    try:
        # FIX v28: case-insensitive lookup — checksummed ya lowercase dono handle
        _rp = auto_trade_stats.get("running_positions", {})
        _matched_key = None
        for _k in _rp:
            if _k.lower() == token_addr.lower():
                _matched_key = _k
                break
        if not _matched_key:
            print(f"⚠️ [FM v28] confirm_close: position not found for {token_addr[:10]} — keys={list(_rp.keys())[:3]}")
            return
        token_addr = _matched_key  # sahi key use karo
        pos  = _rp[token_addr]
        with monitor_lock:
            mon = monitored_positions.get(token_addr, {})
            if not mon:
                # monitored_positions bhi case-insensitive check
                for _mk in monitored_positions:
                    if _mk.lower() == token_addr.lower():
                        mon = monitored_positions[_mk]
                        break

        entry   = pos.get("entry", 0)
        current = mon.get("current", entry)
        size    = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)
        token   = pos.get("token", token_addr[:10])

        if entry <= 0:
            return

        # FIX v26: exit price actual BNB received / tokens sold se calculate karo
        # lastPrice stale hoti hai — TX data se sahi price milta hai
        _actual_bnb_received = pos.get("_actual_bnb_received", 0.0)
        _actual_tokens_sold  = pos.get("_actual_tokens_sold",  0.0)
        if _actual_bnb_received > 0 and _actual_tokens_sold > 0:
            current = _actual_bnb_received / _actual_tokens_sold
            print(f"✅ [FM v26] Exit price from TX: {current:.4e} BNB "
                  f"({_actual_tokens_sold:.0f} tokens → {_actual_bnb_received:.6f} BNB)")
        else:
            current = current * 0.995 if current > 0 else 0

        pnl_pct    = ((current - entry) / entry) * 100 if entry > 0 and current > 0 else -100.0
        sell_size  = size * (sell_pct / 100.0)
        pnl_bnb    = sell_size * (pnl_pct / 100.0)
        return_bnb = _actual_bnb_received if _actual_bnb_received > 0 else sell_size * (1 + pnl_pct / 100.0)

        sess = get_or_create_session(AUTO_SESSION_ID)
        sess["paper_balance"] = round(sess.get("paper_balance", 5.0) + return_bnb, 6)

        if sell_pct >= 100.0:
            auto_trade_stats["auto_pnl_total"] += pnl_pct
            # FIX: sirf final 100% close pe count karo
            auto_trade_stats["total_auto_sells"] += 1

        _banked = pos.get("banked_pnl_bnb", 0.0)
        pos["banked_pnl_bnb"] = round(_banked + pnl_bnb, 6)

        if sell_pct >= 100.0:
            if not isinstance(auto_trade_stats.get("trade_history"), list):
                auto_trade_stats["trade_history"] = []
            _bnb_at_sell  = market_cache.get("bnb_price", 0)
            bought_at_str = pos.get("bought_at", "")
            _orig_sz      = pos.get("orig_size_bnb", size)
            _total_pnl_bnb = round(pos.get("banked_pnl_bnb", 0.0), 6)
            _total_pnl_pct = round((_total_pnl_bnb / _orig_sz * 100), 2) if _orig_sz > 0 else pnl_pct
            auto_trade_stats["trade_history"].append({
                "token":        token,
                "address":      token_addr,
                "entry":        entry,
                "exit":         current,
                "exit_price":   current,
                "pnl_pct":      _total_pnl_pct,
                "pnl_bnb":      _total_pnl_bnb,
                "size_bnb":     _orig_sz,
                "bought_at":    bought_at_str,
                "sold_at":      datetime.utcnow().isoformat(),
                "result":       "win" if _total_pnl_pct > 0 else "loss",
                "exit_reason":  reason,
                "reason":       reason,
                # FIX v30: "real" hardcoded tha — TRADE_MODE use karo
                "mode":         pos.get("mode", TRADE_MODE),
                "tx_hash":      tx_hash_hex,
                "snipe_source": "FM_BC"
})
            auto_trade_stats["running_positions"].pop(token_addr, None)
            remove_position_from_monitor(token_addr)
            # FIX v25: DB mein bhi position remove karo — redeploy pe wapis na aaye
            threading.Thread(target=_persist_positions, daemon=True).start()
            if pnl_pct > 0:
                auto_trade_stats["wins"]   = auto_trade_stats.get("wins", 0) + 1
            else:
                auto_trade_stats["losses"] = auto_trade_stats.get("losses", 0) + 1

        _emoji = "🟢" if pnl_pct >= 0 else "🔴"
        _log("sell", token, f"{_emoji} REAL SELL {sell_pct:.0f}% confirmed · PnL {pnl_pct:+.1f}% · {reason}", token_addr)
        threading.Thread(target=_save_session_to_db, args=(AUTO_SESSION_ID,), daemon=True).start()
        threading.Thread(target=_save_trade_history_to_db, daemon=True).start()
        print(f"✅ [FM] State closed: {token_addr[:10]} PnL:{pnl_pct:+.1f}%")

    except Exception as _ce:
        print(f"⚠️ [FM] confirm close error: {_ce}")


def _fm_track_sell_confirmation(tx_hash_hex, token_addr, token_name, w3, sell_pct=100.0, sell_reason="FM sell"):
    """Background mein TX confirm hone tak track karo — confirm pe paper state close"""
    try:
        for _ in range(30):  # 60s max
            try:
                _receipt = w3.eth.get_transaction_receipt(tx_hash_hex)
                if _receipt:
                    if _receipt["status"] == 1:
                        print(f"✅ [FM] Sell confirmed: {tx_hash_hex[:12]} — closing state")
                        _push_notif("success", "✅ Sell Confirmed",
                            f"{token_name} sell confirmed | TX: {tx_hash_hex[:12]}",
                            token_name, token_addr)
                        _fm_confirm_close(token_addr, sell_pct, sell_reason, tx_hash_hex)
                    else:
                        print(f"❌ [FM] Sell reverted: {tx_hash_hex[:12]} — position still open")
                        _push_notif("critical", "🚨 Sell Reverted",
                            f"{token_name} sell reverted — position still open! Manually sell karo! TX: {tx_hash_hex[:12]}",
                            token_name, token_addr)
                    return
            except Exception:
                pass
            time.sleep(2)
        _push_notif("critical", "⏳ Sell Still Pending",
            f"{token_name} TX pending 60s — manually check! {tx_hash_hex[:12]}",
            token_name, token_addr)
        print(f"⏳ [FM] Still pending 60s: {tx_hash_hex[:12]}")
    except Exception as _te:
        print(f"⚠️ [FM] tracker error: {_te}")


def _fm_real_sell_bc(token_addr: str, sell_pct: float, factory_addr: str, w3=None) -> dict:
    """FM Bonding Curve pe real sell — background tracker + 3 retry + BC minFunds"""
    result = {"success": False, "tx_hash": "", "bnb_received": 0.0, "error": "", "status": ""}

    # FIX v15: Sell dedup — agar ye token already sell ho raha hai toh skip
    _t_lower = token_addr.lower()
    with _fm_selling_lock:
        if _t_lower in _fm_selling_set:
            print(f"⏭️ [FM] Sell already in progress for {token_addr[:10]} — skip")
            result["error"] = "sell already in progress"
            return result
        # FIX v45: add() TX ke baad hoga — pehle add() hoti thi TX se pehle
        # TP fire → add() → MomDead check → "already in progress" → TP skip → SL hit
        # Ab: duplicate check pass hone ke baad lock hold karo, TX ke baad add()

    _selling_lock_held = False
    try:
        with _fm_selling_lock:
            _fm_selling_set.add(_t_lower)
            _selling_lock_held = True
        pk = os.getenv("WALLET_PRIVATE_KEY", "") or os.getenv("PRIVATE_KEY", "") or os.getenv("REAL_PRIVATE_KEY", "")
        wallet_addr = BSC_WALLET or REAL_WALLET
        if not wallet_addr or not pk:
            result["error"] = "no wallet/key"
            with _fm_selling_lock: _fm_selling_set.discard(_t_lower)
            return result
        if not w3: w3 = _get_w3q() or _fm_get_w3()
        if not w3:
            result["error"] = "no RPC"
            with _fm_selling_lock: _fm_selling_set.discard(_t_lower)
            return result

        # Fix 7: checksum wallet
        wallet_cs = Web3.to_checksum_address(wallet_addr)

        # Get token balance
        _tc  = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_FM_ERC20_ABI)
        _bal = _tc.functions.balanceOf(wallet_cs).call()
        _amt = int(_bal * sell_pct / 100)
        if _amt <= 0:
            result["error"] = "zero balance"
            return result

        # FIX: minFunds = 0 — BC pe slippage nahi chahiye
        # buyTokenAMAP bhi 0 use karta hai — BC formula fixed hai
        # Outdated price se minFunds calculate karna = revert
        _min_funds = 0

        print(f"🎓 [FM] Selling: {token_addr[:10]} | amt={_amt} | min_funds={_min_funds}")

        # FIX 5: Gas balance check before BC sell
        try:
            _gas_bal_bc = w3.eth.get_balance(Web3.to_checksum_address(wallet_addr)) / 1e18
            if _gas_bal_bc < 0.0015:
                result["error"] = f"insufficient wallet balance {_gas_bal_bc:.4f} BNB"
                _push_notif("critical", "🔴 Low Balance",
                    f"insufficient wallet balance {_gas_bal_bc:.4f} BNB — wallet top up karo!",
                    token_addr[:10], token_addr)
                return result
        except Exception:
            pass  # gas check fail toh proceed

        _w3_fast = _get_w3q() or w3

        # FIX v18: Version check + dynamic tokenManager via getTokenInfo
        _token_ver = 2
        _dynamic_manager = factory_addr
        try:
            _helper_c = _w3_fast.eth.contract(
                address=Web3.to_checksum_address(_FM_HELPER_ADDR), abi=_FM_HELPER_ABI)
            _tinfo = _helper_c.functions.getTokenInfo(
                Web3.to_checksum_address(token_addr)).call()
            _token_ver       = int(_tinfo[0])
            _dynamic_manager = _tinfo[1]  # actual tokenManager address
            print(f"[FM v18] Token version={_token_ver} | manager={str(_dynamic_manager)[:10]}")
        except Exception as _ve:
            print(f"[FM v18] getTokenInfo failed ({str(_ve)[:40]}), defaulting V2")

        # FIX v18: V1 ya V2 ABI — version ke hisaab se
        _use_abi = _FM_BC_ABI_V1 if _token_ver == 1 else _FM_BC_ABI
        fc = _w3_fast.eth.contract(address=Web3.to_checksum_address(_dynamic_manager), abi=_use_abi)

        # FIX v18: Approve dynamic tokenManager (factory nahi)
        # FIX v46: cache check pehle — allowance RPC call skip karo agar pehle approve ho chuka
        try:
            _tc_approve = _w3_fast.eth.contract(
                address=Web3.to_checksum_address(token_addr), abi=_FM_ERC20_ABI)
            _v46_cache_key = f"{token_addr.lower()}:{_dynamic_manager.lower()}"
            _v46_cache_hit = _fm_approved_cache.get(_v46_cache_key, False)
            if _v46_cache_hit:
                print(f"⚡ [FM] Approve cache HIT — allowance RPC skip: {token_addr[:10]}")
                _allowance = 2**256 - 1  # treat as max approved
            else:
                _allowance = _tc_approve.functions.allowance(
                    wallet_cs, Web3.to_checksum_address(_dynamic_manager)).call()
            if _allowance < _amt:
                print(f"🔑 [FM] Approving Token Manager for sell...")
                _approve_nonce = _w3_fast.eth.get_transaction_count(wallet_cs, "pending")
                _approve_tx = _tc_approve.functions.approve(
                    Web3.to_checksum_address(_dynamic_manager), 2**256 - 1
                ).build_transaction({
                    "from":     wallet_cs,
                    "gas":      100000,
                    "gasPrice": int(_fm_get_cached_gas(_w3_fast) * 1.5),  # FIX v23: was 3.0x
                    "nonce":    _approve_nonce,
                    "chainId":  56
})
                from eth_account import Account as _AccA
                _signed_a = _AccA.sign_transaction(_approve_tx, pk)
                _ah = _w3_fast.eth.send_raw_transaction(_signed_a.raw_transaction)
                # FIX v22: instant poll — koi fixed sleep nahi, receipt milte hi action
                import time as _t22a
                _approve_confirmed = False
                _ap_start = _t22a.time()
                while _t22a.time() - _ap_start < 15:
                    try:
                        _rx2 = _w3_fast.eth.get_transaction_receipt(_ah)
                        if _rx2 is not None:
                            if _rx2["status"] == 1:
                                _approve_confirmed = True
                                print(f"✅ [FM] Approval confirmed ({_t22a.time()-_ap_start:.1f}s)")
                                # FIX v46: runtime approve bhi cache karo — next sell instant hogi
                                _v46_rt_key = f"{token_addr.lower()}:{_dynamic_manager.lower()}"
                                _fm_approved_cache[_v46_rt_key] = True
                                print(f"⚡ [FM] Runtime approve cached: {token_addr[:10]}")
                            else:
                                print(f"❌ [FM] Approval TX failed onchain")
                            break
                    except Exception:
                        pass
                    _t22a.sleep(0.3)
                if not _approve_confirmed:
                    # FIX v22 Bug4: re-approve turant — higher gas
                    print(f"⚠️ [FM] Approve not confirmed — re-approving higher gas...")
                    try:
                        _ra_nonce = _w3_fast.eth.get_transaction_count(wallet_cs, "pending")
                        _ra_tx = _tc_approve.functions.approve(
                            Web3.to_checksum_address(_dynamic_manager), 2**256 - 1
                        ).build_transaction({
                            "from": wallet_cs, "gas": 100000,
                            "gasPrice": int(_fm_get_cached_gas(_w3_fast) * 2.0),  # FIX v23: was 5.0x
                            "nonce": _ra_nonce, "chainId": 56
})
                        from eth_account import Account as _AccRA
                        _rah = _w3_fast.eth.send_raw_transaction(
                            _AccRA.sign_transaction(_ra_tx, pk).raw_transaction)
                        print(f"🔁 [FM] Re-approve TX: {_rah.hex()[:12]}...")
                        _rap_start = _t22a.time()
                        while _t22a.time() - _rap_start < 15:
                            try:
                                _rrx = _w3_fast.eth.get_transaction_receipt(_rah)
                                if _rrx is not None:
                                    if _rrx["status"] == 1:
                                        _approve_confirmed = True
                                        _approve_nonce = _ra_nonce
                                        print(f"✅ [FM] Re-approve confirmed ({_t22a.time()-_rap_start:.1f}s)")
                                    else:
                                        print(f"❌ [FM] Re-approve failed onchain")
                                    break
                            except Exception:
                                pass
                            _t22a.sleep(0.3)
                    except Exception as _rae:
                        print(f"⚠️ [FM] Re-approve error: {str(_rae)[:50]}")
                    if not _approve_confirmed:
                        print(f"❌ [FM] Approve 2x fail — sell aborted")
                        _push_notif("critical", "🚨 MANUAL SELL REQUIRED",
                            f"{token_addr[:10]} — approve 2x fail! MANUALLY SELL KARO!",
                            token_addr[:10], token_addr)
                        result["error"] = "approve failed 2x"
                        with _fm_selling_lock: _fm_selling_set.discard(_t_lower)
                        return result
                # FIX v22 Bug2: sell nonce = approve_nonce+1 race-free
                _sell_nonce_base = _approve_nonce + 1
            else:
                print(f"✅ [FM] Already approved for sell")
                _sell_nonce_base = None
        except Exception as _ae:
            print(f"⚠️ [FM] Approve error: {str(_ae)[:60]}")
            _sell_nonce_base = None

        # FM GRADUATION AUTO DETECT + PC FALLBACK v6
        _FM_PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
        _FM_PANCAKE_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
        _FM_WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

        def _fm_is_graduated(token):
            try:
                # PancakeSwap pair check
                fac = _w3_fast.eth.contract(address=_FM_PANCAKE_FACTORY, abi=[{"inputs":[{"name":"","type":"address"},{"name":"","type":"address"}],"name":"getPair","outputs":[{"name":"","type":"address"}],"type":"function"}])
                pair = fac.functions.getPair(token, _FM_WBNB).call()
                if pair != "0x0000000000000000000000000000000000000000":
                    pr = _w3_fast.eth.contract(address=pair, abi=[{"inputs":[],"name":"getReserves","outputs":[{"name":"","type":"uint112"},{"name":"","type":"uint112"},{"name":"","type":"uint32"}],"type":"function"}])
                    res = pr.functions.getReserves().call()
                    if res[0] > 0 or res[1] > 0:
                        return True
                # liquidityAdded double check via getTokenInfo
                _tinfo_grad = _fm_get_token_info(str(token), _w3_fast)
                if _tinfo_grad and _tinfo_grad.get("liquidityAdded"):
                    return True
                return False
            except:
                return False

        is_grad = _fm_is_graduated(Web3.to_checksum_address(token_addr))
        # Double check with liquidityAdded from helper
        if not is_grad and _tinfo and len(_tinfo) > 11:
            try:
                is_grad = bool(_tinfo[11])  # liquidityAdded field
            except: pass
        print(f"🔍 [FM] Graduated to PC? {'✅ YES (Pancake)' if is_grad else '❌ NO (Curve)'}")

        # SELL LOGIC — Curve ya PC auto
        # Fix 3+4+5: 3 retry turant, pending TX pe retry block, background tracker
        tx_hash = None
        for _attempt in range(1, 4):
            try:
                # FIX v22: attempt 1 = approve_nonce+1, retry = fresh from chain
                if _attempt > 1:
                    _nonce = _w3_fast.eth.get_transaction_count(wallet_cs, "pending")
                elif _sell_nonce_base is not None:
                    _nonce = _sell_nonce_base
                else:
                    _nonce = _w3_fast.eth.get_transaction_count(wallet_cs, "pending")
                if is_grad:
                    # Pancake sell
                    pr = _w3_fast.eth.contract(address=_FM_PANCAKE_ROUTER, abi=[{"name":"swapExactTokensForETH","type":"function","stateMutability":"nonpayable","inputs":[{"name":"amountIn","type":"uint256"},{"name":"amountOutMin","type":"uint256"},{"name":"path","type":"address[]"},{"name":"to","type":"address"},{"name":"deadline","type":"uint256"}],"outputs":[{"name":"amounts","type":"uint256[]"}]}])
                    tx = pr.functions.swapExactTokensForETH(
                        _amt, 0,
                        [Web3.to_checksum_address(token_addr), _FM_WBNB],
                        wallet_cs,
                        int(_w3_fast.eth.get_block('latest')['timestamp'] + 300)
                    ).build_transaction({
                        "from": wallet_cs,
                        "gas": 400000,
                        "gasPrice": int(_fm_get_cached_gas(_w3_fast) * 1.2),  # FIX v23: was 5.5x
                        "nonce": _nonce,
                        "chainId": 56
})
                    print(f"[FM v13] Pancake Sell TX — Gas:650k | chainId=56 | Gwei:{int(_fm_get_cached_gas(_w3_fast)*1.2)/1e9:.1f}")
                else:
                    # FIX v19: _token_ver already set above via getTokenInfo (line ~4538)
                    # Dobara call karne se race condition / version mismatch hoti thi → revert
                    # _token_ver yahan already correct hai — reuse karo
                    print(f"[FM v19] Token version (reused): {_token_ver}")
                    if _token_ver == 1:
                        # V1 — 6 params (no from)
                        _fc_v1 = _w3_fast.eth.contract(
                            address=Web3.to_checksum_address(_dynamic_manager),
                            abi=_FM_BC_ABI_V1
                        )
                        tx = _fc_v1.functions.sellToken(
                            0,                                              # origin
                            Web3.to_checksum_address(token_addr),           # token
                            _amt,                                           # amount
                            _min_funds,                                     # minFunds
                            0,                                              # feeRate
                            "0x0000000000000000000000000000000000000000"    # feeRecipient
                        ).build_transaction({
                            "from":     wallet_cs,
                            "gas":      400000,
                            "gasPrice": int(_fm_get_cached_gas(_w3_fast) * 1.2),  # FIX v23: was 5.5x
                            "nonce":    _nonce,
                            "chainId":  56
})
                        print(f"[FM v18] Curve Sell V1 TX — Gwei:{int(_fm_get_cached_gas(_w3_fast)*1.2)/1e9:.1f}")
                    else:
                        # V2 — 7 params (with from)
                        tx = fc.functions.sellToken(
                            0,                                              # origin
                            Web3.to_checksum_address(token_addr),           # token
                            wallet_cs,                                      # from
                            _amt,                                           # amount
                            _min_funds,                                     # minFunds
                            0,                                              # feeRate
                            "0x0000000000000000000000000000000000000000"    # feeRecipient
                        ).build_transaction({
                            "from":     wallet_cs,
                            "gas":      400000,
                            "gasPrice": int(_fm_get_cached_gas(_w3_fast) * 1.2),  # FIX v23: was 5.5x
                            "nonce":    _nonce,
                            "chainId":  56
})
                        print(f"[FM v18] Curve Sell V2 TX — Gwei:{int(_fm_get_cached_gas(_w3_fast)*1.2)/1e9:.1f}")
                from eth_account import Account
                signed  = Account.sign_transaction(tx, pk)
                tx_hash = _w3_fast.eth.send_raw_transaction(signed.raw_transaction)
                # FIX v45: TX successfully sent — ab lock add karo
                with _fm_selling_lock:
                    _fm_selling_set.add(_t_lower)
                print(f"🔴 [FM] Sell TX attempt {_attempt}: {tx_hash.hex()[:12]}...")
                break  # TX sent — loop se niklo
            except Exception as _se:
                print(f"⚠️ [FM] Sell attempt {_attempt} failed: {str(_se)[:60]}")
                if _attempt == 3:
                    _push_notif("critical", "🚨 MANUAL SELL REQUIRED",
                        f"{token_addr[:10]} — 3 TX send fail! MANUALLY SELL KARO!",
                        token_addr[:10], token_addr)
                    # FIX v16: 3 attempts fail — lock release karo
                    with _fm_selling_lock: _fm_selling_set.discard(token_addr.lower())
                    result["error"] = "3 send attempts failed"
                    return result

        # FIX 3: TX sent — receipt wait karo (60s) taaki bnb_received sahi mile
        result["success"] = True
        result["tx_hash"] = tx_hash.hex()
        result["status"]  = "pending"

        # BC expected BNB estimate (from minFunds)
        _est_bnb = _min_funds / 1e18 if _min_funds > 0 else 0.0
        result["bnb_received"] = _est_bnb

        def _bc_track_and_parse(_th_hex, _t_addr, _t_name, _w3t, _min_f):
            # FIX v22: instant poll — 0.3s interval, receipt milte hi action, revert = turant retry
            import time as _ttr
            def _parse_bnb(_rcpt):
                _bnb_got = _min_f / 1e18 if _min_f > 0 else 0.0
                _wcs = Web3.to_checksum_address(BSC_WALLET or REAL_WALLET)
                for _log in _rcpt["logs"]:
                    _tp = _log.get("topics", [])
                    if len(_tp) >= 3:
                        try:
                            if ("0x" + _tp[2].hex()[-40:]).lower() == _wcs.lower():
                                _d = _log.get("data", "")
                                _d = _d.hex() if isinstance(_d, bytes) else _d
                                _v = int(_d, 16) / 1e18
                                if 0 < _v < 100: return _v
                        except Exception: pass
                return _bnb_got

            def _retry_sell(_t_addr2, _w3r, _attempt_num):
                try:
                    _pk_r = os.getenv("WALLET_PRIVATE_KEY","") or os.getenv("PRIVATE_KEY","") or os.getenv("REAL_PRIVATE_KEY","")
                    _wc_r = Web3.to_checksum_address(BSC_WALLET or REAL_WALLET)
                    _tc_r = _w3r.eth.contract(address=Web3.to_checksum_address(_t_addr2), abi=_FM_ERC20_ABI)
                    _bal_r = _tc_r.functions.balanceOf(_wc_r).call()
                    if _bal_r <= 0:
                        print(f"✅ [FM BC] Balance 0 — already sold"); return None
                    _ti_r = _w3r.eth.contract(
                        address=Web3.to_checksum_address(_FM_HELPER_ADDR), abi=_FM_HELPER_ABI
                    ).functions.getTokenInfo(Web3.to_checksum_address(_t_addr2)).call()
                    _tv_r = int(_ti_r[0]); _mgr_r = _ti_r[1]
                    _fc_r = _w3r.eth.contract(
                        address=Web3.to_checksum_address(_mgr_r),
                        abi=_FM_BC_ABI_V1 if _tv_r == 1 else _FM_BC_ABI)
                    _nn_r = _w3r.eth.get_transaction_count(_wc_r, "pending")
                    _gp_r = int(_fm_get_cached_gas(_w3r) * (1.2 + _attempt_num * 0.3))  # FIX v23: was 5.5+2.0x
                    _zero = "0x0000000000000000000000000000000000000000"
                    if _tv_r == 1:
                        _txr = _fc_r.functions.sellToken(0,Web3.to_checksum_address(_t_addr2),_bal_r,0,0,_zero
                            ).build_transaction({"from":_wc_r,"gas":400000,"gasPrice":_gp_r,"nonce":_nn_r,"chainId":56})
                    else:
                        _txr = _fc_r.functions.sellToken(0,Web3.to_checksum_address(_t_addr2),_wc_r,_bal_r,0,0,_zero
                            ).build_transaction({"from":_wc_r,"gas":400000,"gasPrice":_gp_r,"nonce":_nn_r,"chainId":56})
                    from eth_account import Account as _AccR
                    _hr = _w3r.eth.send_raw_transaction(_AccR.sign_transaction(_txr, _pk_r).raw_transaction)
                    print(f"🔁 [FM BC] Retry sell #{_attempt_num}: {_hr.hex()[:12]}... gwei={_gp_r/1e9:.1f}")
                    return _hr
                except Exception as _re:
                    print(f"⚠️ [FM BC] Retry #{_attempt_num} send error: {str(_re)[:60]}"); return None

            try:
                _cur_hash  = _th_hex
                _max_retry = 3
                _retries   = 0
                while _retries <= _max_retry:
                    _t_start = _ttr.time()
                    _got_rx  = False
                    while _ttr.time() - _t_start < 20:  # 20s per attempt
                        try:
                            _rx = _w3t.eth.get_transaction_receipt(_cur_hash)
                            if _rx:
                                _got_rx = True
                                if _rx["status"] == 1:
                                    _bnb = _parse_bnb(_rx)
                                    print(f"✅ [FM BC] Sell confirmed: {_cur_hash[:12]} | BNB: {_bnb:.6f}")
                                    _push_notif("success","✅ BC Sell Confirmed",
                                        f"{_t_name} confirmed | BNB:{_bnb:.6f} | TX:{_cur_hash[:12]}",_t_name,_t_addr)
                                    # FIX v26: actual BNB + tokens position mein save karo
                                    # _fm_confirm_close sahi exit price calculate karega
                                    try:
                                        _pos_upd = auto_trade_stats["running_positions"].get(_t_addr, {})
                                        if _pos_upd:
                                            _pos_upd["_actual_bnb_received"] = _bnb
                                            # Tokens sold — tx logs se parse karo
                                            for _lg in _rx["logs"]:
                                                _tp = _lg.get("topics", [])
                                                if len(_tp) >= 3:
                                                    _d = _lg.get("data", "")
                                                    _d = _d.hex() if isinstance(_d, bytes) else _d
                                                    try:
                                                        _tok_amt = int(_d, 16) / 1e18
                                                        if _tok_amt > 1000:  # reasonable token amount
                                                            _pos_upd["_actual_tokens_sold"] = _tok_amt
                                                            break
                                                    except Exception:
                                                        pass
                                    except Exception as _upd_e:
                                        print(f"⚠️ [FM v26] Position update error: {str(_upd_e)[:40]}")
                                    _fm_confirm_close(_t_addr, 100.0, "BC sell confirmed", _cur_hash)
                                    with _fm_selling_lock: _fm_selling_set.discard(_t_addr.lower())
                                    return
                                else:
                                    _retries += 1
                                    print(f"❌ [FM BC] Reverted: {_cur_hash[:12]} — turant retry #{_retries}")
                                    if _retries > _max_retry: break
                                    _nh = _retry_sell(_t_addr, _w3t, _retries)
                                    if _nh: _cur_hash = _nh.hex()
                                    else: _retries = _max_retry + 1
                                break
                        except Exception: pass
                        _ttr.sleep(0.3)
                    if not _got_rx:
                        _retries += 1
                        print(f"⏳ [FM BC] No receipt 20s — retry #{_retries} higher gas")
                        if _retries > _max_retry: break
                        _nh = _retry_sell(_t_addr, _w3t, _retries)
                        if _nh: _cur_hash = _nh.hex()
                        else: break
                print(f"🚨 [FM BC] Sell {_max_retry} retry fail — MANUALLY SELL!")
                _push_notif("critical","🚨 MANUAL SELL REQUIRED",
                    f"{_t_name} sell {_max_retry}x fail! MANUALLY SELL! TX:{_cur_hash[:12]}",_t_name,_t_addr)
                with _fm_selling_lock: _fm_selling_set.discard(_t_addr.lower())
            except Exception as _te:
                print(f"⚠️ [FM BC] tracker error: {_te}")
                with _fm_selling_lock: _fm_selling_set.discard(_t_addr.lower())

        import threading as _th
        _th.Thread(
            target=_bc_track_and_parse,
            args=(tx_hash.hex(), token_addr, token_addr[:10], _w3_fast, _min_funds),
            daemon=True
        ).start()

        print(f"✅ [FM] Sell TX sent — background tracker active: {tx_hash.hex()[:12]}")
        return result

    except Exception as e:
        result["error"] = str(e)[:60]
        print(f"❌ [FM] Sell error: {result['error']}")
        # FIX v15: Exception pe bhi lock release
        with _fm_selling_lock: _fm_selling_set.discard(token_addr.lower())
        return result

def _fm_honeypot_sim(token_addr, factory_addr, w3=None):
    """
    Bonding curve pe honeypot sim:
    Tiny buy → check tokens received → tiny sell test
    """
    try:
        if not w3: w3 = _fm_get_w3()
        if not w3: return True  # assume safe if no w3

        fc = w3.eth.contract(address=Web3.to_checksum_address(factory_addr), abi=_FM_BC_ABI)
        _tiny = int(0.001 * 1e18)  # 0.001 BNB

        # Simulate buy via eth_call — v12: 3 params correct
        buy_result = fc.functions.buyTokenAMAP(
            Web3.to_checksum_address(token_addr),
            _tiny,
            0
        ).call({"value": _tiny})

        if buy_result <= 0:
            return False  # honeypot — buy fail

        # Simulate sell — v14: 7 params (from added)
        _sim_wallet = Web3.to_checksum_address(BSC_WALLET or REAL_WALLET)
        sell_result = fc.functions.sellToken(
            0,
            Web3.to_checksum_address(token_addr),
            _sim_wallet,
            buy_result,
            0,
            0,
            "0x0000000000000000000000000000000000000000"
        ).call()

        if sell_result <= 0:
            return False  # honeypot — sell fail

        return True  # safe

    except Exception as e:
        err = str(e).lower()
        if "revert" in err or "honeypot" in err:
            return False
        return True  # other errors = proceed

# REMOVED: duplicate _fm_real_sell_bc (old buggy version with approve) — FIX A

def _save_fm_event(token_addr, liq_bnb, grad_price, snipe_price, pump_pct, result, skip_reason, time_ms,
                   buyers_at_entry=0, momentum_pct=0.0, volume_change=0.0, pump_at_entry=0.0, dev_wallet_pct=0.0, mc_usd=0.0, total_buys_at_entry=0,
                   # FIX v32: ye params pehle undefined the — NameError se har event fail hota tha
                   stage1_ms=0, stage2_ms=0, buy_submit_ms=0,
                   price1=0.0, price2=0.0, actual_fill=0.0, slippage_pct=0.0,
                   entry_type=""):
    """FM event Supabase mein save karo — extra analytics data bhi"""
    try:
        if not supabase: return
        try:
            _cnt = supabase.table("fm_events").select("id", count="exact").execute()
            if (_cnt.count or 0) >= 5000:
                _old = supabase.table("fm_events").select("id").order("detected_at", desc=False).limit(500).execute()
                _ids = [r["id"] for r in (_old.data or []) if r.get("id")]
                if _ids:
                    supabase.table("fm_events").delete().in_("id", _ids).execute()
        except: pass
        supabase.table("fm_events").insert({
            "token_address":    token_addr,
            "token_short":      token_addr[:10],
            "detected_at":      datetime.now(_IST).isoformat(),
            "liquidity_bnb":    round(float(liq_bnb or 0), 6),
            "grad_price":       float(grad_price or 0),
            "snipe_price":      float(snipe_price or 0),
            "pump_pct":         round(float(pump_pct or 0), 2),
            "result":           result,
            "skip_reason":      skip_reason or "",
            "time_taken_ms":    int(time_ms or 0),
            "mode":             TRADE_MODE,
            # Extra analytics
            "buyers_at_entry":  int(buyers_at_entry or 0),
            "momentum_pct":     round(float(momentum_pct or 0), 2),
            "volume_change":    round(float(volume_change or 0), 6),
            "pump_at_entry":    round(float(pump_at_entry or 0), 2),
            "dev_wallet_pct":   round(float(dev_wallet_pct or 0), 2),
            "mc_usd":              round(float(mc_usd or 0), 0),
            "total_buys_at_entry": int(total_buys_at_entry or 0),
            # FIX v32: Timing debug columns
            "stage1_ms":           int(stage1_ms or 0),
            "stage2_ms":           int(stage2_ms or 0),
            "buy_submit_ms":       int(buy_submit_ms or 0),
            "price1":              float(price1 or 0),
            "price2":              float(price2 or 0),
            "actual_fill":         float(actual_fill or 0),
            "slippage_pct":        round(float(slippage_pct or 0), 2),
            "entry_type":          str(entry_type or ""),
}).execute()

        # Post-skip tracking — 5 min baad price check karo
        if result == "SKIP" and skip_reason != "blacklisted":
            def _check_skip_ath(ta):
                try:
                    time.sleep(300)  # 5 min wait
                    _w = _fm_get_w3()
                    if not _w: return
                    _info = _fm_get_token_info(ta, _w)
                    if not _info: return
                    _lp = _info.get("lastPrice", 0)
                    if _lp <= 0: return
                    # Compare with saved snipe_price
                    if snipe_price > 0:
                        _ath_pct = round((_lp / snipe_price - 1) * 100, 1)
                        if _ath_pct > 10:
                            print(f"📊 [FM] Post-skip ATH: {ta[:10]} +{_ath_pct:.1f}% (skip: {skip_reason[:30]})")
                except: pass
            if snipe_price > 0:
                threading.Thread(target=_check_skip_ath, args=(token_addr,), daemon=True).start()

    except Exception as e:
        print(f"⚠️ [FM] event save error: {e}")

def _fm_snipe(token_addr, dev_addr="", detected_at=0.0):
    """
    Four.meme Bonding Curve Sniper v2 — ULTIMATE OPTIMIZED
    - Parallel Stage1 + gas/nonce (3 threads)
    - Dynamic poll time (0.5-1s)
    - Parallel price + buyers check
    - Cached token info (0.5s TTL)
    - Dynamic threshold based on volume
    """
    addr_lower = token_addr.lower()
    _t_start = time.time()

    # FIX v49: _handle_token mein already _fm_sniped check + add ho gaya — duplicate check removed

    # FIX v31: DEBUG — token detect timestamp
    print(f"⏱️ [FM-DEBUG] DETECTED | +0ms | token={token_addr[:10]} dev={dev_addr[:10] if dev_addr else '?'}")
    # FIX v32: Timing trackers — Supabase save ke liye
    _dbg_stage1_ms   = 0
    _dbg_stage2_ms   = 0
    _dbg_buy_ms      = 0
    _dbg_price1      = 0.0
    _dbg_price2      = 0.0
    _dbg_actual_fill = 0.0
    _dbg_slippage    = 0.0

    # Cache helper
    def _get_token_info_cached(addr, w3, ttl=0.5):
        key = addr.lower()
        now = time.time()
        if key in _info_cache and now - _info_cache[key]['ts'] < ttl:
            return _info_cache[key]['data']
        data = _fm_get_token_info(addr, w3)
        if data:
            _info_cache[key] = {'data': data, 'ts': now}
            # FIX v50: cache size limit — unbounded growth prevent karo
            if len(_info_cache) > 200:
                # Oldest entries remove karo
                _sorted = sorted(_info_cache.items(), key=lambda x: x[1]['ts'])
                for _ck, _ in _sorted[:50]:
                    _info_cache.pop(_ck, None)
        return data

    # Analytics containers
    _s1_mc_usd = [0.0]
    _s1_pump_at_entry = [0.0]
    _s1_dev_wallet = [0.0]
    _s2_momentum_pct = [0.0]
    _s2_volume_change = [0.0]
    _s2_buyers = [0]
    _s2_total_buys = [0]

    def _skip(reason):
        ms = int((time.time() - _t_start) * 1000)
        print(f"⏭️ [FM] SKIP — {reason}: {token_addr[:10]}")
        if "no wallet" in reason or "no wallet/key" in reason:
            _push_notif("critical", "🔴 No Wallet/Key", "WALLET_PRIVATE_KEY set nahi hai — real trading blocked!", token_addr[:10], token_addr)
        elif "QuickNode not available" in reason:
            _push_notif("critical", "🔴 QuickNode Down", "QuickNode unavailable — check credits!", token_addr[:10], token_addr)
        elif "insufficient wallet balance" in reason:
            _push_notif("critical", "🔴 Low Balance", f"{reason} — wallet top up karo!", token_addr[:10], token_addr)
        elif "real buy error" in reason:
            _push_notif("critical", "🔴 Buy TX Failed", f"{reason}", token_addr[:10], token_addr)
        elif "Stage2 snapshot failed" in reason:
            _push_notif("warning", "🟡 RPC Issue", "Stage2 snapshot fail — QuickNode check karo", token_addr[:10], token_addr)
        threading.Thread(target=_save_fm_event, args=(
            token_addr, 0, 0, 0, 0, "SKIP", reason, ms
        ), kwargs={
            "pump_at_entry":      _s1_pump_at_entry[0],
            "dev_wallet_pct":     _s1_dev_wallet[0],
            "mc_usd":             _s1_mc_usd[0],
            "momentum_pct":       _s2_momentum_pct[0],
            "volume_change":      _s2_volume_change[0],
            "buyers_at_entry":    _s2_buyers[0],
            "total_buys_at_entry":_s2_total_buys[0],
            # FIX v32: timing debug data bhi save karo SKIP events mein
            "stage1_ms":          _dbg_stage1_ms,
            "stage2_ms":          _dbg_stage2_ms,
            "price1":             _dbg_price1,
            "price2":             _dbg_price2
}, daemon=True).start()

    try:
        if not AUTO_TRADE_ENABLED or not FM_SNIPER_ENABLED:
            print(f"⛔ [FM] Snipe blocked: AUTO={AUTO_TRADE_ENABLED} FM={FM_SNIPER_ENABLED}")
            return
        if len(auto_trade_stats.get("running_positions", {})) >= AUTO_MAX_POSITIONS:
            _skip("max positions"); return
        sess = get_or_create_session(AUTO_SESSION_ID)
        if sess.get("paper_balance", 5.0) < AUTO_BUY_SIZE_BNB:
            _skip("low balance"); return
        _ok, _msg = DataGuard.bnb_price_ok()
        if not _ok: _skip(f"DataGuard: {_msg}"); return
        if is_token_blacklisted(token_addr): _skip("blacklisted"); return
        # FIX v29: O(1) set lookup — 10k list scan avoid karo
        _th_addrs = {t.get("address","").lower() for t in auto_trade_stats.get("trade_history", [])}
        if addr_lower in _th_addrs:
            _skip("already traded"); return
        if dev_addr and is_dev_blacklisted(dev_addr):
            _skip(f"dev blacklisted: {dev_addr[:10]}"); return

        # ========== STAGE 1 — PRE-FILTER (parallel) ==========
        # FIX v31: DEBUG — Stage1 shuru
        print(f"⏱️ [FM-DEBUG] STAGE1 START | +{int((time.time()-_t_start)*1000)}ms | token={token_addr[:10]}")
        # FIX v29: RPC pool — har snipe pe naya Web3 object nahi, reuse karo
        _w3a = _get_stage1_w3()
        if not _w3a:
            _push_notif("critical", "🔴 Stage1 RPC Down", "Sare free RPCs down hain — tokens filter nahi ho rahe!", token_addr[:10], token_addr)
            _skip("Stage1 RPC unavailable"); return

        import concurrent.futures as _cf
        _info_res = [None]
        _dev_pct_res = [0.0]
        _pre_gas = [0]
        _pre_nonce = [0]
        # FIX v29: _price1 baseline Stage 1 ke parallel — 500ms pehle momentum window shuru
        _price_baseline = [0]
        _funds_baseline = [0]

        def _fetch_token_info():
            _info_res[0] = _fm_get_token_info(token_addr, _w3a)

        def _fetch_dev_balance():
            if not dev_addr: return
            try:
                _tc = _w3a.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_FM_ERC20_ABI)
                _total = _tc.functions.totalSupply().call()
                _dev_b = _tc.functions.balanceOf(Web3.to_checksum_address(dev_addr)).call()
                if _total > 0:
                    _dev_pct_res[0] = round(_dev_b / _total * 100, 2)
            except: pass

        def _prefetch_gas_nonce_parallel():
            try:
                _w3pf = _get_w3q()
                if _w3pf:
                    _pre_gas[0] = _fm_get_cached_gas(_w3pf)
                    if TRADE_MODE == "real":
                        _wa = BSC_WALLET or REAL_WALLET
                        if _wa:
                            _pre_nonce[0] = _w3pf.eth.get_transaction_count(_wa, "pending")
            except: pass

        def _fetch_price_baseline():
            # FIX v47: _fetch_token_info result reuse karo — duplicate RPC call hataya
            try:
                if _info_res[0]:
                    _price_baseline[0] = _info_res[0].get("lastPrice", 0)
                    _funds_baseline[0] = _info_res[0].get("funds", 0)
            except: pass

        with _cf.ThreadPoolExecutor(max_workers=3) as _ex:
            f1 = _ex.submit(_fetch_token_info)
            f2 = _ex.submit(_fetch_dev_balance)
            f3 = _ex.submit(_prefetch_gas_nonce_parallel)
            # Wait for token info first, then extract baseline (no extra RPC call)
            try: f1.result(timeout=2)
            except: pass
            _fetch_price_baseline()

        info = _info_res[0]
        if not info:
            _skip("tokenInfo fetch failed"); return
        if info["liquidityAdded"]:
            _skip("already graduated"); return

        _fee_rate = info.get("tradingFeeRate", 0)
        if _fee_rate > 300:
            _skip(f"high fee {_fee_rate/100:.1f}% > 3%"); return

        _last_price = info.get("lastPrice", 0)
        _total_supply = 1_000_000_000
        _bnb_price = market_cache.get("bnb_price", 640)
        _quote_mc = info.get("quote", "").lower()
        _USDT_L = "0x55d398326f99059ff775485246999027b3197955"
        _BUSD_L = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
        _WBNB_L = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
        _ZERO_ADDR = "0x0000000000000000000000000000000000000000"
        if _last_price > 0:
            if _quote_mc in [_USDT_L, _BUSD_L]:
                _mc_usd = (_last_price / 1e18) * _total_supply
            elif _quote_mc in [_WBNB_L, _ZERO_ADDR, ""]:
                _mc_usd = (_last_price / 1e18) * _total_supply * _bnb_price
            else:
                _skip(f"unsupported quote — skip"); return
            if _fm_filters.get("mc_max_enabled",True) and _mc_usd > _fm_filters["mc_max"]:
                _skip(f"MC too high ${_mc_usd:.0f} > ${_fm_filters['mc_max']}"); return
        else:
            _skip("MC calc failed"); return

        _offers = info.get("offers", 0)
        _maxOffers = info.get("maxOffers", 1)
        _pump_at_entry = round((_offers / max(_maxOffers, 1)) * 100, 1)
        if _fm_filters.get("pump_max_enabled",True) and _pump_at_entry > _fm_filters["pump_max"]:
            _skip(f"pump at entry {_pump_at_entry:.1f}% > {_fm_filters['pump_max']}%"); return

        if _fm_filters.get("dev_wallet_enabled",True) and _dev_pct_res[0] > _fm_filters["dev_wallet_max"]:
            _skip(f"dev wallet too high {_dev_pct_res[0]:.0f}%"); return

        _dev_wallet_pct = _dev_pct_res[0]
        _s1_mc_usd[0] = _mc_usd
        _s1_pump_at_entry[0] = _pump_at_entry
        _s1_dev_wallet[0] = _dev_wallet_pct

        _s1_ms = int((time.time() - _t_start) * 1000)
        _dbg_stage1_ms = _s1_ms  # FIX v32: Supabase ke liye save
        print(f"✅ [FM] Stage1 PASS: mc=${_mc_usd:.0f}")
        # FIX v31: DEBUG — Stage1 kitne ms mein complete hua
        print(f"⏱️ [FM-DEBUG] STAGE1 DONE | +{_s1_ms}ms | mc=${_mc_usd:.0f} pump={_s1_pump_at_entry[0]:.1f}% dev={_s1_dev_wallet[0]:.1f}% price_baseline={_price_baseline[0]:.6e}")

        # ========== STAGE 2 — ULTIMATE OPTIMIZED MOMENTUM ==========
        w3q = _get_w3q()
        w3 = w3q
        if not w3: _skip("QuickNode not available"); return

        # FIX v47: Pre-sampling — Stage1 filter pass hone ke baad se hi
        # background mein price history collect karna shuru karo
        # Jab momentum loop shuru ho, already 3-5 samples ready honge
        _pre_price_history = []
        _pre_funds_history = []
        _pre_price_samples = []
        _pre_stop = [False]

        def _pre_sample_loop():
            _w3ps = w3q
            if not _w3ps: return
            while not _pre_stop[0] and len(_pre_price_history) < 8:
                try:
                    _snap = _fm_get_token_info(token_addr, _w3ps)
                    if _snap and not _snap.get("liquidityAdded"):
                        _p = _snap.get("lastPrice", 0)
                        _f = _snap.get("funds", 0)
                        if _p > 0:
                            _pre_price_history.append(float(_p))
                            _pre_funds_history.append(float(_f))
                            _pre_price_samples.append(float(_p))
                except: pass
                time.sleep(0.1)

        import threading as _th47
        _pre_sampler = _th47.Thread(target=_pre_sample_loop, daemon=True)
        _pre_sampler.start()

        # FIX v29: _price_baseline Stage 1 ke saath fetch hua — reuse karo
        # FIX v61: Stage2 snapshot retry — QuickNode rate limit pe ek baar retry karo
        def _get_stage2_snapshot():
            for _attempt in range(3):
                try:
                    _snap = _get_token_info_cached(token_addr, w3, ttl=0.5)
                    if _snap: return _snap
                    if _attempt < 2: time.sleep(0.3)
                except: 
                    if _attempt < 2: time.sleep(0.3)
            return None

        if _price_baseline[0] > 0:
            _price1 = _price_baseline[0]
            _funds1 = _funds_baseline[0]
            _info_fresh = _get_stage2_snapshot()
            if not _info_fresh: _pre_stop[0] = True; _skip("Stage2 snapshot failed"); return
            if _info_fresh.get("liquidityAdded"): _pre_stop[0] = True; _skip("graduated before Stage2"); return
        else:
            _info_fresh = _get_stage2_snapshot()
            if not _info_fresh: _pre_stop[0] = True; _skip("Stage2 snapshot failed"); return
            if _info_fresh.get("liquidityAdded"): _pre_stop[0] = True; _skip("graduated before Stage2"); return
            _price1 = _info_fresh.get("lastPrice", 0)
            _funds1 = _info_fresh.get("funds", 0)

        _pre_stop[0] = True  # Pre-sampler stop karo

        _MIN_BUYERS = _fm_filters['buyers_min']
        _price2 = 0
        _funds2 = 0
        _ub = 0
        _total_buys = 0
        _dbg_price1 = float(_price1)  # FIX v32: Supabase ke liye
        # FIX v31: DEBUG — Stage2 shuru, price1 baseline log karo
        print(f"⏱️ [FM-DEBUG] STAGE2 START | +{int((time.time()-_t_start)*1000)}ms | price1={_price1:.6e} funds1={_funds1/1e18:.4f}BNB min_buyers={_MIN_BUYERS} pre_samples={len(_pre_price_history)}")
        # FIX v34: Ultra-fast momentum monitor — 90-second max window, early exit
        _t_start_loop = time.time()
        _t_end_loop = _t_start_loop + _fm_filters.get("momentum_window_sec", 90)
        _check_interval = _fm_filters.get("momentum_interval_sec", 0.1)

        _price1 = _price_baseline[0] if _price_baseline[0] > 0 else _price1
        _funds1 = _funds_baseline[0] if _funds_baseline[0] > 0 else _funds1

        _momentum_hit = False
        _buyers_checked = False
        _ub = 0
        _total_buys = 0
        _block_wallets_prev = {}
        _block_wallets_curr = {}
        _funds_prev = _funds1
        _bc_prev = 0.0
        _fake_count = 0
        _last_fail_reasons = []
        # FIX v47: Pre-sampled history inject karo — momentum faster detect hoga
        _price_history = list(_pre_price_history[-6:])
        _funds_history = list(_pre_funds_history[-6:])
        _price_samples = list(_pre_price_samples[-6:])
        _ub_history    = []   # holders increasing trend track
        _iter_count    = 0    # SPEED: buyers throttle counter

        print(f"⏱️ [FM-DEBUG] MOMENTUM MONITOR START | window={_fm_filters.get('momentum_window_sec', 90)}s interval={_check_interval}s pre_samples={len(_price_history)}")

        import concurrent.futures as _cf2
        _mom_executor = _cf2.ThreadPoolExecutor(max_workers=2)

        def _parallel_fetch(results_dict):
            def _fetch_price():
                try:
                    info = _get_token_info_cached(token_addr, w3, 0.08)
                    results_dict["info"] = info
                except Exception:
                    results_dict["info"] = None
            def _fetch_buyers():
                # SPEED: buyers har 3rd iteration pe fetch — on-chain heavy call
                if _iter_count % 3 != 0:
                    results_dict["ub"] = _ub          # cached value reuse
                    results_dict["tb"] = _total_buys
                    results_dict["bw"] = _block_wallets_curr
                    return
                try:
                    ub, tb, bw = _fm_get_unique_buyers(token_addr, w3)
                    results_dict["ub"] = ub
                    results_dict["tb"] = tb
                    results_dict["bw"] = bw
                except Exception:
                    results_dict["ub"] = _ub; results_dict["tb"] = _total_buys; results_dict["bw"] = _block_wallets_curr
            f1 = _mom_executor.submit(_fetch_price)
            f2 = _mom_executor.submit(_fetch_buyers)
            try: f1.result(timeout=0.8)
            except: pass
            try: f2.result(timeout=0.8)
            except: pass

        def _check_genuine(price_history, funds_history, ub_history, price_samples):
            score = 0
            reasons = []

            if len(price_history) < 6 or len(funds_history) < 6:
                return False, ["insufficient_data"], 0

            # 1. Price sustained (at least 4/6 green)
            green_price = sum(1 for i in range(1, len(price_history)) if price_history[i] > price_history[i-1])
            if green_price >= 4:
                score += 2
            else:
                reasons.append("price_not_sustained(" + str(green_price) + "/6)")

            # 2. Volume sustained
            green_vol = sum(1 for i in range(1, len(funds_history)) if funds_history[i] > funds_history[i-1])
            if green_vol >= 4:
                score += 2
            else:
                reasons.append("vol_flat_or_dying(" + str(green_vol) + "/6)")

            # 3. Holders strictly increasing (only if enough data)
            if len(ub_history) >= 6:
                green_holders = sum(1 for i in range(1, len(ub_history)) if ub_history[i] > ub_history[i-1])
                if green_holders >= 5:
                    score += 2
                else:
                    reasons.append("holders_stagnant(" + str(green_holders) + "/6)")
            else:
                score += 2  # data nahi hai — penalize mat karo

            # 4. No big spike (bot wash trade)
            steady = True
            if len(price_samples) >= 4:
                diffs = [abs(price_samples[i] - price_samples[i-1]) / max(price_samples[i-1], 1e-18) * 100 for i in range(1, len(price_samples))]
                if max(diffs) > 50:
                    steady = False
                    reasons.append("spike_" + str(int(max(diffs))) + "%")
            if steady:
                score += 2

            # 5. Single-block heavy buy concentration
            if len(ub_history) >= 2:
                deltas = [ub_history[i] - ub_history[i-1] for i in range(1, len(ub_history))]
                max_delta = max(deltas) if deltas else 0
                if max_delta > 8:
                    avg_delta = sum(deltas) / len(deltas)
                    if max_delta > avg_delta * 2.5:
                        reasons.append("heavy_block_buy(" + str(max_delta) + ")")
                        score -= 1

            # 6. Pump ke dauran volume drop
            # FIX v53: -2 → -1 wapas, threshold 6→5
            # -2 zyada aggressive tha — genuine coins bhi reject ho rahe the
            # max score=8, -1 se 7 milega agar baaki sab pass — still genuine
            if len(funds_history) >= 3:
                recent_vol_drop = funds_history[-1] <= funds_history[-2] and funds_history[-2] <= funds_history[-3]
                recent_price_up = price_history[-1] > price_history[-2]
                if recent_price_up and recent_vol_drop:
                    reasons.append("pump_with_vol_drop")
                    score -= 1

            genuine = score >= 6
            return genuine, reasons, score
        while time.time() < _t_end_loop and not _BOT_SHUTDOWN:
            try:
                _elapsed = time.time() - _t_start_loop
                _res = {}
                _parallel_fetch(_res)
                _info_current = _res.get("info")
                if not _info_current:
                    time.sleep(_check_interval); continue
                if _info_current.get("liquidityAdded"):
                    _skip("graduated during momentum check"); return
                _price2 = _info_current.get("lastPrice", 0)
                _funds2 = _info_current.get("funds", 0)
                if _price2 <= 0:
                    time.sleep(_check_interval); continue
                _ub         = _res.get("ub", _ub)
                _total_buys = _res.get("tb", _total_buys)
                _block_wallets_prev = _block_wallets_curr.copy() if _block_wallets_curr else {}
                _block_wallets_curr = _res.get("bw", {})
                _iter_count += 1
                _price_samples.append(float(_price2))
                if len(_price_samples) > 6: _price_samples.pop(0)
                _price_history.append(float(_price2))
                if len(_price_history) > 6: _price_history.pop(0)
                _funds_history.append(float(_funds2))
                if len(_funds_history) > 6: _funds_history.pop(0)
                _ub_history.append(int(_ub))
                if len(_ub_history) > 6: _ub_history.pop(0)
                _max_funds = _info_current.get("maxFunds", 1)
                _bc_curr = (_funds2 / max(_max_funds, 1)) * 100 if _max_funds else 0
                if _bc_prev == 0: _bc_prev = _bc_curr
                _momentum_current = round((_price2 - _price1) / max(_price1, 1) * 100, 2)
                _funds_diff = (_funds2 - _funds1) / 1e18 if _funds2 else 0
                _target_momentum = _fm_filters.get("momentum_min", 25)
                _vol_ok = not _fm_filters.get('vol_min_enabled', True) or _funds_diff >= _fm_filters['vol_min']
                if not _vol_ok and _ub >= 10 and _momentum_current >= 50 and _funds_diff >= 0.5:
                    _vol_ok = True
                    print(f"⚡ Exception rule triggered: buyers={_ub} momentum={_momentum_current:.1f}% vol={_funds_diff:.4f}BNB")
                _basic_ok = _momentum_current >= _target_momentum and _ub >= _fm_filters['buyers_min'] and _vol_ok
                if _basic_ok:
                    _is_genuine, _fail_reasons, _gm_score = _check_genuine(
                        _price_history, _funds_history, _ub_history, _price_samples
                    )
                    _funds_prev = _funds2; _bc_prev = _bc_curr
                    if _is_genuine:
                        _dbg_price2    = float(_price2)
                        _dbg_stage2_ms = int((time.time() - _t_start) * 1000) - _dbg_stage1_ms
                        print(f"✅ [FM] GENUINE MOMENTUM! score={_gm_score}/4 +{_momentum_current:.1f}% in {_elapsed:.1f}s | vol={_funds_diff:.4f}BNB | buyers={_ub}")
                        print(f"⏱️ [FM-DEBUG] EARLY EXIT | +{int((time.time()-_t_start)*1000)}ms | momentum={_momentum_current:.1f}% | target={_target_momentum}%")
                        _momentum_hit = True
                        break
                    else:
                        _fake_count += 1
                        _last_fail_reasons = _fail_reasons
                        if _fail_reasons == ["insufficient_data"]:
                            print(f"⏳ [GM] WAIT data collecting... iter={_iter_count} mom={_momentum_current:.1f}%")
                        else:
                            print(f"🚫 [GM] FAKE score={_gm_score}/4 | fail={_fail_reasons} | mom={_momentum_current:.1f}%")
                else:
                    _funds_prev = _funds2; _bc_prev = _bc_curr
                if int(_elapsed) % 5 == 0 and _elapsed > 0:
                    print(f"⏱️ [FM] Monitoring... {_elapsed:.0f}s | mom={_momentum_current:.1f}% target={_target_momentum}% vol={_funds_diff:.4f}BNB buyers={_ub}")
                time.sleep(_check_interval)
            except Exception as _me:
                print(f"⚠️ [FM] momentum error: {str(_me)[:50]}")
                time.sleep(_check_interval)
        
        try: _mom_executor.shutdown(wait=False)
        except: pass
        if not _momentum_hit:
            _s2_volume_change[0] = round((_funds2 - _funds1) / 1e18, 6) if _funds2 else 0
            if _fake_count > 0:
                _fail_str = ",".join(str(r) for r in _last_fail_reasons[:2])
                _skip(f"fake momentum x{_fake_count} ({_fail_str})")
            else:
                _skip(f"no momentum in {_fm_filters.get('momentum_window_sec', 90)}s")
            return

        # FIX v33: New momentum threshold check based on analysis
        _momentum_actual = round((_price2 - _price1) / max(_price1, 1) * 100, 2)
        if _fm_filters.get("momentum_min_enabled", True) and _momentum_actual < _fm_filters.get("momentum_min", 25):
            _skip(f"momentum {_momentum_actual:.1f}% < {_fm_filters['momentum_min']}%"); return

        # ✅ BUG FIX: timeout pe bhi buyers check — _MIN_BUYERS bypass nahi hoga
        if _ub < _MIN_BUYERS:
            _s2_buyers[0] = _ub
            _skip(f"low buyers {_ub} < {_MIN_BUYERS}"); return

        _funds_diff = (_funds2 - _funds1) / 1e18
        _s2_volume_change[0] = round(_funds_diff, 6)
        _s2_momentum_pct[0] = round((_price2 - _price1) / max(_price1, 1) * 100, 1)
        _s2_buyers[0] = _ub
        _s2_total_buys[0] = _total_buys

        if _fm_filters.get('vol_min_enabled',True) and _funds_diff < _fm_filters['vol_min']:
            # FIX v33d: Exception rule for high conviction trades
            if _fm_filters.get('vol_min_enabled', True):
                # Exception: buyers≥10 AND momentum≥50% → volume≥0.5 allowed
                _exception_allowed = False
                if _ub >= 10 and _momentum_actual >= 50 and _funds_diff >= 0.5:
                    _exception_allowed = True
                    print(f"⚡ Exception rule triggered: buyers={_ub} momentum={_momentum_actual:.1f}% vol={_funds_diff:.4f}BNB (min 0.5)")
                if not _exception_allowed and _funds_diff < _fm_filters['vol_min']:
                    _skip(f"low volume {_funds_diff:.4f} BNB < {_fm_filters['vol_min']}"); return
        _momentum_pct = round((_price2 - _price1) / max(_price1, 1) * 100, 1)

        print(f"✅ [FM] ALL PASS: mc=${_mc_usd:.0f} momentum=+{_momentum_pct:.1f}%")
        _scanner_stats["fm_discovered"] = _scanner_stats.get("fm_discovered", 0) + 1

        # ========== SMART ENTRY: Buy/Sell pressure check ==========
        # Momentum confirm ho gaya — ab check karo ki abhi buy pressure hai ya sell
        _entry_price_check = _price2  # momentum confirm waqt ka price
        _entry_type = "direct"        # CSV ke liye: direct / waited / skipped

        # FIX: strictly falling price = sell pressure, flat/rising = ok
        _ph = _price_history
        _fh = _funds_history

        if len(_ph) >= 2:
            _price_falling = _ph[-1] < _ph[-2]   # strictly gir raha ho
        else:
            _latest_p = _ph[-1] if _ph else _price2
            _price_falling = _latest_p < _price1  # baseline se neeche = falling

        if len(_fh) >= 2:
            _vol_falling = _fh[-1] < _fh[-2]
        else:
            _vol_falling = _price_falling

        # Sell pressure = price actually gir raha ho (flat = ok, rising = ok)
        _sell_pressure = _price_falling and _vol_falling

        if _sell_pressure:
            # Sell pressure hai — reversal ka wait karo max 10s
            print(f"⏳ [FM] Sell pressure at entry — waiting reversal (max 10s)")
            _reversal_found = False
            _price_low = _entry_price_check
            for _ri in range(50):  # 50 x 0.2s = 10s max
                time.sleep(0.2)
                try:
                    _ri_info = _get_token_info_cached(token_addr, w3, ttl=0.1)
                    if not _ri_info: break
                    if _ri_info.get("liquidityAdded"):
                        _skip("graduated during entry wait"); return
                    _ri_price = _ri_info.get("lastPrice", 0)
                    if _ri_price <= 0: continue
                    if _ri_price < _price_low:
                        _price_low = _ri_price
                    elif _ri_price > _price_low * 1.01:
                        # Low se 1% upar = sellers gone, reversal confirm
                        print(f"✅ [FM] Reversal confirmed: price={_ri_price:.2e} low={_price_low:.2e} — ENTERING")
                        _entry_price_check = _ri_price
                        _reversal_found = True
                        _entry_type = "waited"
                        break
                except Exception:
                    break
            if not _reversal_found:
                _entry_type = "skipped"
                _skip("sell pressure — no reversal in 10s"); return
        else:
            print(f"✅ [FM] Buy pressure confirmed (price={'falling' if _price_falling else 'rising/flat'} vol={'falling' if _vol_falling else 'rising/flat'}) — ENTERING NOW")

        # ========== BUY EXECUTION ==========
        size_bnb = _anti_mev_amount(AUTO_BUY_SIZE_BNB)
        token_name = token_addr[:8]
        try:
            # FIX v29: _price2 = Stage 2 ka latest price — info["lastPrice"] stale hota hai
            entry = _price2 / 1e18 if _price2 > 0 else (info["lastPrice"] / 1e18 if info.get("lastPrice", 0) > 0 else 1e-12)
        except:
            entry = 1e-12

        # FIX v31: DEBUG — buy execute shuru — detect se yahan tak total time
        print(f"⏱️ [FM-DEBUG] BUY START | +{int((time.time()-_t_start)*1000)}ms | entry_price={entry:.6e} BNB | size={size_bnb:.4f}BNB | mode={TRADE_MODE}")

        if TRADE_MODE == "real":
            try:
                wallet_addr = BSC_WALLET or REAL_WALLET
                pk = os.getenv("WALLET_PRIVATE_KEY", "") or os.getenv("PRIVATE_KEY", "") or os.getenv("REAL_PRIVATE_KEY", "")
                if not wallet_addr or not pk:
                    _skip("no wallet/key"); return
                _w3_buy = _get_w3q()
                if not _w3_buy:
                    _skip("QuickNode not available"); return
                # FIX v48: Prefetched nonce + balance reuse — Stage1 mein already fetch hua tha
                # Fresh RPC calls avoid karo buy time pe — ~400ms saved
                _bal_check = 0.0
                _fresh_nonce = 0
                try:
                    # Balance: fresh fetch (safety check — zaroori hai)
                    _bal_check = _w3_buy.eth.get_balance(Web3.to_checksum_address(wallet_addr)) / 1e18
                except: pass
                if _bal_check < size_bnb + 0.002:
                    _skip(f"insufficient wallet balance {_bal_check:.4f} BNB"); return
                _min_tokens = 0
                # Nonce: Stage1 prefetch reuse, sirf stale hone pe fresh fetch
                if _pre_nonce[0] > 0:
                    _fresh_nonce = _pre_nonce[0]
                    print(f"⚡ [FM v48] Prefetched nonce reused: {_fresh_nonce}")
                else:
                    try:
                        _fresh_nonce = _w3_buy.eth.get_transaction_count(Web3.to_checksum_address(wallet_addr), "pending")
                    except: pass
                    print(f"⚡ [FM v48] Fresh nonce fetched: {_fresh_nonce}")

                fc = _w3_buy.eth.contract(address=Web3.to_checksum_address(_FM_FACTORY_ADDR), abi=_FM_BC_ABI)
                tx = fc.functions.buyTokenAMAP(
                    Web3.to_checksum_address(token_addr),
                    int(size_bnb * 1e18),
                    _min_tokens
                ).build_transaction({
                    "from": wallet_addr,
                    "value": int(size_bnb * 1e18),
                    "gas": 400000,
                    "gasPrice": int((_pre_gas[0] or _fm_get_cached_gas(_w3_buy)) * 1.5),
                    "nonce": _fresh_nonce,
                    "chainId": 56
})
                from eth_account import Account
                signed = Account.sign_transaction(tx, pk)
                tx_hash = _w3_buy.eth.send_raw_transaction(signed.raw_transaction)
                # FIX v32: Supabase ke liye buy submit ms
                _dbg_buy_ms = int((time.time()-_t_start)*1000)
                # FIX v31: DEBUG — TX blockchain pe submit — detect se yahan tak
                print(f"✅ [FM] Real buy sent: {tx_hash.hex()}")
                print(f"⏱️ [FM-DEBUG] TX SENT | +{_dbg_buy_ms}ms | tx={tx_hash.hex()[:16]} | entry={entry:.6e} BNB")

                def _wait_receipt(_th, _w3b, _addr):
                    try:
                        _r = _w3b.eth.wait_for_transaction_receipt(_th, timeout=60)
                        if _r["status"] == 1:
                            print(f"✅ [FM] Buy confirmed: {_th.hex()[:12]}")
                            # FIX: Actual fill price fetch karo — entry update karo
                            try:
                                _real_info = _fm_get_token_info(_addr, _w3b)
                                if _real_info and _real_info.get("lastPrice", 0) > 0:
                                    _real_entry = _real_info["lastPrice"] / 1e18
                                    with monitor_lock:
                                        if _addr in monitored_positions:
                                            monitored_positions[_addr]["entry"] = _real_entry
                                            monitored_positions[_addr]["current"] = _real_entry
                                    with monitor_lock:
                                        if _addr in auto_trade_stats.get("running_positions", {}):
                                            auto_trade_stats["running_positions"][_addr]["entry"] = _real_entry
                                    # FIX v31: DEBUG — actual fill vs intended — slippage track karo
                                    _slip = round((_real_entry - entry) / max(entry, 1e-18) * 100, 2)
                                    print(f"✅ [FM] Entry updated: {_real_entry:.10f} (was {entry:.10f})")
                                    print(f"⏱️ [FM-DEBUG] TX CONFIRMED | intended={entry:.6e} | actual_fill={_real_entry:.6e} | slippage={_slip:+.2f}% | tx={_th.hex()[:16]}")
                                    try:
                                        if supabase:
                                            # FIX v41 H: Restore timing data update — b54a874 ne delete kiya tha
                                            supabase.table("fm_events").update({
                                                "actual_fill":   float(_real_entry),
                                                "slippage_pct":  float(_slip),
                                                "buy_submit_ms": int(_dbg_buy_ms or 0),
                                                "stage1_ms":     int(_dbg_stage1_ms or 0),
                                                "stage2_ms":     int(_dbg_stage2_ms or 0),
                                                "price1":        float(_dbg_price1 or 0),
                                                "price2":        float(_dbg_price2 or 0),
                                            }).eq("token_address", _addr).order("detected_at", desc=True).limit(1).execute()
                                    except Exception as _dbu:
                                        print(f"⚠️ [FM] Supabase timing update error: {str(_dbu)[:50]}")
                            except Exception as _ep:
                                print(f"⚠️ [FM] Entry update error: {str(_ep)[:40]}")
                            def _pre_approve(_addr2):
                                try:
                                    _pk2 = os.getenv("WALLET_PRIVATE_KEY","") or os.getenv("PRIVATE_KEY","") or os.getenv("REAL_PRIVATE_KEY","")
                                    _wa2 = BSC_WALLET or REAL_WALLET
                                    if not _wa2 or not _pk2: return
                                    _w3p = _get_w3q() or _fm_get_w3()
                                    if not _w3p: return

                                    # FIX v20: getTokenInfo se sahi tokenManager lo
                                    # _FM_FACTORY_ADDR hardcode galat tha — har token ka alag tokenManager hota hai
                                    _spender = _FM_FACTORY_ADDR  # safe fallback
                                    try:
                                        _tinfo2 = _fm_get_token_info(_addr2, _w3p)
                                        if _tinfo2:
                                            _tm2 = _tinfo2.get("tokenManager", "")
                                            if _tm2 and _tm2 != "0x0000000000000000000000000000000000000000":
                                                _spender = _tm2
                                                print(f"✅ [FM] Pre-approve spender (tokenManager): {_tm2[:10]}")
                                            else:
                                                print(f"⚠️ [FM] tokenManager empty — factory fallback use ho raha hai")
                                    except Exception as _te2:
                                        print(f"⚠️ [FM] Pre-approve getTokenInfo failed: {str(_te2)[:40]} — factory fallback")

                                    _tc2 = _w3p.eth.contract(address=Web3.to_checksum_address(_addr2), abi=_FM_ERC20_ABI)
                                    _allow = _tc2.functions.allowance(
                                        Web3.to_checksum_address(_wa2),
                                        Web3.to_checksum_address(_spender)
                                    ).call()
                                    if _allow > 0:
                                        print(f"✅ [FM] Already approved: {_addr2[:10]}"); return
                                    from eth_account import Account as _Acc
                                    _atx = _tc2.functions.approve(
                                        Web3.to_checksum_address(_spender),
                                        2**256 - 1
                                    ).build_transaction({
                                        "from": _wa2,
                                        "gas": 100000,
                                        "gasPrice": int(_fm_get_cached_gas(_w3p) * 1.5),  # FIX v23: was 3.0x
                                        "nonce": _w3p.eth.get_transaction_count(_wa2, "pending")
})
                                    _sa = _Acc.sign_transaction(_atx, _pk2)
                                    _ah = _w3p.eth.send_raw_transaction(_sa.raw_transaction)
                                    print(f"📡 [FM] Pre-approve TX sent: {_ah.hex()[:12]}...")

                                    # FIX v21: non-blocking poll — wait_for_receipt block karta tha 30s
                                    # Ab 2s interval pe check karo, max 30s
                                    import time as _t21
                                    for _pi in range(15):
                                        _t21.sleep(2)
                                        try:
                                            _rx = _w3p.eth.get_transaction_receipt(_ah)
                                            if _rx is not None:
                                                if _rx["status"] == 1:
                                                    print(f"✅ [FM] Pre-approve confirmed ({_pi*2+2}s): {_addr2[:10]} → spender: {_spender[:10]}")
                                                    # FIX v46: cache mein daal do — sell pe allowance RPC skip hogi
                                                    _cache_key = f"{_addr2.lower()}:{_spender.lower()}"
                                                    _fm_approved_cache[_cache_key] = True
                                                    print(f"⚡ [FM] Approve cached: {_cache_key[:24]}")
                                                else:
                                                    print(f"⚠️ [FM] Pre-approve TX failed onchain: {_addr2[:10]}")
                                                break
                                        except Exception:
                                            pass
                                    else:
                                        print(f"⚠️ [FM] Pre-approve not confirmed in 30s — sell pe runtime approve hoga")
                                except Exception as _pe:
                                    print(f"⚠️ [FM] Pre-approve error: {str(_pe)[:50]}")
                            threading.Thread(target=_pre_approve, args=(_addr,), daemon=True).start()
                        else:
                            print(f"⚠️ [FM] Buy failed onchain: {_th.hex()[:12]}")
                            _auto_paper_sell(_addr, "buy tx failed onchain", 100.0)
                    except Exception as _re:
                        print(f"⚠️ [FM] Receipt timeout: {str(_re)[:40]}")
                threading.Thread(target=_wait_receipt, args=(tx_hash, _w3_buy, token_addr), daemon=True).start()
            except Exception as e:
                _err = str(e)[:60]
                if "insufficient funds" in _err.lower():
                    _push_notif("critical", "🔴 FM Buy Failed", f"Insufficient balance: {_err}", token_name, token_addr)
                elif "nonce" in _err.lower():
                    _push_notif("critical", "🔴 FM Nonce Error", f"Nonce conflict: {_err}", token_name, token_addr)
                elif "slippage" in _err.lower() or "minAmount" in _err.lower():
                    _push_notif("warning", "🟡 FM Slippage", f"Price moved too fast: {_err}", token_name, token_addr)
                else:
                    _push_notif("critical", "🔴 FM Buy Error", f"{_err}", token_name, token_addr)
                _skip(f"real buy error: {_err}"); return
        else:
            sess["paper_balance"] = round(sess.get("paper_balance",5.0) - size_bnb, 6)

        ms = int((time.time() - _t_start) * 1000)
        try:
            _buyers_at_entry = min(int(_ub or 0), 2147483647)
            _total_buys_at_entry = min(int(_total_buys or 0), 2147483647)
        except:
            _buyers_at_entry = 0
            _total_buys_at_entry = 0

        # FIX v26: entry price actual TX se calculate karo
        # lastPrice stale hoti hai — actual BNB sent / tokens received = sahi price
        try:
            _w3_entry = _get_w3q() or _fm_get_w3()
            if _w3_entry:
                _tc_entry = _w3_entry.eth.contract(
                    address=Web3.to_checksum_address(token_addr), abi=_FM_ERC20_ABI)
                _token_bal = _tc_entry.functions.balanceOf(
                    Web3.to_checksum_address(BSC_WALLET or REAL_WALLET)).call()
                if _token_bal > 0:
                    _dec_entry = _get_dec(token_addr)
                    _tokens_received = _token_bal / (10 ** _dec_entry)
                    if _tokens_received > 0:
                        entry = size_bnb / _tokens_received
                        print(f"✅ [FM v26] Entry price from TX: {entry:.4e} BNB "
                              f"({_tokens_received:.0f} tokens for {size_bnb} BNB)")
        except Exception as _ep:
            print(f"⚠️ [FM v26] Entry price calc error: {str(_ep)[:50]} — lastPrice fallback")
        add_position_to_monitor(AUTO_SESSION_ID, token_addr, token_name, entry, size_bnb, stop_loss_pct=float(_fm_filters['stop_loss']))
        # FIX B: FM tradingFeeRate fetch karo — sell slippage ke liye
        _fm_fee_rate = 0.0
        try:
            _fm_info_fee = _fm_get_token_info(token_addr, _w3a) if _w3a else None
            if _fm_info_fee and _fm_info_fee.get("tradingFeeRate", 0) > 0:
                _fm_fee_rate = round(_fm_info_fee["tradingFeeRate"] / 1e18 * 100, 2)
        except Exception:
            pass

        auto_trade_stats["running_positions"][token_addr] = {
            "token": token_name,
            "entry": entry,
            "size_bnb": size_bnb,
            "orig_size_bnb": size_bnb,
            "bought_usd": round(size_bnb * market_cache.get("bnb_price",0), 2),
            "sl_pct": 10.0,   # tighter SL — max -10% loss hard floor
            "trail_pct": 20.0,        # FIX B: position manager ke liye
            "tp_sold": 0.0,
            "banked_pnl_bnb": 0.0,
            "bought_at": datetime.utcnow().isoformat(),
            "mode": TRADE_MODE,
            "source": "FM_BC",
            "fm_factory": _FM_FACTORY_ADDR,
            "fm_mc_usd": round(_mc_usd, 0),
            "fm_momentum": _momentum_pct,
            "fm_dev": dev_addr[:10] if dev_addr else "",
            "buy_tax":  _fm_fee_rate,  # FIX B: FM fee = "tax" sell ke liye
            "sell_tax": _fm_fee_rate,  # FIX B: same fee buy aur sell dono pe
            "buy_reasoning": {
                "source": "FM_BC_v2",
                "mc_usd": f"${_mc_usd:.0f}",
                "momentum": f"+{_momentum_pct:.1f}%"
}
}
        auto_trade_stats["total_auto_buys"] += 1
        _scanner_stats["fm_bought"] = _scanner_stats.get("fm_bought", 0) + 1
        threading.Thread(target=_persist_positions, daemon=True).start()

        def _fetch_token_name(ta):
            try:
                _w = _fm_get_w3()
                if not _w: return
                _tc = _w.eth.contract(address=Web3.to_checksum_address(ta), abi=_FM_ERC20_ABI)
                _sym = _tc.functions.symbol().call()
                _name = _tc.functions.name().call()
                _display = _sym or _name or ta[:8]
                if _display:
                    if ta in auto_trade_stats.get("running_positions", {}):
                        auto_trade_stats["running_positions"][ta]["token"] = _display
                    with monitor_lock:
                        if ta in monitored_positions:
                            monitored_positions[ta]["token"] = _display
                    _log("discover", _display, f"Token name: {_display}", ta)
                    print(f"✅ [FM] Token name: {_display}")
            except Exception as _ne:
                print(f"⚠️ [FM] Name fetch: {str(_ne)[:40]}")
        threading.Thread(target=_fetch_token_name, args=(token_addr,), daemon=True).start()

        _push_notif("success", "🚀 FM Bonding Curve!", f"{token_name} mc=${_mc_usd:.0f} momentum=+{_momentum_pct:.1f}% | {ms}ms", token_name, token_addr)
        _log("buy", token_name, f"🚀 FM BC v2 mc=${_mc_usd:.0f} momentum=+{_momentum_pct:.1f}% {ms}ms", token_addr)
        threading.Thread(target=_save_fm_event, args=(
            token_addr, round(_funds2/1e18, 4), 0, entry, _momentum_pct, "BUY", "", ms,
            _buyers_at_entry, _momentum_pct, round(_funds_diff, 6), _pump_at_entry, _dev_wallet_pct, _mc_usd, _total_buys_at_entry
        ), kwargs={
            # FIX v32: timing debug data BUY event ke liye bhi save karo
            "stage1_ms":    _dbg_stage1_ms,
            "stage2_ms":    _dbg_stage2_ms,
            "buy_submit_ms": _dbg_buy_ms,
            "price1":       _dbg_price1,
            "price2":       _dbg_price2,
            "actual_fill":  _dbg_actual_fill,
            "slippage_pct": _dbg_slippage,
            "entry_type":   _entry_type,
}, daemon=True).start()
        print(f"✅ [FM] BC SNIPED: {token_name} mc=${_mc_usd:.0f} momentum=+{_momentum_pct:.1f}% {ms}ms")

        def _fm_price_monitor(ta, t_name):
            """FM BC instant price monitor — har ~1s getTokenInfo se price update"""
            print(f"📡 [FM] Price monitor started: {ta[:10]}")
            _last_price = [0.0]
            _no_change_count = [0]
            _start_time = time.time()
            _buyer_checked = [False]

            while ta in auto_trade_stats.get("running_positions", {}):
                try:
                    _w3f = _get_w3q() or _fm_get_w3()
                    if not _w3f:
                        time.sleep(1); continue

                    _info = _fm_get_token_info(ta, _w3f)
                    if _info and _info.get("lastPrice", 0) > 0:
                        _bnb_p = market_cache.get("bnb_price", 0)
                        _quote  = str(_info.get("quote", "")).lower()
                        if "usdt" in _quote or "busd" in _quote or "usd" in _quote:
                            _price = (_info["lastPrice"] / 1e18) / _bnb_p if _bnb_p > 0 else 0
                        else:
                            _price = _info["lastPrice"] / 1e18

                        if _price > 0:
                            with monitor_lock:
                                if ta in monitored_positions:
                                    monitored_positions[ta]["current"] = _price
                                    if _price > monitored_positions[ta].get("high", 0):
                                        monitored_positions[ta]["high"] = _price

                            # Funds parallel update — position manager ke liye
                            _funds_now = _info.get("funds", 0) / 1e18
                            if _funds_now > 0:
                                _rp_fm = auto_trade_stats["running_positions"].get(ta)
                                if _rp_fm is not None:
                                    _fh = _rp_fm.get("_fm_funds_hist", [])
                                    _fh.append(_funds_now)
                                    if len(_fh) > 6: _fh.pop(0)
                                    _rp_fm["_fm_funds_hist"] = _fh

                            # Price change track karo
                            if _last_price[0] > 0:
                                _chg = abs(_price - _last_price[0]) / _last_price[0] * 100
                                if _chg < 0.01:  # 0.01% se kam change
                                    _no_change_count[0] += 1
                                else:
                                    _no_change_count[0] = 0
                            _last_price[0] = _price

                    # 30s baad buyers check karo — sirf ek baar
                    if not _buyer_checked[0] and (time.time() - _start_time) >= 30:
                        _buyer_checked[0] = True
                        try:
                            TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                            _cur = _w3f.eth.block_number
                            _logs = _w3f.eth.get_logs({
                                "address": Web3.to_checksum_address(ta),
                                "topics": [TRANSFER_TOPIC],
                                "fromBlock": _cur - 20,
                                "toBlock": "latest"
})
                            _ZERO = "0x0000000000000000000000000000000000000000"
                            _DEAD = "0x000000000000000000000000000000000000dead"
                            _FM_FAC = _FM_FACTORY_ADDR.lower()
                            new_buyers = set()
                            for _log in _logs:
                                if len(_log["topics"]) < 3: continue
                                _from = "0x" + _log["topics"][1].hex()[-40:].lower()
                                _to   = "0x" + _log["topics"][2].hex()[-40:].lower()
                                if _from == _FM_FAC and _to not in [_ZERO, _DEAD]:
                                    new_buyers.add(_to)
                            if len(new_buyers) < 2:
                                print(f"⚠️ [FM] No buyers 30s — force exit: {ta[:10]}")
                                _auto_paper_sell(ta, "FM No buyers 30s ❌", 100.0)
                            else:
                                print(f"✅ [FM] {len(new_buyers)} buyers 30s — holding: {ta[:10]}")
                        except Exception:
                            pass

                except Exception as _fe:
                    pass

                time.sleep(0.1)  # FIX v37 C: 0.2s → 0.1s — FM price 10x/sec

            print(f"📡 [FM] Price monitor stopped: {ta[:10]}")

        threading.Thread(target=_fm_price_monitor, args=(token_addr, token_name), daemon=True).start()

    except Exception as e:
        print(f"⚠️ [FM] snipe error: {e}")
        with _fm_sniped_lock: _fm_sniped.discard(addr_lower)

def poll_four_meme_v2():
    """
    FM Bonding Curve Sniper v2 — Continuous Parallel Polling
    3 workers always running — no sleep, max coverage
    """
    TOKEN_CREATE_SIGS = [
        ("0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20",
         "TokenCreate — primary (confirmed on-chain)"),
        ("0xb9d10aa6e0d565720d9f16b6d742668c3406afc3f2592b890549f66f78033b2c",
         "TokenCreate(address,address,uint256,uint256,uint256,uint256,string,string)"),
        ("0x3d96f13f99c3b0aca975bfbf0f185997444b7b43cd455e82b759dae94e99d3f7",
         "TokenCreate(address,address,uint256,uint256,uint256)"),
        ("0xed5b6552bf32030112553a7a7c5ba303430906006a8b80d86928cafbcc4c8e7d",
         "TokenCreate(address,address,uint256,uint256)"),
    ]

    _seen      = set()
    _seen_lock = threading.Lock()
    _active_topic = [None]

    # 3 different RPCs — each worker uses own RPC
    _RPCS = [
        "https://bsc-rpc.publicnode.com",
        "https://bsc.drpc.org",
        "https://1rpc.io/bnb",
    ]

    def _handle_token(token_addr, dev_addr):
        # FIX v49: Single lock mein dono check — race condition fix
        # Pehle _seen aur _fm_sniped alag locks the → 3 workers same token parallel process kar sakte the
        with _fm_sniped_lock:
            _al = token_addr.lower()
            if _al in _fm_sniped: return
            if _al in _seen: return
            _seen.add(_al)
            _fm_sniped.add(_al)  # turant add — koi doosra thread aage nahi nikal sakta
            _fm_sniped_ts[_al] = time.time()
            # FIX v55: _seen clear hone pe _fm_sniped bhi sync karo
            # _seen.clear() ke baad _fm_sniped mein stale entries block karti thi
            if len(_seen) > 1000:
                _seen.clear()
                # _fm_sniped bhi clear karo — 1h se purane hi rakho
                _now_ts = time.time()
                _stale = [k for k, t in _fm_sniped_ts.items() if _now_ts - t > 3600]
                for _sk in _stale:
                    _fm_sniped.discard(_sk)
                    _fm_sniped_ts.pop(_sk, None)

        if not FM_SNIPER_ENABLED: return

        # FIX v29: Thread launch se pehle hi reject karo — useless threads avoid
        if is_token_blacklisted(token_addr): return
        if dev_addr and is_dev_blacklisted(dev_addr): return
        if len(auto_trade_stats.get("running_positions", {})) >= AUTO_MAX_POSITIONS: return

        print(f"🆕 [FM] TokenCreate: {token_addr[:10]} dev:{dev_addr[:10] if dev_addr else '?'}")
        _scanner_stats["fm_discovered"] = _scanner_stats.get("fm_discovered", 0) + 1
        threading.Thread(target=_fm_snipe, args=(token_addr, dev_addr, time.time()), daemon=True).start()

    def _worker(rpc_url, worker_id):
        """Continuous worker — always polling, no sleep"""
        print(f"✅ [FM] Poll worker {worker_id} started: {rpc_url[:30]}")
        _last_block = [0]
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
        while not _fm_stop_event.is_set():
            try:
                if not FM_SNIPER_ENABLED:
                    time.sleep(1); continue

                current = w3.eth.block_number

                # Same block — no new tokens, skip
                if current == _last_block[0]:
                    time.sleep(0.1); continue  # FIX v29: 0.5s → 0.1s

                from_block    = max(_last_block[0] + 1, current - 2)
                _last_block[0] = current

                topics_to_try = [_active_topic[0]] if _active_topic[0] else [t[0] for t in TOKEN_CREATE_SIGS]

                for topic_hash in topics_to_try:
                    try:
                        logs = w3.eth.get_logs({
                            "address":   Web3.to_checksum_address(_FM_FACTORY_ADDR),
                            "topics":    [[topic_hash]],
                            "fromBlock": from_block,
                            "toBlock":   "latest"
})

                        if logs and _active_topic[0] != topic_hash:
                            print(f"✅ [FM] Active topic: {topic_hash[:10]}")
                            _active_topic[0] = topic_hash

                        for log in logs:
                            _data   = log.get("data", "")
                            _data_hex = _data.hex() if hasattr(_data, "hex") else str(_data)
                            if _data_hex.startswith("0x"): _data_hex = _data_hex[2:]

                            if len(_data_hex) < 128: continue

                            dev_addr   = "0x" + _data_hex[24:64]
                            token_addr = "0x" + _data_hex[88:128]

                            if not token_addr: continue

                            _handle_token(token_addr, dev_addr)
                        break

                    except Exception: continue

            except Exception as e:
                err = str(e)
                if "429" in err or "Too Many" in err:
                    time.sleep(2)
                else:
                    time.sleep(0.5)

    # 3 workers — each on different RPC, always running
    for i, rpc in enumerate(_RPCS):
        threading.Thread(target=_worker, args=(rpc, i+1), daemon=True).start()

    print("✅ [FM] 3 parallel poll workers started — continuous, no sleep")


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

def _decode_swap(log: dict, pair_info: dict) -> str:
    """
    PancakeSwap v2 Swap event decode karo — buy ya sell?
    pair_info = {"token": addr, "token0_is_wbnb": bool, "pair": addr}
    Returns: "buy" / "sell" / "unknown"
    """
    try:
        raw = log.get("data", "0x")
        if len(raw) < 130:
            return "unknown"
        raw_hex = raw[2:]  # 0x remove karo
        a0in  = int(raw_hex[0:64],   16)
        a1in  = int(raw_hex[64:128], 16)

        wbnb_is_t0 = pair_info.get("token0_is_wbnb", False)

        if wbnb_is_t0:
            # token0 = WBNB, token1 = token
            # BUY:  WBNB in  (a0in > 0)
            # SELL: WBNB out (a1in > 0)
            return "buy" if a0in > 0 else ("sell" if a1in > 0 else "unknown")
        else:
            # token0 = token, token1 = WBNB
            # BUY:  WBNB in  (a1in > 0)
            # SELL: token in (a0in > 0)
            return "buy" if a1in > 0 else ("sell" if a0in > 0 else "unknown")
    except Exception:
        return "unknown"


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

    _cs5 = os.getenv("BSC_WSS", "")
    WSS_ENDPOINTS = []
    if _cs5: WSS_ENDPOINTS.append(_cs5)
    WSS_ENDPOINTS += [
        "wss://bsc-rpc.publicnode.com",
        "wss://bsc.publicnode.com",
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



def run_full_sniper_checklist(address: str, prefetched_dex: dict = None) -> Dict:
    result = {
        "address": address, "checklist": [],
        "overall": "UNKNOWN", "score": 0, "total": 0,
        "recommendation": "", "dex_data": {}
    }

    # ── STEP 1: Honeypot.is — on-chain buy/sell simulation (fastest reject) ──
    hp_is_honeypot = False
    hp_buy_tax     = 0.0
    hp_sell_tax    = 0.0
    hp_label       = "Unknown"
    try:
        hp_json        = _get_honeypot(address)
        if hp_json:
            hp_is_honeypot = hp_json.get("isHoneypot", False)
            hp_sim         = hp_json.get("simulationResult", {}) or {}
            hp_buy_tax     = float(hp_sim.get("buyTax",  0) or 0)
            hp_sell_tax    = float(hp_sim.get("sellTax", 0) or 0)
            hp_label       = hp_json.get("honeypotResult", {}).get("name", "Unknown") if hp_is_honeypot else "Safe"
            print(f"🍯 Honeypot.is: {address[:10]}... → {'HONEYPOT ❌' if hp_is_honeypot else 'SAFE ✅'} buy={hp_buy_tax:.1f}% sell={hp_sell_tax:.1f}%")
    except Exception as e:
        print(f"⚠️ Honeypot.is error: {e}")

    # Honeypot detected — immediate reject, skip GoPlus + DexScreener
    if hp_is_honeypot:
        result["checklist"].append({"label": "Honeypot Check", "status": "fail", "value": f"HONEYPOT ({hp_label})", "stage": 1})
        result["overall"]        = "DANGER"
        result["score"]          = 0
        result["total"]          = 1
        result["recommendation"] = f"❌ HONEYPOT — sell blocked on-chain ({hp_label})"
        return result

    # ── STEP 2: GoPlus — deep static analysis (cached 5 min) ──
    goplus_data = {}
    for _gp_try in range(2):  # 2 retries (parallel fetch already done)
        try:
            goplus_data = _get_goplus(address)
            if goplus_data:
                break
            if _gp_try < 1:
                time.sleep(0.5)  # 500ms only — parallel se already cached hoga
        except Exception as e:
            print(f"⚠️ GoPlus error (try {_gp_try+1}): {e}")
            if _gp_try < 1:
                time.sleep(0.5)

    goplus_empty = not bool(goplus_data)
    result["_goplus_raw"] = goplus_data  # green signals ke liye
    bscscan_source = "verified" if _gp_str(goplus_data, "is_open_source", "0") == "1" else ""

    # ── STEP 3: DexScreener — use prefetched data if available (avoid double call) ──
    if prefetched_dex is not None:
        dex_data = get_dexscreener_token_data(address, prefetched_raw=prefetched_dex)
    else:
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

    # Honeypot.is result — safe tokens ke liye bhi show karo
    add("Honeypot Check", "pass", f"SAFE (buy={hp_buy_tax:.1f}% sell={hp_sell_tax:.1f}%)", 1)
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
    add(f"Liquidity ≥ {cs['min_liq_bnb']} BNB", "pass" if liq_bnb > cs['min_liq_bnb'] else "fail", f"{liq_bnb:.2f} BNB", 1)
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

    # ── Minimum holder count check ──
    _holder_count = int(goplus_data.get("holder_count", 0) or len(holders_list) or 0)
    _min_holders  = 50
    add("Min Holders ≥ 50",
        "pass" if _holder_count >= _min_holders else "fail",
        f"{_holder_count} holders", 1)

    suspicious  = _gp_bool_flag(goplus_data, "is_airdrop_scam")
    creator_pct = _gp_float(goplus_data, "creator_percent") * 100

    add(f"Top Holder < {cs['max_top_holder']}%",   "pass" if top_holder < cs['max_top_holder'] else ("warn" if top_holder < cs['max_top_holder']*2 else "fail"), f"{top_holder:.1f}%", 1)
    add(f"Top 10 Holders < {cs['max_top10']}%",    "pass" if top10_pct  < cs['max_top10']      else ("warn" if top10_pct  < cs['max_top10']*1.25 else "fail"), f"{top10_pct:.1f}%", 1)
    add("No Suspicious Clustering", "pass" if not suspicious   else "fail", "CLEAN" if not suspicious else "RISK", 1)
    add(f"Dev Wallet < {cs['max_creator_pct']}%",  "pass" if creator_pct < cs['max_creator_pct'] else ("warn" if creator_pct < cs['max_creator_pct']*3 else "fail"), f"{creator_pct:.1f}%", 1)

    honeypot  = _gp_bool_flag(goplus_data, "is_honeypot")
    can_sell  = not _gp_bool_flag(goplus_data, "cannot_sell_all")
    slippage_ok = sell_tax <= 10

    add("Honeypot Safe",           "fail" if honeypot    else "pass", "DANGER" if honeypot    else "SAFE", 2)
    add("Can Sell All Tokens",     "fail" if not can_sell else "pass", "NO"    if not can_sell else "YES",  2)
    add("Slippage OK",             "pass" if slippage_ok  else "fail", f"Sell={sell_tax:.0f}%",             2)

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

    # ── SNIPER DETECTION ──
    # ── Sniper Detection — pct>=5% AND amount>=$300 dono saath hona chahiye ──
    # Retail snipers (<$300) = ignore, dump power nahi
    # Whale snipers (5%+ hold, $300+) = real danger = FAIL
    _sniper_count = 0
    _sniper_bnb   = 0.0
    try:
        _holders_list = goplus_data.get("holders", []) or []
        _bnb_price    = max(market_cache.get("bnb_price", 600), 1)
        _liq_usd      = dex_data.get("liquidity_usd", 0) or 0

        if token_age_min < 15:
            for h in _holders_list[:10]:
                pct          = float(h.get("percent", 0) or 0) * 100
                _is_contract = h.get("is_contract", 0)
                if pct >= 3.0 and not _is_contract:
                    holder_usd = (_liq_usd * pct / 100) if _liq_usd > 0 else 0
                    if holder_usd >= 600:  # $600+ = real dump power
                        _sniper_count += 1
                        _sniper_bnb   += holder_usd / _bnb_price

        # Bot activity — first 5 min mein 20+ buys = suspicious
        if token_age_min < 5 and dex_data.get("buys_5m", 0) > 15:
            _sniper_count = max(_sniper_count, 3)

        if _sniper_count == 0:
            add("Sniper Detection", "pass", "No dangerous snipers ✅", 3)
        else:
            add("Sniper Detection", "fail",
                f"🚨 {_sniper_count} whale snipers ~{_sniper_bnb:.1f} BNB (5%+ hold + $300+) — SKIP", 3)

    except Exception as _se:
        add("Sniper Detection", "warn", "Check failed", 3)

    buys_5m = dex_data.get("buys_5m", 0); sells_5m = dex_data.get("sells_5m", 0)
    buys_1h = dex_data.get("buys_1h", 0); sells_1h = dex_data.get("sells_1h", 0)

    _dex_src = dex_data.get("source", "dexscreener")
    _no_txns = _dex_src == "geckoterminal"  # GeckoTerminal txn data nahi deta
    add("Buy > Sell (5min)", "pass" if buys_5m > sells_5m else ("warn" if _no_txns or buys_5m==0 else "warn"), f"B:{buys_5m} S:{sells_5m}" + (" (no txn data)" if _no_txns else ""), 4)
    add("Buy > Sell (1hr)",  "pass" if buys_1h > sells_1h else ("warn" if _no_txns or buys_1h==0 else "warn"), f"B:{buys_1h} S:{sells_1h}" + (" (no txn data)" if _no_txns else ""), 4)
    add(f"Volume 24h", "pass", f"${dex_data.get('volume_24h',0):,.0f} (not checked — sniper mode)", 4)

    # Stage 6 — DEX checks: GoPlus + DexScreener/GeckoTerminal dono se verify
    # Naye tokens GoPlus mein late index hote hain — isliye dex_data bhi check karo
    price_usd  = dex_data.get("price_usd", 0)
    change_1h  = dex_data.get("change_1h", 0)
    _dex_has_data = dex_data.get("_raw_pairs", False)  # DexScreener/GeckoTerminal se data aaya

    # GoPlus is_in_dex — naye tokens pe False aa sakta hai, dex_data se cross-verify karo
    in_dex_gp  = _gp_bool_flag(goplus_data, "is_in_dex")
    pool_count = len(dex_list) if isinstance(dex_list, list) else 0
    # Cross-verify: price_usd > 0 ZARURI hai — bina price ke koi trade nahi
    in_dex     = (in_dex_gp or _dex_has_data) and (price_usd > 0)
    # Pool: GoPlus pool list ya DexScreener data
    pool_ok    = (pool_count > 0 or _dex_has_data) and (price_usd > 0)

    add("Listed on DEX",
        "pass" if in_dex else "fail",
        ("GoPlus+DEX ✅" if in_dex_gp and _dex_has_data else
         "DEX only ✅"   if _dex_has_data else
         "GoPlus only ✅" if in_dex_gp else "NO ❌"), 6)

    add("DEX Pools",
        "pass" if pool_ok else "fail",
        f"{pool_count} pools (GoPlus)" if pool_count > 0 else
        ("1 pool (DEX data)" if _dex_has_data else "NO POOLS ❌"), 6)

    add("1h Price Change",  "pass" if change_1h > 0  else "warn", f"{change_1h:+.1f}%", 6)
    add("Price Exists",     "pass" if price_usd > 0  else "fail",
        f"${price_usd:.8f}" if price_usd > 0 else "NO PRICE", 6)

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
                    "sort":    "desc"
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
    momentum   = buys_5m_v >= 5 and vol_ratio >= 1.5
    _no_txns_v = dex_data.get("source", "dexscreener") == "geckoterminal"
    add("Buy Momentum (5m)",
        "pass" if momentum else ("warn" if (_no_txns_v or buys_5m_v >= 2) else "fail"),
        f"B:{buys_5m_v} S:{sells_5m_v} ratio:{vol_ratio:.1f}x" + (" (gecko-no txns)" if _no_txns_v else ""), 5)

    # ── FEATURE 5: Dev Wallet Blacklist ──
    creator_addr = goplus_data.get("creator_address", "")
    owner_addr   = goplus_data.get("owner_address", "")
    dev_blocked  = is_dev_blacklisted(creator_addr) or is_dev_blacklisted(owner_addr)
    add("Dev Not Blacklisted",
        "fail" if dev_blocked else "pass",
        "BLACKLISTED" if dev_blocked else "CLEAN", 5)

    # ── FEATURE 5b: Rug DNA Check ──
    # Pehle ke rug tokens se creator/tax/liq pattern match karo
    _buy_tax_gp  = float(goplus_data.get("buy_tax",  0) or 0)
    _sell_tax_gp = float(goplus_data.get("sell_tax", 0) or 0)
    _dna_result  = _check_rug_dna(creator_addr, _buy_tax_gp, _sell_tax_gp, liq_usd_dex)
    if _dna_result.get("match"):
        add("Rug DNA Clean",
            "fail",
            f"🧬 {_dna_result['reason']} (conf={_dna_result['confidence']}%)", 5)
    else:
        add("Rug DNA Clean", "pass", "No rug pattern match", 3)

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
                              "Creator Launch History",  # ✅ serial launcher = instant DANGER
                              "Sniper Detection",        # ✅ pre-sniped = instant DANGER
                              "Contract Verified"}       # ✅ unverified contract = instant DANGER
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

# ========== BOT DECISION LOGGER ==========
def _save_bot_decision(data: dict):
    """Har decision Supabase bot_decisions table mein save karo — background thread"""
    if not supabase:
        return
    try:
        row = {
            "token_address":        data.get("token_address", ""),
            "token_name":           data.get("token_name", ""),
            "decision":             data.get("decision", ""),
            "reason":               data.get("reason", ""),
            "thought":              data.get("thought", ""),
            "score":                data.get("score", 0),
            "total":                data.get("total", 0),
            "checklist":            data.get("checklist"),
            "signals":              data.get("signals"),
            "dex_data":             data.get("dex_data"),
            "pnl_pct":              data.get("pnl_pct"),
            "exit_reason":          data.get("exit_reason"),
            "discovery_source":     data.get("discovery_source", "unknown"),
            "discovery_ts":         data.get("discovery_ts", datetime.utcnow().isoformat()),
            "queue_wait_sec":       data.get("queue_wait_sec", 0),
            "prefilter_skip":       data.get("prefilter_skip", False),
            "prefilter_reason":     data.get("prefilter_reason"),
            "bnb_price_at_entry":   data.get("bnb_price_at_entry", market_cache.get("bnb_price", 0)),
            "fear_greed_at_entry":  data.get("fear_greed_at_entry", market_cache.get("fear_greed", 50)),
            "market_trend":         data.get("market_trend", "unknown"),
            "token_age_min":        data.get("token_age_min", 0),
            "discovery_to_buy_sec": data.get("discovery_to_buy_sec", 0),
            "hold_time_min":        data.get("hold_time_min"),
            "failed_check":         data.get("failed_check"),
            "creator_address":      data.get("creator_address"),
            "creator_launches":     data.get("creator_launches"),
            "whale_count":          data.get("whale_count"),
            "peak_price":           data.get("peak_price"),
            "left_on_table_pct":    data.get("left_on_table_pct"),
            "entry_price":          data.get("entry_price"),
            "exit_price":           data.get("exit_price"),
            "token_type":           data.get("token_type", "meme"),
            "market_condition":     data.get("market_condition", "unknown"),
            "exit_type":            data.get("exit_type")
}
        supabase.table("bot_decisions").insert(row).execute()
    except Exception as e:
        print(f"⚠️ bot_decision save error: {e}")

# ========== TRADE LOGGING ==========
def log_trade_internal(session_id: str, trade: dict):
    sess = get_or_create_session(session_id)
    pnl = float(trade.get("pnl_pct", 0))
    win = pnl > 0
    
    lesson = {
        "token": trade.get("token_address", ""),
        "entry_price": trade.get("entry_price", 0),
        "exit_price": trade.get("exit_price", 0),
        "pnl_pct": pnl,
        "win": win,
        "lesson": trade.get("lesson", ""),
        "timestamp": datetime.utcnow().isoformat()
    }
    
    # Sirf full sell ke liye count badhao
    if trade.get("sell_pct", 100.0) >= 100.0:
        sess["trade_count"] += 1
        if win:
            sess["win_count"] += 1
            sess["pnl_24h"] += pnl
        else:
            _size = float(trade.get("size_bnb", AUTO_BUY_SIZE_BNB) or AUTO_BUY_SIZE_BNB)
            _bnb_lost = _size * abs(pnl) / 100.0
            sess["daily_loss"] = sess.get("daily_loss", 0) + _bnb_lost
    
    # History save only for full sell
    if trade.get("sell_pct", 100.0) >= 100.0:
        if not isinstance(sess.get("pattern_database"), list):
            sess["pattern_database"] = []
        sess["pattern_database"].append(lesson)
        sess["pattern_database"] = sess["pattern_database"][-100:]
    
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
R12. OPEN POSITIONS: OPEN_POSITIONS_DETAIL field mein exact RAM data hai — token naam, entry, current price, PnL — yahi use karo, apne se mat banao.
R13. DB VERIFIED: DB_DECISIONS_VERIFIED field mein Supabase se verified decisions hain — token queries pe yahi reference karo.
R14. CROSS VERIFY: Agar RAM data aur DB data conflict kare — dono batao, apne se decide mat karo.
R8. PERMANENT_USER_RULES field — hamesha follow karo.
R9. USER ORDERS: User jo maange — karo. Agar possible nahi to seedha bolo.
R10. DISCOVERED TOKENS: Context mein list hai to naam aur address dono do.
R11. LEARNING CYCLES: Sirf real CYCLES number use karo — fake number kabhi nahi.
[END HARD RULES]

[ANTI-HALLUCINATION RULES — KABHI MAT TODO]
H1. GUESS MAT KARO: Agar DB mein data nahi hai — seedha bolo "Is token ka koi record nahi mila mujhe".
H2. PRICE: Token price aur BNB price alag hain — kabhi mix mat karo. Token price = 0.000001 BNB jaisa hota hai.
H3. TRADE HISTORY: Sirf woh trades bolo jo [RECENT_DECISIONS] ya context mein hain — apne se mat banao.
H4. WRONG MATCH: Ek token ka data doosre token pe mat chipkao — exact address match hona chahiye.
H5. SYSTEM STATS: Queue size, semaphore, monitoring count — sirf context mein jo diya hai wahi bolo.
H6. CONFLICT: Agar ek token DANGER tha aur buy hua — seedha bolo "Buy ke waqt SAFE tha, ab DANGER hai — liquidity change ho gayi".
H7. CONFIDENCE: Kabhi "100% sure" mat bolo — hamesha "DB ke hisaab se" ya "checklist ke hisaab se" bolo.
H8. NO APOLOGY FOR CORRECT TRADES: Agar trade profit mein tha — "khed" mat karo — sahi decision tha.
H9. TOKEN QUERY WITHOUT ADDRESS: Agar user kisi token ke baare mein pooche aur 0x address na de — pehle address maango. Bina address ke koi bhi decision, score, ya reason mat batao.
H10. ADDRESS MILA TO SIRF DB DATA: Jab address mile — sirf bot_decisions DB ka data batao. Agar record nahi mila to seedha bolo "Maine is token ko discover nahi kiya" — koi assumption nahi, koi guess nahi.
H11. ZERO INVENTION: Checklist score, failed stage, skip reason — kabhi apne se mat banao. Sirf jo DB mein hai wahi batao.
[END ANTI-HALLUCINATION RULES]

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
        # ── Recent trade post-mortems LLM ko do ──
        _recent_hist = [t for t in auto_trade_stats.get("trade_history", [])[-10:] if isinstance(t, dict)]
        _pm_ctx = " | ".join(
            f"{t.get('token','?')}:{t.get('result','?').upper()}({t.get('pnl_pct',0):+.0f}%)-{t.get('reason','?')[:20]}"
            for t in _recent_hist[-5:]
        ) if _recent_hist else ""

        _auto_sess    = get_or_create_session(AUTO_SESSION_ID)
        _auto_balance = _auto_sess.get("paper_balance", 5.0)
        _auto_trades  = _auto_sess.get("trade_count", 0)
        _auto_wins    = _auto_sess.get("win_count", 0)
        _auto_wr      = round(_auto_wins / _auto_trades * 100, 1) if _auto_trades > 0 else 0
        _auto_pos     = len(auto_trade_stats.get("running_positions", {}))
        _auto_pnl     = round(auto_trade_stats.get("auto_pnl_total", 0.0), 2)

        _queue_size   = len(new_pairs_queue)
        _monitor_size = len(monitored_positions)
        _positions    = len(auto_trade_stats.get("running_positions", {}))
        _discovered   = len(discovered_addresses)
        _cycles       = brain.get("total_learning_cycles", 0)

        # ── Open positions detail — RAM se verified ──
        _pos_detail_parts = []
        for _pa, _pv in list(auto_trade_stats.get("running_positions", {}).items())[:10]:
            _tok   = _pv.get("token", _pa[:8])
            _entry = _pv.get("entry", 0)
            _size  = _pv.get("size_bnb", 0)
            _mon   = monitored_positions.get(_pa, {})
            _cur   = _mon.get("current", _entry)
            _pnl   = round(((_cur - _entry) / _entry * 100), 1) if _entry > 0 else 0
            _tp    = _pv.get("tp_sold", 0)
            _sl    = _pv.get("sl_pct", 12)
            _pos_detail_parts.append(
                f"{_tok}|entry={_entry:.2e}|cur={_cur:.2e}|pnl={_pnl:+.1f}%|size={_size:.4f}BNB|tp_sold={_tp:.0f}%|sl={_sl:.0f}%|addr={_pa[:10]}"
            )
        _pos_detail_str = " || ".join(_pos_detail_parts) if _pos_detail_parts else "none"

        # ── bot_decisions recent — DB se verified ──
        _bd_ctx_inline = ""
        try:
            if supabase:
                _bd_r = supabase.table("bot_decisions").select(
                    "token_name,token_address,decision,reason,failed_check,score,total,created_at"
                ).order("created_at", desc=True).limit(15).execute()
                if _bd_r.data:
                    _bd_parts = []
                    for _r in _bd_r.data:
                        _d  = _r.get("decision","?")
                        _t  = _r.get("token_name","?")
                        _rs = (_r.get("reason","") or "")[:40]
                        _fc = (_r.get("failed_check","") or "")[:20]
                        _sc = _r.get("score",0)
                        _tt = _r.get("total",1)
                        _bd_parts.append(f"{_d}|{_t}|{_rs}|fail={_fc}|{_sc}/{_tt}")
                    _bd_ctx_inline = "[DB_DECISIONS_VERIFIED]" + " ; ".join(_bd_parts) + "[/DB_DECISIONS_VERIFIED]"
        except Exception:
            pass

        ctx = (
            f"\n[BNB=${market_cache['bnb_price']:.2f}|F&G={market_cache['fear_greed']}/100"
            f"|Paper={session_data.get('paper_balance',5.0):.3f}BNB"
            f"|Trades={trade_count} WR={win_rate_str}"
            f"|NewPairs={_queue_size}|Monitoring={_monitor_size}|OpenPositions={_positions}"
            f"|TokensDiscovered={_discovered}"
            f"|LearningCyclesExact={_cycles}"
            + (f"|Brain:{brain_ctx}" if brain_ctx else "")
            + (f"|Learned:{learn_ctx}" if learn_ctx else "")
            + (f"|SA:{sa_ctx}" if sa_ctx else "")
            + (f"|User:{user_ctx}" if user_ctx and user_ctx != "NEW_USER" else "")
            + f"|AUTO_BAL={_auto_balance:.4f}|AUTO_POS={_auto_pos}|AUTO_WR={_auto_wr}%|AUTO_PNL={_auto_pnl}%"
            + (f"|RecentTrades:{_pm_ctx}" if _pm_ctx else "")
            + f"|OPEN_POSITIONS_DETAIL={_pos_detail_str}"
            + (f"|{_bd_ctx_inline}" if _bd_ctx_inline else "")
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
        messages += [{"role": m["role"], "content": m["content"]} for m in history[-12:]]  # mem opt

        _perm_rules = user_profile.get("user_rules", [])
        _perm_str = (" | UserRules: " + " | ".join(_perm_rules[-3:])) if _perm_rules else ""
        rules_reminder = (
            f"\n[REAL_CYCLES={brain.get('total_learning_cycles',0)}]"
            f"\n[REPLY: 1.NO NAAM 2.SHORT 3.NO INTERNAL VARS{_perm_str}]"
        )
        messages.append({"role": "user", "content": user_message + ctx + rules_reminder})

        reply_text = None
        # Round-robin keys — MODEL_NAME pehle, MODEL_FAST fallback
        for _model in [MODEL_NAME, MODEL_FAST]:
            try:
                reply_text = client.chat(model=_model, messages=messages, max_tokens=1000)
                if reply_text:
                    break
            except NoProvidersAvailableError:
                raise
            except Exception as e:
                print(f"⚠️ Model {_model} fail: {str(e)[:60]}")

        return reply_text or "AI temporarily unavailable. Thodi der mein try karo."

    except NoProvidersAvailableError:
        return "⚠️ AI temporarily down. Thodi der mein try karo."
    except Exception as e:
        print(f"⚠️ LLM error: {e}")
        return f"🤖 Error: {str(e)[:80]}"

# ========== FLASK ROUTES ==========
def _persist_settings():
    """Sari current settings ek saath DB mein save karo"""
    if not supabase:
        print("⚠️ _persist_settings: supabase not connected — settings not saved!")
        return
    try:
        supabase.table("memory").upsert({
            "session_id": "MRBLACK_SETTINGS",
            "role":       "system",
            "content": json.dumps({
                "buy_amount":    AUTO_BUY_SIZE_BNB,
                "max_positions": AUTO_MAX_POSITIONS,
                "trade_mode":    TRADE_MODE,
                "real_wallet":   REAL_WALLET,
                "checklist":     CHECKLIST_SETTINGS
}),
            "updated_at": datetime.utcnow().isoformat()
        }, on_conflict="session_id").execute()
    except Exception as e:
        print(f"⚠️ Settings persist error: {e}")

def _load_all_settings_from_db():
    """Startup pe Supabase se sari settings load karo — restart ke baad bhi persist rahe"""
    global AUTO_BUY_SIZE_BNB, AUTO_MAX_POSITIONS, CHECKLIST_SETTINGS, TRADE_MODE, REAL_WALLET
    if not supabase:
        print("⚠️ _load_all_settings_from_db: supabase not connected")
        return
    try:
        import time as _t
        rows = []
        for _attempt in range(3):  # 3 retries
            res = supabase.table("memory").select("*").eq("session_id", "MRBLACK_SETTINGS").execute()
            rows = res.data if res and res.data else []
            if rows:
                break
            print(f"⚙️ Settings not found (attempt {_attempt+1}/3) — retrying in 2s...")
            _t.sleep(2)
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
                _load_all_settings_from_db()  # pehle — TRADE_MODE set hoga
            except Exception as e:
                print(f"Settings load error: {e}")
            try:
                _load_trade_history_from_db()  # baad mein — sahi TRADE_MODE pe
            except Exception as e:
                print(f"Trade history load error: {e}")
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
                _load_notifs_from_db()
            except Exception as e:
                print(f"Notif load error: {e}")
            try:
                # AUTO session pre-warm — DB se load karo turant (race condition fix)
                get_or_create_session(AUTO_SESSION_ID)
                _load_session_from_db(AUTO_SESSION_ID)  # paper_balance DB se lo — 5.0 default nahi
                print(f"✅ AUTO session ready | balance={sessions.get(AUTO_SESSION_ID, {}).get('paper_balance', 5.0):.4f} BNB")
            except Exception as e:
                print(f"Session error: {e}")

        threading.Thread(target=_bg_init, daemon=True).start()

        # ✅ Dedicated BNB price loop — tries every 30s until price is live
        def _bnb_price_loop():
            import time as _t
            _sources = [
                ("OKX",          "https://www.okx.com/api/v5/market/ticker",           {"instId":"BNB-USDT"},                   lambda r: float(((r.json() or {}).get("data") or [{}])[0].get("last",0) or 0)),
                ("CoinPaprika",  "https://api.coinpaprika.com/v1/tickers/bnb-binance-coin", None, lambda r: float((r.json() or {}).get("quotes", {}).get("USD", {}).get("price", 0) or 0)),
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
                print("⚠️ NodeReal BNB WS stream disabled — using OKX price feed")
                return
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
                            market_cache["wss_status"] = "live"
                            fail = 0
                            while True:
                                msg  = await ws.recv()  # timeout=None
                                data = _j.loads(msg)
                                raw  = ((data.get("params") or {}).get("result") or {}).get("data", "")
                                if raw:
                                    price = _parse_price(raw)
                                    if price > 10:
                                        market_cache["bnb_price"]    = round(price, 4)
                                        market_cache["last_updated"] = datetime.utcnow().isoformat()
                                        market_cache["wss_status"]   = "live"
                                del msg, data
                    except Exception as e:
                        fail += 1
                        wait = min(5 * fail, 60)
                        market_cache["wss_status"] = f"retry({fail})"
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

        # threading.Thread(target=_delayed(poll_new_pairs, 10), daemon=True).start()  # PC only — disabled
        # Load sniper state from DB — restart pe same ON/OFF state
        _load_sniper_state()

        if FM_SNIPER_ENABLED:
            threading.Thread(target=_delayed(poll_four_meme_v2, 15), daemon=True).start()  # 🎓 FM v2
        else:
            print("🛑 FM Sniper OFF (saved state) — skipping WSS")
        # ⚡ PC Fast Sniper — background mein chalta hai, _pc_add_to_snipe_queue se trigger hota hai
        # threading.Thread(target=_delayed(start_swap_monitor, 20), daemon=True).start()  # PC only — disabled

        # ── Queue Workers Start ──────────────────────────────────

        threading.Thread(target=_delayed(price_monitor_loop,    15),  daemon=True).start()
        threading.Thread(target=_delayed(continuous_learning,   25),  daemon=True).start()
        threading.Thread(target=_delayed(auto_position_manager, 30),  daemon=True).start()
        threading.Thread(target=_delayed(_memory_cleanup_loop,  60),  daemon=True).start()  # MEM FIX
        # threading.Thread(target=_delayed(_whale_follow_loop, 120), daemon=True).start()  # PC only — disabled


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
                                # ✅ FIX: Stats overwrite nahi karo — _load_session_from_db pehle se sahi load kar chuka hai
                                # Sirf trade_history fallback karo agar RAM mein kuch nahi
                                _th = _pdb.get("trade_history", [])
                                if not auto_trade_stats.get("trade_history") and isinstance(_th, list) and _th:
                                    auto_trade_stats["trade_history"] = list(_th)
                                # Sirf total_scanned update karo agar zyada hai
                                _sc = _pdb.get("total_scanned", 0)
                                if _sc > 0 and _sc > brain.get("total_tokens_discovered_ever", 0):
                                    brain["total_tokens_discovered_ever"] = _sc
                                print(f"✅ Positions restore done | history={len(auto_trade_stats['trade_history'])} wins={auto_trade_stats['wins']} losses={auto_trade_stats['losses']}")
                        except Exception as _pdb_err:
                            print(f"⚠️ Auto stats restore error: {_pdb_err}")
                        if _saved:
                            _restored = 0
                            _skipped  = 0
                            _MAX_RESTORE = 200  # All open positions restore karo
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
                                        "mode":           _pd.get("mode", TRADE_MODE)
}
                                    add_position_to_monitor(AUTO_SESSION_ID, _addr, _pd.get("token", _addr[:10]), _entry, _size_bnb, _sl_pct)
                                    _restored += 1
                                    print(f"  ↳ Restored {_pd.get('token',_addr[:10])}: tp_sold={_tp_sold:.0f}% size={_size_bnb:.4f} sl={_sl_pct:.0f}%")
                            if _skipped:
                                print(f"🧹 Skipped {_skipped} positions (invalid entry price)")
                            print(f"✅ Restored {_restored} positions from Supabase")
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

        # Har position ko properly sell karo — trade history mein save hoga
        for addr in closed:
            try:
                _auto_paper_sell(addr, "Manual Close All 🔴", 100.0)
            except Exception as _ce:
                print(f"⚠️ Close position error {addr[:10]}: {_ce}")

        auto_trade_stats["last_action"] = "Manual reset"
        print(f"🔄 Admin reset: closed {count} positions")
        return jsonify({"status": "ok", "closed": count, "addresses": closed})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/verify-password", methods=["POST"])
def verify_password():
    data = request.get_json() or {}
    pwd  = data.get("password", "").strip()
    if pwd == SITE_PASSWORD:
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Wrong password"}), 401

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
        "paper_balance": sess.get("paper_balance", 5.0)
})

@app.route("/trading-data", methods=["GET", "POST"])
def trading_data():
  try:
    # FIX: Hamesha AUTO_SESSION_ID ka data do — random SID se ghost sessions mat banao
    # Random SID se get_or_create_session call hoti thi → memory leak
    _auto_sess_td = sessions.get(AUTO_SESSION_ID) or {"paper_balance":5.0,"trade_count":0,"win_count":0,"loss_count":0,"positions":[],"pnl":0,"daily_loss":0}
    bnb_price     = market_cache.get("bnb_price", 0) or 300  # BUG FIX: 0 nahi dikhao
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
    return jsonify({"error":"data_unavailable","bnb_price":300,"fear_greed":50,"paper":"5.0000","positions":[],"trade_count":0,"win_rate":0,"monitoring":0})

@app.route("/chat", methods=["POST"])
def chat():
    data       = request.get_json() or {}
    user_msg   = data.get("message", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())
    mode       = data.get("mode", "paper")
    if not user_msg:
        return jsonify({"reply": "Kuch toh bolo! 😅", "session_id": session_id})

    # ── Contract address detect karo — 0x paste kiya toh checklist explain karo ──
    import re as _re
    _addr_match = _re.search(r"0x[0-9a-fA-F]{40}", user_msg)
    if _addr_match:
        _ca = _addr_match.group(0)
        try:
            _ca_cs = Web3.to_checksum_address(_ca)
        except Exception:
            _ca_cs = None
        if _ca_cs:
            # Check if already in trade history
            _hist = auto_trade_stats.get("trade_history", [])
            _past = [t for t in _hist if t.get("address","").lower() == _ca.lower()]
            # Check if currently open
            _open = auto_trade_stats.get("running_positions", {}).get(_ca_cs, {})
            # Run checklist
            try:
                _res = run_full_sniper_checklist(_ca_cs)
                _ov  = _res.get("overall", "UNKNOWN")
                _sc  = _res.get("score", 0)
                _tot = _res.get("total", 1)
                _pct = round(_sc / max(_tot, 1) * 100, 1)
                _rec = _res.get("recommendation", "")
                _fails = [c["label"] for c in _res.get("checklist", []) if c.get("status") == "fail"][:3]
                _passes= [c["label"] for c in _res.get("checklist", []) if c.get("status") == "pass"][:3]
                _sigs  = [s["type"] for s in (_res.get("green_signals") or [])]

                _reply_parts = [f"🔍 Token: {_ca[:10]}..."]
                _reply_parts.append(f"📊 Checklist: {_ov} ({_sc}/{_tot} = {_pct}%)")
                if _ov == "SAFE":
                    _reply_parts.append(f"✅ Buy criteria pass — {', '.join(_sigs[:2]) if _sigs else 'checklist only'}")
                else:
                    _reply_parts.append(f"❌ Skip reason: {_rec[:80]}")
                    if _fails:
                        _reply_parts.append(f"Failed: {', '.join(_fails)}")
                # ── Trade history exact match ──
                if _past:
                    _last = _past[-1]
                    _result_str = _last.get("result","?").upper()
                    _pnl_str    = f"{_last.get('pnl_pct',0):+.1f}%"
                    _exit_str   = _last.get("reason","?")[:40]
                    _entry_str  = f"{_last.get('entry',0):.2e}"
                    _exit_p_str = f"{_last.get('exit',0):.2e}"
                    _reply_parts.append(f"📜 Trade: {_result_str} {_pnl_str} | Entry: {_entry_str} BNB | Exit: {_exit_p_str} BNB | Reason: {_exit_str}")
                    _pm = _last.get("post_mortem","")
                    if _pm:
                        _reply_parts.append(f"💡 Post-mortem: {_pm[:120]}")
                    # Buy reasoning bhi dikhao
                    _buy_rsn = (_last.get("buy_reasoning") or {})
                    if _buy_rsn.get("assumption"):
                        _reply_parts.append(f"🎯 Buy reason: {_buy_rsn['assumption'][:100]}")
                    # Checklist state at buy time
                    if _ov != (_last.get("buy_reasoning") or {}).get("overall",""):
                        _reply_parts.append(f"⚠️ Note: Buy ke waqt checklist alag thi — ab {_ov} hai (liquidity/price change ho sakta hai)")
                elif _open:
                    _pnl_now = ((monitored_positions.get(_ca_cs, {}).get("current", _open.get("entry",0)) - _open.get("entry",0)) / max(_open.get("entry",1e-18), 1e-18)) * 100
                    _entry_now = _open.get("entry", 0)
                    _reply_parts.append(f"👁️ Abhi open hai | Entry: {_entry_now:.2e} BNB | PnL: {_pnl_now:+.1f}%")
                else:
                    # bot_decisions table se EXACT address match karo
                    try:
                        if supabase:
                            _bd = supabase.table("bot_decisions").select(
                                "decision,reason,thought,score,total,failed_check,token_age_min,discovery_source,market_condition,created_at"
                            ).eq("token_address", _ca).order("created_at", desc=True).limit(1).execute()
                            if not _bd.data:
                                # lowercase try karo
                                _bd = supabase.table("bot_decisions").select(
                                    "decision,reason,thought,score,total,failed_check,token_age_min,discovery_source,market_condition,created_at"
                                ).eq("token_address", _ca.lower()).order("created_at", desc=True).limit(1).execute()
                            if _bd.data:
                                _row  = _bd.data[0]
                                _dec  = _row.get("decision", "?")
                                _rsn  = _row.get("reason", "")
                                _thgt = _row.get("thought", "")
                                _fail = _row.get("failed_check", "")
                                _age  = _row.get("token_age_min", 0)
                                _src  = _row.get("discovery_source", "")
                                _mkt  = _row.get("market_condition", "")
                                if _dec == "PREFILTER_SKIP":
                                    _reply_parts.append(f"🚫 Bot ne discover kiya lekin blacklisted tha — scan bhi nahi kiya")
                                elif _dec == "SKIP":
                                    _reply_parts.append(f"⏭️ Bot ne scan kiya — SKIP kiya")
                                    _reply_parts.append(f"❌ Reason: {_rsn[:100]}")
                                    if _fail:
                                        _reply_parts.append(f"💀 Failed check: {_fail}")
                                    if _thgt:
                                        _reply_parts.append(f"🧠 Bot ki soch: {_thgt[:150]}")
                                elif _dec == "BUY":
                                    _reply_parts.append(f"✅ Bot ne buy kiya tha")
                                    _reply_parts.append(f"📋 Reason: {_rsn[:100]}")
                                    if _thgt:
                                        _reply_parts.append(f"🧠 Bot ki soch: {_thgt[:150]}")
                                if _age:
                                    _reply_parts.append(f"⏱️ Token age: {_age:.0f}min | Market: {_mkt} | Source: {_src}")
                            else:
                                _reply_parts.append("📭 Is token ka koi record nahi mila — bot ne discover nahi kiya hoga")
                    except Exception as _bde:
                        _reply_parts.append("📭 DB se data nahi mila")

                _auto_reply = "\n".join(_reply_parts)
                sess2 = get_or_create_session(session_id)
                sess2["history"].append({"role": "user",      "content": user_msg})
                sess2["history"].append({"role": "assistant", "content": _auto_reply})
                return jsonify({"reply": _auto_reply, "session_id": session_id,
                                "trading": {"paper": f"{sess2.get('paper_balance',5.0):.3f}", "pnl": "+0.0%"}})
            except Exception as _ce:
                pass  # fallback to normal LLM
    sess = get_or_create_session(session_id)
    sess["mode"] = mode
        # FIX v5: daily_loss ab BNB mein hai, 15% of balance threshold
    _balance = sess.get("paper_balance", 5.0) or 5.0
    _daily_limit = _balance * 0.15  # 15% of current balance
    if sess.get("daily_loss", 0) >= _daily_limit:
        print(f"🛑 Auto-buy BLOCKED: daily_loss={sess.get('daily_loss',0):.4f} BNB >= {_daily_limit:.4f} BNB (15% of {_balance:.3f})")
        return jsonify({"reply": "🛑 Daily loss limit (8%) reach ho gaya. Kal fresh start karo!", "session_id": session_id})
    _extract_user_info_from_message(user_msg)
    # bot_decisions recent context LLM ko do
    _bd_ctx = ""
    try:
        if supabase:
            _bd_recent = supabase.table("bot_decisions").select(
                "decision,token_name,reason,thought,pnl_pct,exit_type,failed_check,created_at"
            ).order("created_at", desc=True).limit(5).execute()
            if _bd_recent.data:
                _bd_lines = []
                for _r in _bd_recent.data:
                    _d   = _r.get("decision","?")
                    _t   = _r.get("token_name","?")
                    _rs  = _r.get("reason","")[:50]
                    _pnl = _r.get("pnl_pct")
                    _pnl_str = f" PnL:{_pnl:+.1f}%" if _pnl is not None else ""
                    _bd_lines.append(f"{_d} {_t}: {_rs}{_pnl_str}")
                _bd_ctx = "\n[RECENT_DECISIONS]\n" + "\n".join(_bd_lines) + "\n[/RECENT_DECISIONS]"
    except Exception:
        pass
    sess["history"].append({"role": "user", "content": user_msg + _bd_ctx})
    reply = get_llm_reply(user_msg + _bd_ctx, sess["history"], sess)
    sess["history"].append({"role": "assistant", "content": reply})
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
        stop_loss_pct = float(data.get("stop_loss_pct", 12.0))
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
    acts = list(_bot_log)

    # System health events inject karo — top pe
    sys_events = []

    # WSS status
    wss = market_cache.get("wss_status", "unknown")
    if wss != "live":
        sys_events.append({
            "type":   "wss_error",
            "token":  "WSS",
            "detail": f"WebSocket status: {wss}",
            "ts":     _now_ist()
})

    # QuickNode — credits check (simple heuristic)
    _qn_errors = market_cache.get("qn_errors", 0)
    if _qn_errors > 5:
        sys_events.append({
            "type":   "ql_limit",
            "token":  "QuickNode",
            "detail": f"QuickNode errors: {_qn_errors} — check credits",
            "ts":     _now_ist()
})

    # BNB price stale
    _last_bnb = market_cache.get("last_updated", "")
    if _last_bnb:
        try:
            _age = (datetime.utcnow() - datetime.fromisoformat(_last_bnb)).total_seconds()
            if _age > 120:
                sys_events.append({
                    "type":   "error",
                    "token":  "BNB Price",
                    "detail": f"BNB price stale — last update {int(_age)}s ago",
                    "ts":     _now_ist()
})
        except Exception:
            pass

    return jsonify({"activity": sys_events + acts})

@app.route("/notifications", methods=["GET"])
def notifications_route():
    return jsonify({
        "notifications": _notifications[:50],
        "unread": sum(1 for n in _notifications if not n.get("read", False)),
        "total":  len(_notifications)
})

@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    data = request.get_json() or {}
    nid  = data.get("id")  # specific id ya "all"
    if nid == "all":
        for n in _notifications:
            n["read"] = True
    else:
        for n in _notifications:
            if n.get("id") == nid:
                n["read"] = True
                break
    threading.Thread(target=_save_notifs_to_db, daemon=True).start()
    return jsonify({"status": "ok"})

@app.route("/notifications/delete", methods=["POST"])
def notifications_delete():
    global _notifications
    data = request.get_json() or {}
    nid  = data.get("id")
    if nid == "all":
        _notifications = []
    else:
        _notifications = [n for n in _notifications if n.get("id") != nid]
    threading.Thread(target=_save_notifs_to_db, daemon=True).start()
    return jsonify({"status": "ok", "remaining": len(_notifications)})

@app.route("/trade-history", methods=["GET"])
def trade_history_route():
    # FIX v30: mode default "paper" tha — real mode trades miss hoti thi
    # Ab: agar mode field hi nahi hai toh TRADE_MODE se match karo (both ways safe)
    hist   = [t for t in auto_trade_stats.get("trade_history", [])
              if isinstance(t, dict) and (t.get("mode") or TRADE_MODE) == TRADE_MODE]
    filt   = request.args.get("filter", "all")
    search = request.args.get("q", "").lower()
    from datetime import datetime as _dt
    now = _dt.utcnow()
    filtered = []
    for t in reversed(hist):
        # FIX v33: sold_at missing hone pe skip mat karo — bought_at fallback use karo
        sold_str = t.get("sold_at") or t.get("bought_at", "")
        if not sold_str and not t.get("result"): continue
        if filt == "win"  and t.get("result") != "win":  continue
        if filt == "loss" and t.get("result") not in ("loss", "sell_failed"): continue
        if sold_str and filt in ("today","week","month"):
            try:
                # FIX v33: robust parse — Z aur microseconds handle karo
                _s = sold_str[:19].replace("Z","")
                sold_dt = _dt.fromisoformat(_s)
                if filt == "today" and (now-sold_dt).days > 0: continue
                if filt == "week"  and (now-sold_dt).days > 7: continue
                if filt == "month" and (now-sold_dt).days > 30: continue
            except: pass
        if search and search not in t.get("token","").lower() and search not in t.get("address","").lower(): continue
        # FIX v33: sold_at missing hone pe bought_at se fill karo
        if not t.get("sold_at"):
            t = dict(t)
            t["sold_at"] = sold_str
        filtered.append(t)
    wins   = [x for x in filtered if x.get("result") == "win"]
    losses = [x for x in filtered if x.get("result") == "loss"]
    best   = max(filtered, key=lambda x: x.get("pnl_pct", 0), default={})
    worst  = min(filtered, key=lambda x: x.get("pnl_pct", 0), default={})
    return jsonify({
        "history":       filtered,
        "total":         len(filtered),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins)/max(len(filtered),1)*100, 1),
        "total_pnl_bnb": round(sum(x.get("pnl_bnb",0) for x in filtered), 4),
        "best_trade":    best,
        "worst_trade":   worst
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
            "total_cycles":     brain["total_learning_cycles"]
}
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
    _all_hist = auto_trade_stats.get("trade_history", [])
    # Sirf current mode ki trades
    # FIX v30: mode default "paper" tha — real mode pe count zero aata tha
    _mode_hist = [t for t in _all_hist if (t.get("mode") or TRADE_MODE) == TRADE_MODE]
    _pos_pnl  = {}
    for _t in _mode_hist:
        _key = _t.get("address", "") + "|" + _t.get("bought_at", "")[:16]
        _pos_pnl[_key] = _pos_pnl.get(_key, 0) + float(_t.get("pnl_bnb", 0) or 0)
    if _pos_pnl:
        wins   = sum(1 for v in _pos_pnl.values() if v > 0)
        losses = sum(1 for v in _pos_pnl.values() if v <= 0)
    else:
        # Real mode mein 0 — paper counter use nahi karna
        wins   = 0
        losses = 0
    trade_count = wins + losses
    win_rate    = round(wins / trade_count * 100, 1) if trade_count > 0 else 0.0


    # FIX: bnb_price 0 hai toh 300 fallback use karo — UI blank nahi rahegi
    bnb_price   = market_cache.get("bnb_price", 0) or 300  # BUG FIX or 300
    paper_bal   = float(sess.get("paper_balance") or 5.0)

    # Build open_trades array for UI — sirf current mode ki positions
    open_trades = []
    for addr, pos in list(auto_trade_stats.get("running_positions", {}).items()):
        if pos.get("mode", TRADE_MODE) != TRADE_MODE:
            continue
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

    # ── GMGN STYLE PNL ──
    # Total PNL = Realized (closed trades) + Unrealized (open positions)
    # % = Total PNL BNB / Total Invested BNB × 100

    _hist_all = auto_trade_stats.get("trade_history", [])
    # Sirf current mode ki trades use karo PNL ke liye
    # FIX v30: mode default bug — real mode PNL zero aata tha
    _hist = [t for t in _hist_all if (t.get("mode") or TRADE_MODE) == TRADE_MODE]

    # Realized — closed trades
    _realized_pnl_bnb  = sum(float(t.get("pnl_bnb", 0) or 0) for t in _hist)
    _realized_invested = sum(float(t.get("size_bnb", AUTO_BUY_SIZE_BNB) or AUTO_BUY_SIZE_BNB) for t in _hist)

    # Unrealized — open positions
    _unrealized_pnl_bnb  = sum(float(p.get("pnl_bnb", 0) or 0) for p in open_trades)
    _unrealized_invested = sum(float(p.get("orig_size_bnb", p.get("size_bnb", AUTO_BUY_SIZE_BNB)) or AUTO_BUY_SIZE_BNB) for p in open_trades)

    _total_pnl_bnb   = _realized_pnl_bnb + _unrealized_pnl_bnb
    _total_invested  = _realized_invested + _unrealized_invested

    total_pnl = round((_total_pnl_bnb / _total_invested * 100), 2) if _total_invested > 0 else 0.0

    return jsonify({
        "paper_balance":        paper_bal,
        "trade_count":          trade_count,
        "wins":                 wins,
        "losses":               losses,
        "win_rate":             win_rate,
        "total_pnl_pct":        total_pnl,
        "total_pnl_bnb":        round(_total_pnl_bnb, 6),
        "realized_pnl_bnb":     round(_realized_pnl_bnb, 6),
        "unrealized_pnl_bnb":   round(_unrealized_pnl_bnb, 6),
        "total_invested_bnb":   round(_total_invested, 6),
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
        "trade_history":   [],  # REMOVED: use /trade-history endpoint instead (was causing huge 2-5MB response)
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
        # Intelligence stats
        "qualified_whales":   sum(1 for d in _smart_wallets.values() if d.get("qualified")),
        "total_wallets":      len(_smart_wallets),
        "rug_dna_patterns":   len(_rug_dna),
        "tokens_blacklisted": sum(1 for v in _token_blacklist.values() if time.time() - v["ts"] < _TOKEN_BL_TTL),
        "devs_blacklisted":   len(_dev_blacklist),
        "brain_best":         len(brain["trading"].get("best_patterns", [])),
        "brain_avoid":        len(brain["trading"].get("avoid_patterns", [])),
        "wss_status":         market_cache.get("wss_status", "unknown"),
        "brain_loaded":       _brain_loaded_from_db or brain.get("total_learning_cycles", 0) > 0 or len(_smart_wallets) > 0 or len(_rug_dna) > 0
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
        "daily_loss": 0, "auto_buys": 0, "auto_sells": 0
})
@app.route("/sys-stats")
def sys_stats():
    """Live RAM usage — paper vs real mode breakdown"""
    try:
        import psutil, os, sys
        proc = psutil.Process(os.getpid())
        mem  = proc.memory_info()
        rss_mb  = round(mem.rss  / 1024 / 1024, 1)  # actual RAM used
        vms_mb  = round(mem.vms  / 1024 / 1024, 1)  # virtual memory

        # Render plan auto-detect — sys_mem.total se actual container limit milti hai
        sys_mem   = psutil.virtual_memory()
        sys_total = round(sys_mem.total / 1024 / 1024, 1)
        # Render plans: Free=512MB, Starter=512MB, Standard=2048MB, Pro=4096MB
        if   sys_total <= 600:   plan_limit = 512.0;  plan_name = "Free/Starter"
        elif sys_total <= 1100:  plan_limit = 1024.0; plan_name = "Basic"
        elif sys_total <= 2200:  plan_limit = 2048.0; plan_name = "Standard"
        elif sys_total <= 4200:  plan_limit = 4096.0; plan_name = "Pro"
        else:                    plan_limit = sys_total; plan_name = "Custom"
        avail_mb = round(max(0, plan_limit - rss_mb), 1)
        used_pct = round((rss_mb / plan_limit) * 100, 1)
        total_mb = plan_limit

        # Estimate paper vs real breakdown
        # Paper: running_positions, trade_history, brain, smart_wallets
        import sys as _sys
        pos_count   = sum(1 for p in auto_trade_stats.get("running_positions", {}).values() if p.get("mode") == "real")
        hist_count  = len(auto_trade_stats.get("trade_history", []))
        brain_pats  = len(brain["trading"].get("best_patterns",[])) + len(brain["trading"].get("avoid_patterns",[]))
        whale_count = len(_smart_wallets)
        rug_count   = len(_rug_dna)
        disc_count  = len(discovered_addresses)

        # Rough estimates (KB)
        paper_kb = round((hist_count * 0.5) + (brain_pats * 0.2) + (whale_count * 0.3) + (rug_count * 0.15) + (disc_count * 0.1), 1)
        real_kb  = round(pos_count * 2.0, 1)  # active positions heavier
        paper_mb = round(paper_kb / 1024, 2)
        real_mb  = round(real_kb  / 1024, 2)

        return jsonify({
            "rss_mb":    rss_mb,
            "vms_mb":    vms_mb,
            "total_mb":  total_mb,
            "avail_mb":  avail_mb,
            "used_pct":  used_pct,
            "paper_mb":  paper_mb,
            "real_mb":   real_mb,
            "paper_items": {
                "open_positions": sum(1 for p in auto_trade_stats.get("running_positions", {}).values() if p.get("mode") != "real"),
                "trade_history": hist_count,
                "brain_patterns": brain_pats,
                "whale_wallets": whale_count,
                "rug_dna": rug_count,
                "discovered": disc_count
},
            "real_items": {
                "open_positions": pos_count
},
            "trade_mode": TRADE_MODE,
            "plan_name":  plan_name,
            "total_mb":   total_mb
})
    except Exception as e:
        return jsonify({"error": str(e), "rss_mb": 0, "used_pct": 0})

@app.route("/rug-dna")
def rug_dna_route():
    """Latest 100 rug DNA fingerprints"""
    try:
        data = list(reversed(_rug_dna[-10000:]))  # latest first
        result = []
        for d in data:
            ts = d.get("ts", 0)
            try:
                ist_time = datetime.fromtimestamp(ts, tz=_IST).strftime("%d %b %I:%M %p") if ts else "—"
            except:
                ist_time = "—"
            result.append({
                "token":    d.get("token","")[:6] + "..." + d.get("token","")[-4:] if d.get("token") else "—",
                "creator":  d.get("creator","")[:6] + "..." + d.get("creator","")[-4:] if d.get("creator") else "—",
                "buy_tax":  d.get("buy_tax", 0),
                "sell_tax": d.get("sell_tax", 0),
                "liq_band": d.get("liq_band", "—"),
                "reason":   d.get("reason", "SL/Rug"),
                "pnl_pct":  d.get("pnl_pct", 0),
                "ts":       ist_time
})
        return jsonify({"dna": result, "total": len(_rug_dna)})
    except Exception as e:
        return jsonify({"dna": [], "total": 0, "error": str(e)})

@app.route("/brain-insights")
def brain_insights():
    """Return bot learning insights"""
    try:
        _ensure_brain_structure()
        history  = auto_trade_stats.get("trade_history", [])
        wins     = [t for t in history if t.get("pnl_pct", 0) > 0]
        losses   = [t for t in history if t.get("pnl_pct", 0) <= 0]
        total    = len(history)
        wr       = round(len(wins)/total*100,1) if total > 0 else 0
        insights = brain["trading"].get("market_insights", [])
        notes    = brain["trading"].get("strategy_notes",  [])
        best     = brain["trading"].get("best_patterns",   [])
        avoid    = brain["trading"].get("avoid_patterns",  [])
        # Top wins/losses
        top_wins   = sorted(wins,   key=lambda x: x.get("pnl_pct",0), reverse=True)[:50]
        top_losses = sorted(losses, key=lambda x: x.get("pnl_pct",0))[:50]
        return jsonify({
            "total_trades":   total,
            "win_rate":       wr,
            "wins":           len(wins),
            "losses":         len(losses),
            "cycles":         brain.get("total_learning_cycles", 0),
            "best_count":     len(best),
            "avoid_count":    len(avoid),
            "latest_note":    notes[-1].get("note","") if notes else "Not enough data yet",
            "top_wins":       [{"token": t.get("token",""), "pnl": t.get("pnl_pct",0), "hold": t.get("hold_minutes",0)} for t in top_wins],
            "top_losses":     [{"token": t.get("token",""), "pnl": t.get("pnl_pct",0), "hold": t.get("hold_minutes",0)} for t in top_losses],
            "insights":       insights[-20:]
})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/whale-detail")
def whale_detail():
    """Return top qualified whale wallets with stats"""
    try:
        with _smart_wallets_lock:
            wallets = dict(_smart_wallets)
        result = []
        for addr, d in wallets.items():
            wins   = d.get("wins", 0)
            losses = d.get("losses", 0)
            total  = wins + losses
            wr     = round(wins / total * 100, 1) if total > 0 else 0
            result.append({
                "address":    addr,
                "short":      addr[:6] + "..." + addr[-4:],
                "wins":       wins,
                "losses":     losses,
                "total":      total,
                "win_rate":   wr,
                "total_pnl":  round(d.get("total_pnl", 0), 4),
                "qualified":  d.get("qualified", False),
                "first_seen": d.get("first_seen", "")[:10] if d.get("first_seen") else "—",
                "last_seen":  _to_ist(d.get("last_seen", "")) if d.get("last_seen") else "—"
})
        # Sort by wins desc
        result.sort(key=lambda x: (x["qualified"], x["wins"]), reverse=True)
        return jsonify({"wallets": result[:50], "total": len(result)})
    except Exception as e:
        return jsonify({"wallets": [], "total": 0, "error": str(e)})

@app.route("/toggle-auto", methods=["POST"])
def toggle_auto():
    global AUTO_TRADE_ENABLED
    AUTO_TRADE_ENABLED = not AUTO_TRADE_ENABLED
    status = "STARTED" if AUTO_TRADE_ENABLED else "PAUSED"
    print(f"🤖 Auto Trade toggled: {status}")
    return jsonify({"enabled": AUTO_TRADE_ENABLED, "status": status})


@app.route('/toggle-fm', methods=['POST'])
def toggle_fm_sniper():
    global FM_SNIPER_ENABLED
    FM_SNIPER_ENABLED = not FM_SNIPER_ENABLED
    if not FM_SNIPER_ENABLED:
        _fm_stop_event.set()
        print("🛑 FM Sniper STOPPED — WSS closing")
    else:
        _fm_stop_event.clear()
        # FM restart karo
        threading.Thread(target=poll_four_meme_v2, daemon=True).start()
        print("✅ FM Sniper STARTED — WSS reconnecting")
    threading.Thread(target=_save_sniper_state, daemon=True).start()
    return jsonify({"enabled": FM_SNIPER_ENABLED})

@app.route('/sniper-status', methods=['GET'])
def sniper_status():
    return jsonify({"pc": False, "fm": FM_SNIPER_ENABLED})

@app.route('/set-trade-mode', methods=['POST'])
def set_trade_mode():
    global TRADE_MODE, REAL_WALLET
    data = request.get_json() or {}
    mode   = data.get("mode", "paper")
    wallet = data.get("wallet", "")
    if mode not in ("paper", "real"):
        return jsonify({"status": "error", "message": "Invalid mode"}), 400
    prev_mode = TRADE_MODE
    TRADE_MODE   = mode
    REAL_WALLET  = wallet
    print(f"🔄 Trade mode switched to: {mode.upper()} | wallet={wallet[:12] if wallet else 'none'}")

    # Paper → Real switch: saare paper open positions silently remove karo
    if prev_mode == "paper" and mode == "real":
        def _close_all_paper():
            try:
                # 1. Paper positions remove
                open_addrs = list(auto_trade_stats["running_positions"].keys())
                if open_addrs:
                    print(f"🔴 Removing {len(open_addrs)} paper positions (no history save)...")
                    for addr in open_addrs:
                        try:
                            auto_trade_stats["running_positions"].pop(addr, None)
                            remove_position_from_monitor(addr)
                        except Exception as _ce:
                            print(f"⚠️ Remove paper position error {addr[:10]}: {_ce}")
                    _persist_positions()

                # 2. Daily loss reset — paper ka loss real mein carry nahi hoga
                sess = get_or_create_session(AUTO_SESSION_ID)
                sess["daily_loss"]      = 0.0
                sess["daily_loss_date"] = datetime.utcnow().strftime("%Y-%m-%d")

                # 3. Stats reset
                auto_trade_stats["total_auto_buys"] = 0
                auto_trade_stats["total_auto_sells"] = 0

                print(f"✅ All paper state cleared — Real mode ready!")
            except Exception as e:
                print(f"⚠️ _close_all_paper error: {e}")
        threading.Thread(target=_close_all_paper, daemon=True).start()

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
        print(f"💰 Auto buy amount changed: {AUTO_BUY_SIZE_BNB} BNB | supabase={'OK' if supabase else 'NOT CONNECTED'}")
        _persist_settings()  # Sync call — background thread pe race condition tha
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

@app.route("/get-fm-filters", methods=["GET"])
def get_fm_filters():
    # FIX v33: Return updated filters with momentum_min
    return jsonify(_fm_filters)

@app.route("/set-fm-filters", methods=["POST"])
def set_fm_filters():
    global _fm_filters
    d = request.get_json(silent=True) or {}
    # FIX v34: Support new momentum_min, momentum_window_sec, momentum_interval_sec
    for k, v in d.items():
        if k in _fm_filters:
            _fm_filters[k] = v
    # Ensure new keys have defaults if not set
    if "momentum_min" not in _fm_filters:
        _fm_filters["momentum_min"] = 25
        _fm_filters["momentum_min_enabled"] = True
    if "momentum_window_sec" not in _fm_filters:
        _fm_filters["momentum_window_sec"] = 90
    if "momentum_interval_sec" not in _fm_filters:
        _fm_filters["momentum_interval_sec"] = 0.1
    threading.Thread(target=_save_brain_to_db, daemon=True).start()
    return jsonify({"ok": True, "filters": _fm_filters})

@app.route("/get-settings", methods=["GET"])
def get_settings():
    return jsonify({
        "buy_amount":    AUTO_BUY_SIZE_BNB,
        "max_positions": AUTO_MAX_POSITIONS,
        "auto_enabled":  AUTO_TRADE_ENABLED
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

@app.route("/force-close-position", methods=["POST"])
def force_close_position():
    """
    Force close — bina real sell ke state clean karo.
    Real mode mein wallet gas nahi hai tab use karo.
    Position trade history mein loss ke saath save hogi.
    """
    try:
        data = request.get_json(silent=True) or {}
        addr = data.get("address", "").strip()

        # Agar address nahi diya toh worst PnL wali lo
        positions = auto_trade_stats.get("running_positions", {})
        if not positions:
            return jsonify({"status": "empty", "message": "Koi open position nahi hai"})

        if addr and addr in positions:
            target_addr = addr
        else:
            # Worst PnL wali position
            def _pnl(item):
                a, p = item
                e = p.get("entry", 0)
                c = monitored_positions.get(a, {}).get("current", e)
                return ((c - e) / e * 100) if e > 0 else 0
            target_addr, _ = sorted(positions.items(), key=_pnl)[0]

        pos   = positions.get(target_addr, {})
        token = pos.get("token", target_addr[:10])
        entry = pos.get("entry", 0)
        size  = pos.get("size_bnb", AUTO_BUY_SIZE_BNB)

        with monitor_lock:
            mon = monitored_positions.get(target_addr, {})
        current = mon.get("current", 0)

        # PnL calculate karo
        if entry > 0 and current > 0:
            pnl_pct = round(((current - entry) / entry) * 100, 2)
        else:
            pnl_pct = -100.0
        pnl_bnb = round(size * (pnl_pct / 100.0), 6)

        bought_at_str = pos.get("bought_at", "")

        # Trade history mein save karo
        if not isinstance(auto_trade_stats.get("trade_history"), list):
            auto_trade_stats["trade_history"] = []
        auto_trade_stats["trade_history"].append({
            "token":        token,
            "address":      target_addr,
            "entry":        entry,
            "exit":         current,
            "pnl_pct":      pnl_pct,
            "pnl_bnb":      pnl_bnb,
            "size_bnb":     size,
            "bought_at":    bought_at_str,
            "sold_at":      datetime.utcnow().isoformat(),
            "result":       "win" if pnl_pct > 0 else "loss",
            "exit_reason":  "Force Close (no gas)",
            "mode":         TRADE_MODE,
            "tx_hash":      "force_closed"
})

        # Stats update
        if pnl_pct > 0:
            auto_trade_stats["wins"]   = auto_trade_stats.get("wins", 0) + 1
        else:
            auto_trade_stats["losses"] = auto_trade_stats.get("losses", 0) + 1
        auto_trade_stats["total_auto_sells"] = auto_trade_stats.get("total_auto_sells", 0) + 1

        # State clean karo
        auto_trade_stats["running_positions"].pop(target_addr, None)
        remove_position_from_monitor(target_addr)

        # DB save
        threading.Thread(target=_save_session_to_db,    args=(AUTO_SESSION_ID,), daemon=True).start()
        threading.Thread(target=_save_trade_history_to_db,                       daemon=True).start()
        _persist_positions()

        print(f"🔴 FORCE CLOSED: {token} ({target_addr[:10]}) PnL:{pnl_pct:+.1f}%")
        _log("sell", token, f"🔴 FORCE CLOSE (no gas) PnL:{pnl_pct:+.1f}%", target_addr)

        return jsonify({
            "status":  "force_closed",
            "address": target_addr,
            "token":   token,
            "pnl_pct": pnl_pct,
            "pnl_bnb": pnl_bnb,
            "message": f"{token} force closed — PnL: {pnl_pct:+.1f}%"
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:100]}), 500


@app.route("/scanner-stats")
def scanner_stats():
    """Scanner performance stats — per min/hour/day"""
    import time as _t
    now   = _t.time()
    hist  = _scanner_stats.get("history", [])

    # Per minute (last 5 min avg)
    last5  = [h for h in hist if now - h["ts"] <= 300]
    last60 = [h for h in hist if now - h["ts"] <= 3600]
    last1d = hist  # all history = up to 24h

    def _sum(arr, key): return sum(h.get(key, 0) for h in arr)
    def _avg(arr, key):
        v = _sum(arr, key)
        return round(v / max(len(arr), 1), 1)

    # Speed avg
    pc_spd = round(_scanner_stats["pc_speed_total"] / max(_scanner_stats["pc_speed_count"], 1), 2)
    fm_spd = round(_scanner_stats["fm_speed_total"] / max(_scanner_stats["fm_speed_count"], 1), 2)

    # Queue sizes
    try:
        pc_q = _discovery_queue.qsize()
        fm_q = _fm_queue.qsize()
    except Exception:
        pc_q = fm_q = 0

    return jsonify({
        "pc": {
            "discovered":     _scanner_stats["pc_discovered"],
            "prefilter_pass": _scanner_stats["pc_prefilter_pass"],
            "prefilter_fail": _scanner_stats["pc_prefilter_fail"],
            "checklist_pass": _scanner_stats["pc_checklist_pass"],
            "checklist_fail": _scanner_stats["pc_checklist_fail"],
            "bought":         _scanner_stats["pc_bought"],
            "queue":          pc_q,
            "avg_speed_s":    pc_spd
},
        "fm": {
            "discovered": _scanner_stats["fm_discovered"],
            "bought":     _scanner_stats["fm_bought"],
            "queue":      fm_q,
            "avg_speed_s": fm_spd
},
        "per_min": {
            "pc_disc": _avg(last5, "pc_disc"),
            "fm_disc": _avg(last5, "fm_disc"),
            "pc_buy":  _avg(last5, "pc_buy"),
            "fm_buy":  _avg(last5, "fm_buy")
},
        "per_hour": {
            "pc_disc": _sum(last60, "pc_disc"),
            "fm_disc": _sum(last60, "fm_disc"),
            "pc_buy":  _sum(last60, "pc_buy"),
            "fm_buy":  _sum(last60, "fm_buy")
},
        "per_day": {
            "pc_disc": _sum(last1d, "pc_disc"),
            "fm_disc": _sum(last1d, "fm_disc"),
            "pc_buy":  _sum(last1d, "pc_buy"),
            "fm_buy":  _sum(last1d, "fm_buy")
},
        "history_points": len(hist),
        "pc_enabled": False,
        "fm_enabled": FM_SNIPER_ENABLED,
        "rejections": {
            "low_liq":   _scanner_stats["rej_low_liq"],
            "high_liq":  _scanner_stats["rej_high_liq"],
            "honeypot":  _scanner_stats["rej_honeypot"],
            "danger":    _scanner_stats["rej_danger"],
            "too_old":   _scanner_stats["rej_too_old"],
            "blacklist": _scanner_stats["rej_blacklist"]
}
})



@app.route("/auto-status", methods=["GET"])
def auto_status():
    try:
        return jsonify({
            "ready":      True,
            "mode":       TRADE_MODE,
            "enabled":    AUTO_TRADE_ENABLED,
            "bnb_price":  market_cache.get("bnb_price", 0),
            "fear_greed": market_cache.get("fear_greed", 50),
            "positions":  len(auto_trade_stats.get("running_positions", {}))
})
    except Exception as e:
        return jsonify({"ready": False, "error": str(e)[:60]})



def _enrich_events(events):
    """FM/PC events ko trade history se enrich karo — exit_price, pnl_pct, token_name, ATH"""
    # Trade history index by address for fast lookup
    _hist = auto_trade_stats.get("trade_history", [])
    _hist_idx = {}
    for t in _hist:
        addr = t.get("address", "").lower()
        if addr:
            _hist_idx[addr] = t

    for ev in events:
        tok = ev.get("token_address", "")
        if not tok:
            continue
        tok_lower = tok.lower()

        # Token name from running positions or trade history
        pos = auto_trade_stats.get("running_positions", {}).get(tok, {})
        hist_t = _hist_idx.get(tok_lower, {})
        ev["token_name"] = (
            pos.get("token") or
            hist_t.get("token") or
            tok[:8]
        )

        if ev.get("result") != "BUY":
            continue

        # Open position — current price
        if pos:
            cur   = pos.get("current", 0)
            entry = pos.get("entry", 0)
            ev["current_price"]   = cur
            ev["current_pnl_pct"] = round((cur - entry) / entry * 100, 2) if entry > 0 else 0
            ev["status"] = "OPEN"
        # Closed position — exit from trade history
        elif hist_t:
            ev["exit_price"]   = hist_t.get("exit_price", 0)
            ev["pnl_pct"]      = hist_t.get("pnl_pct", 0)
            ev["pnl_bnb"]      = hist_t.get("pnl_bnb", 0)
            ev["exit_reason"]  = hist_t.get("exit_reason", "")
            ev["sold_at"]      = hist_t.get("sold_at", "")
            ev["hold_minutes"] = hist_t.get("hold_minutes", 0)
            ev["result_trade"] = hist_t.get("result", "")
            ev["status"]       = "CLOSED"
            # current_pnl = closed pnl
            ev["current_pnl_pct"] = hist_t.get("pnl_pct", 0)
        else:
            ev["status"] = "UNKNOWN"

        # ATH — open position se monitored, closed position se trade history
        snipe = ev.get("snipe_price", 0)
        if ev.get("status") == "CLOSED" and hist_t:
            # Trade history mein save hua ATH use karo
            ev["ath_price"] = hist_t.get("ath_price", 0)
            ev["ath_pct"]   = hist_t.get("ath_pct", 0)
            # exit_price + exit_reason bhi fix karo
            if not ev.get("exit_price"):
                ev["exit_price"] = hist_t.get("exit_price", hist_t.get("exit", 0))
            if not ev.get("exit_reason"):
                ev["exit_reason"] = hist_t.get("exit_reason", hist_t.get("reason", ""))
        else:
            # Open position — monitored_positions se
            mon = monitored_positions.get(tok, {})
            ath = mon.get("high", 0)
            ev["ath_price"] = ath
            ev["ath_pct"]   = round((ath - snipe) / snipe * 100, 1) if ath and snipe and snipe > 0 else 0

    return events

@app.route("/fm-events")
def fm_events_api():
    """FM event log — Supabase se all events + current price + ATH"""
    try:
        if not supabase:
            return jsonify({"events": [], "error": "supabase not connected"})
        res = supabase.table("fm_events").select("*").order("detected_at", desc=True).limit(1000).execute()
        events = res.data or []
        events = _enrich_events(events)
        return jsonify({"events": events})
    except Exception as e:
        return jsonify({"events": [], "error": str(e)[:60]})


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
        "learning_cycles": brain.get("total_learning_cycles", 0)
})


@app.route("/moralis-test")
def moralis_test():
    """Test Moralis connection"""
    try:
        if not MORALIS_API_KEY:
            return jsonify({"error": "No key"})
        WALLET = "0xbf004bff64725914ee36d03b87d6965b0ced4903"
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/{WALLET}/erc20/transfers",
            params={"chain": "bsc", "limit": 5},
            headers={"X-API-Key": MORALIS_API_KEY},
            timeout=15
        )
        return jsonify({
            "status": r.status_code,
            "key_prefix": MORALIS_API_KEY[:20],
            "response_sample": r.json() if r.status_code == 200 else r.text[:200]
        })
    except Exception as e:
        return jsonify({"error": str(e)[:200]})

@app.route("/analyze-wallet/<wallet_address>")
def analyze_wallet(wallet_address):
    """Wallet ka full trade pattern + pool liquidity analyze karo"""
    try:
        if not MORALIS_API_KEY:
            return jsonify({"error": "MORALIS_API_KEY not set"})

        WBNB_ADDR = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
        wallet_lower = wallet_address.lower()
        _headers = {"X-API-Key": MORALIS_API_KEY}

        # Step 1: Last 100 ERC20 token transfers
        r = requests.get(
            f"https://deep-index.moralis.io/api/v2.2/{wallet_address}/erc20/transfers",
            params={"chain": "bsc", "limit": 100, "order": "DESC"},
            headers=_headers, timeout=15
        )
        if r.status_code != 200:
            return jsonify({"error": f"Moralis error: {r.status_code}"})

        txs = r.json().get("result", [])
        txs = [t for t in txs if t.get("token_address","").lower() != WBNB_ADDR.lower()]

        # Step 1b: Native txs — gas fees + BNB spent per tx
        _tx_gas = {}
        try:
            r2 = requests.get(
                f"https://deep-index.moralis.io/api/v2.2/{wallet_address}",
                params={"chain": "bsc", "limit": 100, "order": "DESC"},
                headers=_headers, timeout=15
            )
            if r2.status_code == 200:
                for tx in r2.json().get("result", []):
                    h         = tx.get("hash", "")
                    gas_used  = int(tx.get("receipt_gas_used") or 0)
                    gas_price = int(tx.get("gas_price") or 0)
                    bnb_val   = round(int(tx.get("value") or 0) / 1e18, 6)
                    gas_bnb   = round((gas_used * gas_price) / 1e18, 6)
                    _tx_gas[h] = {"gas_bnb": gas_bnb, "bnb_spent": bnb_val}
        except Exception:
            pass

        # Step 2: Group by token
        tokens = {}
        for t in txs:
            sym  = t.get("token_symbol", "?")
            addr = t.get("token_address", "").lower()
            direction = "BUY" if t.get("to_address","").lower() == wallet_lower else "SELL"
            ts       = t.get("block_timestamp", "")[:19]
            val      = float(t.get("value_decimal") or 0)
            tx_hash  = t.get("transaction_hash", "")
            gas_info = _tx_gas.get(tx_hash, {})
            gas_bnb  = gas_info.get("gas_bnb", 0)
            bnb_spent = gas_info.get("bnb_spent", 0)
            if not addr: continue
            if addr not in tokens:
                tokens[addr] = {"symbol": sym, "address": addr, "buys": [], "sells": []}
            entry = {"ts": ts, "val": val, "gas_bnb": gas_bnb, "bnb_spent": bnb_spent, "tx": tx_hash}
            if direction == "BUY":
                tokens[addr]["buys"].append(entry)
            else:
                tokens[addr]["sells"].append(entry)

        # Step 3: Per token analysis + pool liquidity
        bnb_price = market_cache.get("bnb_price", 0)
        results = []
        for addr, data in tokens.items():
            buys  = data["buys"]
            sells = data["sells"]
            if not buys: continue

            # Sort by time
            buys.sort(key=lambda x: x["ts"])
            sells.sort(key=lambda x: x["ts"])

            first_buy  = buys[0]["ts"]
            first_sell = sells[0]["ts"] if sells else ""
            last_sell  = sells[-1]["ts"] if sells else ""

            # Hold time (first buy to first sell)
            hold_min = 0
            if first_buy and first_sell:
                try:
                    from datetime import datetime as _dt2
                    b = _dt2.fromisoformat(first_buy)
                    s = _dt2.fromisoformat(first_sell)
                    hold_min = round((s - b).total_seconds() / 60, 1)
                except Exception:
                    pass

            # Pool liquidity via getReserves on-chain
            liq_bnb = 0.0
            liq_usd = 0.0
            try:
                _pair = _get_v2_pair(Web3.to_checksum_address(addr))
                if _pair:
                    _pc = w3.eth.contract(
                        address=Web3.to_checksum_address(_pair),
                        abi=PAIR_ABI_PRICE
                    )
                    _res = _pc.functions.getReserves().call()
                    _t0  = _pc.functions.token0().call().lower()
                    liq_bnb = round((_res[0] if _t0 == WBNB_ADDR else _res[1]) / 1e18, 4)
                    liq_usd = round(liq_bnb * bnb_price, 0) if bnb_price else 0
            except Exception:
                pass

            # Gas calculation
            total_gas_bnb   = round(sum(b.get("gas_bnb",0) for b in buys) + sum(s.get("gas_bnb",0) for s in sells), 6)
            total_bnb_spent = round(sum(b.get("bnb_spent",0) for b in buys), 6)

            # TP pattern — sell timing analysis
            tp_pattern = "unknown"
            if sells:
                if len(sells) == 1:
                    tp_pattern = "single_exit"
                elif len(sells) == len(buys):
                    tp_pattern = "scalping"  # same count buys/sells
                elif len(sells) < len(buys):
                    tp_pattern = "partial_sells"  # multiple TPs
                else:
                    tp_pattern = "multi_sell"

                # Hold time buckets
                if hold_min < 1:
                    tp_pattern += "_<1min"
                elif hold_min < 5:
                    tp_pattern += "_1-5min"
                elif hold_min < 15:
                    tp_pattern += "_5-15min"
                elif hold_min < 60:
                    tp_pattern += "_15-60min"
                else:
                    tp_pattern += "_>1hr"

            # Sell intervals (time between each sell)
            sell_intervals = []
            if len(sells) > 1:
                try:
                    from datetime import datetime as _dt3
                    for i in range(1, len(sells)):
                        t1 = _dt3.fromisoformat(sells[i-1]["ts"])
                        t2 = _dt3.fromisoformat(sells[i]["ts"])
                        sell_intervals.append(round(abs((t2-t1).total_seconds()), 1))
                except Exception:
                    pass

            results.append({
                "symbol":         data["symbol"],
                "address":        addr,
                "buy_count":      len(buys),
                "sell_count":     len(sells),
                "first_buy":      first_buy,
                "first_sell":     first_sell,
                "last_sell":      last_sell,
                "hold_min":       hold_min,
                "sold":           len(sells) > 0,
                "fully_sold":     len(sells) > 0 and last_sell != "",
                "liq_bnb_now":    liq_bnb,
                "liq_usd_now":    liq_usd,
                "total_gas_bnb":  total_gas_bnb,
                "bnb_spent":      total_bnb_spent,
                "tp_pattern":     tp_pattern,
                "sell_intervals_sec": sell_intervals
})

        # Sort by first_buy desc (latest first)
        results.sort(key=lambda x: x["first_buy"], reverse=True)

        # Summary
        sold_tokens  = [r for r in results if r["sold"]]
        unsold       = [r for r in results if not r["sold"]]
        avg_hold     = round(sum(r["hold_min"] for r in sold_tokens) / max(len(sold_tokens), 1), 1)
        avg_liq_bnb  = round(sum(r["liq_bnb_now"] for r in results if r["liq_bnb_now"] > 0) / max(1, sum(1 for r in results if r["liq_bnb_now"] > 0)), 2)
        avg_liq_usd  = round(avg_liq_bnb * bnb_price, 0) if bnb_price else 0

        # Liq distribution
        liq_buckets = {"<5BNB": 0, "5-15BNB": 0, "15-30BNB": 0, "30-50BNB": 0, ">50BNB": 0}
        for r in results:
            l = r["liq_bnb_now"]
            if l <= 0: continue
            if l < 5:   liq_buckets["<5BNB"] += 1
            elif l < 15: liq_buckets["5-15BNB"] += 1
            elif l < 30: liq_buckets["15-30BNB"] += 1
            elif l < 50: liq_buckets["30-50BNB"] += 1
            else:        liq_buckets[">50BNB"] += 1

        return jsonify({
            "wallet":           wallet_address,
            "total_tokens":     len(results),
            "sold":             len(sold_tokens),
            "not_sold":         len(unsold),
            "avg_hold_min":     avg_hold,
            "avg_liq_bnb_now":  avg_liq_bnb,
            "avg_liq_usd_now":  avg_liq_usd,
            "liq_distribution": liq_buckets,
            "bnb_price":        bnb_price,
            "trades":           results
})

    except Exception as e:
        return jsonify({"error": str(e)[:200]})

@app.route("/client-config")
def client_config():
    """Frontend ke liye safe config — Moralis key expose karo"""
    return jsonify({
        "moralis_key": MORALIS_API_KEY or "",
        "bsc_wallet":  BSC_WALLET or REAL_WALLET or ""
})

@app.route("/wallet-info")
def wallet_info():
    """Real wallet balance — multiple sources try karo"""
    try:
        addr = BSC_WALLET or REAL_WALLET or ""
        if not addr:
            return jsonify({"wallet": "", "bnb": 0, "usd": 0, "error": "BSC_WALLET not set"})
        bnb_price = market_cache.get("bnb_price", 0)

        # 1. BSCScan API — free, reliable
        if BSC_SCAN_KEY:
            try:
                r = requests.get(BSC_SCAN_API, params={
                    "module": "account", "action": "balance",
                    "address": addr, "tag": "latest", "apikey": BSC_SCAN_KEY
                }, timeout=10)
                if r.status_code == 200 and r.json().get("status") == "1":
                    bnb = float(r.json()["result"]) / 1e18
                    return jsonify({"wallet": addr, "bnb": round(bnb, 6), "usd": round(bnb * bnb_price, 2), "src": "bscscan"})
            except Exception:
                pass

        # 2. BSCScan free (no key) — public endpoint
        try:
            r2 = requests.get(
                f"https://api.bscscan.com/api?module=account&action=balance&address={addr}&tag=latest",
                timeout=8
            )
            if r2.status_code == 200 and r2.json().get("status") == "1":
                bnb = float(r2.json()["result"]) / 1e18
                return jsonify({"wallet": addr, "bnb": round(bnb, 6), "usd": round(bnb * bnb_price, 2), "src": "bscscan_free"})
        except Exception:
            pass

        # 3. Web3 RPC — main w3 instance pehle try karo (already connected hai)
        try:
            bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
            bnb = float(bal) / 1e18
            return jsonify({"wallet": addr, "bnb": round(bnb, 6), "usd": round(bnb * bnb_price, 2), "src": "w3_main"})
        except Exception:
            pass

        _rpcs = [
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-dataseed1.binance.org",
            "https://bsc.drpc.org",
            "https://bsc-rpc.publicnode.com",
            "https://1rpc.io/bnb",
        ]
        for _rpc in _rpcs:
            try:
                w3t = Web3(Web3.HTTPProvider(_rpc, request_kwargs={"timeout": 6}))
                bal = w3t.eth.get_balance(Web3.to_checksum_address(addr))
                bnb = float(bal) / 1e18
                return jsonify({"wallet": addr, "bnb": round(bnb, 6), "usd": round(bnb * bnb_price, 2), "src": "rpc"})
            except Exception:
                continue

        # 4. Moralis — agar key hai
        if MORALIS_API_KEY:
            try:
                _r = requests.get(
                    f"https://deep-index.moralis.io/api/v2.2/{addr}/balance",
                    params={"chain": "bsc"},
                    headers={"X-API-Key": MORALIS_API_KEY},
                    timeout=8
                )
                if _r.status_code == 200:
                    bnb = float(_r.json().get("balance", "0")) / 1e18
                    return jsonify({"wallet": addr, "bnb": round(bnb, 6), "usd": round(bnb * bnb_price, 2), "src": "moralis"})
            except Exception:
                pass

        return jsonify({"wallet": addr, "bnb": 0, "usd": 0, "error": "All balance sources failed"})
    except Exception as e:
        return jsonify({"wallet": "", "bnb": 0, "usd": 0, "error": str(e)[:60]})

@app.route("/pnl-breakdown")
def pnl_breakdown():
    """PNL breakdown — today, week, all time — current mode filtered"""
    try:
        hist = auto_trade_stats.get("trade_history", [])
        # Sirf current mode ke trades
        hist = [t for t in hist if (t.get("mode") or "paper") == TRADE_MODE]
        now  = datetime.utcnow()
        def _calc(trades):
            wins   = sum(1 for t in trades if float(t.get("pnl_bnb", 0) or 0) > 0)
            losses = sum(1 for t in trades if float(t.get("pnl_bnb", 0) or 0) <= 0)
            pnl    = round(sum(float(t.get("pnl_bnb", 0) or 0) for t in trades), 6)
            wr     = round(wins / max(wins+losses, 1) * 100, 1)
            return {"trades": len(trades), "wins": wins, "losses": losses, "pnl_bnb": pnl, "win_rate": wr}
        today = [t for t in hist if t.get("sold_at","")[:10] == now.strftime("%Y-%m-%d")]
        week  = [t for t in hist if t.get("sold_at") and (now - datetime.fromisoformat(t["sold_at"][:19])).days <= 7]
        return jsonify({
            "today":    _calc(today),
            "week":     _calc(week),
            "all_time": _calc(hist),
            "bnb_price": market_cache.get("bnb_price", 0)
        })
    except Exception as e:
        return jsonify({"error": str(e)[:60]})
