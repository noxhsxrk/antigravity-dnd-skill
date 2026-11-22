#!/usr/bin/env python3
"""
lorebook.py — Pre-turn Auto-Lorebook scanner & lorebook_v3 / chara_card_v3 import/export.

Inspired by DiceFrame's Lorebook v2/v3 engine and content-sync contracts.
- `scan`: matches player action text against campaign NPCs (`npcs-full.md`,
  `npcs.md`), locations/nodes (`world.md`, `world-nodes.md`), and custom
  `lorebook.json` entries. Respects priority, visibility, and a character
  budget so the DM receives exact relevant lore before writing a turn.
- `add`: adds a custom entry to `<campaign>/lorebook.json`.
- `import-lorebook` / `export-lorebook`: SillyTavern & DiceFrame `lorebook_v3`.
- `import-card` / `export-card`: Character Card v3 (`chara_card_v3`), including
  embedded `character_book` lore entries.

Usage:
    python3 lorebook.py -c <campaign> scan --text "I ask Brother Aldon about the mine" [--budget 1500]
    python3 lorebook.py -c <campaign> add --name "Lantern Bridge" --keys "bridge,lantern,midstone" --content "..." [--dm-only]
    python3 lorebook.py -c <campaign> import-lorebook <file.json> [--dry-run]
    python3 lorebook.py -c <campaign> export-lorebook <out.json>
    python3 lorebook.py -c <campaign> import-card <card.json> [--import-book]
    python3 lorebook.py -c <campaign> export-card --char <name> <out.json>
"""

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign


def _lorebook_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / "lorebook.json"


