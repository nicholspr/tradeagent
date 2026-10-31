"""Orchestration: analyse -> propose -> (signal | approve | auto-execute) -> notify."""
import hashlib
import hmac
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import analyst, config, notify, policy, store
from .etoro import EtoroClient, EtoroError

_run_lock = threading.Lock()          # one analysis at a time (LLM cost + rate limits)
_exec_lock = threading.Lock()         # one order at a time
_manual_lock = threading.Lock()       # makes "signal -> approved" a single step, so a double click can't queue two orders
SIGNAL_MAX_AGE = timedelta(hours=24)  # an older signal is stale: analyse again instead


def client() -> EtoroClient:
    return EtoroClient()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start_iso() -> str:
    tz = ZoneInfo(config.TZ)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _tracking_start_iso() -> str:
    """Start of the current daily-limit window: today's midnight, or a later manual reset."""
    return max(_day_start_iso(), store.get_settings().get("daily_reset_at") or "")


def daily_usage() -> dict:
    since = _tracking_start_iso()
    done = store.executed_since(since)
    return {"spent": sum(p["amount_usd"] or 0 for p in done if p["action"] == "buy"),
            "trades": len(done), "analyses": store.runs_since(since)}


def reset_daily_tracking() -> str:
    """Restart today's spend, trade and analysis counters from now. Per-instrument cooldowns are untouched."""
    now = _utc_now().isoformat(timespec="seconds")
    store.set_setting("daily_reset_at", now)
    store.log("warn", "daily limits reset manually: spend, trade and analysis counters restart from now")
    return now

def token_for(pid: int, created: str) -> str:
    return hmac.new(config.APP_SECRET.encode(), f"{pid}:{created}".encode(), hashlib.sha256).hexdigest()[:32]


def link_for(p: dict) -> str:
    return f"{config.PUBLIC_BASE_URL}/p/{p['id']}/{token_for(p['id'], p['created'])}"


def _portfolio_context(book) -> dict:
    return {"cash": book.cash_usd, "currency": "USD",
            "positions": [{"ticker": h.symbol, "quantity": round(h.units, 6),
                           "average_price": (round(sum(p["openRate"] * p["units"] for p in h.positions) / h.units, 4)
                                             if h.units else None)}
                          for h in book.holdings.values() if h.units > 0]}


def _describe(p: dict) -> str:
    if p["action"] == "buy":
        return f"BUY {p['ticker']} ${p['amount_usd']:.2f}"
    if p["action"] == "close":
        return f"SELL ALL {p['ticker']} (~${p['amount_usd']:.2f})"
    return f"SELL HALF {p['ticker']} (~${p['amount_usd']:.2f})"


def audit(pid: int, event: str, extra: str = "") -> None:
    """One audit line per proposal event: id, what, rating, mode, status and why."""
    p = store.get_proposal(pid)
    if not p:
        return
    what = _describe(p) if p["action"] != "none" else f"{p['ticker']} no trade"
    line = (f"proposal #{pid} {event}: {what} | rating {p['rating']} | mode {p['mode']} | "
            f"status {p['status']}")
    why = extra or p.get("error") or p.get("reason")
    if why:
        line += f" | {why}"
    store.log("audit", line)


def _first_lines(text: str, n: int = 600) -> str:
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


# ---------------------------------------------------------------- analysis
def analyse(item: policy.WatchItem, *, manual: bool = False) -> dict:
    """Run the agents for one watchlist item and act on the result per the current mode."""
    if not _run_lock.acquire(blocking=False):
        store.log("warn", f"analysis of {item.symbol} skipped: another analysis is running")
        return {"skipped": True}
    try:
        return _analyse(item, manual=manual)
    finally:
        _run_lock.release()


def _analyse(item: policy.WatchItem, *, manual: bool) -> dict:
    s = store.get_settings()
    run_id = store.start_run(item.symbol)
    ec = client()
    try:
        inst = ec.find_instrument(item.symbol)
        book = ec.book()
        store.log("info", f"analysing {item.symbol} ({item.yf}, {item.asset_type})")
        rating, decision = analyst.analyze(item.yf, item.asset_type, _portfolio_context(book))
        store.finish_run(run_id, rating, decision)
    except Exception as e:  # noqa: BLE001
        store.finish_run(run_id, None, None, error=str(e)[:1000])
        store.log("error", f"analysis of {item.symbol} failed: {e}")
        notify.email(f"Analysis failed: {item.symbol}", f"{e}")
        return {"error": str(e)}

    h = book.holdings.get(inst["instrumentId"])
    hv, hpos, hpend = (h.value_usd, h.positions, h.pending_usd) if h else (0.0, [], 0.0)
    prop = policy.propose(rating, holding_value=hv, holding_positions=hpos, pending_usd=hpend,
                          cash=book.cash_usd, portfolio_total=book.total_usd, s=s)
    return _act(item.symbol, inst, book, s, rating, decision, run_id, prop, manual)


