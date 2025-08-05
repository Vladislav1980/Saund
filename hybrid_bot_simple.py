import os, time, math, logging, datetime, requests, json, pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
REDIS_URL = os.getenv("REDIS_URL")

TG_VERBOSE = True
RESERVE_BALANCE = 1.0
MAX_TRADE_USDT = 70.0
MIN_PRICE_DIFF_PCT = 0.003
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2
MIN_NET_PROFIT = 1.50
STOP_LOSS_PCT = 0.008
TAKE_PROFIT_PCT = 0.02

TAKER_BUY_FEE = 0.0010   # 0.10%
TAKER_SELL_FEE = 0.0018  # 0.18%

SYMBOLS = ["TONUSDT","DOGEUSDT","XRPUSDT","WIFUSDT"]
LAST_REPORT_DATE = None
cycle_count = 0

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()])

SKIP_LOG_TIMESTAMPS = {}

def should_log_skip(sym, key, interval=15):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval*60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

def log_skip(sym, msg):
    logging.info(f"{sym}: {msg}")

def send_tg(msg):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log("✅ Loaded full state from Redis" if STATE else "ℹ Starting fresh, no previous state", True)
    ensure_state_consistency()

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {"positions": [], "pnl":0.0, "count":0,
                               "avg_count":0, "last_sell_price":0.0,
                               "max_drawdown":0.0, "last_stop_time":""})

def load_symbol_limits():
    resp = session.get_instruments_info(category="spot")["result"]["list"]
    return {item["symbol"]:{
        "min_qty": float(item.get("lotSizeFilter",{}).get("minOrderQty",0.0)),
        "qty_step": float(item.get("lotSizeFilter",{}).get("qtyStep",1.0)),
        "min_amt": float(item.get("minOrderAmt",10.0))
    } for item in resp if item["symbol"] in SYMBOLS}

LIMITS = load_symbol_limits()

def adjust_qty(qty, step):
    try:
        exp = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exp)) / (10**abs(exp))
    except:
        return qty

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log(msg, True)
    with open("trades.csv","a",encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.4f},{qty},{pnl:.2f},{info}\n")
    save_state()

def signal(df):
    if df.empty or len(df)<50:
        return "none", 0, ""
    df["ema9"] = EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"],9).rsi()
    df["atr"] = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    buy_cnt = sum([last["ema9"]>last["ema21"], last["rsi"]>50, last["macd"]>last["macd_signal"]])
    sell_cnt = sum([last["ema9"]<last["ema21"], last["rsi"]<50, last["macd"]<last["macd_signal"]])
    info = f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['macd_signal']:.4f}"
    return ("buy", last["atr"], info) if buy_cnt>=2 else ("sell", last["atr"], info) if sell_cnt>=2 else ("none", last["atr"], info)

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balance():
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))

def get_coin_balance(sym):
    co = sym.replace("USDT","")
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next((c["walletBalance"] for c in coins if c["coin"]==co),0.0))

def get_qty(sym, price, usdt):
    if sym not in LIMITS: return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc/price, LIMITS[sym]["qty_step"])
    return q if q>=LIMITS[sym]["min_qty"] and q*price>=LIMITS[sym]["min_amt"] else 0.0

def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds()/3600
    except:
        return 999.0

def init_positions():
    total = 0.0; msgs = []
    for sym in SYMBOLS:
        bal = get_coin_balance(sym)
        log(f"DEBUG balance {sym}: {bal:.6f}", True)
        df = get_kline(sym)
        if bal>0 and df is not None and not df.empty:
            price = df["c"].iloc[-1]
            if bal*price >= LIMITS[sym]["min_amt"]:
                qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
                atr = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
                tp = price + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"].append({"buy_price":price,"qty":qty,"tp":tp,"timestamp":datetime.datetime.now().isoformat()})
                total += qty*price
                msgs.append(f"- {sym}: {qty} @ {price:.4f} (${qty*price:.2f})")
    if not STATE["TONUSDT"]["positions"]:
        pr, q = 3.596, 5.28
        tp_val = pr + (MIN_NET_PROFIT/q)
        STATE["TONUSDT"]["positions"].append({"buy_price":pr,"qty":q,"tp":tp_val,"timestamp":datetime.datetime.now().isoformat()})
        msgs.append(f"- TONUSDT restored: {q} @ {pr:.4f} (${q*pr:.2f})")
        total += q*pr
        log("🧩 TONUSDT manually restored", True)
    save_state()
    header = "🚀 Bot started\n"
    header += "💰 Active positions:\n" + "\n".join(msgs) + f"\n📊 Total: ${total:.2f}" if msgs else "💰 No active positions"
    log(header, True)

