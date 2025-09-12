# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — v3 + NetPnL-трейлинг + Profit Tiers + Smart Averaging

Что добавлено к твоей версии:
- Все расчёты прибыли ведутся как net (после комиссий TAKER_FEE).
- Минимальная фиксация прибыли: ≥ $1.50, но бот НЕ «ждёт падения к 1.5»:
  реализованы ступени прибыли [$1.5, $5, $10, $20, ...] + жёсткая защита ступени.
- Трейлинг от пика: если netPnL просел от максимума ≥ $0.6 — выходим.
- Усреднение только при ощутимом откате: AVG_MIN_DRAWDOWN_USD (например, 5$)
  и/или AVG_MIN_DRAWDOWN_PCT (например, 3%).
- Защита от мгновенной повторной покупки после продажи:
  требуется сдвиг цены от last_sell_price и/или таймаут.
- Читаемые логи с причинами «почему пропустили сделку».

Биржа: Bybit Spot (pybit.unified_trading)
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

TAKER_FEE = 0.0018  # 0.18% (Bybit Spot)
BASE_MAX_TRADE_USDT = 35.0
MAX_TRADE_OVERRIDES = {"TONUSDT": 70.0, "AVAXUSDT": 70.0, "ADAUSDT": 60.0, "BTCUSDT": 90.0}
def max_trade_for(sym: str) -> float:
    return float(MAX_TRADE_OVERRIDES.get(sym, BASE_MAX_TRADE_USDT))

RESERVE_BALANCE = 1.0

# Торговые индикаторы/логика
TRAIL_MULTIPLIER = 1.5
STOP_LOSS_PCT = 0.03

# Усреднение — только при ощутимом откате
MAX_AVERAGES = 3
AVG_MIN_DRAWDOWN_USD = 5.0      # абсолютный «откат в $» между средней ценой и текущей
AVG_MIN_DRAWDOWN_PCT = 0.03     # или в процентах (3% по умолчанию)
MAX_DRAWDOWN_PCT_GUARD = 0.12   # страховка: не усредняем если падение >12% (анти-нож)

# Минимальная прибыль (после комиссий)
MIN_PROFIT_PCT   = 0.005        # 0.5% от нотиона позиции
MIN_NET_PROFIT   = 1.50         # минимум $1.5 net
MIN_NET_ABS_USD  = 1.50         # то же, явный «порог» в долларах

# Ступени прибыли (после комиссий) и их «жёсткая защита»
PROFIT_TIERS     = [1.50, 5.00, 10.00, 20.00]  # можно расширять: 50, 100, ...
TIER_LOCK_GAP    = 0.20        # допуск под ступенью: если pnl < tier - gap → продаём

# Трейлинг от пика netPnL
TRAIL_PNL_TRIGGER = 1.50       # включаем трейлинг, когда достигли хотя бы $1.5
TRAIL_PNL_GAP     = 0.60       # если отступили от пика на $0.6 — фиксируем

# Анти «перекуп после продажи»
REENTRY_MIN_USD        = 0.50   # минимальный сдвиг от цены последней продажи (в $)
REENTRY_MIN_PCT        = 0.006  # и/или в процентах (0.6%)
REENTRY_COOLDOWN_MIN   = 10     # и/или время ожидания после продажи (минут)

# Гварды по ликвидности/ордебуку/объёму
SLIP_BUFFER = 0.006
USE_VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_FACTOR_MIN = 0.4
MIN_CANDLE_NOTIONAL = 15.0

USE_ORDERBOOK_GUARD = True
OB_LIMIT_DEPTH = 25
MAX_SPREAD_BP = 25
MAX_IMPACT_BP = 35

# Режим временного высвобождения USDT при «застое»
LIQUIDITY_RECOVERY = True
LIQ_RECOVERY_USDT_MIN = 20.0
LIQ_RECOVERY_USDT_TARGET = 60.0

# BTC: ограничения на разовую продажу и «неснижаемый остаток»
BTC_SYMBOL = "BTCUSDT"
BTC_MAX_SELL_FRACTION_TRADE = 0.18
BTC_MIN_KEEP_USD = 3000.0

# Прочее
INTERVAL = "1"              # 1m
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30
WALLET_CACHE_TTL = 5.0
REQUEST_BACKOFF = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN = 90.0

# ============ LOGGING ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

# ============ Telegram ============
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

# ============ API ============
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# ============ STATE ============
STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

def _state_key(): return "bybit_spot_state_v3"

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
            "last_sell_ts": 0.0,
            "max_drawdown": 0.0
        })
    _save_state()

# ============ HELPERS ============
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
    if (not force) and _wallet_cache["coins"] is not None and time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL:
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

def signal(df):
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

