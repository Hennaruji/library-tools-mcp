"""End-to-end test: starts the server over stdio against the sample library and
exercises every selector type through a real MCP client session.

    python tests/smoke.py          # add -v to print every response
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

ROOT = Path(__file__).resolve().parent.parent
VERBOSE = "-v" in sys.argv
passed = 0


def check(cond: bool, label: str) -> None:
    global passed
    if not cond:
        raise AssertionError(label)
    passed += 1
    print(f"  ok  {label}")


def text_of(result) -> str:
    return "".join(b.text for b in result.content if b.type == "text")


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "library_server.py")],
        env={**get_default_environment(), "LIBRARY_ROOT": str(ROOT / "library")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(tool: str, args: dict):
                r = await s.call_tool(tool, args)
                if VERBOSE:
                    print(f"\n>>> {tool} {json.dumps(args)}")
                    for b in r.content:
                        print(b.text if b.type == "text" else f"<{b.type} {b.mime_type}>")
                return r

            tools = {t.name for t in (await s.list_tools()).tools}
            check(tools == {"list_documents", "get_documents"}, "both tools exposed")

            print("list_documents")
            t = text_of(await call("list_documents", {}))
            check(all(f"- {d} " in t for d in ("auth", "caching", "migrations", "getting-started")),
                  "whole library lists every doc")
            check("L18–38" not in t, "line bounds hidden by default")
            t = text_of(await call("list_documents", {"root": "backend/storage", "line_bounds": True}))
            check("caching" in t and "auth" not in t, "root narrows the tree")
            check("§ fan-out-deletes" in t and "L21–34" in t, "nested sections with line bounds")
            t = text_of(await call("list_documents", {"root": "backend", "section_depth": 1}))
            check("§ refresh-tokens" in t and "§ rotation" not in t, "section_depth limits heading levels")
            t = text_of(await call("list_documents", {"root": "nowhere"}))
            check("No documents under" in t and "backend" in t, "unknown root suggests top-level entries")

            print("get_documents")
            r = await call("get_documents", {"select": ["auth"]})
            t = text_of(r)
            check("=== auth (backend/auth.md) · full document · L1–50 ===" in t, "doc id string selects whole doc")
            check("[image omitted: img-login-sequence" in t, "images off by default")
            check("def refresh(" in t, "code on by default")

            r = await call("get_documents", {"select": [{"root": "backend/storage"}]})
            t = text_of(r)
            check(t.index("=== caching") < t.index("=== migrations") and "=== auth" not in t,
                  "root selects every doc under it, in path order")

            r = await call("get_documents", {"select": [{"doc": "auth", "sections": ["refresh-tokens"]}]})
            t = text_of(r)
            check("L14–42" in t and "### Expiry" in t and "## SSO quirks" not in t,
                  "section includes its subsections and nothing else")

            r = await call("get_documents", {"select": [
                {"doc": "migrations", "sections": ["dropping-a-column/examples"]},
                {"doc": "migrations", "sections": ["examples"]}]})
            t = text_of(r)
            check("DROP COLUMN" in t and "ADD COLUMN" not in t, "full section path resolves")
            check("ambiguous" in t and "adding-a-column/examples" in t, "ambiguous leaf slug lists candidates")

            r = await call("get_documents", {"select": [{"doc": "auth", "lines": [28, 32]}]})
            t = text_of(r)
            check(t.count("```") == 2 and "```python\n    if record.revoked" in t,
                  "line range cut inside a code block gets balanced fences")

            r = await call("get_documents", {"select": [
                "auth", {"doc": "auth", "sections": ["rotation"]}, {"doc": "auth", "lines": [40, 60]}]})
            t = text_of(r)
            check(t.count("def refresh(") == 1, "overlapping selectors never repeat content")
            check("already included earlier" in t, "skipped overlap is reported")

            r = await call("get_documents", {"select": [
                "caching", {"doc": "getting-started", "include_images": True, "include_code": False}]})
            kinds = [b.type for b in r.content]
            t = text_of(r)
            check(kinds.count("image") == 1, "per-selector include_images override")
            check("[image omitted: img-fanout" in t, "call-level default still applies elsewhere")
            check("[code block omitted: bash, 3 lines" in t and "def invalidate_org" in t,
                  "per-selector include_code override")
            i = kinds.index("image")
            check("[image img-service-map:" in r.content[i - 1].text
                  and "## Repository layout" in r.content[i + 1].text,
                  "image sits inline at its stub position")

            r = await call("get_documents", {"select": [{"root": "."}], "include_images": True, "max_tokens": 2000})
            t = text_of(r)
            check("Package not sent" in t and len(r.content) == 1, "max_tokens refuses an oversized package")
            check("- auth · full document" in t, "refusal itemizes the estimate")

            r = await call("get_documents", {"select": ["migrations", "nope",
                                                       {"doc": "auth", "sections": ["nonexistent"]}]})
            t = text_of(r)
            check("[image img-missing: not in image index]" in t, "stub without an index entry is flagged")
            check("unknown document id 'nope'" in t and "no section 'nonexistent'" in t,
                  "bad ids are reported without failing the call")

            r = await call("get_documents", {"select": [{"doc": "auth", "root": "backend"}]})
            check(r.is_error, "invalid selector (doc and root) is rejected")

    print(f"\nall {passed} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
