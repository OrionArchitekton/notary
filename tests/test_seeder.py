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
