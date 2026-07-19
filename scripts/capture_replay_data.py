#!/usr/bin/env python3
"""S6: assemble the frozen replay data the hosted page shows.

Everything comes from the SAME frozen inputs the test suite replays: the
seeded warehouse, the captured LLM completions, and the real dossier and
correction generators (the exact content Notary writes to DataHub).
Nothing is fabricated at page-build time, and two runs are byte-identical.

Usage:
    python scripts/capture_replay_data.py --out web/replay-data.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import duckdb  # noqa: E402

from notary.catalog import _corrected_description, _dossier_markdown  # noqa: E402
from notary.demo.seeder import ANCHOR_DATE, DEFAULT_SEED, MANIFEST, build_warehouse  # noqa: E402
from notary.eval import evaluate, missing_fixtures, unexpected_failures  # noqa: E402
from notary.extract import ReplayLLM, _prompt_key  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from s5_next_agent import _ISO_DATE_RE, S5_SYSTEM, build_question  # noqa: E402

PAYMENTS_TABLE = "fct_payments"
FLAGSHIP_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
    "fiction_retail.fct_payments,PROD)"
)

DISCLOSURE = (
    "This page is a frozen, reproducible replay of the recorded demo run "
    "(run date 2026-07-18), assembled from that run's inputs: the seeded "
    "demo warehouse, the captured Claude extractions (replayed verbatim), "
    "and Notary's own write-back formatters, plus two separately captured "
    "agent answers that are prompt-bound to this evaluation's evidence. "
    "Nothing is generated when this page loads. Full local-run instructions "
    "live in the repository README."
)


def _s5_views(fixtures_dir: str, required_view2: tuple[str, ...] = ()) -> dict:
    """Select the two S5 answer captures, fail-closed (pipeline findings:
    the free-form note plus a substring was the only selector, so a copied
    or stray S5-noted record could be published as the same-asset agent
    evidence). Each record must (a) sit under the prompt key recomputed
    from S5_SYSTEM plus its stored user prompt (the ReplayLLM binding
    rule), (b) carry the flagship asset's question, and (c) fill an empty
    view slot; the view2 prompt must additionally (d) embed every
    required_view2 fragment, which the caller builds from the FRESH
    evaluation, so a stale capture cannot sit beside this run's evidence
    as one captured run. Any violation aborts before output is written."""
    views = {}
    view2_user = ""
    question = build_question(FLAGSHIP_URN)
    for path in glob.glob(f"{fixtures_dir}/*.json"):
        p = Path(path)
        record = json.loads(p.read_text())
        note = (record.get("meta") or {}).get("note", "")
        if not note.startswith("S5 next-agent"):
            continue
        user = record.get("user", "")
        if _prompt_key(S5_SYSTEM, user) != p.stem:
            raise RuntimeError(
                f"S5 fixture {p.name} does not match its prompt key "
                f"(file copied, renamed, or edited?); refusing to publish "
                f"it as captured agent evidence"
            )
        if question not in user:
            raise RuntimeError(
                f"S5 fixture {p.name} does not ask the flagship asset's "
                f"question; refusing to publish it as same-asset evidence"
            )
        key = "view2" if "trust ledger verdict" in user else "view1"
        if key in views:
            raise RuntimeError(
                f"duplicate S5 capture for {key} ({p.name}); refusing to "
                f"pick one silently"
            )
        if key == "view2":
            view2_user = user
        views[key] = record["completion"].strip()
    if set(views) != {"view1", "view2"}:
        raise RuntimeError(
            f"expected both S5 answer captures, found {sorted(views)}"
        )
    for fragment in required_view2:
        if fragment not in view2_user:
            raise RuntimeError(
                f"the S5 view2 capture's embedded catalog context does not "
                f"carry this run's flagship evidence ({fragment!r}); the "
                f"capture is stale relative to the evaluation it would be "
                f"published beside; re-capture the S5 answers"
            )
    return views


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="web/replay-data.json")
    parser.add_argument("--db", default=".notary/replay-wh.duckdb")
    parser.add_argument("--fixtures", default="tests/fixtures/llm")
    args = parser.parse_args(argv)

    # Same fail-closed gates as notary.eval.main (pipeline finding: without
    # them, a missing or corrupt fixture still exits 0 and publishes a
    # partial evaluation as the frozen complete replay). Both run BEFORE any
    # output file is written.
    fixtures = Path(args.fixtures)
    missing = missing_fixtures(fixtures)
    if missing:
        print(
            f"error: fixtures store {fixtures} is missing captured "
            f"completions for: {', '.join(missing)}; refusing to publish a "
            f"partial run as the frozen replay",
            file=sys.stderr,
        )
        return 2

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    build_warehouse(db, seed=DEFAULT_SEED)
    con = duckdb.connect(str(db), read_only=True)
    try:
        report = evaluate(
            MANIFEST, con, ReplayLLM(args.fixtures), as_of=ANCHOR_DATE
        )
    finally:
        con.close()

    unexpected = unexpected_failures(report)
    if unexpected:
        print(
            f"error: extraction failed unexpectedly for "
            f"{', '.join(unexpected)}; refusing to publish a partial run as "
            f"the frozen replay",
            file=sys.stderr,
        )
        return 3

    payments = {
        e.column: e for e in MANIFEST.claims if e.table == PAYMENTS_TABLE
    }
    before = payments["amount"].description

    findings_out = []
    after_description = None
    flagship = None
    for r in report.entries:
        if r.entry.table != PAYMENTS_TABLE:
            continue
        for f in r.findings:
            item = {
                "field": f.claim.field_path or "(table)",
                "claim_text": f.claim.text,
                "verdict": f.verdict.value,
                "rationale": f.rationale,
                "dossier_markdown": _dossier_markdown(
                    f, ANCHOR_DATE,
                    pre_image=r.entry.description,
                ),
            }
            findings_out.append(item)
            if f.verdict.value == "CONTRADICTED" and f.claim.field_path == "amount":
                after_description = _corrected_description(f, ANCHOR_DATE)
                flagship = f
    if after_description is None or flagship is None:
        raise RuntimeError("the flagship cents lie was not contradicted")

    # The S5 view2 prompt embeds the catalog context its answer was captured
    # against; this run's flagship dossier line must appear in it verbatim
    # (dates canonicalized), or the capture is stale relative to the
    # evaluation it would be published beside.
    flagship_line = _ISO_DATE_RE.sub(
        "<run-date>",
        f"field={flagship.claim.field_path}; verdict={flagship.verdict.value}; "
        f"rationale={flagship.rationale}",
    )

    data = {
        "disclosure": DISCLOSURE,
        "run_date": ANCHOR_DATE,
        "repo": "https://github.com/OrionArchitekton/notary",
        "eval_table_markdown": report.to_markdown(),
        "findings": sorted(findings_out, key=lambda x: x["field"]),
        "before_description": before,
        "after_description": after_description,
        "s5": _s5_views(args.fixtures, required_view2=(flagship_line,)),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=1, sort_keys=True)
    out.write_text(payload + "\n")
    # sibling .js so the static page needs no fetch (works from any host
    # and from file://)
    out.with_suffix(".js").write_text(
        "window.REPLAY_DATA = " + payload + ";\n"
    )
    print(f"replay data written: {out} (+ .js sibling)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
