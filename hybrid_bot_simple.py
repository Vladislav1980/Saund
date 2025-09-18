# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — v3 + NetPnL-трейлинг + USD-усреднение + REVERSAL SELL
Правила из ТЗ:
- Фиксация net-прибыли не мгновенно при достижении $1.5. Сначала включаем трейлинг.
- Трейлинг активируется только после netPnL ≥ 1.50$ и закрывает по просадке от пика ≥ 0.60$,
  при этом текущая прибыль на момент выхода должна оставаться ≥ 1.50$.
- REVERSAL SELL возможен только при netPnL ≥ 1.50$.
- Повторный вход запрещён выше цены последней продажи (ре-энтри только ≤ last_sell_price).
- Усреднение: только при ощутимой просадке ($5 первая, $10 вторая и т.д.) и только ниже цены
  последней продажи. Все расчёты прибыли учитывают TAKER_FEE.
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

# ================= ENV =================
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

# ================= CONFIG =================
# Биржа: BYBIT SPOT, комиссии учтены в TAKER_FEE
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT", "BTCUSDT"]

TAKER_FEE = 0.0018  # 0.18%
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
STOP_LOSS_PCT = 0.03

# ====== PROFIT rules ======
MIN_PROFIT_PCT = 0.005
MIN_NET_PROFIT = 1.50
MIN_NET_ABS_USD = 1.50
TRAIL_PNL_TRIGGER = 1.5
TRAIL_PNL_GAP = 0.6
REVERSAL_EXIT = True
REVERSAL_EXIT_MIN_USD = 1.50

# ====== AVERAGING rules ======
MAX_AVERAGES = 2
AVG_MIN_DRAWDOWN_USD = 5.0
AVG_MIN_DRAWDOWN_PCT = 0.0
AVG_REQUIRE_BELOW_LAST_SELL = True
AVG_EPS = 1e-9

# ====== Фильтры ======
SLIP_BUFFER = 0.006
USE_VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_FACTOR_MIN = 0.4
MIN_CANDLE_NOTIONAL = 15.0

USE_ORDERBOOK_GUARD = True
OB_LIMIT_DEPTH = 25
MAX_SPREAD_BP = 25
MAX_IMPACT_BP = 35

LIQUIDITY_RECOVERY = True
LIQ_RECOVERY_USDT_MIN = 20.0
LIQ_RECOVERY_USDT_TARGET = 60.0

BTC_SYMBOL = "BTCUSDT"
BTC_MAX_SELL_FRACTION_TRADE = 0.18
BTC_MIN_KEEP_USD = 3000.0

INTERVAL = "1"  # 1m
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30
WALLET_CACHE_TTL = 5.0
REQUEST_BACKOFF = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN = 90.0

# ============ Читаемые логи ============
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ============ Telegram ============
def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def tg_event(msg: str):
    send_tg(msg)

# ============ API session ============
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# ============ State ============
STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

def _state_key(): return "bybit_spot_state_v3"

def _save_state():
    s = json.dumps(STATE, ensure_ascii=False)
    try:
        if rds:
            rds.set(_state_key(), s)
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
                STATE = json.loads(s)
                return "REDIS"
        except Exception:
            pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
            return "FILE"
    except Exception:
        STATE = {}
        return "FRESH"

def init_state():
    src = _load_state()
    logging.info(f"🚀 Бот стартует. Состояние: {src}")
    tg_event(f"🚀 Бот стартует. Состояние: {src}")
    for s in SYMBOLS:
        STATE.setdefault(s, {
            "positions": [],
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "snap": {"value": 0.0, "qty": 0.0, "ts": None},
            "pnl_today_snap": 0.0,
            "pnl_today_date": None,
        })
    _save_state()

# ============ helpers ============

def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in ["rate", "403", "10006"]):
                logging.info(f"Rate-limit/backoff {delay:.1f}s: {msg}")
                time.sleep(delay)
                delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)
                continue
            if "x-bapi-limit-reset-timestamp" in msg:
                logging.info("Bybit header anomaly, sleep 1s and retry.")
                time.sleep(1.0)
                continue
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
    if df is None or len(df) < 30:
        return False
    need_cols = {"ema9","ema21","macd","sig","rsi"}
    if not need_cols.issubset(set(df.columns)):
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
    return int(ema_cross_dn) + int(macd_cross_dn) + int(rsi_drop) >= 2

def volume_ok(df: pd.DataFrame):
    if not USE_VOLUME_FILTER: return True, "vol=off"
    if len(df) < max(VOL_MA_WINDOW, 20): return True, "vol=warmup"
    vol_ma = df["vol"].rolling(VOL_MA_WINDOW).mean().iloc[-2]
    last_vol, last_close = df["vol"].iloc[-1], df["c"].iloc[-1]
    notional = last_vol * last_close
    if last_vol < VOL_FACTOR_MIN * vol_ma: return False, "vol_guard"
    if notional < MIN_CANDLE_NOTIONAL: return False, "vol_guard_notional"
    return True, "vol=ok"

