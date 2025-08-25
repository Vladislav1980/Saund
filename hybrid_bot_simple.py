# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — v3 + NetPnL‑трейлинг + Unified Averaging + State Comparison
Сохраняет весь оригинальный функционал, добавляет фиксацию прибыли ≥ $1.5, с trailing‑exit при снижении netPnL на ≥ $0.6 от пикового значения,
усреднение ≤ 2 покупок, объединено в одну позицию, и сравнение позиции до/после усреднения.
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
API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
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
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
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

def _state_key(): return "bybit_spot_state_v3"
def _save_state():
    s = json.dumps(STATE, ensure_ascii=False)
    try:
        if rds: rds.set(_state_key(), s)
    except Exception:
        pass
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        logging.error(f"save_state error: {e}")

def _load_state():
    global STATE
    if rds:
        try:
            s = rds.get(_state_key())
            if s:
                STATE = json.loads(s)
                return "REDIS"
        except Exception:
            pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
            return "FILE"
    except Exception:
        STATE = {}
        return "FRESH"

def init_state():
    src = _load_state()
    logging.info(f"🚀 Бот стартует. Состояние: {src}")
    tg_event(f"🚀 Бот стартует. Состояние: {src}")
    for s in SYMBOLS:
        STATE.setdefault(s, {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0,
                              "last_sell_price": 0.0, "max_drawdown": 0.0})
    _save_state()

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
            value = coin_bal * price
            logging.info(f"[{sym}] sig={sig} | {info} | price={price:.6f}, value={value:.2f}, pos={len(state['positions'])}")

            if state["positions"]:
                total_qty = sum(max(p.get("qty", 0.0), 0.0) for p in state["positions"])
                if total_qty > 1e-15:
                    avg_entry = sum(max(p.get("qty", 0.0), 0.0) * float(p.get("buy_price", 0.0)) for p in state["positions"]) / total_qty
                    if avg_entry > 1e-15 and not (math.isnan(avg_entry) or math.isinf(avg_entry)):
                        dd_now = max(0.0, (avg_entry - price) / max(avg_entry, 1e-12))
                        state["max_drawdown"] = max(state.get("max_drawdown", 0.0), dd_now)

            new_pos = []
            for p in state["positions"]:
                b = p["buy_price"]
                q_n = round_step(p["qty"], lm["qty_step"])
                tp = p["tp"]
                q_g = p.get("buy_qty_gross", q_n / (1 - TAKER_FEE))
                if q_n <= 0:
                    continue
                sell_cap_q = q_n
                if sym == BTC_SYMBOL:
                    sell_cap_q = cap_btc_sell_qty(sym, q_n, price, coin_bal)
                if sell_cap_q < lm["min_qty"] or sell_cap_q * price < lm["min_amt"]:
                    new_pos.append(p)
                    logging.info(f"[{sym}] 🔸Hold: ниже лимитов/кап‑по BTC")
                    continue
                pnl = net_pnl(price, b, sell_cap_q, q_g * (sell_cap_q / max(q_n, 1e-12)))
                need = min_net_required(price, sell_cap_q)
                ok_to_sell = pnl >= need

                if price <= b * (1 - STOP_LOSS_PCT) and ok_to_sell:
                    _attempt_sell(sym, sell_cap_q)
                    msg = f"🟠 SL SELL {sym} @ {price:.6f}, qty={sell_cap_q:.8f}, netPnL={pnl:.2f}"
                    logging.info(msg); tg_event(msg)
                    state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                    left = q_n - sell_cap_q
                    if left > 0:
                        ratio = sell_cap_q / max(q_n, 1e-12)
                        p["qty"] = left
                        p["buy_qty_gross"] = max(0.0, p["buy_qty_gross"] * (1.0 - ratio))
                        new_pos.append(p)
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                    continue

                p["max_pnl"] = max(p.get("max_pnl", 0.0), pnl)
                if p["max_pnl"] >= TRAIL_PNL_TRIGGER and (p["max_pnl"] - pnl) >= TRAIL_PNL_GAP:
                    _attempt_sell(sym, sell_cap_q)
                    msg = f"✅ TRAIL SELL {sym} @ {price:.6f}, qty={sell_cap_q:.8f}, netPnL={pnl:.2f}, peak={p['max_pnl']:.2f}"
                    logging.info(msg); tg_event(msg)
                    state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                    left = q_n - sell_cap_q
                    if left > 0:
                        ratio = sell_cap_q / max(q_n, 1e-12)
                        p["qty"] = left
                        p["buy_qty_gross"] = max(0.0, p["buy_qty_gross"] * (1.0 - ratio))
                        new_pos.append(p)
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                    continue

                if ok_to_sell:
                    _attempt_sell(sym, sell_cap_q)
                    msg = f"✅ PROFIT SELL {sym} @ {price:.6f}, qty={sell_cap_q:.8f}, netPnL={pnl:.2f}"
                    logging.info(msg); tg_event(msg)
                    state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                    left = q_n - sell_cap_q
                    if left > 0:
                        ratio = sell_cap_q / max(q_n, 1e-12)
                        p["qty"] = left
                        p["buy_qty_gross"] = max(0.0, p["buy_qty_gross"] * (1.0 - ratio))
                        p["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                        new_pos.append(p)
                    coins = get_wallet(True); coin_bal = coin_balance(coins, sym)
                else:
                    new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                    if new_tp != tp:
                        logging.info(f"[{sym}] 📈 Trail TP: {tp:.6f} → {new_tp:.6f}")
                    p["tp"] = new_tp
                    new_pos.append(p)
                    logging.info(f"[{sym}] 🔸Не продаём: netPnL {pnl:.2f} < need {need:.2f}")

            state["positions"] = new_pos

            if sig == "buy" and volume_ok(df):
                if state["positions"] and state["avg_count"] < (MAX_AVERAGES - 1):
                    total_q = sum(x["qty"] for x in state["positions"])
                    avg_price = sum(x["qty"] * x["buy_price"] for x in state["positions"]) / max(total_q, 1e-12)
                    dd = (price - avg_price) / max(avg_price, 1e-12)
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        if q_gross > 0 and ob_ok and can_place_buy(sym, q_gross, price, usdt):
                            before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            if _attempt_buy(sym, q_gross):
                                append_or_update_position(sym, price, q_gross, price + TRAIL_MULTIPLIER * atr)
                                after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                                state["count"] += 1
                                state["avg_count"] += 1
                                qty_net = q_gross * (1 - TAKER_FEE)
                                msg = f"🟢 BUY(avg) {sym} @ {price:.6f}, qty_net={qty_net:.8f} | dd={dd:.4f}, {ob_info}"
                                logging.info(msg); tg_event(msg)
                                tg_event(f"📊 AVG {sym} POSITION UPDATE\nДо:\n{before}\nПосле:\n{after}")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            reason = f"budget/limits/OB/balance fail — q_gross={q_gross}, OB_OK={ob_ok}, BUY={can_place_buy(sym, q_gross, price, usdt)}"
                            tg_event(f"[{sym}] ❌ Avg BUY skipped: {reason}")
                    else:
                        logging.info(f"[{sym}] 🔸Skip avg: dd={dd:.4f} вне (-{MAX_DRAWDOWN:.2f})")
                elif not state["positions"]:
                    if state["last_sell_price"] and abs(price - state["last_sell_price"]) / price < 0.003:
                        logging.info(f"[{sym}] 🔸Skip buy: слишком близко к последней продаже")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        if q_gross > 0 and ob_ok and can_place_buy(sym, q_gross, price, usdt):
                            before = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                            if _attempt_buy(sym, q_gross):
                                append_or_update_position(sym, price, q_gross, price + TRAIL_MULTIPLIER * atr)
                                after = json.dumps(state["positions"], indent=2, ensure_ascii=False)
                                state["count"] += 1
                                qty_net = q_gross * (1 - TAKER_FEE)
                                msg = f"🟢 BUY {sym} @ {price:.6f}, qty_net={qty_net:.8f} | {ob_info}"
                                logging.info(msg); tg_event(msg)
                                tg_event(f"📊 NEW {sym} POSITION\nДо:\n{before}\nПосле:\n{after}")
                                coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            reason = f"budget/limits/OB/balance fail — q_gross={q_gross}, OB_OK={ob_ok}, BUY={can_place_buy(sym, q_gross, price, usdt)}"
                            tg_event(f"[{sym}] ❌ Buy skipped: {reason}")
                else:
                    logging.info(f"[{sym}] 🔸No buy: sig={sig}")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] Ошибка цикла: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"[{sym}] Ошибка цикла: {e}")
                _last_err_ts = now

    _save_state()

    now = datetime.datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()


