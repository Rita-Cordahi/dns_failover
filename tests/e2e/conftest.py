import sys
import os
import time
import subprocess
import pytest
import requests

def cleanup_db():
    db_files = [
        "test_e2e.db",
        "test_e2e.db-journal",
        "test_e2e.db-shm",
        "test_e2e.db-wal",
        "fallback.db",
        "fallback.db-journal",
        "fallback.db-shm",
        "fallback.db-wal"
    ]
    for f in db_files:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

def kill_port_owner(port):
    try:
        import psutil
        for proc in psutil.process_iter():
            try:
                for conn in proc.connections(kind='inet'):
                    if conn.laddr.port == port:
                        proc.kill()
                        proc.wait(timeout=2)
            except Exception:
                pass
    except Exception:
        pass


@pytest.fixture(scope="session")
def backend_runner(mock_server):
    cleanup_db()
    
    # Environment variables for the backend
    env = os.environ.copy()
    if "PYTEST_CURRENT_TEST" in env:
        del env["PYTEST_CURRENT_TEST"]
    env["CLOUDFLARE_API_URL"] = "http://localhost:8001/client/v4"
    env["DISCORD_WEBHOOK_URL"] = "http://localhost:8001/discord/webhook"
    env["BREVO_API_URL"] = "http://localhost:8001/brevo"
    env["DATABASE_URL"] = "sqlite:///./test_e2e.db"
    env["API_TOKEN"] = "test-secret-key"
    env["API_KEY"] = "test-secret-key"
    env["RATE_LIMIT_LIMIT"] = "5"
    env["RATE_LIMIT_WINDOW"] = "2"
    env["RATE_LIMIT_MAX_REQUESTS"] = "0"
    env["CLOUDFLARE_API_TOKEN"] = "test-cf-token"
    env["CLOUDFLARE_ACCOUNT_ID"] = "test-account-id"
    env["CLOUDFLARE_ZONE_ID"] = "test-zone-id"
    env["CLOUDFLARE_EMAIL"] = "test-email@example.com"
    env["DB_SYNC_INTERVAL"] = "0.1"
    # Ensure port 8000 is not in use/TIME_WAIT before starting
    kill_port_owner(8000)
    import socket
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", 8000))
                break
            except OSError:
                time.sleep(0.1)

    # Run uvicorn in a subprocess
    stdout_file = open("uvicorn_stdout.log", "w")
    stderr_file = open("uvicorn_stderr.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000"],
        env=env,
        stdout=stdout_file,
        stderr=stderr_file
    )
    
    # Wait for the backend to start
    url = "http://127.0.0.1:8000/api/v1/health/failover"
    headers = {"Authorization": "Bearer test-secret-key"}
    start_time = time.time()
    success = False
    while time.time() - start_time < 10:
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                success = True
                break
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.1)
        
    if not success:
        proc.terminate()
        raise RuntimeError("Backend failed to start on 127.0.0.1:8000.")
        
    yield proc
    
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        
    try:
        stdout_file.close()
        stderr_file.close()
    except Exception:
        pass
    cleanup_db()


@pytest.fixture(scope="function", autouse=True)
def reset_db_state():
    import sqlite3
    import time
    time.sleep(2.2)
    for db_file in ["test_e2e.db", "fallback.db"]:
        if os.path.exists(db_file):
            conn = None
            try:
                conn = sqlite3.connect(db_file)
                cursor = conn.cursor()
                # Check tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='failover_logs'")
                if cursor.fetchone():
                    cursor.execute("DELETE FROM failover_logs")
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_events'")
                if cursor.fetchone():
                    cursor.execute("DELETE FROM outbox_events")
                conn.commit()
            except Exception as e:
                print(f"Error resetting database {db_file}: {e}")
            finally:
                if conn:
                    conn.close()
