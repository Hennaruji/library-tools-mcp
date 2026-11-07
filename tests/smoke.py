"""End-to-end smoke test: starts the server over stdio, lists the docs, and
fetches them with different include settings.

    python tests/smoke.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "guide_server.py")],
        env={**get_default_environment(), "GUIDE_DIR": str(ROOT / "examples" / "guide")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == {"list_documents", "get_documents"}, tools

            menu = (await session.call_tool("list_documents", {})).content[0].text
            print(menu)
            assert "`auth`" in menu and "`caching`" in menu

            calls = [
                {"docs": ["auth", "caching", "nope"], "include_images": True},
                {"docs": [{"id": "auth", "include_code": False}, "caching"]},
            ]
            for args in calls:
                result = await session.call_tool("get_documents", args)
                assert not result.is_error
                print(f"\n--- get_documents {json.dumps(args)}")
                for block in result.content:
                    if block.type == "text":
                        print(f"[text]{block.text}")
                    else:
                        print(f"[image] {block.mime_type}, {len(block.data)} base64 chars")

            first = await session.call_tool("get_documents", calls[0])
            kinds = [b.type for b in first.content]
            assert kinds[:3] == ["text", "image", "text"], kinds   # image sits inline
            assert "nope" in first.content[-1].text                # unknown id reported

            second = await session.call_tool("get_documents", calls[1])
            text = "".join(b.text for b in second.content)
            assert "[code block omitted: python" in text           # per-doc override
            assert "[image omitted:" in text                       # images off by default

    print("\nsmoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
