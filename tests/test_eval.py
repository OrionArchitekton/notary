"""S7 slice 1: the eval harness scores the full manifest against ground truth.

The ONE behavior this locks: evaluate() runs every manifest entry end-to-end
(extraction gate -> probe -> rubric) and classifies each against its
planted-lie ground truth into caught / missed / false-positive / clean,
aggregated per claim type, with misses REPORTED, never hidden. Without this
the published S7 table is vibes, not evidence.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from notary.demo.seeder import DEFAULT_SEED, MANIFEST, build_warehouse
from notary.eval import evaluate
from notary.extract import CaptureLLM, ReplayLLM
from notary.types import ClaimType


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    db = tmp_path_factory.mktemp("wh") / "fiction_retail.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    yield con
    con.close()


# One grounded predicate per manifest entry, keyed (table, column). The canned
# LLM below stands in for the extraction boundary ONLY: its output still runs
# through the real parse/grounding/entailment gates, and everything downstream
# (probe, rubric, scoring) is the real pipeline. Every predicate value is
# entailed by the entry's description text, as the gates require.
_PREDICATES = {
    ("fct_payments", "amount"): {"unit": "USD"},
    ("dim_products", "price_usd"): {"unit": "USD"},
    ("fct_orders", "discount_pct"): {"unit": "percentage"},
    ("fct_sessions_daily", "duration_ms"): {"unit": "milliseconds"},
    ("dim_products", "weight_grams"): {"unit": "grams"},
    ("fct_refunds", "amount_cents"): {"unit": "cents"},
    ("dim_customers", "email"): {"nullable": False},
    ("fct_orders", "shipped_at"): {"nullable": False},
    ("fct_refunds", "reason"): {"nullable": False},
    ("fct_payments", "currency"): {"values": ["USD", "EUR", "GBP"]},
    ("dim_customers", "country_code"): {"values": []},
    ("dim_customers", "segment"): {"values": ["consumer", "business"]},
    ("fct_orders", "status"): {
        "values": ["placed", "shipped", "delivered", "returned"]
    },
    ("stg_inventory", "qty"): {"min": 0},
    ("fct_sessions_daily", None): {"cadence": "daily"},
    ("stg_inventory", None): {"cadence": "hourly"},
    ("legacy_orders_v1", None): {"deprecated": True},
}

_BY_KEY = {(e.table, e.column): e for e in MANIFEST.claims}


class _ManifestLLM:
    """Deterministic extraction double: one grounded claim per entry.

    Unknown prompts raise (fail closed), mirroring ReplayLLM's contract."""

    def complete(self, system: str, user: str) -> str:
        table = re.search(r"fiction_retail\.(\w+),", user).group(1)
        m = re.search(r"Target: column `(\w+)`", user)
        col = m.group(1) if m else None
        entry = _BY_KEY[(table, col)]  # KeyError = unknown prompt
        return json.dumps([
            {
                "claim_type": entry.claim_type.value,
                "text": entry.description,
                "predicate": _PREDICATES[(table, col)],
            }
        ])


def test_eval_scores_manifest_against_ground_truth(warehouse):
    report = evaluate(MANIFEST, warehouse, _ManifestLLM())

    # every manifest entry is scored exactly once
    assert len(report.entries) == len(MANIFEST.claims)

    totals = report.totals()
    assert totals["lies"] == 12
    assert totals["controls"] == 5
    assert totals["caught"] + totals["missed"] == totals["lies"]
    assert totals["false_positives"] + totals["clean"] == totals["controls"]

    rows = report.rows()
    assert set(rows) == set(ClaimType)  # every claim type gets a row
    # v1 rubric reality, published honestly: the cents lie is caught, the
    # three non-USD unit lies are reported as MISSED, no control trips
    us = rows[ClaimType.UNIT_SCALE]
    assert us["caught"] == 1
    assert us["missed"] == 3
    assert us["false_positives"] == 0
    # rubric-less claim types report their misses instead of hiding them
    assert rows[ClaimType.FRESHNESS]["missed"] == 2
    assert rows[ClaimType.COMPLETENESS]["missed"] == 3
    assert rows[ClaimType.DEPRECATION_USAGE]["missed"] == 1

    # per-entry outcomes for the known S1 pair
    by_key = {(r.entry.table, r.entry.column): r for r in report.entries}
    assert by_key[("fct_payments", "amount")].outcome == "caught"
    assert by_key[("dim_products", "price_usd")].outcome == "clean"

    # the publishable markdown table carries the honest counts verbatim
    md = report.to_markdown()
    assert "unit_scale" in md
    assert "missed" in md.lower()

    # S7 acceptance: reproducible - a second run yields the identical table
    again = evaluate(MANIFEST, warehouse, _ManifestLLM())
    assert again.to_markdown() == md


