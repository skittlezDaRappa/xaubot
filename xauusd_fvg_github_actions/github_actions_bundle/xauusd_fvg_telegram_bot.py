#!/usr/bin/env python3
"""
XAUUSD HTF FVG Bias -> Telegram Bot
====================================================================
Watches XAUUSD 1H candles, re-implements the SAME logic as the
"HTF FVG Bias [1H] - XAUUSD" Pine Script indicator (3-candle Fair
Value Gap detection, HTF bias, locked-in Entry/SL/TP trade plan,
live PENDING -> IN ZONE -> STOPPED / TARGET HIT status, and account
risk-based position sizing), and pushes a Telegram message whenever:

  - A new trade setup appears (a fresh unfilled 1H FVG starts driving
    the HTF bias)
  - The HTF bias flips direction
  - An active setup's status changes (price taps into the zone, gets
    stopped out, or hits target)
  - An active setup is invalidated (bias flips away before it resolved)

This is a STANDALONE re-implementation, not a bridge to TradingView.
Pine Script cannot call the internet on its own, so this script pulls
its own XAUUSD price data (Yahoo Finance via yfinance, no API key
needed) and recomputes the same math independently. Because the data
source and exact tick timing differ slightly from your broker/chart,
treat this as an alerting companion, not a byte-for-byte mirror of
what TradingView shows — always eyeball the chart before acting on an
alert.

--------------------------------------------------------------------
SETUP
--------------------------------------------------------------------
1. pip install -r requirements.txt   (yfinance, pandas, requests)

2. Create a Telegram bot:
     - Message @BotFather on Telegram -> /newbot -> follow the
       prompts -> it gives you a token like "123456:ABC-DEF...".

3. Get your chat ID:
     - Send any message to your new bot first (e.g. "hi").
     - Visit this URL in a browser (with your real token):
         https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     - Find "chat":{"id": 123456789, ...} in the JSON — that number
       is your chat ID.

4. Set both as environment variables (recommended, keeps secrets out
   of the file):
         export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
         export TELEGRAM_CHAT_ID="123456789"
   ...or just paste them into the CONFIG block below.

5. Run it:
         python3 xauusd_fvg_telegram_bot.py
   You should immediately get a "bot started" message on Telegram —
   that confirms the token/chat ID are wired up correctly.

6. Leave it running in the background, e.g.:
         nohup python3 xauusd_fvg_telegram_bot.py > bot.out 2>&1 &
   or run it inside `tmux`/`screen`, or set it up as a systemd
   service (see the bottom of this file for an example unit).
--------------------------------------------------------------------
"""

import os
import json
import time
import logging
import traceback
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
import yfinance as yf

# ============================================================================
# CONFIG  (mirrors the Pine Script's default inputs 1:1)
# ============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

SYMBOL_PRIMARY  = "XAUUSD=X"   # Yahoo Finance spot gold vs USD
SYMBOL_FALLBACK = "GC=F"       # COMEX gold futures, used if the primary ticker fails

POLL_INTERVAL_SECONDS = 300    # how often the bot checks (5 min)
LOOKBACK_1H_DAYS       = 30    # 1H history window used to rebuild the FVG board
FINE_INTERVAL          = "5m"  # granularity used to track an ACTIVE setup's status
FINE_LOOKBACK_DAYS     = 3     # safety window for the fine-grained fetch

# --- FVG detection ---
MIN_GAP_PCT       = 0.0        # ignore gaps smaller than this % of price (0 = off)
MIT_METHOD        = "wick"     # "wick" or "close" — how a gap counts as filled
MAX_FVGS_PER_SIDE = 15         # keep only the N most recent per side (bull/bear)

# --- Entry / Stop Loss ---
ENTRY_MODE        = "50%"      # "50%" (CE midpoint) | "near" | "far"
SL_BUFFER_TYPE    = "ATR"      # "ATR" or "fixed"
SL_BUFFER_ATR_MULT = 0.10
SL_BUFFER_FIXED    = 0.50      # price units, e.g. 0.50 = 50 cents on XAUUSD
ATR_LEN             = 14       # Wilder ATR length, computed on the 1H series

