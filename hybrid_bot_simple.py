# -*- coding: utf-8 -*-
# seed_position.py
# One-off seeder of a spot position into the bot state (Redis + state.json)
# Работает и с CLI‑аргументами, и с ENV (для Railway).

import os, json, math, argparse, logging, traceback
from dotenv import load_dotenv
import requests
import pandas as pd
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

load_dotenv()

API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# те же дефолты, что в боте
DEFAULT_TAKER_FEE   = float(os.getenv("TAKER_FEE", 0.0018))
DEFAULT_TRAIL_MULTI = float(os.getenv("TRAIL_MULTIPLIER", 1.5))
INTERVAL = os.getenv("SEED_ATR_INTERVAL", "1")  # 1m

# Redis
try:
    import redis
    rds = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    rds = None

def state_key():
    # тот же ключ, что использует твой бот
    return "bybit_spot_state_v3_ob"

def send_tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: 
        logging.info("TG: skipped (no token/chat)")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        logging.error(f"TG send failed: {e}")

def load_state():
    # 1) Redis
    if rds:
        try:
            raw = rds.get(state_key())
            if raw:
                return json.loads(raw)
        except Exception as e:
            logging.warning(f"Redis read err: {e}")
    # 2) File
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    raw = json.dumps(state, ensure_ascii=False)
    if rds:
        try:
            rds.set(state_key(), raw)
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
        # step вида 1, 0.1, 0.01, 0.0001 ...
        exp = int(f"{step:e}".split("e")[-1])  # например, -6
        scale = 10 ** abs(exp)
        return math.floor(qty * scale + 1e-12) / scale
    except Exception:
        return qty

def fetch_limits(session: HTTP, symbol: str):
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        if it.get("symbol") == symbol:
            f = it.get("lotSizeFilter", {}) or {}
            return {
                "min_qty":  float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt":  float(it.get("minOrderAmt", 10.0)),
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

def parse_inputs():
    p = argparse.ArgumentParser(description="Seed single spot position into bot state")
    p.add_argument("--symbol", help="e.g. BTCUSDT")
    p.add_argument("--qty", type=float, help="gross/base qty on wallet (e.g. 0.06296745)")
    p.add_argument("--avg", type=float, help="average entry price in USDT (e.g. 121472.27)")
    p.add_argument("--fee", type=float, help="taker fee (default from ENV or 0.0018)")
    p.add_argument("--trailx", type=float, help="trail multiplier (default from ENV or 1.5)")
    args = p.parse_args()

    # если аргументов нет (Railway), берем из ENV
    symbol = (args.symbol or os.getenv("SEED_SYMBOL", "")).strip().upper()
    qty     = args.qty if args.qty is not None else float(os.getenv("SEED_QTY", "0") or 0)
    avg     = args.avg if args.avg is not None else float(os.getenv("SEED_AVG", "0") or 0)
    fee     = args.fee if args.fee is not None else float(os.getenv("SEED_FEE", DEFAULT_TAKER_FEE))
    trailx  = args.trailx if args.trailx is not None else float(os.getenv("SEED_TRAILX", DEFAULT_TRAIL_MULTI))

    logging.info(f"[INPUT] symbol={symbol!r}, qty(raw)={qty}, avg={avg}, fee={fee}, trailx={trailx}")
    src = "CLI" if args.symbol or args.qty or args.avg else "ENV"
    logging.info(f"[INPUT] source={src}")

    if not symbol or qty <= 0 or avg <= 0:
        raise SystemExit("Missing/invalid inputs. Provide --symbol/--qty/--avg OR set SEED_SYMBOL/SEED_QTY/SEED_AVG.")

    return symbol, qty, avg, fee, trailx

def main():
    symbol, qty_gross, buy_price, taker, trailx = parse_inputs()

    if not API_KEY or not API_SECRET:
        raise SystemExit("BYBIT_API_KEY/SECRET not set")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    limits = fetch_limits(session, symbol)
    min_qty  = float(limits["min_qty"])
    qty_step = float(limits["qty_step"])
    logging.info(f"[LIMITS] {symbol}: {limits}")

    qty_gross_rounded = round_step(qty_gross, qty_step)
    logging.info(f"[QTY] qty_input={qty_gross}, qty_rounded={qty_gross_rounded}, min_qty={min_qty}")

    if qty_gross_rounded < max(min_qty, 1e-12):
        raise SystemExit(f"Provided qty {qty_gross_rounded} < min_qty {min_qty}. Check SEED_QTY (--qty).")

    qty_net = qty_gross_rounded * (1.0 - taker)

    # ATR -> стартовый TP
    try:
        atr = fetch_atr(session, symbol)
    except Exception as e:
        logging.warning(f"ATR fetch error: {e}")
        atr = 0.0
    tp = buy_price + trailx * (atr if atr > 0 else max(1e-6, buy_price * 0.001))

    # загрузка состояния
    state = load_state()
    if symbol not in state:
        state[symbol] = {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0,
                         "last_sell_price": 0.0, "max_drawdown": 0.0}

    state[symbol]["positions"] = [{
        "buy_price": buy_price,
        "qty": qty_net,
        "buy_qty_gross": qty_gross_rounded,
        "tp": tp
    }]
    state[symbol]["avg_count"] = 0
    state[symbol]["last_sell_price"] = 0.0
    state[symbol]["max_drawdown"] = 0.0

    save_state(state)

    msg = (f"♻️ Seeded position\n"
           f"{symbol}: qty_gross={qty_gross_rounded:.8f}, qty_net={qty_net:.8f}\n"
           f"avg={buy_price:.2f}, start TP={tp:.2f}\n"
           f"fee={taker:.4f}, trailx={trailx}\n"
           f"limits: min_qty={min_qty}, step={qty_step}")
    print(msg)
    logging.info(msg)
    send_tg(msg)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    try:
        main()
    except SystemExit as e:
        logging.error(str(e))
        print(str(e))
    except Exception as e:
        logging.error("ERROR: %s", e)
        print("ERROR:", e)
        print(traceback.format_exc())
