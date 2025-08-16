# -*- coding: utf-8 -*-
# Bybit Spot bot — v3.redis.profitStrict.slipfix
# - Маркет BUY на Bybit Spot: qty по базе (marketUnit="baseCoin") — фикс 170131
# - Жёстко: продаём ТОЛЬКО при net PnL >= $1 после 2х комиссий (ProfitOnly)
# - Буфер на проскальзывание/округление SLIP_BUFFER в анти-170131 проверках
# - Redis-состояние (+ JSON фолбэк), восстановление позиций с кошелька
# - Подробные логи: сигналы, причины buy/avg/skip, TP-трейл, лимиты биржи
# - ТГ-нотисы: старт, восстановление, BUY/AVG/SELL, ошибки, дневной отчёт

import os, time, math, logging, datetime, json, traceback, random
import redis
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ---------- ENV / CONFIG ----------
load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""

# твои пары и параметры
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

# комиссии с твоего скрина (Bybit Spot, обычный уровень): тейкер 0.18%
TAKER_FEE = 0.0018          # 0.18%
# финансовые правила
RESERVE_BALANCE = 1.0       # USDT — не трогаем
MAX_TRADE_USDT  = 35.0      # верх бюджета на 1 покупку
TRAIL_MULTIPLIER= 1.5
MAX_DRAWDOWN    = 0.10      # усредняемся до -10%
MAX_AVERAGES    = 3
STOP_LOSS_PCT   = 0.03      # 3% — но SL только если net >= $1
MIN_PROFIT_PCT  = 0.005     # 0.5% от ноторнала
MIN_ABS_PNL     = 3.0       # оставил
MIN_NET_PROFIT  = 1.50      # порог
MIN_NET_ABS_USD = 1.00      # ГАРАНТИЯ: net >= $1 после 2х комиссий
SLIP_BUFFER     = 0.006     # 0.6% запас под проскальзывание/округления
PROFIT_ONLY     = True      # жёстко — без прибыли не продаём

INTERVAL = "1"
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR   = 22
DAILY_REPORT_MINUTE = 30

# анти-лимиты
WALLET_CACHE_TTL   = 5.0
REQUEST_BACKOFF    = 2.5
REQUEST_BACKOFF_MAX= 30.0
TG_ERR_COOLDOWN    = 90.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"),
              logging.StreamHandler()]
)

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def log_event(msg): logging.info(msg); send_tg(msg)

# ---------- SESSION / REDIS / STATE ----------
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

rds = None
if REDIS_URL:
    try:
        rds = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logging.error(f"Redis connect error: {e}")
        rds = None

STATE = {}
LIMITS = {}  # symbol -> {min_qty, qty_step, min_amt}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

def _state_key(): return "bybit_spot_bot_state_v3"
def _save_state():
    global STATE
    try:
        if rds:
            rds.set(_state_key(), json.dumps(STATE, ensure_ascii=False))
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"save_state error: {e}")

def _load_state():
    global STATE
    # сначала Redis
    if rds:
        s = rds.get(_state_key())
        if s:
            STATE = json.loads(s)
            return "REDIS"
    # потом файл
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        return "FILE"
    except Exception:
        STATE = {}
        return "FRESH"

def init_state():
    src = _load_state()
    log_event(f"🚀 Бот стартует. Состояние: {src}")
    for s in SYMBOLS:
        STATE.setdefault(s, {
            "positions": [],  # {buy_price, qty(net), buy_qty_gross, tp}
            "pnl": 0.0, "count": 0, "avg_count": 0,
            "last_sell_price": 0.0, "max_drawdown": 0.0
        })
    _save_state()

# ---------- BYBIT HELPERS ----------
def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            smsg = str(e)
            if any(x in smsg for x in ["rate", "403", "10006"]):
                logging.info(f"Rate-limit/backoff {delay:.1f}s: {smsg}")
                time.sleep(delay)
                delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)
                continue
            raise

def get_wallet(force=False):
    if not force and _wallet_cache["coins"] is not None and time.time()-_wallet_cache["ts"] < WALLET_CACHE_TTL:
        return _wallet_cache["coins"]
    r = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = r["result"]["list"][0]["coin"]
    _wallet_cache.update(ts=time.time(), coins=coins)
    return coins

def usdt_balance(coins):
    return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))

def coin_balance(coins, sym):
    base = sym.replace("USDT","")
    return float(next((c["walletBalance"] for c in coins if c["coin"]==base), 0.0))

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

# ---------- MARKET DATA / SIGNAL ----------
def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df):
    if df.empty or len(df) < 50: return "none", 0.0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    atr = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
    macd = MACD(close=df["c"])
    df["macd"], df["sig"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.5f},EMA21={last['ema21']:.5f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.5f},SIG={last['sig']:.5f}")
    if last["ema9"] > last["ema21"] and last["rsi"]>50 and last["macd"]>last["sig"]:
        return "buy", float(atr), info
    if last["ema9"] < last["ema21"] and last["rsi"]<50 and last["macd"]<last["sig"]:
        return "sell", float(atr), info
    return "none", float(atr), info

