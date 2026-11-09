"""Data preservation regressions for legacy filename cleanup."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import rename_downloaded_media as rename_tool


class RenameCollisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def paths(self, episode):
        source = self.root / f'Example Show (2024) - S02E{episode:02d}.mp4'
        destination = self.root / f'S02E{episode:02d} - Example Show (2024).mp4'
        source.write_bytes(f'source {episode}'.encode())
        return source, destination

    def run_cli(self, *options):
        log = self.root / 'rename-log.json'
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['rename_downloaded_media.py', str(self.root), *options, '--log', str(log)]):
            with contextlib.redirect_stdout(output):
                self.assertEqual(rename_tool.main(), 0)
        return json.loads(log.read_text(encoding='utf-8')), output.getvalue()

    def test_regular_destination_is_skipped_and_preserved(self):
        source, destination = self.paths(9)
        destination.write_bytes(b'occupied')
        report, _ = self.run_cli('--apply')
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertEqual(destination.read_bytes(), b'occupied')
        self.assertEqual(report['renamed_count'], 0)
        self.assertEqual(report['skipped_count'], 1)
        self.assertIn('destination exists', report['skipped'][0]['reason'])

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires optional Windows privileges')
    def test_dangling_destination_is_not_available(self):
        source, destination = self.paths(9)
        destination.symlink_to('missing-target')
        plan, skipped = rename_tool.build_plan([self.root])
        self.assertEqual(plan, [])
        self.assertEqual(len(skipped), 1)
        report, _ = self.run_cli('--apply')
        self.assertEqual(report['skipped_count'], 1)
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), 'missing-target')

    def test_destination_claimed_after_planning_is_skipped(self):
        source, destination = self.paths(9)
        real_rename = rename_tool.rename_noreplace

        def competing_rename(src, dst):
            Path(dst).write_bytes(b'other process')
            return real_rename(src, dst)

        with mock.patch.object(rename_tool, 'rename_noreplace', side_effect=competing_rename):
            report, _ = self.run_cli('--apply')
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertEqual(destination.read_bytes(), b'other process')
        self.assertEqual(report['planned_count'], 1)
        self.assertEqual(report['renamed_count'], 0)
        self.assertEqual(report['skipped_count'], 1)
        self.assertEqual(report['changes'], [])

    def test_directory_claimed_after_planning_is_skipped_and_other_rename_continues(self):
        blocked_source, blocked_destination = self.paths(9)
        good_source, good_destination = self.paths(10)
        real_rename = rename_tool.rename_noreplace

        def competing_rename(src, dst):
            if src == blocked_source:
                Path(dst).mkdir()
                (Path(dst) / 'other-process.txt').write_bytes(b'occupied')
            return real_rename(src, dst)

        with mock.patch.object(rename_tool, 'rename_noreplace', side_effect=competing_rename):
            report, _ = self.run_cli('--apply')
        self.assertEqual(blocked_source.read_bytes(), b'source 9')
        self.assertEqual((blocked_destination / 'other-process.txt').read_bytes(), b'occupied')
        self.assertFalse(good_source.exists())
        self.assertEqual(good_destination.read_bytes(), b'source 10')
        self.assertEqual(report['planned_count'], 2)
        self.assertEqual(report['renamed_count'], 1)
        self.assertEqual(report['skipped_count'], 1)
        self.assertIn('destination exists', report['skipped'][0]['reason'])
        self.assertEqual(report['changes'], [{'from': str(good_source), 'to': str(good_destination)}])

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires optional Windows privileges')
    def test_dangling_destination_claimed_after_planning_is_skipped(self):
        source, destination = self.paths(9)
        real_rename = rename_tool.rename_noreplace

        def competing_rename(src, dst):
            Path(dst).symlink_to('missing-target')
            return real_rename(src, dst)

        with mock.patch.object(rename_tool, 'rename_noreplace', side_effect=competing_rename):
            report, _ = self.run_cli('--apply')
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), 'missing-target')
        self.assertEqual(report['renamed_count'], 0)
        self.assertEqual(report['skipped_count'], 1)
        self.assertIn('destination exists', report['skipped'][0]['reason'])

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires optional Windows privileges')
    def test_source_symlink_moves_without_changing_its_target(self):
        source, destination = self.paths(9)
        source.unlink()
        target = self.root / 'target.bin'
        target.write_bytes(b'target data')
        source.symlink_to(target.name)

        report, _ = self.run_cli('--apply')
        self.assertEqual(report['renamed_count'], 1)
        self.assertFalse(source.is_symlink())
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), target.name)
        self.assertEqual(target.read_bytes(), b'target data')

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires optional Windows privileges')
    def test_dangling_source_symlink_moves_without_following(self):
        source, destination = self.paths(9)
        source.unlink()
        source.symlink_to('missing-target')

        report, _ = self.run_cli('--apply')
        self.assertEqual(report['renamed_count'], 1)
        self.assertFalse(source.is_symlink())
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), 'missing-target')

    def test_normal_rename_does_not_require_hard_links(self):
        source, destination = self.paths(9)
        with mock.patch.object(rename_tool.os, 'link', side_effect=OSError(errno.ENOTSUP, 'Hard links unavailable')):
            report, _ = self.run_cli('--apply')
        self.assertEqual(report['renamed_count'], 1)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b'source 9')

    def test_macos_exclusive_rename_reports_an_occupied_destination(self):
        source, destination = self.paths(9)
        native_rename = mock.Mock(return_value=-1)
        library = mock.Mock(renamex_np=native_rename)
        with mock.patch.object(rename_tool.sys, 'platform', 'darwin'):
            with mock.patch.object(rename_tool.os, 'name', 'posix'):
                with mock.patch.object(rename_tool.ctypes, 'CDLL', return_value=library):
                    with mock.patch.object(rename_tool.ctypes, 'get_errno', return_value=errno.EEXIST):
                        with self.assertRaises(FileExistsError):
                            rename_tool.rename_noreplace(source, destination)
        native_rename.assert_called_once_with(
            os.fsencode(source), os.fsencode(destination), 0x00000004
        )
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertFalse(destination.exists())

    def test_collision_does_not_stop_independent_rename(self):
        blocked_source, blocked_destination = self.paths(9)
        good_source, good_destination = self.paths(10)
        blocked_destination.write_bytes(b'occupied')
        report, _ = self.run_cli('--apply')
        self.assertEqual(blocked_source.read_bytes(), b'source 9')
        self.assertEqual(blocked_destination.read_bytes(), b'occupied')
        self.assertFalse(good_source.exists())
        self.assertEqual(good_destination.read_bytes(), b'source 10')
        self.assertEqual(report['renamed_count'], 1)
        self.assertEqual(report['skipped_count'], 1)

    def test_preview_and_apply_keep_normal_cli_behavior(self):
        source, destination = self.paths(9)
        preview, preview_output = self.run_cli()
        self.assertEqual(source.read_bytes(), b'source 9')
        self.assertFalse(destination.exists())
        self.assertEqual(preview['mode'], 'preview')
        self.assertEqual(preview['planned_count'], 1)
        self.assertEqual(preview['renamed_count'], 0)
        self.assertIn(f'{source} -> {destination}', preview_output)

        applied, apply_output = self.run_cli('--apply')
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b'source 9')
        self.assertEqual(applied['mode'], 'apply')
        self.assertEqual(applied['renamed_count'], 1)
        self.assertEqual(applied['skipped_count'], 0)
        self.assertIn(f'{source} -> {destination}', apply_output)


if __name__ == '__main__':
    unittest.main()
