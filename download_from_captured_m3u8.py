#!/usr/bin/env python3
"""
Capture an m3u8 request from a page URL, then download it with ffmpeg.

Example:
  python download_from_captured_m3u8.py "https://example.com/page" -o video.mp4
  # One link automatically expands a TV-show page, or downloads a movie directly.
  python download_from_captured_m3u8.py "https://example.com/one" "https://example.com/two" -o downloads
  python download_from_captured_m3u8.py --url-file urls.txt -o downloads

For live streams, ffmpeg will keep recording until the stream ends or you press Ctrl+C.
Use --duration to stop after a fixed number of seconds.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


DEFAULT_CAPTURE_SCRIPT = Path(__file__).resolve().with_name("capture_m3u8_curl.py")
DEFAULT_PLAYLIST_MATCH = ".m3u8"
DEFAULT_OPERA_AIR = Path.home() / r"AppData\Local\Programs\Opera Air\opera.exe"
DEFAULT_OPERA_AIR_PROFILE = Path.home() / r"AppData\Roaming\Opera Software\Opera Air Stable"
DEFAULT_CAPTURE_PROFILE = Path(__file__).resolve().parent / "work" / "opera-air-capture-profile"
DEFAULT_DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DEFAULT_EXTENSION_SOURCE = DEFAULT_OPERA_AIR_PROFILE / "Default" / "Extensions"
DEFAULT_EXTENSION_IDS = {
    "new_tab_blocker": "bkanhpjdajkdnibkplooolgpidgfjfnn",
    "pie_adblock": "jpkfgepcmmchgfbjblnodjhldacghenp",
}


def extract_curl_command(text: str) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("curl ")), None)
    if start is None:
        raise ValueError("capture script did not print a curl command")

    command_lines: list[str] = []
    for line in lines[start:]:
        command_lines.append(line)
        if not line.rstrip().endswith("\\"):
            break

    return "\n".join(command_lines)


def parse_curl_command(curl_text: str) -> tuple[str, list[str]]:
    one_line = re.sub(r"\\\s*\r?\n\s*", " ", curl_text).strip()
    parts = shlex.split(one_line, posix=True)
    if not parts or parts[0] != "curl":
        raise ValueError("captured command is not a curl command")

    m3u8_url: str | None = None
    headers: list[str] = []
    i = 1
    while i < len(parts):
        part = parts[i]
        if part in ("-H", "--header"):
            if i + 1 >= len(parts):
                raise ValueError("curl header flag is missing a value")
            headers.append(parts[i + 1])
            i += 2
            continue
        if part.startswith("http://") or part.startswith("https://"):
            m3u8_url = part
        i += 1

    if not m3u8_url:
        raise ValueError("could not find an m3u8 URL in the captured curl command")

    return m3u8_url, headers


def safe_filename_part(value: str) -> str:
    value = unquote(value).strip().lower()
    value = re.sub(r"^www\.", "", value)
    value = re.sub(r"[^a-z0-9._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    return value


def media_title_parts(value: str) -> tuple[str, str | None]:
    """Turn a URL title slug into a readable title and optional release year."""
    slug = unquote(value).strip().strip("/._- ")
    slug = re.sub(r"^\d+-", "", slug)
    slug = re.sub(r"-0$", "", slug)

    year_match = re.search(r"-(19\d{2}|20\d{2})$", slug)
    year = year_match.group(1) if year_match else None
    if year_match:
        slug = slug[: year_match.start()]

    words = re.sub(r"[-_]+", " ", slug).split()
    minor_words = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to"}
    title_words = [
        word.lower() if 0 < index < len(words) - 1 and word.lower() in minor_words else word.capitalize()
        for index, word in enumerate(words)
    ]
    title = " ".join(title_words)
    return title or "Captured Video", year


def clean_media_filename(filename: str) -> str | None:
    """Return a season-first library filename for a legacy MP4 name."""
    title_first_match = re.fullmatch(
        r"(?P<title>.+?) - S(?P<season>\d+)E(?P<episode>\d+)\.mp4",
        filename,
        flags=re.IGNORECASE,
    )
    if title_first_match:
        return (
            f"S{int(title_first_match.group('season')):02d}"
            f"E{int(title_first_match.group('episode')):02d} - "
            f"{title_first_match.group('title')}.mp4"
        )

    match = re.fullmatch(
        r"[^_]+_(?:movies|shows)_play_(?P<slug>.+?)"
        r"(?:_s(?P<season>\d+)-e(?P<episode>\d+))?\.mp4",
        filename,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    title, year = media_title_parts(match.group("slug"))
    stem = f"{title} ({year})" if year else title
    if match.group("season") and match.group("episode"):
        stem = f"S{int(match.group('season')):02d}E{int(match.group('episode')):02d} - {stem}"
    return f"{stem}.mp4"


def episode_coordinates(url: str) -> tuple[int, int] | None:
    """Return season/episode coordinates from a fragment or flat query URL."""
    decoded = unquote(url)
    match = re.search(r"[#&]S(\d+)-E(\d+)(?:-|$)", decoded, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2))

    query = parse_qs(urlparse(url).query)
    for key, values in query.items():
        if key.casefold() != "episode":
            continue
        for value in values:
            if value.isdigit() and int(value) > 0:
                return 1, int(value)
    return None


def default_output_path(url: str) -> Path:
    parsed = urlparse(url)
    path_parts = [safe_filename_part(part) for part in parsed.path.split("/") if part]
    path_parts = [part for part in path_parts if part]
    title_source = path_parts[-1] if path_parts else safe_filename_part(parsed.netloc)
    title, year = media_title_parts(title_source)
    stem = f"{title} ({year})" if year else title
    coordinates = episode_coordinates(url)
    if coordinates:
        season, episode = coordinates
        stem = f"S{season:02d}E{episode:02d} - {stem}"

    return Path.cwd() / f"{stem[:140]}.mp4"


def available_output_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def latest_extension_version_dir(extension_id: str) -> Path | None:
    root = DEFAULT_EXTENSION_SOURCE / extension_id
    if not root.exists():
        return None
    version_dirs = [path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").exists()]
    if not version_dirs:
        return None
    return max(version_dirs, key=lambda path: path.stat().st_mtime)


def default_extension_dirs() -> list[Path]:
    dirs: list[Path] = []
    for extension_id in DEFAULT_EXTENSION_IDS.values():
        path = latest_extension_version_dir(extension_id)
        if path:
            dirs.append(path)
    return dirs


def run_capture(args: argparse.Namespace) -> str:
    if not args.capture_script.exists():
        raise FileNotFoundError(f"capture script not found: {args.capture_script}")

    cmd = [
        sys.executable,
        str(args.capture_script),
        args.current_url,
        "--timeout",
        str(args.capture_timeout),
    ]
    if args.visible:
        cmd.append("--visible")
    if args.contains:
        cmd.extend(["--contains", args.contains])
    if args.user_data_dir:
        cmd.extend(["--user-data-dir", str(args.user_data_dir)])
    if args.browser_executable:
        cmd.extend(["--executable-path", str(args.browser_executable)])
    for extension_dir in args.extension_dir:
        cmd.extend(["--extension-dir", str(extension_dir)])
    if args.top_right_click_delay is not None:
        cmd.extend(["--top-right-click-delay", str(args.top_right_click_delay)])
        cmd.extend(["--top-right-click-offset", str(args.top_right_click_offset)])

    print("Running capture:", " ".join(shlex.quote(p) for p in cmd), file=sys.stderr)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        raise RuntimeError(f"capture script failed with exit code {result.returncode}")
    return result.stdout


def run_discovery(args: argparse.Namespace, url: str) -> list[str]:
    if not args.capture_script.exists():
        raise FileNotFoundError(f"capture script not found: {args.capture_script}")

    cmd = [
        sys.executable,
        str(args.capture_script),
        url,
        "--discover-episode-urls",
        "--discover-wait",
        str(args.discover_wait),
    ]
    if args.discover_debug_output:
        cmd.extend(["--discover-debug-output", str(args.discover_debug_output)])
    if args.discover_dropdowns:
        cmd.append("--discover-dropdowns")
    if args.episode_counts:
        cmd.extend(["--episode-counts", args.episode_counts])
    cmd.extend(["--dropdown-wait", str(args.dropdown_wait)])
    if args.visible:
        cmd.append("--visible")
    if args.user_data_dir:
        cmd.extend(["--user-data-dir", str(args.user_data_dir)])
    if args.browser_executable:
        cmd.extend(["--executable-path", str(args.browser_executable)])
    for extension_dir in args.extension_dir:
        cmd.extend(["--extension-dir", str(extension_dir)])
    print("Running discovery:", " ".join(shlex.quote(p) for p in cmd), file=sys.stderr)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode not in (0, 3):
        raise RuntimeError(f"episode discovery failed with exit code {result.returncode}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith(("http://", "https://"))]


def discover_series_urls(args: argparse.Namespace, urls: list[str]) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()
    for index, url in enumerate(urls, start=1):
        print(f"\n[{index}/{len(urls)}] Discovering episode URLs from: {url}", file=sys.stderr)
        for discovered_url in run_discovery(args, url):
            if discovered_url not in seen:
                seen.add(discovered_url)
                discovered.append(discovered_url)
    return sorted(discovered, key=episode_url_sort_key)


def episode_url_sort_key(url: str) -> tuple[int, int, int, str]:
    """Put S/E episode URLs in playback order while keeping a stable fallback."""
    decoded = unquote(url)
    coordinates = episode_coordinates(url)
    if not coordinates:
        return (1, 0, 0, decoded.casefold())
    season, episode = coordinates
    return (0, season, episode, decoded.casefold())


def should_auto_expand(args: argparse.Namespace, urls: list[str]) -> bool:
    """Use automatic title detection for a single directly supplied link."""
    if args.auto_expand is not None:
        return args.auto_expand
    has_manual_expansion = any(
        value is not None for value in (args.seasons, args.episodes, args.episode_counts)
    )
    return (
        len(urls) == 1
        and len(args.urls) == 1
        and not args.url_file
        and not args.catalog_server
        and not has_manual_expansion
    )


def auto_expand_title(args: argparse.Namespace, urls: list[str]) -> list[str]:
    """Discover a series queue, falling back to the seed for a single title."""
    discovery_args = copy.copy(args)
    if discovery_args.discover_dropdowns is None:
        discovery_args.discover_dropdowns = True
    discovered = discover_series_urls(discovery_args, urls)
    if discovered:
        print(f"\nDetected a TV show; queued {len(discovered)} episode URL(s).", file=sys.stderr)
        return discovered
    print("\nNo episode URLs found; treating the supplied link as one title.", file=sys.stderr)
    return urls


def run_ffmpeg(
    ffmpeg: str,
    m3u8_url: str,
    headers: list[str],
    output: Path,
    duration: float | None,
    overwrite: bool,
) -> int:
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists, pass --overwrite to replace it: {output}")

    header_blob = "".join(f"{header}\r\n" for header in headers)
    cmd = [ffmpeg, "-hide_banner"]
    if overwrite:
        cmd.append("-y")
    else:
        cmd.append("-n")
    if duration is not None:
        cmd.extend(["-t", str(duration)])
    if header_blob:
        cmd.extend(["-headers", header_blob])
    cmd.extend(
        [
            "-allowed_segment_extensions",
            "ALL",
            "-i",
            m3u8_url,
            "-c",
            "copy",
            str(output),
        ]
    )

    print("Downloading to:", output, file=sys.stderr)
    print("Running ffmpeg:", " ".join(shlex.quote(p) for p in cmd), file=sys.stderr)
    return subprocess.call(cmd)


def load_url_file(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def load_catalog_urls(args: argparse.Namespace) -> list[str]:
    if not args.catalog_server:
        return []

    cmd = [
        "ssh",
        args.catalog_server,
        f"python3 {shlex.quote(args.catalog_tool)} export-staging",
    ]
    print("Loading catalog URLs:", " ".join(shlex.quote(part) for part in cmd), file=sys.stderr)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        raise RuntimeError(f"catalog export failed with exit code {result.returncode}")

    rows = []
    for line_number, line in enumerate(result.stdout.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"catalog export line {line_number} is not valid JSON") from exc
        rows.append(row)

    if args.catalog_show:
        rows = [
            row for row in rows
            if str(row.get("show_title", "")).casefold() == args.catalog_show.casefold()
        ]
    for excluded_show in args.catalog_exclude_show:
        rows = [
            row for row in rows
            if str(row.get("show_title", "")).casefold() != excluded_show.casefold()
        ]
    if args.catalog_status:
        rows = [
            row for row in rows
            if str(row.get("status", "")).casefold() == args.catalog_status.casefold()
        ]

    rows.sort(
        key=lambda row: (
            int(row.get("season_number") or 0),
            int(row.get("episode_number") or 0),
            str(row.get("source_url") or ""),
        )
    )

    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("source_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    print(f"Loaded {len(urls)} URL(s) from catalog.", file=sys.stderr)
    return urls


def parse_episode_counts(value: str | None) -> list[int] | None:
    if value is None:
        return None
    counts = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not counts:
        raise ValueError("--episode-counts must contain at least one count")
    if any(count < 1 for count in counts):
        raise ValueError("--episode-counts values must be positive integers")
    return counts


def expand_series_urls(
    urls: list[str],
    seasons: int | None,
    episodes: int | None,
    episode_counts: list[int] | None,
) -> list[str]:
    if episode_counts is None and seasons is None and episodes is None:
        return urls
    if episode_counts is not None and (seasons is not None or episodes is not None):
        raise ValueError("use either --episode-counts or -s/-e, not both")
    if episode_counts is None and (seasons is None or episodes is None):
        raise ValueError("use both -s/--seasons and -e/--episodes for series expansion")
    if episode_counts is None and (seasons < 1 or episodes < 1):
        raise ValueError("-s/--seasons and -e/--episodes must be positive integers")

    pattern = re.compile(r"S\d+-E\d+", flags=re.IGNORECASE)
    expanded: list[str] = []
    for url in urls:
        if not pattern.search(url):
            raise ValueError(f"series URL must contain a fragment like S1-E1: {url}")

        season_ranges = (
            enumerate(episode_counts, start=1)
            if episode_counts is not None
            else ((season, episodes) for season in range(1, seasons + 1))
        )
        for season, episode_total in season_ranges:
            for episode in range(1, episode_total + 1):
                expanded.append(pattern.sub(f"S{season}-E{episode}", url, count=1))
    return expanded


def intended_output_path(args: argparse.Namespace, url: str, multiple: bool) -> Path:
    if args.output:
        output = args.output.expanduser()
        if multiple or output.suffix == "":
            output = output / default_output_path(url).name
    else:
        output = DEFAULT_DOWNLOAD_DIR / default_output_path(url).name

    return output.resolve()


def resolve_output_path(args: argparse.Namespace, url: str, multiple: bool) -> Path:
    output = intended_output_path(args, url, multiple)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not args.overwrite:
        output = available_output_path(output)
    return output


def resolve_output_paths(args: argparse.Namespace, urls: list[str], multiple: bool) -> list[Path]:
    outputs: list[Path] = []
    seen: set[Path] = set()
    for url in urls:
        output = resolve_output_path(args, url, multiple)
        if output in seen:
            for index in range(2, 1000):
                candidate = output.with_name(f"{output.stem}-{index}{output.suffix}")
                if candidate not in seen and (args.overwrite or not candidate.exists()):
                    output = candidate
                    break
        seen.add(output)
        outputs.append(output)
    return outputs


def skip_existing_urls(
    args: argparse.Namespace,
    urls: list[str],
    multiple: bool,
) -> list[str]:
    """Remove URLs whose deterministic target already contains downloaded data."""
    pending: list[str] = []
    for url in urls:
        output = intended_output_path(args, url, multiple)
        if output.is_file() and output.stat().st_size > 0:
            print(f"Skipping existing output: {output}", file=sys.stderr)
            continue
        pending.append(url)
    return pending


def args_for_job(args: argparse.Namespace, index: int) -> argparse.Namespace:
    job_args = copy.copy(args)
    if args.parallel > 1 and args.user_data_dir:
        job_args.user_data_dir = args.user_data_dir.with_name(f"{args.user_data_dir.name}-job-{index}")
    return job_args


def download_one(args: argparse.Namespace, ffmpeg: str, url: str, output: Path, index: int, total: int) -> int:
    args.current_url = url
    print(f"\n[{index}/{total}] Processing: {url}", file=sys.stderr)
    captured_stdout = run_capture(args)
    curl_text = extract_curl_command(captured_stdout)
    m3u8_url, headers = parse_curl_command(curl_text)
    print("Captured m3u8:", m3u8_url, file=sys.stderr)
    return run_ffmpeg(ffmpeg, m3u8_url, headers, output, args.duration, args.overwrite)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture an m3u8 curl command from a page URL and download the stream."
    )
    parser.add_argument("urls", nargs="*", help="Page URL(s) to open in the capture browser.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output video path for one URL, or output directory for multiple URLs.",
    )
    parser.add_argument(
        "--url-file",
        type=Path,
        action="append",
        default=[],
        help="Text file containing one page URL per line. Can be passed more than once.",
    )
    parser.add_argument(
        "--catalog-server",
        help="SSH host alias for a Blacklight media catalog server to read staged URLs from.",
    )
    parser.add_argument(
        "--catalog-tool",
        default="/home/serveradmin/blacklight-media-catalog/media_catalog.py",
        help="Remote media_catalog.py path. Default: /home/serveradmin/blacklight-media-catalog/media_catalog.py.",
    )
    parser.add_argument(
        "--catalog-show",
        help="Only use catalog rows whose show_title matches this value.",
    )
    parser.add_argument(
        "--catalog-exclude-show",
        action="append",
        default=[],
        help="Exclude catalog rows whose show_title matches this value. Can be passed more than once.",
    )
    parser.add_argument(
        "--catalog-status",
        default="pending",
        help='Only use catalog rows whose status matches this value. Default: "pending".',
    )
    parser.add_argument(
        "-s",
        "--seasons",
        type=int,
        default=None,
        help="Expand URL(s) containing S1-E1 into seasons 1..S.",
    )
    parser.add_argument(
        "-e",
        "--episodes",
        type=int,
        default=None,
        help="Expand URL(s) containing S1-E1 into episodes 1..E for each season.",
    )
    parser.add_argument(
        "--episode-counts",
        default=None,
        help="Comma-separated per-season episode counts, e.g. 6,13,16 for S1=6, S2=13, S3=16.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of URLs to process at once. Default: 1.",
    )
    parser.add_argument(
        "--dry-run-urls",
        action="store_true",
        help=(
            "Print the final URL queue and exit without capture or downloading. "
            "Automatic episode discovery may still open the browser."
        ),
    )
    parser.add_argument(
        "--discover-series",
        action="store_true",
        help="Open seed URL(s), discover episode URLs like #S1-E1-35655, then download those exact URLs.",
    )
    parser.add_argument(
        "--auto-expand",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Detect whether the input is a TV show, download all discovered episodes, "
            "or fall back to the supplied link. Enabled automatically for one direct URL."
        ),
    )
    parser.add_argument(
        "--discover-wait",
        type=float,
        default=5,
        help="Seconds to wait before scanning a seed page for episode URLs. Default: 5.",
    )
    parser.add_argument(
        "--discover-debug-output",
        type=Path,
        default=None,
        help="Optional file path to save scanned discovery text for troubleshooting.",
    )
    parser.add_argument(
        "--discover-dropdowns",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "During discovery, traverse paginated All Episodes grids or drive visible "
            "Season/Episode dropdowns and record resulting URLs. Enabled by default in "
            "automatic one-link mode."
        ),
    )
    parser.add_argument(
        "--dropdown-wait",
        type=float,
        default=0.75,
        help="Seconds to wait after each interactive episode click. Default: 0.75.",
    )
    parser.add_argument(
        "--capture-script",
        type=Path,
        default=DEFAULT_CAPTURE_SCRIPT,
        help=f"Path to capture_m3u8_curl.py. Default: {DEFAULT_CAPTURE_SCRIPT}",
    )
    parser.add_argument(
        "--capture-timeout",
        type=float,
        default=60,
        help="Seconds to wait for an m3u8 request. Default: 60.",
    )
    parser.add_argument(
        "--contains",
        default=DEFAULT_PLAYLIST_MATCH,
        help=f'Request URL substring to capture. Default: "{DEFAULT_PLAYLIST_MATCH}".',
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Optional seconds to record. Useful for live streams.",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=DEFAULT_CAPTURE_PROFILE,
        help=(
            "Dedicated Chromium profile directory for capture cookies/session state. "
            f"Default: {DEFAULT_CAPTURE_PROFILE}."
        ),
    )
    parser.add_argument(
        "--browser-executable",
        type=Path,
        default=DEFAULT_OPERA_AIR if DEFAULT_OPERA_AIR.exists() else None,
        help=(
            "Chromium-based browser executable for capture. "
            f"Defaults to Opera Air when found at {DEFAULT_OPERA_AIR}."
        ),
    )
    parser.add_argument(
        "--visible",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show the browser window during capture. Default: enabled.",
    )
    parser.add_argument(
        "--top-right-click-delay",
        type=float,
        default=15,
        help="Seconds to wait after page load, then click near the page top-right. Default: 15.",
    )
    parser.add_argument(
        "--top-right-click-offset",
        type=int,
        default=24,
        help="Pixels inward from the page top-right for the click. Default: 24.",
    )
    parser.add_argument(
        "--no-top-right-click",
        action="store_true",
        help="Disable the automatic delayed top-right click.",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        action="append",
        default=None,
        help=(
            "Unpacked extension directory to load into the capture profile. "
            "Can be passed more than once. Defaults to your installed New-Tab Blocker and Pie Adblock."
        ),
    )
    parser.add_argument(
        "--no-default-extensions",
        action="store_true",
        help="Do not auto-load the default Opera Air extension directories.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite output if it exists.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip URLs whose normal output file already exists and is non-empty.",
    )
    args = parser.parse_args()

    urls = list(args.urls)
    for url_file in args.url_file:
        urls.extend(load_url_file(url_file))

    custom_extension_dirs = args.extension_dir or []
    args.extension_dir = []
    if not args.no_default_extensions:
        args.extension_dir.extend(default_extension_dirs())
    args.extension_dir.extend(custom_extension_dirs)
    if args.no_top_right_click:
        args.top_right_click_delay = None

    try:
        urls.extend(load_catalog_urls(args))
    except Exception as exc:
        parser.error(str(exc))
    if not urls:
        if args.catalog_server:
            parser.error("catalog filters matched no URLs")
        parser.error("provide at least one URL, --url-file, or --catalog-server")

    auto_expand = should_auto_expand(args, urls)
    if args.discover_series and args.auto_expand is True:
        parser.error("use either --discover-series or --auto-expand, not both")
    if args.discover_series:
        auto_expand = False

    if args.discover_series:
        urls = discover_series_urls(args, urls)
        if not urls:
            parser.error("no episode URLs were discovered")
        print(f"\nDiscovered {len(urls)} episode URL(s).", file=sys.stderr)
    elif auto_expand:
        try:
            urls = auto_expand_title(args, urls)
        except Exception as exc:
            parser.error(f"automatic title discovery failed: {exc}")
    else:
        try:
            urls = expand_series_urls(
                urls,
                args.seasons,
                args.episodes,
                parse_episode_counts(args.episode_counts),
            )
        except ValueError as exc:
            parser.error(str(exc))

    if args.dry_run_urls:
        for url in urls:
            print(url)
        print(f"\n{len(urls)} URL(s) queued.", file=sys.stderr)
        return 0

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg was not found on PATH.", file=sys.stderr)
        return 127
    if args.parallel < 1:
        parser.error("--parallel must be at least 1")

    multiple = len(urls) > 1
    if args.skip_existing and not args.overwrite:
        original_count = len(urls)
        urls = skip_existing_urls(args, urls, multiple)
        if not urls:
            print(f"All {original_count} URL(s) already have output files.", file=sys.stderr)
            return 0
        skipped = original_count - len(urls)
        if skipped:
            print(f"Skipped {skipped}; {len(urls)} URL(s) remain.", file=sys.stderr)
    outputs = resolve_output_paths(args, urls, multiple)
    try:
        failures = 0
        if args.parallel == 1:
            for index, (url, output) in enumerate(zip(urls, outputs), start=1):
                try:
                    code = download_one(args_for_job(args, index), ffmpeg, url, output, index, len(urls))
                    if code != 0:
                        failures += 1
                        print(f"[{index}/{len(urls)}] ffmpeg failed with exit code {code}", file=sys.stderr)
                except Exception as exc:
                    failures += 1
                    print(f"[{index}/{len(urls)}] Error: {exc}", file=sys.stderr)
        else:
            print(f"Processing {len(urls)} URL(s) with parallel={args.parallel}.", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=args.parallel) as executor:
                futures = {
                    executor.submit(
                        download_one,
                        args_for_job(args, index),
                        ffmpeg,
                        url,
                        output,
                        index,
                        len(urls),
                    ): index
                    for index, (url, output) in enumerate(zip(urls, outputs), start=1)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        code = future.result()
                        if code != 0:
                            failures += 1
                            print(f"[{index}/{len(urls)}] ffmpeg failed with exit code {code}", file=sys.stderr)
                    except Exception as exc:
                        failures += 1
                        print(f"[{index}/{len(urls)}] Error: {exc}", file=sys.stderr)

        if failures:
            print(f"\nCompleted with {failures} failure(s) out of {len(urls)} URL(s).", file=sys.stderr)
            return 1
        print(f"\nCompleted {len(urls)} URL(s).", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


