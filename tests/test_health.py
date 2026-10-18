"""Health endpoint tests."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gofile_bot.config import DEFAULT_HEALTH_PORT, get_health_port
from gofile_bot.health import create_health_app


async def test_health_returns_200_ok():
    client = TestClient(TestServer(create_health_app()))
    await client.start_server()
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        assert await resp.text() == "OK"
    finally:
        await client.close()


async def test_root_returns_200_ok():
    client = TestClient(TestServer(create_health_app()))
    await client.start_server()
    try:
        resp = await client.get("/")
        assert resp.status == 200
        assert await resp.text() == "OK"
    finally:
        await client.close()


async def test_unknown_path_404():
    client = TestClient(TestServer(create_health_app()))
    await client.start_server()
    try:
        resp = await client.get("/nope")
        assert resp.status == 404
    finally:
        await client.close()


def test_health_port_default(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert get_health_port() == DEFAULT_HEALTH_PORT


def test_health_port_from_env(monkeypatch):
    monkeypatch.setenv("PORT", "12345")
    assert get_health_port() == 12345


def test_health_port_invalid(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError):
        get_health_port()
    monkeypatch.setenv("PORT", "99999")
    with pytest.raises(ValueError):
        get_health_port()
