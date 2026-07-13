import sqlite3
import time
import pytest
import requests

BASE_URL = "http://127.0.0.1:8000"
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

def safe_request(method, url, **kwargs):
    for _ in range(5):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429:
            time.sleep(2.1)
            continue
        return resp
    return resp

def test_db_failover_and_sync(backend_runner):
    # 1. Initial State Check - verify we are on primary database and healthy
    resp = safe_request("GET", f"{BASE_URL}/api/v1/health/failover?t={time.time()}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["database_status"] == "primary"

    # 2. Simulate Primary DB Outage using DBOutageSimulator
    simulator = DBOutageSimulator("test_e2e.db")
    try:
        simulator.lock()
        
        # 3. Verify backend detects outage and falls back to fallback.db
        # We try multiple times as backend might have connection pool retry delays
        fallback_detected = False
        for _ in range(5):
            resp = safe_request("GET", f"{BASE_URL}/api/v1/health/failover?t={time.time()}", headers=HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                if data["database_status"] == "fallback":
                    fallback_detected = True
                    break
            time.sleep(0.5)
            
        assert fallback_detected, "Backend failed to fallback to fallback database"
        assert data["status"] == "degraded"

        # 4. Perform a manual routing change during the database outage
        payload = {"primary_enabled": False, "backup_enabled": True}
        trigger_resp = safe_request("POST", f"{BASE_URL}/api/v1/failover/trigger", json=payload, headers=HEADERS)
        assert trigger_resp.status_code == 200
        assert trigger_resp.json()["status"] == "success"

    finally:
        # 5. Restore database (release exclusive write lock)
        simulator.unlock()

    # 6. Verify database restoration and automatic SQLite-to-cloud/primary sync
    # Backend should automatically re-establish connection to primary
    restored = False
    for _ in range(10):
        resp = safe_request("GET", f"{BASE_URL}/api/v1/health/failover?t={time.time()}", headers=HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            if data["database_status"] == "primary":
                restored = True
                break
        time.sleep(0.5)
        
    assert restored, "Backend failed to restore primary database connection"
    assert data["status"] == "healthy"

    # 7. Check that logs from the fallback DB (such as the DB_FALLBACK_ACTIVE log)
    # were successfully synced to the primary database
    logs_resp = safe_request("GET", f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    
    event_types = [log["event_type"] for log in logs]
    assert "DB_FALLBACK_ACTIVE" in event_types, "DB_FALLBACK_ACTIVE event was not synced to primary DB"

