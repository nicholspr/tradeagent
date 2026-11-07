"""Strategy auto-test: theoretical $100 paper trades opened and closed by the server, 24/7.

Every minute, for each symbol on the auto-test list:
- if it has no open auto-test trade and the strategy (app/strategy.py) says "Entry now",
  a paper buy of $100 is recorded at the live ask, with the signal's stop and target;
- an open trade is closed at its stop or target, using every 1-minute candle since it opened
  (if one candle touched both, the stop is assumed to have come first, the cautious choice),
  or at the live bid after MAX_HOURS.
Costs follow the same eToro rules as the day trader page. Nothing is ever sent to eToro.
Results build up the strategy's real hit rate after fees, symbol by symbol.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone

from . import daytrader, engine, store, strategy

AMT = 100.0
MAX_HOURS = 4.0
COMMISSION = 1.0          # $ per side for shares, as the page's default
SEED = [(100000, "BTC", "crypto"), (100063, "SOL", "crypto"), (100340, "SUI", "crypto"),
        (100085, "AVAX", "crypto"), (15200, "TSLA.24-7", "cfd"), (15574, "NVDA.24-7", "cfd")]

CREATE = """
CREATE TABLE IF NOT EXISTS autotest_symbols (
    iid INTEGER PRIMARY KEY, symbol TEXT, kind TEXT, added TEXT,
    last_state TEXT, last_headline TEXT, last_checked TEXT, last_bid REAL);
CREATE TABLE IF NOT EXISTS autotrades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, iid INTEGER, symbol TEXT, kind TEXT,
    opened TEXT, opened_ts REAL, amt REAL, entry REAL, bid0 REAL, stop REAL, target REAL,
    hurdle REAL, reasons TEXT, status TEXT, checked_ts REAL, last_bid REAL,
    closed TEXT, closed_ts REAL, exit REAL, reason TEXT, gross REAL, fees REAL, net REAL);
CREATE TABLE IF NOT EXISTS autotest_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(timespec="seconds")


def _ensure() -> None:
    with store._lock, store.connect() as c:
        c.executescript(CREATE)
        if not c.execute("SELECT 1 FROM autotest_meta WHERE key='seeded'").fetchone():
            for iid, sym, kind in SEED:
                c.execute("INSERT OR IGNORE INTO autotest_symbols(iid, symbol, kind, added) VALUES (?,?,?,?)",
                          (iid, sym, kind, _now_iso()))
            c.execute("INSERT INTO autotest_meta(key, value) VALUES ('seeded', ?)", (_now_iso(),))


def costs(kind: str, symbol: str, amt: float, ask: float, bid: float, comm: float = COMMISSION, fx: float = 0.0) -> dict:
    """Same rules as costs() on the day trader page: buy `amt` at `ask`, sell at `bid`."""
    units = amt / ask
    value = units * bid
    lines = []
    if kind == "crypto":
        lines += [("Crypto fee 1% (buy)", amt * 0.01), ("Crypto fee 1% (sell)", value * 0.01)]
    if kind == "stock" and comm > 0:
        lines += [("Commission (open)", comm), ("Commission (close)", comm)]
    if kind == "stock" and symbol.upper().endswith(".L"):
        lines.append(("UK stamp duty 0.5%", math.ceil(amt * 0.5) / 100))
    if fx > 0:
        lines += [("Currency conversion (in)", amt * fx / 100), ("Currency conversion (out)", value * fx / 100)]
    fees = sum(v for _, v in lines)
    gross = value - amt
    return {"units": units, "value": value, "gross": gross, "fees": fees, "net": gross - fees, "lines": lines}


def hurdle_pct(kind: str, symbol: str, ask: float, bid: float) -> float:
    """Round-trip fees on $100 (excluding the spread) plus the live spread, in %."""
    if kind == "crypto":
        fees = (1.01 / 0.99 - 1) * 100     # 1% to buy, then 1% of the (higher) sale value: 2.02%
    elif kind == "stock":
        fees = 2 * COMMISSION + (0.5 if symbol.upper().endswith(".L") else 0.0)
    else:
        fees = 0.0
    return fees + (ask - bid) / bid * 100


