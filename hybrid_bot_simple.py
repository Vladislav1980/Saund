# bot_v3_2.py
# -*- coding: utf-8 -*-
"""
V3.2 — unified spot bot (Bybit) с фиксаторами:
- afford_qty: размер заявки считается от бюджета/баланса и приводится к лимитам биржи
- sell_protection: строгая продажа только при выполнении TP/минимального PnL; мягкий выход при deep-DD/реверсе
- cooldown per symbol: защита от частого дерганья (на покупку/усреднение/продажу)
- compact TG errors: схлопывание однотипных ошибок
- request signing fix: обязательные apiKey, recv_window, timestamp, подпись

ENV:
 BYBIT_API_KEY, BYBIT_API_SECRET
 TG_TOKEN, TG_CHAT_ID
 SYMBOLS="XRPUSDT,DOGEUSDT,TONUSDT"
 BUDGET_MIN=150   (usd)
 BUDGET_MAX=230   (usd)   -- берётся min(…, доступно)
 MIN_NET_PNL=1.0  (usd)
 SL_PCT=3.0       (stoploss от средней цены, %)
 TRAIL_X=1.5      (множитель ATR для трейла; можно оставить 1.5)
 COOLDOWN_SEC=30  (между ордерами одного символа)
 IND_WEIGHT="ema,rsi,macd"  (голосование 2 из 3)

Зависимости: requests
"""

import os, time, hmac, hashlib, math, json, threading, queue
import requests
from datetime import datetime, timezone
from statistics import mean

# ---------- CONFIG ----------

API_KEY     = os.getenv("BYBIT_API_KEY","")
API_SECRET  = os.getenv("BYBIT_API_SECRET","")
TG_TOKEN    = os.getenv("TG_TOKEN","")
TG_CHAT     = os.getenv("TG_CHAT_ID","")
SYMBOLS     = os.getenv("SYMBOLS","XRPUSDT,DOGEUSDT,TONUSDT").split(",")

BUDGET_MIN  = float(os.getenv("BUDGET_MIN", "150"))
BUDGET_MAX  = float(os.getenv("BUDGET_MAX", "230"))
MIN_NET_PNL = float(os.getenv("MIN_NET_PNL", "1.0"))
SL_PCT      = float(os.getenv("SL_PCT", "3.0"))/100.0
TRAIL_X     = float(os.getenv("TRAIL_X", "1.5"))
COOLDOWN    = int(os.getenv("COOLDOWN_SEC","30"))
INDS        = os.getenv("IND_WEIGHT","ema,rsi,macd").split(",")

BASE = "https://api.bybit.com"
RECV_WINDOW = 5000

session = requests.Session()
session.headers.update({"Content-Type":"application/json"})

# ---------- UTILS / TG ----------

def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3], "|", *a, flush=True)

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        session.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     json={"chat_id":TG_CHAT, "text":text, "disable_web_page_preview":True})
    except Exception:
        pass

_err_cache = {"msg":"","count":0,"ts":0}
def tg_error_compact(msg):
    """схлопываем одинаковые подряд ошибки"""
    now = time.time()
    if msg==_err_cache["msg"] and now-_err_cache["ts"]<60:
        _err_cache["count"]+=1
        _err_cache["ts"]=now
        return
    # отправляем накопленную
    if _err_cache["msg"] and _err_cache["count"]>0:
        tg_send(f"⚠️ {_err_cache['msg']} ×{_err_cache['count']+1}")
    _err_cache["msg"]=msg
    _err_cache["count"]=0
    _err_cache["ts"]=now
    tg_send("⚠️ "+msg)

# ---------- BYBIT SIGNED HTTP ----------

def _timestamp_ms():
    return int(time.time()*1000)

