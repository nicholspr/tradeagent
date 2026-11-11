"""Strategy auto-test: theoretical $100 paper trades opened and closed by the server, 24/7.

Four versions of the entry strategy (app/strategy.py) run side by side on every symbol:
- fast:  5-minute trend, 1-minute timing; trades close after at most 4 hours (the unchanged baseline);
- fast2: fast plus filters (quiet markets, 1-hour trend, spread-aware stops, home-market hours, spread cap);
- fast3: fast2 with a 1.5:1 target and the stop moved to break-even once the trade is 1R in profit;
- swing: 1-hour trend, 15-minute timing, wider stops and targets; at most 48 hours.
Every trade records its initial risk (the net loss if stopped), so results are also shown in R
(net / initial risk), and the best price reached while open (MFE), to show where targets should sit.

Every minute, for each symbol on the list and each strategy:
- with no open trade for that symbol and strategy, an "Entry now" signal records a $100 paper buy
  at the live ask, with the signal's stop and target;
- an open trade is closed at its stop or target, using every 1-minute candle since it opened
  (if one candle touched both, the stop is assumed to have come first, the cautious choice),
  or at the live bid when its time limit is reached.
Costs follow the same eToro rules as the day trader page. Nothing is ever sent to eToro.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone

from . import daytrader, engine, store, strategy

AMT = 100.0
COMMISSION = 1.0          # $ per side for shares, as the page's default
STRATEGIES = {"fast": {"label": "Fast (5-min trend)", "short": "Fast", "max_hours": 4.0},
              "fast2": {"label": "Fast v2 (filtered)", "short": "Fast v2", "max_hours": 4.0,
                        "cooldown_min": 30, "max_stops_day": 2},
              "fast3": {"label": "Fast v3 (1.5:1, break-even)", "short": "Fast v3", "max_hours": 4.0,
                        "cooldown_min": 30, "max_stops_day": 2, "be_at_r": 1.0},
              "fast2x": {"label": "Fast v2 + US hours (SK hynix 24/7 also in US hours)", "short": "v2+US",
                         "max_hours": 4.0, "cooldown_min": 30, "max_stops_day": 2, "only": ("SKHY",)},
              # Swing: not on crypto or commission shares (its 2% / $2 costs turned stops into $4-5 losses),
              # and one trade at a time per group of symbols that move together (see strategy.GROUPS).
              "swing": {"label": "Swing (1-hour trend)", "short": "Swing", "max_hours": 48.0,
                        "skip_kinds": ("crypto", "stock"), "one_per_group": True}}
MAX_HOURS = STRATEGIES["fast"]["max_hours"]
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
        cols = [r["name"] for r in c.execute("PRAGMA table_info(autotrades)")]
        if "strategy" not in cols:
            c.execute("ALTER TABLE autotrades ADD COLUMN strategy TEXT DEFAULT 'fast'")
        if "mfe" not in cols:
            c.execute("ALTER TABLE autotrades ADD COLUMN mfe REAL")       # highest price seen while open
        scols = [r["name"] for r in c.execute("PRAGMA table_info(autotest_symbols)")]
        for col, kind in (("swing_state", "TEXT"), ("swing_headline", "TEXT"), ("name", "TEXT"),
                          ("fast_on", "INTEGER DEFAULT 1"), ("swing_on", "INTEGER DEFAULT 1"),
                          ("fast2_state", "TEXT"), ("fast2_headline", "TEXT"), ("fast2_on", "INTEGER DEFAULT 1"),
                          ("fast3_state", "TEXT"), ("fast3_headline", "TEXT"), ("fast3_on", "INTEGER DEFAULT 1"),
                          ("fast2x_state", "TEXT"), ("fast2x_headline", "TEXT"), ("fast2x_on", "INTEGER DEFAULT 1")):
            if col not in scols:
                c.execute(f"ALTER TABLE autotest_symbols ADD COLUMN {col} {kind}")
                if col == "fast2x_on":                          # the variant starts on only where it differs
                    for row in c.execute("SELECT iid, symbol FROM autotest_symbols").fetchall():
                        c.execute("UPDATE autotest_symbols SET fast2x_on=? WHERE iid=?",
                                  (1 if _default_on("fast2x", row["symbol"]) else 0, row["iid"]))
        if not c.execute("SELECT 1 FROM autotest_meta WHERE key='seeded'").fetchone():
            for iid, sym, kind in SEED:
                c.execute("INSERT OR IGNORE INTO autotest_symbols(iid, symbol, kind, added, fast2x_on) VALUES (?,?,?,?,?)",
                          (iid, sym, kind, _now_iso(), 1 if _default_on("fast2x", sym) else 0))
            c.execute("INSERT INTO autotest_meta(key, value) VALUES ('seeded', ?)", (_now_iso(),))


def _default_on(key: str, symbol: str) -> bool:
    """Strategies with an 'only' list start switched on just for those symbols."""
    only = STRATEGIES[key].get("only")
    return not only or (symbol or "").upper().split(".")[0] in only


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


def add_symbol(iid: int, symbol: str | None = None, kind: str | None = None, name: str = "") -> dict:
    """Add an eToro instrument. Symbol, name and kind (which decides the costs) are looked up on
    eToro when not given. Both strategies start switched on."""
    _ensure()
    if symbol is None or kind is None:
        sym, nm, kd = daytrader._cached(("name", int(iid)), 3600, lambda: daytrader._name(int(iid)))
        if sym.startswith("#"):
            raise ValueError(f"eToro has no instrument {iid}")
        symbol, kind, name = symbol or sym, kind or kd, name or nm
    with store._lock, store.connect() as c:
        c.execute("INSERT OR IGNORE INTO autotest_symbols(iid, symbol, kind, name, added, fast_on, swing_on, fast2_on, "
                  "fast3_on, fast2x_on) VALUES (?,?,?,?,?,1,1,1,1,?)",
                  (int(iid), symbol, kind, name, _now_iso(), 1 if _default_on("fast2x", symbol) else 0))
    store.log("info", f"auto-test: added {symbol}")
    return {"iid": int(iid), "symbol": symbol, "kind": kind, "name": name}


def set_enabled(iid: int, key: str, on: bool) -> None:
    """Switch one strategy on or off for a symbol. Switching off stops new trades; a trade already
    open carries on to its stop, target or time limit."""
    if key not in STRATEGIES:
        raise ValueError(f"unknown strategy {key}")
    _ensure()
    with store._lock, store.connect() as c:
        c.execute(f"UPDATE autotest_symbols SET {key}_on=? WHERE iid=?", (1 if on else 0, int(iid)))
    store.log("info", f"auto-test: {key} {'on' if on else 'off'} for instrument {iid}")


def remove_symbol(iid: int) -> None:
    """Stop testing a symbol. Open trades in it are closed at the last checked price."""
    _ensure()
    for key in STRATEGIES:
        t = _open_trade(iid, key)
        if t:
            _close(t, t["last_bid"] or t["bid0"], "symbol removed", time.time())
    with store._lock, store.connect() as c:
        c.execute("DELETE FROM autotest_symbols WHERE iid=?", (int(iid),))
    store.log("info", f"auto-test: removed instrument {iid}")


def _open_trade(iid: int, key: str = "fast") -> dict | None:
    with store._lock, store.connect() as c:
        r = c.execute("SELECT * FROM autotrades WHERE iid=? AND status='open' AND COALESCE(strategy,'fast')=? "
                      "ORDER BY id DESC LIMIT 1", (int(iid), key)).fetchone()
    return dict(r) if r else None


def _close(t: dict, exit_bid: float, reason: str, ts: float) -> None:
    c_ = costs(t["kind"], t["symbol"], t["amt"], t["entry"], exit_bid)
    with store._lock, store.connect() as c:
        c.execute("UPDATE autotrades SET status='closed', closed=?, closed_ts=?, exit=?, reason=?, gross=?, fees=?, net=? "
                  "WHERE id=? AND status='open'",
                  (_now_iso(ts), ts, exit_bid, reason, c_["gross"], c_["fees"], c_["net"], t["id"]))
    store.log("info", f"auto-test {t.get('strategy') or 'fast'}: closed {t['symbol']} ({reason}) at {exit_bid:.6g}, "
                      f"net ${c_['net']:+.2f}")


def check_exit(t: dict, bars: list[dict], bid: float | None, is_open: bool, now: float,
               max_hours: float = MAX_HOURS, be_at_r: float | None = None) -> tuple[str, float] | None:
    """(reason, exit price) if the trade should close now, else None.
    With be_at_r, the stop moves up to the entry price once a candle's high has reached entry + be_at_r x the
    initial risk; it takes effect from the next candle (inside one candle the order of high and low is unknown)."""
    first = (int(t["opened_ts"] // 60) + 1) * 60          # candles that started after the entry minute
    stop, armed = t["stop"], False
    be_level = t["entry"] + be_at_r * (t["entry"] - t["stop"]) if be_at_r else None
    for b in bars:
        if b["t"] < first:
            continue
        if b["l"] <= stop:
            return ("break-even", stop) if armed else ("stop", stop)
        if b["h"] >= t["target"]:
            return "target", t["target"]
        if be_level is not None and not armed and b["h"] >= be_level:
            armed, stop = True, max(stop, t["entry"])
    if bid is not None and is_open:
        if bid <= stop:
            return ("break-even", bid) if armed else ("stop", bid)
        if bid >= t["target"]:
            return "target", bid
        if now - t["opened_ts"] >= max_hours * 3600:
            return f"time limit ({max_hours:g}h)", bid
    return None


def best_price(t: dict, bars: list[dict], bid: float | None) -> float | None:
    """Highest price reached since the entry (candle highs after the entry minute, and the live bid),
    never below what was already recorded."""
    first = (int(t["opened_ts"] // 60) + 1) * 60
    vals = [b["h"] for b in bars if b["t"] >= first] + ([bid] if bid is not None else []) + ([t["mfe"]] if t.get("mfe") else [])
    return max(vals) if vals else None


def risk_usd(t: dict) -> float:
    """Initial risk: the net loss (fees included) if the trade had closed at its original stop."""
    return -costs(t["kind"], t["symbol"], t["amt"], t["entry"], t["stop"])["net"]


def r_stats(t: dict) -> dict:
    """R-multiple (net / initial risk) and best move in R, for a trade dict with net and mfe."""
    risk, per = risk_usd(t), t["entry"] - t["stop"]
    out = {"risk": risk, "r": None, "mfe_r": None}
    if risk > 0 and t.get("net") is not None:
        out["r"] = t["net"] / risk
    if per > 0 and t.get("mfe"):
        out["mfe_r"] = (t["mfe"] - t["entry"]) / per
    return out


def _to_bars(candles: list[dict]) -> list[dict]:
    out = []
    for c in candles:
        t = daytrader._parse_ts(c["date"])
        if t is not None:
            out.append({"t": t, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]})
    return out


def _bars(iid: int, interval: str = "OneMinute") -> list[dict]:
    ec = engine.client()
    if interval == "OneMinute":       # shared with the day trader page's cache
        return _to_bars(daytrader._cached(("hist", iid), 25, lambda: ec.candles(iid, "OneMinute", 300)))
    ttl = {"FifteenMinutes": 120, "OneHour": 600}[interval]
    return _to_bars(daytrader._cached((interval, iid), ttl, lambda: ec.candles(iid, interval, 100)))


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


KIND_WHY = {"crypto": "crypto's 2% round-trip fee", "stock": "the $1-a-side share commission"}


def blocked(iid: int, key: str, now: float, symbol: str | None = None, kind: str | None = None) -> str | None:
    """Why a strategy may not open a trade in this symbol right now, or None: not run on this kind of asset,
    already holding a trade in a symbol of the same group, cooling off after a stop, or paused for the day
    after too many stops. Only strategies that set these limits are affected."""
    cfg = STRATEGIES[key]
    if kind and kind in cfg.get("skip_kinds", ()):
        return f"{cfg['short']} is not run here: {KIND_WHY.get(kind, 'the costs')} turned its stops into big losses."
    g = strategy.group(symbol) if (symbol and cfg.get("one_per_group")) else None
    if g:
        with store._lock, store.connect() as c:
            others = [r["symbol"] for r in c.execute(
                "SELECT symbol FROM autotrades WHERE status='open' AND COALESCE(strategy,'fast')=? AND iid<>?",
                (key, int(iid)))]
        same = [o for o in others if strategy.group(o) == g]
        if same:
            return (f"Already holding a {cfg['short']} trade in {same[0]}, which moves with this one "
                    f"({strategy.GROUPS[g][0]} group): one at a time.")
    cool, most = cfg.get("cooldown_min"), cfg.get("max_stops_day")
    if not cool and not most:
        return None
    from zoneinfo import ZoneInfo
    from . import config
    tz = ZoneInfo(config.TZ)
    day0 = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with store._lock, store.connect() as c:
        stops = [r["closed_ts"] for r in c.execute(
            "SELECT closed_ts FROM autotrades WHERE iid=? AND COALESCE(strategy,'fast')=? AND status='closed' AND reason='stop' "
            "AND closed_ts >= ? ORDER BY closed_ts", (int(iid), key, min(day0, now - (cool or 0) * 60)))]
    if most and sum(t >= day0 for t in stops) >= most:
        return f"Paused for the rest of today after {most} stop-outs."
    if cool and stops and now - stops[-1] < cool * 60:
        until = datetime.fromtimestamp(stops[-1] + cool * 60, tz).strftime("%H:%M")
        return f"Cooling off after a stop-out until {until}."
    return None


def _tick_one(s: dict, r: dict | None, now: float) -> None:
    iid, sym, kind = s["iid"], s["symbol"], s["kind"]
    bid = ask = None
    is_open = False
    if r and r.get("bid") is not None:
        bid, ask = float(r["bid"]), float(r.get("ask") or r["bid"])
        ts = daytrader._parse_ts(r.get("date", "")) or now
        is_open = now - ts <= daytrader.STALE_AFTER
    bars1 = _bars(iid)
    hurdle = hurdle_pct(kind, sym, ask, bid) if bid else strategy.default_hurdle(kind)
    sigs = {"fast": strategy.evaluate(bars1, bid, ask, is_open, hurdle)}
    try:
        bars_h = _bars(iid, "OneHour")
        sigs["swing"] = strategy.evaluate_swing(_bars(iid, "FifteenMinutes"), bars_h, bid, ask, is_open, hurdle)
        sigs["fast2"] = strategy.evaluate_fast2(bars1, bars_h, bid, ask, is_open, hurdle, kind, sym,
                                                datetime.fromtimestamp(now, timezone.utc))
        sigs["fast3"] = strategy.evaluate_fast3(bars1, bars_h, bid, ask, is_open, hurdle, kind, sym,
                                                datetime.fromtimestamp(now, timezone.utc))
        sigs["fast2x"] = strategy.evaluate_fast2x(bars1, bars_h, bid, ask, is_open, hurdle, kind, sym,
                                                  datetime.fromtimestamp(now, timezone.utc))
    except Exception as e:  # noqa: BLE001
        for k in ("swing", "fast2", "fast3", "fast2x"):
            sigs.setdefault(k, {"state": "error", "headline": str(e)[:200]})
    for key in STRATEGIES:                                  # show cooling-off / paused on the page
        why = blocked(iid, key, now, sym, kind)
        if why and not _open_trade(iid, key):
            sigs[key] = {**sigs[key], "state": "paused", "headline": why, "blocked": True}
    with store._lock, store.connect() as c:
        c.execute("UPDATE autotest_symbols SET last_state=?, last_headline=?, swing_state=?, swing_headline=?, "
                  "fast2_state=?, fast2_headline=?, fast3_state=?, fast3_headline=?, fast2x_state=?, fast2x_headline=?, "
                  "last_checked=?, last_bid=? WHERE iid=?",
                  (sigs["fast"]["state"], sigs["fast"]["headline"], sigs["swing"]["state"], sigs["swing"]["headline"],
                   sigs["fast2"]["state"], sigs["fast2"]["headline"], sigs["fast3"]["state"], sigs["fast3"]["headline"],
                   sigs["fast2x"]["state"], sigs["fast2x"]["headline"], _now_iso(now), bid, iid))
    for key, cfg in STRATEGIES.items():
        t = _open_trade(iid, key)
        ex = None
        if t:
            ex = check_exit(t, bars1, bid, is_open, now, cfg["max_hours"], cfg.get("be_at_r"))
            mfe = best_price(t, bars1, bid if is_open else None)
            if mfe is not None:
                t["mfe"] = mfe
                with store._lock, store.connect() as c:
                    c.execute("UPDATE autotrades SET mfe=? WHERE id=?", (mfe, t["id"]))
            if ex:
                _close(t, ex[1], ex[0], now)
            elif bid is not None:
                with store._lock, store.connect() as c:
                    c.execute("UPDATE autotrades SET last_bid=?, checked_ts=? WHERE id=?", (bid, now, t["id"]))
            if not ex:
                continue                                    # one trade per symbol per strategy
        sig = sigs[key]
        if s.get(f"{key}_on", 1) in (0, "0"):
            continue                                        # strategy switched off for this symbol
        if sig.get("blocked") or blocked(iid, key, now, sym, kind):
            continue                                        # cooling off / paused after stop-outs
        if sig.get("state") == "enter" and ask is not None:
            with store._lock, store.connect() as c:
                c.execute("INSERT INTO autotrades(iid, symbol, kind, strategy, opened, opened_ts, amt, entry, bid0, stop, "
                          "target, hurdle, reasons, status, checked_ts, last_bid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
                          (iid, sym, kind, key, _now_iso(now), now, AMT, ask, bid, sig["stop"], sig["target"], hurdle,
                           json.dumps(sig["reasons"]), now, bid))
            store.log("info", f"auto-test {key}: opened {sym} at {ask:.6g} (stop {sig['stop']:.6g}, target {sig['target']:.6g})")


def _stats(rows: list[dict]) -> dict:
    rs = [t["r"] for t in rows if t.get("r") is not None]
    return {"trades": len(rows), "wins": sum(t["net"] > 0 for t in rows), "net": sum(t["net"] for t in rows),
            "fees": sum(t["fees"] for t in rows), "gross": sum(t["gross"] for t in rows),
            "targets": sum(t["reason"] == "target" for t in rows), "stops": sum(t["reason"] == "stop" for t in rows),
            "total_r": sum(rs), "avg_r": sum(rs) / len(rs) if rs else None}


def summary(limit: int = 50) -> dict:
    """Everything the day trader page shows for the auto-test."""
    _ensure()
    with store._lock, store.connect() as c:
        open_ = [dict(r) for r in c.execute("SELECT * FROM autotrades WHERE status='open' ORDER BY opened_ts DESC")]
        closed = [dict(r) for r in c.execute("SELECT * FROM autotrades WHERE status='closed' ORDER BY closed_ts DESC LIMIT ?", (limit,))]
        allc = [dict(r) for r in c.execute("SELECT symbol, kind, amt, entry, stop, mfe, COALESCE(strategy,'fast') AS strategy, "
                                           "net, fees, gross, reason, closed_ts FROM autotrades WHERE status='closed'")]
    for t in open_ + closed + allc:
        t["strategy"] = t.get("strategy") or "fast"
        t.pop("reasons", None)
    for t in closed + allc:
        t.update(r_stats(t))
    for t in open_:
        t.update({k: v for k, v in r_stats(t).items() if k != "r"})
    for t in open_:
        if t["last_bid"]:
            t.update({k: v for k, v in costs(t["kind"], t["symbol"], t["amt"], t["entry"], t["last_bid"]).items() if k != "lines"})
    by: dict[tuple, list] = {}
    for t in allc:
        by.setdefault((t["strategy"], t["symbol"]), []).append(t)
    by_symbol = sorted(({"strategy": k[0], "symbol": k[1], **_stats(v)} for k, v in by.items()),
                       key=lambda b: (b["strategy"], -b["trades"]))
    totals = {key: _stats([t for t in allc if t["strategy"] == key]) for key in STRATEGIES}
    return {"symbols": symbols(), "open": open_, "closed": closed, "by_symbol": by_symbol, "totals": totals,
            "total": _stats(allc), "strategies": STRATEGIES, "amt": AMT}


def digest(since_ts: float) -> list[str]:
    """Plain-text lines for the daily summary email."""
    s = summary(500)
    out = ["Strategy auto-test (theoretical $100 trades, every strategy):"]
    for key, cfg in STRATEGIES.items():
        allt = s["totals"][key]
        today = _stats([t for t in s["closed"] if t["strategy"] == key and (t["closed_ts"] or 0) >= since_ts])
        rate = f"{allt['wins'] / allt['trades'] * 100:.0f}% in profit" if allt["trades"] else "no trades yet"
        rr = f", {allt['total_r']:+.1f}R (avg {allt['avg_r']:+.2f}R)" if allt["avg_r"] is not None else ""
        out.append(f"  {cfg['label']}: today {today['trades']} closed, net ${today['net']:+.2f} | "
                   f"all time {allt['trades']} closed, {rate}, net ${allt['net']:+.2f} after ${allt['fees']:.2f} fees{rr}")
    for t in s["open"]:
        net = f"{t['net']:+.2f}" if t.get("net") is not None else "?"
        out.append(f"  open: {t['symbol']} ({t['strategy']}) since {_local_hm(t['opened_ts'])}, net if closed now ${net}")
    for b in s["by_symbol"]:
        out.append(f"  {b['symbol']} ({b['strategy']}): {b['trades']} trades, {b['wins']} in profit, net ${b['net']:+.2f}")
    sig = [f"{x['symbol']} fast {x.get('last_state') or '-'} / v2 {x.get('fast2_state') or '-'} / v3 {x.get('fast3_state') or '-'} "
           f"/ swing {x.get('swing_state') or '-'}"
           for x in s["symbols"]]
    if sig:
        out.append("  signals now: " + "; ".join(sig))
    return out


def _local_hm(ts: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        from . import config
        return datetime.fromtimestamp(ts, ZoneInfo(config.TZ)).strftime("%d %b %H:%M")
    except Exception:  # noqa: BLE001
        return _now_iso(ts)


# --- charts for the auto-test grid on the day trader page -----------------------------------
PAD = 15 * 60                       # show 15 minutes either side of a trade
IDLE_WINDOW = 12 * 3600             # no trade: the last 12 hours
INTERVALS = [("OneMinute", 60, 5 * 3600), ("FiveMinutes", 300, 36 * 3600),
             ("FifteenMinutes", 900, 7 * 86400), ("OneHour", 3600, 10 ** 9)]


def chart(iid: int, now: float | None = None) -> dict:
    """Candles and trade markers for one tested symbol.
    With a trade open, or one that closed under 15 minutes ago: from 15 minutes before the earliest
    such entry up to now (so a closed trade shows until 15 minutes after its exit). Otherwise: the
    last 12 hours. The candle interval is the finest that covers the window in at most ~300 candles."""
    _ensure()
    now = now or time.time()
    with store._lock, store.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, COALESCE(strategy,'fast') AS strategy, status, opened_ts, entry, stop, target, closed_ts, exit, "
            "reason, last_bid, kind, symbol, amt FROM autotrades WHERE iid=? AND (status='open' OR closed_ts >= ?) "
            "ORDER BY opened_ts", (int(iid), now - PAD))]
        srow = c.execute("SELECT symbol, kind, last_bid FROM autotest_symbols WHERE iid=?", (int(iid),)).fetchone()
    for t in rows:
        bid = t["exit"] if t["status"] == "closed" else t["last_bid"]
        if bid:
            t["net"] = costs(t["kind"], t["symbol"], t["amt"], t["entry"], bid)["net"]
    if rows:
        mode, t0 = "trade", min(t["opened_ts"] for t in rows) - PAD
    else:
        mode, t0 = "idle", now - IDLE_WINDOW
    name, step = next((n, s) for n, s, span in INTERVALS if now - t0 <= span)
    count = min(1000, int((now - t0) // step) + 2)
    ec = engine.client()
    raw = daytrader._cached(("agrid", int(iid), name, count // 5), 55, lambda: ec.candles(int(iid), name, count))
    candles = [{"t": b["t"], "d": step, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
               for b in _to_bars(raw) if b["t"] + step > t0]
    return {"iid": int(iid), "symbol": srow["symbol"] if srow else f"#{iid}", "mode": mode, "t0": t0, "t1": now,
            "interval": name, "step": step, "candles": candles, "trades": rows,
            "last_bid": srow["last_bid"] if srow else None}
