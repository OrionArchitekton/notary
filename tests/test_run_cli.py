"""Run-CLI safety rails (PR4 cycle-1 findings), no live GMS needed.

The ONE behavior each test locks: the CLI validates its inputs BEFORE any
network or filesystem mutation, never rebuilds an operator-supplied
warehouse, and a run with any failed write-back is not a successful exit.
"""
import subprocess
import sys
from pathlib import Path

from notary.run import receipt_ok

ROOT = Path(__file__).parent.parent
DEMO_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"
)
FOREIGN_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,prod.finance.payments,PROD)"


def _run(args, env_overrides):
    import os

    env = {**os.environ, **env_overrides}
    env.pop("NOTARY_RUN_DATE", None)
    if "NOTARY_RUN_DATE" in env_overrides:
        env["NOTARY_RUN_DATE"] = env_overrides["NOTARY_RUN_DATE"]
    return subprocess.run(
        [sys.executable, "-m", "notary.run", *args],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT), env=env,
    )


def test_malformed_run_date_is_rejected(tmp_path):
    r = _run(
        ["--asset", DEMO_URN, "--demo", "--db", str(tmp_path / "wh.duckdb")],
        {"NOTARY_RUN_DATE": "yesterday"},
    )
    assert r.returncode == 2
    assert "NOTARY_RUN_DATE" in r.stderr


def test_missing_run_date_is_rejected(tmp_path):
    r = _run(
        ["--asset", DEMO_URN, "--demo", "--db", str(tmp_path / "wh.duckdb")], {}
    )
    assert r.returncode == 2


