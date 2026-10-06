"""
Free automated signal bot (Forex + Deriv synthetic indices) with a learning layer.
Live strategy: London Breakout (break of the overnight range at the London open, with a 200 EMA trend filter)
on EUR/USD, GBP/USD and gold. Research strategies (backtest/sweep only): EMA Cross, Price Action, Mean Reversion.
ATR-based stop loss, two targets (TP1 1:3, TP2 1:4). Runs on a schedule via GitHub Actions,
posts to a Telegram channel, and tracks TP/SL results per strategy in state/signals.json.

Learning layer: every strategy x instrument combination has its own rolling record.
A combination that is clearly losing is paused (no more posts to the channel) but keeps
logging silent "shadow" trades, so it can switch itself back on if it recovers.
Run `python signal_bot.py --backtest` to replay history and seed that learning.
Run `python signal_bot.py --sweep` to compare target sizes and trend filters.
Run `python signal_bot.py --news` to post only the news items (brief, alerts, headlines).
Run `python signal_bot.py --nfp` to test how price reacts after past Non-Farm Payrolls releases.
News filter: normal signals pause on the pairs involved around every high-impact release.
"""
import html
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------------- CONFIG (edit these) ----------------
FOREX = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "GBP/JPY", "XAU/USD"]  # XAU/USD = gold

# Deriv synthetic indices are switched OFF (random-number markets: no strategy can have an edge there).
# To bring them back, move the entries from SYNTHETICS_OFF into SYNTHETICS.
SYNTHETICS = {}
SYNTHETICS_OFF = {
    "Volatility 75": "R_75",
    "Volatility 75 (1s)": "1HZ75V",
    "Volatility 25": "R_25",
    "Volatility 25 (1s)": "1HZ25V",
    "Volatility 100": "R_100",
    "Volatility 100 (1s)": "1HZ100V",
    "Volatility 90 (1s)": "1HZ90V",
}

STRATEGIES = ["London Breakout"]  # LIVE: these post to the channel (delete a name to switch it off)
RESEARCH_STRATEGIES = ["EMA Cross", "Price Action", "Mean Reversion"]  # tested by --backtest/--sweep only; move a
                                                                       # name up into STRATEGIES once it proves itself
STRATEGY_PAIRS = {"London Breakout": ["EUR/USD", "GBP/USD", "XAU/USD"]}  # live pairs per strategy (tests use all pairs)
LONDON_FILTER = True   # London Breakout only trades with the 200 EMA trend (price on that side AND the EMA sloping that way)
TREND_EMA = 200
COSTS = True           # backtest/sweep subtract the spread from every trade (as a fraction of the risk)
SPREADS = {            # typical spread in price units (round trip is paid once); check your own broker
    "EUR/USD": 0.00010, "GBP/USD": 0.00015, "USD/JPY": 0.012, "AUD/USD": 0.00012,
    "USD/CAD": 0.00018, "GBP/JPY": 0.025, "XAU/USD": 0.35,
}
ALL_STRATEGIES = STRATEGIES + RESEARCH_STRATEGIES
STRATEGY_KINDS = {"London Breakout": ("forex",), "Mean Reversion": ("forex",)}  # others run on everything
STRATEGY_TARGETS = {"London Breakout": (1.0, 2.0), "Mean Reversion": (1.0, 1.5)}  # (TP1, TP2) in R; others use TP1_R/TP2_R
BB_PERIOD = 20
SESSION_START, SESSION_END = 7, 11   # London Breakout: signals only when the candle opens 07:00-11:00 UTC
ASIA_RANGE_HOURS = 7                 # overnight range = candles opening 00:00-06:00 UTC

CANDLE_SECONDS = 3600  # H1 candles
CANDLE_COUNT = 600     # enough history for the 200 EMA to settle
FAST_EMA, SLOW_EMA = 20, 50
RSI_PERIOD = 14
ATR_PERIOD = 14
SL_ATR_MULT = 1.5      # EMA Cross: stop loss = 1.5 x ATR
TP1_R = 3.0            # first target = 3 x risk (1:3): close half, move stop to break-even
TP2_R = 4.0            # second target = 4 x risk (1:4): the rest of the trade
BRAND = "PIPPULSE"     # name shown at the top of every signal
CHANNEL_LINK = ""      # e.g. "https://t.me/yourchannel" -- leave empty to hide the link line
EXPIRE_HOURS = 240     # close a trade as "expired" after this long (10 days)

# --- learning layer ---
LEARNING = True        # False = post every signal, never pause anything
WINDOW = 20            # judge each combination on its last 20 finished trades
MIN_TRADES = 8         # need at least this many finished trades before judging
PAUSE_BELOW_R = 0.0    # pause when the window total drops below this (in R)
RESUME_AT_R = 1.0      # switch back on when the window total reaches this (in R)
BACKTEST_MIN = 15      # a backtest needs this many trades before it can pause a combo
BACKTEST_BARS = 5000   # candles to replay (about 7 months of forex H1)
CUT_BELOW_R = -5.0     # REMOVE a combination if its backtest total is this bad (in R) ...
CUT_MIN_TRADES = 10    # ... and the backtest had at least this many trades

SWEEP_TARGETS = [(1.0, 1.5), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (3.0, 5.0)]   # (TP1, TP2) pairs the --sweep test compares
SWEEP_FILTERS = ["none", "ema200", "ema200+slope"]    # trend filters the --sweep test compares
SWEEP_FILE = Path("state/sweep.txt")

DERIV_URLS = [  # tried in order; the first one that answers is used
    "wss://api.derivws.com/trading/v1/options/ws/public",  # current public endpoint
    "wss://ws.derivws.com/websockets/v3?app_id=1089",      # older endpoint (fallback)
]

