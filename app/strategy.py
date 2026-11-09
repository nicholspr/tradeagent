"""Entry signals for the day trader page: a trend-pullback strategy with a fee filter, in two speeds
(FAST: 5-minute trend, 1-minute timing; SWING: 1-hour trend, 15-minute timing).

Plain rules, no AI, so every suggestion can be checked by hand:

1. Trend (5-minute bars built from 1-minute candles): only look for buys when the 20-bar EMA is
   above the 50-bar EMA and rising, and the price is above the 50-bar EMA. Never buy a downtrend.
2. Entry level: in an uptrend, buy pullbacks to the 1-minute 20-bar EMA rather than chasing.
   "Entry now" when the price is at or near that level and 1-minute RSI(14) has turned up from
   below 55 (the dip is being bought). Otherwise "Wait" with the level to buy at.
3. Stop and target: stop 1.5 x the 5-minute ATR(14) below the entry; target 2 x that risk above.
4. Fee filter: the move to target must be at least 1.5 x the round-trip cost (eToro fees and
   spread, from the page's cost settings). If typical moves are too small to pay the fees,
   the signal says so instead of suggesting a trade.

This is a starting rule set to test with paper trades, not a proven edge.
"""
from __future__ import annotations

NAME = "Trend pullback (5-min trend, 1-min timing, fee filter)"
MIN_1M = 60          # 1-minute candles needed at all
FEE_MULT = 1.5       # target move must be at least this many times the round-trip cost
STOP_ATR = 1.5       # stop distance in 5-minute ATRs
REWARD_R = 2.0       # target distance in multiples of the stop distance
NEAR_ATR = 0.25      # "at the entry level" = within this many 1-minute ATRs above it
SPREAD_CAP = 0.15    # Fast v2, Fast v3 and Swing: skip when the spread is more than this share of the stop distance


