# Devpost submission copy (paste-ready)

## Project name

Notary

## Tagline (under 60 chars)

The context lie detector for your DataHub catalog

## Elevator pitch (short)

Notary cross-examines a data catalog's claims against measured reality:
it reads an asset's live descriptions from DataHub, probes the warehouse
with deterministic read-only SQL, adjudicates every extracted claim with
evidence, and writes the verdicts back through the DataHub MCP Server,
where the next agent will look.

## Project story (main description)

### The problem

A column says "transaction amount in USD" but stores integer cents. An
agent grounded on that catalog can put a revenue calculation 100x off, with
the catalog's own authority behind it. Catalogs accumulate claims (units,
freshness, completeness, enums, deprecation) and nothing checks whether they
are still true. As agents inherit catalogs as ground truth, stale context
stops being an annoyance and becomes an amplifier: downstream answers carry
the lie with the catalog's confidence.

### What Notary does

Point Notary at a DataHub asset and it runs a verification loop:

1. Reads the asset's live descriptions from DataHub (GraphQL).
2. Extracts typed, checkable claims with Claude behind strict entailment
   gates (a claim the description does not actually state never reaches a
   probe); an entry whose extraction fails is disclosed and left
   unverified, never guessed at.
3. Probes the warehouse with bounded, read-only SQL. Scans are capped;
   universal claims get a verdict only from complete scans.
4. Adjudicates each extracted claim with a pure three-way rubric:
   CONFIRMED, CONTRADICTED, or UNVERIFIABLE, fail-closed. Notary does not
   guess.
5. Writes what it learned back into DataHub through the MCP Server, so the
   next reader inherits verified context instead of the lie:
   - a trust ledger on the asset (structured properties: verdict,
     verified-at, evidence pointers)
   - an evidence dossier per confirmed or contradicted claim (the probe
     SQL, the measured value, the claim diff), stored as DataHub
     documents; unverifiable claims are summarized in the ledger
   - a provenance-labeled corrected description that preserves the original
     claim next to the measurement
   - an operational incident (GraphQL), raised only when the lie is
     dangerous: a contradicted unit or scale claim on an asset whose
     catalog usage evidence shows real query traffic (the gate fails
     closed without it)

The write-back is the product: a verdict that lives in the catalog changes
what the next agent does. We demonstrate exactly that with a minimal
catalog-grounded agent built on the stock DataHub MCP read tools (no Notary
code in the catalog-read path). Without the trust ledger, the best it can do
is pass the unverified USD claim along with a shrug. With the ledger
present, it refuses the contradicted claim and quotes the measured cents
evidence instead. Both answers are captured and shown side by side.

### The honest evaluation

The demo warehouse plants 12 catalog lies across 5 claim types, plus
truthful controls. One command rebuilds the warehouse from a fixed seed,
replays the captured Claude extractions verbatim (no network, no key),
probes, adjudicates, and scores every entry. The result table is published
verbatim in the README, misses included, and a test fails if the README
table drifts from the command's output:

12 planted lies: 9 caught, 3 missed, 0 false positives.

The three misses are declared, not pending: a 0-to-1 distribution is
scale-ambiguous by design (a stored fraction and legitimate sub-1-percent
values are indistinguishable by distribution alone; our own adversarial
review pipeline killed the fraction rubric we first shipped for exactly that
reason), and millisecond or gram magnitude bands would be domain guesses.
This project does not guess, and it does not hide its scorecard.

### Judge it in 60 seconds

1. Open https://notary-replay.vercel.app: a hosted replay of the recorded
   demo run, assembled from its frozen inputs and disclosed as such
   on-page, with the honest table, the flagship cents catch, the before and
   after catalog descriptions, and the next-agent flip.
2. Expand the evidence dossier on the flagship finding: the probe SQL and
   measurements, exactly as Notary writes them to DataHub.
3. Read the two captured agent answers side by side: the same catalog, with
   and without Notary's trust ledger.

Full local run (DataHub quickstart, seeded lying warehouse, one command per
step, no API key needed): the Quick start section of the README.

### How we built it

- Python, DuckDB for the demo warehouse, DataHub OSS quickstart as the
  catalog; mcp-server-datahub carries the write-back (ledger, dossiers,
  corrected descriptions) and the next-agent read path, with GraphQL for
  description reads and incidents.
- Claude (claude-opus-4-8) for claim extraction, behind token-aware,
  negation-aware entailment gates; captured completions replay verbatim in
  tests and the demo, so the pipeline is deterministic where stability
  matters.
- Deterministic probes and pure rubrics; every verdict carries its evidence.
- 119 tests (8 are live integration round-trips against the quickstart,
  skipped when no quickstart is running): write-back, incident lifecycle,
  and the next-agent flip, plus a test that pins the README table and one
  that byte-compares the checked-in replay payload the host serves to a
  fresh capture.

### Challenges and what we learned

- Fail-closed is a discipline, not a feature: every gate in the pipeline
  (fixture completeness, extraction failures, bounded scans, incident
  usage evidence) defaults to refusing rather than guessing, and our review
  pipeline repeatedly found the spots where we had not yet earned that
  sentence.
- Honest evaluation is a differentiator: publishing the misses, and the
  reasoning for why they stay missed, made the system more credible than a
  padded scorecard would have.
- The catalog is the right place for verdicts: structured properties,
  documents, and incidents mean the correction reaches humans in the UI and
  agents through MCP with zero custom infrastructure.

## Built with

python, duckdb, datahub, mcp, anthropic-claude, playwright, vercel

## Try it out links

- Hosted replay: https://notary-replay.vercel.app
- Repository: https://github.com/OrionArchitekton/notary

## Video demo link

VIDEO_URL_PLACEHOLDER (YouTube; filled at submission time)
