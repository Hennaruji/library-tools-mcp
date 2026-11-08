"""
library_server.py: an MCP server for reading an indexed markdown library.

The agent browses the library as a tree (list_documents), then requests a
"package" of context (get_documents) built from any mix of whole docs,
directory roots, named sections and line ranges. Markdown text (including
fenced code blocks) and images come back interleaved in document order.

Library conventions (all paths relative to LIBRARY_ROOT):

    index.json            document index (cached summaries and token counts)
        {"documents": [{"id", "path", "title", "summary", "approx_tokens"}]}
    images/index.json     image index; "file" is relative to this folder
        {"images": [{"id", "file", "caption"}]}
    *.md                  docs, anywhere under the root, at their index "path"

    Image stubs in markdown:   ![[image:<image_id>]]

Token counts come only from the document index; this server does not
estimate tokens itself.

Sections are not cached: they are parsed from ATX headings (#, ##, ...) on
request (re-parsed only when a file changes). A section id is its heading
slug, nested with "/" for subsections (auth: "refresh-tokens/rotation").
list_documents always shows full ids; a bare leaf slug is also accepted
when unique within the doc. Line numbers are the file's own 1-based line
numbers (lines split on \n only), so they agree with Read and grep.

Run:   python library_server.py           (stdio transport)
Needs: pip install "mcp>=2"
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Annotated
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", Path(__file__).parent / "library")).resolve()
DOC_INDEX_REL = os.environ.get("LIBRARY_DOC_INDEX", "index.json")
IMAGE_INDEX_REL = os.environ.get("LIBRARY_IMAGE_INDEX", "images/index.json")
MAX_IMAGE_BYTES = int(os.environ.get("LIBRARY_MAX_IMAGE_BYTES", 5 * 1024 * 1024))

IMAGE_STUB = re.compile(r"!\[\[image:([A-Za-z0-9_.\-]+)\]\]")
# CommonMark ATX heading: up to 3 spaces of indent, 1-6 #s, then a space or
# end of line. A closing run of #s only counts when preceded by whitespace,
# so "## C#" keeps its title.
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------

class LibraryIndexError(ToolError):
    """An index file is missing, unreadable or has invalid entries. Subclassing
    the SDK's ToolError makes the message reach the calling agent; other
    exceptions are reported only as "Error executing tool"."""


# --- Path enforcement ------------------------------------------------------
# Every file the server reads must resolve (symlinks included) to a location
# inside LIBRARY_ROOT; images must also resolve inside the image index's
# folder. Index paths must be relative, '/'-separated and free of '..'.

def check_relative_path(value: object) -> str | None:
    """Return a problem description, or None if `value` is an acceptable path."""
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty string"
    if "\\" in value:
        return "must use '/' separators"
    p = PurePosixPath(value)
    if p.is_absolute():
        return "must be relative, not absolute"
    if ".." in p.parts:
        return "may not contain '..'"
    return None


def resolve_inside(base: Path, rel: str) -> Path | None:
    """base/rel with symlinks resolved, or None if that lands outside base."""
    base = base.resolve()
    p = (base / rel).resolve()
    return p if base in p.parents else None


def index_file(rel: str, label: str) -> Path:
    err = check_relative_path(rel)
    path = None if err else resolve_inside(LIBRARY_ROOT, rel)
    if path is None:
        raise LibraryIndexError(f"{label} location {rel!r} must be inside the library root"
                                + (f" ({err})" if err else ""))
    return path


def _read_index_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise LibraryIndexError(f"{label} {path.name} is not valid JSON "
                                f"(line {e.lineno}, column {e.colno}): {e.msg}") from None


def _raise_if_problems(label: str, name: str, problems: list[str]) -> None:
    if problems:
        shown = problems[:20]
        more = f"\n- ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise LibraryIndexError(f"{label} {name} has {len(problems)} problem"
                                f"{'' if len(problems) == 1 else 's'}:\n"
                                + "\n".join(f"- {p}" for p in shown) + more)


def _where(kind: str, i: int, d: object) -> str:
    ident = d.get("id") if isinstance(d, dict) else None
    return f"{kind}[{i}]" + (f" (id {ident!r})" if isinstance(ident, str) and ident else "")


# --- Indexes ---------------------------------------------------------------

@dataclass
class DocEntry:
    id: str
    path: str            # posix path relative to LIBRARY_ROOT
    title: str
    summary: str
    approx_tokens: int | None

    def locate(self) -> tuple[Path | None, str | None]:
        """(file, None) if readable, else (None, reason). Enforces the library boundary."""
        file = resolve_inside(LIBRARY_ROOT, self.path)
        if file is None:
            return None, f"{self.path} resolves outside the library; refused"
        if not file.is_file():
            return None, f"file not found at {self.path}"
        return file, None


@dataclass
class ImageEntry:
    id: str
    file: str            # relative to the image index's folder
    caption: str


def load_doc_index() -> dict[str, DocEntry]:
    label = "document index"
    path = index_file(DOC_INDEX_REL, label)
    if not path.is_file():
        raise LibraryIndexError(f"{label} not found at {DOC_INDEX_REL}")
    data = _read_index_json(path, label)
    docs = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        raise LibraryIndexError(f"{label} {path.name} must be an object with a 'documents' list")

    problems: list[str] = []
    out: dict[str, DocEntry] = {}
    for i, d in enumerate(docs):
        where, before = _where("documents", i, d), len(problems)
        if not isinstance(d, dict):
            problems.append(f"{where}: expected an object")
            continue
        for key in ("id", "path"):
            if not isinstance(d.get(key), str) or not d[key].strip():
                problems.append(f"{where}: '{key}' is required and must be a non-empty string")
        if isinstance(d.get("path"), str) and d["path"].strip():
            if err := check_relative_path(d["path"]):
                problems.append(f"{where}: 'path' {err}")
        for key in ("title", "summary"):
            if d.get(key) is not None and not isinstance(d[key], str):
                problems.append(f"{where}: '{key}' must be a string")
        t = d.get("approx_tokens")
        if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t < 0):
            problems.append(f"{where}: 'approx_tokens' must be a non-negative integer")
        if isinstance(d.get("id"), str) and d["id"] in out:
            problems.append(f"{where}: duplicate id (also used by {out[d['id']].path})")
        if len(problems) == before:
            out[d["id"]] = DocEntry(d["id"], PurePosixPath(d["path"]).as_posix(),
                                    d.get("title") or d["id"], d.get("summary") or "", t)
    _raise_if_problems(label, path.name, problems)
    return dict(sorted(out.items(), key=lambda kv: kv[1].path))


def image_dir() -> Path:
    return index_file(IMAGE_INDEX_REL, "image index").parent


def load_image_index() -> dict[str, ImageEntry]:
    label = "image index"
    path = index_file(IMAGE_INDEX_REL, label)
    if not path.is_file():
        return {}                     # a library without images is fine
    data = _read_index_json(path, label)
    imgs = data.get("images") if isinstance(data, dict) else None
    if not isinstance(imgs, list):
        raise LibraryIndexError(f"{label} {path.name} must be an object with an 'images' list")

    problems: list[str] = []
    out: dict[str, ImageEntry] = {}
    for i, d in enumerate(imgs):
        where, before = _where("images", i, d), len(problems)
        if not isinstance(d, dict):
            problems.append(f"{where}: expected an object")
            continue
        for key in ("id", "file"):
            if not isinstance(d.get(key), str) or not d[key].strip():
                problems.append(f"{where}: '{key}' is required and must be a non-empty string")
        if isinstance(d.get("file"), str) and d["file"].strip():
            if err := check_relative_path(d["file"]):
                problems.append(f"{where}: 'file' {err}")
        if d.get("caption") is not None and not isinstance(d["caption"], str):
            problems.append(f"{where}: 'caption' must be a string")
        if isinstance(d.get("id"), str) and d["id"] in out:
            problems.append(f"{where}: duplicate id")
        if len(problems) == before:
            out[d["id"]] = ImageEntry(d["id"], d["file"], d.get("caption") or "")
    _raise_if_problems(label, path.name, problems)
    return out


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
    level: int           # heading level; 0 for _intro / _preamble
    start: int           # 1-based, heading line
    end: int             # inclusive
    children: list[Section] = field(default_factory=list)


@dataclass
class Fence:
    start: int           # opening fence line
    end: int             # closing fence line (or last line if unclosed)
    marker: str          # opening run as written, e.g. ``` or ~~~~
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

    def has_code(self, a: int, b: int) -> bool:
        return any(self.line_fence[i - 1] is not None for i in range(a, b + 1))

    def has_images(self, a: int, b: int) -> bool:
        return any(self.line_fence[i - 1] is None and IMAGE_STUB.search(self.lines[i - 1])
                   for i in range(a, b + 1))


_parse_cache: dict[Path, tuple[float, ParsedDoc]] = {}


def slugify(title: str) -> str:
    s = re.sub(r"[`*_~\[\]()]", "", title.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "section"


def split_lines(text: str) -> list[str]:
    """Split on \\n only (like grep and Read), dropping a trailing \\r per line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def parse_doc(path: Path) -> ParsedDoc:
    mtime = path.stat().st_mtime
    cached = _parse_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    lines = split_lines(path.read_text(encoding="utf-8-sig", errors="replace"))
    n = len(lines)
    fences: list[Fence] = []
    line_fence: list[int | None] = [None] * n
    headings: list[tuple[int, int, str]] = []        # (line, level, title)
    open_fence: tuple[int, str, str] | None = None   # (line, marker, lang)

    for ln, line in enumerate(lines, 1):
        if open_fence is None:
            m = FENCE_OPEN.match(line)
            # A backtick fence's info string may not contain backticks
            # (so "```bash``` is the tool" is inline code, not a fence).
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                open_fence = (ln, m.group(1), m.group(2).strip())
                line_fence[ln - 1] = len(fences)
                continue
            h = HEADING.match(line)
            if h:
                headings.append((ln, len(h.group(1)), (h.group(2) or "").strip()))
        else:
            line_fence[ln - 1] = len(fences)
            c = FENCE_CLOSE.match(line)
            marker = open_fence[1]
            if c and c.group(1)[0] == marker[0] and len(c.group(1)) >= len(marker):
                fences.append(Fence(open_fence[0], ln, marker, open_fence[2], True))
                open_fence = None
    if open_fence:
        fences.append(Fence(open_fence[0], n, open_fence[1], open_fence[2], False))

    # A doc whose first heading is its only H1 uses that H1 as its title: the
    # H1 is not a level of the section tree, and the text under it (up to the
    # next heading) is "_intro". Otherwise every id would start with the title.
    intro: Section | None = None
    h1s = [h for h in headings if h[1] == 1]
    if len(h1s) == 1 and headings[0] == h1s[0]:
        title_ln = h1s[0][0]
        headings = headings[1:]
        nxt = headings[0][0] if headings else n + 1
        intro = Section("_intro", "_intro", h1s[0][2], 0, title_ln, nxt - 1)

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
        i = 2
        while any(s.slug == slug for s in siblings):
            slug, i = f"{base}-{i}", i + 1
        sec = Section(f"{parent.id}/{slug}" if parent else slug, slug, title, level, ln, end)
        siblings.append(sec)
        flat.append(sec)
        stack.append(sec)

    if intro:
        tree.insert(0, intro)
        flat.insert(0, intro)

    first = intro.start if intro else (headings[0][0] if headings else n + 1)
    if first > 1 and "\n".join(lines[:first - 1]).strip():
        pre = Section("_preamble", "_preamble", "(text before the first heading)", 0, 1, first - 1)
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


