# bot.py
# -*- coding: utf-8 -*-
import os, time, logging, datetime, requests, json
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

# Работаем только с двумя волатильными парами
SYMBOLS = ["DOGEUSDT", "XRPUSDT"]

RESERVE_BALANCE = 1.0
MAX_TRADE_USDT  = 120.0
MIN_NET_PROFIT  = 1.0           # строго ≥ $1 чистыми
STOP_LOSS_PCT   = 0.008         # 0.8% от цены входа

# Комиссии пользователя (спот): maker BUY 0.10%, maker SELL 0.18%
MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018

# Перекат лимитника ближе к рынку
ROLL_LIMIT_SECONDS = 45

# Кэш лимитов в Redis (с тик‑сайзом)
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

# для «торможения» однотипных спам‑логов при пропусках
SKIP_LOG_TIMESTAMPS = {}
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""

# ==================== UTILS ====================
def send_tg(msg: str):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg}
            )
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg: str, tg: bool = False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def should_log_skip(sym: str, key: str, interval_min: int = 10) -> bool:
    """Чтобы не засорять логи одинаковыми причинами каждые 10 сек."""
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
            "last_stop_time": "",
            "open_buy": None      # {"orderId","qty","price","ts"}
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

def api_call(fn, *args, **kwargs):
    wait = 0.35
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logging.warning(f"API retry {fn.__name__} attempt={attempt+1} wait={wait:.2f}s error={e}")
            time.sleep(wait)
            wait = min(wait * 2.0, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

# ==================== LIMITS ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    limits = {}
    for item in r["result"]["list"]:
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

def get_limits():
    global _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    if _LIMITS_MEM is not None:
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    cached = redis_client.get(LIMITS_REDIS_KEY)
    if cached:
        _LIMITS_MEM = json.loads(cached)
        _LIMITS_OK = True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from Redis cache")
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

    try:
        limits = _load_symbol_limits_from_api()
        redis_client.setex(LIMITS_REDIS_KEY, LIMITS_TTL_SEC, json.dumps(limits))
        _LIMITS_MEM = limits
        _LIMITS_OK = True
        _BUY_BLOCKED_REASON = ""
        logging.info("LIMITS loaded from API and cached")
    except Exception as e:
        _LIMITS_MEM = {}
        _LIMITS_OK = False
        _BUY_BLOCKED_REASON = f"LIMITS unavailable ({e}); BUY blocked"
        log_msg(f"⚠️ {_BUY_BLOCKED_REASON}", True)

    return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

# ==================== MARKET DATA & BALANCES ====================
def get_kline(sym: str) -> pd.DataFrame:
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=120)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_top_of_book(sym: str):
    """
    Bybit v5 orderbook (spot): result = {'a': [['price','size']], 'b': [['price','size']]}
    """
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        res = r["result"]
        bids = res.get("b") or []
        asks = res.get("a") or []
        bid = float(bids[0][0]) if bids else None
        ask = float(asks[0][0]) if asks else None
        return bid, ask
    except Exception as e:
        logging.info(f"orderbook failed {sym}: {e}")
        return None, None

def get_balances():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance(by: dict, sym: str) -> float:
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== QTY / PRICE HELPERS ====================
def adjust_qty(qty: float, step: float) -> float:
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def adjust_price(price: float, tick: float, mode: str = "nearest") -> float:
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

def get_qty(sym: str, price: float, usdt: float) -> float:
    limits, ok, _ = get_limits()
    if not ok or sym not in limits:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, limits[sym]["qty_step"])
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

# ==================== SIGNALS ====================
def signal(df: pd.DataFrame):
    if df.empty or len(df) < 50:
        return "none", 0.0, {}
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df["c"], 9).rsi()
    macd  = MACD(close=df["c"]); macd_line = macd.macd(); macd_sig = macd.macd_signal()

    atr5  = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range()
    atr15 = AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range()

    i = len(df) - 1
    ema9v, ema21v = ema9.iloc[i], ema21.iloc[i]
    rsi, m, s = rsi9.iloc[i], macd_line.iloc[i], macd_sig.iloc[i]
    atr5_pct = (atr5.iloc[i] / df["c"].iloc[i]) if df["c"].iloc[i] > 0 else 0.0

    two_of_three_buy = ((ema9v > ema21v) + (rsi > 50) + (m > s)) >= 2
    prev_cross = (ema9.iloc[i-1] <= ema21.iloc[i-1]) and (ema9v > ema21v)

    info = {"EMA9": float(ema9v), "EMA21": float(ema21v), "RSI": float(rsi),
            "MACD": float(m), "SIG": float(s), "ATR5%": float(atr5_pct*100.0)}
    if two_of_three_buy or prev_cross:
        return "buy", float(atr15.iloc[i]), info
    return "none", float(atr15.iloc[i]), info

