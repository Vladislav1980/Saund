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
