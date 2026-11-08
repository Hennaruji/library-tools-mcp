# library-tools-mcp

An MCP server that lets an agent browse an indexed markdown library and pull
exactly the context it needs (whole docs, directory roots, sections, line
ranges, or any mix) in one call. Text, code blocks and images come back
interleaved in document order.

```
library_server.py    the server
library/             sample library (proof-of-concept data and conventions)
tests/smoke.py       end-to-end test over stdio, one check per behaviour
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

**Document index** (`index.json`): one entry per doc. Summaries and token
counts are cached here; `path` is relative to the library root.

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
(re-parsed only when a file changes):

- A section id is its heading slug, nested with `/`: `refresh-tokens/rotation`.
  `list_documents` always shows full ids. A unique leaf (`rotation`) is also
  accepted as a convenience; an ambiguous one returns the candidates.
- A doc with exactly one `#` heading treats it as the title. Its sections start
  at `##`, and the text under the title is the `_intro` section.
- Text before the first heading is `_preamble`.
- Line numbers are the file's own 1-based lines, matching Read and grep.

**Code** means fenced blocks (```` ``` ```` or `~~~`). Headings inside code
blocks are ignored.

## Tools

### `list_documents(root=".", line_bounds=false, section_depth=null)`

A tree from `root` (a directory relative to the library root), with doc
summaries, token estimates and section outlines:

```
backend/storage/  (2 docs, ~351 tokens)

- caching  [caching.md]  ~226 tokens  — Redis key conventions, invalidation ...
    sections, 38 lines:
      § _intro  "Caching"  ~34  L1–5
      § key-conventions  "Key conventions"  ~43  L6–16
      § invalidation  "Invalidation"  ~120  L17–34
        § invalidation/fan-out-deletes  "Fan-out deletes"  ~98  L21–34
      § ttl-rules  "TTL rules"  ~28  L35–38
```

Each section shows its full id (copy it verbatim into a `sections` selector) and
its heading text. `line_bounds` adds the `L…` ranges; `section_depth` caps heading levels (0 = none).

### `get_documents(select, include_images=false, include_code=true, max_tokens=50000)`

`select` is a list. Each item is a doc id string or a selector object:

```jsonc
{
  "select": [
    "getting-started",                                       // whole doc
    { "root": "backend/storage" },                           // every doc under a dir
    { "doc": "auth", "sections": ["login-flow", "rotation"] },
    { "doc": "auth", "lines": [43, 50] },
    { "doc": "caching", "include_images": true }             // per-selector override
  ],
  "include_images": false,
  "include_code": true
}
```

- **Order:** output follows `select`; roots expand in path order.
- **No repeats:** content already in the package is skipped and noted.
- **Headers:** each chunk starts with `=== auth (backend/auth.md) · section rotation · L18–38 ===`.
- **Placeholders:** omitted content becomes a one-line note, e.g.
  `[code block omitted: python, 11 lines, L22–34]` or `[image omitted: img-fanout — caption]`.
- **Code cuts:** a line range that starts or ends inside a code block gets its fences re-balanced.
- **Budget:** if the estimate exceeds `max_tokens`, nothing is sent except an
  itemized estimate so the agent can narrow the request. `null` = no limit.
- **Errors:** unknown docs, sections or images are reported in a `Notes:` block
  and don't fail the call. Only a malformed selector (e.g. both `doc` and `root`) is rejected.

## Settings

| env var | default | meaning |
|---|---|---|
| `LIBRARY_ROOT` | `./library` beside the script | library root |
| `LIBRARY_DOC_INDEX` | `index.json` | doc index, relative to root |
| `LIBRARY_IMAGE_INDEX` | `images/index.json` | image index, relative to root |
| `LIBRARY_MAX_TOKENS` | `50000` | default `max_tokens` |
| `LIBRARY_IMAGE_TOKENS` | `1500` | per-image estimate used for budgeting |
| `LIBRARY_MAX_IMAGE_BYTES` | 5 MB | larger images become placeholders |

Token estimates are ~4 characters per token: good for budgeting, not exact.

## Suggested CLAUDE.md line

> Project knowledge lives in the `library` MCP tools. Browse with
> `list_documents`, then fetch everything relevant in ONE `get_documents` call,
> mixing docs, roots, sections and line ranges as needed. Request images only
> where the visual detail matters.
