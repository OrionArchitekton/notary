"""Slice 4 (catalog seam): the S1 round trip against a LIVE local DataHub.

The ONE behavior this locks: Notary ingests the fiction-retail catalog (with
its lying descriptions) into DataHub, runs the pipeline on the cents lie, and
writes the verdict BACK to the catalog through the MCP mutation tools; reading
the asset back shows the trust ledger, the evidence dossier link, and the
provenance-labeled corrected column description. This is the "contribute back
to the graph" loop working end to end, not a claim about it.

Marked integration: skipped when no quickstart answers on localhost:8080.
"""
import asyncio
import json
import urllib.error
import urllib.request

import duckdb
import pytest

from notary.catalog import (
    NOTARY_RUN_DATE_ENV,
    TRUST_VERDICT_URN,
    NotaryWriter,
    ensure_trust_properties,
    ingest_manifest,
)
from notary.demo.seeder import DEFAULT_SEED, build_warehouse
from notary.extract import ReplayLLM, extract_claims
from notary.probe import plan_probe, run_probe
from notary.adjudicate import adjudicate
from notary.types import Verdict

GMS = "http://localhost:8080"
PAYMENTS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)"
)


def _gms_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{GMS}/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.integration

if not _gms_alive():
    pytest.skip("no local DataHub quickstart on :8080", allow_module_level=True)


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    db = tmp_path_factory.mktemp("wh") / "fiction_retail.duckdb"
    manifest = build_warehouse(db, seed=DEFAULT_SEED)
    ensure_trust_properties(GMS)
    urns = ingest_manifest(db, manifest, GMS)
    return db, manifest, urns


def test_ingest_registers_lying_catalog(ingested):
    _, _, urns = ingested
    assert PAYMENTS_URN in urns


def test_s1_round_trip_writes_verdict_back(ingested, monkeypatch):
    db, _, _ = ingested
    monkeypatch.setenv(NOTARY_RUN_DATE_ENV, "2026-07-18")

    claims = extract_claims(
        asset_urn=PAYMENTS_URN,
        descriptions={"amount": "Transaction amount in USD."},
        llm=ReplayLLM("tests/fixtures/llm"),
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        finding = adjudicate(claims[0], run_probe(plan_probe(claims[0]), con))
    finally:
        con.close()
    assert finding.verdict is Verdict.CONTRADICTED

    writer = NotaryWriter(GMS)
    receipt = asyncio.run(writer.write_finding(finding))
    assert receipt["ledger"] and receipt["document"] and receipt["description"]

    # Read back: the graph must carry what Notary learned.
    from datahub.sdk import DataHubClient

    ds = DataHubClient(server=GMS).entities.get(PAYMENTS_URN)
    props = {p.propertyUrn: list(p.values) for p in (ds.structured_properties or [])}
    assert TRUST_VERDICT_URN in props
    assert "CONTRADICTED" in [str(v) for v in props[TRUST_VERDICT_URN]]

    # The corrected description lands on the editable (UI-visible) surface.
    q = json.dumps({
        "query": 'query($urn: String!) { dataset(urn: $urn) { '
                 'editableSchemaMetadata { editableSchemaFieldInfo '
                 '{ fieldPath description } } } }',
        "variables": {"urn": PAYMENTS_URN},
    }).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=q, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    fields = payload["data"]["dataset"]["editableSchemaMetadata"][
        "editableSchemaFieldInfo"
    ]
    desc_text = next(
        f["description"] for f in fields if f["fieldPath"] == "amount"
    )
    assert "Notary" in desc_text and "cents" in desc_text
