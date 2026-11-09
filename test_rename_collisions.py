"""Data preservation regressions for legacy filename cleanup."""
import os
from pathlib import Path
import tempfile
import unittest

import rename_downloaded_media as rename_tool


class RenameCollisionTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'Creating symlinks requires optional Windows privileges')
    def test_dangling_destination_is_not_available(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'Example Show (2024) - S02E09.mp4'
            destination = Path(directory) / 'S02E09 - Example Show (2024).mp4'
            source.write_bytes(b'synthetic fixture')
            destination.symlink_to('missing-target')
            plan, skipped = rename_tool.build_plan([Path(directory)])
            self.assertEqual(plan, [])
            self.assertEqual(len(skipped), 1)
            self.assertTrue(destination.is_symlink())


if __name__ == '__main__':
    unittest.main()
