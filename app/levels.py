"""Watch levels: entry conditions the agents set in a report, checked against each day's close.

After an analysis that did not trade, the conditions it states ("buy on a daily close above 11.015",
"buy a pull-back into 10.46-10.68", "the case breaks on a close below 10.17") are turned into numbers by a
small model call and stored. Every morning the previous day's completed eToro candle is checked:

- close_above: triggered when the daily close is above the level
- zone:        triggered when the day's low reached the top of the zone
- invalid_below (per ticker): every condition for that ticker is cancelled on a close below it

A triggered condition moves its share to the front of the next scheduled analysis, and the agents are told
which condition was met. It never places an order itself: the new analysis's rating and the normal limits
decide whether anything trades.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from . import config, notify, policy, store

DEFAULT_DAYS = 14
MAX_DAYS = 30
ACTIVE = ("watching", "triggered")

SYSTEM = ("You extract explicit, numeric entry conditions from a trading analysis. Only report conditions the "
          "text states with price numbers. Never invent levels. Reply with JSON only.")
PROMPT = """From this analysis of {ticker}, list the conditions under which it says to BUY or ADD.
Use only these types:
- "close_above": buy if a daily close is above "level" (a breakout).
- "zone": buy if price pulls back into the range "low"-"high" (a single number means low = high).
Also give "invalid_below": the price at which the text says the buy idea is broken or the stop sits, or null.
Also give "horizon_days": the time horizon in days if stated (e.g. "1-3 months" = 30), or null.
Return {{"conditions": [...], "invalid_below": number or null, "horizon_days": number or null}}.
Return {{"conditions": []}} if the text states no numeric buy condition.
Current price: {price}

Analysis:
{text}"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def call_model(prompt: str) -> str:
    import anthropic
    resp = anthropic.Anthropic(timeout=120.0).messages.create(
        model=config.model_for("quick"), max_tokens=800, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in resp.content)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"conditions": []}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {"conditions": []}


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _clean(data: dict, price: float | None) -> tuple[list[dict], float | None, int]:
    """Keep only well-formed conditions within 40% of the current price (guards against misread numbers)."""
    def sane(x):
        return x is not None and (not price or 0.6 * price <= x <= 1.4 * price)
    out = []
    for c in data.get("conditions") or []:
        kind = str(c.get("type") or "").lower()
        if kind == "close_above":
            lvl = _num(c.get("level"))
            if sane(lvl):
                out.append({"kind": "close_above", "low": lvl, "high": lvl})
        elif kind == "zone":
            lo, hi = _num(c.get("low")), _num(c.get("high"))
            lo, hi = lo or hi, hi or lo
            if lo and hi:
                lo, hi = min(lo, hi), max(lo, hi)
                if sane(lo) and sane(hi):
                    out.append({"kind": "zone", "low": lo, "high": hi})
    inv = _num(data.get("invalid_below"))
    inv = inv if sane(inv) else None
    days = _num(data.get("horizon_days"))
    days = int(min(days, MAX_DAYS)) if days else DEFAULT_DAYS
    return out, inv, max(days, 1)


def describe(lv: dict) -> str:
    if lv["kind"] == "close_above":
        d = f"daily close above {lv['low']:,.6g}"
    else:
        d = (f"pull-back into {lv['low']:,.6g}–{lv['high']:,.6g}" if lv["high"] != lv["low"]
             else f"pull-back to {lv['low']:,.6g}")
    if lv.get("invalid_below"):
        d += f" (cancelled on a close below {lv['invalid_below']:,.6g})"
    return d


def active(ticker: str | None = None) -> list[dict]:
    q, args = f"SELECT * FROM levels WHERE status IN ({','.join('?' * len(ACTIVE))})", list(ACTIVE)
    if ticker:
        q += " AND ticker=?"
        args.append(ticker)
    with store._lock, store.connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args)]


def triggered_tickers() -> list[str]:
    return sorted({lv["ticker"] for lv in active() if lv["status"] == "triggered"})


