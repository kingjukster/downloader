#!/usr/bin/env python3
"""Preview or apply collision-safe cleanup of legacy downloader filenames."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from download_from_captured_m3u8 import clean_media_filename


def build_plan(roots: list[Path]) -> tuple[list[tuple[Path, Path]], list[dict[str, str]]]:
    plan: list[tuple[Path, Path]] = []
    skipped: list[dict[str, str]] = []
    reserved: set[str] = set()

    for root in roots:
        if not root.is_dir():
            skipped.append({"path": str(root), "reason": "root does not exist"})
            continue
        for source in sorted(root.rglob("*.mp4"), key=lambda path: str(path).casefold()):
            clean_name = clean_media_filename(source.name)
            if not clean_name or clean_name == source.name:
                continue
            destination = source.with_name(clean_name)
            destination_key = str(destination).casefold()
            if destination.exists():
                skipped.append({"path": str(source), "reason": f"destination exists: {destination}"})
                continue
            if destination_key in reserved:
                skipped.append({"path": str(source), "reason": f"duplicate destination: {destination}"})
                continue
            reserved.add(destination_key)
            plan.append((source, destination))
    return plan, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path, help="Folders to scan recursively.")
    parser.add_argument("--apply", action="store_true", help="Apply the planned renames.")
    parser.add_argument("--log", type=Path, help="Write a JSON audit log.")
    args = parser.parse_args()

    roots = [root.resolve() for root in args.roots]
    plan, skipped = build_plan(roots)
    changes: list[dict[str, str]] = []
    for source, destination in plan:
        print(f"{source} -> {destination}")
        if args.apply:
            source.rename(destination)
        changes.append({"from": str(source), "to": str(destination)})

    report = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "mode": "apply" if args.apply else "preview",
        "roots": [str(root) for root in roots],
        "renamed_count": len(changes) if args.apply else 0,
        "planned_count": len(changes),
        "skipped_count": len(skipped),
        "changes": changes,
        "skipped": skipped,
    }
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("mode", "renamed_count", "planned_count", "skipped_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
