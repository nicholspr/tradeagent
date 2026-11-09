"""Dashboard, approval pages and the 24x7 scheduler."""
import html
import hmac
import logging
import threading
from urllib.parse import urlparse, quote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import autotest, daytrader, performance, scorecard, terms, config, engine, markets, opportunities, policy, store

sched = BackgroundScheduler(timezone=config.TZ)


class _QuietAccessLog(logging.Filter):
    """Keep routine polling out of the server log, so it holds days of real events instead of
    minutes of price requests. Page loads, button presses and errors are still logged."""
    NOISY = ('"GET /health', '"GET /daytrader/quotes', '"GET /daytrader/quote/', '"GET /daytrader/candles/',
             '"GET /daytrader/autotest ', '"GET /daytrader/autotest/chart/', '"GET /daytrader/search')

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(n in msg for n in self.NOISY):
            return '" 200' not in msg and '" 304' not in msg          # failures still show
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())


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
    sched.add_job(engine.daily_summary, CronTrigger(hour=21, minute=30),
                  id="summary", replace_existing=True)
    sched.add_job(engine.check_levels, CronTrigger(hour=6, minute=30), id="levels",
                  replace_existing=True, max_instances=1)
    sched.add_job(autotest.tick, "interval", minutes=1, id="autotest", replace_existing=True,
                  max_instances=1, coalesce=True)


@asynccontextmanager
async def lifespan(_app):
    store.init()
    if not config.DRY_RUN:
        engine.apply_live_limits_once()
    engine.apply_watchlist_fix_once()
    performance.seed_once()
    if config.SCHEDULER_ENABLED:
        engine.backfill_levels_once()
    engine.log_settings()
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

NAV = [("Overview", "/"), ("Opportunities", "/opportunities"), ("Day trader", "/daytrader"), ("Scorecard", "/scorecard"), ("Logs", "/logs"),
       ("Settings", "/settings")]


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
    run_card = (f"<form method=get action='{B}/preflight' class=row><div class=grow><select name=symbol>{items}</select></div>"
                f"<button>Check and analyse</button></form>"
                f"<p class=muted>First checks whether your limits would allow a trade, then you choose to run it. "
                f"The analysis takes several minutes and costs roughly $0.50–$3 in Claude usage. Follows the current mode.</p>"
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
            + card("Watch levels", levels_card(), "c12",
                   "<span class=muted>checked each morning at 06:30 against the previous day's close</span>")
            + card("Waiting for you", table(prow), "c6",
                   f"<span class='pill {'warn' if pend else ''}'>{len(pend)}</span>")
            + card("Run an analysis now", run_card, "c3" if config.DRY_RUN else "c6")
            + test_card
            + "</div><p class=muted>Research tool output is not financial advice. Limits are enforced by code, not by the AI.</p>")
    return page("Overview", body, refresh=30 if running else refresh, active="Overview")


def levels_card() -> str:
    from . import levels
    button = (f"<form method=post action='{B}/levels/backfill' style='margin-top:10px'><button class=sec>"
              f"Read conditions from latest reports</button> <span class=muted>no new analysis; fills symbols that "
              f"have none, from reports up to {levels.BACKFILL_DAYS} days old</span></form>")
    rows = levels.active()
    if not rows:
        return button + ("<p class=muted>None yet. When an analysis says to wait for a price (a breakout above a level, or a "
                "pull-back into a zone) the condition appears here and is checked every morning.</p>")
    body = ""
    onlist = {i.symbol for i in policy.parse_watchlist(store.get_settings()["watchlist"])}
    for lv in rows:
        status = ("<span class='pill okp'>Met</span>" if lv["status"] == "triggered" else "<span class=pill>Watching</span>")
        if lv["ticker"] not in onlist:
            status += (f"<br><span class='pill warn'>Not on watchlist</span><br><a href='{B}/preflight?symbol={quote(lv['ticker'])}'>"
                       f"Analyse now</a>")
        last = f"{lv['last_close']:,.6g}" if lv.get("last_close") else "not checked yet"
        info = f"<br><span class=muted>{esc(lv['trigger_info'] or '')}</span>" if lv["status"] == "triggered" else ""
        body += (f"<tr><td><b>{esc(lv['ticker'])}</b></td><td>{esc(levels.describe(lv))}{info}</td><td>{status}</td>"
                 f"<td>{last}</td><td>{lv['expires'][:10]}</td>"
                 f"<td><a href='{B}/run/{lv['run_id']}'>#{lv['run_id']}</a></td></tr>")
    return table(body, "<tr><th>Symbol</th><th>Buy condition</th><th>Status</th><th>Last close</th><th>Expires</th>"
                       "<th>From run</th></tr>") + (
        "<p class=muted>A met condition moves the share to the front of the next scheduled analysis. "
        "The new analysis decides whether to trade; nothing is bought from a condition alone. "
        "Symbols <b>not on the watchlist</b> are still checked and emailed when met, but aren't analysed automatically: "
        "use <b>Analyse now</b>, or add them back on Settings.</p>") + button


@app.post("/levels/backfill")
def levels_backfill():
    engine.backfill_levels()
    return back()


@app.post("/watchlist/refresh")
def watchlist_refresh():
    markets.invalidate()
    return back()


def _sc_pct(x, good=None):
    if x is None:
        return "<td class=muted>—</td>"
    col = "" if good is None else ("color:#4ade80" if good else "color:#f87171")
    return f"<td style='{col}'>{x:+.1f}%</td>"


def _sc_table(groups: list[dict], first: str) -> str:
    head = f"<tr><th>{first}</th><th>Calls</th>" + "".join(
        f"<th>{h}d return</th><th>{h}d vs {scorecard.BENCHMARK}</th><th>{h}d worked</th>" for h in scorecard.HORIZONS) + "</tr>"
    body = ""
    for g in groups:
        body += f"<tr><td><b>{esc(str(g['name']))}</b></td><td>{g['count']}</td>"
        for h in scorecard.HORIZONS:
            vs = g["avg_vs"][h]
            hit = g["hits"][h]
            body += _sc_pct(g["avg"][h]) + _sc_pct(vs, None if vs is None else vs > 0)
            body += (f"<td>{hit:.0f}% <span class=muted>of {g['scored'][h]}</span></td>" if hit is not None
                     else f"<td class=muted>{'pending' if g['pending'][h] else '—'}</td>")
        body += "</tr>"
    return table(body or "<tr><td class=muted>No rated analyses yet.</td></tr>", head)


