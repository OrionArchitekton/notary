"""Slice 2: claim extraction turns catalog descriptions into typed claims.

The ONE behavior this locks: `extract_claims` converts an asset's catalog
descriptions into typed `Claim` objects through the LLM boundary, and the
boundary is replayed from captured completions keyed by the exact prompt.
The replay store is STRICT: an unknown prompt raises instead of returning
something plausible, so if the extractor's prompt or parsing changes (or the
extractor is deleted), these tests fail rather than green-wash. Fixtures are
real captured completions (scripts/capture_llm_fixtures.py), disclosed in
their sidecar metadata.
"""
import pytest

from notary.extract import ReplayLLM, UnknownPromptError, extract_claims
from notary.types import ClaimType

FIXTURES = "tests/fixtures/llm"


@pytest.fixture(scope="module")
def llm():
    return ReplayLLM(FIXTURES)


def test_cents_lie_description_yields_unit_claim(llm):
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        descriptions={"amount": "Transaction amount in USD."},
        llm=llm,
    )
    unit = [c for c in claims if c.claim_type is ClaimType.UNIT_SCALE]
    assert len(unit) == 1
    assert unit[0].field_path == "amount"
    assert unit[0].predicate.get("unit") == "USD"
    assert unit[0].text == "Transaction amount in USD."


def test_multi_claim_description_yields_multiple_typed_claims(llm):
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        descriptions={"email": "Customer email address. Never null."},
        llm=llm,
    )
    types = {c.claim_type for c in claims}
    assert ClaimType.COMPLETENESS in types


def test_unextractable_description_yields_no_claims(llm):
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        descriptions={"customer_id": "Primary key."},
        llm=llm,
    )
    assert claims == []


def test_replay_is_strict_on_unknown_prompts(tmp_path):
    strict = ReplayLLM(str(tmp_path))  # empty store
    with pytest.raises(UnknownPromptError):
        extract_claims(
            asset_urn="urn:li:dataset:x",
            descriptions={"c": "Anything at all."},
            llm=strict,
        )


class _CannedLLM:
    def __init__(self, payload: str):
        self.payload = payload

    def complete(self, system: str, user: str) -> str:
        return self.payload


def test_ungrounded_claim_is_dropped_not_probed():
    """Pipeline-review regression (FR1/FR4/FR12): a hallucinated or injected
    claim whose text does not occur verbatim in the description must be
    dropped at extraction, so it can never drive a catalog rewrite."""
    canned = (
        '[{"claim_type": "unit_scale", '
        '"text": "Amount is stored in USD dollars.", '
        '"predicate": {"unit": "USD"}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:x",
        descriptions={"amount": "Internal ledger amount."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_malformed_predicate_is_dropped():
    canned = (
        '[{"claim_type": "unit_scale", '
        '"text": "Internal ledger amount.", '
        '"predicate": {"unit": 42}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:x",
        descriptions={"amount": "Internal ledger amount."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_predicate_value_must_be_entailed_by_the_text():
    """Cycle-3 regression: quoting a real sentence while inventing the unit
    must not survive extraction."""
    canned = (
        '[{"claim_type": "unit_scale", '
        '"text": "Session duration in milliseconds.", '
        '"predicate": {"unit": "USD"}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:x",
        descriptions={"duration_ms": "Session duration in milliseconds."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_normalized_percent_unit_survives_entailment_gate():
    """Fleet-review regression (A1): SYSTEM_PROMPT teaches normalized unit
    tokens (percent_0_100) that never occur verbatim in prose, so the
    entailment gate silently dropped every percent claim (real captured
    fixture b2b1f38a demonstrates it). The gate must map known normalized
    spellings to their surface forms; the claim below must survive."""
    canned = (
        '[{"claim_type": "unit_scale", '
        '"text": "Discount percentage between 0 and 100.", '
        '"predicate": {"unit": "percent_0_100"}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_orders,PROD)",
        descriptions={"discount_pct": "Discount percentage between 0 and 100."},
        llm=_CannedLLM(canned),
    )
    assert len(claims) == 1
    assert claims[0].predicate["unit"] == "percent_0_100"


def test_percent_unit_without_stated_range_is_dropped():
    """Pipeline regression (P2 thread PRRT..2Q): percent_0_100 encodes BOTH
    the unit and the 0-to-100 range. A description that says percent but not
    the range does not entail the token; accepting it would let the LLM
    invent the scale."""
    canned = (
        '[{"claim_type": "unit_scale", '
        '"text": "Discount rate as a percent.", '
        '"predicate": {"unit": "percent_0_100"}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_orders,PROD)",
        descriptions={"discount_pct": "Discount rate as a percent."},
        llm=_CannedLLM(canned),
    )
    assert claims == []
