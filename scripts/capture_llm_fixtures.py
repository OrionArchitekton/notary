#!/usr/bin/env python3
"""Capture real LLM completions for the extraction test fixtures.

Runs the exact prompts the unit tests replay, against the live Anthropic API
with Notary's deterministic settings, and writes them to tests/fixtures/llm/
with provenance metadata. Run under an environment that carries
ANTHROPIC_API_KEY (e.g. doppler). Usage:

    python scripts/capture_llm_fixtures.py --captured-at 2026-07-18
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notary.extract import (  # noqa: E402
    SYSTEM_PROMPT,
    AnthropicLLM,
    CaptureLLM,
    _user_prompt,
)

# (asset_urn, field_path, description): the pairs the unit tests exercise.
CASES = [
    (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.fct_payments,PROD)",
        "amount",
        "Transaction amount in USD.",
    ),
    (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        "email",
        "Customer email address. Never null.",
    ),
    (
        "urn:li:dataset:(urn:li:dataPlatform:duckdb,fiction_retail.dim_customers,PROD)",
        "customer_id",
        "Primary key.",
    ),
]


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
    for urn, field_path, description in CASES:
        raw = cap.complete(SYSTEM_PROMPT, _user_prompt(urn, field_path, description))
        print(f"captured {field_path}: {len(raw)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
