"""Slice 1: the seeded demo warehouse is deterministic and its manifest is the
ground truth for S7 evaluation.

The ONE behavior this locks: `build_warehouse(path, seed)` produces a DuckDB
file whose contents are byte-for-byte reproducible for a given seed, and whose
manifest plants exactly the spec's lie set (12 lies spanning all five claim
types, plus truthful controls, including the S1 cents-lie centerpiece).
Without this, the S7 eval table and the frozen demo are not reproducible and
every downstream verdict is unanchored.
"""
import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from notary.demo.seeder import DEFAULT_SEED, build_warehouse
from notary.types import ClaimType


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("wh")
    db = root / "fiction_retail.duckdb"
    manifest = build_warehouse(db, seed=DEFAULT_SEED)
    return db, manifest


def _table_digest(db_path: Path) -> str:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        digest = hashlib.sha256()
        tables = [
            r[0]
            for r in con.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'main' order by table_name"
            ).fetchall()
        ]
        for t in tables:
            digest.update(t.encode())
            for row in con.execute(
                f'select * from "{t}" order by all'
            ).fetchall():
                digest.update(repr(row).encode())
        return digest.hexdigest()
    finally:
        con.close()


# Byte-frozen contract for the pre-existing tables: the recorded demo
# narrates median 12795 and the S5 captures quote its evidence, so ANY
# drift in these tables invalidates frozen artifacts. New tables may be
# appended (draws after all existing draws); these nine never move.
_FROZEN_LEGACY_SHAS = {
    "dim_customers": "e796ad8572a83512",
    "dim_products": "364f68b31eb0200d",
    "fct_orders": "39d64543b3ba5163",
    "fct_payments": "2a1ddf3526d4bebf",
    "fct_refunds": "a1fa0f2be90bd514",
    "fct_sessions_daily": "2f1e29d242edf83c",
    "legacy_orders_v1": "6b4b464d78473401",
    "query_log": "748a9435b8e97f98",
    "stg_inventory": "88d94d354f0e5aa3",
}


def test_legacy_tables_are_byte_frozen(built):
    db, _ = built
    con = duckdb.connect(str(db), read_only=True)
    try:
        for table, frozen in _FROZEN_LEGACY_SHAS.items():
            rows = con.execute(
                f'select * from "{table}" order by 1'
            ).fetchall()
            sha = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
            assert sha == frozen, f"{table} drifted from frozen bytes"
    finally:
        con.close()


def test_billing_is_the_canonical_source(built):
    """Judge-slice v3: the billing ledger is seeded FIRST in major
    currency units and the warehouse payments table is DERIVED from it
    by the buggy minor-units load, so the flagship reconciliation
    reference is not a copy of the suspect. The derived payments bytes
    stay frozen (see test_legacy_tables_are_byte_frozen)."""
    db, _ = built
    con = duckdb.connect(str(db), read_only=True)
    try:
        n, joined, exact, fractional = con.execute(
            "select (select count(*) from billing_invoices), count(*), "
            "sum(case when p.amount = cast(round(b.total_major * 100) as bigint) "
            "and p.currency = b.currency then 1 else 0 end), "
            "sum(case when b.total_major <> floor(b.total_major) then 1 else 0 end) "
            "from fct_payments p join billing_invoices b using (order_id)"
        ).fetchone()
        assert n == 2000 and joined == 2000 and exact == 2000
        assert fractional > 1000  # major units carry cent fractions
        legacy = con.execute(
            "select count(*) from information_schema.tables "
            "where table_name = 'stg_billing_totals'"
        ).fetchone()[0]
        assert legacy == 0  # replaced by the canonical billing ledger
    finally:
        con.close()


def test_seeder_is_deterministic(built, tmp_path):
    db, _ = built
    db2 = tmp_path / "again.duckdb"
    build_warehouse(db2, seed=DEFAULT_SEED)
    assert _table_digest(db) == _table_digest(db2)


def test_manifest_plants_the_spec_lie_set(built):
    _, manifest = built
    lies = [c for c in manifest.claims if c.planted_lie]
    controls = [c for c in manifest.claims if not c.planted_lie]
    assert len(lies) == 12
    assert {c.claim_type for c in lies} == set(ClaimType)
    assert len(controls) >= 4


def test_s1_cents_lie_is_planted(built):
    db, manifest = built
    cents = [
        c
        for c in manifest.claims
        if c.planted_lie
        and c.table == "fct_payments"
        and c.column == "amount"
        and c.claim_type is ClaimType.UNIT_SCALE
    ]
    assert len(cents) == 1
    assert "USD" in cents[0].description
    con = duckdb.connect(str(db), read_only=True)
    try:
        median, frac = con.execute(
            "select median(amount), "
            "avg(case when amount != floor(amount) then 1 else 0 end) "
            "from fct_payments"
        ).fetchone()
    finally:
        con.close()
    # stored as integer cents: no fractional part, magnitude far above USD range
    assert frac == 0
    assert median > 1000


def test_manifest_round_trips_to_json(built, tmp_path):
    _, manifest = built
    p = tmp_path / "manifest.json"
    p.write_text(manifest.to_json())
    loaded = json.loads(p.read_text())
    assert len(loaded["claims"]) == len(manifest.claims)


def test_inventory_negative_qty_lie_is_actually_planted(built):
    """Review regression: the manifest promised negative oversell values but
    the default seed happened to generate none; the plant is now forced."""
    db, _ = built
    con = duckdb.connect(str(db), read_only=True)
    try:
        negatives = con.execute(
            "select count(*) from stg_inventory where qty < 0"
        ).fetchone()[0]
    finally:
        con.close()
    assert negatives >= 3


def test_sessions_rollup_dates_are_distinct_and_daily(built):
    """Bot-thread regression: the daily rollup must have one row per date."""
    db, _ = built
    con = duckdb.connect(str(db), read_only=True)
    try:
        total, distinct, latest = con.execute(
            "select count(*), count(distinct event_date), max(event_date) "
            "from fct_sessions_daily"
        ).fetchone()
    finally:
        con.close()
    assert total == distinct == 90
    assert str(latest) == "2026-05-28"
