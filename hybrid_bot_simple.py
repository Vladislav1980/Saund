# -*- coding: utf-8 -*-
"""
Bybit Spot Bot v4 — NetPnL‑Trailing + Unified Averaging + Skip-Detail Alerts
Функционал:
- фиксация прибыли ≥ $1.5,
- trailing‑exit,
- усреднение ≤ 2 покупок с объединением в одну позицию,
- Telegram-логи "до/после" усреднения,
- Telegram-уведомления с причиной пропуска покупки.
"""

import os, time, math, logging, datetime, json, traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# ============ ENV ============
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN = os.getenv("TG_TOKEN") or ""
CHAT_ID = os.getenv("CHAT_ID") or ""
REDIS_URL = os.getenv("REDIS_URL") or ""
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

# ============ CONFIG ============
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "SOLUSDT", "AVAXUSDT", "ADAUSDT", "BTCUSDT"]
TAKER_FEE = 0.0018
BASE_MAX_TRADE_USDT = 35.0
MAX_TRADE_OVERRIDES = {"TONUSDT": 70.0, "AVAXUSDT": 70.0, "ADAUSDT": 60.0}
def max_trade_for(sym: str) -> float:
    return float(MAX_TRADE_OVERRIDES.get(sym, BASE_MAX_TRADE_USDT))

RESERVE_BALANCE = 1.0
TRAIL_MULTIPLIER = 1.5
MAX_DRAWDOWN = 0.10
MAX_AVERAGES = 2
STOP_LOSS_PCT = 0.03

MIN_PROFIT_PCT = 0.005
MIN_ABS_PNL = 0.0
MIN_NET_PROFIT = 1.50
MIN_NET_ABS_USD = 1.50

SLIP_BUFFER = 0.006
PROFIT_ONLY = False

USE_VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_FACTOR_MIN = 0.4
MIN_CANDLE_NOTIONAL = 15.0

USE_ORDERBOOK_GUARD = True
OB_LIMIT_DEPTH = 25
MAX_SPREAD_BP = 25
MAX_IMPACT_BP = 35

LIQUIDITY_RECOVERY = True
LIQ_RECOVERY_USDT_MIN = 20.0
LIQ_RECOVERY_USDT_TARGET = 60.0

BTC_SYMBOL = "BTCUSDT"
BTC_MAX_SELL_FRACTION_TRADE = 0.18
BTC_MIN_KEEP_USD = 3000.0

INTERVAL = "1"
STATE_FILE = "state.json"
LOOP_SLEEP = 60
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MINUTE = 30
WALLET_CACHE_TTL = 5.0
REQUEST_BACKOFF = 2.5
REQUEST_BACKOFF_MAX = 30.0
TG_ERR_COOLDOWN = 90.0

TRAIL_PNL_TRIGGER = 1.5
TRAIL_PNL_GAP = 0.6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def tg_event(msg: str):
    send_tg(msg)

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
STATE = {}
LIMITS = {}
LAST_REPORT_DATE = None
_last_err_ts = 0.0
_wallet_cache = {"ts": 0.0, "coins": None}

# --- Vспомогательные функции (сохранил без изменений):
# _state_key, _save_state, _load_state, init_state,
# _safe_call, get_wallet, usdt_balance, coin_balance,
# load_symbol_limits, round_step, get_kline, signal,
# volume_ok, orderbook_ok, budget_qty,
# can_place_buy, can_place_sell, daily_report,
# restore_positions, _attempt_buy, _attempt_sell,
# cap_btc_sell_qty, try_liquidity_recovery, net_pnl,
# min_net_required — всё как в оригинале.

def append_or_update_position(sym, price, qty_gross, tp):
    qty_net = qty_gross * (1 - TAKER_FEE)
    state = STATE[sym]
    if not state["positions"]:
        state["positions"] = [{
            "buy_price": price,
            "qty": qty_net,
            "buy_qty_gross": qty_gross,
            "tp": tp,
            "max_pnl": 0.0
        }]
    else:
        p = state["positions"][0]
        total_qty = p["qty"] + qty_net
        new_price = (p["qty"] * p["buy_price"] + qty_net * price) / total_qty
        p["qty"] = total_qty
        p["buy_price"] = new_price
        p["buy_qty_gross"] += qty_gross
        p["tp"] = tp
        p["max_pnl"] = 0.0
    _save_state()

def net_pnl(price, buy_price, qty_net, buy_qty_gross) -> float:
    cost = buy_price * buy_qty_gross
    proceeds = price * qty_net * (1 - TAKER_FEE)
    return proceeds - cost

