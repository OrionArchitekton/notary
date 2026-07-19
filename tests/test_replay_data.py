"""S6 slice 1: the replay-data capture is complete and reproducible.

The ONE behavior this locks: one command assembles web/replay-data.json
from the SAME frozen inputs the test suite replays (seeded warehouse,
captured completions), carrying everything the hosted page shows: the
honest eval table, per-finding evidence, before/after catalog text, the
S5 answer flip, and the disclosure that this is a captured run. Two runs
are byte-identical; nothing is fabricated at page-build time.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _capture(tmp_path, name):
    out = tmp_path / name
    r = subprocess.run(
        [
            sys.executable, "scripts/capture_replay_data.py",
            "--out", str(out),
            "--db", str(tmp_path / f"wh-{name}.duckdb"),
        ],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stderr
    return out


def test_replay_data_is_complete_and_reproducible(tmp_path):
    first = _capture(tmp_path, "a.json")
    second = _capture(tmp_path, "b.json")
    assert first.read_bytes() == second.read_bytes()  # reproducible

    data = json.loads(first.read_text())
    # disclosure is data, not an afterthought the page could drop
    assert "captured" in data["disclosure"].lower()
    assert "2026-07-18" in data["run_date"]
    # the honest table rides along verbatim
    assert "| claim type |" in data["eval_table_markdown"]
    assert "| **total** | 12 | 9 |" in data["eval_table_markdown"]
    # per-finding evidence for the flagship asset
    findings = data["findings"]
    assert any(
        f["field"] == "amount" and f["verdict"] == "CONTRADICTED"
        for f in findings
    )
    amount = next(f for f in findings if f["field"] == "amount")
    assert "12795" in amount["rationale"]
    assert amount["dossier_markdown"].startswith("#")
    # before/after catalog text
    assert data["before_description"] == "Transaction amount in USD."
    assert "Notary" in data["after_description"]
    # the S5 flip, straight from the committed captures
    assert "USD" in data["s5"]["view1"]
    assert "cents" in data["s5"]["view2"].lower()
    assert "12795" in data["s5"]["view2"]
