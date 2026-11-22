#!/usr/bin/env python3
"""
world_map.py — Structured world geography, places, routes & travel calculator.

Inspired by NaviDnD (`docs/world-map.md`): prevents LLM distance drift by storing
authoritative coordinates, biomes, roads, hidden secrets, and danger ratings in
`<campaign>/world-map.json`, with deterministic travel-time & route calculations.

Usage:
    python3 world_map.py -c <camp> add-place --name "Oakhaven" --x 0 --y 0 --biome forest --kind town [--desc "..."] [--secret "..."] [--hidden]
    python3 world_map.py -c <camp> connect --a "Oakhaven" --b "Iron Mine" [--road] [--days 1.5]
    python3 world_map.py -c <camp> set-pos --place "Oakhaven"
    python3 world_map.py -c <camp> near [--radius 60] [--include-hidden]
    python3 world_map.py -c <camp> route --to "Iron Mine" [--from "Oakhaven"] [--mode foot|horse|wagon|ship]
    python3 world_map.py -c <camp> travel --to "Iron Mine" [--mode foot|horse|wagon|ship]
    python3 world_map.py -c <camp> overview [--include-hidden]
"""

import argparse
import heapq
import json
import math
import os
import pathlib
import sys
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign

# Standard D&D 5e daily travel speeds (miles per day, normal pace)
TRAVEL_SPEED_MPD = {
    "foot":  24.0,
    "wagon": 20.0,
    "horse": 36.0,
    "ship":  48.0,
}

# Biome terrain multiplier (1.0 = normal, >1.0 = slower)
BIOME_COST = {
    "plains":    1.0,
    "city":      1.0,
    "town":      1.0,
    "coastal":   1.0,
    "forest":    1.25,
    "hills":     1.25,
    "desert":    1.35,
    "swamp":     1.6,
    "mountain":  1.75,
    "arctic":    1.5,
    "underdark": 1.5,
    "cave":      1.5,
    "crypt":     1.2,
}


def _map_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / "world-map.json"


def _load(campaign: str) -> dict:
    p = _map_path(campaign)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("places", {})
                d.setdefault("edges", [])
                d.setdefault("party_location", None)
                return d
        except Exception:
            pass
    return {"campaign": campaign, "unit": "miles", "party_location": None, "places": {}, "edges": []}


def _save(campaign: str, data: dict) -> None:
    _map_path(campaign).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _slug(name: str) -> str:
    return name.strip().lower()


def _find_place(data: dict, name: str) -> "dict | None":
    key = _slug(name)
    if key in data["places"]:
        return data["places"][key]
    for p in data["places"].values():
        if p["name"].lower() == key or key in p["name"].lower():
            return p
    return None


def _dist_miles(a: dict, b: dict) -> float:
    return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))


