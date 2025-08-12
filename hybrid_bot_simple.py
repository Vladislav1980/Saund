# -*- coding: utf-8 -*-
# bybit spot bot with detailed logging, net PnL >= $1 after fees, floating per-trade budget,
# redis state, telegram notifications, and daily balance report.

import os, time, math, logging, datetime, json, random, traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ---- optional redis state ----
try:
    import redis
except Exception:
    redis = None  # allow running without redis if library not installed

# ==================== CONFIG ====================

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Redis: set REDIS_URL like redis://:password@host:6379/0
REDIS_URL = os.getenv("REDIS_URL", "")
REDIS_KEY = os.getenv("REDIS_KEY", "bybit_spot_bot_state_v1")

# symbols
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

# fees from your screenshot (spot taker 0.18%)
TAKER_FEE_SPOT = 0.0018   # 0.18% for market orders (taker)
MAKER_FEE_SPOT = 0.0010   # not used (we send Market)

# money management
RESERVE_BALANCE = 1.0           # keep in USDT
MIN_TRADE_USDT = 150.0
MAX_TRADE_USDT = 230.0
FLOAT_BUDGET_MODE = "signal"    # "signal" | "random" | "fixed_max" | "fixed_min"

# risk/logic
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10    # averaging condition
MAX_AVERAGES = 3
STOP_LOSS_PCT = 0.03

# profit rules (net after fees)
MIN_PROFIT_PCT = 0.005  # 0.5% of position notional (net check uses this as floor)
MIN_NET_ABS_USD = 1.0   # your strict requirement

# ops
INTERVAL = "1"          # minutes
LOOP_SLEEP = 60         # seconds
STATE_FILE = "state.json"
TG_VERBOSE = True

# report time (UTC or your local? using local server time)
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30

# logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

# ==================== TELEGRAM ====================

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception:
        logging.error("Telegram send failed")

def log(msg: str, tg: bool = False):
    logging.info(msg)
    if tg or TG_VERBOSE:
        # TG_VERBOSE включает «стриминговые» логи в тг
        try:
            send_tg(msg)
        except Exception:
            pass

# ==================== BYBIT HTTP ====================

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# ==================== STATE (REDIS + FILE BACKUP) ====================

STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
cycle_count = 0

def _default_symbol_state():
    return {
        "positions": [],     # list of {buy_price, qty (net), buy_qty_gross, tp}
        "pnl": 0.0,
        "count": 0,
        "avg_count": 0,
        "last_sell_price": 0.0,
        "max_drawdown": 0.0
    }

def state_to_json():
    return json.dumps(STATE, ensure_ascii=False)

def save_state_file():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(state_to_json())
    except Exception as e:
        logging.error(f"save_state_file error: {e}")

def load_state_file():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        return True
    except Exception:
        return False

def redis_client():
    if not REDIS_URL or redis is None:
        return None
    try:
        return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    except Exception as e:
        logging.error(f"Redis connect error: {e}")
        return None

def save_state():
    rc = redis_client()
    if rc:
        try:
            rc.set(REDIS_KEY, state_to_json())
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    save_state_file()

def init_state():
    """Load state from Redis first, then fallback to file, else fresh."""
    global STATE
    restored_from = "fresh"
    rc = redis_client()
    if rc:
        try:
            raw = rc.get(REDIS_KEY)
            if raw:
                STATE = json.loads(raw)
                restored_from = "redis"
        except Exception as e:
            logging.error(f"Redis load error: {e}")
    if not STATE:
        if load_state_file():
            restored_from = "file"
        else:
            STATE = {}
    # ensure structure
    for sym in SYMBOLS:
        STATE.setdefault(sym, _default_symbol_state())
    log(f"🚀 Старт бота. Восстановление состояния: {restored_from.upper()}", True)
    if restored_from != "fresh":
        # краткий отчёт по восстановленным позициям
        lines = []
        for sym in SYMBOLS:
            s = STATE[sym]
            cur_q = sum(p.get("qty", 0.0) for p in s["positions"])
            lines.append(f"{sym}: позиций={len(s['positions'])}, qty={cur_q:.6f}, pnl_acc={s['pnl']:.2f}")
        send_tg("♻️ Восстановление позиций:\n" + "\n".join(lines))
    save_state()

