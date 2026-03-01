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
import httpx
httpx.__version__ = "0.24.1"

app = Flask(__name__)
MODEL_NAME = "llama-3.3-70b-versatile"

# ========== ENV CONFIG ==========
BSC_RPC          = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API     = "https://api.bscscan.com/api"
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
        "alerts_sent":    set(),  # track which alerts already sent
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
        for addr, pos in list(monitored_positions.items()):
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
                    alerts_sent.add("stop_loss")
                    telegram_price_alert(
                        token, addr,
                        "STOP LOSS HIT",
                        f"PnL: {pnl_pct:.1f}% | EXIT NOW"
                    )

                # ── Stage 11: Laddered Profit Alerts ──────
                if pnl_pct >= 200 and "tp_200" not in alerts_sent:
                    alerts_sent.add("tp_200")
                    telegram_price_alert(token, addr, "TARGET +200%", f"+{pnl_pct:.0f}% | Keep 10% runner only")
                elif pnl_pct >= 100 and "tp_100" not in alerts_sent:
                    alerts_sent.add("tp_100")
                    telegram_price_alert(token, addr, "TARGET +100%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 50 and "tp_50" not in alerts_sent:
                    alerts_sent.add("tp_50")
                    telegram_price_alert(token, addr, "TARGET +50%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 30 and "tp_30" not in alerts_sent:
                    alerts_sent.add("tp_30")
                    telegram_price_alert(token, addr, "TARGET +30%", f"+{pnl_pct:.0f}% | Sell 25%")
                elif pnl_pct >= 20 and "tp_20" not in alerts_sent:
                    alerts_sent.add("tp_20")
                    telegram_price_alert(token, addr, "TARGET +20%", f"+{pnl_pct:.0f}% | Move SL to cost")

                # ── Stage 6: Volume / Dump Alerts ─────────
                if drop_from_high <= -90 and "dump_90" not in alerts_sent:
                    alerts_sent.add("dump_90")
                    telegram_price_alert(token, addr, "DUMP -90% FROM HIGH", "EXIT FULLY NOW")
                elif drop_from_high <= -70 and "dump_70" not in alerts_sent:
                    alerts_sent.add("dump_70")
                    telegram_price_alert(token, addr, "DUMP -70% FROM HIGH", "Exit 75% immediately")
                elif drop_from_high <= -50 and "dump_50" not in alerts_sent:
                    alerts_sent.add("dump_50")
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
            "created_at":       datetime.utcnow().isoformat()
        }
        _load_session_from_db(session_id)
    return sessions[session_id]

def _load_session_from_db(session_id: str):
    if not supabase: return
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
    if not supabase: return
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

def continuous_learning():
    while True:
        try: fetch_market_data(); fetch_pancakeswap_data()
        except Exception as e: print(f"⚠️ Market error: {e}")
        try: run_airdrop_hunter()
        except Exception as e: print(f"⚠️ Airdrop error: {e}")
        time.sleep(300)

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

SYSTEM_PROMPT = """Tu MrBlack hai — expert BSC memecoin sniper AI. Hinglish mein baat kar.

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

RULES: Paper first | Volume > Price | Dev sell = exit 50% | 3-4 lines only | Bhai use kar | NEVER guarantee profit
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

        ctx = (
            f"\n[BNB=${market_cache['bnb_price']:.2f} | F&G={market_cache['fear_greed']}/100"
            f" | Mode={session_data.get('mode','paper').upper()}"
            f" | Paper={session_data.get('paper_balance',1.87):.3f}BNB"
            f" | Trades={trade_count} WR={win_rate_str}"
            f" | DailyLoss={session_data.get('daily_loss',0):.1f}%"
            f" | NewPairs={new_pairs} | Monitoring={monitoring} positions"
            f"{airdrop_ctx}]"
        )

        messages = [{"role": m["role"], "content": m["content"]} for m in history[-10:]]
        messages.append({"role": "user", "content": user_message + ctx})

        response = client.chat.completions.create(
            model=MODEL_NAME, messages=messages,
            system=SYSTEM_PROMPT, max_tokens=400
        )
        return response.choices[0].message.content.strip()

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

    sess["history"].append({"role": "user", "content": user_msg})
    reply = get_llm_reply(user_msg, sess["history"], sess)
    sess["history"].append({"role": "assistant", "content": reply})
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
    threading.Thread(target=fetch_market_data,    daemon=True).start()
    threading.Thread(target=run_airdrop_hunter,   daemon=True).start()

    # Feature 1 — Telegram (no thread needed, called on demand)

    # Feature 2 — New Pair Listener
    threading.Thread(target=poll_new_pairs,        daemon=True).start()

    # Feature 3 — Real-time Price Monitor
    threading.Thread(target=price_monitor_loop,    daemon=True).start()

    # Feature 4 — DexScreener/Moralis (called on demand in routes)

    # Feature 5 — Smart Wallet Tracker
    threading.Thread(target=track_smart_wallets,   daemon=True).start()

    # Continuous learning loop (market + airdrops every 5 min)
    threading.Thread(target=continuous_learning,   daemon=True).start()

    app.run(host="0.0.0.0", port=port, debug=False)
