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
            "dossier_lines": [
                "Field: amount",
                "Rationale: described as USD but every value is an integer",
                "Verdict: CONTRADICTED",
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
