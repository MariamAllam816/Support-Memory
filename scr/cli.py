"""
cli.py — Command-line entry point for the memory service.
"""

import argparse
import json
import sys
from pathlib import Path

from . import store, ingest, context as ctx


def main():
    parser = argparse.ArgumentParser(description="Support Memory Reliability Layer")
    parser.add_argument("--db", default="memory.db", help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command")

    # ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest events from JSON file")
    ingest_parser.add_argument("--file", default="data/events.json", help="Events JSON file")
    ingest_parser.add_argument("--reset", action="store_true", help="Reset database before ingest")

    # context command
    context_parser = subparsers.add_parser("context", help="Build context for an entity")
    context_parser.add_argument("entity_id", help="Entity ID (e.g., acct_helios_478)")
    context_parser.add_argument("--type", default="account", help="Entity type")
    context_parser.add_argument("--json", action="store_true", help="Output as JSON")
    context_parser.add_argument("--diff", action="store_true", help="Include diff since last build")

    # explain command
    explain_parser = subparsers.add_parser("explain", help="Explain a specific fact")
    explain_parser.add_argument("fact_id", help="Fact ID (e.g., account:acct_helios_478:plan:evt-1003)")

    # flags command
    flags_parser = subparsers.add_parser("flags", help="Show all system flags")

    # list-facts command
    list_parser = subparsers.add_parser("list-facts", help="List facts for an entity")
    list_parser.add_argument("entity_id", help="Entity ID")
    list_parser.add_argument("--type", default="account", help="Entity type")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = store.connect(args.db)

    if args.command == "ingest":
        if args.reset:
            store.reset_database(conn)
        else:
            store.init_schema(conn)

        events_path = Path(args.file)
        if not events_path.exists():
            print(f"Error: {events_path} not found")
            sys.exit(1)

        events = json.loads(events_path.read_text())
        stats = ingest.ingest_events(conn, events)
        print(f"Ingested {stats['new']} new, {stats['duplicate_noop']} duplicate no-ops, "
              f"{stats['idempotency_conflict']} conflicts, {stats['skipped_already_ingested']} skipped")

        fact_stats = ingest.build_facts(conn)
        print(f"Derived {fact_stats['facts_total']} facts: "
              f"{fact_stats['facts_current']} current, "
              f"{fact_stats['facts_superseded']} superseded, "
              f"{fact_stats['facts_contradicted']} contradicted, "
              f"{fact_stats['facts_unverified']} unverified")
        print(f"Detected {fact_stats['ambiguous_pairs']} ambiguous identity pairs")

    elif args.command == "context":
        store.init_schema(conn)
        if args.diff:
            result = ctx.build_context_with_diff(conn, args.entity_id, args.type)
        else:
            result = ctx.build_context(conn, args.entity_id, args.type)

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_context_human_readable(result)

    elif args.command == "explain":
        store.init_schema(conn)
        result = ctx.explain_fact(conn, args.fact_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "flags":
        store.init_schema(conn)
        _print_flags(conn)

    elif args.command == "list-facts":
        store.init_schema(conn)
        _list_facts(conn, args.entity_id, args.type)

    conn.close()


def _print_context_human_readable(context: dict):
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"CONTEXT: {context['entity_type'].upper()} {context['entity_id']}")
    print(f"Built at: {context['built_at']}")
    print(f"{sep}\n")

    if context["account_facts"]:
        print("CURRENT FACTS:")
        for fact in context["account_facts"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"(confidence: {fact['confidence']:.2f}) "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")

    if context["superseded_facts"]:
        print("\nSUPERSEDED FACTS (still visible for traceability):")
        for fact in context["superseded_facts"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")
            print(f"    Reason: {fact['reasoning']}")

    if context["contradicted_facts"]:
        print("\nCONTRADICTED FACTS (human review needed):")
        for fact in context["contradicted_facts"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")
            print(f"    Reason: {fact['reasoning']}")

    if context["policies"]:
        print("\nPOLICIES (account-scoped):")
        for fact in context["policies"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")

    if context["unverified_claims"]:
        print("\nUNVERIFIED CLAIMS:")
        for fact in context["unverified_claims"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"(confidence: {fact['confidence']:.2f}) "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")

    if context["related_facts"]:
        print("\nRELATED ENTITY FACTS:")
        for fact in context["related_facts"]:
            print(f"  - {fact['attribute']}: {fact['value']} "
                  f"[sources: {', '.join(fact['source_event_ids'])}]")

    if context["ambiguous_identities"]:
        print("\nAMBIGUOUS IDENTITIES (NEVER AUTO-MERGED):")
        for amb in context["ambiguous_identities"]:
            print(f"  - {amb['entity_a']} <-> {amb['entity_b']} "
                  f"(basis: {amb['basis']})")

    if context["system_flags"]:
        print("\nSYSTEM FLAGS:")
        for flag in context["system_flags"]:
            print(f"  - {flag['type']}: {json.dumps(flag['details'], default=str)}")

    if "diff" in context:
        diff = context["diff"]
        print(f"\n{sep}")
        print("DIFF SINCE LAST BUILD:")
        print(f"{sep}")
        if diff["first_build"]:
            print("  (First build -- no previous snapshot)")
        else:
            print(f"  Previous hash: {diff['previous_hash'][:16]}...")
            print(f"  Current hash:  {diff['current_hash'][:16]}...")
            print(f"  Added: {len(diff['added'])}")
            print(f"  Changed: {len(diff['changed'])}")
            print(f"  Removed: {len(diff['removed'])}")
            print(f"  Unchanged: {diff['unchanged_count']}")

    print(f"\n{sep}\n")


def _print_flags(conn):
    sep = "=" * 60
    print(f"\n{sep}")
    print("SYSTEM FLAGS OVERVIEW")
    print(f"{sep}\n")

    print("IDEMPOTENCY CONFLICTS:")
    for row in conn.execute("SELECT * FROM ingestion_conflicts"):
        print(f"  - Key '{row['idempotency_key']}': {row['original_event_id']} vs {row['conflicting_event_id']}")
        print(f"    Original hash: {row['original_body_hash'][:16]}...")
        print(f"    Conflicting hash: {row['conflicting_body_hash'][:16]}...")

    print("\nDUPLICATE CANDIDATES:")
    for row in conn.execute("SELECT * FROM duplicate_candidates"):
        print(f"  - {row['event_id']} may duplicate {row['possible_duplicate_of']} ({row['basis']})")

    print("\nAMBIGUOUS IDENTITIES:")
    for row in conn.execute("SELECT * FROM ambiguous_identities"):
        print(f"  - {row['entity_a']} <-> {row['entity_b']} ({row['basis']}) [{row['status']}]")

    print(f"\n{sep}\n")


def _list_facts(conn, entity_id, entity_type):
    print(f"\nFacts for {entity_type}:{entity_id}:\n")
    for row in conn.execute("""
        SELECT fact_id, attribute, value, status, confidence, valid_from, source_event_ids
        FROM facts WHERE entity_type = ? AND entity_id = ? ORDER BY attribute, valid_from
    """, (entity_type, entity_id)):
        print(f"  {row['fact_id']}")
        print(f"    {row['attribute']} = {row['value']} [{row['status']}, confidence={row['confidence']:.2f}]")
        print(f"    Sources: {row['source_event_ids']}")
        print()


if __name__ == "__main__":
    main()
