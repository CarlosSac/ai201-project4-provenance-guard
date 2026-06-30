"""
database.py — SQLite init and helper functions for Provenance Guard

Tables:
  submissions  — one row per analyzed piece of content
  audit_log    — one row per event (analysis or appeal)

Usage:
  from database import init_db, insert_submission, insert_audit_event
  init_db()          # call once at app startup
"""

import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path — sits next to this file, gitignored
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")


def get_connection() -> sqlite3.Connection:
    """Return a connection with row_factory set so rows behave like dicts."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema init — idempotent (IF NOT EXISTS)
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    content_id       TEXT PRIMARY KEY,
    author_id        TEXT,
    title            TEXT,
    content_snippet  TEXT,          -- first 500 chars for audit display
    classification   TEXT,          -- 'ai_generated' | 'human_written' | 'uncertain'
    confidence_score REAL,
    confidence_level TEXT,          -- 'high' | 'medium' | 'low'
    llm_score        REAL,
    stylo_score      REAL,
    signal_gap       REAL,
    stylo_reliable   INTEGER,       -- boolean (0/1), NULL until Signal 2 is wired
    transparency_label TEXT,
    status           TEXT DEFAULT 'active',  -- 'active' | 'under_review'
    created_at       TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id              TEXT,
    event_type              TEXT,   -- 'analysis' | 'appeal'
    appeal_id               TEXT,
    reason                  TEXT,
    previous_classification TEXT,
    previous_confidence     REAL,
    status                  TEXT,
    timestamp               TEXT,
    FOREIGN KEY (content_id) REFERENCES submissions(content_id)
);
"""


def init_db() -> None:
    """Create tables if they don't already exist. Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
    logger.info("Database initialised at %s", _DB_PATH)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------
def insert_submission(row: dict) -> None:
    """
    Insert a row into `submissions`. Expected keys match the schema columns.
    Caller is responsible for providing all non-null fields.
    """
    sql = """
        INSERT INTO submissions (
            content_id, author_id, title, content_snippet,
            classification, confidence_score, confidence_level,
            llm_score, stylo_score, signal_gap, stylo_reliable,
            transparency_label, status, created_at
        ) VALUES (
            :content_id, :author_id, :title, :content_snippet,
            :classification, :confidence_score, :confidence_level,
            :llm_score, :stylo_score, :signal_gap, :stylo_reliable,
            :transparency_label, :status, :created_at
        )
    """
    with get_connection() as conn:
        conn.execute(sql, row)
    logger.debug("Inserted submission %s", row["content_id"])


def insert_audit_event(row: dict) -> None:
    """
    Insert a row into `audit_log`. Expected keys: content_id, event_type,
    appeal_id (nullable), reason (nullable), previous_classification (nullable),
    previous_confidence (nullable), status, timestamp.
    """
    sql = """
        INSERT INTO audit_log (
            content_id, event_type, appeal_id, reason,
            previous_classification, previous_confidence,
            status, timestamp
        ) VALUES (
            :content_id, :event_type, :appeal_id, :reason,
            :previous_classification, :previous_confidence,
            :status, :timestamp
        )
    """
    with get_connection() as conn:
        conn.execute(sql, row)
    logger.debug("Inserted audit event (%s) for %s", row["event_type"], row["content_id"])


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------
def get_submission(content_id: str) -> dict | None:
    """Return a submission row as a dict, or None if not found."""
    sql = "SELECT * FROM submissions WHERE content_id = ?"
    with get_connection() as conn:
        row = conn.execute(sql, (content_id,)).fetchone()
    return dict(row) if row else None


def update_submission_status(content_id: str, new_status: str) -> int:
    """
    Update submissions.status. Returns the number of rows affected
    (0 means content_id not found, 1 means success).
    """
    sql = "UPDATE submissions SET status = ? WHERE content_id = ?"
    with get_connection() as conn:
        cursor = conn.execute(sql, (new_status, content_id))
        return cursor.rowcount


def get_recent_submissions(limit: int = 50) -> list[dict]:
    """Return the most recent submission rows as a list of dicts, newest first."""
    sql = "SELECT * FROM submissions ORDER BY created_at DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_audit_log(content_id: str | None = None, limit: int = 50) -> list[dict]:
    """
    Return audit_log entries as a list of dicts.
    If content_id is provided, filter to that submission.
    """
    if content_id:
        sql  = "SELECT * FROM audit_log WHERE content_id = ? ORDER BY id DESC LIMIT ?"
        args = (content_id, limit)
    else:
        sql  = "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?"
        args = (limit,)

    with get_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]
