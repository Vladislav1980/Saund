import os, time, logging, datetime, requests, pandas as pd
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DECIMALS = {"BTCUSDT": 5, "ETHUSDT": 4}
ORDER_MIN = 30
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
                    handlers=[logging.StreamHandler()])

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    except: pass

def log(msg): logging.info(msg); print(msg); send_tg(msg)

def get_balance():
    try:
        coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        for c in coins: 
            if c["coin"] == "USDT": return float(c["walletBalance"] or 0)
    except: return 0

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts", "o", "h", "l", "c", "vol", "turn"])
    df[["o", "h", "l", "c", "vol"]] = df[["o", "h", "l", "c", "vol"]].astype(float)
    return df

def signal(df):
    if len(df) < 21: return "none"
    ema9 = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    if ema9.iloc[-1] > ema21.iloc[-1]: return "buy"
    if ema9.iloc[-1] < ema21.iloc[-1]: return "sell"
    return "none"

def get_qty(sym, price):
    return round(ORDER_MIN / price, DECIMALS[sym])

def trade():
    usdt = get_balance()
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            sig = signal(df)
            price = df["c"].iloc[-1]
            if sig == "buy" and usdt >= ORDER_MIN:
                qty = get_qty(sym, price)
                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                log(f"BUY {sym} по {price}")
            elif sig == "sell":
                qty = get_qty(sym, price)
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                log(f"SELL {sym} по {price}")
        except Exception as e:
            log(f"Ошибка {sym}: {e}")

if __name__ == "__main__":
    log("Бот запущен")
    while True:
        trade()
        time.sleep(60)
