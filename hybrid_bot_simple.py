os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from pybit.unified_trading import HTTP

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
DAILY_LIMIT = -50
MIN_PROFIT_PCT = 0.005
MIN_ABS_PNL = 2.0

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8", errors="ignore"),
        logging.StreamHandler()
    ]
)

def log(msg, tg=False):
    logging.info(msg)
    if tg:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={
                "chat_id": CHAT_ID,
                "text": msg.encode("utf-8", errors="ignore").decode("utf-8")
            })
        except Exception as e:
            logging.error(f"Telegram error: {e}")

def send_tg(msg): log(msg, tg=True)

def adjust_qty(qty, step):
    try:
        exponent = int(f"{float(step):e}".split("e")[-1])
        return math.floor(qty * 10**abs(exponent)) / 10**abs(exponent)
    except:
        return qty

# Балансы
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

# Лимиты
LIMITS = {}
def load_symbol_limits():
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
    except Exception as e:
        log(f"❌ Ошибка лимитов: {e}")

def get_qty(sym, price, usdt):
    if sym not in LIMITS:
        return 0
    q = adjust_qty(usdt / price, LIMITS[sym]["qty_step"])
    if q < LIMITS[sym]["min_qty"] or q * price < LIMITS[sym]["min_amt"]:
        return 0
    if "PEPE" in sym and q > 1_000_000:
        q = 1_000_000
    return q

# Восстановление состояния
STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            content = f.read().strip()
            if content:
                STATE = json.loads(content)
                for sym in STATE:
                    for p in STATE[sym]["positions"]:
                        log(f"[{sym}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
            else:
                STATE = {}
                log("🆕 state.json пуст — старт с чистого листа.")
    except Exception as e:
        log(f"❌ Ошибка чтения state.json: {e}")
else:
    log("📁 Файл state.json не найден. Начинаем заново.")

# Структура STATE по умолчанию
for s in SYMBOLS:
    STATE.setdefault(s, {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0, "volume_total": 0.0})

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ Ошибка при сохранении state: {e}")

# Индикаторы
def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"])
        df[["o","h","l","c","vol"]] = df[["o","h","l","c","vol"]].astype(float)
        return df
    except:
        return pd.DataFrame()

def signal(df, sym=None):
    df["ema9"] = EMAIndicator(df["c"], window=9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], window=21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    macd = MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    last = df.iloc[-1]

    if sym:
        log(f"[{sym}] INDICATORS → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, ATR={last['atr']:.4f}")

    if last["ema9"] > last["ema21"] and last["rsi"] > 45 and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], "📈 BUY сигнал"
    elif last["ema9"] < last["ema21"] and last["rsi"] < 55 and last["macd"] < last["macd_signal"]:
        return "sell", last["atr"], "🔻 SELL сигнал"
    else:
        return "none", last["atr"], "нет сигнала"
def log_trade(sym, action, price, qty, pnl, reason):
    value = price * qty
    roi = (pnl / value * 100) if value > 0 else 0
    msg = f"{action} {sym} @ {price:.4f}, qty={qty}, USDT={value:.2f}, PnL={pnl:.4f}, ROI={roi:.2f}% | Причина: {reason}"
    log(msg, tg=True)

CYCLE_COUNT = 0

def trade():
    global CYCLE_COUNT
    usdt = get_balance()
    if usdt < RESERVE_BALANCE:
        log("⚠️ Баланс ниже резервного лимита", tg=True)
        return

    if sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LIMIT:
        log("⛔️ Превышен дневной лимит убытков", tg=True)
        return

    CYCLE_COUNT += 1
    load_symbol_limits()
    per_coin = max(0, usdt - RESERVE_BALANCE) / len(SYMBOLS)

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            sig, atr, reason = signal(df, sym)

            if sig == "buy":
                log(f"[{sym}] 📡 Сигнал: BUY | Причина: {reason}")
            elif sig == "sell":
                log(f"[{sym}] 📡 Сигнал: SELL | Причина: {reason}")
            else:
                log(f"[{sym}] 📡 Сигнал: NONE | Причина: {reason}")

            price = df["c"].iloc[-1]
            coin_bal = get_coin_balance(sym)
            state = STATE[sym]
            limits = LIMITS.get(sym, {})
            new_positions = []

            # SELL логика
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
                if coin_bal < q: continue
                pnl = (price - b) * q - price * q * 0.001
                if price >= tp or (sig == "sell" and (pnl >= price * q * MIN_PROFIT_PCT or pnl >= MIN_ABS_PNL)):
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, reason)
                    state["pnl"] += pnl
                    coin_bal -= q
                    state["avg_count"] = 0
                else:
                    pos["tp"] = max(tp, price + TRAIL_MULTIPLIER * atr)
                    new_positions.append(pos)

            state["positions"] = new_positions

            # BUY логика
            if sig == "buy":
                if not state["positions"]:
                    qty = get_qty(sym, price, per_coin)
                    if qty:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        tp = price + TRAIL_MULTIPLIER * atr
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                        state["count"] += 1
                        state["volume_total"] += qty * price
                        log_trade(sym, "BUY", price, qty, 0, reason)
                    else:
                        log(f"[{sym}] ❌ Недостаточно средств или объём слишком мал")

                elif state["avg_count"] < 2:
                    b = state["positions"][0]["buy_price"]
                    drawdown = (b - price) / b
                    if drawdown > 0.02:
                        qty = get_qty(sym, price, per_coin / 2)
                        if qty:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            tp = price + TRAIL_MULTIPLIER * atr
                            state["positions"].append({"buy_price": price, "qty": qty, "tp": tp})
                            state["avg_count"] += 1
                            state["volume_total"] += qty * price
                            log_trade(sym, "BUY AVG", price, qty, 0, f"📉 Усреднение при просадке ({drawdown:.2%})")

            # Периодическая информация
            if CYCLE_COUNT % 30 == 0:
                cb = get_coin_balance(sym)
                if cb:
                    log(f"[{sym}] 📊 Баланс: {cb:.2f} ≈ {cb * price:.2f} USDT")

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка ({type(e).__name__}): {e}")

    save_state()

def daily_report():
    now = datetime.datetime.now()
    if now.hour == 22 and now.minute >= 30:
        rep = f"📊 Отчёт за {now.date()}:\n"
        total_pnl = 0
        for sym, data in STATE.items():
            rep += f"{sym:<10} | Сделок: {data['count']} | PnL: {data['pnl']:.2f} USDT\n"
            total_pnl += data["pnl"]
        rep += f"\n💰 Баланс: {get_balance():.2f} USDT\n📈 Общий PnL: {total_pnl:.2f} USDT"
        send_tg(rep)

def main():
    log("🚀 Бот запущен", tg=True)
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
