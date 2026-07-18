"""Claim extraction: catalog descriptions -> typed claims via the LLM boundary.

The LLM is used ONLY here (spec: Core loop step 2). Everything downstream is
deterministic. The boundary is a tiny protocol so tests replay captured
completions and production uses Anthropic with deterministic settings.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

from notary.types import Claim, ClaimType

SYSTEM_PROMPT = """You extract testable claims from data-catalog descriptions.

Given one column (or table) description, return a JSON array. Each element:
{"claim_type": one of ["unit_scale","freshness","completeness","domain_enum","deprecation_usage"],
 "text": the verbatim sentence from the description that states the claim,
 "predicate": a small object with the machine-checkable content}

predicate shapes by claim_type:
- unit_scale: {"unit": "<the stated unit, e.g. USD, milliseconds, grams, percent_0_100>"}
- freshness: {"cadence": "<stated cadence, e.g. daily, hourly, real-time>"}
- completeness: {"nullable": false}
- domain_enum: {"values": [..]} for enumerations, or {"min": 0} style bounds
- deprecation_usage: {"deprecated": true}

Rules: extract ONLY claims the description actually states. A description
with no testable claim returns []. Output the JSON array alone, no prose."""


class UnknownPromptError(KeyError):
    """Replay store has no completion for this exact prompt."""


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


def _prompt_key(system: str, user: str) -> str:
    return hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:24]


class ReplayLLM:
    """Strict replay of captured completions.

    Unknown prompt -> UnknownPromptError. Never fabricates: if the extractor's
    prompt drifts from what was captured, tests FAIL instead of green-washing.
    """

    def __init__(self, fixtures_dir: str | Path):
        self.dir = Path(fixtures_dir)

    def complete(self, system: str, user: str) -> str:
        p = self.dir / f"{_prompt_key(system, user)}.json"
        if not p.exists():
            raise UnknownPromptError(
                f"no captured completion for prompt key {p.stem} "
                f"(user prompt starts: {user[:80]!r})"
            )
        return json.loads(p.read_text())["completion"]


class AnthropicLLM:
    """Live extraction.

    Current Claude models reject sampling parameters (temperature/top_p), so
    run-to-run determinism is NOT promised here; tests and the frozen demo get
    determinism from strict replay of captured completions instead.
    """

    MODEL = "claude-opus-4-8"

    def __init__(self, model: str | None = None):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or self.MODEL

    def complete(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            raise ExtractionParseError("model declined the extraction request")
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        if not text_blocks:
            raise ExtractionParseError("completion contained no text block")
        return text_blocks[0]


class CaptureLLM:
    """Wraps a live client and writes replay fixtures with provenance."""

    def __init__(self, inner: LLMClient, fixtures_dir: str | Path, meta: dict):
        self.inner = inner
        self.dir = Path(fixtures_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = meta

    def complete(self, system: str, user: str) -> str:
        completion = self.inner.complete(system, user)
        p = self.dir / f"{_prompt_key(system, user)}.json"
        p.write_text(
            json.dumps(
                {"completion": completion, "user": user, "meta": self.meta},
                indent=1,
            )
        )
        return completion


class ExtractionParseError(ValueError):
    """The completion was not the strict JSON contract; surfaced, never
    silently treated as 'no claims' (a silent [] would hide missed lies)."""


def _user_prompt(asset_urn: str, field_path: str | None, description: str) -> str:
    target = f"column `{field_path}`" if field_path else "the table itself"
    return (
        f"Asset: {asset_urn}\nTarget: {target}\n"
        f"Description: {description}"
    )


def _parse_completion(raw: str) -> list[dict]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractionParseError(f"completion is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise ExtractionParseError("completion JSON is not an array")
    return data


# Machine-checkable predicate shapes per claim type. A completion item whose
# predicate does not fit its declared shape is DROPPED, never probed (review
# finding: an ungrounded or malformed claim could otherwise drive a false
# CONTRADICTED all the way into a catalog rewrite).
_PREDICATE_SHAPES: dict[ClaimType, tuple[tuple[str, type], ...]] = {
    ClaimType.UNIT_SCALE: (("unit", str),),
    ClaimType.FRESHNESS: (("cadence", str),),
    ClaimType.COMPLETENESS: (("nullable", bool),),
    ClaimType.DOMAIN_ENUM: (),  # values list OR numeric bound; checked below
    ClaimType.DEPRECATION_USAGE: (("deprecated", bool),),
}


def _grounded(item: dict, description: str) -> bool:
    """The claimed sentence must literally occur in the source description.

    This is the anti-hallucination / anti-injection gate: the LLM can only
    point at text the catalog actually contains, never invent a claim."""
    text = item.get("text")
    return isinstance(text, str) and bool(text.strip()) and text in description


def _predicate_ok(claim_type: ClaimType, predicate) -> bool:
    if not isinstance(predicate, dict):
        return False
    if claim_type is ClaimType.DOMAIN_ENUM:
        values = predicate.get("values")
        has_enum = isinstance(values, list) and all(
            isinstance(v, (str, int, float)) for v in values
        )
        has_bound = any(
            isinstance(predicate.get(k), (int, float)) for k in ("min", "max")
        )
        return has_enum or has_bound
    return all(
        isinstance(predicate.get(key), typ)
        for key, typ in _PREDICATE_SHAPES[claim_type]
    )


def extract_claims(
    asset_urn: str,
    descriptions: dict[str | None, str],
    llm: LLMClient,
) -> list[Claim]:
    """Extract typed claims from each described field (None key = table-level).

    Fail-closed filtering: only claims that are (a) a valid claim type, (b)
    grounded verbatim in the source description, and (c) carrying a
    well-shaped predicate survive. Everything else is dropped here so it can
    never reach probing or write-back."""
    claims: list[Claim] = []
    for field_path, description in descriptions.items():
        raw = llm.complete(
            SYSTEM_PROMPT, _user_prompt(asset_urn, field_path, description)
        )
        for item in _parse_completion(raw):
            try:
                claim_type = ClaimType(item["claim_type"])
            except (KeyError, ValueError) as e:
                raise ExtractionParseError(
                    f"bad claim_type in completion item: {item!r}"
                ) from e
            predicate = item.get("predicate", {})
            if not _grounded(item, description):
                continue
            if not _predicate_ok(claim_type, predicate):
                continue
            claims.append(
                Claim(
                    asset_urn=asset_urn,
                    field_path=field_path,
                    claim_type=claim_type,
                    text=str(item["text"]),
                    predicate=dict(predicate),
                )
            )
    return claims
