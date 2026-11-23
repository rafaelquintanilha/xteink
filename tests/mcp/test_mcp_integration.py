"""Drive `python -m xteink.mcp` (stdio) with the mcp SDK client against the real API."""

import json
import os
import subprocess
import sys

import anyio
import pytest

from xteink.core import DeviceService

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def run_session(env_extra, fn):
    env = {**os.environ, **env_extra}
    params = StdioServerParameters(command=sys.executable, args=["-m", "xteink.mcp"], env=env)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)

    return anyio.run(go)


def text(res):
    return res.content[0].text


def test_lists_four_tools(api_url, api_key):
    async def fn(s):
        return {t.name for t in (await s.list_tools()).tools}

    names = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    assert names == {"push_file", "save_url", "list_library", "send_to_device"}


def test_push_markdown_list_and_queue(api_url, api_key, store, device):
    async def fn(s):
        pushed = await s.call_tool(
            "push_file",
            {"content": "# Title\n\nSome article.", "filename": "post.md", "title": "Post"},
        )
        listed = await s.call_tool("list_library", {"kind": "article"})
        item_id = json.loads(text(pushed))["item"]["id"]
        sent = await s.call_tool("send_to_device", {"device": "reader", "item_id": item_id})
        return pushed, listed, sent, item_id

    pushed, listed, sent, item_id = run_session(
        {"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn
    )
    assert not pushed.isError
    assert json.loads(text(pushed))["item"]["kind"] == "article"
    items = json.loads(text(listed))["items"]
    assert [(i["id"], i["kind"]) for i in items] == [(item_id, "article")]
    assert not sent.isError
    queue = DeviceService(store).queue(device.id)
    assert [e.item_id for e in queue] == [item_id]


def test_push_with_device_queues_in_one_call(api_url, api_key, store, device):
    async def fn(s):
        return await s.call_tool(
            "push_file", {"content": "plain", "filename": "n.txt", "device": str(device.id)}
        )

    res = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    out = json.loads(text(res))
    assert out["queued"]["item_id"] == out["item"]["id"]


PAGE = (
    "<html><head><title>From The Web</title></head><body><article><p>"
    + "A sentence of ordinary prose that is long enough to count as article text. " * 4
    + "</p></article></body></html>"
)


def test_save_url_fetches_converts_and_queues(api_url, api_key, store, device, monkeypatch):
    from xteink.server import capture

    def fake_fetch(url, **kw):
        return capture.Fetched(url=url, data=PAGE.encode(), content_type="text/html", charset="")

    # The API runs in this process (a thread), so its fetch can be replaced here.
    monkeypatch.setattr(capture, "fetch", fake_fetch)

    async def fn(s):
        return await s.call_tool("save_url", {"url": "https://example.org/a", "device": "reader"})

    res = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    assert not res.isError, text(res)
    out = json.loads(text(res))
    assert (out["item"]["title"], out["item"]["kind"], out["created"]) == (
        "From The Web",
        "article",
        True,
    )
    assert out["queued"]["item_id"] == out["item"]["id"]
    assert [e.item_id for e in DeviceService(store).queue(device.id)] == [out["item"]["id"]]


def test_save_url_refusals_are_tool_errors_with_the_code(api_url, api_key):
    async def fn(s):
        lan = await s.call_tool("save_url", {"url": "http://127.0.0.1:1/admin"})
        device = await s.call_tool("save_url", {"url": "https://example.org/a", "device": "nope"})
        empty = await s.call_tool("save_url", {"url": " "})
        return lan, device, empty

    lan, device, empty = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    assert lan.isError and "blocked_url" in text(lan) and "422" in text(lan)
    assert device.isError and "404" in text(device)
    assert empty.isError and "url is required" in text(empty)
    assert "Traceback" not in text(lan) + text(device) + text(empty)


def test_push_path(api_url, api_key, tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("book text")

    async def fn(s):
        return await s.call_tool("push_file", {"path": str(f), "title": "B"})

    res = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    assert json.loads(text(res))["item"]["title"] == "B"


def test_invalid_key_is_tool_error_with_code(api_url):
    async def fn(s):
        return await s.call_tool("list_library", {})

    res = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": "xtk_bogus"}, fn)
    assert res.isError
    assert "401" in text(res)
    assert "bogus" not in text(res)


def test_unknown_device_and_api_error_mapping(api_url, api_key):
    async def fn(s):
        a = await s.call_tool("send_to_device", {"device": "nope", "item_id": 1})
        b = await s.call_tool("push_file", {"content": "x", "filename": "a.xyz"})
        return a, b

    a, b = run_session({"XTEINK_URL": api_url, "XTEINK_API_KEY": api_key}, fn)
    assert a.isError
    assert "unknown_device" in text(a)
    assert b.isError
    assert "415" in text(b)
    assert "Traceback" not in text(b)


def test_missing_key_refuses_to_start(api_url):
    env = {k: v for k, v in os.environ.items() if k != "XTEINK_API_KEY"}
    r = subprocess.run(
        [sys.executable, "-m", "xteink.mcp"],
        env={**env, "XTEINK_URL": api_url},
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 2
    assert "XTEINK_API_KEY" in r.stderr


def _start_http_mcp(api_url, extra_env=None):
    """A streamable-http MCP server with NO server-side key: callers bring their own."""
    import socket
    import time

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "XTEINK_API_KEY"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "xteink.mcp", "--http", "--bind", "127.0.0.1", "--port", str(port)],
        env={**env, "XTEINK_URL": api_url, **(extra_env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return proc, f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
def http_mcp(api_url):
    proc, url = _start_http_mcp(api_url)
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def http_mcp_hosts(api_url):
    """Like http_mcp, with Host/Origin validation on (DNS rebinding protection)."""
    proc, url = _start_http_mcp(
        api_url,
        {
            "XTEINK_MCP_ALLOWED_HOSTS": "127.0.0.1:*",
            "XTEINK_MCP_ALLOWED_ORIGINS": "https://reader.example",
        },
    )
    try:
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _raw_initialize(url, key=None, extra_headers=None):
    """POST an MCP initialize; (status, headers, body). No redirects are followed."""
    import http.client
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **({"Authorization": f"Bearer {key}"} if key else {}),
        **(extra_headers or {}),
    }
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("POST", parts.path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _http_call(url, key, tool, args):
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {key}"}) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool, args)

    return anyio.run(go)


def test_streamable_http_uses_the_callers_key(http_mcp, api_key):
    res = _http_call(http_mcp, api_key, "list_library", {})
    assert not res.isError
    assert json.loads(text(res)) == {"items": []}


def test_streamable_http_without_a_key_is_401(http_mcp):
    import urllib.error
    import urllib.request

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    req = urllib.request.Request(
        http_mcp + "/",  # the mount answers /mcp with a redirect to /mcp/
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)  # nosec B310 - local test server
    assert e.value.code == 401


def test_streamable_http_wrong_key_is_401_before_the_session(http_mcp):
    status, headers, _ = _raw_initialize(http_mcp, "xtk_wrong_wrong_wrong")
    assert status == 401
    assert 'error="invalid_token"' in headers.get("www-authenticate", "")


def test_streamable_http_missing_key_names_the_scheme(http_mcp):
    status, headers, _ = _raw_initialize(http_mcp)
    assert status == 401
    assert headers.get("www-authenticate", "").startswith("Bearer")


def test_streamable_http_answers_at_mcp_without_a_redirect(http_mcp, api_key):
    # Behind a TLS proxy a /mcp -> /mcp/ redirect would point at http://.
    assert http_mcp.endswith("/mcp")
    status, _, _ = _raw_initialize(http_mcp, api_key)
    assert status == 200


def test_streamable_http_host_validation(http_mcp_hosts, api_key):
    assert _raw_initialize(http_mcp_hosts, api_key)[0] == 200
    assert _raw_initialize(http_mcp_hosts, api_key, {"Host": "evil.example"})[0] == 421
    bad_origin = {"Origin": "https://evil.example"}
    assert _raw_initialize(http_mcp_hosts, api_key, bad_origin)[0] == 403
    good_origin = {"Origin": "https://reader.example"}
    assert _raw_initialize(http_mcp_hosts, api_key, good_origin)[0] == 200
    res = _http_call(http_mcp_hosts, api_key, "list_library", {})
    assert not res.isError


def test_streamable_http_refuses_host_paths(http_mcp, api_key, tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("host file")
    res = _http_call(http_mcp, api_key, "push_file", {"path": str(f), "filename": "x.txt"})
    assert res.isError
    assert "path" in text(res)
