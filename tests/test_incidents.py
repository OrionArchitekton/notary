"""S4 slice 1: the incident policy as a pure function.

The ONE behavior this locks: an asset with CONTRADICTED findings drafts
exactly one provenance-labeled incident carrying the contradicted claims and
their measured rationales; an asset with no contradiction drafts nothing.
Without this, "dangerously wrong context raises an incident" is vibes.
"""
from notary.incidents import draft_incident
from notary.types import Claim, ClaimType, Finding, Verdict

URN = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"


def _finding(verdict, text="Transaction amount in USD.", rationale="synthetic"):
    return Finding(
        claim=Claim(
            asset_urn=URN, field_path="amount",
            claim_type=ClaimType.UNIT_SCALE, text=text, predicate={"unit": "USD"},
        ),
        verdict=verdict, evidence={"median": 12800.0}, rationale=rationale,
    )


def test_contradicted_findings_draft_one_incident():
    findings = [
        _finding(Verdict.CONTRADICTED, rationale="integer cents, not dollars"),
        _finding(Verdict.CONFIRMED, text="List price in USD."),
    ]
    draft = draft_incident(URN, findings, run_date="2026-07-18")
    assert draft is not None
    assert draft.resource_urn == URN
    assert "fct_payments" in draft.title
    # the contradicted claim and its measured rationale are in the body,
    # the confirmed one is not
    assert "Transaction amount in USD." in draft.description
    assert "integer cents, not dollars" in draft.description
    assert "List price" not in draft.description
    # provenance labeling (spec constraint): attributable to Notary + run date
    assert "Notary" in draft.description
    assert "2026-07-18" in draft.description


def test_no_contradictions_draft_nothing():
    findings = [
        _finding(Verdict.CONFIRMED),
        _finding(Verdict.UNVERIFIABLE),
    ]
    assert draft_incident(URN, findings, run_date="2026-07-18") is None
