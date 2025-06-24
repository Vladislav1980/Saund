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

SYMBOLS = [ "COMPUSDT","NEARUSDT","TONUSDT","TRXUSDT","XRPUSDT",
            "ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT",
            "WIFUSDT","ARBUSDT","SUIUSDT","FILUSDT" ]

DEFAULT_PARAMS = {
    "risk_pct": 0.01,
    "tp_multiplier": 1.8,
    "trail_multiplier": 1.5,
    "max_drawdown_sl": 0.06,
    "min_avg_drawdown": 0.03,
    "trailing_stop_pct": 0.015,
    "min_profit_usd": 6.0
}

RESERVE_BALANCE = 500
DAILY_LIMIT = -50
MIN_TP_PCT = 0.007
COOLDOWN_AVG_SECS = 3600
MAX_AVG_COUNT = 2
MAX_POSITION_SIZE_USDT = 50
MAX_OPEN_POSITIONS = 4

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        log(f"Telegram error: {e}")

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except:
        return qty

def get_balance():
    try:
        wallets = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
        for w in wallets:
            for c in w["coin"]:
                if c["coin"]=="USDT":
                    return float(c["walletBalance"])
    except Exception as e:
        log(f"❌ get_balance error: {e}")
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        wallets = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
        for w in wallets:
            for c in w["coin"]:
                if c["coin"]==coin:
                    return float(c["walletBalance"])
    except:
        pass
    return 0

LIMITS = {}
def load_symbol_limits():
    try:
        data = session.get_instruments_info(category="spot")["result"]["list"]
        for item in data:
            if item["symbol"] in SYMBOLS:
                f = item.get("lotSizeFilter", {})
                LIMITS[item["symbol"]] = {
                    "min_qty": float(f.get("minOrderQty",0)),
                    "qty_step": float(f.get("qtyStep",1)),
                    "min_amt": float(item.get("minOrderAmt",10))
                }
    except Exception as e:
        log(f"❌ load_symbol_limits error: {e}")

def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
        df[["o","h","l","c","vol"]]=df[["o","h","l","c","vol"]].astype(float)
        return df
    except:
        return pd.DataFrame()

def signal(df, sym=None):
    df["ema9"]=EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]=EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]=RSIIndicator(df["c"]).rsi()
    m=MACD(df["c"])
    df["macd"]=m.macd(); df["macd_signal"]=m.macd_signal()
    df["atr"]=AverageTrueRange(df["h"],df["l"],df["c"]).average_true_range()
    df["volume_change"]=df["vol"].pct_change().fillna(0)
    last=df.iloc[-1]
    if sym: log(f"[{sym}] IND → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, ATR={last['atr']:.4f}")
    if last["volume_change"]<-0.5: return "none", last["atr"], "⛔️ Объём резко упал"
    if last["ema9"]>last["ema21"] and last["macd"]>last["macd_signal"] and last["rsi"]<75: return "buy", last["atr"], "📈 BUY сигнал"
    if last["ema9"]<last["ema21"] and last["macd"]<last["macd_signal"] and last["rsi"]>30: return "sell", last["atr"], "🔻 SELL сигнал"
    return "none", last["atr"], "нет сигнала"

def detect_market_phase(df):
    last=df.iloc[-1]
    if last["ema9"]>last["ema21"] and last["macd"]>last["macd_signal"]: return "bull"
    if last["ema9"]<last["ema21"] and last["macd"]<last["macd_signal"]: return "bear"
    return "sideways"

def calc_adaptive_tp(price, atr, qty, params):
    fee = price*qty*0.001
    base = price + params["tp_multiplier"]*atr
    tp_min = (params["min_profit_usd"]+fee)/qty + price
    return max(base, tp_min)

def calc_adaptive_qty(balance, atr, price, sym, risk_pct):
    risk_usdt = min(balance*risk_pct, MAX_POSITION_SIZE_USDT)
    qty = risk_usdt/price
    step = LIMITS[sym]["qty_step"]
    adj = adjust_qty(qty, step)
    if adj==0 or adj*price < LIMITS[sym]["min_amt"] or adj*price<10: return 0
    return adj

def log_trade(sym, action, price, qty, pnl, reason):
    val = price*qty
    roi = pnl/val*100 if val>0 else 0
    msg=f"{action} {sym} @ {price:.4f}, qty={qty:.6f}, USDT={val:.2f}, PnL={pnl:.4f}, ROI={roi:.2f}% | {reason}"
    log(msg); send_tg(msg)

def should_exit_by_rsi(df): return df["rsi"].iloc[-1]>=80
def should_exit_by_trailing(price, peak, params): return peak and price<=peak*(1-params["trailing_stop_pct"])
def is_profitable_exit(pnl,price,qty,params): return pnl>=params["min_profit_usd"] + price*qty*0.001

STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json","r") as f:
            txt=f.read().strip()
            STATE = json.loads(txt) if txt else {}
            for s in STATE:
                for p in STATE[s].get("positions",[]):
                    p.setdefault("peak_price", p["buy_price"])
                    log(f"[{s}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
    except Exception as e:
        log(f"❌ state.json error: {e}")
save_state = lambda: open("state.json","w").write(json.dumps(STATE,indent=2))

CYCLE_COUNT=0; LAST_REPORT_DATE=None
for s in SYMBOLS:
    STATE.setdefault(s, {"positions":[],"pnl":0.0,"count":0,"avg_count":0,"volume_total":0.0,"last_avg_time":0})

def trade():
    global CYCLE_COUNT
    usdt = get_balance(); log(f"💰 Баланс: {usdt:.2f}")
    if usdt<RESERVE_BALANCE: return
    if sum(STATE[s]["pnl"] for s in SYMBOLS)<DAILY_LIMIT: return
    CYCLE_COUNT+=1; load_symbol_limits()
    for sym in SYMBOLS:
        try:
            df=get_kline(sym)
            if df.empty: continue
            sig, atr, reason = signal(df, sym)
            phase=detect_market_phase(df)
            price=df["c"].iloc[-1]
            state=STATE[sym]
            now=time.time()
            params=DEFAULT_PARAMS
            log(f"[{sym}] 📊 Фаза: {phase} | Сигнал: {sig} | {reason}")

            new_pos=[]
            for pos in state["positions"]:
                b,q,tp = pos["buy_price"],pos["qty"],pos["tp"]
                peak = max(pos.get("peak_price",b), price)
                pnl = (price-b)*q - price*q*0.001
                draw = (b-price)/b
                if price>=tp or (should_exit_by_rsi(df) and is_profitable_exit(pnl,price,q,params)) or (should_exit_by_trailing(price,peak,params) and is_profitable_exit(pnl,price,q,params)) or draw>=params["max_drawdown_sl"]:
                    reason_exit = "🎯 TP" if price>=tp else "📉 RSI" if should_exit_by_rsi(df) else "📉 Trail" if should_exit_by_trailing(price,peak,params) else "🔻 SL"
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, reason_exit)
                    state["pnl"]+=pnl
                    state["avg_count"]=0
                else:
                    pos["tp"]=calc_adaptive_tp(price,atr,q,params)
                    pos["peak_price"]=peak
                    new_pos.append(pos)
            state["positions"]=new_pos

            if sig=="buy" and phase!="bear" and len(state["positions"])<MAX_OPEN_POSITIONS:
                if phase=="sideways" and df["rsi"].iloc[-1]<=45: continue
                qty=calc_adaptive_qty(usdt,atr,price,sym,params["risk_pct"])
                amt=qty*price
                if qty and amt>=LIMITS[sym]["min_amt"] and len(state["positions"])==0:
                    tp=calc_adaptive_tp(price,atr,qty,params)
                    profit=(tp-price)*qty-price*qty*0.001
                    if profit>=params["min_profit_usdt"]:
                        session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                        state["positions"].append({"buy_price":price,"qty":qty,"tp":tp,"peak_price":price})
                        state["count"]+=1; state["volume_total"]+=amt; state["last_avg_time"]=now
                        log_trade(sym,"BUY",price,qty,0,reason)
                elif len(state["positions"]>0) and state["avg_count"]<MAX_AVG_COUNT and state["volume_total"]<MAX_POSITION_SIZE_USDT:
                    b=state["positions"][0]["buy_price"]
                    draw=(b-price)/b
                    if phase in ["bull","sideways"] and draw>=params["min_avg_drawdown"] and now-state["last_avg_time"]>=COOLDOWN_AVG_SECS and df["rsi"].iloc[-1]>50:
                        qty=calc_adaptive_qty(usdt/2,atr,price,sym,params["risk_pct"])
                        amt=qty*price
                        if qty and amt>=LIMITS[sym]["min_amt"]:
                            tp=calc_adaptive_tp(price,atr,qty,params)
                            profit=(tp-price)*qty-price*qty*0.001
                            if profit>=params["min_profit_usdt"]:
                                session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                                state["positions"].append({"buy_price":price,"qty":qty,"tp":tp,"peak_price":price})
                                state["avg_count"]+=1; state["volume_total"]+=amt; state["last_avg_time"]=now
                                log_trade(sym,"BUY AVG",price,qty,0,f"📉 Усреднение {draw:.2%}")

            if CYCLE_COUNT%30==0:
                cb=get_coin_balance(sym)
                if cb: log(f"[{sym}] 📦 Баланс: {cb:.2f} ({cb*price:.2f} USDT)")
        except Exception as e:
            log(f"[{sym}] ❌ Ошибка: {type(e).__name__} — {e}")
    save_state()

def daily_report():
    global LAST_REPORT_DATE
    now=datetime.datetime.now()
    if now.hour==22 and LAST_REPORT_DATE!=now.date():
        rep=f"📊 Отчёт за {now.date()}:\n\nМонета   | Сделок | PnL\n---------------------\n"
        total=0
        for sym,data in STATE.items():
            rep+=f"{sym:<10}| {data['count']:<6}| {data['pnl']:.2f} USDT\n"
            total+=data['pnl']
        rep+=f"--------------------\n💰 Баланс: {get_balance():.2f} USDT\n📈 Всего: {total:.2f} USDT"
        send_tg(rep); LAST_REPORT_DATE=now.date()

def main():
    send_tg("🚀 Бот запущен (агрессивный профиль v2 с улучшениями)")
    log("🚀 Бот запущен (агрессивный профиль v2 с улучшениями)")
    while True:
        try:
            trade()
            daily_report()
            time.sleep(60)
        except Exception as e:
            log(f"❌ Главная ошибка: {type(e).__name__} — {e}")
            time.sleep(60)

if __name__=="__main__":
    main()
