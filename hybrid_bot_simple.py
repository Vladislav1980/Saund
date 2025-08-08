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
MIN_NET_PROFIT    = 1.0           # >= $1 чистыми после комиссий
STOP_LOSS_PCT     = 0.008         # SL по твоей старой логике (0.8%)

TAKER_BUY_FEE  = 0.0010
TAKER_SELL_FEE = 0.0018

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]
LAST_REPORT_DATE = None
cycle_count = 0

# Кэш лимитов в Redis на 12 часов
LIMITS_REDIS_KEY = "limits_cache_v1"
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
_LIMITS_MEM = None           # ленивый кэш в памяти
_LIMITS_OK  = False          # флаг: удалось ли загрузить лимиты
_BUY_BLOCKED_REASON = ""     # причина, если покупки временно заблокированы

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
            "count": 0,
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
    """
    Ленивая загрузка лимитов:
    - сперва память
    - иначе Redis
    - иначе API → Redis
    Если API не даёт (например 403), покупки блокируются, SELL остаётся разрешён.
    """
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
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

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
        return "none", 0, ""
    # индикаторы
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

    # Ослабленный вход: 2 из 3, RSI > 48
    two_of_three_buy = ((last["ema9"] > last["ema21"]) +
                        (last["rsi"] > 48) +
                        (last["macd"] > last["macd_signal"])) >= 2

    # Крест вверх EMA + не низкий RSI
    ema_cross_up   = (prev["ema9"] <= prev["ema21"]) and (last["ema9"] > last["ema21"]) and (last["rsi"] > 45)

    # Селл‑сигнал (для логов/контекста; продажи у нас по TP/SL)
    two_of_three_sell = ((last["ema9"] < last["ema21"]) +
                         (last["rsi"] < 50) +
                         (last["macd"] < last["macd_signal"])) >= 2
    ema_cross_down = (prev["ema9"] >= prev["ema21"]) and (last["ema9"] < last["ema21"]) and (last["rsi"] < 55)

    info = (f"EMA9={last['ema9']:.6f},EMA21={last['ema21']:.6f},RSI={last['rsi']:.2f},"
            f"MACD={last['macd']:.6f},SIG={last['macd_signal']:.6f},"
            f"XUP={int(ema_cross_up)},XDN={int(ema_cross_down)}")

    if two_of_three_buy or ema_cross_up:
        return "buy", last["atr"], info
    if two_of_three_sell or ema_cross_down:
        return "sell", last["atr"], info
    return "none", last["atr"], info

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

