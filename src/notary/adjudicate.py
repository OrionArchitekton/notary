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
    if claim.claim_type is ClaimType.COMPLETENESS:
        return _adjudicate_completeness(claim, result)
    if claim.claim_type is ClaimType.FRESHNESS:
        return _adjudicate_freshness(claim, result)
    if claim.claim_type is ClaimType.DOMAIN_ENUM:
        return _adjudicate_domain_enum(claim, result)
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


# Never-null rubric: a claimed non-nullable column is contradicted by a
# material null share and confirmed only by a literal zero share over a
# meaningful sample. The band in between (a trace of nulls below the floor)
# falls to UNVERIFIABLE rather than either verdict.
_NULL_SHARE_CONTRADICTION_FLOOR = 0.01
_CONFIRM_MIN_ROWS = 100

_COMPLETENESS_RUBRIC_TEXT = (
    f"CONTRADICTED iff null_share >= {_NULL_SHARE_CONTRADICTION_FLOOR}; "
    f"CONFIRMED iff null_share == 0.0 and row_count >= {_CONFIRM_MIN_ROWS}; "
    f"otherwise UNVERIFIABLE"
)


def _adjudicate_completeness(claim: Claim, result: ProbeResult) -> Finding:
    # only a stated never-null claim is checkable by a null-share probe
    if claim.predicate.get("nullable") is not False:
        return _unverifiable_no_rubric(claim, result)
    m = result.measurements
    row_count = int(m.get("row_count") or 0)
    null_share = m.get("null_share")
    if not row_count or null_share is None:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence={"row_count": row_count, "probe_sql": result.spec.sql},
            rationale="no rows to measure; refusing to guess",
        )
    null_share = float(null_share)
    evidence = {
        "null_share": null_share,
        "row_count": row_count,
        "probe_sql": result.spec.sql,
        "rubric": _COMPLETENESS_RUBRIC_TEXT,
    }
    if null_share >= _NULL_SHARE_CONTRADICTION_FLOOR:
        return Finding(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            evidence=evidence,
            rationale=(
                f"described as never null but {null_share:.1%} of "
                f"{row_count} scanned rows are null"
            ),
        )
    if null_share == 0.0 and row_count >= _CONFIRM_MIN_ROWS:
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"no nulls in {row_count} scanned rows; consistent with the "
                f"never-null claim"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence=evidence,
        rationale=(
            f"null_share {null_share:.4f} over {row_count} rows sits below "
            f"the contradiction floor without proving zero; refusing to guess"
        ),
    )


