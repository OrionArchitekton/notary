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

_RECON_JOIN_FLOOR = 100

_RUBRIC_TEXT = (
    f"CONTRADICTED iff integer_share == {_CENTS_INTEGER_SHARE} and median > "
    f"{_CENTS_MEDIAN_FLOOR} AND a declared reconciliation corroborates "
    f"(>= {_RECON_JOIN_FLOOR} DISTINCT matched keys covering EVERY suspect "
    f"key, keys unique on both sides, every ratio at 100x, reference scan "
    f"complete); the distribution alone is suspicion and falls to "
    f"UNVERIFIABLE. CONFIRMED iff fractional_share >= "
    f"{_DOLLARS_FRACTIONAL_FLOOR} and 0 < median <= {_DOLLARS_MEDIAN_CEILING} "
    f"(fractional values are impossible under integer-cents storage, so the "
    f"dollars confirmation is earned by distribution); otherwise "
    f"UNVERIFIABLE; every verdict requires a complete scan (under the scan "
    f"limit)"
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
    if claim.claim_type is ClaimType.DEPRECATION_USAGE:
        return _adjudicate_deprecation(claim, result)
    return _unverifiable_no_rubric(claim, result)


# Percent (0-100 scale) rubric. PR5 review adjudication: stored-as-fraction
# vs rounded sub-1-percent TRUE percentages is scale-invariant (any 0-1
# distribution has a legitimate tiny-percent reading), so a
# distribution-only rubric must NEVER contradict; a [0, 1]-confined
# distribution falls to UNVERIFIABLE with the ambiguity stated. Only
# confirmation is reachable, from values that actually use the percent
# scale.
_PERCENT_RUBRIC_TEXT = (
    "CONFIRMED iff 0 <= min, median > 1, and max <= 100 over a complete "
    "scan (under the scan limit); a [0, 1]-confined distribution is "
    "scale-ambiguous (fraction vs sub-1-percent values) and falls to "
    "UNVERIFIABLE; contradiction is unreachable by design"
)


def _adjudicate_percent(claim: Claim, result: ProbeResult) -> Finding:
    m = result.measurements
    row_count = m.get("row_count") or 0
    if not row_count or m.get("median") is None or m.get("integer_share") is None:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence={"row_count": int(row_count), "probe_sql": result.spec.sql},
            rationale="no non-null values to measure; refusing to guess",
        )
    centi_share = m.get("centi_integer_share")
    if centi_share is None:
        return _unverifiable_no_rubric(claim, result)
    centi_share = float(centi_share)
    lo, hi = float(m["min"]), float(m["max"])
    median = float(m["median"])
    evidence = {
        "unit_claimed": "percent_0_100",
        "median": median,
        "min": lo,
        "max": hi,
        "centi_integer_share": centi_share,
        "row_count": int(row_count),
        "rows_scanned": int(m.get("rows_scanned", row_count)),
        "probe_sql": result.spec.sql,
        "rubric": _PERCENT_RUBRIC_TEXT,
    }
    if 0.0 <= lo and hi <= 1.0:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"values confined to [{lo:.3f}, {hi:.3f}] are "
                f"scale-ambiguous: a stored 0-to-1 fraction and legitimate "
                f"sub-1-percent values are indistinguishable by "
                f"distribution alone; refusing to guess"
            ),
        )
    # PR8 cycle-2 fix: completeness keys on rows SCANNED (raw rows read,
    # nulls included), mirroring the USD path; the non-null row_count can
    # dip under the limit while the scan was still capped.
    scan_limit = int(m.get("scan_limit") or 0)
    rows_scanned = int(m.get("rows_scanned", row_count))
    scanned_all = scan_limit > 0 and rows_scanned < scan_limit
    if 0.0 <= lo and median > 1.0 and hi <= 100.0 and scanned_all:
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"value distribution matches a 0-to-100 percent: median "
                f"{median:.2f} with max {hi:.2f} over the complete table"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence=evidence,
        rationale=(
            f"distribution (median {median:.4f}, range [{lo:.4f}, "
            f"{hi:.4f}]) matches neither the fraction signature nor the "
            f"percent scale; refusing to guess"
        ),
    )


