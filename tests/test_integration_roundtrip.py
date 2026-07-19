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
    from notary.catalog import seed_usage_stats

    db = tmp_path_factory.mktemp("wh") / "fiction_retail.duckdb"
    manifest = build_warehouse(db, seed=DEFAULT_SEED)
    ensure_trust_properties(GMS)
    urns = ingest_manifest(db, manifest, GMS)
    # payments is the demo's high-usage asset: the S4 danger qualification
    # rests on real catalog usage evidence, seeded here
    seed_usage_stats(GMS, PAYMENTS_URN, anchor_date="2026-07-18")
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


def test_incident_raise_and_resolve_round_trip(ingested, tmp_path):
    """S4 tracer: a CONTRADICTED unit lie on a HIGH-USAGE asset (usage
    seeded into the live catalog, fetched back as evidence) raises a REAL
    incident via OSS GraphQL, idempotently, and the incident is resolvable
    (the reversibility half)."""
    from notary.incidents import (
        draft_incident,
        fetch_usage,
        raise_incident_idempotent,
        resolve_incident,
    )

    db, _, _ = ingested
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

    # usage evidence comes from the live catalog (seeded by the fixture),
    # verifying the usageStats GraphQL shape against the real quickstart
    usage = fetch_usage(GMS, PAYMENTS_URN)
    assert usage is not None and usage.queries_last_30d >= 30

    draft = draft_incident(
        PAYMENTS_URN, findings, run_date="2026-07-18", usage=usage
    )
    assert draft is not None
    assert str(usage.queries_last_30d) in draft.description

    urn1, created1 = raise_incident_idempotent(GMS, draft)
    assert urn1.startswith("urn:li:incident:")
    # idempotency is eventual (search index refresh); wait until the raised
    # incident is visible to the dedup query, then a re-raise must reuse it
    import time

    from notary.incidents import find_open_notary_incident

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if find_open_notary_incident(GMS, PAYMENTS_URN, draft.title) == urn1:
            break
        time.sleep(1)
    else:
        raise AssertionError("raised incident never became query-visible")
    urn2, created2 = raise_incident_idempotent(GMS, draft)
    assert urn2 == urn1
    assert not created2
    # reversibility: resolve it (also verifies updateIncidentStatus works)
    resolve_incident(GMS, urn1, note="Notary test cleanup")


def test_run_cli_single_asset_end_to_end(ingested, tmp_path):
    """The operator command: ONE invocation reads the LIVE catalog, extracts,
    probes, adjudicates, writes the verdicts back, and raises an incident for
    the contradicted high-usage asset. This is S1-S5 through the real front
    door, in the explicit --demo mode."""
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
            "--db", str(tmp_path / "cli-demo.duckdb"),
            "--fixtures", "tests/fixtures/llm",
            "--asset", PAYMENTS_URN,
            "--demo",
        ],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert r.returncode == 0, r.stderr
    assert "CONTRADICTED" in r.stdout
    m = re.search(r"urn:li:incident:\S+", r.stdout)
    assert m, r.stdout
    resolve_incident(GMS, m.group(0), note="Notary test cleanup")


def test_obsolete_incident_is_resolved_by_a_clean_run(ingested):
    """Cycle-3 finding: after the lie is fixed, a later clean run must
    resolve the incident it made obsolete instead of leaving a stale page.
    close_obsolete_incident finds the ACTIVE Notary incident by title and
    resolves it with a provenance note."""
    import time

    from notary.incidents import (
        UsageEvidence,
        close_obsolete_incident,
        draft_incident,
        find_open_notary_incident,
        raise_incident_idempotent,
    )
    from notary.extract import ReplayLLM

    db, _, _ = ingested
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
    usage = UsageEvidence(940, 14, "test")
    draft = draft_incident(
        PAYMENTS_URN, findings, run_date="2026-07-18", usage=usage
    )
    urn1, _ = raise_incident_idempotent(GMS, draft)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if find_open_notary_incident(GMS, PAYMENTS_URN, draft.title) == urn1:
            break
        time.sleep(1)
    else:
        raise AssertionError("incident never became query-visible")

    resolved = close_obsolete_incident(GMS, PAYMENTS_URN, run_date="2026-07-19")
    assert resolved == urn1
    # second close finds nothing left to resolve
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if find_open_notary_incident(GMS, PAYMENTS_URN, draft.title) is None:
            break
        time.sleep(1)
    assert close_obsolete_incident(GMS, PAYMENTS_URN, run_date="2026-07-19") is None


def test_run_cli_clean_asset_takes_the_no_incident_path(ingested, tmp_path):
    """Covers the CLI's else branch (cycle-3 import regression): an asset
    whose contradictions are not dangerous (fct_refunds: a completeness lie,
    no unit/scale contradiction) completes cleanly, raises nothing, and
    exercises the obsolete-incident lookup."""
    import os
    import subprocess
    import sys

    refunds = (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
        "fiction_retail.fct_refunds,PROD)"
    )
    # idempotency: a prior run corrected the reason description in the
    # editable overlay; reset so replay fixtures match what is read
    _reset_editable_description(refunds, "reason", "Refund reason. Required field.")
    env = {**os.environ, "NOTARY_RUN_DATE": "2026-07-18"}
    r = subprocess.run(
        [
            sys.executable, "-m", "notary.run",
            "--gms", GMS,
            "--db", str(tmp_path / "clean-demo.duckdb"),
            "--fixtures", "tests/fixtures/llm",
            "--asset", refunds,
            "--demo",
        ],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert r.returncode == 0, r.stderr
    assert "incident raised" not in r.stdout


def _reset_table_description(urn: str, text: str) -> None:
    q = json.dumps({
        "query": "mutation($input: DescriptionUpdateInput!) "
                 "{ updateDescription(input: $input) }",
        "variables": {"input": {"description": text, "resourceUrn": urn}},
    }).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=q,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    assert not payload.get("errors"), payload


def test_run_cli_table_level_deprecation_end_to_end(ingested, tmp_path):
    """PR5 cycle-3 regressions (HIGH x2): a table-only-described asset must
    be runnable OUTSIDE --demo (with the reduced-binding caveat), and a
    contradicted TABLE-level claim gets its corrected description written
    back (not just a ledger and dossier)."""
    import os
    import re
    import subprocess
    import sys

    legacy = (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
        "fiction_retail.legacy_orders_v1,PROD)"
    )
    lie = "DEPRECATED 2025: superseded by fct_orders. No longer used."
    _reset_table_description(legacy, lie)

    db, _, _ = ingested  # full seeded warehouse incl. query_log
    env = {**os.environ, "NOTARY_RUN_DATE": "2026-07-18"}
    r = subprocess.run(
        [
            sys.executable, "-m", "notary.run",
            "--gms", GMS,
            "--db", str(db),
            "--fixtures", "tests/fixtures/llm",
            "--asset", legacy,
        ],
        capture_output=True, text=True, timeout=300, env=env,
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert r.returncode == 0, r.stderr
    assert "CONTRADICTED" in r.stdout
    assert "binding evidence" in r.stdout.lower()  # reduced-binding caveat

    # the corrected TABLE description is on the editable surface
    q = json.dumps({
        "query": 'query($urn: String!) { dataset(urn: $urn) { '
                 'editableProperties { description } } }',
        "variables": {"urn": legacy},
    }).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=q,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read())
    desc = payload["data"]["dataset"]["editableProperties"]["description"]
    assert "Notary" in desc
    # restore the planted lie for the next run
    _reset_table_description(legacy, lie)
