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
    draft_incident,
    fetch_usage,
    raise_incident_idempotent,
)
from notary.probe import plan_probe, run_probe

_DEMO_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail."


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
    args = parser.parse_args(argv)

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
        if not args.asset.startswith(_DEMO_URN_PREFIX):
            print(
                f"error: --demo probes the seeded fiction warehouse; "
                f"writing its verdicts to {args.asset} would be a false "
                f"catalog finding. Only {_DEMO_URN_PREFIX}* urns are "
                f"accepted in demo mode",
                file=sys.stderr,
            )
            return 2
        if db.exists():
            print(
                f"error: --demo builds a fresh seeded warehouse and refuses "
                f"to touch the existing file at {db}; point --db at an "
                f"absent path",
                file=sys.stderr,
            )
            return 2
    elif not db.exists():
        print(
            f"error: warehouse {db} does not exist; the CLI never builds "
            f"one outside --demo mode",
            file=sys.stderr,
        )
        return 2

    llm = AnthropicLLM() if args.live else ReplayLLM(args.fixtures)

    print(f"reading live catalog descriptions for {args.asset}")
    descriptions = read_descriptions(args.gms, args.asset)
    if not descriptions:
        print("no described fields on this asset; nothing to notarize")
        return 0

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
        findings = [
            adjudicate(claim, run_probe(plan_probe(claim, as_of=run_date), con))
            for claim in claims
        ]
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