def subtract(r: Range, taken: list[Range]) -> list[Range]:
    pieces = [r]
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
    return pieces


# --------------------------------------------------------------------------
# Selectors and planning
# --------------------------------------------------------------------------

class Selector(BaseModel):
    """One piece of the requested package. Give exactly one of doc / root."""

    model_config = ConfigDict(extra="forbid")

    doc: str | None = Field(None, description="Document id (from list_documents)")
    root: str | None = Field(None, description=(
        "Directory path relative to the library root; selects every doc under it. "
        "'.' selects the whole library"))
    sections: list[str] | None = Field(None, min_length=1, description=(
        "With doc: section ids exactly as shown by list_documents (subsections "
        "come along), returned in the order given"))
    lines: list[int] | None = Field(None, min_length=2, max_length=2, description=(
        "With doc: [start, end], inclusive, 1-based file line numbers"))
    include_images: bool | None = Field(None, description="Override the call-level setting")
    include_code: bool | None = Field(None, description="Override the call-level setting")

    @model_validator(mode="after")
    def _check(self) -> Selector:
        if (self.doc is None) == (self.root is None):
            raise ValueError("each selector needs exactly one of 'doc' or 'root'")
        if self.root is not None and (self.sections is not None or self.lines is not None):
            raise ValueError("'sections' and 'lines' only apply to a 'doc' selector")
        if self.sections is not None and self.lines is not None:
            raise ValueError("use 'sections' or 'lines', not both (make two selectors)")
        if self.lines is not None and not (1 <= self.lines[0] <= self.lines[1]):
            raise ValueError("'lines' must be [start, end] with 1 <= start <= end")
        return self


