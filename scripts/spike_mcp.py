#!/usr/bin/env python3
"""Day-0 spike: verify the DataHub MCP server surface against local quickstart.

Spawns mcp-server-datahub over stdio, lists tools, runs one read probe, and
reports which of the spec's load-bearing tools exist. Exit nonzero on any
missing hard dependency so the result is a real gate, not a vibe.
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Tools the spec's write-back loop depends on (notary-spec.md, Core loop step 5).
HARD_READ = ["search", "get_lineage"]
# Names vary by server version; count a tool present if any candidate matches.
CANDIDATES = {
    "entity_read": ["get_entities", "get_entity"],
    "query_history": ["get_dataset_queries", "list_queries"],
    "description_write": ["update_description", "update_dataset_description"],
    "structured_props_write": ["add_structured_properties", "upsert_structured_properties"],
    "document_write": ["save_document", "create_document"],
    "tag_write": ["add_tags", "add_tag"],
}


async def main() -> int:
    env = dict(os.environ)
    env.setdefault("DATAHUB_GMS_URL", "http://localhost:8080")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_server_datahub"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            print(f"tools ({len(tools)}): {sorted(tools)}")

            missing_hard = [t for t in HARD_READ if t not in tools]
            surface = {
                key: next((c for c in cands if c in tools), None)
                for key, cands in CANDIDATES.items()
            }
            print("surface map:", json.dumps(surface, indent=1))

            result = await session.call_tool(
                "search", {"query": "*", "num_results": 3}
            )
            text = "".join(
                c.text for c in result.content if getattr(c, "text", None)
            )
            hits = text.count("urn:li:")
            print(f"search probe: {hits} urn mentions in response "
                  f"({len(text)} chars)")

            if missing_hard:
                print(f"FAIL missing hard read tools: {missing_hard}")
                return 1
            if hits == 0:
                print("FAIL search returned no entities (sample data absent?)")
                return 1
            absent = [k for k, v in surface.items() if v is None]
            # Write surface may legitimately be SDK-fallback; report, don't fail.
            if absent:
                print(f"NOTE absent from MCP (SDK fallback required): {absent}")
            print("SPIKE PASS")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
