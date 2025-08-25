# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — Aggregated Position, Controlled Averaging, Redis-Safe Initialization
- Сохраняем и восстанавливаем данные из Redis, включая старые состояния
- Безопасно добавляем новые ключи (position_entries) при запуске
- Контролируем усреднение (макс 2), только при падении цены ≥1.5% от текущей средней
"""

import os, time, math, logging, datetime, json, traceback
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
TAKER_FEE = 0.0018
BASE_MAX_TRADE_USDT = 35.0
MAX_TRADE_OVERRIDES = {"TONUSDT": 70.0, "AVAXUSDT": 70.0, "ADAUSDT": 60.0}
def max_trade_for(sym: str) -> float:
    return float(MAX_TRADE_OVERRIDES.get(sym, BASE_MAX_TRADE_USDT))

RESERVE_BALANCE = 1.0
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2
MIN_AVG_DIFF_PCT = 0.015  # 1.5% падение для усреднения
STOP_LOSS_PCT = 0.03

MIN_PROFIT_PCT = 0.005
MIN_NET_PROFIT = 1.50
MIN_NET_ABS_USD = 1.50

SLIP_BUFFER = 0.006
PROFIT_ONLY = False

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

INTERVAL = "1"
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30
WALLET_CACHE_TTL = 5.0
REQUEST_BACKOFF = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN = 90.0

TRAIL_PNL_TRIGGER = 1.5
TRAIL_PNL_GAP = 0.6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def tg_event(msg: str):
    send_tg(msg)

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

def _state_key(): return "bybit_spot_state_v3"
def _save_state():
    s = json.dumps(STATE, ensure_ascii=False)
    if rds:
        try:
            rds.set(_state_key(), s)
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        logging.error(f"File save error: {e}")

def _load_state():
    global STATE
    if rds:
        try:
            s = rds.get(_state_key())
            if s:
                STATE = json.loads(s)
                return "REDIS"
        except Exception as e:
            logging.error(f"Redis load error: {e}")
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
            return "FILE"
    except Exception:
        STATE = {}
        return "FRESH"

def init_state():
    src = _load_state()
    logging.info(f"🚀 Bot starting. State: {src}")
    tg_event(f"🚀 Bot starting. State: {src}")
    for sym in SYMBOLS:
        sym_state = STATE.setdefault(sym, {})
        sym_state.setdefault("position_entries", [])
        sym_state.setdefault("pnl", 0.0)
        sym_state.setdefault("count", 0)
        sym_state.setdefault("avg_count", 0)
        sym_state.setdefault("last_sell_price", 0.0)
        sym_state.setdefault("max_drawdown", 0.0)
    _save_state()

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
                logging.info("Bybit header anomaly — wait 1s")
                time.sleep(1.0)
                continue
            raise

def get_wallet(force=False):
    if not force and _wallet_cache["coins"] is not None and time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL:
        return _wallet_cache["coins"]
    resp = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = resp["result"]["list"][0]["coin"]
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
    logging.info(f"Loaded limits: {LIMITS}")

def round_step(qty, step):
    try:
        exp = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exp)) / 10**abs(exp)
    except Exception:
        return qty

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
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

def volume_ok(df):
    if not USE_VOLUME_FILTER:
        return True
    if len(df) < max(VOL_MA_WINDOW, 20):
        return True
    vol_ma = df["vol"].rolling(VOL_MA_WINDOW).mean().iloc[-2]
    last_vol = df["vol"].iloc[-1]
    last_close = df["c"].iloc[-1]
    notional = last_vol * last_close
    if pd.isna(vol_ma):
        return True
    if last_vol < VOL_FACTOR_MIN * vol_ma:
        logging.info(f"⏸ Volume guard: last_vol<{VOL_FACTOR_MIN:.2f}*MA")
        return False
    if notional < MIN_CANDLE_NOTIONAL:
        logging.info(f"⏸ Volume guard: notional {notional:.2f} < ${MIN_CANDLE_NOTIONAL:.2f}")
        return False
    return True

def orderbook_ok(sym, side, qty_base, ref_price):
    if not USE_ORDERBOOK_GUARD:
        return True, "ob=off"
    try:
        ob = _safe_call(session.get_orderbook, category="spot", symbol=sym, limit=OB_LIMIT_DEPTH)["result"]
        best_ask = float(ob["a"][0][0]); best_bid = float(ob["b"][0][0])
        spread = (best_ask - best_bid) / max(best_bid, 1e-12)
        if spread > MAX_SPREAD_BP/10000:
            return False, f"ob=spread {spread*100:.2f}%>{MAX_SPREAD_BP/100:.2f}%"
        if side.lower() == "buy":
            need = qty_base; cost = 0.0
            for px, q in ob["a"]:
                px, q = float(px), float(q)
                take = min(need, q); cost += take * px; need -= take
                if need <= 1e-15:
                    break
            if need > 0:
                return False, "ob=depth shallow"
            vwap = cost / qty_base
            impact = (vwap - ref_price) / max(ref_price, 1e-12)
            if impact > MAX_IMPACT_BP/10000:
                return False, f"ob=impact {impact*100:.2f}%>{MAX_IMPACT_BP/100:.2f}%"
            return True, f"ob=ok(spread={spread*100:.2f}%,impact={impact*100:.2f}%)"
        else:
            return True, f"ob=ok(spread={spread*100:.2f}%)"
    except Exception as e:
        return True, f"ob=err({e})"

def budget_qty(sym, price, avail_usdt):
    if sym not in LIMITS:
        return 0.0
    lm = LIMITS[sym]
    budget = min(avail_usdt, max_trade_for(sym))
    if budget <= 0:
        return 0.0
    q = round_step(budget / price, lm["qty_step"])
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return 0.0
    return q

def can_place_buy(sym, q, price, usdt_free):
    if q <= 0:
        return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return False
    needed = q * price * (1 + TAKER_FEE + SLIP_BUFFER)
    return needed <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)

def can_place_sell(sym, q, price, coin_bal_now):
    if q <= 0:
        return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return False
    return q <= coin_bal_now + 1e-12

def aggregate_position(sym):
    entries = STATE[sym]["position_entries"]
    total_qty = sum(e["qty"] for e in entries) if entries else 0.0
    avg_price = (sum(e["qty"] * e["buy_price"] for e in entries) / total_qty) if total_qty > 0 else 0.0
    return total_qty, avg_price

def net_pnl(sym, current_price):
    entries = STATE[sym]["position_entries"]
    if not entries:
        return 0.0
    total_qty = sum(e["qty"] for e in entries)
    avg_price = sum(e["qty"] * e["buy_price"] for e in entries) / total_qty
    gross_qty = sum(e["buy_qty_gross"] for e in entries)
    cost = avg_price * gross_qty
    proceeds = current_price * total_qty * (1 - TAKER_FEE)
    return proceeds - cost

def create_new_entry(sym, buy_price, qty_gross):
    STATE[sym]["position_entries"].append({
        "qty": qty_gross * (1 - TAKER_FEE),
        "buy_price": buy_price,
        "buy_qty_gross": qty_gross
    })
    _save_state()

# daily_report, restore_positions, cap_btc_sell_qty, try_liquidity_recovery, trade_cycle, and __main__...
# — остаются без изменений, с сохранённой логикой из предыдущего варианта
# Только убедись, что везде используется aggregate_position, net_pnl, position_entries, как ранее.

if __name__ == "__main__":
    logging.info("🚀 Bot starting with Redis-safe initialization + aggregated position logic")
    tg_event("🚀 Bot starting with Redis-safe initialization + aggregated position logic")
    init_state()
    load_symbol_limits()
    restore_positions()
    # ... rest of startup message
    while True:
        try:
            trade_cycle()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.error(f"Global error: {e}\n{tb}")
            if time.time() - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"Global error: {e}")
                _last_err_ts = time.time()
        time.sleep(LOOP_SLEEP)
