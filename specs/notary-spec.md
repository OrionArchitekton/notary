# Notary: the context lie detector

**One line:** Notary is an AI agent that cross-examines a data catalog's existing
claims against measured reality, then writes verdicts and evidence back to the
catalog so every later agent and human inherits verified context instead of
confident fiction.

**Audience + decision improved:** data platform and ML platform teams deciding
"can an agent (or analyst) trust what the catalog says about this asset?"

**Hackathon frame:** DataHub Agent Hackathon, Category 1 (Agents That Do Real
Work). Uses DataHub OSS + the DataHub MCP Server (required-tool criterion) and the
Python SDK. Pure OSS surfaces only; no DataHub Cloud dependency.

## Problem

Catalogs accumulate claims: "amount in USD", "updated daily", "never null",
"deprecated", "owned by payments-team". Nothing adjudicates whether those claims
are still true. Agents grounded on stale or wrong context are confidently wrong,
which is worse than unaided guessing because the error carries the catalog's
authority. The context platform needs a verification layer; Notary is that layer.

## Core loop (domain language)

1. **Read** an asset's claims from the catalog (via MCP): free-text description
   claims, freshness statements, deprecation flags, ownership, schema field
   descriptions.
2. **Extract** discrete, testable claims (LLM, deterministic settings, bounded to
   the five claim types below).
3. **Probe** measured reality for each claim (deterministic SQL profiling against
   the warehouse, catalog query history, schema history).
4. **Adjudicate** each claim: CONFIRMED, CONTRADICTED, or UNVERIFIABLE, with the
   probe evidence attached. Adjudication is rubric-driven and deterministic given
   probe results; the LLM never overrides a probe measurement.
5. **Write back** to the catalog: a trust-ledger entry (structured properties), an
   evidence dossier (document), a proposed corrected description with labeled
   provenance, and an incident when a contradiction is dangerous.

## Claim types (v1 scope, exactly five)

1. **Unit/scale**: "amount in USD" (dollars vs cents), "duration in ms", "%" vs
   fraction.
2. **Freshness**: "updated daily/hourly", "real-time".
3. **Completeness**: "never null", "required", "always populated".
4. **Domain/enum**: "one of {A, B, C}", "ISO-3166 codes", "positive".
5. **Deprecation-vs-usage**: asset flagged deprecated but still actively queried,
   or described "live" while unqueried and stale.

Out of scope for v1: ownership validity, lineage-claim verification, cross-asset
consistency, non-SQL sources.

## Scenarios (tracer-bullet slices, each independently demoable)

### S1. The cents lie (demo centerpiece)
A column described "transaction amount in USD" actually stores integer cents.
Notary extracts the unit claim, profiles the column (magnitude distribution,
integer-ness), returns CONTRADICTED with evidence (measured median vs plausible
USD range), writes the trust-ledger entry and evidence dossier to the catalog, and
proposes a corrected description labeled as Notary-authored with the probe cited.
Acceptance: a catalog reader can see verdict, evidence, and corrected text on the
asset without leaving the catalog UI.

### S2. The honest confirmation
A column with a truthful enum description whose values all match the claimed
set (demonstrated with "ISO-4217 currency code"; the originally chosen
ISO-3166 country-code column's extraction prompt is provider-blocked, so that
entry is scored fail-closed as unscored in the evaluation instead). Verdict
CONFIRMED; ledger entry written; the description is NOT modified.
Acceptance: confirmed assets gain a verified badge and nothing else changes.

### S3. Fail-closed on the unverifiable
A claim Notary cannot probe (e.g. "sourced from the billing system") returns
UNVERIFIABLE with the reason. Notary never guesses, never edits the description,
and the ledger records why. Acceptance: no mutation beyond the ledger entry;
UNVERIFIABLE never counts toward "lies caught" in any surfaced metric.

### S4. The dangerous lie raises an incident
A CONTRADICTED verdict whose blast radius qualifies as dangerous (unit/scale lies
on high-usage assets in v1) raises a catalog incident on the asset naming the
claim, the evidence, and the affected-usage summary. Acceptance: the incident is
visible on the asset in the catalog UI with Notary named as reporter.

### S5. The next agent inherits the verdict
After a Notary run, a second, independent agent session reading the same asset
through the catalog (MCP) can retrieve the trust ledger and evidence dossier and
change its behavior (quote the verified unit, refuse the contradicted one).
Acceptance: demonstrated end-to-end with a stock catalog-grounded agent, no
Notary code in the reader path.

### S6. The judge replays a real run
A hosted replay surface shows a completed Notary run (findings ledger, evidence,
before/after catalog state) from frozen-but-real captured outputs, requiring no
local setup. Acceptance: public URL, loads logged-out, discloses that it replays
a captured run; full local-run instructions live in the repo README.

### S7. Honest evaluation
A seeded demo warehouse carries a fixed set of planted lies across the five claim
types plus truthful controls. The eval harness runs Notary end-to-end and reports
caught/missed/false-positive counts per claim type. Acceptance: the table is
reproducible from one command and is published verbatim (including misses) in the
README and demo.

## Constraints

- **Honesty**: every surfaced number comes from a real run; frozen demo artifacts
  are real captured outputs, disclosed as such. UNVERIFIABLE is a first-class
  verdict, never hidden.
- **Determinism**: probes are pure SQL and adjudication is a pure rubric, so
  the same warehouse state and the same extracted claims yield the same
  verdicts. The LLM boundary is deterministic where stability matters: tests
  and the frozen demo replay captured completions verbatim (current Claude
  models accept no sampling knobs, so live extraction does not promise
  run-to-run identity).
- **Provenance labeling**: every catalog mutation Notary makes is attributable to
  Notary in that surface (ledger property, document authorship, description
  provenance line, incident reporter).
- **Reversibility**: a single command removes all Notary-authored catalog state
  (ledger properties, dossiers, incidents, description edits) for a given run.
- **Safety**: Notary edits only metadata surfaces, never warehouse data. Probes
  are read-only SQL with bounded scan cost.
- **No Cloud dependency**: every surface used exists in DataHub OSS quickstart.

## Test seams (decided at spec time)

1. **Core seam (primary): the claim pipeline as pure functions**: claim
   extraction (metadata in → typed claims out), probe planning (claim → probe
   spec), adjudication (claim + probe result → verdict + evidence). Unit-tested
   against fixtures with no network; LLM boundary mocked by replaying captured
   completions.
2. **Catalog seam (secondary): one integration test path** against a live local
   DataHub quickstart covering read → write-back → read-back round-trip (S1
   shape), marked and skipped when the quickstart is absent.

No other seams. The replay UI renders captured run artifacts and is exercised by
the S6 acceptance check, not unit-tested logic.

## Evaluation criteria mapping (why this scope)

- Use of DataHub: the write-back ledger/dossier/incident loop is the product.
- Technical Execution: deterministic core + eval harness + integration round-trip.
- Originality: verifies existing claims; generation/enrichment tools do not.
- Real-World Usefulness: stale-context damage is a named practitioner pain.
- Submission Quality: S6 replay + S7 table are built for the judge path.
