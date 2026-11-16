"""The value by analysis, the market's weather and the company against its rivals."""
from metro.analytics import macro as MA
from metro.analytics import valuation as V
from metro.sources import fred


def test_growth_fades_to_the_long_run():
    g = V.expected_growth(20.0, 10.0, 80.0, "raised")  # profit growth above 60% counts as 60
    assert abs(g - (20 * .45 + 10 * .35 + 60 * .20 + 2)) < 1e-9
    assert V.expected_growth(None, None, None, None) is None
    assert V.expected_growth(200.0, None, None, None) == 40.0  # capped
    # after five years growth runs at the long-run rate
    a, b = V.eps_after(1.0, 30.0, 5.0), V.eps_after(1.0, 30.0, 6.0)
    assert abs(b / a - 1.04) < 0.002
    o = V.outlook(2.0, [20.0] * 30, 10.0)
    assert o["pe"] == 20.0 and abs(o["now"] - V.eps_after(2.0, 10.0, 1.0) * 20) < 1e-9
    assert o["path"]["1.0"] > o["now"] and o["path"]["5.0"] > o["path"]["2.0"]
    assert V.outlook(-1.0, [20.0] * 30, 10.0) is None


def test_fred_csv_and_readings():
    rows = fred.parse_csv("observation_date,DGS10\n2026-01-02,4.10\n2026-01-03,.\n2026-01-05,4.20\n")
    assert rows == [("2026-01-02", 4.1), ("2026-01-05", 4.2)]
    days = [f"2025-{m:02d}-01" for m in range(1, 13)] + [f"2026-{m:02d}-01" for m in range(1, 11)]
    cpi = [[d, 300 * 1.003 ** i] for i, d in enumerate(days)]
    series = {"DGS10": [[d, 4.0] for d in days], "DFF": [[d, 5.0 - i * 0.05] for i, d in enumerate(days)], "CPIAUCSL": cpi,
              "UNRATE": [[d, 4.0 + (0.6 if i > 18 else 0)] for i, d in enumerate(days)], "T10Y2Y": [[d, -0.3] for d in days],
              "VIXCLS": [[d, 33.0] for d in days], "BAMLH0A0HYM2": [[d, 3.5] for d in days]}
    out = MA.read(series, {"pe": 26.0, "earnings_yield": 100 / 26})
    by = {x["key"]: x for x in out["items"]}
    assert by["PE"]["tone"] == "down" and by["ERP"]["tone"] == "down"  # 3.8% earnings yield vs 4% bonds: no premium
    assert by["DFF"]["verdict"] == "הפד מוריד ריבית" and by["T10Y2Y"]["tone"] == "wr" and by["UNRATE"]["tone"] == "down"
    assert by["VIX"]["verdict"] == "פחד" and abs(by["CPI"]["value"] - round((1.003 ** 12 - 1) * 100, 1)) < 0.01
    mv = MA.market_valuation([{"s": "A", "cap": 100, "pe": 20, "ret": 10}, {"s": "A", "cap": 100, "pe": 40, "ret": 30}, {"s": "B", "cap": 50, "pe": -5, "ret": None}])
    assert abs(mv["pe"] - 250 / 7.5) < 1e-9 and mv["sectors"]["A"]["ret_1y"] == 20 and mv["sectors"]["B"]["pe"] is None


def test_api_macro_peers_and_analysis(isolated_env):
    from fastapi.testclient import TestClient

    from metro import demo
    from metro.api import app
    demo.generate(years=1.2)
    c = TestClient(app)
    m = c.get("/api/macro").json()
    assert m["demo"] and {"PE", "DGS10", "VIX"} <= {x["key"] for x in m["items"]} and len(m["sectors"]) == 11
    b = c.get("/api/company/MSFT").json()["biz"]
    assert b["peers"][0]["me"] and b["peers"][0]["t"] == "MSFT" and len(b["peers"]) >= 3
    assert b["dcf"]["shares"] and b["dcf"]["price"]
    row = next(x for x in c.get("/api/city").json()["companies"] if x["t"] == "MSFT")
    assert row["an"] and set(row["an"]["p"]) == {"0.25", "0.5", "1.0", "2.0", "5.0"} and "p" in row["gr"]


def test_value_by_analysis_is_conservative_and_says_how_sure():
    # a fast grower whose own history is full of very high multiples
    m = {"eps_ttm": 3.0, "growth_exp": 45.0, "pe_hist": [90.0, 60.0, 40.0, 35.0] * 15, "fcf_ttm": 60e9, "shares": 24e9, "net_debt": -40e9}
    o = V.outlook_v2(m, sector_pe=30.0, market_pe=20.0)
    assert o["g"] == 25.0 and o["pe"] == (sorted(m["pe_hist"])[30] + 30.0) / 2
    assert o["conf"] == "low" and any("25%" in w for w in o["why"])
    # the multiple fades toward the market's as growth fades: five years out it is paid like the market
    assert abs(o["path"]["5.0"] / o["now"] - V.eps_after(3.0, 25.0, 6.0) * 20.0 / o["by_pe"]) < 1e-9
    # a steady company with consistent numbers: high confidence
    s = {"eps_ttm": 5.0, "growth_exp": 7.0, "pe_hist": [20.0, 21.0, 19.0, 22.0] * 15, "fcf_ttm": 5.0 * 1e9 * 1.0, "shares": 1e9, "net_debt": 0}
    o2 = V.outlook_v2(s, sector_pe=21.0, market_pe=20.0)
    assert o2["conf"] in ("high", "medium") and 0.6 < o2["by_pe"] / o2["by_cash"] < 1.6
