# bot.py
# -*- coding: utf-8 -*-
import os, time, math, logging, datetime, requests, json, traceback
from decimal import Decimal, getcontext
from dataclasses import dataclass, field
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
TG_DEDUP_WINDOW = 180  # сек: защита от одинаковых повторов

SYMBOLS = ["DOGEUSDT", "XRPUSDT"]

RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 120.0
MIN_NET_PROFIT    = 1.0
STOP_LOSS_PCT     = 0.008  # 0.8%

# Комиссии (maker) BUY/SELL
MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018

# Перекат постонли-лимитника каждые N сек
ROLL_LIMIT_SECONDS = 45

# Redis кэш лимитов (bump с tick_size)
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

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp, timestamp}]
            "pnl": 0.0,
            "last_stop_time": "",
            "open_buy": None      # {"orderId", "qty", "price", "ts"}
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

# ==================== API HELPERS (подробный лог) ====================
def api_call(fn, *args, **kwargs):
    # подробный лог запроса
    logging.info(f"Request → {fn.__name__.upper()}: {kwargs}")
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

# ==================== MKT DATA & BALANCES ====================
def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=120)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_top_of_book(sym):
    """Bybit v5 orderbook: get_orderbook(category='spot', symbol=sym, limit=1)"""
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
    """Квантуем qty по шагу и фильтруем пыль (ниже лимитов биржи)."""
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return None
    step    = limits[sym]["qty_step"]
    min_qty = limits[sym]["min_qty"]
    min_amt = limits[sym]["min_amt"]
    q = Decimal(str(qty)); s = Decimal(str(step))
    adj = float((q // s) * s)
    if adj < max(min_qty, 0.0) or adj * price < min_amt:
        return None
    return adj

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

    conds = int(ema9v > ema21v) + int(rsi > 50) + int(macdv > macds)
    info = {
        "EMA9": float(ema9v), "EMA21": float(ema21v),
        "RSI": float(rsi), "MACD": float(macdv), "SIG": float(macds),
        "ATR%": float(atr_pct)
    }
    if conds >= 2:  # 2 из 3
        return "buy", float(atr15.iloc[last]), info
    return "none", float(atr15.iloc[last]), info

def choose_multiplier(atr, price, atr5_pct_hint):
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

# ==================== TRADES & LOGS ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.8f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.8f},{qty},{pnl:.2f},{info}\n")
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
        price = df["c"].iloc[-1]
        bal = get_coin_balance_from(by, sym)
        if bal > 0:
            # отсекаем пыль, не создаём бессмысленную позицию
            if limits_ok and sym in limits:
                step = limits[sym]["qty_step"]; min_qty = limits[sym]["min_qty"]; min_amt = limits[sym]["min_amt"]
                bal_adj = adjust_qty(bal, step)
                if bal_adj < min_qty or bal_adj * price < min_amt:
                    lines.append(f"- {sym}: пыль на балансе ({bal:.8f}), позиция не создана")
                    continue
                bal = bal_adj

            atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
            mul  = choose_multiplier(
                atr=AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range().iloc[-1],
                price=price,
                atr5_pct_hint=(atr5/price if price>0 else 0)
            )
            tick = limits.get(sym, {}).get("tick_size", 0.00000001) if limits_ok else 0.00000001
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

# ==================== ORDER HELPERS (postOnly + roll) ====================
def place_postonly_buy(sym, qty, price):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Buy", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    STATE[sym]["open_buy"] = {"orderId": oid, "qty": qty, "price": price, "ts": time.time()}
    save_state()
    log_msg(f"{sym}: postOnly BUY placed id={oid} price={price} qty={qty}", True)
    return oid

def cancel_order(sym, order_id):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
        log_msg(f"{sym}: order {order_id} canceled", True)
    except Exception as e:
        log_msg(f"{sym}: cancel failed {order_id}: {e}", True)

def is_order_open(sym, order_id):
    try:
        r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=order_id)
        arr = r["result"]["list"]
        return len(arr) > 0
    except Exception:
        return False

def place_tp_postonly(sym, qty, price):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Sell", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    log_msg(f"{sym}: TP postOnly placed id={oid} price={price} qty={qty}", True)
    return oid

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
    limits, limits_ok, buy_blocked_reason = get_limits()

    usdt, by = get_balances_cache()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / max(1, len(SYMBOLS))
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        df = get_kline(sym)
        if df.empty:
            continue

        sig, atr15, info = signal(df)
        price = df["c"].iloc[-1]
        bid, ask = get_top_of_book(sym)
        tick = limits.get(sym, {}).get("tick_size", 0.00000001) if limits_ok else 0.00000001

        atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
        atr5_pct = (atr5 / price) if price > 0 else 0.0
        tp_mult = choose_multiplier(atr15, price, atr5_pct)

        bal_coin = get_coin_balance_from(by, sym)
        bal_val  = bal_coin * price
        pos_open = len(st["positions"]) > 0

        logging.info(
            f"[{sym}] sig={sig}, price={price:.6f}, bal_val={bal_val:.2f}, pos={'1' if pos_open else '0'} | "
            f"bid={bid if bid else '?'} ask={ask if ask else '?'} | "
            f"EMA9={info.get('EMA9'):.6f} EMA21={info.get('EMA21'):.6f} RSI={info.get('RSI'):.2f} "
            f"MACD={info.get('MACD'):.6f} SIG={info.get('SIG'):.6f} | ATR(5/15m)={atr5_pct*100:.2f}% | tp_mult={tp_mult:.2f}"
        )

        # 1) Сопровождение открытого BUY (перекат)
        ob = st.get("open_buy")
        if ob:
            oid = ob["orderId"]; ots = ob["ts"]; oprice = ob["price"]; oqty = ob["qty"]
            still_open = is_order_open(sym, oid)
            if still_open and time.time() - ots >= ROLL_LIMIT_SECONDS:
                nbid, _ = get_top_of_book(sym)
                if nbid:
                    new_price = adjust_price(nbid, tick, mode="down")
                    if new_price != oprice:
                        cancel_order(sym, oid)
                        place_postonly_buy(sym, oqty, new_price)
                        logging.info(f"{sym}: rolled BUY {oprice} → {new_price}")
            elif not still_open:
                # ордер исполнен — ставим TP
                st["open_buy"] = None
                by_now = get_balances_cache()[1]
                coin_bal = get_coin_balance_from(by_now, sym)
                qty = coin_bal if coin_bal > 0 else oqty
                entry_price = oprice
                tp_price = adjust_price(entry_price + tp_mult * atr15, tick, mode="up")
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
                log_msg(f"✅ BUY filled {sym} @~{entry_price:.8f}, qty≈{qty}. TP={tp_price}", True)

        # 2) Продажи по TP/SL, если позиция есть
        if st["positions"]:
            pos = st["positions"][0]
            b, q = pos["buy_price"], pos["qty"]

            # --- АНТИПЫЛЬ: если остаток не проходит лимиты — снимаем позицию и НЕ спамим ---
            dust_qty = normalize_sell_qty(sym, q, price)
            if dust_qty is None:
                if should_log_skip(sym, "dust_skip", interval=1):
                    log_msg(f"{sym}: DUST — qty={q:.8f} @ {price:.8f} ниже лимитов биржи. Позиция снята без продажи.", True)
                st["positions"] = []
                save_state()
            else:
                # SL
                if price <= b * (1 - STOP_LOSS_PCT):
                    try:
                        api_call(session.place_order, category="spot", symbol=sym,
                                 side="Sell", orderType="Market", qty=str(dust_qty))
                        buy_comm  = b * dust_qty * MAKER_BUY_FEE
                        sell_comm = price * dust_qty * MAKER_SELL_FEE
                        pnl = (price - b) * dust_qty - (buy_comm + sell_comm)
                        st["pnl"] += pnl
                        log_trade(sym, "STOP SELL", price, dust_qty, pnl, "stop-loss")
                        st["positions"] = []
                    except Exception as e:
                        log_msg(f"{sym}: STOP SELL failed: {e}", True)
                # если цена >= tp — лимит уже стоит (postOnly при fill). Ничего не делаем.

        # 3) Новые входы (нет позиции и нет открытого лимитника)
        if (sig == "buy") and not st["positions"] and not st["open_buy"]:
            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                    send_tg(f"{sym}: ⛔️ Покупки блокированы: {buy_blocked_reason}")
                continue
            if avail < limits[sym]["min_amt"]:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")
                continue
            if not bid:
                logging.info(f"{sym}: DEBUG_SKIP | no orderbook for BUY")
                continue

            qty = get_qty(sym, price, per_sym)
            if qty <= 0:
                lim = limits.get(sym, {})
                logging.info(f"{sym}: DEBUG_SKIP | qty=0 price={price:.8f} step={lim.get('qty_step')} min_qty={lim.get('min_qty')} min_amt={lim.get('min_amt')}")
                continue

            # Цена постонли — от bid вниз на шаг
            post_price = adjust_price(bid, limits[sym]["tick_size"], mode="down")

            # TP и RR
            tp_raw   = post_price + tp_mult * atr15
            tp_price = adjust_price(tp_raw, limits[sym]["tick_size"], mode="up")

            buy_comm  = post_price * qty * MAKER_BUY_FEE
            sell_comm = tp_price  * qty * MAKER_SELL_FEE
            est_pnl   = (tp_price - post_price) * qty - (buy_comm + sell_comm)
            required  = max(MIN_NET_PROFIT, dynamic_min_profit(atr15, post_price))

            # Risk/Reward: риск = стоп-дистанция (как у SL), ревард = tp - entry
            risk  = post_price * STOP_LOSS_PCT
            reward = max(tp_price - post_price, 0.0)
            rr = (reward / risk) if risk > 0 else 0.0

            logging.info(f"[{sym}] BUY-check qty={qty}, tp={tp_price:.8f}, ppu={tp_price-post_price:.8f}, "
                         f"est_pnl={est_pnl:.2f}, required={required:.2f}, RR={rr:.2f}, tick={limits[sym]['tick_size']}")

            if est_pnl >= required and rr >= 2.0:
                try:
                    place_postonly_buy(sym, qty, post_price)
                    log_msg(f"BUY (maker) {sym} @ {post_price:.8f}, qty={qty}, TP={tp_price:.8f} (поставится после fill)", True)
                    avail_local = qty * post_price
                    avail = max(0.0, avail - avail_local)
                except Exception as e:
                    log_msg(f"{sym}: BUY failed: {e}", True)
            else:
                logging.info(f"{sym}: DEBUG_SKIP | PnL {est_pnl:.2f} < {required:.2f} или RR<{2.0}")

    # Ежедневный отчёт раз в день 22:30+
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    # лимиты поднимем заранее, чтобы стартовые логи были честные
    try:
        _ = get_limits()
    except:
        pass
    reconcile_positions_on_start()
    log_msg("🟢 Бот работает. Maker‑режим, фильтры мягкие, TP≥$1 чистыми.", True)
    while True:
        try:
            trade()
        except Exception as e:
            tb = traceback.format_exc()
            log_msg(f"Global error: {e}\n{tb}", True)
        time.sleep(60)
