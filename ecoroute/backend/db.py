from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional, Tuple

logger = logging.getLogger("ecoroute.db")

BASE_DIR = Path(__file__).resolve().parent

def is_test_mode() -> bool:
    return bool(
        os.getenv("TEST_MODE")
        or os.getenv("PYTEST_CURRENT_TEST")
        or os.getenv("ECOROUTE_DB", "").strip()
    )


TEST_MODE = is_test_mode()


def get_sqlite_path() -> Path:
    sqlite_path = os.getenv("ECOROUTE_DB", "").strip()
    if sqlite_path:
        return Path(sqlite_path)
    return BASE_DIR / "ecoroute.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UnifiedConnection:
    """Wraps either a SQLite connection or a psycopg PostgreSQL connection with a uniform API."""

    def __init__(self, raw_conn: Any, is_postgres: bool = False):
        self.raw_conn = raw_conn
        self.is_postgres = is_postgres

    def _convert_query(self, query: str) -> str:
        """Converts ANSI SQL with ? placeholders to %s for PostgreSQL."""
        if not self.is_postgres:
            return query
        # Replace ? with %s when talking to PostgreSQL
        return re.sub(r"\?", "%s", query)

    def execute(self, query: str, params: Tuple[Any, ...] | list[Any] = ()) -> Any:
        converted_query = self._convert_query(query)
        cursor = self.raw_conn.cursor()
        if params:
            cursor.execute(converted_query, params)
        else:
            cursor.execute(converted_query)
        return cursor

    def executescript(self, script: str) -> None:
        if not self.is_postgres:
            self.raw_conn.executescript(script)
        else:
            # psycopg executes multiple statements in one execute
            cursor = self.raw_conn.cursor()
            cursor.execute(script)

    def commit(self) -> None:
        self.raw_conn.commit()

    def rollback(self) -> None:
        self.raw_conn.rollback()

    def close(self) -> None:
        self.raw_conn.close()


def row_to_dict(cursor_or_row: Any) -> Optional[dict[str, Any]]:
    if cursor_or_row is None:
        return None
    if isinstance(cursor_or_row, dict):
        return dict(cursor_or_row)
    if hasattr(cursor_or_row, "keys"):
        return {k: cursor_or_row[k] for k in cursor_or_row.keys()}
    return dict(cursor_or_row)


def fetchone_dict(cursor: Any) -> Optional[dict[str, Any]]:
    row = cursor.fetchone()
    if row is None:
        return None
    if hasattr(cursor, "description") and cursor.description:
        cols = [d[0] for d in cursor.description]
        if isinstance(row, (tuple, list)):
            return dict(zip(cols, row))
    return row_to_dict(row)


