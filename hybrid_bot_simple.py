import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# --- Список монет ---
SYMBOLS = [
    "ARBUSDT", "TRXUSDT", "DOGEUSDT", "MASKUSDT",
    "NEARUSDT", "COMPUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "SUIUSDT", "FILUSDT"
]

# --- Конфигурация ---
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DEFAULT_PARAMS = {
    "risk_pct": 0.03, "tp_multiplier": 1.8, "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06, "min_profit_usdt": 2.5, "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07, "rebuy_cooldown_secs": 3600
}
RESERVE_BALANCE = 500
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": str(msg)})
    except Exception as e:
        log(f"Telegram error: {e}")

LIMITS = {}
def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {"step": float(f.get("qtyStep", 1)), "min_amt": float(it.get("minOrderAmt", 10))}

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except Exception as e:
        log(f"balance err {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except:
        pass
    return 0

def get_klines(sym, interval="5", limit=100):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval=interval, limit=limit)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except Exception as e:
        log(f"klines err {sym}@{interval}: {e}")
        return pd.DataFrame()

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10 ** abs(e)) / 10 ** abs(e)

def signal(df):
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd(); df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

def check_trend(df):
    last = df.iloc[-1]
    return last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"], last

STATE = {}
if os.path.exists("state.json"):
    try:
        STATE = json.load(open("state.json"))
    except:
        STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {
        "pos": None,
        "last_rebuy": 0,
        "count": 0,
        "pnl": 0.0,
        "last_sig": None,
        "last_summary": None
    })
def save_state(): json.dump(STATE, open("state.json", "w"), indent=2)

def trade():
    bal = get_balance(); log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена: reserve/лимит убытка")
        return

    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        df5 = get_klines(sym, "5"); df15 = get_klines(sym, "15")
        if df5.empty or df15.empty:
            if STATE[sym]["last_summary"] != "нет данных":
                log(f"{sym} → Пропуск: нет данных")
                STATE[sym]["last_summary"] = "нет данных"
            continue

        df5 = signal(df5); df15 = signal(df15)
        last5 = df5.iloc[-1]; price = last5["c"]; atr = last5["atr"]
        cb = get_coin_balance(sym); sig = "none"; summary = "—"

        # 5m-сигналы + объём
        vol_ch = last5["vol_ch"]
        if vol_ch < -DEFAULT_PARAMS["volume_filter"]:
            if last5["rsi"] < 30 or last5["macd"] > last5["macd_s"]:
                if last5["ema9"] > last5["ema21"] and last5["macd"] > last5["macd_s"]:
                    sig = "buy"
                elif last5["ema9"] < last5["ema21"] and last5["macd"] < last5["macd_s"]:
                    sig = "sell"
                summary = f"Пропуск: слабый объем, сиг={sig}"
            else:
                summary = "Пропуск: слабый объем без сигнала"
        else:
            if last5["ema9"] > last5["ema21"] and last5["macd"] > last5["macd_s"]:
                sig = "buy"
            elif last5["ema9"] < last5["ema21"] and last5["macd"] < last5["macd_s"]:
                sig = "sell"
            else:
                summary = "Пропуск: нет сигнала"

        # MTF-фильтр с suppression
        mts = {"15": df15, "60": get_klines(sym, "60"), "240": get_klines(sym, "240")}
        mt_ok = True; mt_states = []
        for tf, df in mts.items():
            if df.empty:
                mt_ok = False; mt_states.append(f"{tf}:no")
                continue
            df2 = signal(df); ok, last = check_trend(df2)
            mt_states.append(f"{tf}:{'OK' if ok else 'NO'}")
            if sig == "buy" and not ok: mt_ok = False
        if sig == "buy" and not mt_ok:
            summary = "Пропуск: MTF fail"
            mt_key = "|".join(mt_states)
            if STATE[sym]["last_sig"] != mt_key:
                log(f"{sym} 5m=BUY, но MTF не подтвердил:")
                for s2 in mt_states: log(f"   {s2}")
                STATE[sym]["last_sig"] = mt_key
        else:
            STATE[sym]["last_sig"] = None

        # Покупка
        if sig == "buy" and mt_ok:
            qty_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
            qty = adjust(qty_usd / price, LIMITS[sym]["step"])
            if qty * price >= LIMITS[sym]["min_amt"]:
                estp = atr * DEFAULT_PARAMS["tp_multiplier"] * qty - price * qty * 0.001 - DEFAULT_PARAMS["min_profit_usdt"]
                if estp >= 0:
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                    tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
                    STATE[sym]["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price, "time": now}
                    STATE[sym]["last_manual_buy"] = price
                    log(f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}")
                    send_tg(f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}")
                    summary = "BUY"
                else:
                    summary = "Пропуск: прибыль<min"
            else:
                summary = "Пропуск: min_amt"

        # Продажа
        elif cb > 0 and sym in LIMITS:
            qty = adjust(cb, LIMITS[sym]["step"])
            if qty > 0:
                sell_signal = last5["ema9"] < last5["ema21"] and last5["macd"] < last5["macd_s"]
                lb = STATE[sym].get("last_manual_buy", 0)
                thr_pct = lb * 1.05 if lb else 0
                thr_atr = lb + DEFAULT_PARAMS["tp_multiplier"] * atr if lb else 0
                prof = lb > 0 and price >= max(thr_pct, thr_atr)
                if sell_signal or prof:
                    typ = "SIGNAL" if sell_signal else "PROFIT"
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                    log(f"SELL {typ} {sym}@{price:.4f}, qty={qty:.6f}")
                    send_tg(f"SELL {typ} {sym}@{price:.4f}, qty={qty:.6f}")
                    STATE[sym]["pos"] = None
                    summary = f"SELL {typ}"

        if STATE[sym]["last_summary"] != summary:
            log(f"{sym} → {summary}")
            STATE[sym]["last_summary"] = summary

    save_state()

def daily_report():
    now = datetime.datetime.now(); today = now.date()
    fn = "last_report.txt"; prev = None
    if os.path.exists(fn):
        with open(fn) as f: prev = f.read().strip()
    if str(today) != prev and now.hour == 22:
        msg = "📊 Ежедневный отчет\n" + "\n".join(
            f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}"
            for s in SYMBOLS
        ) + f"\nБаланс={get_balance():.2f}"
        send_tg(msg)
        for s in SYMBOLS:
            STATE[s]["count"] = STATE[s]["pnl"] = 0
        save_state()
        with open(fn, "w") as f:
            f.write(str(today))

def main():
    log("🚀 Bot старт — встроенный список символов")
    send_tg("🚀 Bot старт: встроенный список символов")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
