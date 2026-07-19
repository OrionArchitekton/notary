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


def test_lineage_gate_refuses_without_upstream_edge(monkeypatch):
    """Judge-slice v3: a declared reconciliation source corroborates only
    when the catalog records it as a lineage UPSTREAM of the suspect;
    absence and query failure both refuse (fail-closed)."""
    import io
    import json
    import urllib.request

    from notary.run import lineage_verified_upstream

    urn = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
           "fiction_retail.fct_payments,PROD)")
    billing = ("urn:li:dataset:(urn:li:dataPlatform:duckdb,"
               "fiction_retail.billing_invoices,PROD)")

    def _resp(payload):
        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R(json.dumps(payload).encode())

    # edge present -> verified
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=15: _resp({"data": {"dataset": {"lineage": {
            "relationships": [{"entity": {"urn": billing}}]}}}}),
    )
    ok, detail = lineage_verified_upstream("http://gms", urn, "billing_invoices")
    assert ok and "lineage-verified" in detail

    # edge absent -> refused
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=15: _resp({"data": {"dataset": {"lineage": {
            "relationships": []}}}}),
    )
    ok, detail = lineage_verified_upstream("http://gms", urn, "billing_invoices")
    assert not ok and "refused" in detail

    # query failure -> refused, never assumed
    def _boom(req, timeout=15):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    ok, detail = lineage_verified_upstream("http://gms", urn, "billing_invoices")
    assert not ok and "refused" in detail
