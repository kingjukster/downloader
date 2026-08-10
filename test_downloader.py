from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
import unittest
from unittest import mock

import capture_m3u8_curl as capture
import download_from_captured_m3u8 as downloader


def make_args(**overrides):
    values = {
        "auto_expand": None,
        "seasons": None,
        "episodes": None,
        "episode_counts": None,
        "urls": ["https://media.example/show"],
        "url_file": [],
        "catalog_server": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class AutomaticExpansionTests(unittest.TestCase):
    @mock.patch.object(downloader.subprocess, "call", return_value=0)
    def test_ffmpeg_allows_extensionless_hls_segments_before_input(self, call):
        with tempfile.TemporaryDirectory() as directory:
            output = downloader.Path(directory) / "episode.mp4"

            result = downloader.run_ffmpeg(
                "ffmpeg",
                "https://cdn.example/video/master.m3u8",
                ["referer: https://media.example/"],
                output,
                None,
                False,
            )

        self.assertEqual(result, 0)
        command = call.call_args.args[0]
        option_index = command.index("-allowed_segment_extensions")
        picky_index = command.index("-extension_picky")
        input_index = command.index("-i")
        self.assertEqual(command[option_index + 1], "ALL")
        self.assertEqual(command[picky_index + 1], "0")
        self.assertLess(option_index, input_index)
        self.assertLess(picky_index, input_index)

    def test_movie_output_uses_readable_title_and_year(self):
        self.assertEqual(
            downloader.default_output_path(
                "https://media.example/movies/play/37287335-obsession-2026"
            ).name,
            "Obsession (2026).mp4",
        )

    def test_episode_output_uses_padded_season_and_episode(self):
        self.assertEqual(
            downloader.default_output_path(
                "https://media.example/shows/play/123-star-trek-strange-new-worlds-2022#S3-E7-99"
            ).name,
            "S03E07 - Star Trek Strange New Worlds (2022).mp4",
        )

    def test_query_episode_output_uses_flat_season_numbering(self):
        self.assertEqual(
            downloader.default_output_path(
                "https://media.example/anime/1735?play=true&episode=23"
            ).name,
            "S01E23 - 1735.mp4",
        )

    def test_legacy_filename_cleanup_uses_season_first_and_preserves_clean_names(self):
        self.assertEqual(
            downloader.clean_media_filename(
                "lookmovie2.to_shows_play_123-example-show-2024_s2-e9.mp4"
            ),
            "S02E09 - Example Show (2024).mp4",
        )
        self.assertEqual(
            downloader.clean_media_filename("Example Show (2024) - S02E09.mp4"),
            "S02E09 - Example Show (2024).mp4",
        )
        self.assertIsNone(downloader.clean_media_filename("S02E09 - Example Show (2024).mp4"))

    def test_title_casing_keeps_internal_minor_words_lowercase(self):
        self.assertEqual(
            downloader.clean_media_filename(
                "lookmovie2.to_shows_play_123-stuart-fails-to-save-the-universe-2026_s1-e1.mp4"
            ),
            "S01E01 - Stuart Fails to Save the Universe (2026).mp4",
        )

    def test_one_direct_url_enables_automatic_expansion(self):
        self.assertTrue(
            downloader.should_auto_expand(
                make_args(),
                ["https://media.example/show"],
            )
        )

    def test_multiple_urls_keep_existing_queue_behavior(self):
        urls = ["https://media.example/one", "https://media.example/two"]
        self.assertFalse(downloader.should_auto_expand(make_args(urls=urls), urls))

    def test_manual_expansion_disables_automatic_expansion(self):
        self.assertFalse(
            downloader.should_auto_expand(
                make_args(seasons=3, episodes=8),
                ["https://media.example/show#S1-E1-100"],
            )
        )

    def test_explicit_setting_wins(self):
        urls = ["https://media.example/one", "https://media.example/two"]
        self.assertTrue(downloader.should_auto_expand(make_args(auto_expand=True, urls=urls), urls))
        self.assertFalse(
            downloader.should_auto_expand(
                make_args(auto_expand=False),
                ["https://media.example/show"],
            )
        )

    def test_episode_urls_sort_by_season_and_episode(self):
        urls = [
            "https://media.example/show#S2-E1-300",
            "https://media.example/show#S1-E10-200",
            "https://media.example/show#S1-E2-100",
        ]
        self.assertEqual(
            sorted(urls, key=downloader.episode_url_sort_key),
            [urls[2], urls[1], urls[0]],
        )

    def test_query_episode_urls_sort_numerically(self):
        urls = [
            "https://media.example/anime/1735?play=true&episode=10",
            "https://media.example/anime/1735?play=true&episode=2",
            "https://media.example/anime/1735?play=true&episode=1",
        ]
        self.assertEqual(
            sorted(urls, key=downloader.episode_url_sort_key),
            [urls[2], urls[1], urls[0]],
        )

    @mock.patch.object(downloader.subprocess, "run")
    def test_discovery_command_omits_legacy_top_right_click(self, run):
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        args = argparse.Namespace(
            capture_script=downloader.DEFAULT_CAPTURE_SCRIPT,
            discover_wait=5,
            discover_debug_output=None,
            discover_dropdowns=True,
            episode_counts=None,
            dropdown_wait=0.75,
            visible=True,
            user_data_dir=downloader.Path("work/profile"),
            browser_executable=downloader.DEFAULT_OPERA_AIR,
            extension_dir=[],
            top_right_click_delay=15,
            top_right_click_offset=24,
        )

        downloader.run_discovery(
            args,
            "https://media.example/anime/1735?play=true&episode=23",
        )

        command = run.call_args.args[0]
        self.assertNotIn("--top-right-click-delay", command)
        self.assertNotIn("--top-right-click-offset", command)

    @mock.patch.object(downloader, "discover_series_urls", return_value=[])
    def test_movie_falls_back_to_supplied_link(self, discover):
        args = make_args(discover_dropdowns=None)
        seed = ["https://media.example/movie"]
        self.assertEqual(downloader.auto_expand_title(args, seed), seed)
        self.assertTrue(discover.call_args.args[0].discover_dropdowns)

    @mock.patch.object(
        downloader,
        "discover_series_urls",
        return_value=["https://media.example/show#S1-E1-100"],
    )
    def test_show_uses_discovered_episode_queue(self, discover):
        args = make_args(discover_dropdowns=False)
        self.assertEqual(
            downloader.auto_expand_title(args, ["https://media.example/show"]),
            ["https://media.example/show#S1-E1-100"],
        )
        self.assertFalse(discover.call_args.args[0].discover_dropdowns)

    def test_skip_existing_urls_keeps_only_missing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            args = make_args(output=downloader.Path(directory))
            urls = [
                "https://media.example/show#S1-E1-101",
                "https://media.example/show#S1-E2-102",
            ]
            existing = downloader.intended_output_path(args, urls[0], multiple=True)
            existing.write_bytes(b"complete media")
            self.assertEqual(
                downloader.skip_existing_urls(args, urls, multiple=True),
                [urls[1]],
            )


class EpisodeExtractionTests(unittest.TestCase):
    def test_extracts_absolute_relative_encoded_and_fragment_urls(self):
        base = "https://media.example/shows/title#S1-E1-100"
        values = [
            "https://media.example/shows/title#S2-E3-900",
            'href="/shows/title#S1-E2-200"',
            "#S1-E1-100",
            "https%3A%2F%2Fmedia.example%2Fshows%2Ftitle%23S3-E4-1200",
        ]
        self.assertEqual(
            capture.extract_episode_urls_from_values(values, base),
            [
                "https://media.example/shows/title#S2-E3-900",
                "https://media.example/shows/title#S1-E2-200",
                "https://media.example/shows/title#S1-E1-100",
                "https://media.example/shows/title#S3-E4-1200",
            ],
        )

    def test_extracts_same_page_query_episode_urls(self):
        base = "https://media.example/anime/1735?play=true&episode=23"
        values = [
            "https://media.example/anime/1735?play=true&episode=100",
            "/anime/1735?play=true&episode=2",
            "?play=true&episode=1",
            "https://ads.example/anime/1735?play=true&episode=9",
            "https://media.example/anime/other?play=true&episode=8",
        ]
        self.assertEqual(
            capture.extract_episode_urls_from_values(values, base),
            [
                "https://media.example/anime/1735?play=true&episode=100",
                "https://media.example/anime/1735?play=true&episode=2",
                "https://media.example/anime/1735?play=true&episode=1",
            ],
        )

    def test_large_nonmatching_payload_is_scanned_quickly(self):
        started = time.perf_counter()
        self.assertEqual(
            capture.extract_episode_urls_from_values(
                ["x" * 500_000],
                "https://media.example/show",
            ),
            [],
        )
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_extracts_complete_queue_from_structured_season_data(self):
        value = (
            r'''window.seasons='{\"1\":{\"episodes\":{'''
            r'''\"1\":{\"episode_number\":\"1\",\"id_episode\":\"101\"},'''
            r'''\"2\":{\"episode_number\":\"2\",\"id_episode\":\"102\"}},'''
            r'''\"season\":\"1\"},\"2\":{\"episodes\":{'''
            r'''\"1\":{\"episode_number\":\"1\",\"id_episode\":\"201\"}},'''
            r'''\"season\":\"2\"}}';'''
        )
        self.assertEqual(
            capture.extract_episode_urls_from_season_data(
                [value],
                "https://media.example/show#S1-E1-101",
            ),
            [
                "https://media.example/show#S1-E1-101",
                "https://media.example/show#S1-E2-102",
                "https://media.example/show#S2-E1-201",
            ],
        )

    def test_parses_paginated_all_episodes_summary(self):
        self.assertEqual(
            capture.parse_episode_grid_summary("All Episodes Page 1 of 5 • 500 total"),
            (1, 5, 500),
        )
        self.assertEqual(
            capture.parse_episode_grid_summary("PAGE 3 OF 8"),
            (3, 8, None),
        )
        self.assertIsNone(capture.parse_episode_grid_summary("Season 1 Episode 5"))

    def test_finds_episode_numbers_already_resolved_from_grid(self):
        self.assertEqual(
            capture.episode_numbers_in_urls(
                [
                    "https://media.example/show#S1-E1-101",
                    "https://media.example/show#S1-E100-200",
                    "https://media.example/movie",
                ]
            ),
            {1, 100},
        )

    def test_expands_numbered_episode_query_without_losing_other_parameters(self):
        self.assertEqual(
            capture.expand_numbered_episode_query_urls(
                "https://media.example/anime/1735?play=true&episode=23#player",
                3,
            ),
            [
                "https://media.example/anime/1735?play=true&episode=1#player",
                "https://media.example/anime/1735?play=true&episode=2#player",
                "https://media.example/anime/1735?play=true&episode=3#player",
            ],
        )

    def test_does_not_invent_query_episode_urls_without_both_proofs(self):
        self.assertEqual(
            capture.expand_numbered_episode_query_urls(
                "https://media.example/anime/1735?play=true",
                500,
            ),
            [],
        )
        self.assertEqual(
            capture.expand_numbered_episode_query_urls(
                "https://media.example/anime/1735?episode=23",
                None,
            ),
            [],
        )

    def test_episode_grid_ignores_separate_pagination_counters(self):
        self.assertEqual(
            capture.largest_contiguous_number_run([2, 5, *range(101, 201)]),
            list(range(101, 201)),
        )

    def test_detects_blank_and_cross_origin_click_diversions(self):
        stable = "https://media.example/show#S1-E1-100"
        self.assertTrue(capture.unexpected_interactive_navigation("about:blank", stable))
        self.assertTrue(
            capture.unexpected_interactive_navigation("https://ads.example/pop", stable)
        )
        self.assertFalse(
            capture.unexpected_interactive_navigation(
                "https://media.example/show#S1-E2-101",
                stable,
            )
        )


class PaginatedEpisodeGridTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_discovery_does_not_auto_scroll(self):
        page = mock.Mock()
        page.wait_for_timeout = mock.AsyncMock()
        with (
            mock.patch.object(
                capture,
                "auto_scroll_page",
                new=mock.AsyncMock(),
            ) as auto_scroll,
            mock.patch.object(
                capture,
                "collect_page_values",
                new=mock.AsyncMock(
                    return_value=["https://media.example/anime/1735?episode=23"]
                ),
            ),
        ):
            urls = await capture.discover_episode_urls(
                page,
                "https://media.example/anime/1735?episode=23",
                5,
                [],
            )

        auto_scroll.assert_not_awaited()
        page.wait_for_timeout.assert_awaited_once_with(5000)
        self.assertEqual(urls, ["https://media.example/anime/1735?episode=23"])

    async def test_accepts_grid_state_from_generic_all_episodes_marker(self):
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(
            return_value={
                "summary": "All Episodes Page 1 of 5 - 500 total",
                "episode_numbers": list(range(1, 101)),
            }
        )

        state = await capture.episode_grid_state(page)

        self.assertEqual(state["current_page"], 1)
        self.assertEqual(state["total_pages"], 5)
        self.assertEqual(state["total_items"], 500)
        self.assertEqual(state["episode_numbers"], list(range(1, 101)))

    async def test_grid_click_climbs_past_summary_only_header(self):
        page = mock.Mock()

        async def evaluate(script, argument):
            self.assertIn("numericCount >= 2", script)
            self.assertEqual(argument, {"label": "NEXT"})
            return True

        page.evaluate = mock.AsyncMock(side_effect=evaluate)

        clicked = await capture.click_episode_grid_control(page, "NEXT")

        self.assertTrue(clicked)
        page.evaluate.assert_awaited_once()

    async def test_query_grid_uses_advertised_total_without_clicking(self):
        page = mock.Mock(url="https://media.example/anime/1735?play=true&episode=23")
        page.context = mock.Mock(pages=[page])
        state = {
            "current_page": 1,
            "total_pages": 5,
            "total_items": 500,
            "episode_numbers": list(range(1, 101)),
        }

        with (
            mock.patch.object(
                capture,
                "episode_grid_state",
                new=mock.AsyncMock(return_value=state),
            ),
            mock.patch.object(
                capture,
                "click_episode_grid_control",
                new=mock.AsyncMock(),
            ) as click_control,
        ):
            detected, urls = await capture.discover_episode_urls_from_paginated_grid(
                page,
                page.url,
                [],
                0.75,
            )

        self.assertTrue(detected)
        self.assertEqual(len(urls), 500)
        self.assertEqual(urls[0], "https://media.example/anime/1735?play=true&episode=1")
        self.assertEqual(urls[-1], "https://media.example/anime/1735?play=true&episode=500")
        click_control.assert_not_awaited()

    async def test_scans_pages_and_clicks_only_unresolved_episode_numbers(self):
        page = mock.Mock(url="https://media.example/show")
        page.wait_for_timeout = mock.AsyncMock()
        page.context = mock.Mock(pages=[page])
        states = [
            {
                "current_page": 1,
                "total_pages": 2,
                "total_items": 4,
                "episode_numbers": [1, 2],
            },
            {
                "current_page": 2,
                "total_pages": 2,
                "total_items": 4,
                "episode_numbers": [3, 4],
            },
        ]
        collected_values = [
            ["https://media.example/show#S1-E1-101"],
            ["https://media.example/show#S1-E2-102"],
            [
                "https://media.example/show#S1-E3-103",
                "https://media.example/show#S1-E4-104",
            ],
        ]

        with (
            mock.patch.object(
                capture,
                "episode_grid_state",
                new=mock.AsyncMock(side_effect=states),
            ),
            mock.patch.object(
                capture,
                "click_episode_grid_control",
                new=mock.AsyncMock(return_value=True),
            ) as click_control,
            mock.patch.object(
                capture,
                "wait_for_episode_grid_page",
                new=mock.AsyncMock(return_value=True),
            ),
            mock.patch.object(
                capture,
                "settle_guarded_click",
                new=mock.AsyncMock(return_value=([], False)),
            ),
            mock.patch.object(
                capture,
                "collect_page_values",
                new=mock.AsyncMock(side_effect=collected_values),
            ),
        ):
            detected, urls = await capture.discover_episode_urls_from_paginated_grid(
                page,
                "https://media.example/show",
                [],
                0,
            )

        self.assertTrue(detected)
        self.assertEqual(
            urls,
            [
                "https://media.example/show#S1-E1-101",
                "https://media.example/show#S1-E2-102",
                "https://media.example/show#S1-E3-103",
                "https://media.example/show#S1-E4-104",
            ],
        )
        self.assertEqual(
            [call.args[1] for call in click_control.await_args_list],
            [2, "NEXT"],
        )

    async def test_reports_when_no_episode_grid_is_present(self):
        page = mock.Mock(url="https://media.example/movie")
        with mock.patch.object(
            capture,
            "episode_grid_state",
            new=mock.AsyncMock(return_value=None),
        ):
            detected, urls = await capture.discover_episode_urls_from_paginated_grid(
                page,
                page.url,
                [],
                0,
            )
        self.assertFalse(detected)
        self.assertEqual(urls, [])


class BrowserLaunchTests(unittest.TestCase):
    def test_automation_browser_is_muted(self):
        self.assertIn("--mute-audio", capture.default_browser_args())

    def test_capture_defaults_match_any_hls_playlist_name(self):
        self.assertEqual(capture.DEFAULT_PLAYLIST_MATCH, ".m3u8")
        self.assertEqual(downloader.DEFAULT_PLAYLIST_MATCH, ".m3u8")

    def test_playlist_match_ignores_encoded_url_inside_analytics_query(self):
        analytics_url = (
            "https://analytics.example/ping.gif?"
            "mu=https%3A%2F%2Fcdn.example%2Fvideo%2Fmaster.m3u8"
        )
        self.assertFalse(capture.request_url_matches_playlist(analytics_url, ".m3u8"))
        self.assertTrue(
            capture.request_url_matches_playlist(
                "https://cdn.example/video/master.m3u8?token=abc",
                ".m3u8",
            )
        )


class BrowserPageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_request_rechecks_future_after_headers(self):
        found = asyncio.get_running_loop().create_future()
        winner = ("https://cdn.example/first.m3u8", "GET", {}, None)
        request = mock.Mock(
            url="https://cdn.example/second.m3u8",
            method="GET",
            post_data=None,
        )

        async def headers():
            found.set_result(winner)
            return {"referer": "https://media.example/"}

        request.all_headers = mock.AsyncMock(side_effect=headers)

        captured = await capture.capture_request_if_matching(request, found, ".m3u8")

        self.assertFalse(captured)
        self.assertEqual(found.result(), winner)

    async def test_matching_playlist_request_resolves_capture(self):
        found = asyncio.get_running_loop().create_future()
        request = mock.Mock(
            url="https://cdn.example/video/master.m3u8?token=abc",
            method="GET",
            post_data=None,
        )
        request.all_headers = mock.AsyncMock(
            return_value={"referer": "https://media.example/"}
        )

        captured = await capture.capture_request_if_matching(request, found, ".m3u8")

        self.assertTrue(captured)
        self.assertEqual(found.result()[0], request.url)

    async def test_reuses_initial_page_and_closes_only_extra_tabs(self):
        primary = mock.Mock()
        primary.is_closed.return_value = False
        primary.close = mock.AsyncMock()
        extra = mock.Mock()
        extra.is_closed.return_value = False
        extra.close = mock.AsyncMock()
        context = mock.Mock(pages=[primary, extra])
        context.new_page = mock.AsyncMock()

        selected = await capture.reuse_or_create_context_page(context)

        self.assertIs(selected, primary)
        primary.close.assert_not_awaited()
        extra.close.assert_awaited_once_with()
        context.new_page.assert_not_awaited()

    async def test_creates_page_when_context_has_no_open_tab(self):
        created = mock.Mock()
        context = mock.Mock(pages=[])
        context.new_page = mock.AsyncMock(return_value=created)

        selected = await capture.reuse_or_create_context_page(context)

        self.assertIs(selected, created)
        context.new_page.assert_awaited_once_with()

    async def test_initial_diversion_is_retried_once_for_capture_too(self):
        target = "https://media.example/anime/1735?episode=23"
        page = mock.Mock(url="about:blank")
        attempts = 0

        async def navigate(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            page.url = "about:blank" if attempts == 1 else target

        page.goto = mock.AsyncMock(side_effect=navigate)

        await capture.navigate_initial_page(page, target)

        self.assertEqual(page.goto.await_count, 2)

    async def test_repeated_initial_diversion_fails_closed(self):
        target = "https://media.example/anime/1735?episode=23"
        page = mock.Mock(url="about:blank")
        page.goto = mock.AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "repeatedly diverted"):
            await capture.navigate_initial_page(page, target)

        self.assertEqual(page.goto.await_count, 2)

    async def test_capture_wait_recovers_late_diversion_before_request(self):
        page = mock.Mock(url="about:blank")
        found = asyncio.get_running_loop().create_future()
        captured_request = ("https://cdn.example/index.m3u8", "GET", {}, None)

        async def recover(*args, **kwargs):
            page.url = "https://media.example/anime/1735?episode=23"
            if not found.done():
                found.set_result(captured_request)
            return [], True

        with mock.patch.object(
            capture,
            "settle_guarded_click",
            new=mock.AsyncMock(side_effect=recover),
        ) as settle:
            result = await capture.wait_for_capture_with_recovery(
                page,
                found,
                [page],
                "https://media.example/anime/1735?episode=23",
                1,
            )

        self.assertEqual(result, captured_request)
        settle.assert_awaited()

    async def test_browser_level_popup_containment_does_not_modify_site_javascript(self):
        page = mock.Mock(url="https://media.example/show")
        page.is_closed.return_value = False
        page.wait_for_timeout = mock.AsyncMock()
        page.evaluate = mock.AsyncMock()
        popup = mock.Mock(url="https://ads.example/pop")
        popup.is_closed.return_value = False
        popup.close = mock.AsyncMock()
        page.context = mock.Mock(pages=[page, popup])

        popup_values, recovered = await capture.settle_guarded_click(
            page,
            [page],
            page.url,
            0,
        )

        self.assertEqual(popup_values, ["https://ads.example/pop"])
        self.assertFalse(recovered)
        popup.close.assert_awaited_once_with()
        page.evaluate.assert_not_awaited()

    async def test_capture_recovery_uses_back_before_reload(self):
        stable = "https://media.example/anime/1735?episode=23"
        page = mock.Mock(url="about:blank")
        page.is_closed.return_value = False
        page.wait_for_timeout = mock.AsyncMock()

        async def go_back(**kwargs):
            page.url = stable

        page.go_back = mock.AsyncMock(side_effect=go_back)
        page.goto = mock.AsyncMock()
        page.context = mock.Mock(pages=[page])

        _, recovered = await capture.settle_guarded_click(
            page,
            [page],
            stable,
            0,
        )

        self.assertTrue(recovered)
        page.go_back.assert_awaited_once_with(
            wait_until="domcontentloaded",
            timeout=10_000,
        )
        page.goto.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
