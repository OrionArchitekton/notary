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
from notary.demo.seeder import ANCHOR_DATE, DEFAULT_SEED, MANIFEST, CatalogEntry, Manifest, build_warehouse
from notary.extract import (
    KNOWN_UNCAPTURABLE,
    SYSTEM_PROMPT,
    LLMClient,
    ReplayLLM,
    _prompt_key,
    _user_prompt,
    extract_claims,
)
from notary.probe import plan_probe, run_probe
from notary.types import ClaimType, Finding, Verdict

_URN_TEMPLATE = "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.{table},PROD)"

# Entry outcomes. A planted lie is either caught (a CONTRADICTED finding
# matching the planted claim) or missed - a wrong CONFIRMED, an UNVERIFIABLE,
# a gate-dropped claim, and an extraction failure all count as missed,
# because in every one of those cases the lie survives in the catalog. A
# truthful control either trips a false positive (CONTRADICTED) or stays
# clean; a control whose extraction FAILED is UNSCORED (cycle-3 adversarial
# finding: counting it as clean would advertise a false-positive rate over
# controls that were never adjudicated).
CAUGHT = "caught"
MISSED = "missed"
FALSE_POSITIVE = "false_positive"
CLEAN = "clean"
UNSCORED = "unscored"


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
    # CONTRADICTED findings that do not match the planted claim, by type or
    # by sentence (fleet + pipeline findings: without this, a future rubric's
    # false positive against a truthful sentence, of a DIFFERENT type or the
    # SAME type, would be laundered into 'caught'). Never credited; disclosed.
    off_target_contradictions: int = 0


def score_entry(entry: CatalogEntry, findings: tuple[Finding, ...],
                extraction_error: str | None = None) -> EntryResult:
    """Pure scoring rule: a lie is caught ONLY by a CONTRADICTED finding that
    matches the planted claim: same claim type AND the finding's claimed
    sentence CONTAINS the full planted sentence (entry.planted_text,
    defaulting to the whole description). Containment is one-directional
    (cycle-2 adversarial finding: mutual substring let a fragment like
    "null." be credited against "Never null."). A control trips a false
    positive on ANY CONTRADICTED finding (its whole description is
    truthful). Off-target contradictions on a lie entry are disclosed,
    never credited."""
    target = entry.planted_text or entry.description

    def _matches_planted(f: Finding) -> bool:
        return (
            f.verdict is Verdict.CONTRADICTED
            and f.claim.claim_type is entry.claim_type
            and target in f.claim.text
        )

    on_target = any(_matches_planted(f) for f in findings)
    off_target = sum(
        1 for f in findings
        if f.verdict is Verdict.CONTRADICTED and not _matches_planted(f)
    )
    if entry.planted_lie:
        # fail-closed on the catch rate: an extraction failure means the lie
        # survived, so it counts as missed
        outcome = CAUGHT if on_target else MISSED
    elif extraction_error is not None:
        # a control that was never extracted proves nothing about the false
        # positive rate; excluded from the controls denominator
        outcome = UNSCORED
    else:
        outcome = FALSE_POSITIVE if (on_target or off_target) else CLEAN
    return EntryResult(
        entry=entry,
        findings=findings,
        outcome=outcome,
        extraction_error=extraction_error,
        off_target_contradictions=off_target,
    )


def _tally(counts: dict[str, int], r: EntryResult) -> None:
    """Fold one entry into a counts dict. An unscored entry (an unextracted
    control) is counted only in 'unscored', never in the controls
    denominator: it proves nothing about the false-positive rate."""
    if r.outcome == UNSCORED:
        counts["unscored"] += 1
        return
    counts["lies" if r.entry.planted_lie else "controls"] += 1
    key = {CAUGHT: "caught", MISSED: "missed",
           FALSE_POSITIVE: "false_positives", CLEAN: "clean"}[r.outcome]
    counts[key] += 1


