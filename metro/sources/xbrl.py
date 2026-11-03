"""Turn SEC companyfacts into clean fiscal quarters.

Handles the traps listed in the plan:
  * 10-Q cash-flow lines are year-to-date: quarters come from differences (6M-3M, 9M-6M, FY-9M).
  * There is no separate Q4 filing: Q4 = FY - 9M (or FY - Q1 - Q2 - Q3).
  * Restatements: every period keeps the latest value and the first-filed value + date
    ("as reported then" vs "current"), so backtests can stay point-in-time.
  * Companies switch tags over time (Revenues -> RevenueFromContract...): per-period priority.
  * 52/53-week fiscal years: duration buckets use tolerances, labels use the period midpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "20-F/A", "40-F", "6-K", "10-KT", "10-QT"}

# key -> candidates in priority order; ("dei", name) for the dei taxonomy, ("ifrs-full", name) for IFRS filers
FLOW: dict[str, list[Any]] = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "RevenuesNetOfInterestExpense", ("ifrs-full", "Revenue")],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", ("ifrs-full", "CostOfSales")],
    "gross_profit": ["GrossProfit", ("ifrs-full", "GrossProfit")],
    "operating_income": ["OperatingIncomeLoss", ("ifrs-full", "ProfitLossFromOperatingActivities")],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", ("ifrs-full", "ProfitLoss")],
    "rnd": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "net_interest_income": ["InterestIncomeExpenseNet", "InterestIncomeExpenseAfterProvisionForLoanLoss"],
    "noninterest_income": ["NoninterestIncome"],
    "credit_provision": ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForLoanAndLeaseLosses", "ProvisionForCreditLosses"],
}
# non-additive durations: never sum, Q4 is an approximation
PER_SHARE: dict[str, list[Any]] = {
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", ("ifrs-full", "DilutedEarningsLossPerShare")],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"],
}
INSTANT: dict[str, list[Any]] = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             ("ifrs-full", "CashAndCashEquivalents")],
    "short_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "debt_total": ["DebtLongtermAndShorttermCombinedAmount", "LongTermDebt"],
    "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "short_borrowings": ["CommercialPaper", "ShortTermBorrowings"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "total_assets": ["Assets", ("ifrs-full", "Assets")],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "inventory": ["InventoryNet"],
    "receivables": ["AccountsReceivableNetCurrent"],
    "deferred_revenue": ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"],
    "rpo": ["RevenueRemainingPerformanceObligation"],
    "goodwill": ["Goodwill"],
    "shares_out": [("dei", "EntityCommonStockSharesOutstanding"), "CommonStockSharesOutstanding"],
}
ALL_CONCEPTS = {k: v for d in (FLOW, PER_SHARE, INSTANT) for k, v in d.items()}

Q_DAYS = (77, 105)
H_DAYS = (168, 196)
N_DAYS = (255, 287)
Y_DAYS = (350, 378)


@dataclass
class Obs:
    start: date | None
    end: date
    val: float
    filed: date
    accn: str
    form: str
    concept: str
    rank: int = 0  # candidate priority (0 = best)


@dataclass
class PeriodVal:
    start: date | None
    end: date
    latest: float
    first: float
    first_filed: date
    latest_filed: date
    derived: bool = False

    @property
    def restated(self) -> bool:
        return abs(self.latest - self.first) > max(1.0, abs(self.first) * 0.002)


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _days(o: Obs | PeriodVal) -> int:
    return (o.end - o.start).days + 1 if o.start else 0


def _between(n: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= n <= rng[1]


def _resolve(spec: Any) -> tuple[str, str]:
    return spec if isinstance(spec, tuple) else ("us-gaap", spec)


def extract(facts: dict[str, Any], key: str, as_of: date | None = None) -> list[Obs]:
    """All observations for a concept key across its candidate tags, optionally as known on `as_of`."""
    out: list[Obs] = []
    root = facts.get("facts", {})
    for rank, spec in enumerate(ALL_CONCEPTS[key]):
        tax, name = _resolve(spec)
        node = root.get(tax, {}).get(name)
        if not node:
            continue
        units = node.get("units", {})
        unit = "USD/shares" if key == "eps_diluted" else ("shares" if key in ("diluted_shares", "shares_out") else "USD")
        rows = units.get(unit)
        if rows is None:  # foreign filers report in their own currency
            rows = next((v for u, v in units.items() if "/" not in u and u != "shares" and unit == "USD"), None)
            if rows is None and unit == "USD/shares":
                rows = next((v for u, v in units.items() if u.endswith("/shares")), None)
        for r in rows or []:
            if r.get("form") not in FORMS:
                continue
            filed = _date(r.get("filed"))
            if filed is None or (as_of and filed > as_of):
                continue
            out.append(Obs(_date(r.get("start")), _date(r["end"]), float(r["val"]), filed, r.get("accn", ""),
                           r.get("form", ""), name, rank))
    return out


def _periods(obs: Iterable[Obs]) -> dict[tuple[date | None, date], PeriodVal]:
    """One value per (start, end): best-ranked concept; latest and first-filed values."""
    groups: dict[tuple[date | None, date], list[Obs]] = {}
    for o in obs:
        groups.setdefault((o.start, o.end), []).append(o)
    out = {}
    for k, g in groups.items():
        best = min(o.rank for o in g)
        g = [o for o in g if o.rank == best]
        latest = max(g, key=lambda o: (o.filed, o.accn))
        first = min(g, key=lambda o: (o.filed, o.accn))
        out[k] = PeriodVal(k[0], k[1], latest.val, first.val, first.filed, latest.filed)
    return out


def quarterize_flow(obs: list[Obs], additive: bool = True) -> dict[date, PeriodVal]:
    """Quarter values keyed by quarter end date."""
    per = _periods(obs)
    q: dict[date, PeriodVal] = {}
    for (s, e), pv in per.items():
        if s and _between(_days(pv), Q_DAYS):
            q[e] = pv
    if not additive:
        # Q4 approximation for EPS: FY - 9M year-to-date, when both are reported
        for (s, e), pv in per.items():
            if s and _between(_days(pv), Y_DAYS) and e not in q:
                nine = next((x for (s2, e2), x in per.items() if s2 == s and _between(_days(x), N_DAYS)), None)
                if nine:
                    q[e] = PeriodVal(nine.end + timedelta(days=1), e, pv.latest - nine.latest, pv.first - nine.first,
                                     max(pv.first_filed, nine.first_filed), max(pv.latest_filed, nine.latest_filed), True)
        return q
    # cumulative chains that share a start date: 3M, 6M, 9M, FY
    by_start: dict[date, list[PeriodVal]] = {}
    for (s, e), pv in per.items():
        if s:
            by_start.setdefault(s, []).append(pv)
    for s, chain in by_start.items():
        chain.sort(key=lambda p: p.end)
        for prev, cur in zip(chain, chain[1:]):
            gap = (cur.end - prev.end).days
            if not _between(gap, Q_DAYS) or cur.end in q:
                continue
            q[cur.end] = PeriodVal(prev.end + timedelta(days=1), cur.end, cur.latest - prev.latest, cur.first - prev.first,
                                   max(cur.first_filed, prev.first_filed), max(cur.latest_filed, prev.latest_filed), True)
    # Q4 = FY - (Q1 + Q2 + Q3) when no 9M figure exists
    for (s, e), pv in per.items():
        if not s or not _between(_days(pv), Y_DAYS) or e in q:
            continue
        inside = sorted((x for x in q.values() if x.start and x.start >= s and x.end < e), key=lambda x: x.end)
        if len(inside) == 3 and inside[0].start and abs((inside[0].start - s).days) <= 7:
            q[e] = PeriodVal(inside[-1].end + timedelta(days=1), e, pv.latest - sum(x.latest for x in inside),
                             pv.first - sum(x.first for x in inside), max([pv.first_filed] + [x.first_filed for x in inside]),
                             max([pv.latest_filed] + [x.latest_filed for x in inside]), True)
    return q


def instants(obs: list[Obs]) -> dict[date, PeriodVal]:
    return {e: pv for (s, e), pv in _periods(obs).items() if s is None}


def fiscal_label(end: date, fye: str | None) -> str:
    """'FQ2 FY26' given the fiscal year end as MMDD (e.g. '0928' for Apple)."""
    if not fye or len(fye) != 4:
        fye = "1231"
    m, d = int(fye[:2]), int(fye[2:])
    d = min(d, 28)

    def fy_end(y: int) -> date:
        return date(y, m, d)

    fy = end.year if end <= fy_end(end.year) + timedelta(days=10) else end.year + 1
    months = round((fy_end(fy) - end).days / 30.44)
    q = max(1, min(4, 4 - round(months / 3)))
    return f"FQ{q} FY{fy % 100:02d}"


def calendar_label(start: date | None, end: date) -> str:
    start = start or end - timedelta(days=90)
    mid = start + (end - start) / 2
    return f"CY{mid.year}Q{(mid.month - 1) // 3 + 1}"


@dataclass
class QuarterRow:
    end: date
    start: date | None
    fiscal: str
    calendar: str
    first_filed: date | None
    values: dict[str, float] = field(default_factory=dict)
    first_values: dict[str, float] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)
    restated: list[str] = field(default_factory=list)


def _near(d: date, pool: dict[date, Any], tol: int = 4) -> date | None:
    if d in pool:
        return d
    best = min(pool, key=lambda x: abs((x - d).days), default=None)
    return best if best is not None and abs((best - d).days) <= tol else None


def build_quarters(facts: dict[str, Any], fye: str | None = None, as_of: date | None = None,
                   min_end: date | None = None) -> list[QuarterRow]:
    flows = {k: quarterize_flow(extract(facts, k, as_of)) for k in FLOW}
    pers = {k: quarterize_flow(extract(facts, k, as_of), additive=False) for k in PER_SHARE}
    insts = {k: instants(extract(facts, k, as_of)) for k in INSTANT}
    anchor = flows["revenue"] or flows["net_income"] or flows["cfo"]
    ends = sorted(set(anchor) | set(flows["net_income"]))
    rows: list[QuarterRow] = []
    for e in ends:
        if min_end and e < min_end:
            continue
        base = anchor.get(e) or flows["net_income"].get(e)
        row = QuarterRow(e, base.start if base else None, fiscal_label(e, fye), calendar_label(base.start if base else None, e), None)
        filed: list[date] = []
        for group in (flows, pers):
            for k, series in group.items():
                pv = series.get(e)
                if pv is None:
                    continue
                row.values[k], row.first_values[k] = pv.latest, pv.first
                filed.append(pv.first_filed)
                if pv.derived:
                    row.derived.append(k)
                if pv.restated:
                    row.restated.append(k)
        for k, series in insts.items():
            # cover-page share counts are dated after the period end
            hit = _near(e, series, 4) if k != "shares_out" else _after(e, series, 75)
            if hit is not None:
                pv = series[hit]
                row.values[k], row.first_values[k] = pv.latest, pv.first
        row.first_filed = max(filed) if filed else None
        _derive(row)
        rows.append(row)
    return rows


def _after(e: date, pool: dict[date, PeriodVal], within: int) -> date | None:
    cands = [d for d in pool if e <= d <= e + timedelta(days=within)]
    return min(cands) if cands else _near(e, pool, 4)


def _derive(r: QuarterRow) -> None:
    v = r.values
    if "gross_profit" not in v and "revenue" in v and "cost_of_revenue" in v:
        v["gross_profit"] = v["revenue"] - v["cost_of_revenue"]
        r.derived.append("gross_profit")
    if "cfo" in v and "capex" in v:
        v["fcf"] = v["cfo"] - v["capex"]
    if "debt_total" not in v:
        parts = [v[k] for k in ("debt_noncurrent", "debt_current", "short_borrowings") if k in v]
        if parts:
            v["debt_total"] = sum(parts)
    elif "short_borrowings" in v:
        v["debt_total"] += v["short_borrowings"]
    if "debt_total" in v:
        v["net_debt"] = v["debt_total"] - v.get("cash", 0.0) - v.get("short_investments", 0.0)
    rev = v.get("revenue")
    if rev:
        for k, out in (("gross_profit", "gross_margin"), ("operating_income", "op_margin"), ("net_income", "net_margin"),
                       ("fcf", "fcf_margin"), ("sbc", "sbc_pct"), ("rnd", "rnd_pct")):
            if k in v:
                v[out] = v[k] / rev * 100


def consecutive(rows: list[QuarterRow]) -> bool:
    return all(_between((b.end - a.end).days, (80, 100)) for a, b in zip(rows, rows[1:]))


def ttm(rows: list[QuarterRow], key: str, upto: int | None = None) -> float | None:
    """Sum of the last four consecutive quarters (or of the four ending at index `upto`)."""
    end = len(rows) if upto is None else upto + 1
    win = rows[max(0, end - 4):end]
    if len(win) < 4 or not consecutive(win) or any(key not in r.values for r in win):
        return None
    return sum(r.values[key] for r in win)


def yoy(rows: list[QuarterRow], key: str, i: int = -1) -> float | None:
    """Growth vs the same fiscal quarter a year earlier, in percent."""
    if not rows:
        return None
    cur = rows[i]
    prev = next((r for r in rows if abs((cur.end - r.end).days - 364) <= 20), None)
    if prev is None or key not in cur.values or key not in prev.values or prev.values[key] == 0:
        return None
    p, c = prev.values[key], cur.values[key]
    return (c - p) / abs(p) * 100


def store_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten curated observations for the facts table (point-in-time history)."""
    out = []
    for key in ALL_CONCEPTS:
        for o in extract(facts, key):
            unit = "USD/shares" if key == "eps_diluted" else ("shares" if key in ("diluted_shares", "shares_out") else "USD")
            out.append(dict(concept=o.concept, unit=unit, start=o.start, end=o.end, val=o.val, accn=o.accn,
                            form=o.form, filed=o.filed))
    return out
