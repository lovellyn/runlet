import httpx2 as httpx
import pytest

from examples.client import connection_headers
from runlet.server import create_app
from tests.test_mcp import connect


async def test_gateway_authorization_and_sandbox_header_are_independent(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={
                "Authorization": "Bearer upstream-example",
                "X-Sandbox-Token": settings.token,
            },
            base_url="http://localhost",
        ) as http:
            async with connect(http) as client:
                result = await client.call_tool("server_info", {})
                assert not result.is_error
                assert result.structured_content["boot_id"] == app.state.services.boot_id


@pytest.mark.parametrize("sandbox_headers", [[], [""], ["wrong"], ["correct", "correct"]])
async def test_authorization_never_substitutes_for_unique_sandbox_header(settings, sandbox_headers):
    app = create_app(settings)
    headers = [("Authorization", f"Bearer {settings.token}")]
    headers += [
        ("X-Sandbox-Token", settings.token if value == "correct" else value)
        for value in sandbox_headers
    ]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            assert (await http.post("/mcp", json={}, headers=headers)).status_code == 401


def test_client_headers_support_optional_gateway(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "sandbox-example")
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    assert connection_headers() == {"X-Sandbox-Token": "sandbox-example"}
    monkeypatch.setenv("GATEWAY_TOKEN", "gateway-example")
    assert connection_headers() == {
        "X-Sandbox-Token": "sandbox-example",
        "Authorization": "Bearer gateway-example",
    }
