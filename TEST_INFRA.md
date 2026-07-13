# E2E Test Infra: Multi-Region DNS Failover Monitor & Control Dashboard

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on implementation design details.
- Methodology: Category-Partition + Boundary Value Analysis + Pairwise Combinatorial Testing + Real-World Workload Testing.
- The test harness runs a mock server to intercept all external HTTP traffic to Cloudflare, Discord, and Brevo, enabling verification of API requests, notification triggers, and manual overrides without hitting production services.

## Feature Inventory
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | Health Monitor Endpoint | R1 (health check API) | 5 | 5 | ✓ |
| 2 | Cloudflare Load Balancer integration | R1 (fetch active pool status) | 5 | 5 | ✓ |
| 3 | Alerting Engine | R1 (Discord/Email alerts) | 5 | 5 | ✓ |
| 4 | Manual Force Failover | R2 (manual override controls) | 5 | 5 | ✓ |
| 5 | Web Dashboard UI | R2 (display stats, charts, logs) | 5 | 5 | ✓ |
| 6 | API Security & SQLite Fallback | R3/R4 (API Key protection, SQLite) | 5 | 5 | ✓ |

## Test Architecture
- **Test Runner**: `pytest` executing tests in `tests/e2e/`.
- **Mock Server**: A python-based Mock Server (running on `localhost:8001`) simulating:
  - Cloudflare Load Balancer APIs (`/accounts/.../load_balancers/...`)
  - Discord webhook delivery (`/discord/...`)
  - Brevo email delivery (`/brevo/...`)
- **Control Interface**: The mock server exposes `/control` endpoints for:
  - Mock state injection (e.g., simulating database error, network timeout, active failover).
  - Asserting sent alerts (checking payload structure, contents).
- **Execution**: Run backend on port `8000` as a subprocess or fixture, configured with API URL pointing to the mock server, then run E2E pytest suite.

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | System Initialization & Auto-Setup | F1, F2, F6 | Low |
| 2 | Primary Region Outage & Auto-Failover Alerting | F1, F2, F3, F5 | High |
| 3 | Manual Overriding Failover & Recovery | F1, F2, F4, F5 | High |
| 4 | Multi-Fault Cascade Recovery (DB & Cloudflare down) | F1, F2, F3, F6 | High |
| 5 | Unauthorized Manual Overrides & Logging | F4, F5, F6 | Medium |

## Coverage Thresholds
- Tier 1: ≥30 test cases (5 per feature)
- Tier 2: ≥30 test cases (5 per feature boundary)
- Tier 3: ≥6 test cases (pairwise combinations)
- Tier 4: ≥5 real-world scenarios
