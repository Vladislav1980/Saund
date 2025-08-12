# -*- coding: utf-8 -*-
# Bybit Spot bot (v3.1):
# - Вход "2 из 3" (EMA/RSI/MACD), смягчённые пороги
# - Net PnL >= $1 после обеих комиссий (taker 0.0018)
# - Плавающий бюджет 150–230 USDT, с ЖЁСТКИМ минимумом $150 на ЛЮБОЙ ордер
# - Safety-buffer $2, чтобы не ловить 170131 (Insufficient balance) на грани
# - Redis + файл состояния, дневной отчёт
# - Подробные логи в файл; в Telegram только события (старт/восстановление/BUY/SELL/ошибки/дневной)
# - Анти rate-limit: кэш /wallet-balance + backoff + антиспам ошибок

import os, time, math, logging, datetime, json, random, traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

try:
    import redis
except Exception:
    redis = None

# ==================== CONFIG ====================

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

REDIS_URL = os.getenv("REDIS_URL", "")
REDIS_KEY = os.getenv("REDIS_KEY", "bybit_spot_bot_state_v3_1")

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

# Fees (market => taker)
TAKER_FEE_SPOT = 0.0018
MAKER_FEE_SPOT = 0.0010  # not used

# Money management
RESERVE_BALANCE = 1.0
MIN_TRADE_USDT = 150.0
MAX_TRADE_USDT = 230.0
FLOAT_BUDGET_MODE = "signal"   # "signal" | "random" | "fixed_max" | "fixed_min"
ENFORCE_MIN_PER_ORDER = True   # строго не входить, если бюджет < MIN_TRADE_USDT
SAFETY_BUFFER_USDT = 2.0       # чтобы не упереться в ноль и не словить 170131
MAX_ORDERS_PER_CYCLE = 0       # 0 = без лимита; >0 = ограничить кол-во новых ордеров за цикл

# Risk / logic
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.15
MAX_AVERAGES = 3
STOP_LOSS_PCT = 0.03
LAST_SELL_REENTRY_PCT = 0.003  # 0.3%

# Profit (NET)
MIN_PROFIT_PCT  = 0.005
MIN_NET_ABS_USD = 1.0

# Ops
INTERVAL = "1"
LOOP_SLEEP = 60
STATE_FILE = "state.json"

# Daily report (server local time)
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30

# Rate-limit helpers
WALLET_CACHE_TTL = 5.0             # сек — кэш /wallet-balance
REQUEST_BACKOFF_BASE = 3.0         # стартовая пауза при 403/10006
REQUEST_BACKOFF_MAX  = 45.0        # максимум паузы
ERROR_TG_COOLDOWN = 120.0          # антиспам ошибок в ТГ (сек)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception:
        logging.error("Telegram send failed")

def log(msg: str):
    logging.info(msg)

def tg_event(msg: str):
    send_tg(msg)

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
cycle_count = 0

_last_error_ts = 0.0  # для антиспама

def _default_symbol_state():
    return {
        "positions": [],     # {buy_price, qty (net), buy_qty_gross, tp}
        "pnl": 0.0,
        "count": 0,
        "avg_count": 0,
        "last_sell_price": 0.0,
        "max_drawdown": 0.0
    }

def state_to_json():
    return json.dumps(STATE, ensure_ascii=False)

def save_state_file():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(state_to_json())
    except Exception as e:
        logging.error(f"save_state_file error: {e}")

def load_state_file():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        return True
    except Exception:
        return False

def redis_client():
    if not REDIS_URL or redis is None:
        return None
    try:
        return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    except Exception as e:
        logging.error(f"Redis connect error: {e}")
        return None

def save_state():
    rc = redis_client()
    if rc:
        try:
            rc.set(REDIS_KEY, state_to_json())
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    save_state_file()

def init_state():
    global STATE
    restored_from = "FRESH"
    rc = redis_client()
    if rc:
        try:
            raw = rc.get(REDIS_KEY)
            if raw:
                STATE = json.loads(raw)
                restored_from = "REDIS"
        except Exception as e:
            logging.error(f"Redis load error: {e}")
    if not STATE:
        if load_state_file():
            restored_from = "FILE"
        else:
            STATE = {}

    for sym in SYMBOLS:
        STATE.setdefault(sym, _default_symbol_state())

    log(f"🚀 Старт бота. Восстановление состояния: {restored_from}")
    tg_event(f"🚀 Старт бота. Восстановление состояния: {restored_from}")

    if restored_from != "FRESH":
        lines = []
        for sym in SYMBOLS:
            s = STATE[sym]
            cur_q = sum(p.get("qty", 0.0) for p in s["positions"])
            lines.append(f"{sym}: позиций={len(s['positions'])}, qty={cur_q:.6f}, pnl_acc={s['pnl']:.2f}")
        tg_event("♻️ Восстановление позиций:\n" + "\n".join(lines))

    save_state()

