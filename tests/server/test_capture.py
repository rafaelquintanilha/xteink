"""URL capture (xteink/server/capture.py): the fetch guards, then what each content type becomes."""

import gzip
import io
import socket
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xteink.core import LibraryService
from xteink.core.ingest import IngestError
from xteink.server import capture
from xteink.server.capture import CaptureError, Fetched, FetchLimits, fetch, resolve_public

PARA = "A sentence of ordinary prose that is long enough to count as article text. " * 4
PAGE = (
    "<html><head><title>Saved Page</title></head>"
    f"<body><article><p>{PARA}</p></article></body></html>"
)


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("mimetype", b"application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
    return buf.getvalue()


# --- which addresses may be fetched -------------------------------------------------


def _addrinfo(*ips):
    def fake(host, port, type=0):  # noqa: A002 - mirrors socket.getaddrinfo
        fam = lambda ip: socket.AF_INET6 if ":" in ip else socket.AF_INET  # noqa: E731
        return [(fam(ip), socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return fake


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.10",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT / tailnet
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:192.168.0.1",
    ],
)
def test_non_public_addresses_are_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo(ip))
    with pytest.raises(CaptureError) as exc:
        resolve_public("evil.example", 80)
    assert exc.value.code == "blocked_url"


def test_one_private_address_among_public_ones_blocks_the_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _addrinfo("93.184.216.34", "192.168.1.1"))
    with pytest.raises(CaptureError) as exc:
        resolve_public("mixed.example", 443)
    assert exc.value.code == "blocked_url"


def test_public_addresses_pass_and_are_deduplicated(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _addrinfo("93.184.216.34", "93.184.216.34", "2606:4700::1111")
    )
    assert resolve_public("ok.example", 443) == ["93.184.216.34", "2606:4700::1111"]


def test_unresolvable_host_is_a_fetch_failure(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(CaptureError) as exc:
        resolve_public("missing.example", 80)
    assert exc.value.code == "fetch_failed"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/a",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.org/no-scheme",
        "http://",
        "http://user:pw@example.org/",
        "http://example.org:notaport/",
    ],
)
def test_bad_urls_are_refused_before_any_lookup(url):
    def never(host, port):
        raise AssertionError("resolved a URL that should have been refused")

    with pytest.raises(CaptureError) as exc:
        fetch(url, resolve=never)
    assert exc.value.code == "bad_url"


