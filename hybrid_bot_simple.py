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

SYMBOLS = ["COMPUSDT","NEARUSDT","TONUSDT","XRPUSDT","ADAUSDT","BCHUSDT",
           "LTCUSDT","AVAXUSDT","SUIUSDT","FILUSDT"]

PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usd": 2.5,
    "volume_filter": 0.3,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600
}

RESERVE_BALANCE = 500
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 100

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
        log(f"TG err: {e}")

LIMITS = {}
def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s=it["symbol"]
        if s in SYMBOLS:
            f=it.get("lotSizeFilter",{})
            LIMITS[s]={"step":float(f.get("qtyStep",1)),"min_amt":float(it.get("minOrderAmt",10))}

def get_klines(sym, interval):
    try:
        r=session.get_kline(category="spot", symbol=sym, interval=interval, limit=200)
        df=pd.DataFrame(r["result"]["list"],columns=["ts","o","h","l","c","vol","turn"])
        return df.astype({"o":"float","h":"float","l":"float","c":"float","vol":"float"})
    except:
        return pd.DataFrame()

def signal_multitime(sym):
    df5 = get_klines(sym, "5")
    if df5.empty: return "none", None
    dfs = {tf: get_klines(sym, tf) for tf in ["60","240","D"]}
    sig5, atr5 = signal_single(df5, sym)
    # фильтр: все тренды совпадают с входом
    ok = True
    for tf, df in dfs.items():
        if df.empty: ok=False; break
        s,_ = signal_single(df, sym)
        if s=="none": ok=False; break
        # тренды должны совпадать (вверх/вниз)
        if (sig5=="buy" and s!="buy") or (sig5=="sell" and s!="sell"):
            ok=False; break
    return (sig5, atr5) if ok else ("none", None)

def signal_single(df, sym=None):
    df["ema9"]=EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]=EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]=RSIIndicator(df["c"]).rsi()
    macd=MACD(df["c"]); df["macd"]=macd.macd(); df["macd_s"]=macd.macd_signal()
    df["atr"]=AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["vol_ch"]=df["vol"].pct_change().fillna(0)
    last=df.iloc[-1]
    if sym:
        log(f"[{sym}][{df.index.name or ''}] ema9={last['ema9']:.4f}, ema21={last['ema21']:.4f}, rsi={last['rsi']:.1f}, macd={last['macd']:.4f}/{last['macd_s']:.4f}, vol_ch={last['vol_ch']:.2f}, atr={last['atr']:.4f}")
    if last["vol_ch"] < -PARAMS["volume_filter"]: return "none", last["atr"]
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_s"]: return "buy", last["atr"]
    if last["ema9"] < last["ema21"] and last["macd"] < last["macd_s"]: return "sell", last["atr"]
    return "none", last["atr"]

STATE = {}
if os.path.exists("state.json"):
    try: STATE=json.load(open("state.json"))
    except: STATE={}
for s in SYMBOLS:
    STATE.setdefault(s, {"pos":None,"last_rebuy":0,"count":0,"pnl":0.0})

def adjust(qty, step):
    e=int(f"{step:e}".split("e")[-1])
    return math.floor(qty*10**abs(e))/10**abs(e)

