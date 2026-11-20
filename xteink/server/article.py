"""Pull the readable article out of a web page (pure stdlib, no network I/O).

:func:`extract` takes the HTML of a page and returns a small, clean HTML document that
holds only the article: paragraphs, headings, lists, quotes, code and links. Scripts,
navigation, forms, images and every attribute except a link's ``href`` are dropped, so the
result is safe to hand to ``pandoc`` (:mod:`xteink.core.ingest`) and reads well on e-ink.

How the article is found:

1. Elements that never hold article text are removed (``script``, ``nav``, ``aside``,
   ``form`` and so on), then small elements whose class or id names them as page furniture
   (comments, share bars, related links), then lists and boxes that are mostly links
   (menus, language pickers, category lists).
2. Every run of text outside a link gives its length to the block that contains it, and a
   share of it to that block's ancestors. Text inside links counts for nothing, so menus
   and link lists score low.
3. The best-scoring block wins. When it sits inside an ``<article>``, an
   ``itemprop="articleBody"`` element or ``<main>``, that element is used instead, so an
   article split over several sibling blocks stays whole. Otherwise its siblings with a
   comparable score are kept with it.

A page with too little text (a JavaScript shell, a login wall) raises
:class:`ArticleError` with code ``no_article``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from xteink.core.errors import CoreError

__all__ = ["Article", "ArticleError", "extract"]

MIN_ARTICLE_CHARS = 200

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
# Never article text: dropped with everything inside.
_DROP = frozenset(
    "script style noscript template svg canvas iframe form button input select textarea "
    "nav aside footer dialog menu video audio object embed map head".split()
)
_INLINE = frozenset(
    "a abbr b bdi bdo cite code data dfn em font i kbd mark q s samp small span strong sub "
    "sup time u var wbr ins del label big tt nobr".split()
)
# Blocks that hold a unit of text: their text scores for the block around them.
_TEXT_BLOCKS = frozenset("p pre blockquote li h1 h2 h3 h4 h5 h6 figcaption dd dt".split())
_HEADINGS = frozenset("h1 h2 h3 h4 h5 h6".split())
_INLINE_OUT = {
    "em": "em",
    "i": "em",
    "cite": "em",
    "strong": "strong",
    "b": "strong",
    "code": "code",
    "kbd": "code",
    "samp": "code",
    "tt": "code",
    "sub": "sub",
    "sup": "sup",
}
_SEMANTIC_ROOTS = ("articlebody", "article", "main")
_FURNITURE = re.compile(
    r"comment|sidebar|share|social|related|promo|newsletter|subscribe|cookie|banner|"
    r"advert|popup|modal|breadcrumb|pagination|pager|footer|masthead|toolbar|"
    r"recommend|outbrain|taboola|disqus|signup|paywall",
    re.IGNORECASE,
)
# An element named like furniture is only dropped when it is a small part of the page.
_FURNITURE_MAX_SHARE = 0.25
# A list or box with at least this many links, whose text is mostly link text, is a menu.
_LINK_BOX_TAGS = frozenset("ul ol dl div table section header span".split())
_LINK_BOX_MIN_LINKS = 3
_LINK_BOX_MIN_SHARE = 0.66
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_WS = re.compile(r"\s+")
_TITLE_SPLIT = re.compile(r"\s+[|–—·:-]\s+")
_BY = re.compile(r"^\s*by\s+", re.IGNORECASE)


class ArticleError(CoreError):
    """No readable article could be taken from the page. ``code`` is machine-readable."""

    code = "no_article"


@dataclass(frozen=True)
class Article:
    title: str
    author: str  # "" when the page names none
    site: str  # the site's name, else its host: stands in for a missing author
    html: str  # a complete UTF-8 HTML document holding only the article
    text_chars: int


@dataclass(eq=False)
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    parent: "_Node | None" = None
    children: list["_Node | str"] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """A forgiving tree: unknown end tags are ignored, unclosed tags close with a parent."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self._open = [self.root]
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
            return
        if tag == "title" and not self.title:
            self._in_title = True
            return
        if tag == "p":
            self._close_open("p", stop_at=_P_BARRIERS)
        elif tag == "li":
            self._close_open("li", stop_at=("ul", "ol"))
        node = _Node(tag, a, self._open[-1])
        self._open[-1].children.append(node)
        if tag not in _VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and tag not in ("meta", "title") and self._open[-1].tag == tag:
            self._open.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        self._open[-1].children.append(data)

    def _close_open(self, tag: str, *, stop_at: tuple[str, ...]) -> None:
        """Implied end tag: ``<p>`` closes an open ``<p>``, ``<li>`` an open ``<li>``."""
        for depth in range(len(self._open) - 1, 0, -1):
            open_tag = self._open[depth].tag
            if open_tag == tag:
                del self._open[depth:]
                return
            if open_tag in stop_at or open_tag not in _INLINE:
                return


