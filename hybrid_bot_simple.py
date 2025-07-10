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
    "COMPUSDT","NEARUSDT","TONUSDT","TRXUSDT","XRPUSDT",
    "ADAUSDT","BCHUSDT","LTCUSDT","AVAXUSDT",
    "WIFUSDT","ARBUSDT","SUIUSDT","FILUSDT"
]

DEFAULT_PARAMS = {
    "risk_pct": 0.01,
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
MAX_POSITION_SIZE_USDT = 50

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

def calc_adaptive_tp(price, atr, qty, params, is_short=False):
    fee = price * qty * 0.001
    if is_short:
        base_tp = price - params["tp_multiplier"] * atr
        tp_min = price - (params["min_profit_usd"] + fee) / qty
        return min(base_tp, tp_min)
    else:
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
        content = open("state.json", "r").read().strip()
        STATE = json.loads(content) if content else {}
        for sym in STATE:
            for p in STATE[sym].get("positions", []):
                p.setdefault("peak_price", p["buy_price"])
                log(f"[{sym}] 🔄 Восстановлена позиция: qty={p['qty']}, price={p['buy_price']}")
    except Exception as e:
        log(f"❌ state.json error: {e}")
        STATE = {}

for s in SYMBOLS:
    STATE.setdefault(s, {
        "positions": [],
        "shorts": [],  # ← добавлено для шортов
        "pnl": 0.0,
        "count": 0,
        "avg_count": 0,
        "volume_total": 0.0,
        "last_avg_time": 0
    })

def save_state():
    try:
        out = {str(k): v for k, v in STATE.items()}
        with open("state.json", "w") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        log(f"❌ save_state error: {e}")
