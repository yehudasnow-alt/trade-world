"""The owner's view: ten fiscal years, the money in and out, and who owns the stock."""
from datetime import date

from metro.analytics import business as B
from metro.sources import ownership as OW
from metro.sources import xbrl


def _facts(years=(2019, 2020, 2021, 2022, 2023, 2024)):
    def fy(tag, vals, unit="USD"):
        return {tag: {"units": {unit: [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v, "filed": f"{y + 1}-02-15", "form": "10-K",
                                        "accn": f"a{y}"} for y, v in vals.items()]}}}

    def inst(tag, vals):
        return {tag: {"units": {"USD": [{"end": f"{y}-12-31", "val": v, "filed": f"{y + 1}-02-15", "form": "10-K", "accn": f"a{y}"}
                                        for y, v in vals.items()]}}}
    rev = {y: 100.0 * 1.1 ** i for i, y in enumerate(years)}
    g = {}
    for d in (fy("Revenues", rev), fy("OperatingIncomeLoss", {y: v * 0.25 for y, v in rev.items()}),
              fy("NetIncomeLoss", {y: v * 0.18 for y, v in rev.items()}),
              fy("NetCashProvidedByUsedInOperatingActivities", {y: v * 0.3 for y, v in rev.items()}),
              fy("PaymentsToAcquirePropertyPlantAndEquipment", {y: v * 0.05 for y, v in rev.items()}),
              fy("PaymentsForRepurchaseOfCommonStock", {y: v * 0.1 for y, v in rev.items()}),
              fy("IncomeTaxExpenseBenefit", {y: v * 0.05 for y, v in rev.items()}),
              fy("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", {y: v * 0.23 for y, v in rev.items()}),
              inst("StockholdersEquity", {y: 120.0 for y in years}), inst("LongTermDebt", {y: 30.0 for y in years}),
              inst("CashAndCashEquivalentsAtCarryingValue", {y: 50.0 for y in years})):
        g.update(d)
    # a quarter in between must not be taken for a year
    g["Revenues"]["units"]["USD"].append({"start": "2024-10-01", "end": "2024-12-31", "val": 40.0, "filed": "2025-02-15", "form": "10-K", "accn": "a2024"})
    return {"facts": {"us-gaap": g}}


def test_annual_years_and_roic():
    rows = xbrl.build_annual(_facts(), "1231")
    assert [r["fy"] for r in rows] == ["FY19", "FY20", "FY21", "FY22", "FY23", "FY24"]
    last = rows[-1]["values"]
    assert round(last["revenue"], 2) == round(100 * 1.1 ** 5, 2)
    assert round(last["fcf"], 4) == round(last["cfo"] - last["capex"], 4)
    tax = 0.05 / 0.23
    assert round(last["roic"], 2) == round(last["operating_income"] * (1 - tax) / (120 + 30 - 50) * 100, 2)
    lt = B.long_term(rows)
    assert lt["rev_up_years"] == 5 and lt["fcf_pos_years"] == 6 and round(lt["rev_cagr_5"], 1) == 10.0
    assert lt["returned_pct_fcf5"] == round(0.1 / 0.25 * 100)


def test_money_flow_is_a_household_budget():
    q = {"revenue": 100.0, "cost_of_revenue": 40.0, "rnd": 10.0, "sga": 20.0, "operating_income": 25.0, "net_income": 18.0,
         "income_tax": 5.0, "cfo": 30.0, "capex": 6.0, "dividends_paid": 4.0, "buybacks": 8.0}
    m = B.money_flow([{"fiscal": f"FQ{i}", "values": q} for i in range(4, 0, -1)], market_cap=4000)
    assert m["revenue"] == 400 and m["other_opex"] == 20 and m["below_oi"] == 28
    assert m["fcf"] == 96 and m["kept"] == 96 - 16 - 32 and m["per_month_net"] == 6 and m["per_100_net"] == 18
    assert m["fcf_yield"] == 2.4 and m["holder_yield"] == 1.2


XML_13G = """<?xml version="1.0"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g"><formData><coverPageHeaderReportingPersonDetails>
<reportingPersonName>The Vanguard Group</reportingPersonName><aggregateAmountOwned>1234567</aggregateAmountOwned>
<classPercent>8.9</classPercent></coverPageHeaderReportingPersonDetails></formData></edgarSubmission>"""

TEXT_13D = """SCHEDULE 13D
1. NAMES OF REPORTING PERSONS
I.R.S. IDENTIFICATION NOS. OF ABOVE PERSONS (ENTITIES ONLY)
Jane Founder
11. AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON
45,000,000
13. PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)
22.4%
"""

FORM4 = """<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle><postTransactionAmounts><sharesOwnedFollowingTransaction><value>1000</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
<nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle><postTransactionAmounts><sharesOwnedFollowingTransaction><value>800</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature></nonDerivativeTransaction>
<nonDerivativeHolding><securityTitle><value>Common Stock</value></securityTitle><postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts><ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership><natureOfOwnership><value>By Family Trust</value></natureOfOwnership></ownershipNature></nonDerivativeHolding>
</nonDerivativeTable></ownershipDocument>"""


