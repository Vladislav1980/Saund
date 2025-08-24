# -*- coding: utf-8 -*-
# One-off seeder for BTC position into bot's state (Redis + state.json)

import os, json, math, argparse, logging, traceback
from dotenv import load_dotenv
import requests
from pybit.unified_trading import HTTP
import pandas as pd
from ta.volatility import AverageTrueRange

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
STATE_FILE = "state.json"

# те же параметры, что в боте
TAKER_FEE = 0.0018
TRAIL_MULTIPLIER = 1.5
INTERVAL = "1"  # 1m kline

# --- FALLBACK DEFAULTS (твои текущие) ---
FALLBACK_SYMBOL = os.getenv("SEED_SYMBOL", "BTCUSDT")
FALLBACK_QTY    = float(os.getenv("SEED_BTC_QTY", 0.06296745))
FALLBACK_AVG    = float(os.getenv("SEED_BTC_AVG", 121472.27))

# Redis
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def state_key(): return "bybit_spot_state_v3_ob"

def load_state():
    if rds:
        try:
            raw = rds.get(state_key())
            if raw: return json.loads(raw)
        except Exception: pass
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    raw = json.dumps(state, ensure_ascii=False)
    if rds:
        try: rds.set(state_key(), raw)
        except Exception as e: logging.error(f"Redis save error: {e}")
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f: f.write(raw)
    except Exception as e:
        logging.error(f"File save error: {e}")

def round_step(qty: float, step: float) -> float:
    try:
        step = float(step)
        if step == 0: return qty
        exp = int(f"{step:e}".split("e")[-1])
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
    if df.empty: return 0.0
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    return float(atr)

def resolve_inputs():
    parser = argparse.ArgumentParser(description="Seed single position into bot state")
    parser.add_argument("--symbol", help="e.g. BTCUSDT")
    parser.add_argument("--qty", type=float, help="gross/base qty on wallet (e.g. 0.06296745)")
    parser.add_argument("--avg", type=float, help="average entry price in USDT")
    parser.add_argument("--fee", type=float, default=float(os.getenv("TAKER_FEE", TAKER_FEE)))
    parser.add_argument("--trailx", type=float, default=float(os.getenv("TRAIL_MULTIPLIER", TRAIL_MULTIPLIER)))
    args = parser.parse_args()

    symbol = (args.symbol or os.getenv("SEED_SYMBOL") or FALLBACK_SYMBOL).upper()
    qty_gross = args.qty if args.qty is not None else \
                (float(os.getenv("SEED_BTC_QTY")) if os.getenv("SEED_BTC_QTY") else FALLBACK_QTY)
    buy_price = args.avg if args.avg is not None else \
                (float(os.getenv("SEED_BTC_AVG")) if os.getenv("SEED_BTC_AVG") else FALLBACK_AVG)
    return symbol, float(qty_gross), float(buy_price), float(args.fee), float(args.trailx)

def main():
    symbol, qty_gross, buy_price, taker, trailx = resolve_inputs()
    if qty_gross <= 0 or buy_price <= 0:
        raise SystemExit(f"qty/avg must be > 0; got qty={qty_gross}, avg={buy_price}")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    limits = fetch_limits(session, symbol)
    qty_step = limits["qty_step"]; min_qty = limits["min_qty"]

    qty_gross = round_step(qty_gross, qty_step)
    if qty_gross < max(min_qty, 1e-12):
        raise SystemExit(f"Provided qty {qty_gross} < min_qty {min_qty}")

    qty_net = qty_gross * (1.0 - taker)

    try: atr = fetch_atr(session, symbol)
    except Exception: atr = 0.0
    tp = buy_price + trailx * (atr if atr > 0 else max(1e-6, buy_price * 0.001))

    state = load_state()
    state.setdefault(symbol, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })
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
        f"♻️ Seeded position\n"
        f"{symbol}: qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}\n"
        f"avg={buy_price:.2f}, start TP={tp:.2f} (ATR-based)\n"
        f"fee={taker:.4f}, trailx={trailx}"
    )
    print(msg); send_tg(msg)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    try:
        main()
    except SystemExit as e:
        print(e)
    except Exception as e:
        print("ERROR:", e); print(traceback.format_exc())
