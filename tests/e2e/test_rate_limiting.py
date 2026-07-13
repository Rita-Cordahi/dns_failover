import asyncio
import time
import pytest
import requests

BASE_URL = "http://127.0.0.1:8000"
HEADERS = {"Authorization": "Bearer test-secret-key"}

@pytest.fixture(autouse=True)
def delay_between_tests():
    time.sleep(3.5)
    yield

def test_rate_limiting_happy_path(backend_runner):
    """
    Happy path: Make fewer requests than the limit (e.g., 3 requests in the 2s window)
    and ensure they all succeed (200).
    """
    for _ in range(3):
        resp = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
        assert resp.status_code == 200

def test_rate_limiting_enforcement(backend_runner):
    """
    Enforcement: Make 6 requests in rapid succession. The first 5 should succeed (200),
    and the 6th should return 429 (Too Many Requests).
    """
    status_codes = []
    for _ in range(6):
        resp = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
        status_codes.append(resp.status_code)
    
    assert status_codes[:5] == [200, 200, 200, 200, 200]
    assert status_codes[5] == 429

def test_rate_limiting_client_ip_isolation(backend_runner):
    """
    Client IP Isolation: Requests from client IP 1.1.1.1 should be rate-limited after 5 requests.
    Requests from client IP 2.2.2.2 should still succeed.
    """
    headers_ip1 = {**HEADERS, "X-Forwarded-For": "1.1.1.1"}
    headers_ip2 = {**HEADERS, "X-Forwarded-For": "2.2.2.2"}
    
    # Client 1 hits the limit
    for _ in range(5):
        resp = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=headers_ip1)
        assert resp.status_code == 200
        
    resp_limit_ip1 = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=headers_ip1)
    assert resp_limit_ip1.status_code == 429
    
    # Client 2 should still succeed
    resp_ip2 = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=headers_ip2)
    assert resp_ip2.status_code == 200

@pytest.mark.asyncio
async def test_rate_limiting_concurrency(backend_runner):
    """
    Concurrency: Send 10 requests concurrently. Exactly 5 should succeed (200)
    and 5 should be rate limited (429).
    """
    def make_req():
        return requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)

    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(None, make_req) for _ in range(10)]
    responses = await asyncio.gather(*tasks)
    
    status_codes = [resp.status_code for resp in responses]
    success_count = status_codes.count(200)
    limited_count = status_codes.count(429)
    
    assert success_count == 5
    assert limited_count == 5

def test_rate_limiting_temporal_reset(backend_runner):
    """
    Temporal Reset: Make 5 requests (limit hit), wait for the window (2s) to expire,
    and verify a subsequent request succeeds.
    """
    for _ in range(5):
        resp = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
        assert resp.status_code == 200
        
    # Verify next request is rate limited
    resp_limit = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
    assert resp_limit.status_code == 429
    
    # Wait for the window (2 seconds) to reset
    time.sleep(3.5)
    
    # Should succeed now
    resp_reset = requests.get(f"{BASE_URL}/api/v1/failover/logs", headers=HEADERS)
    assert resp_reset.status_code == 200
