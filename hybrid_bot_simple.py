import os
import time
import json
import logging
import datetime
import requests
import pandas as pd
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from dotenv import load_dotenv
import random

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "COMPUSDT", "NEARUSDT", "TONUSDT",
    "TRXUSDT", "XRPUSDT", "ADAUSDT", "BCHUSDT", "LTCUSDT", "ZILUSDT",
    "AVAXUSDT", "DOGEUSDT", "EOSUSDT", "POLUSDT"
]

DECIMALS = {
    "BTCUSDT": 5, "ETHUSDT": 4, "SOLUSDT": 3, "COMPUSDT": 3, "NEARUSDT": 2, "TONUSDT": 2,
    "TRXUSDT": 0, "XRPUSDT": 1, "ADAUSDT": 1, "BCHUSDT": 3, "LTCUSDT": 3, "ZILUSDT": 0,
    "AVAXUSDT": 2, "DOGEUSDT": 0, "EOSUSDT": 1, "POLUSDT": 0
}

STATE = {s: {"positions": [], "pnl": 0, "count": 0} for s in SYMBOLS}
LAST_REPORT_DATE = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"[Telegram Error] {e}")

def log(msg, force_tg=False):
    logging.info(msg)
    print(msg)
    if force_tg or any(k in msg for k in ["BUY", "SELL", "Ошибка", "Бот запущен", "📊"]):
        send_tg(msg)

def get_balance():
    try:
        coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["walletBalance"] or 0)
    except:
        return 0

def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts", "o", "h", "l", "c", "vol", "turn"])
        df[["o", "h", "l", "c", "vol"]] = df[["o", "h", "l", "c", "vol"]].astype(float)
        return df
    except:
        return pd.DataFrame()

def get_orderbook(sym):
    try:
        ob = session.get_orderbook(category="spot", symbol=sym)["result"]
        bid = sum(float(b[1]) for b in ob["b"][:5])
        ask = sum(float(a[1]) for a in ob["a"][:5])
        return bid, ask
    except:
        return 0, 0

def get_qty(sym, price, usdt_amount):
    try:
        info = session.get_instruments_info(category="spot", symbol=sym)
        filters = info["result"]["list"][0]
        qty_step = filters.get("lotSizeFilter", {}).get("qtyStep")
        if qty_step:
            exponent = int(f"{float(qty_step):e}".split("e")[-1])
            decimals = abs(exponent)
            qty = round(usdt_amount / price, decimals)
        else:
            qty = round(usdt_amount / price, DECIMALS[sym])
        return qty
    except:
        return 0

def log_trade(sym, side, price, qty, pnl):
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{pnl}\n")

def signal(df, sym):
    if df.empty or len(df) < 21:
        return "none", 0

    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 14).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()

    last = df.iloc[-1]
    vol_spike = last["vol"] > df["vol"].rolling(20).mean().iloc[-1] * 1.2
    bid, ask = get_orderbook(sym)
    bid_strength = bid / (ask + 1e-9)

    log(f"[{sym}] EMA9: {last['ema9']:.4f}, EMA21: {last['ema21']:.4f}, RSI: {last['rsi']:.2f}, ATR: {last['atr']:.4f}")

    if last["ema9"] > last["ema21"] and bid_strength > 1.0 and vol_spike and last["rsi"] > 50:
        return "buy", last["atr"]
    if last["ema9"] < last["ema21"] or bid_strength < 0.85:
        return "sell", last["atr"]
    return "none", last["atr"]

def trade():
    global LAST_REPORT_DATE
    usdt = get_balance()
    now = datetime.datetime.now()
    log("🔁 Мониторинг монет...")

    for sym in SYMBOLS:
        try:
            log(f"[{sym}] 🔍 Проверка")
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ⚠ Нет данных")
                continue

            sig, atr = signal(df, sym)
            price = df["c"].iloc[-1]
            balance_per_coin = usdt / len(SYMBOLS)
            num_orders = random.randint(5, 15)
            order_usdt = balance_per_coin / num_orders
            qty = get_qty(sym, price, order_usdt)

            if qty == 0 or order_usdt < 5:
                log(f"[{sym}] Ордер слишком мал (qty={qty}, usdt={order_usdt:.2f})")
                continue

            state = STATE[sym]

            if sig == "buy":
                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                tp = price + atr * 1.5
                state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                state["count"] += 1
                log(f"✅ BUY {sym} по {price:.4f}, qty={qty}, TP={tp:.4f}", True)
                log_trade(sym, "BUY", price, qty, 0)

            new_positions = []
            for pos in state["positions"]:
                sell_price = price
                profit = sell_price - pos["buy_price"]
                if (sig == "sell" and profit > 0) or sell_price >= pos["tp"]:
                    qty = pos["qty"]
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                    pnl = profit * qty
                    state["pnl"] += pnl
                    log(f"🚫 SELL {sym} по {sell_price:.4f} | PnL: {pnl:.4f}", True)
                    log_trade(sym, "SELL", sell_price, qty, pnl)
                else:
                    new_positions.append(pos)
            state["positions"] = new_positions

        except Exception as e:
            log(f"🛑 Ошибка в {sym}: {e}", True)

    if now.hour == 22 and now.minute >= 30 and (LAST_REPORT_DATE != now.date()):
        report = "\n".join([f"{s} | PnL: {v['pnl']:.4f} | Сделок: {v['count']}" for s, v in STATE.items()])
        log("📊 Дневной отчёт (22:30):\n" + report, True)
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {e}", True)
        time.sleep(60)
