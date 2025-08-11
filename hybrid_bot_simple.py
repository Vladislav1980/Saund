# bot.py
# -*- coding: utf-8 -*-
import os, time, math, logging, datetime, requests, json
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

# Фокус на двух более волатильных монетах
SYMBOLS = ["DOGEUSDT", "XRPUSDT"]

RESERVE_BALANCE   = 1.0          # запас USDT не трогаем
MAX_TRADE_USDT    = 120.0        # максимум на один вход
MIN_NET_PROFIT    = 1.0          # минимальная чистая прибыль в $
# комиссии пользователя (спот): maker BUY 0.10%, maker SELL 0.18%
MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018

# Перекат лимитника (вход и лимит‑стоп)
ROLL_LIMIT_SECONDS = 45

# Риск-менеджмент
STOP_MODE        = "limit"   # "off" | "limit" | "market" (market не советую)
STOP_LOSS_ATR_K  = 1.2       # расстояние стопа = k * ATR(15)
RR_MIN           = 2.0       # минимальный Risk/Reward

# Redis кэш лимитов
LIMITS_REDIS_KEY = "limits_cache_v2"   # bump: v2 включает tick_size
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

def should_log_skip(sym, key, interval_min=10):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval_min * 60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

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
            "open_buy": None,     # {"orderId","qty","price","ts"}
            "open_stop": None     # {"orderId","qty","price","ts"}
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
            logging.info(f"Request → {fn.__name__.upper()}: {kwargs}")
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
    """
    Bybit v5 orderbook limit=1:
    result: {'a': [['price','size']], 'b': [['price','size']]} — строки.
    """
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        res = r["result"]
        bids = res.get("b") or res.get("bids") or []
        asks = res.get("a") or res.get("asks") or []
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

def round_qty_for(sym, qty):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return qty
    step = limits[sym]["qty_step"]
    q = adjust_qty(qty, step)
    # защита от «слишком мало»
    if q < limits[sym]["min_qty"]:
        return 0.0
    return q

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
    """
    BUY если «2 из 3»: (EMA9>EMA21), (RSI>55), (MACD>signal).
    Возвращаем ("buy"/"none", ATR15, info)
    """
    if df.empty or len(df) < 50:
        return "none", 0, {}
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    macd  = MACD(close=df["c"]); macd_line, macd_sig = macd.macd(), macd.macd_signal()

    atr5  = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range()
    atr15 = AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range()

    i = len(df) - 1
    ema9v, ema21v = float(ema9.iloc[i]), float(ema21.iloc[i])
    rsi = float(rsi9.iloc[i])
    m, s = float(macd_line.iloc[i]), float(macd_sig.iloc[i])
    atr5_pct = (float(atr5.iloc[i]) / float(df["c"].iloc[i])) if df["c"].iloc[i] > 0 else 0.0

    conds = int(ema9v > ema21v) + int(rsi > 55) + int(m > s)
    buy = conds >= 2

    info = {"EMA9": ema9v, "EMA21": ema21v, "RSI": rsi, "MACD": m, "SIG": s, "ATR%": atr5_pct}
    return ("buy" if buy else "none"), float(atr15.iloc[i]), info

