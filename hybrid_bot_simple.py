# -*- coding: utf-8 -*-
import os, time, math, logging, datetime, requests, json, threading
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

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 105.0
MIN_NET_PROFIT    = 1.0           # >= $1 чистыми после комиссий (менять не просили)
STOP_LOSS_PCT     = 0.008         # 0.8% (на случай форс-мажора)

# Комиссии: maker вход 0.10%, maker выход 0.18%
MAKER_BUY_FEE  = 0.0010
MAKER_SELL_FEE = 0.0018
# На всякий случай (если что-то уйдёт в маркет):
TAKER_BUY_FEE  = 0.0010
TAKER_SELL_FEE = 0.0018

# Логика лимитников
ROLLOVER_SEC       = 45          # перекатить лимитник, если не исполнился
MAKER_OFFSET_BP    = 2           # отступ от лучшей цены, б.п. (0.02%) в сторону исполнения
TP_SPLIT            = (0.5, 0.5)  # две заявки ТП (можно 1.0,0.0 чтобы была одна)

# Ослабленные фильтры входа (как просил)
RSI_MIN_BUY        = 46
USE_3TF            = True        # мульти‑таймфрейм, но мягкий
TF_SET             = [1, 5, 15]  # 1m, 5m, 15m

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

LAST_REPORT_DATE = None
cycle_count = 0

# ==================== TG ====================
def send_tg(msg: str):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg: send_tg(msg)

# ==================== UTIL ====================
def should_log_skip(sym, key, interval=10):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval * 60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

def round_to_step(x, step):
    q = Decimal(str(x)); s = Decimal(str(step))
    return float((q // s) * s)

def round_price_tick(p, tick):
    if tick <= 0: return float(p)
    d = Decimal(str(p)) / Decimal(str(tick))
    return float((d.to_integral_value(rounding="ROUND_FLOOR")) * Decimal(str(tick)))

def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

def save_state():
    try:
        redis_client.set("bot_state_v2", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def load_state():
    global STATE
    raw = redis_client.get("bot_state_v2")
    STATE = json.loads(raw) if raw else {}
    ensure_state_consistency()

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],         # list of lots [{buy, qty, tp1, tp2, id_buy, id_tp1, id_tp2, time}]
            "pnl": 0.0,
            "open_order": None,      # текущий активный лимитник на вход {id, price, qty, side, ts}
            "last_stop_time": "",
            "avg_count": 0
        })

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

# ==================== LIMITS (Bybit spot) ====================
def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    limits = {}
    for item in lst:
        sym = item["symbol"]
        if sym not in SYMBOLS: continue
        price_tick = float(item.get("priceFilter", {}).get("tickSize", 0.0) or 0.0)
        qty_step   = float(item.get("lotSizeFilter", {}).get("qtyStep", 0.0) or 0.0)
        min_qty    = float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0) or 0.0)
        min_amt    = float(item.get("minOrderAmt", 10.0) or 10.0)
        limits[sym] = {
            "tick": price_tick if price_tick>0 else 0.00000001,
            "qty_step": qty_step if qty_step>0 else 0.00000001,
            "min_qty": min_qty,
            "min_amt": min_amt
        }
    return limits

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

# ==================== DATA & BALANCES ====================
def get_kline(sym, interval="1", limit=200):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def coin_balance(by, sym):
    return float(by.get(sym.replace("USDT",""), 0.0))

