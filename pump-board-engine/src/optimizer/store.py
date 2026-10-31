"""Database-backed persistence for the paper-only optimizer.

The service uses MySQL in production through ``DATABASE_URL`` and PyMySQL.  The
same small SQL surface supports an injected SQLite connection for deterministic
software tests.  Nothing in this module reads or writes checked-in board,
ledger, fixture, or strategy files after the optional one-time seed input.
"""
from __future__ import annotations

import copy
import json
import os
import ssl
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse


FORWARD_NAMESPACE = "optimizer-forward-v1"
SCHEMA_VERSION = 1


class StoreUnavailable(RuntimeError):
    """No durable optimizer database is configured or available."""


class RevisionConflict(ValueError):
    """A durable strategy update lost its optimistic-concurrency race."""


@dataclass(frozen=True)
class DatabaseInfo:
    dialect: str
    durable: bool


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


class DatabaseStore:
    """A narrow DB-API adapter with additive migrations and JSON state records."""

    def __init__(self, database_url: str | None = None, *, connection: Any = None) -> None:
        self._state_cache: dict[str, tuple[float, Any]] = {}
        if connection is not None:
            self.dialect = "sqlite"
            self._connection = connection
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._lock = threading.RLock()
            self._database_url = "sqlite-injected"
            return
        if not database_url:
            raise StoreUnavailable("DATABASE_URL is required for the durable optimizer")
        self._database_url = database_url
        parsed = urlparse(database_url)
        scheme = parsed.scheme.lower()
        self._lock = threading.RLock()
        self._connection = None
        if scheme.startswith("mysql"):
            self.dialect = "mysql"
        elif scheme == "sqlite":
            self.dialect = "sqlite"
            # sqlite:////absolute/path and sqlite:///:memory: are intentionally
            # test/dev-only. Production code uses mysql:// via DATABASE_URL.
            raw_path = unquote(parsed.path or "")
            if raw_path == "/:memory:" or database_url.endswith(":memory:"):
                target = ":memory:"
            else:
                target = raw_path
                if target.startswith("//"):
                    target = target[1:]
                if not target:
                    raise StoreUnavailable("sqlite DATABASE_URL needs a database path")
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(target, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
        else:
            raise StoreUnavailable("DATABASE_URL must use mysql:// or sqlite://")

    @classmethod
    def from_env(cls) -> "DatabaseStore":
        return cls(os.environ.get("DATABASE_URL"))

    @property
    def info(self) -> DatabaseInfo:
        return DatabaseInfo(dialect=self.dialect, durable=self.dialect == "mysql")

    def _mysql_connection(self) -> Any:
        try:
            import pymysql  # type: ignore
        except ImportError as exc:  # pragma: no cover - image dependency guard
            raise StoreUnavailable("PyMySQL is required for mysql DATABASE_URL") from exc
        parsed = urlparse(self._database_url)
        database = (parsed.path or "/").lstrip("/")
        if not database:
            raise StoreUnavailable("mysql DATABASE_URL must include a database name")
        # Managed TiDB URLs commonly include ``?ssl=true``. PyMySQL accepts an
        # SSLContext directly; the default context verifies the server certificate
        # and hostname. Do not log the parsed DSN because it may contain a password.
        query = parse_qs(parsed.query, keep_blank_values=True)
        ssl_setting = query.get("ssl", [None])[-1]
        ssl_disabled = isinstance(ssl_setting, str) and ssl_setting.strip().lower() in {
            "0", "false", "no", "off", "disable", "disabled",
        }
        ssl_context = None
        if ssl_setting is not None and not ssl_disabled:
            ssl_context = ssl.create_default_context()
        tls_options: dict[str, Any] = {"ssl": ssl_context} if ssl_context is not None else {}
        return pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
            **tls_options,
        )

    def _sql(self, statement: str) -> str:
        return statement.replace("?", "%s") if self.dialect == "mysql" else statement

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """One serialized connection; avoid a fresh TLS handshake per state read."""
        with self._lock:
            if self.dialect == "mysql" and (self._connection is None or not getattr(self._connection, "open", False)):
                self._connection = self._mysql_connection()
            connection = self._connection
            if connection is None:
                raise StoreUnavailable("optimizer database connection is unavailable")
            cursor = connection.cursor()
            try:
                yield cursor
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                if self.dialect == "mysql":
                    try:
                        connection.close()
                    finally:
                        self._connection = None
                raise
            finally:
                cursor.close()

    def _execute(self, cursor: Any, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        cursor.execute(self._sql(statement), parameters)
        return cursor

    def migrate(self, seed_document: dict[str, Any] | None = None, *, now: float | None = None) -> bool:
        """Create only optimizer-owned tables and seed only a missing config row."""
        now = time.time() if now is None else now
        with self.transaction() as cur:
            if self.dialect == "mysql":
                statements = [
                    """CREATE TABLE IF NOT EXISTS optimizer_schema_versions (
                        version INT PRIMARY KEY, applied_at DOUBLE NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_strategy_config (
                        namespace VARCHAR(80) PRIMARY KEY, revision INT NOT NULL,
                        document_json LONGTEXT NOT NULL, updated_at DOUBLE NOT NULL,
                        source VARCHAR(40) NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_state (
                        namespace VARCHAR(80) NOT NULL, state_key VARCHAR(100) NOT NULL,
                        value_json LONGTEXT NOT NULL, updated_at DOUBLE NOT NULL,
                        PRIMARY KEY(namespace, state_key)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_leases (
                        lease_name VARCHAR(100) PRIMARY KEY, holder VARCHAR(128) NOT NULL,
                        expires_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_observations (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        namespace VARCHAR(80) NOT NULL, mint VARCHAR(128) NOT NULL,
                        observed_at DOUBLE NOT NULL, market_cap DOUBLE NOT NULL,
                        curve_pct DOUBLE NOT NULL, payload_json LONGTEXT NOT NULL,
                        INDEX optimizer_observation_time(namespace, observed_at),
                        INDEX optimizer_observation_mint(namespace, mint, observed_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_trades (
                        trade_id VARCHAR(100) PRIMARY KEY, namespace VARCHAR(80) NOT NULL,
                        status VARCHAR(16) NOT NULL, entry_at DOUBLE NOT NULL,
                        exit_at DOUBLE NULL, payload_json LONGTEXT NOT NULL,
                        INDEX optimizer_trade_exit(namespace, exit_at),
                        INDEX optimizer_trade_entry(namespace, entry_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_reviews (
                        review_id VARCHAR(100) PRIMARY KEY, namespace VARCHAR(80) NOT NULL,
                        review_at DOUBLE NOT NULL, boundary_at DOUBLE NOT NULL,
                        decision VARCHAR(32) NOT NULL, reason TEXT NOT NULL,
                        metrics_json LONGTEXT NOT NULL,
                        INDEX optimizer_review_time(namespace, review_at),
                        UNIQUE KEY optimizer_review_boundary(namespace, boundary_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                    """CREATE TABLE IF NOT EXISTS optimizer_audits (
                        audit_id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        namespace VARCHAR(80) NOT NULL, at DOUBLE NOT NULL,
                        action VARCHAR(40) NOT NULL, detail_json LONGTEXT NOT NULL,
                        INDEX optimizer_audit_time(namespace, at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
                ]
            else:
                statements = [
                    "CREATE TABLE IF NOT EXISTS optimizer_schema_versions (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS optimizer_strategy_config (namespace TEXT PRIMARY KEY, revision INTEGER NOT NULL, document_json TEXT NOT NULL, updated_at REAL NOT NULL, source TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS optimizer_state (namespace TEXT NOT NULL, state_key TEXT NOT NULL, value_json TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(namespace, state_key))",
                    "CREATE TABLE IF NOT EXISTS optimizer_leases (lease_name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS optimizer_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, mint TEXT NOT NULL, observed_at REAL NOT NULL, market_cap REAL NOT NULL, curve_pct REAL NOT NULL, payload_json TEXT NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS optimizer_observation_time ON optimizer_observations(namespace, observed_at)",
                    "CREATE INDEX IF NOT EXISTS optimizer_observation_mint ON optimizer_observations(namespace, mint, observed_at)",
                    "CREATE TABLE IF NOT EXISTS optimizer_trades (trade_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, status TEXT NOT NULL, entry_at REAL NOT NULL, exit_at REAL, payload_json TEXT NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS optimizer_trade_exit ON optimizer_trades(namespace, exit_at)",
                    "CREATE INDEX IF NOT EXISTS optimizer_trade_entry ON optimizer_trades(namespace, entry_at)",
                    "CREATE TABLE IF NOT EXISTS optimizer_reviews (review_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, review_at REAL NOT NULL, boundary_at REAL NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, metrics_json TEXT NOT NULL, UNIQUE(namespace, boundary_at))",
                    "CREATE INDEX IF NOT EXISTS optimizer_review_time ON optimizer_reviews(namespace, review_at)",
                    "CREATE TABLE IF NOT EXISTS optimizer_audits (audit_id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, at REAL NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL)",
                    "CREATE INDEX IF NOT EXISTS optimizer_audit_time ON optimizer_audits(namespace, at)",
                ]
            for statement in statements:
                self._execute(cur, statement)
            if self.dialect == "mysql":
                self._execute(cur, "INSERT IGNORE INTO optimizer_schema_versions(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, now))
            else:
                self._execute(cur, "INSERT OR IGNORE INTO optimizer_schema_versions(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, now))
            if seed_document is not None:
                existing = self._execute(cur, "SELECT revision FROM optimizer_strategy_config WHERE namespace=?", (FORWARD_NAMESPACE,)).fetchone()
                if not existing:
                    self._execute(
                        cur,
                        "INSERT INTO optimizer_strategy_config(namespace, revision, document_json, updated_at, source) VALUES (?, ?, ?, ?, ?)",
                        (FORWARD_NAMESPACE, int(seed_document["revision"]), _json(seed_document), now, "file_seed"),
                    )
                    return True
        return False

    def get_config(self) -> dict[str, Any] | None:
        with self.transaction() as cur:
            row = self._execute(cur, "SELECT document_json FROM optimizer_strategy_config WHERE namespace=?", (FORWARD_NAMESPACE,)).fetchone()
        return _loads(row["document_json"] if row else None)

    def update_config(self, document: dict[str, Any], expected_revision: int, *, source: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self.transaction() as cur:
            cursor = self._execute(
                cur,
                "UPDATE optimizer_strategy_config SET document_json=?, revision=?, updated_at=?, source=? WHERE namespace=? AND revision=?",
                (_json(document), int(document["revision"]), now, source, FORWARD_NAMESPACE, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict("strategy revision conflict")
        return document

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            cached = self._state_cache.get(key) if self.dialect == "mysql" else None
            if cached is not None and time.monotonic() - cached[0] < 5:
                return copy.deepcopy(cached[1])
            with self.transaction() as cur:
                row = self._execute(cur, "SELECT value_json FROM optimizer_state WHERE namespace=? AND state_key=?", (FORWARD_NAMESPACE, key)).fetchone()
            value = default if row is None else _loads(row["value_json"], default)
            if self.dialect == "mysql" and row is not None:
                self._state_cache[key] = (time.monotonic(), copy.deepcopy(value))
            return copy.deepcopy(value)

    def set_state(self, key: str, value: Any, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        encoded = _json(value)
        with self.transaction() as cur:
            if self.dialect == "mysql":
                self._execute(cur, """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                    VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json), updated_at=VALUES(updated_at)""", (FORWARD_NAMESPACE, key, encoded, now))
            else:
                self._execute(cur, """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(namespace, state_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""", (FORWARD_NAMESPACE, key, encoded, now))

        if self.dialect == "mysql":
            with self._lock:
                self._state_cache[key] = (time.monotonic(), copy.deepcopy(value))

    def set_many_state(self, values: dict[str, Any], *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.transaction() as cur:
            for key, value in values.items():
                encoded = _json(value)
                if self.dialect == "mysql":
                    statement = """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                        VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json), updated_at=VALUES(updated_at)"""
                else:
                    statement = """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                        VALUES (?, ?, ?, ?) ON CONFLICT(namespace, state_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at"""
                self._execute(cur, statement, (FORWARD_NAMESPACE, key, encoded, now))

        if self.dialect == "mysql":
            with self._lock:
                stamp = time.monotonic()
                for key, value in values.items():
                    self._state_cache[key] = (stamp, copy.deepcopy(value))

    def acquire_lease(self, lease_name: str, holder: str, *, ttl_seconds: float, now: float | None = None) -> bool:
        """Acquire/renew one worker lease atomically; stale deployments can recover."""
        now = time.time() if now is None else now
        expires = now + ttl_seconds
        with self.transaction() as cur:
            if self.dialect == "mysql":
                self._execute(cur, """INSERT INTO optimizer_leases(lease_name, holder, expires_at, updated_at)
                    VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE
                    holder=IF(expires_at < ? OR holder = ?, VALUES(holder), holder),
                    expires_at=IF(expires_at < ? OR holder = ?, VALUES(expires_at), expires_at),
                    updated_at=VALUES(updated_at)""", (lease_name, holder, expires, now, now, holder, now, holder))
            else:
                self._execute(cur, """INSERT INTO optimizer_leases(lease_name, holder, expires_at, updated_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(lease_name) DO UPDATE SET
                    holder=excluded.holder, expires_at=excluded.expires_at, updated_at=excluded.updated_at
                    WHERE optimizer_leases.expires_at < ? OR optimizer_leases.holder = ?""", (lease_name, holder, expires, now, now, holder))
            row = self._execute(cur, "SELECT holder, expires_at FROM optimizer_leases WHERE lease_name=?", (lease_name,)).fetchone()
        return bool(row and row["holder"] == holder and float(row["expires_at"]) >= expires)

    def release_lease(self, lease_name: str, holder: str) -> None:
        with self.transaction() as cur:
            self._execute(cur, "DELETE FROM optimizer_leases WHERE lease_name=? AND holder=?", (lease_name, holder))

    def record_observations(self, observations: list[dict[str, Any]]) -> None:
        if not observations:
            return
        rows = [(FORWARD_NAMESPACE, item["mint"], float(item["observed_at"]),
                 float(item["market_cap"]), float(item["curve_pct"]), _json(item))
                for item in observations]
        with self.transaction() as cur:
            statement = """INSERT INTO optimizer_observations(namespace, mint, observed_at, market_cap, curve_pct, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)"""
            cur.executemany(self._sql(statement), rows)

    def observations(self, *, start: float | None = None, end: float | None = None, limit: int = 30_000) -> list[dict[str, Any]]:
        """Read a bounded retained sample; this store never prunes user history itself."""
        clauses = ["namespace=?"]
        values: list[Any] = [FORWARD_NAMESPACE]
        if start is not None:
            clauses.append("observed_at>=?")
            values.append(float(start))
        if end is not None:
            clauses.append("observed_at<=?")
            values.append(float(end))
        statement = "SELECT payload_json FROM optimizer_observations WHERE " + " AND ".join(clauses) + " ORDER BY observed_at ASC, id ASC LIMIT ?"
        values.append(int(limit))
        with self.transaction() as cur:
            rows = self._execute(cur, statement, tuple(values)).fetchall()
        return [_loads(row["payload_json"]) for row in rows]

    def upsert_trade(self, trade: dict[str, Any]) -> None:
        with self.transaction() as cur:
            values = (trade["id"], FORWARD_NAMESPACE, trade["status"], float(trade["entry_at"]), trade.get("exit_at"), _json(trade))
            if self.dialect == "mysql":
                statement = """INSERT INTO optimizer_trades(trade_id, namespace, status, entry_at, exit_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?) ON DUPLICATE KEY UPDATE status=VALUES(status), exit_at=VALUES(exit_at), payload_json=VALUES(payload_json)"""
            else:
                statement = """INSERT INTO optimizer_trades(trade_id, namespace, status, entry_at, exit_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(trade_id) DO UPDATE SET status=excluded.status, exit_at=excluded.exit_at, payload_json=excluded.payload_json"""
            self._execute(cur, statement, values)

    def closed_trades(self) -> list[dict[str, Any]]:
        with self.transaction() as cur:
            rows = self._execute(cur, "SELECT payload_json FROM optimizer_trades WHERE namespace=? AND status='closed' ORDER BY exit_at ASC", (FORWARD_NAMESPACE,)).fetchall()
        return [_loads(row["payload_json"]) for row in rows]

    def record_review(self, review: dict[str, Any]) -> None:
        with self.transaction() as cur:
            values = (review["id"], FORWARD_NAMESPACE, review["at"], review["boundary"], review["decision"], review["reason"], _json(review["metrics"]))
            if self.dialect == "mysql":
                statement = "INSERT IGNORE INTO optimizer_reviews(review_id, namespace, review_at, boundary_at, decision, reason, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
            else:
                statement = "INSERT OR IGNORE INTO optimizer_reviews(review_id, namespace, review_at, boundary_at, decision, reason, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
            self._execute(cur, statement, values)

    def reviews(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.transaction() as cur:
            rows = self._execute(cur, "SELECT review_id, review_at, decision, reason, metrics_json FROM optimizer_reviews WHERE namespace=? ORDER BY review_at DESC LIMIT ?", (FORWARD_NAMESPACE, int(limit))).fetchall()
        return [
            {"id": row["review_id"], "at": float(row["review_at"]), "decision": row["decision"], "reason": row["reason"], "metrics": _loads(row["metrics_json"], {})}
            for row in rows
        ]

    def audit(self, action: str, detail: dict[str, Any], *, at: float | None = None) -> None:
        at = time.time() if at is None else at
        with self.transaction() as cur:
            self._execute(cur, "INSERT INTO optimizer_audits(namespace, at, action, detail_json) VALUES (?, ?, ?, ?)", (FORWARD_NAMESPACE, at, action, _json(detail)))

    def promote_config_with_audit(self, document: dict[str, Any], expected_revision: int, detail: dict[str, Any], *, now: float | None = None) -> None:
        """Atomically persist a reviewed promotion/rollback and its audit record."""
        now = time.time() if now is None else now
        with self.transaction() as cur:
            cursor = self._execute(cur, "UPDATE optimizer_strategy_config SET document_json=?, revision=?, updated_at=?, source=? WHERE namespace=? AND revision=?", (_json(document), document["revision"], now, "optimizer", FORWARD_NAMESPACE, expected_revision))
            if cursor.rowcount != 1:
                raise RevisionConflict("strategy revision conflict")
            self._execute(cur, "INSERT INTO optimizer_audits(namespace, at, action, detail_json) VALUES (?, ?, ?, ?)", (FORWARD_NAMESPACE, now, detail.get("action", "promotion"), _json(detail)))

    def close(self) -> None:
        if self.dialect == "sqlite" and self._connection is not None:
            self._connection.close()