@dataclass
class Piece:
    entry: DocEntry
    doc: ParsedDoc
    start: int
    end: int
    label: str           # "full document", "section <id>", "lines A–B"
    partial: bool        # part of the request was already returned by an overlap
    include_images: bool
    include_code: bool


@dataclass
class Claim:
    """Lines already placed in the package, and by which selector."""
    start: int
    end: int
    selector: int        # 1-based position in `select`
    label: str
    include_images: bool
    include_code: bool


def _describe_settings(c: Claim) -> str:
    return (f"images {'included' if c.include_images else 'omitted'}, "
            f"code {'included' if c.include_code else 'omitted'}")


def plan(selectors: list, include_images: bool, include_code: bool,
         index: dict[str, DocEntry]) -> tuple[list[Piece], list[str], list[str]]:
    """Returns (pieces, overlap notices, other notes)."""
    pieces: list[Piece] = []
    overlaps: list[str] = []
    notes: list[str] = []
    claims: dict[str, list[Claim]] = {}

    def add(entry: DocEntry, doc: ParsedDoc, a: int, b: int, label: str,
            sel_no: int, imgs: bool, code: bool) -> None:
        if a > doc.n:
            notes.append(f"selector {sel_no}: {entry.id} L{a}–{b} is past the end "
                         f"of the file ({doc.n} lines)")
            return
        if b > doc.n:
            notes.append(f"selector {sel_no}: {entry.id} L{a}–{b} clamped to "
                         f"L{a}–{doc.n} (end of file)")
            b = doc.n
        prior = claims.setdefault(entry.id, [])
        for c in prior:
            oa, ob = max(a, c.start), min(b, c.end)
            if oa > ob:
                continue
            same = c.selector == sel_no
            where = "an earlier part of the same selector" if same else f"selector {c.selector}"
            chunk = (f"the '{entry.id} {c.label}' chunk" if same
                     else f"selector {c.selector}'s '{entry.id} {c.label}' chunk")
            msg = (f"selector {sel_no} ({entry.id} {label}) overlaps {where} "
                   f"at L{oa}–{ob}; those lines appear once, in {chunk} above")
            differs = ((c.include_code != code and doc.has_code(oa, ob))
                       or (c.include_images != imgs and doc.has_images(oa, ob)))
            if differs:
                msg += (f", returned with {_describe_settings(c)}. To get them with "
                        f"this selector's settings, request them in a separate call")
            overlaps.append(msg + ".")
        remaining = subtract((a, b), [(c.start, c.end) for c in prior])
        for ra, rb in remaining:
            pieces.append(Piece(entry, doc, ra, rb, label, (ra, rb) != (a, b), imgs, code))
            prior.append(Claim(ra, rb, sel_no, label, imgs, code))

    for sel_no, item in enumerate(selectors, 1):
        if isinstance(item, str):
            if item.lstrip().startswith("{"):
                notes.append(f"selector {sel_no}: got a JSON string, not an object; "
                             f"send selectors as objects, e.g. {{\"doc\": \"auth\"}}")
                continue
            sel = Selector(doc=item)
        else:
            sel = item if isinstance(item, Selector) else Selector.model_validate(item)
        imgs = include_images if sel.include_images is None else sel.include_images
        code = include_code if sel.include_code is None else sel.include_code

        if sel.root is not None:
            try:
                entries = docs_under(index, sel.root)
            except ValueError as e:
                notes.append(f"selector {sel_no}: {e}")
                continue
            if not entries:
                notes.append(f"selector {sel_no}: no documents under root {sel.root!r}")
        else:
            entry = index.get(sel.doc)
            if entry is None:
                notes.append(f"selector {sel_no}: unknown document id {sel.doc!r} "
                             f"(see list_documents)")
                continue
            entries = [entry]

        for entry in entries:
            file, err = entry.locate()
            if err:
                notes.append(f"selector {sel_no}: {entry.id}: {err}")
                continue
            doc = parse_doc(file)
            if doc.n == 0:
                notes.append(f"selector {sel_no}: {entry.id} is empty")
                continue
            if sel.sections is not None:
                for key in sel.sections:
                    sec, err = find_section(doc, entry.id, key)
                    if err:
                        notes.append(f"selector {sel_no}: {err}")
                    else:
                        add(entry, doc, sec.start, sec.end, f"section {sec.id}",
                            sel_no, imgs, code)
            elif sel.lines is not None:
                a, b = sel.lines
                add(entry, doc, a, b, f"lines {a}–{b}", sel_no, imgs, code)
            else:
                add(entry, doc, 1, doc.n, "full document", sel_no, imgs, code)

    return pieces, overlaps, notes


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _image_block(img: ImageEntry, folder: Path) -> tuple[ImageContent | None, str | None]:
    file = resolve_inside(folder, img.file)
    if file is None:
        return None, "file resolves outside the images folder; refused"
    if not file.is_file():
        return None, "file missing"
    if file.suffix.lower() not in MIME_TYPES:
        return None, f"unsupported type {file.suffix}"
    if file.stat().st_size > MAX_IMAGE_BYTES:
        return None, "too large to inline"
    return ImageContent(type="image",
                        data=base64.b64encode(file.read_bytes()).decode(),
                        mime_type=MIME_TYPES[file.suffix.lower()]), None


