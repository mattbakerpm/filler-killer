# Filler Coach

A tiny **local** macOS app that listens to your speech in real time and shows a
floating, always-on-top counter of filler words ("um", "uh", "you know",
"like"...) while you're on a call — so you can finally stop saying them.

- **100% local** — audio is captured from your mic and transcribed *on your
  Mac* by [Vosk](https://alphacephei.com/vosk/), an offline speech-to-text
  engine. No cloud, no API keys, no LLM, no telemetry. Nothing leaves your
  machine.
- **Real-time** — the overlay updates live as you speak, with a red flash on
  every slip.
- **App-agnostic** — works over any call app (Zoom, Meet, Teams, phone,
  Granola...) because it listens to your microphone, not to any app's
  transcript.

## Features

- **Big live counter** + per-filler breakdown (all your configured fillers).
- **Rate per minute**, color-coded: green < 4/min, amber 4–8, red ≥ 8.
- **Timeline graph** — fillers per 30s interval, growing left → right and
  compressing so the whole call stays visible. At the end of a call you can see
  at a glance whether you tightened up.
- **Monologue warning** — if you talk continuously past a limit, the overlay
  warns you to wrap it up. Modes: **Off** (presentations), **Short** (~30s,
  interviews — let them talk), **Medium** (~90s, meetings). A 2s pause resets
  the clock.
- **Pause / Reset / Quit** buttons; drag the panel anywhere.
- **In-app settings** (⚙) — add/remove filler words, pick your mic, set the
  monologue mode. Saved to `config.json`.
- **Dock app** — build `FillerCoach.app` and launch it like any other app.

## Install (one time)

Requires macOS + [Homebrew](https://brew.sh).

```bash
git clone <this repo>
cd filler-coach
./setup.sh    # installs portaudio, creates a venv, downloads the ~40MB model
```

## Run

```bash
./run.sh              # floating overlay
./run.sh --echo       # + prints everything it hears (great for tuning)
```

Or build the Dock app:

```bash
./make_app.sh         # builds FillerCoach.app in this folder
```

Drag `FillerCoach.app` to `/Applications` or your Dock. First launch:
right-click → Open (it's unsigned), then allow **Microphone** access.
Rebuild the app if you move the project folder (the launcher embeds the path).

## How "um" / "uh" are caught (dual-pass design)

Normal speech-to-text **cannot** hear "um"/"uh": the language model
"autocorrects" them into real words ("am", "i'm", "are") — true of cloud
transcription services, Whisper, and Vosk's normal mode alike. Bigger models
don't fix it; they share the same language-model bias. So Filler Coach runs
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

## Excluding other people's voices

Filler Coach counts whatever the microphone hears. On a call:

- **Wear headphones** (recommended) — the other side's audio then never reaches
  your mic, so only *your* speech is counted.
- Or enable macOS **Voice Isolation** for your mic (Control Center → Mic Mode
  while on a call) to suppress non-voice background.
- On speakers, some remote-voice bleed-through into your counts is possible.

## Configuration

Everything lives in `config.json` (editable in-app via ⚙, or by hand):

| Key | Meaning |
|-----|---------|
| `fillers` | Word fillers/phrases for the word pass (multi-word supported). |
| `acoustic_fillers` | Sounds for the acoustic pass (default `um`, `uh`). Must be single in-vocabulary words; adding more raises false-positive risk. |
| `display_priority` | Which fillers show in the breakdown. `[]` = show all; the window sizes itself. |
| `mic_device` | `null` = system default, or a device index (`./run.sh --list-devices`). |
| `monologue` | `mode`: `off` / `short` / `medium`, plus the two thresholds in seconds. |
| `graph.bucket_seconds` | Timeline graph interval (default 30). |
| `window`, `alert` | Position, opacity, flash, rate window. |

## Privacy

Offline by design. No network calls at runtime, no telemetry, no stored
recordings or transcripts. The only download is the Vosk model, once, during
`setup.sh`.

## License

[MIT](LICENSE)