def volume_ok(df):
    if not USE_VOLUME_FILTER:
        return True, "vol=off"
    if len(df) < max(VOL_MA_WINDOW, 20):
        return True, "vol=warmup"
    vol_ma = df["vol"].rolling(VOL_MA_WINDOW).mean().iloc[-2]
    last_vol = df["vol"].iloc[-1]
    last_close = df["c"].iloc[-1]
    notional = last_vol * last_close
    if vol_ma is None or pd.isna(vol_ma):
        return True, "vol=ma_na"
    if last_vol < VOL_FACTOR_MIN * vol_ma:
        return False, f"vol_guard: last<{VOL_FACTOR_MIN:.2f}*MA"
    if notional < MIN_CANDLE_NOTIONAL:
        return False, f"vol_guard: notion<{MIN_CANDLE_NOTIONAL}"
    return True, "vol=ok"

def orderbook_ok(sym: str, side: str, qty_base: float, ref_price: float):
    if not USE_ORDERBOOK_GUARD:
        return True, "ob=off"
    try:
        ob = _safe_call(session.get_orderbook, category="spot", symbol=sym, limit=OB_LIMIT_DEPTH)["result"]
        best_ask = float(ob["a"][0][0]); best_bid = float(ob["b"][0][0])
        spread = (best_ask - best_bid) / max(best_bid, 1e-12)
        if spread > MAX_SPREAD_BP/10000.0:
            return False, f"ob=spread {spread*100:.2f}%> {MAX_SPREAD_BP/100:.2f}%"
        if side.lower() == "buy":
            need = qty_base; cost = 0.0
            for px, q in ob["a"]:
                px = float(px); q = float(q)
                take = min(need, q); cost += take * px; need -= take
                if need <= 1e-15: break
            if need > 0: return False, "ob=depth shallow"
            vwap = cost/qty_base
            impact = (vwap - ref_price) / max(ref_price,1e-12)
            if impact > MAX_IMPACT_BP/10000.0:
                return False, f"ob=impact {impact*100:.2f}%>{MAX_IMPACT_BP/100:.2f}%"
            return True, f"ob=ok(spread={spread*100:.2f}%,impact={impact*100:.2f}%)"
        else:
            return True, f"ob=ok(spread={spread*100:.2f}%)"
    except Exception as e:
        return True, f"ob=err({e})"

def budget_qty(sym: str, price: float, avail_usdt: float) -> float:
    if sym not in LIMITS: return 0.0
    lm = LIMITS[sym]
    budget = min(avail_usdt, max_trade_for(sym))
    if budget <= 0: return 0.0
    q = round_step(budget / price, lm["qty_step"])
    if q < lm["min_qty"] or q * price < lm["min_amt"]: return 0.0
    return q

def can_place_buy(sym: str, q: float, price: float, usdt_free: float) -> (bool, str):
    if q <= 0: return False, "q<=0"
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return False, f"below_limits(min_qty={lm['min_qty']},min_amt={lm['min_amt']})"
    need = q * price * (1 + TAKER_FEE + SLIP_BUFFER)
    ok = need <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)
    return ok, (f"need={need:.2f}" if ok else f"need {need:.2f} > free {max(0.0,usdt_free-RESERVE_BALANCE):.2f}")

def can_place_sell(sym: str, q_net: float, price: float, coin_bal_now: float):
    if q_net <= 0: return False
    lm = LIMITS[sym]
    if q_net < lm["min_qty"] or q_net * price < lm["min_amt"]: return False
    return q_net <= coin_bal_now + 1e-12

def append_or_update_position(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    state = STATE[sym]
    if not state["positions"]:
        state["positions"] = [{
            "buy_price": price,
            "qty": qty_net,
            "buy_qty_gross": qty_gross,
            "tp": tp,
            "max_pnl": 0.0
        }]
    else:
        p = state["positions"][0]
        total_qty = p["qty"] + qty_net
        new_price = (p["qty"] * p["buy_price"] + qty_net * price) / max(total_qty, 1e-12)
        p["qty"] = total_qty
        p["buy_price"] = new_price
        p["buy_qty_gross"] += qty_gross
        p["tp"] = tp
        p["max_pnl"] = 0.0
    _save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    cost = buy_price * buy_qty_gross
    proceeds = price * qty_net * (1 - TAKER_FEE)
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    pct_req = price * qty_net * MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD, MIN_NET_PROFIT, pct_req)

# ====== DAILY REPORT ======
def daily_report():
    try:
        coins = get_wallet(True)
        by = {c["coin"]: float(c["walletBalance"]) for c in coins}
        today_str = str(datetime.date.today())
        lines = ["📊 Daily Report " + today_str, f"USDT: {by.get('USDT',0.0):.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT", "")
            bal_qty = by.get(base, 0.0)
            df = get_kline(sym)
            price = float(df["c"].iloc[-1]) if not df.empty else 0.0
            cur_value = price * bal_qty
            s = STATE[sym]
            lines.append(
                f"{sym}: qty={bal_qty:.6f}, value={cur_value:.2f}, trades={s['count']}, "
                f"realized_total={s['pnl']:.2f}"
            )
        tg_event("\n".join(lines))
    except Exception as e:
        logging.info(f"daily_report error: {e}")

