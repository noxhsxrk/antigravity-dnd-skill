#!/usr/bin/env python3
"""
event_ledger.py — Append-only structured turn event ledger (events.jsonl).

Inspired by Daicer (`game-ledger.ts`): records typed campaign events (combat,
travel, loot, dialogue, quest, entropy, death, rest, skill) in JSONL format for
accurate session recaps, campaign analytics, and auditability.

Usage:
    python3 event_ledger.py -c <camp> log --type combat --actor "เนียม" --summary "Slain Goblin Shaman" [--meta '{"dmg":14}']
    python3 event_ledger.py -c <camp> tail [--limit 15]
    python3 event_ledger.py -c <camp> query [--type combat] [--actor "เนียม"]
    python3 event_ledger.py -c <camp> stats
"""

import argparse
import json
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign

VALID_TYPES = ("combat", "travel", "loot", "dialogue", "quest", "entropy", "death", "rest", "skill", "system")


def _ledger_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / "events.jsonl"


def log_event(
    campaign: str,
    event_type: str,
    summary: str,
    actor: str = "",
    session: "int | None" = None,
    meta: "dict | None" = None,
) -> dict:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "type": event_type.strip().lower(),
        "actor": actor.strip(),
        "summary": summary.strip(),
    }
    if session is not None:
        entry["session"] = int(session)
    if meta:
        entry["meta"] = meta
    with open(_ledger_path(campaign), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_events(campaign: str) -> list[dict]:
    p = _ledger_path(campaign)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Structured JSONL turn event ledger.")
    parser.add_argument("-c", "--campaign", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_log = sub.add_parser("log", help="Append a structured event")
    p_log.add_argument("--type", dest="etype", choices=VALID_TYPES, required=True)
    p_log.add_argument("--actor", default="")
    p_log.add_argument("--summary", required=True)
    p_log.add_argument("--session", type=int, default=None)
    p_log.add_argument("--meta", default="", help="Optional JSON object string")

    p_tail = sub.add_parser("tail", help="Show recent events")
    p_tail.add_argument("--limit", type=int, default=15)

    p_q = sub.add_parser("query", help="Filter events by type or actor")
    p_q.add_argument("--type", dest="etype", default="")
    p_q.add_argument("--actor", default="")

    sub.add_parser("stats", help="Show campaign event statistics")

    args = parser.parse_args()
    try:
        if args.cmd == "log":
            meta = json.loads(args.meta) if args.meta else None
            e = log_event(args.campaign, args.etype, args.summary, actor=args.actor, session=args.session, meta=meta)
            who = f" [{e['actor']}]" if e["actor"] else ""
            print(f"Logged ({e['type']}){who}: {e['summary']}")
        elif args.cmd == "tail":
            events = read_events(args.campaign)[-args.limit :]
            if not events:
                print("(no events logged yet)")
                return
            for e in events:
                who = f" {e['actor']}:" if e.get("actor") else ""
                print(f"  [{e.get('at', '')[:19]}] ({e.get('type')}){who} {e.get('summary')}")
        elif args.cmd == "query":
            events = read_events(args.campaign)
            if args.etype:
                events = [e for e in events if e.get("type", "").lower() == args.etype.lower()]
            if args.actor:
                events = [e for e in events if args.actor.lower() in e.get("actor", "").lower()]
            for e in events:
                who = f" {e['actor']}:" if e.get("actor") else ""
                print(f"  [{e.get('at', '')[:19]}] ({e.get('type')}){who} {e.get('summary')}")
            print(f"Total: {len(events)} event(s)")
        elif args.cmd == "stats":
            events = read_events(args.campaign)
            by_type = Counter(e.get("type", "unknown") for e in events)
            by_actor = Counter(e.get("actor") for e in events if e.get("actor"))
            print(f"Campaign '{args.campaign}' Event Ledger — {len(events)} total events")
            if by_type:
                print("  By type:  " + ", ".join(f"{k}={v}" for k, v in by_type.most_common()))
            if by_actor:
                print("  By actor: " + ", ".join(f"{k}={v}" for k, v in by_actor.most_common(6)))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
