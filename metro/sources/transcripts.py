"""Earnings-call transcripts.

Order of preference:
  1. A text file you saved: data/transcripts/<TICKER>/<YYYY>Q<n>.txt (from the company's IR site PDF).
  2. FMP API, when FMP_API_KEY is set (transcripts are in the Ultimate plan).
Returns None when neither is available; the report card then marks the call stage as missing.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from ..config import get_settings


def local_path(ticker: str, year: int, quarter: int) -> Path:
    return get_settings().data_dir / "transcripts" / ticker / f"{year}Q{quarter}.txt"


def get_transcript(ticker: str, year: int, quarter: int, transport: httpx.BaseTransport | None = None) -> str | None:
    p = local_path(ticker, year, quarter)
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    key = get_settings().fmp_api_key
    if not key:
        return None
    with httpx.Client(timeout=30, transport=transport) as http:
        r = http.get("https://financialmodelingprep.com/stable/earning-call-transcript",
                     params={"symbol": ticker, "year": year, "quarter": quarter, "apikey": key})
    if r.status_code != 200:
        return None
    js = r.json()
    rows = js if isinstance(js, list) else [js]
    text = "\n\n".join(x.get("content", "") for x in rows if isinstance(x, dict))
    if text.strip():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return text
    return None


def split_qa(text: str) -> tuple[str, str]:
    """Prepared remarks vs Q&A, split at the operator's first question cue."""
    low = text.lower()
    cues = ["question-and-answer session", "questions and answers", "we will now begin the question",
            "first question comes from", "our first question", "operator instructions"]
    pos = min((low.find(c) for c in cues if low.find(c) > len(text) * 0.15), default=-1)
    return (text[:pos], text[pos:]) if pos > 0 else (text, "")
