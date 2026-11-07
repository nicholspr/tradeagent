"""Orchestration: analyse -> propose -> (signal | approve | auto-execute) -> notify."""
import hashlib
import re
from html import escape
import hmac
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import analyst, config, levels, notify, performance, policy, store, terms
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


def _research_ticker(etoro_symbol: str) -> str:
    """The ticker the agents research for an eToro symbol (watchlist 'SYM=yf' mapping), so a held position is
    recognised as the analysed instrument: eToro BTC is researched as BTC-USD, CVX.US as CVX."""
    for item in policy.parse_watchlist(store.get_settings()["watchlist"]):
        if item.symbol == etoro_symbol.upper():
            return item.yf
    return etoro_symbol


def _portfolio_context(book) -> dict:
    return {"cash": book.cash_usd, "currency": "USD",
            "positions": [{"ticker": _research_ticker(h.symbol), "quantity": round(h.units, 6),
                           "average_price": (round(sum(p["openRate"] * p["units"] for p in h.positions) / h.units, 4)
                                             if h.units else None)}
                          for h in book.holdings.values() if h.units > 0]}


def _describe(p: dict) -> str:
    if p["action"] == "buy":
        lv = []
        if p.get("stop_loss"):
            lv.append(f"stop {p['stop_loss']:g}")
        if p.get("take_profit"):
            lv.append(f"take-profit {p['take_profit']:g}")
        return f"BUY {p['ticker']} ${p['amount_usd']:.2f}" + (f" ({', '.join(lv)})" if lv else "")
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


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _check_asset_match(item: policy.WatchItem, inst: dict) -> None:
    """Refuse when the eToro symbol is a different kind of asset from the one being researched.

    Example: 'CVX' on eToro is Convex Finance (crypto), while CVX on Yahoo is Chevron (CVX.US on eToro).
    Researching one and buying the other must never happen.
    """
    cls = str(inst.get("assetClass") or "").lower()
    if not cls:
        return
    if (item.asset_type == "crypto") != (cls == "crypto"):
        raise EtoroError(f"eToro symbol {item.symbol} is {inst.get('name') or '?'} ({inst.get('assetClass')}), "
                         f"but the watchlist entry is a {item.asset_type}. Fix the symbol in the watchlist "
                         f"(e.g. CVX.US=CVX for Chevron) before analysing.")


def _analyse(item: policy.WatchItem, *, manual: bool) -> dict:
    s = store.get_settings()
    run_id = store.start_run(item.symbol)
    ec = client()
    try:
        inst = ec.find_instrument(item.symbol)
        _check_asset_match(item, inst)
        store.set_run_meta(run_id, _float(inst.get("rate")), f"{config.model_for('quick')}/{config.model_for('deep')}")
        book = ec.book()
        store.log("info", f"analysing {item.symbol} ({item.yf}, {item.asset_type}) with quick={config.model_for('quick')} deep={config.model_for('deep')}")
        rules = policy.account_rules(s)
        short = terms.agent_note(item.symbol)
        if short:
            rules += "\n\n" + short
        note = levels.trigger_note(item.symbol)
        if note:
            rules += "\n\n" + note
            store.log("info", f"{item.symbol}: analysing because an earlier watch level was met")
        rating, decision = analyst.analyze(item.yf, item.asset_type, _portfolio_context(book), rules=rules)
        store.finish_run(run_id, rating, decision)
    except Exception as e:  # noqa: BLE001
        store.finish_run(run_id, None, None, error=str(e)[:1000])
        store.log("error", f"analysis of {item.symbol} failed: {e}")
        notify.email(f"Analysis failed: {item.symbol}", f"{e}")
        return {"error": str(e)}

    if str(rating).lower() not in ("buy", "overweight"):
        try:
            levels.record(item.symbol, run_id, decision, _float(inst.get("rate")))
        except Exception as e:  # noqa: BLE001
            store.log("warn", f"watch levels for {item.symbol} not saved: {e}")
    else:
        levels.record_none(item.symbol)
    h = book.holdings.get(inst["instrumentId"])
    hv, hpos, hpend = (h.value_usd, h.positions, h.pending_usd) if h else (0.0, [], 0.0)
    prop = policy.propose(rating, holding_value=hv, holding_positions=hpos, pending_usd=hpend,
                          cash=book.cash_usd, portfolio_total=book.total_usd, s=s)
    stops = None
    if prop.action == "buy" and terms.term(item.symbol) == terms.SHORT:
        stops = terms.stops_from(item.yf, decision, _float(inst.get("rate")))
        store.log("info", f"{item.symbol} short-term levels: stop-loss {stops[0]}, take-profit {stops[1]}"
                          + (f" ({stops[2]})" if stops[2] else ""))
    return _act(item.symbol, inst, book, s, rating, decision, run_id, prop, manual, stops=stops)


