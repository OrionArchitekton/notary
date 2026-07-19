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
        "contents": _NOTARY_CONTENTS,
    })
    monkeypatch.setattr(rb, "_search_notary_documents", lambda g, a: [])

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
        "title": "Q3 planning notes", "relatedAssets": [], "contents": "",
    })
    monkeypatch.setattr(rb, "_search_notary_documents", lambda g, a: [])
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


_NOTARY_CONTENTS = (
    "# Notary evidence dossier\n\n- Asset: x\n\nWritten by Notary (the "
    "context lie detector). This dossier is machine-generated evidence; "
    "the next agent reading this asset inherits it.\n"
)


def test_dossier_provenance_is_title_and_related_asset_bound():
    from notary.rollback import dossier_is_notary_authored

    ok = {
        "title": "Notary evidence: fct_payments.amount (2026-07-18)",
        "relatedAssets": [_ASSET],
        "contents": _NOTARY_CONTENTS,
    }
    assert dossier_is_notary_authored(ok, _ASSET)
    assert not dossier_is_notary_authored(
        {"title": "Q3 planning notes", "relatedAssets": [_ASSET],
         "contents": _NOTARY_CONTENTS}, _ASSET
    )
    assert not dossier_is_notary_authored(
        {"title": "Notary evidence: other.x (2026-07-18)",
         "relatedAssets": ["urn:li:dataset:(urn:li:dataPlatform:duckdb,other.x,PROD)"],
         "contents": _NOTARY_CONTENTS},
        _ASSET,
    )
    assert not dossier_is_notary_authored(
        {"title": "Notary evidence: fct_payments.amount (2026-07-18)",
         "relatedAssets": [], "contents": _NOTARY_CONTENTS},
        _ASSET,
    )


def test_dossier_provenance_requires_notary_content_format(monkeypatch=None):
    """Pipeline finding (PR #10 cycle 2): a title is user-controlled. The
    document must also carry Notary's own machine format (header + footer)
    before rollback will delete it; a human note wearing the title is
    refused."""
    from notary.rollback import dossier_is_notary_authored

    human_note = {
        "title": "Notary evidence: fct_payments.amount (2026-07-18)",
        "relatedAssets": [_ASSET],
        "contents": "Reminder to self: check this column next sprint.",
    }
    assert not dossier_is_notary_authored(human_note, _ASSET)


def test_soft_delete_false_result_raises(monkeypatch):
    """Pipeline finding (PR #10 cycle 2): a valid GraphQL response carrying
    batchUpdateSoftDeleted=false is a FAILED delete and must raise, so the
    leg fails and the ledger pointers survive for the retry."""
    import notary.rollback as rb

    monkeypatch.setattr(
        rb, "_graphql", lambda g, q, v: {"batchUpdateSoftDeleted": False}
    )
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        rb._soft_delete("http://gms", ["urn:li:document:x1"])


def test_stacked_corrections_drain_to_the_original():
    """Pipeline finding (PR #10 cycle 2): repeated runs can stack
    corrections; the drain restores the ORIGINAL human description, one
    layer at a time, and stops at the first non-correction text."""
    from notary.rollback import drain_corrections

    inner = (
        '[Contradicted by Notary 2026-07-17] The prior description said '
        '"Original human text.", but the stored values are inconsistent '
        "with it (older evidence). See the Notary evidence dossier before "
        "trusting either statement."
    )
    outer = (
        f'[Contradicted by Notary 2026-07-18] The prior description said '
        f'"{inner}", but the stored values are inconsistent with it '
        f"(newer evidence). See the Notary evidence dossier before "
        f"trusting either statement."
    )
    assert drain_corrections(outer) == "Original human text."
    assert drain_corrections("Plain text.") is None
