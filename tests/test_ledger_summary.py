"""S3 spec gap (PR8 pipeline finding): the trust ledger must record WHY a
claim is unverifiable, not only tally it. The ONE behavior this locks: the
ledger's evidence summary carries each UNVERIFIABLE finding's field and
rationale, so a catalog reader sees the reason without leaving the ledger
(dossiers are written only for adjudicated claims)."""
from notary.catalog import _verdict_summary
from notary.types import Claim, ClaimType, Finding, Verdict

URN = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"


def _finding(field, verdict, rationale):
    return Finding(
        claim=Claim(
            asset_urn=URN, field_path=field,
            claim_type=ClaimType.UNIT_SCALE, text="x", predicate={},
        ),
        verdict=verdict, evidence={}, rationale=rationale,
    )


def test_summary_carries_unverifiable_reasons():
    summary = _verdict_summary([
        _finding("amount", Verdict.CONTRADICTED, "cents not dollars"),
        _finding("source", Verdict.UNVERIFIABLE,
                 "no rubric can probe a provenance claim; refusing to guess"),
    ])
    assert "1 CONTRADICTED" in summary and "1 UNVERIFIABLE" in summary
    assert "unverifiable source:" in summary
    assert "no rubric can probe a provenance claim" in summary


def test_summary_without_unverifiable_is_unchanged_shape():
    summary = _verdict_summary([
        _finding("amount", Verdict.CONTRADICTED, "cents not dollars"),
        _finding("currency", Verdict.CONFIRMED, "in the claimed set"),
    ])
    assert summary.startswith("2 claims: ")
    assert "unverifiable" not in summary.lower()
