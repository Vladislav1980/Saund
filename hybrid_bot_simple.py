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

RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 105.0
MIN_NET_PROFIT    = 0.7          # минимальная ЧИСТАЯ прибыль на сделку, USD

# Стопы: ATR‑базовые + «аварийный» стоп
K_SL_ATR            = float(os.getenv("K_SL_ATR", "1.2"))        # SL = buy - K*ATR_entry
MIN_HOLD_SECONDS    = int(os.getenv("MIN_HOLD_SECONDS", "120"))  # защита от шумового выбивания
EMERGENCY_DROP_PCT  = float(os.getenv("EMERGENCY_DROP_PCT", "0.016"))  # 1.6% — экстренный SL
EMERGENCY_DROP_ATR  = float(os.getenv("EMERGENCY_DROP_ATR", "2.0"))    # 2*ATR — экстренный SL

# Комиссии taker (лучше хранить в .env / обновлять из /v5/account/fee-rate)
TAKER_BUY_FEE  = float(os.getenv("TAKER_BUY_FEE",  "0.0010"))
TAKER_SELL_FEE = float(os.getenv("TAKER_SELL_FEE", "0.0018"))

# Буфер на слиппедж для TP (процент от цены)
TP_SLIPPAGE_PCT = 0.0005  # 0.05%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

LAST_REPORT_DATE = None
cycle_count = 0

getcontext().prec = 28  # Decimal точность

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}  # per symbol: positions[], pnl, last_stop_time, ...

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

SKIP_LOG_TIMESTAMPS = {}

# ==================== UTIL: LOGGING & TG ====================
def should_log_skip(sym, key, interval=15):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval * 60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

def log_skip(sym, msg):
    logging.info(f"{sym}: {msg}")

def send_tg(msg):
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

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],         # [{buy_price, qty, tp, sl, atr_entry, timestamp, buy_order_id}]
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

# ==================== API HELPERS ====================
def api_call(fn, *args, **kwargs):
    """
    Обёртка с экспоненциальным бэкоффом на сетевые/временные ошибки.
    """
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            wait = 0.25 * (2 ** attempt) + random.random() * 0.2
            logging.warning(f"API retry {fn.__name__} attempt={attempt+1} wait={wait:.2f}s error={e}")
            time.sleep(wait)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

def market_order(symbol: str, side: str, qty: float, category: str = "spot"):
    """
    Маркет-ордер с подтверждением: (order_id, avg_price, filled_qty, status).
    """
    link_id = f"{symbol}-{side}-{int(time.time()*1000)}"
    resp = api_call(
        session.place_order,
        category=category, symbol=symbol, side=side,
        orderType="Market", qty=str(qty),
        timeInForce="IOC", orderLinkId=link_id
    )
    order_id = resp["result"]["orderId"]
    logging.info(f"[{symbol}] placed {side} market, orderId={order_id}, linkId={link_id}, req_qty={qty}")

    avg_px, filled_qty, status = 0.0, 0.0, "UNKNOWN"
    deadline = time.time() + 5.0  # до 5 секунд ждём подтверждение
    while time.time() < deadline:
        try:
            det = api_call(session.get_orders, category=category, orderId=order_id)
            lst = det.get("result", {}).get("list", [])
            if lst:
                item = lst[0]
                status = item.get("orderStatus") or item.get("state") or ""
                avg_px = float(item.get("avgPrice") or 0.0)
                filled_qty = float(item.get("cumExecQty") or 0.0)
                logging.info(f"[{symbol}] order status={status}, avgPrice={avg_px}, filledQty={filled_qty}")
                if filled_qty > 0.0 and avg_px > 0.0:
                    break
        except Exception as e:
            logging.warning(f"[{symbol}] get_orders error: {e}")
        time.sleep(0.25)

    if filled_qty == 0.0 or avg_px == 0.0:
        logging.warning(f"[{symbol}] WARNING: no fill info within timeout; fallback to requested qty={qty}")
        filled_qty = qty  # fallback

    return order_id, avg_px, filled_qty, status

