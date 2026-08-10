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
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse

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


async def reuse_or_create_context_page(context: Any) -> Any:
    """Reuse Chromium's initial tab so closing it cannot expose about:blank."""
    existing_pages = [page for page in context.pages if not page.is_closed()]
    if not existing_pages:
        return await context.new_page()

    primary_page = existing_pages[0]
    for extra_page in existing_pages[1:]:
        await close_page_quietly(extra_page)
    return primary_page


def extract_episode_urls_from_values(values: list[str], base_url: str) -> list[str]:
    # Find the distinctive episode fragment first, then inspect only a bounded
    # prefix for its URL. The previous pair of greedy URL-prefix expressions
    # backtracked quadratically on large script/HTML values without a match.
    fragment_pattern = re.compile(r'#S\d+-E\d+-\d+', re.IGNORECASE)
    url_delimiters = frozenset(" \t\r\n\"'<>`()[]{}=,;")
    max_url_prefix = 4096

    urls: list[str] = []
    seen: set[str] = set()
    base = urlparse(base_url)
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

        stripped = normalized.strip(" \t\r\n\"'<>`()[]{}=,;")
        if not stripped or len(stripped) > max_url_prefix or any(char.isspace() for char in stripped):
            continue
        query_candidate = urljoin(base_url, stripped)
        parsed_candidate = urlparse(query_candidate)
        if (
            parsed_candidate.scheme.casefold() != base.scheme.casefold()
            or parsed_candidate.netloc.casefold() != base.netloc.casefold()
            or parsed_candidate.path.rstrip("/") != base.path.rstrip("/")
            or episode_query_number(query_candidate) is None
        ):
            continue
        if query_candidate not in seen:
            seen.add(query_candidate)
            urls.append(query_candidate)
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


def parse_episode_grid_summary(text: str) -> tuple[int, int, int | None] | None:
    """Parse pagination text such as ``Page 1 of 5 • 500 total``."""
    match = re.search(
        r"\bPage\s+(\d+)\s+of\s+(\d+)"
        r"(?:\s*(?:[•·|\-]\s*)?(\d+)\s+total)?\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    current_page = int(match.group(1))
    total_pages = int(match.group(2))
    total_items = int(match.group(3)) if match.group(3) else None
    if current_page < 1 or total_pages < current_page:
        return None
    return current_page, total_pages, total_items


def episode_query_number(url: str) -> int | None:
    """Read a positive ``episode=N`` query parameter when present."""
    query = parse_qs(urlparse(url).query)
    for key, values in query.items():
        if key.casefold() != "episode":
            continue
        for value in values:
            if value.isdigit() and int(value) > 0:
                return int(value)
    return None


def expand_numbered_episode_query_urls(url: str, total_items: int | None) -> list[str]:
    """Build a complete flat episode queue when the URL and grid prove its shape."""
    if total_items is None or total_items < 1 or total_items > 100_000:
        return []
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.casefold() == "episode" for key, _ in query_pairs):
        return []

    urls: list[str] = []
    for episode_number in range(1, total_items + 1):
        replaced = [
            (key, str(episode_number) if key.casefold() == "episode" else value)
            for key, value in query_pairs
        ]
        urls.append(parsed._replace(query=urlencode(replaced)).geturl())
    return urls


def episode_numbers_in_urls(urls: list[str]) -> set[int]:
    """Return episode numbers represented by the downloader's S/E fragments."""
    numbers: set[int] = set()
    for url in urls:
        match = re.search(r"[#&]S\d+-E(\d+)(?:-|$)", unquote(url), re.IGNORECASE)
        if match:
            numbers.add(int(match.group(1)))
            continue
        query_number = episode_query_number(url)
        if query_number is not None:
            numbers.add(query_number)
    return numbers


def largest_contiguous_number_run(numbers: list[int]) -> list[int]:
    """Separate a numeric episode grid from small pagination counters."""
    ordered = sorted(set(numbers))
    if not ordered:
        return []
    runs: list[list[int]] = [[ordered[0]]]
    for number in ordered[1:]:
        if number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    return max(runs, key=lambda run: (len(run), run[-1]))


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


