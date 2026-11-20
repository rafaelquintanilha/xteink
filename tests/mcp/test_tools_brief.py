"""MCP results keep the item size the API returns (field ``size``)."""

from xteink.mcp.tools import _brief


def test_brief_keeps_api_size_field():
    item = {"id": 1, "title": "T", "author": "", "kind": "book", "format": "txt", "size": 42}
    assert _brief(item)["size"] == 42


def test_kind_enum_has_the_four_kinds():
    from xteink.mcp.tools import tool_specs

    specs = {t["name"]: t for t in tool_specs()}
    for name in ("push_file", "save_url", "list_library"):
        enum = specs[name]["inputSchema"]["properties"]["kind"]["enum"]
        assert enum == ["book", "article", "transcript", "newspaper"]
    assert "set_kind" not in specs


def test_save_url_passes_the_arguments_through_and_trims_the_result():
    from xteink.mcp.tools import call_tool, tool_specs

    spec = {t["name"]: t for t in tool_specs()}["save_url"]
    assert spec["inputSchema"]["required"] == ["url"]

    class FakeClient:
        def save_url(self, url, **kw):
            self.got = (url, kw)
            item = {"id": 3, "title": "T", "author": "", "kind": "article", "format": "epub"}
            return {
                "item": {**item, "size": 9, "sha256": "ab"},
                "created": True,
                "queued": {"id": 1},
            }

    client = FakeClient()
    out = call_tool("save_url", {"url": "https://example.org/a", "device": 2}, lambda: client)
    assert client.got == (
        "https://example.org/a",
        {"title": None, "author": "", "kind": None, "device": 2},
    )
    assert out == {
        "item": {
            "id": 3,
            "title": "T",
            "author": "",
            "kind": "article",
            "format": "epub",
            "size": 9,
        },
        "created": True,
        "queued": {"id": 1},
    }
