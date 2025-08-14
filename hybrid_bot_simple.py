# -*- coding: utf-8 -*-
# Bybit Spot bot (v3.2 lite)
# - Net PnL >= 1 USD после обеих комиссий (taker 0.0018)
# - Предвалидация ордеров: min_amt/min_qty/qty_step + afford(комиссия включена)
# - Без излишних усложнений. Логика твоего v3.1 сохранена, только расчёт qty и PnL.

import os, time, math, logging, datetime, json, traceback
import pandas as pd
import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# --------------- ENV / CONFIG -----------------
load_dotenv()
API_KEY   = os.getenv("BYBIT_API_KEY")
API_SECRET= os.getenv("BYBIT_API_SECRET")
TG_TOKEN  = os.getenv("TG_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]

# Комиссия биржи (taker). Считаем обе стороны.
TAKER_FEE = 0.0018

TG_VERBOSE        = True
RESERVE_BALANCE   = 1.0
MAX_TRADE_USDT    = 35.0
TRAIL_MULTIPLIER  = 1.5
MAX_DRAWDOWN      = 0.10
MAX_AVERAGES      = 3
MIN_PROFIT_PCT    = 0.005           # относ. порог дополнительно к 1 USD
MIN_NET_USD       = 1.00            # МИНИМАЛЬНЫЙ net-профит после обеих комиссий
STOP_LOSS_PCT     = 0.03

STATE_FILE = "state.json"
INTERVAL   = "1"

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
    except Exception:
        logging.error("Telegram send failed")

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

# --------------- BYBIT SESSION ----------------
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}
LAST_REPORT_DATE = None
cycle_count = 0

# --------------- STATE ------------------------
def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)

def init_state():
    global STATE
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            STATE = json.load(f)
        log("✅ Loaded state from file", True)
    except Exception:
        STATE = {}
        log("ℹ No valid state file — starting fresh", True)

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],      # [{buy_price, qty_net, qty_gross, tp}]
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0
        })

def log_trade(sym, side, price, qty_net, pnl_net, info=""):
    usdt_val = price * qty_net
    msg = f"{side} {sym} @ {price:.6f}, qty={qty_net:.8f}, USDT≈{usdt_val:.2f}, PnL(net)={pnl_net:.2f}. {info}"
    log(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty_net},{usdt_val},{pnl_net},{info}\n")
    save_state()

# --------------- MARKET INFO ------------------
def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        sym = item["symbol"]
        if sym in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[sym] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0))
            }

def _floor_step(x: float, step: float) -> float:
    # универсально для 0.01 / 0.1 / 1 и т.п.
    if step <= 0:
        return x
    k = round(1.0 / step)
    return math.floor(x * k) / k

def adjust_qty(qty: float, step: float) -> float:
    try:
        return _floor_step(float(qty), float(step))
    except Exception:
        return qty

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balance():
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next(c["walletBalance"] for c in coins if c["coin"]=="USDT"))

def get_coin_balance(sym):
    co = sym.replace("USDT","")
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next((c["walletBalance"] for c in coins if c["coin"]==co), 0.0))

