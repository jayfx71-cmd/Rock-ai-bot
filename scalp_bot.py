"""Fast-scalp signal generator for gold (XAU/USD). Random direction, 1:2 or 1:3 targets, about 60 a day,
split into morning / afternoon / evening sessions. Posts to your channel (TELEGRAM_CHAT_ID), or to a separate chat if you add a SCALP_CHAT_ID secret.
Run every 30 minutes on weekdays: python scalp_bot.py"""
import json, os, random, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import signal_bot as b

SYMBOL = "XAU/USD"
STATE = Path("state/scalp.json")
PER_SESSION = 20                         # 3 sessions x 20 = 60 a day
# UTC (Nigeria time = UTC+1). A 30-minute break closes the morning and the afternoon; the evening runs straight to the end.
SESSIONS = {"MORNING":   (6 * 60, 10 * 60 + 30),     # 07:00 - 11:30 Nigeria
            "AFTERNOON": (11 * 60, 15 * 60 + 30),    # 12:00 - 16:30
            "EVENING":   (16 * 60, 21 * 60)}         # 17:00 - 22:00
RATIOS = [2.0, 3.0]
MAX_PER_RUN = 4
GAP_MIN, GAP_MAX = 240, 420              # seconds between signals inside one run
EXPIRE_MIN = 90                          # a scalp not finished in 90 minutes is closed at the market price
SHOW_SESSION_SUMMARY = False   # the recap card shows the day's winning trades only; the full record stays in state/scalp.json
RECAP_AFTER_MIN = 21 * 60 + 10   # 22:10 Nigeria time: open scalps are closed at the market price and the recap goes out


def load():
    if STATE.exists():
        s = json.loads(STATE.read_text())
    else:
        s = {}
    s.setdefault("day", ""); s.setdefault("count", {}); s.setdefault("open", []); s.setdefault("closed", [])
    s.setdefault("summary_sent", [])
    return s


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["closed"] = s["closed"][-600:]
    STATE.write_text(json.dumps(s, indent=1))


def session_of(now):
    m = now.hour * 60 + now.minute
    for name, (a, c) in SESSIONS.items():
        if a <= m < c:
            return name, a, c
    return None, 0, 0


def fetch():
    df = b.fetch_twelvedata(SYMBOL, 120, "5min", 300)
    return b.only_closed(df, 300)


def live_price(fallback):
    """Latest price right now (not the last closed candle, which can be minutes old)."""
    try:
        r = b.requests.get("https://api.twelvedata.com/price", params={"symbol": SYMBOL, "apikey": b.TD_KEY}, timeout=20)
        return float(r.json()["price"])
    except Exception as e:
        print(f"Live price failed, using the last candle close: {str(e)[:100]}")
        return fallback


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def make_trade(df, session, number):
    entry = live_price(float(df["close"].iloc[-1]))
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    started = now_naive - timedelta(minutes=now_naive.minute % 5)   # start of the 5-minute candle we are in
    risk = max(round(atr(df) * 1.0, 2), 1.5)           # stop about one 5-minute ATR, never tighter than $1.50
    ratio = random.choice(RATIOS)
    side = random.choice(["BUY", "SELL"])
    sign = 1 if side == "BUY" else -1
    return {"n": number, "session": session, "side": side, "entry": round(entry, 2),
            "sl": round(entry - sign * risk, 2), "tp": round(entry + sign * risk * ratio, 2),
            "ratio": ratio, "risk": risk, "opened_at": datetime.now(timezone.utc).isoformat(),
            "candle": started.isoformat()}