def ema(vals: list[float], n: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI of the last close."""
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]
    trs = trs[-n:]
    return sum(trs) / len(trs)


def resample(bars: list[dict], seconds: int = 300) -> list[dict]:
    """Group ascending 1-minute bars {t,o,h,l,c} into `seconds`-long bars."""
    out: list[dict] = []
    for b in bars:
        start = int(b["t"] // seconds * seconds)
        if out and out[-1]["t"] == start:
            o = out[-1]
            o["h"] = max(o["h"], b["h"])
            o["l"] = min(o["l"], b["l"])
            o["c"] = b["c"]
        else:
            out.append({"t": start, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def default_hurdle(kind: str) -> float:
    """Round-trip cost in % of a $100 trade when the page doesn't send one."""
    return {"crypto": 2.02, "stock": 2.0, "etf": 0.0}.get(kind, 0.0)


FAST = {"key": "fast", "name": NAME, "trend": "5-min", "timing": "1-min", "stop_atr": STOP_ATR,
        "reward_r": REWARD_R, "near_atr": NEAR_ATR, "min_trend": 0, "min_timing": MIN_1M}
SWING = {"key": "swing", "name": "Swing pullback (1-hour trend, 15-min timing, fee filter)", "trend": "1-hour",
         "timing": "15-min", "stop_atr": 1.5, "reward_r": 2.0, "near_atr": 0.25, "min_trend": 50, "min_timing": 30,
         "from_ask": True, "spread_cap": SPREAD_CAP}   # stop/target measured from the ask actually paid


def evaluate(bars_1m: list[dict], bid: float | None, ask: float | None, is_open: bool, hurdle_pct: float) -> dict:
    """Fast version: ascending 1-minute bars {t,o,h,l,c}; the 5-minute trend is built from them."""
    if len(bars_1m) < MIN_1M:
        return _judge([], bars_1m, bid, ask, is_open, hurdle_pct, FAST)
    return _judge(resample(bars_1m, 300), bars_1m, bid, ask, is_open, hurdle_pct, FAST)


def evaluate_swing(bars_15m: list[dict], bars_1h: list[dict], bid: float | None, ask: float | None,
                   is_open: bool, hurdle_pct: float) -> dict:
    """Slower version for assets with big fees (crypto): 1-hour trend, 15-minute timing.
    Same rules; stops and targets come out several times wider, so moves can pay 2% fees."""
    return _judge(bars_1h, bars_15m, bid, ask, is_open, hurdle_pct, SWING)


def _judge(trend_bars: list[dict], timing_bars: list[dict], bid, ask, is_open: bool, hurdle_pct: float, P: dict) -> dict:
    out = {"strategy": P["name"], "key": P["key"], "state": "nodata", "headline": "", "entry": None, "stop": None,
           "target": None, "reward_pct": None, "hurdle_pct": round(hurdle_pct, 2), "reasons": [], "ind": {}}
    if not is_open or bid is None:
        out.update(state="closed", headline="Market closed: no live price, so no entry signal.")
        return out
    if len(timing_bars) < P["min_timing"] or len(trend_bars) < max(P["min_trend"], 4):
        out["headline"] = "Not enough recent price history yet to judge an entry."
        return out
    TR, TM = P["trend"], P["timing"]
    spread_pct = ((ask or bid) - bid) / bid * 100
    closes_tm = [b["c"] for b in timing_bars]
    closes_tr = [b["c"] for b in trend_bars]
    e20_tr, e50_tr = ema(closes_tr, 20), ema(closes_tr, 50)
    e20_tm = ema(closes_tm, 20)[-1]
    a_tm, a_tr = atr(timing_bars[-30:], 14), atr(trend_bars, 14)
    r_now, r_prev = rsi(closes_tm, 14), rsi(closes_tm[:-2], 14)
    price = bid
    slope = e20_tr[-1] - e20_tr[-4] if len(e20_tr) >= 4 else 0.0
    out["ind"] = {"ema20_trend": e20_tr[-1], "ema50_trend": e50_tr[-1], "ema20_timing": e20_tm, "atr_timing": a_tm,
                  "atr_trend": a_tr, "rsi": r_now, "rsi_prev": r_prev, "trend_bars": len(trend_bars), "spread_pct": spread_pct}
    reasons = out["reasons"]
    up = e20_tr[-1] > e50_tr[-1] and slope >= 0 and price > e50_tr[-1]
    if not up:
        if e20_tr[-1] <= e50_tr[-1]:
            reasons.append(f"{TR} EMA20 {e20_tr[-1]:.6g} is below EMA50 {e50_tr[-1]:.6g}")
        if slope < 0:
            reasons.append(f"{TR} EMA20 is falling")
        if price <= e50_tr[-1]:
            reasons.append(f"price is below the {TR} EMA50 ({e50_tr[-1]:.6g})")
        out.update(state="avoid", headline=f"Avoid for now: no uptrend. Wait for the {TR} trend to turn up.")
        return out
    reasons.append(f"Uptrend: {TR} EMA20 {e20_tr[-1]:.6g} above EMA50 {e50_tr[-1]:.6g} and rising")
    entry = min(e20_tm, price)
    stop = entry - P["stop_atr"] * a_tr
    if stop <= 0 or not a_tr:
        out.update(state="nodata", headline="Price range too flat to set a stop.")
        return out
    target = entry + P["reward_r"] * (entry - stop)
    reward_pct = (target - entry) / entry * 100
    out.update(entry=entry, stop=stop, target=target, reward_pct=round(reward_pct, 2))
    near = price <= e20_tm + P["near_atr"] * (a_tm or 0)
    turning = r_now is not None and r_prev is not None and r_now > r_prev and r_prev < 55
    if reward_pct < FEE_MULT * hurdle_pct:
        reasons.append(f"Target move {reward_pct:.2f}% is less than {FEE_MULT:g}x the {hurdle_pct:.2f}% round-trip cost")
        out.update(state="fees", headline=f"Uptrend, but typical moves (~{reward_pct:.2f}% to target) are too small to pay "
                                          f"the {hurdle_pct:.2f}% fees. Better to skip.")
        return out
    reasons.append(f"Target move {reward_pct:.2f}% covers the {hurdle_pct:.2f}% round-trip cost {reward_pct / hurdle_pct:.1f}x"
                   if hurdle_pct > 0 else f"Target move {reward_pct:.2f}%; no fees to cover")
    risk = P["stop_atr"] * a_tr
    spread = (ask or bid) - bid
    if P.get("spread_cap") and spread > P["spread_cap"] * risk:
        reasons.append(f"Spread {spread:.6g} is {spread / risk * 100:.0f}% of the {risk:.6g} stop distance "
                       f"(limit {P['spread_cap'] * 100:.0f}%)")
        out.update(state="spread", headline="Spread too wide for this stop: paying it would eat too much of the "
                                            "reward and add to the risk. Better to skip.")
        return out
    if near and turning:
        reasons.append(f"Price is at the {TM} EMA20 pullback level and RSI turned up ({r_prev:.0f} to {r_now:.0f})")
        base = (ask or bid) if P.get("from_ask") else price   # Fast keeps its original bid-based levels as the baseline
        out.update(state="enter", entry=base, stop=base - risk, target=base + P["reward_r"] * risk,
                   reward_pct=round(P["reward_r"] * risk / base * 100, 2),
                   headline="Entry now: uptrend pullback with momentum turning up.")
    elif near:
        reasons.append(f"At the pullback level, waiting for {TM} RSI to turn up (now {r_now:.0f})" if r_now is not None
                       else "At the pullback level, waiting for momentum to turn up")
        out.update(state="wait", headline="Nearly there: price is at the buy level, waiting for momentum to turn up.")
    else:
        dist = (price - entry) / price * 100
        reasons.append(f"Price is {dist:.2f}% above the {TM} EMA20 buy level; don't chase")
        out.update(state="wait", headline="Wait for a pullback to the buy level before entering.")
    return out


# ---------------------------------------------------------------------------------------------
# Fast v2: the fast strategy with the fixes suggested by its first live results (7 Nov 2026:
# three SKHY.24-7 stop-outs on a quiet, drifting weekend price).
QUIET_PCT = 0.15         # skip when the typical 5-minute move (ATR) is below this % of the price
MIN_STOP_PCT = 0.30      # never put the stop closer than this % below the price paid...
SPREAD_STOPS = 3.0       # ...or closer than this many spreads
HOME = {"SKHY": ("Asia/Seoul", (9, 0), (15, 30), "Korean")}     # 24/7 CFDs follow their home market
DEFAULT_HOME = ("America/New_York", (9, 30), (16, 0), "US")      # the US names and pre-IPO CFDs
FAST2 = {"key": "fast2", "name": "Fast v2 (fast + quiet filter, 1-hour trend, spread-aware stops, home-market hours)",
         "reward_r": REWARD_R, "spread_cap": SPREAD_CAP}
# Fast v3 (9 Nov 2026 review of the first 48 auto-test trades: stops hit twice as often as 2:1 targets,
# while trades left to run to the time limit mostly made money): Fast v2 with a nearer 1.5:1 target.
# The auto-test also moves its stop to break-even once the price has gone 1x the risk in its favour.
FAST3 = {"key": "fast3", "name": "Fast v3 (Fast v2 with a 1.5:1 target and a break-even stop after +1R)",
         "reward_r": 1.5, "spread_cap": SPREAD_CAP}


def home_market(symbol: str) -> tuple:
    return HOME.get(symbol.upper().split(".")[0], DEFAULT_HOME)


def home_open(symbol: str, now) -> bool:
    """Is the home exchange of a 24/7 CFD open now (weekdays, regular hours, local time)?"""
    from zoneinfo import ZoneInfo
    tz, start, end, _ = home_market(symbol)
    local = now.astimezone(ZoneInfo(tz))
    hm = (local.hour, local.minute)
    return local.weekday() < 5 and start <= hm < end


def evaluate_fast2(bars_1m: list[dict], bars_1h: list[dict], bid, ask, is_open: bool, hurdle_pct: float,
                   kind: str = "", symbol: str = "", now=None, P: dict | None = None) -> dict:
    """Like evaluate(), plus: (1) skip quiet markets, (2) the 1-hour trend must agree, (3) stop at least
    MIN_STOP_PCT and SPREAD_STOPS spreads away, (4) stop/target measured from the ask actually paid, and
    the reward after costs must be at least the risk after costs, (5) 24/7 CFDs only in home-market hours,
    (6) no trade when the spread is more than SPREAD_CAP of the stop distance.
    hurdle_pct includes the spread, as for the other strategies. P picks the version (FAST2 or FAST3)."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    P = P or FAST2
    R = P["reward_r"]
    out = {"strategy": P["name"], "key": P["key"], "state": "nodata", "headline": "", "entry": None, "stop": None,
           "target": None, "reward_pct": None, "hurdle_pct": round(hurdle_pct, 2), "reasons": [], "ind": {}}
    if not is_open or bid is None:
        out.update(state="closed", headline="Market closed: no live price, so no entry signal.")
        return out
    if kind == "cfd" and not home_open(symbol, now):
        tz, start, end, name = home_market(symbol)
        out.update(state="closed", headline=f"{name} market closed: no trades on 24/7 weekend or overnight prices.")
        out["reasons"].append(f"Trades only {start[0]:02d}:{start[1]:02d}-{end[0]:02d}:{end[1]:02d} {tz} time, Monday to Friday")
        return out
    if len(bars_1m) < MIN_1M or len(bars_1h) < 50:
        out["headline"] = "Not enough recent price history yet to judge an entry."
        return out
    ask = ask or bid
    spread = ask - bid
    spread_pct = spread / bid * 100
    fee_pct = max(0.0, hurdle_pct - spread_pct)              # fees without the spread
    reasons = out["reasons"]
    bars5 = resample(bars_1m, 300)
    closes1, closes5, closes_h = [b["c"] for b in bars_1m], [b["c"] for b in bars5], [b["c"] for b in bars_1h]
    e20_5, e50_5 = ema(closes5, 20), ema(closes5, 50)
    e20_h, e50_h = ema(closes_h, 20), ema(closes_h, 50)
    e20_1 = ema(closes1, 20)[-1]
    a1, a5 = atr(bars_1m[-30:], 14), atr(bars5, 14)
    r_now, r_prev = rsi(closes1, 14), rsi(closes1[:-2], 14)
    price = bid
    out["ind"] = {"ema20_5m": e20_5[-1], "ema50_5m": e50_5[-1], "ema20_1h": e20_h[-1], "ema50_1h": e50_h[-1],
                  "ema20_1m": e20_1, "atr_5m": a5, "atr_5m_pct": a5 / price * 100 if a5 else None, "rsi_1m": r_now,
                  "spread_pct": spread_pct}
    if not a5 or a5 / price * 100 < QUIET_PCT:
        reasons.append(f"Typical 5-min move {a5 / price * 100 if a5 else 0:.3f}% is below {QUIET_PCT}%")
        out.update(state="quiet", headline="Too quiet: price moves are too small for a stop and target to mean anything.")
        return out
    slope5 = e20_5[-1] - e20_5[-4] if len(e20_5) >= 4 else 0.0
    up5 = e20_5[-1] > e50_5[-1] and slope5 >= 0 and price > e50_5[-1]
    up_h = e20_h[-1] > e50_h[-1] and price > e50_h[-1]
    if not (up5 and up_h):
        if not up5:
            reasons.append("5-min trend is not up (EMA20 vs EMA50, slope or price)")
        if not up_h:
            reasons.append(f"1-hour trend is not up (EMA20 {e20_h[-1]:.6g}, EMA50 {e50_h[-1]:.6g})")
        out.update(state="avoid", headline="Avoid for now: the 5-minute and 1-hour trends must both be up.")
        return out
    reasons.append("Uptrend on both the 5-minute and 1-hour charts")
    level = min(e20_1, price)                                # buy level, bid terms
    pay = level + spread                                     # what a buy there would cost
    dist = max(STOP_ATR * a5, MIN_STOP_PCT / 100 * pay, SPREAD_STOPS * spread)
    stop, target = pay - dist, pay + R * dist                # exits are hit by the bid
    reward_gross = (target - pay) / pay * 100
    net_reward, net_risk = reward_gross - fee_pct, dist / pay * 100 + fee_pct
    out.update(entry=pay, stop=stop, target=target, reward_pct=round(reward_gross, 2))
    if reward_gross < FEE_MULT * hurdle_pct or net_reward < net_risk:
        reasons.append(f"After costs the target pays {net_reward:.2f}% against a {net_risk:.2f}% risk")
        out.update(state="fees", headline=f"Uptrend, but after {hurdle_pct:.2f}% costs the reward ({net_reward:.2f}%) "
                                          f"doesn't beat the risk ({net_risk:.2f}%). Better to skip.")
        return out
    if P.get("spread_cap") and spread > P["spread_cap"] * dist:
        reasons.append(f"Spread {spread_pct:.3f}% is {spread / dist * 100:.0f}% of the stop distance "
                       f"(limit {P['spread_cap'] * 100:.0f}%)")
        out.update(state="spread", headline="Spread too wide for this stop: paying it would eat too much of the "
                                            "reward and add to the risk. Better to skip.")
        return out
    reasons.append(f"After costs: reward {net_reward:.2f}% vs risk {net_risk:.2f}%")
    near = price <= e20_1 + NEAR_ATR * (a1 or 0)
    turning = r_now is not None and r_prev is not None and r_now > r_prev and r_prev < 55
    if near and turning:
        d = max(STOP_ATR * a5, MIN_STOP_PCT / 100 * ask, SPREAD_STOPS * spread)
        out.update(state="enter", entry=ask, stop=ask - d, target=ask + R * d,
                   reward_pct=round(R * d / ask * 100, 2),
                   headline="Entry now: pullback in an uptrend on both timeframes, momentum turning up.")
        reasons.append(f"At the 1-min EMA20 buy level and RSI turned up ({r_prev:.0f} to {r_now:.0f})")
    elif near:
        out.update(state="wait", headline="Nearly there: price is at the buy level, waiting for momentum to turn up.")
    else:
        reasons.append(f"Price is {(price - level) / price * 100:.2f}% above the buy level; don't chase")
        out.update(state="wait", headline="Wait for a pullback to the buy level before entering.")
    return out


def evaluate_fast3(bars_1m: list[dict], bars_1h: list[dict], bid, ask, is_open: bool, hurdle_pct: float,
                   kind: str = "", symbol: str = "", now=None) -> dict:
    """Fast v2's rules with a 1.5:1 target (see FAST3)."""
    return evaluate_fast2(bars_1m, bars_1h, bid, ask, is_open, hurdle_pct, kind, symbol, now, P=FAST3)
