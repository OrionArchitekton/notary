# Devpost story update (v4), paste-ready for Dan

Three edits to the LIVE Devpost entry. All are Dan-only (Devpost save is
operator-gated). Apply before the Aug 10, 2:00 PM PDT deadline; never after.

Supersedes DEVPOST-V3-UPDATE.md, which is already applied and live.

## 1. Fix the "Judge it in 60 seconds" CTA (cosmetic, but it is the primary path)

The first CTA currently autolinks the trailing colon into the URL, so the link
text and href both read `https://notary-replay.vercel.app:`. Every browser
normalizes that back to the correct URL (verified: WHATWG URL parser, curl, and
urllib all resolve it to 200), so the path is NOT broken, but it looks sloppy on
the first thing a judge clicks.

FIND this line in the story:

    1. Open https://notary-replay.vercel.app: a hosted replay of the recorded

REPLACE with:

    1. Open [the hosted replay](https://notary-replay.vercel.app/): a real
       captured Notary run, disclosed on-page, with the honest table, the
       flagship cents catch, the before and after catalog descriptions, and the
       next-agent flip.

(The colon now sits OUTSIDE the markdown link, so it cannot be swallowed into
the href. Never leave a bare colon directly after a raw URL, and use no long
dashes: this is public-facing copy.)

## 2. Correct the CI claim (honesty fix)

The current story says public CI "runs the full suite". It does not start a
DataHub quickstart, so the 10 live round-trips SKIP in CI.

FIND:

    Public CI (SHA-pinned actions, locked install) runs the full suite and
    reproduces the eval table on every push; frozen judge artifacts live in
    examples/.

REPLACE with:

    Public CI (SHA-pinned actions, locked install) runs the complete
    deterministic suite and reproduces the evaluation table on every push; the
    10 live DataHub round-trips are validated separately against a local
    quickstart, with a timestamped receipt committed at docs/live-test-receipt.md.
    Frozen judge artifacts live in examples/.

## 3. Add the load-bearing MCP read (new capability, strengthens Use of DataHub)

ADD after the sentence describing the verification loop:

    The verdict gate itself runs through the stock DataHub MCP Server: before a
    reconciliation source may corroborate a contradiction, Notary reads the
    asset's lineage with the stock get_lineage tool and requires the declared
    source to be a real upstream. That MCP receipt is written into the evidence
    dossier, and a failed read, a self-reference, or a possibly-truncated result
    all refuse rather than fall back to another transport. The same agent that
    writes through MCP now reads its gating evidence through MCP.

## 4. Test count

The pasted copy says "N tests, 10 of them live DataHub round-trips", so the
number must come from a run where those 10 ACTUALLY RAN. `pytest -q` SKIPS them
when no DataHub quickstart is listening on localhost:8080, which reports a
smaller passed count and would contradict the sentence it sits in.

At paste time, at the freeze SHA:

1. Confirm the quickstart is up: `curl -s -o /dev/null -w '%{http_code}'
   http://localhost:8080/health` must print `200`. If it does not, either start
   it (`datahub docker quickstart`) or paste the numbers recorded in
   `docs/live-test-receipt.md`, which were captured with it running.
2. Run `pytest -q` and read the summary line.
   - Expected with the quickstart up: `166 passed` and **no** skips.
   - Without it, the live module skips at import (`allow_module_level=True`),
     so the summary reads `156 passed, 1 skipped`: one SKIPPED MODULE, not 10
     skipped tests. Do NOT paste 156, and do not read "1 skipped" as "only one
     test missing"; fix step 1 first.
3. Paste the passed count verbatim. Do not paste a remembered number.

## 5. Form check (the connector cannot read saved answers)

Already verified 2026-07-20 and unchanged since: category "Agents That Do Real
Work", DataHub OSS/Core + MCP Server, OSS contribution #139/#140, artifacts link
pointing at the curated examples/ folder, country, build-window disclosure, and
the Feedback Prize opt-in. Re-confirm only if you touch that step.
