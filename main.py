import os, time, threading, uuid, requests, random
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
from collections import defaultdict

app = Flask(__name__)

# ================= CONFIG =================
PAPER_START_BALANCE = 1.0
STOP_LOSS_PERCENT = 15
AUTO_TRADING_ENABLED = False
AUTO_INTERVAL = 45

# ================= STATE =================
paper = {
    "balance": PAPER_START_BALANCE,
    "trades": [],
    "wins": 0,
    "loss": 0,
    "daily_pnl": 0,
    "patterns": defaultdict(dict)
}

price_cache = {}

# ================= PRICE =================
def fetch_price(token):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={token}",
            timeout=5
        ).json()
        pair = r["pairs"][0]
        price=float(pair["priceUsd"])
        liq=float(pair["liquidity"]["usd"])
        vol=float(pair["volume"]["h24"])
        price_cache[token]=price
        return price,liq,vol
    except:
        return price_cache.get(token,1),100000,50000

# ================= POSITION =================
def position_size(balance,price):
    risk=balance*0.02
    size=risk/(STOP_LOSS_PERCENT/100)
    return round(size/price,6)

# ================= TRADE =================
def trade(action,token,amount,entry=None):

    price,_,_=fetch_price(token)

    if action=="buy":
        cost=amount*price
        if cost>paper["balance"]:
            return "Balance low"
        paper["balance"]-=cost
        paper["trades"].append({
            "token":token,
            "entry":price,
            "amount":amount,
            "time":datetime.now().isoformat()
        })
        return f"✅ BUY {token[:6]}"

    if action=="sell":
        pnl=(price-entry)*amount
        paper["balance"]+=amount*price
        paper["daily_pnl"]+=pnl
        paper["wins"]+= pnl>0
        paper["loss"]+= pnl<=0
        return f"✅ SELL | PnL {round(pnl,4)}"

# ================= SIGNAL =================
def sniper_signal():

    fake_tokens=[
        "0x55d398326f99059fF775485246999027B3197955",
        "0x2170Ed0880ac9A755fd29B2688956BD959F933F8"
    ]

    token=random.choice(fake_tokens)
    price,liq,vol=fetch_price(token)

    score=(vol/liq)*100
    if score>25:
        amt=position_size(paper["balance"],price)
        return token,amt

# ================= AUTO LOOP =================
def auto_loop():
    while True:
        if AUTO_TRADING_ENABLED:
            sig=sniper_signal()
            if sig:
                token,amt=sig
                trade("buy",token,amt)
        time.sleep(AUTO_INTERVAL)

threading.Thread(target=auto_loop,daemon=True).start()

# ================= UI =================
HTML="""
<h2>MrBlack Sniper</h2>
Balance:<span id=b></span>
<br><button onclick="auto()">AUTO TOGGLE</button>
<script>
async function upd(){
let r=await fetch('/stats');let d=await r.json();
b.innerText=d.balance.toFixed(4)
}
setInterval(upd,2000)

async function auto(){
await fetch('/auto')
}
</script>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/stats")
def stats():
    total=paper["wins"]+paper["loss"]
    wr=(paper["wins"]/total*100) if total else 0
    return jsonify(balance=paper["balance"],winrate=wr)

@app.route("/auto")
def auto():
    global AUTO_TRADING_ENABLED
    AUTO_TRADING_ENABLED=not AUTO_TRADING_ENABLED
    return {"auto":AUTO_TRADING_ENABLED}

# ================= RUN =================
if __name__=="__main__":
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port)
