"""Demo data: the real S&P 500 list with synthetic prices, fundamentals, earnings and a sample portfolio.

Everything except the company list, sectors and CIKs is invented, and the API marks the
mode as "demo" so the interface labels it. The data then flows through the same scoring,
thesis and alert code as live data, so the demo also exercises the real pipeline.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import delete, select

from . import alerts, clock, pipeline
from .ai.schema import COVERAGE
from .db import (AiJob, Alert, Annual, Company, CompanyProfile, Earnings, Event, Fact, Filing, Holder, Holding, JournalEntry,
                 PriceBar, Promise, Quarter, Quote, ReportCard, Score, Thesis, Watch, kv_set, session, utcnow)
from .universe import MARKET_ETF, SECTORS, sync_universe

CAPS = {  # rough size tiers (USD billions) so the skyline looks like the market; not live values
    "NVDA": 4300, "MSFT": 3800, "AAPL": 3700, "GOOGL": 3000, "AMZN": 2400, "META": 1800, "AVGO": 1600, "TSLA": 1400,
    "BRK.B": 1050, "JPM": 820, "LLY": 850, "ORCL": 800, "WMT": 800, "V": 680, "MA": 530, "NFLX": 500, "XOM": 470,
    "COST": 420, "JNJ": 430, "PLTR": 420, "HD": 400, "ABBV": 390, "BAC": 380, "AMD": 380, "PG": 360, "UNH": 300,
    "KO": 300, "CVX": 300, "GE": 300, "CSCO": 270, "WFC": 270, "IBM": 260, "PM": 260, "TMUS": 260, "CRM": 240,
    "MS": 240, "GS": 240, "MRK": 220, "LIN": 220, "MCD": 220, "RTX": 220, "CAT": 220, "ABT": 230, "AXP": 230,
    "MU": 200, "DIS": 200, "PEP": 200, "T": 200, "TMO": 200, "ISRG": 190, "NOW": 190, "UBER": 190, "INTU": 180,
    "VZ": 180, "QCOM": 180, "C": 180, "TXN": 170, "BKNG": 170, "SCHW": 170, "BLK": 170, "AMAT": 170, "ANET": 170,
    "GEV": 170, "AMGN": 160, "SPGI": 160, "BA": 160, "ACN": 160, "LRCX": 150, "ADBE": 150, "NEE": 150, "PGR": 150,
    "DHR": 150, "BSX": 150, "INTC": 150, "GILD": 140, "SYK": 140, "LOW": 140, "UNP": 140, "ETN": 140, "HON": 130,
    "PANW": 130, "DE": 130, "KLAC": 120, "CRWD": 120, "COP": 120, "VRTX": 120, "ADP": 120, "CMCSA": 120, "MDT": 120,
    "PFE": 140, "TJX": 155, "BX": 180, "KKR": 120, "COF": 140, "CB": 115, "LMT": 110, "HCA": 100, "SBUX": 100, "NKE": 100,
}
# sector: (price/sales, gross margin, op margin, net margin, fcf margin, growth %, beta, vol)
PROFILE = {
    "Information Technology": (8.0, 62, 28, 22, 24, 14, 1.25, 0.020),
    "Communication Services": (5.0, 55, 24, 18, 18, 9, 1.1, 0.018),
    "Consumer Discretionary": (2.2, 38, 11, 8, 8, 7, 1.2, 0.019),
    "Financials": (3.2, 60, 32, 24, 20, 6, 1.05, 0.015),
    "Health Care": (3.6, 58, 18, 13, 14, 6, 0.8, 0.016),
    "Industrials": (2.6, 32, 15, 11, 11, 6, 1.0, 0.016),
    "Consumer Staples": (1.6, 36, 13, 9, 9, 3, 0.6, 0.011),
    "Energy": (1.4, 30, 14, 10, 12, 1, 0.95, 0.018),
    "Utilities": (2.6, 40, 22, 13, 4, 4, 0.55, 0.012),
    "Real Estate": (8.0, 65, 35, 25, 30, 4, 0.85, 0.015),
    "Materials": (2.0, 30, 15, 10, 10, 3, 1.0, 0.017),
}
SECTOR_TODAY = {"Information Technology": 0.58, "Energy": -0.96, "Financials": -0.3, "Health Care": 0.25,
                "Utilities": 0.15, "Communication Services": 0.1, "Consumer Discretionary": -0.35}
HOLD = {"MSFT": (16, 0.82), "NVDA": (14, 0.71), "GOOGL": (12, 0.80), "JPM": (10, 0.91), "LLY": (10, 1.06),
        "COST": (9, 0.96), "PANW": (8, 1.03), "XOM": (7, 1.13)}
WATCH = {"CRWD": None, "AMD": None, "ADBE": None, "UNH": None, "NKE": None}
PAYOUT = {"Utilities": 0.7, "Consumer Staples": 0.55, "Energy": 0.45, "Real Estate": 0.8, "Financials": 0.35,
          "Health Care": 0.35, "Information Technology": 0.12, "Communication Services": 0.1, "Consumer Discretionary": 0.15}
ROIC = {"Information Technology": 26, "Communication Services": 18, "Consumer Discretionary": 14, "Financials": 12,
        "Health Care": 15, "Industrials": 13, "Consumer Staples": 16, "Energy": 10, "Utilities": 6, "Real Estate": 6, "Materials": 10}
DIPS = ["ADBE", "UNH", "NKE", "CRM", "TGT", "ACN", "INTU", "MRK", "EL", "SBUX", "DE", "EW"]  # demo dips for the hunt view


def _trading_days(n: int) -> list[date]:
    end = clock.last_session()
    out = [end]
    while len(out) < n:
        out.append(clock.prev_trading_day(out[-1]))
    return out[::-1]


def generate(seed: int = 20261103, years: float = 3.0) -> dict:
    rng = np.random.default_rng(seed)
    sync_universe()
    with session() as s:
        for model in (PriceBar, Quote, Quarter, Fact, Earnings, Event, Filing, ReportCard, AiJob, Promise, Holding, Thesis,
                      JournalEntry, Watch, Score, Alert, Annual, Holder, CompanyProfile):
            s.execute(delete(model))
        comps = list(s.scalars(select(Company).where(Company.in_universe.is_(True))))
        kv_set(s, "mode", "demo")
    days = _trading_days(int(252 * years))
    n = len(days)
    mkt = rng.normal(0.00045, 0.0095, n)
    mkt[-1] = -0.0012
    sector_ret = {}
    for name, prof in PROFILE.items():
        r = mkt * (0.6 + prof[6] * 0.4) + rng.normal(0, 0.006, n)
        r[-1] = mkt[-1] + SECTOR_TODAY.get(name, 0.0) / 100
        sector_ret[name] = r
    series: dict[str, np.ndarray] = {MARKET_ETF: 480 * np.cumprod(1 + mkt)}
    for name, (he, etf) in SECTORS.items():
        series[etf] = 80 * np.cumprod(1 + sector_ret[name])

    caps: dict[str, float] = {}
    rets: dict[str, np.ndarray] = {}
    for c in comps:
        prof = PROFILE.get(c.sector, PROFILE["Industrials"])
        cap = CAPS.get(c.ticker) or float(np.clip(math.exp(math.log(42) + rng.normal(0, 0.7)), 7, 170))
        caps[c.ticker] = cap
        beta = float(np.clip(prof[6] + rng.normal(0, 0.22), 0.35, 2.1))
        vol = prof[7] * float(np.clip(rng.normal(1, 0.25), 0.6, 1.8)) * (0.85 if cap > 800 else 1)
        r = beta * mkt + 0.8 * (sector_ret[c.sector] - mkt) + rng.normal(0, vol, n)
        if c.ticker in DIPS:
            r[-45:] -= 0.0045 + rng.uniform(0, 0.002)  # a recent slide in otherwise solid names
            r[-6:] += 0.004
        if c.ticker == "PANW":
            r[-1] = mkt[-1] * beta + 0.0058 * 0.8 - 2.65 * vol
        if c.ticker == "XOM":
            r[-30:] -= 0.004
        rets[c.ticker] = r
    # price levels so that today's close sits at a plausible level
    levels = {c.ticker: float(np.clip(math.exp(math.log(140) + rng.normal(0, 0.75)), 18, 1300)) for c in comps}
    rows_px = []
    with session() as s:
        for sym, path in series.items():
            for i, d in enumerate(days):
                c = float(path[i])
                rows_px.append(dict(ticker=sym, day=d, o=c, h=c * 1.006, l=c * 0.994, c=c, v=5e7))
        for c in comps:
            r = rets[c.ticker]
            path = np.cumprod(1 + r)
            path = path / path[-1] * levels[c.ticker]
            base_v = 2e5 * caps[c.ticker] ** 0.6
            vols = base_v * np.exp(rng.normal(0, 0.28, n))
            if c.ticker == "PANW":
                vols[-1] *= 3.2
            for i, d in enumerate(days):
                cl = float(path[i])
                op = cl / (1 + r[i]) * (1 + rng.normal(0, 0.003))
                hi = max(cl, op) * (1 + abs(rng.normal(0, 0.006)))
                lo = min(cl, op) * (1 - abs(rng.normal(0, 0.006)))
                rows_px.append(dict(ticker=c.ticker, day=d, o=op, h=hi, l=lo, c=cl, v=float(vols[i])))
        s.execute(PriceBar.__table__.insert(), rows_px)
        # today's quote = last bar (prices "as of" the last session); prev close = bar before
        close_ts = datetime.combine(days[-1], clock.CLOSE).replace(tzinfo=clock.NY).astimezone(timezone.utc).replace(tzinfo=None)
        qrows = []
        by_sym: dict[str, list[dict]] = {}
        for r_ in rows_px:
            by_sym.setdefault(r_["ticker"], []).append(r_)
        for sym, rs in by_sym.items():
            qrows.append(dict(ticker=sym, price=rs[-1]["c"], prev_close=rs[-2]["c"], volume=rs[-1]["v"], ts=close_ts, source="demo"))
        s.execute(Quote.__table__.insert(), qrows)

    _fundamentals(comps, caps, levels, rng)
    _annual_and_owners(comps, caps, rng)
    _earnings_and_events(comps, rng, days)
    _portfolio(levels)
    pipeline.compute_scores()
    _theses()
    _cards()
    _profiles()
    pipeline.compute_scores()
    alerts.evaluate_rules()
    with session() as s:
        beacons = list(s.scalars(select(Score.ticker).where(Score.beacon.is_(True))))
    return {"companies": len(comps), "days": n, "beacons": beacons}


def _fundamentals(comps, caps, levels, rng) -> None:
    rows = []
    today = clock.now_ny().date()
    # quarter ends: last 12 calendar quarters fully reported
    q_end = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    if (today - q_end).days < 40:
        q_end = date(q_end.year, ((q_end.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    ends = [q_end]
    for _ in range(11):
        e = ends[-1]
        ends.append(date(e.year, ((e.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1))
    ends = ends[::-1]
    for c in comps:
        ps, gm, om, nm, fm, g, *_ = PROFILE.get(c.sector, PROFILE["Industrials"])
        cap = caps[c.ticker] * 1e9
        shares = cap / levels[c.ticker]
        rev_ttm = cap / (ps * float(np.exp(rng.normal(0, 0.35))))
        growth = (g + rng.normal(0, 6)) / 100
        accel = rng.normal(0, 0.03)
        gm_c, om_c = gm + rng.normal(0, 7), om + rng.normal(0, 5)
        nm_c, fm_c = max(1.0, nm + rng.normal(0, 4)), fm + rng.normal(0, 5)
        if c.ticker in DIPS:  # demo story: quality names that slid, so the hunt view has real candidates
            gm_c, om_c, nm_c, fm_c, growth = gm + 9, om + 7, nm + 5, fm + 6, max(growth, 0.09)
            rev_ttm *= 1.6
        sbc = max(0.3, rng.normal(3.5 if c.sector == "Information Technology" else 1.2, 1.0))
        net_debt = rev_ttm * rng.normal(0.25, 0.4)
        dil = rng.normal(-0.006, 0.006)
        q0 = rev_ttm / 4 / (1 + growth) ** 2.5
        for i, e in enumerate(ends):
            gr = growth + accel * (i / len(ends))
            rev = q0 * (1 + gr) ** (i / 4) * (1 + rng.normal(0, 0.015))
            sh = shares * (1 + dil) ** ((i - len(ends) + 1) / 4)
            ni = rev * (nm_c + rng.normal(0, 1.2)) / 100
            vals = {"revenue": rev, "gross_profit": rev * gm_c / 100, "operating_income": rev * om_c / 100, "net_income": ni,
                    "eps_diluted": ni / sh, "diluted_shares": sh, "shares_out": sh, "cfo": rev * (fm_c + 4) / 100,
                    "capex": rev * 0.04, "sbc": rev * sbc / 100, "cash": rev_ttm * 0.2, "debt_total": max(0.0, net_debt + rev_ttm * 0.2),
                    "equity": cap * 0.3, "rpo": rev * 3.2 if c.sector == "Information Technology" else None}
            rnd_p = {"Information Technology": 13, "Health Care": 15, "Communication Services": 9}.get(c.sector, 2)
            sga_p = max(3.0, gm_c - om_c - rnd_p)
            vals.update({"cost_of_revenue": rev * (100 - gm_c) / 100, "rnd": rev * rnd_p / 100, "sga": rev * sga_p / 100,
                         "interest_expense": max(0.0, net_debt) * 0.045 / 4, "income_tax": max(0.0, ni) * 0.22,
                         "dividends_paid": rev * (fm_c + 4) / 100 * PAYOUT.get(c.sector, 0.25),
                         "buybacks": rev * (fm_c + 4) / 100 * (0.35 if dil < 0 else 0.1),
                         "acquisitions": rev * 0.03 if rng.random() < 0.15 else 0.0})
            vals = {k: float(v) for k, v in vals.items() if v is not None}
            vals["fcf"] = vals["cfo"] - vals["capex"]
            vals["net_debt"] = vals["debt_total"] - vals["cash"]
            for k, out in (("gross_profit", "gross_margin"), ("operating_income", "op_margin"), ("net_income", "net_margin"),
                           ("fcf", "fcf_margin"), ("sbc", "sbc_pct")):
                vals[out] = vals[k] / rev * 100
            fq = (e.month - 1) // 3 + 1
            rows.append(dict(ticker=c.ticker, period_end=e, period_start=e - timedelta(days=90), fiscal_label=f"FQ{fq} FY{e.year % 100:02d}",
                             calendar_label=f"CY{e.year}Q{fq}", first_filed=e + timedelta(days=int(rng.integers(25, 42))),
                             values=vals, first_values=vals, derived=[], restated=[]))
    with session() as s:
        s.execute(Quarter.__table__.insert(), rows)


def _annual_and_owners(comps, caps, rng) -> None:
    """Ten fiscal years built back from the latest quarters, and an invented shareholder register (all marked demo)."""
    today = clock.now_ny().date()
    with session() as s:
        for c in comps:
            qs = list(s.scalars(select(Quarter).where(Quarter.ticker == c.ticker).order_by(Quarter.period_end)))
            if len(qs) < 4:
                continue
            last = qs[-1].period_end
            ttm = {k: sum(q.values.get(k, 0.0) for q in qs[-4:]) for k in ("revenue", "gross_profit", "operating_income", "net_income", "cfo",
                                                                            "capex", "dividends_paid", "buybacks", "rnd", "sga", "acquisitions")}
            g = max(-0.02, (qs[-1].values["revenue"] / qs[-5].values["revenue"] - 1)) if len(qs) >= 5 else 0.05
            cyc = 0.12 if c.sector in ("Energy", "Materials") else 0.04
            roic0 = ROIC.get(c.sector, 12) * float(np.exp(rng.normal(0, 0.3)))
            sh_last = qs[-1].values.get("diluted_shares", 1e9)
            dil = rng.normal(-0.01, 0.012)
            g = float(np.clip(g, -0.02, 0.25))
            scale = [1.0]
            for _ in range(10):  # walk back one year at a time: each year grew by about g, with a cycle around it
                scale.append(scale[-1] / (1 + g * float(np.clip(rng.normal(1, 0.4), -0.6, 2.0)) + rng.normal(0, cyc / 2)))
            for i in range(10, -1, -1):
                f = scale[i]
                m = float(np.clip(rng.normal(1, 0.06 + cyc), 0.5, 1.5))
                v = {k: x * f * (m if k in ("operating_income", "net_income", "cfo") else 1) for k, x in ttm.items()}
                v["fcf"] = v["cfo"] - v["capex"]
                v["op_margin"] = v["operating_income"] / v["revenue"] * 100
                v["gross_margin"] = v["gross_profit"] / v["revenue"] * 100
                v["fcf_margin"] = v["fcf"] / v["revenue"] * 100
                v["roic"] = roic0 * m * float(np.exp(rng.normal(0, 0.08)))
                v["diluted_shares"] = sh_last * (1 + dil) ** (-i)
                end = date(last.year - i, last.month, min(last.day, 28))
                s.add(Annual(ticker=c.ticker, fy=f"FY{end.year % 100:02d}", period_end=end, values={k: float(x) for k, x in v.items()}))
            sh = sh_last
            n_maj = int(rng.integers(2, 5))
            pcts = sorted((float(rng.uniform(5, 9.5)) for _ in range(n_maj)), reverse=True)
            for j, pc in enumerate(pcts):
                s.add(Holder(ticker=c.ticker, kind="major", name=f"גוף מוסדי {j + 1} (דוגמה)", title="13G", shares=sh * pc / 100, pct=pc,
                             as_of=today - timedelta(days=int(rng.integers(20, 400))), form="SCHEDULE 13G", active=False))
            people = [("יו\"ר הדירקטוריון (דוגמה)", float(rng.uniform(0.01, 0.3))),
                      ("מנכ\"ל (דוגמה)", float(rng.uniform(0.02, 0.4))), ("סמנכ\"ל כספים (דוגמה)", float(rng.uniform(0.005, 0.05)))]
            people += [(f"דירקטור {k} (דוגמה)", float(rng.uniform(0.001, 0.03))) for k in range(1, 4)]
            for name, pc in people:
                s.add(Holder(ticker=c.ticker, kind="insider", name=name, title=name.split(" (")[0], shares=sh * pc / 100, pct=pc,
                             as_of=today - timedelta(days=int(rng.integers(5, 300))), form="4"))
            kv_set(s, f"own.{c.ticker}", {"at": utcnow().isoformat(), "demo": True})


def _profiles() -> None:
    """Example business profiles for the focus list, so the layout can be reviewed. Marked demo; no quotes."""
    with session() as s:
        for t in list(HOLD) + list(WATCH):
            c = s.get(Company, t)
            if not c:
                continue
            ex = lambda x: f"דוגמה: {x}"  # noqa: E731
            prof = {
                "demo": True,
                "one_liner_he": ex(f"{c.name} פועלת בתחום {c.sub_industry}. כאן יופיע משפט אחד פשוט על מה החברה עושה."),
                "what_it_does_he": ex("כאן יופיע תיאור של 3-5 משפטים: מה החברה מוכרת, איך היא מרוויחה, ומה מודל העסקים, מתוך הדוח השנתי (10-K)."),
                "segments": [{"name": "Segment A", "share_pct": 58.0, "what_he": ex("המגזר הגדול: מה נמכר ולמי."), "citation": None},
                             {"name": "Segment B", "share_pct": 30.0, "what_he": ex("המגזר השני."), "citation": None},
                             {"name": "Other", "share_pct": 12.0, "what_he": ex("שאר הפעילות."), "citation": None}],
                "customers_he": ex("מי משלם לחברה: צרכנים, עסקים, ממשלות."),
                "customer_concentration_he": ex("האם יש לקוח שמביא יותר מ-10% מההכנסות, כפי שדווח."),
                "top_customers": [],
                "future_customers_he": ex("השווקים והלקוחות החדשים שההנהלה מכוונת אליהם."),
                "competitors": [{"name": "Competitor 1", "note_he": ex("במה הוא מתחרה")}, {"name": "Competitor 2", "note_he": ex("במה הוא מתחרה")}],
                "moat_type": "switching_costs", "moat_strength": "narrow",
                "moat_he": ex("למה למתחרים קשה או קל לקחת את הרווחים, עם הנימוק מהדוח ומעשר שנות מספרים."),
                "moat_evidence": [{"text_he": ex("עדות מהדוח"), "citation": None}],
                "ai_impact": "mixed", "ai_impact_he": ex("האם בינה מלאכותית או מתחרה חדש יכולים להחליף את העסק, ומה הסיכוי שהוא דווקא ירוויח מזה."),
                "ai_evidence": [], "durability": "high", "durability_he": ex("האם העסק צפוי להתקיים ולהרוויח בעוד עשר שנים, ומה היה צריך להשתבש."),
                "capital_allocation_he": ex("מה ההנהלה עושה עם הכסף: השקעה בצמיחה, רכישה עצמית, דיבידנד, רכישות."),
                "key_risks": [{"text_he": ex("סיכון מרכזי מהדוח"), "citation": None}],
                "what_to_watch_he": [ex("מדד לבדוק כל רבעון"), ex("מדד שני"), ex("מדד שלישי")],
            }
            s.add(CompanyProfile(ticker=t, accession=f"demo-{t}", model="demo", profile=prof, verification={"demo": True}, cost_usd=0.0))


def _earnings_and_events(comps, rng, days) -> None:
    today = clock.now_ny().date()
    rows, evs = [], []
    for c in comps:
        reported = rng.random() < 0.46 or c.ticker in ("PANW", "MSFT", "GOOGL")
        if c.ticker in ("NVDA",):
            reported = False
        last = today - timedelta(days=int(rng.integers(1, 20))) if reported else today + timedelta(days=int(rng.integers(1, 55)))
        if c.ticker == "PANW":
            last = clock.prev_trading_day(today) if clock.is_trading_day(today) else clock.prev_trading_day(clock.prev_trading_day(today))
        if c.ticker == "NVDA":
            last = clock.add_trading_days(today, 3)
        while not clock.is_trading_day(last):
            last += timedelta(days=1)
        hour = "amc" if rng.random() < 0.55 else "bmo"
        est = float(abs(rng.normal(1.6, 0.9)) + 0.1)
        surprise = float(rng.normal(0.035, 0.05))
        for k in range(4):
            d = last - timedelta(days=91 * k)
            while not clock.is_trading_day(d):
                d -= timedelta(days=1)
            past = d <= today and (k > 0 or reported)
            sp = surprise if k == 0 else float(rng.normal(0.03, 0.05))
            rows.append(dict(ticker=c.ticker, day=d, hour=hour, fiscal="", eps_est=est, eps_act=est * (1 + sp) if past else None,
                             rev_est=None, rev_act=None, source="demo"))
            if past and d >= today - timedelta(days=30):
                evs.append(dict(ticker=c.ticker, ts=datetime.combine(d, datetime.min.time()) + timedelta(hours=21 if hour == "amc" else 11),
                                kind="8k", code="2.02", severity="info", title="תוצאות כספיות", url="", data={},
                                dedupe=f"demo:2.02:{c.ticker}:{d}"))
    for t, code, sev, title in [("CRM", "P", "good", "קנייה בשוק של בעל עניין: $1,240,000"),
                                 ("UNH", "P", "good", "קנייה בשוק של בעל עניין: $2,050,000"),
                                 ("DIS", "5.02", "watch", "שינוי בהנהלה או בדירקטוריון")]:
        evs.append(dict(ticker=t, ts=utcnow() - timedelta(days=3), kind="form4" if code == "P" else "8k", code=code, severity=sev,
                        title=title, url="", data={}, dedupe=f"demo:{code}:{t}"))
    # demo timeline: expected events and major headlines (generic titles; the app labels the whole demo)
    today = clock.last_session()
    for t, days_ahead, kind, imp, title, when in [
        ("NVDA", 19, "event", "high", "כנס מפתחים שנתי: חשיפת דור השבבים הבא", "במרץ"),
        ("AAPL", 34, "product", "high", "השקת קו מוצרים חדש", "ברבעון הבא"),
        ("LLY", 12, "regulatory", "high", "החלטת רגולטור צפויה על תרופה בפיתוח", "עד סוף החודש"),
        ("MSFT", 46, "event", "medium", "יום משקיעים", "במהלך הרבעון"),
        ("PANW", 27, "deal", "high", "השלמת רכישה של חברת אבטחה", "עד סוף הרבעון"),
        ("XOM", 58, "capacity", "medium", "הפעלת פרויקט הפקה חדש", "במחצית השנייה של השנה"),
        ("COST", 8, "other", "medium", "פרסום מכירות חודשיות", "בתחילת החודש"),
        ("TSLA", 23, "product", "high", "אירוע השקה של דגם חדש", "בחודש הבא"),
        ("AMZN", 40, "event", "medium", "כנס ענן שנתי", "ברבעון הרביעי"),
    ]:
        evs.append(dict(ticker=t, ts=datetime.combine(clock.add_trading_days(today, days_ahead), datetime.min.time()), kind="catalyst",
                        code=kind, severity="watch" if imp == "high" else "info", title=title, url="",
                        data={"when": when, "importance": imp, "quote": ""}, dedupe=f"demo:cat:{t}:{kind}"))
    for t, days_ago, title in [("NVDA", 4, "Nvidia announces partnership to build AI data centers (demo headline)"),
                               ("GOOGL", 9, "Regulator opens antitrust probe into ad business (demo headline)"),
                               ("JPM", 6, "JPMorgan raises net interest income outlook (demo headline)"),
                               ("PANW", 2, "Palo Alto Networks to acquire security startup (demo headline)")]:
        evs.append(dict(ticker=t, ts=utcnow() - timedelta(days=days_ago), kind="news", code="news", severity="watch", title=title, url="",
                        data={"source": "demo"}, dedupe=f"demo:news:{t}"))
    with session() as s:
        s.execute(Earnings.__table__.insert(), rows)
        s.execute(Event.__table__.insert(), evs)


def _portfolio(levels) -> None:
    nav = 250_000.0
    with session() as s:
        for t, (w, cost_ratio) in HOLD.items():
            px = levels[t]
            mv = nav * w / 100
            s.add(Holding(ticker=t, quantity=round(mv / px), avg_cost=px * cost_ratio, market_value=mv, weight_pct=w,
                          pnl_pct=(1 / cost_ratio - 1) * 100, currency="USD"))
        for t in WATCH:
            px = levels[t]
            lo, hi = (px * 0.97, px * 1.01) if t == "ADBE" else (px * 0.86, px * 0.92)
            s.add(Watch(ticker=t, entry_low=round(lo, 2), entry_high=round(hi, 2), note="אזור כניסה לדוגמה"))
        for t, act, note, days_ago in [("NVDA", "buy", "כניסה אחרי תיקון של 12%, ציון 74.", 140), ("PANW", "add", "הוספה אחרי דוח חזק.", 60),
                                       ("XOM", "buy", "גידור לאנרגיה.", 90), ("ADBE", "pass", "יקרה מדי מול הצמיחה, ממתין לאזור.", 12)]:
            s.add(JournalEntry(ticker=t, action=act, price=levels[t] * (0.9 if act != "pass" else 1.06), note=note,
                               ts=utcnow() - timedelta(days=days_ago), snapshot={}))
        kv_set(s, "ibkr.flex", {"positions": len(HOLD), "nav": nav})
        kv_set(s, "portfolio.last", {"positions": len(HOLD), "nav": nav, "cash": nav * (1 - sum(w for w, _ in HOLD.values()) / 100), "source": "demo"})


def _theses() -> None:
    target = {"MSFT": "ok", "NVDA": "ok", "GOOGL": "ok", "JPM": "ok", "LLY": "watch", "COST": "ok", "PANW": "watch", "XOM": "broken"}
    with session() as s:
        for t, want in target.items():
            sc = s.get(Score, t)
            m = (sc.metrics if sc else {}) or {}
            rev, gm, px = m.get("rev_yoy") or 5.0, m.get("gross_margin") or 40.0, m.get("price") or 100.0
            metrics = [{"name": "rev_yoy", "op": ">", "value": round(rev - 4 if want == "ok" else rev + 3, 0)},
                       {"name": "gross_margin", "op": ">", "value": round(gm - 3, 0)}]
            kills = [{"type": "exec_departure"}, {"type": "price_below", "value": round(px * (1.04 if want == "broken" else 0.82), 2)}]
            if t != "PANW":
                kills.insert(0, {"type": "guidance_lowered"})
            s.add(Thesis(ticker=t, text="תזת הדגמה: הסבר במשפטים ספורים למה המניה תעלה ומה השוק מפספס.",
                         metrics=metrics, kills=kills, horizon_weeks=16))


def _cards() -> None:
    """Sample report cards for the focus list, so the card layout can be reviewed. Marked as demo; no quotes."""
    with session() as s:
        focus = list(HOLD) + list(WATCH)
        for t in focus:
            qs = list(s.scalars(select(Quarter).where(Quarter.ticker == t).order_by(Quarter.period_end)))
            if len(qs) < 5:
                continue
            cur, ya = qs[-1].values, qs[-5].values
            yoy = (cur["revenue"] / ya["revenue"] - 1) * 100
            g = "lowered" if t == "PANW" else ("raised" if yoy > 10 else "maintained")
            bil = lambda v: round(v / 1e9, 2)  # noqa: E731
            metrics = [
                {"key": "revenue", "label": "Revenue", "value": bil(cur["revenue"]), "scale": "billions", "unit": "USD",
                 "period": qs[-1].fiscal_label, "yoy_pct": round(yoy, 1), "gaap": True, "note_he": "דוגמה: נתון מחושב מנתוני ההדגמה.", "citation": None, "_n": "xbrl"},
                {"key": "gross_margin", "label": "Gross margin", "value": round(cur["gross_margin"], 1), "scale": "units", "unit": "percent",
                 "period": qs[-1].fiscal_label, "yoy_pct": None, "gaap": True, "note_he": "", "citation": None, "_n": "xbrl"},
                {"key": "eps_diluted", "label": "Diluted EPS", "value": round(cur["eps_diluted"], 2), "scale": "units", "unit": "USD_per_share",
                 "period": qs[-1].fiscal_label, "yoy_pct": round((cur["eps_diluted"] / ya["eps_diluted"] - 1) * 100, 1), "gaap": True,
                 "note_he": "", "citation": None, "_n": "xbrl"},
                {"key": "fcf", "label": "Free cash flow", "value": bil(cur["fcf"]), "scale": "billions", "unit": "USD",
                 "period": qs[-1].fiscal_label, "yoy_pct": None, "gaap": False, "note_he": "", "citation": None, "_n": "xbrl"},
            ]
            cov = []
            for i, (k, (he, _)) in enumerate(COVERAGE.items()):
                rep = i % 3 != 2
                cov.append({"key": k, "status": "reported" if rep else "not_reported",
                            "severity": "watch" if (t == "PANW" and k == "guidance") else ("positive" if i % 5 == 0 else "neutral"),
                            "finding_he": (f"דוגמה: כאן יופיע הממצא על {he}, עם המספרים מהדוח." if rep else "דוגמה: הנושא לא דווח ברבעון."),
                            "citations": []})
            card = {
                "demo": True,
                "headline_he": "כרטיסיית דוגמה: כך ייראה סיכום הדוח אחרי חיבור ל-SEC ול-Claude.",
                "what_happened_he": f"ההכנסות ברבעון {qs[-1].fiscal_label} עמדו על {bil(cur['revenue'])} מיליארד דולר, שינוי של {yoy:+.1f}% מול השנה שעברה (נתוני הדגמה).",
                "metrics": metrics,
                "what_next_he": {"lowered": "דוגמה: ההנהלה הורידה מעט את טווח התחזית לרבעון הבא.",
                                 "raised": "דוגמה: ההנהלה העלתה את טווח התחזית לשנה.",
                                 "maintained": "דוגמה: ההנהלה השאירה את התחזית ללא שינוי."}[g],
                "guidance_direction": g,
                "guidance": [{"metric": "Revenue", "period": "next quarter", "low": round(bil(cur["revenue"]) * 1.01, 2),
                              "high": round(bil(cur["revenue"]) * 1.04, 2), "scale": "billions", "unit": "USD",
                              "prior_low": round(bil(cur["revenue"]) * (1.03 if g == "lowered" else 1.0), 2),
                              "prior_high": round(bil(cur["revenue"]) * (1.06 if g == "lowered" else 1.03), 2),
                              "direction": g, "note_he": "", "citation": None}],
                "where_going_he": "דוגמה: כאן יופיעו מנועי הצמיחה והסיכונים שההנהלה הדגישה, ומה השתנה בניסוח מול הרבעון הקודם.",
                "growth_drivers_he": ["דוגמה: מנוע צמיחה ראשון", "דוגמה: מנוע צמיחה שני"],
                "risks_he": ["דוגמה: סיכון שהוזכר בדוח"],
                "coverage": cov,
                "qa": [{"analyst": None, "question_he": "דוגמה: שאלה קשה של אנליסט על שולי הרווח.",
                        "answer_he": "דוגמה: תשובת ההנהלה, והאם ניתן בה מספר.", "numeric_answer": True, "evasive": False, "citation": None},
                       {"analyst": None, "question_he": "דוגמה: שאלה על קצב סגירת עסקאות.",
                        "answer_he": "דוגמה: תשובה כללית בלי מספר.", "numeric_answer": False, "evasive": True, "citation": None}],
                "promises_new": [], "promises_check": [],
                "risk_changes": [{"kind": "added", "summary_he": "דוגמה: משפט חדש בגורמי הסיכון.", "citation": None}],
                "flags": [{"severity": "watch", "text_he": "דוגמה: הורדת תחזית"}] if g == "lowered" else [],
            }
            s.add(ReportCard(ticker=t, accession=f"demo-{t}", period_end=qs[-1].period_end, stage="final", model="demo",
                             prompt_version="demo", card=card,
                             verification={"quotes": {"exact": 0, "close": 0, "missing": 0}, "numbers": {"xbrl": 4, "text": 0, "unverified": 0},
                                           "coverage_keys": 15, "problems": [], "quote_ok_pct": 0, "number_ok_pct": 100, "demo": True},
                             review={"verdict": "pass", "issues": [], "demo": True}, cost_usd=0.0))
            s.add(Promise(ticker=t, metric="Gross margin", target="דוגמה: שולי רווח גולמי של 45% עד סוף השנה",
                          deadline="סוף שנת הכספים", quote="", status="on_track" if t != "PANW" else "open",
                          evidence="דוגמה: 44.5% ברבעון האחרון", made=qs[-3].period_end))
