# -*- coding: utf-8 -*-
# One-off seeder for a position into bot's state (Redis + state.json)
# Совместим с запуском из GitHub/локально/на сервере.
#
# Источники входа (по приоритету):
# 1) CLI args: --symbol --qty --avg [--fee --trailx]
# 2) ENV: SEED_SYMBOL, SEED_QTY, SEED_AVG, SEED_FEE, SEED_TRAILX
# 3) Fallback-константы (ниже в коде) — на случай, если п.1 и п.2 не заданы.

import os, json, math, argparse, logging, traceback
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
import requests
import pandas as pd
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# ====== Fallback (если не придут CLI/ENV) ======
FALLBACK = {
    "symbol": "BTCUSDT",
    "qty":    "0.06296745",     # из скринов
    "avg":    "121472.27",      # средняя закупка из скринов
    "fee":    "0.0018",
    "trailx": "1.5",
}

# ====== Key/ENV загрузка ======
load_dotenv()
API_KEY    = os.getenv("BYBIT_API_KEY") or ""
API_SECRET = os.getenv("BYBIT_API_SECRET") or ""
TG_TOKEN   = os.getenv("TG_TOKEN") or ""
CHAT_ID    = os.getenv("CHAT_ID") or ""
REDIS_URL  = os.getenv("REDIS_URL") or ""
STATE_FILE = "state.json"

# те же параметры, что в боте
INTERVAL = "1"  # min kline для ATR
STATE_KEY = "bybit_spot_state_v3_ob"   # ключ состояния как в боте

# ====== Redis ======
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

