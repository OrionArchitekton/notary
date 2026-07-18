"""S4: raise a provenance-labeled incident on DANGEROUSLY wrong context.

Policy is a pure function (findings + usage evidence -> at most one
IncidentDraft per asset). Spec S4 qualification, fail-closed: only a
unit/scale contradiction on a HIGH-USAGE asset pages anyone; without usage
evidence the qualification cannot be established and nothing is drafted.
Transport is OSS GraphQL (raiseIncident / updateIncidentStatus, verified
against the local quickstart). GraphQL errors raise, never swallowed.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

from notary.types import ClaimType, Finding, Verdict

# spec S4 v1 danger bar: roughly daily-or-better use over the last month
HIGH_USAGE_QUERY_FLOOR = 30


@dataclass(frozen=True)
class UsageEvidence:
    queries_last_30d: int
    distinct_users: int
    source: str


@dataclass(frozen=True)
class IncidentDraft:
    resource_urn: str
    title: str
    description: str


def _short_asset(urn: str) -> str:
    inner = urn.rsplit(",", 1)[0]
    return inner.split(",")[-1] if "," in inner else urn


def draft_incident(
    asset_urn: str,
    findings: list[Finding],
    run_date: str,
    usage: UsageEvidence | None,
) -> IncidentDraft | None:
    """Spec S4: a CONTRADICTED unit/scale claim on a high-usage asset drafts
    ONE incident naming the claims, the evidence, and the affected-usage
    summary. Fail-closed on every leg: no unit/scale contradiction, no usage
    evidence, or usage below the floor each draft NOTHING."""
    dangerous = [
        f for f in findings
        if f.verdict is Verdict.CONTRADICTED
        and f.claim.claim_type is ClaimType.UNIT_SCALE
    ]
    if not dangerous:
        return None
    if usage is None or usage.queries_last_30d < HIGH_USAGE_QUERY_FLOOR:
        return None
    asset = _short_asset(asset_urn)
    lines = [
        f"Notary contradicted {len(dangerous)} unit/scale claim(s) on "
        f"`{asset}` against measured warehouse reality:",
        "",
    ]
    for f in dangerous:
        field = f.claim.field_path or "(table)"
        lines.append(f"- `{field}`: \"{f.claim.text}\" is contradicted: "
                     f"{f.rationale}")
    lines += [
        "",
        f"Affected usage: {usage.queries_last_30d} queries in the last 30 "
        f"days by {usage.distinct_users} distinct user(s) "
        f"({usage.source}). A unit/scale lie at this usage level propagates "
        f"wrong magnitudes into everything reading this asset.",
        "",
        f"Raised by Notary run {run_date}. Evidence dossiers and the trust "
        f"ledger on the asset carry the probe SQL and measurements.",
    ]
    return IncidentDraft(
        resource_urn=asset_urn,
        title=f"Notary: dangerous unit/scale lie on {asset}",
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


def fetch_usage(gms_url: str, asset_urn: str) -> UsageEvidence | None:
    """Read the asset's usage stats from DataHub (MONTH window). Returns
    None when no usage evidence exists; the policy then fails closed."""
    data = _graphql(
        gms_url,
        "query($urn: String!) { dataset(urn: $urn) { "
        "usageStats(range: MONTH) { aggregations "
        "{ totalSqlQueries uniqueUserCount } } } }",
        {"urn": asset_urn},
    )
    agg = (((data.get("dataset") or {}).get("usageStats") or {})
           .get("aggregations") or {})
    queries = agg.get("totalSqlQueries")
    if not queries:
        return None
    return UsageEvidence(
        queries_last_30d=int(queries),
        distinct_users=int(agg.get("uniqueUserCount") or 0),
        source="datahub usageStats MONTH",
    )


def find_open_notary_incident(
    gms_url: str, asset_urn: str, title: str
) -> str | None:
    """An ACTIVE incident with the same Notary title, if one exists
    (idempotency: re-running a day's verdicts must not page twice).
    Paginates the full ACTIVE list (cycle-2 finding: a single 50-count page
    would miss a matching incident on a heavily paged asset)."""
    start, page = 0, 50
    while True:
        data = _graphql(
            gms_url,
            "query($urn: String!, $start: Int!, $count: Int!) "
            "{ dataset(urn: $urn) { "
            "incidents(state: ACTIVE, start: $start, count: $count) { "
            "incidents { urn title } } } }",
            {"urn": asset_urn, "start": start, "count": page},
        )
        incidents = (((data.get("dataset") or {}).get("incidents") or {})
                     .get("incidents") or [])
        for inc in incidents:
            if inc.get("title") == title:
                return inc.get("urn")
        if len(incidents) < page:
            return None
        start += page


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


def raise_incident_idempotent(gms_url: str, draft: IncidentDraft) -> tuple[str, bool]:
    """Raise unless an ACTIVE incident with this title already exists.
    Returns (incident_urn, created).

    Idempotency is EVENTUAL: the incident search index refreshes
    asynchronously, so two raises within the same refresh window (roughly
    seconds) can both create. Re-runs at human timescales are deduped."""
    existing = find_open_notary_incident(gms_url, draft.resource_urn, draft.title)
    if existing:
        return existing, False
    return raise_incident(gms_url, draft), True


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
