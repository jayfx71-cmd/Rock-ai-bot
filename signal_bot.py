"""
Free automated signal bot (Forex + Deriv synthetic indices) with a learning layer.
Strategies: EMA 20/50 crossover (+RSI filter) and Price Action (engulfing at the 50 EMA).
ATR-based stop loss, 1:4 risk/reward. Runs on a schedule via GitHub Actions,
posts to a Telegram channel, and tracks TP/SL results per strategy in state/signals.json.

Learning layer: every strategy x instrument combination has its own rolling record.
A combination that is clearly losing is paused (no more posts to the channel) but keeps
logging silent "shadow" trades, so it can switch itself back on if it recovers.
Run `python signal_bot.py --backtest` to replay history and seed that learning.
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

# --- learning layer ---
LEARNING = True        # False = post every signal, never pause or remove anything
WINDOW = 20            # judge each combination on its last 20 finished trades
MIN_TRADES = 8         # need at least this many finished trades before judging
PAUSE_BELOW_R = 0.0    # pause when the window total drops below this (in R)
RESUME_AT_R = 1.0      # switch back on when the window total reaches this (in R)
BACKTEST_MIN = 15      # a backtest needs this many trades before it can pause a combo
BACKTEST_BARS = 5000   # candles to replay (about 7 months of forex H1)
CUT_BELOW_R = -5.0     # REMOVE a combination if its backtest total is this bad (in R) ...
CUT_MIN_TRADES = 10    # ... and the backtest had at least this many trades

DERIV_URLS = [  # tried in order; the first one that answers is used
    "wss://api.derivws.com/trading/v1/options/ws/public",  # current public endpoint
    "wss://ws.derivws.com/websockets/v3?app_id=1089",      # older endpoint (fallback)
]
STATE_FILE = Path("state/signals.json")
BACKTEST_FILE = Path("state/backtest.json")
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
    state.setdefault("paused", {})
    for t in state["open"] + state["closed"]:
        t.setdefault("strategy", "EMA Cross")  # trades saved by the first version
        t.setdefault("live", True)             # False = silent shadow trade
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_backtest():
    try:
        return json.loads(BACKTEST_FILE.read_text())
    except Exception:
        return {}


def combo_key(strategy, pair):
    return f"{strategy}|{pair}"


# ---------- Data ----------
def only_closed(df):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return df[df["datetime"] + timedelta(seconds=CANDLE_SECONDS) <= now].reset_index(drop=True)


def fetch_twelvedata(symbol, count=CANDLE_COUNT):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": "1h",
            "outputsize": count,
            "timezone": "UTC",
            "apikey": TD_KEY,
        },
        timeout=60,
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


def deriv_request(url, symbol, count):
    import websocket  # pip package: websocket-client

    ws = websocket.create_connection(url, timeout=30)
    try:
        ws.send(
            json.dumps(
                {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": count,
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


def fetch_deriv(symbol, count=CANDLE_COUNT):
    data, errors = None, []
    for url in DERIV_URLS:
        try:
            data = deriv_request(url, symbol, count)
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
        "live": True,
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
    # Public record: only signals that were actually posted to the channel.
    posted = [t for t in state["closed"] if t.get("live", True)]
    w, l, rate, r = record([t for t in posted if t.get("strategy") == strategy])
    aw, al, arate, ar = record(posted)
    return (
        f"📊 {strategy}: {w}W / {l}L ({rate:.0f}%) | {r:+.1f}R\n"
        f"📊 All strategies: {aw}W / {al}L ({arate:.0f}%) | {ar:+.1f}R"
    )


def walk_trade(side, sl, tp, highs, lows):
    """Return (result, index) for the first candle that hits SL or TP, else (None, None)."""
    for j in range(len(highs)):
        if side == "BUY":
            hit_sl, hit_tp = lows[j] <= sl, highs[j] >= tp
        else:
            hit_sl, hit_tp = highs[j] >= sl, lows[j] <= tp
        if hit_sl:  # if both hit in one candle, count the loss (conservative)
            return "SL", j
        if hit_tp:
            return "TP", j
    return None, None


def resolve_open(state, candles):
    still_open = []
    now = datetime.now(timezone.utc)
    for t in state["open"]:
        df = candles.get(t["pair"])
        if df is None:
            still_open.append(t)
            continue

        after = df[df["datetime"] > pd.Timestamp(t["candle"])]
        result, _ = walk_trade(t["side"], t["sl"], t["tp"], after["high"].values, after["low"].values)

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

        if t.get("live", True):  # shadow trades never message the channel
            label = {"TP": "✅ TP hit", "SL": "❌ SL hit", "EXPIRED": "⏱ Expired"}[result]
            send(
                f"{label}: <b>{t['side']} {t['pair']}</b> ({t['strategy']})\n"
                f"Result: {t['r']:+.1f}R\n\n{stats_line(state, t['strategy'])}"
            )
    state["open"] = still_open


# ---------- Learning layer ----------
def window_trades(state, key):
    """Last WINDOW finished (TP/SL) trades of one combination, live and shadow together."""
    done = [
        t for t in state["closed"]
        if combo_key(t["strategy"], t["pair"]) == key and t["result"] in ("TP", "SL")
    ]
    done.sort(key=lambda t: t.get("closed_at", ""))
    return done[-WINDOW:]


def cut_combos(backtest):
    """Combinations the backtest showed to be clearly losing: removed completely."""
    if not LEARNING:
        return set()
    return {
        key for key, bt in backtest.items()
        if bt["n"] >= CUT_MIN_TRADES and bt["r"] <= CUT_BELOW_R
    }


def combo_active(state, key, backtest):
    """Decide whether a strategy x instrument combination may post to the channel."""
    if not LEARNING:
        return True
    was_paused = state["paused"].get(key, False)
    recent = window_trades(state, key)
    if len(recent) >= MIN_TRADES:  # enough live/shadow evidence: it decides
        total = sum(t["r"] for t in recent)
        paused = (total < RESUME_AT_R) if was_paused else (total < PAUSE_BELOW_R)
        why = f"last {len(recent)} trades = {total:+.1f}R"
    else:  # not enough evidence yet: lean on the backtest, if there is one
        bt = backtest.get(key)
        paused = bool(bt and bt["n"] >= BACKTEST_MIN and bt["r"] < 0)
        why = f"backtest {bt['n']} trades = {bt['r']:+.1f}R" if bt else "no data yet"
    if paused != was_paused:
        print(f"LEARNING: {key} {'PAUSED' if paused else 'RESUMED'} ({why})")
    state["paused"][key] = paused
    return not paused


def print_leaderboard(state, backtest, cuts=frozenset()):
    keys = set(state["paused"]) | set(backtest)
    keys |= {combo_key(t["strategy"], t["pair"]) for t in state["open"] + state["closed"]}
    if not keys:
        return
    print("---- LEARNING LEADERBOARD (strategy | pair) ----")
    rows = []
    for key in sorted(keys):
        recent = window_trades(state, key)
        r_recent = sum(t["r"] for t in recent)
        bt = backtest.get(key)
        bt_txt = f"bt {bt['n']}tr {bt['r']:+.1f}R" if bt else "bt -"
        status = "REMOVED" if key in cuts else ("PAUSED" if state["paused"].get(key) else "active")
        rows.append((key, status, f"{len(recent)}tr {r_recent:+.1f}R", bt_txt))
    for key, status, live_txt, bt_txt in rows:
        print(f"{status:8s} {key:40s} recent {live_txt:14s} {bt_txt}")
    print("-------------------------------------------------")


# ---------- Backtest ----------
def simulate(inst, df):
    """Replay history candle by candle: no peeking, one open trade per strategy at a time."""
    highs, lows = df["high"].values, df["low"].values
    expire = max(1, EXPIRE_HOURS * 3600 // CANDLE_SECONDS)
    trades = []
    for strategy in STRATEGIES:
        func = STRATEGY_FUNCS[strategy]
        busy_until = -1
        for i in range(SLOW_EMA + 6, len(df) - 1):
            if i <= busy_until:
                continue
            sig = func(inst, df.iloc[: i + 1])
            if not sig:
                continue
            end = min(len(df), i + 1 + expire)
            result, j = walk_trade(sig["side"], sig["sl"], sig["tp"], highs[i + 1:end], lows[i + 1:end])
            if result is None:
                if end - (i + 1) < expire:  # history ended before the trade could finish
                    continue
                result, j = "EXPIRED", expire - 1
            busy_until = i + 1 + j
            r = RR if result == "TP" else (-1.0 if result == "SL" else 0.0)
            trades.append({"strategy": strategy, "pair": inst["name"], "i": i, "result": result, "r": r})
    return trades


def summarize(trades, split_index):
    out = {}
    for key in sorted({combo_key(t["strategy"], t["pair"]) for t in trades}):
        mine = [t for t in trades if combo_key(t["strategy"], t["pair"]) == key]
        early = [t for t in mine if t["i"] < split_index[key.split("|")[1]]]
        late = [t for t in mine if t["i"] >= split_index[key.split("|")[1]]]
        w, l, rate, r = record(mine)
        out[key] = {
            "n": w + l, "wins": w, "win_rate": round(rate, 1), "r": round(r, 1),
            "first_70_r": round(sum(t["r"] for t in early), 1), "first_70_n": len(early),
            "last_30_r": round(sum(t["r"] for t in late), 1), "last_30_n": len(late),
        }
    return out


def run_backtest():
    all_trades, split_index = [], {}
    for k, inst in enumerate(INSTRUMENTS):
        try:
            if inst["kind"] == "forex":
                if k:
                    time.sleep(8)
                df = fetch_twelvedata(inst["symbol"], BACKTEST_BARS)
            else:
                df = fetch_deriv(inst["symbol"], BACKTEST_BARS)
            df = add_indicators(df)
        except Exception as e:
            print(f"Could not fetch {inst['name']}: {str(e)[:200]}")
            continue
        split_index[inst["name"]] = int(len(df) * 0.7)
        trades = simulate(inst, df)
        all_trades += trades
        print(f"{inst['name']}: {len(df)} candles, {len(trades)} trades simulated")
    if not all_trades:
        print("Backtest produced no trades. Check your API keys and connections.")
        sys.exit(1)

    result = summarize(all_trades, split_index)
    BACKTEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKTEST_FILE.write_text(json.dumps(result, indent=2))

    print("\n==== BACKTEST RESULTS (R = multiples of the amount risked) ====")
    print(f"{'combination':38s} {'trades':>6s} {'win%':>5s} {'total R':>8s} {'first70% R':>11s} {'last30% R':>10s}")
    for key, v in sorted(result.items(), key=lambda kv: -kv[1]["r"]):
        print(f"{key:38s} {v['n']:6d} {v['win_rate']:5.0f} {v['r']:+8.1f} {v['first_70_r']:+11.1f} {v['last_30_r']:+10.1f}")
    for strategy in STRATEGIES:
        mine = [t for t in all_trades if t["strategy"] == strategy]
        w, l, rate, r = record(mine)
        print(f"ALL {strategy}: {w + l} trades, {rate:.0f}% win rate, {r:+.1f}R")
    print("Tip: a combination that wins in 'first70%' but loses in 'last30%' is probably luck.")


# ---------- Main ----------
def main():
    if "--test" in sys.argv:
        send("✅ Bot is connected and working.")
        print("Test message sent.")
        return
    if "--backtest" in sys.argv:
        run_backtest()
        return

    now = datetime.now(timezone.utc)
    weekend = now.weekday() >= 5

    state = load_state()
    backtest = load_backtest()
    cuts = cut_combos(backtest)
    if cuts:
        print(f"Removed by backtest ({len(cuts)}): " + ", ".join(sorted(cuts)))

    # an instrument is skipped when every strategy on it was removed (unless a trade is still open)
    still_open = {t["pair"] for t in state["open"]}
    todo = []
    for inst in INSTRUMENTS:
        if weekend and inst["kind"] == "forex":
            continue
        wanted = any(combo_key(s, inst["name"]) not in cuts for s in STRATEGIES)
        if wanted or inst["name"] in still_open:
            todo.append(inst)
    if weekend:
        print("Weekend: skipping forex, running synthetic indices only.")
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
            key = combo_key(strategy, inst["name"])
            if key in cuts:
                continue  # removed by the backtest
            if any(t["pair"] == inst["name"] and t["strategy"] == strategy for t in state["open"]):
                continue  # one open trade per instrument per strategy
            sig = STRATEGY_FUNCS[strategy](inst, df)
            if sig and state["last_signal"].get(key) != sig["candle"]:
                sig["live"] = combo_active(state, key, backtest)
                if sig["live"]:
                    send(format_signal(sig))
                else:
                    print(f"Shadow trade (paused, not posted): {key} {sig['side']} @ {sig['entry']}")
                state["open"].append(sig)
                state["last_signal"][key] = sig["candle"]
                save_state(state)

    # keep every combination's pause state fresh, even when it had no new signal
    for strategy in STRATEGIES:
        for inst in INSTRUMENTS:
            key = combo_key(strategy, inst["name"])
            if key not in cuts:
                combo_active(state, key, backtest)
    save_state(state)
    print_leaderboard(state, backtest, cuts)
    print("Run complete.")


if __name__ == "__main__":
    main()