def trade():
    global cycle_count, LAST_REPORT_DATE
    usdt = get_balance()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0

    for sym in SYMBOLS:
        st = STATE[sym]; st["sell_failed"] = False
        df = get_kline(sym)
        if df.empty:
            continue
        sig, atr, info = signal(df)
        price = df["c"].iloc[-1]
        coin_bal = get_coin_balance(sym)
        value = coin_bal * price
        logging.info(f"[{sym}] sig={sig}, price={price:.4f}, value={value:.2f}, pos={len(st['positions'])}, {info}")

        # Stop-loss / Take-profit
        for pos in st["positions"]:
            b, q = pos["buy_price"], pos["qty"]
            cost = b*q
            buy_comm = cost * TAKER_BUY_FEE
            sell_comm = price*q * TAKER_SELL_FEE
            pnl = (price - b)*q - (buy_comm + sell_comm)
            if q>0 and price <= b*(1-STOP_LOSS_PCT) and abs(pnl)>=MIN_NET_PROFIT:
                if coin_bal>=q:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                    log_trade(sym,"STOP LOSS SELL",price,q,pnl,"stop‑loss")
                    st["pnl"] += pnl
                else:
                    log(f"SKIPPED STOP SELL {sym}: insufficient balance", True)
                    st["sell_failed"] = True
                st["last_stop_time"] = datetime.datetime.now().isoformat(); st["avg_count"]=0
                break
            if q>0 and price >= b*(1+TAKE_PROFIT_PCT) and pnl>=MIN_NET_PROFIT:
                if coin_bal>=q:
                    session.place_order(category="spot",symbol=sym,side="Sell",orderType="Market",qty=str(q))
                    log_trade(sym,"TP SELL",price,q,pnl,"take‑profit")
                    st["pnl"] += pnl
                st["positions"], st["avg_count"], st["sell_failed"], st["last_sell_price"] = [],0,False,price
                break
            pos["tp"] = max(pos.get("tp",price), price + TRAIL_MULTIPLIER * atr)

        if st.get("sell_failed"):
            st["positions"] = []

        # BUY logic: if no position and signal and enough USDT
        if sig=="buy" and not st["positions"]:
            last_stop = st.get("last_stop_time","")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs<4:
                if should_log_skip(sym,"stop_buy"):
                    log_skip(sym, f"Skipped BUY — only {hrs:.1f}h since last stop-loss")
            elif avail >= LIMITS[sym]["min_amt"]:
                qty = get_qty(sym, price, avail)
                if qty>0:
                    cost = price*qty
                    buy_comm = cost*TAKER_BUY_FEE
                    # Estimate PnL threshold for later sale
                    tp_price = price*(1+TAKE_PROFIT_PCT)
                    estimated_sell_comm = tp_price*qty*TAKER_SELL_FEE
                    est_pnl = (tp_price-price)*qty - (buy_comm+estimated_sell_comm)
                    if est_pnl >= MIN_NET_PROFIT:
                        session.place_order(category="spot",symbol=sym,side="Buy",orderType="Market",qty=str(qty))
                        STATE[sym]["positions"].append({"buy_price":price,"qty":qty,"tp":price+TRAIL_MULTIPLIER*atr,"timestamp":datetime.datetime.now().isoformat()})
                        log_trade(sym,"BUY",price,qty,0.0,"initiated position from USDT")
                        avail -= cost
                    else:
                        if should_log_skip(sym,"skip_low_profit"):
                            log_skip(sym,"Skipped BUY — expected PnL too small")
                else:
                    if should_log_skip(sym,"skip_qty"):
                        log_skip(sym,"Skipped BUY — qty=0 or below min")
            else:
                if should_log_skip(sym,"skip_funds"):
                    log_skip(sym,"Skipped BUY — not enough free USDT")

    cycle_count += 1
    if datetime.datetime.now().hour==22 and datetime.datetime.now().minute>=30 and LAST_REPORT_DATE != datetime.datetime.now().date():
        report = "📊 Daily report:\n" + "\n".join(f"{s}: PnL={STATE[s]['pnl']:.2f}" for s in SYMBOLS)
        send_tg(report)
        LAST_REPORT_DATE = datetime.datetime.now().date()

if __name__=="__main__":
    init_state()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"Global error: {e}", True)
        time.sleep(60)