# ==================== SIGNALS ====================
def tf_signal(df1m):
    if df1m.empty or len(df1m) < 60:
        return "none", 0.0, "no_data"
    ema9  = EMAIndicator(df1m["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df1m["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(df1m["c"], 9).rsi()
    macd  = MACD(close=df1m["c"])
    macd_line, macd_sig = macd.macd(), macd.macd_signal()
    atr5  = AverageTrueRange(df1m["h"], df1m["l"], df1m["c"], 14).average_true_range()

    last = len(df1m) - 1
    ema9v, ema21v, rsiv = float(ema9.iloc[last]), float(ema21.iloc[last]), float(rsi9.iloc[last])
    macdv, macds = float(macd_line.iloc[last]), float(macd_sig.iloc[last])
    atrv = float(atr5.iloc[last])

    buy_core = (ema9v >= ema21v) and (rsiv >= RSI_MIN_BUY) and (macdv >= macds)

    mtf_ok = True
    if USE_3TF:
        ok_count = 0
        for tf in ["5","15"]:
            d = get_kline(sym_current, interval=tf, limit=120)
            if d.empty: continue
            e9  = EMAIndicator(d["c"], 9).ema_indicator().iloc[-1]
            e21 = EMAIndicator(d["c"], 21).ema_indicator().iloc[-1]
            r9  = RSIIndicator(d["c"], 9).rsi().iloc[-1]
            ok_count += int((e9>=e21) and (r9>=45))
        mtf_ok = ok_count >= 1   # мягкий фильтр: достаточно 1 из 2

    sig = "buy" if (buy_core and mtf_ok) else "none"
    info = f"EMA9={ema9v:.6f} EMA21={ema21v:.6f} RSI={rsiv:.2f} MACD={macdv:.6f} SIG={macds:.6f}"
    return sig, atrv, info

# ==================== QTY/TP helpers ====================
def choose_tp_mult(atr_pct):
    # чуть агрессивнее, но вменяемо
    if atr_pct < 0.004: return 1.30
    if atr_pct < 0.008: return 1.55
    return 1.80

def alloc_qty(sym, price, usdt):
    limits, ok, _ = get_limits()
    if not ok or sym not in limits: return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    step = limits[sym]["qty_step"]
    q = round_to_step(alloc / price, step)
    if q < limits[sym]["min_qty"] or q * price < limits[sym]["min_amt"]:
        return 0.0
    return q

def est_net_pnl(entry, tp, qty, buy_fee=MAKER_BUY_FEE, sell_fee=MAKER_SELL_FEE):
    buy_comm  = entry * qty * buy_fee
    sell_comm = tp    * qty * sell_fee
    return (tp - entry) * qty - (buy_comm + sell_comm)

# ==================== ORDERS ====================
def place_postonly_limit(sym, side, price, qty):
    limits, ok, _ = get_limits()
    tick = limits[sym]["tick"]; step = limits[sym]["qty_step"]
    p_rounded = round_price_tick(price, tick)
    q_rounded = round_to_step(qty, step)
    if q_rounded <= 0:
        raise RuntimeError("qty rounds to zero")

    order = api_call(session.place_order,
        category="spot", symbol=sym, side=side, orderType="Limit",
        qty=str(q_rounded), price=str(p_rounded), timeInForce="PostOnly")
    oid = order["result"]["orderId"]
    return oid, p_rounded, q_rounded

def cancel_order(sym, oid):
    try:
        api_call(session.cancel_order, category="spot", symbol=sym, orderId=oid)
    except Exception as e:
        logging.warning(f"{sym}: cancel_order failed {oid}: {e}")

def best_bid_ask(sym):
    ob = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
    bid = float(ob["result"]["b"][0][0]); ask = float(ob["result"]["a"][0][0])
    return bid, ask

def rollover_worker():
    while True:
        try:
            for sym in SYMBOLS:
                st = STATE[sym]
                od = st.get("open_order")
                if not od: continue
                age = time.time() - od["ts"]
                if age < ROLLOVER_SEC: continue
                # перекатить
                side = od["side"]
                cancel_order(sym, od["id"])
                bid, ask = best_bid_ask(sym)
                px = (bid * (1 - MAKER_OFFSET_BP/10000.0)) if side=="Buy" else (ask * (1 + MAKER_OFFSET_BP/10000.0))
                oid, p2, q2 = place_postonly_limit(sym, side, px, od["qty"])
                st["open_order"] = {"id": oid, "price": p2, "qty": q2, "side": side, "ts": time.time()}
                logging.info(f"{sym}: rollover {side} -> price={p2:.8f}, qty={q2}")
                save_state()
        except Exception as e:
            logging.warning(f"rollover error: {e}")
        time.sleep(5)

# ==================== REPORTS ====================
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy']:.6f} → TP1 {p['tp1']:.6f}" + (f", TP2 {p['tp2']:.6f}" if p.get("tp2") else ""))
        pos_text = ("\n    " + "\n    ".join(pos_lines)) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total:.2f}")
    send_tg("\n".join(lines))

# ==================== MAIN LOOP ====================
def trade():
    global cycle_count, LAST_REPORT_DATE, sym_current
    limits, limits_ok, buy_blocked_reason = get_limits()

    usdt, by = get_balances_cache()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        sym_current = sym
        st = STATE[sym]
        df1 = get_kline(sym, "1", 200)
        if df1.empty: continue

        # рынок
        bid, ask = best_bid_ask(sym)
        price_mid = (bid + ask) / 2.0

        # сигнал
        sig, atr, info = tf_signal(df1)
        atr_pct = atr / price_mid if price_mid>0 else 0.0
        tp_mult = choose_tp_mult(atr_pct)

        coin_bal = coin_balance(by, sym)
        bal_val = coin_bal * price_mid
        pos_count = len(st["positions"])

        logging.info(f"[{sym}] sig={sig}, price={price_mid:.6f}, bal_val={bal_val:.2f}, pos={pos_count} | "
                     f"bid={bid:.6f} ask={ask:.6f} | {info} | ATR(5/15m)={atr:.6f} ({atr_pct*100:.2f}%) | tp_mult={tp_mult:.2f}")

        # === SELL / TP / SL по уже открытым ===
        if pos_count>0:
            keep_positions = []
            for p in st["positions"]:
                qty = p["qty"]; buy = p["buy"]
                # если есть выставленные ТП — ничего не делаем здесь
                keep_positions.append(p)
            st["positions"] = keep_positions

        # === BUY (limiter postOnly) ===
        # Покупаем только если нет позиции и нет активного ордера на вход
        if sig == "buy" and pos_count == 0 and st["open_order"] is None:
            if not limits_ok:
                if should_log_skip(sym, "limits"):
                    logging.info(f"[{sym}] DEBUG_SKIP | Покупки заблокированы: {buy_blocked_reason}")
                continue
            if per_sym < limits[sym]["min_amt"]:
                if should_log_skip(sym, "funds"):
                    logging.info(f"[{sym}] DEBUG_SKIP | Недостаточно USDT (need≥{limits[sym]['min_amt']}, have≈{per_sym:.2f})")
                continue

            # рассчитываем tp и проверяем чистую прибыль
            qty_alloc = alloc_qty(sym, ask, per_sym)
            if qty_alloc <= 0:
                if should_log_skip(sym, "qty0"):
                    logging.info(f"[{sym}] DEBUG_SKIP | qty=0 after rounding (step={limits[sym]['qty_step']})")
                continue

            # Вход по bid с маленьким отступом для maker
            entry_px = bid * (1 - MAKER_OFFSET_BP/10000.0)
            tp_px    = entry_px + tp_mult * atr

            # сплит ТП
            tp1, tp2 = None, None
            if TP_SPLIT[0] > 0:
                tp1 = tp_px
            if TP_SPLIT[1] > 0:
                # второй чуть дальше
                tp2 = entry_px + (tp_mult * 1.25) * atr

            est_p = est_net_pnl(entry_px, tp_px, qty_alloc, MAKER_BUY_FEE, MAKER_SELL_FEE)
            logging.info(f"[{sym}] BUY-check qty_alloc={qty_alloc}, need_qty≈{qty_alloc}, tp={tp_px:.6f}, "
                         f"ppu={(tp_px-entry_px):.6f}, est_pnl={est_p:.2f}, required={MIN_NET_PROFIT:.2f}, max_alloc≈${per_sym:.2f}")

            if est_p < MIN_NET_PROFIT:
                if should_log_skip(sym, "pnl_small"):
                    logging.info(f"{sym}: Пропуск BUY — ожидаемый PnL {est_p:.2f} < {MIN_NET_PROFIT:.2f}")
                continue

            # Размещаем лимитник postOnly
            try:
                oid, pr, q = place_postonly_limit(sym, "Buy", entry_px, qty_alloc)
                st["open_order"] = {"id": oid, "price": pr, "qty": q, "side":"Buy", "ts": time.time(),
                                    "tp1": tp1, "tp2": tp2}
                save_state()
                log_msg(f"🟢 BUY (maker) {sym} @ {pr:.6f}, qty={q}, TP≈{tp_px:.6f}", tg=True)
            except Exception as e:
                log_msg(f"{sym}: BUY place failed: {e}", True)

        # === Проверка исполнений ===
        # Быстрый способ: пробуем получить список активных ордеров и сверить filled
        # (в REST у Bybit нет прямого поля filledQty в ордере get_open_orders — поэтому:
        #  если ордера больше нет в open_orders → считаем исполненным/отменённым, проверяем позицию по балансу)
        try:
            if st["open_order"]:
                oid = st["open_order"]["id"]
                olist = api_call(session.get_open_orders, category="spot", symbol=sym)
                still_open = any(o.get("orderId")==oid for o in olist.get("result", {}).get("list", []))
                if not still_open:
                    # либо исполнился, либо отменён — проверим баланс монеты
                    usdt_bal, by2 = get_balances_cache()
                    cbal = coin_balance(by2, sym)
                    if cbal >= st["open_order"]["qty"]*0.999:  # исполнено
                        buy_px = st["open_order"]["price"]
                        tp1 = st["open_order"]["tp1"]; tp2 = st["open_order"]["tp2"]
                        # ставим ТП(ы) postOnly SELL
                        limits, _, _ = get_limits()
                        if TP_SPLIT[0] > 0 and tp1:
                            qty1 = round_to_step(st["open_order"]["qty"]*TP_SPLIT[0], limits[sym]["qty_step"])
                            try:
                                oid1, pr1, q1 = place_postonly_limit(sym, "Sell", tp1*(1+MAKER_OFFSET_BP/10000.0), qty1)
                                # сохраним позицию
                                STATE[sym]["positions"].append({"buy": buy_px, "qty": q1, "tp1": pr1, "id_tp1": oid1, "time": datetime.datetime.now().isoformat()})
                            except Exception as e:
                                logging.warning(f"{sym}: TP1 place failed: {e}")
                        if TP_SPLIT[1] > 0 and tp2:
                            qty2 = round_to_step(st["open_order"]["qty"]*TP_SPLIT[1], limits[sym]["qty_step"])
                            try:
                                oid2, pr2, q2 = place_postonly_limit(sym, "Sell", tp2*(1+MAKER_OFFSET_BP/10000.0), qty2)
                                STATE[sym]["positions"].append({"buy": buy_px, "qty": q2, "tp1": pr2, "id_tp1": oid2, "time": datetime.datetime.now().isoformat()})
                            except Exception as e:
                                logging.warning(f"{sym}: TP2 place failed: {e}")

                        st["open_order"] = None
                        save_state()
                        log_msg(f"✅ Исполнение входа {sym} @ {buy_px:.6f}. ТП выставлены.", True)
                    else:
                        # отменили — просто очистим
                        st["open_order"] = None
                        save_state()
        except Exception as e:
            logging.warning(f"{sym}: fill check error: {e}")

    cycle_count += 1

    # Ежедневный отчёт
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== ENTRY ====================
def on_start_banner():
    # рассчитать номинал по монетам (для красоты)
    try:
        usdt, by = get_balances_cache()
        total_nom = 0.0
        for s in SYMBOLS:
            df = get_kline(s, "1", 3)
            if df.empty: continue
            total_nom += coin_balance(by, s) * df["c"].iloc[-1]
        send_tg("✅ Состояние загружено из Redis")
        send_tg("🟢 Бот работает. Maker‑режим, фильтры ослаблены, TP≥$1 чистыми.")
        logging.info(f"📊 Номинал: ${total_nom:.2f}")
    except Exception as e:
        logging.info(f"on_start banner err: {e}")

if __name__ == "__main__":
    load_state()
    get_limits()
    on_start_banner()

    # фоновый поток переката лимитников
    th = threading.Thread(target=rollover_worker, daemon=True)
    th.start()

    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
