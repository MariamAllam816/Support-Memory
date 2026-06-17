"""
extract.py — Turn one raw event into candidate facts.

Rule-based extraction over a fixed payload schema. More accurate and 100%
traceable for this seed set. Production would add LLM extraction for free text.
"""

import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class ExtractedFact:
    entity_type: str
    entity_id: str
    attribute: str
    value: Any
    confidence_boost: float = 0.0
    is_correction: bool = False
    supersedes: Optional[str] = None
    is_old_fact: bool = False
    scope_account_id: Optional[str] = None
    source_event_id: str = ""


# Attribute whitelist per entity type
ATTRIBUTE_WHITELIST = {
    "account": {"account_name", "plan", "region", "segment"},
    "contact": {"contact_name", "preference", "phone", "email", "account_id"},
    "ticket": {"priority", "symptom", "affected_seats", "status", "root_cause", "topic"},
    "policy": {"scope_account_id", "no_training", "no_cross_account_analytics", "standard_response"},
}

# Reliability to base confidence mapping
RELIABILITY_MAP = {
    "high": 0.9,
    "medium": 0.6,
    "low": 0.3,
}


def extract_facts(event: Dict[str, Any]) -> List[ExtractedFact]:
    """Extract candidate facts from a single event."""
    facts = []
    entity_type = event["entity_type"]
    entity_id = event["entity_id"]
    payload = event.get("payload", {})
    reliability = event.get("reliability", "medium")
    base_confidence = RELIABILITY_MAP.get(reliability, 0.5)

    # Signal flags from payload
    is_correction = payload.get("correction", False)
    supersedes = payload.get("supersedes")
    is_old_fact = payload.get("old_fact", False)

    # Determine whitelisted attributes for this entity type
    allowed = ATTRIBUTE_WHITELIST.get(entity_type, set())

    for attr, value in payload.items():
        if attr in {"correction", "supersedes", "old_fact", "possible_duplicate_of", 
                    "retry_of", "same_body", "retry_bug", "different_body",
                    "unverified_policy_guess", "policy_hint", "warning"}:
            continue  # Skip meta/signal fields

        if attr not in allowed:
            continue  # Skip unknown attributes

        confidence = base_confidence
        if is_correction:
            confidence += 0.3  # Corrections get higher confidence
        if is_old_fact:
            confidence -= 0.4  # Self-flagged old facts get lower confidence

        # Clamp confidence
        confidence = max(0.1, min(1.0, confidence))

        # For policy facts, extract scope_account_id
        scope_account_id = None
        if entity_type == "policy" and attr in {"no_training", "no_cross_account_analytics"}:
            scope_account_id = payload.get("scope_account_id")

        # For account policy hints, treat as unverified policy facts
        if entity_type == "account" and attr == "policy_hint":
            # This is a hint, not a formal policy — still extract but mark differently
            pass

        fact = ExtractedFact(
            entity_type=entity_type,
            entity_id=entity_id,
            attribute=attr,
            value=value,
            confidence_boost=confidence,
            is_correction=is_correction,
            supersedes=supersedes,
            is_old_fact=is_old_fact,
            scope_account_id=scope_account_id,
            source_event_id=event["event_id"]
        )
        facts.append(fact)

    # Special handling for unverified policy guesses
    if payload.get("unverified_policy_guess"):
        fact = ExtractedFact(
            entity_type=entity_type,
            entity_id=entity_id,
            attribute="unverified_policy_guess",
            value=payload.get("policy_hint", "unknown_policy"),
            confidence_boost=0.2,  # Very low confidence
            is_correction=False,
            supersedes=None,
            is_old_fact=False,
            scope_account_id=None,
            source_event_id=event["event_id"]
        )
        facts.append(fact)

    # Special handling for policy hints on accounts (like Nova's no_training hint)
    if entity_type == "account" and "policy_hint" in payload:
        fact = ExtractedFact(
            entity_type=entity_type,
            entity_id=entity_id,
            attribute=payload["policy_hint"],
            value=True,
            confidence_boost=base_confidence,
            is_correction=False,
            supersedes=None,
            is_old_fact=False,
            scope_account_id=entity_id,  # Scope to this account
            source_event_id=event["event_id"]
        )
        facts.append(fact)

    return facts
