# -*- coding: utf-8 -*-
"""
Bybit Spot maker-bot (PostOnly) с перекатом лимитников и проверкой
чистой прибыли >= $1 после комиссий.

Особенности:
- Вход и TP — лимиты c timeInForce="PostOnly" (maker).
- Перекат неисполненного лимита каждые REPRICE_SEC секунд.
- Комиссии: maker BUY 0.10%, maker SELL 0.18% (по твоей ставке).
- Стоп-лосс аварийный: рыночный выход (taker 0.18%).
- Чистая прибыль >= $1 проверяется ДО размещения заявки (по maker BUY + maker SELL).
- TG-уведомления: старт/восстановление/покупка/перекат/TP/SL/ошибки/ежедневный отчёт.
- Подробные DEBUG‑логи причин пропуска сделок и расчётов.

Требуются переменные окружения (.env):
BYBIT_API_KEY, BYBIT_API_SECRET, TG_TOKEN, CHAT_ID, REDIS_URL
"""

import os, time, logging, datetime, requests, json, random, traceback
from decimal import Decimal, getcontext, ROUND_DOWN
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

# Рабочие пары (по просьбе — две более «живые» для большего объёма на сделку)
SYMBOLS = ["DOGEUSDT", "XRPUSDT"]

RESERVE_BALANCE   = 1.0        # подушка USDT
MAX_TRADE_USDT    = 220.0      # максимум средств на одну покупку (на пару)
MIN_NET_PROFIT    = 1.0        # чистая прибыль >= $1 после комиссий (входной фильтр)
STOP_LOSS_PCT     = 0.009      # SL 0.9% (аварийный выход по маркету)

# Комиссии (твои ставки)
MAKER_BUY_FEE   = 0.0010       # 0.10% вход maker
MAKER_SELL_FEE  = 0.0018       # 0.18% выход maker (по твоей сетке)
TAKER_SELL_FEE  = 0.0018       # 0.18% на SL (market)

# Перекат лимитов
REPRICE_SEC = 45               # каждые 45 сек проверяем и переставляем неисполненный лимит

# Кэш лимитов в Redis на 12 часов
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

# Отчёты
LAST_REPORT_DATE = None

# Decimal точность
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

# ==================== TG ====================
def send_tg(msg: str):
    if not (TG_VERBOSE and TG_TOKEN and CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        logging.error("Telegram send failed: " + str(e))

def log_msg(msg: str, tg: bool=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

# ==================== STATE HELPERS ====================
def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp}]
            "pnl": 0.0,
            "open_buy": None,     # {"orderId","price","qty","time"}
            "open_tp":  None,     # {"orderId","price","qty","time"}
            "last_stop_time": ""
        })

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    ensure_state_consistency()
    log_msg("✅ Состояние загружено из Redis" if raw else "ℹ Начинаем с чистого состояния", True)

