"""Link and image extraction from Markdown/MDX/reStructuredText/HTML.

Every extraction returns (url, line_number) with 1-based line numbers.
Only remote http(s) URLs are returned; everything else
(mailto:, ftp:, relative paths, anchors, protocol-relative) is ignored.
"""

import html
import re
from urllib.parse import urlsplit

MAX_URL_LEN = 2048

# --- Markdown --------------------------------------------------------------

_MD_INLINE = re.compile(
    r"!?\\?\[([^\]\n]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
_MD_REFERENCE_DEF = re.compile(
    r"^\s{0,3}\[([^\]\n]+)\]:\s*(<[^>]*>|[^)\s]+)", re.MULTILINE
)
_MD_ANGLE_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_MD_BARE_AUTOLINK = re.compile(
    r"(?<![\w<\"'=\]\(])(https?://[^\s<>\"'`)\]]+)"
)
_MDX_HTML_ATTR = re.compile(
    r"(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)

# --- reStructuredText ------------------------------------------------------

_RST_ROLE_TARGET = re.compile(
    r"(?:`[^`<>]*`)(?:[a-zA-Z0-9_:+.-]*)?:"
    r"(?:ref|doc|download|image|target|numref|math|external):"
    r"`[^`<]*?(?:<([^`]+)>)?`"
)
_RST_LINK = re.compile(r"`([^`<>]*)(?:<([^`<>]+)>)?`_")
_RST_NAMED_LINK = re.compile(r"`([^`<>]+)<([^`<>]+)>`__?")
_RST_ANON = re.compile(r"`([^`<>]+)`__")
_RST_DIRECTIVE_IMAGE = re.compile(
    r"^\s*\.\.\s+(?:image|figure)::\s*(\S+)", re.MULTILINE
)
_RST_TARGET = re.compile(r"^\s*\.\.\s+_(?:[^:\s]+):\s*(\S+)", re.MULTILINE)
_RST_BARE = re.compile(
    r"(?<![\"'=\w\`])(https?://[^\s<>\"'`)\]]+[^\s<>\"'`)\].,;:!?])"
)

# --- HTML ------------------------------------------------------------------

_HTML_ATTR = re.compile(
    r"(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_HTML_META_REFRESH = re.compile(
    r"url\s*=\s*(https?://[^\"'>\s]+)", re.IGNORECASE
)

_RST_CODE_DIRECTIVE = re.compile(r"^\s*\.\.\s+(?:code-block|sourcecode)::")


def _strip_rst_literal_blocks(text: str) -> str:
    """Blank indented literal blocks in reStructuredText.

    A paragraph ending in ``::`` and ``code-block``/``sourcecode``
    directives make the following indented block literal source; URLs
    inside it are never rendered links, so live-checking them produced
    false "broken" verdicts (same false-positive class as fenced Markdown
    code, v1.2.0). Content lines are replaced by empty lines, keeping
    reported line numbers exact. Deliberately conservative: rendered
    directives like ``.. note::`` do NOT trigger stripping — their links
    are real.
    """
    lines = text.split("\n")
    blanked = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].rstrip()
        if _RST_CODE_DIRECTIVE.match(lines[i]):
            pass
        elif (stripped.endswith("::")
              and len(stripped) > 2
              and not stripped.lstrip().startswith(".. ")):
            pass
        else:
            i += 1
            continue
        i += 1
        while i < n and (lines[i].strip() == "" or lines[i][:1] in (" ", "\t")):
            blanked.append(i)
            i += 1
    for idx in blanked:
        lines[idx] = ""
    return "\n".join(lines)


def _strip_html_comments(text: str) -> str:
    """Blank HTML comments (``<!-- ... -->``) in MDX/HTML text.

    Comment content is never rendered, so href/src inside comments must
    not be live-checked. Each comment is replaced by same-length
    whitespace preserving every newline, so line numbers stay exact.
    """
    def _blank(m):
        return "".join(c if c == "\n" else " " for c in m.group(0))

    return re.sub(r"(?s)<!--.*?-->", _blank, text)


def _is_remote_http(url: str) -> bool:
    if not url or len(url) > MAX_URL_LEN:
        return False
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    # reject obviously malformed netlocs (spaces, control chars)
    if any(ord(c) < 0x21 for c in parts.netloc):
        return False
    return True


def _clean(url: str):
    url = url.strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1]
    # Browsers decode HTML entities (&amp;, &#x3D;, &#38;, etc.) in attribute
    # and inline URLs before dispatching the HTTP request; mirror that so the
    # URL checker checks the real endpoint rather than a mangled entity literal.
    return html.unescape(url)


