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
    "COMPUSDT", "NEARUSDT", "TONUSDT", "XRPUSDT",
    "ADAUSDT", "BCHUSDT", "LTCUSDT", "AVAXUSDT",
    "SUIUSDT", "FILUSDT"
]

DEFAULT_PARAMS = {
    "risk_pct": 0.03,
    "tp_multiplier": 1.8,
    "trail_multiplier": 1.5,
    "max_drawdown_sl": 0.06,
    "min_avg_drawdown": 0.03,
    "trailing_stop_pct": 0.02,
    "min_profit_usd": 5.0
}

RESERVE_BALANCE = 500
DAILY_LIMIT = -50
VOLUME_FILTER = 0.3
MAX_POSITION_SIZE_USDT = 100

session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)

def log(msg): logging.info(msg)
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg})
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
        wallets = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
        for w in wallets:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
        return 0
    except Exception as e:
        log(f"❌ get_balance error: {e}")
        return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT","")
    try:
        wallets = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]
        for w in wallets:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
        return 0
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
        log(f"❌ load_symbol_limits error: {e}")

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
    df["volume_change"] = df["vol"].pct_change().fillna(0)
    last = df.iloc[-1]
    if sym:
        log(f"[{sym}] IND → EMA9={last['ema9']:.4f}, EMA21={last['ema21']:.4f}, RSI={last['rsi']:.1f}, MACD={last['macd']:.4f}/{last['macd_signal']:.4f}, ATR={last['atr']:.4f}")
    if last["volume_change"] < -VOLUME_FILTER:
        return "none", last["atr"], "⛔️ Объём упал сильно"
    if last["ema9"] > last["ema21"] and last["macd"] > last["macd_signal"]:
        return "buy", last["atr"], "📈 BUY сигнал (агрессивный)"
    elif last["ema9"] < last["ema21"] and last["macd"] < last["macd_signal"]:
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
    tp_min = (params["min_profit_usd"] + fee) / qty + price
    return max(base_tp, tp_min)

def calc_adaptive_qty(balance, atr, price, sym, risk_pct):
    risk_usdt = min(balance * risk_pct, MAX_POSITION_SIZE_USDT)
    qty = risk_usdt / price
    step = LIMITS[sym]["qty_step"]
    adjusted = adjust_qty(qty, step)
    if adjusted == 0 or adjusted * price < LIMITS[sym]["min_amt"]:
        log(f"[{sym}] ❌ qty после округления = {adjusted} / сумма не подходит")
        return 0
    return adjusted

def log_trade(sym, action, price, qty, pnl, reason):
    val = price * qty
    roi = (pnl / val * 100) if val > 0 else 0
    msg = f"{action} {sym} @ {price:.4f}, qty={qty:.6f}, USDT={val:.2f}, PnL={pnl:.4f}, ROI={roi:.2f}% | {reason}"
    log(msg)
    send_tg(msg)

def should_exit_by_rsi(df): return df["rsi"].iloc[-1] >= 80
def should_exit_by_trailing(price, peak, params): return peak and price <= peak * (1 - params["trailing_stop_pct"])
def is_profitable_exit(pnl, price, qty, params): return pnl >= params["min_profit_usd"] + price * qty * 0.001

