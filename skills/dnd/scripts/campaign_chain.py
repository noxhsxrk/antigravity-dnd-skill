#!/usr/bin/env python3
"""
campaign_chain.py — Campaign Chaining (continue into a sequel chapter in the same world).

Inspired by DungeonGPT-JS Campaign Chaining: finishes one campaign and seeds a
new sequel campaign sharing the same world, NPCs, relationship graph, lorebook,
and party characters (optionally healed to full HP & spell slots), while starting
with a clean state.md and session-log.md that carry forward continuity.

Usage:
    python3 campaign_chain.py --from <old-campaign> --to <new-campaign> [--title "Book II"] [--heal]
"""

import argparse
import os
import pathlib
import re
import shutil
import sys
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign, campaign_dir, campaign_ruleset
from economy import _apply_rest_to_sheet

CARRY_FILES = [
    "world.md",
    "world-nodes.md",
    "npcs.md",
    "npcs-full.md",
    "graph.json",
    "lorebook.json",
]


def _extract_section(text: str, heading: str) -> str:
    pat = re.compile(rf"(^##\s+{re.escape(heading)}\b.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


def chain_campaign(
    src_name: str,
    dst_name: str,
    title: str = "",
    heal: bool = True,
) -> dict:
    src = find_campaign(src_name)
    if not src.exists():
        raise FileNotFoundError(f"Source campaign not found: {src}")

    dst = campaign_dir(dst_name)
    if dst.exists():
        raise FileExistsError(f"Destination campaign already exists: {dst}")

    dst.mkdir(parents=True, exist_ok=True)
    ruleset = campaign_ruleset(src_name)

    copied_files = []
    for fname in CARRY_FILES:
        fsrc = src / fname
        if fsrc.exists():
            shutil.copy2(fsrc, dst / fname)
            copied_files.append(fname)

    # Carry party characters (update Campaign header & Campaign History, heal if requested)
    copied_chars = []
    src_chars = src / "characters"
    dst_chars = dst / "characters"
    if src_chars.exists():
        dst_chars.mkdir(parents=True, exist_ok=True)
        for cf in sorted(src_chars.glob("*.md")):
            text = cf.read_text(encoding="utf-8")
            text = re.sub(r"(\*\*Campaign:\*\*\s*)\S+", rf"\g<1>{dst_name}", text, count=1)
            # Record previous campaign in Campaign History if section exists
            if "**Previous campaigns:**" in text:
                text = re.sub(
                    r"(\*\*Previous campaigns:\*\*)(.*)",
                    lambda m: f"{m.group(1)} {(m.group(2).strip() + ', ' if m.group(2).strip() else '')}{src_name}",
                    text,
                    count=1,
                )
            if heal:
                text, _ = _apply_rest_to_sheet(text, "long")
            (dst_chars / cf.name).write_text(text, encoding="utf-8")
            copied_chars.append(cf.name)

    # Carry forward Faction Moves, DM Style Notes, and Live State Flags from old state.md
    old_state = ""
    if (src / "state.md").exists():
        old_state = (src / "state.md").read_text(encoding="utf-8", errors="replace")

    style_notes = _extract_section(old_state, "DM Style Notes") or "## DM Style Notes\n- Carried forward from previous campaign."
    live_flags = _extract_section(old_state, "Live State Flags") or "## Live State Flags\n- Sequel chapter begins."
    faction_moves = _extract_section(old_state, "Faction Moves") or "## Faction Moves\n- (Record between-chapter faction shifts)"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    display_title = title.strip() or dst_name
    new_state = f"""# {display_title} — Campaign State
**Campaign:** {dst_name}  **Ruleset:** {ruleset}  **Prequel:** {src_name}  **Started:** {today}

## Session Flags
- autorun: true
- roll_mode: players

## Current Situation
Sequel campaign continuing directly from `{src_name}`. The party {'begins fully rested' if heal else 'carries their exact prior condition'}.

{live_flags}

## Recent Events
- Concluded `{src_name}` and embarked on `{dst_name}`.

{faction_moves}

{style_notes}

## Continuity Archive
- Prequel campaign: `{src_name}` (chained on {today}).
"""
    (dst / "state.md").write_text(new_state, encoding="utf-8")

    new_log = f"""# {display_title} — Session Log
*Chained sequel to `{src_name}` on {today}.*
"""
    (dst / "session-log.md").write_text(new_log, encoding="utf-8")

    return {
        "from": src_name,
        "to": dst_name,
        "path": str(dst),
        "ruleset": ruleset,
        "copied_files": copied_files,
        "copied_characters": copied_chars,
        "healed": heal,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Chain a completed D&D campaign into a sequel chapter.")
    parser.add_argument("--from", dest="src", required=True, help="Source (completed) campaign name")
    parser.add_argument("--to", dest="dst", required=True, help="New sequel campaign name")
    parser.add_argument("--title", default="", help="Optional display title for the sequel")
    parser.add_argument("--no-heal", action="store_true", help="Do not apply a long rest to carried PCs")
    args = parser.parse_args()

    try:
        res = chain_campaign(args.src, args.dst, title=args.title, heal=not args.no_heal)
        print(f"Chained '{res['from']}' → '{res['to']}' (ruleset {res['ruleset']})")
        print(f"  Path: {res['path']}")
        print(f"  Carried files: {', '.join(res['copied_files']) or 'none'}")
        print(f"  Carried PCs ({'healed' if res['healed'] else 'as-is'}): {', '.join(res['copied_characters']) or 'none'}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
