# Plan: Speaker-gate echo filter (replace voice processing)

| | |
|---|---|
| **Date** | 2026-09-30 |
| **Status** | Complete |
| **Project** | filler-killer |
| **Backlog Card ID** | fc13 (BACKLOG.json, status doing until release ships) |

## Why
On a Teams-in-Chrome call, the remote side could barely hear Matt while Filler
Killer was running; quitting FK restored his mic instantly.

Root cause (reproduced 2026-09-30): FK's default "echo cancellation" enables
macOS voice processing (VPIO) on the default mic via
`AVAudioEngine.inputNode.setVoiceProcessingEnabled`. While VPIO is active, any
*other* plain capture of the same mic (how Chrome/WebRTC captures) is
suppressed to near-silence: plain-capture RMS went ~1,900 → ~1.4 (≈ -60 dB)
the instant VPIO started and recovered when it stopped. System input volume
did not change (71 throughout), so it's device-level processing, not a slider.

FK's whole purpose is running during calls, so the default mode breaks the
call. We still want "no headphones needed, remote voices not counted".

## Scope
**In scope:**
- Always capture the mic with plain PortAudio (never disturbs other apps).
- New "speaker gate": a small Swift helper uses a Core Audio process tap
  (macOS 14.2+) to measure what the Mac is *playing*; FK discards recognized
  words/fillers (and their talk-time) that overlap remote playback.
- Works with any mic, including a pinned `mic_device` (VPIO only worked with
  the default mic).
- Info.plist `NSAudioCaptureUsageDescription`, build + sign the helper.
- Settings/menu copy updated; legacy VPIO kept only as a config opt-in with a
  warning.
- Version bump to 1.7.0, README/config docs.

**Out of scope / later:**
- True echo cancellation (subtract speaker audio; keeps fillers during
  double-talk) — "option 3", builds on this helper's audio stream.
- Voice enrollment / speaker verification (Vosk spk model) — "option 2".
- Skipping the gate automatically when output is headphones.
- Publishing the release (GitHub release + cask bump) — needs Matt's go-ahead.

## Approach
1. **Swift helper `fk-systap`**: global process tap (`CATapDescription`,
   stereo global, private, unmuted) → private aggregate device → IOProc
   computes RMS every ~50 ms → prints `dbfs` lines to stdout. Exits when stdin
   closes (parent gone) and tears down tap/aggregate on exit.
2. **Python `SpeakerGate`** (thread): spawns the helper, parses levels, keeps
   intervals where playback is above a threshold (monotonic clock), answers
   `busy(t0, t1)` with padding for acoustic echo tail.
3. **Mic-time → wall-clock mapping**: Listener records (samples fed, arrival
   time) per block, so Vosk word timestamps can be mapped to monotonic time.
4. **Gate in `process_block`**: final word results drop words overlapping
   remote playback (recount fillers, tokens and spoken duration from what
   remains); acoustic um/uh likewise; partial flashes and `speech` (airtime)
   events are suppressed while playback is active now.
5. Remove the VPIO default path; `echo_cancel` (true) now means "speaker
   gate"; `echo_method: "voice_processing"` keeps the old path as an explicit
   opt-in.
6. make_app.sh: compile + sign helper, add usage string; test locally
   (`say` through speakers with fillers → not counted; plain capture level
   unaffected in parallel).
7. Update README/config comments, version 1.7.0, backlog card.

## Decisions & Tradeoffs

| Decision | Rationale |
|---|---|
| Gate, not full AEC | Matt chose option 1: small, low-risk, no new native lib to sign; the tap helper is the reusable half of a later AEC. Cost: fillers spoken *while* remote audio plays are dropped. |
| Core Audio process tap in a Swift helper | Public API since 14.2, no virtual driver install, low latency. PyObjC coverage of CATapDescription/aggregate dicts is shaky; a ~150-line Swift binary is simpler and matches the existing compiled launcher. |
| Gate by word timestamps, not by whole utterance | An utterance often spans both speakers' turns; per-word keeps Matt's words that don't overlap. |
| Keep VPIO as opt-in only | Some users on FaceTime/Zoom-desktop (which use VPIO themselves) may prefer it; default must never mute the call. |

