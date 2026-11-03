"""Filing text: HTML to text, split into legal sections, and compare with the previous filing."""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

# 10-Q: Part I items 1-4, Part II items 1, 1A, 2-6. 10-K: items 1, 1A, 1B, 1C, 2, 3, 5, 7, 7A, 8, 9A ...
TENQ_I = [("1", "financial_statements"), ("2", "mdna"), ("3", "market_risk"), ("4", "controls")]
TENQ_II = [("1", "legal"), ("1A", "risk_factors"), ("2", "repurchases"), ("3", "defaults"), ("5", "other"), ("6", "exhibits")]
TENK = [("1", "business"), ("1A", "risk_factors"), ("1B", "unresolved_comments"), ("1C", "cybersecurity"),
        ("2", "properties"), ("3", "legal"), ("5", "market_equity"), ("7", "mdna"), ("7A", "market_risk"),
        ("8", "financial_statements"), ("9A", "controls")]
LABELS_HE = {
    "financial_statements": "דוחות כספיים וביאורים", "mdna": "דברי ההנהלה (MD&A)", "market_risk": "סיכוני שוק",
    "controls": "בקרות", "legal": "הליכים משפטיים", "risk_factors": "גורמי סיכון", "repurchases": "רכישה עצמית",
    "defaults": "כשלי חוב", "other": "מידע אחר", "exhibits": "נספחים", "business": "תיאור העסק",
    "unresolved_comments": "הערות SEC פתוחות", "cybersecurity": "סייבר", "properties": "נכסים",
    "market_equity": "שוק המניה", "press_release": "הודעה לעיתונות", "transcript": "שיחת ועידה",
}


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        t.decompose()  # hidden inline-XBRL header blocks
    for tag in soup.find_all(["br"]):
        tag.replace_with("\n")
    for tag in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"]):
        tag.append("\n")
    for td in soup.find_all(["td", "th"]):
        td.append(" | ")
    text = soup.get_text()
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"( \| )+\n", "\n", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _item_re(num: str) -> re.Pattern[str]:
    n = num.replace(".", r"\.")
    return re.compile(rf"(?im)^\s*item\s*{n}(?![0-9A-Za-z])\s*[\.:\-\u2013\u2014]?\s*(?=[A-Z(])")


_ANY_ITEM = re.compile(r"(?im)^\s*item\s*\d")


def _split(text: str, spec: list[tuple[str, str]]) -> dict[str, str]:
    """Pick, for each item in order, the heading with the longest body.

    The table of contents repeats every heading with a one-line body, so the real
    heading is the one followed by the most text before the next heading.
    """
    heads = [m.start() for m in _ANY_ITEM.finditer(text)] + [len(text)]

    def body_len(pos: int) -> int:
        nxt = next((h for h in heads if h > pos + 3), len(text))
        return nxt - pos

    found: list[tuple[int, str]] = []
    cursor = 0
    for num, key in spec:
        cands = [m for m in _item_re(num).finditer(text) if m.start() >= cursor]
        if not cands:
            continue
        best = max(cands, key=lambda m: (body_len(m.start()), m.start()))
        found.append((best.start(), key))
        cursor = best.end()
    found.sort()
    out = {}
    for (pos, key), nxt in zip(found, found[1:] + [(len(text), "")]):
        body = text[pos:nxt[0]].strip()
        if len(body) > 40:
            out[key] = body
    return out


def split_10q(text: str) -> dict[str, str]:
    parts = list(re.finditer(r"(?im)^\s*part\s+ii\b", text))
    if parts:
        cut = parts[-1].start()  # the table of contents lists PART II first; the body heading comes last
        out = _split(text[:cut], TENQ_I)
        out.update(_split(text[cut:], TENQ_II))
        return out
    return _split(text, TENQ_I + TENQ_II)


def split_10k(text: str) -> dict[str, str]:
    return _split(text, TENK)


def split_8k(text: str) -> dict[str, str]:
    out = {}
    hits = list(re.finditer(r"(?im)^\s*item\s*(\d\.\d{2})\b", text))
    for m, nxt in zip(hits, hits[1:] + [None]):
        out[m.group(1)] = text[m.start(): nxt.start() if nxt else len(text)].strip()
    return out


def split_filing(form: str, text: str) -> dict[str, str]:
    f = form.upper()
    if f.startswith("10-Q"):
        return split_10q(text)
    if f.startswith("10-K"):
        return split_10k(text)
    if f.startswith("8-K"):
        return split_8k(text)
    return {"document": text}


# ---------------- comparison with the previous filing ----------------
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")
_NUM = re.compile(r"\$?\d[\d,]*(\.\d+)?%?")


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    return [s.strip() for s in _SENT.split(text) if len(s.strip()) > 25]


def _norm(s: str) -> str:
    """Ignore numbers and dates so 'Q2' -> 'Q3' or new amounts do not count as new language."""
    s = _NUM.sub("#", s.lower())
    s = re.sub(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", "<m>", s)
    return re.sub(r"\s+", " ", s).strip()


def cosine(a: str, b: str) -> float:
    ca, cb = Counter(re.findall(r"[a-z]{3,}", a.lower())), Counter(re.findall(r"[a-z]{3,}", b.lower()))
    dot = sum(ca[w] * cb[w] for w in ca)
    na, nb = math.sqrt(sum(v * v for v in ca.values())), math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class SectionDiff:
    section: str
    similarity: float
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def change_score(self) -> float:
        return round(1 - self.similarity, 4)

    def to_dict(self, limit: int = 25) -> dict:
        return {"section": self.section, "similarity": round(self.similarity, 4), "change_score": self.change_score,
                "added": self.added[:limit], "removed": self.removed[:limit],
                "changed": [{"before": a, "after": b} for a, b in self.changed[:limit]],
                "counts": {"added": len(self.added), "removed": len(self.removed), "changed": len(self.changed)}}


def diff_section(section: str, before: str, after: str) -> SectionDiff:
    a, b = sentences(before), sentences(after)
    na, nb = [_norm(x) for x in a], [_norm(x) for x in b]
    d = SectionDiff(section, cosine(before, after))
    sm = difflib.SequenceMatcher(a=na, b=nb, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        olds, news = list(range(i1, i2)), list(range(j1, j2))
        used = set()
        for j in news:  # pair each new sentence with its most similar removed one
            best, score = None, 0.0
            for i in olds:
                if i in used:
                    continue
                r = difflib.SequenceMatcher(a=na[i], b=nb[j]).ratio()
                if r > score:
                    best, score = i, r
            if best is not None and score >= 0.6:
                used.add(best)
                d.changed.append((a[best], b[j]))
            else:
                d.added.append(b[j])
        d.removed.extend(a[i] for i in olds if i not in used)
    return d


def word_changes(before: str, after: str) -> list[tuple[str, str]]:
    """Word-level replacements inside a changed sentence, e.g. ('strong', 'healthy')."""
    wa, wb = before.split(), after.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=wa, b=wb).get_opcodes():
        if op in ("replace", "delete", "insert"):
            old, new = " ".join(wa[i1:i2]), " ".join(wb[j1:j2])
            if _norm(old) != _norm(new):
                out.append((old, new))
    return out


def compare_filings(prev: dict[str, str], cur: dict[str, str],
                    keys: tuple[str, ...] = ("risk_factors", "mdna", "legal", "controls")) -> dict[str, dict]:
    out = {}
    for k in keys:
        if k in prev and k in cur:
            out[k] = diff_section(k, prev[k], cur[k]).to_dict()
    return out
