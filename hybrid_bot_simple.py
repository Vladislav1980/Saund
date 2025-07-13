import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["COMPUSDT", "NEARUSDT", "TONUSDT", "XRPUSDT", "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT", "SUIUSDT", "FILUSDT"]

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
MAX_POS_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
                    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def log(msg): logging.info(msg)

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": str(msg)})
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
                if c["coin"] == "USDT": return float(c["walletBalance"])
    except Exception as e:
        log(f"balance err {e}")
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
        df = pd.DataFrame(r["result"]["list"], columns=["ts", "o", "h", "l", "c", "vol", "turn"]).astype(float)
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
    df["macd"] = macd.macd()
    df["macd_s"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

def trend_check(sym):
    mtf = {"15": None, "60": None, "240": None}
    all_ok = True
    for tf in mtf:
        df = get_klines(sym, interval=tf)
        if df.empty:
            mtf[tf] = "no data"
            all_ok = False
            continue
        df = signal(df)
        last = df.iloc[-1]
        ok = last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]
        mtf[tf] = f"{'OK' if ok else 'fail'} EMA9={last['ema9']:.2f},21={last['ema21']:.2f},MACD={last['macd']:.4f}/{last['macd_s']:.4f},RSI={last['rsi']:.1f},ATR={last['atr']:.4f}"
        if not ok:
            all_ok = False
    return all_ok, mtf

STATE = {}
if os.path.exists("state.json"):
    try:
        STATE = json.load(open("state.json"))
    except:
        STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "last_rebuy": 0, "count": 0, "pnl": 0.0})

def save_state(): json.dump(STATE, open("state.json", "w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена: резерв или лимит убытка")
        return
    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        df5 = get_klines(sym, interval="5")
        if df5.empty: continue
        df5 = signal(df5)
        last = df5.iloc[-1]

        sig = "none"
        if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
            if (last["rsi"] < 30) or (last["macd"] > last["macd_s"]):
                if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
                    sig = "buy"
                elif last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
                    sig = "sell"
                log(f"{sym} слаб/падает объём, RSI/MACD допускают {sig}")
            else:
                log(f"{sym} пропущен: объём вниз, без сигнала")
        else:
            if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
                sig = "buy"
            elif last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
                sig = "sell"

        mt_ok, mts = trend_check(sym)
        if sig == "buy" and not mt_ok:
            log(f"{sym} 5m=BUY, но MTF не подтвердил:")
            for tf, desc in mts.items():
                log(f"  {tf}m -> {desc}")
            sig = "none"

        price = last["c"]
        atr = last["atr"]
        state = STATE[sym]
        pos = state["pos"]

        # SELL логика
        if pos:
            b, q, tp, peak = pos["buy_price"], pos["qty"], pos["tp"], pos["peak"]
            pnl = (price - b) * q - price * q * 0.001
            peak = max(peak, price)
            conds = {
                "TP": price >= tp,
                "RSI": last["rsi"] >= 80 and pnl > 0,
                "Trail": price <= peak * (1 - DEFAULT_PARAMS["trailing_stop_pct"]) and pnl > 0,
                "SL": (b - price) / b >= DEFAULT_PARAMS["max_drawdown_sl"],
                "Held": now - pos["time"] > 12 * 3600 and pnl > 0
            }
            reason = next((k for k, v in conds.items() if v), None)
            if reason:
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                txt = f"SELL {sym}@{price:.4f}, qty={q:.6f}, pnl={pnl:.2f} via {reason}"
                log(txt); send_tg(txt)
                state["pnl"] += pnl; state["count"] += 1; state["pos"] = None
            else:
                state["pos"].update({"tp": max(tp, price + DEFAULT_PARAMS["tp_multiplier"] * atr), "peak": peak})

        # BUY логика
        elif sig == "buy":
            qty_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
            qty = qty_usd / price
            if sym in LIMITS:
                qty = adjust(qty, LIMITS[sym]["step"])
                if qty == 0:
                    log(f"{sym} qty=0 после округления buy"); continue
                if qty * price >= LIMITS[sym]["min_amt"]:
                    est_profit = atr * DEFAULT_PARAMS["tp_multiplier"] * qty - price * qty * 0.001 - DEFAULT_PARAMS["min_profit_usdt"]
                    if est_profit >= 0:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + DEFAULT_PARAMS["tp_multiplier"] * atr
                        state["pos"] = {"buy_price": price, "qty": qty, "tp": tp, "peak": price, "time": now}
                        txt = f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
                        log(txt); send_tg(txt)
                    else:
                        log(f"{sym} пропущен: profit < min ({est_profit:.2f})")
                else:
                    log(f"{sym} пропущен: объём {qty * price:.2f} < min_amt")
        else:
            cb = get_coin_balance(sym)
            if cb > 0:
                qty = adjust(cb, LIMITS.get(sym, {}).get("step", 1))
                if qty == 0:
                    log(f"{sym} qty=0 после округления sell")
                    continue
                session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qty))
                log(f"SELL BALANCE {sym}@{price:.4f}, qty={qty:.6f}"); send_tg(f"SELL BALANCE {sym}@{price:.4f}, qty={qty:.6f}")

        # Усреднение
        if state["pos"]:
            buy = state["pos"]["buy_price"]
            if price <= buy * (1 - DEFAULT_PARAMS["avg_rebuy_drop_pct"]) and now - state["last_rebuy"] >= DEFAULT_PARAMS["rebuy_cooldown_secs"]:
                extra_usd = min(bal * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                extra_qty = adjust(extra_usd / price, LIMITS[sym]["step"])
                if extra_qty * price >= LIMITS[sym]["min_amt"]:
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(extra_qty))
                    total_qty = state["pos"]["qty"] + extra_qty
                    avg_price = (buy * state["pos"]["qty"] + price * extra_qty) / total_qty
                    state["pos"].update({"buy_price": avg_price, "qty": total_qty, "tp": avg_price + DEFAULT_PARAMS["tp_multiplier"] * atr})
                    state["last_rebuy"] = now
                    txt = f"AVERAGE {sym}: +{extra_qty:.6f} @ {price:.4f} => avg {avg_price:.4f}"
                    log(txt); send_tg(txt)

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
            STATE[s]["count"] = 0; STATE[s]["pnl"] = 0.0
        save_state()

def main():
    log("🚀 Bot start full logs + MTF (5m base, 15m/1h/4h filters)")
    send_tg("🚀 Bot старт: фильтрация 5m + 15m, 1h, 4h")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
