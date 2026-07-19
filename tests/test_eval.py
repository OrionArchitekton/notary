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
    ("fct_orders", "discount_pct"): {"unit": "percent_0_100"},
    ("fct_sessions_daily", "duration_ms"): {"unit": "milliseconds"},
    ("dim_products", "weight_grams"): {"unit": "grams"},
    ("fct_refunds", "amount_cents"): {"unit": "cents"},
    ("dim_customers", "email"): {"nullable": False},
    ("fct_orders", "shipped_at"): {"nullable": False},
    ("fct_refunds", "reason"): {"nullable": False},
    ("fct_payments", "currency"): {"values": ["USD", "EUR", "GBP"]},
    # the description names a standard, not a value set: a faithful
    # extraction emits no claim at all
    ("dim_customers", "country_code"): None,
    ("dim_customers", "segment"): {"values": ["consumer", "business"]},
    ("fct_orders", "status"): {
        "values": ["placed", "shipped", "delivered", "returned"]
    },
    ("stg_inventory", "qty"): {"min": 0},
    ("fct_sessions_daily", None): {"cadence": "daily"},
    ("stg_inventory", None): {"cadence": "hourly"},
    ("legacy_orders_v1", None): {"deprecated": True},
    ("billing_invoices", "total_major"): {"unit": "major_currency_units"},
    ("stg_service_fees", "fee_usd"): {"unit": "USD"},
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
        predicate = _PREDICATES[(table, col)]
        if predicate is None:
            return "[]"  # no testable claim in this description
        return json.dumps([
            {
                "claim_type": entry.claim_type.value,
                "text": entry.description,
                "predicate": predicate,
            }
        ])


def test_eval_scores_manifest_against_ground_truth(warehouse):
    from notary.demo.seeder import ANCHOR_DATE

    report = evaluate(MANIFEST, warehouse, _ManifestLLM(), as_of=ANCHOR_DATE)

    # every manifest entry is scored exactly once
    assert len(report.entries) == len(MANIFEST.claims)

    totals = report.totals()
    assert totals["lies"] == 12
    assert totals["controls"] == 7
    assert totals["caught"] + totals["missed"] == totals["lies"]
    assert totals["false_positives"] + totals["clean"] == totals["controls"]

    rows = report.rows()
    assert set(rows) == set(ClaimType)  # every claim type gets a row
    # rubric reality, published honestly: the cents lie is caught; the
    # percent, ms, and grams unit lies are DECLARED misses (fraction vs
    # sub-1-percent is scale-invariant, magnitude bands would be guesses);
    # completeness, freshness, domain_enum, and deprecation rubrics catch
    # their planted lies
    us = rows[ClaimType.UNIT_SCALE]
    assert us["caught"] == 1
    assert us["missed"] == 3
    assert us["false_positives"] == 0
    assert rows[ClaimType.COMPLETENESS]["caught"] == 3
    assert rows[ClaimType.FRESHNESS]["caught"] == 2
    de = rows[ClaimType.DOMAIN_ENUM]
    assert de["caught"] == 2
    assert de["false_positives"] == 0
    assert rows[ClaimType.DEPRECATION_USAGE]["caught"] == 1
    assert totals["caught"] == 9
    assert totals["missed"] == 3

    # per-entry outcomes for the known S1 pair
    by_key = {(r.entry.table, r.entry.column): r for r in report.entries}
    assert by_key[("fct_payments", "amount")].outcome == "caught"
    assert by_key[("dim_products", "price_usd")].outcome == "clean"

    # the publishable markdown table carries the honest counts verbatim
    md = report.to_markdown()
    assert "unit_scale" in md
    assert "missed" in md.lower()

    # S7 acceptance: reproducible - a second run yields the identical table
    again = evaluate(MANIFEST, warehouse, _ManifestLLM(), as_of=ANCHOR_DATE)
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
    # cycle-3 adversarial regression (MEDIUM): an unextracted CONTROL is
    # UNSCORED, not clean; counting it as clean would advertise a false
    # "0 false positives across N controls" for a control never adjudicated
    assert blocked.outcome == "unscored"
    assert not blocked.findings
    totals = report.totals()
    assert totals["unscored"] == 1
    assert totals["controls"] == 6  # scored controls only
    assert "unscored" in report.to_markdown().lower()

    # neighbours in the same table are unaffected (email's completeness lie
    # is caught by the null-share rubric)
    assert by_key[("dim_customers", "email")].outcome == "caught"
    assert not by_key[("dim_customers", "email")].extraction_error
    assert by_key[("fct_payments", "amount")].outcome == "caught"

    # the published table discloses the failure count verbatim
    md = report.to_markdown()
    assert "extraction" in md.lower()
    # and a fully-clean run carries no such disclaimer
    clean_md = evaluate(MANIFEST, warehouse, _ManifestLLM()).to_markdown()
    assert "extraction" not in clean_md.lower()


