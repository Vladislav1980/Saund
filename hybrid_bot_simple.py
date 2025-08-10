# -*- coding: utf-8 -*-
"""
Bybit Spot maker-bot
- Maker лимитки (timeInForce=PostOnly) + "перекат" каждые 45s
- Комиссии: maker 0.10% (покупка и продажа), taker 0.18% (на SL и форс‑выход)
- Строгая проверка чистой прибыли >= $1 ДО размещения заявки
- TP по ATR-мультипликатору, цены/кол-ва строго по tickSize/qtyStep
- Телеграм-уведомления (старт, сделки, ошибки, ежедневный отчёт)
- Подробный DEBUG о причинах пропуска сделок
"""

import os, time, math, logging, datetime, json, requests, random
from decimal import Decimal, getcontext, ROUND_DOWN
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import pandas as pd
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

# Рабочие символы — по умолчанию 2 более «живые»
SYMBOLS = ["DOGEUSDT", "XRPUSDT"]  # можно добавить TONUSDT, WIFUSDT и т.п.

# денежные ограничения
RESERVE_BALANCE   = 1.0                    # держим на кошельке "подушку"
MAX_TRADE_USDT    = 120.0                  # максимум на одну покупку
MIN_NET_PROFIT    = 1.0                    # чистыми >= $1 после комиссий

# стоп-лосс (только аварийный выход)
STOP_LOSS_PCT     = 0.010                  # 1% (осторожный, т.к. мы Maker)

# комиссии спот
MAKER_BUY_FEE   = 0.0010                   # 0.10%
MAKER_SELL_FEE  = 0.0010                   # 0.10%
TAKER_SELL_FEE  = 0.0018                   # 0.18% (на SL/форс)

# лимитник‑менеджер
REPRICE_SEC_MIN = 30                       # перекат: окно
REPRICE_SEC_MAX = 60
DEFAULT_REPRICE = 45

# отчёты
LAST_REPORT_DATE = None

# кэш лимитов в Redis на 12 часов
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

# точность Decimal
getcontext().prec = 28

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}

# открытые лимитники под перекат
# STATE["open_orders"][sym] = { "orderId", "side", "qty", "placed_ts", "plan_tp", "tp_mult", "reprice_at" }
# позиции:
# STATE[sym]["positions"] = [{buy_price, qty, tp, timestamp}]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

# ==================== TG ====================
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

# ==================== SMALL UTILS ====================
def now_iso():
    return datetime.datetime.now().isoformat()

def d(x) -> Decimal:
    return Decimal(str(x))

def floor_step(val: float, step: float) -> float:
    if step <= 0:
        return float(val)
    q = (d(val) / d(step)).to_integral_value(rounding=ROUND_DOWN)
    return float(q * d(step))

def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

# ==================== API WRAPPER ====================
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

# ==================== LIMITS/TICK ====================
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

def _limits_from_redis():
    raw = redis_client.get(LIMITS_REDIS_KEY)
    if not raw: return None
    try: return json.loads(raw)
    except: return None

def _limits_to_redis(limits: dict):
    try:
        redis_client.setex(LIMITS_REDIS_KEY, LIMITS_TTL_SEC, json.dumps(limits))
    except Exception as e:
        logging.warning(f"limits cache save failed: {e}")

def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    out = {}
    for item in lst:
        sym = item["symbol"]
        if sym not in SYMBOLS: continue
        lot = item.get("lotSizeFilter", {})
        pf  = item.get("priceFilter", {})
        out[sym] = {
            "min_qty": float(lot.get("minOrderQty", 0.0)),
            "qty_step": float(lot.get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(pf.get("tickSize", 0.00000001))
        }
    return out

def get_limits():
    global _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    if _LIMITS_MEM is not None:
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    cached = _limits_from_redis()
    if cached:
        _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON = cached, True, ""
        logging.info("LIMITS loaded from Redis cache")
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    try:
        limits = _load_symbol_limits_from_api()
        _limits_to_redis(limits)
        _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON = limits, True, ""
        logging.info("LIMITS loaded from API and cached")
    except Exception as e:
        _LIMITS_MEM, _LIMITS_OK = {}, False
        _BUY_BLOCKED_REASON = f"LIMITS unavailable ({e}); BUY blocked, SELL allowed"
        log_msg(f"⚠️ {_BUY_BLOCKED_REASON}", True)

    return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

# ==================== BALANCES / MARKET DATA ====================
def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_bid_ask(sym):
    # Bybit unified_trading: get_orderbook(category="spot", symbol=sym, limit=1)
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        a = float(r["result"]["a"][0]["p"])
        b = float(r["result"]["b"][0]["p"])
        return b, a
    except Exception as e:
        logging.error(f"orderbook failed {sym}: {e}")
        return None, None

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== STATE ====================
def ensure_state_consistency():
    STATE.setdefault("open_orders", {})
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp}]
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