def choose_multiplier(atr, price, atr5_pct_hint):
    pct = atr / price if price > 0 else 0
    vol = max(pct, atr5_pct_hint)
    if vol < 0.002:
        return 1.55
    elif vol < 0.005:
        return 1.30
    else:
        return 1.10

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
            # округлим позиционный qty вниз до шага — пригодится для ордеров
            if limits_ok and sym in limits:
                bal = round_qty_for(sym, bal)
            atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
            atr15= AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range().iloc[-1]
            mul  = choose_multiplier(atr=atr15, price=price, atr5_pct_hint=(atr5/price if price>0 else 0))
            tick = limits.get(sym, {}).get("tick_size", 0.00000001) if limits_ok else 0.00000001
            tp   = adjust_price(price + mul * atr15, tick, mode="up")
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
    limits, ok, _ = get_limits()
    qty_adj = round_qty_for(sym, qty)
    if qty_adj <= 0:
        raise RuntimeError(f"postonly_buy qty adj=0 (orig {qty}) step={limits.get(sym,{}).get('qty_step')}")
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Buy", orderType="Limit", qty=str(qty_adj),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    STATE[sym]["open_buy"] = {"orderId": oid, "qty": qty_adj, "price": price, "ts": time.time()}
    save_state()
    log_msg(f"{sym}: postOnly BUY placed id={oid} price={price} qty={qty_adj}", True)
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
    qty_adj = round_qty_for(sym, qty)
    if qty_adj <= 0:
        raise RuntimeError(f"tp qty adj=0 (orig {qty})")
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Sell", orderType="Limit", qty=str(qty_adj),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    log_msg(f"{sym}: TP postOnly placed id={oid} price={price} qty={qty_adj}", True)
    return oid

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

        # ATR% для мультипликатора (подсказка по волатильности)
        atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
        atr5_pct = (atr5 / price) if price > 0 else 0.0
        tp_mult = choose_multiplier(atr15, price, atr5_pct)

        bal_coin = get_coin_balance_from(by, sym)
        # округлим для корректных ордеров
        bal_coin = round_qty_for(sym, bal_coin)
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
                # ордер исполнен — ставим TP для всей позиции
                st["open_buy"] = None
                by_now = get_balances_cache()[1]
                coin_bal = get_coin_balance_from(by_now, sym)
                coin_bal = round_qty_for(sym, coin_bal)
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
            b, q = float(pos["buy_price"]), float(pos["qty"])
            q = round_qty_for(sym, q)
            # лимит‑стоп от волатильности
            sl_level = b - STOP_LOSS_ATR_K * atr15

            if STOP_MODE == "off":
                pass
            elif STOP_MODE == "market":
                if price <= sl_level:
                    try:
                        q_m = round_qty_for(sym, q)
                        api_call(session.place_order, category="spot", symbol=sym,
                                 side="Sell", orderType="Market", qty=str(q_m))
                        buy_comm  = b * q_m * MAKER_BUY_FEE
                        sell_comm = price * q_m * MAKER_SELL_FEE
                        pnl = (price - b) * q_m - (buy_comm + sell_comm)
                        st["pnl"] += pnl
                        log_trade(sym, "STOP SELL", price, q_m, pnl, "market stop-loss")
                        st["positions"] = []
                    except Exception as e:
                        log_msg(f"{sym}: STOP SELL failed: {e}", True)
            else:  # limit-эвакуация с перекатом
                ob_stop = st.get("open_stop")
                if price <= sl_level and not ob_stop:
                    new_bid, _ = get_top_of_book(sym)
                    if new_bid:
                        sl_price = adjust_price(new_bid, tick, mode="down")
                        try:
                            q_s = round_qty_for(sym, q)
                            r = api_call(session.place_order, category="spot", symbol=sym,
                                         side="Sell", orderType="Limit", qty=str(q_s),
                                         price=str(sl_price), timeInForce="PostOnly")
                            st["open_stop"] = {"orderId": r["result"]["orderId"], "price": sl_price, "qty": q_s, "ts": time.time()}
                            save_state()
                            log_msg(f"{sym}: LIMIT-STOP placed @{sl_price} qty={q_s}", True)
                        except Exception as e:
                            log_msg(f"{sym}: LIMIT-STOP place failed: {e} | price={sl_price}, tick={tick}", True)
                elif ob_stop:
                    if is_order_open(sym, ob_stop["orderId"]):
                        if time.time() - ob_stop["ts"] >= ROLL_LIMIT_SECONDS:
                            new_bid, _ = get_top_of_book(sym)
                            if new_bid:
                                new_price = adjust_price(new_bid, tick, mode="down")
                                if new_price != ob_stop["price"]:
                                    cancel_order(sym, ob_stop["orderId"])
                                    try:
                                        r = api_call(session.place_order, category="spot", symbol=sym,
                                                     side="Sell", orderType="Limit", qty=str(ob_stop["qty"]),
                                                     price=str(new_price), timeInForce="PostOnly")
                                        st["open_stop"] = {"orderId": r["result"]["orderId"], "price": new_price,
                                                           "qty": ob_stop["qty"], "ts": time.time()}
                                        save_state()
                                        logging.info(f"{sym}: LIMIT-STOP rolled {ob_stop['price']} → {new_price}")
                                    except Exception as e:
                                        log_msg(f"{sym}: LIMIT-STOP roll failed: {e}", True)
                    else:
                        # стоп исполнился
                        st["open_stop"] = None
                        sell_px = ob_stop["price"]
                        q_s = ob_stop["qty"]
                        buy_comm  = b * q_s * MAKER_BUY_FEE
                        sell_comm = sell_px * q_s * MAKER_SELL_FEE
                        pnl = (sell_px - b) * q_s - (buy_comm + sell_comm)
                        st["pnl"] += pnl
                        log_trade(sym, "STOP SELL", sell_px, q_s, pnl, "limit evac")
                        st["positions"] = []

        # 3) Новые входы (если нет позиции и нет открытого лимитника)
        if (sig == "buy") and not st["positions"] and not st["open_buy"]:
            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_msg(f"{sym}: ⛔️ Покупки блокированы: {buy_blocked_reason}", True)
                continue
            if avail < limits[sym]["min_amt"]:
                if should_log_skip(sym, "skip_funds"):
                    logging.info(f"{sym}: Пропуск BUY — свободного USDT мало")
                continue
            if not bid:
                logging.info(f"{sym}: DEBUG_SKIP | no orderbook for BUY")
                continue

            qty = get_qty(sym, price, per_sym)
            if qty <= 0:
                lim = limits.get(sym, {})
                logging.info(f"{sym}: DEBUG_SKIP | qty=0 price={price:.8f} step={lim.get('qty_step')} min_qty={lim.get('min_qty')} min_amt={lim.get('min_amt')}")
                continue

            post_price = adjust_price(bid, limits[sym]["tick_size"], mode="down")
            tp_raw   = post_price + tp_mult * atr15
            tp_price = adjust_price(tp_raw, limits[sym]["tick_size"], mode="up")

            # риск для RR (в $) — даже если стоп выключен, оцениваем на основе ATR
            risk_abs = STOP_LOSS_ATR_K * atr15 * qty

            buy_comm  = post_price * qty * MAKER_BUY_FEE
            sell_comm = tp_price  * qty * MAKER_SELL_FEE
            est_pnl   = (tp_price - post_price) * qty - (buy_comm + sell_comm)

            required  = max(MIN_NET_PROFIT, dynamic_min_profit(atr15, post_price))
            rr        = (est_pnl / risk_abs) if risk_abs > 0 else 999.0

            logging.info(f"[{sym}] BUY-check qty={qty}, tp={tp_price:.8f}, ppu={(tp_price-post_price):.8f}, "
                         f"est_pnl={est_pnl:.2f}, required={required:.2f}, RR={rr:.2f}, "
                         f"sl_level={post_price - STOP_LOSS_ATR_K*atr15:.8f}, tick={limits[sym]['tick_size']}")

            if est_pnl >= required and rr >= RR_MIN:
                try:
                    place_postonly_buy(sym, qty, post_price)
                    log_msg(f"BUY (maker) {sym} @ {post_price:.8f}, qty={qty}, TP={tp_price:.8f} (TP поставим после fill)", True)
                    # «резервируем» часть средств локально
                    avail_local = qty * post_price
                    avail = max(0.0, avail - avail_local)
                except Exception as e:
                    log_msg(f"{sym}: BUY failed: {e}", True)
            else:
                logging.info(f"{sym}: DEBUG_SKIP | PnL {est_pnl:.2f} < {required:.2f} or RR {rr:.2f} < {RR_MIN:.2f}")

    # Ежедневный отчёт
    now = datetime.datetime.now()
    global LAST_REPORT_DATE
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
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}")
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    # прогружаем лимиты заранее (и кешируем)
    try:
        get_limits()
    except Exception:
        pass
    reconcile_positions_on_start()
    log_msg("🟢 Бот работает. Maker‑режим, фильтры мягкие, TP≥$1 чистыми.", True)
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
