# JetKVM Chrome video search and playback flow

Saved at the user's request on 2026-11-16 (America/Chicago). This is a reusable, visually guided procedure, not an unattended automation or fixed-coordinate macro.

## Setup already verified

- Physical server Chrome with uBlock Origin Lite installed; use the existing profile.
- JetKVM device last verified at 192.168.0.141. Access through the shared queued JetKVM broker.
- In uBOL Dashboard > Filter lists > Annoyances, enable EasyList/uBO - Overlay Notices. Default Optimal filtering and popup blocking were already enabled.
- Do not add a cosmetic filter hiding .player-pre-init-ads: tested and removed because it prevented the player from initializing.

## Acquire desktop

1. Read the jetkvm-browser skill and its jetkvm-control prerequisite. Begin a broker desktop task, retain the private handle, and wait until active.
2. Inspect fresh HDMI pixels before every input; each snapshot ID permits one input. Capture and inspect again after input, allowing transitions to settle. Never reuse stored coordinates without checking current pixels.
3. Use server Chrome. Open https://www.lookmovie2.to in the existing suitable tab. Preserve unrelated tabs and work. If the desktop is locked, request the owner to unlock it; if the broker is unavailable or blocked, report the actual condition without bypassing ownership.

## Search for a video

Input: the requested title; year and movie/show type when provided. For a series, also obtain season and episode when needed.

1. Pause any currently playing video before navigating.
2. Return to the top of the page with ctrl+home or upward scrolling. Wait for scrolling to settle and inspect the header.
3. Click the website field labeled Search for a Movie or TV Show, rather than Chrome's address bar. Verify the caret in that field. If text already exists, select it with ctrl+a while that field is focused, then replace it.
4. Type the requested title. Broker type_text supports at most 160 US-keyboard characters per action; inspect the entered text.
5. Wait for the live dropdown. Enter was not needed in the verified test. Results are separated into MOVIES and SHOWS and display title, year, rating and quality labels.
6. Select the matching title AND year/type. Do not assume the first result is the intended version. If the request is ambiguous, ask which version; if no result appears, shorten the query or correct its spelling. VIEW ALL is visible as an optional route, but was not tested in this session.
7. Inspect the destination's title and year. The verified movie result opened a /movies/view/... details page.

## Select a TV season and episode

1. Search for the series and select the correct entry under SHOWS, checking its year to distinguish the original from sequels or reboots. The tested Dexter entry was dated 2006.
2. Verify the /shows/view/... details page and click its large central Play triangle. This opens /shows/play/... and may immediately start a default episode; do not assume it is the requested episode.
3. Locate the Season and Episode dropdowns above the player. Select the requested season first. Wait for the selection and player to update. In the test, changing from Season 1 to Season 2 reset the episode to Episode 1.
4. Open the Episode dropdown and select the requested episode explicitly. If an item is outside the visible list, scroll inside that open dropdown and inspect again before selecting it. Do not substitute a different season or episode if the requested one is absent.
5. Verify both selected menu values, the player title such as S2E2 Waiting to Exhale, and the visible URL fragment when present. The tested URL ended #S2-E2-46020. Treat IDs as site-specific; never construct a new episode URL by guessing its ID.
6. Selection automatically loaded and played the chosen episode in this test. Use Play only if the actual player is paused, and handle a resume dialog if one appears. Scroll down enough to expose the player controls while retaining the season/episode selectors when possible.
7. Verify the selected episode's playback time advances across fresh screenshots and the frames change. Then follow the normal requested end state and broker-release steps below. For test-only requests, pause the player.

## Open and verify playback

1. Click the large central Play triangle (or the actual Watch Now control if the layout differs) on the verified title-details page.
2. Inspect the /movies/play/... or /shows/play/... page and the loaded player. For a show, complete season/episode selection above before final playback verification. Use Play if needed. If the resume dialog appears, select Continue Watching unless the user requested a restart; Start from the Beginning is a separate option.
3. Move the pointer over the player to reveal controls. Verify changing video frames and advancing playback time across separate fresh screenshots. Tool acknowledgments alone do not prove playback.
4. Confirm the intrusive premium playback overlay and external popup tabs/windows are absent in this test. If a popup returns, inspect it and recheck uBOL settings; do not claim that hiding it proves playback works.
5. Leave playback in the state requested by the user. For a test-only task, pause after verifying playback. Finish the broker task and confirm owner is null and blocked is false.

## Verified example and limits

