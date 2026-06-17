"""
context.py — Build compact, evidence-linked context and explain views.
"""

import json
import hashlib
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import sqlite3


def build_context(conn: sqlite3.Connection, entity_id: str, entity_type: str = "account") -> Dict[str, Any]:
    """Build compact context for an entity."""

    cur = conn.cursor()

    # Account-level facts
    account_facts = []
    for row in cur.execute("""
        SELECT * FROM facts 
        WHERE entity_type = ? AND entity_id = ?
        ORDER BY attribute, valid_from
    """, (entity_type, entity_id)):
        fact = dict(row)
        fact["value"] = json.loads(fact["value"])
        fact["source_event_ids"] = json.loads(fact["source_event_ids"])
        account_facts.append(fact)

    # Find related entities
    related_events = cur.execute("""
        SELECT DISTINCT entity_type, entity_id, related_entity_ids 
        FROM raw_events 
        WHERE entity_id = ? OR ? IN (SELECT value FROM json_each(related_entity_ids))
    """, (entity_id, entity_id)).fetchall()

    related_entity_ids = set()
    for row in related_events:
        if row["entity_id"] != entity_id:
            related_entity_ids.add((row["entity_type"], row["entity_id"]))
        try:
            related = json.loads(row["related_entity_ids"])
            for rel_id in related:
                if rel_id.startswith("contact_"):
                    related_entity_ids.add(("contact", rel_id))
                elif rel_id.startswith("ticket_"):
                    related_entity_ids.add(("ticket", rel_id))
                elif rel_id.startswith("acct_"):
                    related_entity_ids.add(("account", rel_id))
                elif rel_id.startswith("policy_"):
                    related_entity_ids.add(("policy", rel_id))
        except:
            pass

    # Get facts for related entities
    related_facts = []
    for rel_type, rel_id in related_entity_ids:
        for row in cur.execute("""
            SELECT * FROM facts 
            WHERE entity_type = ? AND entity_id = ? AND status IN ('current', 'superseded', 'contradicted')
            ORDER BY attribute, valid_from
        """, (rel_type, rel_id)):
            fact = dict(row)
            fact["value"] = json.loads(fact["value"])
            fact["source_event_ids"] = json.loads(fact["source_event_ids"])
            related_facts.append(fact)

    # Get policy facts scoped to this account
    policies = []
    for row in cur.execute("""
        SELECT * FROM facts 
        WHERE entity_type = 'policy' AND scope_account_id = ? AND status = 'current'
        ORDER BY attribute
    """, (entity_id,)):
        fact = dict(row)
        fact["value"] = json.loads(fact["value"])
        fact["source_event_ids"] = json.loads(fact["source_event_ids"])
        policies.append(fact)

    # Get unverified claims
    unverified = []
    for row in cur.execute("""
        SELECT * FROM facts 
        WHERE entity_id = ? AND status = 'unverified'
        ORDER BY attribute
    """, (entity_id,)):
        fact = dict(row)
        fact["value"] = json.loads(fact["value"])
        fact["source_event_ids"] = json.loads(fact["source_event_ids"])
        unverified.append(fact)

    # Get ambiguous identities
    ambiguous = []
    contact_ids = [rel_id for rel_type, rel_id in related_entity_ids if rel_type == "contact"]
    contact_ids.append(entity_id)

    if contact_ids:
        placeholders = ",".join("?" * len(contact_ids))
        for row in cur.execute(f"""
            SELECT * FROM ambiguous_identities 
            WHERE entity_a IN ({placeholders}) OR entity_b IN ({placeholders})
        """, contact_ids + contact_ids):
            amb = dict(row)
            if not any(a.get("pair_id") == amb["pair_id"] for a in ambiguous):
                ambiguous.append(amb)

    # Get system flags
    system_flags = []

    # Find all event IDs related to this entity (including through related_entity_ids)
    all_event_rows = cur.execute("""
        SELECT event_id FROM raw_events 
        WHERE entity_id = ? OR ? IN (SELECT value FROM json_each(related_entity_ids))
    """, (entity_id, entity_id)).fetchall()
    all_event_ids = [r["event_id"] for r in all_event_rows]

    # Idempotency conflicts
    if all_event_ids:
        placeholders = ",".join("?" * len(all_event_ids))
        for row in cur.execute(f"""
            SELECT * FROM ingestion_conflicts 
            WHERE original_event_id IN ({placeholders}) OR conflicting_event_id IN ({placeholders})
        """, all_event_ids + all_event_ids):
            system_flags.append({
                "type": "idempotency_conflict",
                "details": dict(row)
            })

    # Duplicate candidates
    if all_event_ids:
        placeholders = ",".join("?" * len(all_event_ids))
        for row in cur.execute(f"""
            SELECT * FROM duplicate_candidates 
            WHERE event_id IN ({placeholders})
        """, all_event_ids):
            system_flags.append({
                "type": "duplicate_candidate",
                "details": dict(row)
            })

    context = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "account_facts": _format_facts([f for f in account_facts if f["status"] == "current"]),
        "superseded_facts": _format_facts([f for f in account_facts if f["status"] == "superseded"]),
        "contradicted_facts": _format_facts([f for f in account_facts if f["status"] == "contradicted"]),
        "related_facts": _format_facts([f for f in related_facts if f["status"] == "current"]),
        "policies": _format_facts(policies),
        "unverified_claims": _format_facts(unverified),
        "ambiguous_identities": ambiguous,
        "system_flags": system_flags,
    }

    return context


