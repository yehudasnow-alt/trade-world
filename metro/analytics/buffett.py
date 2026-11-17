"""Value the way Warren Buffett describes it, from the company's own reported numbers.

1. Owner earnings: the cash the owners could take out each year without hurting the business. Buffett (1986 letter):
   profit plus non-cash charges minus the capital spending needed to keep the business. From the cash-flow statement:
   operating cash flow - capital spending - stock paid to employees (a real cost even though no cash leaves).
   Normalised: half the last twelve months, half the typical margin of recent years on today's sales, so one
   unusually good or bad year does not decide the value.
2. Quality: return on invested capital over five years, and how steady it was. High and steady means a moat.
   When Claude has read the annual report, its moat assessment is used instead.
3. Growth: what the business can sustain (return on capital x the share of profit it reinvests), what it actually
   did (sales over five years, owner earnings over five years, the last year), and what management guides to.
   The middle of these, capped at 20% a year. It lasts 10 years for a wide moat, 6 for a narrow one, 3 without one,
   fading to the long-run rate. Buybacks add to growth per share.
4. Intrinsic value per share: those owner earnings, discounted at the return you require (9%; +1 for heavy debt).
5. Margin of safety: buy only well below the value (25%).
6. Expected price: the business value grows with owner earnings; the market price closes the gap to the value over
   about five years ("in the short run a voting machine, in the long run a weighing machine"). Dividends come on top.
This is an estimate from the reported numbers, not a promise; the confidence and its reasons say how much to trust it.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any, Optional

HORIZONS = (0.25, 0.5, 1.0, 2.0, 5.0)
REQUIRED, TERMINAL, MOS = 9.0, 3.0, 25.0
GROWTH_CAP, GROWTH_FLOOR = 20.0, -5.0
YEARS = {"wide": 10, "narrow": 6, "none": 3}
CONVERGE_YEARS = 5.0
FIN_TEMPLATES = ("bank", "insurer", "insurance", "reit")
FIT_BAND = (0.4, 2.5)  # value / price outside this: the method probably does not fit the company


def value_per_share(oe_ps: float, g: float, years: int, t: float, r: float) -> Optional[float]:
    """Owner earnings per share growing at g, fading in a straight line to t over `years`, then t forever, discounted at r."""
    if oe_ps is None or oe_ps <= 0 or r <= t or years < 1:
        return None
    v, e = 0.0, oe_ps
    for y in range(1, years + 1):
        gy = g + (t - g) * (y - 1) / years
        e *= 1 + gy / 100
        v += e / (1 + r / 100) ** y
    return v + e * (1 + t / 100) / ((r - t) / 100) / (1 + r / 100) ** years


def oe_after(g: float, years: int, t: float, h: float) -> float:
    """How much owner earnings per share multiply over h years on the same fading path."""
    out, k = 1.0, 0.0
    while k < h - 1e-9:
        step = min(1.0, h - k)
        y = int(k) + 1
        gy = (g + (t - g) * (y - 1) / years) if y <= years else t
        out *= (1 + gy / 100) ** step
        k += step
    return out


def _cagr(a, b, n):
    return ((b / a) ** (1 / n) - 1) * 100 if a and b and a > 0 and b > 0 and n > 0 else None


def analyze(m: dict, annual: list[dict], guidance: Optional[str] = None, moat_ai: Optional[str] = None,
            assume: Optional[dict] = None) -> Optional[dict]:
    a = assume or {}
    price, shares = m.get("price"), m.get("shares")
    if not price or not shares:
        return None
    ys = [r for r in annual if r.get("revenue")][-11:]
    why: list[str] = []

    # 1. owner earnings. For banks and insurers cash flow mixes the business with its customers' money, so Buffett
    #    looks at net profit instead (and at return on equity for quality); for property trusts, at net profit as well.
    fin = m.get("template") in FIN_TEMPLATES
    fcf_t, sbc_t, rev_t = m.get("fcf_ttm"), m.get("sbc_ttm") or 0.0, m.get("rev_ttm")
    if fin:
        oe_ttm = m.get("ni_ttm")
        margins = [r["net_income"] / r["revenue"] for r in ys[-5:] if r.get("net_income") is not None]
    else:
        oe_ttm = fcf_t - sbc_t if fcf_t is not None else None
        margins = [(r["fcf"] - (r.get("sbc") or 0)) / r["revenue"] for r in ys[-5:] if r.get("fcf") is not None]
    oe_typ = median(margins) * rev_t if margins and rev_t else None
    parts = [x for x in (oe_ttm, oe_typ) if x is not None]
    if a.get("oe"):
        oe = a["oe"]
    elif parts:
        oe = sum(parts) / len(parts)
    else:
        return None
    if oe <= 0:
        return {"ok": False, "reason": "החברה לא מייצרת רווח לבעלים (תזרים פחות תגמול במניות) – בשיטה של באפט אין מה להעריך עד שתייצר."}
    oe_ps = oe / shares
    oe_pos_years = sum(1 for x in margins if x > 0)

    # 2. quality and moat
    roic = [r["roic"] for r in ys[-5:] if r.get("roic") is not None] if not fin else ([m["roe"]] if m.get("roe") is not None else [])
    roic5 = sum(roic) / len(roic) if roic else None
    roic_min = min(roic) if roic else None
    om = [r.get("op_margin") for r in ys[-10:] if r.get("op_margin") is not None]
    steady = bool(om) and max(om) - min(om) <= 12 and min(om) > 0
    if moat_ai in YEARS:
        moat, moat_src = moat_ai, "ai"
    elif roic5 is not None and roic5 >= 18 and (roic_min or 0) >= 12 and steady:
        moat, moat_src = "wide", "numbers"
    elif roic5 is not None and roic5 >= 10:
        moat, moat_src = "narrow", "numbers"
    else:
        moat, moat_src = "none", "numbers"
    years = int(a.get("years") or YEARS[moat])

    # 3. growth
    base_of = (lambda r: r.get("net_income") or 0) if fin else (lambda r: (r.get("fcf") or 0) - (r.get("sbc") or 0))  # noqa: E731
    paid = [((r.get("dividends_paid") or 0) + (r.get("buybacks") or 0)) / base_of(r) for r in ys[-5:] if base_of(r) > 0]
    retain = max(0.0, min(1.0, 1 - sum(paid) / len(paid))) if paid else None
    sustain = roic5 * retain if roic5 is not None and retain is not None else None
    rev5 = _cagr(ys[-6]["revenue"], ys[-1]["revenue"], 5) if len(ys) >= 6 else None
    oe5 = None
    key = "net_income" if fin else "fcf"
    if len(ys) >= 6 and ys[-6].get(key) is not None and ys[-1].get(key) is not None:
        sb = (lambda r: 0.0) if fin else (lambda r: r.get("sbc") or 0)  # noqa: E731
        oe5 = _cagr(ys[-6][key] - sb(ys[-6]), ys[-1][key] - sb(ys[-1]), 5)
    recent = m.get("rev_yoy")
    srcs = {"sustain": sustain, "rev5": rev5, "oe5": oe5, "recent": recent}
    vals = [max(-20.0, min(60.0, v)) for v in srcs.values() if v is not None]
    if not vals:
        return None
    g_auto = median(vals) + {"raised": 1.5, "lowered": -2.5, "withdrawn": -2.5}.get(guidance or "", 0.0)
    cap = GROWTH_CAP if moat != "none" else 10.0
    if g_auto > cap:
        why.append(f"צמיחה של {g_auto:.0f}% בשנה נספרה כ-{cap:.0f}% (באפט שמרן בצמיחה)")
    g_biz = a.get("g", max(GROWTH_FLOOR, min(cap, g_auto)))
    sh = [r.get("diluted_shares") for r in ys[-6:] if r.get("diluted_shares")]
    sh_cagr = _cagr(sh[0], sh[-1], len(sh) - 1) if len(sh) >= 3 else (m.get("shares_yoy"))
    shrink = max(-4.0, min(4.0, -(sh_cagr or 0.0)))  # + buys back, - dilutes
    g = g_biz + shrink if "g" not in a else g_biz

    # 4. intrinsic value
    lev = m.get("net_debt_to_oi")
    r = a.get("r") or (REQUIRED + (1.0 if lev is not None and lev > 3 else 0.0))
    t = a.get("t", TERMINAL)
    if lev is not None and lev > 3 and "r" not in a:
        why.append("חוב גבוה: התשואה הנדרשת הועלתה ל-10%")
    value = value_per_share(oe_ps, g, years, t, r)
    if value is None:
        return None
    bear = value_per_share(oe_ps * 0.9, g - 4, max(2, years - 2), t, r + 1)
    bull = value_per_share(oe_ps * 1.1, g + 4, years + 2, t, max(t + 1, r - 1))
    mos = a.get("mos", MOS)
    buy = value * (1 - mos / 100)

    # 5. expected price path: value grows with owner earnings, the price closes the gap over five years
    gap = value / price
    fits = FIT_BAND[0] <= gap <= FIT_BAND[1]
    if not fits:
        why.append("השווי רחוק מאוד מהמחיר: כנראה השיטה לא מתאימה לחברה הזאת (רווח חריג, מחזוריות, או עסק שקשה להעריך מהדוחות)")
        gap = min(1.7, max(0.6, gap))  # do not project a collapse or a tripling from a model that does not fit
    div_y = (m.get("dividends_ttm") or 0) / (price * shares) * 100

    def price_at(h: float) -> float:
        return price * oe_after(g, years, t, h) * gap ** min(1.0, h / CONVERGE_YEARS)

    path = {str(h): price_at(h) for h in HORIZONS}
    cap_path = {str(h): price_at(h) * shares * (1 - shrink / 100) ** h for h in HORIZONS}
    exp5 = ((path["5.0"] / price) ** (1 / 5) - 1) * 100 + div_y

    # 6. confidence
    if len(ys) < 5:
        why.append("פחות מחמש שנים של דוחות שנתיים")
    if margins and oe_pos_years < len(margins):
        why.append(f"רווח לבעלים חיובי רק ב-{oe_pos_years} מתוך {len(margins)} השנים האחרונות")
    if not steady and om:
        why.append("רווחיות תנודתית: עסק מחזורי, קשה לחזות")
    spread = (max(vals) - min(vals)) if len(vals) >= 2 else 0
    if spread > 25:
        why.append("מקורות הצמיחה (היסטוריה, השנה האחרונה, יכולת השקעה מחדש) רחוקים זה מזה")
    conf = ("low" if not fits else "high" if not why or (len(why) == 1 and why[0].startswith("צמיחה של"))
            else "medium" if len(why) <= 2 else "low")

    checks = [
        ("רווח לבעלים חיובי בכל השנים האחרונות", bool(margins) and oe_pos_years == len(margins)),
        ("תשואה על ההון מעל 15% בממוצע", roic5 is not None and roic5 >= 15),
        ("רווחיות יציבה לאורך שנים", steady),
        ("חוב נמוך (חוב נטו פחות מפי 2 מהרווח התפעולי)", lev is not None and lev < 2),
        ("לא מדללת את בעלי המניות", shrink >= 0),
        (f"המחיר לפחות {mos:.0f}% מתחת לשווי (מרווח ביטחון)", price <= buy),
    ]
    return {"ok": True, "price": price, "oe": oe, "oe_ttm": oe_ttm, "oe_typ": oe_typ, "oe_ps": oe_ps, "oe_yield": oe / (price * shares) * 100,
            "roic5": roic5, "roic_min": roic_min, "moat": moat, "moat_src": moat_src, "years": years, "retain": retain,
            "g_src": srcs, "g_auto": g_auto, "g_biz": g_biz, "shrink": shrink, "g": g, "r": r, "t": t, "mos": mos,
            "now": value, "bear": min(bear or value, value), "bull": max(bull or value, value), "buy": buy, "gap_pct": (value / price - 1) * 100,
            "div_yield": div_y, "exp5": exp5, "path": path, "cap_path": cap_path, "checks": checks, "conf": conf, "why": why,
            "mine": bool(a), "assume": a, "guidance": guidance, "fits": fits, "fin": fin}
