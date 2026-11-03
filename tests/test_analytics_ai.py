import json
import math
import random
from datetime import date, timedelta
from types import SimpleNamespace

from metro.ai import engine, verify
from metro.ai.schema import COVERAGE
from metro.analytics import events, scoring, technicals, thesis, valuation

FORM4 = """<?xml version="1.0"?><ownershipDocument>
<issuer><issuerCik>0001327567</issuerCik><issuerTradingSymbol>PANW</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>0</isOfficer></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>2025-11-21</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>2500</value></transactionShares><transactionPricePerShare><value>182.4</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>2025-11-21</value></transactionDate>
<transactionCoding><transactionCode>M</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>9000</value></transactionShares><transactionPricePerShare><value>40</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


def walk(n=320, seed=1, drift=0.0004, vol=0.015, start=100.0):
    r = random.Random(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + drift + r.gauss(0, vol)))
    return out


def test_decomposition_recovers_betas():
    r = random.Random(7)
    mkt, sec, stk = [100.0], [100.0], [100.0]
    for _ in range(300):
        m = r.gauss(0.0003, 0.01)
        s = m + r.gauss(0, 0.006)
        stk.append(stk[-1] * (1 + 1.3 * m + 0.8 * (s - m) + r.gauss(0, 0.004)))
        mkt.append(mkt[-1] * (1 + m))
        sec.append(sec[-1] * (1 + s))
    d = technicals.decompose(stk, mkt, sec)
    assert abs(d.beta_market - 1.3) < 0.12 and abs(d.beta_sector - 0.8) < 0.2
    today = (stk[-1] / stk[-2] - 1) * 100
    assert math.isclose(d.market + d.sector + d.idio, today, abs_tol=1e-9)


def test_indicators_basic():
    c = walk()
    h = [x * 1.01 for x in c]
    l = [x * 0.99 for x in c]
    assert 1.5 < technicals.atr_pct(h, l, c) < 4
    assert 0 <= technicals.rsi(c) <= 100
    dd = technicals.drawdown(c, h, l)
    assert dd["dd_pct"] <= 0 and dd["high52"] >= c[-1]
    assert technicals.rvol(2_000_000, [1_000_000] * 30, progress=0.5) == 4.0


def test_valuation_helpers():
    assert valuation.percentile_rank(10, list(range(1, 21))) == 50.0
    g = valuation.implied_growth(1000.0, 30.0)
    assert 5 < g < 25
    days = [date(2021, 1, 1) + timedelta(days=i) for i in range(800)]
    closes = [100 + i * 0.05 for i in range(800)]
    eps = [(date(2021, 2, 1), 4.0), (date(2022, 2, 1), 5.0)]
    hist = valuation.pe_history(days, closes, eps)
    assert hist and all(10 < x < 40 for x in hist)
    fr = valuation.fair_range(5.0, hist, 10)
    assert fr["bear"] < fr["base"] < fr["bull"]


def test_8k_classification_and_exec_departure():
    ev = events.classify_8k(["2.02", "9.01"])
    assert [e.code for e in ev] == ["2.02"]
    text = {"5.02": "Item 5.02 On November 3, the Chief Financial Officer notified the Company of her resignation, effective immediately."}
    ev = events.classify_8k(["5.02"], text)
    assert ev[0].code == "5.02-exec" and ev[0].severity == "veto"
    ev = events.classify_8k(["5.02"], {"5.02": "Item 5.02 The Board appointed John Roe as a director effective December 1."})
    assert ev[0].severity == "watch"
    assert events.classify_8k(["4.02"])[0].severity == "veto"


def test_form4_open_market_buys_only():
    txs = events.parse_form4(FORM4)
    buys = events.open_market_buys(txs)
    assert len(txs) == 2 and len(buys) == 1
    assert buys[0].title == "Director" and round(buys[0].value) == 456000
    assert events.form4_xml_name("xslF345X05/wk-form4_1.xml") == "wk-form4_1.xml"


def _inputs(t, sector, seed, rev_growth):
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(320)]
    c = walk(seed=seed)
    q = []
    rev = 100.0
    for i in range(9):
        end = date(2023, 12, 31) + timedelta(days=91 * i)
        rev *= 1 + rev_growth / 400
        q.append({"end": end, "values": {"revenue": rev, "gross_margin": 40 + seed, "op_margin": 20 + seed, "fcf": rev * 0.2,
                                         "operating_income": rev * 0.2, "net_income": rev * 0.15, "eps_diluted": 1.0 + i * 0.05,
                                         "net_debt": 50.0, "sbc_pct": 3.0, "diluted_shares": 100.0, "shares_out": 100.0}})
    return scoring.Inputs(t, sector, "standard", days, c, [x * 1.01 for x in c], [x * 0.99 for x in c],
                          [1e6] * 320, walk(seed=99), walk(seed=98), quarters=q,
                          eps_points=[(date(2024, 2, 1), 4.0), (date(2024, 8, 1), 4.4)])


def test_score_universe_ranges_and_veto():
    raws = {}
    for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        raws[t] = scoring.raw_metrics(_inputs(t, "Information Technology", i, 10 + i * 5))
    raws["DDD"]["veto"] = ["אין להסתמך על דוחות קודמים"]
    out = scoring.score_universe(raws)
    for t, s in out.items():
        assert 0 <= s["total"] <= 100
        for k in ("quality", "direction", "valuation", "timing"):
            assert 0 <= s[k] <= 25
    assert out["DDD"]["total"] == 0 and not out["DDD"]["beacon"]
    assert out["CCC"]["quality"] > out["AAA"]["quality"]


def test_thesis_states():
    th = {"metrics": [{"name": "rev_yoy", "op": ">", "value": 20}, {"name": "gross_margin", "op": ">", "value": 70}],
          "kills": [{"type": "guidance_lowered"}, {"type": "price_below", "value": 100}]}
    ok = thesis.evaluate(th, {"rev_yoy": 25, "gross_margin": 72, "price": 150}, [], "maintained")
    assert ok["status"] == "ok"
    watch = thesis.evaluate(th, {"rev_yoy": 18, "gross_margin": 72, "price": 150}, [], "maintained")
    assert watch["status"] == "watch" and "צמיחת הכנסות" in watch["reasons"][0]
    broken = thesis.evaluate(th, {"rev_yoy": 25, "gross_margin": 72, "price": 95}, [], "maintained")
    assert broken["status"] == "broken"
    assert thesis.evaluate(th, {"rev_yoy": 25, "gross_margin": 72, "price": 150}, [], "lowered")["status"] == "broken"


PRESS = ("Revenue for the third quarter was $35.1 billion, up 94% from a year ago. GAAP earnings per diluted share "
         "for the quarter were $0.78. Outlook: revenue is expected to be $37.5 billion, plus or minus 2%. "
         "Data Center revenue was a record $30.8 billion, up 112% from a year ago.")


def _card():
    cov = [{"key": k, "status": "not_reported", "severity": "neutral", "finding_he": "לא דווח", "citations": []} for k in COVERAGE]
    return {
        "headline_he": "x", "what_happened_he": "x", "what_next_he": "x", "guidance_direction": "raised", "where_going_he": "x",
        "growth_drivers_he": [], "risks_he": [], "qa": [], "promises_new": [], "promises_check": [], "risk_changes": [], "flags": [],
        "coverage": cov,
        "metrics": [
            {"key": "revenue", "label": "Revenue", "value": 35.1, "scale": "billions", "unit": "USD", "period": "Q3", "yoy_pct": 94,
             "gaap": True, "note_he": "", "citation": {"quote": "Revenue for the third quarter was $35.1 billion, up 94% from a year ago.",
                                                        "source": "press_release", "section": "PR"}},
            {"key": "eps_diluted", "label": "EPS", "value": 0.78, "scale": "units", "unit": "USD_per_share", "period": "Q3",
             "yoy_pct": None, "gaap": True, "note_he": "", "citation": None},
            {"key": "kpi", "label": "Made up", "value": 12.3, "scale": "billions", "unit": "USD", "period": "Q3", "yoy_pct": None,
             "gaap": False, "note_he": "", "citation": {"quote": "This sentence does not appear anywhere in the release at all.",
                                                         "source": "press_release", "section": "PR"}},
        ],
        "guidance": [{"metric": "Revenue", "period": "Q4", "low": 37.5, "high": 37.5, "scale": "billions", "unit": "USD",
                      "prior_low": None, "prior_high": None, "direction": "initiated", "note_he": "", "citation": None}],
    }


def test_verify_numbers_and_quotes():
    card = _card()
    rep = verify.verify_card(card, {"press_release": PRESS}, {"revenue": 35_082_000_000, "eps_diluted": 0.78})
    assert card["metrics"][0]["_n"] == "xbrl" and card["metrics"][0]["citation"]["_q"] == "exact"
    assert card["metrics"][1]["_n"] == "xbrl"
    assert card["metrics"][2]["_n"] == "unverified" and card["metrics"][2]["citation"]["_q"] == "missing"
    assert card["guidance"][0]["_n_low"] == "text"
    assert rep["coverage_keys"] == 15 and len(rep["problems"]) == 2


def test_verify_close_quote_with_typography():
    idx = verify.SourceIndex({"press_release": PRESS.replace("a year ago", "a year ago")})
    assert idx.find_quote("Data Center revenue was a record $30.8 billion, up 112% from a year ago") == "exact"
    assert idx.find_quote("Data Center revenue was a record $30.8 billion, up 112 percent from a year ago") == "close"


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.sent.append(params)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
                               usage=SimpleNamespace(input_tokens=90_000, output_tokens=8_000, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


def test_engine_builds_strict_request_and_parses():
    ctx = engine.Context("NVDA", "Nvidia", "Semiconductors", "FQ3 FY26 (Aug-Oct 2025)",
                         xbrl_rows=[{"label": "FQ3 FY26", "values": {"revenue": 35.08e9, "gross_margin": 74.6}}],
                         promises=[{"id": 4, "metric": "gross margin", "target": "mid-70s", "deadline": "FY26", "quote": "q"}])
    src = engine.Sources(press_release=PRESS, filing="Item 2. MD&A ...", filing_sections="mdna")
    params = engine.build_card(ctx, src)
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    body = params["messages"][0]["content"]
    assert "<checklist>" in body and "id=4" in body and "35,080,000,000" in body
    fake = FakeClient(_card())
    data, usage, cost = engine.Engine(fake).run(params, "card")
    assert data["guidance_direction"] == "raised"
    assert cost == round((90_000 * 2 + 8_000 * 10) / 1e6, 5)


def test_cost_tiers():
    assert engine.cost_usd("claude-haiku-5-5", {"input_tokens": 50_000, "output_tokens": 2_000}) == round((50_000 * 0.1 + 2_000 * 0.5) / 1e6, 5)
    assert engine.cost_usd("claude-haiku-5-5", {"input_tokens": 150_000, "output_tokens": 2_000}) == round((150_000 * 0.5 + 2_000 * 2.5) / 1e6, 5)
    assert engine.cost_usd("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 0}, batch=True) == 1.0