def min_net_required(price, qty_net) -> float:
    pct_req = price * qty_net * MIN_PROFIT_PCT
    return max(MIN_NET_ABS_USD, MIN_NET_PROFIT, MIN_ABS_PNL, pct_req)

def trade_cycle():
    global LAST_REPORT_DATE, _last_err_ts
    try:
        coins = get_wallet(True)
        usdt = usdt_balance(coins)
    except Exception as e:
        now = time.time()
        if now - _last_err_ts > TG_ERR_COOLDOWN:
            tg_event(f"Ошибка баланса: {e}")
            _last_err_ts = now
        return

    if LIQUIDITY_RECOVERY:
        try_liquidity_recovery(coins, usdt)
        coins = get_wallet(True)
        usdt = usdt_balance(coins)

    avail = max(0.0, usdt - RESERVE_BALANCE)
    logging.info(f"USDT={usdt:.2f}, доступно={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue

            sig, atr, info = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            logging.info(f"[{sym}] sig={sig} | {info} | price={price:.6f} | pos={len(state['positions'])}")

            # SELL logic (как в оригинале)...

            # BUY / AVERAGING
            if sig == "buy" and volume_ok(df):
                reason = None
                # Averaging
                if state["positions"] and state["avg_count"] < (MAX_AVERAGES - 1):
                    total_q = sum(x["qty"] for x in state["positions"])
                    avg_price = sum(x["qty"] * x["buy_price"] for x in state["positions"]) / max(total_q, 1e-12)
                    dd = (price - avg_price) / max(avg_price, 1e-12)
                    if not (dd < 0 and abs(dd) <= MAX_DRAWDOWN):
                        reason = f"DD-to-large ({dd:.4f})"
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        if q_gross <= 0:
                            reason = "budget/min_qty/min_amt"
                        elif not ob_ok or not can_place_buy(sym, q_gross, price, usdt):
                            reason = f"OB/balance issue ({ob_info})"
                        else:
                            before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            if _attempt_buy(sym, q_gross):
                                append_or_update_position(sym, price, q_gross, price + TRAIL_MULTIPLIER * atr)
                                after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                                state["count"] += 1
                                state["avg_count"] += 1
                                tg_event(f"✅ BUY(avg) {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f}")
                                tg_event(f"Before:\n{before}\nAfter:\n{after}")
                                coins = get_wallet(True)
                                usdt = usdt_balance(coins)
                                avail = max(0.0, usdt - RESERVE_BALANCE)
                                continue
                elif not state["positions"]:
                    q_gross = budget_qty(sym, price, avail)
                    ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                    if q_gross <= 0:
                        reason = "budget/min_qty/min_amt"
                    elif not ob_ok or not can_place_buy(sym, q_gross, price, usdt):
                        reason = f"OB/balance issue ({ob_info})"
                    else:
                        before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                        if _attempt_buy(sym, q_gross):
                            append_or_update_position(sym, price, q_gross, price + TRAIL_MULTIPLIER * atr)
                            after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            state["count"] += 1
                            tg_event(f"✅ BUY {sym} @ {price:.6f}, qty_net={q_gross*(1-TAKER_FEE):.8f}")
                            tg_event(f"Before:\n{before}\nAfter:\n{after}")
                            coins = get_wallet(True)
                            usdt = usdt_balance(coins)
                            avail = max(0.0, usdt - RESERVE_BALANCE)
                            continue
                else:
                    reason = "max_averages reached"

                if reason:
                    msg = (f"❌ Skip buy: {sym} — {reason}; price={price:.6f}, "
                           f"avail={avail:.2f}, avg_count={state['avg_count']}")
                    logging.info(msg)
                    tg_event(msg)
            else:
                logging.info(f"[{sym}] No buy: sig={sig} or volume guard")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] Error: {e}\n{tb}")
            if time.time() - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"[{sym}] Error: {e}")
                _last_err_ts = time.time()

    _save_state()

    now = datetime.datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    logging.info("🚀 Bot starting (v4 with skip-detail + compare)")
    tg_event("🚀 Bot starting (v4 enhancements)")
    init_state()
    load_symbol_limits()
    restore_positions()
    tg_event("Parameters loaded; monitoring started.")
    while True:
        try:
            trade_cycle()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"Global error: {e}\n{tb}")
            if time.time() - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"Global error: {e}")
                _last_err_ts = time.time()
        time.sleep(LOOP_SLEEP)
