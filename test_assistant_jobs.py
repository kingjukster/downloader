import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import assistant_jobs as jobs
from windows_job import WindowsJob


def temporary():
    root = Path(__file__).resolve().parent / 'work' / 'fixture-tests'
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


class FixtureProcess:
    def __init__(self, scope):
        self.scope = scope

    def poll(self):
        return None if self.scope.pending else self.scope.code

    def wait(self, timeout=None):
        return self.scope.code


class FixtureScope:
    def __init__(self, code=0, pending=False, on_start=None, broken=False):
        self.code, self.pending, self.on_start, self.broken = code, pending, on_start, broken
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def start(self, argv, log):
        output = Path(argv[-1])
        (output / 'Fixture.mp4').write_bytes(b'fixture output, not playable media')
        if self.on_start:
            self.on_start()
        return FixtureProcess(self)

    def stop(self):
        if self.broken:
            raise OSError('fixture closure not verified')
        self.closed, self.pending = True, False


class JobTests(unittest.TestCase):
    def test_pre_revision_database_migrates_without_replaying_job(self):
        legacy_root = self.root / 'legacy'
        legacy_root.mkdir()
        job_id = 'a' * 64
        db = sqlite3.connect(legacy_root / 'jobs.sqlite3')
        with db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,intent TEXT,state TEXT,reason TEXT,cancel_requested INTEGER,result TEXT)")
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (job_id, '{}', 'cancelled', 'original cancellation', 0, '{}'))
        db.close()
        db = jobs.connect(legacy_root)
        try:
            row = jobs.snapshot(db, legacy_root, job_id)
            self.assertEqual('cancelled', row['state'])
            self.assertEqual(0, row['revision'])
            with self.assertRaises(ValueError):
                jobs.run(db, legacy_root, job_id, scope_factory=FixtureScope)
        finally:
            db.close()

    def setUp(self):
        self.temp = temporary()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = jobs.connect(self.root)
        self.addCleanup(self.db.close)

    def submit(self, key='fixture', url='https://media.example/movie'):
        return jobs.submit(self.db, self.root, key, url)['job_id']

    def test_repeat_submit_and_changed_intent(self):
        job_id = self.submit()
        self.assertEqual(job_id, self.submit())
        self.assertFalse((self.root / job_id).exists())
        with self.assertRaisesRegex(ValueError, 'different download'):
            self.submit(url='https://media.example/other')
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_bad_inputs_and_bridge_cannot_execute(self):
        for url in ['file:///etc/passwd', '--help', 'https://name:secret@example.com', 'https://a.example/\n--overwrite']:
            with self.assertRaises(ValueError):
                self.submit(url=url)
        with self.assertRaises(ValueError):
            jobs.bridge(self.root, {'operation': 'run'})

    def test_bridge_rejects_cancellation_of_different_url_intent(self):
        job_id = self.submit()
        with self.assertRaisesRegex(ValueError, 'cancellation refused'):
            jobs.bridge(self.root, {'operation': 'cancel', 'job_id': job_id, 'intent_sha256': 'f' * 64})
        self.assertEqual('queued', jobs.snapshot(self.db, self.root, job_id)['state'])

    def test_queued_cancel_and_repeat_never_restarts(self):
        job_id = self.submit()
        self.assertEqual(0, jobs.snapshot(self.db, self.root, job_id)['revision'])
        self.assertEqual('cancelled', jobs.cancel(self.db, self.root, job_id)['state'])
        self.assertEqual('cancelled', jobs.cancel(self.db, self.root, job_id)['state'])
        self.assertEqual(1, jobs.snapshot(self.db, self.root, job_id)['revision'])
        self.submit()
        with self.assertRaisesRegex(ValueError, 'never replayed'):
            jobs.run(self.db, self.root, job_id, scope_factory=FixtureScope)

    def test_durable_output_and_no_replay(self):
        job_id = self.submit()
        scope = FixtureScope()
        response = jobs.run(self.db, self.root, job_id, scope_factory=lambda: scope)
        self.assertTrue(scope.closed)
        self.assertEqual('awaiting_review', response['state'])
        self.assertFalse(response['result']['media_verified'])
        self.assertGreater(response['result']['files'][0]['bytes'], 0)
        self.db.close()
        self.db = jobs.connect(self.root)
        self.addCleanup(self.db.close)
        self.assertEqual(response, jobs.snapshot(self.db, self.root, job_id))
        with self.assertRaises(ValueError):
            jobs.run(self.db, self.root, job_id, scope_factory=FixtureScope)

    def test_running_cancel_closes_before_terminal(self):
        job_id = self.submit()
        scope = FixtureScope(pending=True, on_start=lambda: jobs.cancel(self.db, self.root, job_id))
        response = jobs.run(self.db, self.root, job_id, scope_factory=lambda: scope)
        self.assertTrue(scope.closed)
        self.assertEqual('cancelled', response['state'])
        self.assertTrue(response['result']['partial_files_possible'])

    def test_failed_closure_stays_uncertain_and_blocks_other_work(self):
        job_id = self.submit()
        scope = FixtureScope(broken=True)
        with self.assertRaises(OSError):
            jobs.run(self.db, self.root, job_id, scope_factory=lambda: scope)
        self.assertEqual('uncertain', jobs.snapshot(self.db, self.root, job_id)['state'])
        self.db.close()
        self.db = jobs.connect(self.root)
        self.addCleanup(self.db.close)
        saved = jobs.snapshot(self.db, self.root, job_id)
        evidence = json.loads(Path(saved['result']['controller_error_path']).read_text(encoding='utf-8'))
        self.assertEqual(job_id, evidence['job_id'])
        self.assertEqual('close_process_tree', evidence['stage'])
        self.assertEqual('OSError', evidence['error_type'])
        self.assertEqual('fixture closure not verified', evidence['error'])
        self.assertTrue((self.root / job_id / 'files' / 'Fixture.mp4').exists())
        other = self.submit(key='other')
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            jobs.run(self.db, self.root, other, scope_factory=FixtureScope)

    def test_failed_downloader_keeps_evidence(self):
        job_id = self.submit()
        response = jobs.run(self.db, self.root, job_id, scope_factory=lambda: FixtureScope(code=2))
        self.assertEqual('failed', response['state'])
        self.assertEqual(2, response['result']['exit_code'])
        self.assertTrue(response['result']['partial_files_possible'])

    def test_inventory_error_retains_collected_result_and_original_exception(self):
        job_id = self.submit()
        with patch.object(jobs, 'inventory', side_effect=OSError('fixture inventory unavailable')):
            with self.assertRaisesRegex(OSError, 'fixture inventory unavailable'):
                jobs.run(self.db, self.root, job_id, scope_factory=FixtureScope)
        saved = jobs.snapshot(self.db, self.root, job_id)
        self.assertEqual('uncertain', saved['state'])
        self.assertEqual(0, saved['result']['exit_code'])
        evidence = json.loads(Path(saved['result']['controller_error_path']).read_text(encoding='utf-8'))
        self.assertEqual('collect_output', evidence['stage'])
        self.assertEqual({'exit_code': 0}, evidence['collected_result'])

    def test_diagnostic_write_failure_does_not_hide_original_error_or_release_claim(self):
        job_id = self.submit()
        with patch.object(jobs, 'retain_controller_error', side_effect=OSError('fixture evidence unavailable')):
            with self.assertRaisesRegex(OSError, 'fixture closure not verified'):
                jobs.run(self.db, self.root, job_id, scope_factory=lambda: FixtureScope(broken=True))
        saved = jobs.snapshot(self.db, self.root, job_id)
        self.assertEqual('uncertain', saved['state'])
        self.assertTrue(saved['result']['controller_error_capture_failed'])
        self.assertNotIn('controller_error_path', saved['result'])

    def test_launch_error_is_retained_privately_without_replacing_existing_evidence(self):
        job_id = self.submit()
        retained = self.root / 'controller-errors'
        retained.mkdir()
        prior = retained / 'prior.json'
        prior.write_text('original diagnostic', encoding='utf-8')
        sensitive = 'fixture launch error with private token'
        with patch.object(FixtureScope, 'start', side_effect=OSError(sensitive)):
            with self.assertRaisesRegex(OSError, sensitive):
                jobs.run(self.db, self.root, job_id, scope_factory=FixtureScope)
        saved = jobs.snapshot(self.db, self.root, job_id)
        self.assertNotIn(sensitive, json.dumps(saved))
        evidence = json.loads(Path(saved['result']['controller_error_path']).read_text(encoding='utf-8'))
        self.assertEqual('start_process', evidence['stage'])
        self.assertEqual(sensitive, evidence['error'])
        self.assertEqual('original diagnostic', prior.read_text(encoding='utf-8'))

    def test_success_without_output_is_failure(self):
        job_id = self.submit()
        with patch.object(jobs, 'inventory', return_value=[]):
            response = jobs.run(self.db, self.root, job_id, scope_factory=FixtureScope)
        self.assertEqual('failed', response['state'])


