"""Probe planning and execution: deterministic, read-only SQL measurements.

A probe never mutates the warehouse (spec: Safety constraint). Planning is a
pure function of the claim; execution touches only the provided read-only
connection.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import duckdb

from notary.types import Claim, ClaimType, ProbeResult, ProbeSpec, Reconciliation

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

# Bounded scan (spec: Safety): probes measure at most this many non-null rows.
# Recorded in the evidence so a reader knows when measurements are partial.
SCAN_LIMIT = 100_000
# distinct-value observation cap for enum probes: a claimed enum whose
# distinct set exceeds this is never CONFIRMED from the capped sample
DISTINCT_CAP = 50


def _urn_table(asset_urn: str) -> str:
    """Extract the bare table name from a dataset urn's dotted name part."""
    m = re.search(r",([^,]+),[A-Z]+\)$", asset_urn)
    name = (m.group(1) if m else asset_urn).split(".")[-1]
    if not _IDENT.match(name):
        raise ValueError(f"unsafe table identifier from urn: {name!r}")
    return name


def _urn_dataset_name(asset_urn: str) -> str:
    """The full dotted dataset name from the urn (identifier-checked per
    component, safe for SQL literals)."""
    m = re.search(r",([^,]+),[A-Z]+\)$", asset_urn)
    name = m.group(1) if m else asset_urn
    for part in name.split("."):
        if not _IDENT.match(part):
            raise ValueError(f"unsafe dataset name from urn: {name!r}")
    return name


