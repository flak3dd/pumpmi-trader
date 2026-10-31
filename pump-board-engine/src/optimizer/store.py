"""Database-backed persistence for the paper-only optimizer.

The service uses MySQL in production through ``DATABASE_URL`` and PyMySQL. The
same small SQL surface supports an injected SQLite connection for deterministic
software tests. Nothing in this module reads or writes checked-in board, ledger,
fixture, or strategy files after the optional one-time seed input.

The worker uses :meth:`lease_fence` for every durable mutation.  A fence is a
single database transaction: it locks/checks the lease before work and checks
owner plus expiry again immediately before commit. Nested store helpers use
savepoints, so a helper can never commit independently from the outer fence.
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


class LeaseLost(RuntimeError):
    """A worker attempted to commit after its durable lease was lost or expired."""


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
    """A narrow DB-API adapter with additive migrations and JSON state records.

    ``transaction`` is intentionally re-entrant. The root context owns the
    database commit; nested contexts use savepoints. That matters for a worker
    cycle because helpers such as ``set_many_state`` and ``upsert_trade`` must
    be one atomic unit with the portfolio and the lease expiry check.
    """

    def __init__(self, database_url: str | None = None, *, connection: Any = None) -> None:
        self._state_cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._tx_local = threading.local()
        if connection is not None:
            self.dialect = "sqlite"
            self._connection = connection
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._database_url = "sqlite-injected"
            return
        if not database_url:
            raise StoreUnavailable("DATABASE_URL is required for the durable optimizer")
        self._database_url = database_url
        parsed = urlparse(database_url)
        scheme = parsed.scheme.lower()
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

    def _tx_state(self) -> Any:
        """Return per-thread transaction state without relying on globals."""
        local = getattr(self, "_tx_local", None)
        if local is None:  # Supports narrowly constructed cache-only unit tests.
            local = threading.local()
            self._tx_local = local
        if not hasattr(local, "depth"):
            local.depth = 0
            local.savepoint = 0
            local.fence = None
        return local

    def _connection_for_transaction(self) -> Any:
        if self.dialect == "mysql" and (self._connection is None or not getattr(self._connection, "open", False)):
            self._connection = self._mysql_connection()
        if self._connection is None:
            raise StoreUnavailable("optimizer database connection is unavailable")
        return self._connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[Any]:
        """Open a root transaction or a nested savepoint on this store connection."""
        with self._lock:
            state = self._tx_state()
            previous_depth = int(state.depth)
            outer = previous_depth == 0
            connection = self._connection_for_transaction()
            cursor = connection.cursor()
            savepoint: str | None = None
            state.depth = previous_depth + 1
            try:
                if outer:
                    if self.dialect == "sqlite":
                        # Always begin a real root transaction: otherwise the first
                        # nested SAVEPOINT/RELEASE can commit independently of it.
                        cursor.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                    else:
                        begin = getattr(connection, "begin", None)
                        if callable(begin):
                            begin()
                        else:  # pragma: no cover - DB-API compatibility fallback
                            cursor.execute("START TRANSACTION")
                else:
                    state.savepoint = int(state.savepoint) + 1
                    savepoint = f"optimizer_sp_{state.savepoint}"
                    self._execute(cursor, f"SAVEPOINT {savepoint}")
                yield cursor
                if savepoint is not None:
                    self._execute(cursor, f"RELEASE SAVEPOINT {savepoint}")
                elif outer:
                    connection.commit()
            except Exception:
                try:
                    if savepoint is not None:
                        self._execute(cursor, f"ROLLBACK TO SAVEPOINT {savepoint}")
                        self._execute(cursor, f"RELEASE SAVEPOINT {savepoint}")
                    elif outer:
                        connection.rollback()
                except Exception:
                    pass
                self._invalidate_state_cache()
                if outer and self.dialect == "mysql":
                    # A failed network/SQL operation can leave a pooled MySQL
                    # connection indeterminate. Reopen lazily on the next call.
                    try:
                        connection.close()
                    finally:
                        self._connection = None
                raise
            finally:
                # Setup failures belong here too, including BEGIN/SAVEPOINT errors.
                state.depth = previous_depth
                if outer:
                    state.fence = None
                try:
                    cursor.close()
                except Exception:
                    pass

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """One re-entrant DB transaction; nested calls use savepoints."""
        with self._transaction() as cursor:
            yield cursor

    def _execute(self, cursor: Any, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        cursor.execute(self._sql(statement), parameters)
        return cursor

    def _is_fenced(self) -> bool:
        return bool(getattr(self._tx_state(), "fence", None))

    def _invalidate_state_cache(self, keys: set[str] | None = None) -> None:
        """Drop local telemetry cache entries; never publish uncommitted worker data."""
        with self._lock:
            if keys is None:
                self._state_cache.clear()
            else:
                for key in keys:
                    self._state_cache.pop(key, None)

    def _lease_select_sql(self) -> str:
        suffix = " FOR UPDATE" if self.dialect == "mysql" else ""
        return "SELECT holder, expires_at FROM optimizer_leases WHERE lease_name=?" + suffix

    def _assert_lease(self, cursor: Any, lease_name: str, holder: str, *, now: float) -> None:
        row = self._execute(cursor, self._lease_select_sql(), (lease_name,)).fetchone()
        if not row or row["holder"] != holder or float(row["expires_at"]) <= float(now):
            raise LeaseLost("optimizer worker lease is no longer owned or has expired")

    @contextmanager
    def lease_fence(self, lease_name: str, holder: str, *, now: float | None = None) -> Iterator[Any]:
        """Fence a compact worker write unit by durable owner and wall-clock expiry.

        Production callers omit ``now`` so both checks sample the actual wall clock.
        Deterministic tests may pass one logical timestamp. The check immediately
        before root commit rejects a holder that spent longer than its TTL inside
        the transaction; all nested observations, trades, counters and state then
        roll back together.
        """
        state = self._tx_state()
        existing = state.fence
        if existing is not None:
            if existing["lease_name"] != lease_name or existing["holder"] != holder:
                raise LeaseLost("cannot nest a different worker lease fence")
            with self._transaction() as cursor:
                self._assert_lease(cursor, lease_name, holder, now=float(now if now is not None else time.time()))
                yield cursor
            return
        if int(state.depth) != 0:
            # A savepoint cannot own the outer commit or its final expiry check.
            # Require workers to start with a root fence, not wrap it in an
            # ordinary transaction that could continue writing after fence exit.
            raise StoreUnavailable("worker lease fence must start the root transaction")
        with self._transaction(immediate=True) as cursor:
            entry_now = float(now if now is not None else time.time())
            self._assert_lease(cursor, lease_name, holder, now=entry_now)
            # New/renewed ownership must not use a five-second telemetry snapshot
            # as trading input. Reads under the fence also bypass the cache.
            self._invalidate_state_cache()
            state.fence = {"lease_name": lease_name, "holder": holder}
            try:
                yield cursor
                commit_now = float(now if now is not None else time.time())
                self._assert_lease(cursor, lease_name, holder, now=commit_now)
            finally:
                state.fence = None

    # Name kept explicit for callers/tests that describe this as a worker transaction.
    worker_transaction = lease_fence

    def migrate(self, seed_document: dict[str, Any] | None = None, *, now: float | None = None) -> bool:
        """Create only optimizer-owned tables and atomically seed only a missing config row."""
        now = time.time() if now is None else now
        seeded = False
        with self._transaction(immediate=self.dialect == "sqlite") as cur:
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
                if self.dialect == "mysql":
                    inserted = self._execute(
                        cur,
                        "INSERT IGNORE INTO optimizer_strategy_config(namespace, revision, document_json, updated_at, source) VALUES (?, ?, ?, ?, ?)",
                        (FORWARD_NAMESPACE, int(seed_document["revision"]), _json(seed_document), now, "file_seed"),
                    )
                else:
                    inserted = self._execute(
                        cur,
                        "INSERT OR IGNORE INTO optimizer_strategy_config(namespace, revision, document_json, updated_at, source) VALUES (?, ?, ?, ?, ?)",
                        (FORWARD_NAMESPACE, int(seed_document["revision"]), _json(seed_document), now, "file_seed"),
                    )
                seeded = bool(inserted.rowcount == 1)
        return seeded

    def get_config(self) -> dict[str, Any] | None:
        with self.transaction() as cur:
            row = self._execute(cur, "SELECT document_json FROM optimizer_strategy_config WHERE namespace=?", (FORWARD_NAMESPACE,)).fetchone()
        return _loads(row["document_json"] if row else None)

    def _write_config(self, cur: Any, document: dict[str, Any], expected_revision: int, *, source: str, now: float) -> None:
        cursor = self._execute(
            cur,
            "UPDATE optimizer_strategy_config SET document_json=?, revision=?, updated_at=?, source=? WHERE namespace=? AND revision=?",
            (_json(document), int(document["revision"]), now, source, FORWARD_NAMESPACE, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict("strategy revision conflict")

    def update_config(self, document: dict[str, Any], expected_revision: int, *, source: str, now: float | None = None) -> dict[str, Any]:
        """Backward-compatible bare CAS. User-facing writes should use the atomic helper below."""
        now = time.time() if now is None else now
        with self.transaction() as cur:
            self._write_config(cur, document, expected_revision, source=source, now=now)
        return document

    def _write_state(self, cur: Any, key: str, value: Any, *, now: float, only_if_missing: bool = False) -> None:
        encoded = _json(value)
        if only_if_missing:
            if self.dialect == "mysql":
                statement = "INSERT IGNORE INTO optimizer_state(namespace, state_key, value_json, updated_at) VALUES (?, ?, ?, ?)"
            else:
                statement = "INSERT OR IGNORE INTO optimizer_state(namespace, state_key, value_json, updated_at) VALUES (?, ?, ?, ?)"
        elif self.dialect == "mysql":
            statement = """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json), updated_at=VALUES(updated_at)"""
        else:
            statement = """INSERT INTO optimizer_state(namespace, state_key, value_json, updated_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(namespace, state_key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at"""
        self._execute(cur, statement, (FORWARD_NAMESPACE, key, encoded, now))

    def initialize_state_if_missing(self, values: dict[str, Any], *, now: float | None = None) -> None:
        """Idempotently initialize only absent state rows under one transaction.

        Competing starts can both call this safely; no existing portfolio, user
        state, active-promotion marker, or telemetry row is reset.
        """
        now = time.time() if now is None else now
        with self._transaction(immediate=self.dialect == "sqlite") as cur:
            for key, value in values.items():
                self._write_state(cur, key, value, now=now, only_if_missing=True)
        self._invalidate_state_cache(set(values))

    def get_state(self, key: str, default: Any = None, *, fresh: bool = False) -> Any:
        # SQLite tests intentionally exercise the same five-second cache/handoff
        # behavior as MySQL. Fences and explicit fresh worker reads always bypass it.
        use_cache = not fresh and not self._is_fenced()
        with self._lock:
            cached = self._state_cache.get(key) if use_cache else None
            if cached is not None and time.monotonic() - cached[0] < 5:
                return copy.deepcopy(cached[1])
        with self.transaction() as cur:
            row = self._execute(cur, "SELECT value_json FROM optimizer_state WHERE namespace=? AND state_key=?", (FORWARD_NAMESPACE, key)).fetchone()
        value = default if row is None else _loads(row["value_json"], default)
        if use_cache and row is not None:
            with self._lock:
                self._state_cache[key] = (time.monotonic(), copy.deepcopy(value))
        return copy.deepcopy(value)

    def set_state(self, key: str, value: Any, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.transaction() as cur:
            self._write_state(cur, key, value, now=now)
        # Invalidate rather than update cache. The call may have been a nested
        # savepoint under an outer fence that later rolls back.
        self._invalidate_state_cache({key})

    def set_many_state(self, values: dict[str, Any], *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.transaction() as cur:
            for key, value in values.items():
                self._write_state(cur, key, value, now=now)
        self._invalidate_state_cache(set(values))

    def _write_audit(self, cur: Any, action: str, detail: dict[str, Any], *, at: float) -> None:
        self._execute(cur, "INSERT INTO optimizer_audits(namespace, at, action, detail_json) VALUES (?, ?, ?, ?)", (FORWARD_NAMESPACE, at, action, _json(detail)))

    def update_user_config_with_audit(self, document: dict[str, Any], expected_revision: int, detail: dict[str, Any], *, now: float | None = None) -> None:
        """Atomically CAS a user document, audit it, and retire rollback state."""
        now = time.time() if now is None else now
        with self.transaction() as cur:
            self._write_config(cur, document, expected_revision, source="user", now=now)
            self._write_audit(cur, detail.get("action", "user_strategy_update"), detail, at=now)
            # A manual edit is authoritative. Clearing this marker in the same
            # transaction prevents a later rollback from restoring stale settings.
            self._write_state(cur, "active_promotion", None, now=now)
        self._invalidate_state_cache({"active_promotion"})

    def update_config_with_audit_and_state(
        self,
        document: dict[str, Any],
        expected_revision: int,
        detail: dict[str, Any],
        *,
        state_values: dict[str, Any],
        source: str = "optimizer",
        now: float | None = None,
    ) -> None:
        """Atomically CAS config, audit, and promotion/rollback state records.

        When called under :meth:`lease_fence`, this joins its outer transaction and
        therefore also receives the final owner/expiry check before commit.
        """
        now = time.time() if now is None else now
        with self.transaction() as cur:
            self._write_config(cur, document, expected_revision, source=source, now=now)
            self._write_audit(cur, detail.get("action", "promotion"), detail, at=now)
            for key, value in state_values.items():
                self._write_state(cur, key, value, now=now)
        self._invalidate_state_cache(set(state_values))

    def acquire_lease(self, lease_name: str, holder: str, *, ttl_seconds: float, now: float | None = None) -> bool:
        """Acquire/renew one worker lease atomically; stale deployments can recover."""
        now = time.time() if now is None else now
        expires = now + ttl_seconds
        with self.transaction() as cur:
            if self.dialect == "mysql":
                self._execute(cur, """INSERT INTO optimizer_leases(lease_name, holder, expires_at, updated_at)
                    VALUES (?, ?, ?, ?) ON DUPLICATE KEY UPDATE
                    holder=IF(expires_at <= ? OR holder = ?, VALUES(holder), holder),
                    expires_at=IF(expires_at <= ? OR holder = ?, VALUES(expires_at), expires_at),
                    updated_at=IF(expires_at <= ? OR holder = ?, VALUES(updated_at), updated_at)""", (lease_name, holder, expires, now, now, holder, now, holder, now, holder))
            else:
                self._execute(cur, """INSERT INTO optimizer_leases(lease_name, holder, expires_at, updated_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(lease_name) DO UPDATE SET
                    holder=excluded.holder, expires_at=excluded.expires_at, updated_at=excluded.updated_at
                    WHERE optimizer_leases.expires_at <= ? OR optimizer_leases.holder = ?""", (lease_name, holder, expires, now, now, holder))
            row = self._execute(cur, "SELECT holder, expires_at FROM optimizer_leases WHERE lease_name=?", (lease_name,)).fetchone()
        owned = bool(row and row["holder"] == holder and float(row["expires_at"]) >= expires)
        if owned:
            # Acquisition/renewal is also a handoff boundary for five-second cache
            # values. New owner reads must be authoritative trading state.
            self._invalidate_state_cache()
        return owned

    def renew_lease(self, lease_name: str, holder: str, *, ttl_seconds: float, now: float | None = None) -> bool:
        """Extend only an unexpired, continuously owned lease; never reacquire it.

        The worker loop may acquire a new lease for a new collection cycle. A
        post-collection renewal must instead discard a batch if authority expired
        or changed while that batch was being collected, even if a successor has
        since released or expired its lease.
        """
        now = time.time() if now is None else now
        with self._transaction(immediate=True) as cur:
            row = self._execute(cur, self._lease_select_sql(), (lease_name,)).fetchone()
            if not row or row["holder"] != holder or float(row["expires_at"]) <= float(now):
                return False
            self._execute(cur,
                "UPDATE optimizer_leases SET expires_at=?, updated_at=? WHERE lease_name=? AND holder=?",
                (float(now) + ttl_seconds, now, lease_name, holder))
        self._invalidate_state_cache()
        return True

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
            self._write_audit(cur, action, detail, at=at)

    def promote_config_with_audit(self, document: dict[str, Any], expected_revision: int, detail: dict[str, Any], *, now: float | None = None) -> None:
        """Backward-compatible atomic config/audit CAS without promotion markers."""
        self.update_config_with_audit_and_state(
            document,
            expected_revision,
            detail,
            state_values={},
            source="optimizer",
            now=now,
        )

    def close(self) -> None:
        if self.dialect == "sqlite" and self._connection is not None:
            self._connection.close()