# --- the fetch itself, against a local server (the guard is swapped out) --------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802 - http.server API
        status, headers, body = self.routes.get(self.path, (404, {}, b"nope"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).seen.append((self.path, dict(self.headers)))

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    handler = type("H", (_Handler,), {"routes": {}, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    handler.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield handler
    server.shutdown()
    server.server_close()


def local(host, port):
    return ["127.0.0.1"]


def test_fetch_follows_redirects_and_reports_the_final_url(site):
    site.routes["/a"] = (302, {"Location": "/b"}, b"")
    site.routes["/b"] = (200, {"Content-Type": "text/html; charset=ISO-8859-1"}, b"<p>hi</p>")
    got = fetch(site.base + "/a", resolve=local)
    assert got.url == site.base + "/b"
    assert (got.content_type, got.charset, got.data) == ("text/html", "iso-8859-1", b"<p>hi</p>")
    assert "xteink-capture" in site.seen[-1][1]["User-Agent"]


def test_every_redirect_hop_is_checked(site):
    site.routes["/a"] = (302, {"Location": "http://internal.example/secret"}, b"")
    hosts = []

    def guard(host, port):
        hosts.append(host)
        if host == "internal.example":
            raise CaptureError("the address is not public", "blocked_url")
        return ["127.0.0.1"]

    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/a", resolve=guard)
    assert exc.value.code == "blocked_url"
    assert hosts == ["127.0.0.1", "internal.example"]


def test_redirect_to_another_scheme_is_refused(site):
    site.routes["/a"] = (301, {"Location": "file:///etc/passwd"}, b"")
    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/a", resolve=local)
    assert exc.value.code == "bad_url"


def test_redirect_loop_stops(site):
    site.routes["/a"] = (302, {"Location": "/a"}, b"")
    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/a", resolve=local, limits=FetchLimits(max_redirects=3))
    assert "redirects" in str(exc.value)
    assert len(site.seen) == 4


def test_http_error_and_size_cap(site):
    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/missing", resolve=local)
    assert exc.value.code == "fetch_failed" and "404" in str(exc.value)

    site.routes["/big"] = (200, {"Content-Type": "text/html"}, b"x" * 5000)
    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/big", resolve=local, limits=FetchLimits(max_page_bytes=4999))
    assert exc.value.code == "too_large"
    assert (
        len(fetch(site.base + "/big", resolve=local, limits=FetchLimits(max_page_bytes=5000)).data)
        == 5000
    )


def test_gzip_is_decoded_and_a_gzip_bomb_is_capped(site):
    site.routes["/z"] = (
        200,
        {"Content-Type": "text/html", "Content-Encoding": "gzip"},
        gzip.compress(b"<p>zipped</p>"),
    )
    assert fetch(site.base + "/z", resolve=local).data == b"<p>zipped</p>"

    site.routes["/bomb"] = (
        200,
        {"Content-Type": "text/html", "Content-Encoding": "gzip"},
        gzip.compress(b"\0" * 3_000_000),
    )
    with pytest.raises(CaptureError) as exc:
        fetch(site.base + "/bomb", resolve=local, limits=FetchLimits(max_page_bytes=100_000))
    assert exc.value.code == "too_large"


def test_an_epub_link_may_be_larger_than_a_page(site):
    site.routes["/book.epub"] = (200, {"Content-Type": "application/octet-stream"}, b"e" * 3000)
    limits = FetchLimits(max_page_bytes=1000)
    assert len(fetch(site.base + "/book.epub", resolve=local, limits=limits).data) == 3000
    with pytest.raises(CaptureError):
        fetch(site.base + "/book.epub", resolve=local, limits=limits, max_file_bytes=2999)


def test_nothing_listening_is_a_fetch_failure():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(CaptureError) as exc:
        fetch(f"http://127.0.0.1:{port}/", resolve=local)
    assert exc.value.code == "fetch_failed"


# --- what each kind of content becomes ----------------------------------------------


@pytest.fixture
def library(tmp_path):
    from xteink.core import Store

    return LibraryService(Store(tmp_path / "data"))


def serve(url_out, data, ctype, charset=""):
    return lambda url: Fetched(url=url_out, data=data, content_type=ctype, charset=charset)


def test_html_page_becomes_an_article_epub(library, fake_pandoc_log):
    r = capture.capture_and_add(
        library,
        "https://example.org/p",
        fetcher=serve("https://example.org/p", PAGE.encode(), "text/html"),
    )
    assert r.created
    assert (r.item.title, r.item.kind, r.item.format) == ("Saved Page", "article", "epub")
    argv, meta, source = fake_pandoc_log()
    assert "-f html" in argv and "--sandbox" in argv
    assert '"title": "Saved Page"' in meta
    assert "ordinary prose" in source and "Source: <a" in source


def test_given_title_author_and_kind_win(library, fake_pandoc_log):
    r = capture.capture_and_add(
        library,
        "https://example.org/p",
        title="Mine",
        author="Me",
        kind="newspaper",
        fetcher=serve("https://example.org/p", PAGE.encode(), "text/html"),
    )
    assert (r.item.title, r.item.author, r.item.kind) == ("Mine", "Me", "newspaper")


def test_charset_comes_from_the_header_then_the_meta_tag(library, fake_pandoc_log):
    text = PARA + "café naïve"
    latin = (
        f"<html><head><title>T</title></head><body><article><p>{text}</p></article></body></html>"
    )
    capture.capture_and_add(
        library,
        "https://example.org/a",
        fetcher=serve("https://example.org/a", latin.encode("latin-1"), "text/html", "iso-8859-1"),
    )
    assert "café naïve" in fake_pandoc_log()[2]

    meta = latin.replace("<head>", '<head><meta charset="windows-1252">') + " "
    capture.capture_and_add(
        library,
        "https://example.org/b",
        fetcher=serve("https://example.org/b", meta.encode("cp1252"), "text/html"),
    )
    assert "café naïve" in fake_pandoc_log()[2]


def test_epub_markdown_and_text_links_are_ingested_as_files(library, fake_pandoc_log):
    epub = capture.capture_and_add(
        library,
        "https://example.org/dl?id=1",
        fetcher=serve(
            "https://example.org/files/My%20Book.epub", make_epub(), "application/octet-stream"
        ),
    )
    assert (epub.item.format, epub.item.kind) == ("epub", "book")

    md = capture.capture_and_add(
        library,
        "https://example.org/raw/notes.md",
        fetcher=serve("https://example.org/raw/notes.md", b"# Notes\n\nBody.", "text/plain"),
    )
    assert (md.item.format, md.item.kind, md.item.title) == ("epub", "article", "notes")
    assert "-f markdown" in fake_pandoc_log()[0]

    txt = capture.capture_and_add(
        library,
        "https://example.org/essay",
        fetcher=serve("https://example.org/essay", b"plain words", "text/plain"),
    )
    assert (txt.item.format, txt.item.kind) == ("txt", "article")


@pytest.mark.parametrize(
    ("data", "ctype", "code"),
    [
        (b"%PDF-1.7", "application/pdf", "unsupported_content"),
        (b"\x89PNG", "image/png", "unsupported_content"),
        (b"<html><body><div id='app'></div></body></html>", "text/html", "no_article"),
        (b"not a zip", "application/epub+zip", "bad_magic"),
    ],
)
def test_refusals_carry_a_code(library, data, ctype, code):
    with pytest.raises(IngestError) as exc:
        capture.capture_and_add(
            library, "https://example.org/x", fetcher=serve("https://example.org/x", data, ctype)
        )
    assert exc.value.code == code
    assert library.list() == []


def test_default_fetcher_is_the_guarded_fetch(library, monkeypatch):
    seen = {}

    def fake_fetch(url, **kw):
        seen.update(url=url, **kw)
        raise CaptureError("stop here", "blocked_url")

    monkeypatch.setattr(capture, "fetch", fake_fetch)
    with pytest.raises(CaptureError):
        capture.capture_and_add(library, "http://192.168.1.1/admin")
    assert seen["url"] == "http://192.168.1.1/admin"
    assert "resolve" not in seen  # the public-address guard is never swapped out here
