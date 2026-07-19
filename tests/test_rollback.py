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
