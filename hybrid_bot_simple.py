# -*- coding: utf-8 -*-
"""
trade_v3_2.py  — монолитная версия
Bybit Spot (v5, UNIFIED). Подпись, time-sync, anti-spam TG, cooldown,
afford-qty, защита SELL/TP, trailing TP, компактные логи, файл состояния.

Python 3.10+
pip install requests

© you
"""
import os
import hmac
import json
import time
import math
import hashlib
import random
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import requests

# =========================
# CONFIG — заполняйте здесь
# =========================
BYBIT_API_KEY    = "YOUR_BYBIT_API_KEY"
BYBIT_API_SECRET = "YOUR_BYBIT_API_SECRET"
BYBIT_BASE       = "https://api.bybit.com"

# Торгуемые символы (spot)
SYMBOLS = ["XRPUSDT", "DOGEUSDT", "TONUSDT"]

ACCOUNT_TYPE = "UNIFIED"   # важно для приватных методов v5 (баланс и т.п.)
RECV_WINDOW  = 5000        # ms

# Бюджет на сделку в USDT
BUDGET_MIN   = 150.0
BUDGET_MAX   = 230.0

# Комиссия тейкера (в долях), для грубой оценки
TAKER_FEE = 0.0018

# Индикативные параметры TP/SL/trailing
BASE_TP_PCT    = 0.0060    # 0.60% базовая цель
TRAIL_X        = 1.5       # усиливаем трейл от локального максимума
STOP_LOSS_PCT  = 0.030     # 3% от средней входа (жёсткая защита)

# Кулдаун между ордерами по одному символу (сек)
COOLDOWN_SEC   = 20

# Сколько исторических свечей тянуть (для EMA/RSI, если нужно)
KLINE_LIMIT    = 120
KLINE_INTERVAL = "1"        # 1m

# Telegram
TG_TOKEN       = "YOUR_TELEGRAM_BOT_TOKEN"
TG_CHAT_ID     = "YOUR_TELEGRAM_CHAT_ID"

# Файлы
STATE_FILE     = "trade_state.json"
LIMITS_FILE    = "symbol_limits.json"

# Лимиты по умолчанию (если не успели подтянуть с биржи)
DEFAULT_LIMITS = {
    "XRPUSDT":  {"min_qty": 0.44, "qty_step": 0.01, "min_amt": 5.0},
    "DOGEUSDT": {"min_qty": 3.0,  "qty_step": 1.0,  "min_amt": 5.0},
    "TONUSDT":  {"min_qty": 0.38, "qty_step": 0.01, "min_amt": 5.0},
}

# =========================
# Глобалы (смещение времени, кэш лимитов и т.п.)
# =========================
TIME_OFFSET_MS = 0      # serverTime - localTime
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

_error_cache: Dict[str, float] = {}   # анти-спам ошибок
_last_trade_at: Dict[str, float] = {} # cooldown
_symbol_limits: Dict[str, Dict[str, float]] = {}  # min_qty/qty_step/min_amt
_state_lock = threading.Lock()


# ==============
# Утилиты/сервис
# ==============
def now_ms() -> int:
    return int(time.time() * 1000)

def ts_with_offset() -> int:
    # Всегда используем serverTime + offset
    return now_ms() + TIME_OFFSET_MS

def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)

def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        session.post(url, json=payload, timeout=7)
    except Exception:
        pass

def anti_spam(key: str, window_sec: int = 60) -> bool:
    """Возвращает True, если уже недавно слали такое — тогда подавим."""
    now = time.time()
    last = _error_cache.get(key, 0)
    if now - last < window_sec:
        return True
    _error_cache[key] = now
    return False

# ============
# Подпись v5
# ============
def sign_v5(params: Dict[str, Any]) -> str:
    """Bybit v5: сортируем по ключу (ascii), склеиваем key=value&..., HMAC SHA256(secret)."""
    items = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        items.append(f"{k}={v}")
    qs = "&".join(items)
    return hmac.new(BYBIT_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()

def bybit_private_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params)
    p["apiKey"]     = BYBIT_API_KEY
    p["timestamp"]  = ts_with_offset()
    p["recvWindow"] = RECV_WINDOW
    sig = sign_v5(p)
    p["sign"] = sig
    url = BYBIT_BASE + path
    r = session.get(url, params=p, timeout=10)
    return r.json()

