"""Save a web page to the library: a guarded fetch, then extract and ingest.

``POST /api/library/url`` and the MCP tool ``save_url`` end here. The server fetches a URL
on behalf of a caller, so the fetch is the dangerous part and is kept narrow:

* ``http`` and ``https`` only, no credentials in the URL.
* The host must resolve to public addresses only. One private, loopback, link-local or
  otherwise non-global address refuses the whole request (``blocked_url``), so the fetch
  cannot be pointed at the LAN, at this server or at a cloud metadata address.
* The connection goes to the address that was checked (no second DNS lookup), with the
  certificate still verified against the host name.
* Redirects are followed by hand, at most :attr:`FetchLimits.max_redirects`, and every hop
  is checked the same way.
* The body is read up to a size cap and under one deadline for the whole fetch.

What comes back is handed on by type: an HTML page goes through
:func:`xteink.server.article.extract` and becomes an article EPUB; an EPUB, Markdown or plain
text file is ingested as it is. Anything else is refused (``unsupported_content``).
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from xteink.core.ingest import DEFAULT_LIMITS, IngestError, IngestLimits, ingest
from xteink.core.library import LibraryService
from xteink.core.models import AddResult

from .article import ArticleError, extract

__all__ = ["CaptureError", "FetchLimits", "Fetched", "capture_and_add", "fetch", "resolve_public"]

USER_AGENT = "Mozilla/5.0 (compatible; xteink-capture/1; +reader)"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_EPUB_TYPES = frozenset({"application/epub+zip"})
_MARKDOWN_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE)
_CHUNK = 64 * 1024


class CaptureError(IngestError):
    """A URL could not be saved. ``code`` is machine-readable."""

    code = "fetch_failed"


@dataclass(frozen=True)
class FetchLimits:
    max_page_bytes: int = 5 * 1024 * 1024  # an HTML, Markdown or text page
    timeout_s: float = 20.0  # the whole fetch, redirects included
    max_redirects: int = 5


DEFAULT_FETCH_LIMITS = FetchLimits()


@dataclass(frozen=True)
class Fetched:
    url: str  # after redirects
    data: bytes
    content_type: str  # lower case, without parameters
    charset: str  # from the Content-Type header, "" when absent


Resolver = Callable[[str, int], list[str]]
Fetcher = Callable[[str], Fetched]


def resolve_public(host: str, port: int) -> list[str]:
    """The addresses of ``host``, all of them public. Raises ``blocked_url`` otherwise."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise CaptureError(f"cannot resolve {host}", "fetch_failed") from exc
    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:  # a scoped address such as fe80::1%en0
            raise CaptureError("the address is not public", "blocked_url") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            raise CaptureError("the address is not public", "blocked_url")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise CaptureError(f"cannot resolve {host}", "fetch_failed")
    return addresses


def _split(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path-with-query) of a URL this module may fetch."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise CaptureError("not a valid URL", "bad_url") from None
    if parts.scheme not in ("http", "https"):
        raise CaptureError("only http and https URLs can be saved", "bad_url")
    if not parts.hostname:
        raise CaptureError("the URL has no host", "bad_url")
    if parts.username is not None or parts.password is not None:
        raise CaptureError("URLs with credentials are not accepted", "bad_url")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80), path


def _open_socket(ips: list[str], port: int, timeout: float) -> socket.socket:
    """A TCP connection to the first of the checked addresses that answers."""
    error: OSError | None = None
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as exc:
            error = exc
    raise error or OSError("no address to connect to")


def _connect(scheme: str, host: str, port: int, ips: list[str], timeout: float):
    """An HTTP(S) connection to a checked address that still speaks to (and verifies) ``host``."""
    raw = _open_socket(ips, port, timeout)
    if scheme == "http":
        conn: http.client.HTTPConnection = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = raw
        return conn
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        conn.sock = context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    return conn


def _read_capped(resp: http.client.HTTPResponse, cap: int, deadline: float) -> bytes:
    encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
    inflate = None
    if encoding in ("gzip", "x-gzip", "deflate"):
        inflate = zlib.decompressobj(zlib.MAX_WBITS | (16 if "gzip" in encoding else 32))
    elif encoding not in ("", "identity"):
        raise CaptureError(f"unsupported content encoding {encoding}", "unsupported_content")
    out = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise CaptureError("the page took too long to download", "fetch_timeout")
        chunk = resp.read(_CHUNK)
        if not chunk:
            break
        if inflate is not None:
            # max_length bounds what one compressed chunk may expand to (a gzip bomb).
            chunk = inflate.decompress(chunk, cap + 1 - len(out))
            if inflate.unconsumed_tail:
                raise CaptureError("the page exceeds the size limit", "too_large")
        out += chunk
        if len(out) > cap:
            raise CaptureError("the page exceeds the size limit", "too_large")
    return bytes(out)