@unittest.skipUnless(os.name == 'nt', 'Windows containment qualification')
class WindowsScopeTests(unittest.TestCase):
    def test_running_job_cancellation_retains_partial_output(self):
        with temporary() as temp:
            root = Path(temp)
            fixture = root / 'download_fixture.py'
            fixture.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[-1]); (p/"Partial.mp4").write_bytes(b"fixture")\ntime.sleep(30)\n')
            db = jobs.connect(root)
            try:
                job_id = jobs.submit(db, root, 'cancel-fixture', 'https://media.example/fixture')['job_id']
                def request_cancel():
                    deadline = time.monotonic() + 5
                    while not (root / job_id / 'files' / 'Partial.mp4').exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    connection = jobs.connect(root)
                    try:
                        jobs.cancel(connection, root, job_id)
                    finally:
                        connection.close()
                thread = threading.Thread(target=request_cancel)
                thread.start()
                try:
                    with patch.object(jobs, 'SCRIPT', fixture):
                        response = jobs.run(db, root, job_id, timeout=5)
                finally:
                    thread.join(timeout=6)
                self.assertFalse(thread.is_alive())
                self.assertEqual('cancelled', response['state'])
                self.assertEqual([{'name': 'Partial.mp4', 'bytes': 7}], response['result']['files'])
            finally:
                db.close()

    def test_job_timeout_closes_scope_and_preserves_output(self):
        with temporary() as temp:
            root = Path(temp)
            fixture = root / 'download_fixture.py'
            fixture.write_text('import pathlib,sys,time\np=pathlib.Path(sys.argv[-1]); (p/"Partial.mp4").write_bytes(b"fixture")\ntime.sleep(30)\n')
            db = jobs.connect(root)
            try:
                job_id = jobs.submit(db, root, 'timeout-fixture', 'https://media.example/fixture')['job_id']
                with patch.object(jobs, 'SCRIPT', fixture):
                    response = jobs.run(db, root, job_id, timeout=1)
                self.assertEqual('failed', response['state'])
                self.assertIn('Time limit', response['reason'])
                self.assertTrue(response['result']['partial_files_possible'])
            finally:
                db.close()

    def test_controller_death_closes_fixture_descendants(self):
        with temporary() as temp:
            root = Path(temp)
            heartbeat = root / 'heartbeat'
            child = root / 'child.py'
            child.write_text('import pathlib,time\np=pathlib.Path(' + repr(str(heartbeat)) + ')\nwhile True: p.write_text(str(time.time())); time.sleep(.05)\n')
            controller = root / 'controller.py'
            controller.write_text('import sys,time\nsys.path.insert(0,' + repr(str(Path(__file__).resolve().parent)) + ')\n'
                                  'from windows_job import WindowsJob\n'
                                  'with open(' + repr(str(root / 'worker.log')) + ',"xb") as log, WindowsJob() as scope:\n'
                                  ' scope.start([sys.executable,' + repr(str(child)) + '],log)\n time.sleep(30)\n')
            process = subprocess.Popen([sys.executable, str(controller)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import time
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(heartbeat.exists())
                process.terminate()
                process.wait(timeout=5)
                time.sleep(.2)
                before = heartbeat.read_text()
                time.sleep(.2)
                self.assertEqual(before, heartbeat.read_text())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_scope_closes_fixture_descendant(self):
        with temporary() as temp:
            root = Path(temp)
            script = root / 'fixture.py'
            # The child writes a heartbeat; it must stop when the Job Object closes.
            heartbeat = root / 'heartbeat'
            child = "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + ");\nwhile True: p.write_text(str(time.time())); time.sleep(.05)"
            script.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr(child) + '])\ntime.sleep(30)\n')
            with (root / 'worker.log').open('xb') as log, WindowsJob() as scope:
                process = scope.start([sys.executable, str(script)], log)
                import time
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(heartbeat.exists())
                scope.stop()
                process.wait(timeout=5)
                before = heartbeat.read_text()
                time.sleep(.2)
                self.assertEqual(before, heartbeat.read_text())


if __name__ == '__main__':
    unittest.main()
