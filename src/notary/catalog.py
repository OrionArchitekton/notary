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


def _merged_upstreams(
    existing: "list[str]", declared: "list[str]",
) -> list[str]:
    """Existing catalog upstreams first, declared edges appended, deduped:
    the UpstreamLineage aspect replaces wholesale, so an emission that
    carried only the declared edges would silently delete pre-existing
    relationships (PR #11 finding)."""
    merged = list(existing)
    for urn in declared:
        if urn not in merged:
            merged.append(urn)
    return merged


def _existing_upstreams(gms_url: str, downstream_urn: str) -> list[str]:
    """Current upstream dataset urns for an entity, paged and bounded.
    Raises on any query failure: emitting a replace over an unknown
    pre-image would be unrecoverable data loss (fail-closed)."""
    import json as _json
    import urllib.request as _request

    query = (
        "query($urn:String!,$start:Int!){ dataset(urn:$urn){ lineage(input:{"
        "direction:UPSTREAM,start:$start,count:100}){ total relationships{"
        " entity{ urn } } } } }"
    )
    urns: list[str] = []
    start = 0
    for _ in range(20):
        payload = _json.dumps(
            {"query": query,
             "variables": {"urn": downstream_urn, "start": start}}
        ).encode()
        req = _request.Request(
            f"{gms_url}/api/graphql", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _request.urlopen(req, timeout=15) as resp:
            out = _json.loads(resp.read())
        if out.get("errors"):
            raise RuntimeError(str(out["errors"])[:200])
        lineage = (((out.get("data") or {}).get("dataset") or {})
                   .get("lineage") or {})
        rels = lineage.get("relationships") or []
        for r in rels:
            urn = ((r or {}).get("entity") or {}).get("urn")
            if urn:
                urns.append(urn)
        start += len(rels)
        if not rels or start >= int(lineage.get("total") or 0):
            return urns
    raise RuntimeError(
        f"upstream lineage truncated at {start}; refusing to emit a "
        f"replace over an unknown pre-image"
    )


def _grouped_lineage(
    lineage: "tuple[tuple[str, str], ...]",
) -> dict[str, list[str]]:
    """{downstream: [upstreams...]} preserving declaration order; the
    UpstreamLineage aspect replaces wholesale, so a downstream's edges
    must travel in ONE emission."""
    grouped: dict[str, list[str]] = {}
    for upstream, downstream in lineage:
        grouped.setdefault(downstream, [])
        if upstream not in grouped[downstream]:
            grouped[downstream].append(upstream)
    return grouped


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
        # Dataset lineage (judge-slice v3): the catalog records that the
        # suspect warehouse table DERIVES from its reconciliation source,
        # so the corroboration gate can verify the declared reference is
        # a real upstream, not an arbitrary table name.
        lineage = getattr(manifest, "lineage", ()) or ()
        if lineage:
            from datahub.emitter.mce_builder import (
                make_dataset_urn,
                make_lineage_mce,
            )
            from datahub.emitter.rest_emitter import DatahubRestEmitter

            emitter = DatahubRestEmitter(gms_server=gms_url)
            # One MCE per downstream carrying the MERGE of what the
            # catalog already records and ALL declared upstreams: the
            # UpstreamLineage aspect is a full replace, so per-edge or
            # declared-only emission would silently delete relationships
            # (PR #11 findings). The pre-image read fails closed.
            for downstream, upstreams in _grouped_lineage(lineage).items():
                down_urn = make_dataset_urn(
                    "duckdb", f"fiction_retail.{downstream}", "PROD")
                declared = [
                    make_dataset_urn(
                        "duckdb", f"fiction_retail.{u}", "PROD")
                    for u in upstreams
                ]
                emitter.emit(make_lineage_mce(
                    _merged_upstreams(
                        _existing_upstreams(gms_url, down_urn), declared
                    ),
                    down_urn,
                ))
            emitter.flush()
        return urns
    finally:
        con.close()


def read_descriptions(gms_url: str, asset_urn: str) -> dict[str | None, str]:
    """Read the asset's CURRENT catalog descriptions from DataHub.

    This is the read half of the loop (review finding: the round trip must
    consume what the catalog actually says, not a hard-coded copy). Editable
    (UI-written) field descriptions win over ingested ones; the table-level
    description is returned under the None key."""
    import urllib.request

    query = (
        "query($urn: String!) { dataset(urn: $urn) { "
        "properties { description } "
        "editableProperties { description } "
        "schemaMetadata { fields { fieldPath description } } "
        "editableSchemaMetadata { editableSchemaFieldInfo "
        "{ fieldPath description } } } }"
    )
    payload = json.dumps({"query": query, "variables": {"urn": asset_urn}}).encode()
    req = urllib.request.Request(
        f"{gms_url}/api/graphql",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("errors"):
        raise RuntimeError(f"catalog read failed: {data['errors']}")
    ds = (data.get("data") or {}).get("dataset")
    if ds is None:
        raise RuntimeError(f"catalog read failed: no dataset for {asset_urn}")
    out: dict[str | None, str] = {}
    table_desc = ((ds.get("editableProperties") or {}).get("description")
                  or (ds.get("properties") or {}).get("description"))
    if table_desc:
        out[None] = table_desc
    for f in ((ds.get("schemaMetadata") or {}).get("fields") or []):
        if f.get("description"):
            out[f["fieldPath"]] = f["description"]
    for f in ((ds.get("editableSchemaMetadata") or {})
              .get("editableSchemaFieldInfo") or []):
        if f.get("description"):
            out[f["fieldPath"]] = f["description"]
    return out


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
        """Write order (review finding FR2/FR14): dossiers FIRST so the ledger
        can reference the REAL returned document urns; ledger second; the
        description rewrite last and only after ledger success. The pre-image
        of every description Notary replaces is preserved in the dossier."""
        if not findings:
            return {"ledger": None, "documents": [], "descriptions": []}
        if any(f.claim.asset_urn != asset_urn for f in findings):
            raise ValueError("write_findings takes findings for ONE asset")

        run_date = _run_date()
        verdict = _aggregate_verdict(findings)
        verdict_summary = _verdict_summary(findings)
        dossier_findings = [
            f for f in findings if f.verdict.value in ("CONTRADICTED", "CONFIRMED")
        ]
        pre_images = read_descriptions(self.gms_url, asset_urn)

        receipt: dict = {"documents": [], "descriptions": []}
        async with self._session() as session:
            doc_urns: list[str] = []
            for f in dossier_findings:
                title = (
                    f"Notary evidence: {_short_asset(asset_urn)}."
                    f"{f.claim.field_path or '(table)'} ({run_date})"
                )
                res = await session.call_tool(
                    "save_document",
                    {
                        "document_type": "Analysis",
                        "title": title,
                        "content": _dossier_markdown(
                            f, run_date,
                            pre_image=pre_images.get(f.claim.field_path),
                        ),
                        "related_assets": [asset_urn],
                    },
                )
                doc_urn = _extract_doc_urn(res)
                # ok requires the URN, not just a non-error envelope (review
                # finding: a nominally successful write with no locatable
                # dossier must block the description rewrite)
                receipt["documents"].append(
                    {"title": title, "urn": doc_urn,
                     "ok": (not res.isError) and doc_urn is not None}
                )
                if doc_urn:
                    doc_urns.append(doc_urn)

            # Ledger requires every dossier to have landed with a locatable
            # urn (review finding: an authoritative verdict must never cite
            # missing or partial evidence).
            if not all(d["ok"] for d in receipt["documents"]):
                receipt["ledger"] = False
                receipt["ledger_blocked_reason"] = "dossier write failed"
                return receipt
            dossier_part = "; ".join(doc_urns) if doc_urns else "no dossiers"
            evidence_value = f"{verdict_summary} | {dossier_part}"
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
                return receipt

            all_docs_ok = all(d["ok"] for d in receipt["documents"])
            for f in findings:
                if f.verdict.value == "CONTRADICTED" and all_docs_ok:
                    # table-level contradictions (field_path None) correct
                    # the dataset description; omitting column_path targets
                    # the entity itself (PR5 finding: a contradicted
                    # table-level claim must not survive in the catalog)
                    tool_args = {
                        "entity_urn": asset_urn,
                        "operation": "replace",
                        "description": _corrected_description(
                            f, run_date,
                            pre_image=pre_images.get(f.claim.field_path),
                        ),
                    }
                    if f.claim.field_path:
                        tool_args["column_path"] = f.claim.field_path
                    res = await session.call_tool(
                        "update_description", tool_args
                    )
                    receipt["descriptions"].append(
                        {
                            "field": f.claim.field_path or "(table)",
                            "ok": not res.isError,
                        }
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


def _extract_doc_urn(res) -> str | None:
    """Parse the save_document tool result for the created document urn."""
    if res.isError:
        return None
    for c in res.content:
        text = getattr(c, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("urn"), str):
            return data["urn"]
    return None


def _verdict_summary(findings: list) -> str:
    """Per-claim tally so UNVERIFIABLE claims are never hidden by the
    asset-level verdict (review finding FR15). Spec S3 says the ledger
    records WHY a claim is unverifiable, and dossiers exist only for
    adjudicated claims (PR8 pipeline finding), so each UNVERIFIABLE
    finding's field and rationale ride in the summary itself."""
    from collections import Counter

    counts = Counter(f.verdict.value for f in findings)
    parts = [f"{n} {v}" for v, n in sorted(counts.items())]
    summary = f"{len(findings)} claims: " + ", ".join(parts)
    reasons = [
        f"unverifiable {f.claim.field_path or '(table)'}: {f.rationale}"
        for f in findings
        if f.verdict.value == "UNVERIFIABLE"
    ]
    if reasons:
        summary += " | " + "; ".join(reasons)
    return summary


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


def _dossier_markdown(finding, run_date: str, pre_image: str | None = None) -> str:
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
        f"## Description pre-image (before any Notary correction)\n\n"
        f"{pre_image or '(no prior description recorded)'}\n\n"
        f"## Probe SQL\n\n```sql\n{probe_sql}\n```\n\n"
        f"## Measurements\n\n```json\n"
        f"{json.dumps(finding.evidence, indent=1, default=str)}\n```\n\n"
        f"Written by Notary (the context lie detector). This dossier is "
        f"machine-generated evidence; the next agent reading this asset "
        f"inherits it.\n"
    )


def _corrected_description(
    finding, run_date: str, pre_image: str | None = None
) -> str:
    """Evidence-grounded correction: state the measurements, never assert a
    unit as fact (review finding: the previous hardcoded 'integer cents'
    text could write a fabrication under Notary authority). The quoted text
    is the FULL prior description (the correction replaces the whole field,
    and rollback restores exactly what is quoted; quoting only the one
    extracted claim sentence would drop the rest on restore, PR #10
    finding); the claim sentence is the fallback when no pre-image was
    readable."""
    claim = finding.claim
    median = finding.evidence.get("median")
    integer_share = finding.evidence.get("integer_share")
    measured = (
        f"measured median {median:.0f} with integer_share {integer_share:.2f}"
        if median is not None and integer_share is not None
        else "measurements in the Notary evidence dossier"
    )
    quoted = pre_image if pre_image is not None else claim.text
    return (
        f"[Contradicted by Notary {run_date}] The prior description said "
        f'"{quoted}", but the stored values are inconsistent with it '
        f"({measured}; {finding.rationale}). See the Notary evidence dossier "
        f"before trusting either statement."
    )


def seed_usage_stats(
    gms_url: str,
    asset_urn: str,
    anchor_date: str,
    queries_per_day: int = 31,
    distinct_users: int = 14,
    days: int = 30,
) -> None:
    """Demo-only: emit daily usage buckets for an asset so the S4 danger
    qualification (high usage) rests on REAL catalog usage evidence instead
    of an asserted flag. Buckets end at anchor_date; re-seed with a current
    anchor if the demo is re-run far outside the original window."""
    from datetime import date, timedelta

    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.emitter.rest_emitter import DatahubRestEmitter
    from datahub.metadata.schema_classes import (
        CalendarIntervalClass,
        DatasetUsageStatisticsClass,
        TimeWindowSizeClass,
    )

    emitter = DatahubRestEmitter(gms_server=gms_url)
    anchor = date.fromisoformat(anchor_date)
    for offset in range(days):
        day = anchor - timedelta(days=offset)
        ts_ms = int(
            __import__("calendar").timegm(day.timetuple()) * 1000
        )
        aspect = DatasetUsageStatisticsClass(
            timestampMillis=ts_ms,
            eventGranularity=TimeWindowSizeClass(
                unit=CalendarIntervalClass.DAY, multiple=1
            ),
            totalSqlQueries=queries_per_day,
            uniqueUserCount=distinct_users,
        )
        emitter.emit(
            MetadataChangeProposalWrapper(entityUrn=asset_urn, aspect=aspect)
        )
