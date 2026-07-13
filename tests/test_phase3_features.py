import pytest
from unittest.mock import patch, AsyncMock
from backend import config
import backend.database
from backend.database import DatabaseCircuitBreaker, get_db_context, FailoverLog
from sqlalchemy import select


def test_unauthorized_failover_simulate(client):
    payload = {"primary_enabled": False, "backup_enabled": True}
    resp = client.post("/api/v1/failover/simulate", json=payload)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authorized_failover_simulate_success(client):
    headers = {"Authorization": f"Bearer {config.API_TOKEN}"}
    payload = {"primary_enabled": False, "backup_enabled": True}

    with patch("backend.alerts.send_discord_webhook", new_callable=AsyncMock) as mock_discord, \
         patch("backend.alerts.send_brevo_email", new_callable=AsyncMock) as mock_email:
         
        resp = client.post("/api/v1/failover/simulate", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "Successfully simulated routing rules" in data["message"]
        
        mock_discord.assert_called_once()
        mock_email.assert_called_once()

        # Check DB log
        session = backend.database.PrimarySessionLocal()
        try:
            stmt = select(FailoverLog).filter(FailoverLog.event_type == "FAILOVER_SIMULATION")
            res = await session.execute(stmt)
            log = res.scalars().first()
            assert log is not None
            assert "Dry run manual routing trigger simulated" in log.message
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_database_circuit_breaker_trips():
    # Reset state
    DatabaseCircuitBreaker.consecutive_failures = 0
    DatabaseCircuitBreaker.tripped = False
    DatabaseCircuitBreaker.tripped_time = 0.0

    # We mock is_sqlite_file_locked to return False, and mock PrimarySessionLocal to raise an exception
    # to trigger failures through get_db()
    with patch("backend.database.is_sqlite_file_locked", return_value=False), \
         patch("backend.database.PrimarySessionLocal") as mock_session:
         
        mock_session.return_value.execute = AsyncMock(side_effect=Exception("Database connection error"))
        mock_session.return_value.close = AsyncMock()
        
        # 1. Trigger 4 database failures through get_db()
        for _ in range(4):
            async with get_db_context() as db:
                assert db.bind == backend.database.fallback_engine
        
        assert not DatabaseCircuitBreaker.is_tripped()

        # 2. Trigger 5th database failure through get_db() (should trip the circuit breaker)
        async with get_db_context() as db:
            assert db.bind == backend.database.fallback_engine

        assert DatabaseCircuitBreaker.is_tripped()

        # 3. Verify circuit breaker trip log was recorded in fallback DB
        async with get_db_context() as db:
            assert db.bind == backend.database.fallback_engine
            stmt = select(FailoverLog).filter(FailoverLog.event_type == "CIRCUIT_BREAKER_TRIPPED")
            res = await db.execute(stmt)
            log = res.scalars().first()
            assert log is not None
            assert "Bypassing primary DB for 60 seconds" in log.message

    # 4. Success connection should reset circuit breaker
    DatabaseCircuitBreaker.record_success()
    assert not DatabaseCircuitBreaker.is_tripped()