if __name__ == "__main__":
    logging.info("🚀 Bot starting with NetPnL‑trailing + Unified Averaging + State Comparison")
    tg_event("🚀 Bot starting with NetPnL‑trailing + Unified Averaging + State Comparison")
    init_state()
    load_symbol_limits()
    restore_positions()
    tg_event(
        "⚙️ Params: " f"TAKER={TAKER_FEE}, BASE_MAX_TRADE={BASE_MAX_TRADE_USDT}, "
        f"OVR={MAX_TRADE_OVERRIDES}, TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, "
        f"DD={MAX_DRAWDOWN*100:.0f}%, MaxAvg={MAX_AVERAGES}, ProfitOnly={PROFIT_ONLY}, "
        f"VolFilter={'ON' if USE_VOLUME_FILTER else 'OFF'}, "
        f"OBGuard={'ON' if USE_ORDERBOOK_GUARD else 'OFF'} (spread≤{MAX_SPREAD_BP/100:.2f}%, impact≤{MAX_IMPACT_BP/100:.2f}%), "
        f"LiqRecovery={'ON' if LIQUIDITY_RECOVERY else 'OFF'} (min={LIQ_RECOVERY_USDT_MIN}, target={LIQ_RECOVERY_USDT_TARGET}), "
        f"NetPnL-Trailing={'ON' if TRAIL_PNL_TRIGGER else 'OFF'} (trigger=${TRAIL_PNL_TRIGGER}, gap=${TRAIL_PNL_GAP})"
    )
    while True:
        try:
            trade_cycle()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"Global error: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"Global error: {e}")
                _last_err_ts = now
        time.sleep(LOOP_SLEEP)
