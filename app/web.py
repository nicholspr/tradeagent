"""Dashboard, approval pages and the 24x7 scheduler."""
import html
import hmac
import threading
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, engine, markets, opportunities, policy, store

sched = BackgroundScheduler(timezone=config.TZ)


def _hm(v: str, default: str) -> tuple[int, int]:
    try:
        h, m = (v or default).split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def refresh_markets() -> None:
    """Scheduled job: re-read the watchlist from eToro. Read-only; failures only get logged."""
    try:
        symbols = [i.symbol for i in policy.parse_watchlist(store.get_settings()["watchlist"]) if i.symbol]
        markets.invalidate(open_only=True)
        rows = markets.watchlist_rows(engine.client(), symbols) if symbols else []
        bad = [r["symbol"] for r in rows if r.get("error")]
        if bad:
            store.log("warn", f"watchlist refresh: no data for {', '.join(bad)}")
    except Exception as e:  # noqa: BLE001
        store.log("warn", f"watchlist refresh failed: {e}")


def schedule_jobs() -> None:
    s = store.get_settings()
    h, m = _hm(s["schedule_time"], "15:05")
    ch, cm = _hm(s["crypto_time"], "09:05")
    sched.add_job(engine.scheduled_analysis, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
                  id="stocks", replace_existing=True, kwargs={"asset": "stock"}, max_instances=1)
    sched.add_job(engine.scheduled_analysis, CronTrigger(hour=ch, minute=cm), id="crypto",
                  replace_existing=True, kwargs={"asset": "crypto"}, max_instances=1)
    sched.add_job(engine.process_queue, "interval", minutes=5, id="queue", replace_existing=True, max_instances=1)
    sched.add_job(refresh_markets, "interval", minutes=markets.REFRESH_MINUTES, id="markets",
                  replace_existing=True, max_instances=1)
    sched.add_job(engine.daily_summary, CronTrigger(day_of_week="mon-fri", hour=21, minute=30),
                  id="summary", replace_existing=True)


@asynccontextmanager
async def lifespan(_app):
    store.init()
    if config.SCHEDULER_ENABLED:
        schedule_jobs()
        sched.start()
    store.log("info", f"started ({config.ETORO_ENV.upper()} account{', DRY RUN' if config.DRY_RUN else ''}"
                      f"{'' if config.SCHEDULER_ENABLED else ', scheduler off'})")
    yield
    if sched.running:
        sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
B = config.ROOT_PATH  # prefix for links in pages
esc = html.escape

CSS = """
:root{color-scheme:dark;--bg:#0f1218;--card:#171b23;--line:#262c37;--line2:#1f242e;--text:#e6e9ef;
--muted:#8b93a3;--accent:#3b82f6;--accent-h:#2f6fd8;--chip:#232936;--red-bg:#3a1618;--red-t:#fca5a5;
--blue-bg:#14284a;--blue-t:#93c5fd;--input:#0f1218}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--text);line-height:1.45}
a{color:var(--blue-t)}a:hover{color:#bfdbfe}
header{position:sticky;top:0;z-index:10;background:#0b0e13ee;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:16px;padding:0 20px;min-height:56px;flex-wrap:wrap}
.brand{font-weight:700;font-size:18px;color:var(--text);text-decoration:none;white-space:nowrap}
.pills{display:flex;gap:6px;flex-wrap:wrap}
nav{display:flex;gap:4px;margin-left:auto}
nav a{color:var(--muted);text-decoration:none;padding:16px 14px;border-bottom:2px solid transparent;font-size:14px;font-weight:600}
nav a:hover{color:var(--text)}nav a.active{color:var(--text);border-bottom-color:var(--accent)}
main{padding:20px;width:100%}
.layout{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px;align-items:start}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}.c9{grid-column:span 9}.c12{grid-column:span 12}
h1{font-size:20px;margin:0 0 12px}
h2{font-size:13px;margin:0 0 12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;
display:flex;align-items:center;gap:8px;flex-wrap:wrap}
h2 .right{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400}
.clock{font-variant-numeric:tabular-nums;font-weight:600;font-size:14px;color:var(--text)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line2);vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}tr:hover td{background:#1b202a}
.big{font-size:28px;font-weight:700}.stat{display:flex;gap:28px;flex-wrap:wrap;margin-bottom:12px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:var(--chip);color:var(--text);white-space:nowrap}
.real{background:var(--red-bg);color:var(--red-t)}.demo{background:var(--blue-bg);color:var(--blue-t)}.on{background:var(--red-bg);color:var(--red-t)}
.live{background:var(--red-bg);color:var(--red-t)}.test{background:#3a2e12;color:#fcd34d}
.warn{background:#3a2e12;color:#fcd34d}.okp{background:#12301f;color:#86efac}
.banner{padding:10px 20px;font-size:14px;font-weight:600}
.banner.stop{background:var(--red-bg);color:var(--red-t);border-bottom:1px solid #5b2327}
.banner.warn{background:#3a2e12;color:#fcd34d;border-bottom:1px solid #5a4818}
button{border:0;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer;background:var(--accent);color:#fff;min-height:38px;white-space:nowrap}
button:hover{background:var(--accent-h)}button.sec{background:var(--chip);color:var(--text);border:1px solid var(--line)}
button.sec:hover{background:#2c3342}button.red{background:#b91c1c}button.red:hover{background:#991b1b}
input,select,textarea{font-size:14px;padding:8px;border:1px solid var(--line);border-radius:8px;width:100%;
background:var(--input);color:var(--text);min-height:38px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px #3b82f633}
.row{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.row>.grow{flex:1;min-width:160px}
.fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
label{font-size:12px;color:var(--muted);display:block;margin-bottom:4px}.muted{color:var(--muted);font-size:12px}
pre{white-space:pre-wrap;font-size:13px;margin:0}p{margin:10px 0 0}
summary{cursor:pointer;color:var(--muted);font-weight:600;font-size:13px}
.barcell{min-width:140px}
h3{font-size:14px;margin:22px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}h3:first-child{margin-top:0}
.unit{color:var(--muted);font-weight:400;font-size:11px;margin-left:4px}
.help{font-size:11px;color:var(--muted);margin-top:4px}
.err{font-size:12px;color:var(--red-t);margin-top:4px;font-weight:600}
input.invalid,textarea.invalid{border-color:#dc2626;box-shadow:0 0 0 2px #dc262633}.bar{position:relative;height:8px;background:var(--chip);border-radius:4px;margin-bottom:3px}
.bar .fill{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:var(--accent)}
.bar .fill.near{background:#d4a72c}.bar .fill.over{background:#dc2626}
.bar .cap{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--text);opacity:.7}
.mk{display:flex;align-items:center;gap:10px;min-width:170px}.logo{width:34px;height:34px;border-radius:8px;flex:none;background:var(--chip)}
.chg{font-weight:700;font-size:16px;white-space:nowrap}.chg small{display:block;font-size:11px;font-weight:600}
.chg.up{color:#4ade80}.chg.down{color:#f87171}.spark{width:120px;height:34px;display:block}
.px{display:inline-block;min-width:88px;text-align:right;background:var(--chip);border-radius:999px;padding:7px 14px;font-weight:700}
.px.buy{background:#2a3142}.rcell{min-width:150px}.rng{display:flex;justify-content:space-between;font-weight:600;font-size:13px}
.rbar{position:relative;height:5px;background:#3a4152;border-radius:3px;margin-top:6px}
.rbar i{position:absolute;top:-3px;width:0;height:0;margin-left:-5px;border-left:5px solid transparent;border-right:5px solid transparent;border-top:7px solid #cbd5e1}
.scell{min-width:130px}.sent span{font-size:13px}.sbar{height:6px;background:#3a4152;border-radius:3px;margin-top:6px;overflow:hidden}
.sbar b{display:block;height:100%;background:#22c55e}.sbar b.sell{background:#ef4444}
@media (max-width:1280px){.c3,.c4{grid-column:span 6}.c8,.c9{grid-column:span 12}}
@media (max-width:760px){
 header{padding:10px 12px 0;gap:8px}nav{margin-left:0;width:100%;overflow-x:auto}nav a{padding:10px 12px;flex:1;text-align:center}
 main{padding:12px}.layout{gap:12px}.c3,.c4,.c6,.c8,.c9,.c12{grid-column:span 12}
 input,select,textarea{font-size:16px}.big{font-size:24px}
}
"""

