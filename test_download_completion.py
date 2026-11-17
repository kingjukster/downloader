"""Completion regressions, including real generated media without network access."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import download_from_captured_m3u8 as downloader


class CompletionTests(unittest.TestCase):
    def test_zero_exit_without_final_progress_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "episode.mp4"
            with mock.patch.object(downloader, "ffprobe_for_ffmpeg", return_value="ffprobe"), \
                 mock.patch.object(downloader, "media_metadata", return_value={"format": {"duration": "3"}}), \
                 mock.patch.object(downloader.subprocess, "run", return_value=mock.Mock(
                     returncode=0, stdout="progress=continue\n")):
                with self.assertRaisesRegex(RuntimeError, "without completing"):
                    downloader.run_ffmpeg("ffmpeg", "source", [], output, None, False)
            self.assertFalse(output.exists())

    def test_zero_exit_with_invalid_media_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "episode.mp4"
            with mock.patch.object(downloader, "ffprobe_for_ffmpeg", return_value="ffprobe"), \
                 mock.patch.object(downloader, "media_metadata", return_value={"format": {"duration": "3"}}), \
                 mock.patch.object(downloader.subprocess, "run", return_value=mock.Mock(
                     returncode=0, stdout="progress=end\n")):
                with self.assertRaisesRegex(RuntimeError, "validation failed"):
                    downloader.run_ffmpeg("ffmpeg", "source", [], output, None, False)
            self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
    def test_real_media_completion_truncation_and_existing_files(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.m3u8"
            subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                            "testsrc2=size=160x90:rate=10", "-t", "3", "-c:v", "libx264",
                            "-hls_time", "1", "-hls_list_size", "0",
                            str(source)], check=True)
            output = root / "complete.mp4"
            url = "https://example.com/movie"
            self.assertEqual(downloader.run_ffmpeg(ffmpeg, str(source), [], output, None, False, page_url=url), 0)
            self.assertTrue(downloader.validate_media_file(ffprobe, output, 3)[0])
            self.assertFalse(downloader.validate_media_file(ffprobe, output, 26)[0])
            broken = root / "broken.mp4"
            broken.write_bytes(output.read_bytes()[:1000])
            self.assertFalse(downloader.validate_media_file(ffprobe, broken)[0])
            args = argparse.Namespace(output=broken)
            url = "https://example.com/movie"
            self.assertEqual(downloader.skip_existing_urls(args, [url], False), [url])
            self.assertEqual(broken.stat().st_size, 1000)
            args.output = output
            self.assertEqual(downloader.skip_existing_urls(args, [url], False), [])


if __name__ == "__main__":
    unittest.main()
