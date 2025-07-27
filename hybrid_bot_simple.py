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

CONFIG_PATH = "config.json"
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

TG_VERBOSE = config.get("tg_verbose", True)
RESERVE_BALANCE = config.get("reserve_balance", 1.0)
MAX_TRADE_USDT = config.get("max_trade_usdt", 35.0)
TRAIL_MULTIPLIER = config.get("trail_multiplier", 1.5)
MAX_DRAWDOWN = config.get("max_drawdown", 0.10)
MAX_AVERAGES = config.get("max_averages", 3)
MIN_PROFIT_PCT = config.get("min_profit_pct", 0.005)
MIN_ABSOLUTE_PNL = config.get("min_absolute_pnl", 3.0)
MIN_NET_PROFIT = config.get("min_net_profit", 1.5)
STOP_LOSS_PCT = config.get("stop_loss_pct", 0.03)
MIN_VOLUME_USDT = config.get("min_volume_usdt", 1000000)

SYMBOLS = config.get("symbols", ["TONUSDT", "DOGEUSDT", "XRPUSDT"])

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
    except:
        logging.error("Telegram send failed")

def log(msg, tg=False):
    logging.info(msg)
    if tg and TG_VERBOSE:
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

def get_kline(sym, interval="1"):
    r = session.get_kline(category="spot", symbol=sym, interval=interval, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df1m, df5m):
    if df1m.empty or df5m.empty or len(df1m) < 50 or len(df5m) < 50:
        return "none", 0, ""

    def enrich(df):
        df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
        df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
        df["rsi"] = RSIIndicator(df["c"], 14).rsi()
        macd = MACD(close=df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
        return df

    df1m = enrich(df1m)
    df5m = enrich(df5m)

    last1 = df1m.iloc[-1]
    last5 = df5m.iloc[-1]

    info = (f"1m:EMA9={last1['ema9']:.4f},EMA21={last1['ema21']:.4f},RSI={last1['rsi']:.2f},MACD={last1['macd']:.4f},SIG={last1['macd_signal']:.4f} | "
            f"5m:EMA9={last5['ema9']:.4f},EMA21={last5['ema21']:.4f},RSI={last5['rsi']:.2f},MACD={last5['macd']:.4f},SIG={last5['macd_signal']:.4f}")

    cond1 = last1["ema9"] > last1["ema21"] and last1["rsi"] > 50 and last1["macd"] > last1["macd_signal"]
    cond2 = last5["ema9"] > last5["ema21"] and last5["rsi"] > 50 and last5["macd"] > last5["macd_signal"]

    if cond1 and cond2:
        return "buy", last1["atr"], info
    elif not cond1 and not cond2:
        return "sell", last1["atr"], info
    return "none", last1["atr"], info

# фильтрация по объёму
filtered = []
for s in SYMBOLS:
    try:
        vol = float(session.get_kline(category="spot", symbol=s, interval="1", limit=1)["result"]["list"][-1][6])
        if vol >= MIN_VOLUME_USDT:
            filtered.append(s)
    except:
        continue
SYMBOLS = filtered
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
            log(f"[{sym}] Recovered pos qty={qty}, price={price:.4f}, tp={tp:.4f}", False)
    save_state()

def send_daily_report():
    report = f"📊 Daily Report {datetime.datetime.now().date()}\n\n"
    report += "Symbol | Trades | PnL | Max Drawdown | Current Pos\n"
    report += "-------|--------|-----|---------------|-------------\n"
    for sym in SYMBOLS:
        s = STATE[sym]
        cur_pos = sum(p["qty"] for p in s["positions"])
        dd = s.get("max_drawdown", 0.0)
        report += f"{sym:<7}|{s['count']:>6}  |{s['pnl']:>6.2f}|{dd*100:>9.2f}%   |{cur_pos:.4f}\n"
    send_tg(report)

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt = get_balance()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df1 = get_kline(sym, "1")
            df5 = get_kline(sym, "5")
            sig, atr, info_ind = signal(df1, df5)
            price = df1["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price

            logging.info(f"[{sym}] sig={sig}, price={price:.4f}, value={value:.2f}, pos={len(state['positions'])}, {info_ind}")

            if state["positions"]:
                avg_entry = sum(p["buy_price"] * p["qty"] for p in state["positions"]) / sum(p["qty"] for p in state["positions"])
                curr_dd = (avg_entry - price) / avg_entry
                if curr_dd > state["max_drawdown"]:
                    state["max_drawdown"] = curr_dd

            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], adjust_qty(pos["qty"], limits["qty_step"]), pos["tp"]
                commission = price * q * 0.0025
                pnl = (price - b) * q - commission
                min_req = max(price * q * MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL, MIN_NET_PROFIT)

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
                    avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_q
                    drawdown = (price - avg_price) / avg_price
                    if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym:
                        qty = get_qty(sym, price, per_sym - value)
                        if qty and qty * price <= usdt:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                            state["count"] += 1
                            state["avg_count"] += 1
                            log_trade(sym, "BUY (avg)", price, qty, 0.0, f"drawdown={drawdown:.4f}")
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
