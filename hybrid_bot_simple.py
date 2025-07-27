import os, time, math, logging, datetime, requests, json
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
RESERVE_BALANCE = 1.0
MAX_TRADE_USDT = 35.0
TRAIL_MULTIPLIER = 1.2  # уменьшен для частых TP
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 5        # до 5 усреднений
COMMISSION_RATE = 0.0018
MIN_PROFIT_PCT = 0.002  # 0.2%
MIN_ABSOLUTE_PNL = 0.5
MIN_NET_PROFIT = 0.5    # минимум $0.5 чистой прибыли
STOP_LOSS_PCT = 0.02    # стоп-лосс 2%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]
STATE_FILE = "state.json"

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}
LAST_REPORT_DATE = None
cycle_count = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")

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
        log("✅ Loaded state file", True)
    except:
        STATE = {}
        log("ℹ No valid state file — starting fresh", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [], "pnl": 0.0,
            "count": 0, "avg_count": 0,
            "last_sell_price": 0.0, "max_drawdown": 0.0
        })

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
    info = f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}"
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], info
    elif last["ema9"] < last["ema21"] and last["rsi"] < 50 and last["macd"] < last["macd_signal"]:
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
    limits = LIMITS.get(sym, {})
    if not limits: return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, limits["qty_step"])
    if q < limits["min_qty"] or q * price < limits["min_amt"]:
        return 0.0
    return q

def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty: continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(sym)
        if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
            qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
            atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
            tp = price + TRAIL_MULTIPLIER * atr
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
    save_state()

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt = get_balance()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            sig, atr, info_ind = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price

            if state["positions"]:
                avg_entry = sum(p["buy_price"]*p["qty"] for p in state["positions"]) / sum(p["qty"] for p in state["positions"])
                dd = (avg_entry - price) / avg_entry
                state["max_drawdown"] = max(state["max_drawdown"], dd)

            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], adjust_qty(pos["qty"], LIMITS[sym]["qty_step"]), pos["tp"]
                commission = price * q * COMMISSION_RATE
                pnl = (price - b) * q - commission
                min_req = max(price*q*MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL, MIN_NET_PROFIT)

                logging.info(f"[{sym}] DEBUG price={price:.4f}, qty={q:.4f}, pnl={pnl:.4f}, min_req={min_req:.4f}")

                if price <= b * (1 - STOP_LOSS_PCT):
                    if pnl >= MIN_NET_PROFIT:
                        session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                        log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop-loss")
                        state["pnl"] += pnl
                        state["last_sell_price"] = price
                    continue

                if price >= tp and pnl >= min_req:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, "take-profit")
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                else:
                    pos["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                    new_positions.append(pos)

            state["positions"] = new_positions

            if sig == "buy":
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty"] for p in state["positions"])
                    avg_price = sum(p["qty"]*p["buy_price"] for p in state["positions"]) / total_q
                    dd = (price - avg_price) / avg_price
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN and value < per_sym:
                        qty = get_qty(sym, price, per_sym - value)
                        if qty and qty * price <= usdt:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                            state["count"] += 1
                            state["avg_count"] += 1
                            log_trade(sym, "BUY (avg)", price, qty, 0.0, f"drawdown={dd:.4f}")
                elif not state["positions"] and value < per_sym:
                    if abs(price - state["last_sell_price"]) / price < 0.001:
                        pass
                    else:
                        qty = get_qty(sym, price, per_sym)
                        if qty and qty * price <= usdt:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                            state["count"] += 1
                            log_trade(sym, "BUY", price, qty, 0.0, info_ind)
        except Exception as e:
            log(f"[{sym}] Error: {e}", True)

        cycle_count += 1

if __name__ == "__main__":
    log("🚀 Bot started", True)
    init_state()
    ensure_state_consistency()
    save_state()
    load_symbol_limits()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"Global error: {e}", True)
        time.sleep(60)