def _act(symbol, inst, book, s, rating, decision, run_id, prop, manual=False) -> dict:
    """Route a proposal by mode: signal, approval request, or automatic execution."""
    base = {"run_id": run_id, "ticker": symbol, "instrument_id": inst["instrumentId"],
            "action": prop.action, "amount_usd": prop.amount_usd, "units": prop.units,
            "position_ids": prop.position_ids, "rating": rating, "mode": s["mode"]}

    if prop.action == "none":
        store.log("info", f"{symbol}: {prop.reason}")
        if manual or rating.lower() not in ("hold",):
            notify.email(f"{symbol}: {rating}", f"{prop.reason}\n\n{decision}\n\n{config.PUBLIC_BASE_URL}/run/{run_id}")
        return {"rating": rating, "action": "none"}

    # Size against limits now (tradable assumed; re-checked at execution time).
    sized, blocks = _limits(prop, inst, book, s, tradable=True)
    if sized is None:
        pid = store.add_proposal({**base, "status": "blocked", "reason": "; ".join(blocks)})
        audit(pid, "created and blocked by limits")
        msg = f"{symbol}: agent says {rating}, blocked by limits: {'; '.join(blocks)}"
        store.log("info", msg)
        notify.email(f"{symbol}: {rating} (blocked)", f"{msg}\n\n{decision}")
        return {"rating": rating, "blocked": blocks, "proposal": pid}

    base.update(amount_usd=sized.amount_usd, units=sized.units, position_ids=sized.position_ids)
    if s["mode"] == "signals":
        pid = store.add_proposal({**base, "status": "signal", "reason": sized.reason})
        audit(pid, "created as signal (signals mode, no trade)")
        p = store.get_proposal(pid)
        notify.both(f"Signal: {_describe(p)}",
                    f"TradeAgent signal: {_describe(p)} (rating {rating}). Signals mode: place it yourself if you agree.",
                    f"{_describe(p)} — rating {rating}\n\n{decision}")
        return {"rating": rating, "signal": pid}

    if policy.needs_approval(sized, s):
        pid = store.add_proposal({**base, "status": "pending", "reason": sized.reason})
        audit(pid, f"created, awaiting approval (expires in {s['approval_ttl_hours']}h)")
        p = store.get_proposal(pid)
        url = link_for(p)
        notify.sms(f"TradeAgent: {_describe(p)} (rating {rating}). Review & approve within "
                   f"{s['approval_ttl_hours']}h: {url}")
        notify.email(f"Approve? {_describe(p)}", f"{_describe(p)} — rating {rating}\n\nApprove or reject: {url}\n\n{decision}")
        return {"rating": rating, "pending": pid}

    pid = store.add_proposal({**base, "status": "approved", "reason": sized.reason})
    store.update_proposal(pid, decided=_utc_now().isoformat(timespec="seconds"))
    audit(pid, "created and auto-approved (automatic mode)")
    result = execute(pid)
    return {"rating": rating, "auto": pid, "result": result}


def _limits(prop, inst, book, s, *, tradable: bool):
    h = book.holdings.get(inst["instrumentId"])
    since = _tracking_start_iso()
    spent = sum(p["amount_usd"] or 0 for p in store.executed_since(since) if p["action"] == "buy")
    trades = len(store.executed_since(since))
    return policy.check_limits(
        prop, instrument_id=inst["instrumentId"], cash=book.cash_usd, portfolio_total=book.total_usd,
        holding_value=h.value_usd if h else 0.0, pending_usd=h.pending_usd if h else 0.0,
        spent_today=spent, trades_today=trades, last_trade_iso=store.last_executed_for(inst["instrumentId"]),
        tradable=tradable, s=s)


def create_test(symbol: str, action: str) -> dict:
    """Free, AI-free test proposal that follows the current mode. Test mode (DRY_RUN) only."""
    if not config.DRY_RUN:
        return {"error": "test trades are only allowed while DRY_RUN=true"}
    s = store.get_settings()
    ec = client()
    inst = ec.find_instrument(symbol)
    book = ec.book()
    h = book.holdings.get(inst["instrumentId"])
    rating = "Buy" if action == "buy" else "Underweight"
    prop = policy.propose(rating, holding_value=h.value_usd if h else 0.0,
                          holding_positions=h.positions if h else [], pending_usd=h.pending_usd if h else 0.0,
                          cash=book.cash_usd, portfolio_total=book.total_usd, s=s)
    store.log("info", f"TEST trade created: {symbol} {rating}")
    return _act(symbol, inst, book, s, f"{rating} (TEST)", "Test trade created from the dashboard; no AI analysis was run.",
                None, prop, manual=True)