# --- Take Profit ---
TARGET_MODE          = "structure_fallback"  # "fixed" | "structure" | "structure_fallback"
RR_MULTIPLE          = 2.0     # used by "fixed" and as the fallback ratio
MIN_RR_FOR_STRUCTURE = 1.2     # structure target must clear this R:R to be used
MAX_STRUCT_DIST_ATR  = 8.0     # structure target farther than this (x ATR) is rejected

# --- Position sizing ---
CALC_POSITION_SIZE = True
ACCOUNT_BALANCE = 10000.0
RISK_PCT        = 1.0          # % of account risked per trade
CONTRACT_SIZE   = 100.0        # units per 1.0 lot (XAUUSD is commonly 100 oz/lot — check your broker)
LOT_STEP        = 0.01

# --- Behavior ---
SUPPRESS_INITIAL_ALERTS = True   # don't blast old/pre-existing state as "new" on first run
SEND_IN_ZONE_ALERTS     = True   # notify when price taps into the entry zone
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fvg_bot_state.json")
LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fvg_bot.log")

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("fvg_bot")


# ============================================================================
# TELEGRAM
# ============================================================================
def send_telegram(text: str) -> None:
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN or "PASTE_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping send. Set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        log.warning("Message that would have been sent:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            log.error("Telegram send failed (%s): %s", r.status_code, r.text)
    except Exception:
        log.error("Telegram send raised an exception:\n%s", traceback.format_exc())


# ============================================================================
# DATA FETCHING
# ============================================================================
def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_1h_closed() -> pd.DataFrame:
    """Fetch 1H candles and drop the currently-forming (unclosed) bar, so
    every row here is a fully closed candle — matching the Pine script's
    use of only high[1]/high[2]/high[3] (never the live bar)."""
    for sym in (SYMBOL_PRIMARY, SYMBOL_FALLBACK):
        try:
            df = yf.Ticker(sym).history(period=f"{LOOKBACK_1H_DAYS}d", interval="60m")
            df = _flatten_columns(df).dropna()
            if df.empty:
                continue
            now_utc = datetime.now(timezone.utc)
            last_start = df.index[-1].tz_convert("UTC") if df.index.tz is not None else df.index[-1].tz_localize("UTC")
            if last_start + pd.Timedelta(hours=1) > now_utc:
                df = df.iloc[:-1]  # still forming, drop it
            if not df.empty:
                return df
        except Exception:
            log.warning("Fetching %s failed:\n%s", sym, traceback.format_exc())
    return pd.DataFrame()


