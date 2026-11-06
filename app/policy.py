"""Turns an agent rating into a sized trade proposal and enforces the hard limits.

Pure functions only (no I/O) so every rule is unit-tested. Nothing the LLM writes
can change these limits: they come from the dashboard settings.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

MIN_ORDER_USD = 10.0      # eToro minimum position size


@dataclass
class WatchItem:
    symbol: str             # eToro symbol, e.g. META, BTC, IUHC.L
    yf: str                 # ticker for the research agents (Yahoo Finance)
    asset_type: str = "stock"


def parse_watchlist(text: str) -> list[WatchItem]:
    """"META, BTC=BTC-USD@crypto" -> items. '=' gives the research ticker, '@' the asset type."""
    out = []
    for raw in (text or "").replace("\n", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        asset = "stock"
        if "@" in raw:
            raw, asset = raw.split("@", 1)
        sym, _, yf = raw.partition("=")
        sym = sym.strip().upper()
        out.append(WatchItem(sym, (yf.strip() or sym).upper(), asset.strip().lower() or "stock"))
    return out


@dataclass
class Proposal:
    action: str                     # buy | close | partial_close | none
    amount_usd: float = 0.0
    units: float | None = None
    position_ids: list = field(default_factory=list)
    reason: str = ""


def _f(s: dict, k: str) -> float:
    return float(s.get(k) or 0)


def propose(rating: str, *, holding_value: float, holding_positions: list, pending_usd: float,
            cash: float, portfolio_total: float, s: dict) -> Proposal:
    """Map the 5-tier rating to an intended trade, sized by the limits (before daily caps)."""
    r = (rating or "").strip().lower()
    if r in ("buy", "overweight"):
        size = _f(s, "max_trade_usd") * (1.0 if r == "buy" else 0.5)
        return Proposal("buy", amount_usd=round(size, 2), reason=f"rating {rating}")
    if r in ("sell", "underweight"):
        if s.get("allow_sells") != "on":
            return Proposal("none", reason=f"rating {rating}, but sells are switched off")
        if not holding_positions:
            return Proposal("none", reason=f"rating {rating}, nothing held to sell")
        if r == "sell":
            return Proposal("close", amount_usd=round(holding_value, 2),
                            position_ids=[p["positionID"] for p in holding_positions],
                            reason="rating Sell: close all positions")
        biggest = max(holding_positions, key=lambda p: p["units"])
        units = round(biggest["units"] / 2, 6)
        return Proposal("partial_close", amount_usd=round(biggest["value_usd"] / 2, 2), units=units,
                        position_ids=[biggest["positionID"]], reason="rating Underweight: halve largest position")
    return Proposal("none", reason=f"rating {rating or 'unknown'}: no trade")


def check_limits(p: Proposal, *, instrument_id: int, cash: float, portfolio_total: float,
                 holding_value: float, pending_usd: float, spent_today: float, trades_today: int,
                 last_trade_iso: str | None, tradable: bool, s: dict,
                 now: datetime | None = None, override: bool = False) -> tuple[Proposal | None, list[str]]:
    """Apply hard limits. Returns (possibly resized proposal, []) or (None, [reasons blocked]).

    override=True is a deliberate, per-trade waiver by the user of the soft limits only: daily trade count, cooldown,
    daily buy spend, position cap and the cash buffer. It never waives the kill switch, the market being closed, the
    per-trade maximum, the $10 minimum, or the cash actually in the account.
    """
    now = now or datetime.now(timezone.utc)
    blocks = []
    if p.action == "none":
        return None, [p.reason]
    if s.get("kill_switch") == "on":
        blocks.append("kill switch is on")
    if not override:
        if trades_today >= int(_f(s, "max_trades_per_day")):
            blocks.append(f"daily trade limit reached ({trades_today})")
        if last_trade_iso:
            last = datetime.fromisoformat(last_trade_iso)
            if now - last < timedelta(days=_f(s, "cooldown_days")):
                blocks.append(f"cooldown: traded this instrument on {last.date()}")
    if not tradable:
        blocks.append("market closed or instrument not tradable now")

    if p.action == "buy":
        amount = p.amount_usd
        inf = float("inf")
        room_cash = cash - (0.0 if override else _f(s, "cash_buffer_usd"))
        room_day = inf if override else _f(s, "max_daily_usd") - spent_today
        cap_pos = portfolio_total * _f(s, "max_position_pct") / 100.0
        room_pos = inf if override else cap_pos - (holding_value + pending_usd)
        amount = min(amount, _f(s, "max_trade_usd"), room_cash, room_day, room_pos)
        amount = round(max(0.0, amount), 2)
        if amount < MIN_ORDER_USD:
            why = []
            if room_cash < MIN_ORDER_USD:
                why.append(f"cash ${cash:.2f} less buffer")
            if room_day < MIN_ORDER_USD:
                why.append(f"daily spend cap (${spent_today:.2f} used)")
            if room_pos < MIN_ORDER_USD:
                why.append(f"position cap {s.get('max_position_pct')}% of ${portfolio_total:.0f}")
            blocks.append("buy below $10 minimum after limits: " + (", ".join(why) or "limits"))
        p = Proposal(p.action, amount, p.units, p.position_ids, p.reason)
    return (None, blocks) if blocks else (p, [])


def needs_approval(p: Proposal, s: dict) -> bool:
    mode = s.get("mode")
    if mode == "approval":
        return True
    if mode == "auto" and p.action in ("close", "partial_close") and s.get("auto_sells") != "on":
        return True
    return False


def account_rules(s: dict) -> str:
    """Plain-English account rules for the deciding agents, built from the live settings.

    Informational only: the same limits are enforced in code by check_limits() whatever the agents say.
    """
    def n(k):
        v = _f(s, k)
        return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    half = _f(s, "max_trade_usd") / 2
    sells = ("Underweight sells part of the holding (half of its largest position); Sell closes the whole holding."
             if s.get("allow_sells") == "on" else "Sells are switched off: Underweight and Sell do nothing.")
    return (
        "Account rules (enforced automatically by the platform; they cannot be changed by this analysis):\n"
        "- Long-only, unleveraged market orders at the current price. Limit and entry orders are not available, so "
        "do not make them the recommended action; price levels may be given as levels for the investor to review. "
        "Stop-loss and take-profit prices are attached to the order only for short-term ideas (see below).\n"
        f"- The rating alone sets the trade: Buy opens or adds ${n('max_trade_usd')}; Overweight adds "
        f"${half:,.2f}; Hold does nothing. {sells}\n"
        f"- At most ${n('max_daily_usd')} of buys and {n('max_trades_per_day')} trades per day; at least "
        f"${n('cash_buffer_usd')} cash is always kept; no holding may exceed {n('max_position_pct')}% of the "
        f"portfolio; at least {n('cooldown_days')} days between trades in the same instrument.\n"
        "- Sizing advice that differs from these rules will not be followed."
    )
