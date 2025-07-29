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
MAX_TRADE_USDT = 70.0
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2       # <= только две покупки: основная + один раз усреднение
MIN_PROFIT_PCT = 0.005
MIN_ABSOLUTE_PNL = 3.0
MIN_NET_PROFIT = 1.50
STOP_LOSS_PCT = 0.02   # <= стоп‑лосс теперь 2%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
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
    if TG_VERBOSE:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except:
            logging.error("Telegram send failed")

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
    except:
        STATE = {}
        log("ℹ No valid state file — starting fresh", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [], "pnl": 0.0,
            "count": 0, "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0
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

    ema_up = last["ema9"] > last["ema21"]
    rsi_up = last["rsi"] > 50
    macd_up = last["macd"] > last["macd_signal"]
    ema_dn = last["ema9"] < last["ema21"]
    rsi_dn = last["rsi"] < 50
    macd_dn = last["macd"] < last["macd_signal"]

    buy_count = sum([ema_up, rsi_up, macd_up])
    sell_count = sum([ema_dn, rsi_dn, macd_dn])
    info = (f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}")

    if buy_count >= 2:
        return "buy", last["atr"], info
    if sell_count >= 2:
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
    co = sym.replace("USDT", "")
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next((c["walletBalance"] for c in coins if c["coin"] == co), 0.0))

def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

def hours_since(dt_str):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(dt_str)).total_seconds() / 3600
    except:
        return 100.0

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
            STATE[sym]["positions"].append({
                "buy_price": price,
                "qty": qty,
                "tp": tp,
                "timestamp": datetime.datetime.now().isoformat()
            })
            log(f"[{sym}] Recovered pos qty={qty}, price={price:.4f}, tp={tp:.4f}", False)
    save_state()

def send_daily_report():
    report = f"📊 Daily Report {datetime.datetime.now().date()}\n\n"
    report += "Symbol | Trades | PnL | Max Drawdown | Current Pos\n"
    report += "-------|--------|-----|---------------|-------------\n"
    for sym in SYMBOLS:
        s = STATE[sym]
        cur = sum(p["qty"] for p in s["positions"])
        dd = s.get("max_drawdown", 0.0)
        report += f"{sym:<7}|{s['count']:>6}  |{s['pnl']:>6.2f}|{dd*100:>9.2f}%   |{cur:.4f}\n"
    send_tg(report)

def trade():
    global LAST_REPORT_DATE, cycle_count
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

            logging.info(f"[{sym}] sig={sig}, price={price:.4f}, value={value:.2f}, pos={len(state['positions'])}, {info_ind}")

            if state["positions"]:
                avg_entry = sum(p["buy_price"]*p["qty"] for p in state["positions"])/sum(p["qty"] for p in state["positions"])
                curr_dd = (avg_entry - price)/avg_entry
                if curr_dd > state["max_drawdown"]:
                    state["max_drawdown"] = curr_dd

            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], adjust_qty(pos["qty"], limits["qty_step"]), pos["tp"]
                commission = price*q*0.0025
                pnl = (price - b)*q - commission

                if price <= b * (1 - STOP_LOSS_PCT):
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop‑loss")
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    continue

                if price >= tp and pnl >= max(price*q*MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL, MIN_NET_PROFIT):
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, "take‑profit")
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                else:
                    pos["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                    new_positions.append(pos)

            state["positions"] = new_positions

            if sig == "buy":
                if len(state["positions"]) > 0 and state["avg_count"] < MAX_AVERAGES:
                    first_ts = state["positions"][0].get("timestamp", "")
                    hours = hours_since(first_ts)
                    total_q = sum(p["qty"] for p in state["positions"])
                    avg_price = sum(p["qty"]*p["buy_price"] for p in state["positions"]) / total_q
                    drawdown = (price - avg_price) / avg_price
                    if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym and hours >= 4:
                        qty = get_qty(sym, price, per_sym - value)
                        if qty and qty * price <= usdt:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({
                                "buy_price": price,
                                "qty": qty,
                                "tp": tp,
                                "timestamp": datetime.datetime.now().isoformat()
                            })
                            state["count"] += 1
                            state["avg_count"] += 1
                            log_trade(sym, "BUY (avg)", price, qty, 0.0, f"drawdown={drawdown:.4f}")
                elif len(state["positions"]) == 0 and value < per_sym:
                    if abs(price - state["last_sell_price"]) / price < 0.001:
                        pass
                    else:
                        qty = get_qty(sym, price, per_sym)
                        if qty and qty * price <= usdt:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({
                                "buy_price": price,
                                "qty": qty,
                                "tp": tp,
                                "timestamp": datetime.datetime.now().isoformat()
                            })
                            state["count"] += 1
                            log_trade(sym, "BUY", price, qty, 0.0, info_ind)
        except Exception as e:
            log(f"[{sym}] Error: {e}", True)

    cycle_count += 1
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

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
