#!/usr/bin/env python3
"""
battlemap.py — On-Demand 2D Tactical Battlemap & Surface Movement Calculator.

Inspired by NaviDnD (`docs/architecture.md`, `docs/enemy-tactics.md`):
- Only activated when tactical combat or dungeon positioning is needed (text-only
  roleplay stays clean and uncluttered).
- Implements NaviDnD surface & diagonal movement rules:
  * Normal tile: 5 ft (first diagonal 5 ft, subsequent diagonals 10 ft)
  * Difficult terrain / bush / shallow water: 10 ft (subsequent diagonals 15 ft)
  * Wall / deep water / obstacle: impassable
- Pushes live map JSON to `POST /battlemap` on the Display Companion so phones
  and the TV screen render an interactive SVG tactical grid on demand.

Usage:
    python3 battlemap.py -c <camp> init --width 12 --height 10 --title "Forest Ambush"
    python3 battlemap.py -c <camp> terrain --kind wall --cells 3,2 3,3 3,4
    python3 battlemap.py -c <camp> terrain --kind difficult --cells 5,5 6,5
    python3 battlemap.py -c <camp> place --name "เนียม" --x 1 --y 5 --kind pc --hp 20/20
    python3 battlemap.py -c <camp> place --name "Goblin Shaman" --x 9 --y 5 --kind enemy --hp 18/18
    python3 battlemap.py -c <camp> move --name "เนียม" --x 4 --y 5 [--speed 30]
    python3 battlemap.py -c <camp> distance --a "เนียม" --b "Goblin Shaman"
    python3 battlemap.py -c <camp> clear
"""

import argparse
import heapq
import json
import math
import os
import pathlib
import ssl
import sys
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign, display_dir, runtime_dir

TERRAIN_KINDS = ("normal", "difficult", "wall", "cover", "water", "hazard")
IMPASSABLE = {"wall"}


def _map_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / "battlemap.json"


def _load(campaign: str) -> dict:
    p = _map_path(campaign)
    if not p.exists():
        raise FileNotFoundError(f"No active battlemap for '{campaign}'. Run `battlemap.py -c {campaign} init` first.")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(campaign: str, data: dict) -> None:
    _map_path(campaign).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _push_display(data: "dict | None") -> bool:
    scheme_file = display_dir() / ".scheme"
    scheme = scheme_file.read_text(encoding="utf-8").strip() if scheme_file.exists() else "http"
    url = f"{scheme}://localhost:5001/battlemap"
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
    body = json.dumps({"battlemap": data}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=3.0, context=ctx)
        return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _step_cost(terrain_kind: str, is_diag: bool, diag_used: int) -> int:
    """NaviDnD movement rule: normal=5ft, difficult/water=10ft; subsequent diagonals +5ft."""
    base = 10 if terrain_kind in ("difficult", "water", "hazard") else 5
    extra = 5 if (is_diag and diag_used >= 1) else 0
    return base + extra


def find_path(data: dict, sx: int, sy: int, tx: int, ty: int) -> tuple[int, list[tuple[int, int]]]:
    """Dijkstra shortest path on the 2D grid respecting walls, difficult terrain, and diagonals."""
    w, h = int(data["width"]), int(data["height"])
    terrain = data.get("terrain", {})
    if not (0 <= tx < w and 0 <= ty < h):
        raise ValueError(f"Target ({tx},{ty}) is outside {w}x{h} grid.")
    if terrain.get(f"{tx},{ty}") in IMPASSABLE:
        raise ValueError(f"Target ({tx},{ty}) is impassable ({terrain.get(f'{tx},{ty}')}).")

    # State: (cost_ft, diag_used, x, y, path)
    pq: list[tuple[int, int, int, int, list[tuple[int, int]]]] = [(0, 0, sx, sy, [(sx, sy)])]
    best: dict[tuple[int, int, int], int] = {}
    while pq:
        cost, d_used, x, y, path = heapq.heappop(pq)
        if (x, y) == (tx, ty):
            return cost, path
        state_key = (x, y, min(d_used, 1))
        if state_key in best and best[state_key] <= cost:
            continue
        best[state_key] = cost
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                tk = terrain.get(f"{nx},{ny}", "normal")
                if tk in IMPASSABLE:
                    continue
                is_diag = (dx != 0 and dy != 0)
                sc = _step_cost(tk, is_diag, d_used)
                nd = d_used + (1 if is_diag else 0)
                heapq.heappush(pq, (cost + sc, nd, nx, ny, path + [(nx, ny)]))
    raise ValueError(f"No reachable path from ({sx},{sy}) to ({tx},{ty}).")


