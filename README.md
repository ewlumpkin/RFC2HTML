# RFC2HTML

Turn an IETF RFC plain-text document into readable HTML.

RFCs are published as fixed-width plain text: 72-character lines, a running
page header/footer repeated every ~58 lines, and a single monospace font for
everything from prose to ASCII-art diagrams. That's great for a 1980s
line-printer and rough on a modern screen. `rfc2html.py` undoes the
pagination, figures out which blocks of text are headings, prose, lists,
reference entries, or verbatim diagrams, and renders the result as a single
self-contained HTML file with real typography, a table of contents, and
diagrams preserved exactly as drawn.

## Usage

```sh
python3 rfc2html.py rfc8340.txt -o rfc8340.html
python3 rfc2html.py rfc8340.txt -o rfc8340.html --title "YANG Tree Diagrams"

# Or skip the download step and fetch the RFC directly:
python3 rfc2html.py 8340
python3 rfc2html.py rfc6241 -o netconf.html
python3 rfc2html.py https://www.ietf.org/ietf-ftp/rfc/rfc6241.html
```

| Argument | Description |
| --- | --- |
| `input` | A local RFC `.txt` file, or an RFC to fetch: a bare number (`8340`), an `rfcNNNN` token, or any URL that names one (required) |
| `-o`, `--output` | Output `.html` path (default: input's filename, or `rfcNNNN.html` when fetched, with `.html`) |
| `--title` | Override the detected document title |

No dependencies beyond the Python 3 standard library.

### Fetching an RFC directly

If `input` isn't a path to an existing local file, it's treated as an RFC
reference and fetched instead. A bare number, an `rfcNNNN` token, or a URL
all work — the last path segment of a URL is parsed for its RFC number
(so `https://www.ietf.org/ietf-ftp/rfc/rfc6241.html` and
`https://www.rfc-editor.org/rfc/rfc6241.txt` both resolve to RFC 6241).
Regardless of which host or extension was given, the actual fetch always
goes to `https://www.rfc-editor.org/rfc/rfcNNNN.txt` — the canonical
fixed-width plain text — since that's the format this script parses; other
mirrors (like ietf.org's own `.html` rendering) don't serve that format.

## What it does

- **Depaginates** the source: splits on form feeds (or, if those were
  stripped, on the repeated `[Page N]` footer), removes the running
  header/footer on every page, and rejoins the text into one continuous
  document.
- **Classifies each block** of text as a heading, title, masthead, ordered/
  unordered list, bibliography reference, verbatim diagram, or plain prose
  paragraph, based on indentation and whitespace heuristics.
- **Reflows** prose paragraphs and list/reference items into single lines
  (undoing the fixed 72-column wrap), while leaving diagrams byte-for-byte
  as drawn.
- **Builds a table of contents** from the document's numbered section
  headings and drops the original ToC (its dotted leaders and page numbers
  are meaningless once the text is reflowed).
- **Links** bare `RFC 1234` / `[RFC1234]` mentions to rfc-editor.org and
  auto-linkifies URLs.
- Renders everything into one self-contained HTML file (fonts pulled from
  Google Fonts; no other external assets) with a sticky sidebar ToC and a
  print stylesheet.

## Heuristics

This is a heuristic converter, not a full RFC 7991/xml2rfc parser — it's
tuned to the common modern RFC plain-text layout and may need adjustment for
unusual or very old (pre-2000) RFC formatting.

- Body prose is indented; headings and the document title are not.
- A block is treated as a verbatim diagram if it contains alignment
  whitespace or a high density of ASCII-art characters, since that spacing
  is the content (this matters a lot for RFCs about diagram syntax, like
  RFC 8340 itself).
- The document's own "Table of Contents" section is dropped in favor of a
  generated one.

See the comments in `rfc2html.py` for the full detail on each heuristic.

## Requirements

- Python 3.8+ (standard library only)
