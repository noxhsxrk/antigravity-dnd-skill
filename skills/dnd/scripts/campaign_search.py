#!/usr/bin/env python3
"""
campaign_search.py — keyword search across campaign files.

Usage:
    python3 campaign_search.py -c <campaign> <keyword> [keyword2 ...]
    python3 campaign_search.py -c <campaign> <keyword> --files state,log,archive,world,npcs
    python3 campaign_search.py -c <campaign> <keyword> -C 4   # context lines (default 3)

Examples:
    python3 campaign_search.py -c my-campaign dragon
    python3 campaign_search.py -c my-campaign Vael letter --files log,archive
    python3 campaign_search.py -c test-campaign VARETH Kel

Returns: file name, nearest section heading, and matching lines with context.
Case-insensitive. Multiple keywords = AND (all must appear within the same block).
"""

import sys
import os
import json
import argparse
import re

from paths import campaigns_dir as _campaigns_dir
CAMPAIGNS_DIR = str(_campaigns_dir())

FILE_MAP = {
    "state":   "state.md",
    "log":     "session-log.md",
    "archive": "session-log-archive.md",
    "world":   "world.md",
    "seeds":   "world-seeds.md",
    "npcs":    "npcs.md",
    "npcsfull":"npcs-full.md",
}
DEFAULT_FILES = ["state", "log", "archive", "world", "npcs"]
STALE_TAGS = {"dead", "deceased", "killed", "destroyed", "consumed", "closed", "resolved", "retired"}


def _load_graph_index(campaign_dir: str) -> tuple[list[dict], dict[str, list[str]]]:
    """Load graph.json (if present) and build entity metadata + stale-fact warnings.

    Inspired by DungeonGPT-JS RAG_GRAPH_ENHANCEMENT_PLAN: pairs keyword search with
    the campaign relationship graph to boost entity-tagged hits and flag stale facts
    (e.g. NPCs marked dead/deceased or relationships closed in later sessions).
    """
    gpath = os.path.join(campaign_dir, "graph.json")
    if not os.path.exists(gpath):
        return [], {}
    try:
        with open(gpath, encoding="utf-8") as f:
            g = json.load(f)
    except Exception:
        return [], {}

    nodes = g.get("nodes", {})
    if isinstance(nodes, dict):
        node_list = list(nodes.values())
    elif isinstance(nodes, list):
        node_list = nodes
    else:
        node_list = []

    edges = g.get("edges", [])
    if isinstance(edges, dict):
        edges = list(edges.values())

    stale_warnings: dict[str, list[str]] = {}
    id_to_name: dict[str, str] = {}
    for n in node_list:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", ""))
        name = str(n.get("name", "")).strip()
        if not name:
            continue
        id_to_name[nid] = name
        tags = {str(t).lower() for t in (n.get("tags") or [])}
        status = str(n.get("status", "")).lower()
        summary = str(n.get("summary", "")).lower()
        hit_stale = tags & STALE_TAGS
        if status in STALE_TAGS:
            hit_stale.add(status)
        if hit_stale or any(w in summary for w in ("(dead)", "deceased", "killed in session")):
            label = ", ".join(sorted(hit_stale)) or "deceased/closed"
            stale_warnings.setdefault(name.lower(), []).append(
                f"{name} [{n.get('type', 'entity')}] is marked {label} in graph.json"
            )

    for e in edges:
        if not isinstance(e, dict):
            continue
        if e.get("until") is not None or e.get("closed_at") is not None:
            frm = id_to_name.get(str(e.get("from", "")), str(e.get("from", "")))
            to = id_to_name.get(str(e.get("to", "")), str(e.get("to", "")))
            et = e.get("type", "relation")
            closed = e.get("until") if e.get("until") is not None else e.get("closed_at")
            msg = f"edge {frm} -[{et}]-> {to} was closed at session {closed}"
            if frm:
                stale_warnings.setdefault(frm.lower(), []).append(msg)
            if to:
                stale_warnings.setdefault(to.lower(), []).append(msg)

    return node_list, stale_warnings


def _annotate_result(result: dict, nodes: list[dict], stale_warnings: dict[str, list[str]], entity_filter: str = "") -> dict:
    """Compute entity-tag boost score and attach any stale-fact warnings to a search result."""
    hay = (result["heading"] + "\n" + result["snippet"]).lower()
    tags_found = []
    warnings = []
    for n in nodes:
        name = str(n.get("name", "")).strip()
        if len(name) >= 2 and name.lower() in hay:
            ntype = str(n.get("type", "entity")).lower()
            tags_found.append(f"{ntype}:{name}")
            for w in stale_warnings.get(name.lower(), []):
                if w not in warnings:
                    warnings.append(w)

    score = 10 + len(tags_found) * 5
    if entity_filter:
        ef = entity_filter.lower()
        if any(ef in t.lower() for t in tags_found) or ef in hay:
            score += 25
    result["entity_tags"] = tags_found
    result["stale_warnings"] = warnings
    result["score"] = score
    return result


