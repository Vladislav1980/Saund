import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# --- Конфигурация ---
load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN   = os.getenv("TG_TOKEN")
CHAT_ID    = os.getenv("CHAT_ID")

SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","XRPUSDT",
           "ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT",
           "SUIUSDT","FILUSDT"]

DEFAULT_PARAMS = {
    "risk_pct": 0.05,
    "tp_multiplier": 1.5,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usd": 1.0,
    "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600,
    "min_commission_cover": 3.0
}

RESERVE_BALANCE      = 500
DAILY_LOSS_LIMIT     = -50
MAX_POS_USDT         = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(message)s",
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
            f = it.get("lotSizeFilter",{})
            LIMITS[s] = {"step": float(f.get("qtyStep",1)),
                         "min_amt": float(it.get("minOrderAmt",10))}

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]=="USDT": return float(c["walletBalance"])
    except Exception as e: log(f"balance err {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]==coin: return float(c["walletBalance"])
    except: pass
    return 0

def get_klines(sym):
    try:
        r = session.get_kline(category="spot",symbol=sym,interval="1",limit=100)
        df = pd.DataFrame(r["result"]["list"],columns=["ts","o","h","l","c","vol","turn"]).astype(float)
        return df
    except: return pd.DataFrame()

def adjust(qty,step):
    try:
        e = int(f"{step:e}".split("e")[-1])
        return math.floor(qty*10**abs(e))/10**abs(e)
    except: return qty

def signal(df, sym):
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"], df["macd_s"] = macd.macd(), macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    log(f"[{sym}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_s']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
        return "none", last["atr"], "vol drop"
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]:
        return "buy", last["atr"], "bullish"
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]:
        return "sell", last["atr"], "bearish"
    return "none", last["atr"], "no signal"

STATE = {}
if os.path.exists("state.json"):
    try:
        STATE = json.load(open("state.json"))
    except: STATE = {}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "last_rebuy": 0, "count":0, "pnl":0.0})

def save_state():
    json.dump(STATE, open("state.json","w"), indent=2)

def trade():
    bal = get_balance()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Торговля остановлена: резерв или дневной лимит")
        return
    load_limits()
    now = time.time()

    for sym in SYMBOLS:
        df = get_klines(sym)
        if df.empty: continue
        sig, atr, reason = signal(df, sym)
        price = df["c"].iloc[-1]
        st = STATE[sym]

        # SELL
        if st["pos"]:
            p = st["pos"]
            b, q, tp, pk, t0 = p["buy"], p["qty"], p["tp"], p["peak"], p["time"]
            pnl = (price-b)*q - price*q*0.001
            dd = (b-price)/b
            held = now - t0
            peak = max(pk, price)
            take  = price >= tp
            rsi_e = df["rsi"].iloc[-1] >= 80 and pnl > DEFAULT_PARAMS["min_commission_cover"]
            trail = price <= peak*(1-DEFAULT_PARAMS["trailing_stop_pct"]) and pnl > DEFAULT_PARAMS["min_commission_cover"]
            sl    = dd >= DEFAULT_PARAMS["max_drawdown_sl"]
            bear  = sig=="sell" and pnl > DEFAULT_PARAMS["min_commission_cover"]
            held_e= held > 12*3600 and pnl > DEFAULT_PARAMS["min_commission_cover"]
            if take or rsi_e or trail or sl or bear or held_e:
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                reason_str = "take" if take else "rsi" if rsi_e else "trail" if trail else "sl" if sl else "bear" if bear else "held"
                msg=f"SELL {sym}@{price:.4f}, qty={q:.6f}, pnl={pnl:.2f}, 이유={reason_str}"
                log(msg); send_tg(msg)
                st["pnl"] += pnl; st["count"] += 1
                st["pos"] = None
            else:
                p["tp"] = max(tp, price + DEFAULT_PARAMS["tp_multiplier"]*atr)
                p["peak"] = peak

        # BUY
        else:
            if sig=="buy":
                qty_usd = min(bal*DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                qty = qty_usd/price
                if sym in LIMITS:
                    qty = adjust(qty, LIMITS[sym]["step"])
                    if qty*price >= LIMITS[sym]["min_amt"]:
                        profit_est = atr*DEFAULT_PARAMS["tp_multiplier"]*qty - price*qty*0.001
                        if profit_est >= DEFAULT_PARAMS["min_profit_usd"] + DEFAULT_PARAMS["min_commission_cover"]:
                            session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                            tp = price + DEFAULT_PARAMS["tp_multiplier"]*atr
                            st["pos"] = {"buy": price, "qty": qty, "tp": tp, "peak": price, "time": now}
                            msg = f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
                            log(msg); send_tg(msg)
                        else:
                            log(f"{sym} skipped: profit_est={profit_est:.2f} < threshold")
                    else:
                        log(f"{sym} skipped: min_amt")
            # emergency sell
            elif sig=="sell" and get_coin_balance(sym)>0:
                cb = get_coin_balance(sym)
                qty = adjust(cb, LIMITS.get(sym,{}).get("step",1))
                if qty*price*0.001 >= DEFAULT_PARAMS["min_profit_usd"] + DEFAULT_PARAMS["min_commission_cover"]:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(qty))
                    msg = f"EMERG SELL {sym}@{price:.4f}, qty={qty:.6f}"
                    log(msg); send_tg(msg)

        # AVERAGING
        if st["pos"]:
            p = st["pos"]
            buy = p["buy"]
            if price <= buy*(1-DEFAULT_PARAMS["avg_rebuy_drop_pct"]) and now-st["last_rebuy"] >= DEFAULT_PARAMS["rebuy_cooldown_secs"]:
                extra_usd = min(bal*DEFAULT_PARAMS["risk_pct"], MAX_POS_USDT)
                extra_qty = extra_usd/price
                extra_qty = adjust(extra_qty, LIMITS[sym]["step"])
                if extra_qty*price >= LIMITS[sym]["min_amt"]:
                    session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(extra_qty))
                    total_qty = p["qty"] + extra_qty
                    avg_price = (p["buy"]*p["qty"] + price*extra_qty)/total_qty
                    st["pos"].update({"buy": avg_price, "qty": total_qty, "tp": avg_price + DEFAULT_PARAMS["tp_multiplier"]*atr})
                    st["last_rebuy"] = now
                    msg=f"AVERAGE {sym}: +{extra_qty:.6f}@{price:.4f}, avg={avg_price:.4f}"
                    log(msg); send_tg(msg)

    save_state()

def daily_report():
    if datetime.datetime.now().hour == 22:
        rep = "📊 Ежедневный отчёт\n"
        for s in SYMBOLS:
            rep += f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}\n"
        rep += f"Баланс USDT={get_balance():.2f}"
        send_tg(rep)
        for s in SYMBOLS:
            STATE[s]["count"] = 0; STATE[s]["pnl"] = 0.0
        save_state()

def main():
    log("🚀 Bot старт. Логи полные.")
    send_tg("🚀 Bot старт. Логи полные.")
    while True:
        try:
            trade()
            daily_report()
        except Exception as e:
            log(f"‼️ Main error: {type(e).__name__} – {e}")
        time.sleep(60)

if __name__=="__main__":
    main()
