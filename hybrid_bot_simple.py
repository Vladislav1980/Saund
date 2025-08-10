#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bybit Spot maker-bot (DOGEUSDT, XRPUSDT)
- Maker-лимитный вход (PostOnly) с "перекатом" каждые 45 сек до исполнения
- TP (maker limit PostOnly), STOP — клиентский (market, reserve: limit-IOC)
- Комиссии: maker 0.10%, taker 0.18% (настраиваются)
- Чистая прибыль >= $1 до размещения заявки (для TP-сценария)
- Жёсткое округление по tick_size/step_size
- Телеграм-уведомления (старт/вход/перекат/исполнение/TP/STOP/ошибки)
- Подробный лог причин пропуска сделок
"""

import os
import time
import json
import math
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
import requests
from urllib.parse import urlencode

# =======================
# Конфигурация
# =======================

SYMBOLS = ["XRPUSDT", "DOGEUSDT"]         # Две более волатильные
BASE_URL = "https://api.bybit.com"
HTTP_TIMEOUT = 10

# Комиссии (твои)
MAKER_FEE = 0.0010     # 0.10%
TAKER_FEE = 0.0018     # 0.18%

# Риск‑параметры
MAX_PER_TRADE_USD = 35.75     # на символ при наличии свободного USDT
MIN_NET_PROFIT_USD = 1.00     # чистыми после комиссий (для TP сценария)
POSTONLY_REPRICE_SEC = 45     # "перекат" лимитника
ORDER_TTL_SEC = 10 * 60       # максимум 10 минут ждать вход
TP_ATR_MULT_BASE = 1.30       # базовый множитель; может повышаться на низкой воле
TP_ATR_MULT_HIGH = 1.55
TP_SWITCH_ATR_PCT = 0.003     # если 15m ATR% < 0.3% — берём больший мультипликатор

# STOP: клиентский
STOP_ATR_K = 1.2              # стоп отступ: max(STOP_MIN_PCT, STOP_ATR_K * ATR5m)
STOP_MIN_PCT = 0.004          # 0.4% минимум
RESERVE_LIMIT_PCT = 0.002     # если market-недоступен, limit-IOC по bid*(1-0.2%)

# Сигналы: чуть ослабленные фильтры
RSI_BUY_LOW, RSI_BUY_HIGH = 30, 75
EMA_LOOKBACK_FAST = 9
EMA_LOOKBACK_SLOW = 21
KL_INTERVAL = "15"            # минутки для показателей
HISTORY_MINUTES = 120

# Telegram
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# API ключи Bybit (spot)
BYBIT_KEY = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET = os.getenv("BYBIT_SECRET", "")

# Файл состояния (позиции/открытые ордера)
STATE_FILE = "state.json"

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S,%f",
)
log = logging.getLogger("bot")

# =======================
# Утилиты
# =======================

def ts_ms() -> int:
    return int(time.time() * 1000)

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step

def round_tick(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return math.floor(x / tick + 1e-9) * tick

def to_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        log.warning(f"TG send fail: {e}")

# =======================
# HTTP / Bybit
# =======================

session = requests.Session()

def sign(params: Dict[str, Any]) -> str:
    query = urlencode(sorted(params.items()))
    return hmac.new(BYBIT_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def private_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = BASE_URL + path
    body["api_key"] = BYBIT_KEY
    body["timestamp"] = ts_ms()
    body["recv_window"] = 5000
    body["sign"] = sign(body)
    r = session.post(url, data=body, timeout=HTTP_TIMEOUT)
    return r.json()

def public_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = BASE_URL + path
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    return r.json()

# =======================
# Данные биржи: фильтры/тик/шаг
# =======================

class SymbolInfo:
    def __init__(self, symbol: str, tick: float, step: float, min_qty: float, min_notional: float):
        self.symbol = symbol
        self.tick = tick
        self.step = step
        self.min_qty = min_qty
        self.min_notional = min_notional

SYMINFOS: Dict[str, SymbolInfo] = {}

def load_filters():
    # Bybit v5 spot instruments
    res = public_get("/v5/market/instruments-info", {"category": "spot"})
    if res.get("retCode") != 0:
        raise RuntimeError(f"load_filters ret={res}")
    for it in res["result"]["list"]:
        s = it["symbol"]
        if s not in SYMBOLS:
            continue
        tick = to_float(it.get("priceFilter", {}).get("tickSize", "0"))
        step = to_float(it.get("lotSizeFilter", {}).get("basePrecision", "0"))
        min_qty = to_float(it.get("lotSizeFilter", {}).get("minOrderQty", "0"))
        min_notional = to_float(it.get("lotSizeFilter", {}).get("minOrderAmt", "0"))
        SYMINFOS[s] = SymbolInfo(s, tick, step, min_qty, min_notional)
    if set(SYMBOLS) - set(SYMINFOS.keys()):
        miss = list(set(SYMBOLS) - set(SYMINFOS.keys()))
        raise RuntimeError(f"filters missing: {miss}")

# =======================
# Ордербук / свечи
# =======================

def get_best_bid_ask(symbol: str) -> Tuple[float, float]:
    # v5 orderbook
    r = public_get("/v5/market/orderbook", {"category": "spot", "symbol": symbol, "limit": 1})
    if r.get("retCode") != 0:
        raise RuntimeError(f"orderbook {symbol} ret={r}")
    li = r["result"]["a"]  # asks
    lb = r["result"]["b"]  # bids
    # В v5 это массивы строк [price, qty]
    best_ask = to_float(li[0][0]) if li else 0.0
    best_bid = to_float(lb[0][0]) if lb else 0.0
    return best_bid, best_ask

def get_klines(symbol: str, minutes: int) -> List[Dict[str, float]]:
    limit = max(EMA_LOOKBACK_SLOW*3, minutes // int(KL_INTERVAL) + 5)
    r = public_get("/v5/market/kline", {
        "category": "spot",
        "symbol": symbol,
        "interval": KL_INTERVAL,
        "limit": limit
    })
    if r.get("retCode") != 0:
        raise RuntimeError(f"kline {symbol} ret={r}")
    out = []
    # Bybit kline list: [start, open, high, low, close, volume, turnover]
    for row in r["result"]["list"][::-1]:
        out.append({
            "ts": int(row[0]),
            "o": to_float(row[1]),
            "h": to_float(row[2]),
            "l": to_float(row[3]),
            "c": to_float(row[4]),
        })
    return out

# =======================
# Индикаторы
# =======================

def ema(values: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    out = []
    ma = sum(values[:period]) / period
    out.extend([None]*(period-1))
    out.append(ma)
    for v in values[period:]:
        ma = v * k + ma * (1 - k)
        out.append(ma)
    return out

def rsi(values: List[float], period: int = 14) -> List[float]:
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    # Wilder
    rsi_vals = [None]*len(values)
    if len(gains) < period:
        return rsi_vals
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi_vals[period] = 100 if avg_l == 0 else 100 - (100 / (1 + (avg_g / avg_l)))
    for i in range(period+1, len(values)):
        avg_g = (avg_g*(period-1) + gains[i-1]) / period
        avg_l = (avg_l*(period-1) + losses[i-1]) / period
        rsi_vals[i] = 100 if avg_l == 0 else 100 - (100 / (1 + (avg_g / avg_l)))
    return rsi_vals

def atr_percent(kl: List[Dict[str, float]], period: int = 14) -> float:
    trs = []
    for i in range(1, len(kl)):
        h = kl[i]["h"]; l = kl[i]["l"]; pc = kl[i-1]["c"]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    if not trs:
        return 0.0
    atr = sum(trs[-period:]) / min(period, len(trs))
    last_close = kl[-1]["c"]
    return atr / last_close  # относительный ATR

# =======================
# Состояние
# =======================

STATE = {
    "positions": {  # filled entry
        # "XRPUSDT": {"qty": 0.0, "entry": 0.0}
    },
    "working": {    # активные входы/TP ордера
        # "XRPUSDT": {"side":"Buy","orderId":"", "qty":0.0, "price":0.0, "tStart":ts, "route":"entry|tp"}
    }
}

def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        try:
            STATE = json.load(open(STATE_FILE, "r"))
        except Exception:
            pass

def save_state():
    try:
        json.dump(STATE, open(STATE_FILE, "w"))
    except Exception:
        pass

# =======================
# Расчёты прибыли / цели
# =======================

def min_tp_price_for_1usd(entry: float, qty: float) -> float:
    """
    Найти минимальную TP-цену, чтобы PnL >= $1 чистыми при maker→maker.
    PnL = qty*(tp*(1-m) - entry*(1+m)) >= 1
    tp >= (entry*(1+m) + 1/qty) / (1-m)
    """
    m = MAKER_FEE
    if qty <= 0:
        return float("inf")
    return (entry*(1+m) + 1.0/qty) / (1 - m)

# =======================
# Ордеры
# =======================

def place_limit_postonly(symbol: str, side: str, qty: float, price: float) -> Tuple[bool, str, str]:
    """Возвращает ok, orderId, err"""
    info = SYMINFOS[symbol]
    price = round_tick(price, info.tick)
    qty = max(qty, info.min_qty)
    qty = round_step(qty, info.step)
    if qty * price < info.min_notional * 1.001:
        return False, "", f"notional<{info.min_notional}"
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side.capitalize(),
        "orderType": "Limit",
        "timeInForce": "PostOnly",
        "qty": f"{qty:.8f}".rstrip('0').rstrip('.'),
        "price": f"{price:.12f}".rstrip('0').rstrip('.'),
    }
    r = private_post("/v5/order/create", body)
    if r.get("retCode") == 0:
        return True, r["result"]["orderId"], ""
    return False, "", f"{r.get('retCode')}:{r.get('retMsg')}"

def cancel_order(symbol: str, order_id: str) -> None:
    private_post("/v5/order/cancel", {
        "category": "spot", "symbol": symbol, "orderId": order_id
    })

def get_order(symbol: str, order_id: str) -> Dict[str, Any]:
    r = private_post("/v5/order/realtime", {
        "category": "spot", "symbol": symbol, "orderId": order_id
    })
    if r.get("retCode") != 0 or not r["result"]["list"]:
        return {}
    return r["result"]["list"][0]

def market_sell(symbol: str, qty: float) -> Tuple[bool, str]:
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": f"{qty:.8f}".rstrip('0').rstrip('.')
    }
    r = private_post("/v5/order/create", body)
    if r.get("retCode") == 0:
        return True, r["result"]["orderId"]
    return False, f"{r.get('retCode')}:{r.get('retMsg')}"

def limit_ioc_sell(symbol: str, qty: float, price: float) -> Tuple[bool, str]:
    info = SYMINFOS[symbol]
    price = round_tick(price, info.tick)
    qty = round_step(qty, info.step)
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Limit",
        "timeInForce": "IOC",
        "qty": f"{qty:.8f}".rstrip('0').rstrip('.'),
        "price": f"{price:.12f}".rstrip('0').rstrip('.'),
    }
    r = private_post("/v5/order/create", body)
    if r.get("retCode") == 0:
        return True, r["result"]["orderId"]
    return False, f"{r.get('retCode')}:{r.get('retMsg')}"

# =======================
# Сигналы/логика
# =======================

def compute_indicators(symbol: str) -> Dict[str, Any]:
    kl = get_klines(symbol, HISTORY_MINUTES)
    closes = [k["c"] for k in kl]
    e9 = ema(closes, EMA_LOOKBACK_FAST)
    e21 = ema(closes, EMA_LOOKBACK_SLOW)
    r = rsi(closes, 14)
    atr5 = atr_percent(kl[-20:], 14)     # ~5 последних свечей ~ 75 мин → ок для относительной волатильности
    atr15 = atr_percent(kl, 14)
    last = closes[-1]
    bb, aa = get_best_bid_ask(symbol)
    return {
        "price": last, "bid": bb, "ask": aa,
        "ema9": e9[-1], "ema21": e21[-1],
        "rsi": r[-1] if r else None,
        "atr5": atr5, "atr15": atr15
    }

def want_buy(sig: Dict[str, Any]) -> Tuple[bool, str]:
    # Чуть ослаблено: rsi в [30..75], ema9 >= ema21 или пересечение вверх
    if sig["rsi"] is None:
        return False, "RSI: none"
    if not (RSI_BUY_LOW <= sig["rsi"] <= RSI_BUY_HIGH):
        return False, f"RSI:{sig['rsi']:.2f} out [{RSI_BUY_LOW},{RSI_BUY_HIGH}]"
    if not (sig["ema9"] >= sig["ema21"]):
        return False, f"EMA9<{EMA_LOOKBACK_SLOW}"
    if sig["bid"] <= 0 or sig["ask"] <= 0:
        return False, "orderbook empty"
    return True, "ok"

def calc_tp_price(entry: float, qty: float, atr15: float) -> float:
    mult = TP_ATR_MULT_HIGH if atr15 < TP_SWITCH_ATR_PCT else TP_ATR_MULT_BASE
    # ATR‑цель, но не ниже цены, дающей >= $1 чистыми
    atr_target = entry * (1 + mult * atr15)
    req1 = min_tp_price_for_1usd(entry, qty)
    return max(atr_target, req1)

def place_tp(symbol: str, qty: float, price: float) -> Tuple[bool, str, str]:
    return place_limit_postonly(symbol, "Sell", qty, price)

def place_entry(symbol: str, free_usd: float, sig: Dict[str, Any]) -> None:
    """Разместить вход, включив профит‑чек >= $1"""
    info = SYMINFOS[symbol]
    bb, aa = sig["bid"], sig["ask"]
    if bb <= 0 or aa <= 0:
        log.info(f"{symbol}: DEBUG_SKIP | empty orderbook")
        return

    # Аллокация
    alloc = min(MAX_PER_TRADE_USD, free_usd)
    qty = alloc / bb
    qty = round_step(qty, info.step)
    if qty < info.min_qty:
        log.info(f"{symbol}: BUY-skip qty<{info.min_qty}")
        return
    entry_price = round_tick(bb, info.tick)

    # Чек чистой прибыли
    tp_try = calc_tp_price(entry_price, qty, sig["atr15"])
    est_pnl = qty * (tp_try * (1 - MAKER_FEE) - entry_price * (1 + MAKER_FEE))
    if est_pnl < MIN_NET_PROFIT_USD:
        log.info(f"{symbol} BUY-check qty={qty:.4f}, tp={tp_try:.6f}, est_pnl={est_pnl:.2f} < required {MIN_NET_PROFIT_USD:.2f}")
        return

    ok, oid, err = place_limit_postonly(symbol, "Buy", qty, entry_price)
    if not ok:
        log.info(f"{symbol}: entry place FAIL: {err}")
        return

    STATE["working"][symbol] = {
        "side": "Buy",
        "orderId": oid,
        "qty": qty,
        "price": entry_price,
        "tStart": time.time(),
        "route": "entry",
        "lastReprice": time.time()
    }
    save_state()
    msg = f"🟢 BUY placed (PostOnly) {symbol} @ {entry_price:.6f}, qty={qty:.4f}"
    log.info(msg); send_tg(msg)

def reprice_or_check_fill(symbol: str, sig: Dict[str, Any]):
    w = STATE["working"].get(symbol)
    if not w or w["route"] != "entry":
        return
    od = get_order(symbol, w["orderId"])
    status = (od.get("orderStatus") or "").lower()
    # filled?
    if status == "filled":
        # цена входа: возьмём averagePrice если доступно, иначе w["price"]
        fill_price = to_float(od.get("avgPrice") or w["price"])
        qty = to_float(od.get("cumExecQty") or w["qty"])
        STATE["positions"][symbol] = {"qty": qty, "entry": fill_price}
        # TP
        tp_price = round_tick(calc_tp_price(fill_price, qty, sig["atr15"]), SYMINFOS[symbol].tick)
        ok, tp_oid, err = place_tp(symbol, qty, tp_price)
        if ok:
            msg = f"🎯 TP placed {symbol} {qty:.4f} @ {tp_price:.6f}"
            STATE["working"][symbol] = {"side": "Sell", "orderId": tp_oid, "qty": qty, "price": tp_price, "tStart": time.time(), "route": "tp"}
        else:
            msg = f"⚠️ TP place FAIL {symbol}: {err}"
            STATE["working"].pop(symbol, None)
        save_state()
        log.info(msg); send_tg(msg)
        return

    # отмена/перестановка?
    if time.time() - w["tStart"] > ORDER_TTL_SEC:
        cancel_order(symbol, w["orderId"])
        STATE["working"].pop(symbol, None)
        save_state()
        msg = f"⏰ Entry TTL exceeded {symbol} — canceled"
        log.info(msg); send_tg(msg)
        return

    if time.time() - w["lastReprice"] >= POSTONLY_REPRICE_SEC:
        # перекат к новому bid
        try:
            bb, _ = get_best_bid_ask(symbol)
            newp = round_tick(bb, SYMINFOS[symbol].tick)
            if newp != w["price"]:
                cancel_order(symbol, w["orderId"])
                ok, oid, err = place_limit_postonly(symbol, "Buy", w["qty"], newp)
                if ok:
                    w["orderId"] = oid; w["price"] = newp; w["lastReprice"] = time.time()
                    save_state()
                    msg = f"🔁 Reprice {symbol} entry → {newp:.6f}"
                    log.info(msg); send_tg(msg)
                else:
                    msg = f"⚠️ Reprice FAIL {symbol}: {err}"
                    log.info(msg); send_tg(msg)
                    w["lastReprice"] = time.time()
        except Exception as e:
            log.info(f"{symbol} reprice error: {e}")

def monitor_tp_and_stop(symbol: str, sig: Dict[str, Any]):
    # TP filled?
    w = STATE["working"].get(symbol)
    pos = STATE["positions"].get(symbol)
    if w and w["route"] == "tp":
        od = get_order(symbol, w["orderId"])
        status = (od.get("orderStatus") or "").lower()
        if status == "filled":
            STATE["working"].pop(symbol, None)
            STATE["positions"].pop(symbol, None)
            save_state()
            msg = f"✅ TP filled {symbol} qty={w['qty']:.4f} @ ~{w['price']:.6f}"
            log.info(msg); send_tg(msg)

    # Клиентский STOP
    pos = STATE["positions"].get(symbol)
    if pos:
        entry = pos["entry"]; qty = pos["qty"]
        # рассчитать стоп уровень
        stop_level = entry * (1 - max(STOP_MIN_PCT, STOP_ATR_K * sig["atr5"]))
        # если bid пробил — срабатываем
        if sig["bid"] > 0 and sig["bid"] <= stop_level:
            msg = f"⛔ STOP trigger {symbol}: bid={sig['bid']:.6f} <= {stop_level:.6f} → market sell"
            log.info(msg); send_tg(msg)
            ok, oid = market_sell(symbol, qty)
            if not ok:
                # запасной: limit IOC чуть ниже bid
                backup = sig["bid"] * (1 - RESERVE_LIMIT_PCT)
                ok2, oid2 = limit_ioc_sell(symbol, qty, backup)
                if ok2:
                    msg2 = f"⛑ STOP backup IOC {symbol} @ {backup:.6f}"
                    log.info(msg2); send_tg(msg2)
                else:
                    msg2 = f"❌ STOP FAIL {symbol}: {oid} & {oid2}"
                    log.info(msg2); send_tg(msg2)
            # очистка локальной позиции
            STATE["positions"].pop(symbol, None)
            STATE["working"].pop(symbol, None)
            save_state()

# =======================
# Баланс USDT
# =======================

def get_usdt_balance() -> float:
    r = private_post("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if r.get("retCode") != 0:
        return 0.0
    for cur in r["result"]["list"]:
        for c in cur.get("coin", []):
            if c.get("coin") == "USDT":
                return to_float(c.get("availableToWithdraw") or c.get("availableToBorrow") or c.get("walletBalance") or 0)
    return 0.0

# =======================
# Основной цикл
# =======================

def boot_banner():
    # восстановление локального состояния (если есть позиции в ордерах — подтягиваем по факту в процессе)
    load_filters()
    load_state()

    # стартовое резюме
    pos_lines = []
    nominal = 0.0
    for s in SYMBOLS:
        p = STATE["positions"].get(s)
        if p:
            pos_lines.append(f"- {s}: синхр. позиция qty={p['qty']:.4f} по ~{p['entry']:.6f}")
            nominal += p["qty"] * (get_best_bid_ask(s)[0] or p["entry"])
    if not pos_lines:
        pos_lines = ["ℹ Начинаем с чистого состояния"]
    else:
        pos_lines.insert(0, "🚀 Бот запущен (восстановление позиций)")

    for ln in pos_lines:
        log.info(ln)
    log.info(f"📊 Номинал по монетам: ${nominal:.2f}")
    send_tg("\n".join([*pos_lines, f"📊 Номинал по монетам: ${nominal:.2f}"]))

    log.info("🟢 Бот работает. Maker‑режим, фильтры ослаблены, TP≥$1 чистыми.")

def main_loop():
    boot_banner()
    while True:
        try:
            free = get_usdt_balance()
            per_sym = min(MAX_PER_TRADE_USD, max(0.0, free/ max(1, len(SYMBOLS))))
            log.info(f"DEBUG avail={free:.2f}, per_sym={per_sym:.2f}, limits_ok=True")

            for s in SYMBOLS:
                sig = compute_indicators(s)

                # Лог состояния индикаторов
                atr_pct = sig["atr15"]
                tp_mult = TP_ATR_MULT_HIGH if atr_pct < TP_SWITCH_ATR_PCT else TP_ATR_MULT_BASE
                pos = STATE["positions"].get(s)
                log.info(
                    f"[{s}] sig={'none' if not pos else 'pos=1'}, "
                    f"price={sig['price']:.6f}, pos={'1' if pos else '0'} | "
                    f"bid={sig['bid']:.6f} ask={sig['ask']:.6f} | "
                    f"EMA9={sig['ema9']:.6f} EMA21={sig['ema21']:.6f} "
                    f"RSI={sig['rsi']:.2f} | ATR(5/15m)={sig['atr5']*100:.2f}%/{sig['atr15']*100:.2f}% | "
                    f"tp_mult={tp_mult:.2f}"
                )

                # если нет позиции и нет активного входа — пробуем запостить
                if not pos and not (STATE["working"].get(s) and STATE["working"][s]["route"] == "entry"):
                    ok_sig, why = want_buy(sig)
                    if ok_sig:
                        place_entry(s, per_sym, sig)
                    else:
                        log.info(f"{s}: DEBUG_SKIP | {why}")

                # если есть активный вход — проверяем fill/перекатываем
                reprice_or_check_fill(s, sig)

                # мониторим TP/STOP
                monitor_tp_and_stop(s, sig)

            time.sleep(60)   # основной такт
        except Exception as e:
            log.error(f"Global error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # sanity
    if not (BYBIT_KEY and BYBIT_SECRET):
        log.error("Set BYBIT_KEY/BYBIT_SECRET env")
        exit(1)
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("bye")
