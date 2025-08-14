# -*- coding: utf-8 -*-
# trade_v3_2.py — Bybit Spot bot (simple & solid)
# — Signals: "2 of 3" (EMA/RSI/MACD)
# — Net PnL >= max(1 USDT, min_profit_pct) AFTER BOTH taker fees
# — Floating budget (150–230) with afford-qty & exchange limits
# — Trailing TP by ATR, soft SL
# — Anti "Insufficient balance": cap sell qty by real coin balance
# — File state (JSON), compact Telegram events, daily report
# — Minimal safe helpers (wallet cache, per-symbol cooldown)

import os, time, math, json, logging, datetime, traceback
import requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# -------------------- CONFIG --------------------
load_dotenv()
API_KEY   = os.getenv("BYBIT_API_KEY", "")
API_SECRET= os.getenv("BYBIT_API_SECRET", "")
TG_TOKEN  = os.getenv("TG_TOKEN", "")
CHAT_ID   = os.getenv("CHAT_ID", "")

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

TAKER_FEE = 0.0018           # spot taker
RESERVE_USDT = 1.0           # не трогаем
BUDGET_MIN  = 150.0
BUDGET_MAX  = 230.0
MIN_PROFIT_PCT = 0.005       # 0.5% как нижняя рамка
MIN_NET_USDT  = 1.0          # <== обязательная «минимум $1 net»

TRAIL_X = 1.5                # ATR multiplier
STOP_LOSS_PCT = 0.03         # 3%
MAX_AVERAGES  = 3
MAX_DRAWDOWN  = 0.15         # 15% от средней
REENTRY_LOCK  = 0.003        # 0.3% защита от мгновенного повторного входа

INTERVAL = "1"
LOOP_SLEEP = 60
STATE_FILE = "state.json"

DAILY_REPORT_HOUR   = 22
DAILY_REPORT_MINUTE = 30

WALLET_CACHE_TTL = 5.0       # сек
ORDER_COOLDOWN    = 5.0      # сек между ордерами по одному символу

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"),
              logging.StreamHandler()]
)

def tg(msg:str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        logging.error("Telegram send failed")

# -------------------- Session --------------------
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# -------------------- State ----------------------
STATE = {}
LIMITS = {}
LAST_REPORT = None
_last_order_ts = {s:0.0 for s in SYMBOLS}

def _default_state():
    return {"positions":[], "pnl":0.0, "count":0, "avg_count":0,
            "last_sell_price":0.0, "max_drawdown":0.0}

def load_state():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        logging.info("♻️ Loaded state from file")
    except Exception:
        STATE = {}
        logging.info("ℹ️ No state file — fresh start")
    for s in SYMBOLS: STATE.setdefault(s, _default_state())

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"save_state error: {e}")

# -------------------- Exchange helpers --------------------
def load_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        sym = item["symbol"]
        if sym not in SYMBOLS: continue
        f = item.get("lotSizeFilter", {})
        LIMITS[sym] = {
            "min_qty": float(f.get("minOrderQty", 0.0)),
            "qty_step": float(f.get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 5.0))
        }
    logging.info(f"Loaded limits: {LIMITS}")

def _round_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except Exception:
        return qty

def _cap_to_step(value, step):
    return _round_qty(value, step)

_wallet_cache = {"ts":0.0, "coins":None}
def _get_wallet(force=False):
    if (not force and _wallet_cache["coins"] is not None and
        time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL):
        return _wallet_cache["coins"]
    r = session.get_wallet_balance(accountType="UNIFIED")
    coins = r["result"]["list"][0]["coin"]
    _wallet_cache.update(ts=time.time(), coins=coins)
    return coins

def get_usdt(coins): return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))
def get_coin(coins, sym):
    base = sym.replace("USDT","")
    return float(next((c["walletBalance"] for c in coins if c["coin"]==base), 0.0))

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

# -------------------- Signals --------------------
def _last_cross(fast, slow, lookback=3):
    for i in range(1, min(lookback+1, len(fast))):
        a0,a1 = fast.iloc[-i-1], fast.iloc[-i]
        b0,b1 = slow.iloc[-i-1], slow.iloc[-i]
        if a0<=b0 and a1>b1:  return 1
        if a0>=b0 and a1<b1:  return -1
    return 0