def dynamic_min_profit(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.004:
        return 0.6
    elif pct < 0.008:
        return 0.8
    else:
        return 1.2

# ==================== TRADES & LOGS ====================
def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== RESTORE ====================
def _sum_positions_qty(positions):
    return sum(float(p.get("qty", 0.0)) for p in positions)

def _round_to_step(x, step):
    return adjust_qty(x, step)

def reconcile_positions_on_start():
    """
    Восстановление позиций из Redis БЕЗ перетирания buy_price/TP:
    - если баланс ≈ сумме qty → оставляем как есть
    - если баланс меньше → ужимаем до баланса (LIFO)
    - если баланс больше → добавляем «добивающую» позицию от текущей цены
    - если позиций нет, но баланс есть → создаём синтетическую позицию
    """
    usdt, by = get_balances_cache()
    limits, limits_ok, _ = get_limits()
    total_notional = 0.0
    lines = []

    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        atr   = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
        st = STATE[sym]
        positions = st["positions"]

        step = limits.get(sym, {}).get("qty_step", 1.0) if limits_ok else 1.0
        min_amt = limits.get(sym, {}).get("min_amt", 10.0) if limits_ok else 10.0

        bal = _round_to_step(get_coin_balance_from(by, sym), step)
        saved_qty = _round_to_step(_sum_positions_qty(positions), step)
        logging.info(f"[{sym}] restore: balance={bal}, saved_qty={saved_qty}, price={price:.6f}")

        if positions:
            if abs(bal - saved_qty) < max(step, 1e-9):
                qty_view = " + ".join([str(p['qty']) for p in positions])
                lines.append(f"- {sym}: восстановлены позиции ({qty_view}) без изменений")
            elif bal < step or bal * price < min_amt:
                cnt = len(positions)
                positions.clear()
                lines.append(f"- {sym}: баланс≈0 → очищено {cnt} позиций")
            elif bal < saved_qty:
                need = bal
                new_positions = []
                for p in reversed(positions):
                    if need <= 0:
                        break
                    take = _round_to_step(min(p["qty"], need), step)
                    if take >= step and take * price >= min_amt:
                        np = p.copy(); np["qty"] = take
                        new_positions.append(np)
                        need -= take
                new_positions.reverse()
                st["positions"] = new_positions
                lines.append(f"- {sym}: скорректировано вниз до баланса={bal} (было {saved_qty})")
            else:
                extra = _round_to_step(bal - saved_qty, step)
                if extra >= step and extra * price >= min_amt:
                    mul = choose_multiplier(atr, price)
                    tp  = price + mul * atr
                    st["positions"].append({
                        "buy_price": price, "qty": extra, "tp": tp,
                        "timestamp": datetime.datetime.now().isoformat()
                    })
                    lines.append(f"- {sym}: добавлена позиция qty={extra} для выравнивания")
                else:
                    lines.append(f"- {sym}: баланс>{saved_qty}, но extra слишком мал")
        else:
            if bal >= step and bal * price >= min_amt:
                mul = choose_multiplier(atr, price)
                tp  = price + mul * atr
                st["positions"] = [{
                    "buy_price": price, "qty": bal, "tp": tp,
                    "timestamp": datetime.datetime.now().isoformat()
                }]
                lines.append(f"- {sym}: создана синтетическая позиция qty={bal} по текущей цене")
            else:
                lines.append(f"- {sym}: позиций нет и баланс мал для ордера")

        total_notional += bal * price

    save_state()
    log_msg("🚀 Бот запущен (восстановление позиций)\n" + "\n".join(lines) + f"\n📊 Номинал по монетам: ${total_notional:.2f}", True)

# ==================== INIT ====================
def init_positions():
    reconcile_positions_on_start()

# ==================== MAIN LOGIC ====================
def trade():
    global cycle_count, LAST_REPORT_DATE
    limits, limits_ok, buy_blocked_reason = get_limits()

    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        st = STATE[sym]
        st["sell_failed"] = False

        df = get_kline(sym)
        if df.empty:
            continue

        sig, atr, info = signal(df)
        price = df["c"].iloc[-1]
        coin_bal = get_coin_balance_from(by, sym)
        value = coin_bal * price
        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, value={value:.2f}, pos={len(st['positions'])}, {info}")

        # --- чистим фантомы, если были ---
        if st["positions"] and coin_bal < sum(p["qty"] for p in st["positions"]):
            state_count = len(st["positions"])
            st["positions"] = []
            log_msg(f"{sym}: удалены {state_count} фантомные позиции из-за нулевого остатка", True)

        # --- SELL / STOP-LOSS / TP ---
        for pos in list(st["positions"]):
            b, q = pos["buy_price"], pos["qty"]
            cost = b * q
            buy_comm  = cost * TAKER_BUY_FEE
            sell_comm = price * q * TAKER_SELL_FEE
            pnl = (price - b) * q - (buy_comm + sell_comm)

            # SL по % и только если это не «мелочь» (>= MIN_NET_PROFIT)
            if q > 0 and price <= b * (1 - STOP_LOSS_PCT) and abs(pnl) >= MIN_NET_PROFIT:
                if coin_bal >= q:
                    try:
                        api_call(session.place_order, category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                        log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop‑loss")
                        st["pnl"] += pnl
                    except Exception as e:
                        log_msg(f"{sym}: STOP SELL failed: {e}", True)
                        st["sell_failed"] = True
                else:
                    log_msg(f"SKIPPED STOP SELL {sym}: insufficient balance", True)
                    st["sell_failed"] = True

                st["last_stop_time"] = datetime.datetime.now().isoformat()
                st["avg_count"] = 0
                break

            # TP по сохранённой цене и минимальной прибыли
            if q > 0 and price >= pos["tp"] and pnl >= MIN_NET_PROFIT:
                if coin_bal >= q:
                    try:
                        api_call(session.place_order, category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                        log_trade(sym, "TP SELL", price, q, pnl, "take‑profit")
                        st["pnl"] += pnl
                    except Exception as e:
                        log_msg(f"{sym}: TP SELL failed: {e}", True)
                        st["sell_failed"] = True
                st["positions"], st["avg_count"], st["sell_failed"], st["last_sell_price"] = [], 0, False, price
                break

        if st.get("sell_failed"):
            st["positions"] = []

        # --- BUY (теперь с ослабленным входом, но не «диким») ---
        if sig == "buy" and not st["positions"]:
            last_stop = st.get("last_stop_time", "")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs < 4:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — прошло лишь {hrs:.1f}ч после стоп‑лосса (мин 4ч)")
            elif not limits_ok:
                if should_log_skip(sym, "buy_blocked_limits"):
                    log_skip(sym, f"Пропуск BUY — {buy_blocked_reason}")
                    send_tg(f"{sym}: ⛔️ Покупки временно заблокированы: {buy_blocked_reason}")
            elif avail >= (limits[sym]["min_amt"] if sym in limits else 10.0):
                qty = get_qty(sym, price, avail)
                if qty > 0:
                    cost = price * qty
                    buy_comm = cost * TAKER_BUY_FEE
                    mul = choose_multiplier(atr, price)
                    tp_price = price + mul * atr

                    sell_comm = tp_price * qty * TAKER_SELL_FEE
                    est_pnl = (tp_price - price) * qty - (buy_comm + sell_comm)
                    required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))

                    logging.info(f"[{sym}] mul={mul:.2f}, tp_price={tp_price:.6f}")
                    logging.info(f"[{sym}] est_pnl calc: qty={qty}, cost={cost:.6f}, buy_fee={buy_comm:.6f}, tp_price={tp_price:.6f}, sell_fee={sell_comm:.6f}, est_pnl={est_pnl:.6f}, required={required_pnl:.2f}")

                    if est_pnl >= required_pnl:
                        try:
                            api_call(session.place_order, category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            STATE[sym]["positions"].append({
                                "buy_price": price, "qty": qty, "tp": tp_price,
                                "timestamp": datetime.datetime.now().isoformat()
                            })
                            log_trade(sym, "BUY", price, qty, 0.0, "initiated position from USDT")
                            avail -= cost
                        except Exception as e:
                            log_msg(f"{sym}: BUY failed: {e}", True)
                    else:
                        if should_log_skip(sym, "skip_low_profit"):
                            log_skip(sym, f"Пропуск BUY — ожидаемый PnL мал (нужно {required_pnl:.2f}, получили {est_pnl:.2f})")
                else:
                    if should_log_skip(sym, "skip_qty"):
                        limit = limits.get(sym, {"qty_step": "?", "min_qty": "?", "min_amt": "?"})
                        log_skip(sym, f"Пропуск BUY — qty=0 (price={price:.6f}, step={limit['qty_step']}, min_qty={limit['min_qty']}, min_amt={limit['min_amt']})")
            else:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")

    cycle_count += 1

    # Ежедневный отчёт
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
    reconcile_positions_on_start()
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
