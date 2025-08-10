# -*- coding: utf-8 -*-
import os, time, math, logging, datetime, requests, json, random
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

RESERVE_BALANCE      = 1.0
MIN_NET_PROFIT       = 1.00     # $1 чистыми после комиссий — НЕ УМЕНЬШАЕМ
STOP_LOSS_PCT        = 0.008    # 0.8%
MAX_RETRIES_API      = 6

# Комиссии спот (Bybit, обычный пользователь)
# важно: мы целимся в МЕЙКЕРА (лимит postOnly). На такере хуже — учитываем отдельно.
MAKER_FEE_BUY  = 0.0010   # 0.10%
MAKER_FEE_SELL = 0.0010   # 0.10%
TAKER_FEE_BUY  = 0.0018   # 0.18%
TAKER_FEE_SELL = 0.0018   # 0.18%

# Управление аллокацией (динамический размер позиции)
MAX_TRADE_USDT_BASE = 120.0   # базовый максимум
MAX_TRADE_USDT_MAX  = 350.0   # верхняя граница (на низкой волатильности разрешим больше)

# Список инструментов
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

# Таймфреймы для сигналов (микс), ATR для TP берём со старшего ТФ
TF_1M = "1"
TF_3M = "3"
TF_5M = "5"
TF_15M = "15"

# Параметры лимитных заявок (мейкер)
POSTONLY_TIMEOUT_SEC   = 25     # сколько ждём залив/исполнение лимита
POSTONLY_REPRICES      = 5      # сколько раз переустанавливаем лимит
PARTIAL_TP_SPLIT       = (0.50, 0.50)  # 50% + 50%
PARTIAL_TP_FACTORS     = (0.80, 1.00)  # 0.8*TPdist и 1.0*TPdist

# Кэш лимитов в Redis
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

LAST_REPORT_DATE = None
cycle_count = 0

getcontext().prec = 28

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

