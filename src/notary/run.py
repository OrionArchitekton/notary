"""The operator command: notarize one asset end to end (S1-S5).

One invocation reads the asset's LIVE catalog descriptions, extracts typed
claims through the gated LLM boundary, probes the warehouse with bounded
read-only SQL, adjudicates with the pure rubrics, writes the verdicts back
to DataHub (trust ledger, evidence dossiers, provenance-labeled corrected
descriptions), and raises one incident when the spec S4 danger
qualification holds (unit/scale contradiction on a high-usage asset).

Safety rails (PR4 review findings):
- All input validation happens BEFORE any network call or filesystem write.
- Without --demo the supplied --db must already exist and is opened
  READ-ONLY; the CLI never builds or replaces an operator's warehouse.
- --demo builds the seeded fiction-retail warehouse only at an ABSENT path
  and refuses non-demo asset urns (fiction verdicts must not reach real
  assets).
- NOTARY_RUN_DATE is required and must parse as an ISO date; freshness
  anchors and run stamps never come from the wall clock.
- A run with ANY failed write-back (ledger, dossier, or description) exits
  nonzero: the catalog still showing a contradicted claim is not success.
- Incidents are idempotent per asset+title: re-running a day's verdicts
  never pages twice.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import duckdb

from notary.adjudicate import adjudicate
from notary.catalog import NOTARY_RUN_DATE_ENV, NotaryWriter, read_descriptions
from notary.demo.seeder import DEFAULT_SEED, build_warehouse
from notary.extract import AnthropicLLM, ReplayLLM, extract_claims
from notary.incidents import (
    close_obsolete_incident,
    draft_incident,
    fetch_usage,
    raise_incident_idempotent,
)
from notary.demo.seeder import MANIFEST
from notary.probe import _urn_table, plan_probe, run_probe

_URN_TEMPLATE = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.{table},PROD)"
# exact manifest-derived allowlist (cycle-2 finding: a name PREFIX would
# authorize fiction verdicts for any unseeded asset in that namespace)
_DEMO_URNS = frozenset(
    _URN_TEMPLATE.format(table=e.table) for e in MANIFEST.claims
)
_DUCKDB_PLATFORM = "(urn:li:dataPlatform:duckdb,"


def schema_matches(catalog_fields: list[str], warehouse_columns: list[str]) -> bool:
    """Binding evidence beyond a table name (cycle-3 finding): every field
    the catalog describes must exist as a column in the warehouse table.
    No described fields = nothing to bind on: fail closed."""
    if not catalog_fields:
        return False
    return set(catalog_fields) <= set(warehouse_columns)


def receipt_ok(receipt: dict) -> bool:
    """Every write-back leg succeeded. A green ledger with a failed
    description correction leaves the lie visible in the catalog UI, so it
    is NOT a successful run."""
    if not receipt.get("ledger"):
        return False
    if any(not d.get("ok") for d in receipt.get("documents", [])):
        return False
    if any(not d.get("ok") for d in receipt.get("descriptions", [])):
        return False
    return True


def lineage_verified_upstream(
    gms_url: str, asset_urn: str, reference_table: str
) -> tuple[bool, str]:
    """A declared reconciliation source earns trust only when the catalog
    itself records it as an UPSTREAM of the suspect asset (judge-slice
    v3): an arbitrary or self-referential table name must not corroborate
    a contradiction. Fail-closed: any query error or an absent edge
    refuses."""
    import json as _json
    import re as _re
    import urllib.request as _request

    m = _re.match(
        r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),([^,]+),([A-Z]+)\)$",
        asset_urn,
    )
    if not m:
        return False, f"refused: cannot parse asset urn {asset_urn!r}"
    platform, name, env = m.groups()
    # A qualified reference name is taken as-is; a bare one inherits the
    # suspect's schema prefix, and a prefix exists only when the suspect
    # name is itself qualified (PR #11 finding: unconditional rsplit
    # mangled unqualified names).
    if "." in reference_table:
        ref_name = reference_table
    elif "." in name:
        ref_name = f"{name.rsplit('.', 1)[0]}.{reference_table}"
    else:
        ref_name = reference_table
    ref_urn = (
        f"urn:li:dataset:(urn:li:dataPlatform:{platform},{ref_name},{env})"
    )
    if ref_urn == asset_urn:
        # A self-edge in the catalog proves nothing about independence
        # (PR #11 finding): the suspect can never corroborate itself.
        return False, "refused: reference resolves to the suspect asset itself"
    query = (
        "query($urn:String!,$start:Int!){ dataset(urn:$urn){ lineage(input:{"
        "direction:UPSTREAM,start:$start,count:100}){ total relationships{"
        " entity{ urn } } } } }"
    )
    start = 0
    for _ in range(20):  # bound: 2000 upstream relationships
        payload = _json.dumps(
            {"query": query, "variables": {"urn": asset_urn, "start": start}}
        ).encode()
        req = _request.Request(
            f"{gms_url}/api/graphql", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _request.urlopen(req, timeout=15) as resp:
                out = _json.loads(resp.read())
            if out.get("errors"):
                return False, (
                    f"refused: lineage query errors {str(out['errors'])[:120]}"
                )
            lineage = (((out.get("data") or {}).get("dataset") or {})
                       .get("lineage") or {})
            rels = lineage.get("relationships") or []
            upstreams = {((r or {}).get("entity") or {}).get("urn")
                         for r in rels}
        except Exception as e:
            return False, f"refused: lineage query failed ({str(e)[:120]})"
        if ref_urn in upstreams:
            return True, f"lineage-verified upstream: {ref_urn}"
        start += len(rels)
        if not rels or start >= int(lineage.get("total") or 0):
            break
    return False, (
        f"refused: {reference_table} is not a catalog-verified upstream "
        f"of the asset"
    )


def unresolved_unit_suspicion(findings) -> bool:
    """A cents-signature unit claim this run could NOT adjudicate (nothing
    declared, or declared and uncorroborated) is OUTSTANDING suspicion: a
    run carrying one must never clear a standing incident (PR #11
    finding: a configuration omission is not fresh evidence). The
    signature is read from the finding's own evidence, top-level only
    (no-rubric findings nest measurements and never match)."""
    from notary.types import ClaimType, Verdict

    for f in findings:
        if (
            f.verdict is Verdict.UNVERIFIABLE
            and f.claim.claim_type is ClaimType.UNIT_SCALE
        ):
            e = f.evidence or {}
            if (
                e.get("integer_share") == 1.0
                and float(e.get("median") or 0) > 1000
            ):
                return True
    return False


def parse_reconcile_args(items: list[str]) -> dict:
    """Parse repeatable --reconcile declarations
    (FIELD=TABLE:SUSPECT_KEY:REFERENCE_KEY:REFERENCE_COLUMN) into
    {field_path: Reconciliation}. Pure; raises ValueError on any
    malformed item. Every part must be a bare SQL identifier: the probe
    layer addresses a single warehouse schema, and a qualified name
    accepted here would crash probe planning after passing the lineage
    gate (PR #11 finding)."""
    from notary.probe import _IDENT
    from notary.types import Reconciliation

    out: dict = {}
    for item in items or []:
        field, sep, rest = item.partition("=")
        parts = rest.split(":")
        if not sep or not field or len(parts) != 4 or not all(parts):
            raise ValueError(
                "--reconcile expects FIELD=TABLE:SUSPECT_KEY:"
                f"REFERENCE_KEY:REFERENCE_COLUMN, got {item!r}"
            )
        if not all(_IDENT.match(x) for x in (field, *parts)):
            raise ValueError(
                "--reconcile parts must be bare SQL identifiers "
                f"(single warehouse schema today), got {item!r}"
            )
        out[field] = Reconciliation(*parts)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m notary.run",
        description="Notarize one catalog asset against measured reality.",
    )
    parser.add_argument("--gms", default="http://localhost:8080")
    parser.add_argument("--asset", required=True, help="dataset urn to notarize")
    parser.add_argument(
        "--db", default=".notary/fiction_retail.duckdb",
        help="warehouse path (read-only; --demo may build it when absent)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="demo mode: build the seeded fiction warehouse at an absent "
        "--db path; only fiction_retail urns are accepted",
    )
    parser.add_argument(
        "--fixtures", default="tests/fixtures/llm",
        help="captured completions to replay (ignored with --live)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="extract with the live Anthropic API instead of replay",
    )
    parser.add_argument(
        "--reconcile", action="append", default=[],
        metavar="FIELD=TABLE:SUSPECT_KEY:REFERENCE_KEY:REFERENCE_COLUMN",
        help="operator-declared reconciliation source for a money column "
        "(repeatable). Required for a unit-scale CONTRADICTED verdict on "
        "real runs: without one the cents signature is suspicion only and "
        "stays UNVERIFIABLE. Demo mode carries the manifest's declarations "
        "instead.",
    )
    args = parser.parse_args(argv)

    if args.demo and args.reconcile:
        parser.error(
            "--reconcile applies to real runs; --demo carries the "
            "manifest's declared reconciliations"
        )
    try:
        reconcile_map = parse_reconcile_args(args.reconcile)
    except ValueError as e:
        parser.error(str(e))

    # --- validation, before any network or filesystem effect ---
    run_date = os.environ.get(NOTARY_RUN_DATE_ENV)
    if not run_date:
        print(
            f"error: {NOTARY_RUN_DATE_ENV} must be set (ISO date); Notary "
            f"never reads the wall clock",
            file=sys.stderr,
        )
        return 2
    try:
        date.fromisoformat(run_date)
    except ValueError:
        print(
            f"error: {NOTARY_RUN_DATE_ENV}={run_date!r} is not an ISO date",
            file=sys.stderr,
        )
        return 2

    db = Path(args.db)
    if args.demo:
        if args.asset not in _DEMO_URNS:
            print(
                f"error: --demo probes the seeded fiction warehouse; "
                f"writing its verdicts to {args.asset} would be a false "
                f"catalog finding. Demo mode accepts exactly the "
                f"manifest-seeded urns",
                file=sys.stderr,
            )
            return 2
        # atomic path reservation (cycle-3 TOCTOU finding: an exists() check
        # alone leaves a window in which another process's file at --db
        # would later be replaced by the build)
        db.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            print(
                f"error: --demo builds a fresh seeded warehouse and refuses "
                f"to touch the existing file at {db}; point --db at an "
                f"absent path",
                file=sys.stderr,
            )
            return 2
    else:
        if _DUCKDB_PLATFORM not in args.asset:
            print(
                f"error: this CLI probes duckdb warehouses only; verdicts "
                f"for {args.asset} cannot be derived from a local duckdb "
                f"file (asset-warehouse binding)",
                file=sys.stderr,
            )
            return 2
        if not db.exists():
            print(
                f"error: warehouse {db} does not exist; the CLI never "
                f"builds one outside --demo mode",
                file=sys.stderr,
            )
            return 2
        # bind the asset to the warehouse: its table must exist there
        # (probing an unrelated database yields false findings)
        table = _urn_table(args.asset)
        con = duckdb.connect(str(db), read_only=True)
        try:
            hit = con.execute(
                "select count(*) from information_schema.tables "
                "where table_name = ?",
                [table],
            ).fetchone()[0]
        finally:
            con.close()
        if not hit:
            print(
                f"error: table {table} does not exist in warehouse {db}; "
                f"refusing to score {args.asset} against an unrelated "
                f"database",
                file=sys.stderr,
            )
            return 2

    llm = AnthropicLLM() if args.live else ReplayLLM(args.fixtures)

    print(f"reading live catalog descriptions for {args.asset}")
    descriptions = read_descriptions(args.gms, args.asset)
    if not descriptions:
        print("no described fields on this asset; nothing to notarize")
        return 0

    if not args.demo:
        # schema fingerprint (cycle-3 finding): the described fields must
        # exist as columns in the warehouse table before its measurements
        # may speak for this asset; a same-named unrelated table fails here
        described_fields = [k for k in descriptions if k is not None]
        table = _urn_table(args.asset)
        if not described_fields and None in descriptions:
            # table-only-described asset (PR5 finding: the only scope the
            # deprecation probe supports must be reachable live): proceed
            # on table existence alone, with the reduced evidence stated
            print(
                f"binding evidence: table name and existence only "
                f"({table} has no field descriptions to fingerprint)"
            )
        else:
            con = duckdb.connect(str(db), read_only=True)
            try:
                cols = [
                    r[0] for r in con.execute(
                        "select column_name from information_schema.columns "
                        "where table_name = ?",
                        [table],
                    ).fetchall()
                ]
            finally:
                con.close()
            if not schema_matches(described_fields, cols):
                print(
                    f"error: the catalog's described fields "
                    f"{sorted(described_fields)} are not all present as "
                    f"columns of {table} in {db}; refusing to score this "
                    f"asset against a warehouse that does not match its "
                    f"schema",
                    file=sys.stderr,
                )
                return 2

    try:
        claims = extract_claims(args.asset, descriptions, llm)
    except Exception as e:
        print(f"error: extraction failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 3
    print(f"{len(descriptions)} described field(s), "
          f"{len(claims)} claim(s) survived the extraction gates")

    if args.demo:
        db.parent.mkdir(parents=True, exist_ok=True)
        build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        # Reconciliation sources are operator-declared; demo mode carries
        # the manifest's declarations, real runs the --reconcile flags.
        # A declaration only counts once the catalog verifies the source
        # is a lineage upstream of the suspect (judge-slice v3); without
        # a verified one, a unit distribution can only ever reach
        # UNVERIFIABLE (rubric v2, judge-review P0).
        table = _urn_table(args.asset)
        findings = []
        recon_refusals = 0
        for claim in claims:
            recon = (
                MANIFEST.reconciliations.get((table, claim.field_path))
                if args.demo else reconcile_map.get(claim.field_path)
            )
            if recon is not None:
                ok, detail = lineage_verified_upstream(
                    args.gms, args.asset, recon.table
                )
                print(
                    f"[reconciliation] {claim.field_path or '(table)'}: "
                    f"{detail}"
                )
                if not ok:
                    recon = None
                    recon_refusals += 1
            findings.append(adjudicate(claim, run_probe(plan_probe(
                claim, as_of=run_date, reconciliation=recon,
            ), con)))
    finally:
        con.close()

    for f in findings:
        field = f.claim.field_path or "(table)"
        print(f"  {f.verdict.value:13} {field}: {f.rationale}")

    if not findings:
        print("no adjudicable claims; nothing written back")
        return 0

    writer = NotaryWriter(args.gms)
    receipt = asyncio.run(writer.write_findings(args.asset, findings))
    print(
        f"write-back: ledger={receipt.get('ledger')} "
        f"dossiers={sum(1 for d in receipt.get('documents', []) if d.get('ok'))}"
        f"/{len(receipt.get('documents', []))} "
        f"descriptions={sum(1 for d in receipt.get('descriptions', []) if d.get('ok'))}"
        f"/{len(receipt.get('descriptions', []))}"
    )
    if not receipt_ok(receipt):
        print(
            "error: one or more write-backs failed; the catalog may still "
            "show the contradicted claim. Re-run after fixing connectivity; "
            "the receipt above names the failed legs",
            file=sys.stderr,
        )
        return 4

    usage = fetch_usage(args.gms, args.asset)
    if usage is None:
        print("usage evidence: none; incident qualification not established "
              "(fail-closed, no incident)")
    draft = draft_incident(args.asset, findings, run_date=run_date, usage=usage)
    if draft is not None:
        incident_urn, created = raise_incident_idempotent(args.gms, draft)
        verb = "raised" if created else "already open"
        print(f"incident {verb}: {incident_urn}")
    elif recon_refusals or unresolved_unit_suspicion(findings):
        # A refused reconciliation or an outstanding cents-signature
        # suspicion downgraded this run (PR #11 findings): the quiet
        # verdict may reflect a lineage failure or a missing declaration,
        # not the warehouse, so a standing incident must NOT be cleared.
        print(
            "incident left untouched: this run carries "
            f"{recon_refusals} reconciliation refusal(s) and/or an "
            "unadjudicated cents-signature suspicion; a downgraded "
            "verdict never clears an alert"
        )
    else:
        # lifecycle (cycle-3 finding): a clean run resolves the incident it
        # made obsolete instead of leaving a stale page
        closed = close_obsolete_incident(args.gms, args.asset, run_date)
        if closed:
            print(f"obsolete incident resolved: {closed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