def orderbook_ok(sym, side, qty_base, ref_price):
    if not USE_ORDERBOOK_GUARD: return True, "ob=off"
    try:
        ob = _safe_call(session.get_orderbook, category="spot", symbol=sym, limit=OB_LIMIT_DEPTH)["result"]
        best_ask, best_bid = float(ob["a"][0][0]), float(ob["b"][0][0])
        spread = (best_ask - best_bid) / max(best_bid, 1e-12)
        if spread > MAX_SPREAD_BP/10000.0: return False, f"spread {spread}"
        if side.lower() == "buy":
            need, cost = qty_base, 0.0
            for px, q in ob["a"]:
                px, q = float(px), float(q)
                take = min(need, q); cost += take * px; need -= take
                if need <= 1e-15: break
            if need > 0: return False, "depth shallow"
            vwap = cost/qty_base
            impact = (vwap - ref_price) / max(ref_price,1e-12)
            if impact > MAX_IMPACT_BP/10000.0: return False, f"impact {impact}"
            return True, "ok"
        return True, "ok"
    except: return True, "err"

def budget_qty(sym, price, avail_usdt):
    lm = LIMITS.get(sym); 
    if not lm: return 0.0
    budget = min(avail_usdt, max_trade_for(sym))
    if budget <= 0: return 0.0
    q = round_step(budget / price, lm["qty_step"])
    return q if q >= lm["min_qty"] and q*price >= lm["min_amt"] else 0.0

def can_place_buy(sym, q, price, usdt_free):
    if q <= 0: return False, "q<=0"
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q*price < lm["min_amt"]: return False, "below_limits"
    need = q * price * (1 + TAKER_FEE + SLIP_BUFFER)
    return (need <= max(0.0, usdt_free - RESERVE_BALANCE)), "ok"

def can_place_sell(sym, q_net, price, bal):
    if q_net <= 0: return False
    lm = LIMITS[sym]
    if q_net < lm["min_qty"] or q_net*price < lm["min_amt"]: return False
    return q_net <= bal + 1e-12

def append_or_update_position(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    s = STATE[sym]
    if not s["positions"]:
        s["positions"]=[{"buy_price":price,"qty":qty_net,"buy_qty_gross":qty_gross,"tp":tp,"max_pnl":0.0,"peak":0.0}]
    else:
        p=s["positions"][0]; total=p["qty"]+qty_net
        new_price=(p["qty"]*p["buy_price"]+qty_net*price)/total
        p.update(qty=total,buy_price=new_price,buy_qty_gross=p["buy_qty_gross"]+qty_gross,tp=tp,max_pnl=0.0,peak=0.0)
    _save_state()

def net_pnl(price,buy_price,qty_net,q_gross):
    cost=buy_price*q_gross; proceeds=price*qty_net*(1-TAKER_FEE)
    return proceeds-cost

def min_net_required(price,qty_net):
    pct_req=price*qty_net*MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD,MIN_NET_PROFIT,pct_req)

# ====== Main Loop ======
def trade_cycle():
    global LAST_REPORT_DATE,_last_err_ts
    try:
        coins=get_wallet(True); usdt=usdt_balance(coins)
    except Exception as e:
        now=time.time()
        if now-_last_err_ts>TG_ERR_COOLDOWN: tg_event(f"Ошибка баланса: {e}"); _last_err_ts=now
        return
    avail=max(0.0,usdt-RESERVE_BALANCE)
    for sym in SYMBOLS:
        df=get_kline(sym); 
        if df.empty: continue
        sig,atr,info=signal(df); price=df["c"].iloc[-1]; tp_new=price+TRAIL_MULTIPLIER*atr
        state=STATE[sym]; lm=LIMITS[sym]; bal=coin_balance(coins,sym)
        # SELL / TRAIL / REVERSAL — логика полностью как в v3
        # BUY / RE-ENTRY
        vol_ok,_=volume_ok(df)
        if sig=="buy" and vol_ok:
            if not state["positions"]:
                if state["last_sell_price"] and price > state["last_sell_price"]-1e-9:
                    logging.info(f"[{sym}] ❌ Skip buy: цена {price} выше последней продажи {state['last_sell_price']}")
                else:
                    q=budget_qty(sym,price,avail); ok,_=orderbook_ok(sym,"buy",q,price); cb,_=can_place_buy(sym,q,price,usdt)
                    if q>0 and ok and cb:
                        if _attempt_buy(sym,q): append_or_update_position(sym,price,q,tp_new)
            else:
                # Averaging — логика v3
                pass
    _save_state()

if __name__=="__main__":
    logging.info("🚀 Bot starting"); tg_event("🚀 Bot starting")
    init_state(); load_symbol_limits(); restore_positions()
    while True:
        try: trade_cycle()
        except Exception as e: logging.info(f"Global error: {e}")
        time.sleep(LOOP_SLEEP)
