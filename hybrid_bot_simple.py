import os, time, math, logging, datetime, requests, json
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

load_dotenv()
API_KEY, API_SECRET = os.getenv("BYBIT_API_KEY"), os.getenv("BYBIT_API_SECRET")
TG_TOKEN, CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("CHAT_ID")

TG_VERBOSE = True
RESERVE_BALANCE = 1.0
MAX_TRADE_USDT = 70.0
MIN_PRICE_DIFF_PCT = 0.003
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2
MIN_PROFIT_PCT = 0.005
MIN_ABSOLUTE_PNL = 3.0
MIN_NET_PROFIT = 1.50
STOP_LOSS_PCT = 0.008   # 0.8%
TAKE_PROFIT_PCT = 0.02  # 2%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
STATE_FILE = "state.json"

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}
STATE = {}
LAST_REPORT_DATE = None
cycle_count = 0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def send_tg(msg):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
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
        STATE = json.load(open(STATE_FILE))
        log("✅ Loaded state from file", True)
    except:
        STATE = {}
        log("ℹ Starting fresh", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [], "pnl": 0.0, "count": 0,
            "avg_count": 0, "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log(msg, True)
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{pnl},{info}\n")
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
    buy_cnt = sum([last["ema9"] > last["ema21"], last["rsi"] > 50,
                   last["macd"] > last["macd_signal"]])
    sell_cnt = sum([last["ema9"] < last["ema21"], last["rsi"] < 50,
                    last["macd"] < last["macd_signal"]])
    info = (f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}")
    if buy_cnt >= 2:
        return "buy", last["atr"], info
    if sell_cnt >= 2:
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
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp,
                                            "timestamp": datetime.datetime.now().isoformat()})
    save_state()

def trade():
    global cycle_count, LAST_REPORT_DATE
    usdt = get_balance()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        state = STATE[sym]
        state['sell_failed'] = False
        df = get_kline(sym)
        if df.empty:
            continue
        sig, atr, info_ind = signal(df)
        price = df['c'].iloc[-1]
        coin_bal = get_coin_balance(sym)
        value = coin_bal * price
        logging.info(f"[{sym}] sig={sig}, price={price:.4f}, value={value:.2f}, pos={len(state['positions'])}, {info_ind}")

        new_positions = []
        for pos in state['positions']:
            b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
            commission = price * q * 0.0025
            pnl = (price - b) * q - commission

            if price <= b * (1 - STOP_LOSS_PCT):
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop‑loss")
                    state['pnl'] += pnl
                else:
                    log(f"SKIPPED STOP SELL {sym}: insufficient balance", True)
                    state['sell_failed'] = True
                state['last_stop_time'] = datetime.datetime.now().isoformat()
                state['avg_count'] = 0
                continue

            if price >= b * (1 + TAKE_PROFIT_PCT):
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, "take‑profit")
                    state['pnl'] += pnl
                new_positions = []
                state['avg_count'] = 0
                state['sell_failed'] = False
                break

            pos["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
            new_positions.append(pos)

        state["positions"] = [] if state.get('sell_failed') else new_positions

        if sig == "buy":
            last_stop = state.get("last_stop_time", "")
            if last_stop and hours_since(last_stop) < 4:
                log(f"[{sym}] Skipped BUY: only {hours_since(last_stop):.2f}h since stop-loss", False)
                continue

            if state['positions'] and state['avg_count'] < MAX_AVERAGES:
                first_ts = state['positions'][0].get("timestamp", "")
                hours = hours_since(first_ts)
                last_price = state['positions'][-1]["buy_price"]
                total_q = sum(p["qty"] for p in state['positions'])
                avg_price = sum(p["qty"] * p["buy_price"] for p in state['positions']) / total_q
                drawdown = (price - avg_price) / avg_price
                price_diff = abs(price - last_price) / last_price

                if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym and hours >= 4 and price_diff >= MIN_PRICE_DIFF_PCT:
                    qty = get_qty(sym, price, per_sym - value)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp,
                                                        "timestamp": datetime.datetime.now().isoformat()})
                        state['count'] += 1
                        state['avg_count'] += 1
                        log_trade(sym, "BUY (avg)", price, qty, 0.0,
                                  f"drawdown={drawdown:.4f}, pr diff={price_diff:.4f}")
            elif not state['positions'] and value < per_sym:
                if abs(price - state['last_sell_price']) / price >= 0.001:
                    qty = get_qty(sym, price, per_sym)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp,
                                                        "timestamp": datetime.datetime.now().isoformat()})
                        state['count'] += 1
                        log_trade(sym, "BUY", price, qty, 0.0, info_ind)

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
