# Live DataHub test receipt

Public CI runs the deterministic suite only: it does not start a DataHub
quickstart, so the live integration round-trips SKIP there. They are validated
separately against a local quickstart, and this file records that run.

Regenerate by running `pytest -q` with a DataHub quickstart on
http://localhost:8080, then updating the values below from the run.

| field | value |
|---|---|
| commit under test (clean working tree at run time) | `ca8a23941a3b6ec42a7142d84b15e95c4fd5472f` |
| suite result | **168 passed, exit code 0** in 89.75s |
| live round-trips included | 10 (tests/test_integration_roundtrip.py) |
| run started (UTC) | 2026-07-26T21:09:51Z |
| DataHub GMS image | `acryldata/datahub-gms:v1.5.0.6` |
| GMS container started | 2026-07-26T15:41:59.904428705Z |
| acryl-datahub (SDK) | 1.6.0.15 |
| mcp-server-datahub | 0.6.0 |
| mcp | 1.28.1 |
| command | `pytest -q` |

Without a quickstart the live module skips at import, so the same commit reports
`158 passed, 1 skipped`: one skipped MODULE, not 10 skipped tests. That is the
number public CI prints.

The live set covers: catalog write-back (trust ledger, evidence dossiers,
provenance-labeled description correction), incident raise/resolve lifecycle,
the next-agent inheritance flip through the stock MCP read tools, the
lineage-gated reconciliation read through the stock MCP `get_lineage` tool, and
a full `notary.rollback` round-trip that restores the pre-image and leaves the
demo catalog populated.

A captured dossier from a real run, including the MCP lineage-gate receipt that
authorized the contradiction, is committed at
[examples/evidence-dossier-amount-live-mcp.md](../examples/evidence-dossier-amount-live-mcp.md).
The same receipt is carried on the hosted replay: open the flagship finding's
evidence dossier and read the `lineage_gate` block. It is captured by
`scripts/capture_lineage_receipt.py` against a live quickstart and replayed
verbatim, so the page never issues a DataHub call at build or load time.
