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

DECIMALS = {s: 4 for s in SYMBOLS}
STATE = {s: {"positions": [], "pnl": 0, "count": 0, "bought": 0, "sold": 0, "last_sell": None, "reentries": 0} for s in SYMBOLS}
LAST_REPORT_DATE = None
AGGRESSIVE_MODE = False
MIN_BALANCE = 1000
MIN_ORDER_USDT = 25
MIN_RESERVE_USDT = 500

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
    if force_tg or any(msg.startswith(k) for k in ["✅ BUY", "🚫 SELL", "⚠️ SIGNAL SELL", "📈 PARTIAL", "🔼 PARTIAL", "Ошибка", "📊", "Бот запущен"]):
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
    atr_now = last["atr"]
    atr_avg = df["atr"].rolling(20).mean().iloc[-1]

    if atr_now < atr_avg * 0.5:
        return "none", atr_now

    vol_avg = df["vol"].rolling(20).mean().iloc[-1]
    bid, ask = get_orderbook(sym)
    bid_strength = bid / (ask + 1e-9)

    if last["ema9"] > last["ema21"] and bid_strength > 1.0 and last["vol"] > vol_avg * 1.2 and last["rsi"] > 50:
        return "strong_buy", atr_now
    if last["ema9"] > last["ema21"] and bid_strength > 0.92 and last["vol"] > vol_avg * 1.05 and last["rsi"] > 45:
        return "weak_buy", atr_now
    if last["ema9"] < last["ema21"] or bid_strength < 0.85:
        return "sell", atr_now
    return "none", atr_now
def trade():
    global LAST_REPORT_DATE, AGGRESSIVE_MODE
    usdt = get_balance()
    now = datetime.datetime.now()
    active_positions = sum(len(state["positions"]) for state in STATE.values())
    total_pnl = sum(v["pnl"] for v in STATE.values())
    SELL_ONLY = usdt < MIN_BALANCE

    if active_positions == 0 and total_pnl < 20:
        AGGRESSIVE_MODE = True
        log("⚠️ Агрессивный режим: нет активных позиций и низкий доход")
    else:
        AGGRESSIVE_MODE = False

    log(f"💰 Баланс: {usdt:.2f} USDT")

    for sym in SYMBOLS:
        try:
            log(f"[{sym}] 🔍 Проверка")
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ⚠ Нет данных свечей")
                continue

            sig, atr = signal(df, sym)
            log(f"[{sym}] 📶 Сигнал: {sig.upper()} | ATR: {atr:.4f}", force_tg=False)

            price = df["c"].iloc[-1]
            state = STATE[sym]

            allow_reentry = (
                state["last_sell"]
                and (now - state["last_sell"]).seconds < 600
                and state["reentries"] < 2
            )

            if sig in ("strong_buy", "weak_buy") and not SELL_ONLY:
                if state["positions"] and not allow_reentry:
                    log(f"[{sym}] ⛔ Пропуск покупки: уже есть позиция")
                    continue
                order_usdt = max(MIN_ORDER_USDT, usdt / len(SYMBOLS) / 3)
                if sig == "weak_buy":
                    order_usdt *= 0.5
                if usdt - order_usdt < MIN_RESERVE_USDT:
                    log(f"[{sym}] ⏸ Недостаточно средств для покупки с резервом")
                    continue
                qty = get_qty(sym, price, order_usdt)
                if qty == 0:
                    log(f"[{sym}] ❌ Кол-во {qty} — слишком мало")
                    continue
                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                tp1 = price + atr
                tp2 = price + atr * 1.5
                tp3 = price + atr * 2.5
                state["positions"].append({"buy_price": price, "qty": qty, "tp1": tp1, "tp2": tp2, "tp3": tp3})
                state["count"] += 1
                state["bought"] += order_usdt
                if allow_reentry:
                    state["reentries"] += 1
                log(f"✅ BUY {sym} по {price:.4f}, qty={qty}, TP1={tp1:.4f}, TP2={tp2:.4f}", True)
                log_trade(sym, "BUY", price, qty, 0)
            elif sig == "none":
                log(f"[{sym}] ❌ Сигнал 'none' — нет условий для входа")

            new_positions = []
            for pos in state["positions"]:
                b = pos["buy_price"]
                q = pos["qty"]
                tp1, tp2, tp3 = pos["tp1"], pos["tp2"], pos["tp3"]
                sl = b - atr * 1.5
                pnl = (price - b) * q
                commission = price * q * 0.001
                net_pnl = pnl - commission
                sold = False

                if price <= sl or price >= tp3:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    state["sold"] += price * q
                    state["pnl"] += net_pnl
                    log(f"🚫 SELL {sym} по {price:.4f} | PnL: {net_pnl:.4f}", True)
                    log_trade(sym, "SELL", price, q, net_pnl)
                    state["last_sell"] = now
                    state["reentries"] = 0
                    sold = True
                elif price >= tp2:
                    half = round(q * 0.5, DECIMALS[sym])
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(half))
                    state["sold"] += price * half
                    state["pnl"] += (price - b) * half - price * half * 0.001
                    pos["qty"] -= half
                    log(f"📈 PARTIAL SELL-TP2 {sym} по {price:.4f}", True)
                elif price >= tp1:
                    third = round(q * 0.3, DECIMALS[sym])
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(third))
                    state["sold"] += price * third
                    state["pnl"] += (price - b) * third - price * third * 0.001
                    pos["qty"] -= third
                    log(f"🔼 PARTIAL SELL-TP1 {sym} по {price:.4f}", True)
                elif sig == "sell" and pnl > 0:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    state["sold"] += price * q
                    state["pnl"] += net_pnl
                    log(f"⚠️ SIGNAL SELL {sym} по {price:.4f} | PnL: {net_pnl:.4f}", True)
                    log_trade(sym, "SELL", price, q, net_pnl)
                    state["last_sell"] = now
                    state["reentries"] = 0
                    sold = True

                if not sold and pos["qty"] >= 0.0001:
                    new_positions.append(pos)
            state["positions"] = new_positions

        except Exception as e:
            log(f"🛑 Ошибка в {sym}: {e}", True)

    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        report = f"\n📊 Дневной отчёт {now.date()}:\nUSDT: {usdt:.2f}\n"
        for s, v in STATE.items():
            report += f"{s}: сделок={v['count']}, куплено={v['bought']:.2f}, продано={v['sold']:.2f}, PnL={v['pnl']:.2f}\n"
        log(report, True)
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {e}", True)
        time.sleep(60)
