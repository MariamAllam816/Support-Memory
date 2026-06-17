"""
resolve.py — Score competing facts and determine current/superseded/contradicted status.

Resolution formula (fully transparent, explainable):
  score = reliability_weight + recency_rank + signal_override

Where:
  reliability_weight: high=30, medium=20, low=10
  recency_rank: +1 per event in thread, later events score higher
  signal_override:
    correction=True: +50
    supersedes=...: +50
    old_fact=True: -50
    unverified_policy_guess: -9999 (never wins, always unverified)

Ties with disagreeing values → contradicted (never guess).
"""

import json
from typing import List, Dict, Any, Tuple


RELIABILITY_WEIGHTS = {
    "high": 30,
    "medium": 20,
    "low": 10,
}


def compute_score(event: Dict[str, Any], recency_rank: int, is_unverified: bool = False) -> Tuple[float, str]:
    """Compute resolution score and reasoning string."""

    # Unverified facts get a massive penalty so they NEVER win against verified facts
    if is_unverified:
        return -9999, "STATUS=unverified (unverified guess, never competes with verified sources)"

    reliability = event.get("reliability", "medium")
    payload = event.get("payload", {})

    base = RELIABILITY_WEIGHTS.get(reliability, 20)
    recency = recency_rank

    overrides = []
    bonus = 0

    if payload.get("correction"):
        bonus += 50
        overrides.append("explicit correction (+50)")

    if payload.get("supersedes"):
        bonus += 50
        overrides.append(f"explicit supersede of '{payload['supersedes']}' (+50)")

    if payload.get("old_fact"):
        bonus -= 50
        overrides.append("self-flagged old fact (-50)")

    score = base + recency + bonus

    reasoning_parts = [
        f"reliability={reliability} (weight={base})",
        f"recency_rank={recency}",
    ]
    if overrides:
        reasoning_parts.extend(overrides)
    reasoning_parts.append(f"TOTAL={score}")

    reasoning = "; ".join(reasoning_parts)
    return score, reasoning


def resolve_fact_thread(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Given a list of facts for the same (entity_type, entity_id, attribute),
    assign statuses: current, superseded, contradicted, corroborating, unverified.

    Returns the facts with status and reasoning populated.
    """
    if not facts:
        return []

    # Sort by occurred_at to establish recency
    sorted_facts = sorted(facts, key=lambda f: f["occurred_at"])

    # Compute scores
    scored = []
    for i, fact in enumerate(sorted_facts):
        event = fact["_source_event"]
        is_unverified = fact.get("_is_unverified", False)
        score, reasoning = compute_score(event, i + 1, is_unverified)
        scored.append({
            **fact,
            "score": score,
            "reasoning": reasoning,
            "recency_rank": i + 1,
        })

    # Separate verified from unverified
    verified = [f for f in scored if not f.get("_is_unverified", False)]
    unverified = [f for f in scored if f.get("_is_unverified", False)]

    if not verified:
        # All unverified
        for fact in scored:
            fact["status"] = "unverified"
            fact["reasoning"] += "; STATUS=unverified (all sources are unverified guesses)"
        return scored

    # Resolve only among verified facts
    # Group by value to find corroborating facts
    value_groups = {}
    for fact in verified:
        val = json.dumps(fact["value"], sort_keys=True)
        value_groups.setdefault(val, []).append(fact)

    # Find the winning value (highest score among representatives)
    best_value = None
    best_score = -9999
    for val, group in value_groups.items():
        max_score = max(f["score"] for f in group)
        if max_score > best_score:
            best_score = max_score
            best_value = val

    # Check for ties at the top with different values
    top_values = []
    for val, group in value_groups.items():
        max_score = max(f["score"] for f in group)
        if max_score == best_score and max_score > 0:
            top_values.append(val)

    has_tie = len(top_values) > 1

    # Assign statuses to verified facts
    for fact in verified:
        val = json.dumps(fact["value"], sort_keys=True)

        if has_tie and fact["score"] == best_score:
            status = "contradicted"
            fact["reasoning"] += "; STATUS=contradicted (tied with disagreeing value, human review needed)"
        elif val == best_value and fact["score"] == best_score:
            status = "current"
            fact["reasoning"] += "; STATUS=current (highest score, no tie)"
        elif val == best_value:
            status = "corroborating"
            fact["reasoning"] += "; STATUS=corroborating (same value as current, lower score)"
        else:
            status = "superseded"
            fact["reasoning"] += "; STATUS=superseded (lower score than current)"

        fact["status"] = status

    # Unverified facts stay unverified
    for fact in unverified:
        fact["status"] = "unverified"
        fact["reasoning"] += "; STATUS=unverified (unverified guess, never trusted over verified sources)"

    return scored
