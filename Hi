import os
import time
import json
import logging
import datetime
import requests
import pandas as pd
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator
from dotenv import load_dotenv

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

ORDER_MIN = 30  # USDT

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

STATE = {s: {"position": False, "buy_price": 0, "pnl": 0, "count": 0} for s in SYMBOLS}
LAST_REPORT_DATE = None

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
    except Exception as e:
        log(f"⚠️ Ошибка получения баланса: {e}")
    return 0

get_qty(sym, price):
    try:
        info = session.get_instruments_info(category="spot", symbol=sym)
        filters = info["result"]["list"][0]
        qty_step = filters.get("lotSizeFilter", {}).get("qtyStep", None)
        if qty_step:
            decimals = abs(int(round(float(f"{float(qty_step):e}".split("e")[-1]))))
            return round(ORDER_MIN / price, decimals)
        return round(ORDER_MIN / price, DECIMALS[sym])
    except Exception as e:
        log(f"[{sym}] ℹ qtyStep недоступен, используется fallback: {e}")
        return round(ORDER_MIN / price, DECIMALS.get(sym, 2))
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

def signal(df, sym):
    if df.empty or len(df) < 21:
        return "none"

    ema9 = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    df["ema9"] = ema9
    df["ema21"] = ema21

    last = df.iloc[-1]
    vol_spike = df["vol"].iloc[-1] > df["vol"].rolling(20).mean().iloc[-1] * 1.5

    bid, ask = get_orderbook(sym)
    bid_strength = bid / (ask + 1e-9)

    log(f"[{sym}] EMA9: {last['ema9']:.4f}, EMA21: {last['ema21']:.4f}, Vol: {df['vol'].iloc[-1]:.2f}, Vol_MA20: {df['vol'].rolling(20).mean().iloc[-1]:.2f}")

    if last["ema9"] > last["ema21"] and bid_strength > 1.1 and vol_spike:
        return "buy"
    if last["ema9"] < last["ema21"] or bid_strength < 0.85:
        return "sell"
    return "none"

def cascade_sell(sym, buy_price, full_qty):
    try:
        targets = [0, 0.01, 0.02]
        weights = [0.33, 0.33, 0.34]

        for target_pct, weight in zip(targets, weights):
            price = round(buy_price * (1 + target_pct), 6)
            qty = round(full_qty * weight, DECIMALS[sym])
            if qty == 0:
                continue
            session.place_order(
                category="spot",
                symbol=sym,
                side="Sell",
                orderType="Limit",
                qty=str(qty),
                price=str(price),
                timeInForce="GTC"
            )
            log(f"📈 Каскадный ордер: {sym} | {qty} по {price}", True)
        return True
    except Exception as e:
        log(f"[{sym}] ❌ Ошибка каскадной продажи: {e}", True)
        return False

def should_cancel_order(sym, buy_price):
    df = get_kline(sym)
    if df.empty or len(df) < 21:
        return False
    current_price = df["c"].iloc[-1]
    ema9 = EMAIndicator(df["c"], 9).ema_indicator().iloc[-1]
    ema21 = EMAIndicator(df["c"], 21).ema_indicator().iloc[-1]
    vol = df["vol"].iloc[-1]
    vol_ma = df["vol"].rolling(20).mean().iloc[-1]
    bid, ask = get_orderbook(sym)
    bid_strength = bid / (ask + 1e-9)

    if current_price < buy_price * 0.9 and ema9 < ema21 and vol < vol_ma and bid_strength < 1:
        log(f"[{sym}] ❗ Отмена ордера: сильное падение подтверждено")
        return True
    return False
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

            price = df["c"].iloc[-1]
            state = STATE[sym]
            in_pos = state["position"]

            if in_pos and should_cancel_order(sym, state["buy_price"]):
                open_orders = session.get_open_orders(category="spot", symbol=sym)["result"]["list"]
                for order in open_orders:
                    if order["side"] == "Sell":
                        session.cancel_order(category="spot", symbol=sym, orderId=order["orderId"])
                        log(f"❌ Отменён ордер {order['orderId']} для {sym}", True)
                STATE[sym]["position"] = False
                # Автоматическая попытка повторной покупки
                sig = signal(df, sym)
                if sig == "buy" and usdt >= ORDER_MIN:
                    qty = get_qty(sym, price)
                    if qty > 0:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        STATE[sym].update({"position": True, "buy_price": price, "count": state["count"] + 1})
                        log(f"✅ REBUY {sym} по {price} USDT после отмены", True)
                continue

            sig = signal(df, sym)
            bid, ask = get_orderbook(sym)
            log(f"[{sym}] Цена: {price:.4f} | Bid: {bid:.2f} Ask: {ask:.2f} | Сигнал: {sig}")

            if sig == "buy" and not in_pos and usdt >= ORDER_MIN:
                qty = get_qty(sym, price)
                if qty == 0:
                    log(f"[{sym}] QTY = 0, пропуск покупки")
                    continue
                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                STATE[sym].update({"position": True, "buy_price": price, "count": state["count"] + 1})
                log(f"✅ BUY {sym} по {price} USDT", True)

            elif sig == "sell" and in_pos:
                pnl = price - state["buy_price"]
                profit_percent = (pnl / state["buy_price"]) * 100
                if profit_percent < 0.3:
                    log(f"[{sym}] 📉 Продажа невыгодна (PnL: {pnl:.4f}, {profit_percent:.2f}%) — удержание позиции")
                    continue
                full_qty = get_qty(sym, state["buy_price"])
                if full_qty == 0:
                    log(f"[{sym}] QTY = 0, пропуск каскада")
                    continue
                if cascade_sell(sym, state["buy_price"], full_qty):
                    STATE[sym].update({"position": False, "pnl": state["pnl"] + pnl})

        except Exception as e:
            log(f"🛑 Ошибка в {sym}: {e}", True)

    if now.hour == 22 and now.minute >= 30 and (LAST_REPORT_DATE != now.date()):
        report = "\n".join([f"{s} | PnL: {v['pnl']:.4f} | Сделок: {v['count']}" for s, v in STATE.items()])
        log("📊 Дневной отчет (22:30):\n" + report, True)
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {e}", True)
        time.sleep(60)
