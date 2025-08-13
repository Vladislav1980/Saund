#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, hmac, json, math, hashlib, random, threading
import requests
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

# ---------- Config ----------
BYBIT_BASE    = os.getenv("BYBIT_BASE", "https://api.bybit.com")
API_KEY       = os.getenv("BYBIT_API_KEY", "")
API_SECRET    = os.getenv("BYBIT_API_SECRET", "")
RECV_WINDOW   = "5000"

TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")

REDIS_URL     = os.getenv("REDIS_URL", "")

SYMBOLS       = [s.strip().upper() for s in os.getenv("SYMBOLS","XRPUSDT,DOGEUSDT,TONUSDT").split(",") if s.strip()]

TAKER_FEE     = float(os.getenv("TAKER_FEE", "0.0018"))
BUDGET_MIN    = float(os.getenv("BUDGET_MIN", "150"))
BUDGET_MAX    = float(os.getenv("BUDGET_MAX", "230"))

TRAIL_X       = float(os.getenv("TRAIL_X", "1.5"))      # во сколько раз отступ к улучшенной цене
SL_PCT        = float(os.getenv("SL_PCT", "3.0"))/100.0 # safety стоп %
MAX_DD_DCA    = float(os.getenv("MAX_DD_DCA", "0.15"))  # 0.15 → 15%
MIN_NET_PNL   = float(os.getenv("MIN_NET_PNL", "1.0"))
COOLDOWN_SEC  = int(os.getenv("COOLDOWN_SEC", "20"))

LOG_EVERY     = 15   # секунд — частота «живых» логов

# ---------- Light Redis wrapper (optional) ----------
try:
    import redis
    R = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    R = None

# ---------- Helpers ----------
def now_ms() -> str:
    return str(int(time.time() * 1000))

def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def hmac_hex(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def http_headers(payload: dict) -> Dict[str, str]:
    t = now_ms()
    body = json.dumps(payload, separators=(",", ":"))
    sign = hmac_hex(API_SECRET, t + API_KEY + RECV_WINDOW + body)
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": t,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json",
    }

def http_get(path: str, params: dict) -> dict:
    # Bybit v5 GET подпись — в заголовках тело пустое, сигна по пустому payload.
    t = now_ms()
    sign = hmac_hex(API_SECRET, t + API_KEY + RECV_WINDOW + "")
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": t,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign,
    }
    url = BYBIT_BASE + path
    r = requests.get(url, params=params, headers=headers, timeout=10)
    return r.json()

def http_post(path: str, payload: dict) -> dict:
    url = BYBIT_BASE + path
    r = requests.post(url, data=json.dumps(payload), headers=http_headers(payload), timeout=10)
    return r.json()

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID: 
        return
    try:
        url=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text[:4000]}, timeout=7)
    except Exception:
        pass

# ---------- Indicators ----------
def ema(values, period):
    k = 2/(period+1)
    ema_val = None
    out = []
    for v in values:
        if ema_val is None: ema_val = v
        else: ema_val = v*k + ema_val*(1-k)
        out.append(ema_val)
    return out

def rsi(values, period=14):
    gains, losses = 0.0, 0.0
    rsis = []
    prev = None
    for i,v in enumerate(values):
        if prev is None:
            rsis.append(50.0)
        else:
            ch = v-prev
            gains = (gains*(period-1) + (ch if ch>0 else 0))/period
            losses = (losses*(period-1) + (-ch if ch<0 else 0))/period
            rs = gains/(losses+1e-9)
            rsis.append(100 - (100/(1+rs)))
        prev = v
    return rsis