# ---------------------------------------------------------------- execution
def decide(pid: int, approve: bool, actor: str = "") -> dict:
    p = store.get_proposal(pid)
    if not p or p["status"] != "pending":
        return {"error": "not pending", "status": p and p["status"]}
    now = _utc_now().isoformat(timespec="seconds")
    if not approve:
        store.update_proposal(pid, status="rejected", decided=now)
        audit(pid, "rejected", f"by {actor or 'user'}")
        return {"status": "rejected"}
    store.update_proposal(pid, status="approved", decided=now)
    audit(pid, "approved", f"by {actor or 'user'}")
    return execute(pid)


def manual_execute(pid: int, actor: str = "") -> dict:
    """Place a trade the agent suggested, on the user's explicit say-so from the run page.

    Only a pending approval or a signals-mode signal can be started. Blocked trades cannot be forced through, and
    execute() re-checks every limit (kill switch, caps, cooldown, market open) before anything is sent.
    """
    who = actor or "user"
    with _manual_lock:
        p = store.get_proposal(pid)
        if not p:
            return {"error": "not found"}
        if p["status"] == "pending":
            return decide(pid, approve=True, actor=f"{who} (manual execute)")
        if p["status"] != "signal":
            return {"error": "not executable", "status": p["status"]}
        if p["action"] == "none":
            return {"error": "nothing to execute", "status": p["status"]}
        if _utc_now() - datetime.fromisoformat(p["created"]) > SIGNAL_MAX_AGE:
            audit(pid, "manual execute refused", "signal is older than 24h; run a new analysis")
            return {"error": "stale", "status": p["status"]}
        store.update_proposal(pid, status="approved", decided=_utc_now().isoformat(timespec="seconds"))
        audit(pid, "manually approved from signal", f"by {who}")
    return execute(pid)


def cancel(pid: int, actor: str = "") -> dict:
    """Cancel an approved trade that is still queued (e.g. waiting for the market to open)."""
    with _exec_lock:   # same lock as execute(), so a trade can't be placed and cancelled at once
        p = store.get_proposal(pid)
        if not p or p["status"] != "approved":
            return {"error": "not queued", "status": p and p["status"]}
        waited = p.get("error") == "waiting for market"
        store.update_proposal(pid, status="cancelled", decided=_utc_now().isoformat(timespec="seconds"),
                              error="cancelled by user" + (" while waiting for market" if waited else ""))
        audit(pid, "cancelled", f"by {actor or 'user'}" + (" while waiting for market to open" if waited else " before placement"))
        return {"status": "cancelled"}


def execute(pid: int) -> dict:
    """Place an approved proposal if limits still pass and the market is open; else leave queued."""
    with _exec_lock:
        p = store.get_proposal(pid)
        if not p or p["status"] != "approved":
            return {"error": "not approved"}
        s = store.get_settings()
        ec = client()
        try:
            inst = ec.find_instrument(p["ticker"])
            book = ec.book()
        except EtoroError as e:
            store.update_proposal(pid, error=str(e)[:500])
            store.log("error", f"proposal {pid}: eToro unavailable: {e}")
            return {"queued": True, "error": str(e)}
        if not inst["tradable"]:
            if not p.get("error") == "waiting for market":
                store.update_proposal(pid, error="waiting for market")
                audit(pid, "queued", f"waiting for {p['ticker']} market to open")
            return {"queued": True, "reason": "market closed"}

        h = book.holdings.get(inst["instrumentId"])
        prop = policy.Proposal(p["action"], p["amount_usd"] or 0, p["units"], p["position_ids"], p["reason"] or "")
        if p["action"] in ("close", "partial_close"):
            live = {x["positionID"]: x for x in (h.positions if h else [])}
            prop.position_ids = [i for i in p["position_ids"] if i in live]
            if not prop.position_ids:
                return _fail(pid, "positions no longer open")
        sized, blocks = _limits(prop, inst, book, s, tradable=True)
        if sized is None:
            store.update_proposal(pid, status="blocked", error="; ".join(blocks))
            audit(pid, "blocked at execution")
            notify.both("Trade blocked", f"TradeAgent: {_describe(p)} blocked at execution: {'; '.join(blocks)}",
                        f"{_describe(p)} blocked at execution:\n" + "\n".join(blocks))
            return {"blocked": blocks}

        if config.DRY_RUN:
            refs = ["dry-run"]
        else:
            try:
                refs = []
                if sized.action == "buy":
                    r = ec.open_buy(inst["symbol"], inst["instrumentId"], sized.amount_usd)
                    refs.append(str(r.get("orderId") or r.get("token") or r))
                else:
                    for pos_id in sized.position_ids:
                        units = sized.units if sized.action == "partial_close" else None
                        r = ec.close_position(pos_id, inst["instrumentId"], units)
                        o = r.get("orderForClose") or r
                        refs.append(str(o.get("orderID") or o.get("token") or r))
            except EtoroError as e:
                return _fail(pid, str(e))

        now = _utc_now().isoformat(timespec="seconds")
        store.update_proposal(pid, status="executed", executed=now, amount_usd=sized.amount_usd,
                              order_ref=",".join(refs), error=None)
        audit(pid, "executed" + (" (TEST, nothing sent)" if config.DRY_RUN else ""), f"orders {','.join(refs)}")
        desc = _describe({**p, "amount_usd": sized.amount_usd})
        env = "DEMO" if ec.demo else "REAL"
        if config.DRY_RUN:
            store.log("info", f"TEST ONLY proposal {pid}: {desc} [{env}] - dry run, nothing sent to eToro")
            notify.both(f"TEST (not sent): {desc}",
                        f"TradeAgent TEST MODE: would have placed {desc} on your {env} account. Nothing was sent to eToro.",
                        f"Test mode (DRY_RUN=true): {desc} passed every check and would have been placed on your "
                        f"{env} eToro account.\nNothing was sent to eToro.\nRating: {p['rating']}")
        else:
            store.log("info", f"executed proposal {pid}: {desc} [{env}] orders {refs}")
            notify.both(f"Executed: {desc}", f"TradeAgent [{env}] placed: {desc}. Order {','.join(refs)}",
                        f"Placed {desc} on your {env} eToro account.\nOrder refs: {refs}\nRating: {p['rating']}")
        return {"executed": True, "orders": refs}


