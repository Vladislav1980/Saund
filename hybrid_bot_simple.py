import os, time, math, logging, datetime, requests, json
from decimal import Decimal, getcontext
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
import redis

# -------------------- ENV --------------------
load_dotenv()
API_KEY     = os.getenv("BYBIT_API_KEY")
API_SECRET  = os.getenv("BYBIT_API_SECRET")
TG_TOKEN    = os.getenv("TG_TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
REDIS_URL   = os.getenv("REDIS_URL")

# -------------------- CONFIG --------------------
TG_VERBOSE = True

RESERVE_BALANCE = 1.0
MAX_TRADE_USDT  = 105.0
MIN_NET_PROFIT  = 0.7          # в USD, минимальная чистая прибыль на сделку
STOP_LOSS_PCT   = 0.008        # 0.8% от buy_price

# Фактические комиссии (taker). Лучше держать их в .env и/или подтягивать раз в день из /v5/account/fee-rate
TAKER_BUY_FEE  = float(os.getenv("TAKER_BUY_FEE",  "0.0010"))
TAKER_SELL_FEE = float(os.getenv("TAKER_SELL_FEE", "0.0018"))

# небольшая подстраховка на слиппедж в ТР (в процентах цены)
TP_SLIPPAGE_PCT = 0.0005  # 0.05%

SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT", "WIFUSDT"]

LAST_REPORT_DATE = None
cycle_count = 0

getcontext().prec = 28  # для Decimal

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
STATE = {}  # по символу: positions[], pnl, avg_count, last_stop_time, ...

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

SKIP_LOG_TIMESTAMPS = {}

def should_log_skip(sym, key, interval=15):
    now = datetime.datetime.now()
    last = SKIP_LOG_TIMESTAMPS.get((sym, key))
    if last and (now - last).total_seconds() < interval * 60:
        return False
    SKIP_LOG_TIMESTAMPS[(sym, key)] = now
    return True

def log_skip(sym, msg):
    logging.info(f"{sym}: {msg}")

def send_tg(msg):
    if TG_VERBOSE and TG_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg})
        except Exception as e:
            logging.error("Telegram send failed: " + str(e))

