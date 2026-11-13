"""The business in numbers an owner cares about: where the money comes from and goes, over a decade, and who owns it."""
from __future__ import annotations

from statistics import mean
from typing import Any, Optional


def _r(x: Optional[float], n: int = 1) -> Optional[float]:
    return None if x is None else round(x, n)


def _cagr(a: Optional[float], b: Optional[float], years: int) -> Optional[float]:
    if not a or not b or a <= 0 or b <= 0 or years <= 0:
        return None
    return ((b / a) ** (1 / years) - 1) * 100


def long_term(annual: list[dict]) -> dict[str, Any]:
    """A decade at a glance: steady growth, cash every year, a high return on the money invested, few new shares."""
    ys = [a["values"] for a in annual]
    if not ys:
        return {}
    n = len(ys)
    rev = [v.get("revenue") for v in ys]
    fcf = [v.get("fcf") for v in ys]
    out: dict[str, Any] = {"years": n, "first": annual[0]["fy"], "last": annual[-1]["fy"]}
    for span in (10, 5, 3):
        if n > span:
            out[f"rev_cagr_{span}"] = _r(_cagr(rev[-span - 1], rev[-1], span))
            out[f"fcf_cagr_{span}"] = _r(_cagr(fcf[-span - 1], fcf[-1], span))
    if n > 1 and "rev_cagr_3" not in out:
        out["rev_cagr_all"] = _r(_cagr(rev[0], rev[-1], n - 1))
    pairs = [(a, b) for a, b in zip(rev, rev[1:]) if a and b]
    out["rev_up_years"], out["rev_years"] = sum(b > a for a, b in pairs), len(pairs)
    known = [x for x in fcf if x is not None]
    out["fcf_pos_years"], out["fcf_years"] = sum(x > 0 for x in known), len(known)
    roic = [v.get("roic") for v in ys]
    rk = [x for x in roic[-5:] if x is not None]
    out["roic_avg5"] = _r(mean(rk)) if rk else None
    out["roic_min"] = _r(min(x for x in roic if x is not None)) if any(x is not None for x in roic) else None
    om = [v.get("op_margin") for v in ys if v.get("op_margin") is not None]
    if om:
        out["op_margin_lo"], out["op_margin_hi"], out["op_margin_last"] = _r(min(om)), _r(max(om)), _r(om[-1])
    sh = [v.get("diluted_shares") or v.get("shares_out") for v in ys]
    sh_pairs = [(i, x) for i, x in enumerate(sh) if x]
    if len(sh_pairs) >= 2:
        (i0, a), (i1, b) = sh_pairs[0], sh_pairs[-1]
        out["shares_change_pct"], out["shares_span"] = _r((b / a - 1) * 100), i1 - i0
    last5 = ys[-5:]
    f5 = sum(v.get("fcf") or 0 for v in last5)
    back = sum((v.get("dividends_paid") or 0) + (v.get("buybacks") or 0) for v in last5)
    out["returned_pct_fcf5"] = _r(back / f5 * 100, 0) if f5 > 0 else None
    out["acq5"] = sum(v.get("acquisitions") or 0 for v in last5) or None
    return out


def money_flow(quarters: list[dict], market_cap: Optional[float] = None) -> dict[str, Any]:
    """The last twelve months as a household budget: what came in, what each kind of cost took, what was left,
    and where the cash went. `quarters` newest first, each {'values': {...}}."""
    win = quarters[:4]
    if len(win) < 4:
        return {}

    def ttm(k: str) -> Optional[float]:
        vals = [q["values"].get(k) for q in win]
        return sum(vals) if all(v is not None for v in vals) else None

    rev = ttm("revenue")
    if not rev or rev <= 0:
        return {}
    gp = ttm("gross_profit")
    cogs = ttm("cost_of_revenue") or (rev - gp if gp is not None else None)
    rnd, sga, oi, ni = ttm("rnd"), ttm("sga"), ttm("operating_income"), ttm("net_income")
    tax, interest = ttm("income_tax"), ttm("interest_expense")
    other_opex = None
    if oi is not None and cogs is not None:
        other_opex = max(0.0, rev - cogs - (rnd or 0) - (sga or 0) - oi)
    below = None
    if oi is not None and ni is not None:  # interest, tax and everything else between operating profit and net profit
        below = oi - ni
    cfo, capex = ttm("cfo"), ttm("capex")
    fcf = cfo - capex if cfo is not None and capex is not None else ttm("fcf")
    div, bb, acq = ttm("dividends_paid"), ttm("buybacks"), ttm("acquisitions")
    rep, iss = ttm("debt_repaid"), ttm("debt_issued")
    debt_net = (rep or 0) - (iss or 0) if rep is not None or iss is not None else None
    out = {
        "revenue": rev, "cogs": cogs, "rnd": rnd, "sga": sga, "other_opex": other_opex, "operating_income": oi,
        "interest": interest, "tax": tax, "below_oi": below, "net_income": ni, "cfo": cfo, "capex": capex, "fcf": fcf,
        "sbc": ttm("sbc"), "dividends": div, "buybacks": bb, "acquisitions": acq, "debt_paid_net": debt_net,
        "kept": fcf - (div or 0) - (bb or 0) - (acq or 0) - (debt_net or 0) if fcf is not None else None,
        "per_month_net": ni / 12 if ni is not None else None, "per_month_fcf": fcf / 12 if fcf is not None else None,
        "per_100_net": ni / rev * 100 if ni is not None else None, "per_100_fcf": fcf / rev * 100 if fcf is not None else None,
        "period": f"{win[-1].get('fiscal', '')}–{win[0].get('fiscal', '')}",
    }
    if market_cap and fcf is not None:
        out["fcf_yield"] = fcf / market_cap * 100
        out["holder_yield"] = ((div or 0) + (bb or 0)) / market_cap * 100
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items()}


def ownership(holders: list[dict], shares_out: Optional[float]) -> dict[str, Any]:
    """Insiders, holders of 5%+ and the public, in percent of the shares. Overlaps (an insider who also filed a 13D) count once."""
    ins = [h for h in holders if h["kind"] == "insider"]
    maj = [h for h in holders if h["kind"] == "major"]
    ins_pct = sum(h.get("pct") or 0 for h in ins)
    names = {h["name"].lower() for h in ins}
    maj_pct = sum(h.get("pct") or 0 for h in maj if h["name"].lower() not in names)
    ins_pct, maj_pct = min(ins_pct, 100.0), min(maj_pct, max(0.0, 100.0 - ins_pct))
    top = max([*ins, *maj], key=lambda h: h.get("pct") or 0, default=None)
    return {"insiders_pct": round(ins_pct, 2), "majors_pct": round(maj_pct, 2),
            "public_pct": round(max(0.0, 100 - ins_pct - maj_pct), 2), "shares_out": shares_out,
            "top": {"name": top["name"], "pct": top.get("pct")} if top and (top.get("pct") or 0) > 0 else None,
            "controlled": bool(top and (top.get("pct") or 0) >= 25)}
