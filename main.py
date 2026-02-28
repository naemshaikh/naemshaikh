import os, time, threading, random, requests
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
from threading import Lock
from collections import defaultdict

app = Flask(__name__)

# ================= CONFIG =================
PAPER_START_BALANCE = 1.0
STOP_LOSS_PERCENT = 15
TAKE_PROFIT_PERCENT = 35
AUTO_TRADING_ENABLED = False
AUTO_INTERVAL = 30
MAX_OPEN_TRADES = 3

# ================= STATE =================
paper_lock = Lock()
paper = {
    "balance": PAPER_START_BALANCE,
    "trades": [],
    "wins": 0,
    "losses": 0,
    "daily_pnl": 0.0,
    "total_pnl": 0.0
}

price_cache = {}

# ================= PRICE (best pair pick) =================
def fetch_price(token):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={token}",
            timeout=5
        ).json()
        pairs = r.get("pairs", [])
        if not pairs:
            return price_cache.get(token, 1.0), 100000, 50000
        # highest liquidity wala pair le
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0)))
        price = float(best.get("priceUsd", 1))
        liq = float(best.get("liquidity", {}).get("usd", 100000))
        vol = float(best.get("volume", {}).get("h24", 50000))
        price_cache[token] = price
        return price, liq, vol
    except:
        return price_cache.get(token, 1.0), 100000, 50000

# ================= POSITION SIZE =================
def position_size(balance, price):
    risk = balance * 0.02
    size = risk / (STOP_LOSS_PERCENT / 100.0)
    return round(size / price, 6)

# ================= TRADE =================
def trade(action, token, amount, entry=None):
    with paper_lock:
        price, _, _ = fetch_price(token)

        if action == "buy":
            if len(paper["trades"]) >= MAX_OPEN_TRADES:
                return "Max trades reached"
            cost = amount * price
            if cost > paper["balance"] + 0.0001:
                return "Balance low"
            paper["balance"] -= cost
            paper["trades"].append({
                "token": token,
                "entry": price,
                "amount": amount,
                "time": datetime.now().isoformat()
            })
            return f"✅ BUY {token[:8]} | {amount:.6f}"

        if action == "sell" and entry is not None:
            pnl = (price - entry) * amount
            paper["balance"] += amount * price
            paper["daily_pnl"] += pnl
            paper["total_pnl"] += pnl

            if pnl > 0:
                paper["wins"] += 1
            else:
                paper["losses"] += 1

            # remove trade
            paper["trades"] = [t for t in paper["trades"] 
                              if not (t["token"] == token and abs(t["entry"] - entry) < 0.00001)]
            return f"✅ SELL | PnL ${round(pnl, 4)} ({round((price-entry)/entry*100, 2)}%)"

# ================= IMPROVED SNIPER SIGNAL =================
def sniper_signal():
    watchlist = ["PEPE", "DOGE", "SHIB", "WIF", "BONK", "POPCAT", "BRETT", "MOG", "FLOKI", "GIGA"]
    term = random.choice(watchlist)
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search/?q={term}", timeout=5).json()
        pairs = r.get("pairs", [])
        good_pairs = []
        for p in pairs:
            try:
                liq = float(p.get("liquidity", {}).get("usd", 0))
                vol = float(p.get("volume", {}).get("h24", 0))
                if 20000 < liq < 600000 and vol > liq * 0.8:  # strong volume spike
                    token_addr = p["baseToken"]["address"] if p["baseToken"]["symbol"] != "WBNB" and p["baseToken"]["symbol"] != "WSOL" else p["quoteToken"]["address"]
                    good_pairs.append((token_addr, liq, vol))
            except:
                continue
        if good_pairs:
            token, _, _ = random.choice(good_pairs)
            price, _, _ = fetch_price(token)
            amt = position_size(paper["balance"], price)
            if amt > 0:
                return token, amt
    except:
        pass
    return None

# ================= MONITOR POSITIONS (SL + TP) =================
def monitor_positions():
    while True:
        with paper_lock:
            for t in paper["trades"][:]:
                price, _, _ = fetch_price(t["token"])
                pnl_pct = (price - t["entry"]) / t["entry"] * 100

                if pnl_pct <= -STOP_LOSS_PERCENT or pnl_pct >= TAKE_PROFIT_PERCENT:
                    trade("sell", t["token"], t["amount"], t["entry"])
        time.sleep(8)

# ================= AUTO LOOP =================
def auto_loop():
    while True:
        if AUTO_TRADING_ENABLED:
            sig = sniper_signal()
            if sig:
                token, amt = sig
                msg = trade("buy", token, amt)
                print(msg)
        time.sleep(AUTO_INTERVAL)

# Start threads
threading.Thread(target=auto_loop, daemon=True).start()
threading.Thread(target=monitor_positions, daemon=True).start()

# ================= UI =================
HTML = """
<!DOCTYPE html>
<html>
<head><title>MrBlack Sniper v2</title><style>body{font-family:Arial;background:#111;color:#0f0;}</style></head>
<body>
<h2>🚀 MrBlack Sniper v2 (Improved)</h2>
Balance: <b><span id="b"></span></b> USD<br>
Winrate: <span id="wr"></span>% | Total PnL: <span id="pnl"></span><br>
Open Trades: <span id="open"></span><br><br>

<button onclick="toggleAuto()">AUTO: <span id="autoStatus">OFF</span></button>
<button onclick="manualBuy()">Manual Buy Random</button>

<h3>Open Positions</h3>
<div id="positions"></div>

<script>
async function upd() {
  let r = await fetch('/stats');
  let d = await r.json();
  document.getElementById('b').innerText = d.balance.toFixed(4);
  document.getElementById('wr').innerText = d.winrate.toFixed(1);
  document.getElementById('pnl').innerText = d.total_pnl.toFixed(2);
  document.getElementById('open').innerText = d.open_trades;
  document.getElementById('positions').innerHTML = d.positions_html;
  document.getElementById('autoStatus').innerText = d.auto ? 'ON' : 'OFF';
}
setInterval(upd, 1800);
upd();

async function toggleAuto() {
  await fetch('/auto');
}
async function manualBuy() {
  await fetch('/manual_buy');
  upd();
}
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/stats")
def stats():
    with paper_lock:
        total = paper["wins"] + paper["losses"]
        wr = (paper["wins"] / total * 100) if total else 0
        positions_html = "<br>".join([f"{t['token'][:8]} @ {t['entry']:.6f} ({t['amount']:.6f})" for t in paper["trades"]]) or "No open trades"
        return jsonify(
            balance=paper["balance"],
            winrate=wr,
            total_pnl=paper["total_pnl"],
            open_trades=len(paper["trades"]),
            positions_html=positions_html,
            auto=AUTO_TRADING_ENABLED
        )

@app.route("/auto")
def auto():
    global AUTO_TRADING_ENABLED
    AUTO_TRADING_ENABLED = not AUTO_TRADING_ENABLED
    return {"auto": AUTO_TRADING_ENABLED}

@app.route("/manual_buy")
def manual_buy():
    sig = sniper_signal()
    if sig:
        token, amt = sig
        return trade("buy", token, amt)
    return "No signal"

# ================= RUN =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print("🚀 MrBlack Sniper v2 Started! Go to http://localhost:10000")
    app.run(host="0.0.0.0", port=port, debug=False)
