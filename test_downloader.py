from __future__ import annotations

import argparse
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

    def test_episode_grid_ignores_separate_pagination_counters(self):
        self.assertEqual(
            capture.largest_contiguous_number_run([2, 5, *range(101, 201)]),
            list(range(101, 201)),
        )


class PaginatedEpisodeGridTests(unittest.IsolatedAsyncioTestCase):
    async def test_scans_pages_and_clicks_only_unresolved_episode_numbers(self):
        page = mock.Mock(url="https://media.example/show")
        page.wait_for_timeout = mock.AsyncMock()
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


if __name__ == "__main__":
    unittest.main()
