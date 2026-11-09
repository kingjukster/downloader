#!/usr/bin/env python3
"""Preview or apply collision-safe cleanup of legacy downloader filenames."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from download_from_captured_m3u8 import clean_media_filename


def rename_noreplace(source: Path, destination: Path) -> None:
    """Move the directory entry only if the destination is still unoccupied."""
    if os.name == "nt":
        # Windows rename fails if the destination already exists.
        source.rename(destination)
        return
    if sys.platform == "linux":
        # renameat2 makes the no-replace check and move one filesystem operation.
        # Unlike a hard link, it moves symlinks and needs no hard-link support.
        at_fdcwd = -100
        rename_noreplace_flag = 1
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), rename_noreplace_flag
        )
    elif sys.platform == "darwin":
        # macOS provides the same atomic exclusion through renamex_np.
        rename_excl_flag = 0x00000004
        renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(destination), rename_excl_flag)
    else:
        raise OSError("Atomic no-replace rename is unsupported on this platform")
    if result != 0:
        error = ctypes.get_errno()
        if error in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(
                error,
                "Atomic no-replace rename is unavailable on this filesystem",
                str(destination),
            )
        raise OSError(error, os.strerror(error), str(destination))


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
            if destination.exists() or destination.is_symlink():
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
        if args.apply:
            try:
                rename_noreplace(source, destination)
            except OSError as error:
                # A directory claimed after planning can report EISDIR or ENOTEMPTY.
                if error.errno not in (errno.EEXIST, errno.EISDIR, errno.ENOTEMPTY):
                    raise
                skipped.append({"path": str(source), "reason": f"destination exists: {destination}"})
                continue
        print(f"{source} -> {destination}")
        changes.append({"from": str(source), "to": str(destination)})

    report = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "mode": "apply" if args.apply else "preview",
        "roots": [str(root) for root in roots],
        "renamed_count": len(changes) if args.apply else 0,
        "planned_count": len(plan),
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
