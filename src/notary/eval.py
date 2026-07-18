"""S7 eval harness: score Notary end-to-end against the manifest ground truth.

Runs every manifest entry through the real pipeline (extraction gates ->
probe -> rubric) and classifies each against its planted-lie ground truth.
Misses and false positives are first-class results: the published table is
the honest record of what v1 catches and what it does not, never a filtered
highlight reel (spec S7).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import duckdb

from notary.adjudicate import adjudicate
from notary.demo.seeder import DEFAULT_SEED, MANIFEST, CatalogEntry, Manifest, build_warehouse
from notary.extract import LLMClient, ReplayLLM, extract_claims
from notary.probe import plan_probe, run_probe
from notary.types import ClaimType, Finding, Verdict

_URN_TEMPLATE = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.{table},PROD)"

# Entry outcomes. A planted lie is either caught (some claim on its field was
# CONTRADICTED) or missed - a wrong CONFIRMED, an UNVERIFIABLE, and a claim
# the extraction gates dropped all count as missed, because in every one of
# those cases the lie survives in the catalog. A truthful control either
# trips a false positive (CONTRADICTED) or stays clean.
CAUGHT = "caught"
MISSED = "missed"
FALSE_POSITIVE = "false_positive"
CLEAN = "clean"


@dataclass(frozen=True)
class EntryResult:
    entry: CatalogEntry
    findings: tuple[Finding, ...]
    outcome: str
    # Extraction failed for this entry (replay fixture missing, provider
    # content-filter 400, malformed completion). Scored fail-closed like any
    # extraction drop (lie -> missed, control -> clean) but disclosed in the
    # published table so it never reads as a verified result.
    extraction_error: str | None = None
    # CONTRADICTED findings whose claim type differs from the entry's planted
    # claim type (fleet-review finding: without this, a future rubric's false
    # positive against a truthful secondary sentence would be laundered into
    # 'caught' in the wrong row). Never counted as a catch; disclosed.
    off_type_contradictions: int = 0


def score_entry(entry: CatalogEntry, findings: tuple[Finding, ...],
                extraction_error: str | None = None) -> EntryResult:
    """Pure scoring rule: a lie is caught ONLY by a CONTRADICTED finding of
    the entry's own claim type; a control trips a false positive on ANY
    CONTRADICTED finding (its whole description is truthful). Off-type
    contradictions on a lie entry are disclosed, never credited."""
    on_type = any(
        f.verdict is Verdict.CONTRADICTED
        and f.claim.claim_type is entry.claim_type
        for f in findings
    )
    off_type = sum(
        1 for f in findings
        if f.verdict is Verdict.CONTRADICTED
        and f.claim.claim_type is not entry.claim_type
    )
    if entry.planted_lie:
        outcome = CAUGHT if on_type else MISSED
    else:
        outcome = FALSE_POSITIVE if (on_type or off_type) else CLEAN
    return EntryResult(
        entry=entry,
        findings=findings,
        outcome=outcome,
        extraction_error=extraction_error,
        off_type_contradictions=off_type,
    )


@dataclass(frozen=True)
class EvalReport:
    entries: tuple[EntryResult, ...]

    def totals(self) -> dict[str, int]:
        t = {"lies": 0, "controls": 0, "caught": 0, "missed": 0,
             "false_positives": 0, "clean": 0}
        for r in self.entries:
            t["lies" if r.entry.planted_lie else "controls"] += 1
            key = {CAUGHT: "caught", MISSED: "missed",
                   FALSE_POSITIVE: "false_positives", CLEAN: "clean"}[r.outcome]
            t[key] += 1
        return t

    def rows(self) -> dict[ClaimType, dict[str, int]]:
        rows = {
            ct: {"lies": 0, "controls": 0, "caught": 0, "missed": 0,
                 "false_positives": 0, "clean": 0}
            for ct in ClaimType
        }
        for r in self.entries:
            row = rows[r.entry.claim_type]
            row["lies" if r.entry.planted_lie else "controls"] += 1
            key = {CAUGHT: "caught", MISSED: "missed",
                   FALSE_POSITIVE: "false_positives", CLEAN: "clean"}[r.outcome]
            row[key] += 1
        return rows

    def to_markdown(self) -> str:
        lines = [
            "| claim type | planted lies | caught | missed | controls | false positives |",
            "|---|---|---|---|---|---|",
        ]
        for ct, row in self.rows().items():
            lines.append(
                f"| {ct.value} | {row['lies']} | {row['caught']} | "
                f"{row['missed']} | {row['controls']} | {row['false_positives']} |"
            )
        t = self.totals()
        lines.append(
            f"| **total** | {t['lies']} | {t['caught']} | {t['missed']} | "
            f"{t['controls']} | {t['false_positives']} |"
        )
        errored = [r for r in self.entries if r.extraction_error]
        if errored:
            names = ", ".join(
                f"{r.entry.table}.{r.entry.column or '(table)'}" for r in errored
            )
            lines.append("")
            lines.append(
                f"{len(errored)} of {len(self.entries)} entries had no "
                f"extraction ({names}); scored fail-closed "
                f"(lie counts as missed, control counts as clean), not verified."
            )
        # controls' off-type contradictions are already visible as false
        # positives in the table; the invisible case is a lie entry's
        off_type = sum(
            r.off_type_contradictions for r in self.entries
            if r.entry.planted_lie
        )
        if off_type:
            lines.append("")
            lines.append(
                f"{off_type} off-type contradiction(s) on planted-lie entries "
                f"(a CONTRADICTED verdict whose claim type differs from the "
                f"planted lie); disclosed here, never counted as caught."
            )
        return "\n".join(lines)


def evaluate(
    manifest: Manifest,
    con: duckdb.DuckDBPyConnection,
    llm: LLMClient,
) -> EvalReport:
    """Run the pipeline over every manifest entry and score it."""
    untyped = [e for e in manifest.claims if e.claim_type is None]
    if untyped:
        names = ", ".join(f"{e.table}.{e.column or '(table)'}" for e in untyped)
        raise ValueError(
            f"manifest entries without a claim_type cannot be scored: {names}"
        )
    results: list[EntryResult] = []
    for entry in manifest.claims:
        urn = _URN_TEMPLATE.format(table=entry.table)
        error: str | None = None
        findings: tuple[Finding, ...] = ()
        try:
            # one extraction call per entry (prompts are per-field, so the
            # prompt keys are identical to a per-table batch) - a single
            # failing prompt never poisons its table-mates
            claims = extract_claims(urn, {entry.column: entry.description}, llm)
        except Exception as e:  # disclosed in the table, never swallowed
            error = f"{type(e).__name__}: {e}"
        else:
            findings = tuple(
                adjudicate(claim, run_probe(plan_probe(claim), con))
                for claim in claims
            )
        results.append(score_entry(entry, findings, extraction_error=error))
    return EvalReport(entries=tuple(results))


def main(argv: list[str] | None = None) -> int:
    """One command -> the verbatim S7 table (spec S7 acceptance).

    Extraction replays captured completions (ReplayLLM), so the run is
    deterministic and needs no network or API key. The warehouse is rebuilt
    from the fixed seed on every run: cold-start reproducible by design."""
    parser = argparse.ArgumentParser(
        prog="python -m notary.eval",
        description="Run Notary over the seeded manifest and print the "
        "caught/missed/false-positive table per claim type.",
    )
    parser.add_argument(
        "--db", default=".notary/fiction_retail.duckdb",
        help="warehouse path (rebuilt deterministically each run)",
    )
    parser.add_argument(
        "--fixtures", default="tests/fixtures/llm",
        help="directory of captured LLM completions to replay",
    )
    args = parser.parse_args(argv)

    # Fleet-review fix (fail-open exit code): a missing or empty fixtures
    # store is a setup error, not an eval result. Without this guard the run
    # prints a degenerate all-miss table and exits 0, which any script
    # consuming the exit code reads as a valid evaluation.
    fixtures = Path(args.fixtures)
    if not fixtures.is_dir() or not any(fixtures.glob("*.json")):
        print(
            f"error: fixtures directory {fixtures} is missing or has no "
            f"captured completions; run from the repo root or pass --fixtures",
            file=sys.stderr,
        )
        return 2

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = evaluate(MANIFEST, con, ReplayLLM(fixtures))
    finally:
        con.close()
    print(report.to_markdown())
    if all(r.extraction_error for r in report.entries):
        print(
            "error: every entry failed extraction; the table above verified "
            "nothing (wrong fixtures store?)",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
