import json
from datetime import date, datetime

import httpx
import pytest

from metro import clock, universe
from metro.sources import alpaca, finnhub, ibkr, sec, sections, transcripts

SUBMISSIONS = {
    "cik": "320193", "name": "Apple Inc.", "fiscalYearEnd": "0927", "tickers": ["AAPL"],
    "filings": {"recent": {
        "accessionNumber": ["0000320193-25-000071", "0000320193-25-000070"],
        "filingDate": ["2025-07-31", "2025-07-31"], "reportDate": ["2025-06-28", "2025-07-31"],
        "acceptanceDateTime": ["2025-07-31T16:31:02.000Z", "2025-07-31T16:30:11.000Z"],
        "form": ["10-Q", "8-K"], "items": ["", "2.02,9.01"],
        "primaryDocument": ["aapl-20250628.htm", "aapl-20250731.htm"],
    }}}

FEED = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>8-K - Palo Alto Networks Inc (0001327567) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1327567/000132756725000041/0001327567-25-000041-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2025-11-19 &lt;b&gt;AccNo:&lt;/b&gt; 0001327567-25-000041 &lt;b&gt;Size:&lt;/b&gt; 1 MB
&lt;br&gt;Item 2.02: Results of Operations and Financial Condition
&lt;br&gt;Item 5.02: Departure of Directors or Certain Officers</summary>
<updated>2025-11-19T16:05:12-05:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0001327567-25-000041</id>
</entry>
</feed>"""

INDEX_HTML = """<html><body><table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/1327567/000132756725000041/panw-20251119.htm">panw-20251119.htm</a></td><td>8-K</td><td>40 KB</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1327567/000132756725000041/q1fy26pressrelease.htm">q1fy26pressrelease.htm</a></td><td>EX-99.1</td><td>300 KB</td></tr>
</table></body></html>"""

TENQ = """
UNITED STATES SECURITIES AND EXCHANGE COMMISSION
TABLE OF CONTENTS
PART I
Item 1. Financial Statements 1
Item 2. Management's Discussion and Analysis 14
Item 3. Quantitative and Qualitative Disclosures About Market Risk 22
Item 4. Controls and Procedures 22
PART II
Item 1. Legal Proceedings 23
Item 1A. Risk Factors 23
Item 2. Unregistered Sales of Equity Securities 24
PART I - FINANCIAL INFORMATION
Item 1. Financial Statements
Condensed consolidated statements of operations with many numbers and notes about revenue by segment and geography.
Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations
Demand for our products remained strong across all regions. Gross margin was 46.5 percent, driven by mix.
Item 3. Quantitative and Qualitative Disclosures About Market Risk
There have been no material changes to our market risk during the quarter compared with our annual report.
Item 4. Controls and Procedures
Our disclosure controls and procedures were effective as of the end of the period covered by this report.
PART II - OTHER INFORMATION
Item 1. Legal Proceedings
The company is subject to various legal proceedings and claims that arise in the ordinary course of business.
Item 1A. Risk Factors
There have been no material changes to the risk factors disclosed in our most recent annual report on Form 10-K.
Item 2. Unregistered Sales of Equity Securities and Use of Proceeds
During the quarter the company repurchased 92 million shares for 20.0 billion dollars under its program.
"""

FLEX_XML = """<FlexQueryResponse queryName="positions" type="AF"><FlexStatements count="1">
<FlexStatement accountId="U123" fromDate="20251102" toDate="20251102">
<EquitySummaryInBase><EquitySummaryByReportDateInBase reportDate="20251102" total="250000" /></EquitySummaryInBase>
<OpenPositions>
<OpenPosition assetCategory="STK" symbol="MSFT" position="40" markPrice="510.20" positionValue="20408" costBasisPrice="420.5" fifoPnlUnrealized="3588" currency="USD" levelOfDetail="SUMMARY"/>
<OpenPosition assetCategory="STK" symbol="BRK B" position="30" markPrice="480" positionValue="14400" costBasisPrice="500" currency="USD" levelOfDetail="SUMMARY"/>
<OpenPosition assetCategory="OPT" symbol="AAPL  251219C00250000" position="1" markPrice="5" positionValue="500" currency="USD"/>
</OpenPositions>
<Trades><Trade assetCategory="STK" symbol="MSFT" tradeDate="20251015" buySell="BUY" quantity="10" tradePrice="505.1" tradeID="77"/></Trades>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_parse_submissions_and_urls():
    refs = sec.parse_submissions(SUBMISSIONS)
    assert refs[1].form == "8-K" and refs[1].items == ["2.02", "9.01"]
    assert refs[0].url == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000071/aapl-20250628.htm"
    assert refs[0].accepted == datetime(2025, 7, 31, 16, 31, 2)


