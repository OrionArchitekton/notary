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
    """Write a Finding back to the graph through the MCP mutation tools."""

    def __init__(self, gms_url: str):
        self.gms_url = gms_url

    async def write_finding(self, finding) -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        run_date = _run_date()
        claim = finding.claim
        doc_title = (
            f"Notary evidence: {_short_asset(claim.asset_urn)}.{claim.field_path} "
            f"({run_date})"
        )
        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = self.gms_url
        env["TOOLS_IS_MUTATION_ENABLED"] = "true"
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_server_datahub"], env=env
        )
        receipt: dict = {}
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                res = await session.call_tool(
                    "add_structured_properties",
                    {
                        "property_values": {
                            TRUST_VERDICT_URN: [finding.verdict.value],
                            TRUST_VERIFIED_AT_URN: [run_date],
                            TRUST_EVIDENCE_URN: [doc_title],
                        },
                        "entity_urns": [claim.asset_urn],
                    },
                )
                receipt["ledger"] = not res.isError

                res = await session.call_tool(
                    "save_document",
                    {
                        "document_type": "Analysis",
                        "title": doc_title,
                        "content": _dossier_markdown(finding, run_date),
                        "related_assets": [claim.asset_urn],
                    },
                )
                receipt["document"] = not res.isError

                if finding.verdict.value == "CONTRADICTED" and claim.field_path:
                    res = await session.call_tool(
                        "update_description",
                        {
                            "entity_urn": claim.asset_urn,
                            "operation": "replace",
                            "column_path": claim.field_path,
                            "description": _corrected_description(finding, run_date),
                        },
                    )
                    receipt["description"] = not res.isError
                else:
                    receipt["description"] = None
        return receipt


def _short_asset(urn: str) -> str:
    inner = urn.rsplit(",", 2)
    return inner[-2].split(".")[-1] if len(inner) >= 2 else urn


def _dossier_markdown(finding, run_date: str) -> str:
    claim = finding.claim
    return (
        f"# Notary evidence dossier\n\n"
        f"- Asset: {claim.asset_urn}\n"
        f"- Field: {claim.field_path}\n"
        f"- Claim ({claim.claim_type.value}): \"{claim.text}\"\n"
        f"- Verdict: {finding.verdict.value}\n"
        f"- Run date: {run_date}\n"
        f"- Rationale: {finding.rationale}\n\n"
        f"## Probe SQL\n\n```sql\n{_probe_sql(finding)}\n```\n\n"
        f"## Measurements\n\n```json\n{json.dumps(finding.evidence, indent=1, default=str)}\n```\n\n"
        f"Written by Notary (the context lie detector). This dossier is "
        f"machine-generated evidence; the next agent reading this asset "
        f"inherits it.\n"
    )


def _probe_sql(finding) -> str:
    # evidence carries measurements; the SQL travels on the spec when present
    return getattr(getattr(finding, "probe_spec", None), "sql", "") or "(see probe module)"


def _corrected_description(finding, run_date: str) -> str:
    claim = finding.claim
    if "cents" in finding.rationale:
        corrected = "Transaction amount in integer cents."
    else:
        corrected = f"[claim under review: {claim.text}]"
    return (
        f"{corrected} [Corrected by Notary {run_date}: previous description "
        f"said \"{claim.text}\" but {finding.rationale}.]"
    )
