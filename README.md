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

## Planned quick start

Instructions land here with the first working slice: local DataHub quickstart,
seeded demo warehouse with planted lies, one command to run Notary, one command
to reproduce the evaluation table.