def render(piece: Piece, images: dict[str, ImageEntry], folder: Path,
           sent_images: dict[str, set[str]],
           blocks: list[TextContent | ImageContent]) -> None:
    """Document lines are emitted exactly as written; only headers,
    placeholders and fence repairs are added around them.

    `sent_images` maps doc id -> image ids already sent in this call, so an
    image repeated within one document is sent once. The same image in a
    different document is sent again."""
    doc, e = piece.doc, piece.entry
    part = " (part)" if piece.partial else ""
    # A lines selector's label would just repeat the range, so it is omitted.
    what = "" if piece.label.startswith("lines ") else f" · {piece.label}{part}"
    buf: list[str] = [f"\n=== {e.id} ({e.path}){what} · L{piece.start}–{piece.end} ===\n"]

    def flush() -> None:
        text = "".join(buf)
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
                           f"{n_body} line{'' if n_body == 1 else 's'}, L{f.start}–{f.end}]\n")
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
            elif img.id in sent_images.setdefault(e.id, set()):
                buf.append(f"[image {img.id}: same image as shown earlier in this document]")
            else:
                block, err = _image_block(img, folder)
                if err:
                    buf.append(f"[image {img.id} unavailable ({err}): {img.caption}]")
                else:
                    sent_images[e.id].add(img.id)
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
        # Full id, so the agent can copy it verbatim into a sections selector.
        out.append(f"{indent}§ {s.id}  \"{s.title}\"{bounds}")
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


