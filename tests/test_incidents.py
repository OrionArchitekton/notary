"""S4: the incident policy as a pure function, spec-faithful.

The ONE behavior this locks (spec S4): only a CONTRADICTED verdict whose
blast radius qualifies as dangerous (v1: a unit/scale lie on a HIGH-USAGE
asset) drafts an incident, and the draft carries the affected-usage summary.
Everything else, including contradictions without usage evidence, drafts
NOTHING (fail-closed: no qualification, no page).
"""
from notary.incidents import HIGH_USAGE_QUERY_FLOOR, UsageEvidence, draft_incident
from notary.types import Claim, ClaimType, Finding, Verdict

URN = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"

HIGH_USAGE = UsageEvidence(
    queries_last_30d=940, distinct_users=14, source="datahub usageStats MONTH"
)
LOW_USAGE = UsageEvidence(
    queries_last_30d=2, distinct_users=1, source="datahub usageStats MONTH"
)


def _finding(verdict, claim_type=ClaimType.UNIT_SCALE,
             text="Transaction amount in USD.",
             rationale="integer cents, not dollars"):
    return Finding(
        claim=Claim(
            asset_urn=URN, field_path="amount", claim_type=claim_type,
            text=text, predicate={},
        ),
        verdict=verdict, evidence={"median": 12800.0}, rationale=rationale,
    )


def test_high_usage_unit_lie_drafts_incident_with_usage_summary():
    findings = [
        _finding(Verdict.CONTRADICTED),
        _finding(Verdict.CONFIRMED, text="List price in USD."),
    ]
    draft = draft_incident(URN, findings, run_date="2026-07-18", usage=HIGH_USAGE)
    assert draft is not None
    assert draft.resource_urn == URN
    assert "fct_payments" in draft.title
    assert "Transaction amount in USD." in draft.description
    assert "integer cents, not dollars" in draft.description
    assert "List price" not in draft.description
    # spec S4: affected-usage summary + provenance
    assert "940" in draft.description
    assert "14" in draft.description
    assert "Notary" in draft.description
    assert "2026-07-18" in draft.description


def test_no_usage_evidence_drafts_nothing():
    """Fail-closed: qualification cannot be established without usage."""
    findings = [_finding(Verdict.CONTRADICTED)]
    assert draft_incident(URN, findings, run_date="2026-07-18", usage=None) is None


def test_low_usage_drafts_nothing():
    findings = [_finding(Verdict.CONTRADICTED)]
    assert draft_incident(
        URN, findings, run_date="2026-07-18", usage=LOW_USAGE
    ) is None
    assert LOW_USAGE.queries_last_30d < HIGH_USAGE_QUERY_FLOOR


def test_non_unit_contradictions_draft_nothing_even_at_high_usage():
    """v1 danger class is unit/scale only (spec S4)."""
    findings = [
        _finding(Verdict.CONTRADICTED, claim_type=ClaimType.COMPLETENESS,
                 text="Never null.", rationale="5 percent null"),
        _finding(Verdict.CONTRADICTED, claim_type=ClaimType.FRESHNESS,
                 text="Updated daily.", rationale="51 days stale"),
    ]
    assert draft_incident(
        URN, findings, run_date="2026-07-18", usage=HIGH_USAGE
    ) is None


def test_no_contradictions_draft_nothing():
    findings = [
        _finding(Verdict.CONFIRMED),
        _finding(Verdict.UNVERIFIABLE),
    ]
    assert draft_incident(
        URN, findings, run_date="2026-07-18", usage=HIGH_USAGE
    ) is None
