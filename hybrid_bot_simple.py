# -*- coding: utf-8 -*-
import os, time, math, logging, datetime, json, traceback
from decimal import Decimal, getcontext
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import requests
import redis

# ==================== ENV ====================
load_dotenv()
API_KEY     = os.getenv("BYBIT_API_KEY")
API_SECRET  = os.getenv("BYBIT_API_SECRET")
TG_TOKEN    = os.getenv("TG_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
REDIS_URL   = os.getenv("REDIS_URL")

# ==================== CONFIG ====================
# Комиссии с твоего скрина (спот): мейкер 0.10%, тейкер 0.18%
MAKER_FEE = 0.0010
TAKER_FEE = 0.0018

# минимальная чистая прибыль после ВСЕХ комиссий — не трогаем
MIN_NET_PROFIT_USD = 1.0

# ордера оставляем maker postOnly
POST_ONLY = True

# лимиты риска/аллокаций
RESERVE_BALANCE   = 1.0            # USDT в резерве (не трогаем)
MAX_TRADE_USDT    = 105.0          # кап на сделку
STOP_LOSS_PCT     = 0.008          # 0.8% стоп — как было

# символы
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

# включить подробные уведомления
TG_VERBOSE = True
TG_ERRORS_COOLDOWN_SEC = 300       # не спамить одинаковой ошибкой чаще, чем раз в 5 минут

# логгер
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

getcontext().prec = 28

# ==================== SESSIONS & STATE ====================
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}
LAST_REPORT_DATE = None
cycle_count = 0

# локальные кэши
_LIMITS_MEM = None
_LIMITS_OK  = False
_BUY_BLOCKED_REASON = ""
SKIP_LOG_TIMESTAMPS = {}
LAST_ERR_SENT = {}  # для антиспама ошибок в TG

# ==================== UTILS ====================
def send_tg(text: str):
    if not (TG_TOKEN and CHAT_ID): 
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")

def send_tg_dedup(key: str, text: str, cooldown=TG_ERRORS_COOLDOWN_SEC):
    now = time.time()
    last = LAST_ERR_SENT.get(key, 0)
    if now - last >= cooldown:
        LAST_ERR_SENT[key] = now
        send_tg(text)

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg and TG_VERBOSE:
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
        logging.warning(f"Redis save failed: {e}")

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty, tp1, tp2, time}]
            "pnl": 0.0,
            "last_stop_time": "",
            "open_buy_id": None,
            "open_tp_ids": []
        })

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    ensure_state_consistency()
    log_msg("✅ Состояние загружено из Redis", tg=True)

# ==================== API HELPERS ====================
def api_call(fn, *args, **kwargs):
    wait = 0.35
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err = f"{e}"
            logging.warning(f"API retry {fn.__name__} attempt={attempt+1} wait={wait:.2f}s error={err}")
            time.sleep(wait)
            wait = min(wait * 2.0, 8.0)
    raise RuntimeError(f"API call failed after retries: {fn.__name__}")

# ==================== LIMITS (tickSize/qtyStep) ====================
LIMITS_REDIS_KEY = "limits_cache_v2"
LIMITS_TTL_SEC   = 12 * 60 * 60

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

def _load_symbol_limits_from_api():
    r = api_call(session.get_instruments_info, category="spot")
    lst = r["result"]["list"]
    out = {}
    for item in lst:
        sym = item["symbol"]
        if sym not in SYMBOLS: 
            continue
        pf = item.get("priceFilter", {}) or {}
        lf = item.get("lotSizeFilter", {}) or {}
        out[sym] = {
            "min_qty": float(lf.get("minOrderQty", 0)),
            "qty_step": float(lf.get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0)),
            "tick_size": float(pf.get("tickSize", 0.0001)),  # ВАЖНО!
        }
    return out

def get_limits():
    """Ленивая загрузка лимитов + tickSize."""
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

