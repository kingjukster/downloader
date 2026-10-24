# Own Assistant job adapter (candidate)

`assistant_jobs.py` wraps the existing one-link browser downloader. It does not
replace capture, episode discovery, playlist validation or ffmpeg. Submit/status/
cancel use bounded JSON on stdin; the bridge never executes a download. An
explicit foreground Windows `run JOB_ID` command starts one saved job.

Use `--state D:\private-download-state` before `bridge` or `run`. This directory
holds the job database, private logs and output. Use one persistent directory,
separate from source control and other standalone capture-profile users. Each
request has an owner-supplied stable `request_key`; repeating the key and URL
returns the same job. A changed URL is refused. For example, send this JSON to
`python assistant_jobs.py --state D:\private-download-state bridge` on stdin:

```json
{"operation":"submit","request_key":"movie-001","url":"https://media.example/movie"}
```

Use `{"operation":"status","job_id":"RETURNED_64_HEX_ID"}` to observe a job.
To cancel, send `{"operation":"cancel","job_id":"RETURNED_64_HEX_ID","intent_sha256":"RETURNED_INTENT_HASH"}`.
Cancellation verifies that hash inside its transaction before changing the job;
a different URL using the same request key cannot be cancelled accidentally.
Job IDs are SHA-256 hashes of request keys. Then
explicitly execute `python assistant_jobs.py --state D:\private-download-state
run JOB_ID --timeout 3600` to open the browser and download. Use the Python with
the project's existing requirements installed. No dependencies are installed by
the adapter. Keep its controller open and use another terminal for cancellation.

Snapshots include an `intent_sha256` binding the URL and a monotonic `revision`.
The assistant checks the URL binding before accepting status or outputs, and
discards older revisions that arrive after a newer cancellation. Repeated
cancellation requests do not advance the revision when nothing changes. Existing
job databases gain a revision column transactionally, preserving their original
state, identity and files; this never resets or replays work.

The helper joins a Windows Job Object before loading the downloader. The
controller holds the sole remaining job handle, so abrupt controller death kills
descendants. Requested cancellation and timeout terminate that exact job and
verify its active process count is zero before publishing a terminal state. No
PID-based termination, shell commands, global browser kill, service or supervisor
is used. Unsupported containment refuses execution. The guard is a process
lifetime boundary, not a filesystem/network security sandbox.

Outputs remain under `STATE/JOB_ID/files`. `worker.log` is private and may contain
signed URLs or headers. Successful CLI exit and nonempty MP4 output mean
`awaiting_review`, not verified media. Failure/cancellation retain partial files.
Claimed, failed and terminal jobs never rerun. Unknown execution/closure preserves
`uncertain`; controller death can leave `running`. Both retain ownership and
block other runs in this state directory. There is no automatic recovery/reset,
resume or replay; inspect process ownership and original evidence manually.

Caught controller-side errors retain a separate private record under
`STATE/controller-errors/JOB_ID-UNIQUE_ID.json`. It records the failure stage,
exception type/message and any result metadata gathered before interruption.
Status retains `result.controller_error_path`, not the raw diagnostic text.
Like `worker.log`, this file can contain sensitive paths or request details;
keep it private and out of source control. If writing the diagnostic itself
fails, status records `controller_error_capture_failed: true` and the original
exception is still raised. Neither case releases uncertain ownership, verifies
closure, resets a job or permits replay. Abrupt controller termination may
prevent this record from being written; a missing diagnostic proves nothing.

The assistant connection and command experience are documented in its companion
`docs/downloader.md`. Neither candidate was installed during development. Windows
fixtures, including descendant termination, use only temporary local files and
Python processes, never a browser or network media. Run the focused checks with
`python -m unittest test_assistant_jobs -v`. Output and logs have no storage quota.

The complete existing baseline suite passed 51 tests under native Windows
execution. The initial candidate passed 63 tests. In the restricted execution
environment, even a trivial `asyncio.run(asyncio.sleep(0))` stalled before any
downloader code, in Windows Python's socket-pair initialization. Bounded stack
traces distinguish that environment limitation from the mocked browser test.
The native qualification used the existing execution permission gate without
changing security settings or accessing WSL. Private diagnostics remain in
`work/qualification`, excluded from Git.
The final follow-up passed all 65 tests with resource warnings treated as errors
under a 60-second process bound, including revision migration and cancellation
identity checks.

The subsequent controller-diagnostic refinement passed all 68 native Windows
fixture tests under the same 60-second bound with resource warnings treated as
errors. New fixtures cover closure evidence after database reopening, retained
exit metadata after inventory failure, diagnostic-write failure, and private
launch errors without replacing older evidence. This qualifies source fixtures,
not installed integration, live browser/media behavior or WSL access.
