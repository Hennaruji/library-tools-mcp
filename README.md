# library-tools-mcp

An MCP server that serves an indexed folder of markdown docs. One call returns
many docs, with text, code blocks and images interleaved in document order.

```
guide_server.py      the server
examples/guide/      sample docs (front matter, an image, code blocks)
tests/smoke.py       end-to-end test over stdio
```

## Setup

```bash
pip install -r requirements.txt
python tests/smoke.py          # runs the server against examples/guide
```

To use it from another project, point `GUIDE_DIR` at that project's docs and
register the server in the project's `.mcp.json`, so anyone who clones it gets it:

```json
{
  "mcpServers": {
    "guide": {
      "command": "python",
      "args": ["path/to/library-tools-mcp/guide_server.py"],
      "env": { "GUIDE_DIR": "docs/guide" }
    }
  }
}
```

Requires version 2 of the Python MCP SDK (`mcp>=2`). Version 2 renamed `FastMCP`
to `MCPServer`, so examples written for 1.x won't match this code exactly.

## Docs

Each `.md` file starts with front matter. `id` defaults to the filename and
`description` is what the agent uses to choose docs, so make it say what
questions the doc answers.

```markdown
---
id: auth
title: Authentication
description: Login flow, session tokens, SSO quirks
---
```

Images are standard `![alt](relative/path.png)` references. The alt text becomes
the caption the model sees, and the placeholder when images are left out.

## Tools

`list_documents()` returns the menu, with image and code-block counts per doc.

`get_documents(docs, include_images=False, include_code=True)`

```jsonc
{
  "docs": [
    "caching",                                   // uses the call-level settings
    { "id": "auth", "include_images": true },    // per-doc override
    { "id": "deploy", "include_code": false }
  ],
  "include_images": false,
  "include_code": true
}
```

Omitted code and images become one-line placeholders like
`[code block omitted: python, 40 lines]`, so the model knows they exist and can
fetch them on request. Images are off by default because they cost far more
tokens than text.

## Suggested CLAUDE.md line

> The project guide is available through the `guide` MCP tools. Check
> `list_documents`, then fetch everything relevant in ONE `get_documents` call.
> Request images only for docs where the visual detail matters.

## Settings

| env var | default | meaning |
|---|---|---|
| `GUIDE_DIR` | `./guide` beside the script | docs root (images must live inside it) |
| `GUIDE_MAX_IMAGE_BYTES` | 5 MB | larger images are replaced by a placeholder |