async def episode_grid_state(page: Any) -> dict[str, Any] | None:
    """Inspect a visible All Episodes grid without depending on site CSS names."""
    raw_state = await page.evaluate(
        r"""
        () => {
          const visible = element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' &&
              Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
          };
          const textOf = element => (element.innerText || element.textContent || '')
            .replace(/\s+/g, ' ').trim();
          const markers = Array.from(document.querySelectorAll('body *'))
            .filter(element => /^All Episodes$/i.test(textOf(element)) && visible(element));
          markers.sort((a, b) => {
            const aRect = a.getBoundingClientRect();
            const bRect = b.getBoundingClientRect();
            return (aRect.width * aRect.height) - (bRect.width * bRect.height);
          });
          const marker = markers[0];
          if (!marker) return null;

          const pagePattern = /\bPage\s+\d+\s+of\s+\d+/i;
          let root = marker.parentElement;
          while (root) {
            const rootText = textOf(root);
            const numericCount = Array.from(root.querySelectorAll('*')).filter(element =>
              /^\d+$/.test(textOf(element)) && visible(element)
            ).length;
            if (pagePattern.test(rootText) && numericCount >= 2) break;
            if (root === document.body) {
              root = null;
              break;
            }
            root = root.parentElement;
          }
          if (!root) return null;

          const numbers = Array.from(root.querySelectorAll('*'))
            .filter(element => /^\d+$/.test(textOf(element)) && visible(element))
            .map(element => Number(textOf(element)))
            .filter(number => Number.isSafeInteger(number) && number > 0);
          return {
            summary: textOf(root),
            episode_numbers: Array.from(new Set(numbers)).sort((a, b) => a - b),
          };
        }
        """
    )
    if not isinstance(raw_state, dict):
        return None
    summary = str(raw_state.get("summary") or "")
    parsed = parse_episode_grid_summary(summary)
    if not parsed:
        return None
    current_page, total_pages, total_items = parsed
    episode_numbers = largest_contiguous_number_run(
        [
            int(number)
            for number in raw_state.get("episode_numbers", [])
            if str(number).isdigit()
            and int(number) > 0
            and (total_items is None or int(number) <= total_items)
        ]
    )
    if not episode_numbers:
        return None
    return {
        "current_page": current_page,
        "total_pages": total_pages,
        "total_items": total_items,
        "episode_numbers": episode_numbers,
    }


async def click_episode_grid_control(page: Any, label: str | int) -> bool:
    """Click a numeric episode, PREV, or NEXT inside the All Episodes region."""
    return bool(
        await page.evaluate(
            r"""
            ({ label }) => {
              const visible = element => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' &&
                  Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
              };
              const textOf = element => (element.innerText || element.textContent || '')
                .replace(/\s+/g, ' ').trim();
              const markers = Array.from(document.querySelectorAll('body *'))
                .filter(element => /^All Episodes$/i.test(textOf(element)) && visible(element));
              markers.sort((a, b) => {
                const aRect = a.getBoundingClientRect();
                const bRect = b.getBoundingClientRect();
                return (aRect.width * aRect.height) - (bRect.width * bRect.height);
              });
              const marker = markers[0];
              if (!marker) return false;

              const pagePattern = /\bPage\s+\d+\s+of\s+\d+/i;
              let root = marker.parentElement;
              while (root) {
                const rootText = textOf(root);
                const numericCount = Array.from(root.querySelectorAll('*')).filter(element =>
                  /^\d+$/.test(textOf(element)) && visible(element)
                ).length;
                if (pagePattern.test(rootText) && numericCount >= 2) break;
                if (root === document.body) {
                  root = null;
                  break;
                }
                root = root.parentElement;
              }
              if (!root) return false;

              const wanted = String(label).trim().toUpperCase();
              const candidates = Array.from(root.querySelectorAll('*')).filter(element => {
                if (!visible(element)) return false;
                const text = textOf(element).toUpperCase();
                if (/^\d+$/.test(wanted)) return text === wanted;
                return text === wanted || text.startsWith(`${wanted} `);
              });
              if (!candidates.length) return false;
              const semantic = candidates.filter(element => element.matches(
                'a[href],button,[role="button"],[onclick],[data-href],[data-url],[data-link]'
              ));
              const clickable = semantic.length ? semantic : candidates;
              clickable.sort((a, b) => {
                const aRect = a.getBoundingClientRect();
                const bRect = b.getBoundingClientRect();
                return (bRect.width * bRect.height) - (aRect.width * aRect.height);
              });
              clickable[0].click();
              return true;
            }
            """,
            {"label": str(label)},
        )
    )


def unexpected_interactive_navigation(current_url: str, stable_url: str) -> bool:
    """Detect blank or cross-origin navigation caused by an interactive click."""
    if not current_url or current_url == "about:blank":
        return True
    current = urlparse(current_url)
    stable = urlparse(stable_url)
    if current.scheme not in ("http", "https"):
        return True
    return (current.scheme.casefold(), current.netloc.casefold()) != (
        stable.scheme.casefold(),
        stable.netloc.casefold(),
    )


