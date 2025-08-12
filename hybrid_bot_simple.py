# -*- coding: utf-8 -*-
"""
Bybit Spot (Unified Trading) — лимитный (maker-first) бот
- Лимитные PostOnly ордера с адаптивным репрайсом
- Минимальная ожидаемая прибыль (после всех комиссий) >= $1
- Подробные DEBUG‑логи причин пропуска
- Телеграм-уведомления и ежедневный отчёт
- Защита синтетических позиций: без SL или отсрочка SL
"""

import os, time, math, logging, datetime, json
from decimal import Decimal, getcontext
import requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

# ==================== ENV ====================
load_dotenv()
API_KEY     = os.getenv("BYBIT_API_KEY")
API_SECRET  = os.getenv("BYBIT_API_SECRET")
TG_TOKEN    = os.getenv("TG_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
REDIS_URL   = os.getenv("REDIS_URL")

# ==================== CONFIG ====================
TG_VERBOSE = True

RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 105.0
MIN_NET_PROFIT    = 1.00         # чистыми после комиссий
STOP_LOSS_PCT     = 0.008        # 0.8%

# Доп. защита
SYNTHETIC_GRACE_HOURS = 6.0      # сколько часов не трогаем восстановленные позиции
DISABLE_SL_FOR_SYNTH  = True     # полностью отключить SL на синтетике (True приоритнее grace)
MAX_LOSS_USDT         = 4.0      # предельный убыток для стопа (0 — без ограничения)

# Комиссии (SPOT)
MAKER_FEE   = 0.0010             # 0.10%
TAKER_FEE   = 0.0018             # 0.18%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
LAST_REPORT_DATE = None
cycle_count = 0

# Кэш лимитов в Redis на 12 часов
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

# Лимитный режим (maker-first, без срыва в маркет)
POST_ONLY            = True
MAX_WAIT_SEC         = 40           # максимум ждать один лимит
POLL_SEC             = 2
MAX_REPRICES         = 4            # максимум переустановок лимита
MAKER_LAG_BPS        = 2            # buy: bid*(1 - 0.0002); sell: ask*(1 + 0.0002)

getcontext().prec = 28

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

SKIP_LOG_TIMESTAMPS = {}
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

# ==================== UTIL ====================
def send_tg(msg: str):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg}
            )
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def should_log_skip(sym, key, interval=10):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval * 60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

def log_skip(sym, msg):
    logging.info(f"{sym}: {msg}")

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp, synthetic, born}]
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