def _load_lorebook(campaign: str) -> dict:
    lp = _lorebook_path(campaign)
    if lp.exists():
        try:
            data = json.loads(lp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except Exception:
            pass
    return {"spec": "lorebook_v3", "campaign": campaign, "entries": []}


def _save_lorebook(campaign: str, data: dict) -> None:
    _lorebook_path(campaign).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _split_markdown_sections(filepath: pathlib.Path, source_label: str, base_priority: int = 80) -> list[dict]:
    """Parse ## / ### sections from a markdown file into scannable lore entries."""
    if not filepath.exists():
        return []
    text = filepath.read_text(encoding="utf-8", errors="replace")
    entries = []
    current_title = ""
    current_lines: list[str] = []

    def _flush():
        nonlocal current_title, current_lines
        body = "\n".join(current_lines).strip()
        if current_title and body:
            # Extract clean name from heading (strip markdown & parentheticals)
            clean_name = re.sub(r"[*_`]", "", current_title).strip()
            short_name = re.split(r"\s*[—–\-:(]", clean_name)[0].strip()
            keys = [k for k in {clean_name, short_name} if len(k) >= 2]
            # Also add significant words (>3 chars or non-ASCII words >=2 chars)
            for tok in re.findall(r"[^\s,.;:!?()\[\]\"'—–\-]+", short_name):
                if len(tok) >= 4 or (any(ord(ch) > 127 for ch in tok) and len(tok) >= 2):
                    if tok not in keys:
                        keys.append(tok)
            entries.append({
                "name": clean_name,
                "keys": keys,
                "content": body,
                "priority": base_priority,
                "source": source_label,
                "visibility": "dm_only" if "npcs-full" in source_label else "public",
            })
        current_title = ""
        current_lines = []

    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s+(.+)$", line)
        if m:
            _flush()
            current_title = m.group(1).strip()
        else:
            current_lines.append(line)
    _flush()
    return entries


def _safe_regex_match(pattern: str, text: str) -> bool:
    """Safely evaluate a regex pattern; returns False on unsupported/invalid syntax."""
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return False


def _entry_matches(entry: dict, text_lower: str, raw_text: str) -> bool:
    if not entry.get("enabled", True):
        return False
    if entry.get("constant"):
        return True
    keys = entry.get("keys") or []
    for k in keys:
        ks = str(k).strip()
        if not ks:
            continue
        if ks.startswith("/") and ks.rfind("/") > 0:
            # /regex/flags notation
            end = ks.rfind("/")
            pat = ks[1:end]
            if _safe_regex_match(pat, raw_text):
                return True
        else:
            if ks.lower() in text_lower:
                return True
    return False


def scan_lore(
    campaign: str,
    text: str,
    budget: int = 1600,
    include_hidden: bool = True,
) -> list[dict]:
    """Scan all campaign lore sources against `text` and return matched entries within `budget`."""
    cdir = find_campaign(campaign)
    if not cdir.exists() or not text.strip():
        return []

    candidates: list[dict] = []

    # 1. Explicit lorebook.json entries (highest priority default 100)
    lb = _load_lorebook(campaign)
    for e in lb.get("entries", []):
        entry = dict(e)
        entry.setdefault("priority", 100)
        entry.setdefault("source", "lorebook.json")
        entry.setdefault("visibility", "public")
        candidates.append(entry)

    # 2. Campaign markdown files (npcs-full > npcs > world-nodes > world)
    candidates.extend(_split_markdown_sections(cdir / "npcs-full.md", "npcs-full.md", base_priority=95))
    candidates.extend(_split_markdown_sections(cdir / "npcs.md", "npcs.md", base_priority=75))
    candidates.extend(_split_markdown_sections(cdir / "world-nodes.md", "world-nodes.md", base_priority=85))
    candidates.extend(_split_markdown_sections(cdir / "world.md", "world.md", base_priority=70))

    text_lower = text.lower()
    matched = []
    seen_names: set[str] = set()
    for e in candidates:
        if not include_hidden and e.get("visibility") == "dm_only":
            continue
        if _entry_matches(e, text_lower, text):
            key = (e.get("name", "").lower(), e.get("source", ""))
            if key not in seen_names:
                seen_names.add(key)
                matched.append(e)

    # Sort by priority descending, then shorter/tighter entries first
    matched.sort(key=lambda x: (-int(x.get("priority", 50)), len(x.get("content", ""))))

    # Enforce character budget
    selected = []
    used = 0
    for e in matched:
        content = e.get("content", "").strip()
        if used + len(content) > budget:
            remaining = budget - used
            if remaining >= 120:
                clipped = dict(e)
                clipped["content"] = content[: remaining - 3].rstrip() + "..."
                clipped["truncated"] = True
                selected.append(clipped)
                used = budget
            break
        selected.append(e)
        used += len(content)

    return selected


def format_scan_block(matches: list[dict]) -> str:
    if not matches:
        return ""
    lines = ["[[Auto-Lorebook Context (DM-only reference — do not quote verbatim to players):"]
    for m in matches:
        vis = " [DM-ONLY]" if m.get("visibility") == "dm_only" else ""
        lines.append(f"• {m['name']} ({m.get('source', 'lore')}{vis}):")
        lines.append(m["content"].strip())
    lines.append("]]")
    return "\n".join(lines)


def import_lorebook_file(campaign: str, filepath: str, dry_run: bool = False) -> dict:
    """Import SillyTavern or DiceFrame lorebook_v3 JSON into <campaign>/lorebook.json."""
    raw = json.loads(pathlib.Path(filepath).read_text(encoding="utf-8"))
    raw_entries = []
    if isinstance(raw, dict):
        if isinstance(raw.get("entries"), list):
            raw_entries = raw["entries"]
        elif isinstance(raw.get("entries"), dict):
            raw_entries = list(raw["entries"].values())
        elif isinstance(raw.get("data", {}).get("entries"), list):
            raw_entries = raw["data"]["entries"]
    elif isinstance(raw, list):
        raw_entries = raw

    lb = _load_lorebook(campaign)
    existing_names = {e.get("name", "").lower() for e in lb["entries"]}
    imported = 0
    warnings = []

    for idx, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("comment") or f"Entry {idx + 1}").strip()
        keys = item.get("keys") or item.get("key") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        content = (item.get("content") or "").strip()
        if not content:
            continue

        # Validate regex keys safely (DiceFrame rule: preserve verbatim, warn on unmappable)
        for k in keys:
            if isinstance(k, str) and k.startswith("/") and k.rfind("/") > 0:
                pat = k[1 : k.rfind("/")]
                try:
                    re.compile(pat)
                except re.error as err:
                    warnings.append(f"Entry '{name}': unsupported regex {k!r} ({err}) — preserved verbatim")

        entry = {
            "name": name,
            "keys": list(keys),
            "content": content,
            "priority": int(item.get("priority", item.get("insertion_order", 100))),
            "enabled": bool(item.get("enabled", not item.get("disable", False))),
            "constant": bool(item.get("constant", False)),
            "visibility": item.get("visibility", "public"),
            "extensions": item.get("extensions", {}),
        }
        if name.lower() not in existing_names:
            lb["entries"].append(entry)
            existing_names.add(name.lower())
            imported += 1

    if not dry_run:
        _save_lorebook(campaign, lb)
    return {"imported": imported, "total": len(lb["entries"]), "warnings": warnings, "dry_run": dry_run}


