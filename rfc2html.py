#!/usr/bin/env python3
"""
rfc2html.py — turn an IETF RFC plain-text document into readable HTML.

RFCs are published as fixed-width plain text: 72-character lines, a running
page header/footer repeated every ~58 lines, and a single monospace font for
everything from prose to ASCII-art diagrams. That's great for a 1980s
line-printer and rough on a modern screen. This script undoes the pagination,
figures out which blocks of text are headings, prose, lists, reference
entries, or verbatim diagrams, and renders the result as a single
self-contained HTML file with real typography, a table of contents, and
diagrams preserved exactly as drawn.

Usage:
    python3 rfc2html.py rfc8340.txt -o rfc8340.html
    python3 rfc2html.py rfc8340.txt -o rfc8340.html --title "YANG Tree Diagrams"

Heuristics used (see comments below for detail):
  * Body prose is indented; headings and the document title are not.
  * A block is treated as a verbatim diagram if it contains alignment
    whitespace or a high density of ASCII-art characters, since that spacing
    is the content (this matters a lot for RFCs about diagram syntax, like
    RFC 8340 itself).
  * The document's own "Table of Contents" section is dropped in favor of a
    generated one, since dotted leaders and page numbers don't mean anything
    once the text is reflowed.

This is a heuristic converter, not a full RFC 7991/xml2rfc parser -- it's
tuned to the common modern RFC plain-text layout and may need adjustment for
unusual or very old (pre-2000) RFC formatting.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------
# Step 1: load the file and strip pagination (form feeds, running
# header/footer lines) so the text reads as one continuous document again.
# --------------------------------------------------------------------------

FOOTER_RE = re.compile(r"\[Page\s+\d+\]\s*$")


def load_and_depaginate(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Split into physical pages on the form-feed character if present.
    if "\f" in raw:
        pages = raw.split("\f")
    else:
        # Some archived copies drop the literal form feed but keep the
        # "[Page N]" footer. Split just after each footer line instead.
        pages, buf = [], []
        for line in raw.splitlines(keepends=True):
            buf.append(line)
            if FOOTER_RE.search(line):
                pages.append("".join(buf))
                buf = []
        if buf:
            pages.append("".join(buf))

    if len(pages) <= 1:
        return raw

    # The running header is whatever non-blank first line is repeated across
    # most pages after the first (page 1 has a different, fancier header).
    first_lines = []
    for page in pages[1:]:
        for line in page.splitlines():
            if line.strip():
                first_lines.append(line.rstrip("\n"))
                break
    header_line = None
    if first_lines:
        candidate = max(set(first_lines), key=first_lines.count)
        if first_lines.count(candidate) >= max(2, len(first_lines) // 2):
            header_line = candidate

    cleaned_pages = []
    for i, page in enumerate(pages):
        lines = page.split("\n")
        # Strip a trailing footer line ("... [Page N]") and the blank
        # padding around it.
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and FOOTER_RE.search(lines[-1]):
            lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
        # Strip the repeated running header from every page but the first.
        if i > 0 and header_line is not None:
            j = 0
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].rstrip("\n") == header_line:
                lines = lines[j + 1:]
                while lines and not lines[0].strip():
                    lines.pop(0)
        cleaned_pages.append("\n".join(lines))

    # Join pages back together. The blank line that originally separated
    # blocks at a page boundary was eaten by the footer/form-feed/header
    # apparatus, so we need to decide whether to restore it. Heuristic: if
    # the last line before the break and the first line after it sit at the
    # same indentation, they are almost certainly the same block continuing
    # (a paragraph or diagram that happened to word-wrap across the page
    # boundary) and should be joined with nothing. If the indentation
    # differs, they are different blocks (e.g. a diagram ending and a
    # flush-left heading starting on the next page) and need the blank line
    # restored, or the heading/paragraph classifier below will merge them.
    joined = cleaned_pages[0]
    for page in cleaned_pages[1:]:
        prev_lines = [l for l in joined.split("\n") if l.strip()]
        next_lines = [l for l in page.split("\n") if l.strip()]
        same_indent = (
            prev_lines and next_lines
            and indent_of(prev_lines[-1]) == indent_of(next_lines[0])
        )
        joined += ("\n" if same_indent else "\n\n") + page
    return joined


# --------------------------------------------------------------------------
# Step 2: split into blocks (runs of non-blank lines separated by >=1 blank
# line), the basic unit we classify below.
# --------------------------------------------------------------------------


def split_blocks(text: str) -> List[List[str]]:
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def common_indent(lines: List[str]) -> int:
    indents = [indent_of(l) for l in lines if l.strip()]
    return min(indents) if indents else 0


# --------------------------------------------------------------------------
# Step 3: classify blocks.
# --------------------------------------------------------------------------

NUMBERED_HEADING_RE = re.compile(
    r"^(?P<num>Appendix\s+[A-Z]|\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)\.?\s+(?P<title>\S.*)$"
)
MASTHEAD_MARKERS = ("Request for Comments:", "Internet-Draft", "Category:", "ISSN:")
REFERENCE_TAG_RE = re.compile(r"^\[(?P<tag>[^\]\s]+)\]\s*(?P<rest>\S.*)?$")
LIST_MARKER_RE = re.compile(r"^(?P<marker>\d+\.|\*|o|-)\s+(?P<text>\S.*)$")


@dataclass
class Heading:
    number: str          # "" if unnumbered
    title: str
    anchor: str
    level: int


@dataclass
class Node:
    kind: str             # 'heading' | 'para' | 'pre' | 'list' | 'reference' | 'title' | 'masthead'
    text: str = ""
    lines: List[str] = field(default_factory=list)
    heading: Optional[Heading] = None
    items: List[str] = field(default_factory=list)
    ordered: bool = False
    ref_tag: str = ""


def is_heading_block(lines: List[str]) -> bool:
    """Only headings and the title sit flush against the left margin in RFC
    plain text; body prose is always indented. A block made entirely of
    flush-left lines is therefore treated as a heading."""
    return all(indent_of(l) == 0 for l in lines)


def looks_like_diagram(lines: List[str]) -> bool:
    """A block is a verbatim diagram if its whitespace is doing structural
    work (column alignment) rather than just being line-wrapped prose, or if
    it is dense with ASCII-art connector characters."""
    ci = common_indent(lines)
    for line in lines:
        body = line[ci:]
        for m in re.finditer(r"[ \t]{2,}", body):
            start = m.start()
            if start == 0:
                # Leading whitespace after dedenting is just this line's own
                # hanging/continuation indent (e.g. a wrapped list item or
                # reference entry) -- normal prose, not a diagram signal.
                continue
            preceding = body[start - 1]
            run_len = len(m.group())
            # Two spaces right after sentence punctuation is normal prose
            # style (RFCs double-space after periods), not a diagram.
            if run_len == 2 and preceding in ".:!?":
                continue
            return True
        artish = len(re.findall(r"[+\-|/\\<>{}\[\]]", body))
        if artish >= 4 and artish >= 0.15 * max(len(body.strip()), 1):
            return True
    return False


def is_reference_block(lines: List[str]) -> bool:
    ci = common_indent(lines)
    return bool(REFERENCE_TAG_RE.match(lines[0][ci:]))


def is_list_block(lines: List[str]) -> bool:
    ci = common_indent(lines)
    return bool(LIST_MARKER_RE.match(lines[0][ci:]))


def reflow(lines: List[str], ci: Optional[int] = None) -> str:
    ci = common_indent(lines) if ci is None else ci
    words = " ".join(l[ci:].strip() for l in lines)
    return re.sub(r"\s+", " ", words).strip()


def split_list_items(lines: List[str]) -> Tuple[bool, List[str]]:
    ci = common_indent(lines)
    items: List[str] = []
    ordered = False
    for line in lines:
        body = line[ci:]
        m = LIST_MARKER_RE.match(body)
        if m:
            if m.group("marker")[0].isdigit():
                ordered = True
            items.append(m.group("text").strip())
        elif items:
            items[-1] += " " + body.strip()
        else:
            items.append(body.strip())
    items = [re.sub(r"\s+", " ", it).strip() for it in items]
    return ordered, items


def slugify(number: str, title: str) -> str:
    base = number if number else title
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return f"section-{slug}" if slug else "section"


def heading_level(number: str) -> int:
    if not number:
        return 1
    return number.count(".") + 1


def parse_document(text: str) -> Tuple[Optional[List[str]], Optional[str], List[Node], List[Heading]]:
    blocks = split_blocks(text)
    masthead: Optional[List[str]] = None
    title: Optional[str] = None
    start = 0

    if blocks and any(marker in l for l in blocks[0] for marker in MASTHEAD_MARKERS):
        masthead = blocks[0]
        start = 1
        if len(blocks) > 1 and len(blocks[1]) == 1 and indent_of(blocks[1][0]) >= 8:
            title = blocks[1][0].strip()
            start = 2

    nodes: List[Node] = []
    headings: List[Heading] = []
    skip_until_next_heading = False

    for lines in blocks[start:]:
        if is_heading_block(lines):
            raw_title = reflow(lines, ci=0)
            m = NUMBERED_HEADING_RE.match(raw_title)
            number, heading_title = (m.group("num"), m.group("title")) if m else ("", raw_title)
            skip_until_next_heading = heading_title.strip().lower() == "table of contents"
            if skip_until_next_heading:
                # We generate our own contents list, so the original ToC
                # heading and its stale dotted-leader body are both dropped.
                continue
            anchor = slugify(number, heading_title)
            level = heading_level(number)
            heading = Heading(number=number, title=heading_title, anchor=anchor, level=level)
            headings.append(heading)
            nodes.append(Node(kind="heading", heading=heading))
            continue

        if skip_until_next_heading:
            continue

        if is_reference_block(lines):
            # Checked before the diagram heuristic: a bibliography entry's
            # hanging indent ("[RFC7407]  Bjorklund, M. ...") also has the
            # kind of interior whitespace gap that looks diagram-ish, but it
            # should be reflowed as a citation, not preserved verbatim.
            ci = common_indent(lines)
            m = REFERENCE_TAG_RE.match(lines[0][ci:])
            first_rest = m.group("rest") or ""
            rest_lines = [first_rest] + [l[ci:] for l in lines[1:]]
            text_ = re.sub(r"\s+", " ", " ".join(rest_lines)).strip()
            nodes.append(Node(kind="reference", ref_tag=m.group("tag"), text=text_))
        elif is_list_block(lines):
            # Checked before the diagram heuristic: a list item whose text
            # contains angle-bracket terms like "<candidate>" (common in
            # NETCONF/YANG documents) trips the ASCII-art density check,
            # but it's prose, not a diagram.
            ordered, items = split_list_items(lines)
            nodes.append(Node(kind="list", ordered=ordered, items=items))
        elif looks_like_diagram(lines):
            ci = common_indent(lines)
            dedented = [l[ci:] if l.strip() else "" for l in lines]
            nodes.append(Node(kind="pre", lines=dedented))
        else:
            nodes.append(Node(kind="para", text=reflow(lines)))

    nodes = merge_adjacent_lists(nodes)
    return masthead, title, nodes, headings


def merge_adjacent_lists(nodes: List[Node]) -> List[Node]:
    """Consecutive list blocks with no other content between them (e.g. a
    numbered list whose items happen to be separated by blank lines in the
    source, so each parsed as its own block) are combined into one list, so
    they render as a single correctly-numbered <ol>/<ul> instead of several
    lists that each restart at 1."""
    merged: List[Node] = []
    for node in nodes:
        if (
            node.kind == "list"
            and merged
            and merged[-1].kind == "list"
            and merged[-1].ordered == node.ordered
        ):
            merged[-1].items.extend(node.items)
        else:
            merged.append(node)
    return merged


# --------------------------------------------------------------------------
# Step 4: render HTML.
# --------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"'&)]+")
# One combined pattern for both "[RFC1234]" and bare "RFC 1234" / "RFC1234"
# forms, so a single pass decides how to link each match -- running two
# separate substitutions back to back would re-match text already inserted
# by the first one and double-wrap it in nested <a> tags. The bare form
# excludes matches preceded by a word character or "/" so identifiers like
# "10.17487/RFC7407" (a DOI, not a reference mention) are left untouched.
RFC_MENTION_RE = re.compile(r"\[RFC(\d{3,5})\]|(?<![\w/])RFC(\s?)(\d{3,5})\b")


def linkify(escaped_text: str) -> str:
    """Turn RFC references and bare URLs into links. Runs on already
    HTML-escaped text, so patterns are matched against escaped forms (a
    literal '<' or '>' has already become '&lt;'/'&gt;' by this point, which
    is why the URL pattern also stops at a bare '&': it means an entity
    follows, not a real URL character)."""

    def url_sub(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:")
        trailing = m.group(0)[len(url):]
        return f'<a href="{url}" rel="noopener">{url}</a>{trailing}'

    def rfc_sub(m: re.Match) -> str:
        if m.group(1):
            n = m.group(1)
            return f'[<a href="https://www.rfc-editor.org/rfc/rfc{n}">RFC{n}</a>]'
        sep, n = m.group(2), m.group(3)
        sep_html = "&nbsp;" if sep else ""
        return f'<a href="https://www.rfc-editor.org/rfc/rfc{n}">RFC{sep_html}{n}</a>'

    escaped_text = URL_RE.sub(url_sub, escaped_text)
    escaped_text = RFC_MENTION_RE.sub(rfc_sub, escaped_text)
    return escaped_text


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_masthead(masthead: List[str]) -> str:
    rows = []
    for line in masthead:
        m = re.match(r"^(\S.*?)(  +)(\S.*)$", line)
        if m:
            left, right = m.group(1).strip(), m.group(3).strip()
        else:
            left, right = line.strip(), ""
        rows.append(f'<div class="masthead-row"><span>{esc(left)}</span><span>{esc(right)}</span></div>')
    return f'<div class="masthead">{"".join(rows)}</div>'


def render_toc(headings: List[Heading]) -> str:
    items = []
    for h in headings:
        label = f"{h.number}.&nbsp;{esc(h.title)}" if h.number else esc(h.title)
        items.append(
            f'<li class="toc-l{h.level}"><a href="#{h.anchor}">{label}</a></li>'
        )
    return "<ul class=\"toc-list\">" + "".join(items) + "</ul>"


def render_nodes(nodes: List[Node]) -> str:
    out = []
    for node in nodes:
        if node.kind == "heading":
            h = node.heading
            tag = f"h{min(h.level + 1, 6)}"
            number_html = f'<span class="secnum">{esc(h.number)}.</span> ' if h.number else ""
            out.append(f'<{tag} id="{h.anchor}">{number_html}{esc(h.title)}</{tag}>')
        elif node.kind == "para":
            out.append(f"<p>{linkify(esc(node.text))}</p>")
        elif node.kind == "list":
            tag = "ol" if node.ordered else "ul"
            items = "".join(f"<li>{linkify(esc(it))}</li>" for it in node.items)
            out.append(f"<{tag}>{items}</{tag}>")
        elif node.kind == "reference":
            out.append(
                f'<p class="reference"><span class="reftag">[{esc(node.ref_tag)}]</span> '
                f"{linkify(esc(node.text))}</p>"
            )
        elif node.kind == "pre":
            body = "\n".join(node.lines).rstrip("\n")
            out.append(f"<pre><code>{esc(body)}</code></pre>")
    return "\n".join(out)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #f7f7f3;
  --panel: #ffffff;
  --ink: #1a1d22;
  --ink-soft: #5b5f66;
  --rule: #dfe0da;
  --accent: #136f63;
  --accent-soft: #e4f1ee;
  --code-bg: #12161c;
  --code-ink: #d9e6e2;
  --code-border: #1f6f63;
  --serif: "Source Serif 4", Georgia, "Iowan Old Style", serif;
  --sans: "IBM Plex Sans", -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 18px;
  line-height: 1.65;
}}
a {{ color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 2px; }}
a:hover {{ text-decoration-thickness: 2px; }}

.top-bar {{
  font-family: var(--sans);
  font-size: 13px;
  color: var(--ink-soft);
  border-bottom: 1px solid var(--rule);
  padding: 10px 24px;
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
}}
.top-bar strong {{ color: var(--ink); }}

.layout {{
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  max-width: 1180px;
  margin: 0 auto;
  align-items: start;
}}

nav.sidebar {{
  position: sticky;
  top: 0;
  align-self: start;
  max-height: 100vh;
  overflow-y: auto;
  padding: 28px 20px 40px 24px;
  font-family: var(--sans);
  font-size: 14px;
}}
nav.sidebar h2 {{
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--ink-soft);
  margin: 0 0 10px;
  font-weight: 600;
}}
.toc-list {{ list-style: none; margin: 0; padding: 0; }}
.toc-list li {{ margin: 3px 0; }}
.toc-list a {{ color: var(--ink); text-decoration: none; display: block; padding: 3px 0; border-left: 2px solid transparent; padding-left: 10px; }}
.toc-list a:hover {{ border-left-color: var(--accent); color: var(--accent); }}
.toc-l1 {{ font-weight: 600; margin-top: 10px !important; }}
.toc-l2 {{ padding-left: 12px; font-weight: 400; }}
.toc-l3 {{ padding-left: 26px; font-size: 13px; color: var(--ink-soft); }}

main {{
  padding: 40px clamp(20px, 4vw, 56px) 100px;
  min-width: 0;
}}
.doc-header h1 {{
  font-size: clamp(28px, 4vw, 40px);
  line-height: 1.15;
  margin: 6px 0 18px;
}}
.masthead {{
  font-family: var(--sans);
  font-size: 13.5px;
  color: var(--ink-soft);
  border: 1px solid var(--rule);
  background: var(--panel);
  border-radius: 6px;
  padding: 14px 18px;
  margin-bottom: 28px;
}}
.masthead-row {{ display: flex; justify-content: space-between; gap: 20px; padding: 1px 0; }}
.masthead-row span:first-child {{ color: var(--ink); }}

article {{ max-width: 74ch; }}
article h2, article h3, article h4 {{
  font-family: var(--serif);
  font-weight: 600;
  line-height: 1.3;
  margin: 2.1em 0 0.6em;
}}
article h2 {{ font-size: 26px; border-top: 1px solid var(--rule); padding-top: 0.9em; }}
article h3 {{ font-size: 21px; }}
article h4 {{ font-size: 18px; color: var(--ink-soft); }}
.secnum {{ color: var(--accent); font-variant-numeric: tabular-nums; }}

article p {{ margin: 0 0 1.05em; }}
article ul, article ol {{ margin: 0 0 1.05em; padding-left: 1.4em; }}
article li {{ margin: 0.35em 0; }}

article p.reference {{ font-size: 15.5px; padding-left: 4.2em; text-indent: -4.2em; color: var(--ink-soft); }}
article p.reference .reftag {{ color: var(--ink); font-family: var(--mono); font-size: 13.5px; }}

pre {{
  background: var(--code-bg);
  color: var(--code-ink);
  border: 1px solid var(--code-border);
  border-radius: 8px;
  padding: 18px 20px;
  overflow-x: auto;
  margin: 1.3em 0 1.6em;
  box-shadow: inset 0 0 0 1px rgba(19, 111, 99, 0.15);
}}
pre code {{
  font-family: var(--mono);
  font-size: 14px;
  line-height: 1.55;
  white-space: pre;
}}

@media (max-width: 900px) {{
  .layout {{ display: block; }}
  nav.sidebar {{
    position: static;
    max-height: none;
    border-bottom: 1px solid var(--rule);
    padding: 16px 20px;
  }}
  nav.sidebar[data-collapsed="true"] .toc-list {{ display: none; }}
  main {{ padding: 28px 20px 80px; }}
  article {{ max-width: none; }}
}}

@media print {{
  nav.sidebar {{ display: none; }}
  .layout {{ display: block; }}
  body {{ background: white; }}
}}
</style>
</head>
<body>
<div class="top-bar">
  {topbar}
</div>
<div class="layout">
  <nav class="sidebar" aria-label="Table of contents">
    <h2>Contents</h2>
    {toc}
  </nav>
  <main>
    <div class="doc-header">
      <h1>{title}</h1>
      {masthead}
    </div>
    <article>
      {body}
    </article>
  </main>
</div>
</body>
</html>
"""


