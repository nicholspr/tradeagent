import importlib
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