# ==================== INSTRUMENT LIMITS ====================
def load_symbol_limits():
    resp = api_call(session.get_instruments_info, category="spot")["result"]["list"]
    return {
        item["symbol"]: {
            "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0)),
            "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0))
        }
        for item in resp if item["symbol"] in SYMBOLS
    }

LIMITS = load_symbol_limits()

def adjust_qty(qty, step):
    """
    Точное округление вниз к шагу (работает и для «кривых» шагов типа 0.0015)
    """
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return float((q // s) * s)

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

# ==================== SIGNALS ====================
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    df["atr"]   = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]

    # Осторожный вход: тренд + импульс без перекупленности
    buy_ok  = (last["ema9"] > last["ema21"]) and (last["macd"] > last["macd_signal"]) and (50 < last["rsi"] < 70)
    sell_ok = (last["ema9"] < last["ema21"]) and (last["macd"] < last["macd_signal"]) and (last["rsi"] < 50)

    info = (f"EMA9={last['ema9']:.6f},EMA21={last['ema21']:.6f},RSI={last['rsi']:.2f},"
            f"MACD={last['macd']:.6f},SIG={last['macd_signal']:.6f}")
    if buy_ok:
        return "buy", last["atr"], info
    if sell_ok:
        return "sell", last["atr"], info
    return "none", last["atr"], info

def get_kline(sym):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

# ==================== BALANCES (cache per loop) ====================
def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    co = sym.replace("USDT", "")
    return float(by.get(co, 0.0))

# ==================== QTY helper ====================
def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

# ==================== STRATEGY HELPERS ====================
def seconds_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds()
    except:
        return 1e9

def choose_multiplier(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.01:   return 0.7
    elif pct < 0.02: return 1.0
    else:            return 1.5

def dynamic_min_profit(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.004:  return 0.6
    elif pct < 0.008:return 0.8
    else:            return 1.2

def tp_from_required_profit(price, qty, required_pnl):
    """
    Требуемая цена ТР, чтобы гарантировать прибыль P (USD) с учётом комиссий и буфера.
    tp >= [ P/q + price*(1 + taker_buy) ] / (1 - taker_sell)
    """
    numerator   = (required_pnl / qty) + price * (1.0 + TAKER_BUY_FEE)
    denominator = (1.0 - TAKER_SELL_FEE)
    tp = numerator / denominator
    tp *= (1.0 + TP_SLIPPAGE_PCT)
    return tp

# ==================== RESTORE & RECONCILE ====================
REQUIRED_POS_KEYS = ["buy_price", "qty", "tp", "sl", "atr_entry", "timestamp", "buy_order_id"]

def _sum_positions_qty(positions):
    return sum(float(p.get("qty", 0.0)) for p in positions)

def _round_to_step(x, step):
    return adjust_qty(x, step)

def _approx_qty_equal(q1, q2, step):
    return abs(q1 - q2) < max(step, 1e-9)

def _ensure_pos_fields(sym, pos, price, atr):
    """
    Дополняет/чинит недостающие поля позиции (после восстановления из Redis).
    SL/TP пересчитываются, если не заданы.
    """
    pos.setdefault("buy_price", float(price))
    pos.setdefault("qty", 0.0)
    pos.setdefault("atr_entry", float(atr))
    pos.setdefault("timestamp", datetime.datetime.now().isoformat())
    pos.setdefault("buy_order_id", "restored")

    buy = float(pos["buy_price"])
    q   = float(pos["qty"])
    atr_e = float(pos["atr_entry"])

    if "tp" not in pos or pos["tp"] is None:
        mul = choose_multiplier(atr_e, buy)
        atr_tp = buy + mul * atr_e
        req_tp = tp_from_required_profit(buy, max(q, 1e-12), max(MIN_NET_PROFIT, dynamic_min_profit(atr_e, buy)))
        pos["tp"] = float(max(atr_tp, req_tp))

    if "sl" not in pos or pos["sl"] is None:
        pos["sl"] = float(buy - K_SL_ATR * atr_e)

def reconcile_positions_on_start():
    """
    Восстановление позиций из Redis БЕЗ перетирания buy_price/TP/SL:
    - если есть позиции в STATE и фактический баланс ≈ сумме qty → оставляем, дополняем недостающие поля
    - если баланс меньше → уменьшаем позиции (LIFO) до баланса
    - если баланс больше → докидываем «добавочную» позицию с текущей ценой/ATR
    - если позиций нет, но баланс есть → создаём синтетическую позицию
    Всё логируется и отправляется в Telegram.
    """
    usdt, by = get_balances_cache()
    summary_lines = []
    total_notional = 0.0

    for sym in SYMBOLS:
        step = LIMITS[sym]["qty_step"]
        min_amt = LIMITS[sym]["min_amt"]
        df = get_kline(sym)
        if df.empty:
            continue
        price = df["c"].iloc[-1]
        atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
        st = STATE[sym]
        positions = st["positions"]
        bal = get_coin_balance_from(by, sym)
        bal = _round_to_step(bal, step)  # к шагу
        saved_qty = _round_to_step(_sum_positions_qty(positions), step)

        logging.info(f"[{sym}] restore: balance={bal}, saved_qty={saved_qty}, price={price:.6f}")

        if positions:
            # чиним недостающие поля во всех сохранённых позициях
            for p in positions:
                _ensure_pos_fields(sym, p, price, atr)

            if _approx_qty_equal(bal, saved_qty, step):
                qty_view = " + ".join([str(p['qty']) for p in positions])
                summary_lines.append(f"- {sym}: восстановлены позиции ({qty_view}) без изменений")
            elif bal < step or bal * price < min_amt:
                cnt = len(positions)
                positions.clear()
                summary_lines.append(f"- {sym}: баланс≈0 → очищено {cnt} сохранённых позиций")
            elif bal < saved_qty:
                # Уменьшаем позиции LIFO до баланса
                need = bal
                new_positions = []
                for p in reversed(positions):
                    if need <= 0:
                        break
                    take = min(p["qty"], need)
                    take = _round_to_step(take, step)
                    if take >= step and take * price >= min_amt:
                        np = p.copy()
                        np["qty"] = take
                        _ensure_pos_fields(sym, np, price, atr)
                        new_positions.append(np)
                        need -= take
                new_positions.reverse()
                st["positions"] = new_positions
                summary_lines.append(f"- {sym}: скорректировано вниз до баланса={bal} (было {saved_qty})")
            else:  # bal > saved_qty
                # Добавим «доп» позицию на разницу
                extra = _round_to_step(bal - saved_qty, step)
                if extra >= step and extra * price >= min_amt:
                    mul = choose_multiplier(atr, price)
                    atr_tp = price + mul * atr
                    required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))
                    req_tp = tp_from_required_profit(price, extra, required_pnl)
                    tp = max(atr_tp, req_tp)
                    sl = price - K_SL_ATR * atr
                    st["positions"].append({
                        "buy_price": price, "qty": extra,
                        "tp": tp, "sl": sl, "atr_entry": atr,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "buy_order_id": "reconciled-add"
                    })
                    summary_lines.append(f"- {sym}: добавлена позиция qty={extra} для выравнивания до баланса={bal}")
                else:
                    summary_lines.append(f"- {sym}: баланс>{saved_qty}, но extra недостаточен для ордера")
        else:
            # Позиции отсутствуют, но если баланс есть — создаём синтетическую
            if bal >= step and bal * price >= min_amt:
                mul = choose_multiplier(atr, price)
                atr_tp = price + mul * atr
                required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))
                req_tp = tp_from_required_profit(price, bal, required_pnl)
                tp = max(atr_tp, req_tp)
                sl = price - K_SL_ATR * atr
                st["positions"] = [{
                    "buy_price": price, "qty": bal,
                    "tp": tp, "sl": sl, "atr_entry": atr,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "buy_order_id": "imported"
                }]
                summary_lines.append(f"- {sym}: создана синтетическая позиция qty={bal} по текущей цене")
            else:
                summary_lines.append(f"- {sym}: позиций нет и баланс пуст/мало для min ордера")

        total_notional += bal * price

    save_state()
    header = "🚀 Бот запущен (восстановление позиций)\n" + "\n".join(summary_lines) + f"\n📊 Номинал по монетам: ${total_notional:.2f}"
    log_msg(header, True)

