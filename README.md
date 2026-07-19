# Notary: the context lie detector

**Notary is an AI agent that cross-examines your data catalog's claims against
measured reality, and writes the verdicts back into the catalog.**

A column says "transaction amount in USD" but stores integer cents. An agent
grounded on that catalog can quote revenue 100x off, with the catalog's
authority behind it. Catalogs accumulate claims (units, freshness,
completeness, enums, deprecation) and nothing checks whether they are still
true.

Notary reads an asset's live descriptions from [DataHub](https://datahub.com)
(GraphQL), probes the warehouse with deterministic SQL, and adjudicates every
extracted claim: **CONFIRMED**, **CONTRADICTED**, or **UNVERIFIABLE**, with
evidence. Then it writes back what it learned through the DataHub MCP Server,
so the next agent inherits verified context:

- a **trust ledger** on the asset (structured properties: verdict, verified-at,
  evidence link)
- an **evidence dossier** (the probe SQL, the measured value, the claim diff)
- a **corrected description** with labeled provenance
- an **incident** (GraphQL) on assets whose context is dangerously wrong

Built for the [DataHub Agent Hackathon](https://datahub.devpost.com) (Category 1:
Agents That Do Real Work). Apache-2.0.

## Hosted replay (no setup required)

**https://notary-replay.vercel.app** replays the recorded demo run: the
honest evaluation table, the flagship cents-lie catch with its evidence
dossier, the before/after catalog descriptions, and the next-agent answer
flip. The page is a frozen, reproducible bundle assembled from that run's
inputs (the seeded warehouse, the captured Claude extractions, Notary's own
write-back formatters, and the separately captured next-agent answers, which
are prompt-bound to this evaluation's evidence); nothing is generated when
the page loads, and the page says so. Regenerate the data yourself with:

```
python scripts/capture_replay_data.py --out web/replay-data.json
```

## Status

Feature-complete: all seven scenarios of the governing spec
([specs/notary-spec.md](specs/notary-spec.md)) are demonstrated by the test
suite; 8 tests are live DataHub integration round-trips (skipped without a
local quickstart), the rest are deterministic replay and evaluation tests.
Built for the DataHub Agent Hackathon (submission window through 2026-08-10).

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
| freshness | 2 | 2 | 0 | 0 | 0 |
| completeness | 3 | 3 | 0 | 0 | 0 |
| domain_enum | 2 | 2 | 0 | 2 | 0 |
| deprecation_usage | 1 | 1 | 0 | 0 | 0 |
| **total** | 12 | 9 | 3 | 4 | 0 |

1 of 17 entries had no extraction (dim_customers.country_code); scored fail-closed: a lie counts as missed, a control is unscored and excluded from the controls and false-positive columns. Not verified.

This table is published verbatim, misses included. Five deterministic rubrics
run today: null-share (completeness), staleness against an explicit anchor
date (freshness), distinct-set and bounds checks (domain_enum), recent query
activity against the warehouse query log (deprecation), and the USD cents
signature (unit_scale). Together they catch 9 of the 12 planted lies with
zero false positives. The three unit misses are DECLARED, not pending: a
0-to-1 distribution is scale-ambiguous (a stored fraction and legitimate
sub-1-percent values are indistinguishable by distribution alone; our own
review pipeline killed the fraction rubric we first shipped for exactly that
reason), and milliseconds/grams magnitude bands would be domain guesses.
This project does not guess. Each new rubric moves its
row from missed to caught; the table is regenerated, never hand-edited (a
test fails if the README table drifts from the command's output). The one
unextracted entry is a provider-side content-filter block on that exact
capture prompt (deterministic across three attempts); it is scored
fail-closed and disclosed rather than silently dropped: the manifest carries
5 truthful controls, and the table's controls column counts only the 4 that
were actually adjudicated.

## Quick start (full local run)

Prerequisites: Docker (for the DataHub quickstart), Python 3.11+, and
[uv](https://docs.astral.sh/uv/) or pip. No API key is needed: extraction
replays the captured completions by default (pass `--live` with an
`ANTHROPIC_API_KEY` for fresh extraction).

1. Start DataHub OSS locally (GMS answers on http://localhost:8080):

   ```
   pip install acryl-datahub && datahub docker quickstart
   ```

2. Install Notary (from a clone of this repo):

   ```
   uv venv && uv pip install -e '.[dev]'   # or: pip install -e '.[dev]'
   ```

3. Seed the demo: build the lying warehouse and register its catalog in
   DataHub (planted descriptions, trust-ledger structured properties, and
   the usage evidence the incident gate rests on):

   ```
   .venv/bin/python - <<'PY'
   from pathlib import Path
   from notary.catalog import ensure_trust_properties, ingest_manifest, seed_usage_stats
   from notary.demo.seeder import DEFAULT_SEED, build_warehouse

   gms = "http://localhost:8080"
   db = Path(".notary/fiction_retail.duckdb")
   db.parent.mkdir(parents=True, exist_ok=True)
   manifest = build_warehouse(db, seed=DEFAULT_SEED)
   ensure_trust_properties(gms)
   ingest_manifest(db, manifest, gms)
   seed_usage_stats(
       gms,
       "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
       anchor_date="2026-07-18",
   )
   PY
   ```

4. Notarize the flagship asset. This probes the warehouse, adjudicates the
   claims, and writes the trust ledger, evidence dossier, corrected
   description, and (because the asset is high-usage) the incident back to
   DataHub:

   ```
   NOTARY_RUN_DATE=2026-07-18 .venv/bin/python -m notary.run \
     --asset 'urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)' \
     --demo --db .notary/run-wh.duckdb
   ```

   Open the dataset in the DataHub UI (http://localhost:9002) to see what
   it wrote. `--demo` builds its own copy of the seeded warehouse and
   requires an absent `--db` path; `NOTARY_RUN_DATE` is required because
   Notary never reads the wall clock.

5. Watch the next agent flip (the S5 scenario): a minimal catalog-grounded
   agent reads the asset through the stock DataHub MCP tools, then answers
   the same question twice, once from a view with Notary's data withheld
   and once from the full catalog view:

   ```
   .venv/bin/python scripts/s5_next_agent.py
   ```

The evaluation table needs no DataHub at all (see Honest evaluation above),
and `python scripts/capture_replay_data.py --out web/replay-data.json`
regenerates the hosted replay's frozen payload, refusing to publish a
partial or stale run.
