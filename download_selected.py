"""Preview or download exactly one visually selected page, without series expansion."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


def selected_url(value: str) -> str:
    if len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value):
        raise argparse.ArgumentTypeError("Supply one HTTP(S) URL without whitespace")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError()
        parts.port
        # This site's show links require the selected episode's opaque ID too.
        if parts.hostname in {"lookmovie2.to", "www.lookmovie2.to"}:
            if not parts.path.startswith(("/movies/play/", "/shows/play/")):
                raise ValueError("Use the verified player URL, not a search/details page")
            if parts.path.startswith("/shows/play/") and not re.fullmatch(
                r"S[1-9]\d*-E[1-9]\d*-\d+", parts.fragment
            ):
                raise ValueError("Select a season and episode and retain its full URL fragment")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error) or "Invalid HTTP(S) URL or embedded credentials") from None
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=selected_url, help="Exact page URL verified through JetKVM")
    parser.add_argument("--execute", action="store_true", help="Download; default only previews the exact one-item queue")
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    parser.add_argument("--browser-executable", type=Path)
    parser.add_argument("--user-data-dir", type=Path)
    args = parser.parse_args(argv)
    command = [sys.executable, str(Path(__file__).with_name("download_from_captured_m3u8.py")),
               args.url, "--no-auto-expand", "--no-discover-dropdowns",
               "--no-top-right-click", "--parallel", "1", "--skip-existing",
               "--output", str(args.output)]
    for flag, value in (("--browser-executable", args.browser_executable),
                        ("--user-data-dir", args.user_data_dir)):
        if value is not None:
            command.extend([flag, str(value)])
    if not args.execute:
        command.append("--dry-run-urls")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
