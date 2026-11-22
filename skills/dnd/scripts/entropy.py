#!/usr/bin/env python3
"""
entropy.py — Dungeon/World Entropy (Tension & Noise) Meter.

Inspired by Daicer (`src/engine/entropy/`): tracks accumulated noise, heat, and
environmental tension (0–100) during dungeon crawls, heists, or stealth scenes.
Crossing thresholds (25 Uneasy, 50 Alert, 75 Hunted, 90 Crisis) triggers concrete
DM advisories and pushes a live tension meter update to the Display Companion.

Usage:
    python3 entropy.py -c <camp> add --delta 15 --reason "Kicked down iron door"
    python3 entropy.py -c <camp> add --delta -10 --reason "Hid in silent alcove"
    python3 entropy.py -c <camp> status
    python3 entropy.py -c <camp> reset [--value 0]
"""

import argparse
import json
import os
import pathlib
import ssl
import sys
import urllib.request
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from paths import find_campaign, display_dir, runtime_dir

THRESHOLDS = [
    (90, "CRISIS",  "Immediate escalation — reinforcements, boss arrival, or environmental collapse!"),
    (75, "HUNTED",  "Active pursuit — patrols converge on the party's last known position."),
    (50, "ALERT",   "Guards are on edge — Perception checks have advantage, torches lit, doors barred."),
    (25, "UNEASY",  "Faint signs of disturbance — distant footsteps, raised voices, shifting shadows."),
    (0,  "CALM",    "The area is quiet and unaware of the party."),
]


def _entropy_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / "entropy.json"


def _tier(val: int) -> tuple[str, str]:
    for floor, label, advice in THRESHOLDS:
        if val >= floor:
            return label, advice
    return "CALM", THRESHOLDS[-1][2]


def _load(campaign: str) -> dict:
    p = _entropy_path(campaign)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("value", 0)
                d.setdefault("history", [])
                return d
        except Exception:
            pass
    return {"campaign": campaign, "value": 0, "tier": "CALM", "history": []}


def _save(campaign: str, data: dict) -> None:
    _entropy_path(campaign).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _push_display(value: int, tier: str, reason: str = "") -> bool:
    scheme_file = display_dir() / ".scheme"
    scheme = scheme_file.read_text(encoding="utf-8").strip() if scheme_file.exists() else "http"
    url = f"{scheme}://localhost:5001/entropy"
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
    body = json.dumps({"value": value, "tier": tier, "reason": reason}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=3.0, context=ctx)
        return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def adjust_entropy(campaign: str, delta: int, reason: str = "", push: bool = True) -> dict:
    data = _load(campaign)
    before = int(data.get("value", 0))
    after = max(0, min(100, before + int(delta)))
    old_tier, _ = _tier(before)
    new_tier, advice = _tier(after)
    crossed = new_tier != old_tier and after > before

    data["value"] = after
    data["tier"] = new_tier
    data["history"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "before": before,
        "delta": int(delta),
        "after": after,
        "tier": new_tier,
        "reason": reason.strip(),
    })
    data["history"] = data["history"][-50:]
    _save(campaign, data)
    if push:
        _push_display(after, new_tier, reason.strip())

    return {
        "before": before,
        "after": after,
        "delta": int(delta),
        "tier": new_tier,
        "crossed": crossed,
        "advice": advice,
        "reason": reason.strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Dungeon/World Entropy (Tension & Noise) meter.")
    parser.add_argument("-c", "--campaign", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Adjust entropy by +/- delta")
    p_add.add_argument("--delta", type=int, required=True)
    p_add.add_argument("--reason", default="")
    p_add.add_argument("--no-display", action="store_true")

    sub.add_parser("status", help="Show current entropy level and recent events")

    p_rst = sub.add_parser("reset", help="Reset entropy to 0 (or specified value)")
    p_rst.add_argument("--value", type=int, default=0)
    p_rst.add_argument("--no-display", action="store_true")

    args = parser.parse_args()
    try:
        if args.cmd == "add":
            res = adjust_entropy(args.campaign, args.delta, args.reason, push=not args.no_display)
            sign = "+" if res["delta"] >= 0 else ""
            print(f"Entropy: {res['before']} → {res['after']}/100 [{res['tier']}] ({sign}{res['delta']})"
                  + (f" — {res['reason']}" if res["reason"] else ""))
            if res["crossed"]:
                print(f"  ⚡ THRESHOLD CROSSED ({res['tier']}): {res['advice']}")
        elif args.cmd == "status":
            data = _load(args.campaign)
            val = int(data.get("value", 0))
            tier, advice = _tier(val)
            print(f"Entropy: {val}/100 [{tier}] — {advice}")
            for h in data.get("history", [])[-5:]:
                print(f"  {h['before']}→{h['after']} ({h['delta']:+d}) {h.get('reason', '')}")
        elif args.cmd == "reset":
            data = _load(args.campaign)
            val = max(0, min(100, args.value))
            tier, _ = _tier(val)
            data["value"] = val
            data["tier"] = tier
            data["history"] = []
            _save(args.campaign, data)
            if not args.no_display:
                _push_display(val, tier, "Reset")
            print(f"Entropy reset to {val}/100 [{tier}]")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