# ==================== HELPERS ====================

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

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except Exception:
        return qty

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_wallet():
    return session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]

def get_balance_usdt():
    coins = get_wallet()
    return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    coins = get_wallet()
    return float(next((c["walletBalance"] for c in coins if c["coin"]==coin), 0.0))

def calc_signal(df):
    """Return (sig: 'buy'|'sell'|'none', atr, info, confidence[0..1])"""
    if df.empty or len(df) < 50:
        return "none", 0.0, "insufficient data", 0.0
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, "
            f"RSI={last['rsi']:.2f}, MACD={last['macd']:.4f}, SIG={last['macd_signal']:.4f}")
    # confidence (0..1)
    conf_buy  = max(0.0, min(1.0, (last["rsi"]-50)/30))  # rsi 50->80 ≈ 0..1
    conf_sell = max(0.0, min(1.0, (50-last["rsi"])/30))
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], info, conf_buy
    elif last["ema9"] < last["ema21"] and last["rsi"] < 50 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"], info, conf_sell
    return "none", last["atr"], info, 0.0

def choose_trade_budget(confidence: float, avail_usdt: float) -> float:
    """Return dollars to use for this trade given mode, limited by available balance."""
    bmin, bmax = MIN_TRADE_USDT, MAX_TRADE_USDT
    if FLOAT_BUDGET_MODE == "signal":
        budget = bmin + (bmax - bmin) * max(0.0, min(1.0, confidence))
    elif FLOAT_BUDGET_MODE == "random":
        budget = random.uniform(bmin, bmax)
    elif FLOAT_BUDGET_MODE == "fixed_max":
        budget = bmax
    else:
        budget = bmin
    return max(0.0, min(budget, avail_usdt))

def qty_from_budget(sym: str, price: float, budget_usdt: float) -> float:
    if sym not in LIMITS:
        return 0.0
    step = LIMITS[sym]["qty_step"]
    q = adjust_qty(budget_usdt / price, step)
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

def log_trade(sym, side, price, qty, pnl, reason=""):
    usdt_val = price * qty
    msg = (f"{side} {sym} @ {price:.6f}, qty={qty:.8f}, USDT≈{usdt_val:.2f}, "
           f"PnL(net)={pnl:.2f} | {reason}")
    log(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt_val},{pnl},{reason}\n")
    save_state()

def init_positions_from_balance():
    """If we hold coins on balance, add a recovered position with tp initialized."""
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue
            price = df["c"].iloc[-1]
            bal = get_coin_balance(sym)      # this is net coins actually on wallet
            if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
                qty_net = adjust_qty(bal, LIMITS[sym]["qty_step"])
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                tp = price + TRAIL_MULTIPLIER * atr
                STATE[sym]["positions"].append({
                    "buy_price": price,
                    "qty": qty_net,                              # net coins on wallet
                    "buy_qty_gross": qty_net / (1 - TAKER_FEE_SPOT),  # approx gross
                    "tp": tp
                })
                log(f"♻️ [{sym}] Восстановлена позиция qty={qty_net:.8f}, price={price:.6f}, tp={tp:.6f}", True)
        except Exception as e:
            log(f"[{sym}] Ошибка восстановления позиций: {e}", True)
    save_state()

def send_daily_report():
    try:
        coins = get_wallet()
        by_coin = {c["coin"]: float(c["walletBalance"]) for c in coins}
        usdt = by_coin.get("USDT", 0.0)
        lines = [f"📊 Daily Report {datetime.datetime.now().date()}",
                 f"USDT: {usdt:.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT","")
            bal = by_coin.get(base, 0.0)
            price = float(get_kline(sym)["c"].iloc[-1])
            val = price * bal
            s = STATE[sym]
            cur_pos = sum(p["qty"] for p in s["positions"])
            dd = s.get("max_drawdown", 0.0)
            lines.append(f"{sym}: balance={bal:.6f} (~{val:.2f} USDT), "
                         f"trades={s['count']}, pnl={s['pnl']:.2f}, maxDD={dd*100:.2f}%, curPosQty={cur_pos:.6f}")
        send_tg("\n".join(lines))
    except Exception as e:
        log(f"Ошибка формирования отчёта: {e}", True)

