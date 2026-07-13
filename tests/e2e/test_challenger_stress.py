import asyncio
import json
import time
import requests
import websockets
import pytest

BASE_URL = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000/api/v1/logs/ws"
HEADERS = {"Authorization": "Bearer test-secret-key"}

class DBOutageSimulator:
    def __init__(self, filepath):
        self.filepath = filepath
        self.file_handle = None
    def lock(self):
        self.file_handle = open(self.filepath, "r+b")
        import os
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    def unlock(self):
        if self.file_handle:
            import os
            if os.name == "nt":
                import msvcrt
                try:
                    self.file_handle.seek(0)
                    msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            self.file_handle.close()
            self.file_handle = None

@pytest.mark.asyncio
async def test_challenger_rate_limiting_concurrency_and_sliding_window(backend_runner):
    """
    Validate the rate limiting concurrency and sliding-window logic.
    Send 20 requests concurrently. The rate limit is set to 5 requests per 2s window.
    Exactly 5 should succeed (200), and 15 should be rate limited (429).
    Then we wait for 2.5 seconds (the sliding window is 2s, but we wait 2.5s to be safe)
    and send another 5 requests, which should all succeed.
    """
    async def make_request(client_ip):
        loop = asyncio.get_running_loop()
        headers = {**HEADERS, "X-Forwarded-For": client_ip}
        def get_req():
            try:
                return requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=headers, timeout=15)
            except Exception as e:
                return e
        return await loop.run_in_executor(None, get_req)

    # 1. Test Concurrency for same IP
    ip = "10.9.8.7"
    tasks = [make_request(ip) for _ in range(20)]
    responses = await asyncio.gather(*tasks)
    
    status_codes = []
    for resp in responses:
        if isinstance(resp, requests.Response):
            status_codes.append(resp.status_code)
        else:
            status_codes.append(500)
            
    success_count = status_codes.count(200)
    limited_count = status_codes.count(429)
    
    assert success_count == 5, f"Expected exactly 5 successful requests, got {success_count}. Statuses: {status_codes}"
    assert limited_count == 15, f"Expected exactly 15 rate limited requests, got {limited_count}"

    # 2. Test Sliding Window Reset
    # Wait for the 2-second window to reset
    await asyncio.sleep(2.5)
    
    # Try 5 more requests for the same IP
    tasks = [make_request(ip) for _ in range(5)]
    responses = await asyncio.gather(*tasks)
    status_codes = [r.status_code for r in responses if isinstance(r, requests.Response)]
    assert status_codes == [200] * 5, f"Expected all 5 requests to succeed after sliding window reset, got {status_codes}"


@pytest.mark.asyncio
async def test_challenger_websocket_stream_stress(backend_runner):
    """
    Stress-test the WebSocket stream and ensure no messages are dropped or duplicated.
    We connect via WebSocket with ping_interval=None to disable client-side keepalive pings.
    Then we perform 20 manual failovers.
    We expect exactly 20 MANUAL_FAILOVER events to be received,
    without any duplicate messages or dropped messages.
    """
    async with websockets.connect(f"{WS_URL}?token=test-secret-key", ping_interval=None) as ws:
        # Clear/drain historical logs first. The connection sends up to 20 historical logs.
        historical_msgs = []
        try:
            # Receive any initial historical messages
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                historical_msgs.append(json.loads(msg))
        except asyncio.TimeoutError:
            pass # historical messages drained
            
        # Trigger 20 manual failover operations.
        # Since each IP has its own rate limit (5 requests per 2s), we send from unique IPs.
        for idx in range(20):
            headers = {
                **HEADERS,
                "X-Forwarded-For": f"192.168.99.{idx}"
            }
            payload = {"primary_enabled": True, "backup_enabled": False}
            resp = requests.post(f"{BASE_URL}/api/v1/failover/trigger", json=payload, headers=headers, timeout=15)
            assert resp.status_code == 200, f"Trigger {idx} failed with status {resp.status_code}"
            await asyncio.sleep(0.05)
        
        # Now read 20 messages from the WS stream
        received_logs = []
        for _ in range(20):
            try:
                # High timeout to survive any event loop blocking on the server side
                msg_str = await asyncio.wait_for(ws.recv(), timeout=15.0)
                msg = json.loads(msg_str)
                # Ignore pings if any
                if msg.get("type") == "ping":
                    continue
                received_logs.append(msg)
            except asyncio.TimeoutError:
                break
                
        assert len(received_logs) == 20, f"Expected 20 messages, only received {len(received_logs)}"
        
        # Verify no duplicates
        ids = [log["id"] for log in received_logs]
        assert len(set(ids)) == 20, f"Duplicates detected in WS stream! IDs: {ids}"
        for log in received_logs:
            assert log["event_type"] == "MANUAL_FAILOVER"
            assert "Manual routing trigger applied" in log["message"]