# ---------- Bybit helpers (backoff + wallet cache) ----------

def _safe_call(func, *args, **kwargs):
    """Обёртка с бэкоффом для частых эндпойнтов."""
    delay = REQUEST_BACKOFF_BASE
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            is_rate = ("403" in msg or "rate limit" in msg.lower() or "10006" in msg)
            if not is_rate:
                raise
            global _last_error_ts
            now = time.time()
            if now - _last_error_ts > ERROR_TG_COOLDOWN:
                tg_event(f"⚠️ Rate limit/403. Бэкофф {delay:.0f}s. Детали: {msg}")
                _last_error_ts = now
            log(f"Rate limit/403 -> sleep {delay:.0f}s; err: {msg}")
            time.sleep(delay)
            delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)

_wallet_cache = {"ts": 0.0, "coins": None}

def get_wallet_cached(force=False):
    """Кэшируем /wallet-balance. Обновляем раз в WALLET_CACHE_TTL сек."""
    if not force and _wallet_cache["coins"] is not None and time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL:
        return _wallet_cache["coins"]
    r = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = r["result"]["list"][0]["coin"]
    _wallet_cache["coins"] = coins
    _wallet_cache["ts"] = time.time()
    return coins

def get_balance_usdt_from(coins):
    return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))

def get_coin_balance_from(coins, sym):
    coin = sym.replace("USDT","")
    return float(next((c["walletBalance"] for c in coins if c["coin"]==coin), 0.0))

# ==================== BYBIT MARKET DATA ====================

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

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except Exception:
        return qty

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

# ==================== SIGNALS (2 of 3) ====================

def _last_cross(series_fast, series_slow, lookback=3):
    cross = 0
    for i in range(1, min(lookback+1, len(series_fast))):
        if series_fast.iloc[-i-1] <= series_slow.iloc[-i-1] and series_fast.iloc[-i] > series_slow.iloc[-i]:
            cross = 1; break
        if series_fast.iloc[-i-1] >= series_slow.iloc[-i-1] and series_fast.iloc[-i] < series_slow.iloc[-i]:
            cross = -1; break
    return cross

def calc_signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, "insufficient data", 0.0

    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd_obj = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd_obj.macd(), macd_obj.macd_signal()

    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, "
            f"RSI={last['rsi']:.2f}, MACD={last['macd']:.4f}, SIG={last['macd_signal']:.4f}")

    buy_pts = 0; sell_pts = 0
    if last["ema9"] > last["ema21"]: buy_pts += 1
    if last["ema9"] < last["ema21"]: sell_pts += 1
    if last["rsi"] > 45: buy_pts += 1
    if last["rsi"] < 55: sell_pts += 1
    if last["macd"] > last["macd_signal"]: buy_pts += 1
    if last["macd"] < last["macd_signal"]: sell_pts += 1

    macd_cross = _last_cross(df["macd"], df["macd_signal"], lookback=3)

    sig = "none"
    if buy_pts >= 2 or macd_cross == 1: sig = "buy"
    if sell_pts >= 2 or macd_cross == -1:
        if sig == "buy":
            if last["ema9"] < last["ema21"]: sig = "sell"
            elif last["ema9"] > last["ema21"]: sig = "buy"
            else: sig = "none"
        else:
            sig = "sell"

    ema_gap = abs(last["ema9"] - last["ema21"]) / max(abs(last["ema21"]), 1e-12)
    ema_score = max(0.0, min(1.0, ema_gap * 10))
    if sig == "buy":
        pts_part = buy_pts / 3.0
        rsi_part = max(0.0, min(1.0, (last["rsi"] - 45) / 25))
    elif sig == "sell":
        pts_part = sell_pts / 3.0
        rsi_part = max(0.0, min(1.0, (55 - last["rsi"]) / 25))
    else:
        pts_part = 0.0; rsi_part = 0.0
    confidence = max(0.0, min(1.0, 0.5*pts_part + 0.3*ema_score + 0.2*rsi_part))
    return sig, float(last["atr"]), info, confidence

# ==================== POSITION / ORDERS ====================

