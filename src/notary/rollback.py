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

_CORRECTION_RE = re.compile(
    r'^\[Contradicted by Notary \d{4}-\d{2}-\d{2}\] The prior description '
    r'said "(.+?)", but the stored values are inconsistent with it \(',
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
    when the text does not match that exact format (foreign or hand-edited
    descriptions are never touched)."""
    m = _CORRECTION_RE.match(text)
    return m.group(1) if m else None


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

    # Incident: resolve the open Notary incident, if any.
    try:
        incident = find_open_notary_incident(
            args.gms, args.asset, incident_title(args.asset)
        )
        if incident:
            resolve_incident(args.gms, incident, note="Rolled back by Notary")
        receipt["legs"]["incident"] = {"resolved": incident or None}
    except Exception as e:
        receipt["legs"]["incident"] = {"error": str(e)[:200]}
        failed = True

    # Dossier documents: soft-delete the ones the ledger names.
    try:
        if dossiers:
            _soft_delete(args.gms, dossiers)
        receipt["legs"]["dossiers"] = {"deleted": dossiers}
    except Exception as e:
        receipt["legs"]["dossiers"] = {"error": str(e)[:200]}
        failed = True

    # Ledger last, so a failed earlier leg leaves the evidence pointers in
    # place for a retry.
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
