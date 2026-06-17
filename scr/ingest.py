"""
ingest.py — Orchestrate ingestion, idempotency checks, and fact derivation.

Idempotency rules (the "deliberate trap" handling):
1. Same idempotency_key + same body hash → duplicate_noop (true retry)
2. Same idempotency_key + different body hash → idempotency_conflict
3. Same event_id → skipped_already_ingested
4. We NEVER trust free-text claims about idempotency over structured fields
"""

import json
import hashlib
from typing import List, Dict, Any, Tuple
from datetime import datetime
import sqlite3

from . import store, extract, resolve, identity


def compute_body_hash(event: Dict[str, Any]) -> str:
    """Hash everything EXCEPT event_id and idempotency_key."""
    body = {
        "occurred_at": event["occurred_at"],
        "source": event["source"],
        "actor": event["actor"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "related_entity_ids": event.get("related_entity_ids", []),
        "reliability": event["reliability"],
        "text": event["text"],
        "payload": event.get("payload", {}),
    }
    body_str = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body_str.encode("utf-8")).hexdigest()


def ingest_events(conn: sqlite3.Connection, events: List[Dict[str, Any]]) -> Dict[str, int]:
    """Ingest events with full idempotency handling. Returns stats."""
    stats = {
        "new": 0,
        "duplicate_noop": 0,
        "idempotency_conflict": 0,
        "skipped_already_ingested": 0,
    }

    for event in events:
        event_id = event["event_id"]
        idem_key = event["idempotency_key"]
        body_hash = compute_body_hash(event)

        # Check 1: Already ingested by event_id?
        existing = conn.execute(
            "SELECT 1 FROM raw_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing:
            stats["skipped_already_ingested"] += 1
            continue

        # Check 2: Idempotency key seen before?
        idem_row = conn.execute(
            "SELECT first_event_id, body_hash FROM idempotency_index WHERE idempotency_key = ?",
            (idem_key,)
        ).fetchone()

        ingest_status = "new"

        if idem_row:
            stored_hash = idem_row["body_hash"]
            if stored_hash == body_hash:
                # True retry — same key, same body
                # Per Lina's scope change: do NOT create a second raw event or re-confirm facts.
                # Return the same logical result silently.
                stats["duplicate_noop"] += 1
                continue  # Skip ALL processing for this event — no raw insert, no duplicate candidates
            else:
                # Conflict — same key, different body
                ingest_status = "idempotency_conflict"
                stats["idempotency_conflict"] += 1

                conn.execute("""
                    INSERT OR IGNORE INTO ingestion_conflicts
                    (idempotency_key, original_event_id, conflicting_event_id,
                     original_body_hash, conflicting_body_hash)
                    VALUES (?, ?, ?, ?, ?)
                """, (idem_key, idem_row["first_event_id"], event_id,
                      stored_hash, body_hash))
        else:
            conn.execute("""
                INSERT INTO idempotency_index (idempotency_key, first_event_id, body_hash)
                VALUES (?, ?, ?)
            """, (idem_key, event_id, body_hash))
            stats["new"] += 1

        # Preserve the raw event (immutable evidence) — ONLY for new or conflict events
        conn.execute("""
            INSERT INTO raw_events
            (event_id, idempotency_key, occurred_at, source, actor, entity_type,
             entity_id, related_entity_ids, reliability, text, payload, body_hash, ingest_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event_id, idem_key, event["occurred_at"], event["source"],
            event["actor"], event["entity_type"], event["entity_id"],
            json.dumps(event.get("related_entity_ids", [])),
            event["reliability"], event["text"],
            json.dumps(event.get("payload", {})),
            body_hash, ingest_status
        ))

        # Check for soft duplicate signals in payload
        payload = event.get("payload", {})
        if "possible_duplicate_of" in payload:
            conn.execute("""
                INSERT OR IGNORE INTO duplicate_candidates (event_id, possible_duplicate_of, basis)
                VALUES (?, ?, ?)
            """, (event_id, payload["possible_duplicate_of"], "payload_field"))

        if "retry_of" in payload:
            conn.execute("""
                INSERT OR IGNORE INTO duplicate_candidates (event_id, possible_duplicate_of, basis)
                VALUES (?, ?, ?)
            """, (event_id, payload["retry_of"], "payload_field"))

    conn.commit()
    return stats


def build_facts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Derive facts from all raw events. Full rebuild for correctness."""
    conn.execute("DELETE FROM facts")
    conn.execute("DELETE FROM ambiguous_identities")

    events = []
    for row in conn.execute("SELECT * FROM raw_events ORDER BY occurred_at"):
        event = dict(row)
        event["payload"] = json.loads(event["payload"])
        event["related_entity_ids"] = json.loads(event["related_entity_ids"])
        events.append(event)

    # Detect ambiguous identities
    ambiguous_pairs = identity.detect_ambiguous_identities(events)
    for pair in ambiguous_pairs:
        conn.execute("""
            INSERT OR IGNORE INTO ambiguous_identities (entity_a, entity_b, basis, status)
            VALUES (?, ?, ?, ?)
        """, (pair["entity_a"], pair["entity_b"], pair["basis"], pair["status"]))

    # Extract all candidate facts
    all_extracted = []
    for event in events:
        extracted = extract.extract_facts(event)
        # CRITICAL: Check event-level unverified flag and propagate to ALL extracted facts
        is_event_unverified = event.get("payload", {}).get("unverified_policy_guess", False)
        for fact in extracted:
            all_extracted.append({
                "entity_type": fact.entity_type,
                "entity_id": fact.entity_id,
                "attribute": fact.attribute,
                "value": fact.value,
                "confidence": fact.confidence_boost,
                "is_correction": fact.is_correction,
                "supersedes": fact.supersedes,
                "is_old_fact": fact.is_old_fact,
                "scope_account_id": fact.scope_account_id,
                "source_event_id": fact.source_event_id,
                "occurred_at": event["occurred_at"],
                "_source_event": event,
                "_is_unverified": is_event_unverified,  # CRITICAL: from event payload
            })

    # Group by (entity_type, entity_id, attribute) for resolution
    threads = {}
    for fact in all_extracted:
        key = (fact["entity_type"], fact["entity_id"], fact["attribute"])
        threads.setdefault(key, []).append(fact)

    # Resolve each thread
    resolved_facts = []
    for key, thread_facts in threads.items():
        resolved = resolve.resolve_fact_thread(thread_facts)
        resolved_facts.extend(resolved)

    # Find superseding relationships
    for fact in resolved_facts:
        if fact.get("supersedes") and fact["status"] == "current":
            for other in resolved_facts:
                if (other["entity_type"] == fact["entity_type"] and
                    other["entity_id"] == fact["entity_id"] and
                    other["attribute"] == fact["attribute"] and
                    other["status"] in {"current", "corroborating"} and
                    other["source_event_id"] != fact["source_event_id"]):
                    if other["occurred_at"] < fact["occurred_at"]:
                        other["status"] = "superseded"
                        other["reasoning"] += f"; superseded by {fact['source_event_id']}"
                        other["superseded_by"] = fact["source_event_id"]

    # Insert facts
    for fact in resolved_facts:
        fact_id = f"{fact['entity_type']}:{fact['entity_id']}:{fact['attribute']}:{fact['source_event_id']}"

        valid_to = None
        if fact["status"] in {"superseded", "contradicted"}:
            valid_to = fact["occurred_at"]

        conn.execute("""
            INSERT INTO facts
            (fact_id, entity_type, entity_id, attribute, value, status, confidence,
             valid_from, valid_to, source_event_ids, superseded_by, reasoning, scope_account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fact_id,
            fact["entity_type"],
            fact["entity_id"],
            fact["attribute"],
            json.dumps(fact["value"]),
            fact["status"],
            fact["confidence"],
            fact["occurred_at"],
            valid_to,
            json.dumps([fact["source_event_id"]]),
            fact.get("superseded_by"),
            fact["reasoning"],
            fact.get("scope_account_id")
        ))

    conn.commit()

    return {
        "facts_total": len(resolved_facts),
        "facts_current": sum(1 for f in resolved_facts if f["status"] == "current"),
        "facts_superseded": sum(1 for f in resolved_facts if f["status"] == "superseded"),
        "facts_contradicted": sum(1 for f in resolved_facts if f["status"] == "contradicted"),
        "facts_unverified": sum(1 for f in resolved_facts if f["status"] == "unverified"),
        "ambiguous_pairs": len(ambiguous_pairs),
    }
