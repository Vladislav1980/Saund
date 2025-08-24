# -*- coding: utf-8 -*-
# One-off BTC seeder for the bot state (GitHub/local run, no Railway required)
# Записывает позицию BTCUSDT в Redis + state.json в формате твоего бота.

import os, json, math, logging, argparse, traceback
from dotenv import load_dotenv
import requests
import pandas as pd
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
STATE_FILE = "state.json"

# Комиссия/трейл — как в боте
TAKER_FEE        = float(os.getenv("TAKER_FEE", 0.0018))
TRAIL_MULTIPLIER = float(os.getenv("TRAIL_MULTIPLIER", 1.5))
INTERVAL         = "1"  # 1m kline для ATR

# === Redis
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

def send_tg(text: str):
    if not TG_TOKEN or not CHAT_ID: 
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def key_state():
    # тот же ключ, что у твоего бота
    return "bybit_spot_state_v3_ob"

def load_state() -> dict:
    # 1) Redis
    if rds:
        try:
            raw = rds.get(key_state())
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    # 2) Файл
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    raw = json.dumps(state, ensure_ascii=False)
    if rds:
        try:
            rds.set(key_state(), raw)
        except Exception as e:
            logging.error(f"Redis save error: {e}")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception as e:
        logging.error(f"File save error: {e}")

def round_step(qty: float, step: float) -> float:
    try:
        step = float(step)
        if step <= 0:
            return qty
        exp = int(f"{step:e}".split("e")[-1])  # напр. -6
        scale = 10 ** abs(exp)
        return math.floor(qty * scale + 1e-12) / scale
    except Exception:
        return qty

def fetch_limits(session: HTTP, symbol: str):
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        if it.get("symbol") == symbol:
            f = it.get("lotSizeFilter", {})
            return {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(it.get("minOrderAmt", 10.0)),
            }
    return {"min_qty": 0.0, "qty_step": 1.0, "min_amt": 10.0}

def fetch_atr(session: HTTP, symbol: str) -> float:
    r = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    if df.empty:
        return 0.0
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    return float(atr)

def main():
    parser = argparse.ArgumentParser(description="Seed BTC position into bot state")
    parser.add_argument("--symbol", default=os.getenv("SEED_SYMBOL", "BTCUSDT"))
    # Значения со скринов: qty_gross и avg
    parser.add_argument("--qty", type=float, default=float(os.getenv("SEED_QTY", 0.06296745)))
    parser.add_argument("--avg", type=float, default=float(os.getenv("SEED_AVG", 121472.27)))
    parser.add_argument("--fee", type=float, default=float(os.getenv("TAKER_FEE", TAKER_FEE)))
    parser.add_argument("--trailx", type=float, default=float(os.getenv("TRAIL_MULTIPLIER", TRAIL_MULTIPLIER)))
    args = parser.parse_args()

    symbol = args.symbol.upper().strip()
    qty_gross = float(args.qty)
    buy_price = float(args.avg)
    taker     = float(args.fee)
    trailx    = float(args.trailx)

    if not API_KEY or not API_SECRET:
        raise SystemExit("BYBIT_API_KEY/SECRET не заданы в .env")

    if qty_gross <= 0 or buy_price <= 0:
        raise SystemExit(f"Некорректные входные: qty={qty_gross}, avg={buy_price}")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    # Лимиты
    limits = fetch_limits(session, symbol)
    qty_step = limits["qty_step"]
    min_qty  = limits["min_qty"]

    # Приводим кол-во к шагу
    qty_gross = round_step(qty_gross, qty_step)
    if qty_gross < max(min_qty, 1e-12):
        raise SystemExit(f"Provided qty {qty_gross} < min_qty {min_qty}")

    qty_net = qty_gross * (1.0 - taker)

    # ATR -> стартовый TP
    try:
        atr = fetch_atr(session, symbol)
    except Exception:
        atr = 0.0
    tp = buy_price + trailx * (atr if atr > 0 else max(1e-6, buy_price * 0.001))

    # Загружаем + гарантируем каркас
    state = load_state()
    state.setdefault(symbol, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })

    # Перезаписываем позицию символа одной агрегированной записью
    state[symbol]["positions"] = [{
        "buy_price": buy_price,
        "qty": qty_net,
        "buy_qty_gross": qty_gross,
        "tp": tp
    }]
    state[symbol]["avg_count"] = 0
    state[symbol]["last_sell_price"] = 0.0
    state[symbol]["max_drawdown"] = 0.0

    save_state(state)

    msg = (
        "♻️ BTC seeded into bot state\n"
        f"{symbol}: qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}\n"
        f"avg={buy_price:.2f}, start TP={tp:.2f} (ATR-based)\n"
        f"fee={taker:.4f}, trailx={trailx}"
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
