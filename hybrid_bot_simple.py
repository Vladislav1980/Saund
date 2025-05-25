import os
import time
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

LIMITS = {
    "BTCUSDT": {"min_qty": 0.00001, "qty_step": 0.00001},
    "ETHUSDT": {"min_qty": 0.0001, "qty_step": 0.0001},
    "SOLUSDT": {"min_qty": 0.001, "qty_step": 0.001},
    "COMPUSDT": {"min_qty": 0.01, "qty_step": 0.01},
    "NEARUSDT": {"min_qty": 0.1, "qty_step": 0.1},
    "TONUSDT": {"min_qty": 0.1, "qty_step": 0.1},
    "TRXUSDT": {"min_qty": 1, "qty_step": 1},
    "XRPUSDT": {"min_qty": 1, "qty_step": 1},
    "ADAUSDT": {"min_qty": 1, "qty_step": 1},
    "BCHUSDT": {"min_qty": 0.001, "qty_step": 0.001},
    "LTCUSDT": {"min_qty": 0.001, "qty_step": 0.001},
    "ZILUSDT": {"min_qty": 1, "qty_step": 1},
    "AVAXUSDT": {"min_qty": 0.01, "qty_step": 0.01},
    "DOGEUSDT": {"min_qty": 1, "qty_step": 1},
    "EOSUSDT": {"min_qty": 1, "qty_step": 1},
    "POLUSDT": {"min_qty": 1, "qty_step": 1},
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
        step = LIMITS[sym]["qty_step"]
        raw_qty = usdt_amount / price
        decimals = abs(int(f"{step:e}".split("e")[-1]))
        return round(raw_qty, decimals)
    except:
        return 0
def trade():
    global LAST_REPORT_DATE
    usdt = get_balance()
    now = datetime.datetime.now()
    min_amt = 25 if usdt > 300 else 5
    min_required_balance = len(SYMBOLS) * min_amt * 2
    allow_buying = usdt >= min_required_balance

    log(f"🔁 Мониторинг монет... Баланс: {usdt:.2f} USDT, мин. ордер: {min_amt} USDT")

    if not allow_buying:
        log(f"💤 Баланс {usdt:.2f} < {min_required_balance} — только продажи.")
    else:
        log(f"✅ Баланс {usdt:.2f} ≥ {min_required_balance} — полная торговля.")

    for sym in SYMBOLS:
        try:
            log(f"[{sym}] 🔍 Проверка")
            df = get_kline(sym)
            if df.empty or len(df) < 21:
                continue

            df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
            df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
            df["rsi"] = RSIIndicator(df["c"], 14).rsi()
            df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
            last = df.iloc[-1]
            bid, ask = get_orderbook(sym)
            bid_strength = bid / (ask + 1e-9)

            price = last["c"]
            atr = last["atr"]
            step = LIMITS[sym]["qty_step"]
            min_qty = LIMITS[sym]["min_qty"]
            state = STATE[sym]
            already_in_position = len(state["positions"]) > 0

            balance_per_coin = usdt / len(SYMBOLS)
            max_orders = max(1, int(balance_per_coin / min_amt))
            order_usdt = max(min_amt, balance_per_coin / max_orders)
            order_usdt = min(order_usdt, usdt)
            qty = get_qty(sym, price, order_usdt)
            cost = qty * price

            # ПОКУПКА
            if allow_buying and not already_in_position and last["ema9"] > last["ema21"] and bid_strength > 1.0 and last["vol"] > df["vol"].rolling(20).mean().iloc[-1] * 1.2 and last["rsi"] > 50:
                if qty < min_qty:
                    log(f"[{sym}] ❌ qty={qty:.4f} < minQty {min_qty} — отмена")
                    continue
                if cost < min_amt:
                    log(f"[{sym}] ❌ Сумма {cost:.2f} < minAmt {min_amt} — отмена")
                    continue
                if cost > usdt:
                    log(f"[{sym}] ❌ Недостаточно баланса: нужно {cost:.2f}, есть {usdt:.2f}")
                    continue

                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                state["positions"].append({"buy_price": price, "qty": qty, "atr": atr})
                state["count"] += 1
                log(f"✅ BUY {sym} по {price:.4f}, qty={qty}", True)

            # ПРОДАЖА
            new_positions = []
            for pos in state["positions"]:
                sell_price = price
                trigger_price = pos["buy_price"] + pos["atr"]
                if sell_price >= trigger_price:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(pos["qty"]))
                    pnl = (sell_price - pos["buy_price"]) * pos["qty"]
                    state["pnl"] += pnl
                    log(f"🚫 SELL {sym} по {sell_price:.4f} | PnL: {pnl:.4f}", True)
                else:
                    new_positions.append(pos)
            state["positions"] = new_positions

        except Exception as e:
            log(f"[{sym}] 🛑 Ошибка: {e}", True)

    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        report = "\n".join([f"{s}: PnL {v['pnl']:.2f}, Сделок {v['count']}" for s, v in STATE.items()])
        log("📊 Отчёт за день:\n" + report, True)
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {e}", True)
        time.sleep(60)
