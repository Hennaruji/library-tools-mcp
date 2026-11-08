"""End-to-end tests: start the server over stdio and exercise it through a real
MCP client session, once against the sample library and once against
tests/fixtures/edge (parsing edge cases and a missing file).

    python tests/smoke.py          # add -v to print every response
"""

import asyncio
import json
import re
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


async def session(library: Path, body, **env: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "library_server.py")],
        env={**get_default_environment(), "LIBRARY_ROOT": str(library), **env},
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

            await body(s, call)


async def sample_library(s, call) -> None:
    tools = {t.name: t for t in (await s.list_tools()).tools}
    check(set(tools) == {"list_documents", "get_documents"}, "both tools exposed")
    schema = json.dumps(tools["get_documents"].input_schema)
    check("max_tokens" not in schema, "no token budget parameter")
    check("$ref" not in schema and "$defs" not in schema, "selector schema is inlined (no $ref)")
    check(all(tools[n].input_schema["properties"][p].get("description")
              for n, ps in [("get_documents", ["select", "include_images", "include_code"]),
                            ("list_documents", ["root", "line_bounds", "section_depth"])] for p in ps),
          "every top-level parameter has a description")

    print("list_documents")
    t = text_of(await call("list_documents", {}))
    check(all(f"- {d} " in t for d in ("auth", "caching", "migrations", "getting-started")),
          "whole library lists every doc")
    check("- auth  [auth.md]  ~353 tokens" in t, "doc token counts come from the index")
    check(not re.search(r"§ \S+  \"[^\"]*\"  ~", t), "no per-section token estimates")
    check("L18–38" not in t, "line bounds hidden by default")
    t = text_of(await call("list_documents", {"root": "backend/storage", "line_bounds": True}))
    check("caching" in t and "auth" not in t, "root narrows the tree")
    check('§ invalidation/fan-out-deletes  "Fan-out deletes"  L21–34' in t,
          "sections listed with full id, heading text and line bounds")
    check("§ adding-a-column/examples" in t and "§ dropping-a-column/examples" in t,
          "duplicate heading names get distinct ids")
    t = text_of(await call("list_documents", {"root": "backend", "section_depth": 1}))
    check("§ refresh-tokens " in t and "refresh-tokens/rotation" not in t,
          "section_depth limits heading levels")
    t = text_of(await call("list_documents", {"root": "guides", "line_bounds": True}))
    check('§ _preamble  "(text before the first heading)"  L1–3' in t, "text before the first heading is _preamble")
    check('§ getting-started  "Getting started"' in t and '§ next-steps  "Next steps"' in t,
          "a doc with several H1s keeps them as top-level sections")
    t = text_of(await call("get_documents", {"select": [{"doc": "getting-started", "sections": ["_preamble"]}]}))
    check("section _preamble · L1–3" in t and "first stop for new contributors" in t,
          "_preamble can be selected")
    t = text_of(await call("list_documents", {"root": "nowhere"}))
    check("No documents under" in t and "backend" in t, "unknown root suggests top-level entries")

    print("get_documents: selectors")
    t = text_of(await call("get_documents", {"select": ["auth"]}))
    check("=== auth (backend/auth.md) · full document · L1–50 ===" in t, "doc id string selects whole doc")
    check("[image omitted: img-login-sequence" in t, "images off by default")
    check("def refresh(" in t, "code on by default")

    t = text_of(await call("get_documents", {"select": [{"root": "backend/storage"}]}))
    check(t.index("=== caching") < t.index("=== migrations") and "=== auth" not in t,
          "root selects every doc under it, in path order")

    t = text_of(await call("get_documents", {"select": [{"doc": "auth", "sections": ["refresh-tokens"]}]}))
    check("section refresh-tokens · L14–42" in t and "### Expiry" in t and "## SSO quirks" not in t,
          "section includes its subsections and nothing else")

    t = text_of(await call("get_documents", {"select": [
        {"doc": "caching", "sections": ["ttl-rules", "key-conventions"]}]}))
    check(t.index("section ttl-rules") < t.index("section key-conventions"),
          "sections come back in the order requested, each labelled")

    t = text_of(await call("get_documents", {"select": [
        {"doc": "migrations", "sections": ["dropping-a-column/examples"]},
        {"doc": "migrations", "sections": ["examples"]}]}))
    check("DROP COLUMN" in t and "ADD COLUMN" not in t, "full section path resolves")
    check("ambiguous" in t and "adding-a-column/examples" in t, "ambiguous leaf slug lists candidates")

    t = text_of(await call("get_documents", {"select": [{"doc": "auth", "lines": [28, 32]}]}))
    check(t.count("```") == 2 and "```python\n    if record.revoked" in t,
          "line range cut inside a code block gets balanced fences")

    print("get_documents: overlap")
    r = await call("get_documents", {"select": [
        "auth", {"doc": "auth", "sections": ["refresh-tokens/rotation"]}]})
    t = text_of(r)
    check(t.count("def refresh(") == 1, "overlapping lines are returned once")
    check("overlap notice" in r.content[0].text, "summary line flags the overlap notices")
    check("selector 2 (auth section refresh-tokens/rotation) overlaps selector 1 at L18–38" in t,
          "overlap notice names both selectors and the lines")
    check("returned with" not in t, "no settings warning when settings match")

    t = text_of(await call("get_documents", {"select": [
        {"doc": "auth", "include_code": False},
        {"doc": "auth", "sections": ["refresh-tokens/rotation"], "include_code": True}]}))
    check("def refresh(" not in t and "code omitted" in t and "separate call" in t,
          "overlap with different settings says how the lines were returned")

    t = text_of(await call("get_documents", {"select": [
        {"doc": "auth", "sections": ["refresh-tokens/rotation"], "include_code": True}]}))
    check("def refresh(" in t and "overlap" not in t, "separate calls are independent")

    t = text_of(await call("get_documents", {"select": [
        {"doc": "caching", "sections": ["invalidation/fan-out-deletes", "invalidation"]}]}))
    check("section invalidation (part) · L17–20" in t and "same selector" in t,
          "overlap inside one selector is labelled and noticed")

    print("get_documents: images and code")
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
    t = text_of(await call("get_documents", {"select": ["migrations"]}))
    check("[image img-missing: not in image index]" in t, "stub without an index entry is flagged")

    print("get_documents: bad requests")
    t = text_of(await call("get_documents", {"select": [
        "nope", {"doc": "auth", "sections": ["nonexistent"]}]}))
    check("unknown document id 'nope'" in t and "no section 'nonexistent'" in t,
          "bad ids are reported without failing the call")
    t = text_of(await call("get_documents", {"select": ['{"doc": "auth"}']}))
    check("JSON string" in t, "selector sent as a JSON string gets a hint")
    for bad, label in [
        ({"doc": "auth", "root": "backend"}, "doc and root together are rejected"),
        ({"doc": "auth", "section": ["rotation"]}, "unknown (typo) selector field is rejected"),
        ({"doc": "auth", "sections": []}, "empty sections list is rejected"),
    ]:
        r = await call("get_documents", {"select": [bad]})
        check(r.is_error, label)


