#!/usr/bin/env python3
"""
economy.py — Deterministic shop, currency, inventory & rest recovery engine.

Inspired by DungeonGPT-JS ("engine referees, LLM narrates") and DiceFrame
ADR-0002 (payer-confirmed offers) + ADR-0012 (authoritative resource recovery).

All gold deductions, purchases, sales, and Short/Long Rest recoveries execute
atomically on `characters/<name>.md` and record an auditable receipt in
`<campaign>/.economy_ledger.json` (which is also covered by turn_snapshot.py).

Usage:
    # Currency adjustment (refuses negative balance)
    python3 economy.py -c <campaign> gold --char <name> --delta -15gp --reason "Bribe guard"

    # Buy / Sell item (atomic gold + inventory update)
    python3 economy.py -c <campaign> buy  --char <name> --item "Healing Potion" --cost 50gp
    python3 economy.py -c <campaign> sell --char <name> --item "Rusty Dagger" --value 1gp

    # Deterministic 5e Rest (short or long)
    python3 economy.py -c <campaign> rest --type long --char all
    python3 economy.py -c <campaign> rest --type short --char <name> --hit-dice 2 --heal 11

    # Inspect ledger
    python3 economy.py -c <campaign> ledger [--limit 10]
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

# Standard D&D 5e coin conversion to copper pieces (cp)
COIN_TO_CP = {"pp": 1000, "gp": 100, "ep": 50, "sp": 10, "cp": 1}


def _find_char_file(campaign: str, char_name: str) -> pathlib.Path:
    cdir = find_campaign(campaign) / "characters"
    if not cdir.exists():
        raise FileNotFoundError(f"Characters directory not found: {cdir}")
    # Exact filename match first
    for candidate in (
        cdir / f"{char_name}.md",
        cdir / f"{char_name.lower()}.md",
        cdir / f"{char_name.lower().replace(' ', '-')}.md",
    ):
        if candidate.exists():
            return candidate
    # Case-insensitive stem or H1 heading match
    target = char_name.strip().lower()
    for p in sorted(cdir.glob("*.md")):
        if p.stem.lower() == target:
            return p
        try:
            first_line = p.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if first_line.lstrip("#").strip().lower() == target:
                return p
        except Exception:
            pass
    raise FileNotFoundError(f"Character sheet not found for '{char_name}' in {cdir}")


def parse_coin_amount(spec: str) -> int:
    """Parse a coin string like '50gp', '-15gp', '2gp 5sp', '100' (defaults to gp) into cp."""
    s = spec.strip().lower()
    if not s:
        raise ValueError("Empty coin amount")
    sign = -1 if s.startswith("-") else 1
    if s[0] in "+-":
        s = s[1:].strip()
    tokens = re.findall(r"(\d+)\s*(pp|gp|ep|sp|cp)?", s)
    if not tokens:
        raise ValueError(f"Invalid coin amount: {spec!r}")
    total_cp = 0
    for num_s, unit in tokens:
        if not num_s:
            continue
        u = unit or "gp"
        total_cp += int(num_s) * COIN_TO_CP[u]
    return sign * total_cp


def format_cp(total_cp: int) -> str:
    """Format copper pieces into canonical 'Xgp Ysp Zcp' string."""
    sign = "-" if total_cp < 0 else ""
    rem = abs(total_cp)
    gp, rem = divmod(rem, 100)
    sp, cp = divmod(rem, 10)
    return f"{sign}{gp}gp {sp}sp {cp}cp"


def _read_currency_cp(text: str) -> tuple[int, "re.Match | None"]:
    m = re.search(r"(\*\*Currency:\*\*\s*)(.+)", text)
    if not m:
        return 0, None
    raw = m.group(2).strip()
    total_cp = 0
    for num_s, unit in re.findall(r"(\d+)\s*(pp|gp|ep|sp|cp)", raw.lower()):
        total_cp += int(num_s) * COIN_TO_CP[unit]
    return total_cp, m


def _write_currency_cp(text: str, new_cp: int) -> str:
    formatted = format_cp(new_cp)
    m = re.search(r"(\*\*Currency:\*\*\s*)(.+)", text)
    if m:
        return text[: m.start()] + f"{m.group(1)}{formatted}" + text[m.end() :]
    # Append under Equipment & Inventory if missing
    if "## Equipment & Inventory" in text:
        return text.replace(
            "## Equipment & Inventory",
            f"## Equipment & Inventory\n**Currency:** {formatted}",
            1,
        )
    return text.rstrip() + f"\n\n**Currency:** {formatted}\n"


def _add_inventory_item(text: str, item: str) -> str:
    marker = "**Adventuring Gear:**"
    idx = text.find(marker)
    if idx != -1:
        insert_pos = idx + len(marker)
        return text[:insert_pos] + f"\n- {item}" + text[insert_pos:]
    if "## Equipment & Inventory" in text:
        return text.replace(
            "## Equipment & Inventory",
            f"## Equipment & Inventory\n**Adventuring Gear:**\n- {item}",
            1,
        )
    return text.rstrip() + f"\n\n## Equipment & Inventory\n**Adventuring Gear:**\n- {item}\n"


def _remove_inventory_item(text: str, item: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    target = item.strip().lower()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("-", "*")) and target in stripped.lower():
            del lines[i]
            return "".join(lines), True
    return text, False


def _ledger_path(campaign: str) -> pathlib.Path:
    cdir = find_campaign(campaign)
    cdir.mkdir(parents=True, exist_ok=True)
    return cdir / ".economy_ledger.json"


def _append_ledger(campaign: str, entry: dict) -> None:
    lp = _ledger_path(campaign)
    entries = []
    if lp.exists():
        try:
            entries = json.loads(lp.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    entries.append(entry)
    lp.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def adjust_gold(campaign: str, char_name: str, delta_spec: str, reason: str = "") -> dict:
    cf = _find_char_file(campaign, char_name)
    text = cf.read_text(encoding="utf-8")
    cur_cp, _ = _read_currency_cp(text)
    delta_cp = parse_coin_amount(delta_spec)
    new_cp = cur_cp + delta_cp
    if new_cp < 0:
        raise ValueError(
            f"Insufficient funds for {char_name}: has {format_cp(cur_cp)}, "
            f"attempted delta {format_cp(delta_cp)}"
        )
    cf.write_text(_write_currency_cp(text, new_cp), encoding="utf-8")
    rec = {
        "type": "gold",
        "character": char_name,
        "before": format_cp(cur_cp),
        "delta": format_cp(delta_cp),
        "after": format_cp(new_cp),
        "reason": reason,
    }
    _append_ledger(campaign, rec)
    return rec


def buy_item(campaign: str, char_name: str, item: str, cost_spec: str) -> dict:
    cf = _find_char_file(campaign, char_name)
    text = cf.read_text(encoding="utf-8")
    cur_cp, _ = _read_currency_cp(text)
    cost_cp = abs(parse_coin_amount(cost_spec))
    if cur_cp < cost_cp:
        raise ValueError(
            f"Insufficient funds for {char_name} to buy '{item}': "
            f"has {format_cp(cur_cp)}, needs {format_cp(cost_cp)}"
        )
    new_cp = cur_cp - cost_cp
    text = _write_currency_cp(text, new_cp)
    text = _add_inventory_item(text, item)
    cf.write_text(text, encoding="utf-8")
    rec = {
        "type": "buy",
        "character": char_name,
        "item": item,
        "cost": format_cp(cost_cp),
        "before": format_cp(cur_cp),
        "after": format_cp(new_cp),
    }
    _append_ledger(campaign, rec)
    return rec


def sell_item(campaign: str, char_name: str, item: str, value_spec: str) -> dict:
    cf = _find_char_file(campaign, char_name)
    text = cf.read_text(encoding="utf-8")
    new_text, removed = _remove_inventory_item(text, item)
    if not removed:
        raise ValueError(f"Item '{item}' not found in {char_name}'s inventory.")
    cur_cp, _ = _read_currency_cp(new_text)
    val_cp = abs(parse_coin_amount(value_spec))
    new_cp = cur_cp + val_cp
    new_text = _write_currency_cp(new_text, new_cp)
    cf.write_text(new_text, encoding="utf-8")
    rec = {
        "type": "sell",
        "character": char_name,
        "item": item,
        "value": format_cp(val_cp),
        "before": format_cp(cur_cp),
        "after": format_cp(new_cp),
    }
    _append_ledger(campaign, rec)
    return rec


def _apply_rest_to_sheet(text: str, rest_type: str, hit_dice_spent: int = 0, heal: int = 0) -> tuple[str, dict]:
    """Deterministically apply 5e Short or Long Rest to a character sheet markdown."""
    changes = {}

    # Parse HP: **HP:** current / max
    hp_pat = re.compile(r"(\*\*HP:\*\*\s*)(\d+)\s*/\s*(\d+)")
    hp_m = hp_pat.search(text)
    if hp_m:
        cur_hp, max_hp = int(hp_m.group(2)), int(hp_m.group(3))
        if rest_type == "long":
            new_hp = max_hp
        else:
            new_hp = min(max_hp, cur_hp + max(0, heal))
        if new_hp != cur_hp:
            text = hp_pat.sub(rf"\g<1>{new_hp} / {max_hp}", text, count=1)
            changes["hp"] = f"{cur_hp}->{new_hp}/{max_hp}"

    # Parse Hit Dice: **Hit Dice:** XdY (remaining: R)
    hd_pat = re.compile(r"(\*\*Hit Dice:\*\*\s*)(\d+)(d\d+)\s*\(remaining:\s*(\d+)\)")
    hd_m = hd_pat.search(text)
    if hd_m:
        total_hd, die_s, rem_hd = int(hd_m.group(2)), hd_m.group(3), int(hd_m.group(4))
        if rest_type == "long":
            # 5e rule: regain up to half total hit dice (minimum 1)
            regain = max(1, total_hd // 2)
            new_rem = min(total_hd, rem_hd + regain)
        else:
            spend = max(0, min(rem_hd, hit_dice_spent))
            new_rem = rem_hd - spend
        if new_rem != rem_hd:
            text = hd_pat.sub(rf"\g<1>{total_hd}{die_s} (remaining: {new_rem})", text, count=1)
            changes["hit_dice"] = f"{rem_hd}->{new_rem}/{total_hd}"

    # Long rest restores all spell slots (Used -> 0) and resets death saves
    if rest_type == "long":
        def _reset_slot_row(m):
            return f"| {m.group(1)} | {m.group(2)} | 0 |"

        new_text, n_slots = re.subn(
            r"\|\s*(\d+(?:st|nd|rd|th))\s*\|\s*(\d+)\s*\|\s*[1-9]\d*\s*\|",
            _reset_slot_row,
            text,
        )
        if n_slots:
            text = new_text
            changes["spell_slots_restored"] = n_slots

        ds_pat = re.compile(r"(\*\*Death Saves:\*\*\s*)Successes:\s*\d+\s*\|\s*Failures:\s*\d+")
        if ds_pat.search(text):
            text = ds_pat.sub(r"\g<1>Successes: 0 | Failures: 0", text, count=1)

    return text, changes


def apply_rest(
    campaign: str,
    rest_type: str,
    char_name: str = "all",
    hit_dice: int = 0,
    heal: int = 0,
) -> list[dict]:
    cdir = find_campaign(campaign) / "characters"
    if char_name.lower() == "all":
        files = sorted(cdir.glob("*.md"))
    else:
        files = [_find_char_file(campaign, char_name)]
    if not files:
        raise FileNotFoundError(f"No character sheets found in {cdir}")

    results = []
    for cf in files:
        text = cf.read_text(encoding="utf-8")
        new_text, changes = _apply_rest_to_sheet(text, rest_type, hit_dice_spent=hit_dice, heal=heal)
        if new_text != text:
            cf.write_text(new_text, encoding="utf-8")
        cname = cf.stem
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if first_line.startswith("#"):
            cname = first_line.lstrip("#").strip() or cname
        rec = {"type": f"rest_{rest_type}", "character": cname, "changes": changes}
        _append_ledger(campaign, rec)
        results.append(rec)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic D&D economy & rest recovery engine.")
    parser.add_argument("-c", "--campaign", required=True, help="Campaign name")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gold = sub.add_parser("gold", help="Adjust character currency atomically")
    p_gold.add_argument("--char", required=True)
    p_gold.add_argument("--delta", required=True, help="e.g. -15gp, +25gp, 5sp")
    p_gold.add_argument("--reason", default="")

    p_buy = sub.add_parser("buy", help="Buy an item (atomic gold + inventory)")
    p_buy.add_argument("--char", required=True)
    p_buy.add_argument("--item", required=True)
    p_buy.add_argument("--cost", required=True, help="e.g. 50gp")

    p_sell = sub.add_parser("sell", help="Sell an item (atomic inventory + gold)")
    p_sell.add_argument("--char", required=True)
    p_sell.add_argument("--item", required=True)
    p_sell.add_argument("--value", required=True, help="e.g. 5gp")

    p_rest = sub.add_parser("rest", help="Apply deterministic 5e short or long rest")
    p_rest.add_argument("--type", choices=["short", "long"], required=True)
    p_rest.add_argument("--char", default="all", help="Character name or 'all'")
    p_rest.add_argument("--hit-dice", type=int, default=0, help="Hit dice spent (short rest)")
    p_rest.add_argument("--heal", type=int, default=0, help="HP healed from hit dice (short rest)")

    p_led = sub.add_parser("ledger", help="Show recent economy & rest ledger entries")
    p_led.add_argument("--limit", type=int, default=15)

    args = parser.parse_args()
    try:
        if args.cmd == "gold":
            rec = adjust_gold(args.campaign, args.char, args.delta, args.reason)
            print(f"{rec['character']}: {rec['before']} → {rec['after']} ({rec['delta']})"
                  + (f" — {rec['reason']}" if rec["reason"] else ""))
        elif args.cmd == "buy":
            rec = buy_item(args.campaign, args.char, args.item, args.cost)
            print(f"{rec['character']} bought '{rec['item']}' for {rec['cost']} (balance: {rec['after']})")
        elif args.cmd == "sell":
            rec = sell_item(args.campaign, args.char, args.item, args.value)
            print(f"{rec['character']} sold '{rec['item']}' for {rec['value']} (balance: {rec['after']})")
        elif args.cmd == "rest":
            results = apply_rest(args.campaign, args.type, args.char, args.hit_dice, args.heal)
            for r in results:
                ch = ", ".join(f"{k}={v}" for k, v in r["changes"].items()) or "already full"
                print(f"[{args.type.upper()} REST] {r['character']}: {ch}")
        elif args.cmd == "ledger":
            lp = _ledger_path(args.campaign)
            if not lp.exists():
                print("No ledger entries yet.")
                return
            entries = json.loads(lp.read_text(encoding="utf-8"))
            for e in entries[-args.limit :]:
                print(json.dumps(e, ensure_ascii=False))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
