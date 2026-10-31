"""Dashboard, approval pages and the 24x7 scheduler."""
import html
import hmac
import threading
from contextlib import asynccontextmanager

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
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:20px 0 8px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:6px;border-bottom:1px solid #eef0f2;vertical-align:top}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eef1f5}
.real{background:#fde2e1;color:#9b1c1c}.demo{background:#e1f0fd;color:#1c4f9b}.on{background:#fde2e1;color:#9b1c1c}
button{border:0;border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer;background:#1d4ed8;color:#fff}
button.sec{background:#e5e7eb;color:#111}button.red{background:#b91c1c}
input,select,textarea{font-size:14px;padding:6px;border:1px solid #cfd4da;border-radius:6px;width:100%;box-sizing:border-box}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
label{font-size:12px;color:#555}.muted{color:#666;font-size:12px}pre{white-space:pre-wrap;font-size:13px}
"""


def page(title: str, body: str, refresh: int = 0) -> HTMLResponse:
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8>{meta}<meta name=viewport "
                        f"content='width=device-width,initial-scale=1'><title>{esc(title)}</title>"
                        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>")


@app.get("/health")
def health():
    return {"ok": True, "env": config.ETORO_ENV, "mode": store.get_settings()["mode"]}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    s = store.get_settings()
    env_cls = "real" if config.ETORO_ENV == "real" else "demo"
    try:
        book = engine.client().book()
        rows = "".join(f"<tr><td>{esc(h.symbol)}</td><td>${h.value_usd:,.2f}</td><td>{h.units:.6g}</td>"
                       f"<td>{'$%.2f' % h.pending_usd if h.pending_usd else ''}</td></tr>"
                       for h in sorted(book.holdings.values(), key=lambda h: -h.value_usd))
        port = (f"<p><b>${book.total_usd:,.2f}</b> total · cash ${book.cash_usd:,.2f}</p>"
                f"<table><tr><th>Holding</th><th>Value</th><th>Units</th><th>Pending buys</th></tr>{rows}</table>")
    except Exception as e:  # noqa: BLE001
        port = f"<p class=muted>Portfolio unavailable: {esc(str(e))}</p>"

    pend = store.proposals("pending", 20) + store.proposals("approved", 20)
    prow = "".join(
        f"<tr><td>#{p['id']}</td><td>{esc(p['ticker'])}</td><td>{esc(p['rating'] or '')}</td>"
        f"<td>{esc(engine._describe(p))}</td><td>{p['status']}</td><td>"
        + (f"<form method=post action='{B}/p/{p['id']}/{engine.token_for(p['id'], p['created'])}/approve' style=display:inline>"
           f"<button>Approve</button></form> "
           f"<form method=post action='{B}/p/{p['id']}/{engine.token_for(p['id'], p['created'])}/reject' style=display:inline>"
           f"<button class=sec>Reject</button></form>" if p["status"] == "pending" else "queued until market opens")
        + "</td></tr>" for p in pend) or "<tr><td class=muted>None</td></tr>"
    hist = "".join(
        f"<tr><td>#{p['id']}</td><td>{p['created'][:16]}</td><td>{esc(p['ticker'])}</td><td>{esc(p['rating'] or '')}</td>"
        f"<td>{esc(engine._describe(p)) if p['action'] != 'none' else ''}</td><td>{p['status']}</td>"
        f"<td class=muted>{esc((p['error'] or p['reason'] or '')[:120])}</td></tr>"
        for p in store.proposals(None, 25))
    runs = "".join(
        f"<tr><td><a href='{B}/run/{r['id']}'>#{r['id']}</a></td><td>{r['started'][:16]}</td><td>{esc(r['ticker'])}</td>"
        f"<td>{esc(r['rating'] or ('error' if r['error'] else 'running…'))}</td></tr>" for r in store.runs(15))
    evs = "".join(f"<tr><td class=muted>{e['ts'][5:16]}</td><td>{e['level']}</td><td>{esc(e['msg'][:400])}</td></tr>"
                  for e in store.events(40))

    def opt(v, label):
        return f"<option value={v} {'selected' if s['mode'] == v else ''}>{label}</option>"

    fields = [("watchlist", "Watchlist (eToro symbol[=research ticker][@crypto])"),
              ("analyses_per_day", "Analyses per day (LLM cost)"), ("schedule_time", "Stock run time (UK, weekdays)"),
              ("crypto_time", "Crypto run time (UK, daily)"), ("max_trade_usd", "Max per trade ($)"),
              ("max_daily_usd", "Max buys per day ($)"), ("max_trades_per_day", "Max trades per day"),
              ("cash_buffer_usd", "Cash to keep ($)"), ("max_position_pct", "Max % in one holding"),
              ("cooldown_days", "Days between trades per holding"), ("approval_ttl_hours", "Approval expiry (hours)")]
    form = "".join(f"<div><label>{esc(l)}</label><input name={k} value='{esc(s[k])}'></div>" for k, l in fields)
    for k, l in (("allow_sells", "Agent may propose sells"), ("auto_sells", "Auto mode may sell without approval")):
        form += (f"<div><label>{l}</label><select name={k}><option {'selected' if s[k]=='on' else ''}>on</option>"
                 f"<option {'selected' if s[k]!='on' else ''}>off</option></select></div>")
    items = "".join(f"<option>{esc(i.symbol)}</option>" for i in policy.parse_watchlist(s["watchlist"]))

    running = any(not r["finished"] for r in store.runs(5))
    test_card = (f"<h2>Test tools <span class=pill>test mode only</span></h2><div class=card>"
                 f"<form method=post action='{B}/test-trade' style='display:flex;gap:8px;flex-wrap:wrap'>"
                 f"<select name=symbol style='max-width:160px'>{items}</select>"
                 f"<select name=action style='max-width:160px'><option value=buy>Buy</option>"
                 f"<option value=sell>Sell half</option></select><button>Create test trade</button></form>"
                 f"<p class=muted>Instant and free: no AI analysis. Follows the current mode, limits and kill switch. "
                 f"Never reaches eToro.</p></div>") if config.DRY_RUN else ""
    body = f"""
<h1>TradeAgent <span class='pill {env_cls}'>{config.ETORO_ENV.upper()} account</span>
{' <span class=pill>DRY RUN</span>' if config.DRY_RUN else ''}
{' <span class="pill on">KILL SWITCH ON</span>' if s['kill_switch']=='on' else ''}</h1>
{'<p class=muted>Analysis running: this page refreshes every 30 seconds.</p>' if running else ''}
<div class=card><form method=post action='{B}/mode' style='display:flex;gap:8px;align-items:end;flex-wrap:wrap'>
<div style='flex:1;min-width:220px'><label>Mode</label><select name=mode>
{opt('signals','Signals only — never trades')}{opt('approval','Approval — I approve each trade')}{opt('auto','Fully automatic — within limits')}
</select></div><button>Set mode</button></form>
<form method=post action='{B}/kill' style='margin-top:10px'>
<input type=hidden name=value value={'off' if s['kill_switch']=='on' else 'on'}>
<button class={'sec' if s['kill_switch']=='on' else 'red'}>{'Turn kill switch OFF' if s['kill_switch']=='on' else 'KILL SWITCH: stop all trading'}</button></form></div>
<h2>Portfolio</h2><div class=card>{port}</div>
<h2>eToro connection</h2><div class=card><form method=post action='{B}/keys'><div class=grid>
<div><label>Public Key (x-api-key) — {keystate('etoro_api_key', config.ETORO_API_KEY)}</label><input name=api_key type=password autocomplete=off placeholder='leave blank to keep'></div>
<div><label>Private key (x-user-key) — {keystate('etoro_user_key', config.ETORO_USER_KEY)}</label><textarea name=user_key rows=3 autocomplete=off placeholder='paste the long private key; leave blank to keep'></textarea></div>
</div><p><button>Save keys</button> <span class=muted>Stored on your server only; never shown again.</span></p></form></div>
<h2>Waiting for you</h2><div class=card><table>{prow}</table></div>
<h2>Run an analysis now</h2><div class=card><form method=post action='{B}/run' style='display:flex;gap:8px'>
<select name=symbol>{items}</select><button>Analyse</button></form>
<p class=muted>Takes several minutes and costs roughly $0.50–$3 in Claude usage. Follows the current mode.</p></div>
{test_card}
<h2>Limits and schedule</h2><div class=card><form method=post action='{B}/settings'><div class=grid>{form}</div>
<p><button>Save settings</button></p></form></div>
<h2>Recent calls</h2><div class=card><table>{hist}</table></div>
<h2>Agent runs</h2><div class=card><table>{runs}</table></div>
<h2>Log <a class=muted href='{B}/audit'>full audit trail / CSV</a></h2><div class=card><table>{evs}</table></div>
<p class=muted>Research tool output is not financial advice. Limits are enforced by code, not by the AI.</p>"""
    return page("TradeAgent", body, refresh=30 if running else 0)


def keystate(name: str, env_value: str) -> str:
    v = store.get_secret(name)
    if v:
        return f"saved on dashboard (…{esc(v[-4:])}, {len(v)} chars)"
    if env_value:
        return f"from Hostinger settings (…{esc(env_value[-4:])})"
    return "not set"


def back() -> RedirectResponse:
    return RedirectResponse(f"{B}/", status_code=303)


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
    return back()


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
    return page("Audit trail", f"<p><a href='{B}/'>← Dashboard</a> · <a href='{B}/audit.csv'>Download CSV</a></p>"
                f"<h1>Audit trail</h1><p class=muted>Every proposal event, newest first (times UTC).</p>"
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
    return page(f"Run {run_id}", f"<p><a href='{B}/'>← Dashboard</a></p><h1>{esc(r['ticker'])} · {esc(r['rating'] or '')}</h1>"
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
    return page("Approve trade", f"<h1>{esc(engine._describe(p))}</h1><p><span class=pill>{env} account</span> "
                f"<span class=pill>rating {esc(p['rating'] or '')}</span> <span class=pill>status {p['status']}</span></p>"
                f"<div class=card>{acts or '<p>Already ' + p['status'] + '.</p>'}</div>"
                f"<h2>Agents' decision</h2><div class=card><pre>{esc((run.get('decision') or '')[:8000])}</pre></div>")


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
    return page("Done", f"<h1>{esc(engine._describe(p))}</h1><div class=card><p>{text}</p>"
                f"<p class=muted>Status: {p['status']} {esc(p['error'] or '')}</p></div>")
