"""Link and image extraction from Markdown/MDX/reStructuredText/HTML.

Every extraction returns (url, line_number) with 1-based line numbers.
Only remote http(s) URLs are returned; everything else
(mailto:, ftp:, relative paths, anchors, protocol-relative) is ignored.
"""

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
    return url


def _add(out, url, line):
    url = _clean(url)
    if not _is_remote_http(url):
        return
    # trim surrounding markdown/rst punctuation that is not part of the URL
    url = url.rstrip(").,;:!?")
    if not _is_remote_http(url):
        return
    out.append((url, line))


def extract_markdown(text: str):
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
