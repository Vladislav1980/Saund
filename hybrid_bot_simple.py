# -*- coding: utf-8 -*-
"""
Bybit Spot Bot — v3 + NetPnL‑трейлинг + агрегация + ограниченное усреднение по цене
Функции:
- Комплексный трейлинг NetPnL
- Не более 2 усреднений
- Усреднение при снижении ≥1.5% от средней
- Единая агрегированная позиция и PnL
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
MAX_AVERAGES = 2  # максимум усреднений
MIN_AVG_DIFF_PCT = 0.015  # усреднение при падении от средневзвешенной ≥1.5%
STOP_LOSS_PCT = 0.03

MIN_PROFIT_PCT = 0.005
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

def _state_key(): return "bybit_spot_state_v3"
def _save_state():
    s = json.dumps(STATE, ensure_ascii=False)
    try:
        if rds:
            rds.set(_state_key(), s)
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
    logging.info(f"🚀 Bot starting. State: {src}")
    tg_event(f"🚀 Bot starting. State: {src}")
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "position_entries": [],  # list of {"qty":float,"buy_price":float,"buy_qty_gross":float}
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0
        })
    _save_state()

def _safe_call(func, *args, **kwargs):
    delay = REQUEST_BACKOFF
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if any(code in msg for code in ["rate", "403", "10006"]):
                logging.info(f"Rate/backoff {delay:.1f}s: {msg}")
                time.sleep(delay)
                delay = min(REQUEST_BACKOFF_MAX, delay * 1.7)
                continue
            if "x-bapi-limit-reset-timestamp" in msg:
                logging.info("Bybit header anomaly — wait 1s")
                time.sleep(1.0)
                continue
            raise

def get_wallet(force=False):
    if not force and _wallet_cache["coins"] and time.time() - _wallet_cache["ts"] < WALLET_CACHE_TTL:
        return _wallet_cache["coins"]
    resp = _safe_call(session.get_wallet_balance, accountType="UNIFIED")
    coins = resp["result"]["list"][0]["coin"]
    _wallet_cache.update(ts=time.time(), coins=coins)
    return coins

def usdt_balance(coins): return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))
def coin_balance(coins, sym):
    base = sym.replace("USDT", "")
    return float(next((c["walletBalance"] for c in coins if c["coin"] == base), 0.0))

def load_symbol_limits():
    data = session.get_instruments_info(category="spot")["result"]["list"]
    for item in data:
        if item["symbol"] in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[item["symbol"]] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0))
            }
    logging.info(f"Loaded limits: {LIMITS}")

def round_step(q, step):
    try:
        exp = int(f"{float(step):e}".split("e")[-1])
        return math.floor(q * 10**abs(exp)) / 10**abs(exp)
    except Exception:
        return q

def get_kline(sym):
    r = _safe_call(session.get_kline, category="spot", symbol=sym, interval=INTERVAL, limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0.0, ""
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 9).rsi()
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
    macd = MACD(close=df["c"])
    df["macd"], df["sig"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    info = (f"EMA9={last['ema9']:.5f},EMA21={last['ema21']:.5f},"
            f"RSI={last['rsi']:.2f},MACD={last['macd']:.5f},SIG={last['sig']:.5f}")
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 and last["macd"] > last["sig"]:
        return "buy", float(atr), info
    if last["ema9"] < last["ema21"] and last["rsi"] < 50 and last["macd"] < last["sig"]:
        return "sell", float(atr), info
    return "none", float(atr), info

def volume_ok(df):
    if not USE_VOLUME_FILTER:
        return True
    if len(df) < max(VOL_MA_WINDOW, 20):
        return True
    vol_ma = df["vol"].rolling(VOL_MA_WINDOW).mean().iloc[-2]
    last_vol = df["vol"].iloc[-1]
    notional = last_vol * df["c"].iloc[-1]
    if pd.isna(vol_ma):
        return True
    if last_vol < VOL_FACTOR_MIN * vol_ma:
        logging.info(f"⏸ Volume guard: last_vol<{VOL_FACTOR_MIN:.2f}*MA")
        return False
    if notional < MIN_CANDLE_NOTIONAL:
        logging.info(f"⏸ Volume guard: notional {notional:.2f} < ${MIN_CANDLE_NOTIONAL:.2f}")
        return False
    return True

def orderbook_ok(sym, side, qty_base, ref_price):
    if not USE_ORDERBOOK_GUARD:
        return True, "ob=off"
    try:
        ob = _safe_call(session.get_orderbook, category="spot", symbol=sym, limit=OB_LIMIT_DEPTH)["result"]
        best_ask = float(ob["a"][0][0]); best_bid = float(ob["b"][0][0])
        spread = (best_ask - best_bid) / max(best_bid, 1e-12)
        if spread > MAX_SPREAD_BP/10000:
            return False, f"ob=spread {spread*100:.2f}%>{MAX_SPREAD_BP/100:.2f}%"
        if side.lower() == "buy":
            need = qty_base; cost = 0.0
            for px, q in ob["a"]:
                px, q = float(px), float(q)
                take = min(need, q); cost += take * px; need -= take
                if need <= 1e-15:
                    break
            if need > 0:
                return False, "ob=depth shallow"
            vwap = cost / qty_base
            impact = (vwap - ref_price) / max(ref_price, 1e-12)
            if impact > MAX_IMPACT_BP/10000:
                return False, f"ob=impact {impact*100:.2f}%>{MAX_IMPACT_BP/100:.2f}%"
            return True, f"ob=ok(spread={spread*100:.2f}%,impact={impact*100:.2f}%)"
        else:
            return True, f"ob=ok(spread={spread*100:.2f}%)"
    except Exception as e:
        return True, f"ob=err({e})"

def budget_qty(sym, price, avail_usdt):
    if sym not in LIMITS:
        return 0.0
    lm = LIMITS[sym]
    budget = min(avail_usdt, max_trade_for(sym))
    if budget <= 0:
        return 0.0
    q = round_step(budget / price, lm["qty_step"])
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return 0.0
    return q

def can_place_buy(sym, q, price, usdt_free):
    if q <= 0:
        return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return False
    needed = q * price * (1 + TAKER_FEE + SLIP_BUFFER)
    return needed <= max(0.0, usdt_free - RESERVE_BALANCE + 1e-9)

def can_place_sell(sym, q, price, coin_bal_now):
    if q <= 0:
        return False
    lm = LIMITS[sym]
    if q < lm["min_qty"] or q * price < lm["min_amt"]:
        return False
    return q <= coin_bal_now + 1e-12

def aggregate_position(sym):
    """Возвращает (total_qty, avg_price) агрегированной позиции."""
    entries = STATE[sym]["position_entries"]
    if not entries:
        return 0.0, 0.0
    total_qty = sum(e["qty"] for e in entries)
    avg_price = sum(e["qty"] * e["buy_price"] for e in entries) / total_qty
    return total_qty, avg_price

def net_pnl(sym, current_price):
    total_qty, avg_price = aggregate_position(sym)
    if total_qty <= 0:
        return 0.0
    gross_qty = sum(e["buy_qty_gross"] for e in STATE[sym]["position_entries"])
    cost = avg_price * gross_qty
    proceeds = current_price * total_qty * (1 - TAKER_FEE)
    return proceeds - cost

def create_new_entry(sym, buy_price, qty_gross):
    STATE[sym]["position_entries"].append({
        "qty": qty_gross * (1 - TAKER_FEE),
        "buy_price": buy_price,
        "buy_qty_gross": qty_gross
    })
    _save_state()

def daily_report():
    try:
        coins = get_wallet(True)
        by = {c["coin"]: float(c["walletBalance"]) for c in coins}
        lines = [f"📊 Daily Report {datetime.date.today()}", f"USDT: {by.get('USDT',0.0):.2f}"]
        for sym in SYMBOLS:
            base = sym.replace("USDT", "")
            bal = by.get(base, 0.0)
            price = float(get_kline(sym)["c"].iloc[-1])
            val = price * bal
            s = STATE[sym]
            total_qty, _ = aggregate_position(sym)
            lines.append(
                f"{sym}: balance={bal:.6f} (~{val:.2f}), trades={s['count']}, pnl={s['pnl']:.2f}, "
                f"maxDD={s['max_drawdown']*100:.2f}%, curPosQty={total_qty:.6f}"
            )
        tg_event("\n".join(lines))
    except Exception as e:
        logging.info(f"daily_report error: {e}")

def restore_positions():
    restored = []
    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue
            price = df["c"].iloc[-1]
            coins = get_wallet(True)
            bal = coin_balance(coins, sym)
            lm = LIMITS.get(sym, {})
            if price and bal * price >= lm.get("min_amt", 0.0) and bal >= lm.get("min_qty", 0.0):
                qty_net = round_step(bal, lm["qty_step"])
                STATE[sym]["position_entries"] = [{
                    "qty": qty_net,
                    "buy_price": price,
                    "buy_qty_gross": qty_net / (1 - TAKER_FEE)
                }]
                restored.append(f"{sym}: qty={qty_net:.8f} @ {price:.6f}")
        except Exception as e:
            logging.info(f"[{sym}] restore error: {e}")
    _save_state()
    if restored:
        tg_event("♻️ Restored positions:\n" + "\n".join(restored))
    else:
        tg_event("ℹ️ No positions found to restore.")

def cap_btc_sell_qty(sym, q, price, coin_bal_now):
    if sym != BTC_SYMBOL:
        return q
    cap_fraction = coin_bal_now * BTC_MAX_SELL_FRACTION_TRADE
    keep_floor = 0.0
    if BTC_MIN_KEEP_USD > 0:
        keep_floor = max(0.0, (coin_bal_now * price - BTC_MIN_KEEP_USD) / max(price, 1e-12))
    allowed = max(0.0, min(q, cap_fraction, keep_floor if keep_floor > 0 else q))
    if allowed <= 0:
        allowed = min(q, cap_fraction)
    return max(0.0, allowed)

def try_liquidity_recovery(coins, usdt):
    if not LIQUIDITY_RECOVERY:
        return
    avail = max(0.0, usdt - RESERVE_BALANCE)
    if avail >= LIQ_RECOVERY_USDT_MIN:
        return
    target = LIQ_RECOVERY_USDT_TARGET - avail
    if target <= 0:
        return
    logging.info(f"💧 LiquidityRecovery: need +{target:.2f} USDT (avail={avail:.2f})")
    candidates = []
    for sym in SYMBOLS:
        entries = STATE[sym]["position_entries"]
        if not entries:
            continue
        df = get_kline(sym)
        price = df["c"].iloc[-1]
        lm = LIMITS[sym]
        coin_bal = coin_balance(coins, sym)
        pnl = net_pnl(sym, price)
        total_qty, _ = aggregate_position(sym)
        if pnl > MIN_NET_ABS_USD and can_place_sell(sym, total_qty, price, coin_bal):
            sell_try = total_qty * 0.2
            if sym == BTC_SYMBOL:
                sell_try = cap_btc_sell_qty(sym, sell_try, price, coin_bal)
            if sell_try <= 0:
                continue
            est_usdt = price * sell_try * (1 - TAKER_FEE)
            candidates.append((sym, price, sell_try, est_usdt, pnl, coin_bal, lm))
    candidates.sort(key=lambda x: (x[4]/max(x[3],1e-9)), reverse=True)
    for sym, price, sell_q, est, pnl, coin_bal, lm in candidates:
        if target <= 0:
            break
        sell_q = min(sell_q, round_step(target / max(price*(1-TAKER_FEE), 1e-9), lm["qty_step"]))
        if sell_q < lm["min_qty"] or sell_q * price < lm["min_amt"]:
            continue
        if not can_place_sell(sym, sell_q, price, coin_bal):
            continue
        try:
            _safe_call(session.place_order, category="spot", symbol=sym,
                       side="Sell", orderType="Market", qty=str(sell_q))
            tg_event(f"🟠 RECOVERY SELL {sym} @ {price:.6f}, qty={sell_q:.8f}")
            entries = STATE[sym]["position_entries"]
            remaining = sell_q
            new_entries = []
            for e in entries:
                if remaining <= 0:
                    new_entries.append(e)
                    continue
                take = min(e["qty"], remaining)
                ratio = take / max(e["qty"], 1e-12)
                e["qty"] -= take
                e["buy_qty_gross"] *= (1 - ratio)
                remaining -= take
                if e["qty"] > 0:
                    new_entries.append(e)
            STATE[sym]["position_entries"] = new_entries
            STATE[sym]["pnl"] += pnl
            _save_state()
            target -= price * sell_q * (1 - TAKER_FEE)
            coins = get_wallet(True)
        except Exception as e:
            logging.info(f"[{sym}] recovery sell error: {e}")

def trade_cycle():
    global LAST_REPORT_DATE, _last_err_ts
    try:
        coins = get_wallet(True)
        usdt = usdt_balance(coins)
    except Exception as e:
        now = time.time()
        if now - _last_err_ts > TG_ERR_COOLDOWN:
            tg_event(f"Balance error: {e}")
            _last_err_ts = now
        return

    if LIQUIDITY_RECOVERY:
        try_liquidity_recovery(coins, usdt)
        coins = get_wallet(True)
        usdt = usdt_balance(coins)

    avail = max(0.0, usdt - RESERVE_BALANCE)
    logging.info(f"💰 USDT={usdt:.2f} | Available={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                logging.info(f"[{sym}] no candles — skip")
                continue

            sig, atr, info = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            lm = LIMITS[sym]
            coin_bal = coin_balance(coins, sym)
            total_qty, avg_price = aggregate_position(sym)
            logging.info(f"[{sym}] sig={sig} | {info} | price={price:.6f}, pos_qty={total_qty:.6f}, avg_price={avg_price:.6f}")

            # Max drawdown tracking
            if total_qty > 0:
                dd_now = max(0.0, (avg_price - price) / max(avg_price, 1e-12))
                state["max_drawdown"] = max(state["max_drawdown"], dd_now)

            pnl = net_pnl(sym, price)

            # SELL: SL, Trail, Profit
            if total_qty > 0:
                sell_q = cap_btc_sell_qty(sym, total_qty, price, coin_bal)
                need = max(MIN_NET_ABS_USD, MIN_NET_PROFIT, price * sell_q * MIN_PROFIT_PCT)
                ok_sell = pnl >= need

                if price <= avg_price * (1 - STOP_LOSS_PCT) and ok_sell:
                    _safe_call(session.place_order, category="spot", symbol=sym,
                               side="Sell", orderType="Market", qty=str(sell_q))
                    msg = f"🟠 SL SELL {sym} @ {price:.6f}, qty={sell_q:.8f}, pnl={pnl:.2f}"
                    logging.info(msg); tg_event(msg)
                    state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                    state["position_entries"] = []
                else:
                    max_pnl = max(e.get("max_pnl", 0.0) for e in state["position_entries"])
                    if max_pnl < pnl:
                        for e in state["position_entries"]:
                            e["max_pnl"] = pnl
                    if max_pnl >= TRAIL_PNL_TRIGGER and (max_pnl - pnl) >= TRAIL_PNL_GAP:
                        _safe_call(session.place_order, category="spot", symbol=sym,
                                   side="Sell", orderType="Market", qty=str(sell_q))
                        msg = f"✅ TRAIL SELL {sym} @ {price:.6f}, qty={sell_q:.8f}, pnl={pnl:.2f}, peak={max_pnl:.2f}"
                        logging.info(msg); tg_event(msg)
                        state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                        state["position_entries"] = []
                    elif ok_sell:
                        _safe_call(session.place_order, category="spot", symbol=sym,
                                   side="Sell", orderType="Market", qty=str(sell_q))
                        msg = f"✅ PROFIT SELL {sym} @ {price:.6f}, qty={sell_q:.8f}, pnl={pnl:.2f}"
                        logging.info(msg); tg_event(msg)
                        state["pnl"] += pnl; state["last_sell_price"] = price; state["avg_count"] = 0
                        state["position_entries"] = []
                    else:
                        logging.info(f"[{sym}] no sell: pnl {pnl:.2f} < need {need:.2f}")

            # BUY / AVERAGE
            if sig == "buy" and volume_ok(df):
                if total_qty > 0:
                    diff_pct = (avg_price - price) / avg_price
                    if state["avg_count"] < MAX_AVERAGES and diff_pct >= MIN_AVG_DIFF_PCT:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        if q_gross > 0 and ob_ok and can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", timeInForce="IOC",
                                       marketUnit="baseCoin", qty=str(q_gross))
                            create_new_entry(sym, price, q_gross)
                            state["count"] += 1; state["avg_count"] += 1
                            qty_net = q_gross * (1 - TAKER_FEE)
                            msg = f"🟢 BUY(avg) {sym} @ {price:.6f}, qty_net={qty_net:.8f}, diff_pct={diff_pct:.4f}, {ob_info}"
                            logging.info(msg); tg_event(msg)
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] skip avg buy: budget/limits/OB/balance")
                    else:
                        logging.info(f"[{sym}] skip avg: count={state['avg_count']}, diff_pct={diff_pct:.4f}")
                else:
                    if state["last_sell_price"] and abs(price - state["last_sell_price"]) / price < 0.003:
                        logging.info(f"[{sym}] skip buy: too close to last sell price")
                    else:
                        q_gross = budget_qty(sym, price, avail)
                        ob_ok, ob_info = orderbook_ok(sym, "buy", q_gross, price)
                        if q_gross > 0 and ob_ok and can_place_buy(sym, q_gross, price, usdt):
                            _safe_call(session.place_order, category="spot", symbol=sym,
                                       side="Buy", orderType="Market", timeInForce="IOC",
                                       marketUnit="baseCoin", qty=str(q_gross))
                            create_new_entry(sym, price, q_gross)
                            state["count"] += 1
                            qty_net = q_gross * (1 - TAKER_FEE)
                            msg = f"🟢 BUY {sym} @ {price:.6f}, qty_net={qty_net:.8f}, {ob_info}"
                            logging.info(msg); tg_event(msg)
                            coins = get_wallet(True); usdt = usdt_balance(coins); avail = max(0.0, usdt - RESERVE_BALANCE)
                        else:
                            logging.info(f"[{sym}] skip buy: budget/limits/OB/balance")
            else:
                if total_qty == 0:
                    logging.info(f"[{sym}] no buy: sig={sig}")

        except Exception as e:
            tb = traceback.format_exc(limit=2)
            logging.info(f"[{sym}] cycle error: {e}\n{tb}")
            now = time.time()
            if now - _last_err_ts > TG_ERR_COOLDOWN:
                tg_event(f"[{sym}] cycle error: {e}")
                _last_err_ts = now

    _save_state()

    now = datetime.datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE and LAST_REPORT_DATE != now.date():
        daily_report()
        globals()['LAST_REPORT_DATE'] = now.date()

if __name__ == "__main__":
    logging.info("🚀 Bot starting with NetPnL‑trailing + aggregated position + controlled averaging")
    tg_event("🚀 Bot starting with NetPnL‑trailing + aggregated position + controlled averaging")
    init_state()
    load_symbol_limits()
    restore_positions()
    tg_event(
        f"⚙️ Params: TAKER={TAKER_FEE}, BASE_MAX_TRADE={BASE_MAX_TRADE_USDT}, OVR={MAX_TRADE_OVERRIDES}, "
        f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.1f}%, DD={MAX_DRAWDOWN*100:.0f}%, "
        f"ProfitOnly={PROFIT_ONLY}, VolFilter={'ON' if USE_VOLUME_FILTER else 'OFF'}, "
        f"OBGuard={'ON' if USE_ORDERBOOK_GUARD else 'OFF'} (spread≤{MAX_SPREAD_BP/100:.2f}%, impact≤{MAX_IMPACT_BP/100:.2f}%), "
        f"LiqRecovery={'ON' if LIQUIDITY_RECOVERY else 'OFF'} (min={LIQ_RECOVERY_USDT_MIN}, target={LIQ_RECOVERY_USDT_TARGET}), "
        f"NetPnL-Trailing={'ON' if TRAIL_PNL_TRIGGER else 'OFF'} (trigger=${TRAIL_PNL_TRIGGER}, gap=${TRAIL_PNL_GAP}), "
        f"MaxAverages={MAX_AVERAGES}, MinAvgDiffPct={MIN_AVG_DIFF_PCT:.3f}"
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
