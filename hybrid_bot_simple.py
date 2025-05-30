import os, time, math, logging, datetime, requests, json
import pandas as pd
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = [
    "COMPUSDT", "NEARUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "PEPEUSDT", "WIFUSDT", "ARBUSDT", "SUIUSDT", "FILUSDT"
]

RESERVE_BALANCE = 500
TRAIL_MULTIPLIER = 1.5
MIN_PROFIT_PCT = 0.005
MIN_ABSOLUTE_PNL = 3.0
STOP_LOSS_PCT = -0.15
DAILY_LIMIT = -50
MAX_AVERAGES = 2

STATE_FILE = "state.json"
LIMITS = {}
STATE = {s: {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0, "volume_total": 0.0, "last_sell_price": 0.0, "last_stoploss_time": 0} for s in SYMBOLS}
LAST_REPORT_DATE = None
CYCLE_COUNT = 0

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000, timeout=30)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        logging.error(f"[TG Error] {e}")

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        send_tg(msg)

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except: return qty

def load_symbol_limits(retries=3):
    global LIMITS
    for attempt in range(retries):
        try:
            data = session.get_instruments_info(category="spot")["result"]["list"]
            for item in data:
                if item["symbol"] in SYMBOLS:
                    f = item.get("lotSizeFilter", {})
                    LIMITS[item["symbol"]] = {
                        "min_qty": float(f.get("minOrderQty", 0.0)),
                        "qty_step": float(f.get("qtyStep", 1.0)),
                        "min_amt": float(item.get("minOrderAmt", 10.0))
                    }
            log("✅ Лимиты загружены")
            return
        except Exception as e:
            log(f"❌ Попытка {attempt+1} загрузки лимитов не удалась: {e}")
            time.sleep(5)
    if not LIMITS:
        log("🛑 Лимиты не получены — бот останавливается", tg=True)
        exit(1)

def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        return df
    except:
        return pd.DataFrame()

def get_balance():
    try:
        coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        return float(next(c["walletBalance"] for c in coins if c["coin"] == "USDT"))
    except:
        return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        coins = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["coin"]
        return float(next((c["walletBalance"] for c in coins if c["coin"] == coin), 0))
    except:
        return 0

def get_qty(sym, price, usdt):
    if sym not in LIMITS: return 0
    q = adjust_qty(usdt / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0
    return q

def signal(df, sym=""):
    if df.empty or len(df) < 50:
        return "none", 0, "недостаточно данных"
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"], 14).rsi()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"], 14).average_true_range()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    bb = BollingerBands(df["c"])
    df["bb_lower"] = bb.bollinger_lband()
    last = df.iloc[-1]
    vol_mean = df["vol"].rolling(20).mean().iloc[-1]
    vol_spike = last["vol"] > vol_mean * 1.2
    recent_growth = df["c"].iloc[-1] / df["c"].iloc[-20] - 1
    log(f"[{sym}] INDICATORS → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, VolX={last['vol']/vol_mean:.2f}, Δ24ч={recent_growth*100:.2f}%")
    if recent_growth > 0.05:
        return "none", last["atr"], "🚫 рост >5%, поздно входить"
    if last["ema9"] > last["ema21"] and last["rsi"] > 55 and last["macd"] > last["macd_signal"] and vol_spike and last["c"] < last["bb_lower"]:
        return "buy", last["atr"], "💡 BUY сигнал (EMA, RSI, MACD, BB, объём)"
    elif last["ema9"] < last["ema21"] and last["rsi"] < 45 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"], "🔻 SELL сигнал"
    return "none", last["atr"], "нет сигнала"
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ Ошибка сохранения STATE: {e}")

def log_trade(sym, side, price, qty, pnl, reason):
    usdt_value = price * qty
    msg = f"{side} {sym} @ {price:.4f}, qty={qty}, USDT={usdt_value:.2f}, PnL={pnl:.4f} | Причина: {reason}"
    log(msg, tg=True)
    with open("trades.csv", "a") as f:
        f.write(f"{datetime.datetime.now()},{sym},{side},{price},{qty},{usdt_value:.2f},{pnl:.4f},{reason}\n")
    STATE[sym]["volume_total"] += usdt_value

