"""Thesis tracking: the metrics that must hold and the conditions that kill a position."""
from __future__ import annotations

from typing import Any

METRICS_HE = {
    "rev_yoy": "צמיחת הכנסות שנתית (%)", "gross_margin": "שולי רווח גולמי (%)", "op_margin": "שולי רווח תפעולי (%)",
    "fcf_margin": "שולי תזרים חופשי (%)", "eps_yoy": "צמיחת רווח למניה (%)", "rpo_yoy": "צמיחת צבר הזמנות RPO (%)",
    "shares_yoy": "שינוי בכמות המניות (%)", "dd_pct": "מרחק מהשיא (%)", "dist_ma200": "מרחק מממוצע 200 (%)",
    "price": "מחיר ($)", "net_debt_to_oi": "חוב נטו / רווח תפעולי",
}
KILLS_HE = {
    "guidance_lowered": "ההנהלה הורידה תחזית", "exec_departure": "עזיבת מנכ״ל או סמנכ״ל כספים",
    "price_below": "המחיר ירד מתחת ל-", "veto": "דגל וטו (הצגה מחדש, החלפת מבקר, ספק המשך פעילות)",
}
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


def evaluate(thesis: dict[str, Any], metrics: dict[str, Any], events: list[dict], guidance: str | None) -> dict[str, Any]:
    """Returns {status, reasons, checks}. Status: ok | watch | broken."""
    checks, fails, reasons = [], 0, []
    for c in thesis.get("metrics") or []:
        name, op, target = c.get("name"), c.get("op", ">"), c.get("value")
        cur = metrics.get(name)
        if cur is None or target is None or op not in OPS:
            checks.append({**c, "current": cur, "pass": None})
            continue
        ok = OPS[op](cur, float(target))
        checks.append({**c, "current": cur, "pass": ok})
        if not ok:
            fails += 1
            reasons.append(f"{METRICS_HE.get(name, name)}: {cur:.1f} (תנאי {op} {target})")
    killed = []
    for k in thesis.get("kills") or []:
        typ = k.get("type")
        if typ == "guidance_lowered" and guidance == "lowered":
            killed.append(KILLS_HE[typ])
        elif typ == "exec_departure" and any(e.get("code") == "5.02-exec" for e in events):
            killed.append(KILLS_HE[typ])
        elif typ == "price_below" and k.get("value") is not None and (metrics.get("price") or 1e18) < float(k["value"]):
            killed.append(f"{KILLS_HE[typ]}{float(k['value']):.2f}$")
        elif typ == "veto" and metrics.get("veto"):
            killed.append(KILLS_HE[typ])
        elif typ == "metric":
            name, op, target = k.get("name"), k.get("op", "<"), k.get("value")
            cur = metrics.get(name)
            if cur is not None and target is not None and op in OPS and OPS[op](cur, float(target)):
                killed.append(f"{METRICS_HE.get(name, name)} {op} {target}")
    status = "broken" if killed or fails >= 2 else "watch" if fails == 1 else "ok"
    return {"status": status, "reasons": killed + reasons, "checks": checks}
