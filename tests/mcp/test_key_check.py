"""KeyCheck: the HTTP MCP transport's bearer-key check (no network)."""

from xteink import client as api_client
from xteink.mcp.server import KeyCheck


class Verifier:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, key):
        self.calls.append(key)
        if self.error:
            raise self.error


def test_an_accepted_key_is_remembered_until_the_ttl():
    clock = [100.0]
    verify = Verifier()
    check = KeyCheck(verify, ttl=60, now=lambda: clock[0])
    assert check("xtk_a") == "ok"
    assert check("xtk_a") == "ok"
    assert verify.calls == ["xtk_a"]
    clock[0] += 61
    assert check("xtk_a") == "ok"
    assert verify.calls == ["xtk_a", "xtk_a"]


def test_a_refused_key_is_not_remembered():
    verify = Verifier(api_client.AuthenticationError(401, "invalid_key", "nope"))
    check = KeyCheck(verify)
    assert check("xtk_bad") == "invalid"
    verify.error = None  # the key was created since
    assert check("xtk_bad") == "ok"


def test_a_device_key_403_is_invalid_not_unavailable():
    check = KeyCheck(Verifier(api_client.ApiError(403, "forbidden", "device key")))
    assert check("xtk_dev") == "invalid"


def test_an_unreachable_api_is_unavailable_and_not_remembered():
    verify = Verifier(api_client.ServerUnreachable("down"))
    check = KeyCheck(verify)
    assert check("xtk_a") == "unavailable"
    verify.error = api_client.ApiError(502, "http_502", "bad gateway")
    assert check("xtk_a") == "unavailable"
    assert len(verify.calls) == 2


def test_the_cache_stores_digests_not_keys():
    check = KeyCheck(Verifier())
    check("xtk_secret_value")
    assert all("xtk_secret_value" not in k for k in check._accepted)
