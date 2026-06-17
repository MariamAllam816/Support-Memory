"""
store.py — SQLite schema and connection management.

Four layers, structurally separated:
  raw_events          → immutable evidence
  idempotency_index   → key → first (event_id, body_hash) mapping
  ingestion_conflicts → same key, different body = hard conflict
  facts               → one row per (entity, attribute, source_event)
  ambiguous_identities→ pairs flagged for human review, never auto-merged
  context_snapshots   → for diff/digest feature
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables. Safe to call multiple times (IF NOT EXISTS)."""

    # Layer 1: Immutable raw events
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_events (
            event_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            source TEXT NOT NULL,
            actor TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            related_entity_ids TEXT NOT NULL,  -- JSON array
            reliability TEXT NOT NULL,
            text TEXT NOT NULL,
            payload TEXT NOT NULL,  -- JSON object
            body_hash TEXT NOT NULL,
            ingest_status TEXT NOT NULL,  -- new / duplicate_noop / idempotency_conflict / skipped_already_ingested
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Layer 2: Idempotency index
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_index (
            idempotency_key TEXT PRIMARY KEY,
            first_event_id TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Layer 2b: Ingestion conflicts (same key, different body)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            conflicting_event_id TEXT NOT NULL,
            original_body_hash TEXT NOT NULL,
            conflicting_body_hash TEXT NOT NULL,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(idempotency_key, conflicting_event_id)
        )
    """)

    # Layer 2c: Soft duplicate candidates (payload-level signals)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_candidates (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            possible_duplicate_of TEXT NOT NULL,
            basis TEXT NOT NULL,  -- payload_field / text_heuristic
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_id, possible_duplicate_of)
        )
    """)

    # Layer 3: Derived facts — temporal, versioned, never deleted
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            fact_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            attribute TEXT NOT NULL,
            value TEXT NOT NULL,  -- JSON
            status TEXT NOT NULL,  -- current / superseded / contradicted / corroborating / unverified
            confidence REAL NOT NULL,  -- 0.0 to 1.0
            valid_from TEXT NOT NULL,
            valid_to TEXT,  -- NULL means still valid
            source_event_ids TEXT NOT NULL,  -- JSON array
            superseded_by TEXT,  -- fact_id that superseded this one
            reasoning TEXT NOT NULL,
            scope_account_id TEXT,  -- non-null only for policy facts (anti-leakage)
            derived_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_type, entity_id, attribute, valid_from)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status, entity_type, entity_id)
    """)

    # Layer 3b: Ambiguous identities — flagged, never merged
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ambiguous_identities (
            pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_a TEXT NOT NULL,
            entity_b TEXT NOT NULL,
            basis TEXT NOT NULL,  -- shared_phone / text_mention / etc
            status TEXT NOT NULL DEFAULT 'needs_review',
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(entity_a, entity_b)
        )
    """)

    # Layer 4: Context snapshots for diff/digest
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL,
            snapshot_data TEXT NOT NULL,  -- JSON
            built_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_entity ON context_snapshots(entity_id, built_at)
    """)

    conn.commit()


def reset_database(conn: sqlite3.Connection) -> None:
    """Drop and recreate all tables."""
    tables = [
        "raw_events", "idempotency_index", "ingestion_conflicts",
        "duplicate_candidates", "facts", "ambiguous_identities", "context_snapshots"
    ]
    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_schema(conn)
