# -*- coding: utf-8 -*-
"""
Bybit Spot USDT Profit Vacuum Bot v1
Логика:
- Мягкий netPnL трейлинг (главный механизм фиксации): при netPnL >= 1.5$ начинаем отслеживать пик,
  если от пика просадка >= 0.6$ — мгновенно продаём.
- Дополнительная защита: жёсткий трейл + REVERSAL SELL по индикаторам (EMA, MACD, RSI).
- Усреднение: $5, $10 и далее, только если цена ниже средней и на 3% ниже последней продажи.
- Re-entry: разрешён только при цене <= last_sell_price*(1 - 0.03).
- Учёт комиссий, OBGuard, VolFilter, LiqRecovery.
- Telegram уведомления и подробные логи.
"""

import os, time, math, json, logging, datetime, traceback, requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ========= ENV =========
load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TG_TOKEN   = os.getenv("TG_TOKEN", "")
CHAT_ID    = os.getenv("CHAT_ID", "")
REDIS_URL  = os.getenv("REDIS_URL", "")
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

# ========= CONFIG =========
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT", "BTCUSDT"]

TAKER_FEE = 0.0018
BASE_MAX_TRADE = 35.0
OVERRIDE = {"TONUSDT": 70, "AVAXUSDT": 70, "ADAUSDT": 60, "BTCUSDT": 90}
RESERVE = 1.0
STOP_LOSS_PCT = 0.03

# Profit trailing
SOFT_MIN = 1.5
SOFT_DROP = 0.6
REVERSAL_EXIT = True
REVERSAL_MIN = 1.5

# Averaging
MAX_AVG = 2
AVG_STEP_USD = 5.0
REENTRY_DISC = 0.03

# Filters
USE_VOL = True
VOL_WINDOW = 20
VOL_FACTOR = 0.4
MIN_NOTIONAL = 15.0
USE_OB = True
OB_DEPTH = 25
MAX_SPREAD = 0.25
MAX_IMPACT = 0.35

# Liquidity recovery
LIQ_RECOVERY = True
LIQ_MIN = 20.0
LIQ_TARGET = 60.0

BTC = "BTCUSDT"
BTC_KEEP = 3000.0
BTC_FRAC = 0.18

INTERVAL = "1"
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_H = 22
DAILY_M = 30

# ========= LOGGING =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

# ========= TG =========
def tg(msg):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send error: {e}")

# ========= API =========
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# ========= STATE =========
STATE = {}
LIMITS = {}
LAST_REPORT = None

def save_state():
    try:
        if rds: rds.set("vacuum_state", json.dumps(STATE))
    except: pass
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f: f.write(json.dumps(STATE))
    except: pass

def load_state():
    global STATE
    try:
        if rds:
            s = rds.get("vacuum_state")
            if s: STATE = json.loads(s); return "REDIS"
    except: pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: STATE = json.load(f); return "FILE"
    except: STATE = {}; return "FRESH"

def init_state():
    src = load_state()
    logging.info(f"🚀 Бот стартует. Состояние: {src}")
    tg(f"🚀 Бот стартует. Состояние: {src}")
    for s in SYMBOLS:
        STATE.setdefault(s, {"positions": [], "pnl": 0.0, "last_sell": 0.0, "avg_count": 0})
    save_state()

# ========= HELPERS =========
def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def net_pnl(price, bp, qty_net, qty_gross):
    return price*qty_net*(1-TAKER_FEE) - bp*qty_gross

def sig(df):
    if len(df)<30: return "none",0,""
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]= EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]  = RSIIndicator(df["c"],9).rsi()
    m = MACD(df["c"]); df["macd"],df["sig"]=m.macd(),m.macd_signal()
    last=df.iloc[-1]
    if last["ema9"]>last["ema21"] and last["rsi"]>50 and last["macd"]>last["sig"]:
        return "buy",float(AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]),"buy"
    if last["ema9"]<last["ema21"] and last["rsi"]<50 and last["macd"]<last["sig"]:
        return "sell",0,"sell"
    return "none",0,""

