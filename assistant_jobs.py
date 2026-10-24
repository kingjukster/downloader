"""Private, durable one-link jobs for Own Assistant; no implicit execution/retry.

The JSON bridge exposes only submit/status/cancel. Running a saved job is an
explicit Windows CLI action, with a kill-on-close Job Object around descendants.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit


SCRIPT = Path(__file__).resolve().with_name('download_from_captured_m3u8.py')


def connect(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'jobs.sqlite3', timeout=10)
    db.row_factory = sqlite3.Row
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, intent TEXT NOT NULL, state TEXT NOT NULL,
            reason TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0)''')
        if 'revision' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
            db.execute('ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    return db


def get(db, job_id):
    if not isinstance(job_id, str) or len(job_id) != 64 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Invalid download ID')
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown download ID; check the original state directory')
    return dict(row)


def snapshot(db, root, job_id):
    row = get(db, job_id)
    return {'job_id': job_id, 'state': row['state'], 'reason': row['reason'], 'revision': row['revision'],
            'intent_sha256': hashlib.sha256(row['intent'].encode()).hexdigest(),
            'cancel_requested': bool(row['cancel_requested']),
            'output_directory': str(Path(root).resolve() / job_id / 'files'),
            'result': json.loads(row['result']),
            'note': 'Recorded evidence; running may be interrupted. Never replay a claimed job.'}


def submit(db, root, key, url):
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ValueError('Supply a stable request key of 1-200 characters')
    if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError('Supply one HTTP(S) page URL without whitespace')
    parts = urlsplit(url)
    if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
        raise ValueError('Supply one HTTP(S) page URL without embedded credentials')
    job_id = hashlib.sha256(key.encode()).hexdigest()
    intent = json.dumps({'url': url}, sort_keys=True)
    with db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT intent FROM jobs WHERE id=?', (job_id,)).fetchone()
        if previous and previous['intent'] != intent:
            raise ValueError('Request key already belongs to a different download')
        if not previous:
            db.execute('INSERT INTO jobs(id,intent,state,reason) VALUES(?,?,?,?)',
                       (job_id, intent, 'queued', 'Saved only. Explicit owner run required; no browser opened.'))
    return snapshot(db, root, job_id)


def cancel(db, root, job_id, intent_sha256=None):
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = get(db, job_id)
        if intent_sha256 is not None and intent_sha256 != hashlib.sha256(row['intent'].encode()).hexdigest():
            raise ValueError('Download URL intent does not match; cancellation refused')
        if row['state'] == 'queued':
            db.execute("UPDATE jobs SET state='cancelled',reason='Cancelled before execution.',revision=revision+1 WHERE id=?", (job_id,))
        elif row['state'] == 'running' and not row['cancel_requested']:
            db.execute("UPDATE jobs SET cancel_requested=1,reason='Cancellation requested; waiting for verified process-tree closure.',revision=revision+1 WHERE id=?", (job_id,))
    return snapshot(db, root, job_id)


def inventory(directory):
    files = []
    for path in sorted(directory.glob('*.mp4')):
        if path.is_symlink() or not path.is_file():
            continue
        files.append({'name': path.name, 'bytes': path.stat().st_size})
    return files


def retain_controller_error(root, job_id, stage, error, result):
    directory = Path(root).resolve() / 'controller-errors'
    directory.mkdir(exist_ok=True)
    path = directory / (job_id + '-' + str(uuid.uuid4()) + '.json')
    evidence = {'job_id': job_id, 'stage': stage, 'error_type': type(error).__name__,
                'error': str(error), 'collected_result': result}
    with path.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return str(path)


def run(db, root, job_id, timeout=3600, scope_factory=None):
    if not isinstance(timeout, int) or not 1 <= timeout <= 21600:
        raise ValueError('Timeout must be 1-21600 seconds')
    # Establish containment before claiming or dispatching; unsupported hosts refuse.
    from windows_job import WindowsJob
    with (scope_factory or WindowsJob)() as scope:
        directory = Path(root).resolve() / job_id
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = get(db, job_id)
            if row['state'] != 'queued':
                raise ValueError('Only a queued download can run. Claimed/terminal jobs are never replayed.')
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','uncertain')").fetchone():
                raise ValueError('Unresolved download owns this browser/state. Inspect it before dispatch.')
            db.execute("UPDATE jobs SET state='running',reason='Execution claimed; outcome not yet known.',revision=revision+1 WHERE id=?", (job_id,))
        state, reason = 'uncertain', 'Controller interrupted; inspect private evidence and process ownership. No replay.'
        result = {}
        stage = 'prepare_output'
        try:
            directory.mkdir(parents=True, exist_ok=False)
            output = directory / 'files'
            output.mkdir()
            with (directory / 'worker.log').open('xb') as log:
                stage = 'start_process'
                process = scope.start([sys.executable, str(SCRIPT), json.loads(row['intent'])['url'],
                                       '--parallel', '1', '--output', str(output)], log)
                deadline = time.monotonic() + timeout
                stage = 'observe_process'
                while process.poll() is None:
                    if get(db, job_id)['cancel_requested'] or time.monotonic() >= deadline:
                        requested = bool(get(db, job_id)['cancel_requested'])
                        stage = 'close_process_tree'
                        scope.stop()
                        process.wait(timeout=10)
                        state = 'cancelled' if requested else 'failed'
                        reason = 'Cancelled; process tree closed.' if requested else 'Time limit reached; process tree closed.'
                        break
                    time.sleep(0.1)
                else:
                    code = process.wait()
                    result['exit_code'] = code
                    stage = 'close_process_tree'
                    scope.stop()  # Close browser descendants before publishing results.
                    state = 'awaiting_review' if code == 0 else 'failed'
                    reason = 'Download exited successfully; inspect saved files.' if code == 0 else 'Downloader failed; inspect private worker.log (may contain signed URLs).'
                stage = 'collect_output'
                result['files'] = inventory(output)
                if state == 'awaiting_review' and not any(f['bytes'] > 0 for f in result['files']):
                    state, reason = 'failed', 'Downloader returned success without nonempty MP4 output.'
                result['partial_files_possible'] = state != 'awaiting_review'
                result['log_path'] = str(directory / 'worker.log')
                result['media_verified'] = False
        except BaseException as error:
            # Any error in launch, shutdown, or inventory preserves the claim.
            # __exit__ closes the job handle, but closure is not assumed verified.
            # Controller errors are not child output. Retain them privately so
            # closing the CLI cannot erase the reason inspection is required.
            try:
                result['controller_error_path'] = retain_controller_error(root, job_id, stage, error, result)
            except (OSError, ValueError, TypeError):
                result['controller_error_capture_failed'] = True
            with db:
                db.execute("UPDATE jobs SET state='uncertain',reason=?,result=?,revision=revision+1 WHERE id=?",
                           ('Execution or evidence capture interrupted; inspect original process ownership and private files. No replay.',
                            json.dumps(result), job_id))
            raise
        with db:
            db.execute('UPDATE jobs SET state=?,reason=?,result=?,revision=revision+1 WHERE id=?',
                       (state, reason, json.dumps(result), job_id))
    return snapshot(db, root, job_id)


def bridge(root, request):
    if not isinstance(request, dict) or request.get('operation') not in {'submit', 'status', 'cancel'}:
        raise ValueError('Bridge supports submit, status and cancel only; execution is an explicit owner action')
    expected = {'operation', 'request_key', 'url'} if request['operation'] == 'submit' else {'operation', 'job_id'}
    if request['operation'] == 'cancel':
        expected.add('intent_sha256')
    if set(request) != expected:
        raise ValueError('Unexpected or missing bridge request fields')
    with closing(connect(root)) as db:
        if request['operation'] == 'submit':
            return submit(db, root, request['request_key'], request['url'])
        if request['operation'] == 'cancel':
            if not isinstance(request['intent_sha256'], str):
                raise ValueError('Cancellation requires the saved URL intent hash')
            return cancel(db, root, request['job_id'], request['intent_sha256'])
        return snapshot(db, root, request['job_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Private persistent state and output directory')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('bridge', help='One bounded JSON request on stdin; never launches a download')
    execution = commands.add_parser('run', help='Explicit foreground Windows execution of one saved job')
    execution.add_argument('job_id')
    execution.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    try:
        if args.command == 'bridge':
            body = sys.stdin.buffer.read(16385)
            if len(body) > 16384:
                raise ValueError('Request exceeds 16 KiB')
            result = bridge(args.state, json.loads(body.decode('utf-8-sig')))
        else:
            with closing(connect(args.state)) as db:
                result = run(db, args.state, args.job_id, args.timeout)
        print(json.dumps({'ok': True, 'download': result}, ensure_ascii=True))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=True))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
