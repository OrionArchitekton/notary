# Adversarial overclaim review: verdict and adjudication record

Date: 2026-07-18. Two independent engines reviewed every judge-facing claim
surface (README, DEVPOST, YouTube metadata, screenshot captions, demo
narration, the CLI card, and the live replay page) against the code, tests,
git history, and the live site.

## Engine 1: internal adversarial fleet

Verdict as delivered: FIX FIRST (1 blocking, 3 warnings, 6 info). All ten
findings were adjudicated genuine and fixed:

- B1 (blocking): "repeats the lie / Amounts are in dollars" was refuted by
  our own captured pre-ledger answer, which hedges instead of repeating the
  claim. All three surfaces reworded to what the capture shows.
- W1: "930 real queries" reworded; the usage evidence is seeded demo data
  recorded in the catalog.
- W2: "stock agent" reworded to "minimal catalog-grounded agent built on the
  stock DataHub MCP read tools".
- W3, I1 through I6: chapter re-time note, verbatim-stdout wording, checked-in
  payload wording, live-test skip disclosure, README status un-stubbed,
  "Every agent" softened, close narration now distinguishes frozen captures
  from the live catalog.

## Engine 2: Codex adversarial review

Verdict as delivered: FIX FIRST (4 blocking, 5 warnings, 1 info). Each
finding was verified against the code before acting; all were adjudicated
genuine:

- C1 (blocking): description reads use GraphQL, not MCP. All surfaces now
  say reads are GraphQL, write-back rides the DataHub MCP Server, incidents
  ride GraphQL, and the next-agent reader uses the stock MCP tools.
- C2 (blocking): "all seven scenarios demonstrated with live tests"
  overclaimed the test topology; README now states 8 live integration tests
  plus deterministic replay and evaluation tests. Spec S2's example column
  was also updated to the demonstrated ISO-4217 control, with the
  provider-blocked ISO-3166 divergence documented (spec-persistence rule:
  the spec is updated in the same change when behavior diverges).
- C3 (blocking, code fix): the USD unit rubric could issue CONTRADICTED or
  CONFIRMED from capped-scan prefix statistics while its rationale asserted
  a universal statement. Fixed test-first: both verdicts now require a
  complete scan (two new regression tests; rubric text updated to disclose
  the bar). The demo warehouse (2000 rows) is unaffected.
- C4 (blocking): the replay page's "one captured run" framing conflated the
  reproducible local evaluation with the separately captured agent answers.
  The disclosure, README, DEVPOST, and YouTube copy now describe the page
  as a frozen, reproducible bundle assembled from the recorded run's
  inputs, with the agent answers prompt-bound to this evaluation's
  evidence (a build-time gate enforces that binding).
- W5 through W9: "every extracted claim" scoping, dossier-per-adjudicated-
  claim wording, hypothetical framing for the 100x line, accurate S5
  mechanics in the README, and the miss-reasons pointer to the README.
- I10: the raw stdout transcript of the recorded demo run is committed at
  demo/captures/run-cli-2026-07-18.txt and the CLI card cites it.

## Post-fix state

- Full suite: 115 passed (113 prior plus 2 new capped-scan regression
  tests), exit code 0, including the live integration tests.
- Replay payload regenerated with the new disclosure; committed assets
  byte-match a fresh capture; the live site serves the updated payload
  (verified logged-out with a title identity check).
- Long-dash scan clean across every authored surface.

## Verdict

SAFE TO SUBMIT, contingent only on the final demo video cut matching the
corrected narration (the render is produced from the same corrected
DEMO_SCRIPT) and the standard freeze checklist.
