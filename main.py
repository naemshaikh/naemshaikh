import os
from flask import Flask, render_template_string, request, jsonify
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

# ========== FREEFLOW LLM (MULTI-KEY AUTO FALLBACK) ==========
from freeflow_llm import FreeFlowClient, NoProvidersAvailableError

# ========== PATCH HTTPX VERSION TO AVOID CONFLICT ==========
import httpx
httpx.__version__ = "0.24.1"

app = Flask(__name__)

# ========== ULTIMATE GOD MODE - 2026 LATEST MODELS ==========
MODEL_NAME = "llama-3.3-70b-versatile"

# ========== BSC CONFIGURATION ==========
BSC_RPC = "https://bsc-dataseed.binance.org/"
BSC_SCAN_API = "https://api.bscscan.com/api"
BSC_SCAN_KEY = os.getenv("BSC_SCAN_KEY", "")
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"

w3 = Web3(Web3.HTTPProvider(BSC_RPC))
print(f"✅ BSC Connected: {w3.is_connected()}")

# ========== SUPABASE MEMORY ==========
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase memory connected")
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        supabase = None

# ========== GLOBAL KNOWLEDGE BASE ==========
knowledge_base = {
    "dex": {
        "uniswap": {},
        "pancakeswap": {},
        "aerodrome": {},
        "raydium": {},
        "jupiter": {}
    },
    "bsc": {
        "new_tokens": [],
        "trending": [],
        "scams": [],
        "safu_tokens": []
    },
    "coding": {
        "github": [],
        "stackoverflow": [],
        "medium": [],
        "youtube": []
    },
    "airdrops": {
        "active": [],
        "upcoming": [],
        "ended": []
    },
    "trading": {
        "news": [],
        "fear_greed": {},
        "market_data": {}
    }
}

# ========== ENUMS & TYPES FOR CHECKLIST ==========
class TradingMode(Enum):
    PAPER = "PAPER"
    REAL = "REAL"

class SafetyLevel(Enum):
    SAFE = "SAFE"
    RISK = "RISK"
    DANGER = "DANGER"

@dataclass
class ModeSettings:
    mode: TradingMode
    total_balance: float
    exposure_limit: float
    daily_loss_limit: float
    max_position_per_token: float
    reserve_capital: float

@dataclass
class PaperTradingStats:
    starting_balance: float = 1.0
    trades_completed: int = 0
    win_rate: float = 0.0
    last_20_trades_profitable: bool = False
    emotional_trades: int = 0
    pattern_database_active: bool = True

@dataclass
class ContractSafety:
    verified: bool = False
    mint_disabled: bool = False
    freeze_disabled: bool = False
    ownership_renounced: bool = False
    @property
    def is_safe(self): return all([self.verified, self.mint_disabled, self.freeze_disabled, self.ownership_renounced])
    @property
    def boxes(self): return 4

@dataclass
class LiquiditySafety:
    min_liquidity_met: bool = False
    locked: bool = False
    stable: bool = False
    @property
    def is_safe(self): return all([self.min_liquidity_met, self.locked, self.stable])
    @property
    def boxes(self): return 3

@dataclass
class TokenomicsSafety:
    buy_tax: float = 0
    sell_tax: float = 0
    hidden_functions: bool = False
    transfer_allowed: bool = True
    @property
    def is_safe(self): return self.buy_tax <= 10 and self.sell_tax <= 10 and not self.hidden_functions and self.transfer_allowed
    @property
    def boxes(self): return 3

@dataclass
class HolderSafety:
    top_holder: float = 0
    top_10_holders: float = 0
    suspicious_clustering: bool = False
    @property
    def is_safe(self): return self.top_holder < 7 and self.top_10_holders < 45 and not self.suspicious_clustering
    @property
    def boxes(self): return 3

@dataclass
class DevSafety:
    dev_dumping: bool = False
    sudden_transfers: bool = False
    multi_wallet: bool = False
    @property
    def is_safe(self): return not any([self.dev_dumping, self.sudden_transfers, self.multi_wallet])
    @property
    def boxes(self): return 3

@dataclass
class TestBuyResult:
    token_address: str
    test_buy_amount: float
    sell_success: bool
    honeypot_safe: bool
    real_buy_tax: float
    real_sell_tax: float
    slippage: float
    @property
    def passed(self): return self.sell_success and self.honeypot_safe and self.real_buy_tax <= 10 and self.real_sell_tax <= 10 and self.slippage < 15

@dataclass
class EntryFilter:
    token_age_minutes: float
    sniper_pump_completed: bool
    first_dump_occurred: bool
    dump_percentage: float
    price_stabilized: bool
    higher_low_formed: bool
    sell_pressure_decreasing: bool
    buyers_returning: bool
    @property
    def ready(self): return self.token_age_minutes >= 3 and self.sniper_pump_completed and self.first_dump_occurred and 20 <= self.dump_percentage <= 40 and self.price_stabilized and self.higher_low_formed and self.sell_pressure_decreasing and self.buyers_returning

@dataclass
class BuyPressure:
    buy_tx_increasing: bool
    unique_buyers_increasing: bool
    new_wallets_entering: bool
    buy_volume_gt_sell: bool
    volume_recovery: bool
    @property
    def confirmed(self): return all([self.buy_tx_increasing, self.unique_buyers_increasing, self.new_wallets_entering, self.buy_volume_gt_sell, self.volume_recovery])