def macd(values, fast=12, slow=26, signal=9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [a-b for a,b in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m-s for m,s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist

# ---------- Market ----------
def last_price(symbol: str) -> float:
    j = http_get("/v5/market/tickers", {"category":"spot", "symbol": symbol})
    if j.get("retCode")==0 and j.get("result",{}).get("list"):
        return float(j["result"]["list"][0]["lastPrice"])
    raise RuntimeError(f"no price for {symbol}: {j}")

def get_limits() -> Dict[str, dict]:
    # простая матрица лимитов по символам через /v5/market/instruments-info
    out = {}
    for s in SYMBOLS:
        j = http_get("/v5/market/instruments-info", {"category":"spot","symbol":s})
        try:
            it = j["result"]["list"][0]
            step = float(it["lotSizeFilter"]["basePrecision"])
            min_amt = float(it["lotSizeFilter"]["minOrderAmt"])
            # эмпирически минимальный шаг по количеству (basePrecision) → округление вниз
            out[s] = {"qty_step": step, "min_amt": min_amt}
        except Exception:
            # запасной вариант (чтоб не падать)
            out[s] = {"qty_step": 0.01, "min_amt": 5.0}
    return out

def wallet_usdt() -> float:
    j = http_get("/v5/account/wallet-balance", {"accountType":"UNIFIED"})
    if j.get("retCode")==0:
        for c in j["result"]["list"][0]["coin"]:
            if c["coin"]=="USDT":
                return float(c["free"])
    raise RuntimeError(f"wallet error: {j}")

def wallet_free_coin(coin: str) -> float:
    j = http_get("/v5/account/wallet-balance", {"accountType":"UNIFIED"})
    if j.get("retCode")==0:
        for c in j["result"]["list"][0]["coin"]:
            if c["coin"]==coin:
                return float(c["free"])
    return 0.0

# ---------- State ----------
@dataclass
class Pos:
    qty: float = 0.0
    avg: float = 0.0
    tp: float  = 0.0

state: Dict[str, Pos] = defaultdict(Pos)  # per symbol
cooldown_until: Dict[str, float] = defaultdict(float)

def coin_of(symbol:str) -> str:
    return symbol.replace("USDT","")

def save_state(symbol:str):
    if not R: return
    key=f"v3.2:{symbol}"
    R.hmset(key, {"qty": state[symbol].qty, "avg": state[symbol].avg, "tp": state[symbol].tp})
    R.expire(key, 7*24*3600)

def load_state(symbol:str):
    if not R: return
    key=f"v3.2:{symbol}"
    if R.exists(key):
        d=R.hgetall(key)
        state[symbol]=Pos(qty=float(d["qty"]), avg=float(d["avg"]), tp=float(d["tp"]))

# ---------- Logic pieces ----------
def soft_signal(prices:list) -> Tuple[str, float, dict]:
    """Возвращает ('buy'|'sell'|'hold', confidence, diag) по 2‑из‑3 индикаторам."""
    if len(prices)<40: return "hold", 0.0, {}
    ema9  = ema(prices, 9)
    ema21 = ema(prices, 21)
    r = rsi(prices, 14)
    m, s, h = macd(prices)

    bullish = 0
    bearish = 0
    # 1) EMA9 vs EMA21 + наклон
    if ema9[-1] > ema21[-1] and ema9[-1] > ema9[-2]: bullish += 1
    if ema9[-1] < ema21[-1] and ema9[-1] < ema9[-2]: bearish += 1
    # 2) RSI
    if r[-1] < 38: bullish += 1
    elif r[-1] > 62: bearish += 1
    # 3) MACD hist
    if h[-1] > 0 and h[-1] > h[-2]: bullish += 1
    if h[-1] < 0 and h[-1] < h[-2]: bearish += 1

    diag = {"EMA9": round(ema9[-1],4), "EMA21": round(ema21[-1],4), "RSI": round(r[-1],2),
            "MACD": round(m[-1],4), "SIG": round(s[-1],4)}

    if bullish>=2 and bearish==0:
        conf = 0.5 + 0.25*(r[-2] - r[-1] < 0) + 0.25*(ema9[-1]-ema21[-1] > ema21[-1]-ema21[-2])
        return "buy", min(1.0, conf), diag
    if bearish>=2 and bullish==0:
        conf = 0.5 + 0.25*(r[-2] - r[-1] > 0) + 0.25*(ema21[-1]-ema9[-1] > ema21[-1]-ema21[-2])
        return "sell", min(1.0, conf), diag
    return "hold", 0.0, diag

def trailing_tp(symbol:str, price:float):
    """Обновляем TP если улучшилось; возвращаем bool, пора продавать?"""
    p = state[symbol]
    if p.qty <= 0: return False
    # если tp ещё пуст — поставим стартовый как avg*(1 + fee + 0.0005)
    if p.tp <= 0:
        p.tp = p.avg * (1 + TAKER_FEE + 0.0005)
    # если цена выросла — подтянуть цель на TRAIL_X * fee от текущей
    better = price - p.tp
    if better > 0:
        p.tp = price - (TRAIL_X * price * TAKER_FEE)
    save_state(symbol)
    # условие продажи: цена >= tp ИЛИ чистая прибыль в $ >= MIN_NET_PNL
    gross = (price - p.avg) * p.qty
    net   = gross - price * p.qty * TAKER_FEE
    return (price >= p.tp) or (net >= MIN_NET_PNL)

def affordable_qty(symbol:str, usdt_budget:float, price:float, limits:dict) -> float:
    """Максимально доступное кол-во с учётом min_amt/qty_step и запаса под комиссию."""
    step = limits[symbol]["qty_step"]
    min_amt = limits[symbol]["min_amt"]

    # небольшой «запас» против комиссий и округлений
    budget = usdt_budget * (1 - TAKER_FEE - 0.001)
    qty = math.floor((budget/price) / step) * step
    if qty < step:
        return 0.0
    if qty*price < min_amt:
        # подтолкнуть до минимума если бюджет позволяет
        need = min_amt / price
        qty  = math.floor(need/step)*step
    return round(qty, 8)

def try_buy(symbol:str, price:float, limits:dict, budget:Tuple[float,float]) -> Optional[str]:
    if time.time() < cooldown_until[symbol]: 
        return None
    usdt = wallet_usdt()
    minB, maxB = budget
    if usdt < minB: 
        return None
    use = min(maxB, usdt)
    qty = affordable_qty(symbol, use, price, limits)
    if qty <= 0: 
        return None
    payload = {
        "category":"spot","symbol":symbol,"side":"Buy","orderType":"Market",
        "qty": f"{qty:.8f}"
    }
    j = http_post("/v5/order/create", payload)
    if j.get("retCode")==0:
        # обновим позицию (новая средняя)
        p = state[symbol]
        new_cost = p.avg*p.qty + qty*price
        p.qty = round(p.qty + qty, 8)
        p.avg = new_cost / p.qty
        p.tp = max(p.tp, p.avg*(1+TAKER_FEE+0.0005))
        save_state(symbol)
        cooldown_until[symbol] = time.time() + COOLDOWN_SEC
        tg_send(f"🟢 {symbol} BUY @ {price:.6f}, qty={qty:.8f}")
        return "ok"
    else:
        cooldown_until[symbol] = time.time() + COOLDOWN_SEC
        err = f"[{symbol}] Ошибка BUY: {j.get('retMsg','?')} ({j.get('retCode')})"
        tg_send("⚠️ " + err)
        return None

def try_sell(symbol:str, price:float) -> Optional[str]:
    if time.time() < cooldown_until[symbol]:
        return None
    p = state[symbol]
    if p.qty <= 0: 
        return None
    # проверка свободного количества
    free = wallet_free_coin(coin_of(symbol))
    qty  = min(free, p.qty)
    if qty <= 0:
        return None
    payload = {
        "category":"spot","symbol":symbol,"side":"Sell","orderType":"Market",
        "qty": f"{qty:.8f}"
    }
    j = http_post("/v5/order/create", payload)
    if j.get("retCode")==0:
        gross = (price - p.avg)*qty
        net   = gross - price*qty*TAKER_FEE
        tg_send(f"✅ {symbol} SELL @ {price:.6f}, qty={qty:.8f}, pnl={net:.2f}")
        # уменьшить позицию
        p.qty = round(p.qty - qty, 8)
        if p.qty <= 0:
            p.qty, p.avg, p.tp = 0.0, 0.0, 0.0
        save_state(symbol)
        cooldown_until[symbol] = time.time() + COOLDOWN_SEC
        return "ok"
    else:
        cooldown_until[symbol] = time.time() + COOLDOWN_SEC
        err = f"[{symbol}] Ошибка SELL: {j.get('retMsg','?')} ({j.get('retCode')})"
        tg_send("⚠️ " + err)
        return None

def can_dca(symbol:str, price:float) -> bool:
    p = state[symbol]
    if p.qty <= 0: 
        return False
    dd = (p.avg - price)/p.avg
    return 0.0 < dd <= MAX_DD_DCA

# ---------- Klines cache ----------
_price_buf: Dict[str, deque] = {s: deque(maxlen=200) for s in SYMBOLS}

def seed_prices(symbol:str):
    # получим ~200 последних цен по 1m свечам
    j = http_get("/v5/market/kline", {"category":"spot","symbol":symbol,"interval":"1","limit":"200"})
    if j.get("retCode")==0:
        arr = j["result"]["list"]
        arr.sort(key=lambda x:int(x[0]))  # по времени возр.
        for k in arr:
            _price_buf[symbol].append(float(k[4]))  # close
    # на старте добавим текущую цену (на случай пустых)
    _price_buf[symbol].append(last_price(symbol))

# ---------- Main loop ----------
def main():
    # pre-seed
    limits = get_limits()
    for s in SYMBOLS:
        load_state(s)
        seed_prices(s)
    tg_send(f"🚀 Старт бота v3.2. Параметры: TAKER_FEE={TAKER_FEE}, "
            f"BUDGET=[{BUDGET_MIN};{BUDGET_MAX}] TRAILx={TRAIL_X}, SL={int(SL_PCT*100)}%, DD={int(MAX_DD_DCA*100)}%")

    last_log = 0
    err_cnt  = 0

    while True:
        loop_t0 = time.time()
        try:
            for s in SYMBOLS:
                price = last_price(s)
                _price_buf[s].append(price)

                # trailing / take-profit
                if trailing_tp(s, price):
                    try_sell(s, price)

                # safety stop (мягкий) — для «зависших» позиций
                p = state[s]
                if p.qty>0 and (price <= p.avg*(1 - SL_PCT)):
                    tg_send(f"🛑 {s} safety‑SL: price={price:.6f} < {p.avg*(1-SL_PCT):.6f}")
                    try_sell(s, price)

                # сигнал на вход
                signal, conf, diag = soft_signal(list(_price_buf[s]))
                # Покупаем только по сигналу buy; усредняем при dd в пределах
                if signal=="buy":
                    if p.qty<=0:
                        try_buy(s, price, limits, (BUDGET_MIN, BUDGET_MAX))
                    elif can_dca(s, price):
                        # усреднимся на половину бюджета
                        half=(BUDGET_MIN+BUDGET_MAX)/2
                        try_buy(s, price, limits, (half, half))

                # компактный лог раз в LOG_EVERY секунд
                if time.time()-last_log>=LOG_EVERY:
                    pos = state[s]
                    net = max(0.0, (price-pos.avg)*pos.qty - price*pos.qty*TAKER_FEE) if pos.qty>0 else 0.0
                    mark_tp = f" | TP: {pos.tp:.6f}" if pos.tp>0 else ""
                    print(f"{ts()} | [{s}] 🔎 {signal.upper()} (conf={conf:.2f}), price={price:.6f}, "
                          f"balance={pos.qty:.6f} (~{pos.qty*price:.2f} USDT) | EMA9={diag.get('EMA9')}, "
                          f"EMA21={diag.get('EMA21')}, RSI={diag.get('RSI')}, MACD={diag.get('MACD')}, "
                          f"SIG={diag.get('SIG')}{mark_tp}")
                    last_log = time.time()

            err_cnt = 0  # успешный цикл — обнулим счётчик ошибок

        except Exception as e:
            err_cnt += 1
            msg = f"loop error: {e}"
            print(f"{ts()} | {msg}")
            if err_cnt in (1, 3, 10):  # не спамим одинаковое
                tg_send("⚠️ " + msg)

        # небольшой динамический слип: сглаживаем нагрузку и держим частоту
        spent = time.time()-loop_t0
        time.sleep(max(0.6, 1.2 - spent))

if __name__=="__main__":
    assert API_KEY and API_SECRET, "BYBIT_API_KEY/SECRET не заданы"
    main()