def fetchall_dict(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if hasattr(cursor, "description") and cursor.description:
        cols = [d[0] for d in cursor.description]
        result = []
        for r in rows:
            if isinstance(r, (tuple, list)):
                result.append(dict(zip(cols, r)))
            else:
                result.append(row_to_dict(r))
        return result
    return [row_to_dict(r) for r in rows]


_PG_AVAILABLE = False
try:
    import psycopg
    from psycopg.rows import dict_row
    _PG_AVAILABLE = True
except ImportError:
    try:
        import psycopg2 as psycopg
        from psycopg2.extras import RealDictCursor
        _PG_AVAILABLE = True
    except ImportError:
        _PG_AVAILABLE = False


def _connect_postgres(url: str) -> Any:
    # Ensure URL protocol is recognized
    clean_url = url
    if clean_url.startswith("postgres://"):
        clean_url = "postgresql://" + clean_url[len("postgres://"):]

    # Try connecting with 5s timeout
    conn = psycopg.connect(clean_url, autocommit=False)
    return conn


@contextmanager
def db_connection() -> Generator[UnifiedConnection, None, None]:
    """Yields an active database connection within an atomic transaction.
    Falls back to SQLite if PostgreSQL is unavailable or fails.
    """
    conn: Optional[UnifiedConnection] = None
    use_pg = False

    # Check if we should attempt PostgreSQL
    db_url = os.getenv("DATABASE_URL", "").strip()
    is_test = is_test_mode()

    if db_url and not is_test:
        if not _PG_AVAILABLE:
            raise RuntimeError(
                "DATABASE_URL is configured but the PostgreSQL driver is unavailable."
            )
        try:
            raw_conn = _connect_postgres(db_url)
            conn = UnifiedConnection(raw_conn, is_postgres=True)
        except Exception as exc:
            logger.warning(
                "Configured PostgreSQL connection failed (%s); SQLite fallback is disabled.",
                type(exc).__name__,
            )
            raise RuntimeError(
                "Configured PostgreSQL database connection failed; SQLite fallback is disabled."
            ) from None

    if conn is None:
        # SQLite connection
        sqlite_path = get_sqlite_path()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        raw_conn = sqlite3.connect(sqlite_path, timeout=10.0)
        raw_conn.row_factory = sqlite3.Row
        conn = UnifiedConnection(raw_conn, is_postgres=False)

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_migrations() -> None:
    """Safe, additive schema migrations for requests, approvals, bookings, traces, test_runs, and mock sources."""
    with db_connection() as conn:
        if conn.is_postgres:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    city TEXT,
                    pincode TEXT,
                    max_fee REAL NOT NULL,
                    items_json TEXT,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    approved INTEGER NOT NULL,
                    accept_fee_exception INTEGER NOT NULL DEFAULT 0,
                    approved_plan_json TEXT NOT NULL,
                    approved_version TEXT NOT NULL DEFAULT '1.0',
                    approved_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    request_id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    approved_snapshot_json TEXT,
                    submitted_at TEXT NOT NULL,
                    verified_at TEXT,
                    verification_evidence TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                CREATE TABLE IF NOT EXISTS agent_traces (
                    id TEXT PRIMARY KEY,
                    request_id TEXT,
                    step INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    evidence_json TEXT,
                    error_json TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_runs (
                    id SERIAL PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    explanation TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mock_recycler_sources (
                    recycler_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    content_html TEXT,
                    extracted_json TEXT,
                    retrieved_at TEXT NOT NULL
                );
                """
            )
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    item_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    city TEXT,
                    pincode TEXT,
                    max_fee REAL NOT NULL,
                    items_json TEXT,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id TEXT PRIMARY KEY,
                    approved INTEGER NOT NULL,
                    accept_fee_exception INTEGER NOT NULL DEFAULT 0,
                    approved_plan_json TEXT NOT NULL,
                    approved_version TEXT NOT NULL DEFAULT '1.0',
                    approved_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                CREATE TABLE IF NOT EXISTS bookings (
                    request_id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    approved_snapshot_json TEXT,
                    submitted_at TEXT NOT NULL,
                    verified_at TEXT,
                    verification_evidence TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(request_id) REFERENCES requests(id)
                );
                CREATE TABLE IF NOT EXISTS agent_traces (
                    id TEXT PRIMARY KEY,
                    request_id TEXT,
                    step INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    evidence_json TEXT,
                    error_json TEXT,
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    explanation TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mock_recycler_sources (
                    recycler_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    content_html TEXT,
                    extracted_json TEXT,
                    retrieved_at TEXT NOT NULL
                );
                """
            )

        # Additive migration for existing tables if columns are missing
        _add_column_if_missing(conn, "requests", "city", "TEXT")
        _add_column_if_missing(conn, "requests", "pincode", "TEXT")
        _add_column_if_missing(conn, "requests", "items_json", "TEXT")
        _add_column_if_missing(conn, "approvals", "accept_fee_exception", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "approvals", "approved_version", "TEXT DEFAULT '1.0'")
        _add_column_if_missing(conn, "bookings", "idempotency_key", "TEXT")
        _add_column_if_missing(conn, "bookings", "approved_snapshot_json", "TEXT")
        _add_column_if_missing(conn, "bookings", "verified", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "test_runs", "explanation", "TEXT")

        # Normalize legacy statuses to lowercase standard lifecycle
        # draft -> awaiting_approval -> approved -> requested -> verified
        _normalize_legacy_statuses(conn)


def _add_column_if_missing(conn: UnifiedConnection, table: str, column: str, col_type: str) -> None:
    if not conn.is_postgres:
        try:
            cursor = conn.execute(f"PRAGMA table_info({table});")
            existing_cols = {r[1] for r in cursor.fetchall()}
            if column not in existing_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type};")
        except Exception as exc:
            logger.warning("Could not add column %s to %s: %s", column, table, exc)
    else:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type};")
        except Exception as exc:
            logger.warning("Could not add column %s to %s in PostgreSQL: %s", column, table, exc)


def _normalize_legacy_statuses(conn: UnifiedConnection) -> None:
    """Safely normalizes legacy uppercase status values into the required lowercase lifecycle."""
    status_map = {
        "DRAFT": "draft",
        "APPROVAL_REQUIRED": "awaiting_approval",
        "APPROVED": "approved",
        "BOOKED": "requested",
        "VERIFIED": "verified",
        "COLLECTED": "requested",  # Never allow 'collected' status to exist
        "NO_MATCH": "unresolved",
    }
    for old_s, new_s in status_map.items():
        try:
            conn.execute("UPDATE requests SET status = ? WHERE status = ?", (new_s, old_s))
            conn.execute("UPDATE bookings SET status = ? WHERE status = ?", (new_s, old_s))
        except Exception:
            pass
    for table in ("requests", "bookings"):
        conn.execute(
            f"UPDATE {table} SET status = ? WHERE LOWER(status) = ?",
            ("requested", "collected"),
        )


def init_db() -> None:
    run_migrations()