# ==================== CORE ====================

def trade_cycle():
    global LAST_REPORT_DATE, cycle_count
    try:
        usdt = get_balance_usdt()
    except Exception as e:
        log(f"Ошибка получения баланса USDT: {e}", True)
        return

    avail = max(0.0, usdt - RESERVE_BALANCE)
    log(f"💰 Баланс USDT={usdt:.2f} | Доступно={avail:.2f}", False)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ❗Данных по свечам нет — пропускаем", True)
                continue

            sig, atr, info_ind, confidence = calc_signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price

            log(f"[{sym}] 🔎 Сигнал={sig.upper()} (conf={confidence:.2f}), price={price:.6f}, "
                f"balance={coin_bal:.8f} (~{value:.2f} USDT) | {info_ind}", True)

            # update max drawdown for existing positions
            if state["positions"]:
                avg_entry = sum(p["buy_price"] * p["qty"] for p in state["positions"]) / \
                            sum(p["qty"] for p in state["positions"])
                curr_dd = (avg_entry - price) / avg_entry
                if curr_dd > state["max_drawdown"]:
                    state["max_drawdown"] = curr_dd

            # ----- manage open positions -----
            new_positions = []
            for pos in state["positions"]:
                b = pos["buy_price"]
                q_net = adjust_qty(pos["qty"], limits["qty_step"])
                tp = pos["tp"]
                buy_gross = pos.get("buy_qty_gross", q_net / (1 - TAKER_FEE_SPOT))

                cost_usdt = b * buy_gross
                proceeds_usdt = price * q_net * (1 - TAKER_FEE_SPOT)
                pnl_net = proceeds_usdt - cost_usdt

                min_net_req = max(MIN_NET_ABS_USD, price * q_net * MIN_PROFIT_PCT)

                # stop-loss check
                if price <= b * (1 - STOP_LOSS_PCT):
                    session.place_order(category="spot", symbol=sym, side="Sell",
                                        orderType="Market", qty=str(q_net))
                    reason = (f"STOP-LOSS: price {price:.6f} ≤ {b*(1-STOP_LOSS_PCT):.6f}; "
                              f"pnl_net={pnl_net:.2f}")
                    log_trade(sym, "SELL", price, q_net, pnl_net, reason)
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    continue

                # take-profit (trail reached + net requirement)
                if price >= tp and pnl_net >= min_net_req:
                    session.place_order(category="spot", symbol=sym, side="Sell",
                                        orderType="Market", qty=str(q_net))
                    reason = (f"TP HIT: price {price:.6f} ≥ tp {tp:.6f} "
                              f"и pnl_net {pnl_net:.2f} ≥ min_req {min_net_req:.2f}")
                    log_trade(sym, "SELL", price, q_net, pnl_net, reason)
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                else:
                    # trail move
                    new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                    if new_tp != tp:
                        log(f"[{sym}] 📈 Обновлён TP: {tp:.6f} → {new_tp:.6f}", False)
                    pos["tp"] = new_tp
                    new_positions.append(pos)

                    # explain why not sold
                    if price < tp:
                        log(f"[{sym}] 🔸Не продаём: цена {price:.6f} < TP {tp:.6f}", False)
                    elif pnl_net < min_net_req:
                        log(f"[{sym}] 🔸Не продаём: pnl_net {pnl_net:.2f} < требуемого {min_net_req:.2f}", False)

            state["positions"] = new_positions

            # ----- entries (buy / averaging) -----
            if sig == "buy":
                # averaging if we have positions
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty"] for p in state["positions"])
                    avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_q
                    drawdown = (price - avg_price) / avg_price
                    if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN:
                        budget = choose_trade_budget(confidence, avail)
                        qty_gross = qty_from_budget(sym, price, budget)
                        if qty_gross <= 0:
                            log(f"[{sym}] ❌ Не усредняем: не прошли фильтры биржи или недостаточно средств "
                                f"(budget={budget:.2f})", True)
                        elif qty_gross * price > (get_balance_usdt() - RESERVE_BALANCE + 1e-9):
                            log(f"[{sym}] ❌ Не усредняем: недостаточно USDT на кошельке", True)
                        else:
                            session.place_order(category="spot", symbol=sym, side="Buy",
                                                orderType="Market", qty=str(qty_gross))
                            qty_net = qty_gross * (1 - TAKER_FEE_SPOT)
                            tp = price + TRAIL_MULTIPLIER * atr
                            STATE[sym]["positions"].append({
                                "buy_price": price, "qty": qty_net,
                                "buy_qty_gross": qty_gross, "tp": tp
                            })
                            state["count"] += 1
                            state["avg_count"] += 1
                            log_trade(sym, "BUY (avg)", price, qty_net, 0.0,
                                      reason=(f"drawdown={drawdown:.4f}, budget={budget:.2f}, "
                                              f"qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}"))
                    else:
                        log(f"[{sym}] 🔸Не усредняем: drawdown {drawdown:.4f} вне диапазона (-{MAX_DRAWDOWN})", False)

                # first entry
                elif not state["positions"]:
                    # anti-churn: не покупать сразу после продажи на той же цене
                    if state["last_sell_price"] and abs(price - state["last_sell_price"]) / price < 0.001:
                        log(f"[{sym}] 🔸Не покупаем: слишком близко к последней продаже "
                            f"({state['last_sell_price']:.6f})", False)
                    else:
                        budget = choose_trade_budget(confidence, avail)
                        if budget < MIN_TRADE_USDT:
                            log(f"[{sym}] ❌ Не покупаем: доступно {avail:.2f} < минимального бюджета {MIN_TRADE_USDT}", True)
                        else:
                            qty_gross = qty_from_budget(sym, price, budget)
                            if qty_gross <= 0:
                                log(f"[{sym}] ❌ Не покупаем: не прошли фильтры биржи "
                                    f"(min_qty/step/min_amt) при бюджете {budget:.2f}", True)
                            elif qty_gross * price > (get_balance_usdt() - RESERVE_BALANCE + 1e-9):
                                log(f"[{sym}] ❌ Не покупаем: недостаточно USDT на кошельке", True)
                            else:
                                session.place_order(category="spot", symbol=sym, side="Buy",
                                                    orderType="Market", qty=str(qty_gross))
                                qty_net = qty_gross * (1 - TAKER_FEE_SPOT)
                                tp = price + TRAIL_MULTIPLIER * atr
                                STATE[sym]["positions"].append({
                                    "buy_price": price, "qty": qty_net,
                                    "buy_qty_gross": qty_gross, "tp": tp
                                })
                                state["count"] += 1
                                log_trade(sym, "BUY", price, qty_net, 0.0,
                                          reason=(f"signal_conf={confidence:.2f}, budget={budget:.2f}, "
                                                  f"qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}, {info_ind}"))
            else:
                # explain why not buying
                if not state["positions"]:
                    log(f"[{sym}] 🔸Нет покупки: сигнал {sig}, confidence={confidence:.2f}", False)

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            log(f"[{sym}] Ошибка цикла: {e}\n{tb}", True)

    save_state()
    cycle_count += 1

    # daily report
    now = datetime.datetime.now()
    if (now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and
            LAST_REPORT_DATE != now.date()):
        send_daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

# ==================== MAIN ====================

if __name__ == "__main__":
    try:
        log("🚀 Бот запускается...", True)
        init_state()
        load_symbol_limits()
        init_positions_from_balance()
        log(f"⚙️ Параметры: TAKER_FEE={TAKER_FEE_SPOT}, "
            f"BUDGET=[{MIN_TRADE_USDT};{MAX_TRADE_USDT}] mode={FLOAT_BUDGET_MODE}, "
            f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.2f}%", True)

        while True:
            try:
                trade_cycle()
            except Exception as e:
                tb = traceback.format_exc(limit=2)
                log(f"Global cycle error: {e}\n{tb}", True)
            time.sleep(LOOP_SLEEP)

    except Exception as e:
        tb = traceback.format_exc()
        log(f"Fatal error on start: {e}\n{tb}", True)
