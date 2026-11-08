# library-tools-mcp

An MCP server that lets an agent browse an indexed markdown library and pull
exactly the context it needs (whole docs, directory roots, sections, line
ranges, or any mix) in one call. Markdown and images come back interleaved
in document order.

```
library_server.py    the server
library/             sample library (proof-of-concept data and conventions)
tests/smoke.py       end-to-end tests over stdio, one check per behaviour
tests/fixtures/edge  markdown parsing edge cases and an indexed-but-missing file
```

## Quick start

```bash
pip install -r requirements.txt
python tests/smoke.py        # -v prints every request and response
```

Register it in a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "library": {
      "command": "python",
      "args": ["path/to/library-tools-mcp/library_server.py"],
      "env": { "LIBRARY_ROOT": "path/to/your/library" }
    }
  }
}
```

Requires the v2 Python MCP SDK (`mcp>=2`, where `FastMCP` became `MCPServer`).

## Library conventions

These are proof-of-concept conventions. Swap in your real formats by changing
`load_doc_index`, `load_image_index` and `IMAGE_STUB`; nothing else depends on them.

```
library/
  index.json             document index
  images/index.json      image index
  images/*.png
  guides/getting-started.md
  backend/auth.md
  backend/storage/caching.md
  backend/storage/migrations.md
```

**Document index** (`index.json`): one entry per doc; `path` is relative to the
library root. Token counts are taken from here as-is. The server does not
estimate tokens itself.

```json
{"documents": [
  {"id": "auth", "path": "backend/auth.md", "title": "Authentication",
   "summary": "Gateway token validation, login flow, ...", "approx_tokens": 353}
]}
```

**Image index** (`images/index.json`): `file` is relative to the index's folder.

```json
{"images": [
  {"id": "img-login-sequence", "file": "login-sequence.png",
   "caption": "Login sequence: client, gateway, token service"}
]}
```

**Image stubs** in markdown: `![[image:img-login-sequence]]`

**Sections** are not cached. They are parsed from `#` headings on request
(re-parsed only when a file changes), following CommonMark for headings and
fenced code blocks:

- A section id is its heading slug, nested with `/`: `refresh-tokens/rotation`.
  `list_documents` always shows full ids. A unique leaf (`rotation`) is also
  accepted as a convenience; an ambiguous one returns the candidates.
- If a doc's first heading is its only `#` heading, it is the title: sections
  start at `##`, and the text under the title is the `_intro` section.
- Text before the first heading is `_preamble`.
- Line numbers are the file's own 1-based lines (split on `\n` only), matching Read and grep.

**Code** means fenced blocks inside the markdown (```` ``` ```` or `~~~`).

## Tools

### `list_documents(root=".", line_bounds=false, section_depth=null)`

A tree from `root` (a directory relative to the library root). Each doc shows
its id, token count and summary from the index, then its section outline:

```
backend/storage/  (2 docs, ~351 tokens)

- caching  [caching.md]  ~226 tokens  — Redis key conventions, invalidation ...
    sections, 38 lines:
      § _intro  "Caching"  L1–5
      § key-conventions  "Key conventions"  L6–16
      § invalidation  "Invalidation"  L17–34
        § invalidation/fan-out-deletes  "Fan-out deletes"  L21–34
      § ttl-rules  "TTL rules"  L35–38
```

Each section shows its full id (copy it verbatim into a `sections` selector)
and its heading text. `line_bounds` adds the `L…` ranges; `section_depth`
caps heading levels (0 = none).

### `get_documents(select, include_images=false, include_code=true)`

`select` is a list. Each item is a doc id string or a selector object:

```jsonc
{
  "select": [
    "getting-started",                                       // whole doc
    { "root": "backend/storage" },                           // every doc under a dir
    { "doc": "auth", "sections": ["login-flow", "refresh-tokens/rotation"] },
    { "doc": "auth", "lines": [43, 50] },
    { "doc": "caching", "include_images": true }             // per-selector override
  ],
  "include_images": false,
  "include_code": true
}
```

- **Order:** output follows `select`, and sections follow the order they were
  listed; roots expand in path order.
- **Headers:** each chunk starts with
  `=== auth (backend/auth.md) · section refresh-tokens/rotation · L18–38 ===`.
- **Content is verbatim:** document lines are returned exactly as written.
  Only headers, placeholders and fence repairs are added.
- **Placeholders:** omitted content becomes a one-line note, e.g.
  `[code block omitted: python, 11 lines, L22–34]` or `[image omitted: img-fanout — caption]`.
- **Code cuts:** a line range that starts or ends inside a code block gets its fences re-balanced.
- **Overlap within one call:** if selectors overlap, the overlapping lines are
  returned once, and an overlap notice at the end says which selectors overlapped
  and where the lines appear. If the earlier selector used different image or
  code settings, the notice says how the lines were returned, so the agent can
  request them again separately. Separate calls are independent: nothing is
  remembered between them.
- **Errors:** unknown docs, sections or images and missing files are reported
  in a `Notes:` block and don't fail the call. Malformed selectors are rejected:
  both `doc` and `root`, unknown fields (e.g. `section` for `sections`), or an
  empty `sections` list.

## Settings

| env var | default | meaning |
|---|---|---|
| `LIBRARY_ROOT` | `./library` beside the script | library root |
| `LIBRARY_DOC_INDEX` | `index.json` | doc index, relative to root |
| `LIBRARY_IMAGE_INDEX` | `images/index.json` | image index, relative to root |
| `LIBRARY_MAX_IMAGE_BYTES` | 5 MB | larger images become placeholders |

## Suggested CLAUDE.md line

> Project knowledge lives in the `library` MCP tools. Browse with
> `list_documents`, then fetch everything relevant in ONE `get_documents` call,
> mixing docs, roots, sections and line ranges as needed. Request images only
> where the visual detail matters.