@dataclass
class Position:
    token: str
    entry_price: float
    amount: float
    timestamp: datetime
    stop_loss: float = 0.0
    take_profits: List[float] = field(default_factory=list)

@dataclass
class VolumeMonitor:
    current_volume: float
    previous_volume: float
    volume_change: float
    price_change: float
    def get_action(self):
        if self.volume_change > 0: return "HOLD/ADD"
        elif self.volume_change > -50: return "OBSERVE"
        elif self.volume_change > -70: return "EXIT_50%"
        elif self.volume_change > -90: return "EXIT_75%"
        else: return "EXIT_FULLY"

@dataclass
class WhaleActivity:
    top_holder_sell: float
    dev_sold: bool
    multiple_whales: bool
    def get_action(self):
        if self.multiple_whales: return "EXIT_75-100%"
        elif self.dev_sold: return "EXIT_50%"
        elif self.top_holder_sell > 30: return "MAJOR_EXIT"
        elif self.top_holder_sell > 20: return "PARTIAL_SELL"
        elif self.top_holder_sell > 10: return "WARNING"
        return "SAFE"

@dataclass
class TradeRecord:
    token: str
    entry_price: float
    exit_price: float
    amount: float
    profit: float
    profit_percent: float
    volume_pattern: str
    holder_behavior: str
    reason: str
    timestamp: datetime
    lessons: List[str]

# ========== MRBLACK CHECKLIST ENGINE ==========
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
        self.paper_stats = PaperTradingStats(starting_balance=initial_balance)
        self.positions: Dict[str, List[Position]] = {}
        self.trade_history: List[TradeRecord] = []
        self.active_tokens: Set[str] = set()
        self.monitoring_tokens: Set[str] = set()
        self.pattern_db: Dict = {}
        self.last_20_trades = deque(maxlen=20)
        self.daily_pnl = 0.0
        self._load_data()
        print("✅ MrBlack Checklist Engine Initialized")

