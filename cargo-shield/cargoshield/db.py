"""SQLite access. Plain SQL only, so the same queries run on Postgres later."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def utcnow() -> datetime:
    """Naive UTC 'now'. All demo timestamps are naive UTC ISO strings."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO demo_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM demo_meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def is_seeded(path: str | Path | None = None) -> bool:
    """True if the database file holds a seeded demo (not missing, empty or half-built)."""
    path = Path(path or config.db_path())
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT value FROM demo_meta WHERE key = 'seeded_at'").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return False
    return row is not None


def copy_database(src: sqlite3.Connection, target: str | Path) -> None:
    """Overwrite the database at `target` with the contents of `src` (SQLite backup API).

    Safe while other connections have `target` open, unlike deleting the file.
    """
    dst = sqlite3.connect(str(target), timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()


def snapshot(conn: sqlite3.Connection, target: str | Path | None = None) -> Path:
    """Copy the live database to the demo start snapshot (SQLite backup API)."""
    target = Path(target or config.snapshot_path())
    if target.exists():
        target.unlink()
    dst = sqlite3.connect(str(target))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return target


def restore(snapshot_file: str | Path | None = None, live: str | Path | None = None) -> None:
    """Overwrite the live database with the snapshot, in place.

    Uses the backup API rather than copying files, so it is safe while the
    web app holds connections open.
    """
    snapshot_file = Path(snapshot_file or config.snapshot_path())
    if not snapshot_file.exists():
        raise FileNotFoundError(f"No demo snapshot at {snapshot_file}. Run the seed first.")
    src = sqlite3.connect(str(snapshot_file))
    dst = sqlite3.connect(str(live or config.db_path()), timeout=10)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
