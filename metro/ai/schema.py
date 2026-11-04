"""Report-card schema. Claude must fill it exactly (structured outputs), so nothing is free-form."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Source = Literal["press_release", "filing", "transcript_prepared", "transcript_qa"]
Unit = Literal["USD", "USD_per_share", "percent", "count", "ratio", "other"]
Scale = Literal["units", "thousands", "millions", "billions"]

COVERAGE = {
    "revenue_sources": ("מקורות הכנסה: מגזרים, מוצרים וגיאוגרפיה", "segment note, MD&A"),
    "customer_concentration": ("ריכוזיות לקוחות (מעל 10%)", "revenue or concentration note"),
    "gaap_vs_non_gaap": ("פער GAAP מול Non-GAAP ומה הוחרג", "press release reconciliation"),
    "dilution_sbc": ("דילול ותגמול מבוסס מניות", "cash-flow statement, equity note"),
    "working_capital": ("הון חוזר: מלאי, לקוחות, הכנסות נדחות", "balance sheet, MD&A liquidity"),
    "backlog_rpo": ("צבר הזמנות / RPO וקצב ההכרה", "revenue note"),
    "debt_maturities": ("חוב: מועדי פירעון, ריבית, אמות מידה", "debt note"),
    "hidden_liabilities": ("התחייבויות רכש, ערבויות, התחייבויות מחוץ למאזן", "commitments note"),
    "risk_factor_changes": ("שינויים בגורמי הסיכון", "Part II Item 1A, language changes block"),
    "legal": ("הליכים משפטיים וחקירות", "Part II Item 1, contingencies note"),
    "capital_allocation": ("רכישה עצמית, דיבידנד ורכישות", "Part II Item 2, cash-flow statement"),
    "accounting_changes": ("שינויים חשבונאיים, ירידות ערך, ארגון מחדש, בקרות", "notes, Item 4"),
    "subsequent_events": ("אירועים אחרי תאריך המאזן", "subsequent events note"),
    "guidance": ("תחזית ההנהלה מול הקודמת", "press release, prepared remarks"),
    "kpis": ("מדדי פעילות ייחודיים לענף", "press release, MD&A, prepared remarks"),
}
CoverageKey = Literal[
    "revenue_sources", "customer_concentration", "gaap_vs_non_gaap", "dilution_sbc", "working_capital", "backlog_rpo",
    "debt_maturities", "hidden_liabilities", "risk_factor_changes", "legal", "capital_allocation", "accounting_changes",
    "subsequent_events", "guidance", "kpis",
]
MetricKey = Literal["revenue", "segment_revenue", "gross_margin", "operating_income", "operating_margin", "net_income",
                    "eps_diluted", "eps_non_gaap", "cfo", "fcf", "kpi", "other"]


class Citation(BaseModel):
    quote: str = Field(description="Exact English words copied from the source, 6 to 40 words. Never paraphrase.")
    source: Source
    section: str = Field(description="Where it appears, e.g. 'Item 2 MD&A', 'Note 4 Revenue', 'Q&A - Morgan Stanley'.")


class Metric(BaseModel):
    key: MetricKey
    label: str = Field(description="The line item as the company names it, in English.")
    value: Optional[float] = Field(description="The number as printed, before scaling. 35.1 for '$35.1 billion'.")
    scale: Scale
    unit: Unit
    period: str = Field(description="e.g. 'Q3 FY26' or 'nine months'.")
    yoy_pct: Optional[float] = Field(description="Change vs the same quarter last year, in percent, if stated or computable from the sources.")
    gaap: bool
    note_he: str = Field(description="Hebrew, one short sentence of context.")
    citation: Optional[Citation]


class GuidanceItem(BaseModel):
    metric: str
    period: str
    low: Optional[float]
    high: Optional[float]
    scale: Scale
    unit: Unit
    prior_low: Optional[float] = Field(description="The previous range for the same period, if the sources state it.")
    prior_high: Optional[float]
    direction: Literal["raised", "maintained", "lowered", "initiated", "withdrawn", "unknown"]
    note_he: str
    citation: Optional[Citation]


class CoverageItem(BaseModel):
    key: CoverageKey
    status: Literal["reported", "not_reported", "not_applicable"]
    severity: Literal["positive", "neutral", "watch", "red_flag"]
    finding_he: str = Field(description="Hebrew, 1-2 sentences with the numbers.")
    citations: list[Citation] = Field(description="Up to 3.")


class QAItem(BaseModel):
    analyst: Optional[str] = Field(description="Analyst and firm if named.")
    question_he: str
    answer_he: str
    numeric_answer: bool = Field(description="True only if management answered with a specific number.")
    evasive: bool = Field(description="True if management avoided the question or answered a different one.")
    citation: Optional[Citation]


class NewPromise(BaseModel):
    metric: str
    target_he: str = Field(description="What was promised, with the number.")
    deadline_he: str
    citation: Citation


class PromiseCheck(BaseModel):
    promise_id: int
    status: Literal["kept", "on_track", "missed", "revised", "not_mentioned"]
    evidence_he: str
    citation: Optional[Citation]


class RiskChange(BaseModel):
    kind: Literal["added", "removed", "modified"]
    summary_he: str
    citation: Optional[Citation]


class Catalyst(BaseModel):
    title_he: str = Field(description="Hebrew, what is expected: a launch, an investor or developer day, a regulatory decision, a deal closing, capacity coming online.")
    when_text: str = Field(description="The timing exactly as the sources state it, e.g. 'second half of fiscal 2027' or 'March 12'.")
    date: Optional[str] = Field(description="YYYY-MM-DD only when the sources give an exact day, else null.")
    month: Optional[str] = Field(description="YYYY-MM when the sources give a month or a quarter (the quarter's last month), else null.")
    kind: Literal["product", "event", "regulatory", "deal", "capacity", "other"]
    importance: Literal["high", "medium"]
    citation: Citation


class Flag(BaseModel):
    severity: Literal["red_flag", "watch", "positive"]
    text_he: str


class ReportCard(BaseModel):
    headline_he: str = Field(description="One Hebrew sentence: the single most important thing in this report.")
    what_happened_he: str = Field(description="Hebrew, 2-4 sentences: results vs expectations and vs own guidance.")
    metrics: list[Metric]
    what_next_he: str = Field(description="Hebrew, 2-3 sentences on guidance and outlook.")
    guidance_direction: Literal["raised", "maintained", "lowered", "initiated", "withdrawn", "none"]
    guidance: list[GuidanceItem]
    where_going_he: str = Field(description="Hebrew, 2-3 sentences on strategy, growth engines and risks.")
    growth_drivers_he: list[str]
    risks_he: list[str]
    coverage: list[CoverageItem] = Field(description="Exactly one item for each of the 15 checklist keys.")
    qa: list[QAItem]
    promises_new: list[NewPromise]
    promises_check: list[PromiseCheck]
    risk_changes: list[RiskChange]
    flags: list[Flag]
    catalysts: list[Catalyst] = Field(description="Forward-looking, time-bound events named in the sources that could move the stock. Empty if none.")


class LightCard(BaseModel):
    """Cheap first pass for companies outside the focus list (press release only)."""
    headline_he: str
    what_happened_he: str
    metrics: list[Metric]
    guidance_direction: Literal["raised", "maintained", "lowered", "initiated", "withdrawn", "none"]
    guidance: list[GuidanceItem]
    flags: list[Flag]


class ReviewIssue(BaseModel):
    kind: Literal["omission", "contradiction", "unsupported", "minor"]
    text_he: str
    citation: Optional[Citation]


class Review(BaseModel):
    verdict: Literal["pass", "fix"]
    issues: list[ReviewIssue]
