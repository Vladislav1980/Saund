import os, time, math, json, datetime, logging, requests
import pandas as pd
from dotenv import load_dotenv
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from pybit.unified_trading import HTTP

# === Конфигурация ===
DEBUG = False
SYMBOLS = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]
STATE_PATH = "state.json"

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DEFAULT_PARAMS = {
    "risk_pct": 0.05,
    "tp_multiplier": 1.3,
    "trailing_stop_pct": 0.02,
    "max_drawdown_sl": 0.06,
    "min_profit_usdt": 1.5,
    "avg_rebuy_drop_pct": 0.07,
    "rebuy_cooldown_secs": 3600,
    "volume_filter": False
}

RESERVE_BALANCE = 0
DAILY_LOSS_LIMIT = -50
MAX_POS_USDT = 100

# === Telegram ===
def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        log(f"TG error: {e}")

def send_state_to_telegram(filepath):
    try:
        with open(filepath, 'rb') as f:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
            files = {'document': f}
            data = {'chat_id': CHAT_ID}
            requests.post(url, files=files, data=data)
    except Exception as e:
        print(f"❌ Ошибка при отправке state.json: {e}")

def download_state_from_telegram():
    try:
        print("\U0001F4E5 Загрузка state.json из Telegram...")
        res = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates").json()
        docs = []
        for update in res.get("result", []):
            msg = update.get("message", {})
            doc = msg.get("document")
            if doc and doc.get("file_name") == "state.json" and msg.get("chat", {}).get("id") == int(CHAT_ID):
                docs.append((msg["date"], doc["file_id"]))
        if not docs:
            print("❌ state.json не найден в Telegram")
            return
        docs.sort(reverse=True)
        latest_file_id = docs[0][1]
        file_info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile?file_id={latest_file_id}").json()
        file_path = file_info['result']['file_path']
        r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}")
        with open(STATE_PATH, 'wb') as f:
            f.write(r.content)
        print("✅ Загружен state.json из Telegram")
    except Exception as e:
        print(f"⚠️ Ошибка загрузки state.json из Telegram: {e}")

# === Логгирование ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
def log(msg):
    try:
        logging.info(msg)
    except UnicodeEncodeError:
        logging.info(msg.encode('unicode_escape').decode())

# === API ===
session = HTTP(api_key=API_KEY, api_secret=API_SECRET, recv_window=15000)
LIMITS = {}

def load_limits():
    for it in session.get_instruments_info(category="spot")["result"]["list"]:
        s = it["symbol"]
        if s in SYMBOLS:
            f = it.get("lotSizeFilter", {})
            LIMITS[s] = {
                "step": float(f.get("qtyStep", 1)),
                "min_amt": 5.0,
                "max_amt": float(it.get("maxOrderAmt", 1e9))
            }

def get_balance():
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == "USDT":
                    return float(c["walletBalance"])
    except: pass
    return 0

def get_coin_balance(sym):
    coin = sym.replace("USDT", "")
    try:
        for w in session.get_wallet_balance(accountType="UNIFIED")["result"]["list"]:
            for c in w["coin"]:
                if c["coin"] == coin:
                    return float(c["walletBalance"])
    except: pass
    return 0

def get_klines(sym, interval="5", limit=100):
    try:
        r = session.get_kline(category="spot", symbol=sym, interval=interval, limit=limit)
        return pd.DataFrame(r["result"]["list"], columns=["ts","o","h","l","c","vol","turn"]).astype(float)
    except Exception as e:
        log(f"klines err {sym}@{interval}: {e}")
        return pd.DataFrame()

def adjust(qty, step):
    e = int(f"{step:e}".split("e")[-1])
    return math.floor(qty * 10**abs(e)) / 10**abs(e)

def signal(df):
    df["ema9"] = EMAIndicator(df["c"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["c"], 21).ema_indicator()
    df["rsi"] = RSIIndicator(df["c"]).rsi()
    m = MACD(df["c"])
    df["macd"] = m.macd(); df["macd_s"] = m.macd_signal()
    df["atr"] = AverageTrueRange(df["h"], df["l"], df["c"]).average_true_range()
    df["vol_ch"] = df["vol"].pct_change().fillna(0)
    return df

# === Работа с состоянием ===
STATE = {}
download_state_from_telegram()
if os.path.exists(STATE_PATH):
    try:
        with open(STATE_PATH, "r") as f:
            STATE = json.load(f)
    except: STATE = {}
else:
    STATE = {}

for s in SYMBOLS:
    STATE.setdefault(s, {"pos": None, "count": 0, "pnl": 0.0})

def save_state():
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(STATE, f, indent=2)
        send_state_to_telegram(STATE_PATH)
    except Exception as e:
        print(f"❌ Ошибка сохранения state.json: {e}")

# === Весовые коэффициенты ===
def calculate_weights(dfs):
    weights = {}; total = 0
    for sym, df in dfs.items():
        score = 1.0
        if df["15"]["ema9"] > df["15"]["ema21"]: score += 0.2
        if df["60"]["ema9"] > df["60"]["ema21"]: score += 0.2
        if df["240"]["ema9"] > df["240"]["ema21"]: score += 0.2
        if 50 < df["5"]["rsi"] < 65: score += 0.2
        weights[sym] = score
        total += score
    for sym in weights:
        weights[sym] /= total
    return weights

# === Блок предотвращения спама при запуске ===
def send_startup_message():
    try:
        flag = "launch.lock"
        if os.path.exists(flag):
            with open(flag, "r") as f:
                last = datetime.datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
                if datetime.datetime.utcnow() - last < datetime.timedelta(minutes=15):
                    log("⏳ Bot уже запущен недавно, уведомление не отправлено")
                    return
        with open(flag, "w") as f:
            f.write(datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        send_tg("🚀 Bot запущен")
    except Exception as e:
        log(f"Ошибка уведомления запуска: {e}")

# === Главная функция ===
def trade():
    ... # (оставь текущую реализацию без изменений)

def daily_report():
    ... # (оставь текущую реализацию без изменений)

def main():
    log("🚀 Bot запущен")
    send_startup_message()
    while True:
        trade()
        daily_report()
        time.sleep(60)

if __name__ == "__main__":
    main()