def fetch_fine_since(since_epoch: float) -> pd.DataFrame:
    """Fetch fine-grained (5m) candles strictly after `since_epoch`, used to
    step an ACTIVE setup's status forward accurately between polls."""
    for sym in (SYMBOL_PRIMARY, SYMBOL_FALLBACK):
        try:
            df = yf.Ticker(sym).history(period=f"{FINE_LOOKBACK_DAYS}d", interval=FINE_INTERVAL)
            df = _flatten_columns(df).dropna()
            if df.empty:
                continue
            idx_utc = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
            df = df.set_axis(idx_utc)
            df = df[df.index.astype("int64") // 10**9 > since_epoch]
            return df
        except Exception:
            log.warning("Fetching fine data for %s failed:\n%s", sym, traceback.format_exc())
    return pd.DataFrame()


# ============================================================================
# INDICATORS
# ============================================================================
def wilder_atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Matches Pine's ta.atr(): true range smoothed with Wilder's RMA
    (seed = SMA of the first `length` true-range values)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = pd.Series(index=df.index, dtype=float)
    if len(tr) < length:
        return atr
    atr.iloc[length - 1] = tr.iloc[:length].mean()
    for i in range(length, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (length - 1) + tr.iloc[i]) / length
    return atr


# ============================================================================
# FVG DETECTION + MITIGATION REPLAY
# ============================================================================
def detect_fvgs(df: pd.DataFrame):
    """Scan every consecutive 3-candle window for a Fair Value Gap, then
    replay every later candle forward to mark it filled (mitigated) or not
    — exactly the same 3-candle high/low logic and fill rule as the Pine
    script, just run once over history instead of bar-by-bar in real time.
    Returns (bull_list, bear_list), each newest-last, each item:
      {time (anchor, epoch), top, bot, mitigated}
    """
    bulls, bears = [], []
    highs, lows, closes = df["High"].values, df["Low"].values, df["Close"].values
    times = (df.index.astype("int64") // 10**9).values

    for i in range(2, len(df)):
        a_high, a_low = highs[i - 2], lows[i - 2]
        c_high, c_low = highs[i], lows[i]
        anchor_t = int(times[i - 1])  # middle candle, matches Pine's time[2] anchor

        if c_low > a_high:  # bullish FVG
            top, bot = c_low, a_high
            if MIN_GAP_PCT <= 0 or (top - bot) / bot * 100 >= MIN_GAP_PCT:
                bulls.append({"time": anchor_t, "top": top, "bot": bot, "mitigated": False, "idx": i})
        if c_high < a_low:  # bearish FVG
            top, bot = a_low, c_high
            if MIN_GAP_PCT <= 0 or (top - bot) / bot * 100 >= MIN_GAP_PCT:
                bears.append({"time": anchor_t, "top": top, "bot": bot, "mitigated": False, "idx": i})

    def replay_mitigation(gaps, is_bull):
        for g in gaps:
            for j in range(g["idx"] + 1, len(df)):
                check_lo = lows[j] if MIT_METHOD == "wick" else closes[j]
                check_hi = highs[j] if MIT_METHOD == "wick" else closes[j]
                if is_bull and check_lo <= g["bot"]:
                    g["mitigated"] = True
                    break
                if (not is_bull) and check_hi >= g["top"]:
                    g["mitigated"] = True
                    break

    replay_mitigation(bulls, True)
    replay_mitigation(bears, False)

    bulls = bulls[-MAX_FVGS_PER_SIDE:]
    bears = bears[-MAX_FVGS_PER_SIDE:]
    for g in bulls + bears:
        g.pop("idx", None)
    return bulls, bears


def last_unmitigated(gaps):
    for g in reversed(gaps):
        if not g["mitigated"]:
            return g
    return None


def nearest_unmit_above(gaps, ref):
    candidates = [g["bot"] for g in gaps if not g["mitigated"] and g["bot"] > ref]
    return min(candidates) if candidates else None


def nearest_unmit_below(gaps, ref):
    candidates = [g["top"] for g in gaps if not g["mitigated"] and g["top"] < ref]
    return max(candidates) if candidates else None


# ============================================================================
# TRADE PLAN (Entry / SL / TP / Size) — mirrors the Pine script exactly
# ============================================================================
def entry_for(near_edge, far_edge):
    if ENTRY_MODE == "near":
        return near_edge
    if ENTRY_MODE == "far":
        return far_edge
    return (near_edge + far_edge) / 2  # "50%"


def build_candidate(direction, far_edge, near_edge, atr, opposite_gaps, anchor_time):
    """direction: 'LONG' or 'SHORT'."""
    entry = entry_for(near_edge, far_edge)
    buffer_ = atr * SL_BUFFER_ATR_MULT if SL_BUFFER_TYPE == "ATR" else SL_BUFFER_FIXED

    if direction == "LONG":
        sl = far_edge - buffer_
        risk = entry - sl
    else:
        sl = far_edge + buffer_
        risk = sl - entry

    if risk is None or risk <= 0 or pd.isna(risk):
        return None

    if direction == "LONG":
        struct_raw = nearest_unmit_above(opposite_gaps, entry)
        struct_ok = struct_raw is not None and (MAX_STRUCT_DIST_ATR <= 0 or (struct_raw - entry) <= MAX_STRUCT_DIST_ATR * atr)
        struct_tp = struct_raw if struct_ok else None
        fixed_tp = entry + RR_MULTIPLE * risk
        struct_rr = (struct_tp - entry) / risk if struct_tp is not None else None
    else:
        struct_raw = nearest_unmit_below(opposite_gaps, entry)
        struct_ok = struct_raw is not None and (MAX_STRUCT_DIST_ATR <= 0 or (entry - struct_raw) <= MAX_STRUCT_DIST_ATR * atr)
        struct_tp = struct_raw if struct_ok else None
        fixed_tp = entry - RR_MULTIPLE * risk
        struct_rr = (entry - struct_tp) / risk if struct_tp is not None else None

    if TARGET_MODE == "fixed":
        tp = fixed_tp
    elif TARGET_MODE == "structure":
        tp = struct_tp if struct_tp is not None else fixed_tp
    else:  # structure_fallback
        tp = struct_tp if (struct_rr is not None and struct_rr >= MIN_RR_FOR_STRUCTURE) else fixed_tp

    rr = (tp - entry) / risk if direction == "LONG" else (entry - tp) / risk

    lots = None
    if CALC_POSITION_SIZE:
        risk_amount = ACCOUNT_BALANCE * RISK_PCT / 100
        raw_lots = risk_amount / (risk * CONTRACT_SIZE)
        lots = np.floor(raw_lots / LOT_STEP) * LOT_STEP

    return {
        "direction": direction,
        "anchor_time": anchor_time,
        "near_edge": near_edge,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk": risk,
        "rr": rr,
        "lots": lots,
        "status": "PENDING",
    }


def pick_candidate(bias, bulls, bears, atr):
    if bias == "Bullish":
        g = last_unmitigated(bulls)
        if g is None:
            return None
        return build_candidate("LONG", g["bot"], g["top"], atr, bears, g["time"])
    if bias == "Bearish":
        g = last_unmitigated(bears)
        if g is None:
            return None
        return build_candidate("SHORT", g["top"], g["bot"], atr, bulls, g["time"])
    return None


def determine_bias(bulls, bears):
    b1 = last_unmitigated(bulls)
    b2 = last_unmitigated(bears)
    if b1 is None and b2 is None:
        return "Neutral"
    if b2 is None:
        return "Bullish"
    if b1 is None:
        return "Bearish"
    return "Bullish" if b1["time"] > b2["time"] else "Bearish"


# ============================================================================
# LIVE STATUS STEPPING (PENDING -> IN ZONE -> STOPPED / TARGET HIT)
# ============================================================================
def step_status(setup: dict, fine_df: pd.DataFrame):
    """Walk the fine-grained candles forward in order and advance the
    setup's status exactly like the Pine script does bar by bar. Returns
    the list of status transitions that happened (in order)."""
    events = []
    if fine_df.empty:
        return events

    highs, lows, closes = fine_df["High"].values, fine_df["Low"].values, fine_df["Close"].values
    for i in range(len(fine_df)):
        check_lo = lows[i] if MIT_METHOD == "wick" else closes[i]
        check_hi = highs[i] if MIT_METHOD == "wick" else closes[i]
        direction = setup["direction"]
        status = setup["status"]

        if status in ("STOPPED", "TARGET HIT"):
            break

        if direction == "LONG":
            if status == "PENDING" and check_lo <= setup["near_edge"]:
                status = "IN ZONE"
            if check_lo <= setup["sl"]:
                status = "STOPPED"
            elif status == "IN ZONE" and check_hi >= setup["tp"]:
                status = "TARGET HIT"
        else:
            if status == "PENDING" and check_hi >= setup["near_edge"]:
                status = "IN ZONE"
            if check_hi >= setup["sl"]:
                status = "STOPPED"
            elif status == "IN ZONE" and check_lo <= setup["tp"]:
                status = "TARGET HIT"

        if status != setup["status"]:
            events.append(status)
            setup["status"] = status

    return events


# ============================================================================
# MESSAGES
# ============================================================================
def fmt(x):
    return "n/a" if x is None or pd.isna(x) else f"{x:.2f}"


def setup_message(setup: dict, header: str) -> str:
    lines = [
        header,
        f"Direction: <b>{setup['direction']}</b>",
        f"Entry: {fmt(setup['entry'])}",
        f"SL: {fmt(setup['sl'])}",
        f"TP: {fmt(setup['tp'])}  (R:R 1:{fmt(setup['rr'])})",
    ]
    if CALC_POSITION_SIZE and setup.get("lots") is not None:
        risk_amount = ACCOUNT_BALANCE * RISK_PCT / 100
        lines.append(f"Size: {setup['lots']:.4f} lots (risk ${risk_amount:.2f})")
    lines.append(f"Status: {setup['status']}")
    return "\n".join(lines)


# ============================================================================
# STATE PERSISTENCE
# ============================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("Could not read state file, starting fresh.")
    return {"initialized": False, "bias": None, "active_setup": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================================
# MAIN CYCLE
# ============================================================================
def run_once(state: dict) -> dict:
    df = fetch_1h_closed()
    if df.empty or len(df) < ATR_LEN + 5:
        log.warning("Not enough 1H data this cycle, skipping.")
        return state

    atr_series = wilder_atr(df, ATR_LEN)
    atr = atr_series.iloc[-1]
    if pd.isna(atr):
        log.warning("ATR not ready yet, skipping.")
        return state

    bulls, bears = detect_fvgs(df)
    bias = determine_bias(bulls, bears)
    candidate = pick_candidate(bias, bulls, bears, atr)

    is_first_run = not state.get("initialized", False)

    # --- bias flip ---
    prev_bias = state.get("bias")
    if prev_bias is not None and bias != prev_bias and not is_first_run:
        send_telegram(f"\U0001F504 XAUUSD HTF bias flipped to <b>{bias.upper()}</b>")
    state["bias"] = bias

    active = state.get("active_setup")

    same = (
        active is not None
        and candidate is not None
        and active["direction"] == candidate["direction"]
        and active["anchor_time"] == candidate["anchor_time"]
    )

    if not same:
        if active is not None and active["status"] in ("PENDING", "IN ZONE") and not is_first_run:
            send_telegram(
                "⚠️ XAUUSD previous "
                f"{active['direction']} setup (entry {fmt(active['entry'])}) is no longer current — "
                "bias moved on before it resolved."
            )
        if candidate is not None:
            candidate["last_checked"] = time.time()
            active = candidate
            if not is_first_run:
                send_telegram(setup_message(active, "\U0001F195 New XAUUSD HTF setup"))
        else:
            active = None

    if active is not None:
        fine_df = fetch_fine_since(active.get("last_checked", time.time()))
        events = step_status(active, fine_df)
        active["last_checked"] = time.time()
        for ev in events:
            if is_first_run:
                continue
            if ev == "IN ZONE" and SEND_IN_ZONE_ALERTS:
                send_telegram(setup_message(active, "\U0001F4CD XAUUSD setup tapped into entry zone"))
            elif ev == "STOPPED":
                send_telegram(setup_message(active, "\U0001F6D1 XAUUSD setup STOPPED OUT"))
            elif ev == "TARGET HIT":
                send_telegram(setup_message(active, "✅ XAUUSD setup TARGET HIT"))

    state["active_setup"] = active
    state["initialized"] = True
    return state


def main():
    # Running under GitHub Actions (or any CI that sets this) -> do ONE check
    # cycle and exit. The workflow's cron schedule is what provides the
    # "polling loop" in that environment, and it commits fvg_bot_state.json
    # back to the repo between runs so the locked-in setup / already-sent
    # alerts still persist across those separate, short-lived runs.
    single_shot = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    if single_shot:
        first_ever = not os.path.exists(STATE_FILE)
        if first_ever:
            send_telegram("\U0001F916 XAUUSD HTF FVG bot started on GitHub Actions — watching 1H structure for setups.")
        state = load_state()
        try:
            state = run_once(state)
        except Exception:
            log.error("Error in scheduled run:\n%s", traceback.format_exc())
        state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    log.info("Starting XAUUSD HTF FVG Telegram bot (poll every %ss)", POLL_INTERVAL_SECONDS)
    send_telegram("\U0001F916 XAUUSD HTF FVG bot started — watching 1H structure for setups.")
    state = load_state()
    while True:
        try:
            state = run_once(state)
            save_state(state)
        except Exception:
            log.error("Error in main loop:\n%s", traceback.format_exc())
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

# ============================================================================
# OPTIONAL: systemd service, so it survives reboots / auto-restarts on crash
# ----------------------------------------------------------------------------
# Save as /etc/systemd/system/xauusd-fvg-bot.service :
#
#   [Unit]
#   Description=XAUUSD HTF FVG Telegram Bot
#   After=network-online.target
#
#   [Service]
#   Type=simple
#   User=YOUR_USERNAME
#   WorkingDirectory=/path/to/this/script
#   Environment=TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
#   Environment=TELEGRAM_CHAT_ID=123456789
#   ExecStart=/usr/bin/python3 /path/to/this/script/xauusd_fvg_telegram_bot.py
#   Restart=on-failure
#
#   [Install]
#   WantedBy=multi-user.target
#
# Then:  sudo systemctl daemon-reload && sudo systemctl enable --now xauusd-fvg-bot
# ============================================================================
