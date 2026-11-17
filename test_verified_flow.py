"""Offline acceptance for folder routing and fail-closed completion."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import download_from_captured_m3u8 as d


class VerifiedFlowTests(unittest.TestCase):
    def test_show_folders_and_explicit_file_override(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(output=Path(directory))
            url = 'https://example.com/shows/play/123-example-show-2024#S2-E3-99'
            path = d.intended_output_path(args, url, False)
            self.assertEqual(path.relative_to(directory).parts,
                             ('Example Show (2024)', 'S02E03 - Example Show (2024).mp4'))
            args.output = Path(directory) / 'explicit.mp4'
            self.assertEqual(d.intended_output_path(args, url, False), args.output)
            self.assertEqual(len(d.relative_output_path('https://example.com/movie').parts), 1)

    def test_missing_expected_episode_rejects_queue_before_capture(self):
        result = subprocess.run([sys.executable, str(Path(d.__file__)),
                                 'https://example.com/show#S1-E1-1', '--no-auto-expand',
                                 '--expected-count', '2', '--dry-run-urls'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected 2 unique items, but resolved 1', result.stderr)

    def test_unknown_duration_cannot_report_complete(self):
        with mock.patch.object(d, 'media_metadata', return_value={}), \
             mock.patch.object(d, 'ffprobe_for_ffmpeg', return_value='ffprobe'):
            with self.assertRaisesRegex(RuntimeError, 'without a source duration'):
                d.run_ffmpeg('ffmpeg', 'source', [], Path('unused.mp4'), None, False)

    def test_worker_zero_without_verified_file_fails_whole_queue(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(sys, 'argv', ['downloader', 'https://example.com/show#S1-E1-1',
                                           '--no-auto-expand', '--output', directory]), \
             mock.patch.object(d, 'download_one', return_value=0), \
             mock.patch.object(d.shutil, 'which', return_value='ffmpeg'):
            self.assertEqual(d.main(), 1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_audio_video_receipt_skip_tampering_and_decode_failure(self):
        ffmpeg = shutil.which('ffmpeg')
        url = 'https://example.com/show#S1-E1-1'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.m3u8'
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10',
                            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '3',
                            '-c:v', 'libx264', '-c:a', 'aac', '-hls_time', '1',
                            '-hls_list_size', '0', str(source)], check=True)
            args = argparse.Namespace(output=root, overwrite=False)
            output = d.resolve_output_path(args, url, False)
            self.assertEqual(d.run_ffmpeg(ffmpeg, str(source), [], output, None, False, page_url=url), 0)
            self.assertEqual(d.skip_existing_urls(args, [url], False), [])
            receipt = d.verification_path(output)
            saved = receipt.read_text()
            data = json.loads(saved)
            data['expected_seconds'] = 30
            receipt.write_text(json.dumps(data))
            self.assertFalse(d.verify_existing_output(ffmpeg, output, url, None)[0])
            receipt.write_text(saved)
            with mock.patch.object(d, 'validate_full_decode', side_effect=ValueError('decode failed')):
                self.assertEqual(d.skip_existing_urls(args, [url], False), [url])
                failed = root / 'failed.mp4'
                with self.assertRaisesRegex(ValueError, 'decode failed'):
                    d.run_ffmpeg(ffmpeg, str(source), [], failed, None, False, page_url=url)
                self.assertFalse(failed.exists())
                self.assertFalse(d.verification_path(failed).exists())
            output.write_bytes(output.read_bytes()[:-100])
            self.assertFalse(d.verify_existing_output(ffmpeg, output, url, None)[0])
            receipt.unlink()
            self.assertEqual(d.skip_existing_urls(args, [url], False), [url])

            # Exercise the actual queue, remux, decode, receipt and resume paths;
            # only browser capture is replaced with our generated local source.
            queue = ['https://example.com/first-show-2024#S1-E1-11',
                     'https://example.com/second-show-2025#S1-E2-22']
            argv = ['downloader', *queue, '--no-auto-expand', '--expected-count', '2',
                    '--skip-existing', '--output', str(root / 'batch')]
            captured = d.CAPTURED_PLAYLIST_PREFIX + json.dumps({'url': 'https://example.com/source.m3u8', 'headers': {}})
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(d, 'run_capture', return_value=captured), \
                 mock.patch.object(d, 'select_playlist_candidate', return_value=(str(source), [])):
                self.assertEqual(d.main(), 0)
            self.assertEqual(len(list((root / 'batch').glob('*/*.mp4'))), 2)
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(d, 'run_capture') as capture:
                self.assertEqual(d.main(), 0)
                capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