# ---------- QTY / PRE-ORDER GUARD ----------
def budget_qty(sym: str, price: float, avail_usdt: float) -> float:
    """Количество монеты под покупку с учётом бюджета, шага и min_amt."""
    if sym not in LIMITS: return 0.0
    limits = LIMITS[sym]
    budget = min(avail_usdt, MAX_TRADE_USDT)
    if budget <= 0: return 0.0
    q = round_step(budget / price, limits["qty_step"])
    if q < limits["min_qty"] or q*price < limits["min_amt"]:
        return 0.0
    return q

def can_place_buy(sym: str, q: float, price: float, usdt_free: float) -> bool:
    if q <= 0: return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q*price < lm["min_amt"]:
        return False
    need = q*price*(1 + TAKER_FEE + SLIP_BUFFER)  # запас под комсу и проскальз.
    return need <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)

def can_place_sell(sym: str, q_net: float, price: float, coin_bal_now: float) -> bool:
    if q_net <= 0: return False
    lm = LIMITS[sym]
    if q_net < lm["min_qty"] or q_net*price < lm["min_amt"]:
        return False
    return q_net <= coin_bal_now + 1e-12

# ---------- POS / PNL ----------
def append_pos(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    STATE[sym]["positions"].append({
        "buy_price": price,
        "qty": qty_net,                  # после комиссии покупки
        "buy_qty_gross": qty_gross,      # что реально купили
        "tp": tp
    })
    _save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    cost  = buy_price * buy_qty_gross                       # отдано USDT на вход
    proceeds = price * qty_net * (1 - TAKER_FEE)            # получим на выходе
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    pct_req = price * qty_net * MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD, MIN_NET_PROFIT, MIN_ABS_PNL, pct_req)

# ---------- REPORT ----------
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

