"""Example in-house agent: Claude answers questions using the Lemnisca Media KB MCP server.

Starts the knowledge base's MCP server as a local subprocess (stdio), hands its tools to
Claude through the Anthropic SDK's tool runner, and prints a cited answer.

    set ANTHROPIC_API_KEY=sk-ant-...        (or add it to .env)
    .venv\\Scripts\\python examples\\agent.py "What is known about glutamine stability at 37 C?"

Costs: each question is one short agent run on Claude Opus 5 (a few tool calls).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")  # ANTHROPIC_API_KEY may live here too

MODEL = "claude-opus-5"
SYSTEM = """You answer questions for Lemnisca's cell-culture media team using ONLY the
lemnisca-media-kb tools. For every factual claim:
- cite the DOI (and the passage_id when you quote a paper);
- say the evidence level behind it: full_text, abstract, or metadata_only;
- flag it if the citation's DOI was not verified or has an issue.
If the knowledge base has no data on something, say so plainly; do not fill gaps from
general knowledge unless you clearly label it as general knowledge."""

SERVER = StdioServerParameters(
    command=str(ROOT / ".venv" / "Scripts" / "python.exe"),
    args=["-m", "lemkb.mcp_server"],
    cwd=str(ROOT),
    env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
)


async def ask(question: str) -> str:
    client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = [async_mcp_tool(t, mcp) for t in (await mcp.list_tools()).tools]

            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                thinking={"type": "adaptive"},
                # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                tools=tools,
                messages=[{"role": "user", "content": question}],
            )
            final = None
            async for message in runner:  # one message per turn; tools run between turns
                final = message
                for block in message.content:
                    if block.type == "tool_use":
                        print(f"  → {block.name}({block.input})", file=sys.stderr)

    if final is None:
        return "(no response)"
    if final.stop_reason == "refusal":
        return "Claude declined to answer this request."
    if final.stop_reason == "max_tokens":
        print("(answer was cut off at max_tokens)", file=sys.stderr)
    return "".join(b.text for b in final.content if b.type == "text")


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Which transporters carry alanine in CHO cells? Cite the sources."
    print(asyncio.run(ask(q)))