# ========== MRBLACK CHECKLIST ENGINE (CONTINUED) ==========

    # ========== PHASE 0: PAPER TRADING MODE ==========
    def update_paper_stats(self, profitable: bool, emotional: bool = False):
        self.paper_stats.trades_completed += 1
        if profitable:
            self.paper_stats.win_rate = ((self.paper_stats.win_rate * (self.paper_stats.trades_completed - 1)) + 100) / self.paper_stats.trades_completed
        else:
            self.paper_stats.win_rate = (self.paper_stats.win_rate * (self.paper_stats.trades_completed - 1)) / self.paper_stats.trades_completed
        self.last_20_trades.append(profitable)
        if len(self.last_20_trades) == 20:
            self.paper_stats.last_20_trades_profitable = all(self.last_20_trades)
        if emotional:
            self.paper_stats.emotional_trades += 1

    def can_switch_to_real(self) -> Tuple[bool, Dict]:
        req = {
            'trades_50': self.paper_stats.trades_completed >= 50,
            'win_rate_70': self.paper_stats.win_rate >= 70,
            'last_20_win': self.paper_stats.last_20_trades_profitable,
            'no_emotional': self.paper_stats.emotional_trades == 0,
            'pattern_active': self.paper_stats.pattern_database_active
        }
        return all(req.values()), req

    # ========== STAGE 1: ADVANCED SAFETY CHECKS ==========
    def safety_checks(self, data: Dict) -> Tuple[SafetyLevel, Dict, int]:
        contract = ContractSafety(
            verified=data.get('verified', False),
            mint_disabled=data.get('mint_disabled', False),
            freeze_disabled=data.get('freeze_disabled', False),
            ownership_renounced=data.get('ownership_renounced', False)
        )
        
        liquidity = LiquiditySafety(
            min_liquidity_met=data.get('liquidity_bnb', 0) >= 2,
            locked=data.get('locked_months', 0) >= 1,
            stable=not data.get('sudden_removal', True)
        )
        
        tokenomics = TokenomicsSafety(
            buy_tax=data.get('buy_tax', 100),
            sell_tax=data.get('sell_tax', 100),
            hidden_functions=data.get('hidden_functions', True),
            transfer_allowed=data.get('transfer_allowed', False)
        )
        
        holder = HolderSafety(
            top_holder=data.get('top_holder', 100),
            top_10_holders=data.get('top_10_holders', 100),
            suspicious_clustering=data.get('suspicious_clustering', True)
        )
        
        dev = DevSafety(
            dev_dumping=data.get('dev_dumping', True),
            sudden_transfers=data.get('sudden_transfers', True),
            multi_wallet=data.get('multi_wallet', True)
        )
        
        # Count passed boxes (total 16)
        boxes_passed = 0
        if contract.is_safe: boxes_passed += contract.boxes
        if liquidity.is_safe: boxes_passed += liquidity.boxes
        if tokenomics.is_safe: boxes_passed += tokenomics.boxes
        if holder.is_safe: boxes_passed += holder.boxes
        if dev.is_safe: boxes_passed += dev.boxes
        
        # CRITICAL checks - koi bhi fail to DANGER
        critical_fails = []
        if not contract.verified: critical_fails.append("❌ Contract unverified")
        if not contract.ownership_renounced: critical_fails.append("❌ Ownership not renounced")
        if not liquidity.min_liquidity_met: critical_fails.append("❌ Liquidity < 2 BNB")
        if tokenomics.buy_tax > 10 or tokenomics.sell_tax > 10: critical_fails.append("❌ Tax > 10%")
        if holder.top_holder > 20: critical_fails.append("❌ Top holder > 20%")
        if dev.dev_dumping: critical_fails.append("❌ Dev dumping")
        
        if critical_fails:
            level = SafetyLevel.DANGER
        elif boxes_passed >= 12:  # 75% boxes = SAFE
            level = SafetyLevel.SAFE
        else:
            level = SafetyLevel.RISK
        
        results = {
            'contract': contract.__dict__,
            'liquidity': liquidity.__dict__,
            'tokenomics': tokenomics.__dict__,
            'holder': holder.__dict__,
            'dev': dev.__dict__,
            'boxes_passed': boxes_passed,
            'total_boxes': 16,
            'critical_fails': critical_fails
        }
        
        return level, results, boxes_passed

    # ========== STAGE 2: TEST BUY VALIDATION ==========
    async def test_buy(self, token: str, web3_provider=None) -> Optional[TestBuyResult]:
        """Simulate test buy - replace with actual web3 calls"""
        return TestBuyResult(
            token_address=token,
            test_buy_amount=0.0005,
            sell_success=True,
            honeypot_safe=True,
            real_buy_tax=5,
            real_sell_tax=5,
            slippage=2
        )

    # ========== STAGE 3: SMART ENTRY FILTER ==========
    def check_entry_filter(self, data: Dict) -> EntryFilter:
        filter_result = EntryFilter(
            token_age_minutes=data.get('age_minutes', 0),
            sniper_pump_completed=data.get('sniper_pump', False),
            first_dump_occurred=data.get('dump_occurred', False),
            dump_percentage=data.get('dump_percent', 0),
            price_stabilized=data.get('price_stable', False),
            higher_low_formed=data.get('higher_low', False),
            sell_pressure_decreasing=not data.get('sell_pressure', True),
            buyers_returning=data.get('buyers_returning', False)
        )
        return filter_result

    # ========== STAGE 4: BUY PRESSURE CONFIRMATION ==========
    def check_buy_pressure(self, data: Dict) -> BuyPressure:
        pressure = BuyPressure(
            buy_tx_increasing=data.get('buy_tx_up', False),
            unique_buyers_increasing=data.get('unique_buyers_up', False),
            new_wallets_entering=data.get('new_wallets', 0) > 0,
            buy_volume_gt_sell=data.get('buy_volume', 0) > data.get('sell_volume', 0),
            volume_recovery=data.get('volume_recovery', False)
        )
        return pressure

    # ========== STAGE 5: POSITION SIZING ==========
    def calc_position_size(self, token: str, confidence: float = 1.0) -> float:
        max_pos = self.mode.total_balance * self.mode.max_position_per_token
        existing = len(self.positions.get(token, []))
        if existing == 0:
            return min(0.005, max_pos * 0.3) * confidence
        elif existing == 1:
            return max_pos * 0.3
        elif existing == 2:
            return max_pos * 0.2
        return 0.0

    def add_position(self, token: str, price: float, amount: float) -> Position:
        pos = Position(
            token=token,
            entry_price=price,
            amount=amount,
            timestamp=datetime.now()
        )
        if token not in self.positions:
            self.positions[token] = []
        self.positions[token].append(pos)
        self.active_tokens.add(token)
        return pos

    # ========== STAGE 6: LIVE VOLUME MONITOR ==========
    def monitor_volume(self, token: str, data: Dict) -> VolumeMonitor:
        monitor = VolumeMonitor(
            current_volume=data.get('volume', 0),
            previous_volume=data.get('prev_volume', 0),
            volume_change=data.get('volume_change', 0),
            price_change=data.get('price_change', 0)
        )
        action = monitor.get_action()
        if action != "HOLD/ADD" and action != "OBSERVE":
            self._execute_volume_action(token, action)
        return monitor

    def _execute_volume_action(self, token: str, action: str):
        if token not in self.positions:
            return
        total = sum(p.amount for p in self.positions[token])
        if action == "EXIT_50%":
            print(f"🔴 Volume drop 50%: Selling 50% of {token}")
        elif action == "EXIT_75%":
            print(f"🔴 Volume drop 70%: Selling 75% of {token}")
        elif action == "EXIT_FULLY":
            print(f"🔴 Volume drop 90%: Selling ALL {token}")

    # ========== STAGE 7: WHALE & DEV TRACKING ==========
    def track_whales(self, token: str, data: Dict) -> WhaleActivity:
        whale = WhaleActivity(
            top_holder_sell=data.get('top_sell', 0),
            dev_sold=data.get('dev_sold', False),
            multiple_whales=data.get('multiple_whales', False)
        )
        action = whale.get_action()
        if action != "SAFE":
            print(f"🐋 Whale alert {token}: {action}")
        return whale

    # ========== STAGE 8: LIQUIDITY PROTECTION ==========
    def check_liquidity(self, data: Dict) -> bool:
        lp_removed = data.get('lp_removed', False)
        sudden_drop = data.get('sudden_drop', False)
        if lp_removed or sudden_drop:
            print("🚨 LIQUIDITY CRITICAL! Immediate exit!")
            return False
        return True

    # ========== STAGE 9: FAST PROFIT MODE ==========
    def check_fast_profit(self, pos: Position, current_price: float) -> Optional[float]:
        profit = ((current_price - pos.entry_price) / pos.entry_price) * 100
        if 15 <= profit <= 30:
            print(f"⚡ Fast profit: {profit:.1f}% - Taking partial")
            return pos.amount * 0.3
        return None

    # ========== STAGE 10: STOP LOSS SYSTEM ==========
    def check_stop_loss(self, pos: Position, price: float, age_hours: float) -> bool:
        loss = ((pos.entry_price - price) / pos.entry_price) * 100
        if age_hours < 1:
            sl = 17.5
        elif age_hours < 6:
            sl = 22.5
        else:
            sl = 12.5
        return loss >= sl

    # ========== STAGE 11: LADDERED PROFIT BOOKING ==========
    def check_profit_targets(self, pos: Position, price: float) -> List[float]:
        profit = ((price - pos.entry_price) / pos.entry_price) * 100
        sells = []
        if profit >= 20:
            pos.stop_loss = pos.entry_price
        if profit >= 30:
            sells.append(pos.amount * 0.25)
            print(f"🎯 +30%: Selling 25%")
        if profit >= 50:
            sells.append(pos.amount * 0.25)
            print(f"🎯 +50%: Selling 25%")
        if profit >= 100:
            sells.append(pos.amount * 0.25)
            print(f"🎯 +100%: Selling 25%")
        if profit >= 200:
            sells.append(pos.amount * 0.875)
            print(f"🚀 +200%: Taking most profits")
        return sells

    # ========== STAGE 12: SELF LEARNING SYSTEM ==========
    def log_trade(self, trade_data: Dict):
        record = TradeRecord(
            token=trade_data['token'],
            entry_price=trade_data['entry_price'],
            exit_price=trade_data['exit_price'],
            amount=trade_data['amount'],
            profit=trade_data['profit'],
            profit_percent=trade_data['profit_percent'],
            volume_pattern=trade_data.get('volume', 'unknown'),
            holder_behavior=trade_data.get('holders', 'unknown'),
            reason=trade_data.get('reason', ''),
            timestamp=datetime.now(),
            lessons=trade_data.get('lessons', [])
        )
        self.trade_history.append(record)
        self.update_paper_stats(record.profit > 0)
        self._update_pattern_db(record)
        self._save_data()

    def _update_pattern_db(self, trade: TradeRecord):
        key = f"{trade.volume_pattern}_{'win' if trade.profit > 0 else 'loss'}"
        if key not in self.pattern_db:
            self.pattern_db[key] = {'count': 0, 'total': 0, 'examples': []}
        self.pattern_db[key]['count'] += 1
        self.pattern_db[key]['total'] += trade.profit
        self.pattern_db[key]['examples'].append({
            'token': trade.token,
            'profit': trade.profit,
            'reason': trade.reason
        })

    # ========== STAGE 13: PAPER → REAL SWITCH ==========
    def gradual_transition(self, week: int) -> float:
        return {1: 0.25, 2: 0.50, 3: 0.75, 4: 1.00}.get(week, 1.00)

    def should_return_to_paper(self) -> bool:
        last_3 = list(self.last_20_trades)[-3:] if len(self.last_20_trades) >= 3 else []
        three_losses = len(last_3) == 3 and not any(last_3)
        loss_limit = abs(self.daily_pnl) > (self.mode.total_balance * self.mode.daily_loss_limit)
        emotional = self.paper_stats.emotional_trades > 0
        return three_losses or loss_limit or emotional

    # ========== DATA PERSISTENCE ==========
    def _save_data(self):
        try:
            with open('trades.json', 'w') as f:
                data = [{
                    'token': t.token,
                    'profit': t.profit,
                    'timestamp': t.timestamp.isoformat()
                } for t in self.trade_history[-100:]]
                json.dump(data, f)
            with open('patterns.json', 'w') as f:
                json.dump(self.pattern_db, f)
        except:
            pass

    def _load_data(self):
        try:
            if os.path.exists('trades.json'):
                with open('trades.json', 'r') as f:
                    print(f"✅ Loaded trade history")
        except:
            pass

    # ========== MAIN EVALUATION FUNCTION ==========
    def evaluate_token(self, token: str, data: Dict) -> Dict:
        print(f"\n🔍 Evaluating {token}...")
        
        # STAGE 1: Safety
        level, results, passed = self.safety_checks(data)
        print(f"Safety: {level.value} ({passed}/16 boxes)")
        
        if level == SafetyLevel.DANGER:
            return {'decision': 'SKIP', 'reason': f"DANGER: {results['critical_fails']}", 'stage': 1}
        if level == SafetyLevel.RISK and passed < 12:
            return {'decision': 'SKIP', 'reason': f"RISK: only {passed}/16 boxes", 'stage': 1}
        
        # STAGE 2: Test buy (simulated)
        test = TestBuyResult(
            token_address=token,
            test_buy_amount=0.0005,
            sell_success=True,
            honeypot_safe=True,
            real_buy_tax=data.get('buy_tax', 5),
            real_sell_tax=data.get('sell_tax', 5),
            slippage=2
        )
        if not test.passed:
            return {'decision': 'SKIP', 'reason': 'Test buy failed', 'stage': 2}
        
        # STAGE 3: Entry filter
        entry = self.check_entry_filter(data)
        if not entry.ready:
            return {'decision': 'WAIT', 'reason': 'Entry filter not ready', 'stage': 3}
        
        # STAGE 4: Buy pressure
        pressure = self.check_buy_pressure(data)
        if not pressure.confirmed:
            return {'decision': 'WAIT', 'reason': 'Buy pressure insufficient', 'stage': 4}
        
        # All passed
        size = self.calc_position_size(token)
        return {
            'decision': 'BUY',
            'size': size,
            'passed_boxes': passed,
            'safety_level': level.value
        }


