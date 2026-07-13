# Test Readiness - Phase 2 Optimizations E2E Test Suite

## Test Philosophy
- **Requirement-Driven**: All E2E tests are written against functional requirements to verify end-to-end behavior of the DNS failover monitor, including rate limiting, database failover, and WebSocket log streaming.
- **Opaque-box Verification**: The E2E tests interact with the backend service through standard protocols (HTTP, WebSockets) and file system state (SQLite databases) without relying on internal function calls.
- **Resilient Mocking**: We mock external dependencies (Cloudflare API, Discord webhooks, Brevo API) using an independent HTTP mock server, while the backend database and API are tested as a running system.

## Test Architecture
- **Backend Subprocess**: Uvicorn server spawned at `http://127.0.0.1:8000` inside a fixture.
- **Mock Server**: Independent mock server at `http://127.0.0.1:8001` to simulate Cloudflare, Discord, and Brevo services.
- **Test Runner**: Pytest framework with `pytest-asyncio` for async test cases (WebSockets and concurrent rate limiting).
- **Outage Simulation**: Acquires exclusive locks on database files using `sqlite3` to simulate regional/network database outages.

## Feature Checklist
- [x] Initial E2E setup verification (Cloudflare configuration setup, billing error paths)
- [x] Sliding Window Rate Limiting (Happy path, enforcement, client IP isolation, concurrency, temporal reset)
- [x] Database Failover & Sync (Primary DB outage lock, manual routing during outage, db restoration, SQLite-to-cloud sync)
- [x] WebSocket Log Streaming (Unauthorized connection, successful streaming, disconnect & reconnect within 50s)

## Test Inventory & Coverage Table
| Test File | Test Case | Target Feature | Type | Expected Result |
|-----------|-----------|----------------|------|-----------------|
| `test_setup.py` | `test_setup_first_run` | Setup / Cloudflare API | Integration | PASS |
| `test_setup.py` | `test_setup_second_run_updates_existing` | Setup / Cloudflare API | Integration | PASS |
| `test_setup.py` | `test_setup_billing_error` | Billing error paths | Integration | PASS |
| `test_rate_limiting.py` | `test_rate_limiting_happy_path` | Sliding Window Rate Limiting | E2E | XFAIL (Pending Implementation) |
| `test_rate_limiting.py` | `test_rate_limiting_enforcement` | Rate Limiting Enforcement | E2E | XFAIL (Pending Implementation) |
| `test_rate_limiting.py` | `test_rate_limiting_client_ip_isolation` | Client IP Isolation (X-Forwarded-For) | E2E | XFAIL (Pending Implementation) |
| `test_rate_limiting.py` | `test_rate_limiting_concurrency` | Concurrent Request Enforcements | E2E (Async) | XFAIL (Pending Implementation) |
| `test_rate_limiting.py` | `test_rate_limiting_temporal_reset` | Rate Limit Temporal Reset | E2E | XFAIL (Pending Implementation) |
| `test_db_failover_sync.py` | `test_db_failover_and_sync` | DB Outage, Failover Routing, Sync | E2E | XFAIL (Pending Implementation) |
| `test_websocket_logs.py` | `test_websocket_unauthorized` | Log Streaming Authentication | E2E (Async) | XFAIL (Pending Implementation) |
| `test_websocket_logs.py` | `test_websocket_streaming` | Real-time WebSocket Log Streaming | E2E (Async) | XFAIL (Pending Implementation) |
| `test_websocket_logs.py` | `test_websocket_disconnect_reconnect` | WebSocket Session Resiliency | E2E (Async) | XFAIL (Pending Implementation) |

## Expected Run Command
Run the E2E test suite using the following command:
```bash
python -m pytest tests/e2e/ -v
```
Note: Ensure the mock server is running before executing tests if you run them outside of pytest's auto-managed session context. When run via pytest, the mock server and backend runner are automatically managed via session/test fixtures.
