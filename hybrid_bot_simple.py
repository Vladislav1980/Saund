# -*- coding: utf-8 -*-
# Bybit Spot bot (trade_v3_2.py)
# - Твои входы/TP/SL сохранены 1-в-1
# - Минимум $1 net-прибыли после ДВУХ комиссий taker (0.0018 * 2)
# - Предзащита ордеров: проверка min_notional/min_qty/балансов → НЕТ 170140/170131
# - Корректный расчет qty: шаг лота, min_amt, бюджет и резервы
# - Анти rate-limit: кэш /wallet-balance + мягкий backoff; анти‑спам в TG
# - Логи укорочены; в TG только события

import os, time, math, logging, datetime, json, traceback
import random
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# --------------- ENV / CONFIG ---------------

load_dotenv()
API_KEY   = os.getenv("BYBIT_API_KEY")
API_SECRET= os.getenv("BYBIT_API_SECRET")
TG_TOKEN  = os.getenv("TG_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

TAKER_FEE = 0.0018            # комиссия taker
RESERVE_BALANCE = 1.0         # USDT, не трогаем
MAX_TRADE_USDT  = 35.0        # верхний предел бюджета на 1 покупку
TRAIL_MULTIPLIER= 1.5
MAX_DRAWDOWN    = 0.10        # усреднение до -10%
MAX_AVERAGES    = 3
STOP_LOSS_PCT   = 0.03        # 3%
MIN_PROFIT_PCT  = 0.005       # 0.5% от ноторнала, но см. ниже
MIN_ABS_PNL     = 3.0         # старый порог (оставил)
MIN_NET_PROFIT  = 1.50        # твой порог
MIN_NET_ABS_USD = 1.00        # ЯВНАЯ гарантия net >= $1 после 2х ком

INTERVAL = "1"
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR   = 22
DAILY_REPORT_MINUTE = 30

# анти-спам/лимиты
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

def log_event(msg):
    logging.info(msg)
    send_tg(msg)

# --------------- SESSION / STATE ---------------

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

STATE = {}
LIMITS = {}  # symbol -> {min_qty, qty_step, min_amt}
LAST_REPORT_DATE = None
_last_err_ts = 0.0

def init_state():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        log_event("🚀 Бот стартует. Состояние: FILE")
    except Exception:
        STATE = {}
        log_event("🚀 Бот стартует. Состояние: FRESH")
    for s in SYMBOLS:
        STATE.setdefault(s, {
            "positions": [],  # {buy_price, qty(net), buy_qty_gross, tp}
            "pnl": 0.0, "count": 0, "avg_count": 0,
            "last_sell_price": 0.0, "max_drawdown": 0.0
        })
    save_state()

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"save_state error: {e}")

# --------------- BYBIT HELPERS ---------------

def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            # мягкий backoff только для rate-limit/403/10006
            if "rate" in msg.lower() or "403" in msg or "10006" in msg:
                logging.info(f"Rate-limit/backoff {delay:.1f}s: {msg}")
                time.sleep(delay)
                delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)
                continue
            raise

_wallet_cache = {"ts": 0.0, "coins": None}

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

# --------------- MARKET DATA / SIGNAL ---------------

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"],21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    atr = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
    macd = MACD(close=df["c"])
    df["macd"], df["sig"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.4f},EMA21={last['ema21']:.4f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.4f},SIG={last['sig']:.4f}")
    if last["ema9"] > last["ema21"] and last["rsi"]>50 and last["macd"]>last["sig"]:
        return "buy", float(atr), info
    if last["ema9"] < last["ema21"] and last["rsi"]<50 and last["macd"]<last["sig"]:
        return "sell", float(atr), info
    return "none", float(atr), info

# --------------- QTY / PRE-ORDER GUARD ---------------

def budget_qty(sym: str, price: float, avail_usdt: float) -> float:
    """К-во под покупку с учётом бюджета, шага и min_amt."""
    if sym not in LIMITS: return 0.0
    limits = LIMITS[sym]
    budget = min(avail_usdt, MAX_TRADE_USDT)
    if budget <= 0: return 0.0
    q = round_step(budget / price, limits["qty_step"])
    # если по бюджету не дотягиваем до min_amt — не покупаем
    if q < limits["min_qty"] or q*price < limits["min_amt"]:
        return 0.0
    return q

def can_place_buy(sym: str, q: float, price: float, usdt_free: float) -> bool:
    if q <= 0: return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q*price < lm["min_amt"]:
        return False
    # на покупку нужна сумма + комиссия, чтобы не словить 170131
    need = q*price*(1 + TAKER_FEE)  # с запасом под комиссию
    return need <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)

def can_place_sell(sym: str, q_net: float, price: float, coin_bal_now: float) -> bool:
    if q_net <= 0: return False
    lm = LIMITS[sym]
    # На sell тоже действует min_amt и min_qty в Bybit
    if q_net < lm["min_qty"] or q_net*price < lm["min_amt"]:
        return False
    return q_net <= coin_bal_now + 1e-12

# --------------- POS / PNL ---------------