def _adjudicate_unit_scale(claim: Claim, result: ProbeResult) -> Finding:
    unit = str(claim.predicate.get("unit", "")).upper()
    m = result.measurements
    if unit == "PERCENT_0_100" and m:
        return _adjudicate_percent(claim, result)
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
    # Overclaim-review fix (Codex C3) + PR8 pipeline fix: both unit verdicts
    # rest on universal distribution statements, so a scan that hit its cap
    # yields prefix statistics that support neither. The cap detector is
    # rows SCANNED (raw rows read, nulls included); the non-null row_count
    # can dip under the limit while the scan was still capped.
    scan_limit = m.get("scan_limit")
    rows_scanned = m.get("rows_scanned", row_count)
    if scan_limit is not None and int(rows_scanned) >= int(scan_limit):
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"scan capped at {int(scan_limit)} rows; prefix statistics "
                f"cannot support a universal unit verdict; refusing to guess"
            ),
        )
    if integer_share == _CENTS_INTEGER_SHARE and median > _CENTS_MEDIAN_FLOOR:
        # Rubric v2 (judge-review P0): the cents signature alone is
        # SUSPICION, not proof; legitimate whole-dollar amounts match it.
        # Contradiction must be corroborated by the operator-declared
        # reconciliation source measuring every joined key at 100x.
        recon_joined = m.get("recon_joined")
        if recon_joined is None:
            return Finding(
                claim=claim,
                verdict=Verdict.UNVERIFIABLE,
                evidence=evidence,
                rationale=(
                    f"every value is an integer with median {median:.0f}, "
                    f"consistent with integer cents, but distribution alone "
                    f"cannot prove the unit and no reconciliation source is "
                    f"declared; refusing to guess"
                ),
            )
        recon_share = float(m.get("recon_ratio_share") or 0.0)
        ref_scanned = int(m.get("recon_reference_rows_scanned") or 0)
        scan_limit_val = int(m.get("scan_limit") or 0)
        matched_keys = int(m.get("recon_matched_keys") or 0)
        suspect_keys = int(m.get("recon_suspect_keys") or 0)
        suspect_rows = int(m.get("recon_suspect_rows") or 0)
        evidence["recon_joined"] = int(recon_joined)
        evidence["recon_matched_keys"] = matched_keys
        evidence["recon_suspect_keys"] = suspect_keys
        evidence["recon_suspect_rows"] = suspect_rows
        evidence["recon_ratio_share"] = recon_share
        evidence["recon_reference_rows_scanned"] = ref_scanned
        # Corroboration is key-shaped, not row-shaped (PR #10 finding):
        # distinct matched keys clear the floor, every suspect key is
        # matched (full coverage), keys are unique on both sides (joined
        # rows == matched keys == suspect rows rules out fan-out and
        # duplicate suspect keys), every per-row ratio sits at 100x, and
        # the reference scan is complete.
        if (
            matched_keys >= _RECON_JOIN_FLOOR
            and matched_keys == suspect_keys
            and suspect_rows == suspect_keys
            and int(recon_joined) == matched_keys
            and recon_share == 1.0
            and 0 < ref_scanned < scan_limit_val
        ):
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
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"every value is an integer with median {median:.0f}, "
                f"consistent with integer cents, but the declared "
                f"reconciliation did not corroborate a 100x scale on every "
                f"suspect key (matched_keys={matched_keys}, "
                f"suspect_keys={suspect_keys}, joined={int(recon_joined)}, "
                f"ratio_share={recon_share:.2f}); refusing to guess"
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
    scan_limit = int(m.get("scan_limit") or 0)
    scanned_all = scan_limit > 0 and row_count < scan_limit
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
    if not scanned_all:
        # a capped prefix cannot prove a universal never-null claim
        # (PR3 adversarial finding); nulls found in the sample above still
        # contradict, but a clean sample proves nothing beyond itself
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"scan capped at {scan_limit} rows; a clean prefix cannot "
                f"confirm a universal never-null claim; refusing to guess"
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
            "rows_scanned": m.get("rows_scanned"),
            "scan_limit": m.get("scan_limit"),
            "probe_sql": result.spec.sql,
            "rubric": (
                "CONTRADICTED iff any observed distinct value is outside the "
                "claimed set; CONFIRMED iff the complete distinct set was "
                "observed (not capped) over a complete input scan (under the "
                "scan limit), is non-empty, and is a subset of the claimed "
                "set; otherwise UNVERIFIABLE"
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
        rows_scanned = int(m.get("rows_scanned") or 0)
        scan_limit = int(m.get("scan_limit") or 0)
        scanned_all = scan_limit > 0 and rows_scanned < scan_limit
        if observed and not capped and scanned_all:
            return Finding(
                claim=claim,
                verdict=Verdict.CONFIRMED,
                evidence=evidence,
                rationale=(
                    f"complete distinct set {observed} over all "
                    f"{rows_scanned} rows sits inside the claimed set"
                ),
            )
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                "distinct set empty, distinct observation capped, or input "
                "scan capped; a subset seen in a bounded sample proves "
                "nothing; refusing to guess"
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
            "prefix_rows": m.get("prefix_rows"),
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
        scan_limit = int(m.get("scan_limit") or 0)
        prefix_rows = int(m.get("prefix_rows") or 0)
        if not (scan_limit > 0 and prefix_rows < scan_limit):
            # a capped prefix cannot prove a universal bound claim
            return Finding(
                claim=claim,
                verdict=Verdict.UNVERIFIABLE,
                evidence=evidence,
                rationale=(
                    f"scan capped at {scan_limit} rows; an in-bounds prefix "
                    f"cannot confirm a universal bound; refusing to guess"
                ),
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


# Deprecation rubric: "no longer used" is contradicted by material recent
# query activity in the 30-day window before the anchor, and confirmed by a
# literally empty window (measured over a complete, bounded scan).
_RECENT_QUERY_CONTRADICTION_FLOOR = 10

_DEPRECATION_RUBRIC_TEXT = (
    f"over the 30 days up to and including as_of (later activity excluded): "
    f"CONTRADICTED iff recent_queries >= "
    f"{_RECENT_QUERY_CONTRADICTION_FLOOR}; CONFIRMED iff recent_queries == "
    f"0 over a complete log scan (under the scan limit); otherwise "
    f"UNVERIFIABLE"
)


def _adjudicate_deprecation(claim: Claim, result: ProbeResult) -> Finding:
    if claim.predicate.get("deprecated") is not True:
        return _unverifiable_no_rubric(claim, result)
    m = result.measurements
    if m.get("recent_queries") is None:
        return _unverifiable_no_rubric(claim, result)
    recent = int(m["recent_queries"])
    scan_limit = int(m.get("scan_limit") or 0)
    evidence = {
        "recent_queries": recent,
        "distinct_users": int(m.get("distinct_users") or 0),
        "as_of": result.spec.as_of,
        "log_rows_scanned": int(m.get("log_rows_scanned") or 0),
        "scan_limit": scan_limit,
        "probe_sql": result.spec.sql,
        "rubric": _DEPRECATION_RUBRIC_TEXT,
    }
    if recent >= _RECENT_QUERY_CONTRADICTION_FLOOR:
        return Finding(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            evidence=evidence,
            rationale=(
                f"described as no longer used but the query log shows "
                f"{recent} reads by {evidence['distinct_users']} user(s) in "
                f"the 30 days before {result.spec.as_of}"
            ),
        )
    log_rows_scanned = int(m.get("log_rows_scanned") or 0)
    window_rows_any = int(m.get("window_rows_any_table") or 0)
    evidence["window_rows_any_table"] = window_rows_any
    if window_rows_any == 0 and recent == 0:
        # an empty, new, or retention-truncated log shows no life inside
        # the window; its silence proves nothing (cycle-2 finding)
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"the query log shows no activity for ANY table in the 30 "
                f"days before {result.spec.as_of}; a dead or truncated log "
                f"cannot confirm silence; refusing to guess"
            ),
        )
    if recent == 0 and scan_limit > 0 and log_rows_scanned < scan_limit:
        return Finding(
            claim=claim,
            verdict=Verdict.CONFIRMED,
            evidence=evidence,
            rationale=(
                f"no queries in the 30 days before {result.spec.as_of} "
                f"over the complete log; consistent with the deprecation "
                f"claim"
            ),
        )
    if recent == 0:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"no matches in a log scan capped at {scan_limit} rows; a "
                f"silent prefix cannot confirm the deprecation claim; "
                f"refusing to guess"
            ),
        )
    return Finding(
        claim=claim,
        verdict=Verdict.UNVERIFIABLE,
        evidence=evidence,
        rationale=(
            f"{recent} recent query(ies) sits between silence and the "
            f"contradiction floor ({_RECENT_QUERY_CONTRADICTION_FLOOR}); "
            f"refusing to guess"
        ),
    )


