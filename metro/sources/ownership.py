"""Who owns the company, from the SEC's own filings (free, no key).

- Insiders (directors, officers, anyone above 10%) must file a Form 4 within two business days of every trade.
  Each Form 4 states how many shares the person holds after the trade, so the latest one per person is their stake.
- Anyone who crosses 5% of a class must file a Schedule 13D (an active holder, who may push for changes) or
  13G (a passive holder, such as an index fund). The cover page states the percent of the class.
The rest of the shares is the public: other funds, pension funds and private investors, each below 5%.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional
from xml.etree import ElementTree as ET

from .sections import html_to_text

HOLDER_FORMS = {"SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13G", "SCHEDULE 13D/A", "SCHEDULE 13G/A"}
_PCT = re.compile(r"percent\s+of\s+class.{0,200}?(\d{1,3}(?:\.\d+)?)\s*%", re.I | re.S)
_SHARES = re.compile(r"aggregate\s+amount\s+beneficially\s+owned.{0,160}?(\d{1,3}(?:,\d{3})+|\d{4,})", re.I | re.S)
_NAME_HEAD = re.compile(r"names?\s+of\s+reporting\s+persons?", re.I)
_SKIP = re.compile(r"I\.?R\.?S\.?|identification|S\.S\.|entities only|^\W*$|^\(?\d+\)?\.?$|check the appropriate", re.I)


@dataclass
class MajorHolder:
    name: str
    pct: Optional[float]
    shares: Optional[float]
    active: bool  # 13D: may seek influence; 13G: passive


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def raw_doc_name(primary_doc: str) -> str:
    """Submissions list the styled path ('xslSCHEDULE_13G_X01/primary_doc.xml'); the raw file sits in the folder root."""
    return primary_doc.split("/")[-1] if primary_doc.lower().startswith("xsl") else primary_doc


def _num(s: str | None) -> Optional[float]:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_13dg(content: str, form: str) -> Optional[MajorHolder]:
    """Name, percent of class and shares from a 13D/13G: the structured XML (since 2024) or the older text cover page."""
    active = "13D" in form.upper()
    head = content.lstrip()[:200].lower()
    if head.startswith("<?xml") or "<edgarsubmission" in head:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            root = None
        if root is not None:
            name = pct = shares = None
            for el in root.iter():
                t, txt = _local(el.tag), (el.text or "").strip()
                if not txt:
                    continue
                if name is None and t in ("reportingpersonname", "nameofreportingperson", "nameofreportingpersons"):
                    name = txt
                elif pct is None and ("percentofclass" in t or t == "classpercent"):
                    pct = _num(txt.rstrip("%"))
                elif shares is None and ("aggregateamountowned" in t or "aggregateamountbeneficiallyowned" in t):
                    shares = _num(txt)
            if name or pct is not None:
                return MajorHolder(name or "", pct, shares, active)
    text = html_to_text(content) if "<" in content[:2000] else content
    m = _PCT.search(text)
    pct = _num(m.group(1)) if m else None
    sm = _SHARES.search(text)
    name = ""
    h = _NAME_HEAD.search(text)
    if h:
        for line in text[h.end():h.end() + 600].splitlines():
            line = line.strip(" :.-\t")
            if line and not _SKIP.search(line) and re.search(r"[A-Za-z]{2}", line):
                name = line[:120]
                break
    if not name and pct is None:
        return None
    return MajorHolder(name, pct, _num(sm.group(1)) if sm else None, active)


@dataclass
class InsiderStake:
    name: str
    title: str
    shares: float
    director: bool
    officer: bool
    ten_pct: bool


def _flag(x: str | None) -> bool:
    return (x or "").strip().lower() in ("1", "true")


def parse_form4_stake(xml_text: str) -> Optional[InsiderStake]:
    """Shares held after the reported trades: the last post-transaction amount for each holding line, summed.

    A person can hold directly and through trusts or funds (each a line); common stock only, options are not shares yet.
    """
    root = ET.fromstring(xml_text)
    name = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    director = officer = ten = False
    title = ""
    if rel is not None:
        director, officer, ten = _flag(rel.findtext("isDirector")), _flag(rel.findtext("isOfficer")), _flag(rel.findtext("isTenPercentOwner"))
        title = (rel.findtext("officerTitle") or "").strip()
    lines: dict[tuple[str, str, str], float] = {}
    for tag in ("nonDerivativeTransaction", "nonDerivativeHolding"):
        for el in root.iter(tag):
            sec = (el.findtext("securityTitle/value") or "").strip()
            if sec and not re.search(r"common|class|ordinary|share", sec, re.I):
                continue
            after = _num(el.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value"))
            if after is None:
                continue
            nature = (el.findtext("ownershipNature/directOrIndirectOwnership/value") or "D").strip()
            how = (el.findtext("ownershipNature/natureOfOwnership/value") or "").strip()
            lines[(sec.lower(), nature, how.lower())] = after  # later lines in the same form are later in time
    if not name:
        return None
    if not title:
        title = "דירקטור" if director else ("בעל יותר מ-10%" if ten else "")
    return InsiderStake(name, title, sum(lines.values()), director, officer, ten)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def collect(client: Any, cik: int, shares_out: Optional[float], today: Optional[date] = None,
            max_holder_docs: int = 30, max_form4: int = 60) -> dict[str, list[dict]]:
    """Latest stake per insider (Form 4, two years) and per 5% holder (13D/13G, four years)."""
    from .sec import parse_submissions
    today = today or date.today()
    refs = parse_submissions(client.submissions(cik, ttl=6 * 3600))
    majors: dict[str, dict] = {}
    for r in [r for r in refs if r.form.upper() in HOLDER_FORMS and r.filed >= today - timedelta(days=4 * 365)][:max_holder_docs]:
        try:
            raw = client.get(f"{r.folder}/{raw_doc_name(r.primary_doc)}", ttl=0).decode("utf-8", "replace")
            h = parse_13dg(raw, r.form)
        except Exception:  # noqa: BLE001 - one unreadable filing should not hide the others
            continue
        if not h or not h.name:
            continue
        k = _key(h.name)
        if k in majors:  # newest first: the first one seen is the current stake
            continue
        if h.pct is None and h.shares and shares_out:
            h.pct = h.shares / shares_out * 100
        majors[k] = {"name": h.name, "pct": h.pct, "shares": h.shares, "as_of": r.filed.isoformat(), "form": r.form,
                     "url": r.url, "active": h.active}
    insiders: dict[str, dict] = {}
    for r in [r for r in refs if r.form == "4" and r.filed >= today - timedelta(days=2 * 365)][:max_form4]:
        try:
            st = parse_form4_stake(client.get(f"{r.folder}/{raw_doc_name(r.primary_doc)}", ttl=0).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
        if not st or _key(st.name) in insiders:
            continue
        insiders[_key(st.name)] = {"name": st.name, "title": st.title, "shares": st.shares,
                                   "pct": st.shares / shares_out * 100 if shares_out and st.shares else None,
                                   "as_of": r.filed.isoformat(), "form": "4", "url": r.url,
                                   "director": st.director, "officer": st.officer, "ten_pct": st.ten_pct}
    return {"majors": [m for m in majors.values() if (m["pct"] or 0) >= 1 or m["pct"] is None],
            "insiders": sorted(insiders.values(), key=lambda x: -(x["shares"] or 0))}
