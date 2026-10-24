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

Use `{"operation":"status","job_id":"RETURNED_64_HEX_ID"}` or the same request
with `"operation":"cancel"`. Job IDs are SHA-256 hashes of request keys. Then
explicitly execute `python assistant_jobs.py --state D:\private-download-state
run JOB_ID --timeout 3600` to open the browser and download. Use the Python with
the project's existing requirements installed. No dependencies are installed by
the adapter. Keep its controller open and use another terminal for cancellation.

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

The assistant connection and command experience are documented in its companion
`docs/downloader.md`. Neither candidate was installed during development. Windows
fixtures, including descendant termination, use only temporary local files and
Python processes, never a browser or network media. Run the focused checks with
`python -m unittest test_assistant_jobs -v`. Output and logs have no storage quota.
