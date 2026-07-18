# Pick memo, why Notary (2026-07-18)

Recorded at pick time so later sessions do not re-derive or churn the choice.

## The pick

**Notary: the context lie detector.** Category 1 (Agents That Do Real Work),
DataHub Agent Hackathon (deadline 2026-08-10 5pm ET). Chosen from 9 recon-generated
candidates; 3 were killed by an adversarial self-clone/feasibility pass, and Notary
ranked first (43/50) across the five equally weighted judging criteria with no axis
below 8. Operator approved 2026-07-18.

## Why it won

- **Use of DataHub (tie-breaker criterion):** the write-back loop IS the product:
  trust-ledger structured properties, evidence dossiers, corrected descriptions,
  incidents. Reads AND contributes back to the graph, on pure-OSS surfaces.
- **Originality:** DataHub's shipped tools generate missing docs or check metadata
  presence; nothing shipped adjudicates existing claims against measured data.
  Verification is whitespace in both the product and the predicted field.
- **Narrative:** the sponsor's own framing ("agents fail without context",
  "confidently wrong") asks the next question, who verifies the context?
- **Feasibility:** solo-buildable in the window; probes are SQL profiling + MCP
  reads; write-back uses stock mutation tools; no Cloud-only dependency.
- **Paired OSS contribution:** mcp-server-datahub issue #41 (structured properties
  missing from get_entity responses) is a surface Notary itself needs, the
  contribution is organic, small, and has merged precedent.

## Alternates (not chosen)

1. **Sunset Shepherd** (39/50), safe dataset-retirement agent; killed on
   skim-time similarity to the modal migration-codegen archetype (Originality 6).
2. **Predicate** (39/50), usage-grounded dbt test/contract generator; strongest
   execution/packaging axes but read-heavy (Use of DataHub 7, Originality 6).

## Weakest judged axis (gets the single Phase 6.5 late slice)

**Technical Execution.** The risk register: claim extraction from free-text is the
one non-deterministic component (bounded to 5 claim types); the MCP server and
agent-context kit are young and version-pinned; the S1 round-trip integration test
and the S7 eval table are the proof surface. The one permitted post-freeze
improvement slice goes here, nothing else.

## Standing constraints from recon + day-0 spike (do not violate)

- Change Proposals review UI is Cloud-only: use direct-but-labeled writes, never
  the proposal tools.
- Pin DataHub CLI/quickstart to v1.6.0 and mcp-server-datahub to 0.6.0 (both
  verified green 2026-07-18).
- **Spike finding:** the OSS-mode MCP server registers mutation tools ONLY with
  `TOOLS_IS_MUTATION_ENABLED=true` (default off: 6 read tools; with flag: 18
  tools incl. update_description, add_structured_properties, save_document,
  add_tags). The flag must be set in every run config and documented in setup.
- **Spike finding:** quickstart runs `METADATA_SERVICE_AUTH_ENABLED=false`, so
  local runs are tokenless (no PAT needed or creatable); incident raise/resolve
  verified via OSS GraphQL (`raiseIncident`/`updateIncidentStatus`).
- A text-to-SQL / chat-with-metadata framing collides with the shipped Analytics
  Agent, never describe Notary in those terms.
- Recon corpus (private, not in this repo): ~/.orion/notes/datahub-hackathon/.
