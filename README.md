# Notary: the context lie detector

**Notary is an AI agent that cross-examines your data catalog's claims against
measured reality, and writes the verdicts back into the catalog.**

A column says "transaction amount in USD" but stores integer cents. An agent
grounded on that catalog quotes revenue 100x off, with the catalog's authority
behind it. Catalogs accumulate claims (units, freshness, completeness, enums,
deprecation) and nothing checks whether they are still true.

Notary reads an asset's claims from [DataHub](https://datahub.com) through the
MCP Server, probes the warehouse with deterministic SQL, and adjudicates each
claim: **CONFIRMED**, **CONTRADICTED**, or **UNVERIFIABLE**, with evidence. Then
it writes back what it learned, so the next agent inherits verified context:

- a **trust ledger** on the asset (structured properties: verdict, verified-at,
  evidence link)
- an **evidence dossier** (the probe SQL, the measured value, the claim diff)
- a **corrected description** with labeled provenance
- an **incident** on assets whose context is dangerously wrong

Built for the [DataHub Agent Hackathon](https://datahub.devpost.com) (Category 1:
Agents That Do Real Work). Apache-2.0.

## Status

Under construction (submission window: through 2026-08-10). The spec that governs
this build: [specs/notary-spec.md](specs/notary-spec.md).

## Honest evaluation

The demo warehouse plants 12 catalog lies across 5 claim types, plus 5 truthful
controls ([the manifest](src/notary/demo/seeder.py) is the ground truth). One
command reproduces this table: it rebuilds the warehouse from a fixed seed,
replays the captured Claude extractions verbatim (no network, no key), probes,
adjudicates, and scores every entry. From a clean checkout:

```
uv venv && uv pip install -e '.[dev]'   # or: pip install -e '.[dev]'
.venv/bin/python -m notary.eval
```

| claim type | planted lies | caught | missed | controls | false positives |
|---|---|---|---|---|---|
| unit_scale | 4 | 1 | 3 | 2 | 0 |
| freshness | 2 | 0 | 2 | 0 | 0 |
| completeness | 3 | 0 | 3 | 0 | 0 |
| domain_enum | 2 | 0 | 2 | 2 | 0 |
| deprecation_usage | 1 | 0 | 1 | 0 | 0 |
| **total** | 12 | 1 | 11 | 4 | 0 |

1 of 17 entries had no extraction (dim_customers.country_code); scored fail-closed: a lie counts as missed, a control is unscored and excluded from the controls and false-positive columns. Not verified.

This table is published verbatim, misses included. The current rubric covers
USD unit-scale claims only: it catches the cents lie with zero false positives
and honestly reports every claim type it cannot yet verify as missed. Each new
rubric moves a row from missed to caught; the table is regenerated, never
hand-edited (a test fails if the README table drifts from the command's
output). The one unextracted entry is a provider-side content-filter block on
that exact capture prompt (deterministic across three attempts); it is scored
fail-closed and disclosed rather than silently dropped: the manifest carries
5 truthful controls, and the table's controls column counts only the 4 that
were actually adjudicated.

## Planned quick start

Full instructions land with the write-back demo: local DataHub quickstart,
seeded demo warehouse with planted lies, one command to run Notary end to end.