def bybit_private_post(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params)
    p["apiKey"]     = BYBIT_API_KEY
    p["timestamp"]  = ts_with_offset()
    p["recvWindow"] = RECV_WINDOW
    sig = sign_v5(p)
    p["sign"] = sig
    url = BYBIT_BASE + path
    r = session.post(url, json=p, timeout=10)
    return r.json()

def bybit_public_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = BYBIT_BASE + path
    r = session.get(url, params=params, timeout=10)
    return r.json()

# ======================
# Time sync (serverTime)
# ======================
def sync_time() -> None:
    global TIME_OFFSET_MS
    try:
        data = bybit_public_get("/v5/market/time", {})
        st = int(data.get("time", int(time.time()*1000)))
        local = now_ms()
        TIME_OFFSET_MS = st - local
        log(f"⏱  Time sync: server={st}, local={local}, offset={TIME_OFFSET_MS} ms")
    except Exception as e:
        log(f"⚠️  time sync failed: {e}")

# ==========================
# Символьные лимиты (filters)
# ==========================
def load_limits_cache() -> None:
    global _symbol_limits
    _symbol_limits = DEFAULT_LIMITS.copy()
    if os.path.exists(LIMITS_FILE):
        try:
            with open(LIMITS_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                _symbol_limits.update(cached)
        except Exception:
            pass

def save_limits_cache() -> None:
    try:
        with open(LIMITS_FILE, "w", encoding="utf-8") as f:
            json.dump(_symbol_limits, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def fetch_symbol_limit(symbol: str) -> None:
    """Тянем market/instruments-info и забираем minOrderQty / minOrderAmt / qtyStep."""
    try:
        data = bybit_public_get("/v5/market/instruments-info",
                                {"category": "spot", "symbol": symbol})
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            return
        info = rows[0]
        lot = info.get("lotSizeFilter", {})
        min_qty  = float(lot.get("minOrderQty", _symbol_limits.get(symbol, DEFAULT_LIMITS.get(symbol, {})).get("min_qty", 0.0)))
        qty_step = float(lot.get("qtyStep",     _symbol_limits.get(symbol, DEFAULT_LIMITS.get(symbol, {})).get("qty_step", 1.0)))
        min_amt  = float((info.get("priceFilter") or {}).get("minOrderAmt",
                        _symbol_limits.get(symbol, DEFAULT_LIMITS.get(symbol, {})).get("min_amt", 5.0)))
        _symbol_limits[symbol] = {"min_qty": min_qty, "qty_step": qty_step, "min_amt": min_amt}
        save_limits_cache()
        log(f"ℹ️  {symbol} limits: { _symbol_limits[symbol] }")
    except Exception as e:
        log(f"⚠️  fetch limits failed for {symbol}: {e}")

# ===========
# Балансы/клиния
# ===========
def get_usdt_balance() -> float:
    try:
        data = bybit_private_get("/v5/account/wallet-balance",
                                 {"accountType": ACCOUNT_TYPE, "coin": "USDT"})
        if data.get("retCode") == 0:
            result = data.get("result") or {}
            l = (result.get("list") or [])
            if l:
                # В UNIFIED у coin: [{'coin':'USDT', 'walletBalance': '...', ...}]
                coins = l[0].get("coin", [])
                for c in coins:
                    if c.get("coin") == "USDT":
                        return float(c.get("availableToWithdraw") or c.get("walletBalance") or 0.0)
        else:
            _maybe_report_wallet_error(data)
    except Exception as e:
        _maybe_report_wallet_error({"retMsg": str(e)})
    return 0.0

def get_price(symbol: str) -> Optional[float]:
    try:
        data = bybit_public_get("/v5/market/tickers", {"category": "spot", "symbol": symbol})
        if data.get("retCode") == 0:
            rows = (data.get("result") or {}).get("list") or []
            if rows:
                return float(rows[0]["lastPrice"])
    except Exception:
        pass
    return None

# ==========================================
# Индикативные сигналы (очень лёгкая логика)
# ==========================================
def ema(series: List[float], span: int) -> float:
    k = 2 / (span + 1.0)
    e = series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e

def fetch_signal(symbol: str) -> Tuple[str, float, Dict[str, float]]:
    """
    Возвращает ('BUY'|'SELL'|'HOLD', confidence, metrics)
    """
    try:
        data = bybit_public_get("/v5/market/kline",
                                {"category": "spot", "symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT})
        if data.get("retCode") != 0:
            return "HOLD", 0.0, {}
        rows = (data.get("result") or {}).get("list") or []
        closes = [float(r[4]) for r in rows][-60:]  # close
        if len(closes) < 20:
            return "HOLD", 0.0, {}
        ema9  = ema(closes[-30:], 9)
        ema21 = ema(closes[-60:], 21)
        price = closes[-1]
        # простенький сигнальчик:
        if price > ema9 > ema21:
            return "BUY", 0.6, {"price": price, "EMA9": ema9, "EMA21": ema21}
        elif price < ema9 < ema21:
            return "SELL", 0.6, {"price": price, "EMA9": ema9, "EMA21": ema21}
        else:
            return "HOLD", 0.0, {"price": price, "EMA9": ema9, "EMA21": ema21}
    except Exception:
        return "HOLD", 0.0, {}

# ===============
# Файл состояния
# ===============
def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"pos": {}, "cooldown": {}, "last_error": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pos": {}, "cooldown": {}, "last_error": {}}

def _save_state(st: Dict[str, Any]) -> None:
    with _state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)

def get_state() -> Dict[str, Any]:
    with _state_lock:
        return _load_state()

def update_pos(symbol: str, qty: float, avg: float, tp: Optional[float]) -> None:
    st = get_state()
    st.setdefault("pos", {})
    st["pos"][symbol] = {"qty": qty, "avg": avg, "tp": tp}
    _save_state(st)

def get_pos(symbol: str) -> Dict[str, float]:
    st = get_state().get("pos", {})
    return st.get(symbol, {"qty": 0.0, "avg": 0.0, "tp": None})

def set_cooldown(symbol: str) -> None:
    st = get_state()
    st.setdefault("cooldown", {})
    st["cooldown"][symbol] = time.time()
    _save_state(st)

def cooldown_active(symbol: str) -> bool:
    st = get_state().get("cooldown", {})
    t = st.get(symbol, 0.0)
    return (time.time() - t) < COOLDOWN_SEC

# =====================
# Ордеры и afford-qty
# =====================
def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step

def _afford_qty(symbol: str, price: float, want_usdt: float) -> float:
    lim = _symbol_limits.get(symbol, DEFAULT_LIMITS.get(symbol, {}))
    min_qty  = float(lim.get("min_qty", 0.0))
    qty_step = float(lim.get("qty_step", 1.0))
    min_amt  = float(lim.get("min_amt", 5.0))
    # учитываем комиссию
    gross_qty = (want_usdt * (1.0 - TAKER_FEE)) / max(price, 1e-9)
    qty = max(min_qty, _round_step(gross_qty, qty_step))
    # minAmt — сумма в USDT
    if qty * price < min_amt:
        qty = _round_step((min_amt / price), qty_step)
    return max(0.0, qty)

def place_order(symbol: str, side: str, qty: float) -> Dict[str, Any]:
    params = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy" if side.upper() == "BUY" else "Sell",
        "orderType": "Market",
        "qty": f"{qty:.8f}",
    }
    data = bybit_private_post("/v5/order/create", params)
    if data.get("retCode") != 0:
        _maybe_report_wallet_error(data)
    return data

def _maybe_report_wallet_error(data: Dict[str, Any]) -> None:
    # Анти-спам: одинаковые retCode/retMsg минуту не спамим
    key = f"{data.get('retCode')}|{data.get('retMsg')}"
    if anti_spam(key, 60):
        return
    txt = f"Ошибка цикла: {data.get('retMsg')} (retCode={data.get('retCode')})"
    log(f"loop error: {jdump(data)}")
    tg_send(txt)

# ====================
# Защита SELL и TP/SL
# ====================
def calc_tp_from(avg: float) -> float:
    return avg * (1.0 + BASE_TP_PCT)

def need_sell(symbol: str, price: float) -> Tuple[bool, Optional[str]]:
    """Продаём если:
       - достигли/превысили TP (включая трейлинг)
       - сработал SL (жёсткая защита)
       - НО не продаём «ниже нуля» если TP не достигнут.
    """
    p = get_pos(symbol)
    qty = float(p.get("qty", 0.0))
    if qty <= 0:
        return False, None

    avg = float(p.get("avg", 0.0))
    # SL
    if price <= avg * (1.0 - STOP_LOSS_PCT):
        return True, "SL"

    # trailing TP
    curr_tp = p.get("tp")
    base_tp = calc_tp_from(avg)
    if curr_tp is None:
        curr_tp = base_tp

    # если цена росла — сдвигаем (усиливаем) TP
    if price > curr_tp:
        # от последнего high трейлим
        new_tp = price / (1.0 + (BASE_TP_PCT / TRAIL_X))
        if new_tp > curr_tp:
            curr_tp = new_tp
            update_pos(symbol, qty, avg, curr_tp)

    # если достигли tp — продаём
    if price >= curr_tp:
        return True, "TP"

    # защита от продажи в минус
    if price < avg:
        return False, None

    return False, None

# ==========
# Основной цикл
# ==========
def trading_loop():
    load_limits_cache()
    for s in SYMBOLS:
        fetch_symbol_limit(s)

    sync_time()

    log("🚀 Бот запускается...")
    tg_send("🚀 Старт бота. Состояние: FRESH")

    while True:
        try:
            # периодически обновлять смещение времени
            if random.random() < 0.02:
                sync_time()

            usdt = get_usdt_balance()
            log(f"💰 Баланс USDT={usdt:.2f}")

            for symbol in SYMBOLS:
                # Прайс
                price = get_price(symbol)
                if price is None:
                    continue

                # Позиция
                pos = get_pos(symbol)
                qty = float(pos.get("qty", 0.0))
                avg = float(pos.get("avg", 0.0))

                # Сигнал
                signal, conf, m = fetch_signal(symbol)
                conf = round(conf, 2)
                ema9  = m.get("EMA9", 0.0)
                ema21 = m.get("EMA21", 0.0)

                # Логи скромные
                act = "HOLD"
                if qty > 0:
                    act = "HOLD"
                log(f"[{symbol}] 🔎 {act} (conf={conf:.2f}), price={price:.6f}, "
                    f"balance={qty:.6f} (~{qty*price:.2f} USDT) | EMA9={ema9:.4f}, EMA21={ema21:.4f}")

                # Пробуем sell (TP/SL)
                if qty > 0:
                    to_sell, reason = need_sell(symbol, price)
                    if to_sell and not cooldown_active(symbol):
                        # продаём всё
                        sell_qty = _round_step(qty, _symbol_limits.get(symbol, {}).get("qty_step", 1.0))
                        if sell_qty > 0:
                            data = place_order(symbol, "SELL", sell_qty)
                            if data.get("retCode") == 0:
                                pnl = (price - avg) * sell_qty
                                tg_send(f"✅ {symbol} SELL @ {price:.6f}, qty={sell_qty:.6f}, pnl={pnl:.2f}  [{reason}]")
                                update_pos(symbol, 0.0, 0.0, None)
                                set_cooldown(symbol)
                            else:
                                _maybe_report_wallet_error(data)

                # Покупка
                if qty <= 0 and signal == "BUY" and conf >= 0.5 and not cooldown_active(symbol):
                    if usdt >= BUDGET_MIN:
                        want = min(BUDGET_MAX, usdt)
                        buy_qty = _afford_qty(symbol, price, want)
                        if buy_qty * price >= _symbol_limits.get(symbol, DEFAULT_LIMITS.get(symbol, {})).get("min_amt", 5.0):
                            data = place_order(symbol, "BUY", buy_qty)
                            if data.get("retCode") == 0:
                                update_pos(symbol, buy_qty, price, calc_tp_from(price))
                                tg_send(f"🟢 {symbol} BUY @ {price:.6f}, qty={buy_qty:.6f}")
                                set_cooldown(symbol)
                            else:
                                _maybe_report_wallet_error(data)
                    else:
                        log(f"[{symbol}] бюджет < {BUDGET_MIN}, пропускаем")

            time.sleep(5)

        except Exception as e:
            _maybe_report_wallet_error({"retMsg": f"loop exception: {e}", "retCode": "EXC"})
            time.sleep(2)

# ===========
# ENTRYPOINT
# ===========
if __name__ == "__main__":
    # Быстрая валидация ключей
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("❌ Заполните BYBIT_API_KEY / BYBIT_API_SECRET в CONFIG!")
        raise SystemExit(1)

    # Пингуем время один раз перед стартом (критично для подписи v5)
    sync_time()
    trading_loop()
