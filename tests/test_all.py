import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import policy  # noqa: E402

S = {"mode": "approval", "kill_switch": "off", "max_trade_usd": "25", "max_daily_usd": "50",
     "max_trades_per_day": "2", "cash_buffer_usd": "5", "max_position_pct": "40", "cooldown_days": "3",
     "allow_sells": "on", "auto_sells": "off"}
POS = [{"positionID": 1, "units": 0.2, "value_usd": 120.0, "openRate": 500}]


# ---------------------------------------------------------------- policy
def test_watchlist_parse():
    items = policy.parse_watchlist("META, btc=BTC-USD@crypto,\nIUHC.L")
    assert [(i.symbol, i.yf, i.asset_type) for i in items] == [
        ("META", "META", "stock"), ("BTC", "BTC-USD", "crypto"), ("IUHC.L", "IUHC.L", "stock")]


@pytest.mark.parametrize("rating,action,amount", [("Buy", "buy", 25), ("Overweight", "buy", 12.5),
                                                  ("Hold", "none", 0), ("REVIEW", "none", 0)])
def test_propose_buys(rating, action, amount):
    p = policy.propose(rating, holding_value=0, holding_positions=[], pending_usd=0, cash=100,
                       portfolio_total=400, s=S)
    assert p.action == action and p.amount_usd == amount


def test_propose_sells():
    p = policy.propose("Sell", holding_value=120, holding_positions=POS, pending_usd=0, cash=0,
                       portfolio_total=400, s=S)
    assert p.action == "close" and p.position_ids == [1]
    p = policy.propose("Underweight", holding_value=120, holding_positions=POS, pending_usd=0, cash=0,
                       portfolio_total=400, s=S)
    assert p.action == "partial_close" and p.units == 0.1
    assert policy.propose("Sell", holding_value=0, holding_positions=[], pending_usd=0, cash=0,
                          portfolio_total=400, s=S).action == "none"
    assert policy.propose("Sell", holding_value=120, holding_positions=POS, pending_usd=0, cash=0,
                          portfolio_total=400, s={**S, "allow_sells": "off"}).action == "none"


def limits(p, **kw):
    args = dict(instrument_id=1, cash=100, portfolio_total=400, holding_value=0, pending_usd=0,
                spent_today=0, trades_today=0, last_trade_iso=None, tradable=True, s=S)
    args.update(kw)
    return policy.check_limits(p, **args)


def test_buy_is_capped_by_cash_day_and_position():
    buy = policy.Proposal("buy", 25)
    assert limits(buy)[0].amount_usd == 25
    assert limits(buy, cash=20)[0].amount_usd == 15            # keeps $5 buffer
    assert limits(buy, spent_today=38)[0].amount_usd == 12     # daily cap $50
    assert limits(buy, holding_value=150)[0].amount_usd == 10  # 40% of 400 = 160
    p, why = limits(buy, cash=12)
    assert p is None and "minimum" in why[0]


def test_hard_blocks():
    buy = policy.Proposal("buy", 25)
    assert limits(buy, s={**S, "kill_switch": "on"})[0] is None
    assert limits(buy, trades_today=2)[0] is None
    assert limits(buy, tradable=False)[0] is None
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert limits(buy, last_trade_iso=recent)[0] is None
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    assert limits(buy, last_trade_iso=old)[0] is not None


def test_needs_approval():
    sell, buy = policy.Proposal("close"), policy.Proposal("buy", 20)
    assert policy.needs_approval(buy, {**S, "mode": "approval"})
    assert not policy.needs_approval(buy, {**S, "mode": "auto"})
    assert policy.needs_approval(sell, {**S, "mode": "auto"})
    assert not policy.needs_approval(sell, {**S, "mode": "auto", "auto_sells": "on"})


# ---------------------------------------------------------------- eToro client
class Resp:
    def __init__(self, status, data):
        self.status_code, self._d, self.headers = status, data, {}
        self.content = b"x"
        self.text = str(data)

    def json(self):
        return self._d


CREDIT = {"v": 150.0}


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        if "/search" in url:
            return Resp(200, {"items": [{"internalSymbolFull": "META.24-7", "internalInstrumentId": 9},
                                        {"internalSymbolFull": "META", "internalInstrumentId": 1003,
                                         "isCurrentlyTradable": True, "currentRate": 600}]})
        if "/rates" in url:
            return Resp(200, {"rates": [{"instrumentID": 1003, "bid": 600, "conversionRateBid": 1},
                                        {"instrumentID": 3017, "bid": 180, "conversionRateBid": 1}]})
        if "/instruments" in url:
            iid = int(params["instrumentIds"])
            return Resp(200, {"instrumentDisplayDatas": [{"instrumentID": iid, "symbolFull": {1003: "META", 3017: "XLV"}[iid]}]})
        if "/info/portfolio" in url or "/info/demo/portfolio" in url:
            return Resp(200, {"clientPortfolio": {
                "credit": CREDIT["v"],
                "positions": [{"positionID": 11, "instrumentID": 1003, "isBuy": True, "leverage": 1, "mirrorID": 0,
                               "units": 0.25, "openRate": 559},
                              {"positionID": 12, "instrumentID": 1003, "isBuy": True, "leverage": 2, "mirrorID": 0,
                               "units": 1, "openRate": 559}],
                "ordersForOpen": [{"instrumentID": 3017, "isBuy": True, "frozenAmount": 100.0}]}})
        if "/orders" in url or "market-close-orders" in url:
            return Resp(200, {"orderId": 777, "orderForClose": {"orderID": 888}})
        return Resp(404, {})


def test_etoro_book_and_orders():
    from app.etoro import EtoroClient
    CREDIT["v"] = 150.0
    fs = FakeSession()
    ec = EtoroClient("k", "u", env="real", base="https://x", session=fs)
    assert ec.find_instrument("META")["instrumentId"] == 1003
    b = ec.book()
    assert b.cash_usd == 50.0                              # credit minus frozen pending order
    assert b.holdings[1003].value_usd == pytest.approx(150.0)   # leveraged position excluded
    assert b.holdings[3017].pending_usd == 100.0
    assert b.total_usd == pytest.approx(300.0)
    ec.open_buy("META", 1003, 20)
    m, url, _, body, h = fs.calls[-1]
    assert url.endswith("/api/v2/trading/execution/orders") and body["leverage"] == 1 and body["amount"] == 20
    assert body["instrumentId"] == 1003 and "symbol" not in body          # eToro: exactly one of symbol/instrumentId
    assert h["x-api-key"] == "k" and h["x-request-id"]
    ec.close_position(11, 1003, 0.1)
    assert fs.calls[-1][1].endswith("/api/v1/trading/execution/market-close-orders/positions/11")
    demo = EtoroClient("k", "u", env="demo", base="https://x", session=fs)
    demo.open_buy("META", 1003, 20)
    assert "/demo/orders" in fs.calls[-1][1] and fs.calls[-1][3]["settlement"] == "real"


# ---------------------------------------------------------------- engine end to end
@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_SECRET", "s3cret")
    from app import config
    importlib.reload(config)
    from app import store, engine, notify, etoro
    importlib.reload(store)
    importlib.reload(engine)
    store.init()
    store.set_setting("live_limits_applied", "test")   # keep test defaults; the go-live change has its own test
    store.set_setting("watchlist_fix_applied", "test")
    store.set_setting("levels_backfill_applied", "test")
    from app import levels
    monkeypatch.setattr(levels, "call_model", lambda prompt: '{"conditions": []}')
    sent = []
    monkeypatch.setattr(notify, "sms", lambda t: sent.append(("sms", t)) or True)
    monkeypatch.setattr(notify, "email", lambda s, b, html=None: sent.append(("email", s)) or True)
    CREDIT["v"] = 500.0
    fs = FakeSession()
    monkeypatch.setattr(engine, "client", lambda: etoro.EtoroClient("k", "u", env="real", base="https://x", session=fs))
    rating = {"v": "Buy"}
    monkeypatch.setattr(engine.analyst, "analyze", lambda t, a, p, rules="": (rating["v"], f"FINAL: {rating['v']}"))
    return engine, store, sent, fs, rating


def orders(fs):
    return [c for c in fs.calls if c[0] == "POST"]


def test_approval_flow(eng):
    engine, store, sent, fs, rating = eng
    out = engine.analyse(policy.WatchItem("META", "META"))
    pid = out["pending"]
    p = store.get_proposal(pid)
    assert p["status"] == "pending" and p["amount_usd"] == 25
    assert any(k == "sms" and "/p/" in t for k, t in sent)
    assert not orders(fs)                                    # nothing placed before approval
    res = engine.decide(pid, approve=True)
    assert res["executed"] and len(orders(fs)) == 1
    assert store.get_proposal(pid)["status"] == "executed"
    # cooldown now blocks a second buy of the same instrument
    out2 = engine.analyse(policy.WatchItem("META", "META"))
    assert "blocked" in out2


def test_signals_and_auto_and_kill(eng):
    engine, store, sent, fs, rating = eng
    store.set_setting("mode", "signals")
    assert "signal" in engine.analyse(policy.WatchItem("META", "META"))
    assert not orders(fs)
    store.set_setting("mode", "auto")
    store.set_setting("kill_switch", "on")
    assert "blocked" in engine.analyse(policy.WatchItem("META", "META"))
    assert not orders(fs)
    store.set_setting("kill_switch", "off")
    out = engine.analyse(policy.WatchItem("META", "META"))
    assert out["result"]["executed"] and len(orders(fs)) == 1
    # sells in auto mode still need approval by default
    rating["v"] = "Sell"
    store.set_setting("cooldown_days", "0")
    out = engine.analyse(policy.WatchItem("META", "META"))
    assert "pending" in out and len(orders(fs)) == 1


def test_reject_and_expiry(eng):
    engine, store, sent, fs, rating = eng
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    assert engine.decide(pid, approve=False)["status"] == "rejected"
    assert engine.decide(pid, approve=True)["error"]       # cannot approve after reject
    pid2 = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
    store.update_proposal(pid2, created=old)
    engine.process_queue()
    assert store.get_proposal(pid2)["status"] == "expired" and not orders(fs)


