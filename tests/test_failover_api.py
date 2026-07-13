import pytest
from unittest.mock import patch, AsyncMock
from backend import config
import backend.database
import backend.alerts


def test_unauthorized_failover_trigger(client):
    payload = {"primary_enabled": True, "backup_enabled": False}
    resp = client.post("/api/v1/failover/trigger", json=payload)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authorized_failover_trigger_success(client):
    headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
    payload = {"primary_enabled": True, "backup_enabled": False}

    with patch("backend.cloudflare.CloudflareClient.set_pool_routing", return_value=True) as mock_routing:
        resp = client.post("/api/v1/failover/trigger", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "Successfully updated routing rules" in data["message"]
        mock_routing.assert_called_once_with(True, False)

        # Verify manual failover logged in database
        session = backend.database.PrimarySessionLocal()
        try:
            from sqlalchemy import select
            stmt = select(backend.database.FailoverLog).filter(
                backend.database.FailoverLog.event_type == "MANUAL_FAILOVER"
            )
            res = await session.execute(stmt)
            log = res.scalars().first()
            assert log is not None
            assert "Primary enabled=True" in log.message
        finally:
            await session.close()


def test_authorized_failover_trigger_failure(client):
    headers = {"X-API-Token": config.API_TOKEN}
    payload = {"primary_enabled": False, "backup_enabled": True}

    with patch("backend.cloudflare.CloudflareClient.set_pool_routing", return_value=False):
        resp = client.post("/api/v1/failover/trigger", json=payload, headers=headers)
        assert resp.status_code == 502
        assert "Failed to update Cloudflare" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_alert_manager_transitions():
    with patch("backend.alerts.send_discord_webhook", new_callable=AsyncMock) as mock_webhook, \
         patch("backend.alerts.send_brevo_email", new_callable=AsyncMock) as mock_email:

        # Reset AlertManager state
        backend.alerts.AlertManager._last_status = "healthy"
        backend.alerts.AlertManager._last_alert_time = 0.0

        # 1. No transition: healthy -> healthy
        await backend.alerts.AlertManager.process_health_change("healthy", "No change")
        mock_webhook.assert_not_called()
        mock_email.assert_not_called()

        # 2. Transition: healthy -> degraded (should alert)
        await backend.alerts.AlertManager.process_health_change("degraded", "Primary database offline")
        mock_webhook.assert_called_once()
        mock_email.assert_called_once()

        mock_webhook.reset_mock()
        mock_email.reset_mock()

        # 3. Transition: degraded -> healthy (should alert since it transitions)
        await backend.alerts.AlertManager.process_health_change("healthy", "Primary database online")
        mock_webhook.assert_called_once()
        mock_email.assert_called_once()
