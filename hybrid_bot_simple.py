# =========================
#  VladBigMoney v3.2 (spot)
#  bybit spot / redis / telegram
# =========================
import os, time, math, json, hmac, hashlib, logging, threading, random, datetime as dt
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple, List

import requests
import redis

# ---------- CONFIG ----------
API_KEY     = os.getenv("BYBIT_API_KEY",     "YOUR_KEY")
API_SECRET  = os.getenv("BYBIT_API_SECRET",  "YOUR_SECRET")
BYBIT_HOST  = os.getenv("BYBIT_HOST",        "https://api.bybit.com")
TG_TOKEN    = os.getenv("TG_TOKEN",          "YOUR_TELEGRAM_BOT_TOKEN")
TG_CHAT     = int(os.getenv("TG_CHAT",       "123456789"))

REDIS_URL   = os.getenv("REDIS_URL",         "redis://localhost:6379/0")

SYMBOLS     = ["TONUSDT", "DOGEUSDT", "XRPUSDT"]  # по просьбе — только эти
TAKER_FEE   = float(os.getenv("TAKER_FEE", "0.0010"))  # 0.0010 или 0.0018
BUDGET_MIN  = 150.0
BUDGET_MAX  = 230.0
RESERVE_USDT= 10.0    # держим "на воздух"
MIN_NET_PNL_USD = 1.0 # минимальная чистая прибыль, при которой разрешаем продажу
SL_PCT      = 0.03    # стоп-лосс от средней цены позиции (3%)
TRAIL_X     = 1.5     # коэффициент агрессивности трейлинга TP
DCA_MAX_DD  = 0.15    # макс. допустимая просадка для усреднения (15% от средней)
DCA_COOLDOWN_SEC = 45
TRADE_COOLDOWN_SEC = 30
ERROR_COOLDOWN_SEC = 30
KLINE_INTERVAL = "1"  # 1m
KLINE_LIMIT    = 120  # на индикаторы

DAILY_REPORT_HHMM = "01:30"  # время отчёта, локальное
SESSION_TAG = "v3.2"

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S,%f"
)
log = logging.getLogger("trader")

