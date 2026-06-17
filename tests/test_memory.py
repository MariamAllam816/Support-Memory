"""
test_memory.py — Comprehensive verification script.

Covers all four business questions from the brief plus the mid-test scope change.
Run with: python3 -m unittest tests.test_memory -v
"""

import json
import os
import tempfile
import unittest
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store, ingest, context as ctx

# Load real seed data
EVENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "events.json"


class MemoryServiceTests(unittest.TestCase):
    """Test suite covering all strategic edge cases in the real seed data."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.events = json.loads(EVENTS_PATH.read_text())
        self.conn = store.connect(self.db_path)
        store.init_schema(self.conn)
        self.ingest_stats = ingest.ingest_events(self.conn, self.events)
        self.fact_report = ingest.build_facts(self.conn)

    def tearDown(self):
        self.conn.close()

    # =================================================================
    # 1. IDEMPOTENCY & DUPLICATE HANDLING
    # =================================================================

    def test_idempotency_conflict_detected(self):
        """evt-1008 and evt-1009 share idem-1008 but have different bodies/tickets.

        This is the CRITICAL trap: evt-1008's text CLAIMS it's a retry of evt-1004,
        but its actual idempotency_key is idem-1008 (different from evt-1004's idem-1004).
        The system must trust the structured field, not the narrative text.
        """
        self.assertEqual(self.ingest_stats["idempotency_conflict"], 1)

        row = self.conn.execute(
            "SELECT * FROM ingestion_conflicts WHERE idempotency_key = 'idem-1008'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["original_event_id"], "evt-1008")
        self.assertEqual(row["conflicting_event_id"], "evt-1009")

        # Both raw events preserved as evidence
        for eid in ("evt-1008", "evt-1009"):
            r = self.conn.execute(
                "SELECT 1 FROM raw_events WHERE event_id = ?", (eid,)
            ).fetchone()
            self.assertIsNotNone(r, f"{eid} must be preserved as raw evidence")

    def test_true_retry_is_noop(self):
        """Re-send evt-1004 with same idempotency key and body — must not create new facts.

        Per Lina's scope change: true retries should return the same logical result
        WITHOUT creating a second raw event or re-confirming facts.
        """
        before = self.fact_report["facts_current"]
        raw_before = self.conn.execute("SELECT COUNT(*) as c FROM raw_events").fetchone()["c"]

        retry = dict(self.events[3])  # evt-1004
        self.assertEqual(retry["event_id"], "evt-1004")
        retry["event_id"] = "evt-1004-retry"

        stats = ingest.ingest_events(self.conn, [retry])
        self.assertEqual(stats["duplicate_noop"], 1)
        self.assertEqual(stats["new"], 0)

        # CRITICAL: No second raw event created
        raw_after = self.conn.execute("SELECT COUNT(*) as c FROM raw_events").fetchone()["c"]
        self.assertEqual(raw_after, raw_before, "True retry must NOT create a second raw event")

        # No duplicate candidate created for the retry
        dup = self.conn.execute(
            "SELECT 1 FROM duplicate_candidates WHERE event_id = 'evt-1004-retry'"
        ).fetchone()
        self.assertIsNone(dup, "True retry must NOT create duplicate candidate entries")

        report_after = ingest.build_facts(self.conn)
        self.assertEqual(report_after["facts_current"], before)

    def test_replaying_same_event_id_is_skipped(self):
        """Ingesting the same batch twice must not double-count."""
        stats = ingest.ingest_events(self.conn, self.events)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["skipped_already_ingested"], len(self.events))

    def test_evt_1008_not_treated_as_retry_of_evt_1004(self):
        """The deliberate trap: evt-1008 has idem-1008, NOT idem-1004.

        Despite its text claiming to be a retry of evt-1004, the structured
        idempotency_key field says otherwise. The system must NOT treat it
        as a duplicate of evt-1004 at the idempotency level.
        """
        # evt-1008 should be treated as a new event (different idem key from evt-1004)
        row = self.conn.execute(
            "SELECT ingest_status FROM raw_events WHERE event_id = 'evt-1008'"
        ).fetchone()
        self.assertEqual(row["ingest_status"], "new")

        # It should NOT be in duplicate_noop (that's only for same idem key + same body)
        self.assertNotEqual(row["ingest_status"], "duplicate_noop")

        # The soft duplicate signal (retry_of in payload) should be in duplicate_candidates
        dup = self.conn.execute(
            "SELECT 1 FROM duplicate_candidates WHERE event_id = 'evt-1008'"
        ).fetchone()
        self.assertIsNotNone(dup)

    # =================================================================
    # 2. CONTRADICTION, STALENESS & CORRECTION
    # =================================================================

    def test_contradiction_resolution_contract_wins(self):
        """Helios plan: evt-1001 (Starter, medium) vs evt-1003 (Enterprise, high).

        The contract (high reliability) should win over CRM (medium).
        evt-1018 (old note, low, self-flagged old_fact) should be superseded.
        """
        cur = self.conn.cursor()
        rows = cur.execute("""
            SELECT * FROM facts 
            WHERE entity_type='account' AND entity_id='acct_helios_478' AND attribute='plan'
            ORDER BY valid_from
        """).fetchall()

        self.assertEqual(len(rows), 3)  # evt-1001, evt-1003, evt-1018

        by_event = {json.loads(r["source_event_ids"])[0]: dict(r) for r in rows}

        # evt-1003 (Enterprise, high reliability) should be current
        self.assertEqual(by_event["evt-1003"]["status"], "current")
        self.assertEqual(json.loads(by_event["evt-1003"]["value"]), "Enterprise Support")

        # evt-1001 (Starter, medium) should be superseded
        self.assertEqual(by_event["evt-1001"]["status"], "superseded")

        # evt-1018 (Starter, low, old_fact) should be superseded
        self.assertEqual(by_event["evt-1018"]["status"], "superseded")
        self.assertIn("old", by_event["evt-1018"]["reasoning"].lower())

    def test_explicit_correction_wins(self):
        """Affected seats: evt-1006 (42) vs evt-1007 (48, correction=true).

        evt-1007 explicitly claims to correct evt-1006. The correction signal
        gives it +50 score, making it the current fact.
        """
        rows = self.conn.execute("""
            SELECT * FROM facts 
            WHERE entity_type='ticket' AND entity_id='ticket_h_478_p1' AND attribute='affected_seats'
            ORDER BY valid_from
        """).fetchall()

        self.assertEqual(len(rows), 2)
        by_event = {json.loads(r["source_event_ids"])[0]: dict(r) for r in rows}

        # evt-1007 (48, correction) should be current
        self.assertEqual(json.loads(by_event["evt-1007"]["value"]), 48)
        self.assertEqual(by_event["evt-1007"]["status"], "current")
        self.assertIn("correction", by_event["evt-1007"]["reasoning"].lower())

        # evt-1006 (42) should be superseded
        self.assertEqual(by_event["evt-1006"]["status"], "superseded")

    def test_explicit_supersede_changes_preference(self):
        """Contact preference: evt-1002 (WhatsApp) vs evt-1013 (email_only_except_p1, supersedes).

        evt-1013's supersedes signal should mark evt-1002 as superseded.
        """
        rows = self.conn.execute("""
            SELECT * FROM facts 
            WHERE entity_type='contact' AND entity_id='contact_mona_478' AND attribute='preference'
            ORDER BY valid_from
        """).fetchall()

        self.assertEqual(len(rows), 2)
        by_event = {json.loads(r["source_event_ids"])[0]: dict(r) for r in rows}

        # evt-1013 should be current
        self.assertEqual(json.loads(by_event["evt-1013"]["value"]), "email_only_except_p1")
        self.assertEqual(by_event["evt-1013"]["status"], "current")

        # evt-1002 should be superseded
        self.assertEqual(by_event["evt-1002"]["status"], "superseded")

    def test_stale_fact_is_superseded_not_dropped(self):
        """evt-1018 is self-flagged as old_fact. It must exist but be marked superseded."""
        row = self.conn.execute("""
            SELECT * FROM facts WHERE source_event_ids = ?
        """, (json.dumps(["evt-1018"]),)).fetchone()

        self.assertIsNotNone(row, "evt-1018's fact must still exist")
        self.assertEqual(row["status"], "superseded")
        self.assertIn("old", row["reasoning"].lower())

    # =================================================================
    # 3. AMBIGUOUS IDENTITY (NEVER AUTO-MERGE)
    # =================================================================

    def test_ambiguous_identity_detected_not_merged(self):
        """Mona Salem, M. Salem, and Omar Adel share phone numbers.

        The system must flag all pairs but NEVER merge them automatically.
        """
        rows = self.conn.execute("SELECT * FROM ambiguous_identities").fetchall()
        pairs = {frozenset((r["entity_a"], r["entity_b"])) for r in rows}

        # Should detect shared phone between M. Salem and Omar
        self.assertTrue(
            any("contact_m_salem_478" in p and "contact_omar_478" in p for p in pairs),
            "M. Salem and Omar should be flagged as ambiguous"
        )

        # All contacts should remain distinct
        contact_facts = self.conn.execute("""
            SELECT DISTINCT entity_id FROM facts WHERE entity_type='contact'
        """).fetchall()
        contact_ids = {r["entity_id"] for r in contact_facts}

        self.assertIn("contact_mona_478", contact_ids)
        self.assertIn("contact_m_salem_478", contact_ids)
        self.assertIn("contact_omar_478", contact_ids)

    # =================================================================
    # 4. SCOPE ISOLATION (POLICY ANTI-LEAKAGE)
    # =================================================================

    def test_policy_does_not_leak_across_accounts(self):
        """Nova's no_training policy must NOT appear in Delta's context.

        evt-1011 (policy) has scope_account_id=acct_nova_478.
        evt-1012 (Delta) is an unverified guess, not a real policy.
        """
        delta_ctx = ctx.build_context(self.conn, "acct_delta_478")

        # Delta should have no policies from Nova
        self.assertEqual(delta_ctx["policies"], [], 
                        "Delta must not inherit Nova's policy")

        # Delta's unverified guess should be visible but clearly separated
        self.assertTrue(
            any(f["attribute"] == "unverified_policy_guess" for f in delta_ctx["unverified_claims"]),
            "Delta's unverified guess should be in unverified_claims"
        )

        # Nova should have its actual policy
        nova_ctx = ctx.build_context(self.conn, "acct_nova_478")
        self.assertTrue(
            any(f["attribute"] == "no_training" for f in nova_ctx["policies"]),
            "Nova should have its no_training policy"
        )

    def test_nova_policy_scoped_correctly(self):
        """Policy facts must carry scope_account_id."""
        rows = self.conn.execute("""
            SELECT * FROM facts 
            WHERE entity_type='policy' AND attribute='no_training'
        """).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope_account_id"], "acct_nova_478")

    # =================================================================
    # 5. COMPACT, EVIDENCE-LINKED CONTEXT
    # =================================================================

    def test_account_context_includes_evidence(self):
        """Context for Helios must include source event IDs and reasoning."""
        result = ctx.build_context(self.conn, "acct_helios_478")

        # Should have current plan fact with evidence
        plan_facts = [f for f in result["account_facts"] if f["attribute"] == "plan"]
        self.assertEqual(len(plan_facts), 1)
        self.assertEqual(plan_facts[0]["value"], "Enterprise Support")
        self.assertIn("evt-1003", plan_facts[0]["source_event_ids"])
        self.assertTrue(len(plan_facts[0]["reasoning"]) > 0)

    def test_explain_shows_competing_facts(self):
        """Explain view for a fact must show competing facts and reasoning."""
        explanation = ctx.explain_fact(
            self.conn, "account:acct_helios_478:plan:evt-1003"
        )

        self.assertEqual(explanation["status"], "current")
        self.assertTrue(len(explanation["competing_facts"]) >= 2)

        # Should include the superseded Starter facts
        competing_values = [f["value"] for f in explanation["competing_facts"]]
        self.assertIn("Starter", competing_values)

    def test_context_includes_related_entities(self):
        """Helios context should include Mona Salem's contact facts."""
        result = ctx.build_context(self.conn, "acct_helios_478")

        # Should have related facts from contact_mona_478
        related_prefs = [f for f in result["related_facts"] 
                        if f["attribute"] == "preference"]
        self.assertTrue(len(related_prefs) > 0)

    # =================================================================
    # 6. DIFF / DIGEST (Mid-test scope change)
    # =================================================================

    def test_diff_first_build(self):
        """First build should report first_build=true."""
        result = ctx.build_context_with_diff(self.conn, "acct_helios_478")
        self.assertTrue(result["diff"]["first_build"])

    def test_diff_no_change(self):
        """Rebuilding with no new events should show no changes."""
        ctx.build_context_with_diff(self.conn, "acct_helios_478")  # First build
        result = ctx.build_context_with_diff(self.conn, "acct_helios_478")  # Second build

        self.assertFalse(result["diff"]["first_build"])
        self.assertEqual(result["diff"]["added"], [])
        self.assertEqual(result["diff"]["changed"], [])
        self.assertEqual(result["diff"]["removed"], [])

    def test_diff_catches_real_change(self):
        """Adding a new event should show up in the diff."""
        ctx.build_context_with_diff(self.conn, "acct_helios_478")  # Baseline

        # Add a change
        new_event = {
            "event_id": "evt-test-change",
            "idempotency_key": "idem-test-change",
            "occurred_at": "2026-04-20T09:00:00Z",
            "source": "contract",
            "actor": "contract_importer",
            "entity_type": "account",
            "entity_id": "acct_helios_478",
            "related_entity_ids": [],
            "reliability": "high",
            "text": "Helios moved to Business plan.",
            "payload": {"plan": "Business"},
        }
        ingest.ingest_events(self.conn, [new_event])
        ingest.build_facts(self.conn)

        result = ctx.build_context_with_diff(self.conn, "acct_helios_478")

        # Should detect plan change
        changed_attrs = []
        for change in result["diff"]["changed"]:
            if isinstance(change, dict) and "to" in change:
                changed_attrs.append(change["to"].get("attribute"))

        self.assertIn("plan", changed_attrs)

    # =================================================================
    # 7. UNVERIFIED CLAIMS HANDLING
    # =================================================================

    def test_unverified_policy_guess_flagged(self):
        """Delta's policy guess (evt-1012, low reliability) must be marked unverified."""
        rows = self.conn.execute("""
            SELECT * FROM facts 
            WHERE entity_id='acct_delta_478' AND attribute='unverified_policy_guess'
        """).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "unverified")
        self.assertLess(rows[0]["confidence"], 0.5)

    # =================================================================
    # 8. SYSTEM FLAGS
    # =================================================================

    def test_system_flags_in_context(self):
        """Context should include idempotency conflicts and duplicate candidates."""
        result = ctx.build_context(self.conn, "acct_helios_478")

        # Should have the evt-1008/evt-1009 conflict
        conflict_flags = [f for f in result["system_flags"] 
                         if f["type"] == "idempotency_conflict"]
        self.assertTrue(len(conflict_flags) > 0)

        # Should have the evt-1005 duplicate candidate
        dup_flags = [f for f in result["system_flags"] 
                    if f["type"] == "duplicate_candidate"]
        self.assertTrue(len(dup_flags) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
