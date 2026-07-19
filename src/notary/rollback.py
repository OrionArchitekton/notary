"""Rollback: the single command that removes Notary-authored catalog state
(spec Reversibility).

Every leg is the inverse of a write Notary itself made, parsed from
Notary's own formats, and fail-closed: state the command does not
recognize as Notary-authored is left untouched and reported, never
clobbered. Legs report individually; any failed leg makes the exit code
nonzero with a partial receipt, never a silent success.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

from notary.catalog import (
    TRUST_EVIDENCE_URN,
    TRUST_VERDICT_URN,
    TRUST_VERIFIED_AT_URN,
    read_descriptions,
)
from notary.incidents import (
    find_open_notary_incident,
    incident_title,
    resolve_incident,
)

# Full-format match (never a prefix): appended or altered text after a
# Notary correction is FOREIGN state and refuses the whole field. The
# greedy pre-image group binds to the OUTER format boundary, so a
# correction applied over an older correction restores the full inner
# text instead of truncating at the first quote (PR #10 findings).
_CORRECTION_RE = re.compile(
    r'\[Contradicted by Notary \d{4}-\d{2}-\d{2}\] The prior description '
    r'said "(.+)", but the stored values are inconsistent with it \(.+\)\. '
    r'See the Notary evidence dossier before trusting either statement\.',
    re.DOTALL,
)


def dossier_urns_from_evidence(value: str) -> list[str]:
    """Dossier document urns from the ledger's evidence property value
    ("N claims: tally [| unverifiable ...] | urn1; urn2"). Tokens that are
    not document urns (including the "no dossiers" marker) are ignored."""
    urns = []
    for segment in value.split(" | "):
        for token in segment.split("; "):
            token = token.strip()
            if token.startswith("urn:li:document:"):
                urns.append(token)
    return urns


def original_description_from_correction(text: str) -> str | None:
    """The pre-image quoted inside Notary's own correction format, or None
    when the text is not EXACTLY that format end to end (foreign,
    hand-edited, or appended-to descriptions are never touched)."""
    m = _CORRECTION_RE.fullmatch(text)
    return m.group(1) if m else None


def dossier_is_notary_authored(info: dict, asset_urn: str) -> bool:
    """A dossier is deletable only when its own catalog metadata attests
    Notary authored it FOR this asset: the Notary evidence title marker,
    the rolled-back asset among its related assets, AND the body wearing
    Notary's machine format (the dossier header and the written-by
    footer). A urn in the ledger's evidence property is a pointer, not
    proof, and a title alone is user-controlled (PR #10 findings); fail
    closed on every check. This attestation is format-borne, not
    cryptographic (OSS documents carry no immutable creator identity);
    deletion stays recoverable because it is a soft-delete."""
    title = info.get("title") or ""
    related = info.get("relatedAssets") or []
    contents = info.get("contents") or ""
    return (
        title.startswith("Notary evidence: ")
        and asset_urn in related
        and contents.startswith("# Notary evidence dossier")
        and "Written by Notary (the context lie detector)." in contents
    )


def drain_corrections(text: str) -> str | None:
    """The original description underneath one or more stacked Notary
    corrections (repeated runs can layer them), or None when the text is
    not a Notary correction at all. Each layer must match the full
    correction format; the first non-matching layer is the restore
    target."""
    original = original_description_from_correction(text)
    if original is None:
        return None
    while True:
        deeper = original_description_from_correction(original)
        if deeper is None:
            return original
        original = deeper


def _graphql(gms_url: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{gms_url}/api/graphql", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:300])
    return out.get("data") or {}


def _read_structured_properties(gms_url: str, asset_urn: str) -> dict[str, str]:
    data = _graphql(
        gms_url,
        "query($urn: String!) { dataset(urn: $urn) { structuredProperties {"
        " properties { structuredProperty { urn } values {"
        " ... on StringValue { stringValue } } } } } }",
        {"urn": asset_urn},
    )
    props = (((data.get("dataset") or {}).get("structuredProperties") or {})
             .get("properties") or [])
    out = {}
    for p in props:
        urn = (p.get("structuredProperty") or {}).get("urn", "")
        vals = [v.get("stringValue", "") for v in (p.get("values") or [])]
        out[urn] = vals[0] if vals else ""
    return out


def _remove_structured_properties(
    gms_url: str, asset_urn: str, prop_urns: list[str]
) -> None:
    _graphql(
        gms_url,
        "mutation($input: RemoveStructuredPropertiesInput!) {"
        " removeStructuredProperties(input: $input) { properties {"
        " structuredProperty { urn } } } }",
        {"input": {"assetUrn": asset_urn,
                   "structuredPropertyUrns": prop_urns}},
    )


def _restore_description(
    gms_url: str, asset_urn: str, field: str | None, text: str
) -> None:
    variables: dict = {"input": {
        "description": text, "resourceUrn": asset_urn,
    }}
    if field:
        variables["input"]["subResource"] = field
        variables["input"]["subResourceType"] = "DATASET_FIELD"
    data = _graphql(
        gms_url,
        "mutation($input: DescriptionUpdateInput!) "
        "{ updateDescription(input: $input) }",
        variables,
    )
    # A valid response carrying false is a FAILED restore (same class as
    # the batchUpdateSoftDeleted check): report it, never claim restored.
    if data.get("updateDescription") is not True:
        raise RuntimeError(
            f"updateDescription returned "
            f"{data.get('updateDescription')!r} for {field or '(table)'}"
        )


def _document_info(gms_url: str, doc_urn: str) -> dict:
    """Title, related-asset urns, and contents for a document, normalized
    for dossier_is_notary_authored."""
    data = _graphql(
        gms_url,
        "query($urn: String!) { document(urn: $urn) { info {"
        " title contents { text } relatedAssets { asset { urn } } } } }",
        {"urn": doc_urn},
    )
    info = ((data.get("document") or {}).get("info") or {})
    related = [
        ((r or {}).get("asset") or {}).get("urn", "")
        for r in (info.get("relatedAssets") or [])
    ]
    contents = info.get("contents")
    if isinstance(contents, dict):
        contents = contents.get("text") or ""
    return {
        "title": info.get("title") or "",
        "relatedAssets": [u for u in related if u],
        "contents": contents or "",
    }


def _search_notary_documents(gms_url: str, asset_urn: str) -> list[str]:
    """Urns of live documents related to the asset whose title carries the
    Notary evidence marker, discovered via document search (paginated,
    bounded). The ledger's pointers alone are incomplete: a later run
    overwrites the evidence property, orphaning earlier dossiers (PR #10
    finding); search recovers them. Every candidate is still individually
    verified before deletion."""
    urns: list[str] = []
    start, page, total = 0, 100, 0
    for _ in range(20):  # bound: at most 2000 candidates per rollback
        data = _graphql(
            gms_url,
            "query($input: SearchDocumentsInput!) {"
            " searchDocuments(input: $input) {"
            " total documents { urn info { title } } } }",
            {"input": {"query": "Notary evidence",
                       "relatedAssets": [asset_urn],
                       "start": start, "count": page}},
        )
        result = (data.get("searchDocuments") or {})
        docs = result.get("documents") or []
        for d in docs:
            title = ((d or {}).get("info") or {}).get("title") or ""
            urn = (d or {}).get("urn") or ""
            if urn and title.startswith("Notary evidence: "):
                urns.append(urn)
        start += len(docs)
        total = int(result.get("total") or 0)
        if not docs or start >= total:
            return urns
    # The page bound was exhausted with results remaining. Returning the
    # prefix would let the caller delete it, remove the ledger, and report
    # success while authored state survives (PR #10 cycle-3 finding);
    # discovery must fail so the rollback exits nonzero and retains the
    # ledger for a retry.
    raise RuntimeError(
        f"document search truncated at {start} of {total} results; "
        f"re-run rollback to continue"
    )


def _soft_delete(gms_url: str, urns: list[str]) -> None:
    data = _graphql(
        gms_url,
        "mutation($input: BatchUpdateSoftDeletedInput!) "
        "{ batchUpdateSoftDeleted(input: $input) }",
        {"input": {"urns": urns, "deleted": True}},
    )
    # A valid response carrying false is a FAILED delete (PR #10 finding:
    # recording it as deleted orphans the surviving documents).
    if data.get("batchUpdateSoftDeleted") is not True:
        raise RuntimeError(
            f"batchUpdateSoftDeleted returned "
            f"{data.get('batchUpdateSoftDeleted')!r} for {len(urns)} urns"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m notary.rollback",
        description="Remove all Notary-authored catalog state for an asset: "
        "trust ledger properties, evidence dossiers, provenance-labeled "
        "description corrections, and the open Notary incident.",
    )
    parser.add_argument("--gms", default="http://localhost:8080")
    parser.add_argument("--asset", required=True, help="dataset urn to roll back")
    args = parser.parse_args(argv)

    if not args.asset.startswith("urn:li:dataset:("):
        print("error: --asset must be a dataset urn", file=sys.stderr)
        return 2

    receipt: dict = {"asset": args.asset, "legs": {}}
    failed = False

    # Ledger first: it carries the dossier urns the other legs need.
    try:
        props = _read_structured_properties(args.gms, args.asset)
    except Exception as e:
        print(f"error: cannot read structured properties: {e}", file=sys.stderr)
        return 1
    notary_props = {
        u: v for u, v in props.items()
        if u in (TRUST_VERDICT_URN, TRUST_VERIFIED_AT_URN, TRUST_EVIDENCE_URN)
    }
    dossiers = dossier_urns_from_evidence(notary_props.get(TRUST_EVIDENCE_URN, ""))

    # Descriptions: restore any field whose CURRENT text matches Notary's
    # own correction format, draining stacked corrections down to the
    # original human text (repeated runs can layer them, PR #10 finding);
    # everything else is left untouched.
    restored, refused = [], []
    try:
        for field, text in read_descriptions(args.gms, args.asset).items():
            original = drain_corrections(text or "")
            if original is None:
                continue
            try:
                _restore_description(args.gms, args.asset, field, original)
                restored.append(field or "(table)")
            except Exception as e:
                refused.append(f"{field or '(table)'}: {e}")
                failed = True
    except Exception as e:
        refused.append(f"read: {e}")
        failed = True
    receipt["legs"]["descriptions"] = {"restored": restored, "failed": refused}

    # Incident: resolve EVERY open Notary incident for the asset. The
    # incident index is eventually consistent, so a single empty read is
    # not proof of none (an incident the run just raised can surface
    # seconds later) and a just-resolved incident can linger as a stale
    # entry; a bounded drain requires two consecutive empty reads before
    # declaring the leg done.
    resolved_incidents: list[str] = []
    try:
        empty_reads = 0
        for _ in range(20):
            incident = find_open_notary_incident(
                args.gms, args.asset, incident_title(args.asset)
            )
            if incident is None:
                empty_reads += 1
                if empty_reads >= 2:
                    break
                time.sleep(3)
                continue
            empty_reads = 0
            if incident in resolved_incidents:
                # stale index entry for an incident this run resolved
                time.sleep(2)
                continue
            resolve_incident(args.gms, incident, note="Rolled back by Notary")
            resolved_incidents.append(incident)
        if empty_reads >= 2:
            receipt["legs"]["incident"] = {
                "resolved": resolved_incidents or None
            }
        else:
            # Loop bound exhausted without two consecutive empty reads:
            # the drain is UNCONFIRMED and must fail the leg (PR #10
            # cycle-3 finding: success here would remove the ledger over
            # an undrained incident).
            receipt["legs"]["incident"] = {
                "resolved": resolved_incidents or None,
                "error": "drain bound exhausted before two empty reads",
            }
            failed = True
    except Exception as e:
        receipt["legs"]["incident"] = {"error": str(e)[:200]}
        failed = True

    # Dossier documents: the ledger's pointers UNION search discovery (a
    # later run overwrites the evidence property, orphaning earlier
    # dossiers). Verify each candidate is a Notary dossier for THIS asset
    # before soft-deleting it (a pointer or a title is not proof of
    # authorship), then delete the verified set.
    try:
        discovered = _search_notary_documents(args.gms, args.asset)
    except Exception as e:
        discovered = []
        receipt["legs"]["dossier_discovery"] = {"error": str(e)[:200]}
        failed = True
    candidates = list(dict.fromkeys(dossiers + discovered))
    ledger_set = set(dossiers)
    verified_docs, refused_docs = [], []
    for doc_urn in candidates:
        try:
            info = _document_info(args.gms, doc_urn)
        except Exception as e:
            refused_docs.append(f"{doc_urn}: read failed: {str(e)[:120]}")
            failed = True
            continue
        if dossier_is_notary_authored(info, args.asset):
            verified_docs.append(doc_urn)
        elif doc_urn in ledger_set:
            # The LEDGER claimed this urn as Notary evidence and it is
            # not: an anomaly the operator must see; fail the leg.
            refused_docs.append(f"{doc_urn}: not a Notary dossier for this asset")
            failed = True
        else:
            # Search over-approximates by design; refusing a non-Notary
            # match is the fail-closed filter working, not a failure.
            refused_docs.append(f"{doc_urn}: search candidate refused")
    try:
        if verified_docs:
            _soft_delete(args.gms, verified_docs)
        receipt["legs"]["dossiers"] = {
            "deleted": verified_docs, "refused": refused_docs,
        }
    except Exception as e:
        receipt["legs"]["dossiers"] = {
            "error": str(e)[:200], "refused": refused_docs,
        }
        failed = True

    # Ledger last, and ONLY when every earlier leg succeeded: it carries
    # the dossier pointers a retry needs (PR #10 finding: removing it
    # after a failed leg orphans the surviving documents).
    if failed:
        receipt["legs"]["ledger"] = {
            "removed": [],
            "skipped": "earlier leg failed; ledger retained for retry",
        }
    else:
        try:
            if notary_props:
                _remove_structured_properties(
                    args.gms, args.asset, sorted(notary_props)
                )
            receipt["legs"]["ledger"] = {"removed": sorted(notary_props)}
        except Exception as e:
            receipt["legs"]["ledger"] = {"error": str(e)[:200]}
            failed = True

    print(json.dumps(receipt, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