def choose_multiplier(atr: float, price: float, atr5_pct_hint: float) -> float:
    vol = max((atr / price) if price > 0 else 0.0, atr5_pct_hint)
    if vol < 0.002:  # очень низкая вола — целимся дальше
        return 1.55
    elif vol < 0.005:
        return 1.30
    else:
        return 1.10

# ==================== ORDERS ====================
def place_postonly_buy(sym: str, qty: float, price: float):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Buy", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    STATE[sym]["open_buy"] = {"orderId": oid, "qty": qty, "price": price, "ts": time.time()}
    save_state()
    log_msg(f"{sym}: postOnly BUY placed id={oid} price={price} qty={qty}", True)

def cancel_order(sym: str, order_id: str):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
        log_msg(f"{sym}: order {order_id} canceled", True)
    except Exception as e:
        log_msg(f"{sym}: cancel failed {order_id}: {e}", True)

def is_order_open(sym: str, order_id: str) -> bool:
    try:
        r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=order_id)
        return len(r["result"]["list"]) > 0
    except Exception:
        return False

def place_tp_postonly(sym: str, qty: float, price: float):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Sell", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    oid = r["result"]["orderId"]
    log_msg(f"{sym}: TP postOnly placed id={oid} price={price} qty={qty}", True)

# ==================== RESTORE ON START ====================
def reconcile_positions_on_start():
    usdt, by = get_balances()
    limits, limits_ok, _ = get_limits()
    total_notional = 0.0
    lines = []

    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(by, sym)
        if bal > 0:
            atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
            atr15 = AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range().iloc[-1]
            mul = choose_multiplier(atr15, price, (atr5/price if price>0 else 0.0))
            tp  = adjust_price(price + mul * atr15, limits[sym]["tick_size"], mode="up")
            STATE[sym]["positions"] = [{
                "buy_price": price, "qty": bal, "tp": tp,
                "timestamp": datetime.datetime.now().isoformat()
            }]
            lines.append(f"- {sym}: синхр. позиция qty={bal} по ~{price:.6f}")
            total_notional += bal * price
        else:
            lines.append(f"- {sym}: позиций нет")

    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" +
            "\n".join(lines) + f"\n📊 Номинал по монетам: ${total_notional:.2f}", True)

# ==================== MAIN LOOP ====================
LAST_REPORT_DATE = None