def log_msg(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def save_state():
    try:
        redis_client.set("bot_state", json.dumps(STATE))
    except Exception as e:
        log_msg(f"Redis save failed: {e}", True)

def init_state():
    global STATE
    raw = redis_client.get("bot_state")
    STATE = json.loads(raw) if raw else {}
    log_msg("✅ Состояние загружено из Redis" if STATE else "ℹ Начинаем с чистого состояния", True)
    ensure_state_consistency()

def ensure_state_consistency():
    for sym in SYMBOLS:
        STATE.setdefault(sym, {
            "positions": [],
            "pnl": 0.0,
            "count": 0,
            "avg_count": 0,
            "last_sell_price": 0.0,
            "max_drawdown": 0.0,
            "last_stop_time": ""
        })

# -------------------- Instrument limits --------------------
def load_symbol_limits():
    resp = session.get_instruments_info(category="spot")["result"]["list"]
    return {
        item["symbol"]: {
            "min_qty": float(item.get("lotSizeFilter", {}).get("minOrderQty", 0.0)),
            "qty_step": float(item.get("lotSizeFilter", {}).get("qtyStep", 1.0)),
            "min_amt": float(item.get("minOrderAmt", 10.0))
        }
        for item in resp if item["symbol"] in SYMBOLS
    }

LIMITS = load_symbol_limits()

# точное округление вниз к шагу (работает и для “кривых” шагов типа 0.0015)
def adjust_qty(qty, step):
    q = Decimal(str(qty))
    s = Decimal(str(step))
    return float((q // s) * s)

def log_trade(sym, side, price, qty, pnl, info=""):
    msg = f"{side} {sym} @ {price:.6f}, qty={qty}, PnL={pnl:.2f}. {info}"
    log_msg(msg, True)
    with open("trades.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price:.6f},{qty},{pnl:.2f},{info}\n")
    save_state()

# -------------------- Signals --------------------
def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0, ""
    df["ema9"]  = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"]   = RSIIndicator(df["c"], 9).rsi()
    df["atr"]   = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(close=df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    buy_cnt  = sum([last["ema9"] > last["ema21"], last["rsi"] > 50, last["macd"] > last["macd_signal"]])
    sell_cnt = sum([last["ema9"] < last["ema21"], last["rsi"] < 50, last["macd"] < last["macd_signal"]])
    info = (f"EMA9={last['ema9']:.6f},EMA21={last['ema21']:.6f},RSI={last['rsi']:.2f},"
            f"MACD={last['macd']:.6f},SIG={last['macd_signal']:.6f}")
    if buy_cnt >= 2:
        return "buy", last["atr"], info
    if sell_cnt >= 2:
        return "sell", last["atr"], info
    return "none", last["atr"], info

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

# -------------------- Balances (кэш на цикл) --------------------
def get_balances_cache():
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    by = {c["coin"]: float(c["walletBalance"]) for c in coins}
    return float(by.get("USDT", 0.0)), by

def get_coin_balance_from(by, sym):
    co = sym.replace("USDT", "")
    return float(by.get(co, 0.0))

# -------------------- Qty helper --------------------
def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0.0
    alloc = min(usdt, MAX_TRADE_USDT)
    q = adjust_qty(alloc / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0.0
    return q

# -------------------- Helpers --------------------
def hours_since(ts):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
    except:
        return 999.0

def choose_multiplier(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.01:   return 0.7
    elif pct < 0.02: return 1.0
    else:            return 1.5

def dynamic_min_profit(atr, price):
    pct = atr / price if price > 0 else 0
    if pct < 0.004:  return 0.6
    elif pct < 0.008:return 0.8
    else:            return 1.2

# Требуемая цена ТР, чтобы гарантировать прибыль P (USD) с учётом комиссий и небольшого слиппеджа
def tp_from_required_profit(price, qty, required_pnl):
    # tp >= [ P/q + price*(1 + taker_buy) ] / (1 - taker_sell)
    numerator   = (required_pnl / qty) + price * (1.0 + TAKER_BUY_FEE)
    denominator = (1.0 - TAKER_SELL_FEE)
    tp = numerator / denominator
    # добавим минимальный буфер на слиппедж
    tp *= (1.0 + TP_SLIPPAGE_PCT)
    return tp

# -------------------- Init positions (по имеющимся остаткам) --------------------
def init_positions():
    total, msgs = 0.0, []
    usdt, by = get_balances_cache()
    for sym in SYMBOLS:
        bal = get_coin_balance_from(by, sym)
        log_msg(f"DEBUG balance {sym}: {bal:.6f}", True)
        df = get_kline(sym)
        if bal > 0 and not df.empty:
            price = df["c"].iloc[-1]
            if bal * price >= LIMITS[sym]["min_amt"]:
                qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
                atr = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range().iloc[-1]
                mul = choose_multiplier(atr, price)
                atr_tp = price + mul * atr
                req_tp = tp_from_required_profit(price, qty, max(MIN_NET_PROFIT, dynamic_min_profit(atr, price)))
                tp = max(atr_tp, req_tp)
                STATE[sym]["positions"].append({
                    "buy_price": price, "qty": qty, "tp": tp,
                    "timestamp": datetime.datetime.now().isoformat()
                })
                total += qty * price
                msgs.append(f"- {sym}: {qty} @ {price:.6f} (${qty * price:.2f}) TP={tp:.6f}")
    save_state()
    header = "🚀 Бот запущен\n"
    if msgs:
        header += "💰 Активные позиции:\n" + "\n".join(msgs) + f"\n📊 Итого: ${total:.2f}"
    else:
        header += "💰 Активных позиций нет"
    log_msg(header, True)

# -------------------- Trading loop --------------------
def trade():
    global cycle_count, LAST_REPORT_DATE

    usdt, by = get_balances_cache()
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS) if SYMBOLS else 0
    logging.info(f"DEBUG avail={avail:.2f}, per_sym={per_sym:.2f}")

    for sym in SYMBOLS:
        st = STATE[sym]
        st["sell_failed"] = False

        df = get_kline(sym)
        if df.empty:
            continue

        sig, atr, info = signal(df)
        price = df["c"].iloc[-1]
        coin_bal = get_coin_balance_from(by, sym)
        value = coin_bal * price
        logging.info(f"[{sym}] sig={sig}, price={price:.6f}, value={value:.2f}, pos={len(st['positions'])}, {info}")

        # очищаем фантомные позиции (если вдруг)
        if st["positions"] and coin_bal < sum(p["qty"] for p in st["positions"]):
            state_count = len(st["positions"])
            st["positions"] = []
            log_msg(f"{sym}: удалены {state_count} фантомные позиции из-за нулевого остатка", True)

        # ПРОДАЖИ / СТОП
        for pos in list(st["positions"]):
            b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
            cost = b * q
            buy_comm  = cost * TAKER_BUY_FEE
            sell_comm = price * q * TAKER_SELL_FEE
            pnl = (price - b) * q - (buy_comm + sell_comm)

            # Стоп-лосс: только по проценту
            if q > 0 and price <= b * (1 - STOP_LOSS_PCT):
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell",
                                        orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS SELL", price, q, pnl, "stop‑loss")
                    st["pnl"] += pnl
                else:
                    log_msg(f"SKIPPED STOP SELL {sym}: insufficient balance", True)
                    st["sell_failed"] = True
                st["last_stop_time"] = datetime.datetime.now().isoformat()
                st["avg_count"] = 0
                st["positions"] = []
                break

            # Тейк‑профит
            if q > 0 and price >= tp:
                if coin_bal >= q:
                    session.place_order(category="spot", symbol=sym, side="Sell",
                                        orderType="Market", qty=str(q))
                    log_trade(sym, "TP SELL", price, q, pnl, "take‑profit")
                    st["pnl"] += pnl
                st["positions"], st["avg_count"], st["sell_failed"], st["last_sell_price"] = [], 0, False, price
                break

        if st.get("sell_failed"):
            st["positions"] = []

        # ПОКУПКИ
        if sig == "buy" and not st["positions"]:
            last_stop = st.get("last_stop_time", "")
            hrs = hours_since(last_stop) if last_stop else 999
            if last_stop and hrs < 4:
                if should_log_skip(sym, "stop_buy"):
                    log_skip(sym, f"Пропуск BUY — прошло лишь {hrs:.1f}ч после стоп‑лосса (мин 4ч)")
            elif avail >= LIMITS[sym]["min_amt"]:
                qty = get_qty(sym, price, avail)
                if qty > 0:
                    cost = price * qty
                    buy_comm = cost * TAKER_BUY_FEE
                    mul = choose_multiplier(atr, price)
                    atr_tp = price + mul * atr

                    required_pnl = max(MIN_NET_PROFIT, dynamic_min_profit(atr, price))
                    req_tp = tp_from_required_profit(price, qty, required_pnl)

                    tp_price = max(atr_tp, req_tp)

                    # оценим PnL на этом TP
                    sell_comm = tp_price * qty * TAKER_SELL_FEE
                    est_pnl = (tp_price - price) * qty - (buy_comm + sell_comm)

                    logging.info(f"[{sym}] mul={mul:.2f}, atr_tp={atr_tp:.6f}, req_tp={req_tp:.6f}, tp={tp_price:.6f}")
                    logging.info(f"[{sym}] est_pnl: qty={qty}, cost={cost:.6f}, buy_fee={buy_comm:.6f}, "
                                 f"sell_fee={sell_comm:.6f}, est_pnl={est_pnl:.6f}, required={required_pnl:.2f}")

                    if est_pnl >= required_pnl - 1e-6:  # маленькая погрешность
                        session.place_order(category="spot", symbol=sym, side="Buy",
                                            orderType="Market", qty=str(qty))
                        STATE[sym]["positions"].append({
                            "buy_price": price, "qty": qty, "tp": tp_price,
                            "timestamp": datetime.datetime.now().isoformat()
                        })
                        log_trade(sym, "BUY", price, qty, 0.0, f"open from USDT, TP={tp_price:.6f}")
                        avail -= cost
                    else:
                        if should_log_skip(sym, "skip_low_profit"):
                            log_skip(sym, f"Пропуск BUY — TP не даёт требуемый PnL (нужно {required_pnl:.2f}, получилось {est_pnl:.2f})")
                else:
                    if should_log_skip(sym, "skip_qty"):
                        limit = LIMITS[sym]
                        log_skip(sym, f"Пропуск BUY — qty=0 (price={price:.6f}, step={limit['qty_step']}, "
                                      f"min_qty={limit['min_qty']}, min_amt={limit['min_amt']})")
            else:
                if should_log_skip(sym, "skip_funds"):
                    log_skip(sym, "Пропуск BUY — не хватает свободного USDT")

    cycle_count += 1

    # Ежесуточный отчёт (локальное время сервера)
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        send_daily_report()
        LAST_REPORT_DATE = now.date()

# -------------------- Daily report --------------------
def send_daily_report():
    lines = ["📊 Ежедневный отчёт:"]
    total_pnl = 0.0
    for s in SYMBOLS:
        st = STATE[s]
        pos_lines = []
        for p in st["positions"]:
            pos_lines.append(f"{p['qty']} @ {p['buy_price']:.6f} → TP {p['tp']:.6f}")
        pos_text = "\n    " + "\n    ".join(pos_lines) if pos_lines else " нет открытых позиций"
        lines.append(f"• {s}: PnL={st['pnl']:.2f};{pos_text}")
        total_pnl += st["pnl"]
    lines.append(f"Σ Итоговый PnL: {total_pnl:.2f}")
    send_tg("\n".join(lines))

# -------------------- Main --------------------
if __name__ == "__main__":
    init_state()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log_msg(f"Global error: {e}", True)
        time.sleep(60)
