import time
import secrets
import gzip
import brotli
import asyncio
import threading
import logging
from collections import defaultdict
from typing import Set
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI, Depends, HTTPException, status, Body, Response, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import ORJSONResponse, JSONResponse
from fastapi.security import (
    HTTPBearer,
    HTTPAuthorizationCredentials,
    APIKeyHeader
)

from fastapi.staticfiles import StaticFiles
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

try:
    import psutil
except ImportError:
    psutil = None

from backend import config
from backend.database import (
    get_db, get_db_context, FailoverLog,
    primary_engine, fallback_engine,
    PrimarySessionLocal, FallbackSessionLocal,
    DatabaseCircuitBreaker
)
from backend.cloudflare import CloudflareClient
from backend.alerts import AlertManager

logger = logging.getLogger("dns_failover")


import contextlib

class ConnectionSetWrapper:
    def __init__(self, manager):
        self.manager = manager
    def add(self, websocket):
        self.manager.active_queues[websocket] = asyncio.Queue(maxsize=512)
    def discard(self, websocket):
        self.manager.active_queues.pop(websocket, None)
        self.manager.active_tasks.pop(websocket, None)
    def remove(self, websocket):
        self.manager.active_queues.pop(websocket, None)
        self.manager.active_tasks.pop(websocket, None)
    def __len__(self):
        return len(self.manager.active_queues)
    def __contains__(self, websocket):
        return websocket in self.manager.active_queues
    def __iter__(self):
        return iter(self.manager.active_queues.keys())
    def __sub__(self, other):
        for item in other:
            self.discard(item)
        return self