def _adjudicate_domain_enum(claim: Claim, result: ProbeResult) -> Finding:
    m = result.measurements
    claimed_values = claim.predicate.get("values")
    if isinstance(claimed_values, list) and "observed_values" in m:
        claimed = {str(v) for v in claimed_values}
        if not claimed:
            # degenerate claim: an empty claimed set would mark every
            # observed value unexpected and manufacture false positives
            return _unverifiable_no_rubric(claim, result)
        observed = list(m["observed_values"])
        capped = bool(m.get("distinct_capped"))
        unexpected = sorted(set(observed) - claimed)
        evidence = {
            "claimed_values": sorted(claimed),
            "observed_values": observed,
            "unexpected_values": unexpected,
            "distinct_capped": capped,
            "probe_sql": result.spec.sql,
            "rubric": (
                "CONTRADICTED iff any observed distinct value is outside the "
                "claimed set; CONFIRMED iff the complete distinct set was "
                "observed (not capped), is non-empty, and is a subset of the "
                "claimed set; otherwise UNVERIFIABLE"
            ),
        }
        if unexpected:
            return Finding(
                claim=claim,
                verdict=Verdict.CONTRADICTED,
                evidence=evidence,
                rationale=(
                    f"claimed one of {sorted(claimed)} but observed "
                    f"{unexpected} in the data"
                ),
            )
        if observed and not capped:
            return Finding(
                claim=claim,
                verdict=Verdict.CONFIRMED,
                evidence=evidence,
                rationale=(
                    f"complete distinct set {observed} sits inside the "
                    f"claimed set"
                ),
            )
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                "distinct set empty or observation capped; a subset seen in "
                "a capped sample proves nothing; refusing to guess"
            ),
        )
    has_bound = any(
        isinstance(claim.predicate.get(k), (int, float)) for k in ("min", "max")
    )
    if has_bound and m.get("row_count"):
        observed_min = m.get("observed_min")
        observed_max = m.get("observed_max")
        if observed_min is None or observed_max is None:
            return _unverifiable_no_rubric(claim, result)
        claimed_min = claim.predicate.get("min")
        claimed_max = claim.predicate.get("max")
        violations = []
        if isinstance(claimed_min, (int, float)) and observed_min < claimed_min:
            violations.append(
                f"observed min {observed_min} below claimed min {claimed_min}"
            )
        if isinstance(claimed_max, (int, float)) and observed_max > claimed_max:
            violations.append(
                f"observed max {observed_max} above claimed max {claimed_max}"
            )
        evidence = {
            "claimed_min": claimed_min,
            "claimed_max": claimed_max,
            "observed_min": float(observed_min),
            "observed_max": float(observed_max),
            "row_count": int(m["row_count"]),
            "scan_limit": m.get("scan_limit"),
            "probe_sql": result.spec.sql,
            "rubric": (
                "CONTRADICTED iff an observed extremum violates a claimed "
                "bound; CONFIRMED iff all claimed bounds hold over the "
                "scanned sample; otherwise UNVERIFIABLE"
            ),
        }
        if violations:
            return Finding(
                claim=claim,
                verdict=Verdict.CONTRADICTED,
                evidence=evidence,
                rationale="; ".join(violations),
            )
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"observed range [{observed_min}, {observed_max}] respects "
                f"the claimed bounds over {int(m['row_count'])} scanned rows"
            ),
        )
    return _unverifiable_no_rubric(claim, result)


# Cadence rubric bands, in days of staleness measured against the probe's
# explicit as_of anchor: CONFIRMED at or below the confirm ceiling,
# CONTRADICTED at or above the contradiction floor, UNVERIFIABLE between
# (a cadence briefly missed is not yet a lie). Unknown cadences get no
# rubric.
_CADENCE_BANDS: dict[str, tuple[int, int]] = {
    # cadence: (confirm_ceiling_days, contradiction_floor_days)
    "hourly": (0, 2),
    "daily": (1, 7),
    "weekly": (7, 21),
}


def _adjudicate_freshness(claim: Claim, result: ProbeResult) -> Finding:
    cadence = str(claim.predicate.get("cadence", "")).lower()
    band = _CADENCE_BANDS.get(cadence)
    m = result.measurements
    if band is None or m.get("days_stale") is None:
        return _unverifiable_no_rubric(claim, result)
    confirm_ceiling, contradiction_floor = band
    days_stale = int(m["days_stale"])
    evidence = {
        "cadence_claimed": cadence,
        "days_stale": days_stale,
        "latest_value": m.get("latest_value"),
        "latest_column": m.get("latest_column"),
        "as_of": m.get("as_of"),
        "probe_sql": result.spec.sql,
        "rubric": (
            f"for cadence '{cadence}': CONFIRMED iff 0 <= days_stale <= "
            f"{confirm_ceiling}; CONTRADICTED iff days_stale >= "
            f"{contradiction_floor}; otherwise UNVERIFIABLE"
        ),
    }
    if days_stale >= contradiction_floor:
        return Finding(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            evidence=evidence,
            rationale=(
                f"described as {cadence} but the latest value "
                f"({m.get('latest_value')} in {m.get('latest_column')}) is "
                f"{days_stale} days old as of {m.get('as_of')}"
            ),
        )
    if 0 <= days_stale <= confirm_ceiling:
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"latest value is {days_stale} day(s) old as of "
                f"{m.get('as_of')}; consistent with the {cadence} cadence"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence=evidence,
        rationale=(
            f"{days_stale} days stale sits between the {cadence} confirm "
            f"ceiling ({confirm_ceiling}) and contradiction floor "
            f"({contradiction_floor}); refusing to guess"
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
