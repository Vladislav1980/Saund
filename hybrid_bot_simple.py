import os, time, math, json, datetime, logging, requests, sys
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

SYMBOLS = [
    "COMPUSDT", "NEARUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "SUIUSDT", "FILUSDT"
]

DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usd": 5.0,
    "volume_filter": 0.3
}

RESERVE_BALANCE = 400
DAILY_LIMIT = -50
MAX_POSITION_SIZE_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
)

def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        log(f"Telegram error: {e}")

def adjust_qty(qty, step):
    try:
        exponent = int(f"{step:e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except:
        return qty

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except Exception as e:
        log(f"❌ get_balance error: {e}")
    return 0.0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except:
        pass
    return 0.0

LIMITS = {}
def load_symbol_limits():
    try:
        for item in session.get_instruments_info(category="spot")["result"]["list"]:
            s = item["symbol"]
            if s in SYMBOLS:
                f = item.get("lotSizeFilter",{})
                LIMITS[s] = {
                    "qty_step": float(f.get("qtyStep",1)),
                    "min_amt": float(item.get("minOrderAmt",10))
                }
    except Exception as e:
        log(f"❌ load_symbol_limits error: {e}")

def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        return df
    except:
        return pd.DataFrame()

def signal(df, sym):
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    last=df.iloc[-1]
    log(f"[{sym}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_signal']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -DEFAULT_PARAMS["volume_filter"]:
        return "none", last["atr"], "volume_drop"
    if last["ema9"]>last["ema21"] and last["macd"]>last["macd_signal"]:
        return "buy", last["atr"], "ema9>21 & macd>sig"
    if last["ema9"]<last["ema21"] and last["macd"]<last["macd_signal"]:
        return "sell", last["atr"], "ema9<21 & macd<sig"
    return "none", last["atr"], "no_signal"

def calc_tp(price, atr, qty):
    fee = price*qty*0.001
    base_tp = price + DEFAULT_PARAMS["tp_multiplier"]*atr
    required = (DEFAULT_PARAMS["min_profit_usd"]+fee+3)/qty+price
    return max(base_tp, required)

def trade():
    usdt = get_balance()
    log(f"Balance USDT: {usdt:.2f}")
    if usdt < RESERVE_BALANCE:
        log("🛑 Баланс ниже резерва, ни покупать, ни продавать.")
        return

    load_symbol_limits()

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue

            sig, atr, reason = signal(df,sym)
            price = df["c"].iloc[-1]
            log(f"[{sym}] signal={sig} ({reason}), price={price:.4f}")

            balance_coin = get_coin_balance(sym)
            if balance_coin>0:
                val = balance_coin*price
                pnl = val - balance_coin*price*0.001
                if pnl > DEFAULT_PARAMS["min_profit_usd"]+3:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(balance_coin))
                    log(f"💸 SELL BY BALANCE {sym}@{price:.4f}, qty={balance_coin:.6f}, val={val:.2f}, pnl~{pnl:.2f}")
                    send_tg(f"💸 SELL {sym}@{price:.4f}, qty={balance_coin:.6f}, est PnL={pnl:.2f} by balance")
                    continue

            if sig=="buy":
                if sym in LIMITS:
                    qty_usdt=min(usdt*DEFAULT_PARAMS["risk_pct"], MAX_POSITION_SIZE_USDT)
                    qty=adjust_qty(qty_usdt/price, LIMITS[sym]["qty_step"])
                    if qty*price < LIMITS[sym]["min_amt"]:
                        log(f"❌ {sym}: qty*price too low ({qty*price:.2f})")
                        continue
                    tp=calc_tp(price, atr, qty)
                    profit=(tp-price)*qty - price*qty*0.001
                    if profit < DEFAULT_PARAMS["min_profit_usd"]+3:
                        log(f"❌ {sym}: profit too low ({profit:.2f})")
                        continue
                    session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                    log(f"🟢 BUY {sym}@{price:.4f}, qty={qty:.6f}, TP={tp:.4f}, estProfit={profit:.2f}")
                    send_tg(f"🟢 BUY {sym}@{price:.4f}, qty={qty:.6f}, TP={tp:.4f}, estProfit={profit:.2f}")
        except Exception as e:
            log(f"❌ Error {sym}: {type(e).__name__}: {e}")

def main():
    log("🚀 Starting bot with reserve and detailed logs")
    send_tg("🚀 Bot запущен. Полные логи, резерв:", RESERVE_BALANCE)
    while True:
        trade()
        time.sleep(60)

if __name__=="__main__":
    main()
