# Live DataHub test receipt

Public CI runs the deterministic suite only: it does not start a DataHub
quickstart, so the live integration round-trips SKIP there. They are validated
separately against a local quickstart, and this file records that run.

Regenerate by running `pytest -q` with a DataHub quickstart on
http://localhost:8080, then updating the values below from the run.

| field | value |
|---|---|
| commit under test (clean working tree at run time) | `ed4cc3248c2fc3efea5e9228ca66dfc8fa42eef1` |
| suite result | **169 passed, exit code 0** in 115.30s |
| live round-trips included | 10 (tests/test_integration_roundtrip.py) |
| run started (UTC) | 2026-07-26T22:11:17Z |
| DataHub GMS image | `acryldata/datahub-gms:v1.5.0.6` |
| GMS container started | 2026-07-26T15:41:59.904428705Z |
| acryl-datahub (SDK) | 1.6.0.15 |
| mcp-server-datahub | 0.6.0 |
| mcp | 1.28.1 |
| command | `pytest -q` |

Without a quickstart the live module skips at import, so the same commit reports
`159 passed, 1 skipped`: one skipped MODULE, not 10 skipped tests. That is the
number public CI prints.

Known limitation, disclosed rather than hidden: these live tests share one
DataHub quickstart and one demo asset, and `test_rollback_removes_all_notary_state`
asserts that NO Notary incident is left open on that asset. Repeated back to back
local runs leave incidents open faster than the incident search index settles, so
that test can fail on a heavily reused quickstart even though rollback itself is
correct (it passes in isolation from a clean quickstart, repeatedly). The run
recorded above is a clean full-suite pass; if you hit that failure locally, it is
this, not a rollback defect.

The live set covers: catalog write-back (trust ledger, evidence dossiers,
provenance-labeled description correction), incident raise/resolve lifecycle,
the next-agent inheritance flip through the stock MCP read tools, the
lineage-gated reconciliation read through the stock MCP `get_lineage` tool, and
a full `notary.rollback` round-trip that restores the pre-image and leaves the
demo catalog populated.

A captured dossier from a real run, including the MCP lineage-gate receipt that
authorized that contradiction, is committed at
[examples/evidence-dossier-amount-live-mcp.md](../examples/evidence-dossier-amount-live-mcp.md).
Its `lineage_gate` block is from a run predating the receipt hardening, so it
records the asset, the reference and the matched upstream but not the later
`verified`, `matched_via`, `max_hops` and `max_results` fields. It is kept as
what that run actually produced rather than back-edited to look current.

The hosted replay carries a SEPARATE receipt in the current shape, including
`verified: true`. It is captured by `scripts/capture_lineage_receipt.py`
against a live quickstart and replayed verbatim, so the page never issues a
DataHub call at build or load time. Open the flagship finding's evidence
dossier and read the `lineage_gate` block.
