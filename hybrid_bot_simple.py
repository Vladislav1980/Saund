# -*- coding: utf-8 -*-
# One-off seeder for a spot position into the bot's state (Redis + state.json)
# Совместим с твоим форматом состояния: ключ bybit_spot_state_v3_ob, поля positions/qty/tp/…
# Аккуратно работает с Decimal и "кривыми" лимитами Bybit unified.

import os, json, argparse, logging, traceback
from decimal import Decimal, ROUND_DOWN, InvalidOperation, getcontext

from dotenv import load_dotenv
import requests
import pandas as pd
from pybit.unified_trading import HTTP
from ta.volatility import AverageTrueRange

# --------------------------------------------------------------------------
# Настройка точности Decimal
getcontext().prec = 28
getcontext().rounding = ROUND_DOWN
D = Decimal

# --------------------------------------------------------------------------
load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# те же параметры, что использует бот (можно переопределить из .env)
TAKER_FEE       = D(os.getenv("TAKER_FEE", "0.0018"))
TRAIL_MULTIPLIER= D(os.getenv("TRAIL_MULTIPLIER", "1.5"))
INTERVAL        = os.getenv("INTERVAL", "1")   # 1m для ATR

# --------------------------------------------------------------------------
# Redis (если есть)
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: 
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def state_key() -> str:
    # тот же ключ, что у бота
    return "bybit_spot_state_v3_ob"

def load_state() -> dict:
    # 1) Redis
    if rds:
        try:
            raw = rds.get(state_key())
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    # 2) файл
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    raw = json.dumps(state, ensure_ascii=False)
    # Redis
    if rds:
        try:
            rds.set(state_key(), raw)
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    # file
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception as e:
        logging.error(f"File save error: {e}")

# --------------------------------------------------------------------------
# Безопасный парсер Decimal
def Dsafe(x, default="0"):
    try:
        return D(str(x))
    except (InvalidOperation, TypeError):
        return D(default)

# Определяем минимальный «разумный» шаг количества
def choose_step(min_qty: D, qty_step: D, base_prec: D) -> D:
    """
    Бывает, что Bybit unified возвращает qty_step=1 (или вообще некорректно).
    Логика:
      1) если qty_step > 0 и qty_step < 1  -> используем qty_step
      2) иначе, если base_prec > 0 -> берём шаг = 10^(-base_prec)
      3) иначе, если min_qty > 0 -> берём шаг = ближайший порядок min_qty (<= min_qty)
      4) иначе fallback = 1e-6
    """
    if qty_step is None or qty_step <= 0:
        qty_step = D("0")
    if min_qty is None or min_qty <= 0:
        min_qty = D("0")
    if base_prec is None or base_prec < 0:
        base_prec = D("0")

    if qty_step > 0 and qty_step < 1:
        return qty_step

    if base_prec > 0:
        # base_prec – это количество знаков после запятой
        # если прислали десятичное, округляем до целого
        try:
            bp = int(base_prec)
            if bp > 0:
                return D("1") / (D("10") ** bp)
        except Exception:
            pass

    if min_qty > 0:
        # берём порядок min_qty
        # например, 0.000011 -> шаг 0.000001
        s = format(min_qty, 'f').rstrip('0')
        if '.' in s:
            decimals = len(s.split('.')[1])
            return D("1") / (D("10") ** decimals)
        else:
            # min_qty целое -> шаг 1
            return D("1")

    # запасной вариант
    return D("0.000001")

