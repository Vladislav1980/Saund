import os, time, math, logging, datetime, requests, json
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# === НАСТРОЙКИ ===
load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TG_VERBOSE = True
RESERVE_BALANCE = 500
TRAIL_MULTIPLIER = 1.5
MIN_PROFIT_PCT = 0.005
MIN_ABSOLUTE_PNL = 3.00
STOP_LOSS_PCT = -0.15

SYMBOLS = [
    "COMPUSDT", "NEARUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "PEPEUSDT", "WIFUSDT", "ARBUSDT", "SUIUSDT", "FILUSDT"
]

AGGRESSIVE = ["PEPEUSDT", "WIFUSDT", "ARBUSDT", "SUIUSDT"]
BALANCED = ["NEARUSDT", "TONUSDT", "XRPUSDT", "AVAXUSDT", "FILUSDT"]
CONSERVATIVE = ["COMPUSDT", "TRXUSDT", "ADAUSDT", "BCHUSDT", "LTCUSDT"]

STRATEGY_PARAMS = {
    "aggressive": {"tp_mult": 3.0, "max_drawdown": 0.07, "max_avg": 2},
    "balanced": {"tp_mult": 2.0, "max_drawdown": 0.10, "max_avg": 2},
    "conservative": {"tp_mult": 1.5, "max_drawdown": 0.08, "max_avg": 1}
}

STATE_FILE = "state.json"
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

STATE = {s: {
    "positions": [], "pnl": 0.0, "count": 0,
    "avg_count": 0, "last_sell_price": 0.0, "volume_total": 0.0,
    "last_stoploss_time": 0
} for s in SYMBOLS}

LIMITS, LAST_REPORT_DATE = {}, None
cycle_count = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg):
    if TG_VERBOSE:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
        except: pass

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def log_trade(sym, side, price, qty, pnl):
    usdt_value = price * qty
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, USDT={usdt_value:.2f}, PnL={pnl:.4f}"
    log(msg, True)
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt_value:.2f},{pnl:.4f}\n")
    STATE[sym]["volume_total"] += usdt_value

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

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except: return qty

def signal(df):
    if df.empty or len(df) < 50:
        return "none", 0
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 14).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(df["c"])
    df["macd"], df["macd_signal"] = macd.macd(), macd.macd_signal()
    last = df.iloc[-1]
    if last["ema9"] > last["ema21"] and last["rsi"] >= 40 and last["macd"] >= last["macd_signal"]:
        return "buy", last["atr"]
    elif last["ema9"] < last["ema21"] and last["rsi"] < 45 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"]
    return "none", last["atr"]

def get_kline(sym):
    r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
    df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
    df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
    return df

def get_balance():
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
    return float(next((c["walletBalance"] for c in coins if c["coin"] == coin), 0))

def get_qty(sym, price, usdt):
    if sym not in LIMITS: return 0
    q = adjust_qty(usdt / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]: return 0
    return q

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ Ошибка при сохранении STATE: {e}")

def load_state_from_file():
    global STATE
    try:
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
            for sym in SYMBOLS:
                if sym in saved:
                    STATE[sym].update(saved[sym])
        log("📂 STATE восстановлен из файла.")
    except FileNotFoundError:
        log("🆕 STATE-файл не найден — начинаем с чистого.")
    except Exception as e:
        log(f"❌ Ошибка загрузки STATE: {e}")

def get_group(sym):
    if sym in AGGRESSIVE: return "aggressive"
    if sym in BALANCED: return "balanced"
    return "conservative"
def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty: continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(sym)
        if price and bal * price >= LIMITS.get(sym, {}).get("min_amt", 0):
            qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
            tp = price + TRAIL_MULTIPLIER * (df["h"] - df["l"]).mean()
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
            log(f"[{sym}] 🔄 Восстановлена позиция: qty={qty}, price={price:.4f}")

