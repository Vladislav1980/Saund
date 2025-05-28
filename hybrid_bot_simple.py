import os, time, math, logging, datetime, requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TG_VERBOSE = True
RESERVE_BALANCE = 500
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 3
MIN_PROFIT_PCT = 0.001

SYMBOLS = [
    "COMPUSDT", "NEARUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT"
]

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
STATE = {s: {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0} for s in SYMBOLS}
LIMITS, LAST_REPORT_DATE = {}, None
cycle_count = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def send_tg(msg):
    if TG_VERBOSE:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
        except:
            pass

def log(msg, tg=False):
    logging.info(msg)
    if tg and TG_VERBOSE:
        send_tg(msg)

def log_trade(sym, side, price, qty, pnl):
    log(f"{side} {sym} @ {price:.4f}, qty={qty}, PnL={pnl:.4f}", True)
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{pnl}\n")

def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        if item["symbol"] in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[item["symbol"]] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0))
            }

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except:
        return qty

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 14).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    vol_spike = last["vol"] > df["vol"].rolling(20).mean().iloc[-1] * 1.2
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 and last["macd"] > last["macd_signal"] and vol_spike:
        return "buy", last["atr"]
    elif last["ema9"] < last["ema21"] and last["rsi"] < 45 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"]
    return "none", last["atr"]

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balance():
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next((c["walletBalance"] for c in coins if c["coin"] == coin), 0))

def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0
    q = usdt / price
    return adjust_qty(q, LIMITS[sym]["qty_step"])
def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(sym)
        if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
            qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
            tp = price + TRAIL_MULTIPLIER * (df["h"] - df["l"]).mean()
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
            log(f"[{sym}] 🔄 Восстановлена позиция: qty={qty}, price={price:.4f}")

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt = get_balance()
    log(f"💰 Баланс: {usdt:.2f} USDT")
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ❌ Пропущен: пустой график")
                continue

            sig, atr = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price
            new_positions = []

            # ПРОДАЖА
            for p in state["positions"]:
                b, q, tp = p["buy_price"], p["qty"], p["tp"]
                q = adjust_qty(q, limits["qty_step"])
                if q <= 0 or coin_bal < q:
                    continue

                pnl = (price - b) * q - price * q * 0.001
                if price >= tp and pnl >= price * q * MIN_PROFIT_PCT:
                    q = adjust_qty(q, limits["qty_step"])
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl)
                    state["pnl"] += pnl
                    coin_bal -= q
                else:
                    p["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                    new_positions.append(p)

            state["positions"] = new_positions

            # УСРЕДНЕНИЕ
            if sig == "buy" and state["positions"] and state["avg_count"] < MAX_AVERAGES:
                total_qty = sum(p["qty"] for p in state["positions"])
                avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_qty
                drawdown = (price - avg_price) / avg_price
                if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym:
                    usdt_to_use = per_sym - value
                    qty = get_qty(sym, price, usdt_to_use)
                    if qty and qty * price <= usdt:
                        qty = adjust_qty(qty, limits["qty_step"])
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        state["avg_count"] += 1
                        log_trade(sym, "BUY (усреднение)", price, qty, 0)

            # НОВАЯ ПОКУПКА
            if sig == "buy" and not state["positions"] and value < per_sym:
                usdt_to_use = per_sym - value
                qty = get_qty(sym, price, usdt_to_use)
                if qty and qty * price <= usdt:
                    qty = adjust_qty(qty, limits["qty_step"])
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                    tp = price + TRAIL_MULTIPLIER * atr
                    state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                    state["count"] += 1
                    log_trade(sym, "BUY", price, qty, 0)

            # ПРОПУСК ПРОДАЖИ ПО СИГНАЛУ
            if not state["positions"] and sig == "sell":
                log(f"[{sym}] ℹ️ Продажа пропущена: нет позиции")

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка ({type(e).__name__}): {e}")

    cycle_count += 1
    if cycle_count % 3 == 0:
        act = sum(len(v["positions"]) for v in STATE.values())
        log(f"⏳ Активных позиций: {act}")

    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        rep = f"📊 Отчёт {now.date()}:\nБаланс: {usdt:.2f} USDT\n"
        for s, v in STATE.items():
            rep += f"{s:<8} | Сделок: {v['count']} | PnL: {v['pnl']:.2f}\n"
        log(rep, True)
        LAST_REPORT_DATE = now.date()
if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    load_symbol_limits()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {type(e).__name__} — {e}")
        time.sleep(60)