def plan_probe(
    claim: Claim,
    as_of: str | None = None,
    reconciliation: "Reconciliation | None" = None,
) -> ProbeSpec:
    """Plan the measurement SQL for a claim. Pure; never touches the DB."""
    table = _urn_table(claim.asset_urn)
    if claim.claim_type is ClaimType.UNIT_SCALE and claim.field_path:
        col = claim.field_path
        if not _IDENT.match(col):
            raise ValueError(f"unsafe column identifier: {col!r}")
        recon_select = ""
        recon_join = ""
        recon_keys: tuple[str, ...] = ()
        if reconciliation is not None:
            for ident in (
                reconciliation.table, reconciliation.suspect_key,
                reconciliation.reference_key, reconciliation.reference_column,
            ):
                if not _IDENT.match(ident):
                    raise ValueError(f"unsafe reconciliation identifier: {ident!r}")
            # Bounded join of the suspect prefix against the declared
            # reference prefix: per-key ratio suspect/reference. A true
            # cents-as-dollars load sits at exactly 100x the major-unit
            # reference; the tolerance absorbs IEEE 754 representation
            # noise ONLY (measured ulp-scale, <= 2e-14 on the demo
            # warehouse). A wider band would let a systematic business
            # discrepancy, e.g. a 0.5% settlement fee, corroborate a
            # false contradiction (PR #10 cycle-3 finding).
            # Key-shape measurements travel with the join (PR #10 finding:
            # joined ROWS alone can be inflated by duplicate keys or
            # fan-out, and say nothing about unmatched suspect keys); the
            # rubric requires distinct matched keys covering every suspect
            # key, uniquely, before corroboration counts.
            recon_join = (
                f', suspect as (select "{reconciliation.suspect_key}" as k, '
                f'"{col}" as v from "{table}" limit {SCAN_LIMIT})'
                f', recon as (select count(*) as recon_joined, '
                f'count(distinct s.k) as recon_matched_keys, '
                f'avg(case when r."{reconciliation.reference_column}" is null '
                f'then 0.0 when r."{reconciliation.reference_column}" = 0 '
                f'then 0.0 when abs(s.v / r."{reconciliation.reference_column}" '
                f'- 100.0) <= 1e-6 then 1.0 else 0.0 end) as recon_ratio_share '
                f'from suspect s '
                f'join (select "{reconciliation.reference_key}" as rk, '
                f'"{reconciliation.reference_column}" from '
                f'"{reconciliation.table}" limit {SCAN_LIMIT}) r on s.k = r.rk '
                f'where s.v is not null)'
                f', recon_suspect as (select count(distinct k) as '
                f'recon_suspect_keys, count(*) as recon_suspect_rows '
                f'from suspect where v is not null) '
            )
            recon_select = (
                ", recon.recon_joined, recon.recon_matched_keys, "
                "recon.recon_ratio_share, recon_suspect.recon_suspect_keys, "
                "recon_suspect.recon_suspect_rows, "
                f"(select count(*) from (select 1 from "
                f'"{reconciliation.table}" limit {SCAN_LIMIT})) '
                "as recon_reference_rows_scanned"
            )
            recon_keys = (
                "recon_joined", "recon_matched_keys", "recon_ratio_share",
                "recon_suspect_keys", "recon_suspect_rows",
                "recon_reference_rows_scanned",
            )
        # LIMIT bounds the rows READ, so the null filter sits OUTSIDE the
        # bounded subquery (cycle-3 finding: filtering first scans a sparse
        # table until it finds SCAN_LIMIT non-null values)
        # centi_integer_share: share of values that are exact multiples of
        # 0.01 (a 0-1-confined column of round percentages divided by 100
        # is the stored-as-fraction signature for percent claims)
        # PR8 pipeline fix: nulls stay IN the scanned set so rows_scanned
        # counts raw rows read (the cap detector), while the value stats
        # skip nulls via null-safe aggregates. A null inside a full-cap
        # prefix previously shrank the post-filter count under the limit
        # and defeated the complete-scan guard.
        sql = (
            f'with base as (select median(v) as median, '
            f'avg(case when v is null then null '
            f'when v = floor(v) then 1.0 else 0.0 end) as integer_share, '
            f'avg(case when v is null then null '
            f'when v * 100 = floor(v * 100) then 1.0 else 0.0 end) '
            f'as centi_integer_share, '
            f'min(v) as min, max(v) as max, count(v) as row_count, '
            f'count(*) as rows_scanned, '
            f'{SCAN_LIMIT} as scan_limit '
            f'from (select "{col}" as v from "{table}" limit {SCAN_LIMIT}))'
            f'{recon_join} '
            f'select base.*{recon_select} from base'
            f'{", recon, recon_suspect" if reconciliation is not None else ""}'
        )
        return ProbeSpec(
            claim=claim,
            sql=sql,
            measure_keys=(
                "median", "integer_share", "centi_integer_share",
                "min", "max", "row_count", "rows_scanned", "scan_limit",
            ) + recon_keys,
        )
    if claim.claim_type is ClaimType.COMPLETENESS and claim.field_path:
        col = claim.field_path
        if not _IDENT.match(col):
            raise ValueError(f"unsafe column identifier: {col!r}")
        # NOTE: unlike the unit probe, nulls must stay IN the scanned set;
        # the null share is the measurement.
        sql = (
            f'select count(*) as row_count, '
            f'avg(case when "{col}" is null then 1.0 else 0.0 end) as null_share, '
            f'{SCAN_LIMIT} as scan_limit '
            f'from (select "{col}" from "{table}" limit {SCAN_LIMIT})'
        )
        return ProbeSpec(
            claim=claim,
            sql=sql,
            measure_keys=("row_count", "null_share", "scan_limit"),
        )
    if claim.claim_type is ClaimType.DOMAIN_ENUM and claim.field_path:
        col = claim.field_path
        if not _IDENT.match(col):
            raise ValueError(f"unsafe column identifier: {col!r}")
        if isinstance(claim.predicate.get("values"), list):
            # the inner LIMIT bounds the rows READ (bot P1: an outer
            # distinct-limit alone caps output groups, not input scan); one
            # row past DISTINCT_CAP tells the runner the distinct set was
            # not fully observed
            sql = (
                f'select distinct v from '
                f'(select "{col}" as v from "{table}" limit {SCAN_LIMIT}) '
                f'where v is not null '
                f'limit {DISTINCT_CAP + 1}'
            )
            return ProbeSpec(
                claim=claim,
                sql=sql,
                measure_keys=(
                    "observed_values", "distinct_capped",
                    "rows_scanned", "scan_limit",
                ),
            )
        if any(
            isinstance(claim.predicate.get(k), (int, float)) for k in ("min", "max")
        ):
            sql = (
                f'select min(v) as observed_min, '
                f'max(v) as observed_max, count(v) as row_count, '
                f'(select count(*) from (select 1 from "{table}" '
                f'limit {SCAN_LIMIT})) as prefix_rows, '
                f'{SCAN_LIMIT} as scan_limit '
                f'from (select "{col}" as v from "{table}" limit {SCAN_LIMIT}) '
                f'where v is not null'
            )
            return ProbeSpec(
                claim=claim,
                sql=sql,
                measure_keys=(
                    "observed_min", "observed_max", "row_count",
                    "prefix_rows", "scan_limit",
                ),
            )
    if (
        claim.claim_type is ClaimType.DEPRECATION_USAGE
        and claim.predicate.get("deprecated") is True
        and claim.field_path is None  # table-level logs prove nothing
        and as_of is not None         # about a single column's usage
    ):
        # recent usage window anchored to as_of (never the wall clock),
        # bounded BELOW and ABOVE (post-anchor queries are not "the 30 days
        # before"); LIMIT bounds the log rows READ, with the filters outside
        # the bounded subquery; a warehouse without a query_log errors into
        # UNVERIFIABLE
        date.fromisoformat(as_of)  # defensive: literal goes into SQL
        # match both bare and schema-qualified log naming conventions;
        # window_rows_any_table proves the log was ALIVE inside the window
        # (an empty or retention-truncated log must not read as silence)
        window = (
            f"queried_at >= (date '{as_of}' - interval 29 day) "
            f"and queried_at < (date '{as_of}' + interval 1 day)"
        )
        sql = (
            f"select count(*) as recent_queries, "
            f"count(distinct query_user) as distinct_users, "
            f"(select count(*) from (select 1 from query_log "
            f"limit {SCAN_LIMIT})) as log_rows_scanned, "
            f"(select count(*) from (select queried_at from query_log "
            f"limit {SCAN_LIMIT}) where {window}) as window_rows_any_table, "
            f"{SCAN_LIMIT} as scan_limit "
            f"from (select table_name, queried_at, query_user "
            f"from query_log limit {SCAN_LIMIT}) "
            f"where table_name in "
            f"('{table}', '{_urn_dataset_name(claim.asset_urn)}') "
            f"and {window}"
        )
        return ProbeSpec(
            claim=claim,
            sql=sql,
            measure_keys=(
                "recent_queries", "distinct_users",
                "log_rows_scanned", "window_rows_any_table", "scan_limit",
            ),
            as_of=as_of,
        )
    if (
        claim.claim_type is ClaimType.FRESHNESS
        and isinstance(claim.predicate.get("cadence"), str)
        and as_of is not None
    ):
        # Stage 1 (this SQL): discover the table's date/timestamp columns.
        # Stage 2 (run_probe): take the max over each and measure staleness
        # against the explicit as_of anchor. No anchor -> no recipe: the
        # probe never silently reads the wall clock.
        sql = (
            f"select column_name from information_schema.columns "
            f"where table_name = '{table}' "
            f"and (data_type = 'DATE' or data_type like 'TIMESTAMP%')"
        )
        return ProbeSpec(
            claim=claim,
            sql=sql,
            measure_keys=(
                "latest_value", "latest_column", "date_columns",
                "days_stale", "as_of",
            ),
            as_of=as_of,
        )
    # No probe recipe for this claim shape in v1: empty SQL drives UNVERIFIABLE.
    return ProbeSpec(claim=claim, sql="", measure_keys=())


