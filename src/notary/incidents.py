"""S4: raise a provenance-labeled incident on dangerously wrong context.

Policy is a pure function (findings -> at most one IncidentDraft per asset);
transport is OSS GraphQL (raiseIncident / updateIncidentStatus, verified
against the local quickstart in the day-0 spike). Fail-closed: GraphQL
errors raise, they are never swallowed into a fake success.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from notary.types import Finding, Verdict


@dataclass(frozen=True)
class IncidentDraft:
    resource_urn: str
    title: str
    description: str


def _short_asset(urn: str) -> str:
    inner = urn.rsplit(",", 1)[0]
    return inner.split(",")[-1] if "," in inner else urn


def draft_incident(
    asset_urn: str, findings: list[Finding], run_date: str
) -> IncidentDraft | None:
    """At most ONE incident per asset per run, listing every contradicted
    claim with its measured rationale. No contradiction, no incident."""
    contradicted = [f for f in findings if f.verdict is Verdict.CONTRADICTED]
    if not contradicted:
        return None
    asset = _short_asset(asset_urn)
    lines = [
        f"Notary contradicted {len(contradicted)} catalog claim(s) on "
        f"`{asset}` against measured warehouse reality:",
        "",
    ]
    for f in contradicted:
        field = f.claim.field_path or "(table)"
        lines.append(f"- `{field}`: \"{f.claim.text}\" is contradicted: "
                     f"{f.rationale}")
    lines += [
        "",
        f"Raised by Notary run {run_date}. Evidence dossiers and the trust "
        f"ledger on the asset carry the probe SQL and measurements.",
    ]
    return IncidentDraft(
        resource_urn=asset_urn,
        title=f"Notary: catalog description contradicted on {asset}",
        description="\n".join(lines),
    )


def _graphql(gms_url: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        f"{gms_url}/api/graphql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("errors"):
        raise RuntimeError(f"graphql call failed: {data['errors']}")
    return data.get("data") or {}


def raise_incident(gms_url: str, draft: IncidentDraft) -> str:
    """Raise the incident; returns the real incident urn or raises."""
    data = _graphql(
        gms_url,
        "mutation($input: RaiseIncidentInput!) { raiseIncident(input: $input) }",
        {
            "input": {
                "type": "OPERATIONAL",
                "title": draft.title,
                "description": draft.description,
                "resourceUrn": draft.resource_urn,
            }
        },
    )
    urn = data.get("raiseIncident")
    if not urn:
        raise RuntimeError("raiseIncident returned no incident urn")
    return urn


def resolve_incident(gms_url: str, incident_urn: str, note: str = "") -> None:
    """Resolve an incident (the reversibility half). Raises on failure."""
    data = _graphql(
        gms_url,
        "mutation($urn: String!, $input: IncidentStatusInput!) "
        "{ updateIncidentStatus(urn: $urn, input: $input) }",
        {
            "urn": incident_urn,
            "input": {"state": "RESOLVED", "message": note or "Resolved by Notary"},
        },
    )
    if not data.get("updateIncidentStatus"):
        raise RuntimeError(f"updateIncidentStatus failed for {incident_urn}")
