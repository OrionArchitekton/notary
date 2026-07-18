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
import json

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


def test_replay_fixture_must_match_its_prompt(tmp_path):
    """Cycle-3 adversarial regression (HIGH): a fixture file placed under the
    wrong prompt-key filename must not silently supply its completion. The
    stored user prompt is verified against the requesting prompt."""
    import json as _json

    import pytest as _pytest

    from notary.extract import SYSTEM_PROMPT, ReplayLLM, _prompt_key

    key = _prompt_key(SYSTEM_PROMPT, "prompt A")
    (tmp_path / f"{key}.json").write_text(
        _json.dumps({"completion": "[]", "user": "prompt B", "meta": {}})
    )
    llm = ReplayLLM(tmp_path)
    with _pytest.raises(ValueError, match="prompt"):
        llm.complete(SYSTEM_PROMPT, "prompt A")


def test_fabricated_nullable_predicate_is_dropped():
    """PR3 cycle-2 regression (Codex HIGH): a completeness predicate must be
    entailed by the quoted sentence. Quoting a real but claim-free sentence
    while supplying nullable:false would otherwise manufacture CONTRADICTED
    verdicts downstream."""
    canned = (
        '[{"claim_type": "completeness", '
        '"text": "Customer email address.", '
        '"predicate": {"nullable": false}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        descriptions={"email": "Customer email address."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_stated_never_null_predicate_survives():
    """Companion: the stated forms in the seeded catalog (never null, always
    populated, required) entail nullable:false and survive."""
    for text in ("Never null.", "Always populated.", "Required field."):
        canned = json.dumps([{
            "claim_type": "completeness",
            "text": text,
            "predicate": {"nullable": False},
        }])
        claims = extract_claims(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            descriptions={"c": text},
            llm=_CannedLLM(canned),
        )
        assert len(claims) == 1, text


def test_fabricated_enum_values_are_dropped():
    """PR3 cycle-2 regression: every claimed enum value must appear in the
    quoted sentence; invented values are dropped."""
    canned = (
        '[{"claim_type": "domain_enum", '
        '"text": "Order status, one of {placed, shipped}.", '
        '"predicate": {"values": ["placed", "shipped", "cancelled"]}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_orders,PROD)",
        descriptions={"status": "Order status, one of {placed, shipped}."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_fabricated_numeric_bound_is_dropped():
    """PR3 cycle-2 regression: a numeric bound must be stated in the quoted
    sentence (literally, or via non-negative language for min 0)."""
    canned = (
        '[{"claim_type": "domain_enum", '
        '"text": "Units on hand.", '
        '"predicate": {"min": 0}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.stg_inventory,PROD)",
        descriptions={"qty": "Units on hand."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_nonnegative_language_entails_min_zero():
    """Companion: 'Non-negative.' states min 0 and survives."""
    canned = (
        '[{"claim_type": "domain_enum", '
        '"text": "Units on hand. Non-negative.", '
        '"predicate": {"min": 0}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.stg_inventory,PROD)",
        descriptions={"qty": "Units on hand. Non-negative."},
        llm=_CannedLLM(canned),
    )
    assert len(claims) == 1


def test_enum_value_fragments_are_not_entailed():
    """Cycle-3 regression (Codex HIGH): entailment is token-aware; 'US' is
    not stated by a sentence containing only 'USD'."""
    canned = (
        '[{"claim_type": "domain_enum", '
        '"text": "ISO-4217 currency code, one of {USD, EUR, GBP}.", '
        '"predicate": {"values": ["US"]}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        descriptions={"currency": "ISO-4217 currency code, one of {USD, EUR, GBP}."},
        llm=_CannedLLM(canned),
    )
    assert claims == []


def test_numeric_bound_fragments_are_not_entailed():
    """Cycle-3 regression: '5' is not stated by '50', and '2' is not stated
    by '12'; numeric bounds match as whole tokens."""
    for text, predicate in (
        ("Quantity with a maximum of 50.", {"max": 5}),
        ("Value ranges from 0 to 12.", {"min": 2}),
    ):
        canned = json.dumps([{
            "claim_type": "domain_enum", "text": text, "predicate": predicate,
        }])
        claims = extract_claims(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            descriptions={"v": text},
            llm=_CannedLLM(canned),
        )
        assert claims == [], (text, predicate)


def test_negated_required_is_not_a_never_null_claim():
    """Cycle-3 regression (Codex HIGH): 'not required' must not entail
    nullable:false via the 'required' surface form."""
    for text in ("This field is not required.", "Optional and not required."):
        canned = json.dumps([{
            "claim_type": "completeness", "text": text,
            "predicate": {"nullable": False},
        }])
        claims = extract_claims(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
            descriptions={"v": text},
            llm=_CannedLLM(canned),
        )
        assert claims == [], text


def test_open_set_enum_wording_is_not_entailed():
    """Cycle-3 regression (adversarial HIGH): example wording ('include')
    does not state a closed set; only closed-set phrasing (one of, must be,
    braces) entails an enum predicate."""
    open_text = "Currencies include USD and EUR."
    canned = json.dumps([{
        "claim_type": "domain_enum", "text": open_text,
        "predicate": {"values": ["USD", "EUR"]},
    }])
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
        descriptions={"v": open_text},
        llm=_CannedLLM(canned),
    )
    assert claims == []

    closed_text = "Currency, one of {USD, EUR}."
    canned2 = json.dumps([{
        "claim_type": "domain_enum", "text": closed_text,
        "predicate": {"values": ["USD", "EUR"]},
    }])
    claims2 = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
        descriptions={"v": closed_text},
        llm=_CannedLLM(canned2),
    )
    assert len(claims2) == 1


def test_fabricated_deprecated_predicate_is_dropped():
    """Deprecation entailment: deprecated:true must be stated by deprecation
    language in the quoted sentence, not fabricated onto ordinary text."""
    canned = (
        '[{"claim_type": "deprecation_usage", '
        '"text": "Order history table.", '
        '"predicate": {"deprecated": true}}]'
    )
    claims = extract_claims(
        asset_urn="urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.t,PROD)",
        descriptions={None: "Order history table."},
        llm=_CannedLLM(canned),
    )
    assert claims == []
