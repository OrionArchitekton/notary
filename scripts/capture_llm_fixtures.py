#!/usr/bin/env python3
"""Capture real LLM completions for the extraction replay fixtures.

Derives the capture set from the seeder MANIFEST (every described entry the
S7 eval harness prompts for) plus the extra prompts the extract unit tests
replay, runs them against the live Anthropic API, and writes fixtures to
tests/fixtures/llm/ with provenance metadata.

Idempotent by prompt key: an existing fixture is NEVER re-captured. Current
Claude models accept no sampling knobs, so a re-capture would silently swap
the pinned completion for a different one and break strict-replay tests.

Run under an environment that carries ANTHROPIC_API_KEY (e.g. doppler):

    python scripts/capture_llm_fixtures.py --captured-at 2026-07-18
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notary.demo.seeder import MANIFEST  # noqa: E402
from notary.extract import (  # noqa: E402
    KNOWN_UNCAPTURABLE as KNOWN_BLOCKED,
    SYSTEM_PROMPT,
    AnthropicLLM,
    CaptureLLM,
    ExtractionParseError,
    _parse_completion,
    _prompt_key,
    _user_prompt,
)

_URN_TEMPLATE = (
    "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.{table},PROD)"
)

# Extra prompts outside the manifest that unit tests replay.
EXTRA_CASES = [
    (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        "customer_id",
        "Primary key.",
    ),
]


def cases():
    for entry in MANIFEST.claims:
        yield (
            _URN_TEMPLATE.format(table=entry.table),
            entry.column,
            entry.description,
        )
    yield from EXTRA_CASES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--captured-at", required=True)
    ap.add_argument("--out", default="tests/fixtures/llm")
    args = ap.parse_args()

    live = AnthropicLLM()
    cap = CaptureLLM(
        live,
        args.out,
        meta={
            "model": live.model,
            "captured_at": args.captured_at,
            "note": "real completion captured for strict replay in unit tests",
        },
    )
    captured = skipped = failed = blocked = 0
    for urn, field_path, description in cases():
        user = _user_prompt(urn, field_path, description)
        key = _prompt_key(SYSTEM_PROMPT, user)
        fixture = Path(args.out) / f"{key}.json"
        if fixture.exists():
            skipped += 1
            continue
        label = f"{field_path or '(table)'} {description[:40]!r}"
        if key in KNOWN_BLOCKED:
            blocked += 1
            print(f"skipping {label}: known provider block ({key})", flush=True)
            continue
        print(f"capturing {label} ...", flush=True)
        try:
            raw = cap.complete(SYSTEM_PROMPT, user)
            # Fleet-review fix: validate BEFORE the fixture is kept. A
            # malformed completion written once would be pinned forever by
            # the never-re-capture rule; dropping it here means the next run
            # retries instead.
            _parse_completion(raw)
        except ExtractionParseError as e:
            fixture.unlink(missing_ok=True)
            failed += 1
            print(f"  FAILED (unparseable, fixture discarded) {label}: {e}",
                  flush=True)
            continue
        except Exception as e:  # report and continue; retry the stragglers
            failed += 1
            print(f"  FAILED {label}: {e}", flush=True)
            continue
        captured += 1
        print(f"  ok: {len(raw)} chars", flush=True)
    print(
        f"done: {captured} captured, {skipped} already present, "
        f"{blocked} known-blocked, {failed} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
