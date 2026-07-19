"""Rollback (spec Reversibility): the pure inverses of Notary's write-backs.

The ONE behavior this file locks: rollback only ever undoes state Notary
itself authored, parsed from Notary's own formats, and refuses (returns
None / empty) on anything it does not recognize rather than clobbering
foreign catalog state.
"""
from notary.rollback import (
    dossier_urns_from_evidence,
    original_description_from_correction,
)


def test_dossier_urns_parse_from_ledger_evidence():
    value = (
        "2 claims: 1 CONFIRMED, 1 CONTRADICTED | "
        "urn:li:document:shared-aaa; urn:li:document:shared-bbb"
    )
    assert dossier_urns_from_evidence(value) == [
        "urn:li:document:shared-aaa",
        "urn:li:document:shared-bbb",
    ]


def test_dossier_urns_parse_with_unverifiable_reasons_segment():
    value = (
        "3 claims: 1 CONFIRMED, 1 CONTRADICTED, 1 UNVERIFIABLE | "
        "unverifiable source: no rubric can probe a provenance claim | "
        "urn:li:document:shared-ccc"
    )
    assert dossier_urns_from_evidence(value) == ["urn:li:document:shared-ccc"]


def test_dossier_urns_empty_on_no_dossiers_marker():
    assert dossier_urns_from_evidence("1 claims: 1 UNVERIFIABLE | no dossiers") == []


def test_original_description_recovered_from_notary_correction():
    corrected = (
        '[Contradicted by Notary 2026-07-18] The prior description said '
        '"Transaction amount in USD.", but the stored values are '
        "inconsistent with it (measured median 12795 with integer_share "
        "1.00; described as USD but every value is an integer with median "
        "12795; consistent with integer cents, not dollars). See the Notary "
        "evidence dossier before trusting either statement."
    )
    assert (
        original_description_from_correction(corrected)
        == "Transaction amount in USD."
    )


def test_foreign_description_is_refused():
    assert original_description_from_correction("A perfectly normal doc.") is None
    assert original_description_from_correction(
        "[Contradicted by Notary] malformed, no date"
    ) is None


_ASSET = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"

_FULL_CORRECTION = (
    '[Contradicted by Notary 2026-07-18] The prior description said '
    '"Transaction amount in USD.", but the stored values are '
    "inconsistent with it (measured median 12795 with integer_share "
    "1.00; consistent with integer cents, not dollars). See the Notary "
    "evidence dossier before trusting either statement."
)


def test_correction_with_appended_foreign_text_is_refused():
    """Pipeline finding (PR #10): text a human appended AFTER a Notary
    correction is foreign state; rollback must refuse the whole field
    rather than silently deleting the appended note."""
    edited = _FULL_CORRECTION + "\n\nData-team note: keep as-is until Q3."
    assert original_description_from_correction(edited) is None


def test_nested_correction_recovers_the_inner_correction():
    """A correction applied over an older correction restores the FULL inner
    correction (greedy to the outer boundary), never a truncated prefix."""
    outer = (
        f'[Contradicted by Notary 2026-07-19] The prior description said '
        f'"{_FULL_CORRECTION}", but the stored values are inconsistent '
        f"with it (newer evidence). See the Notary evidence dossier before "
        f"trusting either statement."
    )
    assert original_description_from_correction(outer) == _FULL_CORRECTION


def _notary_props(rb):
    return {
        rb.TRUST_VERDICT_URN: "CONTRADICTED",
        rb.TRUST_EVIDENCE_URN: "1 claims: 1 CONTRADICTED | urn:li:document:x1",
    }


def test_ledger_survives_failed_dossier_leg(monkeypatch):
    """Pipeline finding (PR #10): a failed earlier leg must keep the ledger
    (it carries the dossier urns a retry needs), matching the retry-safety
    comment the code previously contradicted."""
    import notary.rollback as rb

    monkeypatch.setattr(
        rb, "_read_structured_properties", lambda g, a: _notary_props(rb)
    )
    monkeypatch.setattr(rb, "read_descriptions", lambda g, a: {})
    monkeypatch.setattr(rb, "find_open_notary_incident", lambda g, a, t: None)
    monkeypatch.setattr(rb, "_document_info", lambda g, u: {
        "title": "Notary evidence: fct_payments.amount (2026-07-18)",
        "relatedAssets": [_ASSET],
    })

    def boom(gms, urns):
        raise RuntimeError("transient GraphQL failure")

    monkeypatch.setattr(rb, "_soft_delete", boom)
    removed = []
    monkeypatch.setattr(
        rb, "_remove_structured_properties",
        lambda g, a, urns: removed.append(urns),
    )
    rc = rb.main(["--asset", _ASSET])
    assert rc == 1
    assert removed == []


def test_foreign_document_in_evidence_is_never_deleted(monkeypatch):
    """Pipeline finding (PR #10): a urn in the evidence property is a
    POINTER, not proof of authorship; rollback verifies each document is
    Notary-authored for THIS asset before soft-deleting it."""
    import notary.rollback as rb

    monkeypatch.setattr(
        rb, "_read_structured_properties", lambda g, a: _notary_props(rb)
    )
    monkeypatch.setattr(rb, "read_descriptions", lambda g, a: {})
    monkeypatch.setattr(rb, "find_open_notary_incident", lambda g, a, t: None)
    monkeypatch.setattr(rb, "_document_info", lambda g, u: {
        "title": "Q3 planning notes", "relatedAssets": [],
    })
    deleted = []
    monkeypatch.setattr(
        rb, "_soft_delete", lambda g, urns: deleted.append(urns)
    )
    monkeypatch.setattr(
        rb, "_remove_structured_properties", lambda g, a, urns: None
    )
    rc = rb.main(["--asset", _ASSET])
    assert deleted == []
    assert rc == 1


def test_dossier_provenance_is_title_and_related_asset_bound():
    from notary.rollback import dossier_is_notary_authored

    ok = {
        "title": "Notary evidence: fct_payments.amount (2026-07-18)",
        "relatedAssets": [_ASSET],
    }
    assert dossier_is_notary_authored(ok, _ASSET)
    assert not dossier_is_notary_authored(
        {"title": "Q3 planning notes", "relatedAssets": [_ASSET]}, _ASSET
    )
    assert not dossier_is_notary_authored(
        {"title": "Notary evidence: other.x (2026-07-18)",
         "relatedAssets": ["urn:li:dataset:(urn:li:dataPlatform:duckdb,other.x,PROD)"]},
        _ASSET,
    )
    assert not dossier_is_notary_authored(
        {"title": "Notary evidence: fct_payments.amount (2026-07-18)",
         "relatedAssets": []},
        _ASSET,
    )
