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
