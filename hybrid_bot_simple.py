import os, time, math, logging, datetime, requests, json
from decimal import Decimal, getcontext
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

# ==================== VERSION ====================
VERSION = "MTF_v2.0_MAKER_LMT_adjTP_afterFees"

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

# Требуемая чистая прибыль (минимум) после всех комиссий
MIN_NET_PROFIT    = 1.0

# Stop-loss (рыночный выход, чтобы не висеть)
STOP_LOSS_PCT     = 0.008        # 0.8% от цены входа
NO_REBUY_AFTER_SL_HRS = 4

# Комиссии (из твоего скрина)
MAKER_BUY_FEE   = 0.0010   # 0.10%
MAKER_SELL_FEE  = 0.0010   # 0.10%
TAKER_SELL_FEE  = 0.0018   # 0.18% (на SL, если сработает)

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
LAST_REPORT_DATE = None
cycle_count = 0

# ====== Мульти-таймфреймы ======
EXEC_PRICE_INTERVAL = "3"                 # цену берём с 3m
INTERVALS = ["3", "5", "15", "30", "60"]  # сводный сигнал + ATR
VOTE_MODE = "majority"                    # majority | all | any
ATR_SOURCE = "max"                        # max | median | mean

# ====== Maker режим ======
MAKER_OFFSET_BP = 5            # отступ от лучшей цены, bps (5 = 0.05%)
REQUOTE_SEC     = 45           # раз в N сек отменяем и переставляем лимит
MAX_TP_MULTIPLIER = 3.0        # потолок авто‑увеличения множителя TP

# Кэш лимитов в Redis на 12 часов
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

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
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

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

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp}]
            "pnl": 0.0,
            "last_stop_time": "",
            "pending": None       # {"id","side","price","qty","type","ts","required_pnl","tp_target"}
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg(f"✅ Запуск {VERSION} | INTERVALS={INTERVALS} | EXEC={EXEC_PRICE_INTERVAL} | ATR={ATR_SOURCE}", True)
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

