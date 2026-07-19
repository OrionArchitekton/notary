"""S6 slice 1: the replay-data capture is complete and reproducible.

The ONE behavior this locks: one command assembles web/replay-data.json
from the SAME frozen inputs the test suite replays (seeded warehouse,
captured completions), carrying everything the hosted page shows: the
honest eval table, per-finding evidence, before/after catalog text, the
S5 answer flip, and the disclosure that this is a captured run. Two runs
are byte-identical; nothing is fabricated at page-build time.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from notary.demo.seeder import MANIFEST
from notary.eval import _entry_prompt_key
from notary.extract import KNOWN_UNCAPTURABLE, _prompt_key

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from s5_next_agent import S5_SYSTEM  # noqa: E402


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


def _broken_store(tmp_path):
    """A copy of the fixture store plus the path of one required capture
    that is neither the flagship (fct_payments) nor provider-blocked, so
    the flagship contradiction alone cannot carry a partial run."""
    fixtures = tmp_path / "llm"
    shutil.copytree(ROOT / "tests" / "fixtures" / "llm", fixtures)
    victim = next(
        e for e in MANIFEST.claims
        if e.table != "fct_payments"
        and _entry_prompt_key(e) not in KNOWN_UNCAPTURABLE
    )
    return fixtures, fixtures / f"{_entry_prompt_key(victim)}.json"


def _capture_with_fixtures(tmp_path, fixtures):
    out = tmp_path / "replay-data.json"
    r = subprocess.run(
        [
            sys.executable, "scripts/capture_replay_data.py",
            "--out", str(out),
            "--db", str(tmp_path / "wh.duckdb"),
            "--fixtures", str(fixtures),
        ],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )
    return r, out


def test_capture_rejects_missing_fixture(tmp_path):
    """The ONE behavior this locks: a fixtures store missing a required
    (non-provider-blocked) capture is rejected with exit 2 BEFORE any
    replay artifact is written, the same fail-closed completeness gate
    notary.eval.main applies. Without it, the documented regeneration
    command can publish a partial evaluation as a completed replay."""
    fixtures, victim = _broken_store(tmp_path)
    victim.unlink()
    r, out = _capture_with_fixtures(tmp_path, fixtures)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "missing" in r.stderr
    assert not out.exists()
    assert not out.with_suffix(".js").exists()


def test_capture_rejects_corrupt_fixture(tmp_path):
    """The ONE behavior this locks: a capture that exists but fails replay
    (malformed JSON) aborts with exit 3 and writes nothing, the same
    unexpected-extraction gate notary.eval.main applies."""
    fixtures, victim = _broken_store(tmp_path)
    victim.write_text("{not valid json")
    r, out = _capture_with_fixtures(tmp_path, fixtures)
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "extraction failed" in r.stderr
    assert not out.exists()
    assert not out.with_suffix(".js").exists()


def test_capture_rejects_unbound_s5_fixture(tmp_path):
    """The ONE behavior this locks: an S5-noted fixture whose filename does
    not match the prompt key recomputed from its stored user prompt (a
    copied, renamed, or stray capture) aborts the run before anything is
    written. Without the binding, the free-form note plus a substring is
    the only selector, and a smuggled record can be published as the
    same-asset before/after agent evidence."""
    fixtures = tmp_path / "llm"
    shutil.copytree(ROOT / "tests" / "fixtures" / "llm", fixtures)
    s5 = next(
        p for p in fixtures.glob("*.json")
        if json.loads(p.read_text())
        .get("meta", {}).get("note", "").startswith("S5")
    )
    record = json.loads(s5.read_text())
    record["completion"] = "An unrelated completion smuggled in as evidence."
    (fixtures / "ffffffffffffffffffffffff.json").write_text(
        json.dumps(record)
    )
    r, out = _capture_with_fixtures(tmp_path, fixtures)
    assert r.returncode != 0, (r.returncode, r.stdout)
    assert not out.exists()
    assert not out.with_suffix(".js").exists()


def test_capture_rejects_stale_s5_evidence(tmp_path):
    """The ONE behavior this locks: an S5 view2 capture whose embedded
    catalog context disagrees with the freshly generated evidence (here, a
    different measured median in the flagship dossier line) aborts the run.
    The prompt-key binding alone proves file integrity, not coherence with
    the evaluation shown beside it as one captured run."""
    fixtures = tmp_path / "llm"
    shutil.copytree(ROOT / "tests" / "fixtures" / "llm", fixtures)
    view2 = next(
        p for p in fixtures.glob("*.json")
        if (r := json.loads(p.read_text()))
        .get("meta", {}).get("note", "").startswith("S5")
        and "trust ledger verdict" in r["user"]
    )
    record = json.loads(view2.read_text())
    stale_user = record["user"].replace("12795", "99999")
    assert stale_user != record["user"]
    record["user"] = stale_user
    view2.unlink()
    # self-consistent: filed under the key its altered prompt hashes to
    (fixtures / f"{_prompt_key(S5_SYSTEM, stale_user)}.json").write_text(
        json.dumps(record)
    )
    r, out = _capture_with_fixtures(tmp_path, fixtures)
    assert r.returncode != 0, (r.returncode, r.stdout)
    assert not out.exists()
    assert not out.with_suffix(".js").exists()


def test_committed_web_assets_match_generated(tmp_path):
    """The ONE behavior this locks: the CHECKED-IN web/replay-data.json and
    web/replay-data.js (what the deployed page actually serves) are
    byte-identical to a fresh capture. Generator determinism alone cannot
    catch a stale committed payload after a source or fixture change."""
    fresh = _capture(tmp_path, "fresh.json")
    assert (ROOT / "web" / "replay-data.json").read_bytes() == fresh.read_bytes()
    assert (
        (ROOT / "web" / "replay-data.js").read_bytes()
        == fresh.with_suffix(".js").read_bytes()
    )


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
