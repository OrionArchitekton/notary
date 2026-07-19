#!/usr/bin/env python3
"""S5: the next agent inherits the verdict.

A SECOND, independent agent session reads the same asset through the stock
DataHub MCP read tools (no mutation flag, no Notary code in the reader
path), canonicalizes what the catalog says into a deterministic context
block, and answers a unit question grounded ONLY in that context. Run
before/after views side by side: without Notary's ledger the agent repeats
the catalog lie; with it, the agent quotes the verified measurement and
refuses the contradicted claim.

Determinism: the context block is a pure function of stable catalog fields
(descriptions, verdict, verified-at, corrected text), so the answer
boundary replays captured completions verbatim. The before view is the
same catalog with Notary's additions withheld, and the output says so.

Usage:
    NOTARY_RUN_DATE=2026-07-18 python scripts/s5_next_agent.py \
        --asset urn:li:dataset:... [--live | --capture]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

QUESTION = (
    "A teammate asks: what unit is fct_payments.amount stored in, and can I "
    "trust the catalog description? Answer in two sentences."
)

_TRUST_PROP_PREFIX = "urn:li:structuredProperty:notary."


def build_prompt(context: str) -> str:
    return (
        "You are a data agent grounded on a catalog. Answer using ONLY the "
        "catalog context below; if the context marks a claim as "
        "contradicted, refuse to repeat it and quote the verified "
        "measurement instead.\n\n"
        f"Catalog context:\n{context}\n\nQuestion: {QUESTION}"
    )


def canonical_context(asset: dict) -> str:
    """Deterministic context block from stable catalog fields only."""
    lines = [f"table description: {asset.get('description', '')}"]
    for field, desc in sorted((asset.get("field_descriptions") or {}).items()):
        lines.append(f"column {field}: {desc}")
    trust = asset.get("trust") or {}
    if trust.get("verdict"):
        lines.append(f"notary trust ledger verdict: {trust['verdict']}")
    if trust.get("verified_at"):
        lines.append(f"notary verified at: {trust['verified_at']}")
    if trust.get("corrected"):
        lines.append(f"notary corrected description: {trust['corrected']}")
    for dossier_line in trust.get("dossier_lines") or []:
        lines.append(f"notary evidence dossier: {dossier_line}")
    return "\n".join(lines)


async def read_asset_via_mcp(gms_url: str, asset_urn: str) -> dict:
    """Read the asset through STOCK mcp-server-datahub read tools (no
    mutation flag). This is the whole reader path: no Notary code."""
    from contextlib import AsyncExitStack

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        k: v for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV")
    }
    env["DATAHUB_GMS_URL"] = gms_url
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server_datahub"], env=env
    )
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        res = await session.call_tool("get_entities", {"urns": [asset_urn]})
        if res.isError:
            raise RuntimeError(f"get_entities failed: {res.content}")
        data = json.loads(res.content[0].text)

        # the trust ledger's evidence property names the dossier documents;
        # grep them (stock tool) for the full verdict and rationale lines
        # the entity view truncates
        dossier_lines: list[str] = []
        entity0 = data[0] if isinstance(data, list) and data else {}
        for entry in (entity0.get("structuredProperties") or {}).get(
            "properties"
        ) or []:
            urn = ((entry.get("structuredProperty") or {}).get("urn") or "")
            if not urn.endswith("notary.evidence"):
                continue
            values = entry.get("values") or []
            text = (values[0] or {}).get("stringValue", "") if values else ""
            doc_urns = re.findall(r"urn:li:document:\S+", text)
            if not doc_urns:
                continue
            doc_urns = [u.rstrip(";,") for u in doc_urns]
            g = await session.call_tool(
                "grep_documents",
                {"urns": doc_urns,
                 "pattern": "- (Field|Verdict|Rationale): .*"},
            )
            if g.isError:
                continue
            gdata = json.loads(g.content[0].text)
            found: set[str] = set()
            for result in gdata.get("results") or []:
                for match in result.get("matches") or []:
                    for line in (match.get("excerpt") or "").splitlines():
                        m = re.match(r"- (Field|Verdict|Rationale): (.+)", line)
                        if m:
                            found.add(f"{m.group(1)}: {m.group(2).strip()}")
            dossier_lines = sorted(found)

    entity = data[0] if isinstance(data, list) and data else {}
    asset: dict = {"description": "", "field_descriptions": {}, "trust": {}}
    props = entity.get("properties") or {}
    asset["description"] = (props.get("description") or "").strip()
    corrected = None
    for f in (entity.get("schemaMetadata") or {}).get("fields") or []:
        if f.get("description"):
            asset["field_descriptions"][f["fieldPath"]] = f["description"]
        edited = f.get("editedDescription")
        if edited and "Notary" in edited:
            corrected = edited
    sp = entity.get("structuredProperties") or {}
    for entry in sp.get("properties") or []:
        urn = ((entry.get("structuredProperty") or {}).get("urn")
               or entry.get("propertyUrn") or "")
        values = entry.get("values") or []
        first = values[0] if values else {}
        value = (
            first.get("stringValue") if isinstance(first, dict) else str(first)
        ) or ""
        if urn == f"{_TRUST_PROP_PREFIX}verdict":
            asset["trust"]["verdict"] = value
        elif urn == f"{_TRUST_PROP_PREFIX}verified_at":
            asset["trust"]["verified_at"] = value
    if corrected:
        asset["trust"]["corrected"] = corrected
    if dossier_lines:
        asset["trust"]["dossier_lines"] = dossier_lines
    return asset


def strip_notary(asset: dict) -> dict:
    """The simulated pre-Notary view: same catalog, Notary additions
    withheld (disclosed in the script output)."""
    return {
        "description": asset.get("description", ""),
        "field_descriptions": dict(asset.get("field_descriptions") or {}),
        "trust": {},
    }


def main(argv: list[str] | None = None) -> int:
    from notary.extract import AnthropicLLM, CaptureLLM, ReplayLLM

    parser = argparse.ArgumentParser()
    parser.add_argument("--gms", default="http://localhost:8080")
    parser.add_argument(
        "--asset",
        default=(
            "urn:li:dataset:(urn:li:dataPlatform:duckdb,"
            "fiction_retail.fct_payments,PROD)"
        ),
    )
    parser.add_argument("--fixtures", default="tests/fixtures/llm")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--capture", action="store_true",
        help="capture live completions into the fixtures store",
    )
    args = parser.parse_args(argv)

    asset = asyncio.run(read_asset_via_mcp(args.gms, args.asset))
    if not asset["trust"].get("verdict"):
        print(
            "error: the asset carries no Notary trust ledger yet; run "
            "python -m notary.run first (S5 reads what S1-S4 wrote)",
            file=sys.stderr,
        )
        return 2

    if args.live or args.capture:
        llm = AnthropicLLM()
        if args.capture:
            llm = CaptureLLM(
                llm, args.fixtures,
                meta={"note": "S5 next-agent answer, captured for replay",
                      "model": AnthropicLLM.MODEL},
            )
    else:
        llm = ReplayLLM(args.fixtures)

    system = "You answer data questions grounded only on provided catalog context."
    before = llm.complete(system, build_prompt(canonical_context(strip_notary(asset))))
    after = llm.complete(system, build_prompt(canonical_context(asset)))
    print("view 1 (pre-Notary catalog view; trust ledger withheld):")
    print(f"  {before.strip()}")
    print("view 2 (same catalog, Notary trust ledger included):")
    print(f"  {after.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