def _totals(node: dict) -> tuple[int, int, bool]:
    """(doc count, summed index tokens, whether any doc lacks a token count)."""
    count = len(node["docs"])
    tokens = sum(e.approx_tokens or 0 for e in node["docs"])
    unknown = any(e.approx_tokens is None for e in node["docs"])
    for child in node["dirs"].values():
        c, t, u = _totals(child)
        count, tokens, unknown = count + c, tokens + t, unknown or u
    return count, tokens, unknown


def _fmt_totals(node: dict) -> str:
    c, t, unknown = _totals(node)
    return f"{c} doc{'' if c == 1 else 's'}, ~{t}{'+' if unknown else ''} tokens"


def _render_tree(node: dict, indent: str, line_bounds: bool,
                 section_depth: int | None) -> list[str]:
    out = []
    for name, child in sorted(node["dirs"].items()):
        out.append(f"{indent}{name}/  ({_fmt_totals(child)})")
        out += _render_tree(child, indent + "  ", line_bounds, section_depth)
    for e in node["docs"]:
        fname = PurePosixPath(e.path).name
        tokens = f"~{e.approx_tokens} tokens" if e.approx_tokens is not None else "tokens unknown"
        out.append(f"{indent}- {e.id}  [{fname}]  {tokens}"
                   f"{f'  — {e.summary}' if e.summary else ''}")
        file, err = e.locate()
        if err:
            out.append(f"{indent}    ({err})")
            continue
        if section_depth is not None and section_depth <= 0:
            continue
        doc = parse_doc(file)
        if doc.tree:
            bounds = f", {doc.n} lines" if line_bounds else ""
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
        "summaries, token counts and section outlines), then request everything you "
        "need in ONE get_documents call: mix whole docs, directory roots, sections "
        "and line ranges in a single select list. Leave images off unless the visual "
        "detail matters."
    ),
)