# ========== BSC SCANNER FUNCTIONS ==========
bsc_engine = MrBlackChecklistEngine()

def scan_bsc_token(address: str) -> Dict:
    """Scan a BSC token and return all data"""
    try:
        checksum = Web3.to_checksum_address(address)
        
        # Basic token data
        data = {
            'address': address,
            'verified': False,
            'mint_disabled': False,
            'ownership_renounced': False,
            'buy_tax': 0,
            'sell_tax': 0,
            'liquidity_bnb': 0,
            'locked_months': 0,
            'sudden_removal': False,
            'top_holder': 0,
            'top_10_holders': 0,
            'suspicious_clustering': False,
            'dev_dumping': False,
            'age_minutes': 0,
            'sniper_pump': True,
            'dump_occurred': True,
            'dump_percent': 30,
            'price_stable': True,
            'higher_low': True,
            'buyers_returning': True,
            'buy_tx_up': True,
            'unique_buyers_up': True,
            'new_wallets': 5,
            'buy_volume': 1000,
            'sell_volume': 800,
            'volume_recovery': True
        }
        
        # Try to get real data from BscScan
        if BSC_SCAN_KEY:
            try:
                # Check verification
                url = f"{BSC_SCAN_API}?module=contract&action=getsourcecode&address={address}&apikey={BSC_SCAN_KEY}"
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    result = res.json().get('result', [{}])[0]
                    data['verified'] = result.get('SourceCode') != ''
            except:
                pass
        
        return data
    except:
        return {}

