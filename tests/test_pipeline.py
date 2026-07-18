"""Slice 3: claim -> probe -> verdict, deterministically, against the seeded
warehouse.

The ONE behavior this locks: a unit-scale claim is planned into read-only SQL,
measured against the real warehouse, and adjudicated by a pure rubric into
CONTRADICTED (the planted cents lie), CONFIRMED (the truthful price control),
or UNVERIFIABLE (a claim Notary cannot probe), with evidence attached. This is
the S1 tracer bullet's deterministic core; without it every verdict is vibes.
"""
import duckdb
import pytest

from notary.adjudicate import adjudicate
from notary.demo.seeder import DEFAULT_SEED, build_warehouse
from notary.probe import plan_probe, run_probe
from notary.types import Claim, ClaimType, Verdict


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    db = tmp_path_factory.mktemp("wh") / "fiction_retail.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    yield con
    con.close()


def _claim(table: str, column: str, text: str, unit: str) -> Claim:
    return Claim(
        asset_urn=f"urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.{table},PROD)",
        field_path=column,
        claim_type=ClaimType.UNIT_SCALE,
        text=text,
        predicate={"unit": unit},
    )


def test_cents_lie_is_contradicted(warehouse):
    claim = _claim("fct_payments", "amount", "Transaction amount in USD.", "USD")
    spec = plan_probe(claim)
    assert "fct_payments" in spec.sql
    result = run_probe(spec, warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert finding.evidence["integer_share"] == 1.0
    assert finding.evidence["median"] > 1000
    assert finding.rationale


def test_truthful_price_control_is_confirmed(warehouse):
    claim = _claim("dim_products", "price_usd", "List price in USD.", "USD")
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.CONFIRMED


def test_unprobeable_claim_is_unverifiable(warehouse):
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        field_path="email",
        claim_type=ClaimType.UNIT_SCALE,
        text="Sourced from the billing system.",
        predicate={"unit": "furlongs"},  # no rubric for this unit in v1
    )
    spec = plan_probe(claim)
    finding = adjudicate(claim, run_probe(spec, warehouse))
    assert finding.verdict is Verdict.UNVERIFIABLE
    assert finding.rationale  # says WHY it could not be verified


def test_probe_failure_yields_unverifiable_not_a_crash(warehouse):
    claim = _claim("nonexistent_table", "amount", "Amount in USD.", "USD")
    result = run_probe(plan_probe(claim), warehouse)
    assert result.error is not None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_probe_sql_is_read_only(warehouse):
    claim = _claim("fct_payments", "amount", "Transaction amount in USD.", "USD")
    sql = plan_probe(claim).sql.lower()
    for banned in ("insert", "update", "delete", "drop", "create", "alter"):
        assert banned not in sql


def test_smallcents_distribution_is_unverifiable_not_confirmed(warehouse):
    """Review regression: a distribution matching NEITHER signature must fall
    to UNVERIFIABLE, never to CONFIRMED-by-complement (fail-open)."""
    claim = _claim("stg_inventory", "qty", "Value in USD.", "USD")
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_allnull_column_is_unverifiable_not_a_crash(tmp_path):
    """Review regression: an all-NULL probed column returns UNVERIFIABLE
    instead of raising TypeError on float(None)."""
    db = tmp_path / "nulls.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table t (amount double)")
    con.execute("insert into t values (NULL), (NULL)")
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = _claim("t", "amount", "Amount in USD.", "USD")
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_evidence_carries_probe_sql(warehouse):
    """Review regression: the dossier's probe SQL comes from evidence, so the
    finding must carry it."""
    claim = _claim("fct_payments", "amount", "Transaction amount in USD.", "USD")
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert "fct_payments" in finding.evidence["probe_sql"]
