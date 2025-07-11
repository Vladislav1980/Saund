import os, time, math, json, datetime, logging, requests, sys
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# === Загрузка настроек и ключей ===
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY"); API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN"); CHAT_ID = os.getenv("CHAT_ID")

# === Настройки параметров и символов ===
SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","TRXUSDT","XRPUSDT",
           "ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT","SUIUSDT","FILUSDT"]
DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usd": 5.0,
    "volume_filter": 0.25,
    "avg_drop_pct": 0.07,
    "avg_confirm_pct": 0.03
}
RESERVE_BALANCE = 500
MAX_POS_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
)
def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        log(f"Telegram error: {e}")

# === Загрузка лимитов ===
LIMITS = {}
def load_limits():
    for item in session.get_instruments_info(category="spot")["result"]["list"]:
        sym = item.get("symbol")
        if sym in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[sym] = {
                "qty_step": float(f.get("qtyStep",1)),
                "min_amt": float(item.get("minOrderAmt",10))
            }

# === Балансы ===
def get_balance(): 
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]=="USDT": return float(c["walletBalance"])
    except Exception as e:
        log(f"❌ get_balance error: {e}")
    return 0

def get_coin_balance(sym):
    coin=sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]==coin: return float(c["walletBalance"])
    except: pass
    return 0

# === Работа с данными ===
def get_kline(sym):
    try:
        r = session.get_kline(category="spot",symbol=sym,interval="1",limit=100)
        df=pd.DataFrame(r["result"]["list"],columns=["ts","o","h","l","c","vol","turn"])
        return df.astype({"o":"float","h":"float","l":"float","c":"float","vol":"float"})
    except: return pd.DataFrame()

def adjust_qty(qty, step):
    exp=int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10**abs(exp)) / 10**abs(exp)

# === Сигналы ===
def signal(df,sym):
    df["ema9"]=EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]=EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]=RSIIndicator(df["c"]).rsi()
    macd=MACD(df["c"])
    df["macd"],df["macd_signal"]=macd.macd(),macd.macd_signal()
    df["atr"]=AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"]=df["vol"].pct_change().fillna(0)
    last=df.iloc[-1]
    log(f"[{sym}] EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, volΔ={last['vol_ch']:.2f}, ATR={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
        return "none", last["atr"], "volume drop"
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], "buy signal"
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"], "sell signal"
    return "none", last["atr"], "no signal"

# === Состояние ===
STATE = {}
if os.path.exists("state.json"):
    try:
        STATE=json.load(open("state.json"))
    except:
        STATE={}
for s in SYMBOLS:
    STATE.setdefault(s,{"positions":[], "last_buy_price":0})

def save_state():
    json.dump(STATE, open("state.json","w"), indent=2)

# === Торговый цикл ===
def trade():
    bal=get_balance(); log(f"Balance USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE: 
        log("🛑 Баланс ниже резерва.")
        return
    load_limits()
    for sym in SYMBOLS:
        df=get_kline(sym)
        if df.empty: continue
        sig, atr, reason = signal(df,sym)
        price=df["c"].iloc[-1]
        state=STATE[sym]
        # Продажа по балансу, если выгодно
        coin_bal=get_coin_balance(sym)
        if coin_bal>0:
            est_pnl=(price - state.get("avg_price",price))*coin_bal - price*coin_bal*0.001
            if est_pnl >= DEFAULT_PARAMS["min_profit_usd"]:
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(coin_bal))
                msg=f"💸 SELL balance {sym}@{price:.4f}, qty={coin_bal}, PnL≈{est_pnl:.2f}"
                log(msg); send_tg(msg)
                state["positions"]=[]; state["last_buy_price"]=0
                continue
        # Управление текущими позициями
        new_pos=[]
        for pos in state["positions"]:
            b,q,tp,peak=pos["buy"],pos["qty"],pos["tp"],pos["peak"]
            pnl=(price-b)*q - price*q*0.001
            draw=(b-price)/b
            peak=max(peak,price)
            if price>=tp or price<=peak*(1-DEFAULT_PARAMS["trailing_stop_pct"]) or draw>=DEFAULT_PARAMS["max_drawdown_sl"] or sig=="sell":
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                msg=f"💸 SELL {sym}@{price:.4f}, qty={q}, PnL={pnl:.2f}"
                log(msg); send_tg(msg)
            else:
                pos["tp"]=max(tp, price+DEFAULT_PARAMS["tp_multiplier"]*atr)
                pos["peak"]=peak
                new_pos.append(pos)
        state["positions"]=new_pos

        # Покупка / усреднение
        if sig=="buy":
            if state["positions"]:
                last_price=state["last_buy_price"]
                drop_pct=(last_price-price)/last_price if last_price else 0
                if drop_pct>=DEFAULT_PARAMS["avg_drop_pct"]:
                    qty_usdt=min(bal*DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                    qty=adjust_qty(qty_usdt/price, LIMITS[sym]["qty_step"])
                    if qty*price>=LIMITS[sym]["min_amt"]:
                        tp=price+DEFAULT_PARAMS["tp_multiplier"]*atr
                        session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                        msg=f"↔️ AVG {sym}@{price:.4f}, qty={qty}"
                        log(msg); send_tg(msg)
                        total_qty=sum(p["qty"] for p in state["positions"])+qty
                        avg_price=(sum(p["buy"]*p["qty"] for p in state["positions"])+price*qty)/total_qty
                        state["positions"].append({"buy":price,"qty":qty,"tp":tp,"peak":price})
                        state["last_buy_price"]=avg_price
            else:
                qty_usdt=min(bal*DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                qty=adjust_qty(qty_usdt/price, LIMITS[sym]["qty_step"])
                if qty*price>=LIMITS[sym]["min_amt"]:
                    tp=price+DEFAULT_PARAMS["tp_multiplier"]*atr
                    session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                    msg=f"🟢 BUY {sym}@{price:.4f}, qty={qty}"
                    log(msg); send_tg(msg)
                    state["positions"]=[{"buy":price,"qty":qty,"tp":tp,"peak":price}]
                    state["last_buy_price"]=price
    save_state()

# === Главная функция ===
def main():
    send_tg("🚀 Бот запущен с подробными логами, TP/SL/Trailing/Averaging enabled.")
    while True:
        try:
            trade()
        except Exception as e:
            log(f"❌ Ошибка: {e}")
            send_tg(f"❌ Ошибка: {e}")
        time.sleep(60)

if __name__=="__main__":
    main()