def floor_to_step(qty: D, step: D) -> D:
    """Обрезаем qty вниз к сетке шага step с Decimal, без сюрпризов."""
    if step <= 0:
        return qty
    return (qty // step) * step

def fetch_limits(session: HTTP, symbol: str):
    """
    Тянем инструменты и аккуратно читаем:
      - minOrderQty
      - qtyStep
      - basePrecision
      - minOrderAmt (не обязателен для seed, но покажем в логах)
    """
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        if it.get("symbol") != symbol:
            continue
        f = it.get("lotSizeFilter", {}) or {}
        min_qty   = Dsafe(f.get("minOrderQty"), "0")
        qty_step  = Dsafe(f.get("qtyStep"), "0")
        base_prec = Dsafe(it.get("basePrecision"), "0")  # иногда бывает строкой цифры
        min_amt   = Dsafe(it.get("minOrderAmt"), "0")
        step = choose_step(min_qty, qty_step, base_prec)
        return {
            "min_qty": min_qty,
            "qty_step_raw": qty_step,
            "base_precision": base_prec,
            "min_amt": min_amt,
            "step": step
        }
    # если не нашли символ:
    return {
        "min_qty": D("0"),
        "qty_step_raw": D("0"),
        "base_precision": D("0"),
        "min_amt": D("0"),
        "step": D("0.000001"),
    }

def fetch_atr(session: HTTP, symbol: str) -> D:
    try:
        r = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
        if df.empty:
            return D("0")
        df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
        atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
        return D(str(atr))
    except Exception:
        return D("0")

# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Seed a single spot position into the bot state (Redis + file)")
    parser.add_argument("--symbol", default=os.getenv("SEED_SYMBOL", "").strip(), help="e.g. BTCUSDT")
    parser.add_argument("--qty",    type=str, default=os.getenv("SEED_QTY", "").strip(), help="gross/base qty on wallet (e.g. 0.06296745)")
    parser.add_argument("--avg",    type=str, default=os.getenv("SEED_AVG", "").strip(), help="average entry price in USDT")
    parser.add_argument("--fee",    type=str, default=os.getenv("SEED_FEE", str(TAKER_FEE)))
    parser.add_argument("--trailx", type=str, default=os.getenv("SEED_TRAILX", str(TRAIL_MULTIPLIER)))
    args = parser.parse_args()

    symbol   = (args.symbol or "").upper()
    qty_raw  = Dsafe(args.qty)
    avg      = Dsafe(args.avg)
    taker    = Dsafe(args.fee, str(TAKER_FEE))
    trailx   = Dsafe(args.trailx, str(TRAIL_MULTIPLIER))

    # Валидация входов
    if not symbol:
        raise SystemExit("Missing symbol. Provide --symbol BTCUSDT (или SEED_SYMBOL в .env)")
    if qty_raw <= 0 or avg <= 0:
        raise SystemExit("Missing/invalid qty/avg. Используй --qty --avg ИЛИ SEED_QTY/SEED_AVG.")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    # Лимиты
    lim = fetch_limits(session, symbol)
    step     = lim["step"]
    min_qty  = lim["min_qty"]
    qty_step_raw = lim["qty_step_raw"]
    base_prec    = lim["base_precision"]

    logging.info(f"[LIMITS] {symbol} -> "
                 f"min_qty={min_qty}, qty_step_raw={qty_step_raw}, base_precision={base_prec}, "
                 f"chosen_step={step}, min_amt={lim['min_amt']}")

    # Приводим количество к сетке шага
    qty_gross = floor_to_step(qty_raw, step)

    if qty_gross <= 0:
        raise SystemExit(f"Provided qty {qty_raw} -> {qty_gross} (rounded) <= 0. "
                         f"Check step={step}. Попробуй задать SEED_QTY чуть больше или уменьши шаг вручную.")

    if min_qty > 0 and qty_gross < min_qty:
        raise SystemExit(f"Rounded qty {qty_gross} < min_qty {min_qty}. "
                         f"Увеличь SEED_QTY или уменьшай шаг (если уверен), см. chosen_step={step}.")

    # Пересчёт нетто (как в боте)
    qty_net = (D("1") - taker) * qty_gross

    # Стартовый TP от ATR
    atr = fetch_atr(session, symbol)
    if atr <= 0:
        # fallback: 0.1% от цены
        atr = avg * D("0.001")
    tp  = avg + trailx * atr

    # Загружаем и модифицируем состояние
    state = load_state()
    state.setdefault(symbol, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })
    state[symbol]["positions"] = [{
        "buy_price": float(avg),                 # хранение как float как в твоём коде
        "qty":       float(qty_net),
        "buy_qty_gross": float(qty_gross),
        "tp":        float(tp)
    }]
    state[symbol]["avg_count"] = 0
    state[symbol]["last_sell_price"] = 0.0
    state[symbol]["max_drawdown"] = 0.0

    save_state(state)

    # Резюме
    msg = (
        "♻️ Seeded position\n"
        f"{symbol}: qty_raw={qty_raw} → qty_gross={qty_gross} → qty_net={qty_net}\n"
        f"avg={avg}, ATR={atr}, start TP={tp}\n"
        f"step={step}, min_qty={min_qty}, fee={taker}, trailx={trailx}"
    )
    print(msg)
    send_tg(msg)

# --------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    try:
        main()
    except SystemExit as e:
        print(e)
    except Exception as e:
        print("ERROR:", e)
        print(traceback.format_exc())
