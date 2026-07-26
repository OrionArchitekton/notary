#!/usr/bin/env python3
"""Capture the replay-page screenshots the Devpost gallery shows.

The gallery is judge-facing evidence, so it must be regenerated from the
CURRENT committed replay payload whenever that payload changes; a stale
image showing an older scorecard reads as a contradiction of the live page.

Only the replay-page shots are captured here. 02-run-cli, 03-datahub-schema
and 04-datahub-incident come from a terminal and the DataHub UI and are
captured by hand.

playwright is a MAINTAINER-only requirement, deliberately not a runtime
dependency of the package: judges install and run notary, never this. Run it
under the interpreter that has playwright (python3.11 in this estate):

    python3.11 scripts/capture_screenshots.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
VIEWPORT = {"width": 1920, "height": 1080}

# anchor -> output name. Each shot scrolls its anchor to the top of the
# viewport so the framing is deterministic across runs.
SHOTS = [
    ("#disclosure", "01-replay-hero.png"),
    ("#s5-view1", "05-s5-flip.png"),
    ("#eval-table", "06-honest-table.png"),
]

RECEIPT_SHOT = "07-mcp-lineage-receipt.png"

# The dossier renders inside a COLLAPSED <details>, and the MCP receipt sits
# at the end of its evidence JSON. Scrolling to the finding alone captures a
# closed disclosure triangle: an image that proves nothing. Open the dossier
# that actually carries the receipt and frame the end of that block.
FRAME_RECEIPT = """() => {
    const blocks = [...document.querySelectorAll('#findings details')];
    const d = blocks.find(b => b.textContent.includes('lineage_gate'));
    if (!d) return false;
    d.open = true;
    const pre = d.querySelector('pre') || d;
    const r = pre.getBoundingClientRect();
    window.scrollBy(0, r.bottom - window.innerHeight + 120);
    return true;
}"""


def _launch(p):
    """Prefer branded Chrome so the published gallery matches what a
    reviewer sees, but fall back to the bundled Chromium instead of failing:
    playwright does not install branded Chrome, so requiring it would make
    this tool unrunnable on an otherwise correctly set up machine."""
    try:
        return p.chromium.launch(channel="chrome")
    except Exception:
        return p.chromium.launch()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "screenshots"))
    parser.add_argument("--page", default=str(ROOT / "web" / "index.html"))
    parser.add_argument(
        "--payload", default=str(ROOT / "web" / "replay-data.json")
    )
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "error: playwright is not importable; run this under the "
            "interpreter that has it (python3.11 in this estate)",
            file=sys.stderr,
        )
        return 2

    # Identify the run from the payload the page serves rather than a
    # hard-coded measurement, so this gate keeps working when the seeded
    # numbers change and never silently passes on a different run.
    run_id = json.loads(Path(args.payload).read_text()).get("run_date")
    if not run_id:
        print(
            f"error: {args.payload} carries no run_date to identify the "
            f"captured run",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out_dir)
    page_url = Path(args.page).resolve().as_uri()

    # Capture into staging and promote only once every shot succeeded. A
    # failure partway through must not leave a half-updated gallery, where
    # some images show this run and the rest show the previous one.
    with tempfile.TemporaryDirectory() as staging:
        stage = Path(staging)
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                page = browser.new_page(viewport=VIEWPORT)
                page.goto(page_url)
                page.wait_for_selector("#eval-table table", timeout=15_000)
                content = page.content()
                # every prerequisite is checked BEFORE the first write
                if run_id not in content:
                    print(
                        f"error: the page did not render the captured run "
                        f"{run_id}; refusing to publish screenshots of a "
                        f"different or empty run",
                        file=sys.stderr,
                    )
                    return 3
                if "lineage_gate" not in content:
                    print(
                        "error: no dossier on the page carries a lineage "
                        "receipt; refusing to publish a gallery that claims "
                        "an MCP-gated verdict without showing it",
                        file=sys.stderr,
                    )
                    return 4
                for anchor, name in SHOTS:
                    page.eval_on_selector(
                        anchor, "el => el.scrollIntoView({block: 'start'})"
                    )
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(stage / name))
                if not page.evaluate(FRAME_RECEIPT):
                    print(
                        "error: could not open the dossier carrying the "
                        "lineage receipt",
                        file=sys.stderr,
                    )
                    return 4
                page.wait_for_timeout(250)
                page.screenshot(path=str(stage / RECEIPT_SHOT))
            finally:
                browser.close()

        out_dir.mkdir(parents=True, exist_ok=True)
        for captured in sorted(stage.iterdir()):
            shutil.move(str(captured), str(out_dir / captured.name))
            print(f"wrote {out_dir / captured.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