def choose_trade_budget(confidence: float, avail_usdt: float) -> float:
    bmin, bmax = MIN_TRADE_USDT, MAX_TRADE_USDT
    if FLOAT_BUDGET_MODE == "signal":
        budget = bmin + (bmax - bmin) * max(0.0, min(1.0, confidence))
    elif FLOAT_BUDGET_MODE == "random":
        budget = random.uniform(bmin, bmax)
    elif FLOAT_BUDGET_MODE == "fixed_max":
        budget = bmax
    else:
        budget = bmin
    return max(0.0, min(budget, avail_usdt))

def qty_from_budget(sym: str, price: float, budget_usdt: float) -> float:
    if sym not in LIMITS: return 0.0
    step = LIMITS[sym]["qty_step"]
    q = adjust_qty(budget_usdt / price, step)
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

def log_trade(sym, side, price, qty, pnl, reason=""):
    usdt_val = price * qty
    msg = (f"{side} {sym} @ {price:.6f}, qty={qty:.8f}, USDT≈{usdt_val:.2f}, "
           f"PnL(net)={pnl:.2f} | {reason}")
    log(msg)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt_val},{pnl},{reason}\n")
    save_state()

def init_positions_from_balance():
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            price = df["c"].iloc[-1]
            coins = get_wallet_cached(force=True)
            bal = get_coin_balance_from(coins, sym)
            if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
                qty_net = adjust_qty(bal, LIMITS[sym]["qty_step"])
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                tp = price + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"].append({
                    "buy_price": price,
                    "qty": qty_net,
                    "buy_qty_gross": qty_net / (1 - TAKER_FEE_SPOT),
                    "tp": tp
                })
                log(f"♻️ [{sym}] Восстановлена позиция qty={qty_net:.8f}, price={price:.6f}, tp={tp:.6f}")
        except Exception as e:
            log(f"[{sym}] Ошибка восстановления позиций: {e}")
            tg_event(f"[{sym}] Ошибка восстановления позиций: {e}")
    save_state()

def send_daily_report():
    try:
        coins = get_wallet_cached(force=True)
        by_coin = {c["coin"]: float(c["walletBalance"]) for c in coins}
        usdt = by_coin.get("USDT", 0.0)
        lines = [f"📊 Daily Report {datetime.datetime.now().date()}",
                 f"USDT: {usdt:.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT","")
            bal = by_coin.get(base, 0.0)
            price = float(get_kline(sym)["c"].iloc[-1])
            val = price * bal
            s = STATE[sym]
            cur_pos = sum(p["qty"] for p in s["positions"])
            dd = s.get("max_drawdown", 0.0)
            lines.append(f"{sym}: balance={bal:.6f} (~{val:.2f} USDT), "
                         f"trades={s['count']}, pnl={s['pnl']:.2f}, maxDD={dd*100:.2f}%, curPosQty={cur_pos:.6f}")
        tg_event("\n".join(lines))
    except Exception as e:
        log(f"Ошибка формирования отчёта: {e}")
        tg_event(f"Ошибка формирования отчёта: {e}")

# ==================== CORE ====================