def export_lorebook_file(campaign: str, out_path: str) -> dict:
    """Export campaign lorebook + NPCs + World as native lorebook_v3 JSON."""
    cdir = find_campaign(campaign)
    lb = _load_lorebook(campaign)
    all_entries = list(lb.get("entries", []))
    seen = {e.get("name", "").lower() for e in all_entries}
    for fname, prio in (("npcs-full.md", 95), ("world.md", 70)):
        for e in _split_markdown_sections(cdir / fname, fname, base_priority=prio):
            if e["name"].lower() not in seen:
                all_entries.append(e)
                seen.add(e["name"].lower())

    payload = {
        "spec": "lorebook_v3",
        "spec_version": "3.0",
        "data": {
            "name": f"{campaign} Lorebook",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "entries": all_entries,
        },
    }
    pathlib.Path(out_path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {"exported": len(all_entries), "path": out_path}


def import_chara_card(campaign: str, card_path: str, import_book: bool = True) -> dict:
    """Import a chara_card_v3 JSON into <campaign>/characters/<name>.md (+ optional embedded lorebook)."""
    raw = json.loads(pathlib.Path(card_path).read_text(encoding="utf-8"))
    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    name = (data.get("name") or "Unnamed Hero").strip()
    desc = (data.get("description") or "").strip()
    personality = (data.get("personality") or "").strip()
    scenario = (data.get("scenario") or "").strip()
    first_mes = (data.get("first_mes") or "").strip()

    slug = re.sub(r"[^\w\-]+", "-", name.lower()).strip("-") or "character"
    cdir = find_campaign(campaign) / "characters"
    cdir.mkdir(parents=True, exist_ok=True)
    out_md = cdir / f"{slug}.md"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sheet = f"""# {name}
**Player:** Imported  **Campaign:** {campaign}  **Last Updated:** {today}

## Identity
- **Race:** Human | **Class:** Adventurer | **Level:** 1 | **Background:** Imported Card
- **Alignment:** Neutral | **XP:** 0 / 300

## Character Pillar
- **Player's sentence:** *"{personality or desc[:140]}"*
- **Derived pillar:** Imported from chara_card_v3

## Ability Scores
| STR | DEX | CON | INT | WIS | CHA |
|-----|-----|-----|-----|-----|-----|
| 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) | 10 (+0) |

## Combat Stats
- **HP:** 10 / 10 | **Temp HP:** 0
- **AC:** 10 | **Initiative:** +0 | **Speed:** 30 ft
- **Hit Dice:** 1d8 (remaining: 1)
- **Death Saves:** Successes: 0 | Failures: 0

## Equipment & Inventory
**Adventuring Gear:**
- Adventurer's Pack

**Currency:** 10gp 0sp 0cp

## Backstory & Notes
{desc}

{('### Personality\n' + personality) if personality else ''}
{('### Scenario\n' + scenario) if scenario else ''}
{('### Greeting\n' + first_mes) if first_mes else ''}
""".strip() + "\n"
    out_md.write_text(sheet, encoding="utf-8")

    book_imported = 0
    if import_book and isinstance(data.get("character_book"), dict):
        tmp_book = data["character_book"]
        lb = _load_lorebook(campaign)
        existing = {e.get("name", "").lower() for e in lb["entries"]}
        for item in tmp_book.get("entries", []):
            if not isinstance(item, dict) or not item.get("content"):
                continue
            ename = (item.get("name") or item.get("comment") or f"{name} lore").strip()
            if ename.lower() not in existing:
                lb["entries"].append({
                    "name": ename,
                    "keys": list(item.get("keys") or item.get("key") or [name]),
                    "content": str(item["content"]).strip(),
                    "priority": int(item.get("priority", 90)),
                    "enabled": True,
                    "visibility": "public",
                })
                existing.add(ename.lower())
                book_imported += 1
        if book_imported:
            _save_lorebook(campaign, lb)

    return {"name": name, "sheet": str(out_md), "book_entries": book_imported}


def export_chara_card(campaign: str, char_name: str, out_path: str) -> dict:
    """Export a campaign character sheet as chara_card_v3 JSON."""
    cdir = find_campaign(campaign) / "characters"
    target = char_name.strip().lower()
    found = None
    for p in sorted(cdir.glob("*.md")):
        if p.stem.lower() == target:
            found = p
            break
        first = p.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first.lstrip("#").strip().lower() == target:
            found = p
            break
    if not found:
        raise FileNotFoundError(f"Character '{char_name}' not found in {cdir}")

    text = found.read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("#").strip() if text.splitlines() else found.stem
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": title,
            "description": text,
            "personality": "",
            "scenario": f"Campaign: {campaign}",
            "tags": ["dnd5e", campaign],
            "extensions": {"dnd_skill": {"campaign": campaign, "source_file": found.name}},
        },
    }
    pathlib.Path(out_path).write_text(
        json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {"name": title, "path": out_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Lorebook scanner & lorebook_v3 / chara_card_v3 tool.")
    parser.add_argument("-c", "--campaign", required=True, help="Campaign name")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan lore entries matching input text")
    p_scan.add_argument("--text", required=True, help="Player action / scene text")
    p_scan.add_argument("--budget", type=int, default=1600, help="Max character budget")
    p_scan.add_argument("--public-only", action="store_true", help="Exclude DM-only entries")
    p_scan.add_argument("--json", action="store_true", help="Output raw JSON")

    p_add = sub.add_parser("add", help="Add an entry to lorebook.json")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--keys", required=True, help="Comma-separated trigger keywords")
    p_add.add_argument("--content", required=True)
    p_add.add_argument("--priority", type=int, default=100)
    p_add.add_argument("--dm-only", action="store_true")

    p_imp = sub.add_parser("import-lorebook", help="Import lorebook_v3 / SillyTavern JSON")
    p_imp.add_argument("file")
    p_imp.add_argument("--dry-run", action="store_true")

    p_exp = sub.add_parser("export-lorebook", help="Export lorebook_v3 JSON")
    p_exp.add_argument("out")

    p_ic = sub.add_parser("import-card", help="Import chara_card_v3 JSON")
    p_ic.add_argument("file")
    p_ic.add_argument("--no-book", action="store_true")

    p_ec = sub.add_parser("export-card", help="Export chara_card_v3 JSON")
    p_ec.add_argument("--char", required=True)
    p_ec.add_argument("out")

    args = parser.parse_args()
    try:
        if args.cmd == "scan":
            matches = scan_lore(
                args.campaign,
                args.text,
                budget=args.budget,
                include_hidden=not args.public_only,
            )
            if args.json:
                print(json.dumps(matches, indent=2, ensure_ascii=False))
            else:
                out = format_scan_block(matches)
                if out:
                    print(out)
        elif args.cmd == "add":
            lb = _load_lorebook(args.campaign)
            lb["entries"].append({
                "name": args.name.strip(),
                "keys": [k.strip() for k in args.keys.split(",") if k.strip()],
                "content": args.content.strip(),
                "priority": args.priority,
                "enabled": True,
                "visibility": "dm_only" if args.dm_only else "public",
            })
            _save_lorebook(args.campaign, lb)
            print(f"Added lorebook entry '{args.name}' to {args.campaign}/lorebook.json")
        elif args.cmd == "import-lorebook":
            res = import_lorebook_file(args.campaign, args.file, dry_run=args.dry_run)
            print(f"{'[DRY RUN] ' if res['dry_run'] else ''}Imported {res['imported']} entries (total: {res['total']})")
            for w in res["warnings"]:
                print(f"  WARN: {w}", file=sys.stderr)
        elif args.cmd == "export-lorebook":
            res = export_lorebook_file(args.campaign, args.out)
            print(f"Exported {res['exported']} lore entries to {res['path']}")
        elif args.cmd == "import-card":
            res = import_chara_card(args.campaign, args.file, import_book=not args.no_book)
            print(f"Imported character '{res['name']}' → {res['sheet']} (+{res['book_entries']} lore entries)")
        elif args.cmd == "export-card":
            res = export_chara_card(args.campaign, args.char, args.out)
            print(f"Exported character '{res['name']}' → {res['path']}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
