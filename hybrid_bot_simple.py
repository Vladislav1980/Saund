import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# === Загрузка переменных окружения ===
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === Настройки ===
DEBUG = False
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]
STATE_PATH = "state.json"
DEFAULT_PARAMS = {
    "risk_pct": 0.05,
    "tp_multiplier": 1.3,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usdt": 1.5,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600,
    "volume_filter": False
}
RESERVE_BALANCE = 0
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 100

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", mode="a", encoding="utf-8"), logging.StreamHandler()]
)
def log(msg): logging.info(msg)

def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        log(f"TG error: {e}")

def send_state_to_telegram():
    try:
        with open(STATE_PATH, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": "📄 Обновлённый state.json"},
                files={"document": f}
            )
    except Exception as e:
        log(f"Ошибка отправки state.json: {e}")

def load_state_from_telegram():
    try:
        updates = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates").json()
        documents = [m["message"]["document"] for m in updates.get("result", []) if "document" in m.get("message", {})]
        for doc in reversed(documents):
            if doc["file_name"] == "state.json":
                file_id = doc["file_id"]
                file_path = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile?file_id={file_id}").json()["result"]["file_path"]
                file_url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}"
                r = requests.get(file_url)
                with open(STATE_PATH, "wb") as f:
                    f.write(r.content)
                log("📥 Загружен state.json из Telegram")
                break
    except Exception as e:
        log(f"Ошибка загрузки state.json из Telegram: {e}")

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}

def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {
                "step": float(f.get("qtyStep", 1)),
                "min_amt": 5.0,
                "max_amt": float(it.get("maxOrderAmt", 1e9))
            }

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT": return float(c["walletBalance"])
    except: pass
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin: return float(c["walletBalance"])
    except: pass
    return 0

def get_klines(sym, interval="5", limit=100):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval=interval, limit=limit)
        return pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
    except Exception as e:
        log(f"klines err {sym}@{interval}: {e}")
        return pd.DataFrame()

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10**abs(e)) / 10**abs(e)

def signal(df):
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    m = MACD(df["c"])
    df["macd"] = m.macd(); df["macd_s"] = m.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

# === STATE ===
STATE = {}
if not os.path.exists(STATE_PATH):
    load_state_from_telegram()
if os.path.exists(STATE_PATH):
    try: STATE = json.load(open(STATE_PATH))
    except: STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "count": 0, "pnl": 0.0})

def save_state():
    try:
        json.dump(STATE, open(STATE_PATH, "w"), indent=2)
        send_state_to_telegram()
    except Exception as e:
        log(f"Ошибка сохранения state.json: {e}")

def calculate_weights(dfs):
    weights = {}
    total = 0
    for sym, df in dfs.items():
        score = 1.0
        if df["15"]["ema9"] > df["15"]["ema21"]: score += 0.2
        if df["60"]["ema9"] > df["60"]["ema21"]: score += 0.2
        if df["240"]["ema9"] > df["240"]["ema21"]: score += 0.2
        if 50 < df["5"]["rsi"] < 65: score += 0.2
        weights[sym] = score
        total += score
    for sym in weights:
        weights[sym] /= total
    return weights

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена по лимиту")
        return

    load_limits()
    dfs = {}
    for sym in SYMBOLS:
        dfs[sym] = {}
        for tf in ["5","15","60","240"]:
            df = signal(get_klines(sym, tf))
            if df.empty:
                log(f"{sym} {tf}m нет данных, пропуск")
                dfs.pop(sym, None)
                break
            dfs[sym][tf] = df.iloc[-1]
    if not dfs:
        return

    weights = calculate_weights(dfs)
    log(f"Весовые коэффициенты: {weights}")

    for sym, df in dfs.items():
        log(f"--- {sym} индикаторы ---")
        for tf, last in df.items():
            log(f"{sym} {tf}m: EMA9={last['ema9']:.2f}, EMA21={last['ema21']:.2f}, MACD={last['macd']:.4f}/{last['macd_s']:.4f}, RSI={last['rsi']:.1f}, ATR={last['atr']:.4f}, vol_ch={last['vol_ch']:.2f}")

        price = df["5"]["c"]
        atr = df["5"]["atr"]
        if df["5"]["ema9"] <= df["5"]["ema21"] or df["5"]["rsi"] > 80:
            log(f"{sym} пропуск по фильтрам EMA или RSI")
            continue

        alloc_usdt = bal * weights[sym]
        qty_usd = min(alloc_usdt * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
        qty_usd = max(qty_usd, 30)
        qty = adjust(qty_usd / price, LIMITS[sym]["step"])
        if qty * price < LIMITS[sym]["min_amt"]:
            log(f"{sym} пропуск: qty*price={qty*price:.2f} < min_amt")
            continue

        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
        tp = price + atr * DEFAULT_PARAMS["tp_multiplier"]
        STATE[sym]["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price}
        msg = f"✅ BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
        log(msg); send_tg(msg)

    for sym in SYMBOLS:
        cb = get_coin_balance(sym)
        if cb > 0 and STATE[sym]["pos"]:
            b, q, tp, peak = STATE[sym]["pos"].values()
            price = dfs[sym]["5"]["c"]
            peak = max(peak, price)
            pnl = (price - b) * q - price * q * 0.001
            dd = (peak - price) / peak
            conds = {
                "STOPLOSS": price < b * (1 - DEFAULT_PARAMS["max_drawdown_sl"]),
                "TRAILING": dd > DEFAULT_PARAMS["trailing_stop_pct"],
                "TPREACHED": price >= tp,
                "PROFIT": pnl >= 1.1 and pnl > price * q * 0.001
            }
            reason = next((k for k,v in conds.items() if v), None)
            if reason:
                qty_s = adjust(cb, LIMITS[sym]["step"])
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty_s))
                msg = f"✅ SELL {reason} {sym}@{price:.4f}, qty={qty_s:.6f}, PNL={pnl:.2f}"
                log(msg); send_tg(msg)
                STATE[sym]["pnl"] += pnl
                STATE[sym]["count"] += 1
                STATE[sym]["pos"] = None

    save_state()

def daily_report():
    now = datetime.datetime.now()
    fn = "last_report.txt"
    prev = open(fn).read().strip() if os.path.exists(fn) else ""
    if now.hour == 22 and str(now.date()) != prev:
        rep = "📊 Ежедневный отчет\n" + "\n".join(
            f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}" for s in SYMBOLS
        ) + f"\nБаланс={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = STATE[s]["pnl"] = 0.0
        save_state()
        open(fn, "w").write(str(now.date()))

def main():
    log("🚀 Bot старт — EMA5, RSI<=80, PROFIT>=1.1USDT")
    send_tg("🚀 Bot старт — EMA5, RSI<=80, PROFIT>=1.1USDT")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