def symbols() -> list[dict]:
    _ensure()
    with store._lock, store.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM autotest_symbols ORDER BY symbol")]


def add_symbol(iid: int, symbol: str, kind: str) -> None:
    _ensure()
    with store._lock, store.connect() as c:
        c.execute("INSERT OR IGNORE INTO autotest_symbols(iid, symbol, kind, added) VALUES (?,?,?,?)",
                  (int(iid), symbol, kind, _now_iso()))
    store.log("info", f"auto-test: added {symbol}")


def remove_symbol(iid: int) -> None:
    """Stop testing a symbol. An open trade in it is closed at the last checked price."""
    _ensure()
    t = _open_trade(iid)
    if t:
        _close(t, t["last_bid"] or t["bid0"], "symbol removed", time.time())
    with store._lock, store.connect() as c:
        c.execute("DELETE FROM autotest_symbols WHERE iid=?", (int(iid),))
    store.log("info", f"auto-test: removed instrument {iid}")


def _open_trade(iid: int) -> dict | None:
    with store._lock, store.connect() as c:
        r = c.execute("SELECT * FROM autotrades WHERE iid=? AND status='open' ORDER BY id DESC LIMIT 1", (int(iid),)).fetchone()
    return dict(r) if r else None


def _close(t: dict, exit_bid: float, reason: str, ts: float) -> None:
    c_ = costs(t["kind"], t["symbol"], t["amt"], t["entry"], exit_bid)
    with store._lock, store.connect() as c:
        c.execute("UPDATE autotrades SET status='closed', closed=?, closed_ts=?, exit=?, reason=?, gross=?, fees=?, net=? "
                  "WHERE id=? AND status='open'",
                  (_now_iso(ts), ts, exit_bid, reason, c_["gross"], c_["fees"], c_["net"], t["id"]))
    store.log("info", f"auto-test: closed {t['symbol']} ({reason}) at {exit_bid:.6g}, net ${c_['net']:+.2f}")