def test_demo_mode_rejects_foreign_urns(tmp_path):
    """Demo mode probes the seeded fiction warehouse; writing its verdicts
    to a non-demo asset would be a false catalog finding."""
    r = _run(
        ["--asset", FOREIGN_URN, "--demo", "--db", str(tmp_path / "wh.duckdb")],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert "demo" in r.stderr.lower()


def test_non_demo_mode_requires_existing_warehouse(tmp_path):
    """Without --demo the CLI NEVER builds or replaces the warehouse; a
    missing --db is a setup error, not a build trigger."""
    missing = tmp_path / "nope.duckdb"
    r = _run(
        ["--asset", FOREIGN_URN, "--db", str(missing)],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert not missing.exists()  # nothing was created at the path


def test_demo_mode_never_overwrites_an_existing_file(tmp_path):
    """--demo builds the seeded warehouse only at an ABSENT path; an
    existing file is refused, never replaced (cycle-1 CRITICAL)."""
    existing = tmp_path / "precious.duckdb"
    existing.write_bytes(b"operator data, not notary's to destroy")
    r = _run(
        ["--asset", DEMO_URN, "--demo", "--db", str(existing)],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert existing.read_bytes() == b"operator data, not notary's to destroy"


def test_receipt_with_failed_description_is_not_ok():
    """PR4 finding: a run that failed to correct a contradicted description
    must not exit 0 on a green ledger alone."""
    good = {
        "ledger": True,
        "documents": [{"ok": True}],
        "descriptions": [{"ok": True}],
    }
    bad_desc = {
        "ledger": True,
        "documents": [{"ok": True}],
        "descriptions": [{"ok": False, "error": "mcp error"}],
    }
    bad_doc = {
        "ledger": True,
        "documents": [{"ok": False}],
        "descriptions": [],
    }
    assert receipt_ok(good)
    assert not receipt_ok(bad_desc)
    assert not receipt_ok(bad_doc)
    assert not receipt_ok({"ledger": False, "documents": [], "descriptions": []})


def test_demo_mode_rejects_urns_outside_the_manifest(tmp_path):
    """Cycle-2 finding: the demo allowlist is EXACT manifest-derived urns,
    not a name prefix; an unseeded fiction_retail.* asset must be refused."""
    fake = (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
        "fiction_retail.made_up_table,PROD)"
    )
    r = _run(
        ["--asset", fake, "--demo", "--db", str(tmp_path / "wh.duckdb")],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert "demo" in r.stderr.lower()


def test_non_demo_rejects_non_duckdb_platforms(tmp_path):
    """Cycle-2 CRITICAL: the CLI probes DuckDB warehouses only; verdicts for
    a snowflake (or any other platform) asset cannot be derived from a local
    DuckDB file."""
    db = tmp_path / "wh.duckdb"
    import duckdb

    con = duckdb.connect(str(db))
    con.execute("create table fct_payments (amount double)")
    con.close()
    r = _run(
        ["--asset", FOREIGN_URN, "--db", str(db)],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert "duckdb" in r.stderr.lower()


def test_non_demo_requires_the_asset_table_in_the_warehouse(tmp_path):
    """Cycle-2 CRITICAL companion: the asset's table must exist in the
    supplied warehouse; probing an unrelated database for an asset's
    verdicts is a false finding."""
    db = tmp_path / "wh.duckdb"
    import duckdb

    con = duckdb.connect(str(db))
    con.execute("create table something_else (x integer)")
    con.close()
    r = _run(
        ["--asset", DEMO_URN, "--db", str(db)],
        {"NOTARY_RUN_DATE": "2026-07-18"},
    )
    assert r.returncode == 2
    assert "fct_payments" in r.stderr


def test_schema_fingerprint_match_is_pure_and_strict():
    """Cycle-3 finding: a bare table NAME is not binding evidence; the
    asset's cataloged field names must be present as columns in the
    warehouse table before its measurements may speak for the asset."""
    from notary.run import schema_matches

    assert schema_matches(
        catalog_fields=["amount", "currency"],
        warehouse_columns=["amount", "currency", "extra_col"],
    )
    assert not schema_matches(
        catalog_fields=["amount", "currency"],
        warehouse_columns=["totally", "different"],
    )
    # no cataloged fields = nothing to bind on: fail closed
    assert not schema_matches(catalog_fields=[], warehouse_columns=["a"])





def test_recon_refusal_blocks_obsolete_incident_resolution():
    """PR #11 adversarial finding: a run downgraded by a reconciliation
    refusal must never clear a standing incident; the decision logic in
    main() gates close_obsolete_incident on zero refusals. Locked here at
    the source level to keep the contract visible."""
    import inspect

    from notary import run as run_mod

    src = inspect.getsource(run_mod.main)
    assert "elif recon_refusals or unresolved_unit_suspicion(findings):" in src
    idx_refusal = src.index("elif recon_refusals")
    idx_close = src.index("close_obsolete_incident")
    assert idx_refusal < idx_close


def test_lineage_edges_group_by_downstream():
    """PR #11 finding: the UpstreamLineage aspect replaces wholesale, so
    all of a downstream's edges must travel in one emission."""
    from notary.catalog import _grouped_lineage

    grouped = _grouped_lineage((
        ("billing", "payments"),
        ("ledger", "payments"),
        ("billing", "payments"),
        ("payments", "reporting"),
    ))
    assert grouped == {
        "payments": ["billing", "ledger"],
        "reporting": ["payments"],
    }


def test_unresolved_unit_suspicion_blocks_incident_clearing():
    """PR #11 cycle-2 finding: a cents-signature unit claim this run could
    not adjudicate (nothing declared, or declared and uncorroborated) is
    OUTSTANDING suspicion; a run carrying one must never clear a standing
    incident. A configuration omission is not fresh evidence."""
    from notary.run import unresolved_unit_suspicion
    from notary.types import Claim, ClaimType, Finding, Verdict

    claim = Claim(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        field_path="amount", claim_type=ClaimType.UNIT_SCALE,
        text="Transaction amount in USD.", predicate={"unit": "USD"},
    )
    suspicious = Finding(
        claim=claim, verdict=Verdict.UNVERIFIABLE,
        evidence={"integer_share": 1.0, "median": 12795.0},
        rationale="cents signature, nothing declared",
    )
    assert unresolved_unit_suspicion([suspicious])

    confirmed = Finding(
        claim=claim, verdict=Verdict.CONFIRMED,
        evidence={"integer_share": 0.01, "median": 127.95},
        rationale="dollars",
    )
    assert not unresolved_unit_suspicion([confirmed])

    # a no-rubric UNVERIFIABLE nests measurements and carries no
    # top-level cents signature: not suspicion
    no_rubric = Finding(
        claim=claim, verdict=Verdict.UNVERIFIABLE,
        evidence={"measurements": {"integer_share": 1.0, "median": 5000}},
        rationale="no v1 rubric",
    )
    assert not unresolved_unit_suspicion([no_rubric])


def test_reconcile_flag_rejects_qualified_identifiers():
    """PR #11 cycle-2 finding: the probe layer addresses one warehouse
    schema with bare identifiers; a qualified reference accepted here
    would crash plan_probe after passing the lineage gate."""
    import pytest as _pytest

    from notary.run import parse_reconcile_args

    with _pytest.raises(ValueError):
        parse_reconcile_args(["amount=finance.billing:order_id:order_id:total"])
    with _pytest.raises(ValueError):
        parse_reconcile_args(['amount=billing:order id:order_id:total'])


def test_lineage_merge_preserves_existing_upstreams():
    """PR #11 cycle-2 finding: the UpstreamLineage aspect replaces
    wholesale, so declared edges must merge with what the catalog already
    records, never silently drop it."""
    from notary.catalog import _merged_upstreams

    merged = _merged_upstreams(
        existing=["urn:a", "urn:b"],
        declared=["urn:b", "urn:c"],
    )
    assert merged == ["urn:a", "urn:b", "urn:c"]


def test_lineage_gate_reads_through_stock_mcp_tool(monkeypatch):
    """Judge-slice v4: the verdict-gating lineage read is LOAD-BEARING MCP:
    it goes through the stock DataHub MCP get_lineage tool, and the tool
    name + target urn travel back as a receipt for the evidence dossier.
    A failing MCP read refuses (fail-closed), never silently downgrades to
    another transport."""
    from notary.run import lineage_verified_upstream

    asset = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
             "fiction_retail.fct_payments,PROD)")
    billing = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
               "fiction_retail.billing_invoices,PROD)")

    calls = []

    def fake_reader(gms_url, asset_urn):
        calls.append((gms_url, asset_urn))
        return [billing]

    ok, detail, receipt = lineage_verified_upstream(
        "http://gms", asset, "billing_invoices", upstream_reader=fake_reader
    )
    assert ok, detail
    assert calls == [("http://gms", asset)]
    assert receipt["tool"] == "get_lineage"
    assert receipt["transport"] == "mcp"
    assert receipt["asset_urn"] == asset
    assert receipt["upstream_urn"] == billing

    def boom(gms_url, asset_urn):
        raise RuntimeError("mcp server unavailable")

    ok2, detail2, receipt2 = lineage_verified_upstream(
        "http://gms", asset, "billing_invoices", upstream_reader=boom
    )
    assert not ok2
    assert "refused" in detail2
    assert receipt2.get("error")


_ASSET_URN = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
              "fiction_retail.fct_payments,PROD)")
_BILLING_URN = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
                "fiction_retail.billing_invoices,PROD)")


def test_lineage_gate_refuses_without_upstream_edge():
    """The declared source corroborates only when MCP reports it as an
    upstream; absence refuses (fail-closed)."""
    from notary.run import lineage_verified_upstream

    ok, detail, receipt = lineage_verified_upstream(
        "http://gms", _ASSET_URN, "billing_invoices",
        upstream_reader=lambda g, a: [],
    )
    assert not ok and "refused" in detail
    assert receipt["upstreams_seen"] == 0


def test_lineage_gate_rejects_self_reference_and_qualified_names():
    """A reference resolving to the suspect itself is refused even when the
    catalog reports a self-edge; a qualified reference name is used
    verbatim; an unqualified suspect name gets no invented prefix."""
    from notary.run import lineage_verified_upstream

    ok, detail, _ = lineage_verified_upstream(
        "http://gms", _ASSET_URN, "fct_payments",
        upstream_reader=lambda g, a: [_ASSET_URN],
    )
    assert not ok and "suspect asset itself" in detail

    qualified = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
                 "other_db.billing,PROD)")
    ok2, detail2, _ = lineage_verified_upstream(
        "http://gms", _ASSET_URN, "other_db.billing",
        upstream_reader=lambda g, a: [qualified],
    )
    assert ok2, detail2

    bare_urn = "urn:li:dataset:(urn:li:dataPlatform:duckdb,payments,PROD)"
    bare_ref = "urn:li:dataset:(urn:li:dataPlatform:duckdb,billing,PROD)"
    ok3, detail3, _ = lineage_verified_upstream(
        "http://gms", bare_urn, "billing",
        upstream_reader=lambda g, a: [bare_ref],
    )
    assert ok3, detail3


