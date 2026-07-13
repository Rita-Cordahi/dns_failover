# DNS Failover Monitor & Control System

A high-performance, glassmorphic monitoring dashboard and API manager designed to orchestrate multi-region database and DNS failover with Cloudflare, equipped with automatic Discord and Brevo SMTP alerting. 

Designed to run 24/7 on permanently free cloud services ($0 Investment).

---

## Architecture Overview

- **Backend**: FastAPI + SQLAlchemy database-aware health checks (`/api/v1/health/failover`).
- **Frontend**: Premium HTML5/CSS3 glassmorphic client utilizing visibility state polling.
- **Alerting Engine**: Stateful health transition notifier via Discord Webhooks and Brevo Email SMTP APIs.
- **Automation Script**: Idempotent Cloudflare load balancer pools and monitors setup script.

---

## 24/7 Permanent Free-Tier Hosting Guide ($0 Investment)

### 1. Persistent PostgreSQL Database (Supabase)
Supabase offers 2 free PostgreSQL projects permanently.
1. Sign up on [Supabase](https://supabase.com/).
2. Create a new database project.
3. Retrieve your connection string from **Project Settings -> Database**. Ensure you use the pooled connection string (port `6543`) for production.
4. Set the `DATABASE_URL` environment variable to this connection string in your backend deployment.

### 2. FastAPI Backend Server (Render)
Render offers a free tier for Web Services.
1. Create a free account on [Render](https://render.com/).
2. Create a new **Web Service** and connect your GitHub repository containing this codebase.
3. Select **Docker** as the Runtime (it will automatically build using the included `Dockerfile`).
4. In the **Environment** tab, add the following variables:
   - `API_TOKEN`: A secure token to protect your endpoints (e.g. `your-secure-token`).
   - `DATABASE_URL`: Your Supabase connection string.
   - `FALLBACK_DATABASE_URL`: `sqlite:///./fallback.db` (local SQLite fallback on Render's disk).
   - `CLOUDFLARE_API_TOKEN`: Your Cloudflare API Token.
   - `CLOUDFLARE_ACCOUNT_ID`: Your Cloudflare Account ID.
   - `CLOUDFLARE_ZONE_ID`: Your Cloudflare Zone ID.
   - `DISCORD_WEBHOOK_URL`: Discord webhook URL to get health state alerts.
   - `BREVO_API_KEY`: Brevo SMTP API Key to receive alert emails.
   - `ALERT_EMAIL_TO`: Your email address.
   - `ALERT_EMAIL_FROM`: Sender email configured in Brevo.
5. Deploy.

### 3. Keep-Alive Configuration (UptimeRobot)
Render Web Services sleep after 15 minutes of inactivity. To keep it awake 24/7 permanently for free:
1. Create a free account on [UptimeRobot](https://uptimerobot.com/).
2. Create a new Monitor:
   - **Monitor Type**: HTTPS
   - **Friendly Name**: DNS Failover Backend
   - **URL (or IP)**: `https://your-render-app.onrender.com/api/v1/health/failover`
   - **Interval**: Every 5 minutes (well within Render's 15-minute timeout window).
   - **Headers**: Add `Authorization: Bearer your-secure-token` as a request header to pass auth.
3. This keeps the backend server fully active, listening, and running checks 24/7!

---

## Local Development & Docker Compose

### Prerequisites
- Docker & Docker Compose installed.

### Start the Stack
1. Clone the project repository.
2. Build and run the container locally:
   ```bash
   docker-compose up --build
   ```
3. Open `http://localhost:8000` in your web browser.
4. Click the key icon (🔑) in the top-right header and enter the API token: `supersecretapitoken`.

---

## Verification Testing

You can execute the automated unit and E2E test suite locally by running:
```bash
$env:PYTHONPATH="."
pytest
```
All tests verify endpoint correctness, Cloudflare client fallbacks, and alerting triggers.