def _find_token(data: dict, name: str) -> "dict | None":
    target = name.strip().lower()
    for t in data.get("tokens", []):
        if t["name"].lower() == target:
            return t
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="On-demand 2D tactical battlemap.")
    parser.add_argument("-c", "--campaign", required=True)
    parser.add_argument("--no-display", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create/reset an active tactical grid")
    p_init.add_argument("--width", type=int, default=12)
    p_init.add_argument("--height", type=int, default=10)
    p_init.add_argument("--title", default="Tactical Encounter")

    p_ter = sub.add_parser("terrain", help="Paint terrain cells (x,y)")
    p_ter.add_argument("--kind", choices=TERRAIN_KINDS, required=True)
    p_ter.add_argument("--cells", nargs="+", required=True, help="List of x,y coords e.g. 3,2 3,3")

    p_pl = sub.add_parser("place", help="Place or update a token on the grid")
    p_pl.add_argument("--name", required=True)
    p_pl.add_argument("--x", type=int, required=True)
    p_pl.add_argument("--y", type=int, required=True)
    p_pl.add_argument("--kind", choices=["pc", "enemy", "npc", "obj"], default="enemy")
    p_pl.add_argument("--hp", default="", help="Optional cur/max e.g. 18/18")

    p_mv = sub.add_parser("move", help="Move a token with surface/diagonal movement cost")
    p_mv.add_argument("--name", required=True)
    p_mv.add_argument("--x", type=int, required=True)
    p_mv.add_argument("--y", type=int, required=True)
    p_mv.add_argument("--speed", type=int, default=0, help="Optional max speed in ft to enforce")

    p_rm = sub.add_parser("remove", help="Remove a token from the grid")
    p_rm.add_argument("--name", required=True)

    p_dist = sub.add_parser("distance", help="Measure 5e distance (ft) between two tokens")
    p_dist.add_argument("--a", required=True)
    p_dist.add_argument("--b", required=True)

    sub.add_parser("clear", help="Close/clear the active tactical battlemap")

    args = parser.parse_args()
    push = not args.no_display

    try:
        if args.cmd == "init":
            data = {
                "active": True,
                "campaign": args.campaign,
                "title": args.title.strip(),
                "width": max(4, min(30, args.width)),
                "height": max(4, min(30, args.height)),
                "terrain": {},
                "tokens": [],
            }
            _save(args.campaign, data)
            if push:
                _push_display(data)
            print(f"Battlemap '{data['title']}' initialized ({data['width']}×{data['height']} grid, 5ft squares)")
        elif args.cmd == "terrain":
            data = _load(args.campaign)
            for c in args.cells:
                x_s, y_s = c.split(",")
                key = f"{int(x_s)},{int(y_s)}"
                if args.kind == "normal":
                    data["terrain"].pop(key, None)
                else:
                    data["terrain"][key] = args.kind
            _save(args.campaign, data)
            if push:
                _push_display(data)
            print(f"Painted {len(args.cells)} cell(s) as {args.kind}")
        elif args.cmd == "place":
            data = _load(args.campaign)
            tok = _find_token(data, args.name)
            if tok:
                tok["x"], tok["y"], tok["kind"] = args.x, args.y, args.kind
                if args.hp:
                    tok["hp"] = args.hp
            else:
                tok = {"name": args.name.strip(), "x": args.x, "y": args.y, "kind": args.kind}
                if args.hp:
                    tok["hp"] = args.hp
                data["tokens"].append(tok)
            _save(args.campaign, data)
            if push:
                _push_display(data)
            print(f"Placed {tok['name']} ({tok['kind']}) at ({tok['x']},{tok['y']})")
        elif args.cmd == "move":
            data = _load(args.campaign)
            tok = _find_token(data, args.name)
            if not tok:
                raise ValueError(f"Token '{args.name}' not on battlemap.")
            cost_ft, path = find_path(data, int(tok["x"]), int(tok["y"]), args.x, args.y)
            if args.speed > 0 and cost_ft > args.speed:
                raise ValueError(f"Insufficient movement: path costs {cost_ft} ft, speed is {args.speed} ft.")
            old_x, old_y = tok["x"], tok["y"]
            tok["x"], tok["y"] = args.x, args.y
            _save(args.campaign, data)
            if push:
                _push_display(data)
            rem = f" ({args.speed - cost_ft} ft remaining)" if args.speed > 0 else ""
            print(f"Moved {tok['name']} ({old_x},{old_y}) → ({args.x},{args.y}) — {cost_ft} ft{rem}")
        elif args.cmd == "remove":
            data = _load(args.campaign)
            data["tokens"] = [t for t in data["tokens"] if t["name"].lower() != args.name.strip().lower()]
            _save(args.campaign, data)
            if push:
                _push_display(data)
            print(f"Removed {args.name} from battlemap")
        elif args.cmd == "distance":
            data = _load(args.campaign)
            ta = _find_token(data, args.a)
            tb = _find_token(data, args.b)
            if not ta or not tb:
                raise ValueError(f"Token(s) not found: {args.a}, {args.b}")
            dx, dy = abs(int(ta["x"]) - int(tb["x"])), abs(int(ta["y"]) - int(tb["y"]))
            cheb_ft = max(dx, dy) * 5
            euclid_ft = round(math.hypot(dx, dy) * 5, 1)
            print(f"Distance {ta['name']} ↔ {tb['name']}: {cheb_ft} ft (grid) / {euclid_ft} ft (exact)")
        elif args.cmd == "clear":
            p = _map_path(args.campaign)
            if p.exists():
                p.unlink()
            if push:
                _push_display(None)
            print(f"Cleared tactical battlemap for '{args.campaign}'")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