# ---------- TELEGRAM ----------
def tg_send(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        log.warning(f"TG send fail: {e}")

def tg_ok(text): tg_send(f"✅ {text}")
def tg_err(text): tg_send(f"⚠️ {text}")
def tg_info(text): tg_send(text)

# ---------- REDIS ----------
rds = redis.from_url(REDIS_URL)

def rkey(sym, field): return f"vlad:{SESSION_TAG}:{sym}:{field}"

# ---------- HTTP utils ----------
def _ts_ms(): return str(int(time.time()*1000))

def sign(params: Dict[str,str], secret: str):
    # for Bybit v5 GET/POST with query/body sorted
    qs = "&".join([f"{k}={params[k]}" for k in sorted(params.keys()) if params[k] is not None])
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

# ---------- BYBIT CLIENT (минимально необходимое) ----------
class BybitClient:
    def __init__(self, host, key, secret):
        self.host, self.key, self.secret = host, key, secret
        self.sess = requests.Session()
        self.sess.headers.update({"X-BAPI-API-KEY": key})

    def _auth(self, params):
        params["api_key"] = self.key
        params["timestamp"] = _ts_ms()
        sign_ = sign(params, self.secret)
        params["sign"] = sign_
        return params

    def get_wallet_balance(self) -> Dict[str, float]:
        # unified spot balance (v5)
        path = "/v5/account/wallet-balance"
        params = self._auth({"accountType": "UNIFIED"})
        try:
            resp = self.sess.get(self.host+path, params=params, timeout=10)
            data = resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"{data.get('retCode')}:{data.get('retMsg')}")
            usdt = 0.0
            coins = {}
            for acc in data["result"]["list"]:
                for c in acc["coin"]:
                    sym = c["coin"].upper()
                    free = float(c["walletBalance"])
                    coins[sym] = coins.get(sym, 0.0) + free
                    if sym == "USDT": usdt += free
            return {"USDT": usdt, **coins}
        except Exception as e:
            raise

    def get_spot_limits(self, symbol: str) -> Tuple[float,float,float]:
        # min_qty, qty_step, min_amt (bybit v5 instrument)
        path = "/v5/market/instruments-info"
        try:
            resp = self.sess.get(self.host+path, params={"category":"spot","symbol":symbol}, timeout=10)
            js = resp.json()
            if js.get("retCode")!=0: raise RuntimeError(js.get("retMsg"))
            i = js["result"]["list"][0]
            lot = i["lotSizeFilter"]
            pr  = i["priceFilter"]
            min_qty   = float(lot["minOrderQty"])
            qty_step  = float(lot["basePrecision"])
            min_amt   = float(lot["minOrderAmt"])
            return min_qty, qty_step, min_amt
        except Exception as e:
            # fallback
            return 0.0, 1.0, 5.0

    def klines(self, symbol: str, interval="1", limit=120):
        path = "/v5/market/kline"
        try:
            resp = self.sess.get(self.host+path, params={"category":"spot","symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
            js = resp.json()
            if js.get("retCode")!=0: raise RuntimeError(js.get("retMsg"))
            # return list of [start, open, high, low, close, volume, turnover]
            rows = js["result"]["list"]
            rows.sort(key=lambda x:int(x[0]))
            closes = [float(x[4]) for x in rows]
            return closes
        except Exception as e:
            raise

    def last_price(self, symbol:str)->float:
        path = "/v5/market/tickers"
        resp = self.sess.get(self.host+path, params={"category":"spot","symbol":symbol}, timeout=10)
        js = resp.json()
        if js.get("retCode")!=0: raise RuntimeError(js.get("retMsg"))
        return float(js["result"]["list"][0]["lastPrice"])

    def order_market(self, symbol: str, side: str, qty: float) -> Dict:
        path = "/v5/order/create"
        body = {
            "category":"spot",
            "symbol":symbol,
            "side":side.title(),
            "orderType":"Market",
            "qty":str(qty),
            "timestamp":_ts_ms()
        }
        body["api_key"]=self.key
        body["sign"]=sign(body, self.secret)
        try:
            resp = self.sess.post(self.host+path, json=body, timeout=10)
            js = resp.json()
            if js.get("retCode")!=0:
                raise RuntimeError(f"{js.get('retCode')}:{js.get('retMsg')}")
            return js
        except Exception as e:
            raise

# ---------- INDICATORS ----------
def ema(series: List[float], n:int)->List[float]:
    k = 2/(n+1)
    out = []
    s=None
    for x in series:
        s = x if s is None else (x - s)*k + s
        out.append(s)
    return out

def rsi(series: List[float], n=14)->List[float]:
    gains, losses = [], []
    for i in range(1,len(series)):
        d=series[i]-series[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    rs= []
    avg_g = sum(gains[:n])/n if len(gains)>=n else 0
    avg_l = sum(losses[:n])/n if len(losses)>=n else 0
    rs_i = (avg_g/(avg_l+1e-9))
    rs_val = 100 - 100/(1+rs_i)
    out=[50]*(len(series)-len(gains)) + [50]*n + [rs_val]
    for i in range(n+1,len(series)):
        g = gains[i-1]; l=losses[i-1]
        avg_g = (avg_g*(n-1)+g)/n
        avg_l = (avg_l*(n-1)+l)/n
        rs_i = (avg_g/(avg_l+1e-9))
        rs_val = 100 - 100/(1+rs_i)
        out.append(rs_val)
    return out

def macd(series: List[float], fast=12, slow=26, sig=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = [a-b for a,b in zip(ema_fast,ema_slow)]
    signal = ema(macd_line, sig)
    hist = [a-b for a,b in zip(macd_line, signal)]
    return macd_line, signal, hist

# ---------- STATE ----------
@dataclass
class Position:
    qty: float = 0.0
    avg_price: float = 0.0
    tp: float = 0.0
    last_trade_ts: float = 0.0
    last_error_ts: float = 0.0

def load_pos(sym)->Position:
    raw = rds.get(rkey(sym,"pos"))
    if not raw: return Position()
    d=json.loads(raw)
    return Position(**d)

def save_pos(sym, pos:Position):
    rds.set(rkey(sym,"pos"), json.dumps(asdict(pos)))

# ---------- UTILS ----------
def round_step(value: float, step: float)->float:
    if step<=0: return value
    return math.floor(value/step)*step

def net_pnl_usd(price: float, pos: Position)->float:
    # чистая прибыль по позиции в USDT (учитывая обе стороны комиссии)
    if pos.qty<=0: return 0.0
    gross = (price - pos.avg_price) * pos.qty
    fees  = (pos.avg_price + price) * pos.qty * TAKER_FEE
    return gross - fees

def calc_tp(avg: float)->float:
    # базовый TP ~ +3% от средней (можно подвинуть)
    base = avg * (1.0 + 0.03)
    return base

def trail_tp(tp: float, price: float, avg: float)->float:
    # если цена ушла выше (tp+delta), подтягиваем TP
    # delta = (price-avg)/TRAIL_X
    delta = max(0.0, (price - avg) / TRAIL_X)
    return max(tp, avg + delta)

def affordable_qty(usdt_free: float, price: float, min_qty: float, qty_step: float, min_amt: float, want_budget: float)->float:
    budget = min(usdt_free - RESERVE_USDT, want_budget)
    if budget <= 0: return 0.0
    qty = budget / price
    qty = max(qty, min_qty)
    qty = round_step(qty, qty_step)
    if qty*price < min_amt: # подгоняем до минимальной суммы
        qty = round_step(min_amt/price, qty_step)
    return max(0.0, qty)

def too_soon(ts: float, cooldown: int)->bool:
    return (time.time() - ts) < cooldown

# ---------- STRATEGY ----------
def build_signal(closes: List[float])->Tuple[str,float,Dict[str,float]]:
    if len(closes)<35: return "NONE", 0.0, {}
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    r   = rsi(closes, 14)
    m, s, h = macd(closes, 12, 26, 9)
    e9v, e21v = e9[-1], e21[-1]
    rsiv = r[-1]
    macdv, sigv = m[-1], s[-1]
    # 2 из 3
    buy_votes  = 0
    sell_votes = 0
    if e9v>e21v: buy_votes+=1
    if e9v<e21v: sell_votes+=1
    if rsiv>=55: buy_votes+=1
    if rsiv<=45: sell_votes+=1
    if macdv>sigv: buy_votes+=1
    if macdv<sigv: sell_votes+=1

    signal="NONE"; conf=0.0
    if buy_votes>=2 and sell_votes==0: signal="BUY"; conf=buy_votes/3
    elif sell_votes>=2 and buy_votes==0: signal="SELL"; conf=sell_votes/3

    details = {"EMA9":round(e9v,6),"EMA21":round(e21v,6),"RSI":round(rsiv,2),"MACD":round(macdv,6),"SIG":round(sigv,6)}
    return signal, conf, details

# ---------- MAIN LOOP WORKER ----------
class Trader:
    def __init__(self, client: BybitClient):
        self.c = client
        self.limits: Dict[str, Tuple[float,float,float]] = {}
        self.cooldowns: Dict[str, float] = {}

    def load_limits(self):
        for s in SYMBOLS:
            self.limits[s] = self.c.get_spot_limits(s)
        log.info(f"Loaded limits: { {s:{'min_qty':self.limits[s][0],'qty_step':self.limits[s][1],'min_amt':self.limits[s][2]} for s in SYMBOLS} }")

    def daily_report_daemon(self):
        while True:
            try:
                hh,mm = map(int, DAILY_REPORT_HHMM.split(":"))
                now = dt.datetime.now()
                tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if tgt<=now: tgt += dt.timedelta(days=1)
                time.sleep((tgt-now).total_seconds())
                self.send_daily()
            except Exception as e:
                time.sleep(60)

    def send_daily(self):
        try:
            bal = self.c.get_wallet_balance()
            usdt = bal.get("USDT",0.0)
            lines = [f"📊 Daily Report {dt.datetime.now():%Y-%m-%d}", f"USDT: {usdt:.2f}"]
            for sym in SYMBOLS:
                base = sym.replace("USDT","")
                qty = bal.get(base,0.0)
                pos = load_pos(sym)
                # приблизительный pnl для текущей цены
                price = self.c.last_price(sym)
                pnl = net_pnl_usd(price, pos)
                dd = 0.0
                if pos.qty>0:
                    dd = max(0.0, (pos.avg_price-price)/pos.avg_price)
                lines.append(f"{sym}: balance={qty:.6f}, trades=?, pnl={pnl:.2f}, maxDD~{dd*100:.2f}%, curPosQty={pos.qty:.6f}")
            tg_info("\n".join(lines))
        except Exception as e:
            tg_err(f"Daily report fail: {e}")

    def maybe_buy(self, sym: str, price: float, signal: str, conf: float, pos: Position, usdt_free: float):
        if signal!="BUY":
            log.info(f"[{sym}] 🔶 Нет покупки: сигнал {signal.lower()}, confidence={conf:.2f}")
            return
        if too_soon(pos.last_trade_ts, TRADE_COOLDOWN_SEC):
            log.info(f"[{sym}] ⏳ Cooldown после сделки")
            return

        min_qty, step, min_amt = self.limits[sym]
        want_budget = random.uniform(BUDGET_MIN, BUDGET_MAX)
        qty = affordable_qty(usdt_free, price, min_qty, step, min_amt, want_budget)
        if qty<=0:
            log.info(f"[{sym}] 🔶 Не покупаем: afford-qty=0 (free={usdt_free:.2f})")
            return

        # усреднение? — если уже есть позиция и просадка <= DCA_MAX_DD и 2/3 за buy
        if pos.qty>0:
            dd = (pos.avg_price - price)/pos.avg_price
            if dd<=0 or dd > DCA_MAX_DD:
                log.info(f"[{sym}] 🔶 Не усредняем: drawdown {dd:.4f} вне (<=0 .. {DCA_MAX_DD})")
                return
            if too_soon(pos.last_trade_ts, DCA_COOLDOWN_SEC):
                log.info(f"[{sym}] ⏳ Cooldown усреднения")
                return

        try:
            res = self.c.order_market(sym, "Buy", qty)
            # пересчёт средней/qty
            cost = qty*price
            fee  = qty*price*TAKER_FEE
            new_qty = pos.qty + qty
            new_avg = (pos.avg_price*pos.qty + cost + fee) / max(1e-9,new_qty)
            if pos.qty<=0:
                pos.tp = calc_tp(new_avg)
            pos.qty = new_qty
            pos.avg_price = new_avg
            pos.last_trade_ts = time.time()
            save_pos(sym, pos)

            log.info(f"[{sym}] | BUY @ {price:.6f}, qty={qty:.8f}, USDT={cost:.2f}, conf={conf:.2f}, budget={want_budget:.2f}, qty_net≈{qty:.8f}")
            tg_ok(f"{sym} BUY @ {price:.6f}, qty={qty:.8f}")
        except Exception as e:
            pos.last_error_ts = time.time()
            save_pos(sym, pos)
            msg = str(e)
            # компактная ошибка в TG
            tg_err(f"[{sym}] err: {msg}")

    def maybe_sell(self, sym: str, price: float, signal: str, conf: float, pos: Position):
        if pos.qty<=0:
            log.info(f"[{sym}] 🔶 Нет продажи: позиция пуста")
            return

        # стоп-лосс
        sl_price = pos.avg_price*(1.0 - SL_PCT)
        tp_trail_before = pos.tp
        pos.tp = trail_tp(pos.tp, price, pos.avg_price)

        want_sell = False
        reason = ""
        if price <= sl_price:
            want_sell = True; reason = f"SL hit {sl_price:.6f}"
        elif price >= pos.tp:
            want_sell = True; reason = f"TP {pos.tp:.6f}"
        elif net_pnl_usd(price, pos) >= MIN_NET_PNL_USD and signal=="SELL":
            want_sell = True; reason = f"net_pnl >= {MIN_NET_PNL_USD:.2f}"

        if not want_sell:
            # подробный лог почему не продаём
            net = net_pnl_usd(price, pos)
            log.info(f"[{sym}] 📈 TP trail: {tp_trail_before:.6f} → {pos.tp:.6f} | не продаём: цена {price:.6f} < TP {pos.tp:.6f} или net {net:.2f} < {MIN_NET_PNL_USD:.1f}")
            return

        if too_soon(pos.last_trade_ts, TRADE_COOLDOWN_SEC):
            log.info(f"[{sym}] ⏳ Cooldown после сделки (sell)")
            return

        try:
            qty = round_step(pos.qty, self.limits[sym][1])
            res = self.c.order_market(sym, "Sell", qty)
            pnl = net_pnl_usd(price, pos)
            log.info(f"[{sym}] | SELL @ {price:.6f}, qty={qty:.8f}, pnl={pnl:.2f} | reason={reason}")
            tg_ok(f"{sym} SELL @ {price:.6f}, qty={qty:.8f}, pnl={pnl:.2f}")
            # обнуляем позицию
            pos.qty = 0.0; pos.avg_price=0.0; pos.tp=0.0
            pos.last_trade_ts = time.time()
            save_pos(sym, pos)
        except Exception as e:
            pos.last_error_ts = time.time()
            save_pos(sym, pos)
            tg_err(f"[{sym}] err: {e}")

    def step_symbol(self, sym: str, usdt_free: float):
        # текущая цена
        price = self.c.last_price(sym)
        pos = load_pos(sym)

        # индикаторы
        closes = self.c.klines(sym, KLINE_INTERVAL, KLINE_LIMIT)
        sig, conf, det = build_signal(closes)

        # консольный лог детальный
        bal_base = 0.0
        try:
            bal = self.c.get_wallet_balance()
            bal_base = bal.get(sym.replace("USDT",""),0.0)
        except: pass
        log.info(f"[{sym}] 🔎 Сигнал={sig} (conf={conf:.2f}), price={price:.6f}, balance={bal_base:.8f} "
                 f"| EMA9={det.get('EMA9')}, EMA21={det.get('EMA21')}, RSI={det.get('RSI'):.2f}, MACD={det.get('MACD')}, SIG={det.get('SIG')}")

        if sig=="SELL":
            self.maybe_sell(sym, price, sig, conf, pos)
        elif sig=="BUY":
            self.maybe_buy(sym, price, sig, conf, pos, usdt_free)
        else:
            log.info(f"[{sym}] 🔶 Нет действия: сигнал none, confidence={conf:.2f}")

    def run(self):
        # старт
        tg_info(f"🚀 Старт бота. Восстановление состояния: { 'FRESH' if all(load_pos(s).qty==0 for s in SYMBOLS) else 'REDIS' }")
        tg_info(f"⚙️ Параметры: TAKER_FEE={TAKER_FEE:.4f}, BUDGET=[{BUDGET_MIN:.1f};{BUDGET_MAX:.1f}] TRAILx={TRAIL_X}, SL={SL_PCT*100:.2f}%")
        # лимиты
        self.load_limits()

        # daily report
        threading.Thread(target=self.daily_report_daemon, daemon=True).start()

        while True:
            try:
                # баланс
                wb = self.c.get_wallet_balance()
                usdt = wb.get("USDT",0.0)
                log.info(f"💰 Баланс USDT={usdt:.2f} | Доступно≈{max(0.0, usdt-RESERVE_USDT):.2f}")

                for sym in SYMBOLS:
                    self.step_symbol(sym, usdt)

                time.sleep(60)
            except Exception as e:
                log.error(f"loop error: {e}")
                tg_err(f"loop: {e}")
                time.sleep(ERROR_COOLDOWN_SEC)

# ---------- ENTRY ----------
if __name__ == "__main__":
    try:
        client = BybitClient(BYBIT_HOST, API_KEY, API_SECRET)
        Trader(client).run()
    except Exception as e:
        tg_err(f"fatal: {e}")
        raise