_P_BARRIERS = ("div", "td", "th", "li", "blockquote", "article", "section", "body", "table")


def _walk(node: _Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in reversed(n.children) if isinstance(c, _Node))


def _text(node: _Node | str, *, links: bool = True) -> str:
    if isinstance(node, str):
        return node
    if not links and node.tag == "a":
        return ""
    return "".join(_text(c, links=links) for c in node.children)


def _chars(node: _Node, *, links: bool = True) -> int:
    return len(_WS.sub(" ", _text(node, links=links)).strip())


def _prune(root: _Node) -> None:
    """Drop elements that never hold article text, then small page furniture."""

    def hidden(n: _Node) -> bool:
        return (
            "hidden" in n.attrs
            or n.attrs.get("aria-hidden") == "true"
            or bool(_HIDDEN_STYLE.search(n.attrs.get("style", "")))
        )

    def strip(n: _Node, keep) -> None:
        n.children = [c for c in n.children if isinstance(c, str) or keep(c)]
        for c in n.children:
            if isinstance(c, _Node):
                strip(c, keep)

    strip(root, lambda c: c.tag not in _DROP and not hidden(c))
    total = max(_chars(root), 1)

    def keep_named(c: _Node) -> bool:
        if c.tag in ("html", "body", "article", "main"):
            return True
        name = f"{c.attrs.get('class', '')} {c.attrs.get('id', '')} {c.attrs.get('role', '')}"
        if not _FURNITURE.search(name):
            return True
        return _chars(c) / total > _FURNITURE_MAX_SHARE

    strip(root, keep_named)

    def keep_link_box(c: _Node) -> bool:
        if c.tag not in _LINK_BOX_TAGS:
            return True
        size = _chars(c)
        if size < 30 or (size - _chars(c, links=False)) / size <= _LINK_BOX_MIN_SHARE:
            return True
        return sum(1 for n in _walk(c) if n.tag == "a") < _LINK_BOX_MIN_LINKS

    strip(root, keep_link_box)


def _block_of(node: _Node) -> _Node:
    """The nearest ancestor (or the node itself) that is not an inline element."""
    while node.parent is not None and node.tag in _INLINE:
        node = node.parent
    return node


def _score(root: _Node) -> dict[_Node, float]:
    scores: dict[_Node, float] = {}

    def visit(node: _Node, in_link: bool) -> None:
        in_link = in_link or node.tag == "a"
        for child in node.children:
            if isinstance(child, _Node):
                visit(child, in_link)
                continue
            size = len(_WS.sub(" ", child).strip())
            if in_link or size < 20:
                continue
            block = _block_of(node)
            if block.tag in _TEXT_BLOCKS and block.parent is not None:
                block = block.parent
                while block.tag in _TEXT_BLOCKS | {"ul", "ol", "dl"} and block.parent is not None:
                    block = block.parent
            share = float(size)
            for _ in range(3):
                if block is None or block.tag == "#root":
                    break
                scores[block] = scores.get(block, 0.0) + share
                share /= 2
                block = block.parent  # type: ignore[assignment]

    visit(root, False)
    return scores