def _fail(pid: int, err: str) -> dict:
    store.update_proposal(pid, status="failed", error=err[:500])
    audit(pid, "failed", err[:300])
    notify.both("Trade failed", f"TradeAgent: order failed: {err[:200]}", f"Proposal {pid} failed:\n{err}")
    return {"failed": err}


# ---------------------------------------------------------------- scheduled jobs
def scheduled_analysis(asset: str = "stock") -> None:
    s = store.get_settings()
    items = [i for i in policy.parse_watchlist(s["watchlist"]) if (i.asset_type == "crypto") == (asset == "crypto")]
    limit = int(float(s["analyses_per_day"] or 0))
    done = store.runs_since(_tracking_start_iso())
    if not items:
        store.log("info", f"scheduled {asset} run: no {asset} entries in the watchlist")
        return
    if done >= limit:
        store.log("warn", f"scheduled {asset} run skipped: daily analysis limit reached "
                          f"({done} of {limit} used today, manual runs included)")
        return
    items.sort(key=lambda i: store.last_run_for(i.symbol) or "")   # least recently analysed first
    chosen = items[:limit - done]
    store.log("info", f"scheduled {asset} run started: {', '.join(i.symbol for i in chosen)} "
                      f"({done} of {limit} analyses used today)")
    for item in chosen:
        analyse(item)


def process_queue() -> None:
    """Expire stale approvals and execute approved proposals whose market has opened."""
    s = store.get_settings()
    ttl = timedelta(hours=float(s["approval_ttl_hours"] or 6))
    now = _utc_now()
    for p in store.proposals("pending", 100):
        if now - datetime.fromisoformat(p["created"]) > ttl:
            store.update_proposal(p["id"], status="expired", decided=now.isoformat(timespec="seconds"))
            audit(p["id"], "expired", f"not approved within {s['approval_ttl_hours']}h")
    for p in store.proposals("approved", 100):
        if now - datetime.fromisoformat(p["decided"] or p["created"]) > timedelta(hours=24):
            store.update_proposal(p["id"], status="expired")
            audit(p["id"], "expired", "approved but market did not open within 24h")
            continue
        execute(p["id"])


def daily_summary() -> None:
    try:
        book = client().book()
        lines = [f"Portfolio ${book.total_usd:,.2f}, cash ${book.cash_usd:,.2f}"]
        for h in sorted(book.holdings.values(), key=lambda h: -h.value_usd):
            lines.append(f"  {h.symbol}: ${h.value_usd:,.2f}" + (f" (+${h.pending_usd:.2f} pending)" if h.pending_usd else ""))
    except Exception as e:  # noqa: BLE001
        lines = [f"Portfolio unavailable: {e}"]
    since = _day_start_iso()
    todays = [p for p in store.proposals(None, 100) if p["created"] >= since]
    s = store.get_settings()
    lines += ["", f"Mode: {s['mode']}  Kill switch: {s['kill_switch']}", f"Today's calls: {len(todays)}"]
    lines += [f"  #{p['id']} {p['ticker']} {p['rating']} {p['action']} ${p['amount_usd'] or 0:.2f} -> {p['status']}"
              for p in todays]
    body = "\n".join(lines) + f"\n\nDashboard: {config.PUBLIC_BASE_URL}/"
    notify.email("Daily summary", body)
