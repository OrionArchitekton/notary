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


# Prompt keys the provider permanently refuses to complete (content-filter
# 400, deterministic across attempts). Shared by the capture script (skip +
# exclude from the failure exit code) and the eval CLI (allow the fixture to
# be absent without failing the required-store gate). One entry:
# dim_customers.country_code "ISO-3166 alpha-2 country code." (2026-07-18).
KNOWN_UNCAPTURABLE = frozenset({"18a856fd6bfc4054c9a7798f"})


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
        record = json.loads(p.read_text())
        # Cycle-3 adversarial fix: bind the fixture to its prompt, not just
        # its filename. A fixture copied under another prompt's key would
        # otherwise silently supply the wrong completion and change the
        # published table.
        if record.get("user") != user:
            raise ValueError(
                f"fixture {p.name} does not match the requesting prompt: "
                f"stored user prompt differs (file moved or copied under the "
                f"wrong key?)"
            )
        return record["completion"]


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


# SYSTEM_PROMPT teaches normalized unit tokens that never occur verbatim in
# prose (fleet-review finding: the captured discount_pct completion carries
# "percent_0_100", which the verbatim entailment check can never match, so
# every percent claim was silently gate-dropped). Map each normalized token
# to the surface forms that entail it; a token not listed here still requires
# its own verbatim occurrence, so the anti-hallucination property is kept.
_UNIT_SURFACE_FORMS: dict[str, tuple[str, ...]] = {
    "percent_0_100": ("percent", "%"),
}

# percent_0_100 encodes a RANGE as well as a unit (pipeline finding: the
# surface word "percent" alone does not entail the 0-to-100 scale; the
# sentence must state the range or the LLM could invent it).
_PERCENT_RANGE_RE = re.compile(r"\b0\s*(?:and|to|-)\s*100\b", re.IGNORECASE)

# Stated surface forms that entail a never-null predicate / a min-of-zero
# bound. A predicate whose value is not stated in the claimed sentence is
# dropped, never probed. Cycle-3 hardening: matching is token-aware (plain
# substring admitted 'US' via 'USD' and '2' via '12'), negated forms are
# stripped before matching ('not required' must not entail never-null), and
# an enum needs closed-set phrasing (example wording like 'include' does not
# state an exhaustive set).
_NEVER_NULL_FORMS = (
    "never null", "not null", "non-null", "always populated", "required",
)
_NEGATED_FORM_RES = (
    re.compile(r"\b(?:not|never)\s+required\b"),
    re.compile(r"\bnot\s+always populated\b"),
)
_MIN_ZERO_FORMS = ("non-negative", "nonnegative", "never negative", ">= 0")
_CLOSED_SET_MARKERS = ("one of", "must be", "allowed values", "exactly", "{")


def _strip_negated_forms(low_text: str) -> str:
    for pattern in _NEGATED_FORM_RES:
        low_text = pattern.sub(" ", low_text)
    return low_text


def _token_stated(token: str, low_text: str) -> bool:
    """The token appears as a whole word, not a fragment of a longer one."""
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])", low_text
        )
    )


def _number_stated(bound: float, low_text: str) -> bool:
    """The literal number appears as a complete numeric token."""
    literal = str(int(bound)) if float(bound).is_integer() else str(bound)
    return bool(
        re.search(rf"(?<![0-9.]){re.escape(literal)}(?![0-9.])", low_text)
    )


def _predicate_ok(claim_type: ClaimType, predicate, text: str = "") -> bool:
    if not isinstance(predicate, dict):
        return False
    # Semantic entailment for string-valued predicates (review finding: a
    # completion can quote a real sentence but invent the predicate value):
    # the stated unit/cadence must literally appear in the claimed sentence.
    if claim_type is ClaimType.UNIT_SCALE:
        unit = predicate.get("unit")
        if not isinstance(unit, str):
            return False
        surface_forms = _UNIT_SURFACE_FORMS.get(unit.lower(), (unit,))
        if not any(f.upper() in text.upper() for f in surface_forms):
            return False
        if unit.lower() == "percent_0_100" and not _PERCENT_RANGE_RE.search(text):
            return False
    if claim_type is ClaimType.FRESHNESS:
        cadence = predicate.get("cadence")
        if not isinstance(cadence, str) or cadence.lower() not in text.lower():
            return False
    if claim_type is ClaimType.COMPLETENESS:
        nullable = predicate.get("nullable")
        if not isinstance(nullable, bool):
            return False
        # entailment (PR3 cycle-2 finding: a shape-only bool lets a
        # completion quote a claim-free sentence and fabricate
        # nullable:false, manufacturing CONTRADICTED verdicts): a never-null
        # predicate must be STATED in the claimed sentence, and a negated
        # form ('not required') states the opposite
        if nullable is False:
            stated = _strip_negated_forms(text.lower())
            if not any(form in stated for form in _NEVER_NULL_FORMS):
                return False
        return True
    if claim_type is ClaimType.DOMAIN_ENUM:
        low = text.lower()
        values = predicate.get("values")
        if (
            isinstance(values, list)
            and values
            and all(isinstance(v, (str, int, float)) for v in values)
        ):
            # closed-set phrasing required, and every claimed value must
            # appear as a whole token in the claimed sentence
            if not any(marker in low for marker in _CLOSED_SET_MARKERS):
                return False
            return all(_token_stated(str(v), low) for v in values)
        has_bound = False
        for key in ("min", "max"):
            bound = predicate.get(key)
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                continue
            has_bound = True
            stated = _number_stated(bound, low) or (
                key == "min"
                and bound == 0
                and any(form in low for form in _MIN_ZERO_FORMS)
            )
            if not stated:
                return False
        return has_bound
    if claim_type is ClaimType.DEPRECATION_USAGE:
        deprecated = predicate.get("deprecated")
        if not isinstance(deprecated, bool):
            return False
        # entailment: a deprecated:true predicate must be stated by
        # deprecation language, not fabricated onto ordinary text
        if deprecated is True and "deprecat" not in text.lower():
            return False
        return True
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
            if not isinstance(item, dict):
                raise ExtractionParseError(
                    f"completion array element is not an object: {item!r}"
                )
            try:
                claim_type = ClaimType(item["claim_type"])
            except (KeyError, ValueError) as e:
                raise ExtractionParseError(
                    f"bad claim_type in completion item: {item!r}"
                ) from e
            predicate = item.get("predicate", {})
            if not _grounded(item, description):
                continue
            if not _predicate_ok(claim_type, predicate, str(item.get("text", ""))):
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