def append_pos(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    STATE[sym]["positions"].append({
        "buy_price": price,
        "qty": qty_net,                  # после комиссии покупки
        "buy_qty_gross": qty_gross,      # что реально купили
        "tp": tp
    })
    save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    """Net PnL после двух комиссий."""
    cost  = buy_price * buy_qty_gross                       # отдано USDT на вход
    proceeds = price * qty_net * (1 - TAKER_FEE)            # получим на выходе
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    # требуемая чистая прибыль — минимум из трёх, но не ниже $1.
    pct_req = price * qty_net * MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD, MIN_NET_PROFIT, MIN_ABS_PNL, pct_req)

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

# --------------- INIT FROM BALANCE ---------------

def restore_positions():
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
                STATE[sym]["positions"].append({
                    "buy_price": price,
                    "qty": q_net,
                    "buy_qty_gross": q_net/(1-TAKER_FEE),
                    "tp": tp
                })
                logging.info(f"♻️ [{sym}] восстановлена позиция qty={q_net:.8f} @ {price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    save_state()

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

            sig, atr, info = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            value = coin_bal*price

            logging.info(f"[{sym}] sig={sig}, price={price:.6f}, value={value:.2f}, "
                         f"pos={len(state['positions'])} | {info}")

            # обновим max DD
            if state["positions"]:
                avg_entry = sum(p["buy_price"]*p["qty"] for p in state["positions"]) / \
                            sum(p["qty"] for p in state["positions"])
                dd = (avg_entry - price)/avg_entry
                if dd > state["max_drawdown"]:
                    state["max_drawdown"] = dd

            # -------- SELL / TP / SL ----------
            new_pos = []
            for p in state["positions"]:
                b   = p["buy_price"]
                q_n = round_step(p["qty"], lm["qty_step"])
                tp  = p["tp"]
                q_g = p.get("buy_qty_gross", q_n/(1-TAKER_FEE))

                # если позиция стала меньше лимитов биржи — держим, не спамим ордером
                if not can_place_sell(sym, q_n, price, coin_bal):
                    new_pos.append(p)
                    logging.info(f"[{sym}] 🔸Не продаём: меньше лимитов биржи "
                                 f"(min_qty={lm['min_qty']}, min_amt={lm['min_amt']})")
                    continue

                pnl = net_pnl(price, b, q_n, q_g)
                need = min_net_required(price, q_n)

                # SL
                if price <= b*(1-STOP_LOSS_PCT) and pnl >= MIN_NET_ABS_USD:
                    _safe_call(session.place_order, category="spot", symbol=sym,
                               side="Sell", orderType="Market", qty=str(q_n))
                    msg = f"SL SELL {sym} @ {price:.6f}, qty={q_n:.8f}, pnl(net)={pnl:.2f}"
                    logging.info(msg); send_tg("❗"+msg)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    # обновим баланс кэша
                    coins = get_wallet(True)
                    coin_bal = coin_balance(coins, sym)
                    continue

                # TP
                if price >= tp and pnl >= need:
                    _safe_call(session.place_order, category="spot", symbol=sym,
                               side="Sell", orderType="Market", qty=str(q_n))
                    msg = f"TP SELL {sym} @ {price:.6f}, qty={q_n:.8f}, PnL(net)={pnl:.2f} | take-profit"
                    logging.info(msg); send_tg("✅ "+msg)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    coins = get_wallet(True)
                    coin_bal = coin_balance(coins, sym)
                else:
                    # тащим TP хвостом
                    new_tp = max(tp, price + TRAIL_MULTIPLIER*atr)
                    if new_tp != tp:
                        logging.info(f"[{sym}] 📈 TP: {tp:.6f} → {new_tp:.6f}")
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
                        if can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", qty=str(q_gross))
                            tp = price + TRAIL_MULTIPLIER*atr
                            append_pos(sym, price, q_gross, tp)
                            state["count"] += 1; state["avg_count"] += 1
                            send_tg(f"🟢 BUY(avg) {sym} @ {price:.6f}, qty={q_gross*(1-TAKER_FEE):.8f}")
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Не усредняем: бюджет/лимиты/баланс")
                    else:
                        logging.info(f"[{sym}] 🔸Не усредняем: dd {dd:.4f} вне (-{MAX_DRAWDOWN:.2f})")
                # первичный вход
                elif not state["positions"]:
                    # защита от обратного входа сразу после продажи
                    if state["last_sell_price"] and abs(price - state["last_sell_price"])/price < 0.003:
                        logging.info(f"[{sym}] 🔸Не покупаем: близко к последней продаже")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        if can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", qty=str(q_gross))
                            tp = price + TRAIL_MULTIPLIER*atr
                            append_pos(sym, price, q_gross, tp)
                            state["count"] += 1
                            send_tg(f"🟢 BUY {sym} @ {price:.6f}, qty={q_gross*(1-TAKER_FEE):.8f}")
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt-RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] ❌ Не покупаем: бюджет/лимиты/баланс "
                                         f"(min_amt={lm['min_amt']}, min_qty={lm['min_qty']})")
            else:
                if not state["positions"]:
                    logging.info(f"[{sym}] 🔸Нет покупки: сигн={sig}")

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
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# --------------- RUN ---------------

if __name__ == "__main__":
    log_event("🚀 Bot started (v3.2)")
    init_state()
    load_symbol_limits()
    restore_positions()
    send_tg(f"⚙️ Params: TAKER={TAKER_FEE}, MAX_TRADE={MAX_TRADE_USDT}, "
            f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, "
            f"DD={MAX_DRAWDOWN*100:.0f}%")

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
