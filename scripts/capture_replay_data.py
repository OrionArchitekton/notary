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
from notary.run import expected_upstream_urn  # noqa: E402

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


LINEAGE_RECEIPT = "tests/fixtures/lineage/flagship-lineage-receipt.json"


def _lineage_receipt(path: str, flagship) -> dict:
    """The captured MCP `get_lineage` receipt that AUTHORIZED the flagship
    contradiction, replayed verbatim. Same contract as the captured
    completions: a real read, frozen once by
    scripts/capture_lineage_receipt.py, never re-issued at page-build time
    (so two builds stay byte-identical and the page builds without a
    quickstart).

    Fail-closed like _s5_views: the receipt must be a VERIFIED read of THIS
    run's flagship asset whose matched upstream is the reconciliation
    reference THIS run actually probed. An unverified receipt, one for
    another asset, or one naming a reference this run never touched is a
    stale capture and aborts before any output is written."""
    receipt = json.loads(Path(path).read_text())
    reference = receipt.get("reference_table")
    probe_sql = flagship.evidence.get("probe_sql", "")
    # Take the required reference from the MANIFEST this run evaluated, and
    # derive the required upstream from THAT, so nothing the receipt carries
    # can influence what it is checked against. Deriving from the receipt's
    # own reference let a doctored one name a SUBSTRING of the real table
    # ("invoices" inside "billing_invoices"), re-derive both urn fields to
    # match it, and pass every check (review finding, PR #13).
    recon = MANIFEST.reconciliations.get((PAYMENTS_TABLE, "amount"))
    required_reference = recon.table if recon else None
    derived = (
        expected_upstream_urn(FLAGSHIP_URN, required_reference)
        if required_reference
        else None
    )
    checks = {
        "is a get_lineage read over MCP": (
            receipt.get("tool") == "get_lineage"
            and receipt.get("transport") == "mcp"
        ),
        "verified (authorized the verdict)": receipt.get("verified") is True,
        "reads THIS run's flagship asset": (
            receipt.get("asset_urn") == FLAGSHIP_URN
        ),
        "matched the upstream THIS run requires": bool(derived)
        and receipt.get("upstream_urn") == derived
        and receipt.get("expected_upstream_urn") == derived,
        "names the reference THIS run reconciled against": (
            required_reference is not None and reference == required_reference
        ),
        # and that reference must really appear in this run's probe, as a
        # QUOTED identifier: bare substring membership matched a fake
        # reference nested inside the real table name
        "probed that reference in THIS run": (
            bool(required_reference)
            and f'"{required_reference}"' in probe_sql
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ValueError(
            f"captured lineage receipt {path} cannot be published beside "
            f"this run; it is not: {'; '.join(failed)}"
        )
    return receipt


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
    parser.add_argument("--lineage-receipt", default=LINEAGE_RECEIPT)
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

    entries = [r for r in report.entries if r.entry.table == PAYMENTS_TABLE]
    flagship = None
    for r in entries:
        for f in r.findings:
            if f.verdict.value == "CONTRADICTED" and f.claim.field_path == "amount":
                flagship = f
    if flagship is None:
        raise RuntimeError("the flagship cents lie was not contradicted")

    # The MCP read that authorized this contradiction rides in the SAME
    # evidence dict the dossier renders, so the hosted page shows the
    # DataHub read behind the verdict, not just the verdict.
    try:
        flagship.evidence["lineage_gate"] = _lineage_receipt(
            args.lineage_receipt, flagship
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    findings_out = []
    after_description = None
    for r in entries:
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
            if f is flagship:
                after_description = _corrected_description(f, ANCHOR_DATE)
    if after_description is None:
        raise RuntimeError("the flagship cents lie was not contradicted")

    # Positive proof the receipt reached the page a judge reads, not merely
    # that no validation raised on the way there.
    published = next(i for i in findings_out if i["field"] == "amount")
    if '"lineage_gate"' not in published["dossier_markdown"]:
        print(
            "error: the flagship dossier does not carry the lineage "
            "receipt; refusing to publish a replay that claims an MCP-gated "
            "verdict without showing the read",
            file=sys.stderr,
        )
        return 5

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