def init_positions():
    for sym in SYMBOLS:
        df = get_kline(sym)
        if df.empty: continue
        if sym not in LIMITS:
            log(f"[{sym}] ⛔ Пропуск восстановления — лимиты отсутствуют")
            continue
        price = df["c"].iloc[-1]
        bal = get_coin_balance(sym)
        if price and bal * price >= LIMITS[sym]["min_amt"]:
            qty = adjust_qty(bal, LIMITS[sym]["qty_step"])
            tp = price + TRAIL_MULTIPLIER * (df["h"] - df["l"]).mean()
            STATE[sym]["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
            log(f"[{sym}] 🔄 Восстановлена позиция: qty={qty}, price={price:.4f}")

def trade():
    global LAST_REPORT_DATE, CYCLE_COUNT
    usdt = get_balance()
    total_pnl_today = sum(v["pnl"] for v in STATE.values())

    if total_pnl_today <= DAILY_LIMIT:
        log("🚫 Превышен дневной лимит убытков, торговля остановлена", tg=True)
        return

    log(f"💰 Баланс: {usdt:.2f} USDT")
    avail = max(0, usdt - RESERVE_BALANCE)
    per_sym = avail / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue

            sig, atr, reason = signal(df, sym)
            log(f"[{sym}] 📡 Сигнал: {sig.upper()} | Причина: {reason}")

            price = df["c"].iloc[-1]
            state = STATE[sym]
            limits = LIMITS[sym]
            coin_bal = get_coin_balance(sym)
            value = coin_bal * price

            new_positions = []
            for p in state["positions"]:
                b, q, tp = p["buy_price"], p["qty"], p["tp"]
                q = adjust_qty(q, limits["qty_step"])
                if q <= 0 or coin_bal < q: continue
                pnl = (price - b) * q - price * q * 0.001
                pnl_pct = (price - b) / b
                if pnl_pct <= STOP_LOSS_PCT:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL (STOP)", price, q, pnl, f"❗️ StopLoss {pnl_pct*100:.1f}%")
                    state["pnl"] += pnl
                    state["last_stoploss_time"] = time.time()
                    coin_bal -= q
                    continue
                if price >= tp and pnl >= max(price * q * MIN_PROFIT_PCT, MIN_ABSOLUTE_PNL):
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, "🎯 TP достигнут")
                    state["pnl"] += pnl
                    state["last_sell_price"] = price
                    coin_bal -= q
                else:
                    p["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                    new_positions.append(p)
            state["positions"] = new_positions

            recently_stopped = time.time() - state.get("last_stoploss_time", 0) < 600

            if sig == "buy" and not recently_stopped and state["positions"] and state["avg_count"] < MAX_AVERAGES:
                total_qty = sum(p["qty"] for p in state["positions"])
                avg_price = sum(p["qty"] * p["buy_price"] for p in state["positions"]) / total_qty
                drawdown = (price - avg_price) / avg_price
                if drawdown < 0 and abs(drawdown) <= 0.10 and value < per_sym:
                    qty = get_qty(sym, price, per_sym - value)
                    if qty and qty * price <= usdt:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        state["avg_count"] += 1
                        log_trade(sym, "BUY (усреднение)", price, qty, 0, "📉 Усреднение")

            if sig == "buy" and not recently_stopped and not state["positions"] and value < per_sym:
                if abs(price - state["last_sell_price"]) / price < 0.001:
                    log(f"[{sym}] ⚠️ Пропущена покупка: цена ≈ последней продаже")
                    continue
                qty = get_qty(sym, price, per_sym)
                if qty and qty * price <= usdt:
                    session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                    tp = price + TRAIL_MULTIPLIER * atr
                    state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                    state["count"] += 1
                    log_trade(sym, "BUY", price, qty, 0, reason)

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка: {type(e).__name__}: {e}", tg=True)

    CYCLE_COUNT += 1
    if CYCLE_COUNT % 3 == 0:
        act = sum(len(v["positions"]) for v in STATE.values())
        log(f"📌 Активных позиций: {act}")

    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30 and LAST_REPORT_DATE != now.date():
        rep = f"📊 Отчёт за {now.date()}:\nUSDT: {usdt:.2f}\n"
        total_pnl = 0
        for s in SYMBOLS:
            v = STATE[s]
            total_pnl += v["pnl"]
            rep += f"{s:<8} | Сделок: {v['count']} | PnL: {v['pnl']:.2f}\n"
        rep += f"\n📈 Общий PnL: {total_pnl:.2f} USDT"
        send_tg(rep)
        LAST_REPORT_DATE = now.date()

    save_state()

if __name__ == "__main__":
    log("🚀 Бот запущен", tg=True)
    load_symbol_limits()
    init_positions()
    while True:
        try:
            trade()
        except Exception as e:
            log(f"🛑 Глобальная ошибка: {e}", tg=True)
        time.sleep(60)