# --- news posts ---
NEWS = True                    # False = signals only
NEWS_FILE = Path("state/news.json")
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = [
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("ForexLive", "https://www.forexlive.com/feed/news"),
]
NEWS_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD"}
BRIEF_HOUR_UTC = 6             # morning brief goes out at the first run after 06:00 UTC (07:00 Nigeria)
ALERT_MINUTES = 90             # warn about high-impact releases up to this long before they happen
BLACKOUT_BEFORE_MIN = 30       # no new signals from this long before a high-impact release ...
BLACKOUT_AFTER_MIN = 90        # ... until this long after it, on the pairs that release affects
NFP_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]   # pairs used by the --nfp test
NFP_PAGES = 7                  # pages of 5000 H1 candles to fetch for the --nfp test (about 5 years)
NFP_FILE = Path("state/nfp.txt")
HEADLINES_PER_RUN = 2
HEADLINES_PER_DAY = 6
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

    df["ema200"] = df["close"].ewm(span=TREND_EMA, adjust=False).mean()
    df["ema200_lag"] = df["ema200"].shift(10)

    mid = df["close"].rolling(BB_PERIOD).mean()
    sd = df["close"].rolling(BB_PERIOD).std(ddof=0)
    df["bb_up"], df["bb_low"] = mid + 2 * sd, mid - 2 * sd
    return df


# ---------- Helpers ----------
def decimals(inst):
    if inst["kind"] == "synthetic":
        return 4
    if "XAU" in inst["name"]:
        return 2
    return 3 if "JPY" in inst["name"] else 5


def unit_size(inst):
    if inst["kind"] == "synthetic":
        return 1.0
    if "XAU" in inst["name"]:
        return 0.1   # gold: 1 pip = 10 cents
    return 0.01 if "JPY" in inst["name"] else 0.0001


def unit_label(inst):
    return "pts" if inst["kind"] == "synthetic" else "pips"


def strategy_applies(strategy, inst):
    """Can this strategy be tested on this instrument?"""
    return inst["kind"] in STRATEGY_KINDS.get(strategy, ("forex", "synthetic"))


def live_ok(strategy, inst):
    """May this strategy post live signals on this instrument?"""
    return strategy_applies(strategy, inst) and inst["name"] in STRATEGY_PAIRS.get(strategy, [i["name"] for i in INSTRUMENTS])


def cost_r(inst, entry, sl):
    """Spread as a fraction of the amount risked (subtracted from backtest results)."""
    risk = abs(entry - sl)
    return min(2.0, SPREADS.get(inst["name"], 0.0) / risk) if COSTS and risk > 0 else 0.0