def calc_signal(df):
    if df.empty or len(df) < 50: return "none",0.0,"insufficient",0.0
    df["ema9"]=EMAIndicator(df["c"],9).ema_indicator()
    df["ema21"]=EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]=RSIIndicator(df["c"],9).rsi()
    df["atr"]=AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range()
    macd=MACD(close=df["c"]); df["macd"]=macd.macd(); df["sig"]=macd.macd_signal()
    last=df.iloc[-1]
    info=f"EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.2f}, MACD={last['macd']:.4f}, SIG={last['sig']:.4f}"
    buy=sell=0
    if last["ema9"]>last["ema21"]: buy+=1
    else: sell+=1
    if last["rsi"]>45: buy+=1
    if last["rsi"]<55: sell+=1
    if last["macd"]>last["sig"]: buy+=1
    if last["macd"]<last["sig"]: sell+=1
    cross=_last_cross(df["macd"],df["sig"],3)
    sig="buy" if (buy>=2 or cross==1) else "sell" if (sell>=2 or cross==-1) else "none"
    ema_gap=abs(last["ema9"]-last["ema21"])/max(abs(last["ema21"]),1e-12)
    ema_score=max(0.0,min(1.0,ema_gap*10))
    pts=(buy if sig=="buy" else sell)/3.0 if sig!="none" else 0.0
    rsi_part=max(0.0,min(1.0,(last["rsi"]-45)/25)) if sig=="buy" else max(0.0,min(1.0,(55-last["rsi"])/25)) if sig=="sell" else 0.0
    conf=max(0.0,min(1.0,0.5*pts+0.3*ema_score+0.2*rsi_part))
    return sig,float(last["atr"]),info,conf

# -------------------- Utils --------------------
def budget_from_conf(conf, avail):
    want = BUDGET_MIN + (BUDGET_MAX-BUDGET_MIN)*max(0.0,min(1.0,conf))
    return max(0.0, min(avail, want))

def qty_from_budget(sym, price, budget):
    lim = LIMITS[sym]
    q = _cap_to_step(budget/price, lim["qty_step"])
    if q < lim["min_qty"] or q*price < lim["min_amt"]:
        return 0.0
    return q

def log_trade(sym, side, price, qty, pnl, reason=""):
    usdt = price*qty
    msg = f"{side} {sym} @ {price:.6f}, qty={qty:.8f}, USDT≈{usdt:.2f}, PnL(net)={pnl:.2f} | {reason}"
    logging.info(msg)
    try:
        with open("trades.csv","a",encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt},{pnl},{reason}\n")
    except: pass
    save_state()

def respect_cooldown(sym):
    now = time.time()
    if now - _last_order_ts.get(sym, 0.0) < ORDER_COOLDOWN:
        return False
    _last_order_ts[sym] = now
    return True

# -------------------- Init positions from wallet --------------------
def init_positions_from_wallet():
    coins = _get_wallet(force=True)
    for sym in SYMBOLS:
        try:
            bal = get_coin(coins, sym)
            if bal <= 0: continue
            price = float(get_kline(sym)["c"].iloc[-1])
            if bal*price < LIMITS[sym]["min_amt"]: continue
            q_net = _cap_to_step(bal, LIMITS[sym]["qty_step"])
            atr = AverageTrueRange(get_kline(sym)["h"],
                                   get_kline(sym)["l"],
                                   get_kline(sym)["c"],14).average_true_range().iloc[-1]
            tp = price + TRAIL_X*atr
            STATE[sym]["positions"].append({"buy_price":price, "qty":q_net,
                                            "buy_qty_gross": q_net/(1-TAKER_FEE),
                                            "tp":tp})
            logging.info(f"♻️ [{sym}] recovered qty={q_net:.8f} @ {price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] recover error: {e}")
    save_state()

# -------------------- Daily report --------------------
def daily_report():
    try:
        coins = _get_wallet(force=True)
        by = {c["coin"]: float(c["walletBalance"]) for c in coins}
        usdt = by.get("USDT", 0.0)
        lines = [f"📊 Daily Report {datetime.date.today()}",
                 f"USDT: {usdt:.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT","")
            bal = by.get(base, 0.0)
            price = float(get_kline(sym)["c"].iloc[-1])
            val = price*bal
            s = STATE[sym]
            cur = sum(p["qty"] for p in s["positions"])
            lines.append(f"{sym}: balance={bal:.6f} (~{val:.2f} USDT), trades={s['count']}, "
                         f"pnl={s['pnl']:.2f}, DDmax={s['max_drawdown']*100:.2f}%, curPos={cur:.6f}")
        tg("\n".join(lines))
    except Exception as e:
        logging.info(f"report error: {e}")

