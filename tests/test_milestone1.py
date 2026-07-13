import pytest
import time
from unittest.mock import patch
from backend import config
from fastapi.testclient import TestClient

def test_compression_gzip(client):
    headers = {
        "Accept-Encoding": "gzip",
        "Authorization": f"Bearer {config.API_TOKEN}"
    }
    response = client.get("/api/v1/health/failover", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"

def test_compression_brotli(client):
    headers = {
        "Accept-Encoding": "gzip, br",
        "Authorization": f"Bearer {config.API_TOKEN}"
    }
    response = client.get("/api/v1/health/failover", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "br"

def test_compression_ignored_for_non_api(client):
    headers = {
        "Accept-Encoding": "br, gzip"
    }
    response = client.get("/index.html", headers=headers)
    assert response.status_code == 200
    assert "content-encoding" not in response.headers

def test_rate_limiting(client):
    with patch("backend.config.RATE_LIMIT_ENABLED", True), \
         patch("backend.config.RATE_LIMIT_MAX_REQUESTS", 3), \
         patch("backend.config.RATE_LIMIT_WINDOW", 5):
         
        headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
        
        # Reset limiter client state by using a unique IP for this test
        headers_ip1 = {**headers, "x-forwarded-for": "1.2.3.4"}
        
        # 3 requests should be successful
        for _ in range(3):
            resp = client.get("/api/v1/failover/logs", headers=headers_ip1)
            assert resp.status_code == 200
            
        # 4th request must be rate limited (429)
        resp = client.get("/api/v1/failover/logs", headers=headers_ip1)
        assert resp.status_code == 429
        assert resp.json() == {"detail": "Rate limit exceeded. Max 3 requests per minute."}
        
        # Request from a different IP should not be rate limited
        headers_ip2 = {**headers, "x-forwarded-for": "5.6.7.8"}
        resp = client.get("/api/v1/failover/logs", headers=headers_ip2)
        assert resp.status_code == 200
        
        # After the window (5 seconds) expires, requests from IP1 should not be rate limited anymore
        time.sleep(5.1)
        resp = client.get("/api/v1/failover/logs", headers=headers_ip1)
        assert resp.status_code == 200

def test_rate_limiting_ignored_for_non_api_and_health(client):
    with patch("backend.config.RATE_LIMIT_ENABLED", True), \
         patch("backend.config.RATE_LIMIT_MAX_REQUESTS", 3), \
         patch("backend.config.RATE_LIMIT_WINDOW", 60):
         
        headers = {
            "Authorization": f"Bearer {config.API_TOKEN}",
            "x-forwarded-for": "9.9.9.9"
        }
        
        # Send 5 requests to the health check endpoint
        for _ in range(5):
            resp = client.get("/api/v1/health/failover", headers=headers)
            assert resp.status_code == 200  # Should never be rate limited

def test_static_cache_control_headers(client):
    resp_html = client.get("/index.html")
    assert resp_html.status_code == 200
    assert resp_html.headers.get("Cache-Control") == "no-cache, must-revalidate"
    
    resp_css = client.get("/index.css")
    assert resp_css.status_code == 200
    assert resp_css.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
    
    resp_js = client.get("/app.js")
    assert resp_js.status_code == 200
    assert resp_js.headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_connection_error_sanitization():
    from backend.database import sanitize_connection_error
    
    # Postgres URL with password
    exc1 = Exception("Could not connect to postgresql://admin:secret123@localhost:5432/mydb")
    sanitized1 = sanitize_connection_error(exc1)
    assert "secret123" not in sanitized1
    assert "admin" not in sanitized1
    assert "postgresql://***:***@localhost:5432/mydb" in sanitized1

    # Postgres URL without password
    exc2 = Exception("Could not connect to postgresql://admin@localhost:5432/mydb")
    sanitized2 = sanitize_connection_error(exc2)
    assert "admin" not in sanitized2
    assert "postgresql://***@localhost:5432/mydb" in sanitized2

    # Sqlite URL (no credentials)
    exc3 = Exception("Could not open sqlite:///path/to/db")
    sanitized3 = sanitize_connection_error(exc3)
    assert sanitized3 == "Could not open sqlite:///path/to/db"


def test_alert_error_sanitization():
    from backend.alerts import sanitize_alert_error
    
    # Mock config settings temporarily
    with patch("backend.config.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123456/abcdefg_98765"), \
         patch("backend.config.BREVO_API_KEY", "xkeysib-brevo-key-123-abc"):
         
        exc = Exception("Failed posting to https://discord.com/api/webhooks/123456/abcdefg_98765 using key xkeysib-brevo-key-123-abc")
        sanitized = sanitize_alert_error(exc)
        
        assert "abcdefg_98765" not in sanitized
        assert "xkeysib-brevo-key-123-abc" not in sanitized
        assert "[REDACTED]" in sanitized or "[REDACTED_DISCORD_WEBHOOK]" in sanitized
        assert "[REDACTED_BREVO_KEY]" in sanitized


@pytest.mark.asyncio
async def test_get_db_session_rollback_on_error():
    from backend.database import get_db
    from unittest.mock import AsyncMock
    
    db_mock = AsyncMock()
    db_mock.execute = AsyncMock()  # for select 1
    
    # We can mock the session local to return our db_mock
    with patch("backend.database.get_all_session_locals", return_value=[lambda: db_mock]), \
         patch("backend.database.is_sqlite_file_locked", return_value=False):
         
        generator = get_db()
        # Retrieve db from generator
        db = await anext(generator)
        assert db is db_mock
        
        # Now raise exception to trigger the try-except rollback in get_db
        try:
            raise RuntimeError("FastAPI handler failed")
        except RuntimeError as e:
            try:
                await generator.athrow(e)
            except RuntimeError:
                pass
        
        # Verify rollback was called and close was called
        db_mock.rollback.assert_called_once()
        db_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_health_failover_commits_outbox_events(client):
    from backend.alerts import AlertManager
    from sqlalchemy.ext.asyncio import AsyncSession
    
    # Reset AlertManager state
    AlertManager._last_status = "healthy"
    
    commit_called = False
    orig_commit = AsyncSession.commit
    async def mock_commit(self):
        nonlocal commit_called
        commit_called = True
        await orig_commit(self)
        
    with patch("sqlalchemy.ext.asyncio.AsyncSession.commit", mock_commit):
        AlertManager._last_status = "degraded"
        resp = client.get(
            "/api/v1/health/failover",
            headers={"Authorization": f"Bearer {config.API_TOKEN}"}
        )
        assert resp.status_code == 200
        assert commit_called is True
