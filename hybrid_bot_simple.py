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

DEFAULT_PARAMS = {
    "risk_pct": 0.01,
    "tp_multiplier": 2.2,
    "trail_multiplier": 1.5,
    "max_drawdown_sl": 0.06,
    "min_avg_drawdown": 0.03,
    "trailing_stop_pct": 0.02,
    "min_profit_usd": 2.5
}

RESERVE_BALANCE = 500
DAILY_LIMIT = -50
MIN_TP_PCT = 0.007
COOLDOWN_AVG_SECS = 600
MAX_AVG_COUNT = 3
MAX_POSITION_SIZE_USDT = 30  # 🔁 Изменено с 50 на 30

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def log(msg):
    logging.info(msg)

def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={
            "chat_id": CHAT_ID,
            "text": msg.encode("utf-8", errors="ignore").decode("utf-8")
        })
    except Exception as e:
        log(f"Telegram error: {e}")

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
        log(f"❌ Ошибка загрузки лимитов: {e}")
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
        log(f"[{sym}] IND → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, ATR={last['atr']:.4f}")

    if last["volume_change"] < -0.5:
        return "none", last["atr"], "⛔️ Объём упал сильно (>50%)"

    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"] and last["rsi"] < 75:
        return "buy", last["atr"], "📈 BUY сигнал (агрессивный)"
    elif last["ema9"] < last["ema21"] and last["macd"] < last["macd_signal"] and last["rsi"] > 30:
        return "sell", last["atr"], "🔻 SELL сигнал"
    else:
        return "none", last["atr"], "нет сигнала"

def detect_market_phase(df):
    if df["ema9"].iloc[-1] > df["ema21"].iloc[-1] and df["macd"].iloc[-1] > df["macd_signal"].iloc[-1]:
        return "bull"
    elif df["ema9"].iloc[-1] < df["ema21"].iloc[-1] and df["macd"].iloc[-1] < df["macd_signal"].iloc[-1]:
        return "bear"
    else:
        return "sideways"

def calc_adaptive_tp(price, atr, qty, params):
    fee = price * qty * 0.001
    base_tp = price + params["tp_multiplier"] * atr
    tp_dollar_min = (params["min_profit_usd"] + fee) / qty + price
    return max(base_tp, tp_dollar_min)

def calc_adaptive_qty(balance, atr, price, sym, risk_pct):
    max_usdt = MAX_POSITION_SIZE_USDT
    risk_usdt = min(balance * risk_pct, max_usdt)
    qty = risk_usdt / price  # ✅ расчёт по цене, не по ATR
    step = LIMITS[sym]["qty_step"]
    return adjust_qty(qty, step)

def log_trade(sym, action, price, qty, pnl, reason):
    value = price * qty
    roi = (pnl / value * 100) if value > 0 else 0
    log_msg = f"{action} {sym} @ {price:.4f}, qty={qty:.6f}, USDT={value:.2f}, PnL={pnl:.4f}, ROI={roi:.2f}% | Причина: {reason}"
    log(log_msg)
    send_tg(f"{action} {sym} @ {price:.4f}\nQty: {qty:.6f}\nValue: {value:.2f} USDT\nPnL: {pnl:.4f} ({roi:.2f}%)\n📌 {reason}")

def should_exit_by_rsi(df):
    return df["rsi"].iloc[-1] >= 80

def should_exit_by_trailing(price, peak_price, params):
    trailing_pct = params["trailing_stop_pct"]
    return peak_price and price <= peak_price * (1 - trailing_pct)

STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            content = f.read().strip()
            if content:
                STATE = json.loads(content)
                for sym in STATE:
                    for p in STATE[sym]["positions"]:
                        p.setdefault("peak_price", p["buy_price"])
                        log(f"[{sym}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
            else:
                STATE = {}
    except Exception as e:
        log(f"❌ Ошибка чтения state.json: {e}")
else:
    log("📁 state.json не найден. Начинаем заново.")

for s in SYMBOLS:
    STATE.setdefault(s, {"positions": [], "pnl": 0.0, "count": 0, "avg_count": 0, "volume_total": 0.0, "last_avg_time": 0})

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ Ошибка при сохранении state: {e}")
CYCLE_COUNT = 0
LAST_REPORT_DATE = None

def trade():
    global CYCLE_COUNT
    usdt = get_balance()
    log(f"💰 USDT Баланс: {usdt:.2f}")

    if usdt < RESERVE_BALANCE:
        log("⚠️ Баланс ниже резервного лимита — торговля отключена")
        return

    if sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LIMIT:
        log("⛔️ Превышен дневной лимит убытков — торговля остановлена")
        return

    CYCLE_COUNT += 1
    load_symbol_limits()

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                log(f"[{sym}] ❌ Нет данных по свечам")
                continue

            sig, atr, reason = signal(df, sym)
            phase = detect_market_phase(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            now = time.time()
            params = DEFAULT_PARAMS

            log(f"[{sym}] 📊 Фаза: {phase} | Сигнал: {sig} | Причина: {reason}")

            # === SELL ===
            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
                peak = max(pos.get("peak_price", b), price)
                pnl = (price - b) * q - price * q * 0.001
                drawdown = (b - price) / b

                if price >= tp or should_exit_by_rsi(df) or should_exit_by_trailing(price, peak, params):
                    reason_exit = "🎯 TP" if price >= tp else ("📉 RSI>80" if should_exit_by_rsi(df) else "📉 Трейлинг стоп")
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, reason_exit)
                    state["pnl"] += pnl
                    state["avg_count"] = 0
                elif drawdown >= params["max_drawdown_sl"]:
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "STOP LOSS", price, q, pnl, f"🔻 SL: просадка {drawdown:.2%}")
                    state["pnl"] += pnl
                    state["avg_count"] = 0
                else:
                    pos["tp"] = calc_adaptive_tp(price, atr, q, params)
                    pos["peak_price"] = peak
                    new_positions.append(pos)

            state["positions"] = new_positions

            # === BUY ===
            if sig == "buy":
                if phase == "bear":
                    log(f"[{sym}] ❌ Пропуск BUY — медвежий рынок")
                    continue
                if phase == "sideways" and df["rsi"].iloc[-1] <= 50:
                    log(f"[{sym}] ⏸ RSI во флете < 50 — отказ от входа")
                    continue

                min_amt = LIMITS[sym]["min_amt"]

                if not state["positions"]:
                    qty = calc_adaptive_qty(usdt, atr, price, sym, params["risk_pct"])
                    amt = qty * price
                    log(f"[{sym}] 💰 Попытка покупки qty={qty:.6f}, сумма={amt:.2f} USDT, мин={min_amt:.2f}")

                    if qty and min_amt <= amt <= MAX_POSITION_SIZE_USDT:
                        tp = calc_adaptive_tp(price, atr, qty, params)
                        profit = (tp - price) * qty - price * qty * 0.001
                        if profit >= params["min_profit_usd"]:
                            session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                            state["positions"].append({
                                "buy_price": price,
                                "qty": qty,
                                "tp": tp,
                                "peak_price": price
                            })
                            state["count"] += 1
                            state["volume_total"] += amt
                            state["last_avg_time"] = now
                            log_trade(sym, "BUY", price, qty, 0, reason)
                        else:
                            log(f"[{sym}] ❌ Пропуск — TP < {params['min_profit_usd']:.2f}")
                    else:
                        log(f"[{sym}] ❌ Пропуск — Объём меньше min_amt или превышает MAX")

                elif state["avg_count"] < MAX_AVG_COUNT and state["volume_total"] < MAX_POSITION_SIZE_USDT:
                    b = state["positions"][0]["buy_price"]
                    drawdown = (b - price) / b
                    if (
                        phase in ["bull", "sideways"] and
                        drawdown >= params["min_avg_drawdown"] and
                        now - state.get("last_avg_time", 0) >= COOLDOWN_AVG_SECS and
                        df["rsi"].iloc[-1] > 50
                    ):
                        qty = calc_adaptive_qty(usdt / 2, atr, price, sym, params["risk_pct"])
                        amt = qty * price
                        if qty and amt >= min_amt:
                            tp = calc_adaptive_tp(price, atr, qty, params)
                            profit = (tp - price) * qty - price * qty * 0.001
                            if profit >= params["min_profit_usd"]:
                                session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                                state["positions"].append({
                                    "buy_price": price,
                                    "qty": qty,
                                    "tp": tp,
                                    "peak_price": price
                                })
                                state["avg_count"] += 1
                                state["volume_total"] += amt
                                state["last_avg_time"] = now
                                log_trade(sym, "BUY AVG", price, qty, 0, f"📉 Усреднение ({drawdown:.2%})")

            if CYCLE_COUNT % 30 == 0:
                cb = get_coin_balance(sym)
                if cb:
                    log(f"[{sym}] 📦 Баланс: {cb:.2f} ({cb * price:.2f} USDT), Позиции: {len(state['positions'])}")

        except Exception as e:
            log(f"[{sym}] ❌ Ошибка ({type(e).__name__}): {e}")

    save_state()

def daily_report():
    global LAST_REPORT_DATE
    now = datetime.datetime.now()
    report_time = now.replace(hour=22, minute=30, second=0, microsecond=0)
    if now >= report_time and LAST_REPORT_DATE != now.date():
        rep = f"📊 Отчёт за {now.date()}:\n\n"
        total_pnl = 0
        rep += f"{'Монета':<10} | {'Сделок':<7} | {'PnL':<10}\n"
        rep += "-" * 30 + "\n"
        for sym, data in STATE.items():
            rep += f"{sym:<10} | {data['count']:<7} | {data['pnl']:.2f} USDT\n"
            total_pnl += data["pnl"]
        rep += "-" * 30
        rep += f"\n💰 Баланс: {get_balance():.2f} USDT\n📈 Общий PnL: {total_pnl:.2f} USDT"
        send_tg(rep)
        LAST_REPORT_DATE = now.date()
def main():
    send_tg("🚀 Бот запущен (агрессивный профиль)")
    log("🚀 Бот запущен (агрессивный профиль)")
    while True:
        try:
            trade()
            daily_report()
            time.sleep(60)
        except Exception as e:
            log(f"❌ Главная ошибка: {type(e).__name__} — {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
