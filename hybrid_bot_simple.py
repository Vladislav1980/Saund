# -*- coding: utf-8 -*-
# One-off seeder for a spot position into the bot's state (Redis + state.json)
# Совместим с форматом состояния вашего бота (ключ Redis: bybit_spot_state_v3_ob)

import os, json, math, argparse, logging, traceback, decimal, re
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
import requests
import pandas as pd

from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

load_dotenv()

# === ENV ===
API_KEY     = os.getenv("BYBIT_API_KEY", "")
API_SECRET  = os.getenv("BYBIT_API_SECRET", "")
TG_TOKEN    = os.getenv("TG_TOKEN", "")
CHAT_ID     = os.getenv("CHAT_ID", "")
REDIS_URL   = os.getenv("REDIS_URL", "")
STATE_FILE  = "state.json"

# Базовые параметры как в боте
DEFAULT_TAKER_FEE    = float(os.getenv("TAKER_FEE", "0.0018"))
DEFAULT_TRAILX       = float(os.getenv("TRAIL_MULTIPLIER", "1.5"))
INTERVAL             = "1"   # 1m kline
STATE_KEY            = "bybit_spot_state_v3_ob"

# --- Redis (если доступен) ---
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

def load_state() -> dict:
    if rds:
        try:
            raw = rds.get(STATE_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    raw = json.dumps(state, ensure_ascii=False)
    if rds:
        try:
            rds.set(STATE_KEY, raw)
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception as e:
        logging.error(f"File save error: {e}")

# --- утилиты чисел ---
def _to_decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (float, int)):
        return Decimal(str(x))
    s = str(x).strip().replace(",", ".")
    # удалить пробелы в числе и скрытые символы
    s = re.sub(r"[^\d\.\-eE]", "", s)
    return Decimal(s)

def _count_decimals(d: Decimal) -> int:
    tup = d.normalize().as_tuple()
    return max(0, -tup.exponent)

def _round_down_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return qty
    # Количество знаков – по step
    places = _count_decimals(step)
    scale = Decimal(10) ** places
    return ( (qty * scale).to_integral_value(rounding=ROUND_DOWN) / scale ).quantize(step, rounding=ROUND_DOWN)

# --- Bybit helpers ---
def fetch_limits(session: HTTP, symbol: str) -> dict:
    """
    Возвращает min_qty, qty_step, min_amt для символа.
    На некоторых символах Bybit может отдать qty_step=1.
    Тогда подхватываем precision из min_qty.
    """
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        if it.get("symbol") == symbol:
            f = it.get("lotSizeFilter", {}) or {}
            min_qty = _to_decimal(f.get("minOrderQty", "0"))
            qty_step = _to_decimal(f.get("qtyStep", "1"))
            min_amt = _to_decimal(it.get("minOrderAmt", "10"))
            # фиксим странный qty_step=1
            if qty_step == 1:
                # берём точность по min_qty
                decs = max( _count_decimals(min_qty), 1 )
                qty_step = Decimal(1) / (Decimal(10) ** decs)
            return {
                "min_qty": min_qty,
                "qty_step": qty_step,
                "min_amt": min_amt
            }
    # fallback
    return {"min_qty": Decimal("0"), "qty_step": Decimal("1"), "min_amt": Decimal("10")}

def fetch_atr(session: HTTP, symbol: str) -> float:
    r = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    if df.empty:
        return 0.0
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    return float(atr)

def parse_inputs_from_env_and_args() -> tuple[str, Decimal, Decimal, float, float]:
    """
    Сбор входных параметров:
      - CLI (--symbol/--qty/--avg/--fee/--trailx)
      - ENV (SEED_SYMBOL, SEED_QTY, SEED_AVG, SEED_FEE, SEED_TRAILX)
    Если нет ни там, ни там — подставим ваши btc‑значения со скрина (разовый дефолт).
    """
    p = argparse.ArgumentParser(description="Seed single spot position into bot state")
    p.add_argument("--symbol", type=str, default=os.getenv("SEED_SYMBOL", "").strip())
    p.add_argument("--qty",    type=str, default=os.getenv("SEED_QTY", "").strip())
    p.add_argument("--avg",    type=str, default=os.getenv("SEED_AVG", "").strip())
    p.add_argument("--fee",    type=str, default=os.getenv("SEED_FEE", str(DEFAULT_TAKER_FEE)))
    p.add_argument("--trailx", type=str, default=os.getenv("SEED_TRAILX", str(DEFAULT_TRAILX)))
    args = p.parse_args()

    symbol = (args.symbol or "").upper()

    # если символ/кол-во/средняя пустые — используем ваши данные (BTCUSDT)
    if not symbol:
        symbol = "BTCUSDT"
    qty_raw = _to_decimal(args.qty or "0")
    avg = _to_decimal(args.avg or "0")

    if qty_raw == 0 or avg == 0:
        # дефолт — ПОД ВАШ СЛУЧАЙ СО СКРИНА
        qty_raw = _to_decimal("0.06296745")
        avg     = _to_decimal("121472.27")

    fee = float(str(args.fee).replace(",", "."))
    trailx = float(str(args.trailx).replace(",", "."))

    logging.info(f"[INPUT] symbol={symbol}, qty(raw)={qty_raw}, avg={avg}, fee={fee}, trailx={trailx}")
    return symbol, qty_raw, avg, fee, trailx

def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit("No BYBIT_API_KEY / BYBIT_API_SECRET in env")

    symbol, qty_raw, buy_price, taker, trailx = parse_inputs_from_env_and_args()

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    # лимиты и аккуратное округление
    limits = fetch_limits(session, symbol)
    min_qty  = limits["min_qty"]
    qty_step = limits["qty_step"]

    logging.info(f"[LIMITS] {symbol} -> {{'min_qty': {min_qty}, 'qty_step': {qty_step}}}")

    if qty_raw <= 0 or buy_price <= 0:
        raise SystemExit(f"Invalid qty/avg: qty={qty_raw}, avg={buy_price}")

    # округляем к шагу
    qty_gross = _round_down_to_step(qty_raw, qty_step)
    if qty_gross < min_qty:
        raise SystemExit(f"Provided qty {qty_gross} < min_qty {min_qty}")

    # как в боте: сохраняем qty нетто
    qty_net = (qty_gross * Decimal(str(1 - taker))).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    try:
        atr = fetch_atr(session, symbol)
    except Exception:
        atr = 0.0

    # стартовый TP вокруг buy_price + trailx*ATR (если ATR нет — небольшой процент)
    tp = float(buy_price) + trailx * (atr if atr > 0 else float(buy_price) * 0.001)

    # грузим и дополняем состояние
    state = load_state()
    state.setdefault(symbol, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })

    state[symbol]["positions"] = [{
        "buy_price": float(buy_price),
        "qty": float(qty_net),
        "buy_qty_gross": float(qty_gross),
        "tp": float(tp)
    }]
    state[symbol]["avg_count"] = 0
    state[symbol]["last_sell_price"] = 0.0
    state[symbol]["max_drawdown"] = 0.0

    save_state(state)

    msg = (
        "♻️ Seeded position\n"
        f"{symbol}: qty_gross={qty_gross}, qty_net={qty_net}\n"
        f"avg={buy_price}, start TP={Decimal(str(tp)).quantize(Decimal('0.01'))}\n"
        f"fee={taker}, trailx={trailx}"
    )
    print(msg)
    send_tg(msg)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    try:
        main()
    except SystemExit as e:
        print(e)
    except Exception as e:
        print("ERROR:", e)
        print(traceback.format_exc())