def build_signal(inst, strategy, side, entry, sl, candle_time):
    risk = abs(entry - sl)
    sign = 1 if side == "BUY" else -1
    r1, r2 = STRATEGY_TARGETS.get(strategy, (TP1_R, TP2_R))
    tp1 = entry + sign * r1 * risk
    tp2 = entry + sign * r2 * risk
    d = decimals(inst)
    return {
        "pair": inst["name"],
        "strategy": strategy,
        "side": side,
        "entry": round(entry, d),
        "sl": round(sl, d),
        "tp1": round(tp1, d),
        "tp2": round(tp2, d),
        "tp1_r": r1,
        "tp2_r": r2,
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


# ---------- Strategy 3 (research): London Breakout ----------
def strategy_london_breakout(inst, df):
    """First candle close beyond the overnight (Asian) range once London opens. Stop at mid-range."""
    if len(df) < 60:
        return None
    last = df.iloc[-1]
    t = last.datetime
    if not (SESSION_START <= t.hour <= SESSION_END):
        return None
    atr = float(last.atr)
    if not atr > 0:
        return None
    recent = df.iloc[-40:]
    today = recent[recent["datetime"].dt.date == t.date()]
    asia = today[today["datetime"].dt.hour < SESSION_START]
    if len(asia) < ASIA_RANGE_HOURS:   # incomplete night (data gap)
        return None
    hi, lo = float(asia["high"].max()), float(asia["low"].min())
    if not (0.5 * atr <= hi - lo <= 4 * atr):   # range must be a sensible size
        return None
    earlier = today[(today["datetime"].dt.hour >= SESSION_START) & (today["datetime"] < t)]
    if ((earlier["close"] > hi) | (earlier["close"] < lo)).any():   # already broke out today
        return None

    entry, mid = float(last.close), (hi + lo) / 2
    if entry > hi:
        side, sl = "BUY", mid - 0.1 * atr
    elif entry < lo:
        side, sl = "SELL", mid + 0.1 * atr
    else:
        return None
    if LONDON_FILTER:   # trade only with the long-term trend
        ema, ema_old = float(last.ema200), float(last.ema200_lag)
        if ema_old != ema_old:   # not enough history yet
            return None
        if side == "BUY" and not (entry > ema and ema > ema_old):
            return None
        if side == "SELL" and not (entry < ema and ema < ema_old):
            return None
    risk = abs(entry - sl)
    if risk > 3 * atr:
        return None
    if risk < 0.5 * atr:
        sl = entry - 0.5 * atr if side == "BUY" else entry + 0.5 * atr
    return build_signal(inst, "London Breakout", side, entry, sl, t)


# ---------- Strategy 4 (research): Mean reversion (Bollinger re-entry) ----------
def strategy_mean_reversion(inst, df):
    """Price closed outside the Bollinger band, then closes back inside: bet on a snap back."""
    if len(df) < BB_PERIOD + 5:
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    atr = float(last.atr)
    if not atr > 0:
        return None
    entry = float(last.close)
    if prev.close < prev.bb_low and last.close > last.bb_low and last.rsi < 45:
        side, sl = "BUY", min(float(prev.low), float(last.low)) - 0.2 * atr
    elif prev.close > prev.bb_up and last.close < last.bb_up and last.rsi > 55:
        side, sl = "SELL", max(float(prev.high), float(last.high)) + 0.2 * atr
    else:
        return None
    risk = abs(entry - sl)
    if risk > 3 * atr:
        return None
    if risk < 0.5 * atr:
        sl = entry - 0.5 * atr if side == "BUY" else entry + 0.5 * atr
    return build_signal(inst, "Mean Reversion", side, entry, sl, last.datetime)


STRATEGY_FUNCS = {
    "EMA Cross": strategy_ema_cross,
    "Price Action": strategy_price_action,
    "London Breakout": strategy_london_breakout,
    "Mean Reversion": strategy_mean_reversion,
}


def format_signal(s, winrate_line):
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    arrow = "📈" if s["side"] == "BUY" else "📉"
    synthetic = s.get("unit") == "pts"
    kind = "SYNTHETIC" if synthetic else "FOREX"
    name = s["pair"] + (" Index" if synthetic else "")
    text = (
        f"{icon} <b>{BRAND} VIP SIGNAL</b>  ·  {kind}\n\n"
        f"<b>Pair:</b> {name}\n"
        f"<b>Direction:</b> {arrow} {s['side']}\n"
        f"<b>Strategy:</b> {s['strategy']}\n\n"
        f"<b>Entry:</b> <code>{s['entry']}</code>\n"
        f"<b>Stop Loss:</b> <code>{s['sl']}</code> ({s['risk_pips']} {s.get('unit', 'pips')})\n"
        f"<b>TP1 (1:{s.get('tp1_r', TP1_R):g}):</b> <code>{s['tp1']}</code>\n"
        f"<b>TP2 (1:{s.get('tp2_r', TP2_R):g}):</b> <code>{s['tp2']}</code>\n\n"
        f"<b>Risk:</b> 1% max per trade\n"
        f"{winrate_line}\n\n"
        f"💡 At TP1 close half and move your stop to entry.\n"
        f"⚠️ Trade at your own risk. Manage your position size. Past performance does not guarantee future results."
    )
    if CHANNEL_LINK:
        text += f"\n\n🔗 <a href=\"{CHANNEL_LINK}\">{BRAND.title()} VIP</a>"
    return text


# ---------- Result tracking ----------
SCORED = ("TP", "TP1", "TP2", "SL")   # "TP" = trades saved by the old single-target version
WINS = ("TP", "TP1", "TP2")


def trade_r(result, tp1_r, tp2_r):
    """Half the trade closes at TP1, the stop then moves to entry, the other half runs to TP2."""
    if result == "SL":
        return -1.0
    if result == "TP1":   # TP1 hit, rest stopped at break-even
        return 0.5 * tp1_r
    if result == "TP2":
        return 0.5 * tp1_r + 0.5 * tp2_r
    return 0.0


def record(trades):
    scored = [t for t in trades if t["result"] in SCORED]
    wins = sum(1 for t in scored if t["result"] in WINS)
    total_r = sum(t["r"] for t in trades)
    n = len(scored)
    rate = (wins / n * 100) if n else 0
    return wins, n - wins, rate, total_r


def winrate_30d_line(state):
    """Real win rate of the signals posted to the channel in the last 30 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent = [t for t in state["closed"]
              if t.get("live", True) and t["result"] in SCORED and t.get("closed_at", "") >= cutoff]
    w, l, rate, r = record(recent)
    if w + l < 10:
        return f"<b>Win Rate (30d):</b> building record ({w + l} closed so far)"
    return f"<b>Win Rate (30d):</b> {rate:.0f}% ({w}W / {l}L, {r:+.1f}R)"


def stats_line(state, strategy):
    # Public record: only signals that were actually posted to the channel.
    posted = [t for t in state["closed"] if t.get("live", True)]
    w, l, rate, r = record([t for t in posted if t.get("strategy") == strategy])
    aw, al, arate, ar = record(posted)
    return (
        f"📊 {strategy}: {w}W / {l}L ({rate:.0f}%) | {r:+.1f}R\n"
        f"📊 All strategies: {aw}W / {al}L ({arate:.0f}%) | {ar:+.1f}R"
    )


def t_score(values):
    """How many standard errors the average result is away from zero (about 2.5+ is worth noticing)."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean / ((var / n) ** 0.5) if var > 0 else 0.0


def walk_trade(side, entry, sl, tp1, tp2, highs, lows):
    """Walk candles forward. Returns (result, index, tp1_hit).
    result: "SL", "TP1" (TP1 hit, rest stopped at break-even), "TP2", or None (still open).
    When one candle could have hit both the stop and a target, the stop is counted (conservative)."""
    buy = side == "BUY"
    stage = 0
    for j in range(len(highs)):
        h, l = highs[j], lows[j]
        if stage == 0:
            if (l <= sl) if buy else (h >= sl):
                return "SL", j, False
            if (h >= tp1) if buy else (l <= tp1):
                stage = 1
                if (h >= tp2) if buy else (l <= tp2):
                    return "TP2", j, True
        else:
            if (l <= entry) if buy else (h >= entry):
                return "TP1", j, True
            if (h >= tp2) if buy else (l <= tp2):
                return "TP2", j, True
    return None, None, stage == 1


def resolve_open(state, candles):
    still_open = []
    now = datetime.now(timezone.utc)
    for t in state["open"]:
        df = candles.get(t["pair"])
        if df is None:
            still_open.append(t)
            continue

        tp1 = t.get("tp1", t.get("tp"))   # old trades only have a single "tp"
        tp2 = t.get("tp2", t.get("tp"))
        a, b = t.get("tp1_r", 4.0), t.get("tp2_r", 4.0)
        after = df[df["datetime"] > pd.Timestamp(t["candle"])]
        result, _, tp1_hit = walk_trade(t["side"], t["entry"], t["sl"], tp1, tp2,
                                        after["high"].values, after["low"].values)

        if result is None:
            opened = datetime.fromisoformat(t["opened_at"])
            if now - opened > timedelta(hours=EXPIRE_HOURS):
                result = "TP1" if tp1_hit else "EXPIRED"

        if result is None:
            if tp1_hit and not t.get("tp1_notified"):
                t["tp1_notified"] = True
                if t.get("live", True):
                    send(
                        f"🎯 <b>TP1 hit:</b> {t['side']} {t['pair']} ({t['strategy']})\n"
                        f"Close half and move your stop to entry. The rest runs to TP2."
                    )
            still_open.append(t)
            continue

        t["result"] = result
        t["r"] = trade_r(result, a, b)
        t["closed_at"] = now.isoformat()
        state["closed"].append(t)

        if t.get("live", True):  # shadow trades never message the channel
            label = {
                "TP2": "✅ TP2 hit (full target)",
                "TP1": "🟡 TP1 hit, rest stopped at break-even",
                "SL": "❌ SL hit",
                "EXPIRED": "⏱ Expired",
            }[result]
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
        if combo_key(t["strategy"], t["pair"]) == key and t["result"] in SCORED
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
    names = {i["name"] for i in INSTRUMENTS}
    keys = {k for k in keys if k.split("|")[0] in STRATEGIES and k.split("|", 1)[1] in names}  # live only
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
    for strategy in ALL_STRATEGIES:
        if not strategy_applies(strategy, inst):
            continue
        func = STRATEGY_FUNCS[strategy]
        busy_until = -1
        for i in range(SLOW_EMA + 6, len(df) - 1):
            if i <= busy_until:
                continue
            sig = func(inst, df.iloc[: i + 1])
            if not sig:
                continue
            end = min(len(df), i + 1 + expire)
            result, j, tp1_hit = walk_trade(sig["side"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"],
                                            highs[i + 1:end], lows[i + 1:end])
            if result is None:
                if end - (i + 1) < expire:  # history ended before the trade could finish
                    continue
                result, j = ("TP1" if tp1_hit else "EXPIRED"), expire - 1
            busy_until = i + 1 + j
            r = trade_r(result, sig["tp1_r"], sig["tp2_r"]) - cost_r(inst, sig["entry"], sig["sl"])
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
    for strategy in ALL_STRATEGIES:
        mine = [t for t in all_trades if t["strategy"] == strategy]
        w, l, rate, r = record(mine)
        print(f"ALL {strategy}{'' if strategy in STRATEGIES else ' (research)'}: {w + l} trades, {rate:.0f}% win rate, {r:+.1f}R")
    print("Tip: a combination that wins in 'first70%' but loses in 'last30%' is probably luck.")


# ---------- Sweep: compare R:R values and trend filters ----------
def trend_ok(filt, side, close, ema, ema_old):
    """Only allow trades that go with the long-term (200 EMA) trend."""
    if filt == "none":
        return True
    up, down = close > ema, close < ema
    if filt == "ema200+slope":  # the 200 EMA itself must also be sloping the same way
        up, down = up and ema > ema_old, down and ema < ema_old
    return up if side == "BUY" else down


def run_sweep():
    data = []
    for k, inst in enumerate(INSTRUMENTS):
        try:
            if inst["kind"] == "forex":
                if k:
                    time.sleep(8)
                df = fetch_twelvedata(inst["symbol"], BACKTEST_BARS)
            else:
                df = fetch_deriv(inst["symbol"], BACKTEST_BARS)
            data.append((inst, add_indicators(df).reset_index(drop=True)))
        except Exception as e:
            print(f"Could not fetch {inst['name']}: {str(e)[:200]}")
    if not data:
        print("Sweep found no data. Check your API keys and connections.")
        sys.exit(1)

    global LONDON_FILTER
    LONDON_FILTER = False   # the sweep applies the trend filters itself, so compare them fairly
    expire = max(1, EXPIRE_HOURS * 3600 // CANDLE_SECONDS)
    rows = {}  # (kind, strategy, filter, (tp1, tp2)) -> list of (is_early, r, won)
    for inst, df in data:
        highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
        ema = df["close"].ewm(span=TREND_EMA, adjust=False).mean().values
        split = int(len(df) * 0.7)
        start = TREND_EMA + 10
        for strategy in ALL_STRATEGIES:
            if not strategy_applies(strategy, inst):
                continue
            func = STRATEGY_FUNCS[strategy]
            cands = []
            for i in range(start, len(df) - 1):
                sig = func(inst, df.iloc[: i + 1])
                if sig:
                    cands.append((i, sig))
            for filt in SWEEP_FILTERS:
                kept = [(i, sg) for i, sg in cands
                        if trend_ok(filt, sg["side"], closes[i], ema[i], ema[i - 10])]
                for tgt in SWEEP_TARGETS:
                    a, b = tgt
                    out = rows.setdefault((inst["kind"], strategy, filt, tgt), [])
                    busy = -1
                    for i, sg in kept:
                        if i <= busy:
                            continue
                        risk = abs(sg["entry"] - sg["sl"])
                        if risk <= 0:
                            continue
                        sign = 1 if sg["side"] == "BUY" else -1
                        tp1, tp2 = sg["entry"] + sign * a * risk, sg["entry"] + sign * b * risk
                        end = min(len(df), i + 1 + expire)
                        result, j, tp1_hit = walk_trade(sg["side"], sg["entry"], sg["sl"], tp1, tp2,
                                                        highs[i + 1:end], lows[i + 1:end])
                        if result is None:
                            if end - (i + 1) < expire:
                                continue
                            result, j = ("TP1" if tp1_hit else "EXPIRED"), expire - 1
                        busy = i + 1 + j
                        r = trade_r(result, a, b) - cost_r(inst, sg["entry"], sg["sl"])
                        out.append((i < split, r, r > 0))
        print(f"{inst['name']}: swept {len(df)} candles")

    lines = ["==== SWEEP (R = multiples of the amount risked" + (", AFTER typical spreads) ====" if COSTS else ") ====")]
    lines.append("Half closes at TP1, stop moves to entry, rest runs to TP2. Each row pools every instrument of that type.")
    lines.append("'t' says how real a result is (under 2 = could easily be luck). 'good' needs t of 3+, positive in BOTH the")
    lines.append("early 70% and the late 30% of history, and 30+ trades.")
    for kind in ("forex", "synthetic"):
        for strategy in ALL_STRATEGIES:
            if kind not in STRATEGY_KINDS.get(strategy, ("forex", "synthetic")):
                continue
            if not any(k[0] == kind and k[1] == strategy and v for k, v in rows.items()):
                continue
            lines.append("")
            lines.append(f"-- {kind.upper()} / {strategy}{'' if strategy in STRATEGIES else ' (research)'} --")
            lines.append(f"{'filter':13s} {'TP1/TP2':>8s} {'trades':>6s} {'win%':>5s} {'total R':>8s} {'R/trade':>8s} {'t':>5s} {'early R':>8s} {'late R':>8s}")
            for filt in SWEEP_FILTERS:
                for tgt in SWEEP_TARGETS:
                    t = rows.get((kind, strategy, filt, tgt), [])
                    if not t:
                        continue
                    n = len(t)
                    wins = sum(1 for x in t if x[2])
                    total = sum(x[1] for x in t)
                    early = sum(x[1] for x in t if x[0])
                    late = sum(x[1] for x in t if not x[0])
                    ts = t_score([x[1] for x in t])
                    good = "  good" if early > 0 and late > 0 and n >= 30 and ts >= 3 else ""
                    lines.append(
                        f"{filt:13s} {tgt[0]:g}/{tgt[1]:g}".ljust(22)
                        + f" {n:6d} {100 * wins / n:5.0f} {total:+8.1f} {total / n:+8.2f} {ts:5.1f} {early:+8.1f} {late:+8.1f}{good}"
                    )
    lines.append("")
    lines.append(("Typical spreads ARE subtracted; slippage is not. " if COSTS else "Spreads and slippage are NOT included. ")
                 + "Many variants were tried, so a lucky winner is likely.")
    lines.append("Trust only rows marked good, and only when neighbouring rows look similar.")
    text = "\n".join(lines)
    print(text)
    SWEEP_FILE.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_FILE.write_text(text + "\n")


# ---------- NFP research: how does price behave after Non-Farm Payrolls? ----------
def fetch_twelvedata_pages(symbol, pages):
    """Fetch several pages of 5000 H1 candles going further back in time (slow: obeys the rate limit)."""
    frames, end = [], None
    for page in range(pages):
        params = {"symbol": symbol, "interval": "1h", "outputsize": 5000, "timezone": "UTC", "apikey": TD_KEY}
        if end:
            params["end_date"] = end
        data = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=90).json()
        if "values" not in data:
            if page == 0:
                raise RuntimeError(data.get("message", str(data)))
            break
        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df)
        if len(df) < 100:
            break
        end = df["datetime"].min().strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(8)
    df = pd.concat(frames).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return only_closed(df)


def nfp_release_times(first, last):
    """NFP comes out at 08:30 New York time on the 3rd Friday after the week containing the 12th,
    moved around holidays (Jan 1-3 -> a week later, July 4th weekend -> the Thursday before)."""
    ny, out = ZoneInfo("America/New_York"), []
    year, month = first.year, first.month - 1      # start one month early: release comes the month after
    if month == 0:
        year, month = year - 1, 12
    while (year, month) <= (last.year, last.month):
        twelfth = datetime(year, month, 12)
        ref_saturday = twelfth + timedelta(days=(5 - twelfth.weekday()) % 7)
        day = ref_saturday + timedelta(days=20)
        if day.month == 1 and day.day <= 3:
            day += timedelta(days=7)
        elif day.month == 7 and day.day in (3, 4):   # Jul 4 on a Friday, or the Friday off when Jul 4 is a Saturday
            day -= timedelta(days=1)
        release = datetime(day.year, day.month, day.day, 8, 30, tzinfo=ny).astimezone(timezone.utc)
        if first <= release.replace(tzinfo=None) <= last:
            out.append(release)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def run_nfp():
    NFP_TARGETS = [(1.0, 1.5), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
    horizon = 24                       # follow each trade for up to 24 hours
    trades = {}                        # (rule, target) -> list of (event_order, r, win)
    used, per_pair = 0, {}
    for k, name in enumerate(NFP_PAIRS):
        try:
            if k:
                time.sleep(8)
            df = add_indicators(fetch_twelvedata_pages(name, NFP_PAGES)).reset_index(drop=True)
        except Exception as e:
            print(f"Could not fetch {name}: {str(e)[:200]}")
            continue
        highs, lows = df["high"].values, df["low"].values
        times = list(df["datetime"])
        events = nfp_release_times(df["datetime"].iloc[0].to_pydatetime(), df["datetime"].iloc[-1].to_pydatetime())
        count = 0
        for n, rel in enumerate(events):
            hour = pd.Timestamp(rel.replace(minute=0, second=0, tzinfo=None))
            idx = df.index[df["datetime"] == hour]
            if len(idx) == 0:
                continue
            i = int(idx[0])
            if i < 30 or i + 1 + horizon >= len(df):
                continue
            atr = float(df["atr"].iloc[i - 1])
            body = float(df["close"].iloc[i] - df["open"].iloc[i])
            if not atr > 0 or abs(body) < 0.5 * atr:      # the first hour must show a clear move
                continue
            move = 1 if body > 0 else -1
            entry = float(df["close"].iloc[i])
            risk = 1.5 * atr
            count += 1
            for rule, direction in (("follow the move", move), ("fade the move", -move)):
                side = "BUY" if direction > 0 else "SELL"
                for a, b in NFP_TARGETS:
                    sl = entry - direction * risk
                    tp1, tp2 = entry + direction * a * risk, entry + direction * b * risk
                    result, j, tp1_hit = walk_trade(side, entry, sl, tp1, tp2,
                                                    highs[i + 1:i + 1 + horizon], lows[i + 1:i + 1 + horizon])
                    if result is None:   # still open after 24h: close at the market
                        last_close = float(df["close"].iloc[i + horizon])
                        r = max(-1.0, min(b, direction * (last_close - entry) / risk))
                    else:
                        r = trade_r(result, a, b)
                    trades.setdefault((rule, (a, b)), []).append((rel, r, r > 0))
        per_pair[name] = (len(events), count)
        used += count
        print(f"{name}: {len(df)} candles, {count} usable NFP releases")
    if not trades:
        print("NFP test found no data.")
        sys.exit(1)

    lines = ["==== NFP TEST (R = multiples of the amount risked) ====",
             "After each NFP the bot looks at the first hour's move (must be at least half an ATR) and either",
             "FOLLOWS it or FADES it. Stop = 1.5 ATR. Half closes at TP1, stop to entry, rest runs to TP2.",
             f"Pairs: {', '.join(NFP_PAIRS)}. Usable releases per pair: "
             + ", ".join(f"{p} {c}" for p, (_, c) in per_pair.items()),
             "'t' says how real the result is (under 2 = could easily be luck). 'good' needs t of 2.5+, positive in BOTH the",
             "earlier 70% and later 30% of releases, and 30+ releases.", "",
             f"{'rule':16s} {'TP1/TP2':>8s} {'trades':>6s} {'win%':>5s} {'total R':>8s} {'R/trade':>8s} {'t':>5s} {'early R':>8s} {'late R':>8s}"]
    for rule in ("follow the move", "fade the move"):
        for tgt in NFP_TARGETS:
            t = sorted(trades.get((rule, tgt), []), key=lambda x: x[0])
            if not t:
                continue
            cut = int(len(t) * 0.7)
            early, late = sum(x[1] for x in t[:cut]), sum(x[1] for x in t[cut:])
            total, n = sum(x[1] for x in t), len(t)
            wins = sum(1 for x in t if x[2])
            by_event = {}
            for x in t:                       # same-day trades on different pairs move together: count each release once
                by_event.setdefault(x[0], []).append(x[1])
            ts = t_score([sum(v) / len(v) for v in by_event.values()])
            good = "  good" if early > 0 and late > 0 and len(by_event) >= 30 and ts >= 2.5 else ""
            lines.append(f"{rule:16s} {tgt[0]:g}/{tgt[1]:g}".ljust(25)
                         + f" {n:6d} {100 * wins / n:5.0f} {total:+8.1f} {total / n:+8.2f} {ts:5.1f} {early:+8.1f} {late:+8.1f}{good}")
    lines += ["", "Spreads and slippage are NOT included, and they are large around NFP. Only a clear, consistent 'good'",
              "result (with neighbouring rows looking similar) would justify trading NFP."]
    text = "\n".join(lines)
    print(text)
    NFP_FILE.parent.mkdir(parents=True, exist_ok=True)
    NFP_FILE.write_text(text + "\n")


# ---------- News posts ----------
FLAGS = {"USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "AUD": "🇦🇺", "CAD": "🇨🇦"}
WAT = timezone(timedelta(hours=1))   # Nigeria time
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; pippulse-bot)"}


def load_news():
    try:
        news = json.loads(NEWS_FILE.read_text())
    except Exception:
        news = {}
    news.setdefault("brief_date", "")
    news.setdefault("alerted", [])
    news.setdefault("seen", [])
    news.setdefault("day", "")
    news.setdefault("day_count", 0)
    return news


def save_news(news):
    NEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    news["alerted"] = news["alerted"][-200:]
    news["seen"] = news["seen"][-500:]
    NEWS_FILE.write_text(json.dumps(news, indent=2))


def fetch_calendar():
    """High-impact releases for the major currencies this week, times in UTC."""
    r = requests.get(CALENDAR_URL, headers=HTTP_HEADERS, timeout=20)
    r.raise_for_status()
    events = []
    for e in r.json():
        if e.get("impact") != "High" or e.get("country") not in NEWS_CURRENCIES:
            continue
        when = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        events.append({
            "id": f"{e['date']}|{e['country']}|{e['title']}",
            "time": when, "ccy": e["country"], "title": e["title"],
            "forecast": e.get("forecast") or "", "previous": e.get("previous") or "",
        })
    return sorted(events, key=lambda x: x["time"])


CURRENT_EVENTS = []   # high-impact releases this week; filled by run_news, used to pause signals


def pair_currencies(inst):
    return set(inst["name"].split("/"))


def blackout_event(inst, now):
    """The high-impact release (if any) that currently makes it unwise to signal this pair."""
    ccys = pair_currencies(inst)
    for e in CURRENT_EVENTS:
        if e["ccy"] in ccys and (
            e["time"] - timedelta(minutes=BLACKOUT_BEFORE_MIN) <= now <= e["time"] + timedelta(minutes=BLACKOUT_AFTER_MIN)
        ):
            return e
    return None


def is_nfp(e):
    return "non-farm" in e["title"].lower()


def affected_pairs(e):
    return [i["name"] for i in INSTRUMENTS if e["ccy"] in pair_currencies(i)]


def event_line(e):
    extra = []
    if e["forecast"]:
        extra.append(f"forecast {e['forecast']}")
    if e["previous"]:
        extra.append(f"previous {e['previous']}")
    tail = f" ({', '.join(extra)})" if extra else ""
    flag = FLAGS.get(e["ccy"], "")
    return f"• {e['time'].astimezone(WAT):%H:%M} {flag} {e['ccy']} {html.escape(e['title'])}{html.escape(tail)}"


def fetch_headlines():
    """Newest headlines from the free RSS feeds. Headline + link only."""
    per_feed = []
    for name, url in FEEDS:
        items = []
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=20)
            r.raise_for_status()
            for item in ET.fromstring(r.content).iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if title and link:
                    items.append({"source": name, "title": title, "link": link})
        except Exception as e:
            print(f"News feed {name} unavailable: {str(e)[:100]}")
        per_feed.append(items[:15])
    mixed = []  # interleave so one feed does not dominate
    for k in range(15):
        for items in per_feed:
            if k < len(items):
                mixed.append(items[k])
    return mixed


def run_news(force=False):
    news = load_news()
    now = datetime.now(timezone.utc)
    local = now.astimezone(WAT)          # dates shown to subscribers use Nigeria time
    today = local.strftime("%Y-%m-%d")

    # 1) calendar: morning brief + "big news soon" warnings
    try:
        events = fetch_calendar()
    except Exception as e:
        events = None
        print(f"Calendar unavailable: {str(e)[:100]}")
    if events is not None:
        CURRENT_EVENTS[:] = events
        todays = [e for e in events if e["time"].astimezone(WAT).strftime("%Y-%m-%d") == today]
        brief_due = force or (now.weekday() < 5 and BRIEF_HOUR_UTC <= now.hour < 20)
        if brief_due and news["brief_date"] != today:
            lines = [f"🗞 <b>{BRAND} MORNING BRIEF</b> · {local:%a %d %b}", ""]
            if todays:
                lines.append("High-impact releases today (Nigeria time):")
                lines += [event_line(e) for e in todays]
                lines += ["", f"Prices can jump around these. We pause new signals on the pairs involved "
                          f"from {BLACKOUT_BEFORE_MIN} min before to {BLACKOUT_AFTER_MIN} min after each release."]
            else:
                lines.append("No high-impact releases on the major currencies today. A quieter day.")
            send("\n".join(lines))
            news["brief_date"] = today
        for e in events:
            mins = (e["time"] - now).total_seconds() / 60
            if 0 < mins <= ALERT_MINUTES and e["id"] not in news["alerted"]:
                until = (e["time"] + timedelta(minutes=BLACKOUT_AFTER_MIN)).astimezone(WAT)
                pairs = ", ".join(affected_pairs(e)) or e["ccy"]
                extra = ("NFP is the biggest mover of the month, so expect wild swings. "
                         if is_nfp(e) else "")
                send(f"⚠️ <b>High-impact news in about {int(mins)} min</b>\n{event_line(e)}\n\n"
                     f"{extra}Spreads widen and price can jump, so we are NOT trading it. "
                     f"New signals on {pairs} are paused until {until:%H:%M} (Nigeria time).")
                news["alerted"].append(e["id"])

    # 2) headlines (daytime only, capped so the channel is not spammed)
    if news["day"] != today:
        news["day"], news["day_count"] = today, 0
    if force or 6 <= now.hour < 21:
        items = fetch_headlines()
        first_run = not news["seen"]
        fresh = [h for h in items if h["link"] not in news["seen"]]
        posted = 0
        for h in fresh:
            if posted >= HEADLINES_PER_RUN or news["day_count"] >= HEADLINES_PER_DAY:
                break
            send(f"📰 <b>Market Pulse</b> ({h['source']})\n{html.escape(h['title'])}\n{h['link']}")
            posted += 1
            news["day_count"] += 1
        # remember everything we saw, so an old backlog never drips out later
        news["seen"] += [h["link"] for h in fresh]
        if first_run:
            print(f"News: first run, posted {posted} headline(s) and marked the rest as seen.")
    save_news(news)


def run_news_safe(force=False):
    if not NEWS:
        return
    try:
        run_news(force)
    except Exception as e:  # news must never stop the signals
        print(f"News step failed: {str(e)[:150]}")


def retire_removed_instruments(state):
    """Close (without scoring) any open trade on an instrument that has been switched off."""
    names = {i["name"] for i in INSTRUMENTS}
    gone = [t for t in state["open"] if t["pair"] not in names]
    if not gone:
        return
    now = datetime.now(timezone.utc).isoformat()
    for t in gone:
        t["result"], t["r"], t["closed_at"] = "EXPIRED", 0.0, now
        state["closed"].append(t)
    state["open"] = [t for t in state["open"] if t["pair"] in names]
    if any(t.get("live", True) for t in gone):
        send("ℹ️ Synthetic index signals have been discontinued. Any open synthetic trade is no longer tracked.")
    save_state(state)


# ---------- Timeframe test: London Breakout with 30 / 15 minute entries ----------
TF_PAIRS = ["EUR/USD", "GBP/USD", "XAU/USD"]
TF_MINUTES = [60, 30, 15]
TF_PAGES = {60: 3, 30: 6, 15: 10}   # pages of 5000 candles (about 1 to 2 years each)
TF_FILE = Path("state/tf.txt")


def fetch_tf(symbol, minutes, pages):
    """Several pages of candles at any size, going back in time (slow: obeys the rate limit)."""
    interval = {60: "1h", 30: "30min", 15: "15min"}[minutes]
    frames, end = [], None
    for page in range(pages):
        params = {"symbol": symbol, "interval": interval, "outputsize": 5000, "timezone": "UTC", "apikey": TD_KEY}
        if end:
            params["end_date"] = end
        data = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=90).json()
        if "values" not in data:
            if page == 0:
                raise RuntimeError(data.get("message", str(data)))
            break
        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        frames.append(df)
        if len(df) < 100:
            break
        end = df["datetime"].min().strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(8)
    df = pd.concat(frames).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    now = datetime.utcnow()
    return df[df["datetime"] + timedelta(minutes=minutes) <= now].reset_index(drop=True)


