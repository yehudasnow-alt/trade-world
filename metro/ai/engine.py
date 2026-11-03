"""Calls Claude with a strict JSON schema (structured outputs), one at a time or as a 50%-cheaper batch."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import get_settings
from . import prompts as P
from .schema import LightCard, ReportCard, Review

# USD per million tokens: (input, output). Haiku 5.5 is priced higher above 100K prompt tokens.
PRICES = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "in_long": 0.50, "out_long": 2.50},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0},
}
LIMITS = {"card": {"press_release": 120_000, "filing": 420_000, "transcript_prepared": 90_000, "transcript_qa": 140_000},
          "light": {"press_release": 60_000}}
SCHEMAS: dict[str, type[BaseModel]] = {"card": ReportCard, "light": LightCard, "review": Review}


class AiError(RuntimeError):
    pass


@dataclass
class Sources:
    press_release: str = ""
    filing: str = ""
    filing_sections: str = ""
    transcript_prepared: str = ""
    transcript_qa: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"press_release": self.press_release, "filing": self.filing,
                "transcript_prepared": self.transcript_prepared, "transcript_qa": self.transcript_qa}


@dataclass
class Context:
    ticker: str
    name: str
    sub_industry: str
    period_label: str
    fiscal_note: str = ""
    xbrl_rows: list[dict] = field(default_factory=list)
    promises: list[dict] = field(default_factory=list)
    previous_card: dict | None = None
    diffs: dict = field(default_factory=dict)


def _format(kind: str) -> dict[str, Any]:
    from anthropic import transform_schema
    return {"format": {"type": "json_schema", "schema": transform_schema(SCHEMAS[kind])}}


def _prev_summary(card: dict | None) -> str:
    if not card:
        return "<previous_card status=\"none\"/>"
    keep = {k: card.get(k) for k in ("headline_he", "what_happened_he", "what_next_he", "guidance_direction")}
    keep["guidance"] = [{k: g.get(k) for k in ("metric", "period", "low", "high", "scale", "unit")} for g in card.get("guidance", [])]
    return "<previous_card>\n" + json.dumps(keep, ensure_ascii=False) + "\n</previous_card>"


def _system(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_card(ctx: Context, src: Sources, model: str | None = None, max_tokens: int = 16000) -> dict[str, Any]:
    lim = LIMITS["card"]
    head = (f"<company>{ctx.ticker} | {ctx.name} | {ctx.sub_industry}{' | ' + ctx.fiscal_note if ctx.fiscal_note else ''}</company>\n"
            f"<period>{ctx.period_label}</period>")
    body = "\n\n".join([
        head, P.xbrl_block(ctx.xbrl_rows), P.promises_block(ctx.promises), _prev_summary(ctx.previous_card),
        P.changes_block(ctx.diffs), P.checklist_block(),
        P.source_block("press_release", src.press_release, lim["press_release"]),
        P.source_block("filing", src.filing, lim["filing"], src.filing_sections),
        P.source_block("transcript_prepared", src.transcript_prepared, lim["transcript_prepared"]),
        P.source_block("transcript_qa", src.transcript_qa, lim["transcript_qa"]),
        "Fill the report card for this period.",
    ])
    return {"model": model or get_settings().ai_model_card, "max_tokens": max_tokens, "system": _system(P.SYSTEM_CARD),
            "messages": [{"role": "user", "content": body}], "output_config": _format("card")}


def build_light(ctx: Context, src: Sources, model: str | None = None) -> dict[str, Any]:
    body = (f"<company>{ctx.ticker} | {ctx.name}</company>\n<period>{ctx.period_label}</period>\n\n"
            + P.source_block("press_release", src.press_release or src.filing, LIMITS["light"]["press_release"]))
    return {"model": model or get_settings().ai_model_light, "max_tokens": 4000, "system": _system(P.SYSTEM_LIGHT),
            "messages": [{"role": "user", "content": body}], "output_config": _format("light")}


def build_review(ctx: Context, src: Sources, card: dict, model: str | None = None) -> dict[str, Any]:
    lim = LIMITS["card"]
    clean = json.loads(json.dumps(card))
    body = "\n\n".join([
        f"<company>{ctx.ticker} | {ctx.name}</company>", "<card>\n" + json.dumps(clean, ensure_ascii=False) + "\n</card>",
        P.source_block("press_release", src.press_release, lim["press_release"]),
        P.source_block("filing", src.filing, lim["filing"], src.filing_sections),
        P.source_block("transcript_qa", src.transcript_qa, lim["transcript_qa"]),
        "Review the card against the sources.",
    ])
    return {"model": model or get_settings().ai_model_review, "max_tokens": 4000, "system": _system(P.SYSTEM_REVIEW),
            "messages": [{"role": "user", "content": body}], "output_config": _format("review")}


def cost_usd(model: str, usage: dict[str, int], batch: bool = False) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    tin = usage.get("input_tokens", 0)
    cache_read, cache_write = usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
    long = "in_long" in p and (tin + cache_read + cache_write) > 100_000
    rin, rout = (p["in_long"], p["out_long"]) if long else (p["in"], p["out"])
    c = (tin * rin + cache_write * rin * 1.25 + cache_read * rin * 0.1 + usage.get("output_tokens", 0) * rout) / 1e6
    return round(c * (0.5 if batch else 1.0), 5)


def parse_message(msg: Any, kind: str) -> dict[str, Any]:
    if getattr(msg, "stop_reason", "") == "max_tokens":
        raise AiError("התשובה נקטעה (max_tokens). הגדל את המגבלה או קצר את המקורות.")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
        SCHEMAS[kind].model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise AiError(f"פלט לא תקין מהמודל: {e}") from e
    return data


def _usage(msg: Any) -> dict[str, int]:
    u = getattr(msg, "usage", None)
    if u is None:
        return {}
    return {k: getattr(u, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


class Engine:
    def __init__(self, client: Any = None):
        if client is None:
            key = get_settings().anthropic_api_key
            if not key:
                raise AiError("חסר ANTHROPIC_API_KEY. מפתח נוצר ב-console.anthropic.com.")
            import anthropic
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    def run(self, params: dict[str, Any], kind: str) -> tuple[dict, dict, float]:
        msg = self.client.messages.create(**params)
        usage = _usage(msg)
        return parse_message(msg, kind), usage, cost_usd(params["model"], usage)

    def submit(self, items: list[tuple[str, dict[str, Any]]]) -> str:
        batch = self.client.messages.batches.create(requests=[{"custom_id": cid, "params": p} for cid, p in items])
        return batch.id

    def status(self, batch_id: str) -> str:
        return self.client.messages.batches.retrieve(batch_id).processing_status

    def collect(self, batch_id: str, kinds: dict[str, str], models: dict[str, str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for entry in self.client.messages.batches.results(batch_id):
            cid, res = entry.custom_id, entry.result
            if res.type != "succeeded":
                out[cid] = {"error": f"{res.type}: {getattr(getattr(res, 'error', None), 'message', '')}"}
                continue
            try:
                usage = _usage(res.message)
                out[cid] = {"data": parse_message(res.message, kinds[cid]), "usage": usage,
                            "cost": cost_usd(models[cid], usage, batch=True)}
            except AiError as e:
                out[cid] = {"error": str(e)}
        return out


def estimate(params: dict[str, Any], batch: bool = False) -> dict[str, float]:
    """Rough cost before sending: ~4 characters per token, output assumed at 60% of max_tokens."""
    chars = sum(len(b["text"]) for b in params["system"]) + sum(len(m["content"]) for m in params["messages"])
    tin = chars / 4
    usage = {"input_tokens": int(tin), "output_tokens": int(params["max_tokens"] * 0.6)}
    return {"input_tokens": int(tin), "usd": cost_usd(params["model"], usage, batch)}