def _format_facts(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Format facts for output with evidence."""
    formatted = []
    for fact in facts:
        formatted.append({
            "attribute": fact["attribute"],
            "value": fact["value"],
            "status": fact["status"],
            "confidence": fact["confidence"],
            "valid_from": fact["valid_from"],
            "valid_to": fact["valid_to"],
            "source_event_ids": fact["source_event_ids"],
            "reasoning": fact["reasoning"],
            "scope_account_id": fact.get("scope_account_id"),
        })
    return formatted


def explain_fact(conn: sqlite3.Connection, fact_id: str) -> Dict[str, Any]:
    """Explain why the system believes a specific fact."""
    row = conn.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
    if not row:
        return {"error": f"Fact {fact_id} not found"}

    fact = dict(row)
    fact["value"] = json.loads(fact["value"])
    fact["source_event_ids"] = json.loads(fact["source_event_ids"])

    # Find competing facts
    competing = []
    for r in conn.execute("""
        SELECT * FROM facts 
        WHERE entity_type = ? AND entity_id = ? AND attribute = ? AND fact_id != ?
        ORDER BY valid_from
    """, (fact["entity_type"], fact["entity_id"], fact["attribute"], fact_id)):
        comp = dict(r)
        comp["value"] = json.loads(comp["value"])
        comp["source_event_ids"] = json.loads(comp["source_event_ids"])
        competing.append(comp)

    # Get source events
    sources = []
    for event_id in fact["source_event_ids"]:
        event_row = conn.execute("SELECT * FROM raw_events WHERE event_id = ?", (event_id,)).fetchone()
        if event_row:
            event = dict(event_row)
            event["payload"] = json.loads(event["payload"])
            event["related_entity_ids"] = json.loads(event["related_entity_ids"])
            sources.append(event)

    return {
        "fact_id": fact_id,
        "entity_type": fact["entity_type"],
        "entity_id": fact["entity_id"],
        "attribute": fact["attribute"],
        "value": fact["value"],
        "status": fact["status"],
        "confidence": fact["confidence"],
        "reasoning": fact["reasoning"],
        "valid_from": fact["valid_from"],
        "valid_to": fact["valid_to"],
        "sources": sources,
        "competing_facts": competing,
    }


def build_context_with_diff(conn: sqlite3.Connection, entity_id: str, entity_type: str = "account") -> Dict[str, Any]:
    """Build context and compute diff since last build."""
    context = build_context(conn, entity_id, entity_type)

    snapshot = _canonicalize_context(context)
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()

    prev = conn.execute("""
        SELECT snapshot_hash, snapshot_data FROM context_snapshots 
        WHERE entity_id = ? ORDER BY built_at DESC LIMIT 1
    """, (entity_id,)).fetchone()

    diff = {
        "first_build": prev is None,
        "previous_hash": prev["snapshot_hash"] if prev else None,
        "current_hash": snapshot_hash,
        "added": [],
        "changed": [],
        "removed": [],
        "unchanged_count": 0,
    }

    if prev:
        prev_snapshot = json.loads(prev["snapshot_data"])
        prev_by_key = {_item_key(item): item for item in prev_snapshot}
        curr_by_key = {_item_key(item): item for item in snapshot}

        all_keys = set(prev_by_key.keys()) | set(curr_by_key.keys())

        for key in all_keys:
            if key in curr_by_key and key not in prev_by_key:
                diff["added"].append(curr_by_key[key])
            elif key in prev_by_key and key not in curr_by_key:
                diff["removed"].append(prev_by_key[key])
            elif key in prev_by_key and key in curr_by_key:
                if json.dumps(prev_by_key[key], sort_keys=True) != json.dumps(curr_by_key[key], sort_keys=True):
                    diff["changed"].append({
                        "from": prev_by_key[key],
                        "to": curr_by_key[key]
                    })
                else:
                    diff["unchanged_count"] += 1
    else:
        diff["added"] = snapshot

    conn.execute("""
        INSERT INTO context_snapshots (entity_id, snapshot_hash, snapshot_data)
        VALUES (?, ?, ?)
    """, (entity_id, snapshot_hash, json.dumps(snapshot)))
    conn.commit()

    context["diff"] = diff
    return context


def _canonicalize_context(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reduce context to canonical, timestamp-free tuples for diffing."""
    tuples = []

    for section in ["account_facts", "related_facts", "policies"]:
        for fact in context.get(section, []):
            tuples.append({
                "scope": f"{context['entity_type']}:{context['entity_id']}",
                "attribute": fact["attribute"],
                "value": fact["value"],
                "status": fact["status"],
            })

    return tuples


def _item_key(item: Dict[str, Any]) -> str:
    """Stable key for diff comparison."""
    return f"{item['scope']}:{item['attribute']}"