# ====== Восстановление позиций по фактическому балансу ======
def restore_positions():
    restored = []
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            price_now = df["c"].iloc[-1]
            coins = get_wallet(True)
            bal = coin_balance(coins, sym)
            lm = LIMITS.get(sym, {})
            if price_now and bal * price_now >= lm.get("min_amt", 0.0) and bal >= lm.get("min_qty", 0.0):
                q_net = round_step(bal, lm.get("qty_step", 1.0))
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                tp = price_now + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"] = [{
                    "buy_price": price_now,
                    "qty": q_net,
                    "buy_qty_gross": q_net / (1 - TAKER_FEE),
                    "tp": tp,
                    "max_pnl": 0.0
                }]
                restored.append(f"{sym}: qty={q_net:.8f} @ {price_now:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    _save_state()
    if restored:
        tg_event("♻️ Восстановлены позиции:\n" + "\n".join(restored))
    else:
        tg_event("ℹ️ Позиции не найдены для восстановления.")

# ====== Ордеры ======
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
                logging.info(f"[{sym}] 170131 on BUY qty={qty}. Shrink & retry")
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
    _safe_call(session.place_order,
        category="spot", symbol=sym, side="Sell",
        orderType="Market", qty=str(qty))
    return True

def cap_btc_sell_qty(sym: str, q_net: float, price: float, coin_bal_now: float) -> float:
    if sym != BTC_SYMBOL: return q_net
    cap_by_fraction = coin_bal_now * BTC_MAX_SELL_FRACTION_TRADE
    keep_qty_floor = 0.0
    if BTC_MIN_KEEP_USD > 0:
        keep_qty_floor = max(0.0, (coin_bal_now * price - BTC_MIN_KEEP_USD) / max(price, 1e-12))
    allowed = max(0.0, min(q_net, cap_by_fraction, keep_qty_floor if keep_qty_floor > 0 else q_net))
    if allowed <= 0: allowed = min(q_net, cap_by_fraction)
    return max(0.0, allowed)

# ====== Временное высвобождение ликвидности ======
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
            q_g = p.get("buy_qty_gross", q_n / (1 - TAKER_FEE))
            pnl = net_pnl(price, p["buy_price"], q_n, q_g)
            if pnl > MIN_NET_ABS_USD and can_place_sell(sym, q_n, price, coin_bal):
                sell_try = max(lm["min_qty"], q_n * 0.2)
                if sym == BTC_SYMBOL: sell_try = cap_btc_sell_qty(sym, sell_try, price, coin_bal)
                if sell_try <= 0: continue
                est_usdt = price * sell_try * (1 - TAKER_FEE)
                candidates.append((sym, price, sell_try, est_usdt, pnl, coin_bal, lm))
    candidates.sort(key=lambda x: (x[4] / max(x[3],1e-9)), reverse=True)
    for sym, price, sell_q, est, pnl, coin_bal, lm in candidates:
        if target_gain <= 0: break
        sell_q = min(sell_q, round_step(target_gain / max(price*(1-TAKER_FEE),1e-9), lm["qty_step"]))
        if sell_q < lm["min_qty"] or sell_q * price < lm["min_amt"]: continue
        if not can_place_sell(sym, sell_q, price, coin_bal): continue
        try:
            _attempt_sell(sym, sell_q)
            tg_event(f"🟠 RECOVERY SELL {sym} @ {price:.6f}, qty={sell_q:.8f} (~{price*sell_q*(1-TAKER_FEE):.2f} USDT)")
            # пропорционально уменьшаем позицию
            rest = sell_q; newpos = []
            for p in STATE[sym]["positions"]:
                if rest <= 0: newpos.append(p); continue
                take = min(p["qty"], rest); ratio = take / max(p["qty"], 1e-12)
                p["qty"] -= take; p["buy_qty_gross"] *= max(0.0, 1.0 - ratio); rest -= take
                if p["qty"] > 0: newpos.append(p)
            STATE[sym]["positions"] = newpos; _save_state()
            target_gain -= price * sell_q * (1 - TAKER_FEE)
            coins = get_wallet(True); usdt = usdt_balance(coins)
        except Exception as e:
            logging.info(f"[{sym}] recovery sell failed: {e}")

# ====== Основной цикл ======
def trade_cycle():
    global LAST_REPORT_DATE, _last_err_ts
    try:
        coins = get_wallet(True); usdt = usdt_balance(coins)
    except Exception as e:
        now = time.time()
        if now - _last_err_ts > TG_ERR_COOLDOWN:
            tg_event(f"Ошибка баланса: {e