def _act(symbol, inst, book, s, rating, decision, run_id, prop, manual=False, stops=None) -> dict:
    """Route a proposal by mode: signal, approval request, or automatic execution."""
    base = {"run_id": run_id, "ticker": symbol, "instrument_id": inst["instrumentId"],
            "action": prop.action, "amount_usd": prop.amount_usd, "units": prop.units,
            "position_ids": prop.position_ids, "rating": rating, "mode": s["mode"]}
    if stops:
        base.update(stop_loss=stops[0], take_profit=stops[1], term=terms.SHORT)

    if prop.action == "none":
        store.log("info", f"{symbol}: {prop.reason}")
        # Every analysis is emailed, scheduled Holds included, so it is easy to see that runs are happening.
        notify.email(f"{symbol}: {rating}{'' if manual else ' (scheduled)'}",
                     f"{prop.reason}\n\n{decision}\n\n{config.PUBLIC_BASE_URL}/run/{run_id}")
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


def _fixes(blocks: list[str], s: dict, *, spent: float, last_trade_iso: str | None, now: datetime) -> list[str]:
    """Plain-English ways to clear each limit that blocked a trade."""
    out = []
    for b in blocks:
        if b.startswith("kill switch"):
            out.append("Turn the kill switch off (Settings, Control).")
        elif b.startswith("daily trade limit"):
            out.append(f"Raise 'Max trades per day' (now {s['max_trades_per_day']}), press 'Reset daily limits' "
                       f"in Settings, or wait until tomorrow.")
        elif b.startswith("cooldown") and last_trade_iso:
            days = float(s.get("cooldown_days") or 0)
            left = datetime.fromisoformat(last_trade_iso) + timedelta(days=days) - now
            hrs = max(left.total_seconds() / 3600, 0)
            when = f"{hrs / 24:.1f} days" if hrs >= 48 else f"{hrs:.1f} hours"
            out.append(f"Wait about {when}, or lower 'Days between trades' (now {s['cooldown_days']}) "
                       f"to below {max(days - left.total_seconds() / 86400, 0):.2f}. 0 turns the cooldown off for every holding.")
        elif b.startswith("buy below"):
            if "cash" in b:
                out.append(f"Add cash to the account, or lower 'Cash to keep' (now ${s['cash_buffer_usd']}).")
            if "daily spend" in b:
                out.append(f"Raise 'Max buys per day' (now ${s['max_daily_usd']}, ${spent:.2f} used) or press "
                           f"'Reset daily limits' in Settings.")
            if "position cap" in b:
                out.append(f"Raise 'Max in one holding' (now {s['max_position_pct']}%), or add cash so the holding "
                           f"is a smaller share of the portfolio.")
        else:
            out.append(b)
    return out


def preflight(item: policy.WatchItem) -> dict:
    """Would a Buy or a Sell/Underweight for this share be allowed right now? No AI call, nothing is placed."""
    s = store.get_settings()
    ec = client()
    inst = ec.find_instrument(item.symbol)
    book = ec.book()
    h = book.holdings.get(inst["instrumentId"])
    since = _tracking_start_iso()
    spent = sum(p["amount_usd"] or 0 for p in store.executed_since(since) if p["action"] == "buy")
    last = store.last_executed_for(inst["instrumentId"])
    now = _utc_now()
    out = {"symbol": item.symbol, "mode": s["mode"], "market_open": bool(inst["tradable"]), "sides": []}
    for label, rating in (("Buy", "Buy"), ("Sell half", "Underweight")):
        prop = policy.propose(rating, holding_value=h.value_usd if h else 0.0,
                              holding_positions=h.positions if h else [], pending_usd=h.pending_usd if h else 0.0,
                              cash=book.cash_usd, portfolio_total=book.total_usd, s=s)
        if prop.action == "none":
            out["sides"].append({"label": label, "ok": False, "blocks": [prop.reason], "fixes": [
                "Switch 'Allow sells' on in Settings." if "switched off" in prop.reason
                else "Nothing to do: you don't hold this share."]})
            continue
        sized, blocks = _limits(prop, inst, book, s, tradable=True)
        side = {"label": label, "ok": sized is not None, "amount": sized.amount_usd if sized else None, "blocks": blocks}
        side["fixes"] = _fixes(blocks, s, spent=spent, last_trade_iso=last, now=now)
        out["sides"].append(side)
    return out