def test_current_feed_and_items():
    rows = sec.parse_current_feed(FEED)
    assert rows == [{"form": "8-K", "name": "Palo Alto Networks Inc", "cik": 1327567, "accession": "0001327567-25-000041",
                     "url": "https://www.sec.gov/Archives/edgar/data/1327567/000132756725000041/0001327567-25-000041-index.htm",
                     "updated": "2025-11-19T16:05:12-05:00", "items": ["2.02", "5.02"]}]


def test_filing_index_finds_press_release():
    docs = sec.parse_filing_index(INDEX_HTML, "https://www.sec.gov/Archives/edgar/data/1327567/000132756725000041")
    ex = [d for d in docs if d["type"] == "EX-99.1"]
    assert ex[0]["url"].endswith("/q1fy26pressrelease.htm")
    assert docs[0]["url"] == "https://www.sec.gov/Archives/edgar/data/1327567/000132756725000041/panw-20251119.htm"


def test_sec_client_requires_contact_user_agent(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "")
    from metro import config
    config.get_settings.cache_clear()
    with pytest.raises(sec.SecError):
        sec.SecClient()


def test_sec_client_sends_user_agent_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=SUBMISSIONS)
    c = sec.SecClient(rps=100, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    c.filings(320193)
    c.filings(320193)
    assert len(calls) == 1 and calls[0].headers["User-Agent"] == "Test Runner test@example.org"
    assert calls[0].url.path == "/submissions/CIK0000320193.json"


def test_tenq_sections_skip_table_of_contents():
    s = sections.split_10q(TENQ)
    assert s["mdna"].startswith("Item 2. Management") and "Gross margin was 46.5" in s["mdna"]
    assert "no material changes to the risk factors" in s["risk_factors"]
    assert "repurchased 92 million" in s["repurchases"]
    assert "ordinary course" in s["legal"] and "Demand" not in s["legal"]


def test_diff_ignores_numbers_and_finds_new_language():
    before = ("Demand for our products remained strong across all regions in the quarter. "
              "Gross margin was 46.5 percent in the quarter driven by mix. We face intense competition in all markets.")
    after = ("Demand for our products remained healthy across most regions in the quarter. "
             "Gross margin was 47.1 percent in the quarter driven by mix. We face intense competition in all markets. "
             "Export restrictions on our products to certain countries could materially reduce revenue.")
    d = sections.diff_section("mdna", before, after)
    assert any("Export restrictions" in s for s in d.added)
    assert len(d.changed) == 1 and "healthy" in d.changed[0][1]
    assert not any("Gross margin" in b for _, b in d.changed)
    assert sections.word_changes(*d.changed[0]) == [("strong", "healthy"), ("all", "most")]


def test_html_to_text_drops_hidden_xbrl_header():
    html = '<html><body><div style="display:none"><ix:header>junk</ix:header></div><p>Item 2.02 Results</p><table><tr><td>Revenue</td><td>$35.1</td></tr></table></body></html>'
    t = sections.html_to_text(html)
    assert "junk" not in t and "Item 2.02 Results" in t and "Revenue | $35.1" in t


def test_flex_parsing_positions_trades_and_weights():
    out = ibkr.parse_flex(FLEX_XML)
    assert out["nav"] == 250000
    t = {p["ticker"]: p for p in out["positions"]}
    assert set(t) == {"MSFT", "BRK.B"}
    assert round(t["MSFT"]["weight_pct"], 2) == round(20408 / 250000 * 100, 2)
    assert round(t["BRK.B"]["pnl_pct"], 1) == -4.0
    assert out["trades"][0]["day"] == date(2025, 10, 15)


def test_flex_two_step_download():
    step = {"n": 0}

    def handler(req):
        if "SendRequest" in str(req.url):
            return httpx.Response(200, text="<FlexStatementResponse><Status>Success</Status><ReferenceCode>123</ReferenceCode>"
                                             "<Url>https://example.org/GetStatement</Url></FlexStatementResponse>")
        step["n"] += 1
        if step["n"] == 1:
            return httpx.Response(200, text="<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
                                             "<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>")
        return httpx.Response(200, text=FLEX_XML)
    xml = ibkr.fetch_flex("tok", "qid", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert "<FlexQueryResponse" in xml and step["n"] == 2


def test_alpaca_snapshots_and_bars():
    def handler(req):
        if req.url.path.endswith("/snapshots"):
            assert req.url.params["feed"] == "delayed_sip"
            return httpx.Response(200, json={"BRK.B": {
                "latestTrade": {"t": "2025-11-03T20:59:58.123456789Z", "p": 480.5},
                "dailyBar": {"t": "2025-11-03T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 480.0, "v": 3_000_000},
                "prevDailyBar": {"t": "2025-10-31T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 470.0, "v": 1}}})
        return httpx.Response(200, json={"bars": {"MSFT": [{"t": "2025-10-31T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
                                         "next_page_token": None})
    c = alpaca.AlpacaClient("k", "s", "delayed_sip", transport=httpx.MockTransport(handler))
    snap = c.snapshots(["BRK.B"])["BRK.B"]
    assert snap["price"] == 480.5 and snap["prev_close"] == 470.0
    assert snap["ts"] == datetime(2025, 11, 3, 20, 59, 58, 123456)
    bars = c.daily_bars(["MSFT"], date(2025, 10, 1), date(2025, 10, 31))
    assert bars["MSFT"][0]["day"] == date(2025, 10, 31)


def test_finnhub_calendar():
    def handler(req):
        assert req.url.params["token"] == "t"
        return httpx.Response(200, json={"earningsCalendar": [{"date": "2025-11-19", "epsActual": None, "epsEstimate": 0.89,
                                                                "hour": "amc", "quarter": 1, "revenueActual": None,
                                                                "revenueEstimate": 2.45e9, "symbol": "PANW", "year": 2026}]})
    c = finnhub.FinnhubClient("t", per_minute=6000, transport=httpx.MockTransport(handler))
    row = c.earnings_calendar(date(2025, 11, 1), date(2025, 11, 30))[0]
    assert row["ticker"] == "PANW" and row["hour"] == "amc" and row["day"] == date(2025, 11, 19)


def test_universe_merges_share_classes():
    members = universe.load_members()
    goog = [m for m in members if m.ticker in ("GOOGL", "GOOG")]
    assert len(goog) == 1 and goog[0].alt and goog[0].sector == "Communication Services"
    assert any(m.template == "bank" for m in members) and any(m.template == "reit" for m in members)
    n, added = universe.sync_universe()
    assert n == added == len(members)


def test_market_calendar():
    assert date(2026, 11, 26) in clock.holidays(2026)  # Thanksgiving
    assert date(2026, 7, 3) in clock.holidays(2026)  # July 4 falls on Saturday
    assert date(2026, 4, 3) in clock.holidays(2026)  # Good Friday
    assert date(2026, 11, 27) in clock.early_closes(2026)
    ny = clock.NY
    assert clock.session_state(datetime(2026, 11, 3, 10, 0, tzinfo=ny)) == "open"
    assert clock.session_state(datetime(2026, 11, 3, 8, 0, tzinfo=ny)) == "pre"
    assert clock.session_state(datetime(2026, 11, 26, 10, 0, tzinfo=ny)) == "closed"
    assert clock.last_session(datetime(2026, 11, 2, 9, 0, tzinfo=ny)) == date(2026, 10, 30)
    assert clock.add_trading_days(date(2026, 11, 25), 1) == date(2026, 11, 27)


def test_transcript_split_and_local_file(isolated_env):
    p = transcripts.local_path("MSFT", 2026, 1)
    p.parent.mkdir(parents=True)
    p.write_text("Prepared remarks " * 50 + "Operator: Our first question comes from Keith. Q: margins? A: up.")
    text = transcripts.get_transcript("MSFT", 2026, 1)
    prepared, qa = transcripts.split_qa(text)
    assert qa.startswith("first question") or "first question" in qa[:40]


def test_live_portfolio_from_ib_gateway():
    from types import SimpleNamespace as NS

    from metro import pipeline
    from metro.db import Holding, kv_get, session
    from metro.sources.ibkr import parse_portfolio
    item = lambda sym, pos, px, cost, typ="STK": NS(contract=NS(symbol=sym, secType=typ, currency="USD"), position=pos, marketPrice=px,
                                                 marketValue=pos * px, averageCost=cost, unrealizedPNL=(px - cost) * pos)
    vals = [NS(tag="NetLiquidation", value="100000", currency="USD"), NS(tag="TotalCashValue", value="25000", currency="USD")]
    pf = parse_portfolio([item("BRK B", 10, 500.0, 400.0), item("MSFT", 50, 400.0, 420.0), item("SPX", 1, 10.0, 9.0, "OPT")], vals)
    assert [p["ticker"] for p in pf["positions"]] == ["BRK.B", "MSFT"] and pf["nav"] == 100000 and pf["cash"] == 25000
    brk = pf["positions"][0]
    assert abs(brk["weight_pct"] - 5.0) < 1e-9 and abs(brk["pnl_pct"] - 25.0) < 1e-9
    assert parse_portfolio([], [])["nav"] is None  # not loaded yet: the caller must not wipe the holdings
    pipeline.apply_flex(pf, source="ibkr_live")
    with session() as s:
        assert {h.ticker for h in s.query(Holding)} == {"BRK.B", "MSFT"} and kv_get(s, "portfolio.last")["source"] == "ibkr_live"
    # the morning statement does not overwrite a fresher live portfolio
    pipeline.apply_flex({"positions": [{"ticker": "AAPL", "quantity": 1, "avg_cost": 1, "market_value": 1, "weight_pct": 1,
                                        "pnl_pct": 0, "currency": "USD"}], "nav": 1}, source="flex")
    with session() as s:
        assert {h.ticker for h in s.query(Holding)} == {"BRK.B", "MSFT"}
