# -*- coding: utf-8 -*-
# Bybit Spot bot (trade_v3_redis.py)
# - Хранилище состояния: Redis (с fallback на авто-восстановление из истории сделок)
# - Комиссии: 2× taker 0.0018 (как на скрине)
# - Решения по ЗАКРЫТОЙ свече; kline отсортированы по времени
# - TP по ATR + трейлинг-стоп по ATR; жёсткий SL
# - Подробные логи (почему купили/продали/пропустили), TG-уведомления: старт/restore/buy/sell/SL/TP/trail/daily
# - Ежедневный отчёт по монетам с PnL
#
# Требуемые ENV:
# BYBIT_API_KEY, BYBIT_API_SECRET, TG_TOKEN, CHAT_ID, REDIS_URL (redis://[:pass]@host:port/db)

import os, time, math, logging, datetime, json, traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

# --------------- ENV / CONFIG ---------------

load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN   = os.getenv("TG_TOKEN")
CHAT_ID    = os.getenv("CHAT_ID")
REDIS_URL  = os.getenv("REDIS_URL")

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

# Комиссии (taker с твоего скрина)
TAKER_FEE = 0.0018  # 0.18%

# Риск/параметры
RESERVE_BALANCE   = 1.0     # USDT, не трогаем
MAX_TRADE_USDT    = 35.0    # верхний предел бюджета на 1 покупку
MAX_DRAWDOWN      = 0.10    # усреднение до -10%
MAX_AVERAGES      = 3
STOP_LOSS_PCT     = 0.03    # 3% жёсткий SL от входа
MIN_PROFIT_PCT    = 0.005   # 0.5% от ноторнала как вариант цели
MIN_ABS_PNL       = 3.0     # альтернативная метрика ($)
MIN_NET_PROFIT    = 1.50    # альтернативная метрика ($)
MIN_NET_ABS_USD   = 1.00    # ЖЁСТКИЙ пол: чистая прибыль >= $1

# TP/TS по ATR
TP_ATR_MULT       = 1.2
TRAIL_MULTIPLIER  = 1.5
USE_TRAILING      = True

INTERVAL = "1"   # минутки
STATE_KEY = "bybit_spot_bot_state_v3"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR   = 22
DAILY_REPORT_MINUTE = 30

# анти-спам/лимиты
WALLET_CACHE_TTL    = 5.0
REQUEST_BACKOFF     = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN     = 90.0
HTTP_TIMEOUT        = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"),
              logging.StreamHandler()]
)

# --------------- TG HELPERS ---------------

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=HTTP_TIMEOUT
        )
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def log_event(msg, to_tg=False):
    logging.info(msg)
    if to_tg:
        send_tg(msg)

# --------------- SESSION / STATE / REDIS ---------------

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
rds = redis.Redis.from_url(REDIS_URL) if REDIS_URL else None

STATE = {}
LIMITS = {}  # symbol -> {min_qty, qty_step, min_amt}
LAST_REPORT_DATE = None
_last_err_ts = 0.0

def _state_template():
    return {
        "positions": [],  # [{buy_price, qty, buy_qty_gross, tp, peak, trail, hard_sl}]
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    }

def load_state_from_redis():
    global STATE
    if not rds:
        return False
    raw = rds.get(STATE_KEY)
    if not raw:
        return False
    try:
        STATE = json.loads(raw)
        # убедимся, что все символы есть
        for s in SYMBOLS:
            STATE.setdefault(s, _state_template())
        return True
    except Exception as e:
        logging.error(f"load_state_from_redis error: {e}")
        return False

def save_state():
    try:
        if rds:
            rds.set(STATE_KEY, json.dumps(STATE, ensure_ascii=False))
        else:
            # запасной вариант на случай отсутствия Redis
            with open("state.json", "w", encoding="utf-8") as f:
                json.dump(STATE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"save_state error: {e}")

def init_state():
    global STATE
    ok = load_state_from_redis()
    if ok:
        log_event("🚀 Бот стартует. Состояние: REDIS", to_tg=True)
    else:
        STATE = {}
        for s in SYMBOLS:
            STATE[s] = _state_template()
        save_state()
        log_event("🚀 Бот стартует. Состояние: FRESH (попытаемся восстановиться из истории сделок)", to_tg=True)