## Changes Made

| File | Change |
|---|---|
| plans/PLAN-2026-09-30-speaker-gate-echo-filter.md | This plan |
| fk-systap.swift (new) | Core Audio process tap helper: global private unmuted tap → private aggregate (clocked by default output) → prints dBFS per ~50 ms; `ready` line; exits on stdin EOF / signal (cleans up tap+aggregate), exit 3 on default-output change, exit 2 if taps unsupported. |
| coach.py | `SpeakerGate` thread (spawns/restarts helper, playback intervals, `busy()` with 0.15 s/0.45 s padding, -50 dBFS threshold); `kept_runs` + gated `process_block` (per-word drop for word + acoustic passes, runs matched separately, partial/airtime suppressed while playing); Listener stamps capture time in the PortAudio callback and maps Vosk stream seconds → monotonic; plain capture + gate is default, VPIO only via `echo_method: "voice_processing"`; diagnostic adds gate status/max dBFS and refreshes each minute; `FILLER_KILLER_ECHO=1` env for app debugging; settings/menu copy ("Ignore Speaker Audio"); `__version__` 1.7.0 (was stale 1.5.1). |
| make_app.sh | Compiles + Developer-ID-signs `Contents/MacOS/fk-systap`; `NSAudioCaptureUsageDescription` in Info.plist. |
| run.sh / .gitignore | Build helper from source when missing/stale; ignore the binary. |
| README.md / config.json | Speaker-gate docs, permission, tradeoff, why VPIO was dropped, `echo_method`. |

## Verification (2026-09-30)
- Offline (Vosk on a `say` WAV): no gate → fillers counted; all-playing → nothing emitted; playback only over sentence 1 → sentence 2 still counted (words, pace, "like"). Gate padding + clock mapping unit-checked.
- Standalone helper from a terminal-launched shell: tap starts but reads pure silence — TCC silently denies System Audio Recording to processes without the usage string. Must run inside the app.
- Local ad-hoc build launched via `open`: helper running, gate `active`; `say` filler speech through speakers → all words logged "ignored (speakers playing)", nothing counted.
- **Original bug:** parallel plain mic capture (Chrome-style) while FK runs: RMS ~1,500–1,970 during speech (vs ~1.4 under VPIO). Fixed.
- Quitting the app leaves no orphaned fk-systap.
- Not exercised: output-device switch restart (exit 3 path), macOS < 14.2 fallback, real human speech during/between remote speech on a live call.

## Follow-on Turns

| Turn / Date | What changed |
|---|---|
| 2026-10-01 | Matt's first live call still had the soft-voice problem — diagnostic showed `voice-processing (echo cancel)`: he'd launched the old v1.6.0 from /Applications (Dock), not the project-folder test build. Installed the test build to /Applications (`make_app.sh --install`); a second call worked. Lesson: always install test builds where the user actually launches from. |
| 2026-10-02 | Release. Found v1.6.0's launcher was built arm64-only with minos 27.0 (swiftc defaults to the build host) — it couldn't open on macOS 13–26 or Intel, despite the cask's `:ventura`. make_app.sh now builds launcher + helper universal with explicit `-target …-macos14.0` / `14.2` (embedded Python needs 14.0). Cask → `depends_on macos: ">= :sonoma"`. Committed 65cf9e6, tagged v1.7.0, signed, notarized (accepted first try), GitHub release, cask bumped (tap 6c51b09), `brew upgrade` on Matt's Mac. |

## Result
**Status:** Complete
**Backlog card:** fc13 moved to Done
**Notes:** Shipped as v1.7.0 (https://github.com/mattbakerpm/filler-killer/releases/tag/v1.7.0) together with the live pace readout. Verified on a real Teams-in-Chrome call (Matt: "worked"). Gotcha: during `brew upgrade`, an FK instance that launched mid-install logged "speaker gate unavailable: helper not found"; the relaunched app had the helper running normally.
