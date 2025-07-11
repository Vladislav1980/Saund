import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# --- Настройка окружения ---
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --- Монеты без частых ошибок баланса ---
SYMBOLS = ["COMPUSDT", "NEARUSDT", "TONUSDT", "XRPUSDT", "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT", "SUIUSDT", "FILUSDT"]

# --- Параметры бота ---
DEFAULT_PARAMS = {
    "risk_pct": 0.015,
    "tp_multiplier": 2.0,
    "trail_pct": 0.02,
    "max_sl_pct": 0.06,
    "min_profit_usd": 5,
    "rebuy_dd_pct": 0.07,
    "rebuy_cooldown": 3600,
    "volume_drop_filter": -0.5
}

RESERVE_BALANCE = 300
MAX_POSITION_USDT = 50
DAILY_LOSS_LIMIT = -50

# --- Инициализация ---
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])
def log(msg): logging.info(msg)
def send_tg(msg): 
    try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e: log(f"Telegram error: {e}")

# --- Данные ---
STATE = {}
LIMITS = {}
LAST_PRICE = {}

if os.path.exists("state.json"):
    try: STATE = json.load(open("state.json"))
    except: STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "pnl": 0.0, "count": 0, "last_rebuy": 0})
    LAST_PRICE[s] = None

def save_state():
    with open("state.json", "w") as f:
        json.dump(STATE, f, indent=2)

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except Exception as e:
        log(f"❌ get_balance error: {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except:
        return 0

def load_limits():
    try:
        for it in session.get_instruments_info(category="spot")["result"]["list"]:
            if it["symbol"] in SYMBOLS:
                LIMITS[it["symbol"]] = {
                    "step": float(it.get("lotSizeFilter", {}).get("qtyStep", 1)),
                    "min_amt": float(it.get("minOrderAmt", 10))
                }
    except Exception as e:
        log(f"❌ load_limits error: {e}")

def get_klines(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except:
        return pd.DataFrame()

def adjust(qty, step):
    exp = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10**abs(exp)) / 10**abs(exp)

def signal(df, sym):
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    log(f"[{sym}] EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_s']:.4f}, ATR={last['atr']:.4f}, VolCh={last['vol_ch']:.2f}")
    if last["vol_ch"] < DEFAULT_PARAMS["volume_drop_filter"]:
        return "none", last["atr"], "Low volume"
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"] and last["rsi"] < 75:
        return "buy", last["atr"], "EMA up + MACD + RSI OK"
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
        return "sell", last["atr"], "EMA down + MACD down"
    return "none", last["atr"], "No clear signal"

def trade():
    balance = get_balance()
    log(f"💰 Баланс: {balance:.2f} USDT")
    if balance < RESERVE_BALANCE:
        log("⛔️ Недостаточный резервный баланс")
        return
    if sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("❌ Достигнут дневной лимит убытка")
        return
    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        df = get_klines(sym)
        if df.empty: continue
        sig, atr, reason = signal(df, sym)
        price = df["c"].iloc[-1]
        state = STATE[sym]
        pos = state["pos"]

        # --- Продажа, если есть позиция ---
        if pos:
            b, q, tp, peak, t0 = pos["buy"], pos["qty"], pos["tp"], pos["peak"], pos["time"]
            pnl = (price - b) * q - price * q * 0.001
            drawdown = (b - price) / b
            peak = max(peak, price)
            tp_reached = price >= tp
            rsi_exit = df["rsi"].iloc[-1] >= 80
            trail_exit = price <= peak * (1 - DEFAULT_PARAMS["trail_pct"])
            sl_exit = drawdown >= DEFAULT_PARAMS["max_sl_pct"]
            held_too_long = now - t0 > 3600 * 12

            if tp_reached or rsi_exit or trail_exit or sl_exit or held_too_long:
                reason_exit = "TP" if tp_reached else "RSI>80" if rsi_exit else "Trailing" if trail_exit else "SL" if sl_exit else "Timeout"
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                log(f"🟥 SELL {sym} @ {price:.4f}, qty={q:.6f}, pnl={pnl:.2f} — {reason_exit}")
                send_tg(f"🟥 SELL {sym} @ {price:.4f}, qty={q:.6f}, pnl={pnl:.2f} — {reason_exit}")
                state["pnl"] += pnl; state["count"] += 1; state["pos"] = None
            else:
                new_tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
                pos.update({"tp": max(tp, new_tp), "peak": peak})

        # --- Покупка ---
        elif sig == "buy":
            if LAST_PRICE[sym] and abs(price - LAST_PRICE[sym]) / price < 0.002:
                log(f"[{sym}] 🚫 Повторная цена, пропуск: {price:.4f}")
                continue
            usdt = min(balance * DEFAULT_PARAMS["risk_pct"], MAX_POSITION_USDT)
            qty = usdt / price
            if sym in LIMITS:
                qty = adjust(qty, LIMITS[sym]["step"])
                if qty * price >= LIMITS[sym]["min_amt"]:
                    tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
                    est_profit = (tp - price) * qty - price * qty * 0.001
                    if est_profit >= DEFAULT_PARAMS["min_profit_usd"]:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        state["pos"] = {"buy": price, "qty": qty, "tp": tp, "peak": price, "time": now}
                        LAST_PRICE[sym] = price
                        log(f"🟩 BUY {sym} @ {price:.4f}, qty={qty:.6f}, tp={tp:.4f}")
                        send_tg(f"🟩 BUY {sym} @ {price:.4f}, qty={qty:.6f}, tp={tp:.4f}")
                    else:
                        log(f"[{sym}] ⛔️ Малая прибыль {est_profit:.2f}")
                else:
                    log(f"[{sym}] ⛔️ Объем меньше min_amt")
        # --- Принудительная продажа остатков ---
        elif sig == "sell" and not pos and get_coin_balance(sym) > 0:
            q = adjust(get_coin_balance(sym), LIMITS.get(sym, {}).get("step", 1))
            if q * price > DEFAULT_PARAMS["min_profit_usd"] * 1.5:
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                log(f"⚠️ SELL leftover {sym} @ {price:.4f}, qty={q:.6f}")
                send_tg(f"⚠️ SELL leftover {sym} @ {price:.4f}, qty={q:.6f}")

    save_state()

def main():
    send_tg("🚀 Бот запущен (интегрированная версия)")
    log("🚀 Бот запущен (интегрированная версия)")
    while True:
        try:
            trade()
            time.sleep(60)
        except Exception as e:
            log(f"❌ Ошибка в main: {type(e).__name__} — {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
