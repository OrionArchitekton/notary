# YouTube metadata (demo upload)

## Title (under 100 chars)

Notary: the context lie detector for DataHub (Agent Hackathon demo)

## Description

A column says "transaction amount in USD". The warehouse stores integer
cents. An agent grounded on that catalog can put a revenue calculation 100x
off, with the catalog's authority behind it.

Notary is an AI agent that cross-examines a DataHub catalog's claims
against measured reality: it reads the live descriptions, probes the
warehouse with bounded read-only SQL, adjudicates every extracted claim
with evidence (CONFIRMED, CONTRADICTED, or UNVERIFIABLE, fail-closed), and
writes the verdicts back through the DataHub MCP Server: a trust ledger,
evidence dossiers, a provenance-labeled corrected description, and an
operational incident when the lie is dangerous.

Everything in this video is real: frozen captured outputs, disclosed as
such, plus a live local catalog the recorded run actually wrote to.

Try it:
- Hosted replay (no setup): https://notary-replay.vercel.app
- Repository and full local quickstart: https://github.com/OrionArchitekton/notary

Built for the DataHub Agent Hackathon (Category 1: Agents That Do Real
Work). Apache-2.0.

Chapters (timed to the final 2:51 cut):
0:00 The problem: catalogs lie with authority
0:20 One command: the run that catches the cents lie
0:54 DataHub after the run: provenance-labeled correction and incident
1:29 The next agent inherits the verdict
2:03 The honest scorecard: misses included
2:28 Replay it yourself

## Tags (comma-separated)

datahub, mcp, data catalog, ai agents, metadata, data quality, claude, duckdb, hackathon, model context protocol

## Upload settings

- Not made for kids
- Visibility: PUBLIC
- After upload: verify logged-out via the oembed endpoint (title match), then
  paste the URL into Devpost's Video demo link and DEVPOST.md.
