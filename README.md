<p align="center">
  <img src="assets/filler-killer-logo.svg" alt="Filler Killer — slay what you say" width="320">
</p>

# Filler Killer

**Slay what you say.** A tiny **local** macOS app that listens to your speech
in real time and shows a floating, always-on-top counter of filler words
("um", "uh", "you know", "like"...) while you're on a call — so you can
finally stop saying them.

- **100% local** — audio is captured from your mic and transcribed *on your
  Mac* by [Vosk](https://alphacephei.com/vosk/), an offline speech-to-text
  engine. No cloud, no API keys, no LLM, no telemetry. Nothing leaves your
  machine.
- **Real-time** — the overlay updates live as you speak, with a red flash on
  every slip.
- **App-agnostic** — works over any call app (Zoom, Meet, Teams, phone,
  Granola...) because it listens to your microphone, not to any app's
  transcript.
- **Counts only you** — words heard while your Mac is playing the other side
  of the call are ignored, so no headphones are required — and your mic is
  never altered, so the call hears you normally.

<p align="center">
<img width="326" height="443" alt="Filler Killer Screenshot" src="https://github.com/user-attachments/assets/989fb71d-9d06-4396-a24b-1bee9f8c821d" />
</p>

## Features

- **Big live counter** — flashes amber (red when your rate is high) on each
  slip; the background never changes.
- **Speaking score (0–100)** — a live composite of how clean you sound:
  - *density* (50%): fillers per 100 spoken words,
  - *spread* (20%): clustered slips score worse than isolated ones,
  - *airtime* (30%): long uninterrupted turns cost points (skipped when the
    airtime warning is off),
  - *pace* (20%): each stretch of talking over the wpm limit costs points
    (skipped when the pace warning is off).
  Appears after ~30 spoken words. Green ≥ 85, amber ≥ 65, red below.
- **Rate per minute**, color-coded: green < 4/min, amber 4–8, red ≥ 8.
- **Pace (wpm), live + call average** — "*172 wpm now*" is your speed over
  your last 10s of talking, updated while you speak. It drops to 0 ~3s
  after you stop, and a pause over 2s starts a fresh reading (shown as "…"
  for the first couple of seconds back), so it reflects how you're talking
  *now*, not the stretch before the pause. "*avg 151 wpm*" is your average for the whole call, so you can
  watch it come down after you slow down. Normal pauses (under 2s) count as
  talking time, the way a listener hears your pace; other people's turns
  don't. Conversational English is ~140–170 wpm; fast talkers slip more
  fillers because the mouth outruns the plan. Modes: **Off**, **Relaxed**
  (warn over 190), **Strict** (warn over 170). Crossing the limit shows
  "▲ SLOW DOWN · 205 wpm", flashes the counter red, and counts a *fast*
  episode in the stats line; the warning clears as soon as you slow down.
- **Timeline graph** — fillers per 30s interval, growing left → right and
  compressing so the whole call stays visible. At the end of a call you can see
  at a glance whether you tightened up.
- **Airtime guard** — warns "◼ WRAP IT UP" when you talk continuously past a
  limit. Modes: **Off** (presentations), **Short** (~30s, interviews — let them
  talk), **Medium** (~90s, meetings). A 2s pause resets the clock. The stats
  line shows your talking **turns**, **median turn length**, and **long-turn
  count** (turns under 3s — "yeah", "mm-hmm" — don't count).
- **Words accordion** — collapsed by default (shows your top offender inline);
  click to expand, always sorted highest count first.
- **Pause / End / Quit** buttons; drag the panel anywhere. **End** saves the
  session to History and starts a fresh one — and sessions also **auto-end
  after 3 minutes of silence** (your call ended; the next call gets its own
  session automatically).
- **Session history** (⌘Y) — sessions autosave every 30 seconds (once you've
  said ~30 words) to `~/Library/Application Support/FillerKiller/sessions/`.
  The History window charts your score over time and lists every session's
  length, words, fillers, rate, average wpm, and score — double-click a name to rename,
  select + Delete to remove. Watch yourself improve call over call.
- **In-app settings** (⌘, or the ⚙ button) — add/remove filler words, pick
  your mic, set the airtime and pace modes. Saved to `config.json`.
- **Dock app** — `FillerKiller.app` with a real menu bar
  (About / Settings / Session History / Quit).

## Install

### Homebrew (easiest)

```bash
brew install mattbakerpm/tap/filler-killer
filler-killer        # launches the floating overlay
```

Settings live in `~/Library/Application Support/FillerKiller/config.json`
(created on first run; the in-app ⚙ edits it).

### From source

Requires macOS 14 (Sonoma) or later — Apple silicon or Intel — + [Homebrew](https://brew.sh).

```bash
git clone https://github.com/mattbakerpm/filler-killer.git
cd filler-killer
./setup.sh    # installs portaudio, creates a venv, downloads the ~40MB model
```

## Run

```bash
./run.sh              # floating overlay
./run.sh --echo       # + prints everything it hears (great for tuning)
```

Or install it as a Dock app:

```bash
./make_app.sh --install   # builds FillerKiller.app + installs to /Applications + launches
```

The bundle is **self-contained** (own venv, model, code, and config — ~100MB),
so it works from anywhere and never reads your project folder at launch
(macOS privacy protection blocks Dock-launched apps from ~/Documents, so a
thin wrapper would silently die). Consequences:

- Right-click the Dock icon → **Options → Keep in Dock** to make it permanent.
- Allow **Microphone** on first launch.
- The app has its *own* `config.json`; use the in-app ⚙ to change settings.
- After changing `coach.py` or the repo config, re-run `./make_app.sh --install`.

## How "um" / "uh" are caught (dual-pass design)

Normal speech-to-text **cannot** hear "um"/"uh": the language model
"autocorrects" them into real words ("am", "i'm", "are") — true of cloud
transcription services, Whisper, and Vosk's normal mode alike. Bigger models
don't fix it; they share the same language-model bias. So Filler Killer runs
**two recognizers over the same audio**:

```
        ┌▶ Vosk word pass ──▶ phrase match ("you know", "like"...) ─┐
mic ────┤                                                           ├─▶ overlay
        └▶ Vosk grammar pass ["um","uh","[unk]"] ─▶ um/uh hits ─────┘
```

1. **Word pass** — normal decoding. Catches word fillers ("you know", "like",
   "i mean", "kind of"...) very reliably.
2. **Acoustic pass** — constrained to the grammar `["um", "uh", "[unk]"]`.
   With no real words available to autocorrect into, filled pauses decode as
   um/uh and all other speech falls into `[unk]`. Hits are filtered by duration
   and confidence. Tested: zero false positives on trap words ("am",
   "umbrella", "umpteen").

Counting happens on **final** results (stable); **partial** hypotheses drive
the live red flash.

## Excluding other people's voices (no headphones needed)

Filler Killer captures your mic plainly and runs a **speaker gate** next to
it: a tiny helper (`fk-systap`, a Core Audio process tap) measures how loud
your Mac's own playback is, and any words the mic picks up *while the Mac is
playing sound* (i.e. everyone else on the call, echoing off your speakers)
are ignored — not counted as fillers, not counted toward your talk time or
pace. Audio from the tap is only measured, never stored.

Why not echo cancellation? Up to v1.6, Filler Killer used macOS's
voice-processing engine. It removed speaker audio well, but while it ran it
also **muted the mic for other apps** — on a Teams/Meet call in the browser,
the other side could barely hear you (measured: about -60 dB). The gate
never touches the mic.

Notes:
- On by default. Toggle it from the **menu bar → Ignore Speaker Audio** or
  the Settings checkbox (`echo_cancel` in `config.json`).
- macOS asks once for **System Audio Recording** permission (System Settings
  → Privacy & Security → Screen & System Audio Recording). If it's denied,
  the gate hears nothing and other voices get counted — use headphones or
  grant it there.
- Tradeoff: fillers you say *while talking over someone* are skipped too.
- Works with any mic, including a pinned `mic_device`. Needs macOS 14.2+;
  on older macOS the gate is off (use headphones).
- It only knows about audio *this Mac* plays. Someone talking in the room
  with you is still heard (headphones can't fix that either).
- The old voice-processing mode is still available for apps that use it
  themselves (FaceTime): set `"echo_method": "voice_processing"` in
  `config.json`. Don't use it for browser calls.

## Configuration

Everything lives in `config.json` (editable in-app via ⚙, or by hand):

| Key | Meaning |
|-----|---------|
| `fillers` | Word fillers/phrases for the word pass (multi-word supported). |
| `acoustic_fillers` | Sounds for the acoustic pass (default `um`, `uh`). Must be single in-vocabulary words; adding more raises false-positive risk. |
| `echo_cancel` | Ignore words heard while the Mac plays audio — the speaker gate (default `true`). |
| `echo_method` | `speaker_gate` (default) or legacy `voice_processing` (mutes your mic for other apps — FaceTime only). |
| `mic_device` | `null` = system default, or a device index (`./run.sh --list-devices`). |
| `monologue` | Airtime guard: `mode` `off` / `short` / `medium`, plus the two thresholds in seconds. |
| `pace` | Pace guard: `mode` `off` / `relaxed` / `strict`, `relaxed_wpm` (190), `strict_wpm` (170), `window_seconds` (10, the live window). |
| `session.auto_end_minutes` | Silence minutes before a session auto-ends and saves (default 3, `0` disables). |
| `graph.bucket_seconds` | Timeline graph interval (default 30). |
| `window`, `alert` | Position, opacity, flash, rate window. |

## Accessibility

Built to WCAG 2.1 AA principles:

- **Contrast**: all text meets 4.5:1 against the panel background (measured,
  not eyeballed — the muted labels are 5.0:1, alerts 6.2:1, body text 15.7:1);
  graph elements meet the 3:1 non-text minimum.
- **Not color alone**: the mic status indicator changes shape as well as color
  (● hearing you · ○ listening, quiet · ✕ problem); rate/score severity is
  always also conveyed by the number itself.
- **Full keyboard access** via the menu bar: Settings **⌘,** · History **⌘Y**
  · Pause/Resume **⌘P** · End Session **⌘E** · Show/Hide Word List **⌘L** ·
  Quit **⌘Q**.
- **VoiceOver**: every icon-only button, value readout, and chart carries a
  descriptive accessibility label (e.g. the timeline reads as "Filler
  timeline: bar height is fillers per 30-second interval").

## Privacy

Offline by design. No network calls at runtime, no telemetry, no stored
recordings or transcripts (session *stats* — counts and scores, never audio or
text — are saved locally under `~/Library/Application Support/FillerKiller/`).
The only download is the Vosk model, once, during `setup.sh`.

## Why I built this

I say "um" and "you know" way too much — at a genuinely distracting level —
and I wanted live feedback during real calls, not a report afterwards, and
definitely not my meeting audio shipped to someone's cloud. If it helps you
slay what you say too, that's the whole point. Issues and PRs welcome.

— [Matt](https://github.com/mattbakerpm)

## License

[MIT](LICENSE)