def save_state():
    try:
        redis_client.set("bot_state_v2", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state_v2")
    STATE = json.loads(raw) if raw else {}
    ensure_state_consistency()
    if raw:
        log_msg("✅ Состояние загружено из Redis", True)
    else:
        log_msg("ℹ Начинаем с чистого состояния", True)

# ==================== ROUNDING ====================
def round_price(sym, price):
    limits, ok, _ = get_limits()
    tick = limits.get(sym, {}).get("tick_size", 0.00000001) if ok else 0.00000001
    return floor_step(price, tick)

def round_qty(sym, qty):
    limits, ok, _ = get_limits()
    step = limits.get(sym, {}).get("qty_step", 1.0) if ok else 1.0
    return floor_step(qty, step)

def qty_from_usdt(sym, price, usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = round_qty(sym, alloc / price)
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== INDICATORS / SIGNALS (слегка ослаблены) ====================
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""

    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    atr14 = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd  = MACD(close=df["c"])
    macd_line, macd_sig = macd.macd(), macd.macd_signal()

    df = df.copy()
    df["ema9"] = ema9; df["ema21"] = ema21
    df["rsi"]  = rsi9; df["atr"]    = atr14
    df["macd"]= macd_line; df["macd_signal"] = macd_sig

    last = df.iloc[-1]; prev = df.iloc[-2]

    # вход ослаблен: 2 из 3 + RSI > 45
    two_of_three_buy = ((last["ema9"] > last["ema21"]) +
                        (last["rsi"] > 45) +
                        (last["macd"] > last["macd_signal"])) >= 2

    # мягкий кросс вверх EMA и RSI > 44
    ema_cross_up = (prev["ema9"] <= prev["ema21"]) and (last["ema9"] > last["ema21"]) and (last["rsi"] > 44)

    # SELL‑контекст (для логов)
    two_of_three_sell = ((last["ema9"] < last["ema21"]) +
                         (last["rsi"] < 55) +
                         (last["macd"] < last["macd_signal"])) >= 2
    ema_cross_down = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"]) and (last["rsi"] < 56)

    info = (f"EMA9={last['ema9']:.6f},EMA21={last['ema21']:.6f},RSI={last['rsi']:.2f},"
            f"MACD={last['macd']:.6f},SIG={last['macd_signal']:.6f}")

    sig = "buy" if (two_of_three_buy or ema_cross_up) else ("sell" if (two_of_three_sell or ema_cross_down) else "none")
    return sig, float(last["atr"]), info

def choose_tp_mult(atr, price):
    # Чем ниже вола — тем ниже TP‑множитель; немного усилен
    pct = (atr / price) if price > 0 else 0
    if pct < 0.004:   return 1.30
    if pct < 0.008:   return 1.40
    if pct < 0.015:   return 1.55
    return 1.75

# ==================== PNL / PROFIT CHECK ====================
def estimate_net_pnl_buy_maker_sell_maker(price_buy, price_sell, qty):
    # чистая прибыль с maker fees
    gross = (price_sell - price_buy) * qty
    fees  = price_buy * qty * MAKER_BUY_FEE + price_sell * qty * MAKER_SELL_FEE
    return gross - fees

def estimate_net_pnl_buy_maker_sell_taker(price_buy, price_sell, qty):
    gross = (price_sell - price_buy) * qty
    fees  = price_buy * qty * MAKER_BUY_FEE + price_sell * qty * TAKER_SELL_FEE
    return gross - fees

# ==================== TRADING HELPERS ====================
def plan_buy(sym, usdt_avail):
    df = get_kline(sym)
    if df.empty:
        return None

    sig, atr, info = signal(df)
    price_last = df["c"].iloc[-1]
    bid, ask = get_bid_ask(sym)
    if not bid or not ask:
        logging.info(f"{sym}: DEBUG_SKIP | orderbook empty")
        return None

    bid = round_price(sym, bid)
    ask = round_price(sym, ask)

    # для maker buy ставим по bid (или чуть ниже)
    price_buy = bid
    qty = qty_from_usdt(sym, price_buy, usdt_avail)

    # Расчёт TP
    tp_mult = choose_tp_mult(atr, price_last)
    tp_price = round_price(sym, price_buy + tp_mult * atr)

    # Проверка чистой прибыли >= $1 при maker->maker
    est_net = estimate_net_pnl_buy_maker_sell_maker(price_buy, tp_price, qty)

    debug = (f"[{sym}] sig={sig}, price={price_last:.6f}, bid={bid:.6f}, ask={ask:.6f} | "
             f"EMA/RSI/MACD in log above | ATR(5/15m)≈{atr/price_last*100:.2f}% | tp_mult={tp_mult:.2f}")

    logging.info(debug)

    if sig != "buy":
        logging.info(f"{sym}: DEBUG_SKIP | сигнала BUY нет")
        return None

    if qty <= 0:
        logging.info(f"{sym}: DEBUG_SKIP | qty_from_usdt==0 (avail too small or limits)")
        return None

    # жесткое правило прибыли
    if est_net < MIN_NET_PROFIT:
        logging.info(f"{sym}: BUY-check qty={qty}, tp={tp_price:.6f}, est_pnl={est_net:.2f}, required={MIN_NET_PROFIT:.2f}")
        logging.info(f"{sym}: Пропуск BUY — ожидаемый PnL {est_net:.2f} < {MIN_NET_PROFIT:.2f}")
        return None

    return {
        "price": price_buy,
        "qty": qty,
        "tp": tp_price,
        "tp_mult": tp_mult
    }

def place_postonly_buy(sym, price, qty):
    price = round_price(sym, price)
    qty   = round_qty(sym, qty)
    r = api_call(session.place_order,
                 category="spot", symbol=sym, side="Buy",
                 orderType="Limit", qty=str(qty), price=str(price),
                 timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    logging.info(f"{sym}: postOnly BUY placed id={oid} price={price} qty={qty}")
    return oid

def cancel_order(sym, order_id):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
    except Exception as e:
        logging.info(f"{sym}: cancel failed {order_id}: {e}")

def fetch_open_orders(sym):
    r = api_call(session.get_open_orders, category="spot", symbol=sym)
    return r["result"]["list"] or []

def fetch_order_detail(sym, order_id):
    r = api_call(session.get_order_history, category="spot", symbol=sym, orderId=order_id)
    lst = r["result"]["list"] or []
    return lst[0] if lst else None

def place_postonly_sell_tp(sym, price, qty):
    price = round_price(sym, price)
    qty   = round_qty(sym, qty)
    r = api_call(session.place_order,
                 category="spot", symbol=sym, side="Sell",
                 orderType="Limit", qty=str(qty), price=str(price),
                 timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    logging.info(f"{sym}: postOnly TP SELL placed id={oid} price={price} qty={qty}")
    return oid

def place_market_sell(sym, qty):
    qty = round_qty(sym, qty)
    r = api_call(session.place_order,
                 category="spot", symbol=sym, side="Sell",
                 orderType="Market", qty=str(qty))
    oid = r["result"]["orderId"]
    logging.info(f"{sym}: MARKET SELL placed id={oid} qty={qty}")
    return oid

# ==================== RESTORE / INIT ====================
def reconcile_positions_on_start():
    """
    Синхронизация по балансам + чистка "фантомов".
    """
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()
    total_notional = 0.0
    lines = []

    for sym in SYMBOLS:
        st = STATE[sym]
        st.setdefault("positions", [])
        st.setdefault("last_stop_time", "")

        df = get_kline(sym)
        if df.empty: 
            continue
        price = df["c"].iloc[-1]
        step = limits.get(sym, {}).get("qty_step", 1.0) if ok else 1.0
        min_amt = limits.get(sym, {}).get("min_amt", 10.0) if ok else 10.0

        bal = floor_step(get_coin_balance_from(by, sym), step)
        if bal * price < min_amt or bal < step:
            if st["positions"]:
                cnt = len(st["positions"])
                st["positions"] = []
                lines.append(f"- {sym}: очищено {cnt} позиций (баланс мал)")
        else:
            # создаём синтетическую позицию по текущей цене (без TP),
            # TP установим при первом цикле
            st["positions"] = [{
                "buy_price": price, "qty": bal, "tp": 0.0, "timestamp": now_iso()
            }]
            lines.append(f"- {sym}: синхр. позиция qty={bal} по ~{price:.6f}")
            total_notional += bal * price

    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" + "\n".join(lines) +
            f"\n📊 Номинал по монетам: ${total_notional:.2f}", True)

# ==================== LOG TRADES ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

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

# ==================== CORE LOOP ====================
def trade_cycle():
    # лимиты
    limits, limits_ok, buy_blocked_reason = get_limits()

    # балансы
    usdt, by = get_balances_cache()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0.0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    # проверка открытых ордеров (перекат + фиксация заливок)
    manage_open_orders(by)

    # основной проход по инструментам
    for sym in SYMBOLS:
        st = STATE[sym]
        st.setdefault("sell_failed", False)

        df = get_kline(sym)
        if df.empty: 
            continue

        sig, atr, sinfo = signal(df)
        price = df["c"].iloc[-1]
        bid, ask = get_bid_ask(sym)
        bid_s = f"{bid:.6f}" if bid else "?"
        ask_s = f"{ask:.6f}" if ask else "?"
        coin_bal = get_coin_balance_from(by, sym)
        value = coin_bal * price

        tp_mult = choose_tp_mult(atr, price)

        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, bal_val={value:.2f}, pos={len(st['positions'])} | "
                     f"bid={bid_s} ask={ask_s} | {sinfo} | ATR(5/15m)={atr/price*100:.2f}% | tp_mult={tp_mult:.2f}")

        # SELL / TP / SL для уже открытых позиций (только когда нет открытых SELL‑ордеров)
        # фактические продажи происходят ЛИМИТНЫМ TP, который мы ставим при покупке;
        # здесь контролируем аварийный выход SL и "подчищаем" состояния.
        if st["positions"]:
            for pos in list(st["positions"]):
                b, q = pos["buy_price"], pos["qty"]
                if q <= 0: 
                    st["positions"].remove(pos); continue
                # SL если нужно (через market)
                if price <= b * (1 - STOP_LOSS_PCT):
                    # чистая потеря (maker buy + taker sell)
                    pnl = estimate_net_pnl_buy_maker_sell_taker(b, price, q)
                    try:
                        place_market_sell(sym, q)
                        log_trade(sym, "STOP LOSS SELL", price, q, pnl, "SL taker")
                        st["pnl"] += pnl
                    except Exception as e:
                        log_msg(f"{sym}: SL SELL failed: {e}", True)
                    st["positions"] = []
                    st["avg_count"] = 0
                    st["last_stop_time"] = now_iso()
                    break

        # если есть открытый лимитник на покупку — новые не ставим
        if STATE["open_orders"].get(sym):
            continue

        # BUY логика
        if not limits_ok:
            logging.info(f"{sym}: DEBUG_SKIP | {buy_blocked_reason}")
            continue

        planned = plan_buy(sym, per_sym)
        if not planned:
            continue

        # закрываем глаза на частичное выделение (alloc/need) — qty уже округлён
        # ставим postOnly BUY и запоминаем, затем TP по факту заливки
        try:
            oid = place_postonly_buy(sym, planned["price"], planned["qty"])
            STATE["open_orders"][sym] = {
                "orderId": oid,
                "side": "Buy",
                "qty": planned["qty"],
                "placed_ts": time.time(),
                "plan_tp": planned["tp"],
                "tp_mult": planned["tp_mult"],
                "reprice_at": time.time() + DEFAULT_REPRICE
            }
            save_state()
            send_tg(f"🟢 BUY (maker) {sym} price={planned['price']:.6f}, qty={planned['qty']:.6f}, TP≈{planned['plan_tp']:.6f}")
        except Exception as e:
            log_msg(f"{sym}: BUY failed: {e}", True)

    # ежедневный отчёт (22:30 лок)
    global LAST_REPORT_DATE
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

def manage_open_orders(by_balances):
    """
    - следим за открытыми лимитниками
    - «перекатываем» каждые 30–60 сек ближе к рынку
    - при заливке BUY ставим TP (maker)
    """
    to_delete = []
    for sym, od in STATE["open_orders"].items():
        side = od["side"]
        oid  = od["orderId"]

        # 1) проверяем — всё ещё открыт?
        open_lst = fetch_open_orders(sym)
        still_open = any(o["orderId"] == oid for o in open_lst)

        if not still_open:
            # уже не открыт — значит либо отменился, либо исполнился (частично/полностью)
            det = fetch_order_detail(sym, oid)
            if not det:
                to_delete.append(sym)
                continue

            exec_qty = float(det.get("cumExecQty", 0.0))
            avg_price = float(det.get("avgPrice", 0.0) or 0.0)
            status = det.get("orderStatus", "")

            if side == "Buy" and exec_qty > 0 and status in ("Filled", "PartiallyFilled", "Completed"):
                # зафиксировалась покупка -> ставим TP лимитник (maker)
                try:
                    tp_price = round_price(sym, od["plan_tp"])
                    sell_oid = place_postonly_sell_tp(sym, tp_price, exec_qty)

                    # сохраним позицию и сам факт TP‑ордера
                    STATE[sym]["positions"] = [{
                        "buy_price": avg_price if avg_price > 0 else tp_price / (1 + 0.01),  # fallback
                        "qty": exec_qty,
                        "tp": tp_price,
                        "timestamp": now_iso()
                    }]
                    save_state()
                    send_tg(f"📈 Filled BUY {sym}: qty={exec_qty}, avg≈{avg_price:.6f}. TP set {tp_price:.6f}")
                except Exception as e:
                    log_msg(f"{sym}: TP place failed after buy fill: {e}", True)

            to_delete.append(sym)
            continue

        # 2) перекат цены, если время пришло
        if time.time() >= od.get("reprice_at", 0):
            try:
                # отменяем старый
                cancel_order(sym, oid)
            except Exception as e:
                logging.info(f"{sym}: cancel for reprice failed: {e}")

            # ставим новый у текущего bid (или ask для продажи — у нас сейчас только BUY)
            bid, ask = get_bid_ask(sym)
            if not bid:
                # перенесём перекат позже
                od["reprice_at"] = time.time() + DEFAULT_REPRICE
                continue

            new_price = round_price(sym, bid)
            try:
                new_oid = place_postonly_buy(sym, new_price, od["qty"])
                od["orderId"] = new_oid
                od["placed_ts"] = time.time()
                od["reprice_at"] = time.time() + random.randint(REPRICE_SEC_MIN, REPRICE_SEC_MAX)
                save_state()
                logging.info(f"{sym}: order re-priced to {new_price}")
            except Exception as e:
                logging.info(f"{sym}: reprice failed: {e}")
                od["reprice_at"] = time.time() + DEFAULT_REPRICE

    for s in to_delete:
        STATE["open_orders"].pop(s, None)
    if to_delete:
        save_state()

# ==================== ENTRY ====================
if __name__ == "__main__":
    try:
        init_state()
        get_limits()  # подгрузим тик‑сайзы/шаги сразу
        reconcile_positions_on_start()
        log_msg("🟢 Бот работает. Maker‑режим, фильтры ослаблены, TP≥$1 чистыми.", True)
    except Exception as e:
        log_msg(f"Старт бота: ошибка инициализации: {e}", True)
        raise

    while True:
        try:
            trade_cycle()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
