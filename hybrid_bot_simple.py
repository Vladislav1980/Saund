# bot_v3_2.py
import os, time, math, logging, datetime, random, json, traceback
from typing import Dict, List
import requests
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# --- optional Redis state ---
REDIS = None
try:
    import redis  # type: ignore
except Exception:
    redis = None

load_dotenv()

API_KEY     = os.getenv("BYBIT_API_KEY")
API_SECRET  = os.getenv("BYBIT_API_SECRET")
TG_TOKEN    = os.getenv("TG_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
REDIS_URL   = os.getenv("REDIS_URL")  # e.g. redis://:pass@host:port/0

# --- constants / params ---
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]  # как просил: ton, doge, xrp
ACCOUNT_TYPE = "UNIFIED"
CATEGORY = "spot"

# комиссии: по твоему скрину
TAKER_FEE = 0.0018  # market
MAKER_FEE = 0.0010  # limit (не используем, но оставил для ясности)

# деньги
RESERVE_BALANCE = 1.0
BUDGET_MIN = 150.0
BUDGET_MAX = 230.0
MAX_PER_TRADE = BUDGET_MAX  # хард‑ограничение

# риск/менеджмент
TRAIL_MULTIPLIER = 1.5
STOP_LOSS_PCT    = 0.03
MAX_DRAWDOWN_AVG = 0.15
MAX_AVERAGES     = 3
MIN_NET_PROFIT_USD = 1.0     # минимальная чистая прибыль после всех комиссий
MIN_PROFIT_PCT      = 0.004  # минимальный % при фиксации (страхует от шума)

STATE_FILE = "state.json"

# индикаторы
RSI_BUY  = 52
RSI_SELL = 48

# отчёт раз в сутки
DAILY_REPORT_HOUR = 22
DAILY_REPORT_MIN  = 30

# логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("bot").info

# --- telegram minimal ---
def tg(msg: str):
    if not TG_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception:
        logging.exception("Telegram send failed")

# --- session ---
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# --- state & redis helpers ---
STATE: Dict = {}
LIMITS: Dict[str, Dict[str, float]] = {}
LAST_REPORT_DATE = None

def redis_connect():
    global REDIS
    if REDIS_URL and redis is not None:
        try:
            REDIS = redis.from_url(REDIS_URL, decode_responses=True)  # type: ignore
            REDIS.ping()
            log("Redis connected")
        except Exception:
            REDIS = None
            log("Redis connect failed — fallback to file state")

def state_key():
    return "bybit_bot_state_v3_2"

def save_state():
    data = json.dumps(STATE, ensure_ascii=False)
    if REDIS:
        try:
            REDIS.set(state_key(), data)
            return
        except Exception:
            log("Redis save failed — writing file")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(data)

def load_state():
    global STATE
    if REDIS:
        try:
            data = REDIS.get(state_key())
            if data:
                STATE = json.loads(data)
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

def ensure_state():
    for s in SYMBOLS:
        STATE.setdefault(s, {
            "positions": [],    # list[{"buy_price":float,"qty":float,"tp":float}]
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "max_drawdown": 0.0,
            "last_sell_price": 0.0
        })

# --- bybit helpers ---
def load_symbol_limits():
    data = session.get_instruments_info(category=CATEGORY)["result"]["list"]
    for item in data:
        sym = item["symbol"]
        if sym in SYMBOLS:
            f = item.get("lotSizeFilter", {})
            LIMITS[sym] = {
                "min_qty": float(f.get("minOrderQty", 0.0)),
                "qty_step": float(f.get("qtyStep", 1.0)),
                "min_amt": float(item.get("minOrderAmt", 10.0)),
            }
    log(f"Loaded limits: {LIMITS}")

def adjust_qty(qty: float, step: float) -> float:
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except Exception:
        return qty

def get_kline(sym: str) -> pd.DataFrame:
    r = session.get_kline(category=CATEGORY, symbol=sym, interval="1", limit=120)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_wallet() -> Dict[str, float]:
    try:
        coins = session.get_wallet_balance(accountType=ACCOUNT_TYPE)["result"]["list"][0]["coin"]
        d = {c["coin"]: float(c["walletBalance"]) for c in coins}
        d["USDT"] = d.get("USDT", 0.0)
        return d
    except Exception as e:
        msg = f"Ошибка получения баланса USDT: {e}"
        log(msg)
        tg(msg)
        raise