def _sign(payload: str) -> str:
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def _signed(method, path, params=None, body=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("API keys not set")
    ts = str(_timestamp_ms())
    params = params or {}
    params["api_key"] = API_KEY
    params["timestamp"] = ts
    params["recv_window"] = RECV_WINDOW
    if method=="GET":
        q = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sig = _sign(q)
        url = f"{BASE}{path}?{q}&sign={sig}"
        r = session.get(url, timeout=10)
    else:
        # Bybit v5 accepts query+body; мы кладём всё в body
        payload = params.copy()
        if body:
            payload.update(body)
        q = "&".join(f"{k}={payload[k]}" for k in sorted(payload))
        sig = _sign(q)
        payload["sign"] = sig
        r = session.post(f"{BASE}{path}", json=payload, timeout=10)
    if r.status_code==429:
        raise RuntimeError("Rate limit")
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    if str(data.get("retCode"))!="0":
        raise RuntimeError(f"{data.get('retCode')}:{data.get('retMsg')}")
    return data.get("result") or data

# ---------- MARKET / ACCOUNT ----------

_limits_cache={}
def load_limits():
    # по‑простому: живые шаги с /v5/market/instruments-info
    for sym in SYMBOLS:
        res = session.get(f"{BASE}/v5/market/instruments-info",
                          params={"category":"spot","symbol":sym}, timeout=10).json()
        if str(res.get("retCode"))!="0":
            raise RuntimeError("limits:"+res.get("retMsg","?"))
        info = res["result"]["list"][0]
        lot = info["lotSizeFilter"]
        prc = info["priceFilter"]
        min_qty = float(lot["minOrderQty"])
        step_qty = float(lot["basePrecision"]) if "basePrecision" in lot else float(lot["qtyStep"])
        min_amt = float(info["minOrderAmt"]) if "minOrderAmt" in info else 5.0
        _limits_cache[sym] = dict(min_qty=min_qty, qty_step=step_qty, min_amt=min_amt)
    log("Loaded limits:", _limits_cache)

def balances():
    res = _signed("POST","/v5/account/wallet-balance",
                  params={"accountType":"UNIFIED"})
    for a in res["list"]:
        for c in a["coin"]:
            if c["coin"]=="USDT":
                av = float(c["availableToWithdraw"])
                tl = float(c["walletBalance"])
                return av, tl
    return 0.0, 0.0

def position_qty(sym):
    # на споте реальной «позиции» нет; используем баланс базовой валюты
    base = sym.replace("USDT","")
    res = _signed("POST","/v5/account/wallet-balance",
                  params={"accountType":"UNIFIED"})
    qty=0.0
    for a in res["list"]:
        for c in a["coin"]:
            if c["coin"]==base:
                qty = float(c["walletBalance"])
    return qty

def last_price(sym):
    r = session.get(f"{BASE}/v5/market/tickers", params={"category":"spot","symbol":sym}, timeout=10).json()
    if str(r.get("retCode"))!="0":
        raise RuntimeError("ticker:"+r.get("retMsg","?"))
    return float(r["result"]["list"][0]["lastPrice"])

def klines(sym, interval="1"):
    r = session.get(f"{BASE}/v5/market/kline",
                    params={"category":"spot","symbol":sym,"interval":interval,"limit":200}, timeout=10).json()
    if str(r.get("retCode"))!="0":
        raise RuntimeError("kline:"+r.get("retMsg","?"))
    rows = r["result"]["list"]
    rows.sort(key=lambda x:int(x[0]))
    close = [float(x[4]) for x in rows]
    high  = [float(x[2]) for x in rows]
    low   = [float(x[3]) for x in rows]
    return close, high, low

# ---------- TECHS ----------

def ema(arr, n):
    if not arr: return 0.0
    k = 2/(n+1)
    e = arr[0]
    for v in arr[1:]:
        e = v*k + e*(1-k)
    return e

def rsi(arr, n=14):
    gains=[]; losses=[]
    for i in range(1,len(arr)):
        d = arr[i]-arr[i-1]
        gains.append(max(0,d)); losses.append(max(0,-d))
    if not gains: return 50.0
    avg_gain = mean(gains[-n:]) if gains else 0.0
    avg_loss = mean(losses[-n:]) if losses else 0.0
    if avg_loss==0: return 70.0
    rs = avg_gain/avg_loss
    return 100-100/(1+rs)

def macd(arr, f=12, s=26, sig=9):
    mac = ema(arr, f) - ema(arr, s)
    signal = ema([mac for _ in arr], sig)  # упрощённо
    return mac, signal

# ---------- SIGNALS (2 из 3) ----------

def vote_signal(sym):
    close, hi, lo = klines(sym, "1")
    p = close[-1]
    e9 = ema(close, 9); e21 = ema(close,21)
    r = rsi(close,14)
    m, sg = macd(close)
    votes_buy = 0; votes_sell = 0

    # ema cross
    if "ema" in INDS:
        if e9>e21: votes_buy+=1
        elif e9<e21: votes_sell+=1
    if "rsi" in INDS:
        if r>55: votes_buy+=1
        elif r<45: votes_sell+=1
    if "macd" in INDS:
        if m>sg: votes_buy+=1
        elif m<sg: votes_sell+=1

    if votes_buy>=2 and votes_buy>votes_sell:
        side="BUY"
    elif votes_sell>=2 and votes_sell>votes_buy:
        side="SELL"
    else:
        side=None
    conf = max(votes_buy, votes_sell)/3.0
    return side, conf, dict(price=p, EMA9=round(e9,4), EMA21=round(e21,4),
                            RSI=round(r,2), MACD=round(m,4), SIG=round(sg,4))

# ---------- ORDER SIZING (afford-qty) ----------

def clamp_qty(sym, qty, price):
    lim = _limits_cache[sym]
    # к шагу
    step = lim["qty_step"]
    if step>0:
        qty = math.floor(qty/step)*step
    # к min_qty
    qty = max(qty, lim["min_qty"])
    # min_amt по USDT
    if qty*price < lim["min_amt"]:
        qty = math.ceil(lim["min_amt"]/price/step)*step
    return round(qty, 8)

def calc_afford_qty(sym, price, budget_min=BUDGET_MIN, budget_max=BUDGET_MAX):
    avail, _ = balances()
    budget = min(budget_max, max(budget_min, 0.0))
    budget = min(budget, avail)  # не лезем выше доступного
    qty = budget/price
    qty = clamp_qty(sym, qty, price)
    return qty, budget, avail

# ---------- ORDERS (с обработкой 170131) ----------

def place_market(sym, side, qty):
    body = {
        "category":"spot",
        "symbol":sym,
        "side":"Buy" if side=="BUY" else "Sell",
        "orderType":"Market",
        "qty": str(qty)
    }
    try:
        res = _signed("POST","/v5/order/create", params={}, body=body)
        return res
    except RuntimeError as e:
        msg = str(e)
        if "170131" in msg or "Insufficient balance" in msg:
            return {"error":"INSUF","msg":msg}
        if "10001" in msg:
            # ошибка параметров — точно отправим с новым штампом/подписью
            time.sleep(0.4)
            try:
                res = _signed("POST","/v5/order/create", params={}, body=body)
                return res
            except Exception as e2:
                return {"error":"PARAM","msg":str(e2)}
        return {"error":"OTHER","msg":msg}

# ---------- STATE / COOLDOWN / TP & SL ----------

state = {sym: {"tp":None, "avg":None, "qty":None, "last_ts":0, "side":None} for sym in SYMBOLS}

def refresh_state(sym):
    qty = position_qty(sym)
    p = last_price(sym)
    st = state[sym]
    if (st["qty"] is None) or abs(st["qty"]-qty)>1e-8:
        # новая/изменённая позиция => сбрасываем трал
        st["avg"] = p if qty>0 else None
        st["tp"]  = None
    st["qty"] = qty
    # трэйлинг цель: от средней вверх на X*ATR (упрощим: процентом 0.35% * TRAIL_X)
    if qty>0:
        trail = p * (0.0035*TRAIL_X)
        st["tp"] = max(st["tp"] or 0.0, p+trail)
    else:
        st["tp"] = None
        st["avg"]= None
        st["side"]= None
    return qty, p, st["tp"]

def can_act(sym):
    return (time.time() - state[sym]["last_ts"]) > COOLDOWN

def mark_act(sym):
    state[sym]["last_ts"] = time.time()

# ---------- SELL PROTECTION & SOFT EXIT ----------

def should_strict_sell(sym, price):
    """строгая продажа: цена >= TP ИЛИ net_pnl>=MIN_NET_PNL"""
    st = state[sym]
    if not st["qty"] or not st["avg"]:
        return False
    tp_ok = st["tp"] and price >= st["tp"]
    net_pnl = (price - st["avg"]) * st["qty"]
    return tp_ok or net_pnl >= MIN_NET_PNL

def should_soft_exit(sym, price, conf_opposite):
    """мягкий выход: глубокая просадка ИЛИ сильный реверс сигналов"""
    st = state[sym]
    if not st["qty"] or not st["avg"]:
        return False
    dd = (price - st["avg"]) / st["avg"]  # <0 в просадке
    deep = dd <= -0.03  # -3% и глубже
    strong_rev = conf_opposite>=0.67  # 2/3 или 3/3
    return deep or strong_rev

# ---------- LOOP PER SYMBOL ----------

def run_symbol(sym):
    try:
        qty, price, tp = refresh_state(sym)
        side, conf, meta = vote_signal(sym)
        price = meta["price"]

        # SELL LOGIC (есть позиция?)
        if qty>0 and can_act(sym):
            opposite_side = "SELL" if side=="SELL" else None
            if should_strict_sell(sym, price):
                # строгая фиксация
                q = clamp_qty(sym, qty, price)
                res = place_market(sym, "SELL", q)
                if "error" in res:
                    tg_error_compact(f"[{sym}] sell strict err: {res['error']}: {res['msg']}")
                else:
                    mark_act(sym)
                    pnl = (price - state[sym]["avg"]) * q
                    tg_send(f"✅ {sym} SELL @ {price:.6f}, qty={q}, pnl={pnl:.2f}")
                    state[sym].update({"qty":qty-q})
                    return
            else:
                # мягкий выход?
                conf_opp = conf if opposite_side else 0.0
                if should_soft_exit(sym, price, conf_opp) and can_act(sym):
                    q = clamp_qty(sym, qty, price)
                    res = place_market(sym, "SELL", q)
                    if "error" in res:
                        tg_error_compact(f"[{sym}] sell soft err: {res['error']}: {res['msg']}")
                    else:
                        mark_act(sym)
                        pnl = (price - (state[sym]["avg"] or price)) * q
                        tg_send(f"⚠️ {sym} SOFT SELL @ {price:.6f}, qty={q}, pnl={pnl:.2f}")
                        state[sym].update({"qty":qty-q})
                        return

        # BUY / (re)ENTRY
        if side=="BUY" and can_act(sym):
            # бюджет и доступность
            q, budget, avail = calc_afford_qty(sym, price)
            if q*price < _limits_cache[sym]["min_amt"]-1e-6:
                return
            # если уже есть позиция — разрешим усреднение, но не чаще cooldown
            res = place_market(sym, "BUY", q)
            if "error" in res:
                if res["error"]=="INSUF":
                    # уменьшаем и пробуем снова минимумом
                    lim = _limits_cache[sym]
                    step = lim["qty_step"]
                    q2 = max(lim["min_qty"], math.floor((avail/price)/step)*step)
                    if q2*price >= lim["min_amt"]:
                        res2 = place_market(sym, "BUY", q2)
                        if "error" in res2:
                            tg_error_compact(f"[{sym}] buy err2: {res2['error']}: {res2['msg']}")
                        else:
                            mark_act(sym); tg_send(f"🟢 {sym} BUY @ {price:.6f}, qty={q2}")
                    else:
                        tg_error_compact(f"[{sym}] buy skip: insufficient after clamp (avail {avail:.2f})")
                else:
                    tg_error_compact(f"[{sym}] buy err: {res['error']}: {res['msg']}")
            else:
                mark_act(sym)
                tg_send(f"🟢 {sym} BUY @ {price:.6f}, qty={q}")
                state[sym]["side"]="LONG"

        # LOG (кратко)
        bal_av, _ = balances()
        tp_s = f"{tp:.6f}" if tp else "-"
        log(f"[{sym}]🔎 side={side or '-'} conf={conf:.2f} | price={price:.6f} | "
            f"qty={state[sym]['qty'] or 0:.6f} avg={state[sym]['avg'] or 0:.6f} TP={tp_s} | "
            f"USDT_avail={bal_av:.2f}")

    except Exception as e:
        tg_error_compact(f"[{sym}] loop error: {e}")

# ---------- MAIN ----------

def main():
    log("🚀 Бот запускается...")
    load_limits()
    tg_send("🚀 Старт бота. Восстановление состояния: FRESH")
    while True:
        for s in SYMBOLS:
            run_symbol(s)
            time.sleep(0.3)  # лёгкая рассинхронизация
        time.sleep(2)

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Bye")
