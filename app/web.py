"""Dashboard, approval pages and the 24x7 scheduler."""
import html
import hmac
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, engine, policy, store

sched = BackgroundScheduler(timezone=config.TZ)


def _hm(v: str, default: str) -> tuple[int, int]:
    try:
        h, m = (v or default).split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def schedule_jobs() -> None:
    s = store.get_settings()
    h, m = _hm(s["schedule_time"], "15:05")
    ch, cm = _hm(s["crypto_time"], "09:05")
    sched.add_job(engine.scheduled_analysis, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
                  id="stocks", replace_existing=True, kwargs={"asset": "stock"}, max_instances=1)
    sched.add_job(engine.scheduled_analysis, CronTrigger(hour=ch, minute=cm), id="crypto",
                  replace_existing=True, kwargs={"asset": "crypto"}, max_instances=1)
    sched.add_job(engine.process_queue, "interval", minutes=5, id="queue", replace_existing=True, max_instances=1)
    sched.add_job(engine.daily_summary, CronTrigger(day_of_week="mon-fri", hour=21, minute=30),
                  id="summary", replace_existing=True)


@asynccontextmanager
async def lifespan(_app):
    store.init()
    schedule_jobs()
    sched.start()
    store.log("info", f"started ({config.ETORO_ENV.upper()} account{', DRY RUN' if config.DRY_RUN else ''})")
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
@media (max-width:1280px){.c3,.c4{grid-column:span 6}.c8,.c9{grid-column:span 12}}
@media (max-width:760px){
 header{padding:10px 12px 0;gap:8px}nav{margin-left:0;width:100%;overflow-x:auto}nav a{padding:10px 12px;flex:1;text-align:center}
 main{padding:12px}.layout{gap:12px}.c3,.c4,.c6,.c8,.c9,.c12{grid-column:span 12}
 input,select,textarea{font-size:16px}.big{font-size:24px}
}
"""

NAV = [("Overview", "/"), ("Settings", "/settings")]


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
              "expired": "", "rejected": "", "signal": "demo"}


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
        acts = f"<span class=muted>{waiting}</span>"
    return (f"<tr><td>#{p['id']}</td><td><b>{esc(engine._describe(p))}</b><br>"
            f"<span class=muted>{esc(p['rating'] or '')}{' · ' + why if why else ''}</span></td>"
            f"<td>{status_pill(p['status'])}<br><span class=muted>{time_left(p, s)}</span></td><td>{acts}</td></tr>")


def page(title: str, body: str, refresh: int = 0, active: str = "", nav: bool = True) -> HTMLResponse:
    s = store.get_settings()
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    pills, banner = status_strip(s)
    links = "".join(f"<a href='{B}{u}' class='{'active' if n == active else ''}'>{n}</a>" for n, u in NAV) if nav else ""
    head = (f"<header><a class=brand href='{B}/'>TradeAgent</a><div class=pills>{pills}</div>"
            f"{'<nav>' + links + '</nav>' if nav else ''}</header>")
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><meta name=theme-color content='#0b0e13'>{meta}"
                        f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{esc(title)} · TradeAgent</title>"
                        f"<style>{CSS}</style></head><body>{head}{banner}<main>{body}</main></body></html>")


def card(title: str, inner: str, cls: str = "c12", right: str = "") -> str:
    r = f"<span class=right>{right}</span>" if right else ""
    return f"<section class='card {cls}'><h2>{title}{r}</h2>{inner}</section>"


def table(rows: str, head: str = "") -> str:
    return f"<div class=tbl><table>{head}{rows}</table></div>"


@app.get("/health")
def health():
    return {"ok": True, "env": config.ETORO_ENV, "mode": store.get_settings()["mode"]}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    s = store.get_settings()
    try:
        book = engine.client().book()
        rows = "".join(f"<tr><td><b>{esc(h.symbol)}</b></td><td>${h.value_usd:,.2f}</td>"
                       f"<td>{h.value_usd / book.total_usd * 100 if book.total_usd else 0:.0f}%</td><td>{h.units:.6g}</td>"
                       f"<td>{'$%.2f' % h.pending_usd if h.pending_usd else ''}</td></tr>"
                       for h in sorted(book.holdings.values(), key=lambda h: -h.value_usd))
        port = (f"<div class=stat><div><div class=muted>Total</div><div class=big>${book.total_usd:,.2f}</div></div>"
                f"<div><div class=muted>Cash</div><div class=big>${book.cash_usd:,.2f}</div></div></div>"
                + table(rows, "<tr><th>Holding</th><th>Value</th><th>Share</th><th>Units</th><th>Pending buys</th></tr>"))
    except Exception as e:  # noqa: BLE001
        port = f"<p class=muted>Portfolio unavailable: {esc(str(e))}</p>"

    pend = store.proposals("pending", 20) + store.proposals("approved", 20)
    prow = "".join(queue_row(p, s) for p in pend) or "<tr><td class=muted>Nothing waiting.</td></tr>"
    hist = "".join(
        f"<tr><td>#{p['id']}</td><td>{p['created'][:16].replace('T', ' ')}</td><td><b>{esc(p['ticker'])}</b></td><td>{esc(p['rating'] or '')}</td>"
        f"<td>{esc(engine._describe(p)) if p['action'] != 'none' else ''}</td><td>{status_pill(p['status'])}</td>"
        f"<td class=muted>{esc((p['error'] or p['reason'] or '')[:120])}</td></tr>"
        for p in store.proposals(None, 25)) or "<tr><td class=muted>None yet.</td></tr>"
    runs = "".join(
        f"<tr><td><a href='{B}/run/{r['id']}'>#{r['id']}</a></td><td>{r['started'][:16].replace('T', ' ')}</td><td><b>{esc(r['ticker'])}</b></td>"
        f"<td>{esc(r['rating'] or ('error' if r['error'] else 'running…'))}</td></tr>" for r in store.runs(15)) \
        or "<tr><td class=muted>None yet.</td></tr>"
    evs = "".join(f"<tr><td class=muted style='white-space:nowrap;width:1%'>{e['ts'][5:16].replace('T', ' ')}</td><td style='width:1%'>{e['level']}</td>"
                  f"<td>{esc(e['msg'][:400])}</td></tr>" for e in store.events(40))

    def opt(v, label):
        return f"<option value={v} {'selected' if s['mode'] == v else ''}>{label}</option>"

    items = "".join(f"<option>{esc(i.symbol)}</option>" for i in policy.parse_watchlist(s["watchlist"]))
    running = any(not r["finished"] for r in store.runs(5))
    kill_on = s["kill_switch"] == "on"
    control = (f"<form method=post action='{B}/mode' class=row><div class=grow><label>Mode</label><select name=mode>"
               f"{opt('signals', 'Signals only — never trades')}{opt('approval', 'Approval — I approve each trade')}"
               f"{opt('auto', 'Fully automatic — within limits')}</select></div><button>Set mode</button></form>"
               f"<form method=post action='{B}/kill' style='margin-top:14px'>"
               f"<input type=hidden name=value value={'off' if kill_on else 'on'}>"
               f"<button class={'sec' if kill_on else 'red'} style='width:100%'>"
               f"{'Turn kill switch OFF' if kill_on else 'KILL SWITCH: stop all trading'}</button></form>")
    run_card = (f"<form method=post action='{B}/run' class=row><div class=grow><select name=symbol>{items}</select></div>"
                f"<button>Analyse</button></form>"
                f"<p class=muted>Takes several minutes and costs roughly $0.50–$3 in Claude usage. Follows the current mode.</p>"
                + ("<p class=muted>Analysis running: this page refreshes every 30 seconds.</p>" if running else ""))
    test_card = card("Test tools <span class=pill>test mode only</span>",
                     f"<form method=post action='{B}/test-trade' class=row>"
                     f"<div class=grow><select name=symbol>{items}</select></div>"
                     f"<div class=grow><select name=action><option value=buy>Buy</option><option value=sell>Sell half</option></select></div>"
                     f"<button>Create test trade</button></form>"
                     f"<p class=muted>Instant and free: no AI analysis. Follows the current mode, limits and kill switch. "
                     f"Never reaches eToro.</p>", "c3") if config.DRY_RUN else ""
    body = ("<div class=layout>"
            + card("Control", control, "c4")
            + card("Portfolio", port, "c8")
            + card("Waiting for you", table(prow), "c6",
                   f"<span class='pill {'warn' if pend else ''}'>{len(pend)}</span>")
            + card("Run an analysis now", run_card, "c3" if config.DRY_RUN else "c6")
            + test_card
            + card("Recent calls", table(hist, "<tr><th>#</th><th>When</th><th>Symbol</th><th>Rating</th><th>Trade</th><th>Status</th><th>Note</th></tr>"), "c8")
            + card("Agent runs", table(runs), "c4")
            + card("Log", table(evs), "c12", f"<a class=muted href='{B}/audit'>Full audit trail / CSV</a>")
            + "</div><p class=muted>Research tool output is not financial advice. Limits are enforced by code, not by the AI.</p>")
    return page("Overview", body, refresh=30 if running else 0, active="Overview")


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    s = store.get_settings()
    fields = [("analyses_per_day", "Analyses per day (LLM cost)"), ("schedule_time", "Stock run time (UK, weekdays)"),
              ("crypto_time", "Crypto run time (UK, daily)"), ("max_trade_usd", "Max per trade ($)"),
              ("max_daily_usd", "Max buys per day ($)"), ("max_trades_per_day", "Max trades per day"),
              ("cash_buffer_usd", "Cash to keep ($)"), ("max_position_pct", "Max % in one holding"),
              ("cooldown_days", "Days between trades per holding"), ("approval_ttl_hours", "Approval expiry (hours)")]
    form = "".join(f"<div><label>{esc(l)}</label><input name={k} value='{esc(s[k])}'></div>" for k, l in fields)
    for k, l in (("allow_sells", "Agent may propose sells"), ("auto_sells", "Auto mode may sell without approval")):
        form += (f"<div><label>{l}</label><select name={k}><option {'selected' if s[k] == 'on' else ''}>on</option>"
                 f"<option {'selected' if s[k] != 'on' else ''}>off</option></select></div>")
    limits = (f"<form method=post action='{B}/settings'>"
              f"<label>Watchlist (eToro symbol[=research ticker][@crypto], comma-separated)</label>"
              f"<textarea name=watchlist rows=2>{esc(s['watchlist'])}</textarea>"
              f"<div class=fields style='margin-top:12px'>{form}</div>"
              f"<p><button>Save settings</button></p></form>")
    keys = (f"<form method=post action='{B}/keys'>"
            f"<label>Public Key (x-api-key) — {keystate('etoro_api_key', config.ETORO_API_KEY)}</label>"
            f"<input name=api_key type=password autocomplete=off placeholder='leave blank to keep'>"
            f"<label style='margin-top:12px'>Private key (x-user-key) — {keystate('etoro_user_key', config.ETORO_USER_KEY)}</label>"
            f"<textarea name=user_key rows=4 autocomplete=off placeholder='paste the long private key; leave blank to keep'></textarea>"
            f"<p><button>Save keys</button></p><p class=muted>Stored on your server only; never shown again.</p></form>")
    body = "<div class=layout>" + card("Limits and schedule", limits, "c8") + card("eToro connection", keys, "c4") + "</div>"
    return page("Settings", body, active="Settings")


def keystate(name: str, env_value: str) -> str:
    v = store.get_secret(name)
    if v:
        return f"saved on dashboard (…{esc(v[-4:])}, {len(v)} chars)"
    if env_value:
        return f"from Hostinger settings (…{esc(env_value[-4:])})"
    return "not set"


def back(to: str = "/") -> RedirectResponse:
    return RedirectResponse(f"{B}{to}", status_code=303)


@app.post("/mode")
def set_mode(mode: str = Form(...)):
    if mode not in ("signals", "approval", "auto"):
        raise HTTPException(400, "bad mode")
    store.set_setting("mode", mode)
    return back()


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
    return back()


NUMERIC = {"analyses_per_day", "max_trade_usd", "max_daily_usd", "max_trades_per_day", "cash_buffer_usd",
           "max_position_pct", "cooldown_days", "approval_ttl_hours"}


@app.post("/settings")
async def save_settings(watchlist: str = Form(...), analyses_per_day: str = Form(...), schedule_time: str = Form(...),
                        crypto_time: str = Form(...), max_trade_usd: str = Form(...), max_daily_usd: str = Form(...),
                        max_trades_per_day: str = Form(...), cash_buffer_usd: str = Form(...),
                        max_position_pct: str = Form(...), cooldown_days: str = Form(...),
                        approval_ttl_hours: str = Form(...), allow_sells: str = Form("off"), auto_sells: str = Form("off")):
    vals = dict(locals())
    for k in NUMERIC:
        try:
            if float(vals[k]) < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"{k} must be a non-negative number")
    for k in ("schedule_time", "crypto_time"):
        _hm(vals[k], "15:05")
    for k, v in vals.items():
        store.set_setting(k, v)
    schedule_jobs()
    return back("/settings")


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
    return page(f"Run {run_id}", f"<h1>{esc(r['ticker'])} · {esc(r['rating'] or '')}</h1>"
                f"<p class=muted>{r['started']} → {r['finished'] or 'running'}</p>"
                f"<div class=card><pre>{esc(r['decision'] or r['error'] or '')}</pre></div>")


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
