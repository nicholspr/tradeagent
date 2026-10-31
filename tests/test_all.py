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
    sent = []
    monkeypatch.setattr(notify, "sms", lambda t: sent.append(("sms", t)) or True)
    monkeypatch.setattr(notify, "email", lambda s, b, html=None: sent.append(("email", s)) or True)
    CREDIT["v"] = 500.0
    fs = FakeSession()
    monkeypatch.setattr(engine, "client", lambda: etoro.EtoroClient("k", "u", env="real", base="https://x", session=fs))
    rating = {"v": "Buy"}
    monkeypatch.setattr(engine.analyst, "analyze", lambda t, a, p: (rating["v"], f"FINAL: {rating['v']}"))
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
        assert "http-equiv=refresh" not in c.get("/").text
        rid = store.start_run("BTC")
        assert "http-equiv=refresh content=30" in c.get("/").text
        store.finish_run(rid, "Hold", "x")
        assert "http-equiv=refresh" not in c.get("/").text


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
        assert "http-equiv=refresh" not in c.get("/").text
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
    monkeypatch.setattr(op, "call_model", lambda prompt: ("```" + json.dumps(ideas) + "```", urls))

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
    monkeypatch.setattr(op, "call_model", lambda p: ("not json", set()))
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
        assert "Change 1D" in page and "52W range" in page and "Sentiment" in page and "Short" in page
        assert not orders(fs)


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
        for title in ("Watchlist and schedule", "Trading limits", "Approvals and sells"):
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