def test_web_approval_page_requires_token(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    p = store.get_proposal(pid)
    tok = engine.token_for(pid, p["created"])
    with TestClient(web.app) as c:
        assert c.get(f"/p/{pid}/wrongtoken").status_code == 404
        r = c.get(f"/p/{pid}/{tok}")
        assert r.status_code == 200 and "Approve" in r.text and not orders(fs)   # GET never trades
        r = c.post(f"/p/{pid}/{tok}/approve")
        assert "Order placed" in r.text and len(orders(fs)) == 1
        assert c.get("/").status_code == 200
        assert c.post("/mode", data={"mode": "yolo"}).status_code == 400


def test_dashboard_keys_override_env(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, etoro
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    long_key = "eyJ" + "x" * 600
    with TestClient(web.app) as c:
        r = c.post("/keys", data={"api_key": "PUB123", "user_key": f"  {long_key[:300]}\n{long_key[300:]} "})
        assert r.status_code == 200
        page = c.get("/settings").text
        assert long_key not in page and "603 chars" in page
    ec = etoro.EtoroClient()
    assert ec.api_key == "PUB123" and ec.user_key == long_key     # whitespace/newlines stripped


def test_split_env_user_key(monkeypatch):
    monkeypatch.setenv("ETORO_USER_KEY", "abc")
    monkeypatch.setenv("ETORO_USER_KEY_2", "def")
    from app import config
    importlib.reload(config)
    assert config.ETORO_USER_KEY == "abcdef"


def test_dashboard_autorefresh_while_running(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    with TestClient(web.app) as c:
        assert "content=30" not in c.get("/").text and "http-equiv=refresh" in c.get("/").text    # idle: slower portfolio/watchlist reload
        rid = store.start_run("BTC")
        assert "http-equiv=refresh content=30" in c.get("/").text
        store.finish_run(rid, "Hold", "x")
        assert "content=30" not in c.get("/").text and "http-equiv=refresh" in c.get("/").text    # idle: slower portfolio/watchlist reload


def test_status_strip_shows_state(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import config, web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    with TestClient(web.app) as c:
        html = c.get("/").text
        assert "LIVE orders" in html and "Mode: Approval" in html and "Trading enabled" in html
        assert "class='banner" not in html
        store.set_setting("kill_switch", "on")
        html = c.get("/").text
        assert "KILL SWITCH ON" in html and "banner stop" in html
        store.set_setting("kill_switch", "off")
        store.set_setting("mode", "auto")
        assert "banner warn" in c.get("/").text
        monkeypatch.setattr(config, "DRY_RUN", True)
        html = c.get("/").text
        assert "TEST MODE" in html and "banner warn" not in html


def test_approval_queue_shows_expiry_and_reason(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    p = store.get_proposal(pid)
    s = store.get_settings()
    created = datetime.fromisoformat(p["created"])
    assert web.time_left(p, s, created + timedelta(hours=1, minutes=30)) == "expires in 4h 30m"
    assert web.time_left(p, s, created + timedelta(hours=5, minutes=45)) == "expires in 15m"
    assert web.time_left(p, s, created + timedelta(hours=7)) == "expiring now"
    with TestClient(web.app) as c:
        html = c.get("/").text
        assert "expires in" in html and "rating Buy" in html and "Details" in html
        assert "pill warn'>pending" in html and f"/p/{pid}/" in html
        engine.decide(pid, approve=False)
        assert "Nothing waiting." in c.get("/").text


def test_run_progress_and_report(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    run = {"started": t0.isoformat(), "finished": None}
    assert web.run_duration(run, t0 + timedelta(minutes=3)) == "running 3m"
    assert web.run_duration(run, t0 + timedelta(seconds=20)) == "just started"
    assert web.run_duration({**run, "finished": (t0 + timedelta(minutes=4)).isoformat()}) == "took 4m"
    assert "okp'>Buy" in web.rating_pill("Buy") and "on'>Sell" in web.rating_pill("Sell")
    with TestClient(web.app) as c:
        rid = store.start_run("BTC")
        html = c.get("/").text
        assert "pill warn'>running" in html and "<b>BTC</b>" in html
        assert "http-equiv=refresh content=30" in c.get(f"/run/{rid}").text
        # a run left unfinished by a restart hours ago is not shown as live work
        with store.connect() as conn:
            conn.execute("UPDATE runs SET started=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(), rid))
        assert "content=30" not in c.get("/").text and "http-equiv=refresh" in c.get("/").text    # idle: slower portfolio/watchlist reload
        out = engine.analyse(policy.WatchItem("META", "META"))
        run_id = store.proposals("pending")[0]["run_id"]
        page = c.get(f"/run/{run_id}").text
        assert "Rating" in page and "BUY META" in page and "pill warn'>pending" in page and "FINAL: Buy" in page
        assert "http-equiv=refresh" not in page


def test_scheduler_can_be_disabled(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import config, web
    importlib.reload(web)
    started = []
    monkeypatch.setattr(web.sched, "start", lambda: started.append(1))
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    with TestClient(web.app) as c:
        assert c.get("/health").status_code == 200
    assert started == []
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    with TestClient(web.app):
        pass
    assert started == [1]


def test_portfolio_allocation_and_cap_warnings(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    assert [web.alloc_state(x, 40) for x in (10, 32, 40, 41)] == ["ok", "near", "near", "over"]
    assert web.alloc_state(90, 0) == "ok"
    with TestClient(web.app) as c:
        html = c.get("/").text               # META is 150 of 650 = 23%, cap 40%
        assert "class=bar" in html and "left:40.0%" in html and "over cap" not in html and "near cap" not in html
        store.set_setting("max_position_pct", "25")
        assert "near cap" in c.get("/").text
        store.set_setting("max_position_pct", "20")
        html = c.get("/").text
        assert "over cap" in html and "above your 20% cap" in html


def test_cancel_queued_trade_is_logged_and_never_executes(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, etoro
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    ec = etoro.EtoroClient("k", "u", env="real", base="https://x", session=fs)
    real_find = ec.find_instrument
    closed = {"v": True}
    monkeypatch.setattr(ec, "find_instrument", lambda s: {**real_find(s), "tradable": not closed["v"]})
    monkeypatch.setattr(engine, "client", lambda: ec)
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    assert engine.decide(pid, approve=True)["queued"]
    assert store.get_proposal(pid)["error"] == "waiting for market"
    assert engine.cancel(98765)["error"]                       # unknown id
    with TestClient(web.app) as c:
        html = c.get("/").text
        assert "waiting for market to open" in html and f"/cancel/{pid}" in html
        assert c.post(f"/cancel/{pid}", follow_redirects=False).status_code == 303
        p = store.get_proposal(pid)
        assert p["status"] == "cancelled" and "while waiting for market" in p["error"]
        assert f"/cancel/{pid}" not in c.get("/").text
        assert engine.cancel(pid)["error"]                     # can't cancel twice
        line = [e["msg"] for e in store.events(20, level="audit") if "cancelled" in e["msg"]]
        assert len(line) == 1 and f"proposal #{pid} cancelled" in line[0] and "by dashboard" in line[0]
        assert "cancelled" in c.get("/audit").text
    closed["v"] = False                                        # market opens: cancelled trade must stay cancelled
    engine.process_queue()
    assert store.get_proposal(pid)["status"] == "cancelled" and not orders(fs)


def test_opportunity_parse_and_source_validation():
    from app import opportunities as op
    text = ('Here you go ```json\n{"opportunities": [\n'
            '{"symbol": "nvda", "name": "NVIDIA", "asset_type": "stock", "thesis": "t", "catalyst": "c", "risks": "r",'
            ' "horizon": "weeks", "sources": [{"title": "Real", "url": "https://www.reuters.com/a"},'
            ' {"title": "Made up", "url": "https://fake.example/x"}, {"title": "bad", "url": "javascript:alert(1)"}]},\n'
            '{"symbol": "NVDA", "sources": [{"url": "https://www.reuters.com/a"}]},\n'
            '{"symbol": "ZZZ", "sources": [{"title": "x", "url": "https://fake.example/y"}]}]} ```')
    raw = op.parse(text)
    out = op.clean(raw, {"https://reuters.com/a/"})
    assert [o["symbol"] for o in out] == ["NVDA"]                    # duplicate and unsourced ideas dropped
    assert [s["url"] for s in out[0]["sources"]] == ["https://www.reuters.com/a"]   # only URLs the search returned
    with pytest.raises(ValueError):
        op.parse("no json here")


def test_opportunity_scan_page_and_watchlist(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, opportunities as op
    from app.etoro import EtoroError
    importlib.reload(web)
    importlib.reload(op)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    ideas = {"opportunities": [
        {"symbol": s, "name": s + " Inc", "asset_type": "crypto" if s == "SOL" else "stock",
         "thesis": f"<b>{s}</b> thesis", "catalyst": "cat", "risks": "risk", "horizon": "weeks",
         "sources": [{"title": "Src", "url": f"https://news.example/{s}"}]} for s in ["AAA", "BBB", "SOL", "CCC", "DDD", "EEE"]]}
    urls = {f"https://news.example/{s}" for s in ["AAA", "BBB", "SOL", "CCC", "DDD", "EEE"]}
    monkeypatch.setattr(op, "call_model", lambda prompt, system=None: ("```" + json.dumps(ideas) + "```", urls))

    class EC:
        def find_instrument(self, sym):
            if sym == "BBB":
                raise EtoroError("not found")
            return {"tradable": sym != "CCC", "name": sym, "rate": 12.5, "assetClass": "x"}
    monkeypatch.setattr(op.engine, "client", lambda: EC())
    with TestClient(web.app) as c:
        assert "No scan yet" in c.get("/opportunities").text
        assert ">Opportunities</a>" in c.get("/").text
        res = op.scan()
        assert len(res["items"]) == op.COUNT                          # capped at five
        html = c.get("/opportunities").text
        assert "Not found on eToro" in html and "market closed" in html and "market open" in html
        assert "&lt;b&gt;AAA&lt;/b&gt;" in html and "<b>AAA</b>" not in html      # model text is escaped
        assert "noopener noreferrer" in html and "news.example/AAA" in html
        assert "/opportunities/watch/BBB" not in html                 # can't watch something eToro lacks
        assert c.post("/opportunities/watch/BBB").status_code == 404
        c.post("/opportunities/watch/SOL")
        c.post("/opportunities/watch/AAA")
        c.post("/opportunities/watch/AAA")                            # idempotent
        wl = store.get_settings()["watchlist"]
        assert wl.endswith("SOL=SOL-USD@crypto,AAA") and wl.count("AAA") == 1
        assert "On your watchlist" in c.get("/opportunities").text
        assert not orders(fs)                                         # nothing was ever traded
        monkeypatch.setattr(op, "MAX_SCANS_PER_DAY", 1)
        assert "daily scan limit" in op.can_scan()
        started = []
        monkeypatch.setattr(op, "scan", lambda: started.append(1))
        c.post("/opportunities/scan")
        assert not started                                            # limit enforced on the route too


def test_failed_scan_is_recorded_and_not_blocking(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import opportunities as op
    importlib.reload(op)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(op, "call_model", lambda p, system=None: ("not json", set()))
    assert "error" in op.scan()
    last = store.latest_scan()
    assert last["finished"] and "parsable" in last["error"]
    assert op.can_scan() is None
    store.start_scan()                                                # a scan that never finished
    assert op.can_scan() == "a scan is already running"
    assert op.stale({"started": "2020-01-01T00:00:00+00:00"})


def test_watchlist_market_rows_and_card(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, markets
    importlib.reload(markets)
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    candles = [{"date": f"2026-10-{d:02d}", "open": 1, "high": 243.31 if d == 5 else 210, "low": 162.95 if d == 6 else 200,
                "close": 213.67 if d == 29 else 204.68} for d in range(1, 31)]
    item = {"internalInstrumentId": 1137, "internalInstrumentDisplayName": "NVIDIA Corporation", "dailyPriceChange": -4.22,
            "buyHoldingPct": 100, "sellHoldingPct": 0, "isCurrentlyTradable": False, "logo50x50": "https://cdn.example/nvda.png"}
    row = markets.build_row("NVDA", item, candles, {"bid": 204.68, "ask": 204.69})
    assert row["bid"] == 204.68 and row["ask"] == 204.69
    assert row["low52"] == 162.95 and row["high52"] == 243.31
    assert 0 < row["range_pos"] < 100 and row["sentiment"] == ("Buying", 100.0)
    assert row["change_abs"] == pytest.approx(204.68 - 204.68 / 0.9578, abs=0.01)     # about -9.02
    assert markets.sentiment({"buyHoldingPct": 30, "sellHoldingPct": 70}) == ("Selling", 70.0)
    assert markets.sentiment({}) is None
    fallback = markets.build_row("X", {}, candles[-2:], None)                       # no search data: change from candles
    assert fallback["change_pct"] == pytest.approx((204.68 / 213.67 - 1) * 100)
    assert markets.range_position(5, 5, 5) == 50.0

    class EC:
        def search_item(self, sym):
            if sym == "BAD":
                raise RuntimeError("boom <script>")
            return {**item, "internalInstrumentId": {"META": 1, "GOOG": 2}.get(sym, 3), "internalInstrumentDisplayName": sym}
        def daily_candles(self, iid, count=365):
            return candles
        def rates(self, ids):
            return {i: {"bid": 10.0, "ask": 10.5} for i in ids}
    rows = markets.watchlist_rows(EC(), ["META", "BAD", "GOOG"])
    assert [r["symbol"] for r in rows] == ["META", "BAD", "GOOG"] and "error" in rows[1] and "error" not in rows[0]
    html = web.market_row(rows[0])
    assert "chg down" in html and "-4.22%" in html and "100% Buying" in html and "closed" in html and "<polyline" in html
    assert "<script>" not in web.market_row(rows[1]) and "Market data unavailable" in web.market_row(rows[1])
    monkeypatch.setattr(web.engine, "client", lambda: EC())
    monkeypatch.setattr(markets, "_cache", {})
    with TestClient(web.app) as c:
        page = c.get("/").text
        assert not orders(fs)

    # ordered by 1-day change, best first; failed symbols last
    changes = {"LOW": -3.0, "HIGH": 4.0, "MID": 1.0}

    class EC2(EC):
        def search_item(self, sym):
            if sym == "BAD":
                raise RuntimeError("x")
            return {**item, "internalInstrumentId": 9, "internalInstrumentDisplayName": sym, "dailyPriceChange": changes[sym]}
    monkeypatch.setattr(markets, "_cache", {})
    monkeypatch.setattr(web.engine, "client", lambda: EC2())
    base_settings = store.get_settings()
    monkeypatch.setattr(web.store, "get_settings", lambda: {**base_settings, "watchlist": "LOW,BAD,HIGH,MID"})
    with TestClient(web.app) as c:
        page = c.get("/").text
        i = [page.index(f"<b>{s}</b>") for s in ("HIGH", "MID", "LOW", "BAD")]
        assert i == sorted(i)


def test_watchlist_hourly_sparkline_and_price_as_of(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import web, markets
    importlib.reload(markets)
    importlib.reload(web)
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    reads = {"AAPL": 0, "BTC": 0}

    class EC:
        def search_item(self, sym):
            return {"internalInstrumentId": 1 if sym == "AAPL" else 2, "internalInstrumentDisplayName": sym,
                    "internalAssetClassName": "Stocks" if sym == "AAPL" else "Cryptocurrencies", "dailyPriceChange": 1.0,
                    "isCurrentlyTradable": sym != "AAPL"}
        def daily_candles(self, iid, count=365):
            return [{"date": "d", "open": 1, "high": 2, "low": 1, "close": 1.5}] * 3
        def intraday_candles(self, iid, minutes=60):
            reads["AAPL" if iid == 1 else "BTC"] += 1
            if iid == 1:      # US stock: last trade minute 19:59 UTC Friday
                return [{"date": f"2026-10-30T19:{57 + i}:00Z", "close": c} for i, c in enumerate((3, 2, 1))]
            base = int(clock["t"] // 60) * 60
            return [{"date": datetime.fromtimestamp(base - 120 + 60 * i, timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"), "close": c}
                    for i, c in enumerate((1, 2, 3))]
        def rates(self, ids):
            return {i: {"bid": 1.5, "ask": 1.6} for i in ids}

    clock = {"t": utc(2026, 10, 30, 21, 0).timestamp()}            # Fri after the US close
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    rows = {r["symbol"]: r for r in markets.watchlist_rows(EC(), ["AAPL", "BTC"])}
    assert rows["AAPL"]["intraday"] == {"closes": [3, 2, 1], "asof": utc(2026, 10, 30, 20, 0).timestamp()}
    assert rows["BTC"]["intraday"]["closes"] == [1, 2, 3] and rows["BTC"]["intraday"]["asof"] == clock["t"]
    clock["t"] += 7200
    markets.invalidate(open_only=True)                             # the scheduled refresh
    markets.watchlist_rows(EC(), ["AAPL", "BTC"])
    assert reads == {"AAPL": 1, "BTC": 2}                          # closed US stock not re-read; crypto is

    html = web.market_row(rows["AAPL"])
    assert html.count("<svg class=spark") == 2                     # 60-minute and 30-day lines
    assert "Price as of 2026-10-30 20:00:00 GMT; market closed, so the price is the last trade" in html
    assert "title='Last read" not in html
    live = web.market_row(rows["BTC"])
    assert "title='Last 60 minutes, one-minute prices'" in live and "market closed" not in live.split("Price as of")[1]
    assert "<svg" not in web.market_row({**rows["BTC"], "intraday": None, "spark": []})   # missing data degrades to n/a

    monkeypatch.setattr(web.engine, "client", lambda: EC())
    base_settings = store.get_settings()
    monkeypatch.setattr(web.store, "get_settings", lambda: {**base_settings, "watchlist": "AAPL,BTC"})
    monkeypatch.setattr(markets, "_cache", {})
    from fastapi.testclient import TestClient
    monkeypatch.setattr(web.sched, "start", lambda: None)
    with TestClient(web.app) as c:
        page = c.get("/").text
        head = page[page.index("<th>Markets"):page.index("</tr>", page.index("<th>Markets"))]
        assert head.index("<th>Change 1D</th>") < head.index("<th>60 min</th>") < head.index("<th>30 days</th>")
        assert head.endswith("<th>Price as of</th>") and "Last refreshed" not in head
        w = page[page.index("<th>Markets"):page.index("</table>", page.index("<th>Markets"))]
        assert {r.count("<td") for r in w.split("<tr>")[2:]} == {head.count("<th>")}   # one cell per column in every row


def test_reset_daily_limits(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    store.set_setting("max_trades_per_day", "1")
    store.set_setting("cooldown_days", "0")
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    assert engine.decide(pid, approve=True)["executed"]
    store.update_proposal(pid, executed=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds"))
    assert engine.daily_usage() == {"spent": 25.0, "trades": 1, "analyses": 1}
    out = engine.analyse(policy.WatchItem("META", "META"))
    assert "blocked" in out and "daily trade limit" in str(out)
    with TestClient(web.app) as c:
        page = c.get("/settings").text
        assert "Reset daily limits" in page and "$25.00 of $50.00 spent" in page
        assert c.get("/reset-daily").status_code == 405               # never reset by a plain link
        assert c.post("/reset-daily", follow_redirects=False).status_code == 303
    assert engine.daily_usage()["trades"] == 0
    assert store.get_settings()["daily_reset_at"]
    assert any("daily limits reset" in e["msg"] for e in store.events(5))
    out = engine.analyse(policy.WatchItem("META", "META"))
    assert "pending" in out                                           # allowed again after the reset
    store.set_setting("cooldown_days", "3")
    store.set_setting("max_trades_per_day", "5")
    assert "cooldown" in str(engine.analyse(policy.WatchItem("META", "META")))   # cooldown is not reset


def test_manual_execute_from_run_page(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    store.set_setting("mode", "signals")
    out = engine.analyse(policy.WatchItem("META", "META"))
    pid = out["signal"]
    run_id = store.get_proposal(pid)["run_id"]
    assert not orders(fs)                                         # a signal never trades by itself

    with TestClient(web.app) as c:
        page = c.get(f"/run/{run_id}").text
        assert f"/run/{run_id}/execute/{pid}" in page and "Execute this trade" in page and "confirm(" in page
        assert c.get(f"/run/{run_id}/execute/{pid}").status_code == 405           # never from a plain link
        assert c.post(f"/run/{run_id + 99}/execute/{pid}").status_code == 404     # proposal must belong to the run
        assert not orders(fs)
        store.set_setting("kill_switch", "on")                                     # limits still apply
        c.post(f"/run/{run_id}/execute/{pid}", follow_redirects=False)
        assert not orders(fs) and store.get_proposal(pid)["status"] == "blocked"
        pg = c.get(f"/run/{run_id}").text
        assert "Blocked by the kill switch" in pg and "/override/" not in pg      # the kill switch can't be overridden
        store.set_setting("kill_switch", "off")

    store.update_proposal(pid, status="signal", error=None)                        # reopen it with the kill switch off
    with TestClient(web.app) as c:
        r = c.post(f"/run/{run_id}/execute/{pid}", follow_redirects=False)
        assert r.status_code == 303
        assert len(orders(fs)) == 1 and store.get_proposal(pid)["status"] == "executed"
        c.post(f"/run/{run_id}/execute/{pid}")                                     # double click: no second order
        assert len(orders(fs)) == 1
    assert any("manually approved from signal" in e["msg"] for e in store.events(50, level="audit"))


def test_manual_execute_refuses_stale_and_none(eng):
    engine, store, sent, fs, rating = eng
    store.set_setting("mode", "signals")
    pid = engine.analyse(policy.WatchItem("META", "META"))["signal"]
    store.update_proposal(pid, created=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds"))
    assert engine.manual_execute(pid)["error"] == "stale" and not orders(fs)
    assert engine.manual_execute(9999)["error"] == "not found"
    rating["v"] = "Hold"
    assert "action" in engine.analyse(policy.WatchItem("META", "META"))             # Hold creates no proposal to execute


def test_manual_execute_approves_pending(eng):
    engine, store, sent, fs, rating = eng
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    assert not orders(fs)
    assert engine.manual_execute(pid, "dashboard")["executed"] and len(orders(fs)) == 1
    assert engine.manual_execute(pid)["error"] == "not executable"                  # already done


def test_preflight_explains_blocks_and_fixes(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    item = policy.WatchItem("META", "META")
    before = len(fs.calls)
    clear = engine.preflight(item)
    buy = clear["sides"][0]
    assert buy["label"] == "Buy" and buy["ok"] and buy["amount"] == 25 and not buy["blocks"]
    assert not orders(fs) and engine.daily_usage()["analyses"] == 0       # no order, no analysis run

    pid = engine.analyse(item)["pending"]
    engine.decide(pid, approve=True)
    store.set_setting("max_trades_per_day", "1")
    blocked = engine.preflight(item)["sides"][0]
    assert not blocked["ok"]
    text = " ".join(blocked["fixes"])
    assert "cooldown" in " ".join(blocked["blocks"]) and "Days between trades" in text and "Wait about" in text
    assert "daily trade limit" in " ".join(blocked["blocks"]) and "Reset daily limits" in text

    store.set_setting("kill_switch", "on")
    assert "kill switch" in " ".join(engine.preflight(item)["sides"][0]["fixes"])
    store.set_setting("kill_switch", "off")
    store.set_setting("allow_sells", "off")
    sell = engine.preflight(item)["sides"][1]
    assert not sell["ok"] and "Allow sells" in " ".join(sell["fixes"])
    store.set_setting("allow_sells", "on")
    store.set_setting("cash_buffer_usd", "100000")
    assert "Cash to keep" in " ".join(engine.preflight(item)["sides"][0]["fixes"])
    assert len(orders(fs)) == 1                                           # only the one approved order, never from the check

    with TestClient(web.app) as c:
        page = c.get("/preflight?symbol=META").text
        assert "blocked" in page and "To allow it" in page and "Run analysis now" in page
        assert "action='/run'" in page.replace(web.B, "") or "/run'" in page
        assert c.get("/preflight?symbol=NOPE").status_code == 404
        home = c.get("/").text
        assert "Check and analyse" in home and "action='/preflight'" in home.replace(web.B, "")
    assert len(orders(fs)) == 1


def test_override_blocked_trade(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    item = policy.WatchItem("META", "META")
    first = engine.analyse(item)["pending"]
    assert engine.decide(first, approve=True)["executed"] and len(orders(fs)) == 1
    out = engine.analyse(item)                                     # same day: the cooldown blocks it
    pid = out["proposal"]
    run_id = store.get_proposal(pid)["run_id"]
    assert store.get_proposal(pid)["status"] == "blocked" and len(orders(fs)) == 1

    with TestClient(web.app) as c:
        page = c.get(f"/run/{run_id}").text
        assert f"/run/{run_id}/override/{pid}" in page and "Override limits and execute" in page and "confirm(" in page
        assert "cooldown" in page and "OVERRIDE your limits" in page
        assert c.get(f"/run/{run_id}/override/{pid}").status_code == 405           # never from a plain link
        assert c.post(f"/run/{run_id + 99}/override/{pid}").status_code == 404
        assert len(orders(fs)) == 1

        store.set_setting("kill_switch", "on")                                     # never waived
        c.post(f"/run/{run_id}/override/{pid}")
        assert len(orders(fs)) == 1 and store.get_proposal(pid)["status"] == "blocked"
        store.set_setting("kill_switch", "off")

        r = c.post(f"/run/{run_id}/override/{pid}", follow_redirects=False)
        assert r.status_code == 303 and len(orders(fs)) == 2
        p = store.get_proposal(pid)
        assert p["status"] == "executed" and p["override"] == 1
        c.post(f"/run/{run_id}/override/{pid}")                                    # double click: no third order
        assert len(orders(fs)) == 2
    log = " ".join(e["msg"] for e in store.events(80, level="audit"))
    assert "LIMITS OVERRIDDEN by user" in log and "waived: cooldown" in log
    # the waiver covered that one trade only
    assert "blocked" in engine.analyse(item)


def test_override_never_waives_hard_limits(eng):
    engine, store, sent, fs, rating = eng
    from app import policy as pol
    base = dict(instrument_id=1, cash=500.0, portfolio_total=1000.0, holding_value=0.0, pending_usd=0.0,
                spent_today=999.0, trades_today=99, last_trade_iso=datetime.now(timezone.utc).isoformat(), tradable=True)
    s = store.get_settings()
    buy = pol.Proposal("buy", 25.0, None, [], "x")
    assert pol.check_limits(buy, s=s, **base)[0] is None                           # blocked without the waiver
    ok, blocks = pol.check_limits(buy, s=s, override=True, **base)
    assert ok and ok.amount_usd == 25.0 and not blocks                             # soft limits waived, size still capped
    big = pol.Proposal("buy", 900.0, None, [], "x")
    assert pol.check_limits(big, s=s, override=True, **base)[0].amount_usd == 25.0   # per-trade maximum still applies
    low = {**base, "cash": 12.0}
    assert pol.check_limits(buy, s=s, override=True, **low)[0].amount_usd == 12.0    # never more than the cash held
    assert pol.check_limits(buy, s=s, override=True, **{**base, "cash": 4.0})[0] is None   # $10 minimum
    assert pol.check_limits(buy, s={**s, "kill_switch": "on"}, override=True, **base)[0] is None
    assert pol.check_limits(buy, s=s, override=True, **{**base, "tradable": False})[0] is None


def test_portfolio_has_live_clock(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    with TestClient(web.app) as c:
        page = c.get("/").text
        assert page.count("data-clock=") == 1
        assert f"data-clock='{web.config.TZ}'" in page
        h2 = page[page.index("Portfolio"):page.index("</h2>", page.index("Portfolio"))]
        assert "<span class=right><span class=clock" in h2              # in the card's top-right slot
        assert "setInterval(tick,1000)" in page                         # ticks every second in the browser
        assert "data-clock" not in c.get("/logs").text.split("<main>")[1].split("</main>")[0]


def test_pages_layout_logs_control_and_portfolio(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    with TestClient(web.app) as c:
        home = c.get("/").text
        for gone in ("Recent calls", "Agent runs", ">Log<", "KILL SWITCH: stop all trading", "Set mode"):
            assert gone not in home
        assert "Portfolio" in home and "section class='card c12'><h2>Portfolio" in home
        assert ">Logs</a>" in home
        logs = c.get("/logs").text
        assert "Recent calls" in logs and "Agent runs" in logs and "Full audit trail" in logs
        st = c.get("/settings").text
        assert "Control" in st and "KILL SWITCH: stop all trading" in st and "Set mode" in st
        r = c.post("/kill", data={"value": "on"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/settings")
        assert store.get_settings()["kill_switch"] == "on"
        assert "Turn kill switch OFF" in c.get("/settings").text
        r = c.post("/mode", data={"mode": "signals"}, follow_redirects=False)
        assert r.headers["location"].endswith("/settings") and store.get_settings()["mode"] == "signals"
        bad = c.post("/settings", data={"watchlist": "META", "max_trade_usd": "abc"})
        assert bad.status_code == 400 and "Turn kill switch OFF" in bad.text      # control still shown on validation errors


def test_watchlist_refreshes_every_15_minutes(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, markets
    importlib.reload(markets)
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    assert markets.REFRESH_MINUTES == 15 and markets.QUOTE_TTL == 900
    calls = {"search": 0, "fail": True}

    class EC:
        def search_item(self, sym):
            calls["search"] += 1
            if sym == "BAD" and calls["fail"]:
                raise RuntimeError("down")
            return {"internalInstrumentId": 5, "internalInstrumentDisplayName": sym, "dailyPriceChange": 1.0,
                    "currentRate": 10, "isCurrentlyTradable": True}
        def daily_candles(self, iid, count=365):
            return [{"date": "d", "open": 1, "high": 12, "low": 8, "close": 10}] * 3
        def rates(self, ids):
            return {5: {"bid": 10, "ask": 10.1}}

    ec, clock = EC(), {"t": datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc).timestamp()}   # Thu 11:00 New York, US open
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    markets.watchlist_rows(ec, ["META", "BAD"])
    assert calls["search"] == 2
    markets.watchlist_rows(ec, ["META", "BAD"])                  # META cached; the failed BAD retried at once
    assert calls["search"] == 3
    calls["fail"] = False
    markets.watchlist_rows(ec, ["META", "BAD"])
    clock["t"] += 14 * 60
    markets.watchlist_rows(ec, ["META", "BAD"])
    assert calls["search"] == 4                                  # still cached after 14 minutes
    clock["t"] += 2 * 60
    markets.watchlist_rows(ec, ["META", "BAD"])
    assert calls["search"] == 6                                  # re-read from eToro after 15 minutes
    assert markets.updated_at() == clock["t"]
    markets.invalidate()
    markets.watchlist_rows(ec, ["META"])
    assert calls["search"] == 7

    monkeypatch.setattr(web.engine, "client", lambda: ec)
    store.set_setting("watchlist", "META,GOOG")
    web.refresh_markets()
    assert calls["search"] == 9                                  # the scheduled job forces a fresh read of both
    web.schedule_jobs()
    job = web.sched.get_job("markets")
    assert job is not None and job.trigger.interval.total_seconds() == 900
    with TestClient(web.app) as c:
        page = c.get("/").text
        assert "http-equiv=refresh content=900" in page and "refreshes from eToro every 15 minutes" in page
        assert "Refresh now" in page
        before = calls["search"]
        assert c.post("/watchlist/refresh", follow_redirects=False).status_code == 303
        c.get("/")
        assert calls["search"] > before                          # manual refresh bypasses the cache
        assert not orders(fs)


def test_market_sessions_and_closed_markets_not_refetched(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import markets
    importlib.reload(markets)
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    # US (EDT, UTC-4 until 1 Nov 2026)
    assert markets.session("META", "Stocks", utc(2026, 10, 29, 14, 0))[0] is True          # 10:00 ET
    assert markets.session("META", "Stocks", utc(2026, 10, 29, 13, 0))[0] is False         # 09:00 ET, pre-open
    closed, last = markets.session("META", "Stocks", utc(2026, 10, 29, 21, 0))             # 17:00 ET
    assert closed is False and last == utc(2026, 10, 29, 20, 0)                            # closed at 16:00 ET = 20:00 UTC
    assert markets.session("META", "Stocks", utc(2026, 10, 31, 15, 0))[0] is False         # Saturday
    assert markets.session("META", "Stocks", utc(2026, 11, 2, 14, 35))[0] is True          # Monday after clocks went back: 09:35 ET
    # London: summer time ended 25 Oct, so 16:30 local = 16:30 UTC
    assert markets.session("IUSA.L", "ETF", utc(2026, 10, 29, 16, 0))[0] is True
    assert markets.session("IUSA.L", "ETF", utc(2026, 10, 29, 16, 45))[0] is False
    assert markets.session("IUSA.L", "ETF", utc(2026, 10, 29, 7, 30))[0] is False
    # crypto never closes; forex-like instruments close Fri 22:00 UTC until Sun 22:00 UTC
    assert markets.session("BTC", "Cryptocurrencies", utc(2026, 10, 31, 3, 0))[0] is True
    assert markets.session("EURUSD", "Currencies", utc(2026, 10, 30, 21, 0))[0] is True
    closed, last = markets.session("EURUSD", "Currencies", utc(2026, 10, 31, 12, 0))
    assert closed is False and last == utc(2026, 10, 30, 22, 0)
    assert markets.session("EURUSD", "Currencies", utc(2026, 11, 1, 23, 0))[0] is True
    # unknown exchange suffix is treated as open so it is never skipped by mistake
    assert markets.session("X.ZZ", "Stocks", utc(2026, 10, 31, 12, 0))[0] is True

    # a closed market is read once after the close, then left alone by the scheduled refresh
    reads = {"n": 0}

    class EC:
        def search_item(self, sym):
            reads["n"] += 1
            return {"internalInstrumentId": 7, "internalInstrumentDisplayName": sym, "internalAssetClassName": "Stocks",
                    "dailyPriceChange": 0.5, "currentRate": 10}
        def daily_candles(self, iid, count=365):
            return [{"date": "d", "open": 1, "high": 12, "low": 8, "close": 10}] * 3
        def rates(self, ids):
            return {7: {"bid": 10, "ask": 10.1}}

    clock = {"t": utc(2026, 10, 29, 21, 0).timestamp()}          # after the US close
    monkeypatch.setattr(markets.time, "time", lambda: clock["t"])
    ec = EC()
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == 1
    clock["t"] += 3600
    markets.invalidate(open_only=True)
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == 1                                       # settled: not refetched while closed
    markets.invalidate()                                         # the manual button still forces it
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == 2
    # data captured while open is refreshed once after the close
    clock["t"] = utc(2026, 10, 30, 19, 0).timestamp()            # Fri 15:00 ET, open
    markets.invalidate()
    markets.watchlist_rows(ec, ["META"])
    n = reads["n"]
    clock["t"] = utc(2026, 10, 30, 20, 5).timestamp()            # just after the close
    markets.invalidate(open_only=True)
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == n + 1
    clock["t"] = utc(2026, 11, 2, 12, 0).timestamp()             # Monday before the open
    markets.invalidate(open_only=True)
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == n + 1
    clock["t"] = utc(2026, 11, 2, 15, 0).timestamp()             # Monday 10:00 ET, open again
    markets.invalidate(open_only=True)
    markets.watchlist_rows(ec, ["META"])
    assert reads["n"] == n + 2


def test_portfolio_shows_price_and_sparkline(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import web, markets
    from app.etoro import Book, Holding
    importlib.reload(markets)
    importlib.reload(web)
    book = Book(cash_usd=100.0, total_usd=1100.0, holdings={
        1: Holding(1, "AAA", units=10, value_usd=500.0, positions=[{"units": 10, "openRate": 40.0, "conv": 1.0}]),
        2: Holding(2, "BBB", units=4, value_usd=500.0)})

    class EC:
        def daily_candles(self, iid, count=365):
            if iid == 2:
                raise RuntimeError("down")
            return [{"date": "d", "open": 1, "high": 60, "low": 40, "close": c} for c in (40, 45, 60)]

    sparks = markets.holding_sparks(EC(), [1, 2])
    assert sparks[1] == [40, 45, 60] and sparks[2] == []
    html = web.portfolio_card(book, web.store.get_settings(), sparks)
    html = web.portfolio_card(book, web.store.get_settings(), sparks,
                              {"AAA": {"open": True, "logo": "http://x/aaa.png", "name": "Triple A"}, "BBB": {"open": False}})
    assert "<img class=logo src='http://x/aaa.png'" in html and "Triple A" in html
    assert html.index("<img class=logo") < html.index("<b>AAA</b>")              # icon leads the row
    assert "<b>AAA</b> <span class='pill okp' title='Market is open'>open</span>" in html
    assert "<b>BBB</b> <span class=pill title='Market is closed" in html
    assert html.count("<span class=logo></span>") >= 2                           # BBB (no logo) and Cash use a placeholder
    bare = web.portfolio_card(book, web.store.get_settings(), sparks, {})
    assert ">open<" not in bare and ">closed<" not in bare                        # unknown: no badge
    from datetime import datetime, timezone
    class MC:
        def search_item(self, sym):
            return {"internalInstrumentId": 1 if sym == "AAA" else 2, "internalAssetClassName": "Stocks" if sym == "AAA" else "Cryptocurrencies"}
        def daily_candles(self, iid, count=365):
            return []
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    assert markets.market_states(MC(), ["AAA", "BBB"], sat) == {"AAA": False, "BBB": True}   # US stock shut Saturday, crypto open
    assert "<th>Price" in html and "<th>30 days</th>" in html
    # ordered by gain vs cost, highest first; no cost basis last
    mix = Book(cash_usd=0.0, total_usd=1000.0, holdings={
        1: Holding(1, "LOSE", units=1, value_usd=80.0, positions=[{"units": 1, "openRate": 100.0, "conv": 1.0}]),
        2: Holding(2, "NOCOST", units=1, value_usd=900.0),
        3: Holding(3, "WIN", units=1, value_usd=150.0, positions=[{"units": 1, "openRate": 100.0, "conv": 1.0}]),
        4: Holding(4, "FLAT", units=1, value_usd=100.0, positions=[{"units": 1, "openRate": 100.0, "conv": 1.0}])})
    order = web.portfolio_card(mix, web.store.get_settings())
    pos = [order.index(f"<b>{s}</b>") for s in ("WIN", "FLAT", "LOSE", "NOCOST")]
    assert pos == sorted(pos)
    assert html.index("<th>Units</th>") < html.index("<th>Value</th>")          # Units sits next to Value
    assert html.index("<th>Value</th>") - html.index("<th>Units</th>") < 30
    assert "▲ +$100.00 (+25.00%)" in html                                        # 500 now vs 10 x 40 paid
    assert html.count("gain/loss") == 1 and html.count("▲") == 1                  # BBB has no cost basis: no P/L shown
    assert "$50.00" in html and "$125.00" in html                 # 500/10 and 500/4 per unit
    assert html.count("<svg class=spark") == 1                    # AAA only; BBB shows n/a
    assert "#22c55e" in html                                      # AAA trended up
    assert "n/a" in html
    assert "<svg" not in web.portfolio_card(book, web.store.get_settings())   # still renders without history


def test_portfolio_refresh_follows_market_state(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, markets
    from app.etoro import Book, Holding
    importlib.reload(markets)
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    sat = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc).timestamp()   # US shut, crypto open
    monkeypatch.setattr(markets.time, "time", lambda: sat)
    calls = {"book": 0}
    held = {"sym": "BTC"}

    class EC:
        def book(self):
            calls["book"] += 1
            return Book(cash_usd=10.0, total_usd=110.0, holdings={
                1: Holding(1, held["sym"], units=1, value_usd=100.0, positions=[{"units": 1, "openRate": 90.0, "conv": 1.0}])})
        def search_item(self, sym):
            return {"internalInstrumentId": 1, "internalInstrumentDisplayName": sym,
                    "internalAssetClassName": "Cryptocurrencies" if sym == "BTC" else "Stocks"}
        def daily_candles(self, iid, count=365):
            return []

    ec = EC()
    s1 = markets.portfolio_snapshot(ec, now=sat)
    assert s1["any_open"] is True and markets.snapshot_ttl(True) == 60
    markets.portfolio_snapshot(ec, now=sat + 59)
    assert calls["book"] == 1                                       # open market: cached for under a minute
    markets.portfolio_snapshot(ec, now=sat + 61)
    assert calls["book"] == 2                                       # then re-read

    markets.invalidate_portfolio()
    held["sym"] = "META"
    markets._cache.clear()
    s2 = markets.portfolio_snapshot(ec, now=sat)
    assert s2["any_open"] is False and markets.snapshot_ttl(False) == 900
    n = calls["book"]
    markets.portfolio_snapshot(ec, now=sat + 600)
    assert calls["book"] == n                                       # everything closed: left alone for 15 minutes
    markets.portfolio_snapshot(ec, now=sat + 901)
    assert calls["book"] == n + 1

    # page: reload interval and the Last refreshed column follow the state
    monkeypatch.setattr(web.engine, "client", lambda: ec)
    markets.invalidate_portfolio()
    held["sym"] = "BTC"
    markets._cache.clear()
    with TestClient(web.app) as c:
        page = c.get("/").text
        assert "http-equiv=refresh content=60" in page
        assert "Last refreshed" not in page and "every 60 seconds while a market you hold is open" in page
        n = calls["book"]
        c.get("/")
        assert calls["book"] == n                                   # second load inside the minute uses the snapshot
        c.post("/kill", data={"value": "off"})                      # any action drops the snapshot
        c.get("/")
        assert calls["book"] == n + 1
        markets.invalidate_portfolio()
        held["sym"] = "META"
        markets._cache.clear()
        assert "http-equiv=refresh content=900" in c.get("/").text


def test_portfolio_hourly_sparkline(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import web, markets, etoro
    from app.etoro import Book, Holding
    importlib.reload(markets)
    importlib.reload(web)

    # client: one-minute candles, newest first from eToro, returned oldest first
    seen = {}

    class Resp(etoro.EtoroClient):
        def __init__(self):
            pass
        def _req(self, method, path, **kw):
            seen["path"] = path
            return {"candles": [{"candles": [{"fromDate": f"2026-10-31T17:{m:02d}:00Z", "open": 1, "high": 2, "low": 1, "close": m}
                                             for m in (3, 2, 1)]}]}
    cs = Resp().intraday_candles(100000, 60)
    assert seen["path"].endswith("/instruments/100000/history/candles/desc/OneMinute/60")
    assert [c["close"] for c in cs] == [1, 2, 3]

    class EC:
        def intraday_candles(self, iid, minutes=60):
            if iid == 3:
                raise RuntimeError("down")
            vals = (5, 6, 7) if iid == 1 else (9, 8, 7)
            return [{"date": f"2026-10-30T19:{57 + i}:00Z", "close": c} for i, c in enumerate(vals)]
    got = markets.holding_intraday(EC(), [1, 2, 3])
    last_trade_end = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc).timestamp()   # last candle 19:59 ends 20:00
    assert got[1] == {"closes": [5, 6, 7], "asof": last_trade_end}
    assert got[2]["closes"] == [9, 8, 7] and got[3] == {"closes": [], "asof": None}

    class Live:
        def intraday_candles(self, iid, minutes=60):
            return [{"date": "2026-10-31T17:35:00Z", "close": 1}]      # current minute, still in progress
    monkeypatch.setattr(markets.time, "time", lambda: datetime(2026, 10, 31, 17, 35, 20, tzinfo=timezone.utc).timestamp())
    assert markets.holding_intraday(Live(), [1])[1]["asof"] == datetime(2026, 10, 31, 17, 35, 20, tzinfo=timezone.utc).timestamp()

    book = Book(cash_usd=0.0, total_usd=300.0, holdings={
        i: Holding(i, n, units=1, value_usd=100.0, positions=[{"units": 1, "openRate": 90.0, "conv": 1.0}])
        for i, n in ((1, "UP"), (2, "DOWN"), (3, "NONE"))})
    info = {"UP": {"open": True}, "DOWN": {"open": False}, "NONE": {"open": True}}
    html = web.portfolio_card(book, web.store.get_settings(), {}, info, None, got)
    assert "<th>Price as of</th>" in html and "Last refreshed" not in html
    t = datetime.fromtimestamp(last_trade_end, web.config_tz())
    assert f"Price as of {t.strftime('%Y-%m-%d %H:%M:%S %Z')}" in html
    assert "market closed, so the price is the last trade" in html                   # DOWN is closed
    assert html.count("Price as of 2026") == 2                                       # UP and DOWN; NONE has none
    assert "<th>60 min</th>" in html and html.index("<th>60 min</th>") < html.index("<th>30 days</th>")
    assert html.index("Price <span") < html.index("<th>60 min</th>")                 # directly after the price column
    assert html.count("<svg class=spark") == 2                                       # UP and DOWN; NONE has no data
    assert "title='Last 60 minutes, one-minute prices'" in html
    assert "title='Market closed: its final hour of trading'" in html
    assert "#22c55e" in html and "#ef4444" in html                                  # up is green, down is red
    # header and rows have the same number of cells (Cash row included)
    cells = [r.count("<td") for r in html.split("<tr>")[2:]]
    assert len(set(cells)) == 1 and cells[0] == html.split("<tr>")[1].count("<th>")


def test_settings_sections_and_inline_validation(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    good = {k: store.get_settings()[k] for k in ["watchlist", *web.NUMERIC, *web.TIME_FIELDS]}
    assert web.validate_settings(good) == {}
    bads = web.validate_settings({**good, "schedule_time": "25:99", "crypto_time": "9am", "max_trade_usd": "abc",
                                  "max_position_pct": "150", "cash_buffer_usd": "-1", "analyses_per_day": "nan"})
    assert set(bads) == {"schedule_time", "crypto_time", "max_trade_usd", "max_position_pct",
                         "cash_buffer_usd", "analyses_per_day"}
    with TestClient(web.app) as c:
        page = c.get("/settings").text
        for title in ("Schedule", "Trading limits", "Approvals and sells", "Watchlist"):
            assert title in page
        assert "% of portfolio" in page and "Cost control" in page
        r = c.post("/settings", data={**good, "max_trade_usd": "abc", "schedule_time": "99:00", "analyses_per_day": "5"})
        assert r.status_code == 400
        assert "Enter a number." in r.text and "Use 24-hour HH:MM" in r.text and "Not saved: fix the 2 highlighted" in r.text
        assert "value='abc'" in r.text and "value='5'" in r.text           # user input kept
        assert store.get_settings()["analyses_per_day"] != "5"            # nothing saved
        r = c.post("/settings", data={**good, "analyses_per_day": "3", "allow_sells": "on"}, follow_redirects=False)
        assert r.status_code == 303 and store.get_settings()["analyses_per_day"] == "3"
        assert store.get_settings()["auto_sells"] == "off"                # unticked toggle -> off


def test_create_test_trade_only_in_dry_run(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import config
    assert "error" in engine.create_test("META", "buy")          # DRY_RUN off -> refused
    monkeypatch.setattr(config, "DRY_RUN", True)
    out = engine.create_test("META", "buy")
    p = store.get_proposal(out["pending"])
    assert p["rating"] == "Buy (TEST)" and p["run_id"] is None and p["amount_usd"] == 25
    res = engine.decide(p["id"], approve=True)
    assert res["executed"] and res["orders"] == ["dry-run"] and not orders(fs)   # nothing sent
    assert any("TEST" in t for k, t in sent if k == "sms")
    store.set_setting("mode", "auto")
    store.set_setting("cooldown_days", "0")
    out = engine.create_test("META", "sell")
    assert "pending" in out                                       # auto mode: sells still need approval


def test_audit_trail(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    pid = engine.analyse(policy.WatchItem("META", "META"))["pending"]
    p = store.get_proposal(pid)
    with TestClient(web.app) as c:
        c.post(f"/p/{pid}/{engine.token_for(pid, p['created'])}/reject", headers={"x-forwarded-for": "81.2.3.4"})
        lines = [e["msg"] for e in store.events(20, level="audit")]
        assert any(f"proposal #{pid} created, awaiting approval" in l and "BUY META $25.00" in l for l in lines)
        assert any(f"proposal #{pid} rejected" in l and "81.2.3.4" in l for l in lines)
        assert "81.2.3.4" in c.get("/audit").text
        csv_text = c.get("/audit.csv").text
        assert csv_text.startswith("timestamp_utc,event") and f"proposal #{pid}" in csv_text


def test_scheduled_run_logs_skip_and_start(eng):
    engine, store, sent, fs, rating = eng
    rating["v"] = "Hold"
    store.set_setting("analyses_per_day", "1")
    engine.scheduled_analysis("crypto")
    assert any("scheduled crypto run started: BTC" in e["msg"] for e in store.events(10))
    engine.scheduled_analysis("crypto")
    assert any("skipped: daily analysis limit reached (1 of 1" in e["msg"] for e in store.events(10))


def test_find_instrument_v2_fallback():
    from datetime import datetime, timezone
    from app.etoro import EtoroClient

    class S:
        def request(self, method, url, params=None, **kw):
            if "/search" in url:
                return Resp(200, {"items": []})
            if "/v2/market-data/instruments" in url:
                return Resp(200, {"results": [{"instrumentId": 1037364, "symbol": "IUCS.L", "type": "ETF",
                                               "displayName": "iShares Consumer Staples"}]})
            if "/rates" in url:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")
                return Resp(200, {"rates": [{"instrumentID": 1037364, "ask": 10.95, "date": now}]})
            return Resp(404, {})

    ec = EtoroClient(api_key="a", user_key="b", session=S())
    inst = ec.find_instrument("iucs.l")
    assert inst["instrumentId"] == 1037364 and inst["tradable"] and inst["rate"] == 10.95


def test_ai_model_settings(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import analyst, config, web
    monkeypatch.setattr(web.sched, "start", lambda: None)
    # defaults: cheap model for the 10 evidence/debate agents, Sonnet for the 2 deciders and the scan
    assert config.model_for("quick") == "claude-haiku-5-5"
    assert config.model_for("deep") == "claude-sonnet-5" and config.model_for("opp") == "claude-sonnet-5"
    cfg = analyst.build_config()
    assert cfg["quick_think_llm"] == "claude-haiku-5-5" and cfg["deep_think_llm"] == "claude-sonnet-5"
    good = {k: store.get_settings()[k] for k in ["watchlist", *web.NUMERIC, *web.TIME_FIELDS]}
    with TestClient(web.app) as c:
        assert "AI models" in c.get("/settings").text
        r = c.post("/settings", data={**good, "quick_model": "gpt-yolo"})
        assert r.status_code == 400 and "Pick a model" in r.text
        assert store.get_settings()["quick_model"] == "claude-haiku-5-5"
        r = c.post("/settings", data={**good, "quick_model": "claude-sonnet-5"}, follow_redirects=False)
        assert r.status_code == 303 and config.model_for("quick") == "claude-sonnet-5"
        r = c.post("/settings", data=good, follow_redirects=False)       # omitted model fields keep their values
        assert r.status_code == 303 and config.model_for("quick") == "claude-sonnet-5"


def test_account_rules_and_go_live_limits(eng):
    engine, store, sent, fs, rating = eng
    from app import analyst, policy
    s = store.get_settings()
    rules = policy.account_rules(s)
    assert "$25" in rules and "$12.50" in rules and "market orders" in rules and "40% of the" in rules
    ctx = analyst.account_context({"cash": 300.0, "currency": "USD", "positions": []}, rules)
    block = ctx.render("META")
    assert block.startswith("Portfolio at the analysis date:") and "Cash available: 300.00 USD" in block
    assert block.rstrip().endswith("will not be followed.")
    # the go-live change only tightens, and only once
    store.set_setting("live_limits_applied", "")
    store.set_setting("max_trades_per_day", "5")
    store.set_setting("max_position_pct", "20")      # already stricter than 25: left alone
    store.set_setting("auto_sells", "on")
    engine.apply_live_limits_once()
    s = store.get_settings()
    assert (s["max_trades_per_day"], s["max_position_pct"], s["cash_buffer_usd"], s["auto_sells"]) == ("3", "20", "50", "off")
    store.set_setting("max_trades_per_day", "6")      # a later edit by the user is respected
    engine.apply_live_limits_once()
    assert store.get_settings()["max_trades_per_day"] == "6"


def test_asset_mismatch_blocks_and_watchlist_fix(eng):
    engine, store, sent, fs, rating = eng
    from app import policy
    item = policy.parse_watchlist("CVX")[0]
    with pytest.raises(engine.EtoroError, match="Convex Finance"):
        engine._check_asset_match(item, {"name": "Convex Finance", "assetClass": "Crypto"})
    engine._check_asset_match(item, {"name": "Chevron", "assetClass": "Stocks"})          # fine
    btc = policy.parse_watchlist("BTC=BTC-USD@crypto")[0]
    engine._check_asset_match(btc, {"name": "Bitcoin", "assetClass": "Crypto"})            # fine
    store.set_setting("watchlist_fix_applied", "")
    store.set_setting("watchlist", "IUCS.L,META,BTC=BTC-USD@crypto,xlv,CVX,CAT")
    engine.apply_watchlist_fix_once()
    assert store.get_settings()["watchlist"] == "IUCS.L,META,BTC=BTC-USD@crypto,CVX.US=CVX,CAT"
    store.set_setting("watchlist", "CVX")            # runs once only; later edits are left alone
    engine.apply_watchlist_fix_once()
    assert store.get_settings()["watchlist"] == "CVX"


def test_scheduled_hold_is_emailed(eng):
    engine, store, sent, fs, rating = eng
    from app import policy
    rating["v"] = "Hold"
    engine.analyse(policy.parse_watchlist("META")[0])                 # scheduled (not manual)
    assert ("email", "META: Hold (scheduled)") in sent
    engine.analyse(policy.parse_watchlist("META")[0], manual=True)
    assert ("email", "META: Hold") in sent


def test_held_crypto_is_recognised_by_research_ticker(eng):
    engine, store, sent, fs, rating = eng
    from types import SimpleNamespace
    from app import analyst
    store.set_setting("watchlist", "META,BTC=BTC-USD@crypto,CVX.US=CVX")
    book = SimpleNamespace(cash_usd=372.68, holdings={
        1: SimpleNamespace(symbol="BTC", units=0.000933, positions=[{"openRate": 73054.62, "units": 0.000933}]),
        2: SimpleNamespace(symbol="CVX.US", units=0.5, positions=[{"openRate": 230.0, "units": 0.5}]),
        3: SimpleNamespace(symbol="MU", units=0.08665, positions=[{"openRate": 1038.65, "units": 0.08665}])})
    ctx = analyst.account_context(engine._portfolio_context(book))
    assert "Current position in BTC-USD: 0.000933 units" in ctx.render("BTC-USD")
    assert "Current position in CVX: 0.5 units" in ctx.render("CVX")
    assert "No current position in META" in ctx.render("META") and "MU" in ctx.render("META")


def test_daily_summary_lists_analyses_trades_outside_buys_and_queue(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app import notify
    store.set_setting("watchlist", "META,GOOG,LLY,BTC=BTC-USD@crypto")
    store.set_setting("analyses_per_day", "3")
    rid = store.start_run("GOOG"); store.finish_run(rid, "Hold", "x")
    store.log("info", "manual analysis of LLY started")
    rid2 = store.start_run("LLY"); store.finish_run(rid2, "Overweight", "x")
    pid = store.add_proposal({"run_id": rid2, "ticker": "LLY", "instrument_id": 1567, "action": "buy", "amount_usd": 25,
                              "units": None, "position_ids": [], "rating": "Overweight", "mode": "auto",
                              "status": "failed", "reason": ""})
    store.update_proposal(pid, error="400: Exactly one of Symbol or InstrumentID")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.013")
    book = SimpleNamespace(total_usd=786.0, cash_usd=347.68, holdings={
        1567: SimpleNamespace(instrument_id=1567, symbol="LLY", value_usd=24.95, pending_usd=0, units=0.0177,
                              positions=[{"units": 0.017692, "openRate": 1413.0, "conv": 1, "opened": now}]),
        1003: SimpleNamespace(instrument_id=1003, symbol="META", value_usd=146.6, pending_usd=0, units=0.24,
                              positions=[{"units": 0.24, "openRate": 559.0, "conv": 1, "opened": "2026-08-03T00:05:07"}])})
    monkeypatch.setattr(engine, "client", lambda: SimpleNamespace(book=lambda: book))
    got = {}
    monkeypatch.setattr(engine.notify, "email", lambda subj, body, html=None: got.update(subj=subj, body=body))
    engine.daily_summary()
    body = got["body"]
    assert "Analyses today: 2 (daily limit 3" in body
    assert "GOOG: Hold (scheduled)" in body and "LLY: Overweight (manual)" in body
    assert "#1 BUY LLY $25.00 | Overweight -> failed (400: Exactly one" in body
    assert "Bought outside the app today" in body and "LLY: 1 position(s), about $25.00" in body
    assert "META: 1 position" not in body
    assert "Next share run: META, GOOG" in body            # never-analysed META first, then least recent



def test_watch_levels_record_check_and_priority(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from datetime import datetime, timezone
    from app import levels, policy
    reply = {"v": '{"conditions": [{"type": "close_above", "level": 11.015}, {"type": "zone", "low": 10.46, '
                  '"high": 10.68}, {"type": "close_above", "level": 999}], "invalid_below": 10.17, "horizon_days": 60}'}
    monkeypatch.setattr(levels, "call_model", lambda prompt: reply["v"])
    got = levels.record("IUCS.L", 1, "report text", 10.95)
    assert [(g["kind"], g["low"], g["high"]) for g in got] == [("close_above", 11.015, 11.015), ("zone", 10.46, 10.68)]
    assert all(g["invalid_below"] == 10.17 for g in got)              # 999 dropped as implausible
    act = levels.active("IUCS.L")
    assert len(act) == 2 and act[0]["expires"][:10] > act[0]["created"][:10]

    class EC:
        bars = []
        def find_instrument(self, s):
            return {"instrumentId": 1037364}
        def candles(self, iid, interval, n):
            return self.bars
    ec = EC()
    now = datetime(2026, 11, 5, 6, 30, tzinfo=timezone.utc)
    ec.bars = [{"date": "2026-11-04T00:00:00Z", "open": 10.9, "high": 11.0, "low": 10.85, "close": 10.98},
               {"date": "2026-11-05T00:00:00Z", "open": 11.2, "high": 11.3, "low": 11.1, "close": 11.25}]  # today: ignored
    ch = levels.check(ec, now)
    assert ch["triggered"] == [] and levels.active("IUCS.L")[0]["last_close"] == 10.98
    ec.bars[0].update(close=11.05)                                    # completed day closes above 11.015
    ch = levels.check(ec, now)
    assert [c[1]["kind"] for c in ch["triggered"]] == ["close_above"]
    assert levels.triggered_tickers() == ["IUCS.L"]
    note = levels.trigger_note("IUCS.L")
    assert "Earlier condition met" in note and "closed 11.05 on 2026-11-04" in note
    # a met level jumps the queue
    items = policy.parse_watchlist("META,GOOG,IUCS.L")
    assert [i.symbol for i in levels.prioritise(items)] == ["IUCS.L", "META", "GOOG"]
    # a close below the invalidation cancels what is still watching
    ec.bars[0].update(close=10.10, low=10.05)
    ch = levels.check(ec, now)
    assert len(ch["cancelled"]) == 2
    assert levels.active("IUCS.L") == []
    # a new analysis replaces earlier conditions
    reply["v"] = '{"conditions": [{"type": "zone", "low": 10.5}]}'
    levels.record("IUCS.L", 2, "t", 10.6)
    assert [(lv["kind"], lv["low"], lv["high"], lv["invalid_below"]) for lv in levels.active("IUCS.L")] == [("zone", 10.5, 10.5, None)]


def test_watch_level_feeds_the_agents_and_is_used_up(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import levels, policy
    monkeypatch.setattr(levels, "call_model", lambda p: '{"conditions": [{"type": "close_above", "level": 605}]}')
    levels.record("META", 1, "t", 600)
    lv = levels.active("META")[0]
    levels._set(lv["id"], status="triggered", trigger_info="closed 610 on 2026-11-04")
    seen = {}
    monkeypatch.setattr(engine.analyst, "analyze", lambda t, a, p, rules="": seen.update(rules=rules) or ("Hold", "x"))
    monkeypatch.setattr(levels, "call_model", lambda p: '{"conditions": []}')
    engine.analyse(policy.parse_watchlist("META")[0])
    assert "Earlier condition met" in seen["rules"] and "closed 610" in seen["rules"]
    assert levels.active("META") == []                                # used up by the new analysis



def test_watch_levels_backfill_from_saved_reports(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import levels, policy
    monkeypatch.setattr(levels, "call_model",
                        lambda p: '{"conditions": [{"type": "zone", "low": 590, "high": 600}], "horizon_days": 30}'
                        if "META report" in p else '{"conditions": []}')
    r1 = store.start_run("META"); store.finish_run(r1, "Hold", "META report: wait for 590-600")
    r2 = store.start_run("GOOG"); store.finish_run(r2, "Overweight", "GOOG report")        # acted at the time: skipped
    r3 = store.start_run("MU"); store.finish_run(r3, None, None, error="boom")             # failed: skipped
    out = levels.backfill(type("EC", (), {"find_instrument": lambda self, s: {"rate": 600.0}})(),
                          policy.parse_watchlist("META,GOOG,MU,VRT"))
    assert out == ["META (run #1): pull-back into 590–600"]
    lv = levels.active("META")
    assert len(lv) == 1 and lv[0]["run_id"] == r1
    assert levels.backfill(type("EC", (), {"find_instrument": lambda self, s: {"rate": 600.0}})(),
                           policy.parse_watchlist("META")) == []                          # already has one


def test_daily_performance_day_change_and_vs_cost(eng):
    engine, store, sent, fs, rating = eng
    from datetime import datetime, timezone
    from app import performance
    from app.etoro import Book, Holding
    now = datetime(2026, 11, 4, 21, 30, tzinfo=timezone.utc)
    meta = Holding(1003, "META", units=0.25, value_usd=0.25 * 610,
                   positions=[{"positionID": 1, "units": 0.25, "openRate": 559.0, "conv": 1, "opened": "2026-08-03T00:05:07"}])
    lly = Holding(1567, "LLY", units=0.02, value_usd=0.02 * 1420,
                  positions=[{"positionID": 2, "units": 0.02, "openRate": 1413.0, "conv": 1, "opened": "2026-11-04T15:08:35.013"}])
    book = Book(cash_usd=300.0, total_usd=300 + 152.5 + 28.4, holdings={1003: meta, 1567: lly})

    class EC:
        def candles(self, iid, interval, n):
            prev = {1003: 600.0, 1567: 1400.0}[iid]
            return [{"date": "2026-11-03T00:00:00Z", "open": 1, "high": 1, "low": 1, "close": prev},
                    {"date": "2026-11-04T00:00:00Z", "open": 1, "high": 1, "low": 1, "close": 999}]   # today: ignored
    perf = performance.compute(EC(), book, now)
    r = {x["symbol"]: x for x in perf["rows"]}
    assert r["META"]["day"] == pytest.approx(0.25 * 10) and r["META"]["day_pct"] == pytest.approx(10 / 600 * 100)
    assert r["META"]["pnl"] == pytest.approx(0.25 * (610 - 559)) and r["META"]["cost"] == pytest.approx(139.75)
    assert r["LLY"]["day"] == pytest.approx(0.02 * 7)                 # bought today: from open price, not prior close
    assert "bought today" in r["LLY"]["note"]
    assert perf["day"] == pytest.approx(2.5 + 0.14)
    assert performance.snapshot(perf, "2026-11-03") is None
    store_total = perf["total"]
    perf["total"] += 5
    prev = performance.snapshot(perf, "2026-11-04")
    assert prev["day"] == "2026-11-03" and prev["total"] == pytest.approx(store_total)
    txt = "\n".join(performance.text(perf, prev))
    assert "Holdings on the day: +$2.64" in txt and "Total vs 2026-11-03: +$5.00" in txt
    assert "META: $152.50 | today +$2.50 (+1.7%) | +$12.75 (+9.1%) vs cost $139.75" in txt
    assert "<table" in performance.html(perf, prev) and "#137333" in performance.html(perf, prev)


def test_seeded_previous_day_is_used_for_comparison(eng):
    engine, store, sent, fs, rating = eng
    from app import performance
    performance.seed_once()
    performance.seed_once()                                             # idempotent
    perf = {"rows": [], "total": 800.0, "cash": 419.58}
    prev = performance.snapshot(perf, "2026-11-04")
    assert prev["day"] == "2026-11-03" and prev["total"] == 794.06
    assert "Total vs 2026-11-03: +$5.94" in "\n".join(performance.text({**perf, "invested_value": 380.42, "day": 0,
                                                                       "day_pct": None, "pnl": None}, prev))


def test_scorecard_returns_vs_benchmark_and_hit_rates(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from datetime import datetime, timezone, timedelta
    from app import scorecard
    scorecard.clear_cache()
    days = [(datetime(2026, 9, 1) + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z") for i in range(80)]
    series = {"VWRL.L": [100 + i * 0.1 for i in range(80)],          # benchmark +0.1/day
              "AAA": [10 + i * 0.1 for i in range(80)],              # strong riser
              "BBB": [50 - i * 0.2 for i in range(80)]}              # faller
    ids = {"VWRL.L": 1, "AAA": 2, "BBB": 3}

    class EC:
        def find_instrument(self, s):
            return {"instrumentId": ids[s]}
        def candles(self, iid, interval, n):
            sym = {v: k for k, v in ids.items()}[iid]
            return [{"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in zip(days, series[sym])]

    def run(t, rating, day, price=None, models=None):
        return {"id": len(runs) + 1, "ticker": t, "rating": rating, "started": f"{day}T15:05:00+00:00",
                "price": price, "models": models}
    runs = []
    runs.append(run("AAA", "Buy", "2026-09-02"))                       # rises faster than benchmark: worked
    runs.append(run("BBB", "Overweight", "2026-09-02", models="claude-haiku-5-5/claude-sonnet-5"))   # falls: did not
    runs.append(run("BBB", "Sell", "2026-09-03"))                      # falls: worked
    runs.append(run("AAA", "Hold", "2026-09-02"))                      # not scored
    runs.append(run("AAA", "Buy", "2026-11-18", models="claude-haiku-5-5/claude-sonnet-5"))   # too recent: pending
    runs.append({"id": 99, "ticker": "AAA", "rating": None, "started": "2026-09-02T15:05:00+00:00"})  # failed run: skipped
    now = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
    rows = scorecard.evaluate(EC(), runs, now)
    assert len(rows) == 5
    r0 = rows[0]
    assert r0["start"] == pytest.approx(10.1) and r0["returns"][5] == pytest.approx((10.6 / 10.1 - 1) * 100)
    assert r0["vs"][5] == pytest.approx((10.6 / 10.1 - 1) * 100 - (100.6 / 100.1 - 1) * 100)
    assert scorecard.worked(rows[0], 20) is True and scorecard.worked(rows[1], 20) is False
    assert scorecard.worked(rows[2], 20) is True and scorecard.worked(rows[3], 20) is None
    assert rows[4]["returns"] == {}                                    # pending
    g = {x["name"]: x for x in scorecard.by_rating(rows)}
    assert g["Buy"]["count"] == 2 and g["Buy"]["scored"][5] == 1 and g["Buy"]["hits"][5] == 100 and g["Buy"]["pending"][5] == 1
    m = {x["name"]: x for x in scorecard.by_models(rows)}
    assert set(m) == {"Sonnet 5 / Sonnet 5", "Haiku 5.5 / Sonnet 5"}
    assert "Buy/Overweight calls after 5 trading days" in scorecard.headline(rows) and "50% beat it (2 calls)" in scorecard.headline(rows)
    # recorded price at the time of the call wins over the day's close
    scorecard.clear_cache()
    rows = scorecard.evaluate(EC(), [run("AAA", "Buy", "2026-09-02", price=10.0)], now)
    assert rows[0]["start"] == 10.0


def test_scorecard_page_renders(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, scorecard
    monkeypatch.setattr(web.sched, "start", lambda: None)
    monkeypatch.setattr(scorecard, "evaluate", lambda ec, runs, now=None: [])
    with TestClient(web.app) as c:
        r = c.get("/scorecard")
        assert r.status_code == 200 and "Does it work?" in r.text and "Not enough history yet" in r.text
        assert "/scorecard" in c.get("/").text                           # in the top menu


def test_watchlist_editor_add_checks_etoro_and_delete(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, markets
    from app.etoro import EtoroError
    monkeypatch.setattr(web.sched, "start", lambda: None)
    known = {"NVDA": {"instrumentId": 1137, "name": "NVIDIA", "assetClass": "Stocks", "buyEnabled": True},
             "ETH": {"instrumentId": 100001, "name": "Ethereum", "assetClass": "Crypto", "buyEnabled": True},
             "CVX.US": {"instrumentId": 1014, "name": "Chevron", "assetClass": "Stocks", "buyEnabled": True},
             "XLV": {"instrumentId": 3017, "name": "Health Care SPDR", "assetClass": "ETF", "buyEnabled": True},
             "IUHC.L": {"instrumentId": 1036574, "name": "iShares Health Care", "assetClass": "ETF", "buyEnabled": True}}

    class EC:
        def find_instrument(self, s):
            if s not in known:
                raise EtoroError(f"instrument {s!r} not found on eToro")
            return known[s]
    monkeypatch.setattr(engine, "client", lambda: EC())
    monkeypatch.setattr(markets, "watchlist_rows", lambda ec, syms: [{"symbol": x, "name": x + " name", "price": 10.0,
                                                                     "change_pct": 1.5, "asset_class": "Stocks"} for x in syms])
    monkeypatch.setattr(markets, "portfolio_snapshot", lambda ec: {"book": type("B", (), {"holdings": {}})()})
    store.set_setting("watchlist", "META,BTC=BTC-USD@crypto")
    with TestClient(web.app) as c:
        page = c.get("/settings").text
        assert "META name" in page and "researched as BTC-USD" in page and "$10.00" in page and "Delete" in page
        assert "<textarea name=watchlist" not in page                      # no longer free text
        r = c.post("/watchlist/add", data={"symbol": "nvda"}, follow_redirects=False)
        assert r.status_code == 303 and "Added%20NVDA" in r.headers["location"]
        r = c.post("/watchlist/add", data={"symbol": "ETH"}, follow_redirects=False)
        r = c.post("/watchlist/add", data={"symbol": "CVX.US"}, follow_redirects=False)
        r = c.post("/watchlist/add", data={"symbol": "IUHC.L"}, follow_redirects=False)  # London ETF: fine
        assert store.get_settings()["watchlist"] == "META,BTC=BTC-USD@crypto,NVDA,ETH=ETH-USD@crypto,CVX.US=CVX,IUHC.L"
        r = c.post("/watchlist/add", data={"symbol": "NOPE"})
        assert r.status_code == 400 and "not found on eToro, so it was not added" in r.text
        r = c.post("/watchlist/add", data={"symbol": "META"})
        assert r.status_code == 400 and "already on the watchlist" in r.text
        r = c.post("/watchlist/add", data={"symbol": "XLV"})
        assert r.status_code == 400 and "CFD" in r.text and "Add anyway" in r.text
        assert "XLV" not in store.get_settings()["watchlist"]
        c.post("/watchlist/add", data={"symbol": "XLV", "force": "1"})
        assert store.get_settings()["watchlist"].endswith(",XLV")
        r = c.post("/watchlist/remove", data={"symbol": "btc"}, follow_redirects=False)
        assert r.status_code == 303 and "BTC" not in store.get_settings()["watchlist"]
        # saving other settings leaves the watchlist alone
        good = {k: store.get_settings()[k] for k in [*web.NUMERIC, *web.TIME_FIELDS]}
        before = store.get_settings()["watchlist"]
        assert c.post("/settings", data=good, follow_redirects=False).status_code == 303
        assert store.get_settings()["watchlist"] == before


def test_opportunity_prompt_has_account_context_and_medium_horizon(eng):
    engine, store, sent, fs, rating = eng
    from app import opportunities
    from app.etoro import Book, Holding
    s = {**store.get_settings(), "watchlist": "META,IUHC.L,BTC=BTC-USD@crypto", "max_trade_usd": "75", "max_position_pct": "25"}
    book = Book(cash_usd=419.58, total_usd=800.0, holdings={
        1: Holding(1, "META", units=0.12, value_usd=80.0), 2: Holding(2, "MU", units=0.08, value_usd=64.0)})
    p = opportunities.build_prompt(s, book, today="2026-11-06")
    assert "the medium term, about 1-6 months" in p and "a few days to four weeks" not in p
    assert "UK eToro account" in p and "do NOT suggest them" in p and "IUHC.L" in p
    assert "at most $75" in p and "25% of the portfolio" in p and "$420 cash" in p
    assert "META 10%, MU 8%" in p and "Already on the watchlist: META, IUHC.L, BTC" in p
    assert "At least 2 of the 5" in p and "outside technology and semiconductors" in p
    assert "Buys only, unleveraged" in p and '"sector"' in p
    assert "{" not in opportunities.build_prompt(s, None, today="x").split("Return ONLY")[0]   # all placeholders filled



def test_opportunity_short_horizon_prompt(eng):
    engine, store, sent, fs, rating = eng
    from app import opportunities
    s = store.get_settings()
    p = opportunities.build_prompt(s, None, today="2026-11-06", horizon="short")
    assert "the short term, roughly a few days to four weeks" in p and "spread on the way in and out" in p
    assert "e.g. 1-2 weeks, 2-4 weeks" in p and "UK eToro account" in p and "Buys only" in p
    assert "within days to a few weeks" in opportunities.system_for("short")
    assert "over months rather than days" in opportunities.system_for("medium")
    assert opportunities.build_prompt(s, None, today="x", horizon="bogus") == opportunities.build_prompt(s, None, today="x")


def test_short_term_idea_gets_agent_note_and_stops_on_the_order(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import terms, policy, config
    store.set_setting("mode", "auto")
    store.set_setting("watchlist", "META,GOOG")
    terms.set_term("META", terms.SHORT, "test")
    assert terms.term("META") == "short" and terms.term("GOOG") == "medium"
    seen = {}
    decision = "Rating: Buy ... Stop-loss: $570.00 and Take-profit: 660 over the next 2-3 weeks."
    monkeypatch.setattr(engine.analyst, "analyze",
                        lambda t, a, p, rules="": seen.update(rules=rules) or ("Buy", decision))
    monkeypatch.setattr(terms, "call_model", lambda p: "{}")
    fs.calls.clear()
    engine.analyse(policy.parse_watchlist("META")[0])
    assert "SHORT TERM" in seen["rules"] and "Stop-loss: <price>" in seen["rules"]
    body = [c for c in fs.calls if c[0] == "POST"][-1][3]
    assert body["stopLossRate"] == 570.0 and body["takeProfitRate"] == 660.0 and "symbol" not in body
    p = store.proposals(None, 1)[0]
    assert p["term"] == "short" and "stop 570" in engine._describe(p) and "take-profit 660" in engine._describe(p)
    # a medium-term symbol is analysed without the note and its buy carries no levels
    terms.set_term("META", "medium")
    store.set_setting("cooldown_days", "0")
    fs.calls.clear()
    engine.analyse(policy.parse_watchlist("META")[0])
    assert "SHORT TERM" not in seen["rules"]
    posts = [c for c in fs.calls if c[0] == "POST"]
    assert not posts or "stopLossRate" not in posts[-1][3]


def test_stop_levels_sanity_and_live_recheck(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from app import terms
    monkeypatch.setattr(terms, "call_model", lambda p: '{"stop_loss": 300, "take_profit": 2000}')
    sl, tp, note = terms.stops_from("META", "no levels in text", 600.0)
    assert sl is None and tp is None and "ignored" in note                # 50% below / 230% above: rejected
    sl, tp, note = terms.stops_from("META", "Stop-loss 585, take-profit 650", 600.0)
    assert (sl, tp) == (585.0, 650.0)
    # at execution, a stop the price has already fallen through is dropped rather than closing instantly
    assert engine._live_stops({"stop_loss": 585.0, "take_profit": 650.0}, {"rate": 584.0}) == (None, 650.0)
    assert engine._live_stops({"stop_loss": 585.0, "take_profit": 650.0}, {"rate": 600.0}) == (585.0, 650.0)


def test_watchlist_term_toggle_and_opportunity_short_tag(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, markets, terms
    monkeypatch.setattr(web.sched, "start", lambda: None)
    monkeypatch.setattr(markets, "watchlist_rows", lambda ec, syms: [{"symbol": x, "name": x, "price": 1.0} for x in syms])
    monkeypatch.setattr(markets, "portfolio_snapshot", lambda ec: {"book": type("B", (), {"holdings": {}})()})
    store.set_setting("watchlist", "META")
    with TestClient(web.app) as c:
        assert "Make short term" in c.get("/settings").text
        c.post("/watchlist/term", data={"symbol": "META", "term": "short"})
        assert terms.term("META") == "short" and "short term</span>" in c.get("/settings").text
        c.post("/watchlist/remove", data={"symbol": "META"})
        assert terms.term("META") == "medium"                              # removing forgets the tag


def test_daytrader_buckets_and_merge():
    from app import daytrader as dt
    w = {"buckets": {}}
    base = 1_800_000_000 // 30 * 30            # on a 30 s boundary
    for off, px in [(0, 10), (5, 12), (10, 9), (25, 11), (30, 11.5), (40, 13)]:
        dt.add_sample(w, base + off, px)
    assert w["buckets"][base] == [10, 12, 9, 11]          # open, high, low, close
    assert w["buckets"][base + 30] == [11.5, 13, 11.5, 13]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hist = [{"date": iso(base - 60 * k), "open": 1, "high": 2, "low": 0.5, "close": 1.5} for k in range(40, -1, -1)]
    out = dt.merge(hist, w["buckets"], base + 60)
    assert all(c["t"] + c["d"] > base + 60 - dt.WINDOW for c in out)       # only the last 30 minutes
    assert [c["d"] for c in out[-2:]] == [30, 30]                          # live candles last
    assert all(c["t"] + 60 <= base for c in out if c["d"] == 60)          # history stops where live starts
    # old buckets fall out of the window
    dt.add_sample(w, base + dt.WINDOW + 120, 14)
    assert base not in w["buckets"]


def test_daytrader_page_and_api(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    from fastapi.testclient import TestClient
    from app import web, daytrader
    importlib.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)
    monkeypatch.setattr(daytrader, "search", lambda q: [{"instrumentId": 1137, "symbol": "NVDA", "name": "NVIDIA", "type": "Stocks"}])
    monkeypatch.setattr(daytrader, "chart", lambda iid, hurdle=None: {"instrumentId": iid, "candles": [], "open": False, "hurdle": hurdle})
    with TestClient(web.app) as c:
        r = c.get("/daytrader")
        assert r.status_code == 200 and "Day trader" in r.text and "dt-openbtn" in r.text and "data-root" in r.text
        assert c.get("/daytrader/search?q=nvd").json()[0]["symbol"] == "NVDA"
        assert c.get("/daytrader/candles/1137").json()["instrumentId"] == 1137
        assert c.get("/daytrader/candles/1137?hurdle=2.02").json()["hurdle"] == 2.02
        assert "Open paper trades" in r.text and "Closed paper trades" in r.text and "breakEven" in r.text
        assert "Strategy auto-test" in r.text
        assert c.get("/daytrader/autotest").json()["total"]["trades"] == 0
        monkeypatch.setattr(daytrader, "_name", lambda iid: ("NVDA", "NVIDIA", "stock") if iid == 1137 else (f"#{iid}", "", "other"))
        daytrader._cache.clear()
        got = c.post("/daytrader/autotest/add", json={"iid": 1137, "kind": "crypto"}).json()["symbols"]
        assert any(x["iid"] == 1137 and x["kind"] == "stock" and x["name"] == "NVIDIA" for x in got)   # type from eToro, not the page
        assert c.post("/daytrader/autotest/add", json={"iid": 999999}).status_code == 400              # not an eToro instrument
        got = c.post("/daytrader/autotest/toggle", json={"iid": 1137, "strategy": "swing", "on": False}).json()["symbols"]
        assert [x["swing_on"] for x in got if x["iid"] == 1137] == [0]
        assert c.post("/daytrader/autotest/toggle", json={"iid": 1137, "strategy": "bogus", "on": True}).status_code == 400
        daytrader._cache.clear()
        assert all(x["iid"] != 1137 for x in c.post("/daytrader/autotest/remove", json={"iid": 1137}).json()["symbols"])
        assert "Day trader" in c.get("/").text                     # in the top menu
    assert not orders(fs)                                          # the page never trades


def test_daytrader_quote_uses_sampled_price(monkeypatch):
    from app import daytrader as dt
    import time as _t
    monkeypatch.setattr(dt, "watch", lambda iid: dt._watch.setdefault(iid, {"buckets": {}, "quote": None, "seen": _t.time()}))
    dt._watch[42] = {"buckets": {}, "seen": _t.time(), "quote": {"bid": 9.9, "ask": 10.0, "ts": _t.time()}}
    q = dt.quote(42)
    assert q["ask"] == 10.0 and q["bid"] == 9.9 and q["open"] and q["instrumentId"] == 42
    dt._watch[42]["quote"]["ts"] = _t.time() - 3600         # an hour-old price means the market is closed
    assert dt.quote(42)["open"] is False
    dt._watch.pop(42, None)


def test_rates_prefer_fresher_v2_price():
    from app import etoro

    class S:
        def __init__(self, v2):
            self.v2 = v2

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/api/v1/market-data/instruments/rates" in url:
                return Resp(200, {"rates": [
                    {"instrumentID": 1, "bid": 90.36, "ask": 90.38, "date": "2026-11-06T15:32:58.6Z", "conversionRateBid": 1.3},
                    {"instrumentID": 2, "bid": 10.0, "ask": 10.1, "date": "2026-11-06T21:45:00Z", "conversionRateBid": 1}]})
            if "/api/v2/market-data/rates" in url:
                return self.v2
            return Resp(404, {})

    v2 = Resp(200, {"results": [{"instrumentId": 1, "bid": 89.95, "ask": 89.96, "date": "2026-11-06T21:45:34.287"},
                                {"instrumentId": 2, "bid": 9.0, "ask": 9.1, "date": "2026-11-06T20:00:00"}]})
    r = etoro.EtoroClient("k", "u", env="real", base="https://x", session=S(v2)).rates([1, 2])
    assert r[1]["bid"] == 89.95 and r[1]["ask"] == 89.96 and r[1]["conversionRateBid"] == 1.3   # stale v1 replaced, FX kept
    assert r[2]["bid"] == 10.0                                                                  # v1 newer, kept
    r = etoro.EtoroClient("k", "u", env="real", base="https://x", session=S(Resp(500, {}))).rates([1])
    assert r[1]["bid"] == 90.36                                                                 # v2 down: v1 still works


def _bars(fn, n=300, t0=1_800_000_000):
    bars, prev = [], fn(0)
    for i in range(n):
        c = fn(i)
        bars.append({"t": t0 + 60 * i, "o": prev, "h": max(prev, c) * 1.0008, "l": min(prev, c) * 0.9992, "c": c})
        prev = c
    return bars


def test_strategy_signals():
    import math
    from app import strategy as st

    def dip(i):                      # steady uptrend, a 7-minute dip, then turning up
        p = 100 * (1 + 0.0004 * i) + 0.05 * math.sin(i / 3)
        if i >= 290:
            p -= (min(i, 297) - 290) * 0.15
        if i >= 298:
            p += (i - 297) * 0.03
        return p
    b = _bars(dip)
    s = st.evaluate(b, b[-1]["c"], b[-1]["c"] * 1.0001, True, 0.2)
    assert s["state"] == "enter" and s["stop"] < s["entry"] < s["target"]
    assert abs((s["target"] - s["entry"]) - 2 * (s["entry"] - s["stop"])) < 1e-9          # 2:1 reward to risk
    assert st.evaluate(b, b[-1]["c"], b[-1]["c"], True, 2.02)["state"] == "fees"           # same move can't pay crypto fees

    down = _bars(lambda i: 100 * (1 - 0.0004 * i) + 0.05 * math.sin(i / 3))
    s = st.evaluate(down, down[-1]["c"], down[-1]["c"], True, 0.2)
    assert s["state"] == "avoid" and s["entry"] is None

    ext = _bars(lambda i: 100 * (1 + 0.0004 * i) + (0.4 if i > 295 else 0))
    s = st.evaluate(ext, ext[-1]["c"], ext[-1]["c"], True, 0.2)
    assert s["state"] == "wait" and s["entry"] < ext[-1]["c"]                               # buy level below the price

    assert st.evaluate(b, None, None, False, 1)["state"] == "closed"
    assert st.evaluate(b[:30], 1, 1, True, 1)["state"] == "nodata"
    assert len(st.resample(b, 300)) == 60


def test_daytrader_chart_includes_signal_and_cfd_kind(monkeypatch):
    import math, time as _t
    from app import daytrader as dt, engine
    now = _t.time()
    t0 = int(now // 60 * 60) - 299 * 60
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    class C:
        def candles(self, iid, interval, count):
            out, prev = [], 100.0
            for i in range(count):
                c = 100 * (1 - 0.0004 * i) + 0.05 * math.sin(i / 3)      # downtrend
                out.append({"date": iso(t0 + 60 * i), "open": prev, "high": max(prev, c) + 0.05, "low": min(prev, c) - 0.05, "close": c})
                prev = c
            return out

        def live_rates(self, ids):
            return {i: {"bid": 88.0, "ask": 88.1, "date": iso(now)} for i in ids}

        rates = live_rates

        def _req(self, method, path, params=None):
            return {"instrumentDisplayDatas": [{"symbolFull": "TSLA.24-7", "instrumentDisplayName": "Tesla 24/7", "instrumentTypeID": 5}]}

    monkeypatch.setattr(engine, "client", lambda: C())
    monkeypatch.setattr(dt, "watch", lambda iid: None)
    dt._cache.clear()
    d = dt.chart(15200, 0.0)
    assert d["kind"] == "cfd" and d["symbol"] == "TSLA.24-7"
    assert d["signal"]["state"] == "avoid"                                   # never buy a downtrend
    assert abs(d["signal"]["hurdle_pct"] - 0.11) < 0.01                      # spread (0.1/88) added to the fees
    assert 50 <= len(d["candles"]) <= 61                                    # chart shows the last hour
    dt._cache.clear()


def test_autotest_opens_on_signal_and_closes_at_stop_target_or_time(eng, monkeypatch):
    import math, time as _t, importlib as _il
    engine, store, sent, fs, rating = eng
    from app import autotest, daytrader, strategy
    _il.reload(autotest)
    now = _t.time()
    t0 = int(now // 60 * 60) - 299 * 60
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def dip(i):
        p = 100 * (1 + 0.0004 * i) + 0.05 * math.sin(i / 3)
        if i >= 290:
            p -= (min(i, 297) - 290) * 0.15
        if i >= 298:
            p += (i - 297) * 0.03
        return p
    state = {"extra": [], "bid": None}

    class C:
        def candles(self, iid, interval, count):
            out, prev = [], dip(0)
            for i in range(count):
                c = dip(i)
                out.append({"date": iso(t0 + 60 * i), "open": prev, "high": max(prev, c) * 1.0008, "low": min(prev, c) * 0.9992, "close": c})
                prev = c
            return out + state["extra"]

        def live_rates(self, ids):
            b = state["bid"] if state["bid"] is not None else dip(299)
            return {i: {"bid": b, "ask": b * 1.0001, "date": iso(now)} for i in ids}

        rates = live_rates

    monkeypatch.setattr(engine, "client", lambda: C())
    daytrader._cache.clear()
    with store._lock, store.connect() as c:                     # test with one CFD only (spread-only costs)
        c.executescript(autotest.CREATE)
        c.execute("INSERT OR IGNORE INTO autotest_meta(key, value) VALUES ('seeded','x')")
    autotest.add_symbol(15200, "TSLA.24-7", "cfd")
    autotest.set_enabled(15200, "fast", False)
    autotest.tick()
    assert autotest.summary()["open"] == []                      # fast switched off: no trade despite Entry now
    autotest.set_enabled(15200, "fast", True)
    daytrader._cache.clear()
    autotest.tick()
    s = autotest.summary()
    assert len(s["open"]) == 1 and s["symbols"][0]["last_state"] == "enter"
    t = s["open"][0]
    assert t["stop"] < t["entry"] < t["target"]
    daytrader._cache.clear()
    autotest.tick()                                              # still Entry now: no second trade in the same symbol
    assert len(autotest.summary()["open"]) == 1

    # a later 1-minute candle touches the stop -> closed at the stop price
    state["extra"] = [{"date": iso(t0 + 60 * 301), "open": t["entry"], "high": t["entry"], "low": t["stop"] * 0.999, "close": t["stop"]}]
    state["bid"] = t["stop"] * 1.0005
    daytrader._cache.clear()
    autotest.tick()
    s = autotest.summary()
    closed = s["closed"][0]
    assert closed["reason"] == "stop" and closed["exit"] == t["stop"] and closed["net"] < 0
    assert s["total"]["trades"] == 1 and s["total"]["stops"] == 1
    assert s["totals"]["fast"]["trades"] == 1 and s["totals"]["swing"]["trades"] == 0 and closed["strategy"] == "fast"
    assert s["symbols"][0]["swing_state"]                                 # the swing strategy is checked too
    lines = autotest.digest(0)
    assert lines[0].startswith("Strategy auto-test") and any("Fast (5-min trend)" in l and "1 closed" in l for l in lines)
    assert any("TSLA.24-7 (fast): 1 trades, 0 in profit" in l for l in lines)

    # check_exit directly: target, and the time limit
    tr = {"opened_ts": 1000.0, "stop": 90.0, "target": 110.0}
    assert autotest.check_exit(tr, [{"t": 1080, "l": 95, "h": 111}], 100, True, 2000) == ("target", 110.0)
    assert autotest.check_exit(tr, [{"t": 1080, "l": 89, "h": 111}], 100, True, 2000) == ("stop", 90.0)   # both: stop first
    assert autotest.check_exit(tr, [{"t": 1000, "l": 80, "h": 120}], 100, True, 2000) is None            # entry minute ignored
    assert autotest.check_exit(tr, [], 100, True, 1000 + 4 * 3600)[0].startswith("time limit")
    assert autotest.costs("crypto", "BTC", 100, 100, 102)["fees"] == 2.02

    autotest.remove_symbol(15200)
    assert autotest.summary()["symbols"] == []
    daytrader._cache.clear()


def test_swing_strategy_targets_are_wide_enough_for_crypto_fees():
    import math
    from app import strategy as st

    def mk(fn, n, step, t0=1_800_000_000, wig=1.004):
        bars, prev = [], fn(0)
        for i in range(n):
            c = fn(i)
            bars.append({"t": t0 + step * i, "o": prev, "h": max(prev, c) * wig, "l": min(prev, c) / wig, "c": c})
            prev = c
        return bars
    h1 = mk(lambda i: 100 * (1 + 0.003 * i) + 0.3 * math.sin(i / 2), 100, 3600, wig=1.01)    # strong hourly uptrend

    def m15(i):
        p = 128 + 0.05 * i + 0.1 * math.sin(i / 3)
        if i >= 90:
            p -= (min(i, 97) - 90) * 0.35
        if i >= 98:
            p += (i - 97) * 0.1
        return p
    q = mk(m15, 100, 900, t0=1_800_000_000 + 99 * 3600 - 100 * 900)
    s = st.evaluate_swing(q, h1, q[-1]["c"], q[-1]["c"] * 1.0001, True, 2.03)
    assert s["key"] == "swing" and s["state"] in ("enter", "wait")
    assert s["reward_pct"] > 1.5 * 2.03                    # can pay crypto fees, unlike the fast version
    assert s["stop"] < s["entry"] < s["target"]
    down = mk(lambda i: 100 * (1 - 0.003 * i), 100, 3600)
    assert st.evaluate_swing(q, down, q[-1]["c"], q[-1]["c"], True, 2.03)["state"] == "avoid"
    assert st.evaluate_swing(q[:10], h1, 1, 1, True, 2)["state"] == "nodata"


def test_watch_levels_off_the_watchlist_say_so_and_can_be_analysed(eng, monkeypatch):
    engine, store, sent, fs, rating = eng
    import importlib as _il, threading as _th, time as _t
    from fastapi.testclient import TestClient
    from app import levels, policy, notify, web
    store.set_setting("watchlist", "META,GOOG")
    monkeypatch.setattr(levels, "call_model", lambda p: '{"conditions": [{"type": "zone", "low": 14.5, "high": 14.9}], "invalid_below": 14}')
    levels.record("IUHC.L", 1, "t", 15.0)                                      # not on the watchlist
    monkeypatch.setattr(levels, "call_model", lambda p: '{"conditions": [{"type": "close_above", "level": 605}]}')
    levels.record("META", 2, "t", 600)                                         # on the watchlist
    off, on = levels.active("IUHC.L")[0], levels.active("META")[0]
    mails = []
    monkeypatch.setattr(notify, "email", lambda subj, body, **k: mails.append(body))
    monkeypatch.setattr(levels, "check", lambda ec: {"triggered": [("IUHC.L", off, "traded down to 14.88"), ("META", on, "closed 610")],
                                                    "cancelled": [], "expired": []})
    levels.overnight(None)
    body = mails[-1]
    assert "META" in body and "It will be analysed first in its next run" in body
    assert "IUHC.L is not on your watchlist, so it will NOT be analysed automatically" in body and "/preflight?symbol=IUHC.L" in body

    _il.reload(web)
    monkeypatch.setattr(web.sched, "start", lambda: None)

    class EC:
        def find_instrument(self, s):
            return {"instrumentId": 1036574, "name": "iShares Health Care", "assetClass": "ETF"}
    monkeypatch.setattr(engine, "client", lambda: EC())
    monkeypatch.setattr(engine, "preflight", lambda item: (_ for _ in ()).throw(RuntimeError("no limits in test")))
    ran = []
    monkeypatch.setattr(engine, "analyse", lambda item, manual=False: ran.append((item.symbol, manual)))
    with TestClient(web.app) as c:
        page = c.get("/preflight?symbol=IUHC.L")
        assert page.status_code == 200 and "is not on your watchlist" in page.text and "Analyse anyway" in page.text
        assert c.post("/run", data={"symbol": "IUHC.L"}, follow_redirects=False).status_code in (200, 303)
        for _ in range(50):
            if ran:
                break
            _t.sleep(0.02)
        assert ran == [("IUHC.L", True)]
        assert c.post("/run", data={"symbol": "TSLA"}, follow_redirects=False).status_code == 404   # no level, not listed
        ov = c.get("/").text
        assert "Not on watchlist" in ov and "preflight?symbol=IUHC.L" in ov


def test_fast2_filters():
    import math
    from datetime import datetime as _dt, timezone as _tz
    from app import strategy as st
    mon = _dt(2026, 11, 9, 15, 0, tzinfo=_tz.utc)            # US market open
    sat = _dt(2026, 11, 7, 20, 0, tzinfo=_tz.utc)

    def dip(i):
        p = 100 * (1 + 0.0004 * i) + 0.05 * math.sin(i / 3)
        if i >= 290:
            p -= (min(i, 297) - 290) * 0.15
        if i >= 298:
            p += (i - 297) * 0.03
        return p
    b1 = _bars(dip)
    h_up = _bars(lambda i: 80 + 0.4 * i, n=100)
    h_down = _bars(lambda i: 200 - 0.8 * i, n=100)
    bid = b1[-1]["c"]
    ask = bid * 1.0002
    s = st.evaluate_fast2(b1, h_up, bid, ask, True, 0.02 + 0.02, "cfd", "TSLA.24-7", mon)
    assert s["state"] == "enter" and s["entry"] == ask                          # bought at the ask
    assert (ask - s["stop"]) / ask >= 0.003 - 1e-12                              # stop at least 0.3% away
    assert abs((s["target"] - ask) - 2 * (ask - s["stop"])) < 1e-9
    assert st.evaluate_fast2(b1, h_up, bid, ask, True, 0.04, "cfd", "TSLA.24-7", sat)["state"] == "closed"   # weekend
    assert st.evaluate_fast2(b1, h_down, bid, ask, True, 0.04, "cfd", "TSLA.24-7", mon)["state"] == "avoid"  # 1h down
    assert st.evaluate_fast2(b1, h_up, bid, ask, True, 2.04, "crypto", "SOL", sat)["state"] == "fees"        # 2% fees
    quiet = _bars(lambda i: 100 + 0.004 * i + 0.002 * math.sin(i))
    for b in quiet:
        b["h"], b["l"] = max(b["o"], b["c"]) + 0.003, min(b["o"], b["c"]) - 0.003
    s = st.evaluate_fast2(quiet, h_up, quiet[-1]["c"], quiet[-1]["c"], True, 0.02, "cfd", "TSLA.24-7", mon)
    assert s["state"] == "quiet"
    assert st.home_open("SKHY.24-7", _dt(2026, 11, 9, 1, 0, tzinfo=_tz.utc)) and not st.home_open("SKHY.24-7", mon)


def test_fast2_cooldown_and_daily_pause(eng):
    import time as _t
    engine, store, sent, fs, rating = eng
    from app import autotest
    autotest._ensure()
    now = _t.time()

    def stop_at(ts, key="fast2"):
        with store._lock, store.connect() as c:
            c.execute("INSERT INTO autotrades(iid, symbol, kind, strategy, opened_ts, amt, entry, stop, target, status, "
                      "closed_ts, exit, reason, gross, fees, net) VALUES (7,'X','cfd',?,?,100,10,9.9,10.2,'closed',?,9.9,'stop',-1,0,-1)",
                      (key, ts - 600, ts))
    assert autotest.blocked(7, "fast2", now) is None
    stop_at(now - 10 * 60)
    assert autotest.blocked(7, "fast2", now).startswith("Cooling off")               # 10 minutes after a stop
    assert autotest.blocked(7, "fast2", now + 25 * 60) is None                        # 35 minutes after
    stop_at(now - 5 * 60, key="fast")
    assert autotest.blocked(7, "fast", now) is None                                   # the original fast has no cooldown
    stop_at(now - 60 * 60 + 1 if (now - 3600) > 0 else now)
    from zoneinfo import ZoneInfo
    from app import config
    from datetime import datetime as _dt
    midday = _dt.fromtimestamp(now, ZoneInfo(config.TZ)).replace(hour=12, minute=0).timestamp()
    with store._lock, store.connect() as c:
        c.execute("DELETE FROM autotrades WHERE iid=7")
    stop_at(midday - 3 * 3600)
    stop_at(midday - 2 * 3600)
    assert autotest.blocked(7, "fast2", midday).startswith("Paused for the rest of today")   # 2 stops today
    assert autotest.blocked(7, "fast2", midday + 24 * 3600) is None                            # a new day