# ==================== API WRAPPER ====================
def api_call(fn, *args, **kwargs):
    wait = 0.35
    for _ in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logging.warning(f"API retry {fn.__name__} wait={wait:.2f}s error={e}")
            time.sleep(wait)
            wait = min(wait*2, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

# ==================== LIMITS (tickSize/qtyStep/minAmt) ====================
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

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

def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    out = {}
    for it in lst:
        sym = it["symbol"]
        if sym not in SYMBOLS:
            continue
        pf  = it.get("priceFilter", {}) or {}
        lot = it.get("lotSizeFilter", {}) or {}
        out[sym] = {
            "tick_size": float(pf.get("tickSize", 0.00000001)),
            "qty_step":  float(lot.get("qtyStep", 0.00000001)),
            "min_qty":   float(lot.get("minOrderQty", 0.0)),
            "min_amt":   float(it.get("minOrderAmt", 10.0))
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

# ==================== MARKET DATA / BALANCES ====================
def get_kline(sym, interval="1", limit=120):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def get_bid_ask(sym):
    """
    Надёжный парсер стакана Bybit Spot:
    - поддерживает оба формата: [{'p':'...','s':'...'}] и [['price','size',...]]
    - при ошибке — fallback на тикер
    """
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        ob = r.get("result", {}) or {}
        asks = ob.get("a") or ob.get("asks") or []
        bids = ob.get("b") or ob.get("bids") or []

        def _price(x):
            if isinstance(x, dict):
                return float(x.get("p") or x.get("price"))
            elif isinstance(x, (list, tuple)) and len(x) > 0:
                return float(x[0])
            return None

        a = _price(asks[0]) if asks else None
        b = _price(bids[0]) if bids else None
        if a is not None and b is not None:
            return b, a
    except Exception as e:
        logging.error(f"orderbook failed {sym}: {e}")

    # Fallback — тикер
    try:
        t = api_call(session.get_tickers, category="spot", symbol=sym)
        lst = t.get("result", {}).get("list") or []
        if lst:
            it = lst[0]
            bid = float(it.get("bid1Price") or it.get("bestBidPrice") or it.get("bidPrice"))
            ask = float(it.get("ask1Price") or it.get("bestAskPrice") or it.get("askPrice"))
            return bid, ask
    except Exception as e2:
        logging.error(f"ticker fallback failed {sym}: {e2}")

    return None, None

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    m = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(m.get("USDT", 0.0)), m

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT",""), 0.0))

# ==================== ROUND / QTY ====================
def floor_to_step(x, step):
    if step <= 0:
        return float(x)
    q = (Decimal(str(x)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    return float(q * Decimal(str(step)))

def round_price_to_tick(price, tick):
    return floor_to_step(price, tick)

def round_qty_to_step(qty, step):
    return floor_to_step(qty, step)

def get_qty(sym, price, usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = round_qty_to_step(alloc / price, limits[sym]["qty_step"])
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS ====================
def signal(df):
    if df.empty or len(df) < 60:
        return "none", 0.0, {}
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    macd  = MACD(close=df["c"])
    macd_line, macd_sig = macd.macd(), macd.macd_signal()
    atr14 = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()

    last = len(df)-1
    ema9v  = float(ema9.iloc[last])
    ema21v = float(ema21.iloc[last])
    rsiv   = float(rsi9.iloc[last])
    macdv  = float(macd_line.iloc[last])
    sigv   = float(macd_sig.iloc[last])
    atrv   = float(atr14.iloc[last])
    price  = float(df["c"].iloc[last])

    # Условие входа: 2 из 3 и/или мягкий кросс EMA вверх
    cond1 = (ema9v > ema21v)
    cond2 = (rsiv  > 48)
    cond3 = (macdv > sigv)
    prev_cross = (float(ema9.iloc[last-1]) <= float(ema21.iloc[last-1])) and cond1 and (rsiv > 45)

    buy_ok = (cond1 and (cond2 or cond3)) or prev_cross

    info = {
        "ema9": ema9v, "ema21": ema21v, "rsi": rsiv,
        "macd": macdv, "sig": sigv, "atr": atrv
    }
    atr_pct = (atrv / price) if price > 0 else 0.0
    return ("buy" if buy_ok else "none"), atr_pct, info

def choose_tp_multiplier(atr_pct):
    # Чем выше волатильность — тем выше целевой мультипликатор от ATR
    if atr_pct < 0.003:   return 1.30
    if atr_pct < 0.006:   return 1.40
    if atr_pct < 0.010:   return 1.55
    return 1.80

# ==================== PNL ====================
def estimate_net_pnl_for_tp(entry_price, tp_price, qty, maker_sell=True):
    """Чистая прибыль после комиссий.
       Вход: maker 0.10%.
       Выход: maker 0.18% (по твоей сетке) если maker_sell=True, иначе taker 0.18% (для SL/ worst-case)."""
    buy_fee  = entry_price * qty * MAKER_BUY_FEE
    sell_fee = tp_price   * qty * (MAKER_SELL_FEE if maker_sell else TAKER_SELL_FEE)
    gross    = (tp_price - entry_price) * qty
    return gross - (buy_fee + sell_fee)

# ==================== ORDERS ====================
def place_postonly_limit(sym, side, price, qty):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        raise RuntimeError("limits not available")
    price = round_price_to_tick(price, limits[sym]["tick_size"])
    qty   = round_qty_to_step(qty,  limits[sym]["qty_step"])
    if qty <= 0 or price <= 0:
        raise RuntimeError("rounded qty/price invalid")
    r = api_call(session.place_order,
                 category="spot", symbol=sym, side=side,
                 orderType="Limit", qty=str(qty), price=str(price),
                 timeInForce="PostOnly")
    return r["result"]["orderId"], price, qty

def cancel_order(sym, order_id):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
    except Exception as e:
        logging.warning(f"{sym}: cancel failed {order_id}: {e}")

def order_is_open(sym, order_id):
    try:
        r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=order_id)
        lst = r.get("result", {}).get("list") or []
        return any(o.get("orderId")==order_id for o in lst)
    except Exception:
        return False

def place_market_sell(sym, qty):
    r = api_call(session.place_order,
                 category="spot", symbol=sym, side="Sell",
                 orderType="Market", qty=str(qty))
    return r["result"]["orderId"]

# ==================== RESTORE ====================
def reconcile_positions_on_start():
    """Синхронизация по балансу: создаём синтетические позиции, если монеты есть."""
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()
    total = 0.0
    lines = []
    for sym in SYMBOLS:
        st = STATE[sym]
        df = get_kline(sym)
        if df.empty: 
            continue
        price = float(df["c"].iloc[-1])
        bal   = get_coin_balance_from(by, sym)
        if bal > 0:
            qty = round_qty_to_step(bal, limits[sym]["qty_step"] if ok else 1.0)
            st["positions"] = [{
                "buy_price": price, "qty": qty, "tp": price * 1.01,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            total += qty * price
            lines.append(f"- {sym}: синхр. позиция qty={qty} по ~{price:.6f}")
        else:
            st["positions"] = []
            lines.append(f"- {sym}: позиций нет, баланса мало")
    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" + "\n".join(lines) + f"\n📊 Номинал по монетам: ${total:.2f}", True)
    log_msg("🟢 Бот работает. Maker‑режим, фильтры ослаблены, TP≥$1 чистыми.", True)

# ==================== TP/REPRICE HELPERS ====================
def reprice_if_needed(sym, open_key, target_price, qty):
    """Перекатываем (cancel→place) ордер, если висит дольше REPRICE_SEC."""
    st = STATE[sym]
    rec = st.get(open_key)
    if not rec:
        return
    if order_is_open(sym, rec["orderId"]):
        # проверь таймер
        if time.time() - rec["time"] < REPRICE_SEC:
            return
        # если просто пора перекатить — отменяем и ставим заново ближе к рынку
        cancel_order(sym, rec["orderId"])
    # ставим новый
    side = "Buy" if open_key == "open_buy" else "Sell"
    try:
        oid, p, q = place_postonly_limit(sym, side, target_price, qty)
        st[open_key] = {"orderId": oid, "price": p, "qty": q, "time": time.time()}
        save_state()
        log_msg(f"{sym}: 🔁 Перекат {side} → {p:.6f}, qty={q}", True)
    except Exception as e:
        logging.warning(f"{sym}: reprice {side} failed: {e}")

def ensure_tp(sym, entry_price, atr_pct, qty):
    """Гарантируем наличие TP (maker). Цена TP адаптивна от ATR%."""
    tp_mult = choose_tp_multiplier(atr_pct)
    tp_price = entry_price * (1 + atr_pct * tp_mult)
    st = STATE[sym]
    # если TP есть — просто перекатим по таймеру в ту же цену
    if st.get("open_tp"):
        reprice_if_needed(sym, "open_tp", tp_price, st["open_tp"]["qty"])
        return
    # выставляем новый TP
    try:
        oid, p, q = place_postonly_limit(sym, "Sell", tp_price, qty)
        st["open_tp"] = {"orderId": oid, "price": p, "qty": q, "time": time.time()}
        save_state()
        log_msg(f"{sym}: TP placed @ {p:.6f} qty={q}", True)
    except Exception as e:
        logging.warning(f"{sym}: place TP failed: {e}")

# ==================== MAIN CYCLE ====================
def trade():
    global LAST_REPORT_DATE
    limits, limits_ok, reason = get_limits()

    usdt, by = get_balances_cache()
    avail   = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0.0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        df = get_kline(sym)
        if df.empty:
            continue

        sig, atr_pct, ind = signal(df)
        price = float(df["c"].iloc[-1])
        bid, ask = get_bid_ask(sym)

        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, pos={1 if st['positions'] else 0} | "
                     f"bid={(f'{bid:.6f}' if bid else '?')} ask={(f'{ask:.6f}' if ask else '?')} | "
                     f"EMA9={ind.get('ema9',0):.6f} EMA21={ind.get('ema21',0):.6f} RSI={ind.get('rsi',0):.2f} "
                     f"MACD={ind.get('macd',0):.6f} SIG={ind.get('sig',0):.6f} | ATR%={atr_pct*100:.2f}%")

        # Если есть позиция — убедимся, что TP стоит/обновлён; SL аварийный
        if st["positions"]:
            pos = st["positions"][0]
            entry = pos["buy_price"]; qty = pos["qty"]
            # SL по % (только если ударили стоп)
            if price <= entry * (1 - STOP_LOSS_PCT):
                pnl_sl = estimate_net_pnl_for_tp(entry, price, qty, maker_sell=False)
                try:
                    place_market_sell(sym, qty)
                    st["positions"] = []
                    st["open_tp"] = None
                    st["pnl"] += pnl_sl
                    save_state()
                    log_msg(f"🛑 STOP-LOSS {sym}: market sell qty={qty}, pnl≈{pnl_sl:.2f}", True)
                except Exception as e:
                    log_msg(f"{sym}: SL failed: {e}", True)
                continue
            # Обновим/поставим TP
            if bid and ask:
                ensure_tp(sym, entry, atr_pct, qty)

        # Покупка — только если позиции нет и нет открытого BUY
        if st["positions"] or st.get("open_buy"):
            # актуализируем перекат BUY (если есть) к текущему bid
            if st.get("open_buy") and bid:
                reprice_if_needed(sym, "open_buy", bid, st["open_buy"]["qty"])
            continue

        if sig != "buy":
            continue
        if not limits_ok:
            logging.info(f"{sym}: Пропуск BUY — {reason}")
            continue
        if not bid or not ask:
            logging.info(f"{sym}: DEBUG_SKIP | orderbook empty")
            continue
        if per_sym < (limits.get(sym, {}).get("min_amt", 10.0)):
            logging.info(f"{sym}: Пропуск BUY — мало USDT per_sym={per_sym:.2f}")
            continue

        # Расчёт объёма и TP
        qty = get_qty(sym, ask, per_sym)
        if qty <= 0:
            L = limits.get(sym, {})
            logging.info(f"{sym}: Пропуск BUY — qty=0 (price={ask:.6f}, step={L.get('qty_step')}, min_qty={L.get('min_qty')}, min_amt={L.get('min_amt')})")
            continue

        tp_mult = choose_tp_multiplier(atr_pct)
        tp_price = ask * (1 + atr_pct * tp_mult)
        # Проверка прибыли по maker→maker
        est_pnl = estimate_net_pnl_for_tp(ask, tp_price, qty, maker_sell=True)
        logging.info(f"[{sym}] BUY-check qty={qty:.6f}, entry≈{ask:.6f}, tp≈{tp_price:.6f}, "
                     f"est_pnl={est_pnl:.2f} vs required={MIN_NET_PROFIT:.2f} (fees: 0.10%/0.18%)")

        if est_pnl < MIN_NET_PROFIT:
            logging.info(f"{sym}: Пропуск BUY — ожидаемый PnL {est_pnl:.2f} < {MIN_NET_PROFIT:.2f}")
            continue

        # Ставим PostOnly BUY по bid (чтобы гарантированно maker)
        try:
            buy_px = bid
            oid, px, qx = place_postonly_limit(sym, "Buy", buy_px, qty)
            st["open_buy"] = {"orderId": oid, "price": px, "qty": qx, "time": time.time()}
            st["positions"] = [{
                "buy_price": px, "qty": qx, "tp": tp_price,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            save_state()
            log_msg(f"🟢 BUY (maker) {sym} @ {px:.6f} qty={qx:.6f}\nTP≈{tp_price:.6f} (ATR×{tp_mult:.2f})", True)
            # Поставим TP сразу
            ensure_tp(sym, px, atr_pct, qx)
        except Exception as e:
            logging.error(f"{sym}: BUY place failed: {e}\n{traceback.format_exc()}")

    # Ежедневный отчёт (22:30 лок.)
    now = datetime.datetime.now()
    global LAST_REPORT_DATE
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== REPORT ====================
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_text = "нет открытых позиций"
        if st["positions"]:
            p = st["positions"][0]
            pos_text = f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}"
        lines.append(f"• {s}: PnL={st['pnl']:.2f}; {pos_text}")
        total += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    try:
        init_state()
        get_limits()  # прогреем тик/шаг
        reconcile_positions_on_start()
        while True:
            try:
                trade()
            except Exception as e:
                log_msg(f"Global error: {e}", True)
            time.sleep(60)
    except Exception as e:
        log_msg(f"Startup error: {e}", True)
