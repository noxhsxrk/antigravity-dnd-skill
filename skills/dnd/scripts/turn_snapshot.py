#!/usr/bin/env python3
"""
turn_snapshot.py — Per-turn atomic snapshots & whole-round rollback (/dnd undo).

Inspired by DiceFrame ADR-0003 (whole-round rollback) and round_safety snapshots.
Snapshots all authoritative campaign files + display runtime state before a turn
mutates anything, enabling clean atomic undo/rewind without leaving partial HP,
gold, inventory, graph, or log deltas behind.

Usage:
    python3 turn_snapshot.py -c <campaign> save [--label "Before tavern fight"]
    python3 turn_snapshot.py -c <campaign> list
    python3 turn_snapshot.py -c <campaign> undo [--to N] [--dry-run] [--no-display]
    python3 turn_snapshot.py -c <campaign> prune [--keep 20]
"""

import argparse
import json
import os
import pathlib
import shutil
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign, runtime_dir, display_dir

MAX_SNAPSHOTS_DEFAULT = 20

# Authoritative campaign-level files to snapshot
CAMPAIGN_FILES = [
    "state.md",
    "world.md",
    "world-nodes.md",
    "world-map.json",
    "npcs.md",
    "npcs-full.md",
    "arc.md",
    "session-log.md",
    "graph.json",
    "tracker.json",
    "lorebook.json",
    "entropy.json",
    "battlemap.json",
    "events.jsonl",
    ".economy_ledger.json",
]

# Optional runtime display files to snapshot alongside campaign state
RUNTIME_FILES = [
    ".stats.json",
    ".log.json",
]