# -------------------- Core cycle --------------------
def trade_cycle():
    global LAST_REPORT
    coins = _get_wallet(force=True)
    usdt = get_usdt(coins)
    avail = max(0.0, usdt - RESERVE_USDT)
    logging.info(f"💰 USDT={usdt:.2f} | Avail={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: 
                logging.info(f"[{sym}] no kline, skip")
                continue

            sig, atr, info, conf = calc_signal(df)
            price = df["c"].iloc[-1]
            st = STATE[sym]; lim = LIMITS[sym]
            coin_bal = get_coin(coins, sym); value = coin_bal*price

            logging.info(f"[{sym}] 🔎 {sig.upper()} (conf={conf:.2f}), price={price:.6f}, "
                         f"bal={coin_bal:.8f} (~{value:.2f}) | {info}")

            # drawdown tracking
            if st["positions"]:
                avg_entry = sum(p["buy_price"]*p["qty"] for p in st["positions"]) / \
                            sum(p["qty"] for p in st["positions"])
                dd = (avg_entry - price) / max(avg_entry, 1e-12)
                if dd > st["max_drawdown"]: st["max_drawdown"] = dd

            new_pos = []
            # -------- SELL logic per lot --------
            for pos in st["positions"]:
                b   = pos["buy_price"]
                q_n = _cap_to_step(pos["qty"], lim["qty_step"])
                tp  = pos["tp"]
                buy_gross = pos.get("buy_qty_gross", q_n/(1-TAKER_FEE))

                # Net cost & proceeds
                cost_usdt     = b * buy_gross
                proceeds_usdt = price * q_n * (1 - TAKER_FEE)
                pnl_net = proceeds_usdt - cost_usdt

                min_req = max(MIN_NET_USDT, price*q_n*MIN_PROFIT_PCT)

                # Stop-loss
                if price <= b*(1-STOP_LOSS_PCT):
                    # cap by real balance to avoid err 170131
                    coins_now = _get_wallet(force=True)
                    real_bal  = get_coin(coins_now, sym)
                    sellable  = _cap_to_step(max(0.0, real_bal - lim["qty_step"]), lim["qty_step"])
                    qty_to_sell = min(q_n, sellable)
                    if qty_to_sell >= lim["min_qty"]:
                        if respect_cooldown(sym):
                            session.place_order(category="spot", symbol=sym, side="Sell",
                                                orderType="Market", qty=str(qty_to_sell))
                        log_trade(sym, "SELL(SL)", price, qty_to_sell, pnl_net,
                                  f"SL b={b:.6f} → p={price:.6f}")
                        tg(f"❗{sym} SELL(SL) {qty_to_sell:.8f} @ {price:.6f} | pnl={pnl_net:.2f}")
                        st["pnl"] += pnl_net
                        st["last_sell_price"] = price
                        st["avg_count"] = 0
                    continue

                # Take-profit (strictly net >= thresholds)
                if price >= tp and pnl_net >= min_req:
                    # cap by real balance
                    coins_now = _get_wallet(force=True)
                    real_bal  = get_coin(coins_now, sym)
                    sellable  = _cap_to_step(max(0.0, real_bal - lim["qty_step"]), lim["qty_step"])
                    qty_to_sell = min(q_n, sellable)
                    if qty_to_sell >= lim["min_qty"]:
                        if respect_cooldown(sym):
                            session.place_order(category="spot", symbol=sym, side="Sell",
                                                orderType="Market", qty=str(qty_to_sell))
                        log_trade(sym, "SELL", price, qty_to_sell, pnl_net,
                                  f"TP {tp:.6f}, net≥{min_req:.2f}")
                        tg(f"✅ {sym} SELL {qty_to_sell:.8f} @ {price:.6f} | pnl={pnl_net:.2f}")
                        st["pnl"] += pnl_net
                        st["last_sell_price"] = price
                        st["avg_count"] = 0
                else:
                    # trail TP
                    new_tp = max(tp, price + TRAIL_X*atr)
                    if new_tp != tp:
                        logging.info(f"[{sym}] 📈 TP: {tp:.6f} → {new_tp:.6f}")
                    pos["tp"] = new_tp
                    new_pos.append(pos)
                    if price < tp:
                        logging.info(f"[{sym}] ▪️hold: price {price:.6f} < TP {tp:.6f}")
                    elif pnl_net < min_req:
                        logging.info(f"[{sym}] ▪️hold: net {pnl_net:.2f} < need {min_req:.2f}")

            st["positions"] = new_pos

            # -------- BUY logic --------
            if sig == "buy":
                # averaging
                if st["positions"] and st["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty"] for p in st["positions"])
                    avg_px  = sum(p["qty"]*p["buy_price"] for p in st["positions"]) / total_q
                    dd = (price - avg_px)/max(avg_px,1e-12)
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN:
                        budget = budget_from_conf(conf, avail)
                        qty_g  = qty_from_budget(sym, price, budget)
                        if qty_g > 0 and qty_g*price <= (usdt-RESERVE_USDT+1e-9):
                            if respect_cooldown(sym):
                                session.place_order(category="spot", symbol=sym, side="Buy",
                                                    orderType="Market", qty=str(qty_g))
                            qty_n = qty_g*(1-TAKER_FEE)
                            tp = price + TRAIL_X*atr
                            st["positions"].append({"buy_price":price,"qty":qty_n,
                                                    "buy_qty_gross":qty_g,"tp":tp})
                            st["count"] += 1; st["avg_count"] += 1
                            log_trade(sym,"BUY(avg)",price,qty_n,0.0,
                                      f"dd={dd:.4f}, budget={budget:.2f}, qty_g={qty_g:.8f}")
                            tg(f"🟢 {sym} BUY(avg) {qty_n:.8f} @ {price:.6f}")
                            # refresh wallet snapshot
                            coins = _get_wallet(force=True); usdt = get_usdt(coins); avail = max(0, usdt-RESERVE_USDT)
                        else:
                            logging.info(f"[{sym}] skip avg: affordability/limits")
                    else:
                        logging.info(f"[{sym}] skip avg: dd {dd:.4f} outside (-{MAX_DRAWDOWN})")
                # fresh entry
                elif not st["positions"]:
                    if st["last_sell_price"] and abs(price-st["last_sell_price"])/price < REENTRY_LOCK:
                        logging.info(f"[{sym}] reentry-locked near last sell {st['last_sell_price']:.6f}")
                    else:
                        budget = budget_from_conf(conf, avail)
                        qty_g  = qty_from_budget(sym, price, budget)
                        if budget >= BUDGET_MIN and qty_g > 0 and qty_g*price <= (usdt-RESERVE_USDT+1e-9):
                            if respect_cooldown(sym):
                                session.place_order(category="spot", symbol=sym, side="Buy",
                                                    orderType="Market", qty=str(qty_g))
                            qty_n = qty_g*(1-TAKER_FEE)
                            tp = price + TRAIL_X*atr
                            st["positions"].append({"buy_price":price,"qty":qty_n,
                                                    "buy_qty_gross":qty_g,"tp":tp})
                            st["count"] += 1
                            log_trade(sym,"BUY",price,qty_n,0.0,
                                      f"conf={conf:.2f}, budget={budget:.2f}, qty_g={qty_g:.8f}")
                            tg(f"🟢 {sym} BUY {qty_n:.8f} @ {price:.6f}")
                            coins = _get_wallet(force=True); usdt = get_usdt(coins); avail = max(0, usdt-RESERVE_USDT)
                        else:
                            logging.info(f"[{sym}] skip buy: budget/limits/affordability")
            else:
                if not st["positions"]:
                    logging.info(f"[{sym}] no buy: sig={sig}, conf={conf:.2f}")

        except Exception as e:
            logging.error(f"[{sym}] cycle error: {e}\n{traceback.format_exc(limit=2)}")
            tg(f"[{sym}] cycle error: {e}")

    save_state()

    # daily report
    now = datetime.datetime.now()
    if now.hour==DAILY_REPORT_HOUR and now.minute>=DAILY_REPORT_MINUTE and LAST_REPORT != now.date():
        daily_report()
        globals()['LAST_REPORT'] = now.date()

# -------------------- Main --------------------
if __name__ == "__main__":
    logging.info("🚀 Bot starting…")
    tg("🚀 Bot started (v3.2)")
    load_state()
    load_limits()
    init_positions_from_wallet()
    tg(f"⚙️ Params: TAKER={TAKER_FEE}, BUDGET=[{BUDGET_MIN};{BUDGET_MAX}], "
       f"TRAILx={TRAIL_X}, SL={STOP_LOSS_PCT*100:.1f}%, DD={MAX_DRAWDOWN*100:.0f}%, reentry>{REENTRY_LOCK*100:.1f}%")

    while True:
        try:
            trade_cycle()
        except Exception as e:
            logging.error(f"Global error: {e}\n{traceback.format_exc(limit=2)}")
            tg(f"Global error: {e}")
        time.sleep(LOOP_SLEEP)