def trade_cycle():
    global LAST_REPORT_DATE, cycle_count, _last_error_ts
    orders_this_cycle = 0

    try:
        coins = get_wallet_cached(force=True)   # 1 вызов на цикл
        usdt = get_balance_usdt_from(coins)
    except Exception as e:
        log(f"Ошибка получения баланса USDT: {e}")
        now = time.time()
        if now - _last_error_ts > ERROR_TG_COOLDOWN:
            tg_event(f"Ошибка получения баланса USDT: {e}")
            _last_error_ts = now
        return

    avail = max(0.0, usdt - RESERVE_BALANCE)
    log(f"💰 Баланс USDT={usdt:.2f} | Доступно={avail:.2f}")

    for sym in SYMBOLS:
        # ограничение на кол-во новых ордеров (по желанию)
        if MAX_ORDERS_PER_CYCLE > 0 and orders_this_cycle >= MAX_ORDERS_PER_CYCLE:
            log("⏸ Лимит ордеров на цикл достигнут — пропускаем оставшиеся символы")
            break

        try:
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ❗Нет данных по свечам — пропускаем")
                continue

            sig, atr, info_ind, confidence = calc_signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance_from(coins, sym)  # из кэша
            value = coin_bal * price

            log(f"[{sym}] 🔎 Сигнал={sig.upper()} (conf={confidence:.2f}), price={price:.6f}, "
                f"balance={coin_bal:.8f} (~{value:.2f} USDT) | {info_ind}")

            # обновление макс. просадки
            if state["positions"]:
                avg_entry = sum(p["buy_price"] * p["qty"] for p in state["positions"]) / \
                            sum(p["qty"] for p in state["positions"])
                curr_dd = (avg_entry - price) / avg_entry
                if curr_dd > state["max_drawdown"]:
                    state["max_drawdown"] = curr_dd

            # ----- управление открытыми -----
            new_positions = []
            for pos in state["positions"]:
                b = pos["buy_price"]
                q_net = adjust_qty(pos["qty"], limits["qty_step"])
                tp = pos["tp"]
                buy_gross = pos.get("buy_qty_gross", q_net / (1 - TAKER_FEE_SPOT))

                cost_usdt = b * buy_gross
                proceeds_usdt = price * q_net * (1 - TAKER_FEE_SPOT)
                pnl_net = proceeds_usdt - cost_usdt
                min_net_req = max(MIN_NET_ABS_USD, price * q_net * MIN_PROFIT_PCT)

                # STOP-LOSS
                if price <= b * (1 - STOP_LOSS_PCT):
                    _safe_call(session.place_order, category="spot", symbol=sym, side="Sell",
                               orderType="Market", qty=str(q_net))
                    reason = (f"STOP-LOSS: price {price:.6f} ≤ {b*(1-STOP_LOSS_PCT):.6f}; pnl_net={pnl_net:.2f}")
                    log_trade(sym, "SELL", price, q_net, pnl_net, reason)
                    tg_event(f"❗{sym} SELL (SL) @ {price:.6f}, qty={q_net:.8f}, pnl={pnl_net:.2f}")
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    continue

                # TAKE-PROFIT
                if price >= tp and pnl_net >= min_net_req:
                    _safe_call(session.place_order, category="spot", symbol=sym, side="Sell",
                               orderType="Market", qty=str(q_net))
                    reason = (f"TP HIT: price {price:.6f} ≥ tp {tp:.6f} и pnl_net {pnl_net:.2f} ≥ {min_net_req:.2f}")
                    log_trade(sym, "SELL", price, q_net, pnl_net, reason)
                    tg_event(f"✅ {sym} SELL @ {price:.6f}, qty={q_net:.8f}, pnl={pnl_net:.2f}")
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                else:
                    new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                    if new_tp != tp:
                        log(f"[{sym}] 📈 TP: {tp:.6f} → {new_tp:.6f}")
                    pos["tp"] = new_tp
                    new_positions.append(pos)
                    if price < tp:
                        log(f"[{sym}] 🔸Не продаём: цена {price:.6f} < TP {tp:.6f}")
                    elif pnl_net < min_net_req:
                        log(f"[{sym}] 🔸Не продаём: pnl_net {pnl_net:.2f} < требуемого {min_net_req:.2f}")

            state["positions"] = new_positions

            # ----- ENTRIES -----
            if sig == "buy":
                # averaging
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty"] for p in state["positions"])
                    avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_q
                    drawdown = (price - avg_price) / avg_price
                    if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN:
                        # бюджет (с проверками минимума и фактического доступного)
                        budget = choose_trade_budget(confidence, avail)
                        if ENFORCE_MIN_PER_ORDER and budget < MIN_TRADE_USDT:
                            log(f"[{sym}] ❌ Не усредняем: бюджет {budget:.2f} < {MIN_TRADE_USDT}")
                        else:
                            coins = get_wallet_cached()
                            usdt_now  = get_balance_usdt_from(coins)
                            real_avail = max(0.0, usdt_now - RESERVE_BALANCE - SAFETY_BUFFER_USDT)
                            use_usdt = min(budget, real_avail, MAX_TRADE_USDT)
                            if use_usdt < MIN_TRADE_USDT:
                                log(f"[{sym}] ❌ Не усредняем: доступно {real_avail:.2f} < {MIN_TRADE_USDT} (с запасом)")
                            else:
                                qty_gross = qty_from_budget(sym, price, use_usdt)
                                if qty_gross > 0:
                                    _safe_call(session.place_order, category="spot", symbol=sym, side="Buy",
                                               orderType="Market", qty=str(qty_gross))
                                    qty_net = qty_gross * (1 - TAKER_FEE_SPOT)
                                    tp = price + TRAIL_MULTIPLIER * atr
                                    STATE[sym]["positions"].append({
                                        "buy_price": price, "qty": qty_net,
                                        "buy_qty_gross": qty_gross, "tp": tp
                                    })
                                    state["count"] += 1
                                    state["avg_count"] += 1
                                    orders_this_cycle += 1
                                    log_trade(sym, "BUY (avg)", price, qty_net, 0.0,
                                              reason=(f"drawdown={drawdown:.4f}, conf={confidence:.2f}, "
                                                      f"use_usdt={use_usdt:.2f}, qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}"))
                                    tg_event(f"🟢 {sym} BUY(avg) @ {price:.6f}, qty={qty_net:.8f}")
                                    # обновим кэш после сделки
                                    get_wallet_cached(force=True)
                                else:
                                    log(f"[{sym}] ❌ Не усредняем: не прошли бирж. фильтры (min_amt/min_qty)")
                    else:
                        log(f"[{sym}] 🔸Не усредняем: drawdown {drawdown:.4f} вне (-{MAX_DRAWDOWN})")

                # fresh entry
                elif not state["positions"]:
                    if state["last_sell_price"] and abs(price - state["last_sell_price"]) / price < LAST_SELL_REENTRY_PCT:
                        log(f"[{sym}] 🔸Не покупаем: близко к последней продаже ({state['last_sell_price']:.6f})")
                    else:
                        budget = choose_trade_budget(confidence, avail)
                        if ENFORCE_MIN_PER_ORDER and budget < MIN_TRADE_USDT:
                            log(f"[{sym}] ❌ Не покупаем: бюджет {budget:.2f} < {MIN_TRADE_USDT}")
                        else:
                            coins = get_wallet_cached()
                            usdt_now  = get_balance_usdt_from(coins)
                            real_avail = max(0.0, usdt_now - RESERVE_BALANCE - SAFETY_BUFFER_USDT)
                            use_usdt = min(budget, real_avail, MAX_TRADE_USDT)
                            if use_usdt < MIN_TRADE_USDT:
                                log(f"[{sym}] ❌ Не покупаем: доступно {real_avail:.2f} < {MIN_TRADE_USDT} (с запасом)")
                            else:
                                qty_gross = qty_from_budget(sym, price, use_usdt)
                                if qty_gross > 0:
                                    _safe_call(session.place_order, category="spot", symbol=sym, side="Buy",
                                               orderType="Market", qty=str(qty_gross))
                                    qty_net = qty_gross * (1 - TAKER_FEE_SPOT)
                                    tp = price + TRAIL_MULTIPLIER * atr
                                    STATE[sym]["positions"].append({
                                        "buy_price": price, "qty": qty_net,
                                        "buy_qty_gross": qty_gross, "tp": tp
                                    })
                                    state["count"] += 1
                                    orders_this_cycle += 1
                                    log_trade(sym, "BUY", price, qty_net, 0.0,
                                              reason=(f"conf={confidence:.2f}, use_usdt={use_usdt:.2f}, "
                                                      f"qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}, {info_ind}"))
                                    tg_event(f"🟢 {sym} BUY @ {price:.6f}, qty={qty_net:.8f}")
                                    get_wallet_cached(force=True)
                                else:
                                    log(f"[{sym}] ❌ Не покупаем: не прошли бирж. фильтры (min_amt/min_qty)")
            else:
                if not state["positions"]:
                    log(f"[{sym}] 🔸Нет покупки: сигнал {sig}, confidence={confidence:.2f}")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            log(f"[{sym}] Ошибка цикла: {e}\n{tb}")
            now = time.time()
            if now - _last_error_ts > ERROR_TG_COOLDOWN:
                tg_event(f"[{sym}] Ошибка цикла: {e}")
                _last_error_ts = now

    save_state()
    cycle_count += 1

    now = datetime.datetime.now()
    if (now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and
            LAST_REPORT_DATE != now.date()):
        send_daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# ==================== MAIN ====================

if __name__ == "__main__":
    try:
        init_state()
        load_symbol_limits()
        init_positions_from_balance()
        tg_event(f"⚙️ Параметры: TAKER_FEE={TAKER_FEE_SPOT}, "
                 f"BUDGET=[{MIN_TRADE_USDT};{MAX_TRADE_USDT}] mode={FLOAT_BUDGET_MODE}, "
                 f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.2f}%, "
                 f"DD={MAX_DRAWDOWN*100:.0f}%, reentry>{LAST_SELL_REENTRY_PCT*100:.1f}%")

        while True:
            try:
                trade_cycle()
            except Exception as e:
                tb = traceback.format_exc(limit=2)
                log(f"Global cycle error: {e}\n{tb}")
                now = time.time()
                if now - _last_error_ts > ERROR_TG_COOLDOWN:
                    tg_event(f"Global cycle error: {e}")
                    _last_error_ts = now
            time.sleep(LOOP_SLEEP)

    except Exception as e:
        tb = traceback.format_exc()
        log(f"Fatal error on start: {e}\n{tb}")
        tg_event(f"Fatal error on start: {e}")
