"""End-of-day portfolio performance: each holding's move on the day and against what it cost.

Day change per holding = units x (current price - previous completed daily close). Positions opened today use
their open price as the starting point instead, so a fresh buy shows only its own move. The portfolio's change
is also compared with the previous day's saved snapshot, which captures cash coming in or going out.
"""
import json
from datetime import datetime, timezone
from html import escape

from . import store
from .levels import _completed_daily, _is_opened_today

CREATE = """CREATE TABLE IF NOT EXISTS snapshots (day TEXT PRIMARY KEY, total REAL, cash REAL, data TEXT, taken TEXT)"""


def _ensure() -> None:
    with store._lock, store.connect() as c:
        c.execute(CREATE)


def _money(x: float, sign: bool = True) -> str:
    s = f"${abs(x):,.2f}"
    return (("+" if x >= 0 else "−") + s) if sign else s


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '−'}{abs(x):.1f}%"


def compute(ec, book, now: datetime | None = None) -> dict:
    """Rows per holding plus portfolio totals. Never raises for one bad holding; that row says so."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for h in sorted(book.holdings.values(), key=lambda h: -h.value_usd):
        units = h.units or 0.0
        price = (h.value_usd / units) if units else None
        cost = h.cost_usd
        row = {"symbol": h.symbol, "value": h.value_usd, "units": units, "price": price, "cost": cost,
               "pnl": (h.value_usd - cost) if cost else None,
               "pnl_pct": ((h.value_usd / cost - 1) * 100) if cost else None,
               "day": None, "day_pct": None, "prev_close": None, "note": ""}
        try:
            bar = _completed_daily(ec.candles(h.instrument_id, "OneDay", 3), now)
            prev = bar["close"] if bar else None
        except Exception as e:  # noqa: BLE001
            prev, row["note"] = None, f"no daily price ({e})"[:80]
        if prev and price:
            conv = float(h.positions[0].get("conv") or 1) if h.positions else 1.0
            day, base_val = 0.0, 0.0
            for p in h.positions:
                u = float(p["units"])
                start = float(p["openRate"]) if (_is_opened_today(p.get("opened"), now) and p.get("openRate")) else prev
                day += u * (price / conv - start) * conv
                base_val += u * start * conv
            row.update(day=day, day_pct=(day / base_val * 100) if base_val else None, prev_close=prev)
            if any(_is_opened_today(p.get("opened"), now) for p in h.positions):
                row["note"] = "bought today: change since purchase"
        rows.append(row)
    cost_total = sum(r["cost"] for r in rows if r["cost"])
    value_total = sum(r["value"] for r in rows)
    day_total = sum(r["day"] for r in rows if r["day"] is not None)
    start_total = sum((r["value"] - r["day"]) for r in rows if r["day"] is not None)
    return {"rows": rows, "total": book.total_usd, "cash": book.cash_usd, "invested_value": value_total,
            "cost": cost_total, "pnl": (value_total - cost_total) if cost_total else None,
            "pnl_pct": ((value_total / cost_total - 1) * 100) if cost_total else None,
            "day": day_total, "day_pct": (day_total / start_total * 100) if start_total else None}


def snapshot(perf: dict, day: str) -> dict | None:
    """Save today's totals and return the previous saved day's, if any."""
    _ensure()
    data = json.dumps({r["symbol"]: {"value": r["value"], "units": r["units"]} for r in perf["rows"]})
    with store._lock, store.connect() as c:
        prev = c.execute("SELECT * FROM snapshots WHERE day < ? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
        c.execute("INSERT OR REPLACE INTO snapshots(day, total, cash, data, taken) VALUES (?,?,?,?,?)",
                  (day, perf["total"], perf["cash"], data, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return dict(prev) if prev else None


def text(perf: dict, prev: dict | None) -> list[str]:
    lines = ["Portfolio performance today",
             f"  Total ${perf['total']:,.2f} (holdings ${perf['invested_value']:,.2f}, cash ${perf['cash']:,.2f})",
             f"  Holdings on the day: {_money(perf['day'])} ({_pct(perf['day_pct'])})"]
    if prev:
        d = perf["total"] - prev["total"]
        lines.append(f"  Total vs {prev['day']}: {_money(d)} (was ${prev['total']:,.2f}; includes any cash in or out)")
    if perf["pnl"] is not None:
        lines.append(f"  Against purchase price: cost ${perf['cost']:,.2f}, now ${perf['invested_value']:,.2f}, "
                     f"{_money(perf['pnl'])} ({_pct(perf['pnl_pct'])})")
    lines.append("")
    for r in perf["rows"]:
        day = f"{_money(r['day'])} ({_pct(r['day_pct'])})" if r["day"] is not None else "day change n/a"
        vs = f"{_money(r['pnl'])} ({_pct(r['pnl_pct'])}) vs cost ${r['cost']:,.2f}" if r["pnl"] is not None else "cost n/a"
        lines.append(f"  {r['symbol']}: ${r['value']:,.2f} | today {day} | {vs}" + (f" [{r['note']}]" if r["note"] else ""))
    return lines


def html(perf: dict, prev: dict | None) -> str:
    def col(x, f):
        if x is None:
            return "<td style='color:#888'>—</td>"
        colr = "#137333" if x >= 0 else "#c5221f"
        return f"<td style='color:{colr};text-align:right'>{f(x)}</td>"
    head = ("<tr style='text-align:left;color:#555'><th>Holding</th><th style='text-align:right'>Value</th>"
            "<th style='text-align:right'>Today</th><th style='text-align:right'>Today %</th>"
            "<th style='text-align:right'>Cost</th><th style='text-align:right'>vs cost</th>"
            "<th style='text-align:right'>vs cost %</th></tr>")
    body = ""
    for r in perf["rows"]:
        note = f"<br><span style='color:#888;font-size:12px'>{escape(r['note'])}</span>" if r["note"] else ""
        body += (f"<tr><td><b>{escape(r['symbol'])}</b>{note}</td><td style='text-align:right'>${r['value']:,.2f}</td>"
                 + col(r["day"], _money) + col(r["day_pct"], _pct)
                 + (f"<td style='text-align:right'>${r['cost']:,.2f}</td>" if r["cost"] else "<td>—</td>")
                 + col(r["pnl"], _money) + col(r["pnl_pct"], _pct) + "</tr>")
    total = ("<tr style='border-top:1px solid #ccc'><td><b>Holdings</b></td>"
             f"<td style='text-align:right'><b>${perf['invested_value']:,.2f}</b></td>"
             + col(perf["day"], _money) + col(perf["day_pct"], _pct)
             + (f"<td style='text-align:right'>${perf['cost']:,.2f}</td>" if perf["cost"] else "<td>—</td>")
             + col(perf["pnl"], _money) + col(perf["pnl_pct"], _pct) + "</tr>")
    vs_prev = ""
    if prev:
        d = perf["total"] - prev["total"]
        vs_prev = (f"<p>Total vs {escape(prev['day'])}: <b style='color:{'#137333' if d >= 0 else '#c5221f'}'>"
                   f"{_money(d)}</b> (was ${prev['total']:,.2f}; includes any cash paid in or out)</p>")
    return (f"<h3 style='margin:0 0 6px'>Portfolio performance today</h3>"
            f"<p>Total <b>${perf['total']:,.2f}</b> (holdings ${perf['invested_value']:,.2f}, cash ${perf['cash']:,.2f})</p>"
            f"{vs_prev}<table cellpadding=6 style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
            f"{head}{body}{total}</table>"
            "<p style='color:#888;font-size:12px'>Today = change since the previous day's close (or since purchase for "
            "anything bought today). Cost = what the open positions cost.</p>")
