"""Entry signals for the day trader page: a trend-pullback strategy with a fee filter.

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


def evaluate(bars_1m: list[dict], bid: float | None, ask: float | None, is_open: bool, hurdle_pct: float) -> dict:
    """bars_1m: ascending 1-minute bars {t,o,h,l,c}. Returns the signal for the page."""
    out = {"strategy": NAME, "state": "nodata", "headline": "", "entry": None, "stop": None, "target": None,
           "reward_pct": None, "hurdle_pct": round(hurdle_pct, 2), "reasons": [], "ind": {}}
    if not is_open or bid is None:
        out.update(state="closed", headline="Market closed: no live price, so no entry signal.")
        return out
    if len(bars_1m) < MIN_1M:
        out["headline"] = "Not enough recent price history yet to judge an entry."
        return out
    spread_pct = ((ask or bid) - bid) / bid * 100
    closes1 = [b["c"] for b in bars_1m]
    bars5 = resample(bars_1m, 300)
    closes5 = [b["c"] for b in bars5]
    e20_5, e50_5 = ema(closes5, 20), ema(closes5, 50)
    e20_1 = ema(closes1, 20)[-1]
    a1, a5 = atr(bars_1m[-30:], 14), atr(bars5, 14)
    r_now, r_prev = rsi(closes1, 14), rsi(closes1[:-2], 14)
    price = bid
    slope = e20_5[-1] - e20_5[-4] if len(e20_5) >= 4 else 0.0
    out["ind"] = {"ema20_5m": e20_5[-1], "ema50_5m": e50_5[-1], "ema20_1m": e20_1, "atr_1m": a1, "atr_5m": a5,
                  "rsi_1m": r_now, "rsi_1m_prev": r_prev, "bars_5m": len(bars5), "spread_pct": spread_pct}
    reasons = out["reasons"]
    up = e20_5[-1] > e50_5[-1] and slope >= 0 and price > e50_5[-1]
    if not up:
        why = []
        if e20_5[-1] <= e50_5[-1]:
            why.append(f"5-min EMA20 {e20_5[-1]:.6g} is below EMA50 {e50_5[-1]:.6g}")
        if slope < 0:
            why.append("5-min EMA20 is falling")
        if price <= e50_5[-1]:
            why.append(f"price is below the 5-min EMA50 ({e50_5[-1]:.6g})")
        reasons.extend(why)
        out.update(state="avoid", headline="Avoid for now: no uptrend. Wait for the 5-minute trend to turn up.")
        return out
    reasons.append(f"Uptrend: 5-min EMA20 {e20_5[-1]:.6g} above EMA50 {e50_5[-1]:.6g} and rising")
    entry = min(e20_1, price)
    stop = entry - STOP_ATR * a5
    if stop <= 0 or a5 <= 0:
        out.update(state="nodata", headline="Price range too flat to set a stop.")
        return out
    target = entry + REWARD_R * (entry - stop)
    reward_pct = (target - entry) / entry * 100
    out.update(entry=entry, stop=stop, target=target, reward_pct=round(reward_pct, 2))
    near = price <= e20_1 + NEAR_ATR * (a1 or 0)
    turning = r_now is not None and r_prev is not None and r_now > r_prev and r_prev < 55
    if reward_pct < FEE_MULT * hurdle_pct:
        reasons.append(f"Target move {reward_pct:.2f}% is less than {FEE_MULT:g}x the {hurdle_pct:.2f}% round-trip cost")
        out.update(state="fees", headline=f"Uptrend, but typical moves (~{reward_pct:.2f}% to target) are too small to pay "
                                          f"the {hurdle_pct:.2f}% fees. Better to skip.")
        return out
    reasons.append(f"Target move {reward_pct:.2f}% covers the {hurdle_pct:.2f}% round-trip cost {reward_pct / hurdle_pct:.1f}x"
                   if hurdle_pct > 0 else f"Target move {reward_pct:.2f}%; no fees to cover")
    if near and turning:
        reasons.append(f"Price is at the 1-min EMA20 pullback level and RSI turned up ({r_prev:.0f} to {r_now:.0f})")
        out.update(state="enter", entry=price,
                   headline="Entry now: uptrend pullback with momentum turning up.")
        # re-base stop/target on the actual price
        out["stop"] = price - STOP_ATR * a5
        out["target"] = price + REWARD_R * STOP_ATR * a5
        out["reward_pct"] = round((out["target"] - price) / price * 100, 2)
    elif near:
        reasons.append(f"At the pullback level, waiting for 1-min RSI to turn up (now {r_now:.0f})" if r_now is not None
                       else "At the pullback level, waiting for momentum to turn up")
        out.update(state="wait", headline="Nearly there: price is at the buy level, waiting for momentum to turn up.")
    else:
        dist = (price - entry) / price * 100
        reasons.append(f"Price is {dist:.2f}% above the 1-min EMA20 buy level; don't chase")
        out.update(state="wait", headline="Wait for a pullback to the buy level before entering.")
    return out
