"""Scorecard: did the agents' calls make money?

For every finished analysis, the price at the time of the call is compared with the close 5, 20 and 60 trading
days later, and with a benchmark (Vanguard FTSE All-World, VWRL.L) over the same dates. A Buy or Overweight
"worked" if the share then beat the benchmark; an Underweight or Sell worked if it lagged it. Holds are shown for
reference but not scored. Results are grouped by rating, by AI models and by symbol.

Read-only: uses eToro daily candles, cached for a few hours. Nothing here trades.
"""
import time
from datetime import datetime, timezone

from . import store

HORIZONS = (5, 20, 60)
BENCHMARK = "VWRL.L"
CACHE_SECONDS = 4 * 3600
# Runs before this time used Sonnet for every agent (the models column was added later).
MODELS_BEFORE = ("2026-11-01T22:44:00", "claude-sonnet-5/claude-sonnet-5")
BULL, BEAR = {"buy", "overweight"}, {"underweight", "sell"}
RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]

_cache: dict = {}


def _daily(ec, symbol: str, now: datetime) -> list[dict]:
    """Completed daily candles (today's forming candle dropped), oldest first. Cached per symbol."""
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    iid = ec.find_instrument(symbol)["instrumentId"]
    today = now.date().isoformat()
    bars = [c for c in ec.candles(iid, "OneDay", 1000) if not str(c["date"]).startswith(today)]
    _cache[symbol] = (time.time(), bars)
    return bars


def clear_cache() -> None:
    _cache.clear()


def _close_on_or_before(bars: list[dict], day: str) -> float | None:
    best = None
    for b in bars:
        if str(b["date"])[:10] <= day:
            best = b["close"]
        else:
            break
    return best


def models_for(run: dict) -> str:
    if run.get("models"):
        return run["models"]
    return MODELS_BEFORE[1] if (run.get("started") or "") < MODELS_BEFORE[0] else "unknown"


NAMES = {"claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5": "Sonnet 5", "claude-sonnet-5-5": "Sonnet 5.5"}


def _short(models: str) -> str:
    """'claude-haiku-5-5/claude-sonnet-5' -> 'Haiku 5.5 / Sonnet 5' (evidence agents / deciding agents)."""
    return " / ".join(NAMES.get(m, m.replace("claude-", "").title()) for m in (models or "unknown").split("/"))


def evaluate(ec, runs: list[dict], now: datetime | None = None) -> list[dict]:
    """One row per finished rated run: returns at each horizon (None while still pending)."""
    now = now or datetime.now(timezone.utc)
    try:
        bench = _daily(ec, BENCHMARK, now)
    except Exception:  # noqa: BLE001
        bench = []
    rows = []
    for r in runs:
        rating = str(r.get("rating") or "").strip().title()
        if rating not in RATINGS or not r.get("started"):
            continue
        row = {"id": r["id"], "ticker": r["ticker"], "rating": rating, "day": r["started"][:10],
               "models": models_for(r), "start": None, "returns": {}, "vs": {}, "note": ""}
        try:
            bars = _daily(ec, r["ticker"], now)
        except Exception as e:  # noqa: BLE001
            row["note"] = f"no price history ({e})"[:80]
            rows.append(row)
            continue
        idx = max((i for i, b in enumerate(bars) if str(b["date"])[:10] <= row["day"]), default=None)
        if idx is None:
            row["note"] = "no price on the analysis date"
            rows.append(row)
            continue
        start = r.get("price") or bars[idx]["close"]
        row["start"] = start
        b0 = _close_on_or_before(bench, row["day"])
        for h in HORIZONS:
            j = idx + h
            if j >= len(bars):
                continue                                   # not enough days yet
            ret = (bars[j]["close"] / start - 1) * 100
            row["returns"][h] = ret
            b1 = _close_on_or_before(bench, str(bars[j]["date"])[:10])
            if b0 and b1:
                row["vs"][h] = ret - (b1 / b0 - 1) * 100
        rows.append(row)
    return rows


def worked(row: dict, h: int) -> bool | None:
    v = row["vs"].get(h)
    if v is None:
        return None
    r = row["rating"].lower()
    if r in BULL:
        return v > 0
    if r in BEAR:
        return v < 0
    return None


def summarise(rows: list[dict], key) -> list[dict]:
    """Group rows by key(row): count, average return and average vs benchmark per horizon, and hit rate."""
    groups: dict = {}
    for row in rows:
        groups.setdefault(key(row), []).append(row)
    out = []
    for name, rs in groups.items():
        g = {"name": name, "count": len(rs), "avg": {}, "avg_vs": {}, "hits": {}, "scored": {}, "pending": {}}
        for h in HORIZONS:
            vals = [x["returns"][h] for x in rs if h in x["returns"]]
            vs = [x["vs"][h] for x in rs if h in x["vs"]]
            w = [worked(x, h) for x in rs]
            w = [x for x in w if x is not None]
            g["avg"][h] = sum(vals) / len(vals) if vals else None
            g["avg_vs"][h] = sum(vs) / len(vs) if vs else None
            g["scored"][h] = len(w)
            g["hits"][h] = (sum(w) / len(w) * 100) if w else None
            g["pending"][h] = sum(1 for x in rs if h not in x["returns"])
        out.append(g)
    return out


def by_rating(rows):
    order = {r: i for i, r in enumerate(RATINGS)}
    return sorted(summarise(rows, lambda r: r["rating"]), key=lambda g: order.get(g["name"], 9))


def by_models(rows):
    return sorted(summarise([r for r in rows if r["rating"].lower() in BULL | BEAR], lambda r: _short(r["models"])),
                  key=lambda g: g["name"])


def by_ticker(rows):
    return sorted(summarise(rows, lambda r: r["ticker"]), key=lambda g: -g["count"])


def headline(rows: list[dict]) -> str:
    """One line for the daily summary."""
    bull = [r for r in rows if r["rating"].lower() in BULL]
    for h in HORIZONS:
        scored = [r for r in bull if h in r["vs"]]
        if scored:
            avg = sum(r["vs"][h] for r in scored) / len(scored)
            hits = sum(1 for r in scored if r["vs"][h] > 0) / len(scored) * 100
            return (f"Buy/Overweight calls after {h} trading days: {avg:+.1f}% vs {BENCHMARK} on average, "
                    f"{hits:.0f}% beat it ({len(scored)} call{'s' if len(scored) != 1 else ''})")
    return f"Not enough history yet: the first {HORIZONS[0]}-day results appear {HORIZONS[0]} trading days after a call"
