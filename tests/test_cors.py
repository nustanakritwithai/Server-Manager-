"""Browser clients on GitHub Pages call the API from another origin."""

from __future__ import annotations

PAGES = "https://nustanakritwithai.github.io"
LOCAL = "http://127.0.0.1:8080"


def test_cors_preflight_allows_github_pages(client) -> None:
    response = client.options(
        "/v1/auth/dev-login",
        headers={
            "Origin": PAGES,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == PAGES
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "content-type" in allowed
    assert "POST" in response.headers["access-control-allow-methods"]


def test_cors_allows_localhost_dev_origin(client) -> None:
    response = client.get("/health", headers={"Origin": LOCAL})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LOCAL


def test_cors_does_not_reflect_an_unknown_origin(client) -> None:
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") in (None, "")


def test_clock_advance_stays_admin_only(client) -> None:
    denied = client.post("/v1/admin/clock/advance", json={"seconds": 1})
    assert denied.status_code == 401