def render_html(masthead: Optional[List[str]], title: Optional[str], nodes: List[Node],
                 headings: List[Heading], override_title: Optional[str]) -> str:
    doc_title = override_title or title or "RFC"

    rfc_num = None
    date_str = None
    if masthead:
        for line in masthead:
            m = re.search(r"Request for Comments:\s*(\d+)", line)
            if m:
                rfc_num = m.group(1)
            m2 = re.search(r"(\d{4})\s*$", line)
            if m2 and re.search(r"[A-Za-z]", line):
                date_str = line.strip().split()[-2:]
                date_str = " ".join(date_str) if date_str else None

    topbar_bits = []
    if rfc_num:
        topbar_bits.append(f"<strong>RFC {rfc_num}</strong>")
    if date_str:
        topbar_bits.append(esc(date_str))
    topbar_bits.append('<span>Reflowed for readability &middot; original pagination removed</span>')
    topbar = " &middot; ".join(topbar_bits)

    return PAGE_TEMPLATE.format(
        title=esc(doc_title),
        topbar=topbar,
        toc=render_toc(headings),
        masthead=render_masthead(masthead) if masthead else "",
        body=render_nodes(nodes),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="Path to the RFC .txt file")
    parser.add_argument("-o", "--output", type=Path, help="Output .html path (default: alongside input)")
    parser.add_argument("--title", help="Override the detected document title")
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 1

    text = load_and_depaginate(args.input)
    masthead, title, nodes, headings = parse_document(text)
    out_html = render_html(masthead, title, nodes, headings, args.title)

    out_path = args.output or args.input.with_suffix(".html")
    out_path.write_text(out_html, encoding="utf-8")
    print(f"Wrote {out_path} ({len(headings)} sections detected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