def test_off_target_contradiction_is_not_a_catch_and_is_disclosed():
    """Fleet-review regression (confirmed WARNING, eval.py:128): a lie entry
    is caught ONLY by a CONTRADICTED finding matching the planted claim. An
    off-type CONTRADICTED (a future rubric's false positive against a
    truthful secondary sentence) must not launder into 'caught', and must be
    disclosed in the table."""
    from notary.eval import EvalReport, score_entry
    from notary.types import Claim, Finding, Verdict

    entry = next(e for e in MANIFEST.claims if e.column == "discount_pct")
    urn = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_orders,PROD)"

    def finding(claim_type):
        return Finding(
            claim=Claim(
                asset_urn=urn, field_path="discount_pct", claim_type=claim_type,
                text=entry.description, predicate={},
            ),
            verdict=Verdict.CONTRADICTED, evidence={}, rationale="synthetic",
        )

    off = score_entry(entry, (finding(ClaimType.DOMAIN_ENUM),))
    assert off.outcome == "missed"  # NOT caught
    assert off.off_target_contradictions == 1

    on = score_entry(entry, (finding(ClaimType.UNIT_SCALE),))
    assert on.outcome == "caught"
    assert on.off_target_contradictions == 0

    md = EvalReport(entries=(off,)).to_markdown()
    assert "off-target" in md.lower()  # disclosed
    clean_md = EvalReport(entries=(on,)).to_markdown()
    assert "off-target" not in clean_md.lower()


def test_same_type_contradiction_off_the_planted_sentence_is_not_a_catch():
    """Pipeline regression (Codex HIGH): a CONTRADICTED finding of the SAME
    claim type against a truthful secondary sentence must not be credited as
    catching the planted lie. The catch must match the planted sentence."""
    from notary.demo.seeder import CatalogEntry
    from notary.eval import score_entry
    from notary.types import Claim, Finding, Verdict

    entry = CatalogEntry(
        "dim_customers", "email", "Customer email address. Never null.",
        True, ClaimType.COMPLETENESS, "~5 percent null",
        planted_text="Never null.",
    )
    urn = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)"

    def finding(text):
        return Finding(
            claim=Claim(
                asset_urn=urn, field_path="email",
                claim_type=ClaimType.COMPLETENESS, text=text, predicate={},
            ),
            verdict=Verdict.CONTRADICTED, evidence={}, rationale="synthetic",
        )

    # same type, different sentence: NOT a catch, disclosed
    off = score_entry(entry, (finding("Customer email address."),))
    assert off.outcome == "missed"
    assert off.off_target_contradictions == 1

    # the planted sentence itself (or a quote containing it): a catch
    assert score_entry(entry, (finding("Never null."),)).outcome == "caught"
    assert score_entry(
        entry, (finding("Customer email address. Never null."),)
    ).outcome == "caught"

    # cycle-2 adversarial regression: a FRAGMENT of the planted sentence
    # ("null.", bare punctuation) must NOT count as a catch; the claimed
    # sentence has to contain the full planted assertion
    frag = score_entry(entry, (finding("null."),))
    assert frag.outcome == "missed"
    assert frag.off_target_contradictions == 1
    assert score_entry(entry, (finding("."),)).outcome == "missed"


def test_untyped_manifest_entry_is_rejected_fast():
    """Fleet-review regression (abstained A3, adjudicated inline): the seeder
    type allows claim_type=None, but rows()/to_markdown() would KeyError(None)
    AFTER all pipeline work. evaluate() must reject the shape up front with a
    clear error instead of crashing at render time."""
    from notary.demo.seeder import CatalogEntry, Manifest

    untyped = CatalogEntry(
        "dim_products", "name", "Product display name.", False, None, "n/a"
    )
    with pytest.raises(ValueError, match="claim_type"):
        evaluate(Manifest(claims=(untyped,)), None, _ManifestLLM())


