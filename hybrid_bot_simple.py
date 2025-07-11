import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# --- Конфигурация ---
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","XRPUSDT",
           "ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT",
           "SUIUSDT","FILUSDT"]

DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usd": 2.5,  # изменено
    "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600,
    "orderbook_ratio_min": 1.05  # новый порог
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
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": str(msg)})
    except Exception as e:
        log(f"Telegram error: {e}")

# --- LIMITS ---
LIMITS = {}
def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {
                "step": float(f.get("qtyStep", 1)),
                "min_amt": float(it.get("minOrderAmt", 10))
            }

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10**abs(e)) / 10**abs(e)

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except Exception as e:
        log(f"balance err {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except:
        pass
    return 0

def get_klines(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except:
        return pd.DataFrame()

def fetch_orderbook(sym):
    try:
        ob = session.get_orderbook(category="spot", symbol=sym, limit=10)["result"]
        bids = sum(float(x[1]) for x in ob["bids"])
        asks = sum(float(x[1]) for x in ob["asks"])
        ratio = bids / asks if asks > 0 else 0
        return ratio
    except Exception as e:
        log(f"orderbook err {sym}: {e}")
        return None

def signal(df, sym):
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    log(f"[{sym}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_s']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
        return "none", last["atr"], "volume drop"
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
        return "buy", last["atr"], "EMA+MACD up"
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
        return "sell", last["atr"], "EMA+MACD down"
    return "none", last["atr"], "no signal"

# --- STATE ---
STATE = {}
if os.path.exists("state.json"):
    try: STATE = json.load(open("state.json"))
    except: STATE = {}

for s in SYMBOLS:
    STATE.setdefault(s, {"pos":None,"last_rebuy":0,"count":0,"pnl":0.0})

def save_state():
    json.dump(STATE, open("state.json","w"), indent=2)

# --- TRADE LOOP ---
def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена (резерв/лимит)")
        return

    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        df = get_klines(sym)
        if df.empty:
            log(f"{sym} — нет данных.")
            continue

        sig, atr, reason = signal(df, sym)
        price = df["c"].iloc[-1]
        ob_ratio = fetch_orderbook(sym)
        if ob_ratio is None or ob_ratio < DEFAULT_PARAMS["orderbook_ratio_min"]:
            log(f"{sym} — пропуск по стакану: ratio={ob_ratio}")
            continue

        log(f"{sym} — sig={sig}, reason={reason}, ob_ratio={ob_ratio:.2f}")
        state = STATE[sym]
        pos = state["pos"]

        # SELL block
        if pos:
            b = pos["buy_price"]; q = pos["qty"]; tp = pos["tp"]; peak = pos["peak"]
            pnl = (price - b)*q - price*q*0.001
            dd = (b - price)/b
            held = now - pos["time"]
            peak = max(peak, price)
            take = price >= tp
            rsi_exit = df["rsi"].iloc[-1] >= 80 and pnl > (0.001*price*q + DEFAULT_PARAMS["min_profit_usd"])
            trail = price <= peak*(1-DEFAULT_PARAMS["trailing_stop_pct"]) and pnl > (0.001*price*q + DEFAULT_PARAMS["min_profit_usd"])
            sl = dd >= DEFAULT_PARAMS["max_drawdown_sl"]
            bear = sig=="sell" and pnl > (0.001*price*q + DEFAULT_PARAMS["min_profit_usd"])
            hold_limit = held > 12*3600 and pnl > (0.001*price*q + DEFAULT_PARAMS["min_profit_usd"])

            if take or rsi_exit or trail or sl or bear or hold_limit:
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                reason_s = ("TP" if take else "RSI" if rsi_exit else "Trail" if trail else "SL" if sl else "Bear" if bear else "Held")
                msg = f"SELL {sym}@{price:.4f}, qty={q:.6f}, PnL={pnl:.2f} via {reason_s}"
                log(msg); send_tg(msg)
                state["pnl"] += pnl; state["count"] += 1
                state["pos"] = None

            else:
                new_tp = max(tp, price + DEFAULT_PARAMS["tp_multiplier"]*atr)
                state["pos"].update({"tp":new_tp, "peak":peak})

        # BUY block
        else:
            if sig == "buy":
                qty_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                qty = qty_usd / price
                qadj = adjust(qty, LIMITS[sym]["step"])
                if qadj * price < LIMITS[sym]["min_amt"]:
                    log(f"{sym} — пропуск BUY: qty*price {qadj*price:.2f} < min_amt")
                else:
                    tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
                    profit_est = (tp - price)*qadj - price*qadj*0.001 - DEFAULT_PARAMS["min_profit_usd"]
                    if profit_est > 0:
                        session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qadj))
                        state["pos"] = {"buy_price":price,"qty":qadj,"tp":tp,"peak":price,"time":now}
                        msg = f"BUY {sym}@{price:.4f}, qty={qadj:.6f}, TP~{tp:.4f}"
                        log(msg); send_tg(msg)
                    else:
                        log(f"{sym} — пропуск: профит после комиссии+2.5USDT не гарантирован")

            elif sig=="sell" and get_coin_balance(sym) > 0:
                cb = get_coin_balance(sym)
                qadj = adjust(cb, LIMITS[sym]["step"])
                if qadj * price * 0.999 < DEFAULT_PARAMS["min_profit_usd"]:
                    log(f"{sym} — пропуск SELL баланса: недостаточно для прибыли")
                else:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(qadj))
                    msg = f"SELL BALANCE {sym}@{price:.4f}, qty={qadj:.6f}"
                    log(msg); send_tg(msg)

        # Averaging block
        if state["pos"]:
            buy = state["pos"]["buy_price"]
            if price <= buy*(1-DEFAULT_PARAMS["avg_rebuy_drop_pct"]) and now - state["last_rebuy"] >= DEFAULT_PARAMS["rebuy_cooldown_secs"]:
                extra_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                extra_qty = adjust(extra_usd/price, LIMITS[sym]["step"])
                if extra_qty * price >= LIMITS[sym]["min_amt"]:
                    session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(extra_qty))
                    total_qty = state["pos"]["qty"] + extra_qty
                    avg_price = (state["pos"]["buy_price"]*state["pos"]["qty"] + price*extra_qty)/total_qty
                    state["pos"].update({"buy_price":avg_price, "qty":total_qty, "tp":avg_price + DEFAULT_PARAMS["tp_multiplier"]*atr})
                    state["last_rebuy"] = now
                    msg = f"AVERAGE {sym}: +{extra_qty:.6f} @ {price:.4f} → avg={avg_price:.4f}"
                    log(msg); send_tg(msg)
                else:
                    log(f"{sym} — пропуск AVERAGE: слишком маленькая qty")
    save_state()

def daily_report():
    now = datetime.datetime.now()
    if now.hour == 22:
        rep = "📊 Ежедневный отчёт\n"
        for s in SYMBOLS:
            rep += f"{s}: сделок={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f} USDT\n"
        rep += f"Баланс USDT={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = 0; STATE[s]["pnl"] = 0.0
        save_state()

def main():
    log("🚀 Bot старт. Логи полные.")
    send_tg("🚀 Bot старт. Логи полные.")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__=="__main__":
    main()
