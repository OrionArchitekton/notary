# Devpost story update (v3), paste-ready for Dan's double-take

Both external judge reviews scored the live entry ~8.9/10 and 87/100 but flagged
the same gap: the Devpost body still describes the pre-v2 project (119 tests, no
reconciliation, no rollback). The blocks below align it with what is actually
shipped. Paste before the Aug 10, 2:00pm PDT deadline; never after.

## 1. Replace the "honest evaluation" paragraph with:

We publish the scorecard verbatim, misses included: 9 of 12 planted lies caught,
0 false positives across 6 adjudicated truthful controls. Two of those controls
are adversaries we built against our own flagship: a legitimate whole-dollar fee
column that matches the cents signature by distribution (Notary refuses to
contradict it), and a self-referential reconciliation declaration (it earns
nothing). A unit-scale contradiction is never issued from distribution alone: it
requires an operator-declared reconciliation source measuring the claimed scale
on every suspect key over complete scans, and the DataHub catalog itself must
record that source as a lineage upstream of the suspect asset. In the demo, the
canonical billing ledger is the source the buggy payments load derives from, and
that derivation is registered as DataHub lineage the gate verifies.

## 2. Add one sentence to the "what it does" section:

Everything a run writes is reversible with one command: python -m
notary.rollback removes the trust ledger, evidence dossiers, incident, and
provenance-labeled corrections, restoring the original descriptions and never
touching text Notary did not author.

## 3. Add to "what's next" or the repo pointer line:

Frozen judge artifacts live in examples/ (evidence dossier, before/after
descriptions, trust ledger snapshot, captured next-agent answers), and CI runs
the full suite plus the eval-table reproduction on every push (150 tests, 10 of
them live DataHub round-trips).

## 4. Contributions field (only if it still says "open"):

Keep mcp-server-datahub#139 + #140 as-is while #140 is open; if it merges
before the deadline, change to "merged as #140".

## 5. Form check (connector cannot read saved answers)

While editing, confirm the hidden selections still read: category "Agents That
Do Real Work", built with DataHub OSS/Core + MCP Server, and the OSS
contribution + sample-output links present.

Note: the test count above (156 total / 10 live) is the count at PR #11 head (150 passed);
re-check `pytest -q` output at paste time and use the live number.