def tf_breakout_trades(inst, df, minutes):
    """London Breakout on any candle size. Range, ATR and trend are always the same H1-based ones,
    so only the ENTRY candle changes (the first 30/15 minute close beyond the overnight range)."""
    k = 60 // minutes
    h1 = df.set_index("datetime").resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    prev_close = h1["close"].shift(1)
    tr = pd.concat([h1["high"] - h1["low"], (h1["high"] - prev_close).abs(), (h1["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr_h = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean().shift(1)     # last COMPLETED hour only
    ema_h = h1["close"].ewm(span=TREND_EMA, adjust=False).mean().shift(1)
    ema_h_old = ema_h.shift(10)
    hour_idx = df["datetime"].dt.floor("1h")
    atr = atr_h.reindex(hour_idx).values
    ema = ema_h.reindex(hour_idx).values
    ema_old = ema_h_old.reindex(hour_idx).values
    dts, highs, lows, closes = df["datetime"], df["high"].values, df["low"].values, df["close"].values
    days = dts.dt.date.values
    hours = dts.dt.hour.values
    expire = max(1, EXPIRE_HOURS * 60 // minutes)
    out, busy = [], -1
    minute_of_day = hours * 60 + dts.dt.minute.values
    split = int(len(df) * 0.7)
    a, b = STRATEGY_TARGETS["London Breakout"]
    starts = {}
    for i, d in enumerate(days):
        starts.setdefault(d, []).append(i)
    for day, idx in starts.items():
        idx = np.array(idx)
        asia = idx[hours[idx] < SESSION_START]
        sess = idx[(minute_of_day[idx] >= SESSION_START * 60) & (minute_of_day[idx] <= SESSION_END * 60)]
        if len(asia) < ASIA_RANGE_HOURS * k or len(sess) == 0:
            continue
        hi, lo = highs[asia].max(), lows[asia].min()
        i = None
        for c in sess:
            if closes[c] > hi or closes[c] < lo:
                i = c
                break
        if i is None or i <= busy or i >= len(df) - 1:
            continue
        at, e, eo = atr[i], ema[i], ema_old[i]
        if not (at > 0) or e != e or eo != eo:
            continue
        if not (0.5 * at <= hi - lo <= 4 * at):
            continue
        entry, mid = closes[i], (hi + lo) / 2
        side = "BUY" if entry > hi else "SELL"
        sl = mid - 0.1 * at if side == "BUY" else mid + 0.1 * at
        if side == "BUY" and not (entry > e and e > eo):
            continue
        if side == "SELL" and not (entry < e and e < eo):
            continue
        risk = abs(entry - sl)
        if risk > 3 * at:
            continue
        if risk < 0.5 * at:
            sl = entry - 0.5 * at if side == "BUY" else entry + 0.5 * at
        risk = abs(entry - sl)
        sign = 1 if side == "BUY" else -1
        end = min(len(df), i + 1 + expire)
        result, j, tp1_hit = walk_trade(side, entry, sl, entry + sign * a * risk, entry + sign * b * risk,
                                        highs[i + 1:end], lows[i + 1:end])
        if result is None:
            if end - (i + 1) < expire:
                continue
            result, j = ("TP1" if tp1_hit else "EXPIRED"), expire - 1
        busy = i + 1 + j
        r = trade_r(result, a, b) - cost_r(inst, entry, sl)
        out.append((i < split, r, r > 0))
    return out


def run_tf():
    results = {}   # minutes -> list of (is_early, r, won)
    for inst in INSTRUMENTS:
        if inst["name"] not in TF_PAIRS:
            continue
        for minutes in TF_MINUTES:
            try:
                time.sleep(8)
                df = fetch_tf(inst["symbol"], minutes, TF_PAGES[minutes])
            except Exception as e:
                print(f"Could not fetch {inst['name']} {minutes}m: {str(e)[:150]}")
                continue
            trades = tf_breakout_trades(inst, df, minutes)
            results.setdefault(minutes, []).extend(trades)
            print(f"{inst['name']} {minutes}m: {len(df)} candles, {len(trades)} trades")
    lines = ["==== TIMEFRAME TEST: London Breakout, 200-EMA+slope filter, TP1 1:1 / TP2 1:2, after spreads ====",
             "Range, ATR and trend are the same H1 ones everywhere. Only the entry candle (60 / 30 / 15 min) changes.",
             "Different timeframes cover different amounts of history, so compare R/trade and t, not total R.", "",
             f"{'entry':>6s} {'trades':>6s} {'win%':>5s} {'total R':>8s} {'R/trade':>8s} {'t':>5s} {'early R':>8s} {'late R':>8s}"]
    for minutes in TF_MINUTES:
        t = results.get(minutes, [])
        if not t:
            continue
        n = len(t)
        wins = sum(1 for x in t if x[2])
        total = sum(x[1] for x in t)
        early = sum(x[1] for x in t if x[0])
        late = sum(x[1] for x in t if not x[0])
        ts = t_score([x[1] for x in t])
        good = "  good" if early > 0 and late > 0 and n >= 30 and ts >= 3 else ""
        lines.append(f"{minutes:>4d}m {n:6d} {100 * wins / n:5.0f} {total:+8.1f} {total / n:+8.2f} {ts:5.1f} {early:+8.1f} {late:+8.1f}{good}")
    lines += ["", "Spreads are subtracted; slippage is not. Lower timeframes pay the spread more often per move,",
              "so a 15/30 minute row only matters if it clearly beats the 60 minute row."]
    text = "\n".join(lines)
    print(text)
    TF_FILE.parent.mkdir(parents=True, exist_ok=True)
    TF_FILE.write_text(text + "\n")


# ---------- Main ----------
def main():
    if "--test" in sys.argv:
        send("✅ Bot is connected and working.")
        print("Test message sent.")
        return
    if "--backtest" in sys.argv:
        run_backtest()
        return
    if "--sweep" in sys.argv:
        run_sweep()
        return
    if "--news" in sys.argv:
        run_news_safe(force=True)
        print("News step done.")
        return
    if "--nfp" in sys.argv:
        run_nfp()
        return
    if "--tf" in sys.argv:
        run_tf()
        return

    run_news_safe()

    now = datetime.now(timezone.utc)
    weekend = now.weekday() >= 5

    state = load_state()
    retire_removed_instruments(state)
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
        wanted = any(live_ok(s, inst) and combo_key(s, inst["name"]) not in cuts for s in STRATEGIES)
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
            if key in cuts or not live_ok(strategy, inst):
                continue  # removed by the backtest, or not a live pair for this strategy
            ev = blackout_event(inst, now)
            if ev:
                print(f"News pause: no {inst['name']} signals around {ev['ccy']} {ev['title']}")
                continue
            if any(t["pair"] == inst["name"] and t["strategy"] == strategy for t in state["open"]):
                continue  # one open trade per instrument per strategy
            sig = STRATEGY_FUNCS[strategy](inst, df)
            if sig and state["last_signal"].get(key) != sig["candle"]:
                sig["live"] = combo_active(state, key, backtest)
                if sig["live"]:
                    send(format_signal(sig, winrate_30d_line(state)))
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
