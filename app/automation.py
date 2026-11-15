"""Automation added 15 Nov 2026, aimed at spending effort where something has changed:

- pick(): which symbols a scheduled run analyses. Triggers first (a met watch level, on the watchlist or not; a
  holding down 10% or more, or not reviewed for 7 days, including holdings that are not on the watchlist; a
  watchlist symbol never analysed; a move of 5% or more on the last day), then watchlist symbols not analysed for
  ROTATE_DAYS days. Slots with nothing to do are left unused, which saves API cost on repeated Holds.
- weekly_scan(): Sunday-evening short-term Opportunities scan; up to AUTO_ADD ideas go on the watchlist.
- expire_ideas(): ideas added by the weekly scan leave the watchlist after IDEA_DAYS days unless held.
- weekly_review(): Sunday email with the strategy league table, the automatic review and the week's analyses.

None of this changes the trading limits: trades still go through the same rules, caps and approvals.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import autotest, config, engine, levels, notify, policy, scorecard, store, terms

ROTATE_DAYS = 7          # re-analyse a quiet watchlist symbol at most once a week
HOLDING_REVIEW_DAYS = 7  # review every holding at least weekly
HOLDING_DROP_PCT = 10.0  # ...and as soon as it is this far below its cost
MOVE_PCT = 5.0           # a daily move this big (either way) is a trigger
AUTO_ADD = 3             # ideas the weekly scan may add
WATCH_MAX = 15           # ...only while the watchlist is shorter than this
IDEA_DAYS = 14           # auto-added ideas expire after this many days unless held


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(iso: str | None, now: datetime) -> float:
    if not iso:
        return 1e9
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 86400
    except ValueError:
        return 1e9


def _is_crypto(item) -> bool:
    return item.asset_type == "crypto"


def pick(asset: str, slots: int, ec=None, book=None, now: datetime | None = None) -> list[tuple]:
    """[(WatchItem, reason)] for a scheduled run of `asset` ('stock' or 'crypto'), at most `slots` long."""
    now = now or _now()
    want_crypto = asset == "crypto"
    wl = [i for i in policy.parse_watchlist(store.get_settings()["watchlist"]) if _is_crypto(i) == want_crypto]
    by_sym = {i.symbol: i for i in wl}
    chosen: dict = {}

    def add(item, reason):
        if item and item.symbol not in chosen and len(chosen) < slots:
            chosen[item.symbol] = (item, reason)

    def off_watchlist(sym):
        if ec is None:
            return None                                     # no live lookups (e.g. the dashboard preview)
        try:
            inst = ec.find_instrument(sym)
        except Exception:  # noqa: BLE001
            return None
        item = policy.parse_watchlist(policy.entry_for(sym, "", inst.get("assetClass") or ""))[0]
        return item if _is_crypto(item) == want_crypto else None

    # 1. met watch levels (on or off the watchlist)
    for sym in levels.triggered_tickers():
        add(by_sym.get(sym) or off_watchlist(sym), "watch level met")
    # 2. holdings: well below cost, or not reviewed for a week
    if book is not None:
        for h in book.holdings.values():
            if h.units <= 0:
                continue
            cost = h.cost_usd
            drop = (1 - h.value_usd / cost) * 100 if cost else 0.0
            age = _age_days(store.last_run_for(h.symbol), now)
            if drop >= HOLDING_DROP_PCT or age >= HOLDING_REVIEW_DAYS:
                why = f"holding {drop:.0f}% below cost" if drop >= HOLDING_DROP_PCT else "holding not reviewed for a week"
                add(by_sym.get(h.symbol) or off_watchlist(h.symbol), why)
    # 3. never analysed (new ideas)
    for i in wl:
        if not store.last_run_for(i.symbol):
            add(i, "new on the watchlist")
    # 4. a big move on the last day
    if ec is not None:
        for i in wl:
            if i.symbol in chosen or len(chosen) >= slots:
                continue
            try:
                iid = ec.find_instrument(i.symbol)["instrumentId"]
                bars = ec.candles(iid, "OneDay", 3)
                if len(bars) >= 2 and bars[-2]["close"]:
                    mv = (bars[-1]["close"] / bars[-2]["close"] - 1) * 100
                    if abs(mv) >= MOVE_PCT:
                        add(i, f"moved {mv:+.1f}% on the day")
            except Exception:  # noqa: BLE001
                continue
    # 5. rotation: the least recently analysed, if not looked at for ROTATE_DAYS days
    for i in sorted(wl, key=lambda i: store.last_run_for(i.symbol) or ""):
        if _age_days(store.last_run_for(i.symbol), now) >= ROTATE_DAYS:
            add(i, f"not analysed for {ROTATE_DAYS}+ days")
    return list(chosen.values())


# --- weekly opportunities ------------------------------------------------------------------
def _ideas() -> dict:
    try:
        return json.loads(store.get_settings().get("auto_ideas") or "{}")
    except ValueError:
        return {}


def _save_ideas(d: dict) -> None:
    store.set_setting("auto_ideas", json.dumps(d, sort_keys=True))


def _entry(o: dict) -> str:
    return f"{o['symbol']}={o['symbol']}-USD@crypto" if o.get("asset_type") == "crypto" else o["symbol"]


def weekly_scan() -> dict:
    """Sunday 20:00 UK: a short-term Opportunities scan; up to AUTO_ADD found ideas go on the watchlist."""
    from . import opportunities
    why = opportunities.can_scan()
    if why:
        store.log("warn", f"weekly opportunity scan skipped: {why}")
        return {"skipped": why}
    res = opportunities.scan("short")
    if res.get("error"):
        return res
    cur = store.get_settings()["watchlist"]
    have = {i.symbol for i in policy.parse_watchlist(cur)}
    ideas, added = _ideas(), []
    for o in res["items"]:
        if len(added) >= AUTO_ADD or len(have) >= WATCH_MAX:
            break
        if o["symbol"] in have or not (o.get("etoro") or {}).get("found"):
            continue
        cur = f"{cur},{_entry(o)}" if cur.strip() else _entry(o)
        have.add(o["symbol"])
        added.append(o["symbol"])
        ideas[o["symbol"]] = {"since": _now().isoformat(timespec="seconds"), "scan": res["id"]}
        terms.set_term(o["symbol"], terms.SHORT, f"weekly opportunity scan #{res['id']}")
    if added:
        store.set_setting("watchlist", cur)
        _save_ideas(ideas)
        store.log("info", f"weekly opportunity scan #{res['id']}: added {', '.join(added)} to the watchlist "
                          f"as short-term ideas for {IDEA_DAYS} days")
    else:
        store.log("info", f"weekly opportunity scan #{res['id']}: nothing new added (watchlist full or already there)")
    return {"id": res["id"], "added": added}


def expire_ideas(book=None, now: datetime | None = None) -> list[str]:
    """Daily: take auto-added ideas off the watchlist after IDEA_DAYS days, unless held. Hand-added
    symbols are never touched."""
    now = now or _now()
    ideas = _ideas()
    if not ideas:
        return []
    if book is None:
        try:
            book = engine.client().book()
        except Exception as e:  # noqa: BLE001
            store.log("warn", f"idea expiry skipped: portfolio unavailable ({e})")
            return []
    held = {h.symbol for h in book.holdings.values() if h.units > 0 or h.pending_usd > 0}
    gone = []
    for sym, meta in list(ideas.items()):
        if _age_days(meta.get("since"), now) < IDEA_DAYS:
            continue
        ideas.pop(sym)
        if sym in held:
            store.log("info", f"{sym}: {IDEA_DAYS}-day idea period over, kept on the watchlist because it is held")
            continue
        raw = [e.strip() for e in store.get_settings()["watchlist"].replace("\n", ",").split(",") if e.strip()]
        store.set_setting("watchlist", ",".join(e for e in raw if policy.parse_watchlist(e)[0].symbol != sym))
        terms.forget(sym)
        gone.append(sym)
        store.log("info", f"watchlist: removed {sym} ({IDEA_DAYS}-day opportunity idea expired, not held)")
    _save_ideas(ideas)
    return gone


# --- weekly review email -----------------------------------------------------------------
def weekly_lines(now: datetime | None = None, ec=None) -> list[str]:
    now = now or _now()
    since = now - timedelta(days=7)
    out = [f"Week to {now.astimezone(ZoneInfo(config.TZ)).strftime('%a %d %b %Y')}", ""]
    with store._lock, store.connect() as c:
        snaps = [dict(r) for r in c.execute("SELECT day, total FROM snapshots ORDER BY day")] \
            if c.execute("SELECT name FROM sqlite_master WHERE name='snapshots'").fetchone() else []
    if snaps:
        last = snaps[-1]
        base = next((s for s in reversed(snaps) if s["day"] <= since.date().isoformat()), snaps[0])
        out.append(f"Portfolio: ${last['total']:.2f} on {last['day']}, {last['total'] - base['total']:+.2f} since "
                   f"{base['day']} (includes any cash paid in or out)")
    runs = [r for r in store.runs(500) if (r.get("started") or "") >= since.isoformat(timespec="seconds")]
    counts: dict = {}
    for r in runs:
        counts[r.get("rating") or "failed"] = counts.get(r.get("rating") or "failed", 0) + 1
    out.append(f"Analyses this week: {len(runs)}" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ")"
                                                      if runs else ""))
    props = [p for p in store.proposals(None, 200) if (p.get("created") or "") >= since.isoformat(timespec="seconds")]
    out.append(f"Trades proposed this week: {len(props)}"
               + (" (" + ", ".join(f"{p['action']} {p['ticker']} [{p['status']}]" for p in props) + ")" if props else ""))
    ideas = _ideas()
    if ideas:
        out.append("Opportunity ideas on trial: " + ", ".join(
            f"{s} (since {m.get('since', '')[:10]})" for s, m in sorted(ideas.items())))
    out += ["", "Strategy league table (theoretical $100 trades; this week | all time):"]
    s = autotest.summary(5000)
    wk = since.timestamp()
    rows = []
    for key, cfg in autotest.STRATEGIES.items():
        a = s["totals"][key]
        w = autotest._stats([t for t in s["closed"] if t["strategy"] == key and (t["closed_ts"] or 0) >= wk])
        rows.append((a["net"], f"  {cfg['short']}: {w['trades']} trades, net ${w['net']:+.2f} | {a['trades']} trades, "
                               f"{(a['wins'] / a['trades'] * 100) if a['trades'] else 0:.0f}% in profit, net ${a['net']:+.2f}"
                               + (f", average {a['avg_r']:+.2f}R" if a["avg_r"] is not None else "")))
    out += [r[1] for r in sorted(rows, key=lambda r: -r[0])]
    out += autotest.review_lines(s.get("review") or {})
    for t in s["open"]:
        if t["strategy"] in autotest.DAILY:
            out.append(f"  open daily test: {t['symbol']} ({autotest.STRATEGIES[t['strategy']]['short']}) since "
                       f"{autotest._local_hm(t['opened_ts'])}, net now ${t.get('net') or 0:+.2f}")
    try:
        out += ["", "Scorecard: " + scorecard.headline(scorecard.evaluate(ec or engine.client(), store.runs(1000)))]
    except Exception as e:  # noqa: BLE001
        out += ["", f"Scorecard unavailable: {e}"]
    out += ["", f"Day trader page: {config.PUBLIC_BASE_URL}/daytrader", f"Dashboard: {config.PUBLIC_BASE_URL}/"]
    return out


def weekly_review() -> None:
    try:
        lines = weekly_lines()
    except Exception as e:  # noqa: BLE001
        store.log("error", f"weekly review failed: {e}")
        return
    notify.email("Weekly review", "\n".join(lines))
    store.log("info", "weekly review emailed")


# --- daily housekeeping ------------------------------------------------------------------
def daily_review() -> None:
    """21:20 UK: strategy review (before the 21:30 summary), then idea expiry."""
    try:
        autotest.review()
    except Exception as e:  # noqa: BLE001
        store.log("error", f"auto-test review failed: {e}")
    try:
        expire_ideas()
    except Exception as e:  # noqa: BLE001
        store.log("error", f"idea expiry failed: {e}")
