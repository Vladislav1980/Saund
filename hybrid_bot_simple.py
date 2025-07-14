import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

DEBUG = False

SYMBOLS = [
    "ARBUSDT", "TRXUSDT", "DOGEUSDT",
    "NEARUSDT", "COMPUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "SUIUSDT", "FILUSDT"
]

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DEFAULT_PARAMS = {
    "risk_pct": 0.03, "tp_multiplier": 1.8, 
    "trailing_stop_pct": 0.02, "max_drawdown_sl": 0.06,
    "min_profit_usdt": 2.5, "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07, "rebuy_cooldown_secs": 3600
}
RESERVE_BALANCE = 500
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 50  # максимум на ордер

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", "utf-8"), logging.StreamHandler()]
)

def log(msg):
    logging.info(msg)

def debug(msg):
    if DEBUG:
        logging.info("(DEBUG) " + msg)

def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        log(f"TG error: {e}")

LIMITS = {}
def load_limits():
    resp = session.get_instruments_info(category="spot")["result"]["list"]
    for it in resp:
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
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except:
        pass
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
        d = session.get_kline(category="spot", symbol=sym, interval=interval, limit=limit)
        return pd.DataFrame(d["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
    except Exception as e:
        log(f"klines err {sym}@{interval}: {e}")
        return pd.DataFrame()

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10 ** abs(e)) / 10 ** abs(e)

def signal(df):
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    m = MACD(df["c"])
    df["macd"] = m.macd(); df["macd_s"] = m.macd_signal()
    df["atr"] = AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

def check_trend(df):
    last = df.iloc[-1]
    return (last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]), last

STATE = {}
if os.path.exists("state.json"):
    try: STATE = json.load(open("state.json"))
    except: STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None,"last_rebuy":0,"count":0,"pnl":0.0,"last_summary":None})
def save_state():
    json.dump(STATE, open("state.json","w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена")
        return

    load_limits()
    now = time.time()

    alloc = bal / len(SYMBOLS)
    for sym in SYMBOLS:
        df5 = get_klines(sym, "5"); df15 = get_klines(sym, "15")
        if df5.empty or df15.empty:
            log(f"{sym} → no data"); continue

        df5 = signal(df5); df15 = signal(df15)
        last5 = df5.iloc[-1]
        price, atr = last5["c"], last5["atr"]
        vol_ch = last5["vol_ch"]
        buy5 = last5["ema9"] > last5["ema21"] and last5["macd"] > last5["macd_s"]
        sell5 = last5["ema9"] < last5["ema21"] and last5["macd"] < last5["macd_s"]

        sig = "none"
        if vol_ch < -DEFAULT_PARAMS["volume_filter"] and (last5["rsi"]<30 or last5["macd"]>last5["macd_s"]):
            if buy5: sig="buy"
            elif sell5: sig="sell"
        else:
            sig = "buy" if buy5 else ("sell" if sell5 else "none")

        mtf = {}
        ok_buy = True
        for tf in ["15","60","240"]:
            df_tf = signal(get_klines(sym, tf))
            if df_tf.empty:
                mtf[tf]=None; ok_buy=False
            else:
                tb, lf = check_trend(df_tf)
                mtf[tf]=lf
                if sig=="buy" and not tb: ok_buy=False

        log(f"{sym}: sig={sig}, price={price:.4f}, vol_ch={vol_ch:.3f}, "
            f"EMA9/21={last5['ema9']:.2f}/{last5['ema21']:.2f}, "
            f"MACD={last5['macd']:.4f}/{last5['macd_s']:.4f}, RSI={last5['rsi']:.1f}")

        if sig=="buy":
            for tf,l in mtf.items():
                if l is None:
                    log(f"{sym} {tf}m: NO DATA")
                else:
                    trendOk = l["ema9"] > l["ema21"] and l["macd"] > l["macd_s"]
                    log(f"{sym} {tf}m trend={'OK' if trendOk else 'FAIL'} "
                        f"EMA9={l['ema9']:.2f}/EMA21={l['ema21']:.2f}, "
                        f"MACD={l['macd']:.4f}/{l['macd_s']:.4f}, RSI={l['rsi']:.1f}")

        cb = get_coin_balance(sym)
        pos = STATE[sym]["pos"]

        if sig=="buy" and ok_buy:
            qty_usd = min(alloc * DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
            qty = adjust(qty_usd / price, LIMITS[sym]["step"])
            if qty*price < LIMITS[sym]["min_amt"]:
                log(f"{sym}: skip buy, <min_amt")
                continue
            if qty*price > LIMITS[sym]["max_amt"]:
                log(f"{sym}: skip buy, >max_amt API")
                continue
            est = atr * DEFAULT_PARAMS["tp_multiplier"] * qty - price*qty*0.001 - DEFAULT_PARAMS["min_profit_usdt"]
            if est < 0:
                log(f"{sym}: skip buy, est_profit<{DEFAULT_PARAMS['min_profit_usdt']}")
                continue
            session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
            tp = price + atr*DEFAULT_PARAMS["tp_multiplier"]
            STATE[sym]["pos"] = {"buy_price":price,"qty":qty,"tp":tp,"peak":price,"time":now}
            msg = f"✅ BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
            log(msg); send_tg(msg)

        elif cb>0 and pos:
            peak = max(pos["peak"], price)
            pos["peak"] = peak
            lb = pos["buy_price"]
            dd_pct = (peak-price)/peak
            stoploss = price < lb * (1 - DEFAULT_PARAMS["max_drawdown_sl"])
            trailing = dd_pct > DEFAULT_PARAMS["trailing_stop_pct"]
            thr_atr = lb + DEFAULT_PARAMS["tp_multiplier"]*atr
            thr_pct = lb * (1 + DEFAULT_PARAMS["tp_multiplier"])
            prof = price >= max(thr_pct, thr_atr)
            sell_signal = sell5 or stoploss or trailing or prof

            if sell_signal:
                qty = adjust(cb, LIMITS[sym]["step"])
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(qty))
                if sell5:
                    typ="SIGNAL"
                elif stoploss:
                    typ="STOPLOSS"
                elif trailing:
                    typ="TRAILING"
                else:
                    typ="PROFIT"
                pnl = (price-lb)*qty - price*qty*0.001
                msg = f"✅ SELL {typ} {sym}@{price:.4f}, qty={qty:.6f}, PNL={pnl:.2f} USDT"
                log(msg); send_tg(msg)
                STATE[sym]["count"] += 1
                STATE[sym]["pnl"] += pnl
                STATE[sym]["pos"] = None

    save_state()

def daily_report():
    now = datetime.datetime.now()
    today = now.date()
    fn = "last_report.txt"
    prev = None
    if os.path.exists(fn):
        with open(fn) as f:
            prev = f.read().strip()
    if str(today) != prev and now.hour == 22:
        msg = "📊 Ежедневный отчет\n" + "\n".join(
            f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}" for s in SYMBOLS
        ) + f"\nБаланс={get_balance():.2f}"
        send_tg(msg)
        for s in SYMBOLS:
            STATE[s]["count"] = STATE[s]["pnl"] = 0
        save_state()
        with open(fn, "w") as f:
            f.write(str(today))

def main():
    log("🚀 Bot старт — финальная версия")
    send_tg("🚀 Bot старт: все функции активны")
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
