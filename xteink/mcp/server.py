"""The MCP server (official ``mcp`` SDK, low-level ``Server`` + ``mcp.types``).

Transports:

* stdio: the server talks to the HTTP API with the key from ``XTEINK_API_KEY`` (a local,
  single-user process).
* streamable-http, served at ``/mcp``: every request must carry
  ``Authorization: Bearer xtk_...``, and the key is checked against the API
  (``GET /api/whoami``) before the request reaches an MCP session. A missing or refused key
  gets 401 with ``WWW-Authenticate: Bearer`` (RFC 6750), an unreachable API 503. A key the
  API accepted is trusted for :data:`KEY_CHECK_TTL_S` without asking again; each tool call
  still uses the caller's key against the API, which decides. The HTTP server holds no key
  of its own, and ``push_file``'s ``path`` (a file on the MCP host) is refused over HTTP.
  ``XTEINK_MCP_ALLOWED_HOSTS`` / ``XTEINK_MCP_ALLOWED_ORIGINS`` (comma-separated) turn on
  the SDK's Host/Origin validation against DNS rebinding.

Never routed through the Cloudflare tunnel. On a VPS it can sit behind the same reverse
proxy as the API (docs/vps-deploy.md): it exposes nothing the API does not.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from xteink import client as api_client
from xteink.mcp import tools

__all__ = ["ServerExtraMissing", "build_server", "run_http", "run_stdio"]


class ServerExtraMissing(ImportError):
    """The ``server`` extra (mcp SDK) is not installed."""


_EXTRA_MESSAGE = "the MCP server needs the 'server' extra: pip install 'xteink[server]'"


def _env() -> dict[str, str]:
    return dict(os.environ)


def _sdk():
    try:
        import mcp.types as types  # noqa: PLC0415 - optional extra
        from mcp.server.lowlevel import Server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    return Server, types


def _bearer(headers: Any) -> str:
    auth = headers.get("authorization") or ""
    scheme, _, key = auth.partition(" ")
    return key.strip() if scheme.lower() == "bearer" else ""


KEY_CHECK_TTL_S = 60.0
_KEY_CACHE_MAX = 256


class KeyCheck:
    """Checks a bearer key with the API before an HTTP request reaches the MCP session.

    Accepted keys are remembered (by sha256, never the key itself) for ``ttl`` seconds, so
    a session's many requests cost one API call. Refused keys are not remembered: a key
    created a moment later works at once.
    """

    def __init__(
        self,
        verify: Callable[[str], None],
        *,
        ttl: float = KEY_CHECK_TTL_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._verify = verify
        self._ttl = ttl
        self._now = now
        self._accepted: dict[str, float] = {}

    def __call__(self, key: str) -> str:
        """``"ok"``, ``"invalid"`` (the API refused the key) or ``"unavailable"``."""
        digest = hashlib.sha256(key.encode()).hexdigest()
        now = self._now()
        if self._accepted.get(digest, 0.0) > now:
            return "ok"
        try:
            self._verify(key)
        except api_client.ApiError as exc:
            # Any 4xx is the key's fault (401 unknown or revoked, 403 a device key).
            return "invalid" if 400 <= exc.status < 500 else "unavailable"
        if len(self._accepted) >= _KEY_CACHE_MAX:
            self._accepted = {d: t for d, t in self._accepted.items() if t > now}
            if len(self._accepted) >= _KEY_CACHE_MAX:
                self._accepted.clear()
        self._accepted[digest] = now + self._ttl
        return "ok"


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_server(client_factory: Callable[[], Any] | None = None, *, http_auth: bool = False):
    """The MCP server. With ``http_auth`` every tool call uses the HTTP caller's bearer key."""
    server_cls, types = _sdk()
    default_factory = client_factory or api_client.from_env
    server = server_cls("xteink")

    def factory_for_call(arguments: dict[str, Any] | None) -> Callable[[], Any]:
        if not http_auth:
            return default_factory
        if arguments and arguments.get("path") is not None:
            raise tools.ToolError(
                "invalid_arguments: path (a file on the MCP host) is not allowed over HTTP; "
                "send content or content_base64"
            )
        key = _bearer(server.request_context.request.headers)
        if not key:
            raise tools.ToolError("missing_key: send Authorization: Bearer xtk_...")
        base_url = api_client.from_env({**_env(), api_client.KEY_ENV: key}).base_url
        return lambda: api_client.Client(base_url, key)

    @server.list_tools()
    async def _list() -> list[Any]:
        return [types.Tool(**spec) for spec in tools.tool_specs()]

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        import anyio  # noqa: PLC0415

        try:
            factory = factory_for_call(arguments)
            # urllib is blocking (uploads may wait on pandoc); keep it off the event loop.
            result = await anyio.to_thread.run_sync(tools.call_tool, name, arguments, factory)
        except tools.ToolError as exc:
            raise ValueError(str(exc)) from exc  # the SDK turns this into isError
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


