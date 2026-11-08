from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("BATON_DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "baton.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  project TEXT,
  departing_name TEXT,
  incoming_name TEXT,
  approver_name TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  published_at TEXT,
  published_by TEXT,
  acknowledged_at TEXT,
  acknowledged_by TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  acl TEXT NOT NULL DEFAULT 'team',
  content TEXT NOT NULL,
  author TEXT,
  source_date TEXT,
  external_url TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  start_char INTEGER NOT NULL,
  end_char INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  topic_key TEXT,
  text TEXT NOT NULL,
  value TEXT,
  owner TEXT,
  due_date TEXT,
  event_date TEXT,
  contact TEXT,
  basis TEXT NOT NULL DEFAULT 'stated',
  evidence_label TEXT NOT NULL DEFAULT 'PENDING',
  base_label TEXT,
  acl TEXT NOT NULL DEFAULT 'team',
  status TEXT NOT NULL DEFAULT 'pending',
  reviewer TEXT,
  reviewed_at TEXT,
  is_gap INTEGER NOT NULL DEFAULT 0,
  gap_critical INTEGER NOT NULL DEFAULT 0,
  gap_accepted INTEGER NOT NULL DEFAULT 0,
  gap_answer TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
  source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
  quote TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  note TEXT
);

CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  topic_key TEXT NOT NULL,
  claim_a TEXT,
  claim_b TEXT,
  value_a TEXT,
  value_b TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  resolved_value TEXT,
  resolved_claim_id TEXT,
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,
  actor TEXT,
  role TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_case ON claims(case_id);
CREATE INDEX IF NOT EXISTS idx_evidence_claim ON evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit(case_id);
CREATE INDEX IF NOT EXISTS idx_sources_case ON sources(case_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)


def query(sql: str, params: tuple = ()) -> list[dict]:
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with get_db() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with get_db() as db:
        cur = db.execute(sql, params)
        return int(cur.lastrowid or 0)


def audit(case_id: str | None, actor: str, role: str, action: str, detail: str = "") -> None:
    execute(
        "INSERT INTO audit(case_id, actor, role, action, detail, created_at) VALUES (?,?,?,?,?,?)",
        (case_id, actor, role, action, detail, now()),
    )


def reset_all() -> None:
    with get_db() as db:
        for table in ("evidence", "conflicts", "claims", "chunks", "sources", "audit", "cases"):
            db.execute(f"DELETE FROM {table}")
