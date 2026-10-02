# Plan: Live pace readout (now + call average)

| | |
|---|---|
| **Date** | 2026-10-01 |
| **Status** | Complete |
| **Project** | filler-killer |
| **Backlog Card ID** | fc14 (BACKLOG.json) |

## Why
Matt's first real test of the v1.7.0 build (2026-10-01):
1. **Pace reads high.** He spoke ~144 wpm; FK showed 162–174.
2. **Pace lags ~20 s.** After he stopped talking it took ~20 s to fall to zero.
3. **Not useful as feedback.** "SLOW DOWN" fires, he slows down, and nothing
   visibly changes. He wants a *real-time* pace that confirms the slowdown,
   plus a call-long *average* that drifts down over the call.

Root causes:
- Pace = words ÷ sum of Vosk utterance spans (first word start → last word
  end). Vosk ends an utterance at ~0.5 s+ pauses, so the natural pauses
  between phrases are thrown out → measures articulation rate, not speaking
  rate. Measured on TTS with realistic 0.5–1.4 s pauses: true 154 wpm,
  current **190** (+23%, matches Matt's +13–21%); counting pauses ≤ 2 s as
  talking time gives **155**.
- The reading averages utterances *finished* in a trailing 30 s window, and
  only updates when an utterance finalizes (needs a pause) → slow to rise,
  ~30 s to fall.

## Scope
**In scope:**
- Per-word timing (mapped to wall clock) instead of per-utterance duration.
- Speaking-rate math: pauses ≤ 2 s count as talking; longer gaps (or other
  people talking) don't.
- **Live pace**: last ~10 s of your speech, updated from Vosk *partial*
  results while you talk (SetPartialWords), goes idle ("—") ~3 s after you
  stop.
- **Call average**: whole-session speaking rate, shown alongside.
- SLOW DOWN driven by live pace, clears as soon as live pace drops below the
  limit (10% hysteresis) or you stop.
- Panel row for "now" + "avg"; session history avg_wpm uses the new math.
- Self-test/snapshot hooks updated.

**Out of scope / later:**
- Pace-over-time graph.
- Calibration per user.

## Approach
1. Pure helpers: `talk_span(words)`, `live_wpm(words, now)` with constants
   PACE_PAUSE_MAX=2.0, LIVE_IDLE=3.0 (revised from 2.5 after measuring partial lag), live window from config (default 10 s).
2. `process_block`: emit word (start, end) in monotonic time for finals
   (`words` event) and current partial words (`live_words`), both gated.
   Listener passes its stream→monotonic clock; VP path stamps too.
3. UI: keep finalized word times (pruned) + current partial; compute live and
   call average each tick; new pace row; SLOW DOWN on live.
4. Verify: offline accuracy harness (TTS with pauses), timing of idle drop,
   self-test exercise, snapshot PNG of the panel.

## Decisions & Tradeoffs

| Decision | Rationale |
|---|---|
| Count pauses ≤ 2 s as talking | Listeners perceive pace including normal phrase pauses; 2 s matches the existing airtime gap. Verified 155 vs true 154 wpm. |
| Live window 10 s, idle at 3 s | Matt asked for 2–5 s responsiveness. Shorter windows get jumpy (few words); 10 s of speech ≈ 25 words → stable to ~±10 wpm. |
| Partial results drive the live number | Finals only arrive at pauses, so a long run-on would show nothing new until you breathe. |
| Two numbers, not one | "Now" gives immediate feedback; "avg" shows the trend over the call (what Matt asked for). |

## Changes Made

| File | Change |
|---|---|
| plans/PLAN-2026-10-01-live-pace-readout.md | This plan |
| coach.py — recognition | `rec_word.SetPartialWords(True)`; `process_block(clock=…)` emits `("words", n, [(start,end) mono])` for kept final words and `("live_words", [...])` for the in-progress utterance (gated words excluded; cleared on each final). Voice-processing path now stamps arrival times too. |
| coach.py — pace math | Removed `trailing_wpm`. New `talk_span` (words + pauses ≤ PACE_PAUSE_MAX 2 s) and `live_wpm(words, window)` — window anchored at the LAST word, not the clock (clock-anchored spiked 157→223 after stopping in testing → false SLOW DOWN). LIVE_IDLE 3.0 s, LIVE_MIN_WORDS 6, CALL_MIN_SPAN 15 s. |
| coach.py — UI | New pace row: "N wpm now" (left, colored vs limit) + "avg N wpm" (right). Rate row now rate + timer only. SLOW DOWN driven by live pace, re-arms below 90% of limit or when idle. Session `avg_wpm` = call average with new math. `_reset_pace/_add_pace_words/_live_pace/_call_pace/_show_pace` helpers. Self-test exercise + snapshot updated (exercise also asserts idle clears the alarm). |
| config.json / code defaults | `window_seconds` 30 → 10; `relaxed_wpm` 220 → 190, `strict_wpm` 180 → 170 (old limits were tuned on readings ~20% high). |
| README.md | Pace section + config table. |

## Verification (2026-10-01)
- Accuracy (TTS, 103 words, realistic 0.5–1.4 s pauses; true 154 wpm): old math 190, new call average **155**.
- Live timeline replayed block-by-block through `process_block`: first reading ~6 s in; readings track local pace (118–205 — the TTS genuinely speaks ~200 wpm between pauses); after speech stops the reading holds, then goes idle **~2.9 s** after the last word (was ~20–30 s).
- Partial words lag the audio by up to ~2 s → why LIVE_IDLE is 3.0 s, not 2. A "mic quiet" shortcut was considered and dropped: it depends on mic gain (rms > 400) and would flicker on quiet mics.
- `FILLER_COACH_EXERCISE` passes (EXERCISE OK); snapshot PNG shows the new row fitting the 280 px panel.
- Gotcha: the exercise writes config.json via save_config (re-escapes "—" as \u2014) — restore it after running locally.
- Not verified: Matt's real voice against a known pace.

## Follow-on Turns

| Turn / Date | What changed |
|---|---|
| 2026-10-01 — "multi-second pause should reset to zero" | `live_wpm` now starts the window after the most recent gap > PACE_PAUSE_MAX (2 s), so pre-pause words never blend into "now" (unit check: 302 wpm → 4 s pause → 120 wpm stretch reads 124, not a blend). Idle shows **"0 wpm now"** (was "—"); "… wpm now" while warming up after a restart (< 6 words or < LIVE_MIN_SPAN 3 s). `_live_pace` returns 0.0 idle / None warming. Exercise extended with the restart case; EXERCISE OK. |

## Result
**Status:** Complete
**Backlog card:** fc14 moved to Done
**Notes:** Shipped in v1.7.0 (2026-10-02) with the speaker gate; Matt tested the build on a real call before release. Follow-up ideas (not done): pace-over-time graph, per-user calibration.
