# bot.py
# -*- coding: utf-8 -*-

import os, time, logging, datetime, requests, json, traceback
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
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ==================== CONFIG ====================
TG_VERBOSE = True
TG_DEDUP_WINDOW = 180  # сек

SYMBOLS = [
    "DOGEUSDT", "XRPUSDT",
    "SOLUSDT", "ARBUSDT",
    "LINKUSDT", "TONUSDT",
]

RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 120.0
MIN_NET_PROFIT    = 1.0         # минимум $1 чистыми
STOP_LOSS_PCT     = 0.008       # 0.8%

# Комиссии (maker)
MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018

# Перекат post-only
ROLL_LIMIT_SECONDS = 45

# Redis кэш лимитов
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

# Прецизионная математика
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
_last_tg = {}
_missing_limit_logged = set()

# ==================== TG ====================
def send_tg(msg: str):
    if not (TG_VERBOSE and TG_TOKEN and CHAT_ID):
        return
    now = time.time()
    last = _last_tg.get(msg, 0)
    if now - last < TG_DEDUP_WINDOW:
        return
    _last_tg[msg] = now
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

# ---------- ROBUST STATE ----------
def _default_state_for(sym: str):
    return {
        "positions": [],
        "pnl": 0.0,
        "last_stop_time": "",
        "open_buy": None
    }

def ensure_state_consistency():
    for sym in SYMBOLS:
        st = STATE.setdefault(sym, {})
        defaults = _default_state_for(sym)
        for k, v in defaults.items():
            if k not in st:
                st[k] = v
        if not isinstance(st.get("positions"), list):
            st["positions"] = []
        if st.get("open_buy") is not None and not isinstance(st.get("open_buy"), dict):
            st["open_buy"] = None

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    try:
        STATE = json.loads(raw) if raw else {}
    except Exception:
        STATE = {}
    ensure_state_consistency()
    log_msg("✅ Состояние загружено из Redis" if raw else "ℹ Начинаем с чистого состояния", True)

# ==================== API HELPERS ====================
def api_call(fn, *args, **kwargs):
    logging.info(f"Request → {fn.__name__.upper()}: {kwargs}")
    wait = 0.35
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            try:
                d = getattr(e, "args", [{}])[0]
                if isinstance(d, dict):
                    rc = d.get("retCode"); rm = d.get("retMsg")
                    if rc is not None:
                        msg = f"{msg} (retCode={rc}, retMsg={rm})"
            except Exception:
                pass
            logging.warning(f"API retry {fn.__name__} attempt={attempt+1} wait={wait:.2f}s error={msg}")
            time.sleep(wait)
            wait = min(wait * 2.0, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

def get_open_orders_map(sym, order_id=None):
    try:
        r = session.get_open_orders(category="spot", symbol=sym, orderId=order_id)
        arr = r["result"]["list"]
        return {item["orderId"]: item for item in arr}
    except Exception:
        return {}

# ==================== LIMITS ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    limits = {}
    for item in lst:
        sym = item["symbol"]
        if sym not in SYMBOLS:
            continue
        lot = item.get("lotSizeFilter", {}) or {}
        pf  = item.get("priceFilter", {}) or {}
        limits[sym] = {
            "min_qty":  float(lot.get("minOrderQty", 0.0)),
            "qty_step": float(lot.get("qtyStep", 1.0)),
            "min_amt":  float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(pf.get("tickSize", 0.00000001)),
        }
    return limits

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

def fetch_limits_for_symbol(sym: str):
    """Точечная догрузка лимитов для конкретного тикера (fallback)."""
    try:
        r = api_call(session.get_instruments_info, category="spot", symbol=sym)
        lst = r["result"]["list"] or []
        if not lst:
            return None
        item = lst[0]
        lot = item.get("lotSizeFilter", {}) or {}
        pf  = item.get("priceFilter", {}) or {}
        return {
            "min_qty":  float(lot.get("minOrderQty", 0.0)),
            "qty_step": float(lot.get("qtyStep", 1.0)),
            "min_amt":  float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(pf.get("tickSize", 0.00000001)),
        }
    except Exception:
        return None

def ensure_symbol_limits(sym: str):
    """Гарантированно вернуть лимиты по символу, при необходимости — догрузить и обновить кеш."""
    global _LIMITS_MEM
    limits, ok, _ = get_limits()
    if not ok:
        return None
    if sym in limits:
        return limits[sym]
    lim = fetch_limits_for_symbol(sym)
    if lim:
        limits[sym] = lim
        _LIMITS_MEM = limits
        _limits_to_redis(limits)
        return lim
    if sym not in _missing_limit_logged:
        _missing_limit_logged.add(sym)
        log_msg(f"⛔️ Нет лимитов для {sym} (пара пропущена до обновления справочника).", True)
    return None

# ==================== MKT DATA & BALANCES ====================
def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=120)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_top_of_book(sym):
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        res = r["result"]
        bids = res.get("b") or res.get("bids") or res.get("B") or []
        asks = res.get("a") or res.get("asks") or res.get("A") or []
        bid = float(bids[0][0]) if bids and bids[0] else None
        ask = float(asks[0][0]) if asks and asks[0] else None
        return bid, ask
    except Exception as e:
        logging.info(f"orderbook failed {sym}: {e}")
        return None, None

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== QTY / ROUNDING ====================
def adjust_qty(qty, step):
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def adjust_price(price, tick, mode="nearest"):
    if tick <= 0:
        return price
    q = Decimal(str(price)) / Decimal(str(tick))
    if mode == "down":
        q = q.to_integral_value(rounding="ROUND_FLOOR")
    elif mode == "up":
        q = q.to_integral_value(rounding="ROUND_CEILING")
    else:
        q = q.to_integral_value(rounding="ROUND_HALF_UP")
    return float(Decimal(str(tick)) * q)

