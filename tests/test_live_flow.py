"""The live path end to end with recorded shapes: SEC filing -> sections, facts, events -> Claude job -> verified card."""
import json
from types import SimpleNamespace

from sqlalchemy import select

from metro import pipeline
from metro.ai import runner
from metro.ai.schema import COVERAGE
from metro.db import AiJob, Company, Event, Filing, Holding, Quarter, ReportCard, session
from metro.sources import sec
from metro.universe import sync_universe

TENQ_HTML = """<html><body><p>PART I</p><p>Item 1. Business</p><p>We design and sell devices and services to customers around the world.</p>
<p>Item 1A. Risk Factors</p><p>Our business depends on component suppliers in several countries and could suffer if supply is disrupted.</p>
<p>PART II</p><p>Item 7. Management's Discussion and Analysis</p><p>Revenue was $105 million for the fourth quarter, compared with prior year.</p>
<p>Demand remained strong across all regions in the quarter and margins expanded meaningfully.</p>
<p>Item 8. Financial Statements</p><p>Revenue table and notes about segments and customers here.</p></body></html>"""
PRESS = "Revenue for the quarter was $105 million. The company expects revenue of $110 million to $115 million next quarter."


class FakeSec:
    def __init__(self, facts):
        self.facts = facts

    def submissions(self, cik, ttl=300):
        return {"cik": str(cik), "fiscalYearEnd": "0928", "filings": {"recent": {
            "accessionNumber": ["0000320193-24-000123", "0000320193-24-000120"], "filingDate": ["2024-11-01", "2024-10-31"],
            "reportDate": ["2024-09-28", "2024-10-31"], "acceptanceDateTime": ["2024-11-01T16:05:00.000Z", "2024-10-31T16:30:00.000Z"],
            "form": ["10-K", "8-K"], "items": ["", "2.02,9.01"], "primaryDocument": ["aapl-20240928.htm", "aapl-8k.htm"]}}}

    def companyfacts(self, cik, ttl=0):
        return self.facts

    def text(self, url):
        from metro.sources.sections import html_to_text
        return html_to_text(TENQ_HTML) if "20240928" in url else PRESS

    def filing_documents(self, ref):
        return [{"type": "8-K", "url": ref.url}, {"type": "EX-99.1", "url": ref.folder + "/ex991.htm"}]

    def get(self, url, ttl=None):
        return b""


class FakeEngine:
    def __init__(self):
        self.calls = []

    def run(self, params, kind):
        self.calls.append((params["model"], kind))
        if kind == "review":
            return {"verdict": "pass", "issues": []}, {}, 0.01
        card = {"headline_he": "x", "what_happened_he": "x", "what_next_he": "x", "where_going_he": "x", "guidance_direction": "raised",
                "growth_drivers_he": [], "risks_he": [], "qa": [], "promises_check": [], "risk_changes": [], "flags": [],
                "promises_new": [{"metric": "revenue", "target_he": "110-115", "deadline_he": "next quarter",
                                  "citation": {"quote": "The company expects revenue of $110 million to $115 million next quarter.",
                                               "source": "press_release", "section": "PR"}}],
                "coverage": [{"key": k, "status": "not_reported", "severity": "neutral", "finding_he": "", "citations": []} for k in COVERAGE],
                "metrics": [{"key": "revenue", "label": "Revenue", "value": 105, "scale": "millions", "unit": "USD", "period": "Q4",
                             "yoy_pct": None, "gaap": True, "note_he": "", "citation": None}],
                "guidance": []}
        return card, {"input_tokens": 1000, "output_tokens": 100}, 0.02


def test_filing_to_verified_card(facts_fy_sep):
    sync_universe()
    with session() as s:
        s.add(Holding(ticker="AAPL", quantity=10))
    facts = json.loads(json.dumps(facts_fy_sep).replace('"val": 380.0', '"val": 380000000.0').replace('"val": 275.0', '"val": 275000000.0'))
    client = FakeSec(facts)
    assert pipeline.sync_filings(client, ["AAPL"], days=4000) == 2
    assert pipeline.process_new(client) == {"parsed": 2}
    with session() as s:
        ev = list(s.scalars(select(Event).where(Event.ticker == "AAPL")))
        assert [e.code for e in ev] == ["2.02"]
        jobs = list(s.scalars(select(AiJob)))
        assert {(j.stage, j.deep) for j in jobs} == {("release", True), ("final", True)}
        assert s.scalar(select(Quarter.id).where(Quarter.ticker == "AAPL")) is not None
        assert s.get(Company, "AAPL").fiscal_year_end == "0928"
    secs = pipeline.load_doc("AAPL", "0000320193-24-000123", "sections.json")
    assert "mdna" in secs and "risk_factors" in secs
    eng = FakeEngine()
    stats = runner.run_jobs(eng)
    assert stats["single"] == 2 and stats["errors"] == 0
    assert ("claude-opus-5-5", "review") in eng.calls
    with session() as s:
        cards = list(s.scalars(select(ReportCard)))
        assert len(cards) == 2
        final = next(c for c in cards if c.stage == "final")
        assert final.card["metrics"][0]["_n"] == "xbrl"  # Q4 revenue 105M = FY - 9M from the fixture
        assert final.verification["quotes"]["exact"] == 1
        assert final.review["verdict"] == "pass"
        assert any(e.code == "guidance-raised" for e in s.scalars(select(Event)))