def _snapshots_root(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    sdir = cdir / ".snapshots"
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _list_snapshots(campaign: str) -> list[dict]:
    sdir = _snapshots_root(campaign)
    items = []
    for entry in sorted(sdir.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("turn_"):
            continue
        meta_path = entry / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        try:
            turn_num = int(entry.name.split("_", 1)[1])
        except ValueError:
            continue
        meta.setdefault("turn", turn_num)
        meta.setdefault("dir", str(entry))
        meta.setdefault("label", "")
        meta.setdefault("timestamp", "")
        items.append(meta)
    items.sort(key=lambda x: x["turn"])
    return items


def _next_turn_number(campaign: str) -> int:
    snaps = _list_snapshots(campaign)
    return (snaps[-1]["turn"] + 1) if snaps else 1


def save_snapshot(campaign: str, label: str = "", keep: int = MAX_SNAPSHOTS_DEFAULT) -> dict:
    cdir = find_campaign(campaign)
    if not cdir.exists():
        raise FileNotFoundError(f"Campaign directory not found: {cdir}")

    turn = _next_turn_number(campaign)
    snap_dir = _snapshots_root(campaign) / f"turn_{turn:04d}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    files_saved = []
    for rel in CAMPAIGN_FILES:
        src = cdir / rel
        if src.exists() and src.is_file():
            dst = snap_dir / "campaign" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files_saved.append(rel)

    # Characters directory
    chars_src = cdir / "characters"
    chars_saved = []
    if chars_src.exists() and chars_src.is_dir():
        chars_dst = snap_dir / "campaign" / "characters"
        chars_dst.mkdir(parents=True, exist_ok=True)
        for cf in sorted(chars_src.glob("*.md")):
            shutil.copy2(cf, chars_dst / cf.name)
            chars_saved.append(cf.name)

    # Display runtime state (stats + log) if present
    rdir = runtime_dir()
    rt_saved = []
    for rf in RUNTIME_FILES:
        src = rdir / rf
        if src.exists() and src.is_file():
            dst = snap_dir / "runtime" / rf
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            rt_saved.append(rf)

    meta = {
        "turn": turn,
        "campaign": campaign,
        "label": label.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files": files_saved,
        "characters": chars_saved,
        "runtime_files": rt_saved,
        "dir": str(snap_dir),
    }
    (snap_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if keep > 0:
        prune_snapshots(campaign, keep=keep)

    return meta


def prune_snapshots(campaign: str, keep: int = MAX_SNAPSHOTS_DEFAULT) -> int:
    snaps = _list_snapshots(campaign)
    if len(snaps) <= keep:
        return 0
    to_remove = snaps[: len(snaps) - keep]
    removed = 0
    for s in to_remove:
        p = pathlib.Path(s["dir"])
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed


def _notify_display_rollback(campaign: str, turn: int) -> bool:
    """Tell the running display server to reload stats/log and refresh clients."""
    scheme_file = display_dir() / ".scheme"
    scheme = scheme_file.read_text(encoding="utf-8").strip() if scheme_file.exists() else "http"
    url = f"{scheme}://localhost:5001/rollback"
    token_file = runtime_dir() / ".token"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DND-Token"] = token
    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    payload = json.dumps({"campaign": campaign, "turn": turn}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=4.0, context=ctx)
        return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def undo_snapshot(
    campaign: str,
    to_turn: "int | None" = None,
    dry_run: bool = False,
    notify_display: bool = True,
) -> dict:
    cdir = find_campaign(campaign)
    if not cdir.exists():
        raise FileNotFoundError(f"Campaign directory not found: {cdir}")

    snaps = _list_snapshots(campaign)
    if not snaps:
        raise RuntimeError(f"No snapshots available for campaign '{campaign}'.")

    if to_turn is None:
        target = snaps[-1]
    else:
        matches = [s for s in snaps if s["turn"] == to_turn]
        if not matches:
            raise RuntimeError(
                f"Snapshot turn {to_turn} not found. Available: {[s['turn'] for s in snaps]}"
            )
        target = matches[0]

    snap_dir = pathlib.Path(target["dir"])
    restored_files = []
    removed_files = []

    if not dry_run:
        # 1. Restore campaign files (and remove files that didn't exist at that turn)
        snap_camp = snap_dir / "campaign"
        for rel in CAMPAIGN_FILES:
            src = snap_camp / rel
            dst = cdir / rel
            if src.exists():
                shutil.copy2(src, dst)
                restored_files.append(rel)
            elif dst.exists():
                dst.unlink()
                removed_files.append(rel)

        # 2. Restore character files atomically
        snap_chars = snap_camp / "characters"
        dst_chars = cdir / "characters"
        if snap_chars.exists():
            dst_chars.mkdir(parents=True, exist_ok=True)
            snap_char_names = {p.name for p in snap_chars.glob("*.md")}
            for existing in dst_chars.glob("*.md"):
                if existing.name not in snap_char_names:
                    existing.unlink()
                    removed_files.append(f"characters/{existing.name}")
            for cf in snap_chars.glob("*.md"):
                shutil.copy2(cf, dst_chars / cf.name)
                restored_files.append(f"characters/{cf.name}")

        # 3. Restore display runtime files
        snap_rt = snap_dir / "runtime"
        rdir = runtime_dir()
        if snap_rt.exists():
            for rf in RUNTIME_FILES:
                src = snap_rt / rf
                if src.exists():
                    shutil.copy2(src, rdir / rf)
                    restored_files.append(f"runtime/{rf}")

        # 4. Branch-cut: discard snapshots strictly newer than target turn
        #    (and remove the target snapshot itself if popping the top undo frame)
        for s in snaps:
            if s["turn"] >= target["turn"]:
                shutil.rmtree(s["dir"], ignore_errors=True)

        display_ok = _notify_display_rollback(campaign, target["turn"]) if notify_display else False
    else:
        restored_files = list(target.get("files", [])) + [
            f"characters/{c}" for c in target.get("characters", [])
        ]
        display_ok = False

    return {
        "campaign": campaign,
        "restored_turn": target["turn"],
        "label": target.get("label", ""),
        "timestamp": target.get("timestamp", ""),
        "restored_files": restored_files,
        "removed_files": removed_files,
        "dry_run": dry_run,
        "display_notified": display_ok,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-turn snapshots & whole-round rollback for D&D campaigns.")
    parser.add_argument("-c", "--campaign", required=True, help="Campaign name")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_save = sub.add_parser("save", help="Save a pre-turn snapshot")
    p_save.add_argument("--label", default="", help="Optional turn/scene label")
    p_save.add_argument("--keep", type=int, default=MAX_SNAPSHOTS_DEFAULT, help="Max snapshots to retain")

    sub.add_parser("list", help="List available turn snapshots")

    p_undo = sub.add_parser("undo", help="Roll back campaign state to a snapshot")
    p_undo.add_argument("--to", type=int, default=None, help="Specific turn number (default: latest)")
    p_undo.add_argument("--dry-run", action="store_true", help="Preview what would be restored")
    p_undo.add_argument("--no-display", action="store_true", help="Do not notify display server")

    p_prune = sub.add_parser("prune", help="Prune old snapshots")
    p_prune.add_argument("--keep", type=int, default=MAX_SNAPSHOTS_DEFAULT)

    args = parser.parse_args()

    try:
        if args.cmd == "save":
            meta = save_snapshot(args.campaign, label=args.label, keep=args.keep)
            lbl = f" ({meta['label']})" if meta["label"] else ""
            print(f"Snapshot saved: turn {meta['turn']}{lbl} — {len(meta['files'])} files, {len(meta['characters'])} characters")
        elif args.cmd == "list":
            snaps = _list_snapshots(args.campaign)
            if not snaps:
                print(f"No snapshots found for campaign '{args.campaign}'.")
                return
            print(f"Snapshots for '{args.campaign}' ({len(snaps)}):")
            for s in snaps:
                lbl = f" — {s['label']}" if s.get("label") else ""
                print(f"  Turn {s['turn']:>3d}  {s.get('timestamp', '')[:19]}Z{lbl}")
        elif args.cmd == "undo":
            res = undo_snapshot(
                args.campaign,
                to_turn=args.to,
                dry_run=args.dry_run,
                notify_display=not args.no_display,
            )
            prefix = "[DRY RUN] Would restore" if res["dry_run"] else "Rolled back"
            lbl = f" ({res['label']})" if res["label"] else ""
            print(f"{prefix} '{args.campaign}' to Turn {res['restored_turn']}{lbl}")
            print(f"  Restored ({len(res['restored_files'])}): {', '.join(res['restored_files'])}")
            if res["removed_files"]:
                print(f"  Removed ({len(res['removed_files'])}): {', '.join(res['removed_files'])}")
            if not res["dry_run"]:
                print(f"  Display refresh: {'sent' if res['display_notified'] else 'offline/skipped'}")
        elif args.cmd == "prune":
            n = prune_snapshots(args.campaign, keep=args.keep)
            print(f"Pruned {n} old snapshot(s).")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