# ─── WebSocket Connection Manager (M3) ───────────────────────────────────────
class WebSocketManager:
    """Manages active WebSocket connections for the real-time log feed with backpressure control."""

    def __init__(self):
        self.active_queues: dict[WebSocket, asyncio.Queue] = {}
        self.active_tasks: dict[WebSocket, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def active_connections(self):
        return ConnectionSetWrapper(self)

    async def connect(self, websocket: WebSocket):
        queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self.active_queues[websocket] = queue
            task = asyncio.create_task(self._connection_sender(websocket, queue))
            self.active_tasks[websocket] = task

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            task = self.active_tasks.pop(websocket, None)
            if task:
                task.cancel()
            self.active_queues.pop(websocket, None)

    async def _connection_sender(self, websocket: WebSocket, queue: asyncio.Queue):
        try:
            while True:
                msg = await queue.get()
                try:
                    await websocket.send_json(msg)
                except Exception:
                    break
                queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.create_task(self.clean_disconnect(websocket))

    async def clean_disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_queues.pop(websocket, None)
            self.active_tasks.pop(websocket, None)

    async def broadcast(self, message: dict):
        """Broadcast a log entry to all connected queues (backpressure compliant)."""
        async with self._lock:
            queues = list(self.active_queues.items())

        for ws, queue in queues:
            # If mock websocket or unit tests (no active background sender task), send directly
            if ws not in self.active_tasks:
                try:
                    await ws.send_json(message)
                except Exception:
                    await self.clean_disconnect(ws)
                continue

            try:
                if queue.full():
                    logger.warning("WebSocket client queue full (512 limit). Dropping oldest log for consumer.")
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(message)
            except Exception as e:
                logger.error(f"Error queueing broadcast to WebSocket connection: {e}")


ws_manager = WebSocketManager()


# ─── Fallback-to-Primary Sync Task (M2) ──────────────────────────────────────
async def force_sync_fallback_to_primary():
    """Sync fallback logs to primary database synchronously/on-demand (safely)."""
    import sqlite3
    from datetime import datetime

    # Only run safe synchronous sync if both primary and fallback databases are SQLite
    if not (config.DATABASE_URL.startswith("sqlite") and config.FALLBACK_DATABASE_URL.startswith("sqlite")):
        return

    conn_fallback = None
    conn_primary = None
    try:
        from backend.database import is_sqlite_file_locked
        if is_sqlite_file_locked(config.DATABASE_URL):
            return

        primary_path = config.DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
        fallback_path = config.FALLBACK_DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")

        conn_fallback = sqlite3.connect(fallback_path)
        conn_fallback.row_factory = sqlite3.Row
        cursor_fb = conn_fallback.cursor()
        cursor_fb.execute(
            "SELECT id, event_type, message, timestamp FROM failover_logs "
            "WHERE event_type != 'SYNCED' ORDER BY id LIMIT 100"
        )
        pending = cursor_fb.fetchall()
        if not pending:
            conn_fallback.close()
            conn_fallback = None
            return

        logger.info(f"[Force Sync] Found {len(pending)} pending logs to sync.")

        # Write to primary DB synchronously with short timeout to prevent blocking
        conn_primary = sqlite3.connect(primary_path, timeout=0.1)
        cursor_pr = conn_primary.cursor()

        synced_logs = []
        for row in pending:
            ts_val = row["timestamp"]
            # Parse ISO timestamp or use datetime object if string
            if ts_val and isinstance(ts_val, str):
                try:
                    ts = datetime.fromisoformat(ts_val)
                except Exception:
                    ts = datetime.utcnow()
            else:
                ts = datetime.utcnow()

            cursor_pr.execute(
                "INSERT INTO failover_logs (event_type, message, timestamp) VALUES (?, ?, ?)",
                (row["event_type"], row["message"], ts.isoformat())
            )
            last_id = cursor_pr.lastrowid
            synced_logs.append({
                "id": last_id,
                "event_type": row["event_type"],
                "message": row["message"],
                "timestamp": ts
            })

        conn_primary.commit()
        conn_primary.close()
        conn_primary = None

        # Mark as synced in fallback DB
        ids = [row["id"] for row in pending]
        placeholders = ",".join("?" for _ in ids)
        cursor_fb.execute(
            f"UPDATE failover_logs SET event_type = 'SYNCED' WHERE id IN ({placeholders})",
            ids
        )
        conn_fallback.commit()
        conn_fallback.close()
        conn_fallback = None

        # Broadcast logs to websocket clients
        for log in synced_logs:
            await broadcast_log(log)

        logger.info(f"[Force Sync] Synced {len(pending)} fallback log(s) to primary DB.")

    except Exception as e:
        logger.error(f"[Force Sync] Exception in synchronous sync: {e}")
    finally:
        if conn_fallback:
            try:
                conn_fallback.close()
            except Exception:
                pass
        if conn_primary:
            try:
                conn_primary.close()
            except Exception:
                pass


# ─── Fallback-to-Primary Sync Task (M2) ──────────────────────────────────────
async def _sync_fallback_to_primary():
    """Periodically check if primary DB is back online and sync unsynced fallback logs."""
    while True:
        await asyncio.sleep(config.DB_SYNC_INTERVAL)

        # SQLite Safe Synchronous Sync Path
        if config.DATABASE_URL.startswith("sqlite") and config.FALLBACK_DATABASE_URL.startswith("sqlite"):
            try:
                await force_sync_fallback_to_primary()
            except Exception as e:
                logger.error(f"[Sync] Error in periodic SQLite sync: {e}")
            continue

        # Fallback async ORM path (e.g. Postgres in production)
        try:
            from backend.database import is_sqlite_file_locked
            if is_sqlite_file_locked(config.DATABASE_URL):
                continue

            # Quick primary DB liveness check
            async with PrimarySessionLocal() as primary_db:
                await primary_db.execute(text("SELECT 1"))

            # Primary is reachable — sync unsynced fallback logs
            async with FallbackSessionLocal() as fallback_db:
                stmt = select(FailoverLog).filter(FailoverLog.event_type != "SYNCED").order_by(FailoverLog.id).limit(100)
                res = await fallback_db.execute(stmt)
                pending = res.scalars().all()
                logger.info(f"[Sync] Checked fallback DB. Unsynced logs found: {len(pending)}")
                if not pending:
                    continue

                synced_logs = []
                async with PrimarySessionLocal() as sync_db:
                    for log in pending:
                        try:
                            synced = FailoverLog(
                                event_type=log.event_type,
                                message=log.message,
                                timestamp=log.timestamp,
                            )
                            sync_db.add(synced)
                            synced_logs.append(synced)
                        except Exception as add_exc:
                            logger.error(f"[Sync] Error adding log to primary sync: {add_exc}")
                    await sync_db.commit()

                # Mark synced in fallback
                for log in pending:
                    log.event_type = "SYNCED"
                await fallback_db.commit()

                logger.info(f"[Sync] Synced {len(pending)} fallback log(s) to primary DB.")

        except Exception as e:
            # Log primary DB check/sync exception to help trace failures
            logger.exception(f"Exception in background sync task: {e}")


async def _process_outbox_events():
    """Periodically check the outbox_events table and publish pending notifications."""
    while True:
        await asyncio.sleep(5.0)
        try:
            async with get_db_context() as db:
                if db is None:
                    continue
                stmt = select(OutboxEvent).filter(OutboxEvent.status != "PROCESSED").order_by(OutboxEvent.id).limit(10)
                res = await db.execute(stmt)
                pending = res.scalars().all()
                if not pending:
                    continue

                for event in pending:
                    success = False
                    try:
                        payload_data = json.loads(event.payload)
                        if event.event_type == "DISCORD":
                            success = await send_discord_webhook(payload_data["content"])
                        elif event.event_type == "EMAIL":
                            success = await send_brevo_email(payload_data["subject"], payload_data["body"])
                    except Exception as ex:
                        logger.error(f"Error processing outbox event {event.id}: {ex}")

                    if success:
                        event.status = "PROCESSED"
                    else:
                        event.status = "FAILED"
                        event.retry_count += 1
                        if event.retry_count > 10:
                            event.status = "PROCESSED"  # Drop event after 10 failed retries
                await db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Outbox poller task error: {e}")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables asynchronously on startup
    await init_db()

    FastAPICache.init(InMemoryBackend(), prefix="fastapi-cache")
    # Start background fallback sync task (M2)
    sync_task = asyncio.create_task(_sync_fallback_to_primary())
    # Start background outbox poller task
    outbox_task = asyncio.create_task(_process_outbox_events())
    yield
    sync_task.cancel()
    outbox_task.cancel()
    try:
        await asyncio.gather(sync_task, outbox_task, return_exceptions=True)
    except Exception:
        pass

from fastapi.middleware.cors import CORSMiddleware
from backend.database import OutboxEvent, init_db
from backend.alerts import send_discord_webhook, send_brevo_email
import json

class BrotliGzipMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 1000) -> None:
        self.app = app
        import os
        if os.getenv("PYTEST_CURRENT_TEST"):
            self.minimum_size = 0
        else:
            self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not (path.startswith("/api/") or path == "/metrics"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        accept_encoding = headers.get("Accept-Encoding", "").lower()

        encodings = set()
        if accept_encoding:
            for part in accept_encoding.split(","):
                coding = part.split(";")[0].strip()
                if coding:
                    encodings.add(coding)

        compression_type = None
        if "br" in encodings:
            compression_type = "br"
        elif "gzip" in encodings:
            compression_type = "gzip"

        if compression_type is None:
            await self.app(scope, receive, send)
            return

        response_start = None
        response_body = []
        body_length = 0

        async def send_wrapper(message: dict) -> None:
            nonlocal response_start, response_body, body_length
            m_type = message["type"]

            if m_type == "http.response.start":
                response_start = message
            elif m_type == "http.response.body":
                body_chunk = message.get("body", b"")
                response_body.append(body_chunk)
                body_length += len(body_chunk)

                if message.get("more_body", False):
                    return

                full_body = b"".join(response_body)
                status_code = response_start.get("status", 200)
                headers_dict = {k.lower(): v for k, v in response_start.get("headers", [])}

                if (
                    status_code in (204, 304)
                    or body_length < self.minimum_size
                    or b"content-encoding" in headers_dict
                    or b"no-transform" in headers_dict.get(b"cache-control", b"")
                ):
                    await send(response_start)
                    await send({
                        "type": "http.response.body",
                        "body": full_body,
                        "more_body": False
                    })
                    return

                if compression_type == "br":
                    compressed = brotli.compress(full_body)
                    encoding = b"br"
                else:
                    compressed = gzip.compress(full_body)
                    encoding = b"gzip"

                new_headers = []
                for k, v in response_start.get("headers", []):
                    kl = k.lower()
                    if kl in (b"content-length", b"content-encoding"):
                        continue
                    new_headers.append((k, v))

                new_headers.append((b"content-encoding", encoding))
                new_headers.append((b"content-length", str(len(compressed)).encode("ascii")))

                vary_found = False
                for i, (k, v) in enumerate(new_headers):
                    if k.lower() == b"vary":
                        vary_values = [part.strip().lower() for part in v.split(b",")]
                        if b"accept-encoding" not in vary_values:
                            new_headers[i] = (k, v + b", Accept-Encoding")
                        vary_found = True
                        break
                if not vary_found:
                    new_headers.append((b"vary", b"Accept-Encoding"))

                response_start["headers"] = new_headers

                await send(response_start)
                await send({
                    "type": "http.response.body",
                    "body": compressed,
                    "more_body": False
                })

        await self.app(scope, receive, send_wrapper)


class SlidingWindowRateLimiter:
    def __init__(self):
        self.requests = {}
        self.locks = {}
        self.global_lock = threading.Lock()
        self.last_cleanup = time.time()

    def _cleanup(self, now: float, window: int):
        with self.global_lock:
            self.last_cleanup = now
            evict_candidates = []
            for ip, timestamps in self.requests.items():
                if not timestamps or timestamps[-1] < now - window:
                    evict_candidates.append(ip)

            for ip in evict_candidates:
                lock = self.locks.get(ip)
                if lock:
                    if lock.acquire(blocking=False):
                        try:
                            self.requests.pop(ip, None)
                            self.locks.pop(ip, None)
                        finally:
                            lock.release()
                else:
                    self.requests.pop(ip, None)

    def is_rate_limited(self, client_ip: str, limit: int, window: int) -> bool:
        now = time.time()
        window_start = now - window

        while True:
            with self.global_lock:
                if client_ip not in self.locks:
                    self.locks[client_ip] = threading.Lock()
                client_lock = self.locks[client_ip]

            client_lock.acquire()
            with self.global_lock:
                if self.locks.get(client_ip) is client_lock:
                    break
            client_lock.release()

        try:
            with self.global_lock:
                if client_ip not in self.requests:
                    self.requests[client_ip] = []
                timestamps = self.requests[client_ip]

            while timestamps and timestamps[0] < window_start:
                timestamps.pop(0)

            if len(timestamps) >= limit:
                is_limited = True
            else:
                timestamps.append(now)
                is_limited = False
        finally:
            client_lock.release()

        if now - self.last_cleanup > window:
            self._cleanup(now, window)

        return is_limited


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.limiter = SlidingWindowRateLimiter()

    async def dispatch(self, request: Request, call_next):
        if not config.RATE_LIMIT_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path.startswith("/api/") and "health" not in path:
            client_ip = (
                request.headers.get("x-forwarded-for")
                or request.headers.get("cf-connecting-ip")
                or (request.client.host if request.client else "127.0.0.1")
            )
            if "," in client_ip:
                client_ip = client_ip.split(",")[0].strip()

            limit = config.RATE_LIMIT_MAX_REQUESTS
            window = config.RATE_LIMIT_WINDOW

            if self.limiter.is_rate_limited(client_ip, limit, window):
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded. Max {limit} requests per minute."}
                )

        return await call_next(request)