def _compass_dir(dx: float, dy: float) -> str:
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "here"
    angle = (math.degrees(math.atan2(dy, dx)) + 360) % 360
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    return dirs[int((angle + 22.5) // 45) % 8]


def add_place(
    campaign: str,
    name: str,
    x: float,
    y: float,
    biome: str = "plains",
    kind: str = "landmark",
    desc: str = "",
    secret: str = "",
    hidden: bool = False,
    danger: int = 1,
) -> dict:
    data = _load(campaign)
    key = _slug(name)
    place = {
        "name": name.strip(),
        "x": float(x),
        "y": float(y),
        "biome": biome.strip().lower(),
        "kind": kind.strip().lower(),
        "desc": desc.strip(),
        "secret": secret.strip(),
        "hidden": bool(hidden),
        "visited": False,
        "danger": max(1, min(5, int(danger))),
    }
    data["places"][key] = place
    if data["party_location"] is None and not hidden:
        data["party_location"] = key
        place["visited"] = True
    _save(campaign, data)
    return place


def connect_places(campaign: str, a_name: str, b_name: str, road: bool = True, miles: "float | None" = None) -> dict:
    data = _load(campaign)
    pa = _find_place(data, a_name)
    pb = _find_place(data, b_name)
    if not pa or not pb:
        raise ValueError(f"Unknown place(s): {a_name!r}, {b_name!r}")
    dist = float(miles) if miles is not None else round(_dist_miles(pa, pb), 1)
    edge = {
        "a": _slug(pa["name"]),
        "b": _slug(pb["name"]),
        "road": bool(road),
        "miles": dist,
    }
    # Replace existing edge between same pair
    data["edges"] = [
        e for e in data["edges"]
        if {e["a"], e["b"]} != {edge["a"], edge["b"]}
    ]
    data["edges"].append(edge)
    _save(campaign, data)
    return edge


def compute_route(campaign: str, to_name: str, from_name: str = "", mode: str = "foot") -> dict:
    data = _load(campaign)
    src_key = _slug(from_name) if from_name else (data.get("party_location") or "")
    pa = _find_place(data, src_key) if src_key else None
    pb = _find_place(data, to_name)
    if not pa:
        raise ValueError("Start location not set. Pass --from or run set-pos first.")
    if not pb:
        raise ValueError(f"Destination '{to_name}' not found in world-map.json.")

    speed = TRAVEL_SPEED_MPD.get(mode, 24.0)
    src_k = _slug(pa["name"])
    dst_k = _slug(pb["name"])

    # Dijkstra over explicit road/trail edges
    adj: dict[str, list[tuple[str, float, bool]]] = {}
    for e in data["edges"]:
        adj.setdefault(e["a"], []).append((e["b"], float(e["miles"]), bool(e.get("road", True))))
        adj.setdefault(e["b"], []).append((e["a"], float(e["miles"]), bool(e.get("road", True))))

    pq: list[tuple[float, str, list[str], float]] = [(0.0, src_k, [src_k], 0.0)]
    best: dict[str, float] = {}
    found = None
    while pq:
        cost_days, cur, path, total_mi = heapq.heappop(pq)
        if cur == dst_k:
            found = (cost_days, path, total_mi)
            break
        if cur in best and best[cur] <= cost_days:
            continue
        best[cur] = cost_days
        for nxt, mi, is_road in adj.get(cur, []):
            biome = data["places"].get(nxt, {}).get("biome", "plains")
            mult = 1.0 if is_road else BIOME_COST.get(biome, 1.2)
            leg_days = (mi * mult) / speed
            heapq.heappush(pq, (cost_days + leg_days, nxt, path + [nxt], total_mi + mi))

    if found:
        days, path_keys, miles = found
        via = [data["places"][k]["name"] for k in path_keys]
        off_road = False
    else:
        # Direct overland travel when no road connects them
        miles = round(_dist_miles(pa, pb), 1)
        mult = max(BIOME_COST.get(pa["biome"], 1.0), BIOME_COST.get(pb["biome"], 1.0))
        days = (miles * mult) / speed
        via = [pa["name"], pb["name"]]
        off_road = True

    days_rounded = round(max(0.1, days), 2)
    danger_max = max(data["places"][_slug(n)].get("danger", 1) for n in via if _slug(n) in data["places"])
    return {
        "from": pa["name"],
        "to": pb["name"],
        "mode": mode,
        "miles": round(miles, 1),
        "days": days_rounded,
        "via": via,
        "off_road": off_road,
        "direction": _compass_dir(pb["x"] - pa["x"], pb["y"] - pa["y"]),
        "biomes": list(dict.fromkeys(data["places"][_slug(n)]["biome"] for n in via if _slug(n) in data["places"])),
        "encounter_checks": max(1, int(math.ceil(days_rounded))) if days_rounded >= 0.5 else 0,
        "max_danger": danger_max,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Structured world geography & travel calculator.")
    parser.add_argument("-c", "--campaign", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add-place", help="Add or update a location on the world map")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--x", type=float, required=True)
    p_add.add_argument("--y", type=float, required=True)
    p_add.add_argument("--biome", default="plains")
    p_add.add_argument("--kind", default="landmark")
    p_add.add_argument("--desc", default="")
    p_add.add_argument("--secret", default="")
    p_add.add_argument("--hidden", action="store_true")
    p_add.add_argument("--danger", type=int, default=1)

    p_conn = sub.add_parser("connect", help="Connect two places with a road or trail")
    p_conn.add_argument("--a", required=True)
    p_conn.add_argument("--b", required=True)
    p_conn.add_argument("--trail", action="store_true", help="Off-road trail (slower biome cost)")
    p_conn.add_argument("--miles", type=float, default=None)

    p_pos = sub.add_parser("set-pos", help="Set current party location")
    p_pos.add_argument("--place", required=True)

    p_near = sub.add_parser("near", help="List nearby places from current party location")
    p_near.add_argument("--radius", type=float, default=100.0)
    p_near.add_argument("--include-hidden", action="store_true")

    p_rt = sub.add_parser("route", help="Calculate route & travel time")
    p_rt.add_argument("--to", required=True)
    p_rt.add_argument("--from", dest="frm", default="")
    p_rt.add_argument("--mode", choices=list(TRAVEL_SPEED_MPD.keys()), default="foot")

    p_tr = sub.add_parser("travel", help="Move party to destination and print travel summary")
    p_tr.add_argument("--to", required=True)
    p_tr.add_argument("--mode", choices=list(TRAVEL_SPEED_MPD.keys()), default="foot")

    p_ov = sub.add_parser("overview", help="Show world map summary")
    p_ov.add_argument("--include-hidden", action="store_true")

    args = parser.parse_args()
    try:
        if args.cmd == "add-place":
            pl = add_place(
                args.campaign, args.name, args.x, args.y,
                biome=args.biome, kind=args.kind, desc=args.desc,
                secret=args.secret, hidden=args.hidden, danger=args.danger,
            )
            tag = " [HIDDEN]" if pl["hidden"] else ""
            print(f"Added place '{pl['name']}' at ({pl['x']}, {pl['y']}) [{pl['biome']}/{pl['kind']}]{tag}")
        elif args.cmd == "connect":
            e = connect_places(args.campaign, args.a, args.b, road=not args.trail, miles=args.miles)
            kind = "road" if e["road"] else "trail"
            print(f"Connected {e['a']} <-> {e['b']} via {kind} ({e['miles']} mi)")
        elif args.cmd == "set-pos":
            data = _load(args.campaign)
            pl = _find_place(data, args.place)
            if not pl:
                raise ValueError(f"Place '{args.place}' not found.")
            data["party_location"] = _slug(pl["name"])
            pl["visited"] = True
            pl["hidden"] = False
            _save(args.campaign, data)
            print(f"Party location set to {pl['name']} ({pl['biome']})")
        elif args.cmd == "near":
            data = _load(args.campaign)
            cur = data["places"].get(data.get("party_location") or "")
            if not cur:
                print("Party location not set.")
                return
            rows = []
            for k, p in data["places"].items():
                if k == _slug(cur["name"]):
                    continue
                if p.get("hidden") and not args.include_hidden:
                    continue
                d = _dist_miles(cur, p)
                if d <= args.radius:
                    rows.append((d, p))
            rows.sort(key=lambda x: x[0])
            print(f"Near {cur['name']} ({cur['biome']}):")
            for d, p in rows:
                d_dir = _compass_dir(p["x"] - cur["x"], p["y"] - cur["y"])
                days = round(d / 24.0, 1)
                hid = " [HIDDEN]" if p.get("hidden") else ""
                print(f"  {p['name']} ({p['biome']}) — {round(d,1)} mi {d_dir} (~{days}d on foot){hid}")
        elif args.cmd == "route":
            r = compute_route(args.campaign, args.to, from_name=args.frm, mode=args.mode)
            off = " [off-road]" if r["off_road"] else ""
            print(f"Route: {' → '.join(r['via'])}{off}")
            print(f"  Distance: {r['miles']} mi ({r['direction']}) · Mode: {r['mode']} · Time: {r['days']} day(s)")
            print(f"  Biomes: {', '.join(r['biomes'])} · Danger: {r['max_danger']}/5 · Encounter checks: {r['encounter_checks']}")
        elif args.cmd == "travel":
            r = compute_route(args.campaign, args.to, mode=args.mode)
            data = _load(args.campaign)
            dst = _find_place(data, args.to)
            data["party_location"] = _slug(dst["name"])
            dst["visited"] = True
            dst["hidden"] = False
            _save(args.campaign, data)
            print(f"Traveled to {dst['name']} ({r['miles']} mi, {r['days']} day(s) by {r['mode']}; {r['encounter_checks']} encounter check(s))")
        elif args.cmd == "overview":
            data = _load(args.campaign)
            cur = data.get("party_location")
            print(f"World Map ({len(data['places'])} places, {len(data['edges'])} routes):")
            for k, p in sorted(data["places"].items()):
                if p.get("hidden") and not args.include_hidden:
                    continue
                here = " ★" if k == cur else ""
                hid = " [HIDDEN]" if p.get("hidden") else ""
                print(f"  {p['name']} ({p['x']}, {p['y']}) [{p['biome']}] danger={p['danger']}/5{here}{hid}")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
