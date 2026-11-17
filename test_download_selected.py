import argparse
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import download_selected as selected


EPISODE = "https://www.lookmovie2.to/shows/play/1690530147-dexter-2006#S2-E2-46020"


class SelectedFlowTests(unittest.TestCase):
    def test_preview_resolves_exact_episode_without_browser_or_expansion(self):
        result = subprocess.run([sys.executable, str(Path(selected.__file__)), EPISODE],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), EPISODE)
        self.assertIn("1 URL(s) queued", result.stderr)

    def test_execute_keeps_selection_and_disables_blind_click(self):
        with patch.object(selected.subprocess, "run") as run:
            run.return_value.returncode = 7
            self.assertEqual(selected.main([EPISODE, "--execute"]), 7)
        command = run.call_args.args[0]
        self.assertEqual(command[2], EPISODE)
        self.assertIn("--no-auto-expand", command)
        self.assertIn("--no-top-right-click", command)
        self.assertNotIn("--dry-run-urls", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_rejects_incomplete_selection_and_credentials(self):
        for url in [EPISODE.split("#")[0], EPISODE.replace("-46020", ""),
                    "https://www.lookmovie2.to/shows/view/dexter", "file:///tmp/video",
                    "https://@example.com/video", "https://example.com:bad/video"]:
            with self.subTest(url=url), self.assertRaises(argparse.ArgumentTypeError):
                selected.selected_url(url)


if __name__ == "__main__":
    unittest.main()
