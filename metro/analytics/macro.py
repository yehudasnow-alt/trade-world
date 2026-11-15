"""The market's weather: plain readings of rates, inflation, jobs, credit, fear, and how expensive stocks are."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional


def _last(rows: list) -> Optional[tuple[str, float]]:
    return tuple(rows[-1]) if rows else None


def _ago(rows: list, days: int) -> Optional[float]:
    if not rows:
        return None
    cut = (date.fromisoformat(rows[-1][0]) - timedelta(days=days)).isoformat()
    older = [v for d, v in rows if d <= cut]
    return older[-1] if older else None


def _monthly(rows: list, n: int = 36) -> list[tuple[str, float]]:
    """One point a month (the last of each), enough for a small chart."""
    by: dict[str, tuple[str, float]] = {}
    for d, v in rows:
        by[d[:7]] = (d, v)
    return list(by.values())[-n:]


def read(series: dict[str, list], market: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"items": [], "market": market}
    s = {k: [tuple(x) for x in v] for k, v in series.items()}

    def item(key, title, value, unit, verdict, tone, explain, chart_key=None, chart=None):
        out["items"].append({"key": key, "title": title, "value": value, "unit": unit, "verdict": verdict, "tone": tone,
                             "explain": explain, "chart": chart if chart is not None else _monthly(s.get(chart_key or key, []))})

    pe, ey = market.get("pe"), market.get("earnings_yield")
    t10 = _last(s.get("DGS10", []))
    if pe:
        tone = "down" if pe > 25 else "wr" if pe > 19 else "up"
        item("PE", "מכפיל הרווח של S&P 500", round(pe, 1), "", "יקר בהשוואה להיסטוריה" if pe > 25 else "מעט מעל הממוצע" if pe > 19 else "סביר",
             tone, "כמה שנות רווח משלמים על כל השוק. הממוצע ההיסטורי סביב 16–18; מעל 25 השוק מתמחר צמיחה גבוהה ומשאיר מעט מקום לטעות.",
             chart=market.get("pe_hist", []))
    if ey and t10:
        prem = ey - t10[1]
        item("ERP", "פרמיית הסיכון של מניות", round(prem, 2), "%", "מניות זולות יחסית לאג\"ח" if prem > 3 else "פרמיה רגילה" if prem > 1 else "כמעט אין פרמיה",
             "up" if prem > 3 else "ac" if prem > 1 else "down",
             f"תשואת הרווח של השוק ({ey:.1f}%: הרווח חלקי המחיר) פחות תשואת אג\"ח ממשלתי בטוח ל-10 שנים ({t10[1]:.1f}%). "
             "כשהפער קטן, אג\"ח ממשלתי בלי סיכון נותן כמעט אותו דבר כמו מניות, ומניות יקרות יחסית.", chart=[])
    if t10:
        prev = _ago(s["DGS10"], 182)
        item("DGS10", "אג\"ח ממשלתי ל-10 שנים", t10[1], "%", ("עולה" if prev and t10[1] > prev + 0.25 else "יורדת" if prev and t10[1] < prev - 0.25 else "יציבה") + " בחצי השנה האחרונה",
             "wr" if prev and t10[1] > prev + 0.25 else "ac", "הריבית ש\"בלי סיכון\" בשוק. כשהיא עולה, כסף עתידי שווה פחות היום, ומניות צמיחה נפגעות ראשונות.")
    ff = _last(s.get("DFF", []))
    if ff:
        prev = _ago(s["DFF"], 182)
        d = ff[1] - prev if prev is not None else 0
        item("DFF", "ריבית הפד", ff[1], "%", "הפד מוריד ריבית" if d < -0.2 else "הפד מעלה ריבית" if d > 0.2 else "ללא שינוי בחצי שנה",
             "up" if d < -0.2 else "down" if d > 0.2 else "ac", "הריבית שהבנק המרכזי האמריקאי קובע. הורדה מקלה על חברות ועל צרכנים; העלאה מאיטה את הכלכלה כדי לרסן אינפלציה.")
    cpi = s.get("CPIAUCSL", [])
    if len(cpi) > 13:
        yoy = (cpi[-1][1] / cpi[-13][1] - 1) * 100
        yoy_prev = (cpi[-7][1] / cpi[-19][1] - 1) * 100 if len(cpi) > 19 else None
        chart = [(cpi[i][0], (cpi[i][1] / cpi[i - 12][1] - 1) * 100) for i in range(12, len(cpi))][-36:]
        item("CPI", "אינפלציה (שנתית)", round(yoy, 1), "%", ("יורדת" if yoy_prev and yoy < yoy_prev - 0.2 else "עולה" if yoy_prev and yoy > yoy_prev + 0.2 else "יציבה") + (", מעל יעד 2% של הפד" if yoy > 2.5 else ""),
             "down" if yoy > 3.5 else "wr" if yoy > 2.5 else "up", "בכמה התייקרו המחירים בשנה. אינפלציה גבוהה מחזיקה את הריבית גבוהה, וזה לוחץ על מכפילי המניות.", chart=chart)
    un = s.get("UNRATE", [])
    if un:
        low12 = min(v for _, v in un[-12:])
        rise = un[-1][1] - low12
        item("UNRATE", "אבטלה", un[-1][1], "%", "עלייה מהירה, סימן אזהרה למיתון" if rise >= 0.5 else "שוק עבודה יציב",
             "down" if rise >= 0.5 else "up", "כשהאבטלה עולה בחצי אחוז מהשפל של השנה האחרונה, זה היה בעבר אחד הסימנים המוקדמים למיתון.")
    yc = _last(s.get("T10Y2Y", []))
    if yc:
        item("T10Y2Y", "עקום התשואות", yc[1], "%", "הפוך: השוק מצפה להאטה" if yc[1] < 0 else "רגיל", "wr" if yc[1] < 0 else "up",
             "ריבית ל-10 שנים פחות ריבית לשנתיים. בדרך כלל חיובי; כשהוא שלילי (הפוך), שוק האג\"ח מצפה שהריבית תרד בגלל האטה. לפני רוב המיתונים הוא התהפך.")
    hy = _last(s.get("BAMLH0A0HYM2", []))
    if hy:
        item("HY", "מרווח אשראי בסיכון", hy[1], "%", "לחץ בשוק האשראי" if hy[1] > 6 else "רגוע" if hy[1] < 4 else "רגיל", "down" if hy[1] > 6 else "up" if hy[1] < 4 else "ac",
             "כמה ריבית מעל ממשלה דורשים מחברות חלשות. כשהמרווח קופץ, המלווים מפחדים, ולרוב גם המניות יורדות.", chart_key="BAMLH0A0HYM2")
    vx = _last(s.get("VIXCLS", []))
    if vx:
        item("VIX", "VIX: מדד הפחד", vx[1], "", "פחד" if vx[1] > 30 else "עצבנות" if vx[1] > 20 else "רגיעה", "down" if vx[1] > 30 else "wr" if vx[1] > 20 else "up",
             "כמה תנודה השוק מצפה לחודש הקרוב, לפי מחירי האופציות. מתחת ל-15 רגוע, מעל 30 פחד. דווקא בזמני פחד נמצאות לעתים קרובות המציאות.", chart_key="VIXCLS")
    out["updated"] = max((v[-1][0] for v in s.values() if v), default=None)
    return out


def market_valuation(rows: list[dict]) -> dict[str, Any]:
    """Cap-weighted P/E of the whole index (total value over total profit), and by sector."""
    tot_cap = tot_e = 0.0
    sec: dict[str, list[float]] = {}
    for r in rows:
        cap, pe = r.get("cap"), r.get("pe")
        if not cap:
            continue
        e = cap / pe if pe and pe > 0 else 0.0  # a loss-maker adds value but no profit
        tot_cap += cap
        tot_e += e
        a = sec.setdefault(r["s"], [0.0, 0.0, 0.0, 0.0])
        a[0] += cap
        a[1] += e
        if r.get("ret") is not None:
            a[2] += cap * r["ret"]
            a[3] += cap
    pe = tot_cap / tot_e if tot_e else None
    return {"pe": pe, "earnings_yield": 100 / pe if pe else None,
            "sectors": {k: {"pe": v[0] / v[1] if v[1] else None, "ret_1y": v[2] / v[3] if v[3] else None, "cap": v[0]} for k, v in sec.items()}}