def trade():
    bal = get_balance_usdt()
    log(f"Баланс USDT: {bal:.2f}")
    if bal < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LOSS_LIMIT:
        log("🚫 Стоп: ниже резерва или лимит убытка")
        return
    load_limits()
    now = time.time()
    for sym in SYMBOLS:
        df_main = get_klines(sym, "5"); 
        if df_main.empty: continue
        sig, atr = signal_multitime(sym)
        price = df_main["c"].iloc[-1]
        st = STATE[sym]; pos = st["pos"]
        # SELL
        if pos:
            b=pos["buy_price"]; q=pos["qty"]; tp=pos["tp"]; peak=pos["peak"]
            pnl=(price-b)*q - price*q*0.001
            dd=(b-price)/b; held=now-pos["time"]; peak=max(peak,price)
            cond = (
                price>=tp or
                df_main["rsi"].iloc[-1]>=80 and pnl>0 or
                price<=peak*(1-PARAMS["trailing_stop_pct"]) and pnl>0 or
                dd>=PARAMS["max_drawdown_sl"] or
                sig=="sell" and pnl>0 or
                held>12*3600 and pnl>0
            )
            if cond and pnl>=PARAMS["min_profit_usd"]:
                session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                reason="TP/SL/Trail/Trend"
                log_msg=f"SELL {sym}@{price:.4f}, qty={q:.6f}, PnL={pnl:.2f}, reason={reason}"
                log(log_msg); send_tg(log_msg)
                st["pnl"]+=pnl; st["count"]+=1; st["pos"]=None
            else:
                st["pos"].update({"tp":max(tp, price+PARAMS["tp_multiplier"]*atr),"peak":peak})
        # BUY
        else:
            if sig=="buy":
                qty_usd = min(bal*PARAMS["risk_pct"], MAX_POS_USDT)
                qty = qty_usd/price
                if sym in LIMITS:
                    qty=adjust(qty,LIMITS[sym]["step"])
                    if qty*price>=LIMITS[sym]["min_amt"]:
                        est_pnl=atr*PARAMS["tp_multiplier"]*qty - price*qty*0.001
                        if est_pnl>=PARAMS["min_profit_usd"]:
                            session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                            tp=price+PARAMS["tp_multiplier"]*atr
                            st["pos"]={"buy_price":price,"qty":qty,"tp":tp,"peak":price,"time":now}
                            log_msg=f"BUY {sym}@{price:.4f}, qty={qty:.6f}, TP~{tp:.4f}"
                            log(log_msg); send_tg(log_msg)
                        else:
                            log(f"{sym} skipBD: profit {est_pnl:.2f}<min")
                    else:
                        log(f"{sym} skipBD: size {qty*price:.2f}<min_amt")
            # авто-продажа баланса
            elif sig=="sell" and get_coin_balance(sym)>0:
                cb=get_coin_balance(sym)
                q=adjust(cb,LIMITS.get(sym,{}).get("step",1))
                if q*price*0.999 - price*q*0.001 >= PARAMS["min_profit_usd"]:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                    log_msg=f"BAL sell {sym}@{price:.4f}, qty={q:.6f}"
                    log(log_msg); send_tg(log_msg)
        # Averaging
        if st["pos"]:
            buy=st["pos"]["buy_price"]
            if price <= buy*(1-PARAMS["avg_rebuy_drop_pct"]) and now-st["last_rebuy"]>=PARAMS["rebuy_cooldown_secs"]:
                extra = adjust((bal*PARAMS["risk_pct"]/price), LIMITS[sym]["step"])
                if extra*price>=LIMITS[sym]["min_amt"]:
                    session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(extra))
                    total=st["pos"]["qty"]+extra
                    avg=(buy*st["pos"]["qty"]+price*extra)/total
                    st["pos"].update({"buy_price":avg,"qty":total,"tp":avg+PARAMS["tp_multiplier"]*atr})
                    st["last_rebuy"]=now
                    log_msg=f"AVERAGE {sym}: +{extra:.6f}@{price:.4f} -> avg={avg:.4f}"
                    log(log_msg); send_tg(log_msg)
    json.dump(STATE, open("state.json","w"), indent=2)

def get_balance_usdt():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]=="USDT": return float(c["walletBalance"])
    except: pass
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"]==coin: return float(c["walletBalance"])
    except: pass
    return 0

def daily_report():
    if datetime.datetime.now().hour==22:
        msg="📊 Daily report\n"
        for s in SYMBOLS:
            msg+=f"{s}: trades={STATE[s]['count']}, pnl={STATE[s]['pnl']:.2f}\n"
        msg+=f"USDT balance={get_balance_usdt():.2f}"
        send_tg(msg)
        for s in SYMBOLS:
            STATE[s]["count"]=0; STATE[s]["pnl"]=0.0
        json.dump(STATE, open("state.json","w"), indent=2)

def main():
    log("🚀 Bot start full logs + MTF")
    send_tg("🚀 Bot start full logs + MTF")
    while True:
        try:
            trade()
            daily_report()
        except Exception as e:
            log(f"MAIN ERR: {e}")
        time.sleep(60)

if __name__=="__main__":
    main()
