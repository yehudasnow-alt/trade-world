"""HTTP API for the city and the company card. Serves the frontend from /web."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import brief as B
from . import clock
from .analytics.thesis import KILLS_HE, METRICS_HE
from .config import ROOT, get_settings
from .db import (AiJob, Alert, Company, Earnings, Event, Holding, JournalEntry, PriceBar, Promise, Quarter, Quote,
                 ReportCard, Score, Thesis, Watch, init_db, kv_age_seconds, kv_get, session, utcnow)
from .universe import SECTORS

WEB = ROOT / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN201
    init_db()
    yield


app = FastAPI(title="Metropolin", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)


# ---------------- access ----------------
def guard(request: Request) -> None:
    tok = get_settings().access_token
    if not tok:
        from .cloud import on_cloud
        if on_cloud():  # an address on the internet never serves your portfolio without a code
            raise HTTPException(503, "המערכת רצה בענן בלי קוד גישה. הוסף ACCESS_TOKEN במשתני הסביבה של השירות והפעל מחדש.")
        return
    if request.headers.get("x-metro-token") == tok or request.cookies.get("metro_token") == tok:
        return
    raise HTTPException(401, "נדרש קוד גישה. פתח את הכתובת עם ?token=... פעם אחת.")


@app.middleware("http")
async def token_cookie(request: Request, call_next):  # noqa: ANN001
    tok = get_settings().access_token
    if tok and request.query_params.get("token") == tok:
        resp = RedirectResponse(str(request.url.remove_query_params("token")))
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie("metro_token", tok, httponly=True, samesite="lax", secure=https, max_age=60 * 60 * 24 * 180)
        return resp
    return await call_next(request)


def _r(x: Any, n: int = 2) -> Any:
    return None if x is None or (isinstance(x, float) and x != x) else round(x, n) if isinstance(x, float) else x


def _bars(s, tickers: list[str], since: date) -> dict[str, list[PriceBar]]:
    out: dict[str, list[PriceBar]] = {t: [] for t in tickers}
    for b in s.scalars(select(PriceBar).where(PriceBar.ticker.in_(tickers), PriceBar.day >= since).order_by(PriceBar.day)):
        out[b.ticker].append(b)
    return out


def _reaction(bars: list[PriceBar], day: date, hour: str) -> float | None:
    days = [b.day for b in bars]
    if day not in days:
        return None
    i = days.index(day)
    j = i + 1 if hour == "amc" else i
    if j >= len(bars) or j == 0:
        return None
    return (bars[j].c / bars[j - 1].c - 1) * 100


def _rep(today: date, e_past: Earnings | None, e_next: Earnings | None, bars: list[PriceBar] | None) -> dict:
    if e_past and (today - e_past.day).days <= 21:
        surp = (e_past.eps_act / e_past.eps_est - 1) * 100 if e_past.eps_act is not None and e_past.eps_est else None
        return {"st": "done", "day": e_past.day.isoformat(), "hour": e_past.hour, "days": (today - e_past.day).days,
                "surp": _r(surp, 1), "react": _r(_reaction(bars or [], e_past.day, e_past.hour), 2)}
    if e_next:
        n = clock.trading_days_between(today, e_next.day)
        return {"st": "soon" if n <= 20 else "later", "day": e_next.day.isoformat(), "hour": e_next.hour, "days": n}
    return {"st": "none"}


def session_info() -> dict:
    now = clock.now_ny()
    return {"state": clock.session_state(now), "ny": clock.fmt_hhmm(now, clock.NY), "il": clock.fmt_hhmm(now, clock.IL),
            "last_session": clock.last_session(now).isoformat()}


# ---------------- city ----------------
@app.get("/api/city", dependencies=[Depends(guard)])
def city() -> dict:
    today = clock.now_ny().date()
    with session() as s:
        comps = list(s.scalars(select(Company).where(Company.in_universe.is_(True))))
        scores = {x.ticker: x for x in s.scalars(select(Score))}
        quotes = {x.ticker: x for x in s.scalars(select(Quote))}
        held = {x.ticker: x for x in s.scalars(select(Holding))}
        theses = {x.ticker: x for x in s.scalars(select(Thesis))}
        watch = set(s.scalars(select(Watch.ticker)))
        past, nxt = {}, {}
        for e in s.scalars(select(Earnings).where(Earnings.day >= today - timedelta(days=30), Earnings.day <= today + timedelta(days=90)).order_by(Earnings.day)):
            if e.day <= today and (e.eps_act is not None or e.day < today):
                past[e.ticker] = e
            elif e.day >= today and e.ticker not in nxt:
                nxt[e.ticker] = e
        recent = [t for t, e in past.items() if (today - e.day).days <= 21]
        bars = _bars(s, recent, today - timedelta(days=40)) if recent else {}
        cards = {}
        for rc in s.scalars(select(ReportCard).where(ReportCard.created >= utcnow() - timedelta(days=120)).order_by(ReportCard.created)):
            cards[rc.ticker] = rc.card.get("guidance_direction")
        rows = []
        for c in comps:
            sc, q = scores.get(c.ticker), quotes.get(c.ticker)
            m = (sc.metrics if sc else {}) or {}
            px = q.price if q else m.get("price")
            prev = (q.prev_close if q and q.prev_close else None) or m.get("prev_close")
            chg = (px / prev - 1) * 100 if px and prev else m.get("chg_pct")
            std = m.get("std_pct")
            dec = m.get("decomp")
            rep = _rep(today, past.get(c.ticker), nxt.get(c.ticker), bars.get(c.ticker))
            if rep["st"] == "done":
                rep["guide"] = cards.get(c.ticker)
            h, th = held.get(c.ticker), theses.get(c.ticker)
            rows.append({
                "t": c.ticker, "n": c.name, "s": c.sector, "u": c.sub_industry, "k": c.cik, "alts": c.alt_tickers,
                "cap": _r((m.get("market_cap") or 0) / 1e9, 1) or None, "px": _r(px), "chg": _r(chg),
                "sig": _r(chg / std, 2) if chg is not None and std else _r(m.get("sigma")), "rvol": _r(m.get("rvol")),
                "atr": _r(m.get("atr_pct")), "std": _r(std), "beta": _r(m.get("beta")), "dd": _r(m.get("dd_pct"), 1),
                "dec": {"m": _r(dec["market"]), "s": _r(dec["sector"]), "i": _r(dec["idio"])} if dec else None,
                "sc": {"q": sc.quality, "d": sc.direction, "v": sc.valuation, "t": sc.timing, "tot": sc.total} if sc else None,
                "veto": (sc.veto if sc else []), "beacon": bool(sc and sc.beacon), "val": _r(m.get("pe_pct_5y"), 0),
                "pe": _r(m.get("pe_ttm"), 1), "rep": rep, "watch": c.ticker in watch, "th": c.themes,
                "hold": {"w": _r(h.weight_pct, 1), "pnl": _r(h.pnl_pct, 1), "th": th.status if th else None, "q": _r(h.quantity, 4),
                         "cost": _r(h.avg_cost), "v": _r(h.market_value, 0)} if h else None,
            })
        alerts = [{"id": a.id, "ts": a.ts.isoformat(), "t": a.ticker, "level": a.level, "kind": a.kind, "title": a.title,
                   "body": a.body, "read": a.read}
                  for a in s.scalars(select(Alert).where(Alert.ts >= utcnow() - timedelta(days=3)).order_by(Alert.ts.desc()).limit(30))]
        etfs = {}
        for name_en, (he, etf) in SECTORS.items():
            q = quotes.get(etf)
            etfs[name_en] = _r((q.price / q.prev_close - 1) * 100) if q and q.prev_close else None
        mode = kv_get(s, "mode", "live")
        pf_info = kv_get(s, "portfolio.last", None) or kv_get(s, "ibkr.flex", None) or {}
        pf_age = kv_age_seconds(s, "portfolio.last")
    caps = [(r["cap"] or 0, r["chg"]) for r in rows if r["chg"] is not None]
    wsum = sum(w for w, _ in caps) or 1
    return {"session": session_info(), "mode": mode, "companies": rows, "alerts": alerts, "sector_etf": etfs,
            "index": {"chg": _r(sum(w * c for w, c in caps) / wsum), "breadth": _r(sum(1 for _, c in caps if c > 0) / max(1, len(caps)) * 100, 0)},
            "relations": relations(), "generated": utcnow().isoformat(),
            "portfolio": {"source": pf_info.get("source", "demo" if mode == "demo" else "flex"), "nav": pf_info.get("nav"),
                          "cash": pf_info.get("cash"), "age_s": pf_age}}


@app.get("/api/quotes", dependencies=[Depends(guard)])
def quotes(since: Optional[str] = None) -> dict:
    t0 = datetime.fromisoformat(since) if since else utcnow() - timedelta(days=5)
    with session() as s:
        rows = {q.ticker: [q.price, _r((q.price / q.prev_close - 1) * 100) if q.prev_close else None]
                for q in s.scalars(select(Quote).where(Quote.ts > t0))}
    return {"quotes": rows, "session": session_info(), "now": utcnow().isoformat()}


# ---------------- company card ----------------
@app.get("/api/company/{ticker}", dependencies=[Depends(guard)])
def company(ticker: str) -> dict:
    t = ticker.upper()
    today = clock.now_ny().date()
    with session() as s:
        c = s.get(Company, t)
        if not c:
            alt = s.scalars(select(Company)).all()
            c = next((x for x in alt if t in (x.alt_tickers or [])), None)
            if not c:
                raise HTTPException(404, f"{t} לא נמצאה ביקום (S&P 500).")
            t = c.ticker
        sc, q = s.get(Score, t), s.get(Quote, t)
        m = dict((sc.metrics if sc else {}) or {})
        bars = list(s.scalars(select(PriceBar).where(PriceBar.ticker == t).order_by(PriceBar.day.desc()).limit(260)))[::-1]
        closes = [b.c for b in bars]
        ma50 = [None if i < 49 else round(sum(closes[i - 49:i + 1]) / 50, 4) for i in range(len(closes))]
        series = {"days": [b.day.isoformat() for b in bars][-126:], "c": closes[-126:], "v": [b.v for b in bars][-126:], "ma50": ma50[-126:]}
        ers = list(s.scalars(select(Earnings).where(Earnings.ticker == t).order_by(Earnings.day.desc()).limit(10)))
        earnings = [{"day": e.day.isoformat(), "hour": e.hour, "fiscal": e.fiscal, "eps_est": e.eps_est, "eps_act": e.eps_act,
                     "rev_est": e.rev_est, "rev_act": e.rev_act,
                     "surp": _r((e.eps_act / e.eps_est - 1) * 100, 1) if e.eps_act is not None and e.eps_est else None,
                     "react": _r(_reaction(bars, e.day, e.hour), 2)} for e in ers]
        nxt = next((e for e in sorted(ers, key=lambda e: e.day) if e.day >= today), None)
        qrows = list(s.scalars(select(Quarter).where(Quarter.ticker == t).order_by(Quarter.period_end.desc()).limit(8)))
        keep = ("revenue", "gross_margin", "op_margin", "net_income", "eps_diluted", "fcf", "fcf_margin", "sbc_pct", "cash",
                "debt_total", "net_debt", "rpo", "inventory", "receivables", "diluted_shares", "buybacks")
        quarters = [{"fiscal": r.fiscal_label, "calendar": r.calendar_label, "end": r.period_end.isoformat(),
                     "values": {k: r.values.get(k) for k in keep if r.values.get(k) is not None},
                     "derived": r.derived, "restated": r.restated} for r in qrows]
        rc = s.scalars(select(ReportCard).where(ReportCard.ticker == t).order_by(ReportCard.created.desc())).first()
        hist = [{"id": x.id, "stage": x.stage, "created": x.created.isoformat(), "headline": x.card.get("headline_he", "")}
                for x in s.scalars(select(ReportCard).where(ReportCard.ticker == t).order_by(ReportCard.created.desc()).limit(8))]
        pending = s.scalar(select(AiJob.id).where(AiJob.ticker == t, AiJob.status.in_(("queued", "batched"))))
        promises = [{"id": p.id, "metric": p.metric, "target": p.target, "deadline": p.deadline, "status": p.status,
                     "evidence": p.evidence, "made": p.made.isoformat() if p.made else None, "quote": p.quote}
                    for p in s.scalars(select(Promise).where(Promise.ticker == t).order_by(Promise.id.desc()).limit(20))]
        events = [{"ts": e.ts.isoformat(), "kind": e.kind, "code": e.code, "severity": e.severity, "title": e.title, "url": e.url,
                   "data": e.data} for e in s.scalars(select(Event).where(Event.ticker == t, Event.ts >= utcnow() - timedelta(days=120))
                                                    .order_by(Event.ts.desc()).limit(30))]
        peers_rows = list(s.scalars(select(Company).where(Company.sub_industry == c.sub_industry, Company.in_universe.is_(True))))
        peer_scores = {x.ticker: x for x in s.scalars(select(Score).where(Score.ticker.in_([p.ticker for p in peers_rows])))}
        peer_quotes = {x.ticker: x for x in s.scalars(select(Quote).where(Quote.ticker.in_([p.ticker for p in peers_rows])))}
        peers = []
        for p in peers_rows:
            ps, pq = peer_scores.get(p.ticker), peer_quotes.get(p.ticker)
            pm = (ps.metrics if ps else {}) or {}
            peers.append({"t": p.ticker, "n": p.name, "cap": _r((pm.get("market_cap") or 0) / 1e9, 1),
                          "chg": _r((pq.price / pq.prev_close - 1) * 100) if pq and pq.prev_close else _r(pm.get("chg_pct")),
                          "pe": _r(pm.get("pe_ttm"), 1), "score": ps.total if ps else None,
                          "rev_yoy": _r(pm.get("rev_yoy"), 1), "gm": _r(pm.get("gross_margin"), 1)})
        peers.sort(key=lambda x: -(x["cap"] or 0))
        pes = sorted(x["pe"] for x in peers if x["pe"])
        th, w, h = s.get(Thesis, t), s.get(Watch, t), s.get(Holding, t)
        journal = [{"id": j.id, "ts": j.ts.isoformat(), "action": j.action, "price": j.price, "note": j.note, "snapshot": j.snapshot}
                   for j in s.scalars(select(JournalEntry).where(JournalEntry.ticker == t).order_by(JournalEntry.ts.desc()).limit(30))]
        out = {
            "company": {"t": t, "n": c.name, "sector": c.sector, "sub": c.sub_industry, "cik": c.cik, "alts": c.alt_tickers,
                        "fye": c.fiscal_year_end, "template": c.template, "themes": c.themes},
            "quote": {"px": q.price, "prev": q.prev_close, "ts": q.ts.isoformat(), "source": q.source} if q else None,
            "score": {"q": sc.quality, "d": sc.direction, "v": sc.valuation, "t": sc.timing, "tot": sc.total,
                      "veto": sc.veto, "beacon": sc.beacon, "computed": sc.computed.isoformat()} if sc else None,
            "metrics": {k: v for k, v in m.items() if k not in ("volume_profile",)}, "volume_profile": m.get("volume_profile", []),
            "series": series, "earnings": earnings, "next_earnings": nxt.day.isoformat() if nxt else None,
            "quarters": quarters,
            "card": {"id": rc.id, "stage": rc.stage, "model": rc.model, "created": rc.created.isoformat(), "card": rc.card,
                     "verification": rc.verification, "review": rc.review, "accession": rc.accession} if rc else None,
            "card_history": hist, "card_pending": bool(pending), "promises": promises, "events": events, "peers": peers[:14],
            "street": {"pe_median": pes[len(pes) // 2] if pes else None, "n": len(peers)},
            "thesis": {"text": th.text, "metrics": th.metrics, "kills": th.kills, "status": th.status, "reasons": th.reasons,
                       "horizon_weeks": th.horizon_weeks, "updated": th.updated.isoformat()} if th else None,
            "watch": {"entry_low": w.entry_low, "entry_high": w.entry_high, "note": w.note} if w else None,
            "holding": {"quantity": h.quantity, "avg_cost": h.avg_cost, "weight": h.weight_pct, "pnl": h.pnl_pct} if h else None,
            "journal": journal, "relations": [r for r in relations() if t in (r["from"], r["to"])],
            "links": {"edgar": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c.cik}&type=&dateb=&owner=include&count=40"},
            "defaults": {"portfolio": get_settings().portfolio_size_usd, "risk_pct": get_settings().risk_per_trade_pct},
        }
    return json.loads(json.dumps(out, default=str))


@app.get("/api/timeline", dependencies=[Depends(guard)])
def timeline(back: int = 30, ahead: int = 75) -> dict:
    """Everything dated around today: reports, expected events, major events and news, and where the season stands."""
    today = clock.now_ny().date()
    lo, hi = today - timedelta(days=back), today + timedelta(days=ahead)
    with session() as s:
        comps = {c.ticker: c for c in s.scalars(select(Company).where(Company.in_universe.is_(True)))}
        held, watch = set(s.scalars(select(Holding.ticker))), set(s.scalars(select(Watch.ticker)))
        caps = {x.ticker: (x.metrics or {}).get("market_cap") for x in s.scalars(select(Score))}
        ers = [e for e in s.scalars(select(Earnings).where(Earnings.day >= lo, Earnings.day <= hi).order_by(Earnings.day)) if e.ticker in comps]
        evs = [e for e in s.scalars(select(Event).where(Event.ts >= datetime.combine(lo, datetime.min.time()),
                                                         Event.ts <= datetime.combine(hi, datetime.max.time())).order_by(Event.ts))
               if e.ticker in comps and (e.kind == "catalyst" or e.severity in ("veto", "high", "good", "watch"))]
        season_lo = today - timedelta(days=45)
        reported = {e.ticker for e in s.scalars(select(Earnings).where(Earnings.day >= season_lo, Earnings.day <= today)) if e.eps_act is not None or e.day < today}
    focus = held | watch
    cap = lambda t: _r((caps.get(t) or 0) / 1e9, 1)  # noqa: E731
    return {
        "today": today.isoformat(), "year": {"day": today.timetuple().tm_yday, "days": 366 if today.year % 4 == 0 else 365},
        "season": {"reported": len(reported & set(comps)), "total": len(comps)},
        "earnings": [{"t": e.ticker, "day": e.day.isoformat(), "hour": e.hour, "held": e.ticker in held, "watch": e.ticker in watch,
                      "cap": cap(e.ticker), "done": e.day < today or e.eps_act is not None,
                      "surp": _r((e.eps_act / e.eps_est - 1) * 100, 1) if e.eps_act is not None and e.eps_est else None} for e in ers],
        "events": [{"t": e.ticker, "day": e.ts.date().isoformat(), "kind": e.kind, "code": e.code, "severity": e.severity, "title": e.title,
                    "url": e.url, "focus": e.ticker in focus, "cap": cap(e.ticker), "when": (e.data or {}).get("when")} for e in evs],
    }


@app.get("/api/search", dependencies=[Depends(guard)])
def search(q: str = "") -> list[dict]:
    ql = q.strip().lower()
    with session() as s:
        comps = list(s.scalars(select(Company).where(Company.in_universe.is_(True))))
    def rank(c: Company) -> int:
        t, n = c.ticker.lower(), c.name.lower()
        if t == ql or ql in [a.lower() for a in c.alt_tickers or []]:
            return 0
        if t.startswith(ql):
            return 1
        if n.startswith(ql):
            return 2
        if ql in n:
            return 3
        if ql in c.sub_industry.lower():
            return 4
        return 9
    hits = sorted((c for c in comps if rank(c) < 9), key=lambda c: (rank(c), c.ticker))[:12] if ql else []
    return [{"t": c.ticker, "n": c.name, "s": c.sector, "u": c.sub_industry} for c in hits]


# ---------------- portfolio, thesis, journal, watchlist ----------------
@app.get("/api/portfolio", dependencies=[Depends(guard)])
def portfolio() -> dict:
    with session() as s:
        comps = {c.ticker: c for c in s.scalars(select(Company))}
        rows = []
        for h in s.scalars(select(Holding)):
            th, sc, q = s.get(Thesis, h.ticker), s.get(Score, h.ticker), s.get(Quote, h.ticker)
            c = comps.get(h.ticker)
            rows.append({"t": h.ticker, "n": c.name if c else "", "s": c.sector if c else "", "themes": c.themes if c else [],
                         "w": h.weight_pct, "pnl": h.pnl_pct, "qty": h.quantity, "cost": h.avg_cost,
                         "px": q.price if q else None, "chg": (q.price / q.prev_close - 1) * 100 if q and q.prev_close else None,
                         "th": th.status if th else None, "reasons": th.reasons if th else [], "score": sc.total if sc else None})
        by_sector: dict[str, float] = {}
        by_theme: dict[str, float] = {}
        for r in rows:
            by_sector[r["s"]] = by_sector.get(r["s"], 0) + (r["w"] or 0)
            for th in r["themes"] or []:
                by_theme[th] = by_theme.get(th, 0) + (r["w"] or 0)
        flex = kv_get(s, "ibkr.flex", {})
    rows.sort(key=lambda r: -(r["w"] or 0))
    return {"holdings": rows, "by_sector": by_sector, "by_theme": by_theme, "nav": flex.get("nav")}


class MetricRule(BaseModel):
    name: str
    op: str = ">"
    value: float


class KillRule(BaseModel):
    type: str
    value: Optional[float] = None
    name: Optional[str] = None
    op: Optional[str] = None


class ThesisIn(BaseModel):
    text: str = Field(default="", max_length=4000)
    metrics: list[MetricRule] = []
    kills: list[KillRule] = []
    horizon_weeks: int = 12


@app.get("/api/thesis-options", dependencies=[Depends(guard)])
def thesis_options() -> dict:
    return {"metrics": METRICS_HE, "kills": KILLS_HE}


@app.put("/api/thesis/{ticker}", dependencies=[Depends(guard)])
def put_thesis(ticker: str, body: ThesisIn) -> dict:
    t = ticker.upper()
    with session() as s:
        if not s.get(Company, t):
            raise HTTPException(404, "מניה לא ידועה")
        th = s.get(Thesis, t) or Thesis(ticker=t)
        th.text, th.metrics = body.text, [m.model_dump() for m in body.metrics]
        th.kills, th.horizon_weeks, th.updated = [k.model_dump(exclude_none=True) for k in body.kills], body.horizon_weeks, utcnow()
        s.merge(th)
    from .pipeline import check_theses
    with session() as s:
        sc = s.get(Score, t)
        raws = {t: sc.metrics} if sc else {}
    check_theses(raws)
    return {"ok": True}


@app.delete("/api/thesis/{ticker}", dependencies=[Depends(guard)])
def delete_thesis(ticker: str) -> dict:
    with session() as s:
        th = s.get(Thesis, ticker.upper())
        if th:
            s.delete(th)
    return {"ok": True}


class JournalIn(BaseModel):
    ticker: str
    action: str = Field(pattern="^(buy|add|trim|sell|pass|note)$")
    price: Optional[float] = None
    note: str = Field(default="", max_length=4000)


@app.get("/api/journal", dependencies=[Depends(guard)])
def journal(limit: int = 100) -> list[dict]:
    with session() as s:
        return [{"id": j.id, "ts": j.ts.isoformat(), "t": j.ticker, "action": j.action, "price": j.price, "note": j.note,
                 "snapshot": j.snapshot} for j in s.scalars(select(JournalEntry).order_by(JournalEntry.ts.desc()).limit(limit))]


@app.post("/api/journal", dependencies=[Depends(guard)])
def add_journal(body: JournalIn) -> dict:
    t = body.ticker.upper()
    with session() as s:
        sc, q = s.get(Score, t), s.get(Quote, t)
        snap = {"score": {"q": sc.quality, "d": sc.direction, "v": sc.valuation, "t": sc.timing, "tot": sc.total} if sc else None,
                "price": q.price if q else None, "pe": (sc.metrics or {}).get("pe_ttm") if sc else None,
                "dd": (sc.metrics or {}).get("dd_pct") if sc else None}
        j = JournalEntry(ticker=t, action=body.action, price=body.price or (q.price if q else None), note=body.note, snapshot=snap)
        s.add(j)
        s.flush()
        return {"id": j.id}


class WatchIn(BaseModel):
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    note: str = ""


@app.put("/api/watchlist/{ticker}", dependencies=[Depends(guard)])
def put_watch(ticker: str, body: WatchIn) -> dict:
    t = ticker.upper()
    with session() as s:
        w = s.get(Watch, t) or Watch(ticker=t)
        w.entry_low, w.entry_high, w.note = body.entry_low, body.entry_high, body.note
        s.merge(w)
    return {"ok": True}


@app.delete("/api/watchlist/{ticker}", dependencies=[Depends(guard)])
def delete_watch(ticker: str) -> dict:
    with session() as s:
        w = s.get(Watch, ticker.upper())
        if w:
            s.delete(w)
    return {"ok": True}


# ---------------- alerts, brief, status ----------------
@app.get("/api/alerts", dependencies=[Depends(guard)])
def alerts(days: int = 7) -> list[dict]:
    with session() as s:
        return [{"id": a.id, "ts": a.ts.isoformat(), "t": a.ticker, "level": a.level, "kind": a.kind, "title": a.title,
                 "body": a.body, "read": a.read}
                for a in s.scalars(select(Alert).where(Alert.ts >= utcnow() - timedelta(days=days)).order_by(Alert.ts.desc()))]


@app.post("/api/alerts/{alert_id}/read", dependencies=[Depends(guard)])
def read_alert(alert_id: int) -> dict:
    with session() as s:
        a = s.get(Alert, alert_id)
        if a:
            a.read = True
    return {"ok": True}


@app.get("/api/brief", dependencies=[Depends(guard)])
def get_brief() -> dict:
    return B.build()


@app.get("/api/status", dependencies=[Depends(guard)])
def status() -> dict:
    st = get_settings()

    def src(key: str, configured: bool, max_age: float) -> dict:
        with session() as s:
            age = kv_age_seconds(s, key)
            val = kv_get(s, key)
        state = "off" if not configured else ("never" if age is None else "ok" if age <= max_age else "stale")
        return {"configured": configured, "age_s": age, "state": state, "info": val}
    def first_fill() -> dict:
        with session() as s:
            bs, pr = kv_get(s, "bootstrap") or {}, kv_get(s, "bootstrap.progress")
        return {"configured": bs.get("state") == "running", "state": "never", "info": pr}
    with session() as s:
        mode = kv_get(s, "mode", "live")
        queued = len(list(s.scalars(select(AiJob.id).where(AiJob.status.in_(("queued", "batched"))))))
        from .ai.runner import month_spend
        spend = month_spend(s)
    open_ = clock.session_state() in ("open", "pre", "post")
    from .cloud import on_cloud
    return {"mode": mode, "cloud": on_cloud(), "session": session_info(), "sources": {
        "sec": src("sec.feed", st.sec_ready, 600 if open_ else 86400),
        "prices": src("quotes.alpaca", st.alpaca_ready, 300 if open_ else 86400 * 3),
        "ibkr_live": src("quotes.ibkr", st.ibkr_live, 120 if open_ else 86400 * 3),
        "ibkr_flex": src("ibkr.flex", st.flex_ready, 86400 * 2),
        "ibkr_portfolio": src("ibkr.portfolio", st.ibkr_live, 300 if open_ else 86400 * 3),
        "earnings": src("earnings", bool(st.finnhub_api_key), 86400 * 2),
        "scores": src("scores", True, 3600 if open_ else 86400 * 3),
        "ai": {"configured": bool(st.anthropic_api_key), "queued": queued, "spend_month_usd": round(spend, 2),
               "budget_usd": st.ai_monthly_budget_usd, "state": "ok" if st.anthropic_api_key else "off"},
        "backup": src("backup", True, 86400 * 2),
        "bootstrap": first_fill(),
        "telegram": {"configured": st.telegram_ready, "state": "ok" if st.telegram_ready else "off"},
    }}


_relations_cache: dict[str, Any] = {}


def relations() -> list[dict]:
    p = get_settings().data_dir / "relations.json"
    if not p.exists():
        return []
    mt = p.stat().st_mtime
    if _relations_cache.get("mt") != mt:
        _relations_cache.update(mt=mt, rows=json.loads(p.read_text(encoding="utf-8")).get("links", []))
    return _relations_cache["rows"]


# ---------------- frontend ----------------
if (WEB / "vendor").exists():
    app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """For the hosting service's health check: answers without a code and reveals nothing."""
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index(request: Request) -> Response:
    p = WEB / "index.html"
    from . import webbuild
    if webbuild.SRC.exists() and webbuild.stale():
        webbuild.build_live()  # generated file, not in git: built on first request and after edits to web/src
    if not p.exists():
        return JSONResponse({"error": "web/index.html חסר. הרץ: metro web"}, 500)
    guard(request)
    return FileResponse(p, headers={"Cache-Control": "no-cache"})