def _add(out, url, line):
    url = _clean(url)
    if not _is_remote_http(url):
        return
    # trim surrounding markdown/rst punctuation that is not part of the URL
    url = url.rstrip(").,;:!?")
    if not _is_remote_http(url):
        return
    out.append((url, line))


_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})", re.MULTILINE)


def _strip_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans in Markdown/MDX.

    Code examples are not rendered, so live-checking the URLs inside them
    only produces false "broken" verdicts in customer reports — the same
    false-positive class as entity-encoded URLs (fixed in v1.1.0). This
    mirrors the open-source scanner's ``strip_code``.

    Only newlines matter for provenance: every removed line is replaced by
    an empty line, so reported line numbers stay exact. Per CommonMark, an
    unclosed fence extends to the end of the document. Applied to the
    Markdown extractor only: backticks are ordinary link syntax in RST.
    """
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        marker = m.group(1)
        close = re.compile(r"^[ \t]{0,3}" + re.escape(marker[0]) +
                           "{%d,}[ \t]*$" % len(marker))
        out.append("")          # the opening fence line
        i += 1
        while i < n and not close.match(lines[i]):
            out.append("")      # every content line keeps its position
            i += 1
        if i < n:
            out.append("")      # the closing fence line
            i += 1
    stripped = "\n".join(out)

    def _blank_span(m):
        return "".join(c if c == "\n" else " " for c in m.group(0))

    return re.sub(r"(?s)(`+).*?\1", _blank_span, stripped)


def extract_markdown(text: str):
    text = _strip_code(text)
    text = _strip_html_comments(text)

    out = []
    for m in _MD_INLINE.finditer(text):
        _add(out, m.group(2), _line_of(text, m.start(2)))
    for m in _MD_REFERENCE_DEF.finditer(text):
        _add(out, m.group(2), _line_of(text, m.start(2)))
    for m in _MD_ANGLE_AUTOLINK.finditer(text):
        _add(out, m.group(1), _line_of(text, m.start(1)))
    for m in _MDX_HTML_ATTR.finditer(text):
        url = next(g for g in m.groups() if g is not None)
        _add(out, url, _line_of(text, m.start()))
    for m in _MD_BARE_AUTOLINK.finditer(text):
        _add(out, m.group(1), _line_of(text, m.start(1)))
    return out


def extract_rst(text: str):
    """Extract (url, line) from RST.

    Patterns run in priority order; text spans already consumed by a
    higher-priority match are not re-scanned by looser patterns, so a
    directive URL is never reported twice (e.g. ``.. image::`` vs the bare
    URL rule).
    """
    out = []
    text = _strip_rst_literal_blocks(text)
    text = _strip_html_comments(text)
    claimed = []  # (start, end) spans consumed by higher-priority rules

    def _claimed(span):
        s, e = span
        return any(s < ce and cs < e for cs, ce in claimed)

    for pattern in (_RST_NAMED_LINK, _RST_ROLE_TARGET, _RST_ANON, _RST_LINK,
                    _RST_DIRECTIVE_IMAGE, _RST_TARGET, _RST_BARE):
        for m in pattern.finditer(text):
            if _claimed(m.span()):
                continue
            url = None
            url_group = 1
            for idx, g in enumerate(m.groups(), start=1):
                if g is not None:
                    url = g
                    url_group = idx
                    break
            if url:
                if pattern in (_RST_DIRECTIVE_IMAGE, _RST_TARGET):
                    # whole directive/target line owns its URL
                    claimed.append(m.span())
                else:
                    claimed.append(m.span(url_group))
                _add(out, url, _line_of(text, m.start()))
    return out


def extract_html(text: str):
    text = _strip_html_comments(text)
    out = []
    for m in _HTML_ATTR.finditer(text):
        url = next(g for g in m.groups() if g is not None)
        _add(out, url, _line_of(text, m.start()))
    for m in _HTML_META_REFRESH.finditer(text):
        _add(out, m.group(1), _line_of(text, m.start(1)))
    return out


_EXTRACTORS = {
    ".md": extract_markdown,
    ".markdown": extract_markdown,
    ".mdx": extract_markdown,
    ".rst": extract_rst,
    ".html": extract_html,
    ".htm": extract_html,
}


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def extract_links(relpath: str, text: str):
    """Return list of (url, line) for a documentation file."""
    lower = relpath.lower()
    for ext, extractor in _EXTRACTORS.items():
        if lower.endswith(ext):
            return extractor(text)
    return []
