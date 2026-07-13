import time
import threading
import concurrent.futures
from unittest.mock import patch
import pytest
from backend.main import SlidingWindowRateLimiter

def test_rate_limiter_concurrency_same_ip():
    """
    Spawns multiple threads making requests for the same IP concurrently.
    Verifies that the rate limiter is thread-safe and enforces the limit exactly.
    """
    limiter = SlidingWindowRateLimiter()
    limit = 50
    window = 10
    client_ip = "192.168.1.1"

    # We will spawn 100 concurrent requests.
    # Exactly 50 should succeed (return False for is_rate_limited), and 50 should be blocked (return True).
    results = []
    results_lock = threading.Lock()

    def make_request():
        # Call is_rate_limited
        limited = limiter.is_rate_limited(client_ip, limit, window)
        with results_lock:
            results.append(limited)

    # Use ThreadPoolExecutor to run concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(make_request) for _ in range(100)]
        concurrent.futures.wait(futures)

    # Verify counts
    allowed_count = results.count(False)
    blocked_count = results.count(True)

    assert len(results) == 100
    assert allowed_count == limit, f"Expected exactly {limit} allowed requests, got {allowed_count}"
    assert blocked_count == 50, f"Expected exactly 50 blocked requests, got {blocked_count}"


def test_rate_limiter_concurrency_multiple_ips():
    """
    Spawns multiple threads making requests for different IPs concurrently.
    Verifies that client rate limiting is independent and thread-safe.
    """
    limiter = SlidingWindowRateLimiter()
    limit = 3
    window = 10
    num_ips = 40
    requests_per_ip = 5

    # Total requests = 200.
    # For each IP, 3 requests should be allowed, and 2 blocked.
    # Total allowed = 120, total blocked = 80.
    results = []
    results_lock = threading.Lock()

    def make_request(ip):
        limited = limiter.is_rate_limited(ip, limit, window)
        with results_lock:
            results.append((ip, limited))

    # Construct execution arguments
    tasks = []
    for i in range(num_ips):
        ip = f"10.0.0.{i}"
        for _ in range(requests_per_ip):
            tasks.append(ip)

    # Shuffle or run in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(make_request, ip) for ip in tasks]
        concurrent.futures.wait(futures)

    # Group results by IP
    by_ip = {}
    for ip, limited in results:
        if ip not in by_ip:
            by_ip[ip] = []
        by_ip[ip].append(limited)

    # Verify each IP individually
    assert len(by_ip) == num_ips
    for ip, ip_results in by_ip.items():
        allowed = ip_results.count(False)
        blocked = ip_results.count(True)
        assert len(ip_results) == requests_per_ip
        assert allowed == limit, f"IP {ip} had {allowed} allowed requests instead of {limit}"
        assert blocked == (requests_per_ip - limit), f"IP {ip} had {blocked} blocked requests instead of {requests_per_ip - limit}"


def test_rate_limiter_sliding_window_boundaries():
    """
    Verifies that requests are allowed/blocked precisely at sliding window boundaries
    by mocking time.time().
    """
    limiter = SlidingWindowRateLimiter()
    limit = 2
    window = 10
    client_ip = "172.16.0.1"

    # Start at simulated time 100.0
    with patch("time.time") as mock_time:
        # Request 1 at t=100.0
        mock_time.return_value = 100.0
        assert limiter.is_rate_limited(client_ip, limit, window) is False  # Allowed

        # Request 2 at t=105.0
        mock_time.return_value = 105.0
        assert limiter.is_rate_limited(client_ip, limit, window) is False  # Allowed

        # Request 3 at t=108.0 (Both previous requests are in window [98.0, 108.0])
        mock_time.return_value = 108.0
        assert limiter.is_rate_limited(client_ip, limit, window) is True   # Blocked

        # Request 4 at t=110.0 (Window is [100.0, 110.0]. The first request is at 100.0.
        # Since timestamps[0] < window_start (100.0 < 100.0 is False), it is NOT popped.
        # So it is still rate limited.)
        mock_time.return_value = 110.0
        assert limiter.is_rate_limited(client_ip, limit, window) is True   # Blocked

        # Request 5 at t=110.0001 (Window is [100.0001, 110.0001].
        # First request at 100.0 is now older than 100.0001, so it gets popped.
        # Timestamps becomes [105.0], len = 1 < 2. Request should be allowed.)
        mock_time.return_value = 110.0001
        assert limiter.is_rate_limited(client_ip, limit, window) is False  # Allowed (Timestamps: [105.0, 110.0001])

        # Request 6 at t=112.0 (Window is [102.0, 112.0]. Timestamps in window: 105.0, 110.0001. Limit reached.)
        mock_time.return_value = 112.0
        assert limiter.is_rate_limited(client_ip, limit, window) is True   # Blocked

        # Request 7 at t=115.0001 (Window is [105.0001, 115.0001].
        # Timestamp 105.0 is popped. Timestamp 110.0001 remains. Len = 1 < 2. Allowed.)
        mock_time.return_value = 115.0001
        assert limiter.is_rate_limited(client_ip, limit, window) is False  # Allowed (Timestamps: [110.0001, 115.0001])


def test_rate_limiter_memory_growth_and_leak():
    """
    Verifies that the rate limiter prevents memory leak and evicts inactive client IPs.
    """
    limiter = SlidingWindowRateLimiter()
    limit = 5
    window = 10

    # 1. Verify initial size is 0
    assert len(limiter.requests) == 0

    # 2. Simulate requests from 1000 unique client IPs
    for i in range(1000):
        ip = f"192.168.100.{i}"
        limiter.is_rate_limited(ip, limit, window)

    # Verify that there are 1000 entries in the requests dict initially
    assert len(limiter.requests) == 1000

    # 3. Simulate a long passage of time (e.g. 1 hour later)
    # The sliding window is only 10 seconds, so all request timestamps are ancient.
    future_time = time.time() + 3600.0
    with patch("time.time") as mock_time:
        mock_time.return_value = future_time

        # Try to make a request from one of the existing IPs
        # This will trigger the cleanup mechanism and pop/evict all inactive keys.
        limiter.is_rate_limited("192.168.100.0", limit, window)

        # Check if the list for that IP got cleared to just the new request (length 1)
        assert len(limiter.requests["192.168.100.0"]) == 1

        # Check the dictionary size. It should be 1 because all other 999 inactive
        # client IPs were evicted by the cleanup mechanism.
        assert len(limiter.requests) == 1, f"Expected dictionary size 1, got {len(limiter.requests)}"

        # Check that another inactive IP is no longer in the dictionary
        assert "192.168.100.1" not in limiter.requests
        assert "192.168.100.1" not in limiter.locks


def test_rate_limiter_concurrency_with_eviction():
    """
    Spawns concurrent requests while eviction/cleanup is triggered repeatedly.
    Verifies that the rate limiter is thread-safe and enforces the limit,
    even when locks are evicted concurrently.
    """
    limiter = SlidingWindowRateLimiter()
    limit = 10
    window = 0.05  # short window to encourage eviction
    client_ip = "192.168.2.2"

    results = []
    results_lock = threading.Lock()

    def make_request():
        # Periodically sleep to let window expire and trigger cleanup
        time.sleep(0.01)
        limited = limiter.is_rate_limited(client_ip, limit, window)
        with results_lock:
            results.append(limited)

    # We spawn 50 concurrent requests.
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(make_request) for _ in range(50)]
        concurrent.futures.wait(futures)

    # Verify counts
    assert len(results) == 50
    allowed_count = results.count(False)
    assert allowed_count >= limit, f"Expected at least {limit} allowed requests, got {allowed_count}"