async def settle_guarded_click(
    page: Any,
    pages_before_click: list[Any],
    stable_url: str,
    wait_seconds: float,
    reload_stable: bool = False,
) -> tuple[list[str], bool]:
    """Observe browser state without modifying site JavaScript."""
    popup_values: list[str] = []
    seen_popups: set[int] = set()
    context = page.context
    recovered = False
    deadline = asyncio.get_running_loop().time() + max(wait_seconds, 0)
    first_check = True
    while first_check or asyncio.get_running_loop().time() < deadline:
        first_check = False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await page.wait_for_timeout(min(100, remaining * 1000))

        for candidate in list(context.pages):
            if (
                candidate is page
                or any(candidate is existing for existing in pages_before_click)
                or id(candidate) in seen_popups
            ):
                continue
            seen_popups.add(id(candidate))
            try:
                popup_values.append(candidate.url)
            finally:
                await close_page_quietly(candidate)

        if not page.is_closed() and unexpected_interactive_navigation(page.url, stable_url):
            print(
                "Primary tab was diverted; restoring the requested page.",
                file=sys.stderr,
            )
            recovered = True
            if not reload_stable:
                try:
                    await page.go_back(wait_until="domcontentloaded", timeout=10_000)
                except Exception:
                    pass
            if unexpected_interactive_navigation(page.url, stable_url):
                await page.goto(stable_url, wait_until="domcontentloaded", timeout=20_000)
            elif reload_stable:
                await page.goto(stable_url, wait_until="domcontentloaded", timeout=20_000)

    if popup_values:
        print(f"Closed {len(popup_values)} unexpected pop-up tab(s).", file=sys.stderr)
    return popup_values, recovered


async def navigate_initial_page(page: Any, url: str) -> None:
    """Navigate to the requested site and retry one blank/cross-origin diversion."""
    attempts = 2
    for attempt in range(1, attempts + 1):
        timed_out = False
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError:
            timed_out = True

        diverted = unexpected_interactive_navigation(page.url, url)
        if not diverted:
            if timed_out:
                print("Page load timed out, but network capture is still active...", file=sys.stderr)
            return
        if attempt < attempts:
            print(
                "Initial page navigation was diverted; retrying once.",
                file=sys.stderr,
            )

    raise RuntimeError("initial page navigation repeatedly diverted away from the requested site")


async def wait_for_capture_with_recovery(
    page: Any,
    found: asyncio.Future[Any],
    pages_before_navigation: list[Any],
    stable_url: str,
    timeout: float,
) -> Any:
    """Wait for a captured request while restoring late browser diversions."""
    deadline = asyncio.get_running_loop().time() + max(timeout, 0)
    while True:
        if found.done():
            return found.result()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            return await asyncio.wait_for(
                asyncio.shield(found),
                timeout=min(0.1, remaining),
            )
        except asyncio.TimeoutError:
            await settle_guarded_click(
                page,
                pages_before_navigation,
                stable_url,
                0,
                reload_stable=True,
            )


