# -*- coding: utf-8 -*-
# MTF_v2.1_MAKER_LMT_Requote_AfterFees
# Требование: чистая прибыль >= $1 после всех комиссий (bybit spot: maker 0.10%, taker 0.18%)

import os, time, math, logging, datetime, requests, json
from decimal import Decimal, getcontext
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

# Требуемая минимальная чистая прибыль за всю сделку (BUY+SELL) после комиссий
MIN_NET_PROFIT_USD = 1.00

# Стоп-лосс (процент от buy_price)
STOP_LOSS_PCT     = 0.008       # 0.8%

# Комиссии (текущие твои ставки на Bybit spot)
MAKER_BUY_FEE  = 0.0010    # 0.10%
MAKER_SELL_FEE = 0.0010    # 0.10%
TAKER_BUY_FEE  = 0.0010    # не используем, но оставим
TAKER_SELL_FEE = 0.0018

# Мейкер‑логика
MAKER_OFFSET_BP = 3                 # отступ от лучшей цены в б.п. (1 bp = 0.01%) → 3 bps = 0.03%
REQUOTE_SEC     = 30                # раз в столько секунд перевыставлять лимит
ORDER_TTL_SEC   = 60 * 10           # safety TTL: отменить, если ордер висит слишком долго

# Защита от повторных покупок после стоп‑лосса
NO_REBUY_AFTER_SL_HRS = 4

# Сигналы и интервалы
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
INTERVALS = ["1", "3", "5", "15", "30", "60"]  # минуты в Bybit get_kline
MIN_BARS = 50

# Логи
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"),
              logging.StreamHandler()]
)

# Расширенный лог причин пропуска сделок
VERBOSE_SKIP = True

LAST_REPORT_DATE = None
cycle_count = 0

# Точные десятичные
getcontext().prec = 28

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

STATE = {}  # по символу: позиции/пнл/последний стоп и т.п.

# pending лимитные ордера (в памяти и в Redis, чтобы переживать рестарты)
PENDING_KEY = "pending_orders_v1"

# Лимиты (минимальные qty/amount/шаг) — ленивый кэш в Redis
LIMITS_REDIS_KEY = "limits_cache_v1"
LIMITS_TTL_SEC   = 12 * 60 * 60
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

SKIP_LOG_TIMESTAMPS = {}

# ==================== UTIL ====================
def send_tg(msg: str):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def dbg(sym: str, text: str):
    if VERBOSE_SKIP:
        logging.info(f"[{sym}] DEBUG_SKIP | {text}")

def should_log_skip(sym, key, interval=15):
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

def load_pending():
    raw = redis_client.get(PENDING_KEY)
    return json.loads(raw) if raw else {}

def save_pending(p):
    try:
        redis_client.set(PENDING_KEY, json.dumps(p))
    except Exception as e:
        logging.warning(f"save_pending failed: {e}")

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp}]
            "pnl": 0.0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    ensure_state_consistency()
    log_msg("✅ Состояние загружено" if raw else "ℹ Начинаем с чистого состояния", True)

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

# ==================== LIMITS ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    return {
        item["symbol"]: {
            "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0)),
            "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(item.get("priceFilter", {}).get("tickSize", 0.0000001)),
        }
        for item in lst if item["symbol"] in SYMBOLS
    }

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

# ==================== MARKET DATA & BALANCES ====================
def get_kline(sym, interval="1", limit=100):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

def get_orderbook(sym):
    try:
        r = api_call(session.get_order_book, category="spot", symbol=sym, limit=1)
        ob = r["result"]["a"], r["result"]["b"]  # asks, bids
        best_ask = float(ob[0][0]["price"]) if ob and ob[0] else None
        best_bid = float(ob[1][0]["price"]) if ob and ob[1] else None
        return best_bid, best_ask
    except Exception as e:
        logging.warning(f"orderbook failed {sym}: {e}")
        return None, None