@app.get("/scorecard", response_class=HTMLResponse)
def scorecard_page(refresh: int = 0):
    if refresh:
        scorecard.clear_cache()
    try:
        rows = scorecard.evaluate(engine.client(), store.runs(1000))
    except Exception as e:  # noqa: BLE001
        return page("Scorecard", card("Scorecard", f"<p class=muted>Unavailable: {esc(str(e))}</p>"), active="Scorecard")
    calls = ""
    for r in sorted(rows, key=lambda r: -r["id"])[:60]:
        start = "" if r["start"] is None else f"{r['start']:,.6g}"
        calls += (f"<tr><td><a href='{B}/run/{r['id']}'>#{r['id']}</a></td><td>{r['day']}</td><td><b>{esc(r['ticker'])}</b></td>"
                  f"<td>{rating_pill(r['rating'])}</td><td>{start}</td>")
        for h in scorecard.HORIZONS:
            w = scorecard.worked(r, h)
            calls += _sc_pct(r["returns"].get(h)) + _sc_pct(r["vs"].get(h), w)
        calls += f"<td class=muted>{esc(scorecard._short(r['models']))}{(' · ' + esc(r['note'])) if r['note'] else ''}</td></tr>"
    calls_head = ("<tr><th>Run</th><th>Date</th><th>Symbol</th><th>Rating</th><th>Price then</th>"
                  + "".join(f"<th>{h}d</th><th>{h}d vs bench</th>" for h in scorecard.HORIZONS) + "<th>Models</th></tr>")
    intro = (f"<p>{esc(scorecard.headline(rows))}.</p><p class=muted>Each call is measured from the price at the time "
             f"of the analysis to the close {', '.join(str(h) for h in scorecard.HORIZONS)} trading days later, and compared "
             f"with {scorecard.BENCHMARK} (Vanguard FTSE All-World) over the same dates. A Buy or Overweight <b>worked</b> "
             "if the share beat the benchmark; an Underweight or Sell worked if it lagged it. Holds are shown but not "
             "scored. Results need many calls before they mean much: treat anything under 20 calls as anecdote.</p>"
             f"<p><a class=muted href='{B}/scorecard?refresh=1'>Refresh prices</a></p>")
    body = ("<div class=layout>"
            + card("Does it work?", intro, "c12")
            + card("By rating", _sc_table(scorecard.by_rating(rows), "Rating"), "c12")
            + card("By AI models (Buy, Overweight, Underweight, Sell calls)", _sc_table(scorecard.by_models(rows), "Models"), "c12")
            + card("By symbol", _sc_table(scorecard.by_ticker(rows), "Symbol"), "c12")
            + card("Every call", table(calls or "<tr><td class=muted>None yet.</td></tr>", calls_head), "c12")
            + "</div>")
    return page("Scorecard", body, active="Scorecard")


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
    ("Schedule", [
        ("analyses_per_day", "Analyses per day", "shares", "Cost control: each analysis costs roughly $0.15–$3 in Claude usage, depending on the AI models below."),
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
MODEL_FIELDS = [("quick_model", "Evidence and debate agents",
                 "10 agents: the four analysts, Bull, Bear, trader and three risk debaters. Most of the cost is here."),
                ("deep_model", "Deciding agents", "2 agents: Research Manager and Portfolio Manager, who make the call."),
                ("opp_model", "Opportunities scan", "Web research on the Opportunities page.")]
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
    for k, *_ in MODEL_FIELDS:
        if k in vals and vals[k] not in config.MODELS:
            errors[k] = "Pick a model from the list."
    if bad:
        errors["watchlist"] = "Each entry needs an eToro symbol, e.g. META or BTC=BTC-USD@crypto."
    return errors


def settings_page(s: dict | None = None, errors: dict | None = None, status: int = 200,
                  msg: str = "", wl_error: str = "", wl_form: dict | None = None) -> HTMLResponse:
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
        sections += f"<div class=fields>{inner}</div>"
    models = "".join(
        f"<div><label>{esc(label)}</label><select name={k}{bad(k)}>"
        + "".join(f"<option value={m} {'selected' if s.get(k) == m else ''}>{esc(t)}</option>" for m, t in config.MODELS.items())
        + f"</select>{err(k)}<div class=help>{esc(help_)}</div></div>" for k, label, help_ in MODEL_FIELDS)
    sections += (f"<h3>AI models</h3><div class=fields>{models}</div>"
                 f"<div class=help>Changes apply to the next analysis. Each run's log line records the models it used.</div>")
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
            + card("Watchlist", watchlist_editor(stored, msg, wl_error, wl_form or {}), "c12",
                   f"<span class=muted>{len(policy.parse_watchlist(stored['watchlist']))} symbols · only these are "
                   f"researched, and only these can be bought</span>")
            + card("Limits and schedule", limits, "c8") + card("eToro connection", keys, "c4") + "</div>")
    resp = page("Settings", body, active="Settings")
    resp.status_code = status
    return resp


@app.get("/settings", response_class=HTMLResponse)
def settings_get(msg: str = ""):
    return settings_page(msg=msg[:300])


def watchlist_editor(s: dict, msg: str = "", error: str = "", form: dict | None = None) -> str:
    """Watchlist as a table (symbol, name, type, latest price, delete) plus an add form that checks eToro first."""
    form = form or {}
    items = policy.parse_watchlist(s["watchlist"])
    try:
        quotes = {r["symbol"]: r for r in markets.watchlist_rows(engine.client(), [i.symbol for i in items])} if items else {}
    except Exception as e:  # noqa: BLE001
        quotes, error = {}, error or f"Prices unavailable: {e}"
    held = set()
    try:
        held = {h.symbol for h in markets.portfolio_snapshot(engine.client())["book"].holdings.values() if h.units > 0}
    except Exception:  # noqa: BLE001
        pass
    rows = ""
    for i in items:
        q = quotes.get(i.symbol) or {}
        research = f"<br><span class=muted>researched as {esc(i.yf)}</span>" if i.yf != i.symbol else ""
        kind = "<span class=pill>crypto</span>" if i.asset_type == "crypto" else esc(q.get("asset_class") or "")
        if q.get("error"):
            price = f"<span class=muted>{esc(q['error'][:60])}</span>"
        elif q.get("price"):
            chg = q.get("change_pct")
            price = f"${float(q['price']):,.2f}" + (
                f" <span style='color:{'#4ade80' if chg >= 0 else '#f87171'}'>{chg:+.1f}%</span>" if chg is not None else "")
        else:
            price = "<span class=muted>—</span>"
        own = " <span class='pill okp'>held</span>" if i.symbol in held else ""
        short = terms.term(i.symbol) == terms.SHORT
        own += " <span class='pill warn'>short term</span>" if short else ""
        toggle = (f"<form method=post action='{B}/watchlist/term' style='display:inline'>"
                  f"<input type=hidden name=symbol value='{esc(i.symbol)}'>"
                  f"<input type=hidden name=term value={'medium' if short else 'short'}>"
                  f"<button class=sec title='Short term: analysed for a days-to-weeks hold, with a stop-loss and "
                  f"take-profit attached to any buy'>{'Make medium term' if short else 'Make short term'}</button></form> ")
        warn = ("This only stops the agent researching and buying it. It does not sell your holding."
                if i.symbol in held else "Remove from the watchlist?")
        rows += (f"<tr><td><b>{esc(i.symbol)}</b>{own}{research}</td><td>{esc(q.get('name') or '')}</td><td>{kind}</td>"
                 f"<td>{price}</td><td style='white-space:nowrap'>{toggle}<form method=post action='{B}/watchlist/remove' style='display:inline' "
                 f"onsubmit=\"return confirm('{esc(i.symbol)}: {esc(warn)}')\">"
                 f"<input type=hidden name=symbol value='{esc(i.symbol)}'><button class=sec>Delete</button></form></td></tr>")
    tbl = table(rows or "<tr><td class=muted>The watchlist is empty.</td></tr>",
                "<tr><th>Symbol</th><th>Name</th><th>Type</th><th>Latest price</th><th></th></tr>")
    flash = f"<div class='banner warn' style='border-radius:8px;margin-bottom:12px'>{esc(error)}</div>" if error else ""
    if msg and not error:
        flash = f"<div class=banner style='border-radius:8px;margin-bottom:12px;background:#12321f;color:#86efac'>{esc(msg)}</div>"
    force = (f"<label style='margin-top:8px'><input type=checkbox name=force value=1 style='width:auto;min-height:0'> "
             f"Add anyway</label>") if form.get("can_force") else ""
    add = (f"<form method=post action='{B}/watchlist/add' class=row style='margin-top:14px'>"
           f"<div class=grow><label>eToro symbol</label><input name=symbol value='{esc(form.get('symbol', ''))}' "
           f"placeholder='e.g. NVDA, IUHC.L, ETH' required></div>"
           f"<div class=grow><label>Research ticker <span class=unit>optional, if Yahoo uses a different one</span></label>"
           f"<input name=research value='{esc(form.get('research', ''))}' placeholder='e.g. CVX for CVX.US'></div>"
           f"<div style='min-width:170px'><label>Holding period</label><select name=term>"
           f"<option value=medium {'selected' if form.get('term') != 'short' else ''}>Medium term</option>"
           f"<option value=short {'selected' if form.get('term') == 'short' else ''}>Short term</option></select></div>"
           f"<button>Check on eToro and add</button>{force}</form>"
           "<p class=muted>The symbol is looked up on eToro before it is added. Crypto is detected automatically and "
           "researched as SYMBOL-USD. US-listed funds (ETFs) are flagged, because on a UK account they can only be "
           "bought as CFDs; use a London-listed UCITS fund instead (e.g. IUHC.L, IGLN.L).</p>")
    return flash + tbl + add


def _entry_for(symbol: str, research: str, inst: dict) -> str:
    cls = str(inst.get("assetClass") or "").lower()
    if cls == "crypto":
        return f"{symbol}={research or symbol + '-USD'}@crypto"
    if not research and symbol.endswith(".US"):
        research = symbol[:-3]                                # eToro CVX.US -> Yahoo CVX
    return f"{symbol}={research}" if research and research != symbol else symbol


@app.post("/watchlist/add", response_class=HTMLResponse)
def watchlist_add(symbol: str = Form(...), research: str = Form(""), force: str = Form(""), term: str = Form("medium")):
    sym, research = symbol.strip().upper(), research.strip().upper()
    form = {"symbol": sym, "research": research, "term": term}
    cur = store.get_settings()["watchlist"]
    if not sym or any(c in sym for c in ",=@ "):
        return settings_page(wl_error="Enter one eToro symbol, e.g. NVDA or IUHC.L.", wl_form=form, status=400)
    if sym in {i.symbol for i in policy.parse_watchlist(cur)}:
        return settings_page(wl_error=f"{sym} is already on the watchlist.", wl_form=form, status=400)
    try:
        inst = engine.client().find_instrument(sym)
    except Exception as e:  # noqa: BLE001
        hint = " For US shares eToro sometimes adds .US (e.g. CVX.US for Chevron)." if "." not in sym else ""
        return settings_page(wl_error=f"{sym} was not found on eToro, so it was not added ({str(e)[:120]}).{hint}",
                             wl_form=form, status=400)
    name, cls = inst.get("name") or sym, str(inst.get("assetClass") or "")
    if not force:
        warn = ""
        if "etf" in cls.lower() and not sym.endswith(".L"):
            warn = (f"{sym} ({name}) is a US-listed fund. On a UK eToro account these can usually only be bought as a "
                    f"CFD, which this app does not use. A London-listed UCITS fund is the usual alternative.")
        elif inst.get("buyEnabled") is False:
            warn = f"eToro reports that buying {sym} ({name}) is currently disabled."
        if warn:
            return settings_page(wl_error=warn + " Tick 'Add anyway' to add it regardless.",
                                 wl_form={**form, "can_force": True}, status=400)
    entry = _entry_for(sym, research, inst)
    store.set_setting("watchlist", f"{cur},{entry}" if cur.strip() else entry)
    terms.set_term(sym, term, "added on the Settings page")
    store.log("info", f"watchlist: added {entry} ({name}, {cls or 'unknown type'}) after eToro check"
                      + (" as a short-term idea" if term == terms.SHORT else ""))
    markets.invalidate()
    return RedirectResponse(f"{B}/settings?msg=" + quote(f"Added {sym}: {name}" + (f" ({cls})" if cls else "") + "."),
                            status_code=303)


@app.post("/watchlist/term")
def watchlist_term(symbol: str = Form(...), term: str = Form("medium")):
    sym = symbol.strip().upper()
    if sym not in {i.symbol for i in policy.parse_watchlist(store.get_settings()["watchlist"])}:
        raise HTTPException(404, "not in watchlist")
    terms.set_term(sym, term, "changed on the Settings page")
    store.log("info", f"watchlist: {sym} set to {'short' if term == terms.SHORT else 'medium'} term")
    return RedirectResponse(f"{B}/settings?msg=" + quote(
        f"{sym} is now {'a short-term idea: analyses judge it over days to weeks and any buy carries a stop-loss and take-profit' if term == terms.SHORT else 'medium term'}."),
        status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(symbol: str = Form(...)):
    sym = symbol.strip().upper()
    items = policy.parse_watchlist(store.get_settings()["watchlist"])
    keep = [i for i in items if i.symbol != sym]
    if len(keep) == len(items):
        return RedirectResponse(f"{B}/settings?msg=" + quote(f"{sym} was not on the watchlist."), status_code=303)
    raw = [e.strip() for e in store.get_settings()["watchlist"].replace("\n", ",").split(",") if e.strip()]
    raw = [e for e in raw if policy.parse_watchlist(e)[0].symbol != sym]
    store.set_setting("watchlist", ",".join(raw))
    terms.forget(sym)
    store.log("info", f"watchlist: removed {sym}")
    return RedirectResponse(f"{B}/settings?msg=" + quote(f"Removed {sym}. Any holding in it is unchanged."),
                            status_code=303)


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
    keys = [*NUMERIC, *TIME_FIELDS] + (["watchlist"] if "watchlist" in form else [])   # watchlist has its own editor
    vals = {k: str(form.get(k, "")).strip() for k in keys}
    vals["allow_sells"] = "on" if form.get("allow_sells") == "on" else "off"
    vals["auto_sells"] = "on" if form.get("auto_sells") == "on" else "off"
    for k, *_ in MODEL_FIELDS:
        vals[k] = str(form.get(k) or store.get_settings().get(k, "")).strip()
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
            f"<span class=pill>horizon: {esc(o['horizon'] or 'n/a')}</span>"
            + (f" <span class=pill>{esc(o['sector'])}</span>" if o.get("sector") else "")
            + (" <span class='pill warn'>US-listed fund: CFD-only on a UK account</span>"
               if "etf" in str((o.get("etoro") or {}).get("asset_class") or o.get("asset_type") or "").lower()
               and not o["symbol"].endswith(".L") else "")
            + "</p>"
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
               f"<div class=row><div class=grow style='max-width:280px'><label>Holding period</label>"
               f"<select name=horizon>" + "".join(f"<option value={k}>{esc(v['label'])}</option>"
                                                for k, v in opportunities.HORIZONS.items()) + "</select></div>"
               f"<button{dis}>Find new opportunities</button></div></form>"
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
def opportunities_scan(horizon: str = Form("medium")):
    block = opportunities.can_scan()
    if block:
        store.log("warn", f"opportunity scan not started: {block}")
        return back("/opportunities")
    threading.Thread(target=opportunities.scan, kwargs={"horizon": horizon}, daemon=True).start()
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
        if o.get("term") == terms.SHORT:
            terms.set_term(o["symbol"], terms.SHORT, f"opportunity scan #{scan['id']}")
        store.log("info", f"{o['symbol']} added to watchlist from opportunity scan #{scan['id']}"
                          + (" as a short-term idea" if o.get("term") == terms.SHORT else ""))
    return back("/opportunities")


# ---- day trader test page --------------------------------------------------------------
DT_CSS = """
.dt-search{position:relative;max-width:520px}
.dt-results{position:absolute;z-index:20;left:0;right:0;top:100%;margin-top:4px;background:var(--card);border:1px solid var(--line);
border-radius:10px;max-height:340px;overflow:auto;display:none;box-shadow:0 8px 24px #0008}
.dt-results div{display:flex;gap:10px;align-items:center;padding:8px 10px;cursor:pointer;border-bottom:1px solid var(--line2)}
.dt-results div:hover,.dt-results div.hl{background:#1f2531}.dt-results img{width:22px;height:22px;border-radius:50%}
.dt-results b{min-width:84px}.dt-results span{color:var(--muted);font-size:12px}
.dt-head{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;margin-bottom:8px}
.dt-head .dt-px{font-size:26px;font-weight:700}.dt-up{color:#22c55e}.dt-dn{color:#ef4444}
.dt-chartcard{grid-column:span 12}.dt-paper{grid-column:span 8}.dt-side{grid-column:span 4}
@media(max-width:1100px){.dt-paper,.dt-side{grid-column:span 12}}
.dt-pchart{width:100%;height:220px;display:block;margin:6px 0}.dt-pchart text{fill:var(--muted);font-size:11px}
.dt-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:6px 14px;font-size:12px;align-items:end}
.dt-stats div span{display:block;color:var(--muted)}.dt-stats div b{font-variant-numeric:tabular-nums;font-size:13px}
.dt-key{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted);margin-bottom:8px}.dt-key i{display:inline-block;width:18px;border-top:2px dashed;vertical-align:middle;margin-right:5px}
.dt-kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px;margin:10px 0}
.dt-kv>span:nth-child(odd){color:var(--muted)}.dt-kv>span:nth-child(even){text-align:right;font-variant-numeric:tabular-nums}.dt-kv>span.tot{color:var(--text)}
.dt-big{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}
.dt-kv .tot{font-weight:700;border-top:1px solid var(--line);padding-top:6px}
.dt-paper button.close{background:#b91c1c}.dt-paper button.close:hover{background:#991b1b}#dt-openbtn{margin-left:auto}
.dt-pos{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:8px}
.dt-prow{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}.dt-prow a{color:var(--text);text-decoration:none}
.dt-pl{margin-left:auto;font-weight:700;font-size:17px;font-variant-numeric:tabular-nums}.dt-pl small{font-size:12px;font-weight:600}
.dt-pos details{margin-top:6px}.dt-pos summary{font-weight:500;font-size:12px}
.dt-paper button.sm{min-height:30px;padding:5px 14px;font-size:13px}
.dt-last{margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}
.dt-sig{margin:10px 0 2px;padding:10px 12px;border-radius:10px;background:#0f131a;border:1px solid var(--line)}
.dt-sig .hd{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}.dt-sig .sp{font-weight:700;font-size:12px;padding:2px 9px;border-radius:999px}
.dt-sig .nums{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12px;margin-top:6px;color:var(--muted)}.dt-sig .nums b{color:var(--text);font-variant-numeric:tabular-nums}
.dt-sig ul{margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted)}.dt-sig details summary{font-size:12px;margin-top:6px}
.dt-psig{font-size:12px;margin:4px 0}.dt-psig .sp{font-weight:700;font-size:11px;padding:1px 8px;border-radius:999px;margin-right:6px}
.dt-toast{position:fixed;right:16px;bottom:16px;z-index:50;background:#14532d;color:#fff;border:1px solid #22c55e;border-radius:12px;padding:12px 16px;max-width:340px;box-shadow:0 8px 24px #0009;font-size:14px}
.dt-auto{grid-column:span 12}.dt-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:10px 0 14px}
.dt-tiles div{background:#0f131a;border:1px solid var(--line);border-radius:10px;padding:10px 12px}.dt-tiles span{display:block;font-size:12px;color:var(--muted)}
.dt-tiles b{font-size:20px;font-variant-numeric:tabular-nums}.dt-auto h3{margin:16px 0 6px}.dt-auto td{font-variant-numeric:tabular-nums}
.dt-arow{display:grid;grid-template-columns:minmax(90px,140px) 1fr auto;gap:4px 12px;padding:9px 0;border-bottom:1px solid var(--line2);align-items:start}
.dt-arow .sym b{display:block}.dt-arow .sym span{display:block;font-size:11px;color:var(--muted);line-height:1.4}.dt-arow .sg{font-size:12px;line-height:1.5}
.dt-arow .sg div{margin-bottom:3px}.dt-arow .sg i{font-style:normal;color:var(--muted);display:inline-block;min-width:44px}
.dt-arow button{min-height:28px;padding:3px 10px;font-size:12px}.dt-sw{display:inline-flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;margin-right:12px;user-select:none}
.dt-sw input{appearance:none;width:30px;height:17px;border-radius:9px;background:#374151;position:relative;cursor:pointer;margin:0;min-height:0;padding:0;border:0}
.dt-sw input:checked{background:#16a34a}.dt-sw input::after{content:'';position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#fff;transition:left .15s}
.dt-sw input:checked::after{left:15px}.dt-arow .ctl{display:flex;flex-wrap:wrap;align-items:center;gap:6px 0;justify-content:flex-end}
.dt-grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:6px}
@media(max-width:1200px){.dt-grid4{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.dt-grid4{grid-template-columns:1fr}}
.dt-gc{background:#0f131a;border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}
.dt-gc .hd{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.dt-gc .hd b{font-size:14px}.dt-gc .st{font-size:11.5px;color:var(--muted);line-height:1.45;min-height:16px}
.dt-gc svg{width:100%;height:170px;display:block;margin-top:4px}.dt-gc svg text{fill:var(--muted);font-size:10px}.dt-gc .run{border-color:#f59e0b66}
.dt-strat{font-size:13px;font-weight:600;margin:12px 0 4px}
@media(max-width:600px){.dt-arow{grid-template-columns:1fr auto}.dt-arow .ctl{justify-content:flex-start}.dt-arow .sg{grid-column:1/-1;grid-row:2}.dt-tiles b{font-size:17px}}
.dt-auto .sp{font-weight:700;font-size:11px;padding:1px 8px;border-radius:999px;margin-right:6px;white-space:nowrap}
.dt-set{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
@media(max-width:700px){.dt-pchart{height:200px}}
"""

DT_JS = """
(()=>{
const B=document.body.dataset.root||'';
const $=id=>document.getElementById(id);
let cur=null,data=null,timer=null,next=0;
const fmt=v=>v==null?'–':(Math.abs(v)>=1000?v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})
  :Math.abs(v)>=1?v.toFixed(Math.abs(v)>=100?2:3):String(+v.toPrecision(4)));
const hm=t=>new Date(t*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
const hms=t=>new Date(t*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
// ---- search
let st=null,hl=-1,items=[];
const box=$('dt-q'),res=$('dt-results');
box.addEventListener('input',()=>{clearTimeout(st);const q=box.value.trim();if(!q){res.style.display='none';return;}
  st=setTimeout(()=>fetch(B+'/daytrader/search?q='+encodeURIComponent(q)).then(r=>r.json()).then(list=>{
    items=list;hl=-1;res.innerHTML=list.length?'':'<div><span>No eToro instruments match</span></div>';
    list.forEach((it,i)=>{const d=document.createElement('div');
      d.innerHTML=(it.logo?'<img src=\"'+it.logo+'\" alt=\"\" referrerpolicy=no-referrer>':'')+'<b></b><span></span>';
      d.querySelector('b').textContent=it.symbol;d.querySelector('span').textContent=it.name+' · '+it.type;
      d.onclick=()=>pick(it);res.appendChild(d);});
    res.style.display='block';}).catch(()=>{}),250);});
box.addEventListener('keydown',e=>{const ds=res.querySelectorAll('div');if(!items.length)return;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();hl=(hl+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;
    ds.forEach((d,i)=>d.classList.toggle('hl',i===hl));}
  else if(e.key==='Enter'){e.preventDefault();pick(items[Math.max(hl,0)]);}
  else if(e.key==='Escape'){res.style.display='none';}});
document.addEventListener('click',e=>{if(!e.target.closest('.dt-search'))res.style.display='none';});
function pick(it){cur=it;res.style.display='none';box.value='';
  try{localStorage.setItem('dt-cur',JSON.stringify(it));}catch(e){}
  history.replaceState(null,'','?iid='+it.instrumentId);
  $('dt-card').style.display='';$('dt-sym').textContent=it.symbol;$('dt-name').textContent=it.name;
  if(typeof autoPaint==='function')autoPaint();data=null;$('dt-px').textContent='–';$('dt-chg').textContent='';$('dt-ba').textContent='';load();if(typeof poll==='function'){paint();poll();}}
// ---- data
function load(){if(!cur)return;clearTimeout(timer);$('dt-status').textContent='Updating…';
  fetch(B+'/daytrader/candles/'+cur.instrumentId+hurdleQ(data&&data.instrumentId===cur.instrumentId?data.kind:null,cur.symbol)).then(r=>{if(!r.ok)throw new Error(r.status);return r.json();})
  .then(d=>{if(!cur||d.instrumentId!==cur.instrumentId)return;data=d;draw();})
  .catch(e=>{$('dt-status').textContent='Update failed ('+e.message+'), retrying';})
  .finally(()=>{next=Date.now()+30000;timer=setTimeout(load,30000);});}
setInterval(()=>{if(cur&&next){const s=Math.max(0,Math.round((next-Date.now())/1000));$('dt-next').textContent='next update in '+s+'s';}},1000);
// ---- chart
const C_LAST='#3b82f6',C_BUY='#f59e0b',C_TGT='#c084fc';
// One SVG chart: candles over [t0,t1], plus horizontal dashed lines. The price scale always
// stretches to include every line, so buy and target levels are never off the chart.
function render(W,H,cs,o){
  const L=8,R=74,T=10,Bm=24,pw=W-L-R,ph=H-T-Bm,t0=o.t0,t1=o.t1,lines=o.lines||[];
  if(!cs.length&&!lines.length)return '<text x=\"12\" y=\"24\">No price data in the last hour.</text>';
  let lo=Infinity,hi=-Infinity;cs.forEach(c=>{lo=Math.min(lo,c.l);hi=Math.max(hi,c.h);});
  lines.forEach(l=>{lo=Math.min(lo,l.v);hi=Math.max(hi,l.v);});
  const pad=(hi-lo)*0.08||hi*0.001||1;lo-=pad;hi+=pad;
  const X=t=>L+(t-t0)/(t1-t0)*pw,Y=v=>T+(hi-v)/(hi-lo)*ph;let g='';
  // price tags on the right, nudged apart so they never sit on top of each other
  const tags=lines.map(l=>({l,y:Y(l.v)})).sort((a,b)=>a.y-b.y);
  for(let k=1;k<tags.length;k++)if(tags[k].y-tags[k-1].y<19)tags[k].y=tags[k-1].y+19;
  const over=tags.length?tags[tags.length-1].y-(T+ph-9):0;if(over>0)tags.forEach(t=>t.y-=over);
  const step=nice((hi-lo)/(H<260?4:5));for(let v=Math.ceil(lo/step)*step;v<=hi;v+=step){const y=Y(v);
    g+='<line x1=\"'+L+'\" x2=\"'+(L+pw)+'\" y1=\"'+y+'\" y2=\"'+y+'\" stroke=\"#262c37\"/>'
     +(tags.some(t=>Math.abs(t.y-y)<14)?'':'<text x=\"'+(L+pw+6)+'\" y=\"'+(y+4)+'\">'+fmt(v)+'</text>');}
  const want=Math.max(2,Math.floor(pw/80)),xs=[300,600,900,1800,3600,7200,10800,21600,43200,86400].find(x=>(t1-t0)/x<=want)||86400;for(let t=Math.ceil(t0/xs)*xs;t<=t1;t+=xs){const x=X(t);
    g+='<line x1=\"'+x+'\" x2=\"'+x+'\" y1=\"'+T+'\" y2=\"'+(T+ph)+'\" stroke=\"#1f242e\"/>'
     +(x-L<18||L+pw-x<18?'':'<text x=\"'+x+'\" y=\"'+(H-7)+'\" text-anchor=\"middle\">'+hm(t)+'</text>');}
  cs.forEach(c=>{const col=c.c>=c.o?'#22c55e':'#ef4444',x0=X(Math.max(c.t,t0)),x1=X(c.t+c.d),
    w=Math.max(1,(x1-x0)*0.7),cx=(x0+x1)/2,yo=Y(c.o),yc=Y(c.c);
    g+='<line x1=\"'+cx+'\" x2=\"'+cx+'\" y1=\"'+Y(c.h)+'\" y2=\"'+Y(c.l)+'\" stroke=\"'+col+'\"/>'
     +'<rect x=\"'+(cx-w/2)+'\" y=\"'+Math.min(yo,yc)+'\" width=\"'+w+'\" height=\"'+Math.max(1,Math.abs(yc-yo))+'\" fill=\"'+col+'\"'
     +(c.d>o.bucket?' fill-opacity=\"0.55\"':'')+'/>';});
  tags.forEach(({l,y})=>{const ly=Y(l.v);
    g+='<line x1=\"'+L+'\" x2=\"'+(L+pw)+'\" y1=\"'+ly+'\" y2=\"'+ly+'\" stroke=\"'+l.c+'\" stroke-dasharray=\"'+(l.dash||'6 4')+'\"/>'
     +(l.label?'<text x=\"'+(L+6)+'\" y=\"'+(ly-5)+'\" style=\"fill:'+l.c+'\">'+l.label+'</text>':'')
     +'<rect x=\"'+(L+pw+2)+'\" y=\"'+(y-9)+'\" width=\"'+(R-4)+'\" height=\"18\" rx=\"4\" fill=\"'+l.c+'\"/>'
     +'<text x=\"'+(L+pw+6)+'\" y=\"'+(y+4)+'\" style=\"fill:#0b0e13;font-weight:600\">'+fmt(l.v)+'</text>';});
  (o.marks||[]).forEach((m,k)=>{if(m.t<t0||m.t>t1)return;const x=X(m.t);
    g+='<line x1=\"'+x+'\" x2=\"'+x+'\" y1=\"'+T+'\" y2=\"'+(T+ph)+'\" stroke=\"'+m.c+'\" stroke-width=\"1.5\" stroke-dasharray=\"3 3\"/>'
     +'<text x=\"'+(x+3)+'\" y=\"'+(T+10+12*(k%3))+'\" style=\"fill:'+m.c+';font-weight:600\">'+m.label+'</text>';});
  if(o.hoverX!=null)g+='<line x1=\"'+X(o.hoverX)+'\" x2=\"'+X(o.hoverX)+'\" y1=\"'+T+'\" y2=\"'+(T+ph)+'\" stroke=\"#8b93a3\" stroke-dasharray=\"2 3\"/>';
  return g;}
// lines for one paper position: its buy price and the break-even target
function posLines(p,many){const tag=many?' ('+hm(p.t)+')':'',be=breakEven(p);
  return [{v:p.ask,c:C_BUY,label:'Paper buy '+fmt(p.ask)+tag},{v:be,c:C_TGT,label:'Target (covers fees) '+fmt(be)+tag}];}
function lastLine(d,q){const cs=d.candles;const v=q&&(d.open||!cs.length)?q.bid:(cs.length?cs[cs.length-1].c:null);
  return v==null?[]:[{v,c:C_LAST,dash:'4 3'}];}
function draw(){
  if(!data)return;
  const q=data.quote,cs=data.candles;
  if(data.symbol&&cur){$('dt-sym').textContent=data.symbol;if(data.name)$('dt-name').textContent=data.name;
    if(cur.symbol!==data.symbol){cur.symbol=data.symbol;cur.name=data.name||cur.name;try{localStorage.setItem('dt-cur',JSON.stringify(cur));}catch(e){}}}
  $('dt-px').textContent=q?fmt(q.bid):'–';
  $('dt-ba').textContent=q?('Bid '+fmt(q.bid)+' · Ask '+fmt(q.ask)+' · Spread '+fmt(q.ask-q.bid)):'';
  const pill=$('dt-live');pill.className='pill '+(data.open?'okp':'warn');
  pill.textContent=data.open?'Live':'Market closed'+(q?' · last price '+hms(q.ts):'');
  $('dt-status').textContent='Updated '+hms(data.serverTime);
  const ch=$('dt-chg');
  if(cs.length&&q){const a=cs[0].o,b=data.open?q.bid:cs[cs.length-1].c,p=(b-a)/a*100;
    ch.textContent=(p>=0?'+':'')+fmt(b-a)+' ('+(p>=0?'+':'')+p.toFixed(2)+'%) over 1 hour';ch.className=p>=0?'dt-up':'dt-dn';}
  else ch.textContent='';
  $('dt-sig').innerHTML=sigBox(data.signal,false,data.symbol||(cur&&cur.symbol));watchSig(data);
  if(typeof posData!=='undefined'){posData[data.instrumentId]=data;if(typeof paint==='function')paint();}}
function nice(x){const p=Math.pow(10,Math.floor(Math.log10(x))),f=x/p;return (f<1.5?1:f<3?2:f<7?5:10)*p;}

// ---- entry signals (rules in app/strategy.py, judged on the server every 30 s)
const SIG={enter:['Entry now','#22c55e','#052e16'],wait:['Wait','#f59e0b','#2a1d05'],avoid:['Avoid','#ef4444','#2a0a0a'],
  fees:['Fees too high','#9ca3af','#1f2430'],closed:['Market closed','#9ca3af','#1f2430'],nodata:['No signal yet','#9ca3af','#1f2430'],
  quiet:['Too quiet','#9ca3af','#1f2430'],paused:['Paused','#a78bfa','#1e1638'],spread:['Spread too wide','#9ca3af','#1f2430']};
const C_ENT='#22c55e';
function hurdleQ(kind,symbol){if(!kind)return '';const p={amt:100,ask:1,kind,symbol:symbol||'',comm:cfg.comm,fx:cfg.fx};
  return '?hurdle='+((breakEven(p)-1)*100).toFixed(3);}
function sigPill(st){const k=SIG[st]||SIG.nodata;return '<span class=sp style=\"color:'+k[1]+';background:'+k[2]+';border:1px solid '+k[1]+'55\">'+k[0]+'</span>';}
function sigBox(sg,compact,sym){if(!sg)return '';
  const nums=sg.entry!=null?('<span>'+(sg.state==='enter'?'Entry':'Buy at')+' <b>'+fmt(sg.entry)+'</b></span><span>Stop <b>'+fmt(sg.stop)+'</b></span><span>Target <b>'+fmt(sg.target)+'</b></span>'
    +'<span>Move to target <b>'+sg.reward_pct+'%</b> vs costs <b>'+sg.hurdle_pct+'%</b></span>'):'';
  if(compact)return '<div class=dt-psig>'+sigPill(sg.state)+'<span>'+sg.headline+'</span>'+(nums?'<div class=nums style=\"display:flex;flex-wrap:wrap;gap:2px 14px;color:var(--muted);margin-top:3px\">'+nums+'</div>':'')+'</div>';
  return '<div class=dt-sig><div class=hd><b>Entry signal</b>'+sigPill(sg.state)+'<span>'+sg.headline+'</span></div>'
    +(nums?'<div class=nums>'+nums+'</div>':'')
    +((sg.reasons||[]).length?'<details><summary>Why</summary><ul>'+sg.reasons.map(r=>'<li>'+r+'</li>').join('')+'</ul>'
      +'<p class=muted style=\"font-size:12px;margin:6px 0 0\">'+(sg.strategy||'')+'. A starting rule set to test with paper trades, not a proven edge.</p></details>':'')+'</div>';}
function entryLine(sg){return sg&&sg.entry!=null&&(sg.state==='enter'||sg.state==='wait')?[{v:sg.entry,c:C_ENT,dash:'2 3',label:(sg.state==='enter'?'Entry now ':'Suggested entry ')+fmt(sg.entry)}]:[];}
// alerts: when any watched symbol's signal changes to "Entry now"
const lastSt={};let alertsOn=false;try{alertsOn=localStorage.getItem('dt-alerts')==='1';}catch(e){}
function watchSig(d){if(!d||!d.signal)return;const was=lastSt[d.instrumentId],now=d.signal.state;lastSt[d.instrumentId]=now;
  if(now==='enter'&&was&&was!=='enter'){const sym=d.symbol||('#'+d.instrumentId),msg=sym+': entry signal at '+fmt(d.signal.entry)+' (stop '+fmt(d.signal.stop)+', target '+fmt(d.signal.target)+')';
    toast(msg);document.title='● '+sym+' entry · TradeAgent';
    if(alertsOn&&'Notification' in window&&Notification.permission==='granted'){try{new Notification('TradeAgent entry signal',{body:msg});}catch(e){}}}}
function toast(msg){const t=document.createElement('div');t.className='dt-toast';t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),15000);t.onclick=()=>t.remove();}
addEventListener('focus',()=>{document.title='Day trader · TradeAgent';});
function alertLink(){const a=$('dt-alerts');if(!a)return;
  a.textContent=alertsOn?'Entry alerts on (turn off)':'Turn on entry alerts';
  a.onclick=e=>{e.preventDefault();if(alertsOn){alertsOn=false;try{localStorage.setItem('dt-alerts','0');}catch(e){}alertLink();return;}
    const done=()=>{alertsOn=true;try{localStorage.setItem('dt-alerts','1');}catch(e){}alertLink();toast('Entry alerts on. You’ll be told here'+(('Notification' in window&&Notification.permission==='granted')?' and by a notification':'')+' while this page is open.');};
    if('Notification' in window&&Notification.permission==='default')Notification.requestPermission().then(done);else done();};}
// ---- paper trades (several can be open at once)
const AMT=100;
const ld=(k,d)=>{try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v);}catch(e){return d;}};
const sv=(k,v)=>{try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}};
let open_=ld('dt-open',null);
if(!Array.isArray(open_)){const one=ld('dt-pos',null);open_=one?[Object.assign({id:'p'+Date.now()},one)]:[];sv('dt-open',open_);sv('dt-pos',null);}
let qs={},ptimer=null,last=null,hist=ld('dt-hist',[]),busy=false;
const cfg=ld('dt-cfg',{comm:1,fx:0});$('dt-comm').value=cfg.comm;$('dt-fx').value=cfg.fx;
$('dt-comm').onchange=$('dt-fx').oninput=()=>{cfg.comm=+$('dt-comm').value;cfg.fx=Math.max(0,+$('dt-fx').value||0);sv('dt-cfg',cfg);paint();};
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const sgn=v=>(v>=0?'+':'-')+'$'+Math.abs(v).toFixed(2);
const cls=v=>v>=0?'dt-up':'dt-dn';
const pc=(v,b)=>{const p=v/b*100;return (p>=0?'+':'')+p.toFixed(2)+'%';};
const dur=s=>{s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+'h ':'')+(h||m?m+'m ':'')+x+'s';};
// eToro cost rules for one round trip: buy `amt` at `ask`, sell at `bid`
function costs(p,bid){const units=p.amt/p.ask,value=units*bid,lines=[];
  if(p.kind==='crypto'){lines.push(['Crypto fee 1% (buy)',p.amt*0.01]);lines.push(['Crypto fee 1% (sell)',value*0.01]);}
  if(p.kind==='stock'&&p.comm>0){lines.push(['Commission (open)',p.comm]);lines.push(['Commission (close)',p.comm]);}
  if(p.kind==='stock'&&/\\.L$/i.test(p.symbol))lines.push(['UK stamp duty 0.5%',Math.ceil(p.amt*0.5)/100]);
  if(p.fx>0){lines.push(['Currency conversion (in)',p.amt*p.fx/100]);lines.push(['Currency conversion (out)',value*p.fx/100]);}
  const fees=lines.reduce((a,l)=>a+l[1],0),gross=value-p.amt;
  return {units,value,gross,lines,fees,net:gross-fees,spread:units*(p.ask-p.bid0)};}
// bid price at which closing gives exactly $0 after every fee (the chart's target line)
function breakEven(p){const units=p.amt/p.ask,r=(p.kind==='crypto'?0.01:0)+(p.fx||0)/100;
  let fixed=0;if(p.kind==='crypto')fixed+=p.amt*0.01;if(p.kind==='stock'&&p.comm>0)fixed+=2*p.comm;
  if(p.kind==='stock'&&/\\.L$/i.test(p.symbol))fixed+=Math.ceil(p.amt*0.5)/100;if(p.fx>0)fixed+=p.amt*p.fx/100;
  return (p.amt+fixed)/(1-r)/units;}
var posData={},ctimer=null;
function loadPosCharts(){clearTimeout(ctimer);const ids=[...new Set(open_.map(p=>p.iid))];
  Promise.all(ids.map(iid=>(data&&data.instrumentId===iid?Promise.resolve(data):fetch(B+'/daytrader/candles/'+iid+hurdleQ((open_.find(p=>p.iid===iid)||{}).kind,(open_.find(p=>p.iid===iid)||{}).symbol)).then(r=>r.ok?r.json():null).catch(()=>null))
    .then(d=>{if(d){posData[iid]=d;watchSig(d);}}))).then(()=>paint()).finally(()=>{if(open_.length)ctimer=setTimeout(loadPosCharts,30000);});}
const getQ=ids=>fetch(B+'/daytrader/quotes?ids='+ids.join(',')).then(r=>{if(!r.ok)throw 0;return r.json();});
function poll(){clearTimeout(ptimer);const ids=[...new Set(open_.map(p=>p.iid).concat(cur?[cur.instrumentId]:[]))];
  if(!ids.length)return;
  getQ(ids).then(d=>{Object.keys(d).forEach(k=>qs[k]=d[k]);paint();}).catch(()=>{})
  .finally(()=>{ptimer=setTimeout(poll,open_.length?5000:30000);});}
function openPos(){if(!cur||busy)return;busy=true;paint();
  getQ([cur.instrumentId]).then(d=>{const q=d[cur.instrumentId];if(!q)throw 0;qs[cur.instrumentId]=q;
    if(!q.open&&!confirm('The market looks closed, so this price is stale. Open anyway?'))return;
    open_.push({id:'p'+Date.now()+Math.random().toString(36).slice(2,6),iid:cur.instrumentId,symbol:cur.symbol,name:cur.name||'',
      kind:(data&&data.instrumentId===cur.instrumentId&&data.kind)||kindOf(cur.type,cur.symbol),amt:AMT,ask:q.ask,bid0:q.bid,t:Date.now()/1000,comm:cfg.comm,fx:cfg.fx});
    sv('dt-open',open_);last=null;draw();poll();loadPosCharts();})
  .catch(()=>alert('Could not get a price from eToro, try again.')).finally(()=>{busy=false;paint();});}
function closePos(id){const p=open_.find(x=>x.id===id);if(!p||busy)return;busy=true;paint();
  getQ([p.iid]).then(d=>{const q=d[p.iid];if(!q)throw 0;qs[p.iid]=q;const c=costs(p,q.bid);
    last=Object.assign({},p,{exit:q.bid,tc:Date.now()/1000},c);
    hist.unshift({symbol:p.symbol,t:p.t,tc:last.tc,ask:p.ask,exit:q.bid,gross:c.gross,fees:c.fees,net:c.net});hist=hist.slice(0,20);
    open_=open_.filter(x=>x.id!==id);sv('dt-open',open_);sv('dt-hist',hist);draw();})
  .catch(()=>alert('Could not get a price from eToro, try again.')).finally(()=>{busy=false;paint();});}
function closeAll(){if(!open_.length||busy)return;if(!confirm('Close all '+open_.length+' paper positions?'))return;busy=true;paint();
  getQ([...new Set(open_.map(p=>p.iid))]).then(d=>{let done=[];
    open_.forEach(p=>{const q=d[p.iid];if(!q)return;qs[p.iid]=q;const c=costs(p,q.bid),tc=Date.now()/1000;
      hist.unshift({symbol:p.symbol,t:p.t,tc,ask:p.ask,exit:q.bid,gross:c.gross,fees:c.fees,net:c.net});done.push(p.id);});
    hist=hist.slice(0,20);open_=open_.filter(p=>!done.includes(p.id));sv('dt-open',open_);sv('dt-hist',hist);last=null;draw();})
  .catch(()=>alert('Could not get prices from eToro, try again.')).finally(()=>{busy=false;paint();});}
function kindOf(t,sym){if(/\\.24-7$/i.test(sym||''))return 'cfd';t=(t||'').toLowerCase();return t.startsWith('crypto')?'crypto':t==='etf'?'etf':t.startsWith('stock')?'stock':'other';}
const KN={crypto:'Crypto',stock:'Share',etf:'ETF',cfd:'24/7 CFD',other:'CFD/other'};
function kv(rows){return '<div class=dt-kv>'+rows.map(r=>'<span'+(r[2]?' class=\"'+r[2]+'\"':'')+'>'+r[0]+'</span><span'+(r[2]?' class=\"'+r[2]+'\"':'')+'>'+r[1]+'</span>').join('')+'</div>';}
function paint(){const el=$('dt-pbody');if(!el)return;const now=Date.now()/1000;
  const ob=$('dt-openbtn');
  if(ob&&cur){const n=open_.filter(p=>p.iid===cur.instrumentId).length;ob.disabled=busy;
    ob.textContent='Open $'+AMT+' paper buy of '+cur.symbol;ob.title=n?n+' already open in '+cur.symbol:'Nothing is sent to eToro';ob.onclick=openPos;}
  let h='';
  if(!open_.length)h='<p class=muted>No open paper trades. Pick a symbol and press <b>Open $'+AMT+' paper buy</b> above. Nothing is ever sent to eToro.</p>';
  else{let tg=0,tn=0;
    h+=open_.map(p=>{const q=qs[p.iid];const c=q?costs(p,q.bid):null;if(c){tg+=c.gross;tn+=c.net;}
      const be=breakEven(p),togo=q?(be-q.bid)/q.bid*100:null;
      return '<div class=dt-pos'+(cur&&cur.instrumentId===p.iid?' style=\"border-color:#f59e0b55\"':'')+'>'
        +'<div class=dt-prow><a href=\"#\" data-show=\"'+p.iid+'\"><b>'+p.symbol+'</b></a><span class=muted>'+KN[p.kind]+' · opened '+hms(p.t)+' · '+dur(now-p.t)+(q&&!q.open?' · market closed':'')+'</span>'
        +(c?'<span class=\"dt-pl '+cls(c.gross)+'\">'+sgn(c.gross)+' <small>'+pc(c.gross,p.amt)+'</small></span>':'<span class=dt-pl>…</span>')+'</div>'
        +sigBox((posData[p.iid]||{}).signal,true)
        +'<div class=dt-stats><div><span>Bought (ask)</span><b>'+fmt(p.ask)+'</b></div><div><span>Now (bid)</span><b>'+(q?fmt(q.bid):'–')+'</b></div>'
        +'<div><span>Target</span><b style=\"color:'+C_TGT+'\">'+fmt(be)+'</b></div>'
        +'<div><span>To target</span><b>'+(togo==null?'–':togo<=0?'<span class=dt-up>reached</span>':'+'+togo.toFixed(2)+'%')+'</b></div>'
        +'<div><span>Costs if closed</span><b>'+(c?money(c.fees):'–')+'</b></div>'
        +'<div><span>Net if closed</span><b class=\"'+(c?cls(c.net):'')+'\">'+(c?sgn(c.net):'–')+'</b></div>'
        +'<div><button class=\"close sm\" data-close=\"'+p.id+'\"'+(busy?' disabled':'')+'>Close</button></div></div></div>';}).join('');
    h+=kv([['Open positions',open_.length+' · '+money(open_.length*AMT)+' invested'],['Total change in value','<span class=\"'+cls(tg)+'\">'+sgn(tg)+'</span>'],
           ['Total net if all closed now','<b class=\"'+cls(tn)+'\">'+sgn(tn)+'</b>','tot']])
      +(open_.length>1?'<button class=\"sec\" id=dt-closeall'+(busy?' disabled':'')+'>Close all</button>':'');}
  el.innerHTML=h;
  const ca=$('dt-closeall');if(ca)ca.onclick=closeAll;
  el.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>closePos(b.dataset.close));
  el.querySelectorAll('[data-show]').forEach(a=>a.onclick=e=>{e.preventDefault();const p=open_.find(x=>String(x.iid)===a.dataset.show);
    if(p&&(!cur||cur.instrumentId!==p.iid))pick({instrumentId:p.iid,symbol:p.symbol,name:p.name});scrollTo({top:0,behavior:'smooth'});});
  // right-hand column: last closed trade and the history
  let sh='';
  if(last){sh+='<div class=dt-lastc><p><b>Closed '+last.symbol+'</b> <span class=muted>held '+dur(last.tc-last.t)+'</span> <a href=\"#\" id=dt-dismiss style=\"float:right\">dismiss</a></p>'
       +'<div class=\"dt-big '+cls(last.net)+'\">'+sgn(last.net)+' <span style=\"font-size:15px\">('+pc(last.net,last.amt)+')</span></div>'
       +'<div class=muted>Final profit/loss after costs</div>'
       +kv([['Bought at (ask)',fmt(last.ask)],['Sold at (bid)',fmt(last.exit)],['Invested',money(last.amt)],['Value at close',money(last.value)],
            ['Price change (incl. spread '+money(last.spread)+')','<span class=\"'+cls(last.gross)+'\">'+sgn(last.gross)+'</span>']]
          .concat(last.lines.length?last.lines.map(l=>[l[0],'-'+money(l[1])]):[['Fees','none for '+KN[last.kind]+'s']])
          .concat([['Total costs','-'+money(last.fees),'tot'],['Final profit/loss','<span class=\"'+cls(last.net)+'\">'+sgn(last.net)+'</span>','tot']]))+'</div>';}
  const tot=hist.reduce((a,x)=>a+x.net,0),wins=hist.filter(x=>x.net>0).length;
  sh+=hist.length?'<div class=tbl><table><tr><th>Symbol</th><th>Closed</th><th>Held</th><th>Costs</th><th>Net</th></tr>'
    +hist.map(x=>'<tr><td>'+x.symbol+'</td><td>'+hm(x.tc)+'</td><td>'+dur(x.tc-x.t)+'</td><td>'+money(x.fees)+'</td><td class=\"'+cls(x.net)+'\">'+sgn(x.net)+'</td></tr>').join('')
    +'</table></div><p class=muted>'+hist.length+(hist.length===1?' trade':' trades')+' · '+wins+' in profit · total net: <b class=\"'+cls(tot)+'\">'+sgn(tot)+'</b> · <a href=\"#\" id=dt-clear>clear</a></p>'
    :(last?'':'<p class=muted>No closed paper trades yet.</p>');
  $('dt-hist').innerHTML=sh;
  const dm=$('dt-dismiss');if(dm)dm.onclick=e=>{e.preventDefault();last=null;paint();};
  const cl=$('dt-clear');if(cl)cl.onclick=e=>{e.preventDefault();if(confirm('Clear the closed paper trade history?')){hist=[];sv('dt-hist',hist);paint();}};}
setInterval(()=>{if(open_.length)paint();},1000);
addEventListener('resize',()=>{paint();if(typeof gridPaint==='function')gridPaint();});
alertLink();
// ---- strategy auto-test (server side)
let auto=null,atimer=null;
function autoLoad(){clearTimeout(atimer);fetch(B+'/daytrader/autotest').then(r=>r.ok?r.json():null).then(d=>{if(d){const first=!auto;auto=d;autoPaint();if(first||!gtimer)gridLoad();else gridPaint();}})
  .catch(()=>{}).finally(()=>{atimer=setTimeout(autoLoad,30000);});}
function autoPost(path,body){return fetch(B+'/daytrader/autotest/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.ok?r.json():r.json().then(e=>{throw new Error(e.detail||r.status);},()=>{throw new Error(r.status);}))
  .then(d=>{auto=d;autoPaint();if(typeof gridLoad==='function')gridLoad();}).catch(e=>toast('Auto-test: '+e.message));}
// search box in the auto-test card
(()=>{const q=$('dt-aq'),res=$('dt-ares');if(!q)return;let st=null,items=[],hl=-1;
  const add=it=>{res.style.display='none';q.value='';
    if(auto&&auto.symbols.some(x=>x.iid===it.instrumentId)){toast(it.symbol+' is already on the test list.');return;}
    autoPost('add',{iid:it.instrumentId}).then(()=>{if(auto&&auto.symbols.some(x=>x.iid===it.instrumentId))toast(it.symbol+' added to the auto-test.');});};
  q.addEventListener('input',()=>{clearTimeout(st);const v=q.value.trim();if(!v){res.style.display='none';return;}
    st=setTimeout(()=>fetch(B+'/daytrader/search?q='+encodeURIComponent(v)).then(r=>r.json()).then(list=>{items=list;hl=-1;
      res.innerHTML=list.length?'':'<div><span>No eToro instruments match</span></div>';
      list.forEach(it=>{const d=document.createElement('div');const there=auto&&auto.symbols.some(x=>x.iid===it.instrumentId);
        d.innerHTML='<b></b><span></span>';d.querySelector('b').textContent=it.symbol;
        d.querySelector('span').textContent=it.name+' · '+it.type+(there?' · already on the list':'');d.onclick=()=>add(it);res.appendChild(d);});
      res.style.display='block';}).catch(()=>{}),250);});
  q.addEventListener('keydown',e=>{const ds=res.querySelectorAll('div');if(!items.length)return;
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();hl=(hl+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;ds.forEach((d,i)=>d.classList.toggle('hl',i===hl));}
    else if(e.key==='Enter'){e.preventDefault();add(items[Math.max(hl,0)]);}else if(e.key==='Escape')res.style.display='none';});
  document.addEventListener('click',e=>{if(!e.target.closest('#dt-ares')&&e.target!==q)res.style.display='none';});})();
const REASON={stop:'Stop',target:'Target','break-even':'Break-even'};
const rx=v=>v==null?'–':(v>=0?'+':'')+v.toFixed(2)+'R';
function autoPaint(){const el=$('dt-auto');if(!el||!auto)return;const now=Date.now()/1000,S=auto.strategies||{fast:{label:'Fast'},swing:{label:'Swing'}};
  const tbl=(head,rows)=>'<div class=tbl><table><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr>'+rows.join('')+'</table></div>';
  const lab=k=>(S[k]||{label:k}).label;
  let h='';
  Object.keys(S).forEach(k=>{const T=(auto.totals||{})[k]||{trades:0,wins:0,targets:0,stops:0,gross:0,fees:0,net:0};
    h+='<div class=dt-strat>'+lab(k)+' <span class=muted style=\"font-weight:400\">· closes at stop, target or after '+S[k].max_hours+'h</span></div>'
      +'<div class=dt-tiles><div><span>Closed trades</span><b>'+T.trades+'</b></div>'
      +'<div><span>In profit after fees</span><b>'+(T.trades?Math.round(T.wins/T.trades*100)+'%':'–')+'</b></div>'
      +'<div><span>Target / stop / time</span><b>'+T.targets+' / '+T.stops+' / '+(T.trades-T.targets-T.stops)+'</b></div>'
      +'<div><span>Price moves</span><b class=\"'+cls(T.gross)+'\">'+sgn(T.gross)+'</b></div>'
      +'<div><span>Fees paid</span><b>'+money(T.fees)+'</b></div>'
      +'<div><span>Total net</span><b class=\"'+cls(T.net)+'\">'+sgn(T.net)+'</b></div>'
      +'<div><span>Total R (average)</span><b class=\"'+cls(T.total_r||0)+'\">'+(T.avg_r==null?'–':rx(T.total_r)+' ('+rx(T.avg_r)+')')+'</b></div></div>';});
  el.innerHTML=h;h='';
  const inList=cur&&auto.symbols.some(x=>x.iid===cur.instrumentId);
  const sg=(st,hd)=>st?sigPill(st==='error'?'nodata':st)+'<span class=muted>'+(hd||'')+'</span>':'<span class=muted>waiting for the first check</span>';
  const on=v=>v===undefined||v===null||+v===1;
  const KL={crypto:'crypto · 2% fees',cfd:'24/7 CFD · spread only',stock:'share',etf:'ETF',other:'other'};
  let lh=(cur&&!inList?'<button class=sec id=dt-aadd style=\"margin:8px 0\">Add '+cur.symbol+' (selected above)</button>':'')
    +(auto.symbols.length?auto.symbols.map(x=>'<div class=dt-arow><div class=sym><b>'+x.symbol+'</b><span>'+(x.name?x.name+' · ':'')+(KL[x.kind]||x.kind)+'</span>'
      +'<span>'+(x.last_checked?'checked '+hm(Date.parse(x.last_checked)/1000):'not checked yet')+'</span></div>'
      +'<div class=sg>'+Object.keys(S).map(k=>{const st=k==='fast'?x.last_state:x[k+'_state'],hd=k==='fast'?x.last_headline:x[k+'_headline'];
          return '<div><i>'+(S[k].short||k)+'</i>'+(on(x[k+'_on'])?sg(st,hd):'<span class=muted>off</span>')+'</div>';}).join('')+'</div>'
      +'<div class=ctl>'+Object.keys(S).map(k=>'<label class=dt-sw><input type=checkbox data-tog=\"'+k+'\" data-iid=\"'+x.iid+'\"'+(on(x[k+'_on'])?' checked':'')+'>'+(S[k].short||k)+'</label>').join('')
      +'<button class=sec data-arm=\"'+x.iid+'\" title=\"Stop testing '+x.symbol+'\">Remove</button></div></div>').join('')
    :'<p class=muted>The test list is empty. Search above to add a symbol.</p>');
  $('dt-alist').innerHTML='<p class=muted style=\"margin:10px 0 0\">'+auto.symbols.length+' symbol'+(auto.symbols.length===1?'':'s')+' on the list</p>'+lh;
  h+='<h3>Open auto-test trades ('+auto.open.length+')</h3>'+(auto.open.length?tbl(['Symbol','Strategy','Opened','Entry (ask)','Stop','Target','Now (bid)','Net if closed'],
      auto.open.map(t=>'<tr><td>'+t.symbol+'</td><td>'+t.strategy+'</td><td>'+hm(t.opened_ts)+' · '+dur(now-t.opened_ts)+'</td><td>'+fmt(t.entry)+'</td><td>'+fmt(t.stop)+'</td><td>'+fmt(t.target)+'</td><td>'
        +(t.last_bid?fmt(t.last_bid):'–')+'</td><td class=\"'+(t.net!=null?cls(t.net):'')+'\">'+(t.net!=null?sgn(t.net):'–')+'</td></tr>'))
    :'<p class=muted>None open. One opens as soon as a tested symbol signals Entry now.</p>');
  if(auto.by_symbol.length)h+='<h3>Results by symbol</h3>'+tbl(['Symbol','Strategy','Trades','In profit','Price moves','Fees','Net'],auto.by_symbol.map(b=>'<tr><td>'+b.symbol+'</td><td>'+b.strategy+'</td><td>'+b.trades+'</td><td>'
      +Math.round(b.wins/b.trades*100)+'%</td><td class=\"'+cls(b.gross)+'\">'+sgn(b.gross)+'</td><td>'+money(b.fees)+'</td><td class=\"'+cls(b.net)+'\">'+sgn(b.net)+'</td></tr>'));
  h+='<h3>Closed auto-test trades</h3>'+(auto.closed.length?tbl(['Symbol','Strategy','Opened','Held','Exit','Entry → exit','Fees','Net','R','Best'],auto.closed.map(t=>'<tr><td>'+t.symbol+'</td><td>'+t.strategy+'</td><td>'
      +new Date(t.opened_ts*1000).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+'</td><td>'+dur(t.closed_ts-t.opened_ts)+'</td><td>'
      +(REASON[t.reason]||t.reason)+'</td><td>'+fmt(t.entry)+' → '+fmt(t.exit)+'</td><td>'+money(t.fees)+'</td><td class=\"'+cls(t.net)+'\">'+sgn(t.net)+'</td><td class=\"'+(t.r!=null?cls(t.r):'')+'\">'+rx(t.r)+'</td><td>'+rx(t.mfe_r)+'</td></tr>'))
    :'<p class=muted>None yet.</p>');
  $('dt-arest').innerHTML=h;
  const ab=$('dt-aadd');if(ab)ab.onclick=()=>autoPost('add',{iid:cur.instrumentId});
  $('dt-alist').querySelectorAll('[data-tog]').forEach(c=>c.onchange=()=>autoPost('toggle',{iid:+c.dataset.iid,strategy:c.dataset.tog,on:c.checked}));
  $('dt-alist').querySelectorAll('[data-arm]').forEach(a=>a.onclick=e=>{e.preventDefault();const x=auto.symbols.find(s=>String(s.iid)===a.dataset.arm);
    if(confirm('Stop auto-testing '+(x?x.symbol:'this symbol')+'?'+(auto.open.some(t=>String(t.iid)===a.dataset.arm)?' Its open trades will be closed at the last price.':'')))autoPost('remove',{iid:+a.dataset.arm});});}

autoLoad();
// ---- chart per tested symbol: trade window (entry-15m .. exit+15m) or the last 12 hours
const gdata={};let gtimer=null;
const GC={entry:'#f59e0b',target:'#34d399',stop:'#f87171',exit:'#c084fc'};
function gridLoad(){clearTimeout(gtimer);const syms=(auto&&auto.symbols)||[];let i=0;
  const next=()=>{if(i>=syms.length){gtimer=setTimeout(gridLoad,60000);return;}const x=syms[i++];
    fetch(B+'/daytrader/autotest/chart/'+x.iid).then(r=>r.ok?r.json():null).then(d=>{if(d){gdata[x.iid]=d;gridPaint();}}).catch(()=>{}).finally(()=>setTimeout(next,400));};
  next();}
function gridPaint(){const el=$('dt-agrid');if(!el||!auto)return;const S=auto.strategies||{},short=k=>(S[k]&&S[k].short)||k,now=Date.now()/1000;
  el.innerHTML=auto.symbols.map(x=>{const d=gdata[x.iid];let st='<span class=muted>loading…</span>';
    if(d){if(d.mode==='trade')st=d.trades.map(t=>'<b style=\"color:'+(t.status==='open'?GC.entry:GC.exit)+'\">'+short(t.strategy)+'</b> '
        +(t.status==='open'?'open since '+hm(t.opened_ts)+' ('+dur(now-t.opened_ts)+')':(t.reason==='target'?'reached its target':t.reason==='stop'?'hit its stop':'closed ('+t.reason+')')+' at '+hm(t.closed_ts))
        +(t.net!=null?' · net <span class=\"'+cls(t.net)+'\">'+sgn(t.net)+'</span>':'')).join('<br>');
      else st='No trade running · last 12 hours'+(d.candles.length?' · '+(((d.candles[d.candles.length-1].c/d.candles[0].o)-1)*100).toFixed(2)+'%':'');}
    return '<div class=\"dt-gc'+(d&&d.mode==='trade'?' run':'')+'\"><div class=hd><b>'+x.symbol+'</b></div><div class=st>'+st+'</div><svg data-g=\"'+x.iid+'\"></svg></div>';}).join('')
    ||'<p class=muted>Add symbols to the test list to see their charts.</p>';
  el.querySelectorAll('svg[data-g]').forEach(svg=>{const d=gdata[svg.dataset.g];const W=svg.clientWidth||300,H=svg.clientHeight||170;
    svg.setAttribute('viewBox','0 0 '+W+' '+H);if(!d){svg.innerHTML='';return;}
    const lines=[],marks=[],many=d.trades.length>1;
    d.trades.forEach(t=>{const tag=many?' '+short(t.strategy):'';
      lines.push({v:t.entry,c:GC.entry,label:'Entry'+tag},{v:t.target,c:GC.target,label:'Target'},{v:t.stop,c:GC.stop,label:'Stop'});
      marks.push({t:t.opened_ts,c:GC.entry,label:'In'+tag});
      if(t.status==='closed'){lines.push({v:t.exit,c:GC.exit,dash:'2 2'});marks.push({t:t.closed_ts,c:GC.exit,label:'Out'+tag});}});
    const last=d.trades.some(t=>t.status==='open')&&d.last_bid?d.last_bid:(d.candles.length?d.candles[d.candles.length-1].c:null);
    if(last!=null)lines.push({v:last,c:C_LAST,dash:'4 3'});
    svg.innerHTML=render(W,H,d.candles,{t0:d.t0,t1:Math.max(d.t1,d.t0+600),bucket:d.step,lines,marks});});}
if(open_.length)loadPosCharts();
// ---- start: ?iid= in the address, else the last symbol viewed
let saved=null;try{saved=JSON.parse(localStorage.getItem('dt-cur')||'null');}catch(e){}
const want=new URLSearchParams(location.search).get('iid');
if(!want&&!saved&&open_.length)saved={instrumentId:open_[0].iid,symbol:open_[0].symbol,name:open_[0].name};
if(saved&&(!want||String(saved.instrumentId)===want))pick(saved);
else if(want)pick({instrumentId:+want,symbol:'#'+want,name:''});
})();
"""


@app.get("/daytrader", response_class=HTMLResponse)
def daytrader_page():
    search = card("Day trader (test)",
                  "<p class=muted>Pick any eToro instrument to see its live price, an entry signal, and open theoretical $100 paper trades, "
                  "as many at once as you like. Nothing is ever sent to eToro from this page. Each open trade gets its own "
                  "live 1-hour candle chart: eToro's smallest candle is 1 minute, so it starts with 1-minute candles (paler) "
                  "and 30-second candles, built from the live price sampled every 5 seconds, take over while the page is open.</p>"
                  "<div class=dt-search><label>Symbol or name</label>"
                  "<input id=dt-q autocomplete=off placeholder='e.g. NVDA, Tesla, BTC, IUCS.L'>"
                  "<div class=dt-results id=dt-results></div></div>")
    chart = ("<div class=layout><section class='card dt-chartcard' id=dt-card style='display:none'>"
             "<div class=dt-head><b id=dt-sym style='font-size:18px'></b><span class=muted id=dt-name></span>"
             "<span class=pill id=dt-live></span><button id=dt-openbtn>Open $100 paper buy</button></div>"
             "<div class=dt-head><span class=dt-px id=dt-px>–</span><span id=dt-chg></span>"
             "<span class=muted id=dt-ba></span></div>"
             "<div id=dt-sig></div>"
             "<div class=dt-head><span class=muted id=dt-status></span><span class=muted>· <span id=dt-next></span></span>"
             "<a href='#' id=dt-alerts style='margin-left:auto;font-size:13px'>Turn on entry alerts</a></div>"
             "</section>"
             "<section class='card dt-paper' id=dt-paper><h2>Open paper trades (theoretical)</h2><div id=dt-pbody></div></section>"
             "<section class='card dt-side'><h2>Closed paper trades</h2><div id=dt-hist></div>"
             "<details style='margin-top:12px'><summary>Cost assumptions</summary>"
             "<div class=dt-set><div><label>Share commission per side</label><select id=dt-comm>"
             "<option value=0>$0</option><option value=1>$1</option><option value=2>$2</option></select></div>"
             "<div><label>Currency conversion % per side</label><input id=dt-fx type=number min=0 max=3 step=0.05></div></div>"
             "<p class=muted>Buys fill at the <b>ask</b> and sells at the <b>bid</b>, so the spread is in the price. "
             "Crypto: 1% fee to buy and 1% to sell. Shares: commission per side (eToro charges $1 or $2 by country and "
             "exchange; check the estimated cost on eToro's trade screen and set it here), plus 0.5% UK stamp duty on buys "
             "of London-listed shares. ETFs and 24/7 CFDs (<code>.24-7</code> symbols): no commission, just the spread; CFD overnight fees are not included. Currency conversion applies only if your account currency "
             "differs from the asset's; 0 for a USD account trading USD assets. "
             "The <b>target</b> line is the price at which selling would exactly cover all of these costs.</p>"
             "</details></section>"
             "<section class='card dt-auto'><h2>Strategy auto-test (runs on the server, 24/7)</h2>"
             "<p class=muted>Every minute the server checks each symbol below against four versions of the entry strategy: "
             "<b>Fast</b> (5-minute trend, 1-minute timing); <b>Fast v2</b> (the same, but it skips quiet markets, needs the "
             "1-hour trend up too, sets stops at least 0.3% away measured from the price paid, wants the reward after costs to beat "
             "the risk, trades 24/7 CFDs only in their home market's hours, and cools off for 30 minutes after a stop-out, "
             "pausing for the day after two); <b>Fast v3</b> (Fast v2 with a nearer 1.5:1 target, and the stop moved up to the "
             "entry price once the trade has gone 1× its risk in profit); and <b>Swing</b> (1-hour trend, 15-minute timing, stops "
             "and targets several times wider, so moves can pay crypto's 2% fees, measured from the price paid). Fast v2, Fast v3 "
             "and Swing skip trades where the spread is more than 15% of the stop distance. <b>Fast</b> is left unchanged as the "
             "baseline. On every <b>Entry now</b> it opens a theoretical $100 trade at the ask "
             "(one per symbol per strategy) and closes it at the signal's stop or target, or at the bid after 4 hours (the fast "
             "versions) or 48 hours (swing). Costs as above. <b>R</b> is the net result divided by the trade's initial risk (the "
             "net loss had it hit its first stop); <b>Best</b> is the furthest the price went in the trade's favour, in R. "
             "Nothing is sent to eToro. "
             "This keeps running with the page closed, so the results below show whether the signals make money after fees.</p>"
             "<div id=dt-auto>Loading…</div>"
             "<h3>Charts</h3><div class=dt-key><span><i style='color:#f59e0b'></i>Entry</span><span><i style='color:#34d399'></i>Target</span>"
             "<span><i style='color:#f87171'></i>Stop</span><span><i style='color:#c084fc'></i>Exit</span><span><i style='color:#3b82f6'></i>Current price</span></div>"
             "<div id=dt-agrid class=dt-grid4></div>"
             "<h3>Test list</h3><div class=dt-search style='max-width:none'><label>Add a symbol (any eToro instrument)</label>"
             "<input id=dt-aq autocomplete=off placeholder='Search by symbol or name, e.g. ETH, XRP, AMD.24-7'>"
             "<div class=dt-results id=dt-ares></div></div>"
             "<p class=muted style='font-size:12px;margin:6px 0 0'>Switch a strategy off to stop that strategy "
             "opening new trades in a symbol (an open trade still runs to its stop, target or time limit). "
             "<b>Remove</b> takes the symbol off the list and closes its open trades at the last price.</p>"
             "<div id=dt-alist></div><div id=dt-arest></div></section></div>")
    html_ = page("Day trader", f"<style>{DT_CSS}</style>" + search + chart, active="Day trader").body.decode()
    html_ = html_.replace("<body>", f"<body data-root='{esc(B)}'>", 1).replace("</body>", f"<script>{DT_JS}</script></body>", 1)
    return HTMLResponse(html_)


@app.get("/daytrader/search")
def daytrader_search(q: str = ""):
    try:
        return daytrader.search(q)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro search failed: {str(e)[:200]}")


@app.get("/daytrader/quotes")
def daytrader_quotes(ids: str = ""):
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        return {str(k): v for k, v in daytrader.quotes(wanted).items()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro prices failed: {str(e)[:200]}")


@app.get("/daytrader/autotest")
def daytrader_autotest():
    return autotest.summary()


@app.get("/daytrader/autotest/chart/{iid}")
def daytrader_autotest_chart(iid: int):
    try:
        return autotest.chart(iid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro price data failed: {str(e)[:200]}")


@app.post("/daytrader/autotest/add")
async def daytrader_autotest_add(request: Request):
    b = await request.json()
    try:
        iid = int(b["iid"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "iid required")
    try:
        autotest.add_symbol(iid)                 # symbol, name and cost type looked up on eToro
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro lookup failed: {str(e)[:200]}")
    return autotest.summary()


@app.post("/daytrader/autotest/toggle")
async def daytrader_autotest_toggle(request: Request):
    b = await request.json()
    try:
        autotest.set_enabled(int(b["iid"]), str(b["strategy"]), bool(b["on"]))
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"iid, strategy and on required ({e})")
    return autotest.summary()


@app.post("/daytrader/autotest/remove")
async def daytrader_autotest_remove(request: Request):
    b = await request.json()
    try:
        autotest.remove_symbol(int(b["iid"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "iid required")
    return autotest.summary()


@app.get("/daytrader/quote/{iid}")
def daytrader_quote(iid: int):
    try:
        return daytrader.quote(iid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro price failed: {str(e)[:200]}")


@app.get("/daytrader/candles/{iid}")
def daytrader_candles(iid: int, hurdle: float | None = None):
    try:
        return daytrader.chart(iid, hurdle)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"eToro price data failed: {str(e)[:200]}")


@app.post("/cancel/{pid}")
def cancel_queued(pid: int):
    r = engine.cancel(pid, "dashboard")
    if r.get("error"):
        store.log("warn", f"cancel of proposal #{pid} ignored: {r['error']} (status {r.get('status')})")
    return back()


@app.get("/preflight", response_class=HTMLResponse)
def preflight_page(symbol: str):
    item, listed = _analysable(symbol)
    off = ("" if listed else f"<p class=muted><b>{esc(item.symbol)} is not on your watchlist</b>; it's here because one of its "
           f"watch levels is still active. This runs a one-off analysis; <a href='{B}/settings'>add it to the watchlist</a> "
           f"to include it in scheduled runs.</p>")
    try:
        r = engine.preflight(item)
    except Exception as e:  # noqa: BLE001
        return page("Check", f"<h1>{esc(item.symbol)}</h1>{off}<div class=card><p class=muted>Couldn't check the limits: {esc(str(e))}</p>"
                    f"{run_form(item.symbol, 'Analyse anyway')}</div>", active="Overview")
    rows = ""
    for side in r["sides"]:
        if side["ok"]:
            what = (f"<span class='pill okp'>allowed</span> up to ${side['amount']:,.2f}" if side.get("amount") and side["label"] == "Buy"
                    else "<span class='pill okp'>allowed</span>")
            detail = ""
        else:
            what = "<span class='pill on'>blocked</span>"
            detail = ("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in side["blocks"]) + "</ul>"
                      + ("<p><b>To allow it:</b></p><ul>" + "".join(f"<li>{esc(f)}</li>" for f in side["fixes"]) + "</ul>"
                         if side["fixes"] else ""))
        rows += f"<tr><td><b>{esc(side['label'])}</b></td><td>{what}{detail}</td></tr>"
    notes = ""
    if r["mode"] == "signals":
        notes += "<p class=muted>Mode is Signals only: even an allowed trade is only suggested, never placed.</p>"
    if not r["market_open"]:
        notes += "<p class=muted>The market is closed now, so an approved trade would queue until it opens.</p>"
    if not any(s["ok"] for s in r["sides"]):
        notes += "<p><b>As things stand, no trade for this share could happen</b>, whatever the agents say. Change the limits above first, or analyse for the research only.</p>"
    return page("Check", f"<h1>{esc(item.symbol)} <span class=muted>can a trade happen now?</span></h1>{off}"
                f"<div class=card><table>{rows}</table>{notes}</div>"
                f"<div class=card style='margin-top:16px'>{run_form(item.symbol, 'Run analysis now')}"
                f"<p class=muted>Costs roughly $0.50–$3 and takes several minutes. "
                f"<a href='{B}/settings'>Open Settings</a> to change limits, then check again.</p></div>", active="Overview")


def run_form(symbol: str, label: str) -> str:
    return f"<form method=post action='{B}/run'><input type=hidden name=symbol value='{esc(symbol)}'><button>{esc(label)}</button></form>"


def _analysable(symbol: str):
    """(WatchItem, on_watchlist) for a manual analysis: a watchlist symbol, or one that is off the watchlist
    but still has an active watch level (built the same way the watchlist editor builds an entry)."""
    from . import levels
    sym = symbol.strip().upper()
    items = {i.symbol: i for i in policy.parse_watchlist(store.get_settings()["watchlist"])}
    if sym in items:
        return items[sym], True
    if not levels.active(sym):
        raise HTTPException(404, "not in watchlist")
    try:
        inst = engine.client().find_instrument(sym)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{sym} could not be found on eToro: {str(e)[:120]}")
    return policy.parse_watchlist(_entry_for(sym, "", inst))[0], False


@app.post("/run")
def run_now(symbol: str = Form(...)):
    item, _ = _analysable(symbol)
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


def run_trade_actions(run_id: int, p: dict) -> str:
    """Manual controls under a suggested trade: execute a signal, or approve/reject a pending one."""
    if p["status"] == "blocked" and p["action"] != "none":
        why = p.get("error") or p.get("reason") or ""
        if "kill switch" in why:
            return "<p class=muted>Blocked by the kill switch. Turn it off in Settings first; it can't be overridden from here.</p>"
        live = "TEST MODE: nothing will be sent to eToro" if config.DRY_RUN else f"This places a real order on your {config.ETORO_ENV.upper()} eToro account"
        ask = esc(f"OVERRIDE your limits for this one trade? {engine._describe(p)}. Waived: {why}. {live}. "
                  f"The kill switch, closed markets, the per-trade maximum and your actual cash still apply.").replace("'", "&#39;")
        return (f"<p class=muted>Blocked by your limits. You can override them for this one trade.</p>"
                f"<div class=row style='margin:4px 0 10px'><form method=post action='{B}/run/{run_id}/override/{p['id']}' "
                f"style='display:inline' onsubmit=\"return confirm('{ask}')\">"
                f"<button class=red>Override limits and execute: {esc(engine._describe(p))}</button></form></div>")
    if p["status"] not in ("signal", "pending") or p["action"] == "none":
        return ""
    live = "TEST MODE: nothing will be sent to eToro" if config.DRY_RUN else f"This places a real order on your {config.ETORO_ENV.upper()} eToro account"
    ask = esc(f"{engine._describe(p)}. {live}. Every limit is re-checked first. Continue?").replace("'", "&#39;")
    label = "Approve and execute" if p["status"] == "pending" else "Execute this trade"
    out = (f"<form method=post action='{B}/run/{run_id}/execute/{p['id']}' style='display:inline' "
           f"onsubmit=\"return confirm('{ask}')\"><button>{label}: {esc(engine._describe(p))}</button></form>")
    if p["status"] == "pending":
        out += (f" <form method=post action='{B}/run/{run_id}/reject/{p['id']}' style='display:inline'>"
                f"<button class=sec>Reject</button></form>")
    return f"<div class=row style='margin:4px 0 10px'>{out}</div>"


@app.post("/run/{run_id}/execute/{pid}")
def run_execute(run_id: int, pid: int, request: Request):
    p = store.get_proposal(pid)
    if not p or p["run_id"] != run_id:
        raise HTTPException(404)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    res = engine.manual_execute(pid, actor=f"dashboard from {ip or 'unknown IP'}")
    if res.get("error"):
        store.log("warn", f"manual execute of proposal #{pid} refused: {res['error']} (status {res.get('status')})")
    return back(f"/run/{run_id}")


@app.post("/run/{run_id}/override/{pid}")
def run_override(run_id: int, pid: int, request: Request):
    p = store.get_proposal(pid)
    if not p or p["run_id"] != run_id:
        raise HTTPException(404)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    res = engine.override_execute(pid, actor=f"dashboard from {ip or 'unknown IP'}")
    if res.get("error"):
        store.log("warn", f"override of proposal #{pid} refused: {res['error']} (status {res.get('status')})")
    return back(f"/run/{run_id}")


@app.post("/run/{run_id}/reject/{pid}")
def run_reject(run_id: int, pid: int):
    p = store.get_proposal(pid)
    if not p or p["run_id"] != run_id:
        raise HTTPException(404)
    engine.decide(pid, approve=False, actor="dashboard")
    return back(f"/run/{run_id}")


@app.get("/run/{run_id}", response_class=HTMLResponse)
def show_run(run_id: int):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404)
    props = store.proposals_for_run(run_id)
    if props:
        trade = "".join(f"<p><b>{esc(engine._describe(p)) if p['action'] != 'none' else 'No trade'}</b> "
                        f"{status_pill(p['status'])} <span class=muted>{esc((p['error'] or p['reason'] or '')[:200])}</span></p>"
                        f"{run_trade_actions(run_id, p)}"
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
