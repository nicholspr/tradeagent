"""Daily strategy paper tests (added 15 Nov 2026).

Three slower strategies with better published evidence than minute-by-minute entries, tested with the same
theoretical $100 trades and eToro costs as the auto-test (results appear with the other strategies):

- trend100: hold BTC, ETH or SOL while the last completed daily close is above its 100-day average; out below it.
  Checked at 00:10 UK time, just after the daily crypto candle closes. Few trades, so the 2% crypto fee matters less.
- ibs: Internal Bar Strength on CSPX.L (iShares Core S&P 500 UCITS, London). Near the close (16:20 UK), buy if the
  day so far closed in the bottom 20% of its range; sell on a close in the top 20%, above the previous day's high,
  or after 5 weekdays.
- tom: turn of the month on CSPX.L. Buy near the close 4 weekdays before month end; sell near the close on the
  3rd weekday of the new month (weekdays approximate trading days; UK holidays are skipped as no candle exists).

Paper only: nothing here ever sends an order to eToro.
"""
import json
import time
from datetime import date, datetime, timedelta, timezone

from . import autotest, engine, store

TREND = [(100000, "BTC"), (100001, "ETH"), (100063, "SOL")]
FUND = (3434, "CSPX.L", "etf")
SMA_DAYS = 100
IBS_LOW, IBS_HIGH, IBS_MAX_DAYS = 0.2, 0.8, 5
TOM_BEFORE, TOM_AFTER = 4, 3


def sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ibs(bar: dict) -> float:
    rng = bar["high"] - bar["low"]
    return (bar["close"] - bar["low"]) / rng if rng > 0 else 0.5


def _weekdays(d0: date, d1: date) -> int:
    """Weekdays strictly after d0 up to and including d1."""
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def weekdays_left(day: date) -> int:
    """Weekdays after `day` until the end of its month."""
    nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return _weekdays(day, nxt - timedelta(days=1))


def weekday_number(day: date) -> int:
    """1 for the first weekday of the month, 2 for the second, ..."""
    return _weekdays(day.replace(day=1) - timedelta(days=1), day)


def tom_action(day: date, holding: dict | None) -> str | None:
    if day.weekday() >= 5:
        return None
    if not holding and weekdays_left(day) == TOM_BEFORE - 1:
        return "enter"
    if holding:
        opened = datetime.fromtimestamp(holding["opened_ts"], timezone.utc).date()
        if (day.month != opened.month and weekday_number(day) >= TOM_AFTER) or (day - opened).days > 14:
            return "exit"
    return None


def _open(key: str, iid: int, sym: str, kind: str, rate: dict, reasons: list[str], stop: float | None = None) -> None:
    bid, ask = float(rate["bid"]), float(rate.get("ask") or rate["bid"])
    now = time.time()
    hurdle = autotest.hurdle_pct(kind, sym, ask, bid)
    with store._lock, store.connect() as c:
        c.execute("INSERT INTO autotrades(iid, symbol, kind, strategy, opened, opened_ts, amt, entry, bid0, stop, target, "
                  "hurdle, reasons, status, checked_ts, last_bid) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?, 'open', ?, ?)",
                  (iid, sym, kind, key, autotest._now_iso(now), now, autotest.AMT, ask, bid, stop, hurdle,
                   json.dumps(reasons), now, bid))
    store.log("info", f"auto-test {key}: opened {sym} at {ask:.6g} ({reasons[0]})")


def _rates(ec, ids: list[int]) -> dict:
    try:
        return ec.live_rates(ids)
    except Exception:  # noqa: BLE001
        return ec.rates(ids)


def run_trend(ec=None, now: datetime | None = None) -> list[str]:
    """00:10 UK: crypto daily trend."""
    autotest._ensure()
    ec = ec or engine.client()
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    rates = _rates(ec, [i for i, _ in TREND])
    done = []
    for iid, sym in TREND:
        try:
            bars = [b for b in ec.candles(iid, "OneDay", SMA_DAYS + 20) if not str(b["date"]).startswith(today)]
            closes = [b["close"] for b in bars]
            avg = sma(closes, SMA_DAYS)
            r = rates.get(iid)
            if avg is None or not r or r.get("bid") is None:
                continue
            t = autotest._open_trade(iid, "trend100")
            above = closes[-1] > avg
            if above and not t:
                _open("trend100", iid, sym, "crypto", r,
                      [f"Daily close {closes[-1]:.6g} is above the {SMA_DAYS}-day average {avg:.6g}"], stop=avg)
                done.append(f"{sym} in")
            elif not above and t:
                autotest._close(t, float(r["bid"]), f"close below {SMA_DAYS}-day average", time.time())
                done.append(f"{sym} out")
            elif t:
                with store._lock, store.connect() as c:
                    c.execute("UPDATE autotrades SET last_bid=?, checked_ts=?, stop=? WHERE id=?",
                              (float(r["bid"]), time.time(), avg, t["id"]))
        except Exception as e:  # noqa: BLE001
            store.log("warn", f"auto-test trend100: {sym} check failed: {e}")
    return done


def run_fund(ec=None, now: datetime | None = None) -> list[str]:
    """16:20 UK on weekdays: IBS and turn of the month on the S&P 500 fund, near the London close."""
    autotest._ensure()
    ec = ec or engine.client()
    now = now or datetime.now(timezone.utc)
    iid, sym, kind = FUND
    bars = ec.candles(iid, "OneDay", 30)
    if not bars or not str(bars[-1]["date"]).startswith(now.date().isoformat()):
        store.log("info", f"auto-test daily: no {sym} candle today (market holiday?), nothing to do")
        return []
    r = _rates(ec, [iid]).get(iid)
    if not r or r.get("bid") is None:
        return []
    bid, done = float(r["bid"]), []
    today, prev = bars[-1], bars[-2] if len(bars) > 1 else None
    v = ibs(today)
    t = autotest._open_trade(iid, "ibs")
    if t:
        held = _weekdays(datetime.fromtimestamp(t["opened_ts"], timezone.utc).date(), now.date())
        why = ("closed in the top 20% of the day's range" if v >= IBS_HIGH else
               "closed above the previous day's high" if prev and today["close"] > prev["high"] else
               f"{IBS_MAX_DAYS} days held" if held >= IBS_MAX_DAYS else None)
        if why:
            autotest._close(t, bid, why, time.time())
            done.append(f"IBS out ({why})")
    elif v <= IBS_LOW:
        _open("ibs", iid, sym, kind, r, [f"Closing in the bottom {IBS_LOW:.0%} of the day's range (IBS {v:.2f})"])
        done.append("IBS in")
    t = autotest._open_trade(iid, "tom")
    act = tom_action(now.date(), t)
    if act == "enter":
        _open("tom", iid, sym, kind, r, [f"{TOM_BEFORE} weekdays before the end of the month"])
        done.append("turn of month in")
    elif act == "exit" and t:
        autotest._close(t, bid, f"weekday {TOM_AFTER} of the new month", time.time())
        done.append("turn of month out")
    return done
