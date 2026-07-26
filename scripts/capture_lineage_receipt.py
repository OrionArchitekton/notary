#!/usr/bin/env python3
"""Capture the MCP get_lineage receipt the frozen replay replays verbatim.

This performs ONE real read against a live DataHub quickstart through the
stock `mcp-server-datahub` `get_lineage` tool and freezes the resulting
receipt as a fixture. The hosted replay then shows the actual DataHub read
that authorized the flagship contradiction, without the page build needing
a quickstart (which would break byte-identical reproducibility).

Same contract as the captured LLM completions: a REAL call, captured once,
replayed verbatim. Re-run this whenever the receipt shape changes.

Usage:
    python scripts/capture_lineage_receipt.py            # needs a quickstart
    python scripts/capture_lineage_receipt.py --gms http://localhost:8080
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notary.run import lineage_verified_upstream  # noqa: E402

FLAGSHIP_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
    "fiction_retail.fct_payments,PROD)"
)
RECON_REFERENCE = "billing_invoices"
DEFAULT_OUT = "tests/fixtures/lineage/flagship-lineage-receipt.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gms", default="http://localhost:8080")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--asset-urn", default=FLAGSHIP_URN)
    parser.add_argument("--reference-table", default=RECON_REFERENCE)
    args = parser.parse_args(argv)

    ok, detail, receipt = lineage_verified_upstream(
        args.gms, args.asset_urn, args.reference_table
    )
    # Capture only a receipt that AUTHORIZED the read. A refusal receipt is
    # real output but it is not what the replay claims to show, and freezing
    # one would publish a gate that never passed as the gate that did.
    if not ok or not receipt.get("verified"):
        print(
            f"error: the live lineage read did not verify ({detail}); "
            f"refusing to freeze a non-authorizing receipt",
            file=sys.stderr,
        )
        print(json.dumps(receipt, indent=1, sort_keys=True), file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(f"captured: {detail}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
