# Live DataHub test receipt

Public CI runs the deterministic suite only: it does not start a DataHub
quickstart, so the live integration round-trips SKIP there. They are validated
separately against a local quickstart, and this file records that run.

Regenerate by running `pytest -q` with a DataHub quickstart on
http://localhost:8080, then updating the values below from the run.

| field | value |
|---|---|
| commit under test | `256c6c63c232d8bb48fcf2870aad8cbb43f2c0e1` |
| suite result | **158 passed, exit code 0** in 103.37s |
| live round-trips included | 10 (tests/test_integration_roundtrip.py) |
| DataHub GMS image | `acryldata/datahub-gms:v1.5.0.6` |
| GMS container started | 2026-07-26T15:41:59.904428705Z |
| acryl-datahub (SDK) | 1.6.0.15 |
| mcp-server-datahub | 0.6.0 |
| command | `pytest -q` |

The live set covers: catalog write-back (trust ledger, evidence dossiers,
provenance-labeled description correction), incident raise/resolve lifecycle,
the next-agent inheritance flip through the stock MCP read tools, the
lineage-gated reconciliation read through the stock MCP `get_lineage` tool, and
a full `notary.rollback` round-trip that restores the pre-image and leaves the
demo catalog populated.

A captured dossier from a real run, including the MCP lineage-gate receipt that
authorized the contradiction, is committed at
[examples/evidence-dossier-amount-live-mcp.md](../examples/evidence-dossier-amount-live-mcp.md).
