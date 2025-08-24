# -*- coding: utf-8 -*-
# One-off BTC seeder -> пишет позицию в Redis + state.json в формате бота

import os, json, math, logging, traceback, requests
from dotenv import load_dotenv
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

# === ДАННЫЕ СО СКРИНА (жёстко) ===
SYMBOL     = "BTCUSDT"
QTY_GROSS  = 0.06296745        # баланс кошелька BTC
AVG_PRICE  = 121472.27         # средняя/спот цена входа (USD за 1 BTC)

# === Параметры как в боте ===
TAKER_FEE        = 0.0018
TRAIL_MULTIPLIER = 1.5
INTERVAL         = "1"         # 1m для ATR

# Redis (если есть)
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

def state_key():
    return "bybit_spot_state_v3_ob"   # тот же ключ, что у бота

def load_state():
    # 1) Redis
    if rds:
        try:
            raw = rds.get(state_key())
            if raw: return json.loads(raw)
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
                "min_qty":  float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep",     1.0)),
                "min_amt":  float(it.get("minOrderAmt",10.0)),
            }
    return {"min_qty":0.0, "qty_step":1.0, "min_amt":10.0}

def fetch_atr(session: HTTP, symbol: str) -> float:
    r = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    if df.empty: return 0.0
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    return float(atr)

def main():
    if QTY_GROSS <= 0 or AVG_PRICE <= 0:
        raise SystemExit("Неверные константы QTY_GROSS/AVG_PRICE")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

    limits = fetch_limits(session, SYMBOL)
    qty_step = limits["qty_step"]; min_qty = limits["min_qty"]

    qty_gross = round_step(QTY_GROSS, qty_step)
    if qty_gross < max(min_qty, 1e-12):
        raise SystemExit(f"Provided qty {qty_gross} < min_qty {min_qty}")

    qty_net = qty_gross * (1.0 - TAKER_FEE)

    try:
        atr = fetch_atr(session, SYMBOL)
    except Exception:
        atr = 0.0

    tp = AVG_PRICE + TRAIL_MULTIPLIER * (atr if atr > 0 else max(1e-6, AVG_PRICE * 0.001))

    state = load_state()
    state.setdefault(SYMBOL, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })

    state[SYMBOL]["positions"] = [{
        "buy_price": AVG_PRICE,
        "qty": qty_net,
        "buy_qty_gross": qty_gross,
        "tp": tp
    }]
    state[SYMBOL]["avg_count"] = 0
    state[SYMBOL]["last_sell_price"] = 0.0
    state[SYMBOL]["max_drawdown"] = 0.0

    save_state(state)

    msg = (f"♻️ Seeded BTC position\n"
           f"{SYMBOL}: qty_gross={qty_gross:.8f}, qty_net={qty_net:.8f}\n"
           f"avg={AVG_PRICE:.2f}, start TP={tp:.2f} (ATR-based)\n"
           f"fee={TAKER_FEE:.4f}, trailx={TRAIL_MULTIPLIER}")
    print(msg); send_tg(msg)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    try:
        main()
    except SystemExit as e:
        print(e)
    except Exception as e:
        print("ERROR:", e)
        print(traceback.format_exc())
