# bot.py
# -*- coding: utf-8 -*-
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
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ==================== CONFIG ====================
TG_VERBOSE = True
SYMBOLS = ["DOGEUSDT", "XRPUSDT"]

RESERVE_BALANCE = 1.0
MAX_TRADE_USDT = 120.0
MIN_NET_PROFIT = 1.0
STOP_LOSS_PCT = 0.008

MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018

ROLL_LIMIT_SECONDS = 45
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

getcontext().prec = 28

# ==================== SESSION & STATE ====================
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

# ==================== UTILS ====================
def send_tg(msg):
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

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {"positions": [], "pnl": 0.0, "last_stop_time": "", "open_buy": None})

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
            logging.warning(f"API retry {fn.__name__} {attempt+1}: {e}")
            time.sleep(wait)
            wait = min(wait * 2.0, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

# ==================== LIMITS ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    limits = {}
    for item in r["result"]["list"]:
        if item["symbol"] in SYMBOLS:
            lot = item.get("lotSizeFilter", {})
            pf  = item.get("priceFilter", {})
            limits[item["symbol"]] = {
                "min_qty":  float(lot.get("minOrderQty", 0.0)),
                "qty_step": float(lot.get("qtyStep", 1.0)),
                "min_amt":  float(item.get("minOrderAmt", 10.0)),
                "tick_size": float(pf.get("tickSize", 0.00000001)),
            }
    return limits

def get_limits():
    global _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    if _LIMITS_MEM:
        return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON
    cached = redis_client.get(LIMITS_REDIS_KEY)
    if cached:
        _LIMITS_MEM = json.loads(cached)
        _LIMITS_OK = True
        return _LIMITS_MEM, _LIMITS_OK, ""
    try:
        limits = _load_symbol_limits_from_api()
        redis_client.setex(LIMITS_REDIS_KEY, LIMITS_TTL_SEC, json.dumps(limits))
        _LIMITS_MEM = limits
        _LIMITS_OK = True
    except Exception as e:
        _LIMITS_MEM = {}
        _LIMITS_OK = False
        _BUY_BLOCKED_REASON = f"LIMITS unavailable ({e}); BUY blocked"
        log_msg(f"⚠️ {_BUY_BLOCKED_REASON}", True)
    return _LIMITS_MEM, _LIMITS_OK, _BUY_BLOCKED_REASON

# ==================== MKT DATA ====================
def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=120)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_top_of_book(sym):
    try:
        r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
        bids = r["result"].get("b", [])
        asks = r["result"].get("a", [])
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

def get_coin_balance(by, sym):
    return float(by.get(sym.replace("USDT", ""), 0.0))

