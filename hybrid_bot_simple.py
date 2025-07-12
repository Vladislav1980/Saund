import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# — Конфиг —
load_dotenv()
API_KEY, API_SECRET = os.getenv("BYBIT_API_KEY"), os.getenv("BYBIT_API_SECRET")
TG_TOKEN, CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("CHAT_ID")
SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","XRPUSDT","ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT","SUIUSDT","FILUSDT"]

DEFAULT = {
    "risk_pct": 0.03,
    "tp_mult": 1.8,
    "trail_pct": 0.02,
    "max_dd": 0.06,
    "min_profit_usd": 2.5,
    "vol_filter": 0.3,
    "avg_drop_pct": 0.07,
    "rebuy_cooldown": 3600
}
RESERVE = 500
DAILY_LOSS = -50
MAX_POS = 100

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
            LIMITS[s] = {"step": float(f.get("qtyStep", 1)), "min_amt": float(it.get("minOrderAmt", 10))}

def get_balance(): 
    for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
        for c in w["coin"]:
            if c["coin"] == "USDT":
                return float(c["walletBalance"])
    return 0

def get_coin_balance(sym):
    c = sym.replace("USDT","")
    for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
        for coin in w["coin"]:
            if coin["coin"] == c:
                return float(coin["walletBalance"])
    return 0

def get_klines(sym, interval="5"):
    try:
        df = pd.DataFrame(session.get_kline(category="spot", symbol=sym, interval=interval, limit=100)["result"]["list"],
                         columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except:
        return pd.DataFrame()

def adjust(q, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(q * 10**abs(e)) / 10**abs(e)

def signal(df, sym):
    df["ema9"], df["ema21"] = EMAIndicator(df["c"],9).ema_indicator(), EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"], df["macd_s"] = macd.macd(), macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    log(f"[{sym}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_s']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT["vol_filter"]:
        return "none", last["atr"]
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
        return "buy", last["atr"]
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
        return "sell", last["atr"]
    return "none", last["atr"]

def check_trend(sym, tf):
    df = get_klines(sym, interval=tf)
    if df.empty: return False
    return signal(df, sym)[0] == "buy"

STATE = {}
if os.path.exists("state.json"):
    STATE = json.load(open("state.json"))
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "last_rebuy": 0, "count": 0, "pnl": 0.0})

def save_state():
    json.dump(STATE, open("state.json","w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS:
        log("🚫 Торговля остановлена: резерв или лимит убытка")
        return

    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        pos = STATE[sym]["pos"]
        sig5, atr5 = signal(get_klines(sym,"5"), sym)
        sig15, _ = signal(get_klines(sym,"15"), sym)
        trend_ok = sum([check_trend(sym, tf) for tf in ["60","240","1440"]]) >= 2
        price = get_klines(sym,"5")["c"].iloc[-1] if not get_klines(sym,"5").empty else None
        if price is None: continue

        # — SELL block —
        if pos:
            b, q, tp, peak = pos["buy_price"], pos["qty"], pos["tp"], pos["peak"]
            pnl = (price - b) * q - price * q * 0.001
            peak = max(peak, price)
            cond = price>=tp or (signal(get_klines(sym,"5"), sym)[0]=="sell" and pnl>0) \
                   or price<=peak*(1-DEFAULT["trail_pct"]) or (b-price)/b >= DEFAULT["max_dd"]
            if cond:
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                reason = "TP/Signal/Trail/Stop"
                log_trade = f"SELL {sym}@{price:.4f}, qty={q:.6f}, PnL={pnl:.2f} via {reason}"
                log(log_trade); send_tg(log_trade)
                STATE[sym]["pnl"] += pnl
                STATE[sym]["count"] += 1
                STATE[sym]["pos"] = None
            else:
                pos.update({"tp": max(tp, price + DEFAULT["tp_mult"]*atr5), "peak": peak})

        # — BUY or SELL from balance without pos —
        else:
            if (sig5=="buy" or sig15=="buy") and trend_ok and get_coin_balance(sym)==0:
                qty_usd = min(bal * DEFAULT["risk_pct"], MAX_POS)
                qty = qty_usd / price
                qty = adjust(qty, LIMITS[sym]["step"])
                if qty * price >= LIMITS[sym]["min_amt"]:
                    est_profit = atr5 * DEFAULT["tp_mult"] * qty - price*qty*0.001 - 2.5
                    if est_profit >= DEFAULT["min_profit_usd"]:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + DEFAULT["tp_mult"] * atr5
                        STATE[sym]["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price}
                        log_msg = f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
                        log(log_msg); send_tg(log_msg)
            elif sig5=="sell" and get_coin_balance(sym)>0:
                cb = get_coin_balance(sym)
                q = adjust(cb, LIMITS[sym]["step"])
                potential = q * price * 0.999
                if potential - (price*q*0.001) >= DEFAULT["min_profit_usd"]:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_msg = f"⚠️ SELL BALANCE {sym}@{price:.4f}, qty={q:.6f}"
                    log(log_msg); send_tg(log_msg)

        # — Averaging —
        pos = STATE[sym]["pos"]
        if pos and price <= pos["buy_price"] * (1 - DEFAULT["avg_drop_pct"]) and now - STATE[sym]["last_rebuy"] >= DEFAULT["rebuy_cooldown"]:
            extra_usd = min(bal * DEFAULT["risk_pct"], MAX_POS)
            extra_q = adjust(extra_usd / price, LIMITS[sym]["step"])
            if extra_q * price >= LIMITS[sym]["min_amt"]:
                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(extra_q))
                total_q = pos["qty"] + extra_q
                avg_price = (pos["buy_price"]*pos["qty"] + price*extra_q) / total_q
                STATE[sym]["pos"].update({"buy_price": avg_price, "qty": total_q, "tp": avg_price + DEFAULT["tp_mult"]*atr5})
                STATE[sym]["last_rebuy"] = now
                msg = f"AVERAGE {sym}: +{extra_q:.6f}@{price:.4f} => avg {avg_price:.4f}"
                log(msg); send_tg(msg)

    save_state()

def daily_report():
    d = datetime.datetime.now()
    if d.hour == 22:
        rep = "📊 Daily report\n"
        for s in SYMBOLS:
            rep += f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}\n"
        rep += f"Balance USDT={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS: STATE[s].update({"count": 0, "pnl": 0.0})
        save_state()

def main():
    log("🚀 Bot start full logs + MTF")
    send_tg("🚀 Bot start full logs + MTF")
    while True:
        try:
            trade()
            daily_report()
        except Exception as e:
            log(f"‼️ MAIN ERROR: {e}")
        time.sleep(60)

if __name__=="__main__":
    main()