# --------------- BYBIT HELPERS ---------------

def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "rate" in msg.lower() or "403" in msg or "10006" in msg:
                logging.info(f"Rate-limit/backoff {delay:.1f}s: {msg}")
                time.sleep(delay)
                delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)
                continue
            raise

_wallet_cache = {"ts": 0.0, "coins": None}

def get_wallet(force=False):
    if (not force and _wallet_cache["coins"] is not None and
        time.time()-_wallet_cache["ts"] < WALLET_CACHE_TTL):
        return _wallet_cache["coins"]
    r = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = r["result"]["list"][0]["coin"]
    _wallet_cache.update(ts=time.time(), coins=coins)
    return coins

def usdt_balance(coins):
    return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))

def coin_balance(coins, sym):
    base = sym.replace("USDT", "")
    return float(next((c["walletBalance"] for c in coins if c["coin"] == base), 0.0))

def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        if item["symbol"] in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[item["symbol"]] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0))
            }
    logging.info(f"Loaded limits: {LIMITS}")

def round_step(qty: float, step: float) -> float:
    try:
        exp = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exp)) / 10**abs(exp)
    except Exception:
        return qty

# --------------- MARKET DATA / SIGNAL ---------------

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=200)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    for col in ["o","h","l","c","vol","ts"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("ts").reset_index(drop=True)
    return df

def signal(df):
    """Возвращает ('buy'|'sell'|'none', atr, info) по ЗАКРЫТОЙ свече."""
    if df.empty or len(df) < 60:
        return "none", 0.0, "insufficient candles"
    close = df["c"]
    ema9  = EMAIndicator(close, 9).ema_indicator()
    ema21 = EMAIndicator(close,21).ema_indicator()
    rsi9  = RSIIndicator(close, 9).rsi()
    macd  = MACD(close=close)
    macd_v, macd_s = macd.macd(), macd.macd_signal()
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()

    i = -2  # закрытая свеча
    info = (f"EMA9={ema9.iloc[i]:.5f}, EMA21={ema21.iloc[i]:.5f}, "
            f"RSI9={rsi9.iloc[i]:.2f}, MACD={macd_v.iloc[i]:.5f}, SIG={macd_s.iloc[i]:.5f}")
    if ema9.iloc[i] > ema21.iloc[i] and rsi9.iloc[i] > 50 and macd_v.iloc[i] > macd_s.iloc[i]:
        return "buy", float(atr.iloc[i]), info
    if ema9.iloc[i] < ema21.iloc[i] and rsi9.iloc[i] < 50 and macd_v.iloc[i] < macd_s.iloc[i]:
        return "sell", float(atr.iloc[i]), info
    return "none", float(atr.iloc[i]), info

# --------------- QTY / PRE-ORDER GUARD ---------------

def budget_qty(sym: str, price: float, avail_usdt: float) -> float:
    """К-во под покупку с учётом бюджета, шага и min_amt."""
    if sym not in LIMITS:
        return 0.0
    limits = LIMITS[sym]
    budget = min(avail_usdt, MAX_TRADE_USDT)
    if budget <= 0:
        return 0.0
    q = round_step(budget / price, limits["qty_step"])
    if q < limits["min_qty"] or q*price < limits["min_amt"]:
        return 0.0
    return q

def can_place_buy(sym: str, q: float, price: float, usdt_free: float) -> bool:
    if q <= 0: return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q*price < lm["min_amt"]:
        return False
    need = q * price * (1 + TAKER_FEE)  # с запасом под комиссию
    return need <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)

def can_place_sell(sym: str, q_net: float, price: float, coin_bal_now: float) -> bool:
    if q_net <= 0: return False
    lm = LIMITS[sym]
    if q_net < lm["min_qty"] or q_net * price < lm["min_amt"]:
        return False
    return q_net <= coin_bal_now + 1e-12

# --------------- POS / PNL ---------------

