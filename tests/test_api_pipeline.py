from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from metro import alerts, brief, demo, pipeline
from metro.db import Alert, session, utcnow


@pytest.fixture
def client():
    demo.generate(years=1.6)
    from metro.api import app
    with TestClient(app) as c:
        yield c


def test_city_payload(client):
    js = client.get("/api/city").json()
    assert js["mode"] == "demo" and len(js["companies"]) == 500
    row = next(r for r in js["companies"] if r["t"] == "PANW")
    assert row["hold"]["th"] == "watch" and row["rep"]["st"] == "done" and row["sig"] < -1.5
    assert {"m", "s", "i"} <= set(row["dec"]) and row["sc"]["tot"] >= 0
    assert any(r["beacon"] for r in js["companies"])
    assert js["relations"] and js["session"]["state"] in ("open", "pre", "post", "closed")


def test_company_card(client):
    js = client.get("/api/company/panw").json()
    assert js["company"]["t"] == "PANW" and len(js["series"]["c"]) > 100
    assert js["card"]["card"]["demo"] is True and len(js["card"]["card"]["coverage"]) == 15
    assert js["thesis"]["status"] == "watch" and js["peers"]
    assert client.get("/api/company/GOOG").json()["company"]["t"] == "GOOGL"
    assert client.get("/api/company/ZZZZ").status_code == 404


def test_thesis_journal_watchlist_roundtrip(client):
    body = {"text": "test", "metrics": [{"name": "rev_yoy", "op": ">", "value": 1000}], "kills": [{"type": "veto"}]}
    assert client.put("/api/thesis/AAPL", json=body).json()["ok"]
    assert client.get("/api/company/AAPL").json()["thesis"]["status"] == "watch"
    jid = client.post("/api/journal", json={"ticker": "AAPL", "action": "pass", "note": "יקרה"}).json()["id"]
    assert any(j["id"] == jid and j["snapshot"]["score"] for j in client.get("/api/journal").json())
    assert client.put("/api/watchlist/AAPL", json={"entry_low": 1, "entry_high": 2}).json()["ok"]
    assert next(r for r in client.get("/api/city").json()["companies"] if r["t"] == "AAPL")["watch"]
    assert client.post("/api/journal", json={"ticker": "AAPL", "action": "yolo"}).status_code == 422


def test_status_brief_search_portfolio(client):
    st = client.get("/api/status").json()
    assert st["mode"] == "demo" and st["sources"]["sec"]["state"] == "never"
    assert client.get("/api/search?q=nvid").json()[0]["t"] == "NVDA"
    pf = client.get("/api/portfolio").json()
    assert len(pf["holdings"]) == 8 and abs(sum(pf["by_sector"].values()) - 86) < 1e-6
    b = client.get("/api/brief").json()
    assert b["portfolio"] and "תדריך בוקר" in brief.to_text(b)


def test_access_token(monkeypatch, client):
    from metro import config
    monkeypatch.setenv("ACCESS_TOKEN", "s3cret")
    config.get_settings.cache_clear()
    assert client.get("/api/city").status_code == 401
    r = client.get("/?token=s3cret", follow_redirects=False)
    assert r.status_code == 307 and "metro_token" in r.headers.get("set-cookie", "")
    assert client.get("/api/status", headers={"x-metro-token": "s3cret"}).status_code == 200


def test_alerts_quiet_hours_and_telegram(monkeypatch):
    demo.generate(years=1.6)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    from metro import clock, config
    config.get_settings.cache_clear()
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"ok": True})
    night = datetime(2026, 11, 3, 2, 0, tzinfo=clock.IL)
    n_night = alerts.dispatch(at=night, transport=httpx.MockTransport(handler))
    with session() as s:
        urgent = [a for a in s.query(Alert).all() if a.level == "urgent"]
    assert n_night == len(urgent) >= 1  # only urgent goes out at night
    day = datetime(2026, 11, 3, 10, 0, tzinfo=clock.IL)
    assert alerts.dispatch(at=day, transport=httpx.MockTransport(handler)) >= 1


def test_live_quote_appends_today(monkeypatch):
    demo.generate(years=1.6)
    from metro.db import Quote
    with session() as s:
        q = s.get(Quote, "MSFT")
        q.price, q.ts = q.price * 1.05, utcnow() + timedelta(days=2)
        for etf in ("SPY", "XLK"):
            e = s.get(Quote, etf)
            e.ts = q.ts
    sc = pipeline.compute_scores()
    assert sc["MSFT"]["raw"]["chg_pct"] > 4