STATE = {}
"""
STATE[sym] = {
    "positions": [
        { "buy_price": float, "qty": float, "tp": float, "timestamp": str,
          "order_ids": {"buy": "...", "tp1": "...", "tp2": "..."} }
    ],
    "pnl": float,
    "avg_count": int,
    "last_sell_price": float,
    "max_drawdown": float,
    "last_stop_time": str
}
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

SKIP_LOG_TIMESTAMPS = {}
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

# ==================== HELPERS (log/TG) ====================
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
            "positions": [],
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
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

# ==================== API RETRIES ====================
def api_call(fn, *args, **kwargs):
    wait = 0.35
    for attempt in range(MAX_RETRIES_API):
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
    return {
        item["symbol"]: {
            "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0)),
            "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0)),
        }
        for item in lst if item["symbol"] in SYMBOLS
    }

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

# ==================== MARKET DATA & BALANCES ====================
def get_kline(sym, interval="1", limit=200):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    if df.empty:
        return df
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_orderbook_best(sym):
    r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
    bids = r["result"]["b"]
    asks = r["result"]["a"]
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    return best_bid, best_ask

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== ROUNDING/QTY ====================
def adjust_qty(qty, step):
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def dynamic_max_alloc_usdt(atr_pct):
    # чем тише рынок, тем больше можем аллоцировать (в разумных пределах)
    # atr_pct ~ 0.005 → множитель 2.0; atr_pct ~ 0.015 → множитель 1.0
    if atr_pct <= 0:
        return MAX_TRADE_USDT_BASE
    base = MAX_TRADE_USDT_BASE
    mult = max(1.0, min(2.5, 0.015 / atr_pct))  # clamp
    alloc = min(MAX_TRADE_USDT_MAX, base * mult)
    return alloc

def get_qty_with_limits(sym, price, desired_usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    q = adjust_qty(desired_usdt / price, limits[sym]["qty_step"])
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS ====================
def calc_indicators(df):
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    macd  = MACD(close=df["c"])
    atr14 = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    out = df.copy()
    out["ema9"] = ema9
    out["ema21"] = ema21
    out["rsi"] = rsi9
    out["macd"] = macd.macd()
    out["sig"]  = macd.macd_signal()
    out["atr"]  = atr14
    return out

def build_signal(sym):
    d1  = get_kline(sym, TF_1M, 200)
    d3  = get_kline(sym, TF_3M, 200)
    d5  = get_kline(sym, TF_5M, 200)
    d15 = get_kline(sym, TF_15M, 200)
    if d1.empty or d3.empty or d5.empty or d15.empty:
        return None

    i1, i3, i5 = calc_indicators(d1), calc_indicators(d3), calc_indicators(d5)
    i15 = calc_indicators(d15)

    last1, last3, last5, last15 = i1.iloc[-1], i3.iloc[-1], i5.iloc[-1], i15.iloc[-1]
    price = last1["c"]

    # биды/аски для логов и цены лимита
    bid, ask = get_orderbook_best(sym)

    # Лёгкие правила входа: 2 из 3 на 1–5м, RSI не ниже 46
    buy_2of3 = sum([
        last1["ema9"] > last1["ema21"],
        last3["rsi"] > 46,
        last5["macd"] > last5["sig"]
    ]) >= 2

    sell_2of3 = sum([
        last1["ema9"] < last1["ema21"],
        last3["rsi"] < 54,
        last5["macd"] < last5["sig"]
    ]) >= 2

    # ATR для TP — берём max из 5м и 15м (более «длинная» дистанция)
    atr_for_tp = max(float(last5["atr"]), float(last15["atr"]))
    atr_pct    = atr_for_tp / price if price > 0 else 0.0

    # адаптивный множитель TP: тише рынок → дальше TP
    if atr_pct < 0.006:
        tp_mult = 1.8
    elif atr_pct < 0.012:
        tp_mult = 1.3
    else:
        tp_mult = 0.9

    info = (f"bid={bid:.6f} | ask={ask:.6f} | "
            f"EMA9={last1['ema9']:.6f} EMA21={last1['ema21']:.6f} | "
            f"RSI={last3['rsi']:.2f} | MACD={last5['macd']:.6f} SIG={last5['sig']:.6f} | "
            f"ATR(5/15m)={atr_for_tp:.6f} ({atr_pct*100:.2f}%) | tp_mult={tp_mult:.2f}")

    return {
        "price": price,
        "bid": bid, "ask": ask,
        "sig": "buy" if buy_2of3 else ("sell" if sell_2of3 else "none"),
        "atr": atr_for_tp,
        "atr_pct": atr_pct,
        "tp_mult": tp_mult,
        "i1": last1, "i3": last3, "i5": last5, "i15": last15,
        "info": info
    }

# ==================== PNL MATH ====================
def per_unit_net_profit(price_buy, price_sell, maker=True):
    if maker:
        fee_b = MAKER_FEE_BUY
        fee_s = MAKER_FEE_SELL
    else:
        fee_b = TAKER_FEE_BUY
        fee_s = TAKER_FEE_SELL
    # прибыль на 1 монету с учётом комиссий
    return (price_sell - price_buy) - (fee_b * price_buy) - (fee_s * price_sell)

def qty_needed_for_usd(net_usd_target, price_buy, price_sell, maker=True):
    ppu = per_unit_net_profit(price_buy, price_sell, maker)
    if ppu <= 0:
        return float("inf")
    return net_usd_target / ppu

# ==================== ORDER MANAGEMENT (POST-ONLY) ====================
def place_postonly_limit(sym, side, price, qty, tif="PostOnly", reduce_only=False):
    # Bybit Unified Trading: timeInForce="PostOnly" поддерживается для спота
    params = dict(
        category="spot",
        symbol=sym,
        side="Buy" if side.lower()=="buy" else "Sell",
        orderType="Limit",
        qty=str(qty),
        price=str(price),
        timeInForce=tif
    )
    # reduce_only флаг для спота нет, просто информативно
    r = api_call(session.place_order, **params)
    return r["result"]["orderId"]

def cancel_order(sym, order_id):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
    except Exception as e:
        logging.warning(f"{sym}: cancel_order failed {order_id}: {e}")

def get_order_filled_qty(sym, order_id):
    r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=order_id)
    # если ордер не в «open», он мог исполниться или быть отменён → проверим через trades
    if not r["result"]["list"]:
        # Попытаемся найти исполнение в истории сделок
        tr = api_call(session.get_executions, category="spot", symbol=sym, orderId=order_id)
        qty = 0.0
        for t in tr.get("result", {}).get("list", []):
            qty += float(t.get("execQty", 0.0))
        return qty
    # open → filled_qty = cumExecQty
    lst = r["result"]["list"][0]
    return float(lst.get("cumExecQty", 0.0))

def wait_postonly_fill_or_reprice(sym, side, qty, max_reprices, timeout_sec, get_price_fn):
    """Ставим postOnly у лучшей цены, ждём fill, по таймауту отменяем, репрайсим.
       Возвращает (filled_qty, avg_price, last_order_id)"""
    filled_total = 0.0
    avg_price_accum = 0.0
    last_order_id = None

    for attempt in range(max_reprices):
        # цена у бида (для buy) или у аска (для sell), чуть «внутрь» для уверенного пост‑онли
        bid, ask = get_orderbook_best(sym)
        if side.lower()=="buy":
            price = min(get_price_fn(), bid)  # не хуже бида
        else:
            price = max(get_price_fn(), ask)  # не хуже аска

        # Небольшой сдвиг внутрь спрэда, чтобы не превращаться в такера
        # (но Bybit сам отклонит при postOnly, если скрестим)
        price = float(f"{price:.10f}")

        try:
            order_id = place_postonly_limit(sym, side, price, qty - filled_total, tif="PostOnly")
            last_order_id = order_id
            logging.info(f"{sym}: postOnly {side} placed id={order_id} price={price:.10f} qty={qty - filled_total:.8f}")
        except Exception as e:
            logging.warning(f"{sym}: postOnly place failed({side}): {e}")
            break

        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            time.sleep(2)
            f = get_order_filled_qty(sym, order_id)
            if f > 0:
                # узнаём среднюю цену исполнения по сделкам ордера
                tr = api_call(session.get_executions, category="spot", symbol=sym, orderId=order_id)
                fill_val = 0.0; fill_qty = 0.0
                for t in tr.get("result", {}).get("list", []):
                    fill_qty += float(t.get("execQty", 0.0))
                    fill_val += float(t.get("execPrice", 0.0)) * float(t.get("execQty", 0.0))
                avg_fill = fill_val / fill_qty if fill_qty>0 else price
                filled_total += f
                avg_price_accum += avg_fill * f
                logging.info(f"{sym}: filled {f:.8f} / {qty:.8f} @ ~{avg_fill:.10f}")
                # если полностью
                if filled_total + 1e-12 >= qty:
                    return filled_total, (avg_price_accum / filled_total), last_order_id
                break  # частично → репрайсим остаток
        # не успели — отменяем и репрайсим
        cancel_order(sym, order_id)

    avg = (avg_price_accum / filled_total) if filled_total>0 else 0.0
    return filled_total, avg, last_order_id

# ==================== STRATEGY HELPERS ====================
def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

def choose_tp_target(price, atr_for_tp, tp_mult):
    return price + tp_mult * atr_for_tp

def calc_buy_plan(sym, mkt, avail_usdt):
    """Рассчитать TP и нужную qty так, чтобы >= $1 чистыми при МЕЙКЕР комиссий."""
    price = mkt["ask"]  # покупаем по аску (мейкер около него)
    tp    = choose_tp_target(price, mkt["atr"], mkt["tp_mult"])
    # сколько нужно qty, чтобы дать >= $1 чистыми при maker
    need_qty = qty_needed_for_usd(MIN_NET_PROFIT, price, tp, maker=True)

    # динамическая аллокация из волатильности
    max_alloc = dynamic_max_alloc_usdt(mkt["atr_pct"])
    desired_usdt = min(max_alloc, avail_usdt)

    # но если при desired_usdt qty меньше need_qty — пробуем поднять до MAX_TRADE_USDT_MAX
    # (в рамках get_qty_with_limits это срежется нижними пределами)
    qty_from_alloc = get_qty_with_limits(sym, price, desired_usdt)

    # если qty_from_alloc < need_qty → значит $1 не покрываем при таком TP → вход невыгоден
    plan = {
        "tp": tp,
        "need_qty": need_qty,
        "qty_alloc": qty_from_alloc,
        "max_alloc": max_alloc,
        "price": price
    }
    return plan

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty:.8f}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty:.8f},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE ====================
def reconcile_positions_on_start():
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()
    total_nominal = 0.0
    lines = []

    for sym in SYMBOLS:
        bal = get_coin_balance_from(by, sym)
        price = get_kline(sym, TF_1M, 2)
        if price.empty:
            continue
        last_price = float(price["c"].iloc[-1])

        step = limits.get(sym, {}).get("qty_step", 1.0) if ok else 1.0
        min_amt = limits.get(sym, {}).get("min_amt", 10.0) if ok else 10.0

        bal = adjust_qty(bal, step)
        st = STATE[sym]
        if st["positions"]:
            # оставляем как есть
            lines.append(f"- {sym}: найдены сохранённые позиции ({len(st['positions'])}) — оставляю без изменений")
        else:
            if bal >= step and bal * last_price >= min_amt:
                # создаём «синтетическую» позицию, TP вычислим по текущему ATR
                mkt = build_signal(sym)
                if not mkt:
                    continue
                tp = choose_tp_target(last_price, mkt["atr"], mkt["tp_mult"])
                st["positions"] = [{
                    "buy_price": last_price, "qty": bal, "tp": tp,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "order_ids": {}
                }]
                lines.append(f"- {sym}: синтетическая позиция qty={bal} @ {last_price:.6f}")
            else:
                lines.append(f"- {sym}: позиций нет, баланса мало")

        total_nominal += bal * last_price

    save_state()
    log_msg("🚀 Бот запущен (восстановление)\n" + "\n".join(lines) + f"\n🧾 Номинал: ${total_nominal:.2f}", True)

# ==================== TP ORDERS ====================
def place_partial_tp_orders(sym, buy_price, qty, tp_price):
    # частичные тейки: два лимита postOnly
    dist = tp_price - buy_price
    tp1 = buy_price + PARTIAL_TP_FACTORS[0] * dist
    tp2 = buy_price + PARTIAL_TP_FACTORS[1] * dist

    q1 = adjust_qty(qty * PARTIAL_TP_SPLIT[0], 1e-12)
    q2 = qty - q1

    # выставим два ордера пост‑онли
    filled1, avg1, oid1 = wait_postonly_fill_or_reprice(
        sym, "sell", q1, 1, 0, lambda: tp1
    )  # здесь мы не ждём исполнения — просто размещаем
    # ИЗЮМИНКА: для «постановки» без ожидания используем прямой place_postonly_limit:
    try:
        oid1 = place_postonly_limit(sym, "sell", tp1, q1, tif="PostOnly")
        logging.info(f"{sym}: TP1 placed id={oid1} @ {tp1:.10f} qty={q1:.8f}")
    except Exception as e:
        logging.warning(f"{sym}: TP1 place failed: {e}")
        oid1 = None

    try:
        oid2 = place_postonly_limit(sym, "sell", tp2, q2, tif="PostOnly")
        logging.info(f"{sym}: TP2 placed id={oid2} @ {tp2:.10f} qty={q2:.8f}")
    except Exception as e:
        logging.warning(f"{sym}: TP2 place failed: {e}")
        oid2 = None

    return {"tp1": oid1, "tp2": oid2}

def try_close_tp_when_reached(sym, st, mkt_price):
    # Проверяем открытые позиции — если продано частично/полностью, пересчитываем PnL
    new_positions = []
    for pos in st["positions"]:
        buy_price = pos["buy_price"]; qty = pos["qty"]
        oids = pos.get("order_ids", {})
        sold_qty = 0.0
        sell_val = 0.0

        for key in ("tp1", "tp2"):
            oid = oids.get(key)
            if not oid: continue
            f = get_order_filled_qty(sym, oid)
            if f > 0:
                tr = api_call(session.get_executions, category="spot", symbol=sym, orderId=oid)
                for t in tr.get("result", {}).get("list", []):
                    q = float(t.get("execQty", 0.0))
                    p = float(t.get("execPrice", 0.0))
                    sold_qty += q
                    sell_val += q * p

        if sold_qty > 0:
            buy_val = buy_price * sold_qty
            pnl = (sell_val - buy_val) - (MAKER_FEE_BUY*buy_val) - (MAKER_FEE_SELL*sell_val)
            st["pnl"] += pnl
            log_trade(sym, "TP PART SELL", sell_val/sold_qty, sold_qty, pnl, "partial take‑profit")
            remaining = qty - sold_qty
            if remaining > 1e-12:
                # оставим «хвост» как новую позицию БЕЗ старых ордеров
                new_positions.append({
                    "buy_price": buy_price, "qty": remaining, "tp": pos["tp"],
                    "timestamp": pos["timestamp"], "order_ids": {}
                })
        else:
            new_positions.append(pos)

    st["positions"] = new_positions
    save_state()

# ==================== MAIN LOOP ====================
def trade():
    global cycle_count, LAST_REPORT_DATE

    limits, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym≈{per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        st["sell_failed"] = False

        mkt = build_signal(sym)
        if not mkt:
            continue

        price = mkt["price"]
        bid, ask = mkt["bid"], mkt["ask"]

        coin_bal = get_coin_balance_from(by, sym)
        bal_val = coin_bal * price

        logging.info(f"[{sym}] sig={mkt['sig']}, price={price:.6f}, bal_val={bal_val:.2f}, pos={len(st['positions'])} | {mkt['info']}")

        # Проверяем частичные TP по уже выставленным заявкам
        try_close_tp_when_reached(sym, st, price)

        # STOP‑LOSS по позиции (если есть), исполняем маркетом ТОЛЬКО если >$1 потерь
        for pos in list(st["positions"]):
            b, q = pos["buy_price"], pos["qty"]
            if q <= 0: continue
            # текущая потенциальная чистая PnL (как будто закроем маркет‑тейкером)
            buy_val  = b * q
            sell_val = price * q
            pnl_now = (sell_val - buy_val) - (TAKER_FEE_BUY*buy_val) - (TAKER_FEE_SELL*sell_val)
            if price <= b * (1 - STOP_LOSS_PCT) and abs(pnl_now) >= MIN_NET_PROFIT and coin_bal >= q:
                try:
                    api_call(session.place_order, category="spot", symbol=sym,
                             side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl_now, "stop‑loss (taker)")
                    st["pnl"] += pnl_now
                except Exception as e:
                    log_msg(f"{sym}: STOP SELL failed: {e}", True)
                st["positions"].clear()
                st["last_stop_time"] = datetime.datetime.now().isoformat()
                st["avg_count"] = 0
                break

        # Если позиция осталась открытой — к покупке не переходим
        if st["positions"]:
            continue

        # BUY LOGIC (мейкер, лимит, пост‑онли), вход только если ожидаем >=$1 чистыми
        if mkt["sig"] == "buy":
            last_stop = st.get("last_stop_time", "")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs < 4:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — {hrs:.1f}ч после SL < 4ч")
                continue
            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                continue
            if avail < (limits[sym]["min_amt"] if sym in limits else 10.0):
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — мало свободного USDT")
                continue

            plan = calc_buy_plan(sym, mkt, avail)
            price_buy = plan["price"]
            tp_price  = plan["tp"]
            qty_alloc = plan["qty_alloc"]
            need_qty  = plan["need_qty"]

            # отладочный расчёт ожидаемого PnL на qty_alloc
            ppu      = per_unit_net_profit(price_buy, tp_price, maker=True)
            est_pnl  = max(0.0, ppu * qty_alloc)

            logging.info(f"[{sym}] BUY-check qty_alloc={qty_alloc:.8f}, need_qty={need_qty:.8f}, "
                         f"tp={tp_price:.6f}, ppu={ppu:.6f}, est_pnl={est_pnl:.2f}, required={MIN_NET_PROFIT:.2f}, "
                         f"max_alloc=${plan['max_alloc']:.2f}")

            if qty_alloc <= 0 or qty_alloc + 1e-12 < need_qty:
                # не покрываем $1 чистыми при текущем TP → пропускаем
                if should_log_skip(sym, "skip_low_profit"):
                    log_skip(sym, f"Пропуск BUY — ожидаемый PnL {est_pnl:.2f} < {MIN_NET_PROFIT:.2f} "
                                  f"(надо qty≥{need_qty:.6f}, есть {qty_alloc:.6f})")
                continue

            # === ВХОД МЕЙКЕРОМ (postOnly лимит у ask) с репрайсом ===
            def buy_price_source():
                # целимся около лучшего ask
                return get_orderbook_best(sym)[1] or price_buy

            filled, avg_buy, oid = wait_postonly_fill_or_reprice(
                sym, "buy", qty_alloc, POSTONLY_REPRICES, POSTONLY_TIMEOUT_SEC, buy_price_source
            )
            if filled <= 0:
                if should_log_skip(sym, "buy_unfilled"):
                    log_skip(sym, "BUY пост‑онли не исполнен — рынок ушёл/нет ликвидности")
                continue

            # выставляем частичные TP (postOnly sell) от средней цены входа
            ids = place_partial_tp_orders(sym, avg_buy, filled, tp_price)

            # сохраняем позицию
            STATE[sym]["positions"].append({
                "buy_price": avg_buy,
                "qty": filled,
                "tp": tp_price,
                "timestamp": datetime.datetime.now().isoformat(),
                "order_ids": {"buy": oid, **ids}
            })
            log_trade(sym, "BUY (maker)", avg_buy, filled, 0.0,
                      f"tp={tp_price:.6f}, postOnly, partial TP placed")
            save_state()

    cycle_count += 1

    # Ежедневный отчёт в 22:30+
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        globals()["LAST_REPORT_DATE"] = now.date()

# ==================== REPORT ====================
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total_pnl = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']:.6f} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}")
        pos_text = ("\n    " + "\n    ".join(pos_lines)) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    log_msg("♻️ Старт бота…", True)
    init_state()
    reconcile_positions_on_start()
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
