import os
import sys
import time
import subprocess
import socket
import pytest
import requests

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

class TestMockServerAPI:
    @pytest.fixture(autouse=True)
    def run_server(self):
        # Forcefully clear port 8002 before starting to avoid collisions
        # with other concurrent test processes or lingering subprocesses
        try:
            import psutil
            for proc in psutil.process_iter():
                try:
                    for conn in proc.connections(kind='inet'):
                        if conn.laddr.port == 8002:
                            proc.kill()
                            proc.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass

        # Ensure port 8002 is not in use before starting
        for _ in range(50):
            if not is_port_in_use(8002):
                break
            time.sleep(0.1)
            
        server_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_server.py")
        env = os.environ.copy()
        env["MOCK_SERVER_PORT"] = "8002"
        proc = subprocess.Popen(
            [sys.executable, server_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Wait for the mock server to become ready
        url = "http://127.0.0.1:8002/control/reset"
        start_time = time.time()
        success = False
        while time.time() - start_time < 5:
            try:
                resp = requests.post(url)
                if resp.status_code == 200:
                    success = True
                    break
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.1)
            
        if not success:
            proc.terminate()
            raise RuntimeError("Failed to start mock server on port 8002")
            
        yield proc
        
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


    def test_cloudflare_monitor_crud(self):
        base_url = "http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/monitors"
        
        # 1. GET (empty list initially)
        r = requests.get(base_url)
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["result"] == []
        
        # 2. POST (create)
        payload = {"description": "Test Monitor", "path": "/health"}
        r = requests.post(base_url, json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        monitor = data["result"]
        assert monitor["description"] == "Test Monitor"
        assert "id" in monitor
        monitor_id = monitor["id"]
        
        # 3. GET (verify list)
        r = requests.get(base_url)
        assert r.status_code == 200
        assert len(r.json()["result"]) == 1
        assert r.json()["result"][0]["id"] == monitor_id
        
        # 4. PUT (update)
        updated_payload = {"description": "Updated Monitor", "path": "/new-health"}
        r = requests.put(f"{base_url}/{monitor_id}", json=updated_payload)
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["result"]["description"] == "Updated Monitor"
        assert data["result"]["id"] == monitor_id

        # 5. PUT (non-existent monitor -> 404)
        r = requests.put(f"{base_url}/monitor-nonexistent", json=updated_payload)
        assert r.status_code == 404

    def test_cloudflare_pool_crud(self):
        base_url = "http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/pools"
        
        # 1. GET (empty list)
        r = requests.get(base_url)
        assert r.status_code == 200
        assert r.json()["result"] == []
        
        # 2. POST (create)
        payload = {"name": "test-pool", "origins": []}
        r = requests.post(base_url, json=payload)
        assert r.status_code == 200
        pool = r.json()["result"]
        assert pool["name"] == "test-pool"
        pool_id = pool["id"]
        
        # 3. GET specific pool details (defaults to healthy=True)
        r = requests.get(f"{base_url}/{pool_id}")
        assert r.status_code == 200
        assert r.json()["result"]["healthy"] is True
        
        # 4. GET health endpoint
        r = requests.get(f"{base_url}/{pool_id}/health")
        assert r.status_code == 200
        assert r.json()["result"]["healthy"] is True
        
        # 5. PUT (update)
        r = requests.put(f"{base_url}/{pool_id}", json={"name": "updated-pool", "origins": []})
        assert r.status_code == 200
        assert r.json()["result"]["name"] == "updated-pool"
        
        # 6. PUT non-existent -> 404
        r = requests.put(f"{base_url}/pool-nonexistent", json={"name": "bad"})
        assert r.status_code == 404

    def test_cloudflare_load_balancer_crud(self):
        base_url = "http://127.0.0.1:8002/client/v4/zones/test-zone/load_balancers"
        
        # 1. GET (empty)
        r = requests.get(base_url)
        assert r.status_code == 200
        assert r.json()["result"] == []
        
        # 2. POST
        payload = {"name": "lb.example.com", "default_pools": []}
        r = requests.post(base_url, json=payload)
        assert r.status_code == 200
        lb = r.json()["result"]
        assert lb["name"] == "lb.example.com"
        lb_id = lb["id"]
        
        # 3. PUT
        r = requests.put(f"{base_url}/{lb_id}", json={"name": "lb-new.example.com", "default_pools": []})
        assert r.status_code == 200
        assert r.json()["result"]["name"] == "lb-new.example.com"
        
        # 4. PUT non-existent -> 404
        r = requests.put(f"{base_url}/lb-nonexistent", json={"name": "bad"})
        assert r.status_code == 404

    def test_other_integrations(self):
        # Discord Webhook Mock
        r = requests.post("http://127.0.0.1:8002/discord/webhook-url")
        assert r.status_code == 204
        
        # Brevo Email Mock
        r = requests.post("http://127.0.0.1:8002/brevo/v3/smtp/email")
        assert r.status_code == 201
        assert "messageId" in r.json()
        
        r = requests.post("http://127.0.0.1:8002/v3/smtp/email")
        assert r.status_code == 201

    def test_control_failures_timeout(self):
        # Set cloudflare timeout failure
        r = requests.post("http://127.0.0.1:8002/control/failures", json={
            "cloudflare": {"timeout": True}
        })
        assert r.status_code == 200
        
        # Verify call to GET monitors hangs and raises 504 Gateway Timeout
        start = time.time()
        try:
            r = requests.get("http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/monitors", timeout=2)
            assert False, "Should have timed out"
        except requests.exceptions.Timeout:
            pass
        assert time.time() - start >= 1.9

    def test_control_failures_error_code(self):
        # Set custom cloudflare error code
        r = requests.post("http://127.0.0.1:8002/control/failures", json={
            "cloudflare": {
                "error_code": 1211,
                "message": "Subscription plan limit exceeded",
                "status_code": 403
            }
        })
        assert r.status_code == 200
        
        # Verify GET monitors returns 403 with formatted error structure
        r = requests.get("http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/monitors")
        assert r.status_code == 403
        data = r.json()
        assert data["success"] is False
        assert len(data["errors"]) == 1
        assert data["errors"][0]["code"] == 1211
        assert data["errors"][0]["message"] == "Subscription plan limit exceeded"

    def test_control_history_and_reset(self):
        # 1. Reset
        r = requests.post("http://127.0.0.1:8002/control/reset")
        assert r.status_code == 200
        
        # 2. Make some API requests
        requests.post("http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/monitors", json={"description": "M1"})
        requests.post("http://127.0.0.1:8002/client/v4/accounts/test-acc/load_balancers/pools", json={"name": "P1"})
        
        # 3. Get history
        r = requests.get("http://127.0.0.1:8002/control/history")
        assert r.status_code == 200
        data = r.json()
        assert len(data["history"]) == 2
        assert len(data["monitors"]) == 1
        assert len(data["pools"]) == 1
        
        # 4. Set pool status
        pool_id = list(data["pools"].keys())[0]
        r = requests.post(f"http://127.0.0.1:8002/control/cloudflare/pools/{pool_id}/status", json={"healthy": False})
        assert r.status_code == 200
        
        # 5. Verify status updated in history/state
        r = requests.get("http://127.0.0.1:8002/control/history")
        assert r.json()["pool_statuses"][pool_id]["healthy"] is False
        
        # 6. Reset and verify history is empty
        r = requests.post("http://127.0.0.1:8002/control/reset")
        assert r.status_code == 200
        r = requests.get("http://127.0.0.1:8002/control/history")
        assert r.json()["history"] == []
        assert r.json()["monitors"] == {}


def test_subprocess_lifecycle_robustness():
    # Start and stop uvicorn mock server subprocess multiple times to check for leaks/errors
    server_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tests", "mock_server.py")
    
    for i in range(5):
        freed_before = False
        for _ in range(50):
            if not is_port_in_use(8002):
                freed_before = True
                break
            time.sleep(0.1)
        assert freed_before, f"Port 8002 still in use before iteration {i}"
        
        env = os.environ.copy()
        env["MOCK_SERVER_PORT"] = "8002"
        proc = subprocess.Popen(
            [sys.executable, server_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Wait for the mock server to start
        url = "http://127.0.0.1:8002/control/reset"
        start_time = time.time()
        success = False
        while time.time() - start_time < 3:
            try:
                resp = requests.post(url)
                if resp.status_code == 200:
                    success = True
                    break
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.1)
            
        assert success, f"Failed to start uvicorn subprocess on iteration {i}"
        
        # Stop subprocess cleanly
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            
        # Give OS a moment to free the socket
        freed_after = False
        for _ in range(50):
            if not is_port_in_use(8002):
                freed_after = True
                break
            time.sleep(0.1)
        assert freed_after, f"Port 8002 was not freed after iteration {i}"

