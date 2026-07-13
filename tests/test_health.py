import pytest
from unittest.mock import patch
from backend import config
import backend.database


def test_unauthorized_access(client):
    # Health endpoint 401
    resp1 = client.get("/api/v1/health/failover")
    assert resp1.status_code == 401

    resp2 = client.get(
        "/api/v1/health/failover",
        headers={"Authorization": "Bearer badtoken"}
    )
    assert resp2.status_code == 401

    # Logs endpoint 401
    resp3 = client.get("/api/v1/failover/logs")
    assert resp3.status_code == 401

    resp4 = client.get(
        "/api/v1/failover/logs",
        headers={"X-API-Token": "badtoken"}
    )
    assert resp4.status_code == 401


def test_successful_authorized_health_check(client):
    # Using default API token
    headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
    response = client.get("/api/v1/health/failover", headers=headers)

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "healthy"
    assert "cpu_percent" in data
    assert "memory_percent" in data
    assert "db_latency_ms" in data
    assert data["database_status"] == "primary"
    assert "cloudflare_pool_status" in data

    # Verify cloudflare pool structure
    cf_status = data["cloudflare_pool_status"]
    assert isinstance(cf_status, list)
    assert len(cf_status) == 2
    assert cf_status[0]["name"] == "primary-pool"
    assert cf_status[1]["name"] == "backup-pool"


@pytest.mark.asyncio
async def test_db_fallback_mechanism(client):
    # Mock PrimarySessionLocal to raise an exception,
    # simulating connection failure.
    def mock_conn_fail(*args, **kwargs):
        raise Exception("Database Connection Refused")

    with patch(
        "backend.database.PrimarySessionLocal",
        side_effect=mock_conn_fail
    ):
        headers = {"X-API-Token": config.API_TOKEN}
        response = client.get("/api/v1/health/failover", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["database_status"] == "fallback"
        assert data["status"] == "degraded"

        # Verify SQLite fallback database recorded a DB_FALLBACK_ACTIVE event
        session = backend.database.FallbackSessionLocal()
        try:
            from sqlalchemy import select
            stmt = select(backend.database.FailoverLog).filter(
                backend.database.FailoverLog.event_type == "DB_FALLBACK_ACTIVE"
            )
            res = await session.execute(stmt)
            logs = res.scalars().all()
            assert len(logs) == 1
            assert "Database Connection Refused" in logs[0].message
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_logs_retrieval(client):
    # Manually insert logs into primary database since it is active
    session = backend.database.PrimarySessionLocal()
    try:
        log1 = backend.database.FailoverLog(
            event_type="DB_FALLBACK_ACTIVE", message="Primary DB is down"
        )
        log2 = backend.database.FailoverLog(
            event_type="MANUAL_FAILOVER", message="Force failover triggered"
        )
        session.add_all([log1, log2])
        await session.commit()
    finally:
        await session.close()

    headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
    response = client.get("/api/v1/failover/logs", headers=headers)

    assert response.status_code == 200
    logs = response.json()
    assert len(logs) >= 2

    event_types = [log["event_type"] for log in logs]
    assert "DB_FALLBACK_ACTIVE" in event_types
    assert "MANUAL_FAILOVER" in event_types

    messages = [log["message"] for log in logs]
    assert "Primary DB is down" in messages
    assert "Force failover triggered" in messages


def test_prometheus_metrics_endpoint(client):
    # Test unauthorized access
    unauth_resp = client.get("/metrics")
    assert unauth_resp.status_code == 401

    headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
    resp = client.get("/metrics", headers=headers)
    assert resp.status_code == 200
    content = resp.text
    assert "http_requests_total" in content

    # Trigger a request that is tracked
    client.get("/api/v1/health/failover")

    resp2 = client.get("/metrics", headers=headers)
    assert resp2.status_code == 200
    content2 = resp2.text
    assert 'http_requests_total{endpoint="/api/v1/health/failover",http_status="401",method="GET"}' in content2


def test_orjson_response_serialization_configuration():
    from fastapi.responses import ORJSONResponse
    from backend.main import app
    assert app.router.default_response_class == ORJSONResponse