def _is_epub_url(path: str) -> bool:
    return path.split("?", 1)[0].lower().endswith(".epub")


def fetch(
    url: str,
    *,
    limits: FetchLimits = DEFAULT_FETCH_LIMITS,
    max_file_bytes: int = DEFAULT_LIMITS.max_bytes,
    resolve: Resolver = resolve_public,
) -> Fetched:
    """GET ``url`` under the guards in the module docstring."""
    deadline = time.monotonic() + limits.timeout_s
    for _ in range(limits.max_redirects + 1):
        scheme, host, port, path = _split(url)
        ips = resolve(host, port)
        left = deadline - time.monotonic()
        if left <= 0:
            raise CaptureError("the page took too long to download", "fetch_timeout")
        conn = None
        try:
            conn = _connect(scheme, host, port, ips, left)
            conn.request(
                "GET",
                path,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/epub+zip,text/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                    "Accept-Language": "en,*;q=0.5",
                },
            )
            resp = conn.getresponse()
            if resp.status in _REDIRECTS:
                location = resp.getheader("Location")
                if not location:
                    raise CaptureError("redirect without a location", "fetch_failed")
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                raise CaptureError(f"the site answered HTTP {resp.status}", "fetch_failed")
            ctype = resp.headers.get_content_type().lower()
            charset = (resp.headers.get_content_charset() or "").lower()
            is_file = ctype in _EPUB_TYPES or _is_epub_url(path)
            cap = max_file_bytes if is_file else limits.max_page_bytes
            data = _read_capped(resp, cap, deadline)
            return Fetched(url=url, data=data, content_type=ctype, charset=charset)
        except CaptureError:
            raise
        except TimeoutError as exc:
            raise CaptureError("the site did not answer in time", "fetch_timeout") from exc
        except ssl.SSLError as exc:
            raise CaptureError("the site's certificate could not be verified") from exc
        except (OSError, http.client.HTTPException, zlib.error) as exc:
            raise CaptureError(f"could not fetch the page ({type(exc).__name__})") from exc
        finally:
            if conn is not None:
                conn.close()
    raise CaptureError("too many redirects", "fetch_failed")


def _decode(page: Fetched) -> str:
    candidates = [page.charset]
    sniffed = _META_CHARSET.search(page.data[:4096])
    if sniffed:
        candidates.append(sniffed.group(1).decode("ascii", "replace").lower())
    candidates.append("utf-8")
    for name in candidates:
        if not name:
            continue
        try:
            return page.data.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return page.data.decode("utf-8", "replace")


def _file_name(url: str, fallback: str) -> str:
    name = os.path.basename(urlsplit(url).path)
    return name if os.path.splitext(name)[1] else fallback


def capture_and_add(
    library: LibraryService,
    url: str,
    *,
    title: str | None = None,
    author: str = "",
    kind: str | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
    fetcher: Fetcher | None = None,
) -> AddResult:
    """Fetch ``url`` and store what it holds. Raises :class:`IngestError` on refusal."""
    page = (fetcher or (lambda u: fetch(u, max_file_bytes=limits.max_bytes)))(url)
    path = urlsplit(page.url).path.lower()
    given_title = (title or "").strip() or None

    if page.content_type in _EPUB_TYPES or path.endswith(".epub"):
        data, filename = page.data, _file_name(page.url, "book.epub")
        if not filename.lower().endswith(".epub"):
            filename += ".epub"
        default_kind = None  # ingest's own default for the format
    elif page.content_type in _HTML_TYPES:
        try:
            article = extract(_decode(page), page.url)
        except ArticleError as exc:
            raise CaptureError(str(exc), exc.code) from exc
        data, filename = article.html.encode("utf-8"), "article.html"
        given_title = given_title or article.title
        author = author or article.author
        default_kind = "article"
    elif page.content_type in _MARKDOWN_TYPES or path.endswith((".md", ".markdown")):
        data = _decode(page).encode("utf-8")
        filename, default_kind = _file_name(page.url, "article.md"), "article"
    elif page.content_type == "text/plain":
        data = _decode(page).encode("utf-8")
        filename = os.path.splitext(_file_name(page.url, "article.txt"))[0] + ".txt"
        default_kind = "article"
    else:
        raise CaptureError(
            f"cannot save content of type {page.content_type or 'unknown'}", "unsupported_content"
        )

    prepared = ingest(
        data,
        filename=filename,
        title=given_title,
        author=author,
        kind=kind or default_kind,
        limits=limits,
    )
    return library.add(
        prepared.data,
        title=prepared.title,
        kind=prepared.kind,
        format=prepared.format,
        author=prepared.author,
    )
