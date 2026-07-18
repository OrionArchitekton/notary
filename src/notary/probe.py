"""Probe planning and execution: deterministic, read-only SQL measurements.

A probe never mutates the warehouse (spec: Safety constraint). Planning is a
pure function of the claim; execution touches only the provided read-only
connection.
"""
from __future__ import annotations

import re

import duckdb

from notary.types import Claim, ClaimType, ProbeResult, ProbeSpec

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

# Bounded scan (spec: Safety): probes measure at most this many non-null rows.
# Recorded in the evidence so a reader knows when measurements are partial.
SCAN_LIMIT = 100_000


def _urn_table(asset_urn: str) -> str:
    """Extract the bare table name from a dataset urn's dotted name part."""
    m = re.search(r",([^,]+),[A-Z]+\)$", asset_urn)
    name = (m.group(1) if m else asset_urn).split(".")[-1]
    if not _IDENT.match(name):
        raise ValueError(f"unsafe table identifier from urn: {name!r}")
    return name


def plan_probe(claim: Claim) -> ProbeSpec:
    """Plan the measurement SQL for a claim. Pure; never touches the DB."""
    table = _urn_table(claim.asset_urn)
    if claim.claim_type is ClaimType.UNIT_SCALE and claim.field_path:
        col = claim.field_path
        if not _IDENT.match(col):
            raise ValueError(f"unsafe column identifier: {col!r}")
        sql = (
            f'select median(v) as median, '
            f'avg(case when v = floor(v) then 1.0 else 0.0 end) as integer_share, '
            f'min(v) as min, max(v) as max, count(*) as row_count, '
            f'{SCAN_LIMIT} as scan_limit '
            f'from (select "{col}" as v from "{table}" '
            f'where "{col}" is not null limit {SCAN_LIMIT})'
        )
        return ProbeSpec(
            claim=claim,
            sql=sql,
            measure_keys=(
                "median", "integer_share", "min", "max", "row_count", "scan_limit"
            ),
        )
    # No probe recipe for this claim shape in v1: empty SQL drives UNVERIFIABLE.
    return ProbeSpec(claim=claim, sql="", measure_keys=())


def run_probe(spec: ProbeSpec, con: duckdb.DuckDBPyConnection) -> ProbeResult:
    """Execute a planned probe against a read-only connection."""
    if not spec.sql:
        return ProbeResult(
            spec=spec, measurements={}, error="no probe recipe for this claim"
        )
    try:
        row = con.execute(spec.sql).fetchone()
    except duckdb.Error as e:
        return ProbeResult(spec=spec, measurements={}, error=str(e))
    measurements = dict(zip(spec.measure_keys, row))
    return ProbeResult(spec=spec, measurements=measurements)
