"""Section Markdown as page HTML: a small converter for headings, lists, emphasis, code, links and tables.

Every piece of text is HTML-escaped, so raw HTML in a section shows as text and never reaches the
page. Only http, https and mailto links become links, and a bare http(s) address links itself; a link with any other scheme stays as its
literal text. Images become links to the image (pages load no external resources). Not CommonMark:
no setext headings, reference links or HTML blocks.
"""

import html
import re

_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})(?:\s+(.*?))?(?:\s+#+)?\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
TOP_LEVEL_ITEM = re.compile(r"^(?:[-*+]|\d{1,9}[.)])\s+\S")
_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")

_CODE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)|<((?:https?://|mailto:)[^>\s]+)>")
_EMPHASIS = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"<strong>\1</strong>"),
    (re.compile(r"\*(?=\S)(.+?)(?<=\S)\*"), r"<em>\1</em>"),
    (re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"), r"<em>\1</em>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"<del>\1</del>"),
)
_BARE = re.compile(r"(?<![\w/\[<\"'=])(?<!\]\()https?://[^\s<>\"'\[\]`]+", re.IGNORECASE)
_TRAILING = ".,;:!?*_~"
SCHEMES = ("http://", "https://", "mailto:")


def escape(text):
    return html.escape(text, quote=True)


def safe_url(url):
    """The URL when its scheme may become a link (http, https, mailto), else None."""
    return url if url and url.strip().lower().startswith(SCHEMES) else None


def inline(text):
    """One run of text with code spans, links and emphasis, escaped everywhere else."""
    parts, last = [], 0
    for match in _CODE.finditer(text):
        parts.append(_links(text[last:match.start()]))
        parts.append(f"<code>{escape(match.group(2).strip())}</code>")
        last = match.end()
    parts.append(_links(text[last:]))
    return "".join(parts)


def _links(text):
    parts, last = [], 0
    for match in _LINK.finditer(text):
        parts.append(_bare(text[last:match.start()]))
        label, url = (match.group(1), match.group(2)) if match.group(3) is None else (match.group(3), match.group(3))
        if safe_url(url):
            tab = ' target="_blank" rel="noopener"' if url.lower().startswith(("http://", "https://")) else ""
            parts.append(f'<a href="{escape(url)}"{tab}>{_emphasis(label) or escape(url)}</a>')
        else:
            parts.append(escape(match.group(0)))  # a scheme that may not link stays as its literal text
        last = match.end()
    parts.append(_bare(text[last:]))
    return "".join(parts)


def _bare(text):
    """Bare http(s) addresses become links; sentence punctuation and an unmatched ) stay outside."""
    parts, last = [], 0
    for match in _BARE.finditer(text):
        url = match.group(0)
        while url and (url[-1] in _TRAILING or (url[-1] == ")" and url.count(")") > url.count("("))):
            url = url[:-1]
        if "://" not in url or url.endswith("://"):
            continue
        parts.append(_emphasis(text[last:match.start()]))
        parts.append(f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(url)}</a>')
        last = match.start() + len(url)
    parts.append(_emphasis(text[last:]))
    return "".join(parts)


def _emphasis(text):
    out = escape(text)
    for pattern, replacement in _EMPHASIS:
        out = pattern.sub(replacement, out)
    return out


def to_html(text, heading_offset=2):
    """A section body as HTML blocks. Headings move down by `heading_offset` levels (at most h6), so a
    section's own `#` sits below the page's section heading."""
    return "".join(_blocks(text.replace("\r\n", "\n").split("\n"), heading_offset))


def _blocks(lines, offset):
    out, i, paragraph = [], 0, []

    def flush():
        if paragraph:
            out.append(f"<p>{inline(chr(10).join(line.strip() for line in paragraph))}</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
        elif fence := _FENCE.match(line):
            flush()
            i = _code_block(lines, i, fence.group(1), out)
        elif heading := _HEADING.match(line):
            flush()
            level = min(len(heading.group(1)) + offset, 6)
            out.append(f"<h{level}>{inline(heading.group(2) or '')}</h{level}>")
            i += 1
        elif _RULE.match(line):
            flush()
            out.append("<hr>")
            i += 1
        elif "|" in line and i + 1 < len(lines) and _DELIMITER.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            i = _table(lines, i, out)
        elif _ITEM.match(line):
            flush()
            i = _list(lines, i, out)
        elif _QUOTE.match(line):
            flush()
            quoted = []
            while i < len(lines) and (match := _QUOTE.match(lines[i])):
                quoted.append(match.group(1))
                i += 1
            out.append(f"<blockquote>{''.join(_blocks(quoted, offset))}</blockquote>")
        else:
            paragraph.append(line)
            i += 1
    flush()
    return out


def _code_block(lines, i, marker, out):
    body, i = [], i + 1
    while i < len(lines) and not (lines[i].strip().startswith(marker) and not lines[i].strip().strip(marker[0])):
        body.append(lines[i])
        i += 1
    out.append(f"<pre><code>{escape(chr(10).join(body))}</code></pre>")
    return i + 1


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _table(lines, i, out):
    header = _cells(lines[i])
    rows, i = [], i + 2
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        cells = _cells(lines[i])
        rows.append((cells + [""] * len(header))[:len(header)])
        i += 1
    head = "".join(f"<th>{inline(cell)}</th>" for cell in header)
    body = "".join("<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    out.append(f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
    return i


def _list(lines, i, out):
    """A run of list items (nested by indent), each item's lazy continuation lines folded into it."""
    entries = []  # [indent, ordered, text, number (a numbered item's own number, else None)]
    while i < len(lines):
        line = lines[i]
        item = _ITEM.match(line)
        if item:
            number = int(item.group(2)[:-1]) if item.group(2)[0].isdigit() else None
            entries.append([len(item.group(1).expandtabs(4)), number is not None, item.group(3), number])
        elif line.strip() and not _starts_block(line):
            entries[-1][2] += "\n" + line.strip()
        elif not line.strip() and i + 1 < len(lines) and (
                _ITEM.match(lines[i + 1]) or (lines[i + 1][:1].isspace() and lines[i + 1].strip())):
            pass  # a blank line inside the list
        else:
            break
        i += 1
    at = 0
    while at < len(entries):  # a bullet list and a numbered one side by side are two lists
        html_list, at = _nest(entries, at)
        out.append(html_list)
    return i


def _starts_block(line):
    return bool(_FENCE.match(line) or _HEADING.match(line) or _QUOTE.match(line) or (
        _RULE.match(line) and not line[:1].isspace()))


def _nest(entries, i):
    indent, ordered, number = entries[i][0], entries[i][1], entries[i][3]
    tag = "ol" if ordered else "ul"
    # A numbered list keeps the number it starts at in the source. A paragraph between two items ends one
    # list and starts another, and without this the second would renumber from 1, so "1. … 2. …" read as
    # "1. … 1. …" on the page.
    start = f' start="{number}"' if ordered and number != 1 else ""
    items = []
    while i < len(entries) and entries[i][0] >= indent and not (entries[i][0] == indent and entries[i][1] != ordered):
        text = inline(entries[i][2])
        i += 1
        while i < len(entries) and entries[i][0] > indent:
            nested, i = _nest(entries, i)
            text += nested
        items.append(f"<li>{text}</li>")
    return f"<{tag}{start}>{''.join(items)}</{tag}>", i