# ---------- RESTORE FROM WALLET ----------
def restore_positions():
    restored = []
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            price = df["c"].iloc[-1]
            coins = get_wallet(True)
            bal = coin_balance(coins, sym)
            lm = LIMITS.get(sym, {})
            if price and bal*price >= lm.get("min_amt", 0.0) and bal >= lm.get("min_qty", 0.0):
                q_net = round_step(bal, lm.get("qty_step", 1.0))
                atr = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
                tp  = price + TRAIL_MULTIPLIER*atr
                STATE[sym]["positions"] = [{
                    "buy_price": price,
                    "qty": q_net,
                    "buy_qty_gross": q_net/(1-TAKER_FEE),
                    "tp": tp
                }]
                restored.append(f"{sym}: qty={q_net:.8f} @ {price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    _save_state()
    if restored:
        send_tg("♻️ Восстановлены позиции:\n" + "\n".join(restored))
    else:
        send_tg("ℹ️ Позиции не найдены для восстановления (баланс пуст или нет сделок).")

# ---------- ORDER HELPERS (с анти-170131 ретраями) ----------
def _attempt_buy(sym: str, qty_base: float) -> bool:
    """Маркет-BUY c marketUnit='baseCoin' + анти-170131 ретрай."""
    lm = LIMITS[sym]
    qty = round_step(qty_base, lm["qty_step"])
    tries = 4
    while tries > 0 and qty >= lm["min_qty"]:
        try:
            _safe_call(session.place_order,
                       category="spot", symbol=sym,
                       side="Buy", orderType="Market",
                       timeInForce="IOC",
                       marketUnit="baseCoin",   # <<< ключевой фикс
                       qty=str(qty))
            return True
        except Exception as e:
            msg = str(e)
            if "170131" in msg or "Insufficient balance" in msg:
                logging.info(f"[{sym}] 170131 on BUY qty={qty}. Shrink & retry")
                # уменьшаем на 1 шаг или на 0.5% — что больше
                shrink = max(lm["qty_step"], qty*0.005)
                qty = round_step(max(lm["min_qty"], qty - shrink), lm["qty_step"])
                tries -= 1
                time.sleep(0.3)
                continue
            raise
    return False

def _attempt_sell(sym: str, qty_base: float) -> bool:
    lm = LIMITS[sym]
    qty = round_step(qty_base, lm["qty_step"])
    _safe_call(session.place_order, category="spot", symbol=sym,
               side="Sell", orderType="Market", qty=str(qty))
    return True

# ---------- MAIN CYCLE ----------
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

            sig, atr, info = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            value = coin_bal*price

            logging.info(f"[{sym}] sig={sig} | {info} | price={price:.6f}, "
                         f"value={value:.2f}, pos={len(state['positions'])}")

            # обновим max DD
            if state["positions"]:
                avg_entry = sum(p["buy_price"]*p["qty"] for p in state["positions"]) / \
                            sum(p["qty"] for p in state["positions"])
                dd = (avg_entry - price)/avg_entry
                if dd > state["max_drawdown"]:
                    state["max_drawdown"] = dd

            # ---------- SELL / TP / SL ----------
            new_pos = []
            for p in state["positions"]:
                b   = p["buy_price"]
                q_n = round_step(p["qty"], lm["qty_step"])
                tp  = p["tp"]
                q_g = p.get("buy_qty_gross", q_n/(1-TAKER_FEE))

                if not can_place_sell(sym, q_n, price, coin_bal):
                    new_pos.append(p)
                    logging.info(f"[{sym}] 🔸Hold: нельзя продать — ниже лимитов (min_qty={lm['min_qty']}, min_amt={lm['min_amt']})")
                    continue

                pnl = net_pnl(price, b, q_n, q_g)
                need = min_net_required(price, q_n)

                # SL — только если тоже >= $1 net
                if price <= b*(1-STOP_LOSS_PCT) and pnl >= MIN_NET_ABS_USD and (not PROFIT_ONLY or pnl >= need):
                    _attempt_sell(sym, q_n)
                    msg = f"🟠 SL SELL {sym} @ {price:.6f}, qty={q_n:.8f}, netPnL={pnl:.2f}"
                    logging.info(msg); send_tg(msg)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                    continue

                # TP — только если выполняем need (>= $1 net и пр.)
                if price >= tp and pnl >= need:
                    _attempt_sell(sym, q_n)
                    msg = f"✅ TP SELL {sym} @ {price:.6f}, qty={q_n:.8f}, netPnL={pnl:.2f}"
                    logging.info(msg); send_tg(msg)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                else:
                    new_tp = max(tp, price + TRAIL_MULTIPLIER*atr)
                    if new_tp != tp:
                        logging.info(f"[{sym}] 📈 Trail TP: {tp:.6f} → {new_tp:.6f}")
                    p["tp"] = new_tp
                    new_pos.append(p)
            state["positions"] = new_pos

            # ---------- BUY / AVERAGE ----------
            if sig == "buy":
                # усреднение
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(x["qty"] for x in state["positions"])
                    avg_price = sum(x["qty"]*x["buy_price"] for x in state["positions"]) / total_q
                    dd = (price - avg_price)/avg_price
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN:
                        q_gross = budget_qty(sym, price, avail)
                        if can_place_buy(sym, q_gross, price, usdt):
                            ok = _attempt_buy(sym, q_gross)
                            if ok:
                                tp = price + TRAIL_MULTIPLIER*atr
                                append_pos(sym, price, q_gross, tp)
                                state["count"] += 1; state["avg_count"] += 1
                                send_tg(f"🟢 BUY(avg) {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f} | reason=sig=buy, dd={dd:.4f}")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip avg: бюджет/лимиты/баланс")
                    else:
                        logging.info(f"[{sym}] 🔸Skip avg: dd={dd:.4f} вне (-{MAX_DRAWDOWN:.2f})")
                # первичный вход
                elif not state["positions"]:
                    if state["last_sell_price"] and abs(price - state["last_sell_price"])/price < 0.003:
                        logging.info(f"[{sym}] 🔸Skip buy: близко к последней продаже")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        if can_place_buy(sym, q_gross, price, usdt):
                            ok = _attempt_buy(sym, q_gross)
                            if ok:
                                tp = price + TRAIL_MULTIPLIER*atr
                                append_pos(sym, price, q_gross, tp)
                                state["count"] += 1
                                send_tg(f"🟢 BUY {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f} | reason=sig=buy")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Skip buy: бюджет/лимиты/баланс (min_amt={lm['min_amt']}, min_qty={lm['min_qty']})")
            else:
                if not state["positions"]:
                    logging.info(f"[{sym}] 🔸No buy: signal={sig}")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] Ошибка цикла: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                send_tg(f"[{sym}] Ошибка цикла: {e}")
                _last_err_ts = now

    _save_state()

    # ежедневный отчёт
    now = datetime.datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# ---------- RUN ----------
if __name__ == "__main__":
    log_event("🚀 Bot starting (v3.redis.profitStrict.slipfix)")
    init_state()
    load_symbol_limits()
    restore_positions()
    send_tg(f"⚙️ Params: TAKER={TAKER_FEE}, MAX_TRADE={MAX_TRADE_USDT}, "
            f"TPxATR={1.0}, TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, "
            f"DD={MAX_DRAWDOWN*100:.0f}%, ProfitOnly={PROFIT_ONLY}, Redis={'ON' if rds else 'OFF'} | SlipBuf={SLIP_BUFFER*100:.2f}%")

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