async def edge_library(s, call) -> None:
    print("edge cases")
    t = text_of(await call("list_documents", {"line_bounds": True}))
    check('§ early  "Early"  L1–4' in t and '§ title  "Title"  L5–' in t,
          "H1 that is not the first heading is a normal section; no overlap")
    check('"C#"' in t and '"C# and F#"' in t, "trailing # in a heading title is kept")
    check("§ title/after" in t, "inline ```bash``` does not open a fence and hide later headings")
    check("§ title/indented-close" in t and "§ title/tilde" in t,
          "~~~ and indented ``` fences follow CommonMark")
    check("(file not found at missing.md)" in t, "listing marks an indexed doc with no file")
    check("(linked-out.md resolves outside the library; refused)" in t,
          "listing refuses a doc that escapes through a symlink")
    check("- alias " in t and "§ title/after" in t.split("- alias ")[1],
          "a symlink that stays inside the library is allowed")

    t = text_of(await call("get_documents", {"select": [{"doc": "parsing", "sections": ["title/code"]}]}))
    check("    pass\n\n\ndef b():" in t, "blank lines inside code blocks are preserved")
    check("hard break  \nnext line" in t, "trailing spaces (markdown hard breaks) are preserved")

    t = text_of(await call("get_documents", {"select": [{"doc": "parsing", "sections": ["title/tilde"]}]}))
    check("``` this does not close a tilde fence\n~~~" in t, "``` inside a ~~~ block stays code")

    t = text_of(await call("get_documents", {"select": [{"doc": "lines", "lines": [5, 5]}]}))
    check("L5–5 ===\nTARGET" in t, "line numbers match grep (form feed is not a line break)")

    r = await call("get_documents", {"select": [{"doc": "gone", "sections": ["x"]}, {"root": "."}]})
    t = text_of(r)
    check(not r.is_error and "gone: file not found at missing.md" in t and "=== parsing" in t,
          "missing file is a note, not a failed call")

    print("edge cases: paths")
    t = text_of(await call("get_documents", {"select": ["linked-out"]}))
    check("SECRET" not in t and "resolves outside the library; refused" in t,
          "doc escaping through a symlink is refused")
    r = await call("get_documents", {"select": [{"root": "../"}]})
    check("may not contain '..'" in text_of(r), "root may not climb out of the library")

    print("edge cases: images")
    r = await call("get_documents", {"select": ["images"], "include_images": True})
    t = text_of(r)
    check([b.type for b in r.content].count("image") == 1, "image repeated within one doc is sent once")
    check("[image img-a: same image as shown earlier in this document]" in t,
          "repeat within a doc points back to the first")
    check("img-escape unavailable (file resolves outside the images folder; refused)" in t,
          "image escaping through a symlink is refused")
    r = await call("get_documents", {"select": ["images", "images2"], "include_images": True})
    check([b.type for b in r.content].count("image") == 2, "same image in two docs is sent for each")

    print("edge cases: ranges and fences")
    t = text_of(await call("get_documents", {"select": [{"doc": "lines", "lines": [3, 99]},
                                                     {"doc": "lines", "lines": [50, 60]}]}))
    check("clamped to L3–5" in t and "TARGET" in t, "range past the end of the file is clamped")
    check("L50–60 is past the end of the file" in t, "range starting past the end is reported")
    t = text_of(await call("get_documents", {"select": [{"doc": "parsing", "lines": [23, 25]}],
                                          "include_code": False}))
    check("[code block omitted: python, 6 lines, L19–26]" in t and "def b" not in t,
          "include_code=false on a range that only partly covers a code block")
    t = text_of(await call("list_documents", {"root": "unclosed.md"}))
    check("§ before" in t and "not-a-heading" not in t, "headings inside an unclosed code block are ignored")
    t = text_of(await call("get_documents", {"select": ["unclosed"]}))
    check(t.rstrip().endswith("y = 2\n```"), "unclosed code block is closed in the output")