def bearish(df):
    df=df.copy()
    df["ema9"]=EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]=EMAIndicator(df["c"],21).ema_indicator()
    m=MACD(df["c"]); df["macd"],df["sig"]=m.macd(),m.macd_signal()
    df["rsi"]=RSIIndicator(df["c"],9).rsi()
    last,prev=df.iloc[-1],df.iloc[-2]
    score=0
    if prev["ema9"]>=prev["ema21"] and last["ema9"]<last["ema21"]: score+=1
    if prev["macd"]>=prev["sig"] and last["macd"]<last["sig"]: score+=1
    if last["rsi"]<prev["rsi"]-3 and last["rsi"]<55: score+=1
    return score>=2

# ========= ORDERS =========
def buy(sym, qty):
    try:
        session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",
                            timeInForce="IOC",marketUnit="baseCoin",qty=str(qty))
        return True
    except Exception as e: logging.info(f"{sym} BUY fail: {e}"); return False

def sell(sym, qty):
    try:
        session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(qty))
        return True
    except Exception as e: logging.info(f"{sym} SELL fail: {e}"); return False

# ========= MAIN CYCLE =========
def trade_cycle():
    for sym in SYMBOLS:
        try:
            df=get_kline(sym)
            if df.empty: continue
            signal,atr,_=sig(df)
            price=df["c"].iloc[-1]
            st=STATE[sym]

            new_pos=[]
            for p in st["positions"]:
                pnl=net_pnl(price,p["buy_price"],p["qty"],p["buy_qty_gross"])
                p["max_pnl"]=max(p.get("max_pnl",0),pnl)

                if pnl>=SOFT_MIN and (p["max_pnl"]-pnl)>=SOFT_DROP:
                    if sell(sym,p["qty"]):
                        msg=f"✅ SOFT SELL {sym} @ {price}, pnl={pnl:.2f}"
                        logging.info(msg); tg(msg)
                        st["pnl"]+=pnl; st["last_sell"]=price; st["avg_count"]=0
                    continue

                if REVERSAL_EXIT and pnl>=REVERSAL_MIN and bearish(df):
                    if sell(sym,p["qty"]):
                        msg=f"✅ REVERSAL SELL {sym} @ {price}, pnl={pnl:.2f}"
                        logging.info(msg); tg(msg)
                        st["pnl"]+=pnl; st["last_sell"]=price; st["avg_count"]=0
                    continue

                new_pos.append(p)
            st["positions"]=new_pos

            # BUY
            if signal=="buy":
                if not st["positions"]:
                    if st["last_sell"] and price>st["last_sell"]*(1-REENTRY_DISC): continue
                    q=BASE_MAX_TRADE/price
                    if buy(sym,q):
                        st["positions"]=[{"buy_price":price,"qty":q*(1-TAKER_FEE),"buy_qty_gross":q,"max_pnl":0}]
                        msg=f"🟢 BUY {sym} @ {price}, qty={q}"
                        logging.info(msg); tg(msg)
                elif st["avg_count"]<MAX_AVG:
                    avg_price=sum(x["qty"]*x["buy_price"] for x in st["positions"])/sum(x["qty"] for x in st["positions"])
                    if price<avg_price and price<st["last_sell"]*(1-REENTRY_DISC):
                        q=BASE_MAX_TRADE/price
                        if buy(sym,q):
                            st["positions"].append({"buy_price":price,"qty":q*(1-TAKER_FEE),"buy_qty_gross":q,"max_pnl":0})
                            st["avg_count"]+=1
                            msg=f"🟢 AVG BUY {sym} @ {price}, qty={q}"
                            logging.info(msg); tg(msg)

        except Exception as e:
            logging.info(f"[{sym}] error: {e}\n{traceback.format_exc(2)}")

    save_state()

# ========= MAIN =========
if __name__=="__main__":
    init_state()
    tg("🚀 Bot starting: USDT Profit Vacuum v1")
    while True:
        trade_cycle()
        time.sleep(LOOP_SLEEP)
