"""
Free automated Forex signal bot.
Strategy: EMA 20/50 crossover + RSI filter, ATR-based stop loss, 1:2 risk/reward.
Runs on a schedule via GitHub Actions, posts to a Telegram channel,
and tracks TP/SL results in state/signals.json.
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
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD"]
INTERVAL = "1h"
FAST_EMA, SLOW_EMA = 20, 50
RSI_PERIOD = 14
ATR_PERIOD = 14
SL_ATR_MULT = 1.5      # stop loss = 1.5 x ATR
RR = 2.0               # take profit = 2 x risk
EXPIRE_HOURS = 96      # close a trade as "expired" after this long
STATE_FILE = Path("state/signals.json")
# -----------------------------------------------------

TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


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
        return json.loads(STATE_FILE.read_text())
    return {"open": [], "closed": [], "last_signal": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------- Data ----------
def fetch_candles(pair):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": pair,
            "interval": INTERVAL,
            "outputsize": 120,
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
    # keep only fully closed candles
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    df = df[df["datetime"] + timedelta(hours=1) <= now].reset_index(drop=True)
    return df


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


# ---------- Strategy ----------
def decimals(pair):
    return 3 if "JPY" in pair else 5


def pip_size(pair):
    return 0.01 if "JPY" in pair else 0.0001


def check_signal(pair, df):
    if len(df) < SLOW_EMA + 5:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    crossed_up = prev.ema_fast <= prev.ema_slow and last.ema_fast > last.ema_slow
    crossed_down = prev.ema_fast >= prev.ema_slow and last.ema_fast < last.ema_slow

    if crossed_up and 50 < last.rsi < 70:
        side = "BUY"
    elif crossed_down and 30 < last.rsi < 50:
        side = "SELL"
    else:
        return None

    entry = float(last.close)
    risk = SL_ATR_MULT * float(last.atr)
    d = decimals(pair)
    if side == "BUY":
        sl, tp = entry - risk, entry + RR * risk
    else:
        sl, tp = entry + risk, entry - RR * risk

    return {
        "pair": pair,
        "side": side,
        "entry": round(entry, d),
        "sl": round(sl, d),
        "tp": round(tp, d),
        "risk_pips": round(risk / pip_size(pair), 1),
        "candle": last.datetime.isoformat(),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }


def format_signal(s):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    return (
        f"{icon} <b>{s['side']} {s['pair']}</b> (H1)\n\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"Stop loss: <code>{s['sl']}</code> ({s['risk_pips']} pips)\n"
        f"Take profit: <code>{s['tp']}</code> (1:{RR:g})\n\n"
        f"⚠️ Risk 1% or less per trade. Not financial advice."
    )


# ---------- Result tracking ----------
def stats_line(state):
    closed = [t for t in state["closed"] if t["result"] in ("TP", "SL")]
    wins = sum(1 for t in closed if t["result"] == "TP")
    total_r = sum(t["r"] for t in state["closed"])
    n = len(closed)
    rate = (wins / n * 100) if n else 0
    return f"📊 Record: {wins}W / {n - wins}L ({rate:.0f}% win rate) | Total: {total_r:+.1f}R"


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
            f"{label}: <b>{t['side']} {t['pair']}</b>\n"
            f"Result: {t['r']:+.1f}R\n\n{stats_line(state)}"
        )
    state["open"] = still_open


# ---------- Main ----------
def main():
    if "--test" in sys.argv:
        send("✅ Bot is connected and working.")
        print("Test message sent.")
        return

    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        print("Weekend, market closed. Skipping.")
        return

    state = load_state()
    candles = {}
    for i, pair in enumerate(PAIRS):
        if i:
            time.sleep(8)  # stay under the free-tier rate limit
        try:
            candles[pair] = add_indicators(fetch_candles(pair))
        except Exception as e:
            print(f"Could not fetch {pair}: {e}")

    resolve_open(state, candles)
    save_state(state)

    for pair, df in candles.items():
        if any(t["pair"] == pair for t in state["open"]):
            continue  # one open trade per pair
        sig = check_signal(pair, df)
        if sig and state["last_signal"].get(pair) != sig["candle"]:
            send(format_signal(sig))
            state["open"].append(sig)
            state["last_signal"][pair] = sig["candle"]
            save_state(state)

    print("Run complete.")


if __name__ == "__main__":
    main()
