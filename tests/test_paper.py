"""The practice account: virtual cash, real prices, real rules."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from metro import clock, paper, universe
from metro.db import PaperOrder, Quote, kv_set, session

OPEN = datetime(2026, 11, 10, 11, 0, tzinfo=clock.NY)  # a Tuesday, mid-session
CLOSED = datetime(2026, 11, 7, 12, 0, tzinfo=clock.NY)  # a Saturday


def _px(s, t, price, prev=None, at=OPEN):
    q = s.get(Quote, t)
    ts = at.astimezone(timezone.utc).replace(tzinfo=None)
    if q is None:
        s.add(Quote(ticker=t, price=price, prev_close=prev or price, ts=ts, source="test"))
    else:
        q.price, q.ts = price, ts
    s.flush()


@pytest.fixture
def market(isolated_env):
    universe.sync_universe()
    with session() as s:
        _px(s, "SPY", 600)
        _px(s, "AAPL", 200)
        _px(s, "MSFT", 400)
    yield


def test_buy_fills_at_the_live_price_and_sell_realizes(market):
    with session() as s:
        o = paper.place(s, "AAPL", "buy", 100, now=OPEN)
        assert o.status == "filled" and o.fill_price == 200 and o.fee == 1.0
        a = paper.account(s)
        assert a.cash == pytest.approx(100_000 - 20_001)
        assert o.snapshot["price"] == 200
    with session() as s:
        _px(s, "AAPL", 220)
        o = paper.place(s, "AAPL", "sell", 40, now=OPEN)
        assert o.status == "filled" and o.realized == pytest.approx((220 - 200.01) * 40 - 1, abs=0.01)
        st = paper.state(s)
        assert st["positions"][0]["q"] == 60 and st["stats"]["closed"] == 1 and st["stats"]["wins"] == 1
        assert st["account"]["nav"] == pytest.approx(st["account"]["cash"] + 60 * 220)


def test_rules_no_short_no_margin_whole_shares(market):
    with session() as s:
        with pytest.raises(paper.PaperError, match="בחסר"):
            paper.place(s, "AAPL", "sell", 1, now=OPEN)
        with pytest.raises(paper.PaperError, match="מזומן"):
            paper.place(s, "MSFT", "buy", 300, now=OPEN)  # $120k with $100k
        with pytest.raises(paper.PaperError, match="אחת"):
            paper.place(s, "AAPL", "buy", 0.4, now=OPEN)
        with pytest.raises(paper.PaperError, match="מתחת"):
            paper.place(s, "AAPL", "buy", 10, stop=210, now=OPEN)


def test_a_market_order_on_the_weekend_waits_for_the_open(market):
    with session() as s:
        o = paper.place(s, "AAPL", "buy", 10, now=CLOSED)
        assert o.status == "pending"
        assert paper.process(s, CLOSED) == 0
        # Tuesday morning, but the quote is still Friday's: no fill on an old price
        _px(s, "AAPL", 199, at=datetime(2026, 11, 6, 15, 59, tzinfo=clock.NY))
        assert paper.process(s, OPEN) == 0
        _px(s, "AAPL", 205)
        assert paper.process(s, OPEN) == 1
        assert s.get(PaperOrder, o.id).fill_price == 205


def test_stop_and_target_ride_with_the_buy(market):
    with session() as s:
        buy = paper.place(s, "AAPL", "buy", 50, stop=190, target=230, now=OPEN)
        kids = s.query(PaperOrder).filter(PaperOrder.parent_id == buy.id).all()
        assert {k.kind for k in kids} == {"stop", "limit"} and all(k.status == "pending" for k in kids)
        _px(s, "AAPL", 195)
        assert paper.process(s, OPEN) == 0
        paper.place(s, "AAPL", "sell", 20, now=OPEN)  # sold part: the stop and target now cover the 30 left
        assert all(k.quantity == 30 for k in kids)
        _px(s, "AAPL", 195)
        _px(s, "AAPL", 186)  # gapped through the stop: fills at the market, below the stop
        assert paper.process(s, OPEN) == 1
        stop = next(k for k in kids if k.kind == "stop")
        tgt = next(k for k in kids if k.kind == "limit")
        assert stop.status == "filled" and stop.fill_price == 186 and stop.realized < 0 and stop.quantity == 30
        assert tgt.status == "cancelled"
        assert paper.state(s)["positions"] == []


def test_limit_buy_waits_for_its_price_and_cancel_takes_the_children(market):
    with session() as s:
        o = paper.place(s, "MSFT", "buy", 10, kind="limit", trigger=380, stop=360, now=OPEN)
        assert o.status == "pending"
        kid = s.query(PaperOrder).filter(PaperOrder.parent_id == o.id).one()
        assert kid.status == "held"
        paper.cancel(s, o.id)
        assert kid.status == "cancelled"
        o2 = paper.place(s, "MSFT", "buy", 10, kind="limit", trigger=380, now=OPEN)
        _px(s, "MSFT", 379)
        assert paper.process(s, OPEN) == 1 and o2.fill_price == 379


def test_versus_the_index_and_reset_keeps_history(market):
    with session() as s:
        paper.place(s, "AAPL", "buy", 100, now=OPEN)
        _px(s, "SPY", 630)
        _px(s, "AAPL", 230)
        summ = paper.summary(s)
        assert summ["spy_ret_pct"] == 5.0 and summ["ret_pct"] > 2.9
        row = paper.snapshot_day(s, OPEN)
        assert row.nav == summ["nav"] and row.spy == 630
        old = paper.account(s).id
        new = paper.reset(s, 50_000)
        assert new.id != old and new.cash == 50_000 and paper.state(s)["positions"] == []


def test_api_and_city(market):
    from metro.api import app
    with session() as s:
        kv_set(s, "mode", "demo")  # the demo database fills at any hour
    c = TestClient(app)
    r = c.post("/api/paper/order", json={"ticker": "aapl", "side": "buy", "qty": 25})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["order"]["status"] == "filled" and j["rows"]["AAPL"]["q"] == 25
    assert c.post("/api/paper/order", json={"ticker": "AAPL", "side": "sell", "qty": 99}).status_code == 400
    city = c.get("/api/city").json()
    row = next(x for x in city["companies"] if x["t"] == "AAPL")
    assert row["paper"]["q"] == 25 and city["paper"]["positions"] == 1
    st = c.get("/api/paper").json()
    assert st["account"]["nav"] > 0 and st["trades"][0]["t"] == "AAPL"
    assert c.post("/api/paper/reset", json={"cash": 25_000}).json()["account"]["cash"] == 25_000