def _limits(prop, inst, book, s, *, tradable: bool, override: bool = False):
    h = book.holdings.get(inst["instrumentId"])
    since = _tracking_start_iso()
    spent = sum(p["amount_usd"] or 0 for p in store.executed_since(since) if p["action"] == "buy")
    trades = len(store.executed_since(since))
    return policy.check_limits(
        prop, instrument_id=inst["instrumentId"], cash=book.cash_usd, portfolio_total=book.total_usd,
        holding_value=h.value_usd if h else 0.0, pending_usd=h.pending_usd if h else 0.0,
        spent_today=spent, trades_today=trades, last_trade_iso=store.last_executed_for(inst["instrumentId"]),
        tradable=tradable, s=s, override=override)


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


def override_execute(pid: int, actor: str = "") -> dict:
    """Place a trade that your limits blocked, on the user's explicit say-so, waiving the soft limits for this trade only.

    The kill switch, a closed market, the per-trade maximum, the $10 minimum and the cash actually available still apply,
    and execute() re-checks them. The waiver is stored on the proposal, so it covers only this one trade.
    """
    who = actor or "user"
    with _manual_lock:
        p = store.get_proposal(pid)
        if not p:
            return {"error": "not found"}
        if p["status"] != "blocked" or p["action"] == "none":
            return {"error": "not overridable", "status": p["status"]}
        if _utc_now() - datetime.fromisoformat(p["created"]) > SIGNAL_MAX_AGE:
            audit(pid, "override refused", "blocked trade is older than 24h; run a new analysis")
            return {"error": "stale", "status": p["status"]}
        if store.get_settings().get("kill_switch") == "on":
            audit(pid, "override refused", "kill switch is on")
            return {"error": "kill switch", "status": p["status"]}
        waived = p.get("error") or p.get("reason") or "limits"
        store.update_proposal(pid, status="approved", decided=_utc_now().isoformat(timespec="seconds"),
                              override=1, error=None)
        audit(pid, "LIMITS OVERRIDDEN by user", f"by {who}; waived: {waived}")
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
        sized, blocks = _limits(prop, inst, book, s, tradable=True, override=bool(p.get("override")))
        if sized is None:
            store.update_proposal(pid, status="blocked", error="; ".join(blocks), override=0)
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
                    sl, tp = _live_stops(p, inst)
                    try:
                        r = ec.open_buy(inst["symbol"], inst["instrumentId"], sized.amount_usd, stop_loss=sl, take_profit=tp)
                    except EtoroError as e:
                        if not (sl or tp) or not re.search(r"stop|take|rate", str(e), re.I):
                            raise
                        # eToro refused the levels (not the trade): nothing was placed, so place it once without them
                        audit(pid, "stop/take-profit refused by eToro; placing without them", str(e)[:200])
                        notify.both("Stop levels refused", f"TradeAgent: eToro refused the stop/take-profit for {p['ticker']}; "
                                    "the buy is being placed without them. Set them in the eToro app.", str(e))
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


def _live_stops(p: dict, inst: dict) -> tuple[float | None, float | None]:
    """Re-check a proposal's stop-loss / take-profit against the price now; drop any that no longer make sense."""
    price = _float(inst.get("rate"))
    sl, tp = p.get("stop_loss"), p.get("take_profit")
    if price and sl and not sl < price * (1 - terms.STOP_MIN / 2):
        sl = None                                  # price has already fallen to (or through) the stop
    if price and tp and not tp > price * (1 + terms.TP_MIN / 2):
        tp = None                                  # price has already reached the target
    return sl, tp


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
    items = levels.prioritise(items)                                # ...but a met watch level goes first
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


def _manual_symbols_today(since: str) -> set[str]:
    """Symbols started by hand today, from the 'manual analysis of X started' log lines."""
    out = set()
    for e in store.events(500):
        if e["ts"] >= since and e["msg"].startswith("manual analysis of ") and e["msg"].endswith(" started"):
            out.add(e["msg"][len("manual analysis of "):-len(" started")])
    return out