def check_exit(t: dict, bars: list[dict], bid: float | None, is_open: bool, now: float) -> tuple[str, float] | None:
    """(reason, exit price) if the trade should close now, else None."""
    first = (int(t["opened_ts"] // 60) + 1) * 60          # candles that started after the entry minute
    for b in bars:
        if b["t"] < first:
            continue
        if b["l"] <= t["stop"]:
            return "stop", t["stop"]
        if b["h"] >= t["target"]:
            return "target", t["target"]
    if bid is not None and is_open:
        if bid <= t["stop"]:
            return "stop", bid
        if bid >= t["target"]:
            return "target", bid
        if now - t["opened_ts"] >= MAX_HOURS * 3600:
            return f"time limit ({MAX_HOURS:g}h)", bid
    return None


def _bars(iid: int) -> list[dict]:
    ec = engine.client()
    long = daytrader._cached(("hist", iid), 25, lambda: ec.candles(iid, "OneMinute", 300))
    out = []
    for c in long:
        t = daytrader._parse_ts(c["date"])
        if t is not None:
            out.append({"t": t, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]})
    return out


def tick() -> None:
    """Run once a minute by the scheduler."""
    syms = symbols()
    if not syms:
        return
    ec = engine.client()
    ids = [s["iid"] for s in syms]
    try:
        rates = ec.live_rates(ids)
    except Exception:  # noqa: BLE001
        rates = ec.rates(ids)
    now = time.time()
    for s in syms:
        try:
            _tick_one(s, rates.get(s["iid"]), now)
        except Exception as e:  # noqa: BLE001
            with store._lock, store.connect() as c:
                c.execute("UPDATE autotest_symbols SET last_state='error', last_headline=?, last_checked=? WHERE iid=?",
                          (str(e)[:200], _now_iso(now), s["iid"]))


def _tick_one(s: dict, r: dict | None, now: float) -> None:
    iid, sym, kind = s["iid"], s["symbol"], s["kind"]
    bid = ask = None
    is_open = False
    if r and r.get("bid") is not None:
        bid, ask = float(r["bid"]), float(r.get("ask") or r["bid"])
        ts = daytrader._parse_ts(r.get("date", "")) or now
        is_open = now - ts <= daytrader.STALE_AFTER
    bars = _bars(iid)
    t = _open_trade(iid)
    if t:
        ex = check_exit(t, bars, bid, is_open, now)
        if ex:
            _close(t, ex[1], ex[0], now)
        elif bid is not None:
            with store._lock, store.connect() as c:
                c.execute("UPDATE autotrades SET last_bid=?, checked_ts=? WHERE id=?", (bid, now, t["id"]))
    hurdle = hurdle_pct(kind, sym, ask, bid) if bid else strategy.default_hurdle(kind)
    sig = strategy.evaluate(bars, bid, ask, is_open, hurdle)
    with store._lock, store.connect() as c:
        c.execute("UPDATE autotest_symbols SET last_state=?, last_headline=?, last_checked=?, last_bid=? WHERE iid=?",
                  (sig["state"], sig["headline"], _now_iso(now), bid, iid))
    if t and not ex:
        return                                              # one trade per symbol at a time
    if sig["state"] == "enter" and ask is not None:
        with store._lock, store.connect() as c:
            c.execute("INSERT INTO autotrades(iid, symbol, kind, opened, opened_ts, amt, entry, bid0, stop, target, hurdle, "
                      "reasons, status, checked_ts, last_bid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
                      (iid, sym, kind, _now_iso(now), now, AMT, ask, bid, sig["stop"], sig["target"], hurdle,
                       json.dumps(sig["reasons"]), now, bid))
        store.log("info", f"auto-test: opened {sym} at {ask:.6g} (stop {sig['stop']:.6g}, target {sig['target']:.6g})")


def summary(limit: int = 50) -> dict:
    """Everything the day trader page shows for the auto-test."""
    _ensure()
    with store._lock, store.connect() as c:
        open_ = [dict(r) for r in c.execute("SELECT * FROM autotrades WHERE status='open' ORDER BY opened_ts DESC")]
        closed = [dict(r) for r in c.execute("SELECT * FROM autotrades WHERE status='closed' ORDER BY closed_ts DESC LIMIT ?", (limit,))]
        allc = [dict(r) for r in c.execute("SELECT symbol, net, fees, gross, reason FROM autotrades WHERE status='closed'")]
    for t in open_:
        if t["last_bid"]:
            t.update({k: v for k, v in costs(t["kind"], t["symbol"], t["amt"], t["entry"], t["last_bid"]).items() if k != "lines"})
        t.pop("reasons", None)
    for t in closed:
        t.pop("reasons", None)
    by: dict[str, dict] = {}
    for t in allc:
        b = by.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "net": 0.0, "fees": 0.0, "gross": 0.0})
        b["trades"] += 1
        b["wins"] += t["net"] > 0
        b["net"] += t["net"]
        b["fees"] += t["fees"]
        b["gross"] += t["gross"]
    total = {"trades": len(allc), "wins": sum(t["net"] > 0 for t in allc), "net": sum(t["net"] for t in allc),
             "fees": sum(t["fees"] for t in allc), "gross": sum(t["gross"] for t in allc),
             "targets": sum(t["reason"] == "target" for t in allc), "stops": sum(t["reason"] == "stop" for t in allc)}
    return {"symbols": symbols(), "open": open_, "closed": closed, "by_symbol": sorted(by.values(), key=lambda b: -b["trades"]),
            "total": total, "max_hours": MAX_HOURS, "amt": AMT}