# ==================== QTY & PRICE ====================
def adjust_qty(qty, step):
    q = Decimal(str(qty)); s = Decimal(str(step))
    return float((q // s) * s)

def adjust_price(price, tick, mode="nearest"):
    q = Decimal(str(price)) / Decimal(str(tick))
    if mode == "down":
        q = q.to_integral_value(rounding="ROUND_FLOOR")
    elif mode == "up":
        q = q.to_integral_value(rounding="ROUND_CEILING")
    else:
        q = q.to_integral_value(rounding="ROUND_HALF_UP")
    return float(Decimal(str(tick)) * q)

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

    two_of_three_buy = ((ema9v > ema21v) + (rsi > 50) + (macdv > macds)) >= 2
    prev_cross = (ema9.iloc[last-1] <= ema21.iloc[last-1]) and (ema9v > ema21v)

    info = {"EMA9": float(ema9v), "EMA21": float(ema21v), "RSI": float(rsi),
            "MACD": float(macdv), "SIG": float(macds), "ATR%": float(atr_pct)}

    if two_of_three_buy or prev_cross:
        return "buy", float(atr15.iloc[last]), info
    return "none", float(atr15.iloc[last]), info

def choose_multiplier(atr, price, atr5_pct_hint):
    vol = max(atr / price if price > 0 else 0, atr5_pct_hint)
    if vol < 0.002:
        return 1.55
    elif vol < 0.005:
        return 1.30
    else:
        return 1.10

# ==================== TRADES ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.8f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.8f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE ====================
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
            mul  = choose_multiplier(atr=AverageTrueRange(df["h"], df["l"], df["c"], 15).average_true_range().iloc[-1],
                                     price=price, atr5_pct_hint=(atr5/price if price>0 else 0))
            tp   = adjust_price(price + mul * atr5, limits[sym]["tick_size"], mode="up")
            STATE[sym]["positions"] = [{"buy_price": price, "qty": bal, "tp": tp,
                                        "timestamp": datetime.datetime.now().isoformat()}]
            lines.append(f"- {sym}: синхр. позиция qty={bal} по ~{price:.6f}")
            total_notional += bal * price
        else:
            lines.append(f"- {sym}: позиций нет")
    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" + "\n".join(lines) +
            f"\n📊 Номинал по монетам: ${total_notional:.2f}", True)

# ==================== ORDERS ====================
def place_postonly_buy(sym, qty, price):
    r = api_call(session.place_order, category="spot", symbol=sym,
                 side="Buy", orderType="Limit", qty=str(qty),
                 price=str(price), timeInForce="PostOnly")
    STATE[sym]["open_buy"] = {"orderId": r["result"]["orderId"], "qty": qty, "price": price, "ts": time.time()}
    save_state()
    log_msg(f"{sym}: postOnly BUY placed @ {price} qty={qty}", True)

def cancel_order(sym, order_id):
    api_call(session.cancel_order, category="spot", symbol=sym, orderId=order_id)
    log_msg(f"{sym}: order {order_id} canceled", True)

def is_order_open(sym, order_id):
    r = api_call(session.get_open_orders, category="spot", symbol=sym, orderId=order_id)
    return len(r["result"]["list"]) > 0

def place_tp_postonly(sym, qty, price):
    api_call(session.place_order, category="spot", symbol=sym,
             side="Sell", orderType="Limit", qty=str(qty),
             price=str(price), timeInForce="PostOnly")
    log_msg(f"{sym}: TP postOnly placed @ {price} qty={qty}", True)

# ==================== MAIN LOOP ====================
LAST_REPORT_DATE = None
def trade():
    global LAST_REPORT_DATE
    limits, limits_ok, reason = get_limits()
    usdt, by = get_balances()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / max(1, len(SYMBOLS))

    for sym in SYMBOLS:
        st = STATE[sym]
        df = get_kline(sym)
        if df.empty: continue
        sig, atr15, info = signal(df)
        price = df["c"].iloc[-1]
        bid, ask = get_top_of_book(sym)
        atr5 = AverageTrueRange(df["h"], df["l"], df["c"], 5).average_true_range().iloc[-1]
        tp_mult = choose_multiplier(atr15, price, atr5 / price if price > 0 else 0)
        bal_coin = get_coin_balance(by, sym)

        ob = st.get("open_buy")
        if ob:
            if is_order_open(sym, ob["orderId"]) and time.time() - ob["ts"] >= ROLL_LIMIT_SECONDS:
                nbid, _ = get_top_of_book(sym)
                if nbid:
                    new_price = adjust_price(nbid, limits[sym]["tick_size"], mode="down")
                    cancel_order(sym, ob["orderId"])
                    place_postonly_buy(sym, ob["qty"], new_price)
            elif not is_order_open(sym, ob["orderId"]):
                st["open_buy"] = None
                tp_price = adjust_price(ob["price"] + tp_mult * atr15, limits[sym]["tick_size"], mode="up")
                st["positions"] = [{"buy_price": ob["price"], "qty": bal_coin, "tp": tp_price,
                                    "timestamp": datetime.datetime.now().isoformat()}]
                place_tp_postonly(sym, bal_coin, tp_price)

        if st["positions"]:
            pos = st["positions"][0]
            if price <= pos["buy_price"] * (1 - STOP_LOSS_PCT):
                api_call(session.place_order, category="spot", symbol=sym,
                         side="Sell", orderType="Market", qty=str(pos["qty"]))
                pnl = (price - pos["buy_price"]) * pos["qty"] - \
                      (pos["buy_price"] * pos["qty"] * MAKER_BUY_FEE + price * pos["qty"] * MAKER_SELL_FEE)
                st["pnl"] += pnl
                log_trade(sym, "STOP SELL", price, pos["qty"], pnl)
                st["positions"] = []

        if sig == "buy" and not st["positions"] and not st["open_buy"]:
            if not limits_ok or avail < limits[sym]["min_amt"] or not bid:
                continue
            qty = get_qty(sym, price, per_sym)
            if qty <= 0:
                continue
            post_price = adjust_price(bid, limits[sym]["tick_size"], mode="down")
            tp_price = adjust_price(post_price + tp_mult * atr15, limits[sym]["tick_size"], mode="up")
            est_pnl = (tp_price - post_price) * qty - \
                      (post_price * qty * MAKER_BUY_FEE + tp_price * qty * MAKER_SELL_FEE)
            if est_pnl >= MIN_NET_PROFIT:
                place_postonly_buy(sym, qty, post_price)
                avail -= qty * post_price

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
        pos_text = "\n    ".join(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}" for p in st["positions"]) or "нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f}; {pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    init_state()
    reconcile_positions_on_start()
    log_msg("🟢 Бот работает. Maker-режим, фильтры мягкие, TP≥$1 чистыми.", True)
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
