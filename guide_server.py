"""
guide_server.py — an MCP server that serves an indexed set of markdown docs,
returning text, code blocks and images interleaved in document order.

Layout it expects (configurable with GUIDE_DIR):

    guide/
      auth.md            <- each doc has YAML front matter with id/title/description
      caching.md
      img/flow.png       <- images referenced relatively from the docs

    ---
    id: auth
    title: Authentication
    description: Login flow, session tokens, SSO quirks
    ---
    # Authentication
    ...

Tools exposed:
    list_documents()                      -> the menu (id, title, description, counts)
    get_documents(docs, include_images,   -> one call, many docs, mixed text+image content
                  include_code)

Run:  python guide_server.py            (stdio transport, for Claude Code)
Requires: pip install "mcp>=2" pyyaml
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

GUIDE_DIR = Path(os.environ.get("GUIDE_DIR", Path(__file__).parent / "guide")).resolve()
MAX_IMAGE_BYTES = int(os.environ.get("GUIDE_MAX_IMAGE_BYTES", 5 * 1024 * 1024))

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


# --------------------------------------------------------------------------
# Parsing: a doc becomes an ordered list of segments
# --------------------------------------------------------------------------

@dataclass
class Segment:
    kind: str                 # "text" | "code" | "image"
    text: str = ""            # text body, code body, or image alt text
    lang: str = ""            # code language
    path: Path | None = None  # resolved image path (None for remote/missing)
    src: str = ""             # image reference as written


@dataclass
class Doc:
    id: str
    title: str
    description: str
    path: Path
    segments: list[Segment] = field(default_factory=list)

    @property
    def n_images(self) -> int:
        return sum(s.kind == "image" for s in self.segments)

    @property
    def n_code(self) -> int:
        return sum(s.kind == "code" for s in self.segments)


def _split_images(text: str, base: Path) -> list[Segment]:
    """Split a run of prose at each ![alt](src) reference."""
    out: list[Segment] = []
    pos = 0
    for m in IMAGE_REF.finditer(text):
        if m.start() > pos:
            out.append(Segment("text", text[pos:m.start()]))
        alt, src = m.group(1), m.group(2)
        local = None
        if not re.match(r"^[a-z]+://", src):
            candidate = (base / src).resolve()
            # stay inside the guide directory
            if candidate.is_file() and GUIDE_DIR in candidate.parents:
                local = candidate
        out.append(Segment("image", alt, path=local, src=src))
        pos = m.end()
    if pos < len(text):
        out.append(Segment("text", text[pos:]))
    return out


def parse_doc(path: Path) -> Doc | None:
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    m = FRONT_MATTER.match(raw)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        raw = raw[m.end():]

    doc = Doc(
        id=str(meta.get("id") or path.stem),
        title=str(meta.get("title") or path.stem),
        description=str(meta.get("description") or ""),
        path=path,
    )

    prose: list[str] = []

    def flush_prose():
        if prose:
            doc.segments.extend(_split_images("".join(prose), path.parent))
            prose.clear()

    lines = raw.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        fm = FENCE_OPEN.match(lines[i].rstrip("\n"))
        if fm:
            fence, lang = fm.group(2), fm.group(3).strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence (or run off the end if unclosed)
            flush_prose()
            doc.segments.append(Segment("code", "".join(body), lang=lang))
            continue
        prose.append(lines[i])
        i += 1
    flush_prose()
    return doc


def load_index() -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    for path in sorted(GUIDE_DIR.rglob("*.md")):
        doc = parse_doc(path)
        if doc is None:
            continue
        if doc.id in docs:
            raise ValueError(f"duplicate doc id {doc.id!r}: {docs[doc.id].path} and {path}")
        docs[doc.id] = doc
    return docs


# --------------------------------------------------------------------------
# Rendering: segments -> MCP content blocks, honouring include flags
# --------------------------------------------------------------------------

def _tidy(text: str) -> str:
    """Trim trailing whitespace and collapse runs of blank lines."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def render(doc: Doc, include_images: bool, include_code: bool) -> list[TextContent | ImageContent]:
    blocks: list[TextContent | ImageContent] = []
    buf: list[str] = [f"\n=== doc: {doc.id} — {doc.title} ===\n\n"]

    def flush():
        text = _tidy("".join(buf))
        if text.strip():
            blocks.append(TextContent(type="text", text=text))
        buf.clear()

    for seg in doc.segments:
        if seg.kind == "text":
            buf.append(seg.text)

        elif seg.kind == "code":
            if include_code:
                buf.append(f"```{seg.lang}\n{seg.text}```\n")
            else:
                n = seg.text.count("\n")
                buf.append(f"[code block omitted: {seg.lang or 'plain'}, {n} lines]\n")

        elif seg.kind == "image":
            label = seg.text or seg.src
            if not include_images:
                buf.append(f"[image omitted: {label}]")
            elif seg.path is None:
                buf.append(f"[image unavailable: {label} ({seg.src})]")
            elif seg.path.stat().st_size > MAX_IMAGE_BYTES:
                buf.append(f"[image too large to inline: {label}]")
            elif seg.path.suffix.lower() not in MIME_TYPES:
                buf.append(f"[unsupported image type: {label} ({seg.path.suffix})]")
            else:
                # Caption stays in the text just before the image, so the model
                # knows what it is looking at and where it sits in the doc.
                buf.append(f"[image: {label}]\n")
                flush()
                blocks.append(ImageContent(
                    type="image",
                    data=base64.b64encode(seg.path.read_bytes()).decode(),
                    mime_type=MIME_TYPES[seg.path.suffix.lower()],
                ))

    buf.append(f"\n=== end doc: {doc.id} ===\n")
    flush()
    return blocks


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