# Cadence rubric bands, in days of staleness measured against the probe's
# explicit as_of anchor: CONFIRMED at or below the confirm ceiling,
# CONTRADICTED at or above the contradiction floor, UNVERIFIABLE between
# (a cadence briefly missed is not yet a lie). Unknown cadences get no
# rubric.
_CADENCE_BANDS: dict[str, tuple[int | None, int]] = {
    # cadence: (confirm_ceiling_days, contradiction_floor_days)
    # hourly confirm_ceiling is None: a date-granular anchor cannot prove an
    # hourly cadence (PR3 finding: a timestamp on the anchor day may still
    # be nearly 24h behind), so hourly confirmation is unreachable in v1
    "hourly": (None, 2),
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
    rows_scanned = int(m.get("rows_scanned") or 0)
    scan_limit = int(m.get("scan_limit") or 0)
    scanned_all = scan_limit > 0 and rows_scanned < scan_limit
    column_count = int(m.get("temporal_column_count") or 0)
    evidence = {
        "cadence_claimed": cadence,
        "days_stale": days_stale,
        "latest_value": m.get("latest_value"),
        "latest_column": m.get("latest_column"),
        "temporal_column_count": column_count,
        "as_of": m.get("as_of"),
        "rows_scanned": rows_scanned,
        "scan_limit": scan_limit,
        "probe_sql": result.spec.sql,
        "rubric": (
            f"for cadence '{cadence}': CONTRADICTED iff days_stale >= "
            f"{contradiction_floor} over a complete scan; CONFIRMED iff "
            f"0 <= days_stale <= {confirm_ceiling} over a complete scan of "
            f"a table with exactly one temporal column; otherwise "
            f"UNVERIFIABLE (a capped-scan max can understate staleness; "
            f"with multiple temporal columns nothing identifies the refresh "
            f"marker, and staleness of the max across ALL columns is only "
            f"an upper bound, valid for contradiction alone). "
            f"Interpretation: this probe verifies the table's temporal DATA "
            f"keeps pace with the claimed cadence; a pipeline that refreshes "
            f"without producing new temporal values, or a historical table "
            f"whose business dates are legitimately old, is outside this "
            f"probe's evidence"
        ),
    }
    if not scanned_all:
        # a prefix max can understate the true latest value, so neither
        # verdict is safe from a capped scan
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"scan capped at {scan_limit} rows; a prefix max cannot "
                f"support a freshness verdict; refusing to guess"
            ),
        )
    if days_stale >= contradiction_floor:
        return Finding(
            claim=claim,
            verdict=Verdict.CONTRADICTED,
            evidence=evidence,
            rationale=(
                f"described as {cadence} but even the newest temporal value "
                f"({m.get('latest_value')} in {m.get('latest_column')}) is "
                f"{days_stale} days old as of {m.get('as_of')}"
            ),
        )
    if confirm_ceiling is None:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"a date-granular anchor cannot prove the {cadence} "
                f"cadence; refusing to guess"
            ),
        )
    if column_count != 1:
        return Finding(
            claim=claim,
            verdict=Verdict.UNVERIFIABLE,
            evidence=evidence,
            rationale=(
                f"{column_count} temporal columns and nothing identifies "
                f"which is the refresh marker; a fresh business date must "
                f"not confirm cadence; refusing to guess"
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
