"""
Free automated signal bot (Forex + Deriv synthetic indices).
Strategies: EMA 20/50 crossover (+RSI filter) and Price Action (engulfing at the 50 EMA).
ATR-based stop loss, 1:4 risk/reward. Runs on a schedule via GitHub Actions,
posts to a Telegram channel, and tracks TP/SL results per strategy in state/signals.json.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# ---------------- CONFIG (edit these) ----------------
FOREX = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "GBP/JPY"]

# Deriv synthetic indices: display name -> Deriv symbol code (trade 24/7)
SYNTHETICS = {
    "Volatility 75": "R_75",
    "Volatility 75 (1s)": "1HZ75V",
    "Volatility 25": "R_25",
    "Volatility 25 (1s)": "1HZ25V",
    "Volatility 100": "R_100",
    "Volatility 100 (1s)": "1HZ100V",
    "Volatility 90 (1s)": "1HZ90V",
}

STRATEGIES = ["EMA Cross", "Price Action"]  # delete a name here to switch that strategy off

CANDLE_SECONDS = 3600  # H1 candles
CANDLE_COUNT = 250
FAST_EMA, SLOW_EMA = 20, 50
RSI_PERIOD = 14
ATR_PERIOD = 14
SL_ATR_MULT = 1.5      # EMA Cross: stop loss = 1.5 x ATR
RR = 4.0               # take profit = 4 x risk (1:4)
EXPIRE_HOURS = 240     # close a trade as "expired" after this long (10 days)
DERIV_URLS = [  # tried in order; the first one that answers is used
    "wss://api.derivws.com/trading/v1/options/ws/public",  # current public endpoint
    "wss://ws.derivws.com/websockets/v3?app_id=1089",      # older endpoint (fallback)
]
STATE_FILE = Path("state/signals.json")
# -----------------------------------------------------

TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

INSTRUMENTS = [{"name": p, "kind": "forex", "symbol": p} for p in FOREX] + [
    {"name": n, "kind": "synthetic", "symbol": s} for n, s in SYNTHETICS.items()
]


# ---------- Telegram ----------
def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


# ---------- State ----------
def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {"open": [], "closed": [], "last_signal": {}}
    for t in state["open"] + state["closed"]:
        t.setdefault("strategy", "EMA Cross")  # trades saved by the first version
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- Data ----------
def only_closed(df):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return df[df["datetime"] + timedelta(seconds=CANDLE_SECONDS) <= now].reset_index(drop=True)


def fetch_twelvedata(symbol):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": "1h",
            "outputsize": CANDLE_COUNT,
            "timezone": "UTC",
            "apikey": TD_KEY,
        },
        timeout=30,
    )
    data = r.json()
    if "values" not in data:
        raise RuntimeError(data.get("message", str(data)))
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return only_closed(df)


def deriv_request(url, symbol):
    import websocket  # pip package: websocket-client

    ws = websocket.create_connection(url, timeout=20)
    try:
        ws.send(
            json.dumps(
                {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": CANDLE_COUNT,
                    "end": "latest",
                    "granularity": CANDLE_SECONDS,
                    "style": "candles",
                }
            )
        )
        data = {}
        for _ in range(5):  # skip any unrelated messages
            data = json.loads(ws.recv())
            if "candles" in data or "error" in data:
                break
    finally:
        ws.close()
    if "error" in data:
        raise RuntimeError(str(data["error"].get("message", data["error"])))
    if "candles" not in data:
        raise RuntimeError("no candles returned")
    return data


def fetch_deriv(symbol):
    data, errors = None, []
    for url in DERIV_URLS:
        try:
            data = deriv_request(url, symbol)
            break
        except Exception as e:
            host = url.split("/")[2]
            errors.append(f"{host}: {str(e)[:70]}")
    if data is None:
        raise RuntimeError(" | ".join(errors))
    df = pd.DataFrame(data["candles"])
    df["datetime"] = pd.to_datetime(df["epoch"], unit="s")
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return only_closed(df)


def add_indicators(df):
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)

    prev_close = df["close"].shift()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()
    return df


# ---------- Helpers ----------
def decimals(inst):
    if inst["kind"] == "synthetic":
        return 4
    return 3 if "JPY" in inst["name"] else 5


def unit_size(inst):
    if inst["kind"] == "synthetic":
        return 1.0
    return 0.01 if "JPY" in inst["name"] else 0.0001


def unit_label(inst):
    return "pts" if inst["kind"] == "synthetic" else "pips"


def build_signal(inst, strategy, side, entry, sl, candle_time):
    risk = abs(entry - sl)
    tp = entry + RR * risk if side == "BUY" else entry - RR * risk
    d = decimals(inst)
    return {
        "pair": inst["name"],
        "strategy": strategy,
        "side": side,
        "entry": round(entry, d),
        "sl": round(sl, d),
        "tp": round(tp, d),
        "risk_pips": round(risk / unit_size(inst), 1),
        "unit": unit_label(inst),
        "candle": candle_time.isoformat(),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- Strategy 1: EMA cross + RSI ----------
def strategy_ema_cross(inst, df):
    if len(df) < SLOW_EMA + 5:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    crossed_up = prev.ema_fast <= prev.ema_slow and last.ema_fast > last.ema_slow
    crossed_down = prev.ema_fast >= prev.ema_slow and last.ema_fast < last.ema_slow

    entry = float(last.close)
    risk = SL_ATR_MULT * float(last.atr)
    if crossed_up and 50 < last.rsi < 70:
        return build_signal(inst, "EMA Cross", "BUY", entry, entry - risk, last.datetime)
    if crossed_down and 30 < last.rsi < 50:
        return build_signal(inst, "EMA Cross", "SELL", entry, entry + risk, last.datetime)
    return None


# ---------- Strategy 2: Price action (engulfing candle at the 50 EMA) ----------
def strategy_price_action(inst, df):
    if len(df) < SLOW_EMA + 5:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    atr = float(last.atr)
    if not atr > 0:
        return None
    if abs(last.close - last.open) < 0.5 * atr:  # engulfing candle must be strong
        return None

    uptrend = last.ema_fast > last.ema_slow
    downtrend = last.ema_fast < last.ema_slow
    near = 0.25 * atr

    bullish = (
        prev.close < prev.open
        and last.close > last.open
        and last.close >= prev.open
        and last.open <= prev.close
    )
    bearish = (
        prev.close > prev.open
        and last.close < last.open
        and last.close <= prev.open
        and last.open >= prev.close
    )

    entry = float(last.close)
    if uptrend and bullish and last.low <= last.ema_slow + near and last.close > last.ema_slow:
        side = "BUY"
        sl = min(float(prev.low), float(last.low)) - 0.2 * atr
    elif downtrend and bearish and last.high >= last.ema_slow - near and last.close < last.ema_slow:
        side = "SELL"
        sl = max(float(prev.high), float(last.high)) + 0.2 * atr
    else:
        return None

    risk = abs(entry - sl)
    if risk > 3 * atr:  # stop too far away, skip
        return None
    if risk < 0.5 * atr:  # stop too tight, widen it
        sl = entry - 0.5 * atr if side == "BUY" else entry + 0.5 * atr
    return build_signal(inst, "Price Action", side, entry, sl, last.datetime)


STRATEGY_FUNCS = {"EMA Cross": strategy_ema_cross, "Price Action": strategy_price_action}


def format_signal(s):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    return (
        f"{icon} <b>{s['side']} {s['pair']}</b> (H1)\n"
        f"Strategy: {s['strategy']}\n\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"Stop loss: <code>{s['sl']}</code> ({s['risk_pips']} {s.get('unit', 'pips')})\n"
        f"Take profit: <code>{s['tp']}</code> (1:{RR:g})\n\n"
        f"⚠️ Risk 1% or less per trade. Not financial advice."
    )


# ---------- Result tracking ----------
def record(trades):
    scored = [t for t in trades if t["result"] in ("TP", "SL")]
    wins = sum(1 for t in scored if t["result"] == "TP")
    total_r = sum(t["r"] for t in trades)
    n = len(scored)
    rate = (wins / n * 100) if n else 0
    return wins, n - wins, rate, total_r


def stats_line(state, strategy):
    w, l, rate, r = record([t for t in state["closed"] if t.get("strategy") == strategy])
    aw, al, arate, ar = record(state["closed"])
    return (
        f"📊 {strategy}: {w}W / {l}L ({rate:.0f}%) | {r:+.1f}R\n"
        f"📊 All strategies: {aw}W / {al}L ({arate:.0f}%) | {ar:+.1f}R"
    )


def resolve_open(state, candles):
    still_open = []
    now = datetime.now(timezone.utc)
    for t in state["open"]:
        df = candles.get(t["pair"])
        if df is None:
            still_open.append(t)
            continue

        result = None
        after = df[df["datetime"] > pd.Timestamp(t["candle"])]
        for _, c in after.iterrows():
            if t["side"] == "BUY":
                hit_sl, hit_tp = c.low <= t["sl"], c.high >= t["tp"]
            else:
                hit_sl, hit_tp = c.high >= t["sl"], c.low <= t["tp"]
            if hit_sl:  # if both hit in one candle, count the loss (conservative)
                result = "SL"
                break
            if hit_tp:
                result = "TP"
                break

        if result is None:
            opened = datetime.fromisoformat(t["opened_at"])
            if now - opened > timedelta(hours=EXPIRE_HOURS):
                result = "EXPIRED"

        if result is None:
            still_open.append(t)
            continue

        t["result"] = result
        t["r"] = RR if result == "TP" else (-1.0 if result == "SL" else 0.0)
        t["closed_at"] = now.isoformat()
        state["closed"].append(t)

        label = {"TP": "✅ TP hit", "SL": "❌ SL hit", "EXPIRED": "⏱ Expired"}[result]
        send(
            f"{label}: <b>{t['side']} {t['pair']}</b> ({t['strategy']})\n"
            f"Result: {t['r']:+.1f}R\n\n{stats_line(state, t['strategy'])}"
        )
    state["open"] = still_open


# ---------- Main ----------
def main():
    if "--test" in sys.argv:
        send("✅ Bot is connected and working.")
        print("Test message sent.")
        return

    now = datetime.now(timezone.utc)
    weekend = now.weekday() >= 5
    todo = [i for i in INSTRUMENTS if not (weekend and i["kind"] == "forex")]
    if weekend:
        print("Weekend: skipping forex, running synthetic indices only.")

    state = load_state()
    candles = {}
    first_forex = True
    for inst in todo:
        try:
            if inst["kind"] == "forex":
                if not first_forex:
                    time.sleep(8)  # stay under the free-tier rate limit
                first_forex = False
                df = fetch_twelvedata(inst["symbol"])
            else:
                df = fetch_deriv(inst["symbol"])
            candles[inst["name"]] = add_indicators(df)
        except Exception as e:
            print(f"Could not fetch {inst['name']}: {str(e)[:200]}")

    print(f"Fetched {len(candles)} of {len(todo)} instruments.")
    if todo and not candles:
        print("Nothing could be fetched. Check your API keys and connections.")
        sys.exit(1)

    resolve_open(state, candles)
    save_state(state)

    for inst in todo:
        df = candles.get(inst["name"])
        if df is None:
            continue
        for strategy in STRATEGIES:
            if any(t["pair"] == inst["name"] and t["strategy"] == strategy for t in state["open"]):
                continue  # one open trade per instrument per strategy
            sig = STRATEGY_FUNCS[strategy](inst, df)
            key = f"{strategy}|{inst['name']}"
            if sig and state["last_signal"].get(key) != sig["candle"]:
                send(format_signal(sig))
                state["open"].append(sig)
                state["last_signal"][key] = sig["candle"]
                save_state(state)

    print("Run complete.")


if __name__ == "__main__":
    main()
