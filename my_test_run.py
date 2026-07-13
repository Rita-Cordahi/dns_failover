import os
import sys
import time
import subprocess
import requests
import asyncio
import websockets
import json

def run_tests():
    print("--- STARTING FORENSIC BEHAVIORAL VERIFICATION ---")
    
    # 1. Start mock server
    mock_server_path = os.path.abspath("tests/mock_server.py")
    mock_proc = subprocess.Popen(
        [sys.executable, mock_server_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1) # wait for start
    
    # 2. Start backend
    env = os.environ.copy()
    env["CLOUDFLARE_API_URL"] = "http://localhost:8001/client/v4"
    env["DISCORD_WEBHOOK_URL"] = "http://localhost:8001/discord/webhook"
    env["BREVO_API_URL"] = "http://localhost:8001/brevo"
    env["DATABASE_URL"] = "sqlite:///./my_test_primary.db"
    env["FALLBACK_DATABASE_URL"] = "sqlite:///./my_test_fallback.db"
    env["API_TOKEN"] = "test-secret-key"
    env["RATE_LIMIT_ENABLED"] = "True"
    env["RATE_LIMIT_MAX_REQUESTS"] = "5"
    env["RATE_LIMIT_WINDOW"] = "2"
    
    # Remove old DB files if any
    for f in ["my_test_primary.db", "my_test_primary.db-journal", "my_test_primary.db-wal", "my_test_primary.db-shm",
              "my_test_fallback.db", "my_test_fallback.db-journal", "my_test_fallback.db-wal", "my_test_fallback.db-shm"]:
        if os.path.exists(f):
            try: os.remove(f)
            except: pass

    backend_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2) # wait for backend to bind port 8000
    
    headers = {"Authorization": "Bearer test-secret-key"}
    
    try:
        # Check keepalive
        res = requests.get("http://127.0.0.1:8000/api/v1/keepalive")
        print("Keepalive check status:", res.status_code, "body:", res.json())
        
        # Check compression
        res_comp = requests.get("http://127.0.0.1:8000/api/v1/health/failover", headers={**headers, "Accept-Encoding": "gzip"})
        print("Compression check status:", res_comp.status_code, "Encoding headers:", res_comp.headers.get("Content-Encoding"))
        
        # Check rate limiting happy path (5 requests should pass)
        print("Running rate limit happy path...")
        for i in range(5):
            res_rl = requests.get("http://127.0.0.1:8000/api/v1/failover/logs", headers=headers)
            print(f"Request {i+1} status:", res_rl.status_code)
            
        # 6th request should fail
        res_rl_6 = requests.get("http://127.0.0.1:8000/api/v1/failover/logs", headers=headers)
        print("6th request status (expected 429):", res_rl_6.status_code, "body:", res_rl_6.json())
        
        # Check temporal reset
        print("Waiting 2.5 seconds for rate limit to reset...")
        time.sleep(2.5)
        res_rl_reset = requests.get("http://127.0.0.1:8000/api/v1/failover/logs", headers=headers)
        print("Request after reset status (expected 200):", res_rl_reset.status_code)

        # Check WebSocket (Authorized)
        async def test_ws_auth():
            try:
                async with websockets.connect("ws://127.0.0.1:8000/api/v1/logs/ws?token=test-secret-key") as ws:
                    print("WS connection succeeded with token")
                    # Send a manual override request to trigger a log which should stream
                    res_trigger = requests.post(
                        "http://127.0.0.1:8000/api/v1/failover/trigger",
                        headers=headers,
                        json={"primary_enabled": True, "backup_enabled": False}
                    )
                    print("Trigger override response status:", res_trigger.status_code)
                    
                    # Receive from WS
                    for _ in range(5):
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        msg_data = json.loads(msg)
                        print("WS received msg:", msg_data)
                        if msg_data.get("event_type") == "MANUAL_FAILOVER":
                            print("WS streaming verified!")
                            break
            except Exception as e:
                import traceback
                print("WS connection failed with error:")
                traceback.print_exc()

        # Check WebSocket (Unauthorized)
        async def test_ws_unauth():
            try:
                async with websockets.connect("ws://127.0.0.1:8000/api/v1/logs/ws?token=wrong-token") as ws:
                    await ws.recv()
                    print("WS connection with wrong token unexpectedly succeeded")
            except websockets.exceptions.InvalidStatus as e:
                print("WS connection with wrong token rejected correctly, status:", e.response.status_code)
            except Exception as e:
                print("WS connection with wrong token rejected with exception:", type(e), e)

        asyncio.run(test_ws_auth())
        asyncio.run(test_ws_unauth())

        # Check Database Fallback & Sync
        print("Simulating DB Outage by locking sqlite database...")
        # To lock my_test_primary.db, we open it in exclusive mode or lock it
        primary_db_path = "my_test_primary.db"
        
        # Let's perform a query first to make sure database is initialized
        res_health = requests.get("http://127.0.0.1:8000/api/v1/health/failover", headers=headers)
        print("Pre-outage health DB status:", res_health.json().get("database_status"))
        
        # Now lock it using python file locking
        import msvcrt
        db_file = open(primary_db_path, "r+b")
        try:
            # Lock the file
            msvcrt.locking(db_file.fileno(), msvcrt.LK_NBLCK, 1)
            print("Successfully locked primary DB file.")
            
            # Make a query and see if it falls back
            # It will take up to SQLite's busy timeout to return, let's measure time
            start = time.time()
            res_fallback = requests.get("http://127.0.0.1:8000/api/v1/health/failover", headers=headers)
            duration = time.time() - start
            print(f"Fallback request completed in {duration:.2f}s, status: {res_fallback.status_code}")
            if res_fallback.status_code == 200:
                print("Fallback response body:", res_fallback.json())
                
        except Exception as lock_err:
            print("Could not lock database or make fallback request:", lock_err)
        finally:
            msvcrt.locking(db_file.fileno(), msvcrt.LK_UNLCK, 1)
            db_file.close()
            print("Unlocked primary DB file.")
            
        # Verify it recovers
        time.sleep(1)
        res_recovered = requests.get("http://127.0.0.1:8000/api/v1/health/failover", headers=headers)
        print("Recovered database status:", res_recovered.json().get("database_status"))
        
    finally:
        # Cleanup
        backend_proc.terminate()
        mock_proc.terminate()
        try: backend_proc.wait(timeout=2)
        except: backend_proc.kill()
        try: mock_proc.wait(timeout=2)
        except: mock_proc.kill()
        
        for f in ["my_test_primary.db", "my_test_primary.db-journal", "my_test_primary.db-wal", "my_test_primary.db-shm",
                  "my_test_fallback.db", "my_test_fallback.db-journal", "my_test_fallback.db-wal", "my_test_fallback.db-shm"]:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

if __name__ == "__main__":
    run_tests()
