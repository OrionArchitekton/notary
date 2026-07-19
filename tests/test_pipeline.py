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


def test_probe_scans_bound_rows_before_null_filter():
    """Cycle-3 regression (Codex HIGH): LIMIT must bound the rows READ, so
    the null filter sits OUTSIDE the bounded subquery; filtering first would
    scan a sparse table until it finds SCAN_LIMIT non-null values."""
    unit = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        field_path="amount", claim_type=ClaimType.UNIT_SCALE,
        text="Transaction amount in USD.", predicate={"unit": "USD"},
    )
    enum = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        field_path="currency", claim_type=ClaimType.DOMAIN_ENUM,
        text="One of {USD}.", predicate={"values": ["USD"]},
    )
    bound = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.stg_inventory,PROD)",
        field_path="qty", claim_type=ClaimType.DOMAIN_ENUM,
        text="Non-negative.", predicate={"min": 0},
    )
    # The unit probe now keeps nulls IN the bounded scan (rows_scanned is
    # the cap detector) and skips them via null-safe aggregates instead of
    # an outer filter; the bounded-read invariant is unchanged.
    unit_sql = plan_probe(unit).sql.lower()
    assert "limit" in unit_sql
    assert "count(*) as rows_scanned" in unit_sql
    assert "case when v is null then null" in unit_sql
    assert "is not null" not in unit_sql, unit_sql
    for claim in (enum, bound):
        sql = plan_probe(claim).sql.lower()
        assert "is not null" in sql
        assert sql.index("limit") < sql.index("is not null"), sql


def test_fraction_scale_ambiguity_is_unverifiable_not_contradicted(warehouse):
    """PR5 cycle-1 adjudication (Codex HIGH x2): stored-as-fraction vs
    rounded sub-1-percent TRUE percentages is scale-invariant; any 0-1
    distribution has a legitimate tiny-percent reading, so a
    distribution-only rubric must NOT contradict. The seeded discount lie
    stays a DECLARED miss (fail-closed beats a false positive)."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_orders,PROD)",
        field_path="discount_pct",
        claim_type=ClaimType.UNIT_SCALE,
        text="Discount percentage between 0 and 100.",
        predicate={"unit": "percent_0_100"},
    )
    result = run_probe(plan_probe(claim), warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.UNVERIFIABLE
    assert "scale" in finding.rationale.lower() or "fraction" in finding.rationale.lower()


def test_genuine_percent_distribution_is_confirmed(tmp_path):
    """Companion: values spread across (1, 100] match the claimed scale."""
    db = tmp_path / "p.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table t (p double)")
    con.execute(
        "insert into t select 1.0 + (range * 98.0 / 999) from range(1000)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="p",
            claim_type=ClaimType.UNIT_SCALE,
            text="Percent between 0 and 100.",
            predicate={"unit": "percent_0_100"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.CONFIRMED


def test_tiny_legitimate_percentages_are_unverifiable(tmp_path):
    """Fail-closed middle: a column of genuinely tiny percentages (all
    below 1) is INDISTINGUISHABLE from stored-as-fraction by magnitude
    alone when it hugs zero; only the fraction signature (spread across
    the full 0-to-1 interval) contradicts. Values clustered near zero
    fall to UNVERIFIABLE."""
    db = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table t (p double)")
    con.execute(
        "insert into t select 0.001 + (range * 0.05 / 999) from range(1000)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="p",
            claim_type=ClaimType.UNIT_SCALE,
            text="Error rate percent between 0 and 100.",
            predicate={"unit": "percent_0_100"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_deprecated_but_actively_read_lie_is_contradicted(warehouse):
    """Unit-rubrics slice 2 (deprecation): the ONE behavior this locks - a
    'no longer used' claim on a table whose seeded query history shows
    active recent reads is CONTRADICTED with the measured query count. The
    legacy_orders_v1 lie is the tracer."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.legacy_orders_v1,PROD)",
        field_path=None,
        claim_type=ClaimType.DEPRECATION_USAGE,
        text="DEPRECATED 2025: superseded by fct_orders. No longer used.",
        predicate={"deprecated": True},
    )
    result = run_probe(plan_probe(claim, as_of="2026-07-18"), warehouse)
    assert result.error is None
    finding = adjudicate(claim, result)
    assert finding.verdict is Verdict.CONTRADICTED
    assert finding.evidence["recent_queries"] >= 10
    assert finding.rationale


