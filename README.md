# One-link browser-assisted downloader

Use this only for streams you are authorized to access and save.

Candidate integration: [durable Own Assistant jobs](ASSISTANT_JOBS.md).
Saving a job does not start a download; execution remains an explicit Windows action.

## Windows setup

Requirements:

- Python 3.11 or newer
- FFmpeg and FFprobe on `PATH`
- A Chromium browser. Opera Air is used automatically when installed; otherwise
  Playwright's Chromium build is used.

From PowerShell:

```powershell
git clone https://github.com/kingjukster/downloader.git
cd .\downloader
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
winget install --id Gyan.FFmpeg --exact
```

Open a new PowerShell window after installing FFmpeg so its updated `PATH` is
available. Confirm the installation with:

```powershell
.\.venv\Scripts\python.exe -m unittest -v
ffmpeg -version
ffprobe -version
```

## One-link mode

Close other Opera Air windows that use the automation profile, then run:

```powershell
.\.venv\Scripts\python.exe .\download_from_captured_m3u8.py "https://example.com/title"
```

The command opens the link in the dedicated Opera Air capture profile. It first
looks for episode links, traverses flat paginated `All Episodes` grids, and falls
back to visible Season/Episode controls. For large grids, it scans each page for
direct links first and clicks only episodes whose URLs remain hidden behind
JavaScript. If episodes are found, it deduplicates them, sorts them by season and
episode, and processes the full queue. If none are found, it treats the link as a
movie or other single title and captures that page directly. Downloader-owned
browser tabs are muted; this does not mute a normal Opera session. Downloads
default to `downloads`.

Flat episode links using `?episode=N` are treated as season 1 and receive names
such as `S01E23 - Title.mp4`. Discovery never performs the optional playback
click; that click is reserved for the later stream-capture stage.

To inspect the resolved queue without downloading:

```powershell
python .\download_from_captured_m3u8.py "https://example.com/title" --dry-run-urls
```

Useful overrides:

- `--no-auto-expand` skips title detection and processes the supplied link only.
- `--no-discover-dropdowns` performs passive episode discovery without changing
  paginated episode grids or season/episode controls.
- `--episode-counts 6,13,16` supplies known per-season counts when a site's
  custom controls do not expose every option at once.
- `--parallel N` processes multiple discovered episodes concurrently. The
  default remains sequential to avoid browser-profile and server contention.
- `--skip-existing` resumes a queue without reprocessing non-empty files at
  their normal output paths.

The browser may still require a manual login, consent click, or playback action.
The existing `--url-file`, catalog, and explicit series-expansion modes remain
available for batch workflows.

During capture, the downloader keeps listening briefly after the first HLS
playlist, then checks every unique candidate with `ffprobe`. PNG-only,
streamless, and otherwise non-video candidates are rejected before FFmpeg runs;
the first normal dimensioned video rendition is selected. Use
`--playlist-collection-wait 15` when you need extra time to choose an alternate
player/server manually, and `--playlist-probe-timeout N` to change the per-
candidate validation limit. If all candidates are transformed rather than
standard HLS media, the downloader stops with a consolidated rejection message.

New downloads use media-library-friendly names such as `Movie Title (2026).mp4`
and `S01E02 - Show Title (2026).mp4`.

To preview cleanup of legacy URL-derived filenames without changing anything:

```powershell
python .\rename_downloaded_media.py .\downloads "F:\Dad Movies"
```

Add `--apply --log .\work\rename-log.json` after reviewing the preview. Existing
destinations are never overwritten; collisions are skipped and written to the log.