def next_in_queue() -> list[str]:
    """Shares the next scheduled share run would pick (least recently analysed first)."""
    s = store.get_settings()
    items = policy.parse_watchlist(s["watchlist"])
    shares = [i for i in items if i.asset_type != "crypto"]
    crypto = len(items) - len(shares)
    slots = max(0, int(float(s["analyses_per_day"] or 0)) - crypto)
    shares.sort(key=lambda i: store.last_run_for(i.symbol) or "")
    shares = levels.prioritise(shares)
    return [i.symbol for i in shares[:slots]]


def backfill_levels() -> list[str]:
    try:
        return levels.backfill(client(), policy.parse_watchlist(store.get_settings()["watchlist"]))
    except Exception as e:  # noqa: BLE001
        store.log("error", f"watch levels backfill failed: {e}")
        return []


def backfill_levels_once() -> None:
    """First start with watch levels: read this week's saved reports once, in the background."""
    if store.get_settings().get("levels_backfill_applied"):
        return
    store.set_setting("levels_backfill_applied", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    threading.Thread(target=backfill_levels, daemon=True).start()


def check_levels() -> None:
    try:
        levels.overnight(client())
    except Exception as e:  # noqa: BLE001
        store.log("error", f"watch levels check failed: {e}")


def daily_summary() -> None:
    s = store.get_settings()
    since = _day_start_iso()
    base = config.PUBLIC_BASE_URL
    book, html_part = None, None
    try:
        ec = client()
        book = ec.book()
        perf = performance.compute(ec, book)
        prev = performance.snapshot(perf, datetime.now(ZoneInfo(config.TZ)).date().isoformat())
        lines = performance.text(perf, prev)
        html_part = performance.html(perf, prev)
    except Exception as e:  # noqa: BLE001
        lines = [f"Portfolio unavailable: {e}"]

    # Analyses
    runs = [r for r in store.runs(200) if (r["started"] or "") >= since]
    runs.reverse()
    manual = _manual_symbols_today(since)
    limit = int(float(s["analyses_per_day"] or 0))
    lines += ["", f"Analyses today: {len(runs)} (daily limit {limit}, manual runs included)"]
    for r in runs:
        when = _local_hm(r["started"])
        kind = "manual" if r["ticker"] in manual else "scheduled"
        result = r["rating"] or ("failed: " + (r["error"] or "")[:80] if r["error"] else "still running")
        lines.append(f"  {when} {r['ticker']}: {result} ({kind}) {base}/run/{r['id']}")
    if not runs:
        lines.append("  none")

    # Trades
    todays = [p for p in store.proposals(None, 200) if p["created"] >= since]
    todays.reverse()
    lines += ["", f"Trades proposed by the app today: {len(todays)}"]
    for p in todays:
        what = _describe(p) if p["action"] != "none" else f"{p['ticker']} no trade"
        why = f" ({(p['error'] or '')[:90]})" if p["status"] in ("failed", "blocked") and p.get("error") else ""
        lines.append(f"  #{p['id']} {what} | {p['rating']} -> {p['status']}{why}")
    if not todays:
        lines.append("  none")

    # Positions opened today that the app did not place
    if book is not None:
        app_buys = {}
        for p in todays:
            if p["action"] == "buy" and p["status"] == "executed":
                app_buys[p["instrument_id"]] = app_buys.get(p["instrument_id"], 0) + 1
        outside = []
        for h in book.holdings.values():
            new = [x for x in h.positions if str(x.get("opened") or "") and _is_today(str(x["opened"]), since)]
            extra = len(new) - app_buys.get(h.instrument_id, 0)
            if extra > 0:
                cost = sum(float(x["units"]) * float(x.get("openRate") or 0) * float(x.get("conv") or 1) for x in new[-extra:])
                outside.append(f"  {h.symbol}: {extra} position(s), about ${cost:,.2f}")
        if outside:
            lines += ["", "Bought outside the app today (eToro app or website):", *outside]

    watching = levels.active()
    if watching:
        onlist = {i.symbol for i in policy.parse_watchlist(s["watchlist"])}
        lines += ["", "Watch levels:"] + [
            f"  {lv['ticker']}: {levels.describe(lv)} [{'MET' if lv['status'] == 'triggered' else 'watching'}]"
            + ("" if lv["ticker"] in onlist else f" (not on watchlist, so not analysed automatically: {levels.analyse_link(lv['ticker'])})")
            for lv in watching]
    try:
        from . import autotest
        lines += [""] + autotest.digest(datetime.fromisoformat(since).timestamp()) + [f"  {base}/daytrader"]
    except Exception as e:  # noqa: BLE001
        lines += ["", f"Strategy auto-test unavailable: {e}"]
    try:
        from . import scorecard
        lines += ["", "Scorecard: " + scorecard.headline(scorecard.evaluate(client(), store.runs(1000))),
                  f"  {base}/scorecard"]
    except Exception as e:  # noqa: BLE001
        lines += ["", f"Scorecard unavailable: {e}"]
    queue = next_in_queue()
    lines += ["", "Next share run: " + (", ".join(queue) if queue else "nothing scheduled (no share slots left after crypto)"),
              "", f"Mode: {s['mode']}  Kill switch: {s['kill_switch']}"]
    body = "\n".join(lines) + f"\n\nDashboard: {base}/"
    html_body = None
    if html_part:
        rest = "\n".join(lines[len(performance.text(perf, prev)):])
        html_body = (f"<div style='font-family:sans-serif'>{html_part}"
                     f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{escape(rest)}</pre>"
                     f"<p><a href='{base}/'>Dashboard</a></p></div>")
    notify.email("Daily summary", body, html=html_body)


def _local_hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(config.TZ)).strftime("%H:%M")
    except (ValueError, TypeError):
        return "?"


