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
from notary.extract import ReplayLLM  # noqa: E402

PAYMENTS_TABLE = "fct_payments"

DISCLOSURE = (
    "This page replays a CAPTURED Notary run from the seeded demo warehouse "
    "(run date 2026-07-18). Every verdict, measurement, dossier, and agent "
    "answer shown is a real, frozen artifact produced by that run; nothing "
    "is generated when this page loads. Full local-run instructions live in "
    "the repository README."
)


def _s5_views(fixtures_dir: str) -> dict:
    views = {}
    for path in glob.glob(f"{fixtures_dir}/*.json"):
        record = json.loads(Path(path).read_text())
        note = (record.get("meta") or {}).get("note", "")
        if not note.startswith("S5 next-agent"):
            continue
        user = record.get("user", "")
        key = "view2" if "trust ledger verdict" in user else "view1"
        views[key] = record["completion"].strip()
    if set(views) != {"view1", "view2"}:
        raise RuntimeError(
            f"expected both S5 answer captures, found {sorted(views)}"
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
    if after_description is None:
        raise RuntimeError("the flagship cents lie was not contradicted")

    data = {
        "disclosure": DISCLOSURE,
        "run_date": ANCHOR_DATE,
        "repo": "https://github.com/OrionArchitekton/notary",
        "eval_table_markdown": report.to_markdown(),
        "findings": sorted(findings_out, key=lambda x: x["field"]),
        "before_description": before,
        "after_description": after_description,
        "s5": _s5_views(args.fixtures),
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
