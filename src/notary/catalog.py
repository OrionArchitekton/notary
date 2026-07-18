"""DataHub-facing surface: ingest the demo catalog, write verdicts back.

Reads use the DataHub SDK; write-back goes through the DataHub MCP server's
mutation tools (TOOLS_IS_MUTATION_ENABLED=true) so the judged loop is the MCP
loop, plus OSS GraphQL for incidents. Every mutation Notary makes is labeled
with Notary provenance in the written surface itself (spec: Provenance).
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

import duckdb

warnings.filterwarnings("ignore", category=Warning, module="datahub")

NOTARY_RUN_DATE_ENV = "NOTARY_RUN_DATE"

_PROP_PREFIX = "urn:li:structuredProperty:notary."
TRUST_VERDICT_URN = f"{_PROP_PREFIX}verdict"
TRUST_VERIFIED_AT_URN = f"{_PROP_PREFIX}verified_at"
TRUST_EVIDENCE_URN = f"{_PROP_PREFIX}evidence"


def _run_date() -> str:
    date = os.environ.get(NOTARY_RUN_DATE_ENV)
    if not date:
        raise RuntimeError(
            f"{NOTARY_RUN_DATE_ENV} must be set (Notary stamps verdicts with an "
            "explicit run date; it never reads the wall clock implicitly)"
        )
    return date


def ensure_trust_properties(gms_url: str) -> None:
    """Create (idempotently) the three Notary trust-ledger property definitions."""
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import (
        PropertyValueClass,
        StructuredPropertyDefinitionClass,
    )

    defs = [
        (
            TRUST_VERDICT_URN,
            "notary.verdict",
            "Notary Verdict",
            "Latest Notary adjudication of this asset's catalog claims.",
            [
                PropertyValueClass(value=v)
                for v in ("CONFIRMED", "CONTRADICTED", "UNVERIFIABLE", "MIXED")
            ],
        ),
        (
            TRUST_VERIFIED_AT_URN,
            "notary.verified_at",
            "Notary Verified At",
            "Run date of the latest Notary verification.",
            None,
        ),
        (
            TRUST_EVIDENCE_URN,
            "notary.evidence",
            "Notary Evidence",
            "Title of the Notary evidence dossier for the latest verification.",
            None,
        ),
    ]
    emitter = DatahubRestEmitter(gms_server=gms_url)
    for urn, qualified_name, display, description, allowed in defs:
        definition = StructuredPropertyDefinitionClass(
            qualifiedName=qualified_name,
            displayName=display,
            description=description,
            valueType="urn:li:dataType:datahub.string",
            cardinality="SINGLE",
            entityTypes=["urn:li:entityType:datahub.dataset"],
            allowedValues=allowed,
        )
        emitter.emit(
            MetadataChangeProposalWrapper(entityUrn=urn, aspect=definition)
        )
    emitter.flush()


_DUCK_TO_SQL = {
    "BIGINT": "bigint",
    "INTEGER": "int",
    "DOUBLE": "double",
    "VARCHAR": "varchar",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
}


def ingest_manifest(db_path: str | Path, manifest, gms_url: str) -> list[str]:
    """Register each warehouse table in DataHub with the manifest's catalog
    descriptions (lies and controls alike). Returns the dataset urns."""
    from datahub.sdk import DataHubClient, Dataset

    by_table: dict[str, dict[str | None, str]] = {}
    for entry in manifest.claims:
        by_table.setdefault(entry.table, {})[entry.column] = entry.description

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        client = DataHubClient(server=gms_url)
        urns: list[str] = []
        for table, descriptions in by_table.items():
            cols = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_name = ? order by ordinal_position",
                [table],
            ).fetchall()
            schema = [
                (
                    name,
                    _DUCK_TO_SQL.get(dtype.upper(), dtype.lower()),
                    descriptions.get(name),
                )
                for name, dtype in cols
            ]
            dataset = Dataset(
                platform="duckdb",
                name=f"fiction_retail.{table}",
                env="PROD",
                description=descriptions.get(None),
                schema=schema,
            )
            client.entities.upsert(dataset)
            urns.append(str(dataset.urn))
        return urns
    finally:
        con.close()


class NotaryWriter:
    """Write an asset's Findings back to the graph through the MCP tools.

    Aggregation happens per ASSET before any write (review finding: the
    verdict property is SINGLE-cardinality per asset, so per-finding writes
    were last-write-wins). Sequence is ledger-gated: if the ledger write
    fails, no dossier or description mutation is attempted. UNVERIFIABLE
    findings produce no dossier and no description change (spec S3).
    """

    def __init__(self, gms_url: str):
        self.gms_url = gms_url

    async def write_findings(self, asset_urn: str, findings: list) -> dict:
        if not findings:
            return {"ledger": None, "documents": [], "descriptions": []}
        if any(f.claim.asset_urn != asset_urn for f in findings):
            raise ValueError("write_findings takes findings for ONE asset")

        run_date = _run_date()
        verdict = _aggregate_verdict(findings)
        dossier_findings = [
            f for f in findings if f.verdict.value in ("CONTRADICTED", "CONFIRMED")
        ]
        doc_titles = {
            id(f): (
                f"Notary evidence: {_short_asset(asset_urn)}."
                f"{f.claim.field_path or '(table)'} ({run_date})"
            )
            for f in dossier_findings
        }
        evidence_value = (
            "; ".join(doc_titles.values())
            if doc_titles
            else "no dossier (all UNVERIFIABLE)"
        )

        receipt: dict = {"documents": [], "descriptions": []}
        async with self._session() as session:
            res = await session.call_tool(
                "add_structured_properties",
                {
                    "property_values": {
                        TRUST_VERDICT_URN: [verdict],
                        TRUST_VERIFIED_AT_URN: [run_date],
                        TRUST_EVIDENCE_URN: [evidence_value],
                    },
                    "entity_urns": [asset_urn],
                },
            )
            receipt["ledger"] = not res.isError
            if res.isError:
                # Ledger is the anchor: without it, evidence and corrected
                # descriptions would be unattributed. Stop here.
                return receipt

            for f in dossier_findings:
                res = await session.call_tool(
                    "save_document",
                    {
                        "document_type": "Analysis",
                        "title": doc_titles[id(f)],
                        "content": _dossier_markdown(f, run_date),
                        "related_assets": [asset_urn],
                    },
                )
                receipt["documents"].append(
                    {"title": doc_titles[id(f)], "ok": not res.isError}
                )

            all_docs_ok = all(d["ok"] for d in receipt["documents"])
            for f in findings:
                if (
                    f.verdict.value == "CONTRADICTED"
                    and f.claim.field_path
                    and all_docs_ok
                ):
                    res = await session.call_tool(
                        "update_description",
                        {
                            "entity_urn": asset_urn,
                            "operation": "replace",
                            "column_path": f.claim.field_path,
                            "description": _corrected_description(f, run_date),
                        },
                    )
                    receipt["descriptions"].append(
                        {"field": f.claim.field_path, "ok": not res.isError}
                    )
        return receipt

    def _session(self):
        from contextlib import asynccontextmanager

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # Minimal child env: the MCP server needs no inherited secrets
        # (review finding: passing the full parent env is a spill surface).
        env = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV")
        }
        env["DATAHUB_GMS_URL"] = self.gms_url
        env["TOOLS_IS_MUTATION_ENABLED"] = "true"
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_server_datahub"], env=env
        )

        @asynccontextmanager
        async def _ctx():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        return _ctx()


def _aggregate_verdict(findings: list) -> str:
    values = {f.verdict.value for f in findings}
    if "CONTRADICTED" in values:
        return "MIXED" if "CONFIRMED" in values else "CONTRADICTED"
    if values == {"CONFIRMED"}:
        return "CONFIRMED"
    if "CONFIRMED" in values:
        return "MIXED"
    return "UNVERIFIABLE"


def _short_asset(urn: str) -> str:
    inner = urn.rsplit(",", 2)
    return inner[-2].split(".")[-1] if len(inner) >= 2 else urn


def _dossier_markdown(finding, run_date: str) -> str:
    claim = finding.claim
    probe_sql = finding.evidence.get("probe_sql", "(no probe SQL recorded)")
    return (
        f"# Notary evidence dossier\n\n"
        f"- Asset: {claim.asset_urn}\n"
        f"- Field: {claim.field_path or '(table-level)'}\n"
        f'- Claim ({claim.claim_type.value}): "{claim.text}"\n'
        f"- Verdict: {finding.verdict.value}\n"
        f"- Run date: {run_date}\n"
        f"- Rationale: {finding.rationale}\n\n"
        f"## Probe SQL\n\n```sql\n{probe_sql}\n```\n\n"
        f"## Measurements\n\n```json\n"
        f"{json.dumps(finding.evidence, indent=1, default=str)}\n```\n\n"
        f"Written by Notary (the context lie detector). This dossier is "
        f"machine-generated evidence; the next agent reading this asset "
        f"inherits it.\n"
    )


def _corrected_description(finding, run_date: str) -> str:
    """Evidence-grounded correction: state the measurements, never assert a
    unit as fact (review finding: the previous hardcoded 'integer cents'
    text could write a fabrication under Notary authority)."""
    claim = finding.claim
    median = finding.evidence.get("median")
    integer_share = finding.evidence.get("integer_share")
    measured = (
        f"measured median {median:.0f} with integer_share {integer_share:.2f}"
        if median is not None and integer_share is not None
        else "measurements in the Notary evidence dossier"
    )
    return (
        f"[Contradicted by Notary {run_date}] The prior description said "
        f'"{claim.text}", but the stored values are inconsistent with it '
        f"({measured}; {finding.rationale}). See the Notary evidence dossier "
        f"before trusting either statement."
    )
