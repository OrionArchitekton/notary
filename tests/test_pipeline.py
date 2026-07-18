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


def test_probe_scan_is_bounded(warehouse):
    """Pipeline-review regression (FR3/FR11): probe SQL carries an explicit
    row bound and records it in the measurements."""
    claim = _claim("fct_payments", "amount", "Transaction amount in USD.", "USD")
    spec = plan_probe(claim)
    assert "limit" in spec.sql.lower()
    result = run_probe(spec, warehouse)
    assert result.measurements["scan_limit"] > 0
    assert result.measurements["row_count"] <= result.measurements["scan_limit"]


def test_never_null_lie_is_contradicted(warehouse):
    """Rubrics slice 1 (completeness): the ONE behavior this locks - a
    'never null' claim on a column with a real null share is planned into a
    null-share probe and CONTRADICTED with the measured share in evidence.
    The seeded email lie (~5 percent nulls) is the tracer."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        field_path="email",
        claim_type=ClaimType.COMPLETENESS,
        text="Never null.",
        predicate={"nullable": False},
    )
    spec = plan_probe(claim)
    assert "email" in spec.sql
    result = run_probe(spec, warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert finding.evidence["null_share"] > 0.01
    assert "probe_sql" in finding.evidence
    assert finding.rationale


def test_trace_null_share_is_unverifiable_not_confirmed(tmp_path):
    """Completeness slice 2: the ONE behavior this locks - a null share
    below the contradiction floor but above zero falls to UNVERIFIABLE,
    never to CONFIRMED (fail-closed middle band) and never to a false
    catch."""
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table t (v integer)")
    con.execute(
        "insert into t select case when range = 0 then NULL else range end "
        "from range(1000)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v",
            claim_type=ClaimType.COMPLETENESS,
            text="Never null.",
            predicate={"nullable": False},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE  # 0.001 share: neither verdict


def test_truthful_never_null_column_is_confirmed(warehouse):
    """Completeness slice 2b: a genuinely never-null column (customer_id)
    earns CONFIRMED with a zero measured share over a real sample."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        field_path="customer_id",
        claim_type=ClaimType.COMPLETENESS,
        text="Primary key.",
        predicate={"nullable": False},
    )
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.CONFIRMED
    assert finding.evidence["null_share"] == 0.0


def test_stale_daily_rollup_lie_is_contradicted(warehouse):
    """Rubrics slice 3 (freshness): the ONE behavior this locks - a
    table-level 'updated daily' claim is probed by discovering the table's
    date columns, measuring the latest value against an explicit as_of
    anchor, and CONTRADICTED when the staleness dwarfs the cadence. The
    seeded fct_sessions_daily lie (~51 days stale) is the tracer."""
    from notary.demo.seeder import ANCHOR_DATE

    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_sessions_daily,PROD)",
        field_path=None,
        claim_type=ClaimType.FRESHNESS,
        text="Updated daily.",
        predicate={"cadence": "daily"},
    )
    spec = plan_probe(claim, as_of=ANCHOR_DATE)
    assert spec.sql  # a real probe recipe, not the empty no-recipe marker
    result = run_probe(spec, warehouse)
    assert result.error is None
    assert result.measurements["days_stale"] >= 45
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert finding.evidence["days_stale"] >= 45
    assert finding.evidence["as_of"] == ANCHOR_DATE
    assert finding.rationale


def test_freshness_without_anchor_is_unverifiable(warehouse):
    """Freshness fail-closed: with no as_of anchor supplied, the probe has
    no reference date; the claim falls to UNVERIFIABLE instead of silently
    using the wall clock."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_sessions_daily,PROD)",
        field_path=None,
        claim_type=ClaimType.FRESHNESS,
        text="Updated daily.",
        predicate={"cadence": "daily"},
    )
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_hidden_enum_value_lie_is_contradicted(warehouse):
    """Rubrics slice 4 (domain_enum values): the ONE behavior this locks - a
    claimed closed enum is contradicted by an observed value outside the
    claimed set, with the extras named in evidence. The seeded segment lie
    (hidden 'partner' value) is the tracer."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        field_path="segment",
        claim_type=ClaimType.DOMAIN_ENUM,
        text="Customer segment, one of {consumer, business}.",
        predicate={"values": ["consumer", "business"]},
    )
    result = run_probe(plan_probe(claim), warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert "partner" in finding.evidence["unexpected_values"]
    assert finding.rationale


def test_negative_bounds_lie_is_contradicted(warehouse):
    """Rubrics slice 4b (domain_enum bounds): a claimed non-negative column
    with observed negative values is contradicted, measured min in
    evidence. The seeded qty lie (oversell negatives) is the tracer."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.stg_inventory,PROD)",
        field_path="qty",
        claim_type=ClaimType.DOMAIN_ENUM,
        text="Units on hand. Non-negative.",
        predicate={"min": 0},
    )
    result = run_probe(plan_probe(claim), warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert finding.evidence["observed_min"] < 0
    assert finding.rationale


def test_truthful_enum_control_is_confirmed(warehouse):
    """Rubrics slice 4c: the truthful currency enum control is CONFIRMED
    only because the complete distinct set was observed within the scan cap
    and sits inside the claimed set."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        field_path="currency",
        claim_type=ClaimType.DOMAIN_ENUM,
        text="ISO-4217 currency code, one of {USD, EUR, GBP}.",
        predicate={"values": ["USD", "EUR", "GBP"]},
    )
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.CONFIRMED