async def wait_for_episode_grid_page(page: Any, expected_page: int, timeout: int = 5000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(150)
        state = await episode_grid_state(page)
        if state and state["current_page"] == expected_page:
            return True
    return False


async def discover_episode_urls_from_paginated_grid(
    page: Any,
    base_url: str,
    captured_values: list[str],
    per_click_wait: float,
) -> tuple[bool, list[str]]:
    """Traverse a flat All Episodes grid, using direct URLs before clicks."""
    state = await episode_grid_state(page)
    if not state:
        return False, []

    print(
        f"Found paginated All Episodes grid: page {state['current_page']} "
        f"of {state['total_pages']}.",
        file=sys.stderr,
    )
    query_urls = expand_numbered_episode_query_urls(base_url, state.get("total_items"))
    if query_urls:
        print(
            f"Constructed {len(query_urls)} query-based episode URL(s) from the "
            "advertised total; no grid clicks needed.",
            file=sys.stderr,
        )
        return True, query_urls

    while state["current_page"] > 1:
        expected_page = state["current_page"] - 1
        pages_before_click = list(page.context.pages)
        stable_url = page.url
        if not await click_episode_grid_control(page, "PREV"):
            print("Could not return the episode grid to page 1.", file=sys.stderr)
            break
        await settle_guarded_click(page, pages_before_click, stable_url, 0.1)
        if not await wait_for_episode_grid_page(page, expected_page):
            print("Episode-grid PREV navigation stalled.", file=sys.stderr)
            break
        state = await episode_grid_state(page) or state

    discovered: list[str] = []
    seen: set[str] = set()
    captured_cursor = 0
    visited_pages: set[int] = set()
    total_pages = min(int(state["total_pages"]), 1000)
    total_items = state.get("total_items")

    def merge(values: list[str]) -> None:
        for url in extract_episode_urls_from_values(values, base_url):
            if url not in seen:
                seen.add(url)
                discovered.append(url)

    while state and len(visited_pages) < total_pages:
        current_page = int(state["current_page"])
        if current_page in visited_pages:
            break
        visited_pages.add(current_page)
        print(
            f"Scanning episode grid page {current_page}/{state['total_pages']}...",
            file=sys.stderr,
        )

        page_values = [page.url, *captured_values[captured_cursor:], *await collect_page_values(page)]
        captured_cursor = len(captured_values)
        merge(page_values)
        represented = episode_numbers_in_urls(discovered)
        missing_numbers = [
            number for number in state["episode_numbers"] if number not in represented
        ]

        for episode_number in missing_numbers:
            pages_before_click = list(page.context.pages)
            stable_url = page.url
            if not await click_episode_grid_control(page, episode_number):
                print(f"Could not click episode {episode_number}; continuing.", file=sys.stderr)
                continue
            popup_values, _ = await settle_guarded_click(
                page,
                pages_before_click,
                stable_url,
                per_click_wait,
            )
            click_values = [page.url, *popup_values, *captured_values[captured_cursor:]]
            captured_cursor = len(captured_values)
            merge(click_values)
            if episode_number not in episode_numbers_in_urls(discovered):
                merge(await collect_page_values(page))

        if current_page >= state["total_pages"]:
            break
        expected_page = current_page + 1
        pages_before_click = list(page.context.pages)
        stable_url = page.url
        if not await click_episode_grid_control(page, "NEXT"):
            print("Episode-grid NEXT control was not clickable.", file=sys.stderr)
            break
        await settle_guarded_click(page, pages_before_click, stable_url, 0.1)
        if not await wait_for_episode_grid_page(page, expected_page):
            print("Episode-grid NEXT navigation stalled.", file=sys.stderr)
            break
        state = await episode_grid_state(page)

    if total_items and len(discovered) < int(total_items):
        print(
            f"Episode grid advertised {total_items} total, but {len(discovered)} "
            "downloadable episode URL(s) were resolved.",
            file=sys.stderr,
        )
    return True, discovered


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
        help=(
            "During discovery, traverse a paginated All Episodes grid or drive visible "
            "Season/Episode dropdowns and record resulting URLs."
        ),
    )
    parser.add_argument(
        "--episode-counts",
        help="Comma-separated per-season episode counts for dropdown discovery.",
    )
    parser.add_argument(
        "--dropdown-wait",
        type=float,
        default=0.75,
        help="Seconds to wait after each interactive episode click. Default: 0.75.",
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
        page = await reuse_or_create_context_page(context)
        pages_before_navigation = list(context.pages)

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
            await navigate_initial_page(page, args.url)
        except RuntimeError as exc:
            await context.close()
            stage = "Discovery" if args.discover_episode_urls else "Capture"
            print(f"{stage} navigation failed: {exc}", file=sys.stderr)
            return 4

        if (
            args.top_right_click_delay is not None
            and not args.discover_episode_urls
            and not found.done()
        ):
            await click_top_right(page, args.top_right_click_delay, args.top_right_click_offset)

        if args.discover_episode_urls:
            popup_values, _ = await settle_guarded_click(
                page,
                pages_before_navigation,
                args.url,
                max(args.discover_wait, 0),
            )
            discovery_values.extend(popup_values)
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
                    "skipping interactive traversal.",
                    file=sys.stderr,
                )
            elif args.discover_dropdowns:
                try:
                    grid_detected, grid_urls = await discover_episode_urls_from_paginated_grid(
                        page, args.url, discovery_values, args.dropdown_wait
                    )
                    seen = set(urls)
                    for grid_url in grid_urls:
                        if grid_url not in seen:
                            seen.add(grid_url)
                            urls.append(grid_url)
                    if grid_detected:
                        print(
                            f"Resolved {len(grid_urls)} episode URL(s) from the paginated grid; "
                            "skipping dropdown traversal.",
                            file=sys.stderr,
                        )
                    else:
                        dropdown_urls = await discover_episode_urls_from_dropdowns(
                            page,
                            args.url,
                            parse_episode_counts(args.episode_counts),
                            args.dropdown_wait,
                        )
                        for dropdown_url in dropdown_urls:
                            if dropdown_url not in seen:
                                seen.add(dropdown_url)
                                urls.append(dropdown_url)
                except Exception as exc:
                    print(f"Interactive episode discovery failed: {exc}", file=sys.stderr)
            print(f"Discovered {len(urls)} episode URL(s).", file=sys.stderr)
            for url in urls:
                print(url)
            await context.close()
            return 0 if urls else 3

        if args.visible:
            print(
                "Browser is open. Click through consent/login and start playback if needed.",
                file=sys.stderr,
            )

        try:
            url, method, headers, post_data = await wait_for_capture_with_recovery(
                page,
                found,
                pages_before_navigation,
                args.url,
                args.timeout,
            )
        except asyncio.TimeoutError:
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

        await context.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