def dec_round_step(qty: Decimal, step: Decimal) -> Decimal:
    """Округление в сторону понижения к шагу через Decimal."""
    if step <= 0:
        return qty
    # сколько знаков после запятой у шага
    step_str = format(step, "f")
    if "." in step_str:
        places = len(step_str.split(".")[1])
    else:
        places = 0
    q = qty.quantize(step, rounding=ROUND_DOWN) if places else (qty // step) * step
    return q

def load_state() -> dict:
    # сначала Redis
    if rds:
        try:
            raw = rds.get(STATE_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    # затем файл
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

def fetch_limits(session: HTTP, symbol: str):
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for it in data:
        if it.get("symbol") == symbol:
            f = it.get("lotSizeFilter", {})
            return {
                "min_qty":  Decimal(str(f.get("minOrderQty", "0"))),
                "qty_step": Decimal(str(f.get("qtyStep", "1"))),
                "min_amt":  Decimal(str(it.get("minOrderAmt", "10"))),
            }
    return {"min_qty": Decimal("0"), "qty_step": Decimal("1"), "min_amt": Decimal("10")}

def fetch_atr(session: HTTP, symbol: str) -> float:
    r = session.get_kline(category="spot", symbol=symbol, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    if df.empty:
        return 0.0
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0

def get_inputs_from_env():
    return {
        "symbol": os.getenv("SEED_SYMBOL", "").strip(),
        "qty":    os.getenv("SEED_QTY", "").strip(),
        "avg":    os.getenv("SEED_AVG", "").strip(),
        "fee":    os.getenv("SEED_FEE", "").strip(),
        "trailx": os.getenv("SEED_TRAILX", "").strip(),
    }

def resolve_inputs(cli_args):
    """Собираем входы: CLI -> ENV -> FALLBACK. Заодно печатаем, что именно взяли."""
    env = get_inputs_from_env()
    src = {}

    symbol = (cli_args.symbol or env["symbol"] or FALLBACK["symbol"]).upper()
    qty    = (cli_args.qty    or env["qty"]    or FALLBACK["qty"])
    avg    = (cli_args.avg    or env["avg"]    or FALLBACK["avg"])
    fee    = (cli_args.fee    if cli_args.fee is not None else (env["fee"] or FALLBACK["fee"]))
    trailx = (cli_args.trailx if cli_args.trailx is not None else (env["trailx"] or FALLBACK["trailx"]))

    # источники (для дебага)
    src["symbol"] = "CLI" if cli_args.symbol else ("ENV" if env["symbol"] else "FALLBACK")
    src["qty"]    = "CLI" if cli_args.qty    else ("ENV" if env["qty"]    else "FALLBACK")
    src["avg"]    = "CLI" if cli_args.avg    else ("ENV" if env["avg"]    else "FALLBACK")
    src["fee"]    = "CLI" if cli_args.fee is not None else ("ENV" if env["fee"] else "FALLBACK")
    src["trailx"] = "CLI" if cli_args.trailx is not None else ("ENV" if env["trailx"] else "FALLBACK")

    logging.info(f"[INPUT] symbol={symbol} ({src['symbol']}), "
                 f"qty(raw)={qty} ({src['qty']}), avg={avg} ({src['avg']}), "
                 f"fee={fee} ({src['fee']}), trailx={trailx} ({src['trailx']})")

    # приведение типов
    try:
        qty_dec = Decimal(str(qty))
    except Exception:
        qty_dec = Decimal("0")

    try:
        avg_f = float(avg)
    except Exception:
        avg_f = 0.0

    try:
        fee_f = float(fee)
    except Exception:
        fee_f = float(FALLBACK["fee"])

    try:
        trailx_f = float(trailx)
    except Exception:
        trailx_f = float(FALLBACK["trailx"])

    return symbol, qty_dec, avg_f, fee_f, trailx_f

def main():
    parser = argparse.ArgumentParser(description="Seed single position into bot state")
    parser.add_argument("--symbol", type=str, default=None, help="e.g. BTCUSDT")
    parser.add_argument("--qty",    type=str, default=None, help="gross/base qty on wallet, e.g. 0.06296745")
    parser.add_argument("--avg",    type=str, default=None, help="average entry price in USDT")
    parser.add_argument("--fee",    type=float, default=None)
    parser.add_argument("--trailx", type=float, default=None)
    args = parser.parse_args()

    symbol, qty_gross_dec, buy_price, taker, trailx = resolve_inputs(args)

    if qty_gross_dec <= 0 or buy_price <= 0:
        logging.error("Missing/invalid inputs. Provide --symbol/--qty/--avg OR set SEED_SYMBOL/SEED_QTY/SEED_AVG.")
        raise SystemExit("Missing/invalid inputs. Provide --symbol/--qty/--avg OR set SEED_SYMBOL/SEED_QTY/SEED_AVG.")

    session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
    limits = fetch_limits(session, symbol)
    logging.info(f"[LIMITS] {symbol} -> {limits}")

    # округление qty по шагу
    qty_before = qty_gross_dec
    qty_gross_dec = dec_round_step(qty_gross_dec, limits["qty_step"])
    if qty_gross_dec < limits["min_qty"]:
        raise SystemExit(f"Provided qty {qty_before} -> {qty_gross_dec} (rounded) < min_qty {limits['min_qty']}")

    # net с учётом комиссии
    qty_net_dec = qty_gross_dec * Decimal(str(1.0 - taker))

    # ATR для TP
    try:
        atr = fetch_atr(session, symbol)
    except Exception:
        atr = 0.0
    tp = buy_price + trailx * (atr if atr > 0 else max(1e-6, buy_price * 0.001))

    # загрузка/обновление состояния
    state = load_state()
    state.setdefault(symbol, {
        "positions": [],
        "pnl": 0.0, "count": 0, "avg_count": 0,
        "last_sell_price": 0.0, "max_drawdown": 0.0
    })
    state[symbol]["positions"] = [{
        "buy_price": buy_price,
        "qty": float(qty_net_dec),
        "buy_qty_gross": float(qty_gross_dec),
        "tp": tp
    }]
    state[symbol]["avg_count"] = 0
    state[symbol]["last_sell_price"] = 0.0
    state[symbol]["max_drawdown"] = 0.0

    save_state(state)

    msg = (
        f"♻️ Seeded position\n"
        f"{symbol}: qty_gross={qty_gross_dec.normalize():f}, qty_net={qty_net_dec.normalize():f}\n"
        f"avg={buy_price:.2f}, start TP={tp:.2f} (ATR-based={atr:.6f})\n"
        f"fee={taker:.4f}, trailx={trailx}"
    )
    print(msg)
    logging.info(msg)
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
