"""S5 slice 1: the second agent's catalog context is canonical and stable.

The ONE behavior this locks: the context block the next agent reasons over
is a pure, deterministic function of the catalog state (description, trust
ledger, corrected text), independent of volatile fields (dossier urns,
timestamps, key order), so the answer boundary can replay captured
completions. Without this, S5's before/after comparison is unreplayable.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from s5_next_agent import build_prompt, canonical_context


def _asset(with_ledger: bool):
    asset = {
        "description": "Transaction amount in USD.",
        "field_descriptions": {"amount": "Transaction amount in USD."},
        "trust": {},
    }
    if with_ledger:
        asset["trust"] = {
            "verdict": "CONTRADICTED",
            "verified_at": "2026-07-18",
            "corrected": (
                "amount: measured median 12800 with every value an integer; "
                "consistent with integer cents. [Notary 2026-07-18]"
            ),
            "dossier_findings": [
                "field=amount; verdict=CONTRADICTED; rationale=described "
                "as USD but every value is an integer",
            ],
        }
    return asset


def test_context_is_deterministic_and_ledger_sensitive():
    before = canonical_context(_asset(with_ledger=False))
    after = canonical_context(_asset(with_ledger=True))
    assert before == canonical_context(_asset(with_ledger=False))  # stable
    assert after == canonical_context(_asset(with_ledger=True))
    assert before != after
    assert "CONTRADICTED" in after
    assert "CONTRADICTED" not in before
    assert "evidence dossier" in after  # the dossier lines ride along
    # volatile fields never leak into the context
    for volatile in ("urn:li:document", "T0", "latency"):
        assert volatile not in after


def test_prompt_binds_the_question_to_catalog_context_only():
    prompt = build_prompt(canonical_context(_asset(with_ledger=True)))
    assert "ONLY" in prompt  # grounded: catalog context is the sole source
    assert "amount" in prompt
    assert "CONTRADICTED" in prompt


def test_context_preserves_field_to_verdict_association():
    """PR6 cycle-1 regression (HIGH): each dossier contributes ONE line
    binding field, verdict, and rationale together; flattening into
    independent sorted strings loses which verdict belongs to which field
    on multi-finding assets."""
    after = canonical_context(_asset(with_ledger=True))
    assert "field=amount; verdict=CONTRADICTED" in after


def test_context_normalizes_run_dates_for_replay():
    """PR6 cycle-1 regression (P2): run-specific ISO dates would break
    replay when the asset is re-notarized on a later date; the canonical
    context normalizes them to a placeholder."""
    after = canonical_context(_asset(with_ledger=True))
    assert "2026-07-18" not in after
    assert "<run-date>" in after


def test_prompt_fences_catalog_text_as_untrusted_data():
    """PR6 cycle-1 regression (adversarial HIGH): catalog-derived text is
    mutable and can carry instruction-like content; the prompt fences it
    as DATA and says instructions inside it are never followed."""
    prompt = build_prompt(canonical_context(_asset(with_ledger=True)))
    assert "BEGIN CATALOG DATA" in prompt
    assert "END CATALOG DATA" in prompt
    assert "not instructions" in prompt.lower()


def test_prompt_requires_measurement_hedged_language():
    """PR6 cycle-1 regression (HIGH): the adjudicator never asserts an
    inferred unit as fact; the answering agent is instructed the same way
    (state measurements; interpretations are consistent-with, not
    established fact)."""
    prompt = build_prompt(canonical_context(_asset(with_ledger=True)))
    assert "consistent with" in prompt.lower()


def test_question_binds_to_the_asset():
    """PR6 cycle-1 regression (P2): the question names the asset actually
    selected, not a hardcoded table."""
    import s5_next_agent as s5

    q = s5.build_question(
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.stg_inventory,PROD)"
    )
    assert "stg_inventory" in q
    assert "fct_payments" not in q