def append_pos(sym, price, qty_gross, atr):
    """Создаёт позицию: net qty уже после комиссии BUY; TP и трейлинг по ATR."""
    qty_net = qty_gross * (1 - TAKER_FEE)
    tp  = price + TP_ATR_MULT * atr
    peak = price
    trail = price - TRAIL_MULTIPLIER * atr if USE_TRAILING else None
    hard_sl = price * (1 - STOP_LOSS_PCT)
    STATE[sym]["positions"].append({
        "buy_price": price,
        "qty": qty_net,                 # после комиссии покупки
        "buy_qty_gross": qty_gross,     # что реально купили
        "tp": tp,
        "peak": peak,
        "trail": trail,
        "hard_sl": hard_sl
    })
    save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    """Net PnL после двух комиссий."""
    cost     = buy_price * buy_qty_gross               # отдано USDT на вход
    proceeds = price * qty_net * (1 - TAKER_FEE)       # получим на выходе
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    """Минимальная чистая прибыль: min(ABS, NET_PROFIT, PCT) но не ниже $1."""
    pct_req  = price * qty_net * MIN_PROFIT_PCT
    base_req = min(MIN_ABS_PNL, MIN_NET_PROFIT, pct_req)
    return max(MIN_NET_ABS_USD, base_req)

# --------------- REPORT ---------------

def daily_report():
    try:
        coins = get_wallet(True)
        by = {c["coin"]: float(c["walletBalance"]) for c in coins}
        lines = ["📊 Daily Report " + str(datetime.date.today()),
                 f"USDT: {by.get('USDT',0.0):.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT","")
            bal  = by.get(base, 0.0)
            price = float(get_kline(sym)["c"].iloc[-1])
            val = price*bal
            s = STATE[sym]
            cur_q = sum(p["qty"] for p in s["positions"])
            lines.append(f"{sym}: balance={bal:.6f} (~{val:.2f} USDT), "
                         f"trades={s['count']}, pnl={s['pnl']:.2f}, "
                         f"maxDD={s['max_drawdown']*100:.2f}%, curPosQty={cur_q:.6f}")
        send_tg("\n".join(lines))
    except Exception as e:
        logging.info(f"daily_report error: {e}")

# --------------- RESTORE FROM TRADES (если Redis пуст) ---------------

def _fetch_fills(sym, days=30, limit=200):
    since_ts = int((time.time() - days*86400) * 1000)
    fills = []
    cursor = None
    while True:
        r = _safe_call(session.get_execution_list, category="spot",
                       symbol=sym, startTime=since_ts, limit=limit, cursor=cursor)
        lst = r.get("result", {}).get("list", [])
        if not lst: break
        fills.extend(lst)
        cursor = r.get("result", {}).get("nextPageCursor")
        if not cursor: break
    return fills

def _vwap_for_open(sym, qty_now, days=30):
    """Возвращает (qty_open, vwap). Простейший FIFO по покупкам-скашенным продажами."""
    if qty_now <= 0:
        return 0.0, 0.0
    fills = _fetch_fills(sym, days=days)
    if not fills:
        return qty_now, 0.0
    buys, sells = [], []
    for f in fills:
        side = f.get("side") or f.get("orderSide")
        px   = float(f.get("execPrice") or f.get("price"))
        q    = float(f.get("execQty") or f.get("qty"))
        if side == "Buy":
            buys.append((px, q))
        elif side == "Sell":
            sells.append((px, q))
    sell_q = sum(q for _, q in sells)
    remaining = []
    for px, q in buys:
        if sell_q <= 0:
            remaining.append((px, q))
        else:
            take = min(q, sell_q)
            left = q - take
            sell_q -= take
            if left > 0:
                remaining.append((px, left))
    rem_q = sum(q for _, q in remaining)
    if rem_q <= 0:
        return qty_now, 0.0
    scale = min(1.0, qty_now / rem_q)
    vw_cost = sum(px * q * scale for px, q in remaining)
    vwap = vw_cost / (rem_q * scale)
    return qty_now, vwap