# ==================== API HELPERS ====================
def api_call(fn, *args, **kwargs):
    wait = 0.35
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = str(e)
            logging.warning(f"API retry {fn.__name__} attempt={attempt+1} wait={wait:.2f}s error={err}")
            time.sleep(wait)
            wait = min(wait * 2.0, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

# ==================== LIMITS (lazy + Redis cache) ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    out = {}
    for item in lst:
        sym = item["symbol"]
        if sym not in SYMBOLS:
            continue
        lot = item.get("lotSizeFilter", {}) or {}
        priceF = item.get("priceFilter", {}) or {}
        out[sym] = {
            "min_qty": float(lot.get("minOrderQty", 0.0)),
            "qty_step": float(lot.get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(priceF.get("tickSize", 0.0001))
        }
    return out

def _limits_from_redis():
    raw = redis_client.get(LIMITS_REDIS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except:
        return None

def _limits_to_redis(limits: dict):
    try:
        redis_client.setex(LIMITS_REDIS_KEY, LIMITS_TTL_SEC, json.dumps(limits))
    except Exception as e:
        logging.warning(f"limits cache save failed: {e}")

def get_limits():
    """
    Ленивая загрузка лимитов:
    - память → Redis → API
    Если API недоступен — блокируем покупки, SELL остаётся разрешён.
    """
    global _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    if _LIMITS_MEM is not None:
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    cached = _limits_from_redis()
    if cached:
        _LIMITS_MEM = cached
        _LIMITS_OK = True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from Redis cache")
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    try:
        limits = _load_symbol_limits_from_api()
        _limits_to_redis(limits)
        _LIMITS_MEM = limits
        _LIMITS_OK = True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from API and cached")
    except Exception as e:
        _LIMITS_MEM = {}
        _LIMITS_OK = False
        _BUY_BLOCKED_REASON = f"LIMITS unavailable ({e}); BUY blocked, SELL allowed"
        log_msg(f"⚠️ {_BUY_BLOCKED_REASON}", True)

    return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

# ==================== MKT DATA & BALANCES ====================
def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# --------- Orderbook (Bybit unified) ----------
def get_orderbook(sym, depth=50):
    """Возвращает (best_bid, best_ask) как float. Безопасный парсер."""
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=depth)
        res = r.get("result", {})
        bids = res.get("b") or (res.get("list", {}) or {}).get("b") or []
        asks = res.get("a") or (res.get("list", {}) or {}).get("a") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        return best_bid, best_ask
    except Exception as e:
        logging.error(f"orderbook failed {sym}: {e}")
        return 0.0, 0.0

# ==================== ROUNDING ====================
def round_down(x, step):
    if step <= 0:
        return x
    return math.floor(x / step) * step

def round_price(price, tick):
    return round_down(price, tick)

def adjust_qty(qty, step):
    if step <= 0:
        return qty
    return math.floor(qty / step) * step

def get_qty(sym, price, usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, limits[sym]["qty_step"])
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS ====================
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, {"why":"short_df"}

    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    atr14 = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    macd_line, macd_sig = macd.macd(), macd.macd_signal()

    df = df.copy()
    df["ema9"] = ema9
    df["ema21"] = ema21
    df["rsi"] = rsi9
    df["atr"] = atr14
    df["macd"] = macd_line
    df["macd_signal"] = macd_sig

    last = df.iloc[-1]; prev = df.iloc[-2]

    two_of_three_buy = ((last["ema9"] > last["ema21"]) +
                        (last["rsi"] > 48) +
                        (last["macd"] > last["macd_signal"])) >= 2
    ema_cross_up = (prev["ema9"] <= prev["ema21"]) and (last["ema9"] > last["ema21"]) and (last["rsi"] > 45)

    two_of_three_sell = ((last["ema9"] < last["ema21"]) +
                         (last["rsi"] < 50) +
                         (last["macd"] < last["macd_signal"])) >= 2
    ema_cross_down = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"]) and (last["rsi"] < 55)

    info = dict(
        EMA9=float(last["ema9"]),
        EMA21=float(last["ema21"]),
        RSI=float(last["rsi"]),
        MACD=float(last["macd"]),
        SIG=float(last["macd_signal"])
    )

    if two_of_three_buy or ema_cross_up:
        return "buy", float(last["atr"]), info
    if two_of_three_sell or ema_cross_down:
        return "sell", float(last["atr"]), info
    return "none", float(last["atr"]), info

# ==================== STRATEGY HELPERS ====================
def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

def choose_multiplier(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.01:
        return 0.7
    elif pct < 0.02:
        return 1.0
    else:
        return 1.5

def dynamic_min_profit(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.004:
        return 0.6
    elif pct < 0.008:
        return 0.8
    else:
        return 1.2

# ==================== TRADES & LOGS ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    try:
        with open("trades.csv", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    except Exception as e:
        logging.warning(f"CSV write failed: {e}")
    save_state()

# ==================== RESTORE ====================
def reconcile_positions_on_start():
    usdt, by = get_balances_cache()
    limits, limits_ok, _ = get_limits()
    total_notional = 0.0
    lines = []

    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = float(df["c"].iloc[-1])
        atr   = float(AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1])
        st = STATE[sym]

        step = limits.get(sym, {}).get("qty_step", 1.0) if limits_ok else 1.0
        min_amt = limits.get(sym, {}).get("min_amt", 10.0) if limits_ok else 10.0

        bal = adjust_qty(get_coin_balance_from(by, sym), step)

        if bal > 0 and bal * price >= min_amt and not st["positions"]:
            mul = choose_multiplier(atr, price)
            tp  = price + mul * atr
            st["positions"] = [{
                "buy_price": price,
                "qty": bal,
                "tp": tp,
                "timestamp": datetime.datetime.now().isoformat(),
                "synthetic": True,
                "born": time.time()
            }]
            lines.append(f"- {sym}: синтетическая позиция qty={bal} по {price:.6f}")
        else:
            lines.append(f"- {sym}: позиций={len(st['positions'])}, баланс={bal}")

        total_notional += bal * price

    save_state()
    log_msg("🚀 Бот запущен (восстановление)\n" + "\n".join(lines) + f"\n📊 Номинал: ${total_notional:.2f}", True)

# ==================== LIMIT ORDER ENGINE ====================
def place_postonly_limit(sym, side, qty, ref_price):
    """
    Ставит PostOnly лимит около лучшего bid/ask с лагом в bps.
    Возвращает (orderId, price) или (None, None).
    """
    limits, ok, _ = get_limits()
    tick = limits.get(sym, {}).get("tick_size", 0.0001) if ok else 0.0001
    qstep = limits.get(sym, {}).get("qty_step", 1.0) if ok else 1.0

    # округлим qty на всякий случай
    qty = adjust_qty(qty, qstep)
    if qty <= 0:
        logging.info(f"[{sym}] place_postonly_limit: qty rounded to 0 (step={qstep})")
        return None, None

    best_bid, best_ask = get_orderbook(sym)
    if best_bid == 0.0 or best_ask == 0.0:
        logging.info(f"[{sym}] DEBUG_SKIP | orderbook empty on {side.upper()}")
        return None, None

    if side == "Buy":
        raw_price = best_bid * (1.0 - MAKER_LAG_BPS / 10000.0)
    else:
        raw_price = best_ask * (1.0 + MAKER_LAG_BPS / 10000.0)

    price = round_price(raw_price, tick)

    try:
        r = api_call(
            session.place_order,
            category="spot",
            symbol=sym,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(price),
            timeInForce="PostOnly" if POST_ONLY else "GTC"
        )
        oid = r["result"]["orderId"]
        logging.info(f"[{sym}] placed {side} PO limit qty={qty} price={price:.6f} oid={oid}")
        return oid, price
    except Exception as e:
        logging.warning(f"[{sym}] place_postonly_limit failed: {e}")
        return None, None

def cancel_order(sym, oid):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=oid)
        logging.info(f"[{sym}] cancel order {oid}")
    except Exception as e:
        logging.warning(f"[{sym}] cancel failed {oid}: {e}")

def query_order(sym, oid):
    """Возвращает (status, cumExecQty) или ('', 0)."""
    try:
        r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=oid)
        lst = r.get("result", {}).get("list", [])
        if not lst:
            h = api_call(session.get_order_history, category="spot", symbol=sym, orderId=oid)
            lsth = h.get("result", {}).get("list", [])
            if not lsth:
                return "", 0.0
            it = lsth[0]
        else:
            it = lst[0]
        status = it.get("orderStatus", "")
        filled = float(it.get("cumExecQty", 0.0))
        return status, filled
    except Exception as e:
        logging.warning(f"[{sym}] query_order failed {oid}: {e}")
        return "", 0.0

def wait_fill_or_reprice(sym, side, qty, ref_price):
    """
    Цикл: выставить лимит → ждать → при необходимости отменить/репрайсить.
    Возвращает (filled_qty, avg_price).
    """
    limits, ok, _ = get_limits()
    qstep = limits.get(sym, {}).get("qty_step", 1.0) if ok else 1.0

    filled_total = 0.0
    avg_price = 0.0
    remain = adjust_qty(qty, qstep)
    attempt = 0

    while remain > 0 and attempt <= MAX_REPRICES:
        attempt += 1
        oid, px = place_postonly_limit(sym, side, remain, ref_price)
        if not oid:
            logging.info(f"[{sym}] DEBUG_SKIP | place failed on {side}")
            return filled_total, avg_price

        t0 = time.time()
        while time.time() - t0 < MAX_WAIT_SEC:
            status, filled = query_order(sym, oid)
            if status == "":
                time.sleep(POLL_SEC)
                continue

            if filled > 0:
                part = adjust_qty(filled, qstep)
                if part > filled_total:  # учитываем прирост
                    inc = part - filled_total
                    new_total = filled_total + inc
                    avg_price = ((avg_price * filled_total) + (px * inc)) / new_total if new_total > 0 else avg_price
                    filled_total = new_total
                    remain = max(adjust_qty(qty - filled_total, qstep), 0.0)

            if status in ("Filled", "Cancelled", "Rejected"):
                break

            time.sleep(POLL_SEC)

        status, filled = query_order(sym, oid)
        if status not in ("Filled",):
            cancel_order(sym, oid)
        if remain <= 0.0:
            break

    return filled_total, avg_price

# ==================== MAIN LOGIC ====================
def compute_estimated_pnl_buy_then_tp(price, qty, tp_price):
    """Покупка maker, продажа maker по TP."""
    buy_cost  = price * qty
    sell_rev  = tp_price * qty
    fees = buy_cost * MAKER_FEE + sell_rev * MAKER_FEE
    return sell_rev - buy_cost - fees

def compute_estimated_pnl_stop(price_buy, qty, price_now):
    """Если сработает стоп и он продастся маркетом (taker)."""
    cost   = price_buy * qty
    rev    = price_now * qty
    fees   = cost * MAKER_FEE + rev * TAKER_FEE  # buy maker, sell taker
    return rev - cost - fees

def trade():
    global cycle_count, LAST_REPORT_DATE
    limits, limits_ok, buy_blocked_reason = get_limits()

    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        st["sell_failed"] = False

        df = get_kline(sym)
        if df.empty:
            continue

        sig, atr, ind = signal(df)
        price = float(df["c"].iloc[-1])
        coin_bal = get_coin_balance_from(by, sym)
        value = coin_bal * price
        best_bid, best_ask = get_orderbook(sym)

        logging.info(
            f"[{sym}] sig={sig}, price={price:.6f}, bal_val={value:.2f}, "
            f"pos={len(st['positions'])}, bid={best_bid:.6f}, ask={best_ask:.6f} | "
            f"EMA9={ind.get('EMA9',0):.6f} EMA21={ind.get('EMA21',0):.6f} RSI={ind.get('RSI',0):.2f} "
            f"MACD={ind.get('MACD',0):.6f} SIG={ind.get('SIG',0):.6f}"
        )

        # -------- SELL / STOP-LOSS / TP --------
        for pos in list(st["positions"]):
            b, q, tp_price = pos["buy_price"], pos["qty"], pos["tp"]
            is_synth = bool(pos.get("synthetic"))
            age_h = (time.time() - float(pos.get("born", time.time()))) / 3600.0
            pnl_now = compute_estimated_pnl_stop(b, q, price)

            logging.info(f"[{sym}] SL check | synth={is_synth} age_h={age_h:.2f} price_now={price:.6f} "
                         f"b={b:.6f} q={q} pnl_now={pnl_now:.2f}")

            # --- STOP LOSS (с защитой синтетики и ограничением убытка) ---
            can_stop = True
            if is_synth and (DISABLE_SL_FOR_SYNTH or age_h < SYNTHETIC_GRACE_HOURS):
                can_stop = False
                logging.info(f"[{sym}] Skip SL — synthetic position (grace/disabled)")

            hit_sl  = price <= b * (1 - STOP_LOSS_PCT)
            loss_ok = (MAX_LOSS_USDT <= 0) or (abs(pnl_now) <= MAX_LOSS_USDT)

            if q > 0 and can_stop and hit_sl and abs(pnl_now) >= MIN_NET_PROFIT and loss_ok:
                # маркет-выход (taker) с округлением qty
                qstep = limits.get(sym, {}).get("qty_step", 1.0) if limits_ok else 1.0
                q_sell = adjust_qty(q, qstep)
                if coin_bal >= q_sell and q_sell > 0:
                    try:
                        r = api_call(
                            session.place_order,
                            category="spot",
                            symbol=sym,
                            side="Sell",
                            orderType="Market",
                            qty=str(q_sell)
                        )
                        log_trade(sym, "STOP SELL", price, q_sell, pnl_now, "taker stop-loss")
                        st["pnl"] += pnl_now
                    except Exception as e:
                        log_msg(f"{sym}: STOP SELL MARKET failed: {e}", True)
                        st["sell_failed"] = True
                else:
                    log_msg(f"{sym}: STOP SELL MARKET failed: qty rounded to 0 or insufficient balance", True)
                    st["sell_failed"] = True

                st["last_stop_time"] = datetime.datetime.now().isoformat()
                st["avg_count"] = 0
                st["positions"].clear()
                break

            # --- TAKE PROFIT (лимитный maker) ---
            pnl_tp_est = compute_estimated_pnl_buy_then_tp(b, q, tp_price)
            if q > 0 and price >= tp_price and pnl_tp_est >= MIN_NET_PROFIT:
                filled, avg_px = wait_fill_or_reprice(sym, "Sell", q, price)
                if filled > 0:
                    pnl_real_est = compute_estimated_pnl_buy_then_tp(b, filled, avg_px)
                    log_trade(sym, "TP SELL", avg_px, filled, pnl_real_est, "maker TP")
                    st["pnl"] += pnl_real_est

                    if filled < q:
                        # частичное закрытие — переносим остаток и сбрасываем флаги синтетики
                        remain_qty = q - filled
                        pos["qty"] = remain_qty
                        pos["buy_price"] = b
                        pos["synthetic"] = False
                        pos["born"] = time.time()
                    else:
                        st["positions"].clear()

                    st["last_sell_price"] = avg_px
                else:
                    logging.info(f"[{sym}] DEBUG | TP not filled; keep position")
                break

        if st.get("sell_failed"):
            st["positions"] = []

        # -------- BUY (лимитный maker) --------
        if sig == "buy" and not st["positions"]:
            last_stop = st.get("last_stop_time", "")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs < 4:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — прошло лишь {hrs:.1f}ч после стоп‑лосса (мин 4ч)")
                continue

            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                    send_tg(f"{sym}: ⛔️ Покупки временно заблокированы: {buy_blocked_reason}")
                continue

            if avail < (limits[sym]["min_amt"] if sym in limits else 10.0):
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")
                continue

            qty = get_qty(sym, price, avail)
            if qty <= 0:
                if should_log_skip(sym, "skip_qty"):
                    limit = limits.get(sym, {"qty_step": "?", "min_qty": "?", "min_amt": "?"})
                    log_skip(sym, f"Пропуск BUY — qty=0 (price={price:.6f}, step={limit['qty_step']}, "
                                   f"min_qty={limit['min_qty']}, min_amt={limit['min_amt']})")
                continue

            mul = choose_multiplier(atr, price)
            tp_price = price + mul * atr

            est_pnl = compute_estimated_pnl_buy_then_tp(price, qty, tp_price)
            required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))

            logging.info(f"[{sym}] BUY‑check qty={qty}, mul={mul:.2f}, tp={tp_price:.6f}, "
                         f"est_pnl={est_pnl:.4f}, required={required_pnl:.2f}")

            if est_pnl < required_pnl:
                if should_log_skip(sym, "skip_low_profit"):
                    log_skip(sym, f"Пропуск BUY — ожидаемый PnL {est_pnl:.2f} < требуемого {required_pnl:.2f}")
                continue

            # Ставим лимитный BUY и ждём исполнения с репрайсами
            filled, avg_px = wait_fill_or_reprice(sym, "Buy", qty, price)
            if filled <= 0:
                logging.info(f"[{sym}] DEBUG | BUY not filled after reprices")
                continue

            STATE[sym]["positions"].append({
                "buy_price": avg_px, "qty": filled, "tp": tp_price,
                "timestamp": datetime.datetime.now().isoformat(),
                "synthetic": False,
                "born": time.time()
            })
            cost = avg_px * filled
            avail_local = max(0.0, avail - cost)
            log_trade(sym, "BUY", avg_px, filled, 0.0, "maker entry")
            avail = avail_local  # уменьшаем доступный кэш

    cycle_count += 1

    # Ежедневный отчёт
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== DAILY REPORT ====================
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total_pnl = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}"
                             + (" (synthetic)" if p.get("synthetic") else ""))
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    reconcile_positions_on_start()
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