def test_genuinely_unused_deprecated_table_is_confirmed(tmp_path):
    """Companion: a deprecated table with an EMPTY recent query history is
    CONFIRMED (the claim holds)."""
    db = tmp_path / "d.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table old_stuff (x integer)")
    con.execute(
        "create table query_log (table_name varchar, queried_at timestamp, "
        "query_user varchar)"
    )
    con.execute(
        "insert into query_log values "
        "('other_table', timestamp '2026-07-17 10:00:00', 'ana')"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.old_stuff,PROD)",
            field_path=None,
            claim_type=ClaimType.DEPRECATION_USAGE,
            text="DEPRECATED. No longer used.",
            predicate={"deprecated": True},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.CONFIRMED


def test_deprecation_without_query_log_is_unverifiable(tmp_path):
    """Fail-closed: a warehouse with no query_log carries no usage evidence
    either way; the claim falls to UNVERIFIABLE, never CONFIRMED."""
    db = tmp_path / "nolog.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table old_stuff (x integer)")
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.old_stuff,PROD)",
            field_path=None,
            claim_type=ClaimType.DEPRECATION_USAGE,
            text="DEPRECATED. No longer used.",
            predicate={"deprecated": True},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_deprecation_window_excludes_post_anchor_queries(tmp_path):
    """PR5 cycle-1 regression (HIGH x2): queries dated AFTER as_of must not
    count toward 'the 30 days before' it; a historical anchored run must
    not be contradicted by later activity. (With only post-anchor rows the
    log also shows no in-window life, so the claim is UNVERIFIABLE rather
    than CONFIRMED: cycle-2 window-liveness rule.)"""
    db = tmp_path / "later.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table old_stuff (x integer)")
    con.execute(
        "create table query_log (table_name varchar, queried_at timestamp, "
        "query_user varchar)"
    )
    con.execute(
        "insert into query_log select 'old_stuff', "
        "timestamp '2026-07-25 10:00:00' + interval (range) hour, 'ana' "
        "from range(20)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.old_stuff,PROD)",
            field_path=None,
            claim_type=ClaimType.DEPRECATION_USAGE,
            text="DEPRECATED. No longer used.",
            predicate={"deprecated": True},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE  # never CONTRADICTED
    assert finding.evidence["recent_queries"] == 0  # later reads excluded


def test_deprecation_probe_bounds_the_log_scan():
    """PR5 cycle-1 regression: the LIMIT bounds rows READ from query_log
    (inner subquery), not matching output rows."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.legacy_orders_v1,PROD)",
        field_path=None,
        claim_type=ClaimType.DEPRECATION_USAGE,
        text="DEPRECATED. No longer used.",
        predicate={"deprecated": True},
    )
    sql = plan_probe(claim, as_of="2026-07-18").sql.lower()
    # the table_name/date filters sit OUTSIDE the bounded subquery
    assert ") where table_name" in sql, sql


def test_column_level_deprecation_claim_is_unverifiable(warehouse):
    """PR5 cycle-2 regression: table-level query logs say nothing about a
    COLUMN's usage; a column-scoped deprecation claim gets no probe recipe
    and falls to UNVERIFIABLE instead of borrowing table evidence."""
    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.legacy_orders_v1,PROD)",
        field_path="total_cents",
        claim_type=ClaimType.DEPRECATION_USAGE,
        text="DEPRECATED column. No longer used.",
        predicate={"deprecated": True},
    )
    finding = adjudicate(
        claim, run_probe(plan_probe(claim, as_of="2026-07-18"), warehouse)
    )
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_empty_query_log_cannot_confirm_deprecation(tmp_path):
    """PR5 cycle-2 regression: a physically complete but EMPTY (or
    window-dead) log proves nothing about the window; CONFIRMED requires
    the log to show life inside the window for some table."""
    db = tmp_path / "emptylog.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table old_stuff (x integer)")
    con.execute(
        "create table query_log (table_name varchar, queried_at timestamp, "
        "query_user varchar)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.old_stuff,PROD)",
            field_path=None,
            claim_type=ClaimType.DEPRECATION_USAGE,
            text="DEPRECATED. No longer used.",
            predicate={"deprecated": True},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_qualified_log_names_also_match(tmp_path):
    """PR5 cycle-2 regression: logs that store schema-qualified identifiers
    (fiction_retail.old_stuff) count toward the same asset."""
    db = tmp_path / "quallog.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table old_stuff (x integer)")
    con.execute(
        "create table query_log (table_name varchar, queried_at timestamp, "
        "query_user varchar)"
    )
    con.execute(
        "insert into query_log select 'fiction_retail.old_stuff', "
        "timestamp '2026-07-10 10:00:00' + interval (range) hour, 'ana' "
        "from range(15)"
    )
    con.close()
    ro = duckdb.connect(str(db), read_only=True)
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.old_stuff,PROD)",
            field_path=None,
            claim_type=ClaimType.DEPRECATION_USAGE,
            text="DEPRECATED. No longer used.",
            predicate={"deprecated": True},
        )
        finding = adjudicate(
            claim, run_probe(plan_probe(claim, as_of="2026-07-18"), ro)
        )
    finally:
        ro.close()
    assert finding.verdict is Verdict.CONTRADICTED


def test_capped_scan_cannot_contradict_usd_cents(tmp_path):
    """Overclaim-review fix (Codex C3): the cents CONTRADICTION asserts
    "every value is an integer", a universal statement, so a capped prefix
    cannot support it; when the scan hits its cap the USD rubric falls to
    UNVERIFIABLE instead of contradicting from prefix statistics."""
    ro = _big_table(
        tmp_path, "ucap1", "create table t (v bigint)",
        "insert into t select 12795 from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.UNIT_SCALE,
            text="Transaction amount in USD.",
            predicate={"unit": "USD"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_null_in_capped_prefix_cannot_hide_the_cap(tmp_path):
    """PR8 pipeline fix (both engines, HIGH): the completeness guard must
    key on rows SCANNED, not non-null values measured. One null inside a
    full-cap prefix previously made row_count dip under the limit and
    re-opened the universal-verdict path from prefix statistics."""
    ro = _big_table(
        tmp_path, "ucap3", "create table t (v double)",
        "insert into t values (NULL); "
        "insert into t select 12795 from range(99999); "
        "insert into t values (12795.5)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.UNIT_SCALE,
            text="Transaction amount in USD.",
            predicate={"unit": "USD"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_capped_scan_cannot_confirm_usd_dollars(tmp_path):
    """Same gate, confirm side: a plausible-dollars PREFIX cannot confirm
    the unit claim for rows beyond the cap."""
    ro = _big_table(
        tmp_path, "ucap2", "create table t (v double)",
        "insert into t select 19.99 + (random() * 0.5) from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="v", claim_type=ClaimType.UNIT_SCALE,
            text="List price in USD.",
            predicate={"unit": "USD"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_null_in_capped_prefix_cannot_confirm_percent(tmp_path):
    """PR8 cycle-2 fix (Codex HIGH): the percent CONFIRM path must key scan
    completeness on rows SCANNED, not the non-null count; a null inside a
    full-cap prefix previously read as a complete scan and confirmed from
    prefix statistics while an out-of-range value sat beyond the cap."""
    ro = _big_table(
        tmp_path, "pcap2", "create table t (p double)",
        "insert into t values (NULL); "
        "insert into t select 50.0 from range(99999); "
        "insert into t values (200.0)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="p", claim_type=ClaimType.UNIT_SCALE,
            text="Percent between 0 and 100.",
            predicate={"unit": "percent_0_100"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE


def test_capped_scan_cannot_confirm_percent_scale(tmp_path):
    """PR5 cycle-3 regression (HIGH): the percent CONFIRM branch requires a
    complete scan like every other universal claim; a capped prefix of
    in-range values proves nothing about row 100,001."""
    ro = _big_table(
        tmp_path, "pcap", "create table t (p double)",
        "insert into t select 50.0 from range(100001)",
    )
    try:
        claim = Claim(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            field_path="p", claim_type=ClaimType.UNIT_SCALE,
            text="Percent between 0 and 100.",
            predicate={"unit": "percent_0_100"},
        )
        finding = adjudicate(claim, run_probe(plan_probe(claim), ro))
    finally:
        ro.close()
    assert finding.verdict is Verdict.UNVERIFIABLE