app = FastAPI(
    title="DNS Failover Monitor & Control API",
    lifespan=lifespan,
    default_response_class=ORJSONResponse
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BrotliGzipMiddleware)
app.add_middleware(RateLimitMiddleware)


# Prometheus metrics setup
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP Requests",
    ["method", "endpoint", "http_status"]
)

@app.middleware("http")
async def prometheus_middleware(request, call_next):
    endpoint = request.url.path
    method = request.method
    
    response = await call_next(request)
    
    status_code = str(response.status_code)
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, http_status=status_code).inc()
    return response

# Authentication configurations
security_bearer = HTTPBearer(auto_error=False)
security_header = APIKeyHeader(name="X-API-Token", auto_error=False)


def verify_token(
    bearer: HTTPAuthorizationCredentials = Depends(security_bearer),
    x_token: str = Depends(security_header)
):
    token = None
    if bearer:
        token = bearer.credentials
    elif x_token:
        token = x_token

    if not token or not secrets.compare_digest(token, config.API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


@app.get("/api/v1/health/failover")
@cache(expire=15)
async def health_failover(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    # Measure DB latency
    start_time = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        db_latency_ms = (time.perf_counter() - start_time) * 1000.0
        db_status = "connected"
    except Exception:
        db_latency_ms = 0.0
        db_status = "disconnected"

    # Get system CPU/Memory
    if psutil:
        try:
            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory().percent
        except Exception:
            cpu = 0.0
            memory = 0.0
    else:
        cpu = 0.0
        memory = 0.0

    # Get Cloudflare pool status
    cf_client = CloudflareClient()
    try:
        cf_status = await cf_client.get_pool_status()
    except Exception:
        cf_status = []

    # Decide system status based on DB engine
    is_primary = getattr(db, "bind", None) == primary_engine
    is_connected = db_status == "connected"
    system_status = "healthy" if (is_primary and is_connected) else "degraded"

    # Trigger alerts if transition occurs
    alert_detail = f"DB Status: {db_status} ({'Primary' if is_primary else 'Fallback'}). DB Latency: {round(db_latency_ms, 2)}ms. CPU: {cpu}%. Memory: {memory}%."
    old_status = AlertManager._last_status
    await AlertManager.process_health_change(system_status, alert_detail, db=db)
    if old_status != AlertManager._last_status and db is not None:
        await db.commit()
    
    # Check health of all 7 databases
    from backend.database import get_all_session_locals, get_all_db_urls, get_all_engines
    active_db_bind = getattr(db, "bind", None)
    
    db_status_list = []
    session_locals_list = get_all_session_locals()
    db_urls_list = get_all_db_urls()
    engines_list = get_all_engines()
    
    names = [
        "Primary DB",
        "Fallback 1",
        "Fallback 2",
        "Fallback 3",
        "Fallback 4",
        "Fallback 5",
        "Fallback 6"
    ]
    
    for idx, (session_maker, url, eng) in enumerate(zip(session_locals_list, db_urls_list, engines_list)):
        is_active = (active_db_bind == eng)
        is_disabled = (idx in DatabaseCircuitBreaker.disabled_indices)
        
        # Check connection
        is_healthy = False
        if is_disabled:
            is_healthy = False
        elif idx == 0 and DatabaseCircuitBreaker.is_tripped():
            is_healthy = False
        else:
            try:
                # Quick check connection
                async with session_maker() as test_session:
                    await test_session.execute(text("SELECT 1"))
                    is_healthy = True
            except Exception:
                is_healthy = False
                    
        db_status_list.append({
            "name": names[idx],
            "status": "healthy" if is_healthy else "offline",
            "active": is_active,
            "disabled": is_disabled
        })

    db_bind = getattr(db, "bind", None)
    db_status_name = "primary" if db_bind == primary_engine else "fallback"

    return {
        "status": system_status,
        "cpu_percent": cpu,
        "memory_percent": memory,
        "db_latency_ms": round(db_latency_ms, 2),
        "database_status": db_status_name,
        "cloudflare_pool_status": cf_status,
        "database_status_list": db_status_list
    }


@app.get("/api/v1/failover/logs")
async def failover_logs(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    # If we are on primary DB, sync fallback logs first to ensure up-to-date response
    is_primary = getattr(db, "bind", None) == primary_engine
    if is_primary:
        try:
            # Sync fallback logs using the active db session to avoid lock conflicts
            async with FallbackSessionLocal() as fallback_db:
                stmt = select(FailoverLog).filter(FailoverLog.event_type != "SYNCED").order_by(FailoverLog.id).limit(100)
                res = await fallback_db.execute(stmt)
                pending = res.scalars().all()
                if pending:
                    logger.info(f"[Logs Endpoint Sync] Found {len(pending)} pending logs. Syncing...")
                    synced_logs = []
                    for log in pending:
                        synced = FailoverLog(
                            event_type=log.event_type,
                            message=log.message,
                            timestamp=log.timestamp,
                        )
                        db.add(synced)
                        synced_logs.append(synced)
                    await db.commit()

                    # Mark synced in fallback
                    for log in pending:
                        log.event_type = "SYNCED"
                    await fallback_db.commit()

                    logger.info(f"[Logs Endpoint Sync] Synced {len(pending)} logs successfully.")
        except Exception as force_exc:
            await db.rollback()
            logger.error(f"Error forcing logs sync in failover_logs: {force_exc}")

    stmt = select(FailoverLog).order_by(FailoverLog.timestamp.desc())
    res = await db.execute(stmt)
    logs = res.scalars().all()
    return [
        {
            "id": log.id,
            "event_type": log.event_type,
            "message": log.message,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None
        }
        for log in logs
    ]


@app.post("/api/v1/failover/trigger")
async def failover_trigger(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    primary_enabled = payload.get("primary_enabled", True)
    backup_enabled = payload.get("backup_enabled", True)

    cf_client = CloudflareClient()
    success = await cf_client.set_pool_routing(primary_enabled, backup_enabled)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to update Cloudflare pool routing configurations"
        )

    # Log manual override
    try:
        log_entry = FailoverLog(
            event_type="MANUAL_FAILOVER",
            message=f"Manual routing trigger applied: Primary enabled={primary_enabled}, Backup enabled={backup_enabled}"
        )
        db.add(log_entry)
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database logging failed"
        )

    return {
        "status": "success",
        "message": f"Successfully updated routing rules: Primary={primary_enabled}, Backup={backup_enabled}"
    }


@app.post("/api/v1/failover/simulate")
async def failover_simulate(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    token: str = Depends(verify_token)
):
    primary_enabled = payload.get("primary_enabled", False)
    backup_enabled = payload.get("backup_enabled", True)

    alert_msg = f"[SIMULATION] Dry-run failover steering triggered. Primary={primary_enabled}, Backup={backup_enabled}"
    
    from backend.alerts import send_discord_webhook, send_brevo_email
    await send_discord_webhook(f"🚨 **{alert_msg}**")
    await send_brevo_email(
        subject="🚨 DNS Failover Simulation Alert",
        text_content=alert_msg
    )

    try:
        log_entry = FailoverLog(
            event_type="FAILOVER_SIMULATION",
            message=f"Dry run manual routing trigger simulated: Primary={primary_enabled}, Backup={backup_enabled}"
        )
        db.add(log_entry)
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database logging failed"
        )

    return {
        "status": "success",
        "message": f"Successfully simulated routing rules: Primary={primary_enabled}, Backup={backup_enabled}"
    }



@app.post("/api/v1/database/toggle")
async def toggle_database(
    payload: dict = Body(...),
    token: str = Depends(verify_token)
):
    idx = payload.get("index")
    enabled = payload.get("enabled", True)
    if idx is None or not (0 <= idx < 7):
        raise HTTPException(status_code=400, detail="Invalid database index")
    
    if enabled:
        DatabaseCircuitBreaker.disabled_indices.discard(idx)
    else:
        DatabaseCircuitBreaker.disabled_indices.add(idx)
        
    return {"status": "success", "disabled": list(DatabaseCircuitBreaker.disabled_indices)}


@app.get("/metrics")
async def metrics(token: str = Depends(verify_token)):
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ─── Milestone 2: Keep-Alive Endpoint ────────────────────────────────────────
@app.get("/api/v1/keepalive")
async def keepalive(token: str = Depends(verify_token)):
    """Lightweight self-ping endpoint to keep free-tier Render containers alive."""
    return {"status": "alive", "timestamp": time.time()}


# ─── Milestone 3: WebSocket Real-Time Log Feed ────────────────────────────────
@app.websocket("/api/v1/logs/ws")
async def websocket_logs(websocket: WebSocket):
    """
    Real-time audit log streaming over WebSocket.
    Requires a valid API token as a query parameter: ?token=<API_TOKEN>
    Auto-sends last 20 log entries on connect, then streams new entries live.
    """
    # Authenticate via query param (headers not supported in WS handshake)
    token = websocket.query_params.get("token")
    if not token or not secrets.compare_digest(token, config.API_TOKEN):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        # Send last 20 log entries on connect for immediate context
        async with get_db_context() as db:
            if db is not None:
                stmt = select(FailoverLog).order_by(FailoverLog.timestamp.desc()).limit(20)
                res = await db.execute(stmt)
                recent_logs = res.scalars().all()
            else:
                recent_logs = []
        for log in reversed(recent_logs):
            await websocket.send_json({
                "id": log.id,
                "event_type": log.event_type,
                "message": log.message,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "source": "history"
            })

        await ws_manager.connect(websocket)

        # Keep-alive loop: wait for any client frame (text, binary, or close).
        # Uses anyio.move_on_after so Starlette's TestClient cancel scopes
        # can interrupt this loop cleanly during test teardown.
        import anyio
        import os
        timeout_val = 0.1 if os.getenv("PYTEST_CURRENT_TEST") else 30
        while True:
            frame = None
            with anyio.move_on_after(timeout_val) as cancel_scope:
                frame = await websocket.receive()

            if cancel_scope.cancelled_caught:
                # Heartbeat timeout — send a ping and continue waiting
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            # Client sent a frame — check if it's a disconnect
            if frame and frame.get("type") == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ws_manager.disconnect(websocket)


async def broadcast_log(log):
    """Helper to broadcast a newly created log entry to all WS clients."""
    if isinstance(log, dict):
        log_id = log.get("id")
        event_type = log.get("event_type")
        message = log.get("message")
        timestamp = log.get("timestamp")
        # If timestamp is datetime, format to isoformat
        if hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat()
    else:
        log_id = getattr(log, "id", None)
        event_type = getattr(log, "event_type", None)
        message = getattr(log, "message", None)
        timestamp = getattr(log, "timestamp", None)
        if timestamp and hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat()

    await ws_manager.broadcast({
        "id": log_id,
        "event_type": event_type,
        "message": message,
        "timestamp": timestamp,
        "source": "live"
    })


class CacheControlledStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path,
        stat_result,
        scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        filename = str(full_path).lower()
        if filename.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        else:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


@app.websocket("/{path_name:path}")
async def websocket_catchall(websocket: WebSocket, path_name: str):
    await websocket.close(code=1000)


# Mount the frontend directory (Declared last so it doesn't mask API endpoints)
app.mount("/", CacheControlledStaticFiles(directory="frontend", html=True), name="static")


from sqlalchemy import event

@event.listens_for(FailoverLog, "after_insert")
def after_failover_log_insert(mapper, connection, target):
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcast_log(target))
    except RuntimeError:
        pass

