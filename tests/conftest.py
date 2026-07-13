import sys
import os
import time
import subprocess
import socket
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.main import app
from backend.database import Base
import backend.database
import backend.main


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


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True



@pytest.fixture(scope="session")
def mock_server():
    # Ensure port 8001 is not in use before starting
    kill_port_owner(8001)
    for _ in range(50):
        if not is_port_in_use(8001):
            break
        time.sleep(0.1)

    server_path = os.path.join(os.path.dirname(__file__), "mock_server.py")
    stdout_file = open("mock_server_stdout.log", "w")
    stderr_file = open("mock_server_stderr.log", "w")
    proc = subprocess.Popen(
        [sys.executable, server_path],
        stdout=stdout_file,
        stderr=stderr_file
    )

    # Wait for the mock server to become ready
    url = "http://127.0.0.1:8001/control/reset"
    start_time = time.time()
    success = False
    while time.time() - start_time < 10:
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
        raise RuntimeError("Mock server failed to start on 127.0.0.1:8001.")

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


@pytest.fixture(autouse=True)
def reset_mock_server(mock_server):
    # Retry reset for up to 5s in case mock server is still starting
    deadline = time.time() + 5.0
    while True:
        try:
            resp = requests.post("http://127.0.0.1:8001/control/reset", timeout=3)
            assert resp.status_code == 200
            return
        except requests.exceptions.ConnectionError:
            if time.time() >= deadline:
                pytest.skip("Mock server on port 8001 unavailable — skipping test")
            time.sleep(0.2)



# Setup test in-memory SQLite engines with StaticPool to keep connection alive
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

test_primary_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
test_fallback_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

TestPrimarySession = async_sessionmaker(
    bind=test_primary_engine,
    class_=AsyncSession,
    expire_on_commit=False
)
TestFallbackSession = async_sessionmaker(
    bind=test_fallback_engine,
    class_=AsyncSession,
    expire_on_commit=False
)


@pytest.fixture(autouse=True)
def setup_test_databases():
    # Helper to run async table setup synchronously
    async def create_tables():
        async with test_primary_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with test_fallback_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_tables():
        async with test_primary_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        async with test_fallback_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.run(create_tables())

    # Patch the sessions and engines in database.py and main.py
    orig_primary_session = backend.database.PrimarySessionLocal
    orig_fallback_session = backend.database.FallbackSessionLocal
    orig_primary_engine = backend.database.primary_engine
    orig_fallback_engine = backend.database.fallback_engine
    orig_main_primary = backend.main.primary_engine

    backend.database.PrimarySessionLocal = TestPrimarySession
    backend.database.FallbackSessionLocal = TestFallbackSession
    backend.database.primary_engine = test_primary_engine
    backend.database.fallback_engine = test_fallback_engine
    backend.main.primary_engine = test_primary_engine

    yield

    # Restore original sessions and engines
    backend.database.PrimarySessionLocal = orig_primary_session
    backend.database.FallbackSessionLocal = orig_fallback_session
    backend.database.primary_engine = orig_primary_engine
    backend.database.fallback_engine = orig_fallback_engine
    backend.main.primary_engine = orig_main_primary

    asyncio.run(drop_tables())


@pytest.fixture
def client():
    # Use context manager to trigger FastAPI startup/shutdown events
    with TestClient(app) as c:
        # Clear FastAPI Cache to avoid cross-test contamination
        from fastapi_cache import FastAPICache
        try:
            if (
                hasattr(FastAPICache, "_backend")
                and hasattr(FastAPICache._backend, "_cache")
            ):
                FastAPICache._backend._cache.clear()
        except Exception:
            pass
        yield c