# ---------- сигналы (как было) ----------
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    df["atr"]   = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["sig"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = f"EMA9={last.ema9:.4f},EMA21={last.ema21:.4f},RSI={last.rsi:.2f},MACD={last.macd:.4f},SIG={last.sig:.4f}"
    if last.ema9 > last.ema21 and last.rsi > 50 and last.macd > last.sig:
        return "buy", last.atr, info
    elif last.ema9 < last.ema21 and last.rsi < 50 and last.macd < last.sig:
        return "sell", last.atr, info
    return "none", last.atr, info

# ---------- помощники по qty / PnL ----------
def afford_qty_gross(sym: str, price: float, usdt_avail: float) -> float:
    """Считаем покупаемое GROSS‑количество так, чтобы:
       - хватило USDT даже с учётом комиссии на покупку
       - выполнялись min_amt/min_qty/qty_step.
    """
    lim = LIMITS[sym]
    # бюджет под одну покупку
    budget = min(usdt_avail, MAX_TRADE_USDT)
    if budget <= 0:
        return 0.0
    # учтём, что комиссия берётся в USDT на покупку
    budget_eff = budget / (1.0 + TAKER_FEE)
    qty = budget_eff / price
    qty = adjust_qty(qty, lim["qty_step"])

    # проверим нижние пороги
    if qty < lim["min_qty"]:
        return 0.0
    if qty * price < lim["min_amt"]:
        # добиваем до min_amt по шагу, но не за предел бюджета
        need_qty = lim["min_amt"] / price
        need_qty = adjust_qty(need_qty, lim["qty_step"])
        # проверим, вписываемся ли в бюджет c комиссией
        if need_qty * price * (1.0 + TAKER_FEE) <= budget + 1e-9:
            qty = need_qty
        else:
            return 0.0
    return qty

def cap_sell_qty_to_balance(sym: str, qty_net: float) -> float:
    """Не продаём больше, чем есть на кошельке, с учётом шага."""
    bal = get_coin_balance(sym)
    q = min(bal, qty_net)
    return adjust_qty(q, LIMITS[sym]["qty_step"])

def pnl_net_after_fees(buy_price: float, sell_price: float, qty_net: float) -> float:
    """Корректный PnL(net) при хранении qty_net (после fee на покупке).
       На продаже снова возьмут taker‑fee, т.е. выручка уменьшится на (1 - fee).
       Стоимость покупки равна buy_price * qty_gross, где qty_gross = qty_net / (1 - fee).
    """
    qty_gross_buy = qty_net / (1.0 - TAKER_FEE)
    cost_usdt     = buy_price  * qty_gross_buy           # на покупку
    proceeds_usdt = sell_price * qty_net * (1.0 - TAKER_FEE)  # на продажу
    return proceeds_usdt - cost_usdt

# ---------- логика бота ----------
def get_qty(sym, price, usdt_avail) -> float:
    """Возвращаем GROSS‑количество к покупке (под ордер)."""
    if sym not in LIMITS:
        return 0.0
    return afford_qty_gross(sym, price, usdt_avail)

def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty: 
            continue
        price = float(df["c"].iloc[-1])
        bal   = get_coin_balance(sym)
        if price and bal * price >= LIMITS.get(sym,{}).get("min_amt",0):
            qty_net = adjust_qty(bal, LIMITS[sym]["qty_step"])
            atr     = AverageTrueRange(df["h"],df["l"],df["c"],14).average_true_range().iloc[-1]
            tp      = price + TRAIL_MULTIPLIER * atr
            STATE[sym]["positions"].append({
                "buy_price": price,
                "qty_net":   qty_net,
                "qty_gross": qty_net / (1.0 - TAKER_FEE),
                "tp":        tp
            })
            log(f"[{sym}] Recovered pos qty={qty_net}, price={price:.6f}, tp={tp:.6f}")
    save_state()

def send_daily_report():
    report = f"📊 Daily Report {datetime.datetime.now().date()}\n\n"
    report += "Symbol | Trades | PnL | Max Drawdown | Current Pos\n"
    report += "-------|--------|-----|---------------|-------------\n"
    for sym in SYMBOLS:
        s = STATE[sym]
        cur_pos = sum(p["qty_net"] for p in s["positions"])
        dd = s.get("max_drawdown",0.0)
        report += f"{sym:<7}|{s['count']:>6}  |{s['pnl']:>6.2f}|{dd*100:>9.2f}%   |{cur_pos:.6f}\n"
    send_tg(report)

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt  = get_balance()
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue
            sig, atr, info_ind = signal(df)
            price  = float(df["c"].iloc[-1])
            state  = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value    = coin_bal * price

            log(f"[{sym}] sig={sig}, price={price:.6f}, value={value:.2f}, pos={len(state['positions'])}, {info_ind}")

            # DD обновление
            if state["positions"]:
                total_q = sum(p["qty_net"] for p in state["positions"])
                if total_q > 0:
                    avg_entry = sum(p["buy_price"]*p["qty_net"] for p in state["positions"]) / total_q
                    curr_dd   = (avg_entry - price)/avg_entry
                    if curr_dd > state["max_drawdown"]:
                        state["max_drawdown"] = curr_dd

            # SELL / trail
            new_positions = []
            for pos in state["positions"]:
                b  = pos["buy_price"]
                qn = adjust_qty(pos["qty_net"], limits["qty_step"])
                tp = pos["tp"]

                # реальная доступная для продажи
                qn = cap_sell_qty_to_balance(sym, qn)
                if qn < limits["min_qty"] or qn * price < limits["min_amt"]:
                    # мелочь — просто тянем дальше
                    pos["qty_net"] = qn
                    new_positions.append(pos)
                    continue

                pnl_net = pnl_net_after_fees(b, price, qn)
                min_req = max(MIN_NET_USD, price * qn * MIN_PROFIT_PCT)

                # stop-loss — по твоей логике продаём только если уже не хуже минимума
                if price <= b*(1.0 - STOP_LOSS_PCT) and pnl_net >= MIN_NET_USD:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qn))
                    log_trade(sym,"STOP LOSS SELL", price, qn, pnl_net, "stop-loss")
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                    continue

                # take-profit c проверкой net-прибыли
                if price >= tp and pnl_net >= min_req:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(qn))
                    log_trade(sym,"TP SELL", price, qn, pnl_net, "take-profit")
                    state["pnl"] += pnl_net
                    state["last_sell_price"] = price
                    state["avg_count"] = 0
                else:
                    # подтягиваем TP
                    new_tp = max(tp, price + TRAIL_MULTIPLIER * atr)
                    pos["tp"] = new_tp
                    new_positions.append(pos)
            state["positions"] = new_positions

            # BUY / averaging
            if sig == "buy":
                # усреднение
                if state["positions"] and state["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty_net"] for p in state["positions"])
                    avg_price = sum(p["qty_net"]*p["buy_price"] for p in state["positions"])/total_q
                    drawdown = (price - avg_price)/avg_price
                    if drawdown < 0 and abs(drawdown) <= MAX_DRAWDOWN and value < per_sym:
                        qty_gross = get_qty(sym, price, per_sym - value)
                        # проверка доступности USDT с комиссией
                        if qty_gross > 0 and qty_gross*price*(1.0 + TAKER_FEE) <= usdt + 1e-9:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market",
                                                qty=str(qty_gross))
                            qty_net = qty_gross * (1.0 - TAKER_FEE)
                            tp = price + TRAIL_MULTIPLIER*atr
                            STATE[sym]["positions"].append({"buy_price":price, "qty_net":qty_net,
                                                            "qty_gross":qty_gross, "tp":tp})
                            state["count"] += 1; state["avg_count"] += 1
                            log_trade(sym, "BUY (avg)", price, qty_net, 0.0, f"drawdown={drawdown:.4f}")
                # первичный вход
                elif not state["positions"] and value < per_sym:
                    # анти‑ребай вблизи последней продажи (как было)
                    if abs(price - state["last_sell_price"]) / price < 0.001:
                        pass
                    else:
                        qty_gross = get_qty(sym, price, per_sym)
                        if qty_gross > 0 and qty_gross*price*(1.0 + TAKER_FEE) <= usdt + 1e-9:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market",
                                                qty=str(qty_gross))
                            qty_net = qty_gross * (1.0 - TAKER_FEE)
                            tp = price + TRAIL_MULTIPLIER*atr
                            STATE[sym]["positions"].append({"buy_price":price, "qty_net":qty_net,
                                                            "qty_gross":qty_gross, "tp":tp})
                            state["count"] += 1
                            log_trade(sym, "BUY", price, qty_net, 0.0, info_ind)

        except Exception as e:
            tb = traceback.format_exc(limit=1)
            log(f"[{sym}] Error: {e}\n{tb}", True)

    cycle_count += 1
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# --------------- MAIN -------------------------
if __name__ == "__main__":
    log("🚀 Bot started (v3.2)", True)
    init_state()
    ensure_state_consistency()
    save_state()
    load_symbol_limits()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"Global error: {e}", True)
        time.sleep(60)