STATE = {}
if os.path.exists("state.json"):
    try:
        with open("state.json", "r") as f:
            STATE = json.load(f)
        for sym in STATE:
            for p in STATE[sym]["positions"]:
                p.setdefault("peak_price", p["buy_price"])
                log(f"[{sym}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
    except Exception as e:
        log(f"❌ state.json error: {e}")
        STATE = {}

for s in SYMBOLS:
    STATE.setdefault(s, {"positions": [], "pnl": 0.0, "count": 0})

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(STATE, f, indent=2)
    except Exception as e:
        log(f"❌ save_state error: {e}")

CYCLE_COUNT = 0
LAST_REPORT_DATE = None

def trade():
    global CYCLE_COUNT
    usdt = get_balance()
    log(f"💰 USDT Баланс: {usdt:.2f}")
    if usdt < RESERVE_BALANCE or sum(STATE[s]["pnl"] for s in SYMBOLS) < DAILY_LIMIT:
        return
    CYCLE_COUNT += 1
    load_symbol_limits()

    for sym in SYMBOLS:
        try:
            df = get_kline(sym)
            if df.empty:
                continue
            sig, atr, reason = signal(df, sym)
            phase = detect_market_phase(df)
            price = df["c"].iloc[-1]
            state = STATE[sym]
            now = time.time()
            params = DEFAULT_PARAMS

            log(f"[{sym}] 📊 Фаза: {phase} | Сигнал: {sig} | Причина: {reason}")

            new_positions = []
            for pos in state["positions"]:
                b, q, tp = pos["buy_price"], pos["qty"], pos["tp"]
                peak = max(pos["peak_price"], price)
                pnl = (price - b) * q - price * q * 0.001
                if price >= tp or (should_exit_by_rsi(df) and is_profitable_exit(pnl, price, q, params)) or \
                   (should_exit_by_trailing(price, peak, params) and is_profitable_exit(pnl, price, q, params)):
                    session.place_order(category="spot", symbol=sym, side="Sell", orderType="Market", qty=str(q))
                    log_trade(sym, "SELL", price, q, pnl, "🟢 TP/RSI/трейл")
                else:
                    pos["tp"] = calc_adaptive_tp(price, atr, q, params)
                    pos["peak_price"] = peak
                    new_positions.append(pos)

            state["positions"] = new_positions

            if sig == "buy" and phase != "bear":
                qty = calc_adaptive_qty(usdt, atr, price, sym, params["risk_pct"])
                if qty:
                    tp = calc_adaptive_tp(price, atr, qty, params)
                    profit = (tp - price) * qty - price * qty * 0.001
                    if profit >= params["min_profit_usd"]:
                        session.place_order(category="spot", symbol=sym, side="Buy", orderType="Market", qty=str(qty))
                        state["positions"].append({"buy_price": price, "qty": qty, "tp": tp, "peak_price": price})
                        state["count"] += 1
                        log_trade(sym, "BUY", price, qty, 0, reason)
                    else:
                        log(f"[{sym}] ❌ Пропуск — прибыль < мин")
                else:
                    log(f"[{sym}] ❌ Пропуск — qty не вычислен")

            if CYCLE_COUNT % 30 == 0:
                cb = get_coin_balance(sym)
                if cb:
                    log(f"[{sym}] 📦 Баланс: {cb:.2f} ({cb * price:.2f} USDT)")
        except Exception as e:
            log(f"[{sym}] ❌ Ошибка: {type(e).__name__} — {e}")

    save_state()

def daily_report():
    global LAST_REPORT_DATE
    now = datetime.datetime.now()
    if now.hour == 22 and LAST_REPORT_DATE != now.date():
        total_pnl = sum(STATE[s]["pnl"] for s in SYMBOLS)
        rep = f"📊 Отчёт за {now.date()}:\n\nМонета     | Сделок  | PnL\n" + "-" * 30 + "\n"
        for sym, data in STATE.items():
            rep += f"{sym:<10} | {data['count']:<7} | {data['pnl']:.2f} USDT\n"
        rep += "-" * 30 + f"\n💰 Баланс: {get_balance():.2f} USDT\n📈 Общий PnL: {total_pnl:.2f} USDT"
        send_tg(rep)
        for sym in SYMBOLS:
            STATE[sym]["count"] = 0
            STATE[sym]["pnl"] = 0.0
        save_state()
        LAST_REPORT_DATE = now.date()

def main():
    send_tg("🚀 Бот запущен. Объём фильтр 0.3, TP×1.8, риск 3%.")
    log("🚀 Бот запущен. Объём фильтр 0.3, TP×1.8, риск 3%.")
    while True:
        try:
            trade()
            daily_report()
        except Exception as e:
            log(f"❌ Главная ошибка: {type(e).__name__} — {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()




