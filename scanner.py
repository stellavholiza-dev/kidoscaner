#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KidoCryptosignal Scanner Pro v2 — Pydroid 3
Combined EMA250 one-shot mean reversion + 5m momentum scalp / 15m trend.
Public market data only; Telegram alerts only; NEVER places orders.

Install: pip install requests

Required environment variable:
  TELEGRAM_BOT_TOKEN=TOKEN = ""

Optional:
  TELEGRAM_CHAT_ID=kidocryptosignal
  ACCOUNT_EQUITY_USDT=1000
  TOP_N=40
  SCAN_ALL_PERPETUALS=0
  CHECK_INTERVAL=300
  FUTURES_BASE_URL=https://fapi.binance.com

Default data adapter is Binance USD-M Futures public REST. HUMAUSDT is monitored
if listed there. For another exchange, adapt the REST functions (exchangeInfo,
24hr ticker, klines); do not assume symbols or contract specs are identical.
"""

import os, sys, json, time, math, html, traceback, datetime
from decimal import Decimal, ROUND_DOWN
import requests

# ---------------- CONFIG ----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@kidocryptosignal").strip()
BASE = os.getenv("FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")
EQUITY = float(os.getenv("ACCOUNT_EQUITY_USDT", "1000"))
RISK_PCT, MAX_LEVERAGE = 1.0, 10.0
TOP_N = int(os.getenv("TOP_N", "40"))
SCAN_ALL = os.getenv("SCAN_ALL_PERPETUALS", "0") == "1"
INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
DELAY = float(os.getenv("REQUEST_DELAY", "0.06"))
HUMA = "HUMAUSDT"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "algowave_state.json")

# Original AlgoWave mean-reversion parameters
MR_LEN, MR_DEV = 250, 1.0
MR_TOL, MR_MAX_BELOW, MR_MIN_PROFIT = 1.8, 2.0, 3.0
MR_LOOKBACK = 50
MR_TFS = ["15m", "1h", "4h", "1d"]

# Momentum scalp parameters
ATR_MULT, RR = 1.5, 2.0
VOL_MULT = 1.2
session = requests.Session()
session.headers.update({"User-Agent": "AlgoWaveScannerPro/2.0"})
meta = {}
state = {"mr": {}, "last": {}, "pending": []}


def log(msg):
    print("[{}] {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def save_state():
    state["pending"] = state["pending"][-500:]
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("State save warning: {}".format(e))


def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        state.update({k: old.get(k, state[k]) for k in state})
        log("Loaded state: {} MR cycles, {} dedup keys, {} queued alerts".format(
            len(state["mr"]), len(state["last"]), len(state["pending"])))
    except FileNotFoundError:
        log("Creating new persistent state file.")
    except Exception as e:
        log("State file unreadable; starting fresh: {}".format(e))


def api(path, params=None, attempts=5):
    url = BASE + path
    last = None
    for n in range(attempts):
        try:
            r = session.get(url, params=params, timeout=12)
            if r.status_code in (418, 429):
                wait = max(float(r.headers.get("Retry-After", 2)), min(60, 2 ** n))
                log("Exchange rate limit {}. Wait {}s".format(r.status_code, wait))
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(min(30, 2 ** n))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(min(20, 1.5 * (2 ** n)))
    raise RuntimeError("API failed: {}".format(last))


def telegram_send(message):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TOKEN)
    for n in range(4):
        try:
            r = session.post(url, data={
                "chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=15)
            data = r.json()
            if r.ok and data.get("ok"):
                log("Telegram signal posted to {}".format(CHAT_ID))
                return True
            if r.status_code == 429:
                time.sleep(float(data.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            log("Telegram rejected alert: {}".format(data.get("description", r.text[:200])))
            return False
        except Exception as e:
            log("Telegram send error: {}".format(e))
            time.sleep(2 ** n)
    return False


def dispatch(message):
    if not telegram_send(message):
        state["pending"].append({"message": message, "queued": datetime.datetime.now().isoformat()})
        state["pending"] = state["pending"][-500:]
        save_state()


def flush_queue():
    sent = 0
    while state["pending"] and sent < 5:
        item = state["pending"][0]
        if telegram_send(item.get("message", "")):
            state["pending"].pop(0)
            sent += 1
            save_state()
        else:
            break


def test_setup():
    global TOKEN

    if not TOKEN:
        print("\nTELEGRAM BOT SETUP")
        TOKEN = input("Paste your Telegram Bot Token: ").strip()

    if not TOKEN:
        raise RuntimeError("No Telegram token entered.")

    try:
        api("/fapi/v1/ping", attempts=2)
        log("Binance market data endpoint online.")
    except Exception as e:
        log("Market endpoint warning: {}".format(e))

    try:
        r = session.get(
            "https://api.telegram.org/bot{}/getMe".format(TOKEN),
            timeout=15
        )
        data = r.json()

        if not r.ok or not data.get("ok"):
            raise RuntimeError(
                "Telegram token error: {}".format(
                    data.get("description", "Unknown error")
                )
            )

        log("Telegram connected successfully!")
        log("Bot username: @{}".format(
            data["result"].get("username", "unknown")
        ))

    except requests.RequestException as e:
        raise RuntimeError("Telegram connection failed: {}".format(e))


def load_universe():
    global meta
    info = api("/fapi/v1/exchangeInfo")
    found = {}
    for s in info.get("symbols", []):
        if (s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"):
            lot = next((f for f in s.get("filters", []) if f.get("filterType") == "LOT_SIZE"), {})
            found[s["symbol"]] = {
                "step": float(lot.get("stepSize", 0) or 0),
                "min": float(lot.get("minQty", 0) or 0)
            }
    meta = found
    log("Found {} tradable USDT perpetual contracts.".format(len(meta)))
    return set(meta)


def choose_symbols(available):
    if SCAN_ALL:
        selected = sorted(available)
    else:
        tickers = api("/fapi/v1/ticker/24hr")
        ranked = sorted(
            [(x["symbol"], float(x.get("quoteVolume", 0))) for x in tickers
             if x.get("symbol") in available],
            key=lambda x: x[1], reverse=True
        )
        selected = [x[0] for x in ranked[:TOP_N]]
    if HUMA in available and HUMA not in selected:
        selected.append(HUMA)
    if HUMA not in available:
        log("NOTICE: HUMAUSDT is not listed on this futures endpoint; dedicated HUMA scan skipped.")
    return selected


def candles(symbol, tf, limit=320):
    raw = api("/fapi/v1/klines", {"symbol": symbol, "interval": tf, "limit": limit}, attempts=3)
    now = int(time.time() * 1000)
    out = []
    for x in raw:
        if len(x) >= 7 and int(x[6]) < now:
            out.append({
                "ot": int(x[0]), "ct": int(x[6]), "o": float(x[1]),
                "h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5])
            })
    return out


# ---------------- INDICATORS (stdlib only) ----------------
def ema(values, length):
    out = [None] * len(values)
    if len(values) < length:
        return out
    out[length - 1] = sum(values[:length]) / length
    a = 2.0 / (length + 1.0)
    for i in range(length, len(values)):
        out[i] = a * values[i] + (1 - a) * out[i - 1]
    return out


def rsi(values, length=14):
    out = [None] * len(values)
    if len(values) <= length:
        return out
    gains, losses = [], []
    for i in range(1, length + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / length, sum(losses) / length

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[length] = value(ag, al)
    for i in range(length + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (length - 1) + max(d, 0)) / length
        al = (al * (length - 1) + max(-d, 0)) / length
        out[i] = value(ag, al)
    return out


def macd(values):
    ef, es = ema(values, 12), ema(values, 26)
    line = [None] * len(values)
    for i in range(len(values)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    start = next((i for i, x in enumerate(line) if x is not None), None)
    sig, hist = [None] * len(values), [None] * len(values)
    if start is not None:
        valid = [x for x in line[start:] if x is not None]
        sig_valid = ema(valid, 9)
        for j, x in enumerate(sig_valid):
            i = start + j
            sig[i] = x
            if x is not None:
                hist[i] = line[i] - x
    return line, sig, hist


def atr(cs, length=14):
    tr = []
    for i, c in enumerate(cs):
        if i == 0:
            tr.append(c["h"] - c["l"])
        else:
            pc = cs[i - 1]["c"]
            tr.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    out = [None] * len(cs)
    if len(tr) >= length:
        cur = sum(tr[:length]) / length
        out[length - 1] = cur
        for i in range(length, len(tr)):
            cur = (cur * (length - 1) + tr[i]) / length
            out[i] = cur
    return out


def sma(values, length):
    return sum(values[-length:]) / length if len(values) >= length else None


def quantity_for(symbol, entry, stop):
    dist = abs(entry - stop)
    if entry <= 0 or dist <= 0 or EQUITY <= 0:
        return 0, 0, 0
    risk_cash = EQUITY * RISK_PCT / 100
    qty = min(risk_cash / dist, (EQUITY * MAX_LEVERAGE) / entry)
    step = meta.get(symbol, {}).get("step", 0)
    minimum = meta.get(symbol, {}).get("min", 0)
    if step > 0:
        q, st = Decimal(str(qty)), Decimal(str(step))
        qty = float((q / st).to_integral_value(rounding=ROUND_DOWN) * st)
    if minimum and qty < minimum:
        qty = 0
    return qty, qty * entry, risk_cash


def price(x):
    if abs(x) < 0.0001:
        return "{:.8f}".format(x)
    if abs(x) < 1:
        return "{:.6f}".format(x)
    return "{:.4f}".format(x)


def make_message(s):
    sym = html.escape(s["symbol"])
    lines = [
        "🚨 <b>ALGOWAVE SCANNER PRO</b> 🚨",
        "━━━━━━━━━━━━━━━━━━",
        "{} <b>{} | {}</b>".format("🟢" if s["side"] == "BUY" else "🔴", s["side"], sym),
        "🧠 <b>Strategy:</b> <code>{}</code>".format(html.escape(s["engine"])),
        "⏱ <b>Entry TF:</b> <code>{}</code> | <b>Trend TF:</b> <code>{}</code>".format(
            s["tf"], s.get("trend_tf", "N/A")),
        "━━━━━━━━━━━━━━━━━━",
        "💲 Entry: <code>{}</code>".format(price(s["entry"])),
        "🛑 Stop: <code>{}</code>".format(price(s["stop"])),
        "🎯 Target: <code>{}</code>".format(price(s["target"])),
        "⚖️ R:R: <code>1:{:.2f}</code>".format(s["rr"]),
        "📊 Confirmation: <code>{}/{}</code>".format(s["score"], s["max_score"]),
    ]
    lines += ["• " + html.escape(x) for x in s["notes"]]
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "Equity basis: <code>{:.2f} USDT</code>".format(EQUITY),
        "Risk budget: <code>{:.2f} USDT ({}%)</code>".format(s["risk_cash"], RISK_PCT),
        "Estimated qty: <code>{:.6f}</code>".format(s["qty"]),
        "Estimated notional: <code>{:.2f} USDT</code>".format(s["notional"]),
        "Max notional leverage: <code>{:.1f}x</code>".format(MAX_LEVERAGE),
        "🕯 Closed candle: <code>{}</code>".format(
            datetime.datetime.fromtimestamp(s["ct"] / 1000, datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")),
        "🔗 <a href='https://www.tradingview.com/chart/?symbol=BINANCE:{}'>TradingView chart</a>".format(sym),
        "⚠️ <i>Alert only. No orders placed. Verify contract specs, fees, slippage and margin before trading.</i>"
    ]
    return "\n".join(lines)


def emit(s):
    key = "{}|{}|{}".format(s["symbol"], s["engine"], s["tf"])
    candle_id = str(s["ct"])
    if state["last"].get(key) == candle_id:
        return False
    # Persist deduplication before network send to avoid duplicate after restart.
    state["last"][key] = candle_id
    save_state()
    msg = make_message(s)
    if not telegram_send(msg):
        state["pending"].append({"message": msg, "queued": datetime.datetime.now().isoformat()})
        state["pending"] = state["pending"][-500:]
        save_state()
    log("{} {} {} @ {}".format(s["side"], s["symbol"], s["engine"], price(s["entry"])))
    return True


# ---------------- ORIGINAL EMA250 ONE-SHOT MEAN REVERSION ----------------
def mean_reversion(symbol, tf, cs):
    if len(cs) < MR_LEN + 15:
        return None
    closes = [x["c"] for x in cs]
    highs = [x["h"] for x in cs]
    lows = [x["l"] for x in cs]
    e = ema(closes, MR_LEN)
    upper, lower = [None] * len(cs), [None] * len(cs)
    start = max(MR_LEN - 1, len(cs) - MR_LOOKBACK - 2)
    for i in range(start, len(cs)):
        if e[i] is None:
            continue
        w = closes[i - MR_LEN + 1:i + 1]
        avg = sum(w) / MR_LEN
        dev = math.sqrt(sum((v - avg) ** 2 for v in w) / MR_LEN) * MR_DEV
        upper[i], lower[i] = e[i] + dev, e[i] - dev

    i = len(cs) - 1
    if upper[i] is None or lower[i] is None:
        return None
    key = symbol + "|" + tf
    phase = state["mr"].get(key, "idle")
    cur = cs[i]

    if phase == "signalled":
        if cur["h"] >= e[i]:
            state["mr"][key] = "consumed"
            save_state()
        else:
            return None

    phase = state["mr"].get(key, phase)
    if phase == "consumed":
        if cur["h"] >= upper[i]:
            phase = "above_red"
        else:
            state["mr"][key] = phase
            return None

    saw_upper = phase == "above_red"
    saw_below = phase == "below_blue"
    for j in range(start, i + 1):
        if upper[j] is None:
            continue
        if not saw_upper and highs[j] >= upper[j]:
            saw_upper = True
        elif saw_upper and closes[j] < e[j]:
            saw_below = True

    phase = "below_blue" if saw_below else ("above_red" if saw_upper else "idle")
    if phase == "below_blue" and cur["c"] >= e[i]:
        phase = "idle"
    state["mr"][key] = phase

    if phase != "below_blue":
        return None

    min_price = lower[i] * (1 - MR_MAX_BELOW / 100)
    max_price = lower[i] * (1 + MR_TOL / 100)
    in_zone = lows[i] <= max_price and cur["c"] >= min_price and cur["c"] <= max_price
    profit_pct = (e[i] - cur["c"]) / cur["c"] * 100
    if not in_zone or profit_pct < MR_MIN_PROFIT:
        return None

    entry, stop, target = cur["c"], lower[i] * 0.985, e[i]
    if not stop < entry < target:
        return None
    qty, notional, risk_cash = quantity_for(symbol, entry, stop)
    if qty <= 0:
        return None

    rv = rsi(closes)
    ml, ms, mh = macd(closes)
    vavg = sma([x["v"] for x in cs], 20)
    score, notes = 0, [
        "Original sequence: upper band touch then close below EMA250",
        "Price inside original lower-band buy zone",
        "Potential return to EMA250: {:.2f}%".format(profit_pct)
    ]
    if rv[i] is not None and rv[i] > 30:
        score += 1
        notes.append("RSI14 above 30")
    if mh[i] is not None and mh[i - 1] is not None and mh[i] > mh[i - 1]:
        score += 1
        notes.append("MACD histogram improving")
    if vavg and cur["v"] > vavg:
        score += 1
        notes.append("Volume above SMA20")
    risk = entry - stop
    state["mr"][key] = "signalled"
    save_state()
    return {
        "symbol": symbol, "engine": "EMA250 ONE-SHOT MEAN REVERSION",
        "side": "BUY", "tf": tf, "trend_tf": "EMA250 baseline",
        "entry": entry, "stop": stop, "target": target,
        "rr": (target - entry) / risk, "qty": qty, "notional": notional,
        "risk_cash": risk_cash, "score": score, "max_score": 3,
        "notes": notes, "ct": cur["ct"]
    }


# ---------------- 5m MOMENTUM SCALP / 15m TREND ----------------
def scalp(symbol, c5, c15):
    if len(c5) < 80 or len(c15) < 80:
        return None
    x = [c["c"] for c in c5]
    y = [c["c"] for c in c15]
    e9, e21 = ema(x, 9), ema(x, 21)
    rv = rsi(x)
    ml, ms, mh = macd(x)
    av = atr(c5, 14)
    vol = [c["v"] for c in c5]
    vavg = [None] * len(vol)
    for k in range(19, len(vol)):
        vavg[k] = sum(vol[k - 19:k + 1]) / 20
    t20, t50 = ema(y, 20), ema(y, 50)
    i, p, j = len(c5) - 1, len(c5) - 2, len(c15) - 1
    vals = [e9[i], e21[i], e9[p], e21[p], rv[i], ml[i], ms[i],
            mh[i], mh[p], av[i], vavg[i], t20[j], t50[j]]
    if any(v is None for v in vals):
        return None

    long_checks = [
        y[j] > t20[j] > t50[j],
        e9[i] > e21[i],
        50 < rv[i] < 70,
        ml[i] > ms[i] and mh[i] > 0,
        c5[i]["v"] > vavg[i] * VOL_MULT,
        x[p] <= e9[p] and x[i] > e9[i]
    ]
    short_checks = [
        y[j] < t20[j] < t50[j],
        e9[i] < e21[i],
        30 < rv[i] < 50,
        ml[i] < ms[i] and mh[i] < 0,
        c5[i]["v"] > vavg[i] * VOL_MULT,
        x[p] >= e9[p] and x[i] < e9[i]
    ]
    if all(long_checks):
        side, score = "BUY", sum(long_checks)
    elif all(short_checks):
        side, score = "SELL", sum(short_checks)
    else:
        return None

    entry, distance = c5[i]["c"], av[i] * ATR_MULT
    if side == "BUY":
        stop, target = entry - distance, entry + distance * RR
    else:
        stop, target = entry + distance, entry - distance * RR
    qty, notional, risk_cash = quantity_for(symbol, entry, stop)
    if qty <= 0:
        return None

    notes = [
        "15m EMA20/EMA50 trend aligned",
        "5m EMA9/EMA21 aligned",
        "RSI14: {:.1f}".format(rv[i]),
        "MACD 12/26/9 confirmed",
        "Volume: {:.2f}x SMA20".format(c5[i]["v"] / vavg[i]),
        "ATR14: {}".format(price(av[i]))
    ]
    return {
        "symbol": symbol, "engine": "EMA MOMENTUM SCALP",
        "side": side, "tf": "5m", "trend_tf": "15m",
        "entry": entry, "stop": stop, "target": target, "rr": RR,
        "qty": qty, "notional": notional, "risk_cash": risk_cash,
        "score": score, "max_score": 6, "notes": notes, "ct": c5[i]["ct"]
    }


def scan_symbol(symbol):
    count = 0
    try:
        c5 = candles(symbol, "5m")
        time.sleep(DELAY)
        c15 = candles(symbol, "15m")
        time.sleep(DELAY)
        sig = scalp(symbol, c5, c15)
        if sig and emit(sig):
            count += 1
    except Exception as e:
        log("{} scalp scan error: {}".format(symbol, e))

    for tf in MR_TFS:
        try:
            cs = c15 if tf == "15m" else candles(symbol, tf)
            if tf != "15m":
                time.sleep(DELAY)
            sig = mean_reversion(symbol, tf, cs)
            if sig and emit(sig):
                count += 1
        except Exception as e:
            log("{} {} MR scan error: {}".format(symbol, tf, e))
    return count


def run():
    load_state()
    test_setup()
    available = load_universe()
    if not available:
        raise RuntimeError("No eligible USDT perpetual contracts returned by exchange.")
    symbols = choose_symbols(available)
    log("Monitoring {} symbols. HUMAUSDT: {}.".format(
        len(symbols), "ON" if HUMA in symbols else "NOT LISTED"))
    log("Scalp: 5m entry / 15m trend. MR timeframes: {}".format(", ".join(MR_TFS)))
    log("Risk: {}% equity, max {}x notional; scalp SL {} ATR, TP {}R.".format(
        RISK_PCT, MAX_LEVERAGE, ATR_MULT, RR))

    cycle = 0
    while True:
        cycle += 1
        started = time.time()
        found = 0
        log("=" * 55 + " SCAN CYCLE {}".format(cycle))
        flush_queue()
        symbols = choose_symbols(available)
        for n, symbol in enumerate(symbols, 1):
            log("[{}/{}] {}".format(n, len(symbols), symbol))
            found += scan_symbol(symbol)
        save_state()
        elapsed = time.time() - started
        log("Cycle {} complete: {} new alerts, {:.1f}s.".format(cycle, found, elapsed))
        time.sleep(max(5, INTERVAL - elapsed))


def main():
    while True:
        try:
            run()
        except KeyboardInterrupt:
            log("Stopped by user.")
            save_state()
            break
        except Exception as e:
            log("Scanner error: {}".format(e))
            traceback.print_exc()
            save_state()
            log("Reconnecting in 10 seconds...")
            time.sleep(10)


if __name__ == "__main__":
    main()
