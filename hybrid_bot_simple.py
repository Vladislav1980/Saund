# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — v3 + NetPnL-трейлинг + Абсолютное усреднение
Правки по запросу:
- прибыль считается NET (после комиссий) и фиксируется только > $1.50;
- начинается трейлинг при netPnL≥$1.50, фиксация при откате ≥$0.60 от пика
  или при медвежьем развороте, при этом netPnL всё ещё ≥$1.50;
- усреднение только при просадке в $: первый раз ≥ $5 ниже среднего входа,
  второй раз — ещё ниже (порог первого + $2).
"""

import os
import time
import math
import logging
import datetime
import json
import traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ============ ENV ============
load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

# ============ CONFIG ============
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT", "BTCUSDT"]

# Комиссии учтены в netPnL
TAKER_FEE = 0.0018

# Лимиты на покупку
BASE_MAX_TRADE_USDT = 35.0
MAX_TRADE_OVERRIDES = {
    "TONUSDT": 70.0,
    "AVAXUSDT": 70.0,
    "ADAUSDT": 60.0,
    "BTCUSDT": 90.0,
}
def max_trade_for(sym: str) -> float:
    return float(MAX_TRADE_OVERRIDES.get(sym, BASE_MAX_TRADE_USDT))

RESERVE_BALANCE = 1.0
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10        # страховка по %-просадке (как было)
MAX_AVERAGES = 2           # всего 2 добора (1-й и 2-й)
STOP_LOSS_PCT = 0.03       # SL выключен по факту (см. ниже), оставил как было

# >>> Абсолютное усреднение в долларах <<<
AVG_MIN_DRAWDOWN_USD = 5.0     # первый добор: цена <= avg_entry - 5$
AVG_NEXT_STEP_USD    = 2.0     # второй добор: ещё ниже на 2$ (итого ≥ 7$ от avg_entry)

# Минимальная прибыль (после комиссий)
MIN_PROFIT_PCT = 0.005     # второстепенно
MIN_NET_ABS_USD = 1.50     # главный фильтр по $ чистой прибыли

SLIP_BUFFER = 0.006
PROFIT_ONLY = True          # запрещаем фиксировать убытки обычными путями

# Фильтры ликвидности/объёма
USE_VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_FACTOR_MIN = 0.4
MIN_CANDLE_NOTIONAL = 15.0

USE_ORDERBOOK_GUARD = True
OB_LIMIT_DEPTH = 25
MAX_SPREAD_BP = 25
MAX_IMPACT_BP = 35

# Временное высвобождение USDT (оставил)
LIQUIDITY_RECOVERY = True
LIQ_RECOVERY_USDT_MIN = 20.0
LIQ_RECOVERY_USDT_TARGET = 60.0

# Спец-ограничения для BTC
BTC_SYMBOL = "BTCUSDT"
BTC_MAX_SELL_FRACTION_TRADE = 0.18
BTC_MIN_KEEP_USD = 3000.0

# Период, состояние и т.п.
INTERVAL = "1"  # 1m
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30
WALLET_CACHE_TTL = 5.0
REQUEST_BACKOFF = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN = 90.0

# Трейлинг netPnL
TRAIL_PNL_TRIGGER = 1.50   # старт трекинга пика
TRAIL_PNL_GAP     = 0.60   # фикс при падении от пика на 0.60$

# Быстрый выход по развороту
REVERSAL_EXIT = True
REVERSAL_EXIT_MIN_USD = 1.50

# Логи
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"),
              logging.StreamHandler()]
)

# TG
def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def tg_event(msg: str): send_tg(msg)

# API
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# Состояние
STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

def _state_key(): return "bybit_spot_state_v3_trailing_absavg"

def _save_state():
    s = json.dumps(STATE, ensure_ascii=False)
    try:
        if rds: rds.set(_state_key(), s)
    except Exception:
        pass
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        logging.error(f"save_state error: {e}")

def _load_state():
    global STATE
    if rds:
        try:
            s = rds.get(_state_key())
            if s:
                STATE = json.loads(s); return "REDIS"
        except Exception:
            pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f); return "FILE"
    except Exception:
        STATE = {}; return "FRESH"

def init_state():
    src = _load_state()
    logging.info(f"🚀 Бот стартует. Состояние: {src}")
    tg_event(f"🚀 Бот стартует. Состояние: {src}")
    for s in SYMBOLS:
        STATE.setdefault(s, {
            "positions": [],          # единственная агрегированная позиция
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,           # сколько раз усреднялись
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_avg_price": None    # по какой средней цене делали последний добор
        })
    _save_state()

# ==== helpers ====
def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try: return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in ["rate", "403", "10006"]):
                logging.info(f"Rate-limit/backoff {delay:.1f}s: {msg}")
                time.sleep(delay); delay = min(REQUEST_BACKOFF_MAX, delay*1.7); continue
            if "x-bapi-limit-reset-timestamp" in msg:
                logging.info("Bybit header anomaly, sleep 1s and retry.")
                time.sleep(1.0); continue
            raise

def get_wallet(force=False):
    if not force and _wallet_cache["coins"] is not None and time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL:
        return _wallet_cache["coins"]
    r = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = r["result"]["list"][0]["coin"]
    _wallet_cache.update(ts=time.time(), coins=coins)
    return coins

def usdt_balance(coins):
    return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))

def coin_balance(coins, sym):
    base = sym.replace("USDT", "")
    return float(next((c["walletBalance"] for c in coins if c["coin"] == base), 0.0))

def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        if item["symbol"] in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[item["symbol"]] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0))
            }
    logging.info(f"Загружены лимиты: {LIMITS}")

def round_step(qty: float, step: float) -> float:
    try:
        exp = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10 ** abs(exp)) / 10 ** abs(exp)
    except Exception:
        return qty

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df: pd.DataFrame):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    macd = MACD(close=df["c"])
    df["macd"], df["sig"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.5f},EMA21={last['ema21']:.5f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.5f},SIG={last['sig']:.5f}")
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 and last["macd"] > last["sig"]:
        return "buy", float(atr), info
    if last["ema9"] < last["ema21"] and last["rsi"] < 50 and last["macd"] < last["sig"]:
        return "sell", float(atr), info
    return "none", float(atr), info

def is_bearish_reversal(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 30: return False
    need = {"ema9","ema21","macd","sig","rsi"}
    if not need.issubset(df.columns):
        df = df.copy()
        df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
        df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
        macd_obj = MACD(close=df["c"])
        df["macd"], df["sig"] = macd_obj.macd(), macd_obj.macd_signal()
        df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    last, prev = df.iloc[-1], df.iloc[-2]
    ema_cross_dn  = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"])
    macd_cross_dn = (prev["macd"] >= prev["sig"])   and (last["macd"] < last["sig"])
    rsi_drop      = (last["rsi"] < prev["rsi"] - 3.0) and (last["rsi"] < 55.0)
    return (int(ema_cross_dn) + int(macd_cross_dn) + int(rsi_drop)) >= 2

def volume_ok(df: pd.DataFrame):
    if not USE_VOLUME_FILTER: return True, "vol=off"
    if len(df) < max(VOL_MA_WINDOW, 20): return True, "vol=warmup"
    vol_ma   = df["vol"].rolling(VOL_MA_WINDOW).mean().iloc[-2]
    last_vol = df["vol"].iloc[-1]
    last_px  = df["c"].iloc[-1]
    notional = last_vol * last_px
    if vol_ma is None or pd.isna(vol_ma): return True, "vol=ma_na"
    if last_vol < VOL_FACTOR_MIN * vol_ma:
        return False, f"vol_guard last<{VOL_FACTOR_MIN:.2f}*MA"
    if notional < MIN_CANDLE_NOTIONAL:
        return False, f"vol_guard notion<{MIN_CANDLE_NOTIONAL}"
    return True, "vol=ok"

def orderbook_ok(sym: str, side: str, qty_base: float, ref_price: float):
    if not USE_ORDERBOOK_GUARD: return True, "ob=off"
    try:
        ob = _safe_call(session.get_orderbook, category="spot", symbol=sym, limit=OB_LIMIT_DEPTH)["result"]
        best_ask = float(ob["a"][0][0]); best_bid = float(ob["b"][0][0])
        spread = (best_ask - best_bid) / max(best_bid, 1e-12)
        if spread > MAX_SPREAD_BP/10000.0:
            return False, f"ob spread {spread*100:.2f}%>{MAX_SPREAD_BP/100:.2f}%"
        if side.lower() == "buy":
            need = qty_base; cost = 0.0
            for px, q in ob["a"]:
                px = float(px); q = float(q)
                take = min(need, q); cost += take*px; need -= take
                if need <= 1e-15: break
            if need > 0: return False, "ob depth shallow"
            vwap = cost/max(qty_base,1e-12)
            impact = (vwap - ref_price) / max(ref_price,1e-12)
            if impact > MAX_IMPACT_BP/10000.0:
                return False, f"ob impact {impact*100:.2f}%>{MAX_IMPACT_BP/100:.2f}%"
            return True, f"ob ok spread={spread*100:.2f}%, impact={impact*100:.2f}%"
        else:
            return True, f"ob ok spread={spread*100:.2f}%"
    except Exception as e:
        return True, f"ob err: {e}"

def budget_qty(sym: str, price: float, avail_usdt: float) -> float:
    if sym not in LIMITS: return 0.0
    lm = LIMITS[sym]
    budget = min(avail_usdt, max_trade_for(sym))
    if budget <= 0: return 0.0
    q = round_step(budget/price, lm["qty_step"])
    if q < lm["min_qty"] or q*price < lm["min_amt"]: return 0.0
    return q

def can_place_buy(sym: str, q: float, price: float, usdt_free: float) -> (bool, str):
    if q <= 0: return False, "q<=0"
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q*price < lm["min_amt"]:
        return False, f"below_limits(min_qty={lm['min_qty']}, min_amt={lm['min_amt']})"
    need = q*price*(1 + TAKER_FEE + SLIP_BUFFER)
    ok = need <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)
    return ok, (f"ok need={need:.2f}" if ok else f"need {need:.2f} > free {max(0.0, usdt_free-RESERVE_BALANCE):.2f}")

def can_place_sell(sym: str, q_net: float, price: float, coin_bal_now: float):
    if q_net <= 0: return False
    lm = LIMITS[sym]
    if q_net < lm["min_qty"] or q_net*price < lm["min_amt"]: return False
    return q_net <= coin_bal_now + 1e-12

def append_or_update_position(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    s = STATE[sym]
    if not s["positions"]:
        s["positions"] = [{
            "buy_price": price,
            "qty": qty_net,
            "buy_qty_gross": qty_gross,
            "tp": tp,
            "max_pnl": 0.0
        }]
    else:
        p = s["positions"][0]
        total_qty = p["qty"] + qty_net
        new_price = (p["qty"]*p["buy_price"] + qty_net*price) / max(total_qty,1e-12)
        p["qty"] = total_qty
        p["buy_price"] = new_price
        p["buy_qty_gross"] += qty_gross
        p["tp"] = tp
        p["max_pnl"] = 0.0
    _save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    cost = buy_price * buy_qty_gross             # покупка (с комиссией в gross)
    proceeds = price * qty_net * (1 - TAKER_FEE) # продажа (минус комиссия)
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    pct_req = price * qty_net * MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD, pct_req)

# ===== DAILY REPORT =====
def daily_report():
    try:
        coins = get_wallet(True)
        by = {c["coin"]: float(c["walletBalance"]) for c in coins}
        today = str(datetime.date.today())
        lines = [f"📊 Daily Report {today}", f"USDT: {by.get('USDT',0.0):.2f}"]

        for sym in SYMBOLS:
            base = sym.replace("USDT","")
            bal_qty = by.get(base, 0.0)
            df = get_kline(sym)
            price = float(df["c"].iloc[-1]) if not df.empty else 0.0
            value = price * bal_qty
            s = STATE[sym]
            cur_q = sum(max(p.get("qty",0.0),0.0) for p in s["positions"])
            lines.append(f"{sym}: balance={bal_qty:.6f} (~{value:.2f} USDT), trades={s['count']}, pnl={s['pnl']:.2f}, maxDD={s['max_drawdown']*100:.2f}%, curPosQty={cur_q:.6f}")
        tg_event("\n".join(lines))
    except Exception as e:
        logging.info(f"daily_report error: {e}")

# ===== RESTORE =====
def restore_positions():
    restored = []
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            price = df["c"].iloc[-1]
            coins = get_wallet(True)
            bal = coin_balance(coins, sym)
            lm = LIMITS.get(sym, {})
            if price and bal*price >= lm.get("min_amt",0.0) and bal >= lm.get("min_qty",0.0):
                q_net = round_step(bal, lm["qty_step"])
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                tp = price + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"] = [{
                    "buy_price": price,
                    "qty": q_net,
                    "buy_qty_gross": q_net/(1-TAKER_FEE),
                    "tp": tp,
                    "max_pnl": 0.0
                }]
                STATE[sym]["last_avg_price"] = price
                restored.append(f"{sym}: qty={q_net:.8f} @ {price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    _save_state()
    if restored: tg_event("♻️ Восстановлены позиции:\n" + "\n".join(restored))
    else: tg_event("ℹ️ Позиции не найдены для восстановления.")

# ===== ORDERS =====
def _attempt_buy(sym: str, qty_base: float) -> bool:
    lm = LIMITS[sym]
    qty = round_step(qty_base, lm["qty_step"])
    tries = 4
    while tries > 0 and qty >= lm["min_qty"]:
        try:
            _safe_call(session.place_order,
                category="spot", symbol=sym, side="Buy",
                orderType="Market", timeInForce="IOC",
                marketUnit="baseCoin", qty=str(qty))
            return True
        except Exception as e:
            msg = str(e)
            if "170131" in msg or "Insufficient balance" in msg:
                shrink = max(lm["qty_step"], qty * 0.005)
                qty = round_step(max(lm["min_qty"], qty - shrink), lm["qty_step"])
                tries -= 1; time.sleep(0.3); continue
            if "x-bapi-limit-reset-timestamp" in msg:
                time.sleep(0.8); continue
            raise
    return False

def _attempt_sell(sym: str, qty_base: float) -> bool:
    lm = LIMITS[sym]
    qty = round_step(qty_base, lm["qty_step"])
    _safe_call(session.place_order, category="spot", symbol=sym,
               side="Sell", orderType="Market", qty=str(qty))
    return True

def cap_btc_sell_qty(sym: str, q_net: float, price: float, coin_bal_now: float) -> float:
    if sym != BTC_SYMBOL: return q_net
    cap_by_fraction = coin_bal_now * BTC_MAX_SELL_FRACTION_TRADE
    keep_qty_floor = 0.0
    if BTC_MIN_KEEP_USD > 0:
        keep_qty_floor = max(0.0, (coin_bal_now*price - BTC_MIN_KEEP_USD) / max(price,1e-12))
    allowed = max(0.0, min(q_net, cap_by_fraction, keep_qty_floor if keep_qty_floor>0 else q_net))
    if allowed <= 0: allowed = min(q_net, cap_by_fraction)
    return max(0.0, allowed)

# ===== Liquidity Recovery (как было) =====
def try_liquidity_recovery(coins, usdt):
    if not LIQUIDITY_RECOVERY: return
    avail = max(0.0, usdt - RESERVE_BALANCE)
    if avail >= LIQ_RECOVERY_USDT_MIN: return
    target_gain = LIQ_RECOVERY_USDT_TARGET - avail
    if target_gain <= 0: return
    logging.info(f"💧 LiquidityRecovery: need +{target_gain:.2f} USDT (avail={avail:.2f})")
    candidates = []
    for sym in SYMBOLS:
        if not STATE[sym]["positions"]: continue
        df = get_kline(sym); price = df["c"].iloc[-1]
        lm = LIMITS[sym]; coin_bal = coin_balance(coins, sym)
        for p in STATE[sym]["positions"]:
            q_n = round_step(p["qty"], lm["qty_step"])
            q_g = p.get("buy_qty_gross", q_n/(1-TAKER_FEE))
            pnl = net_pnl(price, p["buy_price"], q_n, q_g)
            if pnl > MIN_NET_ABS_USD and can_place_sell(sym, q_n, price, coin_bal):
                sell_try = max(lm["min_qty"], q_n * 0.2)
                if sym == BTC_SYMBOL: sell_try = cap_btc_sell_qty(sym, sell_try, price, coin_bal)
                if sell_try <= 0: continue
                est_usdt = price * sell_try * (1 - TAKER_FEE)
                candidates.append((sym, price, sell_try, est_usdt, pnl, coin_bal, lm))
    candidates.sort(key=lambda x: (x[4]/max(x[3],1e-9)), reverse=True)
    for sym, price, sell_q, est, pnl, coin_bal, lm in candidates:
        if target_gain <= 0: break
        sell_q = min(sell_q, round_step(target_gain / max(price*(1-TAKER_FEE),1e-9), lm["qty_step"]))
        if sell_q < lm["min_qty"] or sell_q*price < lm["min_amt"]: continue
        if not can_place_sell(sym, sell_q, price, coin_bal): continue
        try:
            _attempt_sell(sym, sell_q)
            tg_event(f"🟠 RECOVERY SELL {sym} @ {price:.6f}, qty={sell_q:.8f} (~{price*sell_q*(1-TAKER_FEE):.2f} USDT)")
            rest = sell_q; newpos=[]
            for p in STATE[sym]["positions"]:
                if rest <= 0: newpos.append(p); continue
                take = min(p["qty"], rest); ratio = take / max(p["qty"],1e-12)
                p["qty"] -= take; p["buy_qty_gross"] *= max(0.0, 1.0 - ratio); rest -= take
                if p["qty"] > 0: newpos.append(p)
            STATE[sym]["positions"] = newpos; _save_state()
            target_gain -= price*sell_q*(1-TAKER_FEE)
            coins = get_wallet(True); usdt = usdt_balance(coins)
        except Exception as e:
            logging.info(f"[{sym}] recovery sell failed: {e}")

# ===== Main trading loop =====
def trade_cycle():
    global LAST_REPORT_DATE, _last_err_ts
    try:
        coins = get_wallet(True); usdt = usdt_balance(coins)
    except Exception as e:
        now = time.time()
        if now - _last_err_ts > TG_ERR_COOLDOWN:
            tg_event(f"Ошибка баланса: {e}"); _last_err_ts = now
        return

    if LIQUIDITY_RECOVERY:
        try_liquidity_recovery(coins, usdt)
        coins = get_wallet(True); usdt = usdt_balance(coins)

    avail = max(0.0, usdt - RESERVE_BALANCE)
    logging.info(f"💰 USDT={usdt:.2f} | Доступно={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                logging.info(f"[{sym}] нет свечей — пропуск"); continue

            sig, atr, info = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]; lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            value = coin_bal * price
            logging.info(f"[{sym}] sig={sig} | {info} | price={price:.6f}, value={value:.2f}, pos={len(state['positions'])}")

            # DD-tracker
            if state["positions"]:
                total_qty = sum(max(p.get("qty",0.0),0.0) for p in state["positions"])
                if total_qty > 1e-15:
                    avg_entry = sum(max(p.get("qty",0.0),0.0)*float(p.get("buy_price",0.0)) for p in state["positions"]) / total_qty
                    if avg_entry > 1e-15 and not (math.isnan(avg_entry) or math.isinf(avg_entry)):
                        dd_now = max(0.0, (avg_entry - price) / max(avg_entry,1e-12))
                        state["max_drawdown"] = max(state.get("max_drawdown",0.0), dd_now)

            # ===== SELL / TRAILING =====
            new_pos = []
            for p in state["positions"]:
                b   = float(p["buy_price"])
                q_n = round_step(p["qty"], lm["qty_step"])
                tp  = p["tp"]
                q_g = p.get("buy_qty_gross", q_n/(1-TAKER_FEE))
                if q_n <= 0: continue

                sell_cap_q = q_n
                if sym == BTC_SYMBOL:
                    sell_cap_q = cap_btc_sell_qty(sym, q_n, price, coin_bal)

                if sell_cap_q < lm["min_qty"] or sell_cap_q*price < lm["min_amt"]:
                    new_pos.append(p)
                    logging.info(f"[{sym}] 🔸Hold: ниже лимитов/кап-по BTC"); continue

                pnl = net_pnl(price, b, sell_cap_q, q_g * (sell_cap_q/max(q_n,1e-12)))
                p["max_pnl"] = max(p.get("max_pnl",0.0), pnl)

                # Трейлинг и разворот действуют только если достигли минимальной чистой прибыли
                if p["max_pnl"] >= TRAIL_PNL_TRIGGER:
                    should_trail = (p["max_pnl"] - pnl) >= TRAIL_PNL_GAP
                    bearish_rev  = REVERSAL_EXIT and is_bearish_reversal(df)
                    if (should_trail or bearish_rev) and pnl >= MIN_NET_ABS_USD:
                        _attempt_sell(sym, sell_cap_q)
                        reason = "TRAIL" if should_trail else "REVERSAL"
                        msg = f"✅ {reason} SELL {sym} @ {price:.6f}, qty={sell_cap_q:.8f}, netPnL={pnl:.2f}, peak={p['max_pnl']:.2f}"
                        logging.info(msg); tg_event(msg)
                        state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                        left = q_n - sell_cap_q
                        if left > 0:
                            ratio = sell_cap_q / max(q_n,1e-12)
                            p["qty"] = left
                            p["buy_qty_gross"] = max(0.0, p["buy_qty_gross"] * (1.0 - ratio))
                            p["max_pnl"] = 0.0
                            new_pos.append(p)
                        coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                        continue

                # SL фиксировать убыток мы не хотим (PROFIT_ONLY=True) — поэтому пропускаем

                # Обновляем TP-след
                new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                if new_tp != tp:
                    logging.info(f"[{sym}] 📈 Trail TP: {tp:.6f} → {new_tp:.6f}")
                p["tp"] = new_tp
                new_pos.append(p)
            state["positions"] = new_pos

            # ===== BUY / AVERAGING (ТОЛЬКО АБСОЛЮТНЫЙ $-ОТКАТ) =====
            vol_ok, vol_info = volume_ok(df)
            if sig == "buy" and vol_ok:
                # Текущая средняя входа (если есть позиция)
                if state["positions"]:
                    total_q = sum(x["qty"] for x in state["positions"])
                    avg_price = sum(x["qty"] * x["buy_price"] for x in state["positions"]) / max(total_q,1e-12)

                    dd_usd = max(0.0, avg_price - price)  # абсолютная просадка в $
                    # Требуемый порог: 5$ для 1-го добора и 7$ для 2-го
                    need_dd = AVG_MIN_DRAWDOWN_USD + max(0, state["avg_count"]) * AVG_NEXT_STEP_USD
                    cond_price_below_last_avg = True
                    if state["last_avg_price"] is not None:
                        cond_price_below_last_avg = price < state["last_avg_price"] - 1e-12

                    if state["avg_count"] < MAX_AVERAGES and dd_usd >= need_dd and cond_price_below_last_avg:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        can_buy, why_buy = can_place_buy(sym, q_gross, price, usdt)
                        if q_gross > 0 and ob_ok and can_buy:
                            before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            if _attempt_buy(sym, q_gross):
                                tp_new = price + TRAIL_MULTIPLIER * atr
                                append_or_update_position(sym, price, q_gross, tp_new)
                                after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                                state["count"] += 1; state["avg_count"] += 1; state["last_avg_price"] = price
                                qty_net = q_gross * (1 - TAKER_FEE)
                                msg = (f"🟢 BUY(avg) {sym} @ {price:.6f}, qty_net={qty_net:.8f} | "
                                       f"dd_usd={dd_usd:.2f} (need≥{need_dd:.2f}), {ob_info}, {vol_info}")
                                logging.info(msg); tg_event(msg)
                                tg_event(f"📊 AVG {sym} POSITION UPDATE\nДо:\n{before}\nПосле:\n{after}")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip avg: dd_usd={dd_usd:.2f} need≥{need_dd:.2f}, last_avg={state['last_avg_price']}, "
                                         f"q_gross={q_gross:.8f}, {ob_info}, can_buy={can_buy}({why_buy}), free={usdt:.2f}, avail={avail:.2f}, {vol_info}")
                    else:
                        logging.info(f"[{sym}] 🔸Skip avg: dd_usd={dd_usd:.2f} need≥{need_dd:.2f} "
                                     f"or avg_count={state['avg_count']}≥{MAX_AVERAGES} "
                                     f"or price_not_below_last_avg={not cond_price_below_last_avg}")
                else:
                    # Первая покупка
                    if state["last_sell_price"] and abs(price - state["last_sell_price"]) / price < 0.003:
                        logging.info(f"[{sym}] 🔸Skip buy: близко к последней продаже.")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        can_buy, why_buy = can_place_buy(sym, q_gross, price, usdt)
                        if q_gross > 0 and ob_ok and can_buy:
                            before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            if _attempt_buy(sym, q_gross):
                                tp_new = price + TRAIL_MULTIPLIER * atr
                                append_or_update_position(sym, price, q_gross, tp_new)
                                after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                                state["count"] += 1; state["last_avg_price"] = price
                                qty_net = q_gross * (1 - TAKER_FEE)
                                msg = f"🟢 BUY {sym} @ {price:.6f}, qty_net={qty_net:.8f} | {ob_info}, {vol_info}"
                                logging.info(msg); tg_event(msg)
                                tg_event(f"📊 NEW {sym} POSITION\nДо:\n{before}\nПосле:\n{after}")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip buy: q_gross={q_gross:.8f}, {ob_info}, can_buy={can_buy}({why_buy}), free={usdt:.2f}, avail={avail:.2f}, {vol_info}")
            else:
                if sig == "buy" and not vol_ok:
                    logging.info(f"[{sym}] ⏸ Volume guard: {vol_info}")
                else:
                    logging.info(f"[{sym}] 🔸No action: sig={sig}")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] Ошибка цикла: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"[{sym}] Ошибка цикла: {e}"); _last_err_ts = now

    _save_state()

    now = datetime.datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# ===== Main =====
if __name__ == "__main__":
    logging.info("🚀 Bot starting (Bybit Spot) — NetPnL Trailing + Absolute Averaging")
    tg_event("🚀 Bot starting (Bybit Spot) — NetPnL Trailing + Absolute Averaging")
    init_state()
    load_symbol_limits()
    restore_positions()
    tg_event(
        "⚙️ Params: "
        f"TAKER={TAKER_FEE}, BASE_MAX_TRADE={BASE_MAX_TRADE_USDT}, OVR={MAX_TRADE_OVERRIDES}, "
        f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, DD={MAX_DRAWDOWN*100:.0f}%, MaxAvg={MAX_AVERAGES}, "
        f"AvgUSD(min)={AVG_MIN_DRAWDOWN_USD}, NextStep={AVG_NEXT_STEP_USD}, "
        f"NetPnL-Trailing ON (trigger=${TRAIL_PNL_TRIGGER}, gap=${TRAIL_PNL_GAP}), "
        f"ReversalExit={'ON' if REVERSAL_EXIT else 'OFF'}"
    )
    while True:
        try:
            trade_cycle()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"Global error: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"Global error: {e}"); _last_err_ts = now
        time.sleep(LOOP_SLEEP)