def test_ownership_parsers():
    h = OW.parse_13dg(XML_13G, "SCHEDULE 13G")
    assert (h.name, h.pct, h.shares, h.active) == ("The Vanguard Group", 8.9, 1234567, False)
    d = OW.parse_13dg(TEXT_13D, "SC 13D")
    assert d.name == "Jane Founder" and d.pct == 22.4 and d.shares == 45_000_000 and d.active
    st = OW.parse_form4_stake(FORM4)
    assert st.name == "Doe John" and st.title == "Chief Executive Officer" and st.shares == 800 + 5000
    assert OW.raw_doc_name("xslSCHEDULE_13G_X01/primary_doc.xml") == "primary_doc.xml"
    o = B.ownership([{"kind": "major", "name": "Jane Founder", "pct": 22.4}, {"kind": "major", "name": "Index Fund", "pct": 8.0},
                     {"kind": "insider", "name": "Jane Founder", "pct": 22.4}, {"kind": "insider", "name": "CEO", "pct": 0.5}], 1e9)
    assert o["insiders_pct"] == 22.9 and o["majors_pct"] == 8.0 and o["public_pct"] == 69.1 and not o["controlled"]


class FakeSec:
    def __init__(self):
        self.docs = {"https://www.sec.gov/Archives/edgar/data/1/000000000124000001/primary_doc.xml": XML_13G,
                     "https://www.sec.gov/Archives/edgar/data/1/000000000124000002/form4.xml": FORM4}

    def submissions(self, cik, ttl=0):
        rec = {"accessionNumber": ["0000000001-24-000001", "0000000001-24-000002"], "form": ["SCHEDULE 13G", "4"],
               "filingDate": ["2026-02-10", "2026-05-01"], "acceptanceDateTime": ["", ""], "reportDate": ["", ""], "items": ["", ""],
               "primaryDocument": ["xslSCHEDULE_13G_X01/primary_doc.xml", "xslF345X05/form4.xml"]}
        return {"cik": "1", "filings": {"recent": rec}}

    def get(self, url, ttl=None):
        return self.docs[url].encode()


def test_collect_and_api(isolated_env):
    got = OW.collect(FakeSec(), 1, 100_000_000, today=date(2026, 11, 13))
    assert got["majors"][0]["pct"] == 8.9 and got["insiders"][0]["shares"] == 5800
    assert round(got["insiders"][0]["pct"], 4) == 0.0058

    from fastapi.testclient import TestClient

    from metro import demo
    from metro.api import app
    demo.generate(years=1.2)
    js = TestClient(app).get("/api/company/MSFT").json()
    b = js["biz"]
    assert len(b["annual"]) == 11 and b["long"]["rev_years"] == 10
    assert b["money"]["revenue"] > 0 and b["money"]["per_month_net"] is not None
    assert b["owners"]["majors"] and b["owners"]["insiders"] and 0 < b["owners"]["public_pct"] < 100
    assert b["profile"]["profile"]["demo"] is True and b["profile_state"] == "needs_key"


def test_profile_is_written_from_the_annual_report(isolated_env, monkeypatch):
    from metro import config, universe
    from metro.ai import profile as AP
    from metro.ai.schema import CompanyProfile as Schema
    from metro.db import CompanyProfile, Filing, session
    from metro.pipeline import save_doc
    universe.sync_universe()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config.get_settings.cache_clear()
    quote = "We sell subscriptions to businesses of every size around the world"
    with session() as s:
        s.add(Filing(accession="0001-25-000001", ticker="MSFT", cik=789019, form="10-K", filed=date(2025, 7, 30), primary_doc="x.htm",
                     items=[], status="parsed"))
    save_doc("MSFT", "0001-25-000001", "sections.json", {"business": f"Overview. {quote}. More text.", "risk_factors": "Risks."})
    data = {"one_liner_he": "תוכנה", "what_it_does_he": "x", "segments": [], "customers_he": "עסקים", "customer_concentration_he": "אין",
            "top_customers": [], "future_customers_he": "x", "competitors": [], "moat_type": "switching_costs", "moat_strength": "wide",
            "moat_he": "x", "moat_evidence": [{"text_he": "x", "citation": {"quote": quote, "source": "filing", "section": "Item 1"}}],
            "ai_impact": "mixed", "ai_impact_he": "x", "ai_evidence": [], "durability": "high", "durability_he": "x",
            "capital_allocation_he": "x", "key_risks": [], "what_to_watch_he": ["x"]}
    Schema.model_validate(data)
    seen = {}

    class Eng:
        def run(self, params, kind):
            seen["kind"], seen["body"] = kind, params["messages"][0]["content"]
            return data, {}, 0.12

    AP.request("MSFT")
    assert AP.run_profiles(Eng()) == {"done": 1, "errors": 0}
    assert seen["kind"] == "profile" and quote in seen["body"]
    with session() as s:
        p = s.query(CompanyProfile).one()
        assert p.accession == "0001-25-000001" and p.verification["quotes"]["exact"] == 1 and p.cost_usd == 0.12
    assert AP.run_profiles(Eng()) == {"done": 0, "errors": 0}  # nothing left: the same report is not profiled twice