def test_challenger_sqlite_sync_outage_stress(backend_runner):
    """
    Stress-test SQLite database syncing during multiple outage simulations.
    Perform 5 consecutive database outage cycles.
    For each cycle:
    1. Lock the primary database 'test_e2e.db'.
    2. Verify backend status changes to 'degraded' and database_status is 'fallback'.
    3. Trigger a manual failover request (using a unique IP to bypass rate limit)
       so it logs a MANUAL_FAILOVER event to the fallback database.
    4. Unlock the primary database.
    5. Verify backend status recovers to 'healthy' and database_status is 'primary'.
    6. Verify that the logged MANUAL_FAILOVER event is successfully synced to the primary database.
    """
    primary_db_file = "test_e2e.db"
    
    # Warm up: Verify initial state is primary/healthy
    resp = requests.get(f"{BASE_URL}/api/v1/health/failover", headers=HEADERS, timeout=15)
    assert resp.status_code == 200
    assert resp.json()["database_status"] == "primary"
    
    for cycle in range(5):
        # 1. Lock primary DB
        simulator = DBOutageSimulator(primary_db_file)
        try:
            simulator.lock()
            
            # Wait for backend to transition to degraded/fallback
            # We use high timeout to survive event loop blocking from background sync checks
            fallback_detected = False
            for _ in range(40):
                try:
                    resp = requests.get(f"{BASE_URL}/api/v1/health/failover?t={time.time()}", headers=HEADERS, timeout=15)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data["database_status"] == "fallback":
                            fallback_detected = True
                            break
                except Exception:
                    pass
                time.sleep(0.3)
            assert fallback_detected, f"Cycle {cycle}: Backend failed to fallback to fallback database"
            
            # 2. Trigger manual routing change from a unique client IP
            # (To avoid hitting rate limits since we are doing 5 cycles)
            headers = {**HEADERS, "X-Forwarded-For": f"10.10.10.{cycle}"}
            payload = {"primary_enabled": False, "backup_enabled": True}
            resp = requests.post(f"{BASE_URL}/api/v1/failover/trigger", json=payload, headers=headers, timeout=15)
            assert resp.status_code == 200, f"Cycle {cycle}: Trigger request failed with status {resp.status_code}"
            
        finally:
            # 3. Unlock primary DB
            simulator.unlock()
            
        # 4. Wait for database restoration
        restored = False
        for _ in range(40):
            try:
                resp = requests.get(f"{BASE_URL}/api/v1/health/failover?t={time.time()}", headers=HEADERS, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if data["database_status"] == "primary" and data["status"] == "healthy":
                        restored = True
                        break
            except Exception:
                pass
            time.sleep(0.3)
        assert restored, f"Cycle {cycle}: Backend failed to restore primary database connection"
        
        # 5. Verify the logs were synced to primary
        # Poll for up to 8s to give the background sync task time to propagate
        # the MANUAL_FAILOVER log from fallback DB to primary DB.
        expected_msg_part = "Primary enabled=False, Backup enabled=True"
        found = False
        for _ in range(40):
            try:
                logs_resp = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS, timeout=15)
                if logs_resp.status_code == 200:
                    logs = logs_resp.json()
                    messages = [log["message"] for log in logs]
                    if any(expected_msg_part in msg for msg in messages):
                        found = True
                        break
            except Exception:
                pass
            time.sleep(0.2)
        assert found, f"Cycle {cycle}: Expected log message '{expected_msg_part}' not found in synced logs after polling"