# ==================== LIMITS (Redis cache) ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    out = {}
    for item in lst:
        sym = item["symbol"]
        if sym in SYMBOLS:
            lot = item.get("lotSizeFilter", {}) or {}
            pricef = item.get("priceFilter", {}) or {}
            out[sym] = {
                "min_qty":  float(lot.get("minOrderQty", 0.0)),
                "qty_step": float(lot.get("qtyStep", 1.0)),
                "min_amt":  float(item.get("minOrderAmt", 10.0)),
                "tick":     float(pricef.get("tickSize", 0.0001))
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
    global _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    if _LIMITS_MEM is not None:
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    cached = _limits_from_redis()
    if cached:
        _LIMITS_MEM, _LIMITS_OK = cached, True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from Redis cache")
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    try:
        limits = _load_symbol_limits_from_api()
        _limits_to_redis(limits)
        _LIMITS_MEM, _LIMITS_OK = limits, True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from API and cached")
    except Exception as e:
        _LIMITS_MEM, _LIMITS_OK = {}, False
        _BUY_BLOCKED_REASON = f"LIMITS unavailable ({e}); BUY blocked, SELL allowed"
        log_msg(f"⚠️ {_BUY_BLOCKED_REASON}", True)
    return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

# ==================== MARKET DATA ====================
def get_kline(sym, interval="1", limit=200):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_orderbook_top(sym):
    r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
    # v5 возвращает {"a":[["price","size"]...], "b":[["price","size"]...]}
    a = r["result"].get("a", [])
    b = r["result"].get("b", [])
    best_ask = float(a[0][0]) if a else None
    best_bid = float(b[0][0]) if b else None
    return best_bid, best_ask

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== ROUNDING / QTY ====================
def adjust_qty(qty, step):
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def round_down_price(p, tick):
    if tick <= 0:
        return p
    d = Decimal(str(p)); t = Decimal(str(tick))
    return float((d // t) * t)

def round_up_price(p, tick):
    if tick <= 0:
        return p
    d = Decimal(str(p)); t = Decimal(str(tick))
    return float(((d + t - Decimal("0.0000000001")) // t) * t)

def get_qty(sym, price, usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, limits[sym]["qty_step"])
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS (1ТФ) ====================
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, ""
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

    last = df.iloc[-1]
    prev = df.iloc[-2]

    two_of_three_buy = ((last["ema9"] > last["ema21"]) +
                        (last["rsi"] > 48) +
                        (last["macd"] > last["macd_signal"])) >= 2
    ema_cross_up   = (prev["ema9"] <= prev["ema21"]) and (last["ema9"] > last["ema21"]) and (last["rsi"] > 45)

    two_of_three_sell = ((last["ema9"] < last["ema21"]) +
                         (last["rsi"] < 50) +
                         (last["macd"] < last["macd_signal"])) >= 2
    ema_cross_down = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"]) and (last["rsi"] < 55)

    info = (f"EMA9={last['ema9']:.6f},EMA21={last['ema21']:.6f},RSI={last['rsi']:.2f},"
            f"MACD={last['macd']:.6f},SIG={last['macd_signal']:.6f},"
            f"XUP={int(ema_cross_up)},XDN={int(ema_cross_down)}")

    if two_of_three_buy or ema_cross_up:
        return "buy", float(last["atr"]), info
    if two_of_three_sell or ema_cross_down:
        return "sell", float(last["atr"]), info
    return "none", float(last["atr"]), info

# ==================== MTF агрегатор ====================
def multi_signal(sym):
    votes, atrs, parts = [], [], []
    for itv in INTERVALS:
        df = get_kline(sym, interval=itv, limit=200)
        if df.empty or len(df) < 50:
            continue
        sig, atr, _ = signal(df)
        votes.append(sig); atrs.append(float(atr)); parts.append(f"{itv}m:{sig}")
    if not votes:
        return "none", 0.0, "no data"
    buy_cnt  = sum(v == "buy" for v in votes)
    sell_cnt = sum(v == "sell" for v in votes)
    if VOTE_MODE == "all":
        final_sig = "buy" if buy_cnt == len(votes) else ("sell" if sell_cnt == len(votes) else "none")
    elif VOTE_MODE == "any":
        final_sig = "buy" if buy_cnt > 0 else ("sell" if sell_cnt > 0 else "none")
    else:
        final_sig = "buy" if buy_cnt > sell_cnt else ("sell" if sell_cnt > buy_cnt else "none")

    if ATR_SOURCE == "max":
        atr_for_tp = max(atrs)
    elif ATR_SOURCE == "median":
        atr_for_tp = float(pd.Series(atrs).median())
    else:
        atr_for_tp = float(pd.Series(atrs).mean())

    return final_sig, float(atr_for_tp), " | ".join(parts)

# ==================== STRATEGY HELPERS ====================
def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

def base_multiplier(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.01:   return 1.5
    if pct < 0.02:   return 1.8
    return 2.2

def required_profit_floor(atr, price):
    # минимум прибыли в $ для допуска к сделке (но не ниже MIN_NET_PROFIT)
    pct = atr / price if price > 0 else 0
    raw = 0.6 if pct < 0.004 else (0.8 if pct < 0.008 else 1.2)
    return max(MIN_NET_PROFIT, raw)

# ==================== ORDERS (maker) ====================
def place_limit_postonly(sym, side, price, qty):
    return api_call(
        session.place_order,
        category="spot",
        symbol=sym,
        side=side,
        orderType="Limit",
        price=str(price),
        qty=str(qty),
        timeInForce="PostOnly"  # гарантирует maker; если пересекается — ордер отклонят
    )["result"]["orderId"]

def cancel_order(sym, order_id):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
    except Exception as e:
        logging.warning(f"{sym} cancel_order {order_id} failed: {e}")

def get_open_orders(sym):
    r = api_call(session.get_open_orders, category="spot", symbol=sym)
    return r["result"]["list"]

def get_order_status(sym, order_id):
    # если нет в открытых — проверим историю
    open_list = get_open_orders(sym)
    for it in open_list:
        if it.get("orderId") == order_id:
            return "New", float(it.get("price", 0)), float(it.get("qty", 0)), 0.0
    # история
    try:
        h = api_call(session.get_order_history, category="spot", symbol=sym, orderId=order_id)
        lst = h["result"]["list"]
        if lst:
            it = lst[0]
            st = it.get("orderStatus", "")
            avg = float(it.get("avgPrice", 0) or 0.0)
            cum = float(it.get("cumExecQty", 0) or 0.0)
            return st, avg, cum, float(it.get("cumExecValue", 0) or 0.0)
    except Exception as e:
        logging.warning(f"{sym} get_order_history failed: {e}")
    return "Unknown", 0.0, 0.0, 0.0

# ==================== TRADES & LOGS ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE ====================
def reconcile_positions_on_start():
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()
    lines, total = [], 0.0

    for sym in SYMBOLS:
        df_exec = get_kline(sym, interval=EXEC_PRICE_INTERVAL, limit=200)
        if df_exec.empty: continue
        price = float(df_exec["c"].iloc[-1])
        _, atr, _ = multi_signal(sym)

        st = STATE[sym]
        st["pending"] = None
        step = (limits.get(sym) or {}).get("qty_step", 1.0)
        min_amt = (limits.get(sym) or {}).get("min_amt", 10.0)

        bal = adjust_qty(get_coin_balance_from(by, sym), step)
        if bal * price >= min_amt and not st["positions"]:
            # создаём синтетическую позицию
            mul = base_multiplier(atr, price)
            tp = price + mul * atr
            st["positions"] = [{
                "buy_price": price, "qty": bal, "tp": tp,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            lines.append(f"- {sym}: синтетическая позиция qty={bal}")
        total += bal * price

    save_state()
    log_msg("🚀 Бот запущен (восстановление)\n" + "\n".join(lines) + f"\n📊 Номинал: ${total:.2f}", True)

# ==================== MAIN ====================
def trade():
    global cycle_count, LAST_REPORT_DATE
    limits, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        df_exec = get_kline(sym, interval=EXEC_PRICE_INTERVAL, limit=200)
        if df_exec.empty: continue
        price = float(df_exec["c"].iloc[-1])

        sig, atr, info = multi_signal(sym)
        coin_bal = get_coin_balance_from(by, sym)
        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, bal_val={coin_bal*price:.2f}, pos={len(st['positions'])}, {info}")

        # ===== 1) Обновление/переустановка активного лимит-ордера =====
        if st["pending"]:
            pend = st["pending"]
            order_id = pend["id"]
            status, avg, filled_qty, _ = get_order_status(sym, order_id)

            # если ордер исчез (Filled/Cancelled)
            if status not in ("New", "Created", "PartiallyFilled"):
                # FILLED на BUY → добавляем позицию по avg
                if pend["type"] == "entry" and status in ("Filled", "PartiallyFilled", "Deactivated", "Rejected"):  # запишем по факту
                    if filled_qty > 0:
                        bprice = avg if avg > 0 else pend["price"]
                        st["positions"] = [{
                            "buy_price": bprice,
                            "qty": filled_qty,
                            "tp": pend.get("tp_target", bprice * 1.01),
                            "timestamp": datetime.datetime.now().isoformat()
                        }]
                        log_msg(f"{sym}: BUY filled qty={filled_qty} @ {bprice:.6f}", True)
                # FILLED на SELL → фиксируем PnL
                elif pend["type"] in ("takeprofit", "stop") and status in ("Filled", "PartiallyFilled"):
                    sell_price = avg if avg > 0 else pend["price"]
                    qty = filled_qty if filled_qty > 0 else pend["qty"]
                    # ищем позицию
                    if st["positions"]:
                        b, q = float(st["positions"][0]["buy_price"]), float(st["positions"][0]["qty"])
                        use_qty = min(qty, q)
                        buy_comm  = b * use_qty * MAKER_BUY_FEE  # вход был maker
                        sell_fee  = sell_price * use_qty * (MAKER_SELL_FEE if pend["type"]=="takeprofit" else TAKER_SELL_FEE)
                        pnl = (sell_price - b) * use_qty - (buy_comm + sell_fee)
                        st["pnl"] += pnl
                        st["positions"] = []  # закрыли целиком
                        log_trade(sym, "SELL (limit)" if pend["type"]=="takeprofit" else "SL MARKET", sell_price, use_qty, pnl, pend["type"])
                st["pending"] = None
                save_state()
            else:
                # активен → проверим возраст и переустановим
                age = (datetime.datetime.now() - datetime.datetime.fromisoformat(pend["ts"])).total_seconds()
                if age >= REQUOTE_SEC:
                    cancel_order(sym, order_id)
                    st["pending"] = None

        # ===== 2) Управление открытой позицией: TP/SL =====
        # (если нет активного лимит-ордера)
        if not st["pending"] and st["positions"]:
            b = float(st["positions"][0]["buy_price"])
            q = float(st["positions"][0]["qty"])
            # SL условие (с выходом в маркет)
            buy_comm = b * q * MAKER_BUY_FEE
            sell_comm_now = price * q * TAKER_SELL_FEE
            pnl_now = (price - b) * q - (buy_comm + sell_comm_now)
            if price <= b * (1 - STOP_LOSS_PCT) and abs(pnl_now) >= MIN_NET_PROFIT:
                # прямой маркет (тейкер) для гарантированного выхода
                try:
                    api_call(session.place_order, category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl_now, "stop-loss")
                    st["pnl"] += pnl_now
                    st["positions"] = []
                    st["last_stop_time"] = datetime.datetime.now().isoformat()
                except Exception as e:
                    log_msg(f"{sym}: SL sell failed {e}", True)
                save_state()
                continue

            # TP: выставляем лимит‑maker по цене, дающей >= MIN_NET_PROFIT
            limits, _, _ = get_limits()
            tick = (limits.get(sym) or {}).get("tick", 0.0001)
            best_bid, best_ask = get_orderbook_top(sym)
            if best_bid is None or best_ask is None:
                continue

            # расчёт минимально нужной дельты для прибыли >= MIN_NET_PROFIT
            required_pnl = MIN_NET_PROFIT
            required_delta = (buy_comm + (price * q * MAKER_SELL_FEE) + required_pnl) / q  # первичная оценка
            # базовый TP от ATR (на всякий случай, если выше)
            _, atr, _ = multi_signal(sym)
            base_mult = base_multiplier(atr, price)
            base_delta = base_mult * atr
            delta = max(required_delta, base_delta)
            tp_target = b + delta

            # maker‑цена для продажи: не ниже tp_target и не ближе чем 0.05% от ask
            min_maker = best_ask * (1 + MAKER_OFFSET_BP / 10000.0)
            raw_sell = max(tp_target, min_maker)
            lmt_price = round_up_price(raw_sell, tick)
            if lmt_price <= best_ask:  # страховка
                lmt_price = round_up_price(best_ask + tick, tick)

            # проверим ожидаемую чистую прибыль на этой цене
            sell_fee = lmt_price * q * MAKER_SELL_FEE
            est_pnl = (lmt_price - b) * q - (buy_comm + sell_fee)
            if est_pnl >= MIN_NET_PROFIT:
                try:
                    oid = place_limit_postonly(sym, "Sell", lmt_price, q)
                    st["pending"] = {
                        "id": oid, "side": "Sell", "price": lmt_price, "qty": q,
                        "type": "takeprofit", "ts": datetime.datetime.now().isoformat(),
                        "required_pnl": MIN_NET_PROFIT, "tp_target": tp_target
                    }
                    logging.info(f"[{sym}] PLACED TP SELL postOnly price={lmt_price:.6f}, est_pnl={est_pnl:.4f}")
                    save_state()
                except Exception as e:
                    log_msg(f"{sym}: place TP SELL failed {e}", True)

        # ===== 3) Новый вход BUY =====
        if not st["pending"] and not st["positions"] and sig == "buy":
            # защита после недавнего SL
            if st.get("last_stop_time"):
                hrs = hours_since(st["last_stop_time"])
                if hrs < NO_REBUY_AFTER_SL_HRS:
                    if should_log_skip(sym, "stop_buy"):
                        log_skip(sym, f"Пропуск BUY — прошло {hrs:.1f}ч после SL")
                    continue

            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                continue

            limits, _, _ = get_limits()
            if avail < (limits.get(sym) or {}).get("min_amt", 10.0):
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — мало USDT")
                continue

            qty = get_qty(sym, price, avail)
            if qty <= 0:
                if should_log_skip(sym, "skip_qty"):
                    l = limits.get(sym, {})
                    log_skip(sym, f"Пропуск BUY — qty=0 (step={l.get('qty_step')}, min_qty={l.get('min_qty')}, min_amt={l.get('min_amt')})")
                continue

            # ATR и базовый TP
            _, atr, _ = multi_signal(sym)
            base_mult = base_multiplier(atr, price)
            base_delta = base_mult * atr

            # комиссии maker
            buy_comm = price * qty * MAKER_BUY_FEE
            # для минимальной прибыли ≥ MIN_NET_PROFIT нужна такая дельта по цене:
            sell_comm_est = price * qty * MAKER_SELL_FEE
            required_delta = (buy_comm + sell_comm_est + required_profit_floor(atr, price)) / qty

            # если базовый ATR‑TP ближе — подтянем множитель, но ограничим потолком
            if base_delta < required_delta and atr > 0:
                adj_mult = min((required_delta / atr) * 1.05, MAX_TP_MULTIPLIER)
            else:
                adj_mult = base_mult
            tp_target = price + adj_mult * atr

            # maker входная цена: ниже best_bid на отступ
            best_bid, best_ask = get_orderbook_top(sym)
            if best_bid is None or best_ask is None:
                continue
            tick = (limits.get(sym) or {}).get("tick", 0.0001)
            max_maker_buy = best_bid * (1 - MAKER_OFFSET_BP / 10000.0)
            raw_buy = min(price, max_maker_buy)
            lmt_buy = round_down_price(raw_buy, tick)
            if lmt_buy >= best_bid:
                lmt_buy = round_down_price(best_bid - tick, tick)

            # оценим чистую прибыль при продаже по tp_target (и комиссию на выходе maker)
            sell_fee_tp = tp_target * qty * MAKER_SELL_FEE
            est_pnl = (tp_target - lmt_buy) * qty - ((lmt_buy * qty * MAKER_BUY_FEE) + sell_fee_tp)

            logging.info(
                f"[{sym}] atr={atr:.8f}, base_mult={base_mult:.2f}, adj_mult={adj_mult:.2f}, "
                f"required_delta={required_delta:.8f}, tp_target={tp_target:.6f}, buy_lmt={lmt_buy:.6f}, est_pnl={est_pnl:.4f}"
            )

            if est_pnl >= required_profit_floor(atr, price):
                try:
                    oid = place_limit_postonly(sym, "Buy", lmt_buy, qty)
                    st["pending"] = {
                        "id": oid, "side":"Buy", "price": lmt_buy, "qty": qty,
                        "type":"entry", "ts": datetime.datetime.now().isoformat(),
                        "required_pnl": required_profit_floor(atr, price), "tp_target": tp_target
                    }
                    logging.info(f"[{sym}] PLACED BUY postOnly price={lmt_buy:.6f}")
                    save_state()
                except Exception as e:
                    log_msg(f"{sym}: place BUY failed {e}", True)
            else:
                if should_log_skip(sym, "skip_low_profit"):
                    log_skip(sym, f"Пропуск BUY — ожидаемый PnL мал (получили {est_pnl:.2f})")

    cycle_count += 1

    # Ежедневный отчёт
    now = datetime.datetime.now()
    global LAST_REPORT_DATE
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== DAILY REPORT ====================
def send_daily_report():
    lines = [f"📊 Ежедневный отчёт ({VERSION}):"]
    total_pnl = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}")
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        pend = st["pending"]
        pend_txt = f" | лимит {pend['type']} {pend['side']} {pend['qty']} @ {pend['price']:.6f}" if pend else ""
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}{pend_txt}")
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