# ==================== INIT ====================
def init_positions():
    reconcile_positions_on_start()

# ==================== TRADING LOOP ====================
def trade():
    global cycle_count, LAST_REPORT_DATE

    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}")

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

        # ---- ПРОДАЖИ / СТОП по позициям
        for pos in list(st["positions"]):
            _ensure_pos_fields(sym, pos, price, atr)  # страховка
            b, q = float(pos["buy_price"]), float(pos["qty"])
            tp, sl = float(pos["tp"]), float(pos["sl"])
            atr_e, ts = float(pos["atr_entry"]), pos["timestamp"]
            cost = b * q
            buy_comm  = cost * TAKER_BUY_FEE
            sell_comm = price * q * TAKER_SELL_FEE
            est_pnl   = (price - b) * q - (buy_comm + sell_comm)
            held = seconds_since(ts)

            # Экстренный стоп
            emergency_sl_price1 = b * (1.0 - EMERGENCY_DROP_PCT)
            emergency_sl_price2 = b - EMERGENCY_DROP_ATR * atr_e
            emergency_trigger_price = max(emergency_sl_price1, emergency_sl_price2)

            sl_triggered_normal    = (price <= sl) and (held >= MIN_HOLD_SECONDS)
            sl_triggered_emergency = price <= emergency_trigger_price

            if q > 0 and (sl_triggered_emergency or sl_triggered_normal):
                try:
                    order_id, sell_px, filled, status = market_order(sym, "Sell", q)
                    real_sell = sell_px if sell_px > 0 else price
                    pnl = (real_sell - b) * filled - (b * filled * TAKER_BUY_FEE + real_sell * filled * TAKER_SELL_FEE)
                    reason = "emergency-stop" if sl_triggered_emergency else "atr-stop"
                    log_trade(sym, "STOP LOSS SELL", real_sell, filled, pnl,
                              f"{reason}, TP={tp:.6f}, SL={sl:.6f}, ATR_entry={atr_e:.6f}, held={held:.1f}s, orderId={order_id}, status={status}")
                    st["pnl"] += pnl
                    rest = q - filled
                    if rest > LIMITS[sym]["min_qty"] and rest * price >= LIMITS[sym]["min_amt"]:
                        pos["qty"] = rest
                    else:
                        st["positions"].remove(pos)
                except Exception as e:
                    log_msg(f"{sym}: STOP SELL failed: {e}", True)
                    st["sell_failed"] = True
                st["last_stop_time"] = datetime.datetime.now().isoformat()
                st["avg_count"] = 0
                break

            # Тейк‑профит
            if q > 0 and price >= tp:
                try:
                    order_id, sell_px, filled, status = market_order(sym, "Sell", q)
                    real_sell = sell_px if sell_px > 0 else price
                    pnl = (real_sell - b) * filled - (b * filled * TAKER_BUY_FEE + real_sell * filled * TAKER_SELL_FEE)
                    log_trade(sym, "TP SELL", real_sell, filled, pnl,
                              f"take‑profit, TP={tp:.6f}, SL={sl:.6f}, orderId={order_id}, status={status}")
                    st["pnl"] += pnl
                    rest = q - filled
                    if rest > LIMITS[sym]["min_qty"] and rest * price >= LIMITS[sym]["min_amt"]:
                        pos["qty"] = rest
                        pos["buy_price"] = b
                    else:
                        st["positions"].remove(pos)
                    st["avg_count"], st["sell_failed"], st["last_sell_price"] = 0, False, real_sell
                except Exception as e:
                    log_msg(f"{sym}: TP SELL failed: {e}", True)
                    st["sell_failed"] = True
                break

        if st.get("sell_failed"):
            st["positions"] = []

        # ---- ПОКУПКИ
        if sig == "buy" and not st["positions"]:
            last_stop = st.get("last_stop_time", "")
            hrs = (seconds_since(last_stop) / 3600.0) if last_stop else 999
            if last_stop and hrs < 4:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — прошло лишь {hrs:.1f}ч после стоп‑лосса (мин 4ч)")
            elif avail >= LIMITS[sym]["min_amt"]:
                qty = get_qty(sym, price, avail)
                if qty > 0:
                    cost = price * qty
                    buy_comm = cost * TAKER_BUY_FEE
                    mul = choose_multiplier(atr, price)
                    atr_tp = price + mul * atr

                    required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))
                    req_tp = tp_from_required_profit(price, qty, required_pnl)
                    tp_price = max(atr_tp, req_tp)

                    # оценка PnL на этом TP
                    sell_comm = tp_price * qty * TAKER_SELL_FEE
                    est_pnl = (tp_price - price) * qty - (buy_comm + sell_comm)

                    # SL от ATR входа
                    sl_price = price - K_SL_ATR * atr

                    logging.info(f"[{sym}] mul={mul:.2f}, atr={atr:.6f}, atr_tp={atr_tp:.6f}, req_tp={req_tp:.6f}, "
                                 f"tp={tp_price:.6f}, sl={sl_price:.6f}")
                    logging.info(f"[{sym}] est_pnl: qty={qty}, cost={cost:.6f}, buy_fee={buy_comm:.6f}, "
                                 f"sell_fee={sell_comm:.6f}, est_pnl={est_pnl:.6f}, required={required_pnl:.2f}")

                    if est_pnl >= required_pnl - 1e-6:
                        try:
                            order_id, fill_px, filled, status = market_order(sym, "Buy", qty)
                            buy_px = fill_px if fill_px > 0 else price
                            # частичное исполнение — округляем вниз к шагу
                            filled = adjust_qty(filled, LIMITS[sym]["qty_step"])
                            if filled <= 0.0:
                                log_msg(f"{sym}: BUY filled=0 — игнорируем позицию (orderId={order_id})", True)
                            else:
                                # Пересчитать TP/SL от ФАКТИЧЕСКОЙ цены покупки
                                atr_now = atr
                                mul_now = choose_multiplier(atr_now, buy_px)
                                atr_tp_now = buy_px + mul_now * atr_now
                                req_tp_now = tp_from_required_profit(buy_px, filled, required_pnl)
                                tp_now = max(atr_tp_now, req_tp_now)
                                sl_now = buy_px - K_SL_ATR * atr_now

                                STATE[sym]["positions"].append({
                                    "buy_price": buy_px, "qty": filled,
                                    "tp": tp_now, "sl": sl_now,
                                    "atr_entry": atr_now,
                                    "timestamp": datetime.datetime.now().isoformat(),
                                    "buy_order_id": order_id
                                })
                                log_trade(sym, "BUY", buy_px, filled, 0.0,
                                          f"open from USDT, TP={tp_now:.6f}, SL={sl_now:.6f}, ATR_entry={atr_now:.6f}, orderId={order_id}, status={status}")
                                avail -= buy_px * filled
                        except Exception as e:
                            log_msg(f"{sym}: BUY failed: {e}", True)
                    else:
                        if should_log_skip(sym, "skip_low_profit"):
                            log_skip(sym, f"Пропуск BUY — TP не даёт требуемый PnL (нужно {required_pnl:.2f}, получилось {est_pnl:.2f})")
                else:
                    if should_log_skip(sym, "skip_qty"):
                        limit = LIMITS[sym]
                        log_skip(sym, f"Пропуск BUY — qty=0 (price={price:.6f}, step={limit['qty_step']}, "
                                      f"min_qty={limit['min_qty']}, min_amt={limit['min_amt']})")
            else:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")

    cycle_count += 1

    # Ежесуточный отчёт (локальное время сервера)
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
            pos_lines.append(
                f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f} / SL {p['sl']:.6f} (ATR={p['atr_entry']:.6f})"
            )
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== MAIN ====================
if __name__ == "__main__":
    init_state()
    init_positions()  # восстановление/сверка без перезаписи TP/buy_price; дополняем недостающие поля
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