NAV = [("Overview", "/"), ("Opportunities", "/opportunities"), ("Logs", "/logs"), ("Settings", "/settings")]


MODE_LABELS = {"signals": "Signals only", "approval": "Approval", "auto": "Automatic"}


def status_strip(s: dict) -> tuple[str, str]:
    """Header pills and an optional banner describing what the app can do right now."""
    real = config.ETORO_ENV == "real"
    kill = s.get("kill_switch") == "on"
    mode = s.get("mode", "approval")
    live_orders = not config.DRY_RUN
    pills = (f"<span class='pill {'real' if real else 'demo'}' title='eToro account used'>"
             f"{'REAL' if real else 'DEMO'} account</span>"
             + (f"<span class='pill {'live' if live_orders else 'test'}' title='Whether orders are sent to eToro'>"
                f"{'LIVE orders' if live_orders else 'TEST MODE: no orders sent'}</span>")
             + f"<span class='pill {'warn' if mode == 'auto' else ''}' title='Trading mode'>"
               f"Mode: {MODE_LABELS.get(mode, esc(mode))}</span>"
             + (f"<span class='pill on'>KILL SWITCH ON</span>" if kill else
                "<span class='pill okp' title='New orders are allowed'>Trading enabled</span>"))
    if kill:
        banner = "<div class='banner stop'>Kill switch is ON: no new orders will be placed in any mode.</div>"
    elif live_orders and mode == "auto":
        banner = (f"<div class='banner warn'>Automatic mode is live: buys are placed on your "
                  f"{'REAL' if real else 'DEMO'} account without approval, within your limits.</div>")
    else:
        banner = ""
    return pills, banner


STATUS_CLS = {"pending": "warn", "approved": "demo", "executed": "okp", "failed": "on", "blocked": "on",
              "expired": "", "rejected": "", "cancelled": "warn", "signal": "demo"}


def status_pill(status: str) -> str:
    return f"<span class='pill {STATUS_CLS.get(status, '')}'>{esc(status)}</span>"


