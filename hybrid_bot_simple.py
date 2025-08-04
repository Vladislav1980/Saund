import os, time, math, logging, datetime, json, requests, pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

# Загрузка настроек
load_dotenv()
API_KEY, API_SECRET = os.getenv("BYBIT_API_KEY"), os.getenv("BYBIT_API_SECRET")
TG_TOKEN, CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

# Параметры стратегии
TG_VERBOSE = True
RESERVE_BALANCE = 1.0
MAX_TRADE_USDT = 70.0
MIN_PRICE_DIFF_PCT = 0.003
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2
MIN_NET_PROFIT = 1.50
STOP_LOSS_PCT = 0.008
TAKE_PROFIT_PCT = 0.02
TAKER_FEE = 0.0018  # 0.18%
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

# Инициализация
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}
LAST_REPORT_DATE = None
cycle_count = 0

# Логирование
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def send_tg(msg):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                           data={"chat_id": CHAT_ID, "text": msg})
        except: logging.error("Telegram send failed")

def log(msg, tg=False):
    logging.info(msg)
    if tg: send_tg(msg)

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    if STATE:
        log("✅ Loaded state from Redis", True)
    else:
        log("ℹ Starting fresh state", True)
    ensure_state_consistency()

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {"positions": [], "pnl": 0.0, "count": 0,
                               "avg_count": 0, "last_sell_price": 0.0,
                               "max_drawdown": 0.0, "last_stop_time": ""})

def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    return {item["symbol"]: {"min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0)),
                             "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", 1.0)),
                             "min_amt": float(item.get("minOrderAmt", 10.0))}
            for item in data if item["symbol"] in SYMBOLS}

LIMITS = load_symbol_limits()

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / (10**abs(exponent))
    except: return qty

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.4f},{qty},{pnl:.2f},{info}\n")
    save_state()

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, ""
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    cnt_buy = sum([last["ema9"] > last["ema21"],
                   last["rsi"] > 50,
                   last["macd"] > last["macd_signal"]])
    cnt_sell = sum([last["ema9"] < last["ema21"],
                    last["rsi"] < 50,
                    last["macd"] < last["macd_signal"]])
    info = f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}"
    return ("buy", last["atr"], info) if cnt_buy >= 2 else ("sell", last["atr"], info) if cnt_sell >= 2 else ("none", last["atr"], info)

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
    return q if q >= LIMITS[sym]["min_qty"] and q * price >= LIMITS[sym]["min_amt"] else 0.0

def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999

def init_positions():
    total = 0.0; msgs = []
    for sym in SYMBOLS:
        bal = get_coin_balance(sym)
        df = get_kline(sym)
        if bal > 0 and df is not None and not df.empty:
            price = df["c"].iloc[-1]
            if bal * price >= LIMITS[sym]["min_amt"]:
                qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                tp = price + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp,
                                                "timestamp": datetime.datetime.now().isoformat()})
                total += qty * price
                msgs.append(f"- {sym}: {qty} @ {price:.4f} (${qty*price:.2f})")
    save_state()
    header = "🚀 Bot started"
    if msgs:
        header += "\n💰 Активные позиции:\n" + "\n".join(msgs) + f"\n📊 Итого: ${total:.2f}"
    else:
        header += "\n💰 Активных позиций нет"
    log(header, True)

def trade():
    global cycle_count, LAST_REPORT_DATE
    usdt = get_balance(); avail = max(0, usdt - RESERVE_BALANCE); per_sym = avail / len(SYMBOLS)
    for sym in SYMBOLS:
        st = STATE[sym]; st["sell_failed"] = False
        df = get_kline(sym); 
        if df.empty: continue
        sig, atr, info = signal(df); price = df["c"].iloc[-1]
        coin_bal = get_coin_balance(sym); value = coin_bal * price
        logging.info(f"[{sym}] sig={sig}, price={price:.4f}, value={value:.2f}, pos={len(st['positions'])}, {info}")
        new_positions = []
        for pos in st["positions"]:
            b, q = pos["buy_price"], pos["qty"]
            buy_comm = b * q * TAKER_FEE; sell_comm = price * q * TAKER_FEE
            pnl = (price - b) * q - (buy_comm + sell_comm)
            if q > 0 and price <= b * (1 - STOP_LOSS_PCT) and abs(pnl) >= MIN_NET_PROFIT:
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop-loss")
                    st["pnl"] += pnl
                else:
                    log(f"SKIPPED STOP SELL {sym}: insufficient balance", True)
                    st["sell_failed"] = True
                st["last_stop_time"] = datetime.datetime.now().isoformat(); st["avg_count"] = 0
                continue
            if q > 0 and price >= b * (1 + TAKE_PROFIT_PCT) and pnl >= MIN_NET_PROFIT:
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, "take-profit")
                    st["pnl"] += pnl
                st["positions"] = []; st["avg_count"] = 0; st["sell_failed"] = False; st["last_sell_price"] = price
                break
            pos["tp"] = max(pos["tp"], price + TRAIL_MULTIPLIER * atr)
            new_positions.append(pos)
        st["positions"] = [] if st.get("sell_failed") else new_positions

    cycle_count += 1
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_tg("📊 Daily report placeholder")
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    init_state()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"Global error: {e}", True)
        time.sleep(60)