mcp = MCPServer(
    "guide",
    instructions=(
        "Serves the project guide. Call list_documents to see what exists, then "
        "fetch everything you need in ONE get_documents call. Leave images off "
        "unless the visual detail matters; they cost far more tokens than text."
    ),
)


class DocRequest(BaseModel):
    id: str = Field(description="Document id from list_documents")
    include_images: bool | None = Field(
        default=None, description="Override the call-level include_images for this doc")
    include_code: bool | None = Field(
        default=None, description="Override the call-level include_code for this doc")


@mcp.tool(structured_output=False)
def list_documents() -> str:
    """List every document in the guide: id, title, description, and how many
    images and code blocks it contains."""
    docs = load_index()  # re-scan each call so edits show up without a restart
    lines = [f"{len(docs)} documents in {GUIDE_DIR.name}/", ""]
    for d in docs.values():
        extras = ", ".join(x for x in (
            f"{d.n_images} images" if d.n_images else "",
            f"{d.n_code} code blocks" if d.n_code else "",
        ) if x)
        lines.append(f"- `{d.id}` — {d.title}: {d.description}" + (f" ({extras})" if extras else ""))
    return "\n".join(lines)


@mcp.tool(structured_output=False)
def get_documents(
    docs: list[str | DocRequest],
    include_images: bool = False,
    include_code: bool = True,
) -> list[TextContent | ImageContent]:
    """Fetch several documents in one call, in the order given, with text,
    code blocks and images interleaved exactly as they appear in each doc.

    docs: ids as plain strings, or objects {id, include_images?, include_code?}
          to override the call-level settings for that one doc.
    include_images / include_code: defaults for every doc in this call.
    Omitted images and code are replaced by a short placeholder line, so the
    reader still knows something was there.
    """
    index = load_index()
    out: list[TextContent | ImageContent] = []
    missing: list[str] = []
    seen: set[str] = set()

    for item in docs:
        req = DocRequest(id=item) if isinstance(item, str) else item
        if req.id in seen:
            continue
        seen.add(req.id)
        doc = index.get(req.id)
        if doc is None:
            missing.append(req.id)
            continue
        out.extend(render(
            doc,
            include_images=include_images if req.include_images is None else req.include_images,
            include_code=include_code if req.include_code is None else req.include_code,
        ))

    if missing:
        out.append(TextContent(
            type="text",
            text=f"\nUnknown document ids (see list_documents): {', '.join(missing)}\n",
        ))
    return out or [TextContent(type="text", text="No documents requested.")]


if __name__ == "__main__":
    mcp.run("stdio")