def run_probe(spec: ProbeSpec, con: duckdb.DuckDBPyConnection) -> ProbeResult:
    """Execute a planned probe against a read-only connection."""
    if not spec.sql:
        return ProbeResult(
            spec=spec, measurements={}, error="no probe recipe for this claim"
        )
    if spec.claim.claim_type is ClaimType.FRESHNESS and spec.as_of:
        return _run_freshness_probe(spec, con)
    if "observed_values" in spec.measure_keys:
        table = _urn_table(spec.claim.asset_urn)
        try:
            rows = con.execute(spec.sql).fetchall()
            rows_scanned = con.execute(
                f'select count(*) from (select 1 from "{table}" '
                f'limit {SCAN_LIMIT})'
            ).fetchone()[0]
        except duckdb.Error as e:
            return ProbeResult(spec=spec, measurements={}, error=str(e))
        values = sorted(str(r[0]) for r in rows[:DISTINCT_CAP])
        return ProbeResult(
            spec=spec,
            measurements={
                "observed_values": values,
                "distinct_capped": len(rows) > DISTINCT_CAP,
                "rows_scanned": int(rows_scanned),
                "scan_limit": SCAN_LIMIT,
            },
        )
    try:
        row = con.execute(spec.sql).fetchone()
    except duckdb.Error as e:
        return ProbeResult(spec=spec, measurements={}, error=str(e))
    measurements = dict(zip(spec.measure_keys, row))
    return ProbeResult(spec=spec, measurements=measurements)


def _run_freshness_probe(
    spec: ProbeSpec, con: duckdb.DuckDBPyConnection
) -> ProbeResult:
    """Two-stage read-only probe: discover date columns, then measure the
    most recent value across them against the spec's as_of anchor."""
    table = _urn_table(spec.claim.asset_urn)
    try:
        cols = [r[0] for r in con.execute(spec.sql).fetchall()]
        rows_scanned = con.execute(
            f'select count(*) from (select 1 from "{table}" '
            f'limit {SCAN_LIMIT})'
        ).fetchone()[0]
    except duckdb.Error as e:
        return ProbeResult(spec=spec, measurements={}, error=str(e))
    cols = [c for c in cols if _IDENT.match(c)]
    if not cols:
        return ProbeResult(
            spec=spec, measurements={},
            error=f"table {table} has no date or timestamp columns to measure",
        )
    latest_value = None
    latest_column = None
    try:
        for col in cols:
            # bounded input scan (bot P1: an unbounded max() per temporal
            # column violates the spec's bounded scan-cost constraint)
            value = con.execute(
                f'select max(m) from (select "{col}" as m from "{table}" '
                f'limit {SCAN_LIMIT})'
            ).fetchone()[0]
            if value is None:
                continue
            value = value.date() if isinstance(value, datetime) else value
            if latest_value is None or value > latest_value:
                latest_value, latest_column = value, col
    except duckdb.Error as e:
        return ProbeResult(spec=spec, measurements={}, error=str(e))
    if latest_value is None:
        return ProbeResult(
            spec=spec, measurements={},
            error=f"all date columns of {table} are entirely null",
        )
    days_stale = (date.fromisoformat(spec.as_of) - latest_value).days
    return ProbeResult(
        spec=spec,
        measurements={
            "latest_value": latest_value.isoformat(),
            "latest_column": latest_column,
            "date_columns": cols,
            "temporal_column_count": len(cols),
            "days_stale": days_stale,
            "as_of": spec.as_of,
            "rows_scanned": int(rows_scanned),
            "scan_limit": SCAN_LIMIT,
        },
    )
