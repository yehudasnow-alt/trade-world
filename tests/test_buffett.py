"""The Buffett-style valuation: owner earnings, quality, sustainable growth, intrinsic value, expected path."""
from metro.analytics import buffett as BF


def _years(n=6, rev0=100.0, g=0.08, fcf_m=0.25, sbc_m=0.02, roic=25.0, payout=0.5, sh0=1000.0, sh_g=-0.02):
    out = []
    for i in range(n):
        rev = rev0 * (1 + g) ** i
        fcf = rev * fcf_m
        out.append({"revenue": rev, "fcf": fcf, "sbc": rev * sbc_m, "roic": roic, "op_margin": 30.0,
                    "dividends_paid": fcf * payout / 2, "buybacks": fcf * payout / 2, "diluted_shares": sh0 * (1 + sh_g) ** i})
    return out


def _m(ys, price=1.2):
    last = ys[-1]
    return {"price": price, "shares": last["diluted_shares"], "fcf_ttm": last["fcf"], "sbc_ttm": last["sbc"], "rev_ttm": last["revenue"],
            "rev_yoy": 8.0, "net_debt_to_oi": 0.5, "dividends_ttm": last["dividends_paid"]}


def test_a_quality_compounder():
    ys = _years()
    o = BF.analyze(_m(ys), ys)
    assert o["ok"] and o["moat"] == "wide" and o["years"] == 10
    assert abs(o["oe"] - (ys[-1]["fcf"] - ys[-1]["sbc"])) < 1e-9  # steady margins: TTM and typical agree
    # sustainable growth = ROIC x retained share; per share it adds the buyback
    assert abs(o["g_src"]["sustain"] - 25.0 * (1 - 0.25 * 0.5 / 0.23)) < 1e-6 and abs(o["shrink"] - 2.0) < 1e-6  # payout measured against owner earnings
    assert abs(o["now"] - BF.value_per_share(o["oe_ps"], o["g"], 10, 3.0, 9.0)) < 1e-9
    assert o["buy"] == o["now"] * 0.75 and o["bear"] <= o["now"] <= o["bull"]
    # the price closes the gap to the value over five years, while the value grows with owner earnings
    p5 = o["path"]["5.0"]
    assert abs(p5 - o["now"] * BF.oe_after(o["g"], 10, 3.0, 5.0)) < 1e-6
    assert abs(o["path"]["0.25"] - 1.2) < abs(p5 - 1.2) / 5  # a quarter is a small step, not the whole gap
    assert all(ok for t, ok in o["checks"][:5])


def test_weak_or_loss_making_businesses():
    ys = _years(roic=6.0, fcf_m=0.05, sbc_m=0.06)  # owner earnings negative once stock pay is counted
    o = BF.analyze(_m(ys), ys)
    assert o["ok"] is False and "באפט" in o["reason"]
    ys = _years(roic=7.0)
    o = BF.analyze(_m(ys), ys)
    assert o["moat"] == "none" and o["years"] == 3 and o["g_biz"] <= 10
    hot = _years(g=0.6, roic=40.0)
    m = _m(hot)
    m["rev_yoy"] = 70.0
    o = BF.analyze(m, hot)
    assert o["g_biz"] == 20.0 and any("נספרה" in w for w in o["why"])


def test_assumptions_and_guidance():
    ys = _years()
    base = BF.analyze(_m(ys), ys)
    up = BF.analyze(_m(ys), ys, guidance="raised")
    assert up["g_auto"] == base["g_auto"] + 1.5
    mine = BF.analyze(_m(ys), ys, assume={"g": 4, "years": 5, "r": 11, "mos": 40})
    assert mine["mine"] and mine["g"] == 4 and mine["years"] == 5 and mine["now"] < base["now"] and mine["buy"] == mine["now"] * 0.6
    ai = BF.analyze(_m(ys), ys, moat_ai="narrow")
    assert ai["moat"] == "narrow" and ai["moat_src"] == "ai" and ai["years"] == 6


def test_banks_use_profit_and_a_misfit_is_flagged():
    ys = _years()
    for r in ys:
        r["net_income"] = r["revenue"] * 0.3
    m = _m(ys)
    m.update({"template": "bank", "ni_ttm": ys[-1]["net_income"], "roe": 14.0, "fcf_ttm": -500.0})
    o = BF.analyze(m, ys)
    assert o["ok"] and o["fin"] and abs(o["oe"] - ys[-1]["net_income"]) < 1e-9 and o["roic5"] == 14.0
    far = BF.analyze(_m(ys, price=50.0), ys)  # value a fortieth of the price: the method does not fit
    assert not far["fits"] and far["conf"] == "low" and far["path"]["5.0"] / 50.0 > 0.55
