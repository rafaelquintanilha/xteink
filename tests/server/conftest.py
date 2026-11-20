"""Shared fixtures for the HTTP server suite (skips cleanly without the ``server`` extra)."""

import pytest

from xteink.core import DeviceService, KeyService, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def api_key(store):
    _, raw = KeyService(store).create("test")
    return raw


@pytest.fixture
def device_key(store):
    _, raw = DeviceService(store).register("reader")
    return raw


@pytest.fixture
def main_client(store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xteink.server.app import create_app

    return TestClient(create_app(store))


@pytest.fixture
def device_client(store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xteink.server.app import create_device_app

    return TestClient(create_device_app(store))


def bearer(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


# Fake pandoc (as in tests/core/test_ingest.py) that records what it was asked to convert.
_LOGGING_PANDOC = """
import sys, zipfile
a = sys.argv
out = a[a.index("-o") + 1]
meta = open(a[a.index("--metadata-file") + 1], encoding="utf-8").read()
source = open(a[-1], encoding="utf-8").read()
with open(%r, "w", encoding="utf-8") as f:
    f.write(" ".join(a) + chr(0) + meta + chr(0) + source)
with zipfile.ZipFile(out, "w") as z:
    z.writestr("mimetype", "application/epub+zip")
    z.writestr("content.xhtml", source)
"""


@pytest.fixture
def fake_pandoc_log(tmp_path, monkeypatch):
    """Puts a fake ``pandoc`` first on PATH; call the fixture for (argv, metadata, source)."""
    import os
    import stat
    import sys

    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "pandoc.log"
    p = d / "pandoc"
    p.write_text(f"#!{sys.executable}\n" + _LOGGING_PANDOC % str(log))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")
    return lambda: tuple(log.read_text(encoding="utf-8").split(chr(0)))
