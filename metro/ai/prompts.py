"""Prompts for the report engine. English instructions, Hebrew narrative fields, verbatim English quotes."""
from __future__ import annotations

from .schema import COVERAGE

PROMPT_VERSION = "2026-11-b"

SYSTEM_CARD = """You are a meticulous equity research analyst preparing a quarterly report card for one private investor.

Rules:
1. Use only the sources inside <source> tags and the <xbrl> block. Never use outside knowledge about the company or its results.
2. Every number must appear in the sources exactly as printed. Give "value" as printed and the printed "scale" (35.1 with "billions").
3. Every citation "quote" is copied word for word from the source, 6 to 40 words, no ellipsis, no paraphrase.
4. Go through all 15 checklist keys. If a topic is not in the sources, set status "not_reported" and say so. Do not guess.
5. Compare with the previous quarter's card and the open promises when they are given. Check each open promise by id.
6. Use the <language_changes> block for risk_factor_changes; the most decision-relevant new or removed sentences matter most.
7. Write every *_he field in clear Hebrew. Keep financial terms in English in parentheses the first time, e.g. "תזרים חופשי (FCF)".
8. In the Q&A section, prefer the hardest questions. Mark numeric_answer only when management gave a specific figure, and evasive when they avoided the question.
9. You describe what the documents say. You do not give investment advice or price targets.
10. In catalysts, list every forward-looking, time-bound event the sources name: product launches, investor or developer days,
    regulatory decisions, deal closings, capacity or plants coming online. Only what the sources state, with the timing as printed.
"""

SYSTEM_LIGHT = """You summarize a company's earnings press release for an investor dashboard.
Use only the source text. Numbers exactly as printed with their printed scale. Quotes word for word.
Hebrew for *_he fields. No investment advice."""

SYSTEM_REVIEW = """You are the second reader of a quarterly report card. You did not write it.
Compare the card with the sources. List omissions (material facts in the sources that the card misses),
contradictions (the card says something the sources do not), and unsupported claims.
Ignore style. Hebrew for text_he. Quote the source word for word when you cite it.
Verdict "pass" only if there is no omission or contradiction that would change an investor's view."""


def checklist_block() -> str:
    rows = [f"- {k}: {he} (look in: {where})" for k, (he, where) in COVERAGE.items()]
    return "<checklist>\n" + "\n".join(rows) + "\n</checklist>"


def source_block(name: str, text: str, limit: int, sections: str = "") -> str:
    if not text:
        return f'<source name="{name}" status="missing"/>'
    cut = text if len(text) <= limit else text[:limit] + "\n[truncated]"
    attr = f' sections="{sections}"' if sections else ""
    return f'<source name="{name}"{attr}>\n{cut}\n</source>'


def xbrl_block(rows: list[dict]) -> str:
    if not rows:
        return "<xbrl status=\"missing\"/>"
    keys = ["revenue", "gross_margin", "operating_income", "op_margin", "net_income", "eps_diluted", "cfo", "fcf",
            "sbc", "cash", "debt_total", "rpo", "inventory", "receivables", "diluted_shares"]
    head = "period | " + " | ".join(keys)
    lines = [head]
    for r in rows:
        v = r["values"]
        cells = []
        for k in keys:
            x = v.get(k)
            cells.append("" if x is None else (f"{x:.2f}" if abs(x) < 1000 else f"{x:,.0f}"))
        lines.append(f"{r['label']} | " + " | ".join(cells))
    return "<xbrl note=\"From SEC XBRL, in USD (margins in %). Latest quarter first.\">\n" + "\n".join(lines) + "\n</xbrl>"


def promises_block(promises: list[dict]) -> str:
    if not promises:
        return "<open_promises/>"
    rows = [f'- id={p["id"]}: {p["metric"]}: {p["target"]} by {p["deadline"]} (quote: "{p["quote"]}")' for p in promises]
    return "<open_promises>\n" + "\n".join(rows) + "\n</open_promises>"


def changes_block(diffs: dict) -> str:
    if not diffs:
        return "<language_changes status=\"no previous filing\"/>"
    out = []
    for sec, d in diffs.items():
        out.append(f'<section name="{sec}" similarity="{d["similarity"]}">')
        out += [f"+ {s}" for s in d["added"][:15]]
        out += [f"- {s}" for s in d["removed"][:15]]
        out += [f"~ {c['before']}  =>  {c['after']}" for c in d["changed"][:15]]
        out.append("</section>")
    return "<language_changes>\n" + "\n".join(out) + "\n</language_changes>"
