from datetime import date

from metro.sources import xbrl


def by_end(rows):
    return {r.end.isoformat(): r for r in rows}


def test_q4_is_full_year_minus_nine_months(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    q4 = rows["2024-09-28"]
    assert q4.values["revenue"] == 380.0 - 275.0
    assert "revenue" in q4.derived
    assert q4.fiscal == "FQ4 FY24"


def test_cash_flow_quarters_come_from_year_to_date_differences(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    assert rows["2023-12-30"].values["cfo"] == 40.0
    assert rows["2024-03-30"].values["cfo"] == 22.0
    assert rows["2024-06-29"].values["cfo"] == 29.0
    assert rows["2024-09-28"].values["cfo"] == 27.0
    assert rows["2024-03-30"].values["fcf"] == 22.0 - 2.5


def test_restatement_keeps_latest_and_first(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    q2 = rows["2024-03-30"]
    assert q2.values["revenue"] == 92.0 and q2.first_values["revenue"] == 90.0
    assert "revenue" in q2.restated


def test_point_in_time_hides_later_restatements(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928", as_of=date(2024, 6, 1)))
    assert rows["2024-03-30"].values["revenue"] == 90.0
    assert "2024-09-28" not in rows


def test_tag_switch_keeps_older_periods(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    assert rows["2022-12-31"].values["revenue"] == 117.0


def test_eps_q4_approximation(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    assert abs(rows["2024-09-28"].values["eps_diluted"] - (6.08 - 5.11)) < 1e-9
    assert "eps_diluted" in rows["2024-09-28"].derived


def test_instants_and_cover_shares(facts_fy_sep):
    rows = by_end(xbrl.build_quarters(facts_fy_sep, "0928"))
    q3 = rows["2024-06-29"]
    assert q3.values["cash"] == 25.0
    assert q3.values["net_debt"] == 75.0
    assert q3.values["shares_out"] == 15_222_259_000


def test_labels_for_shifted_fiscal_years():
    assert xbrl.fiscal_label(date(2025, 6, 28), "0927") == "FQ3 FY25"
    assert xbrl.fiscal_label(date(2025, 7, 27), "0126") == "FQ2 FY26"  # NVIDIA-style year ending late January
    assert xbrl.fiscal_label(date(2025, 3, 31), "1231") == "FQ1 FY25"
    assert xbrl.calendar_label(date(2025, 4, 28), date(2025, 7, 27)) == "CY2025Q2"


def test_ttm_and_yoy(facts_fy_sep):
    rows = xbrl.build_quarters(facts_fy_sep, "0928")
    fy24 = [r for r in rows if date(2023, 10, 1) <= r.end <= date(2024, 9, 28)]
    assert xbrl.ttm(fy24, "revenue") == 100 + 92 + 85 + 105
    growth = xbrl.yoy(rows, "revenue")  # 2024-12-28 vs 2023-12-30
    assert round(growth, 1) == 24.0


def test_q4_from_three_quarters_when_no_nine_month_figure():
    f = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-03-31", "val": 10, "filed": "2024-05-01", "form": "10-Q", "accn": "a"},
        {"start": "2024-04-01", "end": "2024-06-30", "val": 11, "filed": "2024-08-01", "form": "10-Q", "accn": "b"},
        {"start": "2024-07-01", "end": "2024-09-30", "val": 12, "filed": "2024-11-01", "form": "10-Q", "accn": "c"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 50, "filed": "2025-02-10", "form": "10-K", "accn": "d"},
    ]}}}}}
    rows = by_end(xbrl.build_quarters(f, "1231"))
    assert rows["2024-12-31"].values["revenue"] == 17