def make_scalp_image_basic(tr, path="scalp.png"):
    """Plain backup card (used only if the main card fails): dark theme, white text, a little robot. Only the BUY / SELL button is coloured."""
    plt = b._plt()
    from matplotlib.patches import FancyBboxPatch, Circle
    W, H = 6.4, 5.4
    BG, PANEL, EDGE, FG, DIM = "#08090b", "#111317", "#2a2d33", "#ffffff", "#9aa0a8"
    GREEN, RED = "#1ed760", "#ff3b4e"
    col = GREEN if tr["side"] == "BUY" else RED
    mono, sans = "DejaVu Sans Mono", "DejaVu Sans"
    fig = plt.figure(figsize=(W, H), dpi=150, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    def box(x, y, w, h, fc, ec="none", lw=1.0, r=0.12, z=2):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw, zorder=z))

    box(0.25, 0.25, W - 0.5, H - 0.5, PANEL, EDGE, 1.2, 0.2, 1)
    # header
    ax.text(0.55, H - 0.7, "ROCK AI", color=FG, fontsize=17, fontweight="bold", va="center", family=sans, zorder=5)
    ax.plot([0.55, 0.85, 1.0, 1.25], [H - 0.98, H - 0.98, H - 0.9, H - 0.86], color=FG, lw=1.6, zorder=5)
    ax.text(W - 0.55, H - 0.7, f"#{tr['n']:02d} / 60", color=DIM, fontsize=10, fontweight="bold", ha="right", va="center", family=mono, zorder=5)
    ax.text(W - 0.55, H - 1.0, f"{tr['session']} SESSION", color=DIM, fontsize=7.5, ha="right", va="center", family=mono, zorder=5)
    ax.plot([0.5, W - 0.5], [H - 1.25, H - 1.25], color=EDGE, lw=1, zorder=3)

    # robot (right side): head, antenna, ears, eyes, mouth
    rx, ry = 4.25, H - 3.2
    ax.plot([rx + 0.8, rx + 0.8], [ry + 1.55, ry + 1.85], color=FG, lw=1.8, zorder=4)
    ax.add_patch(Circle((rx + 0.8, ry + 1.92), 0.07, fc=FG, ec="none", zorder=4))
    box(rx - 0.12, ry + 0.5, 0.12, 0.45, "#1d2026", FG, 1.0, 0.04, 3)
    box(rx + 1.6, ry + 0.5, 0.12, 0.45, "#1d2026", FG, 1.0, 0.04, 3)
    box(rx, ry, 1.6, 1.55, "#15181d", FG, 1.6, 0.28, 3)
    box(rx + 0.12, ry + 0.52, 1.36, 0.82, "#050607", EDGE, 1.0, 0.18, 3.2)
    for ex in (rx + 0.28, rx + 0.92):
        box(ex, ry + 0.78, 0.4, 0.3, FG, "none", 1, 0.1, 4)
    for k in range(5):
        box(rx + 0.32 + k * 0.2, ry + 0.15, 0.13, 0.2, DIM if k % 2 else FG, "none", 1, 0.03, 4)

    # BUY / SELL button
    arrow = "\u25b2" if tr["side"] == "BUY" else "\u25bc"
    box(0.55, H - 2.5, 3.3, 0.95, col, "none", 1, 0.2, 4)
    ax.text(2.2, H - 2.02, f"{arrow}  {tr['side']}", color="#ffffff" if tr["side"] == "SELL" else "#04120a", fontsize=29,
            fontweight="bold", ha="center", va="center", family=sans, zorder=6)
    ax.text(0.57, H - 2.8, "XAU/USD  \u00b7  GOLD SCALP", color=FG, fontsize=9, fontweight="bold", va="center", family=mono, zorder=5)
    box(2.35, H - 3.05, 1.5, 0.36, "none", FG, 1.2, 0.18, 4)
    ax.text(3.1, H - 2.87, f"RR 1:{tr['ratio']:g}", color=FG, fontsize=11, fontweight="bold", ha="center", va="center", family=sans, zorder=6)

    # level cards
    risk = tr["risk"]
    cards = [("ENTRY", f"{tr['entry']:.2f}", "market price"),
             ("STOP LOSS", f"{tr['sl']:.2f}", f"-${risk:.2f}"),
             ("TAKE PROFIT", f"{tr['tp']:.2f}", f"+${risk * tr['ratio']:.2f}")]
    cw, gap = (W - 1.1 - 2 * 0.14) / 3, 0.14
    for k, (lab, val, sub) in enumerate(cards):
        x = 0.55 + k * (cw + gap)
        box(x, 0.75, cw, 1.35, "#0c0e11", EDGE, 1.2, 0.12, 3)
        ax.text(x + 0.14, 1.88, lab, color=DIM, fontsize=7.5, fontweight="bold", va="center", family=mono, zorder=5)
        ax.text(x + 0.14, 1.4, val, color=FG, fontsize=15, fontweight="bold", va="center", family=mono, zorder=5)
        ax.text(x + 0.14, 0.98, sub, color=DIM, fontsize=8, va="center", family=mono, zorder=5)
    ax.text(W / 2, 0.45, "R O C K   A I   \u00b7   E X E C U T E   F A S T", color=DIM, fontsize=6.5, ha="center", va="center", family=mono, zorder=5)
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def make_scalp_image(tr, path="scalp.png"):
    try:
        import rock_card
        return rock_card.render(tr, path)
    except Exception as e:
        print(f"Main card failed, using the plain one: {str(e)[:150]}")
        return make_scalp_image_basic(tr, path)


def caption(tr):
    return (f"{'🟢' if tr['side'] == 'BUY' else '🔴'} <b>{tr['side']} XAU/USD</b> · {tr['session']} #{tr['n']}\n"
            f"Entry <code>{tr['entry']:.2f}</code>\nSL <code>{tr['sl']:.2f}</code>\n"
            f"TP <code>{tr['tp']:.2f}</code>  (1:{tr['ratio']:g})")