def test_lineage_gate_refuses_on_possible_truncation():
    """PR #11 truncation lesson carried to MCP: get_lineage returns a
    BOUNDED result set, so 'not found' in a full page is indistinguishable
    from 'truncated'. That must refuse with a truncation-specific reason,
    never a confident 'not an upstream'."""
    from notary.catalog import LINEAGE_MAX_RESULTS
    from notary.run import lineage_verified_upstream

    full_page = [
        f"urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.up{i},PROD)"
        for i in range(LINEAGE_MAX_RESULTS)
    ]
    ok, detail, receipt = lineage_verified_upstream(
        "http://gms", _ASSET_URN, "billing_invoices",
        upstream_reader=lambda g, a: full_page,
    )
    assert not ok
    assert "truncat" in detail.lower()
    assert "truncat" in (receipt.get("error") or "").lower()


def test_upstream_parser_ignores_downstream_subtree(monkeypatch):
    """PR #12 bot finding, adjudicated: mcp-server-datahub 0.6.0 has no
    `downstream` argument (it derives downstream = not upstream), and a
    live reverse-direction probe confirmed no leakage today. But parsing
    the WHOLE payload would leak if a future version ever returned both
    directions in one response, and this gate authorizes catalog
    rewrites. So the parser reads ONLY the upstreams subtree."""
    import json

    import notary.catalog as cat

    asset = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
             "fiction_retail.fct_payments,PROD)")
    good = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
            "fiction_retail.billing_invoices,PROD)")
    bad = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
           "fiction_retail.reporting_mart,PROD)")
    payload = {
        "upstreams": {"searchResults": [{"entity": {"urn": good}}]},
        "downstreams": {"searchResults": [{"entity": {"urn": bad}}]},
    }

    class _Block:
        text = json.dumps(payload)

    class _Res:
        isError = False
        content = [_Block()]

    class _Session:
        async def call_tool(self, name, args):
            assert name == "get_lineage"
            assert args["upstream"] is True
            return _Res()

    class _Ctx:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cat.NotaryWriter, "_session", lambda self: _Ctx())
    urns = cat.mcp_upstream_urns("http://gms", asset)
    assert urns == [good], urns
    assert bad not in urns