def test_cli_fails_fast_on_missing_or_empty_fixtures_dir(tmp_path):
    """Fleet-review regression (confirmed WARNING, fail-open exit code): a
    missing or empty fixtures directory is a setup error, not an eval result.
    The CLI must exit nonzero BEFORE printing a degenerate all-miss table."""
    base = [sys.executable, "-m", "notary.eval", "--db", str(tmp_path / "wh.duckdb")]
    missing = subprocess.run(
        base + ["--fixtures", str(tmp_path / "nope")],
        capture_output=True, text=True, timeout=120,
    )
    assert missing.returncode == 2
    assert "fixtures" in missing.stderr.lower()
    assert "| claim type |" not in missing.stdout

    empty = tmp_path / "empty"
    empty.mkdir()
    emptied = subprocess.run(
        base + ["--fixtures", str(empty)],
        capture_output=True, text=True, timeout=120,
    )
    assert emptied.returncode == 2


def test_cli_rejects_partially_populated_fixture_store(tmp_path):
    """Pipeline regression (P1 thread PRRT..2P + Codex HIGH x2): a store
    missing ANY required prompt fixture beyond the known provider block is a
    setup error. Without this gate, deleting one fixture (or pointing at a
    store with a single matching key) changes the table and still exits 0."""
    import shutil

    root = Path(__file__).parent.parent
    fx = tmp_path / "fx"
    shutil.copytree(root / "tests/fixtures/llm", fx)
    # remove one required, non-blocked fixture (the cents-lie prompt)
    from notary.extract import SYSTEM_PROMPT, _prompt_key, _user_prompt

    urn = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"
    key = _prompt_key(
        SYSTEM_PROMPT, _user_prompt(urn, "amount", "Transaction amount in USD.")
    )
    (fx / f"{key}.json").unlink()

    r = subprocess.run(
        [sys.executable, "-m", "notary.eval",
         "--db", str(tmp_path / "wh.duckdb"), "--fixtures", str(fx)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 2
    assert "amount" in r.stderr  # names what is missing
    assert "| claim type |" not in r.stdout

    # a store with no matching keys at all fails the same way
    bogus = tmp_path / "bogus"
    bogus.mkdir()
    (bogus / "0000000000000000000000ff.json").write_text(
        json.dumps({"completion": "[]"})
    )
    r2 = subprocess.run(
        [sys.executable, "-m", "notary.eval",
         "--db", str(tmp_path / "wh.duckdb"), "--fixtures", str(bogus)],
        capture_output=True, text=True, timeout=120,
    )
    assert r2.returncode == 2


def test_cli_exits_nonzero_on_unexpected_extraction_failure(tmp_path):
    """Companion regression: all required fixtures present, but one is
    malformed, so its entry errors at extraction. The table still prints
    (disclosed) but the process signal must be failure, not success."""
    import shutil

    root = Path(__file__).parent.parent
    fx = tmp_path / "fx"
    shutil.copytree(root / "tests/fixtures/llm", fx)
    from notary.extract import SYSTEM_PROMPT, _prompt_key, _user_prompt

    urn = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"
    key = _prompt_key(
        SYSTEM_PROMPT, _user_prompt(urn, "amount", "Transaction amount in USD.")
    )
    (fx / f"{key}.json").write_text(json.dumps({"completion": "not json ["}))

    r = subprocess.run(
        [sys.executable, "-m", "notary.eval",
         "--db", str(tmp_path / "wh.duckdb"), "--fixtures", str(fx)],
        capture_output=True, text=True, timeout=120,
    )
    assert "| claim type |" in r.stdout  # still disclosed
    assert r.returncode == 3


def test_readme_table_is_the_verbatim_replay_output(tmp_path):
    """S7 slice 4: the ONE behavior this locks - the table published in the
    README is byte-for-byte what the replay run emits. A hand-edited or stale
    README number fails this test; the published table stays evidence."""
    from notary.demo.seeder import ANCHOR_DATE

    root = Path(__file__).parent.parent
    db = tmp_path / "wh.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        # same invocation the CLI uses, anchor included
        report = evaluate(
            MANIFEST, con, ReplayLLM(root / "tests/fixtures/llm"),
            as_of=ANCHOR_DATE,
        )
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
