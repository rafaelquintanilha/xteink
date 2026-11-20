"""``POST /api/library/url``: save a page by its address, optionally queueing it for a reader."""

import pytest

from xteink.core import DeviceService
from xteink.server import capture
from xteink.server.capture import CaptureError, Fetched

from .conftest import bearer

pytest.importorskip("fastapi")

PARA = "A sentence of ordinary prose that is long enough to count as article text. " * 4
PAGE = (
    "<html><head><title>Saved Page</title><meta name='author' content='Ada'></head>"
    f"<body><article><p>{PARA}</p></article></body></html>"
)


@pytest.fixture
def h(api_key):
    return bearer(api_key)


@pytest.fixture
def web(monkeypatch, fake_pandoc_log):
    """Stands in for the internet: maps a URL to (bytes, content type) or to an error."""
    pages: dict = {"https://example.org/post": (PAGE.encode(), "text/html")}
    calls: list[str] = []

    def fake_fetch(url, **kw):
        calls.append(url)
        got = pages.get(url)
        if got is None:
            raise CaptureError("the site answered HTTP 404", "fetch_failed")
        if isinstance(got, Exception):
            raise got
        return Fetched(url=url, data=got[0], content_type=got[1], charset="")

    monkeypatch.setattr(capture, "fetch", fake_fetch)
    pages["calls"] = calls
    return pages


def save(client, h, **body):
    return client.post("/api/library/url", json=body, headers=h)


def test_save_url_adds_an_article(main_client, h, web):
    r = save(main_client, h, url="https://example.org/post")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and "queued" not in body
    item = body["item"]
    assert (item["title"], item["author"], item["kind"], item["format"]) == (
        "Saved Page",
        "Ada",
        "article",
        "epub",
    )
    listed = main_client.get("/api/library?kind=article", headers=h).json()["items"]
    assert [i["id"] for i in listed] == [item["id"]]


def test_save_url_with_a_device_queues_it_by_name_or_id(main_client, h, web, store):
    device, _ = DeviceService(store).register("x4")
    r = save(main_client, h, url="https://example.org/post", device="x4", title="Mine")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item"]["title"] == "Mine"
    assert body["queued"]["item_id"] == body["item"]["id"]
    queue = main_client.get(f"/api/devices/{device.id}/queue", headers=h).json()["entries"]
    assert [e["item_id"] for e in queue] == [body["item"]["id"]]

    # The same page again: the conversion is repeatable, so it is the same item, not a copy.
    again = save(main_client, h, url="https://example.org/post", device=device.id, title="Mine")
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["item"]["id"] == body["item"]["id"]
    assert again.json()["queued"]["device_id"] == device.id
    assert len(main_client.get("/api/library", headers=h).json()["items"]) == 1


def test_unknown_device_is_refused_before_anything_is_fetched(main_client, h, web):
    r = save(main_client, h, url="https://example.org/post", device="nope")
    assert r.status_code == 404
    assert web["calls"] == []
    assert main_client.get("/api/library", headers=h).json()["items"] == []


def test_ambiguous_device_name_is_refused(main_client, h, web, store):
    DeviceService(store).register("twin")
    DeviceService(store).register("twin")
    assert save(main_client, h, url="https://example.org/post", device="twin").status_code == 422
    assert web["calls"] == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (CaptureError("the address is not public", "blocked_url"), 422),
        (CaptureError("not a valid URL", "bad_url"), 422),
        (CaptureError("the page exceeds the size limit", "too_large"), 413),
        (CaptureError("cannot save content of type image/png", "unsupported_content"), 415),
        (CaptureError("the site answered HTTP 500", "fetch_failed"), 502),
        (CaptureError("the site did not answer in time", "fetch_timeout"), 504),
    ],
)
def test_errors_map_to_a_status_and_carry_the_code(main_client, h, web, error, status):
    web["https://example.org/bad"] = error
    r = save(main_client, h, url="https://example.org/bad")
    assert r.status_code == status
    assert r.json() == {"detail": str(error), "code": error.code}


def test_page_without_an_article_is_422(main_client, h, web):
    web["https://example.org/app"] = (
        b"<html><body><div id='root'></div></body></html>",
        "text/html",
    )
    r = save(main_client, h, url="https://example.org/app")
    assert r.status_code == 422 and r.json()["code"] == "no_article"


def test_body_is_validated_and_the_route_needs_a_key(main_client, h, web):
    assert main_client.post("/api/library/url", json={}, headers=h).status_code == 422
    assert save(main_client, h, url="https://example.org/post", kind="nope").status_code == 422
    assert save(main_client, h, url="").status_code == 422
    assert (
        main_client.post("/api/library/url", json={"url": "https://example.org/post"}).status_code
        == 401
    )
    assert web["calls"] == []


def test_the_real_guard_refuses_a_lan_address_end_to_end(main_client, h, fake_pandoc_log):
    for url in (
        "http://127.0.0.1:8780/api/keys",
        "http://192.168.1.1/",
        "http://[::1]/",
        "file:///etc/passwd",
    ):
        r = save(main_client, h, url=url)
        assert r.status_code == 422, (url, r.text)
        assert r.json()["code"] in ("blocked_url", "bad_url")


def test_a_file_upload_can_also_queue_for_a_device(main_client, h, store):
    device, _ = DeviceService(store).register("x4")
    r = main_client.post(
        "/api/library",
        files={"file": ("note.txt", b"sent from the phone")},
        data={"device": "x4", "kind": "article"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["queued"]["item_id"] == r.json()["item"]["id"]
    assert [e.item_id for e in DeviceService(store).queue(device.id)] == [r.json()["item"]["id"]]

    missing = main_client.post(
        "/api/library", files={"file": ("b.txt", b"x")}, data={"device": "nope"}, headers=h
    )
    assert missing.status_code == 404
    assert len(main_client.get("/api/library", headers=h).json()["items"]) == 1
