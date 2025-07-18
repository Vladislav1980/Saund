import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

DEBUG = False
SYMBOLS = [
    "BTCUSDT", "COMPUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "LTCUSDT", "FILUSDT"
]

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DEFAULT_PARAMS = {
    "risk_pct": 0.05,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usdt": 2.5,
    "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600
}

RESERVE_BALANCE = 0
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 50

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
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        log(f"TG error: {e}")

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}

def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {
                "step": float(f.get("qtyStep", 1)),
                "min_amt": float(it.get("minOrderAmt", 10)),
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
        return pd.DataFrame(r["result"]["list"], columns=["ts", "o", "h", "l", "c", "vol", "turn"]).astype(float)
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
    m = MACD(df["c"])
    df["macd"] = m.macd(); df["macd_s"] = m.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

def check_trend(df):
    last = df.iloc[-1]
    return last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"], last

# ——— Блок загрузки состояния ———
STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            STATE = json.load(f)
    except:
        STATE = {}
else:
    STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "count": 0, "pnl": 0.0})
# ————————————————

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"Ошибка сохранения state.json: {e}")

def calculate_weights(dfs):
    weights = {}
    total = 0
    for sym, df in dfs.items():
        score = 1.0
        ok15 = df["15"]["ema9"] > df["15"]["ema21"] and df["15"]["macd"] > df["15"]["macd_s"]
        ok60 = df["60"]["ema9"] > df["60"]["ema21"] and df["60"]["macd"] > df["60"]["macd_s"]
        ok240 = df["240"]["ema9"] > df["240"]["ema21"] and df["240"]["macd"] > df["240"]["macd_s"]
        if ok15 and ok60 and ok240: score += 0.5
        if 50 < df["5"]["rsi"] < 65: score += 0.2
        weights[sym] = score
        total += score
    for sym in weights: weights[sym] /= total
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
        for tf in ["5", "15", "60", "240"]:
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
            log(f"{sym} {tf}m: EMA9={last['ema9']:.2f}, EMA21={last['ema21']:.2f}, "
                f"MACD={last['macd']:.4f}/{last['macd_s']:.4f}, RSI={last['rsi']:.1f}, ATR={last['atr']:.4f}, vol_ch={last['vol_ch']:.2f}")

        price = df["5"]["c"]
        atr = df["5"]["atr"]
        buy5 = df["5"]["ema9"] > df["5"]["ema21"] and df["5"]["macd"] > df["5"]["macd_s"]
        mtf_ok_count = sum(
            1 for tf in ["15", "60", "240"]
            if df[tf]["ema9"] > df[tf]["ema21"] and df[tf]["macd"] > df[tf]["macd_s"]
        )
        mtf_ok = mtf_ok_count >= 1
        rsi5 = df["5"]["rsi"]
        rsi_ok = rsi5 <= 80

        log(f"{sym}: buy5={buy5}, rsi5={rsi5:.1f}, mtf_ok_count={mtf_ok_count}/3")

        if not buy5:
            log(f"{sym} пропуск: 5m сигнал отсутствует")
            continue
        if not rsi_ok:
            log(f"{sym} пропуск: RSI={rsi5:.1f} > 80")
            continue
        if not mtf_ok:
            log(f"{sym} пропуск: MTF OK только на {mtf_ok_count}/3 TF")
            continue

        alloc_usdt = bal * weights[sym]
        qty_usd = min(alloc_usdt * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
        qty = adjust(qty_usd / price, LIMITS[sym]["step"])

        log(f"{sym} шаг qtyStep={LIMITS[sym]['step']}, min_amt={LIMITS[sym]['min_amt']}, qty={qty:.6f}")

        if qty * price < LIMITS[sym]["min_amt"]:
            min_amt = LIMITS[sym]["min_amt"]
            qty = adjust(min_amt / price, LIMITS[sym]["step"])
            log(f"{sym}: qty скорректирован до min_amt — qty={qty:.6f}, price={price:.4f}")

        if qty == 0:
            log(f"⚠️ {sym} qty округлён до 0. Возможно, шаг слишком крупный")
            continue

        est = atr * DEFAULT_PARAMS["tp_multiplier"] * qty - price * qty * 0.001 - DEFAULT_PARAMS["min_profit_usdt"]
        if est < 0:
            log(f"{sym} пропуск: плохая PNL={est + DEFAULT_PARAMS['min_profit_usdt']:.2f}")
            continue

        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
        tp = price + atr * DEFAULT_PARAMS["tp_multiplier"]
        STATE[sym]["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price}
        save_state()
        msg = f"✅ BUY {sym}@{price:.4f}, qty={qty:.6f}, Вес={weights[sym]:.3f}, TP~{tp:.4f}"
        log(msg); send_tg(msg)

        break  # после покупки выходим, чтоб не входить в несколько сразу

    # Продажа
    for sym in SYMBOLS:
        pos = STATE[sym].get("pos")
        if not pos: continue
        price = session.get_ticker(symbol=sym)["result"]["price"]
        cb = get_coin_balance(sym)
        if cb <= 0: continue
        peak = max(pos["peak"], price)
        pnl = (price - pos["buy_price"]) * pos["qty"] - price * pos["qty"] * 0.001
        dd = (peak - price) / peak
        conds = {
            "STOPLOSS": price < pos["buy_price"] * (1 - DEFAULT_PARAMS["max_drawdown_sl"]),
            "TRAILING": dd > DEFAULT_PARAMS["trailing_stop_pct"],
            "PROFIT": price >= pos["tp"]
        }
        reason = next((k for k, v in conds.items() if v), None)
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
            f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}"
            for s in SYMBOLS
        ) + f"\nБаланс={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = STATE[s]["pnl"] = 0.0
        save_state()
        open(fn, "w").write(str(now.date()))

def main():
    log("🚀 Bot старт — MTF фильтр 1/3, RSI<=80")
    send_tg("🚀 Bot старт — MTF:1/3, RSI<=80. Остальной функционал без изменений")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