class _OneBlockedLLM(_ManifestLLM):
    """The country_code prompt raises, mirroring the provider's deterministic
    content-filter 400 on that exact description (req_011CdAB3/6d8/Ak2)."""

    def complete(self, system: str, user: str) -> str:
        if "ISO-3166" in user:
            raise RuntimeError("Output blocked by content filtering policy")
        return super().complete(system, user)


def test_extraction_failure_is_isolated_and_reported(warehouse):
    """S7 slice 3: the ONE behavior this locks - one entry's extraction
    failure never poisons the rest of the eval, is scored fail-closed
    (lie->missed / control->clean), and is REPORTED in the table rather
    than silently folded into a clean-looking count."""
    report = evaluate(MANIFEST, warehouse, _OneBlockedLLM())

    assert len(report.entries) == len(MANIFEST.claims)  # nothing dropped
    by_key = {(r.entry.table, r.entry.column): r for r in report.entries}

    blocked = by_key[("dim_customers", "country_code")]
    assert blocked.extraction_error  # recorded, not swallowed
    assert blocked.outcome == "clean"  # control + no verdict = fail-closed
    assert not blocked.findings

    # neighbours in the same table are unaffected
    assert by_key[("dim_customers", "email")].outcome == "missed"
    assert not by_key[("dim_customers", "email")].extraction_error
    assert by_key[("fct_payments", "amount")].outcome == "caught"

    # the published table discloses the failure count verbatim
    md = report.to_markdown()
    assert "extraction" in md.lower()
    # and a fully-clean run carries no such disclaimer
    clean_md = evaluate(MANIFEST, warehouse, _ManifestLLM()).to_markdown()
    assert "extraction" not in clean_md.lower()


def test_readme_table_is_the_verbatim_replay_output(tmp_path):
    """S7 slice 4: the ONE behavior this locks - the table published in the
    README is byte-for-byte what the replay run emits. A hand-edited or stale
    README number fails this test; the published table stays evidence."""
    root = Path(__file__).parent.parent
    db = tmp_path / "wh.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = evaluate(MANIFEST, con, ReplayLLM(root / "tests/fixtures/llm"))
    finally:
        con.close()
    readme = (root / "README.md").read_text()
    assert report.to_markdown() in readme


def test_one_command_emits_reproducible_table(tmp_path):
    """S7 slice 2: the ONE behavior this locks - a single command produces the
    eval table from a cold start, and running it twice is byte-identical.
    This is the command the README table is generated by; if it drifts from
    the library path, the published table is no longer evidence."""
    fixtures = tmp_path / "llm"
    db = tmp_path / "wh.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        # write replay fixtures via the real capture path (no network)
        evaluate(
            MANIFEST, con,
            CaptureLLM(_ManifestLLM(), fixtures, {"source": "test-canned"}),
        )
    finally:
        con.close()

    cmd = [
        sys.executable, "-m", "notary.eval",
        "--db", str(tmp_path / "cli.duckdb"),
        "--fixtures", str(fixtures),
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    assert "| claim type |" in first.stdout
    assert "unit_scale" in first.stdout

    second = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert second.stdout == first.stdout