def test_empty_claimed_enum_is_unverifiable_not_contradicted(warehouse):
    """Domain_enum fail-closed guard: an empty claimed value set is a
    degenerate claim; treating every observed value as unexpected would
    manufacture false positives. It falls to UNVERIFIABLE."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        field_path="currency",
        claim_type=ClaimType.DOMAIN_ENUM,
        text="ISO-4217 currency code.",
        predicate={"values": []},
    )
    finding = adjudicate(claim, run_probe(plan_probe(claim), warehouse))
    assert finding.verdict is Verdict.UNVERIFIABLE


def _big_table(tmp_path, name, ddl, insert):
    db = tmp_path / f"{name}.duckdb"
    con = duckdb.connect(str(db))
    con.execute(ddl)
    con.execute(insert)
    con.close()
    return duckdb.connect(str(db), read_only=True)


def test_capped_scan_cannot_confirm_never_null(tmp_path):
    """PR3 review (adversarial HIGH): a zero-null PREFIX cannot confirm a
    universal never-null claim; when the scan hits its cap the confirm
    branch falls to UNVERIFIABLE (contradiction from a sample stays valid)."""
    ro = _big_table(
        tmp_path, "cap1", "create table t (v integer)",
        "insert into t select range from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.COMPLETENESS,
            text="Never null.", predicate={"nullable": False},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_capped_scan_cannot_confirm_bounds(tmp_path):
    """PR3 review: same universal-claim guard for the bounds branch."""
    ro = _big_table(
        tmp_path, "cap2", "create table t (v integer)",
        "insert into t select range from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.DOMAIN_ENUM,
            text="Non-negative.", predicate={"min": 0},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_capped_scan_cannot_confirm_enum_subset(tmp_path):
    """PR3 review (bot P1 + Codex HIGH): the distinct probe bounds the rows
    it READS, and a subset observed within a capped input scan proves
    nothing; the confirm branch falls to UNVERIFIABLE."""
    ro = _big_table(
        tmp_path, "cap3", "create table t (v varchar)",
        "insert into t select 'a' from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.DOMAIN_ENUM,
            text="One of {a}.", predicate={"values": ["a"]},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_freshness_never_confirms_from_ambiguous_columns(tmp_path):
    """PR3 review (Codex HIGH x2): with multiple temporal columns nothing
    identifies which one is the refresh marker, so a fresh business date
    must not CONFIRM cadence; staleness of the max across ALL columns is an
    upper bound, so contradiction stays valid."""
    ro = _big_table(
        tmp_path, "fresh1",
        "create table t (updated_at date, delivery_date date)",
        "insert into t values (date '2026-05-01', date '2026-07-18')",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path=None, claim_type=ClaimType.FRESHNESS,
            text="Updated daily.", predicate={"cadence": "daily"},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
        assert finding.verdict is Verdict.UNVERIFIABLE  # fresh biz date, no confirm

        stale = _big_table(
            tmp_path, "fresh2",
            "create table t (updated_at date, delivery_date date)",
            "insert into t values (date '2026-05-01', date '2026-05-02')",
        )
        try:
            f2 = adjudicate(
                claim, run_probe(plan_probe(claim, as_of="2026-07-18"), stale)
            )
            assert f2.verdict is Verdict.CONTRADICTED  # even the newest is stale
        finally:
            stale.close()
    finally:
        ro.close()


def test_hourly_cadence_never_confirms_at_date_precision(tmp_path):
    """PR3 review (bot P1 + adversarial HIGH): a date-granular anchor cannot
    prove an hourly cadence; a timestamp on the anchor day may still be
    nearly 24h behind. Hourly confirmation is unreachable in v1;
    contradiction (days behind) still works."""
    ro = _big_table(
        tmp_path, "hourly1",
        "create table t (snapshot_at timestamp)",
        "insert into t values (timestamp '2026-07-18 00:00:00')",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path=None, claim_type=ClaimType.FRESHNESS,
            text="Refreshed hourly.", predicate={"cadence": "hourly"},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE
