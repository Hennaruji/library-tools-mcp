"""
library_server.py: an MCP server for reading an indexed markdown library.

The agent browses the library as a tree (list_documents), then requests a
"package" of context (get_documents) built from any mix of whole docs,
directory roots, named sections and line ranges. Text, code blocks and
images come back interleaved in document order.

Library conventions (all paths relative to LIBRARY_ROOT):

    index.json            document index (cached summaries and token counts)
        {"documents": [{"id", "path", "title", "summary", "approx_tokens"}]}
    images/index.json     image index; "file" is relative to this folder
        {"images": [{"id", "file", "caption"}]}
    *.md                  docs, anywhere under the root, at their index "path"

    Image stubs in markdown:   ![[image:<image_id>]]

Sections are not cached: they are parsed from ATX headings (#, ##, ...) on
each request. A section id is its heading slug, nested with "/" for
subsections (auth: "refresh-tokens/rotation"). A bare leaf slug ("rotation")
also works when it is unique within the doc. Line numbers are the file's own
1-based line numbers, so they agree with Read and grep.

Run:   python library_server.py           (stdio transport)
Needs: pip install "mcp>=2"
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, model_validator

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", Path(__file__).parent / "library")).resolve()
DOC_INDEX = LIBRARY_ROOT / os.environ.get("LIBRARY_DOC_INDEX", "index.json")
IMAGE_INDEX = LIBRARY_ROOT / os.environ.get("LIBRARY_IMAGE_INDEX", "images/index.json")
DEFAULT_MAX_TOKENS = int(os.environ.get("LIBRARY_MAX_TOKENS", 50_000))
IMAGE_TOKEN_ESTIMATE = int(os.environ.get("LIBRARY_IMAGE_TOKENS", 1_500))
MAX_IMAGE_BYTES = int(os.environ.get("LIBRARY_MAX_IMAGE_BYTES", 5 * 1024 * 1024))

IMAGE_STUB = re.compile(r"!\[\[image:([A-Za-z0-9_.\-]+)\]\]")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CODE_PLACEHOLDER_TOKENS = 12

MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token)."""
    return round(len(text) / 4)


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------

@dataclass
class DocEntry:
    id: str
    path: str            # posix path relative to LIBRARY_ROOT
    title: str
    summary: str
    approx_tokens: int | None

    @property
    def file(self) -> Path:
        return (LIBRARY_ROOT / self.path).resolve()


@dataclass
class ImageEntry:
    id: str
    file: Path
    caption: str


def load_doc_index() -> dict[str, DocEntry]:
    data = json.loads(DOC_INDEX.read_text(encoding="utf-8"))
    out: dict[str, DocEntry] = {}
    for d in data["documents"]:
        entry = DocEntry(
            id=d["id"],
            path=PurePosixPath(d["path"]).as_posix(),
            title=d.get("title") or d["id"],
            summary=d.get("summary", ""),
            approx_tokens=d.get("approx_tokens"),
        )
        if entry.id in out:
            raise ValueError(f"duplicate document id in index: {entry.id!r}")
        out[entry.id] = entry
    return dict(sorted(out.items(), key=lambda kv: kv[1].path))


def load_image_index() -> dict[str, ImageEntry]:
    if not IMAGE_INDEX.is_file():
        return {}
    base = IMAGE_INDEX.parent
    data = json.loads(IMAGE_INDEX.read_text(encoding="utf-8"))
    return {
        i["id"]: ImageEntry(i["id"], (base / i["file"]).resolve(), i.get("caption", ""))
        for i in data["images"]
    }


def normalize_root(root: str) -> str:
    """'./backend/storage/' -> 'backend/storage'; '.' or '' -> '' (whole library)."""
    p = PurePosixPath(root.strip().replace("\\", "/").strip("/") or ".")
    if ".." in p.parts:
        raise ValueError(f"root may not contain '..': {root!r}")
    s = p.as_posix()
    return "" if s == "." else s


def docs_under(index: dict[str, DocEntry], root: str) -> list[DocEntry]:
    r = normalize_root(root)
    return [e for e in index.values()
            if not r or e.path == r or e.path.startswith(r + "/")]


# --------------------------------------------------------------------------
# Parsing (sections and code fences), cached by file mtime
# --------------------------------------------------------------------------

@dataclass
class Section:
    id: str              # full path id, e.g. "refresh-tokens/rotation"
    slug: str            # leaf slug, e.g. "rotation"
    title: str
    level: int           # heading level; 0 for the preamble
    start: int           # 1-based, heading line
    end: int             # inclusive
    tokens: int
    children: list[Section] = field(default_factory=list)


@dataclass
class Fence:
    start: int           # opening fence line
    end: int             # closing fence line (or last line if unclosed)
    marker: str          # ``` or ~~~ (as written)
    lang: str
    closed: bool


@dataclass
class ParsedDoc:
    lines: list[str]
    sections: list[Section]          # flat, document order
    tree: list[Section]              # top-level sections
    fences: list[Fence]
    line_fence: list[int | None]     # per line: index into fences, or None

    @property
    def n(self) -> int:
        return len(self.lines)


_parse_cache: dict[Path, tuple[float, ParsedDoc]] = {}


def slugify(title: str) -> str:
    s = re.sub(r"[`*_~\[\]()]", "", title.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "section"


def parse_doc(path: Path) -> ParsedDoc:
    mtime = path.stat().st_mtime
    cached = _parse_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    lines = path.read_text(encoding="utf-8").splitlines()
    n = len(lines)
    fences: list[Fence] = []
    line_fence: list[int | None] = [None] * n
    headings: list[tuple[int, int, str]] = []        # (line, level, title)
    open_fence: tuple[int, str, str] | None = None   # (line, marker, lang)

    for ln, line in enumerate(lines, 1):
        if open_fence is None:
            m = FENCE.match(line)
            if m:
                open_fence = (ln, m.group(1), m.group(2).strip())
                line_fence[ln - 1] = len(fences)
                continue
            h = HEADING.match(line)
            if h:
                headings.append((ln, len(h.group(1)), h.group(2).strip()))
        else:
            line_fence[ln - 1] = len(fences)
            s, marker = line.strip(), open_fence[1]
            if s and set(s) == {marker[0]} and len(s) >= len(marker):
                fences.append(Fence(open_fence[0], ln, marker, open_fence[2], True))
                open_fence = None
    if open_fence:
        fences.append(Fence(open_fence[0], n, open_fence[1], open_fence[2], False))

    def body(a: int, b: int) -> str:
        return "\n".join(lines[a - 1:b])

    # A doc with exactly one H1 uses it as its title: the H1 is not a level of
    # the section tree, and the text under it (before the first subheading)
    # becomes "_intro". Without this, every id would start with the title slug.
    intro: Section | None = None
    h1s = [h for h in headings if h[1] == 1]
    if len(h1s) == 1:
        title_ln = h1s[0][0]
        headings = [h for h in headings if h[1] != 1]
        nxt = next((h[0] for h in headings if h[0] > title_ln), n + 1)
        intro = Section("_intro", "_intro", h1s[0][2], 0, title_ln, nxt - 1,
                        estimate_tokens(body(title_ln, nxt - 1)))

    tree: list[Section] = []
    flat: list[Section] = []
    stack: list[Section] = []
    for k, (ln, level, title) in enumerate(headings):
        end = next((h[0] - 1 for h in headings[k + 1:] if h[1] <= level), n)
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        siblings = parent.children if parent else tree
        base = slug = slugify(title)
        c = 2
        while any(s.slug == slug for s in siblings):
            slug, c = f"{base}-{c}", c + 1
        sec = Section(f"{parent.id}/{slug}" if parent else slug, slug, title,
                      level, ln, end, estimate_tokens(body(ln, end)))
        siblings.append(sec)
        flat.append(sec)
        stack.append(sec)

    if intro:
        tree.insert(0, intro)
        flat.insert(0, intro)

    first = min([h[0] for h in headings] + ([intro.start] if intro else []) + [n + 1])
    if first > 1 and body(1, first - 1).strip():
        pre = Section("_preamble", "_preamble", "(text before the first heading)", 0,
                      1, first - 1, estimate_tokens(body(1, first - 1)))
        tree.insert(0, pre)
        flat.insert(0, pre)

    doc = ParsedDoc(lines, flat, tree, fences, line_fence)
    _parse_cache[path] = (mtime, doc)
    return doc


def find_section(doc: ParsedDoc, doc_id: str, key: str) -> tuple[Section | None, str | None]:
    key = key.strip().strip("/").lower()
    for s in doc.sections:
        if s.id == key:
            return s, None
    matches = [s for s in doc.sections if s.slug == key or s.id.endswith("/" + key)]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, (f"section {key!r} is ambiguous in {doc_id}; "
                      f"use one of: {', '.join(s.id for s in matches)}")
    top = ", ".join(s.id for s in doc.tree) or "(none)"
    return None, f"no section {key!r} in {doc_id}; top-level sections: {top}"


# --------------------------------------------------------------------------
# Line-range helpers
# --------------------------------------------------------------------------

Range = tuple[int, int]


def merge(ranges: list[Range]) -> list[Range]:
    out: list[Range] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(ranges: list[Range], taken: list[Range]) -> list[Range]:
    out: list[Range] = []
    for a, b in ranges:
        pieces = [(a, b)]
        for ta, tb in taken:
            nxt: list[Range] = []
            for pa, pb in pieces:
                if tb < pa or ta > pb:
                    nxt.append((pa, pb))
                    continue
                if pa < ta:
                    nxt.append((pa, ta - 1))
                if tb < pb:
                    nxt.append((tb + 1, pb))
            pieces = nxt
        out.extend(pieces)
    return out


def fmt_ranges(ranges: list[Range]) -> str:
    return ", ".join(f"L{a}–{b}" for a, b in ranges)


# --------------------------------------------------------------------------
# Selectors and planning
# --------------------------------------------------------------------------

class Selector(BaseModel):
    """One piece of the requested package. Give exactly one of doc / root."""

    doc: str | None = Field(None, description="Document id (from list_documents)")
    root: str | None = Field(None, description=(
        "Directory path relative to the library root; selects every doc under it. "
        "'.' selects the whole library"))
    sections: list[str] | None = Field(None, description=(
        "With doc: section ids to include (subsections come along). Full ids like "
        "'refresh-tokens/rotation' or a unique leaf like 'rotation'"))
    lines: list[int] | None = Field(None, min_length=2, max_length=2, description=(
        "With doc: [start, end], inclusive, 1-based file line numbers"))
    include_images: bool | None = Field(None, description="Override the call-level setting")
    include_code: bool | None = Field(None, description="Override the call-level setting")

    @model_validator(mode="after")
    def _check(self) -> Selector:
        if (self.doc is None) == (self.root is None):
            raise ValueError("each selector needs exactly one of 'doc' or 'root'")
        if self.root is not None and (self.sections or self.lines):
            raise ValueError("'sections' and 'lines' only apply to a 'doc' selector")
        if self.sections and self.lines:
            raise ValueError("use 'sections' or 'lines', not both (make two selectors)")
        if self.lines and not (1 <= self.lines[0] <= self.lines[1]):
            raise ValueError("'lines' must be [start, end] with 1 <= start <= end")
        return self


@dataclass
class Piece:
    entry: DocEntry
    doc: ParsedDoc
    start: int
    end: int
    include_images: bool
    include_code: bool

    @property
    def label(self) -> str:
        if self.start == 1 and self.end == self.doc.n:
            return "full document"
        for s in self.doc.sections:
            if s.start == self.start and s.end == self.end:
                return f"section {s.id}"
        return "lines"

    def estimate(self) -> int:
        tokens, ln = 0, self.start
        while ln <= self.end:
            fi = self.doc.line_fence[ln - 1]
            if fi is None:
                line = self.doc.lines[ln - 1]
                tokens += estimate_tokens(line + "\n")
                if self.include_images:
                    tokens += IMAGE_TOKEN_ESTIMATE * len(IMAGE_STUB.findall(line))
                ln += 1
            else:
                g_end = min(self.end, self.doc.fences[fi].end)
                tokens += (estimate_tokens("\n".join(self.doc.lines[ln - 1:g_end]))
                           if self.include_code else CODE_PLACEHOLDER_TOKENS)
                ln = g_end + 1
        return tokens


def plan(selectors: list[str | Selector], include_images: bool, include_code: bool,
         index: dict[str, DocEntry]) -> tuple[list[Piece], list[str]]:
    pieces: list[Piece] = []
    notes: list[str] = []
    taken: dict[str, list[Range]] = {}

    for item in selectors:
        sel = (Selector(doc=item) if isinstance(item, str)
               else item if isinstance(item, Selector) else Selector.model_validate(item))
        imgs = include_images if sel.include_images is None else sel.include_images
        code = include_code if sel.include_code is None else sel.include_code
        targets: list[tuple[DocEntry, list[Range]]] = []

        if sel.root is not None:
            try:
                found = docs_under(index, sel.root)
            except ValueError as e:
                notes.append(str(e))
                continue
            if not found:
                notes.append(f"no documents under root {sel.root!r}")
            targets = [(e, [(1, -1)]) for e in found]   # -1: through end of file
        else:
            entry = index.get(sel.doc)
            if entry is None:
                notes.append(f"unknown document id {sel.doc!r} (see list_documents)")
                continue
            if sel.sections:
                doc = parse_doc(entry.file)
                ranges = []
                for key in sel.sections:
                    sec, err = find_section(doc, entry.id, key)
                    if err:
                        notes.append(err)
                    else:
                        ranges.append((sec.start, sec.end))
                if not ranges:
                    continue
                targets = [(entry, ranges)]
            elif sel.lines:
                targets = [(entry, [(sel.lines[0], sel.lines[1])])]
            else:
                targets = [(entry, [(1, -1)])]

        for entry, ranges in targets:
            if not entry.file.is_file():
                notes.append(f"{entry.id}: file not found at {entry.path}")
                continue
            doc = parse_doc(entry.file)
            if doc.n == 0:
                notes.append(f"{entry.id}: document is empty")
                continue
            clamped = []
            for a, b in ranges:
                b = doc.n if b == -1 else b
                if a > doc.n:
                    notes.append(f"{entry.id}: L{a}–{b} is past the end of the file ({doc.n} lines)")
                    continue
                if b > doc.n:
                    notes.append(f"{entry.id}: L{a}–{b} clamped to L{a}–{doc.n} (end of file)")
                    b = doc.n
                clamped.append((a, b))
            wanted = merge(clamped)
            remaining = merge(subtract(wanted, taken.get(entry.id, [])))
            dropped = subtract(wanted, remaining)
            if dropped:
                notes.append(f"{entry.id}: {fmt_ranges(merge(dropped))} already included "
                             f"earlier in this package, not repeated")
            for a, b in remaining:
                pieces.append(Piece(entry, doc, a, b, imgs, code))
            taken[entry.id] = merge(taken.get(entry.id, []) + remaining)

    return pieces, notes


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _image_block(img: ImageEntry) -> tuple[ImageContent | None, str | None]:
    if not img.file.is_file():
        return None, "file missing"
    if IMAGE_INDEX.parent not in img.file.parents:
        return None, "file outside the images folder"
    if img.file.suffix.lower() not in MIME_TYPES:
        return None, f"unsupported type {img.file.suffix}"
    if img.file.stat().st_size > MAX_IMAGE_BYTES:
        return None, "too large to inline"
    return ImageContent(type="image",
                        data=base64.b64encode(img.file.read_bytes()).decode(),
                        mime_type=MIME_TYPES[img.file.suffix.lower()]), None


def render(piece: Piece, images: dict[str, ImageEntry],
           blocks: list[TextContent | ImageContent]) -> None:
    doc, e = piece.doc, piece.entry
    buf: list[str] = [f"\n=== {e.id} ({e.path}) · {piece.label} · L{piece.start}–{piece.end} ===\n"]

    def flush() -> None:
        text = _tidy("".join(buf))
        if text.strip():
            blocks.append(TextContent(type="text", text=text))
        buf.clear()

    ln = piece.start
    while ln <= piece.end:
        fi = doc.line_fence[ln - 1]
        if fi is not None:
            f = doc.fences[fi]
            g_end = min(piece.end, f.end)
            if piece.include_code:
                if ln > f.start:                       # range starts mid-block
                    buf.append(f"{f.marker}{f.lang}\n")
                buf.extend(line + "\n" for line in doc.lines[ln - 1:g_end])
                if g_end < f.end or not f.closed:      # range ends mid-block
                    buf.append(f"{f.marker}\n")
            else:
                n_body = max(0, f.end - f.start - (1 if f.closed else 0))
                buf.append(f"[code block omitted: {f.lang or 'plain'}, "
                           f"{n_body} line{'' if n_body == 1 else 's'}, "
                           f"L{f.start}–{f.end}]\n")
            ln = g_end + 1
            continue

        line, pos = doc.lines[ln - 1], 0
        for m in IMAGE_STUB.finditer(line):
            buf.append(line[pos:m.start()])
            img = images.get(m.group(1))
            if img is None:
                buf.append(f"[image {m.group(1)}: not in image index]")
            elif not piece.include_images:
                buf.append(f"[image omitted: {img.id} — {img.caption}]")
            else:
                block, err = _image_block(img)
                if err:
                    buf.append(f"[image {img.id} unavailable ({err}): {img.caption}]")
                else:
                    buf.append(f"[image {img.id}: {img.caption}]\n")
                    flush()
                    blocks.append(block)
            pos = m.end()
        buf.append(line[pos:] + "\n")
        ln += 1

    flush()


# --------------------------------------------------------------------------
# Tree listing
# --------------------------------------------------------------------------

def _section_lines(secs: list[Section], indent: str, line_bounds: bool,
                   depth_left: int | None) -> list[str]:
    if depth_left is not None and depth_left <= 0:
        return []
    out = []
    for s in secs:
        bounds = f"  L{s.start}–{s.end}" if line_bounds else ""
        out.append(f"{indent}§ {s.slug}  ~{fmt_tokens(s.tokens)}{bounds}  {s.title}")
        out += _section_lines(s.children, indent + "  ", line_bounds,
                              None if depth_left is None else depth_left - 1)
    return out


def build_tree(entries: list[DocEntry], root: str) -> dict:
    tree: dict = {"dirs": {}, "docs": []}
    prefix = root + "/" if root else ""
    for e in entries:
        rel = e.path[len(prefix):] if e.path.startswith(prefix) else e.path
        node = tree
        for part in rel.split("/")[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "docs": []})
        node["docs"].append(e)
    return tree


def _doc_tokens(e: DocEntry) -> int:
    if e.approx_tokens is not None:
        return e.approx_tokens
    return estimate_tokens(e.file.read_text(encoding="utf-8")) if e.file.is_file() else 0


def _totals(node: dict) -> tuple[int, int]:
    count = len(node["docs"])
    tokens = sum(_doc_tokens(e) for e in node["docs"])
    for child in node["dirs"].values():
        c, t = _totals(child)
        count, tokens = count + c, tokens + t
    return count, tokens


def _render_tree(node: dict, indent: str, line_bounds: bool,
                 section_depth: int | None) -> list[str]:
    out = []
    for name, child in sorted(node["dirs"].items()):
        c, t = _totals(child)
        out.append(f"{indent}{name}/  ({c} docs, ~{fmt_tokens(t)} tokens)")
        out += _render_tree(child, indent + "  ", line_bounds, section_depth)
    for e in node["docs"]:
        fname = PurePosixPath(e.path).name
        out.append(f"{indent}- {e.id}  [{fname}]  ~{fmt_tokens(_doc_tokens(e))} tokens"
                   f"{f'  — {e.summary}' if e.summary else ''}")
        if not e.file.is_file():
            out.append(f"{indent}    (file not found)")
            continue
        doc = parse_doc(e.file)
        bounds = f", {doc.n} lines" if line_bounds else ""
        if section_depth != 0 and doc.tree:
            out.append(f"{indent}    sections{bounds}:")
            out += _section_lines(doc.tree, indent + "      ", line_bounds, section_depth)
    return out


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

mcp = MCPServer(
    "library",
    instructions=(
        "Reads an indexed markdown library. Browse with list_documents (a tree with "
        "summaries, token estimates and section outlines), then request everything "
        "you need in ONE get_documents call: mix whole docs, directory roots, "
        "sections and line ranges in a single select list. Leave images off unless "
        "the visual detail matters."
    ),
)


@mcp.tool(structured_output=False)
def list_documents(root: str = ".", line_bounds: bool = False,
                   section_depth: int | None = None) -> str:
    """Show the library as a tree starting at `root` (a directory path relative to
    the library root; "." for everything). Each doc shows its id, summary, approx
    tokens and its section outline with per-section token estimates.

    line_bounds: also show each section's file line range (L<start>–<end>), for
        use with get_documents line selectors.
    section_depth: how many heading levels to show (0 = none, default = all).
    """
    index = load_doc_index()
    try:
        r = normalize_root(root)
    except ValueError as e:
        return str(e)
    entries = docs_under(index, r)
    if not entries:
        tops = sorted({e.path.split("/")[0] for e in index.values()})
        return f"No documents under {root!r}. Top-level entries: {', '.join(tops)}"
    tree = build_tree(entries, r)
    c, t = _totals(tree)
    head = [
        f"{r or '.'}/  ({c} docs, ~{fmt_tokens(t)} tokens)",
        "Fetch with get_documents: a doc id, {root}, {doc, sections: [...]} or "
        "{doc, lines: [start, end]}. Section ids nest with '/'; a unique leaf slug also works.",
        "",
    ]
    return "\n".join(head + _render_tree(tree, "", line_bounds, section_depth))


