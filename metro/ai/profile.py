"""Business profiles: one per company per annual report, made on request and for the companies you follow."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from ..config import get_settings
from ..db import Annual, Company, CompanyProfile, Filing, Holding, PaperPosition, Watch, kv_get, kv_set, session
from . import engine as E
from .verify import verify_card

log = logging.getLogger(__name__)
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "40-F")


def latest_annual_report(s, ticker: str, client: Any = None) -> tuple[Optional[str], dict[str, str]]:
    """The newest 10-K's sections: from disk when the filing was already parsed, else fetched once and saved."""
    from ..pipeline import load_doc, save_doc
    from ..sources import sections as SX
    f = s.scalars(select(Filing).where(Filing.ticker == ticker, Filing.form.in_(ANNUAL_FORMS)).order_by(Filing.filed.desc())).first()
    if f:
        secs = load_doc(ticker, f.accession, "sections.json")
        if secs:
            return f.accession, secs
    if client is None:
        return (f.accession if f else None), {}
    c = s.get(Company, ticker)
    ref = next((r for r in client.filings(c.cik, ttl=6 * 3600) if r.form in ANNUAL_FORMS), None)
    if ref is None:
        return None, {}
    secs = SX.split_filing(ref.form, client.text(ref.url))
    save_doc(ticker, ref.accession, "sections.json", secs)
    return ref.accession, secs


def request(ticker: str) -> None:
    with session() as s:
        want = [t for t in (kv_get(s, "profile.want") or []) if t != ticker]
        kv_set(s, "profile.want", [ticker] + want[:30])


def state(s, ticker: str) -> str:
    if ticker in (kv_get(s, "profile.want") or []):
        return "queued"
    return "ready" if get_settings().anthropic_api_key else "needs_key"


def _todo(s) -> list[str]:
    want = list(kv_get(s, "profile.want") or [])
    have = set(s.scalars(select(CompanyProfile.ticker)))
    focus = set(s.scalars(select(Holding.ticker))) | set(s.scalars(select(Watch.ticker))) | set(s.scalars(select(PaperPosition.ticker)))
    return want + sorted(focus - have - set(want))


def run_profiles(eng: Optional[E.Engine] = None, client: Any = None, limit: int = 2) -> dict[str, int]:
    from .runner import month_spend
    st = get_settings()
    out = {"done": 0, "errors": 0}
    with session() as s:
        if month_spend(s) >= st.ai_monthly_budget_usd:
            return {**out, "over_budget": 1}
        todo = _todo(s)[:limit]
    if not todo:
        return out
    eng = eng or E.Engine()
    for t in todo:
        try:
            with session() as s:
                c = s.get(Company, t)
                acc, secs = latest_annual_report(s, t, client)
                if not secs:
                    raise RuntimeError("אין דוח שנתי (10-K) שנקלט לחברה")
                if s.scalars(select(CompanyProfile.id).where(CompanyProfile.ticker == t, CompanyProfile.accession == acc)).first():
                    continue
                annual = [{"fy": a.fy, "values": a.values} for a in s.scalars(select(Annual).where(Annual.ticker == t).order_by(Annual.period_end))]
                name, sub = c.name, c.sub_industry
            params = E.build_profile(t, name, sub, secs, annual)
            data, _, cost = eng.run(params, "profile")
            ver = verify_card(data, {"filing": E.profile_source(secs)})
            with session() as s:
                s.add(CompanyProfile(ticker=t, accession=acc or "", model=params["model"], profile=data, verification=ver, cost_usd=cost))
            out["done"] += 1
        except Exception as e:  # noqa: BLE001
            out["errors"] += 1
            log.warning("profile %s: %s", t, e)
            with session() as s:
                kv_set(s, f"profile.error.{t}", str(e)[:300])
        finally:
            with session() as s:
                kv_set(s, "profile.want", [x for x in (kv_get(s, "profile.want") or []) if x != t])
    return out
