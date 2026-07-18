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
    read_descriptions,
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


def _reset_editable_description(urn: str, field: str, text: str) -> None:
    """Idempotency guard: a prior run's Notary correction lives in the
    editable overlay; reset it so each run reads the original planted lie."""
    q = json.dumps({
        "query": "mutation($input: DescriptionUpdateInput!) "
                 "{ updateDescription(input: $input) }",
        "variables": {"input": {
            "description": text,
            "resourceUrn": urn,
            "subResource": field,
            "subResourceType": "DATASET_FIELD",
        }},
    }).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=q,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    assert not payload.get("errors"), payload


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    db = tmp_path_factory.mktemp("wh") / "fiction_retail.duckdb"
    manifest = build_warehouse(db, seed=DEFAULT_SEED)
    ensure_trust_properties(GMS)
    urns = ingest_manifest(db, manifest, GMS)
    _reset_editable_description(
        PAYMENTS_URN, "amount", "Transaction amount in USD."
    )
    return db, manifest, urns


def test_ingest_registers_lying_catalog(ingested):
    _, _, urns = ingested
    assert PAYMENTS_URN in urns


def test_s1_round_trip_writes_verdict_back(ingested, monkeypatch):
    db, _, _ = ingested
    monkeypatch.setenv(NOTARY_RUN_DATE_ENV, "2026-07-18")

    # Read the description from the catalog itself (review finding FR0: the
    # loop must consume what DataHub actually says, not a hard-coded copy).
    catalog_descriptions = read_descriptions(GMS, PAYMENTS_URN)
    assert catalog_descriptions.get("amount") == "Transaction amount in USD."
    claims = extract_claims(
        asset_urn=PAYMENTS_URN,
        descriptions={"amount": catalog_descriptions["amount"]},
        llm=ReplayLLM("tests/fixtures/llm"),
    )
    con = duckdb.connect(str(db), read_only=True)
    try:
        finding = adjudicate(claims[0], run_probe(plan_probe(claims[0]), con))
    finally:
        con.close()
    assert finding.verdict is Verdict.CONTRADICTED

    writer = NotaryWriter(GMS)
    receipt = asyncio.run(writer.write_findings(PAYMENTS_URN, [finding]))
    assert receipt["ledger"] is True
    assert receipt["documents"] and all(d["ok"] for d in receipt["documents"])
    assert receipt["descriptions"] and all(d["ok"] for d in receipt["descriptions"])

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


def test_incident_raise_and_resolve_round_trip(tmp_path):
    """S4 tracer: a CONTRADICTED verdict raises a REAL incident on the live
    quickstart via OSS GraphQL, and the incident is resolvable (the
    reversibility half). Returns a real urn:li:incident:* or fails."""
    from notary.incidents import draft_incident, raise_incident, resolve_incident

    db = tmp_path / "wh.duckdb"
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        claims = extract_claims(
            PAYMENTS_URN,
            {"amount": "Transaction amount in USD."},
            ReplayLLM("tests/fixtures/llm"),
        )
        findings = [
            adjudicate(c, run_probe(plan_probe(c), con)) for c in claims
        ]
    finally:
        con.close()
    assert any(f.verdict is Verdict.CONTRADICTED for f in findings)

    draft = draft_incident(PAYMENTS_URN, findings, run_date="2026-07-18")
    assert draft is not None
    incident_urn = raise_incident(GMS, draft)
    assert incident_urn.startswith("urn:li:incident:")
    # reversibility: resolve it (also verifies updateIncidentStatus works)
    resolve_incident(GMS, incident_urn, note="Notary test cleanup")


def test_run_cli_single_asset_end_to_end(tmp_path):
    """The operator command: ONE invocation reads the LIVE catalog, extracts,
    probes, adjudicates, writes the verdicts back, and raises an incident for
    the contradicted asset. This is S1-S5 through the real front door."""
    import os
    import re
    import subprocess
    import sys

    from notary.incidents import resolve_incident

    # reset the planted lie in case a prior run corrected it
    _reset_editable_description(
        PAYMENTS_URN, "amount", "Transaction amount in USD."
    )
    env = {**os.environ, "NOTARY_RUN_DATE": "2026-07-18"}
    r = subprocess.run(
        [
            sys.executable, "-m", "notary.run",
            "--gms", GMS,
            "--db", str(tmp_path / "wh.duckdb"),
            "--fixtures", "tests/fixtures/llm",
            "--asset", PAYMENTS_URN,
        ],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert r.returncode == 0, r.stderr
    assert "CONTRADICTED" in r.stdout
    m = re.search(r"urn:li:incident:\S+", r.stdout)
    assert m, r.stdout
    resolve_incident(GMS, m.group(0), note="Notary test cleanup")