def restore_positions_if_needed():
    # если Redis пуст/свежий — построим позиции из баланса + истории сделок
    empty = all(len(STATE[s]["positions"]) == 0 for s in SYMBOLS)
    if not empty:
        return
    restored_msgs = []
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: 
                continue
            price = df["c"].iloc[-1]
            atr = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
            coins = get_wallet(True)
            bal = coin_balance(coins, sym)
            lm = LIMITS.get(sym, {})
            if price and bal*price >= lm.get("min_amt", 0.0) and bal >= lm.get("min_qty", 0.0):
                qty_now, vwap = _vwap_for_open(sym, bal, days=30)
                q_net = round_step(qty_now, lm.get("qty_step", 1.0))
                if q_net <= 0:
                    continue
                buy_price = vwap if vwap > 0 else price
                STATE[sym]["positions"].clear()
                STATE[sym]["avg_count"] = 0
                STATE[sym]["last_sell_price"] = 0.0
                buy_gross = q_net/(1-TAKER_FEE)
                STATE[sym]["positions"].append({
                    "buy_price": buy_price,
                    "qty": q_net,
                    "buy_qty_gross": buy_gross,
                    "tp": buy_price + TP_ATR_MULT*atr,
                    "peak": price,
                    "trail": price - TRAIL_MULTIPLIER*atr if USE_TRAILING else None,
                    "hard_sl": buy_price*(1-STOP_LOSS_PCT)
                })
                restored_msgs.append(f"{sym}: qty={q_net:.8f} @ {buy_price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    save_state()
    if restored_msgs:
        send_tg("♻️ Восстановлены позиции из истории: \n" + "\n".join(restored_msgs))
    else:
        send_tg("ℹ️ Позиции не найдены для восстановления (баланс пуст или нет сделок).")

# --------------- MAIN CYCLE ---------------

def trade_cycle():
    global LAST_REPORT_DATE, _last_err_ts
    try:
        coins = get_wallet(True)
        usdt  = usdt_balance(coins)
    except Exception as e:
        now = time.time()
        if now - _last_err_ts > TG_ERR_COOLDOWN:
            send_tg(f"Ошибка баланса: {e}")
            _last_err_ts = now
        return

    avail = max(0.0, usdt - RESERVE_BALANCE)
    logging.info(f"💰 USDT={usdt:.2f} | Доступно={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                logging.info(f"[{sym}] нет свечей — пропуск")
                continue

            sig, atr_sig, sig_info = signal(df)
            price = df["c"].iloc[-1]
            atr_last = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-2]
            state = STATE[sym]
            lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            value = coin_bal*price

            logging.info(f"[{sym}] sig={sig} | {sig_info} | price={price:.6f}, "
                         f"value={value:.2f}, pos={len(state['positions'])}")

            # обновим max DD
            if state["positions"]:
                total_q = sum(p["qty"] for p in state["positions"])
                if total_q > 0:
                    avg_entry = sum(p["buy_price"]*p["qty"] for p in state["positions"]) / total_q
                    dd = (avg_entry - price)/avg_entry
                    if dd > state["max_drawdown"]:
                        state["max_drawdown"] = dd

            # -------- SELL / TP / SL / TRAIL ----------
            new_pos = []
            for p in state["positions"]:
                b   = p["buy_price"]
                q_n = round_step(p["qty"], lm["qty_step"])
                q_g = p.get("buy_qty_gross", q_n/(1-TAKER_FEE))
                tp  = p["tp"]
                hard_sl = p.get("hard_sl", b*(1-STOP_LOSS_PCT))

                if not can_place_sell(sym, q_n, price, coin_bal):
                    logging.info(f"[{sym}] 🔸Hold: нельзя продать — ниже лимитов (min_qty={lm['min_qty']}, min_amt={lm['min_amt']})")
                    new_pos.append(p)
                    continue

                pnl  = net_pnl(price, b, q_n, q_g)
                need = min_net_required(price, q_n)

                # SL
                if price <= hard_sl:
                    _safe_call(session.place_order, category="spot", symbol=sym,
                               side="Sell", orderType="Market", qty=str(q_n))
                    msg = (f"❗SL SELL {sym} @ {price:.6f}, qty={q_n:.8f}, "
                           f"netPnL={pnl:.2f} | reason=price<=SL({hard_sl:.6f})")
                    log_event(msg, to_tg=True)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                    continue

                # Trailing
                if USE_TRAILING:
                    p["peak"] = max(p.get("peak", price), price)
                    new_trail = max(p.get("trail", price - TRAIL_MULTIPLIER*atr_last),
                                    p["peak"] - TRAIL_MULTIPLIER*atr_last)
                    if abs(new_trail - p.get("trail", 0)) > 1e-12:
                        logging.info(f"[{sym}] 📈 Trail: {p.get('trail')} → {new_trail}")
                        p["trail"] = new_trail
                    if price <= p["trail"]:
                        _safe_call(session.place_order, category="spot", symbol=sym,
                                   side="Sell", orderType="Market", qty=str(q_n))
                        msg = (f"🟠 TRAIL SELL {sym} @ {price:.6f}, qty={q_n:.8f}, netPnL={pnl:.2f} "
                               f"| reason=price<=trail({p['trail']:.6f})")
                        log_event(msg, to_tg=True)
                        state["pnl"] += pnl
                        state["last_sell_price"] = price
                        state["avg_count"] = 0
                        coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                        continue

                # Take Profit
                if price >= tp and pnl >= need:
                    _safe_call(session.place_order, category="spot", symbol=sym,
                               side="Sell", orderType="Market", qty=str(q_n))
                    msg = (f"✅ TP SELL {sym} @ {price:.6f}, qty={q_n:.8f}, netPnL={pnl:.2f} "
                           f"| need>={need:.2f}, tp={tp:.6f}")
                    log_event(msg, to_tg=True)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                    continue

                # Обновить TP (не «убегает»)
                new_tp = max(tp, b + TP_ATR_MULT*atr_last)
                if abs(new_tp - tp) > 1e-12:
                    logging.info(f"[{sym}] 🎯 TP: {tp:.6f} → {new_tp:.6f}")
                p["tp"] = new_tp
                new_pos.append(p)

            state["positions"] = new_pos

            # -------- BUY / AVERAGE ----------
            if sig == "buy":
                # усреднение
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(x["qty"] for x in state["positions"])
                    avg_price = sum(x["qty"]*x["buy_price"] for x in state["positions"]) / total_q
                    dd = (price - avg_price)/avg_price
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN:
                        q_gross = budget_qty(sym, price, avail)
                        if q_gross <= 0:
                            logging.info(f"[{sym}] ❌ Skip avg: бюджет/лимиты не позволяют (avail={avail:.2f})")
                        elif can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", qty=str(q_gross))
                            append_pos(sym, price, q_gross, atr_sig)
                            state["count"] += 1; state["avg_count"] += 1
                            log_event(f"🟢 BUY(avg) {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f} "
                                      f"| reason=sig=buy, dd={dd:.4f}, {sig_info}", to_tg=True)
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip avg: не прошли проверку can_place_buy")
                    else:
                        logging.info(f"[{sym}] 🔸Skip avg: dd {dd:.4f} вне диапазона (-{MAX_DRAWDOWN:.2f})")
                # первичный вход
                elif not state["positions"]:
                    # защита от обратного входа
                    if state["last_sell_price"] and abs(price - state["last_sell_price"])/price < 0.003:
                        logging.info(f"[{sym}] 🔸Skip buy: близко к последней продаже (anti-churn)")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        if q_gross <= 0:
                            logging.info(f"[{sym}] ❌ Skip buy: бюджет/мин-лимиты (avail={avail:.2f}, min_amt={lm['min_amt']}, min_qty={lm['min_qty']})")
                        elif can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", qty=str(q_gross))
                            append_pos(sym, price, q_gross, atr_sig)
                            state["count"] += 1
                            log_event(f"🟢 BUY {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f} "
                                      f"| reason=sig=buy, {sig_info}", to_tg=True)
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip buy: не прошли проверку can_place_buy")
            else:
                if not state["positions"]:
                    logging.info(f"[{sym}] 🔸No buy: signal={sig}, info=({sig_info})")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] Ошибка цикла: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                send_tg(f"[{sym}] Ошибка цикла: {e}")
                _last_err_ts = now

    save_state()

    # ежедневный отчёт
    now = datetime.datetime.now()
    if (now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and
        LAST_REPORT_DATE != now.date()):
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# --------------- RUN ---------------

if __name__ == "__main__":
    log_event("🚀 Bot starting (v3.redis)", to_tg=True)
    init_state()
    load_symbol_limits()
    restore_positions_if_needed()
    send_tg(f"⚙️ Params: TAKER={TAKER_FEE}, MAX_TRADE={MAX_TRADE_USDT}, "
            f"TPxATR={TP_ATR_MULT}, TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, "
            f"DD={MAX_DRAWDOWN*100:.0f}% | Redis={'ON' if rds else 'OFF'}")

    while True:
        try:
            trade_cycle()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"Global error: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                send_tg(f"Global error: {e}")
                _last_err_ts = now
        time.sleep(LOOP_SLEEP)