def run_stdio() -> None:
    try:
        import anyio  # noqa: PLC0415
        from mcp.server.stdio import stdio_server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    server = build_server()

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


def build_http_app(env: dict[str, str] | None = None, key_check: KeyCheck | None = None):
    """The streamable-http ASGI app: ``/mcp`` (and ``/mcp/``), key-checked."""
    try:
        import anyio  # noqa: PLC0415
        from mcp.server.streamable_http_manager import (  # noqa: PLC0415
            StreamableHTTPSessionManager,
        )
        from mcp.server.transport_security import TransportSecuritySettings  # noqa: PLC0415
        from starlette.applications import Starlette  # noqa: PLC0415
        from starlette.routing import Mount, Route  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    import contextlib  # noqa: PLC0415

    from starlette.responses import JSONResponse  # noqa: PLC0415

    env = _env() if env is None else env
    base_url = api_client.from_env({**env, api_client.KEY_ENV: "unused"}).base_url
    if key_check is None:
        key_check = KeyCheck(lambda key: api_client.Client(base_url, key).whoami())
    hosts, origins = _csv(env.get("XTEINK_MCP_ALLOWED_HOSTS")), _csv(
        env.get("XTEINK_MCP_ALLOWED_ORIGINS")
    )
    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
        )
        if hosts
        else None
    )
    manager = StreamableHTTPSessionManager(
        app=build_server(http_auth=True), security_settings=security
    )

    def refuse(status: int, detail: str, challenge: str | None) -> Any:
        headers = {"WWW-Authenticate": challenge} if challenge else None
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)

    class Guard:
        """ASGI app: the key is checked here, before the MCP session sees the request."""

        async def __call__(self, scope, receive, send) -> None:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
            }
            key = _bearer(headers)
            if not key.startswith("xtk_"):
                response = refuse(
                    401,
                    "send Authorization: Bearer xtk_... (an xteink API key)",
                    'Bearer realm="xteink"',
                )
            else:
                verdict = await anyio.to_thread.run_sync(key_check, key)
                if verdict == "ok":
                    await manager.handle_request(scope, receive, send)
                    return
                if verdict == "invalid":
                    response = refuse(
                        401,
                        "the API key is unknown or revoked",
                        'Bearer realm="xteink", error="invalid_token"',
                    )
                else:
                    response = refuse(503, "the xteink API is not reachable", None)
            await response(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    guard = Guard()
    # Both spellings answer directly: a /mcp -> /mcp/ redirect behind a TLS-terminating proxy
    # would point at http://, and clients must not follow it with their key.
    return Starlette(
        routes=[Route("/mcp", endpoint=guard), Mount("/mcp", app=guard)], lifespan=lifespan
    )


def run_http(bind: str, port: int) -> None:
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    uvicorn.run(build_http_app(), host=bind, port=port, log_level="info")