Searched How to Rob a Bank through the visible search field. The dropdown showed versions dated 2026, 2024, and 2007. Selected the 2026 entry, confirmed its details page, clicked the large Play triangle, selected Continue Watching, and observed playback advance to 1:11 / 1:56:55 before pausing. Released the broker with blocked=false.

The preceding filter test verified playback after two reloads and progress to 1:00. Overlay Notices remains enabled. A separate lower-left premium promotion was visible on the title-details page during the search test, and ordinary premium banners remain; the result is not a claim that all site promotions are blocked. TV season/episode navigation was verified in the follow-up test below. VIEW ALL remains untested.

Visual evidence is retained in the originating local task archive; it is not bundled in this repository. No new extension permissions or account changes were made for search. Refresh live UI when reusing this flow.

## TV follow-up test: 2026-11-16

Searched Dexter, selected SHOWS > Dexter (2006), opened the show player, and changed Season 1 to Season 2. Verified the reset to Episode 1, then selected Episode 2. Both dropdowns, the URL fragment #S2-E2-46020, and player title S2E2 Waiting to Exhale agreed. Playback advanced from 00:13 to 00:27 and was paused at 00:33 / 55:58. No intrusive premium playback overlay or external popup was observed. The separate details-page promotion and ordinary banners remain.

Test URL: https://www.lookmovie2.to/shows/play/1690530147-dexter-2006#S2-E2-46020
TV test evidence is retained in the originating local task archive. JetKVM released with owner=null and blocked=false. This verifies the menu-based flow for this series, not availability of every series or episode. No additional browser permissions or filter changes were needed.

## Hand off the selected item to this downloader

After visual verification, retain the complete player URL, including the #S2-E2-ID fragment for an episode. Release the JetKVM desktop turn before running nonvisual commands. Run this command from the downloader checkout on the intended download machine:

```powershell
python .\download_selected.py 'https://www.lookmovie2.to/shows/play/1690530147-dexter-2006#S2-E2-46020'
```

This preview invokes the existing downloader with --dry-run-urls and --no-auto-expand. It must print exactly the selected URL and 1 URL(s) queued. It does not open a browser or download media. The URL above is the observed test selection, not an instruction to download it.

When the user requests saving that selected media, add --execute and an explicit output directory:

```powershell
python .\download_selected.py 'PASTE_EXACT_VERIFIED_PLAYER_URL' --execute --output 'D:\Media Downloads'
```

Use only media you are authorized to access and save. The wrapper disables automatic series expansion, dropdown discovery, and the old blind top-right click. It uses one worker, skips existing files, and preserves the original URL unchanged. It rejects LookMovie details pages and show player URLs missing a complete episode fragment. To download a whole series, use the separately documented series workflow only when that broader scope is requested.

The downloader opens its own capture browser/profile on the machine where this command runs; it does not attach to the existing JetKVM Chrome tab. Chrome selection, cookies and uBlock settings are not transferred automatically. Optional --browser-executable and --user-data-dir select a compatible capture browser and dedicated profile. Do not point it at an actively used Chrome profile. If capture needs a playback or consent click, inspect that actual capture window through JetKVM on the server. Do not blindly replay coordinates from the selection tab.

The existing Own Assistant job adapter remains unchanged and may expand a series; use download_selected.py for this exact-item flow. This is a CLI handoff, not a new background job, scheduled service, or installed server integration.

Before reporting a completed download, inspect the output and verify it contains the intended movie/episode using playback and duration checks. Successful website playback or a queue preview alone does not establish download compatibility.

Downloads require ffprobe. FFmpeg writes a unique `.partial.mp4` (or matching container suffix), and the downloader publishes the final filename only after a zero exit, `progress=end`, readable video metadata, and positive duration. When the source provides a duration, the output must reach it within a tolerance of two seconds or 0.5%, whichever is larger; `--duration` limits that expectation. Failed partials remain available for diagnosis. Existing outputs are skipped only if their local container, video stream and duration validate; this check cannot prove completeness of older, cleanly finalized short recordings without an expected source duration. Invalid existing files are preserved, and a new numbered filename is used.

Server acceptance on 2026-11-17: searched WandaVision through JetKVM, selected Season 1 Episode 1, launched the installed exact-item CLI, and closed the capture browser's visible premium overlay. The resulting 115,405,519-byte MP4 had 1,593.121 seconds of H.264 854x480 video and AAC audio. A full independent decode passed; local-file playback through JetKVM advanced beyond 20 minutes. The prior 55 MB incomplete MP4 was preserved and correctly rejected by skip-existing validation. The original false success arose because completion trusted the process exit code and did not inspect the saved media; the exact cause of that earlier FFmpeg termination was not established by its logs.