def trigger_note(ticker: str) -> str:
    """Text added to the agents' account rules when a share is analysed because a condition was met."""
    hits = [lv for lv in active(ticker) if lv["status"] == "triggered"]
    if not hits:
        return ""
    lines = [f"- {describe(lv)}: {lv['trigger_info']}" for lv in hits]
    return ("Earlier condition met (set by a previous analysis of this instrument; re-check it with fresh data "
            "before relying on it):\n" + "\n".join(lines))


def _set(lid: int, **cols) -> None:
    keys = list(cols)
    with store._lock, store.connect() as c:
        c.execute(f"UPDATE levels SET {', '.join(k + '=?' for k in keys)} WHERE id=?", [cols[k] for k in keys] + [lid])


def record(ticker: str, run_id: int, decision: str, price: float | None, when: datetime | None = None) -> list[dict]:
    """Replace the ticker's conditions with those in this analysis. Returns the new conditions.

    `when` is when the analysis was made (defaults to now); the expiry counts from it."""
    now = when or _now()
    with store._lock, store.connect() as c:
        c.execute("UPDATE levels SET status='used' WHERE ticker=? AND status='triggered'", (ticker,))
        c.execute("UPDATE levels SET status='replaced' WHERE ticker=? AND status='watching'", (ticker,))
    try:
        conds, inv, days = _clean(_parse(call_model(PROMPT.format(ticker=ticker, price=price or "unknown",
                                                                   text=(decision or "")[:12000]))), price)
    except Exception as e:  # noqa: BLE001
        store.log("warn", f"watch levels for {ticker}: could not read conditions ({e})")
        return []
    expires = (now + timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    with store._lock, store.connect() as c:
        for cd in conds:
            c.execute("INSERT INTO levels(ticker, run_id, kind, low, high, invalid_below, created, expires, status) "
                      "VALUES (?,?,?,?,?,?,?,?, 'watching')",
                      (ticker, run_id, cd["kind"], cd["low"], cd["high"], inv, now.isoformat(timespec="seconds"), expires))
            out.append({**cd, "invalid_below": inv})
    if out:
        store.log("info", f"watch levels for {ticker}: " + "; ".join(describe(x) for x in out) + f" (for {days} days)")
    return out


def record_none(ticker: str) -> None:
    """A Buy/Overweight analysis acts now, so earlier conditions for the ticker are done with."""
    with store._lock, store.connect() as c:
        c.execute("UPDATE levels SET status='used' WHERE ticker=? AND status='triggered'", (ticker,))
        c.execute("UPDATE levels SET status='replaced' WHERE ticker=? AND status='watching'", (ticker,))


def _is_opened_today(opened, now: datetime) -> bool:
    if not opened:
        return False
    try:
        t = datetime.fromisoformat(str(opened).replace("Z", "+00:00")[:26])
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.date() == now.date()


def _completed_daily(candles: list[dict], now: datetime) -> dict | None:
    """The most recent daily candle that has finished (today's candle is still forming)."""
    today = now.date().isoformat()
    done = [c for c in candles if not str(c["date"]).startswith(today)]
    return done[-1] if done else None


def check(ec, now: datetime | None = None) -> dict:
    """Check every active condition against the last completed daily candle. Returns what changed."""
    now = now or _now()
    changed = {"triggered": [], "cancelled": [], "expired": []}
    by_ticker = {}
    for lv in active():
        by_ticker.setdefault(lv["ticker"], []).append(lv)
    for ticker, lvs in by_ticker.items():
        try:
            inst = ec.find_instrument(ticker)
            bar = _completed_daily(ec.candles(inst["instrumentId"], "OneDay", 3), now)
        except Exception as e:  # noqa: BLE001
            store.log("warn", f"watch levels: could not check {ticker}: {e}")
            continue
        if not bar:
            continue
        day = str(bar["date"])[:10]
        for lv in lvs:
            stamp = now.isoformat(timespec="seconds")
            if lv["expires"] and stamp > lv["expires"]:
                _set(lv["id"], status="expired", checked=stamp)
                changed["expired"].append((ticker, lv))
                continue
            if lv["invalid_below"] and bar["close"] < lv["invalid_below"]:
                info = f"closed {bar['close']:,.6g} on {day}, below {lv['invalid_below']:,.6g}"
                _set(lv["id"], status="cancelled", checked=stamp, last_close=bar["close"], trigger_info=info)
                changed["cancelled"].append((ticker, lv, info))
                continue
            hit = None
            if lv["status"] == "watching":
                if lv["kind"] == "close_above" and bar["close"] > lv["low"]:
                    hit = f"closed {bar['close']:,.6g} on {day}"
                elif lv["kind"] == "zone" and bar["low"] <= lv["high"]:
                    hit = f"traded down to {bar['low']:,.6g} on {day} (close {bar['close']:,.6g})"
            if hit:
                _set(lv["id"], status="triggered", checked=stamp, last_close=bar["close"], triggered=stamp, trigger_info=hit)
                changed["triggered"].append((ticker, lv, hit))
            else:
                _set(lv["id"], checked=stamp, last_close=bar["close"])
    return changed


def overnight(ec) -> dict:
    """Scheduled morning check: update conditions, log and email anything that changed."""
    changed = check(ec)
    lines = []
    for ticker, lv, info in changed["triggered"]:
        lines.append(f"MET  {ticker}: {describe(lv)}: {info}. It will be analysed first in its next run.")
    for ticker, lv, info in changed["cancelled"]:
        lines.append(f"CANCELLED  {ticker}: {describe(lv)}: {info}.")
    for ticker, lv in changed["expired"]:
        lines.append(f"EXPIRED  {ticker}: {describe(lv)}.")
    store.log("info", "watch levels checked: " + (f"{len(changed['triggered'])} met, {len(changed['cancelled'])} "
                                                  f"cancelled, {len(changed['expired'])} expired" if lines else "no change"))
    if changed["triggered"] or changed["cancelled"]:
        notify.email("Watch levels", "\n".join(lines) + f"\n\n{config.PUBLIC_BASE_URL}/")
    return changed


def prioritise(items: list) -> list:
    """Watchlist items with a met condition first, then the rest in their existing order."""
    hot = set(triggered_tickers())
    return [i for i in items if i.symbol in hot] + [i for i in items if i.symbol not in hot]


BACKFILL_DAYS = 7


def backfill(ec, items: list, days: int = BACKFILL_DAYS) -> list[str]:
    """Read conditions from each watchlist symbol's latest saved report (no new analysis).

    Skips symbols that already have active conditions, reports older than `days`, failed runs and
    Buy/Overweight reports (those acted at the time). Returns a line per symbol for the log."""
    since = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for item in items:
        if active(item.symbol):
            continue
        with store._lock, store.connect() as c:
            r = c.execute("SELECT * FROM runs WHERE ticker=? AND decision IS NOT NULL AND decision != '' "
                          "ORDER BY id DESC LIMIT 1", (item.symbol,)).fetchone()
        if not r or (r["finished"] or r["started"] or "") < since:
            continue
        if str(r["rating"] or "").lower() in ("buy", "overweight"):
            continue
        try:
            price = ec.find_instrument(item.symbol).get("rate")
            price = float(price) if price else None
        except Exception:  # noqa: BLE001
            price = None
        try:
            when = datetime.fromisoformat(r["finished"] or r["started"])
        except (TypeError, ValueError):
            when = None
        got = record(item.symbol, r["id"], r["decision"], price, when=when)
        out.append(f"{item.symbol} (run #{r['id']}): " + ("; ".join(describe(g) for g in got) if got else "no numeric condition"))
    store.log("info", "watch levels read from saved reports: " + (" | ".join(out) if out else "nothing to read"))
    return out
