"""Deterministic rubric adjudication: claim + measurements -> verdict.

Pure functions only. The LLM never overrides a probe measurement (spec: Core
loop step 4). UNVERIFIABLE is the fail-closed default: CONFIRMED and
CONTRADICTED each require their own positive signature, and everything in
between falls to UNVERIFIABLE (review finding: a complement-of-the-lie
CONFIRMED is fail-open).

v1 rubric coverage: unit_scale claims for currency-dollar units (USD). The
thresholds are the demo rubric, stated in the evidence so a reader can audit
the call; the S7 eval harness reports their false-positive/negative rates
honestly. Known documented limitation: an all-integer dollar column with a
high median (e.g. whole-dollar invoices) matches the cents signature; the
written description never asserts a unit as fact, only the measurements.
"""
from __future__ import annotations

from notary.types import Claim, ClaimType, Finding, ProbeResult, Verdict

# Cents-stored-as-dollars signature: every value is an integer AND the typical
# magnitude sits far above a plausible dollar median for row-level amounts.
_CENTS_INTEGER_SHARE = 1.0
_CENTS_MEDIAN_FLOOR = 1000

# Positive dollars signature: a meaningful share of values carry cent
# fractions AND the typical magnitude is a plausible row-level dollar amount.
_DOLLARS_FRACTIONAL_FLOOR = 0.3
_DOLLARS_MEDIAN_CEILING = 1000

_RUBRIC_TEXT = (
    f"CONTRADICTED iff integer_share == {_CENTS_INTEGER_SHARE} and median > "
    f"{_CENTS_MEDIAN_FLOOR}; CONFIRMED iff fractional_share >= "
    f"{_DOLLARS_FRACTIONAL_FLOOR} and 0 < median <= {_DOLLARS_MEDIAN_CEILING}; "
    f"otherwise UNVERIFIABLE"
)


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

    row_count = m.get("row_count") or 0
    if not row_count or m.get("median") is None or m.get("integer_share") is None:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence={"row_count": int(row_count), "probe_sql": result.spec.sql},
            rationale="no non-null values to measure; refusing to guess",
        )

    integer_share = float(m["integer_share"])
    fractional_share = 1.0 - integer_share
    median = float(m["median"])
    evidence = {
        "unit_claimed": unit,
        "median": median,
        "integer_share": integer_share,
        "min": float(m["min"]),
        "max": float(m["max"]),
        "row_count": int(row_count),
        "probe_sql": result.spec.sql,
        "rubric": _RUBRIC_TEXT,
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
    if (
        fractional_share >= _DOLLARS_FRACTIONAL_FLOOR
        and 0 < median <= _DOLLARS_MEDIAN_CEILING
    ):
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"value distribution matches {unit}: median {median:.2f} in a "
                f"plausible dollar range with fractional_share "
                f"{fractional_share:.2f}"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence=evidence,
        rationale=(
            f"distribution matches neither the {unit} signature nor the "
            f"cents-stored signature (median {median:.2f}, integer_share "
            f"{integer_share:.2f}); refusing to guess"
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
