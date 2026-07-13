import os
import re
import contextlib
from datetime import datetime
import json
from sqlalchemy import (
    text, Column, Integer, String, DateTime, Text, event
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import sqlalchemy.pool
from backend import config

Base = declarative_base()


class FailoverLog(Base):
    __tablename__ = "failover_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(50), nullable=False)
    message = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(50), nullable=False)  # 'DISCORD' or 'EMAIL'
    payload = Column(Text, nullable=False)  # JSON-encoded payload
    status = Column(String(20), default="PENDING")  # 'PENDING', 'PROCESSED', 'FAILED'
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


def make_async_url(url: str) -> str:
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://")
    elif url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://")
    elif url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://")
    return url


# Helper to create engines
def _create_engine_helper(url: str):
    return create_async_engine(
        make_async_url(url),
        **(
            {"poolclass": sqlalchemy.pool.NullPool}
            if url.startswith("sqlite")
            else {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_recycle": 1800,
                "pool_pre_ping": True
            }
        )
    )

def _create_session_helper(engine):
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

# Individual engine definitions
primary_engine = _create_engine_helper(config.DATABASE_URL)
fallback_engine = _create_engine_helper(config.FALLBACK_DATABASE_URL)
fallback_engine_2 = _create_engine_helper(config.FALLBACK_DATABASE_URL_2)
fallback_engine_3 = _create_engine_helper(config.FALLBACK_DATABASE_URL_3)
fallback_engine_4 = _create_engine_helper(config.FALLBACK_DATABASE_URL_4)
fallback_engine_5 = _create_engine_helper(config.FALLBACK_DATABASE_URL_5)
fallback_engine_6 = _create_engine_helper(config.FALLBACK_DATABASE_URL_6)

# Individual session local definitions
PrimarySessionLocal = _create_session_helper(primary_engine)
FallbackSessionLocal = _create_session_helper(fallback_engine)
FallbackSessionLocal2 = _create_session_helper(fallback_engine_2)
FallbackSessionLocal3 = _create_session_helper(fallback_engine_3)
FallbackSessionLocal4 = _create_session_helper(fallback_engine_4)
FallbackSessionLocal5 = _create_session_helper(fallback_engine_5)
FallbackSessionLocal6 = _create_session_helper(fallback_engine_6)

# Helper lists mapped dynamically (essential for conftest patch overrides)
def get_all_engines():
    return [
        primary_engine,
        fallback_engine,
        fallback_engine_2,
        fallback_engine_3,
        fallback_engine_4,
        fallback_engine_5,
        fallback_engine_6,
    ]

def get_all_session_locals():
    return [
        PrimarySessionLocal,
        FallbackSessionLocal,
        FallbackSessionLocal2,
        FallbackSessionLocal3,
        FallbackSessionLocal4,
        FallbackSessionLocal5,
        FallbackSessionLocal6,
    ]

def get_all_db_urls():
    return [
        config.DATABASE_URL,
        config.FALLBACK_DATABASE_URL,
        config.FALLBACK_DATABASE_URL_2,
        config.FALLBACK_DATABASE_URL_3,
        config.FALLBACK_DATABASE_URL_4,
        config.FALLBACK_DATABASE_URL_5,
        config.FALLBACK_DATABASE_URL_6,
    ]

# Apply SQLite optimization pragmas (WAL mode, cache, mmap, temp_store) and BEGIN IMMEDIATE to all engines
def _setup_sqlite_listeners(engine, url):
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-65536")  # 64MB Cache
            cursor.execute("PRAGMA mmap_size=268435456")  # 256MB mmap
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()
            
        @event.listens_for(engine.sync_engine, "begin")
        def do_begin(conn):
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")

for _eng, _url in zip(get_all_engines(), get_all_db_urls()):
    _setup_sqlite_listeners(_eng, _url)



async def init_db():
    try:
        async with primary_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"Could not initialize primary database tables: {e}")

    try:
        async with fallback_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        print(f"Could not initialize fallback database tables: {e}")


log_listeners = []
def register_log_listener(callback):
    log_listeners.append(callback)
def sanitize_connection_error(error: Exception) -> str:
    err_msg = str(error)
    def repl(match):
        scheme = match.group(1)
        user = match.group(2)
        password = match.group(3)
        if password:
            return f"{scheme}***:***@"
        else:
            return f"{scheme}***@"
    return re.sub(r'([a-zA-Z0-9+._-]+://)([^:@\s]+)(?::([^@\s]+))?@', repl, err_msg)


