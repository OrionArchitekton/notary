#!/usr/bin/env python3
"""Capture the replay-page screenshots the Devpost gallery shows.

The gallery is judge-facing evidence, so it must be regenerated from the
CURRENT committed replay payload whenever that payload changes; a stale
image showing an older scorecard reads as a contradiction of the live page.

Only the replay-page shots are captured here. 02-run-cli, 03-datahub-schema
and 04-datahub-incident come from a terminal and the DataHub UI and are
captured by hand.

Needs playwright (python3.11 in this estate):
    python3.11 scripts/capture_screenshots.py
"""
from __future__ import annotations

import argparse
import sys
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(ROOT / "docs" / "screenshots"))
    parser.add_argument("--page", default=str(ROOT / "web" / "index.html"))
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

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    page_url = Path(args.page).resolve().as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        try:
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(page_url)
            page.wait_for_selector("#eval-table table", timeout=15_000)
            # fail closed: the payload must actually have rendered, or the
            # shots would freeze an empty page as the published evidence
            if "12795" not in page.content():
                print(
                    "error: the replay page did not render its captured run; "
                    "refusing to publish empty screenshots",
                    file=sys.stderr,
                )
                return 3
            for anchor, name in SHOTS:
                page.eval_on_selector(
                    anchor, "el => el.scrollIntoView({block: 'start'})"
                )
                page.wait_for_timeout(250)
                page.screenshot(path=str(out_dir / name))
                print(f"wrote {out_dir / name}")

            if not page.evaluate(FRAME_RECEIPT):
                print(
                    "error: no findings dossier on the page carries a "
                    "lineage receipt; refusing to publish a gallery image "
                    "that claims an MCP-gated verdict without showing it",
                    file=sys.stderr,
                )
                return 4
            page.wait_for_timeout(250)
            page.screenshot(path=str(out_dir / RECEIPT_SHOT))
            print(f"wrote {out_dir / RECEIPT_SHOT}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
