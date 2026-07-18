"""The operator command: notarize one asset end to end (S1-S5).

One invocation reads the asset's LIVE catalog descriptions, extracts typed
claims through the gated LLM boundary, probes the warehouse with bounded
read-only SQL, adjudicates with the pure rubrics, writes the verdicts back
to DataHub (trust ledger, evidence dossiers, provenance-labeled corrected
descriptions), and raises one incident if any claim was contradicted.

Determinism and honesty rails: NOTARY_RUN_DATE is required (freshness
anchors and run stamps never come from the wall clock); extraction replays
captured completions by default (--live opts into the real API); an
extraction failure is reported and exits nonzero, never scored as a clean
run.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import duckdb

from notary.adjudicate import adjudicate
from notary.catalog import NOTARY_RUN_DATE_ENV, NotaryWriter, read_descriptions
from notary.demo.seeder import DEFAULT_SEED, build_warehouse
from notary.extract import AnthropicLLM, ReplayLLM, extract_claims
from notary.incidents import draft_incident, raise_incident
from notary.probe import plan_probe, run_probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m notary.run",
        description="Notarize one catalog asset against measured reality.",
    )
    parser.add_argument("--gms", default="http://localhost:8080")
    parser.add_argument("--asset", required=True, help="dataset urn to notarize")
    parser.add_argument(
        "--db", default=".notary/fiction_retail.duckdb",
        help="warehouse path (rebuilt deterministically each run)",
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

    run_date = os.environ.get(NOTARY_RUN_DATE_ENV)
    if not run_date:
        print(
            f"error: {NOTARY_RUN_DATE_ENV} must be set (ISO date); Notary "
            f"never reads the wall clock",
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

    db = Path(args.db)
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
        f"dossiers={sum(1 for d in receipt.get('documents', []) if d.get('ok'))} "
        f"descriptions={sum(1 for d in receipt.get('descriptions', []) if d.get('ok'))}"
    )
    if not receipt.get("ledger"):
        print("error: trust ledger write failed; run is incomplete",
              file=sys.stderr)
        return 4

    draft = draft_incident(args.asset, findings, run_date=run_date)
    if draft is not None:
        incident_urn = raise_incident(args.gms, draft)
        print(f"incident raised: {incident_urn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