def trade():
    global LAST_REPORT_DATE

    limits, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances()
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

        bal_coin = get_coin_balance(by, sym)
        bal_val  = bal_coin * price
        pos_open = len(st["positions"]) > 0

        # ====== ПОДРОБНЫЙ СТАТУС ПО СИМВОЛУ (как в «старых» логах) ======
        atr15_pct = (atr15 / price * 100.0) if price > 0 else 0.0
        logging.info(
            f"[{sym}] sig={sig}, price={price:.6f}, bal_val={bal_val:.2f}, pos={'1' if pos_open else '0'} | "
            f"bid={bid if bid is not None else '?'} ask={ask if ask is not None else '?'} | "
            f"EMA9={info.get('EMA9'):.6f} EMA21={info.get('EMA21'):.6f} RSI={info.get('RSI'):.2f} "
            f"MACD={info.get('MACD'):.6f} SIG={info.get('SIG'):.6f} | ATR(5/15m)={atr5_pct*100:.2f}%/{atr15_pct:.2f}% | "
            f"tp_mult={tp_mult:.2f}"
        )

        # ====== сопровождение открытого BUY (перекат/исполнение) ======
        ob = st.get("open_buy")
        if ob:
            oid = ob["orderId"]; oqty = ob["qty"]; oprice = ob["price"]; ots = ob["ts"]
            open_now = is_order_open(sym, oid)

            if open_now and time.time() - ots >= ROLL_LIMIT_SECONDS:
                nbid, _ = get_top_of_book(sym)
                if nbid:
                    new_price = adjust_price(nbid, tick, mode="down")
                    if new_price != oprice:
                        cancel_order(sym, oid)
                        place_postonly_buy(sym, oqty, new_price)
                        logging.info(f"{sym}: rolled BUY {oprice} → {new_price}")

            elif not open_now:
                # исполнен — фиксируем позицию и ставим TP
                st["open_buy"] = None
                # читаем фактический баланс на момент fill
                by_now = get_balances()[1]
                coin_bal = get_coin_balance(by_now, sym)
                qty = coin_bal if coin_bal > 0 else oqty
                entry = oprice
                tp_price = adjust_price(entry + tp_mult * atr15, tick, mode="up")
                st["positions"] = [{
                    "buy_price": entry, "qty": qty, "tp": tp_price,
                    "timestamp": datetime.datetime.now().isoformat()
                }]
                save_state()
                try:
                    place_tp_postonly(sym, qty, tp_price)
                except Exception as e:
                    log_msg(f"{sym}: TP place failed: {e} (price={tp_price}, tick={tick})", True)
                log_msg(f"✅ BUY filled {sym} @~{entry:.8f}, qty≈{qty}. TP={tp_price}", True)

        # ====== стоп‑лосс по открытой позиции ======
        if st["positions"]:
            pos = st["positions"][0]
            b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
            if price <= b * (1 - STOP_LOSS_PCT):
                try:
                    api_call(session.place_order, category="spot", symbol=sym,
                             side="Sell", orderType="Market", qty=str(q))
                    buy_comm  = b * q * MAKER_BUY_FEE
                    sell_comm = price * q * MAKER_SELL_FEE
                    pnl = (price - b) * q - (buy_comm + sell_comm)
                    st["pnl"] += pnl
                    log_msg(f"🛑 STOP SELL {sym} @ {price:.8f}, qty={q}, PnL={pnl:.2f}", True)
                    st["positions"] = []
                except Exception as e:
                    log_msg(f"{sym}: STOP SELL failed: {e}", True)

        # ====== новые входы ======
        if (sig == "buy") and not st["positions"] and not st["open_buy"]:
            if not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_msg(f"{sym}: ⛔️ Покупки блокированы: {buy_blocked_reason}", True)
                continue

            if avail < limits[sym]["min_amt"]:
                if should_log_skip(sym, "skip_funds"):
                    logging.info(f"{sym}: DEBUG_SKIP | недостаточно свободного USDT ({avail:.2f} < min_amt {limits[sym]['min_amt']})")
                continue

            if bid is None:
                logging.info(f"{sym}: DEBUG_SKIP | нет стакана (bid is None)")
                continue

            qty = get_qty(sym, price, per_sym)
            if qty <= 0:
                lim = limits.get(sym, {})
                logging.info(f"{sym}: DEBUG_SKIP | qty=0 price={price:.8f} step={lim.get('qty_step')} min_qty={lim.get('min_qty')} min_amt={lim.get('min_amt')}")
                continue

            post_price = adjust_price(bid, tick, mode="down")
            tp_price   = adjust_price(post_price + tp_mult * atr15, tick, mode="up")

            buy_comm  = post_price * qty * MAKER_BUY_FEE
            sell_comm = tp_price  * qty * MAKER_SELL_FEE
            est_pnl   = (tp_price - post_price) * qty - (buy_comm + sell_comm)

            logging.info(
                f"[{sym}] BUY-check qty={qty}, tp={tp_price:.8f}, ppu={tp_price-post_price:.8f}, "
                f"est_pnl={est_pnl:.2f}, required={MIN_NET_PROFIT:.2f}, tick={tick}"
            )

            if est_pnl >= MIN_NET_PROFIT:
                try:
                    place_postonly_buy(sym, qty, post_price)
                    log_msg(f"BUY (maker) {sym} @ {post_price:.8f}, qty={qty}; TP будет поставлен после fill", True)
                    avail = max(0.0, avail - qty * post_price)  # локальная «резервация»
                except Exception as e:
                    log_msg(f"{sym}: BUY failed: {e}", True)
            else:
                logging.info(f"{sym}: DEBUG_SKIP | ожидаемый PnL {est_pnl:.2f} < требуемого {MIN_NET_PROFIT:.2f}")

    # ежедневный отчёт (один раз в день после 22:30)
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
        if st["positions"]:
            pos_lines = [f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}" for p in st["positions"]]
            pos_text = "\n    " + "\n    ".join(pos_lines)
        else:
            pos_text = " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    reconcile_positions_on_start()
    log_msg("🟢 Бот работает. Maker‑режим, фильтры мягкие, TP≥$1 чистыми.", True)
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