def get_coin_balance(sym: str) -> float:
    coin = sym.replace("USDT", "")
    d = get_wallet()
    return d.get(coin, 0.0)

# --- signal logic (2 of 3) ---
def make_signal(df: pd.DataFrame):
    if df.empty or len(df) < 50:
        return "none", 0.0, 0.0, {}
    ema9  = EMAIndicator(df["c"], 9).ema_indicator()
    ema21 = EMAIndicator(df["c"], 21).ema_indicator()
    rsi   = RSIIndicator(df["c"], 14).rsi()
    macd_calc = MACD(close=df["c"])
    macd = macd_calc.macd()
    macd_sig = macd_calc.macd_signal()
    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()

    last = len(df) - 1
    ema_trend = "buy" if ema9.iloc[last] > ema21.iloc[last] else "sell"
    rsi_sig   = "buy" if rsi.iloc[last] >= RSI_BUY else ("sell" if rsi.iloc[last] <= RSI_SELL else "none")
    macd_sig3 = "buy" if macd.iloc[last] > macd_sig.iloc[last] else "sell"

    votes_buy  = int(ema_trend=="buy") + int(rsi_sig=="buy") + int(macd_sig3=="buy")
    votes_sell = int(ema_trend=="sell") + int(rsi_sig=="sell") + int(macd_sig3=="sell")
    conf = max(votes_buy, votes_sell) / 3.0  # 0.0..1.0

    if votes_buy >= 2:
        side = "buy"
    elif votes_sell >= 2:
        side = "sell"
    else:
        side = "none"

    info = {
        "EMA9": float(ema9.iloc[last]), "EMA21": float(ema21.iloc[last]),
        "RSI": float(rsi.iloc[last]), "MACD": float(macd.iloc[last]),
        "SIG": float(macd_sig.iloc[last])
    }
    return side, float(atr.iloc[last]), conf, info

# --- qty & budget ---
def choose_budget(avail_per_symbol: float) -> float:
    # плавающий бюджет, но не больше доступных средств и MAX_PER_TRADE
    b = random.uniform(BUDGET_MIN, BUDGET_MAX)
    b = min(b, avail_per_symbol, MAX_PER_TRADE)
    return max(0.0, b)

def qty_from_budget(sym: str, price: float, budget: float) -> float:
    lim = LIMITS[sym]
    q = adjust_qty(budget / price, lim["qty_step"])
    if q < lim["min_qty"] or q * price < lim["min_amt"]:
        return 0.0
    return q

# --- fees & pnl ---
def buy_fee_usd(price: float, qty: float) -> float:
    return price * qty * TAKER_FEE

def sell_fee_usd(price: float, qty: float) -> float:
    return price * qty * TAKER_FEE

def net_pnl_usd(buy_price: float, sell_price: float, qty: float) -> float:
    gross = (sell_price - buy_price) * qty
    fees = buy_fee_usd(buy_price, qty) + sell_fee_usd(sell_price, qty)
    return gross - fees

# --- trading core ---
def place_order_safe(**kwargs):
    try:
        return session.place_order(**kwargs)
    except Exception as e:
        emsg = str(e)
        # Типичные ошибки: 403 ip / rate-limit; 170131 insufficient balance
        tg(f"Ошибка цикла: {emsg}")
        if "403" in emsg or "ip" in emsg.lower():
            time.sleep(60)
        return None