def normalize_sell_qty(sym: str, qty: float, price: float):
    lim = ensure_symbol_limits(sym)
    if not lim:
        return None
    step    = lim["qty_step"]
    min_qty = lim["min_qty"]
    min_amt = lim["min_amt"]
    q = Decimal(str(qty)); s = Decimal(str(step))
    adj = float((q // s) * s)
    if adj < max(min_qty, 0.0) or adj * price < min_amt:
        return None
    return adj

def get_qty(sym, price, usdt):
    lim = ensure_symbol_limits(sym)
    if not lim:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, lim["qty_step"])
    if q < lim["min_qty"] or q * price < lim["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS ====================
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, {}
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    macd  = MACD(close=df["c"])
    macd_line, macd_sig = macd.macd(), macd.macd_signal()
    atr5  = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range()
    atr15 = AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range()

    last = len(df) - 1
    ema9v, ema21v = ema9.iloc[last], ema21.iloc[last]
    rsi, macdv, macds = rsi9.iloc[last], macd_line.iloc[last], macd_sig.iloc[last]
    atr_pct = (atr5.iloc[last] / df["c"].iloc[last]) if df["c"].iloc[last] > 0 else 0.0

    rsi_ok  = (rsi > 49.0)
    ema_ok  = (ema9v > ema21v * 0.999)   # допуск 0.1%
    macd_ok = (macdv > macds)

    conds = int(ema_ok) + int(rsi_ok) + int(macd_ok)
    info = {"EMA9": float(ema9v), "EMA21": float(ema21v),
            "RSI": float(rsi), "MACD": float(macdv), "SIG": float(macds),
            "ATR%": float(atr_pct)}
    if conds >= 2:
        return "buy", float(atr15.iloc[last]), info
    return "none", float(atr15.iloc[last]), info

# ==================== TP MULT LOGIC ====================
def base_tp_mult(atr, price, atr5_pct_hint):
    pct = atr / price if price > 0 else 0
    vol = max(pct, atr5_pct_hint)
    if vol < 0.002:   return 1.55
    elif vol < 0.005: return 1.30
    else:             return 1.10

def dynamic_min_profit(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.004: return 0.6
    if pct < 0.008: return 0.8
    return 1.2

def est_pnl_rr(post_price, tp_price, qty):
    buy_comm  = post_price * qty * MAKER_BUY_FEE
    sell_comm = tp_price  * qty * MAKER_SELL_FEE
    pnl = (tp_price - post_price) * qty - (buy_comm + sell_comm)
    risk = post_price * STOP_LOSS_PCT
    reward = max(tp_price - post_price, 0.0)
    rr = (reward / risk) if risk > 0 else 0.0
    return pnl, rr

def find_tp_price(post_price, atr15, tick, qty, required_min_profit, min_rr, start_mult, max_mult=3.5, step=0.15):
    mult = start_mult
    best = None
    while mult <= max_mult:
        raw_tp = post_price + mult * atr15
        tp_price = adjust_price(raw_tp, tick, mode="up")
        pnl, rr = est_pnl_rr(post_price, tp_price, qty)
        best = (tp_price, pnl, rr, mult)
        if pnl >= required_min_profit and rr >= min_rr:
            return best
        mult = round(mult + step, 4)
    return best

# ==================== BALANCE HELPERS ====================
def _free_usdt_buffered():
    usdt, _ = get_balances_cache()
    return max(0.0, usdt - RESERVE_BALANCE - 1.0)  # небольшой буфер

def _downsize_qty_to_balance(sym, price, qty):
    lim = ensure_symbol_limits(sym)
    if not lim:
        return 0.0
    free_usdt = _free_usdt_buffered()
    need = price * qty * (1 + MAKER_BUY_FEE + 0.001)  # +0.1% запас
    if need <= free_usdt:
        return qty
    max_qty = free_usdt / (price * (1 + MAKER_BUY_FEE + 0.001))
    adj = adjust_qty(max_qty, lim["qty_step"])
    if adj < lim["min_qty"] or adj * price < lim["min_amt"]:
        return 0.0
    return adj

# ==================== ORDER HELPERS ====================
def place_tp_postonly(sym, qty, price):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Sell", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    log_msg(f"{sym}: TP postOnly placed id={oid} price={price} qty={qty}", True)
    return oid

def try_cancel_order(sym, order_id):
    m = get_open_orders_map(sym, order_id=order_id)
    if order_id not in m:
        logging.info(f"{sym}: cancel skip — order {order_id} not open")
        return
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
        log_msg(f"{sym}: order {order_id} canceled", True)
    except Exception as e:
        log_msg(f"{sym}: cancel failed {order_id}: {e}", True)

def place_postonly_buy_safe(sym, qty, ref_bid, tick, max_tries=3):
    """
    Пытаемся поставить PostOnly BUY. Если ошибка (taker/баланс/тик) —
    даунсайзим qty по реальному свободному балансу и/или сдвигаем цену ещё ниже.
    """
    err_last = None
    price = adjust_price(ref_bid, tick, mode="down")
    for i in range(max_tries):
        q_try = _downsize_qty_to_balance(sym, price, qty)
        if q_try <= 0:
            raise RuntimeError(f"{sym}: недостаточно свободного USDT для постановки ордера (после даунсайза).")
        try:
            r = api_call(session.place_order, category="spot", symbol=sym,
                         side="Buy", orderType="Limit", qty=str(q_try),
                         price=str(price), timeInForce="PostOnly")
            oid = r["result"]["orderId"]
            STATE[sym]["open_buy"] = {"orderId": oid, "qty": q_try, "price": price, "ts": time.time()}
            save_state()
            log_msg(f"{sym}: postOnly BUY placed id={oid} price={price} qty={q_try}", True)
            return oid, price
        except Exception as e:
            err_last = str(e)
            logging.info(f"{sym}: PostOnly BUY attempt {i+1}/{max_tries} failed: {err_last}")
            # если именно 170131 (Insufficient balance) — дополнительно подрежем размер
            if "170131" in err_last or "Insufficient balance" in err_last:
                qty = max(adjust_qty(qty * 0.7, tick), q_try)  # агрессивнее срежем
            price = adjust_price(price - tick, tick, mode="down")
            time.sleep(0.35 * (i + 1))
    raise RuntimeError(f"{sym}: place_order failed after {max_tries} tries: {err_last}")

# ==================== TRADES & ЛОГИ ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.8f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.8f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE ====================
def reconcile_positions_on_start():
    usdt, by = get_balances_cache()
    total_notional = 0.0
    lines = []
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance_from(by, sym)
        if bal > 0:
            lim = ensure_symbol_limits(sym)
            if lim:
                bal_adj = adjust_qty(bal, lim["qty_step"])
                if bal_adj < lim["min_qty"] or bal_adj * price < lim["min_amt"]:
                    lines.append(f"- {sym}: пыль на балансе ({bal:.8f}), позиция не создана")
                    continue
                bal = bal_adj
            atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
            mul  = base_tp_mult(
                atr=AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range().iloc[-1],
                price=price,
                atr5_pct_hint=(atr5/price if price>0 else 0)
            )
            tick = (lim["tick_size"] if lim else 0.00000001)
            tp   = adjust_price(price + mul * atr5, tick, mode="up")
            STATE[sym]["positions"] = [{
                "buy_price": price, "qty": bal, "tp": tp,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            lines.append(f"- {sym}: синхр. позиция qty={bal} по ~{price:.6f}")
            total_notional += bal * price
        else:
            lines.append(f"- {sym}: позиций нет, баланса мало")
    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" + "\n".join(lines) + f"\n📊 Номинал по монетам: ${total_notional:.2f}", True)

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

# ==================== MAIN LOGIC ====================
LAST_REPORT_DATE = None

def trade():
    global LAST_REPORT_DATE
    _, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances_cache()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / max(1, len(SYMBOLS))
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE.get(sym) or _default_state_for(sym)
        STATE[sym] = st
        df = get_kline(sym)
        if df.empty:
            continue

        lim = ensure_symbol_limits(sym)
        if not lim:
            continue
        tick = lim["tick_size"]

        sig, atr15, info = signal(df)
        price = df["c"].iloc[-1]
        bid, ask = get_top_of_book(sym)
        atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
        atr5_pct = (atr5 / price) if price > 0 else 0.0
        base_mult = base_tp_mult(atr15, price, atr5_pct)
        bal_coin = get_coin_balance_from(by, sym)
        bal_val  = bal_coin * price
        pos_open = len(st.get("positions", [])) > 0

        logging.info(
            f"[{sym}] sig={sig}, price={price:.6f}, bal_val={bal_val:.2f}, pos={'1' if pos_open else '0'} | "
            f"bid={bid if bid else '?'} ask={ask if ask else '?'} | "
            f"EMA9={info.get('EMA9', 0):.6f} EMA21={info.get('EMA21', 0):.6f} RSI={info.get('RSI', 0):.2f} "
            f"MACD={info.get('MACD', 0):.6f} SIG={info.get('SIG', 0):.6f} | ATR(5/15m)={atr5_pct*100:.2f}% | tp_mult≈{base_mult:.2f}"
        )

        # 1) сопровождение открытого BUY (перекат)
        ob = st.get("open_buy")
        if ob:
            oid = ob["orderId"]; ots = ob["ts"]; oprice = ob["price"]; oqty = ob["qty"]
            still_open = oid in get_open_orders_map(sym, order_id=oid)
            if still_open and time.time() - ots >= ROLL_LIMIT_SECONDS:
                nbid, _ = get_top_of_book(sym)
                if nbid:
                    new_price = adjust_price(nbid, tick, mode="down")
                    if new_price != oprice:
                        try:
                            try_cancel_order(sym, oid)
                            place_postonly_buy_safe(sym, oqty, nbid, tick)
                            logging.info(f"{sym}: rolled BUY {oprice} → {new_price}")
                        except Exception as e:
                            log_msg(f"{sym}: roll place failed — {e}. Оставляем без изменений, попробуем позже.", True)
            elif not still_open:
                # ордер исполнен — ставим TP
                st["open_buy"] = None
                by_now = get_balances_cache()[1]
                coin_bal = get_coin_balance_from(by_now, sym)
                qty = coin_bal if coin_bal > 0 else oqty
                entry_price = oprice
                required  = max(MIN_NET_PROFIT, dynamic_min_profit(atr15, entry_price))
                tp_price, pnl, rr, used_mult = find_tp_price(
                    post_price=entry_price, atr15=atr15, tick=tick, qty=qty,
                    required_min_profit=required, min_rr=1.0, start_mult=base_mult
                )
                st["positions"] = [{
                    "buy_price": entry_price,
                    "qty": qty,
                    "tp": tp_price,
                    "timestamp": datetime.datetime.now().isoformat()
                }]
                save_state()
                try:
                    place_tp_postonly(sym, qty, tp_price)
                except Exception as e:
                    log_msg(f"{sym}: place TP failed: {e} | price={tp_price}, tick={tick}", True)
                log_msg(f"✅ BUY filled {sym} @~{entry_price:.8f}, qty≈{qty}. TP={tp_price} (mult≈{used_mult:.2f}, est_pnl≈{pnl:.2f}, RR≈{rr:.2f})", True)

        # 2) SL (TP уже стоит)
        if st.get("positions"):
            pos = st["positions"][0]
            b, q = pos["buy_price"], pos["qty"]
            dust_qty = normalize_sell_qty(sym, q, price)
            if dust_qty is None:
                if should_log_skip(sym, "dust_skip", interval=1):
                    log_msg(f"{sym}: DUST — qty={q:.8f} @ {price:.8f} ниже лимитов биржи. Позиция снята без продажи.", True)
                st["positions"] = []
                save_state()
            else:
                if price <= b * (1 - STOP_LOSS_PCT):
                    try:
                        api_call(session.place_order, category="spot", symbol=sym,
                                 side="Sell", orderType="Market", qty=str(dust_qty))
                        pnl, _ = est_pnl_rr(b, price, dust_qty)
                        st["pnl"] = st.get("pnl", 0.0) + pnl
                        log_trade(sym, "STOP SELL", price, dust_qty, pnl, "stop-loss")
                        st["positions"] = []
                    except Exception as e:
                        log_msg(f"{sym}: STOP SELL failed: {e}", True)

        # 3) новые входы
        if (sig == "buy") and not st.get("positions") and not st.get("open_buy"):
            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                    send_tg(f"{sym}: ⛔️ Покупки блокированы: {buy_blocked_reason}")
                continue
            if avail < lim["min_amt"]:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")
                continue
            if not bid:
                logging.info(f"{sym}: DEBUG_SKIP | no orderbook for BUY")
                continue

            qty = get_qty(sym, price, per_sym)
            if qty <= 0:
                logging.info(f"{sym}: DEBUG_SKIP | qty=0 price={price:.8f} step={lim.get('qty_step')} min_qty={lim.get('min_qty')} min_amt={lim.get('min_amt')}")
                continue

            post_price = adjust_price(bid, lim["tick_size"], mode="down")
            required  = max(MIN_NET_PROFIT, dynamic_min_profit(atr15, post_price))
            tp_price, est_pnl_val, rr_val, used_mult = find_tp_price(
                post_price=post_price, atr15=atr15, tick=lim["tick_size"], qty=qty,
                required_min_profit=required, min_rr=1.0, start_mult=base_mult
            )
            logging.info(f"[{sym}] BUY-check qty={qty}, tp={tp_price:.8f}, mult≈{used_mult:.2f}, "
                         f"ppu={tp_price-post_price:.8f}, est_pnl={est_pnl_val:.2f}, "
                         f"required={required:.2f}, RR={rr_val:.2f}, tick={lim['tick_size']}")
            if est_pnl_val >= required and rr_val >= 1.0:
                try:
                    place_postonly_buy_safe(sym, qty, bid, lim["tick_size"])
                    log_msg(f"BUY (maker) {sym} @ {post_price:.8f}, qty={qty}, плановый TP={tp_price:.8f} (mult≈{used_mult:.2f}) — TP выставится после fill", True)
                    avail_local = qty * post_price
                    avail = max(0.0, avail - avail_local)
                except Exception as e:
                    log_msg(f"{sym}: BUY failed: {e}", True)
            else:
                logging.info(f"{sym}: DEBUG_SKIP | даже с динамическим TP PnL {est_pnl_val:.2f} < {required:.2f} или RR<1.0")

    # ежедневный отчёт 22:30+
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    try:
        _ = get_limits()
    except:
        pass
    reconcile_positions_on_start()
    log_msg("🟢 Бот работает. Динамический TP (≥$1 и RR≥1.0), maker‑режим. Пары: DOGE/XRP/SOL/ARB/LINK/TON", True)
    while True:
        try:
            trade()
        except Exception as e:
            tb = traceback.format_exc()
            log_msg(f"Global error: {e}\n{tb}", True)
        time.sleep(60)
