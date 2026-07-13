import asyncio
import json
import pytest
import requests
import websockets

import sqlite3
import os

WS_URL = "ws://127.0.0.1:8000/api/v1/logs/ws"
HTTP_URL = "http://127.0.0.1:8000"
HEADERS = {"Authorization": "Bearer test-secret-key"}

def clear_db():
    db_files = ["test_e2e.db", "fallback.db"]
    for db_file in db_files:
        if os.path.exists(db_file):
            try:
                conn = sqlite3.connect(db_file)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM failover_logs")
                conn.commit()
                conn.close()
            except Exception:
                pass

@pytest.fixture(autouse=True)
def clean_db():
    clear_db()
    yield
    clear_db()

@pytest.mark.asyncio
async def test_websocket_unauthorized(backend_runner):
    """
    Verify that connection requests without a valid token are rejected
    with a non-101 status code (typically 401) or closed immediately.
    """
    # Try connecting with no token
    try:
        async with websockets.connect(WS_URL) as ws:
            await ws.recv()
        pytest.fail("Connection succeeded but should have been rejected")
    except websockets.exceptions.InvalidStatus as e:
        assert e.response.status_code in (401, 403)
    except Exception as e:
        assert isinstance(e, (websockets.exceptions.InvalidHandshake, websockets.exceptions.ConnectionClosed))

    # Try connecting with invalid token
    try:
        async with websockets.connect(f"{WS_URL}?token=invalid-token") as ws:
            await ws.recv()
        pytest.fail("Connection succeeded with invalid token but should have been rejected")
    except websockets.exceptions.InvalidStatus as e:
        assert e.response.status_code in (401, 403)
    except Exception as e:
        assert isinstance(e, (websockets.exceptions.InvalidHandshake, websockets.exceptions.ConnectionClosed))

@pytest.mark.asyncio
async def test_websocket_streaming(backend_runner):
    """
    Verify successful connection and real-time streaming of new failover log events.
    """
    async with websockets.connect(f"{WS_URL}?token=test-secret-key") as ws:
        # Trigger an action that writes a log entry
        payload = {"primary_enabled": True, "backup_enabled": False}
        resp = requests.post(f"{HTTP_URL}/api/v1/failover/trigger", json=payload, headers=HEADERS)
        assert resp.status_code == 200
        
        # Verify the event is streamed via WebSocket
        message = await asyncio.wait_for(ws.recv(), timeout=5.0)
        log_entry = json.loads(message)
        assert log_entry["event_type"] == "MANUAL_FAILOVER"
        assert "Primary enabled=True" in log_entry["message"]

@pytest.mark.asyncio
async def test_websocket_disconnect_reconnect(backend_runner):
    """
    Verify that a client can disconnect and reconnect within 50s and continue
    streaming messages.
    """
    # Connect and disconnect first client
    async with websockets.connect(f"{WS_URL}?token=test-secret-key") as ws1:
        pass # Disconnects immediately on block exit
        
    # Reconnect immediately (well within 50s window)
    async with websockets.connect(f"{WS_URL}?token=test-secret-key") as ws2:
        payload = {"primary_enabled": False, "backup_enabled": True}
        resp = requests.post(f"{HTTP_URL}/api/v1/failover/trigger", json=payload, headers=HEADERS)
        assert resp.status_code == 200
        
        message = await asyncio.wait_for(ws2.recv(), timeout=5.0)
        log_entry = json.loads(message)
        assert log_entry["event_type"] == "MANUAL_FAILOVER"
        assert "Backup enabled=True" in log_entry["message"]
