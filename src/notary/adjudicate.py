"""Deterministic rubric adjudication: claim + measurements -> verdict.

Pure functions only. The LLM never overrides a probe measurement (spec: Core
loop step 4). UNVERIFIABLE is the fail-closed default: a claim only earns
CONFIRMED or CONTRADICTED when a rubric exists for it AND the probe ran.

v1 rubric coverage: unit_scale claims for currency-dollar units (USD). The
thresholds are the demo rubric, stated in the evidence so a reader can audit
the call; the S7 eval harness reports their false-positive/negative rates
honestly.
"""
from __future__ import annotations

from notary.types import Claim, ClaimType, Finding, ProbeResult, Verdict

# Cents-stored-as-dollars signature: every value is an integer AND the typical
# magnitude sits far above a plausible dollar median for row-level amounts.
_CENTS_INTEGER_SHARE = 1.0
_CENTS_MEDIAN_FLOOR = 1000


def adjudicate(claim: Claim, result: ProbeResult) -> Finding:
    if result.error is not None:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence={"probe_error": result.error},
            rationale=f"probe could not run: {result.error}",
        )
    if claim.claim_type is ClaimType.UNIT_SCALE:
        return _adjudicate_unit_scale(claim, result)
    return _unverifiable_no_rubric(claim, result)


def _adjudicate_unit_scale(claim: Claim, result: ProbeResult) -> Finding:
    unit = str(claim.predicate.get("unit", "")).upper()
    m = result.measurements
    if unit != "USD" or not m:
        return _unverifiable_no_rubric(claim, result)

    integer_share = float(m["integer_share"])
    median = float(m["median"])
    evidence = {
        "unit_claimed": unit,
        "median": median,
        "integer_share": integer_share,
        "min": float(m["min"]),
        "max": float(m["max"]),
        "row_count": int(m["row_count"]),
        "rubric": (
            f"cents-stored signature = integer_share == {_CENTS_INTEGER_SHARE} "
            f"and median > {_CENTS_MEDIAN_FLOOR}"
        ),
    }
    if integer_share == _CENTS_INTEGER_SHARE and median > _CENTS_MEDIAN_FLOOR:
        return Finding(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            evidence=evidence,
            rationale=(
                f"described as {unit} but every value is an integer with "
                f"median {median:.0f}; consistent with integer cents, not dollars"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.CONFIRMED,
        evidence=evidence,
        rationale=(
            f"value distribution is consistent with {unit} "
            f"(median {median:.2f}, integer_share {integer_share:.2f})"
        ),
    )


def _unverifiable_no_rubric(claim: Claim, result: ProbeResult) -> Finding:
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence={"measurements": dict(result.measurements)},
        rationale=(
            f"no v1 rubric for claim_type={claim.claim_type.value} "
            f"predicate={claim.predicate!r}; refusing to guess"
        ),
    )
