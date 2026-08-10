#!/usr/bin/env python3
"""
Open a page in Chromium, watch network traffic, and print a bash curl command
for the first request whose URL contains index.m3u8.

Usage:
  python capture_m3u8_curl.py "https://example.com/page" --visible
  python capture_m3u8_curl.py "https://example.com/page" --contains ".m3u8" --timeout 90
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


NOISY_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
}


def bash_quote(value: str) -> str:
    return shlex.quote(value)


def curl_from_request(url: str, method: str, headers: dict[str, str], post_data: str | None) -> str:
    lines = [f"curl {bash_quote(url)}"]

    if method.upper() != "GET":
        lines.append(f"-X {bash_quote(method.upper())}")

    for name, value in sorted(headers.items(), key=lambda item: item[0].lower()):
        lower = name.lower()
        if lower in NOISY_HEADERS:
            continue
        if lower.startswith(":"):
            continue
        if value == "":
            continue
        lines.append(f"-H {bash_quote(f'{name}: {value}')}")

    if post_data:
        lines.append(f"--data-raw {bash_quote(post_data)}")

    return " \\\n  ".join(lines)


async def click_top_right(page: Any, delay: float, offset: int) -> None:
    if delay < 0:
        return
    if delay:
        await page.wait_for_timeout(delay * 1000)

    viewport = page.viewport_size
    if viewport:
        width = viewport["width"]
        height = viewport["height"]
    else:
        size = await page.evaluate("({ width: window.innerWidth, height: window.innerHeight })")
        width = int(size["width"])
        height = int(size["height"])

    x = max(1, width - offset)
    y = min(max(1, offset), max(1, height - 1))
    print(f"Clicking page top-right at ({x}, {y}) after {delay:g}s...", file=sys.stderr)
    await page.mouse.click(x, y)


async def close_page_quietly(page: Any) -> None:
    try:
        if not page.is_closed():
            await page.close()
    except Exception:
        pass


def extract_episode_urls_from_values(values: list[str], base_url: str) -> list[str]:
    # Find the distinctive episode fragment first, then inspect only a bounded
    # prefix for its URL. The previous pair of greedy URL-prefix expressions
    # backtracked quadratically on large script/HTML values without a match.
    fragment_pattern = re.compile(r'#S\d+-E\d+-\d+', re.IGNORECASE)
    url_delimiters = frozenset(" \t\r\n\"'<>`()[]{}=,;")
    max_url_prefix = 4096

    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = (
            unquote(value)
            .replace("\\u0023", "#")
            .replace("\\x23", "#")
            .replace("\\/", "/")
        )
        for match in fragment_pattern.finditer(normalized):
            start = match.start()
            floor = max(0, start - max_url_prefix)
            while start > floor and normalized[start - 1] not in url_delimiters:
                start -= 1
            # If there was no delimiter within a reasonable URL length, avoid
            # treating an arbitrary chunk of minified source as a relative URL.
            candidate = match.group(0) if floor > 0 and start == floor else normalized[start : match.end()]
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if candidate.startswith("#"):
                candidate = base_url.split("#", 1)[0] + candidate
            else:
                candidate = urljoin(base_url, candidate)
            if candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
    return urls


def extract_episode_urls_from_season_data(values: list[str], base_url: str) -> list[str]:
    """Build the complete episode queue from a page's structured season catalog."""
    assignment_pattern = re.compile(
        r"window\.seasons\s*=\s*('(?:\\.|[^'\\])*')",
        re.DOTALL,
    )
    page_url = base_url.split("#", 1)[0]
    urls: list[str] = []
    seen: set[str] = set()

    def numeric_items(mapping: dict) -> list[tuple[str, object]]:
        return sorted(
            mapping.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else sys.maxsize,
        )

    for value in values:
        if "window.seasons" not in value:
            continue
        for match in assignment_pattern.finditer(value):
            try:
                json_text = ast.literal_eval(match.group(1))
                seasons = json.loads(json_text)
            except (SyntaxError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(seasons, dict):
                continue

            for season_key, season_value in numeric_items(seasons):
                if not isinstance(season_value, dict):
                    continue
                episodes = season_value.get("episodes")
                if not isinstance(episodes, dict):
                    continue
                try:
                    season_number = int(season_value.get("season") or season_key)
                except (TypeError, ValueError):
                    continue
                for episode_key, episode_value in numeric_items(episodes):
                    if not isinstance(episode_value, dict):
                        continue
                    try:
                        episode_number = int(episode_value.get("episode_number") or episode_key)
                    except (TypeError, ValueError):
                        continue
                    episode_id = str(episode_value.get("id_episode") or "").strip()
                    if not episode_id:
                        continue
                    url = f"{page_url}#S{season_number}-E{episode_number}-{episode_id}"
                    if url not in seen:
                        seen.add(url)
                        urls.append(url)
    return urls


async def auto_scroll_page(page: Any) -> None:
    try:
        await page.evaluate(
            """
            async () => {
              const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
              const maxScrolls = 12;
              let lastY = -1;
              for (let i = 0; i < maxScrolls; i++) {
                window.scrollTo(0, document.body.scrollHeight);
                await sleep(350);
                if (window.scrollY === lastY) break;
                lastY = window.scrollY;
              }
              window.scrollTo(0, 0);
            }
            """
        )
    except Exception:
        pass


async def collect_page_values(page: Any) -> list[str]:
    values: list[str] = []
    for frame in page.frames:
        try:
            frame_values = await frame.evaluate(
                """
                () => {
                  const values = [];
                  for (const link of document.querySelectorAll('a[href]')) {
                    values.push(link.href);
                    values.push(link.getAttribute('href') || '');
                    values.push(link.textContent || '');
                  }
                  for (const element of document.querySelectorAll('[data-href], [data-url], [data-link], [data-src]')) {
                    values.push(element.getAttribute('data-href') || '');
                    values.push(element.getAttribute('data-url') || '');
                    values.push(element.getAttribute('data-link') || '');
                    values.push(element.getAttribute('data-src') || '');
                  }
                  for (const script of document.querySelectorAll('script')) {
                    values.push(script.src || '');
                    values.push(script.textContent || '');
                  }
                  values.push(location.href);
                  values.push(document.body ? document.body.innerText : '');
                  values.push(document.documentElement ? document.documentElement.innerHTML : '');
                  try { values.push(JSON.stringify(localStorage)); } catch (e) {}
                  try { values.push(JSON.stringify(sessionStorage)); } catch (e) {}
                  return values;
                }
                """
            )
            values.extend(str(value) for value in frame_values if value)
        except Exception:
            continue
    return values


async def visible_text(page: Any) -> str:
    try:
        return await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        return ""


async def click_visible_text(page: Any, text: str, mode: str = "last", timeout: int = 5000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    while asyncio.get_running_loop().time() < deadline:
        clicked = await page.evaluate(
            """
            ({ text, mode }) => {
              const matches = [];
              const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
              while (walker.nextNode()) {
                const element = walker.currentNode;
                if ((element.textContent || '').trim() !== text) continue;
                const style = window.getComputedStyle(element);
                if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
                const rect = element.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
                matches.push({ element, top: rect.top, left: rect.left });
              }
              if (!matches.length) return false;
              matches.sort((a, b) => a.top - b.top || a.left - b.left);
              const match = mode === 'first' ? matches[0] : matches[matches.length - 1];
              match.element.click();
              return true;
            }
            """,
            {"text": text, "mode": mode},
        )
        if clicked:
            return True
        await page.wait_for_timeout(150)
    return False


async def select_dropdown_value(page: Any, current_label: str, target_label: str, kind: str) -> str:
    print(f"Selecting {kind}: {target_label}", file=sys.stderr)
    if not await click_visible_text(page, current_label, mode="first", timeout=4000):
        raise RuntimeError(f"could not open {kind} dropdown from {current_label!r}")
    await page.wait_for_timeout(350)
    if not await click_visible_text(page, target_label, mode="last", timeout=4000):
        raise RuntimeError(f"could not select {kind} option {target_label!r}")
    await page.wait_for_timeout(500)
    return target_label


async def topmost_visible_label(page: Any, pattern: str, fallback: str) -> str:
    label = await page.evaluate(
        """
        ({ pattern, fallback }) => {
          const regex = new RegExp(pattern, 'i');
          const matches = [];
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
          while (walker.nextNode()) {
            const element = walker.currentNode;
            const text = (element.textContent || '').trim();
            if (!regex.test(text)) continue;
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
            const rect = element.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            if (rect.bottom < 0 || rect.right < 0 || rect.top > window.innerHeight || rect.left > window.innerWidth) continue;
            matches.push({ text, top: rect.top, left: rect.left });
          }
          if (!matches.length) return fallback;
          matches.sort((a, b) => a.top - b.top || a.left - b.left);
          return matches[0].text;
        }
        """,
        {"pattern": pattern, "fallback": fallback},
    )
    return str(label or fallback)


def season_numbers_from_text(text: str) -> list[int]:
    return sorted({int(match.group(1)) for match in re.finditer(r"\bSeason\s+(\d+)\b", text, re.IGNORECASE)})


def episode_numbers_from_text(text: str) -> list[int]:
    return sorted({int(match.group(1)) for match in re.finditer(r"\bEpisode\s+(\d+)\b", text, re.IGNORECASE)})


def parse_episode_counts(value: str | None) -> list[int] | None:
    if not value:
        return None
    counts = [int(part.strip()) for part in value.split(",") if part.strip()]
    if any(count < 1 for count in counts):
        raise ValueError("episode counts must be positive integers")
    return counts


async def discover_episode_urls_from_dropdowns(
    page: Any,
    base_url: str,
    episode_counts: list[int] | None,
    per_click_wait: float,
) -> list[str]:
    discovered: list[str] = []
    seen: set[str] = set()

    await click_visible_text(page, "Season 1", mode="first", timeout=3000)
    await page.wait_for_timeout(300)
    seasons = season_numbers_from_text(await visible_text(page))
    if episode_counts:
        seasons = list(range(1, len(episode_counts) + 1))
    if not seasons:
        return []

    current_season_label = "Season 1"
    for season in seasons:
        season_label = f"Season {season}"
        current_season_label = await select_dropdown_value(
            page,
            current_season_label,
            season_label,
            "season",
        )

        if episode_counts and season <= len(episode_counts):
            episodes = list(range(1, episode_counts[season - 1] + 1))
        else:
            await click_visible_text(page, "Episode 1", mode="first", timeout=3000)
            await page.wait_for_timeout(300)
            episodes = episode_numbers_from_text(await visible_text(page))
            if not episodes:
                episodes = [1]

        current_episode_label = await topmost_visible_label(page, r"^Episode\s+\d+$", "Episode 1")
        for episode in episodes:
            episode_label = f"Episode {episode}"
            current_episode_label = await select_dropdown_value(
                page,
                current_episode_label,
                episode_label,
                "episode",
            )
            await page.wait_for_timeout(per_click_wait * 1000)

            values = [page.url, *await collect_page_values(page)]
            for url in extract_episode_urls_from_values(values, base_url):
                if url not in seen:
                    seen.add(url)
                    discovered.append(url)

    return discovered


async def discover_episode_urls(
    page: Any,
    base_url: str,
    wait_seconds: float,
    captured_values: list[str],
) -> list[str]:
    if wait_seconds > 0:
        await page.wait_for_timeout(wait_seconds * 1000)

    await auto_scroll_page(page)
    raw_values = [*captured_values, *await collect_page_values(page)]
    return extract_episode_urls_from_values(raw_values, base_url)


def default_browser_args() -> list[str]:
    """Browser flags shared by every downloader-owned Opera context."""
    return [
        "--autoplay-policy=no-user-gesture-required",
        "--disable-blink-features=AutomationControlled",
        "--mute-audio",
    ]


async def launch_browser(
    playwright: Any,
    visible: bool,
    user_data_dir: Path | None,
    executable_path: Path | None,
    extension_dirs: list[Path],
):
    browser_args = default_browser_args()
    if extension_dirs:
        extension_arg = ",".join(str(path) for path in extension_dirs)
        browser_args.extend(
            [
                f"--disable-extensions-except={extension_arg}",
                f"--load-extension={extension_arg}",
            ]
        )

    launch_args = {
        "headless": not visible,
        "args": browser_args,
    }

    if executable_path:
        launch_args["executable_path"] = str(executable_path)

    if user_data_dir:
        return await playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            **launch_args,
        )

    if executable_path:
        browser = await playwright.chromium.launch(**launch_args)
        return await browser.new_context()

    for channel in ("chrome", "msedge"):
        try:
            browser = await playwright.chromium.launch(channel=channel, **launch_args)
            return await browser.new_context()
        except Exception:
            continue

    browser = await playwright.chromium.launch(**launch_args)
    return await browser.new_context()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture a Network-tab style curl command for index.m3u8."
    )
    parser.add_argument("url", help="Page URL to open.")
    parser.add_argument(
        "--contains",
        default="index.m3u8",
        help='Substring to match in request URLs. Default: "index.m3u8".',
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Seconds to wait for a matching request. Default: 60.",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Show the browser window so you can click play/login/consent prompts.",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        help="Optional Chromium profile directory to reuse cookies/session state.",
    )
    parser.add_argument(
        "--executable-path",
        type=Path,
        help="Optional Chromium-based browser executable, such as Opera Air's opera.exe.",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        action="append",
        default=[],
        help="Optional unpacked extension directory to load. Can be passed more than once.",
    )
    parser.add_argument(
        "--top-right-click-delay",
        type=float,
        default=None,
        help="Optional seconds to wait after page load, then click near the page top-right.",
    )
    parser.add_argument(
        "--top-right-click-offset",
        type=int,
        default=24,
        help="Pixels inward from the page top-right for the optional click. Default: 24.",
    )
    parser.add_argument(
        "--discover-episode-urls",
        action="store_true",
        help="Print discovered episode URLs matching #S#-E#-##### and exit.",
    )
    parser.add_argument(
        "--discover-wait",
        type=float,
        default=5,
        help="Seconds to wait before scanning for episode URLs. Default: 5.",
    )
    parser.add_argument(
        "--discover-debug-output",
        type=Path,
        help="Optional file path to save scanned discovery text for troubleshooting.",
    )
    parser.add_argument(
        "--discover-dropdowns",
        action="store_true",
        help="During discovery, drive visible Season/Episode dropdowns and record resulting URLs.",
    )
    parser.add_argument(
        "--episode-counts",
        help="Comma-separated per-season episode counts for dropdown discovery.",
    )
    parser.add_argument(
        "--dropdown-wait",
        type=float,
        default=0.75,
        help="Seconds to wait after each dropdown episode click. Default: 0.75.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional file path to save the generated curl command.",
    )
    args = parser.parse_args()

    match_text = args.contains.lower()
    found: asyncio.Future[tuple[str, str, dict[str, str], str | None]] = asyncio.Future()
    discovery_values: list[str] = []

    async with async_playwright() as playwright:
        context = await launch_browser(
            playwright,
            args.visible,
            args.user_data_dir,
            args.executable_path,
            args.extension_dir,
        )
        for restored_page in list(context.pages):
            await close_page_quietly(restored_page)
        page = await context.new_page()

        async def on_request(request):
            if found.done():
                return
            if match_text not in request.url.lower():
                return
            headers = await request.all_headers()
            found.set_result((request.url, request.method, headers, request.post_data))

        async def on_response(response):
            if not args.discover_episode_urls:
                return
            try:
                discovery_values.append(response.url)
                content_type = (response.headers.get("content-type") or "").lower()
                if any(kind in content_type for kind in ("json", "javascript", "html", "text")):
                    text = await response.text()
                    if len(text) <= 2_000_000:
                        discovery_values.append(text)
            except Exception:
                return

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError:
            print("Page load timed out, but network capture is still active...", file=sys.stderr)

        if args.top_right_click_delay is not None and not found.done():
            await click_top_right(page, args.top_right_click_delay, args.top_right_click_offset)

        if args.discover_episode_urls:
            await page.wait_for_timeout(max(args.discover_wait, 0) * 1000)
            await auto_scroll_page(page)
            discovery_values.extend(await collect_page_values(page))
            if args.discover_debug_output:
                args.discover_debug_output.parent.mkdir(parents=True, exist_ok=True)
                args.discover_debug_output.write_text(
                    "\n\n--- VALUE ---\n\n".join(discovery_values),
                    encoding="utf-8",
                    errors="ignore",
                )
                print(f"Saved discovery debug text to {args.discover_debug_output}", file=sys.stderr)
            urls = extract_episode_urls_from_values(discovery_values, args.url)
            structured_urls = extract_episode_urls_from_season_data(discovery_values, args.url)
            seen = set(urls)
            for structured_url in structured_urls:
                if structured_url not in seen:
                    seen.add(structured_url)
                    urls.append(structured_url)
            if structured_urls:
                print(
                    f"Found {len(structured_urls)} episode URL(s) in structured season data; "
                    "skipping dropdown traversal.",
                    file=sys.stderr,
                )
            elif args.discover_dropdowns:
                try:
                    dropdown_urls = await discover_episode_urls_from_dropdowns(
                        page,
                        args.url,
                        parse_episode_counts(args.episode_counts),
                        args.dropdown_wait,
                    )
                    seen = set(urls)
                    for dropdown_url in dropdown_urls:
                        if dropdown_url not in seen:
                            seen.add(dropdown_url)
                            urls.append(dropdown_url)
                except Exception as exc:
                    print(f"Dropdown discovery failed: {exc}", file=sys.stderr)
            print(f"Discovered {len(urls)} episode URL(s).", file=sys.stderr)
            for url in urls:
                print(url)
            await close_page_quietly(page)
            await context.close()
            return 0 if urls else 3

        if args.visible:
            print(
                "Browser is open. Click through consent/login and start playback if needed.",
                file=sys.stderr,
            )

        try:
            url, method, headers, post_data = await asyncio.wait_for(found, timeout=args.timeout)
        except asyncio.TimeoutError:
            await close_page_quietly(page)
            await context.close()
            print(
                f"No request containing {args.contains!r} was seen within {args.timeout:g}s.",
                file=sys.stderr,
            )
            return 2

        curl = curl_from_request(url, method, headers, post_data)
        print(curl)

        if args.output:
            args.output.write_text(curl + "\n", encoding="utf-8")
            print(f"\nSaved to {args.output}", file=sys.stderr)

        await close_page_quietly(page)
        await context.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
