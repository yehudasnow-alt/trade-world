"""Runs queued report-card jobs: builds sources from stored filings, calls Claude, verifies, stores."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func, select

from ..config import get_settings
from ..db import AiJob, Company, Filing, Promise, Quarter, ReportCard, kv_set, session, utcnow
from ..sources import transcripts
from ..sources.sections import LABELS_HE
from . import engine as E
from .prompts import PROMPT_VERSION
from .verify import verify_card

log = logging.getLogger(__name__)
FILING_ORDER = ["mdna", "risk_factors", "legal", "repurchases", "controls", "market_risk", "financial_statements",
                "business", "cybersecurity", "market_equity", "other"]


def month_spend(s) -> float:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(s.scalar(select(func.coalesce(func.sum(ReportCard.cost_usd), 0.0)).where(ReportCard.created >= start)) or 0)


def _filing_text(secs: dict[str, str]) -> tuple[str, str]:
    keys = [k for k in FILING_ORDER if k in secs] + [k for k in secs if k not in FILING_ORDER]
    parts = [f"=== {k} ({LABELS_HE.get(k, k)}) ===\n{secs[k]}" for k in keys]
    return "\n\n".join(parts), ",".join(keys)


def _quarter_for(s, ticker: str, report_date: date | None) -> Quarter | None:
    q = select(Quarter).where(Quarter.ticker == ticker).order_by(Quarter.period_end.desc())
    rows = list(s.scalars(q))
    if report_date:
        near = [r for r in rows if abs((r.period_end - report_date).days) <= 10]
        if near:
            return near[0]
        before = [r for r in rows if r.period_end <= report_date]
        return before[0] if before else (rows[0] if rows else None)
    return rows[0] if rows else None


def build(s, job: AiJob) -> tuple[E.Context, E.Sources, str, str, dict]:
    from ..pipeline import load_doc
    st = get_settings()
    c = s.get(Company, job.ticker)
    f = s.get(Filing, job.accession)
    q = _quarter_for(s, job.ticker, f.report_date if f else None)
    rows = list(s.scalars(select(Quarter).where(Quarter.ticker == job.ticker).order_by(Quarter.period_end.desc()).limit(5)))
    xrows = [{"label": f"{r.fiscal_label} ({r.calendar_label})", "values": r.values} for r in rows]
    label = f"{q.fiscal_label} ({q.period_start or ''}..{q.period_end})" if q else (f.report_date.isoformat() if f and f.report_date else "")
    src = E.Sources()
    if job.stage == "release":
        src.press_release = load_doc(job.ticker, job.accession, "press_release.txt", "")
    else:
        secs = load_doc(job.ticker, job.accession, "sections.json", {}) or {}
        src.filing, src.filing_sections = _filing_text(secs)
        pr = s.scalars(select(Filing).where(Filing.ticker == job.ticker, Filing.form == "8-K", Filing.filed <= f.filed,
                                            Filing.filed >= f.filed - timedelta(days=45))
                       .order_by(Filing.filed.desc())).all()
        for p in pr:
            txt = load_doc(job.ticker, p.accession, "press_release.txt", "")
            if txt:
                src.press_release = txt
                break
        if q:
            fq, fy = int(q.fiscal_label[2]), 2000 + int(q.fiscal_label[-2:])
            text = transcripts.get_transcript(job.ticker, fy, fq)
            if text:
                src.transcript_prepared, src.transcript_qa = transcripts.split_qa(text)
    prev = s.scalars(select(ReportCard).where(ReportCard.ticker == job.ticker, ReportCard.accession != job.accession)
                     .order_by(ReportCard.created.desc())).first()
    promises = [{"id": p.id, "metric": p.metric, "target": p.target, "deadline": p.deadline, "quote": p.quote}
                for p in s.scalars(select(Promise).where(Promise.ticker == job.ticker, Promise.status.in_(("open", "on_track"))))]
    ctx = E.Context(job.ticker, c.name, c.sub_industry, label, f"fiscal year ends {c.fiscal_year_end or '1231'} (MMDD)",
                    xbrl_rows=xrows, promises=promises, previous_card=prev.card if prev else None,
                    diffs=load_doc(job.ticker, job.accession, "diffs.json", {}) or {})
    kind = "card" if job.deep else "light"
    model = st.ai_model_card if job.deep else st.ai_model_light
    return ctx, src, kind, model, (q.values if q else {})


def params_for(ctx: E.Context, src: E.Sources, kind: str, model: str) -> dict:
    return E.build_card(ctx, src, model) if kind == "card" else E.build_light(ctx, src, model)


def store(s, job: AiJob, kind: str, model: str, data: dict, cost: float, src: E.Sources, xbrl_latest: dict,
          review: dict | None = None) -> ReportCard:
    ver = verify_card(data, src.as_dict(), xbrl_latest)
    f = s.get(Filing, job.accession)
    card = ReportCard(ticker=job.ticker, accession=job.accession, period_end=f.report_date if f else None,
                      stage=job.stage if kind == "card" else "light", model=model, prompt_version=PROMPT_VERSION,
                      card=data, verification=ver, review=review or {}, cost_usd=cost)
    s.add(card)
    for p in data.get("promises_new", []):
        s.add(Promise(ticker=job.ticker, metric=p["metric"], target=p["target_he"], deadline=p["deadline_he"],
                      quote=p["citation"]["quote"], source_accn=job.accession, made=f.filed if f else date.today()))
    for pc in data.get("promises_check", []):
        p = s.get(Promise, pc["promise_id"])
        if p and p.ticker == job.ticker:
            if pc["status"] != "not_mentioned":
                p.status = pc["status"]
            p.evidence = pc["evidence_he"]
    g = data.get("guidance_direction")
    if g in ("lowered", "raised", "withdrawn"):
        sev, title = {"lowered": ("high", "ההנהלה הורידה את התחזית"), "raised": ("good", "ההנהלה העלתה את התחזית"),
                      "withdrawn": ("high", "ההנהלה משכה את התחזית")}[g]
        from ..pipeline import add_event
        add_event(s, job.ticker, utcnow(), "earnings", f"guidance-{g}", sev, title, dedupe=f"g:{job.accession}:{g}")
    job.status = "done"
    return card


def run_jobs(eng: E.Engine | None = None, max_single: int = 6) -> dict[str, int]:
    st = get_settings()
    eng = eng or E.Engine()
    stats = {"single": 0, "batched": 0, "errors": 0, "skipped": 0}
    with session() as s:
        spend = month_spend(s)
        jobs = list(s.scalars(select(AiJob).where(AiJob.status == "queued").order_by(AiJob.deep.desc(), AiJob.created)))
        over = spend >= st.ai_monthly_budget_usd
        singles = [j for j in jobs if j.deep][:max_single]
        rest = [j for j in jobs if j not in singles]
        for j in singles:
            try:
                ctx, src, kind, model, xl = build(s, j)
                if not (src.press_release or src.filing):
                    j.status, j.error = "error", "אין מקור טקסט לדוח"
                    stats["errors"] += 1
                    continue
                data, usage, cost = eng.run(params_for(ctx, src, kind, model), kind)
                review = None
                if kind == "card" and not over:
                    try:
                        rv, _, rcost = eng.run(E.build_review(ctx, src, data), "review")
                        review, cost = rv, cost + rcost
                    except E.AiError as e:
                        review = {"verdict": "error", "issues": [], "error": str(e)}
                store(s, j, kind, model, data, cost, src, xl, review)
                stats["single"] += 1
            except Exception as e:  # noqa: BLE001
                j.status, j.error = "error", str(e)[:500]
                stats["errors"] += 1
                log.warning("ai job %s: %s", j.ticker, e)
        if rest and over:
            for j in rest:
                if not j.deep:
                    j.status, j.error = "error", "תקרת התקציב החודשי הושגה"
                    stats["skipped"] += 1
            rest = [j for j in rest if j.deep]
        if rest and st.ai_use_batch:
            items, meta = [], {}
            for j in rest:
                ctx, src, kind, model, _ = build(s, j)
                if not (src.press_release or src.filing):
                    j.status, j.error = "error", "אין מקור טקסט לדוח"
                    continue
                cid = f"job-{j.id}"
                p = params_for(ctx, src, kind, model)
                items.append((cid, p))
                meta[cid] = {"kind": kind, "model": model}
            if items:
                bid = eng.submit(items)
                for j in rest:
                    if f"job-{j.id}" in meta:
                        j.status, j.batch_id, j.request = "batched", bid, meta[f"job-{j.id}"]
                stats["batched"] += len(items)
        kv_set(s, "ai.run", {**stats, "spend_month_usd": round(spend, 2)})
    return stats


def poll_batches(eng: E.Engine | None = None) -> int:
    eng = eng or E.Engine()
    done = 0
    with session() as s:
        ids = set(s.scalars(select(AiJob.batch_id).where(AiJob.status == "batched")))
        for bid in ids:
            if eng.status(bid) != "ended":
                continue
            jobs = {f"job-{j.id}": j for j in s.scalars(select(AiJob).where(AiJob.batch_id == bid))}
            res = eng.collect(bid, {k: j.request["kind"] for k, j in jobs.items()}, {k: j.request["model"] for k, j in jobs.items()})
            for cid, r in res.items():
                j = jobs.get(cid)
                if not j:
                    continue
                if "error" in r:
                    j.status, j.error = "error", r["error"][:500]
                    continue
                ctx, src, kind, model, xl = build(s, j)
                store(s, j, kind, model, r["data"], r["cost"], src, xl)
                done += 1
    return done