@mcp.tool(structured_output=False)
def get_documents(
    select: list[str | Selector],
    include_images: bool = False,
    include_code: bool = True,
    max_tokens: int | None = DEFAULT_MAX_TOKENS,
) -> list[TextContent | ImageContent]:
    """Assemble one package of library content from any mix of selectors, in the
    order given. Text, code blocks and images are interleaved in document order,
    and each chunk starts with a header naming its doc, section and line range.

    select: each item is either a doc id string (the whole doc) or an object with
        exactly one of
          {"doc": id}                          whole doc
          {"doc": id, "sections": [ids]}       those sections (with subsections)
          {"doc": id, "lines": [start, end]}   a file line range, inclusive
          {"root": "dir/path"}                 every doc under a directory
        plus optional per-selector include_images / include_code overrides.
        Content already included earlier in the same call is not repeated.
    include_images / include_code: defaults for every selector. Omitted images and
        code blocks are replaced by one-line placeholders.
    max_tokens: if the estimated package is larger, nothing is returned except a
        per-piece estimate, so you can narrow the request. null = no limit.
    """
    index = load_doc_index()
    images = load_image_index()
    pieces, notes = plan(select, include_images, include_code, index)

    estimates = [p.estimate() for p in pieces]
    total = sum(estimates)
    if max_tokens is not None and total > max_tokens:
        rows = [f"- {p.entry.id} · {p.label} · L{p.start}–{p.end}: ~{fmt_tokens(est)}"
                for p, est in zip(pieces, estimates)]
        return [TextContent(type="text", text="\n".join([
            f"Package not sent: estimated ~{fmt_tokens(total)} tokens exceeds "
            f"max_tokens={max_tokens}.",
            "Narrow the selectors, turn off images/code, or raise max_tokens. Estimate by piece:",
            *rows, *(f"note: {n}" for n in notes)]))]

    docs_count = len({p.entry.id for p in pieces})
    blocks: list[TextContent | ImageContent] = [TextContent(type="text", text=(
        f"Package: {len(pieces)} pieces from {docs_count} docs, ~{fmt_tokens(total)} tokens"))]
    for p in pieces:
        render(p, images, blocks)
    if notes:
        blocks.append(TextContent(type="text", text="\nNotes:\n" + "\n".join(f"- {n}" for n in notes)))
    return blocks


if __name__ == "__main__":
    mcp.run("stdio")