def _semantic_root(node: _Node) -> _Node | None:
    """The nearest articleBody, else article, else main that contains ``node``."""
    found: dict[str, _Node] = {}
    cur: _Node | None = node
    while cur is not None:
        if cur.attrs.get("itemprop", "").lower() == "articlebody":
            found.setdefault("articlebody", cur)
        elif cur.tag == "article":
            found.setdefault("article", cur)
        elif cur.tag == "main" or cur.attrs.get("role") == "main":
            found.setdefault("main", cur)
        cur = cur.parent
    for name in _SEMANTIC_ROOTS:
        if name in found:
            return found[name]
    return None


def _select(root: _Node) -> list[_Node]:
    scores = _score(root)
    if not scores:
        raise ArticleError("no article text found on the page")
    top = max(scores, key=lambda n: scores[n])
    semantic = _semantic_root(top)
    if semantic is not None:
        return [semantic]
    if top.parent is None or top.tag in ("body", "html"):
        return [top]
    threshold = max(scores[top] * 0.2, 40.0)
    picked = [
        c
        for c in top.parent.children
        if isinstance(c, _Node) and (c is top or scores.get(c, 0.0) >= threshold)
    ]
    return picked or [top]


class _Writer:
    """Serializes the chosen nodes as clean blocks. Only whitelisted tags are written."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.blocks: list[str] = []
        self._inline: list[str] = []

    # -- inline ---------------------------------------------------------------

    def _flush(self) -> None:
        html = "".join(self._inline)
        self._inline = []
        html = re.sub(r"^(?:\s|<br/>)+|(?:\s|<br/>)+$", "", html)
        if re.sub(r"<[^>]+>", "", html).strip():
            self.blocks.append(f"<p>{html}</p>")

    def _add_text(self, text: str) -> None:
        text = _WS.sub(" ", text)
        if not text:
            return
        if text == " " and (not self._inline or self._inline[-1].endswith((" ", "<br/>"))):
            return
        self._inline.append(escape(text, quote=False))

    def _href(self, raw: str) -> str:
        url = urljoin(self.base_url, raw.strip())
        return url if urlsplit(url).scheme in ("http", "https") else ""

    def _inline_node(self, node: _Node) -> None:
        if node.tag == "br":
            if self._inline and self._inline[-1] == "<br/>":
                self._inline.pop()
                self._flush()  # two line breaks in a row are a paragraph break
            elif self._inline:
                self._inline.append("<br/>")
            return
        out = _INLINE_OUT.get(node.tag)
        href = self._href(node.attrs.get("href", "")) if node.tag == "a" else ""
        if href:
            start, end = f'<a href="{escape(href, quote=True)}">', "</a>"
        elif out:
            start, end = f"<{out}>", f"</{out}>"
        else:
            start = end = ""
        mark = len(self._inline)
        if start:
            self._inline.append(start)
        self._children(node)
        if start:
            if len(self._inline) == mark + 1:
                self._inline.pop()  # nothing inside: write no empty tag
            elif len(self._inline) > mark:
                self._inline.append(end)
            # A paragraph break inside the tag flushed its opening: nothing left to close.

    # -- blocks ---------------------------------------------------------------

    def _nested(self, node: _Node) -> list[str]:
        inner = _Writer(self.base_url)
        inner._children(node)
        inner._flush()
        return inner.blocks

    def _block_node(self, node: _Node) -> None:
        tag = node.tag
        self._flush()
        if tag in _HEADINGS:
            text = escape(_WS.sub(" ", _text(node)).strip(), quote=False)
            if text:
                self.blocks.append(f"<{tag}>{text}</{tag}>")
        elif tag == "pre":
            code = escape(_text(node).strip("\n"), quote=False)
            if code.strip():
                self.blocks.append(f"<pre><code>{code}</code></pre>")
        elif tag == "blockquote":
            inner = self._nested(node)
            if inner:
                self.blocks.append("<blockquote>" + "".join(inner) + "</blockquote>")
        elif tag in ("ul", "ol"):
            items = []
            for child in node.children:
                if isinstance(child, _Node) and child.tag == "li":
                    inner = self._nested(child)
                    if len(inner) == 1 and inner[0].startswith("<p>"):
                        inner = [inner[0][3:-4]]
                    if inner:
                        items.append("<li>" + "".join(inner) + "</li>")
            if items:
                self.blocks.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
        elif tag == "hr":
            self.blocks.append("<hr/>")
        else:  # div, section, p, table cells, figures...: a paragraph boundary
            self._children(node)
            self._flush()

    def _children(self, node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                self._add_text(child)
            elif child.tag in _DROP or child.tag == "img":
                continue
            elif child.tag in _INLINE or child.tag == "br":
                self._inline_node(child)
            else:
                self._block_node(child)

    def write(self, nodes: list[_Node]) -> list[str]:
        for node in nodes:
            self._block_node(node)
        self._flush()
        return self.blocks


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text).strip().casefold()


def _title(builder: _TreeBuilder, root: _Node, url: str) -> str:
    for key in ("og:title", "twitter:title"):
        if builder.meta.get(key):
            return _WS.sub(" ", builder.meta[key]).strip()
    page = _WS.sub(" ", builder.title).strip()
    heading = next((n for n in _walk(root) if n.tag == "h1" and _chars(n) > 0), None)
    h1 = _WS.sub(" ", _text(heading)).strip() if heading else ""
    if h1 and (not page or _norm(h1) in _norm(page)):
        return h1
    if page:
        # "Article title | Site name": keep the longest part.
        parts = [p for p in _TITLE_SPLIT.split(page) if p.strip()]
        return max(parts, key=len).strip() if len(parts) > 1 else page
    if h1:
        return h1
    parts = urlsplit(url)
    return (parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.netloc or "Untitled")[:200]


def _author(builder: _TreeBuilder) -> str:
    for key in ("author", "article:author", "byl", "parsely-author", "dc.creator"):
        value = _WS.sub(" ", builder.meta.get(key, "")).strip()
        if value and not value.lower().startswith(("http://", "https://")):
            return _BY.sub("", value)[:200]
    return ""


def _site(builder: _TreeBuilder, url: str) -> str:
    """Where the page is from, for a page that names no author: the site name, else the host."""
    name = _WS.sub(" ", builder.meta.get("og:site_name", "")).strip()
    if name:
        return name[:200]
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def extract(html: str, url: str = "", *, min_chars: int = MIN_ARTICLE_CHARS) -> Article:
    """The article in ``html`` as a clean document. ``url`` resolves relative links."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    root = builder.root
    title = _title(builder, root, url)
    _prune(root)
    blocks = _Writer(url).write(_select(root))
    # The title is written from the metadata: drop a leading heading that repeats it.
    if blocks and re.match(r"<h[1-6]>", blocks[0]):
        if _norm(re.sub(r"<[^>]+>", "", blocks[0])) == _norm(escape(title, quote=False)):
            blocks = blocks[1:]
    text_chars = len(_WS.sub(" ", re.sub(r"<[^>]+>", " ", "".join(blocks))).strip())
    if text_chars < min_chars:
        raise ArticleError("the page has too little readable text (is it built by JavaScript?)")
    if urlsplit(url).scheme in ("http", "https"):
        host = escape(urlsplit(url).netloc, quote=False)
        blocks.append(f'<hr/><p>Source: <a href="{escape(url, quote=True)}">{host}</a></p>')
    document = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
        f"<title>{escape(title, quote=False)}</title></head>\n<body>\n"
        + "\n".join(blocks)
        + "\n</body></html>\n"
    )
    return Article(
        title=title,
        author=_author(builder),
        site=_site(builder, url),
        html=document,
        text_chars=text_chars,
    )
