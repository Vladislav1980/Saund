import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","XRPUSDT","ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT","SUIUSDT","FILUSDT"]

DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.6,
    "trailing_stop_pct": 0.015,
    "max_drawdown_sl": 0.05,
    "min_profit_usd": 2.5,
    "volume_filter": 0.1,
    "avg_rebuy_drop_pct": 0.05,
    "rebuy_cooldown_secs": 1800,
    "min_trade_interval_secs": 120,
    "min_price_diff_pct": 0.003
}

RESERVE_BALANCE = 500
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": str(msg)})
    except Exception as e:
        log(f"Telegram error: {e}")

LIMITS = {}
def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {"step": float(f.get("qtyStep",1)), "min_amt": float(it.get("minOrderAmt",10))}

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]=="USDT": return float(c["walletBalance"])
    except Exception as e:
        log(f"balance err {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]==coin: return float(c["walletBalance"])
    except: pass
    return 0

def get_klines(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except:
        return pd.DataFrame()

def adjust(qty,step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty*10**abs(e))/10**abs(e)

def signal(df,sym):
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"]); df["macd"] = macd.macd(); df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    log(f"[{sym}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_s']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
        return "none", last["atr"]
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
        return "buy", last["atr"]
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
        return "sell", last["atr"]
    return "none", last["atr"]

STATE = {}
if os.path.exists("state.json"):
    try:
        STATE = json.load(open("state.json"))
    except:
        STATE = {}

for s in SYMBOLS:
    if s not in STATE:
        STATE[s] = {"pos": None, "last_trade": 0, "last_rebuy":0, "count": 0, "pnl": 0.0}

def save_state():
    json.dump(STATE, open("state.json", "w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена: резерв или лимит убытка")
        return
    load_limits()
    now = time.time()
    for sym in SYMBOLS:
        df = get_klines(sym)
        if df.empty: continue
        sig, atr = signal(df,sym)
        price = df["c"].iloc[-1]
        state = STATE[sym]
        pos = state["pos"]
        # SELL BLOCK
        if pos:
            b, q, tp, peak = pos["buy_price"], pos["qty"], pos["tp"], pos["peak"]
            pnl = (price-b)*q - price*q*0.001; dd=(b-price)/b; held = now-pos["time"]
            peak = max(peak, price)
            conditions = {
                "TP": price>=tp,
                "RSI": df["rsi"].iloc[-1]>=80 and pnl>0,
                "Trail": price<=peak*(1-DEFAULT_PARAMS["trailing_stop_pct"]) and pnl>0,
                "SL": dd>=DEFAULT_PARAMS["max_drawdown_sl"],
                "Bear": sig=="sell" and pnl>0,
                "Held": held>12*3600 and pnl>0
            }
            triggered = [k for k,v in conditions.items() if v]
            if triggered:
                reason = triggered[0]
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                msg = f"SELL {sym}@{price:.4f}, qty={q:.6f}, PnL={pnl:.2f}, reason={reason}"
                log(msg); send_tg(msg)
                state["pnl"] += pnl; state["count"] +=1
                state["pos"] = None
            else:
                state["pos"].update({"tp": max(tp, price+DEFAULT_PARAMS["tp_multiplier"]*atr), "peak": peak})
        else:
            # BUY BLOCK
            if sig=="buy" and now - state["last_trade"] >= DEFAULT_PARAMS["min_trade_interval_secs"]:
                qty_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                qty = qty_usd / price
                if sym in LIMITS:
                    qty = adjust(qty, LIMITS[sym]["step"])
                    if qty*price >= LIMITS[sym]["min_amt"]:
                        profit_est = atr*DEFAULT_PARAMS["tp_multiplier"]*qty - price*qty*0.001
                        if profit_est >= DEFAULT_PARAMS["min_profit_usd"]:
                            last_buy = state["pos"]["buy_price"] if state["pos"] else 0
                            if last_buy==0 or abs(price-last_buy)/last_buy >= DEFAULT_PARAMS["min_price_diff_pct"]:
                                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                                tp = price + DEFAULT_PARAMS["tp_multiplier"]*atr
                                state["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price, "time": now}
                                state["last_trade"] = now
                                msg = f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
                                log(msg); send_tg(msg)
            # BALANCE SELL
            elif sig=="sell" and get_coin_balance(sym)>0:
                cb = get_coin_balance(sym)
                step = LIMITS.get(sym, {}).get("step",1)
                q = adjust(cb, step)
                est_profit = q*price*0.999
                if est_profit >= DEFAULT_PARAMS["min_profit_usd"]*1.5:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    msg = f"SELL_BALANCE {sym}@{price:.4f}, qty={q:.6f}"
                    log(msg); send_tg(msg)
        # AVERAGING
        if state["pos"]:
            buy = state["pos"]["buy_price"]
            if price <= buy*(1-DEFAULT_PARAMS["avg_rebuy_drop_pct"]) and now-state["last_rebuy"]>=DEFAULT_PARAMS["rebuy_cooldown_secs"]:
                extra_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                extra_q = extra_usd / price
                extra_q = adjust(extra_q, LIMITS.get(sym, {}).get("step",1))
                if extra_q*price >= LIMITS[sym]["min_amt"]:
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(extra_q))
                    total = state["pos"]["qty"]+extra_q
                    avgp = (state["pos"]["buy_price"]*state["pos"]["qty"] + price*extra_q)/total
                    state["pos"].update({"buy_price": avgp, "qty": total, "tp": avgp + DEFAULT_PARAMS["tp_multiplier"]*atr})
                    state["last_rebuy"] = now
                    msg = f"AVERAGE {sym}: +{extra_q:.6f}@{price:.4f} => avg {avgp:.4f}"
                    log(msg); send_tg(msg)
    save_state()

def daily_report():
    d = datetime.datetime.now()
    if d.hour == 22:
        rep = "📊 Daily Report\n"
        for s in SYMBOLS:
            rep += f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}\n"
        rep += f"Balance USDT={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = 0; STATE[s]["pnl"] = 0.0
        save_state()

def main():
    log("🚀 Bot start. Full logs.")
    send_tg("🚀 Bot start. Full logs.")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__=="__main__":
    main()