# ==================== QTY / ROUNDING ====================
def adjust_qty(qty, step):
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def round_price(price, tick):
    if tick <= 0: 
        return price
    p = Decimal(str(price)); t = Decimal(str(tick))
    return float((p // t) * t)

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
def compute_signal_on_df(df: pd.DataFrame):
    if df.empty or len(df) < MIN_BARS:
        return "none", 0.0, {"ema9":0,"ema21":0,"rsi":0,"macd":0,"sig":0}

    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    atr14 = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd  = MACD(close=df["c"])
    m, s  = macd.macd(), macd.macd_signal()

    df = df.copy()
    df["ema9"], df["ema21"], df["rsi"], df["atr"], df["macd"], df["sig"] = ema9, ema21, rsi9, atr14, m, s

    last = df.iloc[-1]; prev = df.iloc[-2]

    two_of_three_buy = ((last["ema9"] > last["ema21"]) +
                        (last["rsi"] > 48) +
                        (last["macd"] > last["sig"])) >= 2

    ema_cross_up = (prev["ema9"] <= prev["ema21"]) and (last["ema9"] > last["ema21"]) and (last["rsi"] > 45)

    two_of_three_sell = ((last["ema9"] < last["ema21"]) +
                         (last["rsi"] < 50) +
                         (last["macd"] < last["sig"])) >= 2
    ema_cross_down = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"]) and (last["rsi"] < 55)

    info = {"ema9": float(last["ema9"]), "ema21": float(last["ema21"]),
            "rsi": float(last["rsi"]), "macd": float(last["macd"]), "sig": float(last["sig"]),
            "xup": int(ema_cross_up), "xdn": int(ema_cross_down),
            "atr": float(last["atr"])}

    if two_of_three_buy or ema_cross_up:
        return "buy", float(last["atr"]), info
    if two_of_three_sell or ema_cross_down:
        return "sell", float(last["atr"]), info
    return "none", float(last["atr"]), info

def signal_mtf(sym):
    # базовый фрейм 1m
    d1 = get_kline(sym, "1", 200)
    sig1, atr1, inf1 = compute_signal_on_df(d1)

    # старшие фреймы для подтверждения
    votes_buy, votes_sell = 0, 0
    atr_ref = atr1
    f_info = {}

    for itv in ["3","5","15","30","60"]:
        df = get_kline(sym, itv, 200)
        sig, atr, inf = compute_signal_on_df(df)
        f_info[itv] = sig
        atr_ref = max(atr_ref, atr)  # берём больший ATR
        if sig == "buy": votes_buy += 1
        if sig == "sell": votes_sell += 1

    # условие входа: на 1m есть buy И (любой из 3м/5м/15м/30м/60м = buy)
    buy_ok = (sig1 == "buy") and (votes_buy >= 1)
    sell_hint = (sig1 == "sell") or (votes_sell >= 2)

    final_sig = "buy" if buy_ok else ("sell" if sell_hint else "none")

    return final_sig, atr_ref, f_info, inf1

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

def required_profit_floor(atr, price):
    # динамический пол; но не ниже $1
    pct = atr / price if price > 0 else 0
    base = 0.6 if pct < 0.004 else (0.8 if pct < 0.008 else 1.2)
    return max(MIN_NET_PROFIT_USD, base)

# ==================== ORDERS (maker) ====================
def bp_to_price(base_price, bps, side, tick):
    # bps => 0.01% * bps
    shift = base_price * (bps / 10000.0)
    if side == "Buy":
        p = base_price - shift
    else:
        p = base_price + shift
    return max(0.0, round_price(p, tick))

def place_postonly_limit(sym, side, qty, price):
    return api_call(
        session.place_order,
        category="spot", symbol=sym, side=side,
        orderType="Limit", qty=str(qty), price=str(price),
        timeInForce="PostOnly", reduceOnly=False
    )

def cancel_order(sym, orderId):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=orderId)
    except Exception as e:
        logging.warning(f"cancel_order failed {sym} {orderId}: {e}")

def fetch_order_status(sym, orderId):
    # сначала открытые
    try:
        openr = api_call(session.get_open_orders, category="spot", symbol=sym)
        lst = openr["result"]["list"] or []
        for o in lst:
            if o.get("orderId") == orderId:
                return {"status":"Open", "avgPrice": float(o.get("avgPrice", 0) or 0), "cumExecQty": float(o.get("cumExecQty", 0) or 0)}
    except Exception as e:
        logging.warning(f"get_open_orders failed {sym}: {e}")

    # затем история
    try:
        hist = api_call(session.get_order_history, category="spot", symbol=sym)
        lst = hist["result"]["list"] or []
        for o in lst:
            if o.get("orderId") == orderId:
                st = o.get("orderStatus")
                ap = float(o.get("avgPrice", 0) or 0)
                qty = float(o.get("cumExecQty", 0) or 0)
                return {"status":st, "avgPrice":ap, "cumExecQty":qty}
    except Exception as e:
        logging.warning(f"get_order_history failed {sym}: {e}")

    return {"status":"Unknown", "avgPrice":0.0, "cumExecQty":0.0}

# ==================== TRADES & LOGS ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE / INIT ====================
def reconcile_positions_on_start():
    # просто объявим позиции согласно фактическому остатку (если есть)
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()

    total = 0.0
    lines = []

    for sym in SYMBOLS:
        df = get_kline(sym, "1", 120)
        if df.empty: 
            continue
        price = df["c"].iloc[-1]
        atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
        step = (limits.get(sym) or {}).get("qty_step", 1.0)
        min_amt = (limits.get(sym) or {}).get("min_amt", 10.0)

        bal = adjust_qty(get_coin_balance_from(by, sym), step)
        if bal >= step and bal * price >= min_amt:
            mul = choose_multiplier(atr, price)
            tp  = price + mul * atr
            STATE[sym]["positions"] = [{
                "buy_price": price, "qty": bal, "tp": tp,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            lines.append(f"- {sym}: синтетическая позиция qty={bal} @ {price:.6f}")
            total += bal * price
        else:
            lines.append(f"- {sym}: позиций нет")

    save_state()
    log_msg("🚀 Бот запущен (восстановление)\n" + "\n".join(lines) + f"\n📊 Номинал: ${total:.2f}", True)

# ==================== BUY/SELL DECISION ====================
def est_pnl_from_to(qty, buy_price, sell_price, maker=True):
    if maker:
        buy_fee  = buy_price  * qty * MAKER_BUY_FEE
        sell_fee = sell_price * qty * MAKER_SELL_FEE
    else:
        buy_fee  = buy_price  * qty * TAKER_BUY_FEE
        sell_fee = sell_price * qty * TAKER_SELL_FEE
    return (sell_price - buy_price) * qty - (buy_fee + sell_fee), buy_fee, sell_fee

# ==================== DAILY REPORT ====================
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total_pnl = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}")
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== MAIN LOOP ====================
def trade():
    global cycle_count, LAST_REPORT_DATE

    limits, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    # pending ордера из Redis
    pending = load_pending()

    for sym in SYMBOLS:
        st = STATE[sym]

        # -- проверка и обслуживание pending ордеров --
        pend = pending.get(sym)
        if pend:
            orderId = pend.get("orderId")
            side    = pend.get("side")
            ts      = pend.get("ts", 0)
            qty     = float(pend.get("qty", 0))
            status  = fetch_order_status(sym, orderId)
            nowts   = time.time()

            if status["status"] in ("Filled", "PartiallyFilled"):
                avg = status.get("avgPrice", 0.0) or pend.get("price")
                if side == "Buy":
                    # Создаём позицию с новым TP
                    df = get_kline(sym, "1", 120)
                    price = df["c"].iloc[-1] if not df.empty else avg
                    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1] if not df.empty else max(1e-8, 0.001*price)
                    mul = choose_multiplier(atr, price)
                    tp  = avg + mul * atr
                    st["positions"] = [{"buy_price": avg, "qty": qty, "tp": tp, "timestamp": datetime.datetime.now().isoformat()}]
                    log_trade(sym, "BUY FILLED", avg, qty, 0.0, f"orderId={orderId}")
                else:
                    # Продажа закрыла позицию → считаем pnl
                    # Для простоты возьмём первую позицию
                    if st["positions"]:
                        b = st["positions"][0]["buy_price"]
                        pnl, bf, sf = est_pnl_from_to(qty, b, avg, maker=True)
                        st["pnl"] += pnl
                        log_trade(sym, "SELL FILLED", avg, qty, pnl, f"orderId={orderId}")
                        st["positions"].clear()
                pending.pop(sym, None)
                save_pending(pending)

            elif status["status"] in ("Cancelled", "Rejected"):
                dbg(sym, f"pending {side} {orderId} -> {status['status']}")
                pending.pop(sym, None)
                save_pending(pending)

            else:
                # открыт — возможно, пора перевыставить
                if nowts - ts >= REQUOTE_SEC or nowts - pend.get("create_ts", ts) >= ORDER_TTL_SEC:
                    # отменим и перевыставим по новой лучшей цене
                    cancel_order(sym, orderId)
                    best_bid, best_ask = get_orderbook(sym)
                    if not best_bid or not best_ask:
                        dbg(sym, f"orderbook empty at requote (side={side})")
                        continue
                    tick = limits.get(sym, {}).get("tick_size", 0.0000001)
                    if side == "Buy":
                        new_price = bp_to_price(best_bid, MAKER_OFFSET_BP, "Buy", tick)
                    else:
                        new_price = bp_to_price(best_ask, MAKER_OFFSET_BP, "Sell", tick)
                    try:
                        r = place_postonly_limit(sym, side, qty, new_price)
                        new_id = r["result"]["orderId"]
                        pending[sym] = {"orderId": new_id, "side": side, "qty": qty,
                                        "price": new_price, "ts": time.time(), "create_ts": time.time()}
                        save_pending(pending)
                        logging.info(f"[{sym}] REQUOTE {side} -> {new_price} (orderId={new_id})")
                    except Exception as e:
                        logging.warning(f"[{sym}] requote failed: {e}")

                # пропускаем дальнейшую логику по этому символу, пока висит ордер
                continue

        # --- сигналы ---
        sig, atr, mtf_info, inf1 = signal_mtf(sym)

        # текущая цена
        df1 = get_kline(sym, "1", 5)
        if df1.empty:
            continue
        price = df1["c"].iloc[-1]

        # балансы
        coin_bal = get_coin_balance_from(by, sym)
        value = coin_bal * price

        mtf_str = " | ".join([f"{k}m:{v}" for k,v in mtf_info.items()])
        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, bal_val={value:.2f}, pos={len(st['positions'])}, {mtf_str}")

        # --- TP / SL продажи лимитом ---
        limits, limits_ok, _ = get_limits()
        tick = limits.get(sym, {}).get("tick_size", 0.0000001)

        for pos in list(st["positions"]):
            b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]

            # SL проверяем по рынку (но продаём maker‑лимитом выше bid, если условие выполняется и прибыль >= 1$)
            if price <= b * (1 - STOP_LOSS_PCT):
                # посчитаем, есть ли шанс сделать >= $1, если выставим sell лимитом на best_ask+offset
                best_bid, best_ask = get_orderbook(sym)
                if not best_bid or not best_ask:
                    dbg(sym, "orderbook empty on SL")
                    continue
                sell_lmt = bp_to_price(best_ask, MAKER_OFFSET_BP, "Sell", tick)
                pnl, bf, sf = est_pnl_from_to(q, b, sell_lmt, maker=True)
                if abs(pnl) >= MIN_NET_PROFIT_USD:
                    try:
                        r = place_postonly_limit(sym, "Sell", q, sell_lmt)
                        oid = r["result"]["orderId"]
                        pending[sym] = {"orderId": oid, "side":"Sell", "qty": q,
                                        "price": sell_lmt, "ts": time.time(), "create_ts": time.time()}
                        save_pending(pending)
                        logging.info(f"[{sym}] SL SELL placed postOnly {sell_lmt} (pnl≈{pnl:.2f})")
                    except Exception as e:
                        log_msg(f"{sym}: SL SELL failed: {e}", True)
                else:
                    dbg(sym, f"SL condition but pnl<{MIN_NET_PROFIT_USD:.2f} at lmt={sell_lmt:.6f}")
                continue  # одну позицию обрабатываем за цикл

            # TP: если цена >= tp — ставим maker sell лимит на tp (или чуть выше)
            if price >= tp:
                best_bid, best_ask = get_orderbook(sym)
                if not best_bid or not best_ask:
                    dbg(sym, "orderbook empty on TP")
                    continue
                sell_lmt = max(tp, bp_to_price(best_ask, MAKER_OFFSET_BP, "Sell", tick))
                pnl, bf, sf = est_pnl_from_to(q, b, sell_lmt, maker=True)
                if pnl >= MIN_NET_PROFIT_USD:
                    try:
                        r = place_postonly_limit(sym, "Sell", q, sell_lmt)
                        oid = r["result"]["orderId"]
                        pending[sym] = {"orderId": oid, "side":"Sell", "qty": q,
                                        "price": sell_lmt, "ts": time.time(), "create_ts": time.time()}
                        save_pending(pending)
                        logging.info(f"[{sym}] TP SELL placed postOnly {sell_lmt} (pnl≈{pnl:.2f})")
                    except Exception as e:
                        log_msg(f"{sym}: TP SELL failed: {e}", True)
                else:
                    dbg(sym, f"TP reached but est pnl={pnl:.2f} < {MIN_NET_PROFIT_USD:.2f} (sell_lmt={sell_lmt:.6f})")
                continue

        # --- BUY вход (если позиций нет и нет pending) ---
        if not st["positions"] and sig == "buy":
            last_stop = st.get("last_stop_time", "")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs < NO_REBUY_AFTER_SL_HRS:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — прошло лишь {hrs:.1f}ч после SL (мин {NO_REBUY_AFTER_SL_HRS}ч)")
                    dbg(sym, f"cooldown {hrs:.2f}h")
                continue

            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                    dbg(sym, f"limits_ok={limits_ok}")
                continue

            min_amt = limits.get(sym, {}).get("min_amt", 10.0)
            if avail < min_amt:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — мало USDT")
                    dbg(sym, f"avail={avail:.2f}, min_amt={min_amt}")
                continue

            qty = get_qty(sym, price, avail)
            if qty <= 0:
                if should_log_skip(sym, "skip_qty"):
                    l = limits.get(sym, {})
                    log_skip(sym, "Пропуск BUY — qty=0")
                    dbg(sym, f"price={price:.6f}, step={l.get('qty_step')}, min_qty={l.get('min_qty')}, min_amt={l.get('min_amt')}, avail={avail:.2f}")
                continue

            # цель TP от ATR
            mul = choose_multiplier(atr, price)
            tp_target = price + mul * atr

            # оценка прибыли при maker‑входе по лучшему bid-отступу и maker‑выходе по tp_target (или выше)
            best_bid, best_ask = get_orderbook(sym)
            if not best_bid or not best_ask:
                dbg(sym, "orderbook empty on BUY")
                continue

            tick = limits.get(sym, {}).get("tick_size", 0.0000001)
            lmt_buy  = bp_to_price(best_bid, MAKER_OFFSET_BP, "Buy", tick)
            lmt_sell = max(tp_target, bp_to_price(best_ask, MAKER_OFFSET_BP, "Sell", tick))

            est_pnl, buy_fee, sell_fee = est_pnl_from_to(qty, lmt_buy, lmt_sell, maker=True)
            req_profit = required_profit_floor(atr, price)

            dbg(sym, (
                f"calc qty={qty}, atr={atr:.8f}, mul={mul:.3f}, "
                f"tp_target={tp_target:.6f}, lmt_buy={lmt_buy:.6f}, lmt_sell={lmt_sell:.6f}, "
                f"est_pnl={est_pnl:.4f}, req_profit={req_profit:.2f}, "
                f"fees buy={buy_fee:.4f} sell={sell_fee:.4f}"
            ))

            if est_pnl >= req_profit:
                try:
                    r = place_postonly_limit(sym, "Buy", qty, lmt_buy)
                    oid = r["result"]["orderId"]
                    pending[sym] = {"orderId": oid, "side":"Buy", "qty": qty,
                                    "price": lmt_buy, "ts": time.time(), "create_ts": time.time()}
                    save_pending(pending)
                    logging.info(f"[{sym}] PLACED BUY postOnly {lmt_buy} qty={qty} (req≥{req_profit:.2f}, est≈{est_pnl:.2f})")
                except Exception as e:
                    log_msg(f"{sym}: BUY failed: {e}", True)
            else:
                if should_log_skip(sym, "skip_low_profit"):
                    log_skip(sym, f"Пропуск BUY — ожидаемый PnL мал")
                    dbg(sym, f"REJECT est_pnl={est_pnl:.4f} < req_profit={req_profit:.2f}")

    cycle_count += 1

    # ежедневный отчёт
    now = datetime.datetime.now()
    global LAST_REPORT_DATE
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

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
