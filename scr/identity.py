"""
identity.py — Detect ambiguous identity matches, never auto-merge.

Heuristics:
1. Shared phone number in payload across different contacts
2. Text mentions of ambiguity ("same phone", "do not merge", "may be the same person")
"""

import json
import re
from typing import List, Dict, Any, Set, Tuple
from dataclasses import dataclass


AMBIGUITY_PATTERNS = [
    r"same phone",
    r"do not merge",
    r"may be the same",
    r"shares? phone",
    r"same number",
]


def detect_ambiguous_identities(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Scan events and return ambiguous identity pairs."""
    pairs = []
    seen_pairs = set()

    # Heuristic 1: Shared phone numbers across different contacts
    phone_to_contacts = {}
    for event in events:
        if event["entity_type"] != "contact":
            continue
        phone = event.get("payload", {}).get("phone")
        if phone:
            phone_to_contacts.setdefault(phone, []).append(event["entity_id"])

    for phone, contacts in phone_to_contacts.items():
        if len(contacts) > 1:
            for i in range(len(contacts)):
                for j in range(i + 1, len(contacts)):
                    pair = tuple(sorted([contacts[i], contacts[j]]))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        pairs.append({
                            "entity_a": pair[0],
                            "entity_b": pair[1],
                            "basis": f"shared_phone:{phone}",
                            "status": "needs_review"
                        })

    # Heuristic 2: Text mentions of ambiguity
    for event in events:
        text = event.get("text", "").lower()
        for pattern in AMBIGUITY_PATTERNS:
            if re.search(pattern, text):
                # If this event mentions ambiguity, flag related entities
                related = event.get("related_entity_ids", [])
                entity = event["entity_id"]
                all_entities = [entity] + related

                for i in range(len(all_entities)):
                    for j in range(i + 1, len(all_entities)):
                        if all_entities[i] == all_entities[j]:
                            continue
                        pair = tuple(sorted([all_entities[i], all_entities[j]]))
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            pairs.append({
                                "entity_a": pair[0],
                                "entity_b": pair[1],
                                "basis": "text_mention_ambiguity",
                                "status": "needs_review"
                            })

    return pairs
