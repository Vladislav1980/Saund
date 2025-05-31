import os, time, math, json, datetime, logging, requests
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
    "WIFUSDT", "ARBUSDT", "SUIUSDT", "FILUSDT"
]

# === Глобальные настройки по умолчанию ===
DEFAULT_PARAMS = {
    "risk_pct": 0.01,
    "tp_multiplier": 2.5,
    "trail_multiplier": 1.5,
    "max_drawdown_sl": 0.06,
    "min_avg_drawdown": 0.03,
    "trailing_stop_pct": 0.02,  # 2% откат от максимума — трейлинг
}

PARAMS_BY_SYMBOL = {
    "ADAUSDT": {
        "risk_pct": 0.015,
        "tp_multiplier": 2.8,
        "trail_multiplier": 1.4,
        "max_drawdown_sl": 0.05,
        "min_avg_drawdown": 0.03,
        "trailing_stop_pct": 0.018,
    },
    # Добавляй другие монеты при желании
}

MIN_EXPECTED_PROFIT_USD = 3.0
RESERVE_BALANCE = 500
DAILY_LIMIT = -50
MIN_TP_PCT = 0.007
COOLDOWN_AVG_SECS = 600
MAX_AVG_COUNT = 3
MAX_POSITION_SIZE_USDT = 50

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

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
def get_kline(sym):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval="1", limit=100)
        df = pd.DataFrame(r["result"]["list"], columns=["ts", "o", "h", "l", "c", "vol", "turn"])
        df[["o", "h", "l", "c", "vol"]] = df[["o", "h", "l", "c", "vol"]].astype(float)
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
    df["volume_change"] = df["vol"].pct_change().fillna(0)

    last = df.iloc[-1]

    if sym:
        log(f"[{sym}] INDICATORS → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, ATR={last['atr']:.4f}")

    # Фильтрация по объёму — не входить на слабой активности
    if last["volume_change"] < -0.3:
        return "none", last["atr"], "⛔️ Объём падает"

    # Основные сигналы
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"] and last["rsi"] < 70:
        return "buy", last["atr"], "📈 BUY сигнал"
    elif last["ema9"] < last["ema21"] and last["macd"] < last["macd_signal"] and last["rsi"] > 30:
        return "sell", last["atr"], "🔻 SELL сигнал"
    else:
        return "none", last["atr"], "нет сигнала"

def detect_market_phase(df):
    short_ma = df["ema9"]
    long_ma = df["ema21"]
    macd = df["macd"]
    macd_signal = df["macd_signal"]

    if short_ma.iloc[-1] > long_ma.iloc[-1] and macd.iloc[-1] > macd_signal.iloc[-1]:
        return "bull"
    elif short_ma.iloc[-1] < long_ma.iloc[-1] and macd.iloc[-1] < macd_signal.iloc[-1]:
        return "bear"
    else:
        return "sideways"

def calc_adaptive_tp(price, atr, qty, params):
    fee = price * qty * 0.001
    base_tp = price + params["tp_multiplier"] * atr
    tp_dollar_min = (MIN_EXPECTED_PROFIT_USD + fee) / qty + price
    return max(base_tp, tp_dollar_min)

def calc_adaptive_qty(balance, atr, price, sym, risk_pct):
    risk_usdt = balance * risk_pct
    qty = risk_usdt / atr
    step = LIMITS[sym]["qty_step"]
    return adjust_qty(qty, step)

def log_trade(sym, action, price, qty, pnl, reason):
    value = price * qty
    roi = (pnl / value * 100) if value > 0 else 0
    msg = f"{action} {sym} @ {price:.4f}, qty={qty}, USDT={value:.2f}, PnL={pnl:.4f}, ROI={roi:.2f}% | Причина: {reason}"
    log(msg, tg=True)

def should_exit_by_rsi(df):
    rsi = df["rsi"].iloc[-1]
    return rsi >= 80

def should_exit_by_trailing(price, peak_price, params):
    trailing_pct = params["trailing_stop_pct"]
    if peak_price and price <= peak_price * (1 - trailing_pct):
        return True
    return False
CYCLE_COUNT = 0
LAST_REPORT_DATE = None

STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            content = f.read().strip()
            if content:
                STATE = json.loads(content)
                for sym in STATE:
                    for p in STATE[sym]["positions"]:
                        p.setdefault("peak_price", p["buy_price"])  # для трейлинга
                        log(f"[{sym}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
            else:
                STATE = {}
                log("🆕 state.json пуст — старт с чистого листа.")
    except Exception as e:
        log(f"❌ Ошибка чтения state.json: {e}")
else:
    log("📁 Файл state.json не найден. Начинаем заново.")

for s in SYMBOLS:
    STATE.setdefault(s, {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0, "volume_total": 0.0, "last_avg_time": 0})

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ Ошибка при сохранении state: {e}")

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

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty: continue
            sig, atr, reason = signal(df, sym)
            phase = detect_market_phase(df)
            log(f"[{sym}] 📊 Фаза: {phase} | Сигнал: {sig} | {reason}")

            price = df["c"].iloc[-1]
            coin_bal = get_coin_balance(sym)
            state = STATE[sym]
            now = time.time()
            params = PARAMS_BY_SYMBOL.get(sym, DEFAULT_PARAMS)

            # --- SELL + Трейлинг + RSI выход ---
            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
                peak = pos.get("peak_price", b)
                peak = max(peak, price)
                pnl = (price - b) * q - price * q * 0.001
                drawdown = (b - price) / b

                should_exit_rsi = should_exit_by_rsi(df)
                should_exit_trail = should_exit_by_trailing(price, peak, params)

                if price >= tp or should_exit_rsi or should_exit_trail:
                    reason_exit = "🎯 TP" if price >= tp else ("📉 RSI>80" if should_exit_rsi else "📉 Трейлинг стоп")
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, reason_exit)
                    state["pnl"] += pnl
                    coin_bal -= q
                    state["avg_count"] = 0
                elif drawdown >= params["max_drawdown_sl"]:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS", price, q, pnl, f"🔻 SL: просадка {drawdown:.2%}")
                    state["pnl"] += pnl
                    coin_bal -= q
                    state["avg_count"] = 0
                else:
                    pos["tp"] = calc_adaptive_tp(price, atr, q, params)
                    pos["peak_price"] = peak
                    new_positions.append(pos)

            state["positions"] = new_positions

            # --- BUY логика ---
            if sig == "buy":
                if phase == "bear":
                    log(f"[{sym}] ❌ Игнор BUY — BEAR рынок")
                    continue
                elif phase == "sideways":
                    log(f"[{sym}] ⏸ Флет — вход нецелесообразен")
                    continue

                if not state["positions"]:
                    qty = calc_adaptive_qty(usdt, atr, price, sym, params["risk_pct"])
                    if qty and qty * price <= MAX_POSITION_SIZE_USDT:
                        tp = calc_adaptive_tp(price, atr, qty, params)
                        expected_profit = (tp - price) * qty - price * qty * 0.001
                        if expected_profit >= MIN_EXPECTED_PROFIT_USD:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            state["positions"].append({
                                "buy_price": price,
                                "qty": qty,
                                "tp": tp,
                                "peak_price": price
                            })
                            state["count"] += 1
                            state["volume_total"] += qty * price
                            state["last_avg_time"] = now
                            log_trade(sym, "BUY", price, qty, 0, reason)
                        else:
                            log(f"[{sym}] ❌ TP < {MIN_EXPECTED_PROFIT_USD:.2f} USDT → Пропуск")
                    else:
                        log(f"[{sym}] ❌ Недостаточно средств или объём слишком мал")

                elif state["avg_count"] < MAX_AVG_COUNT and state["volume_total"] < MAX_POSITION_SIZE_USDT:
                    b = state["positions"][0]["buy_price"]
                    drawdown = (b - price) / b
                    if (
                        phase == "bull" and
                        drawdown >= params["min_avg_drawdown"] and
                        now - state.get("last_avg_time", 0) >= COOLDOWN_AVG_SECS and
                        df["rsi"].iloc[-1] > 50
                    ):
                        qty = calc_adaptive_qty(usdt / 2, atr, price, sym, params["risk_pct"])
                        if qty:
                            tp = calc_adaptive_tp(price, atr, qty, params)
                            expected_profit = (tp - price) * qty - price * qty * 0.001
                            if expected_profit >= MIN_EXPECTED_PROFIT_USD:
                                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                                state["positions"].append({
                                    "buy_price": price,
                                    "qty": qty,
                                    "tp": tp,
                                    "peak_price": price
                                })
                                state["avg_count"] += 1
                                state["volume_total"] += qty * price
                                state["last_avg_time"] = now
                                log_trade(sym, "BUY AVG", price, qty, 0, f"📉 Усреднение ({drawdown:.2%})")

            if CYCLE_COUNT % 30 == 0:
                cb = get_coin_balance(sym)
                if cb:
                    log(f"[{sym}] 📊 Баланс: {cb:.2f} ≈ {cb * price:.2f} USDT")

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка ({type(e).__name__}): {e}")

    save_state()

def daily_report():
    global LAST_REPORT_DATE
    now = datetime.datetime.now()
    report_time = now.replace(hour=22, minute=30, second=0, microsecond=0)
    if now >= report_time and LAST_REPORT_DATE != now.date():
        rep = f"📊 Отчёт за {now.date()}:\n"
        total_pnl = 0
        for sym, data in STATE.items():
            rep += f"{sym:<10} | Сделок: {data['count']} | PnL: {data['pnl']:.2f} USDT\n"
            total_pnl += data["pnl"]
        rep += f"\n💰 Баланс: {get_balance():.2f} USDT\n📈 Общий PnL: {total_pnl:.2f} USDT"
        send_tg(rep)
        LAST_REPORT_DATE = now.date()

def main():
    log("🚀 Бот запущен", tg=True)
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
