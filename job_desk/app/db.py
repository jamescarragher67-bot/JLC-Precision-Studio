"""SQLite schema, migrations, and query helpers. No ORM.

Migrations are numbered SQL strings applied in order and recorded in
schema_version. Each migration is split on ';' and run inside one transaction,
so do not put semicolons inside string literals in a migration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app import config

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          client TEXT NOT NULL,
          fiverr_order_no TEXT,
          order_text TEXT NOT NULL,
          price REAL NOT NULL,
          deadline TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN (
            'draft','intake','ticket_ready','building','checking','review',
            'approved','delivered','paid','needs_input','failed')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX jobs_fiverr_order_no
          ON jobs(fiverr_order_no) WHERE fiverr_order_no IS NOT NULL;
        CREATE TABLE job_measurements (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL REFERENCES jobs(id),
          name TEXT NOT NULL,
          expected REAL NOT NULL,
          tolerance REAL,
          unit TEXT NOT NULL,
          source TEXT NOT NULL CHECK (source IN ('typed','ticket'))
        );
        CREATE TABLE stages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL REFERENCES jobs(id),
          stage_name TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL,
          result TEXT NOT NULL CHECK (result IN ('ok','failed','skipped')),
          output_path TEXT,
          error TEXT
        );
        CREATE INDEX stages_job ON stages(job_id, id);
        CREATE TABLE checks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL REFERENCES jobs(id),
          check_name TEXT NOT NULL,
          passed INTEGER NOT NULL CHECK (passed IN (0,1)),
          measured REAL,
          expected REAL,
          tolerance REAL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX checks_job ON checks(job_id, created_at);
        CREATE TABLE api_calls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL REFERENCES jobs(id),
          role TEXT NOT NULL CHECK (role IN ('intake','build','spec_review')),
          model TEXT NOT NULL,
          input_tokens INTEGER NOT NULL,
          output_tokens INTEGER NOT NULL,
          cost_usd REAL NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX api_calls_created ON api_calls(created_at);
        CREATE TABLE finance_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          date TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('in','out')),
          category TEXT NOT NULL,
          description TEXT NOT NULL,
          amount_aud REAL NOT NULL CHECK (amount_aud >= 0),
          source TEXT NOT NULL CHECK (source IN ('manual','fiverr_csv','api_log')),
          external_ref TEXT
        );
        CREATE UNIQUE INDEX finance_external_ref
          ON finance_entries(external_ref) WHERE external_ref IS NOT NULL;
        CREATE TABLE settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        # Columns the dashboard needs that the original spec did not list.
        # revision_count: incremented on each send-back.
        # paused: per-job pause flag (not a status).
        # stage_cursor: last completed stage in the current status group.
        """
        ALTER TABLE jobs ADD COLUMN revision_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE jobs ADD COLUMN paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1));
        ALTER TABLE jobs ADD COLUMN stage_cursor TEXT;
        """,
    ),
]


def now_iso() -> str:
    """Local time with UTC offset, microsecond resolution.

    Microseconds keep check batches distinct. Month filters use the
    'YYYY-MM' prefix, which is the local month.
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def current_month() -> str:
    return datetime.now().astimezone().strftime("%Y-%m")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


class Tx:
    """Handle yielded by transaction(). Callbacks run only after COMMIT."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._after_commit: list = []

    def on_commit(self, fn) -> None:
        self._after_commit.append(fn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[Tx]:
    conn.execute("BEGIN IMMEDIATE")
    tx = Tx(conn)
    try:
        yield tx
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    for fn in tx._after_commit:
        fn()


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    current = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    ).fetchone()[0]
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in sql.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, now_iso()),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# --- jobs ------------------------------------------------------------------

def next_job_id(conn: sqlite3.Connection) -> str:
    n = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(id, 3) AS INTEGER)), 0) FROM jobs"
    ).fetchone()[0]
    return f"J-{n + 1:04d}"


def insert_job(conn: sqlite3.Connection, **fields: Any) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))


JOB_UPDATABLE = frozenset(
    {"status", "updated_at", "retry_count", "revision_count", "paused", "stage_cursor"}
)


def update_job(conn: sqlite3.Connection, job_id: str, **fields: Any) -> None:
    unknown = set(fields) - JOB_UPDATABLE
    if unknown:
        raise ValueError(f"cannot update job columns: {sorted(unknown)}")
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(
    conn: sqlite3.Connection, statuses: Sequence[str] | None = None
) -> list[sqlite3.Row]:
    if statuses is None:
        return conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    marks = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", tuple(statuses)
    ).fetchall()


# --- measurements ----------------------------------------------------------

def insert_measurement(
    conn: sqlite3.Connection,
    job_id: str,
    name: str,
    expected: float,
    tolerance: float | None,
    unit: str,
    source: str,
) -> None:
    conn.execute(
        "INSERT INTO job_measurements (job_id, name, expected, tolerance, unit, source)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, name, expected, tolerance, unit, source),
    )


def get_measurements(
    conn: sqlite3.Connection, job_id: str, source: str | None = None
) -> list[sqlite3.Row]:
    if source is None:
        return conn.execute(
            "SELECT * FROM job_measurements WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM job_measurements WHERE job_id = ? AND source = ? ORDER BY id",
        (job_id, source),
    ).fetchall()


# --- stages ----------------------------------------------------------------

def record_stage(
    conn: sqlite3.Connection,
    job_id: str,
    stage_name: str,
    started_at: str,
    finished_at: str,
    result: str,
    output_path: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO stages (job_id, stage_name, started_at, finished_at, result,"
        " output_path, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, stage_name, started_at, finished_at, result, output_path, error),
    )


def list_stages(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM stages WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()


def last_stage(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stages WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


# --- checks ----------------------------------------------------------------

def save_checks(
    conn: sqlite3.Connection, job_id: str, results: Sequence[Any], created_at: str
) -> None:
    for r in results:
        conn.execute(
            "INSERT INTO checks (job_id, check_name, passed, measured, expected,"
            " tolerance, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                r.check_name,
                1 if r.passed else 0,
                r.measured,
                r.expected,
                r.tolerance,
                r.detail,
                created_at,
            ),
        )


def latest_checks(conn: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """Rows from the most recent checks batch for this job."""
    return conn.execute(
        "SELECT * FROM checks WHERE job_id = ? AND created_at = ("
        "  SELECT MAX(created_at) FROM checks WHERE job_id = ?) ORDER BY id",
        (job_id, job_id),
    ).fetchall()


# --- api calls and settings -------------------------------------------------

def record_api_call(
    conn: sqlite3.Connection,
    job_id: str,
    role: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    conn.execute(
        "INSERT INTO api_calls (job_id, role, model, input_tokens, output_tokens,"
        " cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, role, model, input_tokens, output_tokens, cost_usd, now_iso()),
    )


def spend_for_month(conn: sqlite3.Connection, month: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls"
        " WHERE substr(created_at, 1, 7) = ?",
        (month,),
    ).fetchone()
    return float(row[0])


def job_token_cost(conn: sqlite3.Connection, job_id: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls WHERE job_id = ?", (job_id,)
    ).fetchone()
    return float(row[0])


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


KILL_SWITCH_KEY = "kill_switch"
CAP_KEY = "monthly_api_cap_usd"


def kill_switch_on(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, KILL_SWITCH_KEY, "0") == "1"


def monthly_cap(conn: sqlite3.Connection) -> float:
    raw = get_setting(conn, CAP_KEY)
    return float(raw) if raw is not None else config.DEFAULT_MONTHLY_API_CAP_USD