def _is_today(opened: str, since_iso: str) -> bool:
    """eToro open times come without a zone (UTC). Compare against the local day start."""
    try:
        t = datetime.fromisoformat(opened.replace("Z", "+00:00")[:26])
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t >= datetime.fromisoformat(since_iso)


# Settings changes Paul asked for when going live (1 Nov 2026). Applied once, and only ever tightening:
# a value already stricter than the target is left alone.
LIVE_TARGETS = {"max_trades_per_day": ("max", 3), "max_position_pct": ("max", 25), "cash_buffer_usd": ("min", 50)}


def apply_live_limits_once() -> None:
    s = store.get_settings()
    if s.get("live_limits_applied"):
        return
    changes = []
    for k, (kind, target) in LIVE_TARGETS.items():
        try:
            cur = float(s[k])
        except (KeyError, ValueError):
            continue
        new = min(cur, target) if kind == "max" else max(cur, target)
        if new != cur:
            store.set_setting(k, f"{new:g}")
            changes.append(f"{k} {s[k]} -> {new:g}")
    if s.get("auto_sells") != "off":
        store.set_setting("auto_sells", "off")
        changes.append(f"auto_sells {s.get('auto_sells')} -> off")
    store.set_setting("live_limits_applied", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.log("warn", "go-live limits applied (tighten only): " + ("; ".join(changes) if changes else "no change needed"))


def log_settings() -> None:
    s = store.get_settings()
    keys = ["mode", "kill_switch", "watchlist", "analyses_per_day", "schedule_time", "crypto_time", "max_trade_usd",
            "max_daily_usd", "max_trades_per_day", "cash_buffer_usd", "max_position_pct", "cooldown_days",
            "allow_sells", "auto_sells", "approval_ttl_hours", "quick_model", "deep_model", "opp_model"]
    store.log("info", "settings: " + ", ".join(f"{k}={s.get(k)}" for k in keys))


# One-off watchlist fixes found when checking Paul's settings on 1 Nov 2026 (applied once).
WATCHLIST_FIXES = {"CVX": "CVX.US=CVX",   # eToro 'CVX' is Convex Finance (crypto); Chevron is CVX.US
                   "XLV": None}           # US ETF is CFD-only for UK accounts; IUHC.L (same sector, UCITS) is already listed


def apply_watchlist_fix_once() -> None:
    s = store.get_settings()
    if s.get("watchlist_fix_applied"):
        return
    out, changes = [], []
    for raw in (s["watchlist"] or "").replace("\n", ",").split(","):
        entry = raw.strip()
        if not entry:
            continue
        key = entry.upper()
        if key in WATCHLIST_FIXES:
            repl = WATCHLIST_FIXES[key]
            changes.append(f"{entry} -> {repl or 'removed'}")
            if repl:
                out.append(repl)
        else:
            out.append(entry)
    if changes:
        store.set_setting("watchlist", ",".join(out))
    store.set_setting("watchlist_fix_applied", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.log("warn", "watchlist fix: " + ("; ".join(changes) if changes else "no change needed"))
