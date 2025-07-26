import os, time, math, logging, datetime, requests, json
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# === НАСТРОЙКИ ===
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TG_VERBOSE = True
RESERVE_BALANCE = 1.0
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 3
MIN_PROFIT_PCT = 0.005
MIN_ABSOLUTE_PNL = 3.0
MIN_NET_PROFIT = 1.50
STOP_LOSS_PCT = 0.03
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]
STATE_FILE = "state.json"

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS, LAST_REPORT_DATE = {}, None
cycle_count = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error(f"Tg send error: {e}")

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(STATE, f, indent=2)

def init_state():
    global STATE
    try:
        with open(STATE_FILE, "r") as f:
            STATE = json.load(f)
        log("✅ Loaded state from file", True)
    except (FileNotFoundError, json.JSONDecodeError):
        STATE = {}
        log("ℹ No valid state file — starting fresh", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        if sym not in STATE:
            STATE[sym] = {
                "positions": [],
                "pnl": 0.0,
                "count": 0,
                "avg_count": 0,
                "last_sell_price": 0.0
            }

def log_trade(sym, side, price, qty, pnl, info=""):
    usdt_val = price * qty
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, USDT≈{usdt_val:.2f}, PnL={pnl:.2f}. {info}"
    log(msg, True)
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt_val},{pnl},{info}\n")
    save_state()

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
        return "none", 0, ""
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}")
    if last["ema9"] > last["ema21"] and last["rsi"] < 20 and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], info
    elif last["ema9"] < last["ema21"] and last["rsi"] > 80 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"], info
    return "none", last["atr"], info

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
    return float(next((c["walletBalance"] for c in coins if c["coin"] == coin), 0.0))

def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0.0
    q = adjust_qty(usdt / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(sym)
        if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
            qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
            atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
            tp = price + TRAIL_MULTIPLIER * atr
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
            log(f"[{sym}] Recovered pos: qty={qty}, price={price:.4f}, tp={tp:.4f}", True)
    save_state()

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt = get_balance()
    log(f"USDT Balance: {usdt:.2f}")
    avail = max(0, usdt - RESERVE_BALANCE)
    if avail < min(v["min_amt"] for v in LIMITS.values()):
        log("Недостаточно USDT — продаю активы для пополнения...")
        for sym in SYMBOLS:
            bc = get_coin_balance(sym)
            price = get_kline(sym)["c"].iloc[-1]
            if bc * price >= LIMITS[sym]["min_amt"]:
                qty = adjust_qty(bc, LIMITS[sym]["qty_step"])
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                log_trade(sym, "SELL for USDT", price, qty, 0.0, "replenishing USDT")
        usdt = get_balance()
        avail = max(0, usdt - RESERVE_BALANCE)

    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue
            sig, atr, info_ind = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price
            log(f"[{sym}] price={price:.4f}, sig={sig}, atr={atr:.4f}, {info_ind}")

            new_positions = []
            for pos in state["positions"]:
                b = pos["buy_price"]
                q = adjust_qty(pos["qty"], limits["qty_step"])
                tp = pos["tp"]
                commission = price * q * 0.0025
                pnl = (price - b) * q - commission
                min_req = max(price * q * MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL, MIN_NET_PROFIT)

                if price <= b * (1 - STOP_LOSS_PCT):
                    if pnl >= MIN_NET_PROFIT:
                        session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                        log_trade(sym, "STOP LOSS SELL", price, q, pnl, f"stop-loss hit, pnl≥{MIN_NET_PROFIT}")
                        state["pnl"] += pnl
                        state["last_sell_price"] = price
                    else:
                        log(f"[{sym}] Stop-loss skipped: net pnl {pnl:.2f} < {MIN_NET_PROFIT}")
                    continue

                if price >= tp and pnl >= min_req:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, f"tp hit, pnl≥min_req")
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                else:
                    new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                    pos["tp"] = new_tp
                    log(f"[{sym}] Trailing TP updated {tp:.4f} → {new_tp:.4f}")
                    new_positions.append(pos)

            state["positions"] = new_positions

            if sig == "buy" and state["positions"] and state["avg_count"] < MAX_AVERAGES:
                total_q = sum(p["qty"] for p in state["positions"])
                avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_q
                drawdown = (price - avg_price) / avg_price
                if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym:
                    usdt_to_use = per_sym - value
                    qty = get_qty(sym, price, usdt_to_use)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        state["avg_count"] += 1
                        log_trade(sym, "BUY (avg)", price, qty, 0.0, f"drawdown={drawdown:.4f}")

            if sig == "buy" and not state["positions"] and value < per_sym:
                if abs(price - state["last_sell_price"]) / price < 0.001:
                    log(f"[{sym}] Initial buy skipped: price≈last sell")
                else:
                    usdt_to_use = per_sym - value
                    qty = get_qty(sym, price, usdt_to_use)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        log_trade(sym, "BUY", price, qty, 0.0, info_ind)

            if not state["positions"] and sig == "sell" and coin_bal * price >= limits["min_amt"]:
                log(f"[{sym}] Sell signal, но позиции нет – пропуск")

        except Exception as e:
            log(f"[{sym}] Ошибка: {e}")

    cycle_count += 1
    if cycle_count % 3 == 0:
        act = sum(len(v["positions"]) for v in STATE.values())
        log(f"Активных позиций: {act}")

    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        rep = f"📊 Ежедневный отчёт {now.date()}:\nUSDT balance: {get_balance():.2f}\n"
        for s, v in STATE.items():
            rep += f"{s:<8} | Trades: {v['count']} | PnL: {v['pnl']:.2f}\n"
        log(rep, True)
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    init_state()
    ensure_state_consistency()
    save_state()
    load_symbol_limits()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"Глобальная ошибка: {e}", True)
        time.sleep(60)
