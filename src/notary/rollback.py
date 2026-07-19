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
    """A dossier is deletable only when its own catalog metadata proves
    Notary authored it FOR this asset: the Notary evidence title marker
    AND the rolled-back asset among its related assets. A urn in the
    ledger's evidence property is a pointer, not proof (PR #10 finding);
    fail closed on either check."""
    title = info.get("title") or ""
    related = info.get("relatedAssets") or []
    return title.startswith("Notary evidence: ") and asset_urn in related


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
    _graphql(
        gms_url,
        "mutation($input: DescriptionUpdateInput!) "
        "{ updateDescription(input: $input) }",
        variables,
    )


def _document_info(gms_url: str, doc_urn: str) -> dict:
    """Title + related-asset urns for a document, normalized for
    dossier_is_notary_authored."""
    data = _graphql(
        gms_url,
        "query($urn: String!) { document(urn: $urn) { info {"
        " title relatedAssets { asset { urn } } } } }",
        {"urn": doc_urn},
    )
    info = ((data.get("document") or {}).get("info") or {})
    related = [
        ((r or {}).get("asset") or {}).get("urn", "")
        for r in (info.get("relatedAssets") or [])
    ]
    return {
        "title": info.get("title") or "",
        "relatedAssets": [u for u in related if u],
    }


def _soft_delete(gms_url: str, urns: list[str]) -> None:
    _graphql(
        gms_url,
        "mutation($input: BatchUpdateSoftDeletedInput!) "
        "{ batchUpdateSoftDeleted(input: $input) }",
        {"input": {"urns": urns, "deleted": True}},
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
    # own correction format; everything else is left untouched.
    restored, refused = [], []
    try:
        for field, text in read_descriptions(args.gms, args.asset).items():
            original = original_description_from_correction(text or "")
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
    # finder returns one at a time, and duplicates can accumulate when a
    # raise races the eventually-consistent incident index; a bounded loop
    # with a seen-set drains them without spinning on a stale index entry.
    resolved_incidents: list[str] = []
    try:
        for _ in range(10):
            incident = find_open_notary_incident(
                args.gms, args.asset, incident_title(args.asset)
            )
            if incident is None or incident in resolved_incidents:
                break
            resolve_incident(args.gms, incident, note="Rolled back by Notary")
            resolved_incidents.append(incident)
        receipt["legs"]["incident"] = {"resolved": resolved_incidents or None}
    except Exception as e:
        receipt["legs"]["incident"] = {"error": str(e)[:200]}
        failed = True

    # Dossier documents: verify each urn the ledger names is a Notary
    # dossier for THIS asset before soft-deleting it (the evidence value
    # is a pointer, not proof of authorship), then delete the verified set.
    verified_docs, refused_docs = [], []
    for doc_urn in dossiers:
        try:
            info = _document_info(args.gms, doc_urn)
        except Exception as e:
            refused_docs.append(f"{doc_urn}: read failed: {str(e)[:120]}")
            failed = True
            continue
        if dossier_is_notary_authored(info, args.asset):
            verified_docs.append(doc_urn)
        else:
            refused_docs.append(f"{doc_urn}: not a Notary dossier for this asset")
            failed = True
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
