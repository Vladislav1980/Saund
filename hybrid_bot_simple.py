import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# --- Конфигурация ---
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = [
    "ARBUSDT", "TRXUSDT", "DOGEUSDT",
    "NEARUSDT", "COMPUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "SUIUSDT", "FILUSDT"
]

DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usdt": 2.5,
    "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600
}
RESERVE_BALANCE = 500
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 50

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": str(msg)})
    except Exception as e:
        log(f"Telegram error: {e}")

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}

def load_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {
                "step": float(f.get("qtyStep", 1)),
                "min_amt": float(it.get("minOrderAmt", 10)),
                "max_amt": float(it.get("maxOrderAmt", 999999))
            }

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10 ** abs(e)) / 10 ** abs(e)

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except: pass
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except: pass
    return 0

def get_klines(sym, interval="5", limit=100):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval=interval, limit=limit)
        return pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
    except Exception as e:
        log(f"klines err {sym}@{interval}: {e}")
        return pd.DataFrame()

def signal(df):
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

def check_trend(df):
    last = df.iloc[-1]
    return last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"], last

STATE = {}
if os.path.exists("state.json"):
    try: STATE = json.load(open("state.json"))
    except: STATE = {}

for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "last_rebuy": 0, "count": 0, "pnl": 0.0})

def save_state():
    json.dump(STATE, open("state.json", "w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена: reserve/лимит убытка")
        return

    load_limits()
    now = time.time()
    alloc = bal / len(SYMBOLS)

    for sym in SYMBOLS:
        df5 = get_klines(sym, "5")
        if df5.empty: continue
        df5 = signal(df5)
        last5 = df5.iloc[-1]

        df15 = signal(get_klines(sym, "15"))
        df60 = signal(get_klines(sym, "60"))
        df240 = signal(get_klines(sym, "240"))

        if df15.empty or df60.empty or df240.empty: continue

        mtf_ok = True
        for tf_df in [df15, df60, df240]:
            tf_ok, _ = check_trend(tf_df)
            if not tf_ok: mtf_ok = False

        signal_type = "none"
        if last5["ema9"] > last5["ema21"] and last5["macd"] > last5["macd_s"]:
            signal_type = "buy" if mtf_ok else "none"

        price = last5["c"]
        atr = last5["atr"]
        pos = STATE[sym]["pos"]

        # Покупка
        if signal_type == "buy" and not pos:
            qty_usd = min(alloc * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
            qty = adjust(qty_usd / price, LIMITS[sym]["step"])
            if qty * price < LIMITS[sym]["min_amt"]: continue
            if qty * price > LIMITS[sym]["max_amt"]: continue
            est_profit = atr * DEFAULT_PARAMS["tp_multiplier"] * qty - price * qty * 0.001 - DEFAULT_PARAMS["min_profit_usdt"]
            if est_profit < 0: continue

            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
            tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
            STATE[sym]["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price, "time": now}
            msg = f"✅ BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
            log(msg); send_tg(msg)

        # Продажа
        cb = get_coin_balance(sym)
        if cb > 0 and pos:
            b, q, tp, peak = pos["buy_price"], pos["qty"], pos["tp"], pos["peak"]
            pnl = (price - b) * q - price * q * 0.001
            peak = max(peak, price)
            conds = {
                "TP": price >= tp,
                "RSI": last5["rsi"] >= 80 and pnl > 0,
                "Trail": price <= peak * (1 - DEFAULT_PARAMS["trailing_stop_pct"]) and pnl > 0,
                "SL": (b - price) / b >= DEFAULT_PARAMS["max_drawdown_sl"]
            }
            reason = next((k for k, v in conds.items() if v), None)
            if reason:
                qty = adjust(cb, LIMITS[sym]["step"])
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                msg = f"✅ SELL {reason} {sym}@{price:.4f}, qty={qty:.6f}, pnl={pnl:.2f} USDT"
                log(msg); send_tg(msg)
                STATE[sym]["pnl"] += pnl
                STATE[sym]["count"] += 1
                STATE[sym]["pos"] = None
                break  # ⛔️ Останавливаем цикл, только 1 продажа за тик
            else:
                pos.update({"tp": max(tp, price + DEFAULT_PARAMS["tp_multiplier"] * atr), "peak": peak})

    save_state()

def daily_report():
    d = datetime.datetime.now()
    if d.hour == 22:
        rep = "📊 Ежедневный отчет\n"
        for s in SYMBOLS:
            rep += f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}\n"
        rep += f"Баланс={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = 0
            STATE[s]["pnl"] = 0.0
        save_state()

def main():
    log("🚀 Bot старт — финальная версия")
    send_tg("🚀 Bot старт: все фильтры, SL/TP/Trailing, лимиты, логика активны")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