def is_sqlite_file_locked(url: str) -> bool:
    if not url.startswith("sqlite://") or ":memory:" in url:
        return False
    filepath = url.replace("sqlite:///", "").replace("sqlite://", "")
    if not os.path.exists(filepath):
        return False
    try:
        with open(filepath, "r+b") as f:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    res = False
                except OSError:
                    res = True
            else:
                import fcntl
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    res = False
                except OSError:
                    res = True
        return res
    except (IOError, PermissionError) as e:
        return True


class DatabaseCircuitBreaker:
    consecutive_failures = 0
    tripped = False
    tripped_time = 0.0
    trip_threshold = 5
    cooldown_period = 60.0  # seconds
    disabled_indices = set()

    @classmethod
    def record_failure(cls):
        cls.consecutive_failures += 1
        if cls.consecutive_failures >= cls.trip_threshold and not cls.tripped:
            import time
            cls.tripped = True
            cls.tripped_time = time.time()
            return True
        return False

    @classmethod
    def record_success(cls):
        was_tripped = cls.tripped
        cls.consecutive_failures = 0
        cls.tripped = False
        cls.tripped_time = 0.0
        return was_tripped

    @classmethod
    def is_tripped(cls) -> bool:
        if cls.tripped:
            import time
            if time.time() - cls.tripped_time > cls.cooldown_period:
                return False
            return True
        return False


async def get_db():
    """
    Standard generator function for FastAPI Depends, supporting a 7-tier fallback chain.
    """
    db = None
    active_idx = -1
    primary_exc = None
    circuit_tripped_just_now = False
    circuit_reset_just_now = False

    active_session_locals = get_all_session_locals()
    active_db_urls = get_all_db_urls()

    for idx, session_maker in enumerate(active_session_locals):
        # Skip if manually disabled via the UI simulation
        if idx in DatabaseCircuitBreaker.disabled_indices:
            if idx == 0:
                primary_exc = Exception("Primary database is manually disabled from the control panel.")
            continue

        # Skip primary database if its circuit breaker is tripped
        if idx == 0 and DatabaseCircuitBreaker.is_tripped():
            primary_exc = Exception("Primary database connection pool circuit breaker is tripped (Cooldown active).")
            continue

        locked = is_sqlite_file_locked(active_db_urls[idx])
        if locked:
            if idx == 0:
                primary_exc = Exception("Primary SQLite database file is locked.")
            continue

        try:
            db = session_maker()
            await db.execute(text("SELECT 1"))
            
            # Connected successfully!
            active_idx = idx
            
            # If we successfully reconnected to primary, reset circuit breaker
            if idx == 0:
                circuit_reset_just_now = DatabaseCircuitBreaker.record_success()
                if circuit_reset_just_now:
                    reset_log = FailoverLog(
                        event_type="CIRCUIT_BREAKER_RESET",
                        message="Database connection pool circuit breaker reset. Primary connection recovered."
                    )
                    db.add(reset_log)
                    await db.commit()
                    for callback in log_listeners:
                        try:
                            callback(reset_log)
                        except Exception:
                            pass
            break
        except Exception as e:
            if db:
                await db.close()
                db = None
            if idx == 0:
                primary_exc = e
                circuit_tripped_just_now = DatabaseCircuitBreaker.record_failure()

    # If primary failed, log appropriate warnings to the active fallback database
    if active_idx > 0 and db:
        try:
            if circuit_tripped_just_now:
                trip_log = FailoverLog(
                    event_type="CIRCUIT_BREAKER_TRIPPED",
                    message="Primary database connection failed 5 times consecutively. Tripping circuit breaker. Bypassing primary DB for 60 seconds."
                )
                db.add(trip_log)
                
                fallback_log = FailoverLog(
                    event_type="DB_FALLBACK_ACTIVE",
                    message=f"Primary database connection failed: {sanitize_connection_error(primary_exc)}. Routing to fallback_{active_idx}."
                )
                db.add(fallback_log)
                await db.commit()
                
                for callback in log_listeners:
                    try:
                        callback(trip_log)
                        callback(fallback_log)
                    except Exception:
                        pass
            else:
                log_event = FailoverLog(
                    event_type="DB_FALLBACK_ACTIVE",
                    message=f"Primary database connection failed: {sanitize_connection_error(primary_exc)}. Routing to fallback_{active_idx}."
                )
                db.add(log_event)
                await db.commit()
                for callback in log_listeners:
                    try:
                        callback(log_event)
                    except Exception:
                        pass
        except Exception as log_exc:
            await db.rollback()
            print(f"Failed to log DB_FALLBACK_ACTIVE to active fallback DB: {log_exc}")

    if not db:
        raise Exception("All 7 database tiers failed to connect.")

    try:
        yield db
    except Exception:
        if db:
            await db.rollback()
        raise
    finally:
        if db:
            await db.close()


# Context manager version
get_db_context = contextlib.asynccontextmanager(get_db)
