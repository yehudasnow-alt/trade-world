"""Morning brief: what reported overnight, what changed in the portfolio, what is on today's calendar."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from sqlalchemy import select

from . import clock
from .db import Alert, Company, Earnings, Event, Filing, Holding, Quote, Score, Thesis, Watch, session, utcnow

THESIS_HE = {"ok": "תקפה", "watch": "במעקב", "broken": "נשברה"}


def build(now: datetime | None = None) -> dict:
    now = now or utcnow()
    since = now - timedelta(hours=20)
    today_ny = clock.now_ny().date()
    with session() as s:
        names = {c.ticker: c.name for c in s.scalars(select(Company))}
        held = {h.ticker: h for h in s.scalars(select(Holding))}
        watch = set(s.scalars(select(Watch.ticker)))
        focus = set(held) | watch
        reported = []
        for f in s.scalars(select(Filing).where(Filing.accepted >= since, Filing.form.in_(("8-K", "10-Q", "10-K")))):
            if f.form != "8-K" or "2.02" in (f.items or []):
                reported.append({"ticker": f.ticker, "name": names.get(f.ticker, ""), "form": f.form, "focus": f.ticker in focus})
        reported.sort(key=lambda r: (not r["focus"], r["ticker"]))
        events = [{"ticker": e.ticker, "title": e.title, "severity": e.severity, "focus": e.ticker in focus}
                  for e in s.scalars(select(Event).where(Event.ts >= since, Event.severity.in_(("veto", "high", "good"))))]
        portfolio = []
        for t, h in held.items():
            q, sc, th = s.get(Quote, t), s.get(Score, t), s.get(Thesis, t)
            chg = (q.price / q.prev_close - 1) * 100 if q and q.prev_close else None
            portfolio.append({"ticker": t, "weight": h.weight_pct, "pnl": h.pnl_pct, "chg": chg,
                              "thesis": th.status if th else None, "reasons": (th.reasons if th else [])[:2],
                              "score": sc.total if sc else None})
        portfolio.sort(key=lambda p: -(p["weight"] or 0))
        cal = [{"ticker": e.ticker, "day": e.day.isoformat(), "hour": e.hour, "focus": e.ticker in focus}
               for e in s.scalars(select(Earnings).where(Earnings.day >= today_ny, Earnings.day <= clock.add_trading_days(today_ny, 2)))]
        cal.sort(key=lambda r: (not r["focus"], r["day"], r["ticker"]))
        beacons = [{"ticker": sc.ticker, "score": sc.total} for sc in s.scalars(select(Score).where(Score.beacon.is_(True)).order_by(Score.total.desc()))]
        digest = [{"title": a.title, "level": a.level} for a in s.scalars(select(Alert).where(Alert.ts >= since).order_by(Alert.ts.desc()))][:12]
    return {"generated": now.isoformat(), "session": clock.session_state(), "reported": reported[:40], "events": events[:20],
            "portfolio": portfolio, "calendar": cal[:30], "beacons": beacons, "alerts": digest}


def to_text(b: dict) -> str:
    e = html.escape
    lines = [f"<b>תדריך בוקר · {datetime.now(clock.IL):%d.%m}</b>"]
    if b["portfolio"]:
        lines.append("\n<b>התיק</b>")
        for p in b["portfolio"]:
            chg = f"{p['chg']:+.1f}%" if p["chg"] is not None else "–"
            th = THESIS_HE.get(p["thesis"] or "", "אין תזה")
            lines.append(f"{e(p['ticker'])} {chg} · תזה {th}" + (f" ({e(p['reasons'][0])})" if p["reasons"] else ""))
    focus_rep = [r for r in b["reported"] if r["focus"]]
    lines.append(f"\n<b>דיווחו בלילה</b>: {len(b['reported'])} חברות" + (f", מהן בתיק/מעקב: {', '.join(r['ticker'] for r in focus_rep)}" if focus_rep else ""))
    if b["events"]:
        lines.append("\n<b>אירועים</b>")
        lines += [f"{e(x['ticker'])}: {e(x['title'])}" for x in b["events"][:8]]
    if b["calendar"]:
        lines.append("\n<b>דוחות היום ומחר</b>: " + ", ".join(f"{r['ticker']}{'*' if r['focus'] else ''}" for r in b["calendar"][:15]))
    if b["beacons"]:
        lines.append("\n<b>אלומות</b>: " + ", ".join(f"{x['ticker']} ({x['score']:.0f})" for x in b["beacons"]))
    return "\n".join(lines)


def today_key(d: date | None = None) -> str:
    return f"brief:{(d or date.today()).isoformat()}"