def time_left(p: dict, s: dict, now: datetime | None = None) -> str:
    """Human text for how long a pending/approved proposal has before it expires."""
    now = now or datetime.now(timezone.utc)
    try:
        if p["status"] == "pending":
            end = datetime.fromisoformat(p["created"]) + timedelta(hours=float(s["approval_ttl_hours"] or 6))
        else:
            end = datetime.fromisoformat(p["decided"] or p["created"]) + timedelta(hours=24)
    except (TypeError, ValueError):
        return ""
    secs = int((end - now).total_seconds())
    if secs <= 0:
        return "expiring now"
    h, m = divmod(secs // 60, 60)
    return f"expires in {h}h {m:02d}m" if h else f"expires in {m}m"


def queue_row(p: dict, s: dict) -> str:
    tok = engine.token_for(p["id"], p["created"])
    why = esc((p.get("reason") or "")[:140])
    if p["status"] == "pending":
        acts = (f"<div class=row><form method=post action='{B}/p/{p['id']}/{tok}/approve'><button>Approve</button></form>"
                f"<form method=post action='{B}/p/{p['id']}/{tok}/reject'><button class=sec>Reject</button></form>"
                f"<a href='{B}/p/{p['id']}/{tok}'>Details</a></div>")
    else:
        waiting = "waiting for market to open" if p.get("error") == "waiting for market" else "approved, queued"
        acts = (f"<div class=row><span class=muted>{waiting}</span>"
                f"<form method=post action='{B}/cancel/{p['id']}' onsubmit=\"return confirm('Cancel this queued trade?')\">"
                f"<button class=sec>Cancel</button></form></div>")
    return (f"<tr><td>#{p['id']}</td><td><b>{esc(engine._describe(p))}</b><br>"
            f"<span class=muted>{esc(p['rating'] or '')}{' · ' + why if why else ''}</span></td>"
            f"<td>{status_pill(p['status'])}<br><span class=muted>{time_left(p, s)}</span></td><td>{acts}</td></tr>")


def run_duration(r: dict, now: datetime | None = None) -> str:
    """'running 3m' for an unfinished run, 'took 4m' for a finished one."""
    try:
        start = datetime.fromisoformat(r["started"])
        end = datetime.fromisoformat(r["finished"]) if r["finished"] else (now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return ""
    mins = max(0, int((end - start).total_seconds() // 60))
    return f"{'took' if r['finished'] else 'running'} {mins}m" if mins else ("took <1m" if r["finished"] else "just started")


RATING_CLS = {"buy": "okp", "overweight": "okp", "hold": "", "underweight": "warn", "sell": "on"}


def rating_pill(rating: str | None, r: dict | None = None) -> str:
    if rating:
        return f"<span class='pill {RATING_CLS.get(rating.strip().lower(), 'warn')}'>{esc(rating)}</span>"
    if r and r.get("error"):
        return "<span class='pill on'>error</span>"
    return "<span class='pill warn'>running…</span>"


def active_runs(limit_hours: float = 2) -> list:
    """Unfinished runs started recently; older ones are leftovers from a restart, not live work."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=limit_hours)
    return [r for r in store.runs(5) if not r["finished"] and datetime.fromisoformat(r["started"]) > cutoff]


CLOCK_JS = ("<script>(function(){var els=document.querySelectorAll('[data-clock]');if(!els.length)return;"
            "function tick(){var n=new Date();els.forEach(function(e){try{var o={timeZone:e.dataset.clock,hour12:false,"
            "hour:'2-digit',minute:'2-digit',second:'2-digit'};e.textContent=n.toLocaleTimeString('en-GB',o);"
            "e.title=n.toLocaleString('en-GB',{timeZone:e.dataset.clock,dateStyle:'full',timeStyle:'long'});}catch(x){}});}"
            "tick();setInterval(tick,1000);})();</script>")


def live_clock() -> str:
    """Ticks every second in the browser, in the configured timezone. Shows the server time until the script runs."""
    now = datetime.now(config_tz()).strftime("%H:%M:%S")
    return f"<span class=clock data-clock='{esc(config.TZ)}'>{now}</span>"


def page(title: str, body: str, refresh: int = 0, active: str = "", nav: bool = True) -> HTMLResponse:
    s = store.get_settings()
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    pills, banner = status_strip(s)
    links = "".join(f"<a href='{B}{u}' class='{'active' if n == active else ''}'>{n}</a>" for n, u in NAV) if nav else ""
    head = (f"<header><a class=brand href='{B}/'>TradeAgent</a><div class=pills>{pills}</div>"
            f"{'<nav>' + links + '</nav>' if nav else ''}</header>")
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><meta name=theme-color content='#0b0e13'>{meta}"
                        f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{esc(title)} · TradeAgent</title>"
                        f"<style>{CSS}</style></head><body>{head}{banner}<main>{body}</main>{CLOCK_JS}</body></html>")


def card(title: str, inner: str, cls: str = "c12", right: str = "") -> str:
    r = f"<span class=right>{right}</span>" if right else ""
    return f"<section class='card {cls}'><h2>{title}{r}</h2>{inner}</section>"


def table(rows: str, head: str = "") -> str:
    return f"<div class=tbl><table>{head}{rows}</table></div>"


def alloc_state(share_pct: float, cap_pct: float) -> str:
    """'over' above the cap, 'near' within 80% of it, else 'ok'. No cap (0) never warns."""
    if cap_pct <= 0:
        return "ok"
    if share_pct > cap_pct:
        return "over"
    return "near" if share_pct >= cap_pct * 0.8 else "ok"


def alloc_bar(share_pct: float, cap_pct: float) -> str:
    state = alloc_state(share_pct, cap_pct)
    mark = f"<i class=cap style='left:{min(cap_pct, 100):.1f}%' title='Max {cap_pct:g}% per holding'></i>" if cap_pct > 0 else ""
    return (f"<div class=bar><b class='fill {state}' style='width:{min(max(share_pct, 0), 100):.1f}%'></b>{mark}</div>")


def fmt_price(v) -> str:
    v = float(v)
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}"


def sparkline(points: list[float], up: bool) -> str:
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    w, h = 120, 34
    pts = " ".join(f"{i * w / (len(points) - 1):.1f},{h - 2 - (p - lo) / span * (h - 4):.1f}" for i, p in enumerate(points))
    col = "#22c55e" if up else "#ef4444"
    return (f"<svg class=spark viewBox='0 0 {w} {h}' preserveAspectRatio=none aria-hidden=true>"
            f"<polyline points='{pts}' fill=none stroke='{col}' stroke-width=1.6 stroke-linejoin=round /></svg>")


def logo_img(url: str | None) -> str:
    if not url:
        return "<span class=logo></span>"
    return (f"<img class=logo src='{esc(url)}' alt='' loading=lazy referrerpolicy=no-referrer "
            f"onerror=\"this.style.visibility='hidden'\">")


def market_row(r: dict) -> str:
    if r.get("error"):
        return (f"<tr><td><b>{esc(r['symbol'])}</b></td><td colspan=8 class=muted>"
                f"Market data unavailable: {esc(r['error'])}</td></tr>")
    logo = logo_img(r.get("logo"))
    closed = " <span class=pill title='Market is closed; prices are the last close'>closed</span>" if r.get("tradable") is False else ""
    pct, ab = r.get("change_pct"), r.get("change_abs")
    if pct is None:
        chg = "<span class=muted>n/a</span>"
    else:
        cls = "up" if pct >= 0 else "down"
        arrow = "▲" if pct >= 0 else "▼"
        chg = (f"<div class='chg {cls}'>{'+' if ab is not None and ab > 0 else ''}{fmt_price(ab) if ab is not None else ''}"
               f"<small>{arrow} ({pct:+.2f}%)</small></div>")
    if r.get("range_pos") is None:
        rng = "<span class=muted>n/a</span>"
    else:
        rng = (f"<div class=rng><span>{fmt_price(r['low52'])}</span><span>{fmt_price(r['high52'])}</span></div>"
               f"<div class=rbar><i style='left:{r['range_pos']:.1f}%'></i></div>")
    sent = r.get("sentiment")
    sent_cell = (f"<div class=sent><span>{sent[1]:.0f}% {sent[0]}</span><div class=sbar>"
                 f"<b class='{'sell' if sent[0] == 'Selling' else ''}' style='width:{min(sent[1], 100):.0f}%'></b></div></div>"
                 if sent else "<span class=muted>n/a</span>")
    hour, asof = hour_cells(r.get("intraday"), r.get("tradable") is False)
    return (f"<tr><td><div class=mk>{logo}<div><b>{esc(r['symbol'])}</b>{closed}<br>"
            f"<span class=muted>{esc(r['name'])}</span></div></div></td><td>{chg}</td>"
            f"<td class=sparkcell>{hour}</td>"
            f"<td>{sparkline(r.get('spark') or [], (pct or 0) >= 0)}</td>"
            f"<td><span class=px>{fmt_price(r['bid']) if r.get('bid') else 'n/a'}</span></td>"
            f"<td><span class='px buy'>{fmt_price(r['ask']) if r.get('ask') else 'n/a'}</span></td>"
            f"<td class=rcell>{rng}</td><td class=scell>{sent_cell}</td><td>{asof}</td></tr>")


def hour_cells(idata: dict | None, closed: bool) -> tuple[str, str]:
    """(60-minute sparkline, 'price as of' time) from one-minute candles, shared by the portfolio and watchlist."""
    idata = idata or {}
    mins = idata.get("closes") or []
    note = "Market closed: its final hour of trading" if closed else "Last 60 minutes, one-minute prices"
    hour = (f"<span title='{note}'>{sparkline(mins, mins[-1] >= mins[0])}</span>"
            if len(mins) >= 2 else "<span class=muted>n/a</span>")
    return hour, fetched_cell(idata.get("asof"), closed, "Price as of", "the last trade")


def fetched_cell(ts: float | None, closed: bool = False, what: str = "Last read from eToro",
                 closed_note: str = "the last close") -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts, config_tz())
    same_day = dt.date() == datetime.now(config_tz()).date()
    label = dt.strftime("%H:%M:%S") if same_day else dt.strftime("%a %H:%M")
    note = f"; market closed, so the price is {closed_note}" if closed else ""
    return f"<span class=muted title='{esc(what)} {esc(dt.strftime('%Y-%m-%d %H:%M:%S %Z'))}{note}'>{label}</span>"


def config_tz():
    try:
        return ZoneInfo(config.TZ)
    except Exception:  # noqa: BLE001
        return timezone.utc


def markets_card(s: dict) -> str:
    symbols = [i.symbol for i in policy.parse_watchlist(s["watchlist"]) if i.symbol]
    if not symbols:
        return "<p class=muted>Your watchlist is empty. Add symbols in Settings.</p>"
    try:
        rows = markets.watchlist_rows(engine.client(), symbols)
    except Exception as e:  # noqa: BLE001
        return f"<p class=muted>Market data unavailable: {esc(str(e))}</p>"
    head = ("<tr><th>Markets</th><th>Change 1D</th><th>60 min</th><th>30 days</th><th>Short</th><th>Buy</th>"
            "<th>52W range</th><th>Sentiment</th><th>Price as of</th></tr>")
    ts = markets.updated_at()
    stamp = (datetime.fromtimestamp(ts, config_tz()).strftime("%H:%M") if ts else "just now")
    rows = sorted(rows, key=lambda r: (r.get("change_pct") is None, -(r.get("change_pct") or 0)))   # best 1-day change first, no data last
    return (table("".join(market_row(r) for r in rows), head)
            + f"<form method=post action='{B}/watchlist/refresh' class=row style='margin-top:10px'>"
              f"<span class=muted>Updated {stamp} · refreshes from eToro every {markets.REFRESH_MINUTES} minutes while markets are open</span>"
              f"<button class=sec>Refresh now</button></form>"
            + "<p class=muted>Short and Buy are the bid and ask. "
              "Sentiment is the share of eToro investors holding long or short. Display only.</p>")


def portfolio_card(book, s: dict, sparks: dict | None = None, info: dict | None = None,
                   fetched: float | None = None, intraday: dict | None = None) -> str:
    cap = float(s.get("max_position_pct") or 0)
    total = book.total_usd or 0
    sparks = sparks or {}
    intraday = intraday or {}
    info = info or {}
    def gain_pct(h):
        cost = h.cost_usd
        return (h.value_usd - cost) / cost * 100 if cost and h.units > 0 else None

    # highest gain first; holdings with no cost basis last (by value)
    holdings = sorted(book.holdings.values(), key=lambda h: (gain_pct(h) is None, -(gain_pct(h) or 0), -h.value_usd))
    rows, warns = "", []
    for h in holdings:
        share = h.value_usd / total * 100 if total else 0
        state = alloc_state(share, cap)
        if state != "ok":
            warns.append((h.symbol, share, state))
        flag = {"over": " <span class='pill on'>over cap</span>", "near": " <span class='pill warn'>near cap</span>"}.get(state, "")
        price = h.value_usd / h.units if h.units > 0 else None
        pts = list(sparks.get(h.instrument_id) or [])
        if pts and price:
            pts.append(price)              # end the line on the live price
        trend = sparkline(pts, pts[-1] >= pts[0]) if len(pts) >= 2 else "<span class=muted>n/a</span>"
        cost = h.cost_usd
        pnl = ""
        if cost and h.units > 0:
            diff = h.value_usd - cost
            cls = "up" if diff >= 0 else "down"
            pnl = (f"<div class='chg {cls}'><small>{'▲' if diff >= 0 else '▼'} {'+' if diff >= 0 else '-'}${abs(diff):,.2f} "
                   f"({diff / cost * 100:+.2f}%)</small></div>")
        meta = info.get(h.symbol) or {}
        mk = {True: " <span class='pill okp' title='Market is open'>open</span>",
              False: " <span class=pill title='Market is closed; price is the last close'>closed</span>"}.get(meta.get("open"), "")
        name = f"<br><span class=muted>{esc(meta['name'])}</span>" if meta.get("name") else ""
        hour, asof_cell = hour_cells(intraday.get(h.instrument_id), meta.get("open") is False)
        rows += (f"<tr><td><div class=mk>{logo_img(meta.get('logo'))}<div><b>{esc(h.symbol)}</b>{mk}{flag}{name}</div></div></td>"
                 f"<td>{('$' + fmt_price(price)) if price else ''}{pnl}</td><td class=sparkcell>{hour}</td>"
                 f"<td class=sparkcell>{trend}</td>"
                 f"<td>{h.units:.6g}</td><td>${h.value_usd:,.2f}</td>"
                 f"<td class=barcell>{alloc_bar(share, cap)}<span class=muted>{share:.0f}%</span></td>"
                 f"<td>{'$%.2f' % h.pending_usd if h.pending_usd else ''}</td><td>{asof_cell}</td></tr>")
    cash_share = book.cash_usd / total * 100 if total else 0
    rows += (f"<tr><td><div class=mk><span class=logo></span><span class=muted>Cash</span></div></td><td></td><td></td><td></td><td></td><td>${book.cash_usd:,.2f}</td>"
             f"<td class=barcell>{alloc_bar(cash_share, 0)}<span class=muted>{cash_share:.0f}%</span></td><td></td><td></td></tr>")
    note = ""
    if warns:
        note = ("<p class=muted>" + "; ".join(
            f"<b>{esc(sym)}</b> is {share:.0f}% of the portfolio, {'above' if st == 'over' else 'close to'} your {cap:g}% cap"
            for sym, share, st in warns) + ". New buys in a holding stop at the cap.</p>")
    elif cap > 0:
        note = f"<p class=muted>The marker on each bar is your {cap:g}% per-holding cap.</p>"
    return (f"<div class=stat><div><div class=muted>Total</div><div class=big>${book.total_usd:,.2f}</div></div>"
            f"<div><div class=muted>Cash</div><div class=big>${book.cash_usd:,.2f}</div></div>"
            f"<div><div class=muted>Holdings</div><div class=big>{sum(1 for h in holdings if h.units > 0)}</div></div></div>"
            + table(rows, "<tr><th>Holding</th><th>Price <span class=muted>(gain/loss vs cost)</span></th><th>60 min</th><th>30 days</th><th>Units</th><th>Value</th><th>Share of portfolio</th><th>Pending buys</th><th>Price as of</th></tr>") + note
            + (f"<p class=muted>Prices refresh every {markets.PORTFOLIO_OPEN_SECONDS} seconds while a market you hold is open, "
               f"and every {markets.PORTFOLIO_CLOSED_SECONDS // 60} minutes when they are all closed.</p>" if fetched else ""))


@app.get("/health")
def health():
    return {"ok": True, "env": config.ETORO_ENV, "mode": store.get_settings()["mode"]}


def control_card(s: dict) -> str:
    def opt(v, label):
        return f"<option value={v} {'selected' if s['mode'] == v else ''}>{label}</option>"

    kill_on = s["kill_switch"] == "on"
    u = engine.daily_usage()
    return (f"<form method=post action='{B}/mode' class=row><div class=grow><label>Mode</label><select name=mode>"
            f"{opt('signals', 'Signals only — never trades')}{opt('approval', 'Approval — I approve each trade')}"
            f"{opt('auto', 'Fully automatic — within limits')}</select></div><button>Set mode</button></form>"
            f"<form method=post action='{B}/kill' style='margin-top:14px'>"
            f"<input type=hidden name=value value={'off' if kill_on else 'on'}>"
            f"<button class={'sec' if kill_on else 'red'} style='width:100%'>"
            f"{'Turn kill switch OFF' if kill_on else 'KILL SWITCH: stop all trading'}</button></form>"
            f"<form method=post action='{B}/reset-daily' style='margin-top:14px' class=row "
            f"onsubmit=\"return confirm('Reset the daily counters? This lets the agent spend and trade up to the daily limits again today.')\">"
            f"<div class=grow><label>Daily limits used today</label>"
            f"<div class=muted>${u['spent']:,.2f} of ${float(s['max_daily_usd'] or 0):,.2f} spent · "
            f"{u['trades']} of {s['max_trades_per_day']} trades · {u['analyses']} of {s['analyses_per_day']} analyses</div>"
            f"<div class=help>Per-holding cooldowns are not affected.</div></div>"
            f"<button class=sec>Reset daily limits</button></form>")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    s = store.get_settings()
    refresh = markets.REFRESH_MINUTES * 60
    try:
        ec = engine.client()
        snap = markets.portfolio_snapshot(ec)
        refresh = markets.snapshot_ttl(snap["any_open"])
        port = portfolio_card(snap["book"], s, snap["sparks"], snap["info"], snap["fetched"], snap.get("intraday"))
    except Exception as e:  # noqa: BLE001
        port = f"<p class=muted>Portfolio unavailable: {esc(str(e))}</p>"

    pend = store.proposals("pending", 20) + store.proposals("approved", 20)
    prow = "".join(queue_row(p, s) for p in pend) or "<tr><td class=muted>Nothing waiting.</td></tr>"
    items = "".join(f"<option>{esc(i.symbol)}</option>" for i in policy.parse_watchlist(s["watchlist"]))
    active = active_runs()
    running = bool(active)
    run_card = (f"<form method=post action='{B}/run' class=row><div class=grow><select name=symbol>{items}</select></div>"
                f"<button>Analyse</button></form>"
                f"<p class=muted>Takes several minutes and costs roughly $0.50–$3 in Claude usage. Follows the current mode.</p>"
                + (f"<p><span class='pill warn'>running</span> " + ", ".join(
                    f"<a href='{B}/run/{r['id']}'><b>{esc(r['ticker'])}</b></a> <span class=muted>({run_duration(r)})</span>"
                    for r in active) + "</p><p class=muted>This page refreshes every 30 seconds.</p>" if running else ""))
    test_card = card("Test tools <span class=pill>test mode only</span>",
                     f"<form method=post action='{B}/test-trade' class=row>"
                     f"<div class=grow><select name=symbol>{items}</select></div>"
                     f"<div class=grow><select name=action><option value=buy>Buy</option><option value=sell>Sell half</option></select></div>"
                     f"<button>Create test trade</button></form>"
                     f"<p class=muted>Instant and free: no AI analysis. Follows the current mode, limits and kill switch. "
                     f"Never reaches eToro.</p>", "c3") if config.DRY_RUN else ""
    body = ("<div class=layout>"
            + card("Portfolio", port, "c12", right=live_clock())
            + card("Watchlist", markets_card(s), "c12", f"<span class=muted>{len(policy.parse_watchlist(s['watchlist']))} markets</span>")
            + card("Waiting for you", table(prow), "c6",
                   f"<span class='pill {'warn' if pend else ''}'>{len(pend)}</span>")
            + card("Run an analysis now", run_card, "c3" if config.DRY_RUN else "c6")
            + test_card
            + "</div><p class=muted>Research tool output is not financial advice. Limits are enforced by code, not by the AI.</p>")
    return page("Overview", body, refresh=30 if running else refresh, active="Overview")


@app.post("/watchlist/refresh")
def watchlist_refresh():
    markets.invalidate()
    return back()


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    hist = "".join(
        f"<tr><td>#{p['id']}</td><td>{p['created'][:16].replace('T', ' ')}</td><td><b>{esc(p['ticker'])}</b></td><td>{esc(p['rating'] or '')}</td>"
        f"<td>{esc(engine._describe(p)) if p['action'] != 'none' else ''}</td><td>{status_pill(p['status'])}</td>"
        f"<td class=muted>{esc((p['error'] or p['reason'] or '')[:120])}</td></tr>"
        for p in store.proposals(None, 25)) or "<tr><td class=muted>None yet.</td></tr>"
    runs = "".join(
        f"<tr><td><a href='{B}/run/{r['id']}'>#{r['id']}</a></td><td>{r['started'][:16].replace('T', ' ')}</td><td><b>{esc(r['ticker'])}</b></td>"
        f"<td>{rating_pill(r['rating'], r)}<br><span class=muted>{run_duration(r)}</span></td></tr>" for r in store.runs(15)) \
        or "<tr><td class=muted>None yet.</td></tr>"
    evs = "".join(f"<tr><td class=muted style='white-space:nowrap;width:1%'>{e['ts'][5:16].replace('T', ' ')}</td><td style='width:1%'>{e['level']}</td>"
                  f"<td>{esc(e['msg'][:400])}</td></tr>" for e in store.events(40))
    body = ("<div class=layout>"
            + card("Recent calls", table(hist, "<tr><th>#</th><th>When</th><th>Symbol</th><th>Rating</th><th>Trade</th><th>Status</th><th>Note</th></tr>"), "c8")
            + card("Agent runs", table(runs), "c4")
            + card("Log", table(evs), "c12", f"<a class=muted href='{B}/audit'>Full audit trail / CSV</a>")
            + "</div>")
    return page("Logs", body, refresh=30 if active_runs() else 0, active="Logs")


SETTING_SECTIONS = [
    ("Watchlist and schedule", [
        ("analyses_per_day", "Analyses per day", "shares", "Cost control: each analysis costs roughly $0.50–$3 in Claude usage."),
        ("schedule_time", "Share run time", "UK time, HH:MM", "Weekdays. 15:05 is just after the US market opens."),
        ("crypto_time", "Crypto run time", "UK time, HH:MM", "Daily, for @crypto entries."),
    ]),
    ("Trading limits", [
        ("max_trade_usd", "Max per trade", "$", "A Buy rating uses this; Overweight uses half."),
        ("max_daily_usd", "Max buys per day", "$", "Total spent on buys per day."),
        ("max_trades_per_day", "Max trades per day", "trades", "Buys and sells together."),
        ("cash_buffer_usd", "Cash to keep", "$", "Never spent by the agent."),
        ("max_position_pct", "Max in one holding", "% of portfolio", "Buys stop once a holding would pass this."),
        ("cooldown_days", "Days between trades", "days per holding", "Stops flip-flopping on one share."),
    ]),
    ("Approvals and sells", [
        ("approval_ttl_hours", "Approval expiry", "hours", "Unapproved trades expire after this."),
    ]),
]
TOGGLES = [("allow_sells", "Agent may propose sells", "If off, the agent can only buy."),
           ("auto_sells", "Auto mode may sell without approval", "Keep off so sells always need your OK.")]
TIME_FIELDS = ("schedule_time", "crypto_time")
PCT_FIELDS = {"max_position_pct"}


def validate_settings(vals: dict) -> dict:
    """Return {field: message} for every invalid value; empty means all good."""
    errors = {}
    for k in NUMERIC:
        try:
            n = float(vals.get(k, ""))
        except ValueError:
            errors[k] = "Enter a number."
            continue
        if n != n or n in (float("inf"), float("-inf")) or n < 0:
            errors[k] = "Enter a number of 0 or more."
        elif k in PCT_FIELDS and n > 100:
            errors[k] = "Enter a percentage from 0 to 100."
    for k in TIME_FIELDS:
        v = (vals.get(k) or "").strip()
        try:
            h, m = v.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError
        except ValueError:
            errors[k] = "Use 24-hour HH:MM, e.g. 15:05."
    bad = [t for t in (vals.get("watchlist") or "").replace("\n", ",").split(",")
           if t.strip() and not policy.parse_watchlist(t)[0].symbol]
    if bad:
        errors["watchlist"] = "Each entry needs an eToro symbol, e.g. META or BTC=BTC-USD@crypto."
    return errors


def settings_page(s: dict | None = None, errors: dict | None = None, status: int = 200) -> HTMLResponse:
    stored = store.get_settings()
    s = {**stored, **(s or {})}
    errors = errors or {}

    def err(k: str) -> str:
        return f"<div class=err>{esc(errors[k])}</div>" if k in errors else ""

    def bad(k: str) -> str:
        return " class=invalid" if k in errors else ""

    sections = ""
    for title, fields in SETTING_SECTIONS:
        inner = "".join(
            f"<div><label>{esc(label)} <span class=unit>{esc(unit)}</span></label>"
            f"<input name={k} value='{esc(s[k])}'{bad(k)}>{err(k)}<div class=help>{esc(help_)}</div></div>"
            for k, label, unit, help_ in fields)
        if title == "Approvals and sells":
            for k, label, help_ in TOGGLES:
                inner += (f"<div><label>{esc(label)}</label><select name={k}>"
                          f"<option {'selected' if s[k] == 'on' else ''}>on</option>"
                          f"<option {'selected' if s[k] != 'on' else ''}>off</option></select>"
                          f"<div class=help>{esc(help_)}</div></div>")
        sections += f"<h3>{esc(title)}</h3>"
        if title == "Watchlist and schedule":
            sections += (f"<label>Watchlist <span class=unit>eToro symbol[=research ticker][@crypto], comma-separated</span></label>"
                         f"<textarea name=watchlist rows=2{bad('watchlist')}>{esc(s['watchlist'])}</textarea>{err('watchlist')}"
                         f"<div class=help>Only these shares are researched, and only these can be bought.</div>"
                         f"<div style='height:12px'></div>")
        sections += f"<div class=fields>{inner}</div>"
    banner = (f"<div class='banner stop' style='border-radius:8px;margin-bottom:12px'>Not saved: fix the "
              f"{len(errors)} highlighted field{'s' if len(errors) != 1 else ''}.</div>") if errors else ""
    limits = (f"{banner}<form method=post action='{B}/settings'>{sections}<p><button>Save settings</button></p></form>")
    keys = (f"<form method=post action='{B}/keys'>"
            f"<label>Public Key (x-api-key) — {keystate('etoro_api_key', config.ETORO_API_KEY)}</label>"
            f"<input name=api_key type=password autocomplete=off placeholder='leave blank to keep'>"
            f"<label style='margin-top:12px'>Private key (x-user-key) — {keystate('etoro_user_key', config.ETORO_USER_KEY)}</label>"
            f"<textarea name=user_key rows=4 autocomplete=off placeholder='paste the long private key; leave blank to keep'></textarea>"
            f"<p><button>Save keys</button></p><p class=muted>Stored on your server only; never shown again.</p></form>")
    body = ("<div class=layout>" + card("Control", control_card(s), "c12")
            + card("Limits and schedule", limits, "c8") + card("eToro connection", keys, "c4") + "</div>")
    resp = page("Settings", body, active="Settings")
    resp.status_code = status
    return resp


@app.get("/settings", response_class=HTMLResponse)
def settings_get():
    return settings_page()


def keystate(name: str, env_value: str) -> str:
    v = store.get_secret(name)
    if v:
        return f"saved on dashboard (…{esc(v[-4:])}, {len(v)} chars)"
    if env_value:
        return f"from Hostinger settings (…{esc(env_value[-4:])})"
    return "not set"


def back(to: str = "/") -> RedirectResponse:
    markets.invalidate_portfolio()      # any action may have changed cash or positions
    return RedirectResponse(f"{B}{to}", status_code=303)


@app.post("/mode")
def set_mode(mode: str = Form(...)):
    if mode not in ("signals", "approval", "auto"):
        raise HTTPException(400, "bad mode")
    store.set_setting("mode", mode)
    return back("/settings")


@app.post("/keys")
def save_keys(api_key: str = Form(""), user_key: str = Form("")):
    if api_key.strip():
        store.set_secret("etoro_api_key", api_key)
    if user_key.strip():
        store.set_secret("etoro_user_key", user_key)
    return back("/settings")


@app.post("/kill")
def kill(value: str = Form(...)):
    store.set_setting("kill_switch", "on" if value == "on" else "off")
    return back("/settings")


@app.post("/reset-daily")
def reset_daily():
    engine.reset_daily_tracking()
    return back("/settings")


NUMERIC = {"analyses_per_day", "max_trade_usd", "max_daily_usd", "max_trades_per_day", "cash_buffer_usd",
           "max_position_pct", "cooldown_days", "approval_ttl_hours"}


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    keys = ["watchlist", *NUMERIC, *TIME_FIELDS]
    vals = {k: str(form.get(k, "")).strip() for k in keys}
    vals["allow_sells"] = "on" if form.get("allow_sells") == "on" else "off"
    vals["auto_sells"] = "on" if form.get("auto_sells") == "on" else "off"
    errors = validate_settings(vals)
    if errors:
        return settings_page(vals, errors, status=400)
    for k, v in vals.items():
        store.set_setting(k, v)
    if config.SCHEDULER_ENABLED:
        schedule_jobs()
    return back("/settings")


def etoro_pill(e: dict | None) -> str:
    if not e:
        return "<span class=pill title='eToro could not be checked'>eToro: unchecked</span>"
    if not e.get("found"):
        return "<span class='pill on'>Not found on eToro</span>"
    rate = f" · ${float(e['rate']):,.2f}" if e.get("rate") else ""
    return (f"<span class='pill okp'>On eToro · market open{rate}</span>" if e.get("tradable")
            else f"<span class='pill warn'>On eToro · market closed{rate}</span>")


def watch_entry(o: dict) -> str:
    return f"{o['symbol']}={o['symbol']}-USD@crypto" if o.get("asset_type") == "crypto" else o["symbol"]


def opportunity_card(o: dict, watched: set[str]) -> str:
    srcs = "".join(f"<li><a href='{esc(x['url'])}' target=_blank rel='noopener noreferrer'>{esc(x['title'])}</a> "
                   f"<span class=muted>{esc(urlparse(x['url']).netloc)}</span></li>" for x in o["sources"])
    on = (o.get("etoro") or {}).get("found")
    if o["symbol"] in watched:
        act = "<span class=pill>On your watchlist</span>"
    elif on:
        act = (f"<form method=post action='{B}/opportunities/watch/{esc(o['symbol'])}'>"
               f"<button class=sec>Add to watchlist</button></form>")
    else:
        act = ""
    return (f"<section class='card c12'><h2>{esc(o['symbol'])} <span class=muted>{esc(o['name'])}</span>"
            f"<span class=right>{etoro_pill(o.get('etoro'))}</span></h2>"
            f"<p><span class=pill>{esc(o['asset_type'] or 'asset')}</span> "
            f"<span class=pill>horizon: {esc(o['horizon'] or 'n/a')}</span></p>"
            f"<p><b>Why:</b> {esc(o['thesis'])}</p><p><b>Catalyst:</b> {esc(o['catalyst'])}</p>"
            f"<p><b>Risks:</b> {esc(o['risks'])}</p><p class=muted>Sources</p><ul>{srcs}</ul>{act}</section>")


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_page():
    last, good = store.latest_scan(), store.latest_scan(ok_only=True)
    running = bool(last and not last["finished"] and not opportunities.stale(last))
    block = opportunities.can_scan()
    if running:
        ctl = "<p><span class='pill warn'>Scanning the web and eToro… usually 1–3 minutes. This page refreshes itself.</span></p>"
    else:
        dis = " disabled" if block else ""
        ctl = (f"<form method=post action='{B}/opportunities/scan' "
               f"onsubmit=\"return confirm('Run a new scan? It uses Claude with web search (roughly $0.20-$1).')\">"
               f"<button{dis}>Find new opportunities</button></form>"
               + (f"<p class=muted>Unavailable: {esc(block)}.</p>" if block else ""))
    msg = ""
    if last and last["finished"] and last["error"]:
        msg = f"<p><span class='pill on'>Last scan failed</span> <span class=muted>{esc(last['error'])}</span></p>"
    head = card("Trading opportunities",
                "<p class=muted>Ideas found by searching the web and eToro-listed markets. They are research leads, "
                "not advice, and nothing is traded from this page. Add one to your watchlist to have the agent analyse it, "
                "with all normal limits and approvals.</p>" + ctl + msg)
    if good:
        watched = {i.symbol for i in policy.parse_watchlist(store.get_settings()["watchlist"])}
        cards = "".join(opportunity_card(o, watched) for o in good["items"])
        when = f"<p class=muted>Scan #{good['id']} · {esc(good['finished'])}</p>"
    else:
        cards, when = "<section class='card c12'><p class=muted>No scan yet. Press the button above.</p></section>", ""
    return page("Opportunities", head + when + cards, refresh=10 if running else 0, active="Opportunities")


@app.post("/opportunities/scan")
def opportunities_scan():
    block = opportunities.can_scan()
    if block:
        store.log("warn", f"opportunity scan not started: {block}")
        return back("/opportunities")
    threading.Thread(target=opportunities.scan, daemon=True).start()
    return back("/opportunities")


@app.post("/opportunities/watch/{symbol}")
def opportunities_watch(symbol: str):
    scan = store.latest_scan(ok_only=True)
    o = next((x for x in (scan["items"] if scan else []) if x["symbol"] == symbol.upper()), None)
    if not o or not (o.get("etoro") or {}).get("found"):
        raise HTTPException(404, "not an available opportunity")
    cur = store.get_settings()["watchlist"]
    if o["symbol"] not in {i.symbol for i in policy.parse_watchlist(cur)}:
        store.set_setting("watchlist", f"{cur},{watch_entry(o)}" if cur.strip() else watch_entry(o))
        store.log("info", f"{o['symbol']} added to watchlist from opportunity scan #{scan['id']}")
    return back("/opportunities")


@app.post("/cancel/{pid}")
def cancel_queued(pid: int):
    r = engine.cancel(pid, "dashboard")
    if r.get("error"):
        store.log("warn", f"cancel of proposal #{pid} ignored: {r['error']} (status {r.get('status')})")
    return back()


@app.post("/run")
def run_now(symbol: str = Form(...)):
    items = {i.symbol: i for i in policy.parse_watchlist(store.get_settings()["watchlist"])}
    item = items.get(symbol.upper())
    if not item:
        raise HTTPException(404, "not in watchlist")
    threading.Thread(target=engine.analyse, args=(item,), kwargs={"manual": True}, daemon=True).start()
    store.log("info", f"manual analysis of {item.symbol} started")
    return back()


@app.post("/test-trade")
def test_trade(symbol: str = Form(...), action: str = Form("buy")):
    if not config.DRY_RUN:
        raise HTTPException(403, "test trades only in test mode")
    try:
        engine.create_test(symbol.upper(), "buy" if action == "buy" else "sell")
    except Exception as e:  # noqa: BLE001
        store.log("error", f"test trade failed: {e}")
    return back()


@app.get("/audit", response_class=HTMLResponse)
def audit_page():
    rows = "".join(f"<tr><td class=muted>{e['ts'][:19].replace('T', ' ')}</td><td>{esc(e['msg'])}</td></tr>"
                   for e in store.events(1000, level="audit"))
    return page("Audit trail", f"<h1>Audit trail <a class=muted href='{B}/audit.csv'>Download CSV</a></h1>"
                f"<p class=muted>Every proposal event, newest first (times UTC).</p>"
                f"<div class=card><table>{rows or '<tr><td>None yet</td></tr>'}</table></div>")


@app.get("/audit.csv")
def audit_csv():
    import csv, io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "event"])
    for e in reversed(store.events(100000, level="audit")):
        w.writerow([e["ts"], e["msg"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tradeagent-audit.csv"})


@app.get("/run/{run_id}", response_class=HTMLResponse)
def show_run(run_id: int):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404)
    props = store.proposals_for_run(run_id)
    if props:
        trade = "".join(f"<p><b>{esc(engine._describe(p)) if p['action'] != 'none' else 'No trade'}</b> "
                        f"{status_pill(p['status'])} <span class=muted>{esc((p['error'] or p['reason'] or '')[:200])}</span></p>"
                        for p in props)
    elif r["finished"] and not r["error"]:
        trade = "<p class=muted>No trade proposed (see the decision below).</p>"
    else:
        trade = ""
    state = ("<span class='pill on'>failed</span>" if r["error"] else
             "<span class='pill okp'>complete</span>" if r["finished"] else "<span class='pill warn'>running…</span>")
    summary = (f"<div class=card><div class=stat><div><div class=muted>Rating</div>"
               f"<div class=big>{rating_pill(r['rating'])}</div></div>"
               f"<div><div class=muted>Run</div><div>{state} <span class=muted>{run_duration(r)}</span></div></div></div>{trade}</div>")
    detail = (f"<details open class=card style='margin-top:16px'><summary>Agents' full decision</summary>"
              f"<pre style='margin-top:12px'>{esc(r['decision'] or r['error'] or 'Waiting for the agents…')}</pre></details>")
    return page(f"Run {run_id}", f"<h1>{esc(r['ticker'])} <span class=muted>run #{run_id}</span></h1>"
                f"<p class=muted>{r['started'][:16].replace('T', ' ')} UTC</p>{summary}{detail}",
                refresh=30 if not r["finished"] else 0)


# ---- approval links (work from SMS/email without the dashboard login; token-protected) ----
def _check(pid: int, token: str) -> dict:
    p = store.get_proposal(pid)
    if not p or not hmac.compare_digest(token, engine.token_for(pid, p["created"])):
        raise HTTPException(404)
    return p


@app.get("/p/{pid}/{token}", response_class=HTMLResponse)
def review(pid: int, token: str):
    p = _check(pid, token)
    run = store.get_run(p["run_id"]) or {}
    acts = ""
    if p["status"] == "pending":
        acts = (f"<form method=post action='{B}/p/{pid}/{token}/approve'><button style='width:100%;padding:14px'>"
                f"Approve: {esc(engine._describe(p))}</button></form><br>"
                f"<form method=post action='{B}/p/{pid}/{token}/reject'><button class=sec style='width:100%;padding:14px'>"
                f"Reject</button></form>")
    env = config.ETORO_ENV.upper()
    return page("Approve trade", nav=False, body=f"<h1>{esc(engine._describe(p))}</h1><p><span class=pill>{env} account</span> "
                f"<span class=pill>rating {esc(p['rating'] or '')}</span> <span class=pill>status {p['status']}</span></p>"
                f"<div class=card>{acts or '<p>Already ' + p['status'] + '.</p>'}</div>"
                f"<h2 style='margin-top:20px'>Agents' decision</h2><div class=card><pre>{esc((run.get('decision') or '')[:8000])}</pre></div>")


@app.post("/p/{pid}/{token}/{action}", response_class=HTMLResponse)
def act(pid: int, token: str, action: str, request: Request):
    _check(pid, token)
    if action not in ("approve", "reject"):
        raise HTTPException(400)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    via = "dashboard" if "/trader/" in (request.headers.get("referer") or "") and "/p/" not in (request.headers.get("referer") or "") else "approval link"
    res = engine.decide(pid, approve=(action == "approve"), actor=f"{via} from {ip or 'unknown IP'}")
    p = store.get_proposal(pid)
    msg = {"executed": "Order placed.", "queued": "Approved — will be placed when the market opens.",
           "rejected": "Rejected.", "status": "Rejected."}
    text = next((v for k, v in msg.items() if k in res), esc(str(res)))
    if config.DRY_RUN and "executed" in res:
        text = "Test mode: this would have been placed. Nothing was sent to eToro."
    return page("Done", nav=False, body=f"<h1>{esc(engine._describe(p))}</h1><div class=card><p>{text}</p>"
                f"<p class=muted>Status: {p['status']} {esc(p['error'] or '')}</p></div>")