def find_section_heading(lines, match_idx):
    """Walk backwards from match_idx to find nearest ## or ### heading."""
    for i in range(match_idx, -1, -1):
        if lines[i].startswith("#"):
            return lines[i].strip()
    return "(no heading found)"


def search_file(filepath, keywords, context_lines=3):
    if not os.path.exists(filepath):
        return []

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    results = []
    # Split into blocks of (context_lines*2+1) for AND matching
    # Strategy: find lines matching ALL keywords within a window of 20 lines
    window = 20
    matched_blocks = set()

    for i, line in enumerate(lines):
        if all(kw.lower() in line.lower() for kw in keywords):
            matched_blocks.add(i)
        # Multi-line AND: check if all keywords appear in a window around this line
        elif len(keywords) > 1:
            block_start = max(0, i - window // 2)
            block_end = min(len(lines), i + window // 2)
            block_text = "".join(lines[block_start:block_end]).lower()
            if all(kw.lower() in block_text for kw in keywords):
                # Find the line with the first keyword
                for j in range(block_start, block_end):
                    if keywords[0].lower() in lines[j].lower():
                        matched_blocks.add(j)
                        break

    for idx in sorted(matched_blocks):
        heading = find_section_heading(lines, idx)
        start = max(0, idx - context_lines)
        end = min(len(lines), idx + context_lines + 1)
        snippet = "".join(lines[start:end]).rstrip()
        results.append({
            "file": os.path.basename(filepath),
            "line": idx + 1,
            "heading": heading,
            "snippet": snippet,
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Search campaign files by keyword.")
    parser.add_argument("-c", "--campaign", required=True, help="Campaign name")
    parser.add_argument("keywords", nargs="+", help="Keywords to search (AND logic)")
    parser.add_argument(
        "--files",
        default=",".join(DEFAULT_FILES),
        help=f"Comma-separated file keys to search. Options: {', '.join(FILE_MAP.keys())}. Default: {','.join(DEFAULT_FILES)}",
    )
    parser.add_argument("-C", "--context", type=int, default=3, help="Context lines around match (default 3)")
    parser.add_argument("--entity", default="", help="Boost matches involving this entity tag or name (e.g. npc:Aldon)")
    parser.add_argument("--no-graph", action="store_true", help="Disable graph.json entity boost and stale-fact warnings")
    args = parser.parse_args()

    campaign_dir = os.path.join(CAMPAIGNS_DIR, args.campaign)
    if not os.path.isdir(campaign_dir):
        print(f"ERROR: Campaign directory not found: {campaign_dir}")
        sys.exit(1)

    file_keys = [k.strip() for k in args.files.split(",")]
    keywords = args.keywords
    nodes, stale_warnings = ([], {}) if args.no_graph else _load_graph_index(campaign_dir)

    print(f"Searching campaign: {args.campaign}")
    print(f"Keywords: {' AND '.join(keywords)}")
    print(f"Files: {', '.join(file_keys)}")
    if args.entity:
        print(f"Entity boost: {args.entity}")
    print("=" * 60)

    all_results = []
    for key in file_keys:
        filename = FILE_MAP.get(key)
        if not filename:
            print(f"  [unknown file key: {key}]")
            continue
        filepath = os.path.join(campaign_dir, filename)
        for r in search_file(filepath, keywords, args.context):
            all_results.append(_annotate_result(r, nodes, stale_warnings, entity_filter=args.entity))

    # Rank entity-boosted hits higher while preserving file/line order on ties
    all_results.sort(key=lambda r: (-r["score"], r["file"], r["line"]))

    for r in all_results:
        tags_str = f"  tags=[{', '.join(r['entity_tags'])}]" if r["entity_tags"] else ""
        print(f"\n[{r['file']}  line {r['line']}]  {r['heading']}{tags_str}")
        for w in r["stale_warnings"]:
            print(f"  ⚠ STALE FACT: {w}")
        print("-" * 40)
        print(r["snippet"])

    total = len(all_results)
    print("\n" + "=" * 60)
    if total == 0:
        print(f"No matches found for: {' AND '.join(keywords)}")
    else:
        print(f"{total} match(es) found.")


if __name__ == "__main__":
    main()