# ==================== DATA ====================
def get_kline(sym, interval="1", limit=120):
    r = api_call(session.get_kline, category="spot", symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_orderbook_top(sym):
    r = api_call(session.get_orderbook, category="spot", symbol=sym, limit=1)
    bids = r["result"]["b"] or []
    asks = r["result"]["a"] or []
    bid = float(bids[0][0]) if bids else 0.0
    ask = float(asks[0][0]) if asks else 0.0
    return bid, ask

def get_balances_cache():
    coins = api_call(session.get_wallet_balance, accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def coin_from_symbol(sym):  # "XRPUSDT" -> "XRP"
    return sym[:-4]

def get_coin_balance(by, sym):
    return float(by.get(coin_from_symbol(sym), 0.0))

# ==================== ROUNDING ====================
def round_to_step(x, step):
    if step == 0: return x
    q = Decimal(str(x)); s = Decimal(str(step))
    return float((q // s) * s)

def round_price_to_tick(price, tick):
    if tick <= 0: return price
    d = Decimal(str(price)) / Decimal(str(tick))
    d = d.quantize(Decimal("1."), rounding="ROUND_DOWN")
    return float(d * Decimal(str(tick)))

# ==================== SIGNALS (слегка ослаблены) ====================
def signal(sym):
    """
    Ослабления:
    - RSI порог на 1m снижен: было ~48 → 46
    - допускаем вход, если 2 из 3 (EMA, RSI, MACD) ИЛИ чистый крест EMA (rsi>44)
    Плюс берём ATR на 5m и 15m для расчёта tp-множителя (среднее).
    """
    d1 = get_kline(sym, "1", 200)
    if len(d1) < 50: 
        return "none", 0.0, {}

    ema9  = EMAIndicator(d1["c"], 9).ema_indicator()
    ema21 = EMAIndicator(d1["c"], 21).ema_indicator()
    rsi9  = RSIIndicator(d1["c"], 9).rsi()
    macd  = MACD(close=d1["c"])
    m_line, m_sig = macd.macd(), macd.macd_signal()

    last = len(d1) - 1
    ema9_l, ema21_l = float(ema9.iloc[last]), float(ema21.iloc[last])
    ema9_p, ema21_p = float(ema9.iloc[last-1]), float(ema21.iloc[last-1])
    rsi_l           = float(rsi9.iloc[last])
    macd_l, macd_s  = float(m_line.iloc[last]), float(m_sig.iloc[last])

    # ATR 5m & 15m
    d5  = get_kline(sym, "5", 200)
    d15 = get_kline(sym, "15", 200)
    atr5  = AverageTrueRange(d5["h"], d5["l"], d5["c"], 14).average_true_range().iloc[-1] if not d5.empty else 0.0
    atr15 = AverageTrueRange(d15["h"], d15["l"], d15["c"], 14).average_true_range().iloc[-1] if not d15.empty else 0.0
    atr = float((atr5 + atr15) / 2.0) if (atr5 and atr15) else float(atr5 or atr15 or 0.0)

    price = float(d1["c"].iloc[-1])

    two_of_three_buy = sum([
        1 if ema9_l > ema21_l else 0,
        1 if rsi_l > 46 else 0,
        1 if macd_l > macd_s else 0
    ]) >= 2

    ema_cross_up = (ema9_p <= ema21_p) and (ema9_l > ema21_l) and (rsi_l > 44)

    two_of_three_sell = sum([
        1 if ema9_l < ema21_l else 0,
        1 if rsi_l < 54 else 0,
        1 if macd_l < macd_s else 0
    ]) >= 2

    ema_cross_down = (ema9_p >= ema21_p) and (ema9_l < ema21_l) and (rsi_l < 56)

    # tp-множитель по волатильности (чуть «смягчил» нижнюю границу)
    vol_pct = atr / price if price > 0 else 0.0
    if vol_pct < 0.004:    # <0.4%
        tp_mult = 1.30
    elif vol_pct < 0.008:  # 0.4–0.8%
        tp_mult = 1.55
    else:
        tp_mult = 1.80

    info = {
        "price": price, "ema9": ema9_l, "ema21": ema21_l, "rsi": rsi_l,
        "macd": macd_l, "sig": macd_s, "atr": atr, "tp_mult": tp_mult
    }

    if two_of_three_buy or ema_cross_up:
        return "buy", atr, info
    if two_of_three_sell or ema_cross_down:
        return "sell", atr, info
    return "none", atr, info

# ==================== PNL / FEES ====================
def est_trade_pnl_buy_maker_sell_maker(price_buy, price_sell, qty):
    """Оценка чистой прибыли с обеих сторон как maker."""
    cost = price_buy * qty
    buy_fee  = cost * MAKER_FEE
    sell_fee = price_sell * qty * MAKER_FEE
    return (price_sell - price_buy) * qty - (buy_fee + sell_fee)

# ==================== ORDERS ====================
def place_limit_postonly(sym, side, qty, price):
    """Ставит лимитный PostOnly c корректным округлением."""
    limits, _, _ = get_limits()
    tick = limits.get(sym, {}).get("tick_size", 0.0001)
    step = limits.get(sym, {}).get("qty_step", 1.0)

    px  = round_price_to_tick(price, tick)
    q   = round_to_step(qty, step)

    if q <= 0:
        raise RuntimeError(f"qty rounded to zero (step={step})")
    if px <= 0:
        raise RuntimeError(f"price rounded to zero (tick={tick})")

    r = api_call(session.place_order,
                 category="spot",
                 symbol=sym,
                 side="Buy" if side.lower()=="buy" else "Sell",
                 orderType="Limit",
                 qty=str(q),
                 price=str(px),
                 timeInForce="PostOnly" if POST_ONLY else "GTC")
    return r["result"]["orderId"], q, px

# ==================== START/RESTORE ====================
def reconcile_positions_on_start():
    """Пишем краткий отчёт в TG."""
    usdt, by = get_balances_cache()
    limits, ok, _ = get_limits()
    lines = []
    nominal = 0.0

    for sym in SYMBOLS:
        bal = get_coin_balance(by, sym)
        price = get_kline(sym, "1", 2)["c"].iloc[-1]
        nominal += bal * price
        if bal > 0:
            lines.append(f"• {sym}: баланс {bal:.6f} ~ ${bal*price:.2f}")
        else:
            lines.append(f"• {sym}: позиций нет")

        # чистим локальные «позиции» (без углубления в открытые ордера)
        STATE[sym]["positions"] = []
        STATE[sym]["open_buy_id"] = None
        STATE[sym]["open_tp_ids"] = []

    save_state()
    send_tg("🚀 Бот запущен (восстановление)\n"
            + "\n".join(lines) + f"\n💰 Доступно USDT: {usdt:.2f}\n📊 Номинал: ${nominal:.2f}")

# ==================== MAIN TRADE ====================
def trade():
    global LAST_REPORT_DATE
    limits, limits_ok, buy_blocked_reason = get_limits()
    usdt, by = get_balances_cache()

    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0.0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}, limits_ok={limits_ok}")

    for sym in SYMBOLS:
        sig, atr, info = signal(sym)
        bid, ask = get_orderbook_top(sym)
        bal_val = get_coin_balance(by, sym) * info["price"]
        logging.info(
            f"[{sym}] sig={sig}, price={info['price']:.6f}, bal_val={bal_val:.2f}, "
            f"pos={len(STATE[sym]['positions'])} | bid={bid:.6f} ask={ask:.6f} | "
            f"EMA9={info['ema9']:.6f} EMA21={info['ema21']:.6f} | RSI={info['rsi']:.2f} "
            f"MACD={info['macd']:.6f} SIG={info['sig']:.6f} | ATR(5/15m)={atr:.6f} "
            f"({(atr/info['price']*100) if info['price'] else 0:.2f}%) | tp_mult={info['tp_mult']:.2f}"
        )

        # ПРОДАЖИ по TP/SL — если есть открытые позиции (простая модель без отслеживания ордеров)
        # В этой версии делаем продажи только ордерами TP, которые мы выставляем при покупке.
        # Стоп-лосс на PostOnly смысла мало — его лучше ставить рыночным, но по твоему ТЗ оставляем только maker‑логикой покупки/продажи (TP).
        # Поэтому SL мягкий: если цена провалилась глубоко — просто не докупаем.

        # ПОКУПКА (только если нет позиций и сигнал buy)
        if sig == "buy" and not STATE[sym]["positions"]:
            if not limits_ok:
                if should_log_skip(sym, "limits"):
                    logging.info(f"{sym}: Пропуск BUY — {buy_blocked_reason}")
                    send_tg_dedup("limits", f"{sym}: ⛔️ Покупка заблокирована: {buy_blocked_reason}")
                continue

            # аллокация
            alloc = min(per_sym, MAX_TRADE_USDT)
            if alloc < limits[sym]["min_amt"]:
                if should_log_skip(sym, "funds"):
                    logging.info(f"{sym}: Пропуск BUY — мало USDT (alloc={alloc:.2f} < min_amt={limits[sym]['min_amt']})")
                continue

            # расчёт количества по bid (чтобы стоять в стакане как maker)
            price_buy = max(bid, 0.0)
            if price_buy <= 0:
                if should_log_skip(sym, "no_bid"):
                    logging.info(f"{sym}: Пропуск BUY — пустой bid в стакане")
                continue

            qty_raw = alloc / price_buy
            qty = round_to_step(qty_raw, limits[sym]["qty_step"])
            if qty < limits[sym]["min_qty"]:
                if should_log_skip(sym, "minqty"):
                    logging.info(f"{sym}: Пропуск BUY — qty={qty} < min_qty={limits[sym]['min_qty']}")
                continue

            # цели (две ступени как раньше), обязательно округляем к tickSize
            tp1 = price_buy + info["tp_mult"] * atr
            tp2 = tp1 + 0.50 * atr  # небольшая вторая ступень
            tp1 = round_price_to_tick(tp1, limits[sym]["tick_size"])
            tp2 = round_price_to_tick(tp2, limits[sym]["tick_size"])

            # проверка ожидаемого PnL минимум $1
            # считаем по tp1 (консервативно) и на ВСЮ позицию
            est_pnl = est_trade_pnl_buy_maker_sell_maker(price_buy, tp1, qty)
            max_alloc_txt = f"${qty*price_buy:.2f}"
            logging.info(f"[{sym}] BUY-check qty_alloc={alloc:.2f}, need_qty={qty:.8f}, "
                         f"tp1={tp1:.6f}, ppu={tp1-price_buy:.6f}, est_pnl={est_pnl:.2f}, "
                         f"required={MIN_NET_PROFIT_USD:.2f}, max_alloc={max_alloc_txt}")

            if est_pnl < MIN_NET_PROFIT_USD:
                if should_log_skip(sym, "low_pnl"):
                    logging.info(f"{sym}: Пропуск BUY — ожидаемый PnL {est_pnl:.2f} < {MIN_NET_PROFIT_USD:.2f}")
                continue

            # размещаем покупку postOnly по bid
            try:
                buy_id, q_exec, px_exec = place_limit_postonly(sym, "buy", qty, price_buy)
                STATE[sym]["open_buy_id"] = buy_id
                STATE[sym]["positions"] = [{
                    "buy_price": px_exec, "qty": q_exec, "tp1": tp1, "tp2": tp2,
                    "time": datetime.datetime.now().isoformat()
                }]
                save_state()
                msg = (f"✅ BUY (maker) {sym} @ {px_exec:.6f}, qty={q_exec}\n"
                       f"TP1={tp1:.6f}, TP2={tp2:.6f}\n"
                       f"Ожидаемый чистый PnL≈${est_pnl:.2f} (≥ ${MIN_NET_PROFIT_USD:.2f})")
                log_msg(msg, tg=True)
            except Exception as e:
                err = f"{e}\n{traceback.format_exc(limit=1)}"
                logging.error(f"{sym}: BUY failed: {err}")
                send_tg_dedup(f"buyfail-{sym}", f"❌ BUY {sym} не удалось: {e}")

            # после размещения покупки — ставим 2 TP (postOnly) на весь объём (поровну)
            try:
                pos = STATE[sym]["positions"][0]
                q_all = pos["qty"]
                q1 = round_to_step(q_all * 0.5, limits[sym]["qty_step"])
                q2 = round_to_step(q_all - q1,   limits[sym]["qty_step"])
                ids = []

                if q1 >= limits[sym]["min_qty"]:
                    tp1_id, q1f, tp1_px = place_limit_postonly(sym, "sell", q1, pos["tp1"])
                    ids.append(tp1_id)
                if q2 >= limits[sym]["min_qty"]:
                    tp2_id, q2f, tp2_px = place_limit_postonly(sym, "sell", q2, pos["tp2"])
                    ids.append(tp2_id)

                STATE[sym]["open_tp_ids"] = ids
                save_state()
                log_msg(f"{sym}: TP ордера выставлены (postOnly), ids={','.join(ids)}", tg=True)
            except Exception as e:
                logging.error(f"{sym}: TP place failed: {e}")
                send_tg_dedup(f"tpfail-{sym}", f"⚠️ {sym}: постановка TP не удалась: {e}")

    # дневной отчёт один раз в день
    now = datetime.datetime.now()
    global LAST_REPORT_DATE
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# ==================== REPORT ====================
def send_daily_report():
    usdt, by = get_balances_cache()
    lines = ["📊 Ежедневный отчёт:"]
    total_pnl = 0.0
    nominal = 0.0
    for sym in SYMBOLS:
        price = get_kline(sym, "1", 2)["c"].iloc[-1]
        bal = get_coin_balance(by, sym)
        nominal += bal * price
        st = STATE[sym]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP1 {p['tp1']:.6f} | TP2 {p['tp2']:.6f}")
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {sym}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"💰 USDT: {usdt:.2f} | Номинал по монетам: ${nominal:.2f}")
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# ==================== ENTRY ====================
if __name__ == "__main__":
    try:
        init_state()
        # загружаем лимиты заранее, чтобы сразу был tickSize
        get_limits()
        reconcile_positions_on_start()
        log_msg("🟢 Бот работает. Maker-режим, фильтры ослаблены, TP≥$1 чистыми.", tg=True)
        while True:
            try:
                trade()
            except Exception as e:
                logging.error(f"Global error: {e}")
                send_tg_dedup("global", f"❗️ Global error: {e}")
            time.sleep(60)
    except Exception as e:
        logging.error(f"Fatal start error: {e}")
        send_tg(f"❌ Fatal start error: {e}")