@dataclass(frozen=True)
class EvalReport:
    entries: tuple[EntryResult, ...]

    def totals(self) -> dict[str, int]:
        t = {"lies": 0, "controls": 0, "caught": 0, "missed": 0,
             "false_positives": 0, "clean": 0, "unscored": 0}
        for r in self.entries:
            _tally(t, r)
        return t

    def rows(self) -> dict[ClaimType, dict[str, int]]:
        rows = {
            ct: {"lies": 0, "controls": 0, "caught": 0, "missed": 0,
                 "false_positives": 0, "clean": 0, "unscored": 0}
            for ct in ClaimType
        }
        for r in self.entries:
            _tally(rows[r.entry.claim_type], r)
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
                f"extraction ({names}); scored fail-closed: a lie counts as "
                f"missed, a control is unscored and excluded from the "
                f"controls and false-positive columns. Not verified."
            )
        # controls' off-target contradictions are already visible as false
        # positives in the table; the invisible case is a lie entry's
        off_target = sum(
            r.off_target_contradictions for r in self.entries
            if r.entry.planted_lie
        )
        if off_target:
            lines.append("")
            lines.append(
                f"{off_target} off-target contradiction(s) on planted-lie "
                f"entries (a CONTRADICTED verdict that does not match the "
                f"planted claim's type and sentence); disclosed here, never "
                f"counted as caught."
            )
        return "\n".join(lines)


def _entry_prompt_key(entry: CatalogEntry) -> str:
    """The replay-store key for an entry's extraction prompt."""
    urn = _URN_TEMPLATE.format(table=entry.table)
    return _prompt_key(
        SYSTEM_PROMPT, _user_prompt(urn, entry.column, entry.description)
    )


def missing_fixtures(fixtures: Path) -> list[str]:
    """Manifest entries whose captured completion is absent from the store,
    excluding the declared provider-blocked keys. Non-empty means the store
    cannot represent a complete run; every consumer that replays the full
    manifest (the eval CLI, the replay-data capture) must refuse to proceed
    rather than score or publish a partial run."""
    return [
        f"{e.table}.{e.column or '(table)'}"
        for e in MANIFEST.claims
        if _entry_prompt_key(e) not in KNOWN_UNCAPTURABLE
        and not (fixtures / f"{_entry_prompt_key(e)}.json").exists()
    ]


def unexpected_failures(report: EvalReport) -> list[str]:
    """Entries whose extraction failed outside the declared provider-blocked
    set (a fixture present but malformed, or drifted from its prompt).
    Non-empty means the run is not a valid evaluation."""
    return [
        f"{r.entry.table}.{r.entry.column or '(table)'}"
        for r in report.entries
        if r.extraction_error
        and _entry_prompt_key(r.entry) not in KNOWN_UNCAPTURABLE
    ]


def evaluate(
    manifest: Manifest,
    con: duckdb.DuckDBPyConnection,
    llm: LLMClient,
    as_of: str | None = None,
) -> EvalReport:
    """Run the pipeline over every manifest entry and score it.

    as_of anchors freshness probes (ISO date). Unanchored freshness claims
    fall to UNVERIFIABLE; the probe never reads the wall clock."""
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
                adjudicate(claim, run_probe(plan_probe(
                    claim, as_of=as_of,
                    reconciliation=manifest.reconciliations.get(
                        (entry.table, claim.field_path)
                    ),
                ), con))
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

    # Fleet + pipeline fix (fail-open exit code): the fixtures store must
    # carry EVERY prompt the manifest requires, except the declared
    # provider-blocked keys. Without this a partially populated (or wrong)
    # store changes the table and still exits 0, which any script consuming
    # the exit code reads as a valid evaluation.
    fixtures = Path(args.fixtures)
    if not fixtures.is_dir() or not any(fixtures.glob("*.json")):
        print(
            f"error: fixtures directory {fixtures} is missing or has no "
            f"captured completions; run from the repo root or pass --fixtures",
            file=sys.stderr,
        )
        return 2
    missing = missing_fixtures(fixtures)
    if missing:
        print(
            f"error: fixtures store {fixtures} is missing captured "
            f"completions for: {', '.join(missing)}; refusing to score a "
            f"partial run as a valid evaluation",
            file=sys.stderr,
        )
        return 2

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        # the seeded warehouse's frozen "today": freshness staleness is
        # measured against the same anchor the data was generated around,
        # never the wall clock, so the table is reproducible on any day
        report = evaluate(MANIFEST, con, ReplayLLM(fixtures), as_of=ANCHOR_DATE)
    finally:
        con.close()
    print(report.to_markdown())
    unexpected = unexpected_failures(report)
    if unexpected:
        names = ", ".join(unexpected)
        print(
            f"error: extraction failed unexpectedly for {names}; the table "
            f"above is disclosed but this run is not a valid evaluation",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
