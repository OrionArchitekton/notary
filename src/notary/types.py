"""Core domain types for the Notary claim pipeline.

The pipeline is: catalog metadata -> Claim -> ProbeSpec -> ProbeResult ->
Finding (verdict + evidence). Adjudication is deterministic given probe
results; the LLM only extracts claims (spec: Core loop, steps 2-4).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ClaimType(enum.Enum):
    UNIT_SCALE = "unit_scale"
    FRESHNESS = "freshness"
    COMPLETENESS = "completeness"
    DOMAIN_ENUM = "domain_enum"
    DEPRECATION_USAGE = "deprecation_usage"


class Verdict(enum.Enum):
    CONFIRMED = "CONFIRMED"
    CONTRADICTED = "CONTRADICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class Claim:
    """One discrete, testable assertion extracted from catalog metadata."""

    asset_urn: str
    field_path: str | None  # None = table-level claim
    claim_type: ClaimType
    text: str  # the verbatim metadata sentence the claim came from
    predicate: dict  # typed payload, e.g. {"unit": "USD"} or {"values": [...]}


@dataclass(frozen=True)
class ProbeSpec:
    """A deterministic measurement plan for one claim."""

    claim: Claim
    sql: str  # read-only SQL against the warehouse
    measure_keys: tuple[str, ...]  # names the probe must return


@dataclass(frozen=True)
class ProbeResult:
    spec: ProbeSpec
    measurements: dict  # measure_key -> value
    error: str | None = None  # probe could not run (drives UNVERIFIABLE)


@dataclass(frozen=True)
class Finding:
    claim: Claim
    verdict: Verdict
    evidence: dict = field(default_factory=dict)  # measured values + thresholds
    rationale: str = ""  # one-sentence deterministic rubric explanation