def trade():
    global LAST_REPORT_DATE, cycle_count
    usdt = get_balance()
    log(f"💰 Баланс: {usdt:.2f} USDT")
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            group = get_group(sym)
            params = STRATEGY_PARAMS[group]
            df = get_kline(sym)
            if df.empty: continue
            sig, atr = signal(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price
            new_positions = []

            log(f"[{sym}] 🔍 SIGNAL={sig.upper()} | EMA9={df['ema9'].iloc[-1]:.4f}, EMA21={df['ema21'].iloc[-1]:.4f}, RSI={df['rsi'].iloc[-1]:.1f}, MACD={df['macd'].iloc[-1]:.4f}, VOL={df['vol'].iloc[-1]:.2f}")

            for p in state["positions"]:
                b, q, tp = p["buy_price"], p["qty"], p["tp"]
                q = adjust_qty(q, limits["qty_step"])
                if q <= 0 or coin_bal < q: continue
                pnl = (price - b) * q - price * q * 0.001
                pnl_pct = (price - b) / b
                min_required_profit = max(price * q * MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL)

                if pnl_pct <= STOP_LOSS_PCT:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL (STOP-LOSS)", price, q, pnl)
                    state["pnl"] += pnl
                    state["last_stoploss_time"] = time.time()
                    coin_bal -= q
                    continue

                if price >= tp and pnl >= min_required_profit:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl)
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    coin_bal -= q
                else:
                    p["tp"] = max(tp, price + params["tp_mult"] * atr)
                    new_positions.append(p)

            state["positions"] = new_positions

            recently_stopped = time.time() - state.get("last_stoploss_time", 0) < 600

            if sig == "buy" and not recently_stopped and state["positions"] and state["avg_count"] < params["max_avg"]:
                total_qty = sum(p["qty"] for p in state["positions"])
                avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_qty
                drawdown = (price - avg_price) / avg_price
                if drawdown < 0 and abs(drawdown) <= params["max_drawdown"] and value < per_sym:
                    usdt_to_use = per_sym - value
                    qty = get_qty(sym, price, usdt_to_use)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + params["tp_mult"] * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        state["avg_count"] += 1
                        log_trade(sym, "BUY (усреднение)", price, qty, 0)

            if sig == "buy" and not recently_stopped and not state["positions"] and value < per_sym:
                if abs(price - state["last_sell_price"]) / price < 0.001:
                    log(f"[{sym}] ⚠️ Покупка пропущена: цена почти как последняя продажа ({price:.4f})")
                    continue
                usdt_to_use = per_sym - value
                qty = get_qty(sym, price, usdt_to_use)
                if qty and qty * price <= usdt:
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                    tp = price + params["tp_mult"] * atr
                    state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                    state["count"] += 1
                    log_trade(sym, "BUY", price, qty, 0)

            if not state["positions"] and sig == "sell" and coin_bal * price >= limits["min_amt"]:
                qty = adjust_qty(coin_bal, limits["qty_step"])
                if qty > 0:
                    log(f"[{sym}] ℹ️ Продажа пропущена: нет позиции")

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка: {e}")

    cycle_count += 1
    if cycle_count % 3 == 0:
        act = sum(len(v["positions"]) for v in STATE.values())
        log(f"⏳ Активных позиций: {act}")

    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        rep_lines = [f"📊 Ежедневный отчёт за {now.strftime('%Y-%m-%d')}"]
        rep_lines.append(f"💰 Баланс USDT: {usdt:.2f}")
        total_pnl = 0
        total_volume = 0
        active_positions = 0

        for sym in SYMBOLS:
            state = STATE[sym]
            pnl = state["pnl"]
            deals = state["count"]
            vol = state.get("volume_total", 0)
            coin_bal = get_coin_balance(sym)
            price = get_kline(sym)["c"].iloc[-1]
            value = coin_bal * price
            act = len(state["positions"])
            drawdowns = [(price - p["buy_price"]) / p["buy_price"] for p in state["positions"]]
            avg_dd = sum(drawdowns) / len(drawdowns) if drawdowns else 0
            active_positions += act
            total_pnl += pnl
            total_volume += vol
            rep_lines.append(f"{sym:<10} | Сделок={deals:<3} | PnL={pnl:>7.2f} | Баланс={coin_bal:>7.3f} | $={value:>7.2f} | Активных={act} | 📉Просадка={avg_dd*100:>5.1f}%")

        roi = (total_pnl / total_volume * 100) if total_volume else 0
        rep_lines.append(f"\n📈 Общий PnL: {total_pnl:.2f} USDT")
        rep_lines.append(f"📦 Активных позиций: {active_positions}")
        rep_lines.append(f"📊 Доходность к обороту: {roi:.2f}%")
        send_tg("\n".join(rep_lines))
        LAST_REPORT_DATE = now.date()

if __name__ == "__main__":
    log("🚀 Бот запущен", True)
    load_symbol_limits()
    load_state_from_file()
    init_positions()
    while True:
        try:
            trade()
            save_state()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {type(e).__name__}: {e}")
        time.sleep(60)
