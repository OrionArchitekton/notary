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