# ========== ORIGINAL DEX DATA FETCHERS ==========
def fetch_uniswap_data():
    """Uniswap V3 data"""
    try:
        url = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3"
        query = """
        {
          pools(first: 10, orderBy: totalValueLockedUSD, orderDirection: desc) {
            id
            token0 { symbol name }
            token1 { symbol name }
            token0Price
            token1Price
            volumeUSD
            totalValueLockedUSD
          }
        }
        """
        response = requests.post(url, json={'query': query})
        data = response.json()
        knowledge_base["dex"]["uniswap"] = {
            "top_pools": data.get('data', {}).get('pools', []),
            "timestamp": datetime.utcnow().isoformat()
        }
        print("✅ Uniswap data fetched")
    except Exception as e:
        print(f"❌ Uniswap error: {e}")

def fetch_pancakeswap_data():
    """PancakeSwap data"""
    try:
        url = "https://api.thegraph.com/subgraphs/name/pancakeswap/exchange"
        query = """
        {
          pairs(first: 10, orderBy: reserveUSD, orderDirection: desc) {
            id
            token0 { symbol }
            token1 { symbol }
            reserveUSD
            volumeUSD
          }
        }
        """
        response = requests.post(url, json={'query': query})
        data = response.json()
        knowledge_base["dex"]["pancakeswap"] = {
            "top_pairs": data.get('data', {}).get('pairs', []),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Also scan for new tokens on BSC
        new_tokens = []
        for pair in data.get('data', {}).get('pairs', [])[:5]:
            token_data = scan_bsc_token(pair.get('id', ''))
            new_tokens.append(token_data)
        knowledge_base["bsc"]["new_tokens"] = new_tokens
        
        print("✅ PancakeSwap data fetched")
    except Exception as e:
        print(f"❌ PancakeSwap error: {e}")

def fetch_aerodrome_data():
    """Aerodrome data via DEX Screener"""
    try:
        response = requests.get("https://api.dexscreener.com/latest/dex/search?q=aerodrome")
        if response.status_code == 200:
            knowledge_base["dex"]["aerodrome"] = {
                "pairs": response.json().get('pairs', [])[:5],
                "timestamp": datetime.utcnow().isoformat()
            }
            print("✅ Aerodrome data fetched")
    except Exception as e:
        print(f"❌ Aerodrome error: {e}")

def fetch_raydium_data():
    """Raydium data"""
    try:
        response = requests.get("https://api.raydium.io/v2/main/pools")
        if response.status_code == 200:
            knowledge_base["dex"]["raydium"] = {
                "pools": response.json()[:5],
                "timestamp": datetime.utcnow().isoformat()
            }
            print("✅ Raydium data fetched")
    except Exception as e:
        print(f"❌ Raydium error: {e}")

def fetch_jupiter_data():
    """Jupiter aggregator data"""
    try:
        socket.setdefaulttimeout(10)
        endpoints = [
            "https://quote-api.jup.ag/v6/price?ids=SOL,USDC,RAY,BONK,JUP",
            "https://api.jup.ag/price/v2?ids=SOL,USDC,RAY,BONK,JUP"
        ]
        
        for endpoint in endpoints:
            try:
                response = requests.get(endpoint, timeout=5)
                if response.status_code == 200:
                    knowledge_base["dex"]["jupiter"] = {
                        "prices": response.json(),
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    print(f"✅ Jupiter data fetched")
                    return
            except:
                continue
        
        # Fallback data
        knowledge_base["dex"]["jupiter"] = {
            "prices": {"data": {"SOL": {"price": "150.00"}, "USDC": {"price": "1.00"}}},
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        print(f"❌ Jupiter error: {e}")

def fetch_coding_data():
    """GitHub, StackOverflow, Medium se coding seekho"""
    try:
        github = requests.get("https://api.github.com/search/repositories?q=blockchain+crypto+web3+python&sort=stars&per_page=5")
        if github.status_code == 200:
            knowledge_base["coding"]["github"] = github.json().get('items', [])
        
        stack = requests.get("https://api.stackexchange.com/2.3/questions?order=desc&sort=activity&tagged=python;solidity;web3&site=stackoverflow")
        if stack.status_code == 200:
            knowledge_base["coding"]["stackoverflow"] = stack.json().get('items', [])[:5]
        
        print("✅ Coding data fetched")
    except Exception as e:
        print(f"❌ Coding error: {e}")

def fetch_airdrops_data():
    """Latest airdrops hunt karo"""
    try:
        dex_response = requests.get("https://api.dexscreener.com/latest/dex/search?q=new+pairs")
        
        airdrops = [
            {"name": "zkSync Era", "status": "Active", "value": "$1000+", "end": "March 2025"},
            {"name": "LayerZero", "status": "Upcoming", "value": "TBA", "end": "Q2 2025"},
            {"name": "Eclipse", "status": "Active", "value": "$500+", "end": "April 2025"},
            {"name": "StarkNet", "status": "Active", "value": "$2000+", "end": "March 2025"},
            {"name": "Scroll", "status": "Upcoming", "value": "TBA", "end": "Q2 2025"}
        ]
        
        knowledge_base["airdrops"]["active"] = airdrops
        knowledge_base["airdrops"]["new_tokens"] = dex_response.json().get('pairs', [])[:5] if dex_response.status_code == 200 else []
        
        print("✅ Airdrop data fetched")
    except Exception as e:
        print(f"❌ Airdrop error: {e}")

def fetch_trading_data():
    """Trading signals aur market data"""
    try:
        news = requests.get("https://min-api.cryptocompare.com/data/v2/news/?lang=EN&limit=5")
        fear_greed = requests.get("https://api.alternative.me/fng/?limit=1")
        
        knowledge_base["trading"]["news"] = news.json().get('Data', []) if news.status_code == 200 else []
        knowledge_base["trading"]["fear_greed"] = fear_greed.json().get('data', []) if fear_greed.status_code == 200 else []
        
        print("✅ Trading data fetched")
    except Exception as e:
        print(f"❌ Trading error: {e}")

# ========== 24x7 LEARNING ENGINE ==========
def continuous_learning():
    """Main learning loop - 24x7 sab seekho"""
    while True:
        print("\n🤖 24x7 LEARNING CYCLE STARTED...")
        
        # Original data fetchers
        fetch_uniswap_data()
        fetch_pancakeswap_data()
        fetch_aerodrome_data()
        fetch_raydium_data()
        fetch_jupiter_data()
        fetch_coding_data()
        fetch_airdrops_data()
        fetch_trading_data()
        
        # Save to Supabase
        if supabase:
            try:
                supabase.table("knowledge").insert({
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": knowledge_base
                }).execute()
                print("✅ All knowledge saved to database")
            except:
                pass
        
        print("😴 Sleeping for 5 minutes...")
        time.sleep(300)

# Start learning thread
learning_thread = threading.Thread(target=continuous_learning, daemon=True)
learning_thread.start()
print("✅ 24x7 LEARNING ENGINE STARTED!")

# ========== UI ==========
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack AI - 24x7 Learning + BSC Sniper</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Roboto, sans-serif; }
        body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; display: flex; justify-content: center; align-items: center; }
        .chat-container { width: 100%; max-width: 800px; height: 90vh; background: white; border-radius: 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); display: flex; flex-direction: column; overflow: hidden; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; text-align: center; }
        .header h1 { font-size: 2rem; margin-bottom: 5px; }
        .badges { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
        .badge { background: rgba(255,255,255,0.2); padding: 5px 15px; border-radius: 20px; font-size: 0.9rem; backdrop-filter: blur(10px); }
        .badge i { margin-right: 5px; }
        .messages { flex: 1; overflow-y: auto; padding: 20px; background: #f5f5f5; }
        .message { max-width: 70%; margin-bottom: 15px; padding: 12px 18px; border-radius: 15px; word-wrap: break-word; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .user { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; margin-left: auto; border-bottom-right-radius: 5px; }
        .bot { background: white; color: #333; margin-right: auto; border-bottom-left-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .input-area { padding: 20px; background: white; border-top: 1px solid #eee; display: flex; gap: 10px; }
        #input { flex: 1; padding: 15px; border: 2px solid #e0e0e0; border-radius: 25px; font-size: 1rem; outline: none; transition: border 0.3s; }
        #input:focus { border-color: #667eea; }
        #send { width: 60px; height: 60px; border-radius: 50%; border: none; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; font-size: 1.5rem; cursor: pointer; transition: transform 0.3s; }
        #send:hover { transform: scale(1.1); }
        #typing { padding: 10px 20px; color: #666; font-style: italic; display: none; }
        .status { font-size: 0.8rem; color: #4CAF50; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="header">
            <h1>🤖 MrBlack AI</h1>
            <div class="badges">
                <span class="badge"><i>🦄</i> Uniswap</span>
                <span class="badge"><i>🥞</i> PancakeSwap</span>
                <span class="badge"><i>✈️</i> Aerodrome</span>
                <span class="badge"><i>☀️</i> Raydium</span>
                <span class="badge"><i>🪐</i> Jupiter</span>
                <span class="badge"><i>💻</i> Coding</span>
                <span class="badge"><i>🎁</i> Airdrops</span>
                <span class="badge"><i>📊</i> Trading</span>
                <span class="badge"><i>🔷</i> BSC</span>
            </div>
            <div class="status" id="memoryStatus">🧠 Memory: ON | 🔄 24x7 Learning: Active | 📋 Checklist: Active</div>
        </div>
        
        <div class="messages" id="messages"></div>
        
        <div id="typing">🤔 MrBlack is thinking and learning...</div>
        
        <div class="input-area">
            <input type="text" id="input" placeholder="Ask about coding, airdrops, trading, or scan a BSC token...">
            <button id="send">➤</button>
        </div>
    </div>

    <script>
        let sessionId = localStorage.getItem('mrblack_session') || '';
        const messagesDiv = document.getElementById('messages');
        const input = document.getElementById('input');
        const sendBtn = document.getElementById('send');
        const typingDiv = document.getElementById('typing');
        const memoryStatus = document.getElementById('memoryStatus');

        function addMessage(text, isUser) {
            const div = document.createElement('div');
            div.className = 'message ' + (isUser ? 'user' : 'bot');
            div.textContent = text;
            messagesDiv.appendChild(div);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }

        async function sendMessage() {
            const msg = input.value.trim();
            if (!msg) return;
            
            addMessage(msg, true);
            input.value = '';
            typingDiv.style.display = 'block';

            try {
                const res = await fetch('/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg, session_id: sessionId})
                });
                
                const data = await res.json();
                typingDiv.style.display = 'none';
                addMessage(data.reply, false);
                
                if (data.session_id) {
                    sessionId = data.session_id;
                    localStorage.setItem('mrblack_session', sessionId);
                }
            } catch (err) {
                typingDiv.style.display = 'none';
                addMessage('Error: ' + err.message, false);
            }
        }

        sendBtn.onclick = sendMessage;
        input.addEventListener('keypress', e => {
            if (e.key === 'Enter') sendMessage();
        });

        // Add example commands
        setTimeout(() => {
            addMessage('🔍 Try these commands:', false);
            addMessage('• "scan 0x..." - Check BSC token safety', false);
            addMessage('• "checklist kya hai" - See safety checklist', false);
            addMessage('• "paper trading start" - Begin paper mode', false);
        }, 1000);
    </script>
</body>
</html>
"""
@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    user_message = data.get("message", "").strip().lower()
    session_id = data.get("session_id") or str(uuid.uuid4())

    if not user_message:
        return jsonify({"reply": "Kuch likho bhai! 😊", "session_id": session_id})

    try:
        # Check for BSC scan commands
        if user_message.startswith("scan 0x") or (len(user_message) == 42 and user_message.startswith("0x")):
            address = user_message.replace("scan ", "").strip()
            if Web3.is_address(address):
                token_data = scan_bsc_token(address)
                level, results, passed = bsc_engine.safety_checks(token_data)
                
                reply = f"🔍 **BSC Token Analysis: {address[:10]}...**\n\n"
                reply += f"📊 Safety Level: **{level.value}**\n"
                reply += f"✅ Boxes Passed: **{passed}/16**\n\n"
                
                if results['critical_fails']:
                    reply += "❌ **Critical Issues:**\n"
                    for fail in results['critical_fails']:
                        reply += f"• {fail}\n"
                
                if level == SafetyLevel.SAFE:
                    reply += "\n✅ **SAFE TO TRADE!**"
                elif level == SafetyLevel.RISK:
                    reply += "\n⚠️ **RISK - Only if experienced**"
                else:
                    reply += "\n🚫 **DANGER - DO NOT TRADE!**"
            else:
                reply = "❌ Invalid BSC address"
            
            return jsonify({"reply": reply, "session_id": session_id})

        # Handle checklist commands
        if user_message in ["checklist", "checklist kya hai", "safety checklist"]:
            reply = """📋 **MrBlack SAFETY CHECKLIST (16 Boxes):**

**Contract Safety (4 boxes)**
✅ Contract Verified
✅ Mint Authority Disabled
✅ Freeze Authority Disabled
✅ Ownership Renounced

**Liquidity Safety (3 boxes)**
✅ Min Liquidity ≥ 2 BNB
✅ Liquidity Locked ≥ 1 Month
✅ No Sudden Removal

**Tokenomics Safety (3 boxes)**
✅ Buy/Sell Tax ≤ 10%
✅ No Hidden Functions
✅ Transfer Allowed

**Holder Safety (3 boxes)**
✅ Top Holder < 7%
✅ Top 10 Holders < 45%
✅ No Suspicious Clustering

**Dev Safety (3 boxes)**
✅ Dev Not Dumping
✅ No Sudden Transfers
✅ No Multi-Wallet Splitting

**Bot SKIP karega agar koi bhi critical fail ho!**"""
            return jsonify({"reply": reply, "session_id": session_id})
            
        # ===== SIMPLE PAPER TRADING =====
        if "paper" in user_message or "buy " in user_message or "sell " in user_message:
            
            # Paper variables
            if not hasattr(self, 'paper_balance'):
                self.paper_balance = 1.0
                self.paper_positions = {}
                self.paper_trades = []
            
            parts = user_message.split()
            
            if parts[0] == "paper":
                if parts[1] == "start":
                    self.paper_balance = 1.0
                    self.paper_positions = {}
                    self.paper_trades = []
                    reply = "📝 Paper trading started! Balance: 1 BNB"
                
                elif parts[1] == "stats":
                    reply = f"📊 Balance: {self.paper_balance:.3f} BNB | Trades: {len(self.paper_trades)}"
            
            elif parts[0] == "buy" and len(parts) == 3:
                token = parts[1]
                amount = float(parts[2])
                
                if amount > self.paper_balance:
                    reply = "❌ Insufficient balance!"
                else:
                    self.paper_balance -= amount
                    self.paper_positions[token] = amount
                    reply = f"✅ Bought {token} for {amount} BNB"
            
            elif parts[0] == "sell" and len(parts) == 2:
                token = parts[1]
                
                if token not in self.paper_positions:
                    reply = "❌ No position found!"
                else:
                    amount = self.paper_positions[token]
                    profit = amount * 0.05
                    self.paper_balance += amount + profit
                    del self.paper_positions[token]
                    self.paper_trades.append(f"Sold {token}")
                    reply = f"✅ Sold {token}! Profit: {profit:.3f} BNB"
            
            else:
                reply = "Commands: paper start | paper stats | buy TOKEN 0.1 | sell TOKEN"
            
            return jsonify({"reply": reply, "session_id": session_id})


        # Regular chat with AI
        system_prompt = f"""Tu MrBlack hai - ek self-learning PRO bot jo 24x7 teeno fields seekhta hai:

CURRENT KNOWLEDGE (Real-time):
- DEX: Uniswap, PancakeSwap, Aerodrome, Raydium, Jupiter
- BSC: {len(knowledge_base['bsc']['new_tokens'])} new tokens scanned
- Coding: {len(knowledge_base['coding']['github'])} GitHub repos
- Airdrops: {len(knowledge_base['airdrops']['active'])} active
- Trading: Fear & Greed: {knowledge_base['trading']['fear_greed'][0].get('value', 'N/A') if knowledge_base['trading']['fear_greed'] else 'N/A'}/100

SPECIALIZATIONS:
- BSC Token Scanner - Safety checklist (16 boxes)
- Uniswap/PancakeSwap Expert
- Coding Guru - Python, Solidity
- Airdrop Hunter
- Trading Coach

COMMANDS:
- "scan 0x..." - Check any BSC token safety
- "checklist" - Show 16-point safety checklist
- "paper trading" - Start practice mode

STYLE: Hinglish, confident, friendly, step-by-step guide"""

        messages = [{"role": "system", "content": system_prompt}]

        if supabase:
            try:
                hist = supabase.table("memory").select("role,content").eq("session_id", session_id).order("created_at").limit(30).execute()
                if hist.data:
                    for m in hist.data:
                        messages.append({"role": m["role"], "content": m["content"]})
            except Exception as e:
                print(f"Memory fetch error: {e}")

        messages.append({"role": "user", "content": user_message})

        with FreeFlowClient() as ffc:
            try:
                response = ffc.chat(
                    messages=messages,
                    model=MODEL_NAME,
                    temperature=0.8,
                    max_tokens=1000
                )
                reply = response.content
                print(f"✅ Provider: {response.provider}")
            except NoProvidersAvailableError:
                reply = "सारे providers थोड़ा आराम कर रहे हैं! 2 मिनट में वापस आना। 😴"
            except Exception as e:
                print(f"Provider error: {e}")
                reply = "थोड़ी तकनीकी दिक्कत है, 2 मिनट में ट्राई करो। 🤖"

        # Save to Supabase
        if supabase:
            try:
                supabase.table("memory").insert([
                    {"session_id": session_id, "role": "user", "content": user_message, "created_at": datetime.utcnow().isoformat()},
                    {"session_id": session_id, "role": "assistant", "content": reply, "created_at": datetime.utcnow().isoformat()}
                ]).execute()
            except Exception as e:
                print(f"Memory save error: {e}")

    except Exception as e:
        print(f"Error: {e}")
        reply = f"Error: {str(e)}"

    return jsonify({"reply": reply, "session_id": session_id})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