def trade_cycle():
    global LAST_REPORT_DATE
    wallet = get_wallet()
    usdt = wallet.get("USDT", 0.0)
    avail = max(0.0, usdt - RESERVE_BALANCE)
    per_sym_avail = avail / max(1, len(SYMBOLS))

    log(f"💰 Баланс USDT={usdt:.2f} | Доступно={avail:.2f}")

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: 
                continue
            price = float(df["c"].iloc[-1])
            side, atr, conf, info = make_signal(df)
            s = STATE[sym]
            lim = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            cur_val = coin_bal * price

            # log‑деталка (в телегу не уходит)
            log(f"[{sym}] 🔎 Сигнал={side.upper()} (conf={conf:.2f}), price={price:.6f}, "
                f"balance={coin_bal:.8f} (~{cur_val:.2f} USDT) | "
                f"EMA9={info['EMA9']:.4f}, EMA21={info['EMA21']:.4f}, RSI={info['RSI']:.2f}, "
                f"MACD={info['MACD']:.4f}, SIG={info['SIG']:.4f}")

            # актуализируем max drawdown по открытым позициям
            if s["positions"]:
                total_q = sum(p["qty"] for p in s["positions"])
                avg_price = sum(p["buy_price"]*p["qty"] for p in s["positions"])/total_q
                curr_dd = (avg_price - price)/avg_price
                if curr_dd > s["max_drawdown"]:
                    s["max_drawdown"] = curr_dd

            # --- ПРОДАЖИ / ТРАЛ / SL ---
            new_positions = []
            for pos in s["positions"]:
                b = pos["buy_price"]
                q = adjust_qty(pos["qty"], lim["qty_step"])
                tp = pos["tp"]

                # stop-loss: жёстко, без требования прибыли
                if price <= b * (1.0 - STOP_LOSS_PCT):
                    resp = place_order_safe(category=CATEGORY, symbol=sym, side="Sell",
                                            orderType="Market", qty=str(q))
                    if resp:
                        pnl = net_pnl_usd(b, price, q)
                        s["pnl"] += pnl
                        s["last_sell_price"] = price
                        tg(f"🟥 {sym} SELL(SL) @ {price:.6f}, qty={q:.8f}, pnl={pnl:.2f}")
                        log(f"[{sym}] STOP‑LOSS SELL @ {price:.6f}, qty={q}, netPnL={pnl:.2f}")
                    continue

                # условие фиксации прибыли: >= tp и чистая прибыль >= $1 и >= MIN_PROFIT_PCT
                net = net_pnl_usd(b, price, q)
                pct = (price - b) / b
                if price >= tp and net >= MIN_NET_PROFIT_USD and pct >= MIN_PROFIT_PCT:
                    resp = place_order_safe(category=CATEGORY, symbol=sym, side="Sell",
                                            orderType="Market", qty=str(q))
                    if resp:
                        s["pnl"] += net
                        s["last_sell_price"] = price
                        tg(f"✅ {sym} SELL @ {price:.6f}, qty={q:.8f}, pnl={net:.2f}")
                        log(f"[{sym}] TP SELL @ {price:.6f}, qty={q}, netPnL={net:.2f}, "
                            f"tp={tp:.6f}, pct={pct:.4%}")
                else:
                    # тянем трелл
                    new_tp = price + TRAIL_MULTIPLIER * atr
                    pos["tp"] = max(tp, new_tp)
                    new_positions.append(pos)
                    log(f"[{sym}] 📈 TP trail: {tp:.6f} → {pos['tp']:.6f} | "
                        f"не продаём: цена {price:.6f} < TP {pos['tp']:.6f} или net {net:.2f} < {MIN_NET_PROFIT_USD}")
            s["positions"] = new_positions

            # --- ПОКУПКИ / УСРЕДНЕНИЕ ---
            if side == "buy":
                # если уже есть позиция — смотрим окно усреднения
                if s["positions"] and s["avg_count"] < MAX_AVERAGES:
                    total_q = sum(p["qty"] for p in s["positions"])
                    avg_price = sum(p["qty"]*p["buy_price"] for p in s["positions"])/total_q
                    dd = (price - avg_price) / avg_price  # отрицательное — ниже средней
                    if dd < 0 and abs(dd) <= MAX_DRAWDOWN_AVG:
                        budget = choose_budget(per_sym_avail)
                        qty = qty_from_budget(sym, price, budget)
                        if qty and qty*price <= avail and budget >= BUDGET_MIN:
                            resp = place_order_safe(category=CATEGORY, symbol=sym, side="Buy",
                                                    orderType="Market", qty=str(qty))
                            if resp:
                                tp = price + TRAIL_MULTIPLIER * atr
                                s["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                                s["count"] += 1
                                s["avg_count"] += 1
                                tg(f"🟢 {sym} BUY(avg) @ {price:.6f}, qty={qty:.8f}")
                                log(f"[{sym}] BUY(avg) @ {price:.6f}, qty={qty}, dd={dd:.4f}, budget≈{budget:.2f}")
                    else:
                        log(f"[{sym}] ❌ Не усредняем: drawdown {dd:.4f} вне (-{MAX_DRAWDOWN_AVG})")
                # первичный вход
                elif not s["positions"]:
                    # анти‑повтор: не покупаем по почти той же цене, что и предыдущая продажа
                    if s["last_sell_price"] and abs(price - s["last_sell_price"]) / price < 0.001:
                        log(f"[{sym}] ❌ Пропуск входа: близко к последней продаже")
                    else:
                        budget = choose_budget(per_sym_avail)
                        qty = qty_from_budget(sym, price, budget)
                        if qty and qty * price <= avail and budget >= BUDGET_MIN:
                            resp = place_order_safe(category=CATEGORY, symbol=sym, side="Buy",
                                                    orderType="Market", qty=str(qty))
                            if resp:
                                tp = price + TRAIL_MULTIPLIER * atr
                                s["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                                s["count"] += 1
                                tg(f"🟢 {sym} BUY @ {price:.6f}, qty={qty:.8f}")
                                log(f"[{sym}] BUY @ {price:.6f}, qty={qty}, budget≈{budget:.2f}, conf={conf:.2f}")
                        else:
                            log(f"[{sym}] ❌ Нет покупки: qty={qty}, budget<min или лимиты биржи")
            else:
                log(f"[{sym}] 🔶 Нет покупки: сигнал {side}, confidence={conf:.2f}")

        except Exception as e:
            emsg = f"[{sym}] Ошибка цикла: {e}"
            log(emsg)
            tg(emsg)
            logging.exception(e)

    save_state()
    now = datetime.datetime.now()
    if (now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MIN 
        and LAST_REPORT_DATE != now.date()):
        send_daily_report()
        globals()["LAST_REPORT_DATE"] = now.date()

# --- reporting ---
def send_daily_report():
    wallet = get_wallet()
    usdt = wallet.get("USDT", 0.0)
    lines = [f"📊 Daily Report {datetime.date.today()}",
             f"USDT: {usdt:.2f}"]
    for sym in SYMBOLS:
        s = STATE[sym]
        coin = sym.replace("USDT", "")
        bal_coin = wallet.get(coin, 0.0)
        cur_pos_qty = sum(p["qty"] for p in s["positions"])
        lines.append(
            f"{sym}: balance={bal_coin:.6f}, trades={s['count']}, pnl={s['pnl']:.2f}, "
            f"maxDD={s['max_drawdown']*100:.2f}%, curPosQty={cur_pos_qty:.6f}"
        )
    tg("\n".join(lines))

# --- bootstrap ---
def main():
    log("🚀 Бот запускается...")
    redis_connect()
    src = load_state()
    ensure_state()
    save_state()
    load_symbol_limits()
    tg(f"🚀 Старт бота. Восстановление состояния: {src}")
    log(f"⚙ Параметры: TAKER_FEE={TAKER_FEE:.4f}, BUDGET=[{BUDGET_MIN:.1f};{BUDGET_MAX:.1f}] "
        f"TRAILx={TRAIL_MULTIPLIER}, SL={STOP_LOSS_PCT*100:.2f}%")

    # попытка восстановить позиции из фактического баланса (если локально пусто)
    for sym in SYMBOLS:
        if not STATE[sym]["positions"]:
            try:
                df = get_kline(sym)
                price = float(df["c"].iloc[-1])
                coin_bal = get_coin_balance(sym)
                lim = LIMITS[sym]
                if coin_bal * price >= lim["min_amt"] and coin_bal >= lim["min_qty"]:
                    qty = adjust_qty(coin_bal, lim["qty_step"])
                    atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                    tp = price + TRAIL_MULTIPLIER * float(atr)
                    STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                    log(f"♻️ [{sym}] Восстановлена позиция qty={qty}, price={price:.6f}, tp={tp:.6f}")
            except Exception:
                pass
    save_state()

    while True:
        try:
            trade_cycle()
        except Exception as e:
            tg(f"Global error: {e}")
            logging.exception(e)
        time.sleep(60)

if __name__ == "__main__":
    main()