def resolve(s, df, force_close=False):
    now = datetime.now(timezone.utc)
    done = []
    for tr in list(s["open"]):
        after = df[df["datetime"] > pd.Timestamp(tr["candle"])]
        res = None
        for hi, lo in zip(after["high"].values, after["low"].values):
            buy = tr["side"] == "BUY"
            if (lo <= tr["sl"]) if buy else (hi >= tr["sl"]):
                res = "SL"; break                     # same candle touching both counts as a loss (cautious)
            if (hi >= tr["tp"]) if buy else (lo <= tr["tp"]):
                res = "TP"; break
        if res is None and (force_close or (now - datetime.fromisoformat(tr["opened_at"])).total_seconds() > EXPIRE_MIN * 60):
            last = float(df["close"].iloc[-1]); sign = 1 if tr["side"] == "BUY" else -1
            r = max(min(sign * (last - tr["entry"]) / tr["risk"], tr["ratio"]), -1.0)
            tr["result"], tr["r"] = "TIMEOUT", round(r, 2)
        elif res:
            tr["result"], tr["r"] = res, (tr["ratio"] if res == "TP" else -1.0)
        else:
            continue
        tr["closed_at"] = now.isoformat()
        s["open"].remove(tr); s["closed"].append(tr); done.append(tr)
    return done


def results_text(done):
    if not done:
        return None
    lines = [f"{'✅' if t['result'] == 'TP' else ('❌' if t['result'] == 'SL' else '⏱')} #{t['n']} {t['side']} "
             f"{'TP hit' if t['result'] == 'TP' else ('SL hit' if t['result'] == 'SL' else 'closed')} {t['r']:+.1f}R" for t in done]
    return "📋 <b>Results</b>\n" + "\n".join(lines)


def session_summary(s, name, day):
    ts = [t for t in s["closed"] if t.get("day") == day and t["session"] == name]
    if not ts:
        return None
    w = sum(1 for t in ts if t["r"] > 0); l = len(ts) - w
    return (f"📊 <b>{name} session closed</b>\nTrades: {len(ts)} · Wins {w} · Losses {l} ({w / len(ts) * 100:.0f}%)\n"
            f"Total: {sum(t['r'] for t in ts):+.1f}R")


def main():
    chat = os.environ.get("SCALP_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")   # your channel (or a separate chat if you add SCALP_CHAT_ID)
    if not chat:
        print("No chat id found. Check the TELEGRAM_CHAT_ID secret.")
        sys.exit(1)
    b.TG_CHAT = chat
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        print("Weekend: gold is closed."); return
    s = load()
    day = now.strftime("%Y-%m-%d")
    if s["day"] != day:
        s["day"], s["count"] = day, {}
    df = fetch()
    for t in s["open"]:
        t.setdefault("day", t["opened_at"][:10])
    force = now.hour * 60 + now.minute >= RECAP_AFTER_MIN
    done = resolve(s, df, force_close=force)
    txt = results_text(done)
    if txt:
        b.send(txt)
    if force and s["count"] and day not in s.setdefault("recap_days", []):
        wins = [t for t in s["closed"] if t.get("day") == day and t["r"] > 0]
        label = (now + timedelta(hours=1)).strftime("%a %d %b")
        cap = f"\U0001F3C6 <b>ROCK AI DAY RECAP</b> \u00b7 {label}\n{len(wins)} winning trades today"
        try:
            import rock_recap
            b.send_photo(rock_recap.render_recap(label, wins), cap)
        except Exception as e:
            print(f"Recap card failed, sending text: {str(e)[:150]}")
            b.send(cap)
        s["recap_days"].append(day)
    session, a, c = session_of(now)
    # session-end summary once, at the first run after the session closes
    for name, (sa, sc) in SESSIONS.items():
        key = f"{day}:{name}"
        if SHOW_SESSION_SUMMARY and now.hour * 60 + now.minute >= sc and key not in s["summary_sent"] and s["count"].get(name):
            m = session_summary(s, name, day)
            if m and not [t for t in s["open"] if t["session"] == name]:
                b.send(m); s["summary_sent"].append(key)
    if session:
        have = s["count"].get(session, 0)
        mins_in = now.hour * 60 + now.minute - a
        should = int(PER_SESSION * min(1.0, (mins_in + 30) / (c - a - 30)))   # spread evenly, finished 30 min before the session ends
        todo = max(0, min(should - have, MAX_PER_RUN, PER_SESSION - have))
        for k in range(todo):
            if k:
                time.sleep(random.randint(GAP_MIN, GAP_MAX))
                df = fetch(); resolve_msg = results_text(resolve(s, df))
                if resolve_msg:
                    b.send(resolve_msg)
            n = sum(s["count"].values()) + 1
            tr = make_trade(df, session, n)
            tr["day"] = day
            b.send_image_or_text(lambda: make_scalp_image(tr), caption(tr), caption=caption(tr))
            s["open"].append(tr); s["count"][session] = n
            save(s)
    save(s)
    print("Scalp run done:", s["count"], "open:", len(s["open"]))


if __name__ == "__main__":
    main()