@mcp.tool(structured_output=False)
def list_documents(
    root: Annotated[str, Field(description=(
        "Directory to list, relative to the library root. '.' lists the whole library"))] = ".",
    line_bounds: Annotated[bool, Field(description=(
        "Also show each section's file line range, for use in get_documents line selectors"))] = False,
    section_depth: Annotated[int | None, Field(description=(
        "Heading levels to show: 0 = none, 1 = top-level sections only, null = all"))] = None,
) -> str:
    """Show the library as a tree starting at `root` (a directory path relative to
    the library root; "." for everything). Each doc shows its id, approx tokens
    and summary from the library index, and its section outline: each section's
    full id (use it verbatim in get_documents) and heading text.

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
    head = [
        f"{r or '.'}/  ({_fmt_totals(tree)})",
        "Fetch with get_documents: a doc id, {root}, {doc, sections: [...]} or "
        "{doc, lines: [start, end]}. Use section ids exactly as shown after §.",
        "",
    ]
    return "\n".join(head + _render_tree(tree, "", line_bounds, section_depth))


@mcp.tool(structured_output=False)
def get_documents(
    select: Annotated[list[str | Selector], Field(min_length=1, description=(
        "What to include, in order: doc id strings and/or selector objects"))],
    include_images: Annotated[bool, Field(description=(
        "Default for every selector: send images inline (true) or as one-line placeholders"))] = False,
    include_code: Annotated[bool, Field(description=(
        "Default for every selector: include fenced code blocks (true) or one-line placeholders"))] = True,
) -> list[TextContent | ImageContent]:
    """Assemble one package of library content from any mix of selectors, in the
    order given. Markdown text, fenced code blocks and images are interleaved in
    document order, and each chunk starts with a header naming its doc, what was
    selected, and its line range.

    select: each item is either a doc id string (the whole doc) or an object with
        exactly one of
          {"doc": id}                          whole doc
          {"doc": id, "sections": [ids]}       those sections, in the order given
          {"doc": id, "lines": [start, end]}   a file line range, inclusive
          {"root": "dir/path"}                 every doc under a directory
        plus optional per-selector include_images / include_code overrides.
        If selectors in one call overlap, the overlapping lines are returned
        once and an overlap notice at the end says where they are. Separate
        calls are independent and always return everything requested.
    include_images / include_code: defaults for every selector ("code" means
        fenced code blocks in the markdown). Omitted images and code blocks are
        replaced by one-line placeholders.
    """
    index = load_doc_index()
    images = load_image_index()
    pieces, overlaps, notes = plan(select, include_images, include_code, index)

    docs_count = len({p.entry.id for p in pieces})
    summary = (f"Package: {len(pieces)} chunk{'' if len(pieces) == 1 else 's'} from "
               f"{docs_count} doc{'' if docs_count == 1 else 's'}")
    extras = [f"{len(overlaps)} overlap notice{'' if len(overlaps) == 1 else 's'}"] if overlaps else []
    extras += [f"{len(notes)} note{'' if len(notes) == 1 else 's'}"] if notes else []
    if extras:
        summary += f" ({', '.join(extras)} at the end)"

    blocks: list[TextContent | ImageContent] = [TextContent(type="text", text=summary)]
    folder = image_dir()
    sent_images: dict[str, set[str]] = {}
    for p in pieces:
        render(p, images, folder, sent_images, blocks)
    if overlaps:
        blocks.append(TextContent(type="text", text="\nOverlap notices:\n"
                                  + "\n".join(f"- {o}" for o in overlaps)))
    if notes:
        blocks.append(TextContent(type="text", text="\nNotes:\n"
                                  + "\n".join(f"- {n}" for n in notes)))
    return blocks


def inline_refs(schema: dict) -> dict:
    """Replace local "$ref": "#/$defs/X" pointers with the definitions they name.
    Some MCP clients render $defs/$ref poorly; the inlined schema is equivalent."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(defs[ref.rsplit("/", 1)[1]])
                return {**target, **{k: walk(v) for k, v in node.items() if k != "$ref"}}
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


# The SDK has no public hook for adjusting a tool's generated input schema, so
# this reaches into its tool manager once at startup.
for _tool in mcp._tool_manager.list_tools():
    _tool.parameters = inline_refs(_tool.parameters)


if __name__ == "__main__":
    mcp.run("stdio")
