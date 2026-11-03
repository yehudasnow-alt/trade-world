"""Check a report card against its sources: every quote must be found, every number must match XBRL or the text."""
from __future__ import annotations

import difflib
import re
from typing import Any

SCALE = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
WORD_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12, "k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9}
XBRL_KEY = {"revenue": "revenue", "operating_income": "operating_income", "net_income": "net_income",
            "eps_diluted": "eps_diluted", "cfo": "cfo", "fcf": "fcf", "gross_margin": "gross_margin",
            "operating_margin": "op_margin"}
_NUM = re.compile(r"(?<![\w.])\(?-?\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\)?\s*(%|percent|trillion|billion|million|thousand|bn|mm|b|m|k)?\b",
                  re.I)


def normalize(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("\xa0", " ").replace("|", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


class SourceIndex:
    def __init__(self, sources: dict[str, str]):
        self.raw = sources
        self.norm = {k: normalize(v) for k, v in sources.items() if v}
        self.all = " \n ".join(self.norm.values())
        self.tokens = self.all.split(" ")
        self._numbers: list[float] | None = None

    def find_quote(self, quote: str, source: str | None = None) -> str:
        q = normalize(quote).strip(" .\"'")
        if len(q) < 12:
            return "missing"
        hay = self.norm.get(source or "", "") or self.all
        if q in hay or q in self.all:
            return "exact"
        qt = q.split(" ")
        anchors = [i for i, t in enumerate(self.tokens) if t == qt[0]] + [i - len(qt) + 1 for i, t in enumerate(self.tokens) if t == qt[-1]]
        best = 0.0
        for i in anchors[:400]:
            if i < 0:
                continue
            window = " ".join(self.tokens[i:i + len(qt) + 2])
            best = max(best, difflib.SequenceMatcher(a=q, b=window[:len(q) + 20]).ratio())
            if best > 0.97:
                break
        return "close" if best >= 0.9 else "missing"

    def numbers(self) -> list[float]:
        if self._numbers is None:
            out = []
            for m in _NUM.finditer(self.all):
                base = float(m.group(1).replace(",", "") + (m.group(2) or ""))
                word = (m.group(3) or "").lower()
                if word in ("%", "percent"):
                    out.append(base)
                elif word in WORD_SCALE:
                    out.append(base * WORD_SCALE[word])
                else:  # table cells: scale comes from the column header, try the usual ones
                    out += [base, base * 1e3, base * 1e6]
            self._numbers = out
        return self._numbers


def _tolerance(value: float, scale: float) -> float:
    s = repr(abs(value))
    decimals = len(s.split(".")[1].rstrip("0")) if "." in s and "e" not in s else 0
    return max(abs(value * scale) * 0.006, 0.5 * 10 ** (-decimals) * scale)


def check_number(value: float, scale_name: str, unit: str, key: str | None, idx: SourceIndex,
                 xbrl: dict[str, float] | None) -> str:
    scale = 1.0 if unit in ("percent", "USD_per_share", "ratio") else SCALE.get(scale_name, 1.0)
    absval = value * scale
    tol = _tolerance(value, scale)
    if xbrl and key in XBRL_KEY and XBRL_KEY[key] in xbrl:
        if abs(abs(xbrl[XBRL_KEY[key]]) - abs(absval)) <= tol:
            return "xbrl"
    for n in idx.numbers():
        if abs(n - abs(absval)) <= tol:
            return "text"
    return "unverified"


def _walk_citations(card: dict) -> list[dict]:
    found = []

    def rec(x: Any) -> None:
        if isinstance(x, dict):
            if "quote" in x and "source" in x:
                found.append(x)
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
    rec(card)
    return found


def verify_card(card: dict, sources: dict[str, str], xbrl_latest: dict[str, float] | None = None) -> dict:
    """Annotates the card in place (`_q` on citations, `_n` on numbers) and returns a summary."""
    idx = SourceIndex(sources)
    q_stats = {"exact": 0, "close": 0, "missing": 0}
    for c in _walk_citations(card):
        st = idx.find_quote(c.get("quote", ""), c.get("source"))
        c["_q"] = st
        q_stats[st] += 1
    n_stats = {"xbrl": 0, "text": 0, "unverified": 0}
    problems = []
    for m in card.get("metrics", []):
        if m.get("value") is None:
            continue
        st = check_number(m["value"], m.get("scale", "units"), m.get("unit", "USD"), m.get("key"), idx, xbrl_latest)
        m["_n"] = st
        n_stats[st] += 1
        if st == "unverified":
            problems.append(f"מספר שלא נמצא במקור: {m.get('label')} = {m['value']} {m.get('scale', '')}")
    for g in card.get("guidance", []):
        for k in ("low", "high"):
            if g.get(k) is not None:
                st = check_number(g[k], g.get("scale", "units"), g.get("unit", "USD"), None, idx, None)
                g[f"_n_{k}"] = st
                n_stats[st] += 1
                if st == "unverified":
                    problems.append(f"טווח תחזית שלא נמצא במקור: {g.get('metric')} {k}={g[k]}")
    missing_q = [c["quote"][:80] for c in _walk_citations(card) if c.get("_q") == "missing"]
    problems += [f"ציטוט שלא נמצא במקור: \"{q}\"" for q in missing_q[:10]]
    covered = {c.get("key") for c in card.get("coverage", [])}
    total_q = sum(q_stats.values()) or 1
    total_n = sum(n_stats.values()) or 1
    return {"quotes": q_stats, "numbers": n_stats, "coverage_keys": len(covered), "problems": problems,
            "quote_ok_pct": round((q_stats["exact"] + q_stats["close"]) / total_q * 100, 1),
            "number_ok_pct": round((n_stats["xbrl"] + n_stats["text"]) / total_n * 100, 1)}