async def bad_indexes() -> None:
    print("index validation")
    fixtures = ROOT / "tests" / "fixtures"

    async def bad_index(s, call):
        r = await call("list_documents", {})
        t = text_of(r)
        check(r.is_error and "index.json has 6 problems" in t, "malformed index fails with a problem count")
        for expected in ["documents[1]: 'id' is required",
                         "documents[2] (id 'abs'): 'path' must be relative, not absolute",
                         "documents[3] (id 'dots'): 'path' may not contain '..'",
                         "documents[4] (id 'tok'): 'approx_tokens' must be a non-negative integer",
                         "documents[5] (id 'ok'): duplicate id (also used by ok.md)",
                         "documents[6]: expected an object"]:
            check(expected in t, f"names the bad entry: {expected.split(':')[0]}")

    async def bad_json(s, call):
        r = await call("list_documents", {})
        check(r.is_error and "is not valid JSON (line" in text_of(r), "invalid JSON is reported with its position")

    async def outside_index(s, call):
        r = await call("list_documents", {})
        check(r.is_error and "must be inside the library root" in text_of(r),
              "index location outside the library is refused")

    await session(fixtures / "bad-index", bad_index)
    await session(fixtures / "bad-json", bad_json)
    await session(fixtures / "edge", outside_index, LIBRARY_DOC_INDEX="../bad-index/index.json")


async def main() -> None:
    print("sample library")
    await session(ROOT / "library", sample_library)
    await session(ROOT / "tests" / "fixtures" / "edge", edge_library)
    await bad_indexes()
    print(f"\nall {passed} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